# v2: agent-built engines, admitted by harness checks (design note)

**Status:** written before any code (2026-11-23), at the owner's request. Nothing here is built. File, function,
state and field names are proposals. It extends `architecture-v2.md` §3.3 (the Engine contract),
`v2c-orchestration.md` (the states), `v2-derived-inputs.md` (the sandbox) and the ACQUIRE draft
(`docs/design/acquire.md`, branch `design-acquire`). The stage machine is being split into a package in parallel
(branch `split-sm-pkg-v2`): if that lands first, the handlers below move with the others, and the state names,
`EDGES` and fields stay the same.

## 1. The problem, and the guarantee to keep

A v2 run answers a question only with a registered engine: `ENGINES` in `agents/analysis_backend.py` lists the
toy engine, malaria (Starsim), the package-rate engine (empirical), compartmental (Starsim) and renewal. When no
card fits the spec, SPEC is refused before sign-off ("no candidate engine passes the full checks before
sign-off"), or ENGINE_CHOICE ends the run (`run_failed`, "no registered engine fits the spec"). A question whose
decision turns on a mechanism none of these cards has (four dengue serotypes with secondary-infection severity; a
subclinical TB stage across 17 age groups) gets either no answer or an answer from an engine that leaves that
mechanism out.

The owner's decision of 2026-11-23: frontier coding agents already build such bespoke models quickly, so v2 gets a
lane in which an LLM task writes an engine module for the run. The team rule holds: our own code, Starsim for
dynamic models, no R, no malariasimulation.

**The guarantee, restated for an agent-built engine.** Every number still comes from code that the harness ran
and tested. The agent writes the model's mechanics. The harness owns everything around them:
- the interface, which the owner signs in the spec before anything is built;
- the checks, which harness code generates from the signed spec, including cases the agent never sees;
- running the engine, in a sandbox, through a harness adapter;
- the record of what was admitted (every file's sha256), and the owner's second signature on that record.

An engine that has not passed admission cannot be imported by the analysis.

**What this lane does not promise.** The checks can show that the code does what the signed interface says, that
it conserves what it declares, that every parameter changes something, that it reduces to known closed forms, and
that its intervention effects match the trials in the signed dossier. They cannot show that the biology is right
beyond those points. That judgement stays with the reviewer task (§4.5), the independent reference build (§4.4)
and the owner at ENGINE_SIGN. The admission sheet says so in its first paragraph.

**Terms used here.**

| Term | Meaning |
|---|---|
| task | one bounded LLM session (`SdkTasks._session` in `agents/v2_sdk_tasks.py`), with a dollar cap and a turn cap |
| engine request | the part of the signed spec that states the interface the built engine must implement (§2.1): its components, unit attributes, outputs, parameters and ranges, and the reductions to known models |
| build task | the task that writes the engine module (`build_engine`, §3) |
| try tool | a harness tool offered to the build task only, which runs the public checks on its current code in the sandbox and returns the results (§3.3) |
| admission | the harness step that runs every check on the finished module and writes `engine_build/admission.json` |
| public / hidden cases | checks with the same generators, drawn from a seed the agent sees (public) or from a seed only the harness holds (hidden) |
| oracle | an independent reference value: a closed form, a fixed point or an ODE solution (as in `toolkit/engines/*/benchmarks/oracles.py`) |
| reduction | parameter settings, written in the request, under which the model must equal a known simpler model (for example, one serotype with no cross-immunity equals single-strain SIR) |
| wiring check | the check that each declared parameter and switch changes at least one declared output (§4.2, A6) |
| adapter | `toolkit/engines/built.py` `BuiltEngine`: harness code that implements the Engine contract around the agent's module |
| sandbox | the bubblewrap runner from `scripts/pipeline.py` (`sandbox_kind()`, `_bwrap_cmd`): no network, read-only files, resource limits |

## 2. Where it sits in the state machine

### 2.1 How the spec declares it

`engine` gains a `build` form. The spec drafter writes it, the owner signs it with the rest of the spec, and
nothing is built without it.

```yaml
engine:
  build:
    id: dengue4                        # [a-z0-9_]{3,24}; the module lives in models/ext/engine_dengue4/
    kind: starsim_module               # starsim_module | closed_form (open question 1)
    why: >-                            # why no registered engine serves; the harness checks it cites the rows (below)
      no registered card has serotype-specific immunity or severity on secondary infection
    mechanisms: [...]                  # plain-language list, as on every card: what the model represents
    request:                           # the interface: the card the engine must implement
      components: {vaccine: [0, 1], vector_control: [0, 0.5, 1]}
      absent_values: {vaccine: [0], vector_control: [0]}
      unit_attributes: {N: "population (> 0)", foi: "force of infection per serotype per year [0, 0.5]"}
      outputs: {per_capita: [infections, severe, deaths], windows: [seroprevalence_9y], cost_drivers: [doses]}
      age_groups: [0-4, 5-14, 15+]
      parameters:                      # every parameter the spec binds, calibrates or puts a prior on
        - {name: cross_protection_years, kind: fitted, lo: 0.5, hi: 3.0, units: y, source: "dossier:CP1"}
        - {name: severity_secondary_rr, kind: fitted, lo: 2.0, hi: 25.0, source: "dossier:SEV1"}
      valid_ranges: {foi: [0, 0.5]}
      directions:                      # declared signs, checked (A7); optional per pair
        severity_secondary_rr: {severe: "+"}
        vector_control: {infections: "-"}
      conservation: closed             # closed | births_deaths | open_with_flows (A5)
      reductions:                      # settings under which the model equals a known model (A4)
        - {id: R1, oracle: final_size_lambert, set: {n_serotypes_active: 1, cross_protection_years: 0},
           unit: {foi: 0}, compare: {windows.attack_rate: {abs: 1.0e-3}}}
      stochastic: false                # true for an agent-based model: checks use paired seeds (§4.2)
      configs: {default: {}}           # constructor keywords; the signed configuration in use
    caps: {usd: 40, tries: 2}          # optional; at most the harness maxima (§6.2)
```

A spec may also write `engine: {auto: true, candidates: [...], build: {...}}`: the registered candidates are
considered first, and the build runs only when none is eligible after sign-off. The plain `build` form is for a
question where the owner wants the built engine even though a registered engine is eligible; its `why` must then
name a mechanism of the request that the eligible card lacks (open question 13).

**Schema (`toolkit/spec/schema.py`).** New `validate_build(spec)`:
- `request` has every field above, in the contract's vocabulary (`toolkit/engines/contract.py` `EngineCard`,
  `ParamSpec`, `KINDS`);
- every `source` is a dossier id or `assumption`;
- every `reduction.oracle` is a name in the harness oracle library (§4.3);
- `why` is non-empty.

New `schema.with_requested_engine(s)` builds a stand-in card from `request` (an `EngineCard` with no benchmarks)
so that the rest of the spec is validated before sign-off as it is today: `decision_units.engine_attributes`,
calibration, priors and `attribute_binding`. `engine_needs(spec)` and `card_fits` run against that stand-in, so a
spec whose options need a component the request does not declare is refused at SPEC, not after a build.

**Who drafts it.** `draft_spec` (prompt in `agents/v2_sdk_tasks.py` `PROMPTS["draft_spec"]`) is told that it may
write `engine.build` only when the harness's pre-sign candidate rows (`_ToolkitApi.pre_sign_problems`) show no
eligible engine, and that `why` must name the rows' `why_not` reasons. The harness checks this: a build request
in a spec whose pre-sign rows include an eligible engine is refused at SPEC unless `why` names a request mechanism
missing from every eligible card (`build_request_problems`, new in `agents/analysis_backend.py`).

**What the owner signs at SIGN.** The sign-off sheet (`decision_spec.signoff_sheet.md`) gains an "Engine to be
built" section: the request as tables (components, outputs, parameters with ranges and sources, declared
directions, conservation, reductions), the `why` beside the candidate rows it cites, and the caps. The spec
signature (`toolkit/spec/signoff.py sign`) already covers these bytes. The owner is signing the interface and the
tests derived from it, not code that does not yet exist.

### 2.2 States and edges

```
SIGN -> ENGINE_CHOICE -> ENGINE_BUILD -> ENGINE_ADMIT -> ENGINE_SIGN -> PREREGISTER | CONFIGURE -> ...
                              ^              |                ^
                              +--------------+  (a try left)  +-- owner signs (pause)
GATE | FIX_LOOP -> ENGINE_BUILD  (engine repair, at most once per run, §6.1)
```

Changes to `agents/stage_machine.py`:

| Item | Change |
|---|---|
| `STATES` | `"ENGINE_BUILD"`, `"ENGINE_ADMIT"`, `"ENGINE_SIGN"` after `"ENGINE_CHOICE"` |
| `EDGES` | `"ENGINE_CHOICE"` gains `"ENGINE_BUILD"`; new `"ENGINE_BUILD": {"ENGINE_ADMIT", "ENGINE_BUILD", "FINAL"}`, `"ENGINE_ADMIT": {"ENGINE_SIGN", "ENGINE_BUILD", "FINAL"}`, `"ENGINE_SIGN": {"PREREGISTER", "CONFIGURE", "ENGINE_SIGN", "ENGINE_BUILD", "FINAL"}`; `"GATE"` and `"FIX_LOOP"` gain `"ENGINE_BUILD"` |
| `LLM_STATES` | gains `"ENGINE_BUILD"`, so `step()` checks the budget before the handler runs |
| `RunState` | new field `engine_build: dict` with `id`, `status` (`requested`, `building`, `admitted`, `refused`, `signed`, `declined`), `tries`, `repairs`, `spent_usd`, `compute_usd`, `admission_sha256`, `files_sha256`, `hidden_commitment` |
| new constants | `MAX_ENGINE_BUILD_TRIES = 2`, `MAX_ENGINE_REPAIRS = 1`, `ENGINE_BUILD_CAP_USD`, `ENGINE_ADMIT_COMPUTE_CAP_USD` (§6.2) |
| new pause | `AWAITING_ENGINE_SIGNOFF = "awaiting_engine_signoff"`, printed as `paused:awaiting_engine_signoff`, exit 2 |
| new statuses | `completed_engine_not_admitted` and `completed_engine_declined`, both exit 2 (added to `main.py` and the CLAUDE.md table in the PR that adds them) |
| `POST_CHOICE_STATES` | gains the three states, so a resume past ENGINE_CHOICE re-checks before compute, as it does now |

A state file saved before this change has no `engine_build` field; it loads as `{}` and resumes as today.

**`h_ENGINE_CHOICE`.** Unchanged for specs without `build`. With `build` (or `auto` + `build` and no eligible
candidate): record the candidate rows in `engine_choice.json` with `build_requested: true`, write
`engine_build/request.json` (the request as signed, with its sha256 and the spec's signed sha256), and go to
ENGINE_BUILD. The evidence gate (`evidence_gate`) runs on the request's decision inputs here, before any build: the
signed dossier must cover the requested parameters, as it must for a registered engine.

**`h_ENGINE_BUILD`.** Runs the `build_engine` task (§3) with a harness packet. On return, the harness runs the
static checks (§4.1). A failure goes back to ENGINE_BUILD with the problems while tries remain, else FINAL
`completed_engine_not_admitted`. On success it freezes the module (§5.1) and goes to ENGINE_ADMIT.

**`h_ENGINE_ADMIT`.** Deterministic harness code (`toolkit/engines/admission.py admit(run_dir)`). It draws the
hidden seed, runs every check (§4) through the adapter in the sandbox, and writes `engine_build/admission.json`
and `engine_build/admission_sheet.md`. Then:
- every gating check passes: ENGINE_SIGN;
- a check fails and a try is left: ENGINE_BUILD, with feedback (§4.6);
- otherwise: FINAL `completed_engine_not_admitted`, with `engine_build/request_for_owner.md` saying which checks
  failed and what the owner could amend.

No LLM runs in this state.

**`h_ENGINE_SIGN`.**
- `--signoff owner`: pause `awaiting_engine_signoff` until `python3 toolkit/engines/built_signoff.py sign
  <run_dir> --by owner`. That command shows the sheet and binds the sha256 of `admission.json`, the sheet and the
  frozen files. It uses the same mechanics as `toolkit/spec/signoff.py`, and the owner key once owner
  authentication lands.
- `--signoff lead`: the lead signs provisionally (`by: lead`); the report says so in its first paragraph (open
  question 3).
- `decline --note "<what is missing>"`: one more ENGINE_BUILD try with the note, if a try is left; else FINAL
  `completed_engine_declined`.
- After signing: `engine_choice.json` gets `engine: {ref: "toolkit.engines.built:BuiltEngine", config:
  {run_engine: "engine_<id>", admission_sha256: ..., <request.configs.default>}}`, then PREREGISTER or CONFIGURE
  as today.

Because the reference names a class under `toolkit.engines`, the import guard (`toolkit/import_guard.py`, policy
`engine`) and `schema.engine_class` need no new policy. The adapter's constructor does the admission check (§5.1).

## 3. The build task

### 3.1 Its powers

| Power | `build_engine` task | Every other v2 task |
|---|---|---|
| file tools | `Read`, `Glob`, `Grep`, `Write`, `Edit` (`TOOLS`) | as today |
| Bash, Agent, NotebookEdit, WebFetch, WebSearch | refused at PreToolUse (test S16) | refused |
| running code | only through the try tool, `mcp__engine__try` (§3.3), served in-process by the harness (`claude_agent_sdk.create_sdk_mcp_server`, as ACQUIRE's fetch tool) | none; the hook refuses `mcp__engine__*` for every other task kind |
| writes | `OWNS["build_engine"] = ("models/ext/engine_<id>/",)` plus `_task_output.json` | `models/ext/engine_` is refused for every other task, the fix task included (below) |
| may read | the signed spec, `engine_build/request.json`, the dossier, the derived tables' schema views, `_task_input.json`; in the repository: `toolkit/engines/` (the contract, hooks and the registered engines as examples), `.claude/skills/starsim-dev*/`, and the installed `starsim` package source. Not `engine_build/` (except `request.json` and the public results the try tool writes), not `engine_build/reference/`, not `analysis/`, not other runs | as today |
| acquired inputs | the schema views only (`data/inputs/acquired/_views/`), never the raw files (ACQUIRE §3.3) | as ACQUIRE |
| model | `enforcement.MODEL_DEEP` | as today |
| caps | `max_turns = 150`; dollar allowance per try from the build balance (§6.2) | as today |
| environment | `filtered_env(..., allow_cloud=False)` | as today |

**Write ownership.** `agents/fix_loop.py` `FIX_EDITABLE` keeps `models/ext/`. `_allowed` in
`agents/v2_sdk_tasks.py` gains a rule: a path under `models/ext/engine_` is refused unless the task kind is
`build_engine`, so a fix task cannot change an engine after admission. After freezing (§5.1), the files are
`0444` and in the integrity record, so even the build task cannot change them until a repair re-opens them.

### 3.2 What the module must implement

The adapter owns `run()`. The agent supplies the mechanics behind a narrower interface, so harness code, not the
agent's code, does the seeding, accounting and output reading.

```
models/ext/engine_<id>/
  engine.py            class Model (required); helpers in other .py files of this folder only
  card.yaml            the EngineCard fields plus the agent fields below; benchmarks: [] (the harness fills them)
  ENGINE_NOTES.md      the model in words and equations; assumptions; what it does not represent
  decision_inputs.yaml, attribute_binding.yaml   as for registered engines (formats in toolkit/engines/*/)
```

`kind: starsim_module`:

| Method | Returns | Notes |
|---|---|---|
| `Model(**config)` | | config from `request.configs`; no I/O |
| `describe()` | `EngineCard` | must equal `card.yaml` and, on the structural fields, the request (A1) |
| `parameters()` | `[ParamSpec]` | the request's parameters, plus any extra `fixed` ones, each with a source |
| `build_sim(params, unit, package, horizon)` | an unrun `ss.Sim` | no seed argument: the adapter sets `rand_seed` |
| `read_outcomes(sim, unit, package, horizon)` | `Outcomes` | reads only the finished sim |
| `state_vars()` | `{name: (module, attribute)}` | where the compartments live, for the harness's balance analyzer (A5) |
| `run_batch_state(...)` (optional) | | a shared burn-in for several packages, as `MalariaEngine.run_batch`; if present, A3 checks it |

`kind: closed_form` (if the owner allows it, open question 1): `evaluate(params, unit, package, horizon) ->
Outcomes` and `state_totals(...)`. There is no simulation loop.

**What the adapter does on each `run(params, unit, package, horizon, seed)`.**
1. It resolves `params` with `contract.resolve_params`, so unknown names fail closed.
2. It wraps them in a `TrackedParams` mapping that records which names are read in `build_sim` and which in
   `read_outcomes`.
3. It calls `build_sim`. A sim that is already initialised is refused. The adapter sets the seed, adds its own
   `BalanceAnalyzer` (an `ss.Analyzer` that reads the declared `state_vars` each step), runs the sim, then calls
   `read_outcomes`.
4. It returns the `Outcomes`, with `meta.balance` and `meta.param_reads` added by the harness.

All of this happens inside the sandbox worker (§5.1). A spike in α confirms that the adapter can add an analyzer to
an unrun sim on the pinned Starsim version. If it cannot, the fallback is to have `build_sim` return the sim's
arguments and let the adapter construct the sim.

**Allowed imports** (an AST scan, failing closed; open question 10): `starsim`, `numpy`, `scipy.special`,
`scipy.stats`, `math`, `dataclasses`, `typing`, `functools`, `itertools`, `toolkit.engines.contract`,
`toolkit.engines.hooks`, and the module's own sibling files. Not allowed: `scipy.integrate` (the engine steps on
Starsim's time grid; ODE solvers belong to the oracles), and anything that touches files, processes, the
interpreter or the network.

### 3.3 The try tool

Frontier agents build models quickly because they run their code. The build task has no shell. It gets one
harness tool instead.

| Tool | What it does | Limits |
|---|---|---|
| `mcp__engine__try(checks: [ids] \| "all")` | runs the static checks and the **public** cases of the named checks on the current files, in the sandbox, through the adapter; writes `engine_build/try/<n>.json`, which the task can read, and returns a summary of at most 8 KB: each case's id, inputs, expected value, computed value, pass/fail, and the traceback's last frames for an exception | 30 calls per try, 120 s wall and 2 GB per call, at most one call running at a time; counted in `engine_build/try/ledger.json`, so the count survives a crash |
| `mcp__engine__run(unit, package, params, horizon)` | one adapter run on inputs the task chooses within the request's ranges; returns the `Outcomes` and the balance trace (capped) | counted in the same 30 |

The tool never runs hidden cases. Both tools run the same code that admission runs.

### 3.4 How it gets data

The engine reads no data. The AST scan refuses `open` and `io`, and the sandbox gives it no files. Everything
reaches it through the contract:
- unit attributes, which the signed spec binds from staged or derived tables (`decision_units.engine_attributes`);
- parameters, whose values and ranges come from the signed request and the dossier;
- the configuration.

The task may read the derived tables' schema views and the dossier, so it can pick sensible dimensions and
defaults. A constant the module needs (a contact matrix, a life table) must arrive as a unit attribute or a
parameter the request declares, or as a sourced table on the card (A9). It is never a file in the folder.

### 3.5 The packet and the prompt

`h_ENGINE_BUILD` builds `_task_input.json` with:
- the request;
- the contract's docstring;
- the list of checks with what each tests (§4.2) and its public cases;
- the oracle library's function signatures;
- the module layout (§3.2);
- the allowed imports;
- on a retry, the feedback (§4.6).

The prompt is `COMMON` plus the decision design principles, as every task's is. It adds that the engine carries
trial effects over to the setting (principle 3), that its own burden is a validation check and not the basis of
the decision, and that a parameter the request does not name enters only as `fixed` with a source.

## 4. The admission gate

Every check is deterministic harness code in `toolkit/engines/admission.py`. Its cases come from generators that
read the signed request and dossier (§4.3); the build task chooses none of them. Each generator runs twice:
- with the **public seed**, which the try tool uses and the agent sees;
- with the **hidden seed**, drawn at admission by `secrets.token_bytes`, held in `engine_build/.hidden/` (a harness
  prefix that no task may read), and committed as its sha256 in `admission.json` before any hidden case runs.

The hidden cases use the same generators with different draws: other base points inside the ranges, other units,
other seeds, other orders. An honest engine passes both sets. An engine fitted to the public cases fails the hidden
set.

### 4.1 Static checks (before any run)

| Id | Check | What it catches | What it cannot catch |
|---|---|---|---|
| S1 | files: only `.py`, `card.yaml`, `ENGINE_NOTES.md`, `decision_inputs.yaml`, `attribute_binding.yaml`; no links, no binary or data files; total size at most 300 KB | precomputed answers shipped as data (`.npy`, `.pkl`, `.csv`) | answers written as code literals (S4) |
| S2 | AST import allowlist (§3.2) | file, process, network and interpreter access; external simulators (R bridges, malariasimulation) | nothing at run time on its own; the sandbox is the second layer |
| S3 | banned names and attributes: `open`, `exec`, `eval`, `compile`, `__import__`, `globals`, `locals`, `vars`, `breakpoint`, `input`, `getattr`/`setattr`/`delattr` with a non-literal name, any dunder attribute access other than defining `__init__`; module level holds only imports, constants, functions and classes | test-mode detection (stack inspection, reading the environment), code generated at run time | a module that behaves differently on inputs it can see (hidden draws answer this) |
| S4 | numeric literal audit: an array or sequence of more than 16 numeric literals must be a card `tables:` entry with a source and the parameter or attribute it stands for | lookup tables of expected answers; undeclared contact matrices and life tables | a few scalar constants tuned to pass; the sourced-parameter rule and the wiring check cover those |
| S5 | `describe()` equals `card.yaml`; `card.benchmarks` is empty | a card that says one thing while the code reports another | |

### 4.2 Run checks

All run through the adapter in the sandbox. "Moves" means a change larger than
`max(abs_floor, rel_floor * |base|)`. For a stochastic engine it means a paired change over K common-random-number
seeds whose mean exceeds the floor and three standard errors of the paired difference. Floors are in
`toolkit/engines/admission_thresholds.yaml`, never in the request.

| Id | Check | Cases (generated from) | Catches | Cannot catch |
|---|---|---|---|---|
| A1 | contract conformance: `contract.check_engine` on each generated unit and package; the card equals the request on components, unit attributes, outputs, age groups, parameter names and kinds; every range lies within the request's range | every component value; units at range ends and interior | missing outputs, undeclared attributes, negative or non-finite outcomes, kinds or ranges changed from what was signed | whether the outputs mean what their names say (A4, A8, A10) |
| A2 | determinism under seed: two runs with the same seed are identical in separate processes, with different `PYTHONHASHSEED` values and different case orders; a stochastic engine's runs with different seeds differ; a deterministic one's do not | 8 hidden cases | hidden state carried across calls, order dependence, set or dict iteration leaking into results, unseeded randomness | randomness that is seeded but wrong (A4, A10) |
| A3 | batch equals run: `run_batch` equals `run` pair by pair (rel 1e-12, as `probe_batch_vs_run` in the compartmental oracles); skipped when the module has no batch path | 4 units × all packages | a shared burn-in that leaks one package's state into another | |
| A4 | analytic and oracle cases: each `request.reductions` entry, plus the identities the harness derives without the agent: all components at their absent values equals the no-intervention arm; zero transmission (where the request maps one) gives zero incidence; per-capita outcomes do not change with N when `conservation: closed` and the request does not declare N-dependence | reductions × 3 hidden settings of the parameters the reduction leaves free | wrong core dynamics in any case that reduces to a known model; an intervention that acts when it is off | errors only in the parts no reduction covers (A6, A7, A8, A10, the reviewer) |
| A5 | mass balance and population conservation: from the harness's own `BalanceAnalyzer` trace, every compartment ≥ 0 at every step; sum of compartments = N0 (closed), or N0 + births − deaths (births_deaths), or as declared flows (open_with_flows), to 1e-9 relative (1e-6 for an agent-based model's integer counts); cumulative infections in `read_outcomes` equal the infection flow in the trace | 6 hidden cases at the ends of the ranges | leaks, double counting, negative compartments, outputs computed apart from the dynamics | a compartment left out of `state_vars` (S5 and the reviewer compare the notes with the declaration) |
| A6 | **sensitivity wiring**: each parameter (every kind), each component value and each unit attribute set to each end of its range, and to two interior points, while the rest stay at a hidden base point; it must move at least one declared output, in the decision window, at one or more of 3 hidden base points. If it does not, the card must declare `inert: {name: {reason, outputs_unaffected}}`. Every declared output must also be moved by something. `meta.param_reads`: a parameter read only in `read_outcomes` must have `role: observation` or `role: cost` | parameters × (2 ends + 2 interior) × 3 base points | dead parameters, switches that do nothing, outputs that never change, effects computed after the fact instead of in the dynamics | a parameter that is wired but has the wrong size of effect (A7, A8, A10); an `inert` reason that is false (the owner reads every `inert` entry on the sheet) |
| A7 | declared directions: each `request.directions` sign holds at every A6 point where the parameter moves the output | from A6 | wrong signs (an intervention that raises cases) | directions the request does not declare |
| A8 | trial benchmarks: one `toolkit/engines/benchmark.py` trial benchmark per dossier trial entry that the request's components use, built from the entry (its arms, setting, follow-up window and published interval: `published`, `estimand_type`, `estimand_mapping`), run through the adapter; `benchmark_coverage.assess` applies unchanged (only `in_band` passes; an uncovered component blocks unless the signed spec declares an `engine_departures` entry) | the dossier, not the agent | intervention effects that disagree with the trials (principles 3 and 4) | components with no trial (they need a signed departure, as for a registered engine) |
| A9 | literature benchmarks: values in the dossier and in `expert_priors.yaml` entries whose `applies_when` match (burden by age, R0 ranges, seroprevalence by age), as `value` benchmarks with bands; reported, and gating only where the signed request lists them in `gating_benchmarks` | the dossier and priors | outputs far from what the field reports | a benchmark that no one transcribed |
| A10 | identifiability on the calibration targets: simulation-based calibration with the benchmark runner's `coverage` type. The generator draws true values of the spec's calibrated parameters from their priors (hidden seed), makes synthetic targets with the spec's own observation model (`toolkit/analysis/observation.py`) at the spec's calibration units, and the run's own calibrator recovers them (`toolkit/analysis/joint_calibration.py` or `toolkit/calibration/history_matching.py`, whichever the spec uses). Plus the local Fisher and profile-likelihood classes of `scripts/identifiability.py` at 3 true points | the spec's calibration section | parameters the targets cannot see, ridges such as R0 × s0 (renewal ENGINE_NOTES, RW4), a calibration that cannot recover its own truth | identifiability under a wrong model: it tests the model against itself |
| A11 | independent reference (§4.4): agreement with a second implementation written from the request alone, on the declared outputs at hidden cases | hidden cases from A4 and A6 | misreadings of the request that the two builds do not share; wrong sizes of effect | a misreading both builds share; an error in the request itself |

**Which checks gate.** A1 to A8 and A11 gate admission. A9 gates only for the benchmarks the request lists in
`gating_benchmarks`. A10 does not gate on its own (open question 8): an unidentified calibrated parameter that
moves a decision output goes on the sheet, and the spec must already have a decision test over its range
(principle 6), or the owner amends the spec. This matches how registered engines are treated.

### 4.3 Checks generated from the spec, not chosen by the agent

| Source (signed) | Generated checks |
|---|---|
| `request.parameters`, `components`, `unit_attributes`, `valid_ranges` | A1, A2, A6 cases: Latin hypercube base points inside the box (`benchmark.lhs`), ends and interior points |
| `request.directions` | A7 |
| `request.absent_values`, `conservation`, `stochastic` | the A4 identities, A5's balance rule, A2's seed rule |
| `request.reductions` | A4 oracle cases, from the oracle library `toolkit/engines/built_oracles.py`. It re-exports the existing closed forms (`oracle_final_size_lambert`, `oracle_herd_immunity`, `oracle_wallinga_lipsitch`, `oracle_branching_single`, `oracle_seir_growth_r0`, `oracle_final_size_multigroup`, `oracle_ode_final_size`, `oracle_cohort_susceptible`) and adds a few generic ones: SIS endemic prevalence 1 − 1/R0, the stable age distribution of a constant-rate life table, constant-hazard survival, and a multigroup SEIR ODE |
| dossier trial entries | A8 |
| dossier values, `expert_priors.yaml` | A9 |
| `calibration` | A10 |

The agent may add its own test cases to `card.yaml` (`self_checks:`). They run and are reported. They never
replace a generated check and never gate (open question 11).

**Gaps in the request.** A reduction that the request does not declare is not tested. The pre-sign sheet shows how
many reductions the request has and which outputs no reduction reaches. The spec critics (`toolkit/spec/
spec_critique.py`) get one new item: "a built engine whose request has no reduction for a core mechanism".

### 4.4 The independent reference

A second bounded task, `build_reference`, writes `engine_build/reference/reference.py`. It works from the request
and the dossier only, and cannot read `models/ext/engine_<id>/`; the build task cannot read the reference. The
reference is a test oracle in the sense of the 2026-11-01 owner decision on `oracles.py`. It may use
`scipy.integrate` and plain loops, is never an engine and is never exposed to the analysis. It runs in the same
sandbox.

A11 compares the engine with the reference on the declared outputs at hidden cases:
- a deterministic engine within `request.reference_tolerance` (default 1% relative);
- an agent-based engine through its mean over K seeds at a large N, within the tolerance plus three standard
  errors.

A disagreement is not blamed on either side. The sheet shows both values, and the next build try gets the case ids
and the output names.

This is the most expensive check (§6.2) and the one that most directly tests "did the agent read the mechanism the
way the request describes it". It does not help when the request itself is wrong. Open question 2 asks whether it
is on by default.

### 4.5 The engine reviewer

`critique-engine`: a reviewer task with read access to the frozen module, `ENGINE_NOTES.md`, `admission.json` and
the request. It runs after admission passes and before ENGINE_SIGN. Its checklist:
- the notes match the code;
- every state in the code is in `state_vars`;
- there are no additive terms that exist only to move an output (an `epsilon * p` pattern);
- each `inert` reason is true;
- the assumptions are listed;
- each parameter source matches the dossier.

It writes `critique_engine.yaml` in the blocker schema. A HIGH finding sends the run back to ENGINE_BUILD while a
try is left. Its findings are also on the sheet. It is judgement, not a gate the agent can pass by construction.

### 4.6 Stopping an engine that passes on paper

| Way to game the checks | What stops it |
|---|---|
| hard-code the public expected values | hidden draws (§4); the S4 literal audit |
| detect a test and behave differently | S2 and S3 leave nothing to inspect (no stack, environment, files or time); the sandbox has no network and an empty view of the run; admission and production use the same worker entry point and argument shapes; hidden cases are mixed with public ones in a shuffled order; seeds come from the same range as production seeds |
| add `1e-12 * p` to an output so that a dead parameter "moves" | A6 floors are relative to the output's scale and measured over the full range; `param_reads` exposes parameters read only after the dynamics; the reviewer's checklist |
| compute outputs apart from the dynamics | A5: outputs must equal the harness-read flows; `read_outcomes` gets only the finished sim |
| fit the public set over many try calls | at most 30 calls per try and 2 tries; a public pass with a hidden fail is recorded as `engine_overfit_suspected` (HIGH), and the next try is told only the check ids and output names that failed, never hidden values |
| change the code after admission | files frozen at `0444`, their sha256 in `admission.json` and in the integrity record (`agents/integrity.py`); the adapter refuses a mismatch (§5.1), and `artifact_tampered` (exit 1) follows |
| a card that differs from the code | S5 and A1 |
| claim a parameter is inert | the `inert` list is on the owner's sheet, row by row, with the reviewer's view |
| choose easy benchmarks | A8 and A10 come from the signed dossier and spec; the agent's own checks never gate |

What remains is a wrong model that is consistent with the request, the reductions, the trials and itself, and that
agrees with an independent reading of the same request. The checks do not catch that. The owner's review of the
request (are the reductions and directions the right ones?) and of the sheet is the safeguard, and the sheet says
so.

## 5. Provenance and reuse

### 5.1 Freezing, identity and loading

**Freezing** (end of a successful ENGINE_BUILD):
- every file under `models/ext/engine_<id>/` is listed with its sha256 in `engine_build/frozen.json`;
- the files are set to `0444`;
- each file is added to the integrity record;
- links and hard links are refused (`lib.safe_io` no-follow reads, as for derivations).

**Identity.** `contract.code_fingerprint` hashes the `.py` files beside the engine's class. For the adapter that is
`toolkit/engines/`, which would give every built engine the same fingerprint. The contract therefore gains an
optional `fingerprint_files()` method: when an engine defines it, `code_fingerprint` hashes those files and the
contract. `BuiltEngine.fingerprint_files()` returns the frozen module's files, `toolkit/engines/built.py`,
`built_worker.py` and `admission.py`. `engine_identity` then adds:
- `request_sha256`;
- `admission_sha256`;
- `starsim_version`;
- the lockfile's sha256 (`requirements-agent.txt`).

Batch results, benchmark results and stored rows carry this identity as they do now, so results from other code
are refused (`row_reuse.py`, `compute.py` reuse).

**Loading.** `BuiltEngine(run_engine, admission_sha256, **config)` (`toolkit/engines/built.py`):
1. reads `engine_build/admission.json` from the run directory (`_run_dir` from the spec), checks that its sha256
   equals the signed `admission_sha256`, and checks that its status is `admitted` and the engine sign-off is
   present (`signed`, or `provisional_lead`);
2. re-hashes every frozen file and refuses on any difference;
3. starts one long-lived sandbox worker (`toolkit/engines/built_worker.py`, under bwrap as `scripts/pipeline.py`
   runs derivations) that imports the module from its path under a synthetic name (`importlib.util.
   spec_from_file_location`, never `sys.path`);
4. sends each `run` and `run_batch` to the worker over a pipe, with pickling restricted to the contract's
   dataclasses and plain types.

`scripts/import_audit.py` lists `built_worker.py`'s file import as the one allowed path import, gated by the
admission check. Where no sandbox exists (macOS), the adapter fails closed with `engine_unsandboxed`. The
development flag that derivations use lets it run, and a run that used the flag cannot reach ACCEPT (as
`derive_unsandboxed`). On Coiled workers, `compute.py` already bundles `models/ext/`, and the VMs have `bwrap-sudo`
(`v2-derived-inputs.md` §7).

**Benchmark records.** `benchmark.verified_results(engine)` and `benchmark_coverage.assess` look for results next
to the engine's module. For a built engine they read `engine_build/results.yaml` (written by admission, stamped
with the built identity) and `engine_build/benchmark_coverage.yaml` (generated from the request and dossier by the
harness). The lookup path is the one change.

### 5.2 The card the agent writes

| Field | Who writes it | Notes |
|---|---|---|
| `name`, `version`, `mechanisms`, `components`, `unit_attributes`, `outputs`, `age_groups`, `valid_ranges` | agent | must match the request (A1) |
| `fitted`, `notes` | agent | |
| `benchmarks` | harness | filled from `engine_build/results.yaml` after admission, as `engine.py --card` does for registered engines; the agent leaves it empty (S5) |
| `kind`, `request_sha256` | agent | the harness checks both |
| `parameter_roles` | agent | `dynamics`, `observation` or `cost` per parameter (A6 `param_reads`) |
| `state_vars`, `conservation` | agent | A5 |
| `inert` | agent | each with a reason and the outputs it leaves unchanged; on the sheet |
| `tables` | agent | each literal table with a source and what it stands for (S4) |
| `assumptions`, `limitations` | agent | plain language; on the sheet and in the report's limitations section |
| `self_checks` | agent | reported, never gating |

`ENGINE_NOTES.md` follows the registered engines' notes: the model in equations, what it reports, and what the
data do and do not determine.

### 5.3 What the owner sees at ENGINE_SIGN

`engine_build/admission_sheet.md` holds:
- a header with the engine id, the kind, the files and their sha256, the lines of code, the spend so far, and the
  sentence of §1 on what the checks do not show;
- the request against the card: every structural field, with any difference marked;
- one row per check: the number of cases (public and hidden), the number passed, the worst case and its values;
- the wiring table: each parameter, component and attribute, the outputs it moves, its largest relative change
  over its range, its declared direction and the result;
- the `inert` list with reasons and the reviewer's view of each;
- trial benchmarks: band, computed value, status and the dossier id;
- identifiability: each calibrated parameter's class, coverage and compatible-region width against its prior;
- the reference comparison (if on);
- the assumptions and limitations, and the reviewer's findings.

### 5.4 Reuse across runs

`engine_build/admission.json` records an admission key: sha256 over the frozen files' digest, `request_sha256`, the
dossier entries' sha256, the harness admission code's fingerprint and the lockfile.

A later run (or a child run from `toolkit/spec/amend.py`) whose request, dossier entries and harness code give the
same key can reuse the module and its admission. It copies the files, `admission.json`, `results.yaml` and the
sheet, and re-verifies every hash. It does not draw a new hidden seed or rebuild. The owner still signs the
engine for the new run (the sheet is shown again, marked "reused from run <dir>"). A changed request, dossier or
harness version means a new admission. The module may still be offered to the build task as a starting point,
which is cheap when only the dossier changed.

Hidden seeds are revealed when the run finishes (`engine_build/hidden_reveal.json`, harness-written), so an audit
can re-run every case. Tasks of later runs cannot read other runs (`_search_denied`), so the reveal does not leak
to them (open question 5).

### 5.5 Promotion to the team registry

An admitted engine is part of one run. It moves to `toolkit/engines/<name>/` and `ENGINES` only through a
maintainer PR, under a rule modelled on the two-disease promotion rule (`docs/design/model-library.md` §2):

| Requirement | Why |
|---|---|
| admitted, at the same admission key or a later version, in **two different questions**. For an engine that names a disease, a synthetic eval question counts as one of the two only when what it tests is method or structure, as the rule says for lessons. An engine for a disease-agnostic family (for example an age-structured SEIR with waning) needs **two disease areas** | a model built for one question encodes that question |
| a benchmark pack (`benchmarks/pack.yaml`) holding the generated public cases, the reductions and the trial benchmarks, with **new** hidden draws at promotion, and the reference as `benchmarks/oracles.py` when one was built | registered engines are validated once, by their pack |
| `card.yaml`, `ENGINE_NOTES.md`, `benchmark_coverage.yaml`, `decision_inputs.yaml`, `attribute_binding.yaml`, a `--self-test` that runs `contract.check_engine` and A2, A3 and A5 inline | the registry's existing contract |
| an expert review of the code and notes, recorded in the PR, and the owner's yes | the checks do not judge the biology |
| the `codex_review.py` round and CI, as for any PR | |

`scripts/promote_engine.py <run_dir> <name>` (ε) builds the pack and the folder from the run's admission. It
prints what is missing and changes no registry file. The PR does the rest.

## 6. Failure routing, cost, smoke markers and tests

### 6.1 Failure routing

| Failure | Caught by | Routing |
|---|---|---|
| no registered engine fits and the spec has no build request | pre-sign check | SPEC feedback names `engine.build` as an option (§2.1); after SIGN, `run_failed` as now |
| build task crashes, or reaches its turn or dollar cap without a module | `h_ENGINE_BUILD` | another try within the balance; else `completed_engine_not_admitted` |
| static check fails | §4.1 | feedback with every problem; another try; else not admitted |
| public cases fail at the end of the try | admission | feedback with the full public results; another try; else not admitted |
| public cases pass and hidden cases fail | admission | `engine_overfit_suspected` (HIGH); feedback names only the check ids and outputs; another try; else not admitted |
| reference disagrees (A11) | admission | as for a hidden failure, with both values on the sheet |
| trial benchmark out of band, or a component without one (A8) | `benchmark_coverage.assess` | not admitted for that component. If the request has no signed departure, the run goes to OWNER_DECISION with a request to amend (`paused:awaiting_spec_amendment`). Under `--signoff lead`, the existing amendment path applies (`amended_provisional`) |
| unidentified calibrated parameter (A10) | admission | admitted with a finding on the sheet; ENGINE_SIGN shows it; the decision test covers it or the owner amends |
| reviewer HIGH | `critique-engine` | another try; else on the sheet for the owner |
| no sandbox | adapter | `engine_unsandboxed`, fail closed; the development flag means no ACCEPT |
| owner declines at ENGINE_SIGN | `built_signoff.py decline` | another try with the owner's note; else `completed_engine_declined` |
| a GATE finding about the engine after analysis (`target_stage: ENGINE`, a new value in `VALID_TARGET_STAGE`) | `route_gate` | one repair per run: ENGINE_BUILD with the finding. The files are unfrozen, then re-admitted with a **new** hidden seed and signed again. Analysis outputs from the old identity are invalidated by the identity change. A second repair is refused, and the finding queues for the next run (freeze and batch) |
| an engine file changed after freezing | adapter, integrity check, resume | `artifact_tampered`, exit 1 |
| budget below the floor, or the build balance spent | `_task` | `paused:awaiting_budget` |
| crash or resume during a build or admission | journal; `engine_build/try/ledger.json`; `admission.json` written last and atomically | a completed task is not re-run; an interrupted admission restarts with the same committed hidden seed (it is in `.hidden/`) |

### 6.2 Cost model

The numbers below are proposed caps. The expected values are to be measured in the δ smoke.

| Item | Cap (proposed) | Notes |
|---|---|---|
| build balance | `ENGINE_BUILD_CAP_USD = 40` per run, in `engine_build.spent_usd`; each task's allowance is `min(cap per call, balance left)` | covers build tries, the reference and the reviewer |
| build try | $15 per try, `MODEL_DEEP`, 150 turns; at most 2 tries plus 1 repair | |
| reference | $6, `MODEL_DEEP`, 80 turns, once per admission | open question 2 |
| reviewer | $3, `MODEL_DEEP` | |
| try tool | 30 calls per try, 120 s each, on the agent VM in the sandbox | no Coiled; seconds to minutes |
| admission compute | `ENGINE_ADMIT_COMPUTE_CAP_USD = 15`, through `scripts/compute.py` on Coiled with `vm_job --cap-usd`. Size: A6 is about 4 × (parameters + switches + attributes) × 3 base points (× K seeds if stochastic); A10 is about 100 replicates of the run's calibration | the largest item for a slow engine or a large calibration; A10 replicates are capped in `admission_thresholds.yaml` |
| reuse | $0 for the build; only the copy and the hash checks | §5.4 |

The run's `--max-budget-usd` still caps the total. `budget_ledger.json` gets `stage: ENGINE_BUILD`,
`ENGINE_ADMIT` and `ENGINE_REVIEW` lines, so per-stage costs stay readable.

### 6.3 Smoke markers (real run)

`scripts/smoke_check.py <run> --pipeline v2 --commit <sha>` with:

```
--expect 'run_state.json:history~ENGINE_BUILD'
--expect 'engine_build/request.json:request_sha256'
--expect 'engine_build/frozen.json:files'
--expect 'engine_build/admission.json:status=admitted'
--expect 'engine_build/admission.json:hidden_commitment'
--expect 'engine_build/admission.json:checks.A6.cases'
--expect 'engine_build/admission_sheet.md'
--expect 'engine_build/signoff.json:by'
--expect 'engine_choice.json:engine.ref=toolkit.engines.built:BuiltEngine'
--expect 'engine_build/results.yaml:engine.code_sha256'
--expect 'budget_ledger.json~ENGINE_ADMIT'
```

Only the new code writes these. The δ smoke uses a question that no registered card fits, with staged inputs, under
`--signoff lead`. It is run on the cloud as for any real run, and it alternates the disease area from the previous
retro.

### 6.4 Tests to write first

Each test fails until its code exists. The fixture engines live in `toolkit/engines/built_fixtures/`: small
Starsim modules, each with one planted defect. They are self-test data, not engines (`TEST_ONLY_REFS`).

| Id | Test | Module |
|---|---|---|
| BE1 | a correct two-group SIR fixture passes every check; `admission.json` `status: admitted` | `admission.py` |
| BE2 | a dead parameter fails A6 and names it | `admission.py` |
| BE3 | `1e-12 * p` added to an output fails A6 (relative floor) | `admission.py` |
| BE4 | a parameter read only in `read_outcomes` without `role: observation` fails A6 | `admission.py` |
| BE5 | an unseeded `numpy.random` call fails A2; an order-dependent cache fails A2 | `admission.py` |
| BE6 | a batch path that leaks state across packages fails A3 | `admission.py` |
| BE7 | a model whose population grows by 0.1% a step under `conservation: closed` fails A5 | `admission.py` |
| BE8 | outputs computed from a formula, not from the sim, fail A5's flow equality | `admission.py` |
| BE9 | a module that returns the public expected values from a table passes public A4 and fails hidden A4 (and S4) | `admission.py` |
| BE10 | `import inspect`, `os.environ`, `open(...)` or `getattr(x, name)` are refused by S2 and S3 | `admission.py` |
| BE11 | a card that differs from the request (a range widened, a kind changed) fails A1 | `admission.py` |
| BE12 | an intervention whose effect lies outside the dossier trial's interval fails A8 through `benchmark_coverage.assess` | `admission.py` |
| BE13 | a calibrated pair on a ridge is classed unidentified by A10 and does not gate | `admission.py` |
| BE14 | a reference that disagrees by 5% fails A11 | `admission.py` |
| BE15 | a fix task's write to `models/ext/engine_x/engine.py` is refused; the build task's is allowed | `v2_sdk_tasks.py` |
| BE16 | a frozen file edited after admission: the adapter refuses and the run ends `artifact_tampered` | `built.py`, `stage_machine.py` |
| BE17 | the adapter refuses when the admission's sha256 differs from the signed one, or when the sign-off is missing | `built.py` |
| BE18 | `engine_identity` of two built engines with different files differs; the same files give the same identity | `contract.py`, `built.py` |
| BE19 | a spec with `engine.build` is validated against the stand-in card before sign-off; a missing component is refused at SPEC | `schema.py` |
| BE20 | a build request in a spec whose pre-sign rows include an eligible engine, with a `why` that names no missing mechanism, is refused | `analysis_backend.py` |
| BE21 | the try tool never returns a hidden case; its call cap holds across a crash | `built_tools.py` |
| BE22 | machine: SIGN → ENGINE_CHOICE → ENGINE_BUILD → ENGINE_ADMIT → ENGINE_SIGN pauses `awaiting_engine_signoff`; sign; CONFIGURE | `stage_machine.py` |
| BE23 | machine: admission fails twice, then FINAL `completed_engine_not_admitted` and exit 2 | `stage_machine.py`, `main.py` |
| BE24 | machine: a GATE finding with `target_stage: ENGINE` gets one repair with a new hidden seed; a second is refused | `stage_machine.py` |
| BE25 | resume mid-admission reuses the committed hidden seed; resume mid-build does not re-run a completed try | `stage_machine.py` |
| BE26 | reuse: the same admission key copies the admission without a build; a changed dossier entry does not | `admission.py` |
| BE27 | no sandbox: `engine_unsandboxed`, and with the development flag the gate holds a HIGH non-declarable finding | `built.py` |
| BE28 | the import audit accepts `built_worker.py`'s gated path import and refuses any other | `scripts/import_audit.py` |

`scripts/regression_eval.py` gains planted-defect cases BE2, BE7 and BE9. `scripts/gate_fuzz.py` gains malformed
`admission.json` and `frozen.json` inputs, each of which must give a finding (fail closed).

### 6.5 Rollout (each PR under 800 lines, tests first)

| PR | Content | Done when |
|---|---|---|
| α | the request schema, the stand-in card, the pre-sign checks and sheet section; the failing tests above; the Starsim spike (§3.2: an analyzer added to an unrun sim) | BE19, BE20 pass; the others fail as expected |
| β | the adapter, the sandbox worker, static checks, freezing, identity (`fingerprint_files`), the integrity entries, the write rule | BE10, BE15–BE18, BE27, BE28 pass |
| γ | the check generators, the oracle library, admission with public and hidden draws, `admission.json` and the sheet, the try tool | BE1–BE14, BE21, BE26 pass |
| δ | the states, the build task prompt and powers, the reviewer, sign-off and decline, routing and statuses; the real smoke | BE22–BE25 pass; the §6.3 markers present on a cloud run |
| ε | the reference task (if on), `promote_engine.py` | BE14 on a real reference; a dry promotion of the δ engine lists what is missing |

## 7. Open questions for the owner

| # | Question | Recommended default |
|---|---|---|
| 1 | May a built engine be closed-form (numpy and `scipy.special`, no time loop), as renewal and the package-rate engine are numpy code today? | Yes, as `kind: closed_form`, signed in the request. Anything with state over time must be a Starsim module |
| 2 | Is the independent reference build (A11) on by default? It adds about $6 per admission | On. It is the only check that tests the reading of the mechanism directly |
| 3 | Under `--signoff lead`, may a run with a lead-signed built engine reach ACCEPT? | Yes for test and eval runs, with the report's first paragraph saying the engine was agent-built, harness-admitted and lead-signed. A decision run needs the owner's engine signature |
| 4 | Caps: $40 build balance, $15 admission compute, 2 tries plus 1 repair | As proposed; re-measure after the δ smoke |
| 5 | Reveal the hidden seed at the end of the run, so an audit can re-run every case? | Yes, at FINAL, in a harness file |
| 6 | Engine repair after a GATE finding: how many per run? | One, with re-admission (new hidden seed) and a new signature; after that, findings queue for the next run |
| 7 | Promotion: is "two questions" enough for a disease-specific engine, with "two disease areas" for a generic family? | Yes, plus an expert review in the PR and the owner's yes |
| 8 | Does an unidentified calibrated parameter (A10) block admission? | No. It goes on the sheet, and a decision test must cover it (principle 6), or the owner amends the spec |
| 9 | May `draft_spec` propose `engine.build` without being asked, whenever no registered engine fits? | Yes, only when the harness's pre-sign rows show none eligible; the owner sees it at SIGN |
| 10 | The import allowlist: is `scipy.integrate` excluded from engines? | Excluded. The engine steps on Starsim's grid; ODE solvers stay in oracles and the reference |
| 11 | Do benchmarks the build agent proposes ever gate? | No, they are reported only. Gating benchmarks come from the signed spec and dossier |
| 12 | Which model builds: `MODEL_DEEP` for the build, reference and reviewer? | Yes |
| 13 | May a spec request a build when a registered engine is eligible? | Only with a `why` naming a mechanism of the request that no eligible card has; the harness checks it and the owner signs it |
| 14 | Does a reused admission (§5.4) need the owner's signature again in the new run? | Yes. It is cheap, and the run is a new decision |

## 8. Not in this note

- Building engines in other languages, or around external simulators: excluded by the team rule.
- Letting the agent run arbitrary code: the try tool is the only way it runs anything.
- Changing registered engines through this lane: a registered engine changes by a PR and its pack.
- Data acquisition: ACQUIRE (`docs/design/acquire.md`) precedes SPEC, and acquisition is closed at SIGN, before any
  build.

## 9. Review log

(Filled after the review round.)
