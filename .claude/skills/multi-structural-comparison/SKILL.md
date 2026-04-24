---
name: multi-structural-comparison
description: Contract for multi-structural model comparison. Modeler must
  produce ≥3 candidate model structures (null, simple, full) and a manifest
  at models/model_comparison.yaml listing each model's k_params, predictions,
  and (where possible) leave-one-out predictions. The compare_models.py tool
  computes RMSE, LOO-CV RMSE, AIC, BIC; flags AIC-vs-LOO divergence; and
  specifically flags DEGENERATE FIT — the "AIC lies" pattern where saturated
  k ≥ n/2 gives near-zero training RMSE but LOO-RMSE equals the null model's.
  Use when writing the manifest, interpreting model_comparison_formal.yaml,
  or justifying a choice between candidate models. Trigger phrases include
  "multi-structural", "model comparison", "AIC", "BIC", "LOO-CV", "saturated
  fit", "degenerate fit", "candidate structures".
---

# Multi-Structural Model Comparison

## Why this stage exists

The malaria run reported a 1.1 pp MAE on zone-level PfPR calibration —
seemingly excellent fit. The structural probe revealed this is a saturated
fit: effectively 22 free EIRs fitted to 22 zone targets. LOO-CV showed a
4-coefficient logistic regression generalizes better. The recovery rate γ
lives on a flat ridge (any value in [0.1, 100] yr⁻¹ gives identical
residuals).

A trained modeler NEVER builds one model and calls it done. They build a
ladder of candidate structures, compare formally, and report which
complexity level is actually earned by the data.

## The modeler's contract

Every run must produce:

1. **At least 3 candidate structures**, saved under
   `{run_dir}/models/null/`, `{run_dir}/models/simple/`, `{run_dir}/models/full/`
   (or equivalent). Each directory contains the model code + fit
   artifacts for that structure.

2. **A manifest at `{run_dir}/models/model_comparison.yaml`** following
   this schema:

   ```yaml
   n_targets: 22
   targets: [0.23, 0.31, 0.14, ...]   # observed values, length == n_targets

   models:
     - name: null
       k_params: 1
       description: "Global mean predictor (one parameter: the mean)"
       predictions: [0.28, 0.28, 0.28, ...]     # training fit, length == n_targets
       loo_predictions: [0.27, 0.29, ...]        # leave-one-out; OPTIONAL but strongly recommended

     - name: simple
       k_params: 4
       description: "Pooled logistic regression on 4 covariates (rainfall, urbanization, kdr, zone dummy)"
       predictions: [...]
       loo_predictions: [...]

     - name: full
       k_params: 23
       description: "SEIR compartmental surrogate for the archetype ABM"
       predictions: [...]
       loo_predictions: [...]
   ```

3. Invoke `python3 scripts/compare_models.py {run_dir}` which writes
   `{run_dir}/model_comparison_formal.yaml` with the information-
   criterion results and the degenerate-fit flag.

## The three candidate structures

The tiering is deliberate and matches the statistical-modeling ladder:

### Null (Model 0): zero-DOF baseline

Purpose: the "no structure" reference. What's the RMSE if we predict
zero variation? Options:
- Global mean: `pred_i = mean(observed)`
- Per-category mean (e.g., per-zone): `pred_i = mean(observed in category of i)` — note this inflates effective k
- Climatology / seasonal mean if time series

`k_params` is the number of parameters estimated (1 for global mean,
J for J-category means).

### Simple (Model 1): parsimonious, pooled

Purpose: the "does the structure earn its cost?" comparison. This is
typically a pooled regression with a handful of covariates (3–6). For
spatial modeling: rainfall, urbanization, resistance score, population
density. NO per-unit intercepts (those are in the null baseline).

Why: if the null explains most of the variance and simple adds little,
the added complexity of the full model is unjustified.

### Full (Model 2): the mechanistic / preferred structure

The model the question asked for (the ABM, the compartmental, the
full-rank spatial regression, whatever). Report `k_params` as the
total number of estimated parameters. If the full model has 22 EIRs
and 1 recovery rate fitted on 22 targets, `k_params = 23` — this is
the saturated-fit trap and the tool will catch it.

### Why exactly 3?

Fewer than 3 doesn't show a trend (is simple→full a big jump or a small
one?). More than 3 is fine but rarely materially changes the conclusion.
3 is the minimum to plot "complexity vs fit" and see whether the
full model's advantage is earned.

## The degenerate-fit detector

This is the main value the tool adds beyond AIC/BIC. The pattern:

- `k_params >= n_targets / 2` (high parameterization relative to data)
- `train RMSE << LOO-CV RMSE` (the model interpolates training but
  fails to generalize)

When both conditions hold, AIC and BIC will prefer the saturated
model (the log-likelihood explodes as RMSE → 0) — but LOO reveals
the model's predictions for held-out data are no better than the null.
This is the "AIC lies" result from the malaria run's SEIR surrogate.

If the tool flags DEGENERATE_FIT_DETECTED, the modeler has three
options:

1. **Partial pooling**: introduce priors or multilevel structure that
   shrinks per-unit estimates toward a global mean. This effectively
   reduces k_params.
2. **Remove excess DOF**: tie redundant parameters (e.g., if you have
   22 EIRs and all cluster into 4 archetypes, fit 4 EIRs not 22).
3. **Honest scope declaration**: acknowledge the model's predictions
   for held-out units are no better than the null. This is real
   science — the data simply doesn't identify the structure — and
   should be reported.

## Gate behavior

The STAGE 7 validator will block ACCEPT if:
- `{run_dir}/model_comparison_formal.yaml` does not exist (modeler
  skipped this stage).
- `verdict == INSUFFICIENT_STRUCTURES` (fewer than 3 candidates).
- `verdict == DEGENERATE_FIT_DETECTED` AND the flagged model is the
  "preferred_by_aic" AND the issue has not been explicitly addressed
  (via one of the three options above) in `modeling_strategy.md`.

The gate emits blockers with reviewer="multi-structural-comparison"
and prefix MSC-.

## Interpreting the output

`model_comparison_formal.yaml` example (from a clean run):

```yaml
n_targets: 22
models:
  null:   {k: 1,  rmse_train: 0.146, rmse_loo: 0.153, aic: -20.3, bic: -19.2}
  simple: {k: 4,  rmse_train: 0.087, rmse_loo: 0.100, aic: -37.0, bic: -32.7}
  full:   {k: 10, rmse_train: 0.054, rmse_loo: 0.079, aic: -51.2, bic: -40.5}
preferred_by_aic: full
preferred_by_loo: full
aic_vs_loo_divergence: false
degenerate_fit: {flagged: false}
verdict: CLEAN
```

Interpretation: the full model (k=10) generalizes 40% better than simple
(RMSE_loo 0.079 vs 0.100) and 48% better than null (0.153 vs 0.079). AIC
and LOO agree. The added complexity is earned.

A degenerate-fit output:

```yaml
models:
  null:   {k: 1,  rmse_train: 0.146, rmse_loo: 0.153, aic: -20.3}
  simple: {k: 4,  rmse_train: 0.087, rmse_loo: 0.100, aic: -37.0}
  full:   {k: 23, rmse_train: 1e-17, rmse_loo: 0.153, aic: -499.4}
preferred_by_aic: full     # AIC loves the saturated fit
preferred_by_loo: simple   # LOO reveals full doesn't generalize
aic_vs_loo_divergence: true
degenerate_fit:
  flagged: true
  model: full
  reason: "k_params (23) >= n_targets (22) / 2; train RMSE is 1e-17 while
           LOO RMSE is 0.153 — AIC will prefer this model but LOO reveals
           it doesn't generalize. Classic 'AIC lies' pattern."
verdict: DEGENERATE_FIT_DETECTED
```

Interpretation: the full model's reported "great fit" is interpolation,
not generalization. For policy purposes, the simple model (LOO 0.100)
is a better predictor of held-out units. The allocation should be
driven by the simple model's predictions, OR the full model should be
rebuilt with partial pooling / tied parameters.

## The writer's job

The final report MUST include the comparison table. Suggested format:

| Model  | k | RMSE (train) | RMSE (LOO) | AIC | Preferred? |
|--------|---|--------------|------------|-----|------------|
| Null   | 1  | 0.146 | 0.153 | –20.3 |             |
| Simple | 4  | 0.087 | 0.100 | –37.0 | by LOO      |
| Full   | 23 | 1e-17 | 0.153 | –499.4 | by AIC (degenerate) |

With interpretation prose: "The full model achieves near-zero training
RMSE by fitting one parameter per data point. Its LOO-CV RMSE is
identical to the null model, revealing the improvement is interpolation
rather than generalization. For policy prediction we use the simple
model's predictions, not the full model's."

## For modelers: how to compute LOO cheaply

Full re-fit 22 times is often prohibitive. Common shortcuts:

- **Regression / GLM**: closed-form LOO via leverage/hat matrix
  (PRESS statistic). See statsmodels.
- **ODE / ABM models**: importance-sampled LOO (Pareto-smoothed IS-LOO;
  the arviz package implements this for MCMC fits).
- **For simple models with n ≤ 30**: just re-fit. Even 22 re-fits is
  usually <1 minute.

If none of these are feasible for the full model, report LOO-CV for the
null and simple models (cheap), and state in the manifest that LOO for
full requires cloud compute (see the cloud-compute skill).

## Round-2+ behavior

On re-runs, the modeler must re-supply the manifest. If the structural
comparison hasn't changed materially (same candidates, similar
parameters), the tool's output is stable — no penalty for re-running.
If the modeler dropped to <3 candidates or added a single saturated
variant on a round-2 patch, the gate catches it.
