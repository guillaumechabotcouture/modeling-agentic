"""A tuple parameter updated with another tuple warns 'No known mechanism for handling tuple -> tuple'."""
import starsim as ss

class Mod(ss.Module):
    def __init__(self, **kwargs):
        super().__init__()
        self.define_pars(months=())
        self.update_pars(**kwargs)

m = Mod(months=(7, 8, 9))
print(ss.__version__, m.pars.months)
