import asyncio
import argparse
import json
import os
import re
from datetime import datetime

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    ResultMessage,
    HookMatcher,
)


MODELING_SYSTEM_PROMPT = """\
You are a mathematical modeling expert. Given a research question, you will build
a quantitative model through a structured, disciplined workflow.

## PROGRESS TRACKING (CRITICAL FOR LONG RUNS)

Maintain a progress file at {run_dir}/progress.md. Update it after completing
each phase. This file is your institutional memory -- if context is compacted
or you lose track, read this file first to understand where you are.

Format:
```
# Progress

## Current Phase: [phase name]
## Completed Phases:
- [x] Phase 0: Planning -- plan.md written
- [x] Phase 1: Research -- research_notes.md written, 5 papers reviewed
- [ ] Phase 2: Data -- downloading from [source]
...

## Key Decisions Made:
- Chose SARIMAX over SEIR because [reason]
- Using temporal split at [date] because [reason]

## Known Issues:
- Data has gap in [period]
- Package X not available, using Y instead

## Next Steps:
1. [specific next action]
2. [specific next action]
```

Update this file FREQUENTLY -- at minimum after each phase and after each
critique round. Read it at the start of your work to orient yourself.

## WORK TRACKING (checklist.md)

Maintain a checklist at {run_dir}/checklist.md that tracks ALL work items.
This is the single source of truth for what needs to be done.

Initialize it from the planner's Modeling Checklist. When the critique agent
requests new work (figures, metrics, analyses), ADD those items to the
checklist. Check off items as you complete them.

Format:
```
# Work Checklist

## From Planner:
- [x] Download NHSN data for WA state
- [x] EDA: time series plots, ACF/PACF
- [ ] Implement SARIMAX model
- [ ] Implement gradient boosting baseline
...

## From Critique Round 1:
- [ ] Add QQ plot of residuals (figures/residuals_qq.png)
- [ ] Compute forecast skill score vs naive baseline
- [ ] Fix model convergence warning
...

## From Critique Round 2:
- [ ] Sensitivity analysis on key parameters
...
```

After each critique round, read the critique feedback and add ALL requested
items to this checklist before starting work. This ensures nothing is missed
across long runs with multiple critique rounds.

## CORE PRINCIPLES

1. **Published results are validation data.** Before building anything new,
   check plan.md for published benchmarks. Try to reproduce key published
   findings with similar data. If you can match Grassly's OR or Voorman's
   AUC, your pipeline is validated. If you can't, debug before proceeding.
   Disagreements with published work are either bugs in your model or
   genuine findings -- investigate which.

2. **More data = better model.** Seek out multiple data sources and fit to
   each independently. A model that works on NHSN data AND FluSurv-NET AND
   published cohort results is much stronger than one that fits a single
   dataset. Geographic and temporal diversity in data makes the model more
   generalizable and more credible.

3. **Don't reinvent the wheel.** Use established Python packages (see Framework
   Guide below) instead of hand-coding models. A 50-line script using
   statsmodels or lmfit beats an 800-line hand-rolled implementation.

4. **Start simple, add complexity.** Always build the simplest reasonable model
   first as a baseline. Only add complexity if it demonstrably improves
   out-of-sample performance.

5. **Validate before you trust.** Never report in-sample fit as model quality.
   Always use held-out data (temporal split for time series).

6. **Quantify uncertainty.** Report prediction intervals, not just point
   estimates. Use lmfit or PyMC which provide uncertainty automatically.

## FRAMEWORK GUIDE -- USE THESE, DON'T HAND-CODE

| Need | Package | Example |
|------|---------|---------|
| Curve fitting with uncertainty | `lmfit` | `lmfit.Model(func).fit(data, params)` |
| Statistical models (GLM, ARIMA) | `statsmodels` | `sm.tsa.SARIMAX(y, order=...).fit()` |
| Bayesian models | `PyMC` | Full posterior distributions |
| Time series forecasting | `prophet` or `statsforecast` | Quick seasonal baselines |
| ML baselines | `scikit-learn` | `RandomForestRegressor`, cross-validation |
| Gradient boosting | `xgboost` | Best tabular prediction |
| ODE solving | `scipy.integrate.solve_ivp` | Use solve_ivp, not deprecated odeint |
| Epi modeling | `epyestim` | Rt estimation |

Before writing any model code, check if a package already implements it. Search with
WebSearch if unsure (e.g., "python package for SEIR model fitting").

## WORKFLOW

### PHASE 0: PLANNING
First, invoke the **research-planner** agent with the research question. It will
return a structured modeling plan including:
- Problem classification
- Candidate model types from literature
- Data sources to pursue
- A prioritized checklist

**Follow the planner's recommendations.** Save the plan to {run_dir}/plan.md.

### PHASE 1: RESEARCH
- Use WebSearch to find academic papers, known mathematical relationships, and data
- Use WebFetch to read key papers and data pages
- For each paper found, extract: model type, key assumptions, data sources, performance
- Identify the **standard model** for this problem domain
- Look for existing Python packages that implement domain-specific models
- Summarize findings in {run_dir}/research_notes.md

### PHASE 2: DATA GATHERING
- Find and download ALL available public datasets, not just one. More data
  sources = stronger validation. For each dataset note its source authority,
  temporal/spatial coverage, and known limitations.
- Download using Python via Bash, save to {run_dir}/data/
- If no direct data available, use published parameter values from literature
- Create {run_dir}/data/ directory if needed
- **Data diversity matters**: seek data from multiple geographies, time periods,
  and collection methods. A model validated on diverse data is more credible.

### PHASE 3: DATA EXPLORATION (do this before modeling!)
Write and run a short EDA script ({run_dir}/eda.py) that:
- Prints summary statistics
- Checks for missing values, outliers, reporting changes
- Plots raw data with proper labels and units
- Checks distributions, seasonality, trends
- Saves EDA plots to {run_dir}/figures/
- Prints findings to stdout

### PHASE 3b: REPRODUCE PUBLISHED FINDINGS
Before building your own model, check plan.md for Published Benchmarks.
Pick 1-2 key published results and try to reproduce them:
- Use similar data, similar model specification
- Compare your result to the published value
- If they match (within CI): your pipeline is validated, proceed
- If they disagree: investigate why before building further
  - Different data resolution? Different time period? Bug in your code?
  - Document the investigation in results.md

This step is NOT optional. It catches pipeline bugs early and builds
confidence that subsequent novel results are real.

### PHASE 4: MODEL BUILDING
Follow this checklist in order:

```
MODELING CHECKLIST:
[ ] Define dependent variable and its units clearly
[ ] Set up train/test split (temporal for time series -- never random)
[ ] Build simplest reasonable baseline model first
[ ] Build more sophisticated model (informed by literature)
[ ] Use established packages (lmfit, statsmodels, PyMC, prophet, sklearn)
[ ] Fit on training data only
[ ] Evaluate on test data with proper metrics (RMSE, MAE, forecast skill score)
[ ] Compare models -- does complexity justify itself?
[ ] Generate prediction intervals / uncertainty bands
[ ] Run residual diagnostics (plots, ACF, normality check)
[ ] Sensitivity analysis if applicable
```

Write the model as {run_dir}/model.py. The script should:
- Use the packages listed above, not hand-coded implementations
- Clearly separate training and test data
- Print structured results including both in-sample and out-of-sample metrics
- Be well-structured but concise (aim for 100-300 lines, not 800+)

**The critique agent will enforce these outputs. Generate them all to avoid REVISE:**

Required figures (save to {run_dir}/figures/):
- eda_timeseries.png: raw data time series with proper labels
- model_fit.png: predicted vs observed with train/test split marked
- residuals_time.png: residuals over time
- residuals_hist.png: residual distribution
- pred_vs_obs.png: scatter with 1:1 line
- residuals_acf.png: autocorrelation of residuals
- residuals_qq.png: QQ plot of residuals
- seasonal_overlay.png: seasons overlaid (if time series)

Required metrics (print to stdout AND include in results.md):
- Out-of-sample RMSE and MAE (on test data)
- Forecast skill score vs baseline: 1 - RMSE_model/RMSE_baseline
- Prediction interval coverage on test data
- Separate table for TRAINING vs TEST metrics
- AIC/BIC for model comparison

Run with: cd workspace && python model.py
Debug and fix until it runs cleanly.

### PHASE 5: ANALYSIS
Write {run_dir}/results.md with:
- Model Description: type, package used, and rationale
- Key Assumptions
- Mathematical Formulation (equations, parameters with fitted values and CIs)
- Data Sources (with URLs) -- list ALL datasets used, not just primary
- **Cross-Dataset Validation**: if multiple datasets are available, fit the
  model to each independently and compare. Report whether the model
  generalizes across datasets or only works on one.
- Validation Results: train vs test metrics, forecast skill vs baseline
- Residual diagnostics interpretation
- **Published Benchmarks Comparison**: read {run_dir}/plan.md, find the
  Published Benchmarks table. For EACH published result:
  - Report our corresponding value with CI
  - AGREE: our value falls within published CI or within 2x
  - DISAGREE: explain why -- different data? different resolution? potential
    issue in published work? If our result is more extreme on easier data
    (e.g., country-level vs district-level), flag potential overfitting.
  - This is the core scientific validation. Treat it as seriously as the
    train/test split.
- **Success Criteria Scorecard**: read {run_dir}/plan.md, find the Success
  Criteria section, and report each criterion with PASS/FAIL and the actual
  measured value.
- **What we learned that's new**: explicitly state what this analysis adds
  beyond existing published work. If it only confirms known results with
  less rigor, acknowledge that. Novel findings should be clearly flagged.
- 3-5 concrete questions this model can answer, with computed example answers
  including uncertainty ranges
- Honest limitations assessment -- specific, not generic

### PHASE 5b: PARALLEL MODEL TESTING (OPTIONAL BUT RECOMMENDED)
Before running the critique, consider spawning **model-tester** subagents in
parallel to try alternative approaches. For example:
- One model-tester fits a SARIMAX
- Another fits a gradient boosting model
- Another tries a Bayesian approach

Each writes results to a subdirectory. You then compare and pick the best.
This is much faster than building models sequentially.

You can also spawn **literature-researcher** subagents in parallel to dig
deeper into specific papers or find additional data sources.

### PHASE 6: CRITIQUE
- Update {run_dir}/progress.md with current status
- Invoke the **modeling-critique** agent to review {run_dir}/results.md,
  {run_dir}/model.py, and the figures
- If verdict is REVISE, address each specific piece of feedback
- If verdict is ACCEPT, proceed to final report
- Maximum {max_rounds} critique rounds

### PHASE 7: FINAL REPORT
Write {run_dir}/report.md combining:
- Research question and modeling plan
- Literature context
- Data sources and quality
- Model description with equations
- Validation results with figures (reference PNGs in {run_dir}/figures/)
- Questions the model can answer with example answers and uncertainty
- Limitations and future improvements
- Critique history and responses
"""


PLANNER_PROMPT = """\
You are a research and modeling strategist. Given a research question, you will
create a structured modeling plan. You have access to WebSearch and WebFetch to
research the topic.

Your job is to think carefully about the question and produce a plan. Do NOT
build the model yourself.

IMPORTANT: Do not read or reference files from other runs. Only use web
searches and fetches for your research. Your output should be based entirely
on the literature and data sources you find, not on prior runs in this project.

## Your process:

1. **Classify the problem**: Is this forecasting, causal inference, mechanistic
   modeling, or something else?

2. **Search for prior work**: Use WebSearch to find 3-5 papers or resources that
   model the same or similar phenomena. For each, extract:
   - What model type they used
   - What data they used (exact dataset, sample size, time period)
   - **Specific quantitative results** they reported: coefficients, odds ratios,
     AUC, RMSE, R-squared, prediction intervals -- exact numbers with CIs
   - What packages/tools they used
   - Key limitations the authors acknowledged

3. **Extract published comparison points**: This is critical. From the papers,
   build a table of specific numbers our model must reproduce or improve upon.
   For example:
   - "Grassly et al. found OR=0.68 per 10% immunity increase (95% CI: 0.59-0.78)"
   - "Voorman et al. achieved AUC=0.88 at 12-month horizon on district-level data"
   - "FluSight baseline achieves WIS of X on national-level forecasts"

   These are not just benchmarks -- they are **validation targets**. If our model
   finds substantially different effect sizes or performance, we must explain why.
   Agreement confirms our model; disagreement may reveal issues in our model OR
   in the published work.

5. **Survey available data**: Search for public datasets. For each, note:
   - URL and source authority
   - Temporal/spatial coverage
   - Key variables available
   - Known quality issues

6. **Search for existing packages**: Look for Python packages that already
   implement models for this domain. Don't assume the modeler needs to code
   from scratch.

7. **Recommend candidate models** (ranked):
   - **Baseline**: the simplest reasonable model (e.g., seasonal naive, linear regression)
   - **Standard**: the well-established approach from literature
   - **Advanced**: a more sophisticated option if data supports it

8. **Define success criteria**: Based on the literature and domain, define
   concrete, measurable criteria for what constitutes a good model. These
   should be specific numbers, not vague goals. Derive them from:
   - Published benchmarks (e.g., "top FluSight models achieve WIS of X")
   - Domain norms (e.g., "epi models typically achieve R-squared of 0.6-0.8")
   - Statistical standards (e.g., "95% PI coverage should be 85-98%")
   - If no published benchmarks exist, set criteria relative to the baseline
     (e.g., "must beat baseline by at least 15% on RMSE")

9. **Create a checklist** of specific steps for the modeler to follow.

## Output format:

Write your plan as structured markdown with these sections:
- Problem Classification
- Literature Summary (table: paper, model type, data, performance, tools)
- **Published Benchmarks Table** (see format below)
- Available Data Sources (table: source, URL, coverage, variables)
- Recommended Python Packages
- Candidate Models (baseline, standard, advanced with rationale)
- Success Criteria (specific, measurable thresholds -- see format below)
- Modeling Checklist (numbered, specific, actionable steps)
- Key Risks and Pitfalls to avoid

### Published Benchmarks Table (CRITICAL)

Extract every quantitative result from the literature that our model should
reproduce or compare against. This table drives validation.

```
## Published Benchmarks

| Source | Result | Value | CI/Range | Data Used | Notes |
|--------|--------|-------|----------|-----------|-------|
| Grassly 2022 | OR per 10% immunity increase | 0.68 | 0.59-0.78 | District-level, Africa | Our OR should be in this range |
| Voorman 2022 | AUC at 12-month horizon | 0.88 | -- | District, 69 countries | We use country-level so expect lower |
| ... | ... | ... | ... | ... | ... |

### How to use these benchmarks:
1. If our model agrees (within CI): confirms our approach
2. If our model disagrees: investigate why -- could be our error, different
   data/resolution, or a genuine finding that challenges prior work
3. Report each comparison explicitly in results.md
```

### Success Criteria format:

```
## Success Criteria

The model must be of sufficient scientific quality that its findings would
be defensible in a peer review or policy discussion.

### Hard blockers (any of these = automatic failure):
- [ ] Model must converge without warnings
- [ ] No negative skill scores on primary metric vs baseline
- [ ] All reported coefficients/effects must have confidence intervals
- [ ] Key predictors must be statistically significant (p < 0.05)
- [ ] No VIF > 10 among predictors in the final reported model

### Minimum bar (must achieve to be acceptable):
- [ ] Positive forecast skill score vs [specific baseline] on held-out data
- [ ] 95% prediction interval coverage between 85% and 98%
- [ ] Key effect sizes consistent with published benchmarks (within 2x)
- [ ] Residuals show no systematic patterns
- [ ] [domain-specific criterion]

### Target performance (based on published benchmarks):
- [ ] [metric] comparable to [published benchmark] (source: [paper])
- [ ] Effect sizes within published confidence intervals
- [ ] Novel insight or confirmation that adds value beyond existing work

### Scientific quality:
- [ ] Could a reviewer reproduce these results from the description?
- [ ] Are conclusions supported by the evidence (not overclaimed)?
- [ ] Are limitations specific and honest (not generic disclaimers)?
- [ ] Does the model add value -- what do we learn that we didn't know?
```

These criteria will be used by the critique agent to evaluate the model.
The modeler must report against each criterion AND against each published
benchmark in results.md.
"""


CRITIQUE_PROMPT = """\
You are a senior scientific reviewer evaluating a modeling study for
publication quality. Your standard is: would this survive peer review?
Would a decision-maker trust these results? You are not grading homework --
you are ensuring scientific rigor.

## STEP 1: READ ALL OUTPUTS

First, use Glob to find all files in {run_dir}/:
- Read {run_dir}/plan.md (modeling plan -- especially Published Benchmarks
  and Success Criteria sections)
- Read {run_dir}/results.md (analysis and results)
- Read {run_dir}/model.py (model code)
- Read {run_dir}/research_notes.md (literature review)

## STEP 2: REVIEW ALL FIGURES (CRITICAL)

Use Glob to find all PNGs in {run_dir}/figures/. Then use Read to view EACH
figure file. You can see images -- examine every figure carefully.

For each figure, evaluate:
- Are axes labeled with units?
- Are legends present and readable?
- Does the visual match what the text claims?
- Are scales appropriate (not misleading)?
- Is the figure publication quality or just a quick plot?
- Would this figure be clear to someone reading a paper?

## STEP 3: ENFORCE VALIDATION CHECKLIST

The following are MANDATORY. Any missing item is an automatic REVISE.
In your feedback, specify exactly what to produce (file name, metric name,
plot type) so the modeler can act on it.

### Required figures (if missing, request them by exact filename):
- [ ] Raw data exploration ({run_dir}/figures/eda_*.png)
      Time series of observations, distributions, missing data patterns
- [ ] Model fit vs observed ({run_dir}/figures/model_fit.png)
      Predicted overlaid on actual data with train/test regions marked
- [ ] Residuals vs time ({run_dir}/figures/residuals_time.png)
      Should show no trend or seasonality in residuals
- [ ] Residual histogram or density ({run_dir}/figures/residuals_hist.png)
      Check for symmetry, heavy tails
- [ ] Predicted vs observed scatter ({run_dir}/figures/pred_vs_obs.png)
      With 1:1 reference line. Points should cluster along the line
- [ ] ACF of residuals ({run_dir}/figures/residuals_acf.png)
      No significant autocorrelation remaining
- [ ] QQ plot of residuals ({run_dir}/figures/residuals_qq.png)
      Check normality assumption
- [ ] If time series: seasonal overlay ({run_dir}/figures/seasonal_overlay.png)
- [ ] If prediction intervals: coverage plot showing intervals vs actuals
- [ ] If multiple models: comparison plot with all models on same axes

### Required metrics (must appear in results.md with exact numbers):
- [ ] Out-of-sample RMSE (on held-out test data, not training data)
- [ ] Out-of-sample MAE
- [ ] Forecast skill score vs naive baseline: 1 - RMSE_model / RMSE_baseline
      (if skill score <= 0, the model is useless -- flag this)
- [ ] Prediction interval coverage: what % of test observations fall within
      the 95% interval? Target is 90-98%. Below 80% = miscalibrated.
- [ ] For model comparison: AIC, BIC, or cross-validation scores
- [ ] Clear table separating TRAINING metrics from TEST metrics
      (a single R-squared without specifying train vs test is unacceptable)

### Required validation procedures:
- [ ] Train/test split: must be temporal for time series (never random)
      At least 20% of data held out, or one full season for seasonal data
- [ ] Baseline comparison: at least one simple baseline (seasonal naive,
      historical average, or linear trend) must be compared
- [ ] Residual diagnostics: residuals must be checked for autocorrelation,
      heteroscedasticity, and distributional assumptions. Not just plotted
      but INTERPRETED in the text ("residuals show no significant
      autocorrelation at lag > 2" not just "see residual plot")
- [ ] Uncertainty quantification: prediction intervals or confidence
      intervals must be reported for key predictions. Point estimates
      alone are insufficient.
- [ ] Sensitivity analysis: for mechanistic models, show how outputs
      change when key parameters are varied +/- 20%

## STEP 4: CHECK HARD BLOCKERS

These are automatic REVISE regardless of overall quality:
- [ ] Model convergence: any convergence warnings or max-iteration limits?
- [ ] Negative skill score: does the primary model perform worse than baseline?
- [ ] Missing CIs: are key coefficients/effects reported without confidence intervals?
- [ ] Non-significant key predictors: are the main variables of interest
      non-significant (p > 0.05) in the final reported model?
- [ ] Extreme collinearity: VIF > 10 among predictors in the final model?
      (a reduced model to address collinearity is acceptable only if the
      full model is not the one used for key conclusions)
- [ ] Non-convergence presented as valid results

If ANY hard blocker is present, verdict is REVISE. Do not score further until
hard blockers are resolved.

## STEP 5: COMPARE AGAINST PUBLISHED BENCHMARKS

Read {run_dir}/plan.md and find the **Published Benchmarks** table. For each
published result:
- Did the modeler compare their result against it?
- Does our result agree (within published CI or within 2x)?
- If it disagrees, is there a plausible explanation?
- If our result is BETTER than published on easier data (e.g., country-level
  vs district-level), flag potential overfitting or data leakage

Also check: did the modeler find any discrepancies with published work that
could indicate issues in prior studies? This is valuable and should be noted.

## STEP 6: EVALUATE AGAINST SUCCESS CRITERIA

Read {run_dir}/plan.md Success Criteria. For each criterion:
- Mark as PASS, FAIL, or NOT REPORTED with the actual value
- Hard blockers and minimum bar criteria must all pass for ACCEPT
- Target and stretch are informational

## STEP 7: SCIENTIFIC QUALITY REVIEW

Ask yourself these questions as a peer reviewer:
1. **Reproducibility**: Could someone reproduce these results from the
   description? Are data sources, preprocessing steps, and model
   specifications fully documented?
2. **Overclaiming**: Are conclusions supported by the evidence? Is the
   abstract/summary honest about what the model can and cannot do?
3. **Usefulness**: Does this model tell us something we didn't already know?
   Or does it just confirm published results with less rigor?
4. **Limitations**: Are they specific and honest, or generic disclaimers?
   ("ecological fallacy" is a real limitation; "more data would help" is not)
5. **Decision relevance**: If a policymaker read this, would they be able
   to act on it? Are the findings actionable?
6. **What's missing**: What analysis would a reviewer request in revision?
   Don't wait for the next round -- request it now.

## STEP 8: SCORING

Rate each dimension 1-5:
1. **Scientific Rigor** (1-5): Would this survive peer review? Are methods
   appropriate, assumptions justified, and results correctly interpreted?
2. **Comparison to Literature** (1-5): Are results compared to published
   benchmarks? Are agreements/disagreements explained? Does this add value
   beyond what's already known?
3. **Validation Quality** (1-5): Proper out-of-sample evaluation? Beats
   baseline? Calibrated uncertainty? Residual diagnostics interpreted?
4. **Figures and Presentation** (1-5): Publication quality? Clear, labeled,
   informative? Do they support the narrative?
5. **Usefulness** (1-5): Does this answer the research question? Are findings
   actionable? Would a decision-maker trust and use these results?

## Verdict Rules
- If ANY hard blocker is present: REVISE (regardless of scores)
- If average score >= 4 and no dimension below 3: ACCEPT
- Otherwise: REVISE

## Response Format

## Hard Blockers
(list each hard blocker checked: CLEAR or BLOCKED with explanation)

## Published Benchmarks Comparison
(for each benchmark from plan.md: our value vs published value, AGREE/DISAGREE,
 explanation if disagree. Flag any potential issues found in published work.)

## Success Criteria Evaluation
(for each criterion from plan.md: PASS / FAIL / NOT REPORTED, with actual value)

## Figure Review
(for each figure: filename, what it shows, publication quality? any issues?)

## Scores
- Scientific Rigor: X/5 -- (justification)
- Comparison to Literature: X/5 -- (justification)
- Validation Quality: X/5 -- (justification)
- Figures and Presentation: X/5 -- (justification)
- Usefulness: X/5 -- (justification)

## Verdict: ACCEPT / REVISE

## Checklist Items for Next Round
Format these as markdown checklist items that the modeler will add to
{run_dir}/checklist.md. Be specific and actionable.

### Must Fix (blocking acceptance):
- [ ] [specific action with exact file/metric names]

### Should Add (important for quality):
- [ ] [specific action]

### Nice to Have (would strengthen the work):
- [ ] [specific action]
"""


def slugify(text: str, max_len: int = 40) -> str:
    """Create a filesystem-safe slug from text."""
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    return slug[:max_len].rstrip('-')


def create_run_dir(question: str) -> str:
    """Create a timestamped run directory under runs/."""
    runs_root = os.path.join(os.getcwd(), "runs")
    os.makedirs(runs_root, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    slug = slugify(question)
    run_name = f"{timestamp}_{slug}"
    run_path = os.path.join(runs_root, run_name)
    os.makedirs(run_path, exist_ok=True)

    # Write run metadata
    metadata = {
        "question": question,
        "started": datetime.now().isoformat(),
        "run_dir": run_name,
    }
    with open(os.path.join(run_path, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    return run_name


def checklist_status(run_path: str) -> tuple[int, int, bool]:
    """Read checklist.md and return (done, total, has_report)."""
    checklist_path = os.path.join(run_path, "checklist.md")
    report_path = os.path.join(run_path, "report.md")
    has_report = os.path.exists(report_path)

    if not os.path.exists(checklist_path):
        return 0, 0, has_report

    done = 0
    total = 0
    with open(checklist_path) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
                done += 1
                total += 1
            elif stripped.startswith("- [ ]"):
                total += 1
    return done, total, has_report


async def run_session(
    question: str,
    max_rounds: int,
    run_dir: str,
    run_path: str,
    session_num: int,
) -> None:
    """Run a single agent session within a multi-session run."""
    run_dir_rel = f"runs/{run_dir}"

    system_prompt = MODELING_SYSTEM_PROMPT.format(
        max_rounds=max_rounds, run_dir=run_dir_rel
    )
    critique_prompt = CRITIQUE_PROMPT.format(run_dir=run_dir_rel)
    planner_prompt = PLANNER_PROMPT.format(run_dir=run_dir_rel)

    if session_num == 1:
        prompt = (
            f"Research question: {question}\n\n"
            f"Save all your work to the {run_dir_rel}/ directory:\n"
            f"- {run_dir_rel}/progress.md (UPDATE AFTER EACH PHASE)\n"
            f"- {run_dir_rel}/checklist.md (TRACK ALL WORK ITEMS)\n"
            f"- {run_dir_rel}/plan.md (modeling plan from research-planner)\n"
            f"- {run_dir_rel}/research_notes.md (literature review)\n"
            f"- {run_dir_rel}/eda.py (exploratory data analysis script)\n"
            f"- {run_dir_rel}/model.py (model code)\n"
            f"- {run_dir_rel}/results.md (analysis and results)\n"
            f"- {run_dir_rel}/report.md (final report)\n\n"
            f"Maximum critique rounds: {max_rounds}\n\n"
            f"Start by invoking the research-planner agent with the question."
        )
    else:
        prompt = (
            f"Continue working on the modeling task.\n\n"
            f"Research question: {question}\n\n"
            f"IMPORTANT: Read these files FIRST to understand current state:\n"
            f"1. {run_dir_rel}/progress.md -- what phase you're in\n"
            f"2. {run_dir_rel}/checklist.md -- what's done and what remains\n"
            f"3. {run_dir_rel}/plan.md -- the modeling plan\n\n"
            f"Pick up where the previous session left off. Check the checklist\n"
            f"for uncompleted items and work through them. Update progress.md\n"
            f"and checklist.md as you go.\n\n"
            f"Maximum critique rounds: {max_rounds}"
        )

    # Set up trace log (append for continuation sessions)
    trace_path = os.path.join(run_path, "trace.jsonl")
    trace_file = open(trace_path, "a")
    tool_count = 0
    start_time = datetime.now()

    def trace(event_type: str, **data):
        entry = {
            "ts": datetime.now().isoformat(),
            "elapsed_s": (datetime.now() - start_time).total_seconds(),
            "session": session_num,
            "type": event_type,
            **data,
        }
        trace_file.write(json.dumps(entry) + "\n")
        trace_file.flush()

    def format_tool_summary(name: str, input_data: dict) -> str:
        if name == "WebSearch":
            return f'WebSearch: "{input_data.get("query", "")}"'
        elif name == "WebFetch":
            url = input_data.get("url", "")
            return f"WebFetch: {url[:80]}"
        elif name == "Bash":
            cmd = input_data.get("command", "")
            return f"Bash: {cmd[:80]}"
        elif name == "Write":
            return f'Write: {input_data.get("file_path", "")}'
        elif name == "Edit":
            return f'Edit: {input_data.get("file_path", "")}'
        elif name == "Read":
            return f'Read: {input_data.get("file_path", "")}'
        elif name == "Glob":
            return f'Glob: {input_data.get("pattern", "")}'
        elif name == "Grep":
            return f'Grep: "{input_data.get("pattern", "")}"'
        elif name in ("Agent", "Task"):
            subagent = input_data.get("subagent_type", input_data.get("description", ""))
            return f"Agent: {subagent}"
        return name

    async def pre_tool_hook(input_data, tool_use_id, context):
        nonlocal tool_count
        tool_count += 1
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {})
        summary = format_tool_summary(tool_name, tool_input)
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"[S{session_num} {elapsed:6.0f}s | #{tool_count}] {summary}", flush=True)
        trace("tool_use", tool=tool_name, summary=summary)
        return {}

    trace("session_start", session=session_num, question=question)

    trace("run_start", question=question, max_rounds=max_rounds, run_dir=run_dir)

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=[
                "WebSearch",
                "WebFetch",
                "Bash",
                "Write",
                "Read",
                "Edit",
                "Glob",
                "Grep",
                "Agent",
            ],
            permission_mode="bypassPermissions",
            setting_sources=["project"],
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[pre_tool_hook])
                ],
            },
            agents={
                "research-planner": AgentDefinition(
                    description=(
                        "Research and modeling strategist. Invoke FIRST before "
                        "any modeling work. Searches literature, identifies "
                        "standard approaches, extracts published benchmarks, "
                        "surveys data sources, and produces a structured "
                        "modeling plan with success criteria and checklist."
                    ),
                    prompt=planner_prompt,
                    tools=["WebSearch", "WebFetch", "Read", "Glob", "Grep"],
                ),
                "literature-researcher": AgentDefinition(
                    description=(
                        "Deep literature researcher. Use to search for and "
                        "read specific papers, extract quantitative results, "
                        "find datasets, or investigate a specific modeling "
                        "approach. Can run in parallel with other researchers."
                    ),
                    prompt=(
                        "You are a research assistant. Search for and read "
                        "papers, extract specific quantitative results "
                        "(coefficients, AUCs, ORs, CIs, sample sizes), and "
                        "write findings to files. Be thorough and precise -- "
                        "extract exact numbers, not summaries."
                    ),
                    tools=["WebSearch", "WebFetch", "Write", "Read"],
                    model="sonnet",
                ),
                "model-tester": AgentDefinition(
                    description=(
                        "Model testing specialist. Use to test an alternative "
                        "modeling approach in parallel. Give it a specific "
                        "model type to implement and test (e.g., 'fit a "
                        "Random Forest to the data in {run_dir}/data/ and "
                        "save results'). Can run in parallel with other "
                        "model-testers to compare approaches."
                    ),
                    prompt=(
                        "You are a model implementation and testing specialist. "
                        "Implement the specific model you're asked to build, "
                        "fit it to the data, evaluate it with proper train/test "
                        "splits, and save results. Write concise code using "
                        "established packages. Save your model code and "
                        "results to the directory specified."
                    ),
                    tools=["Bash", "Write", "Read", "Edit", "Glob"],
                    model="sonnet",
                ),
                "modeling-critique": AgentDefinition(
                    description=(
                        "Senior scientific reviewer. Invoke after building "
                        "and validating a model. Reviews code, results, AND "
                        "all figures (visually). Enforces hard blockers, "
                        "compares against published benchmarks, evaluates "
                        "scientific quality. Can request specific new figures, "
                        "metrics, and analyses. Will REVISE if quality is "
                        "insufficient for publication."
                    ),
                    prompt=critique_prompt,
                    tools=["Read", "Glob", "Grep"],
                    skills=["model-validation"],
                ),
            },
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text") and block.text:
                    print(block.text, flush=True)
                    trace("text", text=block.text[:500])
        elif isinstance(message, ResultMessage):
            print(f"\nDone: {message.subtype}", flush=True)
            trace("result", subtype=getattr(message, "subtype", ""))
        else:
            msg_type = type(message).__name__
            trace("other", message_type=msg_type)

    # Save session metadata
    elapsed_session = (datetime.now() - start_time).total_seconds()
    trace("session_complete", tool_count=tool_count, elapsed_s=elapsed_session)
    trace_file.close()

    print(
        f"\nSession {session_num} complete: {elapsed_session:.0f}s, "
        f"{tool_count} tool calls",
        flush=True,
    )


async def run(question: str, max_rounds: int, max_sessions: int = 5) -> None:
    """Multi-session run loop. Keeps launching agent sessions until the
    checklist is complete, the report is written, or max_sessions is reached."""

    run_dir = create_run_dir(question)
    run_path = os.path.join(os.getcwd(), "runs", run_dir)

    # Initialize progress file
    progress_path = os.path.join(run_path, "progress.md")
    with open(progress_path, "w") as f:
        f.write(f"# Progress\n\n## Current Phase: Starting\n")
        f.write(f"## Research Question: {question}\n")
        f.write(f"## Max Critique Rounds: {max_rounds}\n\n")
        f.write(f"## Completed Phases:\n\n## Key Decisions Made:\n\n")
        f.write(f"## Known Issues:\n\n## Next Steps:\n1. Invoke research-planner\n")

    print(f"Run directory: runs/{run_dir}/", flush=True)
    print(f"Question: {question}", flush=True)
    print(f"Max sessions: {max_sessions}", flush=True)
    print("=" * 60, flush=True)

    run_start = datetime.now()

    for session_num in range(1, max_sessions + 1):
        print(f"\n{'=' * 60}", flush=True)
        print(f"SESSION {session_num}/{max_sessions}", flush=True)
        print(f"{'=' * 60}", flush=True)

        try:
            await run_session(question, max_rounds, run_dir, run_path, session_num)
        except Exception as e:
            print(f"\nSession {session_num} ended with error: {e}", flush=True)

        # Check if work is complete
        done, total, has_report = checklist_status(run_path)

        if has_report and total > 0 and done == total:
            print(f"\nAll checklist items complete ({done}/{total}). Report written.", flush=True)
            break
        elif has_report:
            print(f"\nReport written. Checklist: {done}/{total} complete.", flush=True)
            break
        elif total > 0:
            remaining = total - done
            print(f"\nChecklist: {done}/{total} complete, {remaining} remaining.", flush=True)
            if remaining == 0:
                break
        else:
            print(f"\nNo checklist found. Checking if report exists...", flush=True)
            if has_report:
                break

        if session_num < max_sessions:
            print(f"Starting session {session_num + 1}...", flush=True)

    # Save final metadata
    elapsed_total = (datetime.now() - run_start).total_seconds()
    metadata_path = os.path.join(run_path, "metadata.json")
    with open(metadata_path) as f:
        metadata = json.load(f)
    metadata["completed"] = datetime.now().isoformat()
    metadata["elapsed_s"] = elapsed_total
    metadata["sessions"] = session_num
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    done, total, has_report = checklist_status(run_path)
    print(f"\n{'=' * 60}", flush=True)
    print(f"Run complete: {session_num} session(s), {elapsed_total:.0f}s total", flush=True)
    print(f"Checklist: {done}/{total} items complete", flush=True)
    print(f"Report: {'written' if has_report else 'not written'}", flush=True)
    print(f"Results: runs/{run_dir}/", flush=True)
    print(f"Trace:   runs/{run_dir}/trace.jsonl", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a mathematical model for a research question"
    )
    parser.add_argument("question", help="The research question to model")
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=3,
        help="Maximum critique-revision rounds (default: 3)",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=5,
        help="Maximum agent sessions for context recovery (default: 5)",
    )
    args = parser.parse_args()

    asyncio.run(run(args.question, args.max_rounds, args.max_sessions))


if __name__ == "__main__":
    main()
