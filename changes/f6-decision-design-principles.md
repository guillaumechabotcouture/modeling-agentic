---
phase: "F6"
commit: "S2-DP1"
status_group: "F6 Stage 2"
---

## Changelog

**The owner's decision design principles are the default of every v2 task (owner, 2026-11-12).** After the F6 canary's critique (net-type effects, prices, a regional net result, burden bias), the owner set how every run is to be designed. The principles are:
- 0: think from first principles, and keep doing so;
- 1: start from the decision and decompose it into burden, relative effect and cost;
- 2: reprogramming from the decision date, against the plan as funded, with delivered actions fixed;
- 3: anchor burden to external data-driven estimates, relative effects to trials, and costs to current prices;
- 4: trials win over the engine unless the engine has an evidenced explanation, and inputs are never tuned to an expected answer;
- 5: measured data over modelled surfaces;
- 6: decision-moving inputs as ranges with decision tests;
- 7: decide at the granularity the evidence supports, with regret against the funded plan and price thresholds.

They live in one file, `.claude/orchestration/decision_design_principles.md`, the source of truth. `agents/v2_sdk_tasks.py` loads it into `COMMON` (every task's system prompt: spec drafting, engine choice, critique, fixes, writing), and `agents/fix_loop.py` appends it to `FIX_PLANNER_PROMPT`. `CLAUDE.md` summarises it for v1 agents. The text names no disease, place or intervention. Self-tests DP1-DP2 (`agents/v2_sdk_tasks.py`), written first, failed before the change: every task prompt carries the principles, and the file stays disease-agnostic. PF1-PF4 (prompt format, the planner's prompt byte for byte) pass. The harness changes principle 3 needs (burden anchoring, trial-calibrated effects) are separate PRs. No cloud run.
