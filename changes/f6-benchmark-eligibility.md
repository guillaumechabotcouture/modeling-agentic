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
benchmark or component, reason, decision test) makes the engine eligible only once the
owner has signed the spec (the pre-sign check counts it as proposed, including for a spec that names its engine);
it is validated by the schema, shown on the sign-off sheet, recorded in `engine_choice.json` and declared in
`results.md`. Both benchmark roles gate: a held-out benchmark stands as validation (never a fitting target) and a
fit target out of band means the fit failed; each failing benchmark's role is recorded in `why_not`,
`benchmarks_not_in_band` and the departure. Owner decisions 2026-11-13 and review round 1: a cost-per-outcome benchmark gates only when the spec charges the component through the engine cost output it prices (otherwise it is reported); a used component no benchmark covers blocks unless a departure names a decision test on its effect; a departure counts only with the owner's signature (a lead cannot sign a spec that needs one), and only when its decision test varies the related effect; the check also runs when the spec is signed and when the analysis validates it; coverage must be for the engine assessed and match its pack and record; duplicate record ids are refused; interactions apply only when a package can hold every component; the configuration assessed is the one the run uses. Self-tests G1-G18 (analysis_backend), BC1-BC9 (`toolkit/engines/benchmark_coverage.py`).
