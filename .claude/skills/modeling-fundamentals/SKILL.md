# Mathematical Modeling Fundamentals

## When to use this skill
Use when building a mathematical or statistical model for any research question.

## Modeling Workflow

Follow this order strictly. Do not skip to coding before completing steps 1-3.

### Step 1: Problem Formulation
- State the dependent variable (what you're predicting) and its units
- List candidate explanatory variables / drivers
- Identify the spatial and temporal resolution needed
- Classify the problem type:
  - **Forecasting**: predicting future values of a time series
  - **Causal/mechanistic**: understanding how inputs drive outputs
  - **Classification**: categorizing outcomes
  - **Optimization**: finding best parameters under constraints

### Step 2: Literature Review
- Search for 3-5 papers that model the same or similar phenomena
- For each paper, extract: model type used, key assumptions, data sources, reported performance
- Identify the **standard model** for this problem domain (every domain has one)
- Note any domain-specific modeling frameworks or packages

### Step 3: Model Selection
Use this decision tree:

```
Is the goal primarily prediction or understanding?
├── Prediction
│   ├── Time series data?
│   │   ├── Strong seasonality → Prophet, SARIMA, state-space models
│   │   ├── Complex nonlinear patterns → gradient boosting (XGBoost/LightGBM), LSTM
│   │   └── Short series, few features → ARIMA, exponential smoothing
│   └── Cross-sectional data?
│       ├── Linear relationships → linear/ridge/lasso regression
│       ├── Nonlinear, tabular → gradient boosting (XGBoost/LightGBM)
│       └── High-dimensional → random forest, elastic net
├── Understanding / Mechanistic
│   ├── Known dynamics (physics, biology, economics)?
│   │   ├── Differential equations → scipy.integrate, diffrax
│   │   ├── Compartmental models (epi) → use existing frameworks (see below)
│   │   └── Agent-based → Mesa, NetLogo
│   ├── Statistical relationships?
│   │   ├── Count data → Poisson/negative binomial GLM (statsmodels)
│   │   ├── Continuous outcome → linear regression, GAMs
│   │   └── Binary outcome → logistic regression
│   └── Need uncertainty quantification?
│       └── Bayesian approach → PyMC, Stan, NumPyro
└── Both prediction and understanding?
    └── Start mechanistic, compare against ML baseline
```

### Step 4: Data Exploration (before any modeling)
- Summary statistics, distributions, missing values
- Time series: plot the raw data, check for trends, seasonality, outliers
- Correlation analysis between variables
- Check data quality: coverage gaps, reporting changes, unit changes

### Step 5: Implementation Strategy
**Always start with the simplest reasonable model (baseline)**:
- For time series: seasonal naive, simple moving average, or SARIMA
- For regression: linear regression
- For classification: logistic regression
- For mechanistic: the simplest version of the standard model

Then build complexity incrementally. Each added complexity must justify itself via improved out-of-sample performance.

### Step 6: Model Fitting and Comparison
- Use proper train/test splits (for time series: use temporal splits, never random)
- Compare models using appropriate metrics (see model-validation skill)
- Report uncertainty: confidence intervals on parameters, prediction intervals on forecasts

## Don't Reinvent the Wheel

### Python Frameworks to Use

| Domain | Package | When to use |
|--------|---------|-------------|
| **Curve fitting** | `lmfit` | Parameter estimation with bounds, constraints, and uncertainty. Superior to raw `scipy.optimize` |
| **Statistical models** | `statsmodels` | GLMs, ARIMA, state-space models, time series. Use instead of hand-coding |
| **Bayesian modeling** | `PyMC` | When you need posterior distributions, uncertainty quantification |
| **Time series forecasting** | `prophet` or `statsforecast` | Seasonal time series with trend. Quick, solid baselines |
| **ML models** | `scikit-learn` | Random forests, gradient boosting, cross-validation utilities |
| **Gradient boosting** | `xgboost` or `lightgbm` | Best-in-class tabular prediction |
| **Epidemiological models** | `epyestim`, `epiweeks` | Rt estimation, epi-week handling |
| **ODE solving** | `scipy.integrate.solve_ivp` | Use `solve_ivp` not deprecated `odeint` |
| **Plotting** | `matplotlib`, `seaborn` | Always label axes with units, include legends |

### Key Principle
If a well-tested package implements what you need, **use it**. Hand-coding a model that a package already provides is a sign you haven't searched enough. The exceptions are when:
- The model is truly novel (rare)
- You need custom modifications that the package can't accommodate
- The package has a dependency that can't be installed

## Common Pitfalls

1. **Overfitting**: fitting noise instead of signal. Always validate on held-out data.
2. **Ignoring uncertainty**: point estimates without intervals are incomplete. Report confidence/prediction intervals.
3. **Using R-squared on time series**: R-squared is misleading for autocorrelated data. Use RMSE, MAE, MAPE, or proper forecast skill scores.
4. **No baseline comparison**: always compare your model to a simple baseline. If your complex model doesn't beat seasonal naive, it's not useful.
5. **Data leakage**: using future information to predict the past. In time series, always split temporally.
6. **Confusing fit with prediction**: a model that fits training data well may predict poorly. In-sample R-squared is not a measure of predictive skill.
7. **Ignoring domain knowledge**: let the data inform the model, but use domain knowledge to constrain it (parameter bounds, structural assumptions).
8. **Not checking residuals**: residuals should be approximately uncorrelated and homoscedastic. Patterned residuals indicate model misspecification.
