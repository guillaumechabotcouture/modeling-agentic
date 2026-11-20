---
phase: "v2"
commit: "WB α"
---

## Changelog

**Review workbench: specification.** `docs/design/review-workbench.md` specifies a new local page for verifying a run (`tools/workbench/`), agreed with the owner on 2026-11-18. It holds the five jobs of the tool, the rules every view follows, the problem-agnostic design (views return data; one generic model per view with an adapter per method; no domain word in the code; a view counts as built only when it passes on two reference runs of different disease and method), each view's content with acceptance values from stored runs, the science register that extends the world model, twelve things no run file records today (H1-H12, queued for after the F6 report), a scorecard of sixteen past problems the tool is measured against, four layers of verification, and the loop that builds one view per iteration with fresh agents working from the document alone. Docs only.
