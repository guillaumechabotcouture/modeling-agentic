---
phase: "F6"
commit: "S2-LE"
status_group: "F6 Stage 2"
---

## Changelog

**The launch watch enforces the owner's rules in code: dollar caps from the budget's own estimator, checkpoints for v2 runs, and the signed budget before every launch (F6).** On 2026-11-11 an agent spent $106 of a $100 budget: the cap was in credits, the dollar ratio and the checks lived in judgment. `cloud/launch_watch.py` now (a) refuses a v2 config whose `checkpoint_minutes` is 0 or over 30 (exit 9); `--allow-no-checkpoints` lifts that only when `run_timeout_hours` <= 0.5 (a smoke). (b) Takes its cap in dollars only: `--cap-usd` is required, and `--cap`, `--usd-per-credit` and `--credits-per-instance-hour` are gone, with the billing-event credit sum and the per-instance credit projection. Every interval, whatever the listing did, the launch's spend is `cloud/budget.py`'s estimator (`job_meter(scope)`: per cluster the larger of billed, total_cost × $0.061 + cloud_total_cost, and its conservative per-instance estimate) summed over the cluster ids `clusters.json` records; a reading that fails is unknown, and three failed readings in a row count as the cap: final checkpoint, one collect, every cluster of the launch stopped by id, exit 11 (spend unreadable); one or two stop nothing (LW25a-LW25b, LW6c2). The final checkpoint before the cap stop is unchanged. (c) Before the launch it calls `budget.launch_budget_ok(budget_scope, the config's cap_usd if set else --cap-usd, name=the config's name)`; a refusal, a missing module, the stub's NotImplementedError or any error refuses (exit 10), and so does a config with no `budget_scope`. `cloud/budget.py` is added as a stub that refuses both calls, to be replaced whole by the real module (#192); until then no launch passes the watch. (d) The watch stops at the smaller of `--cap-usd` and the config's `cap_usd`. `RunConfig` gains `budget_scope` and `cap_usd` (positive when set). Self-tests were written first and failed before the change: LW3a, LW10b, LW20a-LW20b, LW21a-LW21b, LW22a-LW22b, LW23 (`launch_watch.py`; the budget is a fake with #192's interface, never the stub), B0 (`cloud/budget.py` stub). No cloud call.

## Status

- **S2-LE:** the launch watch refuses v2 configs without checkpoints, caps in dollars from the budget's estimator, and checks the signed budget before launch (stub until #192); a real smoke after #192 merges.
