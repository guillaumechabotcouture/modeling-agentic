---
phase: "Agent-built engines (follow-up)"
commit: "support engine.build with observed_targets calibration, batched BuiltEngine/CandidateEngine run_batch, and multi-mode A6 sensitivity"
---

## Changelog

**Close integration gaps surfaced on the first cloud smoke of the agent-built engine lane (`#363`).**

- `toolkit/analysis/observed_targets.py`: support standard `Engine` contracts (including `BuiltEngine` and `CandidateEngine`) without `initial_state()` by reading per-group susceptible/immune shares from `Outcomes.per_capita['susceptible']` / `1 - Outcomes.per_capita['immunity']` when `meta['s_start']` is absent; handle `RequestedEngine` cleanly during pre-build `SPEC` validation (requiring `immunity` or `susceptible` in `request.outputs.per_capita` and deferring the live prior probe until post-admission `CONFIGURE`); allow single-group intensive shares (`immunity`, `susceptible`, `attack_rate`) without requiring a separate population column; add a `run()` fallback when `run_batch()` is absent. Self-test `OT27`.
- `toolkit/engines/built/adapter.py` and `toolkit/engines/built/candidate.py`: set `batch_equals_run = True` and support per-item `UnitSpec` lists and single-or-sequence `Package` arguments in `run_batch()` via chunked sandboxed `run_many` worker calls (256 items per chunk for `closed_form`, 32 for `starsim_module`) so posterior MCMC calibration (`observed_targets.py`), `DirectModel`, and admission check A10 run in batched sandbox workers rather than one subprocess per evaluation. Self-test `BE_BATCH1`.
- `toolkit/analysis/draw_fanout.py`: keep `engine.build` runs on the local bubblewrap-equipped worker rather than fanning out `BuiltEngine` tasks to remote compute workers.
- `toolkit/engines/built/admission.py`: in `check_a6_a7`, evaluate `decision_inputs` parameters/attributes across all active values of their owning component (up to 4) rather than only `active_vals[-1]`, and chunk `run_many` batches, so component value ordering never hides an active mode's effect while still failing dead or wrong-sign wiring.
- `agents/v2_sdk_tasks.py` and `.claude/skills/decision-spec/SKILL.md`: document `engine.build` with `targets.kind: observations` (`immunity`/`susceptible` in `outputs.per_capita`), A8 active-component-value coverage (`request.trials` or `engine_departures`), and quoting ISO `decision_date`.
