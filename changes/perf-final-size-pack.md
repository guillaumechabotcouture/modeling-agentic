---
phase: "F6"
commit: "FSD γ"
---

## Changelog

**Analytic pack cases for the direct final-size solver.** `toolkit/engines/compartmental/benchmarks/pack.yaml` gains three solver-enabled configurations (`fs_direct`, `multi3_direct`, `multi5_direct`: final_size, no forcing, deterministic importation) and fourteen benchmarks, DS1a-DS8: the Lambert W closed form at R0 1.5, 2 and 18 (abs 1e-9), the seed-size bound at R0 0.5 and the seeded closed form at R0 1.01, the rank-one relation (rel 1e-8), the reference ODE on the identity, rank-one and asymmetric 5 x 5 matrices (abs 1e-6), `oracle_final_size_multigroup` on the 5 x 5 case (abs 1e-10), solver against the stepped sim on single-group, rank-one and 5 x 5 settings (rel 1e-9), and every DS arm converged. `benchmark_coverage.yaml` lists them (no package component); `results.yaml` re-run (76 of 76 assessable in band) and the card regenerated. No engine code changes, so the engine identity and the coverage re-stamp are unchanged.
