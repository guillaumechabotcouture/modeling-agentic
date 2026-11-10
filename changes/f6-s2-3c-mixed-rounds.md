---
phase: "F6"
commit: "S2-3c"
status_group: "F6 Stage 2"
---

## Changelog

**A group round paired with a unit round of one survey (F6 S2-3c, C3).** Before, `change_pairs` (`toolkit/analysis/joint_calibration.py`) recorded a group target and a unit target of one `survey` as not computable. That is critique C3's own case: a state survey with its own interval against the next round's small-area surface with a shared per-(source, group) offset. Such a pair is now compared per group of the group round. The unit round is aggregated with the group round's own members and weights (the group target as built). When `changes.weights` names another column, the entry states that the group round's weights were used. The change is the later round minus the earlier, on the targets' scale. Its sd sums the group round's sampling variance, the aggregated unit sampling variance (independent units, weighted), the unit round's offset for the group (added in full through the signed coefficients, since nothing cancels it across programmes) and paired run noise. `change_stats` and `change_run_var` (`toolkit/calibration/history_matching.py`) take terms with one side, and the run noise sums each member's coefficient per side before pairing. A unit round grouped by another column than the group round's is not computable ("the groups do not match"). The gate's rebuild (`expected_change_ids`) covers the new pairs. Tests: HM45 covers known changes, the offset dominating the sd, paired run noise and the refusals. JA21 covers pairing from the spec with population weights, differing weights stated, mismatched groupings, and a toy run whose pairs the gate's rebuild matches.

## Status

- **S2-3c:** a group round and a unit round of one survey are compared by group, the unit round aggregated with the group round's weights, its shared offset counted in full.
