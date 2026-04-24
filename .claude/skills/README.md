# Skills Index

45 skills attached per-agent in `agents/__init__.py`. Each skill is a directory with a `SKILL.md` that declares its name/description frontmatter and the guidance body.

## Pipeline rigor (STAGE 6 + STAGE 7 backstops)

These skills support the mechanical gates and adversarial review.

- **adversarial-redteam/** — Five duties of the red-team critique agent: cross-file numeric audit, external sanity checks, methodological fidelity vs cited prior work, data-vintage vs decision-year, hidden operational assumptions.
- **critique-blockers-schema/** — Required schema for `critique_*.yaml`: blocker IDs, severities, `target_stage`, categories, `structural_mismatch`, and how the validator computes `unresolved_high` from them.
- **spec-compliance-rules/** — Rules 1–6: framework (Starsim/LASER/stisim/EMOD) verification, approach (ABM vs ODE), budget envelope utilization, archetype K<N with `**Within-archetype error**` bound, data vintage (`**Vintage**: YYYY`), methodological vintage.
- **effect-size-priors/** — Parameter Registry YAML schema in `citations.md`; kind-weighted severities (OR/RR/efficacy/cost_usd/proportion); `@registry:NAME` tagging convention; tagging coverage check (registry → code direction) to catch the R-022 "registered-but-not-propagated" class.
- **uncertainty-quantification/** — `outcome_fn(params) -> dict` contract; 200-draw Monte Carlo propagation via `propagate_uncertainty.py`; surrogate-emulator pattern when the full model is slow.
- **multi-structural-comparison/** — ≥3 candidate structures (null/simple/full); LOO-CV vs training RMSE; DEGENERATE_FIT_DETECTED pattern.
- **identifiability-analysis/** — Fisher SE diagonal + profile-likelihood scans; ridge-trapped parameters as HIGH blockers; resolution via partial pooling, tied parameters, or scope declaration.
- **decision-rule-extraction/** — Required when allocation CSV is produced: `decision_rule.md` schema (tabular / tree / prose-with-exceptions / non-compressible) with `accuracy_vs_optimizer` and exceptions list. Prevents shipping 774-row tables without a defensible rule.

## Modeling strategy & evaluation

- **modeling-strategy/** — Level 0/1/2 progression; AIC/BIC thresholds; when to add complexity.
- **modeling-fundamentals/** — Model classes, when to use each, common pitfalls.
- **model-fitness/** — Fit-for-purpose gate: audience, mechanism test, simpler-question test.
- **model-validation/** — Out-of-sample validation, holdout designs, cross-validation.
- **investigation-threads/** — `threads.yaml` structure; hypothesis → data → model → figure → finding chains.

## Epi fundamentals

- **basic_epi_modeling/** — Disease burden, demography, transmission routes, compartmental basics.
- **sir-models/** — SIR equations, R0, effective reproduction number, interventions.
- **sir-elaborations/** — SEIR / MSIR / SIRS extensions; age structure; contact heterogeneity.
- **parameter-estimation/** — Least squares, MLE, Bayesian inference, chain binomial, TSIR.
- **vaccination/** — Herd immunity, vaccination strategies, vaccine failure modes, eradicability.
- **vectors/** — Ross–MacDonald model, SIWR environmental reservoirs, multi-strain dynamics.
- **surveillance/** — Surveillance types, forecasting, forecast evaluation, genomic epi.

## Disease-specific

- **malaria-modeling/** — Malaria-specific modeling guidance: interventions (ITN/IRS/SMC/ACT), archetypes, seasonality, vector biology.

## Frameworks

- **laser-spatial-disease-modeling/** — LASER (Light Agent Spatial modeling for ERadication): per-patch SEIR + gravity-coupled spatial model + seasonal forcing + campaigns.
- **starsim-dev/** — Starsim framework index skill.
  - **starsim-dev-intro/** — Architecture overview.
  - **starsim-dev-sim/** — `ss.Sim` lifecycle.
  - **starsim-dev-diseases/** — Disease classes (SIR, SIS, custom SEIR).
  - **starsim-dev-networks/** — Contact networks (random, sexual, maternal, household).
  - **starsim-dev-interventions/** — Routine and campaign intervention delivery.
  - **starsim-dev-demographics/** — Births, deaths, aging.
  - **starsim-dev-calibration/** — Built-in Optuna calibration.
  - **starsim-dev-analyzers/** — Result extraction and post-hoc analysis.
  - **starsim-dev-time/** — Time units and conversion.
  - **starsim-dev-random/** — Random state, seeding, reproducibility.
  - **starsim-dev-distributions/** — `ss.Dist` distributions.
  - **starsim-dev-connectors/** — Multi-disease connectors.
  - **starsim-dev-nonstandard/** — Non-standard usage patterns.
  - **starsim-dev-indexing/** — Agent state indexing.
  - **starsim-dev-profiling/** — Profiling and performance.
  - **starsim-dev-run/** — Running, parameter sweeps.
- **stisim-modeling/** — STIsim framework guidance.

## Infrastructure

- **modelops-calabaria/** — Calibration infrastructure; scaling calibration runs.
- **cloud-compute/** — Azure Batch for slow `outcome_fn` evaluations; pool types (dedicated Standard_D4s_v5 vs free-trial Standard_A2_v2); budget guards; auto-teardown.
- **sciris-utilities/** — `sciris` utilities (odict, save/load, plotting helpers).
- **epi-model-parametrization/** — Finding published parameter values; parameter provenance.
