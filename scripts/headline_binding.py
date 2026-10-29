#!/usr/bin/env python3
"""S2: the uncertainty harness's `outcome_fn` must be the model behind the
allocation and the report's headline numbers.

    python3 scripts/headline_binding.py <run_dir>
    python3 scripts/headline_binding.py --self-test

The harness runs `models/outcome_fn.py::outcome_fn` for uncertainty
propagation (scripts/propagate_uncertainty.py). Nothing else tied that
function to the deliverable, so a decoy outcome_fn (a different model, a
simplified copy, a stale surrogate) would make every interval the gate reads
meaningless. Binding comes first (docs/design/stage-machine.md section 3.3): a
decoy fails here before any other check reads it. Phase 38 beta does the same
for the per-unit function (scripts/effect_concordance.py).

The modeler declares which outputs are which claims, and writes no numbers
(`models/headline_binding.yaml`):

    outputs:                       # outcome_fn output key -> claims-ledger claim id
      total_dalys_averted: dalys_det
      total_cost: spend
    package_choice:                # optional: outcome_fn's per-unit package choice
      allocation_csv: models/allocation.csv
      unit_column: district
      package_column: package
      output_prefix: package_      # outcome_fn returns {"package_<unit>": "<package>"} per unit

The harness evaluates outcome_fn ONCE at the registry point estimates (params
= `value` of every entry in the citations.md Parameter Registry, the same
names propagate_uncertainty samples), then requires:

1. each declared output to equal its claims-ledger claim within 0.1% (tight enough
   that a branch variant of the model, typically 0.5-1% off, does not pass);
2. when declared, the per-unit package choice to equal the allocation CSV's
   package column in every row, with no choices for units the CSV lacks;
3. when models/effect_concordance.yaml exists, its `headline_claim` (the claim
   the allocation is bound to) to be among the bound claims, and its
   `allocation_csv` to be the one the package choice is compared with;
4. when uncertainty_report.yaml exists, that it was produced by this function
   from this code: propagate_uncertainty records the function and the sha256
   of every Python file under models/ (and of any other run module it
   imports), so a report from `--outcome-fn <another function>` or from code
   edited since fails. Both run the code from source, never a cached .pyc.
   Data or configuration files the model reads are not fingerprinted.

A mismatch, a missing claim, an outcome_fn that raises or returns no dict, or
an unreadable manifest/registry is HIGH `headline_binding_failed`, not
scope-declarable. Before a claims ledger exists the outputs cannot yet be tied
to the report: MEDIUM `headline_binding_unverified` (re-checked at every gate).
An absent manifest yields nothing here: the rigor manifest
(`.claude/orchestration/rigor_artifacts.yaml: headline_binding`, triggered by
an allocation) reports it.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import contextlib
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join("models", "headline_binding.yaml")
OUTCOME_FN_SPEC = "models/outcome_fn.py::outcome_fn"   # the function propagate_uncertainty runs
TOL = 0.001
_PC_KEYS = ("allocation_csv", "unit_column", "package_column", "output_prefix")


def _sibling(name: str):
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    return __import__(name)


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _point_params(run_dir: str) -> tuple[dict | None, str]:
    """{registry name: point value} for every registry entry, or (None, why)."""
    esr = _sibling("effect_size_registry")
    try:
        params = esr.load_priors(os.path.join(run_dir, "citations.md")).get("parameters") or []
    except Exception as e:  # noqa: BLE001 — fail closed
        return None, f"the citations.md Parameter Registry cannot be read ({type(e).__name__}: {str(e)[:150]})"
    out, bad = {}, []
    for p in params:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        try:
            v = float(p.get("value"))
        except (TypeError, ValueError):
            v = float("nan")
        if not math.isfinite(v):
            bad.append(str(p["name"]))
            continue
        out[str(p["name"])] = v
    if bad:
        return None, f"registry entries without a numeric point `value`: {', '.join(bad[:10])}"
    if not out:
        return None, "the citations.md Parameter Registry has no parameters to evaluate outcome_fn at"
    return out, ""


@contextlib.contextmanager
def _run_imports(run_dir: str):
    """While loading and calling outcome_fn, a plain `import x` in run code
    resolves to the run's models/x.py: cached modules of those names (another
    run's, or a gate module such as pipeline or compute) are set aside. After,
    the run's modules and its path entry are removed and the set-aside modules
    restored, so later imports in the gate process never resolve to run code."""
    models = os.path.realpath(os.path.join(run_dir, "models"))
    saved_path, aside = list(sys.path), {}
    for stem in {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(models, "*.py"))}:
        if stem in sys.modules:
            aside[stem] = sys.modules.pop(stem)
    sys.path.insert(0, models)
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for k, m in list(sys.modules.items()):
            f = getattr(m, "__file__", None)
            if f and os.path.realpath(f).startswith(os.path.join(models, "")):
                sys.modules.pop(k, None)
        for k, m in aside.items():
            if not str(getattr(m, "__file__", "") or "").startswith(os.path.join(models, "")):
                sys.modules[k] = m   # a gate module comes back; another run's module does not


def _load_fn(run_dir: str, params: dict):
    """(fn, fingerprint): outcome_fn loaded fresh from this run (call inside
    _run_imports), with the source hashes propagate_uncertainty records."""
    return _sibling("propagate_uncertainty").load_outcome_fn_fingerprinted(run_dir, OUTCOME_FN_SPEC, params)


def _uq_mismatch(run_dir: str, fp: dict) -> str | None:
    """Why uncertainty_report.yaml was not produced by this outcome_fn, or None.
    No report yet: nothing to compare (the UQ check reports a missing report)."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from lib.rigor_artifacts import produced_path
    p = produced_path("outcome_fn", run_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        return f"uncertainty_report.yaml cannot be read ({type(e).__name__})"
    rec = doc.get("outcome_fn_fingerprint") if isinstance(doc, dict) else None
    if not isinstance(rec, dict) or not isinstance(rec.get("sources"), dict):
        return ("uncertainty_report.yaml does not record which outcome_fn produced it; re-run "
                "scripts/propagate_uncertainty.py so the binding can check it is this function")
    if (rec.get("file"), rec.get("function")) != (fp["file"], fp["function"]):
        return (f"uncertainty_report.yaml was produced by {rec.get('file')}::{rec.get('function')}, not "
                f"{OUTCOME_FN_SPEC}: the intervals are not those of the function bound to the report")
    if rec["sources"] != fp["sources"]:
        changed = sorted(k for k in set(rec["sources"]) | set(fp["sources"])
                         if rec["sources"].get(k) != fp["sources"].get(k))
        return (f"outcome_fn's code changed since uncertainty_report.yaml was produced ({', '.join(changed[:5])}); "
                "re-run scripts/propagate_uncertainty.py so the intervals are those of the bound function")
    return None


def _read_yaml(run_dir: str, rel: str):
    with open(os.path.join(run_dir, rel)) as f:
        return yaml.safe_load(f)


def evaluate(run_dir: str) -> tuple[list[str], dict]:
    """(errors, info). info: the outcome_fn values, claim values and whether a ledger exists."""
    p = os.path.join(run_dir, PATH)
    if not os.path.lexists(p):
        return [], {}     # absent: the rigor manifest reports it when an allocation exists
    try:
        doc = _read_yaml(run_dir, PATH)
    except OSError as e:
        return [f"{PATH} cannot be read ({type(e).__name__})"], {}
    except yaml.YAMLError as e:
        return [f"{PATH} does not parse: {str(e)[:150]}"], {}
    if not isinstance(doc, dict):
        return [f"{PATH} must be a mapping"], {}
    outputs = doc.get("outputs")
    if (not isinstance(outputs, dict) or not outputs
            or not all(str(k).strip() and isinstance(v, str) and v.strip() for k, v in outputs.items())):
        return [f"{PATH}: `outputs:` must map each outcome_fn output key to a claims-ledger claim id"], {}
    outputs = {str(k): str(v).strip() for k, v in outputs.items()}
    pc = doc.get("package_choice")
    if pc is not None and (not isinstance(pc, dict) or any(not str(pc.get(k) or "").strip() for k in _PC_KEYS)):
        return [f"{PATH}: `package_choice:` needs {', '.join(_PC_KEYS)}"], {}
    errors: list[str] = []
    # The allocation and headline the per-unit function is bound to must be the ones bound here.
    try:
        ec = _read_yaml(run_dir, os.path.join("models", "effect_concordance.yaml"))
    except (OSError, yaml.YAMLError):
        ec = None
    if isinstance(ec, dict):
        hc = str(ec.get("headline_claim") or "").strip()
        if hc and hc not in outputs.values():
            errors.append(f"claim {hc} (models/effect_concordance.yaml: headline_claim, the allocation's headline) is "
                          "not bound to an outcome_fn output: the function the uncertainty harness runs must "
                          "reproduce the headline the allocation is bound to")
        ea = str(ec.get("allocation_csv") or "").strip()
        if pc is not None and ea and os.path.normpath(ea) != os.path.normpath(str(pc["allocation_csv"])):
            errors.append(f"package_choice.allocation_csv {pc['allocation_csv']} is not the allocation "
                          f"models/effect_concordance.yaml binds ({ea})")
    params, why = _point_params(run_dir)
    if params is None:
        return errors + [f"cannot evaluate outcome_fn at the registry point estimates: {why}"], {}
    # Load and run from source, never a cached .pyc, so the code run is the code hashed.
    with _sibling("propagate_uncertainty").no_cached_bytecode(), _run_imports(run_dir):
        try:
            fn, fp = _load_fn(run_dir, params)
        except Exception as e:  # noqa: BLE001 — fail closed
            return errors + [f"cannot load {OUTCOME_FN_SPEC}: {type(e).__name__}: {str(e)[:200]}"], {}
        try:
            res = fn(dict(params))
        except Exception as e:  # noqa: BLE001 — fail closed
            return errors + [f"outcome_fn raised at the registry point estimates: {type(e).__name__}: "
                             f"{str(e)[:200]}"], {}
    why_uq = _uq_mismatch(run_dir, fp)
    if why_uq:
        errors.append(why_uq)
    if not isinstance(res, dict):
        return errors + [f"outcome_fn returned {type(res).__name__}, not a dict"], {}
    info: dict = {"params": params, "outputs": {}, "claims": {}}
    ec_mod = _sibling("effect_concordance")
    ledger = os.path.exists(os.path.join(run_dir, "models", "claims_ledger.yaml"))
    info["ledger"] = ledger
    for key, cid in outputs.items():
        v = _num(res.get(key))
        info["outputs"][key] = v
        if v is None:
            errors.append(f"outcome_fn output {key} is {'missing' if key not in res else 'not a finite number'} "
                          f"at the registry point estimates")
            continue
        if not ledger:
            continue
        cv, cwhy = ec_mod._ledger_value(run_dir, cid)
        info["claims"][cid] = cv
        if cv is None:
            errors.append(f"claim {cid} (bound to {key}) is "
                          f"{'missing from' if cwhy == 'missing' else 'not a number in' if cwhy == 'found' else cwhy + ':'} "
                          "models/claims_ledger.yaml")
        elif abs(v - cv) > TOL * max(abs(cv), 1e-12):
            errors.append(f"outcome_fn {key} at the registry point estimates is {v:.6g} but the report's claim {cid} "
                          f"is {cv:.6g} ({abs(v - cv) / max(abs(cv), 1e-12):.1%} off, tolerance {TOL:.1%}): the "
                          "function the uncertainty harness runs is not the model behind the report")
    if pc is not None:
        pre, uc, pkc = str(pc["output_prefix"]), str(pc["unit_column"]), str(pc["package_column"])
        try:
            with open(os.path.join(run_dir, str(pc["allocation_csv"])), newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if not rows or uc not in rows[0] or pkc not in rows[0]:
                raise ValueError(f"needs rows with columns {uc} and {pkc}")
        except Exception as e:  # noqa: BLE001
            return errors + [f"package_choice.allocation_csv {pc['allocation_csv']} cannot be read: "
                             f"{type(e).__name__}: {str(e)[:150]}"], info
        want: dict[str, str] = {}
        dup = []
        for r in rows:
            u = str(r.get(uc) or "").strip()
            if u in want:
                dup.append(u)
            want[u] = str(r.get(pkc) or "").strip()
        got = {k[len(pre):]: str(v).strip() for k, v in res.items() if isinstance(k, str) and k.startswith(pre)}
        bad = sorted(u for u in want if got.get(u) != want[u])
        extra = sorted(set(got) - set(want))
        info["package_units"], info["package_mismatch"] = len(want), len(bad)
        if dup:
            errors.append(f"{pc['allocation_csv']}: units listed more than once: {', '.join(sorted(set(dup))[:5])}")
        if bad:
            ex = ", ".join(f"{u}: {got.get(u, '(none)')} vs {want[u]}" for u in bad[:5])
            errors.append(f"outcome_fn's package choice ({pre}<unit>) differs from {pc['allocation_csv']} in "
                          f"{len(bad)} of {len(want)} units (e.g. {ex}): the uncertainty harness would propagate a "
                          "different allocation than the one recommended")
        if extra:
            errors.append(f"outcome_fn chooses packages for units not in {pc['allocation_csv']}: "
                          f"{', '.join(extra[:5])}")
    return errors, info


def gate_findings(run_dir: str) -> list[dict]:
    """Findings for the STAGE 7 gate. A missing manifest is the rigor manifest's
    finding (models/headline_binding.yaml, triggered by an allocation)."""
    errors, info = evaluate(run_dir)
    out = [{"kind": "headline_binding_failed", "severity": "HIGH", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": False, "claim": f"{PATH}: {e}"} for e in errors]
    if info and not info.get("ledger"):
        out.append({"kind": "headline_binding_unverified", "severity": "MEDIUM", "stage": "MODEL",
                    "target_stage": "ANALYZE", "scope_declarable": False,
                    "claim": f"{PATH}: no models/claims_ledger.yaml yet, so outcome_fn's outputs cannot yet be tied "
                             "to the report's numbers (re-checked at every gate)."})
    return out


# --------------------------------------------------------------------------- self-test

_REG = ("# Citations\n\n## Parameter Registry\n\n```yaml\n"
        "- name: efficacy\n  value: 0.8\n  ci_low: 0.7\n  ci_high: 0.9\n  kind: efficacy\n  source: C1\n"
        "- name: unit_cost\n  value: 5.0\n  ci_low: 4.0\n  ci_high: 6.0\n  kind: cost_usd\n  source: C2\n```\n")
_HONEST = '''\
POP = {"a": 1000.0, "b": 2000.0, "c": 500.0}
FUNDED = {"a", "b"}
def outcome_fn(params):
    e, c = params["efficacy"], params["unit_cost"]
    out = {"dalys": sum(POP[u] * 0.1 * e for u in FUNDED), "cost": sum(POP[u] * c for u in FUNDED)}
    out.update({"pkg_" + u: ("campaign" if u in FUNDED else "none") for u in POP})
    return out
'''


def _self_test() -> int:
    fails = []
    sys.dont_write_bytecode = True   # outcome_fn.py is rewritten within the same second

    def ok(c, m):
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "citations.md"), "w").write(_REG)
        honest = {"dalys": 3000 * 0.1 * 0.8, "cost": 3000 * 5.0}

        def fn(src):
            open(os.path.join(d, "models", "outcome_fn.py"), "w").write(src)

        def ledger(**vals):
            v = {**honest, **vals}
            claims = [{"id": "det_dalys", "value": v["dalys"]}, {"id": "spend", "value": v["cost"]}]
            yaml.safe_dump({"claims": [c for c in claims if c["value"] is not None]},
                           open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))

        def alloc(pk=None):
            pk = pk or {"a": "campaign", "b": "campaign", "c": "none"}
            open(os.path.join(d, "models", "allocation.csv"), "w").write(
                "unit,package\n" + "".join(f"{u},{p}\n" for u, p in pk.items()))

        pc = {"allocation_csv": "models/allocation.csv", "unit_column": "unit", "package_column": "package",
              "output_prefix": "pkg_"}

        def put(outputs=None, package_choice=pc):
            m = {"outputs": outputs or {"dalys": "det_dalys", "cost": "spend"}}
            if package_choice is not None:
                m["package_choice"] = package_choice
            yaml.safe_dump(m, open(os.path.join(d, PATH), "w"))

        fn(_HONEST); ledger(); alloc()
        ok(gate_findings(d) == [], "HB1 no manifest: nothing here (the rigor manifest reports a missing artifact)")
        put()
        errs, info = evaluate(d)
        ok(errs == [] and gate_findings(d) == [] and info["package_units"] == 3,
           f"HB2 an honest binding passes: {errs} {info}")
        fn(_HONEST.replace("0.1 * e", "0.13 * e"))
        f = gate_findings(d)
        ok(len(f) == 1 and f[0]["kind"] == "headline_binding_failed" and "det_dalys" in f[0]["claim"]
           and not f[0]["scope_declarable"] and f[0]["severity"] == "HIGH",
           f"HB3 a decoy outcome_fn (different totals) fails, HIGH, not declarable: {f}")
        fn(_HONEST.replace("0.1 * e", "0.10005 * e"))
        ok(gate_findings(d) == [], "HB3b a difference within 0.1% passes")
        fn(_HONEST.replace("0.1 * e", "0.1008 * e"))
        f = gate_findings(d)
        ok(len(f) == 1 and "0.8% off" in f[0]["claim"] and "tolerance 0.1%" in f[0]["claim"],
           f"HB3c a branch variant 0.8% off fails: {f}")
        fn(_HONEST)
        ledger(cost=None)
        f = gate_findings(d)
        ok(any("spend" in x["claim"] and "missing from" in x["claim"] for x in f), f"HB4 a missing claim fails: {f}")
        ledger()
        fn(_HONEST.replace("    e, c =", "    raise RuntimeError('boom')\n    e, c ="))
        f = gate_findings(d)
        ok(len(f) == 1 and "raised" in f[0]["claim"] and f[0]["severity"] == "HIGH",
           f"HB5 a raising outcome_fn fails closed: {f}")
        fn("def outcome_fn(params):\n    return [1.0]\n")
        ok(any("not a dict" in x["claim"] for x in gate_findings(d)), "HB5b a non-dict result fails closed")
        fn(_HONEST)
        alloc({"a": "campaign", "b": "none", "c": "campaign"})
        f = gate_findings(d)
        ok(any("package choice" in x["claim"] and "2 of 3" in x["claim"] for x in f),
           f"HB6 a package-choice mismatch fails: {f}")
        alloc({"a": "campaign", "b": "campaign"})
        ok(any("not in models/allocation.csv" in x["claim"] for x in gate_findings(d)),
           "HB6b choices for units the allocation lacks fail")
        alloc()
        put({"dalys": "det_dalys", "missing_key": "spend"})
        ok(any("missing_key is missing" in x["claim"] for x in gate_findings(d)),
           "HB7 a declared output outcome_fn does not return fails")
        put()
        os.remove(os.path.join(d, "models", "claims_ledger.yaml"))
        f = gate_findings(d)
        ok([x["kind"] for x in f] == ["headline_binding_unverified"] and f[0]["severity"] == "MEDIUM",
           f"HB8 before a ledger exists the tie to the report is a MEDIUM note: {f}")
        ledger()
        yaml.safe_dump({"headline_claim": "other_claim", "allocation_csv": "models/alloc2.csv"},
                       open(os.path.join(d, "models", "effect_concordance.yaml"), "w"))
        f = gate_findings(d)
        ok(any("other_claim" in x["claim"] for x in f) and any("alloc2" in x["claim"] for x in f),
           f"HB9 the effect check's headline and allocation must be the ones bound here: {f}")
        yaml.safe_dump({"headline_claim": "det_dalys", "allocation_csv": "models/allocation.csv"},
                       open(os.path.join(d, "models", "effect_concordance.yaml"), "w"))
        ok(gate_findings(d) == [], "HB9b matching effect-check declarations pass")
        put(package_choice=None)
        ok(gate_findings(d) == [], "HB10 package choice is optional")
        put(package_choice={"allocation_csv": "models/allocation.csv"})
        ok(any("needs" in x["claim"] for x in gate_findings(d)), "HB11 an incomplete package_choice fails closed")
        put()
        open(os.path.join(d, "citations.md"), "w").write("# Citations\n")
        ok(any("no parameters" in x["claim"] for x in gate_findings(d)), "HB12 an empty registry fails closed")
        open(os.path.join(d, "citations.md"), "w").write(_REG)
        open(os.path.join(d, PATH), "w").write("outputs: [\n")
        f = gate_findings(d)
        ok(len(f) == 1 and "does not parse" in f[0]["claim"], f"HB13 an unparseable manifest fails closed: {f}")
        yaml.safe_dump({"outputs": {}}, open(os.path.join(d, PATH), "w"))
        ok(any("`outputs:`" in x["claim"] for x in gate_findings(d)), "HB14 an empty outputs map fails closed")
        put()
        # The uncertainty report must come from this function and this code.
        pu = _sibling("propagate_uncertainty")
        uq = os.path.join(d, "uncertainty_report.yaml")

        def report(spec="models/outcome_fn.py::outcome_fn"):
            # as propagate_uncertainty does: probe the function at the registry point estimates
            _fn, fp = pu.load_outcome_fn_fingerprinted(d, spec, _point_params(d)[0])
            yaml.safe_dump({"producer": "scripts/propagate_uncertainty.py", "outcome_fn_fingerprint": fp},
                           open(uq, "w"))
        open(os.path.join(d, "models", "helper.py"), "w").write("SCALE = 0.1\n")
        fn(_HONEST.replace("0.1 * e", "helper.SCALE * e").replace("POP =", "import helper\nPOP ="))
        report()
        ok(gate_findings(d) == [], f"HB16 a report from this function and code passes: {gate_findings(d)}")
        open(os.path.join(d, "models", "helper.py"), "w").write("SCALE = 0.1  # edited\n")
        f = gate_findings(d)
        ok(any("changed since" in x["claim"] and "helper.py" in x["claim"] for x in f),
           f"HB17 an imported module edited after the report fails: {f}")
        open(os.path.join(d, "models", "decoy.py"), "w").write(
            "def outcome_fn(params):\n    return {'dalys': params['efficacy'] * 1e6}\n")
        report("models/decoy.py::outcome_fn")
        f = gate_findings(d)
        ok(any("was produced by models/decoy.py" in x["claim"] for x in f),
           f"HB18 a report from `--outcome-fn <another function>` fails: {f}")
        # A module imported lazily, inside the function, is fingerprinted too.
        fn(_HONEST.replace("    e, c =", "    import helper\n    e, c =").replace("0.1 * e", "helper.SCALE * e"))
        report()
        ok(gate_findings(d) == [], f"HB20 a lazily imported module: the honest report passes: {gate_findings(d)}")
        open(os.path.join(d, "models", "helper.py"), "w").write("SCALE = 0.2  # edited\n")
        ok(any("changed since" in x["claim"] for x in gate_findings(d)),
           "HB20b (Codex) a lazily imported module edited after the report fails")
        open(os.path.join(d, "models", "helper.py"), "w").write("SCALE = 0.1\n")
        # A stale .pyc (same size, same mtime) is not what runs: the source is.
        sys.dont_write_bytecode = False
        hp = os.path.join(d, "models", "helper.py")
        open(hp, "w").write("SCALE = 0.9\n")
        st = os.stat(hp)
        sys.modules.pop("helper", None)
        sys.path.insert(0, os.path.join(d, "models"))
        import importlib
        importlib.import_module("helper")          # writes a .pyc of SCALE = 0.9
        sys.modules.pop("helper", None)
        open(hp, "w").write("SCALE = 0.1\n")
        os.utime(hp, ns=(st.st_atime_ns, st.st_mtime_ns))   # the cache now looks current
        sys.dont_write_bytecode = True
        report()
        ok(gate_findings(d) == [], f"HB21 (Codex) a stale .pyc is not executed in place of the source: "
                                   f"{gate_findings(d)}")
        # Review: a models/ file the function does not use is not its code.
        open(os.path.join(d, "models", "fig_tornado.py"), "w").write("import matplotlib\n")
        ok(gate_findings(d) == [], f"HB22 a new unrelated models/ script does not stale the report: {gate_findings(d)}")
        os.remove(os.path.join(d, "models", "fig_tornado.py"))
        # Review: run modules named like gate modules never leak into the gate process.
        import types
        gate_mod = types.ModuleType("pipeline"); gate_mod.__file__ = os.path.join(HERE, "pipeline.py")
        sys.modules["pipeline"] = gate_mod
        open(os.path.join(d, "models", "pipeline.py"), "w").write("X = 1\n")
        path_before = list(sys.path)
        gate_findings(d)
        models_dir = os.path.realpath(os.path.join(d, "models"))
        leaked = [k for k, m in sys.modules.items()
                  if str(getattr(m, "__file__", "") or "").startswith(models_dir + os.sep)]
        ok(sys.modules.get("pipeline") is gate_mod and not leaked and sys.path == path_before,
           f"HB23 the gate's own modules are restored and no run module or path stays loaded: {leaked}")
        sys.modules.pop("pipeline", None)
        os.remove(os.path.join(d, "models", "pipeline.py"))
        yaml.safe_dump({"producer": "scripts/propagate_uncertainty.py"}, open(uq, "w"))
        ok(any("does not record which outcome_fn" in x["claim"] for x in gate_findings(d)),
           "HB19 a report without the fingerprint fails closed")
        os.remove(uq)
        os.remove(os.path.join(d, "models", "outcome_fn.py"))
        ok(any("cannot load" in x["claim"] for x in gate_findings(d)), "HB15 a missing outcome_fn fails closed")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    findings = gate_findings(a.run_dir)
    for x in findings:
        print(f"{x['severity']} {x['kind']}: {x['claim']}")
    if not findings and os.path.exists(os.path.join(a.run_dir, PATH)):
        print("headline binding: ok")
    return 1 if any(x["severity"] == "HIGH" for x in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
