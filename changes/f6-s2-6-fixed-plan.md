---
phase: "F6"
commit: "S2-6"
status_group: "F6 Stage 2"
---

## Changelog

**A plan given in full, evaluated beside the optimised plan (F6 S2-6).** A new plan variant type, `fixed_plan` (`analysis.plan_variants.variants: {id, type: fixed_plan, column, text}`, `toolkit/analysis/variants.py`), reads each unit's package id from a column of the decision-units table and evaluates that plan as given, never solving it, on the same point benefits and costs as the headline plan. It reports the fields the rule variants report (value lost against the unconstrained plan, absolute and as a share; dollars moved between components and reallocated between units; units and population changing package; units with no package), plus its cost, its cost minus the allocatable budget (a plan over or under the budget is reported, never refused for it), the units given a package the signed constraints rule out, with why (kept in the plan and evaluated as given, not dropped), and the caps and group rules it breaks. Its outcome and cost quantiles over the uncertainty draws are reported the way `uncertainty.yaml` reports the headline plan's (`affordability()`), through `UQ.run`'s `on_draw` hook, which now passes each draw's cost table. Model corrections bearing on the plan, at the point or in a draw, stop publication as for every other plan. The spec refuses a missing column, an empty or unknown package id, and a fixed plan as `headline`; evaluation fails closed on the same. Claims `variant.<id>.cost`, `.budget_difference`, `.ineligible`, `.averted_median`, `.cost_median` and `.draws_over_budget` and a `results.md` paragraph report it; the analysis contract accepts status `evaluated` only for a fixed plan and requires its eligibility report and budget difference.

## Status

- **S2-6:** a plan given in full (for F6, the funded plan) is evaluated with the same model and costs beside the optimised plan, with its cost against the budget, its ineligible units and its draws.
