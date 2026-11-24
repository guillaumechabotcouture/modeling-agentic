---
phase: "Harness baseline hb3"
commit: "exact-envelope, ev+by_group, lead critic-HIGH policy"
---

## Changelog

**Exact-envelope allocation, `expected_value` with `deliverable.by_group`, and lead critic-HIGH sign-off policy.** Adds `constraints.envelope_exact: true` across `toolkit/alloc/optimize.py` (exact-cost dominance pruning, cancellation-safe lower-bound pruning, ILP/brute lower bounds, exact `check()`, greedy refusal), `toolkit/spec/schema.py` (validation + refusal of uncertain `costs`, `affordability.enforce`, `per_outcome` cost items, and `sensitivity.cost_items`), `toolkit/analysis/{core,moves,figure_data,decision_tests,value_of_information,plan_rule}.py`, and `decision-spec` guidance so fixed already-held supplies allocate the full total. Removes the schema refusal combining `deliverable.plan: expected_value` with `deliverable.by_group`, recording draw-averaged per-group outcomes and quantities (`per_unit_group_mean`, `per_unit_group_quantity`, and `per_unit_group_other_mean` for subsets) in `plan_rule.py`, verifying them at the completion gate, and writing them to the by-group CSV in `run.py`. Enforces the 2026-11-24 owner policy under `--signoff lead`: `agents/stage_machine.py`, `agents/findings_ledger.py`, and `scripts/scope_signoff.py` refuse provisional lead acceptance for open critic `HIGH` findings (including escalated findings and malformed/missing severities), pausing at `OWNER_DECISION` with the refusal reason in `paused_why`. Self-tests `TEX1`–`TEX3b`, `SPEV6`, `SPEX1`–`SPEX2`, `PR13`–`PR14`, `T_LEAD_NO_CRITIC_HIGH*`.
