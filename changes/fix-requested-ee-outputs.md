---
phase: "Agent-built engines (#363 follow-up: pre-sign expected_effects output coverage & stratified public A6)"
commit: "validate expected_effects output coverage before sign-off and use stratified thresholds in public A6 checks"
---

## Changelog

- `toolkit/engines/built/requested.py`:
  - In `validate_build()`, reject any build request declaring a `per_capita`, `cost_drivers`, or `windows` output that is omitted from `expected_effects` for every non-inert input (mirroring `admission.py` A6's requirement so unsatisfiable specs fail fast before sign-off instead of during `ENGINE_ADMIT`).
  - Update `example_build()` to cover `prev` and add self-test `BE24c8`.
- `toolkit/engines/built/admission.py`:
  - In `run_public_checks()`, use `a6_base_points` / `a6_min_moving_bases` from `thresholds.yaml` (3 stratified LHS bases, >= 2 moving) instead of a single unstratified draw (`n_bases=1, min_moving=1`) that can land in a domain saturation tail and falsely fail `public_ok`.
- `agents/v2_sdk_tasks.py`:
  - Note in `draft_spec` that every declared output must appear in `request.expected_effects` for at least one non-inert input.
  - Warn `build_engine` not to create stray scratch files in the working directory (no delete tool, S1 rejects them) and to read the resolved `params` mapping directly for A6 parameter-read tracking.
- `scripts/promote_engine.py`, `agents/analysis_backend.py`, `toolkit/analysis/observed_targets.py`:
  - Cover all declared outputs in synthetic self-test build fixtures.
