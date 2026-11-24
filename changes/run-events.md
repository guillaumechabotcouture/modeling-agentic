---
phase: "Diag"
commit: "EV α"
---

## Changelog

**The run event stream: `events.jsonl`.** Each v2 run appends one versioned JSON object per line to `events.jsonl` in its run directory (`agents/run_events.py`; schema in `docs/design/diagnostic-data.md` §7): `run_start`, `stage` (every transition, with the time spent in the state left), `round`, `task_start` / `task_end` (key, task, elapsed, spend, model), `heartbeat` (every `MA_EVENTS_HEARTBEAT_S`, default 300 s, while a paid task or the analysis pipeline runs, with the pipeline's ETA when it has one), `gate`, `finding` (each status change, from `findings_ledger.save`), `owner_question` (a pause), `owner_answer` (the run leaving the state it paused in) and `status`. One place per kind in `agents/stage_machine.py`. Appended through `lib.safe_io` (no link followed), at most 4 KB a line, never fatal: a failed append is counted (`dropped_before` on the next event, `run_state.json` `events_dropped`). Outside every hashed record: not a pipeline step input (`scripts/pipeline.py` excludes it like `pipeline_progress.json`), a harness file no task may write (`v2_sdk_tasks.HARNESS_FILES`) and never an agent's change (`fix_loop._HARNESS_OWNED`). Self-tests EV1-EV12 and EVS1-EVS7. Smoke markers: `events.jsonl~"kind": "stage"`, `events.jsonl~"kind": "task_end"`, `events.jsonl~"kind": "status"`, `run_state.json:events_dropped=0`.
