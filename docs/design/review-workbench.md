# Review workbench: specification

Status: agreed by the owner on 2026-11-18 (§11). The loop in §10 is running; the Fit view is first.

How this document is used. It is the only memory the work has. The owner and the lead agent write it
together. Every build or verification task is then given to a fresh agent that knows nothing but
this file and the files it names. If such an agent cannot do its task from this file, the file is
incomplete and is fixed first; the agent is not briefed by hand. §9 says how each part is verified,
and §10 is the loop that works through the goals.

## 1. Why a new tool

The run console (`tools/run_console/`) follows a run as a stream of events and collects sign-offs.
An audit of it found no fit diagnostics by group or by survey round, no scenario traces, no value
of information, no world model, no findings ledger, no measured spend, and no link from a run to
the pull requests whose code it ran.

Every serious problem in the Nigeria runs was visible in a run's own files before it cost a run.
The workbench builds the views that show them, and is measured by whether those views would have
caught the problems already known (§8). The console keeps the sign-off actions.

## 2. The reader and the five jobs

One reader: the owner, an expert epidemiological modeller.

1. **Verify a run in a modeller's order**: spec and evidence, model, fit, scenarios, outcomes.
2. **Inspect the deterministic steps**: stage history, and each harness-written file with the
   inputs it was computed from.
3. **Keep the science register**: every open science issue and uncertainty, with its source,
   whether it bears on the decision, and its disposition (§6).
4. **Understand the development work**: what the pull requests are, and which a run contained.
5. **Understand spend**: signed budgets, measured spend, cost and time per stage, and what each
   limit does when reached.

## 3. Rules for every view

- Local only: a standard-library server (plus PyYAML, which the harness already needs) on 127.0.0.1, loopback `Host` required, no CDN, no outbound
  call. Run files include licensed data, so nothing is published.
- Read-only over run files, with one exception built in G3: the owner's review notes, written to
  `<run>/owner/review_notes.jsonl` under the rules in §6. Sign-offs stay in the console.
- No engine and no harness loader is imported. Reads go through `lib.safe_io.read_nofollow`,
  confined to the run directory; credential file names are refused.
- Fail closed: a file that cannot be read or recognised shows "not available" with the reason.
- The code lives in `tools/workbench/`, outside `compute.code_fingerprint`. A workbench change
  never invalidates a stored engine batch and touches no file on the F6 critical path.
- A check is code. `tools/workbench/checks.py` has one function per check, returning: `status`,
  one plain sentence, the file and field read, an optional table, and the scorecard case.
- Check statuses: `ok`; `look` (the reader should judge; includes anything declared and signed);
  `problem` (a file contradicts another, a rule in code is broken, or work was lost);
  `not_available`. A run that ended `completed_scope_declared` is `look`, not `problem`.
- Declared items: something a rule in code flags is `look` when a signed declaration covers it
  and names a decision test that ran, and the sentence gives the declaration and the test's
  verdict. With no such declaration it is `problem`. A declaration is looked for in: the signed
  spec's declared lists, `scope_declaration.yaml`, and `findings.jsonl` rows with status
  `declared`. An item inside an allowance the signed spec sets (for example a maximum number of
  flagged units) is also `look`, and the sentence gives the allowance and says no test covers it.
- Only a verified signed spec can excuse anything: declarations and allowances count only from
  the signed copy, and only when its hash equals the one in the sign-off record. With no
  verified signed copy, a flagged item is `problem`.
- A missing file, or a recorded hash that is absent or malformed, is never a match.
- A producer's status word is mapped explicitly; an unknown word is `not_available` naming it.
  A record that contradicts itself (a status that disagrees with its own list of blocks) is
  `problem`.
- Something the harness could not compute is never reported as passed.
- A bound is "reached" when a value is within 1e-6 of it, relative to the prior range's width.
- A check reuses a threshold already defined in code, named with its file. It adds a new threshold
  only where §5 says so, and labels it "workbench threshold".
- "Near miss": a value within 25% of a threshold it passed is shown as `look` with both numbers
  (workbench threshold; owner to confirm in §11).
- For the reader: tables for rows; every number with its unit and source file; every figure says
  what is plotted, what each mark means and how to read it; pipeline terms defined where they
  appear; each element says whether it needs the owner.
- Charts are drawn in the page from the run's own tables so they can be filtered and hovered. The
  harness PNG is shown beside each.
- Where the signed spec and the spec on disk differ, every view reads the signed copy
  (`decision_brief.signed/decision_spec.yaml`) and says so.
- Pull requests under 800 lines of code, test first, one Codex review before merge. Fixtures,
  self-test cases and acceptance files are in separate pull requests from the code they test
  when the total would pass the limit.

## 3a. Agnostic to the problem

The workbench must serve any question the pipeline can run: any disease, any decision kind, any
engine, any calibration method. The malaria allocation run is one reference among several.

- **Views are data.** Each view is a Python module (`tools/workbench/views/<name>.py`) that
  returns `{checks, facts, tables, charts, notes}` (§9). The page has no view-specific code: it
  renders checks, facts and tables generically, and draws each chart from a declaration
  (`kind`, the table it reads, which columns are x, y, low, high and group, a title and a
  how-to-read sentence). Chart kinds: `interval_points` (observed with interval against model),
  `ranges` (prior against accepted range per parameter), `bars`, `lines`, `graph`.
- **A view model per view, an adapter per method.** A view defines one generic model. Adapters
  map what a method wrote into it, chosen by the artifact's `schema` and `data.method`. Checks run
  on the generic model only, so a check is written once. A method that does not record something
  leaves that table out, and the view says "this method (name) records no <thing>". An unknown
  schema or method gives `not_available` with its name; it is never guessed at.
- **Words come from the run.** Unit, group, parameter, package and outcome names, and the name of
  the grouping column, are read from the spec and the files. The code and the page use only the
  generic terms: unit, group, target, parameter, package, component, outcome, comparator.
- **No domain words in the code.** A self-test fails if any file under `tools/workbench/`
  (fixtures included) contains a disease, country, intervention or domain-metric word from
  `tools/workbench/domain_words.txt`. Exempt: that word list. Fixtures use neutral names (units U01…, groups G1…,
  parameters p1…, components A and B).
- **Thresholds come from the method's own file** (for example the calibration's recorded
  `thresholds` and cutoff), never from a constant tuned on one run.
- **Deliverable kinds.** Outcomes dispatches on the spec's `deliverable.kind`
  (`package_per_unit`, `estimates`); each kind has its own adapter into the Outcomes model.
- **Promotion rule.** A view is "built" only when its acceptance passes on at least two reference
  runs that differ in disease area and in method (§4), mirroring the model library's rule.
- **Scorecard cases are stated generically** in the checks (an effect applied beyond its
  evidence window; a change between rounds missed; a benchmark near its band's edge). The
  malaria incidents in §8 are the examples that motivated them.

The generic Fit model (what `views/fit.py` returns; an adapter fills what its method has):

| Part | Content |
|---|---|
| `facts` | `method`, `status` (ok, blocked, not_converged), `proceeded` (bool or null), `n_targets_group`, `n_targets_unit`, `n_parameters`, `region_accepted`, `region_candidates`, `region_share_of_prior`, `cutoff`, `change_limit`, `engine_replicates`, `units_at_bound` (either bound), `n_units`, `grouping` (the grouping column's name), `rhat_max`, `ess_min` (samplers). A fact the method does not record is null. |
| `tables.targets` | `id, level (group or unit), group, window, source, observed, low, high, sd_total, model, model_alt, score, score_alt, status` (`model` is the engine's value when the method verifies with the engine, else the fitted value; `model_alt` the emulator's; `score` is the method's own misfit measure, named in `notes.score`) |
| `tables.parameters` | `name, prior_low, prior_high, low, high, best, contraction, at_bound (none, lower, upper, both), prior_dominated` |
| `tables.changes` | `group, from, to, observed, modelled, z, flagged` |
| `tables.blocks` | `code, meaning, declared (bool), test, verdict` |
| `tables.offsets` | `key, mean, sd, accepted_share` |
| `tables.sensitivity` | `setting, status, blocks` (a list of codes) |
| `tables.series` | `series, t, observed, model, low, high`: a fit over time, when the method records a predictive band (the `estimates` deliverable's `posterior_predictive`) |
| `notes` | `method` (the mechanics in words with this run's numbers), `score` (what the score is) |

Checks on the generic model beyond those in §5.5, added from the first acceptance pass:
`near_cutoff` (targets whose score is within 25% of the cutoff they passed); `group_bias` (a
group whose mean signed score is beyond ±1: the model sits on one side of that group's targets;
workbench threshold); `convergence` (samplers: `status` not ok, or `rhat_max` above 1.01, or
`ess_min` below 400; workbench thresholds until the method records its own).
`emulator_vs_engine` counts unit targets as well as group targets and reports both.
`constrained` uses the largest contraction measure the method records for a parameter.
A check whose table the method does not record is `not_available`.

Rules settled while the fixtures were written:
- Check ids are `<view>.<name>` (`fit.status`). A table the method does not record is present in
  `tables` with the value null.
- A run with both `analysis/joint_calibration.yaml` and `analysis/calibration.yaml` is read
  through the joint file; the other is named in `notes`.
- Linking a declaration: `unmatched_decision.declared[]` covers a block when its `target` or
  `offset` appears in the block's `detail`, and covers a change pair when it names the pair's
  group. The test "ran" when `analysis/decision_tests.yaml` gives it `status: run`; the sentence
  quotes its verdict. A declared test that did not run leaves the item `problem`.
- `fit.region` is `look` when any accepted range or the best point reaches a prior bound, or when
  the accepted count is below the method's own minimum (`thresholds.region_min_candidates`);
  otherwise `ok`.
- `fit.status`: not converged is `problem`.
- The near-miss rule of §3 applies to every threshold a check compares against.
- `fit.group_bias`: a method whose score is unsigned gets the sign of model − observed; group and
  unit targets are pooled; a group needs at least two targets. The view adds a derived table
  `groups` (`group, n_targets, n_model_above, mean_signed_score`).
- `fit.convergence`: `problem` when status is not ok; `look` on the thresholds; the near miss for
  R-hat is on its excess over 1.
- `fit.emulator_vs_engine` on unit targets compares each unit's emulator score at the best point
  with its engine score; a unit target's `model_alt` is null where the method records none.
- `fit.constrained` uses the larger of the range contraction and the 95% interval contraction.
  On R4 that names five parameters, not one.
- A declaration counts as signed only when the signed spec copy holds it.
- The targets table keeps the generic columns; a method's own error components (for example
  observation, offset and discrepancy sd) are given in `notes`, not as columns.
- Still to build in Fit: residuals grouped by a column of the units table chosen in the page.
- A block's plain meaning comes from a table in the adapter, one sentence per code the method's
  code can emit; an unknown code is shown as itself. `offsets.accepted_share` is draws accepted
  ÷ draws proposed. `engine_replicates` is the smallest count recorded.

The `posterior` adapter (C = `analysis/calibration.yaml` `data`): `status` from
`C.diagnostics.converged`; targets from `C.diagnostics.fit[]` (`observed`, `expected` as
`model`, sd from `variance`, interval observed ∓ 1.96 sd, `score` = (expected − observed) / sd);
parameters from `C.priors[n].range`, the 2.5% and 97.5% quantiles of `C.sample.rows`, `best` from
`C.theta` or `C.map`, `C.identifiability.contraction`; `prior_dominated` from
`analysis/uncertainty.yaml`; `series` from `analysis/estimates.yaml`
`data.posterior_predictive` when present. Target `status` is the method's own word. `window` is
a `[start, end]` list. `approx` in an acceptance file means a relative tolerance of 1e-3.

Adapters for Fit, by what the reference runs hold: `history_matching`
(`analysis/joint_calibration.yaml`), `posterior` and `likelihood` and `emulator_inversion`
(`analysis/calibration.yaml` `data.method`). Slice β1 builds the model, the checks, the page
charts and the `history_matching` and `posterior` adapters; β2 the other two.

## 4. Reference runs

Stored under the owner's keep folder (`~/modeling-agentic-keep`, not in git). Acceptance tests in
§5 quote values from R4; a test fixture is a small synthetic run with the same file shapes.

| Name | Path under the keep folder | What it is |
|---|---|---|
| R4 | `vm_jobs/v2-nigeria-gf13-f6r4/resume2/out/runs/2026-11-01_231925_build-an-agent-based-model-of-malaria-tr` | F6 run 4: analysis complete, gate round 1, ended `run_failed` at a spec redraft |
| F5 | `nigeria_gf13_v2/full/run/F5/runs/2026-11-01_231925_build-an-agent-based-model-of-malaria-tr` | F5: ended `completed_scope_declared` |
| WM6 | `vm_jobs/wmt-wmstage/smoke6/out/runs/2026-11-16_044750_synthetic-test-question-world-model-stag` | small run with the world-model stages; package-rate engine, `likelihood` calibration |
| FLU | `vm_jobs/cage-flu4/flu-r8/out/runs/2026-11-06_005335_synthetic-test-question-evaluation-suite` | influenza vaccine allocation by age group; compartmental engine, `posterior` calibration |
| DEN | `evals/v2head/dengue_outbreak/R1/runs/2026-11-06_043438_synthetic-test-question-evaluation-suite` | dengue outbreak estimate (`estimates` deliverable, no allocation); renewal engine, `posterior` calibration |

## 5. Views

Each view lists what it reads, what it shows, its checks and its acceptance tests on R4. "Gap Hn"
points to §7: something no run file records today.

### 5.1 Overview

Shows: the question; how the run ended in one sentence; headline numbers; then every check from
every view, worst first, grouped by view, each linking to its evidence. Views not yet built are
listed with the goal that builds them.

### 5.2 Steps (built as a spike)

Reads: `run_state.json` (`history`, `journal`), `metadata.json`, `analysis/manifest.json`, the
header of each `analysis/*.yaml` (`producer`, `spec_sha256`, `inputs`, `written_at`),
`decision_spec.signoff.json`.

Shows: stage timeline with time and model spend per transition; task table; the chain of
harness-written files as a graph (file → the files it was computed from), each marked same,
changed or missing against its recorded hash.

Checks: `outcome`, `pipeline` (C9), `signed_spec` (C10), `artifact_spec` (C10), `inputs_fresh`
(C10), `manifest`, `progress` (C16), `cost` (C11).

Acceptance (R4): status `run_failed`, "the spec did not validate"; `decision_spec.yaml` on disk
(30bf9c2f) differs from the signed 28cf5953; 3 analysis files have a changed input; task
`SPEC#2:try3` failed; model spend $10.40 of $30.00; launch commit c05a631 "not in repository"
(gap H9).

### 5.3 Spec and evidence

Reads: signed spec (`question`, `comparator`, `decision_date`, `objective.horizon`,
`objective.delivery`, `options`, `constraints`, `analysis.outcome_adjustments`,
`engine_departures`, `uncertainty`); `evidence_dossier.yaml` `inputs`; the sign-off sheet;
`decision_spec.signoff.json` and `evidence_planner/superseded_signoff_*/`.

Shows: the decision in a table (decision, comparator, date, window, envelope, packages); each
effect input beside the window it is applied over; the dossier as a table (input, model value,
observations, chosen value or range, decision test); pre-sign results from the sheet; the
sign-off history with what changed in each by spec section.

Checks:
- `effect_window` (C1): an outcome adjustment applied over a window longer than its trial
  follow-up. Today the follow-up is prose only (gap H1), so the check shows the source text beside
  the window and is `look` for every adjustment until H1 lands.
- `dossier_coverage` (C15): entries with no data, entries decided by the owner, observations with
  no "opened" mark (gap H2).
- `signoff_note`: a signature with an empty note (what was accepted is not recorded).
- `presign_sections`: sheet sections the code writes that this sheet lacks (spec critique,
  candidate engines).

Acceptance (R4): OA-DUAL 0.54 and OA-PBO 0.73 against window 2024.0–2029.25; 58 dossier entries,
38 with observations, 20 with no data; 9 sign-offs, the current one by the owner at
2026-11-18T00:49:44Z with an empty note; the sheet's one HIGH: an owner-set parameter range that
lies outside the engine card's range.

### 5.4 Model

Reads: `engine_choice.json`; `analysis/engine.yaml` `data.card.benchmarks`, `data.parameters`,
`data.spec_values`; `analysis/benchmarks_at_spec_values.yaml`. Never the repo's engine card: it
moves on after a run (R4 ran v0.17; the tree is at v0.18).

Shows: the engine chosen and each candidate's reasons; mechanisms; benchmark table (id, metric,
fit or held out, trial value, band, model value, seed range, status, source) with a chart of
model value against band on a log scale; departures with their decision test and its result;
parameters with value, range, source, and owner-set values against the card.

Checks:
- `benchmarks` (C4): out of band (`toolkit/engines/benchmark.py` verdict); in band but within 25%
  of an edge on the log scale (near miss); a band the fitted range cannot reach.
- `departures`: each departure's decision test result, with a near miss flagged against
  `decision_tests.share_threshold` (0.10, `toolkit/analysis/thresholds.yaml`).
- `choice_text`: the choice's `why` contradicts the benchmark table.
- `spec_values`: owner-set values outside the card range.
- `choice_bound`: `engine_choice_sha256` is null in the analysis headers (gap H10).

Acceptance (R4): engine malaria-starsim 0.17, 4 ineligible candidates; 17 benchmarks; B8 held
out, out of band, signed departure, its decision test just under the share threshold (near
miss); B19 "band cannot be reached"; one owner-set value outside the card range.

### 5.5 Fit

The generic model is in §3a. What follows is the `history_matching` adapter, with R4 as its
example; the checks are stated on the generic model and apply to every method that fills the
table they read.

Reads: `analysis/joint_calibration.yaml` `data` (13 MB: parsed once with PyYAML's C loader and
kept in the server's memory, keyed by the file's size and modification time):
`status`, `blocks`, `unmatched_decision`, `region`, `targets`, `unit_targets`, `units`,
`verification`, `survey_changes`, `bias`, `adapter.windows`, `thresholds`, `sensitivity`;
`figures/calibration_*.csv`; `data/derived/units.csv` for grouping; `analysis/uncertainty.yaml`
`data.prior_dominated`.

Shows:
- Method box: implausibility I = |E[f] − y| / sqrt(V_em + V_obs + V_md), cutoff, design, waves,
  verification, with this run's numbers.
- Target table: id, source text, model window, value, sd_obs, sd_bias, sd_md, sd_total, emulator
  value, engine value, implausibility, status. Filter by state and by status.
- Observed against model, by state, with each target's interval; engine and emulator values both.
- Change between survey rounds by group: observed, modelled, z, against `survey_changes.z_max`.
- Offsets by survey and state.
- Parameters: prior range, accepted range, contraction, whether the range reaches a prior bound,
  and whether `uncertainty.yaml` calls the parameter prior-dominated.
- Residuals grouped by state, and by any column of the units table chosen in the page.

Checks:
- `status` (C13): blocked; each block code with its plain meaning; declared misses with their test.
- `region` (C13): accepted share; ranges reaching a prior bound; best point on a bound.
- `changes` (C2): flagged pairs, and near misses (|z| within 25% of `z_max`).
- `emulator_vs_engine`: targets whose implausibility differs between emulator and engine by more
  than 1 (workbench threshold).
- `unit_bounds`: share of unit intervals reaching the unit parameter's bound.
- `target_error` (C3): targets whose assumed sd is labelled an assumption; survey intervals are
  not in the file (gap H3).
- `constrained` (C14): a parameter that contracted by more than half yet is listed as
  prior-dominated.
- `replicates`: engine verification with one run per point.

Field map (JC = `analysis/joint_calibration.yaml` `data`):

| Shown | From |
|---|---|
| Target rows | the 13 group targets in `JC.targets` and the 309 unit targets in `JC.unit_targets[u][0]`; the table opens on group targets, with unit targets behind a filter |
| value, sd_obs, sd_bias, sd_md, sd_total, source | the same keys on each target |
| model window | the prefix of `observable` (`jc0`, `jc1`) looked up in `JC.adapter.windows` |
| emulator value, emulator implausibility (group) | `modelled_at_best`, `implausibility_at_best` on the target |
| engine value, engine implausibility | `JC.verification.targets[id]` (group) or `JC.verification.units[u].targets[]` (unit): `simulated`, `implausibility` |
| target interval drawn on charts | `obs_lo`, `obs_hi` in `JC.figure_data.fit[]`, labelled "from the assumed error, not the survey's interval" |
| residual | logit(engine value) − logit(value) |
| accepted, candidates | `JC.region.accepted`, `JC.region.candidates`; "share of prior volume" is `JC.region.share_of_prior` (not accepted ÷ candidates) |
| contraction, bounds | `JC.region.contraction`, `JC.region.ranges` against `JC.region.prior_ranges`; best point `JC.best.theta` |
| unit interval at bound | `JC.units[u].region_touches_bound` non-empty |
| change between rounds | `JC.survey_changes.pairs[]`: `group`, `observed_natural`, `modelled_natural`, `z`, `flagged`; limit `JC.survey_changes.z_max` |
| offsets | `JC.bias` |
| sensitivity | `JC.sensitivity[]`: its status and blocks, shown as one row per setting |

Check rules: `changes` trusts `flagged` and recomputes near misses from `z`. `unit_bounds` is
`look` above 25% of units and `ok` otherwise (workbench threshold). `target_error` is `look` when
any target's `source` text contains "ASSUMPTION" and always states that survey intervals are not
in the file. Expected status on R4: `status` look (both blocks declared, DT11 and DT12 no
change); `region` look; `changes` look (Yobe declared; three near misses); `emulator_vs_engine`
look (5 of 13 group targets); `unit_bounds` look; `target_error` look; `constrained` look
(`smc_half`); `replicates` look.

Acceptance (R4): status blocked, blocks `verification_failed` and
`offset_sampling_low_acceptance`, proceed true; accepted 122 of 3,072 candidates; one group
target fails engine verification; one change between rounds flagged and three shown as near
misses; nine parameters, each with its contraction. The values are in the acceptance file (§9).

### 5.6 Scenarios

Reads: `analysis/figure_data.yaml` `data.schedule`; `figures/timeline.csv`;
`analysis/trajectories.yaml` when present; `analysis/figures.yaml` `data.not_produced`.

Shows: schedule timeline by region and arm against the delivery period and the outcome window;
trajectories of plan against comparator when the run has them, with the dynamics checks the
harness already computes (`toolkit/analysis/figures.py`: plan equals comparator before delivery
starts; pre-intervention level inside the target interval).

Checks: `trajectories_present` (C5): `problem` when a run reached the outcome batch with no
trajectory run (gap H4); the harness dynamics checks, passed through.

Acceptance (R4): delivery 2024–2027, outcome window 2024–2029.25; 423 schedule events in 13
regions; trajectories, seasonality, seed noise and burn-in each "not produced" with the reason
from `figures.yaml`.

### 5.7 Outcomes

Reads: `analysis/point.yaml`, `uncertainty.yaml`, `decision_tests.yaml` with
`models/decision_tests/<id>.yaml`, `stability.yaml`, `value_of_information.yaml`,
`plan_variants.yaml`, `burden_validation.yaml`, `emulator.yaml`, `emulator_boundary.yaml`,
`allocation.csv`, the signed spec's `options.cost_items`.

Shows: plan mix and map; headline as point, median and interval, and as a share of the comparator
burden; cost against the allocatable budget and reserve; decision tests table (what was varied,
verdict, share, regret, dollars moved); stability across draws and across holdout folds; value of
information by input, marked prior-dominated or not and above its noise floor or not; plan
variants against the optimised plan; burden against external references; emulator accuracy; and a
comparison with another run of the same question chosen in the page (mix, headline, envelope,
comparator text).

Checks:
- `dearest_wins` (C8): the most expensive option of a kind chosen in at least 90% of units
  (`toolkit/analysis/red_flag.py` SHARE 0.9), computed in the workbench from the spec's costs
  (gap H6).
- `share_of_burden` (C7): averted above 50% of comparator burden (workbench threshold).
- `point_vs_draws`: the point value outside the middle half of the draws.
- `tests_that_move` : decision tests that change the plan, each linked to the register row it
  bears on; near misses.
- `burden_refs`: references outside their band.
- `voi_support` (C14): inputs above their noise floor; fewer draws than `informative_draws`.
- `boundary_coverage`: components with no unit in the emulator boundary check.
- `run_shift` (C6): against the comparison run, a package's unit count changing by more than 10%
  of units, or the headline by more than a factor of 1.5 (workbench thresholds). The check
  diffs the two signed specs itself, key by key to two levels (for example
  `analysis.outcome_adjustments`, `constraints.envelope_usd`), and lists every key that differs;
  it does not rely on the harness's comparison figure (gap H12). A shift with no spec key that
  differs is `problem`: something outside the spec changed. Acceptance (F6 run 3
  `vm_jobs/v2-nigeria-gf13-f6r3/resume4/...` against R4): the headline shifts by more than the
  factor; the diff names `analysis.outcome_adjustments` and `analysis.decision_tests`.

Acceptance (R4): two packages in the plan; the headline as point, median and interval, and its
share of the comparator; 42 decision tests: 32 no change, 9 change the plan, 1 not run; the
funded plan as a variant; value of information "indicative"; burden references 2 within, 3
outside; against F5, a six-package mix. The values are in the acceptance file (§9).

### 5.8 Science register

See §6 for the derivation table. The generic Register model (what `views/register.py` returns):
`facts`: `n_rows`, `n_open`, `n_declared`, `n_tested`, `n_implemented`, `n_withdrawn`,
`n_decides_yes`, `n_decides_unknown`, `n_owner_notes`. `tables.rows`: `id, kind, statement,
source, first_visible_stage, decides, disposition, tests (ids joined by a comma), verdicts,
severity, blocking, files`. `tables.by_source`: `source, rows, open`. Charts: `bars` of rows by
disposition and by source. Checks: `register.undisposed` (C12): any row whose disposition is
`open`; `problem` when such a row is blocking or of the highest severity, else `look`;
`register.tests_unlinked`: plan-changing decision tests linked to no issue (`look`);
`register.declared_untested`: a declared row whose test did not run (`problem`);
`register.decides_unknown`: open or declared rows with no test, so whether they bear on the
decision is unknown (`look`). Nothing in the model names a disease or a method: sources that a
run does not have are simply absent from `by_source`.
Acceptance (R4): D-001 and R-001 (trial ratios over the whole window) open; 13 rows from
`findings.jsonl` open; B8 declared, tested by DT9; SD-R3-BOUNDARY declared, tested by DT17,
although it is absent from `findings.jsonl`; the nine plan-changing tests each appear. Row totals
are re-derived in loop step 2 and written to the acceptance file.

### 5.9 World model

Reads: `world_model/belief_table.yaml`, `preregistration.json`, `score.json`, `outcome_map.yaml`.

Shows: beliefs before the run with weights; whether they were pre-registered and why not;
outcome and surprise per belief; "where to look"; each belief joined to register rows on the same
topic. Check: `preregistered`. Acceptance (WM6): 8 beliefs, 8 confirmed, "not pre-registered: no
remote". R4 and F5 have no world-model files and say so.

### 5.10 Development

Reads: `gh pr list` (cached to a local file with its fetch time; the one network read, made by
the owner's own `gh`), `git log --merges --first-parent`, `changes/*.md`.

Shows: pull requests grouped by title rule (17 groups, rules in `tools/workbench/pr_groups.yaml`),
each with its change-fragment summary when one exists; size; open pull requests with checks and
the review rounds found in the body; for a chosen run, the pull requests its launch commit
contains.

Checks: `launch_tree` (C9): launch commit not in the repository (gap H9); open pull requests over
800 lines or with failing checks.

Acceptance (as fetched 2026-11-18): 252 merged, 10 open, 8 closed; 84 over 800 added lines;
commit a6cc6c4 contains 234 pull requests, newest #270; 152 of 252 merged have a summary.

### 5.11 Spend

Reads: `~/.config/modeling-agentic/budget_<scope>.json` (record only, never the signature),
`reservations/ledger.json`; per-run `run_state.json`, `metadata.json` `model_spend_usd`;
`vm_jobs/*/state.json` durations; optionally the output of `cloud/budget.py status`, run by the
owner and saved.

Shows: each signed budget (amount, dates, note); model spend per run and stage; job durations;
measured cloud spend when a saved status exists, else "not recorded locally" (gap H8); a table of
every limit that can stop work, with file and effect, and the open pull request that softens it.

Acceptance: f6 $1,500 expiring 2026-12-02; wmt $100 expiring 2026-11-27; R4 model spend and duration from the run's files, cloud cost "not recorded locally"; limits table lists `budget_exhausted`
(`agents/stage_machine.py`) and the 200 core-hour cap (`scripts/compute.py`) as not covered by
#265 or #267.

### 5.12 Figures (the figure set the report and papers use)

The harness already writes, for any engine, a figure set in three tiers (`analysis/figures.yaml`
`data.tiers`: `data_model`, `simulation`, `decision`). Each figure has a title, a caption, what it
shows, its source files, the table it was drawn from (`figures/<name>.csv`), and a provenance
record. These are the figures a report or paper is built from, so the owner must be able to
verify each one, and they shape the other views.

Reads: `analysis/figures.yaml` (`figures`, `not_produced`, `tiers`, `overviews`);
`figures/*.png`, `*.csv`, `*.png.provenance.json`; `analysis/manifest.json`; `report.md`,
`manuscript.md`, `results.md`, `report_render.json` for where each figure is used.

Shows: every figure by tier, as the harness drew it, with its title, caption, source files, the
table behind it (sortable), whether its sources still have the hashes it was drawn from, and
where the report or manuscript uses it. Figures not produced are listed with the harness's
reason. Each figure links to the view that examines the same data.

Checks: `figure_fresh` (a figure whose recorded sources have changed since it was drawn);
`figure_missing` (a figure of the standard set not produced, with the reason; `problem` when the
report or manuscript relies on it); `figure_unused` (a decision-tier figure the report does not
use); `figure_in_manifest`.

How figures inform the other views:

- Each tier maps to a step of the verification path: `data_model` to Spec and evidence, Model
  and Fit; `simulation` to Scenarios; `decision` to Outcomes. A view shows the report figures of
  its part of the tier beside its own interactive charts, so the owner verifies the figure that
  will be published, not a private version of it.
- A view's charts are drawn from the figure's own table wherever one exists. The figure tables
  are written by the harness for every engine and method, so they are the most problem-agnostic
  data the workbench has. A view reads the method's artifact only for what no figure table holds.
- The standard figure set is the first draft of each view's content: a view must cover every
  figure in its tier, then add what review needs beyond it (checks, filters, declared items).
- The reverse also holds: a workbench chart that proves its worth in review and has no report
  figure (for example the change between survey rounds by group) is proposed as an addition to
  the standard figure set. That is a harness change and joins the queue in §7.

Acceptance (R4): 29 figures in the set (15 `data_model`, 5 `simulation`, 9 `decision`); 24
produced; not produced: trajectories, burn_in, seasonality, stochastic_noise, comparison, each
with its reason. (FLU: 6, 5 and 9.)

## 6. The science register (world-model extension)

Today the world model's beliefs cover decision outcomes only. Reviewer concerns live in
`findings.jsonl` and end with the run. Departures, calibration misses, excluded inputs, declared
limits and plan-changing tests each sit in a different file; several exist only as YAML comments.
Nothing links them to a belief or carries them to the next run. On R4, 13 of the derivable rows
have no disposition and nine plan-changing decision tests are linked to no issue.

Row: `id, kind (issue | uncertainty | assumption), statement, source, first_visible_stage,
decides (yes | no | unknown), links (findings, decision tests, beliefs, files), disposition (open
| implemented | tested by a decision test | declared with the owner's yes | withdrawn), history`.

Step 1, in the workbench (G3): rows derived on read, in this order. A later source that names a
decision test or finding id already on a row is merged into that row, not added.

| Source | Row id | Kind | Disposition on read |
|---|---|---|---|
| `findings.jsonl` rows | the finding `id` | issue | `status` open → open; declared → declared; fixed → implemented; owner_accepted → declared; owner_declined → open |
| `scope_declaration.yaml` `declarations[]` | `blocker_id`, else `id` | issue | declared, with its `decision_test` |
| `engine_choice.json` `engine_departures[]` | `departure:<component or benchmark>` | assumption | declared when `status` is signed, else open |
| signed spec `calibration.joint.unmatched.declared` | `calibration:<target>` | issue | tested by its decision test |
| signed spec `uncertainty.excluded` | `excluded:<input>` | assumption | declared |
| sign-off sheet rows marked HIGH | `presign:<input>` | issue | declared (accepted by signing) |
| `evidence_dossier.yaml` entries with `no_data` | `nodata:<input>` | uncertainty | tested when the entry names a decision test, else open |
| `decision_tests.yaml` tests with verdict CHANGES_PLAN, not already linked | `test:<test_id>` | uncertainty | open, with the sentence "changes the plan and is linked to no issue" |

Corrections from deriving the register by hand on R4, F5 and FLU (2026-11-18):

- `source` values: `findings`, `scope_declaration`, `engine_departures`, `calibration_declared`,
  `uncertainty_excluded`, `signoff_sheet`, `evidence_dossier`, `decision_tests`, `owner_note`.
- Row ids: a scope declaration's id is its `blocker_id`, else its `id`, exactly as written in
  the file. A declared calibration miss is `calibration:<target or offset>`. An excluded input is
  `excluded:<parameter or kind>`. A dossier row keeps the dossier's whole key.
- A test reference is reduced to the test id (a path's file stem).
- Merging: two rows merge when they share a decision test id or a finding id, or when one's id
  ends with the other's input name (`excluded:x` and `nodata:parameter:x`). The earlier source's
  disposition is kept and the later row's source and tests are added. Two critics' findings on
  the same subject are not merged mechanically: the owner merges them with a note
  (`{row, same_as}`), and the register shows how many open findings share a target stage and
  severity as a hint.
- Older runs: `declared_findings.yaml` (ids declared) and `final_gate.json` `resolved_by_test`
  (ids tested) also set dispositions; `findings.jsonl` is not the only record.
- Column `nature`: `science` for a critic's finding and for every non-finding source; `process`
  for a finding raised by a harness check about the run's files (its `source` is not a critic).
  Facts and the `undisposed` check count science rows; process rows are counted apart
  (`n_process`, `n_process_open`).
- Column `signed_by`: who signed the spec that carries a declared row. "Declared with the
  owner's yes" holds only when that is the owner; a lead-signed declaration is shown as such.
- `first_visible_stage` uses the stage machine's names (SIGN, ENGINE_CHOICE, ANALYZE, CRITIQUE,
  WRITE); a scope declaration's stage is the stage of the finding it declares, else GATE.
- A sheet row counts as HIGH only where the sheet marks a finding HIGH, not where the word
  appears in other text. A dossier entry "names a decision test" only through its structured key.
- `register.declared_untested` also fires (`look`) for a declared row with no test at all.
- `register.prose_only` (`look`): the count and the text of comment lines in the signed spec that
  start a departure, limitation or assumption. They are shown, not turned into rows: they
  become rows when gap H7 lands or when the owner adds them as notes. On R4 these include the
  outcome-adjustment assumptions, the burden departure and the parameters at their floors.
- What is specific to some runs: only `findings`, `uncertainty_excluded` and `decision_tests`
  exist on FLU. The model is unchanged; absent sources are absent from `by_source`.

Settled while the register's fixtures and tests were written (2026-11-18):

- The clean fixture's spec has no departure, limitation or assumption comment; those lines are a
  planted variant. A comment line counts when it holds a capitalised marker followed by a colon.
- A scope declaration that names no existing finding keeps its row and makes
  `register.undisposed` a `problem`: a declaration of nothing is a contradiction.
- Older-run sources override an open finding; a finding the owner declined stays open.
- A finding raised by a harness check is `science` when a science source merges into it.
- `by_source` and every register check count science rows only.
- A finding's `first_visible_stage` is the ledger's own stage field for it.
- A lead-signed declaration keeps its disposition; the `signed_by` column and a note say who.
- Tables added: `by_disposition` (`disposition, rows`) for the chart, and `notes` (`at, row,
  disposition, derived_disposition, statement, note`) for the owner's notes beside the derived
  values. A note that adds a row gets the id `note:<n>` in order of writing; in a `same_as` note
  the row named first survives. A malformed note line makes a check `register.notes` a
  `problem` and is shown, never dropped.
- A tested row whose test did not run is `problem`; with the decision tests file missing, the
  checks that need it are `not_available`.
- With no verified signed spec, rows that rest on the spec are shown open and
  `register.undisposed` is `problem`.

`decides`: yes when a linked decision test's verdict is CHANGES_PLAN, no when every linked test
is NO_CHANGE, else unknown. `first_visible_stage`: a finding's `target_stage`; for other sources
the stage that writes the source file (sign-off for the spec, sheet and dossier; engine choice;
analysis for decision tests).

Review notes: one JSON line per note, `{at, row (a row id, or null for a new row), kind,
statement, disposition, decides, note}`. Written only by a POST from the page carrying a token
the server makes at start and puts in the page; loopback only; appended with
`lib.safe_io.write_nofollow` under `<run>/owner/`, the folder the console already writes its
feedback to. A note never edits a harness file; the register shows the derived value and the
owner's note side by side.

Case C12 in files: a HIGH finding present in round n's critique or `findings.jsonl` whose
disposition is still open when the run ends. The planted defect is one such finding; the clean
twin has the same finding declared with a decision test.

Step 2, in the harness (G11, needs the owner's yes; after the F6 report):
`world_model/science_register.yaml`, written by harness code and carried to the next run. Each
open row with decides = yes or unknown becomes a pre-registered belief topic, so "where to look"
ranks science issues beside decision outcomes, and a run cannot finish with an undisposed row.

## 7. What no run file records today

These need a harness change. Each is outside the workbench and, where it touches the critical
path, waits for the F6 report. The workbench shows the gap in the view until then.

| Gap | Missing | Needed by |
|---|---|---|
| H1 | Estimand fields on outcome adjustments (endpoint, scale, age, follow-up months) | C1 |
| H2 | A checked status per dossier observation; follow-up years filled | C15 |
| H3 | On each calibration target: age range, diagnostic, survey round, the survey's own interval, the model window, and a flag on assumed error terms | C3 |
| H4 | The trajectory step run by the run itself. The code exists (`toolkit/analysis/trajectories.py`, from #127 and #128) but no stage calls it: it is a manual command after a run, and no stored run has `analysis/trajectories.yaml`. Three figures (trajectories, seasonality, seed noise) and the dynamics checks depend on it. The dynamics review the owner approved on 2026-11-02 is "not started" in the improvement plan (L2) | C5 |
| H11 | A series over the burn-in: the engine hook's series starts at `horizon.start`, so the burn-in figure can never be drawn | Scenarios |
| H5 | A comparison between runs of one question, written by the harness. The figure code exists (`python -m toolkit.analysis.figures compare`), but nothing calls it and a run does not know its predecessor | C6 |
| H6 | The red-flag result and each package's cost rank, written to a file | C8 |
| H7 | Departures, limitations and assumptions as data, not comments; a ledger that includes scope declarations | C12 |
| H8 | Cloud cost per job (credits, instance hours) saved beside the job | C11 |
| H9 | The launch tree pushed or recorded (base commit and pull requests added) | C9 |
| H10 | The engine choice's hash in the analysis headers | Model |
| H12 | The comparison figure's spec diff (`toolkit/analysis/figures.py` `spec_differences`) misses spec sections: for F6 run 3 against run 4 it named only decision tests, while the signed specs also differ in `analysis.outcome_adjustments`, which is what doubled the headline | C6 |
| H13 | A harness-written file in the analysis folder that is outside the manifest and records no spec hash (`analysis/benchmarks_at_spec_values.yaml`), so nothing ties it to the signed spec | Steps |

## 8. The catch-early scorecard

Score per case: 0 not visible in the page; 1 shown or flagged in the page, but either not at the
earliest stage or resting on text because the structured field is missing (§7); 2 a fresh agent
found it in the planted-defect walkthrough (§9 layer 3). Total after iteration 1: 16 of 32 possible is not yet in reach; 10 points are scored (C2 and
C13 at 2 by walkthrough; C3, C9, C10, C11, C14 and C16 at 1, flagged in the page but not yet
walked through or resting on text).

| # | Problem | Earliest stage | Check | Now |
|---|---|---|---|---|
| C1 | Trial ratios applied over the whole window, not the trial follow-up (F6 run 3) | sign-off | spec `effect_window` | 0 |
| C2 | Model matches levels but not the change between survey rounds (F5) | calibration | fit `changes` | 2 |
| C3 | Assumed survey error understates some states' uncertainty (F5) | calibration | fit `target_error` | 1 |
| C4 | Benchmark out of band or just inside it (B8) | engine choice | model `benchmarks` | 0 |
| C5 | Window cut off campaigns; SMC rebound (F2–F3) | before the outcome batch | scenarios `trajectories_present` | 0 |
| C6 | Large package shift between runs, unflagged (F2–F3) | analysis | outcomes `run_shift` | 0 |
| C7 | Headline averts an implausible share of burden (E7 attempt 6) | analysis | outcomes `share_of_burden` | 0 |
| C8 | Dearest option wins nearly everywhere (F6 run 3) | analysis | outcomes `dearest_wins` | 0 |
| C9 | Wrong pipeline or unknown code ran (2026-11-05) | launch | steps `pipeline`, development `launch_tree` | 1 |
| C10 | Signed spec differs from the spec used or on disk (F6 run 4) | sign-off | steps `signed_spec`, `inputs_fresh` | 1 |
| C11 | Spend or time far above the estimate (P6, F6 run 1) | during the run | steps `cost`, spend | 1 |
| C12 | A reviewer's condition dropped with no disposition (F6 run 3) | review | register | 0 |
| C13 | Calibration blocked, tiny region, parameters on bounds (F6 run 4) | calibration | fit `status`, `region` | 2 |
| C14 | Inputs the data do not constrain carry the decision (F6) | analysis | fit `constrained`, outcomes `voi_support` | 1 |
| C15 | Wrong source behind an input (first malaria review) | evidence | spec `dossier_coverage` | 0 |
| C16 | A stage loops or a task fails silently (WM6) | during the run | steps `progress` | 1 |

## 9. Verification

Four layers. A slice is "built" when layers 1 to 3 pass, and "done" when the owner has also
walked through it (layer 4). The loop moves to the next goal once a slice is built.

1. **Self-tests on fixtures.** `python3 tools/workbench/workbench.py --self-test`, found by
   `scripts/self_tests.py`. Each check has a fixture where it is `ok` and one where it fires.
   Written before the code. Includes: confined reads, loopback only, no HTML built from run text.
2. **Acceptance on the reference runs.** `--accept <run> tools/workbench/acceptance/<name>.yaml`
   compares what the page's data endpoint returns with the values quoted in §5, and fails on any
   difference. The quoted values were read from the files by one agent and spot-checked by the
   lead; before a slice starts, a fresh agent re-derives that view's values from the files and
   any disagreement is settled in this document first.
3. **Planted-defect walkthrough.** For each scorecard case there is a fixture run with that one
   defect planted, and a clean twin. A fresh agent is given only the running page (through the
   browser) and the question "what, if anything, should the owner look at in this run, and
   where is the evidence?" It passes when it names the planted defect and its evidence on the
   defective run and raises nothing of that kind on the twin. This is what scores a case 2.
4. **Owner walkthrough.** The owner reviews one real run in the workbench. Each note is either a
   missing view (a new scorecard case) or a science item (a register row). A slice the owner has
   not seen is "built", not "done".

Contracts the layers share:

- **Three fixtures per check, not two.** Besides the clean run and the planted defect, each
  check has a fixture whose inputs contradict each other or are missing a piece (a status
  against its own blocks, a declaration with no verified signature, a hash that is absent, a
  value the harness could not compute), and the check must not answer `ok` or `look` on it. The
  first external review found eleven fail-open cases of this kind that clean-and-defect
  fixtures could not show. Fixtures write the producer's real words, read from the producer's
  code, never a convenient stand-in.
- **Fixture builder.** `tools/workbench/fixtures.py` `build(dir, upto=None, defect=None,
  method=...)` writes a small clean run with every file §5 reads (three groups, twelve units,
  two survey rounds), shaped like the named method's real files. `upto` is one of launch,
  sign-off, calibration, analysis, final, and leaves out every file a later stage writes, so a
  defect can be shown at its earliest stage. `defect` is a key of `DEFECTS`, which lists for each
  its scorecard case, the check it moves, the status expected and the one edit it makes; a case
  may have several variants (declared, undeclared, near miss). A variant that adds a declaration
  also changes the spec, and so every file that records the spec's hash. On the clean run every check is `ok` or
  `not_available`, never `look`: a near miss counts as raising something.
- **Data endpoint.** `GET /api/view?run=<id>&view=<name>` returns exactly `{checks, facts,
  tables, charts, notes}`: `checks` a list; `facts` `{name: value}`; `tables` `{name: {columns,
  rows, about}}`; `charts` a list of declarations; `notes` `{name: text}`. The page draws only
  from this. A fact's unit is carried by its name's suffix (`_usd`, `_minutes`, `_share`); a
  table column's unit is in its name in brackets. The chart declaration keys, beyond `kind`,
  `table`, `title` and `how_to_read`, are documented in the docstring of
  `tools/workbench/workbench.py` and are the only ones a view may use (`x`, `y`, `low`, `high`,
  `group`, `model`, `model_alt`, `outer_low`, `outer_high`, `nodes`, `scale`, `where`, `panels`,
  `labels`, `limits`).
- **The rail.** Order: Overview; then Spec and evidence, Model, Fit, Scenarios, Outcomes; then
  Steps, Figures, Science register, World model, Development, Spend. Each entry's status mark is
  the worst check status of that view, read from the overview's `tables.views`. A view "exists"
  for the page when its module can be imported; whether it is "built" is the promotion rule of
  §3a, recorded in §10.
- **`--accept`.** The view is the acceptance file's `view:` key; `[col=value]` takes the first
  matching row; an empty file fails.
- **No run values in the repository.** The repository is public and some reference runs' inputs
  are licensed, so values read from a stored run (survey values, model outputs, plan numbers)
  are never committed: not in this file, not in acceptance files, not in fixtures or tests. This
  file quotes counts, statuses and file hashes only. Acceptance files live beside the runs, in
  the keep folder under `workbench/acceptance/`. Before a push, `python3
  tools/workbench/leak_check.py <run> ...` must pass: it fails if a number with four
  or more significant digits in any tracked workbench file or in this file also appears in the
  named runs' analysis files or inputs. `--range <base>..<head>` scans the added lines of every
  commit to be pushed, so a value removed by a later commit is still caught; reviewed chance
  matches are listed with a reason in `tools/workbench/leak_allow.txt`, each for one file and
  one exact value.
- **Acceptance file.** `<keep>/workbench/acceptance/<view>_<run name>.yaml`: a list of `{path, equals |
  approx, source}`, where `path` addresses the endpoint's JSON (for example
  `facts.region_accepted`) and `source` is the run file and key the value came from. Written in
  loop step 2 by the agent that re-derives the values.
- **Walkthrough.** The lead builds the defective run and its twin into two folders with neutral
  names, starts `workbench.py --run-dir <folder>`, and gives a fresh agent the address and the
  question only; the agent uses the browser tools. The lead judges the answer against the
  builder's list of edits.

Also on every slice: the lead opens each new view on R4 and reads it (screenshot) before
reporting, and records anything it shows about the real run that was not already known.

## 10. Goals and the loop

| Goal | Slice | Done when |
|---|---|---|
| G0 | This specification agreed | the owner has answered §11 |
| G1 | α Skeleton, overview, Steps | C10, C16 at 2; §5.2 acceptance passes |
| G2 | β Fit | C2, C13 at 2; C3 at 1 (2 needs H3); C14 at 1 (2 comes with G5) |
| G2b | β3 Figures view; a confined, hash-checked file endpoint for figure images | §5.12 acceptance passes on R4 and FLU |
| G3 | γ Science register, derived; owner notes | C12 at 2 |
| G4 | δ Model | C4 at 2 |
| G5 | ε Outcomes | C6, C7, C8 at 2; C14 at 2 |
| G6 | ζ Spec and evidence | C1, C15 at 1 (2 needs H1, H2) |
| G7 | η Scenarios | C5 at 2 for "no trajectories"; dynamics need H4 |
| G8 | θ Development | C9 at 2 for unknown launch trees |
| G9 | ι Spend | C11 at 1 (2 needs H8) |
| G10 | κ World model | §5.9 acceptance passes |
| G11 | λ Harness changes H1–H10 and the register artifact | each needs the owner's yes |

Each iteration of the loop:

1. Read this file. Take the first goal not done.
2. A fresh agent re-derives the slice's acceptance values from the reference runs (layer 2).
3. A fresh agent writes the failing self-tests and the planted-defect fixtures from §5.
4. A fresh agent builds the slice from §3 and §5 until layers 1 and 2 pass.
5. A fresh agent does the planted-defect walkthrough (layer 3).
6. The lead reads the view on R4, updates §8 and §12, adds the change fragment, opens the pull
   request, runs one Codex review and fixes any P1.
7. Owner notes since the last iteration come first in the next one.

Wherever an agent could not do its step from this file, the lead fixes the file and the step is
rerun by another fresh agent. The loop stops and reports when a goal needs the owner, or when two
iterations in a row raise no score.

## 11. Owner's decisions (2026-11-18: "yes to all")

1. The working model is as described at the top of this file.
2. Order of goals as in §10, Fit first.
3. The workbench thresholds are accepted as written.
4. Harness gaps H1–H10 and the register artifact (G11) are queued for after the F6 report.
5. The run console is kept for sign-offs; its other views retire as the workbench covers them.
6. Workbench pull requests merge at green checks and no P1 without asking each time.
7. The workbench is to be agnostic to the problem at hand (§3a).

The questions as they were put:

1. Is the reading of the working model in the first section right (this file as the only memory;
   fresh agents build and verify from it)?
2. Order of goals: Fit, then register, then Model, then Outcomes (as in §10), or another order?
3. The workbench thresholds named in §3 and §5 (near miss at 25%; averted above 50% of
   comparator; emulator and engine differing by more than 1; shift between runs above 10% of
   units or a factor of 1.5): accept, or change?
4. The register's step 2 and the harness gaps H1–H10: which to queue for after the F6 report?
5. The run console: keep it for sign-offs only, and retire its other views once the workbench
   covers them?
6. Merging: workbench pull requests merge at green checks and no P1, without asking each time?

## 12. Log

- 2026-11-18, loop iteration 1, step 2 (acceptance values re-derived by a fresh agent): no quoted
  number disagreed. Corrections folded in: three units in R4 fail verification under the spec's
  allowance with no test (status rule extended); units at a bound is 161 counting both bounds;
  the posterior field map; `near_cutoff`, `group_bias`, `convergence` and `series` added.
  Candidate checks not yet specified: targets matched by an equal number of free parameters
  (FLU: five targets, five free ratios, so they do not test fit); best point far from the
  posterior median along a ridge (DEN).
- 2026-11-19, loop iteration 2, step 4: register view built by a fresh agent (derivation, six
  checks, the review-notes endpoint and page control). Self-tests pass (register 56 cases);
  acceptance passes on R4, F5 and FLU with no entry changed. First CI run failed two repo-wide
  audits (writes through `lib.safe_io`; guarded dynamic imports); the workbench now complies
  as the audits stand (a static view registry, safe writes), with no edit under `scripts/`,
  which is inside the code fingerprint. Still to do for G3: the lead's read of the screenshots,
  an external review of the register and notes endpoint, the planted-defect walkthrough.
- 2026-11-18, loop iteration 2, steps 2 and 3: register fixtures (17 defects, four of them
  contradictions or missing pieces) and 56 failing cases written by a fresh agent, who checked
  them against a scratch derivation and 22 mutants. Acceptance re-derived under the corrected
  rules; science rows open: 34 of 47 on R4, 9 of 17 on F5, 16 of 19 on FLU (plus 13 process
  rows, all open).
- 2026-11-18, second Codex round on the review fixes: 8 more P1. Seven are being fixed test
  first (a scope declaration excusing a flagged change with no verified spec; the unit allowance
  excusing conflicting units; an artifact with no spec hash dropping out of the check; the leak
  check joining CSV fields, missing leading-dot decimals, an allow entry excusing a different
  number, and a git setting emptying the commit scan; hash and parse from one read). The eighth,
  a race while files are replaced during a read, is answered by stating the limit: the workbench
  does not defend against run files changing while it reads them.
- 2026-11-18, loop step 6 (external review by Codex of the first three pull requests): 14 P1 and
  1 P2, none judged wrong. Each was reproduced with a failing test and fixed: seven fail-open
  cases in the Fit and Steps checks (rules now in §3), three weaknesses in the leak check, four
  in the server's file handling, one test that could not fail. Acceptance on the reference runs
  was unchanged. Left open: scope declarations and findings are not yet tied to the verified
  signed spec; the range scan covers `tools/workbench/` and this file only.
- 2026-11-18, before the first push: the lead found that the repository is public and that this
  file and the acceptance files quoted values from the licensed reference runs. Nothing had been
  pushed. Acceptance files moved to the keep folder; values removed here (counts, statuses and
  hashes stay); `--leak-check` added as a required step before any push (§9).
- 2026-11-18, loop iteration 2, step 2 (register derived by hand by a fresh agent): R4 48 rows
  (35 open), F5 17 (16 open), FLU 32 (29 open, 12 of 13 HIGH rows about stale analysis files,
  not science). Corrections are in §6. Found on the way: on FLU, round-2 critique rows and
  `resolved` marks never reached `findings.jsonl` (schema errors at the gate), a harness fault
  to report after F6. The acceptance files are a first pass and are re-derived under the
  corrected rules in step 3.
- 2026-11-18, loop iteration 1, step 5 (walkthrough): a fresh agent saw three pages only (an
  undeclared calibration block, an undeclared missed change, a clean run, under neutral names).
  It named both planted defects with their check, table row and source file, and raised neither
  kind on the clean run. C2 and C13 score 2. Its reader findings, to fix before the next view:
  1. the codes "scorecard case C13", "goal G6", "gap H3" and "workbench threshold" are undefined
     on the page: each needs its meaning on hover and a glossary at the foot of the page;
  2. a flagged change beside "status ok, no block" with nothing said about how they relate: the
     status sentence must say which other checks can still fire;
  3. "not finished… if nothing is running" cannot tell a live run from a dead one: show the time
     since the last file was written;
  4. the Overview is many near-identical cards: collapse `ok` and `not_available` checks to one
     line each, keep `problem` and `look` open, and keep the view order fixed;
  5. the Steps artifacts table names the manifest as its source when the manifest is missing; a
     clipped column; an axis tick printed as 6.9e-18; "1 of 12 units at bound" does not name the
     unit; the run list names the scanned root wrongly when a run is given by `--run-dir`;
  6. fixture realism: a planted block must also change the table the block is about (offsets),
     and a change pair must be derivable from the fixture's target rows.
- 2026-11-18, loop iteration 1, step 4: Fit view β1 built by a fresh agent (generic model, 11
  checks, charts, `history_matching` and `posterior` adapters). Self-tests pass (Fit 45 cases);
  acceptance R4 298 of 298, FLU 169 of 169, DEN 78 of 78, re-run by the lead, who also read the
  R4 screenshots. First answer on R4 1.4 s, then 13 ms. What the view shows on R4 that was not
  in any report: the engine sits below the later-round unit targets in most groups (mean signed
  score beyond −1 in three), while the same groups' earlier-round targets are matched or
  overshot; emulator and engine scores differ by more than 1 on 103 of 309 unit targets; 11
  targets pass within 25% of the cutoff on one engine run per point; five parameters listed as
  prior-dominated contracted by more than half. Candidates for the science register.
- 2026-11-18, loop iteration 1, page shell built by a fresh agent (generic renderer, five chart
  kinds, `/api/view`, `--accept`, Steps and Overview as view modules; self-test W1–W25 passes;
  §5.2 acceptance on R4: 14 of 14). The lead read the overview and Steps screenshots. Its
  clarifications are folded into §9. Left for later slices: the harness figure beside each chart
  (needs the file endpoint of G2b); interaction tests of sort, filter and hover; a state word on
  graph nodes.
- 2026-11-18: owner asked whether the paper figures get a view and inform the others: §5.12 and
  goal G2b added. Fit charts built before this read the method's artifact; they move to the
  figure tables where one exists when G2b is built.
- 2026-11-18: a fresh agent read this file cold and listed what it could not do from it; its
  points are folded in (statuses for declared items, the Fit field map, the register derivation
  table, the fixture and acceptance contracts, scores reset to 0).
- 2026-11-18: survey by seven fresh readers (console audit; world model and spend; fit; model and
  scenarios; outcomes; spec, evidence and register; pull requests and spend). Spike of G1 built
  (server and eight Steps checks, no page). Spike retro on R4 and F5: see §5.2. Known spike fault:
  it reports `completed_scope_declared` as `problem`; §3 makes it `look`.
