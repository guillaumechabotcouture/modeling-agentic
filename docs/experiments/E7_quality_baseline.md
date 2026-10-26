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

## Deviations during execution (logged before any quality data was seen)

1. **Attempt 1 (2026-10-23 05:23 UTC) was stopped by the Anthropic account's monthly usage limit.**
   - Both runs reached MODEL round 1 (R1 $27.11, R2 $34.80) before every API call failed ("You have reached your specified API usage limits"). They ended `max_sessions_reached` with no report.
   - After the owner restored access, both were resumed from their files (fresh lead session; file-based resume context), on the same code `7fbad16`.
2. **Attempt 2 (resume, 17:15 UTC) was paused after ~50 min.**
   - R2b's integrity record flagged two genuine contract breaches: the modeler's own scripts wrote the gate outputs `identifiability.yaml` (`models/run_calibration.py`) and `models/eligibility_probe.yaml` (`models/run_uq.py`, a self-written stand-in for `scripts/propagate_uncertainty.py`).
   - Under `7fbad16` such violations are permanent, so the run could never reach ACCEPT. Both runs were interrupted (SIGINT, state kept).
   - **Fix (Phase 23 γ):** a violation resolves when the producer script later rewrites the file. Resolved violations stay visible as MEDIUM advisories. The modeler and lead prompts now state that gate outputs come only from their producers.
3. **Budget-ledger reconciliation.**
   - The interrupted sessions produced no SDK cost, so the ledger had charged each its whole remaining allowance.
   - Their spend was instead estimated from the collected CLI transcripts: per-model $/weighted-token was fitted on 96 E5 cases (median error 10%). Estimates: R1b $12.56, R2b $16.59.
   - The estimates were entered with a 20% margin ($15.07, $19.91), labelled as operator reconciliation.

Arms, rubric and decision rules are unchanged. The next resume (attempt 3) runs on the merge commit that contains the fix. Rule 1 (invalid if integrity violations) applies to *open* violations at the end of the run.


## Attempts 3–5 and the attempt-6 amendment (2026-10-25, before attempt 6 launched)

### What happened in attempts 3–5 (no quality data was seen)

**Attempt 3–4** (2 runs, ~$698 by the ledger):
- Both runs completed 8 review rounds and reached DECLARE_SCOPE.
- Neither produced a report. The Claude CLI bundled with the pinned SDK refuses a subagent's Write to `REPORT*.md`, and the writer was told to write `report.md`.
- Fixed in PR #21: the writer drafts `manuscript.md`, `report.md` is built from it, and the preflight checks a real subagent write.

**Attempt 5** (1 run, ~$62 actually used; the ledger showed $450):
- After 2.9 h the session's CLI process and the modeler's own simulation grid were SIGKILLed together, which points to VM memory exhaustion (32 GiB). This is not confirmed: there was no kernel-log access.
- The PR #15 budget rule then charged the crashed session its whole allowance, so the run stopped instead of resuming.
- Fixed in PR #23: after a crash the harness reads the conversation's true cumulative cost back. The VM for attempt 6 is 64 GiB.
- The result was stored to `outputs/runs/e7r5/` (PR #22).

### Attempt 6: design changes (owner-approved)

1. **One run, not two (owner, 2026-10-25):**
   - **Why:** the first full run since the fixes; limit the loss if a new failure appears.
   - **Consequence:** the R1–R2 spread (the only variance estimate) is not available. A second run is decided on this run's result.
2. **Integrity flags do not by themselves invalidate scoring (owner, 2026-10-25).**
   - **Why:** the integrity detector (`agents/integrity.py`) has known false positives. It infers which shell command wrote a file, and it flagged `identifiability.yaml` in attempts 4 and 5. Its replacement (gate outputs re-produced by the harness) is not merged yet.
   - **What changes:** open integrity violations are reported with the scores and shown to all raters. They no longer exclude the run under Decision rule 1.
3. **Unchanged:**
   - **Run settings:** the question, rubric, raters, blinding and comparator; `--max-rounds 8 --max-sessions 5 --max-budget-usd 450`. The SDK's per-session cap can overshoot by one turn (measured), so the run can end slightly above $450.
   - **Code:** `main` at `696e5c8`, on a 64 GiB Coiled VM (8 vCPU).
   - **Results:** stored by the VM itself to `outputs/runs/e7r6/`, with the CLI transcripts (for the per-stage cost and time split).
4. **Not measured here:** the compute service. Nothing makes the modeler use it, and attempt 5 ran its grid serially. This run is a speed and cost baseline without it.

### What the result will change (stated before launch)

- **No `report.md`:** an engineering failure. Diagnose it from the stored run; no scoring.
- **A report, and any owner score below 3:** those dimensions become the next work items, in order of how far below 3 they are (existing rule 3).
- **A report, and every owner score at least 3 with a mean of at least 3.5:** "fit as an expert first draft" for this question. The next step is a second run to estimate variance before any claim of reliability.
- **In every case:** report the dollars and hours per report, with the per-stage split, as the baseline for the compute-service and leaner-stage experiments.

### Attempt 6: second amendment (2026-10-26, before launch; owner decisions A and B)

- **A. Every run ends with a report.**
  - **Reserve:** 8% of the cap ($36 of $450) is held back for writing.
  - **When there is no report:** if the sessions end without one, the harness runs the final gate and a writer session. The report opens with the gate's verdict.
  - **A final-round RUN_FAILED** now produces a report marked not accepted.
  - **Scoring a not-accepted report:** the rubric is applied as written. The verdict is shown to the raters and reported with the scores.
- **B. Simulation batches run on 4 separate compute VMs** (8 vCPU each) through the compute service. The agent's VM (64 GiB) runs no compute-service work.
  - **Consequence for the cost and time figures:** this run's figures are a baseline with the compute service available, not without it.
  - **Unchanged:** whether the modeler uses the compute service is up to the modeler. The autopsy reports its use.
- **Also recorded with the run:**
  - live status and resource history;
  - stage-boundary snapshots (replay fixtures);
  - 30-minute checkpoints including the CLI transcripts;
  - `autopsy.md` with the failure layer;
  - a cross-run register row.
- **Pre-launch smoke on the launch code passed:** preflight, the pipeline's own report, the final gate and storage.
