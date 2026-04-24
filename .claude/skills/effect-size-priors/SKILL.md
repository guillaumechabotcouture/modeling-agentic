---
name: effect-size-priors
description: Machine-readable contract for parameter provenance. Every load-bearing
  numerical constant in model code that is sourced from the literature (intervention
  effect sizes, odds ratios, efficacies, cost figures, calibration targets) must
  have a registry entry in citations.md under a `## Parameter Registry` section.
  Each entry records the point estimate, 95% CI, distribution kind (odds_ratio /
  relative_risk / efficacy / coverage / cost_usd / …), linked source [CNN], the
  subgroup the value applies to, and code_refs at file:line level. The registry
  enables three mechanical checks in the STAGE 7 gate: (a) OR-used-as-RR conflation
  detection; (b) code value vs registry value mismatch; (c) subgroup misapplication
  (overall estimate used for a subgroup context). Use when writing or reviewing
  citations.md, when adding a literature-sourced parameter to model code, or when
  the gate's `--parameter-registry` flag is firing. Trigger phrases include
  "parameter provenance", "odds ratio", "effect size", "citations registry",
  "OR RR conflation", "@registry".
---

# Effect-Size Priors: Parameter Registry

## Why this exists

The malaria Nigeria run (2026-04-23 regression) revealed two categories of
silent error that critique agents missed:

1. **OR-used-as-RR conflation.** Zhou 2022 IRS odds ratio (`OR=0.35, 95% CI
   0.27–0.44`) was used in code as if it were a relative risk in the PfPR
   model. At PfPR 0.3–0.5 the true RR is 0.45–0.65, not 0.35 — the model
   overstated IRS effect by 30–50%.

2. **Code value vs "source" value mismatch.** Intervention costs hardcoded
   in `level3_interventions.py` as $2/net, $2.50/PBO, $3/dual-AI, $4/IRS.
   The "source" CSV at `data/intervention_costs.csv` says $7.50, $10,
   $12.50, $15 — a 3–4× discrepancy. The $320M envelope therefore bought
   3–4× more intervention than reality.

Both are mechanical errors. Both are invisible to a critique agent reading
the report without cross-checking the code. The registry makes them
mechanically detectable.

## What goes in the registry

Every parameter in model code that satisfies ANY of these criteria MUST
have a registry entry:

- Value comes from a specific cited paper (not derived, not a standard constant).
- Value has a published confidence interval, standard error, or range.
- Value is used as an input to a decision-relevant calculation (cost, burden,
  ICER, allocation, budget).
- Value would be a target of sensitivity analysis or uncertainty quantification.

Parameters that do NOT need registry entries:
- Mathematical constants (π, e, …).
- Code-internal values (loop counts, array sizes).
- Time discretization (dt, n_years) when it's a modeling choice, not a literature value.
- Purely presentational defaults (figure DPI, plot colors).

## Schema

Add a section to `citations.md` titled **`## Parameter Registry`** containing
a single fenced YAML block. Each parameter is one entry in the `parameters:`
list:

```yaml
parameters:
  - name: irs_odds_ratio                # unique, snake_case; referenced from code
    value: 0.35                         # point estimate
    ci_low: 0.27                        # 95% CI lower bound (REQUIRED when available)
    ci_high: 0.44                       # 95% CI upper bound (REQUIRED when available)
    kind: odds_ratio                    # see "kind values" below
    source: C11                         # matches existing [C11] in citations.md
    subgroup: "children under 5 in high-transmission settings"
    applies_to: "IRS monthly EIR reduction multiplier"
    code_refs:                          # every use-site in code — file:line
      - "models/level3_interventions.py:184"
    notes: |                            # optional free text
      Zhou 2022 IDP. Meta-analysis of IRS trials. OR from pooled logistic
      regression; NOT a relative risk. Convert via or_to_rr(OR, baseline_p)
      before applying as a multiplicative reduction.
```

### Valid `kind` values

| kind                   | Typical range | Sampling prior           | Notes                                 |
|------------------------|---------------|--------------------------|---------------------------------------|
| `odds_ratio`           | 0 to ∞ (~1)   | log-normal on log(OR)    | **Not interchangeable with RR.**      |
| `relative_risk`        | 0 to ∞ (~1)   | log-normal on log(RR)    | Interchangeable with IRR at small p.  |
| `hazard_ratio`         | 0 to ∞ (~1)   | log-normal on log(HR)    |                                       |
| `incidence_rate_ratio` | 0 to ∞ (~1)   | log-normal on log(IRR)   |                                       |
| `efficacy`             | 0.0 to 1.0    | beta fit to (mean, CI)   | Reported as proportion (0.55, not 55%)|
| `coverage`             | 0.0 to 1.0    | beta fit to (mean, CI)   |                                       |
| `proportion`           | 0.0 to 1.0    | beta fit to (mean, CI)   | E.g. CFR.                             |
| `rate`                 | 0 to ∞        | gamma or log-normal      | E.g. incidence per person-year.       |
| `cost_usd`             | 0 to ∞        | log-normal or truncated  | Report per-unit basis in `notes`.     |
| `prevalence`           | 0.0 to 1.0    | beta                     |                                       |
| `duration_days`        | 0 to ∞        | gamma or log-normal      |                                       |

### The OR / RR trap (most common error)

**Odds ratios and relative risks ARE NOT INTERCHANGEABLE** when the outcome
is common (baseline risk > ~10%). Converting:

    RR = OR / (1 - p_baseline + p_baseline × OR)

where `p_baseline` is the comparator's absolute risk. Example: if baseline
PfPR is 40% and published OR is 0.35:

    RR = 0.35 / (0.60 + 0.40 × 0.35) = 0.35 / 0.74 = 0.47

The "right" way to use a registered `kind: odds_ratio` in code:

```python
# @registry:irs_odds_ratio
irs_or = 0.35
irs_rr = or_to_rr(irs_or, baseline_pfpr)   # convert at the appropriate p
new_prevalence = baseline_pfpr * irs_rr    # then apply as RR
```

A HIGH blocker fires when the registry has `kind: odds_ratio` but the code
uses the value without a conversion function call nearby. See "Gate rules"
below.

## Code-side requirements

Every load-bearing constant in model code carries a `# @registry:<name>`
comment on its line:

```python
# @registry:irs_odds_ratio
irs_or = 0.35

# @registry:smc_rr_clinical
smc_rr = 0.27
```

The registry's `code_refs` must list each such line. The validator resolves
every `code_refs` entry and flags any that:
- Points to a non-existent file:line.
- Points to a line whose numeric literal doesn't match the registry's `value`
  (±1% tolerance for rounding).
- Lacks the matching `# @registry:<name>` tag.

When a line has a `# @registry:<name>` but the name is NOT in the registry,
that's a `param_unregistered` MEDIUM blocker.

## Gate rules

`scripts/validate_critique_yaml.py --parameter-registry` invokes
`scripts/effect_size_registry.py` and adds these checks to the decision:

| Check                                      | Severity | Fires when                                                                     |
|--------------------------------------------|----------|--------------------------------------------------------------------------------|
| `registry_missing_ref`                     | MEDIUM   | code_refs file:line doesn't exist in the repo                                  |
| `registry_value_mismatch`                  | HIGH     | code literal differs from registry value by >1%                                 |
| `or_rr_conflation`                         | HIGH     | `kind: odds_ratio` but the code near code_ref has no conversion (`or_to_rr`, `odds_to_risk`, explicit formula) |
| `subgroup_mismatch`                        | MEDIUM   | `subgroup` field restricts applicability but code uses value in a broader context |
| `param_unregistered`                       | MEDIUM   | code has `# @registry:X` but X not in `parameters:` list                       |
| `cost_crosscheck_mismatch`                 | HIGH     | `kind: cost_usd` code value differs from any CSV referenced in code_refs by >10% |

HIGH blockers fold into `unresolved_high` via the synthetic `OBJ-NNN`
mechanism (same as spec-compliance). A run cannot ACCEPT with any
`or_rr_conflation` or `registry_value_mismatch` unresolved.

## Minimal example (from the malaria run, how it SHOULD have been)

```yaml
## Parameter Registry

parameters:
  - name: irs_odds_ratio
    value: 0.35
    ci_low: 0.27
    ci_high: 0.44
    kind: odds_ratio
    source: C11
    subgroup: "high-transmission; ≥80% coverage"
    applies_to: "IRS effect on infection odds"
    code_refs:
      - "models/level3_interventions.py:184"
    notes: |
      Zhou 2022 IDP meta-analysis. NOT a relative risk. In the ABM,
      convert via or_to_rr(OR, baseline_pfpr) before applying to prevalence.

  - name: smc_rr_clinical
    value: 0.27
    ci_low: 0.25
    ci_high: 0.29
    kind: relative_risk
    source: C13
    subgroup: "children 3-59 months, seasonal transmission, 4 monthly cycles"
    applies_to: "SMC effect on clinical malaria incidence in target age"
    code_refs:
      - "models/level3_interventions.py:72"
    notes: |
      Wilson 2011 Cochrane. RR for clinical malaria during the intervention
      period only; do NOT annualize. Apply only during SMC months (roughly
      July-October in Sahel).

  - name: itn_pbo_net_unit_cost
    value: 10.00
    ci_low: 8.50
    ci_high: 12.50
    kind: cost_usd
    source: C21
    subgroup: "procurement+delivery, 2024 USD, Nigeria"
    applies_to: "Unit cost per PBO net, amortized over 3-year lifespan"
    code_refs:
      - "models/level3_interventions.py:183"
      - "data/intervention_costs.csv"
    notes: |
      Per-net cost delivered to end user. Includes procurement, shipping,
      distribution. Amortize over 3 years of use at ~1.8 nets per household.
      Current code uses $2.50/net which is the procurement-only cost and
      excludes delivery — THIS IS A COST_CROSSCHECK_MISMATCH.
```

## For the modeler: what to do when adding a literature constant

1. Read the source paper. Record the point estimate, 95% CI (or equivalent),
   subgroup, and what the value applies to.
2. Decide the `kind`. **Especially for OR vs RR** — do not guess. Check the
   paper's methods section. A logistic regression produces OR. A cohort or
   RCT typically produces RR. An IRR comes from Poisson regression on rates.
3. Add an entry to `## Parameter Registry` in `citations.md`.
4. In the code, put `# @registry:<name>` on the line of the literal.
5. If `kind: odds_ratio`, write or use an `or_to_rr` helper. Do NOT apply
   the OR as a multiplicative RR. If you're not sure which conversion
   applies, stop and check the paper.

## For the writer: what to report

When the report contains a quantitative claim supported by a registered
parameter, the report should cite the parameter's `source` (e.g., "IRS
reduces infection odds by a factor of 0.35, Zhou 2022 [C11]") and report
the registered CI, not a perturbation-derived one. The writer's per-table
uncertainty-scope footnote rule from Phase 1.5 Commit A applies: state
which registered priors were propagated.

## For critique-methods: what to check

Beyond the mechanical validator checks, critique-methods reviews:
- Did the modeler classify `kind` correctly? (Is that really an OR, or
  did the paper report RR?)
- Is the `subgroup` specification tight enough? (An overall RR being
  applied to a specific subgroup is the single most common citation abuse.)
- Are the `code_refs` complete? (Every use of a registered value in
  decision-relevant code should be listed.)

These judgment calls are critique-methods' domain. The mechanical checks
handle the rest.
