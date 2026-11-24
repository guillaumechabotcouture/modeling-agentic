---
phase: "Harness baseline hb3"
commit: "flat observation-calibration refusal and cohort-survey / panel-dispersion guidance"
---

## Changelog

**Refuse flat state-only observation calibrations and clarify cohort-survey calibration and panel over-dispersion.** `toolkit/analysis/observed_targets.py` (`_state_targets_informed`, called from `problems()`) now checks at spec-validation time that when every observation target measures pre-epidemic state (`immunity` or `susceptible`), at least one calibrated engine parameter or per-unit attribute actually moves `s_start` on an observed group inside the declared prior; otherwise validation refuses the spec instead of running a flat, no-op likelihood (e.g., binding raw small-sample survey proportions as fixed `s0` while calibrating only transmission parameters). Clarifies Principle 5 (`.claude/orchestration/decision_design_principles.md`), `.claude/skills/decision-spec/SKILL.md`, and `agents/v2_sdk_tasks.py` (`draft_spec`) to calibrate an administrative cohort series (`initialiser: cohort_history`, per-unit `coverage_scale`) against finite-sample binomial survey observations rather than plugging raw survey ratios as fixed point values, and to estimate empirical over-dispersion `max(1, chi2 / df)` across multi-round panels for `baseline.sampling.dispersion`. Self-test `OT25`.
