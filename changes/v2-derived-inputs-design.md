---
phase: "v2"
commit: "DI0"
---

## Changelog

**Design note: harness-run derivations for the units table and the spec's data (no code).** `docs/design/v2-derived-inputs.md`. In the full v2 run of 2026-11-01, the planner task (file tools only, no shell) wrote a 309-row units table by hand. It left out 4 groups of engine attributes, which then fell back to engine defaults with no message. It also dropped 2 constraint inputs, used a calibration target with no frame adjustment, and put a prior on a unit attribute; the validator never ran on its work. The note proposes that agents declare derivations (staged inputs plus a pure script) and the harness runs them in a sandbox (no network, read-only inputs, resource caps, run twice for determinism), recording input, script and output hashes. The spec may read only derived or staged files (`units_not_derived`, `data_ref_not_derived`, `derivation_stale`). Every engine attribute is bound to a column or to a default the owner can see, per candidate engine and before SIGN (`engine_attribute_unbound`). Priors may target engine parameters only (`prior_target_not_parameter`), with uncertainty in an attribute handled by the observation model (v2d §3.4, `target_frame_unmapped`). Every input column is either used or ignored with a reason (`input_column_unaccounted`, `ignored_column_used`). Alternatives considered: a shell for agents, more prompt text, and a fixed per-question script. Tests DI1-DI14; build plan α-δ; four open questions for the owner.
