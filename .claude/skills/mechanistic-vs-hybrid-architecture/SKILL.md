---
name: mechanistic-vs-hybrid-architecture
description: >-
  When to use full-mechanistic (EMOD, malariasimulation, Starsim with full
  immunity dynamics), hybrid (mechanistic calibration plus published-RCT
  intervention multipliers), or analytical models for budget-allocation
  analyses. When intervention effects are dominated by published
  meta-analyses, start with hybrid. Trigger phrases include "mechanistic vs
  hybrid", "ABM vs analytical", "intervention effect calibration",
  "domain-implausible ICER", "ICER too high".
---

# Mechanistic vs Hybrid vs Analytical Model Architecture

## Why this skill exists

The lesson comes from four national malaria-allocation runs (1302,
2057, 0912, 1935), but it applies to any allocation question whose
intervention effects are well studied in trials. The modeler defaulted to one of two architectural extremes:

- **1302**: pure analytical/Hill-function overlay (over-claimed 2× efficiency vs Scott 2017)
- **2057**: ABM-as-decoration with surrogate analytical UQ (under-documented surrogate)
- **0912**: hybrid by design (best report among first three runs, but reached the framing only after 5 rounds)
- **1935**: pure ABM in round 1 → ABM ICER $144-306/DALY (3-7× outside Cochrane benchmark $27-44) → forced rebuild to hybrid in round 4

The 1935 run's 4-round evolution narrative is the missing skill content. **The hybrid is the right starting point for allocation analyses** because intervention effects are dominated by published RCT/meta-analysis multipliers, not by ABM-internal dynamics. An experienced senior modeler at a ministry planning unit, WHO, or a foundation would START with the hybrid.

## The three architectural choices

### Full mechanistic
Calibrated transmission AND calibrated intervention effects, all from a single mechanistic model (EMOD, malariasimulation, OpenMalaria, Starsim/stisim, a TB or HIV transmission model with explicit immunity, natural history, and behavior).

**When to use**:
- Transmission research (e.g., how does the force-of-infection–prevalence relationship change under sustained high intervention coverage?)
- Mechanism papers (e.g., does asymptomatic or subclinical infection sustain transmission?)
- Intervention interactions that static multipliers don't capture (e.g., waning vaccine immunity interacting with a seasonal campaign; herd effects that depend on coverage)
- Treatment evaluation where outcomes interact with immunity or resistance

**When NOT to use**:
- Resource allocation decisions
- Cost-effectiveness comparisons where intervention effects are well-studied in RCTs
- Multi-disease or large-scale (>500 admin units) analyses

**Why not for allocation**: pure ABM intervention effects rarely match published meta-analyses on the first try. In the worked example below, a pure-ABM round produced an ICER 3-7× outside the Cochrane-derived benchmark range. This isn't a bug — ABM dynamics produce intervention effects that depend on calibrated immunity, contact networks, and behavior, all of which carry compounding uncertainty.

### Hybrid (RECOMMENDED for allocation)
**Calibrate baseline transmission** with an ABM/ODE/Starsim/EMOD framework. **Estimate intervention effects** with published OR/RR/RR-from-meta-analysis multipliers applied to the calibrated baseline.

**When to use**:
- Budget-allocation decisions (national program planning, Global Fund / Gavi applications, foundation portfolios)
- Cost-effectiveness comparisons where published RCTs/meta-analyses cover the interventions
- Multi-district / multi-province / multi-country analyses where calibrating intervention effects per location is intractable
- Policy-relevance > mechanistic depth

**Why it works**: the hybrid pattern is standard practice in program planning analyses across diseases (e.g., Optima-family allocation models for HIV, TB, and malaria; EMOD and OpenMalaria planning studies). Published RCT/meta-analysis multipliers (vaccine efficacy, chemoprevention incidence-rate ratios, treatment success rates, vector-control odds ratios) are the credible source of intervention effects; the mechanistic model provides heterogeneity in the baseline.

**Critical constraints**:
- Document any OR→RR conversion at the baseline risk explicitly (RR = OR / ((1-P0) + P0·OR))
- Apply effects to the baseline state variable (prevalence, force of infection, incidence rate), not to downstream cases (avoids double-counting through population/incidence chains)
- Apply each multiplier ONLY to the population the intervention reaches and is charged for. An effect measured in an eligible subgroup (e.g., children under five, pregnant women, people living with HIV) must not be applied to the whole population while its cost is charged for the subgroup only; see the `eligible-population-consistency` skill.
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
│             in §Methods (rare for well-trialed interventions; common for
│             novel diseases or products)
│
├── NO, transmission research → Full mechanistic
└── NO, methods comparison only → Analytical (or hybrid as benchmark)
```

## When to abandon HYBRID architecture mid-strategy (Phase 15 α)

HYBRID architecture (calibrated baseline + literature intervention
multipliers) is the default for budget-allocation analyses. But if
the calibration step is unidentifiable (params > targets), the ABM
contributes nothing the literature multipliers don't already supply.
The model becomes a decorative wrapper over `baseline × RR`.

Before committing to HYBRID, run `pre-model-identifiability-arithmetic`
arithmetic on the calibration step. If the verdict is OVER_SATURATED:

- **Tie calibration params across clusters/groups** to bring the
  ratio back to IDENTIFIABLE (best for retaining ABM mechanism diversity).
- **Drop the ABM entirely** and use the analytical model
  `outcome_post = outcome_baseline × RR × programmatic_factor` (cleaner
  and more honest about what the model is actually doing).

The 224202 run shipped a HYBRID with 40 fitted params for 6 targets
and post-hoc identifiability flagged 3/3 fitted params unidentified.
The modeler scope-declared and proceeded; the ABM was decorative.
This is the failure mode this section is meant to prevent.

---

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
#    dynamics that are easy to calibrate per cluster.
class DiseaseModel(ss.Module):
    """Scalar compartments (e.g. S/E/I/R or a disease-specific natural
    history) inside a Starsim ABM container."""
    def __init__(self, pars=None, **kwargs):
        super().__init__(pars=pars, **kwargs)
        # ... scalar compartments, force of infection scaled by
        #     a calibrated transmission parameter, etc.

    def step(self):
        # scalar compartment updates
        ...

# 2. Calibrate baseline transmission with the ABM (one Sim per cluster):
def calibrate_per_cluster(cluster_id: int, target_prevalence: float) -> float:
    """Binary-search the transmission scale to hit the target prevalence."""
    sim = ss.Sim(
        n_agents=1000,                                # explicit ABM size
        people=ss.People(n_agents=1000),              # ss.People constructor
        diseases=DiseaseModel(pars={"beta_scale": 1.0}),
        interventions=[],                             # baseline (no interventions)
        dur=ss.years(3),
        unit="month",
    )
    sim.run()                                          # ABM runs here
    return sim.results["prevalence_mean"]

# 3. Apply published RCT/meta-analysis multipliers AT ALLOCATION TIME
#    (NOT inside the ABM dynamics — keeping these separate is what
#    makes this hybrid rather than full-mechanistic).
#    Every value below must come from the effect-size-priors registry
#    with its source; the numbers here are placeholders.
EFFECTS = {
    # name: (effect, kind, eligible_population_field)
    "vector_control":   (0.55, "odds_ratio", "population_total"),
    "chemoprevention":  (0.30, "rate_ratio", "population_under5"),
    "vaccine":          (0.60, "rate_ratio", "birth_cohort"),
}

def intervention_effect_multiplier(intervention: str, p0: float) -> float:
    """Published effect converted to an RR at baseline risk p0."""
    effect, kind, _ = EFFECTS[intervention]
    if kind == "odds_ratio":
        return effect / ((1 - p0) + p0 * effect)   # OR → RR at p0
    return effect

# 4. Cases averted for intervention i in unit u =
#      baseline_rate[u] * eligible_pop[u][EFFECTS[i][2]] * (1 - RR_i)
#    The SAME eligible population must drive the cost of intervention i.
# 5. Compute DALYs averted with age-stratified weights
#    (see daly-weighted-analysis skill).
```

## What hybrid is NOT

These patterns LOOK like hybrid but actually skip the ABM step. The
spec-compliance gate (Phase 1.5 Commit B + Phase 8 Commit ο) catches
most of them as `approach_mismatch` HIGH:

❌ **Scalar dose-response curve (e.g., prevalence = f(force of
   infection)) + RCT multipliers.** No ABM
   container. Will fail spec-compliance approach_mismatch.

❌ **ODE compartmental + RCT multipliers.** Same problem if not
   wrapped in `ss.Sim()`. Pure `scipy.integrate.solve_ivp` is not
   hybrid; it is full analytical.

❌ **`import starsim as ss` without `ss.Sim()` / `ss.People()`
   construction.** Imports alone don't constitute ABM use — the
   spec-compliance gate explicitly catches this cosmetic-wrap pattern.

❌ **Subclassing `ss.SIS` or `ss.Disease` without ever running
   `sim.run()`.** Same cosmetic-wrap issue.

✅ **`ss.Sim(people=ss.People(n_agents=N), diseases=DiseaseModel(ss.Module))
   .run()` THEN apply published RCT multipliers in `outcome_fn`.**
   Scalar compartments inside the disease module are FINE — that is
   the canonical hybrid pattern.

If the question requires Starsim, the ABM container MUST be
constructed and run, even if the disease dynamics inside it are
compartmental/scalar. The spec-compliance heuristic recognizes
`class X(ss.Module)`, `class X(ss.Disease)`, `class X(ss.Intervention)`,
and `ss.Sim(...)` as unambiguous ABM signals — any one of these makes
the ABM-vs-ODE counting heuristic stand down.

## Effects held to trial evidence, whatever the architecture (Phase 38 β)

E7 attempt 7 built a full agent-based model because the question asked for
one. Every single-trial benchmark passed, but the plan's dual-AI net effect
against the run's comparator was an 83% burden reduction in covered seasons.
The pooled trials of dual-AI against the pyrethroid nets that comparator
kept support at most about 35% at the model's attained use. The fix, a cap
added after the library had run, came in round 2.

`models/effect_concordance.yaml` (format in `scripts/effect_concordance.py`)
makes this a check from the first allocation. Per intervention, record the
model's outcome ratio under the plan vs the decision brief's primary
comparator, in units receiving only that intervention, plus the registry
effect for the trial comparison that matches what the comparator already
delivers. The gate holds the modeled reduction to the trial's largest
plausible reduction, scaled to attained use. Compute it on the pilot before
a large batch. When it fails, the hybrid pattern above is the usual fix: keep
the mechanistic model for baseline transmission and constrain the
intervention effect to the trial.

## Related skills

- `modeling-strategy` — purpose-driven complexity. This skill tells you "which complexity"; use modeling-strategy to decide "how much".
- `model-fitness` — audience-specific structural requirements (GF/WHO).
- `effect-size-priors` — Parameter Registry contract for the published multipliers.
- `daly-weighted-analysis` — once you have intervention effects, compute DALY-averted figures.
- `optimizer-method-selection` — once you have DALYs per (unit, package), choose ILP/greedy/SA.
- `eligible-population-consistency` — every effect and its cost must use the same eligible population.
- `allocation-cross-validation` — once you have an allocation, validate it via spatial holdout.
