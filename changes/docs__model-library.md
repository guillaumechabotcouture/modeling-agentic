---
phase: "v2"
commit: "ML"
---

## Changelog

**Model library design note (docs only).** `docs/design/model-library.md` records the direction the owner approved on 2026-11-05: four layers (core, engine families, domain packs, questions and runs) and what belongs in each; the promotion rule (each run's postmortem tags every lesson core, engine, domain or question, and a lesson becomes core only once it has appeared in two disease areas); three parallel tracks (the Nigeria malaria flagship, owner-signed; the eval suite, lead-signed, with engines in the order age-structured compartmental, renewal, TB; nightly canary regression on Coiled, where a core change that breaks a question does not merge); four acceptance criteria for every core change; the F6 factory package in priority order (canary pipeline, data contracts at pre-sign, joint calibration, incremental rebuilds, one golden launch path, postmortems and delivery metrics); and the counts from 2026-11-03 to 2026-11-05 that motivate it. `docs/design/improvement-plan.md` gains a pointer and status rows L1-L8. No code changes.
