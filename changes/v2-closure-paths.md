---
phase: "F6"
commit: "closure paths α-β"
---

## Changelog

**A run dir given as a relative or an absolute path keys the signed input closure the same way.** Found on the F6 dry-run stand-in. `evidence_dossier.input_closure`, which writes the closure that the sign-off sheet records and that `signoff.preconditions_state` checks, resolved the spec's inputs against the spec's base. With a relative run dir (`runs/<R>`) that gave paths relative to the working directory, which were then joined to the run dir again. The closure keyed the same files as `run:runs/<R>/data/...` instead of `run:data/...`, so a spec signed through one path form had the status `changed` through the other (launchers, `main.py --resume`). `input_closure` now makes the run dir absolute before resolving, so every key is relative to the run dir in either form. It is one producer for the sheet, signing and status, so the fix covers all of them. Signatures made through an absolute run dir keep their keys exactly, so they verify as before. A package signed through a relative run dir holds the doubled keys; it reports `changed` in either form and must be re-signed, after the sheet is regenerated. SG-RD1 and SG-RD2 in `signoff.py` (sign relative and check absolute, and the reverse; run keys relative to the run dir) were written first and failed before the change (α-β).
