---
phase: "Agent-built engines (PR epsilon)"
commit: "independent build_reference task (A11), promote_engine.py promotion tool, built-engine gate fuzzing, and BE2/BE8/BE10 regression tripwires"
---

## Changelog

**Complete the agent-built engine lane with the independent reference task (`build_reference`), team-registry promotion tool (`scripts/promote_engine.py`), fail-closed built-engine gate fuzzing, and planted-defect regression tripwires (PR epsilon of `#363`).**

- `toolkit/engines/built/snapshot.py` and `toolkit/engines/built/admission.py`: add `REFERENCE_ALLOWED_QUALIFIED` (allowing `scipy.integrate` for A11 references while keeping it refused for engines) and `create_reference_snapshot` (`engine_build/reference/snapshots/<sha>/`); record `reference_snapshot` and `reference_comparison` digests in `admission.json["digests"]` while keeping cross-run reuse (`try_reuse_admission`) clean. Self-test `BSNAP_REF1`.
- `agents/v2_tasks.py`, `agents/v2_sdk_tasks.py`, and `agents/stage_machine.py`: add bounded `build_reference` task (`BUILD_REFERENCE_MAX_TURNS = 80`, `ENGINE_REFERENCE_CAP_USD = $6`, strict bidirectional isolation from candidate working copies and snapshots, no shell/MCP/cloud), invoke it in `h_ENGINE_BUILD` when A11 is on with 2 tries (falling back to A11 `unavailable` -> `completed_engine_not_admitted` if both fail), load verified reference snapshots in `h_ENGINE_ADMIT`, and account for interrupted `build_reference` tasks in the lane budget. Self-tests `BE20c`, `BE17_REF`.
- `scripts/promote_engine.py`: build a candidate registry package and benchmark pack from an admitted run (`engine.py` with inline `--self-test`, `card.yaml`, `ENGINE_NOTES.md`, `attribute_binding.yaml`, `decision_inputs.yaml`, `benchmark_coverage.yaml`, `benchmarks/pack.yaml`, and optional `benchmarks/oracles.py`), evaluate §5.5 promotion requirements (owner sign-off, two distinct admitted questions, fresh hidden draws, expert review, registry PR), and never modify registry files. Self-tests `PROM1`–`PROM4`.
- `scripts/built_engine_gate.py`, `scripts/gate_fuzz.py`, `scripts/regression_eval.py`, and `tests/regression_baseline.yaml`: add fail-closed `built_engine_gate.gate_findings` fuzzed by `gate_fuzz.py` over malformed `admission.json`, snapshot manifests `<sha>.json`, and `signoff.json`, plus deterministic `BE2`, `BE8`, and `BE10` tripwires in `regression_eval.py`. Self-tests `BEG1`–`BEG4`.
