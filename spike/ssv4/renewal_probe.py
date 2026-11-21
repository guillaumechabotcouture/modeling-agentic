"""Probe: can a declared Starsim v4 disease reproduce the renewal engine's recursion?

    python3 spike/ssv4/renewal_probe.py

The renewal engine (toolkit/engines/renewal/engine.py, simulate()) runs, per step t,

    lambda_t = R0 c(t) sum_s w_s I_{t-s},   I_t = S_{t-1} (1 - exp(-lambda_t)),   S_t = S_{t-1} - I_t

The declared form tried here: states S, I1..IL, R; I_k holds the people infected k steps ago, each I_k
leaves after exactly one step, and infection is S >> I1 at rate (R0 / dt) sum_k w_k I_k (frequency
dependent). Compared with the engine under each v4 solver that is deterministic.
"""
import os
import sys

import numpy as np
import starsim as ss

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from toolkit.engines.renewal.engine import simulate  # noqa: E402

R0 = 1.8
W = np.array([0.1, 0.3, 0.3, 0.2, 0.1])   # generation interval on lags 1..5 weeks
N_STEPS = 300
SEED = 1e-4
N = 1e6


def engine_series():
    inc, Sp, S0, rt = simulate(np.array([R0]), np.ones((1, N_STEPS)), np.array([1.0]), np.array([SEED]), W)
    return inc[0], Sp[0]


def declared(solver, latent=False):
    L = len(W)
    names = ['S'] + (['E'] if latent else []) + [f'I{k + 1}' for k in range(L)] + ['R']
    st = ss.states(' '.join(names))
    S, R = st[0], st[-1]
    Is = st[2:-1] if latent else st[1:-1]
    force = sum(float(w) * I for w, I in zip(W, Is))
    first = st[1]
    trans = [S >> first @ ss.perweek(R0) * force]
    if latent:
        trans.append(first >> Is[0] @ ss.constant(ss.weeks(1)))
    for k in range(L - 1):
        trans.append(Is[k] >> Is[k + 1] @ ss.constant(ss.weeks(1)))
    trans.append(Is[-1] >> R @ ss.constant(ss.weeks(1)))
    dis = ss.Disease(*trans, init=dict(S=1 - SEED, I1=SEED), name='ren')
    sim = ss.Sim(dis, total_pop=N, unit='week', dt=ss.weeks(1), stop=ss.weeks(N_STEPS), solver=solver, verbose=0)
    sim.run()
    return sim


def main():
    inc, Sp = engine_series()
    print(f'engine: attack rate {inc.sum():.6f}, final S {Sp[-1]:.6f}, peak week {int(np.argmax(inc)) + 1}')
    solvers = {
        "ode (rk4, stages)": 'ode',
        "ode (exact dwells)": ss.solvers.ode(dwells='exact'),
        "ode (euler, exact, 1 substep)": ss.solvers.ode(integrator='euler', substeps=1, dwells='exact'),
    }
    for (label, solver), latent in [(kv, lat) for lat in (False, True) for kv in solvers.items()]:
        label = label + (' + one-step E' if latent else '')
        try:
            sim = declared(solver, latent)
            res = sim.results.ren
            s = np.asarray(res['S'].values if 'S' in res else res.S.values) / N
            print(f'{label}: final S {s[-1]:.6f} (engine {Sp[-1]:.6f}, diff {s[-1] - Sp[-1]:+.2e}); '
                  f'peak week {int(np.argmax(-np.diff(s))) + 1}; S series max abs diff {np.max(np.abs(s[1:len(Sp) + 1] - Sp[:len(s) - 1])):.2e}')
        except Exception as e:  # report every solver, even one that refuses the model
            print(f'{label}: {type(e).__name__}: {e}')


if __name__ == '__main__':
    main()
