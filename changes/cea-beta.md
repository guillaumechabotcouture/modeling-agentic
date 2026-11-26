---
phase: "Cost-effectiveness deliverable (PR beta)"
commit: "add probabilistic CEA (CEAC/CEAF/EVPI/quadrants), uncertainty on_draw/health_weights hooks, artifact/claims/results.md integration, render_claims .icer guard (CE4, CE6, CE8-CE10, CE15)"
---

## Changelog

**Second PR (`cea-beta`) of the cost-effectiveness deliverable (`docs/design/cost-effectiveness-deliverable.md` §§5.7–5.9, §7, §10–§11).**

- `toolkit/analysis/uncertainty.py`: add dedicated `HEALTH_WEIGHT_STREAM` for `health_weights` draws (leaving all other uncertainty streams unchanged) and extend `uncertainty.run` `on_draw` dispatch to pass `sampled` when a callback accepts 4 positional arguments while staying backwards-compatible with 3-argument hooks.
- `toolkit/analysis/cea.py`: add `probabilistic_summary()` (ΔC/ΔE intervals, quadrant shares, CEAC summing to 1, CEAF using expected NMB per Fenwick et al. 2001, EVPI with SE and per-person scaling, ICER at means under §5.4 guard, `min_draws` indicative status), `Collector` (hooks `uncertainty.run` without extra engine calls), `run_cea()` (full `analysis/cost_effectiveness.yaml` payload including price grids, threshold prices, perspective agreement, and undiscounted annual budget impact by payer), `claims()`, and `results_lines()`. Self-tests `CE4` (Drummond et al. 2015 Table 4.5), `CE6` (Gray et al. 2011 Table 2.1 + CEAC), `CE8` (Fenwick et al. 2001), `CE9` (analytic normal CEAC), `CE10` (unit normal loss EVPI), and `CE15` (same-draws byte-identical uncertainty artifact + full claims/results render).
- `scripts/render_claims.py`: refuse planted `.icer` claims for dominated/degenerate strategies, non-positive ICERs, or `.icer` claims carrying intervals in both `load_claims()` and `load_claims_tolerant()` (`CE3_RCL`).
- `toolkit/analysis/artifacts.py`, `toolkit/analysis/claims.py`, `toolkit/analysis/run.py`: register `cost_effectiveness` artifact kind and wire CEA collection, artifact emission, claims ledger, and `results.md` rendering when `cost_effectiveness` is declared.
