---
phase: "v2 ACQUIRE (PR beta)"
commit: "add versioned disease-agnostic source allowlist, SSRF-pinned Fetcher, quarantine/promoter, verify_inventory, safe_io descriptor helpers, and AQ1-AQ18 fixture tests"
---

## Changelog

**Core harness acquisition module (`acquire-beta`, `docs/design/acquire.md` §§3.2, 4, 7, 9.1–9.3).**

- `scripts/lib/safe_io.py`: add descriptor-relative `chmod_nofollow()` and `listdir_nofollow()` (`S18`) so quarantine/promotion permissions and directory listings never follow symlinks.
- `.claude/orchestration/acquire_sources.yaml`: version 1 disease-agnostic allowlist covering the six enabled v1 services (`who_gho`, `worldbank`, `dhs_api`, `europepmc`, `pubmed`, `ctgov`) with verified licence terms dated `2026-11-26`, typed parameter schemas, catalogue-code binding, and explicit exclusion of `un_wpp` and `ihme_gbd`.
- `agents/acquire.py`: implement fail-closed allowlist loader, typed parameter validator (including catalogue-code binding and bounded literature terms), SSRF-pinned stdlib HTTP(S) `Fetcher` (hop-by-hop template/redirect checks, single DNS resolution with public-IP enforcement and TLS SNI verification, zero env/proxy/`.netrc`/auth/cookie leakage, streaming size cap, digest-keyed idempotent resume), nine-step `promote()` (content-type sniffing, strict CSV/JSON/safe-XML parsing, per-record licence check, promotion intent recovery, atomic `0444` freeze, sanitized `_views/` schema generation, provenance & manifest emission), `verify_inventory()`, and local `AcquireFixtureServer` with self-tests `AQ1`–`AQ18`.
- `agents/stage_machine.py`: preserve `origin: "acquired"` entries and `acquired_inventory_sha256` across `stage_inputs()` (`AQ12`).
- `scripts/self_tests.py`: register `agents/acquire.py` in `AGENT_MODULES`.
