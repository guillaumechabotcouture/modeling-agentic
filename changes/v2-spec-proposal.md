---
phase: "F6"
commit: "SP α–β"
---

## Changelog

**A SPEC rethink never overwrites the signed spec (F6 run 3, gap 2).** The rethink drafts a proposal in `proposals/<revision_id>/` inside the run dir, under a narrow lib.safe_io exemption for that one subtree. It is seeded with no link followed, and a manifest of path and sha256 is recorded once the draft validates. SIGN promotes the proposal only once the owner has signed it. Promotion runs through a recorded commit point (`promotion.json`: prepared, archived, committed), keeps the earlier spec in `spec_history/<k>/`, and publishes only the files in the manifest, verified against their digests.
