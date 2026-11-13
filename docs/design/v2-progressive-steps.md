# Progressive steps: answer the simple question first, then build up

**Status:** design, 2026-11-12, owner-approved direction ("make the algorithm learn
progressively: try a one-node simulation, study how the answer changes across settings, then
build up"). Not implemented beyond step 0 (PR `f6-step0-baseline`) and the two preconditions
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

## 4. Changes to the harness

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

## 5. What this means for F6

P6 (the full preview) becomes steps 2 and 3. Order:

1. Step 0 on the F6 inputs, locally, no cloud spend (PR `f6-step0-baseline`).
2. The evidence dossier for F6: species and resistance (done, local), burden, prices, trial
   effects, SMC and case-management inputs. The owner signs it with the amended spec.
3. Step 1: the net-effect ranges fitted to the trials until B7 and B8 pass, or an owner-signed
   departure (PR `f6-net-effect-ranges`; spend of $20 or more goes to the owner first).
4. Steps 2 and 3 on the cloud, with the feature-box design once built; until then, the current
   design with the widened ranges.

## 6. Risks and open questions

- **Combination rules at step 0.** Trials measure components mostly alone; step 0 must declare how
  components combine. Step 3 then tests that rule, which is one of the engine's real jobs.
- **Prevalence versus incidence.** Several net trials report prevalence odds ratios; step 0 needs a
  declared conversion or a range, and the dossier records which.
- **Box size.** Too wide a feature box wastes points outside any real unit; too narrow repeats the
  canary's extrapolation. The box comes from the dossier's ranges plus the real units' hull.
- **Who decides a step is not needed.** Proposed: the harness decides from the exit tests; the
  owner can require a step in the spec.
- **v1.** Unchanged; this applies to v2 only.
