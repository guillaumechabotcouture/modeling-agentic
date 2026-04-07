---
name: epi-model-parametrization
description: Guide for researching epidemiological parameters, identifying
  calibration targets, and determining model structure for spatial disease
  transmission models. Use when the user needs to find R0, infectious/latent
  periods, birth/death rates, vaccination coverage, observed data for
  calibration, or guidance on model component selection (compartment type,
  seasonal forcing, spatial coupling, vaccination strategy). Trigger phrases
  include "research parameters", "what parameters do I need", "find R0 for",
  "epidemiological data sources", "disease parameters", "parametrize model",
  "calibration targets", "model structure for", "parameter space",
  "what data do I need".
---

# Epidemiological Model Parametrization Guide

## Three Outputs

This skill produces three structured outputs for downstream use:

1. **Parameter Space** → calabaria `ParameterSpace` + `ConfigurationSpace`
   - Uncertain parameters with ranges (for calibration)
   - Known parameters with point estimates (fixed)

2. **Calibration Targets** → observed data + loss function design
   - What observable data exists for this disease/region?
   - How to structure it as a calabaria loss function
   - Alignment strategy (temporal, spatial, age-stratified)

3. **Model Structure Guidance** → LASER component selection
   - Which compartment model? (SEIR, SIR, SEIRS, etc.)
   - Which seasonal forcing? (school-term, monsoon, none)
   - Which spatial coupling? (gravity, radiation, none)
   - Which vaccination? (routine, campaign, both, age bands)
   - Which importation? (endemic seeding, stochastic, none)

---

## Output 1: Parameter Space

### Parameter Checklist by Model Type

#### SEIR (Susceptible-Exposed-Infectious-Recovered)
- **Transmission**: R0 (or beta), generation interval
- **Natural history**: Latent period (days), infectious period (days)
- **Case detection**: Infection-to-case ratio (asymptomatics per case)
- **Demographics**: CBR (per 1000/yr), CDR, population pyramid
- **Spatial**: Per-patch populations, coordinates, distance matrix
- **Vaccination**: Routine coverage by age, campaign coverage, frequency
- **Seasonal forcing**: Type (climate/behavioral), peak timing, amplitude
- **Initial conditions**: Fraction immune (R), seeding strategy (I)

#### SIR (no latent period)
- Same as SEIR minus latent period parameters
- Generation interval ≈ infectious period (no pre-infectious delay)

#### SIRS / SEIRS (waning immunity)
- Same as SIR/SEIR plus:
- **Waning rate**: Average duration of immunity (months/years)
- **Boosting**: Whether re-exposure extends immunity

#### SIS (no lasting immunity)
- Transmission and infectious period only
- No recovered compartment; suitable for bacterial STIs, some helminths

### Parameter Validation Ranges

| Parameter | Typical Range | Red Flags |
|-----------|--------------|-----------|
| R0 | 1.5–18 depending on disease | <1 means no sustained transmission |
| Latent period | 2–21 days | >30 days unusual for acute infections |
| Infectious period | 3–14 days | >30 days → chronic, different model needed |
| CBR | 8–50 per 1000/yr | <1 or >60 likely unit error |
| CDR | 3–20 per 1000/yr | Same unit check as CBR |
| Vaccination coverage | 0.0–1.0 | >1.0 is a unit error (not percentage) |
| Generation interval | 5–25 days | Should ≈ latent + infectious/2 for SEIR |
| Infection-to-case ratio | 1:1–1:2000 | Disease-specific; >1:100 common for enteric pathogens |

### Output Format

Structure the research as calabaria-compatible parameter spaces:

```python
from calabaria.parameters import ParameterSpace, ParameterSpec
from calabaria.parameters import ConfigurationSpace, ConfigSpec

# Calibration parameters (uncertain → ranges for Optuna)
PARAMS = ParameterSpace([
    ParameterSpec("beta", lower=<lower>, upper=<upper>, kind="float",
                  doc="<source: Author et al. YYYY, range justification>"),
    ParameterSpec("gravity_k", lower=<lower>, upper=<upper>, kind="float",
                  doc="<source>"),
    # ... additional uncertain parameters
])

# Fixed parameters (well-known → point estimates)
CONFIG = ConfigurationSpace([
    ConfigSpec("latent_period_mean", default=<value>,
               doc="<source: WHO/PubMed systematic review>"),
    ConfigSpec("infectious_period_mean", default=<value>,
               doc="<source>"),
    ConfigSpec("cbr", default=<value>,
               doc="<source: UN World Population Prospects YYYY>"),
    # ... additional fixed parameters
])
```

**Key principle:** A parameter is uncertain (PARAMS) if the literature shows a wide range or strong context-dependence. It is fixed (CONFIG) if there is consensus from multiple systematic reviews.

---

## Output 2: Calibration Targets

### What to Calibrate Against

The calibration target is the **observed data** you want your model to reproduce. Finding the right target is as important as finding the right parameters.

#### Target Types by Data Availability

| Data Type | Example | Loss Function | Alignment |
|-----------|---------|--------------|-----------|
| **Case incidence time series** | Weekly reported cases by district | MSE on log-transformed counts | Temporal (week) × spatial (district) |
| **Annual/total case counts** | Total cases per province per year | Poisson or negative binomial likelihood | Spatial × annual |
| **Seroprevalence** | Fraction immune by age group | Beta-binomial likelihood | Age × spatial |
| **Extinction/persistence** | Fraction of weeks with zero cases | Logistic curve fit (CCS similarity) | Spatial (by population size) |
| **Spatial spread patterns** | Phase lags in wavelet analysis | Phase difference similarity | Spatial (distance from epicenter) |
| **Vaccination impact** | Pre/post-campaign case reduction | Relative reduction ratio | Temporal (before/after) |

#### Data Sources for Calibration Targets

| Source | What It Provides | Format | URL/Access |
|--------|------------------|--------|------------|
| WHO GHO | National incidence, mortality by year | CSV/API | gho.who.int |
| GPEI / POLIS | Subnational polio case counts, AFP surveillance | CSV | polioeradication.org |
| DHS | Seroprevalence, vaccination coverage surveys | Stata/CSV | dhsprogram.com |
| UN WPP | Population, CBR, CDR, age pyramids by country | CSV/Excel | population.un.org |
| GBD / IHME | Disease burden estimates by country/year | CSV | ghdx.healthdata.org |
| Ministry of Health reports | Subnational case data, outbreak reports | PDF/tables | Country-specific |
| Published studies | Age-stratified seroprevalence, outbreak curves | Extract from papers | PubMed |

#### Structuring for calabaria

```python
import polars as pl

# Observed data as polars DataFrame
# Schema should match what your @model_output methods produce
observed = pl.DataFrame({
    "year": [2015, 2015, 2016, 2016, ...],
    "patch": [0, 1, 0, 1, ...],
    "cases": [45, 12, 38, 8, ...],
})

# Loss function for calibration loop
def compute_loss(model_output_df: pl.DataFrame, observed_df: pl.DataFrame) -> float:
    """Compare model weekly_incidence output to observed data.

    Returns single float loss for TrialResult.
    """
    joined = model_output_df.join(observed_df, on=["year", "patch"], suffix="_obs")
    log_model = (joined["cases"] + 1).log()
    log_obs = (joined["cases_obs"] + 1).log()
    return ((log_model - log_obs) ** 2).mean()
```

#### Accounting for Under-Ascertainment

Most surveillance data captures only a fraction of true infections:

| Disease | Typical Detection Ratio | Source |
|---------|------------------------|--------|
| Measles | 1:3–1:10 (varies by surveillance quality) | WHO |
| Polio (WPV1) | 1:200 (paralysis:infection) | CDC |
| Cholera | 1:4–1:25 | WHO |
| Influenza | 1:10–1:100 | CDC |

**Approach:** Either multiply observed cases by the inverse ratio to estimate true incidence, or multiply model infections by the detection ratio to estimate reported cases. Document which direction you chose.

---

## Output 3: Model Structure Guidance

### Decision Tree for Component Selection

Research the disease biology and transmission ecology to answer these questions:

#### 1. Compartment Model

```
Does infection confer lasting immunity?
├── Yes
│   ├── Is there a latent (non-infectious) period?
│   │   ├── Yes → SEIR
│   │   └── No  → SIR
│   └── Does immunity wane on simulation timescale?
│       ├── Yes → SEIRS or SIRS
│       └── No  → SEIR or SIR
└── No → SIS
```

#### 2. Seasonal Forcing

| Driver | Profile Shape | Examples |
|--------|--------------|----------|
| **School terms** | Biweekly step function (Bjornstad) | Measles, influenza (temperate) |
| **Climate/monsoon** | Cosine peaking in wet/warm season | Cholera, dengue, malaria |
| **Behavioral** | Holiday/pilgrimage calendar | Meningitis belt (dry season gatherings) |
| **None apparent** | Flat (`ValuesMap.from_scalar(1.0, ...)`) | Some chronic infections |
| **Unknown** | Include `seasonal_amplitude` in PARAMS | Let calibration decide |

#### 3. Spatial Coupling

| Model | When to Use | LASER Function |
|-------|-------------|----------------|
| **Gravity** | Default for human diseases; well-studied | `gravity()` |
| **Radiation** | When intervening populations create barriers | `radiation()` |
| **Competing destinations** | When destinations compete for travelers | `competing_destinations()` |
| **None** | Single-patch or well-mixed | Set `gravity_k = 0` |

#### 4. Vaccination Strategy

| Strategy | Component | Key Parameters |
|----------|-----------|----------------|
| **Routine only** | `RoutineImmunizationEx` | Age at vaccination, coverage |
| **Campaign only** | `VaccinationCampaign` | Period, coverage, age band |
| **Both** | Both components | Consider correlated missedness |
| **None** | Omit vaccination components | Pre-vaccine era modeling |

**Correlated missedness:** If hard-to-reach populations consistently miss both routine and campaign vaccination, use the `reachable` flag pattern from `custom_components.py`.

#### 5. Importation / Seeding

| Pattern | When to Use | Configuration |
|---------|-------------|---------------|
| **Endemic corridor** | Continuous cross-border transmission | `end_tick = nticks`, moderate `count` |
| **Stochastic reintroduction** | Sustain sub-CCS patches | Moderate `period`, low `count` |
| **Initial seeding only** | No ongoing importation | Seed I > 0 in scenario GeoDataFrame only |
| **None** | Fully closed system | Omit importation component |

---

## Research Workflow

### Step 1: Scope the Problem

Define the modeling question before researching parameters:
- **Disease**: What pathogen? Acute or chronic? Vaccine-preventable?
- **Geographic region**: Country/subnational? Number of spatial patches?
- **Time period**: Historical (calibration) or future (projection)?
- **Spatial resolution**: Districts, provinces, or grid cells?
- **Key question**: Eradication feasibility? Vaccination strategy? Outbreak risk?

### Step 2: Research Parameters (Output 1)

Search strategy by parameter type:

| Parameter Type | Primary Sources | Search Terms |
|---------------|----------------|--------------|
| **R0, generation interval** | PubMed systematic reviews | "{disease} R0 systematic review" |
| **Latent/infectious period** | WHO disease factsheets, PubMed | "{disease} incubation period", "{disease} serial interval" |
| **Demographics (CBR, CDR)** | UN World Population Prospects | "{country} crude birth rate" |
| **Vaccination coverage** | DHS, MICS, WHO/UNICEF estimates | "{country} {disease} vaccine coverage subnational" |
| **Population by patch** | National census, WorldPop | "{country} district population" |
| **Seasonal forcing** | Published modeling studies | "{disease} seasonal transmission {region}" |

**Cross-reference rule:** Every parameter should have at least two independent sources. Flag parameters with only one source or wide disagreement as calibration candidates.

### Step 3: Identify Calibration Targets (Output 2)

- What observed data exists at the required spatial/temporal resolution?
- Is it reported cases (under-ascertained) or true incidence (seroprevalence)?
- What is the case detection ratio / infection-to-case ratio?
- Can you match model output temporal resolution to observed data?
- Structure observed data as polars DataFrame matching model output schema

### Step 4: Determine Model Structure (Output 3)

- Use the decision tree above for each component choice
- Document each choice with literature justification
- This directly maps to LASER component selection in the `laser-spatial-disease-modeling` skill

### Step 5: Validate and Document

**Unit checks (critical):**
- Birth/death rates: per-1000/year (typical CBR: 10-50, CDR: 3-20)
- Durations: days (latent: 2-21, infectious: 3-14 for acute infections)
- Coverages: fraction 0.0-1.0 (not percentage)
- Populations: absolute counts (not thousands or millions)

**Documentation:**
- Record all sources in `ParameterSpec.doc` and `ConfigSpec.doc` fields
- Create a summary table:

| Parameter | Value/Range | Unit | Source | Uncertainty |
|-----------|------------|------|--------|-------------|
| R0 | 12-18 | dimensionless | Systematic review (Author YYYY) | Calibrate |
| Latent period | 8-13 days | days | WHO factsheet | Fixed at 10 |
| CBR | 29.2 | per 1000/yr | UN WPP 2024 | Fixed |
| ... | ... | ... | ... | ... |

---

## Disease-Specific Quick References

### Measles
- R0: 12-18 | Latent: 8-13d | Infectious: 6-7d | Case ratio: 1:3-1:10
- Seasonal: School-term (Bjornstad profile) | CCS: ~300K-500K
- Vaccination: Routine at 9mo + campaign | Immunity: Lifelong

### Polio (WPV1)
- R0: 5-7 (sanitation-dependent) | Latent: 3-6d | Infectious: 14-28d (fecal) | Case ratio: 1:200 (paralysis)
- Seasonal: Monsoon/warm season | CCS: Low (fecal-oral persistence)
- Vaccination: OPV campaigns + routine IPV | Immunity: Type-specific, lifelong

### Cholera
- R0: 1.5-4 | Latent: 1-5d | Infectious: 3-7d | Case ratio: 1:4-1:25
- Seasonal: Monsoon/flooding | No CCS concept (environmental reservoir)
- Vaccination: OCV campaigns | Immunity: Wanes ~3-5 years → SEIRS

### Influenza (Seasonal)
- R0: 1.2-2.0 | Latent: 1-2d | Infectious: 3-7d | Case ratio: 1:10-1:100
- Seasonal: Winter (temperate), year-round (tropical) | CCS: Very low
- Vaccination: Annual campaign | Immunity: Strain-specific, wanes ~1yr → SIRS

---

## Handoff to Downstream Skills

When parametrization is complete, hand off to:

1. **`laser-spatial-disease-modeling`**: Use Output 3 (model structure) to select LASER components, and Output 1 (parameter space) to configure the model
2. **`modelops-calabaria`**: Use Output 1 (ParameterSpace/ConfigurationSpace) directly as BaseModel class attributes, and Output 2 (calibration targets) to design the loss function
