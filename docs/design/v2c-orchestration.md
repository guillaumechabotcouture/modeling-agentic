# v2 workstream C: the code-owned state machine, the findings ledger and the fix loop

**Status:** design note for review (2026-10-31), written before the code. It makes
`stage-machine.md` S3–S4 and `architecture-v2.md` (PR #82) §3.5–3.6 concrete for the v2
pipeline. Branch `v2c-orchestrate`, PR into `v2`.

## 1. What the evidence says the machine must do

| Evidence (stored runs, read-only copies) | Consequence for the design |
|---|---|
| Attempt 7: 11 gate runs over 8 rounds, all PATCH_OR_RETHINK until round 8, then DECLARE_SCOPE; 3 extra in-round gates and a post-write gate at round 8 | The gate is called many times per round. The machine must route each call, including the post-write one, from the gate's own action. |
| Attempt 8: 21 gate runs; round 5 alone ran the gate 10 times | Fixes were checked by re-running the whole gate. Per-fix checks are what S3 replaces. |
| Smoke 1 (Phase 47): RETHINK_STRUCTURAL four times in round 1 (spec-compliance), then a report | Today the lead wrote a report after RETHINK_STRUCTURAL. The machine has no such path: RETHINK is a back-edge. |
| Smoke 2 (Phase 48): PATCH_OR_RETHINK at the post-write gate with 1 round left | A post-write HIGH must go back into the fix loop, not end the run. |
| Pipeline smoke (Phase 40): RUN_FAILED at round 2 | RUN_FAILED leads to a not-accepted write-up, then the end. |
| Lead prompt STAGE 7 Step 3 and nine flow refusals in `agents/hooks.py` | Each routing rule and refusal becomes a transition (§4). |

## 2. Principles applied

1. **The gate's rules are not reimplemented.** The GATE state calls today's validator:
   the CLI, exactly as `main.py`'s final gate does, for legacy runs, and for v2 runs the
   same rule function (`validate_critique_yaml.decide` plus
   `_incorporate_rigor_violations`, which apply `_recompute_action`) on the v2 findings.
   The machine only maps the action to the next state, so the golden-trace acceptance
   test checks two things: the action equals today's gate on the same files, and the
   transition equals what the lead prompt's STAGE 7 Step 3 prescribes for that action.
2. **Structure over heuristics.** Flow that the lead prompt describes and the hooks
   refuse to let go wrong (writer before the gate, critics before work ends, a plan
   before work, sign-off before work) is a transition table. The machine cannot
   take a move the table does not list, so those refusals have nothing to refuse.
3. **One finding, one task, one measured impact.** The fix loop runs one task at a time;
   after each the harness rebuilds through the S1 pipeline runner, re-runs the finding's
   own check, and writes an impact row.
4. **Small and replaceable.** Every LLM step is a bounded task with typed inputs and
   outputs, behind one `Tasks` interface. `StubTasks` (deterministic, no model) drives
   the unit tests and the dry run; an SDK implementation fills the same interface.

## 3. States and transitions (`agents/stage_machine.py`)

`run_state.json` holds the state, the round, the gate mode, the engine choice, the
spend and a log of every transition. It is written atomically after each transition,
so `--resume` continues from the last completed state.

| State | Runs | Outcome → next |
|---|---|---|
| SPEC | task `draft_spec` → `decision_spec.yaml`; harness validates (backend `validate`) | valid → SIGN; invalid → SPEC (2 retries) → FINAL `run_failed` |
| SIGN | owner: wait for `decision_spec.signoff.json`; lead: task `sign_spec`; none: recorded as unsigned | signed → EVIDENCE; not yet → pause `awaiting_signoff` (resumable) |
| EVIDENCE | stub: lists the engine parameters as `unverified` in `evidence/registry.yaml` | → ENGINE_CHOICE |
| ENGINE_CHOICE | harness filters EngineCards against the spec; task `pick_engine` chooses and says why; optional cross-engine check when several are eligible | one chosen → CONFIGURE; none eligible → FINAL `run_failed` (the fallback, an agent-written engine, is outside this workstream) |
| CONFIGURE | harness writes `models/pipeline.yaml` (configure + analyze steps) and runs `configure` through `scripts/pipeline.py` (S1) | ok → ANALYZE; failed → FINAL `run_failed` |
| ANALYZE | S1 runner: the stale steps only | ok → CRITIQUE; failed → FINAL `run_failed` |
| CRITIQUE | tasks: domain and red-team critics write `critique_*.yaml` for the round (presentation is its own state) | → GATE |
| GATE | today's gate (§2.1), pre-write or post-write | by action, table below |
| FIX_LOOP | §5 | → CRITIQUE (round + 1) |
| OWNER_DECISION | Phase 49 β AWAIT_OWNER: waits for `scope_signoff.json` | answered → GATE; not yet → pause `awaiting_scope_signoff` |
| WRITE | task `write` → `manuscript.md`; harness renders `report.md` with the numbers from artifacts | → PRESENTATION |
| PRESENTATION | task: presentation critic on the draft | → GATE (post-write) |
| FINAL | status, as `run_finish.determine_status` names it | terminal |

GATE routing, the rules of lead prompt STAGE 7 Step 3 and STAGE 8.9:

| Action | Pre-write gate | Post-write gate |
|---|---|---|
| ACCEPT | WRITE | FINAL `completed` |
| PATCH_OR_RETHINK | FIX_LOOP | FIX_LOOP (the post-write HIGH is a finding like any other; the loop's critique and gate lead back to WRITE) |
| RETHINK_STRUCTURAL | back-edge: SPEC when a structural blocker targets PLAN, else ENGINE_CHOICE | same |
| DECLARE_SCOPE (rule 3 and early finish 3a) | WRITE, with the declarations rendered into Limitations | FINAL `completed_scope_declared` |
| AWAIT_OWNER (Phase 49 β, PR #85) | OWNER_DECISION | OWNER_DECISION |
| RUN_FAILED | WRITE in not-accepted mode | FINAL `run_failed` |
| schema errors (exit 3) | CRITIQUE, re-running the named critics (2 tries) | same |
| none / VALIDATOR_ERROR | FINAL `completed_gate_error` | same |

Budget: before any LLM task the machine checks the run's cap less the write-up reserve
(`run_budget.writeup_reserve`). With no room left it goes to WRITE when there is no draft
(today's `final_writeup`) and to FINAL `budget_exhausted` otherwise.

## 4. Lead prompt and refusal hooks

The fix planner replaces the lead. Its prompt (`agents/fix_planner.py`) keeps only what
is judgement: rethink versus patch, which findings to group, the order, and when to ask
for a decision test instead of a fix. It drops STAGES 1–8.9 as orchestration, the
validator invocation, the decision record, the spawn templates, the round plan and the
forbidden-move list. Measured in §7.

| Refusal in `agents/hooks.py` today | Becomes |
|---|---|
| `signoff_hold_reason` (no work before the brief is signed) | SIGN precedes every work state |
| owner-mode refusal of `decision_brief.py sign` | the SIGN state reads the owner's file; no agent has a sign task in owner mode |
| `review_wait_reason` (critics and writer wait for running work) | states run one at a time; CRITIQUE starts after ANALYZE or FIX_LOOP returns |
| `writer_hold_reason` (the write-up waits for the gate) | WRITE is reached only from GATE (ACCEPT, DECLARE_SCOPE, RUN_FAILED) or the budget rule |
| `plan_hold_reason` (work waits for `round_plan.yaml`) | FIX_LOOP calls the planner first; tasks exist only from its validated output |
| `critique_hold_reason` (critique waits for planned fixes and fix checks) | each task's check runs inside FIX_LOOP; CRITIQUE follows the loop |
| critic prompt-head rule (`This is critique round N.`) | the machine writes the critic's typed input; the round is a field |
| presentation critic before the draft | PRESENTATION follows WRITE |
| round-plan freeze (`open_problems.freeze`) | the planner's task list is stored in `fix_plan_rN.json` and the loop runs it; nothing can edit it mid-loop |

In `--v2` mode none of these hooks is installed for the lead, because there is no lead
session. The tool-level enforcement (paths, Bash, integrity) stays for every agent task.

## 5. Findings ledger (`findings.jsonl`) and fix loop

**One row per finding**, rewritten at each sync (the file is the current state; history
lives in each row's `events`):

```
{id, key, kind, severity, source, owner_state, target_stage, resolution_type,
 status, scope_declarable, first_seen_round, last_seen_round, claim, done_when,
 decision_test, events: [{round, status, by}]}
```

- `id`: the critic blocker id (`R-005`), or `kind:key` for a gate finding (the gate's
  `RIG-nnn` ids are renumbered every gate, so they are not identities).
- `key`: `stall_rule.finding_key`.
- `owner_state`: the state that can change it, from `target_stage` / `stage`
  (PLAN→SPEC; DATA, MODEL, IDENTIFIABILITY, MULTI_STRUCTURAL, PRE_MODEL→CONFIGURE;
  ANALYZE, UQ, OPTIMIZATION, SENSITIVITY, GATE→ANALYZE; WRITE→WRITE;
  PRESENTATION→PRESENTATION). An unknown stage is kept as given and flagged, not
  guessed.
- `resolution_type`: `owner_decision` (owner-marked, Phase 49 β), `decision_test` (a
  declared finding, or one whose declaration cites a test), else `fix` (a fix with a
  measured impact).
- `status`: `open`, `declared`, `fixed` (absent from a later gate, or its `done_when`
  passes), `owner_accepted`, `owner_declined`.

**Adapters** (the old files stay): critique YAMLs (blockers and their `done_when`),
the gate decision (`unresolved_high`, `accept_blockers`, `rigor_violations`),
`scope_declaration.yaml` (and the decision-test file a declaration cites), the
analysis engine's decision tests (`analysis.json`), and `scope_signoff.json`.

**Fix loop.** The planner task gets a packet: the open findings, the budget left, and
the impact history. It returns ordered tasks
`{id, findings: [...], owner_state, action: fix|rethink|decision_test, brief, files}`.
The harness validates the plan (unknown ids, a finding in two tasks, a grouped task
whose findings share no file: rejected; one retry, then one task per finding in
severity order). For each task:

1. the agent task edits configuration or code (`files_changed` comes from a file hash
   before and after, not from the agent);
2. the harness rebuilds through `scripts/pipeline.py run` (only stale steps);
3. it re-runs the finding's checks: its `done_when` test when it has one, and the
   backend's `gate_findings` on the rebuilt outputs;
4. it appends to `impact_ledger.jsonl`:
   `{round, task, findings, files_changed, steps_rebuilt, seconds, usd,
   headline_before, headline_after, passed, still_open, new_findings}`.

A task whose check fails goes back to the agent once with the evidence; then the row is
written as not passed and the loop continues. When the task list is done or the loop's
budget is used, the machine goes to CRITIQUE for the full critique.

## 6. Interfaces to workstream B

Until `v2b-analysis` lands the backend adapter (`agents/analysis_backend.py`) drives
`spike/analysis` (which runs on the toy engine): `validate(spec)`,
`pipeline_steps(spec_path, run_dir)`, `headline(run_dir)`, `gate_findings(run_dir)`.
The proposal was sent to workstream B; the adapter is the only file that changes when B
lands.

## 7. Acceptance and measurements

- **Golden traces** (`scripts/golden_traces.py`): attempt 7, attempt 8, smoke 1, smoke 2,
  the Phase 40 RUN_FAILED smoke, and an early-finish variant. For each, today's gate CLI
  runs on a fresh copy; the machine's GATE state runs on another fresh copy; the two
  actions must match, and the machine's next state must match the table in §3. Every
  recorded gate in `validator_history.jsonl` is also routed and compared with what the
  run did next.
- **Unit tests** in each module's `--self-test`: every transition, resume after each
  state, the ledger adapters, plan validation, and the impact rows.
- **Prompt size:** lead system prompt plus lead prompt (today) against the fix planner's
  prompt plus its packet, in characters and estimated tokens.
- **Dry run:** one evaluation-suite question on the toy engine with `StubTasks`.

## 8. Results (2026-10-31)

**Golden traces** (`scripts/golden_traces.py`, copies of the stored runs):

| Trace | Stored final gate | Same rule function re-decides it | Machine routes to | Today's gate CLI on a copy | Machine GATE (legacy mode) on another copy | Recorded gates where the run did what the machine would |
|---|---|---|---|---|---|---|
| Attempt 7 | DECLARE_SCOPE (rule 3), post-write | DECLARE_SCOPE (3) | FINAL `completed_scope_declared` | RUN_FAILED (5) | RUN_FAILED (5) → FINAL | 11 of 11 |
| Attempt 8 | DECLARE_SCOPE (3), post-write | DECLARE_SCOPE (3) | FINAL `completed_scope_declared` | RUN_FAILED (5) | RUN_FAILED (5) → FINAL | 21 of 21 |
| Attempt 8, 10 rounds (early finish) | — | DECLARE_SCOPE (3a) | FINAL `completed_scope_declared` | PATCH_OR_RETHINK (2) | PATCH_OR_RETHINK (2) → FIX_LOOP | — |
| Smoke 1 (Phase 47) | RETHINK_STRUCTURAL (1) | RETHINK_STRUCTURAL (1) | ENGINE_CHOICE (back-edge) | PATCH_OR_RETHINK (2) | PATCH_OR_RETHINK (2) → FIX_LOOP | 0 of 4: the run wrote a report after RETHINK |
| Smoke 2 (Phase 48) | PATCH_OR_RETHINK (2), post-write HIGH | PATCH_OR_RETHINK (2) | FIX_LOOP | PATCH_OR_RETHINK (2) | PATCH_OR_RETHINK (2) → FIX_LOOP | 1 of 3: the run wrote its report with a round left, and ended at the post-write HIGH |
| Phase 40 smoke | RUN_FAILED (5) | RUN_FAILED (5) | FINAL `run_failed` | RUN_FAILED (5) | RUN_FAILED (5) → FINAL | 5 of 5 |

Today's gate differs from the stored decisions on attempts 7 and 8 and smoke 1 because checks
were added after those runs (e.g. `decision_layer_undeclared`, `mechanism_unaccounted`,
`headline_binding_failed` are not scope-declarable). On every copy the machine's GATE state and
the CLI agree, action and rule.

**Prompt size** (characters / estimated tokens at 4 characters per token): the lead's system
prompt plus first prompt, 42,170 / 10,542; the fix planner's prompt, 2,470 / 618, plus its packet
(the open findings, the impact history, the budget): 13,735–26,315 / 3,434–6,579 on attempts 7 and
8 and smoke 2. The lead re-reads its prompt on every turn of a 10-hour session; the planner is
called once per loop.

**Dry run** (`malaria_alloc` question and its data, toy engine, `StubTasks`): SPEC → SIGN →
EVIDENCE → ENGINE_CHOICE → CONFIGURE → ANALYZE → CRITIQUE → GATE (PATCH_OR_RETHINK) → FIX_LOOP →
CRITIQUE → GATE (ACCEPT) → WRITE → PRESENTATION → GATE post-write (ACCEPT) → FINAL `completed`.
The one fix task changed `decision_spec.yaml` (uncertainty draws 8 → 32); the harness rebuilt
configure and analyze, the critic's done_when test passed, the 95% interval moved from
[864,983, 1,393,047] to [829,222, 1,416,394] cases with the point estimate unchanged, and a gate
finding the same change fixed (`uncertainty_draws_low`) closed in the ledger. Wall time 18 min on
a loaded laptop (most of it CPU contention).

**Interface with workstream B** (PR #89): `agents/analysis_backend.backend()` uses
`toolkit/analysis/api.py` when present. Checked against the branch: validate, headline and
gate_findings map directly; the analyze step's declared outputs `uncertainty_report.yaml` and
`models/claims_ledger.yaml` are refused by the S1 runner as owned by other producers (raised
with workstream B).

**Open items:** the S1 integrity record (`ArtifactIntegrity`) is not yet passed in v2 runs; the
SDK tasks are exercised by one real task (engine pick, $0.24) only; the evidence state is a stub;
SIGN uses its own section hash until B's `toolkit/spec/signoff.py` lands, after which fixes edit
only `analysis_settings.yaml`.

## 9. Review round 1 (Codex, seven P1s): what changed

Each fix was first reproduced by a failing self-test case, then fixed.

| # | Finding | Change | Test |
|---|---|---|---|
| 1 | A measured decision test that changes the plan could be scope-declared without the owner | `analysis_backend.gate_findings` marks it `decision_bearing`; `stage_machine.apply_owner_rule` sends it through Phase 49 β's rule (`scripts/scope_signoff.py` plus the validator's AWAIT_OWNER path) and, when that rule is absent or fails, makes it not scope-declarable | T30–T32, B18 |
| 2 | Resume could re-run a completed paid task or lose its charge | `run_state.json` holds a task journal (key, task, started and completed times, allowance, spend, session id, output). A completed task is replayed, not re-run; a task found started is charged its allowance once and run again; the fix loop skips tasks that have an impact row and keeps each task's pre-task snapshot | T25, T26, F14 |
| 3 | A failed or interrupted SDK session was charged $0 | `SdkTasks` charges through `run_budget.charge_session` and writes `budget_ledger.json`: a reported cost (also one reported before an exception), else the conversation's cost read back (`crashed_session_cost`), else the whole allowance | S1–S5 |
| 4 | Each call's cap was a fixed $3, not what the run had left | `Machine._allowance`: the task cap, at most the run's remaining budget less the write-up reserve (the write-up may use the reserve), recorded in the journal before the call; every paid call, including each critic of a pair and each retry, is checked first | T27, T28 |
| 5 | Fix tasks could edit `decision_spec.yaml` | The owner signs the whole file (sha256, as workstream B does). Fixes edit `analysis_settings.yaml`, which may hold only `uncertainty.draws`, `uncertainty.seed`, `emulator.points`, `emulator.seed` (B's `schema.SETTINGS_PATHS` in the spike's vocabulary). The engine the harness chose is in `engine_choice.json`, so the spec stays as signed. After each fix the harness checks the spec hash and the settings file; a violation is undone before anything is rebuilt | T19, F13, B15–B17 |
| 6 | The deliverable path was not validated | `deliverable_problems`: relative, inside the run directory, no symlink on the way, not a harness-, gate- or critic-owned file; otherwise nothing is written and a non-declarable HIGH `deliverable_path_invalid` is raised | T29 |
| 7 | The golden traces exercised the legacy wrapper only | Check D runs the machine's v2 GATE state on each copy, fed the harness findings the stored gate recorded, against the stored action and rule; `run` exits 1 on any mismatch or missing decision; a mutation test (v2 gate replaced by ACCEPT) must fail | G7–G9 |

## 10. Engine choice with workstream B's schema (agreed 2026-10-31)

- The signed `decision_spec.yaml` may say `engine: auto` (optionally `{auto: true, candidates: [...]}`).
  An explicit engine in the spec wins.
- The harness's ENGINE_CHOICE writes `engine_choice.json`:
  `{"engine": {"ref", "config"}, "why", "candidates": [{ref, eligible, why_not}], "cross_check"}`.
  Agents never write it.
- B's `schema.load` takes the engine from that file when the spec says auto (fail closed if it is
  missing or not among the candidates); the manifest and each artifact's envelope record its sha256
  beside `spec_sha256`, and a later change is `analysis_contract_stale`. B may expose
  `api.engine_needs(spec)` so the eligibility filter speaks B's vocabulary.
- **Not built yet on B's side** (PR #89 is in review; B asked the coordinator where to put it).
  Until B's schema marks `ENGINE_AUTO`, `analysis_backend.backend()` keeps the spike adapter, so a
  v2 run never fails at CONFIGURE for want of an engine in the signed spec.
- Checked against PR #89 at 491ce7b: its analyze step loads in the S1 runner with no errors; its
  settings paths (including `analysis.workers`) are used through `validate(path)`; its raising
  `validate` (SpecError on a bad settings leaf) is turned into a problem list; its new finding kinds
  flow through the ledger unchanged.

## 11. Review round 2 (Codex, five P1s)

Each was reproduced first: the new test was run against the previous code and failed.

| # | Finding | Change | Test |
|---|---|---|---|
| 1 | An interrupted first spec draft stayed `started` and uncharged (the try counter moved before the journal entry) | The key is fixed before the call and the try is counted after it returns. On resume, `Machine._settle_interrupted` charges every `started` entry once (its allowance, else the task cap) before anything runs | T33, T34 |
| 2 | A session with no cost and no session id was charged a duration estimate | `SdkTasks` never passes a duration: no reported cost means the whole allowance | S6 |
| 3 | The crash read-back resumed the conversation with no dollar cap, up to three times | One read-back, capped at its own budget (at most $0.25, from what the run has left after this call's allowance). With no budget left there is no read-back and the whole allowance is charged. A failed read-back is charged the allowance plus its cap | S7, S8 |
| 4 | A copied `run_state.json` could stop GATE from running in the golden traces | `copy_run` removes the v2 state files (`run_state.json`, `findings.jsonl`, `impact_ledger.jsonl`, `fix_*`, `gate_r*`). Checks B and D pass only if the GATE state actually ran (a GATE transition and its `gate_r<N>.json`) | G10, G11 |
| 5 | Owned paths were compared case-sensitively | `owned_path` compares case-insensitively and, for an existing file, by identity: the same inode as an owned file, or any hard link. The agent write-scope hook does the same | T35 |

## 12. Running on workstream B's analysis engine (checked at e19092d)

B built `engine: auto` (`api.ENGINE_AUTO`, `api.engine_needs`). `backend()` now switches to B's API, and the
machine routes four things through the backend so they use B's own rules:

- **Pre-sign check (SPEC).** B uses `schema.validate(load(path), pending_ok=True)`; the spike adapter keeps its
  section and settings checks.
- **Sign-off (SIGN, the owner CLI, the fix guard, the signed-spec finding).** B uses `toolkit/spec/signoff.py`,
  because B's analysis refuses an unsigned spec. The spike adapter keeps the machine's whole-file hash.
- **The spec the engine filter reads (ENGINE_CHOICE).** B uses `schema.load(path).raw`, with `engine_needs`
  (cost drivers and age groups are now checked against the card too).
- **Pipeline commands.** A step command that starts with a bare `python3` runs under the harness's interpreter.
  Under the system python, B's step failed with "No module named numpy".

The cross-engine check runs on the spike analysis only; with B it is recorded as skipped.

**End-to-end check** (B's toy spec with `engine: auto`, stubbed LLM tasks, from a `git archive` of e19092d): SPEC →
SIGN (B's sign-off, by lead) → EVIDENCE → ENGINE_CHOICE → CONFIGURE → ANALYZE (B's step) → CRITIQUE → GATE
(PATCH_OR_RETHINK) → FIX_LOOP → CRITIQUE → GATE (ACCEPT) → WRITE → PRESENTATION → GATE post-write (ACCEPT) →
FINAL `completed`, in 31 s with 2 workers.

- **Engine choice:** B's manifest is `complete`, with `engine_choice_sha256` set.
- **Contract checks:** none fired.
- **The fix:** changed only `analysis_settings.yaml`.
- **Impact rows:** they now separate what the agent changed (`files_changed`, measured before the rebuild) from
  what the rebuild rewrote (`outputs_changed`).

## 13. Fixes from the first blind real run (v2blind1)

The grader scored the run 1.0, but review found the gate had accepted with eight MEDIUM critic findings open. The
critics wrote `done_when` as prose; `done_when.py` counted every test as missing (three MEDIUM
`critique_done_when_missing`), and MEDIUM does not block ACCEPT under today's rule 4. The report also showed claim
ids (`mix.itn`) instead of values, and the unit costs were typed into the spec with no copy of their file.
Fixtures: the run's three critique YAMLs, `final_gate.json` and claims ledger (`tests/fixtures/v2blind1/`).

| # | Change | Test |
|---|---|---|
| 1 | The critic prompt gives the structured `done_when` forms `done_when.py` accepts, with an example. In v2, a HIGH or MEDIUM blocker whose test is missing or does not parse is a schema error, repaired by the T37 path (only that critic, told its errors) while repairs remain | T38 |
| 2 | v2 rule on top of today's: an ACCEPT with open HIGH/MEDIUM findings (critic blockers not closed by their test; harness-check findings) becomes PATCH_OR_RETHINK rule `4m` while rounds and budget remain, and those findings are planned in the fix loop; when none remain it becomes DECLARE_SCOPE rule `3m`, the findings are written to `declared_findings.yaml` and rendered in the report. The gate file and validator history record the action the machine takes. Golden-trace replays of stored v1 gates run without these rules (`v2_rules: false`) | T39, T42 |
| 3 | The writer writes `[CLAIM:id]`; `render_report` substitutes the value from `models/claims_ledger.yaml`. An unresolved reference, or a ledger id left bare in the writer's text, is a non-declarable HIGH write-up finding | T40 |
| 4 | Before the draft, every repository file the question names is copied into `data/inputs/` with its sha256 (`data/inputs_provenance.json`); every file the spec cites must be inside the run and is recorded with its sha256, else the spec does not pass the pre-sign check | T41 |

**Codex round 1 on #95 (ten P1s) and the coordinator's item 5.** Each was reproduced against a3a31b7 first.

| Finding | Change | Test |
|---|---|---|
| A resolved blocker whose test fails (`done_when_unmet`), or a missing presentation review, was excluded from the open set | Every HIGH/MEDIUM gate finding counts; only one that restates an open critic blocker is dropped as a duplicate | T43 |
| Typed result numbers were never checked | A number in the writer's text outside `[CLAIM:]` is a HIGH finding, except years, list numbering, digits inside identifiers, and numbers the owner gave (the signed spec, the question) | T44 |
| A copied `<!-- harness:` marker hid ids | The checks read `manuscript.md` (the writer's text), not the rendered report | T44b |
| A write-up fix was checked against a stale report | The fix loop's rebuild re-renders the report when a draft exists | T45 |
| Rule 3 / 3a DECLARE_SCOPE did not list the open findings | Every v2 DECLARE_SCOPE writes `declared_findings.yaml` and renders it | T46 |
| A cited path with a space, or a path field without an extension, escaped provenance | Every string value ending in a data extension and every `csv`/`file`/`path`/`table` field is checked | T47 |
| Copying could follow a symlinked `data/inputs` out of the run | No symlink on the way or hard-linked destination; copies are opened without following links | T48 |
| Case variants of `evals/` and `_truth` passed the exclusion | Compared case-insensitively, in the harness and in the agents' read guard | T49, S12 |
| The draft task could rewrite the staged inputs or their manifest | Both are harness-owned in the write guard; after the draft each copy's sha256 is checked against its record and its source | T50, S12 |
| Golden replays ran the new report checks | They run only with `v2_rules` | T51 |
| (item 5) A Grep or Glob at the repository root reached `evals/**/_truth` | A Read, Grep or Glob whose search root (its path, or a Glob pattern's literal prefix) contains the repository's `evals/` tree is refused unless confined to the run directory or `data/` | S13 |

**Codex round 2 on #95 (six P1s), each reproduced against 7aa9642 first:**
- **Typed numbers.** Signed, magnitude-suffixed and scientific forms (`-987654`, `987K`, `987e6`) are now caught (T52).
- **Given numbers.** Only the signed spec and the question count as given. A timestamp's digits in `metadata.json` no longer excuse a result (T53).
- **Unrendered references.** A `[CLAIM:]` left in the rendered report, for example inside code, is a HIGH finding (T54).
- **Engine config paths.** Any value that names an existing file or directory, including engine config paths without an extension, must be inside the run. Containment is checked before the backend's validation reads anything (T55).
- **Search guard.**
  - Grep's `glob` and `type` parameters are checked.
  - A search whose tree reaches a `_truth` entry, or a link into one, is refused even under an allowed root (S14).
  - `_truth` is also checked on the resolved path, so a Read through a symlink is refused (S15).

**Typed-number rule demoted to MEDIUM (process rule 4).** It is a text heuristic. On the stored manuscripts
(v2blind1, attempts 7 and 8, smokes 1 and 2, the Phase 40 smoke) it flags 550 numbers. At least 245 of them (45%) are
false positives by context class: section numbers, citations and DOIs, pasted decision trees, leading-zero ids,
interval levels, data ranges, unit denominators and software versions. So the finding is MEDIUM and declarable. The
interval level and every value in the spec, the ledger or the question are exempt, matched at the precision written
(T56). The structural cases stay HIGH: a bare ledger id and an unresolved or unrendered `[CLAIM:]`.

**Typed-number rule lowered to LOW (advisory).** Item 2 makes an open MEDIUM block ACCEPT in v2. A MEDIUM with a
false-positive rate of 45% or more would therefore spend fix rounds and add spurious declarations to correct reports.
The finding is now LOW. It is recorded in `findings.jsonl` and named in the presentation critic's prompt, but it never
blocks, never goes to the fix loop and is never declared. A run whose only open finding is a typed number reaches
ACCEPT (T57; the same run ended `run_failed` with the MEDIUM).

It goes back up to MEDIUM or HIGH only after both of these:
- its false-positive classes are exempted structurally (section and figure numbers, citations, pasted artifact text,
  identifiers, threshold levels, data ranges, unit denominators, versions);
- a new measurement on the stored runs.
