---
phase: "v2 pre-sign checks"
commit: "follow-up window"
---

## Changelog

**The sign-off sheet flags an effect input whose evidence followed the effect for less time than the objective window.** A dossier observation may state `follow_up_years`, an optional field. `toolkit/spec/spec_critique.py` adds the check `follow_up_window` (principle 3): when any observation of a decision input declares a follow-up shorter than `objective.horizon`, the sheet shows a MEDIUM row for the owner's judgment, naming the input, the source and the follow-up. It covers how the effect is assumed to hold or wane beyond the follow-up, and whether a decision test varies it. It is a flag, never a refusal, and adds no estimand schema (owner direction 2026-11-18).
