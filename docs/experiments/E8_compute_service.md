# E8: MODEL-stage wall time with the compute service (pre-registered)

**Registered:** 2026-10-23, before any E8 case ran. **Code:** PR #19 (Phase 24 α, `scripts/compute.py`) merged into `main`. **Owner direction:** "using coiled to do the compute of simulations is a no brainer, and then we can test whether being smarter about rethink doing less would make sense".

## Question

When the modeler's batches of simulations run on extra Coiled workers rather than on its own VM, how much shorter is the MODEL stage, at what cost, and is the stage's output unchanged in quality?

**Why it matters:** E7's wall-time analysis found that 55–61% of a run's wall time was the modeler waiting for serial simulations. This experiment measures how much of that time the compute service recovers. It does not measure the whole pipeline.

## Arms

Both arms run the same code and modeler prompt (the shipped prompt, which now points batches to `scripts/compute.py`).

- **control:** no scheduler. `compute.py` falls back to a process pool on the case's own VM (8 vCPU).
- **compute:** the variant sets `compute: true`. The case VM plus a shared pool of 6 compute workers (8 vCPU each) are reached through the scheduler. The case's own VM is excluded from compute work.

Using the same prompt in both arms isolates where the simulations run. It does not measure the effect of the prompt change itself.

## Design

- **Fixtures:** the 6 used in E1/E1b (AB_T1, AB_B1, HIST_235631, HIST_161312, MEASLES_213310, MEASLES_150152), from `fixtures_v2/`; 1 seed; 12 cases.
- **Caps** (both arms): `claude-opus-5-5`, 150 turns, $40, 4 h wall. They are raised from E1's 60 turns, $20 and 90 min so that most cases can finish the stage; E1's caps stopped cases before their simulations were done.
- **Backend:** Coiled `idm-coiled-idmad-r2`; `replay.py run --backend coiled --workers 6 --compute-workers 6`.
  - Fixture-affine assignment puts the control and compute cases of a fixture on the same case VM, where they run one after the other.
  - **Compute cap per case:** `MA_COMPUTE_MAX_CORE_HOURS` = 50 core-hours.
- **Spend caps:**
  - **LLM:** $480 hard (12 × $40). Expected $200–300.
  - **Coiled VMs:** about 12 VMs for up to 5 h.

## Outcomes

**Primary:** MODEL-stage wall time per case, paired by fixture (compute vs control). Reported as the median paired ratio, with every pair shown.

**Secondary:**
- wall time inside tool calls versus model generation (from `turns.csv`: the gap after a turn that calls a tool counts as tool time);
- cost per case;
- core-hours used (from `models/compute_ledger.json`);
- cases ending on a cap (turns, dollars, wall time).

**Adoption:**
- number of compute-arm cases that called `compute.py`;
- share of the compute arm's simulation core-hours that went through it.

**Quality guard:**
- effort-floor pass rate and HIGH floor violations per case;
- `artifact_rate`;
- open integrity violations.

## Decision rule (stated in advance)

1. **Adoption first.** If fewer than 4 of 6 compute-arm cases call `compute.py`, the result says nothing about the service. The next step is then the prompt (how the modeler is told to batch), not the compute backend.
2. **Adopt the service as the default for Coiled launches** if all of the following hold:
   - the median paired wall-time ratio is ≤ 0.7 (at least 30% shorter);
   - cost per case rises by no more than 10% (median paired);
   - the compute arm has no more HIGH floor violations in total than the control arm, and no open integrity violations.
3. **Otherwise:** report which condition failed. If the cause is compute, the next work is the service; if it is LLM time, it is the stage design (the "smarter rethink" experiment).

## Limitations

- **n = 6 pairs, 1 seed.** Large effects only; agent behaviour varies between runs, so a pair can differ for reasons other than compute.
- **Not a whole run.** The MODEL stage only; other stages and RETHINK rounds are not measured.
- **Shared compute pool.** Concurrent compute-arm cases compete for the 6 compute workers, as runs would in practice.
- **Uneven benefit.** Fixtures whose models are cheap gain little. The paired design shows this per fixture.
