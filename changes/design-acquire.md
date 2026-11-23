---
phase: "v2"
commit: "AQ0"
---

## Changelog

**Design note: an ACQUIRE state before SPEC, so a run can start from a bare policy question (no code).** `docs/design/acquire.md`. A bounded task chooses public data from a versioned source allowlist (`.claude/orchestration/acquire_sources.yaml`, with licence notes). The harness fetches it through its own tool (no general web tools, no shell, HTTPS to allowlisted hosts only, no credentials), checks, hashes and records each file in `data/inputs_provenance.json` (URL, time, sha256, size, type, licence, source, query), and promotes it read-only into `data/inputs/acquired/`. From SPEC on, nothing changes. Fetched text and the task's own notes never reach a later prompt; later tasks get a harness-built manifest. The owner sees an acquired-inputs table at SIGN, bound by sha256. Off by default (`--acquire`), and it does nothing when the question names its files, so eval runs are unchanged. The note covers failure routing (a new `paused:awaiting_inputs`), caps, tests AQ1-AQ21, S_ACQ_*, T_ACQ_*, smoke markers, a rollout α-ε, and 11 open questions for the owner.
