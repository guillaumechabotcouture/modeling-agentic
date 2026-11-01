"""v2d δ runtime spike (run on the dev VM, 2026-11-01): one ss.Module with the exponential-survival step, timed per sim and per batch of 1,024. Results: runtime_spike.json.

Usage: python spike.py [out.json]
"""
import json, os, platform, sys, time
import numpy as np
import starsim as ss


class GroupEpidemicSpike(ss.Module):
    """Group-level renewal with susceptible depletion, batch axis B x G."""

    def __init__(self, q, C, N, s0, seed, w, **kw):
        super().__init__(name="ge")
        self.q = np.atleast_1d(np.asarray(q, float))          # (B,)
        self.C = np.asarray(C, float)                          # (G, G)
        self.N = np.asarray(N, float)                          # (G,)
        self.s0 = np.atleast_2d(np.asarray(s0, float))         # (B, G)
        self.seed = np.atleast_2d(np.asarray(seed, float))     # (B, G)
        self.w = np.asarray(w, float)                          # (L,)

    def init_post(self):
        super().init_post()
        B, G, L = len(self.q), len(self.N), len(self.w)
        self.S = self.s0 * self.N - self.seed
        self.hist = np.zeros((L, B, G))
        self.hist[0] = self.seed
        self.cum = self.seed.copy()

    def init_results(self):
        super().init_results()
        self.define_results(ss.Result('inc_total', label='incidence (batch member 0, all groups)'))

    def step(self):
        force = np.tensordot(self.w, self.hist, axes=(0, 0)) / self.N          # (B, G)
        lam = self.q[:, None] * force @ self.C.T                               # (B, G)
        inc = self.S * -np.expm1(-lam)
        self.S = self.S - inc
        self.hist = np.roll(self.hist, 1, axis=0)
        self.hist[0] = inc
        self.cum += inc
        self._inc = inc

    def update_results(self):
        super().update_results()
        self.results['inc_total'][self.ti] = float(self._inc[0].sum())


def one(B, steps, G=5, L=10):
    rng = np.random.default_rng(0)
    C = rng.uniform(0.5, 2.0, (G, G))
    N = np.full(G, 1e5)
    w = np.exp(-np.arange(1, L + 1) / 3.0)
    w /= w.sum()
    q = rng.uniform(0.2, 0.6, B)
    s0 = rng.uniform(0.5, 1.0, (B, G))
    seed = np.zeros((B, G))
    seed[:, 0] = 10.0
    t0 = time.perf_counter()
    sim = ss.Sim(n_agents=1, modules=[GroupEpidemicSpike(q, C, N, s0, seed, w)], start=0, stop=steps - 1,
                 dt=ss.days(1), verbose=0, rand_seed=1)
    sim.init()
    t1 = time.perf_counter()
    sim.run()
    t2 = time.perf_counter()
    m = sim.custom.ge
    return t2 - t0, t1 - t0, t2 - t1, float(m.cum[0].sum() / N.sum())


def main():
    out = {"python": sys.version.split()[0], "starsim": ss.__version__, "numpy": np.__version__,
           "platform": platform.platform(), "cpu_count": os.cpu_count(),
           "threads": {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")},
           "cases": []}
    try:
        out["cpu_model"] = [l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")][0]
    except Exception:
        pass
    one(1, 50)  # warm-up (imports, caches)
    for B in (1, 1024):
        for steps in (50, 100, 200):
            reps = 30 if B == 1 else 10
            ts = [one(B, steps) for _ in range(reps)]
            tot = np.array([t[0] for t in ts])
            ini = np.array([t[1] for t in ts])
            run = np.array([t[2] for t in ts])
            rec = {"batch": B, "groups": 5, "steps": steps, "reps": reps,
                   "ms_per_sim_median": round(1e3 * float(np.median(tot)), 3),
                   "ms_init_median": round(1e3 * float(np.median(ini)), 3),
                   "ms_run_median": round(1e3 * float(np.median(run)), 3),
                   "ms_per_member_median": round(1e3 * float(np.median(tot)) / B, 4),
                   "attack_rate_member0": round(ts[0][3], 6)}
            out["cases"].append(rec)
            print(json.dumps(rec), flush=True)
    path = sys.argv[1] if len(sys.argv) > 1 else "spike.json"
    json.dump(out, open(path, "w"), indent=1)
    print("wrote", path)


if __name__ == "__main__":
    main()
