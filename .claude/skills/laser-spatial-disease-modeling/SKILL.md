---
name: laser-spatial-disease-modeling
description: This skill should be used for building spatial disease transmission
  models using the LASER (Light Agent Spatial modeling for ERadication) framework
  (v1.0.0+). This skill is appropriate when the user asks to model disease spread
  across geographic populations, simulate SEIR-type diseases with spatial coupling,
  set up gravity-model migration networks, wrap LASER models as BaseModel for
  calabaria calibration, or reproduce spatial epidemiological phenomena like
  traveling waves and critical community size. Trigger phrases include "LASER model",
  "spatial disease model", "gravity model transmission", "SEIR spatial simulation",
  "wavelet phase analysis", "critical community size", "build SEIR model",
  "wrap model as BaseModel", or "LASER BaseModel".
---

# Spatial Disease Transmission Modeling with LASER

## Overview

The LASER framework (v1.0.0+, December 2025) enables spatially-explicit, agent-based disease transmission modeling across geographic populations. This skill provides a complete workflow for building, calibrating, and analyzing spatial SEIR models.

**Packages:** `pip install laser-generic` (installs both `laser-core` and `laser-generic`)

---

## LAYER 1: Modeling Discipline

LASER will run your model without complaint even when it's epidemiologically wrong. Wrong units, broken vaccination, disconnected networks — all produce output that looks plausible but is silently incorrect. **You must verify.**

### 1.1 Unit Conventions

| Parameter | LASER Expects | Common Mistake | Validation Assert |
|-----------|---------------|----------------|-------------------|
| CBR (birth rate) | per-1000/year (range 10–50) | daily per-capita (0.00008) | `assert np.all(rates >= 1) and np.all(rates <= 60)` |
| CDR (death rate) | per-1000/year (range 5–20) | fraction (0.008) | `assert np.all(rates >= 1) and np.all(rates <= 60)` |
| Seasonal profile | mean ≈ 1.0 | un-normalized | `assert abs(season.mean() - 1.0) < 0.01` |
| gravity_k | 0.001–0.1 | raw flow (500) | `assert network.sum(axis=1).max() < 0.3` |

### 1.2 Silent Failure Gallery

These are the 4 most common failures. Each produces a model that runs without errors but gives wrong results:

**1. Wrong birthrate units → static population, no susceptible replenishment**
`BirthsByCBR` and `calc_capacity` expect per-1000/year. If you pass daily per-capita (e.g., 0.00008 instead of 30), `calc_capacity` sees near-zero growth → capacity ≈ initial population → `LaserFrame.add()` has no free slots → no births occur. The model runs on its initial susceptible pool and dies out.

**2. Vaccination sets susceptibility not state → zero vaccine effect**
`ImmunizationCampaign` and `RoutineImmunization` set `susceptibility = 0`. But all Transmission kernels (`TransmissionSE`, `TransmissionSI`, `TransmissionSIx`) only check `state == SUSCEPTIBLE` (int8 == 0). They do NOT check `susceptibility`. Result: vaccination has zero effect on transmission.

**3. gravity_k too small → patches evolve independently**
If `model.network` is all zeros or near-zero, each patch runs its own isolated epidemic. You get identical timing across all patches (no traveling waves), and small patches go extinct with no reimportation.

**4. Seasonal profile not normalized → systematic bias in R_eff**
If `season.mean() != 1.0`, the effective transmission rate is systematically higher or lower than `beta`. A profile with mean 1.3 means R_eff is 30% higher than intended.

### 1.3 Validation Mindset

**After building ANY component, verify it. After running ANY simulation, verify outputs.**

The `scripts/verification_checks.py` module provides 5 automated checks:
- `check_population_trajectory()` — catches wrong birthrate units
- `check_compartment_nonnegativity()` — catches depletion bugs
- `check_vaccination_effect()` — catches the susceptibility-vs-state bug
- `check_spatial_coupling()` — catches zero networks
- `check_epidemic_dynamics()` — catches beta-too-low or missing importation

Call `verify_model_health(model)` after every `model.run()`:

```python
from verification_checks import verify_model_health

model.run("Simulation")
verify_model_health(model)  # Prints report, raises on critical failures
```

### 1.4 Assertion Patterns

Copy-paste these into custom components to catch bugs at the point they occur:

```python
# After any step that decrements compartment counts:
assert np.all(model.nodes.S[tick + 1] >= 0), \
    f"Negative S at tick {tick}: min={model.nodes.S[tick+1].min()}"

# After births — population should increase:
pop_before = model.nodes.S[tick].sum() + model.nodes.E[tick].sum() + \
             model.nodes.I[tick].sum() + model.nodes.R[tick].sum()
pop_after  = model.nodes.S[tick+1].sum() + model.nodes.E[tick+1].sum() + \
             model.nodes.I[tick+1].sum() + model.nodes.R[tick+1].sum()
assert pop_after >= pop_before, f"Population decreased at tick {tick}"

# After vaccination — S should decrease, R should increase:
assert vaccinated_count == 0 or (s_after < s_before), \
    f"Vaccination had no effect at tick {tick}"

# After setting up gravity network:
assert model.network.sum() > 0, "Network is all zeros — check gravity_k"
assert model.network.sum(axis=1).max() < 0.3, \
    f"Network row sum too high ({model.network.sum(axis=1).max():.3f}) — agents may exceed patch capacity"
```

---

## LAYER 2: LASER as Scaffolding

### 2.1 What LASER Handles For You

> **DO NOT reimplement these. This is the #1 failure mode in model-building.**

LASER provides these as built-in, tested components:

- **Spatial coupling** — `TransmissionSE` computes force of infection with migration via `model.network`. Do not write your own FOI loop.
- **State machine** — `Susceptible`, `Exposed`, `Infectious`, `Recovered` components manage S→E→I→R transitions with stochastic durations. Do not write your own state transitions.
- **Birth capacity** — `Model.__init__()` calls `calc_capacity()` to pre-allocate agent slots. Do not pre-allocate manually.
- **Distance matrix** — `distance()` from `laser.core.migration` computes Haversine distances. Do not implement your own.
- **Gravity network** — if `gravity_k/a/b/c` are in params, `Model.__init__()` auto-computes the network. Manual setup is only needed for custom normalization.
- **Seasonal forcing** — `TransmissionSE` accepts a `seasonality` parameter (ValuesMap or ndarray). Do not build a custom SeasonalTransmission unless you need non-standard behavior.
- **Component ordering** — Components execute in list order each tick. `Susceptible` and `Recovered` propagate counts to maintain `S + E + I + R = N`.

### 2.2 Step 1: Environment Setup & Data Loading

```python
import numpy as np
import pandas as pd
import numba as nb
from pathlib import Path
from laser.core.propertyset import PropertySet
import laser.core.distributions as dists
from laser.core.demographics import AliasedDistribution, KaplanMeierEstimator
from laser.generic import SEIR, Model
from laser.generic.utils import ValuesMap
from laser.generic.vitaldynamics import BirthsByCBR, MortalityByEstimator, MortalityByCDR
from laser.core.migration import gravity, row_normalizer
import matplotlib.pyplot as plt
import geopandas as gpd
from shapely.geometry import Point
```

Additional imports available in v1.0.0:
```python
from laser.core.migration import competing_destinations, stouffer, radiation, distance
from laser.generic.importation import Infect_Random_Agents, Infect_Agents_In_Patch
from laser.generic.components import Susceptible, TransmissionSE, Exposed, InfectiousIR, Recovered
```

Data requirements:
- **Population data**: Per-patch population counts, geographic coordinates (lat/lon)
- **Birth/death data**: Crude birth rates per patch (per-1000/year) or survival curves
- **Case data** (for calibration): Historical incidence time series per patch
- **Distance matrix**: Pairwise geodesic distances between patches (km)

---

### 2.3 Step 2: Build the Geographic Scenario

The scenario is a GeoDataFrame with one row per spatial patch.

```python
scenario = gpd.GeoDataFrame(cells, crs="EPSG:4326")
# Required columns: nodeid, name, population, geometry
# Initial conditions for endemic equilibrium:
scenario["E"] = 0
scenario["I"] = 3  # Seed infectious in every patch
scenario["R"] = np.round(0.95 * scenario.population).astype(np.uint32)
scenario["S"] = scenario.population - scenario["E"] - scenario["I"] - scenario["R"]
```

**Validation — verify initial conditions sum correctly:**
```python
assert (scenario.S + scenario.E + scenario.I + scenario.R == scenario.population).all(), \
    "Initial S+E+I+R must equal population in every patch"
assert (scenario.I > 0).any(), "At least one patch needs initial infections"
```

---

### 2.4 Step 3: Seasonal Forcing

> **LASER handles this:** `TransmissionSE` accepts a `seasonality` parameter. You just need to build the 365-day profile and wrap it in a `ValuesMap`. Do NOT build a custom SeasonalTransmission unless you need non-standard behavior (e.g., per-node profiles, tick%365 cycling).

#### Bjornstad school-term profile (measles)

```python
from laser.generic.utils import ValuesMap

log_betas = np.array([
    0.155, 0.571, 0.46, 0.34, 0.30, 0.34, 0.24, 0.15,
    0.31, 0.40, 0.323, 0.238, 0.202, 0.203, 0.074,
    -0.095, -0.218, -0.031, 0.433, 0.531, 0.479, 0.397,
    0.444, 0.411, 0.291, 0.509
])
beta_season = np.repeat(log_betas, int(np.floor(365 / len(log_betas))))
beta_season = np.append(beta_season, beta_season[-1])
beta_season = np.exp(beta_season - np.mean(beta_season))
```

#### General recipe: cosine/Fourier profiles

```python
# Peaked-season profile (peak at day 200, amplitude 0.3)
days = np.arange(365)
peak_day = 200
season_365 = 1.0 + 0.3 * np.cos(2 * np.pi * (days - peak_day) / 365)
season_365 /= season_365.mean()  # Normalize to mean == 1.0
```

#### No seasonality

```python
seasonality = ValuesMap.from_scalar(1.0, nticks, nnodes)
```

**Gotcha — normalize the profile:**
```python
# ALWAYS normalize to mean 1.0. An un-normalized profile biases R_eff.
assert abs(season_365.mean() - 1.0) < 0.01, \
    f"Seasonal profile mean={season_365.mean():.3f}, must be ~1.0"
```

#### Wrap for LASER

```python
nticks = 40 * 365
season_tiled = np.tile(season_365, nticks // 365 + 1)[:nticks]
seasonality = ValuesMap.from_timeseries(season_tiled, len(scenario))

# Pass directly to the built-in Transmission component (Step 7):
# SEIR.Transmission(model, expdurdist, seasonality=seasonality)
```

---

### 2.5 Step 4: Gravity Migration Network

> **LASER handles this:** If `gravity_k`, `gravity_a`, `gravity_b`, `gravity_c` are in params, `Model.__init__()` auto-computes the network from scenario centroids. Use manual setup below only when you need custom normalization.

Spatial coupling follows a gravity law: $M_{i,j} = k \cdot p_j^b / d_{ij}^c$ (with source exponent `a=0` fixed).

**Manual setup (for custom normalization):**
```python
model.network = gravity(
    np.array(scenario.population), distances,
    1, 0, model.params.gravity_b, model.params.gravity_c
)
# Normalize so k represents average export fraction directly
average_export_frac = np.mean(model.network.sum(axis=1))
model.network = model.network / average_export_frac * model.params.gravity_k
model.network = row_normalizer(model.network, 0.2)  # Cap at 20% export per node
```

**Gotcha — verify the network is non-trivial:**
```python
assert model.network.sum() > 0, \
    "Network is all zeros — gravity_k too small or distances are wrong"
assert model.network.sum(axis=1).max() < 0.3, \
    f"Max row sum = {model.network.sum(axis=1).max():.3f} — may be too high"
```

Alternative migration models: `competing_destinations()`, `stouffer()`, `radiation()` from `laser.core.migration`.

---

### 2.6 Step 5: Vital Dynamics

> **LASER handles this:** `BirthsByCBR` manages births with capacity pre-allocation. `MortalityByCDR` or `MortalityByEstimator` manages deaths. Do NOT implement your own birth/death logic.

#### Births

```python
from laser.generic.vitaldynamics import BirthsByCBR

BirthsByCBR(model, birthrates=birthrate_values, pyramid=pyramid)
```

`birthrates` must be shape `(nticks, nnodes)` with values in **per-1000/year** (typical range 10–50). Use `ValuesMap` to construct from scalars or 1D arrays.

**Gotcha — wrong units are the #1 silent failure:**
```python
# This MUST be true. If birthrates are daily per-capita (e.g., 0.00008),
# calc_capacity sees near-zero growth → no births → model dies out silently.
assert np.all(birthrate_values >= 1) and np.all(birthrate_values <= 60), \
    f"Birthrates must be per-1000/year, got {birthrate_values.min():.4f}-{birthrate_values.max():.4f}"
```

**`on_birth` callback:** After adding newborns, `BirthsByCBR` calls `on_birth(self, istart, iend, tick)` on every component that defines it. Use this to initialize custom properties on newborns.

#### Deaths

```python
from laser.generic.vitaldynamics import MortalityByCDR, MortalityByEstimator

# Option A: Crude death rate (per-1000/year)
MortalityByCDR(model, mortalityrates=deathrate_values)
# NOTE: parameter name is `mortalityrates=`, NOT `deathrates=`

# Option B: Age-based survival curve
MortalityByEstimator(model, estimator=survival_estimator)
```

---

### 2.7 Step 6: Vaccination

> **WARNING: `ImmunizationCampaign` and `RoutineImmunization` have NO effect on transmission.** They set `susceptibility = 0`, but Transmission kernels only check `state == SUSCEPTIBLE`. You must use alternatives that set `state = RECOVERED`.

#### Option A: RoutineImmunizationEx (built-in, correct)

Sets `state = RECOVERED` and updates node S/R counts. Takes Numba-compiled callables:

```python
from laser.generic.immunization import RoutineImmunizationEx
import laser.core.distributions as dists
import numba as nb

dose_timing = dists.constant_int(270)  # Vaccinate at age 270 days

@nb.njit
def coverage_fn(tick, nodeid):
    return 0.85  # 85% coverage

RoutineImmunizationEx(model, coverage_fn, dose_timing)
```

#### Option B: VaccinationCampaign (custom, simpler API)

From `scripts/custom_components.py`. Supports correlated missedness for hard-to-reach populations:

```python
from custom_components import VaccinationCampaign

VaccinationCampaign(model, period=180, coverage=coverage_array,
                    age_lower=0, age_upper=5*365,
                    unreachable_frac=0.15)  # 15% permanently unreachable
```

Use correlated missedness when vaccine access is heterogeneous and multiple rounds target the same population (prevents overestimating cumulative coverage from independent draws).

---

### 2.8 Step 7: Assemble and Run

> **Component ordering matters:** Components execute in list order each tick. `Susceptible` and `Recovered` should wrap the transition steps to preserve `S + E + I + R = N`.

```python
parameters = PropertySet({
    "prng_seed": 4, "nticks": 40 * 365,
    "exp_shape": 40, "exp_scale": 0.25,   # Exposed ~10 days (gamma)
    "inf_mean": 8, "inf_sigma": 2,         # Infectious ~8 days (normal)
    "beta": 3.5,
    "cbr": average_cbr,
    "gravity_k": 0.01, "gravity_b": 0.5, "gravity_c": 1.5,
    "capacity_safety_factor": 3.0,
})

expdurdist = dists.gamma(shape=parameters.exp_shape, scale=parameters.exp_scale)
infdurdist = dists.normal(loc=parameters.inf_mean, scale=parameters.inf_sigma)

# Build seasonality ValuesMap (from Step 3)
nticks = parameters.nticks
nnodes = len(scenario)
season_tiled = np.tile(beta_season, nticks // 365 + 1)[:nticks]
seasonality = ValuesMap.from_timeseries(season_tiled, nnodes)

model = Model(scenario, parameters, birthrates=birthrate_map.values)
# NOTE: birthrate_map.values must be per-1000/year. See Step 5.

model.components = [
    SEIR.Susceptible(model),
    SEIR.Exposed(model, expdurdist, infdurdist),
    SEIR.Infectious(model, infdurdist),
    SEIR.Recovered(model),
    Importation(model, infdurdist, period=30, count=3, end_tick=10 * 365),
    SEIR.Transmission(model, expdurdist, seasonality=seasonality),
    BirthsByCBR(model, birthrates=birthrate_map.values, pyramid=pyramid),
    MortalityByEstimator(model, estimator=survival),
    # Vaccination (do NOT use ImmunizationCampaign — see Step 6):
    # RoutineImmunizationEx(model, coverage_fn, dose_timing),
    # VaccinationCampaign(model, period=180, coverage=coverage_array),
]

# Set up gravity network (Step 4) then run:
model.run("Simulation")

# ALWAYS verify after running:
from verification_checks import verify_model_health
verify_model_health(model)
```

---

### 2.9 Step 8: Graduating to Custom Components

Start with built-in components. Graduate to custom only when you need non-standard behavior. Most models can be built entirely from built-ins.

**When to go custom:**
- Built-in importation infects random agents → custom `Importation` targets susceptibles only
- Built-in vaccination API requires Numba callables → custom `VaccinationCampaign` uses simple arrays
- Need correlated missedness → `VaccinationCampaign` with `unreachable_frac`
- Need tick%365 cycling → custom `SeasonalTransmission`

**Custom component anatomy:**
```python
class MyComponent:
    def __init__(self, model, ...):
        self.model = model
        # Add any custom properties to model.people or model.nodes here

    def step(self, tick):
        # Called once per tick. Modify model state.
        pass

    def on_birth(self, istart, iend, tick):
        # Optional: initialize custom properties for newborns (istart:iend slice)
        pass
```

**Production pattern** (from `scripts/custom_components.py`):
- `__init__`: Store model reference, add custom properties, validate inputs
- `step(tick)`: Main logic — modify states, update node counts
- `on_birth(istart, iend, tick)`: Initialize per-agent properties for newborns
- Always update node-level counts (`nodes.S`, `nodes.R`) when changing agent states

See `scripts/custom_components.py` for complete reference implementations of `Importation`, `VaccinationCampaign`, and `SeasonalTransmission`.

---

### 2.10 Step 9: Wrap as BaseModel for Calibration

After building and testing the LASER model (Steps 1-8), wrap it inside calabaria's `BaseModel` for structured calibration, scenario management, and cloud scaling.

**Why:** calabaria provides structured parameter spaces (with bounds and transforms), Optuna-based optimization (ask/tell loop), scenario management (`@model_scenario`), and optional cloud scaling via modelops.

**Install:** `pip install modelops-calabaria`

#### Bridge Pattern: LASER Model inside BaseModel

```python
from calabaria import BaseModel, model_output, model_scenario, ScenarioSpec
from calabaria.parameters import ParameterSpace, ParameterSpec
from calabaria.parameters import ConfigurationSpace, ConfigSpec

class MySpatialSEIR(BaseModel):
    PARAMS = ParameterSpace([
        ParameterSpec("beta", lower=2.0, upper=6.0, kind="float", doc="..."),
        ParameterSpec("gravity_k", lower=1e-4, upper=0.1, kind="float", doc="..."),
    ])
    CONFIG = ConfigurationSpace([
        ConfigSpec("nticks", default=7300, doc="Simulation duration (days)"),
    ])

    def __init__(self, scenario_gdf, distances, birthrates, deathrates, pyramid):
        super().__init__()
        self.scenario_gdf = scenario_gdf
        # ... store pre-built data from Steps 1-5

    def build_sim(self, params, config):
        # Construct LASER Model + PropertySet + gravity network + seasonality
        # + components using params and config values
        return model  # LASER Model object

    def run_sim(self, state, seed):
        laser.core.random.seed(seed)
        state.run()
        # Post-run verification
        verify_model_health(state, raise_on_critical=False)

    @model_output("weekly_incidence")
    def weekly_incidence(self, state):
        # Extract post-burn-in weekly incidence → pl.DataFrame
        ...
```

A complete disease-agnostic template is in `scripts/laser_basemodel.py`. Customize the component list, parameter ranges, and output extractors for your disease.

**Next step:** Use the `modelops-calabaria` skill for calibration workflow (Sobol sweeps, Optuna optimization, scenario analysis).

---

### 2.11 Step 10: Quick Verification Run

Before calibration, verify the wrapped model produces sensible output:

```python
model = MySpatialSEIR(scenario_gdf, distances, birthrates, deathrates, pyramid)

# Run with plausible parameter values
outputs = model.simulate(
    {"beta": 3.5, "gravity_k": 0.01, "gravity_b": 0.5,
     "gravity_c": 1.5, "seasonal_amplitude": 1.0},
    seed=42,
)

# Check outputs
print(outputs["weekly_incidence"].head())
print(f"Total cases: {outputs['weekly_incidence']['cases'].sum()}")

# Verify non-zero incidence and spatial variation
by_patch = outputs["weekly_incidence"].group_by("patch").agg(
    pl.col("cases").sum()
)
print(by_patch)
```

If the model health report shows failures, consult Layer 3 below for diagnosis.

---

## LAYER 3: Verification and Feedback

### 3.1 Post-Build Verification Suite

Run after every `model.run()` using `verify_model_health(model)` from `scripts/verification_checks.py`. The 5 checks:

| Check | What It Catches | Critical? |
|-------|-----------------|-----------|
| `check_population_trajectory` | Wrong birthrate units (static pop) | Yes |
| `check_compartment_nonnegativity` | Depletion bugs in custom components | Yes |
| `check_vaccination_effect` | susceptibility-vs-state bug | If vaccination active |
| `check_spatial_coupling` | Zero network, no spatial structure | Yes |
| `check_epidemic_dynamics` | beta too low, missing importation, disease extinction | Yes |

Each check returns `{"passed": bool, "message": str, "details": dict}`.

### 3.2 Expected Output Patterns

**Endemic disease (e.g., measles in large populations):**
- Recurrent annual/biennial epidemics with inter-epidemic troughs
- Large cities: continuous transmission, no fadeouts
- Small patches: periodic extinction and reimportation
- Spatial phase lags (traveling waves from large to small populations)

**Emerging outbreak (single introduction):**
- Exponential growth phase → peak → decline
- Spatial spread follows network connectivity (gravity model)
- Total attack rate depends on R0 and population immunity

**Disease under vaccination:**
- Reduced peak incidence proportional to effective coverage
- Longer inter-epidemic periods
- Possible elimination in well-vaccinated patches with continued importation in under-vaccinated ones

### 3.3 Diagnostic Plots

Five plot recipes for verifying model behavior:

```python
import matplotlib.pyplot as plt

# 1. Epidemic curve (total daily incidence across all patches)
total_incidence = model.nodes.newly_infected[:nticks, :].sum(axis=1)
plt.figure(); plt.plot(total_incidence); plt.title("Epidemic Curve")
plt.xlabel("Day"); plt.ylabel("New infections"); plt.show()

# 2. Susceptible fraction over time (S/N per patch)
S = model.nodes.S[:nticks, :]
N = S + model.nodes.E[:nticks, :] + model.nodes.I[:nticks, :] + model.nodes.R[:nticks, :]
frac_S = S.sum(axis=1) / N.sum(axis=1)
plt.figure(); plt.plot(frac_S); plt.title("Susceptible Fraction (National)")
plt.xlabel("Day"); plt.ylabel("S/N"); plt.show()

# 3. Spatial heatmap (weekly incidence by patch over time)
weekly = model.nodes.newly_infected[:nticks, :].reshape(-1, 7, nnodes).sum(axis=1)
plt.figure(); plt.imshow(weekly.T, aspect="auto", cmap="hot")
plt.title("Weekly Incidence by Patch"); plt.xlabel("Week"); plt.ylabel("Patch")
plt.colorbar(label="Cases"); plt.show()

# 4. Population trajectory (total population over time)
pop = N.sum(axis=1)
plt.figure(); plt.plot(pop); plt.title("Total Population")
plt.xlabel("Day"); plt.ylabel("Population"); plt.show()

# 5. Vaccination coverage verification (R fraction over time)
R = model.nodes.R[:nticks, :]
frac_R = R.sum(axis=1) / N.sum(axis=1)
plt.figure(); plt.plot(frac_R); plt.title("Recovered Fraction (includes vaccinated)")
plt.xlabel("Day"); plt.ylabel("R/N"); plt.show()
```

### 3.4 Red Flags Table

| Symptom | Likely Cause | Diagnostic Check |
|---------|-------------|------------------|
| Cases = 0 | beta too low, no initial infections | `check_epidemic_dynamics` — verify beta * I_init > 0 |
| All patches identical timing | Zero or near-zero network | `check_spatial_coupling` — verify `network.sum() > 0` |
| Population static over years | Wrong birthrate units | `check_population_trajectory` — rates must be per-1000/year |
| Vaccination no effect | susceptibility bug | `check_vaccination_effect` — must set `state = RECOVERED` |
| Disease goes extinct | No importation or importation ended too early | `check_epidemic_dynamics` — extend importation period |
| Negative compartment counts | Depletion bug in custom component | `check_compartment_nonnegativity` — add `max(0, ...)` guards |
| `ValueError` from `LaserFrame.add()` | Capacity exceeded | Increase `capacity_safety_factor` (try 3–4) |
| Wavelet NaN | Time series with all-zero patches | Pad with `pad_data()` from wavelet reference |
| BaseModel output wrong type | Returned numpy or pandas | All `@model_output` methods must return `pl.DataFrame` |

---

## Key Concepts

- **Critical Community Size (CCS)**: The minimum population for sustained endemic transmission without stochastic fadeout. Disease-specific (e.g., ~300K-500K for measles).
- **Traveling Waves**: Epidemics propagate from large cities to smaller populations, producing distance-dependent phase lags in wavelet analysis.
- **Gravity Model**: Spatial coupling scales with destination population and inversely with distance. The `a=0` convention means source population does not affect outward flow.
- **Seasonal Forcing**: Transmission oscillations driven by climate, behavior, or school terms. See Step 3 for general recipe.
- **BaseModel Bridge**: Wrapping a LASER Model inside calabaria's `BaseModel` enables structured calibration via Optuna, scenario management, and cloud scaling.

---

## Bundled Resources

- **`scripts/custom_components.py`** — `Importation` (susceptible-targeted seeding), `VaccinationCampaign` (correct state-based vaccination with correlated missedness), and `SeasonalTransmission` (advanced customization example)
- **`scripts/calibration_metrics.py`** — CCS logistic fitting, wavelet phase similarity scoring, combined ranking, and `compute_calibration_loss()` bridge for calabaria `TrialResult`
- **`scripts/laser_basemodel.py`** — Disease-agnostic `SpatialSEIRModel(BaseModel)` template with post-run verification
- **`scripts/verification_checks.py`** — 5 automated model health checks: population trajectory, compartment non-negativity, vaccination effect, spatial coupling, epidemic dynamics. Call `verify_model_health(model)` after every run.
- **`references/laser_api_reference.md`** — Complete LASER v1.0.0 API documentation (Model, LaserFrame, PropertySet, all component variants, vital dynamics, migration models, distributions)
- **`references/wavelet_analysis.md`** — Wavelet transform functions, phase difference computation, and traveling wave detection workflow

---

## Troubleshooting

1. **`ValueError` from `LaserFrame.add()`**: Population growth exceeded pre-allocated capacity. Increase `capacity_safety_factor` (try 3–4 for high-growth populations or long simulations).
2. **All epidemics die out**: Check `beta`, initial `I` counts, importation settings. Verify importation is active during burn-in. See Red Flags Table in Layer 3.
3. **Out of memory**: Reduce `capacity_safety_factor` or `nticks`. Each agent consumes memory for all state arrays.
4. **Wavelet NaN**: Time series with insufficient non-zero values. Pad using `pad_data()` from the wavelet reference.
5. **BaseModel `model_output` returns wrong type**: All `@model_output` methods must return `pl.DataFrame`. Use polars, not numpy or pandas.

---

## References

- [LASER Documentation](https://laser.idmod.org/laser-generic/)
- [Grenfell et al. (2001) - Travelling waves in measles](https://www.nature.com/articles/414716a)
- [Bjornstad et al. (2002) - Estimating transmission rates](https://doi.org/10.1890/0012-9615(2002)072[0169:DOMEES]2.0.CO;2)
- [Conlan et al. (2010) - Waiting time distributions and measles persistence](https://pmc.ncbi.nlm.nih.gov/articles/PMC2842776/)
- [LASER GitHub - laser-core](https://github.com/laser-base/laser-core)
- [LASER GitHub - laser-generic](https://github.com/laser-base/laser-generic)
