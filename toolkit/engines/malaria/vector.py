#!/usr/bin/env python3
"""Species-aware vector bionomics and ITN / IRS mechanics (engine v0.2).

    python3 toolkit/engines/malaria/vector.py --self-test

Bionomics per species (D6 prior `malaria_vector_bionomics_by_species` in
.claude/orchestration/expert_priors.yaml): anthropophagy Q0, share of bites indoors
phi_I and in bed phi_B, adult daily mortality mu0 (Sherrard-Smith et al. 2019 PNAS
10.1073/pnas.1820646116; Griffin et al. 2010 PLoS Med 10.1371/journal.pmed.1000324).
Endophily (share of indoor-fed mosquitoes that rest indoors on a sprayable surface) is
NOT in that prior: the values are assumptions ordered as in Sinka et al. 2010 and are
flagged `unverified` in the engine card.

One feeding attempt on a person, by where it happens:
  outdoors (1 - phi_I)              untouched by nets and IRS
  indoors, not in bed (phi_I - phi_B) IRS deterrence only
  indoors, in bed (phi_B)           IRS deterrence, then the net if the person uses one
A mosquito deterred by IRS is diverted to biting outdoors with probability `irs_divert`
(it then feeds, untouched by nets and IRS) and otherwise retries (it lengthens the feeding
cycle). A mosquito repelled by a net retries; one killed by the net never feeds. A
mosquito that feeds indoors rests indoors with probability `endophily`, and is then
killed by IRS with probability kill(t).

NETS: published hut-trial relationships (Churcher et al. 2016 eLife 10.7554/eLife.16090,
Table 6 and Equations 2, 4, 8, 11-19; read from PMC5025277). From discriminating-dose
pyrethroid bioassay mortality x (1 = susceptible), tau = 0.5:
  hut mortality         logit(l) = 0.63 + 4.00 (x - tau)
  PBO bioassay          logit(f) = 3.41 + 5.88 (x - tau) / (1 + 0.78 (x - tau))    An. gambiae complex
                        (Equation 4; v0.2 divided b1 too, a transcription error from flattened text)
                        logit(f) = 2.53 + 0.89 (x - tau)                            An. funestus
                        (a PBO net uses l evaluated at f instead of x)
  entry vs untreated    m = 1 - [0.071 + 1.26 (l - tau) + 1.52 (l - tau)^2], capped to [0, 1]
  fed and survived      k = 0.02 exp(3.32 (1 - l - tau));  exit j = 1 - l - k;  k0 = 0.70
  per attempt (new net) s0 = m k / k0;  r0 = (1 - s0) j' / (j' + l');  d0 = (1 - s0) l' / (j' + l')
                        with j' = m j + 1 - m, l' = m l
  decay                 logit(gamma_wash) = -2.36 - 3.05 (l - tau);  H_y = H_w / H_w(l=1) x 2.64 y
                        r(t) = (r0 - r_M) exp(-gamma t) + r_M, r_M = 0.24;  d(t) = d0 exp(-gamma t)
Note: Table 6 gives the funestus PBO row as two coefficients (2.53, 0.89); the linear form
used for it is our reading of the table (flagged unverified in the engine card).

Holes (physical decay of nets in use; v0.3, re-derived after review): Briet et al. 2020
Malar J 10.1186/s12936-020-03383-6, main-text equation block (Methods, "Entomological
model") and Additional file 2 Table S9 (PermaNet 2.0; susceptible Kisumu/Zeneti and
resistant Akron An. gambiae s.l.). For a mosquito that has entered the hut, with holed area
H (cm^2), insecticide content X (mg/m^2) and brand factor gamma (1 for PermaNet 2.0):
  logit P_att = b0,att + b1,att ln(H+1) + b2,att gamma ln(X+1) + b3,att gamma ln(X+1) ln(H+1)
  logit P_Bmu = b0,Bmu + b1,Bmu ln(H+1) + b2,Bmu gamma ln(X+1) + b3,Bmu gamma ln(X+1) ln(H+1)
  logit P_Cmu = b0,Cmu + b1,Cmu ln(H+1) + b2,Cmu gamma ln(X+1)      (b3,Cmu = 0, b0,Cmu = logit 0.01)
P_att: attacking (resulting in feeding and/or dying; Fig. S12 caption); P_Bmu: dying before
feeding among attackers; P_Cmu: dying after feeding. Entry (P_ent) does not depend on holes.
Per entered mosquito: repel = 1 - P_att; kill = P_att [P_Bmu + (1 - P_Bmu) P_Cmu];
fed and survived = P_att (1 - P_Bmu)(1 - P_Cmu).
Mapping onto the engine (explicit): Briet's absolute change in each outcome (repel, kill,
fed and survived; per entered mosquito, holed (X(t), H(t)) minus intact (X(t), 0)) is added to
the matching Churcher outcome (repeat r, die d, feed s; per attempt on a net user); the three
changes sum to zero, results are clipped at zero and renormalised; a new net (H = 0) is
unchanged. Absolute, not a ratio: Briet's intact-net feeding share is near zero, so a ratio
multiplies Churcher's larger, decay-driven feeding share many-fold and compounds the two decay
models (it gave 96% feeding at 2 years, against the paper's conclusion that holes act mainly
through attrition and non-use). X(t) = 55 mg/m^2 (PermaNet
2.0 target content, Additional file 2) times the Churcher insecticide-activity decay, so the
insecticide x hole interaction uses the content left in the net. Holed area: ln(H + 1)
grows linearly with net age to the seven cohorts' median mean ln(holed area) of 3.67 at 24
months (Table 3; "increased over time approximately exponentially for the first 3 years"),
capped at 3 years. Unverified (our choices): mixing the two strains linearly in bioassay
mortality (x = 1 susceptible, x = 0 the resistant strain), linear-in-time ln area, X = 55.
Dual-AI nets: the pyrethroid net above plus chlorfenapyr killing a share
cfp_kill exp(-ln2 t / cfp_half) of the mosquitoes that would otherwise feed or be repelled
(no published hut coefficient in the priors: cfp_kill is fitted to the pack).

IRS: deterrence irs_rep D(t) and killing of indoor-resting fed mosquitoes irs_kill D(t),
with a product-specific decay D(t) (D(0) = 1):
  pm_cs   pirimiphos-methyl CS (Actellic 300CS): logistic in time through the WHO cone
          bioassay mortality on sprayed walls in Protopopoff et al. 2018 Lancet
          10.1016/S0140-6736(18)30427-6 (99% at spraying, 82% at 9 months, 59% at 12 months)
  short   generic short-lived product: exp(-ln2 t / irs_half) (attempt 8 value, unverified)
"""
from __future__ import annotations

import sys

import numpy as np

LN2 = np.log(2.0)

# D6 prior values (canonical_value in expert_priors.yaml) plus the endophily assumption.
SPECIES = {
    "gambiae":    {"Q0": 0.92, "phi_B": 0.85, "phi_I": 0.90, "mu0": 0.132, "endophily": 0.80, "complex": "gambiae"},
    "arabiensis": {"Q0": 0.71, "phi_B": 0.80, "phi_I": 0.86, "mu0": 0.132, "endophily": 0.50, "complex": "gambiae"},
    "funestus":   {"Q0": 0.94, "phi_B": 0.78, "phi_I": 0.87, "mu0": 0.112, "endophily": 0.85, "complex": "funestus"},
}
NET_CODE = {"none": -1, "pyr": 0, "pbo": 1, "dual": 2}

# Churcher et al. 2016 eLife, Table 6 (fitted values).
CHURCHER = dict(tau=0.5, a1=0.63, a2=4.00, d1=0.071, d2=1.26, d3=1.52, th1=0.02, th2=3.32,
                mu=-2.36, rho=-3.05, k0=0.70, Hys=2.64, rM=0.24,
                pbo={"gambiae": (3.41, 5.88, 0.78), "funestus": (2.53, 0.89, 0.0)})
CHURCHER_SOURCE = "Churcher et al. 2016 eLife 10.7554/eLife.16090, Table 6 (PMC5025277)"

# Briet et al. 2020 Malar J, Additional file 2 Table S9 (PermaNet 2.0): (b0, b1 holes, b2 insecticide, b3 interaction).
BRIET = {
    "susceptible": {"att": (-2.555, 0.379, 1.807, -0.078), "Bmu": (-1.823, -0.235, 2.245, -0.094),
                    "Cmu": (float(np.log(0.01 / 0.99)), -0.370, 2.816, 0.0)},
    "resistant":   {"att": (-4.069, 0.541, 0.807, -0.101), "Bmu": (-4.840, -0.128, 2.325, -0.113),
                    "Cmu": (float(np.log(0.01 / 0.99)), -0.114, 1.303, 0.0)},
}
HOLES = dict(ln_area_24m=3.67, cap_years=3.0, X_new=55.0, H_max=192000.0, gamma=1.0)
HOLES_SOURCE = ("Briet et al. 2020 Malar J 10.1186/s12936-020-03383-6: main-text equations for P_att, P_Bmu, P_Cmu; "
                "Additional file 2 Table S9 (coefficients, H_max) and the PermaNet 2.0 target content 55 mg/m2; "
                "Table 3 (median of seven cohorts' mean ln holed area at 24 months)")


def expit_(z):
    return 1.0 / (1.0 + np.exp(-np.asarray(z, float)))


def hole_log_area(age_d):
    """ln(holed area + 1) of a net in use, by net age in days."""
    yrs = np.minimum(np.maximum(np.asarray(age_d, float), 0.0) / 365.0, HOLES["cap_years"])
    return HOLES["ln_area_24m"] / 2.0 * yrs


def briet_probs(X, lnH1, strain: str):
    """(P_att, P_Bmu, P_Cmu) from Briet 2020's equations; lnH1 = ln(H + 1), X in mg/m^2."""
    g = HOLES["gamma"] * np.log(np.asarray(X, float) + 1)
    out = []
    for k in ("att", "Bmu", "Cmu"):
        b0, b1, b2, b3 = BRIET[strain][k]
        out.append(expit_(b0 + b1 * lnH1 + b2 * g + b3 * g * lnH1))
    return tuple(out)


def briet_logit_slope_att(X, strain: str):
    """d logit(P_att) / d ln(H + 1) at content X: b1,att + b3,att gamma ln(X + 1)."""
    _, b1, _, b3 = BRIET[strain]["att"]
    return b1 + b3 * HOLES["gamma"] * float(np.log(X + 1))


def briet_outcomes(X, lnH1, strain: str):
    """Per entered mosquito: (repel, kill, fed and survived)."""
    a, b, c = briet_probs(X, lnH1, strain)
    return 1 - a, a * (b + (1 - b) * c), a * (1 - b) * (1 - c)


def briet_hole_deltas(X, lnH1, x):
    """Absolute change in (repel, kill, fed and survived) per entered mosquito from holes lnH1 vs
    none, at the same content X; strains mixed linearly in bioassay mortality x (x = 1
    susceptible, x = 0 resistant). The three changes sum to zero."""
    x = np.clip(np.asarray(x, float), 0.0, 1.0)
    out = []
    for s in ("susceptible", "resistant"):
        h = briet_outcomes(X, lnH1, s)
        z = briet_outcomes(X, 0.0 * np.asarray(lnH1, float), s)
        out.append([h[i] - z[i] for i in range(3)])
    return tuple(x * out[0][i] + (1 - x) * out[1][i] for i in range(3))


def apply_holes(r, d, s, x, age_d, dec, on=True):
    """Add Briet's absolute hole changes (per entered mosquito, at the net's remaining insecticide
    content X = 55 x dec) to the Churcher (repeat, die, feed) outcomes; clip at zero and renormalise.
    Absolute, not relative: Briet's intact-net feeding share is near zero, so a ratio would multiply
    Churcher's (larger, decay-driven) feeding share many-fold and compound the two decay models."""
    if not on:
        return r, d, s
    X = HOLES["X_new"] * np.asarray(dec, float)
    dr, dd, ds = briet_hole_deltas(X, hole_log_area(age_d), x)
    r2, d2, s2 = np.maximum(r + dr, 0), np.maximum(d + dd, 0), np.maximum(s + ds, 0)
    tot = r2 + d2 + s2
    return r2 / tot, d2 / tot, s2 / tot

# Protopopoff et al. 2018 Lancet: WHO cone bioassay mortality on walls sprayed with
# Actellic 300CS (days since spraying, mortality).
IRS_CONE = {"pm_cs": [(0.0, 0.99), (270.0, 0.82), (365.0, 0.59)]}
IRS_SOURCE = {"pm_cs": "Protopopoff et al. 2018 Lancet 10.1016/S0140-6736(18)30427-6 (PMC5910376), wall cone bioassays",
              "short": "attempt 8 exponential decay, irs_half (unverified)"}


def _logit(p):
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p))


def _expit(z):
    return 1.0 / (1.0 + np.exp(-z))


def _fit_cone(points):
    t = np.array([p[0] for p in points])
    y = _logit(np.array([p[1] for p in points]))
    b, a = np.polyfit(t, y, 1)
    return float(a), float(b)


IRS_DECAY = {k: _fit_cone(v) for k, v in IRS_CONE.items()}   # logit(m(t)) = a + b t


def species_mix(shares: dict | None) -> list:
    """[(name, share, bionomics)] with shares normalised; default all gambiae."""
    shares = {k: float(v) for k, v in (shares or {"gambiae": 1.0}).items() if float(v) > 0}
    for k in shares:
        if k not in SPECIES:
            raise KeyError(f"unknown vector species {k!r}; known: {sorted(SPECIES)}")
    tot = sum(shares.values())
    if tot <= 0:
        raise ValueError("species shares sum to zero")
    return [(k, v / tot, SPECIES[k]) for k, v in sorted(shares.items())]


def resistance_of(resistance, species: str) -> float:
    """Bioassay mortality for one species: a float applies to all species; a dict maps
    species (or 'default') to its value."""
    if isinstance(resistance, dict):
        if species in resistance:
            return float(resistance[species])
        if "default" in resistance:
            return float(resistance["default"])
        raise KeyError(f"resistance has no value for {species!r} and no 'default'")
    return float(resistance)


def pbo_bioassay(x, complex_: str = "gambiae"):
    """Churcher 2016 Equation 4 (MathML in PMC5025277): logit(f) = b1 + b2 (x - tau) / (1 + b3 (x - tau))."""
    C = CHURCHER
    b1, b2, b3 = C["pbo"][complex_]
    x = np.clip(np.asarray(x, float), 0.0, 1.0)
    return _expit(b1 + b2 * (x - C["tau"]) / (1 + b3 * (x - C["tau"])))


def hut_new_net(x, pbo: bool, complex_: str = "gambiae"):
    """Churcher 2016 per-attempt outcomes of a NEW net: (r0, d0, s0, gamma per day)."""
    C = CHURCHER
    x = np.clip(np.asarray(x, float), 0.0, 1.0)
    if pbo:
        x = pbo_bioassay(x, complex_)
    l = _expit(C["a1"] + C["a2"] * (x - C["tau"]))
    m = np.clip(1 - (C["d1"] + C["d2"] * (l - C["tau"]) + C["d3"] * (l - C["tau"]) ** 2), 0.0, 1.0)
    k = C["th1"] * np.exp(C["th2"] * (1 - l - C["tau"]))
    j = np.clip(1 - l - k, 0.0, 1.0)
    jp, kp, lp = m * j + (1 - m), m * k, m * l
    s0 = np.clip(kp / C["k0"], 0.0, 1.0)
    r0 = (1 - s0) * jp / (jp + lp)
    d0 = (1 - s0) * lp / (jp + lp)
    g = _expit(C["mu"] + C["rho"] * (l - C["tau"]))
    gs = _expit(C["mu"] + C["rho"] * (1 - C["tau"]))
    hy = (LN2 / g) / (LN2 / gs) * C["Hys"]            # half-life in years
    return r0, d0, s0, LN2 / (hy * 365.0)


def itn_effect(net_type, x, age_d, P, complex_: str = "gambiae", holes: bool = True):
    """Per-contact (repelled, killed, fed) for nets of `net_type` (codes in NET_CODE) against
    one species complex with bioassay mortality x, by net age in days (holes included unless
    holes=False)."""
    net_type = np.asarray(net_type)
    age_d = np.maximum(np.asarray(age_d, float), 0.0)
    out = []
    for pbo in (False, True):
        r0, d0, _, gam = hut_new_net(x, pbo, complex_)
        dec = np.exp(-gam * age_d)
        r = (r0 - CHURCHER["rM"]) * dec + CHURCHER["rM"]
        d = d0 * dec
        out.append((r, d, dec))
    (r_p, d_p, dec_p), (r_b, d_b, dec_b) = out
    is_pbo = net_type == 1
    r = np.where(is_pbo, r_b, r_p)
    d = np.where(is_pbo, d_b, d_p)
    dec = np.where(is_pbo, dec_b, dec_p)
    s = np.clip(1 - r - d, 0.0, 1.0)
    r, d, s = apply_holes(r, d, s, x, age_d, dec, holes and P.get("net_holes", 1.0) > 0)
    c = P["cfp_kill"] * np.exp(-LN2 * age_d / P["cfp_half"])
    dual = net_type == 2
    r = np.where(dual, r * (1 - c), r)
    s = np.where(dual, s * (1 - c), s)
    d = 1 - r - s
    return r, d, s


def irs_decay(age_d, product: str, P):
    """D(t) in [0, 1] for houses sprayed `age_d` days ago (negative age = not sprayed)."""
    age_d = np.asarray(age_d, float)
    on = age_d >= 0
    a = np.maximum(age_d, 0)
    if product == "short":
        dec = np.exp(-LN2 * a / P["irs_half"])
    elif product in IRS_DECAY:
        ia, ib = IRS_DECAY[product]
        dec = _expit(ia + ib * a) / _expit(ia)
    else:
        raise KeyError(f"unknown IRS product {product!r}; known: {sorted(IRS_DECAY) + ['short']}")
    return dec * on


def irs_effect(age_d, P, product: str = "pm_cs"):
    """(deter, kill) for houses sprayed `age_d` days ago."""
    dec = irs_decay(age_d, product, P)
    return P["irs_rep"] * dec, P["irs_kill"] * dec


def feeding(b, has_net, n_defl, n_kill, n_fed, irs_det, irs_kill, divert=0.0):
    """Per-person attempt outcomes for one species `b`: probability the attempt ends in a
    blood meal (y), in a blood meal the mosquito survives (w), or in a deflection that
    makes it retry (z). Arrays are per person."""
    N = has_net.astype(float)
    out, ind, bed = 1 - b["phi_I"], b["phi_I"] - b["phi_B"], b["phi_B"]
    enter = 1 - irs_det
    bed_fed = N * n_fed + (1 - N)
    fed_in = enter * (ind + bed * bed_fed)
    diverted = b["phi_I"] * irs_det * divert
    y = out + diverted + fed_in
    w = out + diverted + fed_in * (1 - b["endophily"] * irs_kill)
    z = b["phi_I"] * irs_det * (1 - divert) + enter * bed * N * n_defl
    return y, w, z


def cycle(b, w0, y, w, z, tau1=0.69, tau2=2.31):
    """Feeding-cycle summary for one species given person weights w0 (biting
    heterogeneity x age): (f, mu). Le Menach 2007 / Griffin 2010 form."""
    sw = w0.sum()
    Q0 = b["Q0"]
    W = 1 - Q0 + Q0 * (w0 * w).sum() / sw
    Z = Q0 * (w0 * z).sum() / sw
    f = 1.0 / (tau1 / (1 - Z) + tau2)
    p10, p2 = np.exp(-b["mu0"] * tau1), np.exp(-b["mu0"] * tau2)
    p1 = W * p10 / (1 - Z * p10)
    mu = -f * np.log(p1 * p2)
    return f, mu


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    P = dict(cfp_kill=0.4, cfp_half=900., irs_rep=0.2, irs_kill=0.6, irs_half=120.)
    mix = species_mix({"gambiae": 2, "arabiensis": 2})
    ok(abs(sum(s for _, s, _ in mix) - 1) < 1e-12 and mix[0][0] == "arabiensis", "V1 species shares are normalised")
    try:
        species_mix({"stephensi": 1})
        ok(False, "V2 an unknown species fails closed")
    except KeyError:
        ok(True, "V2 an unknown species fails closed")
    # Churcher 2016 values, checked by hand from Table 6 (x = 1: l = expit(2.63) = 0.933)
    r0, d0, s0, g = hut_new_net(1.0, False)
    ok(abs(d0 - 0.0925) < 2e-3 and abs(r0 - 0.907) < 2e-3 and s0 < 0.005,
       f"V3 Churcher: new pyrethroid net vs susceptible mosquitoes repels 0.907, kills 0.092 per attempt ({r0:.3f}, {d0:.3f})")
    ok(abs(LN2 / g / 365 - 2.16) < 0.02, f"V4 Churcher: insecticide half-life 2.16 y at x = 1 ({LN2 / g / 365:.2f})")
    r_, d_, s_, g_ = hut_new_net(0.088, False)
    ok(abs(d_ - 0.260) < 3e-3 and abs(s_ - 0.062) < 3e-3 and abs(LN2 / g_ / 365 - 0.33) < 0.02,
       f"V5 Churcher: at 8.8% bioassay mortality a pyrethroid net kills 0.26, lets 0.062 feed, half-life 0.33 y")
    rb, db, sb, gb = hut_new_net(0.088, True)
    ok(g_ / gb > 1.5 and sb < s_,
       f"V6 PBO lengthens insecticide half-life ({g_ / gb:.2f}x) and blocks more feeding under resistance (gambiae complex)")
    _, dbf, _, _ = hut_new_net(0.088, True, "funestus")
    ok(abs(dbf - db) > 0.03,"V7 PBO effect differs by species complex (separate Table 6 coefficients)")
    rs, ds, ss_ = itn_effect(np.array([0, 1, 2]), 0.088, np.array([0.0, 0.0, 0.0]), P)
    ok(np.allclose(rs + ds + ss_, 1) and ds[2] > ds[0] and ss_[2] < ss_[0], "V8 outcomes sum to one; dual-AI kills more, feeds fewer")
    _, d1, _ = itn_effect(np.array([0]), 0.95, np.array([730.0]), P, holes=False)
    _, d0n, _ = itn_effect(np.array([0]), 0.95, np.array([0.0]), P, holes=False)
    ok(abs(d1[0] / d0n[0] - 0.5 ** (2 / 2.07)) < 0.02, "V9 net killing decays with the Churcher half-life (holes off)")
    # IRS decay through the trial's cone points
    D = irs_decay(np.array([0.0, 270.0, 365.0, -1.0]), "pm_cs", P)
    ok(abs(D[0] - 1) < 1e-12 and abs(D[1] - 0.82 / 0.99) < 0.04 and abs(D[2] - 0.59 / 0.99) < 0.04 and D[3] == 0,
       f"V10 Actellic 300CS decay reproduces the trial's cone bioassays ({np.round(D, 3).tolist()})")
    Ds = irs_decay(np.array([270.0]), "short", P)
    ok(Ds[0] < 0.25 < D[1], "V11 the long-lasting product keeps far more effect at 9 months than the short one")
    try:
        irs_decay(np.array([1.0]), "nope", P)
        ok(False, "V12 an unknown IRS product fails closed")
    except KeyError:
        ok(True, "V12 an unknown IRS product fails closed")
    b = SPECIES["gambiae"]
    one, zero = np.ones(1, bool), np.zeros(1, bool)
    dS, kS = irs_effect(np.array([0.0]), P)
    rn, kn, fn = itn_effect(np.array([1]), 0.25, np.array([0.0]), P)
    _, w_n, _ = feeding(b, one, rn, kn, fn, np.zeros(1), np.zeros(1))
    _, w_ni, _ = feeding(b, one, rn, kn, fn, dS, kS)
    _, w_i, _ = feeding(b, zero, rn, kn, fn, dS, kS)
    _, w_0, _ = feeding(b, zero, rn, kn, fn, np.zeros(1), np.zeros(1))
    ok((w_n[0] - w_ni[0]) < (w_0[0] - w_i[0]), "V13 IRS removes fewer surviving blood meals where nets are used (redundancy)")
    y0, w0_, z0 = feeding(b, one, rn, kn, fn, dS, kS, divert=0.0)
    y1, w1_, z1 = feeding(b, one, rn, kn, fn, dS, kS, divert=1.0)
    ok(y1[0] > y0[0] and z1[0] < z0[0] and abs((y1 - y0)[0] - b["phi_I"] * dS[0]) < 1e-12,
       "V14 IRS-deterred mosquitoes diverted outdoors feed instead of retrying (and escape the net)")
    w0 = np.ones(4)
    f, mu = cycle(b, w0, np.ones(4), np.ones(4), np.zeros(4))
    ok(abs(mu - b["mu0"]) < 1e-9, "V15 without interventions the cycle reproduces baseline mortality")
    # Churcher 2016 Equation 4, against the paper: worked value and the published statements
    ok(abs(float(pbo_bioassay(0.088)) - 0.460) < 0.002,
       f"V21 Eq. 4 at x = 0.088 gives PBO bioassay mortality 0.460 ({float(pbo_bioassay(0.088)):.3f})")
    xs = np.linspace(0.0, 1.0, 101)
    gain = pbo_bioassay(xs) - xs
    ok(0.1 < xs[int(np.argmax(gain))] < 0.6,
       f"V22 A. gambiae complex: the PBO gain peaks at intermediate resistance (paper, Discussion; peak x = {xs[int(np.argmax(gain))]:.2f})")
    ok(float(pbo_bioassay(xs, "funestus").min()) > 0.85,
       "V23 A. funestus: PBO restores near-100% mortality at every resistance level (paper, Discussion)")
    # holes (Briet 2020), in isolation
    ok(hole_log_area(0.0) == 0 and abs(hole_log_area(730.0) - 3.67) < 1e-9 and hole_log_area(2000.0) == hole_log_area(3 * 365.0),
       "V17 holed area: none when new, Briet's 24-month median at 2 years, capped at 3 years")
    r_new, d_new, s_new = itn_effect(np.array([0]), 0.95, np.array([0.0]), P)
    r_n0, d_n0, s_n0 = itn_effect(np.array([0]), 0.95, np.array([0.0]), P, holes=False)
    ok(np.allclose([r_new, d_new, s_new], [r_n0, d_n0, s_n0]), "V18 a new net has no holes effect")
    r2y, d2y, s2y = itn_effect(np.array([0]), 0.95, np.array([730.0]), P)
    r2n, d2n, s2n = itn_effect(np.array([0]), 0.95, np.array([730.0]), P, holes=False)
    dec2 = np.exp(-hut_new_net(0.95, False)[3] * 730.0)
    dr_, dd2, ds_ = briet_hole_deltas(55.0 * dec2, 3.67, 0.95)
    ok(s2y[0] > s2n[0] and np.allclose([r2y, d2y, s2y], [r2n + dr_, d2n + dd2, s2n + ds_])
       and abs(r2y + d2y + s2y - 1)[0] < 1e-12,
       "V19 a 2-year-old net: Briet's absolute hole changes added to the Churcher outcomes (more feeding), sum to one")
    # Briet 2020 equations as published (main-text equation block; Additional file 2 Table S9)
    pa, _, _ = briet_probs(0.0, np.log(HOLES["H_max"] + 1), "susceptible")
    ok(abs(pa - expit_(-2.555 + 0.379 * np.log(192001))) < 1e-12 and abs(pa - 0.887) < 0.002,
       f"V20 P_att for an unprotected human (X = 0, H = H_max, as the paper substitutes) = {float(pa):.3f}")
    slope = briet_logit_slope_att(55.0, "susceptible")
    ok(abs(slope - (0.379 - 0.078 * np.log(56))) < 1e-12 and abs(slope - 0.065) < 0.001,
       f"V24 attack slope at 55 mg/m2 includes the insecticide x hole term: 0.379 - 0.078 ln(56) = {slope:.3f}")
    for strain in ("susceptible", "resistant"):
        a0, b0, c0 = briet_probs(55.0, 0.0, strain)
        a1, b1_, c1 = briet_probs(55.0, 3.67, strain)
        ok(a1 > a0 and b1_ < b0 and c1 < c0,
           f"V25 {strain}: holes raise attacks and lower pre- and post-prandial killing (paper: holed nets kill less)")
    one = briet_hole_deltas(55.0, 0.0, 0.95)
    ok(all(abs(float(v)) < 1e-12 for v in one) and abs(sum(float(v) for v in briet_hole_deltas(27.5, 3.67, 0.5))) < 1e-12,
       "V26 no holes: no change; the changes in repel, kill and feed sum to zero")
    # Briet 2020 conclusion: physical decay matters more through attrition and non-use than as a direct
    # loss of effect. At the cohorts' 24-month median holes, the direct change in feeding per attempt is small.
    # (their cohorts' insecticide content was stable: Additional file 2, "chemical stability")
    _, _, sh = itn_effect(np.array([0]), 0.95, np.array([730.0]), P)
    _, _, sn = itn_effect(np.array([0]), 0.95, np.array([730.0]), P, holes=False)
    ok(0 < sh[0] - sn[0] < 0.05,
       f"V27 a net that keeps its insecticide (x = 0.95): holes add {sh[0] - sn[0]:.3f} to feeding per attempt at 2 years (small, as the paper concludes)")
    # where the insecticide activity is gone (resistant population, Churcher half-life 0.33 y), holes act as on
    # Briet's untreated net: the change equals the X ~ 0 equations
    _, _, sh2 = itn_effect(np.array([0]), 0.088, np.array([730.0]), P)
    _, _, sn2 = itn_effect(np.array([0]), 0.088, np.array([730.0]), P, holes=False)
    _, _, ds0 = briet_hole_deltas(55.0 * np.exp(-hut_new_net(0.088, False)[3] * 730.0), 3.67, 0.088)
    ok(abs((sh2[0] - sn2[0]) - float(ds0)) < 0.01 and sh2[0] - sn2[0] > 0.05,
       f"V28 insecticide gone (x = 0.088, 2 years): holes add {sh2[0] - sn2[0]:.3f} to feeding, as Briet's near-untreated net")
    ok(resistance_of({"gambiae": 0.1, "default": 0.5}, "funestus") == 0.5 and resistance_of(0.3, "gambiae") == 0.3,
       "V16 resistance per species, with a default")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
