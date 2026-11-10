---
phase: "F6"
commit: "engine v0.15"
status_group: "F6 Stage 2"
---

## Changelog

**Malaria engine v0.15: the biting rates carry the 1/W term of the feeding-cycle model (F6 engine review, finding 1).** In the feeding-cycle model (Griffin et al. 2010, PLoS Med, Protocol S2) a mosquito bites person i at the rate f Q0 π_i y_i / W and is infected at the rate f Q0 Σ π_i w_i c_i / W, where W is the mean probability that a feeding attempt ends in a blood meal the mosquito survives. From v0.2 to v0.14 the engine used f Q0 π_i y_i and f Q0 Σ π_i y_i c_i. Without nets or IRS, W is 1 and the rates were right. Under nets or IRS both were low by the factor W (0.52 to 0.76 in the self-test probes at 40% to 70% net use), while mosquito mortality and cycle length were already the published ones, so vector control had more effect in the model than in the published one. v0.15 has `vector.cycle` return W and divides both rates by it, with the mosquito side weighted by the bites the mosquito survives (w), as in Griffin 2010 and the Imperial College deterministic model code; `malariasimulation` differs on the mosquito side only (it does not weight each person's infectiousness by their own w), 0.89 to 1.04 times the implemented form in the probes. No parameter is added or removed. Measured: the model's lifetime human bites per mosquito were 0.52 to 0.75 of the exact per-attempt chain under nets and are 0.94 to 1.16 after the fix (0.90 with no intervention, before and after, which is the rate form's own approximation); in a one-seed illustration at the v0.14 fitted values, a past net campaign at 45% use leaves an annual EIR of 4.2 where v0.14 gave 2.4. Every output under vector control changes, so the benchmark evidence and the fitted values are v0.14's and are refused for v0.15 (`verified_results`: "computed for another engine identity or code"; fitted values: "refit required"); they are recomputed by the next fit and pack run, not re-stamped, and the engine self-tests V7 and V8 fail until then. The link to the held-out net-trial benchmarks B7 and B8 is to be measured by that refit; held-out benchmarks did not choose the form. Self-tests written first: V29, V30 (`vector.py`), BR0 to BR4 (`selftest_late.py`). Card and `ENGINE_NOTES.md` (v0.15 section) updated.

## Status

- **Engine v0.15 (engine review, finding 1):** the malaria engine's biting rates carry the published 1/W term, so nets and IRS no longer have more effect than in the published feeding-cycle model; the benchmark evidence is recomputed by the next refit.
