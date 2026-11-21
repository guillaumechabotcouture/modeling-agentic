"""Does a finished Starsim sim free itself when its last reference goes (no gc.collect())? The engine's member loop
empties each finished member (shrink) and calls gc.collect() because Starsim 3 sims hold reference cycles."""
import gc, sys, weakref, resource, pickle
import numpy as np, starsim as ss
v4 = int(ss.__version__[0]) >= 4
kw = dict(rate_per=1000) if v4 else {}

def make():
    dem = [ss.Births(birth_rate=ss.peryear(37), **kw), ss.Deaths(death_rate=ss.peryear(11), **kw)]
    return ss.Sim(n_agents=20000, start=2000.0, stop=2001.0, dt=ss.days(1), demographics=dem, rand_seed=1, verbose=0)

gc.disable()
s = make(); s.run(); r = weakref.ref(s); r_ppl = weakref.ref(s.people)
del s
print(ss.__version__, 'freed by refcount alone:', r() is None, '| people freed:', r_ppl() is None)
n = gc.collect()
print(ss.__version__, 'freed after gc.collect():', r() is None, f'({n} objects collected)')
# a pickled copy, as the fork does
s = make(); s.init(); s.run(until=2000.5)
c = pickle.loads(pickle.dumps(s, protocol=pickle.HIGHEST_PROTOCOL)); c.run(); s.run()
rc = weakref.ref(c); del c
print(ss.__version__, 'restored copy freed by refcount alone:', rc() is None)
gc.collect(); print(ss.__version__, 'restored copy freed after gc:', rc() is None)
