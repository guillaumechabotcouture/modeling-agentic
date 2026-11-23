# v2: self-audit checks the harness computes (design note)

Design only, nothing built. Base: `origin/v2` at `dea0beec`. Written 2026-11-23.

Inputs read: the outside benchmark's lessons and our head-to-head plan (kept outside the repository, cited as
**lessons §n** and **plan §n**); `.claude/orchestration/decision_design_principles.md`; `toolkit/analysis`
(calibration, joint calibration, observed targets, uncertainty, decision tests, trajectories, stability, contract);
`toolkit/calibration/history_matching.py`; `toolkit/engines/benchmark.py`, `benchmark_sources.py`,
`benchmark_coverage.py`, `effect_fit.py`, `effect_settings.py`, `decision_inputs.py` and the three engine cards;
`toolkit/spec/engine_parameters.py`, `evidence_dossier.py`, `preconditions.py`, `spec_critique.py`; and the designs
in flight: agent-built engines (branch `design-agent-engines`), cost-effectiveness (branch `design-cea`, PR #364),
the sensitivity-wiring check (PR #365, branch `sensitivity-wiring`) and the critique checklist (branch
`critique-checklist`).

## 1. The problem

An outside benchmark ran three frontier agents with no harness on four modelling tasks. They built plausible
models, but they did not audit their own work. The reports read well while the code held silent flaws: a sensitivity
case that changed nothing, a burn-in that moved a nominal setting, a holdout that overlapped an epidemic, a waning
curve whose effect over the trial's follow-up was half the trial's (lessons §3).

Our harness's thesis is verification first. Every number comes from harness code, and the audits are steps the
model cannot skip. Most of the benchmark's failure classes are already prevented for our built-in engines (lessons
§3.1-3.2). Six are not: in each, our code computes the quantity but does not compare it with the evidence it should
match. This note designs one deterministic audit for each.

Two rules from earlier owner decisions shape every design here:
- **Preconditions over post-hoc gates.** Where an error can be made impossible, the harness does that instead of
  detecting it (holdout by construction, §5.1; provenance at sign-off, §5.2, §7). Where it must be detected, the
  check calls the run's own code on the run's own values (as the evidence dossier and benchmark eligibility do).
- **Extend, do not duplicate.** Each audit reuses an existing record or step (§9 lists which).

### Terms used here

| Term | Meaning |
|---|---|
| estimand | the exact quantity a study reports: the outcome (endpoint), the scale (risk ratio, rate ratio, odds ratio, hazard ratio, vaccine efficacy = 1 − ratio), the population (age, serostatus, setting), the comparator arm and the follow-up window |
| band | a benchmark's acceptance interval; for a trial, derived from its published interval (`benchmark_sources.py`) |
| run values | the parameter values the analysis actually uses: engine defaults, overridden by `engine_parameters`, then by calibrated values (the point), or one uncertainty draw |
| decision-relevant | an input that can change the plan: §4.1 gives the computed rule |
| holdout | calibration observations kept out of the fit and used only to score it |
| persistence baseline | a forecast that each unit's next value equals its last observed value; the simplest forecast a model must beat |
| skill | 1 − (model error / naive-baseline error) on the holdout; above 0 means the model beats the baseline (the idea behind the mean absolute scaled error, Hyndman & Koehler 2006, *Int J Forecast* 22:679) |
| posterior predictive check | a test of whether the observed data look like data the fitted model would generate: the statistic is computed on simulated data many times, and the observed value is placed in that distribution (Gelman, Meng & Stern 1996, *Stat Sinica* 6:733) |
| provenance class | what kind of number a data row is: `measured`, `derived_by_ratio` or `modelled` (§5.2) |
| lineage | for each column a derivation writes, the input columns it depends on, found by the harness by perturbing inputs (§7.1) |
| nominal setting | a value the spec states for a unit (an eligibility tier, a coverage) that the engine is supposed to reproduce at rollout |
| burn-in | the simulated period before the decision window that brings the engine's state to the start date |
| CRN | common random numbers: arms run with the same seeds, so the difference between arms is not noise |

### How findings route (existing vocabulary)

| Route | Where it lives | Effect |
|---|---|---|
| precondition | registered in `toolkit/spec/preconditions.py` | signing is refused, on every path (signers, launch, resume) |
| pre-sign sheet check | `toolkit/spec/spec_critique.py` | shown on the sign-off sheet; the owner decides; declared in `principle_departures` |
| gate finding, not declarable | `toolkit/analysis/contract.py` `gate_findings` | HIGH; the run cannot reach ACCEPT |
| gate finding, declarable | same, with `scope_declarable: true` | HIGH; the fix loop, or a declaration backed by a decision test |
| owner amendment | a remedy matching `findings_ledger.OWNER_AMENDMENT_RE` | `route_gate` sends it to OWNER_DECISION |
| note | `results.md` | LOW; reported, never blocking |

New thresholds go in one new section, `self_audit`, of `toolkit/analysis/thresholds.yaml`. Every number in it is a
proposal for the owner, marked as such, as for `wiring`.

## 2. Summary

| Id | Audit | Runs | Route of the main finding | Size | Extends |
|---|---|---|---|---|---|
| SA1 | modelled effect integrated at the trial's estimand, at run values | ANALYZE, after calibration, before the outcome batch | HIGH, owner amendment | medium | benchmark runner, `engine_parameters benchmarks` step, A8 of the engine lane |
| SA2a | decision-relevant parameters at a bound | ANALYZE, after calibration | HIGH, owner amendment | small | `at_bound` in `calibration.py`, wiring rows, decision tests |
| SA2b | sign and correlation of temporal changes by group | ANALYZE, after calibration | HIGH, declarable | small | `survey_changes` (F6 S2-3b) |
| SA2c | holdout skill against naive baselines | ANALYZE, after calibration | MEDIUM or HIGH, declarable | small | needs SA3a |
| SA3a | holdout by construction, with shock periods | SPEC (split declared) and ANALYZE (split enforced) | structural; refusals | medium | critique-checklist `periods`, the PR #140 held-out rule |
| SA3b | provenance class of calibration targets | SIGN | precondition | medium (with §7.1) | derivations record, input-column accounting |
| SA4 | nominal setting against its realised value at rollout | ANALYZE, after calibration, before the batch | HIGH, declarable with a decision test | medium | `run_series` hook, trajectories, checklist item `burn_in_drift` |
| SA5 | denominator provenance | SIGN | precondition | small (after §7.1) | lineage of §7.1, `eligibility_probe` invariant (c) |
| SA6 | effect scoping: a component's parameter does not move arms without it | ANALYZE, with the wiring check | HIGH, not declarable | small | PR #365 wiring evaluations; A6/A7 of the engine lane |

## 3. SA1: the modelled effect at the trial's estimand

**The failure.** A waning efficacy curve that starts near the trial's value and decays fast has a mean over the
first year of about 37%, against a trial endpoint at one year of 68-75%. Each piece looks sourced; the integral over
the trial's follow-up is what contradicts the trial. Our own F6 round 3 had the opposite form of the same error:
trial ratios measured over a short follow-up applied as constants over a much longer decision window
(an estimand mismatch the critics found).

**What exists.** Benchmark packs already score trial estimands: `benchmark.py` simulates each trial's arms in its
setting, reads the estimand (`ratio`, `odds_ratio`, `interaction_or`) over the declared window, and compares it with
a band derived from the published interval (`benchmark_sources.py`). `effect_fit.py` fits effect parameters at each
trial's own estimand. `benchmark_coverage.py` makes an engine ineligible when a benchmark covering a used component is
out of band. The gap: all of this runs **once per engine version, at the pack's fitted values**. A run then uses
other values:
- `engine_parameters` may set a fixed or scenario parameter for the run;
- calibration moves shared parameters (`calibration.joint.shared`, likelihood and posterior `parameters`), which can
  include parameters an effect depends on;
- uncertainty draws vary effect parameters over their priors;
- `effects_from_spec` components and the empirical engine's `trial_priors` mode take the effect from the spec, with
  no benchmark at all;
- an agent-built engine's A8 checks its trials at its declared default values only.

Today only `interaction_rescale` decision tests read benchmark arms at the run's spec values
(`analysis/benchmarks_at_spec_values.yaml`, written by `engine_parameters.py benchmarks`).

**What it computes.** For every component a used package holds, and every trial anchoring its effect, the modelled
estimand at the run values:
1. The trial set: every benchmark that `benchmark_coverage.yaml` lists as covering the component; for a built
   engine, `request.trials`; for an `effects_from_spec` component or a `trial_priors` parameter, the dossier entry's
   trial observations, which gain structured estimand fields (below).
2. The parameter sets: the point (engine defaults, then `engine_parameters`, then the calibrated point), and `K`
   uncertainty draws taken from the uncertainty step's own sampler and seed, stratified over each effect parameter's
   range (proposal K = 16).
3. For an engine: each trial's arms run through the benchmark runner at each parameter set, in the trial's own
   setting (the setting's observed conditions win over run values, as in the existing step), with the pack's seeds.
   The estimand is read exactly as the pack reads it.
4. For an effect the spec supplies as a curve (a waning efficacy `VE(t)` declared with its parameters, as the
   cost-effectiveness design and agent-built requests allow): the harness integrates it over the trial's follow-up
   with the trial control arm's hazard `h`, giving the cumulative-incidence ratio
   `(1 − exp(−∫ h (1 − VE(t)) dt)) / (1 − exp(−h T))` over `[0, T]`. A constant ratio integrates to itself, so for a
   static `trial_priors` effect the check reduces to the estimand fields matching (below).

**Estimand fields.** The dossier's trial observations gain `estimand: {endpoint, scale, population, comparator,
window: [t0, t1], window_from: enrolment | last_dose}`. They are compared field by field with how the run uses the
effect: the outcome the component's effect acts on, the scale the engine or the empirical rule applies (an odds ratio
used as a risk ratio is a mismatch unless converted at the trial's control risk), and the decision window against the
follow-up (the existing `follow_up_window` sheet check, now computed from the structured window).

**Artifacts read.** `decision_spec.yaml`; `evidence_dossier.yaml`; the engine's pack, `benchmark_coverage.yaml` and
verified record; the calibration record (`analysis/calibration*.yaml`, `analysis/joint_calibration.yaml`); the
uncertainty sampler's seed. Writes `analysis/effects_at_run_values.yaml` (manifest-listed, hashed). The existing
`benchmarks_at_spec_values.yaml` becomes the `spec_values` slice of the same step, so interaction tests read the same
rows.

**Tolerance and its source.**

| Test | Tolerance | Source |
|---|---|---|
| point inside the band | the benchmark's band: the published interval, or a recorded `band_override` | the trial's own interval (`benchmark_sources.py`); principle 4 (the trial wins) |
| stochastic engine at the point | the pack's acceptance rule: the seed interval of the geometric mean overlaps the band | `effect_fit.py` `seed_interval_overlaps_band` |
| draws | share of the K draws inside the published interval at least `self_audit.effect_draw_share_min` (proposal 0.5) | proposal: a prior centred on the trial's interval puts most draws inside it |
| estimand fields | exact match, or a `mapping` note stating the conversion | `benchmark_sources.py` rule for packs, applied to dossier entries |

**Findings and routing.**
- `effect_estimand_out_of_band` (HIGH, decision-bearing): the point is outside the band. Not declarable on its own.
  It is answered by an `engine_departures` entry with a decision test that varies the effect (the existing
  `benchmark_coverage` departure rule), so the remedy is an owner amendment and routes to OWNER_DECISION. Because
  the step runs before the outcome batch, no paid batch runs on an effect that contradicts its trial.
- `effect_estimand_draws_outside` (MEDIUM, declarable): the point is in band but too many draws are not. The usual
  remedy is narrowing the prior to the trial's interval (an owner amendment).
- `effect_estimand_mismatch` (HIGH, pre-sign sheet check): an estimand field does not match and has no mapping note.

**What it cannot catch.** A trial that is not declared or not covered (an uncovered component already needs a
departure). Transport to the decision's setting: the check is in the trial's setting, and `effect_settings.py` lists
units outside the trial settings. Effects beyond the trial's follow-up (only flagged). Interactions no trial measured.
A mapping note that is present but wrong.

**Tests to write first.**

| Id | Test |
|---|---|
| SA1-1 | a toy engine whose pack is in band at fitted values, with an `engine_parameters` value that halves the effect: `effect_estimand_out_of_band` at the point |
| SA1-2 | a calibrated shared parameter that moves an effect parameter's realised effect out of band: the finding names the calibration as the cause |
| SA1-3 | the waning case: `VE(t) = VE0 exp(−t/τ)` with a one-year mean near 0.37 against a band [0.68, 0.75]: out of band; the integral matches the closed form to 1e-9 |
| SA1-4 | a constant ratio integrates to itself; an odds ratio declared as `scale: risk_ratio` without a mapping is a mismatch |
| SA1-5 | draws: a prior twice the trial's width gives `effect_estimand_draws_outside`; one matching the interval does not |
| SA1-6 | the step's rows are reused (row reuse keyed on the parameter set and engine identity); a changed effect parameter reuses none |
| SA1-7 | an `interaction_rescale` test reads the same rows as before (no change in its result on the existing fixture) |
| SA1-8 | fail closed: a missing published interval, an unreadable record, or a covering benchmark the step did not run is a finding |

**Smoke markers.** `--expect 'analysis/effects_at_run_values.yaml:rows'`, `--expect
'analysis/effects_at_run_values.yaml:point_sha256'`, `--expect 'analysis/manifest.json~effects_at_run_values'`.

## 4. SA2: fit diagnostics beyond average error

### 4.1 SA2a: decision-relevant parameters at a bound

**What exists.** The likelihood fit records `at_bound` (a parameter within 1e-6 relative of its range) and the
contract raises a HIGH `analysis_contract_calibration` finding for any of them. Emulator inversion counts
`clamped_units`. The posterior and joint (history-matching) methods have no edge flag for the region; joint
calibration reports units no value matches, with the direction of the miss. `effect_fit.py` has optional, declared
`edge_checks`.

**What it computes.** For every calibrated parameter and every calibrated unit attribute, under every method:
- point at a bound: within `edge_tol` (proposal 1% of the range width) of either end;
- mass at a bound, for methods with a sample (posterior, joint region, effect-fit region): the share of the sample
  within `edge_band` (proposal 5% of the width) of one end. A sample that ignores the bound puts about 5% there; the
  flag is a share above `edge_mass_max` (proposal 20%, four times that), which says the data pull past the range.

It then classifies each flagged parameter as decision-relevant when any of these holds, all read from records the
run already writes:
1. it is a decision input of a used component (`decision_inputs.lookup`);
2. its wiring row (PR #365, `analysis/wiring.yaml`) moves the objective outcome above the wiring tolerance;
3. a decision test that varies it changes the plan (`decision_changes` in its record);
4. for a unit attribute: the unit is funded in the plan, or among the units closest to switching
   (`boundary.py`'s boundary units).

**Tolerance source.** The edge thresholds are proposals (open question 7). Decision relevance uses the wiring
tolerance and the decision tests' own share threshold (`analysis.share_threshold`).

**Findings and routing.** `calibration_at_bound_decision_relevant` (HIGH, decision-bearing). The remedy is an owner
amendment: widen the signed range with a source, or keep it and add a `parameter_value` decision test at values past
the bound (`engine_parameters` already allows `outside_card_range: true` with a HIGH note). Routes to OWNER_DECISION.
A flagged parameter that is not decision-relevant is a note in `results.md`. The existing likelihood HIGH stays as it
is (open question 8).

**What it cannot catch.** A parameter pinned inside its range by a wrong prior; a ridge (identifiability's job);
a decision relevance that only appears jointly with another parameter.

**Tests to write first.** SA2a-1 a posterior sample piled against the upper bound of a parameter the wiring row marks
as moving the objective: HIGH. SA2a-2 the same parameter, not wired to the objective: a note. SA2a-3 a joint region
whose shared parameter touches the prior edge: flagged. SA2a-4 a funded unit's attribute at its grid end: HIGH; an
unfunded unit far from switching: a note. SA2a-5 no wiring record: a finding (fail closed), not a pass.

**Smoke markers.** `--expect 'analysis/fit_audit.yaml:at_bound'`, `--expect 'analysis/fit_audit.yaml:edge_band'`.

### 4.2 SA2b: temporal change by group

**The failure.** A fit with a good average error whose modelled change over time runs against the observed change:
the levels match one survey round and the change between rounds is wrong in sign in many groups. Our own calibration
reviews have found this shape; the benchmark found it in bare agents' fits.

**What exists.** `survey_changes` in the joint calibration (F6 S2-3b) pairs two rounds of one survey by group and
gives each pair's observed and modelled change, their difference and a z score against `survey_changes.z_max`. Each
pair is judged alone. Large per-group survey errors can leave every z small while the changes are anti-correlated
across groups.

**What it computes.** Over the groups of each survey pair set (and, for the likelihood and posterior methods, over
units with targets in two or more periods), with weights as `changes.weights` declares:
- the sign agreement share between observed and modelled change;
- the weighted correlation `r` between observed and modelled change, and the slope of observed on modelled;
- a posterior predictive reference: `M` (proposal 1,000) simulated observed changes, each the modelled change plus
  noise from that pair's own `sd_parts` and declared `sampling_correlation`, and the same statistics on each;
- `p`: the share of simulated `r` at or below the observed `r`.

**Tolerance and source.** A flag when `p < self_audit.trend_p_min` (proposal 0.05), with at least `trend_min_groups`
groups (proposal 5; fewer is `not_judged`, never passed). The reference distribution comes from the targets' own
declared errors, so a noisy survey is not penalised for noise (posterior predictive check, Gelman, Meng & Stern
1996).

**Findings and routing.** `calibration_trend_mismatch` (HIGH, declarable). It bears on the decision when the
calibrated attribute carries the trend into the decision window; the declaration needs a `calibration_target`
decision test on the flagged groups. Remedies: a structural fix (the change usually means a mechanism is missing,
such as an intervention history in the groups that diverge), which is a spec amendment, or a declaration.

**What it cannot catch.** A trend error common to all groups (the correlation is across groups; the existing
per-pair z covers a shift common to all); trends within one period; groups with one observation.

**Tests to write first.** SA2b-1 a fixture with every per-pair z under `z_max` and anti-correlated changes: flagged.
SA2b-2 correlated changes with large survey errors: not flagged. SA2b-3 four groups: `not_judged`. SA2b-4 declared
sampling correlation is used in the reference (a correlated case differs from an independent one). SA2b-5 the
record's statistics are rebuilt by the gate from the pairs (a tampered `p` is a finding).

**Smoke markers.** `--expect 'analysis/joint_calibration.yaml:survey_changes.trend'`, or for other methods
`--expect 'analysis/fit_audit.yaml:trend'`.

### 4.3 SA2c: holdout skill against naive baselines

Needs SA3a (a holdout exists only when the harness made one).

**What it computes.** For each holdout target series (unit or group, metric, scale), the model's prediction at the
calibrated point, and three naive baselines built from the training rows only: persistence (the last training value),
the training mean, and for monthly or seasonal series the seasonal naive (the same season in the last training year).
Errors are on the target's declared scale (`logit`, `log`, `identity`), as the calibration compares them:
- skill against the best of the baselines, `1 − MAE_model / MAE_best_naive`, with a bootstrap interval over units;
- the rank correlation across units of the holdout values with the model's and with persistence's predictions,
  because the allocation uses the ranking of units more than the levels.

**Tolerance and source.** Skill above 0 (a model must beat the simplest forecast; Hyndman & Koehler 2006). Rows in a
declared shock period are scored separately (`stress`) and not judged against the baselines, since naive forecasts
miss shocks too (the benchmark's dengue case, lessons C-DEN-H).

**Findings and routing.** `holdout_no_skill` (MEDIUM, declarable) when the skill's point is at or below 0;
`holdout_worse_than_naive` (HIGH, declarable) when its 95% upper bound is below 0. A series with no training value
before the holdout is `not_judged`. Principle 3 bears on the reading: when the burden is anchored to external
estimates, the engine's own forecast matters through the calibrated attribute, so the declaration needs a
`calibration_target` or `burden_scale` decision test.

**What it cannot catch.** A holdout that is too easy (SA3a chooses it by rule, not the drafter); skill on one metric
when the decision turns on another the data do not observe.

**Tests to write first.** SA2c-1 a model equal to persistence plus noise: `holdout_no_skill`. SA2c-2 a model that
predicts the opposite trend: `holdout_worse_than_naive`. SA2c-3 a shock-period row is in `stress` and not in the
skill. SA2c-4 the baselines read only training rows (a holdout value placed in the training file is not used).

**Smoke markers.** `--expect 'analysis/holdout.yaml:skill'`, `--expect 'analysis/holdout.yaml:baselines'`.

## 5. SA3: holdout by construction, and the provenance of targets

### 5.1 SA3a: the harness makes the split

**What exists.** No temporal holdout in calibration. The emulator's `holdout_share` scores the emulator, and
`stability.holdout_group` scores the allocation's robustness; neither holds out observations from the fit. The
critique-checklist branch adds a declared `periods: {holdout, shocks}` field and a computed check that a holdout
window does not overlap a shock. Engine packs already enforce the principle at engine level: a held-out benchmark can
never be a fit target, and `engine_parameters` refuses a value chosen with one (the PR #140 rule).

**What it does.** The split is part of how targets reach the fit, not a check after it:
1. **Declared at SPEC.** The spec's `periods` field (the checklist's, extended) gains `train: [a, b]`. When the spec
   says nothing and the targets span two or more periods, the harness assigns the latest period of each target series
   to `holdout` by rule and shows it on the sheet; `periods: {holdout: none, reason}` is a declared departure.
   Rows inside a declared shock window go to `stress`, never to `train` or `holdout`.
2. **Enforced at ANALYZE.** A new module, `toolkit/analysis/target_split.py`, is the only reader of calibration
   targets. It returns a `TrainTargets` object, which is what `calibration.py`, `observed_targets.py` and
   `joint_calibration.py` take; none of them opens a target file. Holdout and stress rows are written to
   `analysis/holdout/targets.json`, whose sha256 the manifest records.
3. **Sealed until the fit is recorded.** `holdout_eval.py` opens the holdout file only after the calibration record
   is written and its sha256 is in the manifest, and the record of the holdout score names that sha256. A fit
   recorded after the holdout was read is refused (`holdout_read_before_fit`, HIGH, not declarable).
4. **Static guard.** `scripts/import_audit.py` refuses an import of the holdout loader from any calibration module.

**What it cannot catch.** The spec drafter reads the staged inputs, including the holdout periods, and could choose
priors, ranges or structure with them. The split prevents the fit from using holdout rows; it cannot prevent a person
or a model who has seen them from choosing a structure that suits them (open question 4). Leakage through a derived
column that mixes periods (a moving average computed over all years) is visible only through lineage (§7.1): a
training-period output column whose lineage reaches holdout-period input rows is refused.

**Tests to write first.** SA3a-1 with `periods` absent and two survey rounds, the later round is holdout and the fit
never sees it (the fit's target count). SA3a-2 a calibration module that opens the targets file directly fails the
import audit. SA3a-3 a holdout read before the fit record: `holdout_read_before_fit`. SA3a-4 a shock-window row is in
`stress`. SA3a-5 a derived training column whose lineage reaches holdout rows is refused. SA3a-6 a spec without
periods and with one period: no holdout, `not_judged`, stated on the sheet.

**Smoke markers.** `--expect 'analysis/holdout/targets.json'`, `--expect 'analysis/holdout.yaml:fit_record_sha256'`,
`--expect 'decision_spec.signoff_sheet.md~Holdout'`.

### 5.2 SA3b: provenance class of each calibration target

**Classes.**

| Class | Means | Examples |
|---|---|---|
| `measured` | a direct observation of the quantity in that unit and period | a survey estimate, a surveillance count, a trial arm |
| `derived_by_ratio` | a measured quantity multiplied or divided by a factor from another source | prevalence turned into incidence with a conversion ratio; notifications scaled by an assumed completeness |
| `modelled` | the output of another model | a geospatial surface, a national burden estimate, an imputation, a projection |

A row is the weakest class of what it depends on (`modelled` below `derived_by_ratio` below `measured`).

**What it computes.** Each staged input declares its class (per file, or per column when a file mixes) in
`data/derivations.yaml` `sources:`. Where ACQUIRE (PR #356) fetched the file, its source allowlist carries a default
class per endpoint, and a declaration that differs is refused. The harness then checks each derived target column
against its lineage (§7.1). Lineage can refute a class but not prove one:
- a column declared `measured` whose lineage reaches a `modelled` input is `modelled`;
- one whose lineage reaches columns of two or more sources, other than identifiers and population weights, is at
  most `derived_by_ratio`;
- one computed with a numeric literal other than 0, 1 or a unit conversion the audit lists is at most
  `derived_by_ratio`. The literal audit is the derivation-side counterpart of S4 in the engine lane: a constant must
  be declared under `constants:` with its source.

**Rule (precondition).** A calibration target whose class is `modelled` or `derived_by_ratio` cannot be signed unless
the target declares `provenance: {class, reason}`. For `derived_by_ratio`, the factor's uncertainty must enter the
target's error: its `bias_sd` or `discrepancy_sd` is at least the factor's relative sd from the dossier, a computed
check. A `modelled` target that is also a `validation.burden` anchor is refused outright (the engine would be scored
against the quantity it was fitted to).

**Why.** Principle 5 prefers measured data to modelled surfaces, and principle 3 anchors burden to external
estimates and uses the engine for relative effects. Fitting to a modelled surface makes the engine reproduce another
model, and fitting to a derived row treats the factor as if it were measured.

**What it cannot catch.** A source mislabelled by its publisher; a modelled quantity published as if it were a survey
estimate; lineage through row filters (§7.1).

**Tests to write first.** SA3b-1 a target derived from a `modelled` input and declared `measured`: refused at
sign-off. SA3b-2 a target that multiplies a survey column by a literal 0.8: `derived_by_ratio`, refused without a
declaration. SA3b-3 with a declaration and a bias sd below the factor's relative sd: refused. SA3b-4 an ACQUIRE
endpoint whose default class differs from the declared one: refused. SA3b-5 a modelled target that is also a burden
anchor: refused.

**Smoke markers.** `--expect 'data/column_lineage.json:columns'`, `--expect 'data/target_provenance.json:targets'`,
`--expect 'decision_spec.signoff_sheet.md~Target provenance'`.

## 6. SA4: nominal settings against their realised values at rollout

**The failure.** A dengue model labelled a stratum by a nominal seroprevalence at age nine of 60%; after the burn-in,
the stratum's simulated value at rollout was about 75%, so the strategy ranked for "the 60% tier" was evaluated in a
75% population (lessons CL-DEN-2). The same shape arises with any state the spec states and the engine simulates:
immunity, coverage, prevalence, net use.

**What exists.** Every dynamic engine has the `run_series` hook (`toolkit/engines/hooks.py`; malaria, compartmental and
renewal), and the trajectory step writes monthly series for a sample of units after a run. The critique checklist adds
`burn_in_drift` as an item the critics answer by judgement. Nothing computes the comparison.

**What it needs declared.**
- **Engine card:** `nominal_states: {<unit attribute>: {metric: <series or window metric>, at: <time rule>}}`, the
  engine's statement that an input describes a state and which output measures that state (for example a coverage
  attribute and the realised use metric, at the attribute's measurement time). It is a card declaration, outside the
  code fingerprint, like `decision_inputs.yaml`.
- **Spec:** each eligibility rule's column and each `constraints` column built from a state declares
  `describes: {metric, at}` or `describes: none` with a reason. Coverage is checked at SPEC, as `columns.uses` is: a
  column whose lineage (§7.1) reaches a state-class input and has no `describes` entry is refused.

**What it computes.** For every declared pair, the realised value at the declared time under the comparator arm (and,
for a coverage a package delivers, under that package), at the calibrated point:
- on every unit within `drift_scan_margin` of an eligibility cutoff (the only units that can flip), plus a
  stratified sample of the rest (the trajectory step's per-region rule);
- through `scripts/compute.py`, so a real run sends it to the cloud; rows reused by input.

It writes `analysis/nominal_drift.yaml`: per unit the nominal value, the realised value, the difference, and for
eligibility whether the unit's side of the cutoff changes; and `analysis/nominal_drift_units.csv` with a `realised`
column for each declared state.

**Tolerance and source.**

| Case | Tolerance | Source |
|---|---|---|
| eligibility cutoff | none: a unit whose realised value is on the other side of the cutoff flips | the spec's own rule |
| continuous setting | the nominal's own uncertainty (its CI half-width in the dossier or the derivation), else `self_audit.nominal_abs_tol` (proposal 0.05 on the share scale) | the source; the default is a proposal |

**Findings and routing.** `nominal_state_drift` (HIGH, decision-bearing, declarable) when any unit's eligibility for an
allocated component flips. The declaration needs a `unit_attribute` decision test with `set_from` the
`realised` column, so the plan is re-solved with eligibility from the engine's state at rollout. The fix is an owner
amendment: derive the rule from the state at rollout. A continuous drift above tolerance with no flip is MEDIUM,
declarable.

**What it cannot catch.** A state the card does not declare; a nominal value that is itself wrong (the dossier's job);
drift after rollout (a different question: when the decision window is long, the state at rollout is the right
comparison for eligibility only).

**Tests to write first.** SA4-1 a toy engine whose burn-in raises immunity past a cutoff in two units: two flips,
HIGH. SA4-2 the same with the decision test declared and run: declared; without the test, not declarable. SA4-3 a
coverage drift of 0.03 with a source CI of ±0.04: no finding; 0.08: MEDIUM. SA4-4 an engine without `run_series`:
`not_offered` and a finding when the spec has a `describes` entry (fail closed). SA4-5 a state-derived column with no
`describes`: refused at SPEC.

**Smoke markers.** `--expect 'analysis/nominal_drift.yaml:pairs'`, `--expect 'analysis/nominal_drift_units.csv'`.

## 7. SA5: denominator provenance, and the lineage both provenance checks share

### 7.1 Column lineage (shared machinery)

The derivation runner already re-runs a derivation with an ignored column removed and compares the outputs
(`v2-derived-inputs.md` §3.5). Lineage generalises this: the harness re-runs each derivation once per numeric input
column, with that column scaled by a fixed factor (and once with each categorical column permuted), and records
which output columns change. It writes `data/column_lineage.json` (a harness file). It also records two homogeneity
facts per output column, used by SA5: whether scaling all the columns of one source together by 2 doubles the output
(degree 1 in that source), and whether it leaves the output unchanged (degree 0).

Cost: one sandboxed run per input column; derivations are cheap pure transformations. A derivation with more than
`lineage_max_columns` inputs (proposal 200) groups columns by file and refines only where a group changes the output.
Lineage does not see dependence through row selection (a filter on a column that the scaling does not cross); the
permutation runs catch filters on categorical columns, not thresholds on numeric ones.

### 7.2 SA5: population denominators

**What it computes.** For every denominator the spec binds: `decision_units.populations.*.column`,
`options.populations.*`, an eligible-population column, and a likelihood target's `exposure` and `n_column`:
- the lineage's input columns and their declared source class;
- whether the denominator is degree 1 in its population-class inputs and degree 0 in every survey-class input.

**Rule (precondition).** Each denominator has at least one input of a population source class, is degree 1 in those
inputs, and is degree 0 in every survey sample count. In words: survey counts may shape shares (an age structure, an
eligible fraction), never sizes. The population source classes are a list in `.claude/orchestration/` (proposal:
census, civil registration, WorldPop, UN WPP, GPW, LandScan, national statistics office projections); anything else
needs `denominators: {<column>: {source_class: other, source, reason}}` in the signed spec.

**Optional cross-check.** When the spec stages a total for the same area and year, the units' sum is compared with it
and the difference is reported (proposal: a note, open question 11).

**Tolerance and source.** Exact (degree tests to 1e-9 relative): the rule is structural, not statistical.

**Findings and routing.** Refusal at sign-off (`denominator_not_population_source`), with the lineage shown on the
sheet. A likelihood `exposure` built from survey counts is the same refusal.

**What it cannot catch.** A population source with the wrong year or area (vintage, lessons item 9); a wrong age
split applied to a correct total (a share is allowed from a survey by design); the benefit and cost denominators
disagreeing (the engine computes benefit on its own groups, and v1's `eligibility_probe` invariants are the model for
a v2 check there; not in this note).

**Tests to write first.** SA5-1 a population column equal to a survey's sample size: refused. SA5-2 census total times
a survey age share: accepted (degree 1 in census, degree 0 in the survey's counts once the share is a ratio of them).
SA5-3 a survey-weighted population estimate: refused. SA5-4 a declared `other` source with a reason: accepted and on the
sheet. SA5-5 lineage on a derivation with a numeric filter: the sheet says the lineage is partial.

**Smoke markers.** `--expect 'data/column_lineage.json:homogeneity'`, `--expect
'decision_spec.signoff_sheet.md~Denominators'`.

## 8. SA6: effect scoping for registered engines

**The failure.** An intervention-specific parameter (a product's efficacy, a campaign's reach) that also changes the
outcomes of arms that do not hold the intervention. The benefit of every package is then wrong, the comparator's
burden too, and the error is invisible in any one arm.

**What exists.** The wiring check (PR #365) moves each declared input to each end of its range and records every
outcome under every package, per unit. It asks whether some output moves. The engine-lane design's A6/A7 test signed
(input, output, sign) pairs for built engines. Neither asks what must **not** move.

**What it computes.** For each spec component `c` and each engine parameter that is a decision input of `c` only
(`decision_inputs.lookup`; a parameter that is also an input of another component or of `*` is shared, and skipped):
from the wiring check's recorded evaluations, the change in every outcome under every package that does not hold `c`
(holds its `absent_values` from `benchmark_coverage.yaml`). No new engine runs for the main check.

Background delivery is the one legitimate exception: an engine can deliver a component in every arm (past or routine
campaigns that every arm carries). The card declares it: `background: {<component>: [<unit attributes that turn it
on>]}`. Units where those attributes are set are excluded from the check, and the harness adds one probe per region:
a copy of a real unit with the background attributes set to absent, run at each end of each scoped parameter, for the
packages without `c`. That is at most two runs per scoped parameter per region.

For a built engine, the same rule is a generated admission case, A6b: a signed `expected_effects` pair gains its
scope from `request.decision_inputs`, and admission tests that the arms without the component do not move.

**Tolerance and source.** The wiring check's own floors: `wiring.rel_tol` for a deterministic engine, the measured
seed noise times `noise_multiplier` for a stochastic engine under CRN. Separately, on the emulator path, the
emulator's answers are scored at `emulator_rel_tol`.

**Findings and routing.**
- `effect_out_of_scope` (HIGH, not declarable): the engine moves an arm without the component. That is an engine
  defect: a registered engine needs an engine fix (outside the run), a built engine fails admission and goes back to
  a build try.
- `emulator_scope_leak` (MEDIUM, declarable): the engine is scoped and the emulator is not (it spreads a parameter's
  effect across packages that share features). The remedy is direct evaluation for those packages, or a declaration
  that states the size.

Costs are scoped by construction: `toolkit/alloc` charges a cost item only to packages that hold its component.

**What it cannot catch.** Indirect effects within one arm (herd protection of non-recipients is legitimate and not
tested here); a parameter the card wrongly lists as shared; background delivery the card does not declare (the
probe would then show a leak, which is the right outcome).

**Tests to write first.** SA6-1 a toy engine whose component-`a` efficacy also scales the comparator's incidence:
`effect_out_of_scope`. SA6-2 the same engine with `a` declared as background through a unit attribute, on a unit
where it is on: excluded; on the probe unit: still flagged. SA6-3 a shared parameter: skipped, listed. SA6-4 a
stochastic toy under CRN: noise below the floor gives no finding. SA6-5 an emulator that leaks while the engine does
not: `emulator_scope_leak` only. SA6-6 no wiring record: a finding (fail closed).

**Smoke markers.** `--expect 'analysis/wiring.yaml:scoping'`, `--expect 'analysis/wiring.yaml:scoping.probes'`.

## 9. What each audit extends

| Existing mechanism | File | Audits that extend it | How |
|---|---|---|---|
| sensitivity wiring (PR #365) | `toolkit/analysis/wiring.py` | SA6, SA2a | SA6 re-reads its evaluations for arms without the component; SA2a reads its rows for decision relevance |
| critique checklist (branch `critique-checklist`) | `toolkit/spec/critique_checklist.py` | SA3a, SA4 | `periods` gains `train` and becomes the split; `burn_in_drift` gains a harness-computed answer (added to `HARNESS_ITEMS`), so critics answer it only when SA4 is `not_offered` |
| identifiability | `scripts/identifiability.py`, `calibration.py` | SA2a | identifiability asks whether data pin a parameter; SA2a asks whether the data push it past its range. Both are reported per parameter in one table |
| decision tests | `toolkit/analysis/decision_tests.py`, `toolkit/spec/perturb.py` | SA1, SA2a-c, SA4 | every declarable finding names the test that answers it; no new test type (`unit_attribute set_from`, `parameter_value`, `calibration_target`) |
| eligibility probe (v1) | `scripts/eligibility_probe.py` | SA5, SA6 | its invariant (c), "the total population scaled leaves the benefit unchanged unless spillover is declared", is the idea SA6 applies across arms and SA5 applies to derivations |
| benchmark runner and coverage | `toolkit/engines/benchmark.py`, `benchmark_coverage.py`, `benchmark_sources.py` | SA1 | the same estimands and bands, scored at run values instead of the pack's |
| spec values benchmark step | `toolkit/spec/engine_parameters.py benchmarks` | SA1 | generalised from interaction arms to every covering trial, at the calibrated point and draws |
| engine-lane admission (branch `design-agent-engines`) | A6, A7, A8 | SA6, SA1 | A6b scope case; A8 re-run at run values after calibration |
| survey changes | `toolkit/calibration/history_matching.py`, `joint_calibration.py` | SA2b | per-pair z plus a cross-group statistic with a predictive reference |
| derivation runner and column accounting | `scripts/pipeline.py`, `v2-derived-inputs.md` §3.5 | SA3b, SA5 | removal test generalised to lineage |
| trajectories and `run_series` | `toolkit/analysis/trajectories.py`, `toolkit/engines/hooks.py` | SA4 | the same hook, run before the batch for the units that can flip |
| evidence dossier and preconditions | `toolkit/spec/evidence_dossier.py`, `preconditions.py` | SA1, SA3b, SA5 | structured estimand fields; two new registered checks |
| pre-sign critics | `toolkit/spec/spec_critique.py` `follow_up_window` | SA1 | computed from the structured window instead of a free number |

## 10. Priority and sizes

Ordered by the decision risk removed per unit of work. Sizes are lines of change including tests; every PR stays under
800 lines and starts with its failing tests. Engine-run costs are on the cloud through `scripts/compute.py`.

| # | PR | Content | Size | Engine runs per run | Depends on |
|---|---|---|---|---|---|
| 1 | SA1 α | structured estimand fields in the dossier; the estimand-field sheet check; the curve integrator | ~400 | none | none |
| 2 | SA1 β | `effects_at_run_values` step before the batch; point and draws; findings | ~600 | covering benchmarks × arms × seeds × (1 + K) | SA1 α |
| 3 | SA6 | scoping over the wiring evaluations; card `background`; probes; A6b text in the engine-lane note | ~350 | ≤ 2 per scoped parameter per region | #365 merged |
| 4 | SA2a | edge and mass flags for every method; decision relevance; `analysis/fit_audit.yaml` | ~300 | none | #365 merged |
| 5 | SA2b | trend statistics and predictive reference in `survey_changes`, and for panel methods | ~300 | none | none |
| 6 | SA3a | `periods.train`; `target_split.py`; sealed holdout; import audit rule | ~650 | holdout predictions only | critique-checklist merged |
| 7 | SA2c | baselines, skill, rank correlation | ~250 | none beyond SA3a | SA3a |
| 8 | lineage | `data/column_lineage.json` with homogeneity facts | ~450 | derivation re-runs (cheap, sandboxed) | none |
| 9 | SA3b | source classes, refutation rules, literal audit, precondition | ~400 | none | lineage |
| 10 | SA5 | denominator rule, population source list, precondition | ~250 | none | lineage |
| 11 | SA4 | card `nominal_states` for three engines; spec `describes`; drift step | ~600 | 1 per unit near a cutoff, plus the sample | none |

Proposed timing, for the owner to set against F6, which keeps priority: items 1-5 after the in-flight wave (wiring,
checklist) lands, proposed lead, from 2026-12-02; items 6-11 from 2026-12-09. Each PR names its smoke markers and is
retro-checked against a stored run directory, as the project's conventions require. `scripts/regression_eval.py`
gains planted-defect cases SA1-3, SA2b-1, SA4-1 and SA6-1; `scripts/gate_fuzz.py` gains malformed versions of each new
record, each of which must give a finding.

## 11. Open questions for the owner

Each has a recommended default; tell us what is missing and we will use your version.

| # | Question | Recommended default |
|---|---|---|
| 1 | SA1: share of draws that must fall inside the trial's interval | 0.5, MEDIUM below it; the point in band is the gate |
| 2 | SA1: run before the outcome batch (blocks paid compute) or after (reports only) | before |
| 3 | SA3a: when the spec is silent, hold out the latest period of each series automatically | yes, shown on the sheet; opting out is a declared departure |
| 4 | SA3a: hide holdout-period rows of staged inputs from the spec drafter | not in the first version; the sheet records that the drafter saw them; revisit with ACQUIRE |
| 5 | SA2c: severity | MEDIUM when the skill is at or below 0; HIGH when its 95% upper bound is below 0 |
| 6 | SA2b: predictive p threshold and minimum groups | 0.05 and 5 groups |
| 7 | SA2a: edge thresholds | point within 1% of the width; mass above 20% within 5% of an end |
| 8 | SA2a: keep the existing HIGH for any likelihood parameter at a bound, decision-relevant or not | keep it; this wave adds checks and loosens none |
| 9 | SA3b: modelled and derived targets | refused unless declared with a reason; derived targets carry the factor's uncertainty in their error |
| 10 | SA5: population source classes | census, civil registration, WorldPop, UN WPP, GPW, LandScan, national statistics projections; others by signed declaration |
| 11 | SA5: cross-check of the units' total against a staged total | a note, not a gate, until we see how often sources disagree |
| 12 | SA4: tolerance for a continuous nominal setting with no stated uncertainty | 0.05 on the share scale |
| 13 | SA6: background delivery | declared on the card; checked on a probe unit with the background removed |
| 14 | SA6: an emulator that leaks while the engine is scoped | MEDIUM, declarable, with direct evaluation as the fix |

## 12. Not in this note

Benefit and cost denominators inside the engine (a v2 version of `eligibility_probe`); data vintage (lessons item 9);
the per-source measurement model in the likelihood (lessons item 8); methods text bound to card mechanisms (lessons
item 11). Each is a separate item in the lessons' ranked list.
