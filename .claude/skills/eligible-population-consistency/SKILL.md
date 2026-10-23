---
name: eligible-population-consistency
description: "Required when a model compares, costs, or allocates targeted interventions. Each intervention's benefit must be computed on the population that can receive it — the same population its cost is charged for — not on the total population. Declare eligible and total population input fields in models/eligibility_manifest.yaml; scripts/eligibility_probe.py checks the outcome function behaviorally. Trigger phrases include \"eligible population\", \"target population\", \"age-restricted intervention\", \"benefit denominator\", \"cost per beneficiary\", \"herd effect\", \"spillover\"."
---

# Eligible-population consistency (Phase 21 δ)

## The error class

A targeted intervention reaches only part of a unit's population. Its
**cost** is usually computed correctly on that subgroup (doses × eligible
people), but its **benefit** is easy to write as
`effect × baseline_rate × total_population`. The optimizer then credits
the intervention with outcomes in people it never touches, making it look
several-fold more cost-effective than it is. A live run did exactly this:
a chemoprevention package for children under 5 was costed on the under-5
population while its cases-averted term used the whole population.

The bug shape is disease-agnostic:

| Area | Intervention | Eligible group | Wrong denominator |
|---|---|---|---|
| Vaccine-preventable disease | infant / adolescent vaccine | birth cohort or age band | total population |
| NTDs | school-based deworming | enrolled school-age children | all ages |
| HIV | ART | diagnosed people living with HIV | all PLHIV or all adults |
| Maternal health | IPTp, iron supplementation | pregnant women | women of reproductive age / total |
| TB | preventive therapy | household contacts | catchment population |
| Vector-borne | chemoprevention | children in the eligible age band, seasonal areas | total population |

## Contract

Write `models/eligibility_manifest.yaml` alongside `models/outcome_fn.py`:

```yaml
outcome_fn: models/outcome_fn.py::evaluate_unit   # path::fn, same convention as outcome_fn
call_style: dict              # dict: fn(inputs) | kwargs: fn(**inputs)
baseline_inputs:              # one representative unit where interventions have an effect
  total_population: 120000
  eligible_children: 21000
  baseline_rate: 0.3
  package: none
# or: baseline_inputs_csv: models/units.csv  (+ baseline_row: N)
interventions:
  - name: school_deworming
    on_inputs:  {package: deworming}
    off_inputs: {package: none}
    eligible_population_field: eligible_children     # str or list
    total_population_field: total_population         # str or list
    benefit_output: dalys_averted
    spillover: false
    spillover_justification: ""
```

Optional knobs: `tolerance` (0.05), `eligible_scale` (0.5),
`total_scale` (2.0), `total_tolerance` (0.10).

The outcome function must be deterministic and must read population
sizes from its inputs (not from module-level constants) so the probe can
perturb them.

### No targeted interventions

If every intervention in the run is delivered to the whole population, there
is no eligible subgroup to probe. Say so explicitly instead of inventing
duplicate population inputs:

```yaml
targeted_interventions: none
no_targeted_justification: "<why each intervention reaches the whole population>"
```

List no `interventions` in that case. The probe reports `NOT_APPLICABLE`. A
missing justification, or a declaration that also lists interventions, is
HIGH `eligibility_manifest_invalid`. Reviewers will check the claim against
the model.

## What the probe checks

`python3 scripts/eligibility_probe.py <run_dir>` computes the marginal
benefit `benefit(on) − benefit(off)` on the baseline record and then:

1. **Eligible → 0** — marginal benefit must vanish
   (`eligibility_benefit_without_eligible`, HIGH).
2. **Eligible × 0.5, total fixed** — marginal benefit must shrink
   (`eligibility_insensitive`, MEDIUM).
3. **Total × 2, eligible fixed** — marginal benefit must stay within 10%
   (`eligibility_benefit_scales_with_total`, HIGH).

## Spillover (herd / indirect effects)

Some interventions genuinely benefit non-recipients: vaccines and vector
control reduce transmission, and treatment-as-prevention reduces
onward infection. Declare `spillover: true` with a one-sentence mechanism
in `spillover_justification`. Check 3 is then informational (LOW). Check 1
still applies, because with nobody eligible there is nobody to protect
anyone else. If you declare spillover, the indirect term should come from
a transmission mechanism or a cited indirect-effect estimate, not from
reusing the direct-effect formula on everyone.

## Fixing a failure

Compute benefit as `effect × baseline_rate_in_eligible × eligible_population`.
If the baseline rate is only known for the whole population, convert it
with an age- or risk-specific multiplier from the literature and record
that multiplier in the parameter registry. Use the same eligible
population for cost and for benefit.
