---
phase: "51"
commit: "β"
status_group: "Merged (Phase 51, development process)"
---

## Changelog

**One fuzz test over every gate check.** About a quarter of the P1 review findings on PRs #62-#81 were the same input-handling bugs found again one module at a time (undecodable bytes, integers too large for float or int-to-str, impossible YAML dates, recursive aliases, symlinks out of the run, empty/truncated/corrupt files, wrong top-level types, NaN/inf). `scripts/gate_fuzz.py` finds every `scripts/*.py` with a module-level `gate_findings(run_dir, ...)` by parsing the source (19 today), builds the positive-control run at round 3 and settles it (every check runs on it until no file changes, so first-seen state such as `acceptance_lock.json` exists before any mutation), records with a patched `open` which run files each check reads, and runs each check, in a forked child, on a copy of the run in which one of those files is corrupted by one of 12 bug classes (744 cases, about 15 s). It fails when a check raises, returns a non-list, times out, or fails open: the file is invalid and the check reports nothing it did not report on the clean run. Value mutations (NaN, inf, 10**400) count only where two finite-value probes change the check's findings; empty or large-number prose and code are checked for crashes only; a missing-file probe is reported, not asserted. The fixture is the positive control plus trigger artifacts for compute, decision_brief, pipeline and stall_rule, so every check gets cases; a check with none is a coverage failure. A `DELEGATES` table names an owner validator per check and file (e.g. claim_units -> build_claims_ledger for models/claims_spec.yaml), run on the same corrupt run; fail-open counts only when neither reports it. Known failures on main (236 cases, each identified by module, file, mutation and outcome; all 19 checks) are listed in `tests/gate_fuzz_expected.yaml`, so CI passes; a new failing case or a listed case that no longer fails stops CI. No check is changed here: the list is the work queue for the fail-closed I/O decision. Retro: on `67ee95d` (before Phase 45 β's Codex round 2) it reports `verdict_binding` raising UnicodeDecodeError, the P1 that round fixed.

## Status

- **β:** `scripts/gate_fuzz.py` runs 12 bad-input classes against every `gate_findings` check in CI; the 236 known failing cases (`tests/gate_fuzz_expected.yaml`) await the fail-closed I/O decision.
