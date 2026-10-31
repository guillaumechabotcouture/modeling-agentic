# Architecture v2: agents configure validated engines, the harness computes everything

Status: proposal for owner review (2026-10-30). It supersedes the order of work in
`stage-machine.md` §4 but keeps its principles; S3–S6 become layer 5 below.

## 1. Why: where attempt 8's effort went

Attempt 8 (E7, the Nigeria GC7 malaria allocation) cost $442 of model spend and
5,074 Coiled credits, ran for 44 h over 8 rounds, and ended DECLARE_SCOPE with its
main weaknesses declared rather than fixed.

| Where the effort went | Evidence |
|---|---|
| **Re-implementing infrastructure.** The modeler wrote 5,850 lines in 42 files, plus 12 driver scripts. These covered the transmission model, calibration, emulator, uncertainty propagation, optimizer, reserve and descoping logic, robustness, figures and claims. | `models/*.py`, root `*.py` |
| **Checking that infrastructure.** 387 gate findings over 21 gate runs. | `validator_history.jsonl` |
| About 54% (≈210) concerned analysis plumbing the agents built: degenerate intervals (96), stale figures (22), sanity checks (33), the robustness bar (21), drift, unbound headlines and claims (≈40). | |
| About 37% (≈145) concerned model content: benchmark drift (63), unstable allocation (21), weak identifiability (37), sensitivity (21). | |
| About 8% were process bookkeeping: scope budget, brief, pre-mortem. | |
| **Self-graded closure.** The run declared its HIGHs out of scope and DECLARE_SCOPE counted as success. | SD-002, SD-007, SD-008 |
| **Model content gaps a validated model would not have.** The ITN×IRS interaction came out as OR 0.94 against the trial's 2.43, and IRS $/DALY as about 330 against a band of about 25. | B15, B19 |

Phases 45–49 added about 15 checks to catch these classes, and the code grew by
about 3,500 lines. Orchestration changes alone (S3–S6) would make the same work
better organised. They would not remove it. The large gains come from changing
**what the agents are asked to produce**.

## 2. Principles

1. **Reuse validated engines; don't rewrite them.** Transmission dynamics, intervention
   effects and their interactions come from established, published simulators or
   promoted toolkit modules. An agent writes model code only for what no engine covers.
2. **Code computes every number.** Calibration, emulation, uncertainty, optimization,
   cross-validation, sensitivity, decision tests, figures and the claims ledger are
   harness code driven by the decision spec. Agents never hand-roll them, and never
   type a number the report uses.
3. **Everything runs from one owner-signed decision spec.**
4. **Parameters carry checked evidence.** No value enters a model without a source the
   evidence service has fetched and checked, and an uncertainty range.
5. **LLMs do judgement.** That means framing, model choice and extension, mapping data
   to model inputs, interpretation, critique and narrative.
6. **The owner decides at three points:** the spec, the model choice, and any
   limitation on the core decision. This replaces autonomous self-declaration.
7. **Every change is measured on the evaluation suite** (`docs/design/eval-suite.md`)
   before it is kept.

## 3. Layers and interfaces

### 3.1 Decision spec (`decision_spec.yaml`, owner-signed)

Typed fields:
- `decision_units` (id, attributes, data source);
- `options` (packages, built from `toolkit/alloc` package rules);
- `objective` (outcome, horizon, discounting);
- `constraints` (budget envelope, caps, coverage, eligibility);
- `comparator`;
- `outcomes_reported`;
- `uncertainty_requirements`;
- `decision_date`;
- `deliverable` (e.g. a package per unit, a national mix, a ranking).

The planner drafts the spec from the question; the owner signs it. Every later layer
reads it. This generalises today's `decision_brief.yaml` and the alloc spec.

### 3.2 Evidence service (`evidence/registry.yaml`)

Each parameter entry records:
- value, distribution and units;
- population, setting and year;
- source (DOI or URL plus a quoted span);
- `verified: fetched|matched|unverified`;
- the agent that proposed it.

The service fetches and checks quotes through the Asta, Semantic Scholar, PubMed,
Evidence Engine and WebFetch tools. It reuses `effect_size_registry.py`,
`scripts/lib/provenance.py` and the citation checks. Only `verified` entries can feed
a decision-relevant parameter; the others are marked as assumptions and varied
widely. This is where today's confabulation and misattribution findings stop.

### 3.3 Model engines (`engines/<name>/`, one contract)

```python
class Engine(Protocol):
    def describe(self) -> EngineCard        # mechanisms, interventions, validated benchmarks, valid ranges
    def parameters(self) -> list[ParamSpec] # names, priors, which are calibrated vs fixed vs scenario
    def run(self, params: dict, unit: UnitSpec, package: Package, horizon: Horizon,
            seed: int) -> Outcomes          # cases, severe, deaths, DALYs, coverage, costs drivers
```

- **First adapters:**
  - `malariasimulation` (MRC-IDE, R with a C++ core) covers species-specific bionomics, ITN types including PBO and dual-AI, IRS, SMC, case management and RTS,S/R21. Its interactions are validated against trials by its authors.
  - The Starsim disease modules.
  - The `toolkit` modules promoted from runs (E4).
  - EMOD and OpenMalaria later, through the same contract.
- **Each engine ships a benchmark pack.** These are its published validation results: the trial arms and the EIR–prevalence curves. The harness re-runs them per version, not per question. Today's per-run trial tuning becomes an engine property.
- **The agent's job:** choose the engine, with a recorded reason checked against `describe()`; map data to `UnitSpec`; declare which parameters are calibrated; and extend the engine only through declared plug-ins with their own tests.
- **Fallback for questions no engine covers:** the agent writes a model behind the same contract, and layers 3.4 onward apply unchanged.

### 3.4 Analysis engine (`analysis/`, harness code)

It reads the spec, the engine contract and the evidence registry, and produces typed artifacts:

| Step | Built from |
|---|---|
| Calibration to targets, with diagnostics | calabaria; `scripts/calibration_spec.py`, `identifiability.py` |
| Emulator over parameters × package × unit archetype, with held-out accuracy | today's ad hoc emulators, standardised |
| Uncertainty propagation | `propagate_uncertainty.py` |
| Allocation optimization | `toolkit/alloc` costs, packages and optimize |
| Leave-one-out and cross-validation of the allocation | `allocation_robustness`, `allocation-cross-validation` |
| Sensitivity, and decision tests for every simplification, decision-layer feature and declaration | D1, D9, A20 schema, computed instead of reported |
| Figures and claims ledger | `derived_rebuild`, `render_claims` |

- **The agent requests analyses; it doesn't write them.** For example: "add a scenario", "vary X", "a reserve rule as a decision-layer feature with its test".
- **Most of the ≈210 plumbing findings can't arise.** The checks that audit agent-written analyses (UQ degeneracy, stale figures, verdict and headline binding, benchmark copies, sanity ratios) shrink to contract checks on harness output.

### 3.5 Orchestration (state machine and fix planner: `stage-machine.md` S3–S4)

- **States:** SPEC → SIGN → EVIDENCE → ENGINE_CHOICE (owner checkpoint) → CONFIGURE/CALIBRATE → ANALYZE → CRITIQUE → GATE → FIX_LOOP → WRITE → PRESENTATION → FINAL.
- **Findings ledger.** One typed ledger replaces today's critique YAMLs, fix checks, done_when, scope declarations, stall diagnoses and validator history. Each finding has an owner state and a resolution type: fix with a measured impact, decision test, or owner decision.
- **Fix planner.** The LLM fix planner picks one finding per fix task. The harness rebuilds, measures the impact, and records it in `impact_ledger.jsonl`.
- **Prompts shrink.** The lead prompt (≈1,000 lines) and the refusal hooks become state-table rows (S6).

### 3.6 Review and owner checkpoints

- **Critics** (domain, red team, sufficiency, presentation) review computed facts and the engine card against the expert priors. They no longer audit plumbing.
- **The owner is asked three times:** to sign the spec, to approve the engine choice and extensions, and to decide on any limitation that bears on the deliverable (A21). Each pause uses the existing sign-off machinery with a time-out.

### 3.7 Report

- Tables, numbers and the declared-limits block are rendered from artifacts, following A22's harness-rendered block and today's `render_claims`.
- The writer writes the narrative and interpretation and never types a number.
- The model card and `ask` (E1/E2) come from the same artifacts, so follow-up what-ifs reuse the run.

## 4. What changes for today's components

| Component | v2 |
|---|---|
| `toolkit/alloc` | Becomes the optimization step of the analysis engine (kept) |
| `pipeline.yaml` runner (S1) | Runs engine configuration and analysis steps (kept) |
| `compute.py` and `cloud/` | Run engine batches (kept; the A16 dispatch matters more) |
| Gate checks (21 `gate_findings` modules plus the validator's 24 inline `_check_*` functions) | About half deleted: their objects become harness output. The rest become contract checks or move to engine benchmark packs. |
| The modeler contract (`agents/modeler.py`, ≈1,265 lines) | Replaced by a configuration and extension role with a much smaller contract |
| The lead prompt | Replaced by the state table plus the fix planner |
| Critics | Kept; their inputs become computed facts |
| Evaluation suite (Phase 50) | The yardstick for every step |
| Phases 45–49 | Their rules carry over as state behaviour (A17, A20, A21) or become unnecessary (A11–A15, A18) |

## 5. Expected effect and how we measure it

These are targets, not promises. Each is measured on the evaluation suite and on one
re-run of E7.

| Metric | Attempt 8 | v2 target |
|---|---|---|
| Rounds to a final decision | 8 | ≤ 3 |
| Model spend | $442 | ≤ $100 |
| Wall time | 44 h | ≤ 8 h |
| Gate findings about plumbing | ≈210 | ≈0 (by construction) |
| Trial benchmarks reproduced | 22/25 (B9, B15, B19 drift) | All the engine covers, from its pack |
| Allocation worst-fold rank r (leave-one-out) | 0.35 | Reported, with owner sign-off if < 0.7 |
| Parameters with a verified source | Not measured | ≥ 90% of decision-relevant parameters |
| Evaluation suite answer score | Baseline (Phase 50) | Clearly above baseline on every question |

## 6. The spike: test the claim before building v2

- **Scope.** A one-state subset of E7, or E7 itself if it is cheap enough. Same question and data.
- **Minimal v2:**
  1. a hand-written `decision_spec.yaml` (owner-reviewed);
  2. a `malariasimulation` adapter behind `Engine` (R on the dev VM, via `rpy2` or a subprocess CLI), using its packaged species parameters and intervention models;
  3. an analysis engine built from what we have: calabaria (or the current calibration code) to state PfPR, an emulator, `propagate_uncertainty`, `toolkit/alloc` optimize, leave-one-out, and decision tests;
  4. the existing critics on the computed facts;
  5. the existing writer with rendered numbers.
- **No state machine yet.** A scripted sequence stands in for it.
- **Success criteria:**
  - B15 and B19 inside their bands without per-run tuning;
  - a headline within attempt 8's interval or a documented reason why not;
  - a leave-one-out allocation result;
  - model spend under $100 and wall time under 8 h;
  - no plumbing findings at the gate.
- **Effort and cost:** 3–5 days of build, under $100 of model spend plus VM time. No E7-scale run is needed until the spike passes.
- **If it fails:** we learn which layer was the bottleneck (engine fit, calibration, evidence) before committing to the full build.

## 7. Migration after the spike (each step measured on the suite)

| Step | Builds | Gate to pass |
|---|---|---|
| V0 | Evaluation suite baseline (Phase 50) on the current architecture | Baseline scores recorded |
| V1 | Decision spec schema and sign-off; planner drafts it | Every suite question produces a valid spec |
| V2 | Engine contract plus the first adapter and its benchmark pack | Pack reproduces the published validation |
| V3 | Analysis engine (calibrate, emulate, UQ, optimize, cross-validate, decision tests, figures, claims) | Suite questions run with no agent-written analysis code |
| V4 | Findings ledger, fix loop and state machine (S3–S4); lead becomes fix planner | Golden-trace replays take the same gate actions; prompt size ↓ |
| V5 | Evidence service | Share of verified parameters ≥ 90% on the suite |
| V6 | Delete superseded checks and prompt stages (S6); promote modules (E4) | Lines of code ↓; suite scores ≥ V5 |
| — | Attempt 9 (E7) on v2 | §5 targets |

## 8. Risks and open questions for the owner

1. **Engine choice and R in the stack.**
   - Option: malariasimulation (mature, R/C++, validated).
   - Option: Starsim malaria (Python, experimental as of 2026-10-27).
   - Option: EMOD (heavier, IDM-maintained).
   - Proposal: malariasimulation first for malaria. Starsim modules are for diseases they cover well, with LASER for spatial questions.
2. **Questions no engine covers.** The fallback is an agent-written model behind the same contract. The risk is that this becomes the common path. We measure it on the suite.
3. **Owner checkpoint latency.** Three pauses per run. Each needs a time-out and a default, as brief sign-off has today.
4. **Calibration framework.** Calabaria (IDM) or our current code. This is a spike decision.
5. **Licensing and provenance of engine parameter sets.** Record versions and citations in the engine card.
6. **Scope creep in "extend the engine".** Extensions must go through declared plug-ins with tests and a D1-style sufficiency test.
