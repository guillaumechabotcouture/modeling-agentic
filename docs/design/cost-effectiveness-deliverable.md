# Cost-effectiveness deliverable (design, not built)

Status: design, 2026-11-23, branch `design-cea`. Docs only; no code changes. Owner decision of 2026-11-23: the
harness's deliverables are allocation decisions, and four outside benchmark tasks (RSV maternal vaccine against a
long-acting monoclonal antibody, TB case-finding, dengue vaccination, malaria vaccine) all ask for
cost-effectiveness outputs the harness cannot produce today. This note designs a `cost_effectiveness` deliverable
kind, and a cost-effectiveness section an allocation run can add, computed only by harness code from engine
outputs and the signed spec. The LLM drafts spec fields, as for every other section. It never computes,
rounds or restates a number.

Read first: `toolkit/spec/schema.py` (deliverable kinds), `toolkit/analysis/run.py` (the steps),
`toolkit/analysis/claims.py`, `toolkit/alloc/costs.py`, `toolkit/analysis/value_of_information.py`,
`.claude/orchestration/decision_design_principles.md`, and the `daly-weighted-analysis` skill.

## 1. What exists and what is missing

| Needed for a cost-effectiveness answer | In the harness today | Gap |
|---|---|---|
| Costs per strategy, by perspective | `toolkit/alloc/costs.py`: programme cost items in base-year prices (index or local-index price rules) | No payer per item, so only one perspective. No costs of health events (hospital stays, treatment) that apply in every arm, the comparator included, so averted treatment costs are never counted. |
| Cost offsets (savings) | Incremental per-outcome items are floored at zero (`item_cost`, F6 S1-3) and reported as floors | A cost-effectiveness analysis must count savings. The floor is right for a budget (it never lets a saving pay for a commodity) and wrong for a cost-effectiveness ratio. |
| Discounting of costs and health | `core.discount_factor`: one factor, the mean of (1+r)^-(t-base) over the decision window assuming uniform accrual, applied to the benefit only | Costs are not discounted. There is one rate, not separate rates for costs and health. There is no per-year timing. |
| A health measure (DALYs or QALYs) | Engines compose DALYs inside the engine (malaria card: GBD 2019 weights; the package-rate engine: declared weighted sums) | Weights live in engine cards, not in the signed spec, and YLL are not discounted. There are no QALYs and no utility weights. |
| Incremental analysis, ICER, NMB | None. `figure_data.by_package` gives cost per outcome averted against the comparator, with a guard when the averted outcome is not positive | No dominance, no extended dominance, no frontier, no net monetary benefit, no thresholds. |
| Threshold price, price grid | `cost_items` decision tests (one price case, re-solved); principle 7 forbids interpolating a price | No root-finding and no price-by-threshold table. |
| Uncertainty | The uncertainty step draws every declared source and evaluates every package in every unit (`uncertainty.run`, hooks `on_draw`, `on_solve`) | Nothing reads those draws for costs and health together, so there are no CE planes, acceptability curves or probability cost-effective. |
| Value of information | `value_of_information.py`: EVPI and EVPPI for the allocation plan (Strong, Oakley & Brennan 2014) | Not per willingness-to-pay threshold, and not over a set of strategies. |
| Budget's implied price of health | `figure_data.frontier`: the marginal cost per outcome averted at the envelope; `solver.budget_dual` | Not compared with a declared threshold. |

## 2. Terms (defined once)

| Term | Meaning here |
|---|---|
| Strategy | One option under comparison: a package applied as the spec says (to every unit in the standalone kind; one package per unit in an allocation). Strategies are mutually exclusive. |
| Reference strategy | The strategy the headline compares with. By principle 2 it is the funded plan (current practice). A no-intervention strategy may be listed too, as context. |
| Perspective | Whose costs count. *Health system*: every cost the health system pays (programme, commodities, delivery, treatment of health events). *Societal*: the health-system costs plus costs to households (out-of-pocket, transport, caregiver time) and, only when declared, productivity. |
| Health measure | DALYs averted or QALYs gained, discounted. DALY: disability-adjusted life-year, YLL (years of life lost to death) + YLD (years lived with disability). QALY: quality-adjusted life-year. |
| ΔC, ΔE | Incremental (discounted) cost and health of one strategy against another. ΔE is DALYs averted or QALYs gained, so more is better. |
| ICER | Incremental cost-effectiveness ratio, ΔC / ΔE, between a strategy and the next less costly strategy on the frontier. Printed only under the guard in §5.4. |
| Dominance | Strategy B is (strongly) dominated when another strategy costs no more and gives at least as much health, with one of the two strictly. It is *extendedly dominated* when a mix of two other strategies would give more health at the same cost: in practice, its ICER is higher than that of the next more effective strategy. |
| Frontier | The strategies left after removing every dominated and extendedly dominated one, in increasing cost. Successive ICERs along it increase. |
| WTP threshold (λ) | Willingness to pay per unit of health, in base-year currency per DALY or QALY. Declared in the spec with its source. |
| NMB | Net monetary benefit at λ: NMB(s) = λ·E(s) − C(s). At any λ the strategy with the highest NMB is the one the frontier's ICER rule chooses (Stinnett & Mullahy 1998). |
| Threshold price | The highest unit price of a priced input at which a strategy is still the one chosen at λ. |
| CE plane | ΔE against ΔC for each strategy against the reference, one point per uncertainty draw. |
| CEAC | Cost-effectiveness acceptability curve: for each λ, the share of draws in which each strategy has the highest NMB. |
| CEAF | Acceptability frontier: for each λ, the strategy with the highest *expected* NMB, and the share of draws in which it is the best. |
| EVPI | Expected value of perfect information at λ: E[max_s NMB] − max_s E[NMB] over the draws. |

## 3. Rules the design keeps

1. Every number comes from harness code: the signed spec, the engine through the `Analysis` object
   (`core.Analysis.evaluate`), and `toolkit/alloc`. A new module, `toolkit/analysis/cea.py`, computes every
   cost-effectiveness quantity. The LLM writes words around claim ids.
2. One set of draws. Cost-effectiveness uncertainty reads the uncertainty step's draws through its hooks. It draws
   nothing of its own except the new `health_weights` source (§4.3), on its own random stream, so every other
   source's draws, and every stored batch, stay the same.
3. Fail closed. A strategy, a perspective, a price or a weight the module cannot read stops the step with a
   finding. It never leaves an empty table.
4. Disease-agnostic. The module names outcomes, components and age groups only from the spec and the engine card.
5. Principles 2 and 7 hold. The headline compares the recommended strategy with the funded plan. A threshold price
   is reported only as the bracket of two prices the harness evaluated (§5.6), never interpolated.

## 4. The spec

### 4.1 Two ways in

| Use | `deliverable.kind` | What is decided |
|---|---|---|
| Standalone | `cost_effectiveness` (new) | Which strategy to adopt, one strategy applied as declared (usually the same package in every unit, a national policy), at each declared threshold, from each perspective. No envelope: the budget impact is reported (§5.9), not used to constrain. |
| Add-on | `package_per_unit` with a top-level `cost_effectiveness:` section | The allocation as today, plus cost-effectiveness per package and for the plan (§9). |

The standalone kind follows `toolkit/spec/estimates.py`. It has its own `REQUIRED` tuple and `NOT_APPLICABLE`
sections (`constraints.envelope_usd` and `deliverable.allocation_file` do not apply). It keeps `options`
(components, packages, cost items), `comparator`, `objective` (outcome horizon and discounting, which §4.2 checks
against the section) and every calibration and uncertainty section. `schema.DELIVERABLE_KINDS` gains
`cost_effectiveness`, and `figures.DECISION_SETS` gains its figure set (§8).

### 4.2 The `cost_effectiveness` section

```yaml
cost_effectiveness:
  strategies: [none, maternal, mab]        # package ids from options.packages; mutually exclusive
  reference: none                          # the funded plan (principle 2); must be a listed strategy
  context: []                              # optional: strategies reported for context only (e.g. a null strategy)
  perspectives: [health_system, societal]  # at least one; the headline perspective comes first
  time_horizon:
    start: 2026.0                          # costs and health counted from here (the decision date)
    stop: 2027.0                           # the simulated window that supplies events; inside `horizon`
    deaths: lifetime                       # lifetime: each death counts its discounted remaining life (§5.2);
                                           # window: only life-years inside [start, stop)
  discounting:
    costs: {rate: 0.03}
    health: {rate: 0.03}
    base_year: 2026                        # year 0 for discounting
    timing: uniform_accrual                # how events fall within a window (§5.1): annual_series when the engine
                                           # supplies per-year outputs, uniform_accrual otherwise (declared)
  health_measure:
    kind: dalys                            # dalys | qalys
    events:                                # engine per-capita outcomes and what each costs in health
      hosp:
        yld: {weight: 0.133, duration_years: 0.019, range: [0.088, 0.190],
              source: "GBD disability weight, <health state>; duration from <source>", verified: unverified}
      deaths:
        death: true                        # a death: YLL from the life table (dalys) or QALYs lost (qalys)
    life_table: {csv: data/life_table.csv, age: age, remaining: ex,
                 source: "GBD reference life table, <round>", verified: unverified}
    age_at_event: {"0-6m": 0.25}           # engine age group -> mean age (years) at the event; source below
    age_at_event_source: "midpoint of the engine age group"
    # qalys only: utility decrements per event ({decrement, duration_years, range, source}) and
    # population-norm utilities by age for life-years lost ({csv, age, utility, source})
  thresholds:                              # at least one; never a default value in code
    - {id: t1, usd_per_unit: 1000, source: "<citation>", basis: "<how it was derived>"}
  lambda_grid: {max_multiple: 3.0, points: 61}   # CEAC axis: 0 to 3 x the highest threshold, plus each threshold
  consequence_costs:                       # costs of health events, charged in every strategy (never floored)
    - {id: hosp_cost, outcome: hosp, value: 850, price_year: 2024, price_rule: index, payer: health_system,
       range: [600, 1200], source: "<citation>"}
    - {id: hosp_household, outcome: hosp, value: 40, price_year: 2024, price_rule: index, payer: household,
       source: "<citation>"}
  payers: {maternal_cost: health_system, mab_cost: health_system}   # cost item id -> payer (default health_system)
  prices:                                  # optional: unit prices still to be set (negotiated or unknown)
    - {id: mab_price, cost_item: mab_cost, range: [5, 150], grid: [5, 10, 20, 30, 50, 75, 100, 150],
       role: negotiated}                   # negotiated: varied on the grid and solved for, never drawn
```

Field rules the validator enforces (fail closed, each problem naming its field):

| Field | Rule |
|---|---|
| `strategies` | At least two, each a package of `options.packages`, no repeats; `reference` and each `context` entry listed. |
| `perspectives` | Each in `(health_system, societal)`. `societal` needs every payer it sums to be declared (`household`, `other_sector`, `productivity`); a societal perspective with no non-health-system item is refused, because it would only repeat the health-system result. |
| `time_horizon` | Inside the spec's `horizon`. `stop` is not after the engine's simulated stop. `deaths: lifetime` needs a life table. |
| `discounting` | Both rates in [0, 0.15]; `base_year` set. When `objective.discounting` is not `none`, its rate must equal `health.rate` (one rate for health in the whole run). |
| `timing` | `annual_series` only when the engine card declares annual outputs (§5.1); `uniform_accrual` is recorded as a declared departure in `principle_departures`, with the timing decision test of §6 generated. |
| `health_measure.events` | Each key is an engine per-capita outcome (`EngineCard.outputs`). Each weight has a `source`, a `verified` state (the evidence-service vocabulary) and either a `range` or an `excluded` reason. Weights in [0, 1]; durations > 0. |
| `life_table`, `age_at_event` | Every engine age group with a death event has an age; the life table covers it. |
| `thresholds` | At least one, each with a source; in the base-year currency of `options.prices` (a threshold in another year or currency is refused, not converted silently). |
| `consequence_costs` | The same price rules and validation as `cost_items` (`costs.py` `_factor`), plus a `payer`. An outcome that has a consequence cost and also a per-outcome cost item of a component is refused unless the item says why they do not overlap (`not_double_counted`, as today). |
| `payers` | Every cost item of a strategy's components has a payer (default `health_system`, stated in the artifact). |
| `prices` | Each names a cost item of a listed strategy; `range` contains the item's value; the grid lies in the range. A price with role `negotiated` may not also be a drawn cost item (`uncertainty.sources` costs): one quantity is either uncertain or a decision variable, not both. |

### 4.3 Uncertainty sources

`schema.UNCERTAINTY_KINDS` gains `health_weights`: each health-measure weight with a `range`, drawn from its
declared distribution (uniform by default), on its own random stream. `consequence_costs` with a `range` join the
existing `costs` source as multipliers. Spike lesson 6 holds: each kind is declared or excluded with a reason, and
the interval label says what the draws cover.

## 5. The computation (`toolkit/analysis/cea.py`)

### 5.1 Totals per strategy

For each strategy s, each perspective and each draw (the point values are draw 0 in what follows):

- **Events.** The engine's per-capita outcomes over the window (`Outcomes.per_capita`), times each unit's
  population of each age group (`decision_units.populations`), from `Analysis.evaluate` as every step reads them.
- **Timing.** With `timing: annual_series` the engine also returns per-year totals (a new optional
  `Outcomes.by_year: {outcome: {age_group: [per-capita per year]}}` and `cost_drivers_by_year`; an engine card
  declares `supports_annual_series`, as `supports_delivery` today). Each year's events are discounted at
  mid-year. With `timing: uniform_accrual` events are spread evenly over the window and discounted by the mean of
  exp(−k(t − base)) over it, k = ln(1 + r): the factor `core.discount_factor` already uses for the benefit.
- **Programme costs.** `costs.cost_matrix` per item, **without the floor** (a new keyword `floor=False`; the
  allocation path keeps its floor). Items charged per year (`years`) are spread one charge per delivery year, each
  discounted at the start of its year; items charged once fall at the start of the delivery period.
- **Consequence costs.** Each strategy's events times each consequence cost's value, discounted with its events.
- **Health.** DALYs lost in strategy s = Σ events × (YLD weight × duration) + Σ deaths × YLL(age). With
  `deaths: lifetime`, YLL(a) = (1 − e^{−kL})/k, L the life table's remaining life expectancy at age a
  (continuous discounting; L itself when the rate is 0), discounted again from the year of death to the base year.
  QALYs: utility decrement × duration per event, and for a death the population-norm utility over the remaining
  life, discounted the same way. No age weighting (dropped by GBD since 2010).
- **Per perspective.** C_hs(s) = programme items paid by the health system + consequence costs with payer
  `health_system`. C_soc(s) = C_hs(s) + the other declared payers.

E(s) is health relative to the worst strategy, so that higher is better: for DALYs,
E(s) = DALYs lost(reference) − DALYs lost(s). For QALYs, E(s) = QALYs(s) − QALYs(reference). Only differences
enter any decision quantity, so the choice of zero does not change a result.

When the engine itself outputs a health measure (its own DALYs), that output is reported beside the
harness-composed one as a cross-check row, with the relative difference. It never feeds a decision number (open
question 3).

### 5.2 Incremental analysis

Per perspective, on the point values and in every draw:

```
input: strategies with (C, E)
1. sort by C ascending, ties by E descending
2. strong dominance: remove s if some t has C(t) <= C(s) and E(t) >= E(s), one strictly
   (exact ties in both are reported as `identical`, the first in strategy order kept)
3. repeat until no change:
     on the remaining list, ICER_i = (C_i − C_{i−1}) / (E_i − E_{i−1}) for i >= 1
     if some ICER_i >= ICER_{i+1}: remove i (extendedly dominated), record by which pair
4. the remaining list is the frontier; each strategy gets a status:
   frontier | dominated (by t) | extendedly_dominated (by the pair i−1, i+1) | identical (to t)
```

Each frontier strategy's ICER is against the next less costly frontier strategy. The artifact also gives every
strategy's ΔC and ΔE against the reference, and against the next less costly frontier strategy.

### 5.3 Net monetary benefit and the choice at each threshold

NMB(s, λ) = λ·E(s) − C(s), per perspective and draw. The strategy chosen at λ is the argmax of point NMB. A check,
on every run, confirms that the argmax equals the frontier strategy whose ICER interval contains λ
(Stinnett & Mullahy 1998); a disagreement stops the step (it means a bug, not a finding). Exact ties in NMB
(within a relative 1e-12) are reported as a tie, never broken silently.

### 5.4 The degenerate-ratio guard

A ratio is printed only in the north-east quadrant (ΔC > 0, ΔE > ε). The floor ε is a relative tolerance on the
reference's health (`thresholds.yaml cost_effectiveness.effect_floor_rel`, proposed 1e-6) plus an absolute 1e-12.

| ΔE | ΔC | Status recorded | What the report says |
|---|---|---|---|
| > ε | > 0 | `icer` | the ICER, with ΔC and ΔE |
| > ε | ≤ 0 | `dominant` | "more health at lower or equal cost"; no ratio |
| ≤ ε and ≥ −ε | any | `no_health_difference` | "no difference in health within the effect floor"; ΔC only; no ratio |
| < −ε | ≥ 0 | `dominated` | "less health at equal or higher cost"; no ratio |
| < −ε | < 0 | `less_health_lower_cost` | ΔC (a saving) and ΔE (a loss) separately, and the choice at each λ from NMB; no ratio, because a south-west ratio reads as an ICER with the decision rule reversed |

The guard is enforced in three places: `cea.py` writes an `icer` field only under status `icer`; the claims spec
emits an ICER claim only for those rows; and `render_claims.py` gains a check that refuses a claim whose id ends
in `.icer` when its sibling `.status` claim is not `icer`. The same guard applies in every draw: a draw's ratio is
never averaged or summarised (§5.7). The existing `figure_data.by_package` cost per outcome averted keeps its own
guard (no ratio when the outcome averted is not positive) and is labelled "cost per outcome averted against the
comparator", not an ICER.

### 5.5 Price grid

For each declared price and each perspective, the harness evaluates the strategies at every grid price (the cost
item's value replaced; nothing else moves, because a negotiated price changes no engine output) and records, per
(price, λ): each strategy's NMB, the chosen strategy, and its probability of being cost-effective over the draws
(§5.7). This is a table of computed cells. Nothing between two cells is printed.

### 5.6 Threshold price

For a priced strategy s and threshold λ, the threshold price p* solves
g(p) = NMB(s, λ, p) − max_{t ≠ s} NMB(t, λ, p) = 0 within the declared range:

1. Evaluate g on the grid (already done in §5.5). With no sign change: report `above_range` (s is chosen at every
   price in the range), `below_range` (at none), or `not_chosen` (never chosen at this λ), with the range ends.
   Never an extrapolated price.
2. With a sign change between grid prices p_a and p_b, run Brent's method on [p_a, p_b] using the harness's own
   NMB evaluation (`scipy.optimize.brentq`, `xtol` = 1e-6 × the range width). Every iterate is a real evaluation.
   Report p* with the bracket of the last two evaluated prices. More than one sign change is reported as several
   switching prices, each with its bracket.
3. Because programme cost is linear in a negotiated price, the closed form also exists. The step computes it and
   checks that Brent's root agrees within the tolerance. A disagreement stops the step: it means the price touched
   something it should not.

Principle 7 is met because each reported p* is bracketed by evaluated prices. A threshold-price curve (p* against
λ) is drawn only at the λ grid points where it was computed.

### 5.7 Uncertainty over the same draws

The uncertainty step already evaluates every package in every unit in every draw. Two small hook changes let
`cea.py` read those draws without re-running anything:

- `uncertainty.run`'s `on_draw(out, events, cost)` also receives `sampled` (the draw's cost multipliers and named
  inputs), as `on_solve` does, so consequence costs and the health-weight draw apply per draw.
- the `Drawer` draws `health_weights` on a separate stream, like `NE_STREAM`, so the other sources' draws are
  unchanged.

Per perspective and per λ on the grid:

| Output | Definition |
|---|---|
| ΔC, ΔE intervals | quantiles over draws of each strategy against the reference, at the spec's `uncertainty.level` |
| Quadrant shares | share of draws in each of the four quadrants of §5.4, per strategy against the reference |
| Expected NMB | mean over draws; the CEAF strategy is its argmax |
| CEAC | share of draws in which each strategy has the highest NMB; the CEAC curves sum to 1 at every λ |
| Probability cost-effective | the CEAF strategy's CEAC value at each declared threshold |
| EVPI at λ | mean_d max_s NMB(s, d) − max_s mean_d NMB(s, d), per person in the population the decision covers and in total; with a standard error from the per-draw gains |
| ICER at the means | mean ΔC / mean ΔE, under the guard of §5.4 applied to the means |

No interval is printed for an ICER. A percentile interval of per-draw ratios is undefined when ΔE crosses zero,
and misleading when it nears zero. The CE plane, the quadrant shares and the CEAC carry that uncertainty instead.

Below `thresholds.yaml cost_effectiveness.min_draws` (proposed 100) the CEAC and EVPI have status `indicative` and
are labelled so. EVPPI per input reuses `value_of_information.py`'s regression machinery with the strategies as
candidates and NMB at λ as the net benefit. That is a later step (§11, ε), not part of the first build.

### 5.8 Perspectives in the report

Both perspectives are computed whenever both are declared. The headline uses the first. The report states, from a
computed label claim, whether the chosen strategy differs between perspectives at each threshold.

### 5.9 Budget impact

Undiscounted programme and consequence cost per year of the chosen strategy against the reference, by payer, over
the time horizon. It is reported in the standalone kind, where no envelope constrains the choice.

## 6. Decision tests for cost-effectiveness

The criterion differs from the allocation tests. A cost-effectiveness test *changes the decision* at λ when the
strategy chosen at λ changes. Tests compile to `perturb` (`toolkit/spec/perturb.py`) with one new criterion,
`chosen_strategy`, and four new targets:

| Target | Moves | Generated by |
|---|---|---|
| `discount_rate` | `costs` and/or `health` rate to each value | the harness: 0% for both, and 0% for health only (open question 1); the spec may add values |
| `health_weight` | one weight to each end of its range | the harness, one test per weight with a range |
| `consequence_cost` | one consequence cost to each end of its range | the harness, one per range |
| `timing` | all events at the start, then all at the end, of the window | the harness, when `timing: uniform_accrual` (discounted totals are monotone in timing, so the two ends bound the assumption) |

Existing targets apply unchanged: `parameter` (engine parameters and priors), `cost_item`, `unit_column`,
`calibration_target`, `outcome`, `burden`.

**Switching values.** For each test with a numeric range, and each declared threshold, the harness finds the value
within the range at which the chosen strategy changes, as in §5.6: the two range ends evaluated first. When the
choice differs between them, a bisection on real evaluations runs until the bracket is narrower than 1% of the
range, or for at most `thresholds.yaml cost_effectiveness.max_switch_evaluations` evaluations (proposed 8; each
costs one evaluation, which on the emulator path is cheap and on the direct path costs engine runs, counted in the
record). The record reports `flips_at: [lo, hi]` (an evaluated bracket) or `no_flip_in_range`. A test whose ends
choose the same strategy is reported as not changing the decision at that λ. It is not proof that nothing inside
the range flips. When the record finds the NMB difference non-monotone between the ends (from the evaluated
points), it says so.

The summary table in `results.md`: one row per input, one column per threshold, each cell "no change" or "changes
between v1 and v2" (claims), ranked by the width of the NMB difference over the input's range at the first
threshold. That is principle 6 for cost-effectiveness: every uncertain input that can move the choice is a range
with its test.

## 7. Artifact and claims ledger

`analysis/cost_effectiveness.yaml` (kind `cost_effectiveness` in `artifacts.EXPECTED_KINDS`, producer
`toolkit/analysis/cea.py`, inputs the point and uncertainty artifacts, the spec and the life table):

```yaml
perspectives:
  health_system:
    strategies: {<s>: {cost, health, delta_cost, delta_health, status, vs: <t>, icer, frontier_rank}}
    frontier: [<s>, ...]
    thresholds: {t1: {lambda, chosen, nmb: {<s>: v}, p_cost_effective, evpi, evpi_per_person, tie}}
    uncertainty: {draws, status, quadrant_share: {<s>: {ne, se, sw, nw}}, delta_cost_interval, delta_health_interval}
    ceac: {lambda: [...], share: {<s>: [...]}, ceaf: [...]}
    prices: {<price id>: {grid: [...], cells: [...], threshold: {t1: {status, value, bracket}}}}
    budget_impact: {by_year: [...], by_payer: {...}}
health_measure: {kind, events, cross_check: {engine_output, relative_difference}}
switching: [{test_id, input, range, thresholds: {t1: {verdict, flips_at, evaluations}}}]
```

Claims (`claims.claim_spec` gains `_cea_claims`; ids follow `_cid`; `p` is the perspective, `t` the threshold id):

| Claim id | Kind | Source field |
|---|---|---|
| `cea.p.s.cost`, `cea.p.s.health` | currency, scalar | `perspectives.p.strategies.s.cost` / `.health` |
| `cea.p.s.delta_cost`, `cea.p.s.delta_health` | currency, scalar (with interval) | `.delta_cost` / `.delta_health` |
| `cea.p.s.status` | label | `.status` |
| `cea.p.s.icer` | currency per unit of health | `.icer`, emitted only when status is `icer` |
| `cea.p.t.chosen` | label | `thresholds.t.chosen` |
| `cea.p.t.s.nmb` | currency | `thresholds.t.nmb.s` |
| `cea.p.t.p_ce` | percentage | `thresholds.t.p_cost_effective` |
| `cea.p.t.evpi`, `cea.p.t.evpi_per_person` | currency | `thresholds.t.evpi` |
| `cea.p.price.t.threshold`, `.status`, `.bracket_lo`, `.bracket_hi` | currency, label | `prices.price.threshold.t` |
| `cea.perspectives_agree.t` | label | computed: whether the chosen strategy is the same in both perspectives |
| `cea.dt.test.t.verdict`, `.flip_lo`, `.flip_hi` | label, scalar | `switching[j].thresholds.t` |
| `cea.headline.*` | as above | the chosen strategy against the reference (principle 2), first perspective, first threshold |

`currency` claims render in the base-year currency with the price year. The ICER claim's units read
"USD (base year) per DALY averted" (or QALY gained); `render_claims.py` needs no new kind. The interval label on
ΔC and ΔE comes from `Spec.uncertainty_scope()` as for every interval claim. `results.md` gains a
cost-effectiveness section rendered from a harness template: the incremental table, the choice at each threshold,
the price table and the switching table, every cell a claim.

## 8. Figures

`figures.DECISION_SETS["cost_effectiveness"]`, drawn from the artifact only (the v2-figures standards: display
names, real units, no offset notation, the reference palette, a caption in `figures.yaml`, a provenance sidecar,
a `not_produced` entry with its reason when an input is missing):

| Figure | What it shows |
|---|---|
| `ce_frontier` | each strategy's discounted cost (y) against health (x); frontier strategies joined; dominated and extendedly dominated strategies marked and labelled; the reference marked |
| `ce_plane` | ΔE (x) against ΔC (y) per strategy against the reference, one point per draw, the point estimate marked, a line of slope λ for each declared threshold; quadrant shares in the caption |
| `ceac` | CEAC curves per strategy against λ; the CEAF drawn over them; declared thresholds as vertical lines |
| `price_grid` | price (y) by λ (x); each cell coloured by the chosen strategy and labelled with its probability of being cost-effective; computed cells only |
| `threshold_price` | the threshold price against λ, at the computed points, with the price range shaded; `above_range` and `below_range` points drawn at the range edge with a marker that says so |
| `evpi` | EVPI against λ (total and per person) |
| `switching` | per input, its range as a bar, with the switching bracket marked at each threshold where one was found |

The add-on (§9) adds `ce_by_package` to the allocation set: the per-package table of §9 as a dot plot of ΔC
against ΔE per package.

## 9. Composition with allocation

An allocation run with a `cost_effectiveness` section keeps its plan, objective and budget. The section adds:

1. **Per unit and package.** The same totals (§5.1) per unit; ΔC and ΔE of each feasible package against the
   unit's comparator package and against the funded plan's package; the incremental analysis (§5.2) within each
   unit over its feasible packages; the package chosen at each λ in each unit.
2. **Per package in the plan.** For the units that receive each package: total ΔC, ΔE, the status and ICER
   against the comparator, and against the next less costly package on those units' frontiers.
3. **The plan.** ΔC, ΔE and NMB of the recommended plan against the funded plan (the headline, principle 2), per
   perspective, with the CE plane and the probability that the plan's NMB beats the funded plan's at each λ.
4. **The plan at each threshold without the envelope.** `A.solve` with benefit λ·E − C per unit and package and the
   signed feasibility, caps and group rules, with the budget row removed (the toolkit's ILP, not a new optimiser).
   The artifact compares it with the budget-constrained plan: cost, health, and units that differ.
5. **The envelope's implied price of health.** The frontier's marginal cost per unit of health at the envelope
   (`figure_data.frontier`, on the health measure), set against each declared threshold: below a threshold, more
   money would buy health the threshold counts as cost-effective. The record says the dual of the budget row
   (`solver.budget_dual`) is from the relaxation and is not used for this.

Consistency rules: the allocation's budget arithmetic keeps its floor and its cost items. Consequence costs and
savings are reported as offsets beside the envelope and never enter it (open question 9). When
`objective.discounting` is set, its rate equals `cost_effectiveness.discounting.health.rate` (§4.2). Letting
`objective.outcome` name the harness-composed health measure, so that the allocation itself maximises discounted
DALYs, is open question 11.

## 10. Acceptance tests

Each module self-test (`python3 toolkit/analysis/cea.py --self-test`) runs these cases. They are written first and
fail before the code. Textbook cases are transcribed with their page or table reference into the test, each with
the edition used. Where an edition's example does not exercise a property, the hand-built case covers it and the
test says so.

| ID | Source | What it checks | Expected |
|---|---|---|---|
| CE1 | Hand-built, six strategies (cost, health): A (0, 0), B (100, 1), C (300, 1.5), D (250, 3), E (600, 4), F (1000, 4.2) | dominance, extended dominance, frontier, ICERs, NMB choice | C dominated by D; B extendedly dominated (ICER B vs A 100 > D vs B 75); frontier A, D, E, F with ICERs 83.33, 350, 2000; chosen at λ = 100: D (NMB 50); at λ = 500: E (NMB 1400) |
| CE2 | CE1 with E's cost = 200 + 10p, p = 40 at base; F and D carry no priced input | threshold price by Brent against the closed form | p* = 55 at λ = 500 (against D); p* = 25 at λ = 200; with range [0, 20] at λ = 200: `above_range`, no number |
| CE3 | Hand-built degenerate cases | the guard of §5.4 | ΔE = 0: `no_health_difference`; ΔE < 0, ΔC > 0: `dominated`; both negative: `less_health_lower_cost`; ΔE = 1e-14: below the floor; no `.icer` claim in any of them, and `render_claims` refuses a planted one |
| CE4 | Drummond MF, Sculpher MJ, Claxton K, Stoddart GL, Torrance GW. *Methods for the Economic Evaluation of Health Care Programmes*, 4th ed. Oxford University Press, 2015: the worked example of mutually exclusive programmes with dominance and extended dominance | the incremental algorithm on a published case | the book's frontier and ICERs |
| CE5 | Briggs A, Claxton K, Sculpher M. *Decision Modelling for Health Economic Evaluation*. Oxford University Press, 2006, chapter 2: the HIV/AIDS Markov model of zidovudine monotherapy against combination therapy (after Chancellor et al. 1997), deterministic | differential discounting of an annual series (the book uses different rates for costs and health) on a toy engine that returns the book's annual costs and life-years through `Outcomes.by_year` | the book's discounted costs, life-years and ICER |
| CE6 | Gray AM, Clarke PM, Wolstenholme JL, Wordsworth S. *Applied Methods of Cost-effectiveness Analysis in Health Care*. Oxford University Press, 2011: the incremental analysis and acceptability-curve exercises | a second published incremental table and a CEAC from its probabilistic exercise | the book's values, the CEAC within Monte Carlo error at its draw count |
| CE7 | Stinnett AA, Mullahy J. Net health benefits. *Med Decis Making* 1998;18(2 Suppl):S68-S80 | the NMB rule equals the frontier ICER rule | property test over 500 random strategy sets and λ values: identical choices (ties excepted) |
| CE8 | Fenwick E, Claxton K, Sculpher M. Representing uncertainty: the role of cost-effectiveness acceptability curves. *Health Econ* 2001;10:779-787 | CEAF uses expected NMB, not the highest CEAC | a constructed skewed case where the strategy with the higher CEAC has the lower expected NMB: the CEAF picks the latter |
| CE9 | Analytic normal case (two strategies, ΔE ~ N(1, 0.5²), ΔC ~ N(300, 200²), independent) | CEAC against its closed form, P(λΔE − ΔC > 0) = Φ((λμ_E − μ_C) / √(λ²σ_E² + σ_C²)) | at λ = 500: 0.734, within the Monte Carlo error of the draw count |
| CE10 | Unit normal loss (two strategies, incremental NMB ~ N(μ, σ²)) | EVPI = σφ(μ/σ) + μΦ(μ/σ) − max(0, μ) | μ = 10, σ = 20: 3.956, within Monte Carlo error |
| CE11 | Murray CJL. Quantifying the burden of disease: the technical basis for disability-adjusted life years. *Bull WHO* 1994;72:429-445 (continuous discounting of YLL, no age weighting) | discounted YLL | L = 30, r = 3%: (1 − e^{−0.9})/0.03 = 19.781; r = 0: 30 |
| CE12 | Hand-built discounting | the two timing conventions | 100 a year for 3 years, start of year, 3%: 291.347; uniform accrual over the same 3 years: 287.083 |
| CE13 | Neumann PJ, Sanders GD, Russell LB, Siegel JE, Ganiats TG (eds). *Cost-Effectiveness in Health and Medicine*, 2nd ed. Oxford University Press, 2016 (the impact inventory) | perspectives | a case with household costs: the societal total equals health system plus household; a societal perspective with no non-health-system item is refused at validation |
| CE14 | Hand-built allocation (four units, three packages, a budget that binds) | §9 | per-unit frontiers match CE1's algorithm unit by unit; the no-envelope plan at λ equals the per-unit NMB argmax when no group rule binds; the headline against the funded plan |
| CE15 | The run's own draws (end to end on the toy engine, both evaluation paths) | §5.7 reads the uncertainty draws, not new ones | the CEA reads exactly `uncertainty.draws` draws with the stored seed; the stored uncertainty artifact is byte-identical with and without the section |

Two further checks run in CI with the module: the shared bad-input fuzz (`scripts/gate_fuzz.py`) gains the
section's fields, and the planted-defect regression eval gains three planted defects: an ICER printed for a
dominated strategy, a floored saving, and costs left undiscounted.

## 11. Build plan

Each PR stays under 800 lines, with failing tests first, as the project's Greek-letter sub-commits:

| PR | Content | Smoke marker (only the new code produces it) |
|---|---|---|
| α | spec section and kind in `schema.py`; payers; `costs.cost_matrix(floor=False)`; CE1-CE3, CE7, CE11-CE13 failing | `toolkit/spec/schema.py --self-test` cases for the section |
| β | `cea.py` deterministic: totals, discounting (uniform accrual), composed health measure, incremental analysis, NMB, guard, price grid, threshold price; artifact and claims | `analysis/cost_effectiveness.yaml:perspectives` |
| γ | uncertainty: hook changes, `health_weights` source, quadrant shares, CEAC, CEAF, EVPI; CE8-CE10, CE15 | `analysis/cost_effectiveness.yaml:perspectives.health_system.ceac` |
| δ | figures and the results.md section | `figures/ceac.png`, `figures/ce_plane.png` |
| ε | decision tests (`chosen_strategy` criterion, new targets, switching values); EVPPI over strategies | `analysis/cost_effectiveness.yaml:switching` |
| ζ | allocation add-on (§9); CE14 | `analysis/cost_effectiveness.yaml:plan` |
| η | `Outcomes.by_year` in the contract and in one engine (the package-rate engine first); CE5 | `analysis/cost_effectiveness.yaml:timing=annual_series` |

The first live check is the synthetic RSV template (`data/world_model_trials/rsv_maternal_vs_monoclonal/`) with a
`cost_effectiveness` section added, on the package-rate engine. It is the cheapest of the four benchmark tasks and
already has two products with trial-anchored effect ranges and one priced product. CE4-CE6 enter with β and γ, once
the cases are transcribed.

## 12. Open questions, with recommended defaults

| # | Question | Recommended default |
|---|---|---|
| 1 | Default discount rates, and the rates the discount-rate test uses | 3% a year for costs and health (the iDSI reference case, Wilkinson et al. *Value Health* 2016;19:921-928, developed with Gates Foundation support; also the WHO-CHOICE convention). Tests at 0% for both and at 0% for health only. A spec may declare others, with their source. |
| 2 | Timing of events within a year | Mid-year for annual series; uniform accrual over the window when the engine has no annual outputs, declared as a departure, with the timing test of §6. Programme charges at the start of each delivery year. |
| 3 | Who composes the health measure | The harness, from the engine's natural outcomes and the spec's signed weights. An engine's own DALYs are a cross-check row only. |
| 4 | Default health measure | DALYs (GBD disability weights, the GBD reference life table, discounted YLL, no age weighting), which LMIC funders and the iDSI reference case use; QALYs when the question or the decision maker asks for them. |
| 5 | Threshold values | No default number in code. The spec declares at least one threshold with its source and how it was derived. A pre-sign spec critic (`spec_critique.py`) flags a spec whose only thresholds are multiples of GDP per capita and asks for a health-opportunity-cost estimate beside them (Woods et al. *Value Health* 2016; Ochalek et al. *BMJ Glob Health* 2018), as `data/world_model_trials/wtp.yaml` already notes. |
| 6 | Effect floor for the ratio guard | Relative 1e-6 of the reference's discounted health loss, plus absolute 1e-12, in `thresholds.yaml`. |
| 7 | South-west quadrant | Never a ratio. ΔC and ΔE separately, and the choice at each λ from NMB. |
| 8 | Productivity costs in the societal perspective | Not included unless declared with a method (human capital or friction cost) and a source; the societal label states whether they are included. |
| 9 | Savings and the allocation envelope | Savings are reported beside the envelope and never added to it. The envelope is programme money; a saving to hospitals does not fund a commodity this year. |
| 10 | ICER uncertainty | No ICER interval. ΔC and ΔE intervals, quadrant shares and the CEAC. |
| 11 | Allocation objective on the composed health measure | Allowed in ζ: `objective.outcome: cea:dalys` makes the plan maximise discounted DALYs averted. Until then the allocation keeps its engine outcome and the add-on reports the health measure beside it. |
| 12 | Standalone strategies that vary by unit | Not in the standalone kind (one strategy as declared). A strategy that differs by unit is an allocation; use the add-on. |
| 13 | Age at death within an engine age group | The spec declares a mean age per group with a source; without one, the group midpoint, stated as such, and a `health_weight`-style test on the group's ends. |
| 14 | Draw count for the CEAC and EVPI | Status `computed` from 100 draws; `indicative` below. Revisit against the RSV live check's Monte Carlo error. |
| 15 | Currency and price year of thresholds | Must equal `options.prices.base_year` and its currency; a mismatch is refused. A later PR may convert with the declared price index. |
| 16 | Transmission effects after the simulated stop (herd effects, infections averted later) | Not counted. The artifact states the truncation. A `time_horizon` test (a shorter `stop`) shows the direction where the engine supports it. |

## 13. Out of scope

Distributional cost-effectiveness (equity weights), budget-impact analysis beyond the per-year table of §5.9,
Fieller or bootstrap ICER intervals, half-cycle corrections for engines with discrete cycles (an engine's annual
series is taken as given), and multi-year re-decision (real options). Each can follow once the core is in use.
