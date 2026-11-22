# v2 on Starsim v4: decisions for Cliff

Plan: [v2-ssv4.md](v2-ssv4.md). Each item: the question, the options, what I did meanwhile, and a `[CK: ]` slot.

## 1. The discrete map: now `ss.solvers.ode(integrator='map')` (done, in Starsim)

Both engines took each step's infections once, at its start (`S (1 - exp(-lambda))`), by overriding `transmit()`, which also turned off the compiled path. Starsim now has the map as an ODE integrator, compiled (Numba, Rust, JAX); the compartmental engine and the renewal engine's v4 check use it with no override, and their outputs are unchanged to rounding. It is the expected value of the chain binomial in whole steps, and the form of any model written in discrete time, so it is not specific to these engines. Its default is one substep; with `substeps=n` each substep is taken as the map (the exponential Euler method), which approaches the ODE as n grows (Cliff, 2026-11-20). It is new in Starsim 4.0.0rc6 (item 5).

[CK: ]

## 2. Speed against v3 and numpy

Measured 2026-11-20 on this machine (4 cores, interleaved, min of runs), Starsim commit 770fe50e (branch `v4-nov20`):

| | v3 / numpy | v4 before | v4 now |
|---|---|---|---|
| Compartmental `engine.py --self-test` | 12 s | 10 s | 2.6 s |
| Compartmental `selftest_age.py --self-test` | 36 s | 131 s | 13.5 s |
| Renewal `engine.py --self-test` | about 1 s | 17 s | 1.7 s |
| Renewal, one run (300 steps) | 1.2 ms | 94 ms | 7.5 ms |
| Renewal, a batch of 1,000 (300 steps) | 6.5 ms | 308 ms | 160 ms |

The renewal engine was still slower per call on v4: a sim costs about 6 ms to build and initialize whatever its size, and 1,000 places cost more than the numpy loop's vectors. **Resolved:** the renewal engine stays numpy (Cliff, 2026-11-20: v4 "just for things that make sense, not a goal unto itself"), so its speed is v0.1's; the v4 version is kept as a check (`toolkit/engines/renewal/benchmarks/starsim_v4.py`).

[CK: ]

## 3. `run_batch()` equals `run()` to rounding, not exactly

v4 splits the population across a batch's members, so a member's numbers differ from a single run by about 1e-16. The compartmental engine's K3 already used 1e-12 (the renewal engine stays numpy, so its K2 is still exact).

[CK: ]

## 4. A conda env for v4 (please make one)

The repo pins its packages in `requirements.txt`; the `modag` env has them, with Starsim 3.3.2 and Sciris 3.2.9. Starsim v4 needs Sciris >= 3.7.0, so `modag` can't import v4. I ran the port in the base env (Starsim v4 from `~/idm/ssv4`), after installing four pinned packages it lacked (PuLP, lmfit, polars, xgboost; nothing upgraded or downgraded). That covers the engines, but not the agent code: base has `claude-agent-sdk` 0.1.50 (pinned: 0.2.169) and `anthropic` 0.86 (pinned: 2.3.0), so `toolkit/spec/evidence_dossier.py` and `scripts/world_model_trials.py` fail there on `AgentDefinition(maxTurns=...)`, as they would without the port. I didn't upgrade base.

Suggested: `conda create -n modag4 --clone modag`, then in it `pip install -r requirements.txt`. I'll then run the full checks there.

[CK: ]

## 5. Pin: `starsim==4.0.0rc6` and `sciris==3.7.0` (done)

The engines need the Starsim changes made for this port (the map integrator and the speed fixes), which are in 4.0.0rc6; `requirements-agent.txt` moves Sciris to 3.7.0. The engines were tested against Starsim at 770fe50e (the malaria translation at 0089ac0f, 4.0.0rc5). The v1 pipeline's LASER pins are unchanged (v1 is out of scope).

[CK: ]

## 6. The coverage pack (compartmental) can't be restamped by a byte-identity proof

The compartmental engine's coverage pack (`coverage_results.yaml`: SB1-SB5, 200 replicates per case, run on dev VMs) was carried from v0.1 to v0.2 by `compat_proof.py`, which shows byte-identical outputs between versions. On v4 the outputs agree with v0.2 only to rounding (about 1e-16), so that proof fails and the self-test `CP6` fails. The malaria engine is different: it is stochastic, and v4's random numbers differ from v3's, so its pack and evidence need a rerun whatever the proof (item 8, once the translation is in).

- **A:** rerun the coverage pack on a VM (cost: as when it was first run).
- **B (recommended):** extend the proof to compare within a tolerance (e.g. 1e-12 relative), running its old side in the v3 env (`modag`) and its new side in the v4 env, then restamp. Cheap, and the claim it makes (same outputs to rounding) is what the analytic pack also shows.

[CK: ]

## 7. `runtime_spike.py` is Starsim 3 code

`toolkit/engines/compartmental/benchmarks/runtime_spike.py` is the one-off timing script from the v2d spike (2026-11-01), exempt from self-tests, written against Starsim 3 (`update_results()`). It no longer runs. Delete it, or leave it as a historical record?

[CK: ]

## 8. Rerun the malaria pack on v0.19?

The malaria engine (v0.19) runs on v4 as the same model: on 3 units, 5 packages and 8 seeds, every outcome and package contrast agrees with v3 within seed noise (`spike/ssv4/logs/v3_v4_report.txt`; one flag at |z| = 3 went away with 16 more seeds). v4's random numbers differ, so the evidence files are stamped for v0.18 and three self-tests fail on that (engine V7 and V8, `pack_trials` PT6), as they should until a rerun. One engine member costs 0.64 times the CPU and 0.39 times the memory of v3. Rerunning `validate_once.sh` is about 2,300 burn-in groups, roughly 110 CPU-hours: about 11 hours on 10 workers (0.7 GB each), or a Coiled run.

[CK: ]

## 9. Should an engine's identity include its Starsim version?

`contract.engine_identity` hashes the engine's code only, so a Starsim upgrade with no code change would leave old results stamped as valid. Here every engine's code changed, so every stamp was invalidated anyway. Adding the Starsim and NumPy versions to the identity would catch the next upgrade; it is shared contract code, so I left it.

[CK: ]

## 10. The malaria engine's start floor

Starsim 3's births and deaths did nothing before 1900, so the engine refused a burn-in starting before then. On v4 they work, but a bare start before 1800 is read as days, not a year, so the floor is now 1800. Keep it (recommended until G8 is fixed), or remove it?

[CK: ]
