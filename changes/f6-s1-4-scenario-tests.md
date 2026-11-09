---
phase: "F6"
commit: "S1-4"
status_group: "F6 Stage 1"
---

## Changelog

**A decision test for every parameter the plan depends on (F6 S1-4).** A scenario engine parameter is a choice, not evidence: before signing, each one needs a declared range (`uncertainty.priors`, or a range on its `engine_parameters` value), so the draws vary it, it enters the emulator design and it gets the automatic prior_range decision test; or the spec excludes it by name with a reason (`uncertainty.excluded`). Otherwise the spec is refused (`scenario_without_range`, `toolkit/spec/priors.py`). Every fitted engine parameter the draws vary that is not a prior gets an automatic `fitted_range_<parameter>` decision test at each end of its range (its jointly calibrated range when it has one), and a scenario parameter excluded by name is listed as `scenario_excluded_<parameter>`, not run, with the spec's reason (`toolkit/analysis/decision_tests.py`). The spec cannot declare tests with these reserved ids. Retro-check on F5: its spec is refused for `cm_add` and `irs_coverage`; `fitted_range_cfp_kill` changes the plan (17.5% of units), `fitted_range_irs_kill` does not (3.9%).

## Status

- **S1-4:** scenario parameters need a range or a named exclusion; fitted parameters get automatic range tests.
