---
name: sensitivity-analysis-remediation
description: Phase 10 ω — interpretation and remediation of `models/sensitivity_analysis.yaml` verdicts (ROBUST / SENSITIVE / UNSTABLE). Phase 8 π made the artifact required and Phase 9 τ pushed it earlier in the pipeline. This skill teaches the modeler what each verdict means operationally and which escalation paths are available when verdict=UNSTABLE — the failure mode of the 0013 malaria run (RIG-003). Trigger phrases include "sensitivity UNSTABLE", "sensitivity SENSITIVE", "RIG-003", "perturbation flips", "primary_recommendation_changes", "narrow CI", "scope-declare sensitivity".
type: rigor-remediation
---

# Sensitivity-Analysis Remediation

## What this skill is for

You ran the optimizer, drafted `models/sensitivity_analysis.yaml`,
and the validator's `_check_sensitivity_analysis` returned a verdict
that's not ROBUST. This skill walks you through what each verdict
means and the decision tree for fixing it. It complements the gate
artifact at `scripts/sensitivity_analysis.py` and the Phase 9 τ
two-phase contract in `agents/modeler.py` § 4g.

## What the three verdicts mean

The validator computes the verdict from the perturbation outcomes
mechanically. The thresholds (in `scripts/sensitivity_analysis.py`):

| Verdict   | Threshold                                                  | What it implies                                                                  |
|-----------|------------------------------------------------------------|----------------------------------------------------------------------------------|
| ROBUST    | 0 perturbations flip primary recommendation AND worst rank-change-top-N ≤ 10 | Recommendation survives parameter uncertainty within published CI ranges. ACCEPT-grade. |
| SENSITIVE | ≤ 1 perturbation flips OR worst rank-change-top-N ≤ 30     | Real but bounded fragility. Surface in §Sensitivity (NOT §Limitations). MEDIUM blocker. |
| UNSTABLE  | ≥ 2 perturbations flip OR worst rank-change-top-N > 30     | Recommendation cannot be defended as-is. HIGH blocker.                            |

The 0013 run produced UNSTABLE: 1 of 8 perturbations flipped (PBO OR
upper CI 0.81 → standard ITN reverts to dominance, 54 of top-50 LGAs
re-ranked, DALYs averted shifted 0.476M → 1.155M).

## Decision tree when verdict ≠ ROBUST

### 1. First, verify the verdict isn't a code artifact

Before treating UNSTABLE as a real modeling problem, rule out the
mechanical false-positive:

- **Is your `outcome_fn(params)` deterministic?** Re-run it with the
  primary parameters; the result must match `primary_objective` in
  the yaml. If it doesn't, the optimizer is reading stale state
  (cached results, dataset drift mid-run). Fix that first.
- **Are your perturbation values realistic?** Don't perturb to
  literature-implausible values just to "find" sensitivity. The
  perturbation must be a 95% CI endpoint or a defensibly-cited
  alternative estimate. If a perturbation comes from your own
  guess, drop it.
- **Are you computing rank_change_top_n correctly?** It's the count
  of top-50 LGAs (or top-N for the chosen N) whose package
  assignment differs between the primary and perturbed allocation —
  not the absolute rank shift. Re-check the calculation.

If all three pass, the UNSTABLE verdict is real. Continue.

### 2. Choose an escalation path

Three options, in order of effort and how much they change the
recommendation:

#### Path A — Narrow the parameter range with stronger evidence (best)

If a perturbation flips on a CI endpoint that's barely defensible
(e.g., a 25-month-extrapolated estimate from a 9-month RCT), find
literature with a tighter range and update the registry.

Concrete pattern:
- The 0013 PBO OR perturbation used Protopopoff 2018 lifecycle
  (0.55) as primary, with 0.40 (9-month) and 0.81 (25-month) as
  bounds. The 0.81 endpoint is a long-extrapolation under
  resistance pressure.
- A subsequent meta-analysis (e.g., a Cochrane update) might
  constrain the upper bound to 0.65. Re-run the perturbation at
  0.65 instead of 0.81; verdict may become ROBUST.

This path is the "right answer" when available. **Cite the new
evidence in `citations.md` § Parameter Registry.**

#### Path B — Reformulate the optimizer objective (medium)

If the recommendation flips because the objective function is too
sensitive to one parameter (PBO OR drives the LLIN-vs-PBO choice in
the 0013 run), consider:

- **Two-stage objective**: first maximize expected DALYs averted,
  then prefer recommendations that are robust under the parameter's
  CI. Implement as a small penalty on `rank_change_top_n` in the
  optimizer.
- **Worst-case (minimax) objective**: maximize DALYs averted at the
  WORST CI endpoint. Conservative; may flatten the recommendation
  (everyone gets standard ITN), which the program officer can
  accept as a defensible default.
- **Robust portfolio**: split budget between two packages that win
  at different ends of the parameter range.

Document the reformulation in `decision_rule.md` and `report.md`.
The validator does not gate this — your report must defend it.

#### Path C — Scope-declare and ship (last resort)

When the parameter is genuinely uncertain and no stronger evidence
exists, scope-declare in §Limitations:

```
The recommendation is sensitive to the PBO OR (Protopopoff 2018,
9-month vs 25-month). At the 25-month endpoint (OR=0.81), standard
ITN dominates PBO in 54 of the top-50 LGAs. We recommend
preferring standard ITN where program-officer judgment expects
real-world durability closer to the 9-month figure, and PBO where
durability assumptions match the 25-month endpoint.
```

This path means the report ships with verdict=UNSTABLE in the
yaml but a §Limitations entry that gives the program officer the
information they need to choose. The validator still emits HIGH
`sensitivity_analysis_unstable`, which the lead may scope-declare
at STAGE 7 (DECLARE_SCOPE outcome).

**Do NOT bury this in §Limitations. The §Sensitivity section of
`report.md` must call out the parameter, the alternative value, and
the operational consequence — exactly the language above.**

## What the gate emits (reference)

Verbatim from `scripts/validate_critique_yaml.py`:

- `sensitivity_analysis_unstable` HIGH on UNSTABLE verdict
- `sensitivity_analysis_sensitive` MEDIUM on SENSITIVE verdict
- `sensitivity_analysis_malformed` HIGH on schema violations or
  reported-vs-computed verdict mismatch
- `sensitivity_analysis_missing` MEDIUM (now consolidated into
  `allocation_rigor_in_progress` / `_drafts_overdue` per Phase 10 ψ)

## Checklist before declaring scope

- [ ] Verified `outcome_fn` is deterministic at primary values
- [ ] Verified each perturbation value is from published evidence
- [ ] Verified `rank_change_top_n` calculation
- [ ] Investigated Path A (tighter literature) and confirmed no better evidence exists
- [ ] Investigated Path B (objective reformulation) and either implemented or rejected with justification in report.md
- [ ] §Sensitivity section of report.md states the load-bearing parameter, the flip threshold, and the operational consequence in plain language

## Related skills and artifacts

- `effect-size-priors` — the Parameter Registry the perturbation values must reference
- `mechanistic-vs-hybrid-architecture` — Phase 7 ν framing decision; the right architecture reduces sensitivity surface area
- `daly-weighted-analysis` — DALY framing makes the operational consequence of a flip visible
- `scripts/sensitivity_analysis.py` — gate validator with self-tests covering ROBUST / SENSITIVE / UNSTABLE / MALFORMED
- `agents/modeler.py` § 4g — Phase 9 τ two-phase drafting contract (draft r2-3, finalize r6-7)
