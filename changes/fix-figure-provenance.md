---
phase: "v2"
commit: "N4"
---

## Changelog

**Analysis figures record their sources relative to the run dir.** Harness figures (`toolkit/analysis/figures.py`) recorded their source artifacts as absolute paths. On a VM that meant `/tmp/...`, so once the outputs moved to the laptop, the analysis contract check emitted a HIGH `analysis_contract_figure` finding for every figure source ("changed after the figure was drawn"), although nothing had changed. The figure sidecar now records `png_path` and every source relative to the run dir (`paths_relative_to: run_dir`). `toolkit/analysis/contract.py` resolves each source against the run dir it is checking, and fails closed on a source outside the run dir (absolute, or a relative path that escapes it, even with a matching hash), on a sidecar whose sources are not a mapping, and on an absolute path that no longer exists; a legacy absolute path inside the run dir still checks. `scripts/figure_validator.py check_staleness` resolves such sidecars against the run dir the PNG sits in, so `derived_rebuild` does not see moved harness figures as stale. The self-tests were written first and failed before the fix: K14a-h (`contract.py`), R12b (`run.py`: a real harness run whose run dir is renamed checks clean) and T7 (`figure_validator.py`). `toolkit/engines/contract.py` is unchanged.
