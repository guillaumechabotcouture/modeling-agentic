# CLAUDE.md — Project guide for code agents

**Purpose**: this file orients a fresh agent (or contributor) before
they touch the codebase. Read this first; then jump to the layer
you're working on. v2's bounded tasks load no settings, so pipeline
agents do not read it. It still describes the system without documenting
how the gates can be satisfied on paper.

## What this project is

A multi-agent pipeline that produces publication-quality
mathematical modeling deliverables (calibration, allocation
optimization, decision rules, sensitivity analyses) for public-health
questions in any disease area: for example "How should a national
program allocate its malaria budget across districts?", "Where should
measles campaigns run next year?", or "Which TB case-finding package
should each district get?". One pipeline runs; a retired one remains in
the tree until it is deleted:

- **v1 (retired by the owner on 2026-11-20)**: a **lead** agent chained
  the stages PLAN → DATA → MODEL → ANALYZE → CRITIQUE → GATE → WRITE,
  spawning specialist sub-agents. Its code (agent registry, prompts, lead
  prompt, hooks, `main.run`, replay lane) has been removed; the inventory is
  `docs/design/v1-retirement.md`. `main.py --v1`, a `--resume` of a run
  recorded as v1, a `cloud/run_cluster.py` config with `pipeline="v1"` and a
  `cloud/vm_job.py` command with `--v1` all refuse: "pipeline v1 is
  retired (owner, 2026-11-20); use v2".
- **v2 (default since 2026-11-05, `python3 main.py "<question>"`; `--v2`
  is accepted as a no-op alias)**: code owns the flow.
  `agents/stage_machine.py` steps through the states SPEC → SIGN →
  EVIDENCE → ENGINE_CHOICE → CONFIGURE → ANALYZE → CRITIQUE → GATE →
  FIX_LOOP / OWNER_DECISION → WRITE → PRESENTATION → FINAL (the full
  transition table is `EDGES` in that file) and persists them in
  `run_state.json`. LLMs do bounded tasks (`agents/v2_tasks.py`,
  `agents/v2_sdk_tasks.py`): draft the decision spec, pick an engine,
  critique, plan and make fixes, write. Every number comes from harness
  code: the owner-signed decision spec (`toolkit/spec/`), an engine
  (`toolkit/engines/`) and the analysis engine (`toolkit/analysis/`).
  Design: `docs/architecture-v2.md`, `docs/design/v2c-orchestration.md`,
  `docs/design/v2-spec-and-analysis.md`.

Old v1 run directories stay readable (their `metadata.json` says
`pipeline: v1`); they are never resumed.

## Decision design principles (owner default)

Every run follows `.claude/orchestration/decision_design_principles.md` unless its spec declares a departure
and its reason (the report repeats it); a principle the harness cannot yet compute is declared the same way. In short: think from first principles, and keep doing so at every stage; start from the decision, using the reprogramming framing from the decision date, with the
funded plan as comparator. Anchor baseline burden to external data-driven estimates, relative effects to trials,
and costs to current procurement prices. When engine and trials disagree, the trial wins unless the engine
explains why. Prefer measured data over modelled surfaces. Treat decision-moving inputs as ranges with
decision tests, and decide at the granularity the evidence supports. v2 tasks carry the file in their system
prompt (`agents/v2_sdk_tasks.py` `PRINCIPLES_FILE`, and the fix planner's prompt).

## The layers

```
┌────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION  main.py + agents/                              │
│  v2: stage machine (agents/stage_machine.py), findings ledger, │
│  fix loop, bounded SDK tasks (agents/v2_sdk_tasks.py); shared: │
│  tool/path enforcement (agents/enforcement.py), integrity,     │
│  run budget, run finish (gate command, run upload), preflight. │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  RIGOR GATES  scripts/                                         │
│  validate_critique_yaml.py — main gate, many _check_* fns      │
│  + standalone validators: identifiability.py,                  │
│  sensitivity_analysis.py, figure_validator.py,                 │
│  spec_compliance.py, coherence_audit.py, render_claims.py,     │
│  eligibility_probe.py, etc. Shared helpers in scripts/lib/.    │
│  Self-tests found and run by scripts/self_tests.py.            │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  TOOLKIT  toolkit/   (v2 harness code; tested, reused by runs) │
│  spec/ decision spec schema, priors, owner sign-off;           │
│  engines/ malaria (Starsim), compartmental (Starsim),          │
│  empirical package-rate, with cards and benchmarks;            │
│  analysis/ calibration, uncertainty, sensitivity, decision     │
│  tests, figures, claims (run.py); alloc/ costs, packages, ILP. │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  CLOUD  cloud/ + scripts/compute.py   (IDM Coiled, optional)   │
│  run_cluster.py launches runs from cloud/configs/*.py; keeper, │
│  run health, VM jobs (vm_job.py), worker-side helpers.         │
│  scripts/compute.py runs engine batches locally or on Dask.    │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  SKILLS  .claude/skills/                                       │
│  58 SKILL.md files: process/rigor contracts (disease-agnostic),│
│  epi fundamentals, frameworks (Starsim, LASER, stisim), and    │
│  disease-specific skills (e.g. malaria-modeling).              │
│  v2's spec task loads decision-spec; the rest are project      │
│  skills for Claude Code sessions in this repository.           │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  RUNS  runs/<timestamp>_<question>/   (gitignored, local only) │
│  Per-question output: data/, models/, figures/, plan.md,       │
│  results.md, decision_rule.md, report.md, critique_*.yaml,     │
│  scope_declaration.yaml, final_gate.json, model_card.yaml.     │
│  v2 runs add run_state.json, decision_spec.yaml (+ signoff),   │
│  engine_choice.json, findings.jsonl, analysis/.                │
└────────────────────────────────────────────────────────────────┘
```

## Where to start as a new contributor

| Goal                                  | Read first                                                    |
|---------------------------------------|---------------------------------------------------------------|
| Understand the CLI                    | `main.py` (`main()`, `main_v2`); run finishing, budget and preflight in `agents/run_finish.py`, `run_budget.py`, `run_preflight.py` |
| Understand what agents may touch      | `agents/enforcement.py` (PreToolUse denies, harness hashing, env filtering) and `agents/integrity.py` (hash record of protected run artifacts) |
| Add or modify a rigor gate            | `scripts/validate_critique_yaml.py` (`_check_*` fns) and any standalone validator under `scripts/`; shared loaders in `scripts/lib/` |
| Add a new skill                       | Create `.claude/skills/<name>/SKILL.md` with `name`/`description` frontmatter (a v2 task loads it by its SKILL.md path), run `python3 scripts/validate_skill_attachments.py` |
| Trace a phase change                  | `CHANGELOG.md` (phase commit ledger and gate internals); `changes/` for merged phases not yet folded in |
| See what artifacts a run produces     | Any local `runs/<timestamp>_*/` directory. `runs/` is gitignored, so there is no checked-in example; run the pipeline or ask a maintainer for one. |
| Understand the v2 run                 | `agents/stage_machine.py` (states, `EDGES`, `route_gate`), `agents/findings_ledger.py`, `agents/fix_loop.py`, `agents/analysis_backend.py`; design in `docs/design/v2c-orchestration.md` |
| Change a v2 engine or the analysis    | `toolkit/engines/contract.py` (the Engine contract) and each engine's `card.yaml`; `toolkit/analysis/run.py`; `toolkit/spec/schema.py` |
| Run on the cloud                      | `cloud/run_cluster.py` docstring (launch, status, collect, shutdown) and a config in `cloud/configs/`; `cloud-compute` skill |
| Reuse a finished run's model          | `<run>/model_card.md` (written at finalize by `scripts/model_card.py`), then `python3 scripts/ask.py <run> --set <input>=<value>` (refuses anything outside the card's valid domain) |

## Agents

v2 spawns no agents: each of its LLM tasks is one bounded SDK session
with a task-specific prompt and file tools only (`agents/v2_sdk_tasks.py`).
The v1 agent registry (`planner`, `data-agent`, `modeler`, `analyst`, five
critics, `writer`) was removed with v1. `agents/enforcement.py` still holds
the agent-name permission policy it enforced, pending a follow-up
(`docs/design/v1-retirement.md`).

Skills: `scripts/validate_skill_attachments.py` checks every SKILL.md's
frontmatter and that each skill a module under `agents/` loads by path
exists (`--show-orphans` lists the rest). `asta-literature-search`,
`pdf-text-extraction` and `semantic-scholar-lookup` are MCP-provided (no
local SKILL.md), allowed via `_MCP_SKILLS`.

## Run outcomes

`main.py` records a terminal status in `metadata.json` and exits with:

| Exit | Statuses |
|------|----------|
| 0    | `completed`, `completed_with_report_restored` |
| 2    | `completed_scope_declared`, `completed_gate_not_accepted`, `completed_gate_error`, `completed_gate_not_run`, `completed_no_report`, `run_failed`, `budget_exhausted`, `awaiting_signoff`, `awaiting_scope_signoff`, `completed_scope_declined`, `paused:awaiting_signoff`, `paused:awaiting_scope_signoff`, `paused:awaiting_budget`, `paused:awaiting_spec_amendment` (v2) |
| 1    | `harness_tampered`, `artifact_tampered`, errors |
| 130  | `interrupted` |

`awaiting_signoff` (the decision brief) and `awaiting_scope_signoff` (Phase 49
β: declarations of findings that bear on the primary decision) are resumable
with `--resume` once signed; `completed_scope_declined` means the owner
declined such a declaration and no rounds were left to fix it.
A v2 run prints `paused:<reason>` when its state machine
stops for the owner, at SIGN (the decision spec: `python3
agents/stage_machine.py sign <run_dir> --by owner`) or at OWNER_DECISION
(scope declarations, or findings whose remedy is an owner amendment: `python3 scripts/scope_signoff.py sign
<run_dir> --by owner`), or before a paid task whose
allowance is below the floor (`paused:awaiting_budget`: resume with a higher `--max-budget-usd`), or at SPEC when
a rethink would change a signed spec (`paused:awaiting_spec_amendment`: read `spec_rethink_request.json`, amend the
spec and relaunch with reuse), and exits 2;
`main.py --resume <run_dir>` continues from `run_state.json`.

Every run records which pipeline ran: `metadata.json` holds `pipeline`
(`v1`|`v2`), `commit` and `launcher` (plus one `invocations` entry per
start or resume), written by `scripts/lib/pipeline_id.py` before the run
starts. A run recorded under one pipeline is not resumed under the other,
and a run recorded as v1 is not resumed at all.
Launchers state the pipeline explicitly and never rely on the default:
`cloud/run_cluster.py` configs set `pipeline=` and the launch refuses a
config whose declaration the shipped code does not confirm;
`cloud/vm_job.py` refuses a `main.py` command without `--v2`.
Readers treat a missing field as `unknown`, never as a pipeline.

Cloud and third-party credentials (`AZ_*`, `AZURE_*`, `OPENAI_*`,
`AWS_*`, `GOOGLE_*`) are blanked in the agent environment unless
`main.py --cloud` is passed (`agents/enforcement.py`, `filtered_env`).

## Phase history

The phase commit ledger (Phases 2-21) and contributor-level gate
internals live in `CHANGELOG.md`. New rows arrive through change
fragments (see Conventions).

## Conventions

- **Disease-agnostic design**: gate logic, process skills, and prompts
  must not hardcode a disease, country, intervention, or unit name
  (no "LGA", "774", "SMC", "PfPR" in `scripts/` logic). Domain
  specifics live in disease skills (e.g. `malaria-modeling`) and in
  `.claude/orchestration/expert_priors.yaml`, gated by
  `applies_when` keywords. Use generic terms: allocation unit,
  eligible population, intervention package, calibration target.
- **Greek-letter commits**: each phase ships ≤4-5 Greek-letter
  sub-commits (α, β, γ …), often squash-merged as one PR. The letter
  is reused as a reference in code comments and skills.
- **Change fragments, not ledger edits**: a PR does not edit the
  phase ledger table in `CHANGELOG.md` or the Status section of
  `docs/design/improvement-plan.md`. It adds one new file,
  `changes/<branch>.md`, holding the ledger row and an optional Status
  line (format in `changes/README.md`). A maintainer folds the
  fragments in at release with `python3 scripts/assemble_changes.py
  --write` and commits the result. CI (`assemble_changes.py
  --check-pr`) fails a PR that edits either region other than as that
  exact fold.
- **Fail closed**: a gate that cannot read, parse, or recognize its
  input (missing file, wrong type, unknown verdict string) must emit a
  finding, not return an empty list.
- **Self-test discipline**: `main.py`, every module under `scripts/`,
  `toolkit/` and `cloud/`, and the v2 harness modules under `agents/`
  are expected to have a `--self-test` flag that runs inline test cases.
  `scripts/self_tests.py` finds them; a module in that scope without one
  fails discovery unless `EXEMPT` in that file names it with a reason.
  Run them all before opening a PR (below).
- **Retro-checks against real runs**: when a phase ships a new gate,
  the PR description includes a retro-check against a recent local
  `runs/` directory showing the gate produces the expected output.
- **Rigor-artifact manifest**: the source of truth for artifact paths
  and stages is `.claude/orchestration/rigor_artifacts.yaml`
  (Phase 16 α); effort minimums are in
  `.claude/orchestration/effort_floors.yaml`.
  `scripts/validate_critique_yaml.py` resolves paths via
  `lib.rigor_artifacts.artifact_path()`. Skills must defer to these
  manifests for thresholds rather than restating numbers.

## How to verify changes end-to-end

Before opening a PR, every command below must exit 0 (each self-test
reports "OK: all self-test cases passed."):

```bash
python3 scripts/self_tests.py --jobs 2             # every --self-test, as CI runs them
python3 scripts/self_tests.py --list               # what runs, and each exemption's reason
python3 scripts/validate_skill_attachments.py      # frontmatter + phantoms
python3 scripts/lib/expert_priors.py --validate    # priors corroboration
python3 scripts/regression_eval.py                 # planted-defect regression eval (CI)
python3 scripts/gate_fuzz.py                       # bad-input fuzz over every gate_findings check (CI)
```

Install with `pip install -r requirements.txt` (pinned; it includes
`requirements-agent.txt`, the libraries agent-written model code and
skill scripts import; add `-r requirements-cloud.txt` for Coiled, run
storage and the legacy Azure Batch path).

Live verification (a real run, on the cloud: `cloud/run_cluster.py
<config> launch`, or `cloud/vm_job.py run` for one job). Prefer
alternating the disease area between retros so gates are not tuned to
one domain. The pipeline is v2 (v1 is retired); state `--v2` anyway in any
command you write down:

```bash
python3 main.py --v2 "<research question>" --max-rounds 4 --signoff owner
```

A v2 run produces `runs/<timestamp>_<question>/` with `run_state.json`,
the gate records and the report. It pauses at SIGN until the owner signs
the decision spec (`python3 agents/stage_machine.py sign <run_dir> --by
owner`); use `--signoff lead` for unattended test runs.

**A smoke must show that the changed code ran.** A run that finishes is
not evidence: on 2026-11-05 a re-run meant to test a v2 fix ran v1 and
finished. Each PR that changes run behaviour names, in its description,
the markers only its changed code produces (a field it records, a file
only it writes, a stage the machine reaches), and the smoke is checked
against them:

```bash
python3 scripts/smoke_check.py <collected run dir> --pipeline v2 --commit <launched sha> \
  --expect 'run_state.json:history' --expect '<file only the new code writes>'
```

The command exits 1 and lists each missing marker. Marker forms: `PATH`,
`PATH:KEY`, `PATH:KEY=VALUE`, `PATH:KEY^=PREFIX`, `PATH~REGEX`
(`python3 scripts/smoke_check.py --help`).

## Pointers to deeper docs

- `README.md` — high-level project description (audience: human reader)
- `CHANGELOG.md` — phase ledger and gate internals
- `docs/design/v1-retirement.md` — what pipeline v1 left behind and why
- `.claude/skills/README.md` — skill index
- `.claude/skills/<name>/SKILL.md` — domain or process knowledge
