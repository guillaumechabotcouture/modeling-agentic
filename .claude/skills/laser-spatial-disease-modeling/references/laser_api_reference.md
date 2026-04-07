# LASER Framework API Reference (v1.0.0)

> **Version**: 1.0.0 (December 2025)
> **Packages**: `laser-core`, `laser-generic` (install via `pip install laser-generic`)
> **GitHub**: [laser-base/laser-core](https://github.com/laser-base/laser-core), [laser-base/laser-generic](https://github.com/laser-base/laser-generic)
> **Docs**: [laser.idmod.org](https://laser.idmod.org/)

---

## Core Package: `laser.core`

### LaserFrame

Data container for managing dynamically allocated agent or node data arrays. Uses struct-of-arrays (SoA) layout for cache-friendly operations.

```python
from laser.core import LaserFrame

frame = LaserFrame(capacity, initial_count=-1, **kwargs)
```

**Properties:**
- `capacity`: Maximum number of entries (read-only)
- `count`: Current active element count (read-only)

**Methods:**
- `add(count)` - Increment count, returns (start, end) indices
- `add_scalar_property(name, dtype, default)` - Creates 1D numpy array property
- `add_vector_property(name, length, dtype, default)` - Creates 2D array property (e.g., per-tick tracking)
- `add_array_property(name, shape, dtype, default)` - Creates N-dimensional array property
- `sort(indices, verbose)` - Reorders all arrays by provided index array
- `squash(indices, verbose)` - Filters arrays using boolean mask
- `describe(target)` - Returns formatted summary of all properties
- `save_snapshot(path, results_r, pars)` - Exports frame state to HDF5
- `load_snapshot(path, n_ppl, cbr, nt)` - Imports frame state from HDF5

---

### PropertySet

Dictionary-like parameter container with attribute-style access.

```python
from laser.core.propertyset import PropertySet

params = PropertySet({"beta": 3.5, "nticks": 14600})
print(params.beta)  # 3.5
```

**Operations:**
- `ps[key]` / `ps.key` - Access values
- `ps1 + ps2` - Merge (creates new PropertySet)
- `ps1 += ps2` - In-place add (new keys only)
- `ps1 <<= ps2` - Override (existing keys only)
- `ps1 |= ps2` - Flexible merge (add or override)
- `ps.to_dict()` - Convert to dictionary
- `ps.save(filename)` / `PropertySet.load(filename)` - JSON persistence

---

### SortedQueue

Priority queue using NumPy heap for agent event scheduling.

```python
from laser.core import SortedQueue

queue = SortedQueue(capacity, values)  # values = external array reference
queue.push(index)
idx = queue.popi()        # Pop minimum-value index
val = queue.popv()        # Pop minimum value
idx, val = queue.popiv()  # Pop both
idx = queue.peeki()       # Peek minimum index (non-destructive)
val = queue.peekv()       # Peek minimum value
idx, val = queue.peekiv() # Peek both
len(queue)                # Current size
```

---

### Random (`laser.core.random`)

Framework-wide PRNG management for reproducible simulations. Seeds both NumPy and per-thread Numba PRNGs.

```python
from laser.core.random import seed, prng, get_seed

seed(42)             # Initialize PRNG (numpy + Numba per-thread)
rng = prng()         # Get global PRNG instance
s = get_seed()       # Retrieve current seed value
```

> **Note:** `Model.__init__()` automatically seeds the PRNG by searching params for `prng_seed` → `prngseed` → `seed` (in that order), defaulting to `20260101`.

---

### Distributions (`laser.core.distributions`)

Numba-compatible probability distributions. Each factory returns a callable that can be used in two ways:

```python
import laser.core.distributions as dists

# Create distribution objects
exp_dist = dists.gamma(shape=40, scale=0.25)   # Mean ~10 days
inf_dist = dists.normal(loc=8, scale=2)         # Mean 8, std 2

# Method 1: Batch sampling (fills a float32 array)
samples = dists.sample_floats(dist, offsets_array)
int_samples = dists.sample_ints(dist, offsets_array)

# Method 2: Direct call with (tick, node) — used inside Numba JIT loops
# value = dist(tick, node)  # returns a single sample
```

**Available distributions:**

| Factory | Parameters | Description |
|---------|------------|-------------|
| `beta(a, b)` | shape params | Beta distribution |
| `binomial(n, p)` | trials, probability | Binomial distribution |
| `constant_float(value)` | value | Constant float |
| `constant_int(value)` | value | Constant integer |
| `exponential(scale)` | scale (1/rate) | Exponential distribution |
| `gamma(shape, scale)` | shape, scale | Gamma distribution |
| `logistic(loc, scale)` | location, scale | Logistic distribution |
| `lognormal(mean, sigma)` | log-mean, log-std | Log-normal distribution |
| `negative_binomial(n, p)` | successes, probability | Negative binomial |
| `normal(loc, scale)` | mean, std | Normal/Gaussian |
| `poisson(lam)` | rate | Poisson distribution |
| `uniform(low, high)` | bounds | Uniform distribution |
| `weibull(a, lam)` | shape, scale | Weibull distribution |

> **v1.0.0 convention:** Inside Numba `@nb.njit` loops, distribution objects are called as `dist(tick, node)`, enabling spatially and temporally varying durations. The older `dists.sample_floats(dist, offsets)` pattern still works for batch initialization.

---

### Demographics (`laser.core.demographics`)

```python
from laser.core.demographics import AliasedDistribution, KaplanMeierEstimator, load_pyramid_csv

# Age pyramid for sampling initial ages
stable_age_dist = np.array(1000 * np.exp(-rate_const * np.arange(89)))
pyramid = AliasedDistribution(stable_age_dist)
ages = pyramid.sample(count=1000, dtype=np.int32)

# Survival estimator for mortality
survival = KaplanMeierEstimator(stable_age_dist.cumsum())
# Accepts: ndarray, list, Path, or string filename
survival.cumulative_deaths           # Original source data
survival.predict_age_at_death(ages_days, max_year=None)
survival.predict_year_of_death(ages_years, max_year=None)
survival.sample(current, max_index=None)

# Load population pyramid from CSV
pyramid_data = load_pyramid_csv(file, verbose=False)
```

---

### Migration (`laser.core.migration`)

```python
from laser.core.migration import (
    gravity, competing_destinations, stouffer, radiation,
    row_normalizer, distance
)
```

**Gravity model:**
```python
# M_{i,j} = k * p_i^a * p_j^b / d_{ij}^c
network = gravity(populations, distances, k, a, b, c)
network = row_normalizer(network, max_fraction=0.2)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `populations` | ndarray | 1D array of patch populations |
| `distances` | ndarray | 2D pairwise distance matrix (km) |
| `k` | float | Overall coupling constant (set to 1 and normalize manually for custom scaling) |
| `a` | float | Source population exponent (convention: `a=0` means source population does not affect outward flow) |
| `b` | float | Destination population exponent |
| `c` | float | Distance decay exponent |

**Alternative migration models (v1.0.0):**

| Function | Description |
|----------|-------------|
| `competing_destinations(...)` | Fotheringham (1984) competing destinations adjustment |
| `stouffer(...)` | Modified Stouffer (1940) intervening opportunities model |
| `radiation(...)` | Simini et al. (2012) radiation model based on intervening populations |

**Distance computation:**
```python
# Haversine great-circle distance
d = distance(lat1, lon1, lat2, lon2)          # Scalar
d_vec = distance(lat1, lon1, lats, lons)      # Vector
d_mat = distance(lats, lons, lats, lons)      # Matrix (pairwise)
```

---

## Generic Package: `laser.generic`

### Model

```python
from laser.generic import Model

model = Model(scenario, params, birthrates=None, name='generic',
              skip_capacity=False, states=None, additional_states=None)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `scenario` | GeoDataFrame | Per-patch data: population, S, E, I, R, geometry |
| `params` | PropertySet | Must include `nticks`, `beta` at minimum |
| `birthrates` | ndarray | Shape (nticks, nnodes) birth rate values |
| `states` | list | Custom state names (default: S, E, I, R) |
| `additional_states` | list | Supplementary state names appended to default states |

**Key attributes:**
- `model.people` - LaserFrame of agents (state, nodeid, etimer, itimer, dob, susceptibility, ...)
- `model.nodes` - LaserFrame of patches with per-tick arrays (S, E, I, R, forces, newly_infected, ...)
- `model.network` - ndarray migration coupling matrix
- `model.components` - List of component objects with `step(tick)` methods (getter/setter property)
- `model.params` - PropertySet of parameters
- `model.scenario` - Original GeoDataFrame

**Methods:**
- `model.run(name)` - Execute simulation for `params.nticks` timesteps. Calls `_initialize_flows()` then steps each component per tick.

**Automatic network setup:** If `gravity_k`, `gravity_a`, `gravity_b`, `gravity_c` are in params, the model computes a distance matrix from scenario centroids and initializes `model.network` via the gravity model automatically.

**PRNG seeding:** Searches params for `prng_seed` → `prngseed` → `seed` (in that order), defaulting to `20260101`.

---

### State Enum

```python
from laser.generic.shared import State
# or via model submodules:
from laser.generic import SEIR
SEIR.State.SUSCEPTIBLE  # etc.
```

| State | Value | Description |
|-------|-------|-------------|
| `DECEASED` | -1 | Dead agent (v1.0.0) |
| `SUSCEPTIBLE` | 0 | Susceptible to infection |
| `EXPOSED` | 1 | Infected, not yet infectious (incubating) |
| `INFECTIOUS` | 2 | Currently infectious |
| `RECOVERED` | 3 | Recovered (immune) |

All values are stored as `np.int8` for memory efficiency.

---

### Model Submodules (Shorthand Re-exports)

Each submodule re-exports the appropriate components for its model type:

```python
from laser.generic import SEIR    # S→E→I→R
from laser.generic import SEIRS   # S→E→I→R→S (waning immunity)
from laser.generic import SIR     # S→I→R
from laser.generic import SIRS    # S→I→R→S
from laser.generic import SIS     # S→I→S
from laser.generic import SI      # S→I
```

Each exposes: `Susceptible`, `Exposed` (SEIR/SEIRS only), `Infectious`, `Recovered` (where applicable), `Transmission`, `State`.

**SEIR mappings (for reference):**

| Alias | Actual class in `components.py` |
|-------|-------------------------------|
| `SEIR.Susceptible` | `Susceptible` |
| `SEIR.Exposed` | `Exposed` |
| `SEIR.Infectious` | `InfectiousIR` |
| `SEIR.Recovered` | `Recovered` |
| `SEIR.Transmission` | `TransmissionSE` |
| `SEIR.State` | `State` (from `shared.py`) |

---

### Disease Transmission Components (`laser.generic.components`)

All components follow the pattern: `__init__(self, model, ...)` + `step(tick)`.

> **Component ordering matters:** Components are executed in list order each tick. Susceptible and Recovered components must wrap the transition steps to preserve the `S + E + I + R = N` population invariant.

> **Validation:** All components accept `validating=False`. Set to `True` to enable pre/post-step consistency checks.

#### Susceptible

```python
from laser.generic.components import Susceptible

Susceptible(model, validating=False)
```

Initializes and tracks susceptible population. Propagates `S[t+1] = S[t]` each step.

#### Transmission Classes

All transmission classes accept a `seasonality` parameter (ValuesMap, ndarray, or None):

```python
# S→E (for SEIR/SEIRS models) — most common for disease modeling
TransmissionSE(model, expdurdist, expdurmin=1, seasonality=None, validating=False)

# S→I (for SIR/SIS/SIRS models)
TransmissionSI(model, infdurdist, infdurmin=1, seasonality=None, validating=False)

# S→I simple (SI model, no recovery, no duration tracking)
TransmissionSIx(model, seasonality=None, validating=False)
```

**Force of infection computation (all transmission classes):**
```python
# 1. Base FOI
ft[:] = beta * seasonality[tick] * I[tick] / N

# 2. Spatial coupling via model.network
transfer = ft[:, None] * model.network
ft += transfer.sum(axis=0)   # incoming infection pressure
ft -= transfer.sum(axis=1)   # outgoing infection pressure

# 3. Rate-to-probability conversion
ft = -np.expm1(-ft)           # p = 1 - exp(-lambda)

# 4. Stochastic Bernoulli trials per agent (Numba parallel)
```

**Seasonality parameter:**
```python
from laser.generic.utils import ValuesMap

# None → defaults to uniform 1.0 (no seasonality)
# ValuesMap → time-varying and optionally node-varying
# ndarray → used directly

# Example: 365-day seasonal profile tiled across simulation
season_tiled = np.tile(beta_season_365, nticks // 365 + 1)[:nticks]
seasonality = ValuesMap.from_timeseries(season_tiled, nnodes)
TransmissionSE(model, expdurdist, seasonality=seasonality)
```

#### Exposed

```python
Exposed(model, expdurdist, infdurdist, expdurmin=1, infdurmin=1, validating=False)
```

Manages E→I transition. Decrements `etimer` each tick; when it reaches 0, transitions agent to INFECTIOUS and assigns `itimer` via `infdurdist`.

#### Infectious Classes

```python
# Permanent recovery (SIR/SEIR)
InfectiousIR(model, infdurdist, infdurmin=1, validating=False)

# Waning immunity (SIRS/SEIRS) — also assigns waning timer
InfectiousIRS(model, infdurdist, wandurdist, infdurmin=1, wandurmin=1, validating=False)

# Return to susceptible (SIS)
InfectiousIS(model, infdurdist, infdurmin=1, validating=False)

# Permanently infectious (SI)
InfectiousSI(model, validating=False)
```

#### Recovered Classes

```python
# Permanent immunity (SIR/SEIR)
Recovered(model, validating=False)

# Waning immunity (SIRS/SEIRS) — decrements waning timer, returns to S
RecoveredRS(model, wandurdist, wandurmin=1, validating=False)
```

**Key node arrays (per tick):**
- `model.nodes.S[tick]`, `.E[tick]`, `.I[tick]`, `.R[tick]` - Compartment counts
- `model.nodes.forces[tick]` - Force of infection per patch
- `model.nodes.newly_infected[tick]` - New infections per patch
- `model.nodes.newly_infectious[tick]` - New infectious per patch

**Key people arrays:**
- `model.people.state` - Current state per agent (np.int8)
- `model.people.nodeid` - Patch assignment per agent
- `model.people.etimer` - Exposed countdown timer
- `model.people.itimer` - Infectious countdown timer
- `model.people.dob` - Date of birth (tick)
- `model.people.susceptibility` - Susceptibility modifier

---

### Vital Dynamics (`laser.generic.vitaldynamics`)

> **All rates are per 1000 population per year.** The framework divides by 1000 internally. Passing daily per-capita rates is a common silent error (see Critical Gotchas in SKILL.md).

```python
from laser.generic.vitaldynamics import (
    BirthsByCBR, MortalityByEstimator, MortalityByCDR, ConstantPopVitalDynamics
)
```

#### BirthsByCBR

```python
BirthsByCBR(model, birthrates=birthrate_array, pyramid=age_pyramid)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `model` | Model | LASER Model instance |
| `birthrates` | ndarray | Shape (nticks, nnodes), values in **per-1000/year** (typical range: 10–50) |
| `pyramid` | AliasedDistribution | Age pyramid for sampling newborn ages (usually age 0) |

**Birth formula:** Each tick, per node: `births = Poisson(N × ((1 + CBR/1000)^(1/365) - 1))` where N is the current node population.

**Capacity interaction:** `Model.__init__()` calls `calc_capacity()` using the birthrates to pre-allocate agent slots. If birthrates are wrong (e.g., daily per-capita), capacity ≈ initial population → `LaserFrame.add()` silently returns no new slots → no births occur.

**`on_birth` callback:** After adding newborns to the population, `BirthsByCBR` calls `on_birth(self, istart, iend, tick)` on every component that defines it. The `istart:iend` slice indexes the newly created agents. Use this to initialize custom properties on newborns (e.g., reachability flags, maternal immunity timers).

#### calc_capacity

```python
from laser.core.utils import calc_capacity

capacity = calc_capacity(birthrates, initial_pop, safety_factor=1.0)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `birthrates` | ndarray | Shape (nticks, nnodes), values in **per-1000/year** (asserted 0–100 internally). `nticks` is inferred from `birthrates.shape[0]`. |
| `initial_pop` | ndarray | Per-node initial populations |
| `safety_factor` | float | Multiplier on projected growth (default 1.0; use 2–4 for growing populations) |

Returns per-node estimated capacity as `np.int32` array. If population growth exceeds capacity during simulation, `LaserFrame.add()` raises `ValueError`.

#### MortalityByCDR

```python
MortalityByCDR(model, mortalityrates=deathrate_array, mappings=None, validating=False)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `model` | Model | LASER Model instance |
| `mortalityrates` | ndarray | Shape (nticks, nnodes), values in **per-1000/year** |
| `mappings` | list or None | State-to-node-property mappings for decrementing counts on death. Default maps S/E/I/R states to their node arrays. |
| `validating` | bool | Enable pre/post-step consistency checks (default: False) |

**Death probability:** Each tick, per agent: `p_death = 1 - (1 - CDR/1000)^(1/365)`. Deaths set `state = State.DECEASED.value` (-1) and decrement the appropriate node compartment count.

#### MortalityByEstimator

```python
MortalityByEstimator(model, estimator=survival_estimator)
```

Uses a `KaplanMeierEstimator` to assign age-at-death based on survival curves. Requires agents to have `dob` (date of birth) property set — this is initialized automatically by `Model.__init__()`.

#### on_birth Callback Pattern

Any component can receive notification when new agents are born by defining:

```python
def on_birth(self, istart, iend, tick):
    """Called by BirthsByCBR after newborns are added.

    Args:
        istart: Start index of new agents in model.people arrays
        iend: End index (exclusive) of new agents
        tick: Current simulation tick
    """
    # Example: initialize a custom property for newborns
    self.model.people.my_property[istart:iend] = default_value
```

This is called automatically for every component in `model.components` that has an `on_birth` method. Essential for custom components that need to set per-agent properties at birth.

---

### Utilities (`laser.generic.utils`)

```python
from laser.generic.utils import (
    ValuesMap, get_centroids, get_default_parameters,
    seed_infections_in_patch, seed_infections_randomly, validate
)
```

**ValuesMap** — Efficient values mapped over nodes and timesteps:

```python
# Uniform value across all nodes and time
vm = ValuesMap.from_scalar(value, nticks, nnodes)

# Same time series for every node
vm = ValuesMap.from_timeseries(timeseries_1d, nnodes)

# Same node data at every timestep
vm = ValuesMap.from_nodes(node_values_1d, nticks)

# Direct 2D array input (shape: nticks x nnodes)
vm = ValuesMap.from_array(array_2d)

# Access
vm.values     # ndarray shape (nticks, nnodes)
vm[tick]      # 1D array for a specific tick (used by seasonality)
vm.shape      # (nticks, nnodes)
vm.nticks     # number of ticks
vm.nnodes     # number of nodes
```

**Utility functions:**

| Function | Description |
|----------|-------------|
| `get_centroids(gdf)` | Extract geometry centroids from GeoDataFrame (EPSG:4326) |
| `get_default_parameters()` | Returns default PropertySet (nticks, beta, inf_mean, seasonality_factor, ...) |
| `seed_infections_in_patch(model, ipatch, ninfections)` | Initialize infections in a specific patch |
| `seed_infections_randomly(model, ninfections)` | Random infections across the population |
| `validate(pre, post)` | Decorator for pre/post-step validation on components |

---

### Immunization (`laser.generic.immunization`)

```python
from laser.generic.immunization import (
    ImmunizationCampaign, RoutineImmunization, immunize_in_age_window
)

# Periodic campaign across an age band
# period, coverage, age_lower, age_upper are required positional arguments
ImmunizationCampaign(model, period=365, coverage=0.9,
                     age_lower=270, age_upper=365*5,
                     start=0, end=-1, verbose=False)

# Routine immunization at specific age
RoutineImmunization(model, period=7, coverage=0.85, age=270,
                    start=0, end=-1, verbose=False)

# Direct immunization utility
immunize_in_age_window(model, lower, upper, coverage, tick)
```

Immunization sets `susceptibility[idx] = 0` for affected agents.

> **⚠ WARNING: ImmunizationCampaign and RoutineImmunization have NO effect on Transmission.**
> All Transmission kernels (`TransmissionSE`, `TransmissionSI`, `TransmissionSIx`) check
> `state == SUSCEPTIBLE` (int8 == 0) to select infection candidates. They do **not** read
> `susceptibility`. Setting `susceptibility = 0` without changing `state` leaves the agent
> fully susceptible to infection in the transmission step.
>
> **Use `RoutineImmunizationEx` instead** — it correctly sets `state = State.RECOVERED.value`
> and updates node-level `S` / `R` counts.

```python
from laser.generic.immunization import RoutineImmunizationEx
import laser.core.distributions as dists
import numba as nb

# RoutineImmunizationEx takes Numba-compiled callables, not scalar values:
#   coverage_fn:      (tick, nodeid) -> float coverage probability
#   dose_timing_dist: (seed, nodeid) -> int ticks until dose

# Example: constant 85% coverage, vaccination at age 270 days
dose_timing = dists.constant_int(270)

@nb.njit
def coverage_fn(tick, nodeid):
    return 0.85

RoutineImmunizationEx(model, coverage_fn, dose_timing,
                      dose_timing_min=1, initialize=True,
                      track=False, validating=False)
```

> **Note:** For simpler campaign-style vaccination without Numba callables, use
> `VaccinationCampaign` from `scripts/custom_components.py`.

For campaign-style vaccination with correlated missedness (modeling hard-to-reach populations), see `VaccinationCampaign` in `scripts/custom_components.py`.

---

### Infection Importation (`laser.generic.importation`)

```python
from laser.generic.importation import Infect_Random_Agents, Infect_Agents_In_Patch
```

**Infect_Random_Agents:**
```python
Infect_Random_Agents(model, verbose=False)
# Reads from model.params:
#   importation_period  - ticks between events (required)
#   importation_count   - agents infected per event (required)
#   importation_start   - start tick (default: 0)
#   importation_end     - stop tick (default: nticks)
# Triggers when: tick >= start AND (tick - start) % period == 0 AND tick < end
# Calls seed_infections_randomly(model, count)
```

**Infect_Agents_In_Patch:**
```python
Infect_Agents_In_Patch(model, verbose=False)
# Additional param:
#   importation_patchlist  - list of patch indices (default: all patches)
#   importation_count      - per patch per event (default: 1)
# Calls seed_infections_in_patch(model, patch, count) for each patch
```

> **Note:** The built-in importation classes infect random agents regardless of state. For susceptible-only importation (epidemiologically more precise), see the custom `Importation` class in `scripts/custom_components.py`.
