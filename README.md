# modeling-agentic

A multi-agent system for building publication-quality epidemiological and public-health models. Give it a research question; it researches the topic, pulls data, builds and calibrates models using established frameworks (Starsim, LASER, stisim, scipy/lmfit), runs quantitative rigor checks (uncertainty propagation, multi-structural comparison, identifiability), critiques the result from four angles — with one of them adversarial — and a **mechanical STAGE 7 gate** then computes the decision (PATCH / RETHINK / ACCEPT / DECLARE_SCOPE / RUN_FAILED) from structured blocker YAMLs so critique agents cannot talk themselves into generous verdicts. Finally a writer assembles the report.

Built on the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview).

## Architecture

A single **lead agent** orchestrates ten specialist subagents through a staged pipeline. The lead reads and writes files, delegates via the `Agent` tool, invokes the mechanical validator, makes PATCH/RETHINK decisions, and tracks state in `pipeline_state.yaml` for crash-safe resume.

| Agent | Role | Model | Tools |
|-------|------|:---:|------|
| **lead** | Orchestration, STAGE 7 mechanical gate, PATCH/RETHINK decisions | opus | Agent, Bash, Read, Write, Glob, Grep |
| **planner** | Literature review, benchmarks, hypotheses, modeling strategy, threads | opus | WebSearch, WebFetch, Read, Write, Glob, Grep |
| **data-agent** | Download datasets, validate quality, run exploratory analysis | sonnet | Bash, Write, Read, Edit, Glob |
| **modeler** | Build, run, and compare models; generate rigor artifacts + figures | opus | Bash, Write, Edit, Read, Glob, Grep, Agent |
| ↳ **model-tester** | *(subagent of modeler)* — reimplement a candidate or clone a published model as a reference baseline | sonnet | Bash, Write, Read, Edit, Glob |
| **analyst** | Interpret results, test hypotheses, assign causal labels | opus | Bash, Write, Read, Glob, Grep |
| **critique-methods** | Statistical validation, parameter provenance | opus | WebSearch, Read, Write, Glob, Grep |
| **critique-domain** | Scientific reasoning, citation verification, fit-for-purpose | opus | WebSearch, WebFetch, Read, Write, Glob, Grep |
| **critique-presentation** | Figure and writing quality | sonnet | Read, Write, Glob, Grep |
| **critique-redteam** | Adversarial: cross-file numeric audits, vintage drift, hidden assumptions | opus | WebSearch, WebFetch, Read, Write, Glob, Grep |
| **writer** | Assemble final publication-quality report; embed decision rule verbatim | opus | Read, Write, Edit, Glob, Grep |

## Pipeline

```
Question
   │
   ▼
 STAGE 1   PLAN                planner         → plan.md, threads.yaml, citations.md,
                                                  metadata.json{decision_year}
 STAGE 2   DATA                data-agent      → data/, data_quality.md (with **Vintage** lines),
                                                  data_provenance.md, eda.py, figures/eda_*.png
 STAGE 3   PRE-MODEL CHECK     lead            → feasibility assessment before modeling
 STAGE 4   MODEL               modeler         → models/, modeling_strategy.md (with
                                                  **Within-archetype error** when aggregating),
                                                  model_comparison.yaml, figures/fig_*.png,
                                                  citations.md (Parameter Registry), outcome_fn.py,
                                                  identifiability.yaml, decision_rule.md (if allocation)
                                (↳ model-tester in parallel for clone-first baselines)
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
 STAGE 8   WRITE                writer          → report.md (embeds decision_rule.md verbatim)
```

Stages 4–7 loop up to `--max-rounds` times. STAGE 7 is not a judgment call — the lead runs the validator and executes whatever action the exit code prescribes. The multi-line `STAGE 7 decision (round N/M)` stderr block is pasted verbatim into `progress.md` as an audit trail.

## Mechanical backstops (what the gate catches that critique agents miss)

Adversarial critique alone has proven insufficient: across three malaria runs, critique agents accepted reports that shipped with registered-but-unused parameters, pre-2010 calibration data used for 2024 decisions, and 774-row allocation tables without articulated rules. Phase 1.5, Phase 2, and Phase 3 added a series of pure-function checks that run independently of critique judgment.

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
- `models/identifiability.yaml` — loss function + per-parameter point estimates and plausible bounds.
- `modeling_strategy.md` with `**Within-archetype error**: Xpp` line when aggregating K<N archetypes.
- `decision_rule.md` (when allocation CSV produced) — tabular / tree / prose / non-compressible rule with `accuracy_vs_optimizer` and exceptions list.
- `data_quality.md` with `**Vintage**: YYYY`, `**Temporal coverage**`, `**Primary calibration**: yes/no` per dataset (written by the data-agent).

## Cloud compute (optional)

Azure Batch support for slow `outcome_fn` evaluations. `scripts/propagate_uncertainty.py --cloud` submits each of the 200 draws as a Batch task on `Standard_D4s_v5` dedicated nodes (or `Standard_A2_v2` for free-trial quota). See the `cloud-compute` skill; env vars live in `.env` (`AZ_*`), pool spin-up is auto, teardown is budget-bounded.

## Skills ecosystem

45 skills under `.claude/skills/` attached per-agent in `agents/__init__.py`. See `.claude/skills/README.md` for the indexed list; broad groups:

- **Pipeline rigor** — `adversarial-redteam`, `critique-blockers-schema`, `spec-compliance-rules`, `effect-size-priors`, `uncertainty-quantification`, `multi-structural-comparison`, `identifiability-analysis`, `decision-rule-extraction`
- **Modeling strategy & evaluation** — `modeling-strategy`, `modeling-fundamentals`, `model-fitness`, `model-validation`, `investigation-threads`
- **Epi fundamentals** — `basic_epi_modeling`, `sir-models`, `sir-elaborations`, `parameter-estimation`, `vaccination`, `vectors`, `surveillance`
- **Disease-specific** — `malaria-modeling`
- **Frameworks** — `laser-spatial-disease-modeling`; `starsim-dev` + 16 starsim-dev-* topic skills; `stisim-modeling`
- **Infrastructure** — `modelops-calabaria` (calibration/scaling), `cloud-compute` (Azure Batch), `sciris-utilities`, `epi-model-parametrization`

## Core principles embedded in the agents

- **Mechanical gates beat argument** — critique agents produce blocker YAMLs; the lead does not override the validator.
- **Clone-first, build-second** — modelers check for existing published code before reimplementing.
- **Frameworks, not hand-rolled dynamics** — Starsim / LASER / stisim for disease transmission; lmfit / statsmodels / PyMC / scipy for fitting.
- **Structured metadata over prose** — vintage, archetype error bounds, decision year, parameter registry all live in machine-readable form.
- **Causal labels required** — every finding must be tagged CAUSAL, ASSOCIATIONAL, or PROXY.
- **Investigation threads** — each hypothesis → data → model → figure → finding chain is tracked in `threads.yaml`.

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key

python main.py "Build a model of malaria incidence in Nigeria including ITNs, IRS, SMC, and case management, to inform Global Fund GC7 resource allocation (~$320M)."
```

### CLI options

| Flag | Default | Purpose |
|------|:-:|---------|
| `--max-rounds N` | 5 | Max critique-revision cycles |
| `--max-sessions N` | 10 | Max top-level sessions (for retry-after-crash / context recovery) |
| `--resume runs/<dir>` | — | Resume a prior run from its `pipeline_state.yaml` or detected artifacts |

## Run outputs

Each run writes to `runs/<timestamp>_<slug>/`:

```
runs/2026-04-23_2328_build-an-agent-based-model-of-malaria-tr/
├── metadata.json                  Question, decision_year, timestamps, sessions, elapsed
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
| `scripts/propagate_uncertainty.py` | 200-draw uncertainty propagation (local or `--cloud`) |
| `scripts/identifiability.py` | Fisher SEs + profile-likelihood scans |
| `scripts/cloud_batch.py` | Azure Batch SDK wrapper (pool mgmt, task submit, teardown) |

## Crash recovery and resume

Long runs (4–8 hours) occasionally crash mid-pipeline (API overload, OOM, orphaned processes). Handled in two places:

- **Within a run** — the lead writes `pipeline_state.yaml` after every completed stage. If a session dies, the next session reads that file and restarts from the next stage.
- **Across sessions** — `main.py` retries up to `--max-sessions` times with exponential backoff, using the saved `session_id` to resume lead context when possible, and rebuilding resume context from artifacts when not.
- **Orphaned-process cleanup** — `cleanup_orphaned_claude_processes()` kills stale CLI processes from prior crashes to prevent accumulating 400–600 MB zombies.

## Requirements

- Python 3.10+
- An [Anthropic API key](https://platform.claude.com/)
- Scientific Python stack: `numpy`, `scipy`, `pandas`, `matplotlib`, `seaborn`, `lmfit`, `statsmodels`, `xgboost`, `prophet`
- Disease modeling: `laser-generic` in `requirements.txt`; `starsim` and `stisim` install separately when skills invoke them
- Optional: `pymc` for Bayesian mechanistic models; Azure Batch + Storage account for `--cloud` UQ

## Layout

```
modeling-agentic/
├── main.py                    CLI entry point + retry/resume loop
├── agents/
│   ├── __init__.py            Lead prompt, agent registry, STAGE 1–8 orchestration
│   ├── planner.py
│   ├── data.py
│   ├── modeler.py             (includes model-tester definitions)
│   ├── analyst.py
│   ├── critique_{methods,domain,presentation}.py
│   ├── red_team.py
│   └── writer.py
├── scripts/                   Mechanical validators + rigor scripts
├── .claude/skills/            45 skills attached to agents by role
├── runs/                      Timestamped run outputs
├── benchmark/                 A/B test infrastructure (Lego-first variant)
└── data/, workspace/          Shared caches
```

## Giving feedback

File issues at the project repo, or use `/feedback` during a Claude Code session.
