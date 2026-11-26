---
phase: "v2 SPEC validation diagnostics"
commit: "reject unknown decision_inputs fields in engine.build validate_build and avoid early return on constraints.envelope_usd so all spec errors surface together on Try 1"
---

## Changelog

- `toolkit/engines/built/requested.py`: check `decision_inputs` items against `decision_inputs.INPUT_FIELDS` during `validate_build()` (catching YAML flow-mapping typos such as unquoted commas immediately on Try 1 instead of after pre-sign checks pass) and test it in `BE24`.
- `toolkit/spec/schema.py`: fix the docstring example `envelope_usd: 1.0e6` (which PyYAML 1.1 parses as a string) to `1000000`, add a YAML 1.1 hint when `envelope_usd` is a string, and avoid short-circuiting `_validate()` before calibration/uncertainty/analysis checks when only scalar `constraints` fields fail.
