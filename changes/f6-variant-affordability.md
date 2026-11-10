---
phase: "F6"
commit: "S2-6b"
status_group: "F6 Stage 2"
---

## Changelog

**A solved plan variant's cost over the uncertainty draws (F6 S2-6b; project review finding A6).** Until now only the headline plan and a `fixed_plan` variant were followed over the uncertainty draws, so a rule variant (`minimum_service`, `group_rule`) reported one point cost and nothing said how often it would cost more than the budget. Each rule variant that solves on the point tables is now followed over the same draws (`variants.solved_plans`, collected by `variants.Draws` through `UQ.run`'s `on_draw` hook): its record in `analysis/plan_variants.yaml` carries `uncertainty` with its outcome and cost quantiles and `affordability` (`draws`, `draws_over_budget`, `share_over_budget`, and `rule_met` against the spec's affordability rule). The budget reserve is set for the headline plan, so a variant can fail the rule; it is reported as failing and is not re-solved with a larger reserve. A `fixed_plan` variant keeps `rule_met` empty, because a plan given in full is evaluated, not held to the rule. `results.md` states each solved variant's median cost, its draws over budget and whether it meets the rule; the claims `variant.<id>.averted_median`, `cost_median` and `draws_over_budget` now exist for solved variants too. The analysis stops if the draws followed a different plan from the one the variant solved to. Codex round 1: a variant solved after the draws but not followed over them stops the analysis instead of being published without its uncertainty, and the plan-variants contract recomputes each affordability record from its own per-draw costs (draw counts, draws over budget, the spec's rule or none for a fixed plan, one budget across variants) and refuses a solved variant missing its draws when others have them (PV34–PV36). Self-tests PV31–PV33 (`variants.py`) and PV-R13 (`run.py`); the first commit names them PV28–PV30 and PV-R11, ids that were already taken. No change to a spec without plan variants.

## Status

- **S2-6b:** each solved plan variant reports its cost over the uncertainty draws and whether it meets the affordability rule, as the headline plan does.
