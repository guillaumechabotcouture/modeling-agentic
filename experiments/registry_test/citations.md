# Test registry — demonstrates Commit A checks against the real malaria run code

## [C21] Protopopoff 2018 — PBO net unit cost (procurement + delivery)

## Parameter Registry

```yaml
parameters:
  - name: itn_standard_unit_cost
    value: 7.50
    ci_low: 5.00
    ci_high: 10.00
    kind: cost_usd
    source: C21
    subgroup: "per standard LLIN, procurement + delivery, 2024 USD Nigeria"
    applies_to: "Unit cost per standard LLIN"
    code_refs:
      - "runs/2026-04-23_1355_build-an-agent-based-model-of-malaria-tr/models/level3_interventions.py:182"
      - "runs/2026-04-23_1355_build-an-agent-based-model-of-malaria-tr/data/intervention_costs.csv"
    notes: |
      Registry holds the CSV mid value ($7.50). Code literal is $2.00. This
      SHOULD trigger registry_value_mismatch and cost_crosscheck_mismatch.

  - name: itn_pbo_unit_cost
    value: 10.00
    ci_low: 7.00
    ci_high: 12.00
    kind: cost_usd
    source: C21
    subgroup: "per PBO LLIN, procurement + delivery, 2024 USD Nigeria"
    applies_to: "Unit cost per PBO LLIN"
    code_refs:
      - "runs/2026-04-23_1355_build-an-agent-based-model-of-malaria-tr/models/level3_interventions.py:183"
      - "runs/2026-04-23_1355_build-an-agent-based-model-of-malaria-tr/data/intervention_costs.csv"

  - name: itn_dual_ai_unit_cost
    value: 12.50
    ci_low: 10.00
    ci_high: 15.00
    kind: cost_usd
    source: C21
    subgroup: "per dual-AI LLIN, 2024 USD Nigeria"
    applies_to: "Unit cost per dual-AI LLIN"
    code_refs:
      - "runs/2026-04-23_1355_build-an-agent-based-model-of-malaria-tr/models/level3_interventions.py:184"
      - "runs/2026-04-23_1355_build-an-agent-based-model-of-malaria-tr/data/intervention_costs.csv"

  - name: demo_or_for_conflation_test
    value: 0.35
    ci_low: 0.27
    ci_high: 0.44
    kind: odds_ratio
    source: C11
    subgroup: "high-transmission, IRS vs no IRS, children 6-59 months"
    applies_to: "IRS effect on infection odds; requires or_to_rr conversion"
    code_refs:
      - "runs/2026-04-23_1355_build-an-agent-based-model-of-malaria-tr/models/level3_interventions.py:185"
    notes: |
      Pointing at line 185 (the IRS cost line, 4.0 * 0.80) solely to
      demonstrate the or_rr_conflation detector fires when kind=odds_ratio
      is used without a nearby conversion. This is a test harness — not a
      claim that line 185 is actually doing OR→RR.
```
