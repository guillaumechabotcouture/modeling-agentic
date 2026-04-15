---
name: malaria-modeling
description: Disease-specific modeling guide for P. falciparum malaria.
  Covers transmission dynamics, intervention mechanisms, known modeling
  pitfalls, calibration targets, cost-effectiveness benchmarks, and
  published benchmark models. Use when the research question involves
  malaria incidence, prevalence, transmission, interventions, or resource
  allocation. Trigger phrases include "malaria", "PfPR", "ITN", "bednet",
  "IRS", "indoor residual spraying", "SMC", "seasonal malaria
  chemoprevention", "MDA", "mass drug administration", "Global Fund
  malaria", "malaria resource allocation", "malaria optimization".
---

# Malaria (P. falciparum) Modeling Guide

## 1. Key Parameters

| Parameter | Value/Range | Notes |
|-----------|------------|-------|
| R0 | 2-100+ | Transmission-intensity dependent; USE EIR as primary metric, not R0 |
| EIR (Entomological Inoculation Rate) | 0.1-1000 ib/p/yr | Primary transmission metric; varies 100-fold across settings |
| Latent period (human) | 10-14 days | Liver-stage development |
| Infectious period (symptomatic) | 7-14 days (treated) | With ACT treatment |
| Infectious period (asymptomatic) | 60-200+ days | Chronic low-density infections; major reservoir |
| Immunity | Partial, age-dependent | Requires sustained exposure to develop and maintain |
| Transmission | Vector-borne (Anopheles spp.) | Seasonal: rainfall-driven with 1-2 month lag for mosquito lifecycle |
| Symptomatic:Asymptomatic ratio | ~1:2 to 1:4 | Higher asymptomatic fraction in high-transmission areas |
| Case fatality rate (under-5) | 0.1-0.5% of clinical cases | Higher in low-access settings |
| Case fatality rate (5+) | 0.01-0.1% of clinical cases | Lower due to acquired immunity |

---

## 2. Critical Modeling Pitfalls

**READ THIS SECTION BEFORE BUILDING ANY MALARIA MODEL.** These are common
errors that produce models with correct-looking calibration but fundamentally
wrong intervention effects and optimization results.

### Pitfall 1: PfPR-EIR Saturation

The PfPR-EIR relationship is nonlinear and saturating. At EIR > 100,
PfPR plateaus around 70-80% due to superinfection and acquired immunity.
An equilibrium formula without immunity saturation will force EIR to
absurd values (1000+) to match observed PfPR in holoendemic areas,
making the model insensitive to interventions in exactly the areas that
need them most.

**What goes wrong:** The model calibrates to observed PfPR by inflating
EIR far beyond published ranges. Then interventions that reduce EIR
(ITN, IRS) have negligible effect on PfPR because the model is on the
flat part of the saturation curve. The optimizer sees zero marginal
benefit and allocates nothing to the highest-burden areas.

**Solution:** Use an empirical PfPR-EIR relationship that saturates
correctly:
- Smith et al. 2005 (Malaria Journal): Hill function fit to field data
- Beier et al. 1999: logarithmic relationship from 31 African sites
- Griffin et al. 2010: mechanistic immunity acquisition model

**Diagnostic:** If any calibrated EIR exceeds 200 ib/p/yr, or if a
calibrated parameter hits an optimizer bound, the model structure is
inadequate for that setting. STOP and fix before proceeding.

### Pitfall 2: Equilibrium Models Destroy Time-Limited Intervention Effects

SMC provides 4-month seasonal chemoprophylaxis to children 3-59 months.
MDA provides a transient reduction in parasite prevalence. Both have
effects concentrated in specific time windows.

**What goes wrong:** An equilibrium model averages the seasonal effect
over 12 months, diluting a 73% reduction during 4 months into a ~20%
annual average. This makes SMC look 3-4x less effective than it actually
is. MDA's transient effect disappears entirely in equilibrium.

**Rule:** If ANY intervention has time-limited action (seasonal
chemoprophylaxis, campaign-based MDA, seasonal IRS rounds), use a
DYNAMIC model (ODE with seasonal time steps, difference equations, or
agent-based), NOT algebraic equilibrium. This is a structural
requirement, not a refinement.

### Pitfall 3: Age Structure Is Required for SMC Evaluation

SMC targets children 3-59 months ONLY, which is ~15-17% of the
population. Without age structure, the model cannot:
- Compute SMC's direct impact on the target population
- Correctly represent the age distribution of malaria burden
- Distinguish between interventions that protect everyone (ITN) vs
  those that protect a specific age group (SMC)

**Rule:** A model without age structure CANNOT evaluate SMC and should
not include SMC as an optimization variable. Minimum age groups for
SMC evaluation: 0-5y, 5-14y, 15+. Preferred: 0-5m, 6-59m, 5-14y, 15+.

### Pitfall 4: ITN Effectiveness Is Context-Dependent

Pyrethroid resistance modifies ITN efficacy substantially. The relevant
parameter depends on:
- Net type: standard LLIN (pyrethroid-only), PBO-pyrethroid, dual-AI
  (chlorfenapyr + pyrethroid)
- Vector species composition: An. gambiae s.s. vs An. arabiensis vs
  An. funestus (different insecticide susceptibility)
- Resistance intensity: metabolic vs target-site resistance

**If the policy question involves net type selection or resistance
scenarios:** model at least 2 net types with different efficacy profiles.

**Key reference:** Protopopoff et al. 2018 (Lancet): PBO nets achieved
44% lower malaria prevalence than standard LLINs after 2 years in a
pyrethroid-resistance setting.

### Pitfall 5: IRS Has Threshold and Complementarity Effects

IRS efficacy is coverage-dependent due to community-level protection:
the higher the coverage, the lower the overall vector density, benefiting
even uncovered households.

**What goes wrong:** Setting IRS efficacy to zero below a coverage
threshold (e.g., <40%) creates a cliff effect. A greedy optimizer sees
zero marginal return from any IRS increment below the threshold, making
it prohibitively expensive to reach the threshold. IRS then appears
useless regardless of its true effectiveness.

**Solution:** Use a continuous dose-response for IRS efficacy, not a
step function. Efficacy should increase smoothly with coverage.

**Also critical:** IRS complements ITN, especially in pyrethroid
resistance settings (Pryce et al. 2022 Cochrane, CD012688.pub3). A
model that reduces ITN efficacy for resistance but doesn't increase the
relative value of non-pyrethroid IRS is missing the whole point of IRS
in resistance contexts.

### Pitfall 6: Published Benchmark Models MUST Be Compared Against

For Nigeria specifically:
- **EMOD** (IDM/Northwestern): Ozodiegwu et al. 2023 -- 774-LGA
  model with archetype calibration, directly informed GC7 request
- **Optima Malaria** (Burnet): Scott et al. 2017 -- allocative
  efficiency optimization across 6 geopolitical zones
- **malariasimulation** (Imperial College): Griffin et al. 2010+
  individual-based with immunity dynamics
- **OpenMalaria** (Swiss TPH): Smith et al. -- detailed within-host
  dynamics, community-level effects

For any Global Fund or policy submission, the model must explain how
and why its results differ from these established tools. **If the
model's allocation recommendation contradicts all published models,
the model is almost certainly wrong -- not innovative.**

---

## 3. Cost-Effectiveness Benchmarks

Published ranges for malaria interventions in Sub-Saharan Africa.
These are MANDATORY checks for any optimization model.

| Intervention | $/DALY averted | $/case averted | Source |
|-------------|---------------|----------------|--------|
| ITN/LLIN | $5-27 | $2-12 | Conteh et al. 2021 (PMC8324482) |
| IRS | $12-100 | $5-50 | Conteh et al. 2021 |
| SMC | $25-183 | $1-5 | Awosolu et al. 2024 |
| Case management (ACT) | $4-29 | $3-10 | Conteh et al. 2021 |
| IPTp (pregnant women) | $2-11 | $1-5 | Conteh et al. 2021 |

### Mandatory Check

**IF THE MODEL PRODUCES COST-EFFECTIVENESS >5x OUTSIDE THESE RANGES
FOR ANY INTERVENTION, THE MODEL STRUCTURE IS WRONG.**

This is NOT a parameter calibration issue. It means the model's
representation of that intervention's mechanism is inadequate. Do not
proceed to optimization -- fix the intervention mechanism first.

Common causes of out-of-range cost-effectiveness:
- Equilibrium averaging of seasonal interventions (SMC, seasonal IRS)
- Missing age structure for age-targeted interventions (SMC)
- Step-function coverage thresholds creating optimizer cliff effects (IRS)
- PfPR-EIR saturation making interventions ineffective at high transmission

---

## 4. Model Structure for Malaria

### Minimum Viable Model for Resource Allocation

| Component | Minimum | Preferred |
|-----------|---------|-----------|
| Compartments | S-E-I-R with clinical/asymptomatic split | S-E-A/D-T-R (Griffin-style) |
| Age groups | 3: 0-5y, 5-14y, 15+ | 4: 0-5m, 6-59m, 5-14y, 15+ |
| Temporal | Dynamic ODE/difference equation | Monthly or weekly time steps |
| Seasonal forcing | Rainfall-driven, 1-2 month lag | Fourier harmonics fit to ERA5 |
| Immunity | Must saturate PfPR at high EIR | Explicit superinfection model |
| Geographic | 6 zones (Nigeria) | 36+1 states or 774 LGAs |

### Intervention Mechanisms

- **ITN:** Reduce vector-human contact rate (force of infection reduction).
  Effect proportional to coverage x efficacy. Waning: 2-3 year half-life
  for pyrethroid LLINs.

- **IRS:** Reduce vector density and survival. Continuous dose-response
  with coverage (NOT step function). Effect strongest in first 3-6 months
  post-spraying, requiring annual or biannual rounds.

- **SMC:** Seasonal chemoprophylaxis for children 3-59 months during
  peak transmission (typically 4 months). Reduce FOI for target age group
  during active window. Must be modeled with explicit seasonality and
  age structure -- cannot be represented in equilibrium.

- **Treatment/case management:** Reduce infectious duration (faster
  clearance with ACT) and prevent mortality. Coverage varies by
  treatment-seeking behavior and health system access.

### What NOT to Do

- Do NOT use algebraic equilibrium for models with seasonal interventions
- Do NOT average SMC effect across 12 months
- Do NOT use step-function efficacy thresholds for IRS
- Do NOT use a single national R0 when sub-national variation is >5-fold
- Do NOT model ITN and IRS effects as independent when they target the
  same vector population (they are sub-additive at best)
- Do NOT hand-code malaria transmission from scratch — clone and adapt
  a published implementation (see below)

### CRITICAL: Use Existing Published Code

Simple SIR/SEIR/SEDATU ODEs CANNOT sustain PfPR above ~30% because
they deplete susceptibles without modeling superinfection and acquired
immunity. This is a fundamental limitation, not a parameter tuning
issue. The Hill function "solves" calibration by abandoning dynamics
entirely, but then intervention effects are just static OR multipliers
— not mechanistic.

**You MUST start from an existing published malaria model implementation:**

1. **Griffin et al. deterministic model** (PREFERRED starting point):
   `git clone --depth 1 https://github.com/mrc-ide/deterministic-malaria-model`
   - R/C code, MIT license, fully open
   - Has: age-structured immunity (clinical + anti-parasite), superinfection,
     ITN/IRS intervention effects, seasonal forcing, 34-site parameterization
   - Translate the core ODE system to Python, keeping the immunity equations
   - This model can sustain PfPR >70% at high EIR because immunity limits
     disease, not infection

2. **malariasimulation** (R package, Imperial College):
   `git clone --depth 1 https://github.com/mrc-ide/malariasimulation`
   - Individual-based version of Griffin et al., R, MIT license
   - More complex but same immunity dynamics
   - Consider if you need individual-level tracking

3. **HBHI Nigeria archetype code** (for Nigeria-specific work):
   `git clone --depth 1 https://github.com/numalariamodeling/hbhi-nigeria-publication-2021`
   - R/Python, Apache-2.0, the actual code behind Ozodiegwu et al. 2023
   - 774-LGA archetype assignments, calibration targets, scenario configs
   - Requires EMOD binary for simulation, but archetype/data code is reusable

4. **OpenMalaria** (Swiss TPH):
   `git clone --depth 1 https://github.com/SwissTPH/openmalaria`
   - C++, GPL, detailed within-host dynamics
   - More complex than needed for allocation models

**Workflow:** Clone Griffin deterministic model → read the ODE system
(especially the immunity equations in `odin_model.R`) → translate to
Python with scipy.integrate.solve_ivp → calibrate to your targets →
add intervention mechanisms → optimize.

This takes less time than building from scratch and produces a model
that actually works at high transmission intensity.

---

## 5. Intervention Effect Validation Ranges

BEFORE running optimization, verify the model produces plausible
marginal intervention effects. Print a table:

| Area | Intervention | Coverage | Baseline PfPR | With intervention | Reduction | Expected range |
|------|-------------|----------|---------------|-------------------|-----------|----------------|

### Expected Ranges (from Cochrane/systematic reviews)

| Intervention | Coverage | Expected effect | Source |
|-------------|----------|----------------|--------|
| ITN | 80% | 15-30% PfPR reduction (moderate), 5-15% (holoendemic) | Lengeler 2004 Cochrane |
| IRS | 80% | 10-30% PfPR reduction (moderate), 5-15% (high) | Pluess et al. 2010 Cochrane |
| SMC | 80% | 70-80% clinical episode reduction in target age group, during season | ACCESS-SMC 2020 |
| ITN+IRS | 80% each | Sub-additive: less than sum of individual effects | Pryce et al. 2022 Cochrane |

### STOP Conditions

If any of these are true, STOP and report to the lead agent. Do NOT
proceed to optimization with wrong intervention effects:

- ITN at 80% reduces PfPR by <1% in any zone (implausible)
- SMC at 80% averts <10% of cases in target age group during season
- IRS has zero effect at any coverage level (likely step-function artifact)
- Any intervention's cost-effectiveness is >5x outside published ranges
- A calibrated EIR exceeds 200 ib/p/yr or hits an optimizer bound

---

## 6. Nigeria-Specific Context

### Epidemiology
- National PfPR (microscopy, 6-59m): 22.6% (NMIS 2021)
- Zone range: 14.2% (South West) to 33.5% (North West), 2.4-fold variation
- WHO 2023 estimates: 68M cases, 184,800 deaths, 299/1000 incidence
- Dominant vectors: An. gambiae s.s. (indoor), An. arabiensis (outdoor)
- Pyrethroid resistance: widespread, especially North West and North Central

### Geographic Units
- 6 geopolitical zones: NW, NE, NC, SW, SE, SS
- 36 states + FCT (Federal Capital Territory)
- 774 Local Government Areas (LGAs)
- Zone-level is minimum for resource allocation; state-level preferred

### Key Policy Context
- Global Fund GC7 (2024-2026): ~$993M total (HIV+TB+malaria); malaria
  component estimated ~$320M (not publicly disaggregated)
- Nigeria's GC7 request includes SMC expansion to 404 LGAs (from ~80)
- WHO 2024 guidelines recommend PBO/dual-AI nets for resistance areas
- NW zone: highest burden, ~25% of population. ANY credible resource
  allocation should invest in NW.

### Calibration Targets
| Target | Value | Source | Type |
|--------|-------|--------|------|
| Zone PfPR (6 zones) | 14.2-33.5% | NMIS 2021 (MIS41) | Primary calibration |
| National PfPR | 22.6% (microscopy) | NMIS 2021 | Validation |
| Case incidence | 299/1000 | WHO WMR 2024 | Validation |
| Total deaths | 184,800 | WHO WMR 2024 | Validation |
| Mean EIR | 13.6 ib/p/yr | Awolola et al. 2009 | Validation (range check) |
| ITN usage (under-5) | 41% national | NMIS 2021 | Baseline coverage |
| R0 estimate | ~2.24 | Amadi et al. 2022 | Validation (order of magnitude) |

### Published Allocation Results (for comparison)
| Study | Budget | Key finding |
|-------|--------|-------------|
| Scott et al. 2017 (Optima) | ~$175M/yr | Optimized: 84,000 deaths avertable/5yr; prioritize LLINs, IPTp, SMC |
| Ozodiegwu et al. 2023 (EMOD) | N/A | NMSP at 80%+ with SMC expansion achieves greatest impact |
| Bhatt et al. 2015 | N/A | ITNs responsible for 68% of 50% prevalence reduction 2000-2015 |

**If your model contradicts all three of these studies, investigate
your model first, not the literature.**

---

## 7. Key References

| Citation | What it provides |
|----------|-----------------|
| Griffin et al. 2010 | Foundational individual-based transmission model with immunity |
| Smith et al. 2005 | Empirical PfPR-EIR relationship (Hill function) |
| Beier et al. 1999 | Logarithmic PfPR-EIR from 31 African sites |
| Ozodiegwu et al. 2023 | EMOD Nigeria 774-LGA model (GC7 reference) |
| Scott et al. 2017 | Optima Nigeria allocative efficiency |
| Lengeler 2004 | Cochrane review: ITN effectiveness |
| Pluess et al. 2010 | Cochrane review: IRS effectiveness |
| Pryce et al. 2022 | Cochrane review: IRS+ITN combination |
| Thwing et al. 2024 | SMC meta-analysis: RR=0.27 clinical, 0.38 parasitemia |
| ACCESS-SMC 2020 | SMC at scale: 88.2% protective effectiveness |
| Yang et al. 2018 | ITN meta-regression: OR=0.44 |
| Zhou et al. 2022 | IRS meta-analysis: OR=0.35 pooled, 0.27 at >=80% coverage |
| Conteh et al. 2021 | Cost-effectiveness systematic review |
| Awosolu et al. 2024 | SMC cost systematic review: $25-183/DALY |
| Protopopoff et al. 2018 | PBO nets: 44% lower prevalence vs standard LLINs |

---

## 8. Handoff to Downstream Skills

When building malaria models, also use:
- **epi-model-parametrization**: For structured parameter spaces and calibration target design
- **laser-spatial-disease-modeling**: If using LASER framework for spatial ABM
- **modelops-calabaria**: For Bayesian calibration and cloud-scale optimization
- **model-validation**: For validation gates before accepting optimization results
- **model-fitness**: For evaluating whether model is fit for its stated purpose
