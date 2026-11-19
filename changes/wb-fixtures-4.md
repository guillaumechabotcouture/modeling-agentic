---
phase: "v2"
commit: "WB θ"
---

## Changelog

**Review workbench: fixtures and shared loaders for the Spec-and-evidence and Scenarios views.** `tools/workbench/fixtures.py` now writes a signed spec with a decision date, spec-set values, an evidence dossier, a sign-off sheet with every section the sign-off code always writes, archived earlier sign-offs, a stored schedule and a trajectory record with the harness's dynamics checks, shaped like the producers' files; 19 Spec and 8 Scenarios variants, each one edit, including contradictions and missing pieces (a signed spec that cannot be verified, an unparsable dossier, an archive folder with a sheet and no record, an unknown event kind, a trajectory record from another engine, an unknown dynamics status). The clean run holds no outcome adjustment; every case with one is a variant. `checks.py` gains shared loaders for the sign-off sheet, the sign-off history and the dossier, and the Steps check holds a file outside the manifest that records the spec among its inputs to the signed spec's hash, so a trajectory record can sit in a clean run. The specification records both views' models and the rules settled from acceptance passes on stored runs: only structured fields count (no prose is parsed for follow-up durations, checked marks or who decided), and a derived file can say a source is unverified but cannot vouch for it. Self-tests pass in every workbench module; no stored run's value is committed.
