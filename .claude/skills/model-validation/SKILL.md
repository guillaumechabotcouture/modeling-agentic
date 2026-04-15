# Model Validation Strategies

## When to use this skill
Use when evaluating model quality, comparing models, or setting up validation procedures.

## Validation Framework

### 1. Train/Test Splitting

**Time series data** (most common in modeling):
- Use **temporal splits**: train on earlier data, test on later data
- Never use random splits -- this causes data leakage
- Common split: train on all but the last season, test on the last season
- For multi-season data: use expanding window or rolling origin cross-validation

```python
# Temporal split for seasonal data
train = data[data['date'] < '2024-10-01']
test = data[data['date'] >= '2024-10-01']
```

**Cross-sectional data**:
- Use k-fold cross-validation (k=5 or k=10)
- For small datasets: leave-one-out cross-validation
- For grouped data: use GroupKFold to prevent leakage

**Time series cross-validation** (best practice):
```python
from sklearn.model_selection import TimeSeriesSplit
tscv = TimeSeriesSplit(n_splits=5)
```

### 2. Metrics by Problem Type

#### Regression / Continuous Prediction
| Metric | Formula | When to use | Notes |
|--------|---------|-------------|-------|
| **RMSE** | sqrt(mean((y - y_hat)^2)) | Default choice | Penalizes large errors more |
| **MAE** | mean(abs(y - y_hat)) | When outliers shouldn't dominate | More robust than RMSE |
| **MAPE** | mean(abs((y - y_hat)/y)) * 100 | When relative error matters | Undefined when y=0 |
| **R-squared** | 1 - SS_res/SS_tot | Cross-sectional data only | Misleading for time series |
| **Adjusted R-squared** | Penalized for # parameters | Model comparison | Better than raw R-squared |

#### Forecasting / Time Series
| Metric | When to use |
|--------|-------------|
| **Forecast skill score** | vs. naive baseline: 1 - RMSE_model/RMSE_naive |
| **Coverage** | % of observations within prediction interval (target: 95%) |
| **WIS (Weighted Interval Score)** | Standard for probabilistic forecasts (e.g., CDC FluSight) |
| **Log score** | For probabilistic forecasts |

#### Count Data
| Metric | When to use |
|--------|-------------|
| **Deviance** | GLM goodness of fit |
| **AIC / BIC** | Model comparison (lower is better) |
| **Dispersion** | Check for over/underdispersion |

### 3. Model Comparison Protocol

When comparing two or more models:

1. **Fit all models on the same training data**
2. **Evaluate all models on the same test data**
3. **Use the same metrics for all models**
4. **Report relative improvement** over the simplest baseline
5. **Statistical significance**: use paired tests (Diebold-Mariano for forecasts) to check if differences are significant
6. **Parsimony**: prefer simpler models unless the complex model is significantly better (use AIC/BIC)

### 4. Residual Diagnostics

After fitting any model, check residuals:

```
Residual Checklist:
[ ] Plot residuals vs. fitted values -- should show no pattern
[ ] Plot residuals vs. time -- should show no trend or seasonality
[ ] Histogram of residuals -- should be approximately symmetric
[ ] ACF of residuals -- should show no significant autocorrelation
[ ] Q-Q plot -- check normality assumption if relevant
[ ] Check for heteroscedasticity -- variance should be constant
```

If residuals show patterns:
- **Trend in residuals** → missing predictor or wrong functional form
- **Seasonality in residuals** → add seasonal terms
- **Increasing variance** → use log transform or heteroscedastic model
- **Autocorrelation** → add AR terms or use time series model

### 5. Uncertainty Quantification

Every model output should include uncertainty:

- **Parameter uncertainty**: confidence intervals on fitted parameters
  - Frequentist: use `lmfit` or `statsmodels` which report CIs automatically
  - Bayesian: posterior distributions from PyMC/Stan
- **Prediction uncertainty**: prediction intervals on forecasts
  - Include both parameter uncertainty and observation noise
  - For time series: prediction intervals should widen into the future
- **Sensitivity analysis**: how much do outputs change when inputs/parameters vary?

### 6. What "Good" Looks Like

Guidelines for interpreting model quality:

| Context | Good RMSE | Good R-squared | Good coverage |
|---------|-----------|----------------|---------------|
| Physical systems | < 5% of range | > 0.95 | 90-98% |
| Biological/epi systems | < 20% of range | > 0.7 | 85-95% |
| Social/economic systems | < 30% of range | > 0.5 | 80-95% |
| Highly stochastic systems | Beat naive baseline | > 0.3 | 75-95% |

These are rough guidelines. Always compare to published benchmarks in your specific domain.

---

### 7. Optimization & Resource Allocation Validation

When a model includes optimization or resource allocation, apply these
validation gates BEFORE accepting results. Do not skip gates.

#### Gate 1: Intervention Effect Verification (after calibration, before scenarios)

Compute the MARGINAL EFFECT of each intervention at high coverage
(e.g., 80%) in each geographic unit. Print a table:

| Area | Intervention | Baseline metric | With 80% cov | Reduction (%) | Published range |
|------|-------------|----------------|--------------|---------------|-----------------|

**STOP conditions (report to lead, do not proceed to optimization):**
- Any intervention reduces burden by <1% where published evidence shows >10%
- Any intervention has ZERO effect in an area where it is deployed
- A calibrated parameter hits an optimizer bound (cap)
- Cost-effectiveness for any intervention is >5x outside published range

#### Gate 2: Scenario Ranking Verification (after scenarios, before optimization)

Verify relative intervention rankings match published evidence:
- Does the most cost-effective intervention in the model match literature?
- Are the relative magnitudes (not just rankings) plausible?
- Does each intervention the question asks about have a meaningful effect?

**STOP condition:** If model's intervention rankings contradict published
evidence (e.g., SMC appears worthless when literature shows it is highly
effective), the model structure cannot support optimization. Fix the model's
intervention mechanism first, then optimize.

#### Gate 3: Optimization Result Sanity (after optimization)

- Does the optimizer invest in the highest-burden areas? If not, is there
  a documented, clinically plausible reason (e.g., diminishing returns at
  very high existing coverage) or is it a model artifact?
- Does every intervention the question asks about get nonzero allocation?
  If one gets 0%, verify the model's cost-effectiveness for that
  intervention against published ranges before accepting.
- Is the overall cost per DALY in the right ballpark vs published ranges?
- Would a domain expert engage with the result, or immediately reject it?

Report ALL gate results in `{run_dir}/modeling_strategy.md` so the lead
agent and critics can review them.

#### Dynamic vs Equilibrium Model Selection

If the research question involves:
- Time-limited interventions (seasonal chemoprophylaxis, campaigns, MDA)
- Optimizing intervention timing or seasonality
- Transient scale-up dynamics
- Interventions with nonlinear dose-response at high coverage

Then you MUST use a dynamic model (ODE with time steps, difference
equations, or agent-based), NOT equilibrium algebra. Equilibrium models
average away seasonal and transient effects, producing systematically
wrong marginal intervention effects for time-limited interventions.
