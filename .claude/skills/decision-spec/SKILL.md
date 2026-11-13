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
   polygon blocks sign-off: fix the derivation, the crosswalk or the boundary file. If you
   believe the data are right, propose an exception by name with a reason in
   `decision_units.geometry_accepted`; only the owner's sign-off approves a HIGH one, and the
   run waits for the owner. Signing freezes the boundary declaration and files.
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
12. **Evidence dossier.** Each engine card declares, per component, the unit attributes,
    parameters and card tables its effect depends on (`decision_inputs`, read by
    `toolkit/engines/decision_inputs.py`). When the options use such a component, name the
    run's dossier (`evidence: {dossier: evidence_dossier.yaml}`) and give one entry per input
    (`<kind>:<name>`, format in `toolkit/spec/evidence_dossier.py`): the value the model uses
    and its source; field or trial observations (source, URL or table, year, sites, n,
    sampling frame, value) and how they compare with the model value on the same estimand,
    or `no_data` with the borrowing rule; the chosen value or range with its rule; and, for a
    range, its decision test. Sign-off refuses a missing, unparseable or incomplete dossier
    and a blank field, and lists what is missing. The sheet shows the table and the
    signature binds the file, so an edit after sign-off needs a new sign-off.

## Closed-population epidemic questions (the compartmental engine)

For a question about an epidemic in a closed population (vaccine doses by age group, a catch-up campaign
by birth cohort, an outbreak's final size), the engine is
`toolkit.engines.compartmental.engine:CompartmentalEngine`. Name it with its configuration in the spec
(`engine: {ref, config}`); it is eligible only with a configuration. Read its `card.yaml`,
`ENGINE_NOTES.md` and `literature.yaml`.
- **Structure.** `structure: age_groups_contact_matrix` binds `contact` (a G x G matrix from the data, or
  a published matrix with its source). `birth_cohorts` mixes homogeneously. `groups` are the question's
  own group labels.
- **Run mode.** `mode: to_extinction` with `final_size: true` gives an outcome that is the epidemic's
  final size. `time_unit: year` reads the horizon in decimal years and turns on the figure hooks.
  `windowed` counts infections inside the window.
- **Generation interval.** `generation_interval: {seir: {latent_days, infectious_days}}`. The final size
  does not depend on it; timing does.
- **Immunity.** `initialiser: given` takes the susceptible share per group. Make it a calibrated
  parameter with `per_group_parameters: [s0]`. `cohort_history` takes dose coverage by birth year, and
  `coverage_scale` is a per-unit latent multiplier on a reported coverage series. When you calibrate it, start
  its prior from the card's sourced range (`attribute_binding.yaml`, `coverage_scale.prior`) and copy the
  source. First check the card's `applicability` assumptions against the question (coverage level, declared
  seroconversion, pathogen, setting), and recompute the ends where they differ. A narrower or wider range
  needs its own source. The results list the card's limitation for it: it is a
  simplification standing in for everything that separates reported from effective coverage. Repeat that in the
  report's limitations.
- **Outcomes.** An outcome per infection (`outcomes: [o]`) is a unit attribute `ratio:<o>`, or calibrated
  per group with `per_group_parameters: [ratio:<o>]`.
- **Interventions.**
  - `kind: campaign` (targets, reach) or `kind: doses` (whole blocks to one group: `block_size`,
    `max_blocks`).
  - Protection is all-or-nothing, `efficacy:<component>`, declared in `engine_parameters` with its
    source.
  - `day` acts during the epidemic; the default, 0, acts before it.
- **Joint dose blocks within one unit.** Use one alloc component per (group, level), with `group:` the
  group so that a package takes at most one level per group. Map each to `{<engine component>: level}`
  and give it a fixed cost item. Use `packages: {generate: true, within_envelope: true}`: packages no
  plan can afford are dropped. Use `deliverable.by_group` (columns `{group, quantity, value}`,
  `quantity: {<group>: <cost_outputs name>}`) for one row per group.
- **Calibration to surveys and counts by group.** Use `calibration: {method: posterior, parameters,
  [attribute, range, scale], priors, sources, targets: {kind: observations, ...}}`. The format is in
  `toolkit/analysis/observed_targets.py`.
  - Each target row names its unit, the groups it covers, its source, and its latent quantity:
    `immunity`, `susceptible`, `attack_rate` or `outcome:<o>`.
  - Each source declares a frame, bias mechanisms (or `{none: reason}`) and a noise model: binomial or
    beta-binomial for shares, Poisson or negative binomial for counts.
  - Values the question gives (an efficacy, a seroconversion share, a reach) go in `engine_parameters`, each with
    `value`, `source`, `reason` and `basis: setting_data` (every field is required).
    Omit `units`, or copy the card's units string exactly. Give no `range` when the question
    says the value is known. With a `posterior` calibration, a drawn prior on a parameter it does not
    calibrate is refused, because the fit does not condition on it. Either calibrate the parameter (if the
    card marks it calibrated) or hold it at its value.
  - A per-unit attribute needs `scale` (`linear`, or `log` where the attribute is exp(z)); its `range` and prior
    are on that scale.
  - A parameter the data do not inform, such as a declared R0 range, may be calibrated with that range
    as its prior. It is then classed prior-dominated and gets its prior_range test.

## When the question asks for quantities, not an allocation

Some questions ask for estimates with intervals (a reproduction number, an attack rate) and a yes/no statement,
with nothing to allocate. Write `deliverable.kind: estimates`. The sections are in the docstring of
`toolkit/spec/estimates.py`, and `estimates.toy_spec()` is a valid example. Leave out `options`, `constraints`,
`comparator` and `objective`: they are refused.

- **One population.** The units table has one row (derived like any other), with the population column bound to the
  engine's population attribute.
- **Dynamics.** `dynamics.generation_interval` cites the question's file and its columns, and copies its values, lag 1
  first. The harness checks the values against the file. `dynamics.transmissibility` is `constant`, or
  `{knots: [...]}` when the question allows the transmissibility to change.
- **Observation sources.** Each data set is a source with a frame and a measurement model: a reporting fraction is
  an `under_ascertainment` bias with a prior and a source, never an engine parameter. A reporting delay is
  `delay: {pmf, source}` on the engine's step grid. Overdispersion is `noise: {model: negbin, dispersion: {param,
  prior, source}}`. No source is the truth.
- **Calibration** is `posterior`. `calibration.priors` gives every engine parameter the card marks `calibrated`, and
  every unit attribute the data do not give (for example the susceptible share at the start, when there is no
  serosurvey). Each has a prior and a source. Take ranges from the engine's `ranges.yaml` or from the question, and
  keep their stated spread. A quantity the question does not give is a prior over its plausible range, never a
  midpoint. `calibration.targets` names the series file, its source, its step column (1 is the first step of the
  horizon) and its count column.
- **Names.** Each observation parameter (a bias parameter, a dispersion) has its own name. It must not be the name of a
  calibration prior, an engine parameter or an engine unit attribute. Frame exclusions are not supported for a count
  series. A reporting fraction's prior lies within [0, 1]. The engine must accept the corners of the prior box, taken
  at each prior's support ends. The validator runs the engine there; above 64 corners it samples them, and the
  analysis still stops on any refusal. For example, keep the susceptible share's lower end above the seed's upper end.
- **Uncertainty.** List `calibration` and `observation` as sources. Exclusions and declared decision tests are
  refused: the posterior carries every declared prior, and the automatic tests are run.
- **Outputs.** `deliverable.windows` names the windows. Each output in `deliverable.outputs` is an engine window
  metric (read its card) with the name the question asks for. Check which reproduction number the question means.
  For example, `reff_ref` is the end of the window with the transmissibility of its start; `reff_end` uses the last
  step's. A yes/no question is `deliverable.decision` (a threshold rule with three descriptive statements). The
  harness computes the probability and picks the statement.
- **What the data do and do not determine** is computed, not written. Each undetermined parameter gets an automatic
  test at both ends of its declared range, and each output is classed `determined_by_data` or
  `set_by_declared_ranges`.

## What not to do

- Do not type a result, an effect size from the data, or an interval into the spec: the
  spec holds inputs and choices; the harness computes the rest.
- Do not leave an owner decision implicit (a group rule, a reserve, a cost item left out).
  If the question does not settle it, write the choice and flag it for the owner at
  sign-off.
- Do not mark a parameter `calibrated` that the engine card fixes or fits from its
  benchmark pack.
