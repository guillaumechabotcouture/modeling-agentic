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
   groups (unit column and engine age groups). Every unit attribute on the engine card is
   bound, moved by the calibration, or, when the card gives it a default (the engine's
   `attribute_binding.yaml`), accepted by name with a reason in
   `decision_units.defaults_accepted: {attribute: reason}`. The sign-off sheet shows the owner
   each accepted default with what it assumes. An attribute left unbound is refused
   (`engine_attribute_unbound`): an engine never falls back to a default silently.

   **Derived data, not tables.** In a v2 run you do not write the units table or any other
   table the spec reads. Write a script per derived file under `data/derive/` and declare it
   in `data/derivations.yaml` (the format is in `scripts/pipeline.py`, section
   "derivations"): its staged inputs, its outputs under `data/derived/`, and the column
   accounting (every column of every staged table it reads is in `columns.uses`, or in
   `columns.ignores` with a reason; a staged table no derivation reads goes in
   `ignored_inputs` with a reason). The harness runs each script as
   `python <script> --inputs <paths> --outputs <paths>` in a sandbox: no network, read-only
   inputs, the standard library, numpy, pandas and pyyaml only. It runs it twice (the
   outputs must match) and once more without the ignored columns (the output must not
   change). It records the hashes of the script, inputs and outputs in
   `data/derivations_record.json`. The sign-off freezes that record, so a later change to a
   derived file needs a new sign-off. A failed script comes back to you with its error.
   Codes: `units_not_derived`, `data_ref_not_derived`, `derivation_stale`, `derivation_failed`,
   `derivation_nondeterministic`, `input_column_unaccounted`, `ignored_column_used`.
   When the units table has coordinates (`decision_units.coordinates: {lat, lon, area_km2}`)
   and the run's presentation file declares boundaries, the harness checks each unit's point
   and area against its polygon (`toolkit/spec/geometry.py`, `data/input_checks.json`). A
   `reciprocal_swap`, a point far outside its own polygon, or an area that matches another
   polygon blocks sign-off: fix the derivation, the crosswalk or the boundary file. Only the
   owner accepts a finding by name (`decision_units.geometry_accepted`).
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
   the engine card marks `calibrated` may be fitted per run. Declare the target's frame:
   `calibration.target_frame: {band, source}` (for a panel, `calibration.targets.frame`), the
   population band the target measures. When that band differs from the engine output it is
   compared with, declare `mapping: {to: <output>, method, source}`; otherwise the target is
   refused (`target_frame_unmapped`). No source is the truth (v2d §3.4).
9. **Uncertainty: declare the sources up front.** Every kind in
   `schema.UNCERTAINTY_KINDS` is either a source or excluded with a reason. The report's
   interval label is rendered from this list ("95% UI over calibration; not covered: unit
   costs, ..."). An interval that covers two sources is not a full 95% UI; say what it
   leaves out (spike lesson 6). Engine parameters with published ranges (the engine's
   `priors.yaml`) are drawn in every draw without being listed, and the label names them.
   A scenario parameter (an achieved coverage, an added treatment rate) is held at its
   value unless you give it a published range in `uncertainty.priors: {name: {dist,
   range, source}}`; the label says it is held fixed. Fix a prior parameter only with
   `uncertainty.excluded: [{parameter, reason}]`. Each drawn prior gets an automatic
   `prior_range` decision test; do not declare one. A prior targets an engine parameter only
   (`prior_target_not_parameter`). A unit attribute bound to a data column is data: if the
   column is uncertain, declare that where it arises, as the observation of a latent quantity
   through a source with a sampling frame and a bias model.
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
