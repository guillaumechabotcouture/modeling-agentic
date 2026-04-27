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

## Quick-start template — explicit Starsim hybrid skeleton

When your run produces an allocation CSV, follow this skeleton. The
**ABM container MUST be constructed and run** (`ss.Sim(people=ss.People(...))
.run()`) even if the disease dynamics inside are scalar/compartmental —
that is precisely the canonical hybrid pattern. The 1721 modeler hit
2 rounds of `approach_mismatch` cycling because its first attempts
were "scalar Hill function + RCT multipliers" with no ABM container at
all. This skeleton is the unambiguous answer.

```python
import starsim as ss
import numpy as np

# 1. Define the disease module. Compartmental scalar dynamics inside an
#    ABM container IS hybrid. The ABM gives you spatial heterogeneity,
#    explicit time stepping, and Starsim's intervention application
#    machinery; the disease module gives you tractable transmission
#    dynamics that are easy to calibrate per archetype.
class MalariaModel(ss.Module):
    """SEADTP scalar compartments inside a Starsim ABM container."""
    def __init__(self, pars=None, **kwargs):
        super().__init__(pars=pars, **kwargs)
        # ... S/E/A/D/T/P scalar compartments,
        #     EIR-driven force of infection, etc.

    def step(self):
        # scalar compartment updates (S/E/A/D/T/P, etc.)
        ...

# 2. Calibrate baseline transmission with the ABM (one Sim per archetype):
def calibrate_per_archetype(archetype_id: int, target_pfpr: float) -> float:
    """Binary-search EIR scaling factor to hit target PfPR."""
    sim = ss.Sim(
        n_agents=1000,                                # explicit ABM size
        people=ss.People(n_agents=1000),              # ss.People constructor
        diseases=MalariaModel(pars={"eir_scale": 1.0}),
        interventions=[],                             # baseline (no interventions)
        dur=ss.years(3),
        unit="month",
    )
    sim.run()                                          # ABM runs here
    return sim.results["pfpr_mean"]

# 3. Apply published RCT/meta-analysis multipliers AT ALLOCATION TIME
#    (NOT inside the ABM dynamics — keeping these separate is what
#    makes this hybrid rather than full-mechanistic).
def intervention_effect_multiplier(intervention: str, p0: float) -> float:
    """OR / RR / IRR from published meta-analyses, returned as RR at p0."""
    if intervention == "llin_standard":
        OR = 0.44                          # Yang 2018
        return OR / ((1 - p0) + p0 * OR)   # OR → RR at baseline PfPR p0
    elif intervention == "pbo_llin":
        OR = 0.55                          # Protopopoff 2018 lifecycle
        return OR / ((1 - p0) + p0 * OR)
    elif intervention == "irs_non_pyrethroid":
        OR = 0.35                          # Zhou 2022
        return OR / ((1 - p0) + p0 * OR)
    elif intervention == "smc_under5":
        return 0.27                        # Thwing 2024 IRR — clinical
    elif intervention == "rts_s":
        return 0.61                        # Asante 2024 IRR — clinical, U5
    raise ValueError(intervention)

# 4. Compute cases averted = baseline_cases * (1 - combined_RR)
# 5. Compute DALYs averted with age-stratified weights
#    (see daly-weighted-analysis skill).
```

## What hybrid is NOT

These patterns LOOK like hybrid but actually skip the ABM step. The
spec-compliance gate (Phase 1.5 Commit B + Phase 8 Commit ο) catches
most of them as `approach_mismatch` HIGH:

❌ **Scalar Hill function PfPR=f(EIR) + RCT multipliers.** No ABM
   container. Will fail spec-compliance approach_mismatch.

❌ **ODE compartmental + RCT multipliers.** Same problem if not
   wrapped in `ss.Sim()`. Pure `scipy.integrate.solve_ivp` is not
   hybrid; it is full analytical.

❌ **`import starsim as ss` without `ss.Sim()` / `ss.People()`
   construction.** Imports alone don't constitute ABM use — the
   spec-compliance gate explicitly catches this cosmetic-wrap pattern.

❌ **Subclassing `ss.SIS` or `ss.Disease` without ever running
   `sim.run()`.** Same cosmetic-wrap issue.

✅ **`ss.Sim(people=ss.People(n_agents=N), diseases=MalariaModel(ss.Module))
   .run()` THEN apply published RCT multipliers in `outcome_fn`.**
   Scalar compartments inside the disease module are FINE — that is
   the canonical hybrid pattern.

If the question requires Starsim, the ABM container MUST be
constructed and run, even if the disease dynamics inside it are
compartmental/scalar. The spec-compliance heuristic recognizes
`class X(ss.Module)`, `class X(ss.Disease)`, `class X(ss.Intervention)`,
and `ss.Sim(...)` as unambiguous ABM signals — any one of these makes
the ABM-vs-ODE counting heuristic stand down.

## Related skills

- `modeling-strategy` — purpose-driven complexity. This skill tells you "which complexity"; use modeling-strategy to decide "how much".
- `model-fitness` — audience-specific structural requirements (GF/WHO).
- `effect-size-priors` — Parameter Registry contract for the published multipliers.
- `daly-weighted-analysis` — once you have intervention effects, compute DALY-averted figures.
- `optimizer-method-selection` — once you have DALYs per (LGA, package), choose ILP/greedy/SA.
- `allocation-cross-validation` — once you have an allocation, validate it via spatial holdout.
