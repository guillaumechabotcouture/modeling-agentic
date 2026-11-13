---
phase: "F6"
commit: "engine v0.17"
status_group: "F6 Stage 2"
---

## Changelog

**Malaria engine v0.17: the net-type relative effects are engine parameters, and the trial fit that sets their ranges is designed (not run).** Up to v0.16 the Churcher 2016 hut-trial coefficients and the decay of PBO's advantage were module constants, so the uncertainty step never drew them. v0.17 adds three parameters (kind `fixed`). `net_kill_shift` is a shift on Churcher's hut-mortality intercept α1 for every net type: deterrence, feeding, killing and insecticide decay then follow from the hut mortality through the paper's own equations, so their correlation is kept. `pbo_gain_scale` scales PBO's gain in bioassay-mortality logit (Equation 4). `pbo_adv_decay` fades a PBO net's advantage over a same-age pyrethroid net. At the defaults (0, 1, 0) the outputs are v0.16's: V31 matches a frozen copy of the v0.16 net code bit for bit, and one engine run_batch (every net type, IRS, a past PBO campaign) has identical output digests under the v0.16 and v0.17 trees. The `restamp_proof.py` proof and the re-stamp of the evidence are pending. `priors.yaml` draws `net_kill_shift` over Churcher 2016's 95% CrI for α1 (0.012–1.29, Results), re-centred on 0.63. The two PBO parameters are fixed, with the reason that the trial fit sets their range. `benchmarks/net_effect_fit.py` is a 5-dimensional joint-v1 fit (the three parameters, cfp_kill and irs_kill) against B9, B9b, B9c, B10, B11, B12, B12b and B12m. It reports Muleba's and Misungwi's PBO regions and their union, because B9b and B9c disagree on durability. It needs 6,644 arm runs, about $15–30, and refuses to run without the owner's approval; a local smoke ran. B7/B8 at v0.16: B7 0.759 is in band, and B8 0.665 is out on the side of too little effect. The card's rows still showed v0.14 (0.50, 0.41); v0.17 rewrites them. A local probe shows that no net-use change brings both into band: the trials cut incidence far more than prevalence (0.55 against 0.83), and the engine does not. B7/B8 stay held out, and a per-trial Cochrane setting is proposed (ENGINE_NOTES.md, v0.17). Self-tests written first: V31–V35 (`vector.py`), NE0–NE3 (`selftest_late.py`), F1–F7 (`net_effect_fit.py`).

## Status

- **Engine v0.17 (net-effect ranges):** the net-type relative effects are engine parameters; the kill shift is drawn over the hut-trial range, and the PBO gain and durability wait for the trial fit (designed, about $15–30, owner approval pending).
