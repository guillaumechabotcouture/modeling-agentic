---
phase: "v2 debt"
commit: "C α–γ"
---

## Changelog

**Shared helpers in leaf files; dead files removed; one Coiled attach call.** (α) `scripts/presentation_pass.py`, `scope_signoff.py`, `decision_brief.py` and `model_card.py` use `lib.safe_io.sha256_file` instead of private copies with the same semantics (hex digest, None when unreadable); `decision_brief._sha` keeps its name for `toolkit/spec/signoff.py`, and `model_card` binds it at import, before its worker loads run code. (β) Deleted `agents/skill_index.py` (no reference anywhere), `cloud/configs/e7r7.py` and `e7r8.py` (finished attempts, named nowhere; `e7r9.py` stays because attempt 9 has not run), and `analysis_backend.load_spec`, a one-line alias of `merged_spec` with one caller. `docs/experiments/launchers/` stays: the Phase 34 ledger row points to it. (γ) `cloud/worker_side.attach_existing` is the single `coiled.Cluster(name=..., shutdown_on_close=False, wait_for_workers=False)` + `verify_attached` call used by `run_cluster.attach`, the keeper loop and the overflow handle; each keeps its own running check first, so a stopping cluster is still never attached by name. The workspace name lives in `worker_side.WORKSPACE` (read by `run_cluster` and `vm_job`); `scripts/replay_coiled.py` keeps its copy because it is uploaded to workers alone, and self-test RC60f holds the copies equal. RC60a-f (`run_cluster.py`) failed before the change. Skipped call sites (files other workstreams are changing, or different semantics) are listed in the PR as follow-ups.
