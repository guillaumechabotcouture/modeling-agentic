# modeling-agentic

A multi-agent system for building publication-quality epidemiological and public-health models, for any disease area (malaria, measles, TB, HIV, polio, NCDs, ...). Give it a research question; it researches the topic, pulls data, builds and calibrates models using established frameworks (Starsim, LASER, stisim, scipy/lmfit), runs quantitative rigor checks (uncertainty propagation, multi-structural comparison, identifiability), critiques the result from several angles (methods, domain, presentation, an adversarial red team, a pre-model pre-mortem, and a post-write evidence-sufficiency check), and a **mechanical STAGE 7 gate** then computes the decision (PATCH / RETHINK / ACCEPT / DECLARE_SCOPE / RUN_FAILED) from structured blocker YAMLs so critique agents cannot talk themselves into generous verdicts. Finally a writer assembles the report.

Built on the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview).

> **Working in this codebase?** Read [`CLAUDE.md`](./CLAUDE.md) first — it lays out the four layers (orchestration / rigor gates / skills / runs), the agent registry, run exit statuses, conventions, and the self-test list. The phase-by-phase history is in [`CHANGELOG.md`](./CHANGELOG.md).

## Architecture

A single **lead agent** orchestrates eleven specialist subagents through a staged pipeline. The lead delegates via the `Agent` tool (registered agent types only), invokes the mechanical validator, makes PATCH/RETHINK decisions, and tracks state in `pipeline_state.yaml` for crash-safe resume. A PreToolUse hook (`agents/enforcement.py`) stops agents from editing harness code or files they don't own: the lead cannot write critique YAMLs or `models/**`, and script outputs can only be written by their scripts. The harness is hashed at start and end of a run, and any change marks the run `harness_tampered`.

| Agent | Role | Model | Tools |
|-------|------|:---:|------|
| **lead** | Orchestration, STAGE 7 mechanical gate, PATCH/RETHINK decisions | opus | Agent, Bash, Read, Write, Glob, Grep, Skill |
| **planner** | Literature review, benchmarks, hypotheses, modeling strategy, threads | opus | WebSearch, WebFetch, Read, Glob, Grep, Bash, Skill |
| **data-agent** | Download datasets, validate quality, run exploratory analysis | sonnet | WebSearch, WebFetch, Bash, Write, Read, Glob |
| **modeler** | Build, run, and compare models; generate rigor artifacts + figures | opus | Bash, Write, Edit, Read, Glob, Grep |
| **analyst** | Interpret results, test hypotheses, assign causal labels; claims ledger | opus | Read, Bash, Write, Glob, Grep |
| **critique-premortem** | Pre-model, clean-slate adversarial critique of the plan (`pre_mortem.yaml`) | opus | Read, Write, Glob, Grep, WebSearch, WebFetch |
| **critique-methods** | Statistical validation, parameter provenance | opus | Read, Write, Glob, Grep, WebSearch |
| **critique-domain** | Scientific reasoning, citation verification, fit-for-purpose | opus | Read, Write, Glob, Grep, WebSearch, WebFetch |
| **critique-presentation** | Figure and writing quality | sonnet | Read, Write, Glob, Grep |
| **critique-redteam** | Adversarial: ledger consistency, external sanity, vintage drift, hidden assumptions | opus | Read, Write, Bash, Glob, Grep, WebSearch, WebFetch |
| **critique-sufficiency** | Post-write check that each claim's strength matches its evidence | opus | Read, Write, Glob, Grep |
| **writer** | Assemble final publication-quality report; embed decision rule verbatim | opus | Read, Write, Glob |

`agents/__init__.py` (`_registry()`) is the source of truth for this table.

## Pipeline

```
Question
   │
   ▼
 STAGE 1   PLAN                planner         → plan.md, threads.yaml, citations.md,
                                                  metadata.json{decision_year}
 STAGE 2   DATA                data-agent      → data/, data_quality.md (with **Vintage** lines),
                                                  data_provenance.md, eda.py, figures/eda_*.png
 STAGE 3   PRE-MODEL CHECK     lead + premortem → identifiability_a_priori.yaml, pre_mortem.yaml
 STAGE 4   MODEL               modeler         → models/, modeling_strategy.md (with
                                                  **Within-archetype error** when aggregating),
                                                  eligibility_manifest.yaml (targeted interventions),
                                                  model_comparison.yaml, figures/fig_*.png,
                                                  citations.md (Parameter Registry), outcome_fn.py,
                                                  identifiability.yaml, decision_rule.md (if allocation)
 STAGE 5   ANALYZE             analyst         → results.md (CAUSAL/ASSOCIATIONAL/PROXY labels)
 STAGE 5b  RIGOR (Bash, 3 scripts, no subagent):
             multi-structural  compare_models.py      → model_comparison_formal.yaml
             uncertainty       propagate_uncertainty.py (local or Azure Batch cloud)
                                                       → uncertainty_report.yaml
             identifiability   identifiability.py     → identifiability.yaml
 STAGE 6   CRITIQUE (║)        methods + domain + presentation + redteam (parallel)
                                                → critique_*.md + critique_*.yaml (blocker manifests)
 STAGE 7   MECHANICAL GATE     lead runs validate_critique_yaml.py with ALL THREE flags:
             --spec-compliance        framework / approach / budget / archetype / data vintage /
                                       methodology vintage against the research question
             --parameter-registry     effect-size priors (OR/RR conflation), code-vs-CSV cost
                                       crosscheck, tagging coverage (catches R-022 class)
             --rigor-artifacts        UQ / MSC / identifiability / decision_rule presence + validity
          │
          ├── RETHINK_STRUCTURAL  → back to STAGE 4 with architectural instructions
          ├── PATCH_OR_RETHINK    → HIGH blockers remain, choose PATCH or RETHINK
          ├── DECLARE_SCOPE       → document what the model does/doesn't answer, proceed
          ├── ACCEPT              → proceed to STAGE 8
          └── RUN_FAILED          → structural mismatch with no rounds left; end, no writer
          │
          ▼
 STAGE 8   WRITE                writer          → report.md (embeds decision_rule.md verbatim;
                                                  numbers via [CLAIM:id] from claims_ledger.yaml)
          then writer-QA, coherence audit, sufficiency critic
 POST-RUN  main.py re-runs the validator → final_gate.json; terminal status + exit code
```

Stages 4–7 loop up to `--max-rounds` times. STAGE 7 is not a judgment call — the lead runs the validator and executes whatever action the exit code prescribes. The multi-line `STAGE 7 decision (round N/M)` stderr block is pasted verbatim into `progress.md` as an audit trail.

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

Azure Batch support for slow `outcome_fn` evaluations. `scripts/propagate_uncertainty.py --cloud` submits each draw as a Batch task on `Standard_D4s_v5` dedicated nodes (or `Standard_A2_v2` for free-trial quota). See the `cloud-compute` skill. Credentials live in `.env` (`AZ_*`) and are passed to agents only when `main.py --cloud` is set. Pools created by a run are deleted in a `finally` block. Install the extras with `pip install -r requirements-cloud.txt`.

## Skills ecosystem

56 skills under `.claude/skills/`, attached per-agent in `agents/__init__.py`. Domain skills attach only when the question matches their trigger words. See `.claude/skills/README.md` for the indexed list; broad groups:

- **Pipeline rigor** — `adversarial-redteam`, `critique-blockers-schema`, `spec-compliance-rules`, `effect-size-priors`, `uncertainty-quantification`, `multi-structural-comparison`, `identifiability-analysis`, `decision-rule-extraction`
- **Modeling strategy & evaluation** — `modeling-strategy`, `modeling-fundamentals`, `model-fitness`, `model-validation`, `investigation-threads`
- **Epi fundamentals** — `basic_epi_modeling`, `sir-models`, `sir-elaborations`, `parameter-estimation`, `vaccination`, `vectors`, `surveillance`
- **Disease-specific** — `malaria-modeling`
- **Frameworks** — `laser-spatial-disease-modeling`; `starsim-dev` + 16 starsim-dev-* topic skills; `stisim-modeling`
- **Infrastructure** — `modelops-calabaria` (calibration/scaling), `cloud-compute` (Azure Batch), `sciris-utilities`, `epi-model-parametrization`

## Core principles embedded in the agents

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

python main.py "Build a model of malaria incidence in Nigeria including ITNs, IRS, SMC, and case management, to inform Global Fund GC7 resource allocation (~$320M)."
# or any other disease area, e.g.
python main.py "Which districts should receive measles follow-up campaigns next year given a fixed vaccine and operations budget?"
```

### CLI options

| Flag | Default | Purpose |
|------|:-:|---------|
| `--max-rounds N` | 5 | Max critique-revision cycles |
| `--max-sessions N` | 10 | Max top-level sessions (for retry-after-crash / context recovery) |
| `--resume runs/<dir>` | — | Resume a prior run from its `pipeline_state.yaml` or detected artifacts |
| `--cloud` | off | Pass Azure credentials to agents for Batch UQ |
| `--self-test` | — | Run inline self-tests (no API calls) and exit |

Exit codes: 0 accepted (`completed`, `completed_with_report_restored`); 2 finished without an accepted gate (`completed_scope_declared`, `completed_gate_not_accepted`, `completed_gate_error`, `completed_gate_not_run`, `completed_no_report`, `run_failed`); 1 `harness_tampered` or error; 130 interrupted.

## Run outputs

Each run writes to `runs/<timestamp>_<slug>/`:

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

## Scripts (invoked by the lead via Bash)

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
| `scripts/cloud_batch.py` | Azure Batch SDK wrapper (pool mgmt, task submit, teardown) |

## Crash recovery and resume

Long runs (4–8 hours) occasionally crash mid-pipeline (API overload, OOM, orphaned processes). Handled in two places:

- **Within a run** — the lead writes `pipeline_state.yaml` after every completed stage. If a session dies, the next session reads that file and restarts from the next stage.
- **Across sessions** — `main.py` retries up to `--max-sessions` times with exponential backoff, resuming the lead conversation by `session_id` when one was captured and rebuilding resume context from artifacts otherwise.
- **Orphaned-process cleanup** — `cleanup_orphaned_claude_processes()` kills stale CLI processes from prior crashes to prevent accumulating 400–600 MB zombies.

## Requirements

- Python 3.14 (the version the pins in `requirements.txt` were validated on; 3.10+ likely works but is untested)
- An [Anthropic API key](https://platform.claude.com/)
- `pip install -r requirements.txt` — pinned harness, scientific stack (`numpy`, `scipy`, `pandas`, `matplotlib`, `seaborn`, `lmfit`, `statsmodels`, `scikit-learn`, `xgboost`, `PuLP`, `optuna`, `geopandas`), and frameworks (`starsim`, `laser-core`, `laser.generic`)
- Optional: `requirements-cloud.txt` (Azure Batch + Storage) for `--cloud` UQ; `pymc` for Bayesian mechanistic models; `stisim` installs separately when its skill is used

## Layout

```
modeling-agentic/
├── main.py                    CLI entry point + retry/resume loop
├── agents/
│   ├── __init__.py            Lead prompt, agent registry, hooks
│   ├── enforcement.py         PreToolUse path/agent denies, harness hashing, env filtering
│   ├── planner.py, data.py, modeler.py, analyst.py
│   ├── critique_{methods,domain,presentation,premortem,sufficiency}.py
│   ├── red_team.py
│   └── writer.py
├── scripts/                   Mechanical validators + rigor scripts (scripts/lib/ shared helpers)
├── .claude/skills/            56 skills attached to agents by role
├── .claude/orchestration/     rigor_artifacts.yaml, effort_floors.yaml, expert_priors.yaml
├── CHANGELOG.md               Phase ledger
├── runs/                      Timestamped run outputs (gitignored)
└── data/                      Small shared reference CSVs
```

## Giving feedback

File issues at the project repo, or use `/feedback` during a Claude Code session.
