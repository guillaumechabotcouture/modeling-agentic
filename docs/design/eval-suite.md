# Eval suite: planted-truth questions with deterministic graders

**Status:** Phase 50 α, for the owner's review (2026-10-30). Not yet run on the cloud. This note describes the planted truths, so it is never shipped to agents (`.gitattributes` export-ignore; `cloud/run_cluster.py` refuses a launch tarball that holds it).

## 1. Why

We judge the harness by single runs of about US$500 on one question (E7 attempts 7–8, the Nigeria GC7 malaria allocation). One run is one sample, and the samples are noisy: two smoke runs on the same small question differed by a whole round. The next phase, the stage-machine refactor (`stage-machine.md`: S3 fix loop, S4 code-owned state machine, S5 harness-computed numbers, S6 retiring lead-prompt stages), changes how every run is driven. Each step needs a measurement.

Each run gives four numbers:

1. **answer quality**, scored by deterministic graders against a known-good answer;
2. **rounds** to converge;
3. **dollars** (model spend);
4. **wall time**.

The planted-defect test (`scripts/regression_eval.py`, 23 of 24 defects caught) checks that the gates catch known defects. Nothing so far scores the answer itself. This suite does.

## 2. Design

The suite follows OmicsBench: a subtask DAG, JSON answer files, deterministic graders, a gated mode and a failure-mode taxonomy.

- **Five small synthetic questions.** Each covers a different disease area and type of decision. Each is meant to cost about US$20–35 and take under an hour on the existing cloud launcher (cap `--max-budget-usd 35`, at most 3 rounds, lead sign-off).
- **A known generating model per question.** A seeded, pure-Python generator (`evals/<q>/generate.py`) writes the inputs to `data/evals/<q>/`, which ship with the code like the rest of `data/`. It also writes the answer key to `evals/<q>/_truth/truth.json`. Running it twice with the same seed gives byte-identical files. The answer key is not committed; the grader regenerates it and checks it against the committed inputs.
- **The generator checks its own planted properties and stops if they fail:**
  - the planted trap changes the decision;
  - a simple estimator applied to the data alone recovers the optimum, so a careful analyst can get the right answer from what we give them.
- **A grader in two steps:**
  - `extract_answer` reads the run's own artifacts into a JSON answer: the allocation CSV, `uncertainty_report.yaml`, `final_gate.json`, `metadata.json`, `budget_ledger.json`, `validator_history.jsonl`, and the text of `report.md`, `results.md`, `decision_rule.md`, `scope_declaration.yaml` and `models/mechanism_manifest.yaml`;
  - `score` compares that answer with the truth.
  - Both steps are in `scripts/eval_suite.py`, which has no disease-specific content. The specifics are in each question's `rubric.yaml` and truth file.
- **What the question text asks for.** It names the deliverables the grader reads: `allocation_optimized.csv` with named columns, and a named output in the uncertainty analysis. Each question is labelled SYNTHETIC in its first words.
- **The answer keys stay hidden:**
  - `evals/**` is export-ignored, so `git archive` (the launch tarball) leaves it out;
  - `code_tarball` refuses any tarball that still holds hidden files (`scripts/lib/eval_paths.py`);
  - the harness manifest hashes none of them;
  - agents may not `Read`, `Grep` or name `evals/…` or `_truth` paths in Bash (`agents/enforcement.py`).
  - Self-tests L1–L7 and RC50 fail if any of this breaks.

## 3. Questions and their truths

All numbers come from the committed seed. "Value ratio" is the true benefit of a plan divided by the true optimum's.

### 3.1 `malaria_alloc` — packages across 10 districts under a budget

- **What it tests:**
  - estimating each package's effect from programme history;
  - an intervention interaction;
  - a budgeted multiple-choice allocation.
- **Generating model:**
  - Each district has a baseline all-age clinical incidence I0 of 0.05–0.62 cases per person-year, with no ITN, IRS or SMC.
  - Each package multiplies incidence by a rate ratio: ITN 0.60, IRS 0.65, SMC 0.85 (seasonal districts only), ITN+SMC 0.51, ITN+IRS+SMC 0.4675.
  - The history covers 10 districts over 2015–2024, one random package per district-year. Recorded cases follow Poisson(pop × I0 × RR × e^ε), with ε ~ N(0, 0.10).
  - The plan covers 2026–2028. The counterfactual is no intervention, because the 2022 nets have expired.
  - Costs per person over the plan: ITN US$5.40, IRS US$12.00, SMC US$2.16. The budget is US$18 M, which does not cover nets everywhere plus SMC.
- **Planted:** ITN+IRS has a rate ratio of 0.55, where a multiplicative model gives 0.39. IRS adds about 8% on top of nets, not 35%.
- **Truth:**
  - The optimum puts ITN+SMC in the 5 seasonal districts (D01, D02, D04, D06, D08), ITN in D03, D05, D07 and D10, and nothing in D09. It averts 1.268 M cases and costs US$17.87 M.
  - **The trap:** a multiplicative ITN×IRS model buys IRS on top of nets in D03 and drops nets elsewhere. Four districts differ and the value ratio is 0.968.
  - The Poisson MLE from the history recovers the exact optimum.

### 3.2 `measles_campaign` — catch-up campaign targeting with age structure

- **What it tests:**
  - an immunity profile from serology rather than from administrative coverage;
  - age targeting;
  - threshold (final-size) dynamics.
- **Generating model:**
  - 12 districts. Each single-year cohort is 3.0% of the population, everyone aged 15 or older is immune, and children under 1 are susceptible.
  - Cohort immunity is 1 − (1 − 0.85·MCV1)(1 − 0.90·MCV2).
  - The 2025 serosurvey tests 150 children per age band per district.
  - Campaign options per district: none; A_1to4 (ages 1–4); B_1to9 (ages 1–9). A campaign reaches 85% of children and seroconverts 93% of the susceptible children it reaches. It costs US$1.10 per targeted child plus US$25,000 per district. The budget is US$800,000.
  - Outcome: one importation per district, with final size N·s·z, where z = 1 − exp(−R0·s·z) and R0 = 15. The question gives R0 as 12–18.
- **Planted:** in M03, M07 and M10, the cohorts born 2020–2022 (aged 4–6 in 2026) had true MCV1 coverage of 0.40 and MCV2 of 0.25. The administrative data report the usual coverage of about 0.9; only the serosurvey shows the gap. An A_1to4 campaign misses most of the gap cohorts.
- **Truth:**
  - The optimum gives B_1to9 to M03, M05, M07 and M10; A_1to4 to M02, M04, M09, M11 and M12; and nothing to M01, M06 and M08. It averts 111,700 expected cases.
  - Six districts are near-ties: any of their acceptable options keeps the plan within 1.5% of the optimum, and the grader accepts any of them.
  - **The trap:** using administrative coverage gives A_1to4 in M03 and M07 and nothing in M10, for a value ratio of 0.756.
  - Serosurvey band estimates give a value ratio of 0.989.

### 3.3 `tb_acf` — active case-finding algorithm per district

- **What it tests:**
  - district-specific yields from screening records;
  - subclinical TB;
  - pooling against heterogeneity.
- **Generating model:**
  - 10 districts, each with an undiagnosed prevalence of 0.30–1.20% and a subclinical fraction f.
  - A screening round reaches 70% of the target population.
  - Sensitivity: SYM = (1 − f)·0.90·0.85; CXR = 0.90·0.85.
  - The ACF records cover 2022–2025 and screen 5,000 people per algorithm per district per year. Detections follow a binomial distribution.
  - Costs per person screened: SYM US$2.50, CXR US$7.00. The budget is US$1.0 M.
- **Planted:** f = 0.75 in T02, T05 and T08 (against 0.30–0.40 elsewhere), so symptom screening finds few cases there.
- **Truth:**
  - The optimum gives CXR to T02 and T05; SYM to T01, T04 and T07; and nothing to the others. It detects 1,206 cases.
  - T03 and T04 are near-ties within 1%.
  - **The trap:** pooling one CXR/SYM yield ratio across districts gives SYM in T05 and T08 and CXR in T07, for a value ratio of 0.931.
  - Using each district's own records gives a value ratio of 0.991.

### 3.4 `flu_vaccine` — doses by age group under a dose limit

- **What it tests:**
  - age-structured transmission calibrated to serology;
  - indirect protection;
  - discrete allocation.
- **Generating model:**
  - 5 M people in five age groups (0–4, 5–17, 18–49, 50–64, 65+), with a reciprocal contact matrix based on POLYMOD.
  - Final size: A_i = s_i(1 − VE_i·v_i)(1 − exp(−q Σ_j C_ij A_j)).
  - Pre-season susceptibility is 0.90, 0.75, 0.70, 0.65, 0.60; VE against infection is 0.60, 0.65, 0.60, 0.50, 0.35; hospitalisations per infection are 0.004, 0.0008, 0.0015, 0.006, 0.03.
  - q = 0.165, which gives R0 = 1.77, a pre-season R_eff of 1.28 and a last-season attack rate of 23%.
  - The data are pre- and post-season serosurveys (400 people per group) and last season's hospitalisations.
  - The decision: 10 blocks of 75,000 doses, with at most 70% of any group vaccinated.
- **Planted:** indirect protection. Children aged 5–17 drive transmission.
- **Truth:**
  - The optimum is 0, 8, 2, 0, 0 blocks, which averts 3,938 hospitalisations. It is unique within 0.5%.
  - **The trap:** counting direct protection only gives 3, 0, 0, 1, 6 (mostly 65+), for a value ratio of 0.298.
  - Calibrating q to the serosurvey (q̂ = 0.1668) gives the exact optimum.

### 3.5 `dengue_scope` — a key quantity the data cannot identify

- **What it tests:**
  - identifiability;
  - an honest, scoped answer;
  - DECLARE_SCOPE and the scope-declaration rules (A20/A21).
- **Generating model:**
  - A single DENV-2 outbreak in a city of 1.2 M.
  - A weekly renewal SIR with generation interval w = (0.15, 0.35, 0.30, 0.20) over lags of 1–4 weeks (mean 2.55 weeks), given to the agent.
  - R0 = 3.6 and s0 = 0.55, so R_eff at the start is 1.98. There are 200 seed infections.
  - Reported cases follow Poisson(ρ·inf·e^ε), with ρ = 0.04. No serosurvey is supplied.
- **Planted:**
  - The curve's shape fixes R0·s0 and its height fixes ρ·s0.
  - R_eff for 2026 = R0·s0·(1 − depletion) is identified: about 0.41. A fit on the curve alone gives 0.410.
  - The 2025 attack rate (0.435) is **not** identified. For reporting fractions from 1/3 to 1/30, anything from 0.054 to 0.535 fits.
  - Assuming 1 in 10 reported gives 0.178.
- **Truth:**
  - R_eff in 2026 is 0.413. Below 1, so no large DENV-2 outbreak is expected.
  - The attack rate should be declared as not determined, with the range the data allow and a serosurvey named as the data that would resolve it.

A sixth question was optional: a small subset of real data with a rubric for the owner to score. It is not built yet (row F6).

## 4. Scoring rules

Each question's `rubric.yaml` lists checks. Each check hangs off a subtask in a DAG:
- for the allocation questions: data → effects → {optimize, uncertainty};
- for dengue: data → identify → {scope, decision}.

| Check type | Score | Passes when |
|---|---|---|
| `inputs_referenced` | share of the input files named anywhere in the run's records | all named |
| `unit_value_accuracy` | share of units whose reported benefit (CSV value column) is within `rel_tol` of the truth for the package chosen | ≥ `pass` |
| `text_mentions` | 1 if one passage matches every pattern group within `window` characters (and names `units.min` of the planted units) | 1 |
| `allocation_concordance` | categorical: share of units whose package is in the acceptable set (the optimum plus near-ties within `tie_tol`); vector: 1 − L1 distance / (2 × blocks) to the nearest acceptable split | ≥ `pass` |
| `allocation_value` | true benefit of the run's plan / optimum, mapped linearly from `floor` (0) to 1 (1); 0 if over budget at true costs | ratio ≥ `pass_ratio` |
| `headline_accuracy` | 1 within `rel_tol` of the truth, falling to 0 at 3 × `rel_tol`. For allocation questions the truth is the true value of **the run's own plan** (estimation is scored apart from choice) | within `rel_tol` |
| `interval_coverage` | 1 if the 95% interval covers the truth and its relative width ≤ `max_rel_width`; 0.5 if it covers but is wider; 0 if it misses | 1 |
| `unidentifiable_declared` | 0.5 for declaring the quantity undetermined (pattern groups in report or scope declaration) + 0.5 for no false precision (no interval, or one that covers the truth with high/low ≥ `min_ratio`) | both |
| `gate_outcome` | 1 if the final gate action is in `expect` | 1 |

How the checks combine:

- **quality** = Σ weight × score / Σ weight, minus penalties, clamped to [0, 1].
- **quality_gated** credits a check only when every upstream subtask passed. This is OmicsBench's gated mode: a right allocation built on unestimated effects earns no optimize credit.

**Penalties:**
- `false_confidence_accept` subtracts 0.3 when the gate ACCEPTs an answer whose quality before penalties is below 0.6.
- `confident_wrong_interval` subtracts 0.1–0.15 per interval that misses the truth while narrower than the rubric's width.

**Proposed weights:**

| Question | inputs | unit estimates | mechanism | concordance | plan value | headline | interval |
|---|---|---|---|---|---|---|---|
| malaria, measles, TB | 0.05 | 0.10 | 0.15 | 0.20 | 0.20 | 0.15 | 0.15 |
| flu | 0.05 | — | 0.20 | 0.25 | 0.25 | 0.10 | 0.15 |

For dengue the weights are: inputs 0.05, R_eff headline 0.15, R_eff interval 0.15, attack-rate scope 0.35, resolving data 0.10, conclusion 0.10, gate outcome 0.10.

**Failure taxonomy:**
- `evals/taxonomy.yaml` maps each failure code to the harness layer most likely at fault, in order from upstream to downstream: INFRA, ORCHESTRATION, DATA, MODEL, OPTIMIZE, UQ, CRITIQUE, GATE, WRITE.
- Each code also lists where to look and the layers that should have caught it.
- Refine rules separate causes. For example, a discordant allocation is blamed on MODEL when the unit estimates or the mechanism check also failed, and on OPTIMIZE when they passed.
- `primary_layer` is the most upstream layer among a run's failures.

**Self-test:** for every question, a hand-built perfect answer scores 1.00 (gated 1.00). A planted wrong answer scores 0.00. The wrong answer is the trap's allocation, a headline twice the truth with a narrow interval, no mechanism named, and ACCEPT. It is penalised for false confidence and blamed on MODEL.

## 5. Running and comparing

```bash
python3 scripts/eval_suite.py generate --check            # inputs match the generators
python3 scripts/eval_suite.py run --tag main --reps 2     # preflight + launch commands (prints only)
python3 scripts/eval_suite.py run --tag main --reps 2 --execute     # launches; costs money
EVAL_TAG=main python3 cloud/run_cluster.py cloud/configs/eval_<q>.py collect
python3 scripts/eval_suite.py grade --root ~/modeling-agentic-keep/evals/main
python3 scripts/eval_suite.py report <A>/suite_scores.json <B>/suite_scores.json
```

How to run each arm:
- An architecture branch runs with `EVAL_TREE=<committed checkout> EVAL_TAG=s3`. Its tree must hold `data/evals/`, or the launch refuses.
- `run` refuses to launch when the inputs are stale or the tarball of HEAD holds hidden files.
- A question with no collected run scores 0 (`run_record_missing`).

**Expected cost:**
- **Per question:** US$20–35 in model spend, capped at US$35, and under an hour. For comparison, the 8-district malaria smoke on 2026-10-31 (cap US$25, 2 rounds) cost US$14.05 in 46 min and stopped after round 1.
- **Per suite run:** five questions × one replicate is about US$100–175 (at most US$175), plus Coiled compute VMs, which `report-cost` reports separately. Two replicates, as recommended for comparing arms, cost about US$200–350.
- **An A/B comparison at 2 replicates:** about US$400–700.
- The clusters run in parallel, so a suite takes about an hour of wall time.

**Reading a comparison:** `report A B` shows the change in quality per question and whether it falls within the replicate spread. With one replicate per arm it cannot separate a change from noise, and it says so.

## 6. Limits

- **Synthetic and simplified.** The truth is the optimum of the generating model. The question text fixes the parts of the model structure that would otherwise make several answers defensible (homogeneous mixing and final size for measles, SIR with the given contact matrix for flu, a stable prevalence for TB). The suite therefore tests estimation from data, mechanism discovery, optimisation, uncertainty and honesty. It does not test the choice of model structure.
- **Lexical mechanism checks.** The checks match regular expressions within a window, so a negated sentence ("there is no redundancy") would still match. The allocation and value checks do not have this weakness, and a missed mechanism usually shows up there too.
- **The repository is public.** The generators are visible on GitHub. The question text does not name the repository, and the data are synthetic. Cloud workers never receive `evals/`. In local runs, reads are refused by path, but a Python string built at run time (`os.path.join("evals", …)`) is not caught. Run the suite on the cloud.
- **Dollars are model spend only** (`metadata.json` `total_cost_usd`). VM credits come from `run_cluster.py report-cost`.
- **The questions are small.** They exercise the full pipeline at about 1/15 of an E7 run's cost. They do not replace an E7-scale run as the final check.

## 7. For the owner's review

1. **Are the generating models plausible?** In particular:
   - malaria rate ratios and the strength of the ITN×IRS redundancy (0.55 against 0.39);
   - SMC's all-age effect (0.85);
   - measles: the gap depth (MCV1 0.40) and the final-size threshold framing with R0 12–18;
   - TB subclinical fractions (0.75 against 0.30–0.40) and algorithm sensitivities;
   - flu contact matrix, VE, hospitalisation rates and R0 1.77;
   - dengue: R0 3.6 with prior immunity of 45%, the generation interval, and ρ = 0.04.
2. **Are the rubric weights and tolerances right?**
   - mechanism 0.15–0.20 against allocation 0.40–0.50;
   - headline tolerances of 10–20%;
   - interval width caps;
   - the false-confidence floor of 0.6 and penalty of 0.3;
   - near-tie tolerances (malaria 0.2%, measles 1.5%, TB 1%, flu 0.5%).
3. **Do the mechanism patterns accept the wording an expert would use?** The patterns are in each `rubric.yaml`. Tell us what is missing and we will use your version.
4. **Should F6 (a real-data subset, scored by the owner) be built next, or a sixth synthetic question?**
