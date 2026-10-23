---
name: pre-model-identifiability-arithmetic
description: >-
  Required PRE-MODEL artifact `models/identifiability_a_priori.yaml`: before
  committing to an architecture, count free fitted parameters vs independent
  calibration targets (ratio < 1 IDENTIFIABLE, 1-3 MARGINAL, > 3
  OVER_SATURATED). A low ratio is necessary, not sufficient; post-hoc
  identifiability still applies. Trigger phrases include "parameter count",
  "calibration targets", "over-saturated", "ridge-trapped", "decorative ABM",
  "model is more complex than data supports".
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
- MEDIUM `pre_model_over_saturated_with_commitment` if verdict OVER_SATURATED but a `resolution.decision` is documented (the modeler has committed to a redesign — advisory until the YAML is updated to show the new fitted count and verdict IDENTIFIABLE)
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
  - source: national TB prevalence survey, regional estimates
    n_independent: 6                # 6 regions × 1 prevalence estimate each
    derivation: "6 regions × 1 prevalence estimate each"
total_independent_targets: 6        # sum of n_independent across sources

proposed_parameters:                # documentation only — the validator
                                    # reads only the totals below, not these
                                    # entries. Use this list to show your
                                    # work; if its sums disagree with the
                                    # totals, fix the totals (the validator
                                    # trusts the scalar fields).
  - name: beta_per_district_cluster
    count: 20                       # 20 clusters × 1 transmission rate each
    fitted: true
    prior_constraint: weak          # weak | strong | none | literature
  - name: case_detection_rate_per_cluster
    count: 20
    fitted: true
    prior_constraint: none
  - name: treatment_success_rate
    count: 1
    fitted: false                   # fixed from program data; not counted
    prior_constraint: literature

total_fitted_parameters: 40         # ENFORCED — validator reads this scalar
total_fixed_parameters: 5           # documentation only

ratio: 6.67                         # total_fitted / total_independent_targets
verdict: OVER_SATURATED             # IDENTIFIABLE | MARGINAL | OVER_SATURATED

architecture_implication: |
  Under HYBRID architecture (intervention effects from literature,
  baseline prevalence from calibration), the calibration round-trips
  6 survey estimates through 40 unidentifiable knobs. The ABM is
  decorative.

resolution:                         # required when verdict != IDENTIFIABLE
  decision: tie_params_by_region
  details: |
    Tie beta to the 5 regions that have survey estimates; fix case
    detection from notification/prevalence ratios. New count: 5 fitted
    / 6 targets = 0.83. Verdict IDENTIFIABLE.
```

Verdict thresholds:
- **IDENTIFIABLE**: ratio < 1.0 — fewer free params than targets, calibration is well-determined
- **MARGINAL**: 1.0 ≤ ratio ≤ 3.0 — at risk for ridge-trapping; post-hoc identifiability will confirm or refute
- **OVER_SATURATED**: ratio > 3.0 — provably unidentifiable; architecture must be redesigned

## Counting rules

**Independent calibration targets** = independent measurements only.

- A zone- or region-level survey estimate counts as **1 target per zone** (e.g., 6 targets for 6 survey zones)
- District-level synthetic disaggregations of zone means (or modeled rasters fitted to the same survey) are NOT independent — they're derived and don't add information
- A national-level incidence anchor (e.g., a WHO or GBD estimate) counts as 1 target if it's used as an independent constraint
- Multiple years of the same survey can count as N targets if the model uses time-varying calibration

**Free fitted parameters** = parameters the optimizer adjusts to match data.

- Parameters fixed from literature (`fitted: false`) do NOT count
- Parameters with strong informative priors count as ~0.5 (Bayesian regularization adds virtual data)
- Parameters tied across groups count as 1 per group, not 1 per member unit
- Tunable architectural choices (e.g., compartment count) count if the modeler actively varies them

## Resolution decisions when verdict ≠ IDENTIFIABLE

### (a) reduce_params

Tie parameters across groups. Best for retaining ABM mechanism diversity.

```yaml
resolution:
  decision: tie_params_by_region
  details: |
    20 cluster-specific transmission rates → 5 region-specific values.
    Each region's clusters share one rate. Ratio: 5/6 = 0.83 → IDENTIFIABLE.
```

### (b) add_calibration_targets

Add independent data. Hardest path because it requires actual new data.

```yaml
resolution:
  decision: add_calibration_targets
  details: |
    Add province-level estimates from an earlier, independent survey
    round (37 provinces). Add a national WHO incidence anchor (1 target).
    New target count: 6 + 37 + 1 = 44. Ratio: 40/44 = 0.91 → IDENTIFIABLE.
```

### (c) downgrade_to_analytical

Drop the ABM entirely. Use the analytical model:
`outcome_post = outcome_baseline × RR_intervention × programmatic_factor`.

```yaml
resolution:
  decision: downgrade_to_analytical
  details: |
    Drop the ABM. Use the survey estimates directly with literature
    effect multipliers from the registry. No calibration step needed;
    no fitted parameters. Predictions are equivalent to what the ABM
    would have produced anyway, and more transparent.
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
    transmission dynamics matter; nonlinear saturation between
    transmission intensity and prevalence that literature multipliers
    don't capture. If you can't
    articulate one of these, choose (a)/(b)/(c) instead.]
```

The validator REQUIRES details text when decision is `accept_decorative`. Empty details fires HIGH `pre_model_decorative_undocumented`.

## Worked example: 224202 retro (national malaria allocation)

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
  - name: beta_per_district_cluster
    count: 20
    fitted: true
    prior_constraint: strong       # narrow prior (e.g., from an external calibration)

# When prior_constraint == 'strong', count this as 0.5 fitted
# parameter (one virtual data point cancels half the free parameter).
# 20 strong-prior fitted = 10 effective fitted. New ratio: 10/6 = 1.67 → MARGINAL.
```

This is acceptable when the prior is genuinely informative (e.g., from a published calibration on an independent dataset for the same setting). Document the prior source in `prior_constraint_source` field.

## The arithmetic is necessary, not sufficient

A ratio below 1 does NOT prove the parameters are identifiable. Two
parameters can be jointly unidentifiable (only their product or
ratio is constrained) even when targets outnumber parameters, and
targets that are near-copies of each other add less information than
their count suggests. The 235631 run declared 17 fitted parameters
against 37 targets (IDENTIFIABLE); the model it actually built fitted
22 parameters to 22 targets, and post-hoc analysis found every one of
them unidentified. Two rules follow:

- The totals in this artifact must describe the model you actually
  build. If the architecture changes, update the artifact; the
  post-hoc identifiability and model-comparison artifacts will be
  compared against it.
- Post-hoc identifiability (`identifiability-analysis`) is still
  required and is the binding check.

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
