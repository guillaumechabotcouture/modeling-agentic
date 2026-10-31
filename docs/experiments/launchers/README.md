# Coiled launchers (E7 attempt 7)

**New runs use `cloud/run_cluster.py` with a config in `cloud/configs/`** (one library
for launch, status, collect, shutdown and the keeper; the cluster stops itself once every
run is stored). The scripts below are the record of how attempts up to 7 were launched.

While a run is live, `python cloud/run_cluster.py <config> health --loop` checks it every
5 minutes (it connects to the existing cluster only and cannot create one) and appends anomalies to `<out_dir>/health.log`: compute queued while
compute VMs sit idle, a gate-finding spike, a `session_error`, no heartbeat or tool use, early
spend, and compute charged with no completed row. Thresholds: `cloud/run_health.yaml`.

Versioned copies of the scripts that start and manage cloud runs, so they are reviewed with the
harness. They run from `~/modeling-agentic-keep/coiled_run/` (they write their outputs next to it);
keep the two copies identical when changing either.

| Script | Purpose |
|---|---|
| `e7r7_cluster.py` | Attempt 7: `launch`, `brief`, `sign`, `report`, `status`, `collect`, `shutdown`. One agent VM plus an autoscaling compute pool (Phase 30). `E7R7_SMOKE=1` runs the same path on a tiny question with Haiku agents. Ships only `origin/main` (except in smoke mode). |
| `pool_keeper.py` | Holds the pool's autoscaler: dask's Adaptive runs in the process that calls `adapt`, so the pool stays at its floor without it (autoscale check 7). Attach-only; reconnects; if it stops, the pool keeps its floor. Run under `caffeinate -i`. |
| `ab_coiled.py` | Shared helpers: key reading from `.env` (values never printed), code tarball, `run_job_impl` (installs the pinned SDK, runs `main.py`, stores results). |
| `dev_vm.py` | Warm dev VM for real-SDK scenarios (`scripts/sdk_scenarios.py`). |
| `autoscale_check7.py` | Detached autoscaling check at the attempt-7 scale (VM cost only). |
| `pipeline_smoke_p34.py` | Two-round whole-pipeline smoke with a session crash (Haiku agents, lead sign-off). |

Rules these follow: the IDM Coiled default workspace only (not the prod subscription); attach-only
for anything but `launch` (never `coiled.Cluster(name=...)` on a cluster that may be gone: that
creates a new one); every cluster shut down after collection; no answer keys (`_truth/`) shipped.
