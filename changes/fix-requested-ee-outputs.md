---
phase: "Agent-built engines (#363 follow-up: pre-sign expected_effects output coverage)"
commit: "validate in requested.py that every declared output appears in expected_effects for at least one non-inert input before sign-off"
---

## Changelog

- `toolkit/engines/built/requested.py`:
  - In `validate_build()`, reject any build request declaring a `per_capita`, `cost_drivers`, or `windows` output that is omitted from `expected_effects` for every non-inert input (mirroring `admission.py` A6's requirement so unsatisfiable specs fail fast before sign-off instead of during `ENGINE_ADMIT`).
  - Update `example_build()` to cover `prev` and add self-test `BE24c8`.
- `agents/v2_sdk_tasks.py`:
  - Note in `draft_spec` that every declared output must appear in `request.expected_effects` for at least one non-inert input.
- `scripts/promote_engine.py`, `agents/analysis_backend.py`, `toolkit/analysis/observed_targets.py`:
  - Cover all declared outputs in synthetic self-test build fixtures.
