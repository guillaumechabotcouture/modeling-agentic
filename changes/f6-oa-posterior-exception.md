---
phase: "F6"
commit: "OA posterior exception α–β"
---

## Changelog

**An owner-signed exception for a net-effect posterior whose verification recorded failures (owner, 2026-11-20).** A spec can now declare `uncertainty.net_effect_posterior.exception` with these fields:

- `failed_points`: each failed verified point, as `{point, targets}`;
- `rule`: the rule those points fail;
- `evidence`: what else was verified;
- `reason`;
- `draws`: `keep` or `exclude`, whether the failed points stay in the draws.

`net_effect_fit.load_posterior` accepts an invalid sample only under such an exception, and only when the exception exactly matches the file's recorded failures: the same points (to the file's 6-decimal rounding) failing on the same targets. The rest of the evidence must still pass the 14 Nov rule as it stands. The chosen point, the other verified points, calibration and the grid edge are all replayed.

The exception travels with the posterior's range record, which the manifest and the uncertainty report carry. The sign-off sheet shows it as a HIGH owner-signed departure.

Checked against refit b2's stored output, with no engine run: refused without the exception; 55 rows loaded with `keep`, 54 with `exclude`. Self-tests FE1 (net_effect_fit.py) and UE1 (uncertainty.py).
