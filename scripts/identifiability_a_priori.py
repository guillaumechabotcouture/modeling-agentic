#!/usr/bin/env python3
"""
Phase 15 Commit α — a-priori identifiability arithmetic.

The Phase 14 RESUME run (224202) reached ACCEPT with all 3 fitted ABM
parameters flagged `unidentified` (profile_flat_ratio: 0). The
modeler scope-declared each individual signal and proceeded; the ABM
was decorative — predictions were equivalent to PfPR × literature_OR.

The deeper failure: this should have been caught at strategy time,
not post-calibration. With 40 free parameters fitting 6 zone-level
PfPR targets, the model is over-saturated by 6.7× — proven by 30
seconds of arithmetic before any code is written.

This script defines and validates the
`models/identifiability_a_priori.yaml` artifact:

```yaml
stage: pre_model
round_drafted: 1

calibration_targets:
  - source: NMIS 2021 zone PfPR
    n_independent: 6
    derivation: "6 zones × 1 PfPR measurement each"
total_independent_targets: 6

proposed_parameters:
  - name: ext_foi_per_archetype
    count: 20
    fitted: true
    prior_constraint: weak
total_fitted_parameters: 40
total_fixed_parameters: 5

ratio: 6.67                   # total_fitted / total_independent_targets
verdict: OVER_SATURATED       # IDENTIFIABLE | MARGINAL | OVER_SATURATED

architecture_implication: |
  Under HYBRID architecture, calibration round-trips data through
  unidentifiable knobs. ABM is decorative.

resolution:
  decision: tie_params_by_ecotype
  details: |
    Reduce ext_foi from 20 archetype-specific to 5 ecotype-specific.
    New ratio 5/6 = 0.83, verdict IDENTIFIABLE.
```

Verdict thresholds (ratio = total_fitted / total_independent_targets):
- IDENTIFIABLE:    ratio < 1.0
- MARGINAL:        1.0 ≤ ratio ≤ 3.0
- OVER_SATURATED:  ratio > 3.0

Resolution required when verdict != IDENTIFIABLE. Acceptable
decisions: reduce_params, add_calibration_targets,
downgrade_to_analytical, accept_decorative (with details), or
tie_params_by_ecotype (or any other named tying scheme).

Usage:
    python3 scripts/identifiability_a_priori.py <run_dir>
    python3 scripts/identifiability_a_priori.py --self-test
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


VALID_VERDICTS = {"IDENTIFIABLE", "MARGINAL", "OVER_SATURATED"}
IDENTIFIABLE_THRESHOLD = 1.0   # ratio < 1.0 → IDENTIFIABLE
MARGINAL_THRESHOLD = 3.0       # 1.0-3.0 → MARGINAL; >3.0 → OVER_SATURATED


def _compute_verdict(ratio: float) -> str:
    """Compute verdict from fitted-params / independent-targets ratio.
    Returns IDENTIFIABLE | MARGINAL | OVER_SATURATED."""
    if ratio < IDENTIFIABLE_THRESHOLD:
        return "IDENTIFIABLE"
    if ratio <= MARGINAL_THRESHOLD:
        return "MARGINAL"
    return "OVER_SATURATED"


def validate_identifiability_a_priori(yaml_path: str) -> dict:
    """Load and validate identifiability_a_priori.yaml. Returns:

      verdict: IDENTIFIABLE | MARGINAL | OVER_SATURATED | MALFORMED | MISSING
      computed_verdict: recomputed from data (None if MALFORMED/MISSING)
      reported_verdict: what the file claims
      errors: list of strings
      summary: {ratio, n_fitted, n_targets, has_resolution}
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
    for required in ("total_independent_targets", "total_fitted_parameters",
                     "verdict"):
        if required not in data:
            errors.append(f"missing required field {required!r}")

    n_fitted = data.get("total_fitted_parameters")
    n_targets = data.get("total_independent_targets")

    try:
        n_fitted = int(n_fitted) if n_fitted is not None else None
        n_targets = int(n_targets) if n_targets is not None else None
    except (TypeError, ValueError):
        errors.append(
            f"total_fitted_parameters / total_independent_targets must be "
            f"integers (got {data.get('total_fitted_parameters')!r}, "
            f"{data.get('total_independent_targets')!r})")
        n_fitted, n_targets = None, None

    if n_targets is not None and n_targets <= 0:
        errors.append(
            f"total_independent_targets must be positive "
            f"(got {n_targets}); a model with zero targets is not "
            f"calibratable")
    if n_fitted is not None and n_fitted < 0:
        errors.append(
            f"total_fitted_parameters must be non-negative "
            f"(got {n_fitted}); negative parameter counts are "
            f"semantically meaningless and would yield a negative "
            f"ratio that silently bypasses the OVER_SATURATED gate")

    if errors:
        return {"verdict": "MALFORMED", "computed_verdict": None,
                "reported_verdict": data.get("verdict"),
                "errors": errors, "summary": {}}

    ratio = n_fitted / n_targets
    computed = _compute_verdict(ratio)
    summary = {
        "ratio": ratio,
        "n_fitted": n_fitted,
        "n_targets": n_targets,
        "has_resolution": False,
    }

    reported = data.get("verdict")
    if reported not in VALID_VERDICTS:
        return {"verdict": "MALFORMED", "computed_verdict": computed,
                "reported_verdict": reported,
                "errors": [f"verdict {reported!r} not recognized; "
                           f"must be one of {sorted(VALID_VERDICTS)}"],
                "summary": summary}

    if reported != computed:
        return {"verdict": "MALFORMED", "computed_verdict": computed,
                "reported_verdict": reported,
                "errors": [f"verdict mismatch: reported {reported!r} but "
                           f"data computes {computed!r} (ratio {ratio:.2f}). "
                           f"Either correct the reported verdict or the "
                           f"parameter/target counts."],
                "summary": summary}

    # When verdict != IDENTIFIABLE, require a resolution decision.
    # Resolution can be top-level mapping or top-level scalar (free-form
    # justification). Both forms acceptable; the validator only checks
    # that something is documented.
    if computed != "IDENTIFIABLE":
        resolution = data.get("resolution")
        has_resolution = False
        if isinstance(resolution, dict):
            decision = resolution.get("decision")
            details = resolution.get("details")
            if decision and str(decision).strip():
                has_resolution = True
                # accept_decorative additionally requires details text
                if (str(decision).strip().lower() == "accept_decorative"
                        and (not details or not str(details).strip())):
                    return {"verdict": "MALFORMED",
                            "computed_verdict": computed,
                            "reported_verdict": reported,
                            "errors": [
                                "resolution.decision is "
                                "'accept_decorative' but resolution.details "
                                "is empty. Decorative architectures must be "
                                "justified (100-300 words explaining what "
                                "the architecture contributes that the "
                                "analytical model doesn't)."],
                            "summary": summary}
        elif isinstance(resolution, str) and resolution.strip():
            has_resolution = True

        summary["has_resolution"] = has_resolution
        if not has_resolution:
            return {"verdict": "MALFORMED", "computed_verdict": computed,
                    "reported_verdict": reported,
                    "errors": [
                        f"verdict {computed} requires a `resolution` "
                        f"field documenting the architecture-redesign "
                        f"decision. Acceptable decisions: reduce_params, "
                        f"add_calibration_targets, downgrade_to_analytical, "
                        f"tie_params_by_<group>, accept_decorative (with "
                        f"100-300 word justification)."],
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
        # W1: IDENTIFIABLE — 5 fitted / 6 targets = 0.83 < 1.0
        f1 = os.path.join(d, "w1.yaml")
        with open(f1, "w") as f:
            f.write(
                "total_independent_targets: 6\n"
                "total_fitted_parameters: 5\n"
                "verdict: IDENTIFIABLE\n"
            )
        r1 = validate_identifiability_a_priori(f1)
        ok(r1["verdict"] == "IDENTIFIABLE",
           f"W1: 5/6 should be IDENTIFIABLE, got {r1}")

        # W2: MARGINAL with resolution — 12 fitted / 6 targets = 2.0
        f2 = os.path.join(d, "w2.yaml")
        with open(f2, "w") as f:
            f.write(
                "total_independent_targets: 6\n"
                "total_fitted_parameters: 12\n"
                "verdict: MARGINAL\n"
                "resolution:\n"
                "  decision: tie_params_partial\n"
                "  details: ridge_trapping_acknowledged\n"
            )
        r2 = validate_identifiability_a_priori(f2)
        ok(r2["verdict"] == "MARGINAL",
           f"W2: 12/6 with resolution should be MARGINAL, got {r2}")

        # W3: OVER_SATURATED with resolution — 40 fitted / 6 targets = 6.67
        f3 = os.path.join(d, "w3.yaml")
        with open(f3, "w") as f:
            f.write(
                "total_independent_targets: 6\n"
                "total_fitted_parameters: 40\n"
                "verdict: OVER_SATURATED\n"
                "resolution:\n"
                "  decision: tie_params_by_ecotype\n"
                "  details: |\n"
                "    Reduce ext_foi from 20 archetype-specific to 5\n"
                "    ecotype-specific. New ratio 5/6 = 0.83.\n"
            )
        r3 = validate_identifiability_a_priori(f3)
        ok(r3["verdict"] == "OVER_SATURATED",
           f"W3: 40/6 with resolution should be OVER_SATURATED, got {r3}")

        # W4: OVER_SATURATED missing resolution → MALFORMED
        f4 = os.path.join(d, "w4.yaml")
        with open(f4, "w") as f:
            f.write(
                "total_independent_targets: 6\n"
                "total_fitted_parameters: 40\n"
                "verdict: OVER_SATURATED\n"
            )
        r4 = validate_identifiability_a_priori(f4)
        ok(r4["verdict"] == "MALFORMED"
           and any("resolution" in e for e in r4["errors"]),
           f"W4: OVER_SATURATED without resolution should be MALFORMED, "
           f"got {r4}")

        # W5: MISSING — file doesn't exist
        r5 = validate_identifiability_a_priori(os.path.join(d, "nope.yaml"))
        ok(r5["verdict"] == "MISSING",
           f"W5: missing file should be MISSING, got {r5}")

        # W6: reported-vs-computed verdict mismatch
        f6 = os.path.join(d, "w6.yaml")
        with open(f6, "w") as f:
            f.write(
                "total_independent_targets: 6\n"
                "total_fitted_parameters: 40\n"
                "verdict: IDENTIFIABLE\n"  # claims IDENTIFIABLE but ratio=6.67
                "resolution:\n"
                "  decision: ignored\n"
            )
        r6 = validate_identifiability_a_priori(f6)
        ok(r6["verdict"] == "MALFORMED"
           and r6["computed_verdict"] == "OVER_SATURATED",
           f"W6: reported IDENTIFIABLE vs computed OVER_SATURATED should "
           f"be MALFORMED, got {r6}")

        # W7b: negative total_fitted_parameters → MALFORMED.
        # Without this guard, -5/6 = -0.83 silently computes
        # IDENTIFIABLE.
        f7b = os.path.join(d, "w7b.yaml")
        with open(f7b, "w") as f:
            f.write(
                "total_independent_targets: 6\n"
                "total_fitted_parameters: -5\n"
                "verdict: IDENTIFIABLE\n"
            )
        r7b = validate_identifiability_a_priori(f7b)
        ok(r7b["verdict"] == "MALFORMED"
           and any("non-negative" in e for e in r7b["errors"]),
           f"W7b: negative fitted params should be MALFORMED, got {r7b}")

        # W7: accept_decorative without details → MALFORMED
        f7 = os.path.join(d, "w7.yaml")
        with open(f7, "w") as f:
            f.write(
                "total_independent_targets: 6\n"
                "total_fitted_parameters: 40\n"
                "verdict: OVER_SATURATED\n"
                "resolution:\n"
                "  decision: accept_decorative\n"
            )
        r7 = validate_identifiability_a_priori(f7)
        ok(r7["verdict"] == "MALFORMED"
           and any("decorative" in e.lower() for e in r7["errors"]),
           f"W7: accept_decorative without details should be MALFORMED, "
           f"got {r7}")

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
                             "identifiability_a_priori.yaml")
    result = validate_identifiability_a_priori(yaml_path)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"verdict: {result['verdict']}", file=sys.stderr)
        if result.get("summary"):
            s = result["summary"]
            print(f"ratio: {s.get('ratio', 0):.2f} "
                  f"(fitted={s.get('n_fitted', 0)}, "
                  f"targets={s.get('n_targets', 0)})",
                  file=sys.stderr)
        if result.get("errors"):
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)

    # Exit 0 for IDENTIFIABLE (proceed) and MARGINAL (proceed with
    # caveats — documented as advisory). Exit 1 for OVER_SATURATED,
    # MALFORMED, MISSING (callers must redesign or supply the
    # artifact). Mirrors the documented severity contract: MARGINAL
    # is MEDIUM advisory, not blocking.
    return 0 if result["verdict"] in {"IDENTIFIABLE", "MARGINAL"} else 1


if __name__ == "__main__":
    sys.exit(main())
