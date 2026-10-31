---
name: decision-spec
description: >-
  Drafting decision_spec.yaml (architecture v2): the one owner-signed file that drives
  the harness analysis engine (toolkit/analysis). Use when planning a question that the
  analysis engine will answer: decision units, packages, objective, constraints
  (envelope, caps, coverage, eligibility, group rules), comparator, calibration targets,
  uncertainty sources declared up front, decision tests and the deliverable. Trigger
  phrases include "decision spec", "decision_spec.yaml", "sign the spec", "analysis
  engine", "uncertainty sources", "group rule", "decision test".
---

# Drafting the decision spec

The planner drafts `decision_spec.yaml` in the run directory; the owner signs it
(`python3 toolkit/spec/signoff.py sign <run_dir> --by owner`, or with the brief through
`scripts/decision_brief.py sign`, which freezes the spec with the brief). After that,
every number in the report comes from harness code reading this file. You do not write
calibration, emulation, uncertainty, optimisation, robustness, sensitivity, decision-test,
figure or claims code; you configure them here.

The schema, with every field, is the docstring of `toolkit/spec/schema.py`. Check your
draft with `python3 toolkit/spec/schema.py <run_dir>/decision_spec.yaml` until it prints
`spec valid`. A self-contained valid example is `schema.toy_spec()`; a full one is
`spike/decision_spec.v2.yaml`.

## Order of work

1. **Question and decision.** Copy the question. Name the decision maker by role and the
   decision date.
2. **Engine.** Pick an engine whose card (`describe()`) covers the interventions, outputs
   and unit attributes the question needs. Read its card: components and their values,
   unit attributes and ranges, outputs, age groups, fitted parameters and benchmarks. If no
   engine covers the question, say so; do not bend the question to an engine. Or write
   `engine: auto` (optionally `{auto: true, candidates: [...]}`): the engine is then chosen
   after sign-off by the harness, which records it in `engine_choice.json`; the decision
   sections are checked at sign-off and the engine-dependent ones once the choice exists.
3. **Decision units.** The units table (one row per unit), its id column, the mapping of
   each engine unit attribute to a column (or a value with its source), and the population
   groups (unit column and engine age groups).
4. **Options.** Components, packages (`generate: true` for every combination with at most
   one per group, or the exact list the question offers, with its names), the engine map
   (component to engine component value), prices and cost items in toolkit/alloc form
   (the `allocation-toolkit` skill covers cost bases and the double-count rules). Use the
   package names the question uses: the deliverable is read against them.
5. **Objective.** The outcome (an engine per-capita output), the decision window, and
   discounting (`none` or a rate and base year).
6. **Constraints.** The envelope and any fixed costs outside the allocation; eligibility
   rules; caps; coverage limits; locks and other funders' footprints. **Group rules**: if a
   decision is taken for a group of units at once (a campaign run state by state, one
   procurement per region), write it as a group rule (`by:` the grouping column). An
   unwritten group rule is an owner decision taken silently (spike lesson 10).
7. **Comparator.** The package that represents what happens without the plan, and the
   text that says so.
8. **Calibration.** `emulator_inversion` (an attribute per unit to a target column, for a
   slow engine), `likelihood` (calibrated engine parameters and a per-unit attribute fitted
   to a panel of observed counts by unit, period and package), or `none`. Only parameters
   the engine card marks `calibrated` may be fitted per run.
9. **Uncertainty: declare the sources up front.** Every kind in
   `schema.UNCERTAINTY_KINDS` is either a source or excluded with a reason. The report's
   interval label is rendered from this list ("95% UI over calibration; not covered: unit
   costs, ..."). An interval that covers two sources is not a full 95% UI; say what it
   leaves out (spike lesson 6).
10. **Analysis.** `evaluation: emulator` for engines too slow to run per unit and draw (give
    the design: points, hold-out share, features), `direct` otherwise; the stability
    hold-out group; sensitivity; and **decision tests** for each simplification,
    decision-layer feature or known engine limit the owner should see tested (an
    interaction benchmark the engine misses, a group rule, the envelope). Give each a
    `finding:` id when it answers a gate finding.
11. **Deliverable.** The allocation file and its column names, and the headline output name
    the question asks for.

## What not to do

- Do not type a result, an effect size from the data, or an interval into the spec: the
  spec holds inputs and choices; the harness computes the rest.
- Do not leave an owner decision implicit (a group rule, a reserve, a cost item left out).
  If the question does not settle it, write the choice and flag it for the owner at
  sign-off.
- Do not mark a parameter `calibrated` that the engine card fixes or fits from its
  benchmark pack.
