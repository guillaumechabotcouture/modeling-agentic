#!/usr/bin/env python3
"""Trial fit of the net-effect parameters (design: ENGINE_NOTES.md, v0.17; option A and v0.18's net_deter_sat below).
Wave 1 ran on cluster 2375765 (net_effect_fit_wave1.yaml: sample invalid, 8/12 verified points in band); `run` is
now wave 2, which reuses wave 1's engine results (load_wave1) and runs only new points (fit_waves). Wave 2 ran on
cluster 2377496 (invalid, 9/12; its choice extrapolated past the design); `run --verify-round` reads wave 2's stored
checkpoint, keeps candidates inside the waves' boxes, and verifies a new sample under ACCEPTANCE_RULE (owner, 14 Nov).

    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py plan                 # design, run count, cost
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py run --approved "<owner approval>" --workers W
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py run --smoke --out <scratch file> --workers 2
    python3 toolkit/engines/malaria/benchmarks/net_effect_fit.py --self-test

F6 option A (owner, 18 Nov; OA3): `run` is now a fresh fit (OPTION_A) on the net trials at their own estimands. The
PBO trials (B9, B9b, B9c, LLINEUP's B9d-B9f) and Misungwi's dual-AI vs PBO contrast (B9g) joined the pack's fit
targets, so wave 1's results (other fit declarations) are not reusable and the waves and verification round above are
history: load_wave1 refuses them on this pack. The PBO parameters join (a training arm now has a PBO net), each in its
card-prior box. The fit's cap is OPTION_A["cap_usd"], set by the owner in code; `run` refuses while it is unset.

Train and held out. The targets are exactly the pack's own fit targets (`fit_joint.targets` in pack.yaml: until
option A B10, B11, B12, B12b, B12m; now also B9-B9g); every other benchmark is held out and stays untouched: the fit never scores one, its objective and
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
import json
import math
import os
import shlex
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
# widen). F6 option A: the PBO boxes are their card priors (priors.yaml: pbo_gain_scale 0.316-1.0 from Gleave 2021's
# hut-trial mortality RR, pbo_adv_decay 0-1.895 per year from LLINEUP's Table 2), used now that PBO trials are targets.
# Engine v0.18 (owner, 18 Nov, form P, fitted): net_deter_sat, PBO nets only, over its card prior (its domain 0-1).
# Refit b, owner option (a), 2026-11-19: pbo_adv_decay's ceiling for this fit is 2.23 per year, the 99% interval of
# LLINEUP's waning (as-treated, 6 -> 18 m: 0.546 + 2.576 x 0.535 = 1.924) minus the engine's own waning at bioassay
# mortality x = 0.30 (-0.306 at the fitted net_deter_sat 0.451, vector.engine_adv_decay), the same rule as the card
# prior's 95% bound (1.895). The 18 Nov fit's choice sat at 1.868 of 1.895. It sets this fit's box and the posterior's
# hard bound (hard_bounds). The card prior file (priors.yaml, 0-1.895) is not edited: its test sits inside the engine's
# code fingerprint, and an edit there would forfeit the stored checkpoint (ENGINE_NOTES.md, v0.18, refit b).
PAD_CEILING = {"param": "pbo_adv_decay", "lo": 0.0, "hi": 2.23, "theta": 0.451,
               "basis": "99% interval of LLINEUP's waning (as-treated, 6 -> 18 m) minus the engine's own waning at "
                        "x = 0.30 (owner, 2026-11-19, option (a))"}
NET_PARAMS = {"net_kill_shift": ({"pyr", "pbo", "dual"}, [-0.618, 0.66]),
              "pbo_gain_scale": ({"pbo"}, [0.316, 1.0]),
              "pbo_adv_decay": ({"pbo"}, [PAD_CEILING["lo"], PAD_CEILING["hi"]]),
              "net_deter_sat": ({"pbo"}, [0.0, 1.0])}
CONFOUNDED = [("pbo_gain_scale", "net_deter_sat")]   # owner, 18 Nov: report their joint posterior in the fit output
# owner, 13 Nov: cfp_half joins the fit. cfp_half is the half-life, in days of net age, of the chlorfenapyr kill share
# (vector.py: cfp_kill exp(-ln2 t / cfp_half)). Wave 1's box, 547.5-1277.5 days (attempt 8's 1.5-3.5 y), put its
# accepted region at both box edges. Wave 2's box, 270-1650 days, is a design box, not a prior: no published half-life
# exists (priors.yaml), so its ends are exponential half-lives derived here from two field durability studies of
# Interceptor G2 (both opened; arithmetic ours, so the box is a modeller reading of them):
#  - lower end, from chlorfenapyr content, which decays faster than the kill it causes: Ngufor et al. 2024 Parasites &
#    Vectors 17:420, 10.1186/s13071-024-06504-1, Table 8 (Benin): 5.0 g/kg new, 2.0 at 12 m, 1.2 at 24 m, 0.7 at 36 m,
#    half-lives 276, 355 and 386 days; Fagbohoun et al. 2026 PLOS Glob Public Health 10.1371/journal.pgph.0005306,
#    abstract (Mozambique): 32% left at 24 m, 444 days. 270 is below the shortest.
#  - upper end, from bioefficacy: Ngufor 2024 Table 7, tunnel-test mortality of pyrethroid-resistant An. coluzzii
#    95.7% new, 82.1% at 12 m, 51.7% at 24 m, 60.2% at 36 m (ratios to new: half-lives 1650, 822 and 1637 days);
#    Fagbohoun 2026 abstract, hut mortality 55% to 39% by 24 m (1472 days). 1650 is the longest.
FIT_EXTRA = {"cfp_half": [270.0, 1650.0]}
# Wave 2 (owner, 13 Nov, "option 1"): wave 1's engine results are reused from its output, a verbatim copy of the run
# on cluster 2375765 (commit fb8deac; engine code identical to this one's, pack.yaml then WAVE1_PACK_SHA256). F6 option
# A: reuse is bound to what the fit runs, the training targets' declarations and the fit rule (benchmark_sources
# fit_declarations: arms, settings, horizon, seeds, engine) of the effective pack (build_pack of the pack wave 1 ran
# on), WAVE1_FIT_DECLARATIONS_SHA256, so held-out benchmarks added to the pack (B9d-B9g) leave it reusable.
# 48 new LHS points in the wave-2 box: wave 1's accepted hull widened by `margin` of its width on each side, to the
# new box edge on a side where the hull reached the old box edge (wave 1 holds no point past it). 48 points: 3 per
# quadratic coefficient in 4 dimensions (15) plus 3; the plan stays under CAP_USD.
WAVE1 = os.path.join(HERE, "net_effect_fit_wave1.yaml")
WAVE1_SHA256 = "54958ad2e79716edf19f2b4b60f263ca41056d3202147ff4bfa23277b318a067"
WAVE1_PACK_SHA256 = "32abb2bb6457ed7008c6410c55b4e0b620b2e982073a7dd5cbdcb7988114a905"
# Codex r2 on #268: this digest is of the REUSING fit's effective pack (wave 2 and verification round 2, cfp_half box
# 270-1650 days per FIT_EXTRA), not of wave 1's own run (box 547.5-1277.5, WAVE1_BOX; digest 191fb4d0...). The widening
# is the owner's 13 Nov "option 1"; wave 1's box is checked against its output by its recorded plan, not by this digest.
WAVE1_FIT_DECLARATIONS_SHA256 = "e0fa57b0822d064c21ea78fd22d00ce214f1f43e704af930766e6aaa4f2a84dc"
WAVE1_BOX = {"net_kill_shift": [-0.618, 0.66], "cfp_kill": [0.0, 0.8], "irs_kill": [0.0, 0.95],
             "cfp_half": [547.5, 1277.5]}
WAVE2 = {"n": 48, "seed": 23, "margin": 0.25}
WAVE1_TARGETS = ["B10", "B11", "B12", "B12b", "B12m"]     # the fit targets waves 1 and 2 and verification round 2 ran on
# F6 option A (owner, 18 Nov; AMENDMENTS.md section 11): a fresh fit on the net trials at their own estimands, with
# its own target declaration (fit record (b)). Wave 1: 3 points per quadratic coefficient in the 7 fitted dimensions
# (36 coefficients: 108), as DESIGN did in 4; wave 2: the same rule plus 3, as WAVE2 did (48 = 3 x 15 + 3), in wave 1's
# accepted hull widened by WAVE2's margin. Shared data:
# pbo_adv_decay's prior box was derived from LLINEUP (B9d, B9f), now also targets; B9g is B10 / B9c of the same arms, so
# it adds a same-trial contrast, not independent data. cap_usd is the owner's, set here when the fit is approved:
# 2026-11-18, one VM of 96 vCPUs (E96s_v5), $250 (about 2.4x the plan's measured-rate upper bound).
# Refit b (owner, 2026-11-19): the 18 Nov fit (f6-refit-optiona, rc 2) found no parameter set reproducing Muleba's
# durable PBO effect with LLINEUP's waning: B9 and B9b in band together at 6 of 220 design points, all with
# pbo_adv_decay 0.06-0.27, where B9f is 0.22-0.32 (band 0.72-0.98); B9b with B9f, and B9 with B9f, at 0/220. One
# global waning rate cannot carry both, so B9 and B9b leave the training targets and are monitored (evaluated at the
# chosen and verified points, reported, never used to choose). The 18 Nov engine results are reused from its stored
# checkpoint (cache_sha256), so its tasks must not change: the design box is the box as ran, the wave-2 box is that
# box (pinned; the 10-target wave-1 hull would otherwise narrow it and every wave-2 task rerun), and the monitored
# arms still run with Muleba. --margin-loo is off (owner). `extension` (None until the owner sets its ceiling): an LHS
# of n points with `param` over `range` (past the old card-prior ceiling) and every other parameter over the design box.
OPTION_A = {"targets": ["B9c", "B9d", "B9e", "B9f", "B9g", "B10", "B11", "B12", "B12b", "B12m"],
            "monitor": ["B9", "B9b"],
            "departure": "B9 and B9b held out (owner, 2026-11-19): one global PBO waning rate cannot fit Muleba's durable "
                         "effect with LLINEUP's waning (18 Nov fit: B9b with B9f and B9 with B9f jointly in band at 0/220 "
                         "design points; B9 with B9b at 6/220, all with pbo_adv_decay 0.06-0.27, where B9f is 0.22-0.32)",
            "design_n": 108, "wave2_n": 111, "cap_usd": 100.0,
            "store": "outputs/_engine/net_effect_fit/option_a_b", "job": "f6-refit-optiona-b",
            "refit_b": {"cache_sha256": "b0c557b808a0b89905ba16b633868957a9794dc72346c9cba2fb363d368cebff",
                        "resume_dir": "_resume_optiona",
                        "design_box": {"net_kill_shift": [-0.618, 0.66], "pbo_gain_scale": [0.316, 1.0],
                                       "pbo_adv_decay": [0.0, 1.895], "net_deter_sat": [0.0, 1.0],
                                       "cfp_kill": [0.0, 0.8], "irs_kill": [0.0, 0.95], "cfp_half": [270.0, 1650.0]},
                        "extension": {"n": 100, "seed": 37, "param": "pbo_adv_decay", "range": [1.895, 2.23]}}}
# Verification round 2 (owner, 14 Nov): wave 2 (cluster 2377496) ran with candidates over the whole box, so its chosen
# point and two failed points sat at cfp_half ~1,600 days, past wave 2's design box (extrapolation). This round reads
# wave 2's engine results from its stored checkpoint (no design point rerun), refits with the candidates and the
# chosen point inside the waves' support, and verifies a NEW joint sample (new candidate and pick seeds: the 12 points
# verified before are not rescored) under ACCEPTANCE_RULE, set before this run.
VERIFY_ROUND = {"n": 2, "candidate_seed": 29, "pick_seed": 31, "plan_cap_usd": 10.0,
                "cache_blob": "outputs/_engine/net_effect_fit/netfit_cache.jsonl",
                "cache_sha256": "3bde5c6ec7b6e6b204968b3a52b33541300316d1f0aa7f571bb5b30f91f3311b",
                "wave2_output_sha256": "148621e379e8049a32bb7399057bef4b4b514244086f755b2df006ccce3ba79f"}
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
# Measured (wave 1, cluster 2375765): $15.18 billed for 27,464 s on 16 vCPUs plus start-up, about 1 credit per
# vCPU-hour at $0.124 per credit (credits x $0.061 plus the Azure VM). `run` refuses a plan above the cap on either basis
USD_REAL_PER_VCPU_H = 0.124
ACTS = {"irs": {"irs_kill"}, "dual": {"cfp_kill", "cfp_half"}}      # fitted parameters by package element


def split(pack: dict) -> tuple[list, list]:
    """(training targets, held-out ids): the pack's own fit targets and every other benchmark."""
    train = list((pack.get("fit_joint") or {}).get("targets") or [])
    return train, [b["id"] for b in pack["benchmarks"] if b["id"] not in train]


def train_nets(pack: dict, train: list) -> set:
    byid = {b["id"]: b for b in pack["benchmarks"]}
    arms = {a for t in train for a in BM._arms_of(byid[t]["estimand"])}
    return {str((pack["arms"][a].get("package") or {}).get("net")) for a in arms} - {"None"}


def build_pack(base: dict, smoke: bool = False, targets: list | None = None) -> dict:
    """The pack with the fit's own targets and a joint fit over the pack's fitted parameters, cfp_half, and every
    net-effect parameter that a training arm identifies. Benchmarks and settings are the pack's (smoke: small runs).
    `targets`: the fit's declared targets, OPTION_A's by default (owner, 18 Nov, fit record (b): the net-effect fit
    declares its own, apart from the pack's fit_joint, so fitted.yaml stays valid); WAVE1_TARGETS for the history."""
    p = copy.deepcopy(base)
    train = list(OPTION_A["targets"] if targets is None else targets)
    unknown = sorted(set(train) - {b["id"] for b in base["benchmarks"]})
    if unknown:
        raise ValueError(f"net-effect fit targets {unknown} are not benchmarks of the pack")
    nets = train_nets(base, train)
    params = {k: list(b) for k, (acts, b) in NET_PARAMS.items() if acts & nets}
    params.update({k: list(v) for k, v in base["fit_joint"]["params"].items()})
    params.update({k: list(v) for k, v in FIT_EXTRA.items()})
    option_a = sorted(train) == sorted(OPTION_A["targets"])         # F6 option A: a fresh wave 1 in 7 dimensions
    design = dict(DESIGN, n=OPTION_A["design_n"]) if option_a else dict(DESIGN)
    p["fit_joint"] = {"rule": "joint-v1", "params": params, "targets": train, "design": design}
    if smoke:
        p["seeds"] = p["seeds"][:1]
        p["fit_joint"]["design"] = {"n": 3, "seed": DESIGN["seed"]}
        for s in p["settings"].values():
            s["params"] = dict(s.get("params") or {}, n_agents=600, burnin_years=2)
    return p


def check(pack: dict, base: dict) -> list[str]:
    """Why `pack` may not be fitted: its targets are not one of the fit's declarations (OPTION_A's, or the history's,
    the pack's own fit targets WAVE1_TARGETS); a target in neither is held out."""
    decl = [list(OPTION_A["targets"]), list(split(base)[0])]
    t = list(pack["fit_joint"]["targets"])
    known = set().union(*map(set, decl))
    bad = [f"{x} is held out (in no declaration of this fit, {decl}): it may not be a target" for x in t
           if x not in known]
    return bad + ([f"targets {t} differ from the fit's declarations {decl}"]
                  if not any(sorted(t) == sorted(d) for d in decl) else [])


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

    def use_grid(self, grid: dict):
        """A calibration grid computed before (wave 1's: {values, m0}), reused instead of rerun."""
        rows = {s: [{"attribute": self._p["settings"][s]["calibrate"]["attribute"], "at_grid_edge": False}
                    for _ in grid["values"]] for s in grid["m0"]}
        self.cal_grid = {"values": list(grid["values"]), "m0": dict(grid["m0"]), "rows": rows}

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
        # refit b: a call that runs a monitored target's setting runs its arms too, so a task holds the same arms as
        # the 18 Nov run's (all 12 targets) and the stored checkpoint matches; sorted, as every caller passes them
        mon = {a for t in (OPTION_A.get("monitor") or [] if OPTION_A.get("refit_b") else [])   # refit b only
               for a in BM._arms_of(self._decl[t]["estimand"])}
        sets = {self._p["arms"][a]["setting"] for a in arms}
        add = {a for a in mon if self._p["arms"][a]["setting"] in sets}
        arms = sorted(set(arms) | add) if add - set(arms) else arms
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

    # ---- checkpoint (owner, 13 Nov: a cap stop must not lose the finished tasks)
    ckpt, ckpt_out = None, None       # {key: result JSON} loaded from --resume-from; the directory appended to
    task_fn = staticmethod(BM._job_list)

    def code_sha256(self) -> str:
        from toolkit.engines.contract import code_fingerprint
        return code_fingerprint(self.engine)

    def checkpoint(self, out_dir: str | None = None, resume_from: str | None = None):
        """Append each finished engine task to <out_dir>/CACHE_NAME as it lands; reuse the matching tasks of
        <resume_from>/CACHE_NAME (load_cache: refused rows fail closed), so a relaunch runs only the missing ones."""
        rows = []
        self.ckpt = load_cache(resume_from, self.code_sha256(), rows) if resume_from else {}
        if out_dir:      # Codex r1 P2: a fresh file of the accepted rows (atomic), never appended after a cut row
            os.makedirs(out_dir, exist_ok=True)
            _safe_io().write_nofollow(out_dir, CACHE_NAME, "".join(r + "\n" for r in rows).encode(), atomic=True)
            self.ckpt_out = out_dir

    def _map(self, jobs, chunk: int = 300):
        """benchmark.Runner._map's tasks (the same grouping and order within a task), each run once: a task in the
        checkpoint is read from it, a new one is appended as it finishes. Every result passes through its JSON form,
        so a resumed run returns exactly what an uninterrupted one does."""
        if self.ckpt is None:
            return BM.Runner._map(self, jobs, max(chunk, _chunk(self.workers)))
        from concurrent.futures import ProcessPoolExecutor, as_completed
        by = {}
        for n, j in enumerate(jobs):
            ref, config, _, unit, _, horizon, seed = j
            by.setdefault(_jdump([ref, config, unit, horizon, seed]) if self._shared(j) else n, []).append(j)
        tasks, code = list(by.values()), self.code_sha256()
        canon = [json.loads(_jdump(t)) for t in tasks]
        keys = [_task_key(code, t) for t in canon]
        res = [json.loads(self.ckpt[k]) if k in self.ckpt else None for k in keys]

        def land(i, r):
            s = _rdump(r)
            res[i] = json.loads(s)
            self.ckpt[keys[i]] = s
            if self.ckpt_out:
                with _safe_io().open_append_nofollow(self.ckpt_out, CACHE_NAME) as fh:
                    fh.write(json.dumps({"key": keys[i], "code_sha256": code, "task": canon[i], "result": res[i],
                                         "result_sha256": _sha(s)}) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
        todo = [i for i, r in enumerate(res) if r is None]
        self.ran_tasks = getattr(self, "ran_tasks", 0) + len(todo)
        if self.workers <= 1 or len(todo) <= 1:
            for i in todo:
                land(i, self.task_fn(tasks[i]))
        else:
            chunk = max(chunk, _chunk(self.workers))
            for c in range(0, len(todo), chunk):
                part = todo[c:c + chunk]
                with ProcessPoolExecutor(min(self.workers, len(part)), **BM._pool_kw()) as ex:
                    fut = {ex.submit(self.task_fn, tasks[i]): i for i in part}
                    for f in as_completed(fut):
                        land(fut[f], f.result())
                print(f"  ran {min(c + chunk, len(todo))}/{len(todo)} engine tasks ({len(tasks) - len(todo)} from "
                      "the checkpoint)", flush=True)
        done = {id(j): o for t, r in zip(tasks, res) for j, o in zip(t, r)}
        return [done[id(j)] for j in jobs]


CACHE_NAME = "netfit_cache.jsonl"


def fetch_cache(blob: str, digest: str, dest: str, account: str | None = None, store=None) -> str:
    """Download a stored checkpoint (CACHE_NAME) into `dest` and return `dest`, refused (ValueError) unless its bytes'
    sha256 is `digest`; load_cache then checks every row."""
    import hashlib
    if store is None:
        if os.path.join(ROOT, "scripts") not in sys.path:
            sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import results_store as store
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, CACHE_NAME)
    store.download(blob, path, account)
    got = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if got != digest:              # refused; the file stays in the scratch directory and nothing reads it
        raise ValueError(f"checkpoint {blob}: sha256 {got} is not the recorded {digest}")
    return dest


def no_rerun(runner, what: str) -> None:
    """Refuse (ValueError) once `runner` has run any engine task: a verification round reruns no design point."""
    n = int(getattr(runner, "ran_tasks", 0) or 0)
    if n:
        raise ValueError(f"{what}: {n} engine task(s) were not in the checkpoint; a verification round reruns no "
                         "design point (stopped before verifying)")


def _safe_io():
    if os.path.join(ROOT, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from lib import safe_io
    return safe_io


def _plain(x):
    return x.tolist() if hasattr(x, "tolist") else repr(x)


def _jdump(x) -> str:
    return json.dumps(x, sort_keys=True, default=_plain)


def _rdump(x) -> str:
    return json.dumps(x, default=_plain)


def _sha(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def _task_key(code: str, task) -> str:
    """A task's key: the engine code and every job's full parameter tuple (fitted values and the calibrated unit
    attribute included, so the existing cache key's calibration is part of it)."""
    return _sha(_jdump([code, task]))


def load_cache(d: str, code: str, accepted: list | None = None) -> dict:
    """{key: result JSON} of <d>/CACHE_NAME. Refused (ValueError, fail closed): no cache file; a malformed row; a row
    from other engine code; a key that is not its task's; a result whose sha256 is not the recorded one. A last row
    without its newline (cut by the stop) is dropped."""
    p = os.path.join(d, CACHE_NAME)
    if not os.path.isfile(p):
        raise ValueError(f"--resume-from {d}: no cache file {CACHE_NAME}")
    lines = open(p).read().split("\n")
    if lines[-1]:
        print(f"  {p}: dropped a last row cut before its newline", flush=True)
    out = {}
    for n, ln in enumerate(lines[:-1], 1):
        try:
            row = json.loads(ln)
        except ValueError as ex:
            raise ValueError(f"{p}: row {n} is malformed ({ex})") from ex
        if not isinstance(row, dict):
            raise ValueError(f"{p}: row {n} is malformed (not an object)")
        if row.get("code_sha256") != code:
            raise ValueError(f"{p}: row {n} is from engine code_sha256 {row.get('code_sha256')}, this engine's is {code}")
        if row.get("key") != _task_key(code, row.get("task")):
            raise ValueError(f"{p}: row {n}'s key is not its task's")
        s = _rdump(row.get("result"))
        if _sha(s) != row.get("result_sha256"):
            raise ValueError(f"{p}: row {n}'s result does not match its result_sha256")
        out[row["key"]] = s
        if accepted is not None:      # the accepted rows as read (checkpoint() rewrites them)
            accepted.append(ln)
    return out


def _task_s(k: int) -> float:
    """Laptop seconds of one engine task: a burn-in group of k arms of one setting at one point and seed."""
    return GROUP_S + k * ARM_S


def _chunk(workers: int) -> int:
    """Tasks per fresh process pool: 300 (worker memory, the spike's limit), and at least 4 per worker so a large VM is
    not idled at each chunk's tail (owner, 18 Nov: one VM of 96 vCPUs)."""
    return max(300, 4 * int(workers))


def _waves(n_tasks: int, vcpus: int, chunk: int | None = None) -> int:
    """RecalRunner._map runs tasks in chunks of _chunk(vcpus), each on a fresh pool of `vcpus` workers."""
    chunk = _chunk(vcpus) if chunk is None else chunk
    return sum(-(-min(chunk, n_tasks - i) // vcpus) for i in range(0, n_tasks, chunk))


WAVE1_POINTS = 46 + VERIFY + 1        # wave 1's engine-evaluated points: design, verified sample points, chosen point


def plan(pack: dict, verify: int = VERIFY, cal_n: int = CAL_N, vcpus: int = VCPUS, wave2_n: int = OPTION_A["wave2_n"],
         wave1_n: int = OPTION_A["design_n"] + 2, verify_round: bool = False, refit_b: bool | None = None) -> dict:
    """Run counts and the price of the allocated VM (review round 2): vcpus x the wall time of each _map wave
    (calibration: cal_n calls of 42 single-arm tasks, none when wave 1's grid is reused; design: the wave-2 points,
    plus wave 1's when it runs here (a smoke); the chosen point: one call per training setting; the sample: one call),
    at VM_FACTOR x the laptop task time, plus START_H and POOL_S per pool. The defaults are option A's fresh fit (the
    calibration grid, wave 1 and wave 2 all run; wave 1's count includes benchmark.fit_joint's centre point and its own
    chosen point, Codex r1 P1-3 on #269); a verification round reuses wave 1's grid and points. Refused (ValueError)
    for a verification count below 1 or a verification round on a pack other than the one it ran on."""
    if isinstance(verify, bool) or not isinstance(verify, int) or verify < 1:
        raise ValueError(f"verify must be a whole number of joint-sample points >= 1, not {verify!r}")
    if verify_round and list(pack["fit_joint"]["targets"]) != WAVE1_TARGETS:
        raise ValueError(f"a verification round reuses wave 2's points, run on WAVE1_TARGETS {WAVE1_TARGETS}; this "
                         f"pack's targets are {pack['fit_joint']['targets']}")
    rb = bool(OPTION_A.get("refit_b")) and not verify_round if refit_b is None else bool(refit_b)
    if rb:      # refit b: calibration, wave 1 and wave 2 come from the stored checkpoint; new: wave 1's re-chosen point,
        ext = (OPTION_A["refit_b"] or {}).get("extension") or {}       # the extension, the chosen point, the sample
        cal_n, wave1_n, wave2_n = 0, 1, int(ext.get("n") or 0)
    fj, byid = pack["fit_joint"], {b["id"]: b for b in pack["benchmarks"]}
    arms = sorted({a for t in fj["targets"] for a in BM._arms_of(byid[t]["estimand"])})
    settings = _fit_settings(pack)
    if rb:      # the monitored arms run with their setting (RecalRunner.prefetch), so they are priced too
        arms = sorted(set(arms) | {a for t in OPTION_A.get("monitor") or [] for a in BM._arms_of(byid[t]["estimand"])
                                   if pack["arms"][a]["setting"] in settings})
    per_set = max(sum(1 for a in arms if pack["arms"][a]["setting"] == s) for s in settings)
    cached = int(WAVE2["n"]) if verify_round else 0    # a verification round reads wave 2's points from its cache
    wave2_n, wave1_n, cal_n = (0, 0, 0) if verify_round else (wave2_n, wave1_n, cal_n)
    npts, ns = int(wave1_n) + int(wave2_n), len(pack["seeds"])
    grid = sum(len(pack["settings"][s]["calibrate"]["grid"]) for s in settings if pack["settings"][s].get("calibrate"))
    cal_tasks = grid * min(2, ns)
    cal = cal_n * cal_tasks                         # the calibration grid in CAL_PARAM; each run its own burn-in
    pts = npts + 1 + verify                         # design, the chosen value, the verified joint-sample points
    groups, sims = pts * len(settings) * ns, pts * len(arms) * ns
    cpu_h = (groups * GROUP_S + sims * ARM_S + cal * (GROUP_S + ARM_S)) / 3600
    dtasks = npts * len(settings) * ns
    batches = [n * len(settings) * ns for n in (int(wave1_n), int(wave2_n)) if n]     # each design batch runs alone
    waves = {"calibration": cal_n * _waves(cal_tasks, vcpus), "design": sum(_waves(t, vcpus) for t in batches),
             "chosen": (2 if wave1_n else 1) * len(settings) * _waves(ns, vcpus),
             "sample": _waves(verify * len(settings) * ns, vcpus)}
    pools = cal_n + len(settings) + sum(-(-n // 300) for n in (*batches, verify * len(settings) * ns))
    lap = waves["calibration"] * _task_s(1) + (waves["design"] + waves["chosen"] + waves["sample"]) * _task_s(per_set)
    wall = [lap * f / 3600 + START_H + pools * POOL_S / 3600 for f in VM_FACTOR]
    vh = [vcpus * w for w in wall]
    _, held = split(pack)
    return {"params": fj["params"], "targets_train": fj["targets"], "held_out_untouched": held,
            "not_fitted": sorted(set(NET_PARAMS) - set(fj["params"])), "calibration_depends_on": sorted(
                calibration_params(pack)), "arms": arms, "settings": settings,
            "design_points": int(wave2_n), "design_points_from_cache": cached, "wave1_points_run": int(wave1_n),
            "reused_points": 0 if wave1_n else WAVE1_POINTS, "calibration_reused": not cal_n,
            "verified_sample_points": verify, "seeds": ns, "calibration_runs": cal,
            "arm_runs": sims + cal, "burn_in_groups": groups + cal, "cpu_work_hours": round(cpu_h, 1),
            "vcpus": vcpus, "waves": waves, "wall_hours": [round(w, 2) for w in wall],
            "vm_vcpu_hours": [round(v, 1) for v in vh],
            "usd": [round(vh[0] * USD_PER_VCPU_H[0], 1), round(vh[1] * USD_PER_VCPU_H[1], 1)],
            "usd_real": [round(v * USD_REAL_PER_VCPU_H, 1) for v in vh], "refit_b": rb,
            "launch": None if verify_round else shlex.join(launch_command(vcpus, wall[1], verify, rb))}


def launch_command(vcpus: int = VCPUS, wall_hours: float | None = None, verify: int = VERIFY,
                   refit_b: bool = False) -> list:
    """The option A launch (F6, OA3): one VM through cloud/vm_job.py, whose job meter stops the VM at --cap-usd in USD
    (cloud/budget.py); the fit writes a copy of its output under $JOB_OUT. Until the owner sets OPTION_A["cap_usd"] and
    gives the approval text, the command carries placeholders that vm_job and `run` refuse (fail closed). The
    verification round's launch (owner, 14 Nov: cap $15, job f6-refit-netfit-v017r2, store round2) ran and is history.
    --max-hours covers the plan's upper wall time plus an hour (vm_job's default is 24 h; Codex r1 P1-4 on #269)."""
    cap = OPTION_A["cap_usd"]
    hours = "<plan wall hours + 1>" if wall_hours is None else str(int(math.ceil(float(wall_hours) + 1)))
    if refit_b:     # the tree plus the stored checkpoint, shipped as one directory; the fit resumes from it
        rd = OPTION_A["refit_b"]["resume_dir"]
        return ["python3", "cloud/vm_job.py", "run", OPTION_A["job"], "net-effect-fit",
                "--dir", f"<git archive of the launch commit, plus {rd}/{CACHE_NAME}>",
                "--store", OPTION_A["store"], "--store-account", "modelingagentic",
                "--cap-usd", "<owner cap>" if cap is None else f"{cap:g}",
                "--budget-scope", "f6", "--cpu", str(vcpus), "--memory", f"{7 * vcpus} GiB", "--max-hours", hours,
                "--", "python3", "toolkit/engines/malaria/benchmarks/net_effect_fit.py", "run",
                "--approved", "<owner approval: option A refit b, its cap>", "--resume-from", rd,
                "--verify", str(int(verify)), "--workers", str(vcpus)]
    return ["python3", "cloud/vm_job.py", "run", OPTION_A["job"], "net-effect-fit", "--tree", ".",
            "--store", OPTION_A["store"], "--store-account", "modelingagentic",
            "--cap-usd", "<owner cap>" if cap is None else f"{cap:g}",
            "--budget-scope", "f6", "--cpu", str(vcpus), "--memory", f"{7 * vcpus} GiB", "--max-hours", hours, "--",
            "python3", "toolkit/engines/malaria/benchmarks/net_effect_fit.py", "run",
            "--approved", "<owner approval: option A net-effect fit, its cap>", "--margin-loo",
            "--verify", str(int(verify)), "--workers", str(vcpus)]


def target_bands(pack: dict) -> dict:
    """{target: band} of the fit's targets (a held-out benchmark, such as a curve rule, may have no band)."""
    t = set(pack["fit_joint"]["targets"])
    return {b["id"]: b["band"] for b in pack["benchmarks"] if b["id"] in t}


def joint_region(X: np.ndarray, Y: dict, bands: dict, names: list, box: np.ndarray, seed: int,
                 n_sample: int = SAMPLE, margin: dict | None = None, support: list | None = None) -> dict:
    """Each target's quadratic surrogate (as joint-v1) on dense candidates; the region where every target is in band:
    its share of the box, its marginal hull and box-edge flags (from every accepted candidate), and a joint sample of
    its points (parameter sets, not marginals). `margin` {target: log margin} (owner, 13 Nov, off by default): a
    candidate is accepted only if its surrogate value is inside each band by the margin (in log); the in-band
    candidates it excludes are recorded (count, share, ranges), a declared limitation of the sample. `support`: a list
    of boxes {name: [lo, hi]} (the waves' design boxes); candidates outside their union are dropped, so the surrogate
    is never extrapolated past the engine-evaluated points (wave 2's finding)."""
    cand = BM._candidates(X.shape[1], seed)
    n_all, sup_extra = len(cand), {}
    if support is not None:
        keep = in_support(cand, support, names, box)
        sup_extra = {"support": {"boxes": support, "share_of_box": round(float(keep.mean()), 4),
                                 "rule": "candidates inside the union of the waves' design boxes"}}
        cand = cand[keep]
    Fc = BM._quad_features(cand)
    m, plain = np.ones(len(cand), bool), np.ones(len(cand), bool)
    for t, y in Y.items():
        coef, _ = BM._quad_fit(X, np.log(np.maximum(np.asarray(y, float), 1e-12)))
        lv = Fc @ coef
        lo, hi = np.log(bands[t][0]), np.log(bands[t][1])
        plain &= (lv >= lo) & (lv <= hi)
        mt = float((margin or {}).get(t, 0.0))
        m &= (lv >= lo + mt) & (lv <= hi - mt)
    to = lambda c: {k: round(float(box[i, 0] + c[i] * (box[i, 1] - box[i, 0])), 6) for i, k in enumerate(names)}  # noqa
    scale = lambda a: {k: [round(float(box[i, 0] + a[:, i].min() * (box[i, 1] - box[i, 0])), 6),  # noqa: E731
                           round(float(box[i, 0] + a[:, i].max() * (box[i, 1] - box[i, 0])), 6)]
                       for i, k in enumerate(names)} if len(a) else None
    extra = {}
    if margin is not None:
        out = plain & ~m
        extra = {"margin": {"rule": "surrogate value inside each band by the target's local leave-one-out error (log)",
                            "by_target": {t: round(float(v), 6) for t, v in margin.items()},
                            "n_in_band_without_margin": int(plain.sum()), "n_excluded": int(out.sum()),
                            "share_excluded": round(float(out.sum() / max(plain.sum(), 1)), 4),
                            "in_band_hull_without_margin": scale(cand[plain]), "excluded_ranges": scale(cand[out])}}
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
    return {"share_of_box": round(float(m.sum() / n_all), 4), "n_accepted": int(m.sum()), "hull": hull, "at_box_edge": edge,
            "correlation": {"names": names, "matrix": corr}, **extra, **sup_extra, "sample": [to(cand[i]) for i in idx]}


def in_support(cand: np.ndarray, support: list, names: list, box: np.ndarray) -> np.ndarray:
    """Mask of normalised candidates inside the union of the boxes in `support` (parameter units)."""
    theta = box[:, 0] + cand * (box[:, 1] - box[:, 0])
    m = np.zeros(len(cand), bool)
    for bx in support:
        lo = np.array([bx[k][0] for k in names], float) - 1e-9
        hi = np.array([bx[k][1] for k in names], float) + 1e-9
        m |= np.all((theta >= lo) & (theta <= hi), axis=1)
    return m


def loo_residuals(X: np.ndarray, y) -> np.ndarray:
    """Each point's leave-one-out residual of the quadratic surrogate (as benchmark._quad_fit computes its RMSE)."""
    F, y = BM._quad_features(X), np.asarray(y, float)
    out = []
    for i in range(len(y)):
        k = np.arange(len(y)) != i
        c, *_ = np.linalg.lstsq(F[k], y[k], rcond=None)
        out.append(float(F[i] @ c - y[i]))
    return np.asarray(out)


def local_loo(Ylog: dict, X: np.ndarray, local) -> dict:
    """{target: root mean square leave-one-out residual of its log surrogate over the `local` points} (the margin)."""
    local = np.asarray(local, bool)
    return {t: round(float(np.sqrt(np.mean(np.square(loo_residuals(X, y)[local])))), 6) for t, y in Ylog.items()}


def wave2_box(hull: dict, old: dict, new: dict, margin: float) -> dict:
    """Wave 1's accepted hull widened by `margin` of its width on each side, clipped to the new box; a side where the
    hull reached the old box's edge (within 1e-3 of the box width) goes to the new box's edge."""
    out = {}
    for k, (lo, hi) in hull.items():
        (olo, ohi), (nlo, nhi), w = old[k], new[k], hi - lo
        tol = 1e-3 * (ohi - olo)
        a = nlo if lo <= olo + tol else max(nlo, lo - margin * w)
        b = nhi if hi >= ohi - tol else min(nhi, hi + margin * w)
        out[k] = [round(float(a), 6), round(float(b), 6)]
    return out


def load_wave1(path: str, engine, base: dict, digest: str = WAVE1_SHA256, pack: dict | None = None) -> dict:
    """Wave 1's engine results, for reuse (no engine run repeated): every engine-evaluated point with its target values
    (46 design points, the verified sample points, the chosen point) and the calibration grid. Refused (ValueError,
    fail closed) unless the file's sha256 is `digest`, it is a full run (not a smoke) of this script, its engine
    identity (name, code, parameters, configuration) is this engine's, the pack's fit declarations are the ones it ran
    on (held-out benchmarks may have been added), its seeds and
    targets are the pack's, its calibration grid is this script's (CAL_N points across the kill shift's box) and does
    not clamp, and every point has a value for every target."""
    import hashlib
    from toolkit.engines.contract import engine_identity
    data = open(path, "rb").read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"wave 1 {path}: sha256 {hashlib.sha256(data).hexdigest()} is not the recorded {digest}")
    doc = yaml.safe_load(data)
    if not isinstance(doc, dict) or doc.get("producer") != BM.PRODUCER or doc.get("smoke") is not False:
        raise ValueError(f"wave 1 {path}: not a full (non-smoke) output of net_effect_fit.py run")
    have, want = doc.get("engine_identity") or {}, engine_identity(engine)
    for k in ("name", "code_sha256", "params_sha256", "config_sha256"):
        if have.get(k) != want[k]:
            raise ValueError(f"wave 1 {path}: engine {k} {have.get(k)}, this engine's is {want[k]}")
    if base != yaml.safe_load(open(PACK)):
        raise ValueError(f"wave 1 {path}: the pack given is not pack.yaml")
    eff = build_pack(base, targets=WAVE1_TARGETS) if pack is None else pack   # the runner's pack (Codex r1 on #266)
    if _fit_digest(eff) != WAVE1_FIT_DECLARATIONS_SHA256:
        raise ValueError(f"wave 1 {path}: the effective pack's fit declarations (sha256 {_fit_digest(eff)}) are not "
                         f"the declarations wave 1 ran on ({WAVE1_FIT_DECLARATIONS_SHA256})")
    fr, train = doc.get("fit_rule") or {}, split(base)[0]
    if list(fr.get("seeds") or []) != list(base["seeds"]):
        raise ValueError(f"wave 1 {path}: seeds {fr.get('seeds')}, the pack's are {base['seeds']}")
    if list(fr.get("targets") or []) != train:
        raise ValueError(f"wave 1 {path}: targets {fr.get('targets')}, the pack's are {train}")
    cg = doc.get("calibration_grid") or {}
    grid = [float(v) for v in np.linspace(*NET_PARAMS[CAL_PARAM][1], CAL_N)]
    vals = [float(v) for v in cg.get(CAL_PARAM) or []]
    if len(vals) != len(grid) or any(abs(a - b) > 1e-9 for a, b in zip(vals, grid)):
        raise ValueError(f"wave 1 {path}: calibration grid {vals} is not this script's {grid}")
    if cg.get("at_grid_edge") != []:
        raise ValueError(f"wave 1 {path}: the calibration grid clamps at its edge for {cg.get('at_grid_edge')}")
    m0 = cg.get("m0") or {}
    if sorted(m0) != _fit_settings(base) or any(len(v) != CAL_N for v in m0.values()):
        raise ValueError(f"wave 1 {path}: calibration grid m0 for {sorted(m0)}, the fit's sites are "
                         f"{_fit_settings(base)}")
    des, jr = doc.get("design") or {}, doc.get("joint_region") or {}
    ver = jr.get("verified") or {}
    comp = des.get("computed") or {}
    at = lambda t, i: comp[t][i] if isinstance(comp.get(t), list) and i < len(comp[t]) else None  # noqa: E731
    pts = [(x, {t: at(t, i) for t in train}) for i, x in enumerate(des.get("x") or [])]
    pts += [(p.get("point"), p.get("computed") or {}) for p in ver.get("points") or []]
    ch = (ver.get("chosen") or {}).get("point")
    pts += [(ch, {t: (doc.get("verification") or {}).get(t, {}).get("computed") for t in train})]
    names = list(WAVE1_BOX)
    out = []
    for x, y in pts:
        if not isinstance(x, dict) or sorted(x) != sorted(names) or any(
                not isinstance(y.get(t), (int, float)) or not y[t] > 0 for t in train):
            raise ValueError(f"wave 1 {path}: a point without its result for every target: {x} {y}")
        out.append({"x": {k: float(x[k]) for k in names}, "wave": 1, "computed": {t: float(y[t]) for t in train}})
    return {"path": path, "sha256": digest, "points": out, "hull": jr.get("hull"), "at_grid_edge": [],
            "calibration_grid": {"values": vals, "m0": {s: [float(v) for v in m0[s]] for s in m0}}}


def _fit_digest(pack: dict) -> str:
    """sha256 of what a fit's engine runs depend on: each training target's declaration and the fit rule."""
    import hashlib
    from toolkit.engines.benchmark_sources import fit_declarations
    return hashlib.sha256(json.dumps(fit_declarations(pack), sort_keys=True).encode()).hexdigest()


def fit_waves(pack: dict, runner, wave1: dict, box2: dict, n: int = WAVE2["n"], seed: int = WAVE2["seed"],
              support: list | None = None, cand_seed: int | None = None, after_design=None,
              extra: list | None = None) -> dict:
    """The joint fit on two waves (rule joint-v1-wave2): joint-v1's steps (quadratic surrogate of each target's log
    value, argmin of (n out of band, band-scaled squared log error) on the dense candidates, the range, verification at
    the chosen value) on wave 1's reused points plus `n` new LHS points in `box2`; only the new points and the chosen
    point run on the engine. Returns benchmark.fit_joint's keys; the design records each point's wave."""
    import itertools
    spec = pack["fit_joint"]
    byid = {b["id"]: b for b in pack["benchmarks"]}
    names, targets = list(spec["params"]), list(spec["targets"])
    box = np.array([spec["params"][k] for k in names], float)
    d = len(names)
    to_theta = lambda x: {k: round(float(box[j, 0] + x[j] * (box[j, 1] - box[j, 0])), 6) for j, k in enumerate(names)}  # noqa: E731
    b2 = np.array([box2[k] for k in names], float)
    new = [{k: round(float(b2[j, 0] + u[j] * (b2[j, 1] - b2[j, 0])), 6) for j, k in enumerate(names)}
           for u in BM.lhs(int(n), d, int(seed))] + [dict(x) for x in (extra or [])]     # refit b: the extension
    arms = sorted({a for t in targets for a in BM._arms_of(byid[t]["estimand"])})
    t0 = time.time()
    k_ext = len(extra or [])        # refit b: the stored wave-2 batch alone (its tasks as ran), then the extension
    runner.prefetch(arms, new[:len(new) - k_ext])
    if k_ext:
        runner.prefetch(arms, new[len(new) - k_ext:])
    print(f"wave 2: {len(new)} design points x {len(arms)} arms x {len(runner.seeds)} seeds in {time.time() - t0:.0f}s"
          f" (wave 1: {len(wave1['points'])} points reused)", flush=True)
    Y2 = {t: [float(runner.evaluate(byid[t], th)["computed"]) for th in new] for t in targets}
    if after_design is not None:
        after_design()
    thetas = [p["x"] for p in wave1["points"]] + new
    wave = [1] * len(wave1["points"]) + [2] * len(new)
    Y = {t: np.array([p["computed"][t] for p in wave1["points"]] + Y2[t]) for t in targets}
    X = np.array([[(th[k] - box[j, 0]) / (box[j, 1] - box[j, 0]) for j, k in enumerate(names)] for th in thetas])
    surr, cand = {}, BM._candidates(d, int(seed if cand_seed is None else cand_seed))
    if support is not None:          # the choice and its range stay where engine-evaluated points are
        cand = cand[in_support(cand, support, names, box)]
    Fc = BM._quad_features(cand)
    pred, inband = {}, {}
    for t in targets:
        coef, loo = BM._quad_fit(X, np.log(np.maximum(Y[t], 1e-12)))
        surr[t] = {"loo_rmse_log": round(loo, 4), "n_points": len(thetas)}
        pred[t] = np.exp(Fc @ coef)
        lo, hi = byid[t]["band"]
        inband[t] = (pred[t] >= lo) & (pred[t] <= hi)
    feasible = [t for t in targets if inband[t].any()]
    conflicts = [[a, b] for a, b in itertools.combinations(feasible, 2) if not (inband[a] & inband[b]).any()]
    n_out = sum((~inband[t]).astype(int) for t in feasible) if feasible else np.zeros(len(cand))
    L = sum(np.array([BM._loss(byid[t], v) for v in pred[t]]) for t in feasible) if feasible else np.zeros(len(cand))
    j = int(np.argmin(n_out * 1e6 + L))
    best = to_theta(cand[j])
    region = cand[n_out == 0] if (n_out == 0).any() else cand[L <= max(len(feasible), 1)]
    if not len(region):
        region = cand[j:j + 1]
    rng_ = {k: (round(float(box[i, 0] + region[:, i].min() * (box[i, 1] - box[i, 0])), 4),
                round(float(box[i, 0] + region[:, i].max() * (box[i, 1] - box[i, 0])), 4)) for i, k in enumerate(names)}
    settings = sorted({pack["arms"][a]["setting"] for a in arms})
    runner.calibrate_settings(best, settings)
    cal_best = {s: round(runner.calibrated[s]["value"], 4) for s in settings if s in runner.calibrated}
    runner.prefetch(arms, [best])
    ver = {}
    for t in targets:
        r = runner.evaluate(byid[t], best)
        ver[t] = {"computed": round(r["computed"], 4), "predicted": round(float(pred[t][j]), 4),
                  "in_band": r["in_band"], "seed_min": r.get("seed_min"), "seed_max": r.get("seed_max"),
                  "per_seed": list(r.get("per_seed") or [])}
    params = {k: {"value": best[k], "lo": min(rng_[k][0], best[k]), "hi": max(rng_[k][1], best[k]),
                  "box": [float(box[i, 0]), float(box[i, 1])], "benchmarks": targets} for i, k in enumerate(names)}
    for k, v in params.items():
        print(f"fit_waves {k}: {v['value']} [{v['lo']}, {v['hi']}]", flush=True)
    return {"parameters": params,
            "fit_rule": {"version": "joint-v1-wave2", "description": JOINT_WAVE2, "seeds": list(runner.seeds),
                         "design_points": len(thetas), "wave2_points": len(new), "wave2_seed": int(seed),
                         "wave2_box": box2, "support": support, "targets": targets},
            "feasibility": {"feasible_in_box": feasible, "infeasible_in_box": [t for t in targets if t not in feasible],
                            "conflicting_pairs": conflicts,
                            "all_feasible_in_band_at_value": bool(feasible) and all(ver[t]["in_band"] for t in feasible),
                            "surrogate_all_feasible_in_band": bool(n_out[j] == 0),
                            "disproved_by_verification": [t for t in feasible if inband[t][j] and not ver[t]["in_band"]],
                            "feasibility_verified": all(ver[t]["in_band"] == bool(inband[t][j]) for t in targets)},
            "surrogate": surr, "calibration": {"at_fitted_value": cal_best}, "verification": ver,
            "design": {"x": [dict(th) for th in thetas], "wave": wave,
                       "computed": {t: [round(float(v), 4) for v in Y[t]] for t in targets}}}


JOINT_WAVE2 = ("joint-v1's surrogate, choice, range and verification on two waves: wave 1's engine-evaluated points "
               "(reused from its output) plus an LHS of new points in wave 1's accepted hull widened by a stated "
               "margin (to the new box edge where the hull reached the old box edge); every point calibrated by "
               "interpolation on wave 1's calibration grid in net_kill_shift")


def extension_points(design_box: dict, ext: dict | None) -> list:
    """Refit b's extension wave: an LHS of ext["n"] points (seed ext["seed"]) with ext["param"] over ext["range"] and
    every other parameter over the design box; [] when no extension is set."""
    if not ext:
        return []
    names = list(design_box)
    bx = {k: (list(ext["range"]) if k == ext["param"] else list(design_box[k])) for k in names}
    return [{k: round(float(bx[k][0] + u[j] * (bx[k][1] - bx[k][0])), 6) for j, k in enumerate(names)}
            for u in BM.lhs(int(ext["n"]), len(names), int(ext["seed"]))]


def edge_diagnostic(point: dict) -> dict:
    """Owner, 19 Nov: whether the chosen pbo_adv_decay sits within 2% of the refit-b ceiling; if so, the engine cannot
    reproduce LLINEUP's waning inside the widened prior, and the report says so."""
    v, c = float(point[PAD_CEILING["param"]]), float(PAD_CEILING["hi"])
    near = v >= c * 0.98
    line = (f"pbo_adv_decay {v:.3f} is within 2% of its ceiling {c:g}: the engine cannot reproduce LLINEUP's waning "
            f"inside the widened prior" if near else f"pbo_adv_decay {v:.3f} is below 98% of its ceiling {c:g}")
    return {"value": v, "ceiling": c, "within_2pct": near, "line": line}


def calibration_ok(cal: dict) -> list:
    """Sites whose control arm is off its calibration target by more than CAL_TOL (relative)."""
    return sorted(s for s, c in (cal or {}).items()
                  if not abs(float(c["achieved"]) / float(c["target"]) - 1.0) <= CAL_TOL)


Z95 = 1.96
ACCEPTANCE_RULE = ("owner, 14 Nov: a verified point, and the chosen point, is in band for a target when the 95% interval "
                   "of its seed mean overlaps the target's band. The seed mean is the geometric mean over the run's "
                   "seeds, the statistic of the target's `computed` value; its interval is exp(mean(log v) +- 1.96 x "
                   "sd(log v) / sqrt(n_seeds)) (sd with n - 1). The point must hold exactly one finite, positive value "
                   "per seed the run declares (pack.yaml's seeds, which the run records); a missing, extra or "
                   "duplicate seed, or a target without values, fails closed. Control arms within 10% and no "
                   "calibration-grid clamp are unchanged")
ACCEPTANCE_STATISTIC = ("geometric mean over seeds; 95% interval exp(mean(log v) +- 1.96 sd(log v)/sqrt(n)), "
                        "one value per declared seed")


def declared_seeds(seeds) -> list:
    """The run's declared seeds as a list of distinct integers; ValueError otherwise (fail closed)."""
    if not isinstance(seeds, (list, tuple)) or not seeds:
        raise ValueError(f"no declared seeds ({seeds!r})")
    if any(isinstance(x, bool) or not isinstance(x, int) for x in seeds):
        raise ValueError(f"declared seeds must be integers ({list(seeds)!r})")
    dup = sorted({x for x in seeds if list(seeds).count(x) > 1})
    if dup:
        raise ValueError(f"duplicate declared seeds {dup} in {list(seeds)}")
    return list(seeds)


def seed_interval(values, n_seeds: int) -> tuple[float, float] | None:
    """(lo, hi) of the seed geometric mean's 95% interval, exp(mean(log v) +- Z95 sd(log v)/sqrt(n)); None unless
    there are exactly `n_seeds` (>= 2) values, all finite and positive."""
    if not isinstance(values, (list, tuple)):
        return None
    try:
        v = [float(x) for x in values]
        n_seeds = int(n_seeds)
    except (TypeError, ValueError):
        return None
    if n_seeds < 2 or len(v) != n_seeds or not all(np.isfinite(v)) or min(v) <= 0:
        return None
    lg = np.log(v)
    m, h = float(np.mean(lg)), Z95 * float(np.std(lg, ddof=1)) / float(np.sqrt(len(v)))
    return float(np.exp(m - h)), float(np.exp(m + h))


def ci_in_band(values, band, n_seeds: int) -> bool:
    """ACCEPTANCE_RULE for one target: the seed geometric mean's 95% interval overlaps the band."""
    iv = seed_interval(values, n_seeds)
    return iv is not None and iv[0] <= float(band[1]) and iv[1] >= float(band[0])


def _per_seed(p) -> dict:
    per = (p or {}).get("per_seed") if isinstance(p, dict) else None
    return per if isinstance(per, dict) else {}


def seed_count_errors(p: dict, bands: dict, seeds) -> list:
    """Targets whose seed values are not one per declared seed, as readable reasons."""
    per, n = _per_seed(p), len(seeds) if isinstance(seeds, (list, tuple)) else 0
    out = []
    for t in bands:
        v = per.get(t)
        k = len(v) if isinstance(v, (list, tuple)) else 0
        if k != n:
            out.append(f"{t}: {k} seed values for {n} declared seeds")
    return out


def point_in_band(p: dict, bands: dict, seeds) -> tuple[bool, dict]:
    """(every target in band under ACCEPTANCE_RULE, {target: [interval lo, hi] or None}) from the point's own seed
    evidence `per_seed` {target: [values]}, one value per declared seed in `seeds`; a target without exactly that
    evidence, or an invalid seed declaration, fails the point."""
    try:
        n = len(declared_seeds(seeds))
    except ValueError:
        return False, {t: None for t in bands}
    per = _per_seed(p)
    ivs = {t: seed_interval(per.get(t) or [], n) for t in bands}
    return all(ci_in_band(per.get(t) or [], b, n) for t, b in bands.items()), {
        t: [round(v[0], 4), round(v[1], 4)] if v else None for t, v in ivs.items()}


def pack_bands() -> dict:
    """The fit's targets' bands from pack.yaml (the loader's replay uses these, never bands written in the file)."""
    return target_bands(build_pack(yaml.safe_load(open(PACK))))         # the option A declaration's bands


def pack_seeds() -> list:
    """The fit's seeds from pack.yaml (the loader's replay uses these; the file's recorded seeds must match)."""
    return list(build_pack(yaml.safe_load(open(PACK)))["seeds"])


def finalize(reg: dict, checked: list, chosen: dict | None = None, grid_edge=(), bands: dict | None = None,
             seeds=None) -> tuple[dict, int]:
    """The acceptance gate (review rounds 1-2; owner, 14 Nov: ACCEPTANCE_RULE). The sample is `verified` (code 0)
    only if every verified point and the chosen point are in band for every target under the rule, from their own
    seed evidence (the summary flags are not evidence), their control arms are within CAL_TOL of the calibration
    targets, and no site's calibration grid clamps at its edge. `bands`: the targets' bands (default pack.yaml's);
    `seeds`: the run's declared seeds, one seed value each per target (default pack.yaml's).
    Otherwise the sample moves to `sample_unverified`, `sample` is emptied, the reasons are recorded, and the code is
    2. run() and load_posterior's replay both call this function."""
    bands = pack_bands() if bands is None else bands
    seeds = pack_seeds() if seeds is None else seeds
    why = []
    try:
        declared_seeds(seeds)
    except ValueError as ex:
        why.append(f"the run's seeds cannot be checked against: {ex}")
    for c in checked:
        c["in_band_by_rule"], c["seed_mean_interval"] = point_in_band(c, bands, seeds)
    failed = [c["point"] for c in checked if not c["in_band_by_rule"] or calibration_ok(c.get("calibration_check"))]
    why += [f"verified point out of band (rule) or off calibration: {p}" for p in failed]
    for c in checked:
        if seed_count_errors(c, bands, seeds):
            why.append(f"verified point without one seed value per declared seed at {c.get('point')}: "
                       f"{seed_count_errors(c, bands, seeds)}")
    for c in checked:
        if calibration_ok(c.get("calibration_check")):
            why.append(f"control arms off target by more than {CAL_TOL:.0%} at {c['point']}: "
                       f"{calibration_ok(c.get('calibration_check'))}")
    if chosen is None:
        why.append("the chosen point's verification is missing")
    else:
        chosen["in_band_by_rule"], chosen["seed_mean_interval"] = point_in_band(chosen, bands, seeds)
        if not chosen["in_band_by_rule"]:
            why.append(f"the chosen point fails its verification under the rule: {chosen['seed_mean_interval']}")
        if seed_count_errors(chosen, bands, seeds):
            why.append(f"the chosen point lacks one seed value per declared seed: "
                       f"{seed_count_errors(chosen, bands, seeds)}")
        if calibration_ok(chosen.get("calibration_check")):
            why.append(f"the chosen point's control arms are off target: {calibration_ok(chosen['calibration_check'])}")
    if grid_edge:
        why.append(f"the calibration grid clamps at its edge for {sorted(grid_edge)}")
    if not checked:
        why.append("no verified sample point")
    reg["verified"] = {"n": len(checked), "all_in_band": len(checked) - len(failed), "failed": failed,
                       "points": checked, "chosen": chosen, "calibration_tolerance": CAL_TOL,
                       "acceptance_rule": ACCEPTANCE_RULE, "acceptance_statistic": ACCEPTANCE_STATISTIC,
                       "seeds": list(seeds) if isinstance(seeds, (list, tuple)) else seeds,
                       "bands": {t: list(b) for t, b in bands.items()}}
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


PURPOSE = ("trial fit of the net-effect parameters (engine v0.18, F6 option A: the fit's own target declaration "
           "OPTION_A; ENGINE_NOTES.md, v0.17 and v0.18); "
           "adopted only through a signed spec's uncertainty.net_effect_posterior (load_posterior)")


def confounding(reg: dict, pairs=CONFOUNDED) -> list:
    """Owner, 18 Nov: each CONFOUNDED pair's correlation in the joint sample (the surrogate's all-in-band region), so
    the output shows how far the trials separate them; a pair not both fitted is left out."""
    names = list((reg.get("correlation") or {}).get("names") or [])
    rows = reg.get("sample") or reg.get("sample_unverified") or []       # the rows the decision analysis draws
    out = []
    for a, b in pairs:
        if a in names and b in names:
            r = None
            if len(rows) >= 3:
                x, y = (np.array([float(w[k]) for w in rows]) for k in (a, b))
                if x.std() > 0 and y.std() > 0:
                    r = round(float(np.corrcoef(x, y)[0, 1]), 4)
            out.append({"pair": [a, b], "correlation": r, "sample_n": len(rows),
                        "note": None if r is not None else "undefined: fewer than 3 exported rows or a constant column",
                        "hull": {k: (reg.get("hull") or {}).get(k) for k in (a, b)}})
    return out


def document(eng, reg: dict, q: dict, smoke: bool, **rest) -> dict:
    """The output file's document (run() writes it; load_posterior reads it): the plan's box, the joint region and
    the confounded pairs' joint posterior (confounding)."""
    return BM.stamp(dict({"purpose": PURPOSE, "smoke": smoke, "plan": q}, **rest, joint_region=reg,
                         confounding=confounding(reg)), eng)


def _fixture_placeholder(path: str, eng, rows: list, box: dict, gate_ok: bool = True, point=None) -> None:
    """A placeholder output in run()'s own format, through finalize() and document() (self-tests only). `point`: the
    chosen point's values (default the first row; False leaves the chosen point without one)."""
    ks = [(-1, 0, 1, 0)[i % 4] for i in range(len(pack_seeds()))]       # one seed value per pack seed
    mid = lambda f: {t: [round(float(np.sqrt(lo * hi)) * f * (1 + 0.01 * k), 4) for k in ks]  # noqa: E731
                     for t, (lo, hi) in pack_bands().items()}            # seed values around each band's middle
    pt = {"point": rows[0], "per_seed": mid(1.0 if gate_ok else 3.0), "calibration_check": {}}
    ch = {"per_seed": mid(1.0), "all_in_band": True, "disproved_by_verification": [], "calibration_check": {}}
    if point is not False:
        ch["point"] = dict(rows[0] if point is None else point)
    reg, _ = finalize({"sample": [dict(r) for r in rows]}, [pt], ch)
    fit = {"parameters": {k: {"value": v} for k, v in ch.get("point", {}).items()}}     # as run()'s **fit
    yaml.safe_dump(document(eng, reg, {"params": box}, False, calibration_grid={"at_grid_edge": []},
                            fit_rule={"seeds": pack_seeds()}, **fit), open(path, "w"), sort_keys=False)


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
    summary fields say (Codex r1 P1-2); a column is not in `ranges`; a value is outside its hard bounds (hard_bounds:
    the priors.yaml range, whatever `ranges` says, Codex r1 P2-3, and the bounds the card's units imply). The values
    of `ranges` are not a bound (owner, 14 Nov, f6-posterior-ranges): the sample's hull sets its columns' draw ranges
    and design box (posterior_ranges)."""
    import copy as _copy
    import hashlib
    from toolkit.engines.contract import engine_identity

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
    seeds, fr = pack_seeds(), doc.get("fit_rule")
    rec = fr.get("seeds") if isinstance(fr, dict) else None
    try:
        declared_seeds(rec)
    except ValueError as ex:
        no(f"the run's recorded seeds (fit_rule.seeds) are not a valid declaration: {ex}")
    if rec != seeds:
        no(f"the run's recorded seeds {rec} are not pack.yaml's {seeds}")
    try:
        rr, code = finalize({"sample": _copy.deepcopy(rows)}, _copy.deepcopy(pts), _copy.deepcopy(ver.get("chosen")),
                           edge, seeds=seeds)
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
    for j, k in enumerate(names):
        for lo, hi, what in hard_bounds(engine, [k], both=True).get(k, []):
            tol = 1e-6 * max(1.0, hi - lo) if math.isfinite(hi - lo) else 1e-6     # the sample is rounded to 6 decimals
            if X[:, j].min() < lo - tol or X[:, j].max() > hi + tol:
                no(f"{k} spans [{X[:, j].min()}, {X[:, j].max()}], outside its hard bound [{lo}, {hi}] ({what})")
    return {"path": os.path.abspath(path), "sha256": str(digest).lower(), "names": names, "rows": X}


# hard bounds a card's units imply (owner, 14 Nov, f6-posterior-ranges): a value outside them is not physical
UNIT_BOUNDS = {"probability": (0.0, 1.0), "share": (0.0, 1.0), "days": (0.0, math.inf), "months": (0.0, math.inf),
               "years": (0.0, math.inf)}
DURATIONS = ("days", "months", "years")     # a margin may not take one to zero (Codex r2 P2: NaN effects at 0)


def hard_bounds(engine, names, both: bool = False) -> dict:
    """{name: (lo, hi, what)}: each parameter's hard bounds from the engine, never a fitted or spec range. Two sources:
    the bounds its card units imply (UNIT_BOUNDS: a probability or share in [0, 1], a duration >= 0) and its
    priors.yaml range, the support of the engine's prior (Codex r1 P2-3: a spec prior wider than the card does not
    widen it). A parameter with neither has no entry. `both`: {name: [every bound]} for load_posterior's check;
    otherwise the intersection."""
    from toolkit.spec import priors as PR
    card = PR.load_card(engine)["priors"]
    units = {q.name: q.units for q in engine.parameters()}
    out = {}
    for k in names:
        b = ([(*UNIT_BOUNDS[units[k]], f"card units {units[k]}")] if units.get(k) in UNIT_BOUNDS else []) + (
            [(PAD_CEILING["lo"], PAD_CEILING["hi"], "refit b ceiling: " + PAD_CEILING["basis"])]
            if k == PAD_CEILING["param"] else
            [(float(card[k].lo), float(card[k].hi), "priors.yaml range")] if k in card else [])
        if b:
            out[k] = b if both else (max(x[0] for x in b), min(x[1] for x in b), "; ".join(x[2] for x in b))
    return out


def posterior_ranges(ne: dict, margin=0.0, bounds: dict | None = None) -> dict:
    """{name: (lo, hi)} of each column of a loaded, verified sample (load_posterior): its hull, each side widened by
    `margin` x the hull's width and never past the column's hard bounds (`bounds`, hard_bounds). The single source of
    the draw ranges and the emulator design box of the sample's columns (toolkit/analysis/uncertainty.py
    net_effect_sample; owner, 14 Nov). Refused: a negative or non-numeric margin; a column without spread (a zero-width
    design box cannot be emulated)."""
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(margin) \
            or not 0 <= margin <= 1:
        raise ValueError(f"net-effect posterior margin {margin!r}: a share of the hull's width in [0, 1]")
    X = np.asarray(ne["rows"], float)
    out = {}
    for j, k in enumerate(ne["names"]):
        lo, hi = float(X[:, j].min()), float(X[:, j].max())
        if not hi > lo:
            raise PosteriorRefused(f"net-effect posterior: column {k} has no spread ({lo}): it cannot set a range")
        if margin:      # never past a hard bound, never inside the hull (a row on a bound within its tolerance)
            w = hi - lo
            blo, bhi, what = (bounds or {}).get(k, (-math.inf, math.inf, ""))
            lo, hi = min(lo, max(lo - margin * w, blo)), max(hi, min(hi + margin * w, bhi))
            if lo <= 0 < X[:, j].min() and any(f"card units {u}" in what for u in DURATIONS):
                raise PosteriorRefused(f"net-effect posterior: margin {margin} takes {k}, a duration, to {lo} (a zero "
                                       "duration is not physical); use a smaller margin")
        out[k] = (lo, hi)
    return out


def run_cap(verify_round: bool, smoke: bool, caller: float | None, owner: float | None) -> float | None:
    """The cap `run` enforces (Codex r1 P1-1 on #269). Option A: the owner's OPTION_A cap (refused while unset), which a
    caller may lower but not raise; the verification round: its own plan cap under the historical CAP_USD; a smoke:
    none (it is small and writes only its scratch file)."""
    if smoke:
        return None
    bad = [x for x in (caller, owner) if x is not None and not (isinstance(x, (int, float)) and math.isfinite(x)
                                                                 and x > 0)]
    if bad:
        raise ValueError(f"a cap must be a finite positive number of USD, not {bad}")
    if verify_round:
        return min(CAP_USD if caller is None else float(caller), VERIFY_ROUND["plan_cap_usd"])
    if owner is None:
        raise ValueError("option A's fit has no owner cap (OPTION_A['cap_usd'] is unset): the owner sets it with the "
                         "fit's approval")
    return float(owner) if caller is None else min(float(caller), float(owner))


def run(pack: dict, base: dict, workers: int, out_path: str, approved: str | None, smoke: bool,
        verify: int = VERIFY, cap_usd: float | None = None, vcpus: int | None = None, resume_from: str | None = None,
        margin_loo: bool = False, verify_round: bool = False, resume_account: str | None = None) -> int:
    """Wave 2 of the fit (with `verify_round`, VERIFY_ROUND: wave 2's points read from its stored checkpoint, a new
    joint sample verified): wave 1's points and calibration grid reused (load_wave1), WAVE2's points run, the surrogate
    refitted on both, the joint region (with --margin-loo, inside each band by the local leave-one-out error), the
    chosen and verified points run, and finalize()'s gate. A smoke runs a small wave 1 itself (fit_joint), then wave 2."""
    if os.path.basename(out_path) in PROTECTED:
        raise ValueError(f"net_effect_fit writes its own file, never {PROTECTED}")
    bad = check(pack, base)
    if bad:
        raise ValueError("net_effect_fit: " + "; ".join(bad))
    if not verify_round and sorted(pack["fit_joint"]["targets"]) != sorted(OPTION_A["targets"]):
        raise ValueError(f"a fresh fit (or smoke) runs only on OPTION_A's declaration {OPTION_A['targets']}, not "
                         f"{pack['fit_joint']['targets']} (the history's targets only replay)")
    if not smoke and not (approved or "").strip():
        raise ValueError("the full fit spends more than $20: pass --approved with the owner's approval (or --smoke)")
    if not smoke and (approved or "").strip().startswith("<"):
        raise ValueError(f"--approved {approved!r} is the launch command's placeholder text, not the owner's "
                         f"approval")
    rb = bool(OPTION_A.get("refit_b")) and not verify_round and not smoke
    if rb:          # refit b: only from the stored 18 Nov checkpoint, under its sha256, and without the margin option
        import hashlib
        if margin_loo:
            raise ValueError("refit b runs without --margin-loo (owner, 19 Nov)")
        if not resume_from:
            raise ValueError(f"refit b resumes from the stored 18 Nov checkpoint: pass --resume-from <dir holding "
                             f"{CACHE_NAME}>")
        cp = os.path.join(resume_from, CACHE_NAME)
        got = hashlib.sha256(open(cp, "rb").read()).hexdigest() if os.path.isfile(cp) else None
        if got != OPTION_A["refit_b"]["cache_sha256"]:
            raise ValueError(f"refit b: {cp} has sha256 {got}, not the stored checkpoint's "
                             f"{OPTION_A['refit_b']['cache_sha256']}")
    nv = vcpus or max(int(workers), 1)
    fresh = smoke or not verify_round          # F6 option A: a fresh fit (smoke: the same steps, small)
    q = plan(pack, verify, 2, nv, 3, int(pack["fit_joint"]["design"]["n"]) + 2, refit_b=False) if smoke else plan(
        pack, verify, 0, nv, verify_round=True) if verify_round else plan(pack, verify, vcpus=nv, refit_b=True) if rb \
        else plan(pack, verify, CAL_N, nv, OPTION_A["wave2_n"], int(pack["fit_joint"]["design"]["n"]) + 2,
                  refit_b=False)
    cap_usd = run_cap(verify_round, smoke, cap_usd, OPTION_A["cap_usd"])
    if not smoke and max(q["usd"][1], q["usd_real"][1]) > cap_usd:
        raise ValueError(f"the plan's upper cost ${max(q['usd'][1], q['usd_real'][1])} on {q['vcpus']} vCPUs is above "
                         f"the cap ${cap_usd}")
    if calibration_params(pack) != {CAL_PARAM}:
        raise ValueError(f"the calibration arms depend on {sorted(calibration_params(pack))}, not only {CAL_PARAM}")
    eng = BM.load_engine(pack["engine"], None)
    runner = RecalRunner(pack, eng, workers, pack["seeds"], mode="fit", evidence=BM.load_evidence(eng, pack=pack))
    wave1 = None if fresh else load_wave1(WAVE1, eng, base, pack=pack)   # before any engine run: refused fails closed
    if verify_round and not smoke:          # wave 2's engine results, under the sha256 recorded when it was stored
        import tempfile
        resume_from = fetch_cache(VERIFY_ROUND["cache_blob"], VERIFY_ROUND["cache_sha256"],
                                  tempfile.mkdtemp(prefix="netfit_w2_"), resume_account)
    ck_out = None if smoke else os.environ.get("JOB_OUT")    # the job's outputs: each finished task as it lands
    if ck_out or resume_from:
        runner.checkpoint(out_dir=ck_out, resume_from=resume_from)
    t0 = time.time()
    names = list(pack["fit_joint"]["params"])
    box = np.array([pack["fit_joint"]["params"][k] for k in names], float)
    bands = target_bands(pack)
    p1, ext, extra = pack, None, []
    if rb:      # refit b: wave 1 and the calibration grid on the design box as ran (their tasks are in the checkpoint)
        dbx = OPTION_A["refit_b"]["design_box"]
        if list(dbx) != names:
            raise ValueError(f"refit b: the design box's parameters {list(dbx)} are not the fit's {names}")
        p1 = copy.deepcopy(pack)
        p1["fit_joint"]["params"] = {k: list(v) for k, v in dbx.items()}
        ext = OPTION_A["refit_b"].get("extension")
        extra = extension_points(dbx, ext)
    if fresh:
        b1 = np.array([p1["fit_joint"]["params"][k] for k in names], float)
        runner.calibrate_grid(_fit_settings(pack), {k: float(b1[i].mean()) for i, k in enumerate(names)},
                              2 if smoke else CAL_N)
        grid_edge = sorted(s for s, rows in runner.cal_grid["rows"].items() if any(r.get("at_grid_edge") for r in rows))
        f1 = BM.fit_joint(p1, eng, runner)
        X1 = np.array([[(th[k] - box[i, 0]) / (box[i, 1] - box[i, 0]) for i, k in enumerate(names)]
                       for th in f1["design"]["x"]])
        wave1 = {"path": None, "sha256": None, "hull": joint_region(X1, f1["design"]["computed"], bands, names, box,
                                                                    int(pack["fit_joint"]["design"]["seed"]))["hull"],
                 "points": [{"x": x, "wave": 1, "computed": {t: f1["design"]["computed"][t][i] for t in bands}}
                            for i, x in enumerate(f1["design"]["x"])]}
        old_box = {k: list(p1["fit_joint"]["params"][k]) for k in names}
    else:
        runner.use_grid(wave1["calibration_grid"])
        grid_edge = list(wave1["at_grid_edge"])
        old_box = WAVE1_BOX
    new_box = {k: list(pack["fit_joint"]["params"][k]) for k in names}
    box2 = wave2_box(wave1["hull"], old_box, new_box, WAVE2["margin"]) if wave1["hull"] else new_box
    if rb:      # refit b: the wave-2 box as it ran on 18 Nov (the design box), so wave 2's tasks are the stored ones
        box2 = dict(old_box)
    support = [old_box, box2]          # the waves' design boxes: candidates and the choice stay inside their union
    if ext:
        support.append({k: (list(ext["range"]) if k == ext["param"] else list(old_box[k])) for k in names})
    rv = verify_round and not smoke
    n2 = 3 if smoke else WAVE2["n"] if verify_round else OPTION_A["wave2_n"]
    fit = fit_waves(pack, runner, wave1, box2, n2, WAVE2["seed"], support=support,
                    cand_seed=VERIFY_ROUND["candidate_seed"] if verify_round else None,
                    after_design=(lambda: no_rerun(runner, "wave 2's design")) if rv else None, extra=extra)
    X = np.array([[(th[k] - box[i, 0]) / (box[i, 1] - box[i, 0]) for i, k in enumerate(names)]
                  for th in fit["design"]["x"]])
    margin = None
    if margin_loo:      # owner, 13 Nov: the margin option; each target's leave-one-out error over the wave-2 box
        inside = np.array([all(box2[k][0] - 1e-9 <= th[k] <= box2[k][1] + 1e-9 for k in names)
                           for th in fit["design"]["x"]])
        margin = local_loo({t: np.log(np.maximum(np.asarray(v, float), 1e-12))
                            for t, v in fit["design"]["computed"].items()}, X, inside)
    reg = joint_region(X, fit["design"]["computed"], bands, names, box, int(
        VERIFY_ROUND["candidate_seed"] if verify_round else pack["fit_joint"]["design"]["seed"]), margin=margin,
                       support=support)
    waves = {"wave1": {"file": None if fresh else os.path.basename(WAVE1), "sha256": wave1["sha256"],
                       "points_reused": 0 if fresh else len(wave1["points"]), "calibration_grid_reused": not fresh,
                       "box": old_box, "accepted_hull": wave1["hull"],
                       "points_run": len(wave1["points"]) if fresh else 0},
             "option_a": None if (smoke or verify_round) else {k: OPTION_A[k] for k in ("targets", "design_n",
                                                                                         "wave2_n", "cap_usd")},
             "wave2": {"box": box2, "n": n2, "seed": WAVE2["seed"],
                       "hull_widening": WAVE2["margin"]},
             "margin_loo": margin_loo,
             "verify_round": ({k: VERIFY_ROUND[k] for k in ("n", "candidate_seed", "pick_seed", "cache_blob",
                                                            "cache_sha256", "wave2_output_sha256")}
                              if verify_round else None), "acceptance_rule": ACCEPTANCE_RULE}
    byid = {b["id"]: b for b in pack["benchmarks"]}
    ctrl = {s: next(a for a, c in pack["arms"].items() if c["setting"] == s and (c.get("package") or {}) ==
                    (pack["settings"][s]["calibrate"].get("arm_package") or {})) for s in _fit_settings(pack)}
    best = {k: v["value"] for k, v in fit["parameters"].items()}
    chosen = {"point": best, "all_in_band": all(v["in_band"] for v in fit["verification"].values()),
              "per_seed": {t: list(v.get("per_seed") or []) for t, v in fit["verification"].items()},
              "disproved_by_verification": fit["feasibility"]["disproved_by_verification"],
              "calibration_check": _control_check(runner, pack, ctrl, best)}
    # re-run `verify` joint-sample points, each at its own interpolated calibration, as the design points were
    rng = np.random.default_rng(int(VERIFY_ROUND["pick_seed"] if verify_round
                                    else int(pack["fit_joint"]["design"]["seed"]) + 1))
    pick = sorted(rng.choice(len(reg["sample"]), min(verify, len(reg["sample"])), replace=False).tolist()) \
        if reg["sample"] else []
    runner.prefetch(sorted({a for t in pack["fit_joint"]["targets"] for a in BM._arms_of(byid[t]["estimand"])}),
                    [reg["sample"][i] for i in pick])
    checked = []
    for i in pick:
        rows = {t: runner.evaluate(byid[t], reg["sample"][i]) for t in pack["fit_joint"]["targets"]}
        checked.append({"point": reg["sample"][i], "all_in_band": all(r["in_band"] for r in rows.values()),
                        "computed": {t: round(r["computed"], 4) for t, r in rows.items()},
                        "per_seed": {t: list(r.get("per_seed") or []) for t, r in rows.items()},
                        "calibration_check": _control_check(runner, pack, ctrl, reg["sample"][i])})
    reg, code = finalize(reg, checked, chosen, grid_edge, bands=bands, seeds=list(runner.seeds))
    # refit b: the monitored (held-out) targets at the chosen point and the verified points, reported only
    mon = {t: {"at_chosen": round(runner.evaluate(byid[t], best)["computed"], 4),
               "at_verified": [round(runner.evaluate(byid[t], c["point"])["computed"], 4) for c in checked],
               "band": byid[t].get("band")} for t in (OPTION_A.get("monitor") or []) if not smoke}
    waves["refit_b"] = ({"cache_sha256": OPTION_A["refit_b"]["cache_sha256"], "wave2_box": "the design box (pinned)",
                         "extension": ext, "extension_points": len(extra), "margin_loo": margin_loo,
                         "departure": OPTION_A.get("departure"), "monitored": mon,
                         "pad_ceiling": PAD_CEILING, "edge": edge_diagnostic(best)} if rb else None)
    doc = document(eng, reg, q, smoke, approved=approved, cap_usd=cap_usd, n_sims=runner.nsims, waves=waves,
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

    # no self-test may start a real fit: run() refuses while the owner's cap is unset, so the shipped cap is set aside
    # for the test (FA12 checks it) and restored at the end
    shipped_cap = OPTION_A["cap_usd"]
    OPTION_A["cap_usd"] = None
    base = yaml.safe_load(open(PACK))
    p = build_pack(base)
    train, held = split(p)          # the fit's own declaration (option A, fit record (b)), not the pack's fit_joint
    # F6 option A: waves 1-2 and the verification round ran with the pack's own fit targets (WAVE1_TARGETS, still the
    # pack's fit_joint); the tests of their reuse and refusals use that declaration (p_w), with PACK at a temporary
    # copy where load_wave1 reads the file
    base_w = copy.deepcopy(base)
    p_w = build_pack(base_w, targets=WAVE1_TARGETS)
    train_w, held_w = split(p_w)

    import contextlib

    @contextlib.contextmanager
    def wave1_identity():
        # engine v0.18 changed the code identity, so wave 1's output (v0.17) is refused by its identity first; the
        # reuse tests below check the other conditions with the identity wave 1 recorded (a test double)
        from toolkit.engines import contract as CT
        orig = CT.engine_identity
        rec = (yaml.safe_load(open(WAVE1)) or {}).get("engine_identity") or {}
        CT.engine_identity = lambda eng: dict(orig(eng), **{k: rec[k] for k in ("name", "code_sha256", "params_sha256",
                                                                                 "config_sha256") if k in rec})
        try:
            yield
        finally:
            CT.engine_identity = orig

    @contextlib.contextmanager
    def as_pack(doc):
        import tempfile
        global PACK
        orig = PACK
        with tempfile.TemporaryDirectory() as d:
            PACK = os.path.join(d, "pack.yaml")
            yaml.safe_dump(doc, open(PACK, "w"), sort_keys=False)
            try:
                yield PACK
            finally:
                PACK = orig
    byid = {b["id"]: b for b in p["benchmarks"]}
    arms_ok = all(a in p["arms"] for t in p["fit_joint"]["targets"] for a in BM._arms_of(byid[t]["estimand"]))
    ok(p["fit_joint"]["targets"] == train == OPTION_A["targets"] and arms_ok and not check(p, base)
       and base["fit_joint"]["targets"] == WAVE1_TARGETS == train_w
       and {"B7", "B8", "B15"} <= set(held) and not set(held) & set(train)
       and {"B9", "B9b", "B9c"} <= set(held_w) and not check(p_w, base_w),
       f"F1 the targets are the fit's own declaration {train} (fit record (b)); the pack's fit_joint keeps "
       f"{WAVE1_TARGETS}; B7, B8, B15 and the rest are held out (waves 1-2: B9, B9b, B9c held out too)")
    for t, want in (("B9c", "differ from the fit's declarations"), ("B7", "B7 is held out")):
        q = copy.deepcopy(p_w)
        q["fit_joint"]["targets"].append(t)
        ok(any(want in x for x in check(q, base_w)),
           f"F1b a target list that is no declaration of the fit ({t} added to waves 1-2's) is refused: {want}")
    ok(list(p_w["fit_joint"]["params"]) == ["net_kill_shift", "cfp_kill", "irs_kill", "cfp_half"]
       and "pbo" not in train_nets(p_w, train_w) and p_w["fit_joint"]["params"]["cfp_half"] == [270.0, 1650.0],
       "F2 the fit takes net_kill_shift (training arms use pyrethroid and dual-AI nets), cfp_kill, irs_kill and cfp_half "
       "(owner, 13 Nov; wave-2 box 270-1650 days, from the durability studies); on waves 1-2's targets the PBO "
       "parameters stay out: no training arm has a PBO net")
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.spec import priors as PR
    e = MalariaEngine()
    sp = {q.name: q.value for q in e.parameters()}
    pr = PR.load_card(e)["priors"]["net_kill_shift"]
    bx = p["fit_joint"]["params"]
    ok(all(bx[k][0] <= sp[k] <= bx[k][1] for k in bx) and [pr.lo, pr.hi] == bx["net_kill_shift"],
       "F3 the box holds every current value; the kill shift's box is its card prior (Churcher 2016 alpha1 CrI)")

    def f4():
        hist = {"cal_n": 0, "wave2_n": WAVE2["n"], "wave1_n": 0, "refit_b": False}     # wave 2 as it ran (history)
        q = plan(p_w, **hist)
        w = q["waves"]
        n2 = WAVE2["n"]
        naive = q["cpu_work_hours"] * VM_FACTOR[1] * USD_PER_VCPU_H[1]
        q64 = plan(p_w, vcpus=64, **hist)
        ok(q["design_points"] == n2 and q["reused_points"] == 46 + 12 + 1 and q["seeds"] == 8 and len(q["arms"]) == 8
           and q["settings"] == ["benin", "irs_added", "misungwi", "muleba"]
           and q["calibration_runs"] == 0 and q["calibration_reused"] is True
           and q["arm_runs"] == (n2 + 1 + 12) * 8 * 8
           and q["not_fitted"] == ["net_deter_sat", "pbo_adv_decay", "pbo_gain_scale"]
           and q["held_out_untouched"] == held_w
           and q["vcpus"] == VCPUS and w["calibration"] == 0 and w["chosen"] == 4
           and w["design"] == _waves(n2 * 4 * 8, VCPUS) and w["sample"] == _waves(12 * 4 * 8, VCPUS)
           and q["usd"][1] > naive and len(q["usd_real"]) == 2 and q["usd_real"][1] < CAP_USD
           and q["usd"][1] < CAP_USD and q64["usd"][1] > q["usd"][1],
           f"F4 (wave 2) the plan prices only the runs not reused: {n2} wave-2 design points, the chosen point and 12 "
           f"verified points ({q['arm_runs']} arm runs); wave 1's {q['reused_points']} evaluated points and its "
           f"calibration grid are reused (0 calibration runs); {q['vm_vcpu_hours']} vCPU-h, ${q['usd'][0]}-"
           f"{q['usd'][1]} on the VM price bound, ${q['usd_real'][0]}-{q['usd_real'][1]} at the measured $0.124 per "
           f"vCPU-hour (cap ${CAP_USD}); the CPU-work estimate would say ${naive:.1f}; 64 vCPUs ${q64['usd'][1]}")
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
        rr.prefetch(["mis_pyr"], [th])         # (refit b: a Muleba call also runs the monitored arms, FB2)
        rr.prefetch(["mis_pyr"], [th])
        rr.set_calibration({"muleba": 60.0, "misungwi": 12.0, "benin": 10.0, "irs_added": 20.0})
        rr.prefetch(["mis_pyr"], [th])
        ok(ran == [1, 1], f"F10 (review P2) the result cache is keyed on the calibration: the same point is reused "
                          f"under the same calibration and re-run under another (jobs per prefetch {ran})")
    case("F10", f10)

    def f11():
        reg = {"sample": [{"a": 0.1}, {"a": 0.2}], "hull": {"a": [0.1, 0.2]}}
        cal = lambda **kw: {s: {"target": t, "achieved": kw.get(s, t)} for s, t in  # noqa: E731
                            (("muleba", 0.55), ("misungwi", 0.458), ("benin", 2.06), ("irs_added", 0.40))}
        bd = {"T": [0.4, 0.6]}
        inb, outb = [0.5, 0.52, 0.48, 0.5], [0.9, 0.92, 0.88, 0.9]      # seed values: in band, far out of band
        pt = lambda a, ok_=True, **kw: {"point": {"a": a}, "per_seed": {"T": inb if ok_ else outb},  # noqa: E731
                                        "all_in_band": True, "calibration_check": cal(**kw)}
        chosen_ok = {"per_seed": {"T": inb}, "all_in_band": True, "disproved_by_verification": [],
                     "calibration_check": cal()}
        fz = lambda c, ch, **kw: finalize(copy.deepcopy(reg), c, ch, bands=bd, seeds=[1, 2, 3, 4], **kw)  # noqa: E731
        runs = {
            "good": (fz([pt(0.1), pt(0.2, muleba=0.58)], chosen_ok), 0),
            "ratio": (fz([pt(0.1), pt(0.2, False)], chosen_ok), 2),
            "misungwi 0.30 vs 0.458": (fz([pt(0.1), pt(0.2, misungwi=0.30)], chosen_ok), 2),
            "muleba 0.15 vs 0.55": (fz([pt(0.1, muleba=0.15)], chosen_ok), 2),
            "chosen out of band (seed evidence)": (fz([pt(0.1)], dict(chosen_ok, per_seed={"T": outb})), 2),
            "chosen calibration": (fz([pt(0.1)], dict(chosen_ok, calibration_check=cal(benin=1.5))), 2),
            "grid at its edge": (fz([pt(0.1)], chosen_ok, grid_edge=["misungwi"]), 2),
            "a summary flag is not evidence": (fz([dict(pt(0.1, False), all_in_band=True)], chosen_ok), 2),
            "no seed evidence": (fz([{k: v for k, v in pt(0.1).items() if k != "per_seed"}], chosen_ok), 2),
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
        cap = cmd[cmd.index("--cap-usd") + 1]
        ok(cmd[:5] == ["python3", "cloud/vm_job.py", "run", OPTION_A["job"], "net-effect-fit"]
           and cap == ("<owner cap>" if OPTION_A["cap_usd"] is None else f"{OPTION_A['cap_usd']:g}")
           and cmd[cmd.index("--store-account") + 1] == "modelingagentic"
           and cmd[cmd.index("--store") + 1] == OPTION_A["store"] != "outputs/_engine/net_effect_fit/round2"
           and "--approved" in cmd and "--margin-loo" in cmd and "--verify-round" not in cmd and str(VCPUS) in cmd
           and cmd[cmd.index("--") + 1:][:2] == ["python3", "toolkit/engines/malaria/benchmarks/net_effect_fit.py"],
           f"F13 the option A launch goes through cloud/vm_job.py run with the owner's --cap-usd (a placeholder vm_job "
           f"refuses until it is set) and its own store prefix (earlier rounds' copies are kept): {' '.join(cmd)}")
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

    def f16():
        import tempfile
        from toolkit.engines.contract import ToyEngine
        hz = {"start": 0.0, "stop": 2.0, "window": [0.0, 2.0]}
        jobs = [("toolkit.engines.contract:ToyEngine", {}, {"eff_a": 0.4 + 0.01 * i, "noise": 0.1}, {"x": 0.1 * i},
                 {"a": i % 2}, hz, s) for i in range(6) for s in (1, 2)]

        def mk():
            r = RecalRunner.__new__(RecalRunner)      # only what _map reads: the engine and the worker count
            r.engine, r.workers = ToyEngine(), 1
            return r
        plain = mk()._map(jobs)                       # no checkpoint: benchmark.Runner's own path
        with tempfile.TemporaryDirectory() as d:
            dirs = {k: os.path.join(d, k) for k in ("full", "cut", "res")}
            full = mk()
            full.checkpoint(out_dir=dirs["full"])
            r_full = full._map(jobs)
            cut, n = mk(), [0]
            cut.checkpoint(out_dir=dirs["cut"])

            def boom(t):                              # the cap stops the job after five engine tasks
                n[0] += 1
                if n[0] > 5:
                    raise KeyboardInterrupt
                return BM._job_list(t)
            cut.task_fn = boom
            try:
                cut._map(jobs)
            except KeyboardInterrupt:
                pass
            kept = open(os.path.join(dirs["cut"], CACHE_NAME)).read().count("\n")
            res, ran = mk(), []
            res.checkpoint(out_dir=dirs["res"], resume_from=dirs["cut"])
            res.task_fn = lambda t: (ran.append(1), BM._job_list(t))[1]
            r_res = res._map(jobs)
            # Codex r1 P2 probe: resuming into the same directory, after a stop that cut the last row mid-write
            same_dir, frag = os.path.join(d, "same"), json.dumps({"key": "cut by the stop"})[:12]
            os.makedirs(same_dir)
            open(os.path.join(same_dir, CACHE_NAME), "w").write(
                open(os.path.join(dirs["cut"], CACHE_NAME)).read() + frag)
            again = mk()
            again.checkpoint(out_dir=same_dir, resume_from=same_dir)
            r_again = again._map(jobs)
            try:
                n_again = len(load_cache(same_dir, again.code_sha256()))
            except ValueError as ex:
                n_again = str(ex)
            ok(n_again == len(jobs) and r_again == r_full,
               f"F18 (Codex r1 P2) resuming into the directory it resumes from rewrites the accepted rows first "
               f"(atomically), so new rows never follow a cut row: the next resume reads {n_again} rows")
            dump =lambda x: yaml.safe_dump(json.loads(json.dumps(x)), sort_keys=False)  # noqa: E731
            same = r_res == r_full == plain and dump(r_res) == dump(plain) and json.dumps(r_res) == json.dumps(plain)
            ok(kept == 5 and len(ran) == len(jobs) - 5 and same,
               f"F16 (checkpoint) each finished engine task is appended to {CACHE_NAME} as it lands ({kept} rows after "
               f"the stop); a relaunch with --resume-from reruns only the {len(ran)} missing tasks, and its results "
               f"equal an uninterrupted run's, with or without a checkpoint ({same})")
            rows = [json.loads(x) for x in open(os.path.join(dirs["full"], CACHE_NAME))]
            code = mk().code_sha256()
            got = {}

            def bad(label, lines, tail=""):
                q = os.path.join(d, label.replace(" ", "_"))
                os.makedirs(q)
                open(os.path.join(q, CACHE_NAME), "w").write("".join(json.dumps(x) + "\n" for x in lines) + tail)
                try:
                    got[label] = len(load_cache(q, code))
                except ValueError as ex:
                    got[label] = str(ex)
            bad("other engine code", [dict(rows[0], code_sha256="0" * 64)] + rows[1:])
            bad("an edited task", [dict(rows[0], task=rows[1]["task"])] + rows[1:])
            bad("an edited result", [dict(rows[0], result=rows[1]["result"])] + rows[1:])
            bad("a malformed middle row", rows[:2], tail="{not json\n" + json.dumps(rows[2]) + "\n")
            bad("a truncated last row", rows[:3], tail=json.dumps(rows[3])[:20])
            try:
                load_cache(os.path.join(d, "absent"), code)
                got["no cache file"] = 0
            except ValueError as ex:
                got["no cache file"] = str(ex)
        want = {"other engine code": "code_sha256", "an edited task": "key", "an edited result": "result_sha256",
                "a malformed middle row": "malformed", "no cache file": "no cache"}
        wrong = sorted(k for k, w in want.items() if w not in str(got.get(k)))
        ok(not wrong and got.get("a truncated last row") == 3,
           f"F17 (checkpoint) a cache row from other engine code, with an edited task or result, a malformed row and a "
           f"missing cache are refused (fail closed); a last row cut by the stop is dropped ({got.get('a truncated '
           f'last row')} rows kept; refused for another reason or not at all: {wrong or 'none'})")
    case("F16", f16)

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
            far = lambda p: p["per_seed"].update(B12m=[0.95, 0.96, 0.94, 0.95])  # noqa: E731 -- seed evidence
            variant("a verified point out of band", lambda x: far(jr(x)["verified"]["points"][0]))
            variant("the chosen point out of band", lambda x: far(jr(x)["verified"]["chosen"]))
            variant("a verified point without seed evidence", lambda x: jr(x)["verified"]["points"][0].pop("per_seed"))
            variant("no verified point", lambda x: jr(x)["verified"].update(points=[]))
            # Codex P2 (PR #233): one seed value per declared seed; the run's recorded seeds are pack.yaml's
            variant("a per-seed list truncated to 2 seeds",
                    lambda x: jr(x)["verified"]["points"][0]["per_seed"].update(B11=[0.5, 0.6]))
            variant("recorded seeds other than the pack's", lambda x: x["fit_rule"].update(seeds=[11, 12]))
            variant("no recorded seeds", lambda x: x.pop("fit_rule"))
            # Codex r1 P2 (this PR): the recorded declaration is validated, and a malformed one is a refusal
            variant("recorded seeds as floats", lambda x: x["fit_rule"].update(seeds=[float(v) for v in pack_seeds()]))
            variant("fit_rule not a mapping", lambda x: x.update(fit_rule=["seeds"]))
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
            # owner, 14 Nov (f6-posterior-ranges): the sample's hull sets the draw ranges of its columns, so a narrower
            # draw range no longer refuses it; a value outside the engine's hard bounds still does
            variant("a probability above 1", lambda x: jr(x)["sample"][7].update(cfp_kill=1.2))
            variant("a negative half-life", lambda x: jr(x)["sample"][7].update(cfp_half=-5.0))
            rows2 = [dict(r) for r in rows]
            rows2[1]["cfp_half"] = 701.0
            other = os.path.join(d, "other.yaml")
            _fixture_placeholder(other, e, rows2, box)
            sha2 = load_posterior(other, e, rg, dg(other))["sha256"]
        gate = ("acceptance gate failed (finalize)", "a verified point out of band", "the chosen point out of band",
                "no verified point", "a clamped calibration grid", "no calibration grid record",
                "a verified point not in the sample", "a verified point without seed evidence",
                "a per-seed list truncated to 2 seeds")
        why = {**dict.fromkeys(gate, "acceptance gate"), "missing file": "unreadable", "malformed YAML": "malformed",
               "an edited row under the signed digest": "signed sha256",
               "a value outside priors.yaml under a wider range": "priors.yaml",
               "not a mapping": "not an output", "another producer": "not an output", "a smoke": "smoke",
               "other engine code": "code_sha256", "empty sample": "empty sample",
               "a row lacks a parameter": "the fit's parameters", "a non-finite value": "non-finite",
               "a non-numeric value": "non-numeric", "a column the fit did not fit": "the fit's parameters",
               "a parameter these draws do not vary": "not varied", "a probability above 1": "hard bound",
               "a negative half-life": "hard bound",
               "recorded seeds other than the pack's": "seeds", "no recorded seeds": "seeds",
               "recorded seeds as floats": "seeds", "fit_rule not a mapping": "seeds"}
        wrong = sorted(k for k in why if why[k] not in got.get(k, ""))
        ok(not wrong and set(got) == set(why) and sha2 != P["sha256"],
           f"F15 the loader fails closed, each for its own reason, in {len(why)} cases ({sorted(why)}; refused for "
           f"another reason or not at all: {wrong or 'none'}); a changed sample has another sha256")
    case("F14", f14)

    def f28():
        """owner, 14 Nov (f6-posterior-ranges): the sample's own hull sets its columns' draw ranges and design box."""
        import hashlib
        import tempfile
        box = {"net_kill_shift": [-0.618, 0.66], "cfp_kill": [0.0, 0.8], "irs_kill": [0.0, 0.95],
               "cfp_half": [270.0, 1650.0]}
        u = [float(v) for v in np.random.default_rng(8).uniform(0, 1, 30)]
        rows = [{"net_kill_shift": round(-0.3 + 0.9 * x, 6), "cfp_kill": round(0.45 + 0.33 * x, 6),
                 "irs_kill": round(0.40 + 0.06 * (1 - x), 6), "cfp_half": round(490 + 960 * x, 6)} for x in u]
        narrow = {"net_kill_shift": (pr.lo, pr.hi), "cfp_kill": (0.465, 0.585), "irs_kill": (0.38, 0.45125),
                  "cfp_half": (547.5, 1277.5)}          # the fitted.yaml and spec ranges the F6 sample fell outside
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "net_effect_fit.yaml")
            _fixture_placeholder(good, e, rows, box)
            P = load_posterior(good, e, narrow, hashlib.sha256(open(good, "rb").read()).hexdigest())
            X = np.asarray([[r[k] for k in P["names"]] for r in rows])
            hull = {k: (float(X[:, j].min()), float(X[:, j].max())) for j, k in enumerate(P["names"])}
            hb = hard_bounds(e, P["names"])
            got = posterior_ranges(P, bounds=hb)
            ok(P["rows"].shape == (30, 4) and got == hull,
               f"F28 a sample whose hull exceeds its fitted and spec draw ranges loads; its ranges are its hull {got}")
            ok(hb["cfp_kill"][:2] == (0.0, 1.0) and hb["irs_kill"][:2] == (0.0, 1.0)
               and hb["cfp_half"][:2] == (0.0, float("inf")) and hb["net_kill_shift"][:2] == (pr.lo, pr.hi),
               f"F28b the hard bounds: a probability in [0, 1], a half-life in days >= 0 (card units), a priors.yaml "
               f"range as the prior's support: {hb}")
            m = posterior_ranges(P, margin=0.1, bounds=hb)
            w = {k: hull[k][1] - hull[k][0] for k in hull}
            ok(all(abs(m[k][0] - (hull[k][0] - 0.1 * w[k])) < 1e-12 and abs(m[k][1] - (hull[k][1] + 0.1 * w[k])) < 1e-12
                   for k in ("cfp_kill", "irs_kill", "cfp_half")),
               f"F28c a stated margin widens each side by that share of the hull's width: {m}")
            near = {"names": ["cfp_kill"], "rows": np.array([[0.9], [0.98]])}
            c = posterior_ranges(near, margin=0.5, bounds=hard_bounds(e, ["cfp_kill"]))
            ok(abs(c["cfp_kill"][0] - 0.86) < 1e-12 and c["cfp_kill"][1] == 1.0,
               f"F28d a margin never crosses a hard bound: {c}")
            edge = {"names": ["cfp_kill"], "rows": np.array([[0.9], [1.000001]])}     # within the rounding tolerance
            c2 = posterior_ranges(edge, margin=0.5, bounds=hard_bounds(e, ["cfp_kill"]))
            ok(c2["cfp_kill"][1] >= 1.000001, f"F28f (Codex r1 P2-2) the range never cuts inside the hull, even where a "
                                              f"row sits on a hard bound within its tolerance: {c2}")
            half = {"names": ["cfp_half"], "rows": np.array([[500.0], [1450.0]])}
            why2 = {}
            for label, m_ in (("a margin taking a half-life to zero", 0.6), ("a margin above 1", 1e308)):
                try:
                    posterior_ranges(half, margin=m_, bounds=hard_bounds(e, ["cfp_half"]))
                    why2[label] = ""
                except (PosteriorRefused, ValueError) as ex:
                    why2[label] = str(ex)
            ok(all(why2.values()) and posterior_ranges(half, 0.5, hard_bounds(e, ["cfp_half"]))["cfp_half"] == (25.0, 1925.0),
               f"F28g (Codex r2 P2s) a margin that takes a duration to zero, or above 1, is refused: "
               f"{ {k: v[:70] for k, v in why2.items()} }")
            why = {}
            for label, arg, kw in (("a column without spread", {"names": ["irs_kill"], "rows": np.array([[0.4], [0.4]])},
                                    {}),
                                   ("a negative margin", P, {"margin": -0.1}),
                                   ("a margin that is not a number", P, {"margin": "wide"})):
                try:
                    posterior_ranges(arg, bounds=hb, **kw)
                    why[label] = ""
                except (PosteriorRefused, ValueError) as ex:
                    why[label] = str(ex)
            ok(all(why.values()), f"F28e refused (fail closed): {sorted(k for k, v in why.items() if v)}; accepted: "
                                  f"{sorted(k for k, v in why.items() if not v) or 'none'}")
    case("F28", f28)

    def f19():
        with as_pack(base_w), wave1_identity():
            f19_(base_w, train_w)

    def f19_(base, train):
        import hashlib
        import tempfile
        w1 = load_wave1(WAVE1, e, base)
        grid = [float(v) for v in np.linspace(*NET_PARAMS["net_kill_shift"][1], CAL_N)]
        ok(len(w1["points"]) == 46 + 12 + 1 and [x["wave"] for x in w1["points"]].count(1) == 59
           and w1["calibration_grid"]["values"] == grid and w1["at_grid_edge"] == []
           and set(w1["calibration_grid"]["m0"]) == {"benin", "irs_added", "misungwi", "muleba"}
           and w1["sha256"] == WAVE1_SHA256 and all(set(x["computed"]) == set(train) for x in w1["points"]),
           f"F19 wave 1's engine results are reused from its output ({os.path.basename(WAVE1)}, sha256 "
           f"{WAVE1_SHA256[:12]}): its 46 design points, 12 verified points and chosen point, and its calibration grid")
        got = {}
        with tempfile.TemporaryDirectory() as d:
            def variant(label, mut, pack=base):
                doc = yaml.safe_load(open(WAVE1))
                mut(doc)
                q = os.path.join(d, label.replace(" ", "_") + ".yaml")
                yaml.safe_dump(doc, open(q, "w"), sort_keys=False)
                try:
                    load_wave1(q, e, pack, hashlib.sha256(open(q, "rb").read()).hexdigest())
                    got[label] = ""
                except ValueError as ex:
                    got[label] = str(ex)
            variant("other engine code", lambda x: x["engine_identity"].update(code_sha256="0" * 64))
            variant("other parameters", lambda x: x["engine_identity"].update(params_sha256="0" * 64))
            variant("a smoke", lambda x: x.update(smoke=True))
            variant("other seeds", lambda x: x["fit_rule"].update(seeds=[1, 2]))
            variant("other targets", lambda x: x["fit_rule"].update(targets=["B10"]))
            variant("another calibration grid", lambda x: x["calibration_grid"].update(net_kill_shift=[0.0, 0.1]))
            variant("a clamped calibration grid", lambda x: x["calibration_grid"].update(at_grid_edge=["muleba"]))
            variant("a design point without its result", lambda x: x["design"]["computed"]["B10"].pop())
            pk = copy.deepcopy(base)
            pk["seeds"] = pk["seeds"][:2]
            variant("another pack", lambda x: None, pack=pk)
            try:
                load_wave1(WAVE1, e, base, "0" * 64)
                got["an edited file"] = ""
            except ValueError as ex:
                got["an edited file"] = str(ex)
        why = {"other engine code": "code_sha256", "other parameters": "params_sha256", "a smoke": "smoke",
               "other seeds": "seeds", "other targets": "targets", "another calibration grid": "calibration grid",
               "a clamped calibration grid": "edge", "a design point without its result": "result",
               "another pack": "pack", "an edited file": "sha256"}
        wrong = sorted(k for k in why if why[k] not in got.get(k, ""))
        ok(not wrong, f"F19b wave 1 is reused only under the same engine identity, pack, seeds, targets and calibration "
                      f"grid, from the recorded file ({len(why)} refusals; for another reason or not at all: "
                      f"{wrong or 'none'})")
    case("F19", f19)

    def f19c():
        # F6 option A: a held-out benchmark (and its setting) added to the pack does not touch what wave 1 ran, so its
        # results stay reusable; a change to a training target's declaration (here a training setting) refuses them
        import tempfile
        global PACK
        held_pk = copy.deepcopy(base_w)
        held_pk["settings"]["zz_site"] = copy.deepcopy(held_pk["settings"]["misungwi"])
        held_pk["arms"].update(zz_a={"setting": "zz_site", "package": {"net": "pyr"}},
                               zz_b={"setting": "zz_site", "package": {"net": "pbo"}})
        held_pk["benchmarks"].append(dict(byid["B9c"], id="ZZ1", estimand=dict(byid["B9c"]["estimand"], num="zz_b",
                                                                                 den="zz_a")))
        train_pk = copy.deepcopy(base_w)
        train_pk["settings"]["muleba"]["calibrate"]["grid"] = [8, 16, 32]
        got, orig = {}, PACK
        with tempfile.TemporaryDirectory() as d, wave1_identity():
            for label, pk in (("held", held_pk), ("train", train_pk)):
                PACK = os.path.join(d, f"{label}.yaml")
                yaml.safe_dump(pk, open(PACK, "w"), sort_keys=False)
                try:
                    load_wave1(WAVE1, e, yaml.safe_load(open(PACK)))
                    got[label] = ""
                except ValueError as ex:
                    got[label] = str(ex)
                finally:
                    PACK = orig
        # Codex r1 P2-3 on #266: the check binds the pack the runner executes (build_pack's), not only the base
        # (F6 option A: on the pack wave 1 ran on, base_w, so the refusal is the effective pack's, not the new targets')
        eff = build_pack(base_w, targets=WAVE1_TARGETS)
        eff["settings"]["misungwi"]["params"]["n_agents"] = 999
        try:
            with as_pack(base_w), wave1_identity():
                load_wave1(WAVE1, e, base_w)                  # the unchanged effective pack is accepted first
                got["effective_ok"] = ""
                load_wave1(WAVE1, e, base_w, pack=eff)
            got["effective"] = ""
        except (ValueError, TypeError) as ex:
            got["effective"] = str(ex)
        ok(got.get("held") == "" and "declarations" in got.get("train", "") and got.get("effective_ok") == ""
           and "declarations" in got["effective"],
           f"F19c wave 1 stays reusable when the pack gains a held-out benchmark and setting, and is refused when a "
           f"training target's declaration changes ({got})")
    case("F19c", f19c)

    def f20():
        old = {"a": [0.0, 1.0], "b": [10.0, 20.0], "c": [0.0, 1.0]}
        new = {"a": [0.0, 1.0], "b": [5.0, 30.0], "c": [0.0, 1.0]}
        hull = {"a": [0.4, 0.6], "b": [10.0, 19.0], "c": [0.1, 0.95]}
        bx = wave2_box(hull, old, new, 0.25)
        ok(bx == {"a": [0.35, 0.65], "b": [5.0, 21.25], "c": [0.0, 1.0]},
           f"F20 the wave-2 box is wave 1's accepted hull widened by a margin (here 25%) of its width on each side, "
           f"clipped to the box; a side where the hull reached the old box edge goes to the new box edge ({bx})")
    case("F20", f20)

    def f21():
        box2 = {"a": [0.0, 1.0], "b": [0.0, 2.0]}

        class Fake:
            seeds, calibrated = [1], {}

            def __init__(self):
                self.ran = []

            def calibrate_settings(self, fitted, names=None):
                pass

            def prefetch(self, arms, thetas):
                self.ran += [dict(t) for t in thetas]

            def evaluate(self, b, th):
                v = float(np.exp(-0.5 * th["a"] - 0.2 * th["b"] + 0.1 * th["a"] * th["b"]))
                return {"computed": v, "in_band": b["band"][0] <= v <= b["band"][1], "seed_min": v, "seed_max": v}
        pk = {"fit_joint": {"rule": "joint-v1", "params": box2, "targets": ["T"], "design": {"n": 10, "seed": 3}},
              "benchmarks": [{"id": "T", "band": [0.6, 0.8], "target": 0.7,
                              "estimand": {"type": "ratio", "num": "x", "den": "y"}}],
              "arms": {"x": {"setting": "s"}, "y": {"setting": "s"}}}
        fk = Fake()
        w1pts = [{"x": {"a": float(u), "b": float(v)}, "wave": 1, "computed": {"T": fk.evaluate(
            pk["benchmarks"][0], {"a": float(u), "b": float(v)})["computed"]}} for u, v in BM.lhs(8, 2, 1) * [1, 2]]
        fit = fit_waves(pk, fk, {"points": w1pts}, {"a": [0.2, 0.8], "b": [0.5, 1.5]}, n=10, seed=3)
        waves = fit["design"]["wave"]
        ok(len(fk.ran) == 10 + 1 and waves.count(1) == 8 and waves.count(2) == 10
           and all(0.2 <= r["a"] <= 0.8 and 0.5 <= r["b"] <= 1.5 for r in fk.ran[:10])
           and fit["fit_rule"]["version"] == "joint-v1-wave2" and len(fit["design"]["x"]) == 18
           and fit["surrogate"]["T"]["n_points"] == 18 and set(fit["parameters"]) == set(box2)
           and fit["verification"]["T"]["in_band"],
           f"F21 wave 2 runs only its own {waves.count(2)} design points (in the wave-2 box) and the chosen point on the "
           f"engine; the surrogate is refitted on both waves ({len(fit['design']['x'])} points)")
    case("F21", f21)

    def f22():
        n2, b2 = ["a", "b"], np.array([[0.0, 1.0], [0.0, 1.0]])
        g2 = np.linspace(0, 1, 9)
        X2 = np.array([[u, v] for u in g2 for v in g2])
        Y2 = {"T": np.exp(X2[:, 0] + X2[:, 1])}
        bands2 = {"T": [float(np.exp(0.8)), float(np.exp(1.2))]}
        plain = joint_region(X2, Y2, bands2, n2, b2, 5)
        same = joint_region(X2, Y2, bands2, n2, b2, 5, margin=None)
        mg = joint_region(X2, Y2, bands2, n2, b2, 5, margin={"T": 0.05})
        S = np.array([[s["a"], s["b"]] for s in mg["sample"]])
        m = mg.get("margin") or {}
        ok(same == plain and "margin" not in plain and m.get("by_target") == {"T": 0.05}
           and 0 < mg["n_accepted"] < plain["n_accepted"] and np.all(np.abs(S.sum(1) - 1.0) <= 0.15 + 1e-3)
           and m.get("n_in_band_without_margin") == plain["n_accepted"]
           and abs(m.get("share_excluded", -1) - (1 - mg["n_accepted"] / plain["n_accepted"])) < 1e-3
           and set(m.get("excluded_ranges") or {}) == set(n2)
           and m["in_band_hull_without_margin"] == plain["hull"]
           and all(plain["hull"][k][0] <= m["excluded_ranges"][k][0] <= m["excluded_ranges"][k][1]
                   <= plain["hull"][k][1] for k in n2),
           f"F22 with the margin flag the sample holds only candidates the surrogate puts in band by the margin "
           f"({m.get('by_target')}); it records the share of the in-band region excluded ({m.get('share_excluded')}) "
           f"and the excluded points' ranges; without it the region is unchanged")
        lo = local_loo({"T": np.log(Y2["T"]) + 0.01 * np.sin(40 * X2[:, 0])}, X2, X2[:, 0] <= 0.5)
        ok(set(lo) == {"T"} and 0 < lo["T"] < 0.05,
           f"F22b the margin is each target's leave-one-out error of the log surrogate, root mean square over the points "
           f"in the wave-2 box ({lo})")
    case("F22", f22)

    def f23():
        n2, b2 = ["a", "b"], np.array([[0.0, 1.0], [0.0, 2.0]])
        g2 = np.linspace(0, 1, 9)
        X2 = np.array([[u, v] for u in g2 for v in g2])
        Y2 = {"T": np.exp(X2[:, 0] + X2[:, 1])}
        sup = [{"a": [0.0, 0.5], "b": [0.0, 2.0]}, {"a": [0.0, 1.0], "b": [0.0, 0.6]}]
        r = joint_region(X2, Y2, {"T": [float(np.exp(0.8)), float(np.exp(1.2))]}, n2, b2, 5, support=sup)
        inside = lambda s: any(all(bx[k][0] <= s[k] <= bx[k][1] for k in n2) for bx in sup)  # noqa: E731
        ok(r["sample"] and all(inside(s) for s in r["sample"]) and r["support"]["boxes"] == sup
           and 0 < r["support"]["share_of_box"] < 1,
           f"F23 (wave 2's extrapolation) the joint-sample candidates lie in the union of the waves' boxes, where the "
           f"engine-evaluated points are ({r['support']['share_of_box']} of the box), never outside it")

        class Fake:
            seeds, calibrated = [1], {}

            def calibrate_settings(self, fitted, names=None):
                pass

            def prefetch(self, arms, thetas):
                pass

            def evaluate(self, b, th):       # the target 0.7 is reached only at (0.8, 1.2), outside the support
                v = float(0.7 * np.exp(-((th["a"] - 0.8) ** 2 + (th["b"] - 1.2) ** 2)))
                return {"computed": v, "in_band": True, "per_seed": [v], "seed_min": v, "seed_max": v}
        pk = {"fit_joint": {"rule": "joint-v1", "params": {"a": [0.0, 1.0], "b": [0.0, 2.0]}, "targets": ["T"],
                            "design": {"n": 6, "seed": 3}},
              "benchmarks": [{"id": "T", "band": [0.6, 0.8], "target": 0.7,
                              "estimand": {"type": "ratio", "num": "x", "den": "y"}}],
              "arms": {"x": {"setting": "s"}, "y": {"setting": "s"}}}
        w1 = [{"x": {"a": float(u), "b": float(v)}, "wave": 1, "computed": {"T": Fake().evaluate(
            None, {"a": float(u), "b": float(v)})["computed"]}} for u, v in BM.lhs(12, 2, 1) * [1, 2]]
        fit = fit_waves(pk, Fake(), {"points": w1}, {"a": [0.0, 0.5], "b": [0.0, 2.0]}, n=6, seed=3, support=sup)
        best = {k: v["value"] for k, v in fit["parameters"].items()}
        ok(inside(best) and fit["fit_rule"]["support"] == sup,
           f"F23b the chosen point is chosen inside the same support ({best})")
    case("F23", f23)

    def f24():
        b = [0.42, 0.70]
        cases = {"mean in band": ([0.50, 0.52, 0.48, 0.51], True),
                 "mean below, interval overlaps": ([0.40, 0.43, 0.41, 0.42, 0.39, 0.42, 0.41, 0.43], True),
                 "interval entirely below": ([0.30, 0.31, 0.30, 0.29, 0.30, 0.31, 0.30, 0.30], False),
                 "mean above, interval overlaps": ([0.71, 0.72, 0.69, 0.70], True),
                 "one seed (no spread)": ([0.50], False),
                 "a non-finite value": ([0.5, float("nan"), 0.5], False),
                 "no values": ([], False)}
        got = {k: ci_in_band(v, b, len(v)) for k, (v, _) in cases.items()}
        wrong = sorted(k for k, (_, w) in cases.items() if got[k] != w)
        v = cases["mean below, interval overlaps"][0]
        lo, hi = seed_interval(v, len(v))
        lg = np.log(v)
        h = 1.96 * float(np.std(lg, ddof=1)) / np.sqrt(len(v))
        ok(not wrong and abs(lo - np.exp(np.mean(lg) - h)) < 1e-12 and abs(hi - np.exp(np.mean(lg) + h)) < 1e-12
           and "1.96" in ACCEPTANCE_RULE,
           f"F24 (owner, 14 Nov) a verified or chosen point is in band for a target when the 95% interval of its seed "
           f"(geometric) mean, on the log scale +- 1.96 sd/sqrt(n) over the run's seeds, overlaps the band; fewer "
           f"than 2 seeds or a non-finite value fail closed ({len(cases)} cases; wrong: {wrong or 'none'})")
        p = {"point": {}, "per_seed": {"B10": [0.5, 0.5, 0.6, 0.6]}}
        ok(point_in_band(p, {"B10": [0.3, 0.67], "B11": [0.42, 0.7]}, [1, 2, 3, 4])[0] is False
           and point_in_band(p, {"B10": [0.3, 0.67]}, [1, 2, 3, 4])[0] is True,
           "F24b a target without seed evidence fails the point (fail closed)")
    case("F24", f24)

    def f25():
        import hashlib
        import tempfile
        q = plan(p_w, verify_round=True)
        ok(q["design_points"] == 0 and q["design_points_from_cache"] == WAVE2["n"] and q["waves"]["design"] == 0
           and q["arm_runs"] == (1 + VERIFY) * 8 * 8 and max(q["usd"][1], q["usd_real"][1]) <= VERIFY_ROUND["plan_cap_usd"]
           and VERIFY_ROUND["candidate_seed"] != DESIGN["seed"] and VERIFY_ROUND["pick_seed"] != DESIGN["seed"] + 1,
           f"F25 the verification round runs only the chosen point and {VERIFY} new sample points ({q['arm_runs']} arm "
           f"runs; wave 2's {WAVE2['n']} design points come from its cache), ${q['usd'][0]}-{q['usd'][1]} on the VM "
           f"bound, ${q['usd_real'][0]}-{q['usd_real'][1]} measured (plan cap ${VERIFY_ROUND['plan_cap_usd']}); the "
           f"candidates and the picks take new seeds {VERIFY_ROUND['candidate_seed']}, {VERIFY_ROUND['pick_seed']}")

        class Store:
            def __init__(self, data):
                self.data, self.asked = data, []

            def download(self, blob, path, account=None):
                self.asked.append((blob, account))
                open(path, "wb").write(self.data)
                return {"blob": blob}
        body = b'{"key": "x"}\n'
        sha = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            st = Store(body)
            got = fetch_cache("outputs/x/netfit_cache.jsonl", sha, d, "acct", store=st)
            fine = got == d and open(os.path.join(d, CACHE_NAME), "rb").read() == body \
                and st.asked == [("outputs/x/netfit_cache.jsonl", "acct")]
            try:
                fetch_cache("outputs/x/netfit_cache.jsonl", "0" * 64, os.path.join(d, "b"), "acct", store=Store(body))
                refused = ""
            except ValueError as ex:
                refused = str(ex)
        ok(fine and "sha256" in refused and VERIFY_ROUND["cache_sha256"] == "3bde5c6ec7b6e6b204968b3a52b33541300316d1f0"
           "aa7f571bb5b30f91f3311b",
           f"F25b wave 2's checkpoint is read from storage only under its recorded sha256 (else refused: {refused!r})")
        rr = RecalRunner.__new__(RecalRunner)
        rr.ran_tasks = 3
        try:
            no_rerun(rr, "wave 2's design")
            r2 = ""
        except ValueError as ex:
            r2 = str(ex)
        rr.ran_tasks = 0
        no_rerun(rr, "wave 2's design")
        ok("3 engine task" in r2, f"F25c a verification round that would rerun a design point stops before verifying "
                                  f"({r2!r})")
    case("F25", f25)

    def f26():
        s8 = list(base["seeds"])
        bd = {"B11": [0.42, 0.70]}
        full = {"point": {}, "per_seed": {"B11": [0.2, 0.4] + [0.2] * 6}}
        trunc = {"point": {}, "per_seed": {"B11": [0.2, 0.4]}}
        rf, rt = point_in_band(full, bd, s8), point_in_band(trunc, bd, s8)
        ok(len(s8) == 8 and rf[0] is False and rt[0] is False and rt[1]["B11"] is None,
           f"F26 (Codex P2, PR #233) the reviewer's probe: B11 seed values [0.2, 0.4] + [0.2]*6 fail, and the list "
           f"truncated to [0.2, 0.4] is refused, not passed, against the {len(s8)} declared seeds ({rf[0]}, {rt})")
        good = [0.50, 0.52, 0.48, 0.51, 0.49, 0.50, 0.53, 0.47]
        cases = {"one value per declared seed": (good, s8, True),
                 "a missing seed (7 values)": (good[:7], s8, False),
                 "an extra seed (9 values)": (good + [0.5], s8, False),
                 "a duplicate declared seed": (good, s8[:7] + [s8[0]], False),
                 "no declared seeds": (good, [], False),
                 "declared seeds missing": (good, None, False),
                 "a non-integer declared seed": (good, s8[:7] + ["x"], False),
                 "a boolean declared seed": (good, s8[:7] + [True], False),
                 "values as a mapping (Codex r1 P2)": ({0.5 + i / 100: None for i in range(8)}, s8, False)}
        got = {k: point_in_band({"per_seed": {"B11": v}}, bd, sd)[0] for k, (v, sd, _) in cases.items()}
        wrong = sorted(k for k, (_, _, w) in cases.items() if got[k] is not w)
        ok(not wrong, f"F26b exactly one seed value per declared seed; a missing, extra or duplicate seed, or no valid "
                      f"declaration, fails closed ({len(cases)} cases; wrong: {wrong or 'none'})")
        pt = {"point": {"a": 0.1}, "per_seed": {"B11": [0.5, 0.52]}, "calibration_check": {}}
        ch = {"per_seed": {"B11": good}, "calibration_check": {}}
        r1, c1 = finalize({"sample": [{"a": 0.1}]}, [copy.deepcopy(pt)], copy.deepcopy(ch), bands=bd, seeds=s8)
        r2, c2 = finalize({"sample": [{"a": 0.1}]}, [dict(pt, per_seed={"B11": good})], copy.deepcopy(ch), bands=bd,
                          seeds=s8[:7] + [s8[0]])
        r3, c3 = finalize({"sample": [{"a": 0.1}]}, [dict(pt, per_seed={"B11": good})], copy.deepcopy(ch), bands=bd,
                          seeds=s8)
        ok(c1 == 2 and any("2 seed values" in w and "8 declared seeds" in w for w in r1["acceptance"])
           and c2 == 2 and any("duplicate" in w for w in r2["acceptance"])
           and c3 == 0 and r3["verified"]["seeds"] == s8,
           f"F26c finalize() names the seed mismatch ({r1['acceptance']}; {r2['acceptance']}) and records the seeds "
           f"it checked against")
    case("F26", f26)

    def f27():
        s8 = list(base["seeds"])
        v = [0.40, 0.43, 0.41, 0.42, 0.39, 0.42, 0.41, 0.43]
        lo, hi = seed_interval(v, len(v))
        lg = np.log(v)
        h = 1.96 * float(np.std(lg, ddof=1)) / np.sqrt(len(v))
        gm = float(np.exp(np.mean(lg)))
        ok(abs(lo - np.exp(np.mean(lg) - h)) < 1e-12 and abs(hi - np.exp(np.mean(lg) + h)) < 1e-12
           and abs(np.sqrt(lo * hi) - gm) < 1e-12,
           f"F27 the rule's interval is on the log scale around the geometric mean of the seed values, the statistic "
           f"each target's `computed` is (benchmark.py: exp(mean(log per_seed))): [{lo:.4f}, {hi:.4f}] around {gm:.4f}")
        skew = [0.1] * 7 + [2.0]          # arithmetic mean 0.34 (its interval reaches 0.80); geometric mean 0.15
        ok(point_in_band({"per_seed": {"B11": skew}}, {"B11": [0.42, 0.70]}, s8)[0] is False
           and point_in_band({"per_seed": {"B11": [0.5, 0.0] + [0.5] * 6}}, {"B11": [0.42, 0.70]}, s8)[0] is False
           and point_in_band({"per_seed": {"B11": [0.5, -0.1] + [0.5] * 6}}, {"B11": [0.42, 0.70]}, s8)[0] is False,
           "F27b a skewed seed set whose arithmetic interval overlaps the band but whose geometric one does not "
           "fails; a zero or negative seed value fails closed (no log)")
        r, _ = finalize({"sample": [{"a": 0.1}]}, [{"point": {"a": 0.1}, "per_seed": {"B11": v}, "calibration_check": {}}],
                        {"per_seed": {"B11": v}, "calibration_check": {}}, bands={"B11": [0.42, 0.70]}, seeds=s8)
        ok(r["verified"].get("acceptance_statistic") == ACCEPTANCE_STATISTIC and "geometric" in ACCEPTANCE_STATISTIC
           and "log" in ACCEPTANCE_STATISTIC and "geometric" in ACCEPTANCE_RULE,
           f"F27c the output records the statistic the rule used: {r['verified'].get('acceptance_statistic')!r}")
    case("F27", f27)

    # F6 option A (owner, 18 Nov; OA3): the net-effect parameters are fitted to the net trials at their own estimands,
    # so the PBO trials (B9, B9b, B9c, LLINEUP's B9d-B9f) and Misungwi's dual-AI vs PBO contrast (B9g) become training
    # targets beside B10, B11 and the IRS targets; B7, B8, B15 and the rest stay held out
    def fa1():
        tr, ho = split(p)
        ok(tr == OPTION_A["targets"] and base["fit_joint"]["targets"] == WAVE1_TARGETS
           and {"B9c", "B9d", "B9e", "B9f", "B9g", "B10", "B11"} <= set(tr) and {"B9", "B9b"} <= set(ho)
           and {"B12", "B12b", "B12m"} <= set(tr) and {"B7", "B8", "B15"} <= set(ho) and not set(tr) & set(ho),
           f"FA1 option A: the net-effect fit's own declaration (fit record (b)) is the net trials at their own "
           f"estimands plus the IRS targets ({tr}); the pack's fit_joint keeps {WAVE1_TARGETS}, so fitted.yaml stays "
           f"valid; B7, B8, B15 and the rest are held out")
    case("FA1", fa1)

    def fa2():
        card = PR.load_card(e)["priors"]
        bx = p["fit_joint"]["params"]
        ok(sorted(bx) == sorted(["net_kill_shift", "pbo_gain_scale", "pbo_adv_decay", "net_deter_sat", "cfp_kill",
                                 "irs_kill", "cfp_half"])
           and "pbo" in train_nets(p, split(p)[0])
           and all(bx[k] == [card[k].lo, card[k].hi] for k in ("net_kill_shift", "pbo_gain_scale", "net_deter_sat"))
           and bx["pbo_adv_decay"] == [PAD_CEILING["lo"], PAD_CEILING["hi"]]       # refit b's ceiling (FB7)
           and calibration_params(p) == {"net_kill_shift"}
           and _fit_settings(p) == ["benin", "irs_added", "llineup", "misungwi", "muleba"],
           f"FA2 option A fits net_kill_shift, pbo_gain_scale, pbo_adv_decay, net_deter_sat (v0.18), cfp_kill, "
           f"cfp_half and irs_kill; the four net-effect boxes are their card priors; the calibration arms still depend on net_kill_shift only; "
           f"the LLINEUP site joins ({ {k: bx[k] for k in sorted(bx)} })")
    case("FA2", fa2)

    def fa3():
        q = plan(p, refit_b=False)              # the 18 Nov fresh fit's plan (refit b reuses it: FB4)
        nd = OPTION_A["design_n"]
        ns_, nset = len(base["seeds"]), 5
        ok(nd == 3 * (7 + 1) * (7 + 2) // 2 and q["wave1_points_run"] == nd + 2
           and q["waves"]["design"] == _waves((nd + 2) * nset * ns_, VCPUS) + _waves(OPTION_A["wave2_n"] * nset * ns_,
                                                                                    VCPUS)
           and q["waves"]["chosen"] == 2 * nset * _waves(ns_, VCPUS)
           and q["design_points"] == OPTION_A["wave2_n"]
           and q["reused_points"] == 0 and q["calibration_reused"] is False and q["calibration_runs"] > 0
           and q["not_fitted"] == [] and len(q["settings"]) == 5,
           f"FA3 the option A plan runs a fresh fit: no wave-1 point or calibration grid is reused (the training "
           f"targets changed), {nd} wave-1 points (3 per quadratic coefficient in 7 dimensions), "
           f"{OPTION_A['wave2_n']} wave-2 points, the calibration grid ({q.get('calibration_runs')} runs); wave 1 "
           f"also runs benchmark.fit_joint's centre point and its own chosen point (Codex r1 P1-3), each design batch "
           f"priced on its own: "
           f"${q['usd_real'][0]}-{q['usd_real'][1]} at the measured price")
    case("FA3", fa3)

    def fa4():
        got, saved, rb_saved = {}, OPTION_A["cap_usd"], OPTION_A.get("refit_b")
        OPTION_A["cap_usd"] = None             # Codex r1 P2: the refusal is tested whatever the production cap is
        OPTION_A["refit_b"] = None             # the fresh fit's refusals (refit b refuses earlier without its cache)
        try:
            for label, kw in (("the old verification round", {"verify_round": True}), ("no owner cap", {})):
                try:
                    run(p, base, 1, os.path.join(HERE, "x.yaml"), approved="x", smoke=False, cap_usd=1e9, **kw)
                    got[label] = ""
                except ValueError as ex:
                    got[label] = str(ex)
        finally:
            OPTION_A["cap_usd"], OPTION_A["refit_b"] = saved, rb_saved
        # the old round is refused at its plan (WAVE1_TARGETS, FA6) before load_wave1 (declarations) is reached
        ok(("declarations" in got["the old verification round"] or "WAVE1_TARGETS" in got["the old verification round"])
           and "cap" in got["no owner cap"],
           f"FA4 option A refuses the old rounds' reuse (wave 1 ran on other fit declarations) and a full fit whose "
           f"cap is not the owner's (OPTION_A cap_usd unset until the owner sets it): {got}")
    case("FA4", fa4)

    def fa5():
        # Codex r1 P1-1: the owner's cap is the cap (no historical $25 floor under it); a caller may only lower it
        got = {}
        for label, args in (("owner 100, caller none", (False, False, None, 100.0)),
                            ("owner 100, caller 40", (False, False, 40.0, 100.0)),
                            ("owner 100, caller 400", (False, False, 400.0, 100.0)),
                            ("verify round, caller none", (True, False, None, None)),
                            ("smoke", (False, True, None, None)),
                            ("caller nan", (False, False, float("nan"), 100.0)),
                            ("owner inf", (False, False, None, float("inf")))):
            try:
                got[label] = run_cap(*args)
            except ValueError as ex:
                got[label] = str(ex)
        ok(got["owner 100, caller none"] == 100.0 and got["owner 100, caller 40"] == 40.0
           and got["owner 100, caller 400"] == 100.0
           and got["verify round, caller none"] == min(CAP_USD, VERIFY_ROUND["plan_cap_usd"])
           and got["smoke"] is None and "finite" in str(got["caller nan"]) and "finite" in str(got["owner inf"]),
           f"FA5 option A's cap is the owner's OPTION_A cap, which a caller may lower but not raise; the verification "
           f"round keeps its own: {got}")
    case("FA5", fa5)

    def fa6():
        # Codex r1 P1-2, P2: an invalid verification count, a placeholder approval and a historical plan on this pack
        # are refused before pricing or any engine set-up
        got = {}
        for label, fn in (("verify -100000", lambda: plan(p, verify=-100000)),
                          ("verify 0", lambda: plan(p, verify=0)),
                          ("verify round on the option A pack", lambda: plan(p, verify_round=True)),
                          ("placeholder approval", lambda: run(p, base, 1, os.path.join(HERE, "x.yaml"),
                                                               approved="<owner approval: x>", smoke=False))):
            try:
                fn()
                got[label] = ""
            except ValueError as ex:
                got[label] = str(ex)
        ok("placeholder" in got["placeholder approval"] and "whole number" in got["verify -100000"]
           and "whole number" in got["verify 0"] and "WAVE1_TARGETS" in got["verify round on the option A pack"]
           and plan(p_w, verify_round=True)["launch"] is None,
           f"FA6 refused before pricing or engine set-up, each for its own reason: {got}")
    case("FA6", fa6)

    def fa7():
        # Codex r1 P1-4: the launch's vm_job timeout covers the plan's upper wall time (vm_job's default is 24 h)
        import shlex
        got = {}
        for v in (VERIFY, 1):
            q = plan(p, verify=v)
            cmd = shlex.split(q["launch"])                  # the printed command, as a shell reads it (Codex r2)
            mh = float(cmd[cmd.index("--max-hours") + 1]) if "--max-hours" in cmd else 0.0
            tail = cmd[cmd.index("--") + 1:]
            got[v] = (mh, q["wall_hours"][1], tail[tail.index("--verify") + 1] if "--verify" in tail else None,
                      cmd[cmd.index("--memory") + 1])
        ok(all(mh >= w + 1 and vv == str(v) and mem.endswith("GiB") for v, (mh, w, vv, mem) in got.items()),
           f"FA7 the printed launch passes --max-hours above the plan's upper wall time and the priced --verify "
           f"count, and reads as one argument per value: {got}")
    case("FA7", fa7)

    def fa8():
        # owner, 18 Nov: the output reports pbo_gain_scale and net_deter_sat's joint posterior (both move a PBO net's
        # killing per attempt at the trial sites)
        nm, bx = ["pbo_gain_scale", "net_deter_sat"], np.array([[0.316, 1.0], [0.0, 1.0]])
        g = np.linspace(0, 1, 7)
        X = np.array([[u, v] for u in g for v in g])
        r = joint_region(X, {"T": np.exp(X[:, 0] + X[:, 1])}, {"T": [np.exp(0.95), np.exp(1.05)]}, nm, bx, 3)
        c = confounding(r)
        doc = document(e, r, {}, True)
        ok(len(c) == 1 and c[0]["pair"] == nm and c[0]["correlation"] < -0.9 and doc.get("confounding") == c
           and confounding({"correlation": {"names": ["pbo_gain_scale"], "matrix": [[1.0]]}}) == [],
           f"FA8 the fit output records each confounded pair's correlation in the joint sample ({c})")
    case("FA8", fa8)

    def fa9():
        # Codex review of the 7-parameter extension: an undefined correlation (0-2 accepted rows, matrix None) is
        # recorded as such, never a crash before the output is written; the coefficient and count come from the
        # exported rows the decision analysis draws (not every accepted candidate)
        nm = ["pbo_gain_scale", "net_deter_sat"]
        got = {}
        for n in (0, 1, 2):
            rows = [{"pbo_gain_scale": 0.5 + 0.1 * i, "net_deter_sat": 0.2 * i} for i in range(n)]
            got[n] = confounding({"correlation": {"names": nm, "matrix": None}, "sample": rows})
        rng = np.random.default_rng(1)
        u = rng.random(400)
        rows = [{"pbo_gain_scale": float(a), "net_deter_sat": float(1 - a + 0.01 * b)}
                for a, b in zip(u, rng.random(400))]
        c = confounding({"correlation": {"names": nm, "matrix": [[1.0, 0.9], [0.9, 1.0]]}, "sample": rows})
        want = float(np.corrcoef([r[nm[0]] for r in rows], [r[nm[1]] for r in rows])[0, 1])
        ok(all(g and g[0]["correlation"] is None and g[0]["sample_n"] == n for n, g in got.items())
           and abs(c[0]["correlation"] - want) < 1e-4 and c[0]["sample_n"] == 400,
           f"FA9 confounding: undefined with fewer than 3 rows (None, not a crash); computed on the exported rows "
           f"({c[0]['correlation']:.3f} vs the candidates' 0.9)")
    case("FA9", fa9)

    def fa10():
        # Codex review: a fresh fit runs only on option A's declaration (a fit on the pack's own five targets would be
        # labelled option A and its posterior refused at replay); the history's targets only replay
        got = ""
        try:
            run(p_w, base, 1, os.path.join(HERE, "x.yaml"), approved="x", smoke=True)
        except ValueError as ex:
            got = str(ex)
        ok("OPTION_A" in got and OPTION_A["job"] == "f6-refit-optiona-b" and "v0.18" in PURPOSE,
           f"FA10 a fresh fit (or smoke) on another declaration is refused before any engine set-up ({got[:120]}); the "
           f"launch job is {OPTION_A['job']} (an f6 budget prefix)")
    case("FA10", fa10)

    def fa11():
        # owner, 18 Nov (one VM, 96 vCPUs): a chunk barrier of 300 tasks idled up to a third of a large VM at each
        # chunk's tail; a chunk is now max(300, 4 x workers) (still a fresh pool per chunk, for worker memory)
        q = plan(p, vcpus=96, refit_b=False)
        nd, ns_, nset = OPTION_A["design_n"] + 2, len(base["seeds"]), 5
        ok(_chunk(16) == 300 and _chunk(96) == 384 and _chunk(1) == 300
           and q["waves"]["design"] == math.ceil(nd * nset * ns_ / 96) + math.ceil(OPTION_A["wave2_n"] * nset * ns_ / 96)
           and "_chunk(self.workers)" in __import__("inspect").getsource(RecalRunner._map),
           f"FA11 chunks of max(300, 4 x workers) in the runner and the plan: 96 vCPUs run {q['waves']['design']} "
           f"design waves, {q['wall_hours']} h")
    case("FA11", fa11)

    def fa12():
        ok(shipped_cap == 100.0, f"FA12 the owner's cap for option A refit b (19 Nov, 1 VM of 96 vCPUs) is $100 (the "
                                 f"18 Nov fit's was $250): {shipped_cap}")
    case("FA12", fa12)

    def fa13():
        # owner, 18 Nov: the Esv5 family (25 x E8s_v5 quota on record; Dsv5 headroom unknown). Coiled tries the first
        # type and does not fall back on a quota refusal, so the launch's memory must make an E*s_v5 type first
        import shlex
        from cloud import budget as BU
        got = {}
        for v in (16, 96):
            cmd = shlex.split(plan(p, vcpus=v)["launch"])
            mem = cmd[cmd.index("--memory") + 1]
            got[v] = BU.resolve_vm_types(v, mem)[0][0]
        ok(got == {16: "Standard_E16s_v5", 96: "Standard_E96s_v5"},
           f"FA13 the printed launch resolves to the Esv5 family first ({got})")
    case("FA13", fa13)

    # F6 option A refit b (owner, 19 Nov): Muleba's B9 and B9b leave the training targets (declared departure) and are
    # monitored; the stored cache of 18 Nov is reused (its tasks must match exactly); the wave-2 box is the box as ran;
    # --margin-loo off; an optional extension wave past pad's old ceiling
    rb = OPTION_A.get("refit_b") or {}

    def fb1():
        ok(OPTION_A["targets"] == ["B9c", "B9d", "B9e", "B9f", "B9g", "B10", "B11", "B12", "B12b", "B12m"]
           and OPTION_A.get("monitor") == ["B9", "B9b"] and "0/220" in str(OPTION_A.get("departure"))
           and not set(OPTION_A["monitor"]) & set(OPTION_A["targets"]) and set(OPTION_A["monitor"]) <= set(held),
           f"FB1 B9 and B9b are held out of the fit and monitored, with the departure stated ({OPTION_A.get('monitor')})")
    case("FB1", fb1)

    def fb2():
        # the 18 Nov tasks held every arm of the 12 targets per setting and seed; dropping B9/B9b would leave Muleba's
        # tasks without mul_pbo and miss the cache: the runner adds a monitored target's arms to a call that runs its
        # setting, so the tasks are the same
        rr = RecalRunner(p, e, 1, [11], mode="fit", evidence=BM.load_evidence(e, pack=p))
        seen = []
        rr._arm_jobs = lambda a, f: (seen.append(a), [("job", a)])[1]
        rr._map = lambda jobs, chunk=300: [{"windows": {}} for _ in jobs]
        rr.set_calibration({"muleba": 60.0, "misungwi": 11.0, "benin": 10.0, "irs_added": 20.0, "llineup": 3.3})
        arms10 = sorted({a for t in OPTION_A["targets"] for a in BM._arms_of(byid[t]["estimand"])})
        arms12 = sorted(set(arms10) | {a for t in OPTION_A["monitor"] for a in BM._arms_of(byid[t]["estimand"])})
        rr.prefetch(arms10, [{"net_kill_shift": 0.0, "cfp_kill": 0.4}])
        seen2 = list(seen)
        seen.clear()
        rr.prefetch(["mis_dual", "mis_pyr"], [{"net_kill_shift": 0.1, "cfp_kill": 0.4}])
        ok(seen2 == arms12 and seen == ["mis_dual", "mis_pyr"],
           f"FB2 a call running Muleba gains the monitored arms in the 18 Nov order ({seen2}); one without it does not "
           f"({seen})")
    case("FB2", fb2)

    def fb3():
        got = {}
        for label, kw in (("no cache", {}), ("margin-loo", {"margin_loo": True, "resume_from": "/nonexistent"}),
                          ("another cache", {"resume_from": HERE})):
            try:
                run(p, base, 1, os.path.join(HERE, "x.yaml"), approved="x", smoke=False, cap_usd=1e9, **kw)
                got[label] = ""
            except ValueError as ex:
                got[label] = str(ex)
        ok(rb.get("cache_sha256") == "b0c557b808a0b89905ba16b633868957a9794dc72346c9cba2fb363d368cebff"
           and "resume" in got["no cache"] and "margin" in got["margin-loo"] and "sha256" in got["another cache"],
           f"FB3 refit b runs only from the stored 18 Nov cache (pinned sha256) and without --margin-loo: {got}")
    case("FB3", fb3)

    def fb4():
        q = plan(p, vcpus=96)
        ext = rb.get("extension") or {}
        n_ext = int(ext.get("n") or 0)
        # Codex review of #285: the monitored arm (mul_pbo) runs with Muleba, so the plan counts it
        ok(q.get("refit_b") is True and "mul_pbo" in q["arms"] and len(q["arms"]) == 12
           and q["calibration_runs"] == 0 and q["design_points"] == n_ext
           and q["wave1_points_run"] == 1 and q["arm_runs"] < 30000,
           f"FB4 refit b prices only new tasks: wave 1's re-chosen point, {n_ext} extension points, the chosen point "
           f"and {VERIFY} sample points ({q['arm_runs']} arm runs, {q['wall_hours']} h, ${q['usd_real']})")
    case("FB4", fb4)

    def fb5():
        import shlex
        OPTION_A["cap_usd"] = shipped_cap
        try:
            cmd = shlex.split(plan(p, vcpus=96)["launch"])
        finally:
            OPTION_A["cap_usd"] = None
        tail = cmd[cmd.index("--") + 1:]
        ok(cmd[3] == "f6-refit-optiona-b" and "--dir" in cmd and "--tree" not in cmd
           and tail[tail.index("--resume-from") + 1] == rb.get("resume_dir") and "--margin-loo" not in tail
           and cmd[cmd.index("--cap-usd") + 1] == "100",
           f"FB5 the refit-b launch ships the tree and the cache (--dir), resumes from it, no --margin-loo, cap $100: "
           f"{' '.join(cmd)}")
    case("FB5", fb5)

    def fb6():
        # the extension wave (when set): LHS points with pad past the old ceiling and every other parameter over the
        # design box; the box the surrogate and the choice use then reaches the new ceiling
        ext = {"n": 7, "seed": 37, "param": "pbo_adv_decay", "range": [1.895, 2.23]}
        pts = extension_points(rb["design_box"], ext)
        dbx = rb["design_box"]
        ok(len(pts) == 7 and all(1.895 <= q["pbo_adv_decay"] <= 2.23 for q in pts)
           and all(dbx[k][0] <= q[k] <= dbx[k][1] for q in pts for k in dbx if k != "pbo_adv_decay")
           and extension_points(dbx, None) == [],
           f"FB6 the extension wave lies past the old ceiling, inside the design box elsewhere ({pts[:1]})")
    case("FB6", fb6)

    def fb7():
        # owner, 19 Nov, option (a): pbo_adv_decay's ceiling is 2.23 per year (LLINEUP's 99% waning bound minus the
        # engine's own waning at x = 0.30); the fit's box and the posterior's hard bound reach it; 100 extension points
        # lie in 1.895-2.23; the card prior file is not edited (its test sits inside the engine's code fingerprint)
        from toolkit.engines.malaria import vector as V
        pt, lo, hi = V.adv_decay_from_prs((0.74, 0.62, 0.87), (0.84, 0.72, 0.98), 1.0, clip=False)
        s = (hi - pt) / 1.959964
        k30 = V.engine_adv_decay(0.30, 182.5, 547.5, dict(V.NET_DEFAULTS, cfp_kill=0.0, cfp_half=1.0,
                                                           net_deter_sat=PAD_CEILING["theta"]))  # at the fitted theta
        ceil = round(pt + 2.575829 * s - k30, 2)
        hb = hard_bounds(e, ["pbo_adv_decay"])["pbo_adv_decay"]
        ext = rb.get("extension") or {}
        ok(ceil == 2.23 == PAD_CEILING["hi"] and NET_PARAMS["pbo_adv_decay"][1] == [0.0, 2.23]
           and (hb[0], hb[1]) == (0.0, 2.23) and ext.get("n") == 100 and ext.get("range") == [1.895, 2.23]
           and ext.get("param") == "pbo_adv_decay" and rb["design_box"]["pbo_adv_decay"] == [0.0, 1.895]
           and p["fit_joint"]["params"]["pbo_adv_decay"] == [0.0, 2.23],
           f"FB7 the widened pbo_adv_decay ceiling {ceil} (99% LLINEUP bound {pt + 2.575829 * s:.3f} minus the engine's "
           f"waning at x 0.30 {k30:.3f}) sets the fit box and the hard bound; 100 extension points ({hb}, {ext})")
    case("FB7", fb7)

    def fb8():
        # owner, 19 Nov: a one-line diagnostic: does the chosen pbo_adv_decay sit within 2% of the new ceiling
        near, far = edge_diagnostic({"pbo_adv_decay": 2.20}), edge_diagnostic({"pbo_adv_decay": 1.60})
        ok(near["within_2pct"] is True and far["within_2pct"] is False and near["ceiling"] == 2.23
           and "cannot reproduce LLINEUP's waning" in near["line"] and "cannot" not in far["line"],
           f"FB8 the edge diagnostic: {near['line']} | {far['line']}")
    case("FB8", fb8)
    OPTION_A["cap_usd"] = shipped_cap
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
    ap.add_argument("--resume-from", default=None, help=f"a directory holding an earlier run's {CACHE_NAME}")
    ap.add_argument("--verify-round", action="store_true", help="VERIFY_ROUND: wave 2's points from its stored "
                    "checkpoint, a new joint sample verified (owner, 14 Nov)")
    ap.add_argument("--resume-account", default=None, help="storage account of the stored checkpoint")
    ap.add_argument("--margin-loo", action="store_true", help="export only candidates in band by each target's local "
                    "leave-one-out error (owner, 13 Nov; off by default)")
    a = ap.parse_args(argv)
    base = yaml.safe_load(open(PACK))
    pack = build_pack(base, a.smoke)
    if a.action == "plan":
        print(yaml.safe_dump(plan(pack, a.verify, vcpus=a.vcpus, verify_round=a.verify_round), sort_keys=False))
        return 0
    if a.smoke and not a.out:
        print("--smoke needs --out (a scratch file): a smoke is not evidence")
        return 2
    return run(pack, base, a.workers, a.out or OUT, a.approved, a.smoke, 2 if a.smoke else a.verify,
               resume_from=a.resume_from, margin_loo=a.margin_loo, verify_round=a.verify_round,
               resume_account=a.resume_account)


if __name__ == "__main__":
    sys.exit(main())
