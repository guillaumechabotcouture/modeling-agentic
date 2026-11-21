# v2 on Starsim v4: plan

Status: started 2026-11-20 on branch `v2-ssv4` (from `no-dollar-ceilings`, 82a15f71). Decisions for Cliff: [v2-ssv4-decisions.md](v2-ssv4-decisions.md). Gaps found in Starsim v4: [v2-ssv4-gaps.md](v2-ssv4-gaps.md).

## Goal

Every simulation the v2 pipeline runs is a Starsim v4 sim. Today v2 runs four engines behind one contract (`toolkit/engines/contract.py`):

| Engine | Today | Target |
|---|---|---|
| `malaria/` | Starsim 3.3.2 agent-based model (coded `ss.Disease`, four coded interventions) | The same model translated to Starsim v4 (for now; `starsim-malaria` may replace it later) |
| `compartmental/` | One Starsim 3 `ss.Module` holding numpy arrays with a batch axis | A declared Starsim v4 disease, stratified by age or birth cohort |
| `renewal/` | Plain numpy recursion | Unchanged: stays numpy (Cliff, 2026-11-20); the same model as a declared v4 disease is kept as a check (`benchmarks/starsim_v4.py`) |
| `empirical/` | Statistical, no simulation | Unchanged (out of scope) |

Out of scope: the v1 pipeline (`main.py --v1`), its agent-written models (LASER, scipy, Starsim 3) and its skills.

## Definition of done

1. `requirements.txt` pins Starsim v4 (a pushed `v4` commit until 4.0 is released), and nothing in `toolkit/` imports Starsim 3 names.
2. The compartmental and malaria engines each run on Starsim v4 and pass their own self-tests and `check_engine()`; the renewal engine stays numpy, with its v4 twin checked against it.
3. Each engine's benchmark pack has been rerun on v4, with every result in band or each miss explained in its `ENGINE_NOTES.md`.
4. `python3 scripts/self_tests.py --jobs 2`, `scripts/regression_eval.py` and `scripts/gate_fuzz.py` pass.
5. Every Starsim v4 gap met on the way is in [v2-ssv4-gaps.md](v2-ssv4-gaps.md), with a repro.
6. A change fragment `changes/v2-ssv4.md` and updated engine notes and cards.

## Checklist

- [x] Branch `v2-ssv4` and worktree
- [x] Inventory of simulation in v2 (above)
- [x] Renewal: probe whether a declared v4 disease reproduces the recursion (`spike/ssv4/renewal_probe.py`)
- [x] Renewal: engine on v4, oracles and pack
- [x] Renewal: reverted to numpy (Cliff, 2026-11-20: "just for things that make sense, not a goal unto itself"); the v4 version kept as a check, `toolkit/engines/renewal/benchmarks/starsim_v4.py` (agrees to 3e-15)
- [x] Compartmental: engine on v4 (age groups with a contact matrix, birth cohorts, dose blocks, campaigns, seasonality, the batch axis)
- [x] Compartmental: analytic pack on v4 (64 of 64 keep their status); coverage pack waits on decision 6
- [x] Malaria: translate `model.py` and `engine.py` to v4 (v0.19; same model within seed noise)
- [x] Malaria: self-tests (all pass except the three evidence stamps)
- [ ] Malaria: the benchmark pack on v4 (decision 8: about 110 CPU-hours)
- [x] Pin (`starsim==4.0.0rc6`, `sciris==3.7.0`); the preflight and `spec_compliance` are v1 only
- [x] Speed (Cliff, 2026-11-20: "the v4 version shouldn't be slower than the hand rolled one"): Starsim gets `integrator='map'` (compiled), arrays in `ss.timeseries`, and a cached name lookup; both engines run each sim whole. Compartmental and age self-tests now beat v3; the renewal engine, still about 6x slower per call on v4, went back to numpy (decisions item 2, gaps G13)
- [x] `integrator='map'` takes `substeps=` (each substep as the map: exponential Euler); default 1
- [ ] Full checks; change fragment; engine notes

## Progress

~85% done (the compartmental and malaria engines on v4, the compartmental one compiled; the renewal engine stays numpy; left: the malaria pack, the coverage-pack proof, and the full checks in a v4 env).

## Deferred

- Replacing the malaria engine with `starsim-malaria` (Cliff: "tbc").
