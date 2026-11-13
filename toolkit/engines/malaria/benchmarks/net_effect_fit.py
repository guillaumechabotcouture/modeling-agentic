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
VCPUS = 16                         # one VM of 16 vCPUs, one worker each: the cheapest of 16/32/64 under the cap
# Acceptance (review round 2): a verified point's control arms must reproduce the baseline each site is calibrated
# to within 10% (relative). The interpolation in net_kill_shift and the seed noise move it; 10% is a third or less
# of the training targets' band half-widths (B10 0.30-0.67, B12m 0.19-0.55 around 0.45 and 0.33), so a baseline
# that close does not move a ratio across its band on its own, and a clamped or badly interpolated m0 is caught
CAL_TOL = 0.10
START_H = 0.25                     # VM start, image and tree upload (an assumption; vm_job starts one VM)
POOL_S = 20.0                      # a fresh process pool per _map chunk (an assumption)
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


def _task_s(k: int) -> float:
    """Laptop seconds of one engine task: a burn-in group of k arms of one setting at one point and seed."""
    return GROUP_S + k * ARM_S


def _waves(n_tasks: int, vcpus: int, chunk: int = 300) -> int:
    """benchmark.Runner._map runs tasks in chunks of 300, each on a fresh pool of `vcpus` workers."""
    return sum(-(-min(chunk, n_tasks - i) // vcpus) for i in range(0, n_tasks, chunk))


def plan(pack: dict, verify: int = VERIFY, cal_n: int = CAL_N, vcpus: int = VCPUS) -> dict:
    """Run counts and the price of the allocated VM (review round 2): vcpus x the wall time of each _map wave
    (calibration: cal_n calls of 42 single-arm tasks; design: one call; the chosen point: one call per training
    setting; the sample: one call), at VM_FACTOR x the laptop task time, plus START_H and POOL_S per pool."""
    fj, byid = pack["fit_joint"], {b["id"]: b for b in pack["benchmarks"]}
    arms = sorted({a for t in fj["targets"] for a in BM._arms_of(byid[t]["estimand"])})
    settings = _fit_settings(pack)
    per_set = max(sum(1 for a in arms if pack["arms"][a]["setting"] == s) for s in settings)
    npts, ns = int(fj["design"]["n"]) + 1, len(pack["seeds"])
    grid = sum(len(pack["settings"][s]["calibrate"]["grid"]) for s in settings if pack["settings"][s].get("calibrate"))
    cal_tasks = grid * min(2, ns)
    cal = cal_n * cal_tasks                         # the calibration grid in CAL_PARAM; each run its own burn-in
    pts = npts + 1 + verify                         # design, the chosen value, the verified joint-sample points
    groups, sims = pts * len(settings) * ns, pts * len(arms) * ns
    cpu_h = (groups * GROUP_S + sims * ARM_S + cal * (GROUP_S + ARM_S)) / 3600
    waves = {"calibration": cal_n * _waves(cal_tasks, vcpus), "design": _waves(npts * len(settings) * ns, vcpus),
             "chosen": len(settings) * _waves(ns, vcpus), "sample": _waves(verify * len(settings) * ns, vcpus)}
    pools = cal_n + len(settings) + sum(-(-n // 300) for n in (npts * len(settings) * ns, verify * len(settings) * ns))
    lap = waves["calibration"] * _task_s(1) + (waves["design"] + waves["chosen"] + waves["sample"]) * _task_s(per_set)
    wall = [lap * f / 3600 + START_H + pools * POOL_S / 3600 for f in VM_FACTOR]
    vh = [vcpus * w for w in wall]
    _, held = split(pack)
    return {"params": fj["params"], "targets_train": fj["targets"], "held_out_untouched": held,
            "not_fitted": sorted(set(NET_PARAMS) - set(fj["params"])), "calibration_depends_on": sorted(
                calibration_params(pack)), "arms": arms, "settings": settings,
            "design_points": npts, "verified_sample_points": verify, "seeds": ns, "calibration_runs": cal,
            "arm_runs": sims + cal, "burn_in_groups": groups + cal, "cpu_work_hours": round(cpu_h, 1),
            "vcpus": vcpus, "waves": waves, "wall_hours": [round(w, 2) for w in wall],
            "vm_vcpu_hours": [round(v, 1) for v in vh],
            "usd": [round(vh[0] * USD_PER_VCPU_H[0], 1), round(vh[1] * USD_PER_VCPU_H[1], 1)],
            "launch": " ".join(launch_command(vcpus))}


def launch_command(vcpus: int = VCPUS) -> list:
    """The launch (the lead session runs it after the owner's approval): one VM through cloud/vm_job.py, whose job
    meter stops the VM at --cap-usd in USD (cloud/budget.py); the fit writes a copy of its output under $JOB_OUT."""
    return ["python3", "cloud/vm_job.py", "run", "f6-netfit", "net-effect-fit", "--tree", ".",
            "--store", "outputs/_engine/net_effect_fit", "--cap-usd", f"{CAP_USD:g}", "--budget-scope", "f6",
            "--cpu", str(vcpus), "--memory", f"{4 * vcpus} GiB", "--",
            "python3", "toolkit/engines/malaria/benchmarks/net_effect_fit.py", "run",
            "--approved", "owner 2026-11-13: fit approved, hard cap 25 USD", "--workers", str(vcpus)]


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


def calibration_ok(cal: dict) -> list:
    """Sites whose control arm is off its calibration target by more than CAL_TOL (relative)."""
    return sorted(s for s, c in (cal or {}).items()
                  if not abs(float(c["achieved"]) / float(c["target"]) - 1.0) <= CAL_TOL)


def finalize(reg: dict, checked: list, chosen: dict | None = None, grid_edge=()) -> tuple[dict, int]:
    """The acceptance gate (review rounds 1-2). The sample is `verified` (code 0) only if every verified point and
    the chosen point keep every target in band, their control arms are within CAL_TOL of the calibration targets,
    the chosen point is not disproved, and no site's calibration grid clamps at its edge. Otherwise the sample moves
    to `sample_unverified`, `sample` is emptied, the reasons are recorded, and the code is 2."""
    failed = [c["point"] for c in checked if not c["all_in_band"] or calibration_ok(c.get("calibration_check"))]
    why = [f"verified point out of band or off calibration: {p}" for p in failed]
    for c in checked:
        if calibration_ok(c.get("calibration_check")):
            why.append(f"control arms off target by more than {CAL_TOL:.0%} at {c['point']}: "
                       f"{calibration_ok(c.get('calibration_check'))}")
    if chosen is None:
        why.append("the chosen point's verification is missing")
    else:
        if not chosen.get("all_in_band") or chosen.get("disproved_by_verification"):
            why.append(f"the chosen point fails its verification: {chosen.get('disproved_by_verification')}")
        if calibration_ok(chosen.get("calibration_check")):
            why.append(f"the chosen point's control arms are off target: {calibration_ok(chosen['calibration_check'])}")
    if grid_edge:
        why.append(f"the calibration grid clamps at its edge for {sorted(grid_edge)}")
    if not checked:
        why.append("no verified sample point")
    reg["verified"] = {"n": len(checked), "all_in_band": len(checked) - len(failed), "failed": failed,
                       "points": checked, "chosen": chosen, "calibration_tolerance": CAL_TOL}
    reg["acceptance"] = why
    if why:
        reg["sample_status"] = "unverified" if why == ["no verified sample point"] else "invalid"
        reg["sample_unverified"], reg["sample"] = reg.get("sample") or [], []
        return reg, 2
    reg["sample_status"] = "verified"
    return reg, 0


def _control_check(runner, pack: dict, ctrl: dict, point: dict) -> dict:
    """Each site's control arm at the point's own calibration, against the baseline it is calibrated to."""
    res = runner.arm_results(sorted(ctrl.values()), point)
    return {s: {"target": pack["settings"][s]["calibrate"]["target"], "achieved": round(float(np.mean(
        [BM.get(r, pack["settings"][s]["calibrate"]["value_path"]) for r in res[a]])), 4)} for s, a in ctrl.items()}


PURPOSE = ("trial fit of the v0.17 net-effect parameters on the pack's training targets (ENGINE_NOTES.md, v0.17); "
           "adopted only through a signed spec's uncertainty.net_effect_posterior (load_posterior)")


def document(eng, reg: dict, q: dict, smoke: bool, **rest) -> dict:
    """The output file's document (run() writes it; load_posterior reads it): the plan's box, the joint region."""
    return BM.stamp(dict({"purpose": PURPOSE, "smoke": smoke, "plan": q}, **rest, joint_region=reg), eng)


def _fixture_placeholder(path: str, eng, rows: list, box: dict, gate_ok: bool = True) -> None:
    """A placeholder output in run()'s own format, through finalize() and document() (self-tests only)."""
    pt = {"point": rows[0], "all_in_band": gate_ok, "calibration_check": {}}
    reg, _ = finalize({"sample": [dict(r) for r in rows]}, [pt], {"all_in_band": True, "disproved_by_verification": [],
                                                                "calibration_check": {}})
    yaml.safe_dump(document(eng, reg, {"params": box}, False, calibration_grid={"at_grid_edge": []}), open(path, "w"),
                   sort_keys=False)


class PosteriorRefused(ValueError):
    """The fit's output cannot be the draws' joint sample (fail closed)."""


def read_signed(path: str, digest) -> bytes:
    """The file's bytes, read once, if their sha256 is the owner-signed `digest` (Codex r1 P1-1); else refused."""
    import hashlib
    if not isinstance(digest, str) or len(digest) != 64:
        raise PosteriorRefused(f"net-effect posterior {path}: no signed sha256 (64 hex characters) for the file")
    try:
        data = open(path, "rb").read()
    except OSError as ex:
        raise PosteriorRefused(f"net-effect posterior {path}: unreadable ({ex})") from ex
    if hashlib.sha256(data).hexdigest() != digest.lower():
        raise PosteriorRefused(f"net-effect posterior {path}: its bytes are not the ones signed (sha256 "
                               f"{hashlib.sha256(data).hexdigest()}, signed sha256 {digest})")
    return data


def load_posterior(path: str, engine, ranges: dict, digest, data: bytes | None = None) -> dict:
    """The fit's accepted joint sample as whole rows: {path, sha256 (the signed one), names, rows (n x k)}.
    `data`: the bytes already read by read_signed (read once, Codex r1 P2-4); they must match `digest` too.
    `ranges` {name: (lo, hi)}: the parameters the uncertainty draws vary and their ranges. Refused (PosteriorRefused)
    when the bytes are not the signed ones or the file is unreadable, malformed or not this script's output; a smoke;
    the engine name or code differs from `engine`; the sample is empty, or a row lacks a fitted parameter or holds a
    non-finite or non-numeric value; the acceptance gate fails when finalize() is replayed on the file's own evidence
    (verified points, which must be sample rows, the chosen point, the calibration grid's edge record), whatever its
    summary fields say (Codex r1 P1-2); a column is not in `ranges`; a value is outside its priors.yaml card range
    (whatever `ranges` says, Codex r1 P2-3) or its draw range."""
    import copy as _copy
    import hashlib
    import math
    from toolkit.engines.contract import engine_identity
    from toolkit.spec import priors as PR

    def no(why):
        raise PosteriorRefused(f"net-effect posterior {path}: {why}")
    data = read_signed(path, digest) if data is None else data
    if hashlib.sha256(data).hexdigest() != str(digest).lower():
        no(f"the bytes given are not the ones signed (signed sha256 {digest})")
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError as ex:
        no(f"malformed YAML ({ex})")
    if not isinstance(doc, dict) or not isinstance(doc.get("joint_region"), dict) or doc.get("producer") != BM.PRODUCER:
        no("not an output of net_effect_fit.py run (producer, joint_region)")
    if doc.get("smoke") is not False:
        no("a smoke run is not evidence")
    have, want = doc.get("engine_identity") or {}, engine_identity(engine)
    for k in ("name", "code_sha256"):
        if have.get(k) != want[k]:
            no(f"fitted under engine {k} {have.get(k)}, this engine's is {want[k]}")
    reg = doc["joint_region"]
    names = list((doc.get("plan") or {}).get("params") or {})
    rows = reg.get("sample") or reg.get("sample_unverified")
    if not names or not isinstance(rows, list) or not rows:
        no("no fitted parameters or an empty sample")
    X = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or sorted(r) != sorted(names):
            no(f"sample row {i} holds {sorted(r) if isinstance(r, dict) else r}, the fit's parameters are {names}")
        if not all(isinstance(r[k], (int, float)) and not isinstance(r[k], bool) and math.isfinite(r[k]) for k in names):
            no(f"sample row {i} holds a non-numeric or non-finite value: {r}")
        X.append([float(r[k]) for k in names])
    ver, edge = reg.get("verified") or {}, (doc.get("calibration_grid") or {}).get("at_grid_edge")
    pts = ver.get("points")
    if not isinstance(edge, list) or not isinstance(pts, list) or any(
            not isinstance(p, dict) or p.get("point") not in rows for p in pts):
        no("the fit's acceptance gate cannot be replayed: a calibration_grid.at_grid_edge record and verified points "
           "that are rows of the sample are needed")
    try:
        rr, code = finalize({"sample": _copy.deepcopy(rows)}, _copy.deepcopy(pts), _copy.deepcopy(ver.get("chosen")),
                           edge)
        why = rr["acceptance"]
    except (KeyError, TypeError, ValueError, AttributeError) as ex:
        code, why = 2, [f"{type(ex).__name__}: {ex}"]
    if code != 0 or reg.get("sample") != rows:
        no(f"the fit's acceptance gate (finalize, replayed on the file's evidence) does not pass: {why}")
    miss = [k for k in names if k not in ranges]
    if miss:
        no(f"{miss} are not varied by these draws (an engine-card or spec prior, or a fitted parameter under the "
           f"engine_parameters source, and not calibrated); the draws vary {sorted(ranges)}")
    X = np.asarray(X, float)
    card = PR.load_card(engine)["priors"]
    for j, k in enumerate(names):
        for lo, hi, what in ([(card[k].lo, card[k].hi, "priors.yaml range")] if k in card else []) + [
                (*ranges[k], "draw range")]:
            lo, hi = float(lo), float(hi)
            tol = 1e-6 * max(1.0, hi - lo)          # the sample is rounded to 6 decimals
            if X[:, j].min() < lo - tol or X[:, j].max() > hi + tol:
                no(f"{k} spans [{X[:, j].min()}, {X[:, j].max()}], outside its {what} [{lo}, {hi}]")
    return {"path": os.path.abspath(path), "sha256": str(digest).lower(), "names": names, "rows": X}


def run(pack: dict, base: dict, workers: int, out_path: str, approved: str | None, smoke: bool,
        verify: int = VERIFY, cap_usd: float = CAP_USD, vcpus: int | None = None) -> int:
    if os.path.basename(out_path) in PROTECTED:
        raise ValueError(f"net_effect_fit writes its own file, never {PROTECTED}")
    bad = check(pack, base)
    if bad:
        raise ValueError("net_effect_fit: " + "; ".join(bad))
    if not smoke and not (approved or "").strip():
        raise ValueError("the full fit spends more than $20: pass --approved with the owner's approval (or --smoke)")
    cal_n = 2 if smoke else CAL_N
    q = plan(pack, verify, cal_n, vcpus or max(int(workers), 1))
    if not smoke and q["usd"][1] > cap_usd:
        raise ValueError(f"the plan's upper cost ${q['usd'][1]} on {q['vcpus']} vCPUs is above the cap ${cap_usd}")
    if calibration_params(pack) != {CAL_PARAM}:
        raise ValueError(f"the calibration arms depend on {sorted(calibration_params(pack))}, not only {CAL_PARAM}")
    eng = BM.load_engine(pack["engine"], None)
    runner = RecalRunner(pack, eng, workers, pack["seeds"], mode="fit", evidence=BM.load_evidence(eng, pack=pack))
    t0 = time.time()
    names = list(pack["fit_joint"]["params"])
    box = np.array([pack["fit_joint"]["params"][k] for k in names], float)
    centre = {k: float(box[i].mean()) for i, k in enumerate(names)}
    runner.calibrate_grid(_fit_settings(pack), centre, cal_n)
    grid_edge = sorted(s for s, rows in runner.cal_grid["rows"].items() if any(r.get("at_grid_edge") for r in rows))
    fit = BM.fit_joint(pack, eng, runner)
    X = np.array([[(th[k] - box[i, 0]) / (box[i, 1] - box[i, 0]) for i, k in enumerate(names)]
                  for th in fit["design"]["x"]])
    reg = joint_region(X, fit["design"]["computed"], target_bands(pack), names, box,
                       int(pack["fit_joint"]["design"]["seed"]))
    byid = {b["id"]: b for b in pack["benchmarks"]}
    ctrl = {s: next(a for a, c in pack["arms"].items() if c["setting"] == s and (c.get("package") or {}) ==
                    (pack["settings"][s]["calibrate"].get("arm_package") or {})) for s in _fit_settings(pack)}
    best = {k: v["value"] for k, v in fit["parameters"].items()}
    chosen = {"point": best, "all_in_band": all(v["in_band"] for v in fit["verification"].values()),
              "disproved_by_verification": fit["feasibility"]["disproved_by_verification"],
              "calibration_check": _control_check(runner, pack, ctrl, best)}
    # re-run `verify` joint-sample points, each at its own interpolated calibration, as the design points were
    rng = np.random.default_rng(int(pack["fit_joint"]["design"]["seed"]) + 1)
    pick = sorted(rng.choice(len(reg["sample"]), min(verify, len(reg["sample"])), replace=False).tolist()) \
        if reg["sample"] else []
    runner.prefetch(sorted({a for t in pack["fit_joint"]["targets"] for a in BM._arms_of(byid[t]["estimand"])}),
                    [reg["sample"][i] for i in pick])
    checked = []
    for i in pick:
        rows = {t: runner.evaluate(byid[t], reg["sample"][i]) for t in pack["fit_joint"]["targets"]}
        checked.append({"point": reg["sample"][i], "all_in_band": all(r["in_band"] for r in rows.values()),
                        "computed": {t: round(r["computed"], 4) for t, r in rows.items()},
                        "calibration_check": _control_check(runner, pack, ctrl, reg["sample"][i])})
    reg, code = finalize(reg, checked, chosen, grid_edge)
    doc = document(eng, reg, q, smoke, approved=approved, cap_usd=cap_usd, n_sims=runner.nsims,
                   wall_s=round(time.time() - t0, 1), fitted_at=time.strftime("%F %T"),
                   calibration_grid={CAL_PARAM: runner.cal_grid["values"], "m0": runner.cal_grid["m0"],
                                     "at_grid_edge": grid_edge}, **fit)
    yaml.safe_dump(doc, open(out_path, "w"), sort_keys=False)
    job_out = os.environ.get("JOB_OUT")
    if job_out and not smoke:
        yaml.safe_dump(doc, open(os.path.join(job_out, os.path.basename(out_path)), "w"), sort_keys=False)
    print(f"wrote {out_path} ({runner.nsims} sims); joint sample {reg['sample_status']}: verified "
          f"{reg['verified']['all_in_band']}/{reg['verified']['n']} all in band; acceptance: {reg['acceptance'] or 'ok'}")
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
        w = q["waves"]
        naive = q["cpu_work_hours"] * VM_FACTOR[1] * USD_PER_VCPU_H[1]
        q64 = plan(p, vcpus=64)
        ok(q["design_points"] == 46 and q["seeds"] == 8 and len(q["arms"]) == 8
           and q["settings"] == ["benin", "irs_added", "misungwi", "muleba"]
           and q["calibration_runs"] == 5 * 21 * 2 and q["arm_runs"] == npt * 8 * 8 + q["calibration_runs"]
           and q["not_fitted"] == ["pbo_adv_decay", "pbo_gain_scale"] and q["held_out_untouched"] == held
           and q["vcpus"] == VCPUS and w["calibration"] == 5 * -(-42 // VCPUS) and w["chosen"] == 4
           and w["design"] == sum(-(-min(300, 1472 - i) // VCPUS) for i in range(0, 1472, 300))
           and w["sample"] == sum(-(-min(300, 384 - i) // VCPUS) for i in range(0, 384, 300))
           and q["usd"][1] > naive and q64["usd"][1] > q["usd"][1] and q["usd"][1] <= CAP_USD,
           f"F4 (review P2) the price is allocated VM time: {q['vcpus']} vCPUs x the wall time of the calibration "
           f"({w['calibration']}), design ({w['design']}), chosen-point ({w['chosen']}) and sample ({w['sample']}) "
           f"waves plus start-up, {q['vm_vcpu_hours']} vCPU-h, ${q['usd'][0]}-{q['usd'][1]} (cap ${CAP_USD}); the "
           f"CPU-work estimate would say ${naive:.1f}; 64 vCPUs ${q64['usd'][1]}")
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
                             ("x.yaml", {"approved": "x", "smoke": False, "cap_usd": 5.0}, "a plan above the cap"),
                             ("x.yaml", {"approved": "x", "smoke": False, "vcpus": 64}, "64 vCPUs above the cap")):
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
        cal = lambda **kw: {s: {"target": t, "achieved": kw.get(s, t)} for s, t in  # noqa: E731
                            (("muleba", 0.55), ("misungwi", 0.458), ("benin", 2.06), ("irs_added", 0.40))}
        pt = lambda a, ok_=True, **kw: {"point": {"a": a}, "all_in_band": ok_, "calibration_check": cal(**kw)}  # noqa
        chosen_ok = {"all_in_band": True, "disproved_by_verification": [], "calibration_check": cal()}
        runs = {
            "good": (finalize(copy.deepcopy(reg), [pt(0.1), pt(0.2, muleba=0.58)], chosen_ok), 0),
            "ratio": (finalize(copy.deepcopy(reg), [pt(0.1), pt(0.2, False)], chosen_ok), 2),
            "misungwi 0.30 vs 0.458": (finalize(copy.deepcopy(reg), [pt(0.1), pt(0.2, misungwi=0.30)], chosen_ok), 2),
            "muleba 0.15 vs 0.55": (finalize(copy.deepcopy(reg), [pt(0.1, muleba=0.15)], chosen_ok), 2),
            "chosen disproved B10": (finalize(copy.deepcopy(reg), [pt(0.1)], dict(
                chosen_ok, all_in_band=False, disproved_by_verification=["B10"])), 2),
            "chosen calibration": (finalize(copy.deepcopy(reg), [pt(0.1)], dict(chosen_ok, calibration_check=cal(
                benin=1.5))), 2),
            "grid at its edge": (finalize(copy.deepcopy(reg), [pt(0.1)], chosen_ok, grid_edge=["misungwi"]), 2),
        }
        bad = {k: (r[1], r[0]["sample_status"]) for k, (r, want) in runs.items()
               if r[1] != want or (want == 0) != (r[0]["sample_status"] == "verified")
               or (want != 0 and (r[0]["sample"] or len(r[0]["sample_unverified"]) != 2))}
        ok(not bad and runs["good"][0][0]["sample"] == reg["sample"],
           f"F11 (review P2, rounds 1-2) the sample is verified only if every verified point and the chosen point keep "
           f"the targets in band and their control arms within {CAL_TOL:.0%} of the calibration target, and no site's "
           f"calibration grid is at its edge; otherwise it is invalidated and the run returns 2 ({sorted(runs)}; "
           f"wrong: {bad})")
    case("F11", f11)

    def f13():
        cmd = launch_command(VCPUS)
        ok(cmd[:3] == ["python3", "cloud/vm_job.py", "run"] and cmd[cmd.index("--cap-usd") + 1] == "25"
           and "--approved" in cmd and str(VCPUS) in cmd and cmd[cmd.index("--") + 1:][:2] == [
               "python3", "toolkit/engines/malaria/benchmarks/net_effect_fit.py"],
           f"F13 the launch goes through cloud/vm_job.py run with --cap-usd 25, so the job's USD meter stops the VM at "
           f"the cap: {' '.join(cmd)}")
    case("F13", f13)

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

    def f14():
        import tempfile
        box = {"net_kill_shift": [-0.618, 0.66], "cfp_kill": [0.0, 0.8], "irs_kill": [0.0, 0.95],
               "cfp_half": [547.5, 1277.5]}
        u = [float(v) for v in np.random.default_rng(5).uniform(0, 1, 40)]
        rows = [{"net_kill_shift": round(-0.5 + x, 6), "cfp_kill": round(0.3 + 0.2 * x, 6), "irs_kill": 0.5,
                 "cfp_half": round(700 + 300 * x, 6)} for x in u]
        rg = {"net_kill_shift": (pr.lo, pr.hi), "cfp_kill": (0.0, 0.85), "irs_kill": (0.0, 0.95),
              "cfp_half": (547.5, 1277.5)}
        import hashlib
        dg = lambda q: hashlib.sha256(open(q, "rb").read()).hexdigest()  # noqa: E731 -- the owner's signed digest
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "net_effect_fit.yaml")
            _fixture_placeholder(good, e, rows, box)
            P = load_posterior(good, e, rg, dg(good))
            ok(P["names"] == list(box) and P["rows"].shape == (40, 4) and P["sha256"] == dg(good)
               and np.allclose(P["rows"][:, 0], [r["net_kill_shift"] for r in rows])
               and np.allclose(P["rows"][:, 3], [r["cfp_half"] for r in rows]),
               "F14 a verified fit output (run()'s own format, placeholder sample) under its signed sha256 loads as "
               f"whole rows, one column per fitted parameter {P['names']}")
            got = {}

            def refuse(label, path, ranges=rg, digest=None):
                try:
                    load_posterior(path, e, ranges, digest or (dg(path) if os.path.exists(path) else "0" * 64))
                    got[label] = ""
                except PosteriorRefused as ex:
                    got[label] = str(ex)

            def variant(label, mut, ranges=rg, digest=None):
                doc = yaml.safe_load(open(good))
                mut(doc)
                q = os.path.join(d, label.replace(" ", "_") + ".yaml")
                yaml.safe_dump(doc, open(q, "w"))
                refuse(label, q, ranges, digest)
            jr = lambda x: x["joint_region"]  # noqa: E731
            # Codex r1 P1-1: an edited row is refused against the signed digest of the accepted output
            variant("an edited row under the signed digest", lambda x: jr(x)["sample"][5].update(cfp_kill=0.55),
                    digest=dg(good))
            # Codex r1 P1-2: the gate is replayed (finalize) on the evidence; passing summaries do not carry it
            variant("a verified point out of band", lambda x: jr(x)["verified"]["points"][0].update(all_in_band=False))
            variant("the chosen point out of band", lambda x: jr(x)["verified"]["chosen"].update(all_in_band=False))
            variant("no verified point", lambda x: jr(x)["verified"].update(points=[]))
            variant("a clamped calibration grid", lambda x: x["calibration_grid"].update(at_grid_edge=["muleba"]))
            variant("no calibration grid record", lambda x: x.pop("calibration_grid"))
            variant("a verified point not in the sample",
                    lambda x: jr(x)["verified"]["points"][0]["point"].update(cfp_kill=0.79))
            # Codex r1 P2-3: a wider caller range (a spec value prior outside the card range) does not widen priors.yaml
            variant("a value outside priors.yaml under a wider range", lambda x: jr(x)["sample"][7].update(
                net_kill_shift=0.9), ranges=dict(rg, net_kill_shift=(-1.0, 1.0)))
            refuse("missing file", os.path.join(d, "absent.yaml"))
            for label, text in (("malformed YAML", "joint_region: [unclosed"), ("not a mapping", "- 1\n")):
                open(os.path.join(d, "t.yaml"), "w").write(text)
                refuse(label, os.path.join(d, "t.yaml"))
            bad_gate = os.path.join(d, "gate.yaml")
            _fixture_placeholder(bad_gate, e, rows, box, gate_ok=False)
            refuse("acceptance gate failed (finalize)", bad_gate)
            variant("a smoke", lambda x: x.update(smoke=True))
            variant("another producer", lambda x: x.update(producer="someone"))
            variant("other engine code", lambda x: x["engine_identity"].update(code_sha256="0" * 64))
            variant("empty sample", lambda x: x["joint_region"].update(sample=[]))
            variant("a row lacks a parameter", lambda x: x["joint_region"]["sample"][3].pop("cfp_half"))
            variant("a non-finite value", lambda x: x["joint_region"]["sample"][3].update(cfp_kill=float("nan")))
            variant("a non-numeric value", lambda x: x["joint_region"]["sample"][3].update(cfp_kill="high"))
            variant("a column the fit did not fit", lambda x: x["plan"]["params"].pop("irs_kill"))
            refuse("a parameter these draws do not vary", good, {k: v for k, v in rg.items() if k != "cfp_half"})
            refuse("a value outside its draw range", good, dict(rg, net_kill_shift=(0.0, pr.hi)))
            rows2 = [dict(r) for r in rows]
            rows2[1]["cfp_half"] = 701.0
            other = os.path.join(d, "other.yaml")
            _fixture_placeholder(other, e, rows2, box)
            sha2 = load_posterior(other, e, rg, dg(other))["sha256"]
        gate = ("acceptance gate failed (finalize)", "a verified point out of band", "the chosen point out of band",
                "no verified point", "a clamped calibration grid", "no calibration grid record",
                "a verified point not in the sample")
        why = {**dict.fromkeys(gate, "acceptance gate"), "missing file": "unreadable", "malformed YAML": "malformed",
               "an edited row under the signed digest": "signed sha256",
               "a value outside priors.yaml under a wider range": "priors.yaml",
               "not a mapping": "not an output", "another producer": "not an output", "a smoke": "smoke",
               "other engine code": "code_sha256", "empty sample": "empty sample",
               "a row lacks a parameter": "the fit's parameters", "a non-finite value": "non-finite",
               "a non-numeric value": "non-numeric", "a column the fit did not fit": "the fit's parameters",
               "a parameter these draws do not vary": "not varied", "a value outside its draw range": "draw range"}
        wrong = sorted(k for k in why if why[k] not in got.get(k, ""))
        ok(not wrong and set(got) == set(why) and sha2 != P["sha256"],
           f"F15 the loader fails closed, each for its own reason, in {len(why)} cases ({sorted(why)}; refused for "
           f"another reason or not at all: {wrong or 'none'}); a changed sample has another sha256")
    case("F14", f14)
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
    ap.add_argument("--vcpus", type=int, default=VCPUS, help="the VM's vCPUs (plan; run: defaults to --workers)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    base = yaml.safe_load(open(PACK))
    pack = build_pack(base, a.smoke)
    if a.action == "plan":
        print(yaml.safe_dump(plan(pack, a.verify, vcpus=a.vcpus), sort_keys=False))
        return 0
    if a.smoke and not a.out:
        print("--smoke needs --out (a scratch file): a smoke is not evidence")
        return 2
    return run(pack, base, a.workers, a.out or OUT, a.approved, a.smoke, 2 if a.smoke else a.verify)


if __name__ == "__main__":
    sys.exit(main())
