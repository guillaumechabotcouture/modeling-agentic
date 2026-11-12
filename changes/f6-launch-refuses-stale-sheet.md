---
phase: "F6"
commit: "S2-LN1"
status_group: "F6 Stage 2"
---

## Changelog

**A launch refuses a resumed run at SIGN whose sign-off sheet is stale.** On 2026-11-12 a canary relaunch stopped in 8 s at SIGN (exit 2, about $5 and 20 min lost). Its decision spec had been amended after its sign-off sheet was generated, and the signer refuses a sheet that records another spec. The pre-launch checks never looked at the sheet. `cloud/run_cluster.py` `stale_sheet_problems` reads, from the code archive that ships, each `--resume` run dir that is still at SIGN. It compares the sheet's recorded `spec_sha256` and `derivations_sha256` (the last-line sources `toolkit/spec/signoff.py` `write_sheet` records) with the shipped `decision_spec.yaml` and `data/derivations_record.json`. The launch is refused, before any cluster or key, with the command that regenerates the sheet. Fail closed: a run at SIGN with no sheet, or a sheet without recorded sources, is refused. A run past SIGN, or a launch that resumes no run, is unaffected. After the review (two reviewers, the same four P2s): v1 launches are not checked (they have no run_state.json or sheet), `--resume=<dir>` and `./` paths are read and normalised as main.py resolves them, and an unreadable run_state.json is refused rather than skipped (RC80d, written first). Self-tests RC80a-c, written first: they failed before the change (the function was undefined). Retro-check on the real launch trees: the failed launch's archive (79ba483) is refused (sheet 2addcb872b1a, shipped spec d54d6a9aa9b8); the running canary's (1429af9) passes. No cloud run.
