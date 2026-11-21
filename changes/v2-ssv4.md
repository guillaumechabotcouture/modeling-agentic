---
phase: "v2-ssv4"
commit: "α"
---

## Changelog

**v2's engines on Starsim v4.** The engines that used Starsim 3 now use Starsim v4 (`starsim==4.0.0rc6`, `sciris==3.7.0`). The compartmental engine (v0.3), until now one Starsim 3 module holding numpy arrays, is a declared Starsim v4 disease: a chain of one-step infection-age states (`S`, `I1..IL`, `R`), stratified by batch member (independent places) and group, mixing through `ss.contacts()`, run whole by the ODE solver's new map integrator (`ss.solvers.ode(integrator='map')`, added to Starsim for this), in one compiled call. Its benchmark pack keeps every status, with values equal to rounding; its self-tests run in 2.6 s and 13.5 s (v3: 12 s and 36 s). The renewal engine stays numpy: on v4 it was 6 times slower per call for no gain, so the same model as a declared v4 disease is kept as a check that agrees to 3e-15 (`toolkit/engines/renewal/benchmarks/starsim_v4.py`). The malaria engine is translated to v4 (see its `ENGINE_NOTES.md`). The empirical engine runs no simulation and is unchanged; the v1 pipeline is out of scope. Plan and decisions: `docs/design/v2-ssv4.md`, `v2-ssv4-decisions.md`.
