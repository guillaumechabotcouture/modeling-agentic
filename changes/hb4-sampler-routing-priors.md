---
phase: "Harness baseline hb4"
commit: "mode-start wiring for estimates/panel posteriors, presentation schema-repair routing, and prior-bound diagnostics"
---

## Changelog

**Wire mode-started adaptive Metropolis into estimates and count-panel posteriors, route presentation schema errors back to `PRESENTATION`, and detect boundary-clipped posterior modes.**

- `toolkit/analysis/mode_start.py` (`fit_posterior_panel`, `near_bounds`), `toolkit/analysis/core.py`, and `toolkit/analysis/estimates.py` (`_sample`): route count-panel `posterior` calibrations and high-dimensional / ESS-fallback `estimates` posteriors through mode-started per-chain adaptive Metropolis (`mode_start.sample`) instead of prior-start Metropolis (without modifying `toolkit/analysis/calibration.py`), and eliminate redundant 3× Metropolis retries when `len(P.names) > max_importance_params_run_batch`. Self-tests `MS10`, `MS11`, `AE30`.
- `toolkit/analysis/observed_targets.py`, `toolkit/analysis/mode_start.py`, and `toolkit/analysis/estimates.py`: populate `diagnostics.at_bound` using `mode_start.near_bounds` (flagging non-prior-dominated posterior modes within 2% of a finite prior bound in natural coordinates, catching `z`-space MAP fits pressed against a prior cap) instead of hardcoding `at_bound: []`. Self-test `OT26`.
- `agents/stage_machine.py` (`EDGES`, `h_GATE`, `h_CRITIQUE`, `h_PRESENTATION`): match critique filenames anywhere in schema-error strings (including `"invalid YAML in .../<file>: ..."`), route presentation-only schema errors directly back to `PRESENTATION` with their error feedback (using the write-up reserve) instead of re-running domain/red-team critics, include `critique-presentation` on post-write wildcard fallback, and keep `schema_feedback` intact on budget pause. Self-test `T37b`.
- `toolkit/engines/compartmental/attribute_binding.yaml` and `agents/v2_sdk_tasks.py`: instruct spec authors to check observed target direction and spread across all units before copying a card calibration prior range (widening when observations approach or exceed either bound), and remind critics to quote YAML scalars containing `: `.
