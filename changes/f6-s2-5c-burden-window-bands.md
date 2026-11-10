---
phase: "F6"
commit: "S2-5c α–δ"
status_group: "F6 Stage 2"
---

## Changelog

**Age-banded burden references under a model window (F6 S2-5c).** With `validation.burden.window`, a reference row whose `age_band` is a `decision_units.populations` group was refused. It is now scored: its model value is that group's window metric times the group's population, divided by the window's years (a rate per `per` people of that group), with the uncertainty draws' interval as for all-age rows. `window_metrics` may map only the population groups the rows reference: an all-ages row needs every group's metric, a banded row only its group's, and a row for a group with no metric is refused. The models record the window value per population group (`burden_group`) beside the all-ages total, which is kept only when every group is mapped. A row whose uncertainty draws lack a model value is unmatched (a finding that cannot be declared) instead of falling back to the point value. Specs and artifacts without a window are unchanged. Self-tests: `burden.py` BV41–BV42, `run.py` BE11.

## Status

- **S2-5c:** age-banded burden references are scored under a model window; window metrics may map only the groups the rows reference.
