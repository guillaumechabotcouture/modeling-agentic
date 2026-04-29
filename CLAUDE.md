# CLAUDE.md — Project guide for code agents

**Purpose**: this file orients a fresh agent (or contributor) before
they touch the codebase. Read this first; then jump to the layer
you're working on.

## What this project is

A multi-agent pipeline that produces publication-quality
mathematical modeling deliverables (calibration, allocation
optimization, decision rules, sensitivity analyses) for public-
health questions like "How should Nigeria allocate its $320M Global
Fund GC7 malaria budget across 774 LGAs?" The harness chains a
**lead** agent through eight pipeline stages (PLAN → DATA → MODEL
→ ANALYZE → CRITIQUE → GATE → WRITE), spawning specialist
sub-agents and running mechanical rigor checks between rounds.

## The four layers

```
┌────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION  main.py + agents/                              │
│  Lead loop, agent registry, prompt definitions, skill          │
│  attachments. Produces critique_*.yaml each round.             │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  RIGOR GATES  scripts/                                         │
│  validate_critique_yaml.py (~4100 lines, 30+ _check_* fns)     │
│  + standalone validators: identifiability.py,                  │
│  sensitivity_analysis.py, figure_validator.py,                 │
│  spec_compliance.py, etc. Each script has --self-test.         │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  SKILLS  .claude/skills/                                       │
│  48 SKILL.md files covering domain knowledge (malaria,         │
│  Starsim, LASER), rigor contracts (UQ, identifiability,        │
│  sensitivity), and process (modeling-strategy, threads).       │
│  Attached to agents in agents/__init__.py.                     │
└────────────┬───────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────────────────────────────────────────┐
│  RUNS  runs/<timestamp>_<question>/                            │
│  Per-question output: data/, models/, figures/, plan.md,       │
│  results.md, decision_rule.md, report.md, critique_*.yaml,     │
│  scope_declaration.yaml.                                       │
└────────────────────────────────────────────────────────────────┘
```

## Where to start as a new contributor

| Goal                                  | Read first                                                    |
|---------------------------------------|---------------------------------------------------------------|
| Understand the lead loop              | `main.py` + `agents/__init__.py` (lines 85-200, agent registry)|
| Add or modify a rigor gate            | `scripts/validate_critique_yaml.py` (look for `_check_*` fns) and any standalone validator under `scripts/` |
| Tweak the modeler agent's behavior    | `agents/modeler.py` SYSTEM_PROMPT (~970 lines, sections 4a-4h are rigor contracts) |
| Add a new skill                       | Create `.claude/skills/<name>/SKILL.md`, attach in `agents/__init__.py`, run `python3 scripts/validate_skill_attachments.py` |
| Trace a Phase X commit                | See "Phase commit ledger" below                               |
| Check what artifacts a run produces   | Look at `runs/2026-04-27_0013_*/` (most recent malaria run)   |

## Skill-to-agent attachment matrix

48 skills exist under `.claude/skills/`. Phase 10 commit φ resolved
the long-standing 54%-orphan rate; the remaining orphans are
domain-specific Starsim sub-skills not yet attached. Run
`python3 scripts/validate_skill_attachments.py --show-orphans` to
see the live picture.

| Agent (`agents/__init__.py`) | Skills attached (high level)                                          |
|------------------------------|-----------------------------------------------------------------------|
| **planner**                  | investigation-threads, malaria-modeling, vaccination, vectors, etc.   |
| **modeler**                  | modeling-strategy, laser-spatial-disease-modeling, parameter-estimation, **all 12 rigor skills** (identifiability, multi-structural, UQ, allocation-CV, decision-rule, optimizer-method, DALY, mech-vs-hybrid, sensitivity-remediation, ecological-fallacy-quantification, sanity-schema, pre-model-identifiability-arithmetic) — Phase 10 ω + Phase 12 γ + Phase 13 α + Phase 15 α |
| **analyst**                  | malaria-modeling, model-validation, surveillance, basic_epi_modeling  |
| **critique-domain**          | model-fitness, malaria-modeling, vectors, vaccination                 |
| **critique-redteam**         | adversarial-redteam, critique-blockers-schema, effect-size-priors     |
| **writer**                   | investigation-threads                                                 |

`asta-literature-search`, `pdf-text-extraction`, `semantic-scholar-lookup`
are MCP-provided — they have no local SKILL.md but are loaded by the
harness from external Asta servers. Allowed via `_MCP_SKILLS` in
`scripts/validate_skill_attachments.py`.

## Phase commit ledger

Every phase ships a Greek-letter-tagged commit. New rigor gates and
skill changes are listed below; bug fixes are not.

| Phase | Commit | Change                                                                                          |
|-------|--------|-------------------------------------------------------------------------------------------------|
| 2     | A, B   | Required artifacts: `outcome_fn.py`, `model_comparison.yaml`, `identifiability.yaml`            |
| 3     | C, D, E| Decision rule artifact (`decision_rule.md`); allocation CSV detection; spec-compliance checks   |
| 4     | α, β, γ, δ | UQ CI quality; surrogate UQ documented; decision-rule self-reference detection; cross-comparator efficiency |
| 5     | ε, ζ   | YAML-structured critique blockers; stuck-blocker escalation                                     |
| 6     | θ, ι, κ| Optimizer-quality benchmark; DALY-weighted analysis; allocation cross-validation                |
| 7     | λ, μ, ν| Writer-QA pass; plan-promised criteria; mechanistic-vs-hybrid skill + universal-coverage benchmark |
| 8     | ξ, ο, π| Multimodal figure-viewing audit (prompt-only); hybrid spec-compliance hardening; sensitivity-analysis required artifact |
| 9     | σ, ρ, τ| Identifiability false-positive fix (assert_loss_fn_is_pointwise); write-time figure validator + provenance hashes; rigor-artifact timeline |
| 10    | φ, χ, ψ, ω | Skills hygiene + dead-code sweep; validator robustness (non-crashing reads + 9 new self-tests + severity recalibration); allocation-gate coordinator (round-aware MEDIUM consolidation); docs + sensitivity-remediation skill |
| 11    | η, υ   | Terminal status discrimination (completed_with_report_restored); F5 run-dir collision prevention                                  |
| 12    | α, β, γ, δ | Cross-file numeric consistency (`scripts/numeric_consistency.py`); round-aware MEDIUM-to-HIGH escalation; ecological-fallacy required artifact + skill; `report.md` snapshot/restore |
| 13    | α, β   | Disease-agnostic sanity schema (`scripts/sanity_checks.py` + skill, 8 internal-only structural checks); α numeric_consistency extended to `decision_rule.md` and new token classes (LGA counts, package counts, budget shares) |
| 14    | α, β   | Universe-completeness sanity check (cross-checks schema's `allocation.units_total` vs allocation CSV row count); exact-match opt-in for integer counts (`exact_counts: [lga_count, package_count]` schema field), `report.md` added to count-drift scan list, `allocation.canonical_csv` field for multi-scenario runs, column-name tolerance |
| 15    | α, β, γ | A-priori identifiability arithmetic — first **PRE-MODEL** rigor gate. α: `models/identifiability_a_priori.yaml` (params/targets ratio, verdict IDENTIFIABLE/MARGINAL/OVER_SATURATED) required before MODEL stage spawns; OVER_SATURATED is **not scope-declarable** — architecture must be fixed at strategy time (inverts Phase 12-14's scope-declare-anything semantics for one class); new `pre-model-identifiability-arithmetic` skill teaches the 30-second arithmetic. β: STAGE 3 lead prompt rewritten with three branches (IDENTIFIABLE/MARGINAL/OVER_SATURATED); validator self-tests I1-I5 cover round gating, OVER_SATURATED-without-resolution, MARGINAL advisory, IDENTIFIABLE silent, and AST-based structural enforcement that the new check does NOT call the scope-declaration loader. γ: skill cross-references in `modeling-strategy`, `identifiability-analysis` (post-hoc as backstop), `mechanistic-vs-hybrid-architecture` (when to abandon HYBRID), `multi-structural-comparison` (pre-build complement) |

## Conventions

- **Greek-letter commits**: each Phase ships ≤4 commits with Greek
  letters (α, β, γ … φ, χ, ψ, ω). The letter is reused as a
  reference in code comments and skills (e.g., "Phase 9 Commit τ").
- **Severity hierarchy**: HIGH blocks STAGE 7 ACCEPT; MEDIUM is a
  warning; LOW is informational. The `_check_*` functions return
  `[{"kind": str, "severity": "HIGH" | "MEDIUM", "claim": str, ...}]`.
- **Self-test discipline**: every script under `scripts/` has a
  `--self-test` flag that runs inline test cases. Run all six
  before opening a PR (see verification below).
- **Retro-checks against real runs**: when a Phase ships a new
  gate, the PR description includes a retro-check against the most
  recent `runs/` directory showing the gate produces the expected
  output. See PR #1 / #2 / #3 for examples.
- **Rigor-artifact timeline**: see `agents/modeler.py` § 4 for which
  artifacts are due at which round. Phase 10 ψ added round-aware
  consolidation: missing artifacts within their drafting window
  produce 1 MEDIUM `allocation_rigor_in_progress`, not 5 separate
  `*_missing` MEDIUMs.

## How to verify changes end-to-end

Before opening a PR:

```bash
# All six self-tests must report "OK: all self-test cases passed."
python3 scripts/figure_validator.py --self-test
python3 scripts/identifiability.py --self-test
python3 scripts/sensitivity_analysis.py --self-test
python3 scripts/spec_compliance.py --self-test
python3 scripts/validate_critique_yaml.py --self-test
python3 scripts/validate_skill_attachments.py --self-test
```

Live verification (kicks off a real malaria run, ~5 hours, ~$25):

```bash
source .venv/bin/activate
nohup python3 main.py "Build an agent-based model of malaria \
  transmission across Nigeria's 774 LGAs..." \
  --max-rounds 8 --max-sessions 5 \
  > run_malaria_phaseN.log 2>&1 &
```

The run produces `runs/<timestamp>_<question>/` with all artifacts.

## Pointers to deeper docs

- `README.md` — high-level project description (audience: human reader)
- `agents/modeler.py` SYSTEM_PROMPT — modeler contract (the longest
  prompt in the repo; ~970 lines)
- `.claude/skills/<name>/SKILL.md` — domain or process knowledge,
  loaded by the agent harness when the skill matches the question
- `runs/2026-04-27_0013_*/` — most recent live malaria run; useful
  for understanding what artifacts a real run produces
