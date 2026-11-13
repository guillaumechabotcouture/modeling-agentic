---
phase: "F6"
commit: "S2-PS"
status_group: "F6 Stage 2"
---

## Changelog

**Progressive steps design.** `docs/design/v2-progressive-steps.md`: a run answers the simple question first and builds up, each step with an exit test computed in code: arithmetic baseline (external burden × trial effect ÷ current price), engine against the trials (eligibility precondition), a one-node sweep over a feature box spanning the evidence dossier's ranges (emulator, flip boundaries, value of information), then the plan per unit compared with the baseline; a spatial model only when a test names unit independence as the weak point. The steps build a world model (`analysis/world_model.yaml`): a compact account of the decision's terms, branching points and rules, with numbers from the simulation and its fidelity to the full analysis; a learning loop after each step states hypotheses, ranks inputs by value of information, searches for data, tests, and updates it, pausing for the owner when the signed dossier would change. Design only; step 0 and the two preconditions ship in their own PRs.

## Status

- **S2-PS:** progressive steps design written (2026-11-12); step 0, evidence dossier and benchmark eligibility in progress.
