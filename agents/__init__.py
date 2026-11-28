"""The harness's agent-side modules.

- The v2 stage machine and its bounded tasks: stage_machine, engine_build_stage, report_render,
  v2_gate, v2_tasks, v2_sdk_tasks, fix_loop, findings_ledger and analysis_backend.
- The shared modules: enforcement, integrity, run_budget and run_finish.

The owner retired pipeline v1 (the lead session, its eleven specialist agents and their registry) on
2026-11-20, and its code has been removed (docs/design/v1-retirement.md). The package still puts scripts/
on sys.path, as it did before, because modules imported through it import scripts by name
(run_finish.store_run_results, for one)."""

import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
