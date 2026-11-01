---
phase: "v2"
commit: "N3"
---

## Changelog

**Launcher: a run that stops before the keeper ticks is reported, not a failed launch; a pool cannot start without a compute VM.** On 2026-11-01 the full v2 Nigeria run paused for owner sign-off about 5 minutes after launch and stopped its cluster. The keeper then exited by design, and `launch` failed with "not confirmed holding the pool". `confirm_keeper` now takes `run_alive`. When the run cluster has stopped, it reports that the keeper exited by design. A live run with an unconfirmed keeper still fails the launch. `RunConfig` now refuses `compute_start=0` when compute tasks need a resource tag (`MA_COMPUTE_RESOURCE`). A local check against distributed 2026.8.0 found that, with no tagged worker, compute.py's queued chunks and demand markers leave `adaptive_target` at 1, so the pool never grows. Self-tests RC52m-n and RC25b-c in `cloud/run_cluster.py`.
