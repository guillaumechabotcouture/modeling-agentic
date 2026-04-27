#!/usr/bin/env python3
"""
Phase 8 Commit π — load-bearing-parameter sensitivity analysis.

When the modeler produces an allocation, they must also report
whether the recommendation is robust to perturbations of its 2-3
load-bearing parameters. The artifact lives at
`{run_dir}/models/sensitivity_analysis.yaml` with the schema:

    load_bearing_parameters:
      - id: SA-001
        parameter: pbo_lifecycle_or
        description: "PBO net OR — drives dual-AI vs PBO selection"
        primary_value: 0.55
        primary_objective: 4565827
        perturbations:
          - value: 0.37
            objective: 4480000
            rank_change_top_n: 0
            primary_recommendation_changes: false
            notes: "PBO becomes preferred over dual-AI in 38 LGAs..."
          - value: 0.81
            objective: 4710000
            rank_change_top_n: 0
            primary_recommendation_changes: false
      - id: SA-002
        ...
    verdict: ROBUST | SENSITIVE | UNSTABLE
    notes: |
      Each load-bearing parameter is perturbed to its 95% CI endpoints
      with the optimizer re-run.

Verdict thresholds (worst perturbation across all parameters):
  ROBUST:    no perturbation flips primary_recommendation AND
             max rank_change_top_n <= 10
  SENSITIVE: <=1 perturbation flips OR max rank_change_top_n <= 30
  UNSTABLE:  >=2 perturbations flip OR max rank_change_top_n > 30

This script validates the file's structure, recomputes the verdict,
and emits a JSON report. It is invoked by the validator's
_check_sensitivity_analysis when an allocation artifact exists.

Usage:
    python3 scripts/sensitivity_analysis.py <run_dir>
    python3 scripts/sensitivity_analysis.py --self-test
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


VALID_VERDICTS = {"ROBUST", "SENSITIVE", "UNSTABLE"}

# Verdict thresholds applied to the worst (most-disruptive) perturbation
# across all load-bearing parameters.
ROBUST_RANK_THRESHOLD = 10
SENSITIVE_RANK_THRESHOLD = 30


def _compute_verdict(parameters: list[dict]) -> tuple[str, dict]:
    """Compute the worst-case verdict from the perturbation outcomes.

    Returns (verdict, summary) where summary has worst_rank_change,
    flips, and total_perturbations. Returns "MALFORMED" with an empty
    summary when there are no perturbations to evaluate — defense-in-
    depth so a caller that bypasses the schema check still cannot
    silently obtain a ROBUST verdict from no data.
    """
    flips = 0
    worst_rank = 0
    total = 0
    for p in parameters:
        for pert in p.get("perturbations") or []:
            total += 1
            if pert.get("primary_recommendation_changes") is True:
                flips += 1
            rc = pert.get("rank_change_top_n")
            if isinstance(rc, (int, float)) and rc > worst_rank:
                worst_rank = int(rc)

    summary = {
        "flips": flips,
        "worst_rank_change_top_n": worst_rank,
        "total_perturbations": total,
    }

    if total == 0:
        return "MALFORMED", summary

    if flips == 0 and worst_rank <= ROBUST_RANK_THRESHOLD:
        return "ROBUST", summary
    if flips <= 1 and worst_rank <= SENSITIVE_RANK_THRESHOLD:
        return "SENSITIVE", summary
    return "UNSTABLE", summary


def validate_sensitivity_analysis(yaml_path: str) -> dict:
    """Load and validate the sensitivity_analysis.yaml file.

    Returns a dict with keys:
      verdict: ROBUST | SENSITIVE | UNSTABLE | MALFORMED | MISSING
      computed_verdict: the verdict recomputed from the data
                       (None when MALFORMED/MISSING)
      reported_verdict: what the file claims (None when MALFORMED/MISSING)
      errors: list of strings
      summary: dict with flips, worst_rank_change_top_n, total_perturbations
    """
    if not os.path.exists(yaml_path):
        return {"verdict": "MISSING", "computed_verdict": None,
                "reported_verdict": None,
                "errors": [f"{yaml_path} does not exist"], "summary": {}}

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return {"verdict": "MALFORMED", "computed_verdict": None,
                "reported_verdict": None,
                "errors": [f"YAML parse error: {e}"], "summary": {}}

    errors: list[str] = []
    parameters = data.get("load_bearing_parameters")
    if not isinstance(parameters, list) or not parameters:
        errors.append(
            "load_bearing_parameters must be a non-empty list. The "
            "modeler must identify at least 2 load-bearing parameters "
            "(parameters whose 95% CI endpoints could change the "
            "recommendation) and report perturbation outcomes for each."
        )

    if isinstance(parameters, list):
        for i, p in enumerate(parameters):
            if not isinstance(p, dict):
                errors.append(f"load_bearing_parameters[{i}] must be a mapping")
                continue
            for required in ("parameter", "primary_value",
                             "primary_objective", "perturbations"):
                if required not in p:
                    errors.append(
                        f"load_bearing_parameters[{i}] missing required "
                        f"field {required!r}")
            perts = p.get("perturbations")
            if not isinstance(perts, list) or not perts:
                errors.append(
                    f"load_bearing_parameters[{i}].perturbations must be "
                    f"a non-empty list (at least one alternative value "
                    f"per parameter)")
                continue
            for j, pert in enumerate(perts):
                if not isinstance(pert, dict):
                    errors.append(
                        f"load_bearing_parameters[{i}].perturbations[{j}] "
                        f"must be a mapping")
                    continue
                for required in ("value", "objective", "rank_change_top_n",
                                 "primary_recommendation_changes"):
                    if required not in pert:
                        errors.append(
                            f"load_bearing_parameters[{i}].perturbations"
                            f"[{j}] missing field {required!r}")
                if "rank_change_top_n" in pert:
                    rc = pert["rank_change_top_n"]
                    if not isinstance(rc, (int, float)):
                        errors.append(
                            f"load_bearing_parameters[{i}].perturbations[{j}]"
                            f".rank_change_top_n must be numeric")
                    elif isinstance(rc, bool) or rc < 0:
                        # bool is a subclass of int in Python; reject
                        # explicitly. Negative rank changes are
                        # meaningless (the count of LGAs whose package
                        # assignment differs cannot be < 0).
                        errors.append(
                            f"load_bearing_parameters[{i}].perturbations[{j}]"
                            f".rank_change_top_n must be a non-negative "
                            f"integer (got {rc!r})")
                if "primary_recommendation_changes" in pert and not isinstance(
                        pert["primary_recommendation_changes"], bool):
                    errors.append(
                        f"load_bearing_parameters[{i}].perturbations[{j}]"
                        f".primary_recommendation_changes must be boolean")

    if errors:
        return {"verdict": "MALFORMED", "computed_verdict": None,
                "reported_verdict": data.get("verdict"),
                "errors": errors, "summary": {}}

    # The schema requires >= 2 load-bearing parameters: a single
    # perturbation does not establish robustness across the recommendation.
    if len(parameters) < 2:
        return {"verdict": "MALFORMED", "computed_verdict": None,
                "reported_verdict": data.get("verdict"),
                "errors": [
                    f"At least 2 load_bearing_parameters required "
                    f"(found {len(parameters)}). Pick parameters whose "
                    f"95% CI endpoints would plausibly change the "
                    f"dominant package or top-N rank ordering."
                ], "summary": {}}

    computed, summary = _compute_verdict(parameters)

    # _compute_verdict returns MALFORMED when total perturbations == 0.
    # Surface that here as a schema error rather than a verdict.
    if computed == "MALFORMED":
        return {"verdict": "MALFORMED", "computed_verdict": None,
                "reported_verdict": data.get("verdict"),
                "errors": [
                    "No perturbations found across any load_bearing_"
                    "parameters; cannot compute a verdict from empty "
                    "data."
                ], "summary": summary}

    reported = data.get("verdict")
    if reported is not None and reported not in VALID_VERDICTS:
        return {"verdict": "MALFORMED", "computed_verdict": computed,
                "reported_verdict": reported,
                "errors": [
                    f"verdict {reported!r} not recognized; "
                    f"must be one of {sorted(VALID_VERDICTS)}"
                ], "summary": summary}

    # Reported-vs-computed mismatch: the modeler self-reports a verdict
    # the data does not support. Flag as MALFORMED so the modeler must
    # either (a) correct the reported verdict or (b) correct the
    # perturbation outcomes that produced the disagreement. Silent
    # acceptance of the computed value would let a self-reported
    # ROBUST stand even when the perturbations show flips.
    if reported is not None and reported != computed:
        return {
            "verdict": "MALFORMED",
            "computed_verdict": computed,
            "reported_verdict": reported,
            "errors": [
                f"verdict mismatch: reported {reported!r} but data "
                f"computes {computed!r} (flips={summary['flips']}, "
                f"worst rank change top-N="
                f"{summary['worst_rank_change_top_n']}). Either correct "
                f"the reported verdict to match the data or fix the "
                f"perturbation outcomes."
            ],
            "summary": summary,
        }

    return {
        "verdict": computed,
        "computed_verdict": computed,
        "reported_verdict": reported,
        "errors": [],
        "summary": summary,
    }


def _run_self_test() -> int:
    """Inline self-test cases. Returns 0 on success, 1 on failure."""
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as d:
        # T1: ROBUST — no flips, all rank changes < 10.
        f1 = os.path.join(d, "t1.yaml")
        with open(f1, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4480000\n"
                "        rank_change_top_n: 5\n"
                "        primary_recommendation_changes: false\n"
                "      - value: 0.81\n"
                "        objective: 4710000\n"
                "        rank_change_top_n: 3\n"
                "        primary_recommendation_changes: false\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: ROBUST\n"
            )
        r1 = validate_sensitivity_analysis(f1)
        ok(r1["verdict"] == "ROBUST",
           f"T1: all rank changes <=10, no flips, expected ROBUST, got {r1}")

        # T2: SENSITIVE — one flip, rank changes within 30.
        f2 = os.path.join(d, "t2.yaml")
        with open(f2, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 25\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: SENSITIVE\n"
            )
        r2 = validate_sensitivity_analysis(f2)
        ok(r2["verdict"] == "SENSITIVE",
           f"T2: 1 flip, max rank 25, expected SENSITIVE, got {r2}")

        # T3: UNSTABLE — multiple flips.
        f3 = os.path.join(d, "t3.yaml")
        with open(f3, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 50\n"
                "        primary_recommendation_changes: true\n"
                "      - value: 0.81\n"
                "        objective: 4720000\n"
                "        rank_change_top_n: 40\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: UNSTABLE\n"
            )
        r3 = validate_sensitivity_analysis(f3)
        ok(r3["verdict"] == "UNSTABLE",
           f"T3: 2 flips, max rank 50, expected UNSTABLE, got {r3}")

        # T4: UNSTABLE — single flip but huge rank change.
        f4 = os.path.join(d, "t4.yaml")
        with open(f4, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 80\n"
                "        primary_recommendation_changes: false\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: UNSTABLE\n"
            )
        r4 = validate_sensitivity_analysis(f4)
        ok(r4["verdict"] == "UNSTABLE",
           f"T4: rank change 80 (>30) should be UNSTABLE, got {r4}")

        # T5: MALFORMED — single load-bearing parameter.
        f5 = os.path.join(d, "t5.yaml")
        with open(f5, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 5\n"
                "        primary_recommendation_changes: false\n"
                "verdict: ROBUST\n"
            )
        r5 = validate_sensitivity_analysis(f5)
        ok(r5["verdict"] == "MALFORMED",
           f"T5: only 1 parameter should be MALFORMED, got {r5}")

        # T6: MALFORMED — empty perturbations.
        f6 = os.path.join(d, "t6.yaml")
        with open(f6, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations: []\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: ROBUST\n"
            )
        r6 = validate_sensitivity_analysis(f6)
        ok(r6["verdict"] == "MALFORMED",
           f"T6: empty perturbations should be MALFORMED, got {r6}")

        # T7: MISSING — file doesn't exist.
        r7 = validate_sensitivity_analysis(os.path.join(d, "nope.yaml"))
        ok(r7["verdict"] == "MISSING",
           f"T7: missing file should be MISSING, got {r7}")

        # T8: MALFORMED — bad type for primary_recommendation_changes.
        f8 = os.path.join(d, "t8.yaml")
        with open(f8, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 5\n"
                "        primary_recommendation_changes: maybe\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
            )
        r8 = validate_sensitivity_analysis(f8)
        ok(r8["verdict"] == "MALFORMED",
           f"T8: non-bool primary_recommendation_changes should be MALFORMED, "
           f"got {r8}")

        # T9 (review fix #2): reported verdict ROBUST disagrees with
        # computed UNSTABLE → MALFORMED.
        f9 = os.path.join(d, "t9.yaml")
        with open(f9, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: 50\n"
                "        primary_recommendation_changes: true\n"
                "      - value: 0.81\n"
                "        objective: 4720000\n"
                "        rank_change_top_n: 40\n"
                "        primary_recommendation_changes: true\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
                "verdict: ROBUST\n"  # data computes UNSTABLE; modeler self-deception
            )
        r9 = validate_sensitivity_analysis(f9)
        ok(r9["verdict"] == "MALFORMED",
           f"T9: reported ROBUST vs computed UNSTABLE should fire MALFORMED, "
           f"got {r9}")
        ok(r9["computed_verdict"] == "UNSTABLE",
           f"T9: computed_verdict should still be exposed as UNSTABLE, "
           f"got {r9['computed_verdict']}")
        ok(r9["reported_verdict"] == "ROBUST",
           f"T9: reported_verdict should be exposed as ROBUST, "
           f"got {r9['reported_verdict']}")

        # T10 (review fix #4): negative rank_change_top_n is meaningless
        # → MALFORMED.
        f10 = os.path.join(d, "t10.yaml")
        with open(f10, "w") as f:
            f.write(
                "load_bearing_parameters:\n"
                "  - id: SA-001\n"
                "    parameter: pbo_or\n"
                "    primary_value: 0.55\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.37\n"
                "        objective: 4400000\n"
                "        rank_change_top_n: -5\n"
                "        primary_recommendation_changes: false\n"
                "  - id: SA-002\n"
                "    parameter: smc_irr\n"
                "    primary_value: 0.27\n"
                "    primary_objective: 4565827\n"
                "    perturbations:\n"
                "      - value: 0.25\n"
                "        objective: 4640000\n"
                "        rank_change_top_n: 8\n"
                "        primary_recommendation_changes: false\n"
            )
        r10 = validate_sensitivity_analysis(f10)
        ok(r10["verdict"] == "MALFORMED",
           f"T10: negative rank_change_top_n should fire MALFORMED, got {r10}")

        # T11 (review fix #3): _compute_verdict on empty parameters
        # returns MALFORMED rather than silent ROBUST.
        verdict, summary = _compute_verdict([])
        ok(verdict == "MALFORMED",
           f"T11: _compute_verdict([]) should return MALFORMED, got {verdict}")
        ok(summary["total_perturbations"] == 0,
           f"T11: summary.total_perturbations should be 0, got {summary}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
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

    yaml_path = os.path.join(args.run_dir, "models", "sensitivity_analysis.yaml")
    result = validate_sensitivity_analysis(yaml_path)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"verdict: {result['verdict']}", file=sys.stderr)
        if result.get("summary"):
            s = result["summary"]
            print(f"flips: {s.get('flips')}, "
                  f"worst rank change top-N: "
                  f"{s.get('worst_rank_change_top_n')}, "
                  f"total perturbations: {s.get('total_perturbations')}",
                  file=sys.stderr)
        if result.get("errors"):
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)

    return 0 if result["verdict"] == "ROBUST" else 1


if __name__ == "__main__":
    sys.exit(main())
