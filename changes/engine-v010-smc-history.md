---
phase: "v2a"
commit: "v0.10 α–γ"
---

## Changelog

**Malaria engine v0.10: past SMC, and a net type for each past net campaign, as unit attributes (owner approval 2026-11-04).** Up to v0.9 there was no attribute for past SMC, so a calibration to a 2021 survey treated SMC-treated units as never having had SMC. Past net campaigns all used one net type (`hist_net`). There are three new optional unit attributes, each with a default declared in `attribute_binding.yaml`:
- `smc_hist_years`: a list of whole calendar years. Default `[]`.
- `smc_hist_rounds`: one whole number, or one per year; a year with n rounds gets the first n of the unit's SMC months. Default: every SMC month.
- `hist_net_by_year`: `{decimal year: pyr | pbo | dual}`, or the string `"2017.5:pyr 2020.5:pbo"`. Years must come from `itn_hist_years`. A campaign it does not name, and every routine campaign, keeps `hist_net`. Default none.

Past SMC uses the SMC intervention that the package's SMC uses, with the same parameters. It runs in every arm and is never charged. `schedule()` lists the past SMC rounds as `history`, and names each past campaign's own net type. A past round at or after the delivery start fails closed, and so does any malformed form (H4). With past SMC, `smc_pe` and `smc_hi_frac` join the burn-in key. A past dual-AI campaign in `hist_net_by_year` adds `cfp_kill` and `cfp_half` to the key. `toolkit/spec/schema.py` (disease-agnostic) lets a split column declare an empty-list token, `{column, split: " ", empty: "none"}`. A blank cell is still refused. With the attributes absent or empty, a run is byte-identical to v0.9 (self-test H3; `benchmarks/restamp_proof.py`, which gains a scenario setting the new attributes at their empty values). Tests: engine H0–H7 and schema SP8b–g, written first (H7 and SP8e–g from Codex review round 1). `benchmarks/restamp_proof.py` compared v0.9 (d24d28d) and v0.10 (2714296) and found 23 of 23 cases identical: 14 pack settings, 5 evidence cases and 4 pinned scenarios. The identity fields of the four evidence files were then re-stamped; the `validate_once.sh` confirmation is pending. The proof ran locally, so no Coiled credits were spent. Codex review: round 1 found 4 P1s (past SMC overlapping the package's SMC without a delivery period; a round run on a step at or after the delivery start; malformed SMC months; a null `empty` token) and round 2 found 1 P1 (falsy `smc_months`). Each was fixed test-first. Round 3 found no P1.
