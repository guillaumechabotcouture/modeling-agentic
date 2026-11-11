---
phase: "F6"
commit: "S2-DT"
status_group: "F6 Stage 2"
---

## Changelog

**Two dev tools: a Codex review that cannot hang silently, and a command kept awake (F6 S2-DT).** `scripts/codex_review.py` runs `codex exec` with stdin from /dev/null and `--sandbox read-only`, always. An extra argument that touches the sandbox, approvals or the session file is refused. It writes the last message to `--out` and everything codex prints to `<out>.log`. Within 90 s a new session file must appear under `~/.codex/sessions/<YYYY>/<MM>/<DD>/` and grow. If not, codex is killed and started once more, and a second failure exits non-zero, saying what was seen. `scripts/keep_awake.py` runs a command under `caffeinate -i` on macOS, and as is elsewhere, and returns its exit code. Self-tests CR1-CR5 (a fake codex binary and session directory) and KA1-KA3 were written first and failed before the change.

## Status

- **S2-DT:** `scripts/codex_review.py` and `scripts/keep_awake.py`; not yet run against a real `codex exec`.
