---
name: sanity-schema
description: Phase 13 α — required disease-agnostic structural sanity manifest. Modeler writes `models/sanity_schema.yaml` declaring abstract slots (outcome name, baseline reservoir, exposure unit, shares, derived-consistency formulas, composite-dimension windows, structural-uncertainty bounds, outlier sniff). `scripts/sanity_checks.py` runs eight generic checks that don't know what disease/topic the run is about. Internal-only — no comparison against external literature; only structural relationships that hold in any optimization-under-budget problem. Trigger phrases include "sanity check", "gut check", "scale plausibility", "internal consistency", "mass balance", "share closure", "counterfactual ratio".
type: rigor-remediation
---

# Sanity Schema — Disease-Agnostic Structural Checks

## What this skill is for

You're producing a budget-constrained optimization output: an
allocation that averts some quantity of bad outcome, evaluated
against a counterfactual, on some population of exposure units. The
headline numbers are internally derived from your model. **No gate
will validate them against external literature** — that would be
circular and topical. But your model has internal relationships
that must hold regardless of what disease, sector, or unit you're
modeling. Phase 13 α makes those relationships explicit and
mechanical.

You write `models/sanity_schema.yaml`. The validator runs eight
checks. Failures emit MEDIUM advisories the modeler must either
fix or explicitly acknowledge.

## When the gate fires

`_check_sanity_schema` in `scripts/validate_critique_yaml.py`
triggers at **round ≥ 3**. Earlier rounds skip the check (you're
still composing outputs). At round ≥ 3:

- Schema absent → MEDIUM `sanity_schema_missing`
- Schema malformed → HIGH `sanity_schema_invalid`
- Per-check failure → MEDIUM `sanity_check_failed_<id>`

## The artifact schema

Every section is optional. Declare only what your model produces;
sections you skip silently skip their checks.

```yaml
outcome:
  name: DALYs averted              # disease-agnostic — could be "tons CO2 reduced"
  unit: DALYs
  baseline_total: 16_600_000       # what's at stake before intervention
  averted_point: 8_390_000         # deterministic-baseline headline number
  averted_uq_mean: 8_570_000       # optional — for documentation only
  ci: [3_330_000, 16_900_000]      # optional — for documentation only

exposure:
  unit: people                     # could be "factories", "transactions"
  total_in_allocated: 32_000_000   # sum across allocated units only
  rate_per_unit_per_year: 0.5      # baseline rate of the bad outcome per unit
  years: 3                         # default 1 if absent

allocation:
  units_total: 774                 # documentation only
  units_allocated: 123
  budget: 319_829_076

shares:                            # every share-set must close to its target
  - name: zone_budget
    sums_to: 1.0
    tol: 0.005
    values: {NW: 0.781, NC: 0.108, NE: 0.111}
  - name: package_cost
    sums_to: 1.0
    tol: 0.005
    values: {dual_ai: 0.78, pbo: 0.18, smc: 0.04}

counterfactual:
  name: null_model
  averted: 421_000                 # bad outcome averted under the null comparator
  optimized_vs_null_acceptable: [1.5, 100]   # ratio band

derived_consistency:               # any two quantities related by a model formula
  - primary: cases_averted
    primary_value: 48_400_000
    derived: deaths_averted
    derived_actual: 194_000        # the headline number you report for derived
    formula: "primary * cfr"       # arithmetic only — no calls, no attributes
    constants: {cfr: 0.004}
    tol: 0.10                      # 10% drift between actual and formula = fail

composite_dimensions:              # composite metrics get an epi-defensible window
  - name: DALY_per_death
    value: 43.2
    window: [15, 60]               # 15 = heavily-discounted adult; 60 = undiscounted U5

structural_uncertainty:            # any rigor artifact with non-BOUNDED verdict
  - source: within_zone_heterogeneity.yaml
    verdict: INCONCLUSIVE          # BOUNDED entries silently pass
    lower_bound: 7_500_000         # the value that must appear in must_appear_in
    must_appear_in: report.md
    section_hint: "§Executive Summary"

outlier_sniff:
  metric: dalys_averted_per_dollar
  rule: max_over_median            # only rule supported in v1
  threshold: 10.0                  # max/median > threshold = fail
  csv_path: models/allocation_result.csv
  numerator_col: dalys_averted
  denominator_col: total_cost_usd  # optional — omit for raw numerator
```

## The eight checks

Each is internal-only. None reads literature, none guesses, none
compares to other runs.

**1. mass_balance** — `outcome.averted_point ≤ 0.95 × outcome.baseline_total`.
You can't avert more than ~95% of what was at stake. Either the
baseline reservoir is mis-scoped (the wrong total) or the averted
estimate is too high. Disease-agnostic.

**2. per_unit_intensity** — `averted ≤ exposure.total × rate × years`.
The total cap is what every exposure unit could possibly contribute;
no allocation can avert more than this.

**3. share_closure** — for each share-set: `abs(sum(values) - sums_to) ≤ tol`.
Zone budget shares, package cost shares, demographic shares — they
all must close to 1.0 (or whatever the modeler declares). Catches
staleness drift across files.

**4. derived_consistency** — for each pair: `abs(derived_actual - eval(formula)) / derived_actual ≤ tol`.
The headline `deaths_averted` should equal `cases_averted × CFR`
within tol. Formulas are restricted to arithmetic over schema-declared
variables — no function calls, no attribute access, no imports. The
parser rejects anything else.

**5. composite_dimensions** — for each composite: `value ∈ [lo, hi]`.
DALY/death, $/QALY, kg-CO2/$, conversion-rate/click — every composite
metric has a defensible epidemiological window. The MODELER declares
the window; the script enforces it.

**6. counterfactual_ratio** — `optimized.averted / counterfactual.averted ∈ [lo, hi]`.
Optimization that's only 1.0× better than the null is unimpressive;
optimization that's 1000× better usually means the null is "no
interventions at all" rather than "current standard of care."
Forces you to declare your counterfactual explicitly.

**7. heterogeneity_carryforward** — for each non-BOUNDED structural-uncertainty
entry: the `lower_bound` value must appear in the `must_appear_in`
file. If `within_zone_heterogeneity.yaml` says INCONCLUSIVE with
lower_bound=7.5M, but report.md only mentions 8.57M, this fires.
The check is tolerant of M/million/k/thousand/comma formatting (±2%).

**8. outlier_sniff** — `max(metric) / median(metric) ≤ threshold`. If one
allocated unit dominates the marginal-effectiveness ranking (top-1
is 50× the median), the optimization is fragile. Domain-agnostic
robustness check on the model's own output.

## How to declare exemptions

Sometimes a check fires legitimately. The modeler can declare an
explicit acknowledgment in `scope_declaration.yaml`:

```yaml
declarations:
  - id: SCOPE-G
    sanity_check_acknowledged: [counterfactual_ratio]
    rationale: |
      The null model is "no GC7 intervention at all" — neither
      maintained 50% LLIN coverage nor existing SMC. This is the
      Global Fund's standard counterfactual for new-money decisions,
      and it produces a 19.9× ratio that exceeds our 100× cap. The
      ratio is informative, not pathological.
```

Or globally for the run:

```yaml
sanity_check_acknowledged: [outlier_sniff]
```

The acknowledgment must be in scope_declaration.yaml. A free-text
note in the report.md is not sufficient — the validator only reads
the structured YAML.

## Worked example — Phase 12 RESUME run (190855)

Headline: 8.39M DALYs averted, baseline 16.6M, on 32M people in 123
NW/NE/NC LGAs at 0.5 cases/person/yr × 3 years.

Plausible schema for that run:

```yaml
outcome:
  name: DALYs averted
  baseline_total: 16_600_000
  averted_point: 8_390_000
exposure:
  unit: people
  total_in_allocated: 32_000_000
  rate_per_unit_per_year: 0.5
  years: 3
shares:
  - name: zone_budget
    sums_to: 1.0
    tol: 0.005
    values: {NW: 0.781, NC: 0.108, NE: 0.111}
counterfactual:
  averted: 421_000
  optimized_vs_null_acceptable: [1.5, 100]
derived_consistency:
  - primary: cases_averted
    primary_value: 48_400_000
    derived: deaths_averted
    derived_actual: 194_000
    formula: "primary * cfr"
    constants: {cfr: 0.004}
    tol: 0.05
composite_dimensions:
  - name: DALY_per_death
    value: 43.2
    window: [15, 60]
structural_uncertainty:
  - source: within_zone_heterogeneity.yaml
    verdict: INCONCLUSIVE
    lower_bound: 7_500_000
    must_appear_in: report.md
```

Expected verdicts on this run:
- mass_balance: 8.39M / 16.6M = 51% → PASS
- per_unit_intensity: 8.39M / (32M × 0.5 × 3) = 0.17 → PASS
- share_closure (zone_budget): 0.781+0.108+0.111 = 1.000 → PASS
- derived_consistency (cases→deaths): 48.4M × 0.004 = 193.6k vs 194k → PASS (0.2% drift)
- composite_dimensions (DALY/death): 43.2 ∈ [15, 60] → PASS
- counterfactual_ratio: 8.39M / 421k = 19.9× ∈ [1.5, 100] → PASS
- heterogeneity_carryforward: 7.5M not in report.md §Executive Summary → **FAIL** (the load-bearing check for this run)
- outlier_sniff: depends on per-LGA distribution

Net: one MEDIUM, surfaced exactly the issue the deep-dive review identified.

## What the schema does NOT do

- It does not compare your numbers to any other run, paper, or
  benchmark. All thresholds are either model-internal mass-balance
  facts (≤95%, ≤unit cap) or modeler-declared windows.
- It does not validate the units of your declared values. You
  declare DALYs vs cases vs deaths; the script trusts the labels.
- It does not block ACCEPT. Every check emits MEDIUM. Persistent
  MEDIUMs may escalate via Phase 12 β, but a single failed check at
  one round is just an advisory.

## Drafting timeline

The artifact is required at **round 3 onward**. Earlier rounds skip
the check. Draft early — many failed checks point at modeling
decisions you'll otherwise unwind in late rounds.

The recommended path:
1. **r2-3:** write the schema with deterministic-baseline numbers
   (no UQ, no CI). Run `python3 scripts/sanity_checks.py <run_dir>`
   and address any failures.
2. **r4-5:** populate `averted_uq_mean` and `ci` fields once UQ
   has run. The check ignores those fields — they're documentation.
3. **r6:** finalize. By the writer-QA pass, the headline numbers
   in the schema must match the headline numbers in report.md.

## Related skills

- `ecological-fallacy-quantification` (Phase 12 γ): produces
  `within_zone_heterogeneity.yaml`, which feeds the
  `structural_uncertainty` slot of the sanity schema.
- `sensitivity-analysis-remediation` (Phase 10 ω): the SENSITIVE/
  UNSTABLE verdict from sensitivity_analysis.yaml is another
  candidate for the `structural_uncertainty` slot.
- `decision-rule-extraction` (Phase 9 ρ): the share-closure check
  on the package shares is most stable when the decision rule is
  finalized.
