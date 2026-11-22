---
phase: "v2"
commit: "bundle data set"
---

## Changelog

**`make_bundle` ships the declared data set to dask workers.** The reuse key's `data_set` part hashes the files `FP_DATA_SET` names (repo `data/`, `.claude/orchestration/`, and the non-Python files of `scripts/` and `toolkit/`, #314), but the bundle shipped only the run's `models/` and `data/` and the repo's `scripts/` and `toolkit/`, so a worker that read repo `data/` or `.claude/orchestration/` computed without them while the key named the launcher's copies; a data-set file over the bundle's size cap was also left out. `_data_set_files` now lists the data set once: `_files_digest` hashes that list and `make_bundle` ships exactly it, whatever a file's size, with `_truth` and the launch stamp left out as before. The bundle record reports `data_set_files`. Self-tests BD1-BD4.
