# E7: Answer quality on the standard question (pre-registered)

**Registered:** 2026-10-23, before either run started. **Owner approval:** 2 runs, $900 total cap (amended from $600 before launch; see Amendments); blinded expert scoring by the owner.

## Question

How good is the pipeline's answer on the standard question, on the merged code (Phase 21–23)? How does it compare with the last accepted pre-Phase-21 report?

This is a quality baseline, not an A/B adoption test. It measures where the shipped system stands and which quality dimensions need work next.

## Standard question (verbatim, identical to every prior malaria run)

> Build an agent-based model of malaria transmission across Nigeria's 774 Local Government Areas (LGAs) using the Starsim framework. The model should include ITNs/LLINs (with PBO and dual-AI net types), indoor residual spraying (IRS), seasonal malaria chemoprevention (SMC), and case management (ACTs). Calibrate to LGA-level PfPR estimates from the Malaria Atlas Project and state-level DHS/NMIS data. The goal is to produce an optimized, cost-constrained resource allocation across LGAs for Nigeria's Global Fund GC7 request (~$320M malaria component), identifying which LGAs should receive which intervention packages.

## Runs

- **Arms:** R1 and R2. Both use the same code (the `main` merge commit after PR #15) and the same question, with independent sessions.
- **Backend:** IDM Coiled, default workspace. Each run gets its own VM with a clean environment.
- **Settings:** `--max-rounds 8 --max-sessions 5 --max-budget-usd 450` per run. The total cap is $900; the cap stops a run with status `budget_exhausted`, and the final gate still runs.
- **Disabled:** the cross-model critic and the methods critic (the shipped defaults).
- **Comparator:** `runs/2026-05-03_235631` (pre-Phase-21 code). It was ACCEPTed at the time and fails today's gate with 46 HIGH findings. It is not re-run.

## Outcomes

**Mechanical (no judgement):**
- terminal status and exit code;
- final-gate action and HIGH findings by kind;
- `artifact_integrity_violations`;
- claims-ledger build (resolved and unresolved claims);
- data-schema findings;
- cost and wall time per run, with the per-stage telemetry split.

**Rubric** (blinded; 1–5, anchors below; each score needs a quoted piece of evidence):

| # | Dimension | 1 | 3 | 5 |
|---|---|---|---|---|
| D1 | Answers the question | no allocation, or one not tied to the stated budget | allocation present, partly justified | a usable LGA × package allocation within ~$320M, with the trade-offs stated |
| D2 | Decision rule usable | none, or a rule restating the table | a rule with gaps or exceptions left unexplained | a short rule a program officer could apply, with its accuracy vs the optimizer stated |
| D3 | Numbers traceable | headline numbers not found in any artifact | most traceable, some contradict their source | every headline number traceable to an artifact; no contradictions |
| D4 | Uncertainty honest | point estimates only, or degenerate CIs | CIs present but incomplete, or sensitivity not reflected in the conclusions | CIs on the headline outputs; conclusions qualified where sensitivity shows instability |
| D5 | External plausibility | headline totals implausible (e.g. DALYs averted above the national burden; nets above the supply ceiling) | plausible but unchecked | totals checked against named external benchmarks (national burden, net supply, the GC7 envelope) |
| D6 | Limitations stated | absent or boilerplate | present but missing a load-bearing one | the load-bearing limitations are named, with their likely effect on the decision |

**Raters:**
- **Model judges:** gpt-6.1-sol (Codex CLI, read-only) and Claude Opus 5.5. Each scores `report.md` plus a common evidence bundle (`data/`, `models/`, `results.md`, `decision_rule.md`, `citations.md`) for every report, in random order.
- **Human:** the owner scores `report.md` only, labelled A/B/C in random order, with run dates, phase names and harness vocabulary stripped where it identifies the code version. About 30 minutes.

**Blinding:**
- Model-judge blinding is partial: new runs contain artifacts the old system didn't produce (`claims_spec.yaml`, `data/data_schema.yaml`, `artifact_integrity.json`). Those files are left out of the bundle, but the report's content may still reveal the code version.
- The owner's blinding is stronger, because only report text is scored.

## Decision use (stated in advance)

1. **Validity:** a run with `artifact_integrity_violations > 0` is invalid for quality scoring; the violation itself is reported.
2. **Headline result:** per-dimension scores for R1 and R2 (owner scores primary, judge mean secondary), with the R1–R2 spread and the comparison with 235631.
3. **"Fit as an expert first draft":** every owner score ≥ 3 and the mean ≥ 3.5 for both runs. Otherwise the dimensions below 3 become the next work items, in order of how far below 3 they are.
4. **Judge calibration:** judge-vs-owner agreement is reported per dimension (exact and within ±1). A dimension where the judges disagree with the owner by more than 1 point on average is flagged: on that dimension the judges are not trusted for future automated evaluation.
5. **Cost:** dollars and hours per accepted report, with the per-stage split. This is the baseline for E4 (decomposed MODEL stage).

## Limitations

- **n = 2:** shows whether a good answer is produced at all, not its reliability; R1–R2 spread is the only variance estimate.
- **One question and one disease:** results are about this question.
- **The comparator** ran on different models and SDK, and under the old gates.
- **The rubric** is the owner's quality criteria as understood here. The owner may revise the anchors before scoring, but not after seeing the scores.

## Amendments (before any run started)

- **2026-10-23, budget $300 → $450 per run ($900 total), approved by the owner.**
  - **Trigger:** the SDK reports cumulative cost for a resumed conversation, and run metadata had summed sessions. Previous multi-session runs therefore cost about half what was recorded: the A/B run T1 cost $316, not $609, and another A/B run $428, not $771.
  - **Why the cap moved:** at $300 a full run would likely be stopped before finishing, and E7 asks about a finished answer.
  - **Unchanged:** the arms, rubric and decision rules.

