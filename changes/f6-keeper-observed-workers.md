---
phase: "F6"
commit: "S2-KP1"
status_group: "F6 Stage 2"
---

## Changelog

**The keeper keeps the attached Cluster's worker view current, so autoscaling no longer retires busy workers.** On 2026-11-12 the P6 preview's first benchmark batch (42 runs of about an hour) took 3.4 h and resubmitted 17 runs; on the resumed launch the scheduler's own events showed workers retired by adaptive scaling with a `_remote_task` chunk still processing (`remove-worker` with non-empty `processing-tasks`), although `ma-compute-busy-guard` was registered on that scheduler. Read-only probes of the keeper process found the cause: its `coiled.Cluster` object observed 5 workers (the pool when it attached) while 31 were requested and the scheduler had 31; the keeper's own per-tick read of the scheduler showed 41. dask's `AdaptiveCore.recommendations` closes workers that are requested but not observed ("not yet arrived") without calling `workers_to_close`, so `BusyWorkerGuard.valid_workers_downscaling` was never consulted for them, and any shrink of the target retired workers in the middle of their chunks. `cloud/worker_side.py` `sync_observed` replaces the Cluster's worker view with the scheduler's table on every keeper tick (one reference swap). Why the Cluster's own worker-status stream stopped adding workers (its watch task was alive and its comm open) is not established; the fix does not depend on it. Self-test RC52k (`cloud/run_cluster.py`), written first, failed before the change. Live check on P6, with the owner's approval: the same table was written into the running keeper once at 16:38 UTC; in the ten minutes before, 3 of 8 retired workers were busy, and the removals after it were all idle. No other cloud run.
