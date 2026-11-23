---
phase: "v2 autonomy"
commit: "W0-J ζ"
---

## Changelog

**A missing or blank Anthropic key refuses before any cluster; supervisor storage account checked (`cloud/run_cluster.py`, `cloud/budget.py`, `cloud/supervisor.py`).**

Codex round 1 on #358 found two problems:
- `run_cluster` launch created and recorded a run cluster before the missing key made the submission fail.
- An invalid `--account` reached the log after `vm_job up` had created the VM.

Changes:
- **`run_cluster.api_keys()`** (launch and relaunch) refuses a missing or blank `ANTHROPIC_API_KEY` before any cluster is created or task submitted. A launch refused there releases the reservation its budget check made, through the new `budget.release_reservation(invocation, name)`, which removes only that invocation's reservation for that name, under the ledger lock.
- **`supervisor.storage_account`** refuses an account that is not a bare name, from `--account` or the environment, without echoing it. It runs before any blob client is built and before `start` runs or prints any `vm_job` command.

Self-tests: RK1-RK3 (run_cluster), RR1-RR2 (budget), SV50-SV51 (supervisor).
