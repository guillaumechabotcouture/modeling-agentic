# Running several agentic modeling runs at once, each launching compute

**Status:** design note for discussion, 2026-10-25. Nothing here is built yet beyond what is marked as existing.

## The problem

Each pipeline run is an agent (on its own VM) that decides by itself when to launch simulation batches. With several runs at once, the questions are:
- who gets which machines;
- how spend stays inside each run's budget and the project's;
- how one run's simulations are kept from starving another run or its own agent;
- how we see all of it in one place.

**Today's limits** (IDM Coiled, default workspace):
- 1,000 cores per user at once;
- 100,000 core-hours per user per month;
- a capacity limit we hit at about 20 VMs (E8: 11 of 12 VMs arrived).

## Principles

1. **Agents never sit next to their simulations.** The agent's VM runs only the agent; simulation batches go to separate compute VMs. Attempt 5's agent was killed when its own simulation grid exhausted the VM.
2. **Every core-hour and every dollar belongs to one run**, recorded where the harness, not the agent, owns the record:
   - the budget ledger (`budget_ledger.json`, dollars);
   - the compute ledger (`compute_ledger.json`, core-hours).
3. **Limits are enforced by the platform, not by agent good behaviour:**
   - **Pool size:** the run's compute pool size bounds what it can burn at once.
   - **Core-hour cap:** the run's core-hour cap bounds the total.
   - **Dollar cap:** the dollar cap bounds LLM spend.
4. **One place to see everything:** every run publishes `status.json` to storage (existing). A fleet view is just a reader of all those files plus the cross-run register (existing).

## Options for compute

| | A. Pool per run (now) | B. Shared project pool | C. Shared pool with a queue |
|---|---|---|---|
| **How** | Each run's cluster gets its own N compute VMs; only that run's agent submits to them | One project cluster; all runs submit to it; each run's chunks are tagged | B plus a small broker: runs request core-hours; the broker admits, queues or refuses by priority and budget |
| **Isolation** | Complete | Runs compete; a big batch delays others | Controlled (quotas, priority) |
| **Utilisation** | Low: VMs idle between batches (they idle-stop after a timeout) | High | High |
| **Attribution** | Trivial (per cluster) | Needs per-run tags in the compute ledger (exists per run) | Built in |
| **Complexity** | Already works (E7 attempt 6 uses it) | Small: one shared scheduler address, per-run worker resources | Moderate: a broker service and its own state |
| **Best for** | 1–3 concurrent runs | 3–10 runs with bursty simulation | Many runs, many users, priorities |

## Recommendation

- **Now (1–3 runs):** option A, already wired.
  - **Agent VM:** each run gets its own agent VM plus 2–4 compute VMs.
  - **Limits:** the compute service's per-call allowance and the run cap.
  - **Visibility:** the run's status and its compute ledger in storage.
- **Next, when we run batches of runs** (replays for stochasticity, A/B arms): option B.
  - One shared compute cluster sized to the workspace limits.
  - Per-run core-hour caps.
  - Fair-share through dask worker resources, a fixed number of slots per run.
  - A fleet watcher that reads every run's `status.json` and compute ledger.
- **Option C** only if we reach many concurrent users or need priorities (e.g. a production run outranking experiments).

## What has to be true before running several at once

1. **The modeler uses the compute service.** Today nothing makes it: attempts 4–5 ran 2.4–12.5 h of simulations on the agent's VM (autopsy). Options: a prompt contract, a check that flags heavy local compute, or a memory watchdog on the agent VM.
2. **Each run has a core-hour cap as well as a dollar cap** (exists in the compute ledger; needs to be shown in status and the register).
3. **A fleet view:** one watcher over all runs' storage prefixes.
4. **Workspace capacity is planned for:** concurrent runs × (1 agent VM + N compute VMs) must stay under the ~20-VM practical limit.
