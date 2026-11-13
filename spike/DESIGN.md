# Architecture-v2 spike: design note

Question the spike answers: does "a validated, reusable engine plus analysis computed
by harness code" beat attempt 8's "write everything per run" on the same question
(Nigeria GC7 malaria allocation)? See `docs/design/architecture-v2.md` §3.3, §3.4, §6.

## Evidence this design starts from (attempt 8, stored run)

- `models/malaria_abm.py`: a Starsim Griffin-lite human module with one scalar vector.
  ITN and IRS act on one feeding cycle (Le Menach / Griffin), with one set of bionomics
  (Q0 0.92, φ_I 0.85, φ_B 0.80) applied everywhere, and IRS killing every indoor feeder.
- `lga_master_774.csv` carries per-LGA species proportions; no model code read them (D6).
- Benchmarks: B15 ITN×IRS interaction OR 0.94 against the trial's 2.43 (Protopopoff 2018,
  PBO×IRS at 9 months, band ×/÷2.5); B19 IRS $/DALY about 330 against 25.16 (band ×/÷5);
  B9 needed a "trial-consistent PBO variant" switched in per run.
- 5,850 lines in `models/*.py`, most of it analysis plumbing (emulator, UQ, optimizer,
  reserve, robustness), and 8 rounds of per-run trial tuning.

## What is built

1. `toolkit/engines/contract.py`: the `Engine` protocol (`describe`, `parameters`, `run`),
   typed `EngineCard`, `ParamSpec`, `UnitSpec`, `Package`, `Horizon`, `Outcomes`, a
   contract checker and a toy engine. No disease names.
2. `toolkit/engines/benchmark.py`: generic benchmark-pack runner. A pack is YAML: settings
   (unit + horizon), arms (setting + package), estimands (ratio, odds ratio, factorial
   interaction OR, cost per outcome, curve thresholds), target bands, and a `fit` section
   (sequential one-parameter grid fits against named benchmarks). Benchmarks not named in
   `fit` are held out. Results and fitted values are written once, to the engine's folder.
3. `toolkit/engines/malaria/`: our Starsim engine, promoted from attempt 8, with:
   - species-specific bionomics per unit (Q0, φ_I, φ_B, μ0 from the D6 prior with its
     citations; endophily as a declared, unverified assumption);
   - ITN effect split into deterrence/repellence, killing and blocking (feeding
     inhibition), for pyrethroid, PBO and dual-AI nets, with resistance (bioassay
     mortality) and decay;
   - IRS deterrence on house entry and killing of indoor-resting (endophilic) fed
     mosquitoes, with decay;
   - SMC and case management from attempt 8.
   ITN×IRS redundancy arises only from these mechanics (IRS deterrence diverts mosquitoes
   away from nets; net-killed mosquitoes never rest on sprayed walls; both act on the same
   indoor fraction, leaving the outdoor fraction). There is no interaction factor.
4. `toolkit/engines/malaria/benchmarks/pack.yaml`: trial settings for pyrethroid ITN vs
   none, PBO vs pyrethroid, dual-AI vs pyrethroid, IRS added to ITN, the 2×2 factorial for
   B15, SMC, the EIR–prevalence curve, and the cost-per-DALY bands (B17–B20). Four
   engine-level parameters are fitted once (ITN maximum killing to B7/B8, PBO restoration
   to B9, chlorfenapyr killing to B10/B11, IRS killing to B12/B12b). B13, B15, B16 and
   B17–B20 are held out: they test the mechanics, they do not tune them.
5. `spike/decision_spec.yaml`: hand-written from attempt 8's brief. Subset: the 13
   Global Fund states (309 LGAs) that hold about 99% of attempt 8's planned spend, so the
   headline compares directly with attempt 8's GF-state total.
6. `spike/analysis/` (harness code, no agent-typed numbers): a space-filling design over
   unit attributes and the fitted engine parameters, one batch of engine runs (all
   packages per design point with common random numbers), a Gaussian-process emulator
   with held-out accuracy, per-unit calibration of baseline transmission by emulator
   inversion, uncertainty from draws of the engine parameters within their benchmark
   ranges, `toolkit/alloc` optimisation, leave-one-archetype-out allocation stability,
   and one decision test (IRS-with-net increments rescaled so the trial-setting
   interaction equals the trial's 2.43).

## Simplifications declared up front

- Campaign timing classes, CF1 comparator, CM coverage, SMC and IRS eligibility and
  populations are taken from attempt 8's processed unit table (data mapping, not
  re-derived). Net use after a campaign is 0.55 for every unit, as in attempt 8.
- The resistance-to-efficacy mapping keeps attempt 8's functional form (killing ∝
  bioassay mortality^k); its maximum is fitted to the pack, not taken from Churcher 2016.
- No reserve, descoping or add-back rules (attempt 8's decision-layer features).
- Uncertainty covers engine parameters and calibration; not costs.

## Success criteria (from architecture-v2 §6)

B15 and B19 in band without per-run tuning; headline within attempt 8's interval or a
documented reason; a leave-one-out result; spend under $100 and wall time under 8 h.
