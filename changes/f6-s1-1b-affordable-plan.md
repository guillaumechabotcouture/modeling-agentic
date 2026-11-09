---
phase: "F6"
commit: "S1-1b"
status_group: "F6 Stage 1"
---

## Changelog

**Affordable plan under uncertainty (F6 S1-1b, O3).** `uncertainty.affordability` takes `enforce: true` beside `max_share_over_budget` (enforce alone is refused). With it, the analysis evaluates the uncertainty step's draws once (same Drawer, seed and number of draws) and searches for the largest budget fraction f in [0.5, 1] whose plan, solved at f × the allocatable budget, costs more than the full allocatable budget in at most the stated share of draws: the unconstrained plan first, then the floor, then bisection to a tolerance of 0.005 with at most 12 ILP solves, each a recorded step. A candidate plan's cost per draw is a sum over the precomputed cost tables. The headline plan becomes that plan; every later re-optimisation (draws, stability, sensitivity) uses the same reserved budget, while affordability is judged against the full budget. `analysis/point.yaml` records `reserve`: f, the dollars held back, the unconstrained and affordable plans' objectives, costs and shares over budget, the value given up (absolute and %), and the search steps; claims `reserve.*` and `api.headline` carry the reserve and the value given up. The uncertainty step reads the same evaluations, so its `affordability` reports the affordable plan and the S1-1 contract check passes. No fraction at or above the floor meeting the rule blocks the analysis (`analysis/blocked.yaml`, where `affordability`). Without `enforce` the analysis is unchanged.

## Status

- **S1-1b:** optimiser mode that meets the affordability rule (budget reserve), on owner decision O3 ("keep within budget"); the owner sets the share at sign-off.
