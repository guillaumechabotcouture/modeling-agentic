---
phase: "Harness baseline hb1"
commit: "fix 11 (L)"
---

## Changelog

**One quantile estimator for both uncertainty files (hb1 triage fix 11, cause L).** The v2 analysis wrote `analysis/uncertainty.yaml` with numpy's interpolated median and percentiles (`toolkit/analysis/core.q`). It wrote `uncertainty_report.yaml` from the same draws with order statistics (`scripts/propagate_uncertainty.py summarise_outputs`: the upper middle value, and floor-indexed bounds). The two medians therefore differed for an even number of draws; in measles R2 the fix task worked around this by changing the draw count. `summarise_outputs` now takes an optional `summary(values, level)` estimator. The v2 analysis passes `core.q`, so both files give the same mean, median and interval. Without the argument (`propagate()`, `scripts/ask.py`), the order statistics are unchanged. `core.q` now takes the mean as an exactly rounded sum (`math.fsum`), so cancelling draws keep their mean. Near the largest float it falls back to an interpolation that does not overflow; on ordinary draws its median and bounds are numpy's, as before. A mean in `uncertainty.yaml` can therefore change in its last digits. The plans and the engine job hashes do not change. Self-tests: UQ-L2 (propagate_uncertainty), UQE1-3 (uncertainty), R5L (run, on an even number of draws).
