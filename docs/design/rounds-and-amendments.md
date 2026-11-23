# Rounds and amendments: what a run's rounds are for

Status: **design only** (2026-11-23). No code changes. Base: `origin/v2` at `da45b505` (after #357 and #367).

This note sets out what the review rounds of a v2 run should do, given what 31 v2 runs show they did. It covers
four things. Findings that need a change to the model or the signed spec become a structured **amendment proposal**
at the first gate, and the owner or lead decides each one. The **fix loop** is replaced by a deterministic settings
step. The writer keeps its revision rounds. It also says how decision (b), #357, the new stability finding and the
architecture wave's lead-amendment redesign (item 6) fit together.

Counts and statuses only. This note quotes no run output value (burden, plan, cost of a plan, fit). The dollar
figures are model API spend by harness stage, not values computed by a run.

## 1. Terms (defined once)

| term | meaning here |
|---|---|
| **round** | one critique of the analysis, its gate, and what the gate routes to. `--max-rounds` caps them (default 3). |
| **gate** | `scripts/validate_critique_yaml.py` `decide` plus the v2 rules in `agents/stage_machine.py` (`apply_medium_rule`, `apply_held_rule`, `apply_owner_declines`). Rule numbers as in `decide`: 1 structural, 2 patch, 2o ask the owner now (#357), 3 declare at the cap, 3h/3m declare early, 4 accept, 4m patch for MEDIUMs, 5 fail. |
| **fix loop** | `FIX_LOOP` (`agents/fix_loop.py`). An LLM planner turns open findings into tasks. A fix worker may edit only `analysis_settings.yaml` (four leaves: `uncertainty.draws`, `uncertainty.seed`, `emulator.points`, `emulator.seed`) and `models/ext/`. Then the harness rebuilds, re-checks and writes `impact_ledger.jsonl`. |
| **rethink** | a back-edge to SPEC or ENGINE_CHOICE (gate rule 1, or a planner `rethink` task). For a signed spec it writes `spec_rethink_request.json`. Under `--signoff lead` with a valid patch, `toolkit/spec/amend.py --from-request` then makes a child run (#330/#349, status `amended_provisional`); otherwise the run pauses `awaiting_spec_amendment`. |
| **owner question** | a finding no task can change, which the owner answers (accept: the run goes on with it stated; decline). Under `--signoff lead` the lead answers provisionally. |
| **productive** | a finding that changed a plan, a headline (more than 0.5%) or a decision-test verdict, inside the run or in the next signed spec or a harness PR. |
| **MCSE** | Monte Carlo standard error: the noise in a number computed from a finite set of draws. |

## 2. Evidence

Sources:
- the per-check analysis of 31 v2 runs with a findings ledger (hb0 to hb2, earlier evals, eight Nigeria runs; 486 ledger rows, 109 fix tasks);
- the hb2 triage (8 runs, commit `53ff2372`);
- the two hb3 malaria runs collected so far (commit `dea0beec`, after #357). The tb and dengue hb3 runs were still running when this was written, and hb3 has no measles arm.

The ops files are outside the repository.

### 2.1 What the fix loop did (31 runs, 109 tasks)

| what the task changed (`impact_ledger.jsonl` `files_changed`, harness files excluded) | tasks | passed its check | model spend |
|---|---|---|---|
| nothing | 82 | 6 | about $20 |
| `manuscript.md` only (before #344 sent write-up findings to the writer) | 17 | 8 | about $7 |
| `analysis_settings.yaml` | 2 | 0 | under $1 |
| `models/ext/` (a markdown note, no code) | 1 | 0 | about $1 |
| rethink (routed to SPEC or ENGINE_CHOICE; the run then ended) | 7 | – | – |

- **No task changed a plan, a headline or a decision-test verdict.** One moved interval ends by about 1% (draws 400 → 401 to align two quantile estimators). Of the other settings change, one raised emulator design points, and the same finding fired again in the next run.
- **Many briefs asked for model changes a task cannot make.** Examples: per-cohort coverage instead of one scale, a dose-dependence assumption, an expected-value objective instead of the posterior mode, trial-anchored effects. Every one of these took effect only across runs, through an owner amendment or a harness PR (#343 for the expected-value plan).
- The fix loop took $36 of $217 of model spend (17%), and fix tasks took $29.

### 2.2 Where the productive catches came from

| source | runs | how it acted |
|---|---|---|
| critic HIGH METHODS (domain and red team) | 27 | owner choices after an estimand mismatch (trial ratios over the wrong window); PR #343 (plan at the mode, not the expected value); an engine change for routine coverage |
| critic HIGH HYPOTHESES | 21–25 | the owner decision to anchor net effects to trials; an unsourced add-on effect made a range with costs |
| critic HIGH CAUSAL | 7 | two trials with different estimands compared directly (same amendment) |
| harness `calibration_change_discrepancy` | 3 | a declared calibration miss tied to a decision test, in the next spec |

Every one acted **across runs**, through a spec amendment or a PR. None acted through the fix loop.

### 2.3 Rounds that had nothing to do

- **hb2, before #357:**
  - 6 of 6 allocation runs used all 3 rounds.
  - Rounds 2 and 3 re-ran both critics, with fix loops that planned 0 tasks (15 of 15 fix-loop visits). The only open HIGHs were `decision_test_changes_plan`, which is an owner question.
  - Cost: about $12 over the six runs, and 23 owner-question visits (re-asked whenever a critic reworded a finding).
- **hb3 malaria, after #357** (2 runs):
  - Both ended `completed` (rule 4, post-write).
  - The plan-changing decision tests went to the lead at the first gate (rule 2o).
  - 0 fix tasks ran. The fix loop cost only its planner call, under $0.25 a run.
  - Every round after the first was a **writer revision with a re-review**, and the re-reviews still found real slips (a test attributed to the wrong unit in the summary, wording about which part of the rule is robust).
  - One MEDIUM METHODS finding (the uncertainty interval draws the effect parameters but not the baseline rates) had no fix in scope. The planner declared it a harness gap and the writer disclosed it. That is a model-change finding handled as a caveat with no route to a change, which is the gap this design closes.

### 2.4 What follows

| observation | consequence for the design |
|---|---|
| In-run fix tasks never changed the answer | the LLM fix loop has no evidence of value and should go |
| The 2 settings changes were mechanical (draw count, emulator points) | harness code can make them by rule without an LLM |
| Model and spec findings acted only across runs, with no record linking them | make them a structured proposal at the first gate, with a decision and a link that can be measured |
| Plan-changing decision tests and stability are questions for the owner | the owner gets them at the first gate (#357 does this for decision tests) |
| Writer revisions find and fix real wording errors | keep them as the main use of later rounds |

## 3. Design: five lanes

Each open finding at a gate falls into exactly one lane, assigned by code from ledger fields (`owner_state`,
`category`, `severity`, `kind`, `resolution_type`, `fix_requires`), with no LLM call. The lane says who acts and
when.

| lane | what goes there | who acts | when | what the run does |
|---|---|---|---|---|
| **W write-up** | `owner_state` WRITE/PRESENTATION; category PRESENTATION; MEDIUM/LOW METHODS, HYPOTHESES or DATA findings whose remedy is a disclosure | writer | after the first gate, then each revision | revise the draft; the domain and red-team critics re-review it (as #344 does today) |
| **O owner question** | `decision_test_changes_plan`; allocation stability UNSTABLE or FRAGILE (new finding, §6.3); held-out target conflicts; any `resolution_type: owner_decision` | owner, or lead provisionally | **first gate** (rule 2o) | one request per gate. Accept: the dependence is stated at the top of the report and ACCEPT stays possible (decision (b)). Decline: never ACCEPT. |
| **S settings** | harness findings with a registered settings remedy: Monte Carlo error too large for a reported number or verdict; emulator accuracy or boundary ranking below threshold | harness code | inside ANALYZE, before the first critique | raise the setting once by a fixed rule, re-run with reuse, and record it; if still failing, declare it with a decision test (§3.3) |
| **A amendment proposal** | critic HIGH in METHODS, HYPOTHESES, CAUSAL or STRUCTURAL; any finding whose remedy starts "Owner amendment:"; a structural mismatch; harness HIGHs with no settings remedy (`calibration_change_discrepancy`, a declarable `calibration_verification_failed`) | owner, or lead within limits (§3.4) | **first gate** | a proposal (what is wrong, the spec patch or the engine capability it needs, the expected effect, the cost of re-analysis). Choices: amend now, declare, or defer. |
| **H harness gap** | an A-lane item whose remedy is a change to harness or engine code (`needs` names `toolkit/` or an engine), which no run can make | maintainer | across runs | declared in the report with what it needs; filed as a work item linked to the finding key |

Integrity and report checks keep their own routes and are not lanes. These are `analysis_contract_*`, the
report claim-reference checks (with the writer repair from #353), acceptance language, and schema errors.

### 3.1 Classification order (code)

The lane of a finding is the first that matches:

1. Integrity or report check → its existing route.
2. `owner_state` WRITE/PRESENTATION, or category PRESENTATION → **W**.
3. `resolution_type: owner_decision`, or a kind in the owner-question registry (decision test, stability, conflict) → **O**.
4. A harness kind with a registered settings remedy, and that remedy not yet applied in this run → **S** (already handled in ANALYZE; at the gate it is only reported).
5. HIGH and (category METHODS, HYPOTHESES, CAUSAL or STRUCTURAL, or an "Owner amendment:" remedy), or a harness HIGH with no settings remedy → **A**.
6. Everything else (MEDIUM/LOW METHODS, HYPOTHESES or DATA) → **W**, as a disclosure. It is also listed in the proposal file under "for a later run", so it is counted.

An A item whose drafted remedy needs code (§3.4) becomes **H**. The lane is recorded on the ledger row
(`lane`), so the next baseline can audit it.

### 3.2 Lane W: the writer's rounds (kept)

This lane keeps the #344 route as it is. The writer revises the draft for the open W findings, and the domain and
red-team critics re-review the revised draft. A re-review that raises only W findings sends the run back to the
writer, up to `max_revisions` (default 2, the most either hb3 malaria run used). After that, the open W findings
are declared in the write-up. The presentation reviewer's findings stay advisory to WRITE, as the per-check
analysis recommends.

### 3.3 Lane S: computational settings that do move outputs

Only four settings may change inside a run. Each has a statistic the harness can compute, so whether a setting
matters is measured, never argued.

| setting | statistic (harness-computed, from stored draws or rows) | rule | never |
|---|---|---|---|
| `uncertainty.draws` | (a) the MCSE of each reported interval end and each per-unit stability share; (b) a split-half check, comparing the plan, the headline rounded as reported, and each decision-test verdict between the two halves of the stored draws (this costs no engine runs) | raise draws ×2, once, when an MCSE could change a reported rounding or verdict (a share within 2 MCSE of its threshold), or the halves disagree; only if the projected cost fits the run cap | raised more than once; raised to make a verdict pass |
| `uncertainty.seed` | the split-half check above | none: a seed is never chosen to change a result | changed by any rule |
| `emulator.points` | the existing emulator accuracy metrics (fit error, boundary ranking agreement) | raise points ×1.25, once; still failing → declare it with a direct-engine decision test on the boundary units | raised more than once |
| `emulator.seed` | the same | none | changed by any rule |

Each check writes `analysis/settings_probe.yaml`. For each setting it records the statistic, the threshold, the
action, and the headline and verdicts before and after. Over baselines this answers which settings ever move
outputs: a rule whose changes never move a reported rounding or verdict across about ten applicable runs is a
candidate to drop. The 2 settings changes in §2.1 are the two cases this table covers.

### 3.4 Lane A: the amendment proposal

**What a proposal holds.** It is one item type in the single owner request of that gate (the format #357 uses
today), so the owner reads it with `scope_signoff.py show`. Each item has:

| field | content |
|---|---|
| `findings` | the ledger ids and their keys (id plus remedy class, as hb2 fix 3 keys owner answers) |
| `class` | estimand, mechanism, objective, effect source, comparator, data frame, or engine structure |
| `what_is_wrong` | the critic's claim, quoted |
| `remedy` | either a **spec patch** in `toolkit/spec/amend.py` format (set, add or remove by dotted path), or **`needs`**: the engine or toolkit capability it requires (then it is lane H) |
| `expected_effect` | which outputs or decision tests the patch should move, and the decision test that would show whether it matters |
| `cost` | the projected cost and time of the re-analysis, with reuse (a spec-only change to inputs reuses stored batches where the reuse key allows) |
| `choices` | `amend_now`, `declare`, `defer` (and `decline_stop`, owner only) |

**Who drafts it.** A bounded "amendment drafter" task, called only when lane A is not empty. It reuses the fix
planner's `rethink` and `patch` output format and the decision design principles. It drafts the remedy and the
expected effect. The harness validates the patch: it applies, it validates, and it changes only paths the finding
names. Spec critics then run on the patched copy, as `amend.py` already does. The harness fills in the cost from
the batch reuse report.

**The choices.**

| choice | what happens in the run | report | status the run can reach |
|---|---|---|---|
| `amend_now` | a new spec version in this run (§6.4), signed, then ANALYZE with reuse, then one critique of the new analysis | the amendment heads the report, provisional under lead | any |
| `declare` | the run goes on with the finding declared | stated near the top, with who decided | DECLARE_SCOPE at best (open question 4) |
| `defer` | as declare, and the proposal is filed for the next run or a PR, linked by finding key | as declare | DECLARE_SCOPE at best |
| `decline_stop` (owner) | the run stops before WRITE | – | `completed_scope_declined` |

**Limits under `--signoff lead`.** The lead may choose `amend_now` only when all of these hold:
- the patch is spec-only;
- it touches only the paths the finding names;
- it adds no new data source;
- the spec critics are clean;
- the projected cost fits the remaining cap;
- no amendment has been made in this run yet (`max_amendments` = 1 under lead).

Otherwise the lead chooses `defer`. Under owner sign-off the run pauses once per gate for all O and A items
together. Later, a signed `owner_policy.yaml` (architecture research P4) can pre-authorise classes of amendment.

**Linking across runs.** `proposals.jsonl` in the run directory holds every proposal with its decision. A child
or amended spec version records `from_proposal`. A harness PR's change fragment cites the finding key. This is the
"finding → amendment or PR" record the per-check analysis found missing: from the next baseline on, it makes the
productive rate of A-lane findings measurable instead of reconstructed by hand.

### 3.5 Lane H: harness gaps

This keeps the #341 and #357 mechanism. The gap is declared on the finding with its `needs`, it never keeps a
round open, and the writer states it. It adds one thing: the gap is also a `proposals.jsonl` row with
`remedy.needs`, so a maintainer sees which capabilities runs keep asking for. An example is drawing baseline
rates jointly with effects, from hb3.

## 4. The fix loop: shrink, scope or remove

| option | what it keeps | evidence for | evidence against |
|---|---|---|---|
| keep as is | LLM planner and fix worker over settings and `models/ext/` | none in 109 tasks | 0 productive tasks; 82 changed nothing; $36 (17% of spend); 15 of 15 hb2 visits planned nothing |
| scope to settings (LLM chooses settings) | the planner for the 4 leaves | 2 tasks changed settings | both were mechanical; an LLM choosing draws or seeds invites tuning a result; a rule does it more cheaply and with a record |
| keep `models/ext/` | code extensions in the run | none (1 task, a markdown note, failed) | an extension bypasses the engine contract and its benchmarks; engine changes go through PRs (engine registry #367) |
| **remove (recommended)** | the settings rules (§3.3) in harness code; the planner's useful outputs move elsewhere: the rethink patch to the amendment drafter, harness-gap declaration to the lane rules | §2.1, §2.3 | loses an LLM pass that sometimes classified findings well; the lane rules and the drafter cover that |

**Recommendation: remove the LLM fix loop from the default route now, and delete its code after the next
baseline confirms the change.**
- `GATE → FIX_LOOP` stops being a route. Rule 2 and rule 4m route by lane instead.
- `FIX_LOOP` stays as a handler only so that older runs can resume, and so that one comparison arm can use it
  behind `--fix-loop` (open question 1).
- `FIX_EDITABLE`, `FIX_PLANNER_PROMPT`, `validate_plan`, `default_plan` and `impact_ledger.jsonl` writes leave the
  default path. `spec_patch` and the patch validation move to the drafter.

## 5. Rounds, cost and time

### 5.1 What a round becomes

`--max-rounds` today counts critique passes of any kind. The proposal separates three budgets:

| budget | default | counts | replaces |
|---|---|---|---|
| analysis passes | 1 + amendments | ANALYZE then a critique of the analysis; the settings re-run is inside ANALYZE and is not a pass | rounds 1 to 3 pre-write |
| `max_amendments` | 1 (lead), 2 (owner) | in-run spec versions from `amend_now` | the cross-folder hand-off (#330) |
| `max_revisions` | 2 | writer revisions, each re-reviewed | write-up rounds (#344) |

`--max-rounds N` stays as an alias, setting `max_revisions = N − 1`, so existing launch configs keep working. The
gate's `rounds_remaining` is computed from the budget that applies to the open lanes.

### 5.2 Effect on the runs we have (counts from the records; dollars are model spend)

| set | today | under this design |
|---|---|---|
| hb2 allocation runs (6) | 3 rounds each; rounds 2–3 re-ran critics with 0 tasks (about $12 in total); 23 owner visits | 1 analysis pass; owner questions at gate 1 (one request per gate, keyed answers); writer revisions as needed. About $2 a run saved, and the re-asked owner visits go (already mostly done by #357) |
| hb3 malaria (2) | `completed`; 2 and 3 rounds, all after the first were writer revisions; fix loop = planner call only | the same path, minus the planner call (under $0.25 a run). The MEDIUM METHODS gap is filed as an H proposal instead of only a caveat |
| fix loop over 31 runs | $36 model spend | $0; the drafter runs only when lane A is not empty (about $0.3–1 a call, the planner's cost) |
| a run with an A-lane HIGH (for example the four Nigeria runs whose critics drove amendments) | the finding went to a fix task that could not act, or to a rethink that ended the run; the change came in a new launch, by hand, sometimes 17 h later | the proposal at gate 1; `amend_now` re-analyses in the same run with reuse. That adds one ANALYZE and one critique (minutes on the package-rate engine; hours and cloud cost on the malaria engine, so the cost field and the cap matter) |

Time: removing the empty critique rounds saved most of the hb2 rounds-2-and-3 time, and #357 already captures
most of that. The new cost is the `amend_now` re-analysis, which is a deliberate, priced choice rather than an
automatic round.

## 6. How the pieces fit

| piece | status | role in this design |
|---|---|---|
| decision (b) (owner, 2026-11-23) | merged in #357 | lane O: an accepted plan-changing decision test may ACCEPT, with the dependence stated at the top. hb3 malaria, both runs: `completed` with the dependence stated. |
| #357 first-gate owner questions (rule 2o, owner-held, harness gaps) | merged | the request mechanism lanes O and A use. Rule 2o widens from `owner_resolution` items to every O and A item, still one request per gate, with answers keyed to finding id plus remedy class. `apply_held_rule` and 3h stay: they are how a held item stops holding a round. |
| new stability finding (another agent; check-value recommendation) | in progress | lane O for UNSTABLE (HIGH) and FRAGILE (MEDIUM). The owner chooses between delivering the robust core with a coarser rule (principle 7, decide at the granularity the evidence supports) and accepting the per-unit list as stated. For NOT_RUN on an allocation deliverable, lane A drafts a mechanical patch (add `holdout_group`). |
| architecture wave item 6: a lead amendment is a new spec version in the same run, journaled | approved, not built | the engine of `amend_now`. SIGN of version k+1, then ENGINE_CHOICE or ANALYZE with reuse, then CRITIQUE (an analysis pass), then GATE, all in one run directory. Until it lands, `amend_now` under lead is today's #330 child hand-off, and under owner it is today's pause. |
| one owner request format and `owner_policy.yaml` (architecture P4, Waves 2–4) | planned | proposals are one item type in it; the policy can pre-authorise classes of amendment |
| executable `done_when` for blocking (architecture item 2) | planned | consistent: an A-lane HIGH is not "fixed" by text; it is decided. A W finding with an executable test closes by it. |
| critique checklist (#366, open) | open | gives the critics fixed structural questions, which makes the category the lane rules read more reliable |

Flow after the change (pre-write):

```
ANALYZE (+ settings step) → CRITIQUE → GATE ─ classify by lane
   ├─ O or A items → OWNER_DECISION (one request) → GATE
   │      └─ amend_now → SPEC v(k+1) → SIGN → ANALYZE (reuse) → CRITIQUE → GATE
   └─ otherwise → WRITE → PRESENTATION → GATE (post-write)
          └─ W items open, revisions left → WRITE (re-review) … → FINAL
```

## 7. Failure modes, and how to measure them in the next baseline

| failure mode | why it could happen | guard | metric (hb4) |
|---|---|---|---|
| a finding that could be fixed in the run is sent to the owner | the lane rules read the critic's category, which can be wrong | checklist (#366); lane recorded on each row | hand audit of 20 random rows: share in the wrong lane (target ≤ 10%) |
| owner overload under owner sign-off | every A HIGH becomes a question | one request per gate; keyed answers; later `owner_policy.yaml` | owner or lead visits per run (target ≤ 2); items per request |
| the lead rubber-stamps its own critics' proposals | the lead and critics are LLMs | lead limits in §3.4; spec critics on the patch; provisional label at the top of the report | `amend_now` count under lead; share later reversed by the owner |
| amendment loop does not converge | the new analysis raises a new HIGH | `max_amendments`; a second-pass HIGH on the same key goes to `defer` | analysis passes per run; repeat finding keys after an amendment |
| reports become lists of caveats | `declare` and `defer` are cheap | DECLARE_SCOPE, not ACCEPT, for a declared A HIGH (open question 4); the proposal is filed for the next run | declared A-lane HIGHs per run; outside-judge F3/F4 scores not worse than hb3 |
| settings rules hide noise or tune results | draws raised until a verdict passes | one raise, a fixed factor, never a seed; probe record with before and after | `settings_probe.yaml` actions; share that moved a reported rounding or verdict |
| costly re-analysis on heavy engines | `amend_now` on the malaria engine | the cost field; cap check before the choice; reuse report | spend and wall time per amendment |
| loss of the fix loop's occasional good classification | the planner sometimes declared gaps well | drafter for A; lane rules for H | H items with a `needs` list (target 100%) |
| resumes of older runs at FIX_LOOP | state saved before the change | handler kept until deletion | resume smoke on one saved FIX_LOOP state |

**Next-baseline protocol.**
- Same questions, two replicates, `--signoff lead`.
- One arm uses this design. If the owner wants a direct comparison (open question 1), a second arm on two
  questions keeps `--fix-loop`.
- Report per run: status; analysis passes, revisions and amendments; dollars by stage (fix-loop dollars should be
  0); owner/lead visits; proposals by choice; amended runs whose plan, headline or verdict changed (now measurable
  inside one run); share of A-lane findings with a link; the settings probe actions; wall time.
- **Smoke markers:**
  - `findings.jsonl~"lane"`;
  - `proposals.jsonl`;
  - `analysis/settings_probe.yaml`;
  - `run_state.json~amendment` (after item 6);
  - no `FIX_LOOP` in `run_state.json` history.

## 8. Implementation order

Each PR is test-first and under 800 lines, with a change fragment.

| PR | content | size | depends on |
|---|---|---|---|
| R1 | lane classification on ledger rows; rule 2 and 4m route by lane; GATE stops routing to FIX_LOOP by default (`--fix-loop` keeps it); rule 2o widened to A items, with accept or decline answers only, until R3 | medium | – |
| R2 | settings step in ANALYZE: MCSE, split-half check, emulator rule, `analysis/settings_probe.yaml` | medium | – |
| R3 | proposal item type, amendment drafter task (from the planner's rethink and patch), `proposals.jsonl`, `from_proposal` on amendment records | medium | R1 |
| R4 | `max_revisions` and `max_amendments`, the `--max-rounds` alias, `harness_metrics.py` columns | small | R1 |
| R5 | `amend_now` as a new spec version in the run | large (architecture wave item 6) | R3 |
| R6 | delete the fix loop code and its self-tests | medium (deletion) | hb4 result |

## 9. Open questions for the owner, each with a recommended default

| # | question | recommended default |
|---|---|---|
| 1 | Remove the LLM fix loop outright, or keep it behind `--fix-loop` for one comparison arm in hb4? | Off by default now. One small arm (2 questions × 1 replicate) keeps it in hb4. Delete it after hb4 if that arm shows no productive task. |
| 2 | Under `--signoff lead`, may the lead choose `amend_now`? | Yes, once per run, within the §3.4 limits; the report heads with it as provisional. |
| 3 | Under owner sign-off, does the run pause at the first gate for A items? | Yes: one pause per gate for all O and A items together. Pre-authorise classes in `owner_policy.yaml` when it exists. |
| 4 | Can a declared (not amended) A-lane HIGH reach ACCEPT, as an accepted decision test can under (b)? | No: DECLARE_SCOPE. A methods defect is a limitation, not an accepted dependence. An accepted O item can still ACCEPT. |
| 5 | How many writer revisions? | 2 (`max_revisions`), the most hb3 used; then the open W findings are declared. |
| 6 | Settings rule thresholds | Raise draws ×2 once when a reported share is within 2 MCSE of its threshold, an interval end's MCSE could change its reported rounding, or the split halves disagree; emulator points ×1.25 once; never a seed. Each only within the run's cap. |
| 7 | MEDIUM METHODS or HYPOTHESES findings: writer disclosure or proposal? | Writer disclosure, and listed in `proposals.jsonl` as "for a later run" so they are counted. |
| 8 | `max_amendments` under owner sign-off | 2. A third becomes `defer`. |
| 9 | Should a HIGH from a single critic be enough for lane A, or only when both critics raise it? | One is enough: they reached different productive items in the records. |
| 10 | Where does the cross-run link live? | `proposals.jsonl` in the run, `from_proposal` in amendment records, and the finding key in PR change fragments. A query over the keep folder reports the productive rate. |
