---
phase: "Simplification Wave A3"
commit: "retire remaining thin bridges, prune unattached skills and orchestration manifests, slim live scripts, tighten FP_DATA_SET, and expand CI to 8 shards"
---

## Changelog

**Wave A3: retire the 4 remaining thin bridges, prune unattached `.claude/skills/` and dead `.claude/orchestration/` manifests, slim live `scripts/` modules, tighten `compute.FP_DATA_SET`, and upgrade CI `self-tests` from 4 to 8 shards.**

- Retire and inline the 4 remaining thin bridges: `scripts/propagate_uncertainty.py` (inlined `summarise_outputs` and helpers into `toolkit/analysis/uncertainty.py`), `scripts/writer_qa.py` (inlined acceptance-language and status-line helpers into `agents/stage_machine.py` while keeping `acceptance_language.yaml` until Wave B1), `scripts/allocation_robustness.py` (inlined `VALID_HOLDOUT_METHODS`, thresholds, and `compute_verdict` into `toolkit/analysis/stability.py`), and `scripts/decision_brief.py` (inlined `_brief_scope_filter` into `scripts/validate_critique_yaml.py`, decoupled `toolkit/spec/signoff.py`, `toolkit/spec/evidence_regressions.py`, `agents/analysis_backend.py`, `scripts/gate_fuzz.py`, `tests/gate_fuzz_expected.yaml`, `cloud/run_cluster.py`, and `cloud/worker_side.py`).
- Prune 39 unattached `v1`/domain directories from `.claude/skills/` (retaining the 19 referenced skills: `decision-spec`, `critique-blockers-schema`, and 17 `starsim-dev*`), update `.claude/skills/README.md`, and delete `.claude/orchestration/{rigor_artifacts,expert_priors,effort_floors}.yaml`.
- Slim 5 live scripts (`scripts/render_claims.py`, `scripts/identifiability.py`, `scripts/scope_signoff.py`, `scripts/lib/provenance.py`, and `scripts/validate_skill_attachments.py`) by removing dead `v1` CLIs, loaders, and obsolete resolvers while preserving all `v2` APIs and `--self-test` entrypoints (`scripts/structure_metrics.py` kept intact).
- Tighten `scripts/compute.py` `FP_DATA_SET` to `{"data": "all", "toolkit": "data"}`, exclude `data/evals` and `data/world_model_trials` inside `_data_set_files()`, and update `FP_DATA_SET_EDIT` / `BD0`–`BD4` self-tests, `scripts/import_audit.py`, and `scripts/write_audit.py` to maintain exact parity.
- Upgrade `.github/workflows/ci.yml` `self-tests` job from 4 to 8 shards (`shard: [1, 2, 3, 4, 5, 6, 7, 8]`, `--shard ${{ matrix.shard }}/8`, and step name `Self-tests, shard ${{ matrix.shard }} of 8`).
