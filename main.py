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
)


MODELING_SYSTEM_PROMPT = """\
You are a mathematical modeling expert. Given a research question, you will build
a quantitative model through a structured, disciplined workflow.

## CORE PRINCIPLES

1. **Don't reinvent the wheel.** Use established Python packages (see Framework Guide
   below) instead of hand-coding models. A 50-line script using statsmodels or lmfit
   beats an 800-line hand-rolled implementation.
2. **Start simple, add complexity.** Always build the simplest reasonable model first
   as a baseline. Only add complexity if it demonstrably improves out-of-sample
   performance.
3. **Validate before you trust.** Never report in-sample fit as model quality. Always
   use held-out data (temporal split for time series).
4. **Quantify uncertainty.** Report prediction intervals, not just point estimates.
   Use lmfit or PyMC which provide uncertainty automatically.

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
- Find and download public datasets (CSV, JSON from government/research sources)
- Download using Python via Bash, save to {run_dir}/data/
- If no direct data available, use published parameter values from literature
- Create {run_dir}/data/ directory if needed

### PHASE 3: DATA EXPLORATION (do this before modeling!)
Write and run a short EDA script ({run_dir}/eda.py) that:
- Prints summary statistics
- Checks for missing values, outliers, reporting changes
- Plots raw data with proper labels and units
- Checks distributions, seasonality, trends
- Saves EDA plots to {run_dir}/figures/
- Prints findings to stdout

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
- Data Sources (with URLs)
- Validation Results: train vs test metrics, forecast skill vs baseline
- Residual diagnostics interpretation
- 3-5 concrete questions this model can answer, with computed example answers
  including uncertainty ranges
- Honest limitations assessment

### PHASE 6: CRITIQUE
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

## Your process:

1. **Classify the problem**: Is this forecasting, causal inference, mechanistic
   modeling, or something else?

2. **Search for prior work**: Use WebSearch to find 3-5 papers or resources that
   model the same or similar phenomena. For each, note:
   - What model type they used
   - What data they used
   - What performance they achieved
   - What packages/tools they used

3. **Identify the standard approach**: What is the "textbook" model for this
   problem? What's the state-of-the-art?

4. **Survey available data**: Search for public datasets. For each, note:
   - URL and source authority
   - Temporal/spatial coverage
   - Key variables available
   - Known quality issues

5. **Search for existing packages**: Look for Python packages that already
   implement models for this domain. Don't assume the modeler needs to code
   from scratch.

6. **Recommend candidate models** (ranked):
   - **Baseline**: the simplest reasonable model (e.g., seasonal naive, linear regression)
   - **Standard**: the well-established approach from literature
   - **Advanced**: a more sophisticated option if data supports it

7. **Create a checklist** of specific steps for the modeler to follow.

## Output format:

Write your plan as structured markdown with these sections:
- Problem Classification
- Literature Summary (table: paper, model type, data, performance, tools)
- Available Data Sources (table: source, URL, coverage, variables)
- Recommended Python Packages
- Candidate Models (baseline, standard, advanced with rationale)
- Modeling Checklist (numbered, specific, actionable steps)
- Key Risks and Pitfalls to avoid
"""


CRITIQUE_PROMPT = """\
You are a rigorous mathematical modeling reviewer. You will review ALL outputs
from the modeling agent: text files, code, AND figures.

## STEP 1: READ ALL OUTPUTS

First, use Glob to find all files in {run_dir}/:
- Read {run_dir}/results.md (analysis and results)
- Read {run_dir}/model.py (model code)
- Read {run_dir}/research_notes.md (literature review)
- Read {run_dir}/plan.md (modeling plan, if it exists)

## STEP 2: REVIEW ALL FIGURES (CRITICAL)

Use Glob to find all PNGs in {run_dir}/figures/. Then use Read to view EACH
figure file. You can see images -- examine every figure carefully.

For each figure, evaluate:
- Are axes labeled with units?
- Are legends present and readable?
- Does the visual match what the text claims?
- Are scales appropriate (not misleading)?
- Is the figure informative or just decorative?

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

## STEP 4: DOMAIN-SPECIFIC REVIEW

Check the model against domain knowledge:
- Are parameter values physiologically/physically plausible?
- Do the results align with published literature?
- Are there known phenomena the model fails to capture?
- Would a domain expert find the conclusions reasonable?

## STEP 5: SCORING

Rate each dimension 1-5:
1. **Mathematical Rigor** (1-5): Equations correct? Assumptions reasonable?
   Established packages used? Parameters plausible?
2. **Data Quality** (1-5): Sources credible? Proper train/test split?
   Data issues acknowledged?
3. **Model Validity** (1-5): Addresses the question? Out-of-sample validation?
   Beats baseline? Prediction intervals included?
4. **Code Quality** (1-5): Concise and well-structured? Uses established
   packages? Runs correctly? Not over-engineered?
5. **Figures and Presentation** (1-5): All required figures present? Axes
   labeled with units? Visually clear? Figures match text claims?
6. **Completeness** (1-5): All required metrics reported? Limitations honest
   and specific? Questions demonstrated with uncertainty?

## Verdict Rules
- ACCEPT if average score >= 4 and no dimension below 3
- REVISE otherwise

## Response Format

## Required Outputs Check
(list each required item with PRESENT or MISSING)

## Figure Review
(for each figure: filename, what it shows, any issues found)

## Scores
- Mathematical Rigor: X/5 -- (justification)
- Data Quality: X/5 -- (justification)
- Model Validity: X/5 -- (justification)
- Code Quality: X/5 -- (justification)
- Figures and Presentation: X/5 -- (justification)
- Completeness: X/5 -- (justification)

## Verdict: ACCEPT / REVISE

## Required Work
(things that MUST be done -- missing required outputs, broken code, wrong results)

## Requested Figures
(specific new figures to generate, with exact descriptions:
 e.g., "Generate a QQ plot of model residuals saved as {run_dir}/figures/qq_residuals.png")

## Requested Metrics
(specific metrics to compute and add to results.md:
 e.g., "Compute forecast skill score: 1 - RMSE_model/RMSE_naive_seasonal")

## Requested Analyses
(deeper analyses to perform:
 e.g., "Run sensitivity analysis varying R0 from 1.0 to 1.5 and plot
  how peak hospitalization predictions change")

## Other Improvements
(nice-to-haves that would strengthen the work but are not blocking acceptance)
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


async def run(question: str, max_rounds: int) -> None:
    run_dir = create_run_dir(question)
    run_path = os.path.join(os.getcwd(), "runs", run_dir)

    print(f"Run directory: runs/{run_dir}/", flush=True)
    print(f"Question: {question}", flush=True)
    print("=" * 60, flush=True)

    system_prompt = MODELING_SYSTEM_PROMPT.format(
        max_rounds=max_rounds, run_dir=f"runs/{run_dir}"
    )
    critique_prompt = CRITIQUE_PROMPT.format(run_dir=f"runs/{run_dir}")
    planner_prompt = PLANNER_PROMPT.format(run_dir=f"runs/{run_dir}")

    prompt = (
        f"Research question: {question}\n\n"
        f"Save all your work to the runs/{run_dir}/ directory:\n"
        f"- runs/{run_dir}/plan.md (modeling plan from research-planner)\n"
        f"- runs/{run_dir}/research_notes.md (literature review)\n"
        f"- runs/{run_dir}/eda.py (exploratory data analysis script)\n"
        f"- runs/{run_dir}/model.py (model code)\n"
        f"- runs/{run_dir}/results.md (analysis and results)\n"
        f"- runs/{run_dir}/report.md (final report)\n\n"
        f"Maximum critique rounds: {max_rounds}\n\n"
        f"Start by invoking the research-planner agent with the question."
    )

    # Set up trace log
    trace_path = os.path.join(run_path, "trace.jsonl")
    trace_file = open(trace_path, "w")
    tool_count = 0
    start_time = datetime.now()

    def trace(event_type: str, **data):
        """Write a trace event to the JSONL log."""
        entry = {
            "ts": datetime.now().isoformat(),
            "elapsed_s": (datetime.now() - start_time).total_seconds(),
            "type": event_type,
            **data,
        }
        trace_file.write(json.dumps(entry) + "\n")
        trace_file.flush()

    def format_tool_summary(name: str, input_data: dict) -> str:
        """Create a human-readable one-line summary of a tool call."""
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
            permission_mode="acceptEdits",
            agents={
                "research-planner": AgentDefinition(
                    description=(
                        "Research and modeling strategist. Invoke FIRST before "
                        "any modeling work. Searches literature, identifies "
                        "standard approaches, surveys data sources, and "
                        "produces a structured modeling plan with checklist."
                    ),
                    prompt=planner_prompt,
                    tools=["WebSearch", "WebFetch", "Read", "Glob", "Grep"],
                ),
                "modeling-critique": AgentDefinition(
                    description=(
                        "Mathematical modeling critic. Invoke after building "
                        "and validating a model. Reviews code, results, AND "
                        "all figures (visually). Enforces validation checklists "
                        "and can request specific new figures, metrics, and "
                        "analyses. Will REVISE if required outputs are missing."
                    ),
                    prompt=critique_prompt,
                    tools=["Read", "Glob", "Grep"],
                    skills=["model-validation"],
                ),
            },
        ),
    ):
        if isinstance(message, AssistantMessage):
            parent = getattr(message, "parent_tool_use_id", None)
            ctx = " (subagent)" if parent else ""

            for block in message.content:
                if hasattr(block, "text") and block.text:
                    print(block.text, flush=True)
                    trace("text", text=block.text[:500], context=ctx.strip())
                elif hasattr(block, "name"):
                    input_data = getattr(block, "input", {}) or {}
                    summary = format_tool_summary(block.name, input_data)
                    tool_count += 1
                    elapsed = (datetime.now() - start_time).total_seconds()
                    print(
                        f"[{elapsed:6.0f}s | #{tool_count}]{ctx} {summary}",
                        flush=True,
                    )
                    trace(
                        "tool_use",
                        tool=block.name,
                        summary=summary,
                        input_preview={
                            k: str(v)[:200] for k, v in input_data.items()
                        },
                        context=ctx.strip(),
                    )
        elif isinstance(message, ResultMessage):
            print(f"\nDone: {message.subtype}", flush=True)
            trace("result", subtype=getattr(message, "subtype", ""))
        else:
            # Log other message types for debugging
            msg_type = type(message).__name__
            trace("other", message_type=msg_type)

    # Save completion metadata
    elapsed_total = (datetime.now() - start_time).total_seconds()
    trace("run_complete", tool_count=tool_count, elapsed_s=elapsed_total)
    trace_file.close()

    metadata_path = os.path.join(run_path, "metadata.json")
    with open(metadata_path) as f:
        metadata = json.load(f)
    metadata["completed"] = datetime.now().isoformat()
    metadata["elapsed_s"] = elapsed_total
    metadata["tool_count"] = tool_count
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"\n{'=' * 60}", flush=True)
    print(f"Run complete in {elapsed_total:.0f}s ({tool_count} tool calls)", flush=True)
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
    args = parser.parse_args()

    asyncio.run(run(args.question, args.max_rounds))


if __name__ == "__main__":
    main()
