---
phase: "F6"
commit: "S2-BG2"
status_group: "F6 Stage 2"
---

## Changelog

**Paid launches are refused unless the owner-signed budget covers them (F6 S2-BG2).** This builds on S2-BG1 (`cloud/budget.py`). `cloud/vm_job.py run` and `up` require `--cap-usd`. `--budget-scope` defaults to the scope whose prefix the name has. Both refuse, before any cluster is listed or created, unless `budget.launch_budget_ok` holds. Each `run` poll reads the job cluster's USD (`budget.job_meter`). At the cap, or after three failed readings in a row, `run` keeps the outputs saved locally so far and stops the cluster by id, even under `--keep-on-failure`. `RunConfig` gains `cap_usd` (required to launch, validated at load) and `budget_scope` (default resolved from `cloud/budget_scopes.yaml` by the name's prefix). `run_cluster.py launch` calls `budget_guard` before any key is read or cluster created. The README and the `cloud-compute` skill name `--cap-usd`. Self-tests RN28-RN30 (`vm_job.py`) and BU1-BU4 (`run_cluster.py`) were written first and failed before the change. The earlier `run` cases pass `--cap-usd` and a stand-in gate.

## Status

- **S2-BG2:** `vm_job.py run`/`up` and `run_cluster.py launch` refuse without budget cover, and `run` stops at its cap; a real cap stop still to be seen.
