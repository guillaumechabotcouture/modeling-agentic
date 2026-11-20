---
phase: "F6"
commit: "S2-BG5"
status_group: "F6 Stage 2"
---

## Changelog

**A live cluster missing one cost field is counted, not refused.** On 2026-11-11 the canary's watcher read "cloud_total_cost is not a number: None" twice in a row on its own live cluster, which had a credit total but no Azure cost yet. Three such readings count as the cap, so a gap in Coiled's billing record would stop a healthy run. A live cluster with `total_cost` or `cloud_total_cost` missing now counts max(the part billed, instance-hours × max(median, the VM type's bound)), with a warning in its `assumptions`. The bound already covers both the Azure VM and the Coiled credits (`cloud/vm_prices.yaml`), so the count stays an upper bound. The partial bill is left out of the measured median rate. Stopped clusters are unchanged (BR8e). Self-test BR8f (`cloud/budget.py`) was written with the change (not run against the old code first); the BR9 case that expected a refusal for this input is removed. No cloud run.
