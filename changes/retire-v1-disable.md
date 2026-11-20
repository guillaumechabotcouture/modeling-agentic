---
phase: "v1 retirement"
commit: "α"
---

## Changelog

**Pipeline v1 refuses to start or resume (owner decision, 2026-11-20).** `scripts/lib/pipeline_id.py` gains `RETIRED` and one message, "pipeline v1 is retired (owner, 2026-11-20); use v2": `select(v1=True)`, `from_flags`/`from_command` with `--v1` and `identity("v1")` raise it, and `resume_conflict` returns it for a run recorded as v1 or, with no record, one without `run_state.json`. `main.py --v1` prints it and exits 2 before the preflight or any run; `--resume` of a v1 run exits 1 with it; the v1 dispatch (`_run_v1`) is gone, and the rest of v1's code stays until the delete PR. `cloud/run_cluster.py` refuses a `RunConfig(pipeline="v1")` when it is built and again in `pipeline_preflight`; `cloud/vm_job.py` refuses a `main.py --v1` command with the same message; `scripts/eval_suite.py run --pipeline` accepts only v2. The two v1 launch configs, `cloud/configs/smoke.py` and `cloud/configs/e7r9.py`, are deleted (they could only launch v1). Old run records still read `pipeline: v1` (`label`). CLAUDE.md, README.md and the `cloud-compute` skill no longer tell anyone to use `--v1`. v2's dispatch, launch flags and preflight are unchanged; tests PI2/PI33c, PV4-PV7b, RP4/RP6/RP6b, VP2b and S19p failed before the change.
