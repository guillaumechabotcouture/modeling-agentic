"""Age structure from the engine's People (age_data: ages 0-85, weights exp(-0.036 age)) and Births/Deaths, v3 vs v4:
the share under 5, 5-14 and 15+ at the start and after 3 and 10 years, mean of 8 seeds, 2,000 agents."""
import sys
import numpy as np, pandas as pd, starsim as ss
v4 = int(ss.__version__[0]) >= 4
kw = dict(rate_per=1000) if v4 else {}
ages = np.arange(0, 86)
out = {0: [], 3: [], 10: []}
for seed in range(1, 9):
    ppl = ss.People(n_agents=2000, age_data=pd.DataFrame({'age': ages, 'value': np.exp(-0.036 * ages)}))
    dem = [ss.Births(birth_rate=ss.peryear(37), **kw), ss.Deaths(death_rate=ss.peryear(11), **kw)]
    sim = ss.Sim(people=ppl, start=2016.0, stop=2026.0, dt=ss.days(1), demographics=dem, rand_seed=seed, verbose=0)
    sim.init()
    for y in (0, 3, 10):
        if y:
            sim.run(until=2016.0 + y - 1/365) if y < 10 else sim.run()
        a = np.asarray(sim.people.age[sim.people.auids])
        out[y].append([np.mean(a < 5), np.mean((a >= 5) & (a < 15)), np.mean(a >= 15), np.mean(a), np.mean(a < 1), len(a)])
for y, v in out.items():
    m = np.mean(v, axis=0)
    print(f"{ss.__version__} year {y:2d}: u5 {m[0]:.4f} 5-14 {m[1]:.4f} 15+ {m[2]:.4f} mean age {m[3]:.2f} u1 {m[4]:.4f} N {m[5]:.0f}")
