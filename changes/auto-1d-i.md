---
phase: "Auto 1D-i"
commit: "α-δ"
---

## Changelog

**Pipeline runner durability (v2 autonomy 1D-i).** `scripts/pipeline.py`: at a step's backstop the runner sends SIGTERM to the step's process group, waits up to `TERM_GRACE_S` (25 s) for every process in the group to end, then sends SIGKILL and waits a bounded `KILL_GRACE_S` (`_stop_group`); before, the kill was an immediate SIGKILL. A step's stdout and stderr are appended to `models/logs/<step id>.log` through `lib.safe_io` (no link followed), the last 800 characters are kept in the report row's `error` on failure and on timeout, and the row names the file (`log`, or `log_error` when it could not be opened; the step still runs). Output goes to a file, never a pipe. A `.log` file directly in `models/logs/` is reserved for the runner: never a step's input, output or code (`_is_step_log`), and outside the compute code fingerprint (self-tests PD5, PD5b, PD12). `lib.safe_io.open_append_nofollow` opens with `O_NONBLOCK` (cleared before use), so a FIFO at the name never blocks an append (PD11). `run()` resets its own run id when it ends or raises; a late progress report keeps the id of the run that started the step. `script_env()` drops the model API credentials (`SECRET_ENV`) and puts the harness interpreter's directory first on `PATH`. Self-tests PD1-PD13. Not in this change: `argv` steps (1f), the derivation memory cap (1g), the reserved-file exemption for harness steps (1d, and with it the wider rollback of 1e), and the budgeted-resume rule (1l).
