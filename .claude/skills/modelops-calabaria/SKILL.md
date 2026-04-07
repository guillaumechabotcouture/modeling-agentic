---
name: modelops-calabaria
description: Use for model calibration, parameter optimization, scenario
  simulation, and scaling to cloud via the modelops-calabaria framework.
  Covers structured parameter spaces, Sobol/grid sampling, Optuna ask/tell
  optimization, scenario management, and cloud deployment. Works with any
  BaseModel-wrapped simulation (LASER, starsim, or custom). Trigger phrases
  include "calibrate model", "parameter sweep", "Sobol sampling", "Optuna",
  "modelops", "calabaria", "run scenarios", "parameter optimization",
  "structured calibration", "cloud scaling", "distributed calibration".
---

# Model Calibration and Optimization with modelops-calabaria

## Overview

**calabaria** is the science-facing framework for structured model calibration, parameter optimization, and scenario management. **modelops** is the infrastructure layer for cloud scaling. Together they provide a complete pipeline from local exploration to distributed calibration.

- **Framework-agnostic**: Works with any `BaseModel` subclass (LASER, starsim, or custom)
- **Local CLI**: `cb` for single runs, sweeps, and calibration
- **Cloud CLI**: `mops` for AKS infrastructure, bundling, and distributed jobs

**Install:** `pip install modelops-calabaria` (Python 3.12+)

**Prerequisite:** A `BaseModel`-wrapped model. See the `laser-spatial-disease-modeling` skill for how to wrap a LASER model, or subclass `BaseModel` directly for other frameworks.

---

## Workflow

### Step 1: Define Parameter Space

Separate uncertain parameters (to calibrate) from fixed settings (known values).

```python
from calabaria.parameters import ParameterSpace, ParameterSpec
from calabaria.parameters import ConfigurationSpace, ConfigSpec

# Uncertain parameters — Optuna explores these ranges
PARAMS = ParameterSpace([
    ParameterSpec("beta", lower=2.0, upper=6.0, kind="float",
                  doc="Transmission rate"),
    ParameterSpec("gravity_k", lower=1e-4, upper=0.1, kind="float",
                  doc="Gravity coupling constant"),
    ParameterSpec("gravity_b", lower=0.1, upper=1.5, kind="float",
                  doc="Destination population exponent"),
    ParameterSpec("gravity_c", lower=0.5, upper=3.0, kind="float",
                  doc="Distance decay exponent"),
    ParameterSpec("seasonal_amplitude", lower=0.0, upper=2.0, kind="float",
                  doc="Seasonal forcing amplitude"),
])

# Fixed settings — not calibrated
CONFIG = ConfigurationSpace([
    ConfigSpec("nticks", default=7300, doc="Simulation duration (days)"),
    ConfigSpec("burnin_years", default=10, doc="Years to discard"),
    ConfigSpec("capacity_safety_factor", default=3.0, doc="LaserFrame capacity"),
])
```

These are class attributes on your `BaseModel` subclass. The `epi-model-parametrization` skill can help identify which parameters need calibration vs. which are well-known from literature.

---

### Step 2: Structured Sampling (Exploration)

Before optimizing, explore the parameter space with structured sampling to understand sensitivity and identify promising regions.

#### Sobol Quasi-Random Sweep

```python
from calabaria.sampling import SobolSampler

sampler = SobolSampler(model.PARAMS)
points = sampler.generate(n=64)  # 64 space-filling parameter sets

# Batch evaluation
results = []
for p in points:
    outputs = model.simulate(p, seed=42)
    loss = compute_loss(outputs["weekly_incidence"], observed_data)
    results.append({"params": p, "loss": loss, "outputs": outputs})

# Analyze: which parameters most affect loss?
```

#### Grid Sweep

```python
from calabaria.sampling import GridSampler

sampler = GridSampler(model.PARAMS, levels={"beta": 5, "gravity_k": 4})
points = sampler.generate()  # 5 × 4 = 20 factorial combinations
```

**When to use which:**
- **Sobol**: Space-filling, efficient for high-dimensional spaces (5+ params)
- **Grid**: Exhaustive, good for 2-3 parameter sensitivity analysis

---

### Step 3: Build Simulator with Transforms

The `SimulatorBuilder` creates a `ModelSimulator` by fixing some parameters and applying transforms to others. Transforms map unbounded optimizer space to bounded parameter domains.

```python
from calabaria.transforms import LogTransform, AffineSqueezedLogit

# Fix seasonal_amplitude, apply log-transform to positive-only params
simulator = model.builder("baseline") \
    .fix(seasonal_amplitude=1.0) \
    .with_transforms(
        beta=LogTransform(),             # R → (0, ∞)
        gravity_k=LogTransform(),        # R → (0, ∞)
    ) \
    .build()

# The simulator now has 3 free parameters (beta, gravity_b, gravity_c)
# with beta and gravity_k optimized in log-space
free_specs = simulator.free_parameter_specs()
```

**Available transforms:**

| Transform | Maps | Use for |
|-----------|------|---------|
| `LogTransform()` | R → (0, ∞) | Rates, coupling constants |
| `AffineSqueezedLogit(lo, hi)` | R → (lo, hi) | Bounded fractions (coverage) |
| `IdentityTransform()` | R → R | Unbounded parameters |

---

### Step 4: Optuna Calibration

The core calibration loop uses Optuna's TPE (Tree-structured Parzen Estimator) via an ask/tell interface.

```python
from calabaria.calibration import create_algorithm_adapter, TrialResult

# Create adapter with Optuna backend
adapter = create_algorithm_adapter(
    "optuna",
    parameter_specs=simulator.free_parameter_specs(),
    config={"n_startup_trials": 20, "study_name": "spatial_seir_cal"},
)
adapter.initialize()
adapter.connect_infrastructure({})  # Local mode

# Ask/tell calibration loop
n_trials = 100
for i in range(n_trials):
    # Ask: get next parameter set to evaluate
    trial = adapter.ask()

    # Evaluate: run simulation
    outputs = simulator.evaluate(trial.params, seed=42)

    # Score: compute loss against observed data
    loss = compute_loss(outputs["weekly_incidence"], observed_data)

    # Tell: report result back to optimizer
    result = TrialResult(
        param_id=trial.param_id,
        loss=loss,
        status="complete",
        diagnostics={
            "total_cases": int(outputs["weekly_incidence"]["cases"].sum()),
        },
    )
    adapter.tell(result)

    if (i + 1) % 20 == 0:
        best = adapter.best_trial()
        print(f"Trial {i+1}: best loss = {best.loss:.4f}")

# Final best parameters
best = adapter.best_trial()
print(f"Best parameters: {best.params}")
print(f"Best loss: {best.loss:.4f}")
```

#### Loss Function Design

The loss function compares model output to observed data and returns a single scalar (lower is better).

```python
import polars as pl

def compute_loss(model_df: pl.DataFrame, observed_df: pl.DataFrame) -> float:
    """Compare model weekly incidence to observed case data."""
    joined = model_df.join(observed_df, on=["year", "patch"], suffix="_obs")
    # Log-transformed MSE handles wide range of case counts
    log_model = (joined["cases"] + 1).log()
    log_obs = (joined["cases_obs"] + 1).log()
    return ((log_model - log_obs) ** 2).mean()
```

**Tips for good loss functions:**
- Log-transform counts before comparing (avoids domination by large patches)
- Match temporal resolution: aggregate model output to match observed data granularity
- Consider multi-objective: combine spatial pattern (CCS) + temporal pattern (wavelet phase) losses
- Use `calibration_metrics.py` from the `laser-spatial-disease-modeling` skill for CCS and wavelet metrics

---

### Step 5: Scenario Analysis

Compare intervention scenarios using the calibrated parameters.

#### Define Scenarios

Scenarios are defined as `@model_scenario` methods on your `BaseModel` subclass:

```python
from calabaria import model_scenario, ScenarioSpec

@model_scenario("baseline")
def baseline(self) -> ScenarioSpec:
    return ScenarioSpec("baseline", param_patches={}, config_patches={})

@model_scenario("no_seasonality")
def no_seasonality(self) -> ScenarioSpec:
    return ScenarioSpec("no_seasonality",
                        param_patches={"seasonal_amplitude": 0.0},
                        config_patches={})

@model_scenario("high_coverage")
def high_coverage(self) -> ScenarioSpec:
    return ScenarioSpec("high_coverage",
                        param_patches={},
                        config_patches={"routine_coverage": 0.95})
```

#### Run and Compare

```python
# Use best calibrated parameters
best_params = best.params

# Run each scenario
baseline_out = model.simulate_scenario("baseline", best_params, seed=42)
no_season_out = model.simulate_scenario("no_seasonality", best_params, seed=42)
high_cov_out = model.simulate_scenario("high_coverage", best_params, seed=42)

# Compare outputs
import polars as pl

comparison = pl.DataFrame({
    "scenario": ["baseline", "no_seasonality", "high_coverage"],
    "total_cases": [
        baseline_out["weekly_incidence"]["cases"].sum(),
        no_season_out["weekly_incidence"]["cases"].sum(),
        high_cov_out["weekly_incidence"]["cases"].sum(),
    ],
})
print(comparison)
```

---

### Step 6: Diagnostics and Results

#### Accessing Results

All model outputs are `Dict[str, pl.DataFrame]`:

```python
outputs = model.simulate(best_params, seed=42)
weekly_inc = outputs["weekly_incidence"]   # pl.DataFrame
compartments = outputs["compartments"]     # pl.DataFrame (if defined)
```

#### Convergence Analysis

```python
import optuna

# Access the underlying Optuna study
study = adapter.study

# Plot optimization history
optuna.visualization.plot_optimization_history(study)

# Plot parameter importances
optuna.visualization.plot_param_importances(study)

# Plot parallel coordinate
optuna.visualization.plot_parallel_coordinate(study)
```

#### CLI Diagnostics

```bash
# Generate diagnostics report from calibration output
cb diagnostics calibration_output/ --output report/
```

---

### Step 7: Scale to Cloud (Optional)

For large-scale calibration (1000+ trials) or ensemble simulations, deploy to Azure Kubernetes Service.

#### Infrastructure Setup

```bash
# Stand up AKS cluster with Dask
mops infra up --config infra.yaml

# Package model as OCI artifact
mops bundle push my_model.py --tag v1.0

# Submit distributed calibration (16 parallel workers)
mops jobs submit calibrate \
    --bundle my_model:v1.0 \
    --n-trials 1000 \
    --workers 16

# Monitor
mops jobs status

# Retrieve results
mops jobs results --output results/

# Tear down
mops infra down
```

#### Performance Expectations

| Workers | 100 Trials | 1000 Trials |
|---------|-----------|-------------|
| 1 | ~60 min | ~10 hrs |
| 4 | ~16 min | ~2.5 hrs |
| 16 | ~4 min | ~40 min |

Speedup is near-linear due to warm process pools that pre-load model state.

---

## Troubleshooting

1. **"ParameterSet validation error"**: Check that all parameter values are within `lower`/`upper` bounds and correct `kind` (float vs int).
2. **Optuna not converging**: Increase `n_startup_trials` (default 20, try 50 for 5+ params), widen parameter bounds, or check that loss function is numerically stable.
3. **`model_output` returns wrong type**: All `@model_output` methods must return `pl.DataFrame`, not numpy arrays or pandas DataFrames.
4. **Slow single evaluations**: Profile `build_sim` vs `run_sim`. If build is slow, consider caching invariant state.
5. **Cloud jobs failing**: Verify bundle includes all dependencies. Check `mops jobs logs` for stack traces.
6. **Loss is NaN/Inf**: Add guards in loss function for zero-count patches. Use `log(x + 1)` instead of `log(x)`.

---

## Bundled Resources

- **`references/modelops_calabaria_reference.md`** — Complete API reference for BaseModel, ParameterSystem, SimulatorBuilder, Decorators, Sampling, Calibration, CLI, and Cloud Scaling

---

## References

- [modelops-calabaria documentation](https://modelops.readthedocs.io/)
- [Optuna: A Next-generation Hyperparameter Optimization Framework](https://optuna.org/)
- [Sobol Sequences for Quasi-Random Sampling](https://en.wikipedia.org/wiki/Sobol_sequence)
