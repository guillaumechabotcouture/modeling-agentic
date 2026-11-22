# Direct final-size solver for the compartmental engine (plan, not built)

Status: plan, 2026-11-22. Context: the hb1 eval run flu_vaccine R2 (direct evaluation, `final_size: true`).

## Why

The flu R2 analysis evaluated 3,374 dose-block packages per uncertainty draw: one unit, every package the
11-block envelope admits (`options.packages.generate: true, within_envelope: true`). The run's records give
264 s per draw. 200 draws took 14.6 h, about 78 ms per engine evaluation, run in `run_batch` calls of 1,024
members. Local timing on the same spec: one `run()` takes 1.1–1.7 s (sim set-up dominates), and a 16-member
`run_batch` takes 0.16 s. With `final_size: true`, no forcing and no component acting during the epidemic
(`final_generations`), the engine already steps one generation per Starsim step. It still steps until the slowest
member of the batch converges to `tol` (1e-10 here, with a 1e-9 seed). Heavily vaccinated arms near the
threshold run for thousands of generations, and the whole 1,024-member batch waits for them.

Under those same conditions the outcome is the multigroup final-size relation, which the benchmark pack
already uses as its reference (`oracle_final_size_multigroup` in
`toolkit/engines/compartmental/benchmarks/oracles.py`):

    z_i = s_i - (s_i - e_i) exp(-q sum_j C_ij z_j)

with z, s and e as fractions of N_i, s the susceptible share after the interventions, and e the seed.

## What changes

- `CompartmentalEngine.run_batch`: when `final_generations` holds and `importation: deterministic`, solve the
  relation for the whole batch at once (arrays B x G). Start the fixed-point iteration at z = e, which gives the
  minimal root (only what the seeds reach), and stop it at the same `tol` the stop rule uses. Then polish with a
  vectorised Newton step on the members whose iteration converged slowly (near-critical members). A member that
  converges to neither fails closed, as the step cap does now.
- Unchanged: the interventions (`apply_component`, doses and wasted doses: `cost_drivers`), `s_first`, p_major,
  the per-group outcome ratios and `Outcomes` itself. `meta.converged` stays. `meta.steps` becomes the iteration
  count, and the card says so.
- `run_series()` (figure timing) and every other mode keep the Starsim simulation. The sim also stays as the
  benchmark path: a new pack case asserts that solver and sim agree within `tol` on every final-size configuration.
- Card mechanism line: "final_size: per-capita outcomes from the multigroup final-size relation, solved directly;
  the simulation gives the timing".

## Version and identity

The solver's arithmetic differs from the step-by-step sum in the last bits, so the outputs are no longer byte
identical: **engine 0.2 → 0.3**, with a new code sha256 in `engine_identity`. Runs on 0.2 keep their rows. A
0.3 analysis does not reuse 0.2 rows: row reuse keys on the engine identity (W0-A).

## What re-runs

- **Analytic pack** (`benchmarks/results.yaml`): re-run in full on 0.3 (about two minutes locally). FS1–FS4, AS1
  and AS7 compare against the oracles at `abs 1e-6` or tighter and should land closer to them.
- **Coverage pack** (`coverage_results.yaml`, 200 replicates per case on a VM): `compat_proof.py prove` shows byte
  identity for every configuration that does not take the new path. Those rows are re-stamped (`restamp`); the
  final-size configurations are re-run on the VM. `prove` needs one addition: it must classify cases by path and
  restamp only the identical ones. Today it requires every case to be identical.
- **CP8** binds the live calibrator files (`CALIBRATOR_FILES`: `toolkit/analysis/calibration.py`,
  `observation.py`, `oracles.py`) to the committed proof. The solver lives in `engine.py` and copies nothing from
  `oracles.py` (it may import it read-only, or reimplement it). None of those files changes, so CP8 holds without
  a new calibrator-code proof. `compat_proof.yaml` is regenerated against the 0.3 identity.
- **Eligibility**: `benchmark_coverage.yaml` and the engine-eligibility check read the re-stamped results. They
  need no other change.
- **Runs**: any eval or F-run spec on `compartmental` with `final_size: true` relaunches on 0.3 (flu_vaccine).
  Other configurations are byte-identical and need nothing.

## Expected speedup

Fixed-point iteration on a 5-group system costs O(G^2) per member per iteration, and a few hundred iterations are
needed even near the threshold (Newton polish). Batched in numpy, this is microseconds to tens of microseconds per
member, against about 78 ms measured. That is roughly 10^3–10^4 times faster per evaluation. One flu draw
(3,374 members) would take well under a second instead of 264 s. Calibration (about 50 min in R2) also calls the
engine and would shrink by the same factor wherever it evaluates final-size arms. Python overhead per draw
(cost tables, the ILP over 3,374 options, the guards) then dominates. We have not measured it in isolation; the
point step's 85 s included its own engine evaluation.
