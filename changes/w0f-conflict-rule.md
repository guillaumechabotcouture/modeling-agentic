---
phase: "v2 Wave 0"
commit: "W0-F"
status_group: "In review (v2 autonomy, Wave 0)"
---

## Changelog

**The conflict rule (owner default D5, 2026-11-21): a calibration target the run cannot meet is held out, declared and tested, and the run continues.** The signed spec states `conflicts: {policy: declare_and_test | stop, max_share_declared, never_declare, test_scales}`. The stage machine appends `policy: declare_and_test` to a new draft that states none, and a spec without the key keeps today's behaviour (stop). New `toolkit/analysis/conflicts.py`: when the joint calibration is blocked only by misses the declaration path understands (a unit no value matches, a group target the verification fails, a shared offset whose region-sample rows fail, with its row breakdown), each undeclared miss is declared automatically. `joint_calibration.unmatched_decision` then decides with those declarations added, so its consistency checks still hold. Each declared miss gets two automatic `calibration_target` decision tests, `conflict_<n>_<key>_lo|hi`: its units' calibration targets are moved to each end of `test_scales` and the plan is re-solved. `analysis/conflicts.yaml` records each held-out target, why, its tests and their verdicts. The findings ledger carries each as a HIGH `evidence_conflict_declared` finding: open for the owner when a test changes the plan or did not run, else declared. The stage machine pauses once at OWNER_DECISION for the open ones, batched with any owner amendments, and the report lists every held-out target. Limits that stay hard: a `never_declare` target, more conflicts of a kind than its share (at least one may always be declared), and any other block. Fit acceptance (the addendum's effect-fit half) waits for W0-C's acceptance record.

## Status

- **W0-F:** conflict rule, declare and test and continue, under `conflicts.policy` (owner default D5); calibration path; fit-acceptance path after W0-C; in review.
