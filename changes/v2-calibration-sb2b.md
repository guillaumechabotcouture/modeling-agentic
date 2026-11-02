---
phase: "v2 D"
commit: "β follow-up"
---

## Changelog

**Population Monte Carlo stages for curved ridges (SB2b follow-up to v2d β).** The importance sampler's adaptive stages now draw from a mixture: a defensive Gaussian over the weighted cloud, plus local Gaussians at 256 points resampled by weight, at four scales. Each stage's density is cached for the balance heuristic, and up to 12 stages are allowed. The ESS floor and the coverage bands are unchanged. Test CB8 (a curved 5-parameter ridge, written failing first) went from ESS 97 to 2,082. On SB2b, 199 of 200 fits now complete (was 57) and attack-rate coverage is 0.915. All ten coverage cases were re-run: 9 in band, unchanged. SB2b still fails its class check (reporting prior-dominated in 0.465 of replicates, against 0.90 required), because ridge loadings in prior-sd units let the narrower s0 and R0 priors absorb the four-parameter ridge. A log-scale loading is proposed in the design note for the owner's decision. VM spend 61.5 credits (about US$3.75).
