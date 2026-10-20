---
name: identifiability-analysis
description: >-
  Contract for post-calibration identifiability analysis. The modeler exposes
  a loss function and `models/identifiability.yaml` (point estimates, bounds,
  loss_type). identifiability.py computes the full Fisher information matrix
  and re-optimized profile likelihoods, classifying each parameter as
  well_identified / weakly_identified / unidentified, which catches
  ridge-trapped and jointly non-identifiable parameters. Trigger phrases
  include "identifiability", "Fisher information", "profile likelihood",
  "ridge trapped", "unidentified parameter".
---

# Parameter Identifiability Analysis

## When to invoke this skill (vs. pre-model-identifiability-arithmetic)

This skill covers POST-HOC identifiability (Fisher info + profile
likelihood after fitting). It is the BACKSTOP for cases where
strategy-time arithmetic missed an over-saturation. The PRIMARY
identifiability check is `pre-model-identifiability-arithmetic`,
invoked at STAGE 3 before any code is written. If that skill's gate
passes, this skill's post-hoc check confirms; if the pre-model
check fails, this skill should never be reached (the architecture
is redesigned at strategy time).

The two are complementary:
- **Pre-model (Phase 15 α):** counts free fitted parameters vs
  independent calibration targets. Catches obvious over-saturation
  in 30 seconds of arithmetic. Verdict: IDENTIFIABLE / MARGINAL /
  OVER_SATURATED.
- **Post-hoc (this skill, Phase 9 σ):** runs Fisher information +
  profile likelihood after fitting. Catches subtle ridge-trapping
  the pre-model arithmetic might miss (e.g., approximate
  identifiability collapse under correlated parameters, parameters
  that a-priori looked free but turn out to interact with others
  in a way that flattens the loss surface).

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

loss_type: sse
  # REQUIRED. nll | deviance | chi2 | sse | mse. The 1.92 profile
  # threshold only means "95% CI" on a negative-log-likelihood scale;
  # sse/mse are converted to the Gaussian profile NLL (n_obs/2)·log(SSE).
  # Missing or unknown → verdict UNKNOWN_LOSS_SCALE, which fails the gate.
n_obs: 22                             # required when loss_type is sse or mse
profile: full                         # full (default; re-optimizes the other
                                      # parameters) | conditional (cheap slice,
                                      # weaker evidence)

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

1. **Fisher SE** — from the FULL finite-difference Hessian H of the
   (NLL-scaled) loss at the point estimate:

   ```
   SE_i = sqrt( (H⁻¹)_ii )
   ```

   NOT 1/sqrt(H_ii). The diagonal-only form is the conditional SE with
   every other parameter held fixed, and it cannot see correlated
   ridges: a loss that depends only on β/γ gives small diagonal SEs
   for both even though neither is identified.

2. **Ridge (eigen) check** — eigen-decomposition of the Hessian in
   relative coordinates. Any parameter loading on a near-null direction
   is `unidentified`, and the output names the unconstrained
   combination (e.g., "β/γ is constrained, β and γ separately are not").

3. **Profile-likelihood scan** — varies this parameter over
   [lower_bound, upper_bound] on a grid (default 20 points) and
   RE-OPTIMIZES the other parameters at each grid point. Records the
   profile loss at each point. (`profile: conditional` holds them fixed
   instead; that is a slice, not a profile, and is recorded as weaker
   evidence.)

4. **95% profile CI** — the range where profile loss stays within 1.92
   units of the minimum (equivalent to 95% CI for a chi-squared /
   normal likelihood).

5. **Flat-ratio** — (max_loss - min_loss) across the scan, divided by
   the CI threshold (1.92). A parameter whose full-range scan spans
   less than 1 CI threshold is effectively unconstrained.

## Classification

| Status               | Criterion                                                 |
|----------------------|-----------------------------------------------------------|
| `well_identified`    | Fisher rel SE < 10%                                       |
| `weakly_identified`  | 10% ≤ rel SE < 50%                                        |
| `unidentified`       | rel SE ≥ 50% OR flat_ratio < 1.0 (ridge-trapped) OR loads on a near-null Hessian direction |

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
   zone-level prevalence alone, age-stratified data or incidence time series would
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
