---
phase: "Cost-effectiveness deliverable (PR alpha)"
commit: "add cost_effectiveness spec schema, floor=False cost_matrix offsets, thresholds, and deterministic CEA core (CE1-CE3, CE7, CE11-CE13)"
---

## Changelog

**First PR (`cea-alpha`) of the cost-effectiveness deliverable (`docs/design/cost-effectiveness-deliverable.md` §§4–5, §10–§11).**

- `toolkit/spec/cea.py` and `toolkit/spec/schema.py`: add standalone `deliverable.kind: cost_effectiveness` and allocation add-on `cost_effectiveness:` section validation (`strategies`, `reference`, `perspectives` with societal non-health-system requirement, `time_horizon`, `discounting` consistency, `health_measure` DALYs/QALYs + life table bounds, `thresholds`, `consequence_costs` with double-count guard, `payers`, `prices` with negotiated-vs-drawn-cost guard, and conditional `health_weights` uncertainty kind). Self-tests `CE_SPEC1`–`CE_SPEC5`.
- `toolkit/alloc/costs.py`: add `floor: bool = True` to `item_cost()` and `cost_matrix()` so cost-effectiveness evaluations can count negative incremental per-outcome offsets as savings (`floor=False`) while allocation budgets retain their zero floor (`floor=True`). Self-test `TC35`.
- `toolkit/analysis/thresholds.yaml`: add top-level `cost_effectiveness` threshold parameters (`effect_floor_rel`, `effect_floor_abs`, `nmb_tie_rel`, `min_draws`, `max_switch_evaluations`, `switch_bracket_rel`).
- `toolkit/analysis/cea.py`: implement deterministic cost-effectiveness computation — uniform-accrual and start-of-year discounting, Murray (1994) continuous discounted YLL, strong and extended dominance frontier, Stinnett & Mullahy (1998) NMB/frontier consistency check, five-case north-east degenerate-ratio guard, evaluated price grids and Brent-bracketed threshold prices cross-checked against closed-form linear roots, and per-perspective total composition. Self-tests `CE1`–`CE3`, `CE7`, `CE11`–`CE13`.
