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

## 3. The steps

| Step | Question | Computation | Exit test | Artifact |
|---|---|---|---|---|
| 0. Arithmetic | What plan does external burden × trial effect ÷ current price give, and which term drives it? | Package-rate engine in trial-prior mode; no simulation | Plan, drivers and robustness recorded | `analysis/step0/` (a full analysis run with the package-rate engine) |
| 1. One node, trial settings | Does the engine reproduce the trials for every component the options use? | The engine's benchmark pack; the trial fit of effect ranges | Every benchmark covering a used component in band, or an owner-signed departure (eligibility precondition) | Engine benchmark record for the version used |
| 2. One node, setting sweep | How does each package's relative effect vary with the setting, and where does the winning package flip? | A designed sweep over a feature box spanning the evidence dossier's ranges, independent of real units; emulator; sensitivity; value of information | Emulator accurate near flip boundaries (held-out error below the gap between competing packages); the inputs that move the choice named | `analysis/sweep.yaml`, emulator, VOI |
| 3. Apply to units | Given each unit's external burden and features, what is the plan, and how robust? | Emulator lookup; burden anchor; optimisation over uncertainty draws; stability; decision tests | Plan compared with step 0; each difference explained by a named mechanism or flagged | The current analysis artifacts plus `analysis/step_comparison.yaml` |
| 4. Space, only if needed | Do coupling, importation or campaign timing between units change the plan? | A multi-node model | Taken only when step 3 shows unit independence is the weak point (a critic or decision test names it) | Declared in the spec when taken |

What each step passes forward:

- Step 0 gives the reference plan and the terms that drive it, which tells step 2 where to look.
- Step 1 gives fitted effect ranges, which become step 2's priors.
- Step 2 gives the flip boundaries and the inputs that matter. Step 3 needs accuracy only there;
  inputs with no value of information can stay at point values, with the omission declared.
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
6. **Stop.** When no input has value of information above a threshold, when the remaining
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
3. **Design over a feature box.** `batch.design()` gains a mode that samples unit features
   (transmission level, seasonality, resistance, species shares, coverage) over a box spanning
   the dossier's ranges, instead of copying real units. Real units are then always inside the
   domain. The sweep is reusable across runs that share an engine version and box, which is how the
   model library (`model-library.md`) accumulates.
4. **Sequential refinement.** After the first sweep, further points are placed where the
   emulator's uncertainty could flip a package choice, not uniformly.
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

## 7. What this means for F6

P6 (the full preview) becomes steps 2 and 3. Order:

1. Step 0 on the F6 inputs, locally, no cloud spend (PR `f6-step0-baseline`).
2. The evidence dossier for F6: species and resistance (done, local), burden, prices, trial
   effects, SMC and case-management inputs. The owner signs it with the amended spec.
3. Step 1: the net-effect ranges fitted to the trials until B7 and B8 pass, or an owner-signed
   departure (PR `f6-net-effect-ranges`; spend of $20 or more goes to the owner first).
4. Steps 2 and 3 on the cloud, with the feature-box design once built; until then, the current
   design with the widened ranges.

## 8. Risks and open questions

- **Combination rules at step 0.** Trials measure components mostly alone; step 0 must declare how
  components combine. Step 3 then tests that rule, which is one of the engine's real jobs.
- **Prevalence versus incidence.** Several net trials report prevalence odds ratios; step 0 needs a
  declared conversion or a range, and the dossier records which.
- **Box size.** Too wide a feature box wastes points outside any real unit; too narrow repeats the
  canary's extrapolation. The box comes from the dossier's ranges plus the real units' hull.
- **Who decides a step is not needed.** Proposed: the harness decides from the exit tests; the
  owner can require a step in the spec.
- **Mostly right, or confidently wrong.** A world model can feel right and mislead. Its track record
  is kept and shown; beliefs with low confidence or a poor record are labelled, and a falling
  record triggers a rethink of the world model rather than more simulation.
- **Hypotheses in an LLM's words.** Proposals may come from a bounded task, but each must map to a
  test the harness can compute; a hypothesis with no computable test is recorded as untestable.
- **v1.** Unchanged; this applies to v2 only.
