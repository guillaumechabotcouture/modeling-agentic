# Architecture v2: cheaper, faster, attributable, testable in parallel

**Status:** proposal v2.1 (2026-10-22). v2.0 was reviewed blind by gpt-6.1-sol (Codex). Its findings and two measurement corrections are folded in below; see the changelog at the end.

## 1. Diagnosis (measured)

**Data sources:**
- **Measles run:** session transcripts parsed with `scripts/telemetry.py`. One row per API response; the CLI writes one transcript line per content block, which ~2×-counts tokens if read naively.
- **A/B run T1:** the CLI's own recorded usage.

**Re-run:**

```
python3 scripts/telemetry.py <session_dir> --lead <lead.jsonl> --run-dir <run> --out turns.csv
```

| Finding | Evidence |
|---|---|
| Cost is context re-reading, not generation | T1 (recorded): 916M cache-read + 59M cache-write input tokens vs 5.7M output; Opus 5.5 = $592 of $609 |
| The modeler dominates | measles: 75% of context tokens (325M of 432M), 951 of 1,840 API turns |
| Huge standing context | modeler starts at **173K tokens** (28 preloaded skills ≈ 90K + 14K prompt + tools); mean 342K, max 581K |
| Waiting is a modest tax | strict definition (`sleep`/wait loops): **10%** of modeler context, ~4% of turn latency. *(v2.0 said 37%; that counted every `tail` as a poll.)* |
| Serial orchestration | the lead is blocked on subagents for most of the run (93% of its 10 h in measles) |
| Global rounds re-run everything | 8 rounds × (modeler + analyst + 4 critics); a one-character ledger-format error persisted 6 rounds and killed T1 (RUN_FAILED, $609, no report) |
| Problems were not attributable | only session totals were recorded; transcript forensics were needed. `telemetry.py` now exists |
| Only end-to-end testing | no way to test a modeler change without re-running PLAN + DATA |

**Not yet measured, and needed before committing to specific designs:**
- which prompt sections, skills, tool outputs and carried history drive the 581K peak;
- replay determinism and fixture-to-fixture variance;
- how often defects arise only from cross-stage interaction;
- agreement between scorers and the expert.

## 2. Principles

1. **LLMs decide; the harness computes.** The harness, not generated code, owns restarts, folds, draws, seeds and output validation.
2. **Small contexts built from contracts.** Each agent receives the *active contract packet* for its task, generated from `rigor_artifacts.yaml` / `effort_floors.yaml`, instead of 28 preloaded skills.
3. **Typed artifacts with producer stamps,** checkpointed per stage, so any stage can be replayed from frozen inputs.
4. **Fix mechanical problems mechanically;** repair semantic problems locally.
5. **Measure per turn, per stage and per decision;** attribute defects through the artifact dependency graph.
6. **Paired, stratified stage-level evaluation in parallel;** end-to-end runs become smoke tests.

## 3. Target architecture

### 3.1 Stage graph (not a rigid DAG)

`stages.yaml` defines nodes with input/output schemas: PLAN, DATA, PRE_MORTEM, A_PRIORI_IDENT, MODEL_SPEC, CALIBRATE, UQ / SENSITIVITY / LOO / OPTIMIZE (parallel), ANALYZE, DECIDE, WRITE and POST_WRITE gates. It also supports:
- **cycles and back-edges:** today's RETHINK_STRUCTURAL / PATCH_OR_RETHINK routes back to PLAN, DATA, MODEL, ANALYZE or WRITE;
- **optional nodes** and dynamic subgraphs, so MODEL_SPEC can propose alternative structures;
- **post-write gates:** render, QA, coherence, sufficiency, final gate.

Orchestration:
- A **Python orchestrator** handles scheduling, waiting and parallelism.
- **Routing rules** currently encoded in lead prose (STAGE 7 action ordering in `validate_critique_yaml.py`) move into code unchanged.
- An **LLM judgement call** is used only where the rules leave a genuine choice, and gets a structured packet.
- **Acceptance test:** replay golden traces that exercise RETHINK_STRUCTURAL, PATCH_OR_RETHINK, DECLARE_SCOPE, RUN_FAILED and a post-write HIGH; the orchestrator must reproduce the same actions.

### 3.2 Compute service with hermetic job bundles

**Job spec:** the modeler supplies `simulate(params, seed) -> outputs` plus a pointwise loss (for profile likelihood), and the targets with held-out folds. The spec names the job kind (calibrate / UQ / sensitivity / LOO / optimizer benchmark); its sizes come from `effort_floors.yaml`, and the modeler may raise but not lower them.

**The harness:**
- builds a **hermetic bundle**: hashes of model code, data inputs, lockfile, spec, seed stream and output schema;
- runs the job on Coiled (generalising the existing `propagate_uncertainty.py` BatchRunner path);
- validates outputs (non-degenerate, finite, schema-valid);
- returns a ≤2K-token summary.

**Acceptance test:** the same bundle run locally and remotely produces identical metadata and statistically equivalent outputs, and a no-op `simulate()` is rejected.

### 3.3 Context diet, measured before adoption

- **Contract packet:** replace the 28-skill preload with an *active contract packet* (exact schemas and floors for the artifacts this task must produce), plus an on-demand skill index.
- **Prompt-cache discipline:** stable prefixes, an isolated dynamic run packet, and capped tool output (head/tail plus file path).
- **Agent lifetime:** fresh short-lived agents per sub-task; compaction around 150K.
- **Adoption gate:** on MODEL replay, the diet is adopted only if it is **non-inferior** on missing-required-artifact rate, effort-floor compliance and expert/judge score, **and** cuts modeler input tokens by at least 50%.

### 3.4 Repair: mechanical fixers, local loops, budgets

- **Mechanical fixers** (code, not LLM rounds, never a non-declarable failure): booleans in ledger values, `prior_round`, `carried_forward`, duplicate IDs, enum case. *T1 died of this class.*
- **Structured outputs for all agents:** `plan.yaml`, `model_spec.yaml`, `analysis_claims.yaml`, `scope_declaration.yaml` and the critic outputs are produced through schema tools. Schema tools prevent syntax and enum errors only. Carry-forward semantics, round numbers and resolution evidence are still checked by the validator.
- **Local repair:** a finding routes to its repair owner with a diff-scoped brief and a repair budget (e.g. 2 attempts). If non-declarable semantic failures persist past the budget, stop early.

### 3.5 Critique and high-leverage decisions

- **Deterministic validators** run on every artifact change.
- **Dependency-slice critics:** a changed artifact is reviewed together with every artifact that transitively depends on it, not just the diff.
- **A full adversarial review** is always kept after DECIDE and after WRITE (Opus plus gpt-6.1-sol), to catch stale claims, cross-file counts, denominator errors and overclaims.
- **Model routing:** extraction, schema repair, summaries and style go to cheaper models; Opus is reserved for architecture, adversarial review and interpretation.
- **Speculative MODEL_SPEC:** 2–3 cheap spec agents in parallel; identifiability and fit to data are scored mechanically; only the winner proceeds to calibration. This aims to avoid multi-hour structural rethinks.
- **Best-of-n with a verifier** at the few decisions that dominate regret: model architecture, allocation objective, interpretation.

### 3.6 Telemetry, decisions, and multi-cause attribution

- **Per-turn telemetry** (`scripts/telemetry.py`, to be emitted live by a hook): stage, agent, prompt version, model, input/cache-read/cache-write/output tokens, cost, latency, tool, artifact writes.
- **Decision log:** every planner/modeler/analyst/writer/critic decision is recorded as `{options_considered, chosen, rejected, evidence_ids, uncertainty, expected_impact, confidence}`.
- **Findings ledger with multi-cause fields:** `introduced_by`, `propagated_by`, `detected_by`, `repair_owner`, linked through the artifact dependency graph. A bad benchmark can be introduced in PLAN, accepted in MODEL, overclaimed in WRITE and detected by SUFFICIENCY.
- **Producer stamps on every artifact:** producer, stage, source hashes, harness commit, prompt version, validator version. Missing or stale stamps are non-declarable, which preserves Phase 21 tamper protection inside the artifact store.
- **Caching** keyed on content hashes: paper fetch and OCR, Semantic Scholar/WebFetch results, deterministic script outputs, and LLM calls with stable prompt-packet hashes.

## 4. Evaluation: paired, stratified, parallel

| Tier | What | When |
|---|---|---|
| Unit | `--self-test`s + mechanical fixers (incl. planted malformed YAML → zero schema respawns) | every commit |
| **Stage replay** | one stage from frozen fixtures; **paired** A/B on the same fixtures and seeds; bootstrap CIs on pass rate, judge/expert score, cost, latency, false-negative deltas | every change to that stage |
| Known-truth | synthetic worlds (answer keys in a protected container): parameter recovery, CI coverage, allocation regret, optimizer gap, overclaim rate | per release |
| End-to-end smoke | 2–3 real questions | per release |
| Expert review | blinded, sampled; calibrates the judges (calibration curves and abstention thresholds before any judge gates anything) | per release |

- **Fixtures** are stratified by disease, allocation or no allocation, data gaps, synthetic truth and planted-defect class. They are re-harvested as the product reaches new diseases and model classes. Synthetic truth doesn't go stale, but the fixture *distribution* does.
- **Attribution:** a single-stage paired replay attributes a regression to that stage. Interaction regressions need the multi-cause ledger and occasional multi-stage replays.
- **Cost guards:** per-stage spend ceilings; a variant stops when its projected spend exceeds budget. Concurrency is limited by LLM rate limits, Coiled quota and cold starts, so the expected speed-up is measured rather than assumed.

## 5. Targets (to validate, not promises)

| Metric | Now | v2 target |
|---|---|---|
| End-to-end cost | $600+ (T1) | ≤$100 |
| End-to-end wall time | 8–13 h | ≤2 h |
| Comparing two variants of one stage | two full runs | paired replay over ≥6 fixtures in ≤30 min, with CIs |
| Modeler input tokens | 173K start / 325M per run | ≥50% lower, non-inferior quality |
| Schema-fix spawns; runs failed by format | ≈20% of spawns; T1 | 0; 0 |

## 6. Migration plan (re-prioritised after review)

1. **Week 1: observability plus one replay lane.**
   - Live per-turn telemetry hook (Parquet).
   - MODEL replay harness over ≥6 curated fixtures harvested from T1, B1, B2, T2, measles and the historical runs, with existing validators as scorers.
   - **Success metric:** compare two modeler prompts by paired replay in ≤30 min, reporting cost and quality deltas with CIs, without an end-to-end run.
2. **Mechanical fixers plus structured outputs.** Kill the T1 class before touching orchestration.
3. **Context diet on MODEL only,** with the full prompt as control; gated as in §3.3.
4. **Compute service for calibration, UQ and LOO,** with hermetic bundles and budget preflight.
5. **Python orchestrator around the existing gates.** Deterministic scripts first, then agent spawns; preserve the action ordering.
6. **Decision packets and judge calibration,** only once replay labels and telemetry exist.
7. **Caching, speculative MODEL_SPEC and best-of-n,** each introduced behind a paired replay comparison.

**Kept from Phase 21:** enforcement, producer stamps, provenance checks, eligibility probe, identifiability math and fail-closed validators. They become stage checks.

## 7. Risks

- **The stage graph constrains novel modeling.** Mitigation: cycles, optional nodes and dynamic subgraphs; the golden-trace acceptance test.
- **The context diet drops contracts.** Mitigation: contract packets plus the non-inferiority gate.
- **Fixtures miss real upstream variance.** Mitigation: stratified, multiple fixtures per stratum, periodic multi-stage replays.
- **Diff-scoped review misses cross-file errors.** Mitigation: dependency slices plus a full post-DECIDE/post-WRITE gate.
- **The compute abstraction leaks.** Mitigation: hermetic bundles and a local/remote equivalence test.

## Changelog

**v2.1:**
- **Corrected measurements:** the polling share is 10% (not 37%), and transcript turn and token counts are de-duplicated (about 2× lower).
- **From the gpt-6.1-sol review:**
  - stage graph with cycles;
  - hermetic job bundles, with the harness owning effort;
  - contract packets instead of an amputated skill index;
  - dependency-slice critics plus a full adversarial gate;
  - multi-cause attribution and a decision log;
  - producer stamps preserved;
  - caching and prompt-cache discipline;
  - structured outputs for all agents;
  - speculative MODEL_SPEC, best-of-n and model routing;
  - paired, stratified replay with CIs and cost guards;
  - re-prioritised migration with a week-1 deliverable;
  - overstated claims removed (*"no shortcuts possible"*, *"no schema-fix spawns"*, *"10×10 in 20 minutes"*, *"synthetic worlds don't go stale"*).
