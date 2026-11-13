---
phase: "F6"
commit: "benchmark eligibility r5 α"
---

## Changelog

**Benchmark eligibility follow-up (#215 narrow review, two P2s).** The contract's check of engine departures now
scopes them by the engine the run actually used (the analysis manifest's engine identity; before the analysis, the
engine the spec or engine_choice.json resolves to), never by the coverage file's own name (every identity source is read: the manifest, the hashed engine artifact's envelope and card, and the engine the spec resolves to; when they disagree the run gets the finding), and only then requires
that engine's coverage file: a run of an engine without one is not blocked by departures written for another, and
renaming a coverage file cannot take a run's departures out of scope. An interaction departure's rescaled spec
component must deliver exactly the benchmark's rescaled engine component, so a bundle with another intervention
cannot carry it. Self-tests G30e0-G30f (agents/analysis_backend.py), K-DEP1-K-DEP2 (toolkit/analysis/contract.py) and two PC5 rows (toolkit/spec/preconditions.py).
