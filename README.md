# modeling-agentic

A multi-agent system that builds mathematical models for research questions. Give it a question, and it will research the topic, gather data, build and validate a model, and produce a report — with a separate critique agent enforcing rigorous validation standards.

Built on the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview).

## How it works

```
You ask a question
    ↓
Research Planner (subagent)
    Searches literature, surveys data sources,
    recommends model types, creates a checklist
    ↓
Modeling Agent
    Downloads data, explores it, builds models
    using established packages (statsmodels, lmfit,
    scikit-learn, Prophet), generates figures
    ↓
Critique Agent (subagent)
    Reads ALL outputs including figures (multimodal).
    Enforces validation checklist: train/test split,
    baseline comparison, residual diagnostics,
    uncertainty quantification, forecast skill scores.
    Can request specific new figures and metrics.
    ↓
    REVISE? → back to modeling with specific feedback
    ACCEPT? → final report
```

## Quick start

```bash
# Install
pip install -r requirements.txt

# Set your API key
export ANTHROPIC_API_KEY=your-key

# Run
python main.py "How does CO2 concentration affect global temperature?"
```

## Usage

```bash
python main.py "your research question here"
python main.py "predict malaria incidence in sub-Saharan Africa" --max-rounds 5
```

**Options:**
- `--max-rounds N` — Maximum critique-revision rounds (default: 3)

## What it produces

Each run creates a timestamped directory under `runs/`:

```
runs/2026-04-06_1430_how-does-co2-concentration/
├── metadata.json       # Question, timestamps, tool count
├── trace.jsonl         # Full structured trace of every tool call
├── plan.md             # Research planner's modeling strategy
├── research_notes.md   # Literature review
├── eda.py              # Exploratory data analysis script
├── model.py            # Model implementation
├── results.md          # Analysis with metrics and validation
├── report.md           # Final report with everything
├── data/               # Downloaded datasets
└── figures/            # Generated plots
    ├── eda_timeseries.png
    ├── model_fit.png
    ├── pred_vs_obs.png
    ├── residuals_time.png
    ├── residuals_hist.png
    ├── residuals_acf.png
    ├── residuals_qq.png
    └── seasonal_overlay.png
```

## Tracing

Every run produces a `trace.jsonl` with structured logs:

```json
{"ts": "2026-04-06T14:30:15", "elapsed_s": 12.3, "type": "tool_use", "tool": "WebSearch", "summary": "WebSearch: \"SEIR influenza model Python\""}
```

Console output shows real-time progress:

```
[   12s | #1]          Agent: research-planner
[   15s | #2] (subagent) WebSearch: "SEIR model influenza hospitalization"
[   25s | #5]          Write: runs/.../model.py
[   30s | #6]          Bash: cd runs/... && python model.py
```

## Architecture

Three agents orchestrated in a single `query()` call:

| Agent | Role | Tools |
|-------|------|-------|
| **Main (modeler)** | Researches, builds models, writes reports | WebSearch, WebFetch, Bash, Read, Write, Edit, Glob, Grep, Agent |
| **Research planner** | Creates modeling strategy before any code | WebSearch, WebFetch, Read, Glob, Grep |
| **Critique** | Reviews all outputs including figures; enforces validation checklists | Read, Glob, Grep |

The critique agent enforces:
- Train/test split (temporal for time series)
- Baseline model comparison with forecast skill score
- Residual diagnostics (ACF, QQ plot, histogram)
- Prediction interval coverage
- Uncertainty quantification on all key predictions
- Proper use of established packages (not hand-rolled implementations)

## Skills

Two skills in `.claude/skills/` guide the agents:

- **modeling-fundamentals** — Model selection decision tree, Python framework guide (lmfit, statsmodels, Prophet, scikit-learn, PyMC), common pitfalls
- **model-validation** — Train/test splitting strategies, metrics by problem type, residual diagnostics checklist, uncertainty quantification requirements

## Requirements

- Python 3.10+
- An [Anthropic API key](https://platform.claude.com/)
- Scientific Python stack: numpy, scipy, pandas, matplotlib, statsmodels, lmfit, scikit-learn, xgboost, prophet
