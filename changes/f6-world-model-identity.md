---
phase: "F6"
commit: "WM-I"
status_group: "F6 progressive steps"
---

## Changelog

**The world model's track record counts only confrontations it can verify (F6 WM-I, follow-up to F6 WM-L).** In `toolkit/analysis/world_model.py`, a conditional claim (the default) is judged only on a design that samples its feature independently of the other inputs by construction (the design's `independent_features`, recorded as a design property), or by an estimate adjusted for declared covariates (`adjust_for`: a partial rank correlation). Otherwise it is inconclusive, at any correlation, and a `scope: marginal` confirmation never raises a belief's confidence. Every attempted direction and flip enters the Benjamini-Hochberg family, a test that could not be fitted at p = 1. A confrontation's evidence identity is the sha256 of exactly the slice it read: its points, region features, package outcomes and comparator, and, for a paired test, incremental costs, shadow price and discount; for a plan test, the matching units' plan and filter values. Its evidence time is the earliest first-seen record of the results' sha256 (`analysis/batch/first_seen.jsonl`, appended by `record_first_seen()` when a batch first appears and never refreshed when a rerun reuses cached results), or `point.yaml`'s `written_at` for a plan test. Without a record the test is post hoc; `batch.yaml`'s `written_at` is not used, since every analysis rewrites it. Adjusted estimates are inconclusive when the covariates are rank-deficient, the feature or response is constant, or the residual variance falls below 1e-10 of the original, and a paired test's evidence identity includes its tie tolerance. Only confrontations re-derived from the run's artifacts are scored; an imported history entry not re-derived with the same identity is listed as unverified. Tests and state are saved at full precision, with only display fields rounded, and a save then reload replays as repeats. With this, the track record is no longer marked provisional. Self-test WM59-66 and WM88-90, written first.

## Status

- **WM-I:** world model track record verified from the run's artifacts (conditional verdicts on independent designs or adjusted estimates, full FDR family, slice evidence identity, first-availability time, verified history, full-precision persistence).
