---
phase: "F6"
commit: "S2-IR"
status_group: "F6 Stage 2"
---

## Changelog

**Lost chunks get three backed-off resubmissions, an infra-only joint-calibration batch runs once more, and the infra resume finds this run's joint-calibration batches without trusting mtimes (F6).** The F6 preview (2026-11-11) failed after 2.5 h: 1 of 42 benchmark tasks in `toolkit/analysis/joint_calibration.py` `_bench_batch` came back `kind: infra` ("chunk failed: its worker ... left the cluster") after `scripts/compute.py` had already resubmitted it within the call, `_bench_batch` raised on the failed row, and the stage machine did not offer the infra resume because `_infra_batch` read only `analysis/batch/` and chose this run's summaries by mtime. (1) `scripts/compute.py`: a chunk lost with its machine is resubmitted up to `MA_COMPUTE_INFRA_RETRIES` (now 3) times per input, each after a doubling backoff (`MA_COMPUTE_INFRA_BACKOFF_S`, 10 s first), recorded in the chunk's `backoff_s` and logged to stderr; a model failure is never resubmitted. (2) `joint_calibration.py`: when a benchmark batch's or a unit batch's only failures are infra, it is run once more on the same results file (finished rows are reused) before failing closed; a model failure, or infra that persists, still fails closed. `toolkit/analysis/batch.py` `run` reports `failed_by_kind`. (3) `scripts/pipeline.py` gives each run an id (`pipeline_run`, in its report and in its steps' environment as `MA_PIPELINE_RUN`), and `compute.py` writes it into each batch summary; `agents/stage_machine.py` `_infra_batch` scans `analysis/batch/` and `analysis/joint_calibration/` recursively and takes a summary as this run's when its `pipeline_run` equals the report's (the mtime rule remains for summaries without it), so a run dir copied or restored from a checkpoint is classified, and `main.py --resume` reopens it at ANALYZE (`resumable_failure` -> 'infra'). Self-tests were written first and failed before the change: IR5, IR5b, IR7 (`compute.py`, LocalCluster), JA24-JA24f (`joint_calibration.py`, fakes), T62 (`stage_machine.py`, a restored run dir with one infra row in a joint-calibration bench batch), PL3b (`pipeline.py`), CP7 (`compute.py`). No cloud call.

## Status

- **S2-IR:** three backed-off resubmissions, one rerun of an infra-only joint-calibration batch, and the infra resume across both batch dirs by pipeline run id.
