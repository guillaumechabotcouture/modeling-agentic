---
phase: "F6"
commit: "OA refit b α–δ"
---

## Changelog

**Option A net-effect refit b (owner, 2026-11-19).** The 18 Nov fit (f6-refit-optiona, rc 2, $117.51) found no parameter set reproducing Muleba's durable PBO effect together with LLINEUP's waning. B9b with B9f, and B9 with B9f, are jointly in band at 0/220 design points; B9 with B9b at 6/220, all with pbo_adv_decay 0.06–0.27, where B9f is 0.22–0.32. `net_effect_fit.py` OPTION_A therefore holds B9 and B9b out of the training targets with a stated departure. They are monitored: evaluated at the chosen and verified points, reported, and never used to choose. Refit b reuses the 18 Nov engine results from the stored checkpoint (sha256 pinned; `--resume-from` required). To keep its tasks identical:
- the calibration grid and wave 1 run on the design box as it ran;
- the wave-2 box is pinned to that box;
- a call that runs Muleba still runs the monitored arms.

`--margin-loo` is refused (owner). pbo_adv_decay's ceiling for this fit is 2.23 per year (owner, option (a): the 99% interval of LLINEUP's waning minus the engine's own waning at x = 0.30; `PAD_CEILING` sets the fit box and the posterior's hard bound; priors.yaml keeps 1.895 because its test sits inside the engine fingerprint), with 100 extension points in 1.895–2.23, and the output records whether the chosen value sits within 2% of the ceiling. `plan` prices only the new tasks: 10,944 arm runs, 3.3–4.8 h and $40–58 on 96 vCPUs. The launch ships the tree plus the checkpoint (`vm_job --dir`) and resumes from it. Job f6-refit-optiona-b, cap $100. Self-tests FB1–FB8; F4, F10, FA1, FA3, FA4, FA10–FA12 updated.
