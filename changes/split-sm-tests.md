---
phase: "architecture wave 1"
commit: "1 α–γ"
---

## Changelog

**The stage machine's inline self-tests move to `tests/stage_machine/`, by a script that can be re-run (owner yes 2026-11-22).** `scripts/refactor/split_stage_machine.py --stage tests` (α) moves `_self_test` and each `_self_test_<x>` of `agents/stage_machine.py` into one file each under `tests/stage_machine/` (`sm_selftest_core.py`, `sm_selftest_<x>.py`), with the helpers only they use (`_t_*`, `_TOY_*`) in `sm_selftest_common.py`; β is its output, unedited. The module keeps a `_self_test` stub that runs the moved cases against the module itself, so `python3 agents/stage_machine.py --self-test` prints the same cases and `scripts/self_tests.py` still finds it. The moved code is unchanged except where a test rebinds the module's own globals: `global REPO`, `globals()["open_findings"]` and `sys.modules[Machine.__module__].v2_gate` become assignments to the module (`_SM.REPO`, `SM_GLOBALS[...]`, `_SM`), so they still reach the code under test. The script stops, naming the code, on anything it cannot classify (a statement among the self-tests named like neither a test nor a test helper, `exec`/`eval`, `__file__`, other `sys.modules` uses), and does nothing on a module it has already split. Check: the ordered (status, id) list of the self-test's cases is identical before and after (`--compare-cases`). The script's own self-test (S1–S20) runs both stages on a toy module. γ: `scripts/write_audit.py` reviews the script's one write (its output files, in the checkout it is pointed at).
