---
name: decision-rule-extraction
description: Required rigor artifact when a run produces an allocation
  CSV (LGA-level, district-level, geography-level budget/intervention
  assignment). Instead of shipping a 774-row table that no human can
  defend, the modeler must also ship a `decision_rule.md` that states
  the rule a program officer will use (or declare the allocation
  non-compressible with justification). The STAGE 7 validator blocks
  HIGH when `*allocation*.csv` exists without a validated
  `decision_rule.md`. Trigger phrases include "decision rule",
  "allocation rule", "774 rows no rule", "what is the rule", "how do I
  defend this allocation", "per-LGA choices".
---

# Decision Rule Extraction

## Why this exists

Across three malaria runs, the pipeline produced an `lga_allocation.csv`
— 774 rows mapping each Nigerian LGA to an intervention package. In
each case, a Global Fund program officer asked the same reasonable
question: **"What is the rule? How do I defend per-LGA choices?"** —
and there was no answer. Critique agents did not catch it because a
tabular allocation looks normal in a paper. But for the end user of
the model, a 774-row table without an articulated rule is an
optimizer dump, not a policy recommendation.

A decision rule is the compact statement — a table, a short tree, a
prose paragraph — that a program officer can carry into a meeting.
"Every LGA in archetype A3 with PfPR > 0.2 gets ITN+IRS+SMC; A1-A2
LGAs get ITN+SMC; everything else ITN only." That is an artifact the
NMEP can implement. A spreadsheet of 774 rows is not.

## When the rule is required

This skill applies whenever the run produces an allocation artifact:

- `{run_dir}/*allocation*.csv`
- `{run_dir}/*budget*.csv`
- `{run_dir}/*optimization*.csv`

If any such file exists, the validator expects `decision_rule.md`. If
no allocation CSV exists, the rule is not required.

## Schema

Write `{run_dir}/decision_rule.md` with YAML front-matter and four
required sections:

```markdown
---
rule_type: tabular | tree | prose-with-exceptions | non-compressible
---

# Decision Rule

## Features (required)

List every feature the rule uses. Each line:
- `feature_name`: source (dataset filename), type (categorical/numeric),
  range (if numeric) or categories (if categorical).

Example:
- `archetype`: lga_archetypes.csv, categorical, {A1,A2,...,A6}
- `PfPR`: map_pfpr.csv, numeric, [0.02, 0.78]
- `access_to_care`: dhs.csv, numeric, [0.12, 0.94]

## Rule (required)

The rule itself. Format depends on `rule_type`:

- `tabular`: a Markdown table of feature buckets → intervention package.
- `tree`: a short decision tree (≤ 7 nodes) in Markdown or mermaid.
- `prose-with-exceptions`: a paragraph + a short exceptions list.
- `non-compressible`: the literal string "See Justification below".

## Validation (required)

- `accuracy_vs_optimizer`: fraction of rows where the rule recovers
  the optimizer's choice. Float in [0, 1]. Required even for
  `non-compressible` (will be ≤ 0.X).
- `exceptions_count`: integer, count of rows where the rule disagrees.
- `exceptions_list`: list of LGA/unit identifiers where the rule
  disagrees. Required if `exceptions_count > 0`.

## Justification (required iff rule_type=non-compressible)

If no compact rule achieves ≥ 90% accuracy and the optimizer's choices
truly depend on LGA-specific detail, explain why. A legitimate
non-compressibility argument references: (1) interaction terms too
complex to summarize, (2) LGA-specific constraints from stakeholder
consultation, (3) optimizer exploiting numerical near-ties. A program
officer must still be able to read this section and understand why
they can't answer "what's the rule?" with a short statement.
```

## How to author the rule

1. **Load the allocation** — read `{run_dir}/*allocation*.csv` and
   identify the decision column (`package`, `intervention_bundle`,
   `allocated_package`, etc.) and the feature columns.

2. **Try the simple cases first**:
   - Does a single feature predict the decision? (e.g., archetype alone)
   - Does a 2-feature table (archetype × PfPR bucket) predict it?
   - Do the decision column's values cluster by a threshold rule on a
     continuous feature?

3. **Measure accuracy** — for the rule you propose, count how many
   rows in the CSV actually match. Report as `accuracy_vs_optimizer`.

4. **Decide rule type** based on accuracy and parsimony:
   - ≥ 95% accuracy with ≤ 7 rule nodes → `tabular` or `tree`.
   - ≥ 90% accuracy with a short natural-language form plus a listed
     exception set → `prose-with-exceptions`.
   - < 90% accuracy and no compact rule found → `non-compressible`
     (and argue it honestly in the Justification section).

5. **Do NOT fabricate a rule** — if the allocation is genuinely
   optimizer-driven with per-LGA heterogeneity, say so. The gate
   accepts `non-compressible` with a real justification. It does NOT
   accept a fake 60%-accuracy "rule" presented as if it were the real
   policy.

## Validator rules

The validator parses the YAML front-matter + required section
headings. Violations:

| Violation                          | Severity | Trigger                                                                 |
|------------------------------------|----------|-------------------------------------------------------------------------|
| `decision_rule_missing`            | HIGH     | `*allocation*.csv` exists (top-level, `data/`, or `models/`); `decision_rule.md` absent. |
| `decision_rule_malformed`          | HIGH     | File exists but front-matter or required section is missing/invalid.    |
| `decision_rule_low_accuracy`       | HIGH     | `rule_type ≠ non-compressible`, `accuracy_vs_optimizer < 0.90`, and `exceptions_count == 0`. You can't claim a rule with < 90% accuracy AND no declared exceptions. |
| `decision_rule_self_referential`   | MEDIUM   | A rule node references the optimizer's output rather than an input feature (Phase 4 Commit γ). Tokens that fire: `the optimizer`, `optimizer's choice`, `optimizer output`, `optimized choice`, `budget cut`, `funded set`, `in the funded`, `cost-effective enough`, `fall within the budget`, `ranked by`, `selected by the model`, `the model recommends`. The Justification section is exempt. |

## Forbidden patterns: self-referential rule nodes

A rule node's question must be answerable from **input features alone**
(archetype, PfPR, population, geographic zone, a concrete CE-cutoff
value like `> $20/case`). It must NOT be answerable only after running
the optimizer. The following patterns are mechanically rejected (γ
check):

❌ "Is the LGA in the funded set?" — funded set is the optimizer's
   output. A program officer can't apply this without running the
   model.

❌ "Is the LGA cost-effective enough to fund at $107M/yr?" — same
   problem. The "cost-effective enough" threshold is implicit in the
   optimizer's ranking; it's not a value the rule states.

❌ "Was the LGA ranked by the optimizer?" — output, not input.

✅ "Is PfPR > 25%?" — input feature with a stated cutoff.

✅ "Is the LGA in the SMC-eligible northern zones (NW, NE, NC)?" —
   geographic input from the archetype table.

✅ "Is cases-averted-per-dollar > $20/case?" — concrete CE cutoff
   value the rule itself states. (Compute the cutoff value during
   rule extraction and bake it into the node.)

If you cannot find a compact set of input features that recovers ≥90%
of the optimizer's choices, switch `rule_type` to `non-compressible`
and explain in the Justification section why the allocation doesn't
admit a compact rule. The Justification section is exempt from the
self-reference check, so referencing the optimizer is allowed there
to honestly explain the non-compressibility.

## Writer integration

When `decision_rule.md` exists, the writer agent MUST embed it
verbatim in §Policy Recommendations of the final report. A program
officer reading the report should see the rule, its features, its
accuracy, and (if applicable) the list of exceptions. Hiding the rule
in an appendix defeats the purpose.

## Example — tabular rule (happy path)

```markdown
---
rule_type: tabular
---

# Decision Rule

## Features
- `archetype`: lga_archetypes.csv, categorical, {A1..A6}
- `pfpr_u5`: map_pfpr.csv, numeric, [0.02, 0.78]

## Rule

| archetype     | PfPR<0.2        | PfPR 0.2-0.4   | PfPR≥0.4          |
|---------------|------------------|-----------------|--------------------|
| A1-A2 (stable) | ITN only        | ITN+SMC         | ITN+SMC+ACT        |
| A3-A4 (seasonal) | ITN+SMC       | ITN+IRS+SMC     | ITN+IRS+SMC+ACT    |
| A5-A6 (epidemic) | ITN          | ITN+IRS         | ITN+IRS+SMC        |

## Validation

- `accuracy_vs_optimizer`: 0.94
- `exceptions_count`: 45
- `exceptions_list`: [Borno-Maiduguri, Yobe-Damaturu, ...]
```

## Example — non-compressible (legitimate)

```markdown
---
rule_type: non-compressible
---

# Decision Rule

## Features
- (all 12 features used by optimizer listed)

## Rule

See Justification below.

## Validation

- `accuracy_vs_optimizer`: 0.61
- `exceptions_count`: 325
- `exceptions_list`: (not listed — exceeds 10% of allocations)

## Justification

The allocation is driven by strong interaction between stockout risk,
stakeholder pre-commitment (11 LGAs with SMC already committed in GC6
Q4 residual), and a near-tie in the optimizer's score between
ITN+IRS+SMC and ITN+SMC for most A3 LGAs. A tabular rule with
≥90% accuracy would require > 20 cells. The per-LGA allocation is
tracked in `lga_allocation.csv`; program officers should use the CSV
with explicit per-LGA consultation, not a compact rule.
```
