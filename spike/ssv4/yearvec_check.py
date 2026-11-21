import numpy as np, starsim as ss, sys
tl = ss.Timeline(start=2016.0 - 3, stop=2026.0, dt=ss.days(1)); tl.init()
yv = np.asarray(tl.yearvec)
np.save(sys.argv[1], yv)
print(ss.__version__, len(yv), yv[:3], yv[-3:], np.diff(yv)[:3])
