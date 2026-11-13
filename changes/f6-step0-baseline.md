---
phase: "F6"
commit: "step 0"
---

## Changelog

**Step 0: the decision by arithmetic before any simulation (package-rate engine, `mode: trial_priors`).** Owner decision 2026-11-12: runs build up, and step 0's plan is the reference every later step explains its differences from. `toolkit/engines/empirical/engine.py` gains a mode in which nothing is calibrated: per outcome, rate = external baseline (a bound unit attribute `baseline_rate`, each outcome's source declared) x M(package) / M(baseline coverage), so a reduction is never applied twice to a baseline that already includes it (`baseline.measured_under`: `coverage_attribute` or `no_components`). Each component's effect per outcome is a trial prior (`effects.<c>.<o>: {rr, range, dist, measure, source}`, parameter `lrr:<c>:<o>`, kind fixed, drawn by the uncertainty step on the log scale), optionally relative to another component (`relative_to`). Components combine by a declared rule (`multiplicative` with per-pair interaction terms `lint:<a>+<b>:<o>`, or `strongest`), shown on the card and in the engine version; further outcomes (DALYs) are declared weighted sums (`compose`, `w:<t>:<o>`). A value without a source or range, a component without an effect on an outcome, an undeclared rule, and a unit without its baseline are refused. `toolkit/spec/priors.py` `load_card()` reads an engine's `priors_doc()` when it gives one (the signed `engine.config`), else `priors.yaml` as before. The calibrated mode is unchanged. No change to `toolkit/analysis/`, `toolkit/engines/contract.py` or `toolkit/spec/signoff.py`.
