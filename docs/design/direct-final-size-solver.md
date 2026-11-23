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
  relation for the whole batch at once (arrays B x G), on the groups the seeds reach. Reach is computed per member,
  after the interventions, following the direction of infection: from source i, destination j is reached when
  C_ji > 0 (the force on j sums C_ji z_i), along a path that starts from a group with a surviving
  seed (e_i > 0) through groups with susceptibles left (s > e). A group behind a fully immune one is not reached,
  even though the contact mask connects it (Codex review: a chain A–B–C with B immune). Unreached groups get
  z = e. On the reached groups the relation has a single root in [e, s]. The map T(z) = s − (s − e) exp(−q C z) is
  monotone, so iterating from the lower bound z = e and from the upper bound z = s brackets the root from both
  sides. Stop when the bracket is narrower than `tol` in every group: that is a bound on the error. A step-size test
  is not enough. With a 1e-12 seed in a growing epidemic, the first step from e changes z by about 1e-12 while the
  root is about 0.8 (Codex review). Speed up the bracket with a vectorised Newton step on the upper iterate, kept
  only while it stays inside the bracket. A member whose bracket does not close within the iteration cap fails
  closed, as the step cap does now.
- Unchanged: the interventions (`apply_component`, doses and wasted doses: `cost_drivers`), `s_first`, p_major,
  the per-group outcome ratios and `Outcomes` itself. `meta.converged` stays. `meta.steps` becomes the iteration
  count, and the card says so.
- `run_series()` (figure timing) and every other mode keep the Starsim simulation. The sim also stays as the
  benchmark path: a new pack case asserts that solver and sim agree within `tol` on every final-size configuration.
- The solver code lives in `engine.py`, which `engine_identity()` fingerprints. It does not import
  `benchmarks/oracles.py`: that directory is outside the engine fingerprint, so a change there would move results
  without moving the identity. It would also stop the oracle from being an independent check.
- Card mechanism line: "final_size: per-capita outcomes from the multigroup final-size relation, solved directly;
  the simulation gives the timing".

## Version and identity

The solver's arithmetic differs from the step-by-step sum in the last bits, so the outputs are no longer byte
identical: **engine 0.2 → 0.3**, with a new code sha256 in `engine_identity`. Runs on 0.2 keep their rows. A
0.3 analysis does not reuse 0.2 rows: row reuse keys on the engine identity (W0-A).

## What re-runs

- **Analytic pack** (`benchmarks/results.yaml`): re-run in full on 0.3 (about two minutes locally). It needs new
  cases first. FS1–FS4 run configurations without `final_size: true` (FS1a runs `r0_1p5` on `single_day`), and AS7
  compares two engine arms, so neither covers the solver against an independent oracle. Add solver-enabled
  configurations of the FS1 (Lambert), FS3 (rank-one) and multigroup ODE final-size cases, at `abs 1e-6` or tighter
  (the `ode_final_size` cases already use 5 groups).
- **Coverage pack** (`coverage_results.yaml`, 200 replicates per case on a VM): its rows were computed by 0.1 and
  re-stamped to 0.2 (`restamped.old_identity.version: 0.1`). `restamp` checks the proof's old identity against the
  identity that computed the rows (`compat_proof.py` near line 244), so a 0.2 → 0.3 proof alone is refused. There
  are two ways forward. One: `compat_proof` accepts a chain of committed proofs (0.1 → 0.2 → 0.3) and classifies
  cases by path, re-stamping only the byte-identical ones. Two: re-run the coverage pack on the VM. The second is
  simpler, and its cost should be measured before choosing. With option two, `compat_proof.py`'s self-test needs a
  path for freshly computed rows: CP5 requires an all-identical proof and CP6 requires `restamped.proof`, and
  neither holds for a fresh run. CP9 also calls `restamp()` on a fixture that needs `restamped.old_identity`, so
  CP9 and CP10 need a restampable fixture of their own, kept separate from the fresh rows. None of the three coverage configurations enables `final_size` today. Re-running
  them therefore tests nothing new; a final-size coverage configuration has to be added for the pack to cover the
  solver.
- **CP8** binds the live calibrator files (`CALIBRATOR_FILES`: `toolkit/analysis/calibration.py`,
  `observation.py`, `oracles.py`) to the committed proof. The solver lives in `engine.py` and neither copies nor imports
  `oracles.py`. None of those files changes, so CP8 holds without
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
