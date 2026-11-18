---
phase: "v2 pre-sign checks"
commit: "resume smoke"
---

## Changelog

**A resume smoke checks, on the code a launch ships, that an interrupted run resumes without recomputing engine rows and with its signature intact.** New `scripts/resume_smoke.py <launch tree>` runs the tree's own copy of itself (`python -I`, cwd the tree) on three toy scenarios, locally, in about a minute. `analyze_timeout`: the batch step exceeds its limit with 3 of 6 rows done. `killed_batch`: the step's process group is killed mid-batch. `cap_stop`: the per-call compute allowance stops the batch. In each it asserts that the rows reused equal the ok rows the interrupted attempt kept, that none is recomputed (the toy function logs every call) and that the resumed batch is complete. For the two stage-machine scenarios, the run is signed by the lead at SIGN, `--resume` reopens it at ANALYZE and finishes, and the sign-off record is unchanged and still binds the spec's sha256. The stage machine does not treat a cap stop as resumable (`analysis_failure_cause` accepts infra only), so `cap_stop` is checked at the batch level, as a relaunch reruns it, and the report says so. The `cloud/run_cluster.py` docstring documents it as a step before any paid launch.
