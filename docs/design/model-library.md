# The model library: layers, the promotion rule and parallel tracks

Status: design note, docs only. The owner approved the direction on 2026-11-05; this note
writes it down so that each piece can be built and reviewed as its own PR into `v2`. It
complements `architecture-v2` (PR #82), `v2-spec-and-analysis.md` (the decision spec and
the analysis engine), `v2d-compartmental-engines.md` (the compartmental engine and the
empirical yield mode) and the determinism roadmap in `improvement-plan.md`.

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

Four layers, from the most general to the most specific. A layer may use the layers above it
in this list, never the ones below it.

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
| Calibration | `toolkit/analysis/calibration.py` (`emulator_inversion`, `likelihood`, `posterior`); engine-level `fit_joint` in `toolkit/engines/benchmark.py` | per-unit inversion counts clamped units but does not stop on them; joint calibration across all targets is §5.3 |
| Emulator | `toolkit/analysis/model.py` | merged |
| Allocation and uncertainty | `toolkit/alloc/`, `toolkit/analysis/uncertainty.py`, `stability.py`, `decision_tests.py` | merged |
| Canary | none | §5.1 |
| Data contracts | `toolkit/spec/geometry.py` (unit coordinates inside their own polygon), `scripts/data_schema.py` | partial; per-source contracts are §5.2 |
| Launch | `cloud/run_cluster.py`, `cloud/vm_job.py`, `scripts/compute.py` | more than one path in use; one golden path is §5.5 |
| Figure tiers | `toolkit/analysis/figures.py`, `docs/design/v2-figures.md` | merged |
| Postmortems | none | §5.6 |

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
| Compartmental, age-structured | `toolkit/engines/compartmental/` (v0.1) | `flu_vaccine`, `measles_campaign` | card and pack (analytic identities, simulation-based coverage); published benchmark FS7 not yet assessed; no ENGINE_NOTES or hooks yet; age-structured extension started 2026-11-05 |
| Renewal (generation interval) | started 2026-11-05 | `dengue_outbreak` | the compartmental engine's `windowed` mode already uses a renewal step; the family gets its own card and pack, with an observation model for an unknown reporting fraction |
| Tuberculosis natural history | later | `tb_acf` (the empirical yield mode answers it today) | not started |
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
  (`lessons.yaml`: id, statement, tag, disease area, run, evidence, the change made).
- When a second disease area shows the same lesson, a core PR generalises it. The PR cites
  both postmortem entries and passes the acceptance criteria in §4.
- The eval suite counts: a synthetic measles or dengue question is a different disease area
  from the Nigeria malaria run. This is what makes Track B the fast route to promotion.

Two cases for the owner to confirm (open items in §7):
- **Infrastructure and integrity defects** (a launcher that drops jobs, a gate that can be
  bypassed, data leaving its storage) are not disease knowledge. This note proposes that they
  are fixed in core at the first occurrence, tagged `core`, with the two-area test still
  required before merge.
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
one.

### 3.2 Track B: the eval suite

Five synthetic questions (`evals/suite.yaml`), each with a seeded generator, a hidden answer
key and a deterministic grader (`scripts/eval_suite.py grade`). Specs are signed by the lead
(`--signoff lead`); the owner reviews the rubrics once (improvement plan F2), not each run.

Engine order, one family at a time:

| Order | Engine family | Questions | What it adds to the library |
|---|---|---|---|
| 1 | Compartmental, age-structured | `flu_vaccine`, `measles_campaign` | a contact matrix by age band; birth-cohort immunity from coverage history and a serosurvey; dose blocks coupled within a unit |
| 2 | Renewal | `dengue_outbreak` | the case with **no allocation units**: estimates only (a reproduction number and an attack rate), and a reporting fraction that the data do not determine, carried through to the interval |
| 3 | Tuberculosis | `tb_acf` | screening yield and natural history; today answered by the empirical yield mode |

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
| AC3 | The canary self-tests include a question with no allocation units | `dengue_outbreak`'s canary is in the set every core PR runs | no | added when the renewal family lands; until then a toy estimate-only spec |
| AC4 | Thresholds live in manifests or priors, not in code | review, plus the AC1 scan flagging numeric literals in check functions | manifests exist (`rigor_artifacts.yaml`, `effort_floors.yaml`, `toolkit/analysis/thresholds.yaml`, `scope_declaration_policy.yaml`) | the literal check in the AC1 scan |

A core PR description states the result of each criterion, with the canary run ids.

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
- Acceptance: every canary passes nightly for a week on `v2`; a planted defect in core
  (for example a dropped batch row) fails the canary of every question.

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
- **Acceptance:** no unit is clamped at the edge of its range. A cheap reachability check
  (can the engine reach each unit's target anywhere in the prior box?) runs before the batch;
  a unit that cannot is a finding at that point, not after the batch.
- When a target is missed, the question is which joint region fits, and the first check is
  whether the mechanism follows the standard published model (determinism roadmap R2).

### 5.4 Incremental rebuilds

Every step's output is cached under a key made of the hashes of its code, its inputs, the
spec section it reads and the engine identity. A change recomputes only the steps and units
whose key changed. Today `compute.py` reuses results by function and code fingerprint, but the
fingerprint covers all of `scripts/` and `toolkit/`, so any code change recomputes the whole
batch.

- Acceptance: a change to one unit's input recomputes that unit's rows and the steps
  downstream, and nothing else; a canary run twice on the same tree does no engine work the
  second time.

### 5.5 One golden launch path

One launcher path for every run, canary and eval: submit, running, stored, self-stop. It is
tested nightly with a trivial real job on a real cluster (about one credit), not only with a
fake cluster. Orchestration runs on a cloud VM and writes results to storage, so a laptop that
sleeps or loses its network only loses its view of the run.

- Acceptance: the nightly real-cluster test passes for a week; a new launcher change merges
  only after it has passed one real run; other launch paths are retired.

### 5.6 Postmortems and delivery metrics

Each run, canary failure and eval run ends with a postmortem written from its records:
`postmortem.md` and `lessons.yaml` (each lesson tagged as in §2). Three delivery metrics are
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
| Confirm the proposed reading of the promotion rule for infrastructure and integrity defects (§2) | owner | at review of this PR |
| Confirm that a synthetic eval question counts as a disease area for the two-area test (§2) | owner | at review of this PR |
| Age-structured compartmental engine (started) | engine agent | PR into `v2` by 2026-11-08 |
| Renewal engine (started) | engine agent | PR into `v2` by 2026-11-08 |
| Malaria v0.11 (in progress) | engine agent | PR into `v2` by 2026-11-07 |
| §5.1 canary pipeline, first PR (Nigeria canary and `malaria_alloc`) | lead | 2026-11-09 |
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
