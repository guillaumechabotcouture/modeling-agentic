# Experiment roadmap (architecture v2)

Every experiment is pre-registered in this directory before it runs. Stage experiments use the replay lane (`scripts/replay.py`, evidence-based fixtures, Coiled backend).

**Guiding hypothesis:** the system is built as long-lived agents that remember everything and police each other. The evidence (see `../architecture-v2.md` §1) points instead to many short, typed calls orchestrated by code, each seeing only what it needs, with deterministic checks first, a few *measured* targeted reviews, and human checkpoints where decisions matter most. Each experiment tests one piece of that.

| ID | Question | Status | Notes |
|---|---|---|---|
| E1 | Context diet: contract packet vs 28 preloaded skills (MODEL) | **done**: not adopted | start context −82%, input tokens −43%, cost −26%; quality underpowered; packet gaps found |
| E1b | Packet gap fix: every effort floor as an explicit instruction | **done**: inconclusive | HIGH floor violations 4/6 → 1/6, but progress fell (cap confound) |
| E2 | History growth: tool-output cap, fresh-context handoff, reasoning effort | ready to pre-register after E1b | tool output is only ~16% of re-reads; retained reasoning and whole-file reads dominate |
| **E5** | **Critic effectiveness: which critics catch which planted defects, at what cost and noise** | **done** (matcher v2) | union 0.74; red-team alone 0.61; methods adds 0 unique (save ~$45/round); domain kept (unique 0.074); gaps: data_vintage, ledger_mismatch, unit_error |
| **E4** | **Decomposed MODEL stage: spec → code → harness job → interpret, as fresh typed calls, vs the monolithic modeler** | design | needs the compute-service job runner (v2 §3.2) in minimal form |
| **E6** | **Expert checkpoints: ~15-min human review of plan + model spec vs none** | design | outcome: downstream defects and rework; uses the owner's time sparingly |
| E3 | Powered quality test at stage completion, with blinded expert sample | after E2/E4 | validates the design we will keep, not an intermediate one |

**Order:** E1b → E5 (cheap, decides which critics to keep) → E2 → E4 → E6 → E3.

## Standing rules

- **Pre-register first:** decision rules and power are set before any case runs; deviations are logged in the experiment file.
- **Preflight caps:** payload, upload time and dollar caps are enforced by `replay.py` preflight.
- **No contamination:** experiments run from a clean worktree at a committed hash, never from a tree with in-progress edits.
- **Collect before trusting:** results are collected and sha256-verified before anything is concluded; `collect --watch` runs for every Coiled batch.
- **Separate claims:** efficiency claims need tight CIs; quality claims need adequate power or are labelled as screening.
