#!/usr/bin/env python3
"""Fixture runs for the review workbench (docs/design/review-workbench.md §9, §3a).

    python3 tools/workbench/fixtures.py <dir> [--method M] [--defect NAME] [--upto STAGE]   # write one run
    python3 tools/workbench/fixtures.py --list                                               # every planted edit
    python3 tools/workbench/fixtures.py --self-test

`build(dir, upto=None, defect=None, method="history_matching")` writes a small synthetic run: three groups, twelve
units, two survey rounds, three shared parameters, components A and B. It holds every file the Steps view (§5.2)
and the Fit view (§5.5) read, with the key names and nesting of a stored v2 run for that calibration method:

- `history_matching`: analysis/joint_calibration.yaml (and the small analysis/calibration.yaml a real run writes
  beside it). Keys the views do not read (figure_data.points, pairs, transmission, design_share; the full copy of
  the report under each sensitivity setting) are left out or shortened; nothing is renamed.
- `posterior`: analysis/calibration.yaml with `data.method: posterior`, and analysis/estimates.yaml with the
  predictive band of a fit over time (a stored run writes that file for the `estimates` deliverable; the fixture
  carries it beside its allocation so the Fit view's `series` table has a case).

Names are neutral on purpose (§3a): units U01.., groups G1.., parameters p1.., components A and B, and a grouping
column called `stratum` so that a view which hard-codes a column name fails. No figure image is written.

The files agree with each other as a stored run's do: a change between rounds in survey_changes.pairs is computed
from the run's own target rows (so a planted change moves those rows), and a planted block also shows in the table
the block is about (the offset's own row).

On the clean run every check is `ok` or `not_available`. `defect` names one entry of DEFECTS and applies that one
edit; files that record a hash of an edited file (later analysis files, the manifest, the sign-off record) follow
from it, as they would in a real run. `upto` names a stage of STAGES and leaves out every file a later stage writes.
The launch commit recorded is this checkout's HEAD, so the Steps `pipeline` check is ok on a clean fixture.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
METHODS = ("history_matching", "posterior")
STAGES = ("launch", "sign-off", "calibration", "analysis", "final")
GROUPING = "stratum"
GROUPS = ("G1", "G2", "G3")
UNITS = tuple(f"U{i:02d}" for i in range(1, 13))
PARAMS = ("p1", "p2", "p3")
PRIOR = {"p1": [0.2, 0.8], "p2": [1.0, 5.0], "p3": [10.0, 50.0]}
RANGE = {"p1": [0.42, 0.58], "p2": [1.6, 4.4], "p3": [14.0, 46.0]}
BEST = {"p1": 0.5, "p2": 3.1, "p3": 28.0}
CUTOFF, Z_MAX, UNIT_LO, UNIT_HI = 3.0, 3.0, -1.0, 6.0
SD_OBS, SD_BIAS, SD_MD = 0.3, 0.15, 0.5                # a target's error terms; the discrepancy is the largest, so a
#                                                        level can be matched while the change between rounds is not
CHANGE_PARTS = {"sampling": 0.31, "offset": 0.15, "run": 0.06}     # the sd of a change leaves the discrepancy out
OFFSET = "S2|G2"                                       # the offset key the declared variants name
JC, CAL, UNC, DT = ("analysis/joint_calibration.yaml", "analysis/calibration.yaml", "analysis/uncertainty.yaml",
                    "analysis/decision_tests.yaml")
EST = "analysis/estimates.yaml"                        # the posterior fixture's fit over time (a predictive band)
SPEC_FILES = ["decision_spec.yaml", "decision_brief.signed/decision_spec.yaml"]
ENGINE = {"name": "toy-engine", "version": "0.1", "card_sha256": hashlib.sha256(b"toy card").hexdigest()}
HARNESS_INPUT = {"../../toolkit/engines/toy/priors.yaml": hashlib.sha256(b"toy priors").hexdigest()}
# Which stage writes each file. run_state.json and metadata.json are written at every stage.
STAGE_OF = {"decision_spec.yaml": "launch", "data/derived/units.csv": "launch", "data/derived/targets.csv": "launch",
            "metadata.json": "launch", "run_state.json": "launch",
            "decision_spec.signoff.json": "sign-off", "decision_brief.signed/decision_spec.yaml": "sign-off",
            JC: "calibration", CAL: "calibration", "figures/": "calibration",
            "analysis/point.yaml": "analysis", UNC: "analysis", DT: "analysis", EST: "analysis", "allocation.csv": "analysis",
            "analysis/manifest.json": "analysis"}
HISTORY = [("SPEC", "SIGN", 30, 1.0), ("SIGN", "EVIDENCE", 60, 1.0), ("EVIDENCE", "ENGINE_CHOICE", 70, 1.4),
           ("ENGINE_CHOICE", "CONFIGURE", 80, 1.8), ("CONFIGURE", "ANALYZE", 90, 2.0), ("ANALYZE", "CRITIQUE", 240, 2.0),
           ("CRITIQUE", "GATE", 260, 3.0), ("GATE", "WRITE", 270, 3.0), ("WRITE", "PRESENTATION", 290, 3.6),
           ("PRESENTATION", "FINAL", 300, 3.8)]
REACHED = {"launch": 1, "sign-off": 4, "calibration": 5, "analysis": 6, "final": 10}   # transitions made by each stage


def _r(x):
    return round(float(x), 6)


def _logit(p):
    return math.log(p / (1 - p))


def _expit(x):
    return 1 / (1 + math.exp(-x))


def _at_error(value, err):
    """The model value that sits `err` from an observed value on the logit scale (kept to ten places, so a change
    recomputed from the stored rows agrees with the one recorded)."""
    return round(_expit(_logit(value) + err), 10)


def _group(u):
    return GROUPS[UNITS.index(u) // 4]


def _wobble(i):
    return ((i * 5) % 7) - 3            # -3..3, fixed per unit: the fixture has no random numbers


def _at(minute):
    return f"2026-01-01T{minute // 60:02d}:{minute % 60:02d}:00+00:00"


# ------------------------------------------------------------------------------------------- the run, as objects

def _spec(method):
    spec = {"schema_version": 1, "question": "Which package should each unit get? (synthetic fixture)",
            "decision_date": "2026-06-01", "engine": {"name": ENGINE["name"]},
            "decision_units": {"csv": "data/derived/units.csv", "id": "unit_id", "label": "name", "region": GROUPING},
            "options": {"components": {"A": {}, "B": {}}, "packages": {"generate": True}},
            "comparator": {"package": "A"},
            "uncertainty": {"draws": 50},
            "analysis": {"decision_tests": [{"id": "DT1", "type": "parameter_values", "parameter": "p2",
                                             "values": [1.6, 4.4], "text": "Does the plan change across p2's range?"}]},
            "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation.csv",
                            "columns": {"unit": "unit_id", "package": "package", "value": "outcome_averted"}}}
    if method == "history_matching":
        spec["calibration"] = {"method": "emulator_inversion", "joint": {
            "shared": list(PARAMS), "discrepancy_sd": SD_MD, "waves": 1, "points_per_unit": 4, "shared_points": 16,
            "targets": [{"id": "unit_r2", "kind": "unit", "column": "target_r2", "window": [2021.5, 2022.0],
                         "metric": "m1", "scale": "logit", "sd": SD_OBS, "bias_sd": SD_BIAS, "bias_by": GROUPING,
                         "bias_source": "S2", "survey": "survey_a", "source": _source("unit")},
                        {"id": "grp_r1", "kind": "group", "csv": "data/derived/targets.csv",
                         "filter": {"round_id": "S1"}, "value": "value_pct", "multiplier": 0.01,
                         "ci": ["ci_low_pct", "ci_high_pct"], "ci_level": 0.95, "window": [2018.5, 2019.0],
                         "metric": "m1", "scale": "logit", "survey": "survey_a", "source": _source("group")}],
            "sensitivity": [{"id": "sd_015", "label": "sd 0.15 on the round 2 unit target",
                             "targets": {"unit_r2": {"sd": 0.15}}}],
            "unmatched": {"mode": "flag", "max_units": 2, "declared": []}}}
    else:
        spec["calibration"] = {"method": "posterior", "parameters": list(PARAMS),
                               "priors": {p: {"dist": "uniform", "range": PRIOR[p]} for p in PARAMS},
                               "sources": [{"id": s, "latent": {"quantity": q}, "noise": {"model": "binomial"}}
                                           for s, q in (("S1", "q1"), ("S2", "q2"))],
                               "targets": {"csv": "data/derived/targets.csv", "groups": GROUPING}}
    return spec


def _source(kind, assumed=False):
    if kind == "group":
        return "survey_a round 1 group value with its 95% interval (targets.csv)"
    tail = ("sd 0.3 for the spread within the group is an ASSUMPTION (no unit-level interval in the inputs)"
            if assumed else "sd 0.3 from the round's reported unit-level interval")
    return f"survey_a round 2 unit value from the units table (target_r2); {tail}"


def _unit_error(jc, u, err, emulator=None):
    """Unit u's one target: the engine's verification sits `err` from the observed value on the target's scale, with
    the implausibility that follows; the emulator's implausibility at the best point is `emulator` (0.8 of the
    engine's when not given)."""
    t = jc["unit_targets"][u][0]
    imp, sim = _r(abs(err) / t["sd_total"]), _at_error(t["value"], err)
    jc["units"][u].update(implausibility_verified=imp, implausibility_at_best=_r(0.8 * imp if emulator is None else emulator))
    jc["verification"]["units"][u].update(implausibility=imp)
    jc["verification"]["units"][u]["targets"][0].update(simulated=sim, implausibility=imp)
    next(f for f in jc["figure_data"]["fit"] if f["target"] == t["id"])["verified"] = sim


def _group_error(jc, g, err=None, emulator=None):
    """Group g's earlier-round target: the engine's verification sits `err` from the observed value, and the
    emulator's value at the best point `emulator` from it (each left as it is when not given)."""
    tid = f"grp_r1:{g}"
    t, row = jc["targets"][tid], next(f for f in jc["figure_data"]["fit"] if f["target"] == tid)
    if err is not None:
        sim = _at_error(t["value"], err)
        jc["verification"]["targets"][tid].update(simulated=sim, implausibility=_r(abs(err) / t["sd_total"]))
        row["verified"] = sim
    if emulator is not None:
        mid = _r(_expit(_logit(t["value"]) + emulator))
        t.update(modelled_at_best=mid, implausibility_at_best=_r(abs(emulator) / t["sd_total"]))
        row.update(model_lo=_r(mid * 0.9), model_mid=mid, model_hi=_r(mid * 1.1))


def _errors(jc, g):
    """(the engine's error on each unit target of group g, its error on the group's target), on the logit scale."""
    def err(t, sim):
        return _logit(sim) - _logit(t["value"])
    units = {u: err(jc["unit_targets"][u][0], jc["verification"]["units"][u]["targets"][0]["simulated"])
             for u in UNITS if _group(u) == g}
    return units, err(jc["targets"][f"grp_r1:{g}"], jc["verification"]["targets"][f"grp_r1:{g}"]["simulated"])


def _changes(jc):
    """survey_changes.pairs, computed from the target rows as the method computes them: for each group, the mean of
    its later-round unit targets minus its earlier-round group target, observed and modelled (the engine's
    verification), on the natural and on the targets' scale; z is the difference of the two over the sd of a change."""
    sd, pairs = _r(math.sqrt(sum(v * v for v in CHANGE_PARTS.values()))), []
    for g in GROUPS:
        us = [u for u in UNITS if _group(u) == g]
        obs = [jc["unit_targets"][u][0]["value"] for u in us]
        mod = [jc["verification"]["units"][u]["targets"][0]["simulated"] for u in us]
        obs_g, mod_g = jc["targets"][f"grp_r1:{g}"]["value"], jc["verification"]["targets"][f"grp_r1:{g}"]["simulated"]
        o, m = sum(map(_logit, obs)) / len(us) - _logit(obs_g), sum(map(_logit, mod)) / len(us) - _logit(mod_g)
        o_n, m_n = sum(obs) / len(us) - obs_g, sum(mod) / len(us) - mod_g
        z = _r((m - o) / sd)
        pairs.append({"id": f"survey_a:grp_r1->unit_r2:{g}", "survey": "survey_a", "from": "grp_r1", "to": "unit_r2",
                      "group": g, "by": GROUPING, "n_terms": len(us) + 1, "rho": 0.0, "observed": _r(o), "modelled": _r(m),
                      "observed_natural": _r(o_n), "modelled_natural": _r(m_n), "observed_natural_sd": 0.03,
                      "difference": _r(m - o), "sd": sd, "sd_parts": dict(CHANGE_PARTS), "z": z,
                      "flagged": abs(z) > Z_MAX, "ratio": _r(m_n / o_n),
                      "text": f"modelled change {_r(m_n)} against {_r(o_n)} observed",
                      "basis": "engine verification at the best point",
                      "modelled_region": {"q025": _r(m - 0.1), "median": _r(m), "q975": _r(m + 0.1)}})
    jc["survey_changes"].update(pairs=pairs, flagged=[p["id"] for p in pairs if p["flagged"]])


def _move_change(m, g, z):
    """Group g's change between rounds is moved to exactly z: 0.3 of the needed difference is put on the engine's
    values for the group's unit targets (all moved by the same amount) and the rest, with the other sign, on its
    group target, so that no single target comes near the cutoff; the emulator's values move with the engine's."""
    jc = m["jc"]
    sd = math.sqrt(sum(v * v for v in CHANGE_PARTS.values()))
    units, _ = _errors(jc, g)
    mean = sum(units.values()) / len(units)
    for u, e in units.items():
        _unit_error(jc, u, e - mean + 0.3 * z * sd)
    _group_error(jc, g, err=-0.7 * z * sd, emulator=-0.63 * z * sd)
    _changes(jc)
    pair = next(p for p in jc["survey_changes"]["pairs"] if p["group"] == g)
    pair["z"] = _r(z)           # the rows give z to within their rounding; the pair records the value intended


def _region():
    return {"candidates": 2048, "accepted": 600, "share_of_prior": 0.21, "relaxed": 0,
            "ranges": copy.deepcopy(RANGE), "prior_ranges": copy.deepcopy(PRIOR),
            "contraction": {p: _r(1 - (RANGE[p][1] - RANGE[p][0]) / (PRIOR[p][1] - PRIOR[p][0])) for p in PARAMS},
            "interval95": {p: {"region": [_r(RANGE[p][0] * 1.02), _r(RANGE[p][1] * 0.98)], "prior": PRIOR[p],
                               "median": BEST[p], "point": BEST[p], "contraction95": _r(
                                   1 - (RANGE[p][1] * 0.98 - RANGE[p][0] * 1.02) / (PRIOR[p][1] - PRIOR[p][0]))}
                           for p in PARAMS}}


def _thresholds():
    return {"history_matching": {"implausibility_cutoff": CUTOFF, "candidates_log2": 11, "region_min_candidates": 64,
                                 "refine_rounds": 2, "refine_sd": 0.05, "z_grid": 41, "group_t": 21, "group_tries": 20,
                                 "waves": 1, "points_per_unit": 4, "shared_points": 16, "draws_kept": 40,
                                 "min_target_sd": 1e-06, "include_run_noise": True, "bias_draws": 64,
                                 "bias_draws_max": 1024, "bias_accept_min": 0.001, "best_point_weighting": "per_group"},
            "emulator": {"holdout_share": 0.1, "std_error_max": 3.0, "outside_share_max": 0.05, "min_holdout": 10,
                         "min_points_per_unit": 4, "ridge": 1e-06},
            "blocking": {"unmatched_units_max": 0, "clamped_units_max": 0}}


def _joint():
    """analysis/joint_calibration.yaml `data`, clean."""
    sd_u, sd_g = _r(math.sqrt(SD_OBS ** 2 + SD_BIAS ** 2 + SD_MD ** 2)), _r(math.sqrt(SD_OBS ** 2 + SD_MD ** 2))
    units, unit_targets, v_units, fit, best_z = {}, {}, {}, [], {}
    for i, u in enumerate(UNITS):
        val, err, zb = 0.10 + 0.02 * i, 0.07 * _wobble(i), 1.0 + 0.3 * i
        imp, sim, tid = _r(abs(err) / sd_u), _at_error(val, err), f"unit_r2:{u}"
        top = i == len(UNITS) - 1                      # one unit of twelve reaches the upper bound on the clean run
        units[u] = {"matched": True, "status": "matched", "best_implausibility": _r(0.1 * imp),
                    "implausibility_at_best": _r(0.8 * imp), "z_best": _r(zb),
                    "z_interval": [_r(zb - 1.2), UNIT_HI if top else _r(zb + 1.2)],
                    "region_touches_bound": ["hi"] if top else [], "direction": None, "misses": None,
                    "z_verified": _r(zb), "implausibility_verified": imp}
        unit_targets[u] = [{"id": tid, "kind": "unit", "observable": "jc0.m1", "value": _r(val), "scale": "logit",
                            "sd_obs": SD_OBS, "sd_bias": SD_BIAS, "sd_md": SD_MD, "sd_total": sd_u, "source": _source("unit"),
                            "unit": u, "bias_key": f"S2|{_group(u)}"}]
        v_units[u] = {"implausibility": imp, "ok": True, "z": _r(zb), "runs": 4, "recorded": {"jc2.m2": _r(2.0 + i)},
                      "targets": [{"target": tid, "simulated": sim, "implausibility": imp}]}
        best_z[u] = _r(zb)
        mid = _expit(_logit(val) + 0.8 * err)
        fit.append({"target": tid, "kind": "unit", "unit": u, "observable": "jc0.m1", "observed": _r(val),
                    "obs_lo": _r(_expit(_logit(val) - 1.96 * sd_u)), "obs_hi": _r(_expit(_logit(val) + 1.96 * sd_u)),
                    "model_lo": _r(mid * 0.8), "model_mid": _r(mid), "model_hi": _r(mid * 1.2), "verified": sim,
                    "status": "matched"})
    targets, v_targets = {}, {}
    for i, g in enumerate(GROUPS):
        val, em, en, tid = 0.30 + 0.06 * i, (0.05, -0.08, 0.10)[i], (0.08, -0.12, 0.16)[i], f"grp_r1:{g}"
        sim = _at_error(val, en)
        targets[tid] = {"id": tid, "kind": "group", "observable": "jc1.m1", "value": _r(val), "scale": "logit",
                        "sd_obs": SD_OBS, "sd_bias": 0.0, "sd_md": SD_MD, "sd_total": sd_g, "source": _source("group"),
                        "n_units": 4, "implausibility_at_best": _r(abs(em) / sd_g),
                        "modelled_at_best": _r(_expit(_logit(val) + em)), "non_implausible_share": 1.0}
        v_targets[tid] = {"simulated": sim, "implausibility": _r(abs(en) / sd_g), "ok": True}
        mid = targets[tid]["modelled_at_best"]
        fit.append({"target": tid, "kind": "group", "unit": None, "observable": "jc1.m1", "observed": _r(val),
                    "obs_lo": _r(_expit(_logit(val) - 1.96 * sd_g)), "obs_hi": _r(_expit(_logit(val) + 1.96 * sd_g)),
                    "model_lo": _r(mid * 0.9), "model_mid": mid, "model_hi": _r(mid * 1.1), "verified": sim,
                    "status": "matched"})
    keys = [f"S2|{g}" for g in GROUPS]
    region, th = _region(), _thresholds()
    prior = {p: {"range": PRIOR[p], "dist": "uniform", "mode": BEST[p], "source": f"fixture prior for {p}"}
             for p in PARAMS}
    wave = {"wave": 1, "unit_runs": 48, "shared_runs": 16,
            "validation": {o: {"n": 6, "status": "ok", "share_outside": 0.0, "max_abs": 1.1, "mean": 0.02, "sd": 0.6,
                               "how": "held-out runs", "covers_all": True} for o in ("jc0.m1", "jc1.m1")},
            "candidates": region["candidates"], "accepted": region["accepted"],
            "share_of_prior": region["share_of_prior"], "relaxed": 0, "unmatched": [], "conflicting": [],
            "ranges": copy.deepcopy(RANGE)}
    names = list(PARAMS) + [f"u0:{u}" for u in UNITS]
    jc = {
        "method": "history_matching", "producer": "toolkit/calibration/history_matching.py",
        "figure_data": {"names": list(PARAMS), "prior": {p: {**prior[p], "point": BEST[p]} for p in PARAMS},
                        "cutoff": CUTOFF, "fit": fit,
                        "waves": [{k: wave[k] for k in ("wave", "share_of_prior", "accepted", "candidates", "ranges")}]},
        "status": "ok", "blocked": False, "blocks": [], "cutoff": CUTOFF, "shared": prior,
        "unit_parameter": {"name": "u0", "lo": UNIT_LO, "hi": UNIT_HI, "scale": "log"},
        "region": region, "best": {"theta": dict(BEST), "z": best_z, "score": 1.9},
        "units": units, "unmatched": [], "conflicting": [], "n_units": len(UNITS), "n_matched": len(UNITS),
        "targets": targets, "unit_targets": unit_targets,
        "verification": {"units": v_units, "targets": v_targets, "bias": {k: 0.0 for k in keys}},
        "bias": {k: {"draws": 64, "proposed": 64, "sd_prior": 0.15, "mean": _r(0.02 * (i - 1)), "sd": 0.14,
                     "q025": _r(0.02 * (i - 1) - 0.27), "q975": _r(0.02 * (i - 1) + 0.27), "units": 4,
                     "offset_verified": 0.0} for i, k in enumerate(keys)},
        "bias_components": [{"blocks": [k], "units": 4, "aggregates": [f"grp_r1:{k[3:]}"], "candidates": 2048,
                             "feasible": 2048, "empty_block": 0, "no_accepted": 0, "draws_max": 64,
                             "acceptance_in_region": 1.0} for k in keys],
        "survey_changes": {"status": "computed", "z_max": Z_MAX,
                           "statistic": "z = (modelled change - observed change) / sd on the targets' scale; sd from "
                                        "sampling, offsets and run noise, not discrepancy",
                           "pairs": [], "flagged": []},
        "weighting": {"best_point": "per_group", "bias_draws": 64}, "waves": [wave], "simulated_at_boundary": {},
        "emulators": {"unit": {o: {"random_effect_var": 0.4, "gp_var": 1.2, "length_scales": [1.0, 2.0, 3.0, 4.0],
                                   "run_noise_var": 0.01, "residual_var": 0.02, "nll": -12.5}
                               for o in ("jc0.m1", "jc1.m1")}, "shared": {}},
        "thresholds": th, "prior_runs": None,
        "adapter": {"windows": {"jc0": [2021.5, 2022.0], "jc1": [2018.5, 2019.0], "jc2": [2021.0, 2022.0]},
                    "dropped_targets": [], "units": len(UNITS), "shared": list(PARAMS), "simulator_calls": 64,
                    "stored_rows_reused": 0, "stored_rows_reused_legacy": 0, "theta_held": {}},
        "primary": {"targets": {"unit_r2": {"sd": 0.3, "bias_sd": 0.15}, "grp_r1": {}}, "discrepancy_sd": SD_MD,
                    "note": "the primary setting; declared sensitivities are reported beside it, never as a block"},
        "unmatched_decision": {"proceed": True, "mode": "flag", "flagged": [], "missed": [], "declared": [],
                               "why_not": None},
        "figures": {"figures": [], "not_produced": []},
        "sensitivity": [{"id": "sd_015", "label": "sd 0.15 on the round 2 unit target",
                         "targets": {"unit_r2": {"sd": 0.15}},
                         "report": {"method": "history_matching", "status": "ok", "blocked": False, "blocks": [],
                                    "cutoff": CUTOFF, "region": {**_region(), "accepted": 410, "share_of_prior": 0.14},
                                    "n_units": len(UNITS), "n_matched": len(UNITS)}}],
        "sample": {"sample_id": hashlib.sha256(b"fixture sample").hexdigest(), "names": names,
                   "rows": [[_r(RANGE[p][0] + (RANGE[p][1] - RANGE[p][0]) * ((k * 3 + j) % 8) / 8)
                             for j, p in enumerate(PARAMS)] + [best_z[u] for u in UNITS] for k in range(8)],
                   "full_sample_id": hashlib.sha256(b"fixture full sample").hexdigest(), "weighting": "per_group"}}
    _changes(jc)
    return jc


def _companion(jc):
    """The small analysis/calibration.yaml `data` a history-matching run writes beside the joint file."""
    return {"method": "emulator_inversion",
            "units": {u: {"z": jc["best"]["z"][u], "clamped": False, "slope": 0.7} for u in UNITS},
            "diagnostics": {"units": len(UNITS), "clamped_units": 0, "grid_points": 41, "range": [UNIT_LO, UNIT_HI],
                            "target_logit_residual_max": 0.21, "slope_median": 0.7, "slope_min": 0.4},
            "identifiability": {"design": "one value per unit", "flat_slope_threshold": 0.05, "units_unidentified": [],
                                "status": "identified"},
            "joint": {"artifact": JC, "shared": list(PARAMS), "best": dict(jc["best"]["theta"]),
                      "ranges": copy.deepcopy(jc["region"]["ranges"]), "sample_id": jc["sample"]["sample_id"]}}


def _posterior():
    """analysis/calibration.yaml `data` for the posterior method, clean."""
    fit = []
    for i, u in enumerate(UNITS):
        for src, lat, base in (("S1", "q1", 0.30), ("S2", "q2", 0.10)):
            share = base + 0.02 * i
            exp = share + 0.01 * _wobble(i) / 3
            fit.append({"unit": u, "groups": [_group(u)], "source": src, "latent": lat, "observed": _r(200 * share),
                        "n": 200.0, "noise": "binomial", "expected": _r(200 * exp), "expected_share": _r(exp),
                        "observed_share": _r(share), "variance": _r(200 * exp * (1 - exp))})
    width = {p: PRIOR[p][1] - PRIOR[p][0] for p in PARAMS}
    return {"method": "posterior",
            "diagnostics": {"converged": True, "message": "CONVERGENCE: relative reduction of the objective below tolerance",
                            "n_targets": len(fit), "n_parameters": len(PARAMS), "at_bound": [],
                            "observation": "per-source (calibration.sources)", "evaluation": "run_batch",
                            "loglik_at_mode": -41.2, "start_search_refusals": 0, "fit": fit,
                            "sampler": "adaptive_metropolis", "chains": 4, "warmup": 400, "draws_per_chain": 800,
                            "rhat_max": 1.004, "ess_bulk_min": 910.0, "acceptance": 0.24, "doublings": 0},
            "identifiability": {"method": "Fisher information of the likelihood at its maximum; contraction "
                                          "1 - var_post/var_prior on each prior's own scale",
                                "classes": {p: "identified" for p in PARAMS},
                                "likelihood_classes": {p: "identified" for p in PARAMS},
                                "contraction": {"p1": 0.95, "p2": 0.9, "p3": 0.2}, "ridges": [],
                                "status": "well_identified"},
            "theta": dict(BEST), "names": list(PARAMS),
            "sample": {"sample_id": hashlib.sha256(b"fixture posterior sample").hexdigest(), "names": list(PARAMS),
                       "rows": [[_r(BEST[p] + width[p] * 0.1 * (((k * (3 + j)) % 11) - 5) / 5)
                                 for j, p in enumerate(PARAMS)] for k in range(40)],
                       "method": "adaptive_metropolis", "seed": 0},
            "priors": {p: {"dist": "uniform", "source": f"fixture prior for {p}", "range": PRIOR[p]} for p in PARAMS},
            "prior_range_tests": [], "prior_dominated": ["p3"], "units": {u: {"z": None} for u in UNITS}}


def _estimates():
    """analysis/estimates.yaml `data` for the posterior method: the predictive band of a fit over time. A stored run
    writes this file for the `estimates` deliverable; the fixture carries it so the Fit view's `series` has a case."""
    steps = list(range(1, 9))
    med = [_r(20 + 15 * math.sin(k / 2.0)) for k in steps]
    return {"level": 0.95, "label": "95% interval over calibration and observation",
            "posterior_predictive": {"steps": steps, "observed": [_r(m + _wobble(k)) for k, m in zip(steps, med)],
                                     "median": med, "lo": [_r(m - 8) for m in med], "hi": [_r(m + 8) for m in med],
                                     "coverage": 1.0, "level": 0.95, "draws": 50}}


def _test(tid, ran=True):
    return {"test_id": tid, "path": f"models/decision_tests/{tid}.yaml" if ran else None,
            "status": "run" if ran else "not_run", "verdict": "NO_CHANGE" if ran else "NOT_RUN",
            "can_move_plan": True if ran else None, "threshold_pct": 10.0, "share_pct": 0.0 if ran else None,
            "regret_pct": 0.0 if ran else None,
            "statement": "This test can move the plan: it was re-solved for each value." if ran
            else "This test was not run: its values could not be reached."}


def _model(method):
    m = {"method": method, "spec": _spec(method), "unc": {"draws": 50, "seed": 1, "calibration_clamped": 0,
                                                          "prior_dominated": ["p3"]},
         "dt": {"tests": [_test("DT1")]}}
    if method == "history_matching":
        m["jc"] = _joint()
    else:
        m["cal"], m["est"] = _posterior(), _estimates()
    return m


# ------------------------------------------------------------------------------------------- the planted edits

def _flag(m):
    _move_change(m, "G2", -4.5)


def _near(m):
    _move_change(m, "G3", 2.5)


def _block(m, proceed=False):
    jc = m["jc"]
    rows = list(range(0, 36, 3))
    jc.update(status="blocked", blocked=True, blocks=[{"code": "offset_sampling_low_acceptance", "detail": {
        "rows": 40, "failed": len(rows), "failed_rows": rows, "by_component": [{"blocks": [OFFSET], "rows": rows}],
        "why": "region-sample rows whose offset and unit values never met the group targets; they were redrawn"}}])
    most = jc["thresholds"]["history_matching"]["bias_draws_max"]      # the block shows in the offset's own row too
    jc["bias"][OFFSET].update(draws=2, proposed=most, mean=-0.31, sd=0.02, q025=-0.33, q975=-0.29)
    next(c for c in jc["bias_components"] if OFFSET in c["blocks"]).update(
        no_accepted=len(rows), draws_max=most, acceptance_in_region=_r(1 - len(rows) / 40))
    jc["unmatched_decision"].update(proceed=proceed, why_not=None if proceed else
                                    "block offset_sampling_low_acceptance is not declared in the spec")


def _declare(m, ran=True):
    """A signed declaration covering offset S2|G2 that names decision test DT2, and DT2's result."""
    d = {"offset": OFFSET, "reason": "the round 2 offset for G2 is not reachable at sd 0.15; tested by DT2",
         "decision_test": "DT2"}
    m["spec"]["calibration"]["joint"]["unmatched"]["declared"].append(dict(d))
    m["spec"]["analysis"]["decision_tests"].append({"id": "DT2", "type": "calibration_offset", "offset": OFFSET,
                                                    "text": "Does the plan change when G2's offset is freed?"})
    m["jc"]["unmatched_decision"]["declared"].append(dict(d))
    m["dt"]["tests"].append(_test("DT2", ran))


def _blocked_declared(m, ran=True):
    _block(m, proceed=True)
    _declare(m, ran)


def _flag_declared(m):
    _flag(m)
    _declare(m)


def _set_range(m, p, lo, hi, best=None):
    reg = m["jc"]["region"]
    reg["ranges"][p] = [lo, hi]
    reg["contraction"][p] = _r(1 - (hi - lo) / (PRIOR[p][1] - PRIOR[p][0]))
    reg["interval95"][p]["region"] = [lo, hi]
    for w in (m["jc"]["waves"][-1], m["jc"]["figure_data"]["waves"][-1]):
        w["ranges"][p] = [lo, hi]
    if best is not None:
        m["jc"]["best"]["theta"][p] = best
        reg["interval95"][p]["point"] = best


def _tiny(m):
    reg = m["jc"]["region"]
    reg.update(accepted=40, share_of_prior=2e-05)
    for w in (m["jc"]["waves"][-1], m["jc"]["figure_data"]["waves"][-1]):
        w.update(accepted=40, share_of_prior=2e-05)
    _set_range(m, "p2", PRIOR["p2"][0], PRIOR["p2"][1], best=PRIOR["p2"][0])


def _on_bound(m):
    if m["method"] == "history_matching":
        _set_range(m, "p2", PRIOR["p2"][0], RANGE["p2"][1], best=PRIOR["p2"][0])
    else:
        cal = m["cal"]
        cal["theta"]["p2"] = PRIOR["p2"][0]
        cal["sample"]["rows"][0][1] = PRIOR["p2"][0]
        cal["diagnostics"]["at_bound"] = ["p2"]


def _bound_gap(share):
    def edit(m):
        lo, hi = PRIOR["p3"]
        _set_range(m, "p3", RANGE["p3"][0], hi - share * (hi - lo))
    return edit


def _assumed(m):
    m["jc"]["unit_targets"]["U07"][0]["source"] = _source("unit", assumed=True)


def _dominated(m):
    m["unc"]["prior_dominated"] = ["p1", "p3"]


def _emulator_gap(m):
    _group_error(m["jc"], "G3", emulator=1.9 * m["jc"]["targets"]["grp_r1:G3"]["sd_total"])


def _unit_bounds(m):
    for u in UNITS[:3]:
        m["jc"]["units"][u]["region_touches_bound"] = ["hi"]
        m["jc"]["units"][u]["z_interval"][1] = UNIT_HI


def _one_run(m):
    for v in m["jc"]["verification"]["units"].values():
        v["runs"] = 1


def _not_converged(m):
    m["cal"]["diagnostics"].update(converged=False, message="ABNORMAL: iteration limit reached", rhat_max=1.41)


def _unit_score(m, u, engine, emulator):
    """Unit u's one target: the engine's verification moves to implausibility `engine` (model above the observed
    value, simulated value to match) and the emulator's implausibility at the best point to `emulator`."""
    _unit_error(m["jc"], u, engine * m["jc"]["unit_targets"][u][0]["sd_total"], emulator)
    _changes(m["jc"])


def _near_cutoff(m):
    _unit_score(m, "U03", 2.6, 2.3)


def _unit_gap(m):
    _unit_score(m, "U03", 1.9, 0.3)


def _one_side(m):
    if m["method"] == "history_matching":
        jc = m["jc"]
        _, before = _errors(jc, "G2")
        for u in UNITS[4:8]:
            _unit_score(m, u, 1.6, 1.4)
        units, _ = _errors(jc, "G2")       # the group's earlier-round target moves as far, so the change is as it was
        up = before + sum(units.values()) / len(units)
        _group_error(jc, "G2", err=up, emulator=0.9 * up)
        _changes(jc)
        return
    for f in m["cal"]["diagnostics"]["fit"]:
        if f["groups"] == ["G2"]:
            exp = f["observed"] + 1.5 * math.sqrt(f["variance"])
            f.update(expected=_r(exp), expected_share=_r(exp / f["n"]), variance=_r(exp * (1 - exp / f["n"])))


def _second_measure(m):
    m["jc"]["region"]["interval95"]["p3"]["contraction95"] = 0.8


def _diagnostic(key, value):
    def edit(m):
        m["cal"]["diagnostics"][key] = value
    return edit


def _no_series(m):
    del m["est"]["posterior_predictive"]


HM, BOTH, POST = ("history_matching",), METHODS, ("posterior",)
SIGNED = SPEC_FILES + [JC, DT]
UNIT_FILES = [JC, "figures/calibration_fit.csv", "figures/calibration_residuals.csv", "figures/calibration_unit_fit.csv"]
# Defect name -> case (scorecard, or None), check (the one Fit check that moves), status (what it moves to),
# edit (exactly what is changed, in one sentence), files (the files the edit itself touches, per method; files that
# record a hash of those follow), methods, and the function that applies it. `status: ok` marks a variant kept to
# show a rule's edge: nothing may be raised on it. `also` lists any other check the same edit must move, by rule.
DEFECTS = {
    "C2_change_flagged": {
        "case": "C2", "check": "changes", "status": "problem", "methods": HM, "apply": _flag, "files": UNIT_FILES,
        "edit": "The engine's values for group G2 are moved (its four later-round unit targets down, its earlier-round "
                "group target up, each still well inside the cutoff) so that the group's change between rounds, "
                "recomputed from those rows in survey_changes.pairs, has z -4.5 against the limit z_max 3.0, with "
                "flagged true and its id listed in survey_changes.flagged; nothing declares it."},
    "C2_change_flagged_declared": {
        "case": "C2", "check": "changes", "status": "look", "methods": HM, "apply": _flag_declared,
        "files": SIGNED + UNIT_FILES[1:],
        "edit": "The same flagged G2 pair (z -4.5, from the same moved target rows), covered by a declaration for offset S2|G2 in the signed spec's "
                "calibration.joint.unmatched.declared and in unmatched_decision.declared that names decision test "
                "DT2, which ran with verdict NO_CHANGE."},
    "C2_change_near_miss": {
        "case": "C2", "check": "changes", "status": "look", "methods": HM, "apply": _near, "files": UNIT_FILES,
        "edit": "The engine's values for group G3 are moved (unit targets up, group target down) so that its change in "
                "survey_changes.pairs, recomputed from those rows, has z 2.5, inside the limit z_max 3.0 but within 25% "
                "of it, with flagged left false."},
    "C13_blocked_undeclared": {
        "case": "C13", "check": "status", "status": "problem", "methods": HM, "apply": _block, "files": [JC],
        "edit": "status becomes blocked with one block, offset_sampling_low_acceptance on offset S2|G2, that no "
                "declaration covers; that offset's row in bias has 2 of 1,024 draws accepted and its row in "
                "bias_components counts the failed rows; unmatched_decision.proceed is false."},
    "C13_blocked_declared": {
        "case": "C13", "check": "status", "status": "look", "methods": HM, "apply": _blocked_declared, "files": SIGNED,
        "edit": "The same block on offset S2|G2 with unmatched_decision.proceed true, covered by a declaration in "
                "the signed spec and in unmatched_decision.declared that names decision test DT2, which ran with "
                "verdict NO_CHANGE."},
    "C13_blocked_test_not_run": {
        "case": "C13", "check": "status", "status": "problem", "methods": HM, "files": SIGNED,
        "apply": lambda m: _blocked_declared(m, ran=False),
        "edit": "The same block and declaration naming DT2, but analysis/decision_tests.yaml records DT2 with "
                "status not_run and verdict NOT_RUN."},
    "C13_tiny_region": {
        "case": "C13", "check": "region", "status": "look", "methods": HM, "apply": _tiny,
        "files": [JC, "figures/calibration_region.csv", "figures/calibration_waves.csv"],
        "edit": "region.accepted drops from 600 to 40 of 2,048 candidates (below region_min_candidates 64; "
                "share_of_prior 0.00002), p2's accepted range equals its prior range (contraction 0) and "
                "best.theta.p2 sits on the prior's lower bound."},
    "C13_best_on_bound": {
        "case": "C13", "check": "region", "status": "look", "methods": BOTH, "apply": _on_bound,
        "files": {"history_matching": [JC, "figures/calibration_region.csv"], "posterior": [CAL]},
        "edit": "p2's best value is set to its prior's lower bound 1.0 (history matching: region.ranges.p2[0] and "
                "best.theta.p2; posterior: theta.p2, one sample row and diagnostics.at_bound)."},
    "bound_within_tolerance": {
        "case": "C13", "check": "region", "status": "look", "methods": HM, "apply": _bound_gap(5e-07),
        "files": [JC, "figures/calibration_region.csv"],
        "edit": "p3's accepted upper end is moved to 0.5e-6 of the prior range's width below the prior's upper "
                "bound, which counts as reached."},
    "bound_outside_tolerance": {
        "case": "C13", "check": "region", "status": "ok", "methods": HM, "apply": _bound_gap(2e-06),
        "files": [JC, "figures/calibration_region.csv"],
        "edit": "p3's accepted upper end is moved to 2e-6 of the prior range's width below the prior's upper "
                "bound, which does not count as reached."},
    "C3_assumed_sd": {
        "case": "C3", "check": "target_error", "status": "look", "methods": HM, "apply": _assumed, "files": [JC],
        "edit": "The source text of unit target unit_r2:U07 labels its sd 0.3 an ASSUMPTION."},
    "C14_prior_dominated": {
        "case": "C14", "check": "constrained", "status": "look", "methods": BOTH, "apply": _dominated, "files": [UNC],
        "edit": "analysis/uncertainty.yaml data.prior_dominated gains p1, whose range contracted by more than half."},
    "emulator_vs_engine": {
        "case": None, "check": "emulator_vs_engine", "status": "look", "methods": HM, "apply": _emulator_gap,
        "files": [JC, "figures/calibration_unit_fit.csv"],
        "edit": "The emulator's value at the best point for group target grp_r1:G3 moves to implausibility 1.9 "
                "(modelled_at_best to match) against the engine's 0.27, a difference above 1 with both under the "
                "cutoff 3.0; the engine's values, and so the change between rounds, are as on the clean run."},
    "unit_bounds": {
        "case": None, "check": "unit_bounds", "status": "look", "methods": HM, "apply": _unit_bounds,
        "files": [JC, "figures/calibration_bound_share.csv"],
        "edit": "Units U01, U02 and U03 get region_touches_bound ['hi'] (interval end 6.0), so 4 of 12 unit "
                "intervals reach the unit parameter's bound instead of 1."},
    "replicates": {
        "case": None, "check": "replicates", "status": "look", "methods": HM, "apply": _one_run, "files": [JC],
        "edit": "verification.units[u].runs is 1 for every unit instead of 4."},
    "C13_not_converged": {
        "case": "C13", "check": "status", "status": "problem", "methods": POST, "apply": _not_converged,
        "files": [CAL], "also": {"convergence": "problem"},
        "edit": "diagnostics.converged is false (message ABNORMAL: iteration limit reached; rhat_max 1.41)."},
    # Added with the checks the spec gained after the first acceptance pass (section 3a).
    "near_cutoff": {
        "case": None, "check": "near_cutoff", "status": "look", "methods": HM, "apply": _near_cutoff, "files": UNIT_FILES,
        "edit": "The engine's verification of unit target unit_r2:U03 moves to implausibility 2.6 (emulator 2.3), "
                "under the cutoff 3.0 and within 25% of it, with the unit still matched."},
    "group_bias": {
        "case": None, "check": "group_bias", "status": "look", "methods": BOTH, "apply": _one_side,
        "files": {"history_matching": UNIT_FILES, "posterior": [CAL, "figures/calibration_fit.csv"]},
        "edit": "Every unit target of group G2 gets a model value above its observed value (history matching: engine "
                "implausibility 1.6, emulator 1.4, and the group's earlier-round target moved up as far, so its change "
                "between rounds is as on the clean run; posterior: expected 1.5 sd above observed), so the group's "
                "mean signed score is beyond 1 while no single target nears a cutoff."},
    "emulator_vs_engine_unit": {
        "case": None, "check": "emulator_vs_engine", "status": "look", "methods": HM, "apply": _unit_gap,
        "files": UNIT_FILES,
        "edit": "The engine's verification of unit target unit_r2:U03 moves to implausibility 1.9 against the "
                "emulator's 0.3, a difference above 1 on a unit target, with every group target unchanged."},
    "C14_second_measure": {
        "case": "C14", "check": "constrained", "status": "look", "methods": HM, "apply": _second_measure, "files": [JC],
        "edit": "region.interval95.p3.contraction95 becomes 0.8 while region.contraction.p3 stays 0.2, for a "
                "parameter that analysis/uncertainty.yaml lists as prior-dominated."},
    "convergence_rhat": {
        "case": None, "check": "convergence", "status": "look", "methods": POST, "files": [CAL],
        "apply": _diagnostic("rhat_max", 1.03),
        "edit": "diagnostics.rhat_max becomes 1.03 (above 1.01) with diagnostics.converged still true."},
    "convergence_ess": {
        "case": None, "check": "convergence", "status": "look", "methods": POST, "files": [CAL],
        "apply": _diagnostic("ess_bulk_min", 250.0),
        "edit": "diagnostics.ess_bulk_min becomes 250 (below 400) with diagnostics.converged still true."},
    "series_not_recorded": {
        "case": None, "check": "status", "status": "ok", "methods": POST, "apply": _no_series, "files": [EST],
        "edit": "analysis/estimates.yaml loses data.posterior_predictive, so the method records no fit over time."},
}


def touched(defect, method):
    """The files a defect's edit itself changes for a method."""
    f = DEFECTS[defect]["files"]
    return list(f[method] if isinstance(f, dict) else f)


# ------------------------------------------------------------------------------------------- writing the run

def _yaml(doc):
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000)


def _csv(header, rows):
    return "\n".join(",".join("" if v is None else str(v) for v in r) for r in [header, *rows]) + "\n"


def _head_commit():
    try:
        r = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and len(r.stdout.strip()) == 40:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "0" * 40          # no git: Run.commit_known cannot ask either, and the Steps check stays ok


def _figures(m):
    if m["method"] == "posterior":
        f = m["cal"]["diagnostics"]["fit"]
        return {"figures/calibration_fit.csv": _csv(list(f[0]), [list(r.values()) for r in f])}
    jc = m["jc"]
    fit, reg = jc["figure_data"]["fit"], jc["region"]
    unit_rows = [r for r in fit if r["kind"] == "unit"]
    return {
        "figures/calibration_fit.csv": _csv(
            ["unit", "region", "target", "modelled", "residual_logit"],
            [[r["unit"], _group(r["unit"]), r["observed"], r["verified"],
              _r(_logit(r["verified"]) - _logit(r["observed"]))] for r in unit_rows]),
        "figures/calibration_residuals.csv": _csv(
            ["target", "observed", "modelled", "residual", "status"],
            [[r["target"], r["observed"], r["verified"], _r(r["observed"] - r["verified"]), r["status"]]
             for r in unit_rows]),
        "figures/calibration_unit_fit.csv": _csv(list(fit[0]), [list(r.values()) for r in fit]),
        "figures/calibration_bound_share.csv": _csv(
            ["group", "units", "best_at_top", "interval_reaches_top"],
            [[g, 4, 0.0, sum("hi" in jc["units"][u]["region_touches_bound"] for u in UNITS if _group(u) == g) / 4]
             for g in GROUPS]),
        "figures/calibration_region.csv": _csv(
            ["panel", "region_min", "region_max", "prior_lo", "prior_hi", "abs_spearman"],
            [[p, *reg["ranges"][p], *reg["prior_ranges"][p], None] for p in PARAMS]),
        "figures/calibration_waves.csv": _csv(
            ["wave", "share_of_prior", "accepted", "candidates"],
            [[w["wave"], w["share_of_prior"], w["accepted"], w["candidates"]] for w in jc["waves"]])}


def _render(m, stage):
    """Every file of the run as {path: text}, for a run that has reached `stage`."""
    method, files = m["method"], {}

    def sha(rel):
        return hashlib.sha256(files[rel].encode()).hexdigest()

    def artifact(rel, kind, producer, inputs, data, minute):
        files[rel] = _yaml({"schema": f"analysis.{kind}/1", "produced_by": "harness", "producer": producer,
                            "spec_sha256": sha("decision_spec.yaml"), "spec_signed": True, "engine": ENGINE,
                            "engine_choice_sha256": None, "inputs": {**{i: sha(i) for i in inputs}, **HARNESS_INPUT},
                            "written_at": _at(minute), "data": data})

    files["decision_spec.yaml"] = _yaml(m["spec"])
    cols = ["unit_id", GROUPING, "name", "pop", "target_r2", "x1"]
    files["data/derived/units.csv"] = _csv(cols, [[u, _group(u), f"unit {u}", 1000 + 150 * i, _r(0.10 + 0.02 * i),
                                                   _r(0.5 + 0.03 * _wobble(i))] for i, u in enumerate(UNITS)])
    if method == "history_matching":
        files["data/derived/targets.csv"] = _csv(
            [GROUPING, "level", "round_id", "value_pct", "ci_low_pct", "ci_high_pct"],
            [[g, "group", "S1", 30 + 6 * i, 22 + 6 * i, 39 + 6 * i] for i, g in enumerate(GROUPS)])
    else:
        files["data/derived/targets.csv"] = _csv(
            ["unit", GROUPING, "source", "latent", "observed", "n"],
            [[f["unit"], f["groups"][0], f["source"], f["latent"], int(f["observed"]), int(f["n"])]
             for f in m["cal"]["diagnostics"]["fit"]])
    files["decision_brief.signed/decision_spec.yaml"] = files["decision_spec.yaml"]
    files["decision_spec.signoff.json"] = json.dumps(
        {"signed_by": "owner", "signed_at": _at(50), "note": "fixture sign-off",
         "sha256": {"decision_spec.yaml": sha("decision_spec.yaml")}}, indent=1)
    data_in = ["decision_spec.yaml", "data/derived/units.csv", "data/derived/targets.csv"]
    if method == "history_matching":
        artifact(JC, "joint_calibration", "toolkit/analysis/joint_calibration.py", data_in, m["jc"], 100)
        artifact(CAL, "calibration", "toolkit/analysis/calibration.py", ["data/derived/units.csv", JC],
                 _companion(m["jc"]), 110)
    else:
        artifact(CAL, "calibration", "toolkit/analysis/calibration.py", data_in[1:], m["cal"], 110)
    files.update(_figures(m))
    artifact("analysis/point.yaml", "point", "toolkit/analysis/point.py", [CAL],
             {"n_units": len(UNITS), "mix": {"A": 7, "A+B": 5}}, 150)
    artifact(UNC, "uncertainty", "toolkit/analysis/uncertainty.py", ["analysis/point.yaml"], m["unc"], 200)
    artifact(DT, "decision_tests", "toolkit/analysis/decision_tests.py", ["analysis/point.yaml", UNC], m["dt"], 230)
    if "est" in m:
        artifact(EST, "estimates", "toolkit/analysis/estimates.py", [CAL], m["est"], 232)
    files["allocation.csv"] = _csv(["unit_id", "package", "outcome_averted"],
                                   [[u, "A" if i % 12 < 7 else "A+B", 40 + 3 * i] for i, u in enumerate(UNITS)])
    arts = [r for r in files if r.startswith("analysis/")]
    files["analysis/manifest.json"] = json.dumps({
        "artifacts": {r: {"kind": r[9:-5], "producer": f"toolkit/analysis/{r[9:-5]}.py", "sha256": sha(r)} for r in arts},
        "complete": True,
        "deliverables": {"allocation.csv": {"producer": "toolkit/analysis/run.py", "role": "allocation",
                                            "sha256": sha("allocation.csv")}},
        "engine": ENGINE, "engine_choice_sha256": None,
        "expected": {"decision_tests": {t["test_id"]: t["path"] for t in m["dt"]["tests"]},
                     "kinds": [r[9:-5] for r in arts]},
        "finished_at": _at(235), "produced_by": "harness", "reason": "", "settings_sha256": None,
        "spec_sha256": sha("decision_spec.yaml"), "spec_signed": True, "status": "complete", "wall_s": 8100.0}, indent=1)

    final, commit = stage == "final", _head_commit()
    hist = HISTORY[:REACHED[stage]]
    files["metadata.json"] = json.dumps({
        "question": m["spec"]["question"], "started": _at(0), "pipeline": "v2", "max_rounds": 2, "signoff": "owner",
        "commit": commit, "launcher": "fixture",
        "invocations": [{"pipeline": "v2", "commit": commit, "at": _at(0), "launcher": "fixture"}],
        "engine": "toolkit.engines.toy.engine:ToyEngine",
        **({"status": "completed", "rounds": 1, "model_spend_usd": hist[-1][3], "finished": _at(305)} if final else {})},
        indent=1)
    files["run_state.json"] = json.dumps({
        "question": m["spec"]["question"], "state": hist[-1][1], "round": 1, "max_rounds": 2, "signoff": "owner",
        "engine": ENGINE["name"], "budget_usd": 30.0, "spent_usd": hist[-1][3],
        "status": "completed" if final else None, "paused": "awaiting_signoff" if stage == "launch" else None,
        "journal": {"SPEC#0:try1": {"task": "draft_spec", "status": "completed", "started": _at(1), "ok": True,
                                    "allowance_usd": 3.0, "usd": 1.0, "completed": _at(30),
                                    "output": {"ok": True, "notes": "drafted the decision spec"}}},
        "history": [{"at": _at(t), "from": a, "to": b, "why": "ok", "round": 1, "spent_usd": usd}
                    for a, b, t, usd in hist]}, indent=1)
    keep = STAGES[:STAGES.index(stage) + 1]
    return {r: t for r, t in files.items() if STAGE_OF["figures/" if r.startswith("figures/") else r] in keep}


def build(dir, upto=None, defect=None, method="history_matching"):
    """Write one fixture run into `dir` (created if missing). Returns {path: sha256} of the files written."""
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; one of {', '.join(METHODS)}")
    stage = "final" if upto is None else upto
    if stage not in STAGES:
        raise ValueError(f"unknown stage {upto!r}; one of {', '.join(STAGES)}")
    m = _model(method)
    if defect is not None:
        if defect not in DEFECTS:
            raise ValueError(f"unknown defect {defect!r}; see --list")
        if method not in DEFECTS[defect]["methods"]:
            raise ValueError(f"defect {defect} is not defined for method {method}")
        DEFECTS[defect]["apply"](m)
    out = {}
    for rel, text in _render(m, stage).items():
        path = os.path.join(dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        out[rel] = hashlib.sha256(text.encode()).hexdigest()
    return out


# ------------------------------------------------------------------------------------------- self-test

def domain_words():
    with open(os.path.join(HERE, "domain_words.txt"), encoding="utf-8") as f:
        return [w.strip().lower() for w in f if w.strip() and not w.startswith("#")]


def _headers(dir, shas):
    """Each analysis file's header, parsed from the file on disk: {path: (spec_sha256, {input: sha})}."""
    out = {}
    for rel in shas:
        if rel.startswith("analysis/") and rel.endswith(".yaml"):
            with open(os.path.join(dir, rel), encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            out[rel] = (doc["spec_sha256"], doc["inputs"], doc)
    return out


def _follows(clean_dir, clean, first):
    """`first` plus every file that records a hash of a changed file, found from the clean run's own headers."""
    changed, heads = set(first), _headers(clean_dir, clean)
    while True:
        more = {r for r, (_, inputs, _) in heads.items()
                if "decision_spec.yaml" in changed or any(i in changed for i in inputs)}
        if "decision_spec.yaml" in changed:
            more.add("decision_spec.signoff.json")
        if any(r.startswith("analysis/") or r == "allocation.csv" for r in changed | more):
            more.add("analysis/manifest.json")
        if more <= changed:
            return changed
        changed |= more


def _underived(dir):
    """What cannot be recomputed in a built history-matching run's change pairs from its own target rows (nothing
    when every pair follows): the change is the mean of the group's later-round unit rows minus its earlier-round
    group row, observed and modelled, and z is their difference on the targets' scale over the pair's sd."""
    with open(os.path.join(dir, JC), encoding="utf-8") as f:
        jc = yaml.safe_load(f)["data"]
    with open(os.path.join(dir, "data/derived/units.csv"), encoding="utf-8") as f:
        head, *rows = [line.strip().split(",") for line in f]
    group_of = {r[0]: r[head.index(GROUPING)] for r in rows}
    out, sc = [], jc["survey_changes"]
    for p in sc["pairs"]:
        us = [u for u in jc["unit_targets"] if group_of[u] == p["group"]]
        obs = [jc["unit_targets"][u][0]["value"] for u in us]
        mod = [jc["verification"]["units"][u]["targets"][0]["simulated"] for u in us]
        tid = f"{p['from']}:{p['group']}"
        obs_g, mod_g = jc["targets"][tid]["value"], jc["verification"]["targets"][tid]["simulated"]
        want = {"observed_natural": sum(obs) / len(obs) - obs_g, "modelled_natural": sum(mod) / len(mod) - mod_g,
                "observed": sum(map(_logit, obs)) / len(obs) - _logit(obs_g),
                "modelled": sum(map(_logit, mod)) / len(mod) - _logit(mod_g)}
        out += [f"{p['group']} {k}: {p[k]} in the pair, {_r(w)} from the target rows" for k, w in want.items()
                if abs(p[k] - w) > 1e-5]
        if abs(p["sd"] - math.sqrt(sum(v * v for v in p["sd_parts"].values()))) > 1e-5:
            out.append(f"{p['group']} sd {p['sd']} is not the root sum of squares of its parts")
        if abs(p["z"] - (p["modelled"] - p["observed"]) / p["sd"]) > 1e-4 or p["flagged"] != (abs(p["z"]) > sc["z_max"]):
            out.append(f"{p['group']} z {p['z']} (flagged {p['flagged']}) does not follow from its change and sd")
    if sorted(sc["flagged"]) != sorted(p["id"] for p in sc["pairs"] if p["flagged"]):
        out.append("survey_changes.flagged does not list the flagged pairs")
    return out


def _self_test():
    fails = []

    def ok(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        clean = {}
        for method in METHODS:
            d = os.path.join(tmp, "clean", method)
            clean[method] = build(d, method=method)
            cal = JC if method == "history_matching" else CAL
            with open(os.path.join(d, cal), encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            want = {"run_state.json", "metadata.json", "decision_spec.yaml", "decision_spec.signoff.json",
                    "decision_brief.signed/decision_spec.yaml", "data/derived/units.csv", "analysis/manifest.json",
                    UNC, DT, cal, "figures/calibration_fit.csv"}
            ok(f"X1 {method}: the clean run builds with every file the Steps and Fit views read",
               want <= set(clean[method]) and doc["data"]["method"] == method, str(sorted(want - set(clean[method]))))
            again = build(os.path.join(tmp, "again", method), method=method)
            ok(f"X2 {method}: two builds of the clean run are identical", again == clean[method])

            heads = _headers(d, clean[method])
            spec_sha = clean[method]["decision_spec.yaml"]
            stale = [(r, i) for r, (_, inputs, _) in heads.items() for i, s in inputs.items()
                     if not i.startswith("..") and clean[method].get(i) != s]
            ok(f"X3 {method}: every input hash an analysis header records matches the file written",
               not stale and all(len(inputs) > 1 for _, inputs, _ in heads.values()), str(stale))
            with open(os.path.join(d, "analysis/manifest.json")) as f:
                man = json.load(f)
            with open(os.path.join(d, "decision_spec.signoff.json")) as f:
                so = json.load(f)
            listed = {**man["artifacts"], **man["deliverables"]}
            ok(f"X4 {method}: headers, sign-off and manifest all record the spec and files on disk",
               all(s == spec_sha for s, _, _ in heads.values()) and so["sha256"] == {"decision_spec.yaml": spec_sha}
               and clean[method]["decision_brief.signed/decision_spec.yaml"] == spec_sha
               and set(man["artifacts"]) == set(heads) and all(clean[method][r] == v["sha256"] for r, v in listed.items()))
            with open(os.path.join(d, "run_state.json")) as f:
                state = json.load(f)
            moves = [(h["from"], h["to"]) for h in state["history"]]
            ok(f"X5 {method}: the run finished, with no repeated transition and no failed task",
               state["state"] == "FINAL" and state["status"] == "completed" and len(set(moves)) == len(moves)
               and all(j["ok"] and j["usd"] < 0.9 * j["allowance_usd"] for j in state["journal"].values()))
            with open(os.path.join(d, "data/derived/units.csv")) as f:
                rows = [line.strip().split(",") for line in f]
            ok(f"X6 {method}: twelve units in three groups under the grouping column",
               rows[0][1] == GROUPING and len(rows) == 13 and sorted({r[1] for r in rows[1:]}) == list(GROUPS))

            for stage in STAGES:
                got = build(os.path.join(tmp, "upto", method, stage), upto=stage, method=method)
                later = {r for r in clean[method]
                         if STAGES.index(STAGE_OF["figures/" if r.startswith("figures/") else r]) > STAGES.index(stage)}
                ok(f"X7 {method}: upto {stage} leaves out the {len(later)} file(s) later stages write",
                   set(got) == set(clean[method]) - later, str(sorted(set(got) ^ (set(clean[method]) - later))))

            for name, spec in DEFECTS.items():
                if method not in spec["methods"]:
                    continue
                got = build(os.path.join(tmp, "defect", method, name), defect=name, method=method)
                diff = {r for r in set(got) | set(clean[method]) if got.get(r) != clean[method].get(r)}
                expect = _follows(d, clean[method], touched(name, method))
                ok(f"X8 {method}: {name} changes the files its edit names, the files that hash them, and no others",
                   diff == expect, f"unexpected {sorted(diff - expect)}, missing {sorted(expect - diff)}")

        jc_doc = _headers(os.path.join(tmp, "clean", "history_matching"), {JC: 1})[JC][2]["data"]
        zs = [abs(p["z"]) for p in jc_doc["survey_changes"]["pairs"]]
        assert len(zs) == len(GROUPS)
        gaps = [abs(jc_doc["verification"]["targets"][t]["implausibility"] - v["implausibility_at_best"])
                for t, v in jc_doc["targets"].items()]
        at_bound = sum(bool(u["region_touches_bound"]) for u in jc_doc["units"].values())
        ok("X9 the clean history-matching run sits clear of every threshold and its near-miss band",
           max(zs) < 0.75 * Z_MAX and max(gaps) < 0.75 and at_bound / len(UNITS) < 0.75 * 0.25
           and jc_doc["status"] == "ok" and not jc_doc["blocks"]
           and all(v["runs"] > 1 for v in jc_doc["verification"]["units"].values())
           and all(PRIOR[p][0] < lo and hi < PRIOR[p][1] for p, (lo, hi) in jc_doc["region"]["ranges"].items()))
        ok("X10 every defect entry names its case, check, status, methods and one-sentence edit",
           all(s["check"] and s["status"] in ("ok", "look", "problem") and s["methods"] and s["edit"].endswith(".")
               and s["edit"].count(". ") == 0 and "case" in s for s in DEFECTS.values()),
           str([n for n, s in DEFECTS.items() if s["edit"].count(". ")]))
        for name in [None] + [n for n, spec in DEFECTS.items() if "history_matching" in spec["methods"]]:
            d = os.path.join(tmp, "defect", "history_matching", name) if name else os.path.join(tmp, "clean", "history_matching")
            ok(f"X13 {name or 'the clean run'}: each change between rounds can be recomputed from the run's own target rows",
               not _underived(d), str(_underived(d)[:3]))
        for name in ("C13_blocked_undeclared", "C13_blocked_declared", "C13_blocked_test_not_run"):
            jc = _headers(os.path.join(tmp, "defect", "history_matching", name), {JC: 1})[JC][2]["data"]
            share = {k: b["draws"] / b["proposed"] for k, b in jc["bias"].items()}
            comp = next(c for c in jc["bias_components"] if OFFSET in c["blocks"])
            ok(f"X14 {name}: the block on offset {OFFSET} shows in that offset's own row (few draws accepted), not only in the block list",
               share[OFFSET] < 0.01 and all(v == 1.0 for k, v in share.items() if k != OFFSET)
               and comp["no_accepted"] == jc["blocks"][0]["detail"]["failed"] and comp["acceptance_in_region"] < 1.0,
               str((share, comp)))
        for bad in ({"method": "unknown"}, {"defect": "unknown"}, {"upto": "unknown"},
                    {"defect": "C2_change_flagged", "method": "posterior"}):
            try:
                build(os.path.join(tmp, "bad"), **bad)
                refused = False
            except ValueError:
                refused = True
            ok(f"X11 build refuses {bad}", refused)

        words, hits = domain_words(), []
        for root, _, names in os.walk(tmp):
            for n in names:
                with open(os.path.join(root, n), encoding="utf-8") as f:
                    text = (os.path.relpath(os.path.join(root, n), tmp) + "\n" + f.read()).lower()
                hits += [(n, w) for w in words if w in text]
        ok(f"X12 no built fixture file holds a domain word ({len(words)} words)", len(words) > 50 and not hits,
           str(sorted(set(hits))[:8]))
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--method", default="history_matching", choices=METHODS)
    ap.add_argument("--defect", choices=sorted(DEFECTS))
    ap.add_argument("--upto", choices=STAGES)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.list:
        for name, s in DEFECTS.items():
            print(f"{name}\t{s['case'] or '-'}\t{s['check']} -> {s['status']}\t{', '.join(s['methods'])}\t{s['edit']}")
        return 0
    if not a.dir:
        ap.error("a directory to write the run into is needed")
    print(f"{len(build(a.dir, upto=a.upto, defect=a.defect, method=a.method))} files written to {a.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
