---
phase: "Agent-built engines (PR beta)"
commit: "immutable snapshots with link-count checks, static checks S1-S4, minimal-mount engine sandbox and verified-bytes worker, contract fingerprint_files, BuiltEngine adapter checks, and read/write isolation"
---

## Changelog

**Add snapshot provenance, minimal-mount sandboxing, static checks S1-S4, and the `BuiltEngine` adapter (PR beta of `#363`).**

- `toolkit/engines/built/snapshot.py`: content-addressed immutable snapshots (`0555`/`0444`) with O_NOFOLLOW single-read verification, `st_nlink == 1` enforcement, exact directory membership checks before compile (`SnapshotTampered`), `verify_engine_snapshot()`, and AST static checks S1–S4 (file rules, strict import allowlist, banned reflection/dunders/library I/O, module-level statement restrictions, and >16 numeric literal table audit). Self-test `BE12`, `BE19`.
- `toolkit/engines/built/worker.py`: minimal-mount bubblewrap sandbox (`_bwrap_engine_cmd`, omitting `--ro-bind / /`), verified-bytes in-memory module compilation (registered in `scripts/import_audit.py`), `TrackedParams`, opaque unit/package IDs, `BalanceAnalyzer` hook, mandatory harness overwrite of `meta.age_shares`, and fail-closed `engine_unsandboxed` handling with HIGH non-declarable dev-mode finding. Self-test `BE14`, `BE18`, `BE35`.
- `toolkit/engines/built/adapter.py`, `toolkit/engines/built/__init__.py`, `toolkit/engines/contract.py`, `toolkit/engines/benchmark.py`, `toolkit/engines/benchmark_coverage.py`, `toolkit/spec/schema.py`: add `BuiltEngine` adapter enforcing build-request match, sign-off mode check, admission/bench digest binding, config digest match, and pre-compile snapshot verification; extend `code_fingerprint`/`engine_identity` with `fingerprint_files()` and `identity_extra()` (omitting `admission_sha256`); wire `pack_path`/`coverage_path` and harness-injected `run_dir` in `load_engine()`. Self-test `BE19`, `BE22`, `BE23`.
- `scripts/import_audit.py` and `toolkit/import_guard.py`: audit bare `exec()` calls (accepting `worker.py`'s single verified-bytes compile site and refusing unreviewed dynamic loads) and refuse `toolkit.engines.built.candidate` across every import-guard policy. Self-test `BE36`.
- `agents/stage_machine.py`, `agents/fix_loop.py`, and `agents/v2_sdk_tasks.py`: add `engine_build/*` and `models/ext/engine_*` to `harness_owned()`, refuse `models/ext/engine_*` in `fix_loop.in_edit_scope` and non-build tasks, and enforce cross-run and `.hidden`/`reference`/`hidden_reveal.json` read isolation across every task kind. Self-test `BE20`, `BE21`.
