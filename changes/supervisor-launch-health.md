---
phase: "v2 autonomy"
commit: "W0-J η"
---

## Changelog

**Supervisor launches (`cloud/supervisor.py` `enqueue-launch`).** The off-laptop supervisor can now launch eval configs itself. It runs as one long job: `--max-hours` defaults to 168, and there is no hand-over to a successor (owner, 2026-11-23).

`enqueue-launch cloud/configs/eval_<q>.py --tag T --reps N --commit SHA` is checked on the laptop first. The config must load with that tag and those reps and be in budget scope `ev`. The commit must resolve to a full sha. The signed budget record of the scope the launch uses goes with the request, and only when `budget.py`'s checks pass on the laptop: signature, scope, expiry, and the high-water mark against a rolled-back record.

For each request the loop makes its own clean checkout of the commit. It clones afresh, and clones again whenever anything in the checkout differs from the commit, ignored files included. It installs the budget record only when it verifies on the VM, and otherwise keeps the record it had. It then runs `run_cluster.py <checkout>/<config> launch` with `EVAL_TAG`, `EVAL_REPS`, `EVAL_TREE` and `MA_LAUNCH_INVOCATION=sv-<request>`, which is run_cluster's own launch code and budget guard.

The request is recorded as launching before the launch process starts. A request found launching after a restart is never launched again. A launch process running past 45 minutes is read as run_cluster exit 4. On exit 0, 3, 4 or 5 the loop records the launch id, cluster ids and assignments and queues the launch for collection. On any other exit it stops only this invocation's own failed record's clusters, by id. The loop's sleep is bounded by the next due check.

Fixes from Codex round 1 on #358:
- `run_cluster` launch and relaunch refuse a missing `ANTHROPIC_API_KEY` before any cluster is created or recorded (`api_keys`).
- Without a `.env` file, `read_keys` reads the keys from the environment.
- `vm_job --secret-env` allows `DASK_COILED__TOKEN` and `ANTHROPIC_API_KEY`. It reads them where the launcher keeps them (dask config, `.env`) and never copies them into its own environment.
- The supervisor refuses a storage account that is not a bare name, whether from an argument or the environment, before it builds any blob client. The value is not echoed.

Health checks and stop rules follow in a separate change; until then a request with stop rules is refused. Self-tests: SV25-SV30, SV41-SV46 (supervisor), VS6-VS6b (vm_job), RK1-RK4 (run_cluster).
