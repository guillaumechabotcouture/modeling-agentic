---
phase: "F6"
commit: "benchmark eligibility α-γ"
---

## Changelog

**Engine eligibility requires the engine's trial benchmarks for the components the spec uses to be in band (owner
decision 2026-11-12).** Each engine with benchmarks keeps `benchmark_coverage.yaml` beside its module (malaria,
compartmental, renewal), saying which component values and which estimand (effect against none, against another
component, durability, interaction, cost per outcome) each benchmark covers, or why it covers none, and which
components no benchmark covers. `agents/analysis_backend.py` `eligible_engines` refuses an engine when a covering
benchmark is `out_of_band`, `not_assessable` or `band_mis_based` (each named in `why_not`) in the verified benchmark
record of the engine version in use (`toolkit/engines/benchmark.py` `verified_results`, its version equal to the
card's); a record from another engine identity counts for nothing. A spec-level `engine_departures` entry (engine,
benchmark, reason, and a decision test or the reason none is possible) makes the engine eligible only once the
owner has signed the spec (the pre-sign check counts it as proposed, including for a spec that names its engine);
it is validated by the schema, shown on the sign-off sheet, recorded in `engine_choice.json` and declared in
`results.md`. Self-tests G1-G12 (analysis_backend), BC1-BC8 (`toolkit/engines/benchmark_coverage.py`).
