---
phase: "Harness baseline hb4"
commit: "mode-start wiring for estimates/panel posteriors, missed-mode probe, presentation schema-repair routing, and prior-bound diagnostics"
---

## Changelog

**Wire mode-started adaptive Metropolis with missed-mode verification into estimates and count-panel posteriors, route presentation schema errors back to `PRESENTATION`, and detect boundary-clipped posterior modes.**

- `toolkit/analysis/mode_start.py` (`find_modes`, `_probe_missed_modes`, `sample`, `fit_posterior_panel`, `near_bounds`), `toolkit/analysis/core.py`, `toolkit/analysis/observed_targets.py`, and `toolkit/analysis/estimates.py` (`_sample`): route count-panel `posterior` calibrations and high-dimensional / ESS-fallback `estimates` posteriors through mode-started per-chain adaptive Metropolis (`mode_start.sample`) without modifying `toolkit/analysis/calibration.py`, eliminate redundant 3× Metropolis retries above `max_importance_params_run_batch`, diversify mode starts, and add an independent 1024-point prior-space Sobol probe after MCMC convergence so narrow unsampled competing modes are discovered and retried or refused rather than silently missed. Self-tests `MS10`, `MS10b`, `MS11`, `MS12`, `AE30`.
- `toolkit/analysis/observed_targets.py`, `toolkit/analysis/mode_start.py`, and `toolkit/analysis/estimates.py`: populate `diagnostics.at_bound` using `mode_start.near_bounds` (per-end natural-bound + prior-tail CDF check so Beta spikes at one end do not mask opposite-end hits and prior-concentrated medians do not false-trigger on point-only fits; computed per contributing variant before structural pooling in `estimates.py`) instead of hardcoding `at_bound: []`. Self-tests `OT26`, `AE31`.
- `agents/stage_machine.py` (`EDGES`, `v2_gate`, `h_GATE`, `h_CRITIQUE`, `h_PRESENTATION`): record exact per-reviewer `schema_errors_by_reviewer` in `v2_gate`, route presentation-only schema errors directly back to `PRESENTATION` with error feedback (using the write-up reserve) without misrouting when an error body quotes another filename, include `critique-presentation` on post-write wildcard fallback, and keep `schema_feedback` intact on budget pause. Self-tests `T37b`, `T37c`.
- `toolkit/engines/compartmental/attribute_binding.yaml` and `agents/v2_sdk_tasks.py`: instruct spec authors to check observed target direction, spread, and attainable saturation across all units before copying a card calibration prior range, and remind critics to quote YAML scalars containing `: `.
