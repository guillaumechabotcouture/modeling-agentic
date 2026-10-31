---
phase: "49"
commit: "δ"
---

## Changelog

**Presentation fix pass names the gate's current round (smoke 3 fix).** Smoke 3 (2026-10-31, main 52a6178) ended `completed_gate_error`: the run stopped after round 1 on budget, the A23 fix pass found no presentation critique and its prompt said "This is critique round N.", the critic wrote `round: 2`, and the final gate (which takes the highest critique round) then rejected the round-1 domain and red-team critiques. The pass now uses the round of the other critiques (the gate's current round, presentation excluded), else the presentation critique's own round, else 1 (`agents/run_finish._pass_round`), so a stale presentation round never wins in either direction (Codex). Self-test PF5c reproduces the smoke and failed before the fix.
