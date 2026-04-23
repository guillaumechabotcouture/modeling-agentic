---
name: critique-blockers-schema
description: Structured YAML contract for critique agents. Each critique writes a
  machine-readable sibling YAML alongside its markdown file, listing blockers with
  stable IDs, severity, target stage, and resolution state. The lead agent's
  STAGE 7 ACCEPT/DECLARE_SCOPE/RETHINK decision is computed mechanically from
  these YAML files via scripts/validate_critique_yaml.py — NOT judged from the
  prose. Use when writing critique_methods.yaml, critique_domain.yaml, or
  critique_presentation.yaml, or when the lead is deciding whether the writer
  may be spawned. Trigger phrases include "blocker", "critique yaml",
  "structural mismatch", "unresolved high", "carried forward", "stage 7 gate".
---

# Critique Blockers Schema

## Why this exists

Free-form prose critiques let the lead drift into premature ACCEPT. The YAML
sibling lets the gate be mechanical: if any HIGH blocker is unresolved or any
critique flags `structural_mismatch`, the writer cannot be spawned.

Three files, one per critique agent:

- `{run_dir}/critique_methods.yaml`       — ID prefix **M-**
- `{run_dir}/critique_domain.yaml`        — ID prefix **D-**
- `{run_dir}/critique_presentation.yaml`  — ID prefix **P-**

Each YAML is a sibling to the existing markdown file. Keep the markdown as
today — prose is still where reviewers do their reasoning. The YAML is the
contract.

## Schema

```yaml
reviewer: critique-methods       # critique-methods | critique-domain | critique-presentation
round: 2                         # 1-indexed; matches current critique round
verdict: REVISE                  # PASS | REVISE

# Single-agent architectural veto. Only critique-methods and critique-domain
# may set detected=true. critique-presentation MUST set detected=false.
structural_mismatch:
  detected: false
  # When detected=true, the following are REQUIRED:
  # description: "Question requires ABM with Starsim; delivered model is a
  #   deterministic ODE in pure numpy. No agent-based dynamics, no Starsim
  #   framework usage."
  # evidence_files:
  #   - "model/core.py"       # what you read
  #   - "plan.md:§3.2"        # where question's requirement is stated
  # fix_requires: RETHINK     # always RETHINK; structural mismatch is not
  #                           # patchable and is NOT a scope-declarable
  #                           # limitation.

blockers:
  - id: M-001                    # {prefix}-NNN, zero-padded; see ID rules below
    severity: HIGH               # HIGH | MEDIUM | LOW
    category: HARD_BLOCKER       # see category list below
    target_stage: MODEL          # PLAN | DATA | MODEL | ANALYZE | WRITE
    first_seen_round: 1
    claim: >
      Primary model performs worse than baseline
      (skill score = -0.14 on held-out test set).
    evidence: "model_comparison.md §Baseline comparison; figures/skill_bar.png"
    fix_requires: >
      Either structural change (add seasonality component or switch to
      state-space formulation) OR explicit scope declaration that this
      model is not skilled vs baseline and results should be read as
      exploratory only.
    resolved: false
    # When resolved=true, the following are REQUIRED:
    # resolved_in_round: 2
    # resolved_evidence: "model_comparison.md now shows skill_score=0.31;
    #   figures/skill_bar.png regenerated 2026-04-22."

# Blockers from prior rounds that this critique re-checked.
# REQUIRED on round >= 2. Empty list on round 1.
carried_forward:
  - id: M-003
    prior_round: 1
    still_present: true
    # When still_present=true:
    notes: >
      Greedy optimizer still used; round-2 patch added a budget cap but
      did not address the local-optimum concern flagged in round 1.
    # When still_present=false, notes explain what resolved it and
    # resolved_evidence is REQUIRED on the matching entry in the
    # `blockers` list above (where resolved: true).
```

## Severity criteria

Severity is anchored in your critique's existing checklists, not free
judgment. The goal is that **HIGH** means "the writer genuinely cannot
ship over this," not "I'd prefer it fixed."

**HIGH** — any of:
- An item from your agent's "Hard Blockers" or "automatic REVISE" section
  (if your prompt has one).
- A HIGH-severity parameter/citation issue (value mismatch, wrong subgroup,
  author mismatch, combined-budget-treated-as-disease-specific).
- A missing deliverable explicitly required by the research question
  (e.g., the question names a specific output and it's absent).
- A primary quantitative claim in results.md that's unsupported by the
  evidence in the run directory.

**MEDIUM** — quality issues that degrade the report but do not invalidate
the core conclusions. Most presentation issues (missing captions,
low-quality figures) are MEDIUM unless they break a required deliverable.

**LOW** — polish, style, redundancy, non-blocking suggestions.

If you're unsure between HIGH and MEDIUM, the tiebreaker is:
*would a peer reviewer at Lancet / PLOS Med reject the manuscript for
this specific issue?* HIGH if yes, MEDIUM if they'd request a revision.

## Category values

- `HARD_BLOCKER`   — convergence failure, negative skill score, missing
                     confidence intervals on primary claims, etc.
- `METHODS`        — statistical validity, model specification, validation
                     methodology.
- `CAUSAL`         — causal reasoning, causal-vs-associational labeling,
                     confounding.
- `HYPOTHESES`     — hypothesis framing, falsifiability, verdict support.
- `CITATIONS`      — parameter provenance, author/value verification,
                     budget/funding claim verification.
- `PRESENTATION`   — figures, tables, captions, writing quality.
- `DATA`           — data availability, provenance, validation data.
- `STRUCTURAL`     — model architecture does not match the question. (If
                     you use this, you should also consider setting
                     `structural_mismatch.detected=true` at the top of
                     the YAML. Use `STRUCTURAL` blockers for component-level
                     structure issues; use `structural_mismatch` for the
                     architectural class of the whole model.)

## Target stage values

- `PLAN`      — planner must revise plan.md, threads.yaml, citations.md
- `DATA`      — data-agent must download/validate/re-examine data
- `MODEL`     — modeler must change code, specification, or outputs
- `ANALYZE`   — analyst must re-interpret, re-label verdicts, or add
                missing comparisons
- `WRITE`     — writer (at STAGE 8) must address during report assembly

Most blockers target MODEL; but **don't default to MODEL** — if the root
cause is data or hypothesis framing, sending to MODEL just makes the
modeler work around the gap instead of fixing it.

## ID assignment rules

1. **Prefix** is always your critique's single letter:
   - critique-methods       → `M-`
   - critique-domain        → `D-`
   - critique-presentation  → `P-`

2. **First round** (`round: 1`): assign IDs in write order, starting at
   `M-001`, `D-001`, `P-001`. Three-digit zero-padded. Do not re-use IDs
   even if a blocker was later merged.

3. **Later rounds** (`round: >= 2`): you MUST read the prior round's YAML
   (`{run_dir}/critique_{name}.yaml`) before writing. For each HIGH or
   MEDIUM blocker in the prior round:

   - If the issue still exists: reuse the SAME `id` in your new `blockers`
     list; set `first_seen_round` to the prior round's `first_seen_round`
     (not the current round); set `resolved: false`.
   - If the issue is now resolved: do NOT include it in `blockers`;
     instead, include a `blockers` entry with the SAME `id`,
     `resolved: true`, `resolved_in_round: <current_round>`,
     `resolved_evidence: "..."`. Also include it in `carried_forward`
     with `still_present: false`.

4. **New issues found in later rounds**: assign the next sequential ID
   after the highest prior-round ID. E.g., if round 1 issued M-001 through
   M-005 and round 2 finds a new one, it's `M-006` (not `M-001` again).

5. **LOW-severity items** do not need to be carried forward — they're
   polish, and the gate doesn't depend on them.

## carried_forward rules

- Round 1: `carried_forward: []`
- Round ≥ 2: MUST contain one entry per HIGH or MEDIUM blocker from the
  immediately prior round. If the prior round had 8 HIGH/MEDIUM blockers,
  you have 8 `carried_forward` entries.
- `still_present: true` → the matching entry must also appear in your
  current `blockers` list (with `resolved: false`, same id).
- `still_present: false` → the matching entry must also appear in your
  current `blockers` list with `resolved: true` and `resolved_evidence`.

This redundancy is deliberate: it forces you to actually check each prior
blocker rather than silently dropping it.

## structural_mismatch criteria

Only critique-methods and critique-domain can set
`structural_mismatch.detected: true`. Criteria:

**critique-domain** sets it when:
- The delivered model answers a SIMPLER question than the one asked.
  (See `model-fitness` skill's "simpler question test.")
- The audience would REJECT the model structure outright (e.g., question
  asks for sub-regional comparison, model is national-only).
- Required mechanism is absent (e.g., "compare age-targeted interventions"
  → model has no age structure).

**critique-methods** sets it when:
- Model CLASS is inappropriate for the data and question (e.g., question
  requires stochastic dynamics, model is deterministic ODE; question
  requires time-series, model is cross-sectional regression).
- Question explicitly names a modeling framework (e.g., "ABM", "Starsim",
  "compartmental SEIR") and the delivered model does not use it.

**critique-presentation** MUST set `detected: false`. Presentation does
not have architectural veto power. Presentation issues that rise to
"blocking" are HIGH blockers, not structural mismatches.

If you set `structural_mismatch.detected: true`, you MUST also populate
`description`, `evidence_files`, and `fix_requires: RETHINK`. A structural
mismatch is NEVER scope-declarable — the delivered model does not answer
the question and the run fails if rounds are exhausted without RETHINK
resolving it.

## Gate rules applied by the lead (for your awareness)

The lead runs `scripts/validate_critique_yaml.py {run_dir}` after each
critique round. It computes:

```
unresolved_high   = [b for c in critiques for b in c.blockers
                     if b.severity == "HIGH" and not b.resolved]
structural       = any(c.structural_mismatch.detected for c in critiques)
rounds_remaining = max_rounds - current_round
```

Then:

| Condition                                          | Action                                            |
|----------------------------------------------------|---------------------------------------------------|
| `structural` true                                  | RETHINK. If no rounds left → run FAILS, no writer.|
| `unresolved_high` non-empty, rounds remaining      | PATCH or RETHINK. ACCEPT is forbidden.            |
| `unresolved_high` non-empty, no rounds remaining   | DECLARE_SCOPE with per-blocker acknowledgment.    |
| `unresolved_high` empty                            | ACCEPT. Proceed to STAGE 8.                       |

Your YAML is the input to this decision. If you mark things HIGH that
aren't truly blocking, you waste rounds. If you mark blocking things
MEDIUM to help the run finish, you ship broken work. Be calibrated.

## Minimal round-1 example

```yaml
reviewer: critique-presentation
round: 1
verdict: REVISE

structural_mismatch:
  detected: false

blockers:
  - id: P-001
    severity: HIGH
    category: PRESENTATION
    target_stage: MODEL
    first_seen_round: 1
    claim: "Figures 1–7 have no numbered captions; results.md references
            them only by filename."
    evidence: "results.md §Results; figures/ directory listing"
    fix_requires: "Add numbered captions in results.md for every embedded
                   figure; caption must explain what the figure shows AND
                   the takeaway."
    resolved: false
  - id: P-002
    severity: MEDIUM
    category: PRESENTATION
    target_stage: WRITE
    first_seen_round: 1
    claim: "Discussion section is a bullet list, not prose."
    evidence: "results.md §Discussion"
    fix_requires: "Rewrite as 3–4 paragraphs during report assembly."
    resolved: false

carried_forward: []
```

## Minimal round-2 example (same critique)

```yaml
reviewer: critique-presentation
round: 2
verdict: REVISE

structural_mismatch:
  detected: false

blockers:
  - id: P-001
    severity: HIGH
    category: PRESENTATION
    target_stage: MODEL
    first_seen_round: 1
    claim: "Figures 1–7 have no numbered captions (STILL PRESENT after round 1)."
    evidence: "results.md §Results unchanged; no captions added"
    fix_requires: "Add numbered captions in results.md for every embedded figure."
    resolved: false
  - id: P-002
    severity: MEDIUM
    category: PRESENTATION
    target_stage: WRITE
    first_seen_round: 1
    claim: "Discussion section now prose, caption issue fixed here."
    evidence: "results.md §Discussion, 4 paragraphs"
    fix_requires: "n/a"
    resolved: true
    resolved_in_round: 2
    resolved_evidence: "results.md §Discussion rewritten as 4 paragraphs."
  - id: P-003
    severity: HIGH
    category: PRESENTATION
    target_stage: MODEL
    first_seen_round: 2
    claim: "New figure added in round 2 (fig_sensitivity.png) has axis
            labels cut off at 300dpi rendering."
    evidence: "figures/fig_sensitivity.png"
    fix_requires: "Re-render with tight_layout() or increase figure size."
    resolved: false

carried_forward:
  - id: P-001
    prior_round: 1
    still_present: true
    notes: "No captions added between rounds; modeler focused on
            compartment changes per the PATCH instructions."
  - id: P-002
    prior_round: 1
    still_present: false
    notes: "Discussion rewritten to prose in round-2 writer draft."
```

Note in the round-2 example:
- `P-001` carried forward same ID, same `first_seen_round: 1`.
- `P-002` resolved; appears in both `blockers` (with `resolved: true`)
  and `carried_forward` (`still_present: false`).
- `P-003` is new in round 2 → next available ID.
- `carried_forward` has entries for both HIGH and MEDIUM prior blockers.
