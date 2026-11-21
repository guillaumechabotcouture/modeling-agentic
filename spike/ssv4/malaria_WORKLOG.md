# Worklog: malaria engine, Starsim 3.3.2 to Starsim 4 (4.0.0rc5)

Worktree at 82a15f71 (detached). Starsim 4 from `/home/cliffk/idm/ssv4` (branch v4, 0089ac0f), Python from the base conda env. Starsim 3.3.2 baselines from the `modag` env on the unchanged code.

## What changed, file by file

The migration script (`python -m starsim.converters.migrate toolkit/engines/malaria --check`) found 3 changes in 2 files and nothing to do by hand; I made them by hand and then the rest below.

- `toolkit/engines/malaria/model.py`: `ss.lognorm_ex(mean=1.0, std=sd)` is `ss.lognormal(mean=1.0, std=sd)` (the `zeta` default; `_households()` overwrites it for every agent anyway). Docstring notes v0.19. Nothing else: the model draws almost all its random numbers from its own NumPy generator (`Malaria.rs`), and its Starsim surface (`ss.Disease` with `define_states`, `ss.FloatArr`/`ss.BoolState`, `ss.random()` defaults, `self.ti`, `self.sim.t.yearvec`, `sim.people.auids`/`age`, `ss.Intervention.step`) runs unchanged on v4.
- `toolkit/engines/malaria/engine.py`:
  - `_demog()`: `ss.Births(birth_rate=ss.peryear(37), rate_per=1000)` and `ss.Deaths(death_rate=ss.peryear(11), rate_per=1000)`. Starsim 3.3.2 read these as per 1,000 (`rate_units=1e-3`), which the engine's comments (37 and 11 per 1,000) and POP_GROWTH 0.0269 confirm; without `rate_per` v4 would read 37 births per person per year.
  - `MIN_START_YEAR` 1900 to 1800, with a new reason and message. The 1900 floor existed because Starsim 3.3.2's Births and Deaths did nothing before 1900; v4's Births and Deaths work at any start (`spike/ssv4/pre1900_check.py`: 500 agents from 1850 grow to 558 in 5 years, v3 stays at 500). But v4 reads a bare start before 1800 as days from 0 (gap 1), so the guard stays, at 1800. Self-test V2c now asks for a 250-year burn-in (start 1765) and "1800" in the message.
  - `_run_members()`: removed the emptying of each finished member (`dz.daily, dz.mos, dz.wdaily = {}, [], {}` and `s.shrink(...)` in a try/except). Starsim 3.3.2 kept finished sims alive even after `gc.collect()`; v4 frees them at `gc.collect()` (`spike/ssv4/release_check.py`), which the loop still calls. Peak memory: see the timing below.
  - Kept: the pickle snapshot fork. On v4 it is still the fastest copy (`spike/ssv4/fork_copy_check.py`, 4,000 agents: dumps 0.04 s, loads 0.03 s, `copy.deepcopy` 0.22 s; v3: 0.05, 0.07, 0.48 s), the snapshot is smaller (1.6 MB against 4.0 MB), and restored copies run on identically. v4's `sim.new()` rebuilds from the inputs, not the state, so it is no replacement.
  - `VERSION` 0.18 to 0.19; a v0.19 paragraph in the module docstring.
- `toolkit/engines/malaria/benchmarks/equilibration.py`: one comment (the burn-in floor is 1800). START stays 2050.
- `toolkit/engines/malaria/ENGINE_NOTES.md`: a v0.19 section at the top.
- `vector.py`, `selftest_late.py`, the other `benchmarks/*.py`: no change needed (no Starsim calls; `restamp_proof.py` only records `starsim.__version__`).
- New, outside the engine: `spike/ssv4/malaria_v3_v4.py` (the comparison), `spike/ssv4/timing.py` (one engine member's cost), and the repro scripts named below; logs in `spike/ssv4/logs/`. `spike/ssv4/_v3tree/` is the unchanged `toolkit/` at 82a15f71, extracted by the comparison script (`git archive`), safe to delete.
- Not changed: `requirements.txt` still pins `starsim==3.3.2` for this engine and `toolkit/engines/compartmental/`; the coordinator owns that pin.

Checked and unchanged on v4: the daily time vector (`ss.Timeline(start, stop, dt=ss.days(1)).yearvec`) is identical to v3's to the bit (`spike/ssv4/yearvec_check.py`), so event steps, windows and the fork step are the same; the loop order (demographics, interventions, then the disease's `step()`, deaths, aging) is the same; initial ages from `age_data` follow the histogram in both (`spike/ssv4/age_dist_check.py`).

## Self-tests (v4, run from the worktree root)

- `engine.py --self-test`: 72 pass, 2 fail: V7 and V8, "evidence: computed for engine '0.18', not '0.19'" (stamps of `guess_sensitivity.yaml` and `equilibration.yaml`). Before the version bump all 74 passed on v4. They pass again once `validate_once.sh` recomputes the two files on v0.19.
- `selftest_late.py --self-test`: 272 pass.
- `vector.py`: 40 pass. `benchmarks/net_checks.py` 1, `guess_sensitivity.py` 1, `render_notes.py` 2, `restamp_proof.py` 18, `net_effect_fit.py` 76, `equilibration.py` 6: all pass.
- `benchmarks/pack_trials.py`: 24 pass, 1 fail: PT6, "results.yaml was computed for another engine identity or code: re-run the pack". The identity hashes the engine's code, which changed. Passes on the unchanged code under v3. Should stay as it is until the pack is rerun.
- `model.py` has no self-test. The v3 baseline (unchanged code, modag env) passed all 74 engine cases in 12 min (in parallel with other jobs); v4 took 5 min 47 s.

## Comparison (`spike/ssv4/malaria_v3_v4.py`)

Three units (m0 3.5, 9, 28), five packages, 2,000 agents, burn-in 3 years, 2019-2026, window 2022-2025, seeds 1-8 per version; then m0 28 again with seeds 9-24. Full tables: `spike/ssv4/logs/v3_v4_report.txt` and `v3_v4_m28_s9-24.txt` (raw rows in the `.json` beside them).

- Every outcome was within seed noise (|z| < 2.5, z the v4 - v3 difference over its standard error) except severe cases, deaths and DALYs with no package at m0 28 (z -3.0; v4/v3 0.95). The same three were 3-5% lower at m0 3.5 and 9 (z -1.4, -2.0).
- Cause: the units share seeds, so within a version each seed's initial population is the same draw in all three units: the three z values are one draw, not three. Per age group the severe rates agreed (under 5: v4/v3 0.98-0.99), while the all-age rate, which weights the groups by their person-time, was 3-5% lower: v4's eight populations held fewer under-5s (inferred from the two rates; the first run did not record the age shares, so I added `share_u5` to the script). Initial ages are right in both versions (`spike/ssv4/age_dist_check.py`: 200,000 agents give the histogram's shares to 0.001). With seeds 9-24 at m0 28: severe 0.996 times v3 (z -0.2), u5 share equal (0.1694 against 0.1695), and every outcome |z| < 1.8.
- No systematic timing shift: the model does all its work in its own `step()` and records its own daily arrays, so v4's change of where stocks are recorded does not touch it; the loop order is the same.
- The case-management contrast (a 4-8% reduction in cases) is the noisiest: v4/v3 ratios 0.6-1.24 in both directions across units and seed sets, all |z| < 1.8.

## Timing (`spike/ssv4/timing.py`)

One engine member, `benchmarks/runtime.py`'s v05_shared task (one `run_batch` of 4 packages, 4,000 agents, 50-year burn-in, 2016-2027, m0 9, seed 1, one core each, run side by side): v3 269.9 CPU s, peak RSS 1,804 MB; v4 172.8 CPU s, 706 MB. v4/v3 0.64 in time, 0.39 in memory. The comparison's smaller calls agree: 0.71-0.73 (2,000 agents, 10 years).

Rough estimate of rerunning `validate_once.sh` on v4: about 2,300 burn-in groups (guess sensitivity 3 levels x 3 guesses x 96 seeds = 864; equilibration 96 with 24 at a doubled burn-in; calibration grids about 400 and the joint fit about 900 groups for its 1,748 arm runs; pack run about 150 for 298 arm runs; B15 scan about 110; runtime 6 points), each about 170 s on v4 (4,000-5,000 agents), so about 110 CPU-hours (v3: about 170). With `--workers 4`: about 27 hours of wall time; with 10 workers: about 11 hours. Memory is no longer the bound: 0.7 GB per process (v3 needed up to 1.8, and the script warns of 5 GB per batch worker), so 10 workers need about 7 GB. The group count is my reading of the scripts and pack.yaml, not a measurement; the per-group time is measured.

## v4 gaps

Each has a minimal repro under `spike/ssv4/` (run with `python3` for v4, `/software/conda/envs/modag/bin/python` for v3).

1. **A bare start before 1800 silently becomes a time in days from 0.** `ss.Timeline(start=1750.0, stop=1752.0, dt=ss.days(1))` (and `ss.Sim` with the same) gives 3 steps at "years" 4.79-4.80, with no error or note: 1750 and 1752 are read as days, while the same call from 1800.0 gives 731 daily steps from 1800 to 1802. The migration guide documents the 1800 rule, but a start of 1750.0 with `stop=1752.0` and `dt=ss.days(1)` is almost certainly a year, and a 3-step run is a quiet failure. A warning when a bare start looks like a year (or when start and stop are less than one `dt` apart in the chosen unit) would help. Repro: `spike/ssv4/pre1800_check.py`. The engine fails closed before this happens (MIN_START_YEAR, below).
2. **An unhelpful warning for a tuple parameter.** A module that defines `define_pars(months=())` and is given `months=(7, 8, 9)` warns "No known mechanism for handling <class 'tuple'> -> <class 'tuple'>; using default" (the value is still set correctly). Starsim 3.3.2 does the same, so this is not new in v4, but it fires on every malaria sim (the engine's `smc_months`), 57 times in one engine self-test, and the text sounds as if the value was ignored. Repro: `spike/ssv4/tuple_par_check.py`.
3. **Finished sims still hold reference cycles** (not a regression; better than v3). A run sim is not freed when its last reference goes; `gc.collect()` frees it in v4, where in Starsim 3.3.2 it was not freed even then (the reason the engine shrank each finished member). Repro: `spike/ssv4/release_check.py`. Not a problem for the engine, which calls `gc.collect()`, but a loop of many sims without it grows until Python's collector runs.

## Not sure about

- Whether to remove `MIN_START_YEAR` entirely: the v3 reason is gone, but the v4 calendar rule needs a floor, so I kept it at 1800. No evidence run starts before 1900 anyway (equilibration starts in 2050 - 100).
- The engine identity (`contract.engine_identity`) hashes the engine's code, not the Starsim version. Here the code changed, so old evidence is refused anyway, but a Starsim upgrade with no code change would not invalidate results. Worth adding the Starsim (and NumPy) version to the identity; that is shared contract code, so I left it.
- The tuple-parameter warning (gap 2) could be silenced in the engine by passing `smc_months` as a list; I left it, since it is a Starsim message and the value is right.

