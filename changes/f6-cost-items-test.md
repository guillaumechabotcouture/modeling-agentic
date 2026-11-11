---
phase: "F6"
commit: "S2-7"
status_group: "F6 Stage 2"
---

## Changelog

**A price case as a decision test (F6 S2-7).** A new decision-test type, `cost_items` (`analysis.decision_tests: {id, type: cost_items, multipliers: {cost_item_id: m}, text}`), multiplies the unit costs of the listed cost items together, re-solves the plan at those prices with the benefits unchanged, and reports what the other decision tests report (units and population changing package, value lost by keeping the point plan, dollars moved, whether the point plan still fits the budget). The record and its one result carry `cost_multipliers`. It is for prices that move together, such as one supplier price list covering several products: the cost sensitivity (`analysis.sensitivity.cost_items`) varies one item at a time and the cost draws are independent per item, so neither can show a joint case. The schema refuses a test without `multipliers`, an unknown cost item and a multiplier that is not positive and finite (SP11g–j). Package pruning (`options.packages.within_envelope`) bounds each cost item at the lowest multiplier any such test applies, so a package the price case can afford is kept (SPJ7). The analysis self-test runs a case at the declared prices (no change) and a case that prices one component out of the plan (R5n1, R5n2). The run console names each cost item and its multiplier (DR9b). Codex round 1: a multiplier keyed by a numeric cost-item id is applied (the ids are kept as the cost items declare them), a price case with no affordable plan leaves a `not_run` record with its prices and the reason instead of stopping the analysis (other solver failures still stop it), and the tests check the re-solve uses every listed multiplier on its own item (R5n3–R5n6). No change to a spec without such a test.

## Status

- **S2-7:** a decision test can state one joint price case (several unit costs changed together) and re-solve the plan at it; the F6 cost amendment uses it for the 2026 net price list and for the upper SMC unit cost.
