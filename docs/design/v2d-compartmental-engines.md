# v2 workstream D: compartmental engine, yield mode and the spec changes they need (design note)

Status: design note for owner review (2026-10-31), written before any code. Branch `v2d-design`, PR into
`v2` (from 881e90c). No engine code and no cloud spend are part of this note.

## Why this workstream

Today v2 has two engines: the malaria engine (Starsim, benchmarked against trials) and the package-rate
engine (an empirical rate model fitted to a unit panel). Four of the five evaluation-suite question
families cannot run on v2. This note proposes the smallest set of changes that lets them run, built as
general capabilities rather than one engine per question.

| Question family | What it asks for | Why v2 cannot answer it today |
|---|---|---|
| Screening yield (one algorithm per unit, within a budget) | maximise the outcome a package *produces* | `schema.py` (line 500) accepts only `objective.sense: avert`. The package-rate engine needs a no-package baseline per unit, which screening records never contain. So `log_rate[unit]` and `lrr[package]` form a ridge. The covariance is then made positive-definite by jitter (`calibration.py` 178–180), and the probe's interval ran from −122,735,601 to 245,994 |
| Catch-up campaign (one age-range option per unit) | immunity gaps by birth cohort from coverage history and a serosurvey; outbreak size after one importation | no engine with cohort immunity or a final-size outbreak |
| Vaccine allocation (whole dose blocks to age groups, coverage caps) | an age-structured epidemic with a contact matrix, all-or-nothing protection, calibrated to last season | no engine; the outcome is not separable across age groups, but the optimiser assumes it is separable across units |
| Outbreak estimation (no allocation) | the reproduction number and the attack rate from weekly reported cases and a generation interval, with an unknown reporting fraction | no engine; no deliverable kind without an allocation; nothing carries a non-identified parameter through to the interval |

Findings this note starts from:
- **The tb probe** (scratchpad `v2probe/`): sense refused, effects unidentified, interval meaningless.
- **D-001 in the first blind run** (`tests/fixtures/v2blind1/critique_domain.yaml`). The interval was labelled
  "over calibration", but the sampler varied 6 of 16 calibrated parameters. The 10 unit baselines were held
  at their maximum-likelihood values. The cause is structural: `Spec.uncertainty_scope()` renders the label
  from the declared source *kinds*, not from what the draws actually varied.
- **The v1 baseline on the outbreak question** under-covered the attack-rate interval, because the
  reporting fraction was fixed rather than propagated.

## 1. Engines: one compartmental engine with two solvers, and a yield mode of the empirical engine

**Proposal: two changes, not four engines.**

| Engine | Change | Covers |
|---|---|---|
| `toolkit/engines/compartmental/` (new, numpy/scipy) | groups (age bands or birth cohorts) × a contact matrix; a susceptibility initialiser; interventions that move people out of S; two solvers: `final_size` and `renewal` | campaign, vaccine allocation, outbreak estimation, and later any closed-population epidemic question |
| `toolkit/engines/empirical/` (extended) | `reference` package and `structural_zero` packages: the rate is identified relative to an observed package, and a package that cannot produce the outcome returns zero by construction | screening yield, and any "rate per person reached" question |

### 1.1 Compartmental engine

**State.** One unit is a closed population split into `G` groups. A group is an age band (with a contact
matrix `C[i][j]`: mean daily contacts of a person in group i with people in group j) or a birth cohort (with
homogeneous mixing, `C` = ones). The engine has no births, deaths, waning or migration within the horizon.
These are stated on the card as out of range.

**Susceptibility initialisers** (engine config, chosen in the spec):
- `given`: the initial susceptible fraction `s_i` per group, from a unit attribute (for example a serosurvey
  proportion, calibrated with its sampling error).
- `cohort_history`: for each birth cohort, susceptibility after the doses it received. The doses come from
  dose-coverage history and per-dose seroconversion. Dose k reaches its coverage `c_k` and seroconverts a
  share `e_k` of those still susceptible. Whether a later dose goes preferentially to people who received an
  earlier one is a declared `dose_dependence` (`independent`, `nested`); it is not fitted. Cohorts are
  indexed by **birth year**, never by age, so a survey taken in year Y and a decision in year Y+1 shift
  cohorts by construction. A structural test checks this (§6, CK3). Groups declared always-immune or
  always-susceptible (for example by age bounds) come from the spec, with a source.
- **Survey observation model (harness, not engine).** A serosurvey band is a mixture of cohorts. Its
  expected seropositive share is the population-weighted immune share of those cohorts. The likelihood is
  binomial. How the survey and the coverage history are reconciled is the one place this design needs
  an owner decision (open question 4):
  - (a) a per-unit coverage-measurement factor `kappa`. It reads as a denominator error in the coverage
    data, has a declared prior range, and is calibrated to the survey;
  - (b) survey only, interpolated across cohorts by the coverage pattern.

  The spec declares one as primary; the other runs as a structure decision test (§2.4).

**Interventions.** Each component removes susceptibles before the epidemic. It reaches a share of the
targeted groups, and in those it protects a share `e` of the susceptibles it reaches. That share is called
**all-or-nothing** protection. A **leaky** variant (susceptibility multiplied by `1 − e`) is declared but
built only if a question needs it (out of scope here). Vaccination does not depend on immune status unless
the spec declares `targets_susceptible: true`, so doses given to immune people are wasted, as in practice.
Packages are either campaign options (which groups are targeted) or **dose vectors** (a whole number of
blocks per group; §2.2).

**Solver `final_size`** (no time axis). It solves the multigroup final-size relation

    z_i = s_i [1 − exp(−q Σ_j C_ij z_j)]          (z_i: share of group i infected over the epidemic)

by damped fixed-point iteration from `z = s`, with a residual check (tolerance 1e-10). It reports
`converged`, and an unconverged solve fails closed. `q` is the transmissibility per contact. It is
calibrated, or set from a declared R0 via the dominant eigenvalue of the next-generation matrix:
`R0 = q · ρ(C)` with all groups fully susceptible, and `Reff = q · ρ(diag(s) C)`. Outputs:
- per-capita infections per group (and cases, hospitalisations and other outcomes, through per-group
  outcome-per-infection ratios that are unit attributes or calibrated);
- window metrics `r0`, `reff_start`, `attack_rate` and `p_major`;
- cost drivers: doses per group.

**Importation.** With `importation: deterministic` the major-outbreak final size applies whenever
`Reff > 1`, and zero otherwise. With `importation: branching` the expected size is
`p_major · z`, where `p_major = 1 − 1/Reff` is the single-type branching approximation for one
introduction. The spec declares which.

**Solver `renewal`** (weekly or daily steps). It has susceptible depletion:

    I_t = (S_{t−1}/N) · R(t) · Σ_s w_s I_{t−s},   S_t = S_{t−1} − I_t,   R(t) = R0 · f(t)

- `w` is the given generation-interval distribution; it must sum to 1, and a gap or a negative value fails
  closed.
- `f(t)` is 1 (unforced) or a declared seasonal forcing (`cosine` with amplitude and phase parameters).
- Seeding is a calibrated initial incidence over the first generation interval.
- Outputs per window are the infections (so the harness can fit reported counts) and these metrics:
  - `attack_rate` = `(N − S_end)/N`;
  - `reff_end` = `R0 · S_end/N` (with `R0` at the declared reference phase when forced);
  - `growth_rate`;
  - the `R(t)` trajectory.

For one group with `f = 1`, the renewal epidemic's final size converges to the `final_size` solution. That
is an identity, and the pack checks it (§5).

**Why two solvers and not an ODE engine.** For a closed population with interventions before the epidemic,
final size gives the decision quantity exactly and fast (about 1 ms per solve). That makes per-draw
re-optimisation over thousands of packages cheap. Estimation from an incidence series needs a time axis;
the renewal equation is the SIR-type model written in terms of the generation interval the data come with.
A multigroup SIR ODE (`scipy.integrate.solve_ivp`) is written **only as a reference inside the benchmark
pack**, to check both solvers. It is not exposed to runs until a question needs timing, for example a
peak, vaccination during the season, or hospital capacity (out of scope).

**Why numpy/scipy and not Starsim.** These are deterministic compartmental systems, and the owner's rule
allows plain numpy/scipy where a final-size or ODE solver is simpler. A Starsim agent-based version would add
stochastic noise and run time to a quantity that has a closed form. If a later question needs stochastic
fade-out or contact heterogeneity beyond a matrix, a Starsim version goes behind the same contract and is
benchmarked against this one.

### 1.2 Empirical engine: a yield mode

`PackageRateEngine(components, outcome, reference=<package>, structural_zero=[<package>...],
structure=main_effects|cell)`. In this mode the rate is

    rate(unit, package) = exp(log_rate[unit] + lrr[package]),   lrr[reference] = 0

- `log_rate[unit]` is the rate under the **reference** package (observed in the panel), not under "none".
- A `structural_zero` package returns 0 for the outcome without a parameter. Its card output role is
  `produced` (§4): with no screening, the screening finds no cases. Its exposure-based cost drivers are 0.
- `structure: cell` gives every unit × package cell its own rate. It is the saturated alternative to
  `main_effects` and runs as a structure decision test.
- The current constructor (no `reference`) keeps today's behaviour exactly.

Rejected alternative: a separate `yield` engine. Its run function would be the same two lines as this one.

## 2. Decision-spec changes (all optional fields; every current spec validates unchanged)

### 2.1 Objective sense

| `objective.sense` | Benefit `b[u][p]` used by the optimiser | Reported headline |
|---|---|---|
| `avert` (default, today) | `outcome(comparator) − outcome(p)` | averted against the comparator |
| `minimise` | same as `avert` (an alias, normalised at load) | averted, and the plan's total beside it |
| `maximise` (new) | `outcome(p) − outcome(comparator)` | the plan's total, and the gain over the comparator |

- **One sign convention.** `Analysis.evaluate()` is the single place where the sign is applied, so stability,
  sensitivity, decision tests and claims all keep reading "benefit, higher is better".
- **Summaries.** `summary()` reports `difference` (averted or gained) and `total`. `deliverable.headline`
  gains `quantity: difference | total` (default `difference`, today's behaviour). The claims vocabulary
  ("averted" or "gained") comes from the sense, never from agent text. `cost_per_outcome_averted` becomes
  `cost_per_unit_benefit`, and the old key is kept for `avert`.
- **Structural check (schema).** `maximise` needs the engine card to declare the objective outcome's role as
  `produced`; `avert` and `minimise` need `prevented`. Maximising a prevented outcome, or averting a
  produced one, is refused before sign-off: these are sign errors, caught by structure rather than by
  reading the text.
- **Decision-test relevance rule.** The rule ("a test that only lowers the benefit of absent packages cannot
  move the plan") is written in benefit terms already. It holds unchanged, with a test for each sense.

### 2.2 Constraints

| Need | Spec form | Implementation |
|---|---|---|
| Fixed cost per unit when a package is delivered | already supported: a cost item with `basis: fixed` on a component (one charge per unit receiving it) | a self-test for two options that share one fixed campaign cost through a shared component. Note: `constraints.fixed_costs_usd` means money spent *outside* the allocation; the skill text says so to prevent confusion |
| Non-monetary envelope (doses, blocks, staff) | `constraints.resources: {doses: {amount: 770000, use: at_most \| exactly}}`; cost items gain `resource: doses` (default `usd`) | each resource becomes a toolkit/alloc cap, with weights from the cost matrix; `envelope_usd` becomes optional when a resource is given; `use: exactly` adds an equality row to the ILP |
| Indivisible blocks per group | `options.quantities: {v: {groups: [...], step: 70000, unit: doses}}` | expands at load into components `v@g` with integer levels `0..k_g`, at most one level per group (today's `group` rule). The package is the vector of levels |
| Coverage cap per group | `quantities.v.max_share: 0.7` of the group population (or `max_share_column`) | caps each group's level at `floor(max_share · N_g / step)`; infeasible vectors never enter the package set |
| Outcome not separable across the groups that share a budget | groups that interact (a contact matrix) are groups *inside one decision unit*, not separate units | the optimiser stays a multiple-choice knapsack. A coupled allocation is one unit whose packages are all feasible level vectors, evaluated jointly by the engine. The package space is enumerated, and its size is checked at load: above `max_joint_packages` (proposal: 50,000, in `thresholds.yaml`) the spec is refused (an optimiser for larger joint spaces is out of scope). For 11 blocks over 5 groups there are at most C(16,5) = 4,368 vectors before caps |

Coupling *between* decision units, such as spatial transmission between districts, stays out of scope.
The engine card declares `decision_coupling: independent_units`, and the schema refuses a spec that
declares units coupled.

### 2.3 Estimate-only deliverables

`deliverable.kind: estimates` (new, beside `package_per_unit`):

```yaml
deliverable:
  kind: estimates
  outputs:                                   # each from an engine window metric, named for the report
    - {name: <reported name>, metric: reff_end, window: season, scale: ratio}
    - {name: <reported name>, metric: attack_rate, window: season, scale: proportion}
  decision:                                  # optional: a threshold rule computed by the harness
    {kind: threshold, output: <reported name>, threshold: 1.0, direction: above,
     probability: 0.95, statements: {yes: "...", no: "...", undetermined: "..."}}
```

- With `kind: estimates`, the sections `options`, `constraints` and `comparator` may be absent. The point,
  stability and optimisation steps write their artifacts with `not_applicable` and the reason, so the
  contract check still sees every expected artifact.
- The threshold rule returns `yes` when `P(output > threshold) ≥ probability`, `no` when it is
  `≤ 1 − probability`, and `undetermined` otherwise. The probability comes from the uncertainty draws.
- The statement is chosen by the harness, and the writer cites it as a claim.

### 2.4 Uncertainty and decision tests

**New uncertainty kinds.**
- `priors`: parameters the data do not inform, drawn from their declared prior.
- `observation`: sampling error of survey proportions and counts. This kind is covered automatically when
  calibration is `posterior` (the likelihood carries it); it must be listed or excluded like the others.

**Per-parameter coverage (the D-001 fix, §3.3).** `uncertainty.excluded` accepts `{parameter: <name>,
reason}` as well as `{kind, reason}`.

**Per-draw re-optimisation over a joint package space.** One final-size solve per package per draw: 4,368
packages × 1,000 draws × about 1 ms is about 75 s on one core. This is cheap enough that `direct`
evaluation stays the default for the compartmental engine, and no emulator is needed.

**New decision-test types.**
- `prior_range`: each parameter classified prior-dominated (§3.2) is set to each end of its declared range,
  and the plan or threshold decision is recomputed. The harness generates one automatically for every
  prior-dominated parameter that the decision depends on. The spec may not remove it.
- `structure`: the analysis is re-run with an alternative engine configuration named in the signed spec,
  for example `structure: cell` versus `main_effects`, forced versus unforced renewal, or observation model
  (a) versus (b). It reports whether the plan, or the threshold decision, changes.

**Existing types.** `envelope` takes a resource amount as well as US dollars. On an `estimates`
deliverable, the "plan changes" criterion becomes "the threshold decision changes" (`share_units_changed`
is not applicable).

## 3. Calibration and identifiability

### 3.1 A `posterior` calibration method for low-dimensional problems

Compartmental calibrations have few global parameters: for example `q` plus seeding, `R0` plus reporting
plus dispersion, or one factor per unit. Each comes with a declared prior. The method:

```yaml
calibration:
  method: posterior
  priors:                                     # every parameter the calibration may move, with its source
    reporting: {dist: loguniform, range: [0.0333, 0.333], source: "<citation or the question>", verified: unverified}
  targets:
    - {csv: ..., kind: incidence_series | survey_proportion | group_attack_rate | count_panel, ...,
       observation: negbin | binomial | poisson, links: {...}}
  per_unit: [kappa]                           # optional: independent per-unit parameters (posterior factorises by unit)
```

**Observation models** (new, `toolkit/analysis/observation.py`, harness): Poisson, negative binomial
(dispersion as a parameter with a prior) and binomial. A reporting or detection fraction is an
**observation parameter**, never an engine parameter. So engines stay mechanistic, and the data-quality
assumption is visible in the spec.

**Sampler**, chosen by dimension and reported:
- up to 4 parameters: a scrambled Sobol design of 2^14 points over the prior, with importance weights from
  the likelihood. One adaptive stage then refines the high-weight region (a mixture proposal), followed by
  resampling. The effective sample size is reported, and an ESS below the floor (proposal: 1,000) fails closed;
- above 4 parameters: adaptive Metropolis, 4 chains, with R-hat ≤ 1.01 and bulk ESS ≥ 400, or the step fails
  closed.

Per-unit parameters with no shared parameter are sampled unit by unit (a factorised posterior).

**Draws carry the correlations.** Uncertainty draws are posterior samples taken jointly, so correlations
between parameters, such as reporting fraction against R0 or unit rates against effects, are carried
automatically.

**The existing `likelihood` method stays** for count panels (the yield mode). Two changes:
- It is refused when `identifiability.py` reports a ridge or the condition number exceeds a threshold
  (proposal 1e8). The jitter at `calibration.py` 179–180 becomes a fail-closed finding
  (`calibration_ridge`), which tells the planner to reparameterise (for example `reference:`) or to declare
  priors and use `posterior`.
- Every fitted unit attribute (`z:<unit>`) enters the joint draws, which it already does via the MVN; §3.3
  makes this checkable.

### 3.2 Non-identified parameters stay at their declared range, never fixed

**Contraction.** For each parameter, calibration reports the prior-to-posterior **contraction**,
`1 − var_post/var_prior` (on the prior's own scale, for example log for a log-uniform prior), alongside the
profile likelihood from `identifiability.py`. Classes (thresholds in `thresholds.yaml`, proposals):
- `identified` (contraction ≥ 0.5);
- `partly_identified` (0.1–0.5);
- `prior_dominated` (< 0.1).

**Consequences.** A prior-dominated parameter:
- is drawn from its posterior, which is about its prior, so its whole declared range reaches the interval;
- gets an automatic `prior_range` decision test (§2.4);
- is named in the interval label ("its width is set mainly by the declared range of `reporting`,
  1/30–1/3").

The report section "what the data do and do not determine" is rendered from these classes. It is a
harness-written claim set, not prose.

**Fixing a parameter.** A parameter can be fixed only through `uncertainty.excluded: {parameter, reason}`
in the signed spec. The label then says "not covered: <parameter> (<reason>)".

**No silent defaults.** A parameter that has no prior and no data is a spec error, not a default. In
particular, R0 given as a range in the question is a `priors` source with that range; nothing picks a
midpoint.

### 3.3 The structural check for D-001: realised draws, not declared kinds

- `uncertainty.yaml` records `varied`. This is computed from the **realised draw matrix**: every parameter,
  unit attribute and observation parameter whose sampled values have non-zero spread, with its sd and its
  source kind.
- `calibration.yaml` records `estimated`: every quantity the calibration moved, including per-unit
  attributes, with its posterior or MVN sd.
- **New contract check** `analysis_contract_uncovered_parameter` (HIGH, fail closed). For every name in
  `estimated`, in the `priors` section, or among the engine's fitted parameters with a range, one of these
  must hold:
  - (i) it is in `varied` with a spread of at least `min_ratio` (proposal 0.5) × its calibration sd (or prior
    sd);
  - (ii) it is excluded by name in the signed spec.

  A quantity held at its estimate while `calibration` is a declared source fails (i) and (ii). That is the
  D-001 case: 10 unit baselines with sd 3–9% each and draw spread 0.
- **The label is rendered from `varied` and the exclusions**, not from `sources` kinds
  (`uncertainty_scope()` changes accordingly). A label can no longer claim more coverage than the draws
  have.
- **Test-first fixture.** The v2blind1 calibration and uncertainty artifacts are not in the repository; only
  the critique YAML is. The test therefore rebuilds the case on the toy engine with likelihood calibration:
  draws that vary only `lrr:*` must fail, and full MVN draws must pass.

## 4. Engine card and `engine_needs` vocabulary

Today `card_fits()` matches names only (components, outputs, windows, attributes). A configured engine
(the package-rate engine takes its components from the spec) therefore fits almost any spec. The proposal
is a closed vocabulary of **capabilities**, defined in `toolkit/engines/contract.py` (a typo fails closed).
It goes on `EngineCard.capabilities`, optional with default `{}`, so existing cards load unchanged.
Capabilities do not depend on the engine's configuration.

| Key | Values | Derived from the spec by `engine_needs()` |
|---|---|---|
| `dynamics` | `none`, `closed_epidemic_final_size`, `closed_epidemic_renewal`, `agent_based_endemic` (malaria) | the deliverable's metrics and the calibration target kinds |
| `outcome_role` | per outcome: `prevented`, `produced` | `objective.sense` |
| `intervention_action` | `reduce_rate`, `remove_susceptible_all_or_nothing`, `reduce_susceptibility_leaky`, `detect` | `options.components.<c>.action` (new optional field) |
| `structure` | `single_group`, `age_groups_contact_matrix`, `birth_cohorts` | `decision_units.populations` (several groups with a matrix, or cohorts) |
| `calibration_data` | `count_panel`, `survey_proportion`, `group_attack_rate`, `incidence_series`, `coverage_history` | `calibration.targets[].kind` |
| `estimands` | `r0`, `reff_start`, `reff_end`, `attack_rate`, `growth_rate`, `p_major` | `deliverable.outputs[].metric` |
| `decision_coupling` | `independent_units`, `joint_within_unit` | `options.quantities` present → `joint_within_unit` |

- **Matching.** `card_fits()` adds: every needed capability value must be in the card's list. A card without
  `capabilities` fits only specs that need none, which is today's behaviour.
- **Malaria card.** It gains `dynamics: agent_based_endemic`, `outcome_role: {cases: prevented, ...}` and
  `intervention_action: [reduce_rate]`. So it is no longer eligible for a final-size or renewal spec, and the
  compartmental engine is not eligible for an endemic vector-borne allocation.
- **Explicit `engine_needs`.** When the spec gives `engine_needs` explicitly (allowed today), the schema checks
  that it is a superset of the derived needs. A planner can add needs but cannot drop one the spec implies.

## 5. Benchmark packs

The compartmental engine has **no engine-level fitted parameters**: every parameter is calibrated per run
or declared per question. Its pack is therefore pure validation. Every benchmark has role `held_out`, and
`fit` is empty. The runner gains three estimand types, `value` (one output against a target), `identity`
(two computations of one quantity) and `coverage` (simulation-based calibration). The coverage type runs
in a separate pack file, because it exercises the harness calibration, not the engine alone.

**Compartmental engine, `final_size` solver**

| ID | Result | Source | Band |
|---|---|---|---|
| FS1 | single-group final size `z = 1 − exp(−R0 z)` at R0 = 1.5, 2, 4, 12, 18 | Kermack–McKendrick (1927); closed form via the Lambert W function | abs. 1e-8 |
| FS2 | R0 ≤ 1 gives no major outbreak (z = 0); R0 = 1 + ε gives z ≈ 2ε | threshold theorem | abs. 1e-6 |
| FS3 | multigroup with a rank-one (proportionate) matrix reduces to the scalar relation | Ma & Earn (2006), *Bull Math Biol*; Andreasen (2011) | rel. 1e-6 |
| FS4 | all-or-nothing vaccination of a share v equals final size at `R0(1 − v·e)`; zero at `v·e ≥ 1 − 1/R0` | herd-immunity identity | rel. 1e-8 |
| FS5 | `R0` from the next-generation matrix equals the growth-rate-consistent R0 of the reference ODE | Diekmann, Heesterbeek & Metz (1990) | rel. 1e-6 |
| FS6 | multigroup final size equals the reference ODE run to extinction, for 3 contact matrices (identity, rank-one, an asymmetric 5×5) | the pack's ODE (`solve_ivp`, rtol 1e-10) | abs. 1e-5 |
| FS7 (held out) | published age-specific final sizes from a contact-matrix model | candidates: Mossong et al. (2008) POLYMOD-based results; Wallinga, Teunis & Kretzschmar (2006, *AJE*) age-specific attack-rate fit. Values are transcribed with a quoted span through the evidence service before the pack is written; until then the benchmark is `not_assessable` | the paper's reported precision |

**Compartmental engine, `renewal` solver**

| ID | Result | Source | Band |
|---|---|---|---|
| RN1 | in the growth phase, `R = 1 / Σ_s w_s e^{−r s}` for the observed growth rate r | Wallinga & Lipsitch (2007, *Proc R Soc B*) | rel. 1e-3 |
| RN2 | unforced single-group cumulative attack rate converges to the FS1 solution | Diekmann, Heesterbeek & Britton (2013), ch. 1 | abs. 2e-3 (discretisation) |
| RN3 | `reff_end = R0 · S_end/N`, and `reff_end < 1` after an unforced major epidemic | identity | rel. 1e-9 |
| RN4 | a weekly series simulated by the reference ODE (aggregated to weeks), refitted: R0 recovered | the pack's ODE | rel. 5% |
| RN5 (held out) | published reproduction-number estimates from incidence series | Cori et al. (2013, *AJE*) worked examples (pandemic influenza 1918, SARS 2003, Hagelloch measles 1861); values transcribed as for FS7 | the paper's 95% CrI |

**Cohort initialiser**

| ID | Result | Band |
|---|---|---|
| CI1 | susceptibility after doses with given coverages and seroconversion, `independent` and `nested` dependence, against hand-computed values | abs. 1e-12 |
| CI2 | a cohort's susceptibility is invariant to the survey-year/decision-year shift (birth-year indexing) | exact |
| CI3 | a mixed-cohort survey band's expected positivity equals the population-weighted mean of its cohorts | abs. 1e-12 |

**Empirical engine, yield mode**

| ID | Result | Band |
|---|---|---|
| EY1 | the reference parameterisation gives the same fitted cell rates as an unconstrained Poisson GLM (statsmodels as the reference) | rel. 1e-6 |
| EY2 | a `structural_zero` package returns 0 with no parameter; `maximise` against it equals the plan's total | exact |
| EY3 | a panel without the reference package is refused (`calibration_ridge`), not drawn | exact |

**Coverage pack (`coverage.yaml`)**

The pack uses simulation-based calibration of the harness calibration plus uncertainty chain. Its
synthetic generator is the reference ODE or an independent Poisson generator, not the engine under test.
Each case runs 200 replicates.

| ID | Case | Pass |
|---|---|---|
| SB1 | identified renewal fit (unforced, a reporting prior that the data can contract) | 95% interval coverage of R0, the attack rate and `reff_end` in [0.90, 0.99] (binomial SE ≈ 1.5%) |
| SB2 | non-identified renewal fit (forcing present, so the turnover does not pin reporting down) | coverage of the attack rate in [0.90, 0.99] **with** the prior range carried; `reporting` classified `prior_dominated` in ≥ 90% of replicates; coverage **below 0.80** when `reporting` is fixed at its prior median (the negative control that reproduces the v1 failure) |
| SB3 | survey-calibrated cohort immunity (per-unit `kappa`) | coverage of the unit susceptible share in [0.90, 0.99] |
| SB4 | yield-mode panel (unit rates and package ratios) | coverage of the plan total in [0.90, 0.99]; the D-001 negative control (unit rates held at the MLE) is below the band |
| SB5 | group-attack-rate calibration of `q` | coverage of hospitalisations-averted for a fixed plan in [0.90, 0.99] |

**Held-out benchmarks and gating.** The held-out published benchmarks (FS7, RN5) are reported but do not
block until their values are transcribed. The analytic identities (FS1–6, RN1–4, CI, EY) and the coverage
pack must pass before any run uses the engine, and `card.yaml` records `results.yaml`'s sha256 as the
malaria card does. Every pack result carries the engine's code fingerprint, so a code change invalidates
it.

## 6. Build plan (five sub-commits, each test-first)

| Commit | Content | Tests written first (each must fail on 881e90c) |
|---|---|---|
| α spec | `sense` (`maximise`, `minimise` alias); `headline.quantity`; `constraints.resources`; `options.quantities` with caps and the joint-package limit; `deliverable.kind: estimates` with the threshold rule; `priors` and `observation` kinds; per-parameter exclusions; `capabilities` vocabulary in contract.py; `engine_needs` derivation and the superset check; the sense/outcome-role check | SP-a1 `maximise` validates and `avert` specs are byte-identical after load; SP-a2 maximising a `prevented` outcome is refused; SP-a3 quantity expansion yields C(16,5) vectors for 11 steps over 5 groups and drops capped ones; SP-a4 an over-limit joint space is refused; SP-a5 an `estimates` spec without options validates; SP-a6 a needs-superset violation is refused; SP-a7 every existing self-test spec validates unchanged |
| β analysis | sign in `evaluate()`; `summary()` totals; resource caps into toolkit/alloc; the `estimates` path (not-applicable artifacts); observation models; `posterior` calibration and contraction classes; ridge refusal in `likelihood`; realised-draw `varied` and the label from it; `analysis_contract_uncovered_parameter`; `prior_range` and `structure` decision tests | AN-b1 the toy engine with a produced outcome: the maximise plan equals the brute-force optimum; AN-b2 the D-001 rebuild fails the new check and full draws pass; AN-b3 a ridge panel stops with `calibration_ridge` (today: jittered MVN); AN-b4 a prior-dominated toy parameter gets an automatic `prior_range` test and is named in the label; AN-b5 a threshold decision on toy draws gives yes/no/undetermined at the right probabilities; AN-b6 `use: exactly` resource is honoured by ILP and brute force |
| γ empirical yield | `reference`, `structural_zero`, `structure: main_effects \| cell`; card capabilities; pack EY1–3 and SB4 | EY1–3 as in §5; E5 the old constructor's outputs are unchanged |
| δ compartmental engine | groups, contact matrix, initialisers (`given`, `cohort_history`), components, `final_size` and `renewal` solvers, card; the pack's reference ODE; runner estimand types `value`, `identity` and `coverage`; packs FS, RN, CI and SB1–3, SB5 | contract test (`check_engine`); FS1–6, RN1–4, CI1–3 written as failing tests before the solvers; SB1–SB3 and SB5 run on the dev VM |
| ε wiring and retro | register the engine in `ENGINES`; capabilities on the malaria card; the planner's `decision-spec` skill gains the new fields (vocabulary only, no thresholds); CHANGELOG row and `improvement-plan.md` update; local dry run of each family's spec on the harness with **no model calls** (hand-written specs from the public inputs, kept out of the repository) to confirm each validates, chooses the right engine and produces every artifact | T-e1 engine choice selects exactly one eligible engine for each of the four family specs and the malaria spec; T-e2 every script's `--self-test` passes |

**Cost (estimates, to be confirmed before spending).**
- **VM.** The pack work is light: analytic checks run in seconds. The coverage pack is 5 cases × 200
  replicates × one calibration (under 1 s for Sobol importance sampling on vectorised numpy): about
  20–40 minutes on the 8-vCPU dev VM with set-up, under US$5. Nothing heavy runs on the laptop. The
  malaria pack is not re-run, because only its card text changes (the code fingerprint does not).
- **Model calls.** One v2 run per question, four runs, after ε merges. This note does not have a measured
  per-run spend for v2. It should be read from the first blind run's `run_state.json` before asking. As
  a planning range: US$15–40 per run, US$60–160 for four. This is above the US$10 approval line, so the
  runs wait for an explicit owner go-ahead with the measured figure.

**Out of scope.**
- An ODE solver exposed to runs (timing, peaks, vaccination during the season).
- Leaky protection.
- Births, deaths, waning and migration.
- Stochastic or agent-based versions.
- Coupling between decision units (spatial transmission; LASER later).
- Hierarchical pooling across units beyond `main_effects`/`cell`.
- An optimiser for joint package spaces above the limit.
- Cost-effectiveness of a campaign when no costs are given.
- The evidence service (priors stay `unverified` with a source until V5).
- Deleting gate checks the new contract check supersedes (listed in the PR, not deleted).

## 7. Risks and open questions for the owner

**Risks**
1. **Prior-driven answers.** Where the data do not identify a parameter, the interval reflects the
   declared prior. That is honest, but a wrong prior range gives a confidently wrong interval. Mitigations:
   - the label and the "what the data do not determine" section name the parameter and its range;
   - the `prior_range` test shows whether the decision depends on it;
   - the priors are in the signed spec.
2. **Structure choice is still a choice.** Forced or unforced renewal, and observation model (a) or (b),
   can change the answer. The `structure` test reports it, but the primary structure is signed, not
   learned.
3. **Final-size outcomes ignore timing.** Final size is wrong for decisions where the epidemic is
   interrupted (by a season's end or a mid-epidemic intervention). The card states this range, and the
   ODE solver is the planned extension.
4. **The joint-package limit.** The limit makes larger dose-allocation problems fail closed rather than be
   solved approximately.
5. **Coverage pack noise.** 200 replicates give about ±1.5% on the coverage estimate. A borderline engine
   could pass or fail by chance. The pack therefore fixes its seeds and reports the binomial interval.

**Open questions**
1. **Sense vocabulary.** Is `maximise` plus `minimise` (as an alias of `avert`) right, or should `minimise`
   report levels only, with no "averted"?
2. **Joint allocation as one decision unit with enumerated packages.** Is the limit of 50,000 packages
   acceptable, or is a structured optimiser (for example marginal-gain with a bound) wanted now?
3. **Calibration method.** Adopt `posterior` (declared priors, Sobol importance sampling or adaptive
   Metropolis) beside `likelihood`, and refuse ridged likelihood fits instead of jittering them?
4. **Survey reconciliation.** For cohort immunity, is a per-unit coverage-measurement factor (read as a
   denominator error, with a prior) acceptable under "mechanisms over fitted correction factors", or
   should survey-only (b) be primary?
5. **Numpy/scipy, not Starsim,** for the compartmental engine?
6. **Gating on published benchmarks.** May the engine be used while FS7 and RN5 are `not_assessable`
   (values not yet transcribed), given that the analytic and coverage packs pass?
7. **Thresholds.** Do the proposed values suit you: contraction classes 0.1/0.5, ESS floor 1,000, R-hat
   1.01, the coverage band [0.90, 0.99], the realised-spread ratio 0.5, and condition number 1e8? All of
   them go in `thresholds.yaml`.
8. **Spend.** Approval for four v2 runs after ε, at the spend measured from the first blind run.
