---
phase: "HB"
commit: "R α–β"
---

## Changelog

**Harness run records for the benchmark metric.** The v2 stage machine now writes the records `scripts/harness_metrics.py` otherwise has to infer. `Machine.run` appends a history entry `{"at", "paused", "state", "round", "spent_usd"}` whenever it stops paused (`awaiting_signoff`, `awaiting_scope_signoff`, `awaiting_spec_amendment`; `awaiting_budget` keeps its own entry from `_task`), so I1 and owner-wait are records for every pause kind. `run_v2` records on the invocation entry main.py appended (`scripts/lib/pipeline_id.py stamp`) the budget asked for (`budget_usd`), the cap in force (`cap_usd`, so a raise shows between invocations beside the existing `budget_raised` entry), the model ids and the Agent SDK version, and writes `models` and `sdk_version` at the top of `metadata.json`. `init_run` of a new run records `metadata.parent_run` (`run_dir`, `run_state_sha256` read through `lib.safe_io`) when the launcher sets `MA_PARENT_RUN` to the run it relaunches from; a `--parent-run` flag on main.py waits for v1 retirement. All writes go through `lib.safe_io`. Self-tests RR1-RR5 in `agents/stage_machine.py`.
