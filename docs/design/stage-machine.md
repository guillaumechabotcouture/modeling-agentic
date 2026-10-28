# Stage machine: code runs the pipeline, LLMs reason and edit code

**Status:** design for review (2026-10-27). It makes `docs/architecture-v2.md` §3.1 (stage graph, Python orchestrator, migration step 5) concrete, and adds what E7 attempts 6 and 7 showed is missing:
- a declared build pipeline per run;
- small fixes whose impact is measured one at a time;
- gate inputs computed by the harness instead of written by agents;
- (§3.6) a reusable, tested toolkit by problem type, so each run configures and extends tools instead of rewriting them, and the toolset improves and is reused more with every run.

## 1. Why (measured on E7 attempts 6 and 7)

| Observation | Evidence |
|---|---|
| The flow is enforced, not run | An LLM lead follows a ~1,000-line prompt (STAGES 1–8.9). The harness pushes back with refusals: round plan, fix checks, critic order, writer wait, presentation wait. Each refusal is code enforcing a flow that code could run. |
| Agents write the pipeline each round | Attempt 7's modeler wrote new driver scripts per round (`run_all_r2.sh`, `lib2r_driver.sh`, `run_r4_chain.sh`). Staleness and drift followed: `figure_staleness_detected` at gates 2, 3 and 5; `numeric_drift_*` at gates 2 and 5. |
| Rounds are large and unattributable | A fix round is one long modeler session that changes several things. It is judged only at the next full critique (about 2.4 h and $45 per round). Which change moved the headline, or opened a new finding, is reconstructed afterwards. |
| Self-reported numbers get gamed | The first trial check read agent-written ratios, and review found 7 ways around it. The harness-computed version found none of that kind. The gate inventory lists about 15 checks that read numbers agents typed (sensitivity flips, optimizer gap, leave-one-out metrics, calibration restarts, benchmark values). |
| Most of a round's findings are not modeling | At attempt 7's round-5 gate, 7 of 13 findings were propagation side effects (figures, number drift, claims ledger, integrity), 3 were declared and could not close, and 3 were modeling content. |

## 2. Principles

1. **Code owns flow, computation and numbers.** It decides what runs next, runs it, and computes every number a gate or the report uses.
2. **LLMs own reasoning and code edits.** That covers framing, model design, choosing what to fix next, writing and editing code, interpreting results, critique, and prose.
3. **One fix, one rebuild, one measurement.** A fix is a small change aimed at one finding. The harness rebuilds what it affects and records its impact before the next fix starts.
4. **Agents return code or typed outputs, never hand-written numbers.**
5. **Reuse before writing.** A run starts from the toolkit for its problem type and writes only what the toolkit lacks. What it writes that proves generic is promoted into the toolkit, with tests, after the run. So the next run starts further along.
6. **Every LLM-caught error class becomes a deterministic check.** The share of findings that only an LLM catches should fall from run to run, and is measured.

## 3. Architecture

### 3.1 The run state machine (`agents/stage_machine.py`)

The harness owns the state, persisted in `run_state.json` so a run resumes where it stopped. Transitions are code:

| State | Runs | Next |
|---|---|---|
| PLAN | planner → brief, plan, success criteria | SIGNOFF |
| SIGNOFF | owner (or lead) signs the brief, including the comparator | DATA |
| DATA | data agent → data and `data_schema.yaml` | MODEL_BUILD |
| MODEL_BUILD | modeler → model code, the run's `pipeline.yaml`, the function contracts (§3.3) | BUILD |
| BUILD | harness runs the pipeline (§3.2), then all harness-computed checks | GATE |
| GATE | mechanical gate (today's rules, moved out of the lead prompt unchanged) | FIX_LOOP / CRITIQUE / WRITE / FAIL |
| FIX_LOOP | one fix task at a time (§3.4) | BUILD after each fix |
| CRITIQUE | domain and red-team critics, on computed facts and figures | GATE |
| WRITE | writer → manuscript; harness renders claims | PRESENTATION |
| PRESENTATION | presentation critic on the draft, then writer or analyst fixes | FINAL_GATE |
| FINAL_GATE | gate in post-write mode | done |

**What the LLM lead becomes.** The lead no longer orchestrates. It becomes a **fix planner**, called at GATE with a structured packet: open findings, their tests, the impact ledger (§3.4) and the budget. It returns an ordered list of fix tasks, and for each one a rethink-or-patch choice. Routing rules that are mechanical today (which agent owns which stage, when critics run, when the writer may start) move into the state table.

**Acceptance test (from v2):** replay golden traces that exercise RETHINK_STRUCTURAL, PATCH_OR_RETHINK, DECLARE_SCOPE (including early finish), RUN_FAILED and a post-write HIGH. The machine must take the same actions as today's gate.

### 3.2 The run's build pipeline (`models/pipeline.yaml`)

The modeler declares every step once, and the harness runs them:

```yaml
steps:
  - id: library
    run: compute: models/library.py::run_cell   # through the compute service
    inputs: [models/library.py, models/malaria_abm.py, models/lib_inputs.jsonl]
    outputs: [models/lib_results.jsonl]
    purpose: plan: impact library
  - id: allocate
    run: python3 models/scenarios.py
    inputs: [models/lib_results.jsonl, data/lga_master.csv, models/costs.py]
    outputs: [allocation_primary_774_lgas.csv, models/emulator_cache.npz]
  - id: figures
    run: python3 models/model_figures.py
    inputs: [allocation_primary_774_lgas.csv]
    outputs: [figures/*.png]
```

- **Incremental.** The harness hashes each step's inputs and code. After a change it reruns that step and everything downstream, like `make`. Phase 39 β already does this for figures from their sidecars; this extends it to every step.
- **Owned outputs.** Pipeline outputs are protected artifacts. Only the harness's pipeline run writes them, so an agent cannot hand-edit a results file. The integrity record already supports this through producer grants.
- **Agents still iterate quickly.** An agent can run any step in a scratch copy (`pipeline.py run <step> --scratch`) to try a change before submitting it. Only the harness's run writes the real outputs.
- **Harness steps are appended.** These are uncertainty, identifiability, sensitivity, optimizer benchmark, effect check, claims render and figure rebuild, each driven through the function contracts.

### 3.3 Function contracts: the model exposes code, the harness computes facts

The run exposes a few functions with fixed signatures. The first three exist today:
- `outcome_fn(params) -> outputs`, for uncertainty;
- `evaluate_unit(unit, package) -> outcomes`, for the eligibility probe and the effect check;
- `loss(params) -> float`, for identifiability.

**Binding comes first** (gate inventory #1). The harness checks that these functions reproduce the deliverable: the allocation CSV and the headline, within tolerance. A decoy therefore fails before any other check reads it. The effect check (PR #32) does this for `evaluate_unit`: on attempt 7 it matched within 0.03% over 774 LGAs.

**Conversions, in the inventory's order:**
1. Bind `outcome_fn` to the headline.
2. Compute sensitivity from `outcome_fn` at the registry CI ends; the harness picks the parameters by swing.
3. Benchmark the optimizer on a harness-built benefit and cost matrix from `evaluate_unit`, with the harness's own ILP.
4. Calibration multi-start and leave-one-out refits from `loss`.
5. Benchmarks and sanity values by pointer only.

After these, every number the gate reads comes from model code run by the harness.

### 3.4 The fix loop: one finding, one change, one measured impact

```
GATE → fix planner orders the open findings → for each task, in order:
    agent (modeler / analyst / data) gets ONE finding + its test + a scoped brief
    agent edits code (may run steps in --scratch), submits
    harness: BUILD (incremental) → harness checks → the finding's done_when test
    harness appends to models/impact_ledger.jsonl:
        {task, finding, files changed, steps rebuilt, core-hours, $,
         headline before/after, test passed?, new gate findings opened}
    test fails → back to the agent once with the evidence; then the planner decides
→ when the task list is done or the budget for this loop is used: CRITIQUE
```

- **Small and attributable.** Every change has a measured effect on the headline and on the findings. That's the "test each one individually and understand its impact" loop. It also answers "is this compute buying anything?" per fix, building on Phase 37's `--purpose`.
- **Cheaper.** An agent's context is one finding plus the files it touches, not a whole round's backlog. Critics run once per loop rather than after every change. Fixes whose tests are mechanical need no critic at all.
- **Regressions are caught at the step that causes them.** A fix that breaks an earlier test, or opens a new finding, shows up in its own ledger row rather than at the next gate.

### 3.6 Toolkit and templates: configure and extend, don't rewrite

**The problem.** Attempt 7's modeler wrote 111 files under `models/`. Most are not the malaria model. They are generic machinery that every allocation question needs:
- an optimizer and cost accounting;
- scenario and package composition;
- uncertainty plumbing, figures, per-round drivers and the claims spec.

Rewritten each run, this machinery carries new bugs each time. Critics spent rounds catching them: the SMC drug double count, case-management reach, number drift, stale figures. The inventory (`a7_models_inventory`, step T0 below) measures this split.

**Templates by problem type, not by disease.** Examples: "budget allocation across units", "calibrate and project", "decision rule". A template is:
- **A spec schema.** A machine-readable question spec: units, interventions and packages, unit costs, budget, comparators (from the signed brief), outcomes and their accounting (window, discounting, DALY weights), calibration targets and benchmarks. The planner drafts it, the owner signs it with the brief, and the harness validates it.
- **A pipeline of tested steps.** Data load and schema; library or scenario composition through the compute service; calibration; binding; uncertainty; sensitivity; optimizer and its benchmark; cross-validation; decision rule; figures; claims. Each step is a toolkit module with unit tests and a reference case, configured from the spec.
- **Contracts for the run's own model.** The model the LLM writes plugs in through fixed functions: `outcome_fn`, `evaluate_unit`, `loss` and, for library models, a cell runner. Everything around the model comes from the toolkit.

**Where the toolkit lives.** In the repo as `toolkit/<template>/`, versioned, with tests in CI. Disease mechanics that are reusable across runs of one disease live beside it (for example `toolkit/diseases/malaria/`, behind the same contracts). They are gated by the existing `applies_when` keywords, so gate logic stays disease-agnostic.

**How the toolset improves over time:**
- **Start:** the planner picks the template; the modeler configures it and writes only the model and any missing pieces.
- **During the run:** a missing piece is written in the run under `models/ext/`, behind a declared interface.
- **After the run (promotion):** each `models/ext/` piece is reviewed. Generic ones move into the toolkit with a unit test and a reference case, and get a CHANGELOG row. Its version is recorded in `run_state.json`, so a run's results trace to the exact toolkit version.
- **Error classes:** each critic finding that a deterministic check could have caught gets that check added to the toolkit or the gate, as the comparator, trial-effect and figure-staleness findings did this session.
- **Measured:** each run reports toolkit lines reused against lines written, and findings caught deterministically against findings caught by LLM. Both trends should improve from run to run.

**Deterministic, end-to-end tests per template (no LLM).** A planted-truth case uses synthetic units with known effects and a known optimal allocation; the template's pipeline, given the spec and a reference model, must recover the answer. This runs in CI, so toolkit and harness changes are tested without a paid run. It follows the OmicsBench pattern: answer files plus deterministic graders.

**Novel models stay possible.** Templates fix the steps around the model, not the model. A question that fits no template runs as today, with its custom steps declared in `pipeline.yaml`, and is the first candidate for a new template.

### 3.5 What stays LLM judgement

- **Framing:** the brief, comparators, out of scope, and the open questions for the owner.
- **Model design and code:** architecture choice and every code edit.
- **Choosing and ordering fixes, and rethink versus patch.**
- **Critique of what can't be computed:** domain plausibility, whether a trial matches the comparator, hidden assumptions, the framing of results.
- **Interpretation and prose:** results, limitations, the write-up.

Critics judge computed facts. They read the impact ledger, harness reports and figures, not the modeler's self-description.

## 4. Migration: small steps, each tested on its own

Each step ships as its own PR. Each is checked on a replay or a short real run against a stated metric before the next starts.

| Step | Builds | Metric to pass |
|---|---|---|
| T0 | Inventory of attempt 7's `models/`: reusable machinery vs disease-specific vs question-specific vs superseded; proposed allocation toolkit and spec fields | Each file classified; lines of code by class; toolkit modules mapped to the files they come from (started 2026-10-27) |
| T1 | Allocation spec schema plus the first toolkit modules, extracted from attempt 7 and the existing `scripts/` (optimizer and benchmark, cost and package composition, uncertainty, sensitivity), each with unit tests | Planted-truth allocation recovered by the toolkit alone; attempt 7's allocation reproduced from its spec and model within 1% |
| S1 | `pipeline.yaml` schema plus harness runner (incremental, owned outputs); producers and figures first (generalises Phase 39 β) | On attempt 7's copy: one changed input reruns exactly its downstream steps; zero stale artifacts at the gate |
| S2 | Binding of `outcome_fn` to the headline (inventory #1) | Attempt 7: bound within 1%; a decoy fails |
| S3 | Fix loop plus `impact_ledger.jsonl`; one finding per modeler task, scoped brief | Replay of attempt 7's round 4 → 5 fixes as single tasks: each has a ledger row; total cost ≤ that round's |
| S4 | State machine in `main.py` for the deterministic transitions; the lead becomes the fix planner | Golden-trace replays take the same gate actions; lead context per round ↓ (measured) |
| S5 | Gate conversions #2–#5 (sensitivity, optimizer, calibration and leave-one-out, pointers) | Each: the self-reported field is no longer read; the retro on attempt 7 matches or explains |
| S6 | Retire lead-prompt stages and the refusals S4 replaced | Prompt size ↓; no regressions in replays |

**Sequencing with what's open:** PRs #32, #34, #35 and #36 (trial check, figure rebuild, early finish, presentation deferral) land first. They are compatible with this design and become stage behaviour in S4. T0–T1 run alongside S1–S2. The toolkit's steps are what `pipeline.yaml` runs, and gate conversions #2–#5 become toolkit steps rather than separate scripts.

## 5. Risks

- **The machine constrains novel modeling.** Mitigations: back-edges for RETHINK, optional steps, and a modeler-defined `pipeline.yaml` (the harness fixes the shape of steps, not their content).
- **Contracts don't fit a model.** Example: a model with no per-unit evaluator. Mitigation: contracts are per artifact. Where one can't be met, that check stays as today and the gap is listed, not hidden.
- **Many small tasks lose the big picture.** Mitigation: the fix planner sees the whole backlog, and a full critique runs after each loop.
- **Pipeline runs get slow.** Mitigations: incremental rebuilds, the compute service for heavy steps, and `--scratch` for the agent's own iteration.

## 6. Decisions for you

1. **Fix-loop granularity:** strictly one finding per task, or allow a small group of findings in the same files? Proposed: one by default; the planner may group findings that share files.
2. **Lead model:** does the fix planner stay on the top model? Proposed: yes. It is short and decision-heavy; the savings come from dropping orchestration context.
3. **Owned outputs:** may agents ever write pipeline outputs directly, for example in an emergency? Proposed: no. They use `--scratch`, then the harness run.
4. **Where to start:** S1 (pipeline runner) or S3 (fix loop with the impact ledger)? Proposed: S1, because S3 needs a harness-run rebuild to measure anything.
5. **Promotion review:** who approves moving a run's `models/ext/` code into the toolkit? Proposed: an agent drafts the promotion PR with tests after each run, and you approve it, like any other PR.
6. **First template:** "budget allocation across units". Proposed: yes; E7 has two real instances of it to extract from and test against.
