---
phase: "F6"
commit: "S2-BG2"
status_group: "F6 Stage 2"
---

## Changelog

**Paid launches are refused unless the owner-signed budget covers them (F6 S2-BG2).** This builds on S2-BG1 (`cloud/budget.py`). `cloud/vm_job.py run`, `up` and `start` require `--cap-usd`. `--budget-scope` defaults to the scope whose prefix the name has. They refuse, before any cluster is listed or created or any job is submitted, unless `budget.launch_budget_ok` holds. `run` and `up` reserve the cap. Interactive `start` keeps a paid cluster busy, so it checks again without reserving, while `start` inside `run` is not checked twice. Each `run` poll saves the outputs, then reads the job cluster's USD (`budget.job_meter`, by that cluster's id) before it returns any state. At the cap, or after three failed readings in a row, the cluster is stopped by id, even under `--keep-on-failure` and even when the job has just ended. `RunConfig` gains `cap_usd` (required to launch, validated at load) and `budget_scope` (default resolved from `cloud/budget_scopes.yaml` by the name's prefix). `run_cluster.py` calls `budget_guard` on every path that creates or keeps paid work. `launch` reserves, before any key is read or cluster created. `keeper` reserves before its overflow pool and keeper clusters (`start_helpers`). `relaunch` does not reserve, and checks before it submits. The README and the `cloud-compute` skill name `--cap-usd`. Self-tests RN28-RN31 (`vm_job.py`) and BU1-BU5 (`run_cluster.py`) were written first and failed before each change. The strengthened RN29 checks two things: only the job's own cluster is read, and the output written just before the cap is saved. The earlier cases pass `--cap-usd` and a stand-in gate.

## Status

- **S2-BG2:** every vm_job and run_cluster path to paid work refuses without budget cover, and `run` stops at its cap even on a terminal state; a real cap stop still to be seen.
