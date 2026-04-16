---
name: modeling-strategy
description: Principled framework for model selection, complexity management,
  and strategic decision-making in mathematical modeling. Covers purpose-driven
  model selection, information criteria (AIC/BIC), parameter identifiability,
  pattern-oriented modeling, and the rethink-vs-patch decision. Use when
  deciding model complexity, comparing model architectures, assessing
  identifiability, declaring model scope, or deciding whether to simplify.
  Trigger phrases include "model selection", "should I add complexity",
  "AIC BIC comparison", "identifiability", "parsimony", "fit for purpose",
  "rethink approach", "simplify model", "model scope", "pattern-oriented".
---

# Modeling Strategy: Principled Complexity Management

## 1. Purpose-Driven Model Selection

The model's purpose determines its appropriate complexity. Not every question
needs the most biologically realistic model.

> "Model choice should be driven by the question, not by the desire for
> realism." — James, Salomon, Buckee, Menzies (2021)

### Three Modeling Purposes

| Purpose | Goal | Complexity Principle | Example |
|---------|------|---------------------|---------|
| **Prediction / Forecasting** | Predict future values with uncertainty | PARSIMONY — simplest model with positive skill score | "How many flu hospitalizations next week?" |
| **Scenario Comparison / Allocation** | Rank interventions or allocations | FIT-FOR-PURPOSE — capture mechanisms that differentiate scenarios | "Should Nigeria invest more in ITNs or IRS?" |
| **Mechanistic Understanding** | Understand why a phenomenon occurs | IDENTIFIABILITY — every parameter must be estimable | "Why does northern Nigeria have more cVDPV2 emergences?" |

### Decision Rule

1. Classify your question into one of the three purposes
2. Select complexity based on that purpose, not on what's biologically possible
3. A scenario comparison model doesn't need to predict historical trends —
   it needs relative rankings to be right
4. A forecasting model doesn't need mechanistic realism —
   it needs out-of-sample skill

### Common Mistake: Purpose Mismatch

Building a complex mechanistic model when the question only needs scenario
comparison. Signs of mismatch:
- Model takes hours to calibrate but the question is about relative rankings
- Model can't reproduce historical trends but the question doesn't require it
- Model has 20+ parameters but only 6 calibration targets

---

## 1b. Build vs Adapt: The Implementation Decision

After deciding WHAT model to build, decide HOW to build it. Published
implementations exist for most well-studied diseases. The choice between
building from scratch and adapting existing code is a cost-risk tradeoff,
not a preference.

### Classify Each Model Component

For each mechanism your model needs, ask: **how hard is this to get right
from scratch?**

| Difficulty | Examples | Build vs Adapt |
|-----------|---------|----------------|
| **Routine** — standard equations, well-documented, hard to get wrong | SEIR compartments, seasonal forcing (sinusoidal), cost optimization, data loading | Build from scratch — faster than learning someone else's code |
| **Moderate** — requires careful parameterization, easy to make subtle errors | Age-structured transmission, OR-to-RR conversion, waning immunity (single-rate), dose-response curves | Either — depends on time budget and available code |
| **Hard** — multi-layer dynamics where subtle errors produce models that calibrate but give wrong intervention effects | Superinfection + acquired immunity (clinical + anti-parasite), within-host parasite dynamics, vector genetics, multi-strain competition | Strongly prefer adapting existing code — these dynamics took domain experts years to get right |

### The Key Question

> "If I build this from scratch and get the hard dynamics slightly wrong,
> will I know? Or will the model calibrate fine and produce plausible-looking
> but incorrect intervention effects?"

For **routine** components, mistakes are obvious (negative populations,
conservation violations, NaN). For **hard** components, mistakes hide:
the model calibrates to baseline data but intervention effects are wrong
because the feedback loops are subtly broken. A fudge factor (scale factor,
detection fraction) can mask this — the model matches totals but the
marginal response to interventions is mechanistically wrong.

### Decision Framework

```
1. List the HARD mechanisms your model needs
2. For each: does published, open-source code exist?
   - YES and same language → clone and adapt (lowest risk)
   - YES but different language → assess translation effort:
     * Can you call it as a subprocess? (R from Python, C from Python)
     * Can you run it as a reference via a model-tester subagent?
     * Is the translation tractable in your time budget?
   - NO → build from scratch, but budget extra time for validation
3. For ROUTINE mechanisms: build from scratch (faster, cleaner)
4. Document your decision and reasoning in modeling_strategy.md
```

### Running Reference Implementations

Even when building from scratch, consider running a published
implementation in parallel as a **reference baseline**:
- Spawn a model-tester subagent to clone, install, and run the
  reference model with the same inputs
- Compare your model's intervention effects against the reference
- If they agree on direction and magnitude (within 2x): your model
  is likely correct
- If they disagree: investigate before trusting your model —
  the published implementation has been validated by domain experts

This is especially valuable for hard dynamics where your from-scratch
implementation might calibrate correctly but produce wrong marginal
effects. The reference model catches this.

### Common Trap: Underestimating Translation Cost, Overestimating Build Cost

Modelers often think "translating R to Python is too risky" and choose to
build from scratch. But building complex dynamics from scratch has its own
risks — and the failure mode is worse: translation errors are usually
obvious (code doesn't run, numbers don't match), while from-scratch
errors are often invisible (model runs, calibrates, but intervention
effects are subtly wrong).

If you find yourself adding a scale factor, detection fraction, or
calibration multiplier to match targets that the published model matches
mechanistically — that's a signal you should have adapted instead of
building from scratch.

---

## 2. Pattern-Oriented Modeling (Grimm 2005)

> "A model from which the patterns emerge should contain the right mechanisms
> to address the problem."

### The Principle

Before adding any mechanism, ask: **does the model need to reproduce this
pattern to answer the question?**

### Pattern Selection Process

1. List the patterns the model MUST reproduce (from the research question)
2. List the patterns that would be NICE to reproduce (for validation)
3. List the patterns that are IRRELEVANT to the question
4. Build mechanisms only for categories 1 and 2
5. Do NOT add mechanisms for category 3 patterns

### Example (Malaria Nigeria Allocation)

| Pattern | Category | Mechanism Needed? |
|---------|----------|-------------------|
| Zone-level PfPR heterogeneity | MUST (drives allocation) | Yes: zone-specific EIR |
| Seasonal transmission timing | MUST (affects intervention timing) | Yes: seasonal forcing |
| ITN dose-response | MUST (key intervention) | Yes: coverage → prevalence |
| Historical 2010-2021 decline | NICE (validation) | Only if data exists to calibrate |
| Stochastic fadeout in low-transmission zones | IRRELEVANT at zone scale | No |
| Individual-level immune dynamics | IRRELEVANT for allocation | No |

---

## 3. Information Criteria for Model Comparison

Complexity must earn its place via measurable improvement in predictive
accuracy, penalized for parameter count.

### AIC (Akaike Information Criterion)

```
AIC = 2k + n × log(RSS/n)
```
where k = number of parameters, n = number of observations, RSS = residual sum of squares.

### BIC (Bayesian Information Criterion)

```
BIC = k × log(n) + n × log(RSS/n)
```
BIC penalizes complexity more heavily than AIC for n > 7.

### Interpretation

| ΔAIC (complex − simple) | Evidence for complex model |
|--------------------------|---------------------------|
| < 2 | Essentially no difference — prefer simpler |
| 2–10 | Weak evidence — complexity questionable |
| > 10 | Strong evidence — complexity justified |

### When to Use

- **Always** when comparing two or more model architectures
- **Always** when deciding whether to add a parameter
- Report in model_comparison.md alongside RMSE and skill scores

### When NOT to Use

- Comparing models fit to different datasets
- Comparing mechanistic and statistical models (different likelihood structures)
- When the question is about mechanism, not prediction

---

## 4. Parameter Identifiability

> "Parameter nonidentifiability is a critical challenge... models are often
> practically nonidentifiable when calibrated with limited data."

### The Principle

Before adding a parameter, ask: **can it be uniquely estimated from the
available data?** If not, it creates false precision.

### Methods to Test Identifiability

**1. One-at-a-Time (OAT) Sensitivity**
- Vary parameter ±50% from its calibrated value
- Re-fit remaining parameters
- If calibration target changes by less than its measurement uncertainty:
  parameter is non-identifiable → fix to literature value

**2. Profile Likelihood**
- Fix the parameter at a grid of values
- Optimize all other parameters at each grid point
- Plot: loss vs parameter value
- Flat profile = non-identifiable; U-shaped profile = identifiable

**3. Pairwise Compensation**
- Vary two parameters simultaneously
- If one can increase while the other decreases with no change in fit:
  they are jointly non-identifiable → fix one

### Decision Rules

| Result | Action |
|--------|--------|
| Parameter identifiable (clear profile minimum) | Estimate from data, report CI |
| Parameter non-identifiable (flat profile) | Fix to literature value, report as fixed |
| Joint non-identifiability (compensation pair) | Fix one to literature, estimate the other |
| No literature value available | Report as structural uncertainty in scope |

### Common Mistake: Estimating Non-Identifiable Parameters

Run 10 example: (β_eff, wan_mean) were jointly non-identifiable from
6-point cross-sectional PfPR. The agent estimated both, creating a
non-identifiability ridge. A principled response: fix wan_mean to the
literature value (180 days) and estimate β_eff only.

---

## 5. The Rethink Decision

When critique feedback arrives, the strategic question is: **patch or rethink?**

### PATCH (fix within current approach)

Appropriate when:
- Code bug (calculation error, wrong formula)
- Missing output (sensitivity analysis, figure, metric)
- Presentation issue (captions, formatting)
- Parameter value needs adjustment (within the model's structure)

### RETHINK (change approach)

Appropriate when:
- Same key metric hasn't improved for 2+ critique rounds
- Parameter non-identifiability discovered and can't be resolved with
  available data
- Model can't reproduce a pattern NEEDED for the question
- Out-of-sample skill score ≤ 0 after 2+ attempts to fix
- Added module didn't improve AIC by >10 (not justified)
- Model complexity exceeds what the purpose requires

### REDIRECT (wrong stage)

Appropriate when:
- Problem is data quality or availability → back to DATA
- Problem is wrong hypotheses or wrong question framing → back to PLAN

### DECLARE SCOPE (honest limitations)

Appropriate when:
- Model can answer some but not all parts of the question
- Some hypotheses are untestable with available data/model
- A structural limitation exists that can't be resolved in this iteration
- The model is fit for its primary purpose even if not for everything

### Decision Heuristic

```
1. Is the critique about a code bug or missing output? → PATCH
2. Has this same issue appeared before? → RETHINK
3. Did the last revision improve the key metric? → if no, RETHINK
4. Is the issue about data, not model? → REDIRECT to DATA
5. Is the model already fit for its stated purpose? → DECLARE SCOPE
6. None of the above? → PATCH (default, but log reasoning)
```

---

## 6. Scope Declaration

Every model has a valid scope. Declaring it honestly is not failure —
it's scientific integrity.

### Format

```markdown
## Model Scope Declaration

### Fit for:
- [Specific use case the model addresses, with evidence]
- Example: "Cross-sectional zone-level allocation ranking at 2021
  epidemiology (6 zones calibrated within ±3pp of NMIS 2021)"

### Not fit for:
- [Specific use case the model cannot address, with evidence]
- Example: "Forward projection of 2024-2030 incidence trends
  (SS_long = 0.000; historical decline not reproducible without
  treatment time series data)"

### What would extend the scope:
- [Specific data, model changes, or compute needed]
- Example: "LGA-level DHIS2 treatment data + Bayesian calibration
  via calabaria cloud would enable longitudinal validation"
```

---

## References

1. James LP, Salomon JA, Buckee CO, Menzies NA. "The Use and Misuse of
   Mathematical Modeling for Infectious Disease Policymaking." Med Decis
   Making. 2021. [PMC7862917](https://pmc.ncbi.nlm.nih.gov/articles/PMC7862917/)
2. Grimm V et al. "Pattern-Oriented Modeling of Agent-Based Complex Systems:
   Lessons from Ecology." Science. 2005.
3. MODELS Framework. [PMC11022334](https://pmc.ncbi.nlm.nih.gov/articles/PMC11022334/)
4. CDC Modeling Handbook. [Overview](https://www.cdc.gov/cfa-modeling-and-forecasting/modeling-handbook/mh-overview.html)
5. Burnham KP, Anderson DR. "Model Selection and Multimodel Inference."
   Springer. 2002. (AIC/BIC reference)
