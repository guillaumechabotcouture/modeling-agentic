---
phase: "F6"
commit: "S2-PS"
status_group: "F6 Stage 2"
---

## Changelog

**Progressive steps design.** `docs/design/v2-progressive-steps.md`: a run answers the simple question first and builds up, each step with an exit test computed in code: arithmetic baseline (external burden × trial effect ÷ current price), engine against the trials (eligibility precondition), a one-node sweep over a feature box spanning the evidence dossier's ranges (emulator, flip boundaries, value of information), then the plan per unit compared with the baseline; a spatial model only when a test names unit independence as the weak point. The steps build a world model (`analysis/world_model.yaml`), the modeller's working understanding made explicit: mechanisms, rough magnitudes, branching points and open questions, each with its basis (literature, data, simulation) and confidence, mostly right rather than precise. Running it yields hypotheses, priors and where to look; confronting it with papers, data and simulation refines it, with a track record of confirmed expectations; a learning loop after each step does this, pausing for the owner when the signed dossier would change. Design only; step 0 and the two preconditions ship in their own PRs.

## Status

- **S2-PS:** progressive steps design written (2026-11-12); step 0, evidence dossier and benchmark eligibility in progress.
