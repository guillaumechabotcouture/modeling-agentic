# E1b: Contract-packet gap fix (pre-registered)

**Registered:** 2026-10-22, before any E1b case ran. **Follows:** E1 (`E1_context_diet.md`).

## Question

Do the refined contract packet's explicit floor instructions remove the HIGH effort-floor violations that E1's variant showed (`calibration_result_missing`, `sensitivity_min_perturbation_points`, and others), while keeping E1's efficiency gains?

## Arms

- **New:** the refined variant `experiments/E1b_context_diet/variant.yaml`. It is identical to E1's variant except the packet turns every effort floor into an explicit instruction (file, fields, when required).
- **Comparators, reused from E1 (not re-run):**
  - E1 control: the full modeler prompt plus preloaded skills;
  - E1 variant: the original packet.

  Re-using the control is valid because it is unchanged and ran in the same Coiled environment. It saves about $60.

## Design

- **Fixtures:** the same 6 as E1 (AB_T1, AB_B1, HIST_235631, HIST_161312, MEASLES_213310, MEASLES_150152), 1 seed, identical caps: `claude-opus-5-5`, 60 turns, $20, 90 min.
- **Fixture source:** `fixtures_v2/`, the evidence-based slim versions. *Deviation from E1:* E1 used the full versions, which differ only in files with no evidence of use by the stage. This could slightly change what the agent finds when it lists directories. That is disclosed here, and judged unlikely to affect floor behaviour.
- **Backend:** Coiled, 6 cases in parallel. Batch cap $130.

## Outcomes

**Primary:** HIGH effort-floor violations per case, and floor pass rate (passed / evaluated).

**Guards:**
- start context and input tokens within 10% of E1's variant;
- `artifact_rate` reported, but not powered (see the E1 power analysis: n = 6 detects only differences larger than ~0.47).

**Secondary:** which floors were evaluated and violated; Skill-tool loads.

## Decision rule (stated in advance)

The gap fix **works** if:
1. at most 1 of 6 cases has a HIGH effort-floor violation (E1 variant: 4/6; E1 control: 0/6);
2. no single floor is violated in more than 1 case;
3. the efficiency guards hold.

If it works, the refined variant becomes the base for E2 (history-growth levers). If not, inspect the violating cases and iterate on the packet before E2.

## Limitations

- **n = 6 with one seed:** this is a mechanism check, not a quality-equivalence test. The powered quality test is E3, run to stage completion.
- **Selection effect on floor counts:** more drafted artifacts means more floors can be evaluated and violated. The pass *rate* is reported alongside counts.

## Results (2026-10-22; 6/6 collected, sha256-verified; cluster shut down)

| Criterion | Result | Pass |
|---|---|---|
| ≤1/6 cases with a HIGH floor violation | 1/6 (E1 variant 4/6, control 0/6) | ✓ |
| no floor violated in >1 case | `shortcut_markers_in_model_code` (MEDIUM) in 2 cases | ✗ (strict reading) |
| efficiency within 10% of the E1 variant | start 34K vs 32K; input 10.5M vs 11.1M; cost $6.40 vs $7.31 | ✓ |

**Outcome: inconclusive.**

**Confound.** Artifact progress fell to a mean of 0.17, vs 0.50 for the E1 variant and 0.38 for the control, with all 6 fixtures ≤ 0.29. The drop in HIGH violations may partly reflect fewer drafted artifacts, which means fewer floors are evaluable. The likely mechanism is that explicit floor instructions shift effort within the 60-turn cap toward calibration bookkeeping.

**Method lesson.** Progress measured at a fixed turn cap has twice produced results that can't be attributed cleanly. Compliance and progress trade off against the cap.

**Next:**
- Run MODEL replays to stage completion under a dollar cap. Outcomes: cost-to-completion, plus compliance and quality at completion. This folds into E2's design.
- Packet follow-up: keep scratch and exploratory scripts outside `models/` (the shortcut-scanner scope).

**Spend:** about $38.
