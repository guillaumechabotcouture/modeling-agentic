---
name: pre-model-identifiability-arithmetic
description: Phase 15 α — required PRE-MODEL artifact `models/identifiability_a_priori.yaml`. Before committing to a model architecture, count free fitted parameters vs independent calibration targets. Verdict IDENTIFIABLE (ratio < 1), MARGINAL (1-3), or OVER_SATURATED (> 3). The 224202 run shipped a HYBRID ABM with 40 fitted params / 6 zone targets (6.67× ratio); post-hoc identifiability flagged 3 unidentified params and the modeler scope-declared. This skill prevents that failure mode by requiring 30 seconds of arithmetic at strategy time. Trigger phrases include "identifiability", "parameter count", "calibration targets", "over-saturated", "ridge-trapped", "decorative ABM", "saturated fit", "model is more complex than data supports".
type: rigor-remediation
---

# Pre-Model Identifiability Arithmetic

## What this skill is for

Before committing to a model architecture, count the free parameters
you propose to fit and the independent calibration targets available
to you. If params >> targets, the model is structurally
unidentifiable — proven by 30 seconds of arithmetic, before any
code is written. This is the strategic check that prevents the
post-hoc "all parameters unidentified" discovery (which Phase 9
σ catches but only after weeks of compute).

Phase 15 α adds a required pre-model artifact:
`models/identifiability_a_priori.yaml`. The validator
(`scripts/identifiability_a_priori.py`) computes the verdict
(IDENTIFIABLE, MARGINAL, OVER_SATURATED) from the ratio.

## When the gate fires

`_check_identifiability_a_priori` in
`scripts/validate_critique_yaml.py` triggers at **round ≥ 2**.
Drafting window is round 1. The artifact is required by round 2,
escalating to HIGH at round 3+.

The check fires:
- MEDIUM `identifiability_a_priori_missing` at r=2 (drafting deadline)
- HIGH `identifiability_a_priori_missing` at r≥3
- HIGH `identifiability_a_priori_invalid` if YAML malformed
- HIGH `pre_model_over_saturated` if verdict OVER_SATURATED without resolution
- MEDIUM `pre_model_marginal_identifiability` if verdict MARGINAL
- HIGH `pre_model_decorative_undocumented` if `accept_decorative` lacks details

## The non-scope-declarable rule

**You CANNOT scope-declare an OVER_SATURATED verdict.**

Scope-declaration is for issues outside pipeline reach (proprietary
data, infeasible computation, MAP raster availability). Architecture
choice is inside pipeline reach. If your model has more knobs than
data, you must reduce knobs, increase data, or pick a simpler
architecture.

This is the inversion of Phases 12-14's scope-declare-anything
policy. Phase 15 establishes that some failures are arithmetic
facts, not negotiable.

## The artifact schema

```yaml
stage: pre_model
round_drafted: 1

calibration_targets:
  - source: NMIS 2021 zone PfPR
    n_independent: 6                # 6 zones × 1 PfPR measurement each
    derivation: "6 zones × 1 PfPR measurement each"
total_independent_targets: 6        # sum of n_independent across sources

proposed_parameters:
  - name: ext_foi_per_archetype
    count: 20                       # 20 archetypes × 1 ext_foi each
    fitted: true                    # tells the validator to count
    prior_constraint: weak          # weak | strong | none | literature
  - name: dur_immune_per_archetype
    count: 20
    fitted: true
    prior_constraint: none
  - name: standard_llin_or
    count: 1
    fitted: false                   # fixed from Yang 2018; not counted
    prior_constraint: literature

total_fitted_parameters: 40         # sum of fitted=true counts
total_fixed_parameters: 5

ratio: 6.67                         # total_fitted / total_independent_targets
verdict: OVER_SATURATED             # IDENTIFIABLE | MARGINAL | OVER_SATURATED

architecture_implication: |
  Under HYBRID architecture (intervention effects from literature,
  baseline PfPR from calibration), the calibration round-trips NMIS
  PfPR through 40 unidentifiable knobs. ABM is decorative.

resolution:                         # required when verdict != IDENTIFIABLE
  decision: tie_params_by_ecotype
  details: |
    Reduce ext_foi from 20 archetype-specific to 5 ecotype-specific
    values. Drop dur_immune as free parameter. New count: 5 fitted
    / 6 targets = 0.83. Verdict IDENTIFIABLE.
```

Verdict thresholds:
- **IDENTIFIABLE**: ratio < 1.0 — fewer free params than targets, calibration is well-determined
- **MARGINAL**: 1.0 ≤ ratio ≤ 3.0 — at risk for ridge-trapping; post-hoc identifiability will confirm or refute
- **OVER_SATURATED**: ratio > 3.0 — provably unidentifiable; architecture must be redesigned

## Counting rules

**Independent calibration targets** = independent measurements only.

- Zone-mean PfPR from NMIS counts as **1 target per zone** (6 for Nigeria's 6 zones)
- LGA-level synthetic disaggregations from zone means are NOT independent (they're derived; they don't add information)
- A national-level WHO incidence anchor counts as 1 target if it's used as an independent constraint
- Multiple years of the same survey can count as N targets if the model uses time-varying calibration

**Free fitted parameters** = parameters the optimizer adjusts to match data.

- Parameters fixed from literature (`fitted: false`) do NOT count
- Parameters with strong informative priors count as ~0.5 (Bayesian regularization adds virtual data)
- Parameters tied across groups count as 1 per group, not 1 per archetype
- Tunable architectural choices (e.g., compartment count) count if the modeler actively varies them

## Resolution decisions when verdict ≠ IDENTIFIABLE

### (a) reduce_params

Tie parameters across groups. Best for retaining ABM mechanism diversity.

```yaml
resolution:
  decision: tie_params_by_ecotype
  details: |
    20 archetype-specific ext_foi → 5 ecotype-specific (NW/NE/NC/S/coastal).
    Each ecotype's archetypes share one ext_foi. Ratio: 5/6 = 0.83 → IDENTIFIABLE.
```

### (b) add_calibration_targets

Add independent data. Hardest path because it requires actual new data.

```yaml
resolution:
  decision: add_calibration_targets
  details: |
    Add state-level PfPR from MIS 2018 (37 states, independent of NMIS 2021).
    Add WHO WMR 2024 incidence anchor (1 target).
    New target count: 6 + 37 + 1 = 44. Ratio: 40/44 = 0.91 → IDENTIFIABLE.
```

### (c) downgrade_to_analytical

Drop the ABM entirely. Use the analytical model:
`PfPR_post = PfPR_baseline × OR_intervention × programmatic_factor`.

```yaml
resolution:
  decision: downgrade_to_analytical
  details: |
    Drop the Starsim ABM. Use NMIS PfPR directly with literature
    multipliers (Yang OR=0.44, Protopopoff OR=0.40). No calibration
    step needed; no fitted parameters. Predictions are equivalent
    to what the ABM would have produced anyway, more transparent.
```

### (d) accept_decorative

Acknowledge the ABM is decorative but justify keeping it.

```yaml
resolution:
  decision: accept_decorative
  details: |
    [100-300 word justification required. Acceptable justifications:
    age-structured immunity dynamics that the analytical model
    cannot represent; projection beyond calibration window where
    transmission dynamics matter; nonlinear PfPR-EIR saturation
    that literature OR multipliers don't capture. If you can't
    articulate one of these, choose (a)/(b)/(c) instead.]
```

The validator REQUIRES details text when decision is `accept_decorative`. Empty details fires HIGH `pre_model_decorative_undocumented`.

## Worked example: 224202 retro

The Phase 14 RESUME run (224202) shipped:

- Calibration targets: 6 zone-level PfPR (NMIS 2021)
- Fitted parameters: ext_foi (20) + dur_immune (20) = 40
- Ratio: 40 / 6 = **6.67× over-saturated**
- Verdict (would have been): **OVER_SATURATED**
- Post-hoc identifiability.yaml confirmed: 3/3 fitted params on flat ridge (`profile_flat_ratio: 0`)

If Phase 15 had been in place at strategy time:

```yaml
# What the modeler would have written at round 1
total_independent_targets: 6
total_fitted_parameters: 40
ratio: 6.67
verdict: OVER_SATURATED
architecture_implication: |
  The HYBRID architecture means intervention effects come from
  literature multipliers (Yang, Protopopoff). The ABM's only
  output is baseline PfPR — identical to NMIS PfPR after the
  calibration round-trip. The ABM is decorative.
resolution:
  decision: tie_params_by_ecotype
  details: |
    20 ext_foi → 5 ecotype values. Drop dur_immune (set to 8 weeks
    per Griffin 2010). Total fitted: 5. Ratio 5/6 = 0.83 → IDENTIFIABLE.
```

The pipeline would have either accepted this resolution (and the
post-hoc identifiability would have confirmed `well_identified: 5,
unidentified: 0`), or — if the modeler refused to redesign — blocked
ACCEPT at round 2. The decorative-ABM failure mode would have been
structurally impossible.

## Bayesian alternative (advanced)

Strong priors function as virtual data. If a parameter has a prior
SD that's 10% of its plausible range, that's roughly equivalent to
adding 1 calibration target for that parameter.

```yaml
proposed_parameters:
  - name: ext_foi_per_archetype
    count: 20
    fitted: true
    prior_constraint: strong       # narrow prior (e.g., from EMOD)

# When prior_constraint == 'strong', count this as 0.5 fitted
# parameter (one virtual data point cancels half the free parameter).
# 20 strong-prior fitted = 10 effective fitted. New ratio: 10/6 = 1.67 → MARGINAL.
```

This is acceptable when the prior is genuinely informative (e.g., from EMOD calibration on an external Nigeria dataset). Document the prior source in `prior_constraint_source` field.

## Related skills

- `identifiability-analysis` (Phase 9 σ): POST-HOC backstop. If pre-model arithmetic catches the issue, this skill's check should always pass.
- `multi-structural-comparison` (Phase 2 D): catches `degenerate_fit` post-hoc. Phase 15 catches the obvious cases pre-build.
- `mechanistic-vs-hybrid-architecture` (Phase 7 ν): teaches initial architecture choice. Phase 15 teaches when to abandon HYBRID if calibration would be unidentifiable.
- `modeling-strategy`: now cross-references this skill in its "A-Priori Identifiability" section.

## Drafting timeline

The artifact is required at **round 2 onward**. Round 1 is the drafting window. The recommended path:

1. **r1 (planning):** While planner produces plan.md, ALSO produce
   identifiability_a_priori.yaml with the proposed model's parameter
   counts. This forces the planner to think about identifiability
   before settling on architecture.
2. **r2 (model build):** Modeler reads the artifact. If
   verdict=IDENTIFIABLE, builds the model with confidence. If
   MARGINAL, builds but flags ridge-trapping risk in modeling_strategy.md.
   If OVER_SATURATED, refuses to build until resolution is documented.
3. **r3+ (post-hoc backstop):** STAGE 5b RIGOR runs
   `scripts/identifiability.py` (Phase 9 σ). If the pre-model
   verdict was IDENTIFIABLE, the post-hoc check should report
   `well_identified: K, unidentified: 0`. If they disagree, the
   pre-model arithmetic was wrong (the modeler under-counted free
   parameters, e.g., missed implicit free knobs in the architecture).
