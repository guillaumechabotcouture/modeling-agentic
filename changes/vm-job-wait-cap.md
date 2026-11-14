---
phase: "F6"
commit: "α–β"
---

## Changelog

**`vm_job wait --cap-usd` re-attaches a cost cap.** On 2026-11-13 a session restart killed the `run` that metered cluster 2375765 and it ran on with no cap. `wait <name> <job_id> --cap-usd N [--budget-scope S]` now checks that the cluster id recorded in job.json is live, reads that cluster's spend so far, and refuses unless the spend is under N and the signed budget of its scope covers it plus the rest of N. It then attaches by that id and meters with `run`'s `_cap_reading`. It stops the cluster by id at the cap, after three failed readings (three before attaching too), once the job ends, or on any error. Without `--cap-usd`, `wait` is unchanged. Self-tests RN32a-g.
