#!/usr/bin/env python3
"""Species-aware vector bionomics and ITN / IRS mechanics (improvement plan D6, D7).

    python3 toolkit/engines/malaria/vector.py --self-test

Bionomics per species (D6 prior `malaria_vector_bionomics_by_species` in
.claude/orchestration/expert_priors.yaml): anthropophagy Q0, share of bites indoors
φ_I and in bed φ_B, adult daily mortality μ0. Values are the malariasimulation presets
(R/vector_parameters.R, commit 5bc9028; φ from Sherrard-Smith et al. 2019 PNAS
10.1073/pnas.1820646116; Q0 from Griffin et al. 2010 PLoS Med 10.1371/journal.pmed.1000324).
Endophily (share of indoor-fed mosquitoes that rest indoors on a sprayable surface) is
NOT in that prior: the values below are assumptions, ordered as in Sinka et al. 2010
(Parasit Vectors 10.1186/1756-3305-3-117; An. arabiensis the most exophilic), and are
flagged `unverified` in the engine card.

One feeding attempt on a person, by where it happens:
  outdoors (1 - φ_I)              untouched by nets and IRS
  indoors, not in bed (φ_I - φ_B) IRS deterrence only
  indoors, in bed (φ_B)           IRS deterrence, then the net if the person uses one
A mosquito deterred by IRS or repelled / blocked by a net survives and retries (it
lengthens the feeding cycle). A mosquito killed by the net never feeds. A mosquito that
feeds indoors rests indoors with probability `endophily`, and is then killed by IRS with
probability d_S. So the two interventions share their target (indoor, endophilic
feeders): ITN×IRS redundancy comes from the mechanics, with no interaction factor.

Net effect (per contact in bed) from net type, pyrethroid bioassay mortality m (1 =
susceptible) and net age a (days):
  m_eff   = m for pyrethroid and dual-AI nets; PBO restores susceptibility:
            m + (1 - m) * pbo_restore * exp(-ln2 a / pbo_half)
  kill    = itn_kill_max * m_eff^itn_kexp * exp(-ln2 a / ins_half); dual-AI adds an
            independent chlorfenapyr kill cfp_kill * exp(-ln2 a / cfp_half)
  repel   = itn_rep_max * (0.3 + 0.7 m_eff) * exp(-ln2 a / ins_half)   (excito-repellency)
  block   = itn_block * exp(-ln2 a / block_half)  of the surviving contacts cannot feed
  fed     = (1 - kill - repel) * (1 - block); deflected (retry) = 1 - kill - fed
IRS effect from spray age a: deter = irs_rep * exp(-ln2 a / irs_half),
kill = irs_kill * exp(-ln2 a / irs_half).
"""
from __future__ import annotations

import sys

import numpy as np

LN2 = np.log(2.0)

# D6 prior values (canonical_value in expert_priors.yaml) plus the endophily assumption.
SPECIES = {
    "gambiae":    {"Q0": 0.92, "phi_B": 0.85, "phi_I": 0.90, "mu0": 0.132, "endophily": 0.80},
    "arabiensis": {"Q0": 0.71, "phi_B": 0.80, "phi_I": 0.86, "mu0": 0.132, "endophily": 0.50},
    "funestus":   {"Q0": 0.94, "phi_B": 0.78, "phi_I": 0.87, "mu0": 0.112, "endophily": 0.85},
}
NET_CODE = {"none": -1, "pyr": 0, "pbo": 1, "dual": 2}


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


def itn_effect(net_type, m, age_d, P):
    """Per-contact (deflected, killed, fed) for nets of `net_type` (codes in NET_CODE),
    resistance proxy m (bioassay mortality) and age in days; P holds engine parameters."""
    net_type = np.asarray(net_type)
    age_d = np.maximum(np.asarray(age_d, float), 0.0)
    m = np.clip(np.asarray(m, float), 0.0, 1.0)
    ins = np.exp(-LN2 * age_d / P["ins_half"])
    m_eff = np.where(net_type == 1, m + (1 - m) * P["pbo_restore"] * np.exp(-LN2 * age_d / P["pbo_half"]), m)
    kill = P["itn_kill_max"] * m_eff ** P["itn_kexp"] * ins
    cfp = P["cfp_kill"] * np.exp(-LN2 * age_d / P["cfp_half"])
    kill = np.where(net_type == 2, 1 - (1 - kill) * (1 - cfp), kill)
    kill = np.clip(kill, 0.0, 0.98)
    repel = np.minimum(P["itn_rep_max"] * (0.3 + 0.7 * m_eff) * ins, 1 - kill)
    block = P["itn_block"] * np.exp(-LN2 * age_d / P["block_half"])
    fed = np.clip(1 - kill - repel, 0, 1) * (1 - block)
    return 1 - kill - fed, kill, fed


def irs_effect(age_d, P):
    """(deter, kill) for houses sprayed `age_d` days ago (negative age = not sprayed)."""
    age_d = np.asarray(age_d, float)
    on = age_d >= 0
    dec = np.exp(-LN2 * np.maximum(age_d, 0) / P["irs_half"]) * on
    return P["irs_rep"] * dec, P["irs_kill"] * dec


def feeding(b, has_net, n_defl, n_kill, n_fed, irs_det, irs_kill):
    """Per-person attempt outcomes for one species `b`: probability the attempt ends in a
    blood meal (y), in a blood meal the mosquito survives (w), or in a deflection that
    makes it retry (z). Arrays are per person."""
    N = has_net.astype(float)
    out, ind, bed = 1 - b["phi_I"], b["phi_I"] - b["phi_B"], b["phi_B"]
    enter = 1 - irs_det
    bed_fed = N * n_fed + (1 - N)
    fed_in = enter * (ind + bed * bed_fed)
    y = out + fed_in
    w = out + fed_in * (1 - b["endophily"] * irs_kill)
    z = b["phi_I"] * irs_det + enter * bed * N * n_defl
    return y, w, z


def cycle(b, w0, y, w, z, tau1=0.69, tau2=2.31):
    """Feeding-cycle summary for one species given person weights w0 (biting
    heterogeneity x age): (f, mu, mean y). Le Menach 2007 / Griffin 2010 form."""
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

    P = dict(itn_kill_max=0.5, itn_kexp=2.0, itn_rep_max=0.1, itn_block=0.5, block_half=900., ins_half=700.,
             pbo_restore=0.6, pbo_half=700., cfp_kill=0.4, cfp_half=900., irs_rep=0.2, irs_kill=0.6, irs_half=120.)
    mix = species_mix({"gambiae": 2, "arabiensis": 2})
    ok(abs(sum(s for _, s, _ in mix) - 1) < 1e-12 and mix[0][0] == "arabiensis", "V1 species shares are normalised")
    try:
        species_mix({"stephensi": 1})
        ok(False, "V2 an unknown species fails closed")
    except KeyError:
        ok(True, "V2 an unknown species fails closed")
    d0, k0, f0 = itn_effect(np.array([0, 1, 2]), 0.25, 0.0, P)
    ok(k0[1] > k0[0] and k0[2] > k0[0], "V3 under resistance, PBO and dual-AI nets kill more than pyrethroid nets")
    ok(np.allclose(d0 + k0 + f0, 1), "V4 net outcomes (deflected, killed, fed) sum to one")
    _, ks, _ = itn_effect(np.array([0, 1]), 1.0, 0.0, P)
    ok(abs(ks[0] - ks[1]) < 1e-12, "V5 PBO adds nothing against susceptible mosquitoes")
    _, kold, _ = itn_effect(np.array([0]), 1.0, 1500.0, P)
    ok(kold[0] < ks[0], "V6 net killing decays with net age")
    b = SPECIES["gambiae"]
    one, zero = np.ones(1, bool), np.zeros(1, bool)
    dS, kS = irs_effect(np.array([0.0]), P)
    _, kn, fn = itn_effect(np.array([1]), 0.25, 0.0, P)
    dn = 1 - kn - fn
    # kill per attempt among in-bed attempts with the net, with and without IRS deterrence
    y_n, w_n, _ = feeding(b, one, dn, kn, fn, np.zeros(1), np.zeros(1))
    y_ni, w_ni, _ = feeding(b, one, dn, kn, fn, dS, kS)
    y_i, w_i, _ = feeding(b, zero, dn, kn, fn, dS, kS)
    y_0, w_0, _ = feeding(b, zero, dn, kn, fn, np.zeros(1), np.zeros(1))
    surv = lambda w: float(w[0])  # noqa: E731
    # redundancy: the IRS-attributable drop in surviving meals is smaller when nets are used
    ok((surv(w_n) - surv(w_ni)) < (surv(w_0) - surv(w_i)),
       "V7 IRS removes fewer surviving blood meals where nets are used (mechanistic redundancy)")
    arab = SPECIES["arabiensis"]
    _, wa, _ = feeding(arab, zero, dn, kn, fn, dS, kS)
    _, wg, _ = feeding(b, zero, dn, kn, fn, dS, kS)
    ok(wa[0] > wg[0], "V8 IRS kills a smaller share of meals for the more exophilic, outdoor-biting species")
    w0 = np.ones(4)
    f, mu = cycle(b, w0, np.ones(4), np.ones(4), np.zeros(4))
    ok(abs(mu - b["mu0"]) < 1e-9, "V9 without interventions the cycle reproduces baseline mortality")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
