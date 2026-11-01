---
phase: "v2"
commit: "port"
---

## Changelog

**main's scheduler-read fixes merged into v2 (#99 via main 61b311c, and #102's `fix-scheduler-reads`).** Two merge commits, no squash, no conflicts: v2 had not touched `cloud/run_cluster.py`, `cloud/worker_side.py`, `cloud/vm_job.py` or `scripts/replay_coiled.py`, and its `scripts/compute.py` changes (the local pool recycle, spike lesson 7) are in `_map_local`, away from the dask reads. v2's engine batch (`toolkit/analysis/batch.run`) reaches the guarded code as `compute.run_map` → `map_fn` → `_map_dask` → `_sched_workers`, which loads `cloud/worker_side.py` from the repo checkout. On Coiled, an unpopulated scheduler identity before the batch's first dispatch is now waited out (bounded), or the batch fails with `SchedulerInfoUnavailable` naming the last state, instead of `KeyError: 'workers'`.
