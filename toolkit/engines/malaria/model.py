"""Starsim malaria model: Griffin-lite humans, species-aware vectors, ITN / IRS / SMC / CM.

Promoted from E7 attempt 8 (`models/malaria_abm.py`, Starsim 3.3). Human module unchanged:
S / D (clinical untreated) / T (treated) / A (patent asymptomatic) / U (sub-patent),
superinfection of A/U, exposure-driven immunity (IB, IC, ID, IV) with maternal IC/IV,
lognormal biting heterogeneity and age-dependent biting; immunity and natural-history
parameters fixed at Griffin et al. 2010 / 2014 / 2015 values (10.1371/journal.pmed.1000324).
Severe episodes and deaths are accumulated as expected values (probabilities summed over
infections) to keep package contrasts low-noise.

Changed for the engine (D7): one mosquito population per species, each with its own
bionomics (vector.SPECIES), emergence share, feeding cycle and mortality; ITN effects
split into deflection (repellence + blocking), killing and feeding; IRS deterrence and
killing of indoor-resting mosquitoes (vector.feeding). Daily records feed the engine's
named windows; decision-window accumulators feed per-capita outcomes and cost drivers.
"""
from __future__ import annotations

import numpy as np
import starsim as ss

from . import vector as V

LN2 = np.log(2.0)
G = dict(b0=0.590, b1=0.5, IB0=43.9, kb=2.16, ub=7.2, db=3650.,
         phi0=0.792, phi1=0.00074, IC0=18.02, kc=2.37, uc=6.06, dc=10950., PM=0.774, dm=67.7,
         d1=0.161, ID0=1.578, kd=0.477, ud=9.45, dd=3650., fd0=0.007, ad=21.9 * 365, gd=4.82,
         th0=0.0749, th1=0.0001191, IV0=1.096, kv=2.00, uv=11.43, dv=10950., fv0=0.141,
         av=2493., gv=2.91, PVM=0.195, dvm=76.8,
         dur_D=5., dur_T=5., dur_A=195., dur_U=110., dur_P=25.,
         cD=0.068, cT=0.0219, cU=0.0062, gam1=1.82,
         rho=0.85, a0=8 * 365., sigma2=1.67, tau1=0.69, tau2=2.31, eip=10)
S_, D_, T_, A_, U_ = 0, 1, 2, 3, 4
DAILY = ("pfpr_u5", "pfpr_6m14y", "pfpr_2_10", "inc_u5", "inc_all", "inc_u5_smc", "eir")


class Malaria(ss.Disease):
    def __init__(self, pars=None, **kwargs):
        super().__init__()
        self.define_pars(M0=5.0, season=np.ones(12), resistance=0.5, ft=0.3, species=None, P=None,
                         init_eir=50., win_start=None, win_stop=None, smc_months=(),
                         import_eir=0.1)
        self.update_pars(pars, **kwargs)
        sd = np.sqrt((np.exp(G['sigma2']) - 1))
        self.define_states(
            ss.FloatArr('st', default=0.0), ss.FloatArr('zeta', default=ss.lognorm_ex(mean=1.0, std=sd)),
            ss.FloatArr('IB', default=0.0), ss.FloatArr('IC', default=0.0), ss.FloatArr('ID', default=0.0),
            ss.FloatArr('IV', default=0.0), ss.FloatArr('lb', default=-1e9), ss.FloatArr('lc', default=-1e9),
            ss.FloatArr('ld', default=-1e9), ss.FloatArr('lv', default=-1e9),
            ss.FloatArr('prot_until', default=-1.0), ss.FloatArr('prot_eff', default=0.0),
            ss.BoolState('has_net', default=False), ss.FloatArr('net_t', default=0.0),
            ss.FloatArr('net_type', default=0.0), ss.FloatArr('spray_t', default=-1e9),
            ss.FloatArr('prop_net', default=ss.random()), ss.FloatArr('prop_smc', default=ss.random()),
            ss.FloatArr('prop_spray', default=ss.random()),
        )

    def init_post(self):
        super().init_post()
        p = self.pars
        self.P = dict(p.P)
        self.mix = V.species_mix(p.species)
        u = self.sim.people.auids
        age_d = self.sim.people.age[u] * 365
        bites = p.init_eir / 365 * self.zeta[u] * (1 - G['rho'] * np.exp(-age_d / G['a0']))
        inf = bites * 0.35
        for nm, rate, d, uu in (('IB', bites, G['db'], G['ub']), ('IC', inf, G['dc'], G['uc']),
                                ('ID', inf, G['dd'], G['ud']), ('IV', inf, G['dv'], G['uv'])):
            getattr(self, nm)[u] = np.minimum(rate, 1 / uu) * d * (1 - np.exp(-age_d / d))
        self.st[u] = np.where(self.rs.random(len(u)) < 0.3, A_, S_)
        M = p.M0 * self._season(0)
        self.mos = [dict(S=M * sh * 0.97, I=M * sh * 0.03, q=np.zeros(G['eip'])) for _, sh, _ in self.mix]
        n = len(self.timevec)
        self.daily = {k: np.zeros(n) for k in DAILY}
        self.acc = {k: np.zeros(3) for k in ('cases', 'severe', 'deaths', 'pdays', 'treated')}
        self.acc_cost = dict(smc_doses=0.0, irs_sprayed=0.0, nets_given=0.0, pdays_all=0.0)
        sel = (age_d > 18 * 365) & (age_d < 25 * 365)
        self.ic20 = float(self.IC[u][sel].mean()) if sel.any() else 0.0
        self.iv20 = float(self.IV[u][sel].mean()) if sel.any() else 0.0

    @property
    def rs(self):
        if not hasattr(self, '_rng'):
            self._rng = np.random.default_rng(int(self.sim.pars.rand_seed) * 1000003 + 7)
        return self._rng

    def _season(self, ti):
        doy = (ti % 365) / 365 * 12 - 0.5
        sv = self.pars.season
        i0 = int(np.floor(doy)) % 12
        w = doy - np.floor(doy)
        return (1 - w) * sv[i0] + w * sv[(i0 + 1) % 12]

    def _month(self):
        return int((self.ti % 365) / 365 * 12) % 12 + 1

    def year(self):
        return float(self.sim.t.yearvec[self.ti])

    def in_window(self):
        p, yr = self.pars, self.year()
        return p.win_start is not None and p.win_start <= yr < p.win_stop

    def step(self):
        p, ti, P = self.pars, self.ti, self.P
        u = self.sim.people.auids
        n = len(u)
        age_d = self.sim.people.age[u] * 365
        st = self.st[u]
        w0 = self.zeta[u] * (1 - G['rho'] * np.exp(-age_d / G['a0']))
        sw = w0.sum()
        hn = self.has_net[u]
        n_defl, n_kill, n_fed = V.itn_effect(self.net_type[u], p.resistance, ti - self.net_t[u], P)
        irs_det, irs_kill = V.irs_effect(ti - self.spray_t[u], P)
        # infectiousness of humans to mosquitoes
        q = self._detect(age_d)
        c = np.zeros(n)
        c[st == D_], c[st == T_], c[st == U_] = G['cD'], G['cT'], G['cU']
        isA = st == A_
        c[isA] = G['cU'] + (G['cD'] - G['cU']) * q[isA] ** G['gam1']
        s_t = self._season(ti)
        eir_i = np.full(n, p.import_eir / 365) * w0
        for (name, share, b), mo in zip(self.mix, self.mos):
            y, w, z = V.feeding(b, hn, n_defl, n_kill, n_fed, irs_det, irs_kill)
            f, mu = V.cycle(b, w0, y, w, z, G['tau1'], G['tau2'])
            lam_m = f * b['Q0'] * (w0 * y * c).sum() / sw
            beta = b['mu0'] * p.M0 * share * s_t
            surv = np.exp(-mu)
            newE = mo['S'] * (1 - np.exp(-lam_m))
            k = ti % G['eip']
            emerge = mo['q'][k] * np.exp(-mu * G['eip'])
            mo['q'][k] = newE
            mo['S'] = (mo['S'] - newE) * surv + beta
            mo['I'] = mo['I'] * surv + emerge
            eir_i += mo['I'] * f * b['Q0'] * n / sw * w0 * y
        # human infection
        bitten = self.rs.random(n) < 1 - np.exp(-eir_i)
        self._boost('IB', 'lb', u, bitten, ti, G['ub'])
        bvec = G['b0'] * (G['b1'] + (1 - G['b1']) / (1 + (self.IB[u] / G['IB0']) ** G['kb']))
        prot = np.where(self.prot_until[u] > ti, self.prot_eff[u], 0.0)
        elig = (st != D_) & (st != T_)
        newinf = bitten & elig & (self.rs.random(n) < bvec * (1 - prot))
        ui = u[newinf]
        for nm, ln, uu in (('IC', 'lc', G['uc']), ('ID', 'ld', G['ud']), ('IV', 'lv', G['uv'])):
            self._boost(nm, ln, u, newinf, ti, uu)
        a_i = age_d[newinf]
        icm = G['PM'] * self.ic20 * np.exp(-a_i / G['dm'])
        phi = G['phi0'] * (G['phi1'] + (1 - G['phi1']) / (1 + ((self.IC[ui] + icm) / G['IC0']) ** G['kc']))
        ivm = G['PVM'] * self.iv20 * np.exp(-a_i / G['dvm'])
        fv = 1 - (1 - G['fv0']) / (1 + (a_i / G['av']) ** G['gv'])
        theta = G['th0'] * (G['th1'] + (1 - G['th1']) / (1 + fv * ((self.IV[ui] + ivm) / G['IV0']) ** G['kv']))
        clin = self.rs.random(len(ui)) < phi
        treat = clin & (self.rs.random(len(ui)) < p.ft)
        st[newinf] = np.where(clin, np.where(treat, T_, D_), A_)
        rnd = self.rs.random(n)
        old = ~newinf
        st[old & (st == D_) & (rnd < 1 / G['dur_D'])] = A_
        toP = old & (st == T_) & (rnd < 1 / G['dur_T'])
        st[toP] = S_
        st[old & (st == U_) & (rnd < 1 / G['dur_U'])] = S_
        st[old & (st == A_) & (rnd < 1 / G['dur_A'])] = U_
        self.st[u] = st
        if toP.any():
            self.prot_until[u[toP]] = ti + G['dur_P']
            self.prot_eff[u[toP]] = 1.0
        for nm, d in (('IB', G['db']), ('IC', G['dc']), ('ID', G['dd']), ('IV', G['dv'])):
            getattr(self, nm)[u] = getattr(self, nm)[u] * np.exp(-1 / d)
        lose = hn & (self.rs.random(n) < LN2 / P['net_retention_half'])
        if lose.any():
            self.has_net[u[lose]] = False
        if ti % 365 == 0:
            sel = (age_d > 18 * 365) & (age_d < 25 * 365)
            if sel.any():
                self.ic20, self.iv20 = float(self.IC[u][sel].mean()), float(self.IV[u][sel].mean())
        # daily records (microscopy-detectable prevalence)
        det = ((st == D_) | (st == T_)).astype(float)
        det[st == A_] = q[st == A_]
        u5 = (age_d >= 182) & (age_d < 5 * 365)
        c14 = (age_d >= 182) & (age_d < 15 * 365)
        c210 = (age_d >= 2 * 365) & (age_d < 10 * 365)
        dd = self.daily
        dd['pfpr_u5'][ti] = det[u5].mean() if u5.any() else 0.0
        dd['pfpr_6m14y'][ti] = det[c14].mean() if c14.any() else 0.0
        dd['pfpr_2_10'][ti] = det[c210].mean() if c210.any() else 0.0
        dd['eir'][ti] = eir_i.mean()
        dd['inc_all'][ti] = clin.sum() / n
        nu5 = max((age_d < 5 * 365).sum(), 1)
        dd['inc_u5'][ti] = clin[a_i < 5 * 365].sum() / nu5
        if self._month() in p.smc_months:
            e = (age_d >= 91) & (age_d < 5 * 365)
            dd['inc_u5_smc'][ti] = clin[(a_i >= 91) & (a_i < 5 * 365)].sum() / max(e.sum(), 1)
        if self.in_window():
            ag = np.where(age_d < 5 * 365, 0, np.where(age_d < 15 * 365, 1, 2))
            agi = ag[newinf]
            pt = min(1.0, P['severe_access_add'] + p.ft)
            cfr = (1 - pt) * P['cfr_severe_untreated'] + pt * P['cfr_severe_treated']
            for g in range(3):
                m = agi == g
                self.acc['cases'][g] += clin[m].sum()
                self.acc['treated'][g] += treat[m].sum()
                self.acc['severe'][g] += theta[m].sum()
                self.acc['deaths'][g] += theta[m].sum() * cfr
                self.acc['pdays'][g] += (ag == g).sum()
            self.acc_cost['pdays_all'] += n

    def _detect(self, age_d):
        fd = 1 - (1 - G['fd0']) / (1 + (age_d / G['ad']) ** G['gd'])
        return G['d1'] + (1 - G['d1']) / (1 + fd * (np.maximum(self.ID[self.sim.people.auids], 0) / G['ID0']) ** G['kd'])

    def _boost(self, nm, ln, u, mask, ti, uu):
        if not mask.any():
            return
        um = u[mask]
        uo = um[(ti - getattr(self, ln)[um]) > uu]
        getattr(self, nm)[uo] = getattr(self, nm)[uo] + 1
        getattr(self, ln)[uo] = ti


def _event_tis(sim, targets):
    yv = np.asarray(sim.t.yearvec)
    return {int(np.argmin(np.abs(yv - t))) for t in targets if yv[0] - 1e-9 <= t <= yv[-1]}


class ITNCampaign(ss.Intervention):
    """Mass campaign(s) at given decimal years reaching achieved use `use`."""
    def __init__(self, years=(), use=0.55, net_type='pyr', count=False, name=None):
        super().__init__(name=name)
        self.years, self.use, self.code, self.count = list(years), use, V.NET_CODE[net_type], count

    def step(self):
        if not hasattr(self, '_tis'):
            self._tis = _event_tis(self.sim, self.years)
        if self.ti in self._tis:
            dz = self.sim.diseases.malaria
            u = self.sim.people.auids
            give = u[dz.prop_net[u] < self.use]
            dz.has_net[give] = True
            dz.net_t[give] = self.ti
            dz.net_type[give] = self.code
            if self.count and dz.in_window():
                dz.acc_cost['nets_given'] += len(give) / len(u)


class IRSRound(ss.Intervention):
    """Annual IRS round at `month` of each year; `coverage` = share of people in sprayed houses
    (the same houses each round)."""
    def __init__(self, years=(), month=5, coverage=0.8, name=None):
        super().__init__(name=name)
        self.years, self.month, self.cov = list(years), month, coverage

    def step(self):
        if not hasattr(self, '_tis'):
            self._tis = _event_tis(self.sim, [y + (self.month - 1) / 12 for y in self.years])
        if self.ti in self._tis:
            dz = self.sim.diseases.malaria
            u = self.sim.people.auids
            hit = u[dz.prop_spray[u] < self.cov]
            dz.spray_t[hit] = self.ti
            if dz.in_window():
                dz.acc_cost['irs_sprayed'] += len(hit) / len(u)


class SMCDelivery(ss.Intervention):
    """SMC monthly cycles for 3-59 months: clears infection, 28-day protection with
    efficacy `pe`; semi-correlated cycle coverage (high / low access groups)."""
    def __init__(self, years=(), months=(7, 8, 9, 10), pe=0.882, hi_frac=0.85, p_hi=0.95, p_lo=0.5, name=None):
        super().__init__(name=name)
        self.years, self.months, self.pe = list(years), list(months), pe
        self.hi_frac, self.p_hi, self.p_lo = hi_frac, p_hi, p_lo

    def step(self):
        if not hasattr(self, '_tis'):
            self._tis = _event_tis(self.sim, [y + (m - 1) / 12 for y in self.years for m in self.months])
        if self.ti in self._tis:
            dz = self.sim.diseases.malaria
            u = self.sim.people.auids
            a = self.sim.people.age[u]
            e = u[(a >= 0.25) & (a < 5)]
            hi = dz.prop_smc[e] < self.hi_frac
            got = e[dz.rs.random(len(e)) < np.where(hi, self.p_hi, self.p_lo)]
            if dz.in_window():
                dz.acc_cost['smc_doses'] += len(got) / len(u)
            dz.st[got] = S_
            dz.prot_until[got] = self.ti + 28
            dz.prot_eff[got] = self.pe


class CaseManagement(ss.Intervention):
    """Effective treatment probability of clinical episodes; `add` from `start`."""
    def __init__(self, base=0.3, add=0.0, start=2024.0, name=None):
        super().__init__(name=name)
        self.base, self.add, self.start = base, add, start

    def step(self):
        yr = float(self.sim.t.yearvec[self.ti])
        self.sim.diseases.malaria.pars.ft = min(0.95, self.base + (self.add if yr >= self.start else 0.0))
