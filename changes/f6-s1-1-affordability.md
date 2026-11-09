---
phase: "F6"
commit: "S1-1"
status_group: "F6 Stage 1"
---

## Changelog

**Affordability under uncertainty (F6 S1-1).** The uncertainty step records the point plan's cost in every draw, beside its outcomes; costs move with treated cases and unit prices, so a plan within the budget at the point values can exceed it in a draw. `analysis/uncertainty.yaml` and `uncertainty_report.yaml` report `affordability`: the allocatable budget, the draws over it, their share and the cost quantiles. An optional signed-spec rule `uncertainty.affordability.max_share_over_budget` is checked by the analysis contract; a plan above it is the gate finding `analysis_contract_affordability`. Without a rule the share is reported, not judged. The report's harness numbers table and the claims ledger carry the share and the cost quantiles. Retro-check on F5: the plan exceeds the allocatable budget in 48 of 64 draws. An optimiser mode that meets the rule (reserve or chance constraint) follows once the owner sets the rule (F6 plan, O3).

## Status

- **S1-1:** affordability reported and gated by an optional spec rule; optimiser mode pending owner decision O3.
