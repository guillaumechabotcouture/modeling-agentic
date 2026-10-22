# E1: Context diet for the MODEL stage (pre-registered)

**Registered:** 2026-10-22, before any E1 case ran.
**Design reference:** docs/architecture-v2.md §3.3 and §6 step 3.

## Question

Does replacing the modeler's 28 preloaded skills (about 173K-token starting context) with an **active contract packet** plus an on-demand skill index cut cost per turn and starting context, **without** reducing MODEL-stage progress or contract compliance?

## Arms

- **Control:** the current modeler (agents/modeler.py SYSTEM_PROMPT plus the default attached skills).
- **Variant:** a trimmed modeler prompt plus the contract packet (generated from `rigor_artifacts.yaml` and `effort_floors.yaml`) plus a skill index, with **no preloaded skills** and the Skill tool available.

Everything else is identical: model `claude-opus-5-5`, enforcement hooks, `max_turns` 60, `budget_usd` 20, wall timeout 90 min.

## Design

- **Pairing:** a paired design. Both arms run on the same fixtures, one seed each.
- **Fixtures (6):**
  - malaria: AB_T1, AB_B1, HIST_235631, HIST_161312;
  - measles: MEASLES_213310, MEASLES_150152.

  The other four fixtures are held out for later replication.
- **Backend:** IDM Coiled, one VM per case, 12 cases in parallel.
- **Batch spend cap:** $260 (12 × $20 plus $20 headroom). The batch is refused above it.

## Outcomes (computed by `scripts/stage_scorers.py` and telemetry; no LLM judges)

**Primary (non-inferiority):**
- **Progress:** fraction of applicable MODEL-stage required artifacts drafted within the cap (`artifact_rate`).
- **Contract compliance:** effort-floor checks passed among those evaluable, plus a framework-compliance pass.

**Primary (efficiency):**
- starting context tokens;
- mean context tokens per turn;
- cost per turn and cost per case.

**Secondary:** turns used, wall time, Skill-tool loads in the variant (which skills were pulled and when), denials, errors.

## Decision rule (stated in advance)

Adopt the variant for MODEL if **both** hold:
1. the mean paired difference in `artifact_rate` (variant − control) has a 95% bootstrap CI lower bound ≥ −0.10, and no fixture shows a framework-compliance regression;
2. mean cost per turn falls by at least 40%.

Otherwise, report and iterate. With n = 6 pairs this is a screening test: a "pass" justifies a larger replication on the held-out fixtures, not a final verdict.

## Known limitations

- A 60-turn / $20 cap measures *early-stage progress*, not complete MODEL output quality. A complete-stage comparison follows once the compute service lands (v2 §3.2).
- Fixture fidelity limits are as documented in `scripts/fixtures.py`: some spawn prompts are synthesized, and some historical fixtures lack `pre_mortem.yaml`.

## Results (2026-10-22; 12/12 cases collected, sha256-verified)

**Decision: NOT ADOPTED under the pre-registered rule.** It's a screening result; follow-up is E1b.

| Criterion | Result | Pass |
|---|---|---|
| Δ artifact_rate 95% CI lower bound ≥ −0.10 | Δ +0.12, CI [−0.31, +0.50] (n = 6) | ✗ inconclusive (underpowered) |
| No framework-compliance regression | 6/6 OK in both arms | ✓ |
| Cost per turn −40% | $0.166 → $0.122 (−26%) | ✗ |

**Efficiency:**
- Start context 186K → 32K tokens (−82%).
- Input tokens per case −43%, CI [−8.6M, −8.0M].
- Cost per case $9.93 → $7.31, CI on the difference [−$4.56, −$0.70].
- The rest of the cost is conversation-history growth: the start-context saving × 60 turns accounts for the whole input-token reduction.

**Quality signals** (n = 6, not significant): compare_models CLEAN 0.50 vs 0.17; artifacts drafted higher; eligibility findings similar.

**Concern:** HIGH effort-floor violations in the variant in 4/6 cases (control 0/6).
- Part of this is a selection effect: the variant drafted more artifacts, with 24 floor checks evaluated vs 21.
- But `calibration_result_missing` (both measles cases) and `sensitivity_min_perturbation_points` point to packet gaps. The full prompt instructs these explicitly; the packet only lists them as floors.

**Spend:** about $100 of API cost, against a $240 cap.

**Deviations:**
- The first auto-collector crashed, because status tasks queued behind single-threaded workers. Fixed to use `client.run`; no data lost.
- One variant case (HIST_235631) ended by wall timeout, after a late start caused by fixture upload.
- Fixtures were the pre-slimming versions (3.2 GB total, mostly unused raw files). The inputs the modeler used were identical across arms, so this doesn't bias the comparison.

Full table: `E1_results_table.md`.
