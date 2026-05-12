---
name: evidence-sufficiency
description: Adversarial check on claim-vs-evidence adequacy. Use when the writer has produced report.md and the claims ledger, benchmark_match, effort_floors_report are in place. Determines whether each high-stakes claim is supported by the evidence base or is overclaimed relative to model fidelity and data quantity.
---

# Evidence sufficiency (Phase 19 δ)

## What this is for

The four existing critics review correctness in scope:
- `critique-methods` — statistical validity of the analysis
- `critique-domain` — scientific plausibility of the claims
- `critique-presentation` — figure and prose quality
- `critique-redteam` — cross-file consistency and adversarial holes

None of them asks the question: **given the strength of the claim, is
the evidence base sufficient?** A model can fit, the methods can be
clean, the domain can be plausible, the prose can be consistent —
and the report can still overclaim relative to what 100 UQ draws, 2
restarts, and zero held-out folds actually demonstrate.

The sufficiency critic reads:

- `report.md` — the claims being made, with their strength markers
- `models/claims_ledger.yaml` — every quantitative/categorical claim
- `benchmark_match.yaml` — whether the model matches published reality
- `effort_floors_report.yaml` — whether the underlying work was done
  with enough effort
- `models/calibration_result.yaml` (if present) — held-out, restarts
- `uncertainty_report.yaml` — n_draws, CI quality

And emits per-claim verdicts:

- **OVERCLAIMED** — claim strength exceeds evidence base. Examples:
  point estimate with no CI; causal language ("X reduces Y by Z%")
  when only association was tested; precision implied (28%, 95% CI
  22-34) when underlying calibration used 50 UQ draws and 1 restart.
- **ADEQUATE** — claim strength matches evidence.
- **UNDERCLAIMED** — the model supports a stronger claim than the
  prose makes (rare; the failure mode is the opposite direction).

## How to invoke

The critique-sufficiency agent runs POST-WRITE, parallel to writer-QA
and the coherence audit. It is the only critic that explicitly reads
the rigor-effort and benchmark-match artifacts.

```
This is critique round N. Research question: ...
Run directory: runs/<run_name>.
Read report.md, models/claims_ledger.yaml, benchmark_match.yaml,
effort_floors_report.yaml, models/calibration_result.yaml (if present),
and uncertainty_report.yaml.

Produce critique_sufficiency.yaml per the evidence-sufficiency skill.
```

## Output schema

```yaml
generated_at: <ISO 8601>
agent: critique-sufficiency
verdict: OVERCLAIMED | ADEQUATE | UNDERCLAIMED
n_overclaimed: <int>
n_adequate: <int>
n_underclaimed: <int>

claim_verdicts:
  - claim_id: c042
    current_claim: "Allocation A reduces deaths by 28% (95% CI 22-34)"
    verdict: OVERCLAIMED
    reason: |
      The 95% CI implies posterior-derived uncertainty. The underlying
      UQ used n_draws=80 (below the local floor of 200), and
      calibration was a single-restart Nelder-Mead with no held-out
      fold. The CI is bootstrap noise, not posterior coverage.
    evidence_pointers:
      - "effort_floors_report.yaml: uq_min_draws_local violated (50 < 200)"
      - "models/calibration_result.yaml: n_restarts=1"
      - "models/calibration_result.yaml: held_out_fold is null"
    suggested_restatement: |
      "Allocation A reduces deaths by ~28% in our calibrated model
      (model-internal range 22-34 across UQ draws); held-out
      validation absent, treat as exploratory."

  - claim_id: c071
    current_claim: "Cost-effectiveness $5,300 per DALY averted"
    verdict: ADEQUATE
    reason: |
      Cost figures bound by procurement_priors registered in
      citations.md; DALY weights from GBD 2021. Sensitivity_analysis
      shows ±20% perturbations keep the point estimate within the
      published $4,000-$8,000 range.

  - ...

# Aggregate gate signals the validator reads:
gate_signals:
  blocks_accept:        true | false       # any OVERCLAIMED → true
  scope_declarable:     true               # critic verdicts are heuristics
```

## Decision rules

- **Always OVERCLAIMED**: a point-estimate causal claim
  ("X reduces Y by Z%") when calibration has n_restarts < 3 OR
  held_out_fold is missing.
- **Likely OVERCLAIMED**: a 95% CI on a model output when UQ
  n_draws < 200 (local) or n_draws < 1000 (cloud).
- **Likely OVERCLAIMED**: a benchmark-anchored claim
  ("our model agrees with WMR") when `benchmark_match.yaml` shows
  the relevant target is in DRIFT or missing_computed.
- **Likely OVERCLAIMED**: shortcut markers from effort_floors_report
  hit the relevant model_files (e.g., n_replicates=1 in the file
  producing the claim).

## What to skip

- Do not flag prose stylistic choices ("clearly", "we believe") as
  overclaiming. That's critique-presentation's territory.
- Do not flag claims whose source_artifact is outside the run dir
  (e.g., parameters quoted from citations.md). Those are domain
  facts, not model outputs.
- Do not propose alternative quantitative values. Your job is to
  signal mismatch and suggest qualitative restatement; the modeler /
  writer make the numerical decision.

## Severity to validator integration

The critique YAML is read by `_check_sufficiency_critic` in
`scripts/validate_critique_yaml.py`. Each OVERCLAIMED verdict becomes
one HIGH `claim_overclaimed` violation referencing the specific
claim_id. The HIGH is scope-declarable because the critic operates
on heuristics, not arithmetic — the modeler may push back with a
literature-grounded justification.
