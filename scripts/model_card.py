#!/usr/bin/env python3
"""Phase 44 α (E1): the model card of a finished run.

    python3 scripts/model_card.py <run_dir> [--no-evaluate] [--timeout S]
    python3 scripts/model_card.py --self-test

A finished run leaves a model that can answer more questions than the report
asks (attempt 8 answered its alternative budget envelopes with its own
`outcome_fn`), but nothing recorded what the model can be called with, over
which range it was simulated or calibrated, or how well it was validated.
This script writes `model_card.yaml` (and a short `model_card.md`) from the
run's own artifacts, the ones the gate already reads. Nothing in it is
written by an agent, and nothing is invented:

* interface: `models/outcome_fn.py::outcome_fn` (the function the uncertainty
  harness runs), loaded in a fresh process with the run's `models/` on the
  path (propagate_uncertainty's loader, no cached bytecode, run-import
  isolation) and evaluated once at the registry point estimates; its inputs
  (the citations.md Parameter Registry names, plus any declared in
  `models/model_domain.yaml`), its outputs at the point estimates, and its
  source fingerprint compared with the one `uncertainty_report.yaml` records.
  The per-unit function (`models/eligibility_manifest.yaml: outcome_fn`) is
  evaluated at the manifest's baseline inputs when declared.
* valid domain: each input's range. A range the run declares in
  `models/model_domain.yaml` (the range it simulated, calibrated or fitted an
  emulator over) wins; otherwise the registry prior's 95% interval
  (ci_low..ci_high). An input with neither has no established range and
  cannot be changed by `scripts/ask.py`.
* validation (benchmark match, held-out calibration, identifiability,
  declared validation files such as an emulator check), uncertainty draws
  available, environment (the third-party distributions the model imported
  and the repo's pins), known limits (scope declarations, open findings at
  the final gate) and provenance (run id, commits, stored blob, files not
  collected).

Fail closed: when the interface cannot be loaded or evaluated, the model code
is not the code behind uncertainty_report.yaml, model_domain.yaml is
malformed, or no input has an established range, `status: not_usable` and
`not_usable_reasons` say why; `scripts/ask.py` then refuses.

`models/model_domain.yaml` (optional, written by the modeler):

    parameters:                 # interface inputs (outcome_fn params keys)
      <name>: {min: 0.2, max: 0.9, basis: "range the emulator was fitted over (models/<grid> inputs)"}
    covariates:                 # per-unit inputs the model was simulated over (recorded, not settable)
      <column>: {min: 0.0, max: 1.0, basis: "..."}
    whatif_interface:           # optional: map ask's --budget / --objective to outcome_fn params keys
      arguments:
        budget: {param: <params key>}
        objective: {param: <params key>, choices: [<output name>, ...]}
    validation:                 # optional: label -> run-relative file with validation results
      emulator: models/<file>.json
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CARD = "model_card.yaml"
CARD_MD = "model_card.md"
DOMAIN = os.path.join("models", "model_domain.yaml")
OUTCOME_FN_SPEC = "models/outcome_fn.py::outcome_fn"
WHATIF_ARGS = ("budget", "objective")
DEFAULT_TIMEOUT_S = 900
_SECRET_RE = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|^AZ_|^AZURE_|^MA_COMPUTE_SCHEDULER$|^COILED", re.I)


def _sibling(name: str):
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    return __import__(name)


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def _read_yaml(path: str):
    """(doc, error). A missing file is (None, None)."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path) as f:
            return yaml.safe_load(f), None
    except (OSError, yaml.YAMLError) as e:
        return None, f"{os.path.basename(path)} cannot be read ({type(e).__name__}: {str(e)[:120]})"


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _short(s, n=240) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------- run inputs

def load_registry(run_dir: str) -> tuple[list[dict] | None, str | None]:
    esr = _sibling("effect_size_registry")
    try:
        params = esr.load_priors(os.path.join(run_dir, "citations.md")).get("parameters") or []
    except Exception as e:  # noqa: BLE001 — fail closed
        return None, f"the citations.md Parameter Registry cannot be read ({type(e).__name__}: {str(e)[:150]})"
    params = [p for p in params if isinstance(p, dict) and p.get("name")]
    if not params:
        return None, "the citations.md Parameter Registry has no parameters (no interface inputs to vary)"
    return params, None


def _range_entry(d, where: str) -> tuple[dict | None, str | None]:
    if not isinstance(d, dict):
        return None, f"{where} must be a mapping with min, max and basis"
    lo, hi = _num(d.get("min")), _num(d.get("max"))
    if lo is None or hi is None or lo > hi:
        return None, f"{where} needs numeric min <= max"
    if not str(d.get("basis") or "").strip():
        return None, f"{where} needs a basis (what was simulated or fitted over this range)"
    return {"min": lo, "max": hi, "basis": _short(d["basis"], 300)}, None


def load_model_domain(run_dir: str) -> tuple[dict | None, list[str]]:
    """(normalised doc or None when absent, errors). Errors make the card not usable."""
    doc, err = _read_yaml(os.path.join(run_dir, DOMAIN))
    if err:
        return None, [err]
    if doc is None:
        return None, []
    if not isinstance(doc, dict):
        return None, [f"{DOMAIN} must be a mapping"]
    errs, out = [], {"parameters": {}, "covariates": {}, "whatif_arguments": {}, "validation": {}}
    for sec in ("parameters", "covariates"):
        m = doc.get(sec) or {}
        if not isinstance(m, dict):
            errs.append(f"{DOMAIN}: `{sec}:` must map names to {{min, max, basis}}")
            continue
        for name, d in m.items():
            e, why = _range_entry(d, f"{DOMAIN}: {sec}.{name}")
            if why:
                errs.append(why)
            else:
                out[sec][str(name)] = e
    wi = doc.get("whatif_interface") or {}
    args = wi.get("arguments") if isinstance(wi, dict) else None
    if wi and not isinstance(args, dict):
        errs.append(f"{DOMAIN}: `whatif_interface.arguments:` must map {', '.join(WHATIF_ARGS)} to a params key")
    for k, d in (args or {}).items():
        if k not in WHATIF_ARGS:
            errs.append(f"{DOMAIN}: whatif_interface.arguments.{k} is not one of {', '.join(WHATIF_ARGS)}")
        elif not isinstance(d, dict) or not str(d.get("param") or "").strip():
            errs.append(f"{DOMAIN}: whatif_interface.arguments.{k} needs `param:` (the outcome_fn params key)")
        elif k == "objective" and (not isinstance(d.get("choices"), list) or not d["choices"]):
            errs.append(f"{DOMAIN}: whatif_interface.arguments.objective needs `choices:` (the allowed values)")
        else:
            out["whatif_arguments"][k] = {"param": str(d["param"]).strip(),
                                          **({"choices": [str(c) for c in d["choices"]]} if k == "objective" else {})}
    val = doc.get("validation") or {}
    if not isinstance(val, dict):
        errs.append(f"{DOMAIN}: `validation:` must map a label to a run-relative file")
    else:
        rd = os.path.realpath(run_dir)
        for k, p in val.items():
            real = os.path.realpath(os.path.join(run_dir, str(p)))
            if not real.startswith(os.path.join(rd, "")):
                errs.append(f"{DOMAIN}: validation.{k} ({p}) is outside the run directory")
            else:
                out["validation"][str(k)] = str(p)
    return out, errs


def point_params(registry: list[dict]) -> dict:
    return _sibling("propagate_uncertainty").point_params({"parameters": registry})


def domain(registry: list[dict], md: dict | None) -> dict:
    """Each interface input's range: model_domain.yaml first, else the registry prior's 95% interval."""
    params, missing = {}, []
    for p in registry:
        name = str(p["name"])
        lo, hi = _num(p.get("ci_low")), _num(p.get("ci_high"))
        e = {"point": _num(p.get("value"))}
        if lo is not None and hi is not None and lo <= hi:
            e.update({"min": lo, "max": hi, "source": "registry_ci",
                      "basis": f"prior 95% interval in citations.md ({p.get('kind') or 'no kind'}, source "
                               f"{p.get('source') or 'none'})"})
        params[name] = e
    for name, d in ((md or {}).get("parameters") or {}).items():
        e = params.setdefault(name, {"point": None})
        if "min" in e:
            e["registry_ci"] = [e["min"], e["max"]]
        e.update({"min": d["min"], "max": d["max"], "source": "model_domain", "basis": d["basis"]})
    for name, e in params.items():
        if "min" not in e:
            e["source"] = "not_established"
            missing.append(name)
    return {"parameters": params, "not_established": sorted(missing),
            "covariates": (md or {}).get("covariates") or {},
            "covariates_note": None if (md or {}).get("covariates") else
            f"no covariate ranges declared ({DOMAIN} absent or has no `covariates:`)"}


DIGEST_FILES = ("citations.md", DOMAIN, "uncertainty_report.yaml")


def inputs_digest(run_dir: str) -> dict:
    """sha256 of the files that define the card's interface inputs, domain and draws
    (None when absent). scripts/ask.py refuses a card whose digest no longer matches."""
    return {rel: _sha(os.path.join(run_dir, rel)) for rel in DIGEST_FILES}


def static_fingerprint_check(run_dir: str) -> tuple[bool | None, str | None]:
    """Do the source files uncertainty_report.yaml fingerprinted still hash the same?
    (None, why) when there is no report or no fingerprint. The worker's check also
    catches modules the function newly imports."""
    doc, err = _read_yaml(os.path.join(run_dir, "uncertainty_report.yaml"))
    if err:
        return False, err
    rec = doc.get("outcome_fn_fingerprint") if isinstance(doc, dict) else None
    if doc is None or not isinstance(rec, dict) or not isinstance(rec.get("sources"), dict):
        return None, "uncertainty_report.yaml is absent or records no outcome_fn fingerprint"
    changed = [k for k, h in rec["sources"].items() if _sha(os.path.join(run_dir, k)) != h]
    if changed:
        return False, f"model code changed since uncertainty_report.yaml was produced: {', '.join(sorted(changed)[:5])}"
    return True, None


def fingerprint_mismatch(report_fp, fp) -> str | None:
    if not isinstance(report_fp, dict) or not isinstance(report_fp.get("sources"), dict):
        return None
    if not isinstance(fp, dict):
        return "the function's fingerprint could not be computed"
    if (report_fp.get("file"), report_fp.get("function")) != (fp.get("file"), fp.get("function")):
        return (f"uncertainty_report.yaml was produced by {report_fp.get('file')}::{report_fp.get('function')}, "
                f"not {fp.get('file')}::{fp.get('function')}")
    if report_fp["sources"] != fp.get("sources"):
        diff = sorted(k for k in set(report_fp["sources"]) | set(fp.get("sources") or {})
                      if report_fp["sources"].get(k) != (fp.get("sources") or {}).get(k))
        return ("the model code on disk is not the code behind uncertainty_report.yaml "
                f"({', '.join(diff[:5])} differ)")
    return None


# --------------------------------------------------------------------------- the worker (fresh process)

def _jsonable_outputs(res) -> tuple[dict | None, list[str]]:
    if not isinstance(res, dict):
        return None, []
    out, skipped = {}, []
    for k, v in res.items():
        if hasattr(v, "item") and not isinstance(v, (str, bytes)):
            try:
                v = v.item()
            except (TypeError, ValueError):
                pass
        if isinstance(v, bool) or isinstance(v, str):
            out[str(k)] = v
        elif isinstance(v, (int, float)):
            out[str(k)] = float(v) if math.isfinite(v) else None
        else:
            skipped.append(str(k))
    return out, skipped


def _load_harness(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _third_party_versions(run_dir: str) -> dict:
    from importlib import metadata
    try:
        pkg_map = metadata.packages_distributions()
    except Exception:  # noqa: BLE001
        pkg_map = {}
    rd = os.path.join(os.path.realpath(run_dir), "")
    out = {}
    for name, m in list(sys.modules.items()):
        top = name.split(".")[0]
        f = getattr(m, "__file__", None) or ""
        if "." in name or top in sys.stdlib_module_names or not f or os.path.realpath(f).startswith(rd) \
                or os.path.realpath(f).startswith(os.path.join(HERE, "")):
            continue
        for dist in pkg_map.get(top, []):
            try:
                out[dist] = metadata.version(dist)
            except Exception:  # noqa: BLE001
                pass
    return dict(sorted(out.items()))


def _unrecorded_run_modules(run_dir: str, sources: dict) -> list[str]:
    """Run modules loaded in this process that `sources` does not record, or whose bytes differ."""
    rd = os.path.realpath(run_dir)
    ad = os.path.abspath(run_dir)
    out = []
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if not f:
            continue
        a = os.path.abspath(f)
        if any(a.startswith(os.path.join(x, "")) for x in (ad, rd)) \
                and not os.path.realpath(f).startswith(os.path.join(rd, "")):
            out.append(f"{os.path.basename(a)} (a symlink to code outside the run)")
            continue
        if not os.path.realpath(f).startswith(os.path.join(rd, "")):
            continue
        rel = os.path.relpath(os.path.realpath(f), rd)
        if rel not in sources:
            out.append(f"{rel} (not recorded)")
        elif _sha(os.path.realpath(f)) != sources[rel]:
            out.append(f"{rel} (changed)")
    return sorted(set(out))


def _worker(job_path: str, out_path: str) -> int:
    """Run inside `python -I -B`: load the run's function(s) and evaluate the job's params lists."""
    with open(job_path) as f:
        job = json.load(f)
    run_dir = job["run_dir"]
    res: dict = {"python": sys.version.split()[0], "evals": {}}
    pu = _load_harness("propagate_uncertainty")
    hb = _load_harness("headline_binding")
    sys.path[:] = [p for p in sys.path if os.path.realpath(p or ".") != os.path.realpath(HERE)]
    with pu.no_cached_bytecode(), hb._run_imports(run_dir), contextlib.redirect_stdout(sys.stderr):
        t0 = time.time()
        try:
            fn, fp = pu.load_outcome_fn_fingerprinted(run_dir, job["spec"], job.get("point"))
            res["fingerprint"] = fp
            try:
                res["signature"] = f"{getattr(fn, '__name__', 'fn')}{inspect.signature(fn)}"
            except (TypeError, ValueError):
                res["signature"] = None
            res["doc"] = _short(inspect.getdoc(fn) or getattr(sys.modules.get(getattr(fn, "__module__", "")),
                                                              "__doc__", "") or "", 600)
        except Exception as e:  # noqa: BLE001 — fail closed
            res["load_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            fn = None
        res["load_s"] = round(time.time() - t0, 2)
        why = fingerprint_mismatch(job.get("expected_fingerprint"), res.get("fingerprint")) if fn else None
        if why:
            res["fingerprint_mismatch"] = why
        if fn is not None and not why:
            for name, plist in job.get("evals", {}).items():
                rows = []
                for p in plist:
                    t = time.time()
                    try:
                        o, skipped = _jsonable_outputs(fn(dict(p)))
                        rows.append({"ok": o is not None, "out": o, "skipped": skipped,
                                     "s": round(time.time() - t, 3),
                                     **({} if o is not None else {"error": "outcome_fn did not return a dict"})})
                    except Exception as e:  # noqa: BLE001
                        rows.append({"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}",
                                     "s": round(time.time() - t, 3)})
                res["evals"][name] = rows
            # Codex review: a module imported only on a branch the queries take is not in the
            # recorded code; every run module loaded now must be recorded, with the same bytes.
            exp = (job.get("expected_fingerprint") or res.get("fingerprint") or {}).get("sources")
            if isinstance(exp, dict):
                late = _unrecorded_run_modules(run_dir, exp)
                if late:
                    res["fingerprint_mismatch"] = (
                        "the evaluation ran model code that is not the recorded code: "
                        + ", ".join(late[:5]))
                    res["evals"] = {}
        unit = job.get("unit")
        if unit:
            ur = {"spec": unit["spec"]}
            try:
                ufn = pu.load_outcome_fn(run_dir, unit["spec"])
                try:
                    ur["signature"] = f"{getattr(ufn, '__name__', 'fn')}{inspect.signature(ufn)}"
                except (TypeError, ValueError):
                    ur["signature"] = None
                ur["doc"] = _short(inspect.getdoc(ufn) or "", 400)
                if unit.get("inputs") is not None:
                    inp = dict(unit["inputs"])
                    o = ufn(**inp) if unit.get("call_style") == "kwargs" else ufn(inp)
                    ur["outputs"], _ = _jsonable_outputs(o)
                    if ur["outputs"] is None:
                        ur["error"] = "did not return a dict"
            except Exception as e:  # noqa: BLE001
                ur["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            res["unit"] = ur
        res["third_party"] = _third_party_versions(run_dir)
    with open(out_path, "w") as f:
        json.dump(res, f, default=str)
    return 0


def _worker_env() -> dict:
    """The model runs without credentials or a compute scheduler: no cloud, no API calls."""
    return {k: v for k, v in os.environ.items() if not _SECRET_RE.search(k)}


def run_worker(run_dir: str, job: dict, timeout: float | None = DEFAULT_TIMEOUT_S) -> dict:
    """Evaluate `job` in a fresh interpreter (isolated mode, no bytecode). Never raises:
    a crash or timeout comes back as {"worker_error": ...}."""
    with tempfile.TemporaryDirectory(prefix="model-card-") as td:
        jp, op = os.path.join(td, "job.json"), os.path.join(td, "out.json")
        with open(jp, "w") as f:
            json.dump({"run_dir": os.path.abspath(run_dir), **job}, f)
        try:
            p = subprocess.run([sys.executable, "-I", "-B", os.path.abspath(__file__), "--worker", jp, op],
                               cwd=os.path.abspath(run_dir), env=_worker_env(), capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"worker_error": f"the model did not finish within {timeout:.0f} s"}
        if p.returncode != 0 or not os.path.exists(op):
            tail = (p.stderr or "").strip().splitlines()[-3:]
            return {"worker_error": f"the model process exited {p.returncode}: {_short(' | '.join(tail), 400)}"}
        with open(op) as f:
            return json.load(f)


# --------------------------------------------------------------------------- card sections

def _brief(run_dir: str) -> dict:
    signed = os.path.join(run_dir, "decision_brief.signed", "decision_brief.yaml")
    path = signed if os.path.exists(signed) else os.path.join(run_dir, "decision_brief.yaml")
    doc, err = _read_yaml(path)
    so = _read_json(os.path.join(run_dir, "decision_brief.signoff.json")) or {}
    if not isinstance(doc, dict):
        return {"available": False, "note": err or "no decision_brief.yaml"}
    out = {"available": True, "file": os.path.relpath(path, run_dir),
           "signed": bool(so.get("signed_at")), "signed_by": so.get("signed_by"), "signed_at": so.get("signed_at")}
    for k in ("decision", "decision_maker", "objective"):
        if doc.get(k):
            out[k] = _short(doc[k], 400)
    out["answers"] = [{"id": a.get("id"), "statement": _short(a.get("statement"), 200)}
                      for a in doc.get("answers") or [] if isinstance(a, dict)]
    out["comparators"] = [{"id": c.get("id"), "role": c.get("role"),
                           "description": _short(c.get("description") or c.get("text") or c.get("name"), 160)}
                          for c in doc.get("comparators") or [] if isinstance(c, dict)]
    out["n_constraints"] = len(doc.get("constraints") or [])
    return out


def _scalars(d, limit=25) -> dict:
    out = {}
    for k, v in (d or {}).items() if isinstance(d, dict) else []:
        if isinstance(v, (str, bool)) or _num(v) is not None:
            out[str(k)] = _short(v, 160) if isinstance(v, str) else v
        if len(out) >= limit:
            break
    return out


def _validation(run_dir: str, md: dict | None) -> dict:
    out: dict = {}
    bm, err = _read_yaml(os.path.join(run_dir, "benchmark_match.yaml"))
    if isinstance(bm, dict):
        res = [r for r in bm.get("results") or [] if isinstance(r, dict)]
        out["benchmarks"] = {"file": "benchmark_match.yaml", "verdict": bm.get("verdict"),
                             "n_benchmarks": bm.get("n_benchmarks", len(res)),
                             "n_within_band": sum(1 for r in res if r.get("within_band") is True),
                             "n_drifted": bm.get("n_drifted"),
                             "outside_band": [r.get("id") for r in res if r.get("within_band") is False][:10]}
    else:
        out["benchmarks"] = {"available": False, "note": err or "no benchmark_match.yaml (gate-produced)"}
    cr, err = _read_yaml(os.path.join(run_dir, "models", "calibration_result.yaml"))
    if isinstance(cr, dict):
        ho = cr.get("held_out_fold")
        out["calibration"] = {"file": "models/calibration_result.yaml",
                              **{k: cr.get(k) for k in ("method", "n_restarts", "n_iterations", "best_loss",
                                                        "n_targets_fit", "n_params") if k in cr},
                              "held_out": ({"n_held_out": len(ho.get("indices") or []),
                                            **_scalars({k: v for k, v in ho.items() if k != "indices"})}
                                           if isinstance(ho, dict) else None)}
    else:
        out["calibration"] = {"available": False, "note": err or "no models/calibration_result.yaml"}
    idf, err = _read_yaml(os.path.join(run_dir, "identifiability.yaml"))
    if isinstance(idf, dict):
        out["identifiability"] = {"file": "identifiability.yaml", "verdict": idf.get("verdict"),
                                  "n_parameters": idf.get("n_parameters"), "status_counts": idf.get("status_counts")}
    else:
        out["identifiability"] = {"available": False, "note": err or "no identifiability.yaml (harness-produced)"}
    probe, _ = _read_yaml(os.path.join(run_dir, "models", "eligibility_probe.yaml"))
    if isinstance(probe, dict):
        out["eligibility_probe"] = {"verdict": probe.get("verdict"), "n_findings": len(probe.get("findings") or [])}
    declared = {}
    for label, rel in ((md or {}).get("validation") or {}).items():
        p = os.path.join(run_dir, rel)
        if not os.path.exists(p):
            declared[label] = {"file": rel, "available": False, "note": "declared file not found"}
        elif rel.endswith(".json"):
            declared[label] = {"file": rel, **_scalars(_read_json(p))}
        elif rel.endswith((".yaml", ".yml")):
            declared[label] = {"file": rel, **_scalars(_read_yaml(p)[0])}
        else:
            with open(p, errors="replace") as f:
                declared[label] = {"file": rel, "n_lines": sum(1 for _ in f)}
    out["declared"] = declared or None
    return out


def _uncertainty(run_dir: str) -> dict:
    doc, err = _read_yaml(os.path.join(run_dir, "uncertainty_report.yaml"))
    if not isinstance(doc, dict):
        return {"available": False, "note": err or "no uncertainty_report.yaml"}
    return {"available": True, "file": "uncertainty_report.yaml",
            **{k: doc.get(k) for k in ("n_draws", "n_successful", "n_failed", "seed", "backend", "producer")},
            "degenerate_outputs": doc.get("degenerate_outputs") or [],
            "scalar_outputs": sorted((doc.get("scalar_outputs") or {}).keys()),
            "draws_reproducible": "the draws are regenerated from the registry with this seed and n_draws by "
                                  "scripts/effect_size_registry.py::sample_prior (scripts/ask.py checks they match)"}


def _environment(wres: dict | None) -> dict:
    pins = {}
    try:
        with open(os.path.join(REPO, "requirements.txt")) as f:
            for line in f:
                m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*==\s*([^\s#]+)", line)
                if m:
                    pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    except OSError:
        pass
    used = (wres or {}).get("third_party") or {}
    rows = {d: {"installed": v, "pinned": pins.get(d.lower().replace("_", "-"))} for d, v in used.items()}
    return {"python": (wres or {}).get("python") or sys.version.split()[0],
            "model_imports": rows or None,
            "unpinned_or_different": sorted(d for d, r in rows.items() if r["pinned"] != r["installed"]),
            "requirements_sha256": _sha(os.path.join(REPO, "requirements.txt")),
            "note": None if rows else "third-party imports are recorded when the interface is evaluated"}


def _limits(run_dir: str) -> dict:
    out: dict = {}
    sd, err = _read_yaml(os.path.join(run_dir, "scope_declaration.yaml"))
    decl = sd.get("declarations") if isinstance(sd, dict) else None
    out["scope_declarations"] = [{"id": d.get("id"), "finding": d.get("finding"),
                                  "scope": _short(d.get("scope_declared"), 300)}
                                 for d in decl or [] if isinstance(d, dict)]
    na = sd.get("not_applicable") if isinstance(sd, dict) else None
    if na:
        out["not_applicable"] = [_short(x if not isinstance(x, dict) else x.get("artifact") or x, 120) for x in na]
    if err:
        out["scope_note"] = err
    gate = _read_json(os.path.join(run_dir, "final_gate.json"))
    out["final_gate_action"] = gate.get("action") if isinstance(gate, dict) else None
    if isinstance(gate, dict):
        if gate.get("rationale"):
            out["final_gate_rationale"] = _short(gate["rationale"], 400)
        if gate.get("schema_errors") or gate.get("error"):
            out["final_gate_error"] = _short(gate.get("schema_errors") or gate.get("error"), 300)
    op = _read_json(os.path.join(run_dir, "open_problems.json"))
    items = op.get("items") if isinstance(op, dict) else None
    if isinstance(items, list):
        src, rows = "open_problems.json", [{"id": i.get("id"), "severity": i.get("severity"),
                                            "source": i.get("source"), "claim": _short(i.get("claim"), 240)}
                                           for i in items if isinstance(i, dict)]
    elif isinstance(gate, dict):
        src = "final_gate.json"
        rows = [{"id": i.get("id") or i.get("kind"), "severity": i.get("severity") or "HIGH",
                 "source": i.get("reviewer") or i.get("stage"), "claim": _short(i.get("claim"), 240)}
                for i in (gate.get("unresolved_high") or []) + (gate.get("rigor_violations") or [])
                if isinstance(i, dict)]
    else:
        src, rows = None, []
    out["open_findings"] = {"source": src, "n": len(rows),
                            "n_high": sum(1 for r in rows if str(r.get("severity")).upper() == "HIGH"),
                            "items": rows}
    return out


def _git_head() -> str | None:
    try:
        return subprocess.check_output(["git", "-C", REPO, "rev-parse", "--short=7", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL, timeout=10).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _provenance(run_dir: str, stored_blob: str | None, run_commit: str | None) -> dict:
    meta = _read_json(os.path.join(run_dir, "metadata.json")) or {}
    # A collected run (cloud/run_cluster.py collect): <dest>/result.json, two levels up.
    rj = _read_json(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(run_dir))), "result.json"))
    run_id = meta.get("run_dir") or os.path.basename(os.path.abspath(run_dir))
    rj = rj if isinstance(rj, dict) and rj.get("run_dir") == run_id else None
    skipped = [{"file": os.path.relpath(p, os.path.join("runs", rj.get("run_dir") or "")) if rj.get("run_dir") else p,
                "bytes": n} for p, n in (rj or {}).get("skipped_large_files") or []]
    missing = []
    for root, _, files in os.walk(os.path.join(run_dir, "models")):
        for fn in files:
            if fn.endswith(".summary.json") and not os.path.exists(os.path.join(root, fn[: -len(".summary.json")])):
                missing.append(os.path.relpath(os.path.join(root, fn[: -len(".summary.json")]), run_dir))
    return {"run_id": run_id,
            "run_status": meta.get("status"), "started": meta.get("started"), "completed": meta.get("completed"),
            "total_cost_usd": meta.get("total_cost_usd"),
            "run_commit": run_commit or (rj or {}).get("commit"),
            "card_built_with_commit": _git_head(),
            "harness_manifest_sha256": _sha(os.path.join(run_dir, "harness_manifest.json")),
            "stored_blob": stored_blob or meta.get("stored") or (rj or {}).get("run_stored"),
            **({"stored_blob_note": "written at finalize, before the upload: metadata.json `stored` / "
                                    "`store_error` records whether it succeeded"} if stored_blob else {}),
            "files_not_collected": skipped,
            "compute_results_missing": sorted(set(missing) - {f["file"] for f in skipped})}


def _unit_job(run_dir: str) -> dict | None:
    m, _ = _read_yaml(os.path.join(run_dir, "models", "eligibility_manifest.yaml"))
    if not isinstance(m, dict) or not m.get("outcome_fn"):
        return None
    inputs = m.get("baseline_inputs") if isinstance(m.get("baseline_inputs"), dict) else None
    if inputs is None and m.get("baseline_inputs_csv"):
        import csv
        try:
            with open(os.path.join(run_dir, str(m["baseline_inputs_csv"])), newline="") as f:
                rows = list(csv.DictReader(f))
            ec = _sibling("effect_concordance")
            inputs = {k: ec._coerce(v) for k, v in rows[int(m.get("baseline_row") or 0)].items()}
        except Exception:  # noqa: BLE001 — recorded as not evaluated
            inputs = None
    return {"spec": str(m["outcome_fn"]), "call_style": str(m.get("call_style") or "dict"), "inputs": inputs}


def _headline(run_dir: str, point_out: dict | None) -> dict | None:
    doc, _ = _read_yaml(os.path.join(run_dir, "models", "headline_binding.yaml"))
    outs = doc.get("outputs") if isinstance(doc, dict) else None
    if not isinstance(outs, dict):
        return None
    ec = _sibling("effect_concordance")
    rows = {}
    for key, cid in outs.items():
        cv, _ = ec._ledger_value(run_dir, str(cid))
        pv = _num((point_out or {}).get(key))
        rows[str(key)] = {"claim": str(cid), "claim_value": cv, "point_value": pv,
                          "reproduces_claim": (None if cv is None or pv is None else
                                               abs(pv - cv) <= 0.001 * max(abs(cv), 1e-12))}
    return rows


# --------------------------------------------------------------------------- build / write

def build(run_dir: str, evaluate: bool = True, timeout: float | None = DEFAULT_TIMEOUT_S,
          stored_blob: str | None = None, run_commit: str | None = None) -> dict:
    """The model card. evaluate=False skips the worker (the static sections and the
    domain only; scripts/ask.py evaluates in its own worker)."""
    reasons: list[str] = []
    meta = _read_json(os.path.join(run_dir, "metadata.json")) or {}
    registry, rerr = load_registry(run_dir)
    if rerr:
        reasons.append(rerr)
    md, merrs = load_model_domain(run_dir)
    reasons += merrs
    dom = domain(registry or [], md)
    if registry is not None and not any("min" in e for e in dom["parameters"].values()):
        reasons.append("no interface input has an established range (no registry prior intervals and no "
                       f"{DOMAIN}): the valid domain cannot be established")
    point = point_params(registry or [])
    for name, v in point.items():
        e = dom["parameters"].get(name) or {}
        if "min" in e and not e["min"] <= v <= e["max"]:
            reasons.append(f"the registry point estimate {name} = {v:g} lies outside its valid domain "
                           f"[{e['min']:g}, {e['max']:g}] ({e['source']}): the run's own headline is an extrapolation")
    spec = OUTCOME_FN_SPEC
    fn_entry: dict = {"spec": spec, "role": "outcome_fn: the function the uncertainty harness runs",
                      "call": "outcome_fn(params: dict) -> dict",
                      "inputs": {"registry": [str(p["name"]) for p in registry or []],
                                 "declared": sorted(set(((md or {}).get("parameters") or {})) -
                                                    {str(p["name"]) for p in registry or []})}}
    if not os.path.exists(os.path.join(run_dir, "models", "outcome_fn.py")):
        reasons.append(f"{spec} does not exist: the run declares no callable interface")
        fn_entry["loaded"] = False
    report_doc, _ = _read_yaml(os.path.join(run_dir, "uncertainty_report.yaml"))
    report_fp = report_doc.get("outcome_fn_fingerprint") if isinstance(report_doc, dict) else None
    if report_fp is not None and (not isinstance(report_fp, dict) or not isinstance(report_fp.get("sources"), dict)
                                  or not report_fp["sources"]):
        reasons.append("uncertainty_report.yaml's outcome_fn_fingerprint is malformed: which code produced the "
                       "run's intervals cannot be checked")
        report_fp = None
    static_ok, static_why = static_fingerprint_check(run_dir)
    fn_entry["matches_uncertainty_report"] = static_ok
    if static_ok is False:
        reasons.append(static_why)
    wres = None
    unit_job = _unit_job(run_dir)
    if evaluate and fn_entry.get("loaded") is not False and registry is not None:
        wres = run_worker(run_dir, {"spec": spec, "point": point, "evals": {"point": [point]}, "unit": unit_job},
                          timeout)
        fn_entry["checked"] = True
        if wres.get("worker_error"):
            fn_entry["loaded"] = False
            reasons.append(f"the interface could not be evaluated: {wres['worker_error']}")
        elif wres.get("load_error"):
            fn_entry["loaded"] = False
            fn_entry["load_error"] = wres["load_error"]
            reasons.append(f"{spec} cannot be loaded: {wres['load_error']}")
        elif wres.get("fingerprint_mismatch"):
            fn_entry["loaded"] = False
            reasons.append(wres["fingerprint_mismatch"])
        else:
            fn_entry.update({"loaded": True, "signature": wres.get("signature"), "doc": wres.get("doc"),
                             "load_seconds": wres.get("load_s"), "fingerprint": wres.get("fingerprint")})
            row = (wres.get("evals", {}).get("point") or [{}])[0]
            if row.get("ok"):
                o = row["out"]
                fn_entry["point_evaluation"] = {"ok": True, "seconds": row.get("s")}
                fn_entry["outputs"] = {"scalar": {k: v for k, v in o.items() if not isinstance(v, (str, bool))},
                                       "categorical": {k: v for k, v in o.items() if isinstance(v, (str, bool))},
                                       "not_scalar": row.get("skipped") or []}
                if not fn_entry["outputs"]["scalar"]:
                    reasons.append("outcome_fn returns no scalar outputs at the registry point estimates")
            else:
                fn_entry["point_evaluation"] = {"ok": False, "error": row.get("error")}
                reasons.append(f"outcome_fn raised at the registry point estimates: {row.get('error')}")
            why = fingerprint_mismatch(report_fp, wres.get("fingerprint"))
            fn_entry["matches_uncertainty_report"] = (None if report_fp is None else why is None)
            if why:
                reasons.append(why)
            if not ((wres.get("fingerprint") or {}).get("sources")):
                reasons.append("the interface's source fingerprint could not be recorded, so later queries "
                               "cannot check they run this code")
    else:
        fn_entry["checked"] = False
    functions = [fn_entry]
    if unit_job:
        u = {"spec": unit_job["spec"], "role": "per-unit function (models/eligibility_manifest.yaml)",
             "call_style": unit_job["call_style"],
             "inputs": sorted(unit_job["inputs"]) if unit_job.get("inputs") else None}
        ur = (wres or {}).get("unit")
        if ur:
            u.update({"signature": ur.get("signature"), "doc": ur.get("doc"),
                      "outputs": sorted(ur["outputs"]) if ur.get("outputs") else None,
                      "evaluated_at_manifest_baseline": not ur.get("error") and ur.get("outputs") is not None,
                      **({"error": ur["error"]} if ur.get("error") else {})})
        functions.append(u)
    prov = _provenance(run_dir, stored_blob, run_commit)
    if reasons and (prov["files_not_collected"] or prov["compute_results_missing"]):
        reasons.append("files the model may read are absent from this copy of the run: "
                       + ", ".join([f["file"] for f in prov["files_not_collected"]] +
                                   prov["compute_results_missing"])[:400])
    caveats = []
    if fn_entry.get("matches_uncertainty_report") is None and fn_entry.get("loaded"):
        caveats.append("uncertainty_report.yaml records no outcome_fn fingerprint (or is absent): its intervals "
                       "cannot be tied to this code; scripts/ask.py re-evaluates the draws itself")
    if not reasons and (prov["files_not_collected"] or prov["compute_results_missing"]):
        caveats.append("files absent from this copy of the run (the interface loaded and evaluated without them): "
                       + ", ".join([f["file"] for f in prov["files_not_collected"]]
                                   + prov["compute_results_missing"])[:400])
    point_out = None
    if fn_entry.get("outputs"):
        point_out = {**fn_entry["outputs"]["scalar"], **fn_entry["outputs"]["categorical"]}
    card = {
        "schema_version": 1,
        "producer": "scripts/model_card.py",
        "inputs_digest": inputs_digest(run_dir),
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": "not_usable" if reasons else "usable",
        "not_usable_reasons": reasons,
        "caveats": caveats,
        "question": _short(meta.get("question"), 1200) or None,
        "brief": _brief(run_dir),
        "interface": {"functions": functions,
                      "point_estimates": point,
                      "whatif_arguments": (md or {}).get("whatif_arguments") or {},
                      "headline": _headline(run_dir, point_out)},
        "domain": dom,
        "validation": _validation(run_dir, md),
        "uncertainty": _uncertainty(run_dir),
        "environment": _environment(wres),
        "limits": _limits(run_dir),
        "provenance": prov,
    }
    return card


def render_md(card: dict) -> str:
    L = [f"# Model card: {card['provenance']['run_id']}", ""]
    L += [f"**Status: {card['status']}**", ""]
    for r in card["not_usable_reasons"]:
        L.append(f"- {r}")
    for r in card.get("caveats") or []:
        L.append(f"- Caveat: {r}")
    if card.get("question"):
        L += ["", f"Question: {card['question']}"]
    b = card["brief"]
    if b.get("available"):
        L += ["", f"Decision: {b.get('decision', '')}",
              f"Brief: {'signed by ' + str(b.get('signed_by')) + ' at ' + str(b.get('signed_at')) if b['signed'] else 'not signed'}"]
    L += ["", "## Interface", ""]
    for f in card["interface"]["functions"]:
        L.append(f"- `{f['spec']}`: {f['role']}. Signature: `{f.get('signature') or f.get('call') or 'unknown'}`."
                 + (f" Loaded: {f.get('loaded')}." if "loaded" in f else ""))
        outs = (f.get("outputs") or {}).get("scalar") if isinstance(f.get("outputs"), dict) else None
        if outs:
            L.append(f"  - {len(outs)} scalar outputs at the point estimates, e.g. "
                     + "; ".join(f"{k} = {v:.4g}" for k, v in list(outs.items())[:8] if v is not None))
    wa = card["interface"]["whatif_arguments"]
    L.append(f"- What-if arguments: {', '.join(f'--{k} -> params[{v['param']}]' for k, v in wa.items()) or 'none declared'}")
    L += ["", "## Valid domain", "", "| input | min | max | source |", "|---|---|---|---|"]
    for name, e in card["domain"]["parameters"].items():
        if "min" in e:
            L.append(f"| {name} | {e['min']:.4g} | {e['max']:.4g} | {e['source']} |")
    if card["domain"]["not_established"]:
        L += ["", f"No established range (cannot be changed): {', '.join(card['domain']['not_established'])}"]
    v = card["validation"]
    L += ["", "## Validation", ""]
    if v["benchmarks"].get("file"):
        L.append(f"- Benchmarks: {v['benchmarks']['n_within_band']} of {v['benchmarks']['n_benchmarks']} within "
                 f"band (verdict {v['benchmarks']['verdict']}).")
    ho = (v["calibration"].get("held_out") or {}) if v["calibration"].get("file") else {}
    if v["calibration"].get("file"):
        L.append(f"- Calibration: {v['calibration'].get('method') or 'method not stated'}; "
                 f"{ho.get('n_held_out', 0)} held-out targets"
                 + (f" ({', '.join(f'{k} {x}' for k, x in ho.items() if k != 'n_held_out' and _num(x) is not None)})"
                    if ho else "") + ".")
    if v["identifiability"].get("file"):
        L.append(f"- Identifiability: {v['identifiability'].get('verdict')}.")
    for label, d in (v.get("declared") or {}).items():
        L.append(f"- {label}: {d.get('file')} ({len(d) - 1} recorded values).")
    u = card["uncertainty"]
    L += ["", "## Uncertainty", "",
          (f"- {u.get('n_successful')} of {u.get('n_draws')} registry draws (seed {u.get('seed')}, {u.get('backend')});"
           f" degenerate outputs: {', '.join(u['degenerate_outputs']) or 'none'}." if u.get("available")
           else f"- {u.get('note')}")]
    lim = card["limits"]
    of = lim["open_findings"]
    L += ["", "## Known limits", "",
          f"- Final gate: {lim.get('final_gate_action') or lim.get('final_gate_error') or 'not run'}.",
          f"- {len(lim['scope_declarations'])} scope declarations; {of['n']} open findings ({of['n_high']} HIGH)."]
    for it in of["items"][:5]:
        L.append(f"  - {it['id']} ({it['severity']}): {it['claim']}")
    p = card["provenance"]
    L += ["", "## Provenance", "",
          f"- Run commit: {p['run_commit'] or 'not recorded'}; card built with {p['card_built_with_commit'] or 'unknown'}.",
          f"- Stored: {p['stored_blob'] or 'not stored'}."]
    if p["files_not_collected"] or p["compute_results_missing"]:
        L.append(f"- Absent from this copy: {', '.join([f['file'] for f in p['files_not_collected']] + p['compute_results_missing'])}.")
    mi = card["environment"]["model_imports"] or {}
    pinned = [f"{d} {r['installed']}" for d, r in mi.items() if r.get("pinned")]
    L += ["", f"Environment: Python {card['environment']['python']}"
          + (f"; pinned packages the model imported: {', '.join(pinned)}" if pinned else "")
          + (f"; {len(mi) - len(pinned)} other distributions (see model_card.yaml)" if len(mi) > len(pinned) else ""),
          ""]
    return "\n".join(L)


def write(run_dir: str, **kw) -> dict:
    card = build(run_dir, **kw)
    for name, text in ((CARD, yaml.safe_dump(card, sort_keys=False, allow_unicode=True, width=110)),
                       (CARD_MD, render_md(card))):
        tmp = os.path.join(run_dir, f".{name}.tmp")
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, os.path.join(run_dir, name))
    return card


# --------------------------------------------------------------------------- self-test

_REG = ("# Citations\n\n## Parameter Registry\n\n```yaml\nparameters:\n"
        "- name: effect_rr\n  value: 0.6\n  ci_low: 0.5\n  ci_high: 0.72\n  kind: relative_risk\n  source: C1\n"
        "- name: unit_cost\n  value: 5.0\n  ci_low: 4.0\n  ci_high: 6.0\n  kind: cost_usd\n  source: C2\n"
        "- name: budget_total\n  value: 1000.0\n  kind: cost_usd\n  source: C3\n```\n")
_FN = '''"""Toy allocation model."""
import helper
def outcome_fn(params):
    """Benefit of funding units in order of yield until the budget runs out."""
    b = params.get("budget_total", 1000.0)
    n = int(b // (100 * params["unit_cost"]))
    return {"benefit": helper.SCALE * n * (1 - params["effect_rr"]), "n_funded": float(n),
            "plan": "A" if n > 1 else "B", "detail": [1, 2]}
def evaluate_unit(inp):
    return {"benefit": float(inp["pop"]) * (1 - 0.6)}
'''


def _self_test() -> int:
    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        w = lambda rel, s: open(os.path.join(d, rel), "w").write(s)  # noqa: E731
        w("citations.md", _REG)
        w("models/outcome_fn.py", _FN)
        w("models/helper.py", "SCALE = 10.0\n")
        w("metadata.json", json.dumps({"question": "Toy question?", "run_dir": "toy_run", "status": "completed"}))
        w("models/eligibility_manifest.yaml", "outcome_fn: models/outcome_fn.py::evaluate_unit\n"
                                               "baseline_inputs: {pop: 100, package: none}\n")
        # MC1: a loadable interface with registry ranges is usable; outputs, signature, unit fn recorded.
        c = build(d)
        f0 = c["interface"]["functions"][0]
        ok(c["status"] == "usable" and f0["loaded"] and f0["point_evaluation"]["ok"],
           f"MC1 usable card; got {c['status']} {c['not_usable_reasons']}")
        ok(abs(f0["outputs"]["scalar"]["benefit"] - 10.0 * 2 * 0.4) < 1e-9 and f0["outputs"]["categorical"]["plan"] == "A"
           and f0["outputs"]["not_scalar"] == ["detail"], f"MC1b outputs at point estimates; got {f0.get('outputs')}")
        ok(f0["signature"] == "outcome_fn(params)" and "Benefit" in f0["doc"]
           and "models/helper.py" in f0["fingerprint"]["sources"], f"MC1c signature/doc/fingerprint; got {f0}")
        u = c["interface"]["functions"][1]
        ok(u["outputs"] == ["benefit"] and u["evaluated_at_manifest_baseline"], f"MC1d per-unit fn; got {u}")
        # MC2: domain = registry CI; an entry without CI is not established (never invented).
        dp = c["domain"]["parameters"]
        ok(dp["effect_rr"]["min"] == 0.5 and dp["effect_rr"]["source"] == "registry_ci"
           and c["domain"]["not_established"] == ["budget_total"], f"MC2 registry ranges; got {c['domain']}")
        # MC3: model_domain.yaml declares a range and the what-if budget argument; it wins over the CI.
        w(DOMAIN, "parameters:\n  budget_total: {min: 500, max: 2000, basis: scenarios solved in the run}\n"
                  "  effect_rr: {min: 0.4, max: 0.8, basis: emulator grid}\n"
                  "whatif_interface:\n  arguments:\n    budget: {param: budget_total}\n"
                  "validation:\n  emulator: models/emu.json\n")
        w("models/emu.json", json.dumps({"r2": 0.98, "n_runs": 40, "rows": [1, 2]}))
        c = build(d, evaluate=False)
        dp = c["domain"]["parameters"]
        ok(dp["budget_total"]["source"] == "model_domain" and dp["effect_rr"]["min"] == 0.4
           and dp["effect_rr"]["registry_ci"] == [0.5, 0.72] and c["domain"]["not_established"] == []
           and c["interface"]["whatif_arguments"] == {"budget": {"param": "budget_total"}},
           f"MC3 model_domain ranges and what-if arguments; got {c['domain']} {c['interface']['whatif_arguments']}")
        ok(c["validation"]["declared"]["emulator"] == {"file": "models/emu.json", "r2": 0.98, "n_runs": 40},
           f"MC3b declared validation file summarised; got {c['validation']['declared']}")
        # MC4: a malformed model_domain.yaml fails closed (no basis; max < min).
        w(DOMAIN, "parameters:\n  effect_rr: {min: 0.9, max: 0.4, basis: x}\n  unit_cost: {min: 1, max: 9}\n")
        c = build(d, evaluate=False)
        ok(c["status"] == "not_usable" and sum("model_domain.yaml" in r for r in c["not_usable_reasons"]) == 2,
           f"MC4 malformed domain declaration fails closed; got {c['not_usable_reasons']}")
        os.remove(os.path.join(d, DOMAIN))
        # MC5: the model reads a missing file at call time: not usable, and the absent compute results are named.
        w("models/outcome_fn.py", _FN.replace("def outcome_fn(params):\n",
                                              "def outcome_fn(params):\n    open(__file__ + '.grid')\n"))
        w("models/grid_results.jsonl.summary.json", "{}")
        c = build(d)
        ok(c["status"] == "not_usable" and any("raised at the registry point estimates" in r and "FileNotFoundError" in r
                                               for r in c["not_usable_reasons"])
           and any("grid_results.jsonl" in r for r in c["not_usable_reasons"]),
           f"MC5 evaluation failure fails closed and names absent files; got {c['not_usable_reasons']}")
        # MC6: code changed since uncertainty_report.yaml: not usable.
        w("models/outcome_fn.py", _FN)
        w("uncertainty_report.yaml", yaml.safe_dump({"n_draws": 10, "seed": 1, "outcome_fn_fingerprint": {
            "file": "models/outcome_fn.py", "function": "outcome_fn",
            "sources": {"models/outcome_fn.py": "0" * 64, "models/helper.py": _sha(os.path.join(d, "models/helper.py"))}}}))
        c = build(d)
        ok(c["status"] == "not_usable" and any("changed since uncertainty_report.yaml" in r for r in c["not_usable_reasons"])
           and c["interface"]["functions"][0]["matches_uncertainty_report"] is False,
           f"MC6 stale model code fails closed; got {c['not_usable_reasons']}")
        # MC6b: a malformed fingerprint is not read as "no fingerprint".
        w("models/outcome_fn.py", _FN)
        w("uncertainty_report.yaml", "n_draws: 10\noutcome_fn_fingerprint: {file: models/outcome_fn.py, sources: {}}\n")
        ok(any("fingerprint is malformed" in r for r in build(d, evaluate=False)["not_usable_reasons"]),
           "MC6b malformed report fingerprint fails closed")
        # MC7: no outcome_fn at all / unloadable module.
        w("uncertainty_report.yaml", "n_draws: 10\n")
        w("models/outcome_fn.py", "import does_not_exist_anywhere\n")
        c = build(d)
        ok(c["status"] == "not_usable" and any("cannot be loaded" in r and "does_not_exist_anywhere" in r
                                               for r in c["not_usable_reasons"]),
           f"MC7 unloadable interface fails closed; got {c['not_usable_reasons']}")
        os.remove(os.path.join(d, "models/outcome_fn.py"))
        ok(any("does not exist" in r for r in build(d)["not_usable_reasons"]), "MC7b missing outcome_fn fails closed")
        # MC8: a registry with no prior intervals and no declaration: domain not established.
        w("models/outcome_fn.py", _FN)
        w("citations.md", _REG.replace("  ci_low: 0.5\n  ci_high: 0.72\n", "").replace("  ci_low: 4.0\n  ci_high: 6.0\n", ""))
        c = build(d, evaluate=False)
        ok(any("valid domain cannot be established" in r for r in c["not_usable_reasons"]),
           f"MC8 no ranges at all fails closed; got {c['not_usable_reasons']}")
        # MC9: the worker strips credentials from the model's environment; the card files are written.
        os.environ["MC_SELFTEST_API_KEY"] = "x"
        ok("MC_SELFTEST_API_KEY" not in _worker_env() and "PATH" in _worker_env(), "MC9 credentials not passed")
        del os.environ["MC_SELFTEST_API_KEY"]
        w("citations.md", _REG)
        c = write(d)
        ok(os.path.exists(os.path.join(d, CARD)) and "## Valid domain" in open(os.path.join(d, CARD_MD)).read()
           and yaml.safe_load(open(os.path.join(d, CARD)))["status"] == "usable",
           f"MC9b card files written; got {c['status']} {c['not_usable_reasons']}")
        # MC11: run code that is a symlink to code outside the run is not fingerprinted: not usable.
        with tempfile.TemporaryDirectory() as ext:
            open(os.path.join(ext, "helper.py"), "w").write("SCALE = 10.0\n")
            os.remove(os.path.join(d, "models/helper.py"))
            os.symlink(os.path.join(ext, "helper.py"), os.path.join(d, "models/helper.py"))
            w("models/outcome_fn.py", _FN)
            c = build(d)
            ok(c["status"] == "not_usable" and any("symlink to code outside the run" in r for r in c["not_usable_reasons"]),
               f"MC11 symlinked external code fails closed; got {c['not_usable_reasons']}")
            os.remove(os.path.join(d, "models/helper.py"))
            w("models/helper.py", "SCALE = 10.0\n")
        # MC10: a model that hangs is stopped and reported.
        w("models/outcome_fn.py", "import time\ndef outcome_fn(p):\n    time.sleep(30)\n")
        c = build(d, timeout=3)
        ok(any("did not finish within 3 s" in r for r in c["not_usable_reasons"]), f"MC10 timeout; got {c['not_usable_reasons']}")
    if fails:
        print(f"FAIL: {len(fails)} case(s)", file=sys.stderr)
        for f in fails:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    if argv is None and len(sys.argv) == 4 and sys.argv[1] == "--worker":
        return _worker(sys.argv[2], sys.argv[3])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--no-evaluate", action="store_true", help="skip loading and evaluating the interface")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir or not os.path.isdir(a.run_dir):
        ap.error("run_dir (a run directory) is required")
    card = write(a.run_dir, evaluate=not a.no_evaluate, timeout=a.timeout)
    print(f"{CARD}: {card['status']}" + "".join(f"\n  - {r}" for r in card["not_usable_reasons"]))
    return 0 if card["status"] == "usable" else 3


if __name__ == "__main__":
    sys.exit(main())
