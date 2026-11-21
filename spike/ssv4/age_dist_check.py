"""Initial ages from the engine's age_data, 200,000 agents, v3 vs v4, against the exact shares of the histogram."""
import numpy as np, pandas as pd, starsim as ss
ages = np.arange(0, 86); w = np.exp(-0.036 * ages)
exact = lambda lo, hi: w[(ages >= lo) & (ages < hi)].sum() / w.sum()
ppl = ss.People(n_agents=200_000, age_data=pd.DataFrame({'age': ages, 'value': w}))
sim = ss.Sim(people=ppl, start=2016.0, stop=2017.0, dt=ss.days(1), rand_seed=1, verbose=0); sim.init()
a = np.asarray(sim.people.age[sim.people.auids])
print(ss.__version__, ' '.join(f"[{lo},{hi}) {np.mean((a>=lo)&(a<hi)):.4f} (exact {exact(lo,hi):.4f})" for lo, hi in ((0,1),(0,5),(5,15),(15,200))), f"max {a.max():.2f}")
