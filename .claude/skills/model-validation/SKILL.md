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
