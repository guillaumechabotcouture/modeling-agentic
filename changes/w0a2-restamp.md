---
phase: "W0"
commit: "A2 α–β"
---

## Changelog

**One-time restamp of batches stored under the whole-tree key (W0-A2).** `python3 scripts/compute.py restamp <run_dir> [--launch-tree DIR] [--dry-run]` decides, for each batch summary stored under a pre-W0-A key, whether its rows may be reused under the narrowed key. A batch is accepted only when (A) its stored old key equals the old key (`_legacy_fingerprint`) of the code at its launch tree (a tree given, or the launch commit in `metadata.json` when this repository has it; otherwise refused as "launch code not provable"), and (B) every file the new key holds (the closure, or the whole tree when the closure is not computed, and the dependency manifests) is the same there as here. Key code (`FP_KEY_CODE`) is left out of (B), and so are `compute._run_map` and `batch.run`, but those two only in their recorded forms (`FP_RESTAMP_FORMS`, checked by a self-test). Every decision is written to the summary as `restamp`. `run_map` and `reuse-check` reuse an accepted batch while its recorded key parts still hold; the engine-provenance rule for old rows still applies. Tests: `FP_RESTAMP_DRY_RUN`, `_ACCEPTED`, `_KEY_CODE`, `_CLOSURE_DIFFERS`, `_OLD_KEY`, `_NO_LAUNCH_CODE`, `_FORMS`. Retro (read-only): F6 runs 5 and 3, 0 of 12 old-key batches restamped (launch code not provable; neither launch commit is in the repository and the runs store no code archive).
