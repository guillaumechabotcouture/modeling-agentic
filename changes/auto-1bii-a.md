---
phase: "v2 autonomy"
commit: "1B-ii(a) α–γ"
---

## Changelog

**v2 autonomy 1B-ii (a): the evidence-stage helpers, `toolkit/spec/evidence_draft.py` (owner default D1).** The first of two stacked PRs; the stage machine uses the helpers in 1B-ii (b). `assess` reports what a spec's options need, using the signer's own `evidence_dossier.required_inputs` and `check`. Its spec problems (a spec that needs inputs and names no dossier) go back to the draft; its dossier problems are the signer's. `scaffold` adds, for each required input with no entry, what execution uses: model value and source from the dossier module's execution view, and the engine's value as `chosen`, with the generated range test for a drawn parameter. It never writes observations or `no_data`, keeps entries already written, leaves a dossier that does not parse for the task to repair, refuses a link, and writes only `evidence_dossier.yaml` at the run root (never another file a spec names). `finish` writes `evidence/dossier_draft.json`: the inputs, the drafts, the problems left, the dossier's sha256 and the declared gaps (inputs with no data, a chosen value that differs from what execution uses, a value left at the engine's, a source the harness could not locate as a file). `report_block` renders who drafted and who signed the dossier, provisional unless the owner signed, and every gap; it shows none of the stage's gaps unless the record is current (`record_current`: the path the spec names now and the same sha256). Every write is through `lib.safe_io`. Self-tests EDR1-EDR13, EDR9b, EDR9c.
