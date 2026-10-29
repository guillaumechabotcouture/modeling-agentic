# Improvement plan: lessons from stisim.ai, Starsim v4, and attempts 7–8

Status: draft for review, 2026-10-28. It complements `stage-machine.md` (the architecture and migration steps S1–S6, T0–T2, M0–M2). Each item below is one small PR, checked on its own against the metric stated for it, in the order given.

## Why

- **stisim.ai** (`starsimhub/stisim/stisim/ai`, v0.1, 2026-09-23) is a Claude Code plugin of 14 skills for interactive STIsim/HIVsim research: grilling intake, method selection, calibration strategy, upstream-first extension, and meaningful tests. Its discipline is good, but nothing checks that the agent follows it. Our pipeline checks the work mechanically, but lacks several of the disciplines it teaches.
- **Starsim v4** (branch `v4`, in development, not released) lets a disease be declared as states and transitions, and runs it as an ODE, a stochastic compartmental model, or agents. It has compiled kernels in Rust and Numba, and JAX.
- **Attempts 7 and 8** showed four problems:
  - calibration knobs ran to their bounds (attempt 8: the net-use and case-management multipliers);
  - each run rewrote its costs and optimizer code;
  - interrupted compute calls were charged their whole reservation (attempt 7: 55 core-hours; attempt 8: 400);
  - the question decided the method before anyone checked whether it fit.

## Principles

1. Turn advice into checks where possible. A skill that says "do X" becomes an artifact that records X, plus a gate that reads it.
2. The researcher decides; the agent finds facts. Questions to the owner are limited to decisions that would change the analysis.
3. Reuse before rewriting, both for our toolkit and for the frameworks' own plugins.
4. Measure each change: the gate-finding trend, compute lost, spend, and the structure metrics on each PR.

## Workstream A — harness fixes from attempts 7–8 (first; they protect every run)

| # | Change | Check / metric |
|---|---|---|
| A1 | **Compute charged for work done, not reservations.** A dead or interrupted caller is charged for the chunks that actually ran, from the scheduler or the results file, and no longer for its whole reservation. Long batches run detached, so an agent's tool timeout cannot kill the caller. | Attempt 8 replay: 400 core-hours charged → about 35. Self-tests for a killed caller and an interrupted call. |
| A2 | **Pipeline-runner review fixes.** The open PR from the agent: outputs no longer hashed as code; a step that won't fit the budget is skipped with its outputs kept; outputs still used as inputs are not retired; commands pass the Bash deny rules; parse failures are handled fail-closed; scratch copies are cleaned up. | Real check on the attempt-7 copy; self-tests per finding. |
| A3 | **#47 review fixes** (manifest tolerance, UQ fingerprint, gate import isolation, keeper reuse). | Open PR; CI green. |
| A4 | **The modeler is prompted to use `toolkit/alloc`** (cost spec, packages, optimizer) and to declare `models/pipeline.yaml`. | A smoke run uses the toolkit and declares a pipeline. Attempt 9 costs reproduce the toolkit's within 0.1%. |

## Workstream B — disciplines from stisim.ai, as checked artifacts

| # | Change | Artifact and gate | Metric |
|---|---|---|---|
| B1 | **Calibration spec.** Each parameter gets one role: data-informed, fixed from literature, poorly observed and country-specific, behavioural, tuning, or scenario assumption. Each target has full metadata. | `models/calibration_spec.yaml`. Gate: HIGH if a scenario assumption is fitted to history; HIGH if a fitted parameter is not in the spec; MEDIUM if a bound was widened without a recorded diagnosis (B4). | Knobs at their bounds at the first gate (attempt 8: 2); identifiability findings by round. |
| B2 | **Method fit before tool.** The planner records the analytical objective (description, association, structure, causal, forecasting, dynamic-mechanistic) and why the requested framework fits, including compartmental vs agent-based by stisim.ai's criteria. It may recommend against the requested tool, which becomes an open question for the owner. | `method_assessment` in `decision_brief.yaml`, required by the brief validator. | Present in 100% of briefs. Redirects are reported, not scored. |
| B3 | **Facts vs decisions in the brief.** Each brief item is tagged fact found (with a source), decision needed, or blocked on evidence. Owner questions are limited to decisions, and an optional second round runs before sign-off. | Brief schema; the validator rejects owner questions that are facts. | Owner questions per brief (attempt 8: 5); amendments after sign-off. |
| B4 | **Diagnosis order before widening.** A calibration fix names which check it ran: missing data, missing mechanism, structural constraint, inconsistent targets, mismatched definition, then bounds. | Field in the fix ledger (S3); B1's gate reads it. | Widened bounds with no diagnosis = 0. |
| B5 | **Test rule.** Every new self-test states the bug it would catch. Remove tests that only re-read a value they set. | Review checklist; structure metrics count tests per script. | Self-test runtime and count go down without losing a planted-defect detection (regression eval unchanged). |
| B6 | **Reuse the frameworks' own skills.** Compare our Starsim and stisim skills with `starsim_ai`, `stisim.ai` and `calib`; attach theirs where they are maintained and cover the same ground; keep ours only for what is specific to the pipeline. | `validate_skill_attachments.py` lists the plugins they come from. | Lines of skill text we maintain, which should go down. |

## Workstream C — Starsim v4 (evaluate before adopting)

| # | Step | Decision it informs |
|---|---|---|
| C1 | **Benchmark on our own models**, in an isolated environment, with your approval to run code installed from the `v4` branch. Run attempt 8's malaria ABM cell (Griffin-lite, 4,000 agents, 2016–2026) under v3.3.4 and v4 as agents, and a declared version as compartmental. Also run the starsim-malaria port (`starsimhub/starsim-malaria`, updated 2026-10-27). | Whether v4 changes the cost of the policy grid (attempt 8: 8,960 simulations, about 58 core-hours). |
| C2 | **If C1 shows large gains:** a v4 skill and a migration path for the modeler (v4 ships `docs/migration/v3_v4.md` and a migration script), behind a flag, tried on a smoke run first. | Whether attempt 9 or 10 uses v4. |
| C3 | **Wait for the release** before making v4 the default. The branch is 469 commits ahead of main, and its own targets for agent runs are "at least slightly faster". | Pin a released version. |

## Order and checkpoints

1. A1, A2, A3 (in progress), then A4 with a smoke run.
2. B1 and B4 (calibration spec and diagnosis order), B2 and B3 (brief), each with a smoke run.
3. C1 benchmark (no run spend; VM time only), alongside step 2.
4. Attempt 9: same question, same settings. Compare with attempts 7 and 8 on findings by round, compute lost, spend, and headline basis.
5. B5, B6, and the M2 structure work (`stage-machine.md` §3.7) between runs.

## What stays as it is

The mechanical gate, harness-computed checks, critics, owner sign-off, cloud compute, and the run record. stisim.ai's interactive, researcher-led style suits a different use, months-long projects with a human in the loop. Its memory and handoff skills are noted, not planned, for now.
