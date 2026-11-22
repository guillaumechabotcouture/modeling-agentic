---
phase: "v2"
commit: "cited-prose-paths"
---

## Changelog

**A prose `source:` naming files is checked file by file.** `scripts/pipeline.py` `cited_paths` took any spec string ending in a data extension as one path, so a `source:` sentence naming an input and its derived copy was reported as one path that "does not exist" although both files exist. New `path_tokens`: a string with no whitespace, or one naming an existing file as a whole, stays one path; prose yields each whitespace-separated token ending in a data extension once sentence punctuation is stripped, so each is checked and recorded, and a missing one is named by its own token in `record_cited_inputs`' message. Self-tests CP1-CP4 (two files in prose, a missing one named, single paths and paths with spaces kept whole, prose naming no file) failed before the fix.
