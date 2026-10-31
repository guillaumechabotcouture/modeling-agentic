---
name: model-fitness
description: >-
  Framework for judging whether a model is fit for its stated purpose and
  audience: audience-specific requirements, structural gap detection, the
  simpler-question test, and complexity escalation criteria. Use when deciding
  whether to accept a model, escalate complexity, or declare scope. Trigger
  phrases include "fit for purpose", "is this model good enough", "should we
  accept", "structural gap", "escalate complexity", "does this answer the
  question".
---

# Model Fitness Evaluation

## 1. The Core Question

> "Does this model have the right structure to answer the research question
> credibly for the intended audience?"

This is NOT about whether the model runs correctly, has good metrics, or
passes validation checks. A model can be perfectly implemented, well-
calibrated, and honestly documented — and still be unfit for its purpose
because its structure can't capture what matters for the decision.

Fitness is about **structure matching purpose**, not implementation quality.

---

## 2. Audience Requirements

Different audiences have different structural expectations. A model that
is fit for one audience may be disqualifying for another.

### Global Fund / GAVI / Donor Agencies

**They decide**: How much money to allocate, to which interventions, in
which geographies.

**They require**:
- Cost per DALY averted (not just cost per case) — needs mortality module
  with age-weighted case fatality rates
- Geographic targeting at the level they fund (often subnational admin-1
  or admin-2, not national or continental)
- Comparison of ALL interventions in the funding request, including
  age-targeted ones (requires age structure if any intervention is
  age-targeted)
- Comparison to the country's own national strategic plan
- Benchmark against established models (EMOD, malariasimulation,
  OpenMalaria, Optima, etc.) used in prior submissions

**Disqualifying gaps**:
- No mortality/DALY computation when the proposal is framed in DALYs
- Cannot evaluate an intervention in the proposal (e.g., an
  age-targeted intervention without age structure, vaccines without
  immunity waning)
- Coarser geographic resolution than the funding decision requires
- No comparison to the model the country used in their own plan

### WHO / Policy Advisory Bodies

**They decide**: Whether to recommend a strategy change for member states.

**They require**:
- Generalizability across settings (not just one country)
- Sensitivity to context-specific parameters (transmission intensity,
  health system capacity, vector species)
- Comparison to existing WHO-endorsed models and guidance
- Uncertainty quantification that propagates through to recommendations
- Clear separation of model-driven vs data-driven conclusions

**Disqualifying gaps**:
- Model only works for one specific setting with no pathway to generalize
- Conclusions depend on a single parameter value without sensitivity analysis
- No uncertainty on the key policy-relevant output

### Academic Journals (Lancet, PLOS Med, Nature Medicine)

**They decide**: Whether the work advances scientific understanding.

**They require**:
- Out-of-sample validation (temporal or spatial holdout)
- Comparison to at least one alternative model structure
- Residual diagnostics demonstrating model assumptions hold
- Reproducible methods (code availability, parameter tables)
- Novel contribution beyond what existing models already show

**Disqualifying gaps**:
- No out-of-sample validation at all
- No comparison to existing published models
- Results are a subset of what an established model already produces

### Internal Decision Support / Rapid Analysis

**They decide**: Whether to investigate further or change course.

**They require**:
- Directionally correct answers (sign and rough magnitude)
- Honest uncertainty bounds
- Clear statement of what the model can and cannot answer

**Disqualifying gaps**: Few — this is the most forgiving audience. A Level 1
model is usually sufficient. The main risk is presenting a rapid analysis
as if it were a full evaluation.

---

## 3. Structural Gap Detection

A structural gap exists when the model cannot capture a mechanism that
matters for the decision, regardless of how well it's implemented.

### The Mechanism Test

For each decision the model is supposed to inform:

1. **List the options** being compared (e.g., bednets vs seasonal
   chemoprevention vs indoor spraying; or HPV vaccination vs screening)
2. **For each pair of options**: what mechanism makes them different?
   - Malaria bednets vs seasonal chemoprevention: nets protect all ages
     via reduced biting; chemoprevention protects children 3-59 months
     → **age structure** differentiates
   - Bednets vs indoor spraying: both reduce transmission but at
     different costs and durations → **no age structure needed**
   - HPV vaccination vs cervical screening: vaccination prevents
     infection in adolescents with benefits decades later; screening
     detects lesions in adult women now → **age structure and long time
     horizon** differentiate
3. **Does the model capture that differentiating mechanism?**
   - If yes → the comparison is structurally valid
   - If no → the comparison is structurally invalid, regardless of metrics

### The "Simpler Question" Test

Read the research question. Now read what the model actually answers.
Are they the same question?

Common downgrades:
| Asked | Actually answers | Missing |
|-------|-----------------|---------|
| "Optimize allocation of A+B+C+D" (e.g., four interventions) | "Optimize allocation of A+B" | Age structure for the age-targeted one, transient dynamics for a mass campaign |
| "Cost per DALY averted" | "Cost per case averted" | Mortality module with age-specific CFR |
| "Subnational targeting" | "Zone-level allocation" | Sub-zonal resolution |
| "Compare to the national strategic plan" | "Independent optimization" | Ability to represent the plan's scenarios |
| "Project impact to 2030" | "Equilibrium at 2021" | Time-varying dynamics, demographic projections |

If the model answers a simpler question → this is a structural gap,
not a parameter error.

### The Audience Rejection Test

Imagine presenting this model to the intended audience. Would they:

(a) Engage with the results and debate the findings? → FIT
(b) Immediately ask "but where is [feature X]?" → STRUCTURAL GAP
(c) Say "this is a useful starting point, but we need [X] before we
    can use it for our decision" → LEVEL ESCALATION NEEDED

If the answer is (b) or (c), the model needs structural changes before
acceptance. Individual metric improvements won't help.

---

## 4. Level Escalation Criteria

Models should progress from simple to complex. The modeling-strategy skill
covers when to ADD complexity. This section covers when to ESCALATE from
a completed Level N to Level N+1.

### When to Accept at Current Level

- The model answers the stated question at the required resolution
- The intended audience would engage with the results (not reject structure)
- All intervention comparisons are structurally valid
- Remaining limitations are acknowledged and don't affect the core finding

### When to Escalate

- The model answers a simpler question than asked (see table above)
- A key intervention comparison is structurally invalid
- The audience would reject the model structure before examining results
- Critique identifies the same structural gap from multiple angles
  (e.g., "no age structure" shows up as: can't evaluate the
  age-targeted intervention, can't compute DALYs, can't compare to the
  national strategic plan)

### How to Escalate

1. **Keep Level N as the baseline**. Do not discard it. It provides:
   - A sanity check (Level N+1 should broadly agree on the comparisons
     Level N can make)
   - A "floor" result (if Level N+1 fails, you still have Level N)
   - Documentation of how conclusions change with complexity

2. **Add the minimum structural change** to close the gap:
   - If the gap is age structure → add 2 age groups (under-5, 5+), not 20
   - If the gap is mortality → add age-weighted CFR, not a full demographic model
   - If the gap is resolution → go to admin-1, not admin-2
   - Each escalation should close ONE structural gap, not three

3. **Re-run the same hypotheses** with Level N+1 and compare:
   - Do intervention rankings change? (If yes → Level N was misleading)
   - Do magnitudes change by >2x? (If yes → Level N was unreliable)
   - Do conclusions change? (If yes → document why and which level is
     more appropriate for the audience)

---

## 5. Fitness Evaluation Checklist

Use this checklist at the STAGE 7 decision point, before triaging
individual critique items.

```
## Fit-for-Purpose Assessment

### 1. Audience: [who will use this]
### 2. Decisions to be made: [list them]
### 3. Mechanism coverage:
For each decision:
- Options compared: [A vs B]
- Differentiating mechanism: [what makes them different]
- Model captures it? [YES/NO — if NO, this is a structural gap]

### 4. Question match:
- Question asked: [original research question]
- Question answered: [what the model actually answers]
- Match? [YES / SIMPLER — if simpler, specify what's missing]

### 5. Audience test:
- Would the audience engage with results? [YES / REJECT STRUCTURE]

### 6. Decision:
- [ ] All mechanisms covered, question matches → ACCEPT or PATCH
- [ ] Structural gap exists → RETHINK (escalate to Level N+1)
- [ ] Gap exists but unfixable in this iteration → DECLARE_SCOPE
      (document what Level N+1 would need)
```

---

## 6. Common Pitfalls

### "The metrics are good so the model is good"
Metrics evaluate implementation quality, not structural fitness.
A model can have perfect calibration (RMSE=0) and still be structurally
unfit if it can't compare the interventions the audience cares about.

### "The critique items are individually patchable"
Multiple patchable items that all trace to the same structural gap
are NOT patchable — they're symptoms. Fixing one intervention's cost, adding a
post-hoc DALY layer, and adjusting the budget framing are all patches
around the same gap: the model lacks age structure for a question that
requires it.

### "We declared scope so it's fine"
Scope declaration is appropriate when the model answers its question
but the question has a boundary. It's NOT appropriate when the model
can't answer its own stated question. "This model allocates
interventions A and B but not C" is a scope declaration. "This model allocates all four
interventions but can't properly evaluate one of them" is a structural gap.

A declaration is also a claim that the decision does not depend on the
finding, and the gate asks for the evidence (Phase 49 α, A20). For every HIGH
gate finding you declare, re-run the decision with the finding's quantity set
to its target and record whether the recommendation changes: the held-out
fold's own parameters for an unstable allocation, the trial's value for a
drifting effect benchmark, the benchmark's cost for a cost drift. If it does
not change, the declaration stands and the report cites the test; if it does,
the finding is a structural gap, not a scope boundary, and must be fixed.
"No data to fix it" explains why a finding cannot be fixed; it is not evidence
that the decision does not depend on it. The result format, where it goes and
the few exemptions (the owner scoped it out; a finding with no quantity to
vary) are in the rigor manifest, entry `decision_tests`.

When the finding bears on the primary decision itself (a per-unit allocation
that does not hold out of sample; a benchmark drift for an intervention the
decision chooses among), the lead cannot declare it alone: the owner reads the
declaration and its test and accepts or declines it (Phase 49 β, entry
`scope_signoff`). Write the test so it answers the owner's question: does the
package per unit change?

### "Level 1 is good enough for now"
Level 1 IS good enough when the audience is internal decision support
or rapid analysis. Level 1 is NOT good enough when the audience is a
funding proposal (Global Fund, Gavi) or a Lancet paper. Match the level to the audience,
not to your time budget.

---

## 7. Optimization Result Sanity Checks

When the model includes optimization or resource allocation, apply
these checks BEFORE deciding ACCEPT vs RETHINK at the Stage 7 decision
point.

### The "Would a Domain Expert Laugh?" Test

Mentally show the optimization result to a domain expert. Would they:
(a) Engage with the tradeoffs --> PASS
(b) Immediately say "this can't be right" --> FAIL --> RETHINK

Examples of obvious failures that indicate structural model problems:
- Zero investment in the highest-burden geographic area
- Zero allocation to an intervention WHO recommends for this setting
- Cost per DALY 100x outside the published range for an intervention
- The optimal portfolio ignores a dimension the question specifically
  asks about (e.g., "allocate across A, B, and C" but C gets 0%)

### Cost-Effectiveness Benchmark Table

Before accepting any optimization, the lead should verify:

| Intervention | Model $/DALY | Published $/DALY range | Within 5x? |
|-------------|-------------|----------------------|------------|
| ... | ... | ... | YES/NO |

If ANY intervention is outside 5x of its published range, the model's
representation of that intervention is structurally wrong. This is
RETHINK, not PATCH -- fixing a parameter won't fix a broken mechanism.

### Geographic Allocation Sanity

For geographic optimization, verify:
- Highest-burden area gets investment (or there is a documented,
  clinically plausible reason it doesn't -- e.g., already at near-
  universal coverage)
- No area is completely zeroed out unless it has near-zero baseline
  burden
- The allocation pattern is explainable in terms of burden, cost, and
  intervention effectiveness -- not model artifacts (a capped transmission parameter, step-
  function thresholds, equilibrium saturation)

### Convergent Structural Critique

If the methods AND domain critics both flag the same structural issue
as HIGH severity, this is converging evidence of a fundamental problem.
This is RETHINK regardless of what individual critique items say. Do
not let individually-patchable symptoms obscure a shared structural
root cause.
