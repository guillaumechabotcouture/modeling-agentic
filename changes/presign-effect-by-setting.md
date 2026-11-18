---
phase: "v2 pre-sign checks"
commit: "effect by setting"
---

## Changelog

**The sign-off sheet warns where a unit's setting lies outside every trial setting behind a component's effect.** New `toolkit/engines/effect_settings.py` takes, for each spec component an allowed package holds, the effect benchmarks that cover it (`benchmark_coverage.yaml`, cost benchmarks excluded). It reads the arms their estimand names (num, den, interaction arms, `arms`, value or identity sides) and those arms' settings' unit attributes in the engine's pack. These give the trials' covered range per attribute: per key for a mapping, with a scalar setting value counting for every key the units table binds. It then lists the units whose bound column value lies outside, with the population share and its column, and whether a `unit_attribute` decision test on that column reaches them. An attribute that every pack setting states with one value is a pack convention and is skipped. An engine without a pack is reported as not compared. It is a warning for the owner (owner direction 2026-11-18): it refuses nothing, registers no precondition and adds no spec key. No engine runs.
