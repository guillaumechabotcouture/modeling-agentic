---
phase: "F6"
commit: "S2-6c"
status_group: "F6 Stage 2"
---

## Changelog

**A fixed plan compared with the optimum at its own cost (F6 S2-6c; project review finding A2).** A `fixed_plan` variant's `value_lost` compares it with the unconstrained plan solved within the headline plan's budget (the reserve when enforced), so it mixes a different allocation with a different spend. Each evaluated fixed plan's record in `analysis/plan_variants.yaml` now also carries `same_spend`: the optimum re-solved on the same point tables and the unconstrained (signed) feasibility with the budget set to the fixed plan's own point cost, guarded against the point model corrections as every other solve. It records `budget`, `objective`, `cost`, `value_gain` (its objective minus the fixed plan's), `value_gain_pct` (as a share of its objective), the move summary from the given plan to it, and `plan`; when no plan is feasible at that spend it is `status: not_solved` with the reason, and any other solver error stops the analysis. `value_lost` is unchanged. The plan-variants contract requires the block for every evaluated fixed plan and checks it against the record (budget equals the plan's cost, the optimum within it, value gain and its share recomputed). `results.md` states the equal-spend comparison for each fixed plan, with the claims `variant.<id>.same_spend_gain` and `same_spend_gain_pct`. The comparison is at the point estimate; the same-spend plan is not followed over the uncertainty draws. Self-tests PV37–PV40 (`variants.py`, PV37 against a brute force over the toy's packages) and PV-R14 (`run.py`). No change to a spec without a fixed plan.

## Status

- **S2-6c:** each fixed plan is also compared with the best plan at its own cost, so the value lost by following it is not confounded with a different spend.
