# Starsim v4 gaps found porting v2's engines

Each entry: what the engine needs, what v4 does, a repro, and how the port works around it. Starsim `v4` at 0089ac0f (4.0.0rc5); G2, G11, G12 and most of G13 are fixed in Starsim 4.0.0rc6, which `requirements.txt` pins.

## G1. Infectiousness by time since infection (renewal engine)

**Needs.** A generation interval given as weights `w_1..w_L` on whole steps: a person infected at step t adds `R0 w_s` to the force of infection at step t + s (the renewal equation of EpiEstim, EpiNow2 and epidemia).

**v4.** `ss.solvers.ode(dwells='exact')` is the renewal equation, but its generation interval comes from the latent and infectious periods. Infectiousness can't vary with time in a state: a rate that reads `I.time_in` or `I.progress` is refused (`"infection" reads I.time_in, which has a value only for people in I, but moves people from S`). The only form is a chain of L states, each with a one-step `ss.constant` dwell, and the force of infection `sum_k w_k I_k` written out by hand.

**Repro.** `spike/ssv4/renewal_probe.py` (the chain), and:

```python
S, I, R = ss.states('S I R')
inf = S >> I @ ss.perweek(1.8) * (I * (I.time_in < ss.weeks(1)))
ss.Sim(ss.Disease(inf, I >> R @ ss.constant(ss.weeks(5)), init=dict(S=0.9999, I=1e-4)), total_pop=1e6, unit='week', stop=ss.weeks(300), solver=ss.solvers.ode(dwells='exact')).run()
```

**Would fix it.** An infectiousness profile on a state, e.g. `ss.State(rel_trans=<function of time in state>)`, or a generation-interval form of infection.

## G2. No deterministic discrete-time map (renewal engine)

**Needs.** The engine's recursion: in each step, `S_t = S_{t-1} exp(-lambda_t)`, with the step's infections entering the infectious classes at the end of the step.

**v4.** The ODE solvers integrate in continuous time, so people infected during a step are infectious within it. With the chain above, `rk4` gives the same final size as the engine (to 5e-6, R0 1.8), but the epidemic peaks at week 31 instead of 39; a one-step latent state moves it to week 46. `integrator='euler', substeps=1` applies rates linearly (`S lambda`, not `S (1 - exp(-lambda))`), so its final size is off by 0.018. The chain binomial is the discrete map, but only stochastically.

The discrete map is there, but hidden: a declared disease that overrides `transmit()`, even as `return super().transmit(transition)`, has its infections taken once a step, at its start, as `S (1 - exp(-rate dt))` (`Compartmental.transmitted()`). With that override the chain reproduces the engine to 3e-16. The override also turns off the compiled path: one run takes 77 ms instead of 0.8 ms, and a batch of 1,000 members 0.33 s instead of 0.10 s.

**Repro.** `python3 spike/ssv4/renewal_probe.py` (no override), and `toolkit/engines/renewal/engine.py` `_Renewal` (the override).

**Fixed in Starsim** (770fe50e): `ss.solvers.ode(integrator='map')`, the expected value of `ss.solvers.binomial(substeps=1)`: each step taken whole, with `1 - exp(-rate dt)` of each compartment leaving it, shared between its exits by rate. It runs in the compiled kernels (Numba, Rust and JAX). The compartmental engine and the renewal engine's v4 check use it, with no `transmit()` override. With `substeps=n`, each substep is taken as the map (exponential Euler).

## G3. A stratification with one level is dropped silently

`dis.stratify(member=ss.levels(['m0'], kind='space'))` leaves the results unstratified, so `res.S.by('member')` raises `The result "S" has no dimension "member"`. Code that builds a batch of B members has to special-case B = 1. A one-level dimension that stays a dimension would be simpler.

## G4. After a run, the compartments are one step past the last result

**Needs.** To continue a run in a new sim (the compartmental engine runs a long epidemic in segments, so no sim holds its whole length), the state at the end of the last step.

**v4.** A sim with `stop=ss.days(5)` records 6 time points (the start, and after each of 5 steps), but it also takes a step at its last time point: interventions run there and the compartments move again. So after `sim.run()`, `dis.S.n` and `dis.y` are one step past `sim.results.<dis>.S[-1]`, and an `until=` callback is not called for that step. Reading the live state after a run gives the wrong starting point (the engine skipped a step at every segment before this was found).

**Repro.**

```python
S, I, R = ss.states('S I R')
sim = ss.Sim(ss.Disease(S >> I @ ss.perday(0.5)*I, I >> R @ ss.days(3), init=dict(I=0.01), name='x'), total_pop=1.0, unit='day', dt=ss.days(1), stop=ss.days(5), verbose=0).run()
sim.results.x.S.values[-1], sim.diseases.x.S.n   # 0.953184 and 0.942214
```

**Workaround.** The engine takes the state inside its `until=` callback after the last step it counts.

**Would fix it.** No step at the last time point (or one whose effects are recorded), so that the live state after a run is the last result.

## G5. `init=` puts people in a whole-step dwell's slots inconsistently

**Needs.** A state that people leave after exactly one step (`ss.constant(ss.days(1))`, `dwells='exact'`), started with people in it.

**v4.** Such a state has two rows in `dis.y` (its slots). During a run, the people in it are always in the last slot, and leave at the next step. `init=` put the starting people of `I1` in its last slot, but those of `I2`, `I3` and `I4` in their first, so they stayed two steps. This is related to the deferred item on seeds in a state with a minimum stay (`lingua_franca/docs/admin/deferred.md`).

**Repro.** People who start in `I2` should reach `R` after one step, and those in `I1` after two; instead both arrive after two:

```python
S, I1, I2, R = ss.states('S I1 I2 R')
d = ss.Disease(S >> I1 @ ss.perday(0)*I1, I1 >> I2 @ ss.constant(ss.days(1)), I2 >> R @ ss.constant(ss.days(1)), init=dict(S=0.8, I1=0.1, I2=0.1), name='x')
sim = ss.Sim(d, total_pop=1.0, unit='day', dt=ss.days(1), stop=ss.days(3), verbose=0, solver=ss.solvers.ode(dwells='exact')).init()
sim.diseases.x.y[:, 0]                 # [0.8, 0, 0.1, 0.1, 0, 0]: I1 in its last slot (row 2), I2 in its first (row 3)
sim.run().results.x.R.values           # [0, 0, 0.2, 0.2], not [0, 0.1, 0.2, 0.2]
```

**Workaround.** After `sim.init()`, the engine moves each `I_k`'s starting people to its last slot (`dis.y[rows[-1]]`).

**Would fix it.** `init=` placing people in a state with a timed exit the same way a flow does.

## G6. `ss.Sim` copies the modules it is given

Not a gap so much as a surprise: `ss.Sim(..., interventions=Steps(run))` deep-copies the intervention, so an intervention that writes to an object it was given (here the engine's run state) writes to a copy. `copy_inputs=False` fixes it. The engine's v3 code read the module back from the sim for the same reason.

## G7. A starting share just above 1 is reported as "1"

`init=` refuses a share above 1, which is right, but the message formats it with `:n`, so a share of 1 + 2e-16 (from rounding, in a state carried between segments) reads `init= of "R" in "epi" is 1, but init= is a fraction of people (1.0 is everyone)`. Printing the value in full (or allowing 1 + a few ulps) would make the cause obvious. Repro: `ss.Disease(S >> I @ ss.perday(0.1)*I, I >> R @ ss.days(3), init=dict(S=0.0, R=1.0 + 2e-16), name='x')` in any sim with `total_pop=1.0`. The engine clips its shares to [0, 1].

## G8. A bare start before 1800 becomes days from 0, silently (malaria engine)

`ss.Timeline(start=1750.0, stop=1752.0, dt=ss.days(1))` gives 3 steps at "years" 4.79–4.80, with no warning: v4 reads a bare number below 1800 as days, not a year. The malaria engine's long burn-ins can start that early, so it keeps a floor (`MIN_START_YEAR` 1800, failing closed). A warning, or reading a float with a fractional calendar context as a year, would help. Repro: `spike/ssv4/pre1800_check.py`.

## G9. A misleading warning for tuple parameters

Giving a tuple to a parameter whose default is a tuple warns "No known mechanism for handling tuple → tuple; using default", although the value is set correctly. Starsim 3 did the same; it fires 57 times per malaria self-test (`smc_months`). Repro: `spike/ssv4/tuple_par_check.py`.

## G10. Finished sims hold reference cycles

Dropped sims are freed only at `gc.collect()` (Starsim 3.3.2 never freed them, so v4 is better). The malaria engine's emptying of finished sims is removed; it calls `gc.collect()`. Repro: `spike/ssv4/release_check.py`.

## G11. A value by group and time was slow to build (renewal and compartmental engines)

**Needs.** `beta` per batch member and step: an `ss.timeseries` per member, `ss.by(member=[ss.timeseries(steps, b) for b in beta])`.

**v4.** For 1,000 members over 300 steps, building, initializing and running the sim took about 13 s (300 steps each): every time and value was a Python object, walked one at a time by the distribution search, deep copies, the dimension checks and the parameter fingerprint, and `ss.perday(...)` around the series made each value a rate object.

**Fixed in Starsim.** A time series of plain numbers keeps its times and values as arrays (read, copied and searched as one object), and with no calendar converts its times at once. The same sim takes 0.19 s. Its values are bare numbers in the sim's unit, so the engines leave out `ss.perday()`.

## G12. Bare times with no calendar warned as ambiguous, one warning per time

A time series over steps 0 to 10,958 in a sim with no calendar (start 0, in days) warned `time=1017 is ambiguous ...` for each time from 1001 to 1799, although with no calendar a bare time is a time from 0 (`ss.timeseries` docstring). **Fixed in Starsim** with G11 (times with no calendar are converted at once, without the check).

## G13. The fixed cost of a small sim

A declared ODE sim of 7 states over 300 steps took about 4 ms to build, 8.5 ms to initialize and 1.5 ms to run; the renewal engine's numpy loop took about 1 ms in all. Fixed in Starsim (770fe50e):
- Reading the name a module is assigned to disassembled the calling function each time; it is cached per function.
- The distributions were found by searching the whole sim. Now each module is searched where the sim keeps it, without its engine, graph, states and transitions; the engine, the graph and the people list their own (`own_dists()`), so a declared model's are known rather than searched for. Every sim of Starsim's test suite finds the same distributions as before (checked against the old search), with new seeds (their paths changed), so random numbers changed and the regression baseline was regenerated.
- Two deep copies (the inputs at `ss.Sim()`, and the whole sim at `init()`, for `sim.new()`), about 1 ms each, are what `ss.Sim()` promises; the engines pass `copy_inputs=False`, which skips both.

A renewal call on v4 now takes about 7.5 ms (numpy: 1.2 ms), so the renewal engine stays numpy, with its v4 version as a check. Agent sims initialize 10–27% faster than at Starsim's HEAD (Starsim's performance guard, A/B). What is left is spread out: building the graph, the capability checks, and setting up the people and results.

The malaria translation's full log, with what was unsure: `spike/ssv4/malaria_WORKLOG.md`.
