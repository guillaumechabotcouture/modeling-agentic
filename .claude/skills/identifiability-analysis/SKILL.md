---
name: identifiability-analysis
description: Contract for parameter identifiability analysis. The modeler,
  after calibration, exposes a loss function over fitted parameters and a
  manifest at models/identifiability.yaml listing each parameter with its
  point estimate and plausible bounds. identifiability.py computes a
  Fisher-information diagonal (via finite differences) and runs a
  profile-likelihood scan for each parameter, classifying each as
  well_identified / weakly_identified / unidentified. Catches the
  "ridge-trapped" failure mode — parameters whose values don't actually
  affect the fit (e.g., γ in the malaria structural probe). Use when
  writing the manifest, interpreting identifiability.yaml, or when the
  gate flags ridge-trapped parameters that are policy-relevant. Trigger
  phrases include "identifiability", "Fisher information", "profile
  likelihood", "ridge trapped", "unidentified parameter", "parameter
  estimability".
---

# Parameter Identifiability Analysis

## Why this exists

The malaria structural probe found that the ABM's recovery rate γ lives
on a flat ridge: any γ ∈ [0.1, 100] yr⁻¹ gives identical residuals.
The calibration "fit γ to 0.2" is meaningless — the data doesn't
constrain γ at all. The 22 archetype-specific EIRs were trivially
identifiable because each was tied to one target, but their Fisher-SE
was artificially tight: they'd been estimated on a flat ridge in γ.

A trained modeler checks identifiability BEFORE trusting parameter
estimates for downstream policy. This stage makes that check mechanical.

## The modeler's contract

Every run with fitted parameters must produce a manifest at
`{run_dir}/models/identifiability.yaml`:

```yaml
loss_fn: "models/outcome_fn.py::loss"
  # An importable callable: loss(params: dict) -> float
  # Returns the calibration objective (negative log-likelihood,
  # sum-of-squared-residuals, etc.) evaluated at the given params.
  # Must be deterministic. Use the same file as outcome_fn when possible
  # to share pre-loaded state.

parameters:
  - name: beta                        # fitted parameter name
    point_estimate: 0.5               # from calibration
    lower_bound: 0.1                  # prior / plausibility lower
    upper_bound: 2.0                  # prior / plausibility upper
    profile_n_points: 20              # optional, default 20

  - name: gamma
    point_estimate: 0.2
    lower_bound: 0.01
    upper_bound: 2.0
  ...
```

`lower_bound` / `upper_bound` should reflect the plausible range
(prior support, physical constraints). Too-tight bounds mask ridge
trapping; too-loose bounds can trigger false unidentified flags. Use
the same bounds you'd use for the prior when sampling.

## What the tool computes

For each parameter:

1. **Fisher SE** — local curvature of the loss at the point estimate:

   ```
   d²loss/dp² ≈ (loss(p+h) - 2·loss(p) + loss(p-h)) / h²
   Fisher SE = 1 / sqrt(d²loss/dp²)
   ```

2. **Profile-likelihood scan** — holds other parameters at their point
   estimates; varies this parameter over [lower_bound, upper_bound] on
   a grid (default 20 points). Records the loss at each grid point.

3. **95% profile CI** — the range where profile loss stays within 1.92
   units of the minimum (equivalent to 95% CI for a chi-squared /
   normal likelihood).

4. **Flat-ratio** — (max_loss - min_loss) across the scan, divided by
   the CI threshold (1.92). A parameter whose full-range scan spans
   less than 1 CI threshold is effectively unconstrained.

## Classification

| Status               | Criterion                                                 |
|----------------------|-----------------------------------------------------------|
| `well_identified`    | Fisher rel SE < 10%                                       |
| `weakly_identified`  | 10% ≤ rel SE < 50%                                        |
| `unidentified`       | rel SE ≥ 50% OR flat_ratio < 1.0 (ridge-trapped)          |

The `flat_ratio < 1.0` check is what catches ridge trapping. If the
profile loss barely moves across the full plausible range, Fisher SE
at the optimum is meaningless (it's local curvature at a single point
on a flat ridge).

## Gate behavior

The STAGE 7 validator blocks ACCEPT when:
- `identifiability.yaml` is missing AND the model has >1 fitted
  parameter (unfitted models are exempt).
- Any parameter is `unidentified` AND is listed as policy-relevant in
  `modeling_strategy.md` (e.g., appears in intervention-effect
  computations or allocation logic).

MEDIUM blockers fire for:
- `weakly_identified` parameters used in policy outputs.
- Parameters with `profile_ci_high - profile_ci_low > 0.5 * (upper_bound - lower_bound)`
  (CI spans >50% of the plausible range — barely constrained).

Gate blockers use prefix `ID-` (e.g., `ID-001`).

## How to resolve unidentified parameters

Three options:

1. **Tie the parameter to an informative prior** (e.g., literature
   value with narrow CI). If γ comes from Griffin 2010's published
   estimate ± 10%, don't fit it — fix it at the literature value.

2. **Add data that constrains the parameter**. If γ is unidentified by
   zone-level PfPR, age-stratified data or incidence time series would
   identify it. Adding data is often the right move but expensive.

3. **Remove the parameter** (fix at a default value). If γ is
   unidentified AND doesn't affect policy outputs (via sensitivity
   analysis), it can simply be fixed at a plausible value and dropped
   from the fitted set.

DO NOT pretend unidentified parameters are identified. The profile
scan will be in the record regardless.

## For the writer

The report must include an "Identifiability" section (can be in
Methods or Appendix) listing each fitted parameter with its status and
Fisher SE. For any unidentified parameters that couldn't be resolved,
the report must state which policy outputs they affect and what
additional data would be needed to identify them.

## Round-2+ behavior

Re-runs must include a fresh `identifiability.yaml` — the manifest
reflects the current calibration, so if the modeler changed the
parameter set or re-calibrated, the analysis must re-run.

If the modeler addresses a round-1 unidentified flag by tying a
parameter to a prior (option 1), the round-2 manifest should no longer
list that parameter as fitted.
