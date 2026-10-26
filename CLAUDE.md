# CLAUDE.md — Project guide for code agents

**Purpose**: this file orients a fresh agent (or contributor) before
they touch the codebase. Read this first; then jump to the layer
you're working on. It is also loaded into every pipeline agent's
context (`setting_sources=["project"]`), so it describes the system
without documenting how the gates can be satisfied on paper.

## What this project is

A multi-agent pipeline that produces publication-quality
mathematical modeling deliverables (calibration, allocation
optimization, decision rules, sensitivity analyses) for public-health
questions in any disease area: for example "How should a national
program allocate its malaria budget across districts?", "Where should
measles campaigns run next year?", or "Which TB case-finding package
should each district get?". The harness chains a **lead** agent
through the pipeline stages (PLAN → DATA → MODEL → ANALYZE → CRITIQUE
→ GATE → WRITE), spawning specialist sub-agents and running mechanical
rigor checks between rounds.

## The four layers

```
┌────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION  main.py + agents/                              │
│  Lead loop, agent registry, prompt definitions, skill          │
│  attachments, tool/path enforcement (agents/enforcement.py).   │
│  Runs the STAGE 7 validator after the session → final_gate.json│
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
│  Every script under scripts/ has --self-test.                  │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  SKILLS  .claude/skills/                                       │
│  56 SKILL.md files: process/rigor contracts (disease-agnostic),│
│  epi fundamentals, frameworks (Starsim, LASER, stisim), and    │
│  disease-specific skills (e.g. malaria-modeling).              │
│  Attached to agents in agents/__init__.py.                     │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  RUNS  runs/<timestamp>_<question>/   (gitignored, local only) │
│  Per-question output: data/, models/, figures/, plan.md,       │
│  results.md, decision_rule.md, report.md, critique_*.yaml,     │
│  scope_declaration.yaml, final_gate.json.                      │
└────────────────────────────────────────────────────────────────┘
```

## Where to start as a new contributor

| Goal                                  | Read first                                                    |
|---------------------------------------|---------------------------------------------------------------|
| Understand the lead loop              | `main.py` + `agents/__init__.py` (`_registry()`, `build_lead_prompt()`, `create_hooks()`) |
| Understand what agents may touch      | `agents/enforcement.py` (PreToolUse denies, harness hashing, env filtering) and `agents/integrity.py` (hash record of protected run artifacts) |
| Add or modify a rigor gate            | `scripts/validate_critique_yaml.py` (`_check_*` fns) and any standalone validator under `scripts/`; shared loaders in `scripts/lib/` |
| Tweak the modeler agent's behavior    | `agents/modeler.py` SYSTEM_PROMPT (~950 lines rendered; § 4 rigor contracts, timeline rendered from the manifest) |
| Add a new skill                       | Create `.claude/skills/<name>/SKILL.md` with `name`/`description` frontmatter, attach in `agents/__init__.py`, run `python3 scripts/validate_skill_attachments.py` |
| Trace a phase change                  | `CHANGELOG.md` (phase commit ledger and gate internals)       |
| See what artifacts a run produces     | Any local `runs/<timestamp>_*/` directory. `runs/` is gitignored, so there is no checked-in example; run the pipeline or ask a maintainer for one. |

## Agents

The registry in `agents/__init__.py` (`_registry()`) is the source of
truth for agents, models, tools, `maxTurns`, and skills. Current
agents: `planner`, `data-agent`, `modeler`, `analyst`,
`critique-methods` (opt-in, `--methods-critic`), `critique-domain`, `critique-premortem`,
`critique-presentation`, `critique-redteam`, `critique-sufficiency`,
`writer`. The lead may only spawn these registered types; built-in
types such as `general-purpose` are denied.

Skills: process/rigor skills attach unconditionally; domain skills
(`malaria-modeling`, `vectors`, Starsim/stisim sub-skills, …) attach
only when the question matches their trigger words
(`select_skills()`). `asta-literature-search`, `pdf-text-extraction`,
and `semantic-scholar-lookup` are MCP-provided (no local SKILL.md) and
allowed via `_MCP_SKILLS` in `scripts/validate_skill_attachments.py`.
Run `python3 scripts/validate_skill_attachments.py --show-orphans` for
the live attachment picture.

## Run outcomes

`main.py` records a terminal status in `metadata.json` and exits with:

| Exit | Statuses |
|------|----------|
| 0    | `completed`, `completed_with_report_restored` |
| 2    | `completed_scope_declared`, `completed_gate_not_accepted`, `completed_gate_error`, `completed_gate_not_run`, `completed_no_report`, `run_failed`, `budget_exhausted` |
| 1    | `harness_tampered`, `artifact_tampered`, errors |
| 130  | `interrupted` |

Cloud credentials (`AZ_*` / `AZURE_*`) are stripped from the agent
environment unless `main.py --cloud` is passed.

## Phase history

The phase commit ledger (Phases 2-21) and contributor-level gate
internals live in `CHANGELOG.md`. Add a row there for every phase.

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
- **Fail closed**: a gate that cannot read, parse, or recognize its
  input (missing file, wrong type, unknown verdict string) must emit a
  finding, not return an empty list.
- **Self-test discipline**: every script under `scripts/` (and
  `scripts/lib/`), plus `main.py`, has a `--self-test` flag that runs
  inline test cases. Run all of them before opening a PR (list below).
- **Retro-checks against real runs**: when a phase ships a new gate,
  the PR description includes a retro-check against a recent local
  `runs/` directory showing the gate produces the expected output.
- **Rigor-artifact manifest**: the source of truth for artifact paths
  and stages is `.claude/orchestration/rigor_artifacts.yaml`
  (Phase 16 α); effort minimums are in
  `.claude/orchestration/effort_floors.yaml`. `agents/modeler.py`
  renders the manifest into its prompt at module load and
  `scripts/validate_critique_yaml.py` resolves paths via
  `lib.rigor_artifacts.artifact_path()`. Skills must defer to these
  manifests for thresholds rather than restating numbers.

## How to verify changes end-to-end

Before opening a PR, every command below must report
"OK: all self-test cases passed." (or exit 0):

```bash
for f in main.py \
  scripts/allocation_robustness.py scripts/benchmark_match.py \
  scripts/build_claims_ledger.py scripts/data_schema.py \
  scripts/cloud_batch.py scripts/coherence_audit.py \
  scripts/compare_models.py scripts/compute.py \
  scripts/cross_model_critic.py \
  scripts/effect_size_registry.py \
  scripts/effort_floors.py scripts/eligibility_probe.py \
  scripts/figure_validator.py scripts/identifiability.py \
  scripts/identifiability_a_priori.py scripts/numeric_consistency.py \
  scripts/optimization_quality.py scripts/plan_criteria.py \
  scripts/propagate_uncertainty.py scripts/render_claims.py \
  scripts/results_store.py scripts/run_autopsy.py \
  scripts/score_reports.py scripts/autopsy_review.py \
  scripts/sanity_checks.py scripts/sdk_scenarios.py \
  scripts/sensitivity_analysis.py \
  scripts/source_checks.py \
  scripts/spec_compliance.py scripts/validate_critique_yaml.py \
  scripts/validate_skill_attachments.py scripts/watch_run.py \
  scripts/within_zone_sensitivity.py scripts/writer_qa.py \
  scripts/lib/expert_priors.py scripts/lib/md_helpers.py \
  scripts/lib/positive_control.py scripts/lib/provenance.py \
  scripts/lib/rigor_artifacts.py scripts/lib/safe_io.py \
  scripts/lib/text_spans.py; do
  echo "== $f"; python3 "$f" --self-test || echo "FAILED: $f"
done
python3 scripts/validate_skill_attachments.py      # frontmatter + phantoms
python3 scripts/lib/expert_priors.py --validate    # priors corroboration
python3 scripts/regression_eval.py                 # planted-defect regression eval (CI)
```

Install with `pip install -r requirements.txt` (pinned; add
`-r requirements-cloud.txt` for Azure Batch).

Live verification (kicks off a real run, ~5 hours, ~$25). Prefer
alternating the disease area between retros so gates are not tuned to
one domain:

```bash
source .venv/bin/activate
nohup python3 main.py "<research question>" \
  --max-rounds 8 --max-sessions 5 \
  > run_phaseN.log 2>&1 &
```

The run produces `runs/<timestamp>_<question>/` with all artifacts and
`final_gate.json` (the post-run validator decision).

## Pointers to deeper docs

- `README.md` — high-level project description (audience: human reader)
- `CHANGELOG.md` — phase ledger and gate internals
- `agents/modeler.py` SYSTEM_PROMPT — modeler contract (the longest
  prompt in the repo)
- `.claude/skills/README.md` — skill index
- `.claude/skills/<name>/SKILL.md` — domain or process knowledge
