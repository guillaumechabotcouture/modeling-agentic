# v2 workstream D: compartmental engine, yield mode and the spec changes they need (design note)

Status: design note for owner review (2026-10-31), written before any code. Branch `v2d-design`, PR into
`v2` (from 881e90c). No engine code and no cloud spend are part of this note. Updated 2026-11-01 with the
owner's answers to open questions 4, 5, 6 and 10 (§7): the compartmental engine is built on Starsim (§1.1),
every observation source gets a measurement model (§3.4), the engine may run before the published
benchmarks are transcribed, with a label (§5), and `contract.py` stays unchanged (§4).

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

## 1. Engines: one Starsim compartmental engine with two run modes, and a yield mode of the empirical engine

**Proposal: two changes, not four engines.**

| Engine | Change | Covers |
|---|---|---|
| `toolkit/engines/compartmental/` (new, Starsim) | a Starsim compartmental module: groups (age bands or birth cohorts) × a contact matrix; a susceptibility initialiser; interventions that move people out of S; one discrete-time transmission step, run in two modes: `to_extinction` (final-size questions) and `windowed` (incidence series) | campaign, vaccine allocation, outbreak estimation, and later any closed-population epidemic question |
| `toolkit/engines/empirical/` (extended) | `reference` package and `structural_zero` packages: the rate is identified relative to an observed package, and a package that cannot produce the outcome returns zero by construction | screening yield, and any "rate per person reached" question |

### 1.1 Compartmental engine

**State.** One unit is a closed population split into `G` groups of size `N_i`. A group is either:
- an age band, with a contact matrix `C[i][j]`: mean daily contacts of a person in group i with people in
  group j;
- a birth cohort, with homogeneous mixing. That is `C_ij = c · N_j / N`, population-weighted, **not** a
  matrix of ones. A matrix of ones would weight a small cohort like a large one (Codex review, finding 1).

The transmissibility `q` is per contact, integrated over the infectious period, so `q · C` is
dimensionless. The engine has no births, deaths, waning or migration within the horizon. These are stated
on the card as out of range.

**Built on Starsim** (decided 2026-11-01, owner). The stack is our own Python and Starsim code; no R,
malariasimulation, EMOD or other external simulator. The engine follows the compartmental pattern in the
`starsim-dev-nonstandard` skill: one `ss.Module` subclass, `GroupEpidemic`, holds group-level state as
numpy vectors (`S`, `I` history, cumulative infections, length `G`) instead of per-agent arrays, and runs
inside an `ss.Sim` with no network and no demographics. Group sizes `N_i` come from the unit attributes, not
from the number of agents, so the sim's `People` object is a minimal placeholder. Interventions are
`ss.Intervention` subclasses that act at the start of the sim, and outputs are `ss.Result` series plus the
window metrics below. The malaria engine already runs this way (`build_sim()` returns an `ss.Sim`), so the
harness, the batch runner and the benchmark runner need nothing new to run it.

**Susceptibility initialisers** (engine config, chosen in the spec):
- `given`: the initial susceptible fraction `s_i` per group, a latent quantity whose value comes from
  calibration to the declared observation sources (§3.4), never copied from one survey.
- `cohort_history`: for each birth cohort, susceptibility after the doses it received. Dose k reaches a
  **latent true coverage** `c_k` and seroconverts a share `e_k` of those still susceptible. Whether a later
  dose goes preferentially to people who received an earlier one is a declared `dose_dependence`
  (`independent`, `nested`); it is not fitted. Cohorts are indexed by **birth year**, never by age, so a
  survey taken in year Y and a decision in year Y+1 shift cohorts by construction. A structural test checks
  this (§6, CI2). Groups declared always-immune or always-susceptible (for example by age bounds) come from
  the spec, with a source.
- The engine never reads administrative coverage or survey results directly. Each data source is an
  **observation** of the latent coverage or immunity, with its own sampling frame and measurement bias,
  modelled in the harness (§3.4). Neither source is treated as the truth.

**Interventions.** Each component removes susceptibles before the epidemic. It reaches a share of the
targeted groups, and in those it protects a share `e` of the susceptibles it reaches. That share is called
**all-or-nothing** protection. A **leaky** variant (susceptibility multiplied by `1 − e`) is declared but
built only if a question needs it (out of scope here). Vaccination does not depend on immune status unless
the spec declares `targets_susceptible: true`, so doses given to immune people are wasted, as in practice.
Packages are either campaign options (which groups are targeted) or **dose vectors** (a whole number of
blocks per group; §2.2).

**The transmission step.** One discrete-time step of length `dt` (one day or one week, `ss.days(...)`),
with susceptible depletion, in the bounded exponential-survival form:

    I_i,t = S_i,t−1 · [1 − exp(−λ_i,t)],   λ_i,t = q f(t) Σ_j C_ij Σ_s w_s I_j,t−s / N_j,   S_i,t = S_i,t−1 − I_i,t

- `w` is the generation-interval distribution on the `dt` grid; it must sum to 1, and a gap, a negative
  value or a grid that differs from `dt` fails closed.
- `f(t)` is 1 (unforced) or a declared seasonal forcing (`cosine` with amplitude and phase parameters).
- `q` is calibrated, or set from a declared R0 via the dominant eigenvalue of the next-generation matrix:
  `R0 = q · ρ(C)` with all groups fully susceptible, and `Reff = q · ρ(diag(s) C)`.
- This form never infects more than the remaining susceptibles. With `f = 1`, summing the log survival over
  the epidemic gives `log(S_i,∞/S_i,0) = −q Σ_j C_ij z_j`, with `z_j` the cumulative infections of group j
  (seeds included) over `N_j`. That is the multigroup final-size relation, exactly, at any step length. The
  linear form `I_t = (S_{t−1}/N) R Σ w I` does not satisfy it: with `w_1 = 1` and `R0 = 2`, it ends at an
  attack rate of 0.8687 against the exact 0.7968 (Codex review, finding 2, reproduced).

So one Starsim module answers both question types. The run mode is engine config:
- **`to_extinction`** (campaign, vaccine allocation). The sim runs until incidence falls below a declared
  tolerance (proposal: `1e-9 · N`), within a step cap. An epidemic still running at the cap reports
  `converged: false` and fails closed. Outputs:
  - per-capita infections per group (and cases, hospitalisations and other outcomes, through per-group
    outcome-per-infection ratios that are unit attributes or calibrated);
  - window metrics `r0`, `reff_start`, `attack_rate` and `p_major`;
  - cost drivers: doses per group.
- **`windowed`** (outbreak estimation). The sim runs over the horizon, and the harness fits reported counts
  per window. Seeding is a calibrated initial incidence over the first generation interval. Outputs per
  window are the infections and these metrics (Codex review, finding 5):
  - `attack_rate`: infections *within the window* divided by `N`. This is new infections only;
    pre-existing immunity is never counted;
  - `reff_end` = `R0 · f(t_end) · S_end/N`: the reproduction number at the window's end;
  - `reff_ref` = `R0 · f_ref · S_end/N`: at a declared reference phase, for example a season's start. With
    `f = 1`, `reff_end` and `reff_ref` coincide;
  - `growth_rate`;
  - the `R(t)` trajectory.

  A deliverable must name which reproduction number it reports.

**Importation.** The spec declares the group the introduction enters (or a distribution over groups) and
one of two modes:
- `importation: deterministic`: the sim is seeded in the introduction group only, so infection reaches only
  the groups connected to it in the contact graph; no maximal solution over disconnected components is
  taken. Below threshold the deterministic run gives a minor outbreak of about the seed's size, which the
  benchmarks bound (FS2).
- `importation: branching`: the expected size is `p_major · z`. `p_major` comes from the multitype
  extinction equations, `x_i = G_i(x)`, the probability that a chain started by one case in group i dies
  out, given a declared offspring distribution:
  - `poisson`: `G_i(x) = exp(−Σ_j K_ji (1 − x_j))`, with `K` the next-generation matrix;
  - `geometric` (an exponential infectious period).

  `p_major` is 0 when `Reff ≤ 1`. The scalar `1 − 1/Reff` holds only for one group with geometric
  offspring and is not used otherwise (Codex review, finding 4). A deterministic sim has no trajectory
  that gives a probability of extinction, so `p_major` is the one quantity the engine computes from an
  equation rather than from the sim. It is labelled as derived on the card, and BR1 checks it.

**Determinism and seeding.** The module draws no random numbers: it uses no `ss` distributions and no
networks. The contract's `seed` is still passed to `ss.Sim(rand_seed=seed)` and recorded in `Outcomes.meta`,
as the malaria engine does. A contract self-test asserts that outputs are **identical across seeds**, which
is stronger than today's same-seed check in `check_engine`. A stochastic variant (chain-binomial, for
fade-out) would draw from Starsim's seeded random streams behind the same contract; it is out of scope.

**Runtime per evaluation, and batching.** A Starsim sim has a fixed cost per sim (initialisation) and per
step (the module loop), which a closed-form solve does not. The estimate, to be measured in δ before
anything depends on it, is 20–100 ms per sim for one unit, 5 groups and 50–200 steps on one core. One sim
per evaluation is too slow in two places:
- per-draw re-optimisation over a joint package space: 4,368 packages × 1,000 draws × 50 ms is about
  60 core-hours;
- `posterior` calibration (§3.1): 2^14 prior points × 50 ms is about 14 minutes per fit, and the coverage
  pack fits 1,400 times.

So the module's state carries a leading **batch axis**: one sim evaluates many (parameter set, package)
pairs at once, as vectors of shape batch × groups, at close to the cost of one sim. The engine exposes this
as an optional `run_batch(param_sets, unit, packages, horizon, seed)` method beside the contract's `run()`.
`contract.py` is unchanged, and the harness uses the method when an engine has it. A contract test checks
that `run_batch()` equals `run()` pair by pair (rel. 1e-12). With batching:
- per-draw re-optimisation is 1,000 sims per unit, about 1–2 minutes on one core, so `direct` evaluation
  stays the default;
- a calibration is about 16 sims of 1,024 points, a few seconds.

If the δ measurement is more than 5× these estimates, the existing emulator path is the fallback, and the
note comes back to the owner before ε.

**Analytic solvers are test oracles only** (decided 2026-11-01, owner). The multigroup final-size fixed
point, the Lambert W closed form, the herd-immunity identity, the Wallinga–Lipsitch relation and a
multigroup SIR ODE (`scipy.integrate.solve_ivp`) are written **inside the benchmark pack** (§5), to check the
Starsim engine. None is exposed to runs. If a question later needs timing within a season (a peak,
vaccination during the season, hospital capacity), it is a new run mode of the same Starsim module with
continuous-time progression, benchmarked against the pack's ODE (out of scope).

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

- **One sign convention, in one function.** A new `core.benefit(spec, y_package, y_comparator)` is the only
  place where the sign is applied. Today the same subtraction is repeated in three places, and each one
  must call the new function (Codex review, finding 6):
  - `Analysis.evaluate()` and `summary()`;
  - `uncertainty.run()`, whose per-draw headline repeats it;
  - `decision_tests._interaction()`, whose `r_inc < 1` admissibility test also assumes a prevented
    outcome. That admissibility becomes explicit per outcome role.

  A grep-based self-test fails if the comparator-minus-package expression appears anywhere else under
  `toolkit/analysis/`.
- **Stability on the new paths** (Codex review, finding 7).
  - `stability.run()` today treats any method other than inversion or likelihood as "nothing calibrated" and
    skips the check. It gains a branch for `posterior`: each fold refits the posterior without the held-out
    group. Any method it does not know becomes a fail-closed finding, not a skip.
  - A single joint unit has no groups to hold out, so stability writes `not_applicable` with that reason.
  - Value lost is normalised by `|optimum − comparator|`, which stays meaningful when the
    comparator-relative benefit is zero or negative. Today it returns 0 there.
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
| Non-monetary envelope (doses, blocks, staff) | `constraints.resources: {doses: {amount: 770000, use: at_most \| exactly}}`; cost items gain `resource: doses` (default `usd`) | each resource becomes a toolkit/alloc cap, with weights from the cost matrix; `envelope_usd` becomes optional when a resource is given. In α, `use` is `at_most` only. `exactly` needs lower bounds carried through `prune()` (whose dominance assumes upper bounds and would drop the package an equality needs), the ILP, brute force and `check()` (Codex review, finding 8). That is deferred unless the owner wants it (open question 9) |
| Indivisible blocks per group | `options.quantities: {v: {groups: [...], step: 70000, unit: doses}}` | toolkit/alloc packages are component *sets*, so levels cannot be values (Codex review, finding 9). The expansion makes one component per level, `v@g:1 … v@g:k_g`, sharing the group `v@g` (so at most one per group, today's rule). Each level component gets its own generated cost item (level × step × price), resource weight and `engine_map` entry (`{v_g: level}`). The package is the set of chosen levels, so nothing in toolkit/alloc changes |
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
- `observation`: the sampling error and the measurement-bias parameters of each declared observation
  source (§3.4). This kind is covered automatically when calibration is `posterior` (the likelihood and the
  bias priors carry it); it must be listed or excluded like the others.

**Per-parameter coverage (the D-001 fix, §3.3).** `uncertainty.excluded` accepts `{parameter: <name>,
reason}` as well as `{kind, reason}`.

**Per-draw re-optimisation over a joint package space.** One batched Starsim sim per draw evaluates every
package of the unit (§1.1): 1,000 draws is about 1–2 minutes on one core, to be measured in δ. `direct`
evaluation stays the default for the compartmental engine, with the emulator path as the fallback.

**New decision-test types.**
- `prior_range`: each parameter classified prior-dominated (§3.2) is set to each end of its declared range,
  and the plan or threshold decision is recomputed. The harness generates one automatically for every
  prior-dominated parameter that the decision depends on. The spec may not remove it.
- `structure`: the analysis is re-run with an alternative engine configuration named in the signed spec,
  for example `structure: cell` versus `main_effects`, forced versus unforced renewal, or an alternative
  measurement-bias model for one observation source (§3.4). It reports whether the plan, or the threshold
  decision, changes.

**Existing types.** `envelope` takes a resource amount as well as US dollars. On an `estimates`
deliverable, the "plan changes" criterion becomes "the threshold decision changes" (`share_units_changed`
is not applicable).

## 3. Calibration and identifiability

### 3.1 A `posterior` calibration method for low-dimensional problems

Compartmental calibrations have few global parameters: for example `q` plus seeding, `R0` plus reporting
plus dispersion, or latent coverage per unit with a few measurement-bias parameters per source (§3.4).
Each comes with a declared prior. The method:

```yaml
calibration:
  method: posterior
  priors:                                     # every parameter the calibration may move, with its source
    reporting: {dist: loguniform, range: [0.0333, 0.333], source: "<citation or the question>", verified: unverified}
  targets:
    - {source: <observation source id, §3.4>, kind: incidence_series | survey_proportion | group_attack_rate
       | coverage_report | count_panel, ..., observation: negbin | binomial | betabinomial | poisson, links: {...}}
  per_unit: [<latent quantity>]               # optional: per-unit parameters (the posterior factorises by unit
                                              # only when no shared bias parameter links the units)
```

**Observation models** (new, `toolkit/analysis/observation.py`, harness): Poisson, negative binomial
(dispersion as a parameter with a prior), binomial and beta-binomial (for a declared design effect). Each
target names its **observation source**, and the source declares its sampling frame and measurement-bias
model (§3.4). A reporting or detection fraction is one such measurement-bias parameter, of a
case-notification source, never an engine parameter. So engines stay mechanistic, and every data-quality
assumption is visible in the spec.

**Calibrating latent immunity does not need the epidemic.** When the targets observe coverage or immunity
only (not cases), the likelihood needs the initialiser's output, not a run. The engine exposes an optional
`initial_state(params, unit)` beside `run_batch()`, checked against the sim's state at its first step, so
these fits cost microseconds per point even with many bias parameters.

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

**Two separate measures** (Codex review, finding 12). Marginal contraction alone is not identifiability.
Suppose the data fix only a product of two parameters, and narrow prior bounds then contract both
marginals. A ridge remains that contraction cannot see.

- **Likelihood identifiability**, from the joint likelihood, independent of the prior. It uses the Fisher
  eigen-decomposition and ridge detection in `identifiability.py`, and the profile likelihood over the prior
  support. Each parameter is classed `identified`, `on_ridge` (it loads on a flat direction) or
  `unidentified` (flat profile).
- **Prior dependence**, from the posterior: the **contraction** `1 − var_post/var_prior` on the prior's own
  scale (for example log for a log-uniform prior), and its posterior correlation with ridge directions.

**Classes** (thresholds in `thresholds.yaml`, proposals). A parameter is `prior_dominated` when it is
`on_ridge` or `unidentified`, **or** when its contraction is below 0.1. It is `identified` only when its
likelihood class is `identified` **and** its contraction is at least 0.5. Everything else is
`partly_identified`, which gets the same automatic test as `prior_dominated`.

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
- **Draw provenance, not just spread** (Codex review, finding 10). A spread threshold cannot certify
  coverage: draws at half the posterior sd would pass a 0.5 ratio and cover about 67% of the posterior mass.
  So the primary rule is structural:
  - every draw is a row of one canonical sample, the joint posterior sample or the joint MVN over all
    `estimated` names;
  - `uncertainty.yaml` records, per draw, the sample's id and row index, and the values **actually passed
    to evaluation**, including the per-unit `zs`. Today's `sampled` mapping omits these.
  - A contract check re-reads the canonical sample and confirms that the applied values equal it, row by
    row.
- **New contract check** `analysis_contract_uncovered_parameter` (HIGH, fail closed). For every name in
  `estimated`, in the `priors` section, or among the engine's fitted parameters with a range, one of these
  must hold:
  - (i) its applied values come from the canonical joint sample, as checked above;
  - (ii) it is excluded by name in the signed spec.

  A quantity held at its estimate while `calibration` is a declared source fails (i) and (ii). That is the
  D-001 case: 10 unit baselines with sd 3–9% each and draw spread 0. A second diagnostic stays as a
  missing-variation alarm, not as a coverage certificate: a realised spread below 0.5 × the sample's sd
  for any name means the draws are too few or broken.
- **The label is rendered from `varied` and the exclusions**, not from `sources` kinds
  (`uncertainty_scope()` changes accordingly). A label can no longer claim more coverage than the draws
  have.
- **Test-first fixture.** The v2blind1 calibration and uncertainty artifacts are not in the repository; only
  the critique YAML is. The test therefore rebuilds the case on the toy engine with likelihood calibration:
  draws that vary only `lrr:*` must fail, and full MVN draws must pass.

### 3.4 Observation sources: a sampling frame and a measurement model for each (decided 2026-11-01, owner)

The owner's answer: "admin coverage can be wrong and so can serosurvey, it depends on the sampling frame
and the bias of the measurement." So no source is primary truth, and none is reconciled to another by a
fitted correction factor (owner rule: model the mechanism, never a fitted fudge). The per-unit factor
`kappa` of the first draft is withdrawn.

**The model.** The quantity of interest is **latent**: the true immunity, coverage or incidence per unit
(and per cohort or group). Each data set is an **observation source** that sees the latent quantity
through two declared parts:
- **(a) a sampling frame**: who could have been counted or sampled, and how. The frame decides which
  population the observation describes, which may not be the unit's whole population.
- **(b) a measurement process**: the named bias mechanisms and the error between the frame's true value and
  the reported value.

Bias mechanisms are parameters with priors (published where available, with a source). They are named from
a closed vocabulary, each with a fixed functional form, so a bias is always a mechanism with a meaning and
never a free multiplier.

**Worked example: measles immunity by birth cohort.** The latent quantities are the true coverage `c_k` of
each dose, per unit and birth cohort, and from them (with seroconversion) the immune share (§1.1).

| Source | (a) Sampling frame | (b) Measurement process |
|---|---|---|
| Administrative coverage (doses reported / estimated target population) | every facility that reports, over the target population as projected | `numerator_inflation`: reported doses = true doses × (1 + ω), from over-reporting and doses given to children outside the target; `denominator_error`: target estimate = true target × (1 + δ), from census projections and migration. Observed coverage = `c_k · (1 + ω)/(1 + δ)`, so it can exceed 1. Reporting noise. Facilities that do not report are a frame gap |
| Coverage survey, card or recall (household cluster survey) | households in the sampled clusters of the survey frame; the frame excludes some populations (for example insecure areas, nomadic or institutional populations), declared with their share of the unit | `recall_bias` in children without cards, and the card-retention difference; sampling error with the survey's design effect (beta-binomial) |
| Serosurvey (cluster sample, assay) | as for the household survey, or the facility or school frame it used | `assay_sensitivity` and `assay_specificity`: observed positivity = Se · p + (1 − Sp)(1 − p); `antibody_waning`: protected people who have fallen below the assay cut-off, by years since vaccination; `maternal_antibody`: infants positive without vaccine immunity (an age band below a declared bound, modelled or excluded); design effect |

- **Frame gaps are modelled, not ignored.** A survey's latent value is the immunity of the frame
  population. The unit's immunity mixes the frame population and the out-of-frame share `φ` (from the
  frame's documentation or the census), with a prior on the out-of-frame relative immunity.
- **What is identifiable.** A bias parameter is identified only where sources **overlap**: the same unit,
  cohort and period seen through different frames and bias mechanisms. Administrative data alone identify
  only `c_k (1 + ω)/(1 + δ)`, so ω and δ trade off exactly, and both trade off against `c_k`. A serosurvey in
  the same cohorts pins `c_k` up to the assay and waning terms, and with them the ratio
  `(1 + ω)/(1 + δ)`, but still not ω and δ separately. The pre-model arithmetic
  (`pre-model-identifiability-arithmetic`) states this before calibration, and the measures of §3.2 confirm
  it after.
- **What the harness reports.** Every bias parameter is classed with §3.2's classes (`identified`,
  `partly_identified`, `prior_dominated`, with `on_ridge` where it trades off). The report's "what the data
  do and do not determine" section lists them per source. Prior-dominated bias parameters keep their full
  prior range in the draws, get the automatic `prior_range` decision test, and are named in the interval
  label ("its width is set mainly by the declared denominator error of the administrative coverage,
  −10% to +25%"). So the decision carries the measurement uncertainty, not just the sampling error.

**How the spec declares a source.**

```yaml
observations:
  sources:
    - id: admin_mcv1
      measures: dose_coverage             # closed: dose_coverage | seropositivity | case_notifications | ...
      latent: {quantity: coverage, dose: 1, by: [unit, birth_year]}
      frame:
        design: administrative            # administrative | cluster_survey | facility_based | sentinel
        population: "target population as projected by <source>"
        excluded: []                      # each {who, share | share_column, relative_value_prior}
      bias:
        - {mechanism: numerator_inflation, param: omega_admin, prior: {...}, source: "<citation>", verified: unverified}
        - {mechanism: denominator_error, param: delta_admin, prior: {...}, source: "<citation>", verified: unverified}
      noise: {model: normal_logit, sd_prior: {...}}
    - id: sero_2024
      measures: seropositivity
      latent: {quantity: immunity, by: [unit, birth_year]}
      frame: {design: cluster_survey, design_effect: 2.1, excluded: [{who: "...", share: 0.06, relative_value_prior: {...}}]}
      bias:
        - {mechanism: assay_sensitivity, param: se_igg, prior: {...}, source: "<assay validation>"}
        - {mechanism: assay_specificity, param: sp_igg, prior: {...}, source: "<assay validation>"}
        - {mechanism: antibody_waning, param: wane_rate, prior: {...}, source: "<citation>"}
        - {mechanism: maternal_antibody, handling: exclude_below_age, bound_months: 9}
```

- `bias: none` is allowed only with a stated reason, and the label then says "assumed unbiased: <source>
  (<reason>)".
- Bias parameters are shared across units by default. A per-unit bias needs its own declared prior; it is
  then one more latent quantity, not a fit to the residual.
- The generic vocabulary covers the other families: case notifications have a reporting fraction
  (`under_ascertainment`) and a reporting delay; a screening panel has a detection fraction. Measles is the
  worked example, not a special case.

**Structural checks** (schema and contract, fail closed, version-2 specs):
- `observation_source_undeclared` (HIGH): a calibration target, or a unit attribute that comes from data,
  that names no declared source, or a source without a `frame` or a `bias` entry.
- `bias_without_mechanism` (HIGH): a bias parameter whose mechanism is not in the closed vocabulary, or
  that has no prior and source. A free per-unit factor is refused by this check.
- `frame_gap_unmodelled` (MEDIUM): a source whose frame excludes part of a population that the decision
  covers, with no out-of-frame share and prior.
- `single_source_treated_as_truth` (HIGH): a latent quantity observed by one source whose bias parameters
  are all fixed or `none` without a reason.

**Critic check.** `critique-domain` reads each declared frame and bias list against the source's own
documentation (survey report, assay validation, the denominator's source) and raises a blocker for a
missing mechanism that the documentation names. This is a judgement, recorded as a critique blocker, and
it does not replace the structural checks.

## 4. Engine card and `engine_needs` vocabulary

Today `card_fits()` matches names only (components, outputs, windows, attributes). A configured engine
(the package-rate engine takes its components from the spec) therefore fits almost any spec. The proposal
is a closed vocabulary of **capabilities**, defined in a new `toolkit/engines/capabilities.py` (a typo
fails closed). **`contract.py` is unchanged** (decided 2026-11-01, owner): `EngineCard` gains no field, and
no engine's code fingerprint changes, so the malaria pack needs no re-stamp. Capabilities are read from
each engine's `card.yaml`, which is not fingerprinted; the empirical and compartmental engines gain one.
An engine without capabilities in its card has none, so it matches only version-1 specs, as today.

**Capabilities come from the resolved configuration** (Codex review, finding 14). The card lists a base set
and a `by_config` map from configuration fields to the values they add (for example `mode: windowed` adds
`incidence_series` and `growth_rate`). `capabilities.resolve(engine)` reads the engine's configuration and
returns the set for that configuration, so a compartmental engine configured `to_extinction` does not
advertise `incidence_series`. The empirical engine advertises `produced` only with `structural_zero` set. A
configuration field the map does not know fails closed. A self-test checks that every configuration field
the map names exists on the engine, so the card cannot drift from the code unnoticed.

Some spec contents do not determine the dynamics: an allocation with a count panel could suit an endemic
engine or an empirical one. For those, the spec carries a typed `engine_needs.dynamics`, signed with the
rest. The planner's skill says when it is required, and the schema refuses an allocation spec without it
once two engine families could fit.

| Key | Values | Derived from the spec by `engine_needs()` |
|---|---|---|
| `dynamics` | `none`, `closed_epidemic_final_size`, `closed_epidemic_renewal`, `agent_based_endemic` (malaria) | the deliverable's metrics and the calibration target kinds |
| `outcome_role` | per outcome: `prevented`, `produced` | `objective.sense` |
| `intervention_action` | `reduce_rate`, `remove_susceptible_all_or_nothing`, `reduce_susceptibility_leaky`, `detect` | `options.components.<c>.action` (new optional field) |
| `structure` | `single_group`, `age_groups_contact_matrix`, `birth_cohorts` | `decision_units.populations` (several groups with a matrix, or cohorts) |
| `calibration_data` | `count_panel`, `survey_proportion`, `group_attack_rate`, `incidence_series`, `coverage_report` | `calibration.targets[].kind` |
| `estimands` | `r0`, `reff_start`, `reff_end`, `attack_rate`, `growth_rate`, `p_major` | `deliverable.outputs[].metric` |
| `decision_coupling` | `independent_units`, `joint_within_unit` | `options.quantities` present → `joint_within_unit` |

- **Matching.** `card_fits()` adds a rule: every needed capability value must be in the card's list.
- **Backward compatibility** (Codex review, finding 13). The new obligations apply only to specs that opt in,
  marked by `schema_version: 2`. A version-1 spec:
  - derives no capability needs (so it matches cards with `capabilities: {}` exactly as today);
  - does not require the `priors` or `observation` kinds to be listed or excluded;
  - keeps kind-level exclusions valid;
  - keeps any explicit `engine_needs` as given, with no superset rule.

  The D-001 check (§3.3) does apply to version-1 specs, because the label it protects already exists. A
  version-1 spec whose draws hold a calibrated quantity at its estimate gets the finding. For a kind-level
  exclusion of `calibration`, the label already says calibration is not covered, so it passes. SP-a7 runs
  every existing self-test spec and fixture unchanged.
- **Malaria card.** Its `card.yaml` gains `dynamics: agent_based_endemic`, `outcome_role: {cases: prevented, ...}` and
  `intervention_action: [reduce_rate]`. So it is no longer eligible for a final-size or renewal spec, and the
  compartmental engine is not eligible for an endemic vector-borne allocation.
- **Explicit `engine_needs`.** When a version-2 spec gives `engine_needs` explicitly (allowed today), the
  schema checks that it is a superset of the derived needs. A planner can add needs but cannot drop one the spec implies.

## 5. Benchmark packs

The compartmental engine has **no engine-level fitted parameters**: every parameter is calibrated per run
or declared per question. Its pack is therefore pure validation. Every benchmark has role `held_out`, and
`fit` is empty. The runner gains three estimand types, `value` (one output against a target), `identity`
(two computations of one quantity) and `coverage` (simulation-based calibration). The coverage type runs
in a separate pack file, because it exercises the harness calibration, not the engine alone.

**Configured engines in the runner** (Codex review, finding 15). Today `benchmark.load_engine()` calls the
class with no arguments, and its workers cache engines by reference alone. The empirical engine already
needs `components`, so it cannot be benchmarked as it stands. The changes:
- the pack schema gains `configs: {<name>: {...}}`;
- each setting names its config;
- the canonical config is part of the worker job, the cache key and the validation identity
  (`engine_identity` already hashes the configured structure);
- `results.yaml` lists the configurations validated.

A run may use a configuration only if it was validated, or if every field that changes the dynamics (the
run mode, the initialiser, `structure`, `reference`) matches a validated configuration. Sizes and names may
differ.

Every compartmental benchmark below runs the **Starsim engine** and compares it with an oracle written
inside the pack: a closed form, the final-size fixed point, or the pack's reference ODE. The oracles are
not engines and are never exposed to runs (§1.1). The oracle's final size includes the seed term, so the
comparison is exact up to the engine's stop tolerance.

**Compartmental engine, `to_extinction` mode**

| ID | Result | Source | Band |
|---|---|---|---|
| FS1 | single-group final size `z = 1 − exp(−R0 z)` at R0 = 1.5, 2, 4, 12, 18 | Kermack–McKendrick (1927); closed form via the Lambert W function | abs. 1e-6 |
| FS2 | R0 ≤ 1 gives no major outbreak: the attack rate stays below the seed-size bound `seed/(1 − R0)`; R0 = 1 + ε gives z ≈ 2ε | threshold theorem | abs. 1e-6 (seed 1e-9) |
| FS3 | multigroup with a rank-one (proportionate) matrix reduces to the scalar relation | Ma & Earn (2006), *Bull Math Biol*; Andreasen (2011) | rel. 1e-6 |
| FS4 | all-or-nothing vaccination of a share v: whole-population attack rate `z = s·x`, with `s = 1 − v·e` and `x = 1 − exp(−R0 s x)` (x is the attack rate among those left susceptible); both denominators checked (R0 = 3, v·e = 0.5 gives x = 0.5828, z = 0.2914); zero at `v·e ≥ 1 − 1/R0` (Codex review, finding 3) | herd-immunity identity | rel. 1e-8 |
| FS5 | `R0` from the next-generation matrix equals the growth-rate-consistent R0 of the reference ODE | Diekmann, Heesterbeek & Metz (1990) | rel. 1e-6 |
| FS6 | multigroup final size equals the reference ODE run to extinction, for 3 contact matrices (identity, rank-one, an asymmetric 5×5) | the pack's ODE (`solve_ivp`, rtol 1e-10) | abs. 1e-5 |
| FS8 | the final size is the same at `dt` of one day and one week, and the step cap is never reached in FS1–FS6 | exponential-survival identity (§1.1) | rel. 1e-6 |
| FS7 (held out) | published age-specific final sizes from a contact-matrix model | candidates: Mossong et al. (2008) POLYMOD-based results; Wallinga, Teunis & Kretzschmar (2006, *AJE*) age-specific attack-rate fit. Values are transcribed with a quoted span through the evidence service before the pack is written; until then the benchmark is `not_assessable` | the paper's reported precision |

**Compartmental engine, `windowed` mode**

| ID | Result | Source | Band |
|---|---|---|---|
| RN1 | in the growth phase, `R = 1 / Σ_s w_s e^{−r s}` for the observed growth rate r | Wallinga & Lipsitch (2007, *Proc R Soc B*) | rel. 1e-3 |
| RN2 | unforced single-group cumulative attack rate equals the FS1 solution at any step length (the exponential-survival form), and the linear form is shown to fail (the negative control: 0.8687 against 0.7968 at `w_1 = 1`, R0 = 2) | Diekmann, Heesterbeek & Britton (2013), ch. 1 | abs. 1e-6 (seed size only) |
| RN3 | `reff_end = R0 · f(t_end) · S_end/N`; `reff_end < 1` after an unforced major epidemic; `attack_rate` counts only infections inside the window when the window starts with `S < N` | identity | rel. 1e-9 |
| BR1 | multitype extinction: for one group with Poisson offspring, `1 − p` solves `1 − p = exp(−R0 p)`; with geometric offspring, `p = 1 − 1/R0`; for a block-diagonal matrix, an introduction into a subcritical block gives `p_major = 0` | branching-process identities | abs. 1e-8 |
| RN4 | a weekly series simulated by the reference ODE (aggregated to weeks), refitted: R0 recovered | the pack's ODE | rel. 5% |
| RN5 (held out) | published reproduction-number estimates from incidence series | Cori et al. (2013, *AJE*) worked examples (pandemic influenza 1918, SARS 2003, Hagelloch measles 1861); values transcribed as for FS7 | the paper's 95% CrI |

**Cohort initialiser**

| ID | Result | Band |
|---|---|---|
| CI1 | susceptibility after doses with given coverages and seroconversion, `independent` and `nested` dependence, against hand-computed values | abs. 1e-12 |
| CI2 | a cohort's susceptibility is invariant to the survey-year/decision-year shift (birth-year indexing) | exact |
| CI3 | a mixed-cohort survey band's expected positivity, with Se = Sp = 1 and no waning, equals the population-weighted mean of its cohorts | abs. 1e-12 |
| CI4 | `initial_state()` equals the Starsim sim's state at its first step, for both initialisers | abs. 1e-12 |
| OB1 | each bias mechanism's forward map against hand-computed values (for example admin coverage `c(1 + ω)/(1 + δ)`, positivity `Se·p + (1 − Sp)(1 − p)`, a frame gap mixing `φ`) | abs. 1e-12 |

**Empirical engine, yield mode**

| ID | Result | Band |
|---|---|---|
| EY1 | the reference parameterisation gives the same fitted cell rates as an unconstrained Poisson GLM (statsmodels as the reference) | rel. 1e-6 |
| EY2 | a `structural_zero` package returns 0 with no parameter; `maximise` against it equals the plan's total | exact |
| EY3 | a panel without the reference package is refused (`calibration_ridge`), not drawn | exact |

**Coverage pack (`coverage.yaml`)**

The pack uses simulation-based calibration of the harness calibration plus uncertainty chain. Its
synthetic generator is the reference ODE, an independent Poisson generator, or (for SB3) an independent
generator of latent coverage and of each source's frame and biases, never the engine or the observation
code under test. Each case runs 200 replicates.

**What identifies a reporting fraction in a renewal fit.** Whether forcing is present does not decide it
(Codex review, finding 11). Reporting is identified when the observed series shows substantial depletion
and the initial susceptible share is known: the turnover then pins down the attack rate, and cases divided
by infections gives the reporting fraction. Reporting is confounded in two situations:
- the series ends before appreciable depletion, so reporting trades off against the seeding;
- the initial susceptible share is unknown, so reporting trades off against both transmission and the
  susceptible share, even with complete observations.

A turnover that can be explained by something other than depletion (an unknown seasonal forcing, or
behaviour change) also weakens it. The SB cases are therefore built from these identifying assumptions,
not from whether forcing is present. The classification is checked against the profile likelihood of each
synthetic case, computed from its known generator, not asserted.

| ID | Case | Pass |
|---|---|---|
| SB1 | identified: known initial susceptibility, unforced, a complete epidemic with substantial depletion | 95% interval coverage of R0, the attack rate and `reff_end` in [0.90, 0.99] (binomial SE ≈ 1.5%); `reporting` classed `identified` in ≥ 90% of replicates |
| SB2a | confounded: the series is truncated before depletion | coverage of the attack rate in [0.90, 0.99] **with** the prior range carried; `reporting` classed `prior_dominated` or `on_ridge` in ≥ 90% of replicates; coverage **below 0.80** when `reporting` is fixed at its prior median (the negative control that reproduces the v1 failure) |
| SB2b | confounded: unknown initial susceptible share (declared prior) plus unknown forcing amplitude, complete series | as SB2a, and the ridge reported names reporting with the susceptible share |
| SB2c | identified despite known forcing: known susceptibility and depletion | as SB1 (guards against classing every forced fit as non-identified) |
| SB3a | cohort immunity from two overlapping sources (administrative coverage with numerator inflation and denominator error, and a serosurvey with assay error, waning and a frame gap), biases drawn from their priors | coverage of the unit susceptible share in [0.90, 0.99]; the coverage-to-immunity ratio identified, ω and δ separately classed `prior_dominated` or `on_ridge` in ≥ 90% of replicates |
| SB3b | negative controls for SB3a: (i) administrative coverage taken as the truth; (ii) the serosurvey taken as the truth | each below 0.80 coverage of the susceptible share (the "one source is primary" error the owner's rule excludes) |
| SB3c | administrative coverage only | every bias parameter classed `prior_dominated`; coverage in [0.90, 0.99] with the prior ranges carried |
| SB4 | yield-mode panel (unit rates and package ratios) | coverage of the plan total in [0.90, 0.99]; the D-001 negative control (unit rates held at the MLE) is below the band |
| SB5 | group-attack-rate calibration of `q` | coverage of hospitalisations-averted for a fixed plan in [0.90, 0.99] |

**Held-out benchmarks and gating** (decided 2026-11-01, owner: use the engine before they are transcribed,
labelled). The held-out published benchmarks (FS7, RN5) are reported as `not_assessable` and do not block
until their values are transcribed. Until then, every report from the engine carries a harness-rendered
label: "validated against analytic identities and simulation-based coverage; published benchmarks FS7 and
RN5 not yet assessed". The analytic identities (FS1–8, BR1, RN1–4, CI1–4, OB1, EY1–3) and the coverage
pack must pass before any run uses the engine, and `card.yaml` records `results.yaml`'s sha256 as the
malaria card does. Every pack result carries the engine's code fingerprint, so a code change invalidates
it.

## 6. Build plan (five sub-commits, each test-first)

| Commit | Content | Tests written first (each must fail on 881e90c) |
|---|---|---|
| α spec | `sense` (`maximise`, `minimise` alias); `headline.quantity`; `constraints.resources`; `options.quantities` with caps and the joint-package limit; `deliverable.kind: estimates` with the threshold rule; `priors` and `observation` kinds; per-parameter exclusions; `schema_version: 2` gating of the new obligations; `observations.sources` with frames, the closed bias-mechanism vocabulary and the four structural checks (§3.4); `capabilities` vocabulary in `capabilities.py`, read from `card.yaml` with `by_config` resolution (`contract.py` unchanged); `engine_needs` derivation and the superset check; the sense/outcome-role check | SP-a1 `maximise` validates and `avert` specs are byte-identical after load; SP-a2 maximising a `prevented` outcome is refused; SP-a3 quantity expansion yields C(16,5) level sets for 11 steps over 5 groups, distinct level components per group, and drops capped ones; SP-a4 an over-limit joint space is refused; SP-a5 an `estimates` spec without options validates; SP-a6 a needs-superset violation is refused; SP-a7 every existing self-test spec and fixture validates unchanged and derives no capability needs; SP-a8 a `to_extinction`-configured card does not match an `incidence_series` need; SP-a9 a target with no declared source, a bias with no mechanism or prior, and a free per-unit factor are each refused; SP-a10 `contract.py` and the malaria engine's code fingerprint are byte-identical to 881e90c |
| β analysis | one `benefit()` function used by evaluate, summary, uncertainty and interaction tests; `summary()` totals; posterior folds and `|optimum − comparator|` normalisation in stability; resource caps into toolkit/alloc; the `estimates` path (not-applicable artifacts); observation models and the per-source measurement model (frames, bias mechanisms, frame gaps); `posterior` calibration, using `run_batch()` and `initial_state()` when an engine has them; likelihood identifiability plus contraction classes; ridge refusal in `likelihood`; draw provenance (canonical sample id and row, applied values including `zs`) and the label from it; `analysis_contract_uncovered_parameter`; `prior_range` and `structure` decision tests | AN-b1 the toy engine with a produced outcome: the maximise plan equals the brute-force optimum; AN-b2 the D-001 rebuild fails the new check and full draws pass; AN-b3 a ridge panel stops with `calibration_ridge` (today: jittered MVN); AN-b4 a prior-dominated toy parameter gets an automatic `prior_range` test and is named in the label; AN-b5 a threshold decision on toy draws gives yes/no/undetermined at the right probabilities; AN-b6 a maximise headline interval has the same sign as its point value (today's `uncertainty.run()` would flip it); AN-b7 a posterior-calibrated allocation runs its stability folds instead of reporting "nothing calibrated"; AN-b8 a product-only-identified pair under narrow priors is classed `on_ridge`, not `identified`; AN-b9 draws at half the posterior sd fail the provenance check; AN-b10 on a toy latent quantity seen by two sources, a bias parameter seen by one source only is classed `prior_dominated`, gets a `prior_range` test and is named in the label |
| γ empirical yield | `reference`, `structural_zero`, `structure: main_effects \| cell`; card capabilities; pack EY1–3 and SB4 | EY1–3 as in §5; E5 the old constructor's outputs are unchanged |
| δ compartmental engine (Starsim) | first, a runtime spike on the dev VM: one `ss.Module` with the exponential-survival step, timed per sim and per batch of 1,024 (the go/no-go for `direct` evaluation, §1.1). Then the `GroupEpidemic` module, start-of-sim interventions, `ss.Result` outputs, groups and contact matrix, initialisers (`given`, `cohort_history`), `to_extinction` and `windowed` modes, `run_batch()` and `initial_state()`, multitype extinction for `p_major`, `card.yaml`; the pack's oracles (closed forms, final-size fixed point, reference ODE); runner estimand types `value`, `identity` and `coverage`, and pack `configs` carried through workers, cache keys and identity; packs FS, BR, RN, CI, OB and SB1–3, SB5 | contract test (`check_engine`), plus outputs identical across seeds and `run_batch()` equal to `run()`; FS1–8, BR1, RN1–4, CI1–4, OB1 written as failing tests before the module (RN2 includes the linear-form negative control); SB1–SB3 and SB5 run on the dev VM |
| ε wiring and retro | register the engine in `ENGINES`; capabilities in the malaria `card.yaml`; the planner's `decision-spec` skill gains the new fields (vocabulary only, no thresholds); CHANGELOG row and `improvement-plan.md` update; local dry run of each family's spec on the harness with **no model calls** (hand-written specs from the public inputs, kept out of the repository) to confirm each validates, chooses the right engine and produces every artifact | T-e1 engine choice selects exactly one eligible engine for each of the four family specs and the malaria spec; T-e2 every script's `--self-test` passes |

**Cost (estimates, to be confirmed before spending).**
- **VM.** The analytic checks run in seconds. The coverage pack is 10 cases × 200 replicates × one
  calibration. With batching, a renewal fit is a few seconds and an immunity fit (initialiser only) well
  under one, so about 1–2 hours on the 8-vCPU dev VM with set-up and the δ runtime spike, under US$5, to be
  measured. Nothing heavy runs on the laptop.
- **No malaria re-stamp** (Codex review, finding 16; decided 2026-11-01, owner). `code_fingerprint()`
  hashes `toolkit/engines/contract.py` for every engine, so changing it would invalidate the malaria
  `results.yaml`. `contract.py` stays byte-identical, capabilities live in `capabilities.py` and the cards'
  `card.yaml`, and the optional `run_batch()` and `initial_state()` methods are on the new engine only.
  SP-a10 checks that the malaria fingerprint is unchanged. No VM time is needed for the malaria pack.
- **Model calls.** One v2 run per question, four runs, after ε merges. This note does not have a measured
  per-run spend for v2. It should be read from the first blind run's `run_state.json` before asking. As
  a planning range: US$15–40 per run, US$60–160 for four. This is above the US$10 approval line, so the
  runs wait for an explicit owner go-ahead with the measured figure.

**Out of scope.**
- A continuous-time run mode exposed to runs (timing, peaks, vaccination during the season).
- Any analytic solver exposed to runs; they stay as the pack's oracles.
- Leaky protection.
- Births, deaths, waning and migration.
- Stochastic (chain-binomial) or agent-based versions of the Starsim engine.
- Coupling between decision units (spatial transmission; LASER later).
- Hierarchical pooling across units beyond `main_effects`/`cell`.
- An optimiser for joint package spaces above the limit.
- Cost-effectiveness of a campaign when no costs are given.
- The evidence service (priors, including the measurement-bias priors, stay `unverified` with a source
  until V5).
- Deleting gate checks the new contract check supersedes (listed in the PR, not deleted).

## 7. Risks and open questions for the owner

**Risks**
1. **Prior-driven answers.** Where the data do not identify a parameter, the interval reflects the
   declared prior. That is honest, but a wrong prior range gives a confidently wrong interval. Mitigations:
   - the label and the "what the data do not determine" section name the parameter and its range;
   - the `prior_range` test shows whether the decision depends on it;
   - the priors are in the signed spec.
2. **Structure choice is still a choice.** Forced or unforced renewal, and which bias mechanisms a source
   declares, can change the answer. The `structure` test reports it, but the primary structure is signed, not
   learned.
3. **Final-size outcomes ignore timing.** Final size is wrong for decisions where the epidemic is
   interrupted (by a season's end or a mid-epidemic intervention). The card states this range, and a
   continuous-time run mode of the same Starsim module is the planned extension.
4. **The joint-package limit.** The limit makes larger dose-allocation problems fail closed rather than be
   solved approximately.
5. **Coverage pack noise.** 200 replicates give about ±1.5% on the coverage estimate. A borderline engine
   could pass or fail by chance. The pack therefore fixes its seeds and reports the binomial interval.
6. **Measurement-bias priors are thin.** Published values for over-reporting, denominator error,
   out-of-frame immunity and antibody waning are scattered and context-specific. Where only one source
   observes a quantity, the answer's width is set by these priors. Mitigations: they are named in the
   label, tested by `prior_range`, stay `unverified` with a source until the evidence service (V5), and
   `critique-domain` checks them against the source documents.
7. **Starsim runtime.** The batching estimate (§1.1) is not yet measured. The δ spike measures it first;
   above 5× the estimate the emulator path is used and the note returns to the owner before ε.

**Open questions.** Questions 4, 5, 6 and 10 were answered by the owner on 2026-11-01; the others stand as
proposed.

1. **Sense vocabulary.** Is `maximise` plus `minimise` (as an alias of `avert`) right, or should `minimise`
   report levels only, with no "averted"?
2. **Joint allocation as one decision unit with enumerated packages.** Is the limit of 50,000 packages
   acceptable, or is a structured optimiser (for example marginal-gain with a bound) wanted now?
3. **Calibration method.** Adopt `posterior` (declared priors, Sobol importance sampling or adaptive
   Metropolis) beside `likelihood`, and refuse ridged likelihood fits instead of jittering them?
4. **Survey reconciliation.** *Decided 2026-11-01 (owner).* Neither source is primary: administrative
   coverage and serosurveys can both be wrong, depending on the sampling frame and the measurement bias.
   Each source gets a declared frame and bias model, biases are parameters with priors, and the harness
   reports which are identified (§3.4). The per-unit factor `kappa` is withdrawn.
5. **Engine framework.** *Decided 2026-11-01 (owner).* Starsim, our own Python and Starsim code, never R,
   malariasimulation, EMOD or another external simulator. Analytic solvers remain as test oracles in the
   pack, not as the engine (§1.1).
6. **Gating on published benchmarks.** *Decided 2026-11-01 (owner).* Yes: the engine may be used while
   FS7 and RN5 are `not_assessable`, with a label on every report (§5).
7. **Thresholds.** Do the proposed values suit you: contraction classes 0.1/0.5, ESS floor 1,000, R-hat
   1.01, the coverage band [0.90, 0.99], the realised-spread ratio 0.5, condition number 1e8, the
   to-extinction stop tolerance `1e-9 · N`, and the 5× runtime trigger for the emulator fallback? All of
   them go in `thresholds.yaml`.
8. **Spend.** Approval for four v2 runs after ε, at the spend measured from the first blind run.
9. **Exact resource use.** Is `use: exactly` (spend every block) needed now? It needs lower bounds through pruning, the ILP, brute force and `check()`; without it, `at_most` is used and unused blocks are reported.
10. **Malaria re-stamp.** *Decided 2026-11-01 (owner).* Keep `contract.py` unchanged and read capabilities
    from the engine cards (option b); no re-stamp (§4, §6).

## Addendum: Codex review round 1 (read-only, on e108b50)

Codex raised 16 findings (12 P1, 4 P2). Each is addressed above, at the section it names. The two numerical
claims were re-computed before editing:
- the linear renewal form ends at 0.8687 against 0.7968;
- all-or-nothing vaccination gives 0.5828 among those left susceptible and 0.2914 of the whole population.

| # | Finding | Change |
|---|---|---|
| 1 | a matrix of ones is not homogeneous mixing across unequal cohorts | `C_ij = c·N_j/N`; `q` defined per contact over the infectious period (§1.1) |
| 2 | the linear renewal update breaks the final-size identity and can over-deplete | exponential-survival form; RN2 tightened, with the linear form as a negative control |
| 3 | FS4 confused the denominators | `z = s·x`; both denominators benchmarked |
| 4 | `1 − 1/Reff` is not general | multitype extinction equations with a declared offspring distribution and introduction group; BR1 |
| 5 | the attack rate included prior immunity; `reff_end` ignored forcing | attack rate within the window; `reff_end` with `f(t_end)`; `reff_ref` separate |
| 6 | sign applied in three places | one `benefit()`; a grep self-test; interaction admissibility per outcome role |
| 7 | stability skipped posterior calibration and mis-normalised value lost | posterior folds; `not_applicable` for one joint unit; `\|optimum − comparator\|` |
| 8 | `use: exactly` breaks dominance pruning | deferred: `at_most` in α, `exactly` is open question 9 |
| 9 | levels cannot be component values | one component per level sharing a group, each with generated cost, resource weight and engine map |
| 10 | a spread ratio does not certify coverage | draw provenance from one canonical joint sample, applied values recorded; spread kept as an alarm only |
| 11 | forcing does not decide reporting identifiability | identifying assumptions stated; SB2a/b/c built from them |
| 12 | marginal contraction misses ridges | likelihood identifiability (ridges, profiles) and prior dependence both required |
| 13 | new obligations broke version-1 specs | `schema_version: 2` opt-in; version-1 semantics unchanged except the D-001 check |
| 14 | family capabilities do not certify a configuration | matching on the configured card; typed `engine_needs.dynamics` where the spec is ambiguous |
| 15 | the runner cannot build configured engines | pack `configs` through workers, cache keys and identity |
| 16 | changing `contract.py` invalidates malaria results | byte-identical contract, capabilities from `card.yaml` (owner decision, open question 10) |
