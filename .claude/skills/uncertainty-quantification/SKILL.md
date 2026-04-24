---
name: uncertainty-quantification
description: Contract for STAGE 5b UNCERTAINTY. Modeler exposes an outcome_fn
  in models/outcome_fn.py that takes sampled parameter values (from the
  effect-size priors registry) and returns decision outputs as a dict. The
  UQ script samples N draws from each registered parameter's prior, runs
  outcome_fn once per draw, and aggregates per-output 95% credible intervals
  into uncertainty_report.yaml. Scalar outputs (DALYs, costs, burden) get CIs;
  categorical outputs (per-archetype package choice, per-LGA assignment) get
  stability distributions. The writer must report these posterior-derived CIs
  as the primary uncertainty claim — NOT an ensemble ±X% perturbation. The
  gate blocks ACCEPT without uncertainty_report.yaml. Use when writing
  outcome_fn, interpreting posterior CIs, or deciding whether to use cloud
  compute (see cloud-compute skill). Trigger phrases include "uncertainty
  quantification", "UQ", "propagate priors", "outcome_fn", "posterior CI",
  "credible interval", "ensemble uncertainty".
---

# Uncertainty Quantification via Prior Propagation

## Why this stage exists

The malaria run's reported "±13% on DALYs" was 2.4–8× too narrow. It came
from perturbing two calibrated parameters by ±20% across 3 seeds — an
ensemble of computational replicates, not a posterior from literature
priors. A trained modeler propagates uncertainty from the source-paper
CIs that define intervention effect sizes, cost estimates, calibration
targets. That's what the UQ probe demonstrated and what this stage
implements.

## The modeler's contract: outcome_fn

Every run must expose a file at `{run_dir}/models/outcome_fn.py`
containing a deterministic callable:

```python
def outcome_fn(params: dict) -> dict:
    """
    Run the decision-relevant portion of the model under a specific
    parameter set, return the outputs we care about for uncertainty
    analysis.

    Args:
        params: {parameter_name: sampled_value}. Keys match the `name`
            field of entries in citations.md `## Parameter Registry`.

    Returns:
        dict of {output_name: scalar_or_str}:
          - scalars → aggregated as 95% credible intervals
          - strings → aggregated as stability distributions
            (e.g., which package was optimal per archetype in this draw)

    Must be deterministic given its inputs (seed random state using a
    per-draw seed derived from the hash of params if you need stochasticity).

    Must be cheap enough that 200 invocations complete in ~15 min locally
    OR the modeler must build a cloud-parallel path (see the
    cloud-compute skill).
    """
```

## When the full ABM is too slow: build a surrogate

If the full model takes >5 minutes per invocation, 200 draws will take
>16 hours locally. Build a surrogate:

1. Run the full ABM on a sparse grid (e.g., 30 parameter combinations
   spanning the prior supports).
2. Fit a smooth emulator (Gaussian process, neural net, response
   surface, or just multiple linear regression on log-parameters) to
   predict outputs from parameters.
3. Expose the emulator via `outcome_fn`. The UQ stage runs the
   emulator, not the full ABM.
4. Document the emulator's RMSE against the grid points in
   `{run_dir}/models/outcome_fn_calibration.md`. A surrogate with RMSE
   >10% of the mean output is suspect — either the grid is too sparse
   or the emulator is under-parameterized.

## Example outcome_fn (malaria-style allocation)

```python
# models/outcome_fn.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from level3_interventions import (compute_archetype_ce,
                                   greedy_budget_allocate)
from level2_archetype_starsim import load_archetype_data

# Pre-load invariant data once at import.
_DATA = load_archetype_data()


def outcome_fn(params: dict) -> dict:
    """
    params is a dict sampled from the parameter registry. We override
    the default intervention effect sizes with sampled values, rerun
    the cost-effectiveness + allocation step, and return decision outputs.

    This uses a precomputed ABM response surface (see
    models/outcome_fn_calibration.md) rather than re-running the full
    Starsim ABM 200 times.
    """
    # Override the defaults with sampled values.
    effects = {
        'irs': params.get('irs_rr', 0.35),
        'smc': params.get('smc_rr', 0.27),
        'itn_pbo': params.get('itn_pbo_rr', 0.45),
        'itn_dual_ai': params.get('itn_dual_ai_rr', 0.45),
    }
    costs = {
        'irs_per_py': params.get('irs_cost_per_py', 3.20),
        'itn_pbo_per_net': params.get('itn_pbo_unit_cost', 10.0),
        'itn_dual_per_net': params.get('itn_dual_ai_unit_cost', 12.50),
    }

    archetype_ce = compute_archetype_ce(_DATA, effects, costs)
    allocation = greedy_budget_allocate(archetype_ce, budget=320e6)

    # Scalar: total DALYs averted, total budget spent
    # Categorical: per-archetype package choice (for stability analysis)
    out = {
        'dalys_averted_5yr': float(allocation['total_dalys_averted']),
        'budget_spent_usd': float(allocation['total_cost']),
        'n_lgas_allocated': int(allocation['n_lgas']),
    }
    for arch_id, pkg in allocation['per_archetype_package'].items():
        out[f'package_{arch_id}'] = pkg  # string → stability distribution
    return out
```

## UQ script contract

Invoke from lead or manually:

```bash
python3 scripts/propagate_uncertainty.py {run_dir} --n-draws 200
```

Writes `{run_dir}/uncertainty_report.yaml`. Schema:

```yaml
n_draws: 200
seed: 42
n_errors: 0           # number of draws where outcome_fn raised

scalar_outputs:
  dalys_averted_5yr:
    mean: 7.41e6
    median: 7.28e6
    ci_low: 5.45e6    # 2.5th percentile
    ci_high: 9.66e6   # 97.5th percentile
    n: 200
  budget_spent_usd:
    mean: 318.2e6
    ci_low: 311.5e6
    ci_high: 320.0e6
    n: 200

categorical_outputs:
  package_A1:
    counts: {itn_pbo_80: 193, itn_dual_80: 7}
    dominant: itn_pbo_80
    dominance: 0.965   # fraction of draws choosing the dominant
    n: 200
  package_A2:
    counts: {itn_pbo_80: 154, smc_80: 46}
    dominant: itn_pbo_80
    dominance: 0.770

parameter_samples:  # what got drawn
  irs_rr:
    mean: 0.35
    ci_low: 0.27
    ci_high: 0.44
  ...
```

## The writer's primary-CI rule (Commit A of Phase 1.5 + this)

When reporting uncertainty on a quantitative claim:

- **Primary**: the CI from `uncertainty_report.yaml`. This is the CI
  that propagates source-paper uncertainty through the decision.
- **Secondary (optional)**: ensemble-perturbation CIs from the ABM
  itself, clearly labeled as "computational replicate uncertainty
  only."

The writer MUST NOT report ensemble CIs as the primary uncertainty when
`uncertainty_report.yaml` exists. Decision-makers need to see source-CI
width; computational replicate width misleads them into over-confidence.

## Categorical stability: what it means for policy

`categorical_outputs` gives you per-category stability. Interpretation:

- **dominance > 0.95**: robust decision. Report as a confident
  recommendation ("PBO-ITN dominates in this archetype in 96% of posterior
  draws").
- **0.70 < dominance < 0.95**: moderately robust. Report as "likely
  optimal but contingent on parameter values in the published-CI range."
- **dominance < 0.70**: policy choice is uncertain. The report must
  flag this as an ICER coin-flip and either recommend additional data
  collection OR present both options as defensible.

Ignoring stability and reporting the point-estimate choice as
"optimal" is a HIGH misrepresentation under the writer prompt rule.

## When to use cloud compute

See the `cloud-compute` skill for the decision rule.  Quick version:

- **outcome_fn < 2s/call**: run locally (200 draws × 2s = 7 min).
- **outcome_fn 2–60s/call**: run locally if you can wait 30 min–3 hr,
  otherwise use cloud.
- **outcome_fn > 60s/call**: MUST use cloud, OR build a surrogate.
- **>1000 draws needed** (e.g., for per-LGA stability in 774 units,
  not per-archetype): use cloud.

## Gate behavior

The STAGE 7 validator blocks ACCEPT if:
- `uncertainty_report.yaml` is missing AND at least one parameter has CIs
  in the registry (i.e., UQ is applicable).
- `uncertainty_report.yaml` has `n_errors > n_draws / 4` (outcome_fn
  raised on >25% of draws — the surrogate is broken or misspecified).

MEDIUM blockers fire if:
- `n_draws < 100` (insufficient for stable 2.5–97.5 percentiles).
- Any scalar output's CI width > 3× its mean (suggests the priors
  dominate the data — modeler may need to tighten priors to
  data-consistent posteriors via MCMC, a Phase 3 job).
- Any categorical output has `dominance < 0.50` (coin-flip; writer must
  present as a genuine toss-up).

## Round-2+ behavior

On re-runs, invoke with same `--seed` for reproducibility. Changes in
`uncertainty_report.yaml` between rounds reflect either (a) modeler
updates to outcome_fn or (b) registry parameter changes. Both are
audit-visible in git history.

## Minimal policy on correlated priors

The current implementation samples each registered parameter
independently. In reality some parameters are correlated (e.g., IRS and
ITN efficacy share pyrethroid susceptibility mechanics). If you know a
correlation exists and matters:

1. Document it in `citations.md` `## Parameter Registry` as a `correlations:`
   block (schema TBD — for now just narrative notes in the `notes:` field).
2. Implement the correlation inside `outcome_fn` by transforming
   independently-sampled params into correlated ones at the top.

A future enhancement to `propagate_uncertainty.py` will support declared
correlations at the registry level. Until then, correlated priors are
the modeler's responsibility inside outcome_fn.
