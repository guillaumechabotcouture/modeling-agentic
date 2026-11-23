---
phase: "v2"
commit: "fix/spec-checks-p1s"
---

## Changelog

**SPEC checks: four Codex round-4 findings on code merged through #342.** (1) A first capped SPEC try with no draft on disk no longer stops later capped tries' drafts from being checked: when the incoming feedback was the checks' own, the carried feedback stays re-checkable (`spec_feedback_checked`). (2) A cited input that exists but is neither a regular file nor a directory (a FIFO, a device) is refused in `_cites_outside`, and no later check opens it; before, the geometry check could block on a FIFO with no writer. (3) `decision_spec.yaml` and `analysis_settings.yaml` are refused as a symbolic link, a hard link or a non-regular file before any check reads them (`_draft_file_problems`). (4) `SdkTasks(max_turns=0)` (or any non-positive or non-integer cap) raises `ValueError`: the SDK passes `--max-turns` only for a truthy value, so 0 meant no turn limit. After the PR's Codex round: every entry in the run directory that is not a regular file, a directory or a link to one is refused before derivations or any check opens a file (`_special_files`), whatever path names it; the conflicts default is applied inside `_spec_checks`, after the link checks (it replaces the file, which hid a hard link); and feedback made only of "draft_spec failed:" notes (a run saved before this fix) stays re-checkable. Self-tests SP1-SP7 (all failed before the change).
