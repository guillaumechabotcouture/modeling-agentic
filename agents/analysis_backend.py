#!/usr/bin/env python3
"""v2 C: the stage machine's interface to the engines and the analysis engine (workstream B).

    python3 agents/analysis_backend.py configure <decision_spec.yaml> <out_dir>   # pipeline step
    python3 agents/analysis_backend.py analyze   <decision_spec.yaml> <out_dir>   # pipeline step
    python3 agents/analysis_backend.py --self-test

This is the only file that changes when workstream B (`v2b-analysis`) lands. Until then
it drives `spike/analysis` (origin/v2), which runs on any engine behind
`toolkit/engines/contract.py`, the toy engine included. The machine calls:

  eligible_engines(spec)        harness filter: each registered EngineCard against the spec's needs
  cross_check(spec, refs, ...)  optional: the analysis on a subset with each eligible engine, plans compared
  validate(spec, base)          fail closed, before any simulation
  pipeline_steps(spec_path, run_dir)  the S1 steps (models/pipeline.yaml) the harness runs: configure, analyze
  headline(run_dir)             the numbers each impact-ledger row records before and after a fix
  gate_findings(run_dir)        contract checks on the analysis output, in the common finding shape

Nothing here names a disease; the engines and the spec carry the specifics. This file
does not import the `agents` package (that pulls in the SDK): pipeline steps run it as a
script.
"""
from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

SPEC_FILE = "decision_spec.yaml"
OUT_DIR = "analysis"
ANALYSIS_JSON = f"{OUT_DIR}/analysis.json"
ALLOCATION_CSV = f"{OUT_DIR}/allocation.csv"
def _empirical_config(spec: dict) -> dict:
    """The package-rate engine is configured by the spec: its components and the outcome."""
    comps = list(((spec.get("options") or {}).get("components") or {}).keys())
    if len(comps) > EMPIRICAL_MAX_COMPONENTS:
        # the engine enumerates every combination of its components (2^n rate ratios): a joint dose-block space with
        # dozens of level components would exhaust memory while the harness only asks whether it fits (flu run 1)
        raise ValueError(f"the package-rate engine takes at most {EMPIRICAL_MAX_COMPONENTS} components (one rate ratio "
                         f"per combination); this spec has {len(comps)}")
    return {"components": comps, "outcome": (spec.get("objective") or {}).get("outcome") or "cases"}


EMPIRICAL_MAX_COMPONENTS = 16


COMPARTMENTAL = "toolkit.engines.compartmental.engine:CompartmentalEngine"


def _signed_config(ref: str):
    """A configured engine whose configuration is part of the signed spec (`engine: {ref, config}`): its card is the
    card of that configuration. A spec that names another engine (or `auto`) gives it none, so it is not eligible
    (its structure, run mode and groups cannot be guessed)."""
    def cfg(spec: dict) -> dict:
        e = spec.get("engine")
        if isinstance(e, dict) and e.get("ref") == ref:
            return dict(e.get("config") or {})
        # measles run 6: a rethink re-drafted the spec as `engine: auto`. The schema then reads the engine from the
        # run's engine_choice.json, so the choice's configuration is the one this engine runs with
        auto = e == "auto" or (isinstance(e, dict) and e.get("auto") is True)
        rd = spec.get("_run_dir")
        if auto and rd and os.path.exists(os.path.join(rd, ENGINE_CHOICE)):
            try:
                with open(os.path.join(rd, ENGINE_CHOICE)) as f:
                    ch = (json.load(f) or {}).get("engine") or {}
            except (OSError, ValueError):
                ch = {}
            if isinstance(ch, dict) and ch.get("ref") == ref and isinstance(ch.get("config"), dict):
                return dict(ch["config"])
        raise ValueError(f"{ref} is configured by the signed spec: write engine: {{ref: {ref}, config: {{...}}}} (its card "
                         "and the decision-spec skill list the fields); engine: auto cannot configure it")
    return cfg


def _renewal_config(spec: dict) -> dict:
    """The renewal engine is configured by an estimates spec's `dynamics` (generation interval, transmissibility);
    any other spec gets no configuration, and the card read fails, so the engine is not eligible."""
    from toolkit.spec import estimates as ES
    if not ES.is_estimates(spec):
        raise ValueError("the renewal engine is configured from an estimates spec's dynamics section")
    return ES.engine_config(spec)


# Registered engines: (import ref, card file read without importing the engine or None,
# config from the spec or None). A configured engine's card depends on its config.
ENGINES = [
    ("toolkit.engines.contract:ToyEngine", None, None),
    ("toolkit.engines.malaria.engine:MalariaEngine", "toolkit/engines/malaria/card.yaml", None),
    ("toolkit.engines.empirical.engine:PackageRateEngine", None, _empirical_config),
    (COMPARTMENTAL, None, _signed_config(COMPARTMENTAL)),
    ("toolkit.engines.renewal.engine:RenewalEngine", None, _renewal_config),
]
# Contract thresholds on the analysis output (B owns these once it lands).
EMULATOR_R2_MIN = 0.8
EMULATOR_MAE_MAX = 0.05


# ------------------------------------------------------------------ engine choice
def _card(ref: str, card_path: str | None, config: dict | None = None):
    import importlib
    from toolkit.engines.contract import EngineCard
    if card_path:
        import yaml
        with open(os.path.join(REPO, card_path)) as f:
            doc = yaml.safe_load(f) or {}
        return EngineCard(**{k: v for k, v in doc.items() if k in EngineCard.__dataclass_fields__})
    from toolkit.spec import schema
    return schema.engine_class(ref)(**(config or {})).describe()     # refused before any import when not allowed


def engine_needs(spec: dict) -> dict:
    """What the spec asks of an engine: its declared `engine_needs`, else what it implies."""
    if isinstance(spec.get("engine_needs"), dict):
        return spec["engine_needs"]
    cal = spec.get("calibration") or {}
    comps: dict = {}
    for m in ((spec.get("options") or {}).get("engine_map") or {}).values():
        for k, v in (m or {}).items():
            comps.setdefault(k, []).append(v)
    return {"components": comps,
            "outputs": [o for o in [(spec.get("objective") or {}).get("outcome")] if o],
            "windows": [w for w in [cal.get("metric")] if w],
            "unit_attributes": [a for a in [cal.get("attribute")] if a]
            + list(((spec.get("decision_units") or {}).get("engine_attributes") or {}))}


def card_fits(card, needs: dict) -> list[str]:
    """Why an engine does not fit the spec (empty: it fits)."""
    why = []
    for k, vals in (needs.get("components") or {}).items():
        if k not in card.components:
            why.append(f"no component {k}")
            continue
        why += [f"component {k} has no value {v!r}" for v in vals if v not in card.components[k]]
    outs = set((card.outputs or {}).get("per_capita") or [])
    why += [f"no outcome {o}" for o in needs.get("outputs") or [] if o not in outs]
    wins = set((card.outputs or {}).get("windows") or [])
    why += [f"no window metric {w}" for w in needs.get("windows") or [] if w not in wins]
    why += [f"no unit attribute {a}" for a in needs.get("unit_attributes") or [] if a not in card.unit_attributes]
    drivers = set((card.outputs or {}).get("cost_drivers") or []) | set((card.outputs or {}).get("cost_drivers_when") or {})
    why += [f"no cost driver {c}" for c in needs.get("cost_drivers") or [] if c not in drivers]
    why += [f"no age group {g}" for g in needs.get("age_groups") or [] if g not in (card.age_groups or [])]
    for a, (lo, hi) in (needs.get("ranges") or {}).items():
        vr = (card.valid_ranges or {}).get(a)
        if vr and (lo < vr[0] or hi > vr[1]):
            why.append(f"{a} range [{lo}, {hi}] outside the validated [{vr[0]}, {vr[1]}]")
    return why


def candidate_entries(spec: dict, engines=None) -> list:
    """The engines to consider: the registered ones, or only the signed candidates (`engine: {auto: true, candidates:
    [...]}`; a candidate that is not registered is considered with no card file and no configuration)."""
    entries = list(engines or ENGINES)
    eng = spec.get("engine")
    cands = eng.get("candidates") if isinstance(eng, dict) and eng.get("auto") is True else None
    if engines is None and isinstance(cands, list) and cands:
        known = {e[0]: e for e in entries}
        entries = [known.get(c, (c, None, None)) for c in cands]
    return entries


def _departures(spec: dict, departures: str) -> tuple[dict, str]:
    """(the spec whose departures may count, their status). "proposed": the pre-sign check, the spec's own. Otherwise
    only those of the run's spec file as the owner signed it (benchmark_coverage.signature: signed_by owner, bytes
    unchanged); a lead's signature or a changed spec counts none."""
    if departures == "proposed":
        return spec, "proposed"
    from toolkit.engines import benchmark_coverage as BC
    rd = spec.get("_run_dir")
    try:
        st = BC.signature(rd) if rd else "unsigned"
        if st == "signed":
            import yaml
            signed = (yaml.safe_load(open(os.path.join(rd, SPEC_FILE))) or {}).get("engine_departures") or []
            return dict(spec, engine_departures=signed), "signed"
    except Exception:  # noqa: BLE001 -- unreadable sign-off: no departure counts (fail closed)
        pass
    return spec, "unsigned"


def benchmark_gate(ref: str, card, config: dict, spec: dict, departures: str = "signed") -> dict:
    """Owner decisions 2026-11-12/13: the benchmark precondition (toolkit/engines/benchmark_coverage.py) on the
    verified record of the configuration in use, never the card's copy of it."""
    from toolkit.engines import benchmark_coverage as BC
    raw, status = _departures(spec, departures)
    return BC.assess(ref, config, raw, status, engine_name=card.name, card_benchmarks=card.benchmarks,
                     base=spec.get("_run_dir"))


def eligible_engines(spec: dict, engines=None, departures: str = "signed") -> list[dict]:
    """[{ref, name, version, eligible, why_not, card}] for every registered engine (or every signed candidate).
    `departures`: "signed" (the engine choice) or "proposed" (the pre-sign check)."""
    needs = engine_needs(spec)
    rows = []
    for entry in candidate_entries(spec, engines):
        ref, card_path, cfg_fn = (tuple(entry) + (None, None))[:3]
        try:
            e = spec.get("engine")
            # the configuration the run uses: the engine's function of the spec, else the spec's own for this engine
            config = cfg_fn(spec) if cfg_fn else (dict(e.get("config") or {}) if isinstance(e, dict)
                                                 and e.get("ref") == ref else {})
            card = _card(ref, card_path, config)
        except Exception as e:  # noqa: BLE001 -- recorded: an engine that cannot describe itself is not eligible
            rows.append({"ref": ref, "eligible": False, "why_not": [f"card unreadable: {type(e).__name__}: {e}"]})
            continue
        why = card_fits(card, needs)
        bg = benchmark_gate(ref, card, config, spec, departures) if not why else {}
        why += bg.get("why_not") or []
        rows.append({"ref": ref, "config": config, "name": card.name, "version": card.version, "eligible": not why,
                     "why_not": why, "mechanisms": list(card.mechanisms)[:8], "benchmarks": len(card.benchmarks or []),
                     "departures": bg.get("departures") or [], "benchmark_record": bg.get("record"),
                     "benchmarks_not_in_band": bg.get("blocking") or [],
                     "benchmarks_reported": bg.get("reported") or [],
                     "card_benchmark_rows_disagree_with_record": bg.get("card_rows_disagree") or [],
                     "components_without_benchmark": bg.get("uncovered") or {}})
    return rows


def cross_check(spec: dict, refs: list[str], work_dir: str, n_units: int = 8, *, run_dir: str) -> dict:
    """Run the analysis on the first `n_units` units with each engine and compare the plans. `work_dir` lies in the
    trusted `run_dir` (required), and every file this writes there is anchored at `run_dir` with no link followed."""
    import csv
    import io
    from toolkit.analysis.artifacts import _safe_io
    plans = {}
    for ref in refs:
        s = copy.deepcopy(spec)
        s["engine"] = ref
        d = os.path.join(work_dir, ref.replace(":", "_").replace(".", "_"))
        src = s["decision_units"]["csv"]
        rows = list(csv.DictReader(open(src)))[:n_units]
        sub = os.path.join(d, "units.csv")
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        _safe_io().publish(sub, buf.getvalue(), run_dir)     # creates d below run_dir with no link followed
        s["decision_units"]["csv"] = sub
        if isinstance(s.get("options", {}).get("units"), dict):
            s["options"]["units"]["csv"] = sub
        s.setdefault("uncertainty", {})["draws"] = min(int(s["uncertainty"].get("draws", 4)), 4)
        _configure(s, d, run_dir)
        A = _analyze(s, d)
        plans[ref] = {u: p for u, p in _plan_of(d).items()}
        plans[ref + "#headline"] = A["point"]["averted"]
    a, b = refs[0], refs[1]
    same = [u for u in plans[a] if plans[b].get(u) == plans[a][u]]
    return {"engines": refs, "units": len(plans[a]), "same_package": len(same),
            "agreement": len(same) / max(1, len(plans[a])),
            "headlines": {r: plans[r + "#headline"] for r in refs}}


# ------------------------------------------------------------------ spec and steps
def _abs_spec(spec: dict, base: str) -> dict:
    s = copy.deepcopy(spec)
    for sect in ("decision_units", "options"):
        sub = s.get(sect) if sect == "decision_units" else (s.get("options") or {}).get("units")
        if isinstance(sub, dict) and isinstance(sub.get("csv"), str) and not os.path.isabs(sub["csv"]):
            sub["csv"] = os.path.join(base, sub["csv"])
    return s


SETTINGS_FILE = "analysis_settings.yaml"
ENGINE_CHOICE = "engine_choice.json"
# The computational leaves a fix may change without a new sign-off (workstream B's
# schema.SETTINGS_PATHS, in the spike's vocabulary). Everything else is in the signed spec.
SETTINGS_PATHS = ("uncertainty.draws", "uncertainty.seed", "emulator.points", "emulator.seed")


def _leaves(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out.update(_leaves(v, key + "."))
        else:
            out[key] = v
    return out


def settings_problems(run_dir: str, spec: dict | None = None) -> list[str]:
    """What is wrong with analysis_settings.yaml: unreadable, a symlink, a leaf outside
    SETTINGS_PATHS, or a leaf the signed spec also sets. Fail closed."""
    import yaml
    p = os.path.join(run_dir, SETTINGS_FILE)
    if os.path.islink(p):
        return [f"{SETTINGS_FILE} is a symlink"]
    if not os.path.exists(p):
        return []
    try:
        doc = yaml.safe_load(open(p)) or {}
        if spec is None:
            spec = yaml.safe_load(open(os.path.join(run_dir, SPEC_FILE))) or {}
    except Exception as e:  # noqa: BLE001
        return [f"{SETTINGS_FILE} or {SPEC_FILE} unreadable: {type(e).__name__}"]
    if not isinstance(doc, dict):
        return [f"{SETTINGS_FILE} is not a mapping"]
    sl = _leaves(spec)
    bad = [f"{SETTINGS_FILE}: {k} is not a computational setting (allowed: {', '.join(SETTINGS_PATHS)})"
           for k in _leaves(doc) if k not in SETTINGS_PATHS]
    bad += [f"{SETTINGS_FILE}: {k} is also set in the signed {SPEC_FILE}" for k in _leaves(doc) if k in sl]
    return bad


def merged_spec(run_dir: str) -> dict:
    """The signed spec, plus the settings file's computational leaves, plus the engine the
    harness chose (engine_choice.json). Raises on settings problems (fail closed)."""
    import yaml
    spec = yaml.safe_load(open(os.path.join(run_dir, SPEC_FILE))) or {}
    probs = settings_problems(run_dir, spec)
    if probs:
        raise ValueError("; ".join(probs))
    p = os.path.join(run_dir, SETTINGS_FILE)
    if os.path.exists(p):
        for k, v in _leaves(yaml.safe_load(open(p)) or {}).items():
            cur = spec
            parts = k.split(".")
            for q in parts[:-1]:
                cur = cur.setdefault(q, {})
            cur[parts[-1]] = v
    eng = engine_choice_ref(run_dir)
    auto = spec.get("engine") in (None, "auto") or (isinstance(spec.get("engine"), dict) and spec["engine"].get("auto"))
    if eng and auto:                      # an explicit engine in the signed spec wins
        spec["engine"] = eng
    return _abs_spec(spec, run_dir)


def engine_choice_record(ref: str, why: str, candidates: list, cross_check=None, config=None) -> dict:
    """engine_choice.json, the format agreed with workstream B: the harness writes it; a spec
    with `engine: auto` takes its engine from it."""
    rec = {"engine": {"ref": ref, "config": config or {}}, "why": why, "candidates": candidates,
           "engine_departures": next((r.get("departures") or [] for r in candidates or [] if r.get("ref") == ref), [])}
    if cross_check is not None:
        rec["cross_check"] = cross_check
    return rec


def engine_choice_ref(run_dir: str) -> str | None:
    try:
        e = json.load(open(os.path.join(run_dir, ENGINE_CHOICE))).get("engine")
    except (OSError, ValueError):
        return None
    if isinstance(e, dict):
        return e.get("ref")
    return e if isinstance(e, str) else None


def pre_sign_problems(run_dir: str, derived: bool | None = None) -> list[str]:
    """Before the engine is chosen: every section the spike needs, and a valid settings file. (`derived`: the
    derived-input checks are workstream B's schema's; the spike's spec has none.)"""
    import yaml
    from spike.analysis.spec import REQUIRED
    try:
        spec = yaml.safe_load(open(os.path.join(run_dir, SPEC_FILE))) or {}
    except Exception as e:  # noqa: BLE001
        return [f"{SPEC_FILE} unreadable: {type(e).__name__}"]
    return [f"missing section {k}" for k in REQUIRED if k != "engine" and k not in spec] + settings_problems(run_dir, spec)


def choice_spec(run_dir: str) -> dict:
    """The spec the engine filter reads (merged with its settings)."""
    return merged_spec(run_dir)


# Sign-off: the spike adapter has none of its own; the machine's whole-file hash is used (sign = None).
sign = None


def validate(spec, base: str | None = None) -> list[str]:
    """`spec` is the spec file's path (merged with its settings and the engine choice) or a mapping."""
    from spike.analysis import spec as S
    try:
        if isinstance(spec, str):
            base = os.path.dirname(os.path.abspath(spec))
            spec = merged_spec(base)
        return S.validate(_abs_spec(spec, base), base)
    except Exception as e:  # noqa: BLE001 -- fail closed
        return [f"spec validation failed: {type(e).__name__}: {e}"]


def pipeline_steps(spec_path: str, run_dir: str) -> list[dict]:
    """The S1 pipeline the harness writes for a v2 run. The spec and its data are inputs, so
    an edit to either reruns exactly what depends on it."""
    import yaml
    py = sys.executable
    me = os.path.abspath(__file__)
    data = []
    try:
        spec = yaml.safe_load(open(os.path.join(run_dir, SPEC_FILE))) or {}
        for p in {(spec.get("decision_units") or {}).get("csv"), ((spec.get("options") or {}).get("units") or {}).get("csv")}:
            if isinstance(p, str) and not os.path.isabs(p):
                data.append(p)
    except OSError:
        pass
    data += [f for f in (SETTINGS_FILE, ENGINE_CHOICE) if os.path.exists(os.path.join(run_dir, f))]
    return [
        {"id": "configure", "run": f"{py} {me} configure {SPEC_FILE} {OUT_DIR}",
         "inputs": [SPEC_FILE] + sorted(data),
         "outputs": [f"{OUT_DIR}/design.json", f"{OUT_DIR}/jobs.jsonl", f"{OUT_DIR}/results.jsonl",
                     f"{OUT_DIR}/batch_summary.json"],
         "purpose": "engine batch over the emulator design"},
        {"id": "analyze", "run": f"{py} {me} analyze {SPEC_FILE} {OUT_DIR}",
         "inputs": [SPEC_FILE, f"{OUT_DIR}/design.json", f"{OUT_DIR}/results.jsonl"] + sorted(data),
         "outputs": [ANALYSIS_JSON, ALLOCATION_CSV],
         "purpose": "calibrate, emulate, optimise, uncertainty, leave-one-out, decision tests"},
    ]


def _configure(spec: dict, out: str, root: str) -> dict:
    from spike.analysis import batch as B
    from toolkit.analysis.artifacts import _safe_io
    for f in ("results.jsonl", "results_prev.jsonl"):
        _safe_io().remove_path(os.path.join(out, f), root)      # a changed spec invalidates every engine run
    d = B.design(spec, out)
    s = B.run(out, max(1, min(2, os.cpu_count() or 1)))   # at most 2 local workers
    return {"design": d, "batch": s}


def _analyze(spec: dict, out: str) -> dict:
    from spike.analysis.run import Analysis
    return Analysis(spec, out, log=lambda *a: None).run()


def _plan_of(out: str) -> dict:
    import csv
    with open(os.path.join(out, "allocation.csv")) as f:
        rows = list(csv.reader(f))
    return {r[0]: r[2] for r in rows[1:]}


# ------------------------------------------------------------------ facts for the machine
def _analysis(run_dir: str) -> dict | None:
    try:
        with open(os.path.join(run_dir, ANALYSIS_JSON)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def headline(run_dir: str) -> dict:
    A = _analysis(run_dir)
    if not A:
        return {}
    out = {f"averted.{k}": v for k, v in (A.get("point") or {}).get("averted", {}).items()}
    out["cost_allocated"] = (A.get("point") or {}).get("cost_allocated")
    for k, q in ((A.get("uncertainty") or {}).get("fixed_plan") or {}).items():
        out[f"fixed_plan.{k}.lo95"], out[f"fixed_plan.{k}.hi95"] = q.get("lo95"), q.get("hi95")
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in out.items()}


def _f(kind, sev, stage, claim, key="", declarable=True):
    return {"kind": kind, "severity": sev, "stage": stage, "target_stage": stage, "claim": claim, "key": key,
            "scope_declarable": declarable}


def gate_findings(run_dir: str) -> list[dict]:
    """Contract checks on the harness's own analysis output. Fail closed: no output, or
    output without a required section, is a non-declarable HIGH."""
    A = _analysis(run_dir)
    if A is None:
        return [_f("analysis_missing", "HIGH", "ANALYZE", f"{ANALYSIS_JSON} is missing or unreadable: the "
                   "pipeline's analyze step has not produced it.", "analysis", False)]
    out = []
    for sect in ("point", "uncertainty", "emulator_accuracy", "calibration", "loo"):
        if not isinstance(A.get(sect), dict):
            out.append(_f("analysis_section_missing", "HIGH", "ANALYZE", f"{ANALYSIS_JSON} has no `{sect}`.", sect, False))
    if out:
        return out
    acc = A["emulator_accuracy"]
    r2, mae = acc.get("reduction_r2_median"), acc.get("reduction_mae_median")
    if not ((isinstance(r2, (int, float)) and r2 >= EMULATOR_R2_MIN)
            or (isinstance(mae, (int, float)) and mae <= EMULATOR_MAE_MAX)):
        out.append(_f("emulator_inaccurate", "HIGH", "CONFIGURE",
                      f"Emulator held-out accuracy below the contract: R2 median {r2}, MAE median {mae} "
                      f"(needs R2 >= {EMULATOR_R2_MIN} or MAE <= {EMULATOR_MAE_MAX}). Add design points.", "emulator"))
    cal = A["calibration"]
    if cal.get("clamped_units"):
        out.append(_f("calibration_clamped", "MEDIUM", "CONFIGURE",
                      f"{cal['clamped_units']} of {cal.get('units')} units could not be calibrated inside the "
                      "calibration range (clamped).", "calibration"))
    if (A["point"].get("solver") or {}).get("status") != "optimal":
        out.append(_f("optimizer_not_optimal", "HIGH", "ANALYZE", "The allocation is not a proven optimum.",
                      "solver", False))
    for k, q in (A["uncertainty"].get("fixed_plan") or {}).items():
        if not (isinstance(q, dict) and isinstance(q.get("lo95"), (int, float)) and q.get("hi95", 0) > q["lo95"]):
            out.append(_f("ci_degenerate", "HIGH", "ANALYZE", f"The fixed-plan interval for {k} is degenerate: {q}.", k))
    draws = A["uncertainty"].get("draws")
    if isinstance(draws, int) and draws < 20:
        out.append(_f("uncertainty_draws_low", "MEDIUM", "ANALYZE",
                      f"Uncertainty from {draws} draws; intervals at the 2.5/97.5 percentiles need more.", "draws"))
    v = A["loo"].get("verdict")
    if v == "UNSTABLE":
        out.append(_f("allocation_unstable", "HIGH", "ANALYZE", "Leave-one-group-out allocation is UNSTABLE.", "loo"))
    elif v == "FRAGILE":
        out.append(_f("allocation_fragile", "MEDIUM", "ANALYZE", "Leave-one-group-out allocation is FRAGILE.", "loo"))
    elif v != "ROBUST":
        out.append(_f("allocation_robustness_unknown", "HIGH", "ANALYZE", f"Unknown LOO verdict {v!r}.", "loo", False))
    for dt in A.get("decision_tests") or []:
        if dt.get("plan_changes"):
            # Decision-bearing: the stage machine sends it through the owner rule (Phase 49 β)
            # before it can be declared; without that rule it is not declarable (fail closed).
            out.append(dict(_f("decision_test_changes_plan", "HIGH", "ANALYZE",
                               f"Decision test {dt.get('id')} changes the plan: {dt.get('text', '')}",
                               str(dt.get("id"))), decision_bearing=True))
    return out


# ------------------------------------------------------------------ workstream B (PR #89)
class _ToolkitApi:
    """Workstream B's `toolkit/analysis/api.py` behind the same calls (validate, pipeline_steps,
    headline, gate_findings); engine choice stays here. B's step key `cmd` is S1's `run`."""

    def __init__(self, api):
        self.api = api
        self.headline = api.headline
        self.gate_findings = api.gate_findings
        # B's computational settings (incl. analysis.workers); its validate(path) refuses others.
        try:
            from toolkit.spec import schema
            self.settings_paths = tuple(".".join(p) for p in sorted(schema.SETTINGS_PATHS))
        except ImportError:
            self.settings_paths = SETTINGS_PATHS

    def pre_sign_problems(self, run_dir: str, derived: bool | None = None) -> list[str]:
        """B's sign-off checks. With `engine: auto` the engine-independent sections first, then the full checks
        (engine attributes, calibration, priors) per candidate the card filter keeps: a candidate that fails them is
        ineligible, and the spec is refused only when no candidate passes. Without this, those checks ran only at
        CONFIGURE, after SIGN (docs/design/v2-derived-inputs.md §1). The rows are kept for the sign-off sheet."""
        from toolkit.spec import schema
        self.candidates = []
        try:
            s = schema.load(os.path.join(run_dir, SPEC_FILE))
            if derived is not None:
                s.derived = derived
            if not s.engine_pending:
                bad = list(schema.validate(s))
                self.candidates = self._named_rows(s)       # the sheet shows the named engine's departures
                return bad
            bad = list(schema.validate(s, pending_ok=True))
            # The candidates are checked even when an engine-independent section fails, so one try of the draft
            # task hears every problem (MAX_SPEC_TRIES is small).
            rows = self.candidate_rows(s, departures="proposed")
            self.candidates = rows
            if any(r.get("eligible") for r in rows):
                return bad
            # engines configured by the signed spec cannot be chosen by `auto`: say so first, so a short feedback window
            # (the machine log truncates) still carries it (flu run 7 spent its last try on `auto`)
            conf = [r["ref"] for r in rows if "configured by the signed spec" in " ".join(r.get("why_not") or [])]
            hint = ([f"engine: auto cannot configure {', '.join(conf)}: name it as engine: {{ref, config}}"]
                    if conf else [])
            return bad + ["no candidate engine passes the full checks before sign-off"] + hint + [
                f"[{r['ref']}] {w}" for r in rows for w in (r.get("why_not") or []) if w not in bad]
        except Exception as e:  # noqa: BLE001
            return [f"spec validation failed: {type(e).__name__}: {e}"]

    def _named_rows(self, s) -> list[dict]:
        """A spec that names its engine: that engine's row before sign-off (proposed departures count), for the sheet.
        schema.validate already refuses the spec when the engine fails the benchmark precondition."""
        e = s.raw.get("engine")
        ref = e.get("ref") if isinstance(e, dict) else e
        entry = next((x for x in ENGINES if x[0] == ref), (ref, None, None))
        try:
            return self._card_rows(dict(s.raw, _run_dir=s.base), [entry], "proposed")
        except Exception:  # noqa: BLE001 -- the sheet then shows no candidate; validation reports the engine
            return []

    def candidate_rows(self, s, engines=None, departures: str = "signed") -> list[dict]:
        """eligible_engines on the card, then the spec's full validation with each card-eligible candidate."""
        import yaml
        from toolkit.spec import schema
        signed = s.raw.get("engine")
        try:      # the signed candidates, also once engine_choice.json exists (a back-edge to ENGINE_CHOICE)
            signed = (yaml.safe_load(open(s.path)) or {}).get("engine", signed) if s.path else signed
        except (OSError, yaml.YAMLError):
            pass
        rows = self._card_rows(dict(s.raw, engine=signed, _run_dir=s.base), engines, departures)
        for r in rows:
            if r.get("eligible"):
                probs = schema.validate(schema.with_engine(s, r["ref"], r.get("config")))
                if probs:
                    r.update(eligible=False, why_not=list(probs))
        return rows

    def choice_spec(self, run_dir: str) -> dict:
        from toolkit.spec import schema
        return dict(schema.load(os.path.join(run_dir, SPEC_FILE)).raw, _run_dir=run_dir)

    # B signs the spec itself (toolkit/spec/signoff.py); its analysis refuses an unsigned spec.
    def sign(self, run_dir: str, by: str, note: str = "", expect_sha256: str | None = None,
             expect_derivations_sha256: str | None = None, expect_sheet_sha256: str | None = None) -> dict:
        from toolkit.spec import signoff
        return signoff.sign(run_dir, by, note, expect_sha256=expect_sha256,
                            expect_derivations_sha256=expect_derivations_sha256,
                            expect_sheet_sha256=expect_sheet_sha256)

    def signed_sha(self, run_dir: str):
        from toolkit.spec import signoff
        return signoff.signed_sha(run_dir) if signoff.status(run_dir) in ("signed", "changed") else None

    def signoff_status(self, run_dir: str) -> str:
        from toolkit.spec import signoff
        return signoff.status(run_dir)

    def cross_check(self, spec, refs, work_dir, n_units: int = 8, *, run_dir: str):
        return {"skipped": "the cross-engine check runs on the spike analysis only; not yet for workstream B's"}

    def validate(self, spec, base=None) -> list[str]:
        """B's validate raises on some refusals (a bad settings leaf: schema.SpecError); the
        machine needs a list of problems, so any exception is one (fail closed)."""
        try:
            return list(self.api.validate(spec, base) if base is not None else self.api.validate(spec))
        except Exception as e:  # noqa: BLE001
            return [f"spec validation failed: {type(e).__name__}: {e}"]

    def _card_rows(self, spec, engines=None, departures: str = "signed"):
        """The filter in B's vocabulary when B exposes engine_needs(spec); otherwise ours."""
        en = getattr(self.api, "engine_needs", None)
        if en is not None:
            spec = dict(spec, engine_needs=en(spec))
        return eligible_engines(spec, engines, departures)

    def eligible_engines(self, spec, engines=None):
        """The card filter, then (for a spec read by choice_spec, which knows its run directory) the full checks per
        candidate, so ENGINE_CHOICE never picks an engine the spec cannot bind."""
        rd = spec.get("_run_dir") if isinstance(spec, dict) else None
        if not rd:
            return self._card_rows(spec, engines)
        from toolkit.spec import schema
        return self.candidate_rows(schema.load(os.path.join(rd, SPEC_FILE)), engines)

    def pipeline_steps(self, spec_path, run_dir):
        out = []
        for st in self.api.pipeline_steps(spec_path, run_dir):
            st = dict(st)
            run = st.pop("cmd", st.get("run"))
            if isinstance(run, str) and run.split(" ", 1)[0] in ("python", "python3"):
                # The harness's interpreter (its venv), not whatever `python3` is first on PATH:
                # B's step failed with "No module named numpy" under the system python.
                run = f"{sys.executable} {run.split(' ', 1)[1]}"
            st["run"] = run
            st.pop("produced_by", None)
            out.append(st)
        return out


def backend(prefer: str = "auto"):
    """The analysis backend the machine uses: workstream B's API when it is on this branch
    (`toolkit/analysis/api.py`), else this module's spike adapter."""
    if prefer in ("auto", "toolkit"):
        try:
            from toolkit.analysis import api
            from toolkit.spec import schema
        except ImportError:
            if prefer == "toolkit":
                raise
        else:
            # The machine chooses the engine after SIGN, so the signed spec says `engine: auto`
            # and the engine comes from engine_choice.json (agreed with B; B's schema marks it
            # ENGINE_AUTO). Without it every v2 run would fail at CONFIGURE: keep the spike adapter.
            if getattr(api, "ENGINE_AUTO", False) or getattr(schema, "ENGINE_AUTO", False) or prefer == "toolkit":
                return _ToolkitApi(api)
    return sys.modules[__name__]


# ------------------------------------------------------------------ CLI (pipeline steps)
def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    if len(argv) == 3 and argv[0] in ("configure", "analyze"):
        # the trusted run dir is the one the pipeline runs this step in (scripts/pipeline.py sets it per step); never
        # taken from the spec's path, the output dir or the cwd
        run_dir = os.environ.get("MA_PIPELINE_RUN_DIR")
        if not run_dir:
            print("ERROR: MA_PIPELINE_RUN_DIR is not set: run this step through scripts/pipeline.py", file=sys.stderr)
            return 2
        spec = merged_spec(os.path.dirname(os.path.abspath(argv[1])))   # the spec's directory is the run directory
        out = os.path.abspath(argv[2])
        from toolkit.analysis.artifacts import _safe_io
        sio = _safe_io()
        fd, rel = sio.anchored(os.path.join(out, ".keep"), run_dir)     # out lies in the run dir; made no-follow
        try:
            parts = rel.split("/")[:-1]
            if parts:
                os.close(sio.dir_fd_nofollow(fd, parts, create=True))
        finally:
            os.close(fd)
        if argv[0] == "configure":
            print(json.dumps(_configure(spec, out, run_dir)))
        else:
            A = _analyze(spec, out)
            print(json.dumps({"point": A["point"]["averted"], "wall_s": A.get("wall_s")}, default=float))
        return 0
    print(__doc__)
    return 2


def toy_spec(units_csv: str, n_units: int | None = None, draws: int = 6) -> dict:
    """A toy-engine spec over a units table with columns id, region, grp, cov, pop, elig, tgt
    (shared by the self-tests here and in the stage machine)."""
    return {
        "engine": "toolkit.engines.contract:ToyEngine",
        "decision_units": {"csv": units_csv, "id": "id", "region": "region", "holdout_group": "grp",
                           "populations": {"all": {"column": "pop", "age_groups": ["all"]}}, "engine_attributes": {}},
        "calibration": {"attribute": "x", "scale": "linear", "target_column": "tgt", "log_range": [-2, 2],
                        "window": [1, 2], "metric": "prev", "target_uncertainty": {"common_state_sd_logit": 0.05},
                        "loo_covariates": ["cov"]},
        "horizon": {"start": 0, "stop": 3, "window": [0, 3]},
        "emulator": {"holdout_share": 0.15, "features": []},
        "options": {"units": {"csv": units_csv, "id": "id", "populations": {"total": "pop"}},
                    "components": {"a": {}, "b": {}}, "packages": {"generate": True},
                    "eligibility": {"b": {"column": "elig"}}, "engine_map": {"a": {"a": 1}, "b": {"b": 1}},
                    "prices": {"base_year": "2024", "index": {"2024": 1.0}},
                    "cost_items": [{"id": "ca", "component": "a", "value": 1.0, "price_year": "2024",
                                    "price_rule": "as_published", "basis": "per_person", "includes": ["x"]},
                                   {"id": "cb", "component": "b", "value": 0.6, "price_year": "2024",
                                    "price_rule": "as_published", "basis": "per_person", "includes": ["y"]}]},
        "objective": {"outcome": "dalys"}, "outcomes_reported": ["dalys", "cases"],
        "constraints": {"envelope_usd": 20000}, "comparator": {"package": "none"},
        "uncertainty": {}, "deliverable": "package per unit"}


def toy_settings(draws: int = 6, points: int = 32) -> dict:
    """The computational settings (analysis_settings.yaml) that go with toy_spec."""
    return {"uncertainty": {"draws": draws, "seed": 1}, "emulator": {"points": points, "seed": 5}}


def write_toy_units(path: str, n: int = 24, seed: int = 3) -> None:
    import math
    import numpy as np
    rng = np.random.default_rng(seed)
    with open(path, "w") as f:
        f.write("id,region,grp,cov,pop,elig,tgt\n")
        for i in range(n):
            x = rng.uniform(-1.5, 1.5)
            prev = 1 - math.exp(-0.5 * math.exp(0.8 * x))
            f.write(f"u{i:02d},r{i % 3},g{i % 4},{x + rng.normal(0, 0.1):.4f},{1000 + 50 * i},{i % 2},{prev:.5f}\n")


def _self_test_benchmark_gate(ok) -> None:
    """Owner decisions 2026-11-12/13 and review round 1 of #215 (G1-G19): an engine is not eligible for a component
    whose benchmark is not in band, or that no benchmark covers, unless the owner signs a departure naming a decision
    test that varies that effect; checked at the engine choice, at sign-off and at the analysis's validation."""
    import json
    import tempfile
    import yaml
    from toolkit.engines import benchmark as B
    from toolkit.engines import benchmark_coverage as BCmod
    from toolkit.engines.contract import ToyEngine
    from toolkit.spec import schema, signoff
    TOY = "toolkit.engines.contract:ToyEngine"
    cov = {"engine": "toy", "absent_values": {"a": [0], "b": [0]},
           "benchmarks": {"T1": {"covers": {"a": [1]}, "estimand": "effect_vs_none", "effect_parameters": ["eff_a"]},
                          "T2": {"covers": {"b": [1]}, "estimand": "effect_vs_none", "effect_parameters": ["eff_b"]},
                          "T3": {"covers": {}, "covers_none_because": "the baseline burden, not a component"}}}
    rows_in = [{"id": "T1", "metric": "a vs none", "role": "held_out", "status": "out_of_band", "computed": 0.3,
                "band": [0.5, 0.7]},
               {"id": "T2", "metric": "b vs none", "role": "held_out", "status": "in_band", "computed": 0.6,
                "band": [0.5, 0.7]},
               {"id": "T3", "metric": "baseline", "role": "held_out", "status": "out_of_band", "computed": 9,
                "band": [1, 2]}]
    dt1 = {"id": "DT1", "type": "interaction_rescale", "components": ["b"], "with_any": ["a"], "values": [1.0, 3.0],
           "text": "Does the plan change?"}
    # review round 2: the departure's test must run on the run's path and change the departed effect (eff_a is a
    # fitted parameter, so an emulator input); DT1 names `a` only in with_any, so it does not count for T1
    dta = {"id": "DTa", "type": "parameter_value", "parameter": "eff_a", "values": [0.3, 0.7], "text": "a's effect"}
    dep = {"engine": "toy", "benchmark": "T1", "reason": "the trial measured a different product", "decision_test": "DTa"}
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "cov", "benchmarks"))
        cp, rp = os.path.join(d, "cov", "benchmark_coverage.yaml"), os.path.join(d, "results.yaml")
        pp = os.path.join(d, "cov", "benchmarks", "pack.yaml")
        from toolkit.engines.benchmark_sources import band_digest

        def write_cov(c, band=(0.5, 0.7)):
            """the coverage file and the pack it was written for (same ids; one declaration per benchmark)"""
            yaml.safe_dump(c, open(cp, "w"))
            yaml.safe_dump({"engine": TOY, "benchmarks": [{"id": i, "metric": i, "band": list(band), "estimand":
                                                           {"type": "ratio", "num": "x", "den": "y"}}
                                                          for i in c["benchmarks"]]}, open(pp, "w"))
        write_cov(cov)
        BCmod.OVERRIDES[TOY] = cp
        try:
            def write_results(rows, **change):
                pk = yaml.safe_load(open(pp))
                decl = {b["id"]: b for b in pk["benchmarks"]}
                rows = [dict(r, band_sha256=band_digest(decl[r["id"]], pk)) if r["id"] in decl else r for r in rows]
                doc = B.stamp({"benchmarks": rows}, ToyEngine(results_path=rp))
                doc["engine_identity"].update(change)
                yaml.safe_dump(doc, open(rp, "w"))
            write_results(rows_in)
            entry = [(TOY, None, lambda s: {"results_path": rp})]
            dti = {"id": "DTI", "type": "interaction_rescale", "components": ["a"], "with_any": ["b"],
                   "benchmark": "T2", "values": [1.0, 2.0], "text": "interaction"}
            gen = {"packages": {"generate": True}}
            spec_ab = {"options": {"engine_map": {"a": {"a": 1}, "b": {"b": 1}}, "components": {"a": {}, "b": {}}, **gen},
                       "uncertainty": {},
                       "analysis": {"decision_tests": [dt1, dta, dti]}}     # the planner reads the draws' priors
            spec_b = dict(spec_ab, options={"engine_map": {"b": {"b": 1}}, "components": {"b": {}}, **gen})
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("T1" in w and "out_of_band" in w and "held-out validation" in w
                                         for w in r["why_not"])
               and r["benchmarks_not_in_band"] == [{"id": "T1", "role": "held_out", "status": "out_of_band"}]
               and not any("T3" in w for w in r["why_not"]),
               f"G1 an out-of-band benchmark covering a used component makes the engine ineligible, naming it and "
               f"its role ({r['why_not']})")
            r = eligible_engines(spec_b, entry)[0]
            ok(r["eligible"], f"G2 the same engine is eligible for a spec that does not use that component ({r['why_not']})")
            r = eligible_engines(dict(spec_ab, engine_departures=[dep]), entry)[0]
            ok(not r["eligible"] and any("not signed by the owner" in w for w in r["why_not"]),
               f"G3 a departure in a spec that is not signed is refused at the engine choice ({r['why_not']})")
            r = eligible_engines(dict(spec_ab, engine_departures=[dep]), entry, departures="proposed")[0]
            ok(r["eligible"] and r["departures"][0]["status"] == "proposed",
               f"G3b before sign-off (the pre-sign check) a proposed departure counts, marked proposed "
               f"({r.get('departures')})")
            other = {"id": "DT2", "type": "parameter_value", "parameter": "k", "values": [0.5, 1.0], "text": "k"}
            r = eligible_engines(dict(spec_ab, engine_departures=[dict(dep, decision_test="DT2")],
                                      analysis={"decision_tests": [dt1, dta, other]}), entry, departures="proposed")[0]
            ok(not r["eligible"] and any("does not count" in w for w in r["why_not"]),
               f"G3c a departure whose decision test does not vary the related effect does not count ({r['why_not']})")
            r = eligible_engines(dict(spec_ab, engine_departures=[dict(dep, decision_test="DT1")]), entry,
                                 departures="proposed")[0]
            ok(not r["eligible"] and any("interaction" in w and "does not count" in w for w in r["why_not"]),
               f"G3d an interaction test that names the departed component only in with_any does not count "
               f"({r['why_not'][-1:]})")
            r = eligible_engines(dict(spec_ab, engine_departures=[dict(dep, decision_test="DTI")]), entry,
                                 departures="proposed")[0]
            ok(not r["eligible"] and any("does not count" in w for w in r["why_not"]),
               f"G3e an interaction test whose benchmark has no arm ratios would not run, so it does not count "
               f"({r['why_not'][-1:]})")
            from toolkit.spec import preconditions as PC
            sp_em = schema.Spec(dict(spec_ab, engine={"ref": TOY, "config": {"results_path": rp}},
                                     analysis=dict(spec_ab["analysis"], evaluation="emulator",
                                                   decision_tests=[dta, other])), d)
            ok(PC.decision_test_runs(sp_em, "DTa", {"parameters": ["eff_a"], "components": {"a"}})[0]
               and not PC.decision_test_runs(sp_em, "DT2", {"parameters": ["k"], "components": {"a"}})[0],
               "G3f on the emulator path a parameter test runs only on an emulator input (a fitted or drawn parameter, "
               "or a spec value the design spans); k is neither")
            run = os.path.join(d, "run")
            os.makedirs(run)
            p = schema.toy_spec(run, bench=rp)
            raw = yaml.safe_load(open(p))
            e = schema.validate(schema.load(p), pending_ok=True)
            ok(any("engine benchmarks" in x and "T1" in x for x in e),
               f"G16 a spec naming an engine with a failing benchmark and no departure fails validation ({e[:2]})")
            try:
                signoff.sign(run, "owner")
                ok(False, "G16b signing it is refused")
            except ValueError as ex:
                ok("T1" in str(ex), f"G16b signing it is refused ({str(ex)[:120]})")
            raw["engine_departures"] = [dep]
            raw["analysis"]["decision_tests"].append(dta)
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            e = schema.validate(schema.load(p), pending_ok=True)
            ok(e == [], f"G4 the spec with a proposed departure validates before sign-off ({e[:2]})")
            try:
                signoff.sign(run, "lead")
                ok(False, "G5e a lead cannot sign a spec whose engine needs a departure")
            except ValueError as ex:
                ok("owner" in str(ex), f"G5e a lead cannot sign a spec whose engine needs a departure ({str(ex)[:100]})")
            signoff.sign(run, "owner")
            r = eligible_engines(dict(spec_ab, _run_dir=run), entry)[0]
            ok(r["eligible"] and r["departures"] == [dict(dep, status="signed", benchmark_status="out_of_band",
                                                           role="held_out", components=["a=1"])],
               f"G5 an owner-signed departure makes the engine eligible and is recorded on the row ({r.get('departures')})")
            ok(schema.validate(schema.load(p)) == [], "G5g the analysis's validation of the owner-signed spec passes")
            rec = engine_choice_record(r["ref"], "test", [r])
            ok(rec.get("engine_departures") == r["departures"] and rec["engine_departures"][0]["role"] == "held_out",
               "G5b the departure is recorded in engine_choice.json with the benchmark's role")
            sh = signoff.sheet(run, candidates=[r])
            ok("## Engine departures from benchmark evidence" in sh and "T1" in sh and "DTa" in sh
               and "the result stands" in sh and "the trial measured a different product" in sh,
               "G5c the sign-off sheet lists the departure with its reason, decision test and role")
            so = os.path.join(run, signoff.SPEC_SIGNOFF)
            rec0 = json.load(open(so))
            json.dump(dict(rec0, signed_by="lead"), open(so, "w"))
            r = eligible_engines(dict(spec_ab, _run_dir=run), entry)[0]
            e = schema.validate(schema.load(p))
            ok(not r["eligible"] and any("engine benchmarks" in x for x in e),
               f"G5f a spec signed by the lead: its departure counts neither at the engine choice nor at the "
               f"analysis's validation ({e[:1]})")
            json.dump(rec0, open(so, "w"))
            with open(p, "a") as f:
                f.write("\n# edited after sign-off\n")
            r = eligible_engines(dict(spec_ab, _run_dir=run), entry)[0]
            ok(not r["eligible"] and any("T1" in w for w in r["why_not"]),
               f"G6 a spec edited after sign-off: its departure is refused ({r['why_not'][:1]})")
            bad = [{k: v for k, v in dep.items() if k != "decision_test"}, {k: v for k, v in dep.items() if k != "reason"},
                   dict(dep, decision_test="DT9"), dict(dep, extra=1), dict(dep, component="a"),
                   dict(dep, no_decision_test_because="none")]
            for i, b in enumerate(bad):
                raw2 = dict(raw, engine_departures=[b])
                ok(any("engine_departures" in x for x in schema.validate(schema.Spec(raw2, run), pending_ok=True)),
                   f"G7.{i} a departure without a decision test, without a reason, naming an unknown decision test, "
                   "with an unknown field, naming both a benchmark and a component, or with a reason-for-none is refused")
            write_results(rows_in, version="0")
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("record" in w for w in r["why_not"]),
               f"G8 a benchmark record stamped for another engine version is refused (fail closed) ({r['why_not'][:1]})")
            write_results([dict(rows_in[0], status="not_assessable"), rows_in[1], rows_in[2]])
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("T1" in w and "not_assessable" in w for w in r["why_not"]),
               "G9 a not_assessable benchmark covering a used component makes the engine ineligible (fail closed)")
            write_results([dict(rows_in[0], role="fit"), rows_in[1], rows_in[2]])
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("the fit failed" in w for w in r["why_not"]),
               "G5d a fit-target benchmark out of band also makes the engine ineligible (its fit failed)")
            write_results([dict(rows_in[0], status="in_band"), rows_in[1], rows_in[2]])
            r = eligible_engines(spec_ab, entry)[0]
            ok(r["eligible"], f"G10 with every covering benchmark in band the engine is eligible ({r['why_not']})")
            write_results([dict(rows_in[0], status="in_band"), rows_in[1], rows_in[2], rows_in[0]])
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("twice" in w for w in r["why_not"]),
               f"G11b a record listing a benchmark twice is refused ({r['why_not'][:1]})")
            write_results([dict(rows_in[0], status="in_band"), rows_in[1], rows_in[2]])
            write_cov(dict(cov, benchmarks={"T1": cov["benchmarks"]["T1"]}))
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("component b" in w for w in r["why_not"]),
               f"G11 a component the coverage does not cover, and record ids it does not map, are refused ({r['why_not']})")
            write_cov(dict(cov, engine="malaria-starsim"))
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("is for engine" in w for w in r["why_not"]),
               "G11c a coverage file for another engine is refused")
            os.remove(cp)
            r = eligible_engines(spec_ab, entry)[0]
            write_cov(cov)
            ok(not r["eligible"] and any("benchmark_coverage.yaml" in w for w in r["why_not"]),
               "G12 an engine with benchmarks and no coverage file is not eligible (the card's copy is not consulted)")
            # owner decision 1: cost-per-outcome benchmarks gate only through the engine's own cost outputs
            cov2 = dict(cov, benchmarks=dict(cov["benchmarks"], T4={"covers": {"a": [1]}, "estimand": "cost_per_outcome",
                                                                      "cost_drivers": ["a_doses"]}))
            write_cov(cov2)
            r4 = {"id": "T4", "metric": "a $ per outcome", "role": "held_out", "status": "band_mis_based"}
            write_results([dict(rows_in[0], status="in_band"), rows_in[1], rows_in[2], r4])
            r = eligible_engines(spec_ab, entry)[0]
            ok(r["eligible"] and [b["id"] for b in r["benchmarks_reported"]] == ["T4"]
               and r["benchmarks_reported"][0]["status"] == "band_mis_based",
               f"G13 a cost-per-outcome benchmark is reported, not gating, when the spec prices the component itself "
               f"({r['why_not']}, {r.get('benchmarks_reported')})")
            ok("Benchmarks reported, not gating" in "\n".join(BCmod.sheet_lines({}, [r])),
               "G13c the sheet lists the reported benchmark")
            priced = dict(spec_ab, options=dict(spec_ab["options"], cost_items=[
                {"id": "ca", "component": "a", "basis": "per_outcome", "outcome": "doses"}]),
                analysis={"decision_tests": [dt1], "cost_outputs": {"doses": {"cost_driver": "a_doses"}}})
            r = eligible_engines(priced, entry)[0]
            ok(not r["eligible"] and any("T4" in w for w in r["why_not"]),
               f"G13b it gates when the spec charges the component through the engine cost output it prices "
               f"({r['why_not'][:1]})")
            # owner decision 2: a used component no benchmark covers blocks unless a departure names a decision test
            cov3 = dict(cov2, benchmarks={k: v for k, v in cov2["benchmarks"].items() if k != "T2"},
                        components_without_benchmark={"b": {"reason": "no trial in the pack",
                                                            "effect_parameters": ["eff_b"]}})
            write_cov(cov3)
            write_results([dict(rows_in[0], status="in_band"), rows_in[2], dict(r4, status="in_band")])
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("component b" in w and "no benchmark" in w for w in r["why_not"]),
               f"G14 a used component no benchmark covers makes the engine ineligible ({r['why_not']})")
            dtb = {"id": "DTb", "type": "parameter_value", "parameter": "eff_b", "values": [0.1, 0.5], "text": "b"}
            cdep = {"engine": "toy", "component": "b", "reason": "no trial; its effect is tested", "decision_test": "DTb"}
            r = eligible_engines(dict(spec_ab, engine_departures=[cdep], analysis={"decision_tests": [dt1, dtb]}),
                                 entry, departures="proposed")[0]
            ok(r["eligible"] and r["departures"][0].get("component") == "b",
               f"G14b a departure naming a decision test on the component's effect parameter makes it eligible "
               f"({r['departures']})")
            write_cov(dict(cov3, components_without_benchmark={}, effects_from_spec={"*": "spec inputs"}))
            r = eligible_engines(spec_ab, entry)[0]
            ok(r["eligible"] and r["components_without_benchmark"] == {"b": "spec inputs"},
               f"G15 an engine whose component effects are spec inputs (effects_from_spec) lists them, not gating "
               f"({r['why_not']})")
            # review round 2: cost drivers charged by another component, packages, declarations, effect parameters
            write_cov(cov2)
            write_results([dict(rows_in[0], status="in_band"), rows_in[1], rows_in[2], r4])
            spray = dict(spec_ab, options={"engine_map": {"a": {"a": 1}, "b": {"b": 1}, "spraying": {}}, **gen,
                                           "components": {"a": {}, "b": {}, "spraying": {}},
                                           "cost_items": [{"id": "cs", "component": "spraying", "basis": "per_outcome",
                                                           "outcome": "doses"}]},
                         analysis={"decision_tests": [dta], "cost_outputs": {"doses": {"cost_driver": "a_doses"}}})
            r = eligible_engines(spray, entry)[0]
            ok(not r["eligible"] and any("T4" in w for w in r["why_not"]),
               f"G20 a cost benchmark gates when its engine cost driver is charged through any component's item "
               f"({r['why_not'][:1]})")
            write_cov(cov)
            write_results(rows_in)
            only_b = dict(spec_ab, options={"engine_map": {"a": {"a": 1}, "b": {"b": 1}},
                                            "packages": {"none": [], "b_only": ["b"]}})
            r = eligible_engines(only_b, entry)[0]
            ok(r["eligible"], f"G21 a component in no allowed package is not checked ({r['why_not']})")
            write_cov(cov, band=(0.4, 0.7))          # the pack the coverage was written for now declares another band
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("declaration" in w for w in r["why_not"]),
               f"G19 a record row scored against another declaration of the same id is refused ({r['why_not'][:1]})")
            write_cov(dict(cov, benchmarks=dict(cov["benchmarks"], T1=dict(cov["benchmarks"]["T1"],
                                                                         effect_parameters=["no_such"]))))
            write_results(rows_in)
            r = eligible_engines(spec_ab, entry)[0]
            ok(not r["eligible"] and any("no_such" in w for w in r["why_not"]),
               f"G23 a declared effect parameter the engine does not have is refused ({r['why_not'][:1]})")
            write_cov(cov)
            write_results(rows_in)
            run3 = os.path.join(d, "run3")
            os.makedirs(run3)
            schema.toy_spec(run3, bench=rp)
            from toolkit.spec import geometry as GEO
            yaml.safe_dump({"hard_blockers": [{"id": "HB-001"}]}, open(os.path.join(run3, signoff.DB.CRITERIA), "w"))
            yaml.safe_dump(signoff.DB.example_brief(), open(os.path.join(run3, signoff.DB.BRIEF), "w"))
            GEO.toy_write_sheet(run3)
            try:
                signoff.DB.sign(run3, "owner", accept_proposed=True)
                ok(False, "G22 the brief's signing path refuses an engine that fails the benchmark precondition")
            except ValueError as ex:
                ok("T1" in str(ex), f"G22 the brief's signing path refuses an engine that fails the benchmark "
                                    f"precondition ({str(ex)[:90]})")
            # review round 3: effective packages, departure shape on every signing path, engine auto candidates
            from toolkit.spec import preconditions as PC
            write_cov(cov)
            write_results([dict(rows_in[0], status="in_band"), dict(rows_in[1], status="out_of_band"), rows_in[2]])
            run4 = os.path.join(d, "run4")
            os.makedirs(run4)
            p4 = schema.toy_spec(run4, bench=rp)
            raw4 = yaml.safe_load(open(p4))
            e4 = PC.problems(run4, open(p4, "rb").read(), "owner")
            ok(any("T2" in x for x in e4), f"G25a the toy spec using b fails on T2 ({e4[:1]})")
            raw4["options"]["packages"] = {"generate": True, "within_envelope": True}
            raw4["options"]["cost_items"][1]["value"] = 1.0e9           # b costs more than the envelope everywhere
            yaml.safe_dump(raw4, open(p4, "w"), sort_keys=False)
            pk4 = schema.load(p4).packages()
            e4 = PC.problems(run4, open(p4, "rb").read(), "owner")
            ok(not any("b" in c for c in pk4.values()) and not any("T2" in x for x in e4),
               f"G25 a component envelope pruning removes from every package is not checked ({sorted(pk4)}, {e4[:1]})")
            write_results(rows_in)
            raw5 = yaml.safe_load(open(p))
            raw5["engine_departures"] = [{k: v for k, v in dep.items() if k != "reason"}]
            b5 = yaml.safe_dump(raw5, sort_keys=False).encode()
            e5 = PC.problems(run, b5, "owner")
            ok(any("engine_departures" in x and "reason" in x for x in e5),
               f"G26 the shared signing check validates the departures' shape ({e5[:1]})")
            run6 = os.path.join(d, "run6")
            os.makedirs(run6)
            p6 = schema.toy_spec(run6, bench=rp)
            raw6 = yaml.safe_load(open(p6))
            raw6["engine"] = {"auto": True, "candidates": [TOY]}
            yaml.safe_dump(raw6, open(p6, "w"), sort_keys=False)
            json.dump({"engine": {"ref": TOY, "config": {"results_path": rp}}}, open(os.path.join(run6, "engine_choice.json"), "w"))
            e6 = PC.problems(run6, open(p6, "rb").read(), "owner")
            os.remove(os.path.join(run6, "engine_choice.json"))
            e7 = PC.problems(run6, open(p6, "rb").read(), "owner")
            ok(any("T1" in x for x in e6) and any("T1" in x for x in e7),
               f"G27 engine auto: the resolved engine (engine_choice.json) and, while pending, every candidate is "
               f"checked ({e6[:1]}, {e7[:1]})")
            # round 4: the spec-only signer runs the shared check for engine: auto too (every candidate)
            try:
                signoff.sign(run6, "owner")
                ok(False, "G28 signing an engine: auto spec whose only candidate fails is refused")
            except ValueError as ex:
                ok("T1" in str(ex), f"G28 signing an engine: auto spec whose only candidate fails is refused "
                                    f"({str(ex)[:90]})")
            e8 = schema.validate(schema.load(p6), pending_ok=True)
            ok(any("T1" in x for x in e8), f"G28b pending schema validation runs the shared check ({e8[:1]})")
            # round 4: candidate assessment reads run-local inputs from the run directory, whatever the process cwd
            write_results([dict(rows_in[0], status="in_band"), dict(rows_in[1], status="out_of_band"), rows_in[2]])
            raw4b = dict(yaml.safe_load(open(p4)), _run_dir=run4)
            r = eligible_engines(raw4b, [(TOY, None, None)])[0]
            ok(r["eligible"], f"G29 the candidate check prunes packages with the run's own units table (base is the "
                              f"run directory) ({r['why_not'][:1]})")
            write_results(rows_in)
            # round 4: a departure whose test did not take effect during the run is a non-declarable finding that
            # bears on the decision (it goes to the owner; it blocks delivery)
            # λ: the contract recomputes each departure's verdict from the manifest-hashed decision-test records;
            # there is no separate check file to edit
            from toolkit.analysis import contract as CT
            dk = os.path.join(run, "models", "decision_tests")
            os.makedirs(dk, exist_ok=True)
            fd = [f for f in CT.gate_findings(run) if f["kind"] == "departure_test_ineffective"]
            ok(fd and fd[0]["scope_declarable"] is False and fd[0].get("decision_bearing") is True,
               f"G30 a departure whose test has no record in the manifest is a non-declarable, decision-bearing "
               f"finding ({fd[:1]})")
            pk_ab = next(k for k, cs in schema.load(p).packages().items() if cs == {"a"})
            good = {"test_id": "DTa", "status": "run", "produced_by": "harness", "perturbation_identity": [False, False],
                    "benefit_change_by_package": {pk_ab: 1.0}, "missing_subtraction": [],
                    "package_components": {k: sorted(v) for k, v in schema.load(p).packages().items()}}
            rp_ = os.path.join(dk, "DTa.yaml")
            yaml.safe_dump(good, open(rp_, "w"))
            from toolkit.analysis.artifacts import sha256 as _sha
            json.dump({"deliverables": {"models/decision_tests/DTa.yaml": {"role": "decision_test",
                                                                           "sha256": _sha(rp_)}}},
                      open(os.path.join(run, "analysis", "manifest.json") if os.path.isdir(os.path.join(run, "analysis"))
                           else (os.makedirs(os.path.join(run, "analysis")) or os.path.join(run, "analysis",
                                                                                          "manifest.json")), "w"))
            fd = [f for f in CT.departure_findings(run)]
            ok(fd == [], f"G30c a manifest-listed record showing the test ran and changed the pair's benefit: no "
                         f"finding ({fd[:1]})")
            yaml.safe_dump(dict(good, benefit_change_by_package={}), open(rp_, "w"))
            fd = [f for f in CT.departure_findings(run)]
            ok(fd and "not in the manifest" in fd[0]["claim"],
               f"G30b a record edited after the run no longer matches its manifest hash: refused ({fd[:1]})")
            raw_o = yaml.safe_load(open(p))
            yaml.safe_dump(dict(raw_o, engine_departures=[dict(dep, engine="other-engine")]),
                           open(os.path.join(run, "decision_spec.yaml"), "w"), sort_keys=False)
            fd = [f for f in CT.departure_findings(run)]
            ok(fd == [], f"G30d departures for an engine the run did not use are out of the check's scope ({fd[:1]})")
            yaml.safe_dump(raw_o, open(os.path.join(run, "decision_spec.yaml"), "w"), sort_keys=False)
            # r5: the scope is the run's own engine (the manifest's engine identity), never the coverage file's name
            mp_ = os.path.join(run, "analysis", "manifest.json")
            man_ = json.load(open(mp_))
            yaml.safe_dump(dict(good, perturbation_identity=[True, True]), open(rp_, "w"))     # an ineffective test
            man_["deliverables"]["models/decision_tests/DTa.yaml"]["sha256"] = _sha(rp_)
            json.dump(dict(man_, engine={"name": "package-rate", "version": "1"}), open(mp_, "w"))
            fd = [f for f in CT.departure_findings(run)]
            ok(fd == [], f"G30e a run of another engine (one without a coverage file) is not blocked by departures "
                         f"for this engine ({fd[:1]})")
            yaml.safe_dump(dict(good, perturbation_identity=[True, True]), open(rp_, "w"))
            man_["deliverables"]["models/decision_tests/DTa.yaml"]["sha256"] = _sha(rp_)
            json.dump(dict(man_, engine={"name": "toy", "version": "1"}), open(mp_, "w"))
            write_cov(dict(cov, engine="renamed"))
            fd = [f for f in CT.departure_findings(run)]
            ok(fd and all(f["kind"] == "departure_test_ineffective" for f in fd),
               f"G30f renaming the coverage file's engine does not take the run's departures out of scope ({fd[:1]})")
            write_cov(cov)
            # review: the configuration the run uses, and the named-engine sheet
            write_cov(cov)
            write_results(rows_in)
            named = dict(spec_ab, engine={"ref": TOY, "config": {"results_path": rp}})
            r = eligible_engines(named, [(TOY, None, None)])[0]
            ok(r.get("config") == {"results_path": rp} and any("T1" in w for w in r["why_not"]),
               f"G17 a registered engine without a configuration function is assessed with the spec's own "
               f"configuration ({r.get('config')}, {r['why_not'][:1]})")
            api = backend()
            if isinstance(api, _ToolkitApi):
                run2 = os.path.join(d, "run2")
                os.makedirs(run2)
                p2 = schema.toy_spec(run2, bench=rp)
                raw2 = yaml.safe_load(open(p2))
                raw2["engine_departures"] = [dep]
                raw2["analysis"]["decision_tests"].append(dta)
                yaml.safe_dump(raw2, open(p2, "w"), sort_keys=False)
                probs = api.pre_sign_problems(run2)
                cands = getattr(api, "candidates", [])
                sh = signoff.sheet(run2, candidates=cands)
                ok(probs == [] and cands and cands[0]["departures"][0]["status"] == "proposed"
                   and "role: held-out validation" in sh,
                   f"G18 a named engine's required departure is on the sign-off sheet with status, role and "
                   f"components ({probs[:1]}, {[c.get('departures') for c in cands]})")
        finally:
            BCmod.OVERRIDES.pop(TOY, None)
    # review round 3: an alias import of an engine behaves exactly like its canonical reference
    em = {"pyr": {"net": "pyr"}, "cm": {"cm": 1}}
    f6 = {"options": {"engine_map": em, "components": {"pyr": {}, "cm": {}}, "packages": {"generate": True}}}
    canon = "toolkit.engines.malaria.engine:MalariaEngine"
    rc = eligible_engines(f6, [(canon, None, None)])[0]
    ra = eligible_engines(f6, [("toolkit.engines.malaria:MalariaEngine", None, None)])[0]
    strip = lambda r: sorted(w.replace(r["ref"], "") for w in r["why_not"])  # noqa: E731
    ok(not rc["eligible"] and strip(rc) == strip(ra) and any("component cm" in w for w in ra["why_not"]),
       f"G24 an alias import of the malaria engine is assessed exactly like the canonical reference "
       f"({strip(ra)[:1]})")


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    from toolkit.engines.contract import ToyEngine
    card = ToyEngine().describe()
    ok(card_fits(card, {"components": {"a": [1], "b": [1]}, "outputs": ["dalys"], "windows": ["prev"],
                        "unit_attributes": ["x"]}) == [], "B1 the toy card fits a spec it covers")
    why = card_fits(card, {"components": {"net": ["pbo"]}, "outputs": ["deaths"], "unit_attributes": ["m0"],
                           "ranges": {"x": [-5, 1]}})
    ok(len(why) == 4, f"B2 missing component, outcome, attribute and an out-of-range value are each named: {why}")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "units.csv")
        write_toy_units(p)
        spec = toy_spec("units.csv")
        import yaml
        yaml.safe_dump(toy_settings(), open(os.path.join(d, SETTINGS_FILE), "w"))
        rows = eligible_engines(_abs_spec(spec, d))
        toy = next(r for r in rows if r["ref"].endswith("ToyEngine"))
        mal = next(r for r in rows if "malaria" in r["ref"])
        ok(toy["eligible"] and not mal["eligible"] and mal["why_not"],
           f"B3 the filter picks the toy engine for a toy spec and says why the other is not eligible ({mal['why_not'][:2]})")
        ok(validate(spec, d) == [], f"B4 the toy spec validates from run-relative paths {validate(spec, d)}")
        bad = copy.deepcopy(spec)
        bad["options"]["engine_map"]["a"] = {"a": 7}
        ok(any("engine component value" in e for e in validate(bad, d)), "B5 a component the engine lacks fails validation")
        ok(gate_findings(d)[0]["kind"] == "analysis_missing" and not gate_findings(d)[0]["scope_declarable"],
           "B6 no analysis output is a non-declarable HIGH")
        yaml.safe_dump(spec, open(os.path.join(d, SPEC_FILE), "w"))
        ok(validate(os.path.join(d, SPEC_FILE)) == [] and merged_spec(d)["uncertainty"]["draws"] == 6,
           "B4b the spec path validates merged with its settings file")
        steps = pipeline_steps(os.path.join(d, SPEC_FILE), d)
        ok([s["id"] for s in steps] == ["configure", "analyze"] and "units.csv" in steps[0]["inputs"]
           and SETTINGS_FILE in steps[1]["inputs"],
           "B7 the pipeline has configure and analyze, with the spec and its data as inputs")
        sys.path.insert(0, os.path.join(REPO, "scripts"))
        import pipeline as P
        yaml.safe_dump({"steps": steps}, open(os.path.join(d, "models_pipeline.yaml"), "w"))
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        os.replace(os.path.join(d, "models_pipeline.yaml"), os.path.join(d, "models", "pipeline.yaml"))
        rep = P.run(d)
        ok(rep["ok"] and [s["id"] for s in rep["steps"]] == ["configure", "analyze"], f"B8 the S1 runner runs both steps {rep.get('errors')}")
        h = headline(d)
        ok(h.get("averted.dalys", 0) > 0 and h["fixed_plan.dalys.hi95"] >= h["fixed_plan.dalys.lo95"],
           f"B9 the headline is read from the harness output {h}")
        kinds = {f["kind"] for f in gate_findings(d)}
        ok("uncertainty_draws_low" in kinds and "analysis_missing" not in kinds, f"B10 contract checks on real output {kinds}")
        rep2 = P.run(d)
        ok(rep2["ok"] and rep2["steps"] == [], "B11 nothing reruns when nothing changed")
        yaml.safe_dump(toy_settings(draws=24), open(os.path.join(d, SETTINGS_FILE), "w"))
        rep3 = P.run(d)
        ok([s["id"] for s in rep3["steps"]] == ["configure", "analyze"]
           and "uncertainty_draws_low" not in {f["kind"] for f in gate_findings(d)},
           "B12 a settings edit reruns its dependants and the finding clears")
        yaml.safe_dump({"uncertainty": {"draws": 9}, "constraints": {"envelope_usd": 1}},
                       open(os.path.join(d, SETTINGS_FILE), "w"))
        probs = settings_problems(d)
        ok(any("constraints.envelope_usd is not a computational setting" in x for x in probs)
           and any("validation failed" in x for x in validate(os.path.join(d, SPEC_FILE))),
           f"B15 a decision leaf in the settings file is refused and validation fails closed {probs}")
        yaml.safe_dump({"calibration": {"attribute": "y"}}, open(os.path.join(d, SETTINGS_FILE), "w"))
        ok(settings_problems(d), "B16 a settings leaf the signed spec also sets is refused")
        os.remove(os.path.join(d, SETTINGS_FILE))
        os.symlink(os.path.join(d, SPEC_FILE), os.path.join(d, SETTINGS_FILE))
        ok(any("symlink" in x for x in settings_problems(d)), "B17 a symlinked settings file is refused")
        os.remove(os.path.join(d, SETTINGS_FILE))
        yaml.safe_dump(toy_settings(draws=24), open(os.path.join(d, SETTINGS_FILE), "w"))
        A = json.load(open(os.path.join(d, ANALYSIS_JSON)))
        A["decision_tests"] = [{"id": "DT1", "plan_changes": True, "text": "t"}]
        json.dump(A, open(os.path.join(d, ANALYSIS_JSON), "w"))
        dtf = [f for f in gate_findings(d) if f["kind"] == "decision_test_changes_plan"]
        ok(dtf and dtf[0].get("decision_bearing") and dtf[0]["severity"] == "HIGH",
           "B18 a measured decision test that changes the plan is a decision-bearing HIGH")
        cc = cross_check(merged_spec(d), ["toolkit.engines.contract:ToyEngine"] * 2, os.path.join(d, "xc"), 8, run_dir=d)
        ok(cc["agreement"] == 1.0 and cc["units"] == 8, f"B13 the cross-engine check compares plans (same engine twice agrees) {cc['agreement']}")
    from types import SimpleNamespace as NS
    fake = NS(validate=lambda s, b=None: [], headline=lambda d: {}, gate_findings=lambda d: [],
              pipeline_steps=lambda sp, d: [{"id": "analyze", "cmd": "python3 x.py", "inputs": [], "outputs": [],
                                             "produced_by": "harness"}])
    st = _ToolkitApi(fake).pipeline_steps("s", "d")
    ok(st == [{"id": "analyze", "run": f"{sys.executable} x.py", "inputs": [], "outputs": []}],
       f"B14 workstream B's step (cmd, produced_by) becomes an S1 step (run) under the harness's interpreter {st}")
    fake.engine_needs = lambda spec: {"components": {"a": [1]}, "outputs": ["dalys"]}
    rows = _ToolkitApi(fake).eligible_engines({}, [("toolkit.engines.contract:ToyEngine", None)])
    ok(rows[0]["eligible"], "B19 with B's engine_needs the filter uses B's vocabulary")
    pr = eligible_engines({"options": {"components": {"itn": {}, "smc": {}}, "engine_map": {"itn": {"itn": 1}, "smc": {"smc": 1}}},
                           "objective": {"outcome": "cases"}, "calibration": {"attribute": "log_rate"}})
    emp = next(r for r in pr if r["ref"].endswith("PackageRateEngine"))
    ok(emp["eligible"] and emp["config"] == {"components": ["itn", "smc"], "outcome": "cases"},
       f"B23 the empirical engine is registered, configured from the spec, and eligible for a panel question {emp.get('why_not')}")
    with tempfile.TemporaryDirectory() as d:
        json.dump(engine_choice_record("x.y:Z", "w", []), open(os.path.join(d, ENGINE_CHOICE), "w"))
        ok(engine_choice_ref(d) == "x.y:Z", "B20 engine_choice.json in the agreed format ({engine: {ref, config}})")

    def boom(spec, base=None):
        raise ValueError("analysis_settings.yaml: constraints.envelope_usd is not a computational setting")
    fake.validate = boom
    v = _ToolkitApi(fake).validate("x/decision_spec.yaml")
    ok(len(v) == 1 and "not a computational setting" in v[0], f"B21 B's raising validate becomes a problem list {v}")
    import types
    saved = {k: sys.modules.get(k) for k in ("toolkit.analysis", "toolkit.analysis.api", "toolkit.spec", "toolkit.spec.schema")}
    try:
        for k in saved:
            sys.modules[k] = types.ModuleType(k)
        sys.modules["toolkit.analysis"].api = fake
        sys.modules["toolkit.analysis.api"] = fake
        sys.modules["toolkit.spec"].schema = sys.modules["toolkit.spec.schema"]
        sys.modules["toolkit.spec.schema"].SETTINGS_PATHS = frozenset({("uncertainty", "draws")})
        no_auto = backend()
        fake.ENGINE_AUTO = True
        with_auto = backend()
    finally:
        for k, mod in saved.items():
            if mod is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = mod
    ok(no_auto is sys.modules[__name__] and isinstance(with_auto, _ToolkitApi),
       "B22 B's API is used only once its schema takes `engine: auto` from engine_choice.json")
    # B25: the compartmental engine is registered with the configuration its signed spec gives
    cfg = {"groups": ["a", "b"], "structure": "age_groups_contact_matrix", "outcomes": ["hosp"],
           "components": {"da": {"kind": "doses", "group": "a", "block_size": 10, "max_blocks": 2}}}
    sp = {"engine": {"ref": COMPARTMENTAL, "config": cfg}, "objective": {"outcome": "hosp"},
          "options": {"engine_map": {"x1": {"da": 1}, "x2": {"da": 2}}},
          "decision_units": {"engine_attributes": {"N": {}, "contact": {}, "s0": {}, "ratio:hosp": {}},
                             "populations": {"all": {"age_groups": ["a", "b"]}}}}
    rows = {r["ref"]: r for r in eligible_engines(sp)}
    ok(COMPARTMENTAL in rows and rows[COMPARTMENTAL]["eligible"] and rows[COMPARTMENTAL]["config"] == cfg,
       f"B25 the compartmental engine is eligible for a spec that names it with its configuration: "
       f"{rows.get(COMPARTMENTAL, {}).get('why_not')}")
    sp2 = dict(sp, engine={"ref": "toolkit.engines.contract:ToyEngine"})
    r2 = {r["ref"]: r for r in eligible_engines(sp2)}
    ok(not r2[COMPARTMENTAL]["eligible"] and "configured by the signed spec" in str(r2[COMPARTMENTAL]["why_not"]),
       "B26 without its configuration in the signed spec the compartmental engine is not eligible")
    many = dict(sp2, options={"components": {f"c{i}": {} for i in range(40)}, "engine_map": {}})
    r3 = {r["ref"]: r for r in eligible_engines(many)}
    emp = next(v for k, v in r3.items() if "PackageRateEngine" in k)
    ok(not emp["eligible"] and "at most" in str(emp["why_not"]),
       "B27 a spec with 40 components makes the package-rate engine ineligible at once (it would enumerate 2^40 "
       "combinations)")
    import tempfile as _tf2
    with _tf2.TemporaryDirectory() as d:
        json.dump(engine_choice_record(COMPARTMENTAL, "test", [], config=cfg), open(os.path.join(d, ENGINE_CHOICE), "w"))
        r4 = {r["ref"]: r for r in eligible_engines(dict(sp, engine="auto", _run_dir=d))}
        r5 = {r["ref"]: r for r in eligible_engines(dict(sp, engine="auto"))}
        ok(r4[COMPARTMENTAL]["eligible"] and r4[COMPARTMENTAL]["config"] == cfg and not r5[COMPARTMENTAL]["eligible"],
           "B28 an `engine: auto` spec (a rethink) keeps the compartmental configuration of the run's engine choice; "
           "with no choice in the run it is not eligible")
    # an estimates spec (no allocation units): the renewal engine is the one eligible registered engine, configured
    # from the spec's dynamics; for an allocation spec it is not eligible
    import tempfile as _tf
    from toolkit.analysis import api as _api
    from toolkit.spec import estimates as _ES
    with _tf.TemporaryDirectory() as d:
        raw = dict(_ES.toy_spec(), engine="auto")
        rows = eligible_engines(dict(raw, engine_needs=_api.engine_needs(raw)))
        el = [r["ref"] for r in rows if r.get("eligible")]
        ren = next(r for r in rows if r["ref"].endswith("RenewalEngine"))
        ok(el == ["toolkit.engines.renewal.engine:RenewalEngine"]
           and ren["config"]["generation_interval"] == {"dt": "week", "w": [0.5, 0.5]},
           f"B23 an estimates spec: only the renewal engine is eligible, configured from dynamics ({el})")
        tspec = toy_spec(os.path.join(d, "u.csv"))
        rows2 = eligible_engines(dict(tspec, engine_needs=_api.engine_needs(tspec)))
        ok(not next(r for r in rows2 if r["ref"].endswith("RenewalEngine"))["eligible"],
           "B23b an allocation spec: the renewal engine is not eligible")
    _self_test_benchmark_gate(ok)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    # Pipeline steps run with at most 2 BLAS threads per process: oversubscribed BLAS threads
    # stalled toy analyses for an hour on a loaded laptop (workstream B measured 8.6 min -> 42 s).
    for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ.setdefault(_v, "2")
    sys.exit(main())
