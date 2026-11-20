---
phase: "v2 debt"
commit: "warn, don't kill: pipeline steps"
---

## Changelog

**A pipeline step's time limit is a soft deadline; only a far-off backstop kills (owner decision, 2026-11-17).** Two F6 runs died on `scripts/pipeline.py`'s fixed 3600 s step limit while their analyze step needed 5–6 h. A step's `timeout_s` (default 3600 s, or `compute.step_wall_s` on a cluster) is now a soft deadline. Past it the runner warns on stderr and writes `pipeline_progress.json` every 15 min (elapsed, soft deadline, backstop and its time, a projection from the batch's rows or the last runtime), and the step runs on. The step is killed only at its backstop, `max(3 × soft, 24 h)`, inside any caller's own `budget_s`. A compute step's batch gets the backstop as its wall limit. The kill message still starts with `timed out after`, so the stage machine's resume still works. The progress file is a runner-owned record (`OWN_FILES`, `agents/enforcement.py`).
