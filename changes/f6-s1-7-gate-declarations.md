---
phase: "F6"
commit: "S1-7"
status_group: "F6 Stage 1"
---

## Changelog

**Gate and declarations (F6 S1-7).** Declared findings keep their full claim and required remedy: the v2 machine cut each open finding's claim at 300 characters (`agents/stage_machine.py:open_findings`) and never carried the critic's `fix_requires`, so `declared_findings.yaml` and the report's open-findings list ended mid-sentence with no remedy. Each declared entry now has a separate short `label` beside the full `claim`, `required_remedy` and `tested`; the report lists each in full with its remedy, also for `scope_declaration.yaml` entries that state one. The ledger (`findings.jsonl`), the owner's scope sign-off items, the done_when registry, the gate's `unresolved_high` (now with `fix_requires`) and the persistence escalation's quoted claim no longer truncate. A done_when test whose result is null is reported as `not_judged` (its detail opens "not judged: …", not "pass …"), and the gate records `check_summary` (passed, failed, not judged, withdrawn; `required_not_judged`); a required test that is not judged cannot yield ACCEPT (v2 rule 3j declares it; the STAGE 7 validator adds an accept blocker). `report.md` opens with a harness status line ("Run status: declared scope, not accepted." for `completed_scope_declared`), re-rendered at every post-write gate. Acceptance language ("accepted", "validated", "conclusively", "definitive", "proven", "passed the gate", "approved plan") is listed in `.claude/orchestration/acceptance_language.yaml`, with negations and allowed owner phrases; `scripts/writer_qa.py` flags it in a report whose run was not accepted, the v2 gate raises `report_acceptance_language` (MEDIUM) for a declared or not-accepted write-up, and `final_gate.json` records any use in a report the final gate did not accept. Retro-check on the local measles run 2026-10-20_213310: 31 open findings, 20 with claims over 300 characters (cut before), all 31 kept in full with their remedies; on the accepted malaria run 2026-05-03_235631 the check finds 3 uses of "validated" that a declared-scope report could not contain.

## Status

- **S1-7:** declared findings in full with their remedy; not judged is not a pass; no acceptance language in a declared-scope report.
