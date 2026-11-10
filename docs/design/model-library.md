# The model library: layers, the promotion rule and parallel tracks

Status: design note, docs only. The owner approved the direction on 2026-11-05; this note
writes it down so that each piece can be built and reviewed as its own PR into `v2`. It
complements `architecture-v2` (PR #82), `v2-spec-and-analysis.md` (the decision spec and
the analysis engine), `v2d-compartmental-engines.md` (the compartmental engine, and the
empirical yield mode it proposes) and the determinism roadmap in `improvement-plan.md`.

## Why

v2 made the model an engine validated once and reused by every run, and made the analysis
harness code driven by a signed spec. What it does not yet say is where a lesson from a run
should go. Between 2026-11-03 and 2026-11-05 the Nigeria malaria run taught several lessons
(§6). Some are about the harness in general, some about the malaria engine and some about
one question's inputs. Without a rule, a malaria-specific fix can land in generic code, and a
generic weakness can be patched in one question's spec.

The library has one job: put each piece of knowledge in the layer where it belongs, and
test each layer on more than one disease area before calling it general.

## 1. Layers and what belongs in each

Four layers, from the most general to the most specific.

**Dependency rule.** Code depends only on layers above it. Core code defines the interfaces
(the `Engine` contract, the spec schema, manifest loaders) and never imports a concrete
engine or names a domain pack. At run time, core receives an engine, priors, contracts and
thresholds as inputs, named by the signed spec and loaded through those interfaces. So the
calibration step runs any engine it is handed, and reads its thresholds from a manifest,
without knowing which disease either belongs to.

| Layer | Holds | Must not hold |
|---|---|---|
| Core | the machinery every question uses | a disease, country, intervention or unit name; a threshold written in code |
| Engine families | validated models behind the `Engine` contract | a country, a question's units or a decision |
| Domain packs | what a disease area knows: priors, skills, data contracts, thresholds | code that runs an analysis |
| Questions and runs | one decision: its spec and inputs | anything reused by another question |

### 1.1 Core (disease-agnostic)

| Piece | Where it lives today | State on 2026-11-05 |
|---|---|---|
| Stage machine and fix loop | `agents/stage_machine.py`, `agents/fix_loop.py`, `agents/findings_ledger.py` | merged (v2c, #90) |
| Decision spec and sign-off | `toolkit/spec/schema.py`, `signoff.py`, `engine_parameters.py`, `priors.py` | merged; held-out values cannot be signed |
| Calibration | `toolkit/analysis/calibration.py` (`emulator_inversion`, `likelihood`, `posterior`); engine-level `fit_joint` in `toolkit/engines/benchmark.py` | per-unit inversion: clamped units now stop the run at the calibration step; joint calibration across all targets: `toolkit/calibration/history_matching.py` behind `calibration.joint` (§5.3) |
| Emulator | `toolkit/analysis/model.py` | merged |
| Allocation and uncertainty | `toolkit/alloc/`, `toolkit/analysis/uncertainty.py`, `stability.py`, `decision_tests.py` | merged |
| Canary | none | §5.1 |
| Data contracts | `toolkit/spec/geometry.py` (unit coordinates inside their own polygon), `scripts/data_schema.py` | partial; per-source contracts are §5.2 |
| Launch | `cloud/run_cluster.py`, `cloud/vm_job.py`, `scripts/compute.py` | more than one path in use; one golden path is §5.5 |
| Figure tiers | `toolkit/analysis/figures.py`, `docs/design/v2-figures.md` | merged |
| Postmortems | `scripts/run_autopsy.py` (`autopsy.md`/`.json`, primary failing layer) and `scripts/lessons.py` with `docs/lessons/` (lessons ledger and recurrence checks), both called at run finish by `agents/run_finish.py` | merged (Phase 29 and the lessons ledger); layer tags for the promotion rule and delivery metrics are §5.6 |

Core code is checked against the acceptance criteria in §4 on every change.

### 1.2 Engine families

An engine family is one modelling approach, implemented once on Starsim (our own code) and
validated once. Each engine in a family ships four things:

1. **A card** (`card.yaml`): mechanisms, components, unit attributes, outputs, capabilities
   by configuration, and every parameter with its kind, units, range and source. A range is
   the literature value with its spread across studies, years and places, not a single
   picked value. The malaria engine's `priors.yaml` is the model to follow.
2. **A benchmark pack** (`benchmarks/pack.yaml`): fit targets and held-out benchmarks, each
   with its source and band, run by `toolkit/engines/benchmark.py`. Held-out benchmarks are
   never fitted (the sign-off refuses it).
3. **ENGINE_NOTES.md**: the benchmark results by version, rendered from the result files,
   and the standard published model each mechanism follows, or why it departs from it
   (determinism roadmap item R2).
4. **The hooks contract** (`toolkit/engines/hooks.py`, `figures-hooks/1`): the delivery
   schedule and monthly series the figures and the dynamics review read.

| Family | Engine | Questions | State on 2026-11-05 |
|---|---|---|---|
| Malaria agent-based | `toolkit/engines/malaria/` (v0.10) | Nigeria flagship, `malaria_alloc` | card, priors, pack, ENGINE_NOTES, hooks; v0.11 in progress |
| Compartmental, age-structured | `toolkit/engines/compartmental/` (v0.1) | `flu_vaccine`, `measles_campaign` | card and pack (analytic identities, simulation-based coverage). The age-structured pieces already exist: age bands with a contact matrix, birth-cohort initialisation from coverage history, dose blocks and campaigns (`engine.py`, card `capabilities`). Remaining: published benchmark FS7, ENGINE_NOTES, the hooks contract, and graded end-to-end v2 runs of both questions, with any spec or optimiser support those runs show missing (v2d §2). This work started 2026-11-05 |
| Renewal (generation interval) | started 2026-11-05 | `dengue_outbreak` | the compartmental engine's `windowed` mode already uses a renewal step; the family gets its own card and pack, with an observation model for an unknown reporting fraction |
| Tuberculosis natural history | later | `tb_acf` | not started. Interim route: the empirical yield mode of v2d §1.2 (`reference` and `structural_zero` packages, a yield objective), which is designed but not built: the engine has neither field and the spec accepts only `objective.sense: avert` |
| Empirical package-rate | `toolkit/engines/empirical/` | panels with a reference package | merged; a helper engine, not a family to grow |

A family is added when a question needs it and no existing card covers it
(`v2d-compartmental-engines.md` §4, `engine_needs`).

### 1.3 Domain packs

A domain pack is what a disease area brings to the library. It holds no analysis code.

- **Skills:** the disease skill (for example `.claude/skills/malaria-modeling/`), attached by
  trigger words (`select_skills()`).
- **Priors:** entries in `.claude/orchestration/expert_priors.yaml`, each with `applies_when`
  so it reaches only matching questions; engine-card priors such as
  `toolkit/engines/malaria/priors.yaml`.
- **Data contracts for common sources:** survey data (DHS), population rasters (WorldPop),
  boundaries and settlements (GRID3, geoBoundaries), modelled surfaces (MAP), programme
  records. A contract names the schema, units, plausible ranges, join keys, geometry rules
  and provenance, and runs at pre-sign (§5.2). A contract for a source used by many disease
  areas (population, boundaries) is core once two areas use it (§2).
- **Threshold manifests:** bands and cut-offs specific to the area, in YAML, read by core
  code that names none of them.

Today these pieces exist in four places. A pack index (`domains/<area>/pack.yaml`, listing
the skill, the prior ids, the contracts and the manifests) lets the term scan in §4 and the
canary in §5.1 find them. Files do not move.

### 1.4 Questions and runs

A question is a signed decision spec plus its inputs. A run is one execution of it, with its
record in `runs/` (gitignored) or on cloud storage.

- Licensed or restricted data (survey microdata, partner data) stays local or in the project
  storage account, never in the repository. The repository holds the code that derives the
  inputs and the hashes of what it read.
- Synthetic eval inputs ship with the code (`data/evals/`); their answer keys are the
  encrypted hidden half (`evals/README.md`).
- A question may name units, places and interventions freely. Nothing in a question is
  reused by another question without first moving to a domain pack or an engine.

## 2. The promotion rule

**Every run ends with a postmortem** (§5.6) that lists its lessons. Each lesson gets one tag:

| Tag | Meaning | Where the fix goes |
|---|---|---|
| `core` | the harness would make the same mistake on any disease | core code, with a test on two disease areas |
| `engine` | a mechanism, parameter or benchmark of one engine family | the engine card, pack and ENGINE_NOTES; a new engine version |
| `domain` | knowledge of the disease area | the domain pack: a prior, a skill section, a contract, a threshold |
| `question` | this decision's inputs or spec | the next spec amendment, or the fix queue for the next run |

**A lesson is promoted to core only once the same lesson has appeared in two different
disease areas.** Until then it stays in its domain pack (or engine notes), even when it looks
general. "The same lesson" means the same failure in the postmortem's own words, tested by
the same kind of check, not just a similar topic. The postmortem record links the two runs.

Why: a check written from one disease tends to encode that disease. Workstream D of the
improvement plan shows the pattern: one malaria miss suggested generic rules, and D8 asked
for a second disease area before the rules could be called generic.

How it works in practice:
- The first occurrence goes into the domain pack, with a pointer in the postmortem
  and an entry in the lessons ledger (`docs/lessons/ledger.yaml`, extended with the tag and
  the disease area: id, statement, tag, disease area, run, evidence, the change made).
- When a second disease area shows the same lesson, a core PR generalises it. The PR cites
  both postmortem entries and passes the acceptance criteria in §4.
Owner decisions (2026-11-05, at review of this note):
- **Infrastructure and integrity defects** (a launcher that drops jobs, a gate that can be
  bypassed, data leaving its storage) are fixed in core the first time they appear, tagged
  `core`. The fix must still pass on two disease areas (AC2) before it merges.
- **A synthetic eval question counts as a disease area for the acceptance tests** (AC2),
  because it exercises a different engine and question type. **For promoting a domain lesson
  into core, it counts only when the lesson is about method or structure** (for example a
  calibration that clamps, or an interval that leaves out a source), **not about real-world
  data** (a source's quirks, a programme's records). A lesson about real-world data needs a
  second real-data disease area before it is promoted.

Also:
- **Core rules made before this note** stay as they are. Each one whose evidence comes from
  one disease area is listed in the first postmortem round and checked on an eval canary;
  none is removed by this note.

## 3. Parallel tracks

Three tracks run at the same time. Budget is not the limiting factor; the owner's review time
is. So only Track A asks the owner to sign.

### 3.1 Track A: the Nigeria malaria flagship

Real data, an owner-signed spec, the malaria engine. Its job is to produce the decision the
programme will use, and to be the hardest test of the library. After a signed launch, new
findings go into one fix queue for the next run; they do not trigger a rebuild of the current
one. The freeze never overrides failing closed: an integrity failure, an invalid input or a
finding that bears on the decision still blocks acceptance of the current run (the gate and
the owner sign-off of scope declarations are unchanged). What the freeze saves is the
computation, not the verdict.

### 3.2 Track B: the eval suite

Five synthetic questions (`evals/suite.yaml`), each with a seeded generator, a hidden answer
key and a deterministic grader (`scripts/eval_suite.py grade`). Specs are signed by the lead
(`--signoff lead`); the owner reviews the rubrics once (improvement plan F2), not each run.

Engine order, one family at a time:

| Order | Engine family | Questions | What it adds to the library |
|---|---|---|---|
| 1 | Compartmental, age-structured | `flu_vaccine`, `measles_campaign` | the engine's existing age bands, birth cohorts and dose blocks, taken through graded v2 runs; the published benchmark, ENGINE_NOTES and hooks |
| 2 | Renewal | `dengue_outbreak` | the case with **no allocation units**: estimates only (a reproduction number and an attack rate), and a reporting fraction that the data do not determine, carried through to the interval. Needs the estimate-only deliverable of v2d §2.3 first: today the spec requires `decision_units` and allocation sections, and `package_per_unit` is the only deliverable kind |
| 3 | Tuberculosis | `tb_acf` | screening yield and natural history; until then the empirical yield mode (v2d §1.2, not built) is the interim route |

`malaria_alloc` already runs on v2 (the blind v2 run graded 1.0 on 2026-10-31).

### 3.3 Nightly regression

- **What:** the canary (§5.1) of every question that has an engine, Track A and Track B.
- **Where:** on Coiled, launched by the golden launch path (§5.5).
- **When:** nightly, and on every pull request that changes core.
- **Rule:** a core change that breaks a question's canary does not merge. A canary breaking
  on an engine or domain change blocks that change only.
- **Who:** no owner sign-off. Eval and regression runs need no per-run approval; the running
  spend is reported with each nightly summary.

## 4. Acceptance criteria for every core change

| # | Criterion | How it is checked | Today | To build |
|---|---|---|---|---|
| AC1 | No disease, country, intervention or unit terms in the logic of `toolkit/` or `scripts/` | a scan of code (identifiers and string literals, not comments) against a vocabulary built from the domain-pack indexes and the eval questions; exemptions in a manifest, each with a reason; run in CI | `pipeline.py` DI13 scans one section; other modules rely on review | one repository-wide scan, as a self-test and a CI step |
| AC2 | Passes on at least two disease areas | the PR's canary results: the Nigeria malaria canary plus one eval canary on a different engine family | not required | the canary job reports per question; the PR template asks for both |
| AC3 | The canary self-tests include a question with no allocation units | `dengue_outbreak`'s canary is in the set every core PR runs | no; the spec cannot express such a question yet | the estimate-only deliverable (v2d §2.3), then the `dengue_outbreak` canary |
| AC4 | Thresholds live in manifests or priors, not in code | a threshold is a value a check compares a result with to decide pass or fail; such a comparison must read its value through a manifest loader. The scan lists module-level numeric constants and literals used in comparisons in check code; each is either loaded from a manifest or listed as a mathematical or numerical constant (a tolerance, a unit conversion) with a reason | manifests exist (`rigor_artifacts.yaml`, `effort_floors.yaml`, `toolkit/analysis/thresholds.yaml`, `scope_declaration_policy.yaml`); some thresholds are still module constants (for example `FLAT_SLOPE` in `toolkit/analysis/calibration.py`) | the scan, and moving each listed threshold into a manifest |

A core PR description states the result of each criterion, with the canary run ids.

**Bootstrap.** AC2 needs a second engine family with a canary, and AC3 needs the
estimate-only deliverable. Until each exists, a core PR states "AC2 (or AC3) not yet
available" and runs every canary there is; the criterion becomes blocking on the day its
first canary passes nightly. The first canary PR therefore ships the Nigeria and
`malaria_alloc` canaries, and the compartmental canaries follow with the age-structured
PR (§7).

## 5. The F6 factory package

"F6" here is the package of factory work to land before the next Nigeria run, F6 (the run
after F5). It is not item F6 of workstream F in the improvement plan. In priority order:

### 5.1 Canary pipeline

A canary is a question's signed spec cut down to minutes: a few units, a short horizon, a few
uncertainty draws, small batches. It goes through the same stage path as a full run, on the
same launcher, and writes the same artifacts. Its outputs are compared with stored reference
ranges for that canary (not with the full run's numbers), and every step's status is
recorded.

- One canary per question with an engine: Nigeria malaria (a subset of units drawn across
  the archetypes) and each eval question.
- It must exercise the real path at full width where size matters: one full-size batch chunk,
  the real analyze timeout and the real memory peak (lesson of 2026-11-02: no full-size batch
  had gone through `pipeline.py` before the first full run).
- Acceptance: every canary passes nightly for a week on `v2`. A set of planted defects, each
  declared with the execution paths it touches (for example a dropped batch row touches the
  emulator path; a wrong per-unit outcome touches the direct path too), fails the canary of
  every question whose path it touches and leaves the others passing.

### 5.2 Data contract tests at pre-sign

Each input source gets a contract (§1.3), and the pre-sign job runs every contract before the
spec reaches the owner: schema, units, ranges, join keys, unit coordinates inside their own
polygon, crosswalk coverage, and a check of each derived input against its cited source.
The dynamics review (events fire, trajectories stay in range) runs in the same job. A failed
contract blocks signing. Everything a reviewer would otherwise find after sign-off should be
found here, in one pass.

- Acceptance: replayed on the Nigeria inputs as first prepared, the contracts report the two
  late input findings in §6 (the missing past-intervention history and the campaign
  calendar) before the first sign-off.

### 5.3 Joint calibration

Calibration fits all uncertain parameters together, not one at a time.

- **Method:** history matching (implausibility over waves) or Bayesian calibration (Starsim's
  calibration or calabaria), chosen by dimension and cost, behind one harness step.
- **Priors:** literature values are reference points, not truth. They often come from another
  time or place, so each prior carries the spread across studies, years and settings, from
  the engine card or the domain pack.
- **Targets:** all of them at once: survey targets across units and years, and the engine's
  fit targets. Held-out benchmarks stay held out and are used only to validate.
- **Output:** the compatible region (all parameter sets consistent with the data), carried
  into the allocation uncertainty as draws. No single picked value is reported as the answer.
- **Observation models:** each target has its measurement model and a declared model
  discrepancy (v2d §3.4), so "consistent with the data" has a stated meaning.
- **Acceptance**, each failing closed, with thresholds in a manifest:
  - no unit is clamped at the edge of its range;
  - the compatible region is not empty: at least one parameter set meets every target
    jointly, not each target at its own value of a shared parameter;
  - the method's own diagnostics pass (implausibility over all targets for history matching;
    effective sample size and convergence for a sampler);
  - the fit is checked on the held-out benchmarks and reported beside them.
- **Reachability first.** A cheap check (can the engine reach each unit's target anywhere
  in the prior box, and all of them together?) runs before the batch; a unit that cannot is
  a finding at that point, not after the batch. Passing it is necessary, not sufficient.
- When a target is missed, the question is which joint region fits, and the first check is
  whether the mechanism follows the standard published model (determinism roadmap R2).

**Implementation (2026-11-06, in review).** No maintained Python package gave a compatible region for this problem
shape (shared parameters plus one parameter per unit across a few hundred units, an expensive stochastic engine):
mogp-emulator's HistoryMatching (last release 2022) fails on the current numpy; pyabc (maintained) needs thousands of
engine runs per fit and has no emulator; emukit offers designs and Bayesian optimisation, not implausibility;
calabaria and Starsim's calibration search for a point with Optuna. So the step is our own, built on scipy and
scikit-learn: `toolkit/calibration/history_matching.py` (disease-agnostic; thresholds in
`toolkit/calibration/thresholds.yaml`) and the spec adapter `toolkit/analysis/joint_calibration.py`.
- Shared parameters are priors the draws already vary (engine-card priors, spec priors, spec values with a range), so
  their sourced ranges are the priors and the outcome emulator spans them. The per-unit parameter is the existing
  `calibration.attribute` on `calibration.range`.
- Targets: unit (one per unit per survey year), group (a survey at a coarser level: a weighted aggregate of units),
  benchmark (fit targets of the engine's own pack only; a held-out id is refused). Each has a sampling sd (from its
  interval or sample size), a source-bias sd and a discrepancy sd; a target with none is refused.
- Profiling: the per-unit parameter moves only its unit, so a shared value is compatible with a unit when some value
  of the unit's parameter makes all its targets non-implausible. Units no value matches are listed with the direction
  of the miss and each target's modelled value; units matchable alone but not together are listed as conflicting.
  Either stops ANALYZE at the calibration step, before the outcome batch.
- After the last wave the engine runs at the best point for every unit (verification). The region's shared values
  feed the uncertainty draws (with provenance), and the automatic prior_range tests use the region's ends.

**Error model (F6 S2-3, part 1).** A survey's error is partly shared by the units of a group: one design and fieldwork,
and one modelled small-area surface. A unit target with `bias_by: <units column>` gets one latent offset per source and
group, b ~ N(0, bias_sd²): y = f + b + e, with e (sampling and discrepancy) independent per target. `bias_source` names
the survey (default: the target id), so two targets from one survey share one offset per group. At most one survey
per unit may carry an offset; a second is refused (declare it as a group target, an aggregate over the group's
units). The old rule added bias_sd² to each target's own variance. That lets every unit use its own offset, so a
group's units pull on one survey anchor as if they were independent, and the within-group contrasts that identify
shared parameters are lost.
- **Exact feasible offsets.** At a candidate and a grid value g of a unit's parameter, every target of the unit holds
  on one closed interval of its block's offset: [y − m(g) − 3 sd(g), y − m(g) + 3 sd(g)], inside ±3 bias_sd. The unit's
  feasible offsets are the union of those intervals over g, in closed form and exact on the grid, as the unit
  parameter is. A block's feasible set is the intersection of its units' unions, computed by a sweep over the interval
  ends.
- **Components.** Units, their blocks and every member of every group target are joined transitively, including
  members without an offset and group targets connected only through a shared member. A 2018 state aggregate over a
  state's units and the state's 2021 offset are one component, and an aggregate over several groups joins them. Each
  unit belongs to one component, so a row draws it once. Group-target checks include the members' emulator sds
  (perfectly correlated) and their run noise (independent), as the group-target scoring and the verification do.
- **Offset draws.** Offsets are drawn from their prior restricted to each block's exact set, by inverse cdf over its
  intervals. Each proposal also draws one unit assignment under its offset (each unit uniform over its matching
  values), and it is kept only when that one assignment meets every group target of the component. The group targets
  are judged and reported at the kept assignment with the least worst implausibility, never clipped.
  - Proposals start at `bias_draws` (64) per candidate and double while none is kept, up to `bias_draws_max` (1,024).
  - A component is infeasible at a candidate only when a block's set is empty or nothing is kept at the maximum. The
    report records the count reached (`bias_components`).
  - Each unit's matching grid values at a candidate are those that fit at one of the kept draws.
- **Region sample.** Each row draws a fresh offset from the restricted prior, then each unit's parameter uniformly over
  its matching values at that offset, redrawn together until the group targets hold. The values of a component with
  group targets are kept as drawn (no jitter), so the returned rows meet them. There is no fixed witness. A row that
  never holds, or an acceptance rate over the region below `bias_accept_min`, blocks the calibration
  (`offset_sampling_low_acceptance`).
- **Engine verification.** The verification point of a component with group targets is built from joint draws at the
  best candidate. It takes the draw whose worst unit misfit at its own offset is least, then puts each unit at its best
  value under that offset when every group target still holds there (else the draw's own values). That gives one
  offset and one assignment, feasible together. The engine verification then solves the common offset exactly from
  the engine's own runs, with the same sweep.
- **Not marginal implausibility.** Profiling is a feasibility rule. A single target may sit up to 3 (sd_e + sd_b)
  from the model, where integrating the offset out (marginal Gaussian implausibility) allows 3 √(sd_e² + sd_b²). It is
  stricter on contrasts between units of one group. Vernon et al. (2010, Bayesian Analysis 5:619) treat structured
  discrepancy through the covariance instead. A simulation of one group of n units at the truth (200,000 draws,
  sd_b / sd_e of 0.5, 1 and 3) compares the two rules:
  - The profiled rule keeps the truth in 99.9–100% of draws for n = 1 to 6, and 99.4% for n = 24.
  - The marginal rule keeps it in 94.0–99.7%.
  - For a contrast of 4 sd_e between two halves of a 6-unit group, the profiled rule keeps 30–62% of draws, against
    4–93% for the marginal rule. The marginal rule keeps most of them when the offset sd is large.
- **Best point.** By default the score sums the units, as before. A spec may choose
  `calibration.joint.best_point_weighting: per_group`: each group then counts once (its units' mean squared
  implausibility). This is a chosen balancing rule, so a large group does not
  outweigh small ones by repetition; it is not derived from a likelihood. The region does not depend on it; only the
  verification point does.

The change between surveys by group (a calibration diagnostic with a threshold) follows in S2-3b. Part 2 (historical
and future intervention parameters, and histories anchored to delivery surveys) needs an engine parameter for past
coverage. It follows separately.

### 5.4 Incremental rebuilds

Every step's output is cached under a key made of the hashes of its code, its inputs, the
spec section it reads and the engine identity. A change recomputes only the steps and units
whose key changed. Today `compute.py` reuses results by function and code fingerprint, but the
fingerprint covers all of `scripts/` and `toolkit/`, so any code change recomputes the whole
batch.

- **Provenance.** What is reused is the computed payload (engine results, fitted draws),
  never a whole artifact. Artifact envelopes bind the full signed spec hash, and the contract
  check refuses an artifact from another spec (`toolkit/analysis/contract.py`). So after a
  spec change, the harness writes fresh envelopes and manifest hashes for the current spec
  around reused payloads, each recording the cache key it reused.
- Acceptance: a change to one unit's input recomputes that unit's rows and the steps
  downstream, and nothing else, and the run passes the contract check; a canary run twice on
  the same tree does no engine work the second time.

### 5.5 One golden launch path

One launcher path for every run, canary and eval: submit, running, stored, self-stop. It is
tested nightly with a trivial real job on a real cluster (about one credit), not only with a
fake cluster. Orchestration runs on a cloud VM and writes results to storage, so a laptop that
sleeps or loses its network only loses its view of the run.

- Acceptance: the nightly real-cluster test passes for a week; a new launcher change merges
  only after it has passed one real run; other launch paths are retired.

### 5.6 Postmortems and delivery metrics

This extends what exists: `run_autopsy.py` already writes each run's autopsy with its
primary failing layer, and `lessons.py` keeps the lessons ledger in `docs/lessons/` and checks
whether a known error class came back. The additions are a layer tag on every lesson
(core, engine, domain or question, §2), the disease area, a link between two occurrences
for promotion, autopsies for canary failures and eval runs, and a check that every v2 run
path calls them. Three delivery metrics are
reported across runs, from the trace, the findings ledger and git:

| Metric | Definition |
|---|---|
| Lead time | from the moment a finding is recorded to the first run whose result includes its fix |
| Failure rate per stage | runs that fail at each stage (DATA, SIGN, BATCH, ANALYZE, WRITE) over runs that reach it |
| Time to recover | from a failed run to the next successful run of the same question |

- Acceptance: postmortems for F5 and each Track B run; the metrics table in the nightly
  summary.

## 6. Lessons from 2026-11-03 to 2026-11-05

Counts only; no Nigeria data values.

| Lesson | Counts | What it motivates |
|---|---|---|
| Serial late discoveries forced full re-preps | the spec was ready to sign on 11-03; 3 late findings (an intervention durability value, a past-intervention history input, a campaign calendar) each forced a full re-preparation; the spec was re-signed twice more (amendments 5 and 6) before launch | §5.2 contracts and dynamics review in one pre-sign pass; the freeze-and-queue rule in §3.1 |
| One-parameter-at-a-time fixes | one parameter changed twice in 2 days (first to a value that closed a held-out benchmark gap, later withdrawn; then to a literature value); after F5 a second parameter became the next single-parameter suspect | §5.3 joint calibration |
| Fake-only infrastructure tests let a launcher drop jobs | a launcher PR merged after 27 fake-cluster self-tests and 5 review rounds; its first real use hung for 39 minutes; a plain start lost 3 of 3 tasks | §5.5 golden path tested on a real cluster |
| The laptop's network drops | lost jobs and network drops added about 6 hours | §5.5 orchestration on a cloud VM |
| A calibration clamp found only after the batch | F5 ran about 1.75 h of batch before failing at ANALYZE; 57 of 309 units were clamped at the edge of their range, and 19 units in one stability fold needed extrapolation; about 190 credits and US$2.13 of model spend | §5.3 reachability check before the batch; §5.1 canary at full chunk size |

Proposed tags for F5's postmortem: the launcher and the laptop are `core` (infrastructure,
§2); the late input findings and the clamp are `domain` until a second disease area shows
them; the parameter that changed twice and the chemoprevention effect are `engine`
(malaria v0.11).

## 7. Open items and proposed next steps

| Item | Owner | Target date |
|---|---|---|
| Age-structured compartmental engine (started) | engine agent | PR into `v2` by 2026-11-08 |
| Renewal engine (started) | engine agent | PR into `v2` by 2026-11-08 |
| Malaria v0.11 (in progress) | engine agent | PR into `v2` by 2026-11-07 |
| §5.1 canary pipeline, first PR (Nigeria canary and `malaria_alloc`; AC2 and AC3 not yet blocking, §4) | lead | 2026-11-09 |
| Estimate-only deliverable (v2d §2.3), so the `dengue_outbreak` canary can exist | lead | with the renewal PR |
| §5.2 data contracts at pre-sign, replayed on the first Nigeria inputs | lead | 2026-11-11 |
| §5.3 joint calibration, design note first | lead | note by 2026-11-11 |
| §5.4, §5.5, §5.6 | lead | after §5.3, dated in its note |
| AC1 repository-wide term scan | lead | with the canary PR |
| Compartmental card: ENGINE_NOTES, hooks contract and published benchmark FS7 | engine agent | with the age-structured PR |
| Postmortem for F5, lessons tagged | lead | 2026-11-07 |

Tell us what is missing from the layers or the acceptance criteria, and we will use your
version.

## 8. Not in this note

The code for any item above; the contents of the Nigeria inputs; the eval rubrics and answer
keys (hidden half); moving existing files into domain directories.
