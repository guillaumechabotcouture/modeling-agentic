---
phase: "v2"
commit: "CEA"
---

## Changelog

**Cost-effectiveness deliverable design note (docs only).** `docs/design/cost-effectiveness-deliverable.md` designs a `cost_effectiveness` deliverable kind and an add-on section for allocation runs, every number computed by harness code from engine outputs and the signed spec: the spec section (strategies, reference, perspectives, time horizon, separate discount rates, health measure composed from engine outcomes with sourced weights, thresholds, consequence costs, payers, negotiated prices); incremental analysis with dominance and extended dominance, NMB, a degenerate-ratio guard enforced in the module, the claims spec and render_claims; price grid and threshold price by Brent's method bracketed by evaluated prices; CE plane, CEAC, CEAF and EVPI over the uncertainty step's own draws; decision tests with a chosen-strategy criterion and switching values; claims and figures; composition with allocation; 15 acceptance tests (hand-built and textbook cases); a seven-PR build plan; 16 open questions with recommended defaults. No code changes.
