---
phase: "v2 pre-sign checks"
commit: "spec critique"
---

## Changelog

**The sign-off sheet opens with a spec-level critique against the decision design principles, so findings the gate raised only after paid compute are seen before signing.** New `toolkit/spec/spec_critique.py` runs four deterministic checks on the spec and the evidence dossier: `headline_comparator` (principle 2: a no-action comparator package makes the headline context only; a fixed_plan variant is named), `decision_date` (principle 2: a delivery window that began before today, HIGH; a past decision date, MEDIUM), `baseline_burden` (principle 3: no `validation.burden` anchor row, HIGH; only a `burden_scale` test, MEDIUM) and `decision_input_ranges` (principle 6: dossier inputs recorded with no data and held at a point that no test or draw varies, HIGH; evidenced point inputs and unvaried `engine_parameters` values, MEDIUM; cost items of allocated components with no uncertainty range or price test, MEDIUM). A new optional spec key `principle_departures: [{principle, check, reason}]` marks a finding declared; a malformed entry is HIGH. Findings are shown, not enforced: signing is not refused. Items that need judgment (whether a non-empty comparator is the funded plan, whether an anchor's estimand matches the objective) are labelled owner judgment; no LLM task runs. Retro-check on the F6 rebuilt spec reproduces the run's gate findings D-001, D-002 and R-001 as HIGH before signing.
