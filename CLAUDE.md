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
│  52 SKILL.md files covering domain knowledge (malaria,         │
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

52 skills exist under `.claude/skills/`. Phase 10 commit φ resolved
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
| 15    | α, β, γ | A-priori identifiability arithmetic — first **PRE-MODEL** rigor gate. α: `models/identifiability_a_priori.yaml` (params/targets ratio, verdict IDENTIFIABLE/MARGINAL/OVER_SATURATED) required before MODEL stage spawns; OVER_SATURATED is **not scope-declarable** — architecture must be fixed at strategy time (inverts Phase 12-14's scope-declare-anything semantics for one class); new `pre-model-identifiability-arithmetic` skill teaches the 30-second arithmetic. β: STAGE 3 lead prompt rewritten with three branches (IDENTIFIABLE/MARGINAL/OVER_SATURATED); validator self-tests I1/I1b (round-gated missing-artifact MEDIUM/HIGH), I2 (OVER_SATURATED-without-resolution → HIGH), I3 (OVER_SATURATED-with-commitment → MEDIUM), I3b/I3c (MARGINAL with/without resolution → MEDIUM), I4 (IDENTIFIABLE silent), I5/I5b (AST-based structural enforcement plus behavioral check that scope_declaration cannot silence the HIGH), I6 (round_n=1/None silent for non-IDENTIFIABLE artifacts). γ: skill cross-references in `modeling-strategy`, `identifiability-analysis` (post-hoc as backstop), `mechanistic-vs-hybrid-architecture` (when to abandon HYBRID), `multi-structural-comparison` (pre-build complement) |
| 16    | α      | **Architecture, not a new gate.** Rigor-artifact manifest extracted to `.claude/orchestration/rigor_artifacts.yaml` as the single source of truth for artifact paths, draft/finalize rounds, scope-declarability, and skill cross-references. New `scripts/lib/rigor_artifacts.py` exposes `artifact_path()` / `produced_path()` / `render_timeline_markdown()`. `agents/modeler.py` SYSTEM_PROMPT renders the timeline table from the manifest at module load (sentinel substitution). `scripts/validate_critique_yaml.py` resolves all 11 rigor-artifact paths through the manifest instead of ~12 hardcoded `os.path.join(run_dir, "models", "<name>.yaml")` constants. Phase 16+ artifact additions are now a one-file YAML edit instead of ≥5 synchronized changes (manifest + modeler prompt + validator + skill + ledger). |
| 17    | γ, α, β, δ | **Adversarial restructure.** Targets the three error classes that survived ACCEPT in 2026-04-29_161312: (1) paraphrase/label drift across files, (2) self-contradicting artifacts after patches, (3) real-world plausibility (procurement, supply, vintage). γ: `.claude/orchestration/expert_priors.yaml` — 10-prior cross-run institutional memory (Nigeria GBD 12.8M DALYs, global PBO LLIN supply ceiling, GC7 disaggregation, MAP API auth, OR-to-RR Zhang-Yu, multiplicative-independence pitfall, etc.); new `scripts/lib/expert_priors.py` loader with question-keyword + decision-year matchers. α: new `agents/critique_premortem.py` agent + `pre-mortem-domain` skill — clean-slate adversarial critic spawned in PRE-MODEL window (STAGE 3, parallel to identifiability_a_priori). Reads only plan.md + hypotheses.md + matching priors; emits `pre_mortem.yaml` with HIGH-impact concerns categorized ARCHITECTURE/DATA/FEASIBILITY/BLIND_SPOT/EXPERT_PRIOR. Modeler must address each HIGH in `modeling_strategy.md § Pre-mortem Responses` (or scope-declare). 6 validator self-tests (PM1-PM6). β: new `scripts/coherence_audit.py` — three duties (label coherence, cross-file count/cost, self-contradicting artifacts) reusing `_scan_*` primitives from `numeric_consistency.py`. Runs alongside `writer_qa.py` post-WRITE. Validator integration via `_check_coherence_audit` (5 self-tests CA1-CA6) with Phase 10 ψ-style MEDIUM consolidation per duty. Retro on 2026-04-29_161312 fires 8 HIGH label_coherence (R-019), 1 HIGH cross_file_counts (R-005), 1 MEDIUM self_contradicting (M-010). δ: priors registry rides on **literature consensus**, not reviewer judgment. Schema upgrade adds `literature_corroboration: list[Citation]` (≥2 for MEDIUM, ≥3 for HIGH; primary/corroborating/supporting/contextual relevance) and `last_literature_check` per prior. New `auto_validation:` config block (per-severity min sources + max age). New `validate_priors()` (structural — no network) emits issues for insufficient_sources / stale / no_primary_source / unverified_doi; warnings auto-flow into pre-mortem agent spawn prompt via `validation_warnings_for_pre_mortem()`. New `retro_test(run_dir)` walks past critique markdowns and reports coverage of expert-prior-class blockers. New CLI `--validate`, `--match-yaml` (YAML with full corroboration for spawn injection), `--retro-test`, `--enrich-suggestions`. 10 priors re-corroborated with 2-3 cited sources each. Retro-test on 2026-04-29_161312: **87.5% coverage on expert-prior-class blockers** (caught R-007/R-009/R-010/R-011/R-012/R-014/R-018); on 2026-04-28_224202: 100% (D-008). |

## Conventions

- **Greek-letter commits**: each Phase ships ≤4 commits with Greek
  letters (α, β, γ … φ, χ, ψ, ω). The letter is reused as a
  reference in code comments and skills (e.g., "Phase 9 Commit τ").
- **Severity hierarchy**: HIGH blocks STAGE 7 ACCEPT; MEDIUM is a
  warning; LOW is informational. The `_check_*` functions return
  `[{"kind": str, "severity": "HIGH" | "MEDIUM", "claim": str, ...}]`.
- **Self-test discipline**: every script under `scripts/` (and
  `scripts/lib/`) has a `--self-test` flag that runs inline test
  cases. Run all of them before opening a PR (see verification block
  below for the current list).
- **Retro-checks against real runs**: when a Phase ships a new
  gate, the PR description includes a retro-check against the most
  recent `runs/` directory showing the gate produces the expected
  output. See PR #1 / #2 / #3 for examples.
- **Rigor-artifact timeline**: the source of truth is
  `.claude/orchestration/rigor_artifacts.yaml` (Phase 16 α).
  `agents/modeler.py` § 4 renders that manifest into a markdown table
  at module load; `scripts/validate_critique_yaml.py` resolves
  artifact paths via `lib.rigor_artifacts.artifact_path()`. To add a
  Phase N+1 artifact, edit the manifest and add the validator check;
  the prompt, ledger, and skill links will pull from the manifest.
  Phase 10 ψ's round-aware consolidation (1 `allocation_rigor_in_progress`
  MEDIUM in window vs 5 separate `*_missing`) still applies.

## How to verify changes end-to-end

Before opening a PR:

```bash
# All thirteen self-tests must report "OK: all self-test cases passed."
python3 scripts/coherence_audit.py --self-test             # Phase 17 β
python3 scripts/figure_validator.py --self-test
python3 scripts/identifiability.py --self-test
python3 scripts/identifiability_a_priori.py --self-test    # Phase 15 α
python3 scripts/lib/expert_priors.py --self-test           # Phase 17 γ
python3 scripts/lib/rigor_artifacts.py --self-test         # Phase 16 α
python3 scripts/numeric_consistency.py --self-test         # Phase 12 α
python3 scripts/sanity_checks.py --self-test               # Phase 13 α
python3 scripts/sensitivity_analysis.py --self-test
python3 scripts/spec_compliance.py --self-test
python3 scripts/validate_critique_yaml.py --self-test
python3 scripts/validate_skill_attachments.py --self-test
python3 scripts/within_zone_sensitivity.py --self-test     # Phase 12 γ
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
