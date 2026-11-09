---
phase: "F6"
commit: "S1-5"
status_group: "F6 Stage 1"
---

## Changelog

**Decision summaries that show what moves (F6 S1-5).** Beside the share of units whose package changes, every decision-test value, every stability fold and every sensitivity end now reports (`toolkit/analysis/moves.py`): regret (the re-optimised objective minus the point plan's objective under the test), as a value and as a share of the re-optimised objective; the population of the units whose package changes (the spec's `options.populations.total` column), as a count and a share; the dollars moved between components (the re-solved plan's cost minus the point plan's, component by component, under the test's costs; dollars moved is the larger of the increases and the decreases, also as a share of the allocatable budget); and the units changing package by `decision_units.region` group. A test changes the plan on any of three criteria, each against its threshold in `toolkit/analysis/thresholds.yaml` (proposals for the owner: share 10%, kept; regret 1% of the objective; dollars moved 5% of the allocatable budget), which the spec's `analysis.share_threshold`, `analysis.regret_threshold` and `analysis.dollars_threshold` override; `triggered_by` names the criteria that fired. Stability reports, per component, the held-out units where the component is in both the point and the fold plan (stable provision) apart from those where it is in neither (stable absence), and the worst fold's regret, dollars moved and population changed. The cost tables carry their split by component (`core.CostTable`). With `uncertainty.affordability.enforce`, re-optimisations still use the reserved budget and envelope tests `A.planned()`. The fields reach the decision-test result files, the decision_tests artifact, the claims (`dt.<id>.regret_pct`, `.dollars`, `.population`, `.group<k>`; `stability.*`), `results.md` (decision tests, stability, the sensitivity table) and the decision-test figure (its table, and the criterion when it is not the share). The Phase 49 loader (`scripts/mechanism_manifest.py`) checks the verdict against every criterion a result file gives, and `triggered_by` against them.

## Status

- **S1-5:** decision tests, stability folds and sensitivity ends report regret, population and dollars moved by component and units by group; regret and dollars are criteria beside the share; stable provision apart from stable absence.
