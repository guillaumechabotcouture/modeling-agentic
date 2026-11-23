# Compartmental engine v0.3: notes

Engine `compartmental-starsim` v0.3. Design note: `docs/design/v2d-compartmental-engines.md` (§1.1, §5).
v0.2 adds what age-structured allocation questions need. Each addition acts only when a configuration or a
component asks for it, so every v0.1 configuration gives byte-identical outputs.

v0.3 adds the direct final-size solver (plan: `docs/design/direct-final-size-solver.md`, PR #346). A `final_size: true`
run with `forcing: none`, no component acting during the epidemic (`day` 0) and `importation: deterministic` no
longer steps the sim. It solves the multigroup final-size relation z_i = s_i - (s_i - e_i) exp(-q sum_j C_ij z_j)
on the groups the seeds reach after the interventions (`solve_final_size`, `infection_reach` in `engine.py`):
- Reach follows the direction of infection from the groups with a seed left, through groups with susceptibles
  left, so a group behind a fully immune one keeps z = e.
- Lower and upper iterates start at e and s and stay sub- and super-solutions. Plain steps move by T less or
  plus a bound on its rounding error; Newton candidates (and the Newton point offset either way) are kept group by
  group only where the check holds with that bound against them, so rounding never certifies a bound. The map is evaluated in the
  expm1 form: s - a exp(-x) loses about 2e-12 to cancellation at R = 1.
- A member stops when its bracket is at most `tol` wide in every group and returns the lower bound (within tol
  of the root, never above it). A bracket still open at `step_cap` iterations, or one whose bounds have not
  moved at all for 50 iterations, fails closed (`converged: false`). That happens when the arithmetic cannot
  certify `tol`: in a chain of exactly critical groups (FD14) the limit is about 1e-9, and such a member crawls
  on plain steps to `step_cap` (10^6 for final_size, roughly a minute) before failing. The rounding bound includes
  an absolute, unscaled allowance for underflow, so a force rounded to zero is never certified as zero (FD18,
  FD23); inputs are promoted to binary64; a group no reached group acts on is exact (z = e); an overflowing force
  saturates the map; overflow and underflow are handled inside, whatever the caller's numpy error settings.
- `meta.steps` and `windows.*.steps` count the solver's iterations, per member, and `meta.time_axis` is
  `solver_iterations`. `meta.final_size_path` is
  `direct_solver` or `simulation` on every final_size run.
- Interventions, doses, `s_first`, p_major and the outcome ratios are unchanged. `run_series()` and every other
  configuration keep the sim. Their per-capita outcomes, windows and cost drivers are byte-identical to v0.2;
  a final_size run on the simulation path (forcing, a component during the epidemic, or branching importation)
  also gains `meta.final_size_path: simulation`, and every other configuration's whole output is unchanged.
- The engine does not import `benchmarks/oracles.py`: that file is outside the engine fingerprint and is the
  independent check (`selftest_final_size.py`, FD1–FD25).

## Model and framework

The engine is one Starsim `ss.Module` (`GroupEpidemic`) holding group-level state as numpy arrays with a batch
axis, inside an `ss.Sim` with no network and no demographics (the `starsim-dev-nonstandard` compartmental
pattern, decided 2026-11-01). v0.2 keeps that pattern:
- it reuses the module that is already benchmarked (51 analytic checks and the coverage pack);
- `run_batch()` evaluates thousands of dose vectors in one sim, which the joint dose-block allocation needs.

A plain scipy ODE was not used: it would be a second engine with its own validation, and its final size would
match the one the renewal step already reproduces exactly (FS6, AS1).

The age-structured model is the discrete-time renewal form of a multigroup SEIR:

    I_i,t = S_i,t-1 [1 - exp(-q f(t) sum_j C_ij sum_s w_s I_j,t-s / N_j)]

- Groups are age bands with a contact matrix `C` (mean daily contacts of a person in i with people in j), or
  birth cohorts with homogeneous mixing.
- `w` is the generation interval. `{seir: {latent_days, infectious_days}}` discretises the SEIR model's
  interval, `g(t) = (e^{-t/L} - e^{-t/D}) / (L - D)`, with the triangular kernel. This discretisation keeps its
  mean at `L + D` and its growth rate at `R0 = (1 + rL)(1 + rD)` within 1% (AS2). The interval form shifts the
  mean by half a step and biases R0 from growth by 3–13%.
- The final size satisfies the multigroup relation exactly for any `w` (FS6, AS1, AS5).
- Pre-existing immunity by age enters in one of two ways. With the `given` initialiser it is the initial
  susceptible share per group: a unit attribute `s0`, or (`per_group_parameters: [s0]`) calibrated parameters
  `s0:<group>`. With the `cohort_history` initialiser, susceptibility comes from dose coverage by birth year;
  `coverage_scale` is a per-unit latent multiplier on a reported coverage series. It is a simplification
  standing in for everything that separates reported from effective coverage, and it cannot represent a gap that
  differs between cohorts. Its sourced prior (uniform on [0.5, 1.1]) and its limitation text are in
  `attribute_binding.yaml`; the results list both when a run calibrates it. A per-cohort hierarchical
  latent-coverage model is the planned follow-up (F6).
- Vaccination by age group uses whole dose blocks per group (`kind: doses`, `block_size`, `max_blocks`) or
  campaigns (`kind: campaign`, targeted groups, reach). Protection is all-or-nothing with `efficacy:<component>`.
  Doses given to immune people are wasted unless `targets_susceptible` is set.
  - Timing: `day` (v0.2) is the number of days after `horizon.start` when the component acts. The default, 0,
    acts before the epidemic. A later day acts on the susceptibles left at that point, and a to_extinction run
    continues until then.
- Seasonality: cosine forcing `1 + a cos(2 pi (t - phase) / period)`.
- `time_unit: year` (v0.2) reads the horizon in decimal years, with day 0 at `horizon.start`. In to_extinction
  mode, infections after the decision window's end above the stop tolerance fail closed. They are not dropped.
- Outcomes are infections per group, and any outcome through an outcome-per-infection ratio per group. The ratio
  is a unit attribute `ratio:<o>`, or (`per_group_parameters: [ratio:<o>]`) calibrated parameters
  `ratio:<o>:<group>`.
- Figure hooks (`figures-hooks/1`, `time_unit: year`):
  - `schedule()`: one charged decision event per component a package sets.
  - `run_series()`: monthly incidence (a rate) and the susceptible share (a mean). It is consistent with the run
    to 1e-12 (AS6).

## Parameters

The engine's parameters are generic. Each has a value, a range (`lo`/`hi`), units and a source on the card:
- `r0` or `q`, and the forcing amplitude and phase: calibrated per run, or declared by the spec with a source.
- `efficacy:<component>` and `seroconversion:<dose>`: fixed, declared per question.
- `s0:<group>` and `ratio:<o>:<group>`: calibrated, range [0, 1].

Pathogen-specific values are priors with their spread across settings, never defaults. They are in
`literature.yaml` (R0, serial and incubation intervals, dose effectiveness, contact-matrix sources), each with its
citation, marked `unverified` until the evidence service checks it.

## Validation

- `benchmarks/results.yaml` (re-run on v0.3): 77 of 77 assessable benchmarks are in band. DS1–DS8 run the direct
  solver against the closed forms, the reference ODE, the pack's fixed-point oracle and the stepped sim; AS7 compares the
  direct solver with the stepped declared interval near criticality. The ten v0.2 benchmarks are AS1–AS6.
  - AS1: a 5-age-group SEIR with pre-existing immunity by age matches the reference ODE's final size per group.
  - AS1b: the per-group parameters give the attribute path's epidemic.
  - AS2: the SEIR growth relation.
  - AS3a–c: age-targeted dose blocks below and above the multigroup threshold, `rho(diag(s) K) < 1`.
  - AS4: a campaign after extinction changes nothing.
  - AS5a–b: birth cohorts with an always-immune cohort, against the rank-one final-size relation at R0 12 and 18.
  - AS6: hooks consistency.
- FS7 (a published age-specific final-size pattern) and RN5 remain `not_assessable`, as in v0.1. Their values
  have not been transcribed with a quoted span: the evidence service was unreachable on 2026-11-05. Every report
  carries the engine's validation label.
- `benchmarks/coverage_results.yaml` (SB1–SB5, 200 replicates each, v0.1 on two dev VMs) is carried by a
  byte-identity proof, `benchmarks/compat_proof.py`. Every pack setting of a v0.1 configuration, and one
  representative unit per coverage-pack configuration, give identical outputs under v0.1 (ce79ccf) and v0.2: 27
  of 27 cases. Its identity fields were re-stamped by that proof, recorded under `restamped:`, and were not
  recomputed. For v0.3 the proof was re-run against the v0.1 commit that computed the rows (b24d147), not against
  v0.2: the restamp checks the proof's old side against the code that computed the values. 27 of 27 cases are
  identical, since no coverage configuration sets `final_size`. The coverage pack therefore does not test the
  solver; the analytic pack and `selftest_final_size.py` do.
- Self-tests: `engine.py --self-test` (v0.1 contract, batching, stop rule, pack subset, stamps),
  `selftest_final_size.py --self-test` (v0.3, FD1–FD25) and `selftest_age.py --self-test` (v0.2, A1–A24), about 15 s and 25 s locally with two BLAS threads.
