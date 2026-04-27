#!/usr/bin/env python3
"""
Phase 7 Commit μ — plan-promised criteria enforcement.

Currently the planner writes prose ## Success Criteria sections in
plan.md (### Hard Blockers, ### Minimum Bar, ### Targets) that the
pipeline never gate-enforces. The 1935 malaria run promised
"malariasimulation cross-validation" as a minimum-bar criterion;
modeler couldn't deliver; it became §Limitations bullet #3 instead
of an actual blocker.

Phase 7 μ requires the planner to ALSO emit a structured
`{run_dir}/success_criteria.yaml` alongside plan.md. The schema:

    hard_blockers:
      - id: HB-001
        criterion: "Model reproduces NMIS 2021 zone PfPR within ±3 pp"
        metric: zone_pfpr_rmse_pp
        threshold: 3.0
        operator: "<="
        artifact: model_comparison_formal.yaml
        artifact_field: rmse_pp  # path to value in artifact (dot-syntax)

    minimum_bar:
      - id: MB-001
        criterion: "Cross-validation against malariasimulation"
        metric: malariasimulation_comparison_done
        threshold: 1
        operator: "=="
        artifact: model_comparison_formal.yaml
        artifact_field: malariasimulation_comparison_done

    targets:
      - ...

This script evaluates each criterion against the named artifact and
returns PASS / FAIL / NOT_TESTED per entry. The validator folds
hard_blocker failures as HIGH, minimum_bar failures as MEDIUM
(escalating to HIGH after Phase 5 ζ stuck-blocker logic).

Usage:
    python3 scripts/plan_criteria.py <run_dir>
    python3 scripts/plan_criteria.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed", file=sys.stderr)
    sys.exit(2)


VALID_OPERATORS = {"<=", ">=", "<", ">", "==", "!="}
TIER_NAMES = ("hard_blockers", "minimum_bar", "targets")


def _resolve_dotted(data: dict, path: str):
    """Resolve `a.b.c` style dot-path into nested dict. Returns None
    if any intermediate key is missing."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _evaluate_criterion(crit: dict, run_dir: str) -> dict:
    """Evaluate a single criterion against its named artifact.

    Returns {id, criterion, status, evidence, value (or None),
    threshold, operator}. Status is PASS / FAIL / NOT_TESTED /
    MALFORMED.
    """
    out = {
        "id": crit.get("id"),
        "criterion": crit.get("criterion"),
        "metric": crit.get("metric"),
        "threshold": crit.get("threshold"),
        "operator": crit.get("operator"),
        "artifact": crit.get("artifact"),
        "status": "NOT_TESTED",
        "value": None,
        "evidence": "",
    }

    artifact_name = crit.get("artifact")
    if not artifact_name:
        out["status"] = "MALFORMED"
        out["evidence"] = "criterion missing 'artifact' field"
        return out

    operator = crit.get("operator")
    if operator not in VALID_OPERATORS:
        out["status"] = "MALFORMED"
        out["evidence"] = (
            f"operator {operator!r} not in {sorted(VALID_OPERATORS)}"
        )
        return out

    threshold = crit.get("threshold")
    if not isinstance(threshold, (int, float, bool)):
        out["status"] = "MALFORMED"
        out["evidence"] = f"threshold must be numeric/bool, got {threshold!r}"
        return out

    artifact_path = os.path.join(run_dir, artifact_name)
    if not os.path.exists(artifact_path):
        # Try common subdirectories.
        for sub in ("models", "results", "data"):
            candidate = os.path.join(run_dir, sub, artifact_name)
            if os.path.exists(candidate):
                artifact_path = candidate
                break

    if not os.path.exists(artifact_path):
        out["status"] = "NOT_TESTED"
        out["evidence"] = f"artifact {artifact_name!r} not found"
        return out

    try:
        with open(artifact_path) as f:
            artifact_data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        out["status"] = "NOT_TESTED"
        out["evidence"] = f"artifact load error: {e}"
        return out

    field = crit.get("artifact_field") or crit.get("metric")
    value = _resolve_dotted(artifact_data, field) if field else None
    if value is None:
        out["status"] = "NOT_TESTED"
        out["evidence"] = (
            f"field {field!r} not found in {artifact_name!r}"
        )
        return out

    out["value"] = value
    try:
        v = float(value)
        t = float(threshold)
    except (TypeError, ValueError):
        # Boolean / string comparison
        v = value
        t = threshold

    passed = False
    try:
        if operator == "<=":
            passed = v <= t
        elif operator == ">=":
            passed = v >= t
        elif operator == "<":
            passed = v < t
        elif operator == ">":
            passed = v > t
        elif operator == "==":
            passed = v == t
        elif operator == "!=":
            passed = v != t
    except TypeError as e:
        out["status"] = "MALFORMED"
        out["evidence"] = f"comparison error: {e}"
        return out

    out["status"] = "PASS" if passed else "FAIL"
    out["evidence"] = f"{field}={v} {operator} {t} → {out['status']}"
    return out


def evaluate_plan_criteria(run_dir: str) -> dict:
    """Load success_criteria.yaml from run_dir and evaluate each tier.

    Returns {
      verdict: "MISSING" | "MALFORMED" | "OK",
      hard_blockers: [eval_result, ...],
      minimum_bar: [...],
      targets: [...],
      n_hard_failed: int, n_min_failed: int, n_not_tested: int,
    }
    """
    yaml_path = os.path.join(run_dir, "success_criteria.yaml")
    if not os.path.exists(yaml_path):
        return {"verdict": "MISSING", "errors": [
            "success_criteria.yaml not found"]}

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return {"verdict": "MALFORMED", "errors": [f"YAML parse error: {e}"]}

    if not isinstance(data, dict):
        return {"verdict": "MALFORMED", "errors": [
            "success_criteria.yaml must be a top-level mapping"]}

    out = {"verdict": "OK"}
    n_hard_failed = 0
    n_min_failed = 0
    n_not_tested = 0

    for tier in TIER_NAMES:
        entries = data.get(tier) or []
        if not isinstance(entries, list):
            out[tier] = []
            continue
        results = []
        for crit in entries:
            if not isinstance(crit, dict):
                continue
            r = _evaluate_criterion(crit, run_dir)
            results.append(r)
            if r["status"] == "FAIL":
                if tier == "hard_blockers":
                    n_hard_failed += 1
                elif tier == "minimum_bar":
                    n_min_failed += 1
            elif r["status"] == "NOT_TESTED":
                n_not_tested += 1
        out[tier] = results

    out["n_hard_failed"] = n_hard_failed
    out["n_min_failed"] = n_min_failed
    out["n_not_tested"] = n_not_tested
    return out


def _run_self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as d:
        # Setup: success_criteria.yaml + a target artifact.
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "hard_blockers:\n"
                "  - id: HB-001\n"
                "    criterion: \"PfPR RMSE under threshold\"\n"
                "    metric: zone_pfpr_rmse_pp\n"
                "    threshold: 3.0\n"
                "    operator: \"<=\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: zone_pfpr_rmse_pp\n"
                "minimum_bar:\n"
                "  - id: MB-001\n"
                "    criterion: \"malariasimulation cross-validation\"\n"
                "    metric: malariasimulation_comparison_done\n"
                "    threshold: 1\n"
                "    operator: \"==\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: malariasimulation_comparison_done\n"
                "  - id: MB-002\n"
                "    criterion: \"LOO RMSE under 8 pp\"\n"
                "    metric: loo_rmse_pp\n"
                "    threshold: 8.0\n"
                "    operator: \"<=\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: loo_rmse_pp\n"
            )
        with open(os.path.join(d, "model_comparison_formal.yaml"), "w") as f:
            f.write(
                "zone_pfpr_rmse_pp: 2.5\n"
                "loo_rmse_pp: 5.0\n"
                # malariasimulation_comparison_done not present → NOT_TESTED
            )

        result = evaluate_plan_criteria(d)

        # Case T1: hard blocker PASS.
        hb = result["hard_blockers"][0]
        ok(hb["status"] == "PASS",
           f"T1: HB-001 should PASS (2.5 <= 3.0), got {hb}")

        # Case T2: minimum bar NOT_TESTED (field missing).
        mb_001 = result["minimum_bar"][0]
        ok(mb_001["status"] == "NOT_TESTED",
           f"T2: MB-001 should be NOT_TESTED (missing field), got {mb_001}")

        # Case T3: minimum bar PASS.
        mb_002 = result["minimum_bar"][1]
        ok(mb_002["status"] == "PASS",
           f"T3: MB-002 should PASS (5.0 <= 8.0), got {mb_002}")

        # Case T4: counts.
        ok(result["n_hard_failed"] == 0,
           f"T4: n_hard_failed should be 0, got {result['n_hard_failed']}")
        ok(result["n_not_tested"] == 1,
           f"T4: n_not_tested should be 1, got {result['n_not_tested']}")

    # Case T5: hard blocker FAIL.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "hard_blockers:\n"
                "  - id: HB-001\n"
                "    criterion: \"PfPR RMSE under threshold\"\n"
                "    metric: zone_pfpr_rmse_pp\n"
                "    threshold: 3.0\n"
                "    operator: \"<=\"\n"
                "    artifact: model_comparison_formal.yaml\n"
                "    artifact_field: zone_pfpr_rmse_pp\n"
            )
        with open(os.path.join(d, "model_comparison_formal.yaml"), "w") as f:
            f.write("zone_pfpr_rmse_pp: 5.0\n")  # > 3.0
        result = evaluate_plan_criteria(d)
        ok(result["hard_blockers"][0]["status"] == "FAIL",
           f"T5: 5.0 > 3.0 should FAIL, got {result['hard_blockers'][0]}")
        ok(result["n_hard_failed"] == 1,
           f"T5: n_hard_failed should be 1")

    # Case T6: missing success_criteria.yaml → MISSING.
    with tempfile.TemporaryDirectory() as d:
        result = evaluate_plan_criteria(d)
        ok(result["verdict"] == "MISSING",
           f"T6: missing yaml should be MISSING, got {result['verdict']}")

    # Case T7: malformed (operator invalid).
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "hard_blockers:\n"
                "  - id: HB-001\n"
                "    criterion: \"foo\"\n"
                "    metric: bar\n"
                "    threshold: 3.0\n"
                "    operator: \"approximately\"\n"
                "    artifact: x.yaml\n"
                "    artifact_field: bar\n"
            )
        with open(os.path.join(d, "x.yaml"), "w") as f:
            f.write("bar: 2.0\n")
        result = evaluate_plan_criteria(d)
        ok(result["hard_blockers"][0]["status"] == "MALFORMED",
           f"T7: invalid operator should be MALFORMED, "
           f"got {result['hard_blockers'][0]}")

    # Case T8: dotted artifact_field.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "success_criteria.yaml"), "w") as f:
            f.write(
                "minimum_bar:\n"
                "  - id: MB-001\n"
                "    criterion: \"Worst-fold rank correlation\"\n"
                "    metric: rank_corr\n"
                "    threshold: 0.7\n"
                "    operator: \">=\"\n"
                "    artifact: allocation_robustness.yaml\n"
                "    artifact_field: \"metrics.rank_correlation_worst_fold\"\n"
            )
        with open(os.path.join(d, "allocation_robustness.yaml"), "w") as f:
            f.write(
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.85\n"
            )
        result = evaluate_plan_criteria(d)
        ok(result["minimum_bar"][0]["status"] == "PASS",
           f"T8: nested field 0.85 >= 0.7 should PASS, "
           f"got {result['minimum_bar'][0]}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f_ in failures:
            print(f"  - {f_}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()
    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")

    result = evaluate_plan_criteria(args.run_dir)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"verdict: {result.get('verdict')}", file=sys.stderr)
        if result.get("verdict") == "OK":
            print(f"  hard_blockers: {len(result.get('hard_blockers', []))} "
                  f"({result['n_hard_failed']} FAILED)", file=sys.stderr)
            print(f"  minimum_bar:   {len(result.get('minimum_bar', []))} "
                  f"({result['n_min_failed']} FAILED)", file=sys.stderr)
            print(f"  not_tested:    {result['n_not_tested']}", file=sys.stderr)

    return 0 if (result.get("verdict") == "OK"
                 and result.get("n_hard_failed", 0) == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
