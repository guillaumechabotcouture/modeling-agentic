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
