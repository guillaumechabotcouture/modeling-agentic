---
phase: "Auto 1D-i"
commit: "α-β"
---

## Changelog

**Pipeline runner durability (v2 autonomy 1D-i).** `scripts/pipeline.py`: at a step's backstop the runner sends SIGTERM to the step's process group, waits up to `TERM_GRACE_S` (25 s) for every process in the group to end, then sends SIGKILL and waits a bounded `KILL_GRACE_S` (`_stop_group`); before, the kill was an immediate SIGKILL. A step's stdout and stderr are appended to `models/logs/<step id>.log` through `lib.safe_io` (no link followed), the last 800 characters are kept in the report row's `error` on failure and on timeout, and the row names the file (`log`, or `log_error` when it could not be opened; the step still runs). Output goes to a file, never a pipe. The log is never a step input and is outside the compute code fingerprint (self-tests PD5, PD5b). `run()` resets its run id when it ends or raises; a late progress report keeps the id of the run that started the step. `script_env()` drops the model API credentials (`SECRET_ENV`) and puts the harness interpreter's directory first on `PATH`. Self-tests PD1-PD10. Not in this change: `argv` steps (1f), the derivation memory cap (1g), the reserved-file exemption for harness steps (1d, and with it the wider rollback of 1e), and the budgeted-resume rule (1l).
