"""The engine forks a sim mid-run by pickling it once and unpickling per member ('faster than copy.deepcopy').
Time both on a malaria-sized sim (4,000 agents, run 3 years), and check that a restored copy runs on identically."""
import copy, pickle, sys, time
import numpy as np, starsim as ss
v4 = int(ss.__version__[0]) >= 4
kw = dict(rate_per=1000) if v4 else {}
dem = [ss.Births(birth_rate=ss.peryear(37), **kw), ss.Deaths(death_rate=ss.peryear(11), **kw)]
sim = ss.Sim(n_agents=4000, start=2000.0, stop=2004.0, dt=ss.days(1), demographics=dem, rand_seed=1, verbose=0)
sim.init(); sim.run(until=2003.0)
t = time.time(); snap = pickle.dumps(sim, protocol=pickle.HIGHEST_PROTOCOL); tp = time.time() - t
t = time.time(); a = pickle.loads(snap); tl = time.time() - t
t = time.time(); b = copy.deepcopy(sim); td = time.time() - t
a.run(); b.run(); sim.run()
same = all(int(x.people.alive.sum()) == int(sim.people.alive.sum()) for x in (a, b))
print(f"{ss.__version__}: pickle.dumps {tp:.3f} s ({len(snap)/1e6:.1f} MB), loads {tl:.3f} s, deepcopy {td:.3f} s; copies run on identically: {same}")
