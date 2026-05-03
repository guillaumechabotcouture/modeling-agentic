---
name: pre-mortem-domain
description: Phase 17 α — required PRE-MODEL artifact `pre_mortem.yaml`, produced by the critique-premortem agent on a clean slate (only plan.md, hypotheses.md, success_criteria.yaml, the question, and matching expert priors). Catches HIGH-impact concerns — feasibility, supply-chain, vintage, blind spots — that would be expensive to fix once MODEL has built. The modeler must address each HIGH in `modeling_strategy.md § Pre-mortem Responses` before STAGE 7 ACCEPT. Trigger phrases include "pre-mortem", "what would go wrong", "domain blind spot", "feasibility", "procurement constraint", "expert prior".
type: rigor-remediation
---

# Pre-Mortem Domain Critique

## What this skill is for

The detection ratchet (Phases 1-16) caught failure modes inside a
finished run. But experts and external reviewers keep raising the
same domain concerns across runs — Nigeria's GBD 2021 DALY
denominator, global PBO LLIN supply ceiling, MAP API
authentication, multiplicative PBO+IRS independence, etc. Each was
re-discovered per run.

Phase 17 α inserts a **pre-mortem domain critic** (the
`critique-premortem` agent) into the PRE-MODEL window — alongside
the Phase 15 α a-priori identifiability check. It runs on a clean
slate (no run-dir contamination beyond plan.md / hypotheses.md /
success_criteria.yaml), uses Opus 4.7's adversarial-roleplay
strength most effectively (no compute-time constraint), and emits
structured concerns BEFORE the modeler builds.

## When the gate fires

`_check_premortem_addressed` in `scripts/validate_critique_yaml.py`
triggers as follows:

- MEDIUM `pre_mortem_missing` at round 1 if `pre_mortem.yaml` is
  not produced (drafting window).
- HIGH `pre_mortem_missing` at round ≥ 2 if still absent.
- MEDIUM `pre_mortem_high_unaddressed` at round 1 for each HIGH
  concern with `addressed_in: null` (drafting window).
- HIGH `pre_mortem_high_unaddressed` at round ≥ 2 for each HIGH
  concern with `addressed_in: null` (blocks ACCEPT, but is
  **scope-declarable** — pre-mortem concerns are domain heuristics,
  not arithmetic facts; the modeler may scope-declare with
  justification in `scope_declaration.yaml`).

## The artifact schema

```yaml
generated_at: 2026-05-03T10:00:00Z
agent: critique-premortem
question_first_60_chars: "Build an agent-based model of malaria transmission"
matching_prior_ids:
  - nigeria_malaria_total_burden_dalys
  - pbo_llin_global_supply_ceiling
  - gc7_malaria_budget_disaggregation
  # ... (matched against expert_priors.yaml's applies_when)

concerns:
  - id: PM-001
    severity: HIGH
    category: FEASIBILITY     # ARCHITECTURE | DATA | FEASIBILITY | BLIND_SPOT | EXPERT_PRIOR
    concern: |
      Plan recommends universal PBO LLIN at 80% coverage for ~205M
      population without procurement feasibility section.
    what_could_go_wrong: |
      ~30M PBO nets/yr required for Nigeria alone. Global LLIN
      procurement is ~30M/yr (UNICEF 2022); PBO share is < 50%.
      Recommendation as written would consume the entire global PBO
      LLIN supply for one country. Reviewers (Global Fund, PMI) will
      reject without a supply-chain caveat or phased roll-out.
    suggested_check: |
      Add a §Procurement Feasibility section to results.md citing
      UNICEF Supply Annual Report and discuss either (a) phased
      coverage, (b) mixed PBO + dual-AI portfolio, or (c) an
      explicit scope-declaration of the supply constraint.
    evidence_files:
      - "plan.md:§Allocation"
      - "expert_prior:pbo_llin_global_supply_ceiling"
    addressed_in: null

  # ... up to 8 concerns total

n_high: 2
n_medium: 4
n_low: 1
```

## How the modeler responds

The modeler must add a `## Pre-mortem Responses` section to
`modeling_strategy.md` containing one subsection per HIGH (and
preferably each MEDIUM). For each concern:

1. Quote the `concern` text (one line).
2. State the response: ADDRESSED, SCOPE_DECLARED, or DEFERRED.
3. If ADDRESSED, point to the specific plan / model / analysis
   change that resolves it.
4. If SCOPE_DECLARED, write the justification and add an entry to
   `scope_declaration.yaml`.
5. Update `pre_mortem.yaml`'s `addressed_in` field to point to the
   `modeling_strategy.md` section (e.g.,
   `"modeling_strategy.md#pre-mortem-responses-pm-001"`).

The validator does not parse the prose — it only checks that
`addressed_in` is non-null. Prose quality is the
`critique-domain` agent's territory at later rounds.

## The scope-declarable rule

Unlike Phase 15 α's a-priori identifiability check (which is
arithmetic and non-negotiable), pre-mortem concerns are domain
heuristics. The modeler may scope-declare a HIGH with justification
— e.g., "the procurement constraint is real but the question
explicitly assumes idealized supply; document as a Limitation."

This is permitted because the critic is identifying judgment calls,
not facts. The check is a forcing function: the modeler must
explicitly think about each HIGH and explicitly defend skipping it.

## Literature corroboration (Phase 17 δ)

Every entry in `expert_priors.yaml` carries
`literature_corroboration: list[Citation]` — ≥2 sources for MEDIUM
priors, ≥3 for HIGH. Each citation has `source_type` (paper / report
/ dataset / tool / meta_analysis), `relevance` (primary / corroborating
/ supporting / contextual), `year`, optional `doi_or_pmid`, and an
optional supporting `excerpt`. The auto-validator
(`scripts/lib/expert_priors.py::validate_priors`) enforces these
counts on every load and warns when:

- A HIGH/MEDIUM prior has fewer than the required source count
  (`insufficient_sources`)
- `last_literature_check` is older than the per-severity max age
  (`stale`: HIGH every 365 days, MEDIUM every 730, LOW every 1825)
- No source is marked `relevance: primary` (`no_primary_source`)
- Paper/meta-analysis citations lack a DOI or PMID
  (`unverified_doi` — advisory only)

Warnings flow into the pre-mortem agent's spawn prompt via
`validation_warnings_for_pre_mortem()`. They do NOT block operation.

## Retro-test smoke check

`scripts/lib/expert_priors.py --retro-test <run_dir>` walks the past
run's `critique_redteam.md` / `critique_domain.md` / `critique_methods.md`,
extracts blocker IDs, and reports which would have been caught
pre-MODEL by the current registry. The 161312 retro-test confirms
87.5% coverage on expert-prior-class blockers (R-007 procurement,
R-009 MAP API, R-010 IRS cost, R-011 delivery, R-012 multiplicative,
R-014 OR derivation, R-018 GBD attribution).

## Sources

- `agents/critique_premortem.py` — agent system prompt (Phase 17 α)
- `.claude/orchestration/expert_priors.yaml` — registry of
  cross-run institutional concerns (Phase 17 γ + δ schema)
- `scripts/lib/expert_priors.py` — loader, matcher, validator,
  retro-tester
- `scripts/validate_critique_yaml.py::_check_premortem_addressed`
  — round-aware severity gate
