---
phase: "Agent-built engines (#363 follow-up: categorical component A7 & posterior prior-dominated A10)"
commit: "compare categorical component modes against absent in A6/A7 and recognize automatic posterior prior_range coverage in ENGINE_ADMIT A10 routing"
---

## Changelog

- `toolkit/engines/built/admission.py`:
  - In `check_a6_a7()`, evaluate non-numeric (categorical string) components by comparing every active mode against the component's declared `absent` value (independent of list order), passing `+`/`-` when at least one active mode moves in the declared direction and none moves opposite, while preserving strict step-by-step monotonicity for ordered numeric inputs.
  - Add self-test `BE_A7_CAT` covering order-independent categorical components, mixed-sign failure, and preserved numeric monotonicity failure.
- `agents/stage_machine.py`:
  - In `h_ENGINE_ADMIT`, recognize that under `calibration.method: posterior`, `prior_dominated` parameters with declared `calibration.priors` are already automatically covered by `toolkit/analysis/calibration.py:prior_range_tests` during `ANALYZE` (and cannot be declared manually as `prior_range_*` in `decision_spec.yaml`), extract parameter names from `perturb` decision tests as well, and only route A10 uncovered parameters to `SPEC` when gating admission checks passed.
  - Add self-test `BE_A10_PRIOR` verifying `posterior` `prior_dominated` proceeds to `ENGINE_REVIEW` while non-posterior `unidentified` still routes to `SPEC`.
- `agents/v2_sdk_tasks.py`:
  - Clarify in `draft_spec` to use string labels rather than integer codes for unordered multi-mode `request.components`.
