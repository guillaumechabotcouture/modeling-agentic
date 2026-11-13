#!/usr/bin/env python3
"""Trial fit of the v0.17 net-effect parameters on the pack's training targets (design: ENGINE_NOTES.md, v0.17).
Not yet run.

    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py plan                 # design, run count, cost
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py run --approved "<owner approval>" --workers W
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py run --smoke --out <scratch file> --workers 2
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py --self-test

Train and held out. The targets are exactly the pack's own fit targets (`fit_joint.targets` in pack.yaml: B10, B11,
B12, B12b, B12m); every other benchmark is held out and stays untouched: the fit never scores one, its objective and
its ranges use only the targets, and the script refuses a target list that differs from the pack's (a held-out
benchmark used to choose a value would no longer test the engine; toolkit/spec/engine_parameters.py refuses such
values). The parameters fitted are those the training arms can identify: a net-effect parameter joins only when a
training arm uses a net it acts on, so net_kill_shift (every net type) joins cfp_kill, irs_kill and cfp_half (owner,
13 Nov), while pbo_gain_scale and pbo_adv_decay stay out (no training arm has a PBO net; the PBO trials B9, B9b, B9c
are held out; their ranges are card priors from trials outside the pack).

`run` is the pack's joint fit (benchmark.fit_joint, rule joint-v1) on a RecalRunner: net_kill_shift moves the sites'
pyrethroid-only calibration arms, so every parameter point is calibrated to the site's control-arm baseline, by
log-linear interpolation of m0 on a grid in net_kill_shift (the only fitted parameter those arms depend on). Results
are cached under their calibration. Besides joint-v1's value and marginal range it exports a JOINT sample of
trial-compatible parameter sets (the surrogate's all-in-band region; hull and edge flags from every accepted
candidate), with `verify` of them re-run on the engine at their own calibration, each with its control-arm check:
one out of band invalidates the sample and the run exits 2. It writes net_effect_fit.yaml beside this file, never
fitted.yaml or results.yaml. A full run needs --approved and a plan under the $25 cap; the lead launches it after
this PR is merged. `--smoke` checks the script end to end with a few short runs and writes only to its scratch file.
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
# The net-effect parameters, the net types each acts on, and their design box. net_kill_shift: Churcher 2016's 95%
# CrI of alpha1 (0.012-1.29) re-centred on 0.63 (the card prior): the field trials may narrow the hut-trial range, not
# leave it (a target out of band everywhere in it is reported infeasible, a principle-4 finding, not a reason to
# widen). The PBO boxes are design assumptions with no source, used only once a PBO trial is a training target.
NET_PARAMS = {"net_kill_shift": ({"pyr", "pbo", "dual"}, [-0.618, 0.66]),
              "pbo_gain_scale": ({"pbo"}, [0.0, 2.0]),
              "pbo_adv_decay": ({"pbo"}, [0.0, 1.5])}
# owner, 13 Nov: cfp_half joins the fit. Box 1.5-3.5 years (547.5-1277.5 days): attempt 8's range, a modeller reading
# of the dual-AI trials' durability (priors.yaml: no published half-life), a design assumption, not a prior
FIT_EXTRA = {"cfp_half": [547.5, 1277.5]}
CAL_PARAM = "net_kill_shift"       # the only fitted parameter the sites' calibration arms depend on (F9)
CAL_N = 5                          # calibration grid points in net_kill_shift across its box
DESIGN = {"n": 45, "seed": 17}     # 15 quadratic coefficients in 4 dimensions; 3 points per coefficient
VERIFY = 12                        # joint-sample points re-run on the engine, each at its own calibration
SAMPLE = 2000                      # joint-sample points exported (surrogate all-in-band region)
CAP_USD = 25.0                     # owner, 13 Nov: hard cap for the fit; `run` refuses a plan above it
# Cost basis (measured 2026-11-12 on the laptop, arm64, one process, OMP_NUM_THREADS=2, v0.16 tree, 5000 agents,
# Muleba setting): a 50-year burn-in with the history to the fork and one arm 77 s CPU, with four arms 99.5 s, so
# about 70 s per (parameter point, setting, seed) group and 7.4 s per extra arm. VM_FACTOR 2-3: a VM vCPU against one
# laptop core (an assumption). USD per vCPU-hour: 1 Coiled credit x $0.061 + $0.048 Azure Dv5 list price (P6
# cluster records, cloud/vm_prices.yaml); bound: 1.5 credits and 1.25 margin as in vm_prices.yaml.
GROUP_S, ARM_S = 70.0, 7.4
VM_FACTOR = (2.0, 3.0)
USD_PER_VCPU_H = (1.0 * 0.061 + 0.048, 1.5 * 0.061 + 0.048 * 1.25)
ACTS = {"irs": {"irs_kill"}, "dual": {"cfp_kill", "cfp_half"}}      # fitted parameters by package element


def split(pack: dict) -> tuple[list, list]:
    """(training targets, held-out ids): the pack's own fit targets and every other benchmark."""
    train = list((pack.get("fit_joint") or {}).get("targets") or [])
    return train, [b["id"] for b in pack["benchmarks"] if b["id"] not in train]


def train_nets(pack: dict, train: list) -> set:
    byid = {b["id"]: b for b in pack["benchmarks"]}
    arms = {a for t in train for a in BM._arms_of(byid[t]["estimand"])}
    return {str((pack["arms"][a].get("package") or {}).get("net")) for a in arms} - {"None"}


def build_pack(base: dict, smoke: bool = False) -> dict:
    """The pack with its own targets and a joint fit over its fitted parameters, cfp_half, and every net-effect
    parameter that a training arm identifies. Benchmarks, settings and targets are the pack's (smoke: small runs)."""
    p = copy.deepcopy(base)
    train, _ = split(base)
    nets = train_nets(base, train)
    params = {k: list(b) for k, (acts, b) in NET_PARAMS.items() if acts & nets}
    params.update({k: list(v) for k, v in base["fit_joint"]["params"].items()})
    params.update({k: list(v) for k, v in FIT_EXTRA.items()})
    p["fit_joint"] = {"rule": "joint-v1", "params": params, "targets": train, "design": dict(DESIGN)}
    if smoke:
        p["seeds"] = p["seeds"][:1]
        p["fit_joint"]["design"] = {"n": 3, "seed": DESIGN["seed"]}
        for s in p["settings"].values():
            s["params"] = dict(s.get("params") or {}, n_agents=600, burnin_years=2)
    return p


def check(pack: dict, base: dict) -> list[str]:
    """Why `pack` may not be fitted: a target that is not one of the base pack's fit targets (held out)."""
    train, held = split(base)
    t = list(pack["fit_joint"]["targets"])
    bad = [f"{x} is held out in pack.yaml (not one of its fit targets {train}): it may not be a target" for x in t
           if x in held]
    return bad + ([f"targets {t} differ from the pack's fit targets {train}"] if sorted(t) != sorted(train) else [])


def _fit_settings(pack: dict) -> list:
    byid = {b["id"]: b for b in pack["benchmarks"]}
    arms = {a for t in pack["fit_joint"]["targets"] for a in BM._arms_of(byid[t]["estimand"])}
    return sorted({pack["arms"][a]["setting"] for a in arms})


def calibration_params(pack: dict) -> set:
    """The fitted parameters that the calibration arms of the fit's sites depend on (from their packages)."""
    out = set()
    for s in _fit_settings(pack):
        pk = (pack["settings"][s].get("calibrate") or {}).get("arm_package") or {}
        net = pk.get("net")
        out |= {k for k, (acts, _) in NET_PARAMS.items() if net in acts}
        out |= ACTS["dual"] if net == "dual" else set()
        out |= ACTS["irs"] if pk.get("irs") else set()
    return out & set(pack["fit_joint"]["params"])


def interp_calibration(grid, values, v: float) -> float:
    """Log-linear interpolation of a calibrated attribute (m0) on the calibration grid of CAL_PARAM."""
    return float(np.exp(np.interp(float(v), np.asarray(grid, float), np.log(np.asarray(values, float)))))


class RecalRunner(BM.Runner):
    """benchmark.Runner with the sites calibrated at every parameter point (review P2). Each site's calibration arm
    depends only on CAL_PARAM, so the sites are calibrated once on a grid of CAL_PARAM and each point's m0 is the
    log-linear interpolation at its value. Results are cached under the calibration they were run with."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.cal_grid = None          # {"values": [...], "m0": {setting: [...]}, "rows": {setting: calibrated row}}

    def calibrate_grid(self, settings, centre: dict, n: int):
        lo, hi = self._p["fit_joint"]["params"][CAL_PARAM]
        vals = [float(x) for x in np.linspace(lo, hi, n)]
        m0, rows = {s: [] for s in settings}, {s: [] for s in settings}
        for v in vals:
            self.calibrated = {}
            BM.Runner.calibrate_settings(self, dict(centre, **{CAL_PARAM: v}), settings)
            for s in settings:
                m0[s].append(self.calibrated[s]["value"])
                rows[s].append(self.calibrated[s])
        self.cal_grid = {"values": vals, "m0": m0, "rows": rows}

    def set_calibration(self, values: dict):
        for s, v in values.items():
            row = dict(self.calibrated.get(s) or {"attribute": self._p["settings"][s]["calibrate"]["attribute"]})
            self.calibrated[s] = dict(row, value=float(v))

    def calibrate_settings(self, fitted: dict, names=None):
        if self.cal_grid is None:
            return BM.Runner.calibrate_settings(self, fitted, names)
        g = self.cal_grid
        v = float(dict(self._fitted, **fitted).get(CAL_PARAM, 0.0))
        self.set_calibration({s: interp_calibration(g["values"], g["m0"][s], v) for s in g["m0"]})

    def _calkey(self):
        return tuple(sorted((s, round(float(c["value"]), 9)) for s, c in self.calibrated.items()))

    def prefetch(self, arms, fitted_list):
        jobs, need = [], []
        for f in fitted_list:
            if self.cal_grid is not None:
                self.calibrate_settings(f)
            key = (tuple(sorted(f.items())), self._calkey())
            for a in arms:
                if (a, key) in self.cache or (a, key) in need:
                    continue
                jobs += self._arm_jobs(a, f)
                need.append((a, key))
        if jobs:
            res = self._map(jobs)
            self.nsims += len(jobs)
            k = len(self.seeds)
            for i, ak in enumerate(need):
                self.cache[ak] = res[i * k:(i + 1) * k]

    def arm_results(self, arms, fitted: dict):
        self.prefetch(arms, [fitted])
        key = (tuple(sorted(fitted.items())), self._calkey())
        return {a: self.cache[(a, key)] for a in arms}


def plan(pack: dict, verify: int = VERIFY, cal_n: int = CAL_N) -> dict:
    fj, byid = pack["fit_joint"], {b["id"]: b for b in pack["benchmarks"]}
    arms = sorted({a for t in fj["targets"] for a in BM._arms_of(byid[t]["estimand"])})
    settings = _fit_settings(pack)
    npts, ns = int(fj["design"]["n"]) + 1, len(pack["seeds"])
    grid = sum(len(pack["settings"][s]["calibrate"]["grid"]) for s in settings if pack["settings"][s].get("calibrate"))
    cal = cal_n * grid * min(2, ns)                 # the calibration grid in CAL_PARAM; each run its own burn-in
    pts = npts + 1 + verify                         # design, the chosen value, the verified joint-sample points
    groups, sims = pts * len(settings) * ns, pts * len(arms) * ns
    cpu_h = (groups * GROUP_S + sims * ARM_S + cal * (GROUP_S + ARM_S)) / 3600
    vcpu = [cpu_h * f for f in VM_FACTOR]
    _, held = split(pack)
    return {"params": fj["params"], "targets_train": fj["targets"], "held_out_untouched": held,
            "not_fitted": sorted(set(NET_PARAMS) - set(fj["params"])), "calibration_depends_on": sorted(
                calibration_params(pack)), "arms": arms, "settings": settings,
            "design_points": npts, "verified_sample_points": verify, "seeds": ns, "calibration_runs": cal,
            "arm_runs": sims + cal, "burn_in_groups": groups + cal, "laptop_cpu_hours": round(cpu_h, 1),
            "vm_vcpu_hours": [round(v, 1) for v in vcpu],
            "usd": [round(vcpu[0] * USD_PER_VCPU_H[0], 1), round(vcpu[1] * USD_PER_VCPU_H[1], 1)]}


def target_bands(pack: dict) -> dict:
    """{target: band} of the fit's targets (a held-out benchmark, such as a curve rule, may have no band)."""
    t = set(pack["fit_joint"]["targets"])
    return {b["id"]: b["band"] for b in pack["benchmarks"] if b["id"] in t}


def joint_region(X: np.ndarray, Y: dict, bands: dict, names: list, box: np.ndarray, seed: int,
                 n_sample: int = SAMPLE) -> dict:
    """Each target's quadratic surrogate (as joint-v1) on dense candidates; the region where every target is in band:
    its share of the box, its marginal hull and box-edge flags (from every accepted candidate), and a joint sample of
    its points (parameter sets, not marginals)."""
    cand = BM._candidates(X.shape[1], seed)
    Fc = BM._quad_features(cand)
    m = np.ones(len(cand), bool)
    for t, y in Y.items():
        coef, _ = BM._quad_fit(X, np.log(np.maximum(np.asarray(y, float), 1e-12)))
        v = np.exp(Fc @ coef)
        m &= (v >= bands[t][0]) & (v <= bands[t][1])
    to = lambda c: {k: round(float(box[i, 0] + c[i] * (box[i, 1] - box[i, 0])), 6) for i, k in enumerate(names)}  # noqa
    acc = cand[m]
    hull = ({k: [round(float(box[i, 0] + acc[:, i].min() * (box[i, 1] - box[i, 0])), 6),
                 round(float(box[i, 0] + acc[:, i].max() * (box[i, 1] - box[i, 0])), 6)] for i, k in enumerate(names)}
            if len(acc) else None)
    edge = sorted(k for i, k in enumerate(names) if hull and (
        hull[k][1] >= box[i, 1] - 1e-3 * (box[i, 1] - box[i, 0])
        or (box[i, 0] != 0.0 and hull[k][0] <= box[i, 0] + 1e-3 * (box[i, 1] - box[i, 0]))))
    corr = np.corrcoef(acc.T).round(3).tolist() if len(acc) > 2 else None
    idx = np.flatnonzero(m)
    if len(idx) > n_sample:
        idx = np.sort(np.random.default_rng(seed).choice(idx, n_sample, replace=False))
    return {"share_of_box": round(float(m.mean()), 4), "n_accepted": int(m.sum()), "hull": hull, "at_box_edge": edge,
            "correlation": {"names": names, "matrix": corr}, "sample": [to(cand[i]) for i in idx]}


def finalize(reg: dict, checked: list) -> tuple[dict, int]:
    """Attach the verification; a verified point out of band invalidates the sample (review P2): it is moved to
    `sample_unverified`, `sample` is emptied, and the code is 2. Every verified point in band: `verified`, code 0."""
    failed = [c["point"] for c in checked if not c["all_in_band"]]
    reg["verified"] = {"n": len(checked), "all_in_band": len(checked) - len(failed), "failed": failed,
                       "points": checked}
    if failed or not checked:
        reg["sample_status"] = "invalid" if failed else "unverified"
        reg["sample_unverified"], reg["sample"] = reg.get("sample") or [], []
        return reg, 2
    reg["sample_status"] = "verified"
    return reg, 0


def run(pack: dict, base: dict, workers: int, out_path: str, approved: str | None, smoke: bool,
        verify: int = VERIFY, cap_usd: float = CAP_USD) -> int:
    if os.path.basename(out_path) in PROTECTED:
        raise ValueError(f"net_effect_fit writes its own file, never {PROTECTED}")
    bad = check(pack, base)
    if bad:
        raise ValueError("net_effect_fit: " + "; ".join(bad))
    if not smoke and not (approved or "").strip():
        raise ValueError("the full fit spends more than $20: pass --approved with the owner's approval (or --smoke)")
    cal_n = 2 if smoke else CAL_N
    q = plan(pack, verify, cal_n)
    if not smoke and q["usd"][1] > cap_usd:
        raise ValueError(f"the plan's upper cost ${q['usd'][1]} is above the cap ${cap_usd}")
    if calibration_params(pack) != {CAL_PARAM}:
        raise ValueError(f"the calibration arms depend on {sorted(calibration_params(pack))}, not only {CAL_PARAM}")
    eng = BM.load_engine(pack["engine"], None)
    runner = RecalRunner(pack, eng, workers, pack["seeds"], mode="fit", evidence=BM.load_evidence(eng, pack=pack))
    t0 = time.time()
    names = list(pack["fit_joint"]["params"])
    box = np.array([pack["fit_joint"]["params"][k] for k in names], float)
    centre = {k: float(box[i].mean()) for i, k in enumerate(names)}
    runner.calibrate_grid(_fit_settings(pack), centre, cal_n)
    fit = BM.fit_joint(pack, eng, runner)
    X = np.array([[(th[k] - box[i, 0]) / (box[i, 1] - box[i, 0]) for i, k in enumerate(names)]
                  for th in fit["design"]["x"]])
    reg = joint_region(X, fit["design"]["computed"], target_bands(pack), names, box,
                       int(pack["fit_joint"]["design"]["seed"]))
    # re-run `verify` joint-sample points, each at its own interpolated calibration, as the design points were
    byid = {b["id"]: b for b in pack["benchmarks"]}
    rng = np.random.default_rng(int(pack["fit_joint"]["design"]["seed"]) + 1)
    pick = sorted(rng.choice(len(reg["sample"]), min(verify, len(reg["sample"])), replace=False).tolist()) \
        if reg["sample"] else []
    runner.prefetch(sorted({a for t in pack["fit_joint"]["targets"] for a in BM._arms_of(byid[t]["estimand"])}),
                    [reg["sample"][i] for i in pick])
    # each site's control arm at the interpolated calibration, against the baseline it is calibrated to
    ctrl = {s: next(a for a, c in pack["arms"].items() if c["setting"] == s and (c.get("package") or {}) ==
                    (pack["settings"][s]["calibrate"].get("arm_package") or {})) for s in _fit_settings(pack)}
    checked = []
    for i in pick:
        rows = {t: runner.evaluate(byid[t], reg["sample"][i]) for t in pack["fit_joint"]["targets"]}
        res = runner.arm_results(sorted(ctrl.values()), reg["sample"][i])
        cal = {s: {"target": pack["settings"][s]["calibrate"]["target"], "achieved": round(float(np.mean(
            [BM.get(r, pack["settings"][s]["calibrate"]["value_path"]) for r in res[a]])), 4)} for s, a in ctrl.items()}
        checked.append({"point": reg["sample"][i], "all_in_band": all(r["in_band"] for r in rows.values()),
                        "computed": {t: round(r["computed"], 4) for t, r in rows.items()}, "calibration_check": cal})
    reg, code = finalize(reg, checked)
    doc = BM.stamp({"purpose": "trial fit of the v0.17 net-effect parameters on the pack's training targets "
                               "(ENGINE_NOTES.md, v0.17); not adopted",
                    "smoke": smoke, "approved": approved, "cap_usd": cap_usd, "plan": q, "n_sims": runner.nsims,
                    "wall_s": round(time.time() - t0, 1), "fitted_at": time.strftime("%F %T"),
                    "calibration_grid": {CAL_PARAM: runner.cal_grid["values"], "m0": runner.cal_grid["m0"]},
                    "joint_region": reg, **fit}, eng)
    yaml.safe_dump(doc, open(out_path, "w"), sort_keys=False)
    print(f"wrote {out_path} ({runner.nsims} sims); joint sample {reg['sample_status']}: verified "
          f"{reg['verified']['all_in_band']}/{reg['verified']['n']} all in band")
    return code


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def case(label, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case, not a crash of the test
            ok(False, f"{label} ({type(ex).__name__}: {ex})")

    base = yaml.safe_load(open(PACK))
    p = build_pack(base)
    train, held = split(base)
    byid = {b["id"]: b for b in p["benchmarks"]}
    arms_ok = all(a in p["arms"] for t in p["fit_joint"]["targets"] for a in BM._arms_of(byid[t]["estimand"]))
    ok(p["fit_joint"]["targets"] == train == base["fit_joint"]["targets"] and arms_ok and not check(p, base)
       and {"B7", "B8", "B9", "B9b", "B9c", "B15"} <= set(held) and not set(held) & set(train),
       f"F1 the targets are the pack's own fit targets {train}; B7, B8, B9, B9b, B9c, B15 and the rest are held out")
    for t in ("B9c", "B7"):
        q = copy.deepcopy(p)
        q["fit_joint"]["targets"].append(t)
        ok(any(f"{t} is held out" in x for x in check(q, base)), f"F1b a held-out benchmark ({t}) as a target is refused")
    ok(list(p["fit_joint"]["params"]) == ["net_kill_shift", "cfp_kill", "irs_kill", "cfp_half"]
       and "pbo" not in train_nets(base, train) and p["fit_joint"]["params"]["cfp_half"] == [547.5, 1277.5],
       "F2 the fit takes net_kill_shift (training arms use pyrethroid and dual-AI nets), cfp_kill, irs_kill and cfp_half "
       "(owner, 13 Nov; box 1.5-3.5 y); the PBO parameters stay out: no training arm has a PBO net")
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.spec import priors as PR
    e = MalariaEngine()
    sp = {q.name: q.value for q in e.parameters()}
    pr = PR.load_card(e)["priors"]["net_kill_shift"]
    bx = p["fit_joint"]["params"]
    ok(all(bx[k][0] <= sp[k] <= bx[k][1] for k in bx) and [pr.lo, pr.hi] == bx["net_kill_shift"],
       "F3 the box holds every current value; the kill shift's box is its card prior (Churcher 2016 alpha1 CrI)")

    def f4():
        q = plan(p)
        npt = 45 + 1 + 1 + 12
        ok(q["design_points"] == 46 and q["seeds"] == 8 and len(q["arms"]) == 8
           and q["settings"] == ["benin", "irs_added", "misungwi", "muleba"]
           and q["calibration_runs"] == 5 * 21 * 2 and q["arm_runs"] == npt * 8 * 8 + q["calibration_runs"]
           and q["not_fitted"] == ["pbo_adv_decay", "pbo_gain_scale"] and q["held_out_untouched"] == held
           and q["usd"][1] <= CAP_USD,
           f"F4 plan: 46 design points + the chosen value + 12 verified, x 8 arms x 8 seeds, plus a 5-point calibration "
           f"grid in net_kill_shift; {q['arm_runs']} arm runs, {q['vm_vcpu_hours']} vCPU-h, ${q['usd'][0]}-{q['usd'][1]} "
           f"(cap ${CAP_USD})")
    case("F4", f4)
    # F5: a narrow trade-off (a + b = 1 +- 0.05) is kept by the joint sample and lost by independent marginal draws
    names, box = ["a", "b"], np.array([[0.0, 1.0], [0.0, 1.0]])
    g = np.linspace(0, 1, 7)
    X = np.array([[u, v] for u in g for v in g])
    Y = {"T": np.exp(X[:, 0] + X[:, 1])}
    r = joint_region(X, Y, {"T": [np.exp(0.95), np.exp(1.05)]}, names, box, 3)
    S = np.array([[s["a"], s["b"]] for s in r["sample"]])
    rng = np.random.default_rng(0)
    ind = np.column_stack([rng.uniform(*r["hull"]["a"], 2000), rng.uniform(*r["hull"]["b"], 2000)])
    ok(len(S) > 50 and np.all(np.abs(S.sum(1) - 1) <= 0.051) and r["correlation"]["matrix"][0][1] < -0.9
       and np.mean(np.abs(ind.sum(1) - 1) <= 0.05) < 0.2,
       f"F5 the joint sample keeps a narrow trade-off (corr {r['correlation']['matrix'][0][1]}); independent draws "
       f"from the marginal hulls fall outside it {1 - np.mean(np.abs(ind.sum(1) - 1) <= 0.05):.0%} of the time")

    def f6():
        for bad, kw, why in (("fitted.yaml", {"approved": "x", "smoke": False}, "writing fitted.yaml"),
                             ("x.yaml", {"approved": None, "smoke": False}, "a full fit without --approved"),
                             ("x.yaml", {"approved": "x", "smoke": False, "cap_usd": 5.0}, "a plan above the cap")):
            try:
                run(p, base, 1, os.path.join(HERE, bad), **kw)
                ok(False, f"F6 refused: {why}")
            except ValueError:
                ok(True, f"F6 refused before any run: {why}")
    case("F6", f6)
    s = build_pack(base, smoke=True)
    ok(s["seeds"] == base["seeds"][:1] and s["fit_joint"]["design"]["n"] == 3
       and all(v["params"]["n_agents"] == 600 for v in s["settings"].values()) and not check(s, base),
       "F7 --smoke: one seed, 3 design points, 600 agents, 2-year burn-in, the same targets")
    tb = target_bands(p)
    ok(set(tb) == set(train) and all(len(v) == 2 for v in tb.values()),
       "F8 the region step reads the bands of the fit's targets only (B16c has none; the first smoke's failure)")

    def f9():
        dep = calibration_params(p)
        m = interp_calibration([-0.6, 0.0, 0.6], [10.0, 20.0, 40.0], 0.3)
        ok(dep == {"net_kill_shift"}
           and abs(interp_calibration([-0.6, 0.0, 0.6], [10.0, 20.0, 40.0], 0.0) - 20.0) < 1e-12
           and abs(m - 20.0 * 2 ** 0.5) < 1e-9,
           f"F9 (review P2) each site's calibration arm (pyrethroid nets, no IRS) depends on {sorted(dep)} only, so "
           "every parameter point is calibrated by log-linear interpolation on a grid in net_kill_shift (exact at the "
           f"nodes; {m:.3f} half-way between 20 and 40)")
    case("F9", f9)

    def f10():
        rr = RecalRunner(p, e, 1, [11], mode="fit", evidence=BM.load_evidence(e, pack=p))
        ran = []
        rr._map = lambda jobs, chunk=300: (ran.append(len(jobs)), [{"windows": {}} for _ in jobs])[1]
        th = {"net_kill_shift": 0.0, "cfp_kill": 0.4, "irs_kill": 0.4, "cfp_half": 912.5}
        rr.set_calibration({"muleba": 60.0, "misungwi": 11.0, "benin": 10.0, "irs_added": 20.0})
        rr.prefetch(["mul_pyr"], [th])
        rr.prefetch(["mul_pyr"], [th])
        rr.set_calibration({"muleba": 70.0, "misungwi": 11.0, "benin": 10.0, "irs_added": 20.0})
        rr.prefetch(["mul_pyr"], [th])
        ok(ran == [1, 1], f"F10 (review P2) the result cache is keyed on the calibration: the same point is reused "
                          f"under the same calibration and re-run under another (jobs per prefetch {ran})")
    case("F10", f10)

    def f11():
        reg = {"sample": [{"a": 0.1}, {"a": 0.2}], "hull": {"a": [0.1, 0.2]}}
        good, c0 = finalize(copy.deepcopy(reg), [{"point": {"a": 0.1}, "all_in_band": True}])
        bad, c1 = finalize(copy.deepcopy(reg), [{"point": {"a": 0.1}, "all_in_band": True},
                                                {"point": {"a": 0.2}, "all_in_band": False}])
        ok(c0 == 0 and good["sample_status"] == "verified" and len(good["sample"]) == 2
           and c1 != 0 and bad["sample_status"] == "invalid" and bad["sample"] == []
           and len(bad["sample_unverified"]) == 2 and bad["verified"]["failed"] == [{"a": 0.2}],
           "F11 (review P2) a verified point out of band invalidates the exported sample (kept apart as "
           "sample_unverified, with the failing points) and the run returns non-zero")
    case("F11", f11)

    def f12():
        n2, b2 = ["a", "b"], np.array([[0.0, 1.0], [0.0, 1.0]])
        g2 = np.linspace(0, 1, 9)
        X2 = np.array([[u, v] for u in g2 for v in g2])
        Y2 = {"T": np.exp(np.ones(len(X2)))}          # every candidate is in band: the region is the whole box
        r2 = joint_region(X2, Y2, {"T": [2.0, 3.0]}, n2, b2, 17, n_sample=50)
        ok(r2["hull"] == {"a": [0.0, 1.0], "b": [0.0, 1.0]} and r2["at_box_edge"] == ["a", "b"]
           and len(r2["sample"]) == 50,
           f"F12 (review P2) the hull and the box-edge flags come from every accepted candidate, not the 50-point "
           f"subsample ({r2['hull']}, edges {r2['at_box_edge']})")
    case("F12", f12)
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
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--approved", default=None)
    ap.add_argument("--verify", type=int, default=VERIFY)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    base = yaml.safe_load(open(PACK))
    pack = build_pack(base, a.smoke)
    if a.action == "plan":
        print(yaml.safe_dump(plan(pack, a.verify), sort_keys=False))
        return 0
    if a.smoke and not a.out:
        print("--smoke needs --out (a scratch file): a smoke is not evidence")
        return 2
    return run(pack, base, a.workers, a.out or OUT, a.approved, a.smoke, 2 if a.smoke else a.verify)


if __name__ == "__main__":
    sys.exit(main())
