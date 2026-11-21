---
phase: "v2 Wave 0"
commit: "W0-G"
status_group: "In review (v2 autonomy, Wave 0)"
---

## Changelog

**Every review condition raised before sign-off has a disposition before the spec can be signed.** New `toolkit/spec/review_conditions.py` reads `review_conditions.yaml` in the run directory: one row per condition from a spec critic, an outside review, an owner note or the parent run, each `adopted` (naming a spec path or an `analysis.decision_tests` id that exists), `declined` (with a reason; the owner only) or `deferred` (with an owner and an ISO due date). It registers the precondition `review_conditions` (`toolkit/spec/preconditions.py`), so every signing path refuses a spec while a row is open or does not resolve, and a signed run whose file gains an open row reports `preconditions_failed` at launch and resume. A file that cannot be read, is a link, or does not have the shape is a problem (fail closed); no file means no condition was raised. The sign-off sheet lists the rows in a "Review conditions" table and records the file's sha256 among its sources; a sheet that shows other conditions than the file holds is refused as stale. `import` adds the spec critique's undeclared HIGH findings, a review file's items and the parent run's open HIGH findings without overwriting a row already disposed. Importing is a command for now; the stage machine does not call it.

## Status

- **W0-G:** reviewer conditions need a disposition before sign-off (`review_conditions.yaml`, precondition `review_conditions`); in review.
