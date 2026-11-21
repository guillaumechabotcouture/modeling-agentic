---
phase: "v1 retirement"
commit: "β"
---

## Changelog

**The v1 stage-replay lane and the lead session's SDK and watch tools are deleted (9 files, 3,938 lines).** `scripts/replay.py`, `replay_coiled.py`, `replay_compare.py`, `stage_scorers.py`, `score_critiques.py`, `telemetry.py`, `agents/output_caps.py` (experiments E1, E2 and E5), `scripts/sdk_scenarios.py` and `scripts/watch_run.py` (the lead's heartbeat) are deleted. The E5 deterministic arm that CI's `scripts/regression_eval.py` runs, `deterministic_critic`, moves into that file unchanged apart from being synchronous (R6; the regression baseline still passes). `cloud/run_cluster.py` RC60f compares the two remaining workspace names; `scripts/self_tests.py`, `write_audit.py` and `import_audit.py` drop the deleted files. `docs/design/v1-retirement.md` is the inventory behind this and the next delete PR: what only v1 uses, what v2 shares (the legacy gate's validator and every gate it runs, `run_finish.gate_command`, `run_budget`, `enforcement`), what stays for an owner decision (`agents/integrity.py`, `pipeline.harness_build`, the enforcement agent policy, `--preflight-only`, every skill), and the fingerprint consequence of touching `scripts/`. No v2 module imports any deleted file.
