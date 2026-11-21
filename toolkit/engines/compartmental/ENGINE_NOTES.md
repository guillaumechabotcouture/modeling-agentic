# Compartmental engine v0.3: notes

Engine `compartmental-starsim` v0.3. Design note: `docs/design/v2d-compartmental-engines.md` (§1.1, §5).

## v0.3: a declared Starsim v4 disease

v0.3 runs the same model as a declared Starsim v4 disease (plan: `docs/design/v2-ssv4.md`), where v0.2 was one Starsim 3 `ss.Module` holding numpy arrays (described below as it was).

- **States.** `S`, `I1..IL` and `R`: `I_l` holds the people infected `l` steps ago and leaves after exactly one step (`ss.constant` dwells, `dwells='exact'`), so the force of infection is `sum_l w_l I_l` and infection is `S >> I1 @ beta * force`.
- **Batch and groups.** The disease is stratified by `member` (the batch's members, as independent places) and `group` (the engine's groups, with each member's own sizes as fractions). The groups mix through `ss.contacts(group=C)`, which gives `beta sum_j C_ij I_j / N_j`. Members with different contact matrices run in separate sims; `beta` is `q f(t)` per member, an `ss.timeseries` over the sim's steps (or a number, without forcing).
- **The discrete map.** The ODE solver's map integrator (`ss.solvers.ode(integrator='map', dwells='exact')`) takes each step's infections once, at its start, as `S (1 - exp(-lambda))`, as v0.2 did.
- **Engine logic between sims.** Each sim runs whole, in one compiled call. A component acting during the epidemic ends one sim and starts the next, moving people from `S` to `R` in the carried state at the start of its step. The stop rule is applied to each step's results after the sim (the infections of each step, and `S`), and the steps after every member has stopped are dropped. A to_extinction run continues in a new sim, from the last state, in segments as before.
- **Outputs.** Unchanged code (`_outcomes`, `run_series`, `p_major` from the extinction equations, still derived, not simulated).
- **Same numbers.** Every benchmark keeps its v0.2 status (`benchmarks/results.yaml`). Batched and single runs agree to 1e-12 (K3, A35), as v0.2's K3 already allowed; v4 splits the population across a batch's members, so they differ by about 1e-16. A finished member is frozen in the outputs (its later infections are not counted), as v0.2 froze it in the module.
- **Gaps met.** G2 (no compiled discrete map: added to Starsim as `integrator='map'`), G4 (the live state after a run is one step past its last result: the carried state is read from the results) and G5 (`init=` places people in a one-step state's slots inconsistently: moved after `init()`), in `_V4Run`.
- **Speed.** `engine.py --self-test` takes 2.6 s (v0.2: 12 s); `selftest_age.py --self-test` 13.5 s (v0.2: 36 s), of which 6 s is the extinction equations (numpy, as in v0.2). Each sim is made with `copy_inputs=False`, since its modules are made for it.
- **Coverage pack.** `benchmarks/coverage_results.yaml` is still stamped for v0.2; `compat_proof.py` shows byte identity, which v4 doesn't give (decisions file, item 6).

v0.2 adds what age-structured allocation questions need. Each addition acts only when a configuration or a
component asks for it, so every v0.1 configuration gives byte-identical outputs.

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

- `benchmarks/results.yaml`: 61 of 61 assessable benchmarks are in band. The ten v0.2 benchmarks are AS1–AS6.
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
  recomputed.
- Self-tests: `engine.py --self-test` (v0.1 contract, batching, stop rule, pack subset, stamps) and
  `selftest_age.py --self-test` (v0.2, A1–A24), about 15 s and 25 s locally with two BLAS threads.
