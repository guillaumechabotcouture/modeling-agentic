"""Starsim 3.3.2's Births/Deaths did nothing in a sim that starts before 1900 (engine MIN_START_YEAR). Does v4?"""
import starsim as ss
v4 = int(ss.__version__[0]) >= 4
kw = dict(rate_per=1000) if v4 else {}
for start in (1850.0, 1890.0, 1899.0, 1900.0, 2000.0):
    dem = [ss.Births(birth_rate=ss.peryear(37), **kw), ss.Deaths(death_rate=ss.peryear(11), **kw)]
    sim = ss.Sim(n_agents=500, start=start, stop=start + 5, dt=ss.days(1), demographics=dem, rand_seed=1, verbose=0)
    sim.run()
    print(ss.__version__, start, 'alive after 5 y:', int(sim.people.alive.sum()))
