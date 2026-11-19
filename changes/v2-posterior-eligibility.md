---
phase: "F6"
commit: "posterior eligibility α-β"
---

## Changelog

**Engine eligibility for a declared posterior's training benchmarks takes its status from the fit's verified chosen point.** Owner decision, 19 Nov. With `uncertainty.net_effect_posterior` declared, the run evaluates every posterior column at the fit's chosen point (v2-posterior-point). The benchmark precondition still read each benchmark's status from the engine's `results.yaml`, which is scored at card defaults, so a training target could block an engine at a point the run never uses. Now `benchmark_coverage.posterior_trained` loads the posterior through the run's own path (`uncertainty.net_effect_sample`). `load_posterior` returns `trained`: the file's `plan.targets_train` that its `finalize()` replay checked the chosen point in band on. `gate` treats those benchmarks as in band, with `status_source: posterior_chosen_point`. Every other benchmark, held out of the fit, still takes its status from the record, still blocks when it is not `in_band`, and still accepts an owner-signed `engine_departures` entry. The eligibility output (`gate`, `assess` and the engine-choice rows' `benchmark_status_source`) records the status source per benchmark. A declared posterior that does not load makes the engine not eligible (fail closed). Specs without the field are unchanged. These tests were written first and failed before the change: BC11 to BC15 in `benchmark_coverage.py` (α-β).
