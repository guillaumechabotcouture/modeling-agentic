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
    return {"components": comps, "outcome": (spec.get("objective") or {}).get("outcome") or "cases"}


# Registered engines: (import ref, card file read without importing the engine or None,
# config from the spec or None). A configured engine's card depends on its config.
ENGINES = [
    ("toolkit.engines.contract:ToyEngine", None, None),
    ("toolkit.engines.malaria.engine:MalariaEngine", "toolkit/engines/malaria/card.yaml", None),
    ("toolkit.engines.empirical.engine:PackageRateEngine", None, _empirical_config),
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
    mod, cls = ref.split(":")
    return getattr(importlib.import_module(mod), cls)(**(config or {})).describe()


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
    drivers = set((card.outputs or {}).get("cost_drivers") or [])
    why += [f"no cost driver {c}" for c in needs.get("cost_drivers") or [] if c not in drivers]
    why += [f"no age group {g}" for g in needs.get("age_groups") or [] if g not in (card.age_groups or [])]
    for a, (lo, hi) in (needs.get("ranges") or {}).items():
        vr = (card.valid_ranges or {}).get(a)
        if vr and (lo < vr[0] or hi > vr[1]):
            why.append(f"{a} range [{lo}, {hi}] outside the validated [{vr[0]}, {vr[1]}]")
    return why


def eligible_engines(spec: dict, engines=None) -> list[dict]:
    """[{ref, name, version, eligible, why_not, card}] for every registered engine."""
    needs = engine_needs(spec)
    rows = []
    for entry in engines or ENGINES:
        ref, card_path, cfg_fn = (tuple(entry) + (None, None))[:3]
        try:
            config = cfg_fn(spec) if cfg_fn else {}
            card = _card(ref, card_path, config)
        except Exception as e:  # noqa: BLE001 -- recorded: an engine that cannot describe itself is not eligible
            rows.append({"ref": ref, "eligible": False, "why_not": [f"card unreadable: {type(e).__name__}: {e}"]})
            continue
        why = card_fits(card, needs)
        rows.append({"ref": ref, "config": config, "name": card.name, "version": card.version, "eligible": not why,
                     "why_not": why, "mechanisms": list(card.mechanisms)[:8], "benchmarks": len(card.benchmarks or [])})
    return rows


def cross_check(spec: dict, refs: list[str], work_dir: str, n_units: int = 8) -> dict:
    """Run the analysis on the first `n_units` units with each engine and compare the plans."""
    import csv
    plans = {}
    for ref in refs:
        s = copy.deepcopy(spec)
        s["engine"] = ref
        d = os.path.join(work_dir, ref.replace(":", "_").replace(".", "_"))
        os.makedirs(d, exist_ok=True)
        src = s["decision_units"]["csv"]
        rows = list(csv.DictReader(open(src)))[:n_units]
        sub = os.path.join(d, "units.csv")
        with open(sub, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        s["decision_units"]["csv"] = sub
        if isinstance(s.get("options", {}).get("units"), dict):
            s["options"]["units"]["csv"] = sub
        s.setdefault("uncertainty", {})["draws"] = min(int(s["uncertainty"].get("draws", 4)), 4)
        _configure(s, d)
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
    rec = {"engine": {"ref": ref, "config": config or {}}, "why": why, "candidates": candidates}
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


def load_spec(path: str) -> dict:
    return merged_spec(os.path.dirname(os.path.abspath(path)))


def pre_sign_problems(run_dir: str) -> list[str]:
    """Before the engine is chosen: every section the spike needs, and a valid settings file."""
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


def _configure(spec: dict, out: str) -> dict:
    from spike.analysis import batch as B
    for f in ("results.jsonl", "results_prev.jsonl"):
        if os.path.exists(os.path.join(out, f)):
            os.remove(os.path.join(out, f))      # a changed spec invalidates every engine run
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

    def pre_sign_problems(self, run_dir: str) -> list[str]:
        """B's checks of every section that does not need the engine (its sign-off check)."""
        from toolkit.spec import schema
        try:
            return list(schema.validate(schema.load(os.path.join(run_dir, SPEC_FILE)), pending_ok=True))
        except Exception as e:  # noqa: BLE001
            return [f"spec validation failed: {type(e).__name__}: {e}"]

    def choice_spec(self, run_dir: str) -> dict:
        from toolkit.spec import schema
        return schema.load(os.path.join(run_dir, SPEC_FILE)).raw

    # B signs the spec itself (toolkit/spec/signoff.py); its analysis refuses an unsigned spec.
    def sign(self, run_dir: str, by: str, note: str = "") -> dict:
        from toolkit.spec import signoff
        return signoff.sign(run_dir, by, note)

    def signed_sha(self, run_dir: str):
        from toolkit.spec import signoff
        return signoff.signed_sha(run_dir) if signoff.status(run_dir) in ("signed", "changed") else None

    def signoff_status(self, run_dir: str) -> str:
        from toolkit.spec import signoff
        return signoff.status(run_dir)

    def cross_check(self, spec, refs, work_dir, n_units: int = 8):
        return {"skipped": "the cross-engine check runs on the spike analysis only; not yet for workstream B's"}

    def validate(self, spec, base=None) -> list[str]:
        """B's validate raises on some refusals (a bad settings leaf: schema.SpecError); the
        machine needs a list of problems, so any exception is one (fail closed)."""
        try:
            return list(self.api.validate(spec, base) if base is not None else self.api.validate(spec))
        except Exception as e:  # noqa: BLE001
            return [f"spec validation failed: {type(e).__name__}: {e}"]

    def eligible_engines(self, spec, engines=None):
        """The filter in B's vocabulary when B exposes engine_needs(spec); otherwise ours."""
        en = getattr(self.api, "engine_needs", None)
        if en is not None:
            spec = dict(spec, engine_needs=en(spec))
        return eligible_engines(spec, engines)

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
        spec = load_spec(argv[1])
        out = os.path.abspath(argv[2])
        os.makedirs(out, exist_ok=True)
        if argv[0] == "configure":
            print(json.dumps(_configure(spec, out)))
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
        cc = cross_check(merged_spec(d), ["toolkit.engines.contract:ToyEngine"] * 2, os.path.join(d, "xc"), 8)
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
