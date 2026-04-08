---
name: investigation-threads
description: Framework for managing hypothesis-driven investigation threads
  that connect goals → data → models → figures → findings into traceable
  evidence chains. Each thread is a self-contained investigation from question
  to answer. Use when building, updating, or reasoning about threads.yaml.
  Trigger phrases include "thread", "evidence chain", "investigation",
  "traceability", "threads.yaml", "which hypotheses are affected",
  "what evidence supports", "thread status", "blocked threads".
---

# Investigation Threads: Evidence Chain Management

## What is a Thread?

A thread is a **traceable chain from question to answer**:

```
Question → Hypothesis → Data Required → Model Test → Figure → Finding → Policy
```

Every hypothesis in plan.md becomes a thread. The thread tracks whether each
link in the chain is complete, what evidence supports the finding, and what
assumptions could change the verdict.

## threads.yaml Structure

The manifest lives at `{run_dir}/threads.yaml`. It is created by the planner
and updated by every subsequent agent.

```yaml
threads:
  - id: T1_short_name          # Unique ID
    hypothesis: H1              # Links to plan.md hypothesis
    question: "..."             # The specific question this thread answers
    
    data_required:              # What data is needed
      - name: "..."
        source: "..."           # Dataset or paper
        benchmark: B1           # Links to published benchmark
        status: available|missing|partial
    
    model_test:                 # How the model tests this
      description: "..."
      code_file: model.py       # Which file
      key_parameter: "..."      # The parameter being tested
      sensitive_to: "..."       # What would change the result
    
    evidence:                   # The proof
      primary_figure: fig.png   # Main evidence figure
      diagnostic_figures: []    # Supporting diagnostics
      benchmarks_checked: []    # Which benchmarks validated
      benchmarks_status: {}     # PASS/FAIL for each
    
    verdict:                    # The conclusion
      value: SUPPORTED|REFUTED|INCONCLUSIVE|NOT_TESTED
      confidence: HIGH|MEDIUM|LOW
      causal_label: CAUSAL|ASSOCIATIONAL|PROXY
      grounded_in: []           # Specific figures + benchmarks
      would_change_if: "..."    # What would reverse the verdict
    
    dependencies:               # Thread relationships
      depends_on: []            # Other thread IDs
      blocks: []                # Threads waiting on this one
    
    policy_implication: "..."   # What this means for decision-makers
    
    status: planned|data_blocked|model_complete|complete|conditional
```

## How to Read threads.yaml

### Check completeness
```
For each thread:
  - data_required: all status == "available"?
  - model_test: code_file exists on disk?
  - evidence: primary_figure exists on disk?
  - verdict: value is not null?
  
If all yes → status: complete
If data missing → status: data_blocked
If model not run → status: model_complete (data ready, awaiting model)
If verdict has caveats → status: conditional
```

### Trace impact of a critique item
```
Critique says: "South-South EIR/PfPR inconsistency"
→ Search threads for data_required entries referencing South-South or B11
→ Find: T3 (H3), T5 (H5), T6 (H6) all reference B11
→ These threads' verdicts are now CONDITIONAL on resolving the data issue
```

### Decide what to work on next
```
Priority order:
1. Threads that BLOCK other threads and are incomplete
2. Threads with data_blocked status (need DATA agent)
3. Threads with model_complete but no verdict (need ANALYZE)
4. Threads marked conditional (need clarification or scope declaration)
```

## How to Update threads.yaml

### Planner (creates skeleton)
```yaml
# Create one thread per hypothesis from plan.md
# Fill: id, hypothesis, question, data_required (from plan benchmarks)
# Set: status: planned
# Leave: model_test, evidence, verdict as null/empty
```

### Data Agent (updates data status)
```yaml
# For each thread's data_required:
#   - Check if the dataset was downloaded
#   - Update status: available|missing|partial
#   - If any data_required is missing: set thread status: data_blocked
```

### Modeler (updates model + evidence)
```yaml
# For each thread:
#   - Fill model_test (code_file, key_parameter, sensitive_to)
#   - Fill evidence.primary_figure when figure is created
#   - Fill evidence.benchmarks_checked after validation
#   - Set status: model_complete
```

### Analyst (updates verdicts)
```yaml
# For each thread:
#   - Fill verdict (value, confidence, causal_label, grounded_in)
#   - Fill would_change_if
#   - Fill policy_implication
#   - Set status: complete or conditional
```

### Strategist (reads for decisions)
```yaml
# Read all threads, check:
#   - How many complete? How many blocked?
#   - Which critique items affect which threads?
#   - Are dependencies satisfied?
#   - Is the model fit for the stated purpose based on thread completion?
```

### Writer (assembles report)
```yaml
# Include sections for:
#   - Complete threads: full evidence + verdict
#   - Conditional threads: with explicit caveats
#   - Blocked/not_tested: in Future Work section
# Order by: thread dependencies (dependent threads after their prerequisites)
```

## Thread Status Meanings

| Status | Meaning | Next Action |
|--------|---------|-------------|
| `planned` | Hypothesis defined, no work done | Data agent collects data |
| `data_blocked` | Required data not available | Redirect to DATA or declare scope |
| `model_complete` | Model ran, figures exist | Analyst writes verdict |
| `complete` | Full chain: data → model → figure → verdict | Include in report |
| `conditional` | Verdict depends on unresolved assumption | Include with caveats |
| `not_testable` | Model structure can't test this hypothesis | Retract or redesign |

## Evidence Grounding Rules

Every verdict must be grounded in specific evidence:

1. **Primary figure must exist on disk** and match the thread's question
2. **At least one benchmark must be checked** (PASS or explained FAIL)
3. **Causal label must be justified** (not just assigned)
4. **"Would change if"** must name a specific, falsifiable condition
5. **If grounding is incomplete**, status must be `conditional`, not `complete`
