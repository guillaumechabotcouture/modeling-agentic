---
phase: "v2 D"
commit: "β follow-up 2"
---

## Changelog

**Identifiability ridge loadings on each parameter's log scale (owner-approved follow-up to the SB2b fix).** Likelihood-class ridge loadings are measured on log x, log(1 + x) or x, depending on the parameter's support, instead of in prior-sd units. That makes the classes independent of the prior, as v2d §3.2 intends. The Fisher point is polished to the likelihood maximum, and the cut-offs are unchanged. CB9 (the SB2b misclassification) was written failing first, and the CB10 guard passes before and after. All ten coverage cases were re-run, with coverage unchanged. SB2b's class check now passes: reporting prior-dominated 0.98, ridge share 0.97 (was 0.465 and 0.34). The case is still flagged out of band by its one fit below the ESS floor. SB3a's coverage-to-immunity ratio is now prior-dominated in 200/200, because it is pinned only through the declared assay-sensitivity range, so SB3a fails its `identified` expectation. Restating the expectation, or treating a validated assay range as data, is an owner decision (design note, addendum "log-scale ridge loadings measured"). 8 of 10 cases are in band (9 before). VM spend 62.4 credits (about US$3.81).
