---
phase: "v2"
commit: "collect import"
---

## Changelog

**`run_cluster.py collect` imports results_store from its own repo.** `collect` (and `checkpoint`, `checkpoint-now`, `relaunch`, `cost`) imported `results_store` after putting `<cfg.tree>/scripts` on `sys.path`, so whenever the config's launch tree was absent on the collecting machine it failed with ModuleNotFoundError unless `PYTHONPATH=scripts` was set. One helper, `_results_store()`, now imports it from the `scripts/` directory beside `cloud/`, as the module already imports `eval_paths` and `pipeline_id`. Self-tests CI1-CI3 run `collect` in a fresh interpreter, with a working directory outside the repo and a config whose tree does not exist, with no `PYTHONPATH` and with one that names another checkout's `scripts/` first (Codex round 1: the repo's directory goes first on every call); each checks the module's file is this repo's.
