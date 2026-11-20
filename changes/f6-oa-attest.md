---
phase: "F6"
commit: "OA-AT α"
---

## Changelog

**A closed analysis schema, and the sign-off sheet attests the base outcome adjustments (F6 run 4, owner R3; external review points a and d).**
- An `analysis` key the code does not read (`schema.ANALYSIS_KEYS`) is now refused at sign-off. A misspelt or unsupported signed field can no longer be a silent no-op. Every one of the repository's 73 decision specs, and F6's, uses only known keys.
- With `analysis.outcome_adjustments`, the sheet gets a section "Base outcome adjustments", listing each adjustment's id, component, value, reference and source. A spec without the field gets no new section, so existing signed sheets are unchanged.
- Self-tests SP-AK1 (schema) and SS-OA1 (signoff) were written first and failed.
