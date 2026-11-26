---
phase: "v2 ACQUIRE (PR beta2)"
commit: "add narrowed minimal-mount derivation sandbox for acquired runs, refuse unsandboxed derivations on acquired inputs, and sanitize acquired derivation errors (DS1, DS2)"
---

## Changelog

**Narrowed derivation sandbox for acquired runs (`acquire-beta2`, `docs/design/acquire.md` §3.3, §9.1, §9.3).**

- `scripts/pipeline.py`:
  - Add `_has_acquired_inputs()` detecting acquired trees, provenance entries, or derivation inputs.
  - Add `_narrow_ro_prefixes()` and `_bwrap_narrow_cmd()` (DS1): when a run has acquired inputs, derivations execute inside a minimal-mount `bubblewrap` sandbox (NO `--ro-bind / /`; only Python runtime/libs, the launcher script, private `/tmp`, `work`, and declared read-only inputs).
  - Refuse `--derive-unsandboxed` (`unsandboxed_ok=True`) when a run has acquired inputs (`derivation_unsandboxed_acquired`, DS2).
  - Sanitize derivation failures on acquired inputs (`derivation_failed_on_acquired`) to report only exception class, script line number, and input path without quoting acquired stderr text (DS3).
  - Add self-tests `DS1`, `DS2`, and `DS3` to `_self_test_derivations`.
