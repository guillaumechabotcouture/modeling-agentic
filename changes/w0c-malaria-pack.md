---
phase: "v2 Wave 0"
commit: "W0-C (ii)"
status_group: "In review (v2 autonomy, Wave 0)"
---

## Changelog

**The malaria pack declares its effect fit in data, each training target with its trial's estimand.** New `toolkit/engines/malaria/benchmarks/effect_fit.yaml` (`effect_fit/v1`) states what `net_effect_fit.py` held as module constants for F6 option A, refit b and the 20 Nov verification round. That covers 10 training targets (B9c, B9d-B9f, B9g, B10, B11, B12, B12b, B12m), each with contrast, outcome, scale, window, population, measurement and, where the engine's observable differs from the trial's, a mapping. It also covers B9 and B9b as monitors, seven parameters with boxes and sources (the `pbo_adv_decay` ceiling included), the confounded pair, three design waves, the history-matching cutoff and the 14 Nov acceptance rule. `benchmark_coverage.yaml` gains `effect_parameters` for B9c, B10, B11, B12, B12b and B12m. The core's new `coverage_problems` requires them for every training target and refuses a fitted parameter that no target names. The core gains `runner_evaluator`, the `benchmark.Runner` adapter: it recalibrates each point at its full fitted values, reads each calibrated setting's control arm, refuses a runner whose seeds are not the pack's, and reads unrounded per-seed values (`Runner.full_precision`, a new opt-in `per_seed_full`; no other output changes) (EF_RUNNER). `net_effect_fit.py` stays in place as the record of F6's runs, with a pointer to the core. Its new self-test NE_DECL checks that the declaration states its constants and that the core's seed interval and verdict agree with `ACCEPTANCE_RULE` on 300 cases. The core's EF_PACKS checks every pack's declaration against its pack and coverage. Open: the replay proof from the stored engine cache (EF_PACK_REPLAY) needs that cache, which is held in cloud storage. The 20 Nov round used a local leave-one-out margin and the core uses a global one, which the replay will settle. Also open for the owner: `must_meet` is empty, so a target no candidate can meet is declared and tested under the conflict rule rather than failing the whole sample as `finalize` did; listing the ten targets restores the 14 Nov all-targets rule. The adapter reads unrounded per-seed values where the old runner rows were rounded to 4 decimals.

## Status

- **W0-C (ii):** malaria pack's effect-fit declaration with estimand fields, coverage effect parameters, NE_DECL; replay proof open; in review.
