# modelops-calabaria API Reference

## Overview

`modelops-calabaria` (v0.1.0+) provides a structured framework for model calibration, parameter optimization, scenario simulation, and cloud scaling. It is simulation-framework-agnostic — any model wrapped as a `BaseModel` subclass can use the full calibration pipeline.

- **calabaria**: Science-facing layer (local development, `cb` CLI)
- **modelops**: Infrastructure orchestration (cloud deployment, `mops` CLI)

**Install:** `pip install modelops-calabaria`

---

## BaseModel

The central abstraction. Subclass `BaseModel` to wrap any simulation.

```python
from calabaria import BaseModel, model_output, model_scenario
from calabaria.parameters import ParameterSpace, ParameterSpec
from calabaria.parameters import ConfigurationSpace, ConfigSpec

class MyModel(BaseModel):
    PARAMS = ParameterSpace([...])   # Calibration parameters (uncertain)
    CONFIG = ConfigurationSpace([...])  # Fixed runtime settings

    def __init__(self, ...):
        """Accept pre-built data (GeoDataFrame, distances, etc.)."""
        super().__init__()
        self.data = ...

    def build_sim(self, params: dict, config: dict) -> object:
        """Construct the simulation from parameters. Returns state object."""
        ...

    def run_sim(self, state: object, seed: int) -> None:
        """Execute the simulation. Modifies state in-place."""
        ...

    @model_output("weekly_incidence")
    def weekly_incidence(self, state: object) -> pl.DataFrame:
        """Extract weekly incidence from completed simulation state."""
        ...

    @model_scenario("baseline")
    def baseline(self) -> ScenarioSpec:
        """Define a named scenario with parameter/config patches."""
        return ScenarioSpec("baseline", param_patches={}, config_patches={})
```

### Lifecycle

1. `model = MyModel(data)`
2. `state = model.build_sim(params, config)` — construct simulation
3. `model.run_sim(state, seed)` — execute simulation
4. `outputs = model.collect_outputs(state)` — gather all `@model_output` results

**Convenience method:**
```python
outputs = model.simulate(params, seed=42, config=None)
# Equivalent to build_sim → run_sim → collect_outputs
```

---

## Parameter System

### ParameterSpace

Defines **uncertain** parameters to be calibrated. Each parameter has bounds that Optuna explores.

```python
from calabaria.parameters import ParameterSpace, ParameterSpec

PARAMS = ParameterSpace([
    ParameterSpec("beta", lower=2.0, upper=6.0, kind="float",
                  doc="Transmission rate (R0 proxy)"),
    ParameterSpec("gravity_k", lower=1e-4, upper=0.1, kind="float",
                  doc="Gravity coupling constant"),
    ParameterSpec("gravity_b", lower=0.1, upper=1.5, kind="float",
                  doc="Destination population exponent"),
    ParameterSpec("gravity_c", lower=0.5, upper=3.0, kind="float",
                  doc="Distance decay exponent"),
    ParameterSpec("seasonal_amplitude", lower=0.0, upper=2.0, kind="float",
                  doc="Seasonal forcing amplitude"),
])
```

#### ParameterSpec Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Parameter name (unique within space) |
| `lower` | float | Lower bound for sampling/optimization |
| `upper` | float | Upper bound for sampling/optimization |
| `kind` | str | `"float"`, `"int"`, or `"categorical"` |
| `doc` | str | Documentation string (source, units, etc.) |
| `log_scale` | bool | If True, sample in log space (default False) |

### ConfigurationSpace

Defines **fixed** runtime settings (not calibrated).

```python
from calabaria.parameters import ConfigurationSpace, ConfigSpec

CONFIG = ConfigurationSpace([
    ConfigSpec("nticks", default=7300, doc="Simulation duration in days (20 years)"),
    ConfigSpec("burnin_years", default=10, doc="Years to discard before analysis"),
    ConfigSpec("capacity_safety_factor", default=3.0, doc="LaserFrame capacity multiplier"),
    ConfigSpec("exp_shape", default=40, doc="Gamma shape for exposed duration"),
    ConfigSpec("exp_scale", default=0.25, doc="Gamma scale for exposed duration"),
    ConfigSpec("inf_mean", default=8, doc="Mean infectious period (days)"),
    ConfigSpec("inf_sigma", default=2, doc="Std dev infectious period (days)"),
])
```

#### ConfigSpec Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Config key (unique within space) |
| `default` | Any | Default value used unless patched |
| `doc` | str | Documentation string |

---

## SimulatorBuilder and ModelSimulator

### Building a Simulator with Transforms

The `SimulatorBuilder` creates a `ModelSimulator` by fixing some parameters and applying transforms to others.

```python
simulator = model.builder("baseline") \
    .fix(seasonal_amplitude=1.0) \
    .with_transforms(
        beta=LogTransform(),
        gravity_k=LogTransform(),
    ) \
    .build()
```

### Available Transforms

| Transform | Domain → Range | Use Case |
|-----------|---------------|----------|
| `LogTransform()` | R → (0, ∞) | Positive rates: beta, gravity_k |
| `AffineSqueezedLogit(lo, hi)` | R → (lo, hi) | Bounded params: coverage ∈ [0, 1] |
| `IdentityTransform()` | R → R | No transform (default) |

### ModelSimulator

The built simulator provides a simplified interface:

```python
from calabaria.parameters import ParameterSet

# Evaluate at a point in transformed space
param_set = ParameterSet({"beta": 1.2, "gravity_k": -3.0})  # log-space values
outputs = simulator.evaluate(param_set, seed=42)

# Get free parameter specs (for Optuna)
free_specs = simulator.free_parameter_specs()
```

---

## Decorators

### @model_output(name)

Registers a method as a named output extractor. Must return `pl.DataFrame`.

```python
@model_output("weekly_incidence")
def weekly_incidence(self, state) -> pl.DataFrame:
    """Extract post-burn-in weekly incidence by patch."""
    ...
    return pl.DataFrame({"week": ..., "patch": ..., "cases": ...})

@model_output("compartments")
def compartments(self, state) -> pl.DataFrame:
    """S/E/I/R time series for diagnostics."""
    ...
    return pl.DataFrame({"tick": ..., "patch": ..., "S": ..., "E": ..., "I": ..., "R": ...})
```

### @model_scenario(name)

Registers a named scenario with parameter and config patches.

```python
@model_scenario("baseline")
def baseline(self) -> ScenarioSpec:
    return ScenarioSpec("baseline", param_patches={}, config_patches={})

@model_scenario("no_seasonality")
def no_seasonality(self) -> ScenarioSpec:
    return ScenarioSpec("no_seasonality",
                        param_patches={"seasonal_amplitude": 0.0},
                        config_patches={})

@model_scenario("high_vaccination")
def high_vaccination(self) -> ScenarioSpec:
    return ScenarioSpec("high_vaccination",
                        param_patches={},
                        config_patches={"routine_coverage": 0.95})
```

### ScenarioSpec

```python
from calabaria import ScenarioSpec

spec = ScenarioSpec(
    name="lockdown",
    param_patches={"beta": 1.5},      # Override calibration params
    config_patches={"nticks": 3650},   # Override config settings
)
```

---

## Sampling

### SobolSampler

Quasi-random space-filling design using Sobol sequences.

```python
from calabaria.sampling import SobolSampler

sampler = SobolSampler(model.PARAMS)
points = sampler.generate(n=64)  # List[dict] of 64 parameter sets

# Batch evaluation
results = []
for p in points:
    outputs = model.simulate(p, seed=42)
    results.append(outputs)
```

### GridSampler

Full factorial design over specified grid points.

```python
from calabaria.sampling import GridSampler

sampler = GridSampler(model.PARAMS, levels={"beta": 5, "gravity_k": 4})
points = sampler.generate()  # 5 × 4 = 20 parameter sets
```

---

## Calibration (Optuna Integration)

### Algorithm Adapter

calabaria wraps Optuna's TPE (Tree-structured Parzen Estimator) sampler in an ask/tell interface.

```python
from calabaria.calibration import create_algorithm_adapter, TrialResult

adapter = create_algorithm_adapter(
    "optuna",
    parameter_specs=simulator.free_parameter_specs(),
    config={"n_startup_trials": 20, "study_name": "my_calibration"},
)
adapter.initialize()
adapter.connect_infrastructure({})  # Local mode, no cloud resources
```

### Ask/Tell Loop

```python
n_trials = 100
for i in range(n_trials):
    # Ask for next parameter set to try
    trial = adapter.ask()
    param_id = trial.param_id
    params = trial.params  # dict of parameter values

    # Run simulation
    outputs = simulator.evaluate(params, seed=42)

    # Compute loss against observed data
    loss = compute_loss(outputs["weekly_incidence"], observed_data)

    # Tell the adapter the result
    result = TrialResult(
        param_id=param_id,
        loss=loss,
        status="complete",
        diagnostics={"total_cases": outputs["weekly_incidence"]["cases"].sum()},
    )
    adapter.tell(result)

# Get best parameters
best = adapter.best_trial()
print(f"Best loss: {best.loss}, params: {best.params}")
```

### TrialResult Fields

| Field | Type | Description |
|-------|------|-------------|
| `param_id` | str | ID from the `ask()` trial |
| `loss` | float | Scalar loss value (lower is better) |
| `status` | str | `"complete"`, `"failed"`, or `"pruned"` |
| `diagnostics` | dict | Optional metadata for analysis |

---

## CLI Tools

### calabaria CLI (`cb`)

```bash
# Run a single simulation with default parameters
cb run my_model.py --scenario baseline --seed 42

# Run Sobol sweep (64 points)
cb sweep my_model.py --sampler sobol --n 64 --output results/

# Run calibration
cb calibrate my_model.py --n-trials 100 --output calibration/

# Generate diagnostics report
cb diagnostics calibration/ --output report/
```

### modelops CLI (`mops`)

```bash
# Stand up cloud infrastructure (AKS + Dask cluster)
mops infra up --config infra.yaml

# Package model as OCI artifact
mops bundle push my_model.py --tag v1.0

# Submit distributed calibration job
mops jobs submit calibrate --bundle my_model:v1.0 --n-trials 1000 --workers 16

# Tear down infrastructure
mops infra down
```

---

## Cloud Scaling

### Architecture

- **Dask cluster** on Azure Kubernetes Service (AKS)
- **Warm process pools**: Pre-loaded model state avoids rebuild overhead
- **SimTask**: Unit of work = one parameter evaluation
- **OCI bundles**: Versioned model packages for reproducibility

### Workflow

```python
from modelops.infrastructure import DaskCluster
from modelops.jobs import CalibrationJob

# Connect to running cluster
cluster = DaskCluster.from_config("infra.yaml")

# Submit calibration job
job = CalibrationJob(
    model_bundle="my_model:v1.0",
    n_trials=1000,
    n_workers=16,
    sampler="optuna",
)
job.submit(cluster)

# Monitor progress
job.wait()
print(f"Best loss: {job.best_trial().loss}")
```

### Performance

| Workers | Trials | Wall Time | Speedup |
|---------|--------|-----------|---------|
| 1 | 100 | ~60 min | 1x |
| 4 | 100 | ~16 min | ~4x |
| 16 | 100 | ~4 min | ~16x |

---

## Common Patterns

### Loss Function Design

```python
import polars as pl

def compute_loss(model_df: pl.DataFrame, observed_df: pl.DataFrame) -> float:
    """Compare model output to observed data.

    Both DataFrames should have compatible columns for joining
    (e.g., year, patch, cases).
    """
    joined = model_df.join(observed_df, on=["year", "patch"], suffix="_obs")
    # Log-transformed MSE (handles wide range of case counts)
    log_model = (joined["cases"] + 1).log()
    log_obs = (joined["cases_obs"] + 1).log()
    return ((log_model - log_obs) ** 2).mean()
```

### Multi-Objective Calibration

```python
# Combine multiple metrics into single scalar loss
def combined_loss(outputs, observed):
    ccs_loss = ccs_similarity(outputs, observed)
    phase_loss = phase_similarity(outputs, observed)
    # Weighted combination
    return 0.6 * ccs_loss + 0.4 * phase_loss
```
