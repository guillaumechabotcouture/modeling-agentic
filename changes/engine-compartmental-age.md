---
phase: "v2 D"
commit: "ζ"
---

## Changelog

**Compartmental engine v0.2: age-structured transmission for vaccine-allocation and catch-up-campaign questions.**

Engine (`toolkit/engines/compartmental/`, our own Starsim module). Every addition defaults to v0.1 behaviour:
- `time_unit: year` (decimal-year horizons; turns on the figure hooks);
- `final_size` (to_extinction outcomes are the epidemic's final size);
- `per_group_parameters` (`s0:<group>` and `ratio:<o>:<group>` as calibrated parameters);
- `coverage_scale` (a per-unit latent multiplier on a reported coverage series);
- an SEIR generation interval, discretised with the triangular kernel (growth within 1% of `R0 = (1 + rL)(1 + rD)`);
- components acting on a `day` during the epidemic;
- `schedule()` and `run_series()` (figures-hooks/1);
- `batch_equals_run`.

Validation:
- Pack AS1–AS6, 10 new benchmarks: a 5-group SEIR with pre-existing immunity against the reference ODE,
  age-targeted dose blocks against the multigroup threshold, a component timed after extinction, birth cohorts
  against the rank-one final size at R0 12 and 18, and hooks consistency.
- 62 of 62 assessable benchmarks are in band (re-run locally). FS7 and RN5 stay not_assessable.
- The coverage pack is carried by a 27-case byte-identity proof against ce79ccf and re-stamped, not recomputed.
- Self-tests A1–A40. A39: final-size runs stream on the generation grid instead of keeping a per-step history.
  A40: finished Starsim sims are emptied, because Starsim 3.3 keeps dropped Sim objects alive.

`literature.yaml` holds sourced pathogen priors, all unverified.

Core changes (they need acceptance on two diseases):
- `toolkit/analysis/observed_targets.py`: posterior calibration to observations of group states (immunity,
  susceptibility), attack rates and outcome counts through declared observation sources. Source bias parameters
  are sampled as nuisance rows.
- The direct model batches engines that declare `batch_equals_run` and keeps per-group outcomes.
- `options.packages.within_envelope`.
- `deliverable.by_group`.
- The compartmental engine is registered with the configuration its signed spec gives.
- Claim ids are sanitised for tests named after parameters.
- A floor on the cost-per-outcome figure's height.
- Observation targets carry their frame per row.
- The decision-spec skill gains the vocabulary (no thresholds).
- `toolkit/analysis/contract.py`: a spec that names its engine ignores the harness's `engine_choice.json`, so
  the gate no longer flags that file as stale for it (K12b). Flu run 8 and measles runs 6–7 carried a spurious
  HIGH `analysis_contract_stale` at every gate.
