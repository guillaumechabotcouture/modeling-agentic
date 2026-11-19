#!/usr/bin/env python3
"""Fixture runs for the review workbench (docs/design/review-workbench.md §9, §3a).

    python3 tools/workbench/fixtures.py <dir> [--method M] [--figures SET] [--defect NAME] [--upto STAGE]   # write one run
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

It also holds every file the science register (§5.8, §6) is derived from, shaped as the producers write them
(agents/findings_ledger.py for findings.jsonl; a stored run for the rest): findings.jsonl (critics' findings and one
raised by a harness check), engine_choice.json, the signed spec's uncertainty.excluded, decision_spec.signoff_sheet.md
(no row marked HIGH on the clean run; defect sheet_high marks one), evidence_dossier.yaml with no-data entries, and
decision tests of each verdict. On the clean run every science row has a disposition, the one plan-changing test is
linked to a row, and the signed spec holds no comment line that starts a departure, limitation or assumption (such a
line is itself something the register raises, so it is planted: defect prose_only).

The clean run holds nothing declared (§9): no engine departure, no declared calibration miss, no scope declaration
and no finding with the status declared, and no two of its files disagree. Every declared case is a variant: defect
`declared` is the run with one of each (a signed departure for benchmark B2, which is then out of band, and one for
component B; a declared calibration miss for history matching; scope_declaration.yaml declaring finding R-001), each
naming a decision test that ran; the edits that change a declaration are made on top of it.

It also holds every file the Model view (§5.4) and the Figures view (§5.12) read, shaped as the producers write them:
- analysis/engine.yaml (toolkit/analysis/run.py): the card with four benchmarks, each in band at the centre of its band
  on the log scale (status words of toolkit/engines/benchmark.py), the engine's parameters and the one value the spec
  sets; engine_choice.json with three candidates (agents/analysis_backend.py), the chosen one reporting B4 without
  letting it gate. The signed spec says `engine: auto`, so every analysis header holds the hash of the engine choice,
  as the harness writes it only then. The clean run has no departure: an engine choice records one only for a
  benchmark that is not in band or a component with no benchmark (toolkit/engines/benchmark_coverage.py gate()). Defect
  C4_out_of_band_departure puts B2 out of band and adds the signed departure that covers it, to the signed spec and
  to the choice record with the benchmark's own status.
- analysis/figures.yaml in the shape of either producer (`figures="standard"`: toolkit/analysis/figures.py, a subset of
  its figure set under its own names, in its three tiers; `figures="estimates"`: toolkit/analysis/estimates.py, for the
  posterior method), every figure produced, each with a one-pixel PNG, a vector copy, its table and its provenance
  record (scripts/figure_validator.py); the manifest lists each image; report.md names each decision figure by path or
  file name, manuscript.md one more, and report_render.json holds the report's hash.
The clean run holds no analysis/benchmarks_at_spec_values.yaml: that file has no artifact header and sits outside the
manifest (gap H13), which the Steps view raises; variant spec_benchmarks_recomputed adds it with its record file.

Names are neutral on purpose (§3a): units U01.., groups G1.., parameters p1.., components A and B, and a grouping
column called `stratum` so that a view which hard-codes a column name fails.

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
import struct
import subprocess
import sys
import tempfile
import zlib

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
_SCRIPTS = os.path.join(REPO, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from lib.safe_io import write_nofollow  # noqa: E402

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
# What the science register reads beside the spec and the decision tests (§6).
LEDGER, SCOPE, CHOICE, SHEET, DOSSIER = ("findings.jsonl", "scope_declaration.yaml", "engine_choice.json",
                                         "decision_spec.signoff_sheet.md", "evidence_dossier.yaml")
OLD_DECLARED, OLD_GATE = "declared_findings.yaml", "final_gate.json"       # where an older run keeps dispositions
GATES = ("gate_r1.json", "gate_r2.json")               # agents/stage_machine.py: one gate record per round
# What the Model view (§5.4) and the Figures view (§5.12) read beside those.
ENGINE_REC, FIGS = "analysis/engine.yaml", "analysis/figures.yaml"
BSV, BSV_REC = "analysis/benchmarks_at_spec_values.yaml", "analysis/benchmarks_at_spec_values.record.json"
REPORT, MANUSCRIPT, RESULTS, RENDER, LABELS = "report.md", "manuscript.md", "results.md", "report_render.json", "presentation.yaml"
FIGURE_SETS = ("standard", "estimates")                # the two producers of analysis/figures.yaml (§5.12)
FIG_PRODUCER = {"standard": "toolkit/analysis/figures.py", "estimates": "toolkit/analysis/estimates.py"}
# The producers' own words. toolkit/engines/benchmark.py: STATUSES and the two reasons a band cannot be met;
# toolkit/engines/benchmark_coverage.py gate(): why a benchmark is reported and does not gate; toolkit/analysis/figures.py:
# the reasons a figure is not produced.
BENCH_STATUSES = ("in_band", "out_of_band", "not_assessable", "band_mis_based")
MIS_BASED = ("price_floor", "unreachable_in_fitted_range")
REPORTED_WHY = "the spec prices the component itself, not through this engine cost output"
NO_TRAJECTORIES = "needs trajectory runs (python -m toolkit.analysis.trajectories run <run_dir>)"
NO_TESTS_FILE = "needs analysis/decision_tests.yaml, which this run does not have"
ENGINE_REF = "toolkit.engines.toy.engine:ToyEngine"
HELD = "c1"                                            # a scenario value the signed spec excludes from the draws by name
CAL_DECLARED = {"target": "grp_r1:G1", "decision_test": "DT6",
                "reason": "the earlier-round value for G1 is matched only at the primary error setting; tested by DT6"}
ENGINE = {"name": "toy-engine", "version": "0.1", "card_sha256": hashlib.sha256(b"toy card").hexdigest()}
# The engine departures a variant signs ({engine, benchmark | component, reason, decision_test}: benchmark_coverage.py).
DEPARTURES = {
    "B2": {"engine": ENGINE["name"], "benchmark": "B2", "decision_test": "DT4",
           "reason": "B2 is held out; where the engine sits outside its band, DT4 runs the plan across the band"},
    "B": {"engine": ENGINE["name"], "component": "B", "decision_test": "DT5",
          "reason": "no benchmark compares component B with its absence; DT5 runs its effect from none to the top"}}
SCOPE_WHY = "The data cannot narrow p1 further; DT3 runs the plan at both ends of its accepted range."
MECHANISMS = ("components A and B on one outcome", "one value per unit sets its level before any package",
              "the two components combine on the ratio scale")
HARNESS_INPUT = {"../../toolkit/engines/toy/priors.yaml": hashlib.sha256(b"toy priors").hexdigest()}
# Which stage writes each file. run_state.json and metadata.json are written at every stage.
STAGE_OF = {"decision_spec.yaml": "launch", "data/derived/units.csv": "launch", "data/derived/targets.csv": "launch",
            "metadata.json": "launch", "run_state.json": "launch",
            "decision_spec.signoff.json": "sign-off", "decision_brief.signed/decision_spec.yaml": "sign-off",
            JC: "calibration", CAL: "calibration", ENGINE_REC: "calibration", BSV: "calibration", BSV_REC: "calibration",
            FIGS: "analysis", RESULTS: "analysis", REPORT: "final", MANUSCRIPT: "final", RENDER: "final", LABELS: "launch",
            "analysis/point.yaml": "analysis", UNC: "analysis", DT: "analysis", EST: "analysis", "allocation.csv": "analysis",
            "analysis/manifest.json": "analysis",
            SHEET: "sign-off", DOSSIER: "sign-off", CHOICE: "sign-off",
            LEDGER: "final", SCOPE: "final", OLD_DECLARED: "final", OLD_GATE: "final", GATES[0]: "final", GATES[1]: "final"}
HISTORY = [("SPEC", "SIGN", 30, 1.0), ("SIGN", "EVIDENCE", 60, 1.0), ("EVIDENCE", "ENGINE_CHOICE", 70, 1.4),
           ("ENGINE_CHOICE", "CONFIGURE", 80, 1.8), ("CONFIGURE", "ANALYZE", 90, 2.0), ("ANALYZE", "CRITIQUE", 240, 2.0),
           ("CRITIQUE", "GATE", 260, 3.0), ("GATE", "WRITE", 270, 3.0), ("WRITE", "PRESENTATION", 290, 3.6),
           ("PRESENTATION", "FINAL", 300, 3.8)]
REACHED = {"launch": 1, "sign-off": 4, "calibration": 5, "analysis": 6, "final": 10}   # transitions made by each stage


def stage_of(rel):
    """The stage that writes a file. Under figures/, the tables of the calibration figures are written with the
    calibration (the Fit view reads them from there); every other figure file is written at the end of the analysis."""
    if rel.startswith("figures/"):
        return "calibration" if rel.startswith("figures/calibration_") and rel.endswith(".csv") else "analysis"
    return STAGE_OF[rel]


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
            "decision_date": "2026-06-01", "engine": "auto",
            "engine_parameters": {"p2": {"value": 3.1, "units": "ratio", "source": "the owner's range",
                                         "reason": "no measurement", "basis": "owner_judgement", "range": [1.0, 5.5],
                                         "dist": "uniform"}},
            "decision_units": {"csv": "data/derived/units.csv", "id": "unit_id", "label": "name", "region": GROUPING},
            "options": {"components": {"A": {}, "B": {}}, "packages": {"generate": True}},
            "comparator": {"package": "A"},
            "uncertainty": {"draws": 50, "excluded": [
                {"parameter": HELD, "reason": f"{HELD} is a scenario value held fixed by name; DT7 runs it at both ends"}]},
            "analysis": {"decision_tests": [{"id": "DT1", "type": "parameter_values", "parameter": "p2",
                                             "values": [1.6, 4.4], "text": "Does the plan change across p2's range?"},
                                            *_spec_tests(method)]},
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


def _spec_tests(method):
    """The decision tests the spec declares for the register's rows (each names its finding, as a stored spec's do)."""
    def test(tid, kind, finding=None, **what):
        return {"id": tid, "type": kind, **what, "text": f"Does the plan change under {tid}?",
                "finding": finding or f"decision_test:{tid}"}
    return [test("DT3", "parameter_value", "R-001", parameter="p1", values=[0.42, 0.58]),
            test("DT4", "interaction_rescale", components=["B"], with_any=["A"], benchmark="B2", values=[0.5, 2.0]),
            test("DT5", "parameter_value", parameter="b_add", values=[0.0, 0.1]),
            *([test("DT6", "calibration_target", calibration_target={"units": list(UNITS[:4])})]
              if method == "history_matching" else []),
            test("DT7", "parameter_value", parameter=HELD, values=[0.0, 1.0]),
            test("DT8", "parameter_value", parameter="p2", values=[1.0, 5.5]),
            test("DT9", "unit_attribute", unit_attribute={"column": "x1", "units": "all"})]


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
        "status": "matched", "blocked": False, "blocks": [], "cutoff": CUTOFF, "shared": prior,
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
                         "report": {"method": "history_matching", "status": "matched", "blocked": False, "blocks": [],
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
                            "rhat_max": 1.0, "ess_bulk_min": 910.0, "acceptance": 0.24, "doublings": 0},
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


def _test(tid, ran=True, changes=False):
    share = 25.0 if changes else 0.0
    return {"test_id": tid, "path": f"models/decision_tests/{tid}.yaml" if ran else None,
            "status": "run" if ran else "not_run",
            "verdict": "NOT_RUN" if not ran else "CHANGES_PLAN" if changes else "NO_CHANGE",
            "can_move_plan": True if ran else None, "threshold_pct": 10.0, "share_pct": share if ran else None,
            "regret_pct": (2.0 if changes else 0.0) if ran else None,
            "statement": ("This test was not run: its values could not be reached." if not ran else
                          f"This test changes the plan: {share:g}% of units change package, above the 10% threshold."
                          if changes else "This test can move the plan: it was re-solved for each value.")}


def _set_test(m, tid, **how):
    m["dt"]["tests"] = [_test(tid, **how) if t["test_id"] == tid else t for t in m["dt"]["tests"]]


# The ledger's own vocabulary (agents/findings_ledger.py): STATUSES, and OWNER_STATE for the stages used here.
OWNER_STATE = {"PLAN": "SPEC", "ANALYZE": "ANALYZE", "WRITE": "WRITE"}
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
# A finding a harness check raises about the run's files, with the id and the claim toolkit/analysis/contract.py gives
# it: kind, then the key "<artifact>:<input>". On the clean run it is fixed and the input is fresh; the planted
# variant that leaves it open also makes the input stale, so the register and the Steps check agree on every fixture.
STALE_ID = "analysis_contract_stale:analysis/point.yaml:analysis/calibration.yaml"
STALE_CLAIM = "artifact analysis/point.yaml: input analysis/calibration.yaml changed (or vanished) after it was computed"


def _finding(fid, source, severity, stage, claim, status="fixed", test=None, blocking=False):
    """One row of findings.jsonl with the ledger's twenty fields and the events the ledger writes on the way to
    `status` (a critic's finding follows its critique file; a gate finding absent at a later gate is fixed)."""
    critic = source.startswith("critique")
    kind, _, key = ("critique_blocker", "", "") if critic else fid.partition(":")
    events = [{"by": source, "round": 1, "status": "open"}]
    if status == "fixed":
        events.append({"by": source, "round": 2, "status": "fixed"} if critic else
                      {"by": "gate", "note": "absent at this gate", "round": 2, "status": "fixed"})
    elif status == "declared":
        events.append({"by": "scope_declaration", "round": 1, "status": "declared"})
    elif status in ("owner_accepted", "owner_declined"):
        events += [{"by": "scope_declaration", "round": 1, "status": "declared"}, {"by": "owner", "round": 1, "status": status}]
    return {"id": fid, "kind": kind, "severity": severity, "source": source, "key": key,
            "owner_state": OWNER_STATE[stage], "owner_state_known": True, "target_stage": stage,
            "resolution_type": "decision_test" if test else "fix", "status": status, "blocking": blocking,
            "scope_declarable": True, "first_seen_round": 1, "last_seen_round": 2 if status == "fixed" else 1,
            "instances": 1, "claim": claim,
            "fix_requires": "State the limit in the report, or test it." if critic else None,
            "done_when": [{"contains": "limit", "file": "manuscript.md"}] if critic else None,
            "decision_test": ({"decision_changes": False, "file": f"models/decision_tests/{test}.yaml",
                               "finding": fid, "valid": True} if test else None),
            "events": events}


def _findings():
    return {f["id"]: f for f in (
        _finding("D-001", "critique-domain", "HIGH", "PLAN",
                 "The comparator leaves out a delivery already funded, so the gain of every package is overstated."),
        _finding("D-002", "critique-domain", "MEDIUM", "WRITE",
                 "The report gives the point value of the headline without its interval."),
        _finding("R-001", "critique-redteam", "MEDIUM", "ANALYZE",
                 "The plan may depend on p1, which the fit narrows only to the middle of its prior."),
        _finding(STALE_ID, "rigor-artifacts", "HIGH", "ANALYZE", STALE_CLAIM))}


def _declaration(n, blocker, test, why):
    return {"id": f"SD-{n:03d}", "blocker_id": blocker, "why_unresolved": why,
            **({"decision_test": f"models/decision_tests/{test}.yaml"} if test else {})}


def _choice():
    return {"at": _at(75),
            "candidates": [{"ref": ENGINE_REF, "config": {}, "name": ENGINE["name"], "version": ENGINE["version"],
                            "eligible": True, "why_not": [], "mechanisms": list(MECHANISMS),
                            "benchmarks": 4, "departures": [], "benchmark_record": "verified",
                            "benchmarks_not_in_band": [],
                            "benchmarks_reported": [{"id": "B4", "why": REPORTED_WHY, "status": "in_band", "role": "held_out"}],
                            "card_benchmark_rows_disagree_with_record": [], "components_without_benchmark": {}},
                           # agents/analysis_backend.py eligible_engines(): a candidate whose card does not fit, and
                           # one whose card cannot be read (no name, no version, no list of any kind)
                           {"ref": "toolkit.engines.other.engine:OtherEngine", "config": {}, "name": "other-engine",
                            "version": "1", "eligible": False, "why_not": ["no component B", "no outcome outcome_averted"],
                            "mechanisms": ["closed-form outcome"], "benchmarks": 0, "departures": [],
                            "benchmark_record": None, "benchmarks_not_in_band": [], "benchmarks_reported": [],
                            "card_benchmark_rows_disagree_with_record": [], "components_without_benchmark": {}},
                           {"ref": "toolkit.engines.third.engine:ThirdEngine", "eligible": False,
                            "why_not": ["card unreadable: ValueError: the engine is configured by the signed spec"]}],
            "engine": {"ref": ENGINE_REF, "config": {}},
            "why": "the one eligible engine for the spec's components and outcome",
            "engine_departures": []}


def _no_data(value, reason, rule, test=None, source="spec default accepted by name"):
    return {"model_value": value, "model_source": source, "no_data": {"reason": reason, "borrowing_rule": rule},
            "chosen": {"value": value, "rule": rule}, **({"decision_test": test} if test else {})}


def _dossier():
    return {"meta": {"compiled": "2026-01-01", "status": "fixture dossier: four entries",
                     "format": "toolkit/spec/evidence_dossier.py", "local_only": True},
            "inputs": {
                "parameter:p1": {
                    "model_value": {"value": BEST["p1"], "range": PRIOR["p1"], "dist": "uniform"},
                    "model_source": "fixture prior for p1",
                    "observations": [{"source": "study one", "where": "G1", "year": "2019", "sites": "3", "n": "400",
                                      "sampling_frame": "households", "value": "0.45"},
                                     {"source": "study two", "where": "G3", "year": "2021", "sites": "2", "n": "250",
                                      "sampling_frame": "households", "value": "0.55"}],
                    "comparison": "the model value sits between the two observations",
                    "chosen": {"range": PRIOR["p1"], "rule": "the prior range"}, "decision_test": "DT3"},
                "parameter:p2": _no_data(BEST["p2"], "no measurement of p2 was found", "draw over the owner's range",
                                         "DT8", "spec engine_parameters.p2 (owner's range)"),
                f"parameter:{HELD}": _no_data(0.5, f"no source gives {HELD}", "hold at the default; run both ends as a "
                                              "scenario", "DT7"),
                "unit_attribute:x1": _no_data("the units table's column x1", "x1 is a modelled surface, not a measurement",
                                              "use the surface; test the plan against the flat alternative", "DT9")}}


def _sheet_text(m, spec_sha):
    """decision_spec.signoff_sheet.md, in the words of toolkit/spec (engine_parameters.py writes the HIGH lines). The
    clean sheet marks no row HIGH (the card's range holds the spec's); with m["high"] the card's range is narrower and
    the row is marked. The word also appears where it marks nothing (the geometry section's sentence, a table cell)."""
    card_high = 5 if m["high"] else 6
    lines = ["# Decision spec sign-off sheet", "",
             f"Signing accepts `decision_spec.yaml` (sha256 `{spec_sha[:16]}`). It also accepts what follows.", "",
             "## Engine defaults accepted by name",
             "- **seed**: 1: the engine's own seed. Reason: the result does not depend on it", "",
             "## Input geometry check",
             "Proposed exceptions to HIGH findings (the owner's sign-off approves them; a lead sign-off does not):",
             "- none", "",
             "## Engine parameter values set by the spec",
             "- **p2** = 3.1. Basis: owner_judgement. Source: the owner's range. Reason: no measurement. "
             "Uncertainty: drawn over [1, 5.5] (uniform).",
             f"  - `{ENGINE_REF}`: card value 3 (fitted; fixture card); card range 1 to {card_high}", ""]
    lines += [f"- **HIGH**: the uncertainty range of {name} (1 to 5.5) reaches outside the card range of `{ENGINE_REF}` "
              f"(1 to 5; outside_card_range: true). Signing accepts it; its source and reason are above."
              for name in m["high"]]
    lines += ["", "## Evidence dossier (inputs that drive an option's effect)",
              "| input | used by | model value | chosen | observations |", "|---|---|---|---|---|",
              "| parameter:p1 | component A | 0.5 | 0.5 (owner decision, HIGH confidence in both studies) | 2 |", ""]
    return "\n".join(lines)


# ---- the engine record and the figure set (§5.4, §5.12)

def _seeds(row, computed, low=0.96, high=1.04):
    """A benchmark row with its computed value, four seeds spread around it, and the status
    toolkit/engines/benchmark.py gives a value against its band (`in_band` follows the status)."""
    seeds = [_r(computed * (low + (high - low) * k / 3)) for k in range(4)]
    inside = row["band"][0] <= computed <= row["band"][1]
    row.update(computed=computed, se_log=0.02, per_seed=seeds, seed_min=min(seeds), seed_max=max(seeds),
               in_band=inside, status="in_band" if inside else "out_of_band")
    return row


def _bench_row(bid, metric, role, target, band, source, **more):
    """One row of the engine record's benchmark table, with the keys toolkit/engines/benchmark.py writes. The clean
    value is the band's centre on the log scale, so it is clear of both edges, and so are its seeds."""
    row = {"id": bid, "metric": metric, "role": role, "target": target, "band": list(band), "source": source,
           "band_sha256": hashlib.sha256(f"{bid} band".encode()).hexdigest()}
    _seeds(row, _r(math.sqrt(band[0] * band[1])))
    row.update(more)
    return row


def _engine_record():
    """analysis/engine.yaml `data` (toolkit/analysis/run.py): the card as the run used it, the engine's parameters and
    the values the signed spec sets, shaped as a stored run's. Four benchmarks, all in band at the centre of the band;
    B4 is a cost benchmark whose band spans more than a factor of ten, and the engine choice only reports it."""
    def par(name, value, kind, units, lo, hi, source):
        return {"name": name, "value": value, "kind": kind, "units": units, "lo": lo, "hi": hi, "source": source,
                "verified": "matched"}
    fitted_src = "fitted once to benchmarks B1 and B4 (fixture card)"
    return {
        "card": {"name": ENGINE["name"], "version": ENGINE["version"], "mechanisms": list(MECHANISMS),
                 "components": {"A": [0, 1], "B": [0, 1]}, "unit_attributes": {"x1": "level before any package, [0, 1]"},
                 "outputs": {"per_capita": ["outcome"], "windows": ["m1"], "cost_drivers": ["A_given"]},
                 "age_groups": ["all"], "valid_ranges": {"x1": [0.0, 1.0]},
                 "fitted": {"p2": {"value": 3.0, "lo": 1.0, "hi": 6.0, "box": [0.5, 8.0], "benchmarks": ["B1", "B4"]},
                            "f1": {"value": 0.4, "lo": 0.3, "hi": 0.6, "box": [0.0, 1.0], "benchmarks": ["B1", "B4"]}},
                 "benchmarks": [
                     _bench_row("B1", "component A against none, outcome ratio at 12 months", "fit", 0.6, [0.4, 0.9],
                                "fixture study one, table 2"),
                     _bench_row("B2", "component B against none, outcome ratio over 24 months", "held_out", 0.5,
                                [0.36, 0.7], "fixture study two, table 1"),
                     _bench_row("B3", "components A and B against A alone, outcome ratio at 24 months", "held_out", 0.8,
                                [0.64, 1.0], "fixture study three, table 3"),
                     _bench_row("B4", "cost per outcome averted, component A against none", "held_out", 12.0, [2.0, 72.0],
                                "fixture costing review", outcome_averted_per_capita=0.1, cost_per_capita=1.2,
                                comparator_outcome_per_capita=0.4, share_averted=0.25, price_floor=3.6,
                                target_below_price_floor=False, share_averted_needed_for_band=0.05)],
                 "notes": ["one outcome only; no change of the level within the horizon"]},
        "parameters": [par("p1", 0.5, "calibrated", "share", 0.2, 0.8, "calibrated per run to the unit targets"),
                       par("p2", 3.0, "fitted", "ratio", 1.0, 6.0, fitted_src),
                       par("p3", 30.0, "calibrated", "days", 10.0, 50.0, "calibrated per run to the unit targets"),
                       par("f1", 0.4, "fitted", "share", 0.3, 0.6, fitted_src),
                       par("b_add", 0.05, "scenario", "share", 0.0, 0.1, "fixture study two, table 4"),
                       par(HELD, 0.5, "scenario", "share", 0.0, 1.0, "fixture study one, table 5")],
        # toolkit/spec/engine_parameters.py rows(): one entry per value the signed spec sets
        "spec_values": {"p2": {"value": 3.1, "units": "ratio", "card_value": 3.0, "card_kind": "fitted",
                               "card_range": [1.0, 6.0], "card_source": fitted_src, "range": [1.0, 5.5],
                               "outside_card_range": False, "outside": None, "source": "the owner's range",
                               "reason": "no measurement", "uncertainty": "drawn over [1, 5.5] (uniform)"}}}


def _identity():
    return {"name": ENGINE["name"], "version": ENGINE["version"],
            **{k: hashlib.sha256(f"toy {k}".encode()).hexdigest() for k in ("code_sha256", "params_sha256", "config_sha256")}}


def _at_spec_values():
    """analysis/benchmarks_at_spec_values.yaml (toolkit/engines/benchmark.py, written for the harness step of
    toolkit/spec/engine_parameters.py): the benchmark a decision test of the spec reads, recomputed at the spec's
    values. It has no artifact header; its record file holds its hash and the spec's."""
    row = _bench_row("B2", "component B against none, outcome ratio over 24 months", "held_out", 0.5, [0.36, 0.7],
                     "fixture study two, table 1")
    _seeds(row, 0.51)
    by = {s: int(s == row["status"]) for s in BENCH_STATUSES}
    arm = {"arm": "a1", "setting": "s1", "ref": ENGINE_REF, "config": None, "config_name": None,
           "engine_identity": _identity(), "config_sha256": None, "superseded": {}}
    return {"producer": "toolkit/engines/benchmark.py", "engine_identity": _identity(), "benchmarks": [row],
            "initialisation": {"method": "no-package run-in before the horizon starts", "evidence": []},
            "settings_calibrated": {}, "fitted_used": {"p2": 3.0, "f1": 0.4}, "configs_validated": {},
            "configs_not_exercised": [], "seeds": [11, 12, 13, 14], "n_sims": 8,
            "summary": {"in_band": by["in_band"], "total": 1, "by_status": by, "held_out_in_band": by["in_band"],
                        "held_out_total": 1},
            "spec_parameters": {"p2": 3.1}, "arm_engines": {"B2": {"arms": {"a1": arm}}}}


def _png(name):
    """A valid PNG of one pixel whose colour follows from `name`, so each figure's image has its own hash."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    pixel = b"\x00" + hashlib.sha256(name.encode()).digest()[:3]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixel, 9)) + chunk(b"IEND", b""))


SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>\n'


def _figure_set(m):
    """The figures of the run as (name, tier, the analysis files drawn from, title, what it shows). `standard`: a
    subset of the standard set of toolkit/analysis/figures.py under its own names and its three tiers, with one figure
    per calibration table the method writes. `estimates`: the four figures toolkit/analysis/estimates.py draws."""
    if m["fig"]["set"] == "estimates":
        return [("diagnostics_ppc", "diagnostics", [EST], None, None), ("diagnostics_rt", "diagnostics", [EST], None, None),
                ("estimates_q1", "decision", [EST], None, None), ("estimates_q2", "decision", [EST], None, None)]
    cal = JC if m["method"] == "history_matching" else CAL
    return [("data_coverage", "data_model", [], "12 units in 3 groups; every engine attribute bound", "data coverage and provenance"),
            ("parameters", "data_model", [ENGINE_REC], "6 engine parameters; 1 set by the spec", "engine parameters and their ranges"),
            *[(os.path.basename(rel)[:-4], "data_model", [cal], f"{os.path.basename(rel)[12:-4].replace('_', ' ')} of the "
               "calibration, 12 units", "calibration diagnostics") for rel in _figures(m)],
            ("timeline", "simulation", [], "what each package delivers, and when", "delivery schedule"),
            ("trajectories", "simulation", ["analysis/point.yaml"], "outcome over time under the plan and the comparator",
             "simulated outcome over time"),
            ("headline", "decision", ["analysis/point.yaml", UNC], "the plan against the comparator, with its interval",
             "headline result"),
            ("frontier", "decision", ["analysis/point.yaml"], "outcome averted against the envelope", "budget frontier"),
            ("decision_tests", "decision", [DT], "which decision tests change the plan",
             "decision tests: does the plan change?")]


def _figure_files(m, files, sha, artifact):
    """analysis/figures.yaml and, per figure, its image, vector copy, table and provenance record, as the producer of
    m["fig"]["set"] writes them (toolkit/analysis/figures.py render() and _save(); toolkit/analysis/estimates.py _fig();
    scripts/figure_validator.py for the record). Returns the manifest's entries for the images."""
    f, standard = m["fig"], m["fig"]["set"] == "standard"
    shown = [{"path": LABELS, "sha256": sha(LABELS), "kind": "presentation"}] if standard else []
    entries, missing, listed, earlier = [], [], {}, hashlib.sha256(b"the source as it was when the figure was drawn").hexdigest()
    plan = _figure_set(m)
    for name, tier, drawn_from, title, shows in plan:
        reason = f["not_produced"].get(name) or (NO_TESTS_FILE if DT in drawn_from and m["dt"] is None else None)
        if reason:
            missing.append({"figure": name, "reason": reason, "tier": tier})
            continue
        png, sources = f"figures/{name}.png", drawn_from + (["decision_spec.yaml", "data/derived/units.csv"] if standard else [])
        files[png], files[png[:-4] + ".svg"] = _png(name), SVG
        if standard:
            files.setdefault(png[:-4] + ".csv", _csv(["item", "value"], [[name, 1], ["units", len(UNITS)]]))
            entry = {"figure": name, "path": png, "svg": png[:-4] + ".svg", "table": png[:-4] + ".csv", "title": title,
                     "caption": f"{title[0].upper()}{title[1:]}; drawn by the harness from the files listed as its sources.",
                     "shows": shows, "sources": sources, "offset_free": True, "theme": "light", "tier": tier,
                     "group": "decision" if tier == "decision" else "diagnostics", "presentation": copy.deepcopy(shown)}
        else:
            entry = {"id": name, "path": png, "svg": png[:-4] + ".svg", "sources": sources, "tier": tier}
        if name in f["no_path"]:
            del entry["path"]
        entries.append(entry)
        record = {"expected_annotations": [], "expected_n_data_points": None,
                  "generator_script": "/work/toolkit/analysis/figures.py", "paths_relative_to": "run_dir", "png_path": png,
                  "presentation": copy.deepcopy(shown), "produced_by": "harness", "producer": FIG_PRODUCER["standard"],
                  "source_hashes": {x: earlier if f["stale"].get(name) == x else sha(x) for x in sources},
                  "written_at_utc": _at(233)}
        text = json.dumps(record, indent=2, sort_keys=True)
        if name not in f["no_record"]:
            files[png + ".provenance.json"] = text[:len(text) // 2] if name in f["bad_record"] else text
        if name not in f["unlisted"]:
            listed[png] = {"producer": FIG_PRODUCER[f["set"]], "role": "figure", "sha256": sha(png)}
    tiers = {}
    for name, tier, *_ in plan if standard else []:
        tiers.setdefault(tier, []).append(name)
    overviews = {t: f"figures/overview_{t}.png" for t in tiers} if entries else {}
    for rel in overviews.values():
        files[rel] = _png(rel)
    groups = {"decision": list(tiers.get("decision", [])),
              "diagnostics": [n for t in ("data_model", "simulation") for n in tiers.get(t, [])]} if standard else {}
    inputs = sorted({x for _, _, drawn_from, _, _ in plan for x in drawn_from if x in files})
    artifact(FIGS, "figures", f["producer"] or FIG_PRODUCER[f["set"]], inputs,
             {"figures": entries, "not_produced": missing, "tiers": tiers, "groups": groups, "overviews": overviews}, 234)
    return listed


def _report_files(m):
    """report.md, manuscript.md and the render record. The report names figures by path (and one by its file name
    alone); it also uses the word trajectories in a sentence, which names no figure."""
    lines = ["# Report (synthetic fixture)", "", "The plan, its tests and the trajectories behind it are set out below.", ""]
    for rel in m["fig"]["report"]:
        lines += [f"![{os.path.basename(rel)[:-4].replace('_', ' ')}]({rel})", ""]
    report = "\n".join(lines)
    script = ("# Manuscript (synthetic fixture)\n\nThe limit of this analysis is stated with each result.\n\n"
              + "".join(f"See {rel}.\n" for rel in m["fig"]["manuscript"]))
    return {REPORT: report, MANUSCRIPT: script,
            RENDER: json.dumps({"report_sha256": hashlib.sha256(report.encode()).hexdigest(),
                                "writer_span": [len(lines[0]) + 2, len(report)]}, indent=1)}


def _model(method, figures="standard"):
    names = (["figures/estimates_q1.png", "estimates_q2.png"], ["figures/diagnostics_ppc.png"]) if figures == "estimates" \
        else (["figures/headline.png", "figures/frontier.png", "decision_tests.png"], ["figures/data_coverage.png"])
    m = {"method": method, "spec": _spec(method),
         # the engine record (§5.4): `bound`: the analysis headers hold the engine choice's hash; `choice_after`: an
         # edit made to engine_choice.json after the analysis; `cut`: the engine record stops inside its benchmark table
         # `bsv`: the benchmarks recomputed at the spec's values, when the run has them (the clean run has none: the
         # file has no artifact header and is outside the manifest, which the Steps view raises: gap H13)
         "eng": _engine_record(), "bsv": None, "bound": True, "choice_after": None, "cut": False,
         # the figure set (§5.12): `producer` another producer name; `not_produced` {figure: reason}; `stale` {figure:
         # the source whose recorded hash is an earlier one}; `no_path` entries with no path; `no_record`, `bad_record`
         # figures whose provenance record is absent or cut off; `unlisted` images the manifest leaves out; `dropped`,
         # `redrawn` images removed or replaced after the manifest; `report`, `manuscript` what the texts name
         "fig": {"set": figures, "producer": None, "not_produced": {}, "stale": {}, "no_path": (), "no_record": (),
                 "bad_record": (), "unlisted": (), "dropped": (), "redrawn": (), "report": names[0], "manuscript": names[1]}, "unc": {"draws": 50, "seed": 1, "calibration_clamped": 0,
                                                          "prior_dominated": ["p3"]},
         "dt": {"tests": [_test("DT1")] + [_test(t["id"], changes=t["id"] == "DT9") for t in _spec_tests(method)]
                + [_test(f"scenario_excluded_{HELD}", ran=False)]},
         # the register's sources (§6); `comments` are comment lines of the spec, `disk` an edit made to the spec on
         # disk after the sign-off, `old` the two files an older run keeps dispositions in
         "findings": _findings(), "ledger_extra": [], "choice": _choice(), "dossier": _dossier(), "high": [],
         "scope": None,
         "signed_by": "owner", "signoff_note": "fixture sign-off", "comments": [], "disk": None, "old": None,
         # `gates`: the run keeps its gate records; `unjudged`: findings whose done-when test the last gate did not
         # judge; `stale`: analysis/point.yaml records another hash for its input than the file has;
         # `high`: the parameters the sign-off sheet marks HIGH
         "gates": True, "unjudged": (), "stale": False}
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


# ---- the science register's edits (§5.8, §6)

def _restate(m, fid, **how):
    f = m["findings"][fid]
    m["findings"][fid] = _finding(fid, f["source"], f["severity"], f["target_stage"], f["claim"], **how)


def _left_open(fid, blocking=False):
    return lambda m: _restate(m, fid, status="open", blocking=blocking)


def _stale_open(m):
    _restate(m, STALE_ID, status="open", blocking=True)
    m["stale"] = True          # what the finding is about is true of the run's files


def _gate(m, rnd):
    """A gate decision as scripts/validate_critique_yaml.py writes it for round `rnd`; done_when_status holds, per
    critic's finding, what scripts/done_when.py run() records: {passes, detail, mechanical, frozen_round, result,
    open, severity}. In round 1 no test passes; in the last round the test of each finding the ledger holds as
    fixed passes, unless the variant leaves it not judged."""
    last, status = rnd == len(GATES), {}
    for f in m["findings"].values():
        if not f["source"].startswith("critique"):
            continue
        passes = (None if f["id"] in m["unjudged"] else True) if last and f["status"] == "fixed" else False
        status[f["id"]] = {
            "passes": passes, "mechanical": passes is not None, "frozen_round": 1,
            "detail": ("pass manuscript.md exists; pass manuscript.md: found 'limit'" if passes else
                       "manuscript.md: 'limit' not found" if passes is False else "needs a judgement that was not recorded"),
            "result": "passed" if passes else "failed" if passes is False else "not_judged",
            "open": f["status"] == "open" or not last, "severity": f["severity"]}
    held = [{"id": f["id"], "reviewer": f["source"], "severity": f["severity"]} for f in m["findings"].values()
            if f["severity"] == "HIGH" and f["source"].startswith("critique") and (f["status"] == "open" or not last)]
    results = [e["result"] for e in status.values()]
    return json.dumps({
        "unresolved_high": held, "accept_blockers": [], "structural_mismatch": False, "structural_reviewers": [],
        "spec_violations": [], "registry_violations": [], "rigor_violations": [], "blocker_attempts": {},
        "escalation_required": False, "rounds_remaining": len(GATES) - rnd, "action": "PATCH" if held else "ACCEPT",
        "rule_matched": 2 if held else 1, "rationale": f"{len(held)} unresolved HIGH blocker(s)." if held else "No blocker is open.",
        "declared": [], "declared_prev": [], "done_when_status": status,
        "check_summary": {k: results.count(k) for k in ("passed", "failed", "not_judged")}, "post_write": False}, indent=1)


def _scope_declare(m, blocker, test, why):
    """scope_declaration.yaml gains a declaration (the file is written only when something is declared)."""
    m["scope"] = m["scope"] or {"declarations": []}
    m["scope"]["declarations"].append(_declaration(len(m["scope"]["declarations"]) + 1, blocker, test, why))


def _depart(m, *subjects, status="signed"):
    """A signed engine departure per subject (a key of DEPARTURES): the entry in the signed spec, and its copy in
    engine_choice.json as toolkit/engines/benchmark_coverage.py gate() records it: the status of the signature, then
    for a benchmark its own status and role in the engine record and the components it covers (`component=value`),
    for a component `no benchmark`, role `none` and the component."""
    for s in subjects:
        d = DEPARTURES[s]
        m["spec"].setdefault("engine_departures", []).append(dict(d))
        if "benchmark" in d:
            row = _bench(m, s)
            failing = {"benchmark_status": row["status"], "role": row["role"], "components": ["B=1"]}
        else:
            failing = {"benchmark_status": "no benchmark", "role": "none", "components": [s]}
        for deps in (m["choice"]["engine_departures"], _chosen(m)["departures"]):
            deps.append({**d, "status": status, **failing})


def _declared(m):
    """One of each declaration, each naming a decision test that ran with no change: a signed departure for benchmark
    B2, which is out of band, and one for component B; for history matching a declared calibration miss; finding
    R-001 held as declared by the ledger and declared in scope_declaration.yaml."""
    _b2_out(m)
    _depart(m, "B")
    if m["method"] == "history_matching":
        m["spec"]["calibration"]["joint"]["unmatched"]["declared"].append(dict(CAL_DECLARED))
        m["jc"]["unmatched_decision"]["declared"].append(dict(CAL_DECLARED))
    _restate(m, "R-001", status="declared", test="DT3")
    _scope_declare(m, "R-001", "DT3", SCOPE_WHY)


def _on_declared(*edits):
    """An edit made on top of the run with one of each declaration."""
    def edit(m):
        _declared(m)
        for e in edits:
            e(m)
    return edit


def _c12_declared(m):
    _restate(m, "D-001", status="declared", test="DT1", blocking=True)
    _scope_declare(m, "D-001", "DT1", "The funded delivery cannot be costed from the inputs of this run; DT1 runs the "
                                      "plan across the range that it moves.")


def _each_departure(m):
    return [d for deps in (m["choice"]["engine_departures"], m["choice"]["candidates"][0]["departures"]) for d in deps]


def _departure_no_test(m):
    for d in _each_departure(m) + m["spec"]["engine_departures"]:     # signed that way: the choice copies the spec's entry
        if d.get("component") == "B":
            del d["decision_test"]


def _disk_only(m):
    m["disk"] = lambda spec: spec["uncertainty"]["excluded"].append(
        {"parameter": "q7", "reason": "added to the file after the sign-off; no signature covers it"})


def _merge_test(m):
    m["dossier"]["inputs"]["unit_attribute:x2"] = _no_data(
        "the units table's column x2", "x2 has no measurement", "use the default; DT3 covers it with p1", "DT3")


def _merge_input(m):
    m["spec"]["uncertainty"]["excluded"].append({"parameter": "q4", "reason": "q4 is held fixed by name"})
    m["dossier"]["inputs"]["unit_attribute:q4"] = _no_data(
        "the units table's column q4", "q4 has no measurement", "hold at the default; DT1 runs the plan across it", "DT1")


def _lead(m):
    _declared(m)
    m.update(signed_by="lead", signoff_note="lead mode: the harness accepts the drafted spec")
    for d in _each_departure(m):
        d["status"] = "unsigned"       # toolkit/engines/benchmark_coverage.py signature(): a lead's signature


def _older(m):
    for fid in ("D-002", "R-001"):
        _restate(m, fid, status="open")
    m["scope"] = None
    m["old"] = {"declared": ["R-001"], "tested": ["D-002"]}


PROSE = ["# DEPARTURE (owner decision, fixture): the comparator is the funded plan, not the status quo; DT1 covers it.",
         "# LIMITATION (owner-visible): the best point of p2 sits near the floor of its range.",
         "# The values below are drawn from two studies. ASSUMPTION (owner-visible): the two share one sampling frame.",
         "# The tests of the declared choice are listed under analysis; see the DEPARTURE block above."]


def _prose(m):
    m["comments"] = list(PROSE)


def _bad_line(m):
    whole = json.dumps(m["findings"]["D-002"], sort_keys=True)
    m["ledger_extra"].append(whole[:len(whole) // 2])       # a row cut off in the middle, as a write that stopped


def _odd_status(m):
    m["findings"]["D-002"]["status"] = "resolved"            # not one of the ledger's STATUSES


def _ghost(m):
    _scope_declare(m, "X-404", "DT1", "Declared by an id that no finding has.")


def _no_tests_file(m):
    m["dt"] = None


# ---- the Model view's edits (§5.4)

def _bench(m, bid):
    return next(b for b in m["eng"]["card"]["benchmarks"] if b["id"] == bid)


def _chosen(m):
    return m["choice"]["candidates"][0]


def _moved(bid, computed, **more):
    """Benchmark `bid` of the engine record gets another computed value (seeds and status to match); where the engine
    choice only reports the benchmark, its row there copies the status, as the producer does."""
    def edit(m):
        row = _seeds(_bench(m, bid), computed)
        row.update(more)
        if more.get("status"):
            row["in_band"] = more["status"] == "in_band"
        for r in _chosen(m)["benchmarks_reported"]:
            if r["id"] == bid:
                r["status"] = row["status"]
    return edit


def _b2_out(m, ran=True):
    """Benchmark B2 out of band, with the signed departure that names it and decision test DT4."""
    _moved("B2", 0.78)(m)
    _depart(m, "B2")
    if not ran:
        _set_test(m, "DT4", ran=False)


def _b3_out_disk(m):
    _moved("B3", 1.12)(m)
    m["disk"] = lambda spec: spec.setdefault("engine_departures", []).append(
        {"engine": ENGINE["name"], "benchmark": "B3", "decision_test": "DT1",
         "reason": "added to the file after the sign-off; no signature covers it"})


def _seed_cross(m):
    seeds = [0.5, 0.58, 0.66, 0.93]
    _bench(m, "B1").update(per_seed=seeds, seed_min=min(seeds), seed_max=max(seeds))


def _no_benchmarks(m):
    card = m["eng"]["card"]
    card["benchmarks"] = []
    for f in card["fitted"].values():
        f["benchmarks"] = []
    _chosen(m).update(benchmarks=0, benchmarks_reported=[])


def _list_disagrees(m):
    _chosen(m)["benchmarks_not_in_band"].append({"id": "B1", "role": "fit", "status": "out_of_band"})


def _unbound(m, named=False):
    m["bound"] = False
    if named:
        m["spec"]["engine"] = {"ref": ENGINE_REF, "config": {}}


def _choice_edited(m):
    m["choice_after"] = lambda doc: doc.update(at=_at(240))


def _outside_card(m):
    m["eng"]["spec_values"]["p2"].update(card_range=[1.0, 5.0], outside_card_range=True, outside="range")
    next(p for p in m["eng"]["parameters"] if p["name"] == "p2")["hi"] = 5.0
    m["eng"]["card"]["fitted"]["p2"]["hi"] = 5.0
    m["spec"]["engine_parameters"]["p2"]["outside_card_range"] = True
    m["high"] = ["p2"]


def _flag_contradicts(m):
    m["eng"]["spec_values"]["p2"].update(range=[1.0, 7.0], uncertainty="drawn over [1, 7] (uniform)")


def _unsourced(m):
    by = {p["name"]: p for p in m["eng"]["parameters"]}
    by["b_add"]["verified"] = "unverified"
    by[HELD].update(lo=None, hi=None)


def _fitted_on_bound(m):
    m["eng"]["card"]["fitted"]["f1"]["value"] = 0.6
    next(p for p in m["eng"]["parameters"] if p["name"] == "f1")["value"] = 0.6


def _b1(**change):
    return lambda m: _bench(m, "B1").update(change)


def _share(tid, share):
    def edit(m):
        t = next(t for t in m["dt"]["tests"] if t["test_id"] == tid)
        t.update(share_pct=share, statement=f"This test can move the plan: {share:g}% of units change package, under "
                                            f"the {t['threshold_pct']:g}% threshold.")
    return edit


# ---- the Figures view's edits (§5.12)

FIG_ROLE = {"standard": {"decision": "frontier", "other": "parameters", "record": "calibration_fit",
                         "stale": ("headline", "analysis/point.yaml"), "plain": "data_coverage"},
            "estimates": {"decision": "estimates_q1", "other": "diagnostics_ppc", "record": "diagnostics_rt",
                          "stale": ("estimates_q1", EST), "plain": "diagnostics_ppc"}}


def _role(m, role):
    return FIG_ROLE[m["fig"]["set"]][role]


def _fig_stale(m):
    name, source = _role(m, "stale")
    m["fig"]["stale"][name] = source


def _fig_absent(m, named=False):
    m["fig"]["not_produced"]["trajectories"] = NO_TRAJECTORIES
    if named:
        m["fig"]["report"].append("figures/trajectories.png")


def _fig_unused(m):
    m["fig"]["report"].remove(f"figures/{_role(m, 'decision')}.png")


def _fig_set(key, role):
    return lambda m: m["fig"].update({key: (_role(m, role),)})


def _fig_producer(m):
    m["fig"]["producer"] = "toolkit/analysis/other.py"


def _fig_files(role, *ends):
    """The files of one figure, by its role in the figure set of the build (FIG_ROLE)."""
    def files(method, figures):
        name = FIG_ROLE[figures][role]
        return [f"figures/{name if isinstance(name, str) else name[0]}{end}" for end in ends]
    return files


HM, BOTH, POST = ("history_matching",), METHODS, ("posterior",)
SIGNED = SPEC_FILES + [JC, DT]
DECLARED = SPEC_FILES + [CHOICE, ENGINE_REC, LEDGER, SCOPE, GATES[1]]     # what the `declared` variant itself changes
DEPARTED = SPEC_FILES + [CHOICE]                                           # what a signed departure itself changes
UNIT_FILES = [JC, "figures/calibration_fit.csv", "figures/calibration_residuals.csv", "figures/calibration_unit_fit.csv"]
# Defect name -> view (the view whose check moves; none: the Fit view), case (scorecard, or None), check (the one
# check of that view that moves), status (what it moves to), also (any other check of that view the same edit must
# move, by rule), unpinned (checks of that view whose status the edit leaves open), figure_sets (the figure sets a
# Figures edit is defined for),
# edit (exactly what is changed, in one sentence), files (the files the edit itself touches, per method; files that
# record a hash of those follow, and so does the last gate record when a critic's finding changes its status), methods, and the function that applies it. `status: ok` marks a variant kept to
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
    # The science register (§5.8, §6): `view` names the view whose check moves (the entries above are the Fit view's).
    # `follows: False` marks an edit made after the run finished, which no recorded hash follows.
    "C12_open_high": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": _left_open("D-001", blocking=True), "files": [LEDGER, GATES[1]], "also": {"decides_unknown": "look"},
        "edit": "In findings.jsonl the critic's finding D-001 (HIGH) is left with status open and blocking true, its "
                "last event open, with no decision test and no declaration."},
    "C12_declared_tested": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _c12_declared, "files": [LEDGER, SCOPE, GATES[1]],
        "edit": "The same finding D-001 (HIGH, blocking) has status declared with decision test DT1, which ran with "
                "verdict NO_CHANGE, and scope_declaration.yaml declares it by its id with that test."},
    "test_unlinked": {
        "view": "register", "case": None, "check": "tests_unlinked", "status": "look", "methods": BOTH,
        "apply": lambda m: _set_test(m, "DT1", changes=True), "files": [DT], "also": {"undisposed": "look"},
        "edit": "analysis/decision_tests.yaml records DT1, a test no finding, declaration or dossier entry names, with "
                "verdict CHANGES_PLAN (25% of units against the 10% threshold)."},
    # §9: the clean run holds nothing declared, so every declared case is a variant; this one holds one of each
    "declared": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _declared, "files": DECLARED,
        "edit": "The run holds one of each declaration, each naming a decision test that ran with verdict NO_CHANGE: "
                "benchmark B2 is out of band (0.78 against 0.36 to 0.7) under a signed engine departure naming DT4, "
                "component B has a signed departure naming DT5, the signed spec declares the calibration miss "
                "grp_r1:G1 with DT6 (history matching), and the ledger holds finding R-001 as declared with DT3, "
                "which scope_declaration.yaml declares by its id."},
    "declared_test_not_run": {
        "view": "register", "case": None, "check": "declared_untested", "status": "problem", "methods": BOTH,
        "apply": _on_declared(lambda m: _set_test(m, "DT4", ran=False)), "files": DECLARED + [DT],
        "edit": "On the run of variant declared, analysis/decision_tests.yaml records DT4, the test the signed engine "
                "departure for benchmark B2 names, with status not_run and verdict NOT_RUN."},
    "declared_no_test": {
        "view": "register", "case": None, "check": "declared_untested", "status": "look", "methods": BOTH,
        "apply": _on_declared(_departure_no_test), "files": DECLARED, "also": {"decides_unknown": "look"},
        "edit": "On the run of variant declared, the signed spec's engine departure for component B, and its copy in "
                "engine_choice.json, have no decision_test key."},
    "open_no_test": {
        "view": "register", "case": None, "check": "decides_unknown", "status": "look", "methods": BOTH,
        "apply": _left_open("D-002"), "files": [LEDGER, GATES[1]], "also": {"undisposed": "look"},
        "edit": "In findings.jsonl the critic's finding D-002 (MEDIUM, not blocking) is left with status open and no "
                "decision test."},
    "process_open": {
        "view": "register", "case": None, "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _stale_open, "files": [LEDGER, "analysis/point.yaml"],
        "edit": "In findings.jsonl the finding a harness check raised about a run file (analysis_contract_stale for "
                "analysis/point.yaml and its input analysis/calibration.yaml, source rigor-artifacts, HIGH) is left "
                "with status open and blocking true, and analysis/point.yaml records another hash for that input "
                "than the file has, so the Steps check on recorded inputs fires on the same run."},
    "implemented_unverified": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "look", "methods": BOTH,
        "apply": lambda m: m.update(unjudged=("D-002",)), "files": [GATES[1]],
        "edit": "gate_r2.json, the last gate record, gives the done-when test of finding D-002 (MEDIUM), which the "
                "ledger holds as fixed, as not judged (passes null) instead of passed."},
    "sheet_high": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": lambda m: m.update(high=["p2"]), "files": [SHEET],
        "edit": "decision_spec.signoff_sheet.md gives a card range that the spec's range of p2 reaches outside and "
                "marks that line HIGH, and the sign-off record holds no hash for the sheet (toolkit/spec/signoff.py "
                "sign() records none), so nothing binds the line to the signature."},
    "implemented_no_gate": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": lambda m: m.update(gates=False), "files": list(GATES),
        "edit": "The run has no gate record (gate_r1.json and gate_r2.json are not in the run folder), while the "
                "ledger holds three findings as fixed, one of them HIGH."},
    "declared_only_on_disk": {
        "view": "register", "case": None, "check": "declared_untested", "status": "ok", "methods": BOTH,
        "apply": _disk_only, "files": ["decision_spec.yaml"], "follows": False,
        "edit": "After the sign-off, decision_spec.yaml on disk gains an uncertainty.excluded entry for parameter q7 "
                "that the signed copy, and the hash in the sign-off record, do not hold."},
    "merge_shared_test": {
        "view": "register", "case": None, "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _on_declared(_merge_test), "files": DECLARED + [DOSSIER],
        "edit": "On the run of variant declared, evidence_dossier.yaml gains a no-data entry unit_attribute:x2 whose "
                "decision_test is DT3, the test the declared finding R-001 already names."},
    "merge_excluded_nodata": {
        "view": "register", "case": None, "check": "declared_untested", "status": "ok", "methods": BOTH,
        "apply": _merge_input, "files": SPEC_FILES + [DOSSIER],
        "edit": "The signed spec's uncertainty.excluded gains parameter q4 and evidence_dossier.yaml gains a no-data "
                "entry unit_attribute:q4 whose decision_test is DT1, which ran with verdict NO_CHANGE."},
    "signed_by_lead": {
        "view": "register", "case": None, "check": "undisposed", "status": "look", "methods": BOTH,
        "apply": _lead, "files": DECLARED + ["decision_spec.signoff.json"],
        "edit": "The run of variant declared, signed by lead instead of the owner: decision_spec.signoff.json records "
                "signed_by lead, and engine_choice.json, as the producer writes it under a lead's signature, marks "
                "both engine departures unsigned."},
    "older_layout": {
        "view": "register", "case": None, "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _older, "files": [LEDGER, OLD_DECLARED, OLD_GATE, GATES[1]],
        "also": {"declared_untested": "look", "decides_unknown": "look"},
        "edit": "The run has an older layout: findings.jsonl holds D-002 and R-001 as open with no decision test, "
                "there is no scope_declaration.yaml, declared_findings.yaml lists R-001 as declared and "
                "final_gate.json lists D-002 under resolved_by_test."},
    "prose_only": {
        "view": "register", "case": None, "check": "prose_only", "status": "look", "methods": BOTH,
        "apply": _prose, "files": SPEC_FILES,
        "edit": "Both copies of the spec gain four comment lines: one starts a DEPARTURE, one a LIMITATION, one "
                "starts an ASSUMPTION in the middle of the line, and one only mentions a departure."},
    # §9, the third fixture: inputs that contradict each other or miss a piece; no check may answer ok or look
    "findings_bad_line": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": _bad_line, "files": [LEDGER],
        "edit": "findings.jsonl gains a last line that is a row cut off in the middle, which is not valid JSON."},
    "finding_unknown_status": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "not_available", "methods": BOTH,
        "apply": _odd_status, "files": [LEDGER, GATES[1]],
        "edit": "In findings.jsonl finding D-002 has the status resolved, a word the ledger does not write."},
    "scope_names_no_finding": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": _ghost, "files": [SCOPE],
        "edit": "scope_declaration.yaml gains a declaration whose blocker_id X-404 is the id of no finding in "
                "findings.jsonl, with decision test DT1."},
    "decision_tests_missing": {
        "view": "register", "case": None, "check": "declared_untested", "status": "not_available", "methods": BOTH,
        "apply": _no_tests_file, "files": [DT] + [f"figures/decision_tests{e}" for e in (".png", ".svg", ".csv")],
        "also": {"tests_unlinked": "not_available", "decides_unknown": "not_available"},
        "edit": "analysis/decision_tests.yaml is not in the run folder, and the manifest does not list it."},
    # The Model view (§5.4). The clean run's engine record has four benchmarks in band at the centre of their bands;
    # B4 is reported by the engine choice and does not gate; nothing is declared, so each departure is a variant.
    "C4_out_of_band": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "problem", "methods": BOTH,
        "apply": _moved("B3", 1.12), "files": [ENGINE_REC], "also": {"choice_text": "problem"},
        "edit": "In analysis/engine.yaml benchmark B3 (held out) has computed 1.12 against its band 0.64 to 1.0, with "
                "status out_of_band and in_band false, while no departure names it and engine_choice.json lists it "
                "neither as reported only nor among its benchmarks not in band."},
    "C4_out_of_band_departure": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _b2_out, "files": DEPARTED + [ENGINE_REC], "also": {"departures": "look"},
        "edit": "In analysis/engine.yaml benchmark B2 (held out) has computed 0.78 against its band 0.36 to 0.7, with "
                "status out_of_band; the signed spec gains a departure for B2 naming decision test DT4, which ran "
                "with verdict NO_CHANGE, and engine_choice.json records it as signed with the benchmark's status."},
    "C4_out_of_band_departure_not_run": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "problem", "methods": BOTH,
        "apply": lambda m: _b2_out(m, ran=False), "files": DEPARTED + [ENGINE_REC, DT], "also": {"departures": "problem"},
        "edit": "Benchmark B2 is out of band as in C4_out_of_band_departure, and analysis/decision_tests.yaml records "
                "DT4, the test its signed departure names, with status not_run and verdict NOT_RUN."},
    "C4_out_of_band_departure_unsigned": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "problem", "methods": BOTH,
        "apply": _b3_out_disk, "files": [ENGINE_REC, "decision_spec.yaml"], "also": {"choice_text": "problem"},
        "edit": "Benchmark B3 is out of band as in C4_out_of_band, and after the sign-off decision_spec.yaml on disk "
                "gains an engine departure for B3 naming DT1 that the signed copy does not hold."},
    "C4_not_gating_out_of_band": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _moved("B4", 1.5), "files": [ENGINE_REC, CHOICE],
        "edit": "Benchmark B4, which engine_choice.json lists under benchmarks_reported (it does not gate), has "
                "computed 1.5 against its band 2.0 to 72.0, with status out_of_band in the engine record and in "
                "the reported row."},
    "C4_near_edge": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _moved("B1", 0.84), "files": [ENGINE_REC],
        "edit": "Benchmark B1 has computed 0.84 in its band 0.4 to 0.9: in band, within a quarter of the band's log "
                "width of the upper edge, with every seed still inside the band."},
    "C4_unreachable": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _moved("B4", 95.0, status="band_mis_based", mis_based_reason=MIS_BASED[1], reach_in_fitted_range=[80.0, 110.0]),
        "files": [ENGINE_REC, CHOICE],
        "edit": "Benchmark B4, reported only, has computed 95.0 above its band 2.0 to 72.0 with status band_mis_based "
                "and mis_based_reason unreachable_in_fitted_range (reach 80.0 to 110.0), in the engine record and in "
                "the reported row."},
    "no_benchmarks": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _no_benchmarks, "files": [ENGINE_REC, CHOICE],
        "edit": "The engine record's data.card.benchmarks is an empty list (no fitted parameter names a benchmark), "
                "and engine_choice.json counts 0 benchmarks for the engine and reports none."},
    "choice_list_disagrees": {
        "view": "model", "case": None, "check": "choice_text", "status": "problem", "methods": BOTH,
        "apply": _list_disagrees, "files": [CHOICE],
        "edit": "In engine_choice.json the chosen engine's benchmarks_not_in_band lists B1 as out_of_band, while the "
                "engine record has B1 in band."},
    "choice_unbound_named": {
        "view": "model", "case": None, "check": "choice_bound", "status": "ok", "methods": BOTH,
        "apply": lambda m: _unbound(m, named=True), "files": SPEC_FILES,
        "edit": "The signed spec names the engine by its reference instead of leaving it to the engine choice, and "
                "engine_choice_sha256 is null in every analysis header and in the manifest, as the harness writes "
                "it for a named engine."},
    "choice_unbound_open": {
        "view": "model", "case": None, "check": "choice_bound", "status": "look", "methods": BOTH,
        "apply": _unbound, "files": lambda method, figures: artifacts(method),
        "edit": "engine_choice_sha256 is null in every analysis header and in the manifest, while the signed spec "
                "says engine: auto and so leaves the engine to the engine choice."},
    "choice_hash_mismatch": {
        "view": "model", "case": None, "check": "choice_bound", "status": "problem", "methods": BOTH,
        "apply": _choice_edited, "files": [CHOICE], "follows": False,
        "edit": "engine_choice.json is rewritten after the analysis (another time in its `at`), so the hash the "
                "analysis headers hold for it is no longer the file's."},
    "spec_value_outside_card": {
        "view": "model", "case": None, "check": "spec_values", "status": "look", "methods": BOTH,
        "apply": _outside_card, "files": SPEC_FILES + [ENGINE_REC, SHEET],
        "edit": "The card's range for p2 is 1.0 to 5.0, so the spec's range 1.0 to 5.5 reaches outside it: the "
                "engine record's spec_values.p2 has outside_card_range true and outside range, the signed spec "
                "sets outside_card_range true for p2, and the sign-off sheet marks the line HIGH."},
    "spec_value_flag_contradicts": {
        "view": "model", "case": None, "check": "spec_values", "status": "problem", "methods": BOTH,
        "apply": _flag_contradicts, "files": [ENGINE_REC],
        "edit": "The engine record's spec_values.p2 has range 1.0 to 7.0 against card_range 1.0 to 6.0 while its "
                "outside_card_range stays false and its outside stays null."},
    "seed_range_crosses_edge": {
        "view": "model", "case": None, "check": "seed_range", "status": "look", "methods": BOTH,
        "apply": _seed_cross, "files": [ENGINE_REC],
        "edit": "Benchmark B1 keeps computed 0.6 at the centre of its band 0.4 to 0.9 and status in_band, with seeds "
                "from 0.5 to 0.93, so its seed range crosses the upper edge."},
    "parameters_unverified": {
        "view": "model", "case": None, "check": "parameters_sourced", "status": "look", "methods": BOTH,
        "apply": _unsourced, "files": [ENGINE_REC],
        "edit": "In the engine record's parameters, b_add is marked unverified and c1 has no range (lo and hi null)."},
    "fitted_on_bound": {
        "view": "model", "case": None, "check": "fitted_at_bound", "status": "look", "methods": BOTH,
        "apply": _fitted_on_bound, "files": [ENGINE_REC],
        "edit": "The fitted parameter f1 has the value 0.6, the upper end of its fitted range 0.3 to 0.6, in "
                "data.card.fitted and in data.parameters."},
    "departure_component": {
        "view": "model", "case": None, "check": "departures", "status": "look", "methods": BOTH,
        "apply": lambda m: _depart(m, "B"), "files": DEPARTED,
        "edit": "The signed spec gains an engine departure for component B, which no benchmark covers, naming "
                "decision test DT5, which ran with verdict NO_CHANGE and no unit changing package; engine_choice.json "
                "records it as signed with benchmark_status no benchmark."},
    "departure_near_miss": {
        "view": "model", "case": None, "check": "departures", "status": "look", "methods": BOTH,
        "apply": lambda m: (_b2_out(m), _share("DT4", 9.0)(m)), "files": DEPARTED + [ENGINE_REC, DT],
        "also": {"benchmarks": "look"},
        "edit": "Benchmark B2 is out of band under its signed departure as in C4_out_of_band_departure, and "
                "analysis/decision_tests.yaml records DT4, the test the departure names, with 9% of units changing "
                "package against its 10% threshold: verdict NO_CHANGE, within 25% of the threshold."},
    "spec_benchmarks_recomputed": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "ok", "methods": BOTH,
        "apply": lambda m: m.update(bsv=_at_spec_values()), "files": [BSV, BSV_REC, DT],
        "edit": "The run also holds analysis/benchmarks_at_spec_values.yaml and its record file: benchmark B2, which "
                "decision test DT4 reads, recomputed at the spec's values (0.51, in band), listed as an input of the "
                "decision tests and, having no artifact header, left outside the manifest."},
    # §9, the third fixture for the Model view
    "departure_test_not_run": {
        "view": "model", "case": None, "check": "departures", "status": "problem", "methods": BOTH,
        "apply": lambda m: (_depart(m, "B"), _set_test(m, "DT5", ran=False)), "files": DEPARTED + [DT],
        "edit": "The signed spec holds the departure for component B as in departure_component, and "
                "analysis/decision_tests.yaml records DT5, the test it names, with status not_run and verdict "
                "NOT_RUN, while every benchmark is in band."},
    "benchmark_unknown_status": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "not_available", "methods": BOTH,
        "apply": _b1(status="passed"), "files": [ENGINE_REC], "also": {"choice_text": "not_available"},
        "edit": "Benchmark B1 has the status passed, a word toolkit/engines/benchmark.py does not write."},
    "benchmark_no_band": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "not_available", "methods": BOTH,
        "apply": _b1(band=None, target=None), "files": [ENGINE_REC],
        "edit": "Benchmark B1 has band null and target null while its status stays in_band, so its place in the "
                "band cannot be computed."},
    "benchmark_status_contradicts": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "problem", "methods": BOTH,
        "apply": _b1(computed=0.95), "files": [ENGINE_REC],
        "edit": "Benchmark B1 has computed 0.95, above its band 0.4 to 0.9, while its status stays in_band and its "
                "in_band stays true."},
    "engine_record_unparsable": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "not_available", "methods": BOTH,
        "apply": lambda m: m.update(cut=True), "files": [ENGINE_REC], "unpinned": ("choice_bound",),
        "also": {k: "not_available" for k in ("choice_text", "spec_values", "seed_range", "parameters_sourced", "fitted_at_bound")},
        "edit": "analysis/engine.yaml stops inside its benchmark table, in the middle of a list that is never "
                "closed, so it is not valid YAML."},
    # The Figures view (§5.12). `figure_sets` names the producers of analysis/figures.yaml an edit is defined for.
    "figure_stale_source": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "problem", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_stale, "files": _fig_files("stale", ".png.provenance.json"),
        "edit": "The provenance record of one decision figure (headline; estimates_q1 in the estimates set) holds "
                "another hash for one of its sources than the file now has."},
    "figure_spec_redrafted": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "ok", "methods": BOTH,
        "apply": _disk_only, "files": ["decision_spec.yaml"], "follows": False,
        "edit": "After the sign-off, decision_spec.yaml on disk gains an entry the signed copy does not hold, so "
                "the hash every provenance record holds for the spec is the signed copy's and not the file's."},
    "figure_not_produced": {
        "view": "figures", "case": None, "check": "figure_missing", "status": "look", "methods": BOTH,
        "apply": _fig_absent,
        "files": [FIGS] + [f"figures/trajectories{e}" for e in (".png", ".svg", ".csv", ".png.provenance.json")],
        "edit": "analysis/figures.yaml lists trajectories under not_produced with the producer's reason (it needs "
                "trajectory runs), and the figure's image, table and provenance record are not in the run folder."},
    "figure_not_produced_named": {
        "view": "figures", "case": None, "check": "figure_missing", "status": "problem", "methods": BOTH,
        "apply": lambda m: _fig_absent(m, named=True),
        "files": [FIGS, REPORT] + [f"figures/trajectories{e}" for e in (".png", ".svg", ".csv", ".png.provenance.json")],
        "edit": "trajectories is not produced as in figure_not_produced, and report.md names it by its path "
                "figures/trajectories.png."},
    "figure_unused": {
        "view": "figures", "case": None, "check": "figure_unused", "status": "look", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_unused, "files": [REPORT],
        "edit": "report.md no longer names one produced decision figure by its path (frontier; estimates_q1 in the "
                "estimates set), and no other text names it."},
    "figure_image_missing": {
        "view": "figures", "case": None, "check": "figure_missing", "status": "problem", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("dropped", "other"), "files": _fig_files("other", ".png"),
        "follows": False, "also": {"figure_in_manifest": "problem"},
        "edit": "The image of one figure the index lists as produced (parameters; diagnostics_ppc in the estimates "
                "set) is not in the run folder, while the index and the manifest still list it."},
    "figure_outside_manifest": {
        "view": "figures", "case": None, "check": "figure_in_manifest", "status": "problem", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("unlisted", "other"), "files": ["analysis/manifest.json"],
        "edit": "analysis/manifest.json does not list the image of one produced figure (parameters; diagnostics_ppc "
                "in the estimates set) among its deliverables."},
    "figure_manifest_hash": {
        "view": "figures", "case": None, "check": "figure_in_manifest", "status": "problem", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("redrawn", "other"), "files": _fig_files("other", ".png"),
        "follows": False,
        "edit": "The image of one produced figure (parameters; diagnostics_ppc in the estimates set) is replaced "
                "after the manifest was written, so the manifest's hash for it is not the file's."},
    # §9, the third fixture for the Figures view
    "figure_record_missing": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "not_available", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("no_record", "record"),
        "files": _fig_files("record", ".png.provenance.json"),
        "edit": "One produced figure (calibration_fit; diagnostics_rt in the estimates set) has no provenance "
                "record in the run folder."},
    "figure_record_malformed": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "not_available", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("bad_record", "record"),
        "files": _fig_files("record", ".png.provenance.json"),
        "edit": "The provenance record of one produced figure (calibration_fit; diagnostics_rt in the estimates "
                "set) is cut off in the middle and is not valid JSON."},
    "figures_unknown_producer": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "not_available", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_producer, "files": [FIGS],
        "also": {k: "not_available" for k in ("figure_missing", "figure_unused", "figure_in_manifest")},
        "edit": "analysis/figures.yaml names toolkit/analysis/other.py as its producer, which is neither of the "
                "two producers of that file."},
    "figure_no_path": {
        "view": "figures", "case": None, "check": "figure_missing", "status": "not_available", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("no_path", "plain"), "files": [FIGS],
        "also": {"figure_fresh": "not_available", "figure_in_manifest": "not_available"},
        "edit": "In analysis/figures.yaml the entry of one produced figure outside the decision tier (data_coverage; "
                "diagnostics_ppc in the estimates set) has no path key."},
}


REGISTER_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "register")
MODEL_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "model")
FIGURES_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "figures")


def artifacts(method):
    """The analysis files that carry an artifact header, for a method (standard figure set)."""
    return ([JC, CAL] if method == "history_matching" else [CAL, EST]) + [ENGINE_REC, "analysis/point.yaml", UNC, DT, FIGS]


def figure_sets(defect):
    """The figure sets an edit is defined for: those it names, else the standard set."""
    return DEFECTS[defect].get("figure_sets", FIGURE_SETS[:1])


def touched(defect, method, figures="standard"):
    """The files a defect's edit itself changes for a method and a figure set."""
    f = DEFECTS[defect]["files"]
    return list(f(method, figures) if callable(f) else f[method] if isinstance(f, dict) else f)


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


def _spec_text(spec, comments):
    """The spec as YAML; comment lines go in front of its `calibration` key, where a stored spec keeps most of them."""
    text = _yaml(spec)
    return text.replace("\ncalibration:", "\n" + "\n".join(comments) + "\ncalibration:", 1) if comments else text


def _register_files(m):
    """findings.jsonl as the ledger saves it (open rows first, then by severity and id; keys sorted), the scope
    declaration, and the two files an older run keeps dispositions in."""
    rows = sorted(m["findings"].values(), key=lambda f: (f["status"] != "open", SEVERITY_ORDER[f["severity"]], f["id"]))
    out = {LEDGER: "".join(json.dumps(f, sort_keys=True) + "\n" for f in rows) + "".join(x + "\n" for x in m["ledger_extra"])}
    if m["scope"] is not None:
        out[SCOPE] = _yaml(m["scope"])
    if m["gates"]:
        out.update({rel: _gate(m, n) for n, rel in enumerate(GATES, 1)})
    if m["old"]:
        named = [m["findings"][i] for i in m["old"]["declared"]]
        why = (f"{len(named)} blocker(s) unresolved, rounds exhausted. Must write scope_declaration.yaml acknowledging "
               "each blocker by id.")
        out[OLD_DECLARED] = _yaml({"rule": 3, "rationale": why, "declared": [
            {k: f[k] for k in ("id", "severity", "source", "claim")} for f in named]})
        out[OLD_GATE] = json.dumps({
            "unresolved_high": [], "accept_blockers": [], "structural_mismatch": False, "structural_reviewers": [],
            "spec_violations": [], "registry_violations": [], "rigor_violations": [], "blocker_attempts": {},
            "escalation_required": True, "rounds_remaining": 0, "action": "DECLARE_SCOPE", "rule_matched": 3,
            "rationale": why, "declared": [], "declared_prev": [],
            "done_when_status": {i: {"passes": True, "detail": "pass manuscript.md exists; pass manuscript.md: found 'limit'",
                                     "mechanical": True, "frozen_round": 1} for i in m["old"]["tested"]},
            "resolved_by_test": list(m["old"]["tested"]), "post_write": True}, indent=1)
    return out


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
        return hashlib.sha256(_raw(files[rel])).hexdigest()

    def artifact(rel, kind, producer, inputs, data, minute, stale=False):
        earlier = hashlib.sha256(b"the input as it was when this file was computed").hexdigest()
        files[rel] = _yaml({"schema": f"analysis.{kind}/1", "produced_by": "harness", "producer": producer,
                            "spec_sha256": sha("decision_spec.yaml"), "spec_signed": True, "engine": ENGINE,
                            "engine_choice_sha256": sha(CHOICE) if m["bound"] else None,
                            "inputs": {**{i: earlier if stale else sha(i) for i in inputs}, **HARNESS_INPUT},
                            "written_at": _at(minute), "data": data})

    # as signed; an edit made to the file on disk after the sign-off (m["disk"]) replaces it once every hash is taken
    files["decision_spec.yaml"] = _spec_text(m["spec"], m["comments"])
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
    files[SHEET] = _sheet_text(m, sha("decision_spec.yaml"))
    files[DOSSIER] = _yaml(m["dossier"])
    # The record holds what toolkit/spec/signoff.py sign() lists under sha256 (`{SPEC: sha, **extra}`, extra being
    # the derivation record, a reference file and the evidence files): here the spec and the dossier. The sheet's hash
    # is not there: sign() compares it with the one the owner was shown (expect_sheet_sha256) and records none.
    files["decision_spec.signoff.json"] = json.dumps(
        {"signed_by": m["signed_by"], "signed_at": _at(50), "note": m["signoff_note"],
         "sha256": {"decision_spec.yaml": sha("decision_spec.yaml"), DOSSIER: sha(DOSSIER)},
         "producer": "toolkit/spec/signoff.py"}, indent=1)
    files[CHOICE] = json.dumps(m["choice"], indent=1)
    files[LABELS] = _yaml({"labels": {"units": {u: f"unit {u}" for u in UNITS[:2]}}, "theme": "light"})
    files.update(_register_files(m))
    data_in = ["decision_spec.yaml", "data/derived/units.csv", "data/derived/targets.csv"]
    artifact(ENGINE_REC, "engine", "toolkit/analysis/run.py", ["decision_spec.yaml"], m["eng"], 95)
    if m["cut"]:       # the file stops inside the benchmark table, in a list that is never closed
        files[ENGINE_REC] = files[ENGINE_REC][:files[ENGINE_REC].index("    benchmarks:\n")] + "    benchmarks: [{id: B1,\n"
    if m["bsv"]:
        files[BSV] = _yaml(m["bsv"])
        files[BSV_REC] = json.dumps({
            "benchmarks": m["bsv"]["arm_engines"], "config_sha256": "no-config", "engine_identity": _identity(), "file": BSV,
            "pack_sha256": hashlib.sha256(b"toy pack").hexdigest(),
            "producer": "toolkit/spec/engine_parameters.py:write_spec_benchmarks", "sha256": sha(BSV),
            "spec_parameters": m["bsv"]["spec_parameters"], "spec_sha256": sha("decision_spec.yaml"), "written_at": _at(85)},
            indent=1, sort_keys=True)
    if method == "history_matching":
        artifact(JC, "joint_calibration", "toolkit/analysis/joint_calibration.py", data_in, m["jc"], 100)
        artifact(CAL, "calibration", "toolkit/analysis/calibration.py", ["data/derived/units.csv", JC],
                 _companion(m["jc"]), 110)
    else:
        artifact(CAL, "calibration", "toolkit/analysis/calibration.py", data_in[1:], m["cal"], 110)
    files.update(_figures(m))
    artifact("analysis/point.yaml", "point", "toolkit/analysis/point.py", [CAL],
             {"n_units": len(UNITS), "mix": {"A": 7, "A+B": 5}}, 150, stale=m["stale"])
    artifact(UNC, "uncertainty", "toolkit/analysis/uncertainty.py", ["analysis/point.yaml"], m["unc"], 200)
    if m["dt"] is not None:
        artifact(DT, "decision_tests", "toolkit/analysis/decision_tests.py", ["analysis/point.yaml", UNC] + [BSV] * bool(m["bsv"]), m["dt"], 230)
    if "est" in m:
        artifact(EST, "estimates", "toolkit/analysis/estimates.py", [CAL], m["est"], 232)
    files["allocation.csv"] = _csv(["unit_id", "package", "outcome_averted"],
                                   [[u, "A" if i % 12 < 7 else "A+B", 40 + 3 * i] for i, u in enumerate(UNITS)])
    files[RESULTS] = "# Results (synthetic fixture)\n\n- units: 12\n- packages: A, A+B\n"
    images = _figure_files(m, files, sha, artifact)
    arts = [r for r in files if r.startswith("analysis/") and r.endswith(".yaml") and r != BSV]
    files["analysis/manifest.json"] = json.dumps({
        "artifacts": {r: {"kind": r[9:-5], "producer": f"toolkit/analysis/{r[9:-5]}.py", "sha256": sha(r)} for r in arts},
        "complete": True,
        "deliverables": {"allocation.csv": {"producer": "toolkit/analysis/run.py", "role": "allocation",
                                            "sha256": sha("allocation.csv")},
                         **images,
                         RESULTS: {"producer": "toolkit/analysis/claims.py", "role": "results", "sha256": sha(RESULTS)}},
        "engine": ENGINE, "engine_choice_sha256": sha(CHOICE) if m["bound"] else None,
        "expected": {"decision_tests": {t["test_id"]: t["path"] for t in (m["dt"] or {"tests": []})["tests"]},
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
    files.update(_report_files(m))
    # what happens after the run wrote its hashes: an image removed or replaced, the engine choice rewritten, the spec
    # on disk edited
    for name in m["fig"]["dropped"]:
        del files[f"figures/{name}.png"]
    for name in m["fig"]["redrawn"]:
        files[f"figures/{name}.png"] = _png(f"{name}, drawn again")
    if m["choice_after"]:
        doc = copy.deepcopy(m["choice"])
        m["choice_after"](doc)
        files[CHOICE] = json.dumps(doc, indent=1)
    if m["disk"]:
        disk = copy.deepcopy(m["spec"])
        m["disk"](disk)
        files["decision_spec.yaml"] = _spec_text(disk, m["comments"])
    keep = STAGES[:STAGES.index(stage) + 1]
    return {r: t for r, t in files.items() if stage_of(r) in keep}


def _raw(content):
    return content if isinstance(content, bytes) else content.encode("utf-8")


def build(dir, upto=None, defect=None, method="history_matching", figures="standard"):
    """Write one fixture run into `dir` (created if missing). Returns {path: sha256} of the files written. Each file is
    written with lib.safe_io.write_nofollow below `dir`: a link at any name under it is refused, never followed.
    `figures` names the producer whose shape analysis/figures.yaml has (FIGURE_SETS): `standard` is
    toolkit/analysis/figures.py; `estimates` is toolkit/analysis/estimates.py, for the posterior method only (it
    draws from analysis/estimates.yaml)."""
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; one of {', '.join(METHODS)}")
    if figures not in FIGURE_SETS:
        raise ValueError(f"unknown figure set {figures!r}; one of {', '.join(FIGURE_SETS)}")
    if figures == "estimates" and method != "posterior":
        raise ValueError("the estimates figure set is drawn from analysis/estimates.yaml, which only the posterior method writes")
    stage = "final" if upto is None else upto
    if stage not in STAGES:
        raise ValueError(f"unknown stage {upto!r}; one of {', '.join(STAGES)}")
    m = _model(method, figures)
    if defect is not None:
        if defect not in DEFECTS:
            raise ValueError(f"unknown defect {defect!r}; see --list")
        if method not in DEFECTS[defect]["methods"]:
            raise ValueError(f"defect {defect} is not defined for method {method}")
        if figures != "standard" and figures not in figure_sets(defect):
            raise ValueError(f"defect {defect} is not defined for the {figures} figure set")
        DEFECTS[defect]["apply"](m)
    out = {}
    os.makedirs(dir, exist_ok=True)
    for rel, content in _render(m, stage).items():
        write_nofollow(dir, rel, _raw(content))
        out[rel] = hashlib.sha256(_raw(content)).hexdigest()
    return out


# ------------------------------------------------------------------------------------------- self-test

def domain_words():
    with open(os.path.join(HERE, "domain_words.txt"), encoding="utf-8") as f:
        return [w.strip().lower() for w in f if w.strip() and not w.startswith("#")]


def _headers(dir, shas):
    """Each analysis file's header, parsed from the file on disk: {path: (spec_sha256, {input: sha})}."""
    out = {}
    for rel in shas:
        if rel.startswith("analysis/") and rel.endswith(".yaml") and rel != BSV:     # BSV has no artifact header
            with open(os.path.join(dir, rel), encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            out[rel] = (doc["spec_sha256"], doc["inputs"], doc)
    return out


def _follows(clean_dir, clean, first):
    """`first` plus every file that records a hash of a changed file, found from the clean run's own headers."""
    changed, heads = set(first), _headers(clean_dir, clean)
    signed = SPEC_FILES[1] in changed       # the spec as signed changed: every file that records its hash follows
    records = {}                            # each figure's provenance record: the files whose hashes it holds
    for rel in clean:
        if rel.endswith(".provenance.json"):
            with open(os.path.join(clean_dir, rel), encoding="utf-8") as f:
                doc = json.load(f)
            records[rel] = set(doc["source_hashes"]) | {e["path"] for e in doc["presentation"]}
    while True:
        more = {r for r, (_, inputs, _) in heads.items()
                if signed or CHOICE in changed or any(i in changed for i in inputs if i != "decision_spec.yaml")}
        more |= {r for r, sources in records.items() if signed or (sources - {"decision_spec.yaml"}) & changed}
        if (signed or BSV in changed) and BSV_REC in clean:
            more.add(BSV_REC)
        if REPORT in changed:
            more.add(RENDER)
        if CHOICE in changed or any(r.endswith(".png") and "overview_" not in r for r in changed):
            more.add("analysis/manifest.json")
        if signed:
            more |= {"decision_spec.signoff.json", SHEET}
        if DOSSIER in changed | more:                    # the sign-off record holds its hash
            more.add("decision_spec.signoff.json")
        if any((r.startswith("analysis/") and r not in (BSV, BSV_REC)) or r in ("allocation.csv", RESULTS) for r in changed | more):
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


def _ledger_words():
    """(the fields of a ledger row, its status words), read from the producer's source without importing it."""
    import ast
    with open(os.path.join(REPO, "agents", "findings_ledger.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fields = next([s.target.id for s in n.body if isinstance(s, ast.AnnAssign)] for n in tree.body
                  if isinstance(n, ast.ClassDef) and n.name == "Finding")
    words = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "STATUSES" for t in n.targets))
    return fields, words


def _register_cases(tmp, ok):
    """The register's files (§6): shaped as the producers write them, and agreeing with each other on the clean run."""
    fields, words = _ledger_words()
    for method in METHODS:
        d = os.path.join(tmp, "clean", method)

        def text(rel, d=d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                return f.read()
        rows = [json.loads(line) for line in text(LEDGER).splitlines()]
        ok(f"X15 {method}: each findings.jsonl row has exactly the ledger's fields and one of its status words",
           len(rows) == 4 and all(sorted(r) == sorted(fields) and r["status"] in words for r in rows)
           and all(e["status"] in words for r in rows for e in r["events"]), str([sorted(set(r) ^ set(fields)) for r in rows]))
        critics = [r for r in rows if r["source"].startswith("critique")]
        ok(f"X16 {method}: no finding is open or declared; critics' findings and one raised by a harness check are both present",
           all(r["status"] == "fixed" for r in rows) and len(critics) == 3
           and [r["id"] for r in rows if r not in critics] == [STALE_ID]
           and {r["severity"] for r in critics} == {"HIGH", "MEDIUM"})

        def built(name, rel, d=d, method=method):
            with open(os.path.join(tmp, "defect", method, name, rel), encoding="utf-8") as f:
                return f.read()

        def named_tests(read, there):
            """(the tests file's rows by id, the test each declaration, no-data entry and finding names) of a run."""
            tests = {t["test_id"]: t for t in yaml.safe_load(read(DT))["data"]["tests"]}
            scope = yaml.safe_load(read(SCOPE)) if there(SCOPE) else {"declarations": []}
            spec, found = yaml.safe_load(read(SPEC_FILES[1])), [json.loads(line) for line in read(LEDGER).splitlines()]
            named = ([os.path.basename(e["decision_test"])[:-5] for e in scope["declarations"]]
                     + [e["decision_test"] for e in json.loads(read(CHOICE))["engine_departures"]]
                     + [e["decision_test"] for e in yaml.safe_load(read(DOSSIER))["inputs"].values() if "no_data" in e]
                     + [e["decision_test"] for e in (spec["calibration"].get("joint") or {}).get("unmatched", {}).get("declared", [])]
                     + [os.path.basename(r["decision_test"]["file"])[:-5] for r in found if r["decision_test"]])
            return tests, named
        tests, named = named_tests(text, lambda rel: os.path.exists(os.path.join(d, rel)))
        ran = {k for k, t in tests.items() if t["status"] == "run"}
        choice, spec = json.loads(text(CHOICE)), yaml.safe_load(text(SPEC_FILES[1]))
        dossier = yaml.safe_load(text(DOSSIER))["inputs"]
        verdicts = sorted({t["verdict"] for t in tests.values()})
        changing = [k for k, t in tests.items() if t["verdict"] == "CHANGES_PLAN"]
        ok(f"X17 {method}: decision tests of each verdict; every test a dossier entry names ran; the one plan-changing "
           f"test is named by a row; the test that did not run is named by none",
           verdicts == ["CHANGES_PLAN", "NOT_RUN", "NO_CHANGE"] and set(named) <= ran and changing == ["DT9"]
           and "DT9" in named and len(set(named)) == len(named) == 3, str((verdicts, sorted(set(named) - ran), named)))
        eng = yaml.safe_load(text(ENGINE_REC))["data"]["card"]["benchmarks"]
        chosen = choice["candidates"][0]
        status = {b["id"]: b["status"] for b in eng}
        ok(f"X18 {method}: the clean run holds nothing declared (no departure, no declared miss, no scope declaration, "
           f"no finding held as declared), and the engine choice and the engine record agree on every benchmark (§9)",
           not os.path.exists(os.path.join(d, SCOPE)) and "engine_departures" not in spec and choice["engine_departures"] == []
           and chosen["departures"] == [] and all(r["status"] not in ("declared", "owner_accepted") and not r["decision_test"] for r in rows)
           and (method != "history_matching" or (spec["calibration"]["joint"]["unmatched"]["declared"] == []
                                                 and yaml.safe_load(text(JC))["data"]["unmatched_decision"]["declared"] == []))
           and chosen["benchmarks"] == len(eng) and all(r["status"] == status[r["id"]] for r in chosen["benchmarks_reported"])
           and [r["id"] for r in chosen["benchmarks_not_in_band"]] == [i for i, st in status.items() if st != "in_band"] == [])
        dtests, dnamed = named_tests(lambda rel: built("declared", rel), lambda rel: os.path.exists(os.path.join(tmp, "defect", method, "declared", rel)))
        drows = [json.loads(line) for line in built("declared", LEDGER).splitlines()]
        dscope, dchoice = yaml.safe_load(built("declared", SCOPE)), json.loads(built("declared", CHOICE))
        dspec = yaml.safe_load(built("declared", SPEC_FILES[1]))
        dstatus = {b["id"]: b["status"] for b in yaml.safe_load(built("declared", ENGINE_REC))["data"]["card"]["benchmarks"]}
        ok(f"X18b {method}, declared: every test a declaration names ran with no change; the scope declaration names a "
           f"finding the ledger holds as declared with the same test; each engine departure is signed with the keys the "
           f"engine choice writes, is the signed spec's entry, and gives the benchmark the engine record's own status"
           f"{'; the signed spec declares a calibration miss' if method == 'history_matching' else ''}",
           all(dtests[t]["status"] == "run" and dtests[t]["verdict"] == "NO_CHANGE" for t in set(dnamed) - {"DT9"})
           and len(set(dnamed)) == len(dnamed) - 1 and {"DT3", "DT4", "DT5"} <= set(dnamed)
           and all(e["blocker_id"] in {r["id"] for r in drows} for e in dscope["declarations"])
           and [r["id"] for r in drows if r["status"] == "declared"] == ["R-001"]
           and all(r["status"] != "declared" or r["decision_test"]["file"] in [e["decision_test"] for e in dscope["declarations"]] for r in drows)
           and [sorted(e) for e in dchoice["engine_departures"]] == [
               sorted(["engine", k, "reason", "decision_test", "status", "benchmark_status", "role", "components"])
               for k in ("benchmark", "component")] and all(e["status"] == "signed" for e in dchoice["engine_departures"])
           and [{k: e[k] for k in s} for e, s in zip(dchoice["engine_departures"], dspec["engine_departures"])] == dspec["engine_departures"]
           and dchoice["candidates"][0]["departures"] == dchoice["engine_departures"]
           and [(e["benchmark_status"], e["role"]) for e in dchoice["engine_departures"]] == [(dstatus["B2"], "held_out"), ("no benchmark", "none")]
           and dstatus["B2"] == "out_of_band" and dchoice["candidates"][0]["benchmarks_not_in_band"] == []
           and (method != "history_matching" or dspec["calibration"]["joint"]["unmatched"]["declared"] == [CAL_DECLARED]
                == yaml.safe_load(built("declared", JC))["data"]["unmatched_decision"]["declared"]))
        sheet = text(SHEET).splitlines()
        ok(f"X19 {method}: the clean sign-off sheet marks no row HIGH, names the signed spec's hash, and holds the word "
           f"HIGH where it marks nothing",
           sum(line.startswith("- **HIGH**:") for line in sheet) == 0 and sum("HIGH" in line for line in sheet) == 2
           and hashlib.sha256(text(SPEC_FILES[1]).encode()).hexdigest()[:16] in sheet[2])
        no_data = [k for k, e in dossier.items() if "no_data" in e]
        ok(f"X20 {method}: the dossier holds no-data entries and one with observations; the signed spec excludes an "
           f"input by name; no comment line",
           len(no_data) == 3 and any("observations" in e for e in dossier.values())
           and spec["uncertainty"]["excluded"][0]["parameter"] == HELD and f"parameter:{HELD}" in no_data
           and not any(line.lstrip().startswith("#") for line in text(SPEC_FILES[1]).splitlines()))

        high = built("sheet_high", SHEET).splitlines()
        ok(f"X19b {method}, sheet_high: the sheet marks one row HIGH, and the sign-off record is the clean run's: it "
           f"holds a hash for the spec and the dossier and none for the sheet, as toolkit/spec/signoff.py sign() writes it",
           sum(line.startswith("- **HIGH**:") for line in high) == 1 and sum("HIGH" in line for line in high) == 3
           and built("sheet_high", "decision_spec.signoff.json") == text("decision_spec.signoff.json")
           and sorted(json.loads(text("decision_spec.signoff.json"))["sha256"]) == sorted(["decision_spec.yaml", DOSSIER]))
        signed, disk = built("declared_only_on_disk", SPEC_FILES[1]), built("declared_only_on_disk", SPEC_FILES[0])
        ok(f"X21 {method}: declared_only_on_disk changes the spec on disk alone; the signed copy and the sign-off "
           f"record's hash are the clean run's",
           "q7" in disk and "q7" not in signed and signed == text(SPEC_FILES[1])
           and built("declared_only_on_disk", "decision_spec.signoff.json") == text("decision_spec.signoff.json"))
        prose = [line for line in built("prose_only", SPEC_FILES[1]).splitlines() if line.startswith("#")]
        ok(f"X22 {method}: prose_only puts the same four comment lines in both copies of the spec, which still parse",
           prose == PROSE and built("prose_only", SPEC_FILES[0]) == built("prose_only", SPEC_FILES[1])
           and yaml.safe_load(built("prose_only", SPEC_FILES[1])) == spec)
        last = built("findings_bad_line", LEDGER).splitlines()[-1]
        try:
            json.loads(last)
            parses = True
        except ValueError:
            parses = False
        odd = [json.loads(x)["status"] for x in built("finding_unknown_status", LEDGER).splitlines()]
        ok(f"X23 {method}: the bad ledger line is not JSON and the four rows before it are; the odd status is not a "
           f"word the ledger writes",
           not parses and len(built("findings_bad_line", LEDGER).splitlines()) == 5
           and [w for w in odd if w not in words] == ["resolved"])
        old = yaml.safe_load(built("older_layout", OLD_DECLARED))
        gate = json.loads(built("older_layout", OLD_GATE))
        ok(f"X24 {method}: the older layout has no scope declaration, and its two files name findings the ledger holds as open",
           not os.path.exists(os.path.join(tmp, "defect", method, "older_layout", SCOPE))
           and [e["id"] for e in old["declared"]] == ["R-001"] and gate["resolved_by_test"] == ["D-002"]
           and {json.loads(x)["id"]: json.loads(x)["status"] for x in built("older_layout", LEDGER).splitlines()}
           == {"D-001": "fixed", "D-002": "open", "R-001": "open", STALE_ID: "fixed"})
    ok("X25 twenty-one edits name the register as their view, each for both methods; the Fit view's edits name none",
       len(REGISTER_DEFECTS) == 21 and all(DEFECTS[n]["methods"] == METHODS for n in REGISTER_DEFECTS)
       and all(s.get("view") in (None, "register", "model", "figures") for s in DEFECTS.values()))


def _producer_words():
    """What the producers' code holds, read from their source without importing it: the benchmark status words
    (toolkit/engines/benchmark.py STATUSES), the tiers of the standard figure set, its figure names and the producer
    name (toolkit/analysis/figures.py TIERS, DECISION_SETS, PRODUCER), and whether each sentence the fixture copies is
    still in the file that writes it."""
    import ast

    def source(*parts):
        with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
            return f.read()

    def constant(text, name):
        return next(ast.literal_eval(n.value) for n in ast.parse(text).body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == name for t in n.targets))
    bench, figs = source("toolkit", "engines", "benchmark.py"), source("toolkit", "analysis", "figures.py")
    cover, est = source("toolkit", "engines", "benchmark_coverage.py"), source("toolkit", "analysis", "estimates.py")
    tiers, sets = constant(figs, "TIERS"), constant(figs, "DECISION_SETS")
    quoted = (all(f'"{w}"' in bench for w in MIS_BASED) and REPORTED_WHY.split(", ")[0] in cover
              and NO_TRAJECTORIES in figs and NO_TESTS_FILE.replace("decision_tests", "{kind}") in figs
              and 'if name.startswith("estimates_") else "diagnostics"' in est)
    return {"statuses": constant(bench, "STATUSES"), "tiers": {**tiers, "decision": sets["package_per_unit"]},
            "producer": constant(figs, "PRODUCER"), "quoted": quoted}


def clean_of(tmp, method):
    """The names in the clean run's analysis folder."""
    return {f"analysis/{n}" for n in os.listdir(os.path.join(tmp, "clean", method, "analysis"))}


def _model_cases(tmp, ok):
    """The Model view's files (§5.4): shaped as the producers write them, and clear of every rule on the clean run."""
    words = _producer_words()
    ok("X26 the status words, the figure tiers and names, the producer name and the copied sentences are the producers' own",
       tuple(words["statuses"]) == BENCH_STATUSES and words["producer"] == FIG_PRODUCER["standard"] and words["quoted"])
    for method in METHODS:
        d = os.path.join(tmp, "clean", method)

        def doc(rel, d=d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                return json.load(f) if rel.endswith(".json") else yaml.safe_load(f)
        eng, choice, spec = doc(ENGINE_REC), doc(CHOICE), doc(SPEC_FILES[1])
        rows, chosen = eng["data"]["card"]["benchmarks"], doc(CHOICE)["candidates"][0]
        clear = [0.25 * math.log(b["band"][1] / b["band"][0]) < math.log(v / b["band"][0]) < 0.75 * math.log(b["band"][1] / b["band"][0])
                 for b in rows for v in (b["computed"], b["seed_min"], b["seed_max"])]
        ok(f"X27 {method}: four benchmarks, each in band with its value and its seeds clear of both edges on the log "
           f"scale; both roles; one band spans more than a factor of ten",
           len(rows) == 4 and all(b["status"] == "in_band" and b["in_band"] is True for b in rows) and all(clear)
           and {b["role"] for b in rows} == {"fit", "held_out"} and sum(b["band"][1] / b["band"][0] > 10 for b in rows) == 1)
        heads = _headers(d, {r: 1 for r in artifacts(method)})
        with open(os.path.join(d, CHOICE), "rb") as f:
            now = hashlib.sha256(f.read()).hexdigest()
        ok(f"X28 {method}: the spec leaves the engine to the engine choice, and every analysis header and the manifest "
           f"hold the hash of engine_choice.json",
           spec["engine"] == "auto" and all(h[2]["engine_choice_sha256"] == now for h in heads.values())
           and doc("analysis/manifest.json")["engine_choice_sha256"] == now and len(heads) == len(artifacts(method)))
        ok(f"X29 {method}: the engine choice has three candidates (one eligible, one whose card does not fit, one with "
           f"no readable card); the chosen one lists no benchmark as not in band and one as reported, with the record's status",
           [c["eligible"] for c in choice["candidates"]] == [True, False, False] and choice["engine"]["ref"] == chosen["ref"]
           and "name" not in choice["candidates"][2] and chosen["benchmarks_not_in_band"] == [] and chosen["benchmarks"] == 4
           and [(r["id"], r["status"], r["why"]) for r in chosen["benchmarks_reported"]] == [("B4", "in_band", REPORTED_WHY)])
        sv, pars = eng["data"]["spec_values"], eng["data"]["parameters"]
        ok(f"X30 {method}: one spec-set value, inside the card's range and flagged so, also in the signed spec; every "
           f"parameter has a range and is not marked unverified; no fitted parameter sits on its fitted range's end",
           list(sv) == list(spec["engine_parameters"]) == ["p2"] and sv["p2"]["outside_card_range"] is False
           and sv["p2"]["outside"] is None and sv["p2"]["card_range"][0] <= sv["p2"]["range"][0] and sv["p2"]["range"][1] <= sv["p2"]["card_range"][1]
           and sv["p2"]["value"] == spec["engine_parameters"]["p2"]["value"]
           and all(p["lo"] is not None and p["hi"] is not None and p["verified"] == "matched" for p in pars)
           and all(f["lo"] < f["value"] < f["hi"] for f in eng["data"]["card"]["fitted"].values())
           and sorted(p["name"] for p in pars if p["kind"] == "fitted") == sorted(eng["data"]["card"]["fitted"]))
        def built(name, rel, method=method):
            with open(os.path.join(tmp, "defect", method, name, rel), encoding="utf-8") as f:
                return f.read()
        held = {}
        for v in ("C4_out_of_band_departure", "departure_component"):
            signed, record = yaml.safe_load(built(v, SPEC_FILES[1]))["engine_departures"], json.loads(built(v, CHOICE))
            tests = {t["test_id"]: t for t in yaml.safe_load(built(v, DT))["data"]["tests"]}
            row = {b["id"]: b for b in yaml.safe_load(built(v, ENGINE_REC))["data"]["card"]["benchmarks"]}
            held[v] = (len(signed) == 1 and record["engine_departures"] == record["candidates"][0]["departures"]
                       and [{k: e[k] for k in signed[0]} for e in record["engine_departures"]] == signed
                       and record["engine_departures"][0]["status"] == "signed"
                       and record["engine_departures"][0]["benchmark_status"] == (row[signed[0]["benchmark"]]["status"] if "benchmark" in signed[0] else "no benchmark")
                       and all(tests[e["decision_test"]]["status"] == "run" and tests[e["decision_test"]]["verdict"] == "NO_CHANGE"
                               and tests[e["decision_test"]]["share_pct"] < 0.75 * tests[e["decision_test"]]["threshold_pct"] for e in signed))
        ok(f"X31 {method}: a variant's signed departure is in the signed spec and, with the benchmark's own status, in "
           f"the engine choice; its test ran, changed nothing and sits clear of its threshold", all(held.values()), str(held))
        v = "spec_benchmarks_recomputed"
        again, record, tests_file = yaml.safe_load(built(v, BSV)), json.loads(built(v, BSV_REC)), yaml.safe_load(built(v, DT))
        ok(f"X32 {method}: the clean run has no benchmarks recomputed at the spec's values; the variant's file has no "
           f"artifact header, its record holds the file's hash and the signed spec's, the decision tests list it as an "
           f"input and the manifest does not list it",
           BSV not in clean_of(tmp, method) and "spec_sha256" not in again and again["producer"] == "toolkit/engines/benchmark.py"
           and [b["id"] for b in again["benchmarks"]] == ["B2"] == list(record["benchmarks"])
           and record["sha256"] == hashlib.sha256(built(v, BSV).encode()).hexdigest() and record["file"] == BSV
           and record["spec_sha256"] == tests_file["spec_sha256"] and BSV in tests_file["inputs"]
           and BSV not in json.loads(built(v, "analysis/manifest.json"))["artifacts"])
        try:
            yaml.safe_load(built("engine_record_unparsable", ENGINE_REC))
            parses = True
        except yaml.YAMLError:
            parses = False
        out = [b for b in yaml.safe_load(built("C4_unreachable", ENGINE_REC))["data"]["card"]["benchmarks"] if b["status"] != "in_band"]
        ok(f"X33 {method}: the cut engine record is not YAML; the unreachable band is written in the producer's words",
           not parses and [(b["id"], b["status"], b["mis_based_reason"], b["in_band"]) for b in out] == [("B4", BENCH_STATUSES[3], MIS_BASED[1], False)])
        signed, disk = built("C4_out_of_band_departure_unsigned", SPEC_FILES[1]), built("C4_out_of_band_departure_unsigned", SPEC_FILES[0])
        ok(f"X34 {method}: the unsigned departure is in the spec on disk alone",
           "benchmark: B3" in disk and "benchmark: B3" not in signed and signed == yaml.safe_dump(spec, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000))
    ok("X35 the Model view's edits: every check has one that fires and one whose inputs contradict each other or miss a piece",
       {DEFECTS[n]["check"] for n in MODEL_DEFECTS} == {"benchmarks", "departures", "choice_text", "spec_values", "choice_bound",
                                                        "seed_range", "parameters_sourced", "fitted_at_bound"}
       and all(DEFECTS[n]["methods"] == METHODS for n in MODEL_DEFECTS) and len(MODEL_DEFECTS) == 25, str(len(MODEL_DEFECTS)))


def _figures_cases(tmp, ok):
    """The Figures view's files (§5.12): shaped as the two producers write them, fresh and used on the clean run."""
    words = _producer_words()
    for label, method, figures in (("history_matching", "history_matching", "standard"), ("posterior", "posterior", "standard"),
                                   ("estimates", "posterior", "estimates")):
        d = os.path.join(tmp, "clean", label)

        def raw(rel, d=d):
            with open(os.path.join(d, rel), "rb") as f:
                return f.read()

        def doc(rel):
            return json.loads(raw(rel)) if rel.endswith(".json") else yaml.safe_load(raw(rel))
        index, man = doc(FIGS), doc("analysis/manifest.json")
        data, standard = index["data"], figures == "standard"
        entries = data["figures"]
        want = (["figure", "path", "svg", "table", "title", "caption", "shows", "sources", "offset_free", "theme", "tier",
                 "group", "presentation"] if standard else ["id", "path", "svg", "sources", "tier"])
        ok(f"X36 {label}: analysis/figures.yaml names its producer and holds every figure as produced, with the "
           f"producer's own keys; nothing is listed as not produced",
           index["producer"] == FIG_PRODUCER[figures] and index["schema"] == "analysis.figures/1" and data["not_produced"] == []
           and all(list(e) == want for e in entries) and list(data) == ["figures", "not_produced", "tiers", "groups", "overviews"])
        names = [e["figure" if standard else "id"] for e in entries]
        if standard:
            ok(f"X37 {label}: three tiers under the producer's names, every figure name one of its tier in the producer's "
               f"code, and an overview image per tier",
               list(data["tiers"]) == ["data_model", "simulation", "decision"] and sorted(sum(data["tiers"].values(), [])) == sorted(names)
               and all(set(v) <= set(words["tiers"][k]) and v for k, v in data["tiers"].items())
               and all(e["tier"] == next(k for k, v in data["tiers"].items() if e["figure"] in v) for e in entries)
               and all(raw(rel)[:8] == b"\x89PNG\r\n\x1a\n" for rel in data["overviews"].values()) and len(data["overviews"]) == 3)
        else:
            ok(f"X37 {label}: the estimates producer records no tiers, groups or overviews; each entry's tier follows its name",
               data["tiers"] == data["groups"] == data["overviews"] == {} and
               all(e["tier"] == ("decision" if e["id"].startswith("estimates_") else "diagnostics") for e in entries) and len(entries) == 4)
        fresh, listed, images = [], [], set()
        for e in entries:
            rec, png = doc(e["path"] + ".provenance.json"), raw(e["path"])
            images.add(hashlib.sha256(png).hexdigest())
            fresh.append(png[:8] == b"\x89PNG\r\n\x1a\n" and sorted(rec["source_hashes"]) == sorted(e["sources"]) and rec["png_path"] == e["path"]
                         and all(hashlib.sha256(raw(x)).hexdigest() == h for x, h in rec["source_hashes"].items())
                         and all(hashlib.sha256(raw(x["path"])).hexdigest() == x["sha256"] for x in rec["presentation"])
                         and rec["presentation"] == e.get("presentation", []) and (not standard or raw(e["table"])))
            listed.append(man["deliverables"].get(e["path"]) == {"producer": FIG_PRODUCER[figures], "role": "figure",
                                                                 "sha256": hashlib.sha256(png).hexdigest()})
        ok(f"X38 {label}: each figure has a valid image of its own, its table and a provenance record whose source and "
           f"presentation hashes are the files'; the manifest lists each image with its hash",
           all(fresh) and all(listed) and len(images) == len(entries) and len(entries) == (4 if not standard else 13 if method == "history_matching" else 8))
        texts = raw(REPORT).decode() + raw(MANUSCRIPT).decode() + raw(RESULTS).decode()
        decision = [e["path"] for e in entries if e["tier"] == "decision"]
        ok(f"X39 {label}: every decision figure is named by its path or its file name in a report text; the render "
           f"record holds the report's hash; the word trajectories is in the report and names no file",
           len(decision) >= 2 and all(os.path.basename(x) in texts for x in decision) and any(x not in texts for x in decision)
           and doc(RENDER)["report_sha256"] == hashlib.sha256(raw(REPORT)).hexdigest() and "trajectories" in texts
           and "trajectories.png" not in texts)
    ok("X40 the Figures view's edits: every check has one that fires and one whose inputs contradict each other or miss a piece",
       {DEFECTS[n]["check"] for n in FIGURES_DEFECTS} == {"figure_fresh", "figure_missing", "figure_unused", "figure_in_manifest"}
       and len(FIGURES_DEFECTS) == 12 and sum("estimates" in figure_sets(n) for n in FIGURES_DEFECTS) == 9, str(len(FIGURES_DEFECTS)))


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
               all(s == spec_sha for s, _, _ in heads.values()) and so["sha256"] == {"decision_spec.yaml": spec_sha, DOSSIER: clean[method][DOSSIER]}
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
                later = {r for r in clean[method] if STAGES.index(stage_of(r)) > STAGES.index(stage)}
                ok(f"X7 {method}: upto {stage} leaves out the {len(later)} file(s) later stages write",
                   set(got) == set(clean[method]) - later, str(sorted(set(got) ^ (set(clean[method]) - later))))

            for name, spec in DEFECTS.items():
                if method not in spec["methods"]:
                    continue
                got = build(os.path.join(tmp, "defect", method, name), defect=name, method=method)
                diff = {r for r in set(got) | set(clean[method]) if got.get(r) != clean[method].get(r)}
                expect = (_follows(d, clean[method], touched(name, method)) if spec.get("follows", True)
                          else set(touched(name, method)))
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
           and jc_doc["status"] == "matched" and not jc_doc["blocks"]
           and all(v["runs"] > 1 for v in jc_doc["verification"]["units"].values())
           and all(PRIOR[p][0] < lo and hi < PRIOR[p][1] for p, (lo, hi) in jc_doc["region"]["ranges"].items()))
        ok("X10 every defect entry names its case, check, status, methods and one-sentence edit",
           all(s["check"] and s["status"] in ("ok", "look", "problem", "not_available") and s["methods"] and s["edit"].endswith(".")
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
        _register_cases(tmp, ok)
        _model_cases(tmp, ok)
        est = build(os.path.join(tmp, "clean", "estimates"), method="posterior", figures="estimates")
        for name in FIGURES_DEFECTS:
            if "estimates" not in figure_sets(name):
                continue
            got = build(os.path.join(tmp, "defect", "estimates", name), defect=name, method="posterior", figures="estimates")
            diff = {r for r in set(got) | set(est) if got.get(r) != est.get(r)}
            first = touched(name, "posterior", "estimates")
            expect = _follows(os.path.join(tmp, "clean", "estimates"), est, first) if DEFECTS[name].get("follows", True) else set(first)
            ok(f"X8 estimates figure set: {name} changes the files its edit names, the files that hash them, and no others",
               diff == expect, f"unexpected {sorted(diff - expect)}, missing {sorted(expect - diff)}")
        _figures_cases(tmp, ok)
        for bad in ({"method": "unknown"}, {"defect": "unknown"}, {"upto": "unknown"},
                    {"defect": "C2_change_flagged", "method": "posterior"}, {"figures": "unknown"}, {"figures": "estimates"},
                    {"figures": "estimates", "method": "posterior", "defect": "figure_not_produced"}):
            try:
                build(os.path.join(tmp, "bad"), **bad)
                refused = False
            except ValueError:
                refused = True
            ok(f"X11 build refuses {bad}", refused)

        words, hits = domain_words(), []
        for root, _, names in os.walk(tmp):
            for n in names:
                with open(os.path.join(root, n), "rb") as f:        # an image is searched as the bytes it is
                    text = (os.path.relpath(os.path.join(root, n), tmp) + "\n" + f.read().decode("latin-1")).lower()
                hits += [(n, w) for w in words if w in text]
        ok(f"X12 no built fixture file holds a domain word ({len(words)} words)", len(words) > 50 and not hits,
           str(sorted(set(hits))[:8]))
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--method", default="history_matching", choices=METHODS)
    ap.add_argument("--figures", default="standard", choices=FIGURE_SETS)
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
    print(f"{len(build(a.dir, upto=a.upto, defect=a.defect, method=a.method, figures=a.figures))} files written to {a.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
