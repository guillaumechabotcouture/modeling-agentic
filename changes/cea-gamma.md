---
phase: "Cost-effectiveness deliverable (PR gamma)"
commit: "add CEA decision tests & switching-value bisection, perturb chosen_strategy/targets, allocation add-on (CE14), and CEA figure set"
---

## Changelog

**Third PR (`cea-gamma`) of the cost-effectiveness deliverable (`docs/design/cost-effectiveness-deliverable.md` §§6, 8–10, §11).**

- `toolkit/spec/perturb.py`: add `criterion: chosen_strategy` and the four cost-effectiveness targets (`discount_rate`, `health_weight`, `consequence_cost`, `timing`) with validation and compiler round-trip (`PT_CEA1`–`PT_CEA2`).
- `toolkit/analysis/cea.py` & `toolkit/analysis/run.py`:
  - Add `find_switching_bracket()` and `run_switching_tests()` (§6) evaluating discount-rate (0% both / 0% health only), timing (`start`/`end` bounds), and ranged health-weight / consequence-cost switching brackets on real evaluations (`CE_SW1`), wired into `analysis/cost_effectiveness.yaml`, `claims()`, and `results.md`.
  - Add `allocation_addon()` (§9, `CE14`) computing per-unit feasible frontiers, per-package totals, recommended plan vs comparator, and no-envelope NMB-optimal plans via `A.solve` (with consequence savings reported beside the envelope and LP `budget_dual` explicitly not used).
- `toolkit/analysis/figures.py`: add `DECISION_SETS["cost_effectiveness"]` (`ce_frontier`, `ce_plane`, `ceac`, `price_grid`, `threshold_price`, `evpi`, `switching`) and the allocation add-on figure `ce_by_package`, all drawn solely from `analysis/cost_effectiveness.yaml` (`F_CEA1`).
