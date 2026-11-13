#!/usr/bin/env python3
"""Trial fit of the v0.17 net-effect parameters (design: ENGINE_NOTES.md, v0.17). Not yet run.

    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py plan [--with-b7b8]       # design, run count, cost
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py run --approved "<owner approval>" --workers W
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py run --smoke --out <scratch file> --workers 2
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py --self-test

`run` is the pack's joint fit (benchmark.fit_joint, rule joint-v1) over five parameters: the three net-effect
parameters and the two already fitted (cfp_kill, irs_kill), against the net and IRS trials. It writes
net_effect_fit.yaml beside this file, never fitted.yaml: adopting the result (the parameters' kind becomes `fitted`,
fitted.yaml and results.yaml are recomputed) is a separate engine version. The full run costs more than $20 and is
launched by the lead session after the owner approves; `--smoke` is a few short runs that check the script end to
end. Beyond joint-v1's own range (the region where every target is in band), the output gives each PBO trial's own
region and their union: B9/B9b (Muleba, durable PBO advantage) and B9c (Misungwi, none at 24 months) may not be
compatible, and a range for another setting must then cover both, not average them.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
import time

import numpy as np
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines import benchmark as BM  # noqa: E402

PACK = os.path.join(HERE, "pack.yaml")
OUT = os.path.join(HERE, "net_effect_fit.yaml")
PROTECTED = ("fitted.yaml", "results.yaml")
# Box of the design. net_kill_shift: Churcher 2016's 95% CrI of alpha1 (0.012-1.29) re-centred on 0.63, the card
# prior: the field trials can narrow the hut-trial range, not leave it (a target out of band everywhere in it is
# reported as infeasible, a principle-4 finding, not a reason to widen). pbo_gain_scale [0, 2] and pbo_adv_decay
# [0, 1.5] per year (half-life >= 0.46 y) are design assumptions with no source; a range at a box edge is reported.
# cfp_kill and irs_kill keep the pack's boxes.
BOX = {"net_kill_shift": [-0.618, 0.66], "pbo_gain_scale": [0.0, 2.0], "pbo_adv_decay": [0.0, 1.5],
       "cfp_kill": [0.0, 0.8], "irs_kill": [0.0, 0.95]}
TARGETS = ["B9", "B9b", "B9c", "B10", "B11", "B12", "B12b", "B12m"]
B7B8 = ["B7", "B8"]        # only after the Cochrane setting represents its trials (ENGINE_NOTES.md, v0.17)
GROUPS = {"muleba_pbo": ["B9", "B9b"], "misungwi_pbo": ["B9c"]}
DESIGN = {"n": 80, "seed": 17}        # 21 quadratic coefficients in 5 dimensions; about 4 points per coefficient
# Cost basis (measured 2026-11-12 on the laptop, arm64, one process, OMP_NUM_THREADS=2, v0.16 tree, 5000 agents,
# Muleba setting): a 50-year burn-in with the history to the fork and one arm 77 s CPU, with four arms 99.5 s, so
# about 70 s per (design point, setting, seed) group and 7.4 s per extra arm. VM_FACTOR 2-3: a VM vCPU against one
# laptop core (an assumption). USD per vCPU-hour: 1 Coiled credit x $0.061 + $0.048 Azure Dv5 list price (P6
# cluster records, cloud/vm_prices.yaml); bound: 1.5 credits and 1.25 margin as in vm_prices.yaml.
GROUP_S, ARM_S = 70.0, 7.4
VM_FACTOR = (2.0, 3.0)
USD_PER_VCPU_H = (1.0 * 0.061 + 0.048, 1.5 * 0.061 + 0.048 * 1.25)


def build_pack(base: dict, with_b7b8: bool = False, smoke: bool = False) -> dict:
    p = copy.deepcopy(base)
    p["fit_joint"] = {"rule": "joint-v1", "params": {k: list(v) for k, v in BOX.items()},
                      "targets": (B7B8 if with_b7b8 else []) + list(TARGETS), "design": dict(DESIGN)}
    if smoke:
        p["seeds"] = p["seeds"][:1]
        p["fit_joint"]["design"] = {"n": 3, "seed": DESIGN["seed"]}
        for s in p["settings"].values():
            s["params"] = dict(s.get("params") or {}, n_agents=600, burnin_years=2)
    return p


def plan(pack: dict) -> dict:
    fj, byid = pack["fit_joint"], {b["id"]: b for b in pack["benchmarks"]}
    arms = sorted({a for t in fj["targets"] for a in BM._arms_of(byid[t]["estimand"])})
    settings = sorted({pack["arms"][a]["setting"] for a in arms})
    npts, ns = int(fj["design"]["n"]) + 1, len(pack["seeds"])
    grid = sum(len(pack["settings"][s]["calibrate"]["grid"]) for s in settings if pack["settings"][s].get("calibrate"))
    cal = 2 * grid * min(2, ns)                         # at the box centre and at the chosen value; each its own burn-in
    groups, sims = (npts + 1) * len(settings) * ns, (npts + 1) * len(arms) * ns   # design points + verification
    cpu_h = (groups * GROUP_S + sims * ARM_S + cal * (GROUP_S + ARM_S)) / 3600
    vcpu = [cpu_h * f for f in VM_FACTOR]
    return {"params": fj["params"], "targets": fj["targets"], "arms": arms, "settings": settings,
            "design_points": npts, "seeds": ns, "arm_runs": sims + cal, "burn_in_groups": groups + cal,
            "laptop_cpu_hours": round(cpu_h, 1), "vm_vcpu_hours": [round(v, 1) for v in vcpu],
            "usd": [round(vcpu[0] * USD_PER_VCPU_H[0], 1), round(vcpu[1] * USD_PER_VCPU_H[1], 1)]}


def target_bands(pack: dict) -> dict:
    """{target: band} of the fit's targets (a benchmark outside the fit, such as a curve rule, may have no band)."""
    t = set(pack["fit_joint"]["targets"])
    return {b["id"]: b["band"] for b in pack["benchmarks"] if b["id"] in t}


def regions(X: np.ndarray, Y: dict, bands: dict, groups: dict, names: list, box: np.ndarray, seed: int) -> dict:
    """Each target's quadratic surrogate (as joint-v1) on dense candidates; the hull of the region where the common
    targets and one group's targets are in band, for each group, for all at once, and for their union."""
    cand = BM._candidates(X.shape[1], seed)
    Fc = BM._quad_features(cand)
    inb = {}
    for t, y in Y.items():
        coef, _ = BM._quad_fit(X, np.log(np.maximum(np.asarray(y, float), 1e-12)))
        v = np.exp(Fc @ coef)
        inb[t] = (v >= bands[t][0]) & (v <= bands[t][1])
    grouped = {t for g in groups.values() for t in g}
    common = np.logical_and.reduce([inb[t] for t in Y if t not in grouped] or [np.ones(len(cand), bool)])
    masks = {g: common & np.logical_and.reduce([inb[t] for t in ts]) for g, ts in groups.items()}
    masks["all"] = np.logical_and.reduce([masks[g] for g in groups])
    masks["union"] = np.logical_or.reduce([masks[g] for g in groups])

    def hull(m):
        if not m.any():
            return None
        return {k: [round(float(box[i, 0] + cand[m, i].min() * (box[i, 1] - box[i, 0])), 4),
                    round(float(box[i, 0] + cand[m, i].max() * (box[i, 1] - box[i, 0])), 4)] for i, k in enumerate(names)}
    out = {g: {"share_of_box": round(float(m.mean()), 4), "hull": hull(m)} for g, m in masks.items()}
    out["groups_compatible"] = bool(masks["all"].any())
    # a hull on an edge the design chose (not a natural bound: 0 for the scale, the decay and the kills) is reported
    edge = set()
    for g in ("union", "all"):
        for i, k in enumerate(names):
            if out[g]["hull"]:
                lo, hi = out[g]["hull"][k]
                tol = 1e-3 * (box[i, 1] - box[i, 0])
                if hi >= box[i, 1] - tol or (box[i, 0] != 0.0 and lo <= box[i, 0] + tol):
                    edge.add(k)
    out["at_box_edge"] = sorted(edge)
    return out


def run(pack: dict, workers: int, out_path: str, approved: str | None, smoke: bool) -> int:
    if os.path.basename(out_path) in PROTECTED:
        raise ValueError(f"net_effect_fit writes its own file, never {PROTECTED}")
    if not smoke and not (approved or "").strip():
        raise ValueError("the full fit spends more than $20: pass --approved with the owner's approval (or --smoke)")
    eng = BM.load_engine(pack["engine"], None)
    runner = BM.Runner(pack, eng, workers, pack["seeds"], mode="fit", evidence=BM.load_evidence(eng, pack=pack))
    t0 = time.time()
    fit = BM.fit_joint(pack, eng, runner)
    names = list(pack["fit_joint"]["params"])
    box = np.array([pack["fit_joint"]["params"][k] for k in names], float)
    X = np.array([[(th[k] - box[i, 0]) / (box[i, 1] - box[i, 0]) for i, k in enumerate(names)]
                  for th in fit["design"]["x"]])
    bands = target_bands(pack)
    reg = regions(X, fit["design"]["computed"], bands, GROUPS, names, box, int(pack["fit_joint"]["design"]["seed"]))
    doc = BM.stamp({"purpose": "trial fit of the v0.17 net-effect parameters (ENGINE_NOTES.md, v0.17); not adopted",
                    "smoke": smoke, "approved": approved, "plan": plan(pack), "n_sims": runner.nsims,
                    "wall_s": round(time.time() - t0, 1), "fitted_at": time.strftime("%F %T"),
                    "trial_regions": reg, **fit}, eng)
    yaml.safe_dump(doc, open(out_path, "w"), sort_keys=False)
    print(f"wrote {out_path} ({runner.nsims} sims); groups compatible: {reg['groups_compatible']}")
    return 0


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    base = yaml.safe_load(open(PACK))
    p = build_pack(base)
    byid = {b["id"]: b for b in p["benchmarks"]}
    arms_ok = all(a in p["arms"] for t in p["fit_joint"]["targets"] for a in BM._arms_of(byid[t]["estimand"]))
    ok(set(p["fit_joint"]["targets"]) == set(TARGETS) and arms_ok and not set(B7B8) & set(p["fit_joint"]["targets"])
       and base.get("fit_joint", {}).get("params") != p["fit_joint"]["params"],
       "F1 the design targets the net and IRS trials (B7/B8 held out), every target's arms exist, the pack is not changed")
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.spec import priors as PR
    e = MalariaEngine()
    sp = {q.name: q.value for q in e.parameters()}
    pr = PR.load_card(e)["priors"]["net_kill_shift"]
    ok(all(BOX[k][0] <= sp[k] <= BOX[k][1] for k in BOX) and [pr.lo, pr.hi] == BOX["net_kill_shift"],
       "F2 the box holds every current value; the kill shift's box is its card prior (Churcher 2016 alpha1 CrI)")
    q = plan(p)
    ok(q["design_points"] == 81 and q["seeds"] == 8 and len(q["arms"]) == 10 and q["settings"] ==
       ["benin", "irs_added", "misungwi", "muleba"] and q["arm_runs"] == 82 * 10 * 8 + 2 * 21 * 2 and q["usd"][0] > 0,
       f"F3 plan: 81 design points x 10 arms x 8 seeds, 4 settings; {q['arm_runs']} arm runs, "
       f"{q['vm_vcpu_hours']} vCPU-h, ${q['usd'][0]}-{q['usd'][1]}")
    q2 = plan(build_pack(base, with_b7b8=True))
    ok("cochrane_itn" in q2["settings"] and q2["usd"][1] > q["usd"][1], "F4 with B7/B8 the Cochrane setting joins and costs more")
    # F5: two trials that cannot both be in band: target A in band only for theta < 0.4, B (Misungwi) only above 0.6
    names, box = ["pbo_adv_decay"], np.array([[0.0, 1.5]])
    X = np.linspace(0, 1, 21)[:, None]
    Y = {"B9": np.exp(X[:, 0]), "B9c": np.exp(-X[:, 0]), "B10": np.full(21, 1.0)}
    bands = {"B9": [0.5, np.exp(0.4)], "B9c": [0.1, np.exp(-0.6)], "B10": [0.5, 2.0]}
    r = regions(X, Y, bands, {"muleba_pbo": ["B9"], "misungwi_pbo": ["B9c"]}, names, box, 1)
    hu, hm, hs = r["union"]["hull"]["pbo_adv_decay"], r["muleba_pbo"]["hull"], r["misungwi_pbo"]["hull"]
    ok(not r["groups_compatible"] and r["all"]["hull"] is None and hm["pbo_adv_decay"][1] < 0.61
       and hs["pbo_adv_decay"][0] > 0.89 and hu[0] < 0.01 and hu[1] > 1.49,
       f"F5 incompatible trials: no common region; each trial's region is reported and the range is their union {hu}")
    for bad, kw in (("fitted.yaml", {"approved": "x", "smoke": False}), ("x.yaml", {"approved": None, "smoke": False})):
        try:
            run(p, 1, os.path.join(HERE, bad), **kw)
            ok(False, f"F6 refused: {bad} {kw}")
        except ValueError:
            ok(True, f"F6 refused before any run: {'writing ' + bad if bad != 'x.yaml' else 'a full fit without --approved'}")
    tb = target_bands(p)
    ok(set(tb) == set(TARGETS) and all(len(v) == 2 for v in tb.values()),
       "F8 the region step reads the bands of the fit's targets only (B16c has none; the smoke's first failure)")
    s = build_pack(base, smoke=True)
    ok(s["seeds"] == base["seeds"][:1] and s["fit_joint"]["design"]["n"] == 3
       and all(v["params"]["n_agents"] == 600 for v in s["settings"].values()),
       "F7 --smoke: one seed, 3 design points, 600 agents, 2-year burn-in")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    if (argv if argv is not None else sys.argv[1:])[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["plan", "run"])
    ap.add_argument("--with-b7b8", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--approved", default=None)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    pack = build_pack(yaml.safe_load(open(PACK)), a.with_b7b8, a.smoke)
    if a.action == "plan":
        print(yaml.safe_dump(plan(pack), sort_keys=False))
        return 0
    if a.smoke and not a.out:
        print("--smoke needs --out (a scratch file): a smoke is not evidence")
        return 2
    return run(pack, a.workers, a.out or OUT, a.approved, a.smoke)


if __name__ == "__main__":
    sys.exit(main())
