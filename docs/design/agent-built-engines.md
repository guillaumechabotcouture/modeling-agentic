# v2: agent-built engines, admitted by harness checks (design note)

**Status:** written before any code (2026-11-23), at the owner's request. Nothing here is built. Revised the same
day after one Codex review round (§9). File, function, state and field names are proposals. It extends
`architecture-v2.md` §3.3 (the Engine contract), `v2c-orchestration.md` (the states), `v2-derived-inputs.md` (the
sandbox) and the ACQUIRE draft (`docs/design/acquire.md`, branch `design-acquire`). The stage machine is being split
into a package in parallel (branch `split-sm-pkg-v2`): if that lands first, the handlers below move with the
others, and the state names, `EDGES` and fields stay the same.

## 1. The problem, and the guarantee to keep

A v2 run answers a question only with an engine under `toolkit/engines/`. `ENGINES` in
`agents/analysis_backend.py` lists the toy engine, malaria (Starsim), the package-rate engine (empirical),
compartmental (Starsim) and renewal. When no card fits the spec, SPEC is refused before sign-off ("no candidate
engine passes the full checks before sign-off"), or ENGINE_CHOICE ends the run (`run_failed`, "no registered engine
fits the spec"). A question whose decision turns on a mechanism none of these cards has (four dengue serotypes with
secondary-infection severity; a subclinical TB stage across 17 age groups) gets either no answer or an answer from
an engine that leaves that mechanism out.

The owner's decision of 2026-11-23: frontier coding agents already build such bespoke models quickly, so v2 gets a
lane in which an LLM task writes an engine module for the run. The team rule holds: our own code, Starsim for
dynamic models, no R, no malariasimulation.

**The guarantee, restated for an agent-built engine.** Every number still comes from code that the harness ran
and tested. The agent writes the model's mechanics. The harness owns everything around them:
- the interface, which the owner signs in the spec before anything is built;
- the checks, which harness code generates from the signed spec, including cases the agent never sees;
- running the engine, in a sandbox, from a hashed snapshot, through a harness adapter;
- the record of what was admitted (every file's sha256 and every piece of evidence), and the owner's second
  signature on that record.

The analysis cannot construct an agent-built engine that has not passed admission and been signed for this run.

**What this lane does not promise.** The checks can show several things:
- the code implements the signed interface;
- it conserves what the signed compartment graph says;
- each parameter moves the outputs the request says it moves, in the signed direction;
- it reduces to known closed forms;
- its intervention effects match the structured trial declarations;
- an independent build agrees with it.

They cannot show that the biology is right beyond those points. That judgement stays with the reviewer task
(§4.5) and the owner at ENGINE_SIGN. The admission sheet says so in its first paragraph.

**Terms used here.**

| Term | Meaning |
|---|---|
| task | one bounded LLM session (`SdkTasks._session` in `agents/v2_sdk_tasks.py`), with a dollar cap and a turn cap |
| engine request | the part of the signed spec that states what the built engine must implement (§2.1): its interface, domains, compartment graph, expected effects, reductions and trial declarations |
| build task | the task that writes the engine module (`build_engine`, §3) |
| working copy | `models/ext/engine_<id>/`, where the build task writes |
| snapshot | `engine_build/snapshots/<sha>/`: an immutable, content-addressed copy of one try's working copy. Admission and production run only snapshots |
| try tool | a harness tool offered to the build task only, which runs the public checks on its working copy in the sandbox (§3.3) |
| admission | the harness step that runs every check on a snapshot and writes `engine_build/admission.json` |
| public / hidden cases | checks from the same generators, drawn from a seed the agent sees (public) or from a seed only the harness holds (hidden) |
| oracle | an independent reference value: a closed form, a fixed point or an ODE solution (as in `toolkit/engines/*/benchmarks/oracles.py`) |
| reduction | parameter settings, in the request, under which the model must equal a known simpler model |
| wiring check | the check that each signed (input, output) pair moves as the request says (§4.2, A6) |
| candidate engine | `toolkit/engines/built/candidate.py` `CandidateEngine`: the harness's sandboxed runner for an unadmitted snapshot, used by the try tool and admission only |
| adapter | `toolkit/engines/built/__init__.py` `BuiltEngine`: the Engine-contract class the analysis uses for an admitted, signed snapshot |
| sandbox | a bubblewrap runner with an explicit minimal mount set (§5.1); not the derivation runner as it stands |

## 2. Where it sits in the state machine

### 2.1 How the spec declares it

`engine` gains a `build` form. The spec drafter writes it, the owner signs it with the rest of the spec, and
nothing is built without that signature.

```yaml
engine:
  build:
    id: dengue4                        # [a-z0-9_]{3,24}
    kind: starsim_module               # starsim_module | closed_form (open question 1)
    why: >-                            # why no registered engine serves; checked against the full registry (below)
      no registered card has serotype-specific immunity or severity on secondary infection
    mechanisms: [...]                  # plain-language list, as on every card
    request:
      components:                      # each with its domain and its value meaning "not delivered"
        vaccine: {values: [0, 1], absent: 0}
        vector_control: {values: [0, 0.5, 1], absent: 0}
      unit_attributes:                 # each with a domain; an unbounded quantity has a declared test range
        N: {type: continuous, test_range: [1.0e+4, 1.0e+7], meaning: "population (> 0)"}
        foi: {type: continuous, range: [0, 0.5], meaning: "force of infection per serotype per year"}
        age_pop: {type: vector, length: 3, meaning: "population by age group"}
      outputs:
        per_capita: [infections, severe, deaths]
        windows: {sero9: [seroprevalence_9y]}  # window name -> metrics, as the contract's windows.<w>.<m> paths
        cost_drivers: [doses]
      age_groups: [0-4, 5-14, 15+]
      parameters:                      # every parameter the engine has; the analysis draws over exactly these
        - {name: cross_protection_years, kind: fitted, value: 1.5, lo: 0.5, hi: 3.0, units: y, source: "dossier:CP1"}
        - {name: severity_secondary_rr, kind: fitted, value: 8.0, lo: 2.0, hi: 25.0, units: ratio, source: "dossier:SEV1"}
        - {name: n_serotypes_active, kind: fixed, value: 4, domain: {type: integer, values: [1, 4]},
           units: count, source: assumption}
      attribute_binding: {N: required, foi: required, age_pop: required}
      decision_inputs:                 # per component, the inputs the dossier must cover (as decision_inputs.yaml)
        vaccine: [{kind: parameter, name: severity_secondary_rr, why: "..."}]
      expected_effects:                # every parameter, component and attribute: the outputs it must move, with sign
        severity_secondary_rr: {severe: "+", deaths: "+"}
        vaccine: {infections: "-", doses: "+"}
        foi: {infections: "+"}
      inert: {}                        # owner-signed exceptions: {name: {reason, outputs_unaffected}}
      parameter_roles: {}              # owner-signed: a parameter read only after the dynamics (observation | cost)
      compartments:                    # the state graph the balance analyzer checks (A5)
        states: [S, I1, R1, I2, R2]
        transitions:
          - {from: S, to: I1, counts: [infections]}
          - {from: R1, to: I2, counts: [infections, severe]}   # an output counts transitions, never a free formula
        conservation: closed           # closed | births_deaths | open_with_flows
      per_capita_invariant_to_N: true  # only when the model's assumptions imply it (A4)
      stochastic: false
      seed_test_setting: {unit: {foi: 0.1, N: 1.0e+5}}   # a non-degenerate setting for A2's seed test
      reductions:                      # each validated at SIGN: names, domains, oracle arguments, output paths
        - {id: R1, oracle: final_size_lambert, oracle_args: {r0: 2.0},
           set: {n_serotypes_active: 1, cross_protection_years: 0.5},
           unit: {foi: 0.0}, horizon: {...}, compare: {windows.sero9.seroprevalence_9y: {abs: 1.0e-3}}}
      trials:                          # structured, from dossier entries (A8): arms, setting, estimand, interval
        - {id: T1, dossier: VE1, component: vaccine, arms: {...}, setting: {...}, window: [0, 5],
           estimand_type: ratio, estimand_mapping: {...}, published: {lower: ..., upper: ..., level: 0.95}}
      gating_benchmarks: []            # literature benchmarks (A9) the owner makes gating
      reference_tolerance: {rel: 0.01} # A11; at most the harness ceiling
      configs: {default: {}}           # constructor keywords; harness-reserved keywords are refused (§5.1)
```

Caps are not in the spec. They are run settings (§6.2), so raising them never needs a spec amendment.

A spec may also write `engine: {auto: true, candidates: [...], build: {...}}`. The registered candidates are
considered first, and the build runs only when none of them is eligible after sign-off.

**The justification is checked against the whole registry.** `build_request_problems(spec)` (new, in
`agents/analysis_backend.py`) computes the eligible rows over the full `ENGINES` list, ignoring any `candidates`
shortlist, so a drafter cannot leave out an engine that fits. When a registered engine is eligible, `why` must
name a mechanism of the request that no eligible card has (open question 13). The rows and the `why` are on the
sheet.

**Schema (`toolkit/spec/schema.py`).** New `validate_build(spec)` checks:
- every field above, in the contract's vocabulary (`EngineCard`, `ParamSpec`, `KINDS`);
- every parameter has `value`, `lo` and `hi` as `ParamSpec` requires;
- every source is a dossier id or `assumption`;
- `expected_effects` names every parameter, component and attribute, unless it is in `inert`;
- every transition's `counts` names a per-capita output, and every per-capita output is counted by some transition;
- each reduction sets only declared names, to values inside their domains, gives every argument the oracle needs,
  and compares paths that exist in the request (`benchmark.value_path_ok`);
- each trial declaration is complete for the benchmark runner (`benchmark_sources` checks, as for registered
  packs).

A request that fails any of these is refused at SPEC.

**A stand-in engine, not a stand-in card.** `schema.validate` needs an `Engine`. It calls `describe()` and
`parameters()` and resolves attribute binding. New `toolkit/engines/built/requested.py` `RequestedEngine(request)`
is harness code. Its `describe()` and `parameters()` come from the request, and its binding and decision inputs
come from `request.attribute_binding` and `request.decision_inputs`. Its `run()` raises `EngineNotBuilt`.
`schema.with_engine(s, RequestedEngine)` lets the rest of the spec (engine attributes, calibration, priors) be
validated before sign-off as today. `engine_needs` and `card_fits` run against it, so a spec whose options need a
component the request does not declare is refused at SPEC.

The same declaration is the engine's decision inputs everywhere: at EVIDENCE (`toolkit/spec/evidence_dossier.py`
resolves the requested parameters), at SIGN (`signoff.py`'s evidence binding), at the ENGINE_CHOICE evidence gate,
at admission and on resume. `decision_inputs.lookup` gains a branch: an engine whose ref is the built adapter or the
requested stand-in reads the signed request, never a `decision_inputs.yaml` beside a class directory.

**Who drafts it.** `draft_spec` may write `engine.build` only when the harness's pre-sign rows show no eligible
engine, or with a `why` that passes `build_request_problems`. The prompt says so, and the harness checks it.

**Signing.** The sign-off sheet (`decision_spec.signoff_sheet.md`) gains an "Engine to be built" section. It shows,
as tables:
- the request;
- the full-registry rows that `why` cites;
- the generated check plan: how many cases each check will run, which outputs no reduction reaches, and which
  components have no trial.

`toolkit/spec/signoff.py` changes in one way: the persisted record gains the sheet's sha256 (`sha256[SHEET]`). Today
`expect_sheet_sha256` checks the sheet at signing, but the record does not keep its digest, so an audit cannot
show which check plan the owner saw. This applies to every spec signature, not only build specs.

**`--signoff none` refuses a build spec** at SPEC ("an engine build needs a signed request"). No paid build starts
on an unsigned request.

### 2.2 States and edges

```
SIGN -> ENGINE_CHOICE -> ENGINE_BUILD -> ENGINE_ADMIT -> ENGINE_REVIEW -> ENGINE_SIGN -> PREREGISTER | CONFIGURE
                              ^              |    |             |               |
                              +--- a try left ---+-------------+---------------+  (decline with a try left)
                                             |
                                             +--> SPEC  (a signed-spec amendment is needed: A8, A10)
GATE | FIX_LOOP -> ENGINE_BUILD  (engine repair, at most once per run, §6.1)
```

Changes to `agents/stage_machine.py`:

| Item | Change |
|---|---|
| `STATES` | `"ENGINE_BUILD"`, `"ENGINE_ADMIT"`, `"ENGINE_REVIEW"`, `"ENGINE_SIGN"` after `"ENGINE_CHOICE"` |
| `EDGES` | `"ENGINE_CHOICE"` gains `"ENGINE_BUILD"`. New entries: `"ENGINE_BUILD": {"ENGINE_ADMIT", "ENGINE_BUILD", "FINAL"}`, `"ENGINE_ADMIT": {"ENGINE_REVIEW", "ENGINE_BUILD", "SPEC", "FINAL"}`, `"ENGINE_REVIEW": {"ENGINE_SIGN", "ENGINE_BUILD", "ENGINE_REVIEW", "FINAL"}`, `"ENGINE_SIGN": {"PREREGISTER", "CONFIGURE", "ENGINE_SIGN", "ENGINE_BUILD", "FINAL"}`. `"GATE"` and `"FIX_LOOP"` gain `"ENGINE_BUILD"` |
| `LLM_STATES` | gains `"ENGINE_BUILD"` and `"ENGINE_REVIEW"` |
| `RunState` | new field `engine_build: dict` with `id`, `status` (`requested`, `building`, `admitted`, `refused`, `reviewed`, `signed`, `declined`), `tries`, `repairs`, `spent_usd`, `compute_reserved_usd`, `snapshot_sha256`, `admission_sha256`, `hidden_commitment` |
| new constants | `MAX_ENGINE_BUILD_TRIES = 2`, `MAX_ENGINE_REPAIRS = 1`, `ENGINE_BUILD_CAP_USD`, `ENGINE_ADMIT_COMPUTE_CAP_USD` (§6.2) |
| new pause | `AWAITING_ENGINE_SIGNOFF = "awaiting_engine_signoff"`, printed as `paused:awaiting_engine_signoff`, exit 2 |
| new statuses | `completed_engine_not_admitted` and `completed_engine_declined`, both exit 2 (added to `main.py` and the CLAUDE.md table in the PR that adds them) |
| `POST_CHOICE_STATES` | gains the four states; the resume check (`Machine.run`, around line 1689) accepts the pending-build choice record (below) |
| run lock | the whole-run lock, which today applies to lead runs only, applies in every mode while the state is ENGINE_BUILD or ENGINE_ADMIT. Try numbering, the hidden-seed commitment and compute submission all happen under it, so two resumes cannot start concurrent paid attempts |
| `harness_owned()` | gains `engine_build/*` and `models/ext/engine_*`. Every path-taking writer refuses them: deliverables (`_write_deliverable`) and supplied dossiers |

A state file saved before this change has no `engine_build` field; it loads as `{}` and resumes as today.

**How the schema resolves a build spec.** `_resolve_engine_choice` (`toolkit/spec/schema.py`) gains a build branch
for both forms (plain `build`, and `auto` with `build`):
- **before admission is signed:** `engine_choice.json` holds `{"build": {"id", "status": "requested" |
  "building" | "admitted"}}` and no `engine`, and the spec loads with `engine_pending = True`;
- **after ENGINE_SIGN:** it holds `engine: {ref: "toolkit.engines.built:BuiltEngine", config: {...}}`, and the
  branch accepts that ref for a build spec, even outside a signed candidates list. A spec without `build` that names
  `toolkit.engines.built:*` or `toolkit.engines.built.*` is refused (`check_engine_ref`). The adapter is reachable
  only through the build form.

**`h_ENGINE_CHOICE`.** Unchanged for specs without `build`. With `build` (or `auto` + `build` and no eligible
candidate), it:
- records the candidate rows and the pending-build choice in `engine_choice.json`;
- writes `engine_build/request.json` (the request as signed, its sha256 and the spec's signed sha256);
- runs the evidence gate (`evidence_gate`) on the request's decision inputs;
- goes to ENGINE_BUILD.

**`h_ENGINE_BUILD`.**
1. Run the `build_engine` task (§3). When A11 is on and no reference exists yet, also run the `build_reference`
   task (§4.4), which works in a separate folder.
2. Run the static checks (§4.1) on the working copy.
3. On success, snapshot the working copy (§5.1) and go to ENGINE_ADMIT.
4. A failure goes back to ENGINE_BUILD with the problems while tries remain, else FINAL
   `completed_engine_not_admitted`.

A reference task that fails twice leaves A11 `unavailable`, which does not admit while A11 is on.

**`h_ENGINE_ADMIT`.** Deterministic harness code (`toolkit/engines/built/admission.py admit(run_dir)`); no LLM runs
here.
1. Reserve the admission compute against the run balance (§6.2).
2. Draw and commit the hidden seed.
3. Run every check (§4) through the candidate engine.
4. Write the pack, the results, the coverage declaration, `admission.json` and `admission_sheet.md`.

Then:

| Outcome | Next state |
|---|---|
| every gating check passes | ENGINE_REVIEW |
| a check fails that a new try could fix, and a try is left | ENGINE_BUILD, with feedback (§4.6) |
| A8 fails with no signed departure, or A10 finds an unidentified decision-moving parameter that no decision test covers | SPEC, with a `spec_rethink_request.json` naming the amendment. This is the existing route: `paused:awaiting_spec_amendment` under `--signoff owner`, and `toolkit/spec/amend.py --from-request` with `amended_provisional` under `--signoff lead` |
| otherwise | FINAL `completed_engine_not_admitted`, with `engine_build/request_for_owner.md` |

**`h_ENGINE_REVIEW`.** The `critique-engine` task (§4.5) writes `critique_engine.yaml`. A malformed file gets one
repair. A file that is still malformed is recorded as `review_unavailable`, which blocks the lead's signature and is
shown to the owner. A valid HIGH finding sends the run back to ENGINE_BUILD while a try is left; otherwise it goes
on the sheet. Then ENGINE_SIGN.

**`h_ENGINE_SIGN`.**
- `--signoff owner`: pause `awaiting_engine_signoff` until `python3 toolkit/engines/built/signoff.py sign <run_dir>
  --by owner`. The command shows the sheet and binds the sha256 of the evidence set (§5.3). It uses the same
  mechanics as `toolkit/spec/signoff.py`, and the owner key once owner authentication lands.
- `--signoff lead`: the lead signs provisionally (`by: lead`), and the report says so in its first paragraph (open
  question 3). The adapter accepts a lead signature only in a run whose recorded sign-off mode is `lead`, as
  `benchmark_coverage` already checks departures by mode.
- `decline --note "<what is missing>"`: one more ENGINE_BUILD try with the note, if a try is left; else FINAL
  `completed_engine_declined`.
- After signing, `engine_choice.json` gets the built engine's ref and config (§5.1), then PREREGISTER or CONFIGURE
  as today.

## 3. The build task

### 3.1 Its powers

| Power | `build_engine` task | Every other v2 task |
|---|---|---|
| file tools | `Read`, `Glob`, `Grep`, `Write`, `Edit` (`TOOLS`) | as today |
| Bash, Agent, NotebookEdit, WebFetch, WebSearch | refused at PreToolUse (test S16) | refused |
| running code | only through the try tool (`mcp__engine__try`, `mcp__engine__run`, §3.3), served in-process by the harness (`claude_agent_sdk.create_sdk_mcp_server`, as ACQUIRE's fetch tool) | none; the hook refuses `mcp__engine__*` for every other task kind |
| writes | `OWNS["build_engine"] = ("models/ext/engine_<id>/",)` plus `_task_output.json` | `models/ext/engine_` is refused for every other task kind |
| may read | the signed spec, `engine_build/request.json`, the dossier, the derived tables' schema views, `_task_input.json`, its own `engine_build/try/`; in the repository, `toolkit/engines/` (the contract, hooks and the registered engines as examples), `.claude/skills/starsim-dev*/` and the installed `starsim` package source | as today |
| may not read | `engine_build/` other than the above (no snapshots of other tries' admissions, no `.hidden/`, no `reference/`), `analysis/`, any other run | |
| acquired inputs | schema views only (`data/inputs/acquired/_views/`), never the raw files (ACQUIRE §3.3) | as ACQUIRE |
| model | `enforcement.MODEL_DEEP` | as today |
| caps | `max_turns = 150`; dollar allowance per try from the build balance (§6.2) | as today |
| environment | `filtered_env(..., allow_cloud=False)` | as today |

**Read rules for every task.** `_search_denied` today does not stop a `Read` of another run's files; only the
world-model seed task has a cross-run rule. A new `_engine_read_denied`, applied to every task kind and to `Read`,
`Glob` and `Grep`, refuses:
- any path under `engine_build/.hidden/`, `engine_build/hidden_reveal.json` and `engine_build/reference/`;
- for the build task, everything outside its readable list;
- for every task, any path outside the current run directory that resolves into a `runs/` tree.

**Write ownership.** `_allowed` in `agents/v2_sdk_tasks.py` refuses `models/ext/engine_` unless the task kind is
`build_engine`. `agents/fix_loop.py` `in_edit_scope` refuses the same prefix, so a fix plan cannot name an engine
file (it would pass the planner's scope check and then be refused by the hook). `RETHINK_STATES` gains
`ENGINE_BUILD`, so the planner can route an engine finding to the repair edge (§6.1).

### 3.2 What the module must implement

The harness owns `run()`. The agent supplies the mechanics behind a narrower interface, so harness code, not the
agent's code, does the seeding, the accounting and the age weights.

```
models/ext/engine_<id>/
  engine.py            class Model (required); helpers in other .py files of this folder only
  card.yaml            the agent's card fields (§5.2); benchmarks: [] (the harness fills them)
  ENGINE_NOTES.md      the model in words and equations; assumptions; what it does not represent
```

`decision_inputs.yaml` and `attribute_binding.yaml` are not agent files: they come from the signed request.

`kind: starsim_module`:

| Method | Returns | Notes |
|---|---|---|
| `Model(**config)` | | config from `request.configs`; no I/O |
| `describe()` | `EngineCard` | checked against the request (A1) |
| `parameters()` | `[ParamSpec]` | exactly the request's parameters (A1) |
| `build_sim(params, unit, package, horizon)` | an unrun `ss.Sim` | no seed argument: the harness sets `rand_seed` |
| `read_outcomes(sim, unit, package, horizon)` | `Outcomes` | reads only the finished sim; `meta.age_shares` is ignored and replaced (A1) |
| `state_vars()` | `{state: (module, attribute)}` | where each compartment of `request.compartments.states` lives |
| `run_batch_state(...)` (optional) | | a shared burn-in for several packages, as `MalariaEngine.run_batch`; if present, A3 checks it |

`kind: closed_form` (if the owner allows it, open question 1): `evaluate(params, unit, package, horizon) ->
Outcomes`. There is no simulation and no independent state witness, so A5 does not apply. Such an engine relies on
A4, A6, A8 and A11, and the sheet says so.

**What the harness does on each run** (in the sandbox worker, for both the candidate engine and the adapter):
1. Resolve `params` with `contract.resolve_params`, so unknown names fail closed.
2. Replace `unit.id` and `package.id` with opaque digests, so the module cannot branch on a decision unit's or a
   package's name.
3. Wrap the parameters in `TrackedParams`, which records which names are read in `build_sim` and which in
   `read_outcomes`.
4. Call `build_sim`. A sim that is already initialised is refused.
5. Set the seed, add the harness's `BalanceAnalyzer` (an `ss.Analyzer` that reads every declared state each step),
   and run the sim.
6. Call `read_outcomes`.
7. Overwrite `meta.age_shares` with shares computed from the unit's signed age populations, and add `meta.balance`
   and `meta.param_reads`.

The analysis weights age groups with `meta.age_shares` (`toolkit/analysis/model.py`), so the module never supplies
them.

A spike in α confirms that the harness can add an analyzer to an unrun sim on the pinned Starsim version. If it
cannot, the fallback is to have `build_sim` return the sim's arguments and let the harness construct the sim.

**Allowed imports and calls** (an AST scan, failing closed; open question 10):
- Allowed imports: `starsim`, `numpy`, `scipy.special`, `scipy.stats`, `math`, `dataclasses`, `typing`,
  `functools`, `itertools`, `toolkit.engines.contract`, `toolkit.engines.hooks`, and sibling files.
- Not allowed: `scipy.integrate` (ODE solvers belong to oracles and the reference), and anything that touches files,
  processes, the interpreter or the network.
- The allowed libraries have their own file and serialisation functions, so the scan also refuses calls and
  attribute access to a listed set: `numpy.load`, `save`, `savez`, `fromfile`, `tofile`, `memmap`, `loadtxt`,
  `genfromtxt` and `DataSource`; `starsim`'s and `sciris`'s load, save and file helpers; any `.to_*` or `.from_*`
  file method.
- The sandbox's mount set (§5.1) is the second layer: there is nothing to read.

### 3.3 The try tool

Frontier agents build models quickly because they run their code. The build task has no shell. It gets two
harness tools instead.

| Tool | What it does | Limits |
|---|---|---|
| `mcp__engine__try(checks: [ids] \| "all")` | snapshots the current working copy into a scratch snapshot, then runs on it the static checks and the **public** cases of the named checks, through the candidate engine in the sandbox. It writes `engine_build/try/<n>.json`, which the task can read, and returns a summary of at most 8 KB: each case's id, inputs, expected value, computed value, pass or fail, and the last frames of any traceback | 30 calls per try, 120 s wall and 2 GB per call, one call at a time, all under the run lock; counted in `engine_build/try/ledger.json`, so the count survives a crash |
| `mcp__engine__run(unit, package, params, horizon)` | one candidate run on inputs the task chooses within the request's domains; returns the `Outcomes` and the balance trace (capped) | counted in the same 30 |

The tools never run hidden cases. They use the same candidate engine and generators that admission uses.

### 3.4 How it gets data

The engine reads no data. The AST scan refuses file access, and the sandbox mounts nothing but the interpreter,
the allowed libraries and the snapshot. Everything reaches the engine through the contract:
- unit attributes, which the signed spec binds from staged or derived tables (`decision_units.engine_attributes`);
- parameters, whose values and ranges come from the signed request and the dossier;
- the configuration.

The task may read the derived tables' schema views and the dossier, so it can choose sensible dimensions. A
constant the module needs (a contact matrix, a life table) arrives as a unit attribute or a parameter the request
declares, or as a sourced table on the card (S4). It is never a file in the folder.

### 3.5 The packet and the prompt

`h_ENGINE_BUILD` builds `_task_input.json` with:
- the request;
- the contract's docstring;
- the list of checks with what each tests (§4.2) and its public cases;
- the oracle library's function signatures;
- the module layout (§3.2) and the allowed imports;
- on a retry, the feedback (§4.6).

The prompt is `COMMON` plus the decision design principles, as for every task. It adds:
- the engine carries trial effects over to the setting (principle 3);
- its own burden is a validation check, not the basis of the decision;
- it has exactly the parameters the request names.

## 4. The admission gate

Every check is deterministic harness code in `toolkit/engines/built/admission.py`. Its cases come from generators
that read the signed request, the spec and the dossier (§4.3). The build task chooses none of them. Each generator
runs twice:
- with the **public seed**, which the try tool uses and the agent sees;
- with the **hidden seed**, drawn at admission by `secrets.token_bytes`, held in `engine_build/.hidden/` (a harness
  path no task may read, §3.1), and committed as its sha256 in `admission.json` before any hidden case runs.

The hidden set differs from the public one in three ways:
- **Draws:** other base points inside the domains, other seeds and another order.
- **The decision's own inputs:** every signed decision unit's actual attribute values (all of them when there are
  at most 200 units, else a stratified sample of 200 plus every unit at an attribute's extreme), crossed with every
  package the options allow (all, or a covering sample when there are more than 64), on the spec's actual horizon
  and windows.
- **Invariance:** each hidden case runs twice with different opaque ids, and the results must be identical.

So an engine that behaves correctly on generated cases but differently on one real unit or one package combination
is tested on that unit and that combination. Because the harness replaces ids (§3.2), the module cannot recognise a
unit by name.

### 4.1 Static checks (before any run)

| Id | Check | What it catches | What it cannot catch |
|---|---|---|---|
| S1 | files: only `.py`, `card.yaml` and `ENGINE_NOTES.md`; no links; every file with link count 1; no binary or data files; at most 300 KB in all | precomputed answers shipped as data | answers written as code literals (S4) |
| S2 | AST import allowlist (§3.2) | file, process, network and interpreter access; external simulators (R bridges, malariasimulation) | nothing at run time on its own; the sandbox is the second layer |
| S3 | banned names, calls and attributes: `open`, `exec`, `eval`, `compile`, `__import__`, `globals`, `locals`, `vars`, `breakpoint`, `input`; `getattr`, `setattr` and `delattr` with a non-literal name; dunder attribute access other than defining `__init__`; the library I/O list (§3.2). Module level holds only imports, constants, functions and classes | test-mode detection (stack inspection, the environment), code generated at run time, file I/O through numpy or Starsim | a module that behaves differently on inputs it can see (the hidden set, which includes the real units, answers this) |
| S4 | literal audit: an array or sequence of more than 16 numeric literals must be a card `tables:` entry with a source and the parameter or attribute it stands for | lookup tables of expected answers; undeclared contact matrices and life tables | a few scalar constants tuned to pass (the request fixes the parameter list, and A6 and A11 cover the rest) |

The card's agreement with the code (`describe()` against `card.yaml`) calls agent code, so it is run check A1, not a
static check.

### 4.2 Run checks

All run through the candidate engine in the sandbox.

**"Moves."** A change counts as moving an output when it is larger than `max(abs_floor, rel_floor * |base|)`.
For a stochastic engine, the paired change over K common-random-number seeds must have its whole 95% interval
beyond the floor. The floors are in `toolkit/engines/built/thresholds.yaml`, never in the request.

**Input domains.** Generators sample each input by its declared domain type. Continuous inputs get Latin hypercube
draws (`benchmark.lhs`) inside the range or the declared `test_range`. Integer and categorical inputs get every
value, or a covering sample of them. Vector inputs get scaled versions of the signed units' vectors.

| Id | Check | Cases (generated from) | Catches | Cannot catch |
|---|---|---|---|---|
| A1 | conformance. `contract.check_engine` on each case, plus `check_built_outputs`: every declared window metric inside every window; every per-capita outcome and cost driver finite and ≥ 0; `describe()` equal to `card.yaml`; the parameter list equal to the request's on name, kind, value, `lo`, `hi`, units and source; components, unit attributes, outputs and age groups equal to the request's | every component value; inputs at the ends and inside their domains; the hidden set | missing or empty metrics, negative drivers, a range narrowed or widened from what was signed, a card that says one thing while the code does another | whether the outputs mean what their names say (A4, A5, A8, A11) |
| A2 | determinism under seed. The same seed gives identical results in separate processes, with different `PYTHONHASHSEED` values and different case orders. At `request.seed_test_setting`, a stochastic engine's runs with different seeds differ, and a deterministic engine's do not | 8 hidden cases plus the seed-test setting | hidden state carried across calls, order dependence, set or dict iteration leaking into results, unseeded randomness | randomness that is seeded but wrong (A4, A11) |
| A3 | batch equals run: `run_batch` equals `run` pair by pair (rel 1e-12, as `probe_batch_vs_run` in the compartmental oracles); skipped when the module has no batch path | 4 units × all packages | a shared burn-in that leaks one package's state into another | |
| A4 | analytic and oracle cases: each `request.reductions` entry, plus identities the harness derives: all components at their absent values equals the no-intervention arm; per-capita outcomes do not change with N, checked only when the request declares `per_capita_invariant_to_N` | reductions × 3 hidden settings of the inputs the reduction leaves free | wrong core dynamics in any case that reduces to a known model; an intervention that acts when it is off | errors in the parts no reduction covers (the sheet lists those outputs) |
| A5 | mass balance and population conservation, from the harness's own trace of the signed states. Every state ≥ 0 at every step. The sum of states is N0 (closed), N0 + births − deaths (births_deaths), or follows the declared flows (open_with_flows), to 1e-9 relative (exact integers for an agent-based model). Each step's change in each state equals its inflows minus its outflows over the signed transitions. Each per-capita output equals the count of the transitions the request says it counts | 6 hidden cases at the ends of the domains, and the hidden decision units | leaks, double counting, negative states, and outputs computed apart from the dynamics: an output must be a transition count of the signed graph | a state the module keeps outside `state_vars` that changes the dynamics; the reviewer compares the code with the declaration. Not applicable to `closed_form` |
| A6 | **sensitivity wiring**. Each parameter, component value and unit attribute is set to each end and two interior points of its domain, with the rest at a hidden base point. It must move **every output named for it in `request.expected_effects`**, in the decision window, at 2 or more of 3 hidden base points. Every output must be moved by some input. `meta.param_reads`: a parameter that is never read fails; one read only in `read_outcomes` must be listed in the request's `parameter_roles`. The only exceptions are the request's owner-signed `inert` entries | inputs × (2 ends + 2 interior) × 3 base points | dead parameters, switches that do nothing, outputs that never change, a parameter wired to the wrong output, effects computed after the dynamics | a wired parameter whose effect has the wrong size (A8, A11) |
| A7 | declared directions: each sign in `expected_effects` holds at every A6 point. Because A6 requires the pair to move, a sign can never pass on points where nothing changes | from A6 | wrong signs, such as an intervention that raises cases | |
| A8 | trial benchmarks: one `toolkit/engines/benchmark.py` trial benchmark per `request.trials` entry (arms, setting, window, estimand and published interval), through the candidate engine. Then the **built-engine coverage policy** (below) | the signed trial declarations | intervention effects that disagree with the trials (principles 3 and 4) | components with no trial (they need a signed departure) |
| A9 | literature benchmarks: values in the dossier and in matching `expert_priors.yaml` entries (burden by age, R0 ranges, seroprevalence by age), as `value` benchmarks with bands. Reported, and gating only for those in `gating_benchmarks` | the dossier and priors | outputs far from what the field reports | a benchmark no one transcribed |
| A10 | identifiability on the calibration targets, by simulation-based calibration through the **run's own calibration path**. A harness calibrator (`toolkit/engines/built/calibrators.py calibrate_as_run`) wraps `toolkit/analysis/core.py`'s dispatch in the coverage runner's `(make_engine, data, ...) -> {intervals, classes}` form: `emulator_inversion` goes to `calibration.invert`, `likelihood` and `posterior` to `fit_likelihood` and `fit_posterior_panel`, and `calibration.joint` to `toolkit/analysis/joint_calibration.py`. The generator draws true values of the calibrated parameters from their priors (hidden seed) and makes synthetic targets with the spec's observation model at the spec's calibration units. The calibrator recovers them. Interval coverage is scored as the runner scores it. `scripts/identifiability.py`'s Fisher and profile-likelihood classes are added at 3 true points | the spec's calibration section | parameters the targets cannot see, ridges such as R0 × s0 (renewal ENGINE_NOTES, RW4), a calibration that cannot recover its own truth | identifiability under a wrong model: it tests the model against itself |
| A11 | independent reference (§4.4): equivalence with a second implementation written from the request alone, on every declared output at hidden cases | hidden cases from A4, A6 and the decision units | misreadings of the request that the two builds do not share; wrong effect sizes | a misreading both builds share; an error in the request itself |

**The built-engine coverage policy (A8).** `benchmark_coverage.assess` does not by itself enforce "only `in_band`
passes":
- a component listed in `effects_from_spec` passes without a benchmark or a departure;
- a benchmark bound to a trained posterior's chosen point can pass when its stored default row is out of band.

For a built engine, admission calls `assess` with a stricter policy (`policy="built"`, new):
- no `effects_from_spec` entries exist. The harness generates the coverage declaration from `request.trials`, and a
  component without a trial is uncovered;
- the posterior choice-point rule does not apply, because a built engine has no trained posterior;
- only `in_band` passes. An uncovered or failing component blocks unless the signed spec declares an
  `engine_departures` entry for it.

**Which checks gate.** A1 to A8 and A11 gate admission. A9 gates only for `gating_benchmarks`. A10 does not gate
on its own (open question 8). An unidentified calibrated parameter that moves a decision output must already be
covered by a decision test over its range (principle 6); if it is not, the run goes to SPEC for an amendment (§2.2).

### 4.3 Checks generated from the spec, not chosen by the agent

| Source (signed) | Generated checks |
|---|---|
| request domains, components, parameters | A1, A2, A6 cases |
| `decision_units`, `options`, horizon, windows | the hidden set's real units, packages and horizon (§4); A1 and A5 on them |
| `expected_effects`, `inert`, `parameter_roles` | A6, A7 |
| `compartments`, `per_capita_invariant_to_N`, `stochastic`, `seed_test_setting` | A5, the A4 identities, A2 |
| `reductions` | A4 oracle cases, from the oracle library `toolkit/engines/built/oracles.py`. The module name ends in `oracles`, as the import guard's oracle policy requires. It re-exports the closed forms in the existing oracle modules (`oracle_final_size_lambert`, `oracle_herd_immunity`, `oracle_wallinga_lipsitch`, `oracle_branching_single`, `oracle_seir_growth_r0`, `oracle_final_size_multigroup`, `oracle_ode_final_size`, `oracle_cohort_susceptible`) and adds a few generic ones: SIS endemic prevalence 1 − 1/R0, the stable age distribution of a constant-rate life table, constant-hazard survival, and a multigroup SEIR ODE |
| `trials` | A8 |
| dossier values, `expert_priors.yaml`, `gating_benchmarks` | A9 |
| `calibration` | A10 |

The calibrator module sits under `toolkit/engines/`, which the import guard's calibrator policy allows.

The agent may add its own cases to `card.yaml` (`self_checks:`). They run and are reported, never replace a
generated check, and never gate (open question 11).

**Gaps in the request.** A reduction the request does not declare is not tested. The pre-sign sheet shows which
outputs no reduction reaches and which components have no trial. The spec critics (`toolkit/spec/spec_critique.py`)
gain one item: "a built engine whose request has no reduction for a core mechanism".

### 4.4 The independent reference

A second bounded task, `build_reference`, runs in ENGINE_BUILD and writes `engine_build/reference/reference.py`.
- It works from the request and the dossier only. It cannot read the working copy or the snapshots, and the build
  task cannot read the reference.
- It is a test oracle in the sense of the 2026-11-01 owner decision on `oracles.py`. It may use `scipy.integrate`
  and plain loops, it is never an engine, and it is never exposed to the analysis.
- It runs in the same sandbox and has its own snapshot.

A11 is an equivalence test, not a difference test:
- **Deterministic engine:** every compared output must lie within `reference_tolerance`. The request's tolerance
  may not exceed the harness ceiling (`thresholds.yaml`, 5% relative).
- **Stochastic engine:** the 95% interval of the paired mean difference over K seeds, at a large N, must lie
  entirely inside the tolerance. If it does not and the interval is too wide to decide, the result is
  `inconclusive`. K is doubled up to a cap. A result that is still `inconclusive` does not admit, and the sheet says
  the comparison was underpowered. A noisier model therefore needs more seeds, never a looser test.

A disagreement is not blamed on either side. The sheet shows both values, and the next build try gets the case ids
and output names. Open question 2 asks whether A11 is on by default.

### 4.5 The engine reviewer

`critique-engine` runs in ENGINE_REVIEW. It has read access to the snapshot, `ENGINE_NOTES.md`, `admission.json`
and the request. Its checklist:
- the notes match the code;
- every state in the code is among the signed states;
- there are no additive terms that exist only to move an output (an `epsilon * p` pattern);
- each parameter source matches the dossier;
- the assumptions are listed.

It writes `critique_engine.yaml` in the blocker schema. This is judgement, not a gate the agent can pass by
construction. Its findings are on the sheet.

### 4.6 Stopping an engine that passes on paper

| Way to game the checks | What stops it |
|---|---|
| hard-code the public expected values | hidden draws; the S4 literal audit |
| behave correctly on generated cases and differently on a real unit or package | the hidden set includes the signed units, packages and horizon (§4); ids are opaque and invariance is checked |
| detect a test | S2 and S3 leave nothing to inspect (no stack, environment, files or time); the sandbox mounts nothing else; admission and production use the same worker and argument shapes; ids are opaque; seeds come from the same range as production seeds |
| add `1e-12 * p` to an output so that a dead parameter moves | A6 requires each signed (input, output) pair to move above a relative floor; `param_reads`; the reviewer |
| wire a parameter to the wrong output | A6 and A7 test the signed pairs, not any output |
| compute outputs apart from the dynamics | A5: an output is a transition count of the signed graph, measured by the harness |
| supply wrong age weights | the harness computes `meta.age_shares` |
| choose its own exceptions | `inert` and `parameter_roles` come from the signed request only |
| fit the public set over many try calls | 30 calls per try and 2 tries; a public pass with a hidden fail is recorded as `engine_overfit_suspected` (HIGH), and the next try is told only check ids and output names, never hidden values |
| change the code after admission | production runs only the snapshot, loaded from bytes the harness verified (§5.1); snapshot checks at every later state; `artifact_tampered` |
| make the reference test weak by adding noise | A11 requires the whole interval inside the tolerance; an underpowered result does not admit |
| choose easy benchmarks | A8 and A10 come from the signed request and spec; the agent's own checks never gate |

What remains is a wrong model that is consistent with the request, the graph, the reductions, the trials and
itself, and that agrees with an independent reading of the same request. The checks do not catch that. The owner's
review of the request (are these the right reductions, effects and graph?) and of the sheet is the safeguard, and
the sheet says so.

## 5. Provenance and reuse

### 5.1 Snapshots, the sandbox, identity and loading

**Snapshots.** At the end of a successful ENGINE_BUILD (and for each try-tool call, as a scratch snapshot), the
harness snapshots the working copy:
1. It reads every file of the working copy once, through no-follow reads (`lib.safe_io`), and refuses links and
   files with a link count above 1. `read_nofollow` alone accepts hard links.
2. It writes the bytes to `engine_build/snapshots/<sha>/`, where `<sha>` is the sha256 of the sorted (name, sha256)
   list, so the folder's exact membership is part of its name. The folder and files are set to `0555` and `0444`.
3. It records the list in `engine_build/snapshots/<sha>.json`.

The working copy stays writable for the next try, so a failed admission does not leave the build task with
read-only files. Production never reads the working copy.

**Loading from verified bytes.** The worker never imports from a path:
1. The harness process reads the snapshot's files and checks the membership and every sha256.
2. It sends the bytes to the worker.
3. The worker compiles each module from those bytes into a fresh module namespace (`types.ModuleType`, `exec` of
   `compile(bytes, "<snapshot>/<name>", "exec")`), with sibling imports resolved from the same byte set.

Nothing is read twice, so no file can be swapped between the check and the import. `scripts/import_audit.py` lists
this compile in `toolkit/engines/built/worker.py` as the one allowed dynamic load, and only after the byte check.

**The sandbox.** `scripts/pipeline.py _bwrap_cmd` binds `/` read-only and then masks the run directory, so the
derivation sandbox can still read host files outside the run. Engine workers use a new `_bwrap_engine_cmd` with an
explicit minimal mount set:
- the interpreter prefix and the site-packages folders of the allowed libraries (read-only);
- `toolkit/engines/contract.py` and `hooks.py` (read-only);
- `/proc` and a minimal `/dev`;
- a private tmpfs as the only writable path.

It has no home directory, no run directory and no repository beyond those files. It has no network and the same
resource limits as derivations, it runs `python -I` with a cleared environment, and it keeps the existing two kinds
(`bwrap`, `bwrap-sudo`). Where no sandbox exists (macOS), the worker fails closed with `engine_unsandboxed`. The
development flag lets it run, and a run that used the flag cannot reach ACCEPT (as `derive_unsandboxed`).

**Identity.** `contract.code_fingerprint` hashes the `.py` files beside the engine's class. For the adapter that
directory is `toolkit/engines/built/`, which would give every built engine the same fingerprint. The contract gains
an optional `fingerprint_files()`; when an engine defines it, `code_fingerprint` hashes those files and the
contract. `BuiltEngine.fingerprint_files()` returns the snapshot's files and the harness files of
`toolkit/engines/built/`. `engine_identity` then adds:
- `request_sha256` and `config_sha256` (the complete effective configuration);
- `starsim_version`;
- the dependency digest that `scripts/compute.py` already computes over `requirements.txt`,
  `requirements-agent.txt` and `requirements-cloud.txt`.

The identity does **not** include the admission's sha256. Results are stamped with the identity, the admission
binds the results' digests, and the signature binds the admission, so there is no circular dependency.

**How production constructs the engine.** `schema.load_engine` calls an engine class with `**config` only, and the
run directory reaches candidate selection but not the constructor. For the built ref, `load_engine` gains a branch
that passes the run directory it loaded the spec from (`s.base`) as a harness argument. The harness-reserved
keywords are `run_dir`, `run_engine`, `snapshot_sha256`, `admission_sha256` and `config_sha256`. They are refused
in `request.configs` and in any spec `config`, so no spec can set them.

`BuiltEngine(run_dir=..., **config)`:
1. reads the signed spec and checks that it has `engine.build` with the same `id`;
2. reads `engine_build/signoff.json` and checks that its signer is allowed in the run's recorded sign-off mode;
3. reads `admission.json`, checks its sha256 against the signature and its status;
4. checks that the snapshot sha256 and the effective configuration's sha256 equal the admitted ones;
5. verifies the snapshot's bytes (above) and starts the worker.

Every entry path goes through this constructor, so naming the adapter in a spec achieves nothing: the schema refuses
the ref outside the build form (§2.2), and the constructor refuses without a matching build request, admission and
signature. The candidate engine cannot be named at all: `import_guard` refuses `toolkit.engines.built.candidate`
for every policy.

**After construction.** `ArtifactIntegrity` is not active in v2 task sessions, so the design does not rely on it.
New `verify_engine_snapshot(run_dir)` runs at every state entry after ENGINE_SIGN, before every task start, on
resume, before WRITE and in the final gate. A mismatch is terminal: `artifact_tampered`, exit 1.

**Benchmark records.** `benchmark.verified_results(engine)` reads `engine.results_path`, checks the producer and
`engine_identity`, then verifies the rows against a pack. The results loader looks for a sibling `pack.yaml`, while
`benchmark_coverage` looks for `benchmarks/pack.yaml`. Admission therefore writes one authoritative set:
- `engine_build/bench/pack.yaml`;
- `engine_build/bench/results.yaml`, stamped with the built identity;
- `engine_build/bench/benchmark_coverage.yaml`.

`BuiltEngine` exposes `results_path` and a new `pack_path` attribute. Both loaders use `pack_path` when an engine
defines it.

**Coiled workers.** `scripts/compute.py make_bundle` ships the run's `models/` and `data/`. It gains the production
subset of `engine_build/`:
- the snapshot and its list;
- `admission.json` and `signoff.json`;
- `request.json`;
- `bench/`.

It never ships `.hidden/`, `reference/`, `try/` or the working copy. The batch fingerprint covers the shipped
files, and the VMs have `bwrap-sudo` (`v2-derived-inputs.md` §7).

### 5.2 The card the agent writes

| Field | Who writes it | Notes |
|---|---|---|
| `name`, `version`, `mechanisms`, `components`, `unit_attributes`, `outputs`, `age_groups`, `valid_ranges` | agent | equal to the request (A1) |
| `notes` | agent | |
| `benchmarks` | harness | filled from `bench/results.yaml` after admission, as `engine.py --card` does for registered engines; the agent leaves it empty |
| `kind`, `request_sha256` | agent | checked by the harness |
| `tables` | agent | each literal table with a source and what it stands for (S4) |
| `assumptions`, `limitations` | agent | plain language; on the sheet and in the report's limitations section |
| `self_checks` | agent | reported, never gating |

Parameters, roles, `inert`, states, transitions, binding and decision inputs are not on the agent's card: they are
in the signed request.

### 5.3 What the owner sees and signs at ENGINE_SIGN

`engine_build/admission_sheet.md` holds:
- a header with the engine id, kind, snapshot sha256, lines of code and spend, and the sentence of §1 on what the
  checks do not show;
- one row per check: the number of cases (public, hidden and real-unit), the number passed, the worst case and its
  values;
- the wiring table: each signed (input, output) pair, its largest relative change and its direction result;
- trial benchmarks: band, computed value, status and dossier id;
- identifiability: each calibrated parameter's class, coverage and compatible-region width against its prior;
- the reference comparison: equivalent, inconclusive or not, and with what power;
- the assumptions, limitations and the reviewer's findings.

The signature (`engine_build/signoff.json`) binds the sha256 of:
- `admission.json`;
- the sheet;
- the snapshot;
- `bench/pack.yaml`, `bench/results.yaml` and `bench/benchmark_coverage.yaml`;
- the reference's snapshot and its comparison record.

`admission.json` itself lists the same digests, so changing a computed benchmark row after signing breaks the
chain. Downstream decision tests read those rows.

### 5.4 Reuse across runs

`admission.json` records a **reuse key**. It is the sha256 over the resolved inputs of every check:
- the snapshot;
- the request (including trials, reductions, effects and graph);
- the spec sections the generators read: decision units and their data, options, horizon and windows, calibration
  and its target data, `engine_departures`;
- the dossier entries;
- `thresholds.yaml`, the oracle library and the harness's admission code;
- the dependency digest.

A later run (or a child run from `toolkit/spec/amend.py`) with the same key can reuse the admission:
- it copies the snapshot, `admission.json`, `bench/` and the sheet, and re-verifies every hash;
- it does not draw a new hidden seed or rebuild;
- the owner signs the engine again for the new run, and the sheet says "reused from run <dir>".

When any of those inputs differ, a new admission runs. For example, an amendment that changes only the calibration
targets re-runs A10 and everything else, because the key changes. The snapshot may still be offered to the build
task as a starting point.

Hidden seeds are revealed when the run finishes (`engine_build/hidden_reveal.json`), so an audit can re-run every
case. The cross-run read rule (§3.1) keeps tasks of later runs from reading it (open question 5).

### 5.5 Promotion to the team registry

An admitted engine belongs to one run. It moves to `toolkit/engines/<name>/` and `ENGINES` only through a maintainer
PR, under a rule modelled on the two-disease promotion rule (`docs/design/model-library.md` §2):

| Requirement | Why |
|---|---|
| admitted in **two different questions**, at the same reuse key or a later version. For an engine that names a disease, a synthetic eval question counts as one of the two only when what it tests is method or structure, as the rule says for lessons. An engine for a disease-agnostic family (for example an age-structured SEIR with waning) needs **two disease areas** | a model built for one question encodes that question |
| a benchmark pack (`benchmarks/pack.yaml`) holding the generated public cases, the reductions and the trial benchmarks, with **new** hidden draws at promotion, and the reference as `benchmarks/oracles.py` when one was built | registered engines are validated once, by their pack |
| `card.yaml`, `ENGINE_NOTES.md`, `benchmark_coverage.yaml`, `decision_inputs.yaml` and `attribute_binding.yaml` (from the request), and a `--self-test` that runs `contract.check_engine` with A2, A3 and A5 inline | the registry's existing contract |
| an expert review of the code and notes, recorded in the PR, and the owner's yes | the checks do not judge the biology |
| the `codex_review.py` round and CI, as for any PR | |

`scripts/promote_engine.py <run_dir> <name>` (ε) builds the pack and the folder from the run's admission. It prints
what is missing and changes no registry file. The PR does the rest.

**A gap in the current registry that this note does not fix.** `candidate_entries` considers a named engine that is
not in `ENGINES`, and the import guard resolves any Engine class under `toolkit.engines`. For example,
`toolkit.engines.hooks:ToyScheduleEngine` can be named in a spec. So `ENGINES` is a list of candidates for `auto`,
not an admission boundary. The built lane closes this for its own classes (§5.1). A separate fix should restrict
named engines to registered refs (open question 15).

## 6. Failure routing, cost, smoke markers and tests

### 6.1 Failure routing

| Failure | Caught by | Routing |
|---|---|---|
| no registered engine fits and the spec has no build request | pre-sign check | SPEC feedback names `engine.build` as an option; after SIGN, `run_failed` as now |
| a build spec under `--signoff none` | SPEC | refused: a build needs a signed request |
| build task crashes, or reaches its turn or dollar cap without a module | `h_ENGINE_BUILD` | another try within the balance; else `completed_engine_not_admitted` |
| reference task fails twice | `h_ENGINE_BUILD` | A11 `unavailable`; with A11 on, not admitted |
| static check fails | §4.1 | feedback with every problem; another try; else not admitted |
| public cases fail | admission | feedback with the full public results; another try; else not admitted |
| public cases pass and hidden cases fail | admission | `engine_overfit_suspected` (HIGH); feedback names check ids and outputs only; another try; else not admitted |
| reference not equivalent, or inconclusive at the seed cap | admission | as for a hidden failure, with both values and the power on the sheet |
| trial benchmark out of band, or a component without a trial, and no signed departure | admission (built policy) | SPEC with a rethink request: `paused:awaiting_spec_amendment` (owner) or `amend.py --from-request` and `amended_provisional` (lead) |
| unidentified decision-moving parameter with no covering decision test | admission | the same SPEC route |
| reviewer HIGH | ENGINE_REVIEW | another try; else on the sheet |
| reviewer output malformed after one repair | ENGINE_REVIEW | `review_unavailable`: blocks the lead's signature; shown to the owner |
| no sandbox | worker | `engine_unsandboxed`, fail closed; with the development flag, no ACCEPT |
| owner declines at ENGINE_SIGN | `built/signoff.py decline` | another try with the owner's note; else `completed_engine_declined` |
| a GATE finding about the engine after analysis (`target_stage: ENGINE`, a new value in `VALID_TARGET_STAGE`; the fix planner may route to `ENGINE_BUILD`) | `route_gate`, `fix_loop` | one repair per run: ENGINE_BUILD with the finding, then a new snapshot, a new admission with a new hidden seed, review and signature. `h_GATE`'s back-edge bookkeeping (clearing `post_write`, a new round, the report withheld until rewritten) applies to this edge as to SPEC and ENGINE_CHOICE. The analysis pipeline steps declare `engine_build/admission.json` and the snapshot list as inputs, so the identity change re-runs them. A second repair is refused, and the finding queues for the next run |
| a snapshot file changed | `verify_engine_snapshot` | `artifact_tampered`, exit 1 |
| a writer targets `engine_build/` or `models/ext/engine_` | `harness_owned()`, `_allowed`, `in_edit_scope` | refused |
| run balance below the floor | `_task`, `step()` | `paused:awaiting_budget`, as today |
| build balance spent while the run has money | `_task` | `paused:awaiting_budget` with the reason `engine_build_cap`; resume with a higher `--engine-build-usd` (a run setting, up to a harness maximum) |
| admission compute cannot be reserved | `h_ENGINE_ADMIT` | `paused:awaiting_budget` before any dispatch |
| two resumes of the same run | run lock | the second refuses to start |
| crash or resume during a build or admission | journal; `try/ledger.json`; `admission.json` written last and atomically; reserved compute reconciled from the job record before any retry | a completed task is not re-run; an interrupted admission restarts with the same committed hidden seed |

### 6.2 Cost model

The numbers below are proposed caps. The expected values are to be measured in the δ smoke.

| Item | Cap (proposed) | Notes |
|---|---|---|
| build balance | `ENGINE_BUILD_CAP_USD = 40` per run (run setting `--engine-build-usd`, up to a harness maximum), held in `engine_build.spent_usd`; each task's allowance is `min(cap per call, balance left, run allowance)` | covers build tries, the reference and the reviewer |
| build try | $15 per try, `MODEL_DEEP`, 150 turns; at most 2 tries plus 1 repair | |
| reference | $6, `MODEL_DEEP`, 80 turns, once per admission | open question 2 |
| reviewer | $3, `MODEL_DEEP` | |
| try tool | 30 calls per try, 120 s each, on the agent VM in the sandbox | no Coiled |
| admission compute | `ENGINE_ADMIT_COMPUTE_CAP_USD = 15`, through `scripts/compute.py` on Coiled with `vm_job --cap-usd`. The cap is **reserved against the run's balance** before dispatch (charged to `RunState.spent_usd` through `agents/run_budget.py`, then reconciled to the measured cost), so `--max-budget-usd` covers it | A6 runs about 4 × inputs × 3 base points (× K seeds if stochastic); A10 runs about 100 replicates of the run's calibration; the hidden set adds up to 200 units × 64 packages × 2 |
| reuse | $0 for the build; only the copy and the hash checks | §5.4 |

`budget_ledger.json` gets `stage: ENGINE_BUILD`, `ENGINE_ADMIT` and `ENGINE_REVIEW` lines, so per-stage costs stay
readable.

### 6.3 Smoke markers (real run)

`scripts/smoke_check.py <run> --pipeline v2 --commit <sha>` with:

```
--expect 'run_state.json~"ENGINE_BUILD"'
--expect 'run_state.json~"ENGINE_REVIEW"'
--expect 'engine_build/request.json:request_sha256'
--expect 'engine_build/admission.json:status=admitted'
--expect 'engine_build/admission.json:hidden_commitment'
--expect 'engine_build/admission.json:reuse_key'
--expect 'engine_build/admission_sheet.md'
--expect 'engine_build/signoff.json:by'
--expect 'engine_choice.json:engine.ref=toolkit.engines.built:BuiltEngine'
--expect 'engine_build/bench/results.yaml:engine_identity.code_sha256'
--expect 'budget_ledger.json~ENGINE_ADMIT'
```

`parse_spec` reads `~` before `:`, so state history is checked with a file-text regex. The results marker uses the
field the benchmark stamp writes (`engine_identity`). Only the new code writes these files. The δ smoke uses a
question that no registered card fits, with staged inputs, under `--signoff lead`. It runs on the cloud as for any
real run, and it alternates the disease area from the previous retro.

### 6.4 Tests to write first

Each test fails until its code exists. The fixture engines live in `toolkit/engines/built/fixtures/`: small Starsim
modules, each with one planted defect, used as self-test data only.

| Id | Test | Module |
|---|---|---|
| BE1 | a correct two-group SIR fixture passes every check; `status: admitted` | `admission.py` |
| BE2 | a dead parameter fails A6 and names it | `admission.py` |
| BE3 | `1e-12 * p` added to an output fails A6 | `admission.py` |
| BE4 | a parameter wired to the wrong output fails A6 for its signed pair; A7 cannot pass vacuously | `admission.py` |
| BE5 | a parameter read only in `read_outcomes`, without a signed role, fails A6 | `admission.py` |
| BE6 | an unseeded `numpy.random` call or an order-dependent cache fails A2; zero transmission does not make A2's seed test fail (the signed seed-test setting is used) | `admission.py` |
| BE7 | a batch path that leaks state across packages fails A3 | `admission.py` |
| BE8 | a population that grows by 0.1% a step under `conservation: closed` fails A5 | `admission.py` |
| BE9 | severity computed from a formula, not counted from a transition, fails A5 | `admission.py` |
| BE10 | a module that returns the public expected values from a table passes public A4 and fails hidden A4 and S4 | `admission.py` |
| BE11 | a module that misbehaves for one signed unit's attribute values passes the generated cases and fails the real-unit set; a module that reads `unit.id` sees an opaque id, and the invariance check holds | `admission.py`, `worker.py` |
| BE12 | `import inspect`, `os.environ`, `open(...)`, `numpy.load(...)` or `getattr(x, name)` are refused by S2 and S3 | `admission.py` |
| BE13 | a parameter range narrowed from the request, or a changed unit or source, fails A1; an empty window metric and a negative cost driver fail A1 | `admission.py` |
| BE14 | swapped `meta.age_shares` are overwritten by the harness and do not change the analysis | `worker.py` |
| BE15 | an intervention outside the trial's interval fails A8 under the built policy; an `effects_from_spec` entry does not pass it | `admission.py`, `benchmark_coverage.py` |
| BE16 | A10 under `calibration.method: posterior` calls `fit_posterior_panel`; a ridge pair is classed unidentified | `calibrators.py` |
| BE17 | a reference off by 5% fails A11; a stochastic fixture with a 5% bias and wide noise is `inconclusive`, not admitted | `admission.py` |
| BE18 | the engine sandbox cannot read a sentinel file outside its mount set | `worker.py` |
| BE19 | a snapshot file replaced after admission, or a sibling added, is refused before any compile; a hard-linked file is refused at snapshot | `built/__init__.py` |
| BE20 | a fix task's or a deliverable's write to `models/ext/engine_x/` or `engine_build/admission.json` is refused; the build task's write to its working copy is allowed | `v2_sdk_tasks.py`, `stage_machine.py` |
| BE21 | a task's `Read` of another run's `hidden_reveal.json`, or of this run's `.hidden/`, is refused | `v2_sdk_tasks.py` |
| BE22 | the adapter refuses: a spec without the build request, a changed admission or results digest, a lead signature in an owner-mode run, a changed constructor keyword; a spec naming `toolkit.engines.built:BuiltEngine` directly is refused by the schema | `built/__init__.py`, `schema.py` |
| BE23 | `engine_identity` differs for different snapshots and is the same for the same one; it does not include the admission sha256 | `contract.py` |
| BE24 | a build spec validates against `RequestedEngine` before sign-off; a missing component, an incomplete trial declaration and an invalid reduction are each refused at SPEC | `schema.py` |
| BE25 | `build_request_problems` uses the full registry: a `candidates` list that leaves out an eligible engine does not avoid the `why` check | `analysis_backend.py` |
| BE26 | the spec sign-off record holds the sheet's sha256 | `signoff.py` |
| BE27 | `--signoff none` with a build spec is refused at SPEC | `stage_machine.py` |
| BE28 | machine: SIGN → ENGINE_CHOICE → ENGINE_BUILD → ENGINE_ADMIT → ENGINE_REVIEW → ENGINE_SIGN pauses `awaiting_engine_signoff`; sign; CONFIGURE. The pending-build choice record survives a resume | `stage_machine.py`, `schema.py` |
| BE29 | machine: admission fails twice, then `completed_engine_not_admitted`, exit 2; an A8 failure without a departure routes to SPEC and pauses for amendment | `stage_machine.py`, `main.py` |
| BE30 | machine: a post-write GATE finding with `target_stage: ENGINE` gets one repair, re-runs the analysis and rewrites the report; a second repair is refused | `stage_machine.py`, `fix_loop.py` |
| BE31 | resume mid-admission reuses the committed hidden seed; two concurrent resumes: the second is refused | `stage_machine.py` |
| BE32 | reuse: the same key copies the admission; a changed calibration target or dependency pin does not | `admission.py` |
| BE33 | admission compute is reserved against the run balance; with less than the cap left, the run pauses before dispatch | `stage_machine.py` |
| BE34 | `make_bundle` ships the production subset of `engine_build/` and never `.hidden/` or `reference/` | `compute.py` |
| BE35 | no sandbox: `engine_unsandboxed`; with the development flag, a HIGH non-declarable finding | `worker.py` |
| BE36 | the import audit accepts the worker's verified-bytes compile and refuses any other dynamic load; the import guard refuses `toolkit.engines.built.candidate` | `scripts/import_audit.py`, `import_guard.py` |
| BE37 | the §6.3 marker list passes `smoke_check.py` on a fixture run directory | `smoke_check.py` |

`scripts/regression_eval.py` gains planted-defect cases BE2, BE8 and BE10. `scripts/gate_fuzz.py` gains malformed
`admission.json`, snapshot lists and sign-off records, each of which must give a finding (fail closed).

### 6.5 Rollout (each PR under 800 lines, tests first)

| PR | Content | Done when |
|---|---|---|
| α | the request schema, `RequestedEngine`, `build_request_problems`, the pre-sign sheet section, the sheet digest in the sign-off record, the `--signoff none` refusal; the failing tests above; the Starsim spike (§3.2) | BE24–BE27 pass; the others fail as expected |
| β | the snapshot, the engine sandbox, the worker with verified-bytes loading, the static checks, identity (`fingerprint_files`), `BuiltEngine` checks, read and write rules, `harness_owned()` | BE12, BE14, BE18–BE23, BE35, BE36 pass |
| γ | the generators (including the real-unit set), the oracle library, the calibrator wrapper, the built coverage policy, admission and its records, the try tool, compute reservation, bundling | BE1–BE11, BE13, BE15–BE17, BE32–BE34 pass |
| δ | the states, the build and reviewer tasks, sign-off and decline, routing, statuses, the run lock, the fix-loop changes; the real smoke | BE28–BE31, BE37 pass; the §6.3 markers present on a cloud run |
| ε | the reference task (if on), `promote_engine.py` | BE17 on a real reference; a dry promotion of the δ engine lists what is missing |

## 7. Open questions for the owner

| # | Question | Recommended default |
|---|---|---|
| 1 | May a built engine be closed-form (numpy and `scipy.special`, no time loop), as renewal and the package-rate engine are numpy code today? It has no independent state witness (A5) | Yes, as `kind: closed_form` signed in the request, with A11 required. Anything with state over time must be a Starsim module |
| 2 | Is the independent reference build (A11) on by default? It adds about $6 per admission | On. It is the only check that tests the reading of the mechanism directly |
| 3 | Under `--signoff lead`, may a run with a lead-signed built engine reach ACCEPT? | Yes for test and eval runs, with the report's first paragraph saying that the engine was agent-built, harness-admitted and lead-signed. A decision run needs the owner's engine signature |
| 4 | Caps: $40 build balance, $15 admission compute, 2 tries plus 1 repair | As proposed; re-measure after the δ smoke |
| 5 | Reveal the hidden seed at the end of the run, so an audit can re-run every case? | Yes, at FINAL, in a harness file behind the cross-run read rule |
| 6 | Engine repair after a GATE finding: how many per run? | One, with a new admission and a new signature; after that, findings queue for the next run |
| 7 | Promotion: is "two questions" enough for a disease-specific engine, with "two disease areas" for a generic family? | Yes, plus an expert review in the PR and the owner's yes |
| 8 | Does an unidentified calibrated parameter (A10) block admission? | No, unless it moves a decision output and no decision test covers it; then the spec is amended |
| 9 | May `draft_spec` propose `engine.build` without being asked whenever no registered engine fits? | Yes, only as `build_request_problems` allows; the owner sees it at SIGN |
| 10 | The import allowlist: is `scipy.integrate` excluded from engines? | Excluded. The engine steps on Starsim's grid; ODE solvers stay in oracles and the reference |
| 11 | Do benchmarks the build agent proposes ever gate? | No, they are reported only |
| 12 | Which model builds: `MODEL_DEEP` for the build, reference and reviewer? | Yes |
| 13 | May a spec request a build when a registered engine is eligible? | Only with a `why` naming a mechanism of the request that no eligible card has; the harness checks it against the full registry and the owner signs it |
| 14 | Does a reused admission (§5.4) need the owner's signature again in the new run? | Yes. It is cheap, and the run is a new decision |
| 15 | Should named engines be restricted to registered refs (the gap in §5.5), in a separate PR before this lane? | Yes, before β |
| 16 | The hidden set's size on large specs: 200 units and 64 packages | As proposed; the sheet reports coverage of the real units and packages |

## 8. Not in this note

- Building engines in other languages, or around external simulators: excluded by the team rule.
- Letting the agent run arbitrary code: the try tool is the only way it runs anything.
- Changing registered engines through this lane: a registered engine changes by a PR and its pack.
- Data acquisition: ACQUIRE (`docs/design/acquire.md`) precedes SPEC, and acquisition is closed at SIGN, before any
  build.

## 9. Review log

**Round 1 (Codex, `scripts/codex_review.py --pr 363`): 35 P1, 7 P2.** Every finding about current code was checked
against the code before folding in; all held. All 42 are folded in:

- **Signing and selection:**
  - the justification uses the full registry;
  - a harness stand-in engine replaces the stand-in card;
  - signed decision inputs and binding sit in the request, used from EVIDENCE on;
  - the sheet's digest goes into the sign-off record;
  - explicit pending and signed build resolution in the schema;
  - `--signoff none` is refused, and the signer is checked against the run's mode;
  - the adapter is not constructible outside the build form;
  - a separate candidate engine runs the first admission;
  - the harness passes the run directory and checks reserved keywords and the configuration digest.
- **What admission verifies:**
  - a valid reduction example and reductions validated at SIGN;
  - A1 requires the exact parameter declaration and checks window metrics and cost-driver signs;
  - the harness computes age shares;
  - outputs are transition counts of a signed graph;
  - A6 tests signed (input, output) pairs, and exceptions are owner-signed;
  - the hidden set includes the real units and packages, with opaque ids;
  - structured trial declarations;
  - a stricter coverage policy than `assess` as it stands;
  - A10 calls the run's own calibration path;
  - the oracle module is named to fit the import policy;
  - A11 is an equivalence test with an inconclusive route.
- **Isolation, provenance and reuse:**
  - an explicit sandbox mount set, and a list of library I/O calls;
  - content-addressed snapshots loaded from verified bytes, with link-count checks;
  - snapshot checks at every later state instead of `ArtifactIntegrity`;
  - `harness_owned()` coverage;
  - one authoritative pack, results and coverage set, bound by the signature;
  - the production subset in the Coiled bundle;
  - a reuse key over every check's inputs;
  - all three dependency manifests;
  - a cross-run read rule for every task.
- **Routing and verification:**
  - an A8 or A10 amendment goes through SPEC;
  - the repair edge uses the GATE back-edge bookkeeping and the analysis inputs;
  - admission compute is reserved against the run balance;
  - corrected smoke markers.
- **P2s:**
  - a writable working copy with immutable snapshots;
  - stochastic-aware A2 and A4;
  - typed input domains;
  - routes for a missing reference or review (a new ENGINE_REVIEW state);
  - planner support for the repair;
  - the run lock in every mode;
  - caps as run settings with a resumable pause.

Finding 1 also showed an existing gap in the registry, which is recorded in §5.5 and open question 15 rather than
fixed here.
