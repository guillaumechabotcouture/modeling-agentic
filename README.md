# modeling-agentic

A multi-agent system for building publication-quality epidemiological and public-health models, for any disease area (malaria, measles, TB, HIV, polio, NCDs, ...). It has one pipeline, v2; v1 is retired:

- **v2 (the default since 2026-11-05; `main.py --v2` still works)**: code runs the pipeline as a state machine, every number comes from tested harness code (an owner-signed decision spec, a disease engine and the analysis engine), and LLMs do bounded tasks. See [v2: the stage machine](#v2-the-stage-machine).
- **v1 (retired by the owner on 2026-11-20, code removed)**: a lead agent and eleven specialist agents. See [Pipeline v1 (retired)](#pipeline-v1-retired). `main.py --v1`, a `--resume` of a run recorded as v1, a `cloud/run_cluster.py` config with `pipeline="v1"` and a `cloud/vm_job.py` command with `--v1` all refuse: "pipeline v1 is retired (owner, 2026-11-20); use v2".

Every run records the pipeline it ran, the code commit and its launcher in `metadata.json` (`pipeline`, `commit`, `launcher`); launchers and the eval grader read and check them (see [Pipeline identity](#pipeline-identity)).

Built on the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview).

> **Working in this codebase?** Read [`CLAUDE.md`](./CLAUDE.md) first — it lays out the layers (orchestration / rigor gates / toolkit / cloud / skills / runs), the agent registry, run exit statuses, conventions, and how to run every self-test. The phase-by-phase history is in [`CHANGELOG.md`](./CHANGELOG.md).

## Pipeline v1 (retired)

From 2026-04 to 2026-11, a single lead agent orchestrated eleven specialist subagents (planner, data agent,
modeler, analyst, five critics, a red team, a writer) through PLAN → DATA → MODEL → ANALYZE → CRITIQUE → GATE →
WRITE. The owner retired it on 2026-11-20, and its code (the agent registry and prompts, the lead prompt and hooks,
`main.run`, the replay lane) has been removed. `docs/design/v1-retirement.md` lists what was removed and what v2
shares with it. These shared parts remain:
- the mechanical STAGE 7 gate (`scripts/validate_critique_yaml.py`, run by v2's legacy gate mode);
- the rigor scripts it runs, described below;
- the skills.

Old v1 run directories stay readable and are never resumed. Their `metadata.json` says `pipeline: v1`; runs from before 2026-11-05 carry no `pipeline` field and are recognised by having no `run_state.json`.

## v2: the stage machine

`python3 main.py "<question>"` (or with `--v2`) runs `agents/stage_machine.py`. Code owns the state, every transition and every number; the state is saved to `run_state.json` after each transition, so `main.py --resume <run_dir>` continues where a run stopped.

```
SPEC → SIGN → EVIDENCE → ENGINE_CHOICE → CONFIGURE → ANALYZE → CRITIQUE → GATE
                                                                            │
        ┌───────────────────────────┬───────────────────────────────────────┤
        ▼                           ▼                                       ▼
    FIX_LOOP                  OWNER_DECISION                              WRITE → PRESENTATION → GATE (post-write) → FINAL
 (back to CRITIQUE,            (back to GATE)
  SPEC or ENGINE_CHOICE)
```

The full transition table is `EDGES` in `agents/stage_machine.py`.

| State | What happens | Who |
|---|---|---|
| SPEC, SIGN | An LLM task drafts `decision_spec.yaml` (question, decision maker, engine, interventions, costs, budget, outcomes, analysis settings); `toolkit/spec/schema.py` validates it; the owner signs its sha256 (`python3 agents/stage_machine.py sign <run_dir> --by owner`) | LLM task, then owner |
| EVIDENCE | Placeholder in this version | harness |
| ENGINE_CHOICE | An LLM task picks one of the eligible engines; the harness records `engine_choice.json` | LLM task, harness |
| CONFIGURE | The harness checks the spec against the engine and writes the run's build pipeline (`models/pipeline.yaml`) | harness |
| ANALYZE | `toolkit/analysis/run.py`: engine batch or direct runs, calibration with identifiability, point outcomes and the `toolkit/alloc` optimum, uncertainty, leave-one-group-out stability, sensitivity, decision tests, figures, claims ledger, deliverable CSV, each under `analysis/` | harness |
| CRITIQUE | `critique-domain` and `critique-redteam` write today's critique YAML schema | LLM tasks |
| GATE | The STAGE 7 validator's rule functions, run in-process on the findings ledger (`findings.jsonl`) | harness |
| FIX_LOOP | An LLM task plans fixes; each fix is one bounded task, and the harness measures its effect (`impact_ledger.jsonl`) | LLM tasks, harness |
| OWNER_DECISION | Waits for the owner on scope declarations that bear on the decision | owner |
| WRITE, PRESENTATION | An LLM task writes `manuscript.md`; the harness renders `report.md` from the claims ledger; `critique-presentation` reviews the draft before the post-write gate | LLM tasks, harness |

Each LLM task is one short Agent SDK session (`agents/v2_sdk_tasks.py`) with a task prompt, file tools only (Read, Glob, Grep, Write, Edit), a turn cap, a dollar cap and a hook that refuses writes outside the files the task owns. A run that stops for the owner prints `paused:awaiting_signoff` or `paused:awaiting_scope_signoff` and exits 2.

**Engines** (`toolkit/engines/`, behind the contract in `toolkit/engines/contract.py`; each has a `card.yaml` listing its mechanisms, parameters and valid domain):

| Engine | What it is |
|---|---|
| `malaria/` | Starsim malaria engine (human natural history and immunity, species-specific vectors, vector control and treatment interventions), fitted once to trial benchmarks (`malaria/benchmarks/`) |
| `compartmental/` | Starsim group-level transmission engine (age bands or birth cohorts, contact matrix, seasonal forcing) |
| `empirical/` | Package-rate engine: per-unit rates and per-package rate ratios estimated from a unit panel |

Design notes: `docs/architecture-v2.md`, `docs/design/v2c-orchestration.md` (state machine), `docs/design/v2-spec-and-analysis.md` (spec and analysis), `docs/design/v2a-malaria-engine.md`, `docs/design/v2d-compartmental-engines.md`.

## Mechanical backstops (what the gate catches that critique agents miss)

Adversarial critique alone has proven insufficient: in early runs, critique agents accepted reports that shipped with registered-but-unused parameters, calibration data more than a decade older than the decision year, and allocation tables of several hundred rows without an articulated rule. Phase 1.5, Phase 2, and Phase 3 added a series of pure-function checks that run independently of critique judgment.

| Check | Invoked via | Catches |
|---|---|---|
| **spec-compliance** | `--spec-compliance` | Framework/approach mismatch (e.g., "Starsim" imported but never `.run()`); budget underutilization; archetype K<N without structured `**Within-archetype error**` bound; data vintage gap ≥10yr on primary calibration target; methodology-of-record ≥15yr old. |
| **parameter-registry** | `--parameter-registry` | OR/RR conflation; code-vs-CSV cost crosscheck; `param_unregistered` tag scan (code → registry direction); `param_frozen_in_uq` / `param_not_in_code` coverage scan (registry → code direction; follows `outcome_fn.py` import closure). |
| **rigor-artifacts** | `--rigor-artifacts` | Missing `outcome_fn.py` / `uncertainty_report.yaml` / `model_comparison.yaml` / `identifiability.yaml` / `decision_rule.md`; DEGENERATE_FIT_DETECTED; UNIDENTIFIED_PARAMETERS; decision-rule malformed or low-accuracy-without-exceptions. |

The validator folds all HIGH mechanical violations into `unresolved_high` (synthetic `OBJ-NNN` / `RIG-NNN` blockers) or forces `structural_mismatch: true` — the critique agents' `structural_mismatch: false` verdict cannot override this.

## Rigor artifacts (produced by the modeler, checked at STAGE 7)

The modeler ships these alongside the model code:

- `citations.md` with a `## Parameter Registry` YAML block — every literature-sourced constant declared with kind, value/CI, source, and `code_refs` pointing to where it's used.
- `models/outcome_fn.py` — deterministic `outcome_fn(params: dict) -> dict` callable that runs the decision-relevant portion of the model under a parameter set. The registry's sampled draws flow through this.
- `models/model_comparison.yaml` — ≥3 candidate model structures (null, simple, full) with their training/LOO-CV errors.
- `models/identifiability.yaml` — loss function, `loss_type`, and per-parameter point estimates and plausible bounds.
- `models/claims_ledger.yaml` — every quantitative or categorical claim the report makes, with its source artifact and field.
- `models/eligibility_manifest.yaml` — for targeted interventions, which input fields hold the eligible and total populations (checked behaviorally by `scripts/eligibility_probe.py`).
- `modeling_strategy.md` with `**Within-archetype error**: Xpp` line when aggregating K<N archetypes.
- `decision_rule.md` (when allocation CSV produced) — tabular / tree / prose / non-compressible rule with `accuracy_vs_optimizer` and exceptions list.
- `data_quality.md` with `**Vintage**: YYYY`, `**Temporal coverage**`, `**Primary calibration**: yes/no` per dataset (written by the data-agent).

## Cloud compute (optional)

**IDM Coiled (current).** `cloud/run_cluster.py <config.py> launch` starts a whole run (v2) on Coiled from a config in `cloud/configs/`. Each run gets its own agent VM, keeps running when the launching laptop disconnects, and uploads its directory to the project storage account when it ends; `status`, `collect`, `health`, `sign` and `shutdown` manage it (see the script's docstring). `cloud/vm_job.py run <name> <job_id> --tree . --store outputs/<prefix> --cap-usd <USD> -- <command>` runs a detached job on its own VM in one command: it brings the cluster up, submits the job at once, polls until it ends, and stops the cluster whatever the outcome (non-zero exit with the reason unless the job is done with its outputs verified in storage; `--keep-on-failure` keeps the cluster after a failure for debugging). `up`/`start`/`wait`/`down` remain for interactive use. Launches (`run`, `up`, `run_cluster.py launch` with the config's `cap_usd`) are refused unless the owner-signed budget in `cloud/budget.py` covers the scope's spend plus the cap. Inside a run, `scripts/compute.py map` fans batches of model evaluations (calibration grids, uncertainty draws, v2 engine batches) out over the run's Dask workers, or runs them locally. See the `cloud-compute` skill.

### Pipeline identity

On 2026-11-05 an eval re-run meant to test a v2 change ran v1: `main.py` then defaulted to v1, the eval config passed no pipeline flag, and nothing recorded which pipeline ran. Now:

- `scripts/lib/pipeline_id.py` decides the pipeline (v2; `--v1` is refused as retired) and writes `pipeline`, `commit` and `launcher` into every run's `metadata.json` before the run starts. The commit of a launch tarball comes from `scripts/lib/code_commit.txt`, which `git archive` fills in (`.gitattributes` export-subst).
- `cloud/run_cluster.py`: each config declares `pipeline="v2"` (`"v1"` is refused as retired); the launcher passes it to `main.py` explicitly and, before anything is created, asks the shipped code which pipeline those arguments run and refuses any disagreement or a tree that cannot answer. `assignments.json` and each collected `result.json` record the launch's pipeline and commit and the run's own.
- `cloud/vm_job.py` refuses a `main.py` command without `--v2` (and one with `--v1`), and writes a launch record in the grader's format.
- `scripts/eval_suite.py` prints pipeline and commit with every score, refuses a run whose recorded identity is not its launch's, and `report` refuses to compare score files from different or unknown pipelines unless `--allow-mixed-pipelines` is given. Runs from before the change read `unknown`.
- `scripts/smoke_check.py <run_dir> --pipeline v2 --commit <sha> --expect <marker>` fails a smoke run that lacks the markers its PR's changed code should produce.

**Azure Batch (legacy, v1).** `scripts/propagate_uncertainty.py --cloud` can submit each uncertainty draw as an Azure Batch task through `scripts/cloud_batch.py`. Credentials live in `.env` (`AZ_*`) and are passed to agents only when `main.py --cloud` is set. Pools created by a run are deleted in a `finally` block. Install the extras with `pip install -r requirements-cloud.txt`.

## Skills ecosystem

58 skills under `.claude/skills/`. v2's spec task loads `decision-spec`. The others were attached to the retired v1 agents; Claude Code sessions in this repository still load them as project skills. See `.claude/skills/README.md` for the indexed list; broad groups:

- **Pipeline rigor** — `adversarial-redteam`, `critique-blockers-schema`, `spec-compliance-rules`, `effect-size-priors`, `uncertainty-quantification`, `multi-structural-comparison`, `identifiability-analysis`, `pre-model-identifiability-arithmetic`, `decision-rule-extraction`, `benchmark-locked-acceptance`, `evidence-sufficiency`, `pre-mortem-domain`, `sanity-schema`, `sensitivity-analysis-remediation`
- **Allocation and decisions** — `decision-spec` (v2), `allocation-toolkit`, `allocation-cross-validation`, `optimizer-method-selection`, `daly-weighted-analysis`, `eligible-population-consistency`, `ecological-fallacy-quantification`, `mechanistic-vs-hybrid-architecture`
- **Modeling strategy & evaluation** — `modeling-strategy`, `modeling-fundamentals`, `model-fitness`, `model-validation`, `investigation-threads`
- **Epi fundamentals** — `basic_epi_modeling`, `sir-models`, `sir-elaborations`, `parameter-estimation`, `vaccination`, `vectors`, `surveillance`
- **Disease-specific** — `malaria-modeling`
- **Frameworks** — `laser-spatial-disease-modeling`; `starsim-dev` + 16 starsim-dev-* topic skills; `stisim-modeling`
- **Infrastructure** — `modelops-calabaria` (calibration/scaling), `cloud-compute` (Azure Batch), `sciris-utilities`, `epi-model-parametrization`

## Core principles

- **Mechanical gates beat argument** — critique agents produce blocker YAMLs; the lead does not override the validator.
- **Clone-first, build-second** — modelers check for existing published code before reimplementing.
- **Disease-agnostic gates** — rigor checks reason about allocation units, eligible populations, and claims, never about a specific disease; domain knowledge lives in disease skills and the expert-priors registry.
- **Frameworks, not hand-rolled dynamics** — Starsim / LASER / stisim for disease transmission; lmfit / statsmodels / PyMC / scipy for fitting.
- **Structured metadata over prose** — vintage, archetype error bounds, decision year, parameter registry all live in machine-readable form.
- **Causal labels required** — every finding must be tagged CAUSAL, ASSOCIATIONAL, or PROXY.
- **Investigation threads** — each hypothesis → data → model → figure → finding chain is tracked in `threads.yaml`.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key

python main.py --v2 "Build a model of malaria incidence in Nigeria including ITNs, IRS, SMC, and case management, to inform Global Fund GC7 resource allocation (~$320M)."
# or any other disease area, e.g.
python main.py --v2 "Which districts should receive measles follow-up campaigns next year given a fixed vaccine and operations budget?"
```

### CLI options

| Flag | Default | Purpose |
|------|:-:|---------|
| `--max-rounds N` | 5 | Max critique-revision cycles |
| `--max-sessions N` | 10 | Accepted for existing launch configs (it capped the retired v1 lead's sessions) |
| `--resume runs/<dir>` | — | Resume a v2 run from its `run_state.json` (a run recorded as v1 is refused: v1 is retired) |
| `--v1` | off | Refused: pipeline v1 is retired (owner, 2026-11-20); use v2 |
| `--v2` | on | The v2 stage machine (the default; the flag is a no-op kept for existing commands) |
| `--launcher NAME` | `$MA_LAUNCHER` or `direct` | Name of the launcher, recorded in `metadata.json` (the cloud launchers set it) |
| `--signoff owner\|lead\|none` | owner | Who signs the decision spec before compute; `lead` for unattended test runs |
| `--signoff-hours H` | 24 | Accepted for existing launch configs; it set the retired v1 sign-off wait |
| `--max-budget-usd X` | — | Hard dollar cap for the whole run, including earlier sessions of a resumed run |
| `--methods-critic`, `--cross-model` | off | Accepted for existing launch configs (they added critics to the retired v1 STAGE 6) |
| `--cloud` | off | Pass cloud credentials (`AZ_*`, `AZURE_*`) to agents |
| `--preflight-only` | — | Refused: it ran the v1 lead session's live preflight, retired with v1 |
| `--skip-preflight` | — | Accepted for existing launch commands; v2 runs no preflight before a run |
| `--self-test` | — | Run inline self-tests (no API calls) and exit |

Exit codes: 0 accepted (`completed`, `completed_with_report_restored`); 2 finished without an accepted gate, or paused for the owner (resumable); 1 `harness_tampered`, `artifact_tampered` or error; 130 interrupted. The full status list is in `CLAUDE.md` ("Run outcomes").

## Run outputs

A run writes to `runs/<timestamp>_<slug>/`. The tree below is a v1 run's (old runs keep it); a v2 run adds `run_state.json`, `decision_spec.yaml` and its signoff, `engine_choice.json`, `findings.jsonl`, `impact_ledger.jsonl` and `analysis/`):

```
runs/<timestamp>_<question-slug>/      (gitignored; local only)
├── metadata.json                  Question, decision_year, timestamps, sessions, terminal status
├── final_gate.json                Post-run validator decision
├── trace.jsonl                    Structured log of every tool call + subagent lifecycle
├── pipeline_state.yaml            Resume state: current stage, round, completed stages
├── progress.md                    Stage log + verbatim STAGE 7 decision blocks
│
├── plan.md                        Literature review, benchmarks, candidate models, hypotheses
├── threads.yaml                   Investigation threads
├── citations.md                   References + ## Parameter Registry YAML
│
├── data/                          Downloaded datasets
├── data_quality.md                Per-dataset **Vintage**, **Temporal coverage**,
│                                  **Primary calibration** + quality notes
├── data_provenance.md             Licensing, caveats, dataset URIs
├── eda.py                         Exploratory analysis script
│
├── modeling_strategy.md           Level 1/2 choices, framework selection, **Within-archetype error**
├── models/
│   ├── model.py (and variants)    Candidate model implementations
│   ├── outcome_fn.py              Deterministic outcome callable for UQ
│   ├── model_comparison.yaml      ≥3 structures with errors
│   └── identifiability.yaml       Loss fn + parameter bounds for profile scans
│
├── model_comparison_formal.yaml   ICs + LOO-CV + DEGENERATE_FIT verdict (from compare_models.py)
├── uncertainty_report.yaml        Per-output CIs + categorical stability (propagate_uncertainty.py)
├── identifiability.yaml           Fisher SEs + profile-likelihood scan (identifiability.py)
├── decision_rule.md               Tabular/tree/prose rule + accuracy + exceptions (if allocation)
├── *allocation*.csv               Optimizer output (if policy task)
│
├── figures/                       eda_*.png, fig_*.png
├── figure_rationale.md            Why each figure exists
│
├── results.md                     Analyst interpretation with causal labels
│
├── critique_methods.{md,yaml}
├── critique_domain.{md,yaml}
├── critique_presentation.{md,yaml}
├── critique_redteam.{md,yaml}     Adversarial findings (R-NNN blocker IDs)
├── pre_mortem.yaml                Pre-model concerns (PM-NNN)
│
└── report.md                      Final report; decision_rule embedded in §Policy Recommendations
```

## Scripts (rigor gates and tools)

| Script | Purpose |
|---|---|
| `scripts/validate_critique_yaml.py` | STAGE 7 mechanical gate; emits action + JSON/stderr |
| `scripts/spec_compliance.py` | Framework / budget / archetype / vintage checks (from the question) |
| `scripts/effect_size_registry.py` | Parameter registry parse + tagging coverage |
| `scripts/compare_models.py` | Multi-structural LOO-CV + degenerate-fit detection |
| `scripts/propagate_uncertainty.py` | Uncertainty propagation (local or `--cloud`; draw floors in `effort_floors.yaml`) |
| `scripts/identifiability.py` | Full-Hessian Fisher SEs, ridge check, re-optimized profile likelihood |
| `scripts/eligibility_probe.py` | Behavioral check that targeted-intervention benefits scale with the eligible population |
| `scripts/coherence_audit.py`, `scripts/render_claims.py` | Claims-ledger rendering and report↔ledger binding audit |
| `scripts/effort_floors.py`, `scripts/benchmark_match.py` | Effort minimums and published-benchmark acceptance bands |
| `scripts/compute.py` | Compute service: fans batches of model evaluations out locally or over Dask/Coiled workers |
| `scripts/cloud_batch.py` | Azure Batch SDK wrapper (legacy; pool mgmt, task submit, teardown) |
| `scripts/self_tests.py` | Finds and runs every module's `--self-test` (as CI does) |

## Requirements

- Python 3.14 (the version the pins in `requirements.txt` were validated on; 3.10+ likely works but is untested)
- An [Anthropic API key](https://platform.claude.com/)
- `pip install -r requirements.txt` — pinned harness and v2 toolkit (`numpy`, `scipy`, `pandas`, `matplotlib`, `scikit-learn`, `PuLP`, `starsim`, `dask`), plus, through its include of `requirements-agent.txt`, the libraries agent-written model code and skill scripts import (`seaborn`, `lmfit`, `statsmodels`, `xgboost`, `optuna`, `geopandas`, `sciris`, `laser-core`, `laser.generic`, `polars`, `numba`)
- Optional: `requirements-cloud.txt` (`coiled`; Azure Storage for run uploads; Azure Batch, legacy) for cloud runs; `pymc` for Bayesian mechanistic models; `stisim` installs separately when its skill is used

## Layout

```
modeling-agentic/
├── main.py                    CLI entry point: v2 dispatch; refuses --v1 (retired)
├── agents/
│   ├── run_budget.py, run_finish.py   Spend ledger; gate command, exit codes and run upload
│   ├── enforcement.py         PreToolUse path/agent denies, harness hashing, env filtering
│   ├── integrity.py           Hash record of protected run artifacts
│   └── stage_machine.py, findings_ledger.py, fix_loop.py, analysis_backend.py,
│       v2_tasks.py, v2_sdk_tasks.py   v2 state machine and its bounded LLM tasks
├── toolkit/                   v2 harness code: spec/, engines/, analysis/, alloc/
├── cloud/                     Coiled launcher (run_cluster.py), configs/, keeper, VM jobs, run health
├── scripts/                   Mechanical validators + rigor scripts (scripts/lib/ shared helpers)
├── evals/                     Eval-suite questions (public half; scripts/eval_suite.py)
├── docs/                      Architecture and design notes (docs/design/)
├── changes/                   One change fragment per PR, folded into CHANGELOG.md at release
├── .claude/skills/            58 project skills (v2 loads decision-spec)
├── .claude/orchestration/     rigor_artifacts.yaml, effort_floors.yaml, expert_priors.yaml
├── CHANGELOG.md               Phase ledger
├── runs/                      Timestamped run outputs (gitignored)
└── data/                      Small shared reference CSVs
```

## Giving feedback

File issues at the project repo, or use `/feedback` during a Claude Code session.
