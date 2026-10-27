# E7 attempt 7: does the loop converge? (pre-registered before launch)

Written 2026-10-26, before attempt 7 launched and before any of its output was seen.

## Question

Attempt 6 did not converge: open blocking findings by round were 39, 9, 10, 6, 11, 26 → 15, and the
critics raised 18, 8, 7, 6, 10, 9 new HIGH/MEDIUM findings per round, with no taper. Attempt 7 tests
whether the structural changes below make the findings the critics raise get resolved by iterating.

## Standard question

Identical to attempts 1-6 (`e7_question.txt`, verbatim).

## What changed since attempt 6

| Phase | Change | Aimed at (attempt 6) |
|---|---|---|
| 30 | Compute on an autoscaling pool (4 → up to 24 VMs of 8 cores; floor 4), held by a keeper process; caps 1,500 core-hours per run, 400 per call | Grids timed out with 46% of 2,664 simulations unrun; no compute after hour 5 |
| 31 | Turn warnings, budget note, 1-hour prompt cache | Spawns cut off at the turn limit; cost of re-reads |
| 32 | Open-problem list, round plan (actions fixed at hand-out), fix checks before the full critique, one return trip; reviews wait for running work | MEDIUMs never worked, then escalating together; round-6 fix breakage found after the write-up |
| 33 | Decision brief signed by the owner before compute; acceptance criteria frozen; critic findings tied to brief items | Benchmark targets edited mid-run; findings outside the decision |
| 34 | A `done_when` test per finding, frozen, re-run every gate as a regression suite; crash cost from transcripts; harness read-only during the run | Moving targets ("narrowed, still present"); D-014 broken in round 6, raised only as new D-031 |

## Hypotheses and measures (attempt-6 baseline in brackets)

1. **Arrivals taper.** New HIGH/MEDIUM findings per round fall after round 2: round 3+ mean below
   round 1-2 mean [18, 8, then 7, 6, 10, 9: no taper].
2. **Findings close.** Median rounds from first seen to resolved ≤ 2 [six MEDIUMs took 4-5 attempts];
   share of resolved findings closed by their test reported.
3. **Regressions are caught when they happen.** Every `done_when_regressed` is reported with the round
   that broke it; none surfaces only as a new finding [D-014 → D-031, P-001 → P-012, P-002 → P-014].
4. **Compute completes.** No batch ends "timed out" with more than 10% of its rows unrun [3 of 8
   calls, 46% unrun]; compute is used in later rounds, not only the first.
5. **Open blocking findings at the last gate** fewer than attempt 6's 15.

Also reported: hours and dollars per round (model and VM separately), holds that fired and how long
they held, critic tests by kind (runnable / judgement / mixed), withdrawn tests, unmet tests.

## Checkpoint and stop rules (decided in advance)

- **After the round-2 gate** (≈ 8 h): report measures 1-4 so far. Stop the run if a hold has kept
  work blocked for more than 1 h with no agent able to clear it, or if more than a third of open
  findings are blocked only by tests the critic itself considers fixed (unmet tests).
- Budget: the run stops at its cap like any run; reaching it is an outcome, not a failure of the test.

## Decision use

- Hypotheses 1-3 hold: keep the structure; the next work is model and data quality (lessons
  L019-L021).
- Arrivals do not taper but findings close: critics find genuinely new problems; look at what kind.
- Findings do not close: inspect the tests (too strict, wording-bound) and the holds before more runs.

## Limitations

One run, one question; Opus critics are stochastic. The structure was tested in short real-model
scenarios and 2-round Haiku smokes, never over 8 rounds. Attempt 7 costs more per round than attempt 6
by design (fixes for MEDIUMs, fix checks, return trips).

## Owner decisions before launch

Budget cap; launch time (the run waits for the owner's sign-off about 40 minutes in); constraints or
data to add at sign-off (proposed: primary sources for decision-driving inputs; cost multipliers on
the same eligible population as the benefit; out of scope up front for data known to be unavailable).
