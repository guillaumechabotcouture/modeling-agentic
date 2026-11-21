---
phase: "F6"
commit: "S2-DT"
status_group: "F6 Stage 2"
---

## Changelog

**Two dev tools: a Codex review that cannot hang silently, and a command kept awake (F6 S2-DT).** `scripts/codex_review.py` runs `codex exec` with stdin from /dev/null, `--sandbox read-only` and `--skip-git-repo-check`, always. An extra argument that touches the sandbox, approvals or the session file is refused. It writes the last message to its output file and everything codex prints to `<output>.log`. Within 90 s a new session file must appear under `~/.codex/sessions/<YYYY>/<MM>/<DD>/` and grow. If not, codex is killed and started once more, and a second failure exits non-zero, saying what was seen. A pull request review (`--pr N`, or `--head REF --base REF`) does what builders did by hand: the head commit is exported with `git archive` to a temporary directory (tracked files only, no `.git`; removed afterwards), the diff under review is written there as `PR.diff` (`git diff base...head`), and codex runs in that directory. `--since SHA` narrows the review to the commits after one an earlier round reviewed, over the files the PR changes. Each round's last message is `round-<n>.md`, by default under the git directory, which all worktrees share. A round past `--max-rounds` (default 3) is refused before codex starts, and a review that hung is not a round. `scripts/keep_awake.py` runs a command under `caffeinate -i` on macOS, and as is elsewhere, and returns its exit code. Self-tests CR1-CR15 (a fake codex binary, a fake session directory and a toy git repository; no codex, no network) and KA1-KA3 were written first and failed before the change. The tool's two write sites are listed in `scripts/write_audit.py` with their reason.

## Status

- **S2-DT:** `scripts/codex_review.py` and `scripts/keep_awake.py`. The PR review mode ran once against a real `codex exec` (the narrowed round 7 of #262: one round, exit 0, export removed).
