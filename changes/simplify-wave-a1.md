---
phase: "Simplification Wave A1"
commit: "retire dead v1 scripts, cloud v1 artifacts, and spike adapter"
---

## Changelog

**Wave A1: retire dead v1 scripts, cloud v1 artifacts, and the spike analysis adapter.**

- `agents/integrity.py`, `scripts/{cloud_batch,model_card,fixtures,derived_rebuild,ask,gate_replay,golden_traces,convergence_report,lessons,run_autopsy}.py`, and the retired integrity fixture in `tests/fixtures/`: delete retired `v1` tooling and update `agents/run_budget.py`, `agents/v2_sdk_tasks.py`, `agents/enforcement.py`, `scripts/pipeline.py`, `scripts/propagate_uncertainty.py`, `scripts/writer_qa.py`, `scripts/validate_critique_yaml.py`, `main.py`, `scripts/import_audit.py`, `scripts/write_audit.py`, and `scripts/self_tests.py` while preserving `main.py` `--v1` refusal checks.
- `cloud/{run_health.py,run_health.yaml,platform_probe.py,fixtures/e7r8_health/}`, unlaunched run-input/config files in `cloud/`, and `cloud/{run_cluster,worker_side}.py`: delete dead `cloud/` artifacts and strip `ack-probe`, `smoke-run`, `health`, `brief`, and `report` subcommands and helpers while keeping live `v2` cluster launch, checkpointing, and `sign_side` intact.
- `agents/{analysis_backend,stage_machine,world_model_stage}.py`, `toolkit/spec/signoff.py`, and `scripts/resume_smoke.py`: remove the `spike/` analysis adapter, migrate stage-machine, world-model, and resume-smoke fixtures to the `toolkit` backend (`schema.direct_toy`) with a fast deterministic toy `analyze` backend in `_self_test()` for `signoff="none"` runs, keep `agents.stage_machine.sign()` delegating to the `toolkit` backend, and accept `sha256` alongside `evidence_sha256` in `ED11b`.
