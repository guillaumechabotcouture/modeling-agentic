---
phase: "F6"
commit: "OA posterior bounds α–β"
---

## Changelog

**A posterior column's declared range is the posterior's (F6 option A refit b).** `toolkit/spec/preconditions.py` `_declared_range`, which a benchmark departure's `parameter_value` test must span, now gives a net-effect posterior column its posterior range: the sample's hull within its hard bounds, from `uncertainty.net_effect_sample`. Before, it used the drawn card prior. Refit b fits pbo_adv_decay up to 2.23 per year while priors.yaml keeps 0–1.895, so a test spanning the posterior could have been refused, and one spanning only the card prior accepted. Parameters that are not posterior columns keep the drawn prior, else the engine's range. A spec whose posterior cannot be loaded gives no range, so the test is refused (fail closed). Loading the posterior itself already uses the fit's ceiling (`net_effect_fit.hard_bounds`, #285). Self-test PC11.
