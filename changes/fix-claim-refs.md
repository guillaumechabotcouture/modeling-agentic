---
phase: "F6"
commit: "CR α–β"
---

## Changelog

**Claim references repaired inside WRITE, never at the round cap.** Harness baseline hb2: 3 of 8 runs ended RUN_FAILED (gate rule 5, post-write) on non-declarable claim-reference findings first seen at the post-write gate of the last round, so nothing could repair them. Three causes, each now prevented by the harness. (1) `render_report` appended finding text (claims and required remedies) holding critics' `[CLAIM:x]` and the placeholder `[CLAIM:id]` to report.md unrendered, and `report_claim_unrendered` read them as the writer's: `render_claims.quote_refs` now renders a ledger reference in the harness's own blocks as its value and turns any other into the words `claim x`. (2) The writer repeated a declared remedy's placeholder in full: `write_declared` and the write-up findings WRITE passes to the writer keep ledger references and turn the others into words. (3) The writer named ledger ids in inline code: `render_claims.normalise_refs` reads an inline span whose whole content is a known reference or a known dotted/underscored id as that reference, in both the renderer and `report_claim_findings`. Anything left (an id outside the ledger, a bare id in prose, a reference in a fenced block) goes back to the writer inside WRITE (`Machine._repair_claim_refs`, at most `MAX_CLAIM_REPAIRS` = 2 sessions, history entries `claim_repair`), which costs no round. The gate still refuses whatever remains (non-declarable HIGH). Self-tests CR1-CR5; T54 now uses a fenced block.
