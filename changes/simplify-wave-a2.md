---
phase: "Simplification Wave A2"
commit: "retire v1 STAGE 7 gates, gut validate_critique_yaml.py, and remove dead benchmark archaeology"
---

## Changelog

**Wave A2: retire `v1` STAGE 7 gates, decouple and slim `scripts/validate_critique_yaml.py`, remove dead `v1` benchmark archaeology, and update CI eval harnesses.**

- Inline live `v2` helpers and compatibility definitions into `scripts/{validate_skill_attachments,writer_qa,propagate_uncertainty,render_claims,scope_signoff,done_when}.py`, `scripts/lib/scope_declarations.py`, `agents/findings_ledger.py`, `toolkit/analysis/{contract,moves,run,uncertainty,figures}.py`, `toolkit/spec/{geometry,evidence_dossier,preconditions}.py`, `toolkit/engines/{effect_fit,benchmark_coverage}.py`, and `toolkit/engines/*/benchmarks/pack_trials.py`, removing dependencies on deleted `v1` gate modules and `.claude/orchestration/{effort_floors,rigor_artifacts}.yaml` (including top-level repo-root `sys.path` setup in `toolkit/analysis/moves.py` and `effect_fit` (`EF`) alias updates in `toolkit/analysis/{run,uncertainty}.py`).
- Delete all 37 `v1` STAGE 7 gate modules, `scripts/plant_defects.py`, `scripts/lib/positive_control.py`, and 7 inlined thin-bridge scripts (`scripts/lib/text_spans.py`, `scripts/{alloc_toolkit_use,declaration_tests,stall_rule,open_problems,mechanism_manifest,figure_validator}.py`).
- Gut `scripts/validate_critique_yaml.py` to retain only the live `v2` schema validator, decision engine, done_when/brief-scope helpers, optional critique loader, violation incorporators, and `v2` unit self-tests; remove `legacy_gate()` from `agents/stage_machine.py` and `VALIDATOR`/`gate_command` from `agents/run_finish.py`.
- Delete dead `v1` benchmark history and timing/golden files (`toolkit/engines/*/benchmarks/{history/*.yaml,net_effect_fit.py,net_effect_fit_wave1.yaml,net_checks.py,b15_mechanisms.*,runtime.*,runtime_spike.*}`, `toolkit/alloc/examples/e7a7_costs.yaml`, `toolkit/analysis/dt_golden.*`), and update `toolkit/engines/*/benchmarks/validate_once.sh`, `toolkit/alloc/optimize.py`, and `toolkit/analysis/run.py`.
- Update `.github/workflows/ci.yml`, `scripts/regression_eval.py`, `tests/regression_baseline.yaml`, `scripts/gate_fuzz.py`, `tests/gate_fuzz_expected.yaml`, `scripts/self_tests.py`, `scripts/import_audit.py`, and `scripts/write_audit.py` for the streamlined `v2` gate set; isolate `REPO` in `scripts/compute.py` self-test `make_bundle`/`_remote_task` calls so parallel CI shards do not overwrite live repository files.
