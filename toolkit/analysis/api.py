#!/usr/bin/env python3
"""Entry points for the stage machine (workstream C, agents/analysis_backend.py).

    validate(spec, base=None) -> list[str]           fail closed (toolkit/spec/schema.py)
    pipeline_steps(spec_path, run_dir) -> list[dict] steps for models/pipeline.yaml (S1 runner)
    headline(run_dir) -> dict[str, float]            numbers the impact ledger records before/after a fix
    gate_findings(run_dir) -> list[dict]             contract checks, common finding shape

    python3 toolkit/analysis/api.py --self-test
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis import contract  # noqa: E402
from toolkit.analysis.artifacts import read  # noqa: E402
from toolkit.spec import schema  # noqa: E402


# The schema reads `engine: auto` (the engine chosen after sign-off, in engine_choice.json written by the harness).
ENGINE_AUTO = True


def engine_needs(spec: dict) -> dict:
    """What an engine must offer to run this spec (for the stage machine's eligibility filter): engine component values,
    per-capita outputs, cost drivers, window metrics, unit attributes and age groups."""
    comps = {}
    for m in ((spec.get("options") or {}).get("engine_map") or {}).values():
        for k, v in (m or {}).items():
            comps.setdefault(k, [])
            if v not in comps[k]:
                comps[k].append(v)
    cal, an = spec.get("calibration") or {}, spec.get("analysis") or {}
    outs = [(spec.get("objective") or {}).get("outcome")] + list(spec.get("outcomes_reported") or [])
    if cal.get("method") == "likelihood":
        outs.append((cal.get("targets") or {}).get("outcome") or (spec.get("objective") or {}).get("outcome"))
    wins = ([cal["metric"]] if cal.get("method") == "emulator_inversion" and cal.get("metric") else []) + \
        [d.get("metric") for d in (an.get("diagnostics") or {}).values()]
    du = spec.get("decision_units") or {}
    attrs = list((du.get("engine_attributes") or {}).keys()) + ([cal["attribute"]] if cal.get("attribute") else [])
    ages = []
    for pm in (du.get("populations") or {}).values():
        ages += [g for g in (pm or {}).get("age_groups") or [] if g not in ages]
    uniq = lambda xs: [x for i, x in enumerate(xs) if x and x not in xs[:i]]  # noqa: E731
    return {"components": comps, "outputs": uniq(outs),
            "cost_drivers": uniq([(m or {}).get("cost_driver") for m in (an.get("cost_outputs") or {}).values()]),
            "windows": uniq(wins), "unit_attributes": uniq(attrs), "age_groups": ages}


def validate(spec, base: str | None = None) -> list[str]:
    if isinstance(spec, str):
        return schema.validate(schema.load(spec))
    return schema.validate(spec, base)


def pipeline_steps(spec_path: str, run_dir: str) -> list[dict]:
    """One step: the analysis reads the spec and its data and writes analysis/ and the deliverables. The engine
    batch inside it is resumable (scripts/compute.py keeps rows per input), so a rerun after a spec edit only
    computes what changed in the batch; the steps after it are cheap."""
    spec = schema.load(spec_path)
    deps = [os.path.relpath(spec_path, run_dir), os.path.relpath(spec.resolve(spec["decision_units"]["csv"]), run_dir)]
    if spec.settings_path:
        deps.append(os.path.relpath(spec.settings_path, run_dir))
    if spec.engine_auto:
        deps.append(schema.ENGINE_CHOICE)
    if spec["calibration"].get("method") == "likelihood":
        deps.append(os.path.relpath(spec.resolve(spec["calibration"]["targets"]["csv"]), run_dir))
    # uncertainty_report.yaml and models/claims_ledger.yaml are written too, but S1 reserves them for their
    # registered producers (agents/enforcement.py); the manifest (and the contract checks) track them instead.
    outs = ["analysis/manifest.json", spec["deliverable"]["allocation_file"], "results.md"]
    return [{"id": "analyze", "cmd": f"{sys.executable} {os.path.join(ROOT, 'toolkit', 'analysis', 'run.py')} {run_dir} "
                                     f"--spec {os.path.relpath(spec_path, run_dir)}",
             "inputs": deps, "outputs": outs, "produced_by": "harness"}]


def headline(run_dir: str) -> dict:
    p = read(run_dir, "analysis/point.yaml")["data"]
    u = read(run_dir, "analysis/uncertainty.yaml")["data"]
    o = next(iter(p["averted"]))
    out = {f"point_averted_{k}": float(v) for k, v in p["averted"].items()}
    fp = u["fixed_plan"][o]
    out.update({f"fixed_plan_{o}_lo": fp["ci_low"], f"fixed_plan_{o}_hi": fp["ci_high"],
                "cost_allocated": float(p["cost_allocated"])})
    try:
        s = read(run_dir, "analysis/stability.yaml")["data"]
        if s["metrics"].get("total_benefit_loss_pct_worst_fold") is not None:
            out["stability_value_lost_pct_worst"] = float(s["metrics"]["total_benefit_loss_pct_worst_fold"])
    except OSError:
        pass
    return out


def gate_findings(run_dir: str) -> list[dict]:
    return contract.gate_findings(run_dir)


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        ok(validate(p) == [] and validate({"schema_version": 1}, d), "AP1 validate: a path or a mapping, fail closed")
        st = pipeline_steps(p, d)
        ok(st and st[0]["outputs"][0] == "analysis/manifest.json" and "decision_spec.yaml" in st[0]["inputs"],
           f"AP2 pipeline steps name their inputs and outputs: {st}")
        ok(st[0]["cmd"].startswith(sys.executable + " "), f"AP7 the step runs with the harness interpreter: {st[0]['cmd'][:60]}")
        ok(gate_findings(d) and gate_findings(d)[0]["kind"] == "analysis_contract_missing",
           "AP3 gate_findings before the analysis ran: missing (fail closed)")
        try:
            need = engine_needs(schema.load(p).raw)
            ok(need["components"] == {"a": [1], "b": [1]} and "dalys" in need["outputs"] and "cases" in need["outputs"]
               and need["windows"] == ["prev"] and "x" in need["unit_attributes"],
               f"AP4 engine_needs states what an engine must offer for this spec: {need}")
        except Exception as e:  # noqa: BLE001
            ok(False, f"AP4 engine_needs: {type(e).__name__}: {e}")
        ok(globals().get("ENGINE_AUTO") is True, "AP5 the API declares engine: auto support")
        import json
        import yaml
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"auto": True}
        yaml.safe_dump(raw, open(p, "w"))
        json.dump({"engine": {"ref": "toolkit.engines.contract:ToyEngine"}, "why": "w"},
                  open(os.path.join(d, "engine_choice.json"), "w"))
        try:
            st = pipeline_steps(p, d)
            ok("engine_choice.json" in st[0]["inputs"], f"AP6 the engine choice is an input of the analyze step: {st}")
        except Exception as e:  # noqa: BLE001
            ok(False, f"AP6 the engine choice is an input of the analyze step: {type(e).__name__}: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else (print(__doc__) or 2))
