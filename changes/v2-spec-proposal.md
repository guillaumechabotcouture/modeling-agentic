---
phase: "F6"
commit: "SP α–ε"
---

## Changelog

**A SPEC rethink never overwrites the signed spec (F6 run 3, gap 2).** When the spec has been signed (a sign-off record exists), SPEC no longer redrafts it. The machine writes `spec_rethink_request.json` and pauses as `paused:awaiting_spec_amendment` (exit 2). The request holds the latest fix plan's rethink briefs, the open findings the spec owns with their remedies, and the signed spec's sha256. The owner amends the spec and relaunches with reuse; a signed run dir cannot be re-signed in any case. A resume stays paused and drafts nothing. With `--signoff none`, nothing is signed, so the redraft stays in place as before. An in-run proposal with promotion was designed and built (#261, design note) and then dropped after its Codex review (9 P1); the PR description records why. Self-tests R3-2 and R3-2b failed first.
