# F6 plan: from F5's declared-scope report to results we can stand behind

Status: draft, 2026-11-08. Owner approval to start Stage 1: 2026-11-08.

F6 is the next Nigeria GC7 allocation run (13 Global Fund states, 309 LGAs). This note turns the F5 outcome, my
review of it, and an independent critique by GPT-6.1 Sol into one ordered plan. It sits alongside the factory
package in `model-library.md` §5; §6 below says how the two fit together.

Counts only: no Nigeria survey values appear here (the inputs derive from licensed DHS and MAP data). The full F5
review and the critique stay local.

## 1. Where F5 left us

F5 finished on 2026-11-08 as `completed_scope_declared`, after round 4 of 4. It declared four findings: two high
(the case-management effect `cm_add` is a fixed, unsourced value; the package chosen for each LGA is not robust)
and two medium (SMC in 9 of about 230 eligible LGAs; a withdrawal comparator). It reused every stored engine run.

The critique ran with read access to the run's outputs and code, an emulator-backed what-if helper (22 experiments,
no engine runs) and live web search (26 sources). Its verdict: F5 can support discussion of trade-offs and
scenarios, but not an allocation. It found eight P1 problems. I verified two of them myself before writing this
note (C6, C4 below); the others carry file and line references and are checked again, test first, in the PR that
fixes them.

| ID | P1 finding (critique) | Verified | Fixed in |
|---|---|---|---|
| C1 | The comparator withdraws services other funders already deliver, including partner-funded SMC; what the allocation buys is not defined | not yet | S2-1 |
| C2 | The CM add-on is an implementation-free, all-age rise in effective treatment, and the same rise lifts access to severe care one for one; its cost has no setup or capacity part | code read | S2-2, S1-3 |
| C3 | Calibration error structure: the state survey error enters each LGA target's variance separately (no shared state term); intervention coverage absorbs history errors; the 2018→2021 change is wrong in several states | not yet | S2-3 |
| C4 | Four trial benchmark bands (B10, B11, B12, B12b) are symmetric around the estimate, so each lower bound is wider than the published interval | **yes** | S1-2 |
| C5 | Emulator: no season-shape, SMC-history or past-net features; only package-reduction errors are propagated, not comparator burden or treated-case cost; no check of ranking accuracy near package-switch boundaries | not yet | S2-4 |
| C6 | The fixed plan exceeds the allocatable budget in 48 of 64 uncertainty draws; uncertainty never checks the plan's affordability | **yes** (replay through the harness's own draws reproduces the published interval) | S1-1 |
| C7 | Absolute cases, severe disease and deaths are not validated by state and age; deaths drive the objective | not yet | S2-5 |
| C8 | The reported interval and robustness claim more than the analysis supports: the region sample is not a posterior, decision-critical inputs are held fixed, the leave-out test does not redo the calibration | not yet | S1-4, S1-5 |

Its P2 findings that bear on F6 are listed with the item that handles them. Five claims in my own review were wrong
or overstated and have been corrected in the local review page. The one that changes this plan: the "binding
transmission ceiling" is withdrawn as a cause. Best-fit values sit well below the ceiling; only compatible intervals
reach it.

## 2. What "results we can stand behind" means

F6 launches only when every item in the launch checklist holds, and its report is written only when every item in
the result checklist holds. Both lists are mechanical where possible, and each item names the artifact that shows
it.

**Launch checklist** (before the owner signs the F6 spec):

1. Every P1 above is either fixed (a merged PR with a test that failed before the fix) or carried as an owner-signed
   scope with a decision test that shows its effect on the plan.
2. The comparator is the continuation of committed services, from a commitments table the owner has seen (S2-1).
3. Every benchmark band matches its cited source, checked by script (S1-2).
4. Every scenario parameter a plan component depends on has a decision test (S1-4).
5. A canary of the F6 spec passes end to end on the golden launch path (`model-library.md` §5.1, §5.5).
6. An emulator-only preview of the F6 plan, built from the new calibration, has been reviewed by me, and by
   GPT-6.1 Sol with the what-if helper and web search, before the full analysis runs (S3-2).
7. The cost of the run is quoted, with the share reused from stored runs.

**Result checklist** (before the report is written):

1. The plan's budget-exceedance probability is reported, and the headline plan meets the affordability rule the
   spec declares (S1-1).
2. Calibration diagnostics pass on the new error model, including the change between survey years by state (S2-3).
3. Continuation-arm cases, severe disease and deaths sit inside stated ranges of WHO, GBD and survey estimates by
   state and age band, or the gap is declared (S2-5).
4. Decision summaries report regret, population and dollars moved, not only the share of LGAs that change (S1-5).
5. The "net in every LGA" and "one net decision per state" variants are reported beside the unconstrained plan (S1-6).
6. My review and an independent GPT-6.1 Sol critique, both with the what-if helper, find no unresolved P1.

## 3. Stage 0: owner inputs (in parallel with Stage 1)

| # | Decision or input | Why | Needed by |
|---|---|---|---|
| O1 | Source for GC7 and partner commitments by LGA, year, intervention and funder (NMEP, Global Fund grant budgets, PMI, SMC partners) | C1: defines the comparator | start of Stage 2 |
| O2 | CM framing: an allocatable package with effect scenarios and linked costs, or CM kept in the fixed floor | C2 | start of Stage 2 |
| O3 | Affordability rule: plan must fit the envelope at the point, in a stated share of draws, or with a reserve | C6 | S1-1 review |
| O4 | Order of the deferred items: determinism roadmap R1–R4, v1 retirement | they were waiting on F5 | this week |

## 4. Stage 1: harness fixes (no engine runs; start now)

Each is a PR into `v2` under 800 lines, test first, with self-tests, a Codex review and, where it touches the
launch or compute path, one real smoke run. Every check fails closed. All of it is disease-agnostic.

**S1-1 Affordability under uncertainty (C6, C8).** In each uncertainty draw, record the fixed plan's cost beside its
outcomes. Report the budget-exceedance probability and the fixed-plan cost quantiles in `uncertainty.yaml`, the
claims and the report. Add a spec field for the affordability rule (O3) and, when it asks for one, an optimiser
mode that meets it (a reserve, or a chance constraint over the draws). A plan that fails the declared rule is a
gate finding. Test first: a toy whose plan is affordable at the point but not in most draws must report the
exceedance and fail the rule. Retro-check: F5 reports 48 of 64.

**S1-2 Benchmark bands from their sources (C4).** Store each benchmark's published interval as given (lower,
upper, scale), with the source table, and derive the band from it. A script compares every band with its recorded
interval and fails on any difference. Record the estimand (odds ratio, risk or rate ratio, hazard ratio),
diagnostic and age band, and refuse a pack entry whose estimand the engine's observable does not match. Correct
B10, B11, B12 and B12b. Test first: the current pack fails the check on exactly these four.

**S1-3 Component cost structure (C2, P2 costing).** Costs can have a fixed part per unit receiving a component
(setup, supervision, capacity) beside per-person and per-outcome parts. Per-outcome increments floored at zero are
reported. The spec states what each price includes, and a price whose listed parts overlap a separate cost item is
refused. Test first: a CM-like component with a per-unit setup cost changes the toy plan as computed by hand.

**S1-4 A decision test for every scenario parameter a plan depends on (C2, C8).** The harness lists every engine
parameter of kind `scenario` that a component in the plan uses, and every fitted parameter whose sensitivity
changes the plan beyond the threshold, and generates a decision test for each. A parameter it cannot test is
declared, never skipped silently. Retro-check: F5 gets tests for `cm_add` and `cfp_kill`.

**S1-5 Decision summaries that show what moves (C8, P2).** Beside the share of units that change package, each
decision test, stability fold and sensitivity reports: regret (value lost by keeping the point plan), population
whose package changes, dollars moved between components, and units changing by group. The share threshold stays as
one criterion; a test can also change the plan on regret or dollars, with thresholds in the manifest. Stability
reports stable provision separately from stable absence.

**S1-6 Operational plan variants (P2).** The spec can ask for variants solved beside the unconstrained plan: a
minimum service (at least one component per unit), a group-wide rule (one decision per group for a component).
Each reports its value lost. In F5's tables these cost 0.16% and 0.25% of DALYs.

**S1-7 Gate and declarations (P2).** Declared findings keep their full text and required remedy. A declared-scope
report cannot use acceptance language. A check that is null (not judged) is reported as not judged, never as a
pass.

**S1-8 Launcher and reuse (operations).** Once the scheduler lists the run, a lost client connection hands over to
the watch loop and never triggers a shutdown; `run/clusters.json` is written at submission. The compute fingerprint
hashes code content only, not the commit stamp, the spec or analysis outputs written under `models/`. Real smoke
before merge.

## 5. Stage 2: model, spec and evidence (after O1 and O2)

**S2-1 Continuation comparator (C1).** A commitments table (LGA × year × component × funder) becomes a spec input
under a data contract (`model-library.md` §5.2). Committed partner activity runs in every arm; the allocatable
decision is the gap. A withdrawal comparator stays as a reported sensitivity.

**S2-2 Case management as an implementable package (C2, C7).** In the malaria engine: separate uncomplicated
treatment, referral and severe-case treatment; the CM add-on targets declared ages and settings with a ramp, and
lifts severe-care access only through a declared, sourced link. Effect scenarios 0, 0.05, 0.10 and 0.15 (the
critique's reading of Nigerian iCCM evaluations: gains of roughly 8–16 points in targeted settings), each with
linked costs from S1-3. Engine notes cite the standard model for each part (determinism roadmap R2).

**S2-3 Calibration error model and histories (C3).** A shared bias term per survey and state, so a state's LGAs share
one survey anchor instead of each adding it to its own variance; spatially correlated error for the modelled
within-state surface; survey-specific state uncertainty. Past SMC coverage anchored to state-year delivery surveys
(per-cycle and all-cycle shares), and historical coverage kept separate from future coverage. The calibration
reports the change between survey years by state as a diagnostic with a threshold. Population denominators are
reconciled against a second source.

**S2-4 Decision-focused emulator (C5).** Emulator features for season shape, SMC history and past net types;
grouped hold-outs; accuracy reported on package contrasts and on rankings near package-switch boundaries, at
calibrated values; comparator burden, reduction and treated-case cost errors propagated together, with group-level
correlation.

**S2-5 Absolute burden validation (C7).** Continuation-arm cases, severe disease, deaths and treatments by state and
age band, compared with WHO country and state profiles, GBD and survey estimates, with a declared tolerance. A gap
beyond it is a finding, not a footnote.

## 6. How this fits the factory package (`model-library.md` §5)

| Factory item | Role for F6 |
|---|---|
| §5.1 canary pipeline | launch checklist 5: an F6 canary on the real launch path |
| §5.2 data contracts | carries the commitments table (S2-1) and the benchmark source check (S1-2) |
| §5.3 joint calibration | merged; S2-3 changes its error model |
| §5.4 incremental rebuilds | S1-8's content-only fingerprint is its first step |
| §5.5 golden launch path | S1-8; F5's launches failed four times on a slow or dropped network |
| §5.6 postmortems | F5 postmortem tags: C1, C2 domain; C3, C5, C6, C8 core; C4 engine pack |

## 7. Stage 3: before launch, and Stage 4: after

**S3-1** Sign the F6 spec only after the launch checklist (§2) holds.
**S3-2** Preview: rebuild the emulator and plan from the new calibration, and run the what-if helper set (CM
scenarios, costs, SMC coverage, net kill, variants, affordability). I review it, then GPT-6.1 Sol reviews it with
the helper and web search, using the same component-by-component prompt as the F5 critique. Fix what they find
before the full analysis.
**S3-3** Engine check of the decisions the preview shows are close (CM versus SMC, dual-AI versus PBO): direct engine
runs for a sample of LGAs at the boundary values.
**S4** Run, then the result checklist, then my review and the GPT-6.1 Sol critique of the outputs, then the report.

## 8. Order, owners and dates

| Step | Owner | Target | Status |
|---|---|---|---|
| O1–O4 | owner | O3 and O4 this week; O1 and O2 before Stage 2 | O3 answered 2026-11-08: "keep within budget"; the F6 spec will carry an affordability rule (proposed: at most 5% of draws over budget); the optimiser mode that meets it (a budget reserve, `uncertainty.affordability.enforce`) is in #157 (S1-1b) |
| S1-1 affordability, S1-2 benchmark bands | lead | PRs by 2026-11-11 | S1-1 merged (#155); S1-2 merged (#154); S1-2b merged (#158) |
| S1-4, S1-5, S1-7 | lead | PRs by 2026-11-14 | S1-4 merged (#156); S1-5 merged (#162); S1-7 merged (#160) |
| S1-3, S1-6, S1-8 | lead | PRs by 2026-11-16 | S1-3 merged (#159); S1-6 merged (#163); S1-8 merged (#161: launch handover, code-only fingerprint, `reuse-check`; real smoke passed, cases A and B, 2026-11-09) |
| S2-2 case management | engine agent | PR by 2026-11-10 | #167 in review, Codex rounds 1 and 2 fixed (engine v0.12: age-targeted add-on with a ramp, `cm_severe_link` default 0, one `cm_catchment_share` for effect and cost, Collins recurrent total with no separate commodity charge; byte-identical to v0.11 when `cm_target` is 0). O2 decided: CM is an allocatable package |
| S2-3 calibration error model | lead; engine agent for part 2 | part 1 now; part 2 after the engine change | part 1 merged (#166: shared offset per survey and group, drawn with the region; optional group-weighted best point); S2-3b next (change between surveys by group with a threshold); part 2 after that: historical and future coverage as separate parameters, history anchored to delivery surveys (needs an engine parameter for past coverage) |
| S2-1, S2-4 | lead and engine agent | dated once O1 and O2 are in | waiting on O1, O2 |
| S2-5 absolute burden validation | lead | PR by 2026-11-11 | merged (#165: `validation.burden`, `analysis/burden_validation.yaml`); the Nigeria reference file (estimated state cases and deaths with intervals) is built outside the repository before the F6 spec is signed |
| S2-5b burden window, report-only rows, no published interval | lead | PR by 2026-11-10 | in review: `validation.burden.window` with `window_metrics` (past years from engine window metrics in the same runs; deaths and DALYs need an engine change); `role`, `interval` columns |
| S2-6 funded plan beside the optimised plan | lead | PR by 2026-11-11 | in review (#170: `fixed_plan` plan variant) |
| S3, S4 | lead | after Stage 2; cost quoted before launch | |

Cost: Stage 1 is laptop and review time. The F6 run needs a full recalibration (the benchmark and error-model changes
invalidate F5's calibration runs), roughly F5's calibration cost plus the analysis. I will quote it before launch.

## 9. Critique points not adopted, or adopted differently

- Undiscounted DALYs stay primary; a discounted sensitivity is added (the critique agrees this is a choice, not an
  error).
- "Retrospective analysis" (P2): F6 will state the decision date and, if the owner wants, add a forward-looking
  reprogramming question. It is not a reason to stop.
- Replacing the stochastic-run DALY accounting (remaining-life values, sequelae) is deferred; S2-5 checks whether it
  matters at the level of the decision first.
- The critique could not audit the raw survey derivations (deliberately not shared). S2-3 and the data contracts
  cover them.

Tell us what is missing from the checklists and we will use your version.
