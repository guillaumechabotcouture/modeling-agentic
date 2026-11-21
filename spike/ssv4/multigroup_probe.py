"""Probe: the compartmental engine's multigroup recursion as a declared Starsim v4 disease.

    python3 spike/ssv4/multigroup_probe.py

The engine (toolkit/engines/compartmental/engine.py, GroupEpidemic) steps, for each batch member b and group i,

    I_bi,t = S_bi,t-1 [1 - exp(-lambda_bi,t)],   lambda_bi,t = q_b f_b(t) sum_j C_ij sum_s w_s I_bj,t-s / N_bj

Tried here: states S, I1..IL, R; a stratification by member (independent places) and group; mixing from
ss.contacts(group=C); the per-member q_b f_b(t) as a function of the sim; and the transmit() override for the
discrete map. Compared with a numpy loop of the recursion.
"""
import numpy as np
import starsim as ss

W = np.array([0.5, 0.3, 0.2])
C = np.array([[2.0, 0.5, 0.2], [0.6, 1.5, 0.4], [0.3, 0.5, 1.0]])
N = np.array([[3e5, 2e5, 1e5], [1e5, 1e5, 4e5]])           # (B, G) group sizes, different per member
S0 = np.array([[1.0, 0.9, 0.8], [0.7, 0.7, 0.7]])
SEED = np.array([[1e-4 * 6e5, 0, 0], [0, 6e-5 * 6e5, 0]])  # counts
Q = np.array([0.4, 0.55])
AMP, PHASE, PER = np.array([0.0, 0.3]), np.array([0.0, 40.0]), 365.0
T = 400
B, G = N.shape


def reference():
    L = len(W)
    S = S0 * N - SEED
    hist = np.zeros((L, B, G))
    hist[0] = SEED
    out = [S.copy()]
    for k in range(1, T + 1):
        f = 1 + AMP * np.cos(2 * np.pi * (k - PHASE) / PER)
        force = np.einsum('l,lbg->bg', W, hist) / N
        lam = (Q * f)[:, None] * np.einsum('ij,bj->bi', C, force)
        new = S * -np.expm1(-lam)
        S = S - new
        hist = np.roll(hist, 1, axis=0)
        hist[0] = new
        out.append(S.copy())
    return np.array(out)                                   # (T + 1, B, G)


class Chain(ss.Disease):
    def transmit(self, transition):
        return super().transmit(transition)


def declared():
    st = ss.states(' '.join(['S'] + [f'I{k + 1}' for k in range(len(W))] + ['R']))
    S, Is, R = st[0], st[1:-1], st[-1]
    force = sum(float(w) * I for w, I in zip(W, Is))
    # the sim's step k (from 0) is the engine's step k + 1
    beta = ss.par(lambda sim: ss.perday(np.repeat(Q * (1 + AMP * np.cos(2 * np.pi * (sim.ti + 1 - PHASE) / PER)), G)),
                  name='beta')
    trans = [S >> Is[0] @ beta * force] + [a >> b @ ss.constant(ss.days(1)) for a, b in zip(Is, list(Is[1:]) + [R])]
    tab = lambda x: ss.by(np.asarray(x, float), dims=['member', 'group'])  # noqa: E731
    frac = N / N.sum()
    dis = Chain(*trans, init=dict(S=tab(S0 - SEED / N), I1=tab(SEED / N), R=tab(1 - S0)), name='mg')
    dis.stratify(member=ss.levels({f'm{b}': frac[b].sum() for b in range(B)}, kind='space'),
                 group=ss.levels([f'g{i}' for i in range(G)], kind='plain', fractions=tab(N / N.sum(axis=1, keepdims=True))))
    return dis


def main():
    ref = reference()
    try:
        dis = declared()
        sim = ss.Sim(dis, networks=ss.contacts(group=C), total_pop=N.sum(), unit='day', dt=ss.days(1),
                     stop=ss.days(T), verbose=0, solver=ss.solvers.ode(dwells='exact'))
        sim.run()
        print(sim.describe() if False else '', end='')
        res = sim.results.mg.S
        print('axes', list(res.axes()))
        for b in range(B):
            for i in range(G):
                v = np.asarray(res.select(member=f'm{b}', group=f'g{i}').values, float).ravel()
                print(f'member {b} group {i}: v4 S[0] {v[0]:.6g} ref {ref[0, b, i]:.6g}; final v4 {v[-1]:.8g} '
                      f'ref {ref[-1, b, i]:.8g}; max rel diff {np.max(np.abs(v - ref[:, b, i]) / N[b, i]):.2e}')
    except Exception as e:
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
