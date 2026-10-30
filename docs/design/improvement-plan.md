# Improvement plan: lessons from stisim.ai, Starsim v4, and attempts 7–8

Status: draft for review, 2026-10-28. It complements `stage-machine.md` (the architecture and migration steps S1–S6, T0–T2, M0–M2). Each item below is one small PR, checked on its own against the metric stated for it, in the order given.

## Status (2026-10-30)

Merged to main:
- **A:** A1 compute charged for work done (#51); A2, A3 (#47, #48); A4 + A8 toolkit, pipeline and once-per-version UQ prompt (#50); A5–A7 cloud VM reuse, quota overflow and durable VM jobs (#53); A11–A13 grouped uncertainty findings and declarations that must match the draws (#62); A14 pass/fail verdicts bound to computed checks (#63); A15 benchmark check reads current computed values (#65).
- **B:** B1 calibration spec (#56); B2 + B3 method fit and fact/decision tags in the brief (#55).
- **D:** D1–D5 generic rules (#52) and D6/D8 priors (#49); owner-specified simplifications (#58).
- **E:** E1 model card and E2 what-if queries (#60).
- **M2:** removed duplicated helpers (#54).
- **Integrity fixes found by the smokes:** #57 (final-gate false "deleted") and #59 (missing presentation critique after the harness write-up).
- **Attempt 9 config** (#64): attempt 8's question and settings, unchanged.

Merged (Phase 46, simplification with no change to gate output; #67, #68):
- **α:** `scripts/gate_replay.py` replays the STAGE 7 validator on saved runs and diffs the findings; `agents/contract_packet.py` (unused, failing self-test) and five unused back-compat helpers removed.
- **β:** the uncertainty-report, benchmark and comparator-efficiency checks moved from `validate_critique_yaml.py` (10,104 → 8,672 lines) into `scripts/uq_ci_quality.py`, `scripts/benchmark_gate.py` and `scripts/comparator_efficiency.py`, each with `gate_findings(run_dir)` and its own self-test.

Merged (Phase 47, harness fixes; #69–#73):
- **A9:** the rebuild re-records provenance for figures it redrew (α, #69).
- **A18:** a `percentage` claim whose source holds a proportion (attempt 7, R-064: 0.587 would print as 0.6%) is a gate finding, HIGH once the draft cites it (`scripts/claim_units.py`; β, #72).
- **D9:** decision-layer features listed under `decision_layer:` in `models/mechanism_manifest.yaml`, each with a with/without sufficiency test (or `specified_by` the question or brief) and the dates of the information it reads (γ, #71).
- **A10:** a PreToolUse rule refuses kills chosen by pattern; agents stop processes by recorded PID (δ, #70).
- **A17:** a gate finding present at 3 gates in a row needs a diagnosis in `models/stall_diagnoses.yaml` or a scope declaration before the next decision (`scripts/stall_rule.py`; ε, #73).

In review (Phase 48, simplification before attempt 9, with no change to gate output; checked with `gate_replay.py` on attempts 7 and 8 and every local run):
- **α:** duplicated helpers moved to `scripts/lib/`: the check-module command line (`lib/gate_cli.py`, six copies) and the private by-path loader (`lib/siblings.py`, three copies); two more copies replaced by the existing `lib.safe_io` helpers; two unused functions removed.

Before attempt 9:
- A16 (compute places chunks on free workers itself, so VMs that join mid-batch get work): merged (Phase 45 δ, #66);
- A9 (stale data-stage figures): merged (Phase 47 α, #69);
- a pre-launch smoke on main, then owner sign-off on the brief.

After attempt 9:
- D10 (observed-vs-modelled inputs; D9 is merged above);
- E3, E4; B4 (needs S3), B5, B6; C1–C3; D7;
- smaller items: a gate-side P(output > 0) check from the per-draw values (A12 kept only the reporting); a pin or deterministic declaration can still exempt a frozen output with any reason (no weaker than before #62); attempt 8's B9 scope declaration rested on a stale benchmark value.

## Why

- **stisim.ai** (`starsimhub/stisim/stisim/ai`, v0.1, 2026-09-23) is a Claude Code plugin of 14 skills for interactive STIsim/HIVsim research: grilling intake, method selection, calibration strategy, upstream-first extension, and meaningful tests. Its discipline is good, but nothing checks that the agent follows it. Our pipeline checks the work mechanically, but lacks several of the disciplines it teaches.
- **Starsim v4** (branch `v4`, in development, not released) lets a disease be declared as states and transitions, and runs it as an ODE, a stochastic compartmental model, or agents. It has compiled kernels in Rust and Numba, and JAX.
- **Attempts 7 and 8** showed four problems:
  - calibration knobs ran to their bounds (attempt 8: the net-use and case-management multipliers);
  - each run rewrote its costs and optimizer code;
  - interrupted compute calls were charged their whole reservation (attempt 7: 55 core-hours; attempt 8: 400);
  - the question decided the method before anyone checked whether it fit.

## Principles

1. Turn advice into checks where possible. A skill that says "do X" becomes an artifact that records X, plus a gate that reads it.
2. The researcher decides; the agent finds facts. Questions to the owner are limited to decisions that would change the analysis.
3. Reuse before rewriting, both for our toolkit and for the frameworks' own plugins.
4. Measure each change: the gate-finding trend, compute lost, spend, and the structure metrics on each PR.

## Workstream A — harness fixes from attempts 7–8 (first; they protect every run)

| # | Change | Check / metric |
|---|---|---|
| A1 | **Compute charged for work done, not reservations.** A dead or interrupted caller is charged for the chunks that actually ran, from the scheduler or the results file, and no longer for its whole reservation. A defunct (zombie) caller counts as dead: `os.kill(pid, 0)` succeeds on zombies, so attempt 8 held 400 core-hours for over 7 hours on one. Long batches run detached, so an agent's tool timeout cannot kill the caller. | Attempt 8 replay: 400 core-hours charged → about 35. Self-tests for a killed caller, an interrupted call and a zombie. |
| A2 | ~~Pipeline-runner review fixes~~ (merged, #48). | Done. |
| A3 | ~~#47 review fixes~~ (merged). | Done. |
| A4 | **The modeler is prompted to use `toolkit/alloc`** (cost spec, packages, optimizer) and to declare `models/pipeline.yaml`. | A smoke run uses the toolkit and declares a pipeline. Attempt 9 costs reproduce the toolkit's within 0.1%. |
| A5 | **VM time vs useful work.** Attempt 8: about 1,480 Coiled credits for about 225 core-hours of simulation (about 6.5×), with 734 worker VMs created in 18 h. Keep a short scale-down delay so consecutive batches reuse VMs; size the floor to the agent's typical small batch (UQ of 1,100 draws ran on one VM for 24–36 min); report credits per useful core-hour in the convergence report. | Credits per useful core-hour ≤ 2 on attempt 9; worker VMs created per hour ↓. |
| A6 | **VM family fallback on quota errors.** Coiled tries the `WORKER_VM_TYPES` list for availability, but not on a quota refusal; attempt 8's pool was refused at 348 of 350 ESv5 cores, shared with other users. Fall back to another family (separate quota) on `OperationNotAllowed`, and record refusals in the status file. | Smoke with a forced quota error; pool grows on the fallback family. |
| A7 | **Long jobs on a dev VM survive.** The 45-minute idle stop counts only Dask tasks, so detached jobs were killed twice on 2026-10-28 and their outputs lost. `cloud/` jobs save outputs after every step (or to storage) and hold a keep-alive task, or set the idle timeout to the job's expected length. | A 60-minute detached job on the dev VM completes and its outputs are fetched. |
| A8 | **Uncertainty reruns for binding only.** Attempt 8 ran the 1,100-draw UQ 9 times in rounds 2–3, several only to refresh the fingerprint. With #47's narrower fingerprint, count reruns whose only purpose is binding, and run UQ once per final model version. | UQ runs per round on attempt 9. |
| A9 | **Stale figures still reaching the gate** (attempt 8: 6 in round 2, 1 in round 3) despite the automatic rebuild. Cause, from attempt 8's records: the data-stage `eda.py` redrew its PNGs without calling `validate_figure`, so each rebuild ran it (exit 0) and the sidecars kept their old source hashes (trace: "still stale: eda_01…eda_07", rounds 1–5). The rebuild now re-records the source hashes of a figure whose PNG its script rewrote (mtime or content against a snapshot taken before the run), marked `provenance_by: derived_rebuild`; a figure not redrawn, or whose script failed, stays stale (Phase 47 α, in review). | Stale figures at the gate = 0. Retro on a copy of attempt 8 with eda.py's pre-fix behaviour: main leaves 7 `eda_*` stale after the rebuild, the branch 0. |
| A10 | **Session crash in round 2** (attempt 8, about 07:00 UTC; recovered by cost reconciliation and a re-spawned modeler). Cause (from the stored trace): the modeler stopped its uncertainty run by pattern, `pkill -f "propagate_uncertainty.py"` at 06:59:59, then `ps aux | grep "scripts/propagate_uncertainty.py" | … | awk '{print $2}'` fed to a kill at 07:00:12; the session ended with exit 143 (SIGTERM) at 07:00:25. The pipeline's own Claude CLI runs with the lead's system prompt on its command line, and that prompt names the harness scripts, so the pattern matched the session itself. Fix (Phase 47 δ): a PreToolUse rule refuses kills chosen by pattern or process group and kills of the harness's own processes; agents stop processes by the PID recorded at launch. | Cause identified; fix or an issue. Retro: the rule refuses all 5 pattern kills in attempt 8 and all 3 in attempt 7, with no false positives. |
| A11 | **One degenerate-interval finding per output family, not per output.** Attempt 8's round 5 raised 64 HIGH `ci_degenerate` findings from one cause (a reserve-and-descoping layer repeated across 8 plan variants; 60 related outputs were declared, these 64 were not). Gate counts per round jumped 19 → 83, which misreads as divergence in the convergence report. Group by the output's base name and list the variants in one finding. | Attempt 8 round-5 report replays as about 8 grouped findings; the per-round count no longer tracks the number of plan variants. |
| A12 | **Rare-event outputs reported as a probability, not a quantile interval.** For an output that is zero in most draws, the 95% quantile interval is [0, 0] while the mean is positive (attempt 8: mean 0.16, interval [0, 0] for 4 outputs). `propagate_uncertainty.py` reports P(output > 0) with its standard error for such outputs, and the gate checks that probability instead of the interval width. | Self-test with a zero-inflated output; the attempt 8 report no longer shows a mean outside its own interval. |
| A13 | **Outputs pinned by a binding constraint need a declaration that names the constraint.** Total cost at the budget envelope and utilisation near 1 are flat by construction; a declaration naming the constraint (budget, cap) exempts them, a free-text reason does not. Outputs downstream of a rule that absorbs uncertainty (overrun, reserve used) are not exempt, since that is how the gate and the red-team critic (R-045) both found that the reserve analysis could not show a trade-off. | Attempt 8 round-5 replay: cost and utilisation outputs pass with a constraint declaration; overrun and reserve outputs still flag. |
| A14 | **Pass/fail verdicts bound to computed checks.** Attempt 8's round 4 removed allocation options; the cost-effectiveness rows for two interventions became empty (`n_lga = 0`), yet `results.md` still said "the cost-effectiveness clause holds (B17 in band)" while its own benchmark table said "not computed this round" (D-044, R-048, found by hand). A verdict in the prose that cites an artifact or benchmark id must be backed by a non-empty computed value there that agrees with it. | Attempt 8 retro: the B17 sentence is flagged; few or no other findings on attempts 7 and 8. |
| A15 | **Benchmark check reads current computed values.** `models/benchmark_targets.yaml` is hash-locked after the gate first sees it, so the computed values the modeler was told to write there froze at round 1–2; attempts 7 and 8 both kept current values in an unread `models/benchmark_computed.yaml`. Attempt 8 therefore reported B17 in band (frozen 35.4) after it became uncomputed. The check now takes each value from `benchmark_computed.yaml` when it lists the id (null/NaN = not computed), and flags HIGH a frozen copy that would turn a failing benchmark into a pass. Done in #65. | Attempt 8 retro: B17 and B19 not computed, B23 drift (hidden by the frozen copy), B9 in band; attempt 7: B5, B15 drift, B16 in band. |
| A16 | **Compute places chunks on free workers itself.** On attempt 8 the pipeline's own 34-hour dask task made the scheduler's mean occupancy about 9,900 s per thread, so work stealing never treated a compute worker as saturated: 24 queued chunks sat on one VM while 23 others idled, and round-5 UQ batches of 1,100 draws ran on one worker for 36–47 min. `compute.py` now pins each chunk to a compute worker with nothing processing and re-reads the worker list every 2 s, so a VM that joins gets a chunk at once; the autoscaler still sees the queued work through demand markers that never run. Lost workers and a lost bundle go through the lost-chunk path. | Attempt 9: UQ batches spread over the VMs up (ledger `workers` > 1 for any batch with more chunks than one VM runs); a self-test that emulates attempt 8's occupancy (P45D) runs 8 chunks on 4 workers; main's code runs them on 1. |
| A18 | **A percentage claim whose source holds a proportion.** Attempt 7's red team (R-064) found `r5p_as_delivered_gain_topped_up_p10`: `claim_kind: percentage`, value 0.587 read from `as_delivered.gain_topped_up_p10`, a proportion. `render_claims.py` prints a percentage as `{v:.1f}%`, so the report would have read 0.6% for 58.7%; the provenance check accepts x or 100x for a percentage, so nothing else could see it. `scripts/claim_units.py` flags a percentage claim with 0 < v ≤ 1 when the quantity's own values (v, its CI, its sibling statistics, or its CSV column) lie in [0, 1], its source field is not named as a percent, and the source says the number is on the 0–1 scale: the mapping that holds it names its percents and writes them above 1, or every value beside it lies in [0, 1] with at least two strictly inside, or the claim's `units` name a proportion. A proportion-like name alone is not evidence. HIGH when the draft cites the claim, MEDIUM before; render_claims reports it as it renders (Phase 47 β, in review). | Attempt 7 with its round-7 spec: `r5p_as_delivered_gain_topped_up_p10` flagged HIGH, nothing else; attempts 7 and 8 as stored and the 49 local runs: 0 findings. |

| A17 | **Stalled findings need a diagnosis, not another patch.** Attempt 8's gate reported nearly the same findings from round 5 to round 8 (benchmark drift B9, B15, B19; out-of-sample allocation instability; 19 weakly identified transmission parameters; minimum bar MB-005 on the same allocation metric; a sensitive sensitivity analysis; a failed sanity ratio) while rounds 6–8 patched other things; attempt 7 carried an unstable sensitivity result and a benchmark drift through all 8 rounds. The persistence escalation only raised severity. A finding (kind plus the instance key its check sets: benchmark id, parameter set, criterion id, figure path; unkeyed findings are not tracked) present at N consecutive gates (N = `stall_rounds`, 3) with no diagnosis recorded in those rounds becomes HIGH `finding_stalled_without_diagnosis` until `models/stall_diagnoses.yaml` records the cause classes checked in B4's order, the test file, the result and the decision (fix with a named change, or declare), or the finding is scope-declared. A diagnosis whose test file is missing fails closed. The decision text and JSON list stalled findings. Also: a gate kind's own `*_persistent` escalation, declared with its base kind, no longer blocks the early DECLARE_SCOPE (attempt 8 rounds 6–7 were held open by it alone). | Attempt 8 history replay by kind (it predates keys): MB-005 flagged at round 3, the sensitivity result at 5, the two sanity checks at 6 and 7 (the declared items are not flagged); attempt 7: four findings at round 3. Attempt 8's final state gated three times with keyed history flags every plateau item. Gate replay on 51 runs: no change (no run has keyed history). |

## Workstream B — disciplines from stisim.ai, as checked artifacts

| # | Change | Artifact and gate | Metric |
|---|---|---|---|
| B1 | **Calibration spec.** Each parameter gets one role: data-informed, fixed from literature, poorly observed and country-specific, behavioural, tuning, or scenario assumption. Each target has full metadata. | `models/calibration_spec.yaml`. Gate: HIGH if a scenario assumption is fitted to history; HIGH if a fitted parameter is not in the spec; MEDIUM if a bound was widened without a recorded diagnosis (B4). | Knobs at their bounds at the first gate (attempt 8: 2); identifiability findings by round. |
| B2 | **Method fit before tool.** The planner records the analytical objective (description, association, structure, causal, forecasting, dynamic-mechanistic) and why the requested framework fits, including compartmental vs agent-based by stisim.ai's criteria. It may recommend against the requested tool, which becomes an open question for the owner. | `method_assessment` in `decision_brief.yaml`, required by the brief validator. | Present in 100% of briefs. Redirects are reported, not scored. |
| B3 | **Facts vs decisions in the brief.** Each brief item is tagged fact found (with a source), decision needed, or blocked on evidence. Owner questions are limited to decisions, and an optional second round runs before sign-off. | Brief schema; the validator rejects owner questions that are facts. | Owner questions per brief (attempt 8: 5); amendments after sign-off. |
| B4 | **Diagnosis order before widening.** A calibration fix names which check it ran: missing data, missing mechanism, structural constraint, inconsistent targets, mismatched definition, then bounds. | Field in the fix ledger (S3); B1's gate reads it. | Widened bounds with no diagnosis = 0. |
| B5 | **Test rule.** Every new self-test states the bug it would catch. Remove tests that only re-read a value they set. | Review checklist; structure metrics count tests per script. | Self-test runtime and count go down without losing a planted-defect detection (regression eval unchanged). |
| B6 | **Reuse the frameworks' own skills.** Compare our Starsim and stisim skills with `starsim_ai`, `stisim.ai` and `calib`; attach theirs where they are maintained and cover the same ground; keep ours only for what is specific to the pipeline. | `validate_skill_attachments.py` lists the plugins they come from. | Lines of skill text we maintain, which should go down. |

## Workstream C — Starsim v4 (evaluate before adopting)

| # | Step | Decision it informs |
|---|---|---|
| C1 | **Benchmark on our own models**, in an isolated environment, with your approval to run code installed from the `v4` branch. Run attempt 8's malaria ABM cell (Griffin-lite, 4,000 agents, 2016–2026) under v3.3.4 and v4 as agents, and a declared version as compartmental. Also run the starsim-malaria port (`starsimhub/starsim-malaria`, updated 2026-10-27). | Whether v4 changes the cost of the policy grid (attempt 8: 8,960 simulations, about 58 core-hours). |
| C2 | **If C1 shows large gains:** a v4 skill and a migration path for the modeler (v4 ships `docs/migration/v3_v4.md` and a migration script), behind a flag, tried on a smoke run first. | Whether attempt 9 or 10 uses v4. |
| C3 | **Wait for the release** before making v4 the default. The branch is 469 commits ahead of main, and its own targets for agent runs are "at least slightly faster". | Pin a released version. |

## Workstream D — domain mechanisms the model must cover (from the vector-bionomics miss)

**What happened.**
- Attempt 7's pre-mortem raised PM-008: net and IRS redundancy and residual outdoor transmission by *An. arabiensis* depend on the indoor and in-bed biting fractions. It pointed to the hbhi species-abundance file and asked for a robustness check.
- It rated the concern **LOW**. The gate checks only HIGH concerns (`pre_mortem_high_unaddressed`), and the modeler closed it as "ADDRESSED. Unchanged" without the check.
- Attempt 8 had the species abundance for every LGA in `lga_master_774.csv`, but no model code read it. It used one set of bionomics everywhere (Q0 0.92, φ_I 0.85, φ_B 0.80).
- North of 11°N (216 LGAs, 63M people, the highest prevalence, the core SMC area), the population-weighted mix is 67% arabiensis. That gives an effective Q0 of about 0.78 (`malariasimulation` per-species values, from Sherrard-Smith 2019).
- The trial benchmarks passed because vector-control strength was tuned to the Tanzanian and Benin trial sites, so nothing tested whether the mechanism transfers.

**The pattern, and it is not malaria-specific.** The target setting varies in a way the available data captures and the model collapses, and the model's effect parameters were tuned where the variation looks different. Examples elsewhere:
- **TB:** household vs community transmission, or the drug-resistance mix by region.
- **Measles:** age-specific contact patterns and how long maternal immunity lasts.
- **HIV:** network and risk-group structure.
- **Cholera:** water and sanitation access.

Domain knowledge reaches the model only as prose, and a self-graded severity decides whether anything checks it.

**Split by layer (CLAUDE.md: gate logic disease-agnostic; specifics in priors and skills gated by `applies_when`).** The gate gets generic rules. A disease contributes content (priors, optional toolkit modules) that those rules enforce.

*Generic harness rules (`scripts/`, no disease names):*

| # | Change | Check | Metric |
|---|---|---|---|
| D1 | **Mechanism manifest, parsimony first** `models/mechanism_manifest.yaml`. The first cut stays as simple as the question allows. For each mechanism a matched prior flags, the model records **modelled** (code reference), **fixed** (source), or **simplified** (the assumption made). A simplified mechanism carries a **sufficiency test**: the assumption varied over its plausible range, and whether the decision changes (the package for a stated share of units, or the headline beyond its uncertainty interval). If it does not change the decision, the simplification stands and the report cites the test. If it does, the mechanism is added in the next round. | Gate: HIGH if a flagged mechanism has no entry, or is simplified with no sufficiency-test result. It is never HIGH for leaving a feature out when the test passed. | Flagged mechanisms with an entry; features added only after a failed test. |
| D2 | **Severity floor from priors.** A concern that matches a prior cannot be graded below the prior's severity, so its sufficiency test is required. The severity sets whether the test is required, not whether the feature is. | Pre-mortem validator. | Matched concerns graded below their prior = 0. |
| D3 | **Data-use audit.** Every processed data column is read by model code, or listed as unused with a reason. | Static scan of `models/` against `data/processed/` headers; MEDIUM per unexplained unused column. | Unused columns without a reason = 0 (attempt 8: 3 species columns). |
| D4 | **"Addressed" needs evidence.** A concern whose suggested check is a sensitivity analysis or a data use can only be closed by an artifact path that exists, at any severity. | Pre-mortem validator. | Concerns closed by prose = 0. |
| D5 | **Transfer check for tuned effects.** When effect strengths are tuned to benchmark sites, also report the effect under the target setting's covariates, with the difference stated. | Benchmark report field. | Present whenever tuning is used. |
| D9 | **Sufficiency tests for decision-layer features too.** D1 covers mechanisms in the disease model, not rules added to the decision. Attempt 8 added a reserve ladder, descoping rules, supply caps and a minimax hedge in rounds 3–5; its own results showed the reserve's overrun tolerance never binds (0 of 1,100 draws at every rung, D-045), and the 2026 rule used information not available at decision time (R-045). A rule added to the decision records whether it changes the recommendation, and which information it uses at the decision date; if it changes nothing, it is removed. | Phase 47 γ: `decision_layer:` in the mechanism manifest, required once the run has an allocation (`[]` = none beyond the brief). Gate as D1: HIGH for no list, an untested feature, or a feature whose test shows no change that stays in the plan (unless a brief amendment names it); MEDIUM for an input available after the decision date. An unlisted feature is left to the red-team and sufficiency critics. Retro: catches attempt 8's overrun tolerance once listed; the list itself is HIGH from round 2. | Decision-layer features kept without a test = 0. |
| D10 | **Model inputs checked against observed counterparts.** D3 checks that data columns are used, not that model inputs agree with observed records the run already has. Attempt 8: Ogun's modelled net campaign is 1.93× the observed procurement (others 1.10–1.20×, R-047), and the observed procurement record contradicts the supply caps (R-046). Where an observed counterpart exists for a model input, report the ratio per unit and flag outliers. | Data-use audit field; MEDIUM per unexplained outlier. | Input/observed outliers found by critics but not by the audit = 0. |

*Disease content (priors and toolkit; malaria is the first instance):*

| # | Change | Where |
|---|---|---|
| D6 | Prior: vector bionomics vary by species (Sherrard-Smith 2019 PNAS; Griffin 2010; `malariasimulation` defaults); required mechanism when species data exist. `applies_when`: malaria with nets or IRS. | `.claude/orchestration/expert_priors.yaml` |
| D7 | Optional species-aware vector module: larval stage optional, a published resistance-to-efficacy mapping. Tested against `malariasimulation` on the EIR–prevalence curve and the trial arms. | `toolkit/malaria/` |
| D8 | The same audit for one other disease area's priors (for example TB or measles), so the generic rules have more than one source of required mechanisms. | `expert_priors.yaml` |

**Test of the split:** run D1–D5 on a smoke run for a non-malaria question, as CLAUDE.md asks (alternate disease areas). The rules must work on that run, and a scan of `scripts/` must find no disease names.

## Workstream E — the finished run as a reusable asset

**Why.** A finished run leaves a complete stored asset: data, model code, calibration, emulator, uncertainty draws, claims ledger and report. Attempt 8's model even has a working interface (`outcome_fn(params)`, `alloc_core.solve(U, budget=, objective=)`, `evaluate_unit`), and it answered the alternative envelopes with it. But the pipeline has no way to reuse a finished model: what-ifs are done by hand with no checks, `--resume` only finishes an interrupted run, and any follow-up question starts from scratch. Model code is not packaged, versioned, or described by its valid range (attempt 7: 23% generic, 26% malaria-reusable, 30% question-specific, 21% duplicates; attempt 8: a second, independent 2,728-line model for the same question).

| # | Change | Check / metric |
|---|---|---|
| E1 | **Model card** written by the harness at the end of every run (`model_card.yaml` + a short `model_card.md`): the callable interface (functions, inputs, outputs), the valid domain (parameter and covariate ranges the model was simulated or calibrated over), validation results (trial benchmarks, held-out calibration, emulator accuracy), the uncertainty draws available, environment pins, and known limits (scope declarations, open findings). Built from artifacts the gate already reads; fails closed when an interface or the domain cannot be established. | Cards for attempts 7 and 8; every interface entry callable. |
| E2 | **What-if queries without agents:** `ask <stored run> --set <param>=<value> ...` loads the stored model, checks the request is inside the card's valid domain (refuses outside it), runs the interface with the run's uncertainty draws, and returns the answer with intervals, recording the query. Deterministic, no model API calls. | Attempt 8: re-answer its own alternative envelopes and match them; an out-of-domain request is refused. |
| E3 | **Follow-up runs:** `main.py --from <stored run> "<follow-up or feedback>"` starts from the stored model, data and declared pipeline; the planner writes a brief of what changes from the base (signed by the owner); the pipeline reruns only stale steps; critics and gates review the change; the report is an addendum. | One real follow-up on attempt 8 (e.g. a new envelope plus an SMC unit-cost change) at a fraction of a full run's cost. |
| E4 | **Promotion into the toolkit:** after a run, an agent proposes moving its best reusable modules (e.g. a malaria vector-and-immunity module, D7) into the toolkit as a PR the owner approves, with tests from the run's validation. | Next same-area run starts from the toolkit module. |

## Order and checkpoints

1. A1, A2, A3 (in progress), then A4 with a smoke run.
2. D1–D5 (generic rules) with D6 (the first prior that uses them), then a non-malaria smoke run; then B1 and B4 (calibration spec and diagnosis order), B2 and B3 (brief), each with a smoke run.
3. C1 benchmark (no run spend; VM time only), alongside step 2.
4. Attempt 9: same question, same settings. Compare with attempts 7 and 8 on findings by round, compute lost, spend, and headline basis.
5. E1 and E2 (model card and what-if queries), so attempt 8's model is usable when it finishes; then E3 with one real follow-up on attempt 8; E4 after.
6. B5, B6, and the M2 structure work (`stage-machine.md` §3.7) between runs.

## What stays as it is

The mechanical gate, harness-computed checks, critics, owner sign-off, cloud compute, and the run record. stisim.ai's interactive, researcher-led style suits a different use, months-long projects with a human in the loop. Its memory and handoff skills are noted, not planned, for now.
