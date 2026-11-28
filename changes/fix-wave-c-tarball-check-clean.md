---
phase: "Simplification Wave C"
commit: "restore check_clean parameter in cloud/coiled_ops.py tarball_from_tree"
---

## Changelog

**Hotfix `CO.tarball_from_tree` `check_clean` parameter:** Restore keyword-only `check_clean: bool = True` in `cloud/coiled_ops.py:tarball_from_tree` (wrapping `git status --porcelain` so `cloud/run_cluster.py:code_tarball` can pass `check_clean=False`), and add unmocked git-repo tests in `cloud/coiled_ops.py` and `tests/test_run_cluster.py`.
