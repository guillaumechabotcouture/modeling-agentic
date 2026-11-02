# v2: harness-run derivations for the units table and the spec's data (design note)

**Status:** design note for owner review (2026-11-01), written before any code. Branch
`v2-derived-inputs-design`, PR into `v2`. No code and no cloud spend are part of this note. It
extends `v2-spec-and-analysis.md` §1 (the decision spec), `v2c-orchestration.md` §3 (the SPEC and
SIGN states) and `v2d-compartmental-engines.md` §3.4 (observation sources).

## 1. Evidence (full v2 run, 2026-11-01)

The run asked a malaria allocation question over 13 states of one country. In the SPEC state the
planner task (`draft_spec`, `agents/v2_sdk_tasks.py`) drafted `decision_spec.yaml` and the units
table. A v2 task has file tools only (Read, Glob, Grep, Write, Edit). It has no shell, so it cannot
run a script. The prompt asks it to "derive what the spec reads ... from [the staged inputs], into
data/". With no way to execute anything, the planner wrote a 309-row `units.csv` itself; its own
note says the table was transcribed by hand without a shell.

| What went wrong | Count | Effect |
|---|---|---|
| Columns the engine reads were not carried over: the 12 monthly seasonality values, campaign timing, net-campaign history and latitude | 4 attribute groups (seasonality alone is 12 columns) | the spec bound none of them, and the engine fell back to its own defaults with no message |
| The calibration target was copied from a survey-model prevalence surface for a different age band, with no frame adjustment and no region-level error | 1 target column | the target did not describe the population the engine output describes (v2d §3.4: no source is the truth) |
| An eligibility rule for one component and the eligible population for another were not carried over | 2 constraint inputs | the allocation's feasible set was wrong |
| The schema validator was not run on the drafted files | | the planner cannot run it, and `h_SPEC` runs `pre_sign_problems`, which with `engine: auto` checks only the engine-independent sections (`_validate_pending`). Engine attributes, calibration and priors are checked at CONFIGURE, after SIGN |
| A prior was placed on a unit attribute | 1 | a unit attribute is data, not an engine parameter |

A harness-only path, in which a script (`derive_units.py`) built the same table from the same
staged inputs, produced the correct table. The owner rejected the pipeline spec and resumed the run
from the scripted one. This note describes the evidence by counts only. The inputs carry licences
that do not allow redistribution, so no data values from the run are given here.

**What this shows.** Each failure is a step that code can do and check, done by hand instead.
The prompt already said "take every number from a file, never type one from memory". The planner
followed the letter of that rule, because it read every value from a file, and the table was still
wrong. Rules in a prompt do not make a check, and the current checks could not see the gaps:
- an engine attribute left unbound is not an error today;
- a prior on a unit attribute is not checked before SIGN when the engine is `auto`, because the prior
  checks (`priors.problems`) need the engine card;
- nothing records how `units.csv` was made.

## 2. Alternatives considered

| Option | What it fixes | Why it is not enough or not chosen |
|---|---|---|
| **A. Give the planner a shell** (Bash, as in v1) | the planner could run a script and the validator | v1's evidence is that agents with a shell write new driver scripts every round, and outputs drift from the code that should have made them (`stage-machine.md` §1). A shell also reaches the network and the whole file system, so `agents/enforcement.py` has to police every command. It records nothing: a table made by a script and one typed by hand look the same afterwards. Running the validator would still be up to the agent |
| **B. More prompt instructions** ("never transcribe", "bind every attribute", "frame-adjust the target") | nothing structural | the run already had the relevant instruction. Each new failure would need a new sentence. This project prefers structural prevention to detection or prompt text |
| **C. A fixed, harness-owned derivation per question** (like `derive_units.py`) | correct tables for questions someone has already scripted | it does not scale: every new question would need a person to write its script. It is kept as a reference fixture and a retro check (§5, DI14) |
| **D. Agents declare derivations; the harness runs them in a sandbox and records hashes** (chosen) | every data file the spec reads is reproducible from a recorded script and recorded inputs; a hand-written table is refused by construction; completeness and prior checks run before SIGN | it needs a sandbox and a new record (§3). The planner still writes the transformation code, so a wrong script gives a wrong table. That is a judgement for critics (§3.6), but the table is now reviewable and re-runnable |

## 3. Proposal

### 3.1 Derivations: declared by the agent, executed by the harness

The planner writes code, never tables. Into the run directory it writes:
- `data/derive/<id>.py`: one pure transformation per derived file. It takes input paths and output
  paths as arguments, reads only those inputs and writes only those outputs;
- `data/derivations.yaml`, the declaration:

```yaml
derivations:
  - id: units
    script: data/derive/units.py
    inputs: [data/inputs/<staged file>.csv, data/inputs/<staged file>.csv]
    outputs: [data/derived/units.csv]
    columns:                 # input-column accounting (§3.5)
      uses: {data/inputs/<file>.csv: [colA, colB, "month_*"]}
      ignores: {data/inputs/<file>.csv: {colC: "superseded by <file>"}}
  - id: targets
    script: data/derive/targets.py
    inputs: [data/inputs/<file>.csv, data/derived/units.csv]
    outputs: [data/derived/calibration_targets.csv]
```

The harness runs derivations as pipeline steps. It reuses `scripts/pipeline.py` for the DAG, the
input and code hashing, staleness and owned outputs (`stage-machine.md` §3.2). What is new is a
sandboxed executor:
- **no network**: a new network namespace on Linux (`unshare -n` or `bwrap --unshare-net`);
- **read-only inputs**: `data/inputs/` and upstream derived outputs are bound read-only;
- **a private output directory**: the script writes into a temporary directory, and the harness
  copies the declared outputs into `data/derived/` after the run. An undeclared output is an error;
- **resource caps**: `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE` and `RLIMIT_NPROC`, plus a
  wall-clock timeout (the existing process-group kill);
- **imports from an allowlist**: an AST scan before the run, failing closed. The proposal is the
  standard library, numpy, pandas and pyyaml (open question 2);
- **a filtered environment**: no credentials (as `script_env()` already does).

Each derivation is run twice. Different output hashes give `derivation_nondeterministic` (HIGH). A
transformation is cheap, so running it twice is cheap too.

Where no sandbox is available, a derivation fails closed with `derivation_unsandboxed`. This is the
case on macOS today, since `scripts/pipeline.py` notes "no portable write sandbox". A development
flag lets it run, records `sandbox: none`, and a run that used the flag cannot reach ACCEPT
(open question 1).

**The record.** The harness writes `data/derivations_record.json`, a harness file added to
`HARNESS_FILES`. For each derivation it holds:
- the script's sha256;
- the sha256 of each input, which must match `data/inputs_provenance.json` for staged inputs;
- the sha256 of each output;
- the sandbox kind and limits, the Python and library versions, the runtime and the last lines of
  stderr.

The sign-off record freezes the record's hash next to the spec's, so a later change to a derived
file breaks the sign-off.

**Ownership.** `data/derived/` joins `HARNESS_PREFIXES`, so a Write or Edit there is refused at
PreToolUse. The `draft_spec` task owns `data/derive/`, `data/derivations.yaml` and
`data/README.md`, instead of the whole of `data/`. A hand-written table therefore cannot reach the
path the spec must cite, and the check in §3.2 is a second line, not the only one.

### 3.2 The spec reads only derived or staged files

Every data path in the spec must be either a declared output of a recorded derivation, with a
current hash, or a staged input under `data/inputs/`, with its hash in `inputs_provenance.json`.
This covers `decision_units.csv`, calibration target files, cost tables and any file named in
engine config. The paths are found by the same walk that `record_cited_inputs` uses. Codes (HIGH,
fail closed):

| Code | When |
|---|---|
| `units_not_derived` | `decision_units.csv` is not a derivation output (a hand-written or agent-written table) |
| `data_ref_not_derived` | another data path in the spec is neither derived nor staged |
| `derivation_stale` | a derived file's hash, or its script's or an input's hash, differs from the record |
| `derivation_failed` | the script exited non-zero, hit a cap, wrote an undeclared output or used a refused import |

**In the stage machine.** `h_SPEC` runs, in order:
1. the draft task;
2. the derivations;
3. `record_cited_inputs`;
4. the full pre-sign checks (§3.3–§3.5).

Any problem goes back to the planner as `spec_feedback` within `MAX_SPEC_TRIES`, as today, with the
derivation's stderr tail. The planner never needs a shell to learn whether its script works,
because the harness runs it and reports.

### 3.3 Engine-attribute completeness: no silent defaults

Today a card lists `unit_attributes` as descriptions, and an engine applies its own defaults when
an attribute is not given. Some defaults appear only in the description text (for example
"(default 0)").

- **Card metadata.** Next to `unit_attributes`, a card gains `attribute_binding: {attr: required |
  {default: <value>, meaning: "<what the default assumes>"}}`. It is metadata, like `priors.yaml`
  (v2 UQ), so `contract.py` and engine code fingerprints are unchanged. An attribute missing from
  `attribute_binding` counts as `required` (fail closed).
- **The spec binds every attribute.** Each attribute is bound in
  `decision_units.engine_attributes`, to a column of a derived table or to a `{value, source}`. Or
  the spec accepts the default by name in `decision_units.defaults_accepted: {attr: "<reason>"}`.
- **Codes.** `engine_attribute_unbound` (HIGH): an attribute neither bound nor accepted.
  `attribute_column_empty` (HIGH): a bound column that is missing or blank in any row.
- **Before SIGN.** With `engine: auto`, the check runs against every eligible candidate's card. A
  candidate whose required attributes the derived tables cannot bind becomes ineligible, with the
  unbound list as its reason in `engine_choice.json`. CONFIGURE runs the check again on the chosen
  engine.
- **Owner-visible.** The sign-off sheet lists each accepted default with its card meaning and the
  spec's reason. The report's assumptions block renders the same list from the spec.

### 3.4 Priors target engine parameters only

`toolkit/spec/priors.py` already refuses a spec prior that is not an engine parameter
(`uncertainty.priors`). This note extends the rule to every place a prior can be declared:
- **`prior_target_not_parameter`** (HIGH): a prior whose name is not an engine parameter. The only
  exception is a latent quantity that the calibration moves and that a declared observation source
  observes (v2d §3.1 and §3.4).
- **A unit attribute bound to a data column cannot carry a prior.** If that column is uncertain, the
  uncertainty is declared where it arises: the column becomes the observation of a latent quantity
  through a source with a sampling frame and a bias model, and the calibration or the measurement
  model carries it into the draws (v2d §3.4, `observation_source_undeclared` and
  `single_source_treated_as_truth`). This puts the run's second failure in the same structure:
  - the target is a derived table whose source declares its frame, including the population band
    it measures;
  - its noise model declares the level at which the error applies (for example per region);
  - **`target_frame_unmapped`** (HIGH): a target whose frame's population band differs from that of
    the engine output it is compared with, and the observation model declares no mapping between
    them.

### 3.5 Inputs that no derivation accounts for

A dropped column is the failure that no later check can see, because it is absent everywhere. So
the derivations account for every column of every staged tabular input:
- each column is in `columns.uses` or in `columns.ignores` with a reason. Globs are allowed, and an
  ignore glob needs its own reason;
- **`input_column_unaccounted`** (MEDIUM, before SIGN): a column in neither list;
- **`ignored_column_used`** (HIGH): the harness re-runs each derivation on its inputs with the
  ignored columns removed. If the output changes, the declaration is wrong. This makes the `ignores`
  list a tested claim, not a comment;
- the sign-off sheet shows the ignored columns per input, so the owner sees what the run chose not
  to use.

In the run, the eligibility and eligible-population columns would have appeared there, as ignored
or unaccounted.

### 3.6 What stays judgement

The planner still writes the transformation, and a wrong join or a wrong unit is still possible.
The critics already read the spec. With this change they also read `data/derive/*.py` and the
record: `critique-domain` checks each transformation against the input's documentation. Those
findings are critique blockers, and they do not replace the checks above.

### 3.7 Disease-agnostic

The harness code names no disease, place, intervention or unit. The checks speak of:
- allocation units and unit attributes;
- calibration targets and population bands;
- components, eligibility and observation sources.

Card metadata (`attribute_binding`) belongs to each engine. The tests use the toy engine and
synthetic inputs.

## 4. Tests to write first

All of these are self-tests that fail before the code exists: in `scripts/pipeline.py`,
`toolkit/spec/schema.py`, `toolkit/spec/priors.py`, `agents/stage_machine.py` and
`agents/v2_sdk_tasks.py`. They run on the toy engine with synthetic data.

| ID | Test |
|---|---|
| DI1 | a fixture shaped like the evidence: `units.csv` written by the task. The spec check gives `units_not_derived`, and a Write to `data/derived/units.csv` is refused at PreToolUse |
| DI2 | a derivation that opens a socket fails inside the sandbox: `derivation_failed`, with the reason recorded |
| DI3 | a derivation that writes to an input path, or outside its output directory, fails, and the input's hash is unchanged |
| DI4 | memory, CPU-time and file-size caps each kill a derivation, and the record names the cap |
| DI5 | an unseeded random or time-dependent derivation gives `derivation_nondeterministic` |
| DI6 | after a run, editing a derived output, its script or an input gives `derivation_stale`, and re-running clears it. A post-sign edit breaks the sign-off |
| DI7 | toy card with one required attribute and one attribute with a default. Leaving the first unbound gives `engine_attribute_unbound`. Leaving the second unbound without `defaults_accepted` gives the same code; with a reason, it passes and appears on the sign-off sheet |
| DI8 | `engine: auto` with two candidates, one of which needs an attribute that no derived column binds. The completeness check runs in `h_SPEC` before SIGN, and that candidate is ineligible, with the reason |
| DI9 | a prior on a data-bound unit attribute (in `uncertainty.priors` and in `calibration.priors`) gives `prior_target_not_parameter`. A prior on an engine parameter passes. A posterior's latent quantity with a declared observation source passes |
| DI10 | an input column that is neither used nor ignored gives `input_column_unaccounted`. An ignored column whose removal changes the output gives `ignored_column_used` |
| DI11 | a target whose frame band differs from the engine output's band, with no mapping, gives `target_frame_unmapped`. With a declared mapping it passes |
| DI12 | every existing self-test spec and fixture outside a v2 machine run validates unchanged. The obligations apply when the stage machine drafts the spec. `contract.py` and the engine code fingerprints are byte-identical |
| DI13 | the new harness code and its messages contain none of the disease, place or intervention terms that the convention lists |
| DI14 | retro check, local only: run on the 2026-11-01 run directory, the pipeline draft is refused (`units_not_derived`, plus the unbound-attribute and prior findings, reported as counts). `derive_units.py` declared as a derivation passes. Only counts go into the PR |

## 5. Build plan (four Greek-letter steps, each test-first)

| Step | Content | Done when |
|---|---|---|
| α tests | DI1–DI13 as failing self-tests; a synthetic fixture run directory (toy engine, toy inputs, a correct and an incorrect derivation) | every new test fails for the stated reason; existing self-tests pass |
| β derivation runner | `data/derivations.yaml` schema; the sandboxed executor in `scripts/pipeline.py` (Linux namespaces and rlimits; fail closed elsewhere, with a development flag); the run-twice determinism check; `data/derivations_record.json`; `data/derived/` as a harness prefix and the narrowed `draft_spec` ownership; `h_SPEC` runs derivations and feeds errors back | DI1–DI6 pass |
| γ spec checks | `units_not_derived`, `data_ref_not_derived`, `derivation_stale`; card `attribute_binding` for the three engines and `engine_attribute_unbound` before SIGN, per candidate; `prior_target_not_parameter`; `target_frame_unmapped`; input-column accounting; the sign-off record freezes the derivation record; the sign-off sheet lists accepted defaults and ignored columns; the `decision-spec` skill and the `draft_spec` prompt say how to write a derivation (a consequence of the structure, not the fix) | DI7–DI13 pass; all self-tests in `CLAUDE.md` pass |
| δ retro and live | DI14 on the 2026-11-01 run directory; then the SPEC stage alone (planner plus derivations, no engine batch) re-run on the dev VM against the same staged inputs, compared column by column with the scripted table (match counts only). Its cost estimate goes to the owner before it starts | the retro refuses the hand-written draft and accepts the scripted one; the live SPEC stage produces a units table whose bound attributes and constraint columns match the scripted table, or the differences are listed as findings |

## 6. Open questions for the owner

1. **The sandbox on macOS.** Linux namespaces are available on the dev VM and the agent VM.
   Proposal: macOS fails closed, and the development flag blocks ACCEPT. The alternative is a
   container on macOS.
2. **The import allowlist.** The proposal is the standard library, numpy, pandas and pyyaml.
   Geometry inputs (for example a centroid's latitude) would need geopandas or shapely. Add them,
   or require geometry to be staged as tables?
3. **Input-column accounting on wide inputs.** Globs with reasons keep it short. Should
   `input_column_unaccounted` be MEDIUM (declarable) or HIGH?
4. **Scope of the obligations.** Proposal: every spec the stage machine drafts. Specs written by
   hand for harness-only runs could opt in with the same declaration, as the scripted path
   effectively did.
