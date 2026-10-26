# E9: Plan-before-compute for the MODEL stage (pre-registered)

**Registered:** 2026-10-26, before any E9 code was built or case run. **Owner direction:** "yes, pre-register it" (2026-10-26). Running it needs a separate owner approval of the budget (below).

## Question

If the modeler must have its plan checked before it can change anything, does the MODEL stage reach a gate-clean model with less wasted compute and fewer RETHINK cycles, at no loss of output quality?

The plan covers model structure, calibration targets, sensitivity design and simulation budget. The modeler starts in Claude Code's `plan` permission mode; an automated plan check then approves or returns the plan.

**Why it matters:**
- **Wasted compute after a wrong approach.** The costliest pattern in the post-mortems is the modeler committing to an approach, spending hours of simulation on it, and the gate then sending it back. In attempt 4, `sensitivity_analysis_unstable` blocked through to round 8 in both runs; the modeler worked 10–12.5 h per run.
- **Planning is cheap; simulating is not.** Planning tokens cost little next to simulation hours and rounds.

## Arms

Both arms use `claude-opus-5-5`, the same enforcement hooks, the same fixtures, caps and seeds.

**Control:** the current modeler (`permission_mode: bypassPermissions`).

**Plan-check:**
1. The modeler starts in `permission_mode: plan`. It can read the fixture's inputs (plan, data, pre-mortem) but cannot run commands or write files. It ends planning with a written plan (ExitPlanMode).
2. The harness's `can_use_tool` callback intercepts ExitPlanMode and runs the **plan check**:
   - **Mechanical checks** (no LLM): the plan names the calibration targets and parameter count (identifiability arithmetic within the manifest thresholds), states a sensitivity design that meets the effort floors (perturbation points, parameters), gives a simulation budget in core-hours, and answers each HIGH pre-mortem item.
   - **A short plan critic** (`claude-sonnet-5-5`, at most 6 turns): the five most likely reasons the gate will reject this approach, each rated blocking or not.
3. **If the check passes:** the plan is approved; the session switches to `bypassPermissions` and executes.
4. **If it fails:** the plan is returned with the check's reasons, and the modeler revises in plan mode. At most 2 revisions; after that the plan is approved anyway, with the open concerns recorded.

## Design

- **Fixtures (8):**
  - the 6 used in E1/E1b (AB_T1, AB_B1, HIST_235631, HIST_161312, MEASLES_213310, MEASLES_150152);
  - 2 MODEL-stage snapshots from E7 attempt 6 (`outputs/runs/e7r6/live/<run>/snapshots/*_MODEL_modeler.tar.gz`): the first MODEL launch and the first MODEL launch after a gate returned work.
  - If attempt 6 yields fewer MODEL snapshots, the 6 alone are used and that is recorded.
- **Pairing:** paired by fixture, 1 seed per arm. 16 cases.
- **Caps per case:** 150 turns, $40, 4 h wall. Compute through the compute service; the core-hour cap is 50 per case.
- **Backend:** Coiled replay lane. Each case on its own VM, with results stored by the case itself (Phase 26) and live status, snapshots and transcripts (Phase 29).
- **Spend cap:** $640 hard (16 × $40). Expected $150–300.

## Outcomes

After each case, the MODEL output is graded by the existing validator and scorers (`scripts/stage_scorers.py`, `scripts/validate_critique_yaml.py` in per-round mode). No LLM judges.

**Primary:**
- **Gate-clean rate:** HIGH findings from the validator on the stage's output (lower is better), and the fraction of cases with 0 HIGH.
- **Wasted compute:** core-hours spent on simulation work that the stage later discarded or replaced. This is measured from the compute ledger and the transcripts: batches whose outputs were superseded.

**Secondary:**
- wall time and cost per case;
- turns;
- `artifact_rate`;
- HIGH effort-floor violations;
- time from stage start to first simulation.

**Plan-check diagnostics** (plan-check arm only):
- how often the check returned a plan, and why;
- **agreement:** for returned plans, whether the reason given matches a HIGH finding the validator raises later in the control arm on the same fixture. This tests whether the check predicts the gate.

## Decision rule (stated in advance)

Adopt plan-check for the MODEL stage in the pipeline if **all** hold:
1. **Quality:** total validator HIGH findings in the plan-check arm ≤ those in the control arm (paired), and the gate-clean fraction is not lower.
2. **Waste:** wasted core-hours are at least 30% lower (median paired), *or* wall time to the stage's end is at least 20% lower.
3. **Cost:** cost per case is no more than 15% higher (median paired).

**Otherwise:**
- If the check's returns agree with the later gate findings in ≥ 50% of returned plans, keep the plan check as an *advisory* (logged, not blocking) and iterate on it.
- If not, drop it.

With n = 8 pairs this is a screening test. A pass justifies one full-pipeline run with plan-check before the pipeline default changes.

## Build needed before running (Claude; about 2–3 hours)

1. **Replay variant fields:** `permission_mode: plan` and `plan_check: {mechanical: true, critic_model: claude-sonnet-5-5, max_revisions: 2}`.
2. **Plan check:** a `can_use_tool` callback in `scripts/replay.py` that handles ExitPlanMode, runs the plan check, and records each decision to `plan_checks.jsonl` in the case workspace.
3. **Mode switch:** after approval, switch the session to `bypassPermissions` (SDK `set_permission_mode`), confirmed with the scenario harness (`scripts/sdk_scenarios.py`) on the pinned SDK before any paid case.
4. **Wasted-compute measure:** superseded batches identified from the compute ledger and the output files (`scripts/stage_scorers.py`).
5. **Pre-launch:** a whole-batch dry run (no LLM) and one Haiku case per arm.

## Limitations

- **Single-stage replay.** It measures the MODEL stage, not the rounds of a whole run. A whole-run effect (fewer RETHINK rounds) needs a full-pipeline run.
- **Seeds.** One seed per arm. Agent behaviour varies between runs (no seed control in the model API), so single pairs can differ for reasons other than the arm. The paired design and the plan-check diagnostics mitigate this.
- **The plan check is our design.** A null result could mean the check is weak rather than that planning first does not help. The agreement diagnostic separates the two.
- **Fixture snapshots** from E7 attempt 6 exist only if that run reaches MODEL. The fixture list is fixed when this experiment is launched, not after seeing results.

## Owner and dates

| Step | Owner | When |
|---|---|---|
| Pre-registration (this document) | Claude | 2026-10-26 (done) |
| Budget approval ($640 cap) | Owner | before the build starts |
| Build and checks (above) | Claude | about 3 h after approval; not while E7 attempt 6 is still being diagnosed |
| Run and results | Claude | about 5 h after the build (runs in parallel on Coiled) |
