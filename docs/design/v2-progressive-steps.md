# Progressive steps: answer the simple question first, then build up

**Status:** design, 2026-11-12, owner-approved direction ("make the algorithm learn
progressively: try a one-node simulation, study how the answer changes across settings, then
build up"). The world model and the learning loop (§4, §5) were added the same day at the owner's request
("a compact representation, with numbers or branching points that come from simulation work").
Not implemented beyond step 0 (PR `f6-step0-baseline`) and the two preconditions
(PRs `f6-evidence-dossier`, `f6-benchmark-eligibility`).

## 1. Why

What the Nigeria F-series runs and the 12 Nov canary showed:

- **The decision turned on a few inputs, and we checked them last.** The plan depends on relative
  burden between units, the relative effect of each option and its price. The inputs behind those
  were wrong or out of date: a state's vector species share (model 83% *An. arabiensis*, field
  surveys 3%), northern pyrethroid resistance (model bioassay mortality about 0.73, field about 0.13),
  2023 net prices, and absolute burden from the engine. We found them after paid runs, through
  critics and manual checks. No step required them to be checked first.
- **The engine failed its most basic trial benchmark and was still used.** Pyrethroid nets against
  no nets: prevalence ratio 0.50 against the Cochrane estimate 0.83 (B7), incidence 0.41 against
  0.55 (B8).
- **The "large" simulation is many independent single-node simulations.** The malaria engine models
  each unit as one node with a fixed importation rate; there is no coupling between units. The
  emulator design copies one real unit's features per point (`toolkit/analysis/batch.py`
  `design()`), so the engine only learns the parts of setting space where real units sit; with few
  points, units fall outside it (the 32-point canary left 61 of 309 outside the emulator's range).
- **The data constrain little, and unit-level choices are not robust.** In F5, history matching
  narrowed the shared parameters by under 0.5%. Total value was robust, but only 9 of 309 units kept
  their package in 90% of draws.

A modeller would have answered simpler questions first: does arithmetic already give the answer,
does the engine reproduce the trials, how does the answer change across settings, and only then
what is the plan for each unit. This design makes that order the pipeline's.

## 2. Principle

Each step answers one question with the cheapest computation that can answer it, writes a small
artifact with an exit test, and passes forward what it learned. A step is taken only when the
previous one cannot settle the decision. Cost grows with the question, not with the number of
units. This applies `decision_design_principles.md` principle 0 (first principles, throughout)
and principle 7 (granularity the evidence supports).

**Scope.** The steps apply to allocation deliverables. Estimates-only runs (`deliverable.kind:
estimates`, which have no options, comparator or objective) keep their existing route.

**Two kinds of test per step.** A *validity* test says whether the step's result can be trusted
(the engine matches its benchmarks; the emulator is accurate enough). A *sufficiency* test says
whether the decision is settled or the next step is needed. A step is skipped only when the
previous step is valid *and* sufficient; a valid step whose plan changes across plausible inputs
is not sufficient. Unresolved findings carry forward to the next step.

Transitions: an *invalid* result goes to the owner or to SPEC (the inputs or the engine are wrong);
a *valid but insufficient* result goes to the next step; a *valid and sufficient* result may end
the progression. The deliverable always comes from a valid, sufficient step that produces a plan
per unit, that is step 0 or step 3; a sweep (step 2) cannot end the progression. Sufficiency
always requires the plan to be feasible under the spec's declared affordability rule in the
draws, not only to have low regret.

## 3. The steps

| Step | Question | Computation | Exit test | Artifact |
|---|---|---|---|---|
| 0. Arithmetic | What plan does external burden × trial effect ÷ current price give, and which term drives it? | Package-rate engine in trial-prior mode; no simulation | Validity: every input sourced, and each burden and effect matched on comparator, outcome, population and window (principle 3); the combination rule is uncertain in the draws (from independent action to substantial overlap) unless evidence supports one rule. Sufficiency: the plan holds across the decision tests (regret against the per-draw optimum under a declared tolerance) | `analysis/step0/` (a full analysis run with the package-rate engine) |
| 1. One node, trial settings | Does the engine reproduce the trials for every component the options use? | The engine's benchmark pack; the trial fit of effect ranges | Validity: every held-out benchmark covering a used component in band at the run's effective configuration (the benchmarks at spec values, including overrides), or an owner-signed departure (eligibility precondition). Effect ranges are fitted only to the pack's training targets; held-out benchmarks stay validation | Engine benchmark record for the version used |
| 2. One node, setting sweep | How does each package's relative effect vary with the setting, and where does the winning package flip? | A designed sweep over a feature box spanning the evidence dossier's ranges, independent of real units; emulator; sensitivity; value of information | Validity: emulator error small enough that the plan's regret from emulator error is under a declared tolerance, judged on feasible allocations under the shared budget, not only within-unit flips; near-ties within an indifference tolerance are reported as ties (as `boundary.py` does). Sufficiency: the inputs that move the choice named | `analysis/sweep.yaml`, emulator, VOI |
| 3. Apply to units | Given each unit's external burden and features, what is the plan, and how robust? | Emulator lookup; burden anchor; optimisation over uncertainty draws; stability; decision tests | Plan compared with step 0; each difference explained by a named mechanism or flagged | The current analysis artifacts plus `analysis/step_comparison.yaml` |
| 4. Space, only if needed | Do coupling, importation or campaign timing between units change the plan? | A multi-node model | Triggered by evidence, not by step 3's own tests, which assume independence: a coupling stress test (importation varied with neighbours' coverage) that changes the plan, or evidence of strong connectivity between units. Needs a new engine contract first: with transmission between units, one unit's benefit depends on its neighbours' packages, so benefits are no longer a per-unit table and the optimiser must evaluate whole plans | Declared in the spec when taken |

What each step passes forward:

- Step 0 gives the reference plan and the terms that drive it, which tells step 2 where to look.
- Step 1 gives a joint sample of trial-compatible effect parameters fitted to training targets,
  which becomes step 2's prior. Marginal ranges are not enough: the trials can admit a narrow
  trade-off (for example efficacy against duration) that independent draws would break. A failing
  held-out benchmark is fixed by finding a mechanism or setup error that has its own evidence,
  then re-checked; a held-out benchmark is never used to choose parameter values
  (`toolkit/spec/engine_parameters.py` refuses that). Moving a benchmark from held-out to training
  needs a replacement validation set and the owner's sign-off.
- Step 2 gives the flip boundaries and the inputs that matter. An input with low value of
  information alone is still a candidate for research when it belongs to a group of inputs whose
  joint value is high; in all cases its uncertainty stays in the draws.
- Step 3 gives the plan and its comparison with step 0. When the two agree, the report says the
  simulation added little for this decision.

## 4. The world model: the modeller's working understanding

A good modeller carries a model of the world in their head: simpler than any simulation, and not
meant to be precise, only mostly right. They build it from reading papers, analysing data and
running simulations. They *run* it in their head to get expectations ("in the north, with this
resistance, PBO nets should beat dual-AI on cost") and rough priors ("SMC should avert somewhere
between a third and two thirds of cases in children it reaches"). They then confront it with
papers, data and simulations, and refine it where it was wrong.

The harness makes this explicit as the **world model**: a short, readable file of beliefs about
how the decision works, which every step reads and refines. Precision is not its job; the
simulation and the analysis provide the numbers. Its job is to generate the right hypotheses
and sensible priors, and to say where it is unsure.

**What it holds** (`analysis/world_model.yaml`):

- **Mechanisms and their direction.** What causes what, and which way: "PBO restores net killing
  where pyrethroid resistance is high; the advantage fades as the synergist decays".
- **Rough magnitudes.** Bands, not point values: "about half", "small (under 10%)", "a factor of 2
  to 5".
- **Conditions and branching points.** Where the answer changes, roughly located: "dual-AI is
  preferred where resistance is strong, somewhere below 20–40% bioassay mortality, at today's
  price ratio".
- **Each belief's basis and confidence.** Its source type (literature, data, simulation), the
  evidence (citation and table, dossier entry, or the simulation artifact), how confident we are,
  and any conflicting evidence kept side by side, never averaged.
- **Open questions.** What is unknown and would change the choice if known.
- **Track record.** How often its expectations have been confirmed when confronted. This is how
  "mostly right" is measured, and it falls when the world model needs rethinking.

**Running it.** For a setting or a question, the world model is read through to produce:

- **Hypotheses**: testable expectations, each with what would refute it ("the plan puts
  dual-AI nets in the northern states and PBO nets in the south"; refuted if a northern state
  gets PBO in most draws).
- **Priors**: ranges for inputs and effects, which go into the evidence dossier as proposals and
  into the signed spec only after the dossier's checks and the owner's sign-off.
- **Where to look**: which simulation runs or which evidence would most change a belief that
  matters to the decision.

A bounded reasoning task can run it, because this is judgement, not arithmetic. What it produces
must be testable: each hypothesis names the result that would refute it, so the harness can
check it mechanically.

**Confronting it.** After a paper is read, a dataset analysed or a simulation step run, its
hypotheses are compared with what was found. Confirmed beliefs gain confidence. A contradiction
is the most informative event in a run: either the world model is missing a mechanism, or the
source or the simulation is wrong. The contradiction is recorded, its likely cause stated as a new
hypothesis, and the belief revised once the cause is tested. It is never resolved by adjusting
the simulation's inputs until they agree.

**It exists before any simulation.** Version 0 comes from literature, data and step 0's
arithmetic, and states what the modeller expects the engine to show. A world model from one run is
the starting point for the next run on a similar question.

**What it is not.** It is not a quantitative surrogate of the engine (the emulator does that),
and its numbers are never decision numbers: the report's figures come from the analysis' claims.
The report may use the world model to explain the result in plain terms, citing which beliefs the
analysis confirmed.

## 5. The learning loop

After each step, the harness runs the same loop on the world model:

1. **State hypotheses.** Each one is an edit to the world model: a term's form, a branching point
   or a rule, with what would refute it and the test. Example: "in the north, the choice of net
   type is driven by resistance and flips below about 0.3 bioassay mortality". Hypotheses are
   recorded in `analysis/hypotheses.yaml` with their status (open, supported, refuted, untestable
   with available data).
2. **Rank inputs by value of information.** The inputs that both move the decision and have a weak
   dossier entry come first. Example: northern resistance, where every value comes from one
   irrigated-rice site.
3. **Look for data.** A bounded evidence task searches for better sources for those inputs only,
   with every value cited. When the data exist but are not public, it says so and the request goes
   to the owner.
4. **Test.** Each hypothesis gets a decision test or a targeted sweep. Only the steps an input
   touches are re-run, using the incremental rebuild (`model-library.md` §5.4).
5. **Update the world model.** Supported hypotheses change it; refuted ones are kept with their
   evidence, so the next run does not repeat them.
6. **Stop.** When neither any single input nor any declared group of inputs, nor all of them
   together (the expected value of perfect information), has value above a threshold, since
   inputs can matter only jointly; when the remaining
   uncertainty cannot be reduced with available data, or when the budget is reached. The stopping
   reason is recorded.

**The owner's role.** New data changes the signed dossier. The loop proposes the changes and
pauses; the owner re-signs. The loop never edits a signed input itself, so it cannot tune
inputs toward an answer (principle 4).

## 6. Changes to the harness

1. **Stage machine.** Between ENGINE_CHOICE and ANALYZE, states `STEP0`, `TRIALS` (eligibility
   and benchmark record), `SWEEP`. Each writes its artifact and evaluates its exit test in code. A
   failed exit test routes to the owner or to SPEC, never to a fix task that edits inputs until the
   test passes (principle 4).
2. **Preconditions, not post-hoc gates.** Entry to the steps requires the signed evidence dossier
   (sign-off refuses a spec whose dossier does not cover every `decision_inputs` entry of the used
   components). TRIALS is the eligibility check on the engine's benchmark record.
3. **Design over a feature space.** `batch.design()` gains a mode that samples unit features
   (transmission level, seasonality, resistance, species shares, coverage) over the dossier's
   ranges, instead of copying real units. Three things go with it:
   - each synthetic point carries its own attributes into the emulator, which today rebuilds
     features from the real units table by `unit_id` (`toolkit/analysis/model.py`);
   - constrained sampling where inputs are joint: species shares on the simplex, seasonal profiles
     as valid profiles; the effective attributes the engine used are recorded, since the engine
     renormalises shares;
   - the emulator's domain is the declared space, the design includes its boundaries, and the
     harness checks that every real unit lies inside the sampled hull before step 3.
   A sweep is reused only when the full job identity matches (`batch.job_sha()`,
   `engine_identity()`: code, parameters, configuration, intervention history, delivery dates and
   windows), never on engine version and range alone.
4. **Sequential refinement.** After the first sweep, further points go where emulator uncertainty
   could change the allocation (its regret under the shared budget), not uniformly. Refinement
   stops at the indifference tolerance or the point budget; unresolved boundaries are reported.
5. **Step comparison.** A harness artifact compares step 3's plan with step 0's: mix, cost, value,
   and the units and packages that differ, with the driver of each difference. It is cited in the
   report.
6. **World model and hypotheses.** Two run artifacts, `analysis/world_model.yaml` (beliefs) and
   `analysis/hypotheses.yaml` (each with its refutation test and status). A bounded reasoning task
   runs the world model and proposes hypotheses, priors and where to look; literature beliefs
   carry a citation and location the harness checks. Harness code checks each hypothesis against
   results mechanically, keeps the track record, and summarises simulation results coarsely
   (direction, rough size, where a choice flips) for the confrontation. A stage `LEARN` after each
   step runs the loop of §5 and routes to the owner when it proposes dossier changes.

7. **Unit-attribute uncertainty.** Today the uncertainty draws vary engine parameters,
   calibration, costs and emulator error, but each unit's features (resistance, species shares,
   seasonality) stay at their point values: `spec.priors` refuses priors on bound unit attributes
   and the emulator caches unit features. So a range found in the dossier would be explored in
   the sweep but never in the allocation draws, and the plan would look robust to it. Needed: a
   contract for drawing unit attributes (per unit or as shared shifts by group, with their
   dependence), propagated through calibration and emulator prediction. Until it exists, such
   ranges enter only as decision tests and the omission is declared.
8. **Burden-dependent costs.** When burden is anchored to external estimates, cost drivers that
   scale with cases (treatment volume) scale with it, through a signed mapping (PR #210);
   population-based delivery quantities do not.

## 7. What this means for F6

P6 (the full preview) becomes steps 2 and 3. Order:

1. An exploratory step 0 on the F6 inputs, locally, no cloud spend (PR `f6-step0-baseline`).
   It is unsigned and labelled exploratory: it can inform the dossier but cannot satisfy any exit
   test.
2. The evidence dossier for F6: species and resistance (done, local), burden, prices, trial
   effects, SMC and case-management inputs. The owner signs it with the amended spec, and step 0
   is re-run on the signed inputs as the reference.
3. Step 1: the net-effect ranges fitted to the pack's training targets; B7 and B8 (held out)
   then checked as validation. If they still fail, the cause is investigated as a mechanism or
   setup error with its own evidence, or the owner signs a departure (PR `f6-net-effect-ranges`;
   spend of $20 or more goes to the owner first).
4. Steps 2 and 3 on the cloud, with the feature-box design once built; until then, the current
   design with the widened ranges.

## 8. Risks and open questions

- **Combination rules at step 0.** Trials measure components mostly alone; step 0 must declare how
  components combine and carry that rule's uncertainty, since step 0 can end the progression.
  Step 3 tests the rule when it runs, which is one of the engine's real jobs.
- **Prevalence versus incidence.** Several net trials report prevalence odds ratios; step 0 needs a
  declared conversion or a range, and the dossier records which.
- **Space size.** Too wide a feature space wastes points outside any real unit; too narrow repeats
  the canary's extrapolation. It comes from the dossier's ranges plus the real units' hull.
- **Thresholds from simulation are conditional.** A flip point found with one set of fixed inputs
  can move when a plausible input changes. Flip ranges come from the uncertainty draws jointly,
  not from refitting the same sweep, and they widen rather than narrow as conflicting evidence
  is added.
- **Who decides a step is not needed.** Proposed: the harness decides from the exit tests; the
  owner can require a step in the spec.
- **Mostly right, or confidently wrong.** A world model can feel right and mislead. Its track record
  is kept and shown; beliefs with low confidence or a poor record are labelled, and a falling
  record triggers a rethink of the world model rather than more simulation.
- **Hypotheses in an LLM's words.** Proposals may come from a bounded task, but each must map to a
  test the harness can compute; a hypothesis with no computable test is recorded as untestable.
- **v1.** Unchanged; this applies to v2 only.
