---
phase: "HB"
commit: "R α–δ"
---

## Changelog

**Harness run records for the benchmark metric.** The v2 stage machine now writes the records `scripts/harness_metrics.py` otherwise has to infer. `Machine.run` appends a history entry `{"at", "paused", "state", "round", "spent_usd"}` whenever it stops paused short of FINAL (`awaiting_signoff`, `awaiting_scope_signoff`, `awaiting_spec_amendment`; `awaiting_budget` keeps its own entry from `_task`), so I1 and owner-wait are records for every pause kind. `run_v2` records on the invocation entry main.py appended (`scripts/lib/pipeline_id.py stamp`) the budget asked for (`budget_usd`), the cap in force (`cap_usd`, so a raise shows between invocations beside the existing `budget_raised` entry), the model ids and the Agent SDK version, and writes `models` and `sdk_version` at the top of `metadata.json` (read with no link followed; an unreadable file is left as it is). `init_run` of a new run records `metadata.parent_run` (`run_dir`, `run_state_sha256` read through `lib.safe_io`, null with the error when unreadable) when the launcher sets `MA_PARENT_RUN` to the run it relaunches from; a `--parent-run` flag on main.py waits for v1 retirement. `harness_metrics.py`: a recorded pause is ended only by a later resume, an older build's unrecorded pause before a recorded one in the same visit is still inferred, and a `parent_run` whose hash is null or differs is never linked. Self-tests RR1-RR7 (`agents/stage_machine.py`), HM39-41.
