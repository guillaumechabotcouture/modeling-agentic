---
phase: "F6"
commit: "S2-KP3"
status_group: "F6 Stage 2"
---

## Changelog

**Dispatch code in `scripts/compute.py` is not part of the batch-reuse fingerprint (owner, 2026-11-12).** PR #204 changed only how chunks are placed, retried and shown to the autoscaler. Yet it changed `code_fingerprint` for every stored batch: on the P6 preview's snapshot all five finished calibration batches hashed `4d089cdf7112` on the old base and `2e84f6b5e8d6` with #204, so a run on it would have recomputed about $200 of finished work. Only ok rows are reused, and a chunk's rows come from `_remote_task` and `_call`, so the placement, retry, scheduler-plugin and ledger code cannot change a reused row. `FP_DISPATCH_EXEMPT` names those top-level definitions (the dask dispatch loop `_map_dask`, `_sched_view`, the demand marker, BusyWorkerGuard, ComputeTagPlugin, the retry and adaptive constants, the ledger, progress and heartbeat functions). `_fp_compute_source` hashes compute.py with each of them, and the comment lines before it, replaced by a placeholder. Everything else in the file stays hashed: the model call path, `_remote_task`, the bundle, the worker environment, the breaker, `map_fn`, the fingerprint code and its constants, and every comment outside an exempt definition. A listed name that is not found stays hashed (FPX1 checks they all exist), and a file that does not parse is hashed whole. This change itself moves the fingerprint once: P6 and F6 stay on base 15b8b0d, and the exemption helps from the next run. Self-tests FPX1-FPX5 (`scripts/compute.py`), written first, failed before the change (`FP_DISPATCH_EXEMPT` undefined). No cloud run.
