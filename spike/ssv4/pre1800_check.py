"""v4: a bare start before 1800 is not a calendar year. What does a daily sim from 1750 do?"""
import numpy as np, starsim as ss
for start in (1750.0, 1799.0, 1800.0):
    try:
        tl = ss.Timeline(start=start, stop=start + 2, dt=ss.days(1)); tl.init()
        print('Timeline', start, len(tl.yearvec), np.asarray(tl.yearvec)[[0, 1, -1]])
    except Exception as e:
        print('Timeline', start, 'ERROR', type(e).__name__, str(e)[:300])
    try:
        dem = [ss.Births(birth_rate=ss.peryear(37), rate_per=1000), ss.Deaths(death_rate=ss.peryear(11), rate_per=1000)]
        sim = ss.Sim(n_agents=500, start=start, stop=start + 2, dt=ss.days(1), demographics=dem, rand_seed=1, verbose=0)
        sim.init()
        print('Sim', start, len(sim.t.yearvec), np.asarray(sim.t.yearvec)[[0, 1, -1]])
    except Exception as e:
        print('Sim', start, 'ERROR', type(e).__name__, str(e)[:300])
