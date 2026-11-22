---
phase: "v2 Wave 0"
commit: "W0-C (i)"
status_group: "In review (v2 autonomy, Wave 0)"
---

## Changelog

**Effect-fit core: fit intervention-effect parameters jointly to trial results, each at its trial's own estimand.** New `toolkit/engines/effect_fit.py`, with no disease, intervention or parameter names. An engine pack declares the fit in data (`effect_fit.yaml`, schema `effect_fit/v1`): its training targets, each with the estimand fields `contrast` (treatment and comparator arm), `outcome`, `scale`, `window`, `population` and `measurement`; monitored benchmarks; the parameters with their boxes and sources; confounded pairs; the design waves; the history-matching cutoff; and the acceptance rule with its level, verification count, seeds, sample size and control-arm tolerance. Nothing has a module default. The estimand fields are checked against the pack: the contrast must be the benchmark's arms, the scale its estimand type, and the window the one its outcome is read over. The method is the one the malaria net-effect fit used: Latin-hypercube waves, a quadratic surrogate of each target's log value with its leave-one-out error, candidates restricted to the waves' boxes, and an implausibility per target (the signed distance outside the band in leave-one-out units) against the declared cutoff. Verified points from the region and the chosen point are judged by the seed-interval rule. A target no candidate can meet is recorded as a conflict and the fit continues (`accepted_with_conflicts`, for the conflict rule of #312), unless it is declared `must_meet`. The record (`write_record`, read back only by its sha256) holds the point, the joint region with `sample_status`, per-target implausibility and verified intervals, failures, conflicts, target digests, engine identity and the engine-row cache digest. Every engine row must hold one finite positive value per declared seed; control arms are judged against the pack's calibration targets; the cache is scoped by each target's resolved declaration (arms, settings, horizon, seeds, engine) and a required engine identity. Self-tests use a toy engine (EF_TOY_RECOVERS, EF_ESTIMAND_REQUIRED, EF_ESTIMAND_MATCH, EF_ACCEPTANCE_DECLARED, EF_SUPPORT_ONLY, EF_NOISE_RULE, EF_CONTROLS, EF_CONFLICT, EF_MONITOR, EF_CACHE, EF_RECORD_DIGEST, EF_SEED_ROWS, EF_VERIFY_N, EF_CHOICE). No stage is wired (W0-D).

## Status

- **W0-C (i):** effect-fit core and toy-engine self-tests; the malaria pack's declaration follows in W0-C (ii); in review.
