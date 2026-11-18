---
phase: "v2 simplification"
commit: "S1b"
---

## Changelog

**A retro that re-runs a collected run's analysis on its stored rows with zero engine jobs (simplification pass S1b, stacked after #262).** `toolkit/analysis/dt_golden.py retro <run_dir> --work <dir> (--write F | --expect F)` works in five steps:
1. It copies the run into an empty `--work`. The work dir must sit outside the run dir, checked by real path and by file identity.
2. It removes the copy's decision-test outputs, and refuses if any of them sit under a link.
3. It re-runs the analysis on the current code with the run's own number of draws. Meanwhile `compute.map_fn` and the engine's evaluating methods (`run`, `run_batch`, `run_series`, `initial_state`, `build_sim`) raise `EngineJobRefused`, under a process lock.
4. It digests the result.
5. It compares that with the run's stored results on every part the stored records hold.

`--write` refuses a re-run that differs from the stored results unless `--accept-stored-diff` is given. Outputs are never written inside the run dir or through a link, symbolic or hard. Expected digests of real runs stay outside the repo. Tests: DG7–DG10, DG12 and the DG16 retro cases, plus RT1 (`run.py`), a retro of the toy emulator run with 0 engine jobs, the stored digests reproduced and the run dir untouched.
