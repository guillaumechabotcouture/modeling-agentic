#!/usr/bin/env python3
"""
Phase 6 Commit κ — allocation cross-validation.

When the modeler produces an allocation, they must also test whether
the allocation rule generalizes to held-out spatial units. The
required artifact `{run_dir}/models/allocation_robustness.yaml` records
the modeler's k-fold holdout results.

Schema:

    holdout_method: leave-one-archetype-out | leave-one-state-out |
                     5-fold-by-archetype | spatial-block-cv | ...
    n_folds: 22
    metrics:
      rank_correlation_mean: 0.87
      rank_correlation_worst_fold: 0.62
      cases_averted_gap_pct_mean: 4.3
      cases_averted_gap_pct_worst_fold: 18.1
      rule_classification_concordance_pct_mean: 91.2
      rule_classification_concordance_pct_worst_fold: 73.0
    verdict: ROBUST | FRAGILE | UNSTABLE  # modeler's call; recomputed
    notes: |
      Per-fold details ...

Why this matters:

A 22-archetype calibration that achieves 7.8pp RMSE in-sample tells
you nothing about whether the resulting ALLOCATION RULE generalizes
to a held-out 23rd archetype. An experienced senior modeler validates
that the optimal allocation is stable under spatial holdout — i.e.,
that re-optimizing on n-k of the units produces a substantially
similar allocation rule for the remaining k. Without this test, the
"23.1% efficiency gain" could be an artifact of overfitting the
optimizer to specific archetype EIRs.

Usage:
    python3 scripts/allocation_robustness.py <run_dir>
    python3 scripts/allocation_robustness.py --self-test
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


VALID_HOLDOUT_METHODS = {
    "leave-one-archetype-out",
    "leave-one-state-out",
    "leave-one-zone-out",
    "5-fold-by-archetype",
    "10-fold-by-archetype",
    "spatial-block-cv",
    "leave-one-out",
    "k-fold",
}

# Verdict thresholds — applied to WORST-fold metrics.
ROBUST_RANK_CORR_MIN = 0.70
ROBUST_CASES_GAP_MAX = 15.0
ROBUST_RULE_CONCORDANCE_MIN = 80.0

FRAGILE_RANK_CORR_MIN = 0.40
FRAGILE_CASES_GAP_MAX = 30.0
FRAGILE_RULE_CONCORDANCE_MIN = 60.0


def compute_verdict(metrics: dict) -> str:
    """Recompute verdict from worst-fold metrics. Returns ROBUST,
    FRAGILE, or UNSTABLE."""
    rank_worst = metrics.get("rank_correlation_worst_fold")
    gap_worst = metrics.get("cases_averted_gap_pct_worst_fold")
    rule_worst = metrics.get("rule_classification_concordance_pct_worst_fold")

    # Any UNSTABLE signal → UNSTABLE
    if rank_worst is not None and rank_worst < FRAGILE_RANK_CORR_MIN:
        return "UNSTABLE"
    if gap_worst is not None and gap_worst > FRAGILE_CASES_GAP_MAX:
        return "UNSTABLE"
    if rule_worst is not None and rule_worst < FRAGILE_RULE_CONCORDANCE_MIN:
        return "UNSTABLE"

    # All ROBUST signals → ROBUST
    rank_ok = rank_worst is None or rank_worst >= ROBUST_RANK_CORR_MIN
    gap_ok = gap_worst is None or gap_worst <= ROBUST_CASES_GAP_MAX
    rule_ok = rule_worst is None or rule_worst >= ROBUST_RULE_CONCORDANCE_MIN
    if rank_ok and gap_ok and rule_ok:
        return "ROBUST"

    return "FRAGILE"


def validate_allocation_robustness(yaml_path: str) -> dict:
    """Validate the YAML, recompute the verdict from metrics. Returns
    dict with keys: verdict (ROBUST/FRAGILE/UNSTABLE/MALFORMED/MISSING),
    metrics, errors, modeler_verdict (if present), holdout_method,
    n_folds.
    """
    if not os.path.exists(yaml_path):
        return {"verdict": "MISSING", "errors": [
            f"{yaml_path} does not exist"], "metrics": None}

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return {"verdict": "MALFORMED", "errors": [
            f"YAML parse error: {e}"], "metrics": None}

    errors: list[str] = []

    holdout = data.get("holdout_method")
    if not isinstance(holdout, str) or holdout not in VALID_HOLDOUT_METHODS:
        # Permissive: accept other strings if they at least look like
        # cross-validation methods (contain "fold" or "leave" or "cv").
        if isinstance(holdout, str) and any(
                tok in holdout.lower() for tok in ("fold", "leave", "cv", "holdout")):
            pass  # accept custom name
        else:
            errors.append(
                f"holdout_method {holdout!r} not recognized. "
                f"Must be one of {sorted(VALID_HOLDOUT_METHODS)} or "
                f"contain a CV-style token (fold/leave/cv/holdout)."
            )

    n_folds = data.get("n_folds")
    if not isinstance(n_folds, int) or n_folds < 2:
        errors.append(f"n_folds must be int >= 2, got {n_folds!r}")

    metrics = data.get("metrics") or {}
    if not isinstance(metrics, dict):
        errors.append("metrics must be a mapping")
        metrics = {}

    # At minimum, require ONE of the three metric pairs.
    has_any_metric = any(
        f"{base}_worst_fold" in metrics
        for base in ("rank_correlation",
                     "cases_averted_gap_pct",
                     "rule_classification_concordance_pct")
    )
    if not has_any_metric:
        errors.append(
            "metrics must include at least one of: "
            "rank_correlation_worst_fold, cases_averted_gap_pct_worst_fold, "
            "rule_classification_concordance_pct_worst_fold"
        )

    if errors:
        return {"verdict": "MALFORMED", "errors": errors, "metrics": metrics,
                "holdout_method": holdout, "n_folds": n_folds}

    verdict = compute_verdict(metrics)
    return {
        "verdict": verdict,
        "errors": [],
        "metrics": metrics,
        "modeler_verdict": data.get("verdict"),
        "holdout_method": holdout,
        "n_folds": n_folds,
    }


def _run_self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as d:
        # Case T1: ROBUST.
        f1 = os.path.join(d, "t1.yaml")
        with open(f1, "w") as f:
            f.write(
                "holdout_method: leave-one-archetype-out\n"
                "n_folds: 22\n"
                "metrics:\n"
                "  rank_correlation_mean: 0.87\n"
                "  rank_correlation_worst_fold: 0.78\n"
                "  cases_averted_gap_pct_worst_fold: 8.5\n"
                "  rule_classification_concordance_pct_worst_fold: 88\n"
                "verdict: ROBUST\n"
            )
        r1 = validate_allocation_robustness(f1)
        ok(r1["verdict"] == "ROBUST",
           f"T1: all metrics within ROBUST band, got {r1['verdict']}")

        # Case T2: FRAGILE.
        f2 = os.path.join(d, "t2.yaml")
        with open(f2, "w") as f:
            f.write(
                "holdout_method: leave-one-state-out\n"
                "n_folds: 37\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.55\n"
                "  cases_averted_gap_pct_worst_fold: 20.0\n"
                "  rule_classification_concordance_pct_worst_fold: 70\n"
            )
        r2 = validate_allocation_robustness(f2)
        ok(r2["verdict"] == "FRAGILE",
           f"T2: middle band metrics, got {r2['verdict']}")

        # Case T3: UNSTABLE (worst rank corr < 0.4).
        f3 = os.path.join(d, "t3.yaml")
        with open(f3, "w") as f:
            f.write(
                "holdout_method: 5-fold-by-archetype\n"
                "n_folds: 5\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.30\n"
                "  cases_averted_gap_pct_worst_fold: 12\n"
                "  rule_classification_concordance_pct_worst_fold: 85\n"
            )
        r3 = validate_allocation_robustness(f3)
        ok(r3["verdict"] == "UNSTABLE",
           f"T3: worst rank corr 0.30 should fire UNSTABLE, got {r3['verdict']}")

        # Case T4: MALFORMED (no metrics).
        f4 = os.path.join(d, "t4.yaml")
        with open(f4, "w") as f:
            f.write(
                "holdout_method: leave-one-archetype-out\n"
                "n_folds: 22\n"
                "metrics: {}\n"
            )
        r4 = validate_allocation_robustness(f4)
        ok(r4["verdict"] == "MALFORMED",
           f"T4: empty metrics should be MALFORMED, got {r4['verdict']}")

        # Case T5: MISSING.
        r5 = validate_allocation_robustness(os.path.join(d, "nope.yaml"))
        ok(r5["verdict"] == "MISSING",
           f"T5: nonexistent file should be MISSING, got {r5['verdict']}")

        # Case T6: custom holdout name accepted (contains "fold").
        f6 = os.path.join(d, "t6.yaml")
        with open(f6, "w") as f:
            f.write(
                "holdout_method: spatial_block_3fold_by_zone\n"
                "n_folds: 3\n"
                "metrics:\n"
                "  rank_correlation_worst_fold: 0.85\n"
            )
        r6 = validate_allocation_robustness(f6)
        ok(r6["verdict"] == "ROBUST",
           f"T6: custom holdout name with 'fold' token should accept, "
           f"got {r6}")

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

    yaml_path = os.path.join(args.run_dir, "models", "allocation_robustness.yaml")
    result = validate_allocation_robustness(yaml_path)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"verdict: {result['verdict']}", file=sys.stderr)
        if result.get("metrics"):
            for k, v in result["metrics"].items():
                print(f"  {k}: {v}", file=sys.stderr)
        for e in result.get("errors") or []:
            print(f"  - {e}", file=sys.stderr)

    return 0 if result["verdict"] == "ROBUST" else 1


if __name__ == "__main__":
    sys.exit(main())
