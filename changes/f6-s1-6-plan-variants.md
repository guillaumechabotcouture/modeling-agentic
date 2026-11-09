---
phase: "F6"
commit: "S1-6"
status_group: "F6 Stage 1"
---

## Changelog

**Operational plan variants (F6 S1-6).** The spec can ask for plan variants solved beside the unconstrained plan (`analysis.plan_variants: {variants: [...], headline: <id>}`, `toolkit/analysis/variants.py`): `minimum_service` (every unit receives at least one of the listed components, by default any component; a unit that cannot fails closed) and `group_rule` (one decision per group of a unit column for the listed components, through the existing `toolkit/alloc` group rules, rule `same_choice`). Each variant is solved on the point benefits and costs within the headline plan's budget (the reserved budget with `uncertainty.affordability.enforce`), and reports its objective, cost, value lost against the unconstrained plan (absolute and as a share of its objective), dollars moved between components and reallocated between units, units and population changing package, units changing by region group, and units with no package. A variant that the rule or the budget makes infeasible is reported as not solved, with why. The new optional `analysis/plan_variants.yaml` artifact holds them; claims `variant.<id>.*` and a `results.md` section report them. The headline plan is unchanged unless the spec says `headline: <variant>`: then the analysis's feasibility carries the variant's rule from the start, so the point plan, the affordability reserve, every re-optimisation (draws, stability, sensitivity, decision tests, including group-rule tests) and `figure_data.rebuild` solve under it; `point.yaml` records `headline_variant`, the unconstrained plan stays in the artifact, and a headline plan that is not the variant's solved plan fails closed. The spec's variants are validated (types, known components, unit columns, unique claim-safe ids, descriptive text, a headline that names a variant).

## Status

- **S1-6:** minimum-service and group-rule plan variants solved and reported beside the unconstrained plan, with value lost and dollars moved; a variant becomes the headline only when the spec says so.
