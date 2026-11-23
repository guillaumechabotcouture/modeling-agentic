---
phase: "v2"
commit: "AE0"
---

## Changelog

**Design note: agent-built engines, admitted by harness checks (no code).** `docs/design/agent-built-engines.md`. When no registered engine fits a question, the signed spec may carry an engine request (`engine.build`: the interface, parameter ranges and sources, declared directions, conservation rule and reductions to known models). After SIGN, new states ENGINE_BUILD, ENGINE_ADMIT, ENGINE_REVIEW and ENGINE_SIGN have a bounded task write a Starsim module under `models/ext/engine_<id>/` (file tools plus a harness try tool, no shell), harness code admit it (static checks, contract conformance, determinism, batch equals run, oracle reductions, mass balance, sensitivity wiring, declared directions, dossier trial benchmarks, identifiability by simulation-based calibration, an independent reference build), using checks generated from the signed spec with public and hidden draws, and the owner sign the admission sheet. A harness adapter (`toolkit/engines/built.py`) runs the module in the sandbox and refuses any file change. The note covers identity and reuse, a promotion rule to the registry, failure routing, caps, smoke markers, tests BE1-BE37, a rollout α-ε and 16 open questions for the owner. Revised after one Codex round (42 findings folded in).
