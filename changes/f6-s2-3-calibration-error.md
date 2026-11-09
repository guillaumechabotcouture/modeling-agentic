---
phase: "F6"
commit: "S2-3"
status_group: "F6 Stage 2"
---

## Changelog

**Calibration error model, part 1 (F6 S2-3, C3).** A unit target with `bias_by: <units column>` in `calibration.joint.targets` has one latent offset per source and group, b ~ N(0, bias_sd²). Before, bias_sd² was added to every target's own variance. The offset is profiled in `toolkit/calibration/history_matching.py` the way the unit parameter is. A shared value is compatible with a group when one offset with |b| / sd within the cutoff leaves every unit of the group matchable. The offset grid is `bias_grid` points over ±cutoff sd, with half a step as slack. Blocks linked through units are profiled jointly, at most `bias_blocks_max` of them. The engine verification re-profiles the offsets on the engine's runs. The report gives each block's offset at the best point (`bias`). The best point's score weights each group of units once (`best_point_weighting: per_group`) and counts each offset once, so a large group no longer outweighs small ones by repetition. `calibration.joint.change` compares the change between two unit targets (two surveys) by group: observed, with the sampling sd and the shared offsets, against the engine at the best point, with its run noise. A wrong sign beyond `change.sign_z` blocks (`change_wrong_sign`) unless it is declared (mode `flag`, with a reason). With no verification runs the change cannot be judged, and it blocks (`change_not_judged`). Thresholds are in `toolkit/calibration/thresholds.yaml`. Tests: HM26 covers four groups with planted survey offsets. With independent errors, a value the within-group contrast rules out stays compatible and the region is wider; with shared offsets the truth is kept and that value is refused. HM27: the best point between a 10-unit and a 2-unit group sits at 0.51, not the per-unit 0.38. HM28: a wrong-sign group blocks, is declarable, and is not judged without verification. HM29 and JA15: refusals and the spec adapter. Part 2 (historical and future coverage as separate parameters, histories anchored to delivery surveys) needs an engine parameter for past coverage and follows separately.

## Status

- **S2-3 part 1:** one survey offset per source and group, a group-weighted best point, and the change between surveys by group with a threshold. The F6 spec declares `bias_by` and `change`. Part 2 (past and future coverage) waits on the engine.
