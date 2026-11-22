---
phase: "v2"
commit: "question-path-punct"
---

## Changelog

**A file named at the end of a sentence in the question is staged.** `agents/stage_machine.py` `_question_paths` tested the data extension before stripping sentence punctuation, so a path such as `data/x.csv.` ending a sentence (or followed by `,`, `;`, `)` or a closing quote) failed the `endswith` test and was silently dropped: never copied into `data/inputs/` nor recorded in its provenance. Trailing punctuation (and an opening bracket or quote) now comes off first. Self-test T41c (paths ending in `.`, `,`, `;`, `)` and quotes; a repository file named at the end of a sentence is staged) failed before the fix.
