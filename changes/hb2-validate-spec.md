---
phase: "v2"
commit: "hb2-validate-spec"
---

## Changelog

**The spec draft checks its own draft during its try (validate_spec).** Baseline hb1, ranked fix 10: the draft task had no way to learn whether its draft passed before the try ended, so each problem cost a whole SPEC try (`MAX_SPEC_TRIES` is small). Now `h_SPEC` hands the task its own pre-sign check chain (`Machine._spec_checks`, the checks the machine runs when the try ends) for the length of the try (`SdkTasks.set_spec_check`, withdrawn in a `finally`), and `agents/v2_sdk_tasks.py` offers it to the draft_spec session as an in-process MCP tool, `validate_spec` (`validate_spec_tool`, server `harness`; no shell). It takes no arguments and returns every problem, or that none was found. A check that raises is an error result, never a pass. After `MAX_VALIDATE_CALLS` = 12 calls in one try the tool says so, and checks run one at a time. Only the spec draft holds the tool: it is allowed by the PreToolUse hook for that session alone, and the crash read-back probe has no MCP servers. A draft holding the tool has a turn cap of `SPEC_MAX_TURNS_VALIDATING` = 80 (60 without it; a caller's explicit cap still holds). The draft_spec prompt tells the drafter to call it after writing and after each fix. Self-tests VS1-VS6 (all failed before the change).
