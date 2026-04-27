---
name: mechanistic-vs-hybrid-architecture
description: Phase 7 Commit ν guidance. Teaches the modeler when to use
  full-mechanistic (EMOD/malariasimulation/Starsim with full immunity
  dynamics), hybrid (mechanistic calibration + published-RCT
  intervention multipliers), or analytical (regression overlays) for
  budget-allocation analyses. The 1935 malaria run wasted 3 rounds
  reaching the hybrid framing because no skill taught this decision
  upfront. For allocation analyses where intervention effects are
  dominated by published meta-analyses, START WITH HYBRID. Trigger
  phrases include "mechanistic vs hybrid", "ABM vs analytical",
  "intervention effect calibration", "domain-implausible ICER", "LLIN
  ICER too high".
---

# Mechanistic vs Hybrid vs Analytical Model Architecture

## Why this skill exists

Across four Nigeria malaria runs (1302, 2057, 0912, 1935), the modeler
defaulted to one of two architectural extremes:

- **1302**: pure analytical/Hill-function overlay (over-claimed 2× efficiency vs Scott 2017)
- **2057**: ABM-as-decoration with surrogate analytical UQ (under-documented surrogate)
- **0912**: hybrid by design (best report among first three runs, but reached the framing only after 5 rounds)
- **1935**: pure ABM in round 1 → ABM ICER $144-306/DALY (3-7× outside Cochrane benchmark $27-44) → forced rebuild to hybrid in round 4

The 1935 run's 4-round evolution narrative is the missing skill content. **The hybrid is the right starting point for allocation analyses** because intervention effects are dominated by published RCT/meta-analysis multipliers, not by ABM-internal dynamics. An experienced senior modeler at a Gates Foundation / WHO / NMP planning unit would START with the hybrid.

## The three architectural choices

### Full mechanistic
Calibrated transmission AND calibrated intervention effects, all from a single mechanistic model (EMOD, malariasimulation, Starsim with custom Malaria/Disease class with explicit immunity, superinfection, anti-vector behavior).

**When to use**:
- Transmission research (e.g., what happens to the EIR-PfPR relationship under sustained 80% LLIN coverage?)
- Mechanism papers (e.g., does asymptomatic infection sustain transmission?)
- Studying intervention interactions that aren't well-captured by static multipliers (e.g., how does waning ITN efficacy interact with seasonally-targeted SMC?)
- Severe-malaria treatment evaluation where outcomes interact with immunity

**When NOT to use**:
- Resource allocation decisions
- Cost-effectiveness comparisons where intervention effects are well-studied in RCTs
- Multi-disease or large-scale (>500 LGA/admin units) analyses

**Why not for allocation**: pure ABM intervention effects rarely match published meta-analyses on the first try. The 1935 run's pure-ABM round 3 produced LLIN ICER $144-306/DALY, far outside Cochrane's $27-44/DALY range. This isn't a bug — it's that ABM dynamics produce intervention effects that depend on calibrated immunity, contact networks, and biting behavior, all of which carry compounding uncertainty.

### Hybrid (RECOMMENDED for allocation)
**Calibrate baseline transmission** with an ABM/ODE/Starsim/EMOD framework. **Estimate intervention effects** with published OR/RR/RR-from-meta-analysis multipliers applied to the calibrated baseline.

**When to use**:
- Budget-allocation decisions (Global Fund, NMP planning, CDC, foundation portfolios)
- Cost-effectiveness comparisons where published RCTs/meta-analyses cover the interventions
- Multi-LGA / multi-state / multi-country analyses where calibrating intervention effects per location is intractable
- Policy-relevance > mechanistic depth

**Why it works**: the hybrid pattern is standard practice in NMP planning analyses. Ozodiegwu 2023 (EMOD), Scott 2017 (Optima), Galactionova 2017 (OpenMalaria) all use it. The published RCT multipliers (LLIN OR=0.44 from Yang 2018, IRS OR=0.35 from Zhou 2022, SMC RR=0.27 from Thwing 2024, PBO 44% from Protopopoff 2018) are the credible source of intervention effects; the ABM provides spatial heterogeneity in baseline transmission.

**Critical constraints**:
- Document the OR→RR conversion at baseline PfPR explicitly (RR = OR / ((1-P0) + P0·OR))
- Apply effects multiplicatively to PfPR or EIR, not to cases (avoids double-counting through population/incidence chains)
- Disclose the architecture in §Methods: "We calibrate baseline with ABM and apply published RCT multipliers as intervention effects." This is standard, accepted practice.

### Analytical
Regression overlay on top of published parameter estimates, no mechanistic baseline calibration.

**When to use**:
- Benchmarking only ("what would Cochrane RCT effects predict for this geography?")
- Sanity checks against more sophisticated models
- Quick-look feasibility studies (1-day analyses)

**When NOT to use**: any analysis whose policy recommendations depend on getting baseline-transmission heterogeneity right (i.e., almost any allocation analysis).

## Decision tree

```
Is the analysis a budget-allocation decision?
├── YES → Are intervention effects covered by published RCTs/meta-analyses?
│   ├── YES → START WITH HYBRID. Calibrate baseline with ABM, apply published
│   │         OR/RR multipliers. Document architecture in §Methods.
│   └── NO  → Full mechanistic; document calibration of intervention effects
│             in §Methods (rare for malaria; common for novel diseases)
│
├── NO, transmission research → Full mechanistic
└── NO, methods comparison only → Analytical (or hybrid as benchmark)
```

## Worked example: 1935 malaria 4-round evolution

This is the canonical "what happens when the modeler skips this skill":

**Round 1** — Hill function PfPR=f(EIR), no ABM, no agents:
- Too simple to evaluate intervention effects
- All four critique agents flagged structural mismatch (no Starsim)

**Round 2** — ODE compartmental, manually-set parameters:
- 7 ridge-trapped parameters (identifiability fails)
- LLIN ICER acceptable but the model has no spatial structure

**Round 3** — Pure Starsim ABM with built-in intervention calculations:
- Calibration RMSE 0.26pp (excellent)
- BUT LLIN ICER $144-306/DALY (3-7× outside Cochrane $27-44)
- Domain critic D-015 fires STRUCTURAL: "0% LLIN allocation at $107M/yr — domain-implausible"
- 0% of LGAs receive LLIN because the optimizer correctly rejects them at this ICER

**Round 4** — Hybrid (FINALLY):
- Same Starsim calibration as round 3
- BUT intervention effects from Yang 2018 / Zhou 2022 / Thwing 2024 meta-analyses applied as multipliers
- LLIN ICER drops to $43.19/DALY (matches Conteh 2021 benchmark $44.51)
- 34% of LGAs receive LLIN — sensible allocation

**Lesson**: Round 4 should have been round 1. The 1935 modeler wasted 3 rounds and roughly $20-30 of API spend reaching a framing that's standard practice in the field. The hybrid is the right answer; the only reason to deviate is a mechanistic-research question, which allocation analyses are not.

## Quick-start template

When your run produces an allocation CSV:

```python
# In your modeler's outcome_fn or burden estimator:

# 1. Calibrate baseline PfPR per archetype using your ABM
calibrated_pfpr = calibrate_starsim_per_archetype(targets_nmis_2021)

# 2. Apply published intervention multipliers (NOT ABM-derived effects)
def intervention_effect(intervention, p0):
    """Convert published OR/RR to RR at baseline PfPR p0."""
    if intervention == "llin_standard":
        OR = 0.44  # Yang 2018
        RR = OR / ((1 - p0) + p0 * OR)
    elif intervention == "pbo_llin":
        # 44% prevalence reduction vs standard (Protopopoff 2018)
        RR_standard = intervention_effect("llin_standard", p0)
        RR = RR_standard * 0.56  # 44% additional reduction
    elif intervention == "irs_non_pyrethroid":
        OR = 0.35  # Zhou 2022
        RR = OR / ((1 - p0) + p0 * OR)
    elif intervention == "smc":
        return 0.27  # RR for clinical malaria, U5 only — Thwing 2024
    # ... etc.
    return RR

# 3. Compute cases averted = baseline_cases * (1 - RR_combination)
# 4. Compute DALYs averted with age-stratified weights (see daly-weighted-analysis skill)
```

## Related skills

- `modeling-strategy` — purpose-driven complexity. This skill tells you "which complexity"; use modeling-strategy to decide "how much".
- `model-fitness` — audience-specific structural requirements (GF/WHO).
- `effect-size-priors` — Parameter Registry contract for the published multipliers.
- `daly-weighted-analysis` — once you have intervention effects, compute DALY-averted figures.
- `optimizer-method-selection` — once you have DALYs per (LGA, package), choose ILP/greedy/SA.
- `allocation-cross-validation` — once you have an allocation, validate it via spatial holdout.
