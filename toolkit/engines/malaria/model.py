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
per species complex from the Churcher 2016 hut-trial relationships; IRS deterrence (with
diversion to outdoor biting) and killing of indoor-resting mosquitoes with a
product-specific decay (vector.feeding). Daily records feed the engine's named windows;
decision-window accumulators feed per-capita outcomes and cost drivers.

v0.3: use below access. has_net is ACCESS (a net to sleep under); each night a person with
access uses it with the nightly-use rule in using_net (use_rate from Bertozzi-Villa 2021).
Holes grow with net age (vector.apply_holes, Briet 2020).

v0.9: attrition follows the smooth-compact net survival function the same source fits (Bertozzi-Villa 2021,
Supplementary Note 1.4): S(t) = exp(k - k / (1 - (t/L)^2)) for t < L and 0 after, with k fixed at 20 and L set by
the median retention net_retention_half (S(median) = 1/2, so L = median sqrt((k + ln2) / ln2)). Each day a net of
age a days is lost with the conditional hazard 1 - S(a+1)/S(a) (net_loss_prob). v0.3-v0.8 lost nets at the constant
rate ln2 / net_retention_half (an exponential with the median as its half-life).

v0.11: SMC from the literature (ENGINE_NOTES.md, v0.11). A child's per-round reach is a persistent beta propensity
(smc_propensity: mean smc_cov, intra-child correlation smc_rho), a reached child completes the course with probability
smc_adh, every reached child's blood-stage infection is cleared, and new infections are blocked with the Weibull
protection smc_protection (E0 exp(-(t/L)^k ln2), Burgert 2022 Table 1), E0 x smc_partial after the day-1 dose only.
v0.3-v0.10 cleared, then protected at a flat smc_pe for 28 days, with two access groups (smc_hi_frac).

v0.12: case management as a package (ENGINE_NOTES.md, v0.12), with cm_target = 1. The add-on raises effective
treatment of clinical episodes only in ages [cm_age_lo_m, cm_age_hi_m) months (default 2-59 months), ramped in
linearly over cm_ramp_years from its start (cm_ramp); cm_add is the gain inside the iCCM catchment
(cm_gain_at), capped there at 0.95 and then weighted by cm_catchment_share (cm_treat_prob). Severe-care access is
severe_access_add + the unit's effective treatment, plus, inside the catchment, cm_severe_link x the add-on's capped
gain, capped at 1 and then weighted by the share (cm_severe_access; default link 0). The cost drivers count
children in the catchment (cm_catchment_share x the targeted ages) while the add-on runs, and treated episodes in the
targeted ages. cm_target = 0 (the default)
is the v0.11 add-on: all ages, from its start, severe-care access raised one for one.

v0.13 (F6 S2-3 part 2): past SMC rounds may have their own mean per-round reach (SMCDelivery `covs`, from the unit's
smc_hist_cov), apart from smc_cov, which the package's and routine SMC keep. The child's persistent latent and
smc_rho are shared, so the same children tend to be missed in past and future rounds. smc_rounds_pmf gives the
beta-binomial shares of rounds received, which links a survey's per-cycle and all-cycle coverage.

v0.2 (workstream A): households. Agents are grouped into households of mean size
`hh_size`. Three latent normals drive coverage and exposure:
  z_hh   household latent; a person's net-use latent is
         z_net = sqrt(hh_net_corr) z_hh + sqrt(1 - hh_net_corr) e1, and the person uses a net
         after a campaign if Phi(z_net) < itn_use (persistent: the same people use nets);
  z_sp   household spraying latent, corr(z_sp, z_hh) = cov_corr; a household is sprayed if
         Phi(z_sp) < coverage (the same houses each round);
  zeta   biting-rate multiplier, lognormal with Griffin's sigma^2 = 1.67, whose log is
         exp_corr z_net + sqrt(1 - exp_corr^2) e2: high-exposure people use nets less.
Newborns join a random existing household.
"""
from __future__ import annotations

import numpy as np
import starsim as ss

from scipy.special import ndtr

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
# Microscopy prevalence by age band (years, [lo, hi)). v0.4 adds the B7 trials' own bands (Pryce 2018
# characteristics table): under 5 from birth (Halbluetzel 1996), under 10 (Smithuis 2013), 15 and under
# (Moyou-Somo 1995; ages < 16) and all ages (Rowland 1996, Sochantha 2006).
PFPR_BANDS = {"pfpr_u5": (0.5, 5.0), "pfpr_6m14y": (0.5, 15.0), "pfpr_2_10": (2.0, 10.0),
              "pfpr_lt5": (0.0, 5.0), "pfpr_lt10": (0.0, 10.0), "pfpr_le15": (0.0, 16.0), "pfpr_all": (0.0, 1e9)}
DAILY = tuple(PFPR_BANDS) + ("inc_u5", "inc_all", "inc_u5_smc", "eir", "net_use", "net_access")


def net_survival_L(median_days, k) -> float:
    """v0.9: the smooth-compact time by which every net is gone, from the median retention: S(median) = 1/2 gives
    (median / L)^2 = ln2 / (k + ln2). A shape or median that is not finite and positive fails closed (k < 0 would
    give S > 1)."""
    m, k = float(median_days), float(k)
    if not (np.isfinite(m) and m > 0 and np.isfinite(k) and k > 0):
        raise ValueError(f"net survival needs a finite positive median and shape k (median {m!r} d, k {k!r})")
    return m * float(np.sqrt((k + LN2) / LN2))


def _log_net_survival(age_days, median_days, k):
    """log S(t) = k - k / (1 - x^2) = -k x^2 / (1 - x^2) with x = t / L (the second form keeps very long medians
    exact); -inf at or after L."""
    x = np.maximum(np.asarray(age_days, float), 0.0) / net_survival_L(median_days, k)
    x2 = x * x
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(x2 < 1.0, -k * x2 / (1.0 - x2), -np.inf)


def net_survival(age_days, median_days, k):
    """v0.9: share of nets still held at age `age_days` (days), smooth-compact (Bertozzi-Villa 2021 Supplementary
    Note 1.4): exp(k - k / (1 - (t/L)^2)) for t < L, else 0. The sim's attrition and access_for_use both use it."""
    return np.exp(_log_net_survival(age_days, median_days, k))


def net_loss_prob(age_days, median_days, k):
    """v0.9: probability that a net of age a (days) held this morning is lost today, 1 - S(a+1)/S(a); 1 at or past
    L. Compounded from age 0 it reproduces S(t)."""
    a = np.maximum(np.asarray(age_days, float), 0.0)
    l0, l1 = _log_net_survival(a, median_days, k), _log_net_survival(a + 1.0, median_days, k)
    with np.errstate(invalid="ignore"):
        return np.where(np.isfinite(l0), -np.expm1(l1 - l0), 1.0)


def prevalence_bands(det, age_d) -> dict:
    """Mean microscopy-detectable share (det: per-person probability of a positive slide) in each
    age band of PFPR_BANDS; age_d in days. The 6-month lower bound of the old bands is 182 days."""
    out = {}
    for k, (lo, hi) in PFPR_BANDS.items():
        lo_d = 182.0 if lo == 0.5 else lo * 365
        sel = (age_d >= lo_d) & (age_d < hi * 365)
        out[k] = float(det[sel].mean()) if sel.any() else 0.0
    return out


class Malaria(ss.Disease):
    def __init__(self, pars=None, **kwargs):
        super().__init__()
        self.define_pars(M0=5.0, season=np.ones(12), resistance=0.5, ft=0.3, species=None, P=None,
                         init_eir=50., win_start=None, win_stop=None, smc_months=(),
                         import_eir=0.1, irs_product="pm_cs", rec_year=None, del_start=None, del_stop=None)
        self.update_pars(pars, **kwargs)
        sd = np.sqrt((np.exp(G['sigma2']) - 1))
        self.define_states(
            ss.FloatArr('st', default=0.0), ss.FloatArr('zeta', default=ss.lognorm_ex(mean=1.0, std=sd)),
            ss.FloatArr('IB', default=0.0), ss.FloatArr('IC', default=0.0), ss.FloatArr('ID', default=0.0),
            ss.FloatArr('IV', default=0.0), ss.FloatArr('lb', default=-1e9), ss.FloatArr('lc', default=-1e9),
            ss.FloatArr('ld', default=-1e9), ss.FloatArr('lv', default=-1e9),
            ss.FloatArr('prot_until', default=-1.0), ss.FloatArr('prot_eff', default=0.0),
            ss.FloatArr('smc_t', default=-1e9), ss.FloatArr('smc_eff', default=0.0),
            ss.BoolState('has_net', default=False), ss.FloatArr('net_t', default=0.0),
            ss.FloatArr('net_type', default=0.0), ss.FloatArr('spray_t', default=-1e9),
            ss.FloatArr('prop_net', default=ss.random()), ss.FloatArr('prop_smc', default=ss.random()),
            ss.FloatArr('prop_spray', default=ss.random()),
            ss.FloatArr('hh', default=-1.0), ss.FloatArr('z_hh', default=0.0), ss.FloatArr('z_sp', default=0.0),
            ss.FloatArr('prop_use', default=ss.random()),
        )

    def init_post(self):
        super().init_post()
        p = self.pars
        self.P = dict(p.P)
        self.mix = V.species_mix(p.species)
        self.res = {name: V.resistance_of(p.resistance, name) for name, _, _ in self.mix}
        u = self.sim.people.auids
        self._households(u, initial=True)
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
        # v0.6: treatments and person-days in the delivery period (the treatment cost driver); the same terms in the
        # same order as acc['treated'] and pdays_all, so they are equal when the delivery period is the window
        self.acc_del_treated, self.acc_cost['pdays_del'] = np.zeros(3), 0.0
        # v0.12 (cm_target = 1): the add-on's state, set by CaseManagement each step, and its cost drivers in the
        # delivery period (child-days in the targeted ages while the add-on runs; treated episodes in those ages)
        self.cm_on, self.cm_gain, self.acc_cm = False, 0.0, np.zeros(2)
        sel = (age_d > 18 * 365) & (age_d < 25 * 365)
        self.ic20 = float(self.IC[u][sel].mean()) if sel.any() else 0.0
        self.iv20 = float(self.IV[u][sel].mean()) if sel.any() else 0.0
        self.n_start, self.start_sha = None, None

    def state_hash(self, u) -> str:
        """sha256 of the human and mosquito state and the daily records so far (v0.5: packages forked from one
        burn-in must agree on it at horizon.start)."""
        import hashlib
        h = hashlib.sha256()
        for nm in ('st', 'zeta', 'IB', 'IC', 'ID', 'IV', 'lb', 'lc', 'ld', 'lv', 'prot_until', 'prot_eff', 'smc_t',
                   'smc_eff', 'has_net', 'net_t', 'net_type', 'spray_t', 'hh', 'z_hh', 'z_sp', 'prop_net', 'prop_use',
                   'prop_smc'):
            h.update(np.ascontiguousarray(np.asarray(getattr(self, nm)[u], float)).tobytes())
        h.update(np.ascontiguousarray(np.asarray(self.sim.people.age[u], float)).tobytes())
        for mo in self.mos:
            h.update(np.asarray([mo['S'], mo['I']], float).tobytes() + np.asarray(mo['q'], float).tobytes())
        for k in DAILY:
            h.update(self.daily[k][:self.ti].tobytes())
        return h.hexdigest()

    def _households(self, u, initial=False):
        """Assign households and the correlated coverage / exposure latents (see module doc)."""
        P, rs, n = self.P, self.rs, len(u)
        if n == 0:
            return
        if initial:
            n_hh = max(int(np.ceil(n / P["hh_size"])), 1)
            hh = rs.permutation(n) % n_hh
            zh, z2 = rs.normal(size=n_hh), rs.normal(size=n_hh)
            c = P["cov_corr"]
            self.hh[u] = hh
            self.z_hh[u] = zh[hh]
            self.z_sp[u] = c * zh[hh] + np.sqrt(1 - c * c) * z2[hh]
        else:
            have = self.sim.people.auids
            have = have[self.hh[have] >= 0]
            # v0.5 (review P2): newborns found new households as needed to keep the mean household size at
            # hh_size while the population grows (v0.4 never created households: over a 50-year burn-in the mean
            # size grew about 4-fold); the others join the household of a random existing person
            k = len(np.unique(np.asarray(self.hh[have]))) if len(have) else 0
            need = int(np.clip(np.ceil((len(have) + n) / P["hh_size"]) - k, 0, n)) if len(have) else n
            order = rs.permutation(n)
            fnd, join = u[order[:need]], u[order[need:]]
            if len(fnd):
                start = (float(np.max(np.asarray(self.hh[have]))) + 1) if len(have) else 0.0
                zh, z2 = rs.normal(size=len(fnd)), rs.normal(size=len(fnd))
                c = P["cov_corr"]
                self.hh[fnd] = start + np.arange(len(fnd))
                self.z_hh[fnd] = zh
                self.z_sp[fnd] = c * zh + np.sqrt(1 - c * c) * z2
            if len(join):
                donor = have[rs.integers(0, len(have), size=len(join))]
                self.hh[join] = self.hh[donor]
                self.z_hh[join] = self.z_hh[donor]
                self.z_sp[join] = self.z_sp[donor]
        r, a = P["hh_net_corr"], P["exp_corr"]
        z_net = np.sqrt(r) * self.z_hh[u] + np.sqrt(1 - r) * rs.normal(size=n)
        self.prop_net[u] = ndtr(z_net)
        self.prop_spray[u] = ndtr(self.z_sp[u])
        sig = np.sqrt(G['sigma2'])
        zz = a * z_net + np.sqrt(1 - a * a) * rs.normal(size=n)
        self.zeta[u] = np.exp(sig * zz - G['sigma2'] / 2)

    def using_net(self, u):
        """Who sleeps under a net tonight: access (has_net), not a persistent non-user, and the
        nightly draw. A share use_persist of the use gap (1 - use_rate) is persistent non-use
        (prop_use < q); the others use their net on a night with probability use_rate / (1 - q),
        so mean nightly use among those with access is use_rate."""
        P = self.P
        acc = self.has_net[u]
        q = P['use_persist'] * (1 - P['use_rate'])
        p_night = min(1.0, P['use_rate'] / max(1 - q, 1e-9))
        draw = self.rs.random(len(u)) < p_night
        return acc & (self.prop_use[u] >= q) & draw

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

    def in_delivery(self):
        """v0.6: inside the delivery period (what the decision pays for); the window when none is set."""
        p = self.pars
        return delivery_contains(self.year(), p.del_start, p.del_stop, p.win_start, p.win_stop)

    def step(self):
        p, ti, P = self.pars, self.ti, self.P
        u = self.sim.people.auids
        new = u[self.hh[u] < 0]
        if len(new):
            self._households(new)
        if self.n_start is None and p.rec_year is not None and self.year() >= p.rec_year - 1e-9:
            self.n_start, self.start_sha = len(u), self.state_hash(u)      # the state at horizon.start
        n = len(u)
        age_d = self.sim.people.age[u] * 365
        st = self.st[u]
        w0 = self.zeta[u] * (1 - G['rho'] * np.exp(-age_d / G['a0']))
        sw = w0.sum()
        hn = self.using_net(u)
        net_type, net_age = self.net_type[u], ti - self.net_t[u]
        irs_det, irs_kill = V.irs_effect(ti - self.spray_t[u], P, p.irs_product)
        # infectiousness of humans to mosquitoes
        q = self._detect(age_d)
        c = np.zeros(n)
        c[st == D_], c[st == T_], c[st == U_] = G['cD'], G['cT'], G['cU']
        isA = st == A_
        c[isA] = G['cU'] + (G['cD'] - G['cU']) * q[isA] ** G['gam1']
        s_t = self._season(ti)
        eir_i = np.full(n, p.import_eir / 365) * w0
        for (name, share, b), mo in zip(self.mix, self.mos):
            n_defl, n_kill, n_fed = V.itn_effect(net_type, self.res[name], net_age, P, b["complex"])
            y, w, z = V.feeding(b, hn, n_defl, n_kill, n_fed, irs_det, irs_kill, P["irs_divert"])
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
        # v0.11: SMC protection against new infection decays from the last dose; it combines with the post-treatment
        # prophylaxis as 1 - (1 - a)(1 - b)
        dosed = self.smc_t[u] > -1e8
        if dosed.any():
            ps = np.zeros(n)
            ps[dosed] = smc_protection(ti - self.smc_t[u][dosed], self.smc_eff[u][dosed], P['smc_half'], P['smc_k'])
            prot = 1.0 - (1.0 - prot) * (1.0 - ps)
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
        tgt = bool(P.get('cm_target', 0.0))
        if tgt:     # v0.12: the add-on's gain only in the targeted ages (the same draws as v0.11)
            pft, in_t = cm_treat_prob(a_i, p.ft, self.cm_gain, P['cm_catchment_share'], P['cm_age_lo_m'],
                                      P['cm_age_hi_m'])
            treat = clin & (self.rs.random(len(ui)) < pft)
        else:
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
        # v0.9: smooth-compact attrition by net age (net_loss_prob); one uniform draw per person, as before
        lose = self.has_net[u] & (self.rs.random(n) < net_loss_prob(net_age, P['net_retention_half'], P['net_survival_k']))
        if lose.any():
            self.has_net[u[lose]] = False
        if ti % 365 == 0:
            sel = (age_d > 18 * 365) & (age_d < 25 * 365)
            if sel.any():
                self.ic20, self.iv20 = float(self.IC[u][sel].mean()), float(self.IV[u][sel].mean())
        # daily records (microscopy-detectable prevalence)
        det = ((st == D_) | (st == T_)).astype(float)
        det[st == A_] = q[st == A_]
        dd = self.daily
        for k, v in prevalence_bands(det, age_d).items():
            dd[k][ti] = v
        dd['eir'][ti] = eir_i.mean()
        dd['net_use'][ti] = hn.mean()
        dd['net_access'][ti] = self.has_net[u].mean()
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
            cfr_i = None
            if tgt and self.cm_gain > 0 and P['cm_severe_link'] > 0:
                # v0.12: severe-care access rises only through the declared link, in the targeted ages
                pti = np.where(in_t, cm_severe_access(P['severe_access_add'], p.ft, P['cm_severe_link'], self.cm_gain,
                                                      P['cm_catchment_share']), pt)
                cfr_i = (1 - pti) * P['cfr_severe_untreated'] + pti * P['cfr_severe_treated']
            for g in range(3):
                m = agi == g
                self.acc['cases'][g] += clin[m].sum()
                self.acc['treated'][g] += treat[m].sum()
                self.acc['severe'][g] += theta[m].sum()
                self.acc['deaths'][g] += theta[m].sum() * cfr if cfr_i is None else (theta[m] * cfr_i[m]).sum()
                self.acc['pdays'][g] += (ag == g).sum()
            self.acc_cost['pdays_all'] += n
            if self.in_delivery():         # the delivery period lies inside the window (the engine checks it)
                for g in range(3):
                    self.acc_del_treated[g] += treat[agi == g].sum()
                self.acc_cost['pdays_del'] += n
                if tgt:
                    if self.cm_on:
                        self.acc_cm[0] += cm_in_ages(age_d, P['cm_age_lo_m'], P['cm_age_hi_m']).sum()
                    self.acc_cm[1] += (treat & in_t).sum()

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


def delivery_contains(yr, del_start, del_stop, win_start, win_stop) -> bool:
    """v0.8: whether a step at decimal year `yr` is in the delivery period, where cost drivers count (the window when
    no delivery period is set). Malaria.in_delivery and the engine's schedule() share it."""
    if del_start is None:
        return win_start is not None and win_start <= yr < win_stop
    return del_start <= yr < del_stop


def event_steps(yv, targets) -> set:
    """v0.8: the daily steps at which delivery targets (decimal years) act: each target's nearest step inside the
    time vector; targets on one step are one delivery. The interventions and the engine's schedule() share it."""
    yv = np.asarray(yv)
    return {int(np.argmin(np.abs(yv - t))) for t in targets if yv[0] - 1e-9 <= t <= yv[-1]}


def _event_tis(sim, targets):
    return event_steps(sim.t.yearvec, targets)


def smc_targets(years, months, after=None, before=None) -> list:
    """Decimal times of SMC rounds: month starts of `months` in `years`, kept when after <= t < before (None: no
    bound). SMCDelivery and the engine's delivery plan share it."""
    return [t for t in (y + (m - 1) / 12 for y in years for m in months)
            if (after is None or t >= after) and (before is None or t < before)]


def irs_targets(years, month) -> list:
    """Decimal times of IRS rounds: the start of `month` in each of `years` (IRSRound and the delivery plan)."""
    return [y + (month - 1) / 12 for y in years]


class ITNCampaign(ss.Intervention):
    """Mass campaign(s) at given decimal years reaching achieved use `use`.
    v0.10: `net_types` (optional, one net type per entry of `years`) gives each campaign its own net type; a step
    reached by several campaigns takes the type of the first of them in `years` (as the schedule lists it). None:
    every campaign gives `net_type`."""
    def __init__(self, years=(), use=0.55, net_type='pyr', count=False, name=None, net_types=None):
        super().__init__(name=name)
        self.years, self.use, self.code, self.count = list(years), use, V.NET_CODE[net_type], count
        if net_types is not None and len(net_types) != len(self.years):
            raise ValueError(f"ITNCampaign: {len(net_types)} net types for {len(self.years)} campaign years")
        self.codes = None if net_types is None else [V.NET_CODE[t] for t in net_types]

    def _code(self):
        if self.codes is None:
            return self.code
        if not hasattr(self, '_codes'):
            yv = self.sim.t.yearvec
            self._codes = {}
            for y, c in zip(self.years, self.codes):
                for ti in event_steps(yv, [y]):
                    self._codes.setdefault(ti, c)
        return self._codes.get(self.ti, self.code)

    def step(self):
        if not hasattr(self, '_tis'):
            self._tis = _event_tis(self.sim, self.years)
        if self.ti in self._tis:
            dz = self.sim.diseases.malaria
            u = self.sim.people.auids
            give = u[dz.prop_net[u] < self.use]
            dz.has_net[give] = True
            dz.net_t[give] = self.ti
            dz.net_type[give] = self._code()
            if self.count and dz.in_delivery():
                dz.acc_cost['nets_given'] += len(give) / len(u)


class IRSRound(ss.Intervention):
    """Annual IRS round at `month` of each year; `coverage` = share of people in sprayed houses
    (the same households each round, drawn from the household spraying latent)."""
    def __init__(self, years=(), month=5, coverage=0.8, name=None):
        super().__init__(name=name)
        self.years, self.month, self.cov = list(years), month, coverage

    def step(self):
        if not hasattr(self, '_tis'):
            self._tis = _event_tis(self.sim, irs_targets(self.years, self.month))
        if self.ti in self._tis:
            dz = self.sim.diseases.malaria
            u = self.sim.people.auids
            hit = u[dz.prop_spray[u] < self.cov]
            dz.spray_t[hit] = self.ti
            if dz.in_delivery():
                dz.acc_cost['irs_sprayed'] += len(hit) / len(u)


SMC_PARAM_NAMES = ("smc_cov", "smc_rho", "smc_adh", "smc_e0", "smc_half", "smc_k", "smc_partial")


def smc_check(P: dict) -> None:
    """v0.11: the SMC parameters' domains; a value outside fails closed (ValueError naming the parameter)."""
    def num(k):
        v = P[k]
        if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)) or not np.isfinite(v):
            raise ValueError(f"{k} must be a finite number (got {v!r})")
        return float(v)
    if not 0.0 < num("smc_cov") <= 1.0:
        raise ValueError(f"smc_cov must be in (0, 1] (got {P['smc_cov']!r})")
    if not 0.0 <= num("smc_rho") < 1.0:
        raise ValueError(f"smc_rho must be in [0, 1) (got {P['smc_rho']!r})")
    for k in ("smc_adh", "smc_e0", "smc_partial"):
        if not 0.0 <= num(k) <= 1.0:
            raise ValueError(f"{k} must be in [0, 1] (got {P[k]!r})")
    for k in ("smc_half", "smc_k"):
        if not num(k) > 0.0:
            raise ValueError(f"{k} must be finite and positive (got {P[k]!r})")


def smc_protection(dt_days, e0, half, k):
    """v0.11: SMC protection against new infection dt_days after a dose, E0 exp(-(dt/L)^k ln2) (Burgert et al. 2022,
    PLOS Glob Public Health, Table 1 and Eq 1: E0 1, L 32 days, k 5.4); 0 before a dose (dt < 0)."""
    dt = np.asarray(dt_days, float)
    x = np.maximum(dt, 0.0) / float(half)
    return np.where(dt >= 0.0, np.asarray(e0, float) * np.exp(-(x ** float(k)) * LN2), 0.0)


def smc_rounds_pmf(mean, rho, n) -> list:
    """v0.13: the share of children reached in 0..n of n rounds when each child's per-round reach is the beta
    propensity of smc_propensity (mean `mean`, intra-child correlation `rho`): the beta-binomial pmf. Its last entry
    is the all-rounds share that coverage surveys report beside the per-cycle mean. rho = 0 gives the binomial.
    Rising-factorial ratios, each bounded, so neither limit of rho needs a special case (Codex r1 and r2 P3).
    Outside smc_cov's and smc_rho's domains, or with n not a whole number >= 0, it fails closed (Codex r1 P2)."""
    from math import comb, isfinite, prod

    def num(x):
        return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, (bool, np.bool_)) \
            and isfinite(float(x))
    if not num(mean) or not 0.0 < float(mean) <= 1.0:
        raise ValueError(f"smc_rounds_pmf: mean {mean!r} must be in (0, 1]")
    if not num(rho) or not 0.0 <= float(rho) < 1.0:
        raise ValueError(f"smc_rounds_pmf: rho {rho!r} must be in [0, 1)")
    if not num(n) or float(n) != int(float(n)) or int(float(n)) < 0:
        raise ValueError(f"smc_rounds_pmf: n {n!r} must be a whole number of rounds >= 0")
    m, n, r = float(mean), int(float(n)), float(rho)
    # each factor (a + i) / (a + b + i) with a = m s, b = (1 - m) s, s = 1/rho - 1, multiplied through by rho: bounded
    # by n, so no overflow near rho 1 (Codex r2 P3) and exact at rho 0
    return [comb(n, k) * prod((m * (1.0 - r) + i * r) / ((1.0 - r) + i * r) for i in range(k))
            * prod(((1.0 - m) * (1.0 - r) + j * r) / ((1.0 - r) + (k + j) * r) for j in range(n - k))
            for k in range(n + 1)]


def smc_propensity(u, mean, rho):
    """v0.11: a child's per-round SMC reach probability from its uniform latent u: the u-quantile of a beta with mean
    `mean` and intra-child correlation `rho` (a = mean s, b = (1 - mean) s, s = 1/rho - 1), so the rounds a child is
    reached in a season are beta-binomial (fitted to Malaria Consortium 2020 Table 21). rho = 0: every child has the
    mean (independent rounds); mean 1: every child is reached."""
    u = np.asarray(u, float)
    m, r = float(mean), float(rho)
    if r < 1e-9 or m >= 1.0:          # Codex r1 P3: a tiny positive rho is the independent limit (1/rho overflows)
        return np.full(u.shape, m)
    from scipy.special import betaincinv
    s = 1.0 / r - 1.0
    return betaincinv(m * s, (1.0 - m) * s, np.clip(u, 0.0, 1.0))


class SMCDelivery(ss.Intervention):
    """SMC monthly cycles for 3-59 months (v0.11; ENGINE_NOTES.md, v0.11). Each round reaches a child with its
    persistent propensity (smc_propensity of prop_smc: mean `cov`, correlation `rho`); a reached child is cleared of
    blood-stage infection and protected from that day by smc_protection, with E0 = `e0` after a full three-day
    course (probability `adh`) and `e0` x `partial` after the day-1 dose only. The decay (smc_half, smc_k) is read
    from the disease's parameters at each step.
    v0.7: `count` False never adds doses to the cost driver (routine SMC); `after` / `before` keep only the rounds
    at decimal times after <= t < before (None: no bound).
    v0.10: `times` (optional) gives the rounds' decimal times directly (past SMC, whose rounds per year may differ);
    None: the rounds are the month starts of `months` in `years`.
    v0.13: `covs` (optional, one per entry of `times`) gives each round its own mean reach in place of `cov` (past
    SMC with smc_hist_cov); None: every round at `cov`, as in v0.12."""
    def __init__(self, years=(), months=(7, 8, 9, 10), P=None, name=None, count=True, after=None, before=None,
                 times=None, covs=None):
        super().__init__(name=name)
        self.years, self.months = list(years), list(months)
        self.set_smc(P if P is not None else dict(smc_cov=0.796, smc_rho=0.417, smc_adh=0.944, smc_e0=1.0,
                                                  smc_partial=0.910))
        self.count, self.after, self.before = count, after, before
        self.times = None if times is None else [float(t) for t in times]
        if covs is not None and (self.times is None or len(covs) != len(self.times)):
            raise ValueError("SMCDelivery: covs needs one mean reach per entry of times")
        self.covs = None if covs is None else [float(c) for c in covs]

    def set_smc(self, P: dict) -> None:
        """The delivery's reach and dose parameters from an engine parameter dict."""
        self.cov, self.rho, self.adh = float(P["smc_cov"]), float(P["smc_rho"]), float(P["smc_adh"])
        self.e0, self.partial = float(P["smc_e0"]), float(P["smc_partial"])

    def targets(self):
        """The decimal times of the rounds."""
        if self.times is not None:
            return list(self.times)
        return smc_targets(self.years, self.months, self.after, self.before)

    def step(self):
        if not hasattr(self, '_tis'):
            self._tis = _event_tis(self.sim, self.targets())
        if self.ti in self._tis:
            dz = self.sim.diseases.malaria
            u = self.sim.people.auids
            a = self.sim.people.age[u]
            e = u[(a >= 0.25) & (a < 5)]
            cov = self.cov
            if self.covs is not None:          # v0.13: this round's own mean reach (past SMC, smc_hist_cov)
                if not hasattr(self, '_cov_ti'):
                    self._cov_ti = {s: c for t, c in zip(self.times, self.covs) for s in _event_tis(self.sim, [t])}
                cov = self._cov_ti[self.ti]
            pi = smc_propensity(dz.prop_smc[e], cov, self.rho)
            got = e[dz.rs.random(len(e)) < pi]
            full = dz.rs.random(len(got)) < self.adh
            if self.count and dz.in_delivery():
                dz.acc_cost['smc_doses'] += len(got) / len(u)
            dz.st[got] = S_                     # clearance at dosing (kept from v0.10)
            dz.smc_t[got] = self.ti
            dz.smc_eff[got] = np.where(full, self.e0, self.e0 * self.partial)


CM_PARAM_NAMES = ("cm_target", "cm_age_lo_m", "cm_age_hi_m", "cm_ramp_years", "cm_severe_link", "cm_catchment_share")
DAYS_PER_MONTH = 365.0 / 12.0        # ages are kept in days of 365 per year (age_d = age x 365)


def cm_ramp(yr, start, years) -> float:
    """v0.12: the share of the add-on's gain in effect at decimal year `yr`: linear from 0 at `start` to 1 at
    start + years, 1 after; a step (1 from `start`) when years is 0."""
    if years <= 0:
        return 1.0
    return float(min(1.0, max(0.0, (yr - start) / years)))


def cm_gain_at(yr, add, start, stop, ramp_years, pkg) -> float:
    """v0.12: the within-catchment gain in effective treatment in the targeted ages at decimal year `yr`: `add` x
    cm_ramp while the package's add-on runs (start <= yr < stop; stop None: no end), else 0. A delivery period that
    stops before the ramp completes ends the gain at the stop. The catchment share weights it after the caps
    (cm_treat_prob, cm_severe_access; Codex r2 P2-1)."""
    if not pkg or yr < start or (stop is not None and yr >= stop):
        return 0.0
    return add * cm_ramp(yr, start, ramp_years)


def cm_in_ages(age_days, lo_m, hi_m):
    """v0.12: who is in the targeted ages [lo_m, hi_m) months (ages in days of 365 per year)."""
    a = np.asarray(age_days, float)
    return (a >= lo_m * DAYS_PER_MONTH) & (a < hi_m * DAYS_PER_MONTH)


def cm_treat_prob(age_days, ft, gain, share, lo_m, hi_m):
    """v0.12: (per-person effective-treatment probability, in the targeted ages). In the targeted ages it is the
    catchment-weighted mean (1 - share) ft + share min(0.95, ft + gain): the cap applies inside the catchment, before
    weighting (Codex r2 P2-1). Elsewhere, and with no gain, ft exactly."""
    in_t = cm_in_ages(age_days, lo_m, hi_m)
    if not gain > 0:
        return np.full(in_t.shape, ft, dtype=float), in_t
    return np.where(in_t, (1.0 - share) * ft + share * min(0.95, ft + gain), ft), in_t


def cm_severe_access(access_add, ft, link, gain, share) -> float:
    """v0.12: severe-care access of a person in the targeted ages, the catchment-weighted mean
    (1 - share) min(1, access_add + ft) + share min(1, access_add + ft + link x (min(0.95, ft + gain) - ft)): each cap
    inside, then the weights (Codex r2 P2-1)."""
    out = min(1.0, access_add + ft)
    inside = min(1.0, access_add + ft + link * (min(0.95, ft + gain) - ft))
    return (1.0 - share) * out + share * inside


def cm_check(P: dict) -> None:
    """v0.12: the case-management options' domains; a value outside fails closed (ValueError naming the parameter).
    With cm_target 0 (the v0.11 add-on) only the switch is checked."""
    def num(k):
        v = P[k]
        if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)) or not np.isfinite(v):
            raise ValueError(f"{k} must be a finite number (got {v!r})")
        return float(v)
    if P.get("cm_target", 0.0) not in (0.0, 1.0):
        raise ValueError(f"cm_target must be 0 (the v0.11 add-on) or 1 (got {P['cm_target']!r})")
    if not P.get("cm_target", 0.0):
        return
    lo, hi = P["cm_age_lo_m"], P["cm_age_hi_m"]
    if not (np.isfinite(lo) and np.isfinite(hi) and 0.0 <= lo < hi):
        raise ValueError(f"cm_age_lo_m and cm_age_hi_m must be finite with 0 <= cm_age_lo_m < cm_age_hi_m (months; "
                         f"got {lo!r}, {hi!r})")
    if not num("cm_ramp_years") >= 0.0:
        raise ValueError(f"cm_ramp_years must be >= 0 (got {P['cm_ramp_years']!r})")
    for k, top in (("cm_severe_link", 1.0), ("cm_add", 0.95)):
        if not 0.0 <= num(k) <= top:
            raise ValueError(f"{k} must be in [0, {top:g}] (got {P[k]!r})")
    if not 0.0 < num("cm_catchment_share") <= 1.0:
        raise ValueError(f"cm_catchment_share must be in (0, 1] (got {P['cm_catchment_share']!r})")


class CaseManagement(ss.Intervention):
    """Effective treatment probability of clinical episodes; `add` from `start` until `stop` (v0.6; None: no end).
    v0.12: with cm_target = 1 the unit's level `base` applies to every age and the add-on's within-catchment gain
    (cm_gain_at: `add` x cm_ramp) to the targeted ages only, weighted by cm_catchment_share after the cap
    (Malaria.step); `pkg` (set by the engine) says the package has the add-on, whose cost
    drivers count even at add 0."""
    def __init__(self, base=0.3, add=0.0, start=2024.0, stop=None, name=None):
        super().__init__(name=name)
        self.base, self.add, self.start, self.stop = base, add, start, stop
        self.pkg = False

    def step(self):
        yr = float(self.sim.t.yearvec[self.ti])
        on = yr >= self.start and (self.stop is None or yr < self.stop)
        dz = self.sim.diseases.malaria
        if not dz.P.get("cm_target", 0.0):
            dz.pars.ft = min(0.95, self.base + (self.add if on else 0.0))
            return
        dz.pars.ft = min(0.95, self.base)
        dz.cm_on = bool(on and self.pkg)
        dz.cm_gain = cm_gain_at(yr, self.add, self.start, self.stop, dz.P["cm_ramp_years"], self.pkg)
