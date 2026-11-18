---
phase: "v2 simplification"
commit: "S1"
---

## Changelog

**Golden records of decision tests (simplification pass S1).** The new module `toolkit/analysis/dt_golden.py` digests each decision-test record in four parts:
- `decision`: what the test decided, plus the run's summary entry. `type` and `perturb` are excluded, and a field the module does not name counts as decision, so it is pinned.
- `detail`: case labels and the type's own block.
- `words`: the sentences.
- `plans`: each case's allocation digest.

Each case's allocation digest is the new `plan_sha256` field that `decision_tests.py` adds to each result. Numbers are rounded to 6 significant digits, values below 1e-9 count as zero, and absolute paths keep only their file name. `toolkit/analysis/run.py`'s self-test captures every record `decision_tests.run_one` returns and compares it with `toolkit/analysis/dt_golden.json`, which holds one line per self-test function (DG-R1). `MA_DT_GOLDEN_UPDATE=1` rewrites that file, except after a failed case or an empty capture. `dt_golden.py digest <run_dir>` digests a finished run and fails closed:
- links;
- paths outside `models/decision_tests/<id>.yaml`;
- ids with path parts;
- results whose id, status or verdict contradicts their summary entry.

Tests: DG1–DG16. No decision changes. The retro on stored rows follows in its own PR.
