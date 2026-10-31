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
    if spec["calibration"].get("method") == "likelihood":
        deps.append(os.path.relpath(spec.resolve(spec["calibration"]["targets"]["csv"]), run_dir))
    # uncertainty_report.yaml and models/claims_ledger.yaml are written too, but S1 reserves them for their
    # registered producers (agents/enforcement.py); the manifest (and the contract checks) track them instead.
    outs = ["analysis/manifest.json", spec["deliverable"]["allocation_file"], "results.md"]
    return [{"id": "analyze", "cmd": f"python3 {os.path.join(ROOT, 'toolkit', 'analysis', 'run.py')} {run_dir} "
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
        ok(gate_findings(d) and gate_findings(d)[0]["kind"] == "analysis_contract_missing",
           "AP3 gate_findings before the analysis ran: missing (fail closed)")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else (print(__doc__) or 2))
