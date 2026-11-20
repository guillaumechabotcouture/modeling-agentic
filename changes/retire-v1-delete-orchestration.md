---
phase: "v1 retirement"
commit: "γ"
---

## Changelog

**The v1 lead session is deleted: its agents, prompts, hooks, heartbeat, registry and run loop.** The eleven specialist prompt modules (`agents/planner.py`, `data.py`, `modeler.py`, `analyst.py`, `writer.py`, `red_team.py`, `critique_{domain,methods,premortem,presentation,sufficiency}.py`), `agents/lead_prompt.py`, `agents/hooks.py` and `agents/observability.py` are removed. `agents/__init__.py` keeps only the `scripts/` `sys.path` line that v2 imports rely on. `agents/run_finish.py` keeps what v2 uses: `gate_command` (the legacy gate), `exit_code_for` and `store_run_results`. `agents/run_session.py` keeps `LEAD_TOOLS` for the preflight. `main.py` loses `run()` and its helpers (resume context, sign-off waits, the lead's brief answers) and the self-test cases of the deleted code. It keeps every CLI flag, `main_v2`, the PV, EV and ST cases, and the cases that test kept shared modules (enforcement, integrity, run budget, preflight); `reg` is now a literal set of the agent names enforcement's policy was written for. `scripts/validate_skill_attachments.py` also counts a skill that a module under `agents/` loads by its SKILL.md path (T15-T16 failed first), so v2's `decision-spec` stays checked. CLAUDE.md, README.md and the skills index describe v1 as retired and removed. `agents/stage_machine.py`, toolkit/ and the gates are untouched. The stage machine, v2 tasks, fix loop, golden traces, gate replay and launcher self-tests pass, as do `regression_eval` and `gate_fuzz`.
