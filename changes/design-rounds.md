---
phase: "v2"
commit: "RA"
---

## Changelog

**Rounds and amendments design note (docs only).** `docs/design/rounds-and-amendments.md` sets out what a v2 run's review rounds are for, from the record of 31 v2 runs (109 fix tasks, none of which changed a plan, a headline or a decision-test verdict) and the hb2 and hb3 baselines. Each open finding is put in one of five lanes by code: write-up (the writer's revisions, re-reviewed), owner question (plan-changing decision tests and allocation stability, at the first gate), settings (a deterministic draws and emulator rule in ANALYZE with a recorded probe), amendment proposal (critic HIGH METHODS, HYPOTHESES, CAUSAL or STRUCTURAL findings, with a spec patch or a named capability, decided at the first gate as amend now, declare or defer) and harness gap. It recommends taking the LLM fix loop off the default route and deleting it after the next baseline, replaces the single round cap with analysis passes, `max_amendments` and `max_revisions`, and says how decision (b), #357, the stability finding and the in-run spec version (architecture wave item 6) fit. It adds failure modes with next-baseline metrics and smoke markers, a six-PR order, and 10 open questions with recommended defaults. No code changes.
