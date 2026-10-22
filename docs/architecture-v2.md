# Architecture v2: cheaper, faster, attributable, testable in parallel

Status: proposal (2026-10-22). Based on measurements from the Phase 21 live runs, not intuition.

## 1. Diagnosis (measured)

The data comes from the measles run (3,873 turns, 887M context tokens) and A/B run T1 (13 h, $609, RUN_FAILED).

| Finding | Evidence |
|---|---|
| Cost is context re-reading, not generation | T1: 916M cache-read + 59M cache-write input tokens vs 5.7M output; Opus 5.5 = 97% of $609 |
| The modeler dominates | 74% of all context tokens; 1,915 turns; 8.5 h |
| Huge standing context | modeler starts at **173K tokens** (28 preloaded skills ≈ 90K + 14K prompt + tools); grows to mean 342K, max 581K |
| Polling is a tax | 37% of the modeler's context re-reads and 27% of its wall time are `sleep`/check loops |
| LLM turns, not compute, set wall time | modeler: 63% LLM time, 2% compute jobs |
| Strictly serial orchestration | lead is blocked on subagents 93% of its 10 h |
| Global rounds re-run everything | 8 rounds × (modeler + analyst + 4 critics); a one-character ledger format error persisted for 6 rounds and killed T1 |
| Problems are not attributable | we needed ad-hoc transcript forensics to learn the above; no per-stage cost/quality telemetry |
| The only test is an 8–13 h end-to-end run | no way to test a modeler change without re-running plan + data |

## 2. Principles

1. **LLMs decide; programs compute.** Numerical work (calibration, UQ, sensitivity, LOO-CV, optimization) runs as declarative jobs outside the agent loop. Agents submit specs and receive summaries.
2. **Small, purpose-built contexts.** Every agent turn should carry only what that decision needs. Budget contexts like money.
3. **Typed stage contracts plus checkpoints.** Each stage reads and writes declared artifacts, so any stage can be replayed from frozen upstream artifacts.
4. **Local repair, not global rounds.** A failing check routes to the owning stage only. Mechanical problems are fixed mechanically.
5. **Measure everything per stage and per agent**, so every failure and every dollar is attributable.
6. **Test at the stage level in parallel**; end-to-end runs become a smoke test, not the unit of iteration.

## 3. Target architecture

### 3.1 Stage DAG with typed contracts

```
PLAN ──► DATA ──► MODEL_SPEC ──► CALIBRATE* ──► ANALYZE ──► DECIDE ──► WRITE
                     │             (jobs)          │
                     └──► PRE-MORTEM               └──► UQ*, SENSITIVITY*, LOO*, OPTIMIZE*  (jobs, parallel)
```

- **Contracts:** each node has an input/output schema, stored in `.claude/orchestration/stages.yaml` (extends `rigor_artifacts.yaml`).
- **Artifact store:** artifacts are content-addressed (sha256) and checkpointed to the project storage account (`checkpoints/<run>/<stage>/`), with provenance: stage, agent, prompt version, model, harness commit.
- **Orchestrator:** a Python orchestrator, not an LLM lead, walks the DAG. The LLM lead is reduced to *judgement* calls (scope, trade-offs, repair routing when ambiguous), invoked with a short evidence packet. This removes the 10-hour blocked lead context.
- **Parallelism:** independent nodes run concurrently: UQ, sensitivity, LOO-CV and optimizer benchmarks are all downstream of calibration and independent of each other.

### 3.2 Compute service

- **What the modeler writes:** a model module (`models/model.py` with `simulate(params) -> outputs`) plus a job spec, e.g. `{kind: calibrate, method: optuna, n_trials, restarts, targets, held_out}`.
- **What the harness does:** a `jobs/` runner executes specs on Coiled (fan-out per draw, restart or fold), enforces the effort floors by construction (no shortcuts possible), and returns a **≤2K-token summary** plus artifact paths.
- **Effect:** no polling, no tool-output floods, and calibration and UQ get the cloud's parallelism. Effort floors become job parameters rather than after-the-fact gates.

### 3.3 Context diet

| Agent | Now (start) | Target (start) | How |
|---|---|---|---|
| modeler | 173K | ≤30K | skills loaded on demand via `Skill` (index only, ~2K); prompt split into stage-specific briefs; no CLAUDE.md |
| others | 13–53K | ≤15–25K | same |

Additional rules:
- **Fresh agents per task:** short-lived subagents per sub-task with an explicit input packet, instead of one modeler session accumulating 581K.
- **Bounded tool output:** a hook truncates large tool results to a head/tail plus a file path.
- **Compaction:** enable or force it at about 150K.

### 3.4 Repair loops instead of global rounds

- **Routing:** a check finding has an *owner stage*. Only that stage re-runs, with the specific finding and a diff-scoped brief.
- **Mechanical fixers:** format and schema issues are fixed by code, e.g. booleans in ledger values, missing `prior_round`, YAML schema fields. They are never LLM rounds and never a non-declarable failure. (T1 died of this.)
- **Budgets and early stop:** each stage has a repair budget (e.g. 2 attempts). If non-declarable semantic failures persist past the budget, stop early with a clear failure report.
- **Critic YAML:** critics output schema-validated JSON through a tool, so there are no schema-fix spawns (about 20% of spawns historically).

### 3.5 Critique and decisions

- **Deterministic first:** the existing validators run on every artifact change, for free.
- **Targeted LLM review:** critics review the *diff and evidence packet* for the stage that changed, not the whole run every round.
- **Model tiers:**
  - routine (presentation, schema-adjacent): Sonnet;
  - methods and domain: Opus;
  - adversarial: Opus plus gpt-6.1-sol, at two gates only (after MODEL_SPEC, after DECIDE).
- **Decision packets:** any agent making a design or interpretation decision gets a structured packet (claims, evidence pointers, check results, uncertainty), not raw logs. Judges are calibrated against expert labels (see §4) before their verdicts gate anything.

### 3.6 Telemetry and the findings ledger

- **Per-turn telemetry:** every turn logs `{run, stage, agent, prompt_version, model, input/cache/output tokens, cost, latency, tool}` to the registry (Parquet in storage).
- **Findings ledger:** every defect or failure (from checks, critics, scorers or the expert) is recorded against a component and stage, with evidence. That is how problems become attributable: *which stage introduced it, which check caught it, at what cost*.

## 4. Evaluation: parallel, stage-level, attributable

| Tier | What | Cost / time | When |
|---|---|---|---|
| **Unit** | existing `--self-test`s + mechanical fixers | free, seconds | every commit |
| **Stage replay** | run one stage (e.g. MODEL_SPEC + CALIBRATE) from frozen upstream fixtures across N cases in parallel; score with stage scorers | ≈$1–10 per case, ≈5–20 min | every change to that stage |
| **Known-truth** | synthetic worlds (truth in a protected container): parameter recovery, CI coverage, allocation regret | ≈$20–60 per world with v2 | nightly / per release |
| **End-to-end smoke** | 2–3 real questions | target ≤$100, ≤2 h each | per release |
| **Expert review** | blinded, sampled | your time | per release; calibrates the judges |

- **Fixture library:** stage-boundary snapshots harvested from past and partial runs (the 18 historical malaria runs, T1, the in-flight A/B runs, measles), synthetic worlds, and planted-defect variants. Each fixture has expected outcomes.
- **Parallel by construction:** the experiment runner fans stage-replay cases across Coiled workers, so ten modeler-prompt variants × ten fixtures run concurrently in about 20 minutes instead of ten 8-hour runs.
- **Problem identifiability:** because stages replay from frozen inputs, a regression is attributed to the stage that changed. The findings ledger plus telemetry say where quality and cost come from.

## 5. Targets

| Metric | Now | v2 target |
|---|---|---|
| End-to-end cost | $600+ | ≤$100 |
| End-to-end wall time | 8–13 h | ≤2 h |
| Iteration on one stage | one full run (8–13 h) | stage replay, ≈10–20 min, parallel |
| Modeler start context | 173K | ≤30K |
| Spawns spent on schema fixes | ≈20% | 0 |
| Runs failing on format issues | T1 | 0 (mechanical fixers) |

## 6. Migration plan (each step measured with the eval harness)

1. **Telemetry and fixtures (first, cheap).** Per-turn telemetry hook; harvest fixtures from existing runs; stage-replay runner for MODEL and ANALYZE. Establish *current* stage-level baselines.
2. **Context diet.** On-demand skills, briefs, tool-output caps. Measure on stage replay: cost per case, quality unchanged or better.
3. **Compute service.** Job specs, Coiled fan-out, summaries; remove polling. Measure wall time and cost on MODEL replay.
4. **Python orchestrator, repair loops and mechanical fixers;** critics as tool-validated JSON.
5. **Critique tiers and decision packets,** with judges calibrated against expert labels.
6. **End-to-end smoke and known-truth benchmarks** on v2; then resume the larger experiment programme.

Keep: the Phase 21 enforcement, provenance checks, eligibility probe, identifiability math and fail-closed validators. They become stage checks instead of an end-of-round gate.

## 7. Risks

- **Over-structuring:** a fixed DAG can block legitimately novel approaches. Mitigation: MODEL_SPEC allows alternative structures; the judgement lead can add nodes.
- **Context diet hurting quality:** measured, not assumed, on stage replay before adoption.
- **Fixture staleness:** fixtures are re-harvested from new runs; synthetic worlds don't go stale.
