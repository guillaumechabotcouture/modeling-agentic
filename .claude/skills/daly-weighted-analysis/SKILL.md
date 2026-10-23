---
name: daly-weighted-analysis
description: >-
  Required when a model produces an allocation or policy recommendation:
  report DALYs averted alongside cases (or infections) averted unless absence
  is justified. Cases alone weight an infant's and an adult's averted episode
  equally and under-weight interventions targeting high-mortality groups
  (infant vaccination, chemoprevention in young children, ART for advanced
  HIV, TB treatment). Trigger phrases include "DALY", "disability-adjusted
  life-years", "cost per DALY", "DALY-averted", "GBD weight".
---

# DALY-Weighted Analysis

## Why this skill exists

In three early allocation runs (1302, 2057, 0912), the modeler reported
"cases averted" as the headline outcome metric. That is the wrong
metric for most health-allocation decisions because:

1. **Cases-averted treats all cases equal.** An episode in an infant
   (higher case-fatality, more life-years lost per death) carries far
   more public-health weight than the same episode in a young adult.
   Interventions targeted at high-mortality groups (chemoprevention or
   vaccination in young children, ART for advanced HIV, treatment of
   severe disease) look much better under DALYs than under
   cases-averted, while all-age interventions look similar under both.
   The optimal allocation differs.

2. **Funders and burden estimates use DALYs as the standard.** Global
   Fund, Gavi, WHO-CHOICE and GBD comparisons are DALY-based. Any
   allocation analysis WILL get asked "what is the cost per DALY
   averted?" If the report doesn't answer, it doesn't ship.

3. **Cost-effectiveness thresholds are DALY-based.** GiveWell's
   ~$5,000/life-saved threshold, the WHO-CHOICE 1×GDP-per-capita
   threshold for cost-effective interventions, and CEA thresholds in
   Cochrane reviews are all DALY-denominated. Cases-averted alone
   can't be compared to these references.

The Phase 6 Commit ι gate enforces: any run with `decision_rule.md`
or `*allocation*.csv` must mention DALYs in `report.md` (or
explicitly justify their absence).

## When DALYs matter (high-stakes scenarios)

- Allocation decisions with **age- or group-targeted interventions**
  (childhood vaccines, chemoprevention in young children or pregnant
  women, paediatric ARVs, TB preventive therapy for contacts)
- Allocation decisions with **mortality differentiation** between
  packages (e.g., treatment of severe disease vs prevention)
- Cost-effectiveness comparisons against **published benchmarks**
  (disease-specific CEA reviews, GiveWell, Tufts CEA Registry)
- Anything destined for **Global Fund / Gavi / PEPFAR / PMI / ministry**
  decision-makers

## When DALYs add little

- Within a single age group / interventions with similar age profile
  (e.g., comparing two product variants delivered to the same
  population at the same coverage)
- Pure prevalence or coverage outcomes without case-counting
- Rapid screening / resource-prioritization at the same stratum

In these cases the modeler MAY justify cases-averted as the headline
in a paragraph in report.md §Methods, but should still report DALYs
in a Limitations or Sensitivity section for completeness.

## Computation: the DALY formula

For each averted case, DALYs averted = YLL (years of life lost) +
YLD (years lived with disability). For an averted infection that
would have led to:

- A non-fatal clinical episode → contributes YLD only:
  ```
  YLD = duration_in_years × disability_weight
  ```

- A fatal case → contributes YLL only:
  ```
  YLL = (life_expectancy_at_age - 0)  # discounted in some conventions
  ```

For an allocation model with age-stratified cases, the typical
aggregation is:

```python
def compute_dalys_averted(cases_averted_by_age: dict, country_anchors: dict):
    """cases_averted_by_age: {"0-5m": N, "6-59m": N, "5-14y": N, "15+": N}
    country_anchors: see DISEASE_ANCHORS below."""
    total_dalys = 0.0
    for age_group, n_cases in cases_averted_by_age.items():
        a = country_anchors[age_group]
        # YLD: clinical episodes that don't become fatal
        n_clinical_nonfatal = n_cases * (1 - a["p_fatal"])
        yld = n_clinical_nonfatal * a["episode_duration_yrs"] * a["disability_weight"]
        # YLL: cases that become fatal
        n_fatal = n_cases * a["p_fatal"]
        yll = n_fatal * a["yll_per_death"]
        total_dalys += yld + yll
    return total_dalys
```

## Disease-specific anchor tables

### Malaria (P. falciparum, sub-Saharan Africa)

Source: GBD 2019 / Murray 2020 / IHME malaria estimates. Newer GBD
rounds (GBD 2021, and GBD 2023 where released) supersede these; pull
current values from the IHME GBD Results tool and cite the round used.

| Age group | p_fatal (per case) | episode_duration_yrs | disability_weight | yll_per_death |
|---|---|---|---|---|
| 0-5 mo (infants) | 0.0070 | 0.027 (10 days) | 0.211 (severe acute) | 60.0 |
| 6-59 mo (U5) | 0.0035 | 0.027 | 0.211 | 58.0 |
| 5-14 yr | 0.0008 | 0.022 (8 days) | 0.137 (moderate) | 50.0 |
| 15+ yr | 0.0003 | 0.022 | 0.069 (mild, semi-immune) | 32.0 |

**Notes:**
- `p_fatal` includes both directly-attributable and indirect
  malaria deaths. Sub-Saharan African values from IHME malaria
  estimates 2020-2022.
- `yll_per_death` is undiscounted and should come from the study
  country's life table (or the GBD reference life table, stated
  explicitly). Life expectancy at age 5 is higher than at birth where
  infant mortality is high.
- `disability_weight` from GBD 2019 disability-weights catalog:
  - Severe acute malaria (Diseases): 0.211
  - Moderate clinical: 0.137
  - Mild / semi-immune adult: 0.069

### TB (drug-susceptible, sub-Saharan Africa)

| Population | p_fatal_untreated | yll_per_death | disability_weight | duration_yrs |
|---|---|---|---|---|
| HIV-negative adult | 0.43 | 30 | 0.333 (severe pulmonary) | 0.5 |
| HIV-positive adult (no ART) | 0.78 | 24 | 0.408 (severe + HIV) | 0.5 |
| Child under 15 | 0.21 | 50 | 0.226 (moderate pulmonary) | 0.4 |

### HIV (with ART access)

| Population | p_fatal_yr | yll_per_death | disability_weight | duration_yrs |
|---|---|---|---|---|
| Adult on ART | 0.022/yr | 25 | 0.078 (chronic, asympt.) | lifetime |
| Adult off ART | 0.31/yr | 20 | 0.582 (AIDS) | up to 3-5 yrs |
| Pediatric (PMTCT failure) | 0.20/yr | 60 | 0.333 (severe pediatric) | up to 2 yrs |

### Measles

| Age group | p_fatal | yll_per_death | disability_weight | duration_yrs |
|---|---|---|---|---|
| 0-11 mo | 0.025 (LMICs) | 60 | 0.133 (moderate) | 0.025 |
| 1-4 yr | 0.012 | 58 | 0.133 | 0.025 |
| 5-14 yr | 0.005 | 50 | 0.067 (mild) | 0.025 |
| 15+ yr | 0.002 | 32 | 0.067 | 0.025 |

## Required output for Phase 6 Commit ι

When a model produces an allocation, the report MUST include AT
LEAST ONE of:

1. **DALY-averted column alongside cases-averted column** in the
   primary results table (preferred, e.g., Table 7 / Table 10):
   ```
   | Package | Units | Cases Averted | DALYs Averted | $/Case | $/DALY |
   ```

2. **A separate §Cost-Effectiveness section** with DALY computation
   methodology and per-package cost-per-DALY estimates.

3. **A justification paragraph** in §Methods or §Limitations
   explaining why DALYs are not relevant for this analysis (must
   address: are interventions age-targeted? do packages differ in
   mortality risk? is the audience a funder or ministry that ranks
   by cost per DALY?).

The validator regex looks for `\b(DALY|disability-adjusted)` in
report.md. If absent, fires `daly_analysis_missing` MEDIUM.

## Worked example: national malaria allocation

For the 2057/0912 national malaria setup (PBO+SMC dominant in 218
districts; SMC eligible only in the Sahelian zones), the DALY-averted
shift typically looks like:

| Package | Cases averted/yr | U5 cases share | DALYs averted/yr | $/DALY |
|---|---|---|---|---|
| Standard LLIN | 311K | 18% | 22.7K | $1,960 |
| PBO LLIN | 685K | 18% | 50.0K | $1,430 |
| SMC only | 220K | **100%** (U5-only) | **64.6K** | $510 |
| PBO + SMC | 870K | 36% blended | 122K | $730 |
| Dual-AI | 256K | 18% | 18.7K | $1,890 |

**SMC cost-per-DALY ($510) is far better than PBO ($1,430)** —
3× advantage that's invisible under cases-averted ($150/case for
SMC vs $80/case for PBO would suggest the opposite ranking).

This is the kind of recommendation shift that program planners depend
on. Without DALY weighting, the model is structurally biased toward
all-age interventions over child-targeted ones. Note that the SMC row
is only valid if both its cases averted and its cost are computed over
the SAME eligible population (children in the eligible age band in
eligible zones); see the `eligible-population-consistency` skill.

## Common pitfalls

1. **Using global or regional GBD anchors for a specific country.**
   Case-fatality and life expectancy vary widely between countries;
   use country-specific values where available (IHME GHDx country
   profiles, national vital registration). Document the source.

2. **Forgetting age-stratified case attribution.** If your model
   doesn't track cases by age group, you cannot compute proper
   DALYs without an assumption about the U5 share. Document the
   assumption.

3. **YLL discounting choices.** GBD (since GBD 2010) reports DALYs
   with no discounting and no age weighting. Many economic-evaluation
   guidelines (e.g., WHO-CHOICE, the Gates Reference Case) discount
   health outcomes at 3% per year. Document which convention you use;
   it can shift cost per DALY by about 2× for deaths in young children.

4. **Disability weights vs life expectancy table mismatch.**
   Disability weights are GBD-uniform; life expectancy comes from
   country tables. Use them consistently (don't mix GBD weights
   with WHO regional life tables, etc.).
