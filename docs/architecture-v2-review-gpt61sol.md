I made no file changes.

**1. Diagnosis vs Design Moves**

The §1 direction is mostly right, but the evidence is still a summary, not a measurement package. I’d treat the live-run numbers in [docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:7) as un-audited aggregates because v2 does not name a trace schema, a query, or a breakdown that another engineer can rerun.

- **Context diet is supported, but not yet targeted enough.** §1 says the modeler starts at 173K tokens and 28 preloaded skills account for about 90K ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:13)). That aligns with today’s modeler registry, which attaches a very large skill set, including framework, rigor, optimizer, DALY, identifiability, sensitivity, allocation, benchmark, ecological-fallacy, and eligibility skills ([agents/__init__.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/__init__.py:262)). The missing measurement is **which** skill text, prompt sections, tool outputs, and carried conversation slices drove the 581K max. Without that, “Skill index only, ~2K” is a blunt amputation.

- **Compute-service / no-polling is directionally supported.** “37% of modeler context re-reads and 27% of wall time are sleep/check loops” ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:14)) maps directly onto the modeler prompt teaching shell polling and `nohup` loops ([agents/modeler.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/modeler.py:132)). But §1 also says compute jobs were only 2% of modeler wall time ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:15)); the doc does not show whether the wasted time was *cloud job runtime*, *LLM decisions around jobs*, or *log polling*. A job service removes the third class; it may not reduce the second unless calibration/UQ specs are validated before launch.

- **A Python orchestrator is supported for wait states, not automatically for repair quality.** Today one `query()` session drives the whole run with a giant prose state machine ([main.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/main.py:516), [agents/__init__.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/__init__.py:404)). §1’s “lead is blocked on subagents 93% of its 10 h” supports taking wait/scheduling out of the lead ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:16)). It does **not** prove that the lead can be reduced to a few short “judgement calls” without losing the very detailed Stage 7 routing rules now encoded in prompt prose ([agents/__init__.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/__init__.py:773)).

- **Local repair is supported; the owner model is underdefined.** Current Stage 7 already routes critic blockers by `target_stage` ([scripts/validate_critique_yaml.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/scripts/validate_critique_yaml.py:608)) and folds deterministic findings into synthetic reviewers ([scripts/validate_critique_yaml.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/scripts/validate_critique_yaml.py:5124)). §3.4’s “owner stage” needs that same richness. Many generated RIG blockers default to `target_stage: MODEL` today ([scripts/validate_critique_yaml.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/scripts/validate_critique_yaml.py:5161)), which is exactly how attribution becomes “blame the modeler for everything.”

- **Stage replay is the least supported move.** §1 only establishes that the current test loop is end-to-end ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:19)). It does not quantify fixture variance, replay determinism, scorer/expert agreement, or how often a defect appears only when PLAN, DATA, MODEL, ANALYZE, and WRITE interact.

**2. Biggest Risks**

- **Fixed DAG constrains the actual workflow.** The v2 graph has PLAN/DATA/MODEL_SPEC/CALIBRATE/ANALYZE/DECIDE/WRITE plus parallel jobs ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:34)). The current protocol has pre-mortem and a-priori identifiability before the first modeler spawn ([agents/__init__.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/__init__.py:456)), post-write render/QA/coherence/sufficiency/final-gate loops ([agents/__init__.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/__init__.py:915)), and Stage 7 can jump back to PLAN, DATA, MODEL, ANALYZE, or WRITE ([agents/__init__.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/__init__.py:910)).
  **Mitigation/test:** make `stages.yaml` support cycles, optional nodes, post-write gates, and dynamic subgraphs; replay golden traces that exercise `RETHINK_STRUCTURAL`, `PATCH_OR_RETHINK`, `DECLARE_SCOPE`, `RUN_FAILED`, and a post-write HIGH.

- **The context diet can suppress contracts.** The current modeler prompt is huge because it carries exact schemas and failure patterns for calibration, UQ, identifiability, allocation CV, optimizer benchmarking, universal coverage, sensitivity, and decision rules ([agents/modeler.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/modeler.py:508)). If v2 replaces those with an index, the modeler will simply not draft required artifacts unless the structured input packet includes the exact active contract.
  **Mitigation/test:** for each replay fixture, score missing required artifacts from `.claude/orchestration/rigor_artifacts.yaml` ([.claude/orchestration/rigor_artifacts.yaml](/Users/guillaume.chabot-couture/modeling-agentic-dev/.claude/orchestration/rigor_artifacts.yaml:48)) and effort-floor compliance from `.claude/orchestration/effort_floors.yaml` ([.claude/orchestration/effort_floors.yaml](/Users/guillaume.chabot-couture/modeling-agentic-dev/.claude/orchestration/effort_floors.yaml:47)); adopt the diet only if it is non-inferior on those rates and cheaper on actual input/cache tokens.

- **The compute-service API is too small.** `simulate(params) -> outputs` plus `{kind: calibrate, method: optuna, ...}` ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:47)) does not cover pointwise losses for profile likelihood, held-out folds, stochastic seeds, dependency capture, package installs, generated CSVs, or optimizer benchmark comparability. The existing Azure path already has to tar `models/` so a worker can import arbitrary `outcome_fn.py` ([scripts/cloud_batch.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/scripts/cloud_batch.py:548)).
  **Mitigation/test:** require hermetic job bundles with hashes of model code, data inputs, lockfile, job spec, seed stream, and output schema; run the same fixture locally and remotely and require byte-for-byte metadata plus statistically equivalent numeric outputs.

- **Diff-scoped critics will miss whole-run errors.** Current red-team/domain/sufficiency/coherence checks read broad slices: report, claim ledger, benchmark match, effort floors, calibration, uncertainty, figures, citations. §3.5 narrows LLM critics to a changed-stage diff ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:72)). That is unsafe for stale claims, cross-file counts, denominator mistakes, and overclaims that arise after WRITE.
  **Mitigation/test:** keep a dependency-slice critic for every artifact affected by a change and a full post-DECIDE/post-WRITE adversarial gate; plant stale-ledger and stale-report defects and require targeted review to catch them.

- **The artifact store can accidentally weaken tamper protection.** Today script outputs, `metadata.json`, final gates, cross-model outputs, and validator history are write-protected from agents ([agents/enforcement.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/enforcement.py:47)). v2’s content store must preserve “producer wrote this” as a typed fact, not just “bytes exist at this hash.”
  **Mitigation/test:** every artifact must carry producer, stage, source hashes, harness commit, prompt version, and validator version; the gate rejects missing or stale producer stamps as non-declarable.

**3. Missing for Goals 2-4**

- **Goal 2, decision quality:** v2 does not define the decision rows. Log every planner/modeler/analyst/writer/critic decision as `{options_considered, chosen, rejected, evidence_ids, uncertainty, expected_impact, confidence}`. Score design decisions with blinded expert labels and known-truth worlds: parameter recovery, CI coverage, allocation regret, optimizer gap, claim overstatement rate, and high-blocker false-negative rate. Judges need calibration curves and abstention thresholds before they gate anything; §4’s “expert review calibrates the judges” is not enough ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:93)).

- **Goal 3, attribution:** a findings ledger keyed to one component/stage ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:82)) will lie on emergent defects. Store `introduced_by`, `propagated_by`, `detected_by`, and `repair_owner` separately, backed by the artifact dependency graph. A bad benchmark can be introduced in PLAN, accepted in MODEL, overclaimed in WRITE, and detected by SUFFICIENCY.

- **Goal 4, stage replay:** “N cases” needs power. Use paired A/B designs on the same fixture seeds, stratify fixtures by disease, allocation/no-allocation, data-gap, synthetic truth, and planted defect class, then report bootstrap CIs on pass rate, expert score, cost, latency, and false-negative deltas. Gate on cost ceilings per stage and stop a variant when projected token or cloud cost exceeds budget.

**4. Alternatives Not Considered**

- **Cache before rewriting.** Add content-addressed caching for paper fetch/OCR, Semantic Scholar lookups, WebFetch results, deterministic script outputs, and LLM calls whose prompt packet hash is stable. This directly attacks repeated PLAN/critic retrieval cost without a DAG rewrite.

- **Provider prompt-cache discipline.** The §1 bill is dominated by cache reads ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:11)); stable prefixes, isolated dynamic run packets, and shorter tool outputs can cut cache churn even before skills are removed.

- **Structured outputs for all agents, not only critics.** Put `plan.yaml`, `model_spec.yaml`, `analysis_claims.yaml`, and `scope_declaration.yaml` behind schemas. This converts “one-character ledger format error” fixes into deterministic coercions earlier than §6 step 4.

- **Speculative MODEL_SPEC.** Run 2-3 cheap model-spec agents in parallel, score identifiability and data fit mechanically, then send only the winner to calibration. Expected impact: fewer multi-hour structural rethinks.

- **Best-of-n with verifier for high-leverage calls.** Use three small/medium candidates plus one Opus verifier for model architecture, allocation objective, and interpretation decisions. This buys decision quality only at the narrow points that dominate regret.

- **Specialized model routing.** The registry still uses Opus for planner, modeler, analyst, methods/domain/redteam/sufficiency, and writer, with Sonnet only on data and presentation ([agents/__init__.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/__init__.py:236)). Move extraction, schema repair, summary, and style passes to cheaper models and reserve Opus for architecture and adversarial review.

**5. Re-Prioritized Migration**

1. **Week 1: observability plus one replay lane.** Emit per-turn JSONL/Parquet keyed by `stage, agent_type, model, tokens, cache_read, cache_write, cost, latency, tool, artifact_writes`; current `trace.jsonl` records tools and subagent starts/stops but not per-turn cost or stage ([agents/__init__.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/agents/__init__.py:1241)), while `main.py` only stores session totals ([main.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/main.py:687)). Deliver a MODEL replay harness over at least six curated fixtures with existing validators as scorers. **Success metric:** compare two modeler prompts in paired replay in ≤30 min and produce cost/quality deltas with CIs, without an end-to-end run.

2. **Mechanical schema fixers and structured critic output.** Kill the T1 class before changing orchestration. Coerce booleans, `prior_round`, `carried_forward`, duplicate IDs, and enum case, then prove zero critic schema respawns on planted malformed YAML.

3. **Context diet on MODEL only.** Replace the 28-skill preload with an active contract packet generated from `rigor_artifacts.yaml` and `effort_floors.yaml`; keep the full modeler prompt as control. Gate on non-inferior replay quality and ≥50% modeler input-token reduction.

4. **Compute service for UQ/calibration.** Generalize the existing `BatchRunner` path in `propagate_uncertainty.py` ([scripts/propagate_uncertainty.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/scripts/propagate_uncertainty.py:118)) to calibration restarts and LOO folds; require bundle hashes and budget preflight.

5. **Python orchestrator around existing Stage 5b/7/8 gates.** Migrate deterministic scripts first, then agent spawns. Preserve `validate_critique_yaml.py`’s single action ordering ([scripts/validate_critique_yaml.py](/Users/guillaume.chabot-couture/modeling-agentic-dev/scripts/validate_critique_yaml.py:474)).

6. **Decision packets and judge calibration.** Add only after you have replay labels and telemetry, otherwise the “calibrated judge” has nothing to calibrate against.

**6. Overstated or Wrong Claims**

- “**enforces the effort floors by construction (no shortcuts possible)**” ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:48)). False unless the harness, not generated model code, owns restarts, folds, draws, seeds, and output validation. A spec can still request too few trials or a `simulate()` can be a no-op.

- “**critics output schema-validated JSON through a tool, so there are no schema-fix spawns**” ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:68)). A schema tool prevents YAML syntax and enum errors; it does not ensure correct carry-forward semantics, right round numbers, non-duplicate blockers, or resolved evidence.

- “**critics review the diff and evidence packet … not the whole run every round**” ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:73)). Safe only if the evidence packet is the full transitive dependency slice and a full post-write review still runs.

- “**ten modeler-prompt variants × ten fixtures run concurrently in about 20 minutes**” ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:96)). This ignores LLM rate limits, Coiled quota, cold starts, and straggler fixtures.

- “**because stages replay from frozen inputs, a regression is attributed to the stage that changed**” ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:97)). True for a single-stage paired test; false for interaction regressions across PLAN/DATA/MODEL/WRITE unless the ledger supports multi-cause attribution.

- “**synthetic worlds don’t go stale**” ([docs/architecture-v2.md](/Users/guillaume.chabot-couture/modeling-agentic-dev/docs/architecture-v2.md:125)). The truth may not stale, but the fixture distribution absolutely can as the product moves into new diseases, model classes, or allocation objectives.