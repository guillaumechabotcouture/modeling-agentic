---
phase: "F6"
commit: "OA3 α–β"
---

## Changelog

**Net-effect fit on the net trials at their own estimands (option A, step 3).** `toolkit/engines/malaria/benchmarks/pack.yaml` makes the PBO trials B9, B9b and B9c, LLINEUP's B9d-B9f and Misungwi's dual-AI vs PBO contrast B9g fit targets, beside B10, B11, B12, B12b and B12m. B7, B8, B15 and the rest stay held out. `net_effect_fit.py` therefore fits net_kill_shift, pbo_gain_scale, pbo_adv_decay, cfp_kill, cfp_half and irs_kill (the PBO parameters join because a training arm now has a PBO net). The PBO boxes are their card priors (0.316-1.0; 0-1.895 per year). `run` is now a fresh fit, `OPTION_A`: the calibration grid in net_kill_shift is computed again with the LLINEUP site; wave 1 has 84 points (3 per quadratic coefficient in 6 dimensions) and wave 2 has 87, followed by the existing joint sample, verification and noise-aware acceptance rule. Earlier rounds' reuse is refused on this pack, because their fit declarations differ; their tests run on the pack as they ran it (`WAVE1_TARGETS`). The fit's cap is `OPTION_A["cap_usd"]`, set by the owner in code; `run` refuses while it is unset, and the printed launch carries a placeholder. The plan is 185 CPU-hours, about $50-75 at the measured price on 16 vCPUs (25-38 h) or $57-84 on 64 vCPUs (7-11 h). The stored pack fit (fitted.yaml) is now `refit required` until the pack is refitted (BS43). Self-tests FA1-FA4; F1-F4, F13, F19, F19c, F25, PT4 and BS43 updated.
