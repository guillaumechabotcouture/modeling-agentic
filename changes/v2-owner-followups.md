---
phase: "F6"
commit: "R3 follow-up α–β"
---

## Changelog

**Owner-path follow-ups to #258 and #261.** R3-8 is a dedicated self-test for fix 4 of Codex round 3 on #258. It drives the real gate step and shows that the decline override reads the ledger synced with this gate's decision: a gate-supplied finding that the owner declined, restated with a revised claim, is no longer held by the old decline. R3-8 fails if the override syncs with `decision=None`. The amendment pause now notes when the spec's sign-off record is missing or unreadable (`signing_evidence` in `spec_rethink_request.json`, with a warning in the log). This is a warning only and never blocks the run. Self-test R3-2f failed first.
