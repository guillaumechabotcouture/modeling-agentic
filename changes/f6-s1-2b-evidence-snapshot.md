---
phase: "F6"
commit: "S1-2b α–δ"
status_group: "F6 Stage 1"
---

## Changelog

**One validated snapshot of benchmark evidence (F6 S1-2b, Codex round 4 on #154).** `load_evidence(engine, pack, rows)` in `toolkit/engines/benchmark_sources.py` is the one loader of benchmark evidence. It returns an immutable `Evidence` snapshot of the validated pack, the fitted values the engine actually uses (cached or defaults) with the digest of the file that backs them, and the stored rows. Its `problems` are the source check; "rerun required" for rows scored against another declaration; and "refit required" for fitted values without current provenance: a missing, dangling or unreadable file, other declarations, or values in use that are not the file's. Runner, `verified_benchmarks`, merge, rescore, the `run` CLI, `fitted_ranges` (core, batch design, joint calibration, the emulator's design box), the analysis entry, `spec_benchmarks`, `verified_spec_benchmarks`, the `engine_parameters benchmarks` CLI, estimates acceptance, `render_notes` and `b15_mechanisms` read only from it, and the per-entry-point checks are removed. `Runner` keeps its own copy of the validated pack and `evaluate()` resolves each benchmark by id, refusing a declaration passed in directly. Every estimand type now has a declaration digest: coverage rows keep their own `declaration_sha256`, and oracle rows stored before digests are accepted only with an oracle row's shape, so a change of type invalidates stored evidence. Self-tests BS54–BS62 start with Codex's four reproductions: a direct `evaluate`, ratio to value, the `run` CLI on a copied pack with no adjacent fit, and a fitted file replaced after the engine cached it. The compartmental and renewal stored results still verify. The malaria artifacts still report refit and rerun required.

## Status

- **S1-2b:** benchmark evidence read through one validated snapshot; must merge before any F6 refit or pack run.
