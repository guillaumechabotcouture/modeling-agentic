---
phase: "v2 simplification"
commit: "S1"
---

## Changelog

**Golden records of decision tests, and a retro that re-runs a collected run's analysis on its stored rows with zero engine jobs (simplification pass S1).** The new module `toolkit/analysis/dt_golden.py` digests each decision-test record in three parts. `decision` holds what the test decided, without `type` or `perturb`. `detail` holds what was tested. `words` holds the sentences. Numbers are rounded to 6 significant digits. A field the module does not name counts as decision, so it is pinned. `toolkit/analysis/run.py`'s self-test captures every record `decision_tests.run_one` returns. It compares them with `toolkit/analysis/dt_golden.json`, keyed by self-test function, test id and call order (DG-R1), and `MA_DT_GOLDEN_UPDATE=1` rewrites that file. `dt_golden.py retro <run_dir> --work <dir> (--write F | --expect F)` copies a run, re-runs its analysis on the current code and digests the result. During the re-run, `compute.map_fn` and the engine's run methods raise `EngineJobRefused`, so a run whose stored rows do not cover the analysis is refused, never computed. Expected digests of real runs stay outside the repo. Tests: DG1–DG8 and RT1, a retro of the toy emulator run (0 engine jobs, stored digests reproduced, run dir untouched). No analysis behaviour changes.
