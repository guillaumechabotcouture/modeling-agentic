#!/usr/bin/env python3
"""
Phase 12 Commit γ — ecological-fallacy / within-zone heterogeneity
sensitivity analysis.

When a model calibrates to k zones but allocates to n>>k spatial
units (Nigeria: 6 zones → 774 LGAs in the 104914 run), within-zone
PfPR variation is invisible to the optimizer. results.md
acknowledges this as a one-line caveat but doesn't bound the
impact. A reviewer asking "is this a 5% impact loss or 25%?" gets
no answer.

This script defines and validates the
`models/within_zone_heterogeneity.yaml` artifact:

```yaml
calibration_units: 6
allocation_units: 774
within_unit_value_range: {min: 0.059, max: 0.773, metric: PfPR}
modeled_uniform_per_unit: true
sensitivity:
  - perturbation: lga_pfpr_uniform_within_zone (baseline)
    cases_averted: 54700000
  - perturbation: lga_pfpr_normal_within_zone (sd from data)
    cases_averted: 49000000
    impact_loss_pct: 10.4
verdict: BOUNDED   # <10%, INCONCLUSIVE 10-25%, UNBOUNDED >25%
notes: |
  Per-LGA PfPR distributed Normal(zone_mean, sd_observed); optimizer
  re-run; cases-averted reduction is the impact loss bound.
```

Verdict thresholds (worst-case impact_loss_pct across perturbations):
- BOUNDED:    max impact_loss_pct < 10%
- INCONCLUSIVE: 10-25%
- UNBOUNDED:  > 25%

Usage:
    python3 scripts/within_zone_sensitivity.py <run_dir>
    python3 scripts/within_zone_sensitivity.py --self-test
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


VALID_VERDICTS = {"BOUNDED", "INCONCLUSIVE", "UNBOUNDED"}
BOUNDED_THRESHOLD = 10.0      # impact_loss_pct < 10% → BOUNDED
INCONCLUSIVE_THRESHOLD = 25.0  # 10-25% → INCONCLUSIVE; >25% → UNBOUNDED


def _compute_verdict(perturbations: list[dict]) -> tuple[str, dict]:
    """Compute verdict from worst-case impact_loss_pct across
    perturbations. Returns (verdict, summary). Summary contains
    worst_loss, n_perturbations. Returns "MALFORMED" with empty
    summary when no perturbations have impact_loss_pct (defense-in-
    depth)."""
    worst = 0.0
    n = 0
    for p in perturbations:
        loss = p.get("impact_loss_pct")
        if loss is None:
            continue  # baseline perturbation (no loss)
        try:
            loss = float(loss)
        except (TypeError, ValueError):
            continue
        n += 1
        if abs(loss) > worst:
            worst = abs(loss)

    summary = {"worst_loss_pct": worst, "n_perturbations": n}

    if n == 0:
        return "MALFORMED", summary

    if worst < BOUNDED_THRESHOLD:
        return "BOUNDED", summary
    if worst < INCONCLUSIVE_THRESHOLD:
        return "INCONCLUSIVE", summary
    return "UNBOUNDED", summary


def validate_within_zone_heterogeneity(yaml_path: str) -> dict:
    """Load and validate within_zone_heterogeneity.yaml. Returns:

      verdict: BOUNDED | INCONCLUSIVE | UNBOUNDED | MALFORMED | MISSING
      computed_verdict: recomputed from data (None if MALFORMED/MISSING)
      reported_verdict: what the file claims
      errors: list of strings
      summary: {worst_loss_pct, n_perturbations}
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
    for required in ("calibration_units", "allocation_units",
                     "modeled_uniform_per_unit", "sensitivity"):
        if required not in data:
            errors.append(f"missing required field {required!r}")

    perturbations = data.get("sensitivity") or []
    if not isinstance(perturbations, list):
        errors.append("sensitivity must be a list of perturbations")
        perturbations = []

    if isinstance(perturbations, list):
        # At least 2: baseline + ≥1 perturbation with impact_loss_pct
        n_with_loss = sum(1 for p in perturbations
                          if isinstance(p, dict) and "impact_loss_pct" in p)
        if n_with_loss < 1:
            errors.append(
                "at least 1 perturbation must include impact_loss_pct "
                "(the baseline-vs-perturbation impact bound)")
        for i, p in enumerate(perturbations):
            if not isinstance(p, dict):
                errors.append(f"sensitivity[{i}] must be a mapping")
                continue
            if "perturbation" not in p:
                errors.append(f"sensitivity[{i}] missing 'perturbation' label")
            if "cases_averted" not in p:
                errors.append(f"sensitivity[{i}] missing 'cases_averted'")
            if "impact_loss_pct" in p:
                try:
                    float(p["impact_loss_pct"])
                except (TypeError, ValueError):
                    errors.append(
                        f"sensitivity[{i}].impact_loss_pct must be numeric "
                        f"(got {p['impact_loss_pct']!r})")

    if errors:
        return {"verdict": "MALFORMED", "computed_verdict": None,
                "reported_verdict": data.get("verdict"),
                "errors": errors, "summary": {}}

    computed, summary = _compute_verdict(perturbations)
    if computed == "MALFORMED":
        return {"verdict": "MALFORMED", "computed_verdict": None,
                "reported_verdict": data.get("verdict"),
                "errors": ["No perturbations with impact_loss_pct found"],
                "summary": summary}

    reported = data.get("verdict")
    if reported is not None and reported not in VALID_VERDICTS:
        return {"verdict": "MALFORMED", "computed_verdict": computed,
                "reported_verdict": reported,
                "errors": [f"verdict {reported!r} not recognized; "
                           f"must be one of {sorted(VALID_VERDICTS)}"],
                "summary": summary}

    if reported is not None and reported != computed:
        return {"verdict": "MALFORMED", "computed_verdict": computed,
                "reported_verdict": reported,
                "errors": [f"verdict mismatch: reported {reported!r} but "
                           f"data computes {computed!r} (worst impact loss "
                           f"{summary['worst_loss_pct']:.1f}%). Either "
                           f"correct the reported verdict or the perturbation "
                           f"outcomes."],
                "summary": summary}

    return {"verdict": computed, "computed_verdict": computed,
            "reported_verdict": reported, "errors": [], "summary": summary}


def _run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as d:
        # W1: BOUNDED — worst impact 8% < 10
        f1 = os.path.join(d, "w1.yaml")
        with open(f1, "w") as f:
            f.write(
                "calibration_units: 6\n"
                "allocation_units: 774\n"
                "modeled_uniform_per_unit: true\n"
                "sensitivity:\n"
                "  - perturbation: baseline\n"
                "    cases_averted: 54700000\n"
                "  - perturbation: lga_normal\n"
                "    cases_averted: 50300000\n"
                "    impact_loss_pct: 8.0\n"
                "verdict: BOUNDED\n"
            )
        r1 = validate_within_zone_heterogeneity(f1)
        ok(r1["verdict"] == "BOUNDED",
           f"W1: 8% loss should be BOUNDED, got {r1}")

        # W2: INCONCLUSIVE — 18% loss
        f2 = os.path.join(d, "w2.yaml")
        with open(f2, "w") as f:
            f.write(
                "calibration_units: 6\n"
                "allocation_units: 774\n"
                "modeled_uniform_per_unit: true\n"
                "sensitivity:\n"
                "  - perturbation: baseline\n"
                "    cases_averted: 54700000\n"
                "  - perturbation: lga_high_var\n"
                "    cases_averted: 44850000\n"
                "    impact_loss_pct: 18.0\n"
                "verdict: INCONCLUSIVE\n"
            )
        r2 = validate_within_zone_heterogeneity(f2)
        ok(r2["verdict"] == "INCONCLUSIVE",
           f"W2: 18% should be INCONCLUSIVE, got {r2}")

        # W3: UNBOUNDED — 32% loss
        f3 = os.path.join(d, "w3.yaml")
        with open(f3, "w") as f:
            f.write(
                "calibration_units: 6\n"
                "allocation_units: 774\n"
                "modeled_uniform_per_unit: true\n"
                "sensitivity:\n"
                "  - perturbation: baseline\n"
                "    cases_averted: 54700000\n"
                "  - perturbation: lga_extreme\n"
                "    cases_averted: 37200000\n"
                "    impact_loss_pct: 32.0\n"
                "verdict: UNBOUNDED\n"
            )
        r3 = validate_within_zone_heterogeneity(f3)
        ok(r3["verdict"] == "UNBOUNDED",
           f"W3: 32% should be UNBOUNDED, got {r3}")

        # W4: MISSING — file doesn't exist
        r4 = validate_within_zone_heterogeneity(os.path.join(d, "nope.yaml"))
        ok(r4["verdict"] == "MISSING",
           f"W4: missing file should be MISSING, got {r4}")

        # W5: MALFORMED — required field missing
        f5 = os.path.join(d, "w5.yaml")
        with open(f5, "w") as f:
            f.write(
                "sensitivity:\n"
                "  - perturbation: baseline\n"
                "    cases_averted: 54700000\n"
                "  - perturbation: x\n"
                "    cases_averted: 50000000\n"
                "    impact_loss_pct: 5.0\n"
            )  # missing calibration_units, allocation_units, modeled_uniform
        r5 = validate_within_zone_heterogeneity(f5)
        ok(r5["verdict"] == "MALFORMED",
           f"W5: missing required field should be MALFORMED, got {r5}")

        # W6: MALFORMED — reported verdict mismatch (claims BOUNDED but
        # data computes UNBOUNDED).
        f6 = os.path.join(d, "w6.yaml")
        with open(f6, "w") as f:
            f.write(
                "calibration_units: 6\n"
                "allocation_units: 774\n"
                "modeled_uniform_per_unit: true\n"
                "sensitivity:\n"
                "  - perturbation: baseline\n"
                "    cases_averted: 54700000\n"
                "  - perturbation: x\n"
                "    cases_averted: 30000000\n"
                "    impact_loss_pct: 45.0\n"
                "verdict: BOUNDED\n"  # claims BOUNDED but data is UNBOUNDED
            )
        r6 = validate_within_zone_heterogeneity(f6)
        ok(r6["verdict"] == "MALFORMED",
           f"W6: reported BOUNDED vs computed UNBOUNDED should be MALFORMED, "
           f"got {r6}")
        ok(r6["computed_verdict"] == "UNBOUNDED",
           f"W6: computed_verdict should still be UNBOUNDED, got {r6}")

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
        p.error("run_dir required (or use --self-test)")

    yaml_path = os.path.join(args.run_dir, "models",
                             "within_zone_heterogeneity.yaml")
    result = validate_within_zone_heterogeneity(yaml_path)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"verdict: {result['verdict']}", file=sys.stderr)
        if result.get("summary"):
            s = result["summary"]
            print(f"worst impact loss: {s.get('worst_loss_pct', 0):.1f}%, "
                  f"n_perturbations: {s.get('n_perturbations', 0)}",
                  file=sys.stderr)
        if result.get("errors"):
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)

    return 0 if result["verdict"] == "BOUNDED" else 1


if __name__ == "__main__":
    sys.exit(main())
