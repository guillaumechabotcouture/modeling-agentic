---
phase: "F6"
commit: "α–δ"
---

## Changelog

**`vm_job wait --cap-usd` re-attaches a cost cap.** On 2026-11-13 a session restart killed the `run` that metered cluster 2375765 and it ran on with no cap. `wait <name> <job_id> --cap-usd N [--budget-scope S]` now checks that the cluster id recorded in job.json is live, reads that cluster's lifetime cost, and refuses unless it is under N and the signed budget of its scope covers used + other reservations + the rest of N; `budget.reattach_budget_ok` then replaces the cluster's reservation with one of the rest of N. It then attaches by that id and meters with `run`'s `_cap_reading`. It stops the cluster by id at the cap, after three failed readings (three before attaching too), once the job ends, or on any error. Without `--cap-usd`, `wait` is unchanged. Self-tests RN32a-h, BR22a-d.
