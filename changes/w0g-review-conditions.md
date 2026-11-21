---
phase: "v2 Wave 0"
commit: "W0-G"
status_group: "In review (v2 autonomy, Wave 0)"
---

## Changelog

**Every review condition raised before sign-off has a disposition before the spec can be signed.** New `toolkit/spec/review_conditions.py` reads `review_conditions.yaml` in the run directory: one row per condition from a spec critic, an outside review, an owner note or the parent run, each `adopted` (naming a path of the signed spec's own YAML or an `analysis.decision_tests` id), `declined` (with a reason; the owner only) or `deferred` (with an owner and an ISO due date). It registers the context precondition `review_conditions` (`toolkit/spec/preconditions.py`). At signing, whoever signs and through any signer, every row must resolve, the sign-off sheet (when there is one) must show this file (its sources line records the file's sha256), and, while the file exists, each undeclared HIGH finding of the spec critique must be a row. Each signer (`toolkit/spec/signoff.py`, `scripts/decision_brief.py`, the stage machine's) records the file's sha256 in its sign-off record. A signed run whose file changed since, or whose rows no longer resolve, reports `preconditions_failed` at launch and resume. A draft or a run nobody signs is not checked. A file that cannot be read, is a symbolic or hard link, has a duplicate key or does not have the shape is a problem (fail closed); no file means no condition was raised. `import` adds the spec critique's undeclared HIGH findings, a review file's items and the parent run's open HIGH findings without overwriting a row already disposed, and refuses another text under an existing id. Importing is a command for now; the stage machine does not call it.

## Status

- **W0-G:** reviewer conditions need a disposition before sign-off (`review_conditions.yaml`, precondition `review_conditions`); in review.
