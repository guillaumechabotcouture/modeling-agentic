---
phase: "Harness baseline hb1"
commit: "fix 11"
---

## Changelog

**Expected-value plan as a spec option (hb1 triage fix 11, cause J).** A spec may now write `deliverable.plan: expected_value`. The analysis then delivers the plan with the best mean benefit over the uncertainty draws, within the budget in every draw and at the point costs, instead of the plan solved at the point values. It is the same solve as the value-of-information step's `robust` candidate: both call `toolkit/analysis/plan_rule.py mean_plan`. The draws are evaluated once and the uncertainty step reuses them. The allocation file then carries each unit's mean over those draws. The claims add `headline.mean` (the fixed plan's mean over the draws) and `plan_rule.units_differ`, and results.md says which rule was used. `point.yaml` records `plan_rule`: the point-value plan, its solve and the units that differ. Stability, sensitivity and the decision tests still re-solve at the point values, so they compare with the point-value plan, and the results text says so. The schema refuses an unknown value, and refuses `expected_value` without an uncertainty source, with `uncertainty.affordability.enforce`, on an `estimates` deliverable and with `by_group`. A spec without the key behaves as before: the same plan, allocation file, uncertainty files and claims, and no engine job hash changes. The decision-spec skill names the option. Self-tests: SPEV1-4 (schema), PR1-8 (plan_rule), EV-R1-6 (run).
