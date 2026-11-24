---
phase: "v2 autonomy"
commit: "CV-T α"
---

## Changelog

**Check-value table (offline analysis tool, indicative).** `scripts/harness_metrics.py --check-value <run dirs>` prints the per-check productive / noise table of the check-value analysis from existing records only: runs, rows and HIGH, executed fix tasks and their dollars, owner items, resolution codes, productive in a run (impact rows: headline beyond 0.5%, the spec's allocation file, decision-test files), amended and changed, wording and re-raise noise, class. It also prints a per-finding-kind table of the findings a run's `motivated_by` (toolkit/spec/amend.py, PR #373) names and whether the amended run's plan, headline or decision-test verdicts changed against the parent's (`provenance`, `compare`, `amendment_table`). Not computed: blocking gate-rounds, allocated loop dollars, and findings shown false by a later commit. Known accounting gaps are listed on the pull request; the counts are indicative. Self-tests HM43-HM52.
