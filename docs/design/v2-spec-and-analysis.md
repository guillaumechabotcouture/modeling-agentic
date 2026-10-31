# v2 workstream B: the decision spec and the analysis engine

Status: design note for owner review (2026-10-31), written before the code. Scope is
architecture-v2 §3.1 (decision spec) and §3.4 (analysis engine) as harness code. It
promotes `spike/analysis/` (920 lines, run once on the 13-state question) into
`toolkit/spec/` and `toolkit/analysis/`, and adds the spike's lessons 5–8 and 10.

## Evidence this starts from

- The spike's chain ran from harness code end to end. Re-running
  `spike/analysis/run.py` on the stored batch (8,192 engine runs) reproduces
  `spike/out/analysis.json` and `allocation.csv` exactly (only timings differ), so the
  stored batch is a fixed reference for the port.
- Lesson 5: rank r and package concordance flagged near-ties as instability. The fold
  plans lost at most 0.73% of DALYs.
- Lesson 6: the interval covered two sources and was labelled "95% UI".
- Lesson 7: the process pool broke at about 750 runs per VM; a fresh pool per chunk
  fixed it.
- Lesson 8: DT1 could not move a plan with no IRS in it, and said "no change".
- Lesson 10: a state-wide campaign rule (attempt 8) changed the mix (30 LGAs with no
  package in the spike); it was not in the spec.
- The spike code names `treated_all`, `eir_annual` and "EIR" in harness code. v2 harness
  code must not.

## 1. The decision spec (`toolkit/spec/schema.py`, `schema_version: 1`)

One YAML file, `decision_spec.yaml` in the run directory. Typed sections:

| Section | Content | Checked against |
|---|---|---|
| `question`, `decision_maker`, `decision_date` | text; date is a year or ISO date | non-empty |
| `engine` | `{ref: module:Class, config: {...}}` | imports, implements the contract |
| `decision_units` | units table, id, label, region, hold-out group, engine attribute mapping, populations (unit column to engine age groups) | engine card attributes and age groups; table columns |
| `options` | toolkit/alloc `components`, `packages`, `prices`, `cost_items`, `engine_map` | toolkit/alloc cost validation; engine component values |
| `objective` | `outcome`, `sense: avert`, `horizon: [t0, t1]`, `discounting: {rate, base_year}` | outcome is an engine per-capita output; horizon inside the engine horizon |
| `constraints` | `envelope_usd`, `fixed_costs_usd`, `caps`, `coverage`, `eligibility`, `locks`, `footprints`, `group_rules` | toolkit/alloc package validation and feasibility |
| `comparator` | a package name and its text | a package of the spec |
| `outcomes_reported` | engine outputs | engine card |
| `calibration` | method (`emulator_inversion`, `likelihood`, `none`) and its targets | columns, engine attributes and parameters |
| `uncertainty` | `sources` (declared up front) and `excluded` (with a reason each), draws, seed | every known source kind is either included or excluded |
| `analysis` | evaluation (`emulator` or `direct`), emulator design, stability, sensitivity, decision tests | parameter names, components |
| `deliverable` | kind, files, column names, headline output name | |

Two structural choices:
- **Constraints live in `constraints`, not in `options`.** `schema.alloc_spec(spec)`
  builds the toolkit/alloc spec by merging them, so there is one place to read
  eligibility and caps.
- **Group rules (lesson 10)** are typed: `{id, components, by: <column>, rule:
  same_choice}` means every unit sharing a value of `by` takes the same choice among
  those components (one of them, or none). toolkit/alloc gains a `links` constraint for
  it: the ILP adds one indicator per group and class, dominance pruning compares the
  class, `check()` verifies it, brute force respects it, and greedy refuses it (no
  bound holds). Coverage (`max_units_share`, `max_population_share`) becomes toolkit/alloc
  caps.

**Uncertainty sources (lesson 6).** Known kinds: `engine_parameters` (fitted parameters
within their card ranges), `calibration` (method-specific: target error for emulator
inversion, the parameter covariance for likelihood), `costs` (a multiplier range per
cost item), `emulator` (held-out error of the emulator). Each must appear in `sources`
or in `excluded` with a reason. The interval label is rendered from that list ("95% UI
over engine parameters and calibration; not covered: costs (...), emulator error
(...)"); the analysis refuses to produce an interval without it.

**Sign-off.** `toolkit/spec/signoff.py` is a thin adapter over
`scripts/decision_brief.py`: `decision_spec.yaml` joins the files frozen at brief
sign-off (absent files hash to None, so runs without a spec are unchanged). When a run
has a spec and no brief, the adapter writes the same sign-off record for the spec alone.
The analysis refuses an unsigned spec unless called with `--unsigned`, and every
artifact records `spec_signed`.

**Planner.** A skill, `.claude/skills/decision-spec/SKILL.md`, attached to the planner:
how to draft each section from the question and the data, with the validator as the
check. The planner drafts; the owner signs.

## 2. The analysis engine (`toolkit/analysis/`, harness code)

Driven only by the spec, the `Engine` contract and the run's data. Every step writes a
typed artifact under `<run>/analysis/` with an envelope:

```yaml
schema: analysis.<kind>/1
produced_by: harness
producer: toolkit/analysis/<module>.py
spec_sha256: ...          # the decision_spec.yaml the step read
spec_signed: true
engine: {name, version, card_sha256}
inputs: {<path>: sha256}  # data, batch results, upstream artifacts
data: {...}               # the step's payload
```

and `analysis/manifest.json` lists every artifact with its hash.

| Step | Module | Reuses | Artifact |
|---|---|---|---|
| Engine batch | `batch.py` | `scripts/compute.py` (dask or local pool, ledger, resume) | `analysis/batch.yaml`, `results.jsonl` |
| Outcome model | `emulator.py` | spike GP emulator, made engine-agnostic (cost-driver and diagnostic outputs named in the spec) | `analysis/emulator.yaml` (held-out accuracy) |
| Calibration | `calibration.py` | `scripts/identifiability.py` (Fisher, profile, classify) for `likelihood` | `analysis/calibration.yaml` (fit, residuals, clamped units, identifiability) |
| Point outcomes and optimisation | `optimize.py` | toolkit/alloc costs, packages, ILP | `analysis/point.yaml`, deliverable CSV |
| Uncertainty | `uncertainty.py` | `propagate_uncertainty.summarise_outputs` (extracted, no behaviour change) | `analysis/uncertainty.yaml`, `uncertainty_report.yaml` |
| Allocation stability | `stability.py` | `allocation_robustness.compute_verdict` for identity metrics | `analysis/stability.yaml`, `models/allocation_robustness.yaml` |
| Sensitivity | `sensitivity.py` | | `analysis/sensitivity.yaml` |
| Decision tests | `decision_tests.py` | Phase 49 result schema | `models/decision_tests/<id>.yaml` |
| Figures | `figures.py` | `figure_validator.validate_figure` sidecars | `figures/*.png` + sidecar |
| Claims and results | `claims.py` | `build_claims_ledger` (claims spec to ledger), `render_claims` | `models/claims_spec.yaml`, `models/claims_ledger.yaml`, `results.md` |
| Contract checks | `contract.py` | | gate findings |

**Two evaluation paths.** `emulator` (the spike: an engine too slow to run per unit,
per draw) fits Gaussian processes over a design and evaluates everything through them.
`direct` (a cheap engine) runs the engine itself for every unit, package and draw. Both
expose one interface (`OutcomeModel.predict`), so the later steps do not know which
one ran.

**Calibration.** `emulator_inversion` (the spike): per unit, the calibrated attribute
that reproduces its target, monotone in the attribute; clamped units are counted.
Identifiability there is one attribute per target; the diagnostic is the local slope of
the metric in the attribute, and a flat slope is `unidentified`. `likelihood`: the
spec's `calibrated` engine parameters and a per-unit attribute fitted to a panel of
targets (unit, period, package, observed count, exposure) by Poisson quasi-likelihood,
with the dispersion estimated from Pearson residuals; identifiability from
`identifiability.py` on the dispersion-scaled negative log-likelihood.

**Stability (lesson 5).** Leave-one-group-out as in the spike, and the headline metric
is the share of the objective lost when the fold plan is judged by the full calibration.
Rank r and package concordance are kept beside it; the verdict on identity uses
`allocation_robustness.compute_verdict`; a separate `value_verdict` uses value lost.
Thresholds for value lost are proposals in `toolkit/analysis/thresholds.yaml`.

**Decision tests (lesson 8).** Types: `interaction_rescale` (the spike's DT1),
`parameter_value` (an engine parameter set to a value), `group_rule` (a group rule on or
off), `envelope` (another budget). Before solving, each test states whether it can move
the plan: a test that only lowers the benefit of a component absent from the point plan
and from every re-optimised draw cannot add it, and the result file says so first. The
result file follows the Phase 49 schema (`finding`, `varied`, `criterion`,
`decision_changes`, `recommendation.with/without`, `share_units_changed`,
`share_threshold`) with `produced_by: harness`.

**Claims.** The harness writes `models/claims_spec.yaml` (source artifact and field, no
values) and builds the ledger with `build_claims_ledger`; `results.md` is rendered from
it with `render_claims`. A ledger claim may carry `interval_label`, which
`render_claims` prints in place of "95% CI".

## 3. Contract checks

`toolkit/analysis/contract.py: gate_findings(run_dir)` checks schema and provenance, not
numbers: every manifest artifact exists, its hash matches, its envelope is complete with
`produced_by: harness` and a producer under `toolkit/analysis/`, its `spec_sha256` is the
current spec's, the spec is signed, the payload has its kind's fields, the deliverable
CSV matches the allocation artifact, decision-test files are harness-produced and pass
the Phase 49 loader rules, every claim in the ledger cites a manifest artifact, and
figure sidecars are current. Findings fail closed (`analysis_contract_*`, HIGH). The PR
lists the existing gate checks that these make unnecessary; none is deleted here.

## 4. Tests and evidence

- `--self-test` on the toy engine for every module (both evaluation paths, both
  calibration methods, links in toolkit/alloc, the pool recycling in compute.py).
- Spike reproduction: the 13-state spec translated to the v2 schema
  (`spike/decision_spec.v2.yaml`), run on the stored batch; compared field by field with
  `spike/out/analysis.json`.
- One evaluation-suite question end to end on a cheap engine
  (`toolkit/engines/empirical/`, package rate ratios estimated from a panel), graded
  with `scripts/eval_suite.py grade` after the run.

## 5. Not in this workstream

The state machine and fix planner (§3.5), the evidence service (§3.2), engine choice,
deletion of gate checks, and the writer's narrative.
