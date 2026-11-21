#!/usr/bin/env python3
"""The renewal engine's recursion as a declared Starsim v4 disease: a check that v4 reproduces it.

    python3 toolkit/engines/renewal/benchmarks/starsim_v4.py --self-test

The engine runs its recursion in numpy (`engine.simulate()`): a Starsim sim costs about 6 ms to build and run,
against about 1 ms for the loop, and the engine is called many times (docs/design/v2-ssv4.md). This file keeps the
same model as a declared v4 disease and checks that the two agree to rounding, so a change to either shows up.

States `S`, `I1..IL` and `R`: `I_k` holds the people infected `k` steps ago and leaves after exactly one step, and
infection is `S >> I1` at rate `R0 c(t) sum_k w_k I_k`. The ODE solver's map integrator takes each step's infections
once, at its start, as `S (1 - exp(-lambda))`, which is the engine's recursion. A batch is one sim stratified into
independent places (`member`, kind='space'), each with its own R0, profile, starting state and seed.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import starsim as ss

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def simulate(r0, cmat, s0, seed, w):
    """As `engine.simulate()`: r0 (B,), cmat (B, n), s0 (B,), seed (B,), w (L,). Returns (incidence (B, n), S after
    each step (B, n), S before the first step (B,), R_t (B, n)). Time is in steps (the sim's unit is a day)."""
    r0, cmat, s0, seed = (np.asarray(x, float) for x in (r0, cmat, s0, seed))
    B, n = cmat.shape
    states = ss.states(" ".join(["S"] + [f"I{k + 1}" for k in range(len(w))] + ["R"]))
    S, Is, R = states[0], states[1:-1], states[-1]
    force = sum(float(wk) * I for wk, I in zip(w, Is))
    beta = r0[:, None] * cmat
    rate = ss.by(member=[ss.timeseries(np.arange(n), b) for b in beta])
    one_step = ss.constant(ss.days(1))
    trans = [S >> Is[0] @ rate * force] + [a >> b @ one_step for a, b in zip(Is, list(Is[1:]) + [R])]
    by = lambda x: ss.by(member=[float(v) for v in x])  # noqa: E731
    dis = ss.Disease(*trans, init=dict(S=by(s0 - seed), I1=by(seed), R=by(1 - s0)), name="renewal")
    dis.stratify(member=ss.levels([f"m{i}" for i in range(B)], kind="space"))
    sim = ss.Sim(dis, total_pop=float(B), unit="day", dt=ss.days(1), stop=ss.days(n), verbose=0,  # one person per member
                 solver=ss.solvers.ode(integrator="map", dwells="exact"), copy_inputs=False)
    sim.run()
    res = sim.results.renewal
    # (B, time); a stratification with one level leaves the results unstratified
    per = lambda r: np.asarray(r.by("member").values if B > 1 else r.values, float).reshape(len(r), B).T  # noqa: E731
    Sall = per(res.S)                               # before step 0, then after each step
    return per(res.infection)[:, :n], Sall[:, 1:n + 1], Sall[:, 0].copy(), beta * Sall[:, :n]


def _self_test() -> int:
    from toolkit.engines.renewal.engine import simulate as simulate_numpy
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    rng = np.random.default_rng(1)
    w = np.array([0.1, 0.3, 0.35, 0.2, 0.05])
    n = 120
    cases = {
        "one place, constant": (np.array([1.8]), np.ones((1, n)), np.array([1.0]), np.array([1e-4])),
        "three places, changing c(t)": (np.array([1.3, 2.5, 4.0]), rng.uniform(0.3, 1.2, (3, n)),
                                        np.array([1.0, 0.6, 0.9]), np.array([1e-6, 1e-3, 1e-5])),
    }
    for name, (r0, cmat, s0, seed) in cases.items():
        a, b = simulate_numpy(r0, cmat, s0, seed, w), simulate(r0, cmat, s0, seed, w)
        err = max(float(np.max(np.abs(x - y))) for x, y in zip(a, b))
        ok(err < 1e-12, f"SV1 {name}: Starsim v4 equals the numpy recursion (max difference {err:.1e})")
    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
