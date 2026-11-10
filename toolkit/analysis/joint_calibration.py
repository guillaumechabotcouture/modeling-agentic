#!/usr/bin/env python3
"""The spec adapter of the joint calibration (toolkit/calibration/history_matching.py): ANALYZE's calibration step
before the outcome batch, and a diagnostic command.

    python3 toolkit/analysis/joint_calibration.py <run_dir> [--spec decision_spec.yaml] [--out DIR] [--units FILE]
                                                  [--workers N] [--waves N] [--points-per-unit N]
    python3 toolkit/analysis/joint_calibration.py --self-test

The spec field (signed with the spec; without it nothing changes):

    calibration:
      method: emulator_inversion      # unchanged: attribute, scale, range, target_column, window, metric
      joint:
        shared: [names]               # engine parameters calibrated jointly; each must be a prior the uncertainty
                                      # draws vary (engine-card priors, uncertainty.priors, or an engine_parameters
                                      # value with a range), so its sourced range is its prior and the outcome
                                      # emulator's design spans it
        features: [columns]           # optional unit features of the calibration emulator: a numeric column, or
                                      # {column, count: <separator>, empty: <text>} (the number of listed items)
        discrepancy_sd: 0.1           # model discrepancy sd of every target on its scale (a target may override)
        seed, waves, points_per_unit, shared_points   # optional; defaults in toolkit/calibration/thresholds.yaml
        best_point_weighting: per_unit | per_group    # optional (default per_unit): per_group counts each group once
        changes: {by, weights, sampling_correlation}  # optional (F6 S2-3b): how the change between two rounds of one
                                      # survey is compared by group: by a units column (default the survey's bias_by,
                                      # else decision_units.region), units weighted by a column (default equally),
                                      # the rounds' sampling errors correlated as declared (default 0). Declaring it
                                      # makes a pair that cannot be computed a gate finding; without it the pairs are
                                      # still computed and recorded
        targets:
          - {id, kind: unit, column, window: [a, b], metric, scale, sd | sd_column | ci_columns: [lo, hi] |
             n_column, bias_sd, bias_by, bias_source, discrepancy_sd, source}
             # survey: the survey a round belongs to (default bias_source when bias_by is set); its rounds are
             # paired by group (F6 S2-3b, survey_changes in the artifact)
             # bias_by: a units column (e.g. the region); bias_sd is then one offset per survey (bias_source,
             # default the target id) and group, shared by the group's units and the survey's targets (F6 S2-3)
          - {id, kind: group, csv, filter: {col: value}, key: {csv: col, units: col}, value, multiplier,
             ci: [lo_col, hi_col], ci_level, weights: <units column>, window, metric, scale, bias_sd, discrepancy_sd,
             source}
          - {id: <benchmark id>, kind: benchmark, discrepancy_sd}   # a fit target of the engine's own pack only

A benchmark the engine's pack holds out (every id not in toolkit/engines/benchmark.py fit_targets) is refused: it
never enters the fit (the PR #140 rule). Unit and group targets are simulated with the comparator package through
the analysis batch (scripts/compute.py, so a Coiled cluster runs them as it runs the outcome batch); benchmark
targets through the benchmark runner at the shared values. Nothing here names a disease.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.calibration import history_matching as HM  # noqa: E402

PRODUCER = "toolkit/analysis/joint_calibration.py"
FIELD = "joint"
TARGET_KINDS = ("unit", "group", "benchmark")
OUT_REL = "analysis/joint_calibration"


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def declared(spec) -> dict | None:
    raw = spec.raw if hasattr(spec, "raw") else spec
    j = (raw.get("calibration") or {}).get(FIELD)
    return j if j is not None else None


def _window(w):
    return isinstance(w, list) and len(w) == 2 and all(_isnum(x) for x in w) and w[0] < w[1]


def _feature(row, f):
    if isinstance(f, str):
        return float(row[f])
    v = str(row.get(f["column"], "")).strip()
    if not v or v == str(f.get("empty", "")):
        return 0.0
    return float(len([x for x in v.split(f.get("count") or " ") if x.strip()]))


def _benchmarks(eng):
    from toolkit.engines import benchmark as BM
    from toolkit.engines.benchmark_sources import load_evidence
    ev = load_evidence(eng)             # S1-2b: the snapshot's declarations (fitted freshness: the analysis entry)
    if ev.decl_problems:
        return None, set(), "the engine's benchmark evidence is not usable: " + "; ".join(ev.decl_problems)
    pack = ev.pack
    if not pack.get("benchmarks"):
        return None, set(), "the engine has no benchmark pack"
    return pack, BM.fit_targets(pack), None


def spec_problems(spec) -> list[str]:
    """Shape and meaning of calibration.joint (fail closed): every reason, or []."""
    j = declared(spec)
    if j is None:
        return []
    w = f"calibration.{FIELD}"
    if not isinstance(j, dict):
        return [f"{w}: a mapping {{shared, targets, ...}}"]
    raw = spec.raw
    cal = raw["calibration"]
    bad = []
    if cal.get("method") != "emulator_inversion":
        bad.append(f"{w}: needs calibration.method emulator_inversion (its attribute, range and scale are the "
                   "per-unit parameter)")
    if (raw.get("analysis") or {}).get("evaluation") != "emulator":
        bad.append(f"{w}: needs analysis.evaluation emulator")
    unk = sorted(set(j) - {"shared", "features", "discrepancy_sd", "seed", "waves", "points_per_unit",
                           "shared_points", "targets", "independent", "record", "unmatched", "sensitivity",
                           "best_point_weighting", "changes"})
    if unk:
        bad.append(f"{w}: unknown keys {unk}")
    ch = j.get("changes")
    if ch is not None:
        if not (isinstance(ch, dict) and set(ch) <= {"by", "weights", "sampling_correlation"}):
            bad.append(f"{w}.changes: a mapping {{by, weights, sampling_correlation}}")
        else:
            rho = ch.get("sampling_correlation", 0.0)
            if not (_isnum(rho) and -1.0 <= rho <= 1.0):
                bad.append(f"{w}.changes.sampling_correlation: a number in [-1, 1]")
    if j.get("best_point_weighting", "per_unit") not in ("per_unit", "per_group"):
        bad.append(f"{w}.best_point_weighting: per_unit (the default) or per_group")
    sh = j.get("shared")
    if not (isinstance(sh, list) and all(isinstance(x, str) and x for x in sh) and len(set(sh)) == len(sh)):
        bad.append(f"{w}.shared: a list of distinct engine parameter names")
        sh = []
    for k in ("waves", "points_per_unit", "shared_points"):
        if k in j and not (isinstance(j[k], int) and not isinstance(j[k], bool) and j[k] >= 1):
            bad.append(f"{w}.{k}: a positive integer")
    if "seed" in j and not (isinstance(j["seed"], int) and not isinstance(j["seed"], bool)):
        bad.append(f"{w}.seed: an integer")
    if "discrepancy_sd" in j and not (_isnum(j["discrepancy_sd"]) and j["discrepancy_sd"] >= 0):
        bad.append(f"{w}.discrepancy_sd: a number >= 0")
    try:
        cols = set(spec.units()[0]) if spec.units() else set()
    except (OSError, KeyError) as e:
        return bad + [f"{w}: the units table is unreadable ({e})"]
    for q in ("by", "weights"):
        if isinstance(ch, dict) and q in ch and ch[q] not in cols:
            bad.append(f"{w}.changes.{q}: no units column {ch[q]!r}")
    for f in j.get("features") or []:
        c = f if isinstance(f, str) else (f.get("column") if isinstance(f, dict) else None)
        if c not in cols:
            bad.append(f"{w}.features: no units column {c!r}")
    if not spec.engine_pending:
        from toolkit.spec import priors as PRI
        eng = spec.engine()
        try:
            dr = PRI.drawn(spec)
        except Exception as e:  # noqa: BLE001 -- the priors' own check names it
            dr = {}
            bad.append(f"{w}: the priors cannot be read ({e})")
        # names only: whether the fitted values are current is the analysis entry's check (a blocked run, F6 S1-2)
        fr = {p.name for p in eng.parameters() if p.kind == "fitted"}
        for s in sh:
            if s not in dr and s not in fr:
                bad.append(f"{w}.shared: {s} is not a prior the uncertainty draws vary (an engine-card prior, an "
                           "uncertainty.priors entry or an engine_parameters value with a range) nor an engine-fitted "
                           "parameter: a shared parameter needs a sourced range, and the outcome emulator must span it")
        # Codex r2 P1-3: every parameter the uncertainty draws vary moves with the region rows (shared) or is declared
        # independent of the calibration targets, with the reason; otherwise a draw could pair a region row with a
        # value that row was never fitted at
        ind = j.get("independent") or []
        if not (isinstance(ind, list) and all(isinstance(x, dict) and isinstance(x.get("parameter"), str)
                                              and str(x.get("reason") or "").strip() for x in ind)):
            bad.append(f"{w}.independent: a list of {{parameter, reason}}")
            ind = []
        indn = {x["parameter"] for x in ind}
        srcs = {x.get("kind") for x in (raw.get("uncertainty") or {}).get("sources") or [] if isinstance(x, dict)}
        varying = set(dr) | (set(fr) if "engine_parameters" in srcs else set())
        for v in sorted(varying - set(sh) - indn):
            bad.append(f"{w}: {v} is varied by the uncertainty draws but is neither in shared nor declared "
                       "independent of the calibration targets (independent: [{parameter, reason}])")
        for v in sorted(indn & set(sh)):
            bad.append(f"{w}.independent: {v} is also shared")
        card = eng.describe()
        wins = card.outputs.get("windows", [])
    else:
        eng, wins = None, None
    bad += _unmatched_problems(j.get("unmatched"), w)
    bad += _sensitivity_problems(j.get("sensitivity"), j.get("targets"), w)
    for n, r in enumerate(j.get("record") or []):
        if not (isinstance(r, dict) and _window(r.get("window")) and (wins is None or r.get("metric") in wins)):
            bad.append(f"{w}.record[{n}]: {{window: [a, b], metric: <engine window metric>}} (kept with every run, "
                       "never a target)")
    tg = j.get("targets")
    if not (isinstance(tg, list) and tg):
        return bad + [f"{w}.targets: a non-empty list"]
    ids, surveys = [], {}
    for n, t in enumerate(tg):
        tw = f"{w}.targets[{n}]"
        if not isinstance(t, dict):
            bad.append(f"{tw}: a mapping")
            continue
        ids.append(t.get("id"))
        k = t.get("kind")
        if k not in TARGET_KINDS:
            bad.append(f"{tw}: kind one of {TARGET_KINDS}")
            continue
        if not (isinstance(t.get("id"), str) and t["id"]):
            bad.append(f"{tw}: id (text)")
        if "discrepancy_sd" in t and not (_isnum(t["discrepancy_sd"]) and t["discrepancy_sd"] >= 0):
            bad.append(f"{tw}.discrepancy_sd: a number >= 0")
        if k == "benchmark":
            if eng is not None:
                pack, fit, why = _benchmarks(eng)
                ids_pack = {b["id"] for b in (pack or {}).get("benchmarks") or []}
                if pack is None:
                    bad.append(f"{tw}: the engine has no benchmark pack ({why})")
                elif t.get("id") not in ids_pack:
                    bad.append(f"{tw}: {t.get('id')} is not a benchmark of the engine's pack")
                elif t["id"] not in fit:
                    bad.append(f"{tw}: {t['id']} is a held-out benchmark of the engine's pack; a held-out benchmark "
                               "never enters a fit (only the pack's fit targets may)")
            dep = t.get("depends_on")
            if dep is not None and not (isinstance(dep, list) and dep and set(dep) <= set(sh)):
                bad.append(f"{tw}.depends_on: a non-empty list of shared parameters (the ones the benchmark's settings "
                           "use; the emulator of this benchmark takes only those)")
            continue
        if not _window(t.get("window")):
            bad.append(f"{tw}.window: [a, b] with a < b")
        if wins is not None and t.get("metric") not in wins:
            bad.append(f"{tw}.metric: {t.get('metric')!r} is not an engine window metric")
        if t.get("scale", "logit") not in HM.SCALES:
            bad.append(f"{tw}.scale: one of {HM.SCALES}")
        if not str(t.get("source") or "").strip():
            bad.append(f"{tw}.source: the observed values' source")
        for q in ("bias_sd", "sd"):
            if q in t and not (_isnum(t[q]) and t[q] >= 0):
                bad.append(f"{tw}.{q}: a number >= 0")
        if "bias_by" in t:          # F6 S2-3: one offset per source and group, shared by the group's units
            if k != "unit":
                bad.append(f"{tw}.bias_by: for unit targets (a group target's bias is its own)")
            elif t["bias_by"] not in cols:
                bad.append(f"{tw}.bias_by: no units column {t['bias_by']!r}")
            if not (_isnum(t.get("bias_sd")) and t["bias_sd"] > 0):
                bad.append(f"{tw}.bias_by: needs bias_sd > 0 (the sd of the shared offset)")
            src = t.get("bias_source", t.get("id"))      # Codex r1 P2-4: the survey, shared by its targets
            if not _text(src):
                bad.append(f"{tw}.bias_source: the survey's name (text)")
            other = surveys.setdefault(src, (t.get("bias_sd"), t["bias_by"]))
            if other != (t.get("bias_sd"), t["bias_by"]):
                bad.append(f"{tw}: survey {src!r} is declared with two bias_sd or bias_by values ({other})")
        elif "bias_source" in t:
            bad.append(f"{tw}.bias_source: needs bias_by")
        if "survey" in t and not _text(t["survey"]):
            bad.append(f"{tw}.survey: the survey's name (text)")
        if k == "unit":
            c = t.get("column", cal.get("target_column"))
            if c not in cols:
                bad.append(f"{tw}.column: no units column {c!r}")
            for q in ("sd_column", "n_column"):
                if q in t and t[q] not in cols:
                    bad.append(f"{tw}.{q}: no units column {t[q]!r}")
            if "ci_columns" in t and not (isinstance(t["ci_columns"], list) and len(t["ci_columns"]) == 2
                                          and all(c in cols for c in t["ci_columns"])):
                bad.append(f"{tw}.ci_columns: [lo, hi] units columns")
        else:
            for q in ("csv", "value", "weights"):
                if not t.get(q):
                    bad.append(f"{tw}.{q}: required for a group target")
            key = t.get("key")
            if not (isinstance(key, dict) and key.get("csv") and key.get("units") in cols):
                bad.append(f"{tw}.key: {{csv: <column>, units: <units column>}}")
            if t.get("weights") and t["weights"] not in cols:
                bad.append(f"{tw}.weights: no units column {t['weights']!r}")
            if not (isinstance(t.get("ci"), list) and len(t["ci"]) == 2) and "sd" not in t:
                bad.append(f"{tw}: ci: [lo, hi] columns of the table, or sd")
    if len(set(ids)) != len(ids):
        bad.append(f"{w}.targets: repeated ids")
    if len(surveys) > 1:         # Codex r2: every unit target applies to every unit, so two surveys mean two blocks
        bad.append(f"{w}.targets: {len(surveys)} surveys carry bias_by ({sorted(surveys)}); one survey per unit may "
                   "carry a shared offset. Declare the other as a group target (an aggregate over the group's units) "
                   "or without bias_by")
    return bad


UNMATCHED_MODES = ("block", "flag")
SENS_KEYS = ("sd", "bias_sd", "discrepancy_sd")


def _text(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _unmatched_problems(u, w) -> list:
    """calibration.joint.unmatched (owner decision 2026-11-06): absent means block. With mode flag, units that no
    compatible value matches or that fail the engine verification proceed, each flagged low-confidence with a
    recorded reason: reasons per unit or per region, else default_reason; at most max_units of them."""
    if u is None:
        return []
    ww = f"{w}.unmatched"
    if not isinstance(u, dict):
        return [f"{ww}: a mapping {{mode: block | flag, default_reason, max_units, reasons}}"]
    bad = []
    unk = sorted(set(u) - {"mode", "default_reason", "max_units", "reasons"})
    if unk:
        bad.append(f"{ww}: unknown keys {unk}")
    if u.get("mode") not in UNMATCHED_MODES:
        bad.append(f"{ww}.mode: one of {UNMATCHED_MODES}")
    if u.get("mode") == "flag":
        if not _text(u.get("default_reason")):
            bad.append(f"{ww}.default_reason: the reason recorded for a flagged unit without its own (text)")
        mu = u.get("max_units")
        if not (isinstance(mu, int) and not isinstance(mu, bool) and mu >= 0):
            bad.append(f"{ww}.max_units: the most units that may proceed flagged (an integer >= 0)")
    rs = u.get("reasons", [])
    if not isinstance(rs, list):
        bad.append(f"{ww}.reasons: a list of {{unit: <id>, reason}} or {{region: <name>, reason}}")
        rs = []
    for n, r in enumerate(rs):      # Codex r5 P2-5: exactly one selector, text, besides the reason
        sel = [k for k in ("unit", "region") if isinstance(r, dict) and k in r]
        if not (isinstance(r, dict) and _text(r.get("reason")) and len(sel) == 1 and _text(r.get(sel[0]))
                and set(r) == {sel[0], "reason"}):
            bad.append(f"{ww}.reasons[{n}]: {{unit: <id>, reason}} or {{region: <name>, reason}}")
    return bad


def _sensitivity_problems(s, targets, w) -> list:
    """calibration.joint.sensitivity: [{id, label, targets: {target id: {sd | bias_sd | discrepancy_sd: v}}}],
    each calibrated on every run of the primary setting and reported, never a block."""
    if s is None:
        return []
    ww = f"{w}.sensitivity"
    if not isinstance(s, list):
        return [f"{ww}: a list of {{id, label, targets}}"]
    ids = {t.get("id") for t in targets or [] if isinstance(t, dict)}
    bad, seen = [], set()
    for n, x in enumerate(s):
        if not (isinstance(x, dict) and _text(x.get("id")) and _text(x.get("label"))
                and isinstance(x.get("targets"), dict) and x["targets"]):
            bad.append(f"{ww}[{n}]: {{id, label, targets: {{target id: {{sd, bias_sd, discrepancy_sd}}}}}}")
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(x["id"])):     # Codex r6 P2: it names a directory
            bad.append(f"{ww}[{n}].id: letters, digits, _ and - only")
        if x["id"] in seen:
            bad.append(f"{ww}[{n}]: repeated id {x['id']}")
        seen.add(x["id"])
        kinds = {t.get("id"): t for t in targets or [] if isinstance(t, dict)}
        for tid, ov in x["targets"].items():
            t0 = kinds.get(tid) or {}
            if tid in ids and t0.get("kind") == "benchmark" and isinstance(ov, dict) and set(ov) - {"discrepancy_sd"}:
                bad.append(f"{ww}[{n}].targets.{tid}: a benchmark's sampling error comes from its band; only "
                           "discrepancy_sd can change")
                continue
            if tid in ids and isinstance(ov, dict) and "sd" in ov and any(
                    k in t0 for k in ("sd_column", "ci_columns", "n_column", "ci")):
                bad.append(f"{ww}[{n}].targets.{tid}: sd cannot override a target whose sampling error comes from "
                           "its data (sd_column, ci_columns, n_column or ci); change bias_sd or discrepancy_sd")
                continue
            if tid not in ids:
                bad.append(f"{ww}[{n}].targets: {tid!r} is not a target id")
            elif not (isinstance(ov, dict) and ov and set(ov) <= set(SENS_KEYS)
                      and all(_isnum(v) and v >= 0 for v in ov.values())):
                bad.append(f"{ww}[{n}].targets.{tid}: {{{' | '.join(SENS_KEYS)}: a number >= 0}}")
    return bad


def unmatched_decision(spec, rep) -> dict:
    """Whether a blocked joint calibration may proceed under calibration.joint.unmatched (fail closed: without the
    option, with mode block, with any block that is not a unit's miss, or with more misses than max_units, it may
    not). {proceed, mode, flagged: [{unit, status, reason, implausibility, direction}]}."""
    j = declared(spec) or {}
    u = j.get("unmatched") or {"mode": "block"}
    units = rep.get("units") or {}
    missed = [k for k, v in units.items() if v.get("status") != "matched"]
    unit_codes = {"unmatched_units", "verification_failed"}
    codes = {b["code"] for b in rep.get("blocks") or []}
    target_fail = [x for b in rep.get("blocks") or [] if b["code"] == "verification_failed"
                   for x in ((b.get("detail") or {}).get("targets") or [])]
    reg = spec.raw["decision_units"].get("region")
    rows = {str(r[spec.uid]): r for r in spec.units()}
    by_unit = {str(r["unit"]): r["reason"] for r in u.get("reasons") or [] if r.get("unit")}
    by_reg = {str(r["region"]): r["reason"] for r in u.get("reasons") or [] if r.get("region")}
    flagged = []
    for k in sorted(missed, key=str):
        v = units[k]
        region = str(rows.get(k, {}).get(reg, "")) if reg else ""
        flagged.append({"unit": k, "region": region, "status": v.get("status"),
                        "reason": by_unit.get(k) or by_reg.get(region) or u.get("default_reason"),
                        "implausibility": v.get("implausibility_verified", v.get("best_implausibility")),
                        "direction": v.get("direction")})
    ok = (u.get("mode") == "flag" and codes <= unit_codes and not target_fail and not rep.get("conflicting")
          and len(flagged) <= int(u.get("max_units", -1)) and _blocks_consistent(rep, missed))
    return {"proceed": bool(ok) if rep.get("blocked") else True, "mode": u.get("mode", "block"),
            "flagged": flagged if ok or not rep.get("blocked") else [], "missed": flagged,
            "why_not": None if ok or not rep.get("blocked") else
            ("no flag option" if u.get("mode") != "flag" else f"blocks {sorted(codes)}; {len(flagged)} misses, max "
             f"{u.get('max_units')}; target failures {target_fail}; conflicting {rep.get('conflicting')}")}


def _blocks_consistent(rep, missed) -> bool:
    """Codex r6 P1-4: a blocked report may let unit misses proceed only when its blocks are complete and name
    exactly the units whose status is not matched (fail closed on a partial or malformed report)."""
    named = set()
    blocks = rep.get("blocks")
    if not (isinstance(blocks, list) and blocks):
        return False
    for b in blocks:
        d = b.get("detail") if isinstance(b, dict) else None
        if not isinstance(d, dict):
            return False
        if b["code"] == "unmatched_units":
            if not (isinstance(d.get("unmatched"), list) and d.get("conflicting") == []):
                return False
            named |= {str(x) for x in d["unmatched"]}
        elif b["code"] == "verification_failed":
            if not (isinstance(d.get("units"), list) and d.get("targets") == []):
                return False
            named |= {str(x) for x in d["units"]}
        else:
            return False
    units = rep.get("units") or {}
    return bool(named) and named == {str(k) for k in missed} and named <= {str(k) for k in units}


def _with_overrides(raw, overrides) -> dict:
    import copy
    r = copy.deepcopy(raw)
    tg = {t["id"]: t for t in r["calibration"][FIELD]["targets"]}
    for tid, ov in overrides.items():
        for k, v in ov.items():
            tg[tid][k] = v
    r["calibration"][FIELD].pop("sensitivity", None)
    return r


def run_sensitivities(spec, rep, run_dir, compute_kw=None, log=print, units=None) -> list:
    """Each declared sensitivity: the spec with its target errors changed, calibrated on every run of the primary
    setting (no new wave; the engine verification runs), reported beside the primary, never a block. A sensitivity
    that fails at any step is reported with its error and the primary continues (Codex r5 P2-4, r6 P2)."""
    out = []
    j = declared(spec) or {}
    for n, x in enumerate(j.get("sensitivity") or []):
        try:
            r2 = _one_sensitivity(spec, rep, run_dir, compute_kw, log, units, j, n, x)
        except Exception as e:  # noqa: BLE001  the primary is decided; this optional result is reported as failed
            out.append({"id": x["id"], "label": x["label"], "targets": x["targets"],
                        "error": f"{type(e).__name__}: {str(e)[:500]}"})
            log(f"sensitivity {x['id']}: failed ({type(e).__name__}); reported, the primary continues")
            continue
        out.append({"id": x["id"], "label": x["label"], "targets": x["targets"], "report": r2})
        log(f"sensitivity {x['id']}: {r2['n_matched']} of {r2['n_units']} units matched (reported, not a block)")
    return out


def _one_sensitivity(spec, rep, run_dir, compute_kw, log, units, j, n, x) -> dict:
    import yaml
    _, meta1 = build(spec, units)        # Codex r7 P2: inside the sensitivity's error handling
    from toolkit.analysis.artifacts import _safe_io
    from toolkit.spec import schema
    raw = _with_overrides(spec.raw, x["targets"])
    rel = os.path.join(OUT_REL, f"sensitivity_{x['id']}", "decision_spec.yaml")
    _safe_io().write_nofollow(run_dir, rel, yaml.safe_dump(raw, sort_keys=False).encode(), atomic=True)
    s2 = schema.load(os.path.join(run_dir, rel))
    s2.base = spec.base              # the copy resolves data paths as the spec does
    check_same_simulator(spec, s2)
    P2, meta2 = build(s2, units)
    prior = carry_runs(rep["runs"], meta1["windows"], meta2["windows"])
    sim = Simulator(s2, run_dir, meta2, compute_kw, log)
    sim.calls = 1000 * (n + 1)
    r2 = HM.calibrate(P2, sim, seed=int(j.get("seed", 0)), waves=0, log=log, prior=prior,
                      record=meta2["record"],
                      **{k: j[k] for k in ("points_per_unit", "shared_points", "best_point_weighting") if k in j})
    r2["prior_runs"] = {"reused": len(prior["unit"]), "shared_reused": len(prior["shared"]),
                        "from": "the primary setting, same process"}
    r2["adapter"] = {"windows": meta2["windows"], "units": len(meta2["units"]), "shared": list(j["shared"])}
    return r2


# --------------------------------------------------------------------------- problem
def theta0(spec) -> dict:
    """The point parameter values every simulation starts from (as toolkit/analysis/core.py Analysis.theta0)."""
    from toolkit.analysis.batch import fitted_ranges
    from toolkit.spec import engine_parameters as PV
    from toolkit.spec import priors as PRI
    eng = spec.engine()
    dr = dict(fitted_ranges(eng), **{k: (p.lo, p.hi, p.value) for k, p in PRI.drawn(spec).items()})
    return PV.params(spec, {k: v[2] for k, v in dr.items()})


def _unit_sd(t, row, scale, value):
    if "sd_column" in t:
        return float(row[t["sd_column"]])
    if "ci_columns" in t:
        return HM.sd_from_ci(float(row[t["ci_columns"][0]]), float(row[t["ci_columns"][1]]), scale,
                             float(t.get("ci_level", 0.95)))
    if "n_column" in t:
        return HM.sd_from_n(value, float(row[t["n_column"]]), scale)
    return float(t.get("sd", 0.0))


def _windows(j) -> dict:
    """{window key: [a, b]}, one key per distinct window of the unit and group targets (jc0, jc1, ...)."""
    out = {}
    for t in list(j["targets"]) + [dict(r, kind="record") for r in j.get("record") or []]:
        if t["kind"] in ("unit", "group", "record"):
            w = [float(x) for x in t["window"]]
            if w not in out.values():
                out[f"jc{len(out)}"] = w
    return out


def _observable(t, wins):
    key = next(k for k, v in wins.items() if v == [float(x) for x in t["window"]])
    return f"{key}.{t['metric']}"


def build(spec, units=None) -> tuple:
    """(Problem, meta) from the spec. `units`: a subset of unit ids (diagnostics only); group targets then need every
    unit of their group in the subset, or they are dropped and listed in meta['dropped']."""
    bad = spec_problems(spec)
    if bad:
        raise HM.CalibrationInputError(bad)
    from toolkit.spec import priors as PRI
    j = declared(spec)
    cal = spec.raw["calibration"]
    from toolkit.analysis.batch import fitted_ranges
    dr = PRI.drawn(spec)
    eng_ = spec.engine()
    fr = fitted_ranges(eng_)                     # S1-2b: the snapshot's fitted values, carried (not reread)
    src = {p.name: p.source for p in eng_.parameters() if p.kind == "fitted"}
    shared = [HM.Param(n, dr[n].lo, dr[n].hi, dr[n].dist, dr[n].value, dr[n].source) if n in dr else
              HM.Param(n, fr[n][0], fr[n][1], "uniform", fr[n][2], f"engine card fitted range ({src.get(n)})")
              for n in j["shared"]]
    rows = {str(r[spec.uid]): r for r in spec.units()}
    ids = [i for i in spec.unit_ids() if units is None or i in set(units)]
    missing = sorted(set(units or []) - set(rows))
    if missing:
        raise HM.CalibrationInputError([f"--units: not units of the spec: {missing[:5]}"])
    wins = _windows(j)
    md0 = float(j.get("discrepancy_sd", 0.0))
    targets, dropped = [], []
    for t in j["targets"]:
        md = float(t.get("discrepancy_sd", md0))
        if t["kind"] == "unit":
            col = t.get("column", cal.get("target_column"))
            sc = t.get("scale", "logit")
            for i in ids:
                v = float(rows[i][col])
                targets.append(HM.Target(f"{t['id']}:{i}", "unit", _observable(t, wins), v, sc,
                                         sd_obs=_unit_sd(t, rows[i], sc, v), sd_bias=float(t.get("bias_sd", 0.0)),
                                         sd_md=md, unit=i, source=t["source"],
                                         bias_key=f"{t.get('bias_source', t['id'])}|{rows[i][t['bias_by']]}"
                                         if t.get("bias_by") else None))
        elif t["kind"] == "group":
            sc = t.get("scale", "logit")
            mult = float(t.get("multiplier", 1.0))
            with open(spec.resolve(t["csv"]), newline="", encoding="utf-8") as f:
                tab = [r for r in csv.DictReader(f)
                       if all(str(r.get(k)) == str(v) for k, v in (t.get("filter") or {}).items())]
            if not tab:
                raise HM.CalibrationInputError([f"group target {t['id']}: no row of {t['csv']} matches "
                                                f"{t.get('filter')}"])
            for r in tab:
                g = str(r[t["key"]["csv"]])
                members_all = [i for i, u in rows.items() if str(u[t["key"]["units"]]) == g]
                members = [i for i in members_all if i in ids]
                if not members_all:
                    raise HM.CalibrationInputError([f"group target {t['id']}: group {g!r} has no unit in the units "
                                                    f"table (key {t['key']})"])
                if len(members) < len(members_all):
                    dropped.append({"target": f"{t['id']}:{g}", "why": f"{len(members)} of {len(members_all)} units "
                                                                       "of the group are calibrated"})
                    continue
                v = mult * float(r[t["value"]])
                sd = (HM.sd_from_ci(mult * float(r[t["ci"][0]]), mult * float(r[t["ci"][1]]), sc,
                                    float(t.get("ci_level", 0.95))) if t.get("ci") else float(t["sd"]))
                targets.append(HM.Target(f"{t['id']}:{g}", "group", _observable(t, wins), v, sc, sd_obs=sd,
                                         sd_bias=float(t.get("bias_sd", 0.0)), sd_md=md,
                                         weights={i: float(rows[i][t["weights"]]) for i in members},
                                         source=t["source"]))
        else:
            pack, fit, why = _benchmarks(spec.engine())
            if pack is None:
                raise HM.CalibrationInputError([f"benchmark target {t['id']}: no usable engine pack ({why})"])
            b = next(x for x in pack["benchmarks"] if x["id"] == t["id"])
            lo, hi = (float(x) for x in b["band"])
            sc = "log" if lo > 0 and float(b["target"]) > 0 else "identity"
            from toolkit.engines.benchmark_sources import band_level, band_sd    # F6 S1-2: at the recorded level
            try:
                sd = band_sd(b, lambda x: HM.tr(x, sc))
            except ValueError as e:
                raise HM.CalibrationInputError([f"benchmark target {t['id']}: {e}"]) from None
            targets.append(HM.Target(t["id"], "shared", f"bench.{t['id']}", float(b["target"]), sc, sd_obs=sd,
                                     sd_md=md, source=f"engine benchmark pack: {b.get('source', '')} (band as an "
                                                      f"interval at level {band_level(b)})", depends_on=t.get("depends_on")))
    feats = j.get("features") or []
    F = {i: [_feature(rows[i], f) for f in feats] for i in ids} if feats else {}
    up = {"name": cal["attribute"], "lo": float(cal["range"][0]), "hi": float(cal["range"][1]),
          "scale": cal.get("scale", "log")}
    reg = spec.raw["decision_units"].get("region")
    P = HM.Problem(shared, up, ids, targets, features=F,
                   feature_names=[f if isinstance(f, str) else f["column"] for f in feats],
                   groups={i: rows[i].get(reg, "") for i in ids} if reg else None,
                   changes=change_pairs(j, rows, ids, targets, reg))
    rec = [_observable(r, wins) for r in j.get("record") or []]
    return P, {"windows": wins, "dropped": dropped, "units": ids, "record": rec}


# --------------------------------------------------------------------------- survey changes (F6 S2-3b)
def _survey(t):
    return t.get("survey") or (t.get("bias_source", t["id"]) if t.get("bias_by") else None)


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _round_mismatch(a, b):
    """Why two rounds of one survey cannot be compared (their definitions differ), or None."""
    if a["kind"] != b["kind"]:
        return f"definitions differ: {a['id']} is a {a['kind']} target, {b['id']} a {b['kind']} target"
    for q, dflt in (("metric", None), ("scale", "logit")):
        if a.get(q, dflt) != b.get(q, dflt):
            return f"definitions differ: {q} {a.get(q, dflt)!r} and {b.get(q, dflt)!r}"
    if [float(x) for x in a["window"]] == [float(x) for x in b["window"]]:
        return "the two rounds have the same window"
    if a["kind"] == "unit" and a.get("column") == b.get("column"):
        return f"the two rounds read one column ({a.get('column')})"
    if a["kind"] == "group" and ((a.get("key") or {}).get("units"), a.get("weights")) != (
            (b.get("key") or {}).get("units"), b.get("weights")):
        return "definitions differ: the rounds aggregate over different groupings or weights"
    return None


def change_pairs(j, rows, ids, targets, reg) -> list:
    """F6 S2-3b: each pair of rounds of one survey (targets with the same survey, by window), one entry per group:
    unit rounds by the group column (changes.by, the survey's bias_by, else the region), each unit weighted (changes.
    weights, else equally); group rounds by their group. What cannot be compared is an entry with its reason."""
    ch = j.get("changes") or {}
    rho = float(ch.get("sampling_correlation", 0.0))
    series = {}
    for t in j["targets"]:
        if t["kind"] in ("unit", "group") and _survey(t):
            series.setdefault(_survey(t), []).append(t)
    if not series:
        return [{"id": "*", "not_computable": "no unit or group target names its survey (survey, or bias_source with "
                                              "bias_by), so no rounds can be paired"}]
    built = {tg.id: tg for tg in targets}
    out = []
    for s, ts in sorted(series.items()):
        ts = sorted(ts, key=lambda t: float(t["window"][0]))
        if len(ts) < 2:
            out.append({"id": s, "survey": s, "not_computable": f"a single round ({ts[0]['id']})"})
        for n, a in enumerate(ts):
            for b in ts[n + 1:]:
                base = {"id": f"{s}:{a['id']}->{b['id']}", "survey": s, "from": a["id"], "to": b["id"]}
                why = _round_mismatch(a, b)
                by = ch.get("by") or a.get("bias_by") or reg if a["kind"] == "unit" else (a.get("key") or {}).get("units")
                if why or not by:
                    out.append(dict(base, not_computable=why or "no group column (changes.by, bias_by or the region)"))
                    continue
                if a["kind"] == "unit":
                    grp = {}
                    for i in ids:
                        grp.setdefault(str(rows[i][by]), []).append(i)
                    for g, us in sorted(grp.items()):
                        wt = {i: _num(rows[i][ch["weights"]]) if ch.get("weights") else 1.0 for i in us}
                        tot = sum(v for v in wt.values() if v is not None and v > 0)
                        e = dict(base, id=f"{base['id']}:{g}", group=g, by=by)
                        badw = sorted(i for i, v in wt.items() if v is None or v < 0)
                        out.append(dict(e, not_computable=f"weights not finite or negative for units {badw[:5]}") if badw
                                   else dict(e, rho=rho, terms=[{"from": f"{a['id']}:{i}", "to": f"{b['id']}:{i}",
                                                                 "w": wt[i] / tot} for i in us if wt[i] > 0])
                                   if tot > 0 else dict(e, not_computable="the group's units have no positive weight"))
                    continue
                ga, gb = ({k[len(t["id"]) + 1:]: k for k, v in built.items() if v.kind == "group"
                           and k.startswith(t["id"] + ":")} for t in (a, b))
                for g in sorted(set(ga) | set(gb) | {str(r.get(by)) for r in rows.values()}):
                    e = dict(base, id=f"{base['id']}:{g}", group=g, by=by)
                    out.append(dict(e, rho=rho, terms=[{"from": ga[g], "to": gb[g], "w": 1.0}]) if g in ga and g in gb
                               else dict(e, not_computable=f"group {g} has no value in round "
                                                           f"{' or '.join(t['id'] for t, q in ((a, ga), (b, gb)) if g not in q)}"
                                                           " (not in its table, or not every unit of the group is "
                                                           "calibrated)"))
    return out


def _cf(kind, claim, key, declarable):
    return {"kind": kind, "severity": "HIGH", "stage": "ANALYZE", "target_stage": "ANALYZE", "claim": claim,
            "key": key, "scope_declarable": declarable, "source": PRODUCER}


def change_findings(raw, data) -> list:
    """Gate findings of the survey-change diagnostic in analysis/joint_calibration.yaml (F6 S2-3b). A flagged group is
    declarable (the gap is declared with its reason); a missing or malformed record, another threshold, or a flag its
    numbers do not give is not (fail closed). A pair that cannot be computed is a finding only when the spec asks for
    the diagnostic (calibration.joint.changes); it is recorded either way."""
    F = "analysis/joint_calibration.yaml"
    sc = data.get("survey_changes") if isinstance(data, dict) else None
    zmax = float(HM.thresholds()["survey_changes"]["z_max"])
    if not (isinstance(sc, dict) and isinstance(sc.get("pairs"), list) and sc["pairs"] and sc.get("z_max") == zmax):
        return [_cf("calibration_change_unrecorded", f"{F} records no survey-change diagnostic at the threshold "
                    f"z_max {zmax:g} (survey_changes)", "survey_changes", False)]
    asked = isinstance(raw, dict) and ((raw.get("calibration") or {}).get(FIELD) or {}).get("changes") is not None
    out = []
    for p in sc["pairs"]:
        pid = str(p.get("id")) if isinstance(p, dict) else "?"
        if isinstance(p, dict) and _text(p.get("not_computable")):
            if asked:
                out.append(_cf("calibration_change_not_computable", f"survey change {pid}: not computable: "
                                                                    f"{p['not_computable']}", f"change:{pid}", True))
            continue
        z, d, sd, o, m = (p.get(k) for k in ("z", "difference", "sd", "observed", "modelled")) if isinstance(p, dict) \
            else (None,) * 5
        if not (all(_isnum(v) for v in (z, d, sd, o, m)) and sd > 0 and abs(z - d / sd) <= 1e-6 * max(1.0, abs(z))
                and abs(d - (m - o)) <= 1e-9 * max(1.0, abs(m), abs(o))
                and p.get("flagged") is (abs(z) > zmax)):
            out.append(_cf("calibration_change_inconsistent", f"{F}: survey change {pid}: its flag or z does not follow "
                                                              "from its numbers", f"change:{pid}", False))
        elif p["flagged"]:
            out.append(_cf("calibration_change_discrepancy", (
                f"survey {p.get('survey')}, {p.get('from')} to {p.get('to')}, group {p.get('group')}: {p.get('text')} "
                f"(z {z:+.2f}; flagged beyond {zmax:g})"), f"change:{pid}", True))
    return out


# --------------------------------------------------------------------------- simulation
class _Deferred(Exception):
    """A benchmark runner needs engine jobs that are not in the cache yet (recorded; run them as a batch)."""


def _jdefault(x):
    return x.tolist() if hasattr(x, "tolist") else repr(x)


def _jkey(j) -> str:
    return json.dumps(list(j), sort_keys=True, default=_jdefault)


def _runner_cls():
    from toolkit.engines import benchmark as BM

    class CachedRunner(BM.Runner):
        """The benchmark runner with its engine calls answered from a cache; a miss records the jobs and stops this
        evaluation (_Deferred), so the jobs of every point are batched together."""

        def __init__(self, pack, engine, cache, pending, base_params=None):
            super().__init__(pack, engine, workers=1, base_params=base_params, mode="fit")   # θ includes fitted
            self._jc_cache, self._jc_pending = cache, pending

        def _map(self, jobs, chunk: int = 300):
            keys = [_jkey(j) for j in jobs]
            miss = [j for j, k in zip(jobs, keys) if k not in self._jc_cache]
            if miss:
                self._jc_pending.extend(miss)
                raise _Deferred()
            return [self._jc_cache[k] for k in keys]
    return CachedRunner


def _CachedRunner(*a, **k):
    return _runner_cls()(*a, **k)


class Simulator:
    """HistoryMatch's simulate(): unit points through the analysis batch (comparator package), shared points through
    the benchmark runner. Every call writes its own results file under analysis/joint_calibration/."""

    def __init__(self, spec, run_dir, meta, compute_kw=None, log=print):
        from toolkit.analysis.batch import calibrated_value
        self.spec, self.run_dir, self.meta, self.log = spec, run_dir, meta, log
        self.kw = dict(compute_kw or {})
        self.eng = spec.engine()
        self.th0 = theta0(spec)
        self.cv = calibrated_value
        self.calls = 0
        self.rows = {str(r[spec.uid]): r for r in spec.units()}
        pk = spec.packages()
        self.comp = spec.comparator
        self.comp_engine = spec.engine_package(pk[self.comp])
        h = spec.raw["horizon"]
        self.horizon = {"start": h["start"], "stop": h["stop"], "window": list(spec.raw["objective"]["horizon"])}
        if spec.raw["objective"].get("delivery") is not None:
            self.horizon["delivery"] = [float(x) for x in spec.raw["objective"]["delivery"]]
        self.bench = None

    def job(self, p, k, windows=None, call=None) -> dict:
        """The batch job of unit point p at position k of a call (the batch's job_sha covers every field but id)."""
        a = self.spec.unit_attributes(self.rows[p["unit"]])
        a[self.spec.raw["calibration"]["attribute"]] = self.cv(self.spec, p["z"])
        return {"id": f"jc{self.calls if call is None else call}|{k}", "point": k, "package": self.comp,
                "components": self.comp_engine, "unit": a, "params": self._theta(p["theta"]),
                "seed": int(p["seed"]) % (2 ** 31), "engine": self.spec.raw["engine"], "horizon": self.horizon,
                "windows": self.meta["windows"] if windows is None else windows}

    def _theta(self, th):
        from toolkit.spec import engine_parameters as PV
        return PV.params(self.spec, dict({k: v for k, v in self.th0.items()}, **th))

    def _shared(self, pts, six, out):
        """Benchmark targets at each point's shared values, through the compute backend (scripts/compute.py: a
        Coiled cluster when the job has one). Each point's runner re-calibrates the pack's settings there and
        evaluates every benchmark; its engine jobs are collected across all points and run as one batch per phase
        (settings first, then the arms they calibrate), the runner replaying from the results."""
        from toolkit.analysis.batch import fitted_ranges
        from toolkit.engines import benchmark as BM
        pack, fit, why = _benchmarks(self.eng)
        if pack is None:
            raise HM.CalibrationInputError([f"benchmark targets: the engine has no pack ({why})"])
        want = [t["id"] for t in self.spec.raw["calibration"][FIELD]["targets"] if t["kind"] == "benchmark"]
        held = [t for t in want if t not in fit]
        if held:                    # a held-out benchmark never enters a fit (also refused at validation)
            raise HM.CalibrationInputError([f"benchmark targets {held} are held out by the engine's pack"])
        byid = {b["id"]: b for b in pack["benchmarks"]}
        fitted = {k: v[2] for k, v in fitted_ranges(self.eng).items()}
        settings = sorted({pack["arms"][a]["setting"] for t in want for a in BM._arms_of(byid[t]["estimand"])})
        cache = {}
        for phase in range(int(self.kw.get("bench_phases", 8))):
            pending, done = [], {}
            for n in six:
                R = _CachedRunner(pack, self.eng, cache, pending,
                                  base_params={k: v for k, v in self._theta(pts[n]["theta"]).items() if k not in fitted})
                try:
                    R.calibrate_settings(fitted, settings)
                except _Deferred:
                    continue
                vals = {}
                for t in want:
                    try:
                        vals[f"bench.{t}"] = float(R.evaluate(byid[t], fitted)["computed"])
                    except _Deferred:
                        pass
                if len(vals) == len(want):
                    done[n] = vals
            if not pending:
                for n in six:
                    out[n] = done[n]
                return
            self._bench_batch(pending, cache, phase)
        raise HM.CalibrationInputError(["benchmark targets: the runner still needed engine jobs after "
                                        f"{self.kw.get('bench_phases', 8)} phases"])

    def _bench_batch(self, pending, cache, phase):
        """Run the collected benchmark engine jobs once each, grouped as the runner groups them (jobs of one setting,
        horizon and seed share a starting state for an engine that offers it)."""
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import compute
        from toolkit.engines import benchmark as BM
        uniq = {}
        for j in pending:
            uniq.setdefault(_jkey(j), j)
        groups = {}
        for k, j in uniq.items():
            ref, config, params, unit, package, horizon, seed = j
            g = json.dumps([ref, config, unit, horizon, seed], sort_keys=True, default=_jdefault) \
                if BM.shares_state(BM._engine_of(j)) else k
            groups.setdefault(g, []).append(j)
        tasks = [[list(x) for x in g] for g in groups.values()]
        outp = os.path.join(self.run_dir, OUT_REL, f"bench_call{self.calls}_phase{phase}.jsonl")
        kw = {k: v for k, v in self.kw.items() if k in ("max_workers", "max_core_hours", "timeout_s")}
        rows, summ = compute.run_map(self.run_dir, os.path.join(ROOT, "toolkit", "engines", "benchmark.py") + "::_job_list",
                                     tasks, outp, purpose="v2 joint calibration: benchmark settings", **kw)
        bad = [n for n, r in enumerate(rows) if not r.get("ok")]
        if bad:
            raise HM.CalibrationInputError([f"benchmark batch: {len(bad)} of {len(rows)} tasks failed (e.g. "
                                            f"{(rows[bad[0]].get('error') or '')[:300]})"])
        for g, r in zip(tasks, rows):
            for j, o in zip(g, r["result"]):
                cache[_jkey(j)] = o

    def __call__(self, pts):
        from toolkit.analysis import batch as Bt
        from concurrent.futures import ThreadPoolExecutor
        self.calls += 1
        out = [None] * len(pts)
        uix = [n for n, p in enumerate(pts) if p["kind"] == "unit"]
        six = [n for n, p in enumerate(pts) if p["kind"] == "shared"]
        attr = self.spec.raw["calibration"]["attribute"]
        self._units(pts, uix, out, attr, Bt)           # both through the compute backend, one after the other
        if six:
            self._shared(pts, six, out)
        _ = ThreadPoolExecutor
        return out

    def _units(self, pts, uix, out, attr, Bt):
        if uix:
            jobs = [self.job(pts[n], k) for k, n in enumerate(uix)]
            b = Bt.run(self.run_dir, jobs, self.eng, out_rel=f"{OUT_REL}/call{self.calls}/results.jsonl",
                       **{k: v for k, v in self.kw.items() if k != "bench_workers"})
            if b["problems"]:
                raise HM.CalibrationInputError([f"joint calibration batch: {len(b['problems'])} rows lack matching "
                                                f"provenance (e.g. {b['problems'][:2]})"])
            for k, n in enumerate(uix):
                r = b["results"].get(jobs[k]["id"])
                ref = f"call{self.calls}:{k}"
                if r is None:
                    raise HM.CalibrationInputError([f"joint calibration batch: job {jobs[k]['id']} (unit "
                                                    f"{pts[n]['unit']}) did not complete"])
                out[n] = {f"{w}.{m}": v for w, d in (r.get("windows") or {}).items() for m, v in d.items()}
                out[n]["_ref"] = ref


# --------------------------------------------------------------------------- the step
def remap_windows(obs: dict, from_windows: dict, to_windows: dict) -> dict:
    """Observations keyed by an earlier run's window names, renamed to the current names of the same window
    endpoints (Codex r3 P1-2); a window the current problem does not have is dropped."""
    to = {tuple(float(x) for x in v): k for k, v in to_windows.items()}
    name = {k: to.get(tuple(float(x) for x in v)) for k, v in from_windows.items()}
    out = {}
    for key, v in obs.items():
        w, _, m = key.partition(".")
        if w in name and name[w] is not None:
            out[f"{name[w]}.{m}"] = v
        elif w not in from_windows:          # not a window observable (a benchmark): kept as it is
            out[key] = v
    return out


def _remap_failures(d: dict, from_windows: dict, to_windows: dict) -> dict:
    """Codex r7 P1: carried emulator failures keyed by observable, renamed as the observations are. A failure whose
    window the current problem lacks keeps a name no current observable has, so it never clears (fail closed)."""
    out = {}
    for key, v in (d or {}).items():
        r = remap_windows({key: v}, from_windows, to_windows)
        out.update(r if r else {f"unmapped:{key}": v})
    return out


def carry_runs(runs: dict, from_windows: dict, to_windows: dict) -> dict:
    """Saved runs as another problem's prior: observations and carried failures renamed by window endpoints."""
    def one(r, rename_obs):
        x = dict(r, obs=remap_windows(r["obs"], from_windows, to_windows)) if rename_obs else dict(r)
        if r.get("failed_checks"):
            x["failed_checks"] = _remap_failures(r["failed_checks"], from_windows, to_windows)
        return x
    out = {"unit": [one(r, True) for r in runs.get("unit") or []],
           "shared": [one(r, False) for r in runs.get("shared") or []]}
    if runs.get("unresolved_checks"):
        out["unresolved_checks"] = _remap_failures(runs["unresolved_checks"], from_windows, to_windows)
    return out


def sim_signature(spec) -> dict:
    """What a unit or benchmark run simulates, apart from its own point (Codex r3 P1-3): the engine and its code,
    the horizon, the units' engine attributes, the comparator, the per-unit attribute and its scale, the held
    parameter values. Two specs with different signatures cannot share runs."""
    import hashlib
    from toolkit.engines.contract import engine_identity
    j = declared(spec) or {}
    raw = spec.raw
    sh = set(j.get("shared") or [])
    units = {str(u[spec.uid]): spec.unit_attributes(u) for u in spec.units()}
    blob = json.dumps(units, sort_keys=True, default=_jdefault).encode()
    return {"engine": engine_identity(spec.engine()), "engine_ref": raw["engine"], "horizon": raw["horizon"],
            "objective": {k: raw["objective"].get(k) for k in ("horizon", "delivery")},
            "comparator": spec.engine_package(spec.packages()[spec.comparator]),
            "attribute": raw["calibration"].get("attribute"), "scale": raw["calibration"].get("scale", "log"),
            "held": {k: v for k, v in sorted(theta0(spec).items()) if k not in sh}, "shared": sorted(sh),
            "units": hashlib.sha256(blob).hexdigest()}


def check_same_simulator(a, b) -> None:
    sa, sb = sim_signature(a), sim_signature(b)
    diff = sorted(k for k in sa if sa[k] != sb.get(k))
    if diff:
        raise HM.CalibrationInputError([f"the second setting changes what the runs simulate ({diff}); its runs cannot "
                                        "be reused"])


def load_prior(spec, prior_dir, meta, log=print) -> tuple:
    """Earlier unit runs from a diagnostic output directory (the CLI's --out: runs.json, joint_calibration.yaml and
    jc_rows/call*/results.jsonl). A run is kept only when the batch job rebuilt from the current spec (with the
    earlier windows) has a non-empty job_sha equal to its stored row's and the same engine identity; its
    observations are then read from that authenticated row, renamed by window endpoints to the current names, and
    never from runs.json (Codex r3 P1-1, P1-2). A run that carries its row reference is matched to that row; an
    older run without one by its position in the calls. Earlier shared runs carry no batch provenance and are not
    reused. Every run dropped is counted."""
    import glob
    import yaml
    from toolkit.analysis import batch as Bt
    from toolkit.engines.contract import engine_identity
    runs = json.load(open(os.path.join(prior_dir, "runs.json")))
    rep = yaml.safe_load(open(os.path.join(prior_dir, "joint_calibration.yaml")))
    wins = (rep.get("adapter") or {}).get("windows")
    if not wins:
        raise HM.CalibrationInputError([f"{prior_dir}: the earlier report records no windows"])
    files = {int(os.path.basename(os.path.dirname(p))[4:]): p
             for p in glob.glob(os.path.join(prior_dir, "jc_rows", "call*", "results.jsonl"))}
    rows = {c: {r.get("i"): r for r in (json.loads(x) for x in open(f) if x.strip())} for c, f in files.items()}
    sim = Simulator(spec, prior_dir, meta)
    ident = engine_identity(spec.engine())
    P_, _ = build(spec, meta.get("units"))
    need = set(P_.observables("unit"))
    shared_now = set(P_.names)
    unit_runs, kept = runs.get("unit") or [], []
    order = [(c, i) for c in sorted(rows) for i in sorted(k for k in rows[c] if isinstance(k, int))]
    pos = 0
    for run_ in unit_runs:
        if run_.get("ref"):
            c, _, k = str(run_["ref"]).partition(":")
            try:
                c, k = int(c[4:]), int(k)
            except ValueError:
                continue
        elif pos < len(order):
            c, k = order[pos]
            pos += 1
        else:
            continue
        r = rows.get(c, {}).get(k)
        if not r or not r.get("ok"):
            continue
        pv = ((r.get("result") or {}).get("provenance") or {})
        # Codex r4 P1: the job is rebuilt from the current shared names and the current held values only; a run
        # whose shared values name anything else was simulated under other held values and cannot match
        th = run_.get("theta") or {}
        if set(th) != shared_now:
            continue
        try:
            sha = Bt.job_sha(sim.job(dict(run_, theta={k: th[k] for k in shared_now}), k, windows=wins))
        except (KeyError, TypeError, ValueError):
            continue                                    # Codex r3 P2-4: a job that cannot be rebuilt is dropped
        if not (isinstance(pv.get("job_sha"), str) and pv["job_sha"] and pv["job_sha"] == sha
                and pv.get("engine") == ident):
            continue
        res = (r.get("result") or {}).get("result") or {}
        raw_obs = {f"{w}.{m}": v for w, d in (res.get("windows") or {}).items() for m, v in d.items()}
        obs = remap_windows(raw_obs, wins, meta["windows"])
        if need <= set(obs):                     # every observable the current problem emulates, from this row
            x = dict(run_, obs=obs)
            if run_.get("failed_checks"):        # Codex r7 P1: carried failures renamed as the observations are
                x["failed_checks"] = _remap_failures(run_["failed_checks"], wins, meta["windows"])
            kept.append(x)
    dropped = len(unit_runs) - len(kept)
    log(f"earlier runs: {len(kept)} unit runs reused (provenance matched), {dropped} not matched, "
        f"{len(runs.get('shared') or [])} shared runs not reused (no batch provenance)")
    out = {"unit": kept, "shared": []}
    if runs.get("unresolved_checks"):     # Codex r6 P1-1: failed checks not yet cleared stay failed
        out["unresolved_checks"] = _remap_failures(runs["unresolved_checks"], wins, meta["windows"])
    return out, {"reused": len(kept), "unmatched": dropped, "from": prior_dir}


def run(spec, run_dir, compute_kw=None, log=print, units=None, prior_dir=None, waves=None, **override) -> dict:
    """The joint calibration of the spec: the history-matching report, plus the adapter's meta. Raises
    CalibrationInputError on a refused declaration; a blocked result is returned (report['blocked'])."""
    P, meta = build(spec, units)
    j = declared(spec)
    ov = {k: j[k] for k in ("points_per_unit", "shared_points", "best_point_weighting") if k in j}
    ov.update(override)
    prior, pinfo = load_prior(spec, prior_dir, meta, log) if prior_dir else (None, None)
    sim = Simulator(spec, run_dir, meta, compute_kw, log)
    rep = HM.calibrate(P, sim, seed=int(j.get("seed", 0)), waves=j.get("waves") if waves is None else waves, log=log,
                       prior=prior, record=meta["record"], **ov)
    rep["prior_runs"] = pinfo
    rep["adapter"] = {"windows": meta["windows"], "dropped_targets": meta["dropped"], "units": len(meta["units"]),
                      "shared": list(j["shared"]), "simulator_calls": sim.calls,
                      "theta_held": {k: v for k, v in sim.th0.items() if k not in j["shared"]}}
    rep["primary"] = {"targets": {t["id"]: {k: t[k] for k in SENS_KEYS if k in t} for t in j["targets"]
                                  if t["kind"] != "benchmark"},
                      "discrepancy_sd": j.get("discrepancy_sd"),
                      "note": "the primary setting; declared sensitivities are reported beside it, never as a block"}
    rep["unmatched_decision"] = unmatched_decision(spec, rep)
    if j.get("sensitivity"):
        rep["sensitivity"] = run_sensitivities(spec, rep, run_dir, compute_kw, log, units)
    return rep


def render_figures(rep, run_dir, labels=None, dest_rel="figures/joint_calibration") -> dict:
    """The calibration figures (toolkit/calibration/figures.py), drawn at the calibration step so a blocked run has
    them too; the figure step redraws them from analysis/joint_calibration.yaml for the manifest of a complete run."""
    from toolkit.calibration import figures as CF
    try:
        return CF.render(rep.get("figure_data"), os.path.join(run_dir, dest_rel), run_dir, labels,
                         sources=[os.path.join("analysis", "joint_calibration.yaml")])
    except Exception as e:  # noqa: BLE001 -- a figure failure is recorded, never hides the calibration's verdict
        return {"figures": [], "not_produced": [{"figure": n, "reason": f"{type(e).__name__}: {e}", "tier": CF.TIER}
                                                for n in CF.NAMES]}


def region_sample(rep) -> dict:
    """The canonical sample the uncertainty draws read: every region row, shared values and each unit's value
    together (Codex r1 P1-2: a unit's value comes from the joint fit of all its targets, never from re-inverting one
    target)."""
    s = rep["sample"]
    return {"sample_id": s["sample_id"], "names": list(s["names"]), "rows": np.asarray(s["rows"], float)}


def block_hits(rep) -> list:
    """The blocked calibration as AnalysisBlocked hits: one per unmatched or conflicting unit, one per other reason."""
    hits = []
    for u in rep["unmatched"] + rep["conflicting"]:
        x = rep["units"][u]
        hits.append({"unit": u, "package": "*", "kind": f"calibration_{x['status']}",
                     "best_implausibility": round(x["best_implausibility"], 3), "direction": x["direction"]})
    for u, x in rep["units"].items():
        if x["status"] == "verification_failed":
            hits.append({"unit": u, "package": "*", "kind": "calibration_verification_failed"})
    for b in rep["blocks"]:
        if b["code"] not in ("unmatched_units", "verification_failed"):
            hits.append({"unit": "*", "package": "*", "kind": f"calibration_{b['code']}", "detail": b["detail"]})
    return hits


def inputs(spec) -> list:
    """The files the joint calibration reads beyond the units table: its group-target tables."""
    j = declared(spec) or {}
    return sorted({spec.resolve(t["csv"]) for t in j.get("targets") or [] if t.get("kind") == "group"})


def block_reason(rep) -> str:
    parts = []
    if rep["unmatched"]:
        parts.append(f"{len(rep['unmatched'])} units no value in the prior box matches")
    if rep["conflicting"]:
        parts.append(f"{len(rep['conflicting'])} units unmatched at every compatible shared value")
    parts += [b["code"] for b in rep["blocks"] if b["code"] not in ("unmatched_units", "conflicting_units")]
    return "joint calibration blocked: " + "; ".join(parts or [b["code"] for b in rep["blocks"]])


def artifact(rep) -> dict:
    """The analysis/joint_calibration.yaml payload: the report without the bulky sample rows (their id is kept)."""
    out = {k: v for k, v in rep.items() if k not in ("sample", "runs", "sensitivity")}   # the runs: own file
    if rep.get("sensitivity"):
        out["sensitivity"] = [dict({k: v for k, v in s.items() if k != "report"},
                                   **({"report": {k: v for k, v in s["report"].items() if k not in ("sample", "runs")}}
                                      if "report" in s else {}))
                              for s in rep["sensitivity"]]
    if rep.get("sample"):
        s = region_sample(rep)
        out["sample"] = {"sample_id": s["sample_id"], "names": s["names"], "rows": s["rows"].tolist(),
                         "full_sample_id": rep["sample"]["sample_id"], "weighting": rep["sample"]["weighting"]}
    return out


def summary(rep) -> str:
    L = [f"joint calibration: {rep['status']}; {rep['n_matched']} of {rep['n_units']} units matched; region "
         f"{rep['region']['accepted']} of {rep['region']['candidates']} candidates (share of prior "
         f"{rep['region']['share_of_prior']})"]
    for n, r in rep["region"]["ranges"].items():
        i95 = (rep["region"].get("interval95") or {}).get(n) or {}
        L.append(f"  {n}: region {r} vs prior {rep['region']['prior_ranges'][n]} (contraction "
                 f"{rep['region']['contraction'][n]}); 95% region {i95.get('region')} vs prior {i95.get('prior')} "
                 f"(contraction95 {i95.get('contraction95')})")
    if rep["unmatched"] or rep["conflicting"]:
        L.append(f"  unmatched {len(rep['unmatched'])}, conflicting {len(rep['conflicting'])}")
    for b in rep["blocks"]:
        L.append(f"  block: {b['code']}")
    for s in rep.get("sensitivity") or []:
        r = s.get("report")
        L.append(f"  sensitivity {s['id']} ({s['label']}): " + (f"failed, {s.get('error')}" if r is None else
                 f"{r['n_matched']} of {r['n_units']} units matched (reported, not a block)"))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--spec", default="decision_spec.yaml")
    ap.add_argument("--out", help="diagnostic output directory (default <run_dir>/analysis/joint_calibration)")
    ap.add_argument("--units", help="a file of unit ids, one per line (diagnostics only)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--bench-workers", type=int, help="processes of the benchmark runner (default --workers)")
    ap.add_argument("--max-core-hours", type=float, help="compute.py allowance per batch call")
    ap.add_argument("--prior", help="an earlier diagnostic output directory whose runs are reused (provenance checked)")
    ap.add_argument("--then-spec", help="a second spec (another setting) calibrated next on every run of the first")
    ap.add_argument("--then-out", help="its output directory")
    ap.add_argument("--then-waves", type=int, default=0, help="new waves of the second setting (default 0)")
    ap.add_argument("--waves", type=int)
    ap.add_argument("--points-per-unit", type=int)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir is required")
    import yaml
    from toolkit.spec import schema
    rd = os.path.abspath(a.run_dir)
    spec = schema.load(os.path.join(rd, a.spec))
    units = [x.strip() for x in open(a.units) if x.strip()] if a.units else None
    kw = {"max_workers": a.workers, "bench_workers": a.bench_workers or a.workers}
    if a.max_core_hours:
        kw["max_core_hours"] = a.max_core_hours
    ov = {}
    if a.points_per_unit:
        ov["points_per_unit"] = a.points_per_unit
    say = lambda *x, **k: print(*x, flush=True)  # noqa: E731
    rep = run(spec, rd, kw, log=say, units=units, prior_dir=a.prior, waves=a.waves, **ov)
    _write_out(rep, rd, a.out)
    rc = 0 if not rep["blocked"] else 3
    if a.then_spec:     # a second setting on the same runs, in this process: every run of the first is its data
        spec2 = schema.load(os.path.join(rd, a.then_spec))
        check_same_simulator(spec, spec2)
        P2, meta2 = build(spec2, units)
        _, meta1 = build(spec, units)
        first = carry_runs(rep["runs"], meta1["windows"], meta2["windows"])
        j2 = declared(spec2)
        sim2 = Simulator(spec2, rd, meta2, kw, say)
        sim2.calls = 1000            # its batch files apart from the first setting's
        rep2 = HM.calibrate(P2, sim2, seed=int(j2.get("seed", 0)), waves=a.then_waves, log=say, prior=first,
                            record=meta2["record"], **{k: j2[k] for k in ("points_per_unit", "shared_points") if k in j2})
        rep2["adapter"] = {"windows": meta2["windows"], "dropped_targets": meta2["dropped"], "units": len(meta2["units"]),
                           "shared": list(j2["shared"]), "simulator_calls": sim2.calls - 1000,
                           "theta_held": {k: v for k, v in sim2.th0.items() if k not in j2["shared"]}}
        rep2["prior_runs"] = {"reused": len(rep["runs"]["unit"]), "shared_reused": len(rep["runs"]["shared"]),
                              "from": "the first setting, same process"}
        _write_out(rep2, rd, a.then_out or os.path.join(rd, OUT_REL + "_second"))
        rc = max(rc, 0 if not rep2["blocked"] else 3)        # Codex r3 P2-10
    return rc


def _write_out(rep, rd, out_arg):
    import yaml
    out = os.path.abspath(out_arg or os.path.join(rd, OUT_REL))
    os.makedirs(out, exist_ok=True)
    froot = rd if os.path.commonpath([rd, out]) == rd else out
    rep["figures"] = render_figures(rep, froot, dest_rel=os.path.relpath(os.path.join(out, "figures"), froot))
    print("figures:", [f["path"] for f in rep["figures"]["figures"]], rep["figures"]["not_produced"], flush=True)
    from toolkit.analysis.artifacts import _safe_io      # no link followed inside the run dir (write_audit WA3)
    _safe_io().write_nofollow(froot, os.path.relpath(os.path.join(out, "joint_calibration.yaml"), froot),
                              yaml.safe_dump(HM_plain(artifact(rep)), sort_keys=False).encode(), atomic=True)
    _safe_io().write_nofollow(froot, os.path.relpath(os.path.join(out, "runs.json"), froot),
                              json.dumps(HM_plain(rep.get("runs") or {})).encode(), atomic=True)
    import glob       # the batch rows the runs reference (Codex r3 P2-6): this directory is a valid --prior source
    for f in glob.glob(os.path.join(rd, OUT_REL, "call*", "results.jsonl")):
        rel = os.path.join("jc_rows", os.path.basename(os.path.dirname(f)), "results.jsonl")
        _safe_io().write_nofollow(froot, os.path.relpath(os.path.join(out, rel), froot), open(f, "rb").read(),
                                  atomic=True)
    print(summary(rep), flush=True)


def HM_plain(x):
    from toolkit.analysis.artifacts import plain
    return plain(x)


# --------------------------------------------------------------------------- self-test
def _toy_joint(d, n=12, unreachable=None, shared=("base",), held_out=False, group=True):
    """The toy spec (toolkit/spec/schema.py toy_spec) with a prior on `base` and a joint calibration block."""
    import yaml
    from toolkit.spec import schema
    p = schema.toy_spec(d, n=n)
    raw = yaml.safe_load(open(p))
    rows = list(csv.DictReader(open(os.path.join(d, "units.csv"))))
    if unreachable is not None:
        for r in rows:
            if r["id"] == unreachable:
                r["tgt"] = "0.9999"
        with open(os.path.join(d, "units.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    with open(os.path.join(d, "region_targets.csv"), "w") as f:
        f.write("region,level,prev,lo,hi\n")
        for g in sorted({r["region"] for r in rows}):
            m = [r for r in rows if r["region"] == g]
            ws = sum(float(r["pop"]) for r in m)
            v = sum(float(r["pop"]) / ws * float(r["tgt"]) for r in m)
            f.write(f"{g},region,{v:.5f},{max(v - 0.05, 0.001):.5f},{min(v + 0.05, 0.999):.5f}\n")
    raw["uncertainty"]["priors"] = {"base": {"dist": "uniform", "range": [0.3, 0.7], "source": "toy range"}}
    tg = [{"id": "unit_t", "kind": "unit", "column": "tgt", "window": [1, 2], "metric": "prev", "scale": "logit",
           "sd": 0.1, "source": "toy targets"}]
    if group:
        tg.append({"id": "region_t", "kind": "group", "csv": "region_targets.csv", "filter": {"level": "region"},
                   "key": {"csv": "region", "units": "region"}, "value": "prev", "ci": ["lo", "hi"],
                   "weights": "pop", "window": [1, 2], "metric": "prev", "scale": "logit", "source": "toy survey"})
    if held_out:
        tg.append({"id": "T2", "kind": "benchmark"})
    raw["calibration"]["joint"] = {"shared": list(shared), "discrepancy_sd": 0.05, "seed": 3, "waves": 1,
                                   "points_per_unit": 6, "targets": tg,
                                   "independent": [{"parameter": p, "reason": "acts only under its package; the "
                                                    "calibration simulates the comparator"} for p in ("eff_a", "eff_b")]}
    yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
    return p


def _self_test() -> int:
    import tempfile
    from toolkit.spec import schema
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def safe(fn, msg):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok(False, f"{msg}: raised {type(e).__name__}: {e}")

    q = lambda *a, **k: None  # noqa: E731
    fast = {"candidates_log2": 8}

    def ja1():
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d, shared=("k_not_a_prior",))
            bad = spec_problems(schema.load(p))
            ok(any("not a prior the uncertainty draws vary" in b for b in bad),
               "JA1 a shared parameter without a sourced prior is refused")
    safe(ja1, "JA1")

    def ja2():
        with tempfile.TemporaryDirectory() as d:
            from toolkit.spec import toy_engines as TE
            TE.enable_for_test()
            p = _toy_joint(d)
            import yaml
            raw = yaml.safe_load(open(p))
            raw["engine"] = {"ref": "toolkit.spec.toy_engines:ToySpecEngine"}
            raw["calibration"]["joint"]["targets"].append({"id": "TH1", "kind": "benchmark"})
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            bad = spec_problems(schema.load(p))
            ok(any("held-out benchmark" in b and "TH1" in b for b in bad),
               f"JA2a a held-out benchmark of the engine's pack never enters the fit ({bad})")
            raw["calibration"]["joint"]["targets"][-1]["id"] = "TF1"
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            bad = spec_problems(schema.load(p))
            ok(not any("TF1" in b for b in bad), f"JA2b a fit target of the pack is accepted ({bad})")
            raw["calibration"]["joint"]["targets"][-1]["id"] = "NOPE"
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            ok(any("not a benchmark" in b for b in spec_problems(schema.load(p))), "JA2c an unknown benchmark id")
    safe(ja2, "JA2")

    def ja3():
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d)
            P, meta = build(schema.load(p))
            g = [t for t in P.targets if t.kind == "group"]
            u = [t for t in P.targets if t.kind == "unit"]
            ok(len(g) == 3 and len(u) == 12 and all(t.sd_obs > 0 and t.sd_md == 0.05 for t in g)
               and meta["windows"] == {"jc0": [1.0, 2.0]} and u[0].observable == "jc0.prev",
               "JA3a unit and group targets built from the spec: sampling sd from the table's interval, the declared "
               "discrepancy, one window key per distinct window")
            P2, meta2 = build(schema.load(p), units=[str(i) for i in range(6)])
            ok(len([t for t in P2.targets if t.kind == "group"]) == 0 and len(meta2["dropped"]) == 3,
               "JA3b with a subset of units, a group target missing some of its units is dropped and listed")
    safe(ja3, "JA3")

    def ja4():
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d)
            rep = run(schema.load(p), d, {"max_workers": 2}, log=q, **fast)
            ok(rep["status"] == "matched" and rep["n_matched"] == 12 and rep["sample"] is not None,
               f"JA4a the toy engine through the analysis batch: every unit matched ({summary(rep)})")
            ok((rep.get("weighting") or {}).get("best_point") == "per_unit" and not rep.get("bias"),
               f"JA4d Codex r1 P2-6: a spec without the new options keeps the per-unit best point and no offsets "
               f"({rep.get('weighting')})")
            ok(os.path.exists(os.path.join(d, OUT_REL, "call1", "results.jsonl")),
               "JA4b the simulations went through the analysis batch (scripts/compute.py), with provenance")
            s = region_sample(rep)
            from toolkit.analysis import calibration as C
            ok(s["names"] == ["base"] + [f"z:{i}" for i in range(12)] and C.check_provenance(s, C.draws(s, 5, 1)) == [],
               "JA4c the region sample is a canonical sample the uncertainty draws can cite")
    safe(ja4, "JA4")

    def ja5():
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d, unreachable="5", group=False)
            rep = run(schema.load(p), d, {"max_workers": 2}, log=q, **fast)
            hits = block_hits(rep)
            ok(rep["blocked"] and "5" in rep["unmatched"] and any(h["unit"] == "5" and h["direction"] ==
                                                                   "target_above_reach" for h in hits),
               f"JA5 a unit whose target the toy cannot reach is unmatched and blocks, with the direction ({hits[:2]})")
    safe(ja5, "JA5")

    def ja6():           # Codex r2 P1-3: a parameter the draws vary is calibrated jointly or declared independent
        import yaml
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d)
            raw = yaml.safe_load(open(p))
            raw["calibration"]["joint"]["independent"] = [x for x in raw["calibration"]["joint"]["independent"]
                                                          if x["parameter"] != "eff_a"]
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            bad = spec_problems(schema.load(p))
            ok(any("eff_a" in b and "independent" in b for b in bad),
               f"JA6a a drawn parameter neither calibrated jointly nor declared independent is refused ({bad})")
            raw["calibration"]["joint"]["shared"] = ["base", "eff_a"]
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            bad = spec_problems(schema.load(p))
            P, _ = build(schema.load(p))
            ok(not bad and [x.name for x in P.shared] == ["base", "eff_a"] and P.shared[1].lo == 0.3,
               f"JA6b an engine-fitted parameter can be calibrated jointly, over its card range ({bad})")
    safe(ja6, "JA6")

    def ja7():           # 2026-11-06: benchmark targets run through the compute backend, equal to the runner's own
        from toolkit.analysis import run as RUN
        from toolkit.engines import benchmark as BM
        pack = dict(RUN._toy_pack(), fit_joint={"targets": ["T2"]})
        global _benchmarks
        real = _benchmarks
        _benchmarks = lambda eng: (pack, BM.fit_targets(pack), None)  # noqa: E731
        try:
            with tempfile.TemporaryDirectory() as d:
                import yaml
                p = _toy_joint(d, group=False)
                raw = yaml.safe_load(open(p))
                raw["calibration"]["joint"]["targets"].append({"id": "T2", "kind": "benchmark"})
                yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
                spec = schema.load(p)
                P, meta = build(spec)
                sim = Simulator(spec, d, meta, {"max_workers": 2})
                pts = [{"kind": "shared", "theta": {"base": b}, "seed": 1} for b in (0.35, 0.6)]
                got = sim(pts)
                ref = []
                for b in (0.35, 0.6):
                    R = BM.Runner(pack, spec.engine(), 1, base_params=dict(theta0(spec), base=b))
                    R.calibrate_settings({k: v for k, v in theta0(spec).items() if k in ("eff_a", "eff_b")})
                    ref.append(R.evaluate(pack["benchmarks"][0], {k: v for k, v in theta0(spec).items()
                                                                    if k in ("eff_a", "eff_b")})["computed"])
                ok(all(abs(g["bench.T2"] - r) < 1e-9 for g, r in zip(got, ref))
                   and any(f.startswith("bench_call") for f in os.listdir(os.path.join(d, OUT_REL))),
                   f"JA7 benchmark targets run through the compute backend and equal the runner's own values "
                   f"({[g['bench.T2'] for g in got]} vs {ref})")
                pack["fit_joint"] = {"targets": []}
                try:
                    sim(pts)
                    ok(False, "JA7b a benchmark the pack holds out is refused at simulation time too")
                except HM.CalibrationInputError:
                    ok(True, "JA7b a benchmark the pack holds out is refused at simulation time too")
        finally:
            _benchmarks = real
    safe(ja7, "JA7")

    def ja8():           # 2026-11-06: earlier runs reused only where their batch provenance matches
        import shutil
        import yaml
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as pd_:
            p = _toy_joint(d, group=False)
            raw = yaml.safe_load(open(p))
            raw["calibration"]["joint"]["record"] = [{"window": [0, 3], "metric": "prev"}]
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            spec = schema.load(p)
            rep = run(spec, d, {"max_workers": 2}, log=q, **fast)
            ok(all(any(k.endswith(".prev") and k != "jc0.prev" for k in r["obs"]) for r in rep["runs"]["unit"]),
               "JA8a a recorded window metric is kept with every run")
            shutil.copytree(os.path.join(d, OUT_REL), os.path.join(pd_, "jc_rows"))
            json.dump(rep["runs"], open(os.path.join(pd_, "runs.json"), "w"))
            yaml.safe_dump(HM_plain(artifact(rep)), open(os.path.join(pd_, "joint_calibration.yaml"), "w"))
            P, meta = build(spec)
            pr, info = load_prior(spec, pd_, meta, log=q)
            ok(info["reused"] == len(rep["runs"]["unit"]) and info["unmatched"] == 0,
               f"JA8b every earlier run whose batch row matches is reused ({info})")
            runs = json.load(open(os.path.join(pd_, "runs.json")))
            runs["unit"][3]["z"] = runs["unit"][3]["z"] + 0.01          # a run that no stored row computed
            json.dump(runs, open(os.path.join(pd_, "runs.json"), "w"))
            pr2, info2 = load_prior(spec, pd_, meta, log=q)
            ok(info2["unmatched"] >= 1 and all(r["z"] != runs["unit"][3]["z"] for r in pr2["unit"]),
               f"JA8c a run its stored row does not match is not reused ({info2['unmatched']} dropped)")
            with tempfile.TemporaryDirectory() as d2:
                p2 = _toy_joint(d2, group=False)
                shutil.copy(p, p2)
                rep2 = run(schema.load(p2), d2, {"max_workers": 2}, log=q, prior_dir=pd_, waves=0, **fast)
                ok(rep2["prior_runs"]["reused"] == info2["reused"] and rep2["waves"][0].get("prior_runs"),
                   "JA8d a calibration from earlier runs alone (no new wave)")
    safe(ja8, "JA8")

    def ja9():           # Codex r3 P1-1/P1-2/P1-3, P2-4/P2-6/P2-10
        import yaml
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as o1:
            p = _toy_joint(d, group=False)
            spec = schema.load(p)
            rep = run(spec, d, {"max_workers": 2}, log=q, **fast)
            _write_out(rep, d, o1)
            P, meta = build(spec)
            pr, info = load_prior(spec, o1, meta, log=q)
            ok(info["reused"] == len(rep["runs"]["unit"]),
               f"JA9a the CLI's own output directory is a valid earlier-runs source ({info})")
            runs = json.load(open(os.path.join(o1, "runs.json")))
            true_v = runs["unit"][2]["obs"]["jc0.prev"]
            runs["unit"][2]["obs"]["jc0.prev"] = 0.999
            runs["unit"][4]["seed"] = None
            json.dump(runs, open(os.path.join(o1, "runs.json"), "w"))
            pr2, info2 = load_prior(spec, o1, meta, log=q)
            vals = [r["obs"]["jc0.prev"] for r in pr2["unit"]]
            ok(0.999 not in vals and true_v in vals and info2["unmatched"] >= 1,
               "JA9b observations come from the authenticated batch rows, never from runs.json; a run whose job "
               "cannot be rebuilt is dropped")
            raw = yaml.safe_load(open(p))
            raw["calibration"]["joint"]["targets"][0]["window"] = [0.5, 2.5]       # the same name, another window
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            spec2 = schema.load(p)
            P2, meta2 = build(spec2)
            pr3, info3 = load_prior(spec2, o1, meta2, log=q)
            ok(info3["reused"] == 0, f"JA9c earlier values of another window are never read as this window's ({info3})")
            raw["calibration"]["joint"]["targets"][0]["window"] = [1, 2]
            raw["calibration"]["scale"] = "log" if raw["calibration"].get("scale") == "linear" else "linear"
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            try:
                check_same_simulator(spec, schema.load(p))
                ok(False, "JA9d a second setting that changes what the runs simulate is refused")
            except HM.CalibrationInputError:
                ok(True, "JA9d a second setting that changes what the runs simulate is refused")
    safe(ja9, "JA9")

    def ja10():          # Codex r4 P1: an earlier shared parameter now held cannot carry its old values in
        import yaml
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as o1:
            p = _toy_joint(d, group=False)
            raw = yaml.safe_load(open(p))
            raw["uncertainty"]["priors"]["k"] = {"dist": "uniform", "range": [0.6, 1.0], "source": "toy range"}
            raw["calibration"]["joint"]["shared"] = ["base", "k"]
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            rep = run(schema.load(p), d, {"max_workers": 2}, log=q, **fast)
            _write_out(rep, d, o1)
            raw["calibration"]["joint"]["shared"] = ["base"]
            raw["calibration"]["joint"]["independent"].append({"parameter": "k", "reason": "toy: held"})
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            spec2 = schema.load(p)
            P2, meta2 = build(spec2)
            pr, info = load_prior(spec2, o1, meta2, log=q)
            ok(info["reused"] == 0, f"JA10 runs simulated with a now-held parameter varied are not reused ({info})")
    safe(ja10, "JA10")

    def ja11():          # 2026-11-06 owner decision: unmatched units may proceed flagged, never silently
        import yaml
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d)
            raw = yaml.safe_load(open(p))
            for bad, why in (({"mode": "maybe"}, "mode"), ({"mode": "flag"}, "default_reason"),
                             ({"mode": "flag", "default_reason": "r", "max_units": -1}, "max_units"),
                             ({"mode": "flag", "default_reason": "r", "max_units": 3,
                               "reasons": [{"region": "r0"}]}, "reasons"), ("flag", "mapping")):
                raw["calibration"]["joint"]["unmatched"] = bad
                yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
                ok(any("unmatched" in b and why in b for b in spec_problems(schema.load(p))),
                   f"JA11a a malformed unmatched option is refused ({why})")
            raw["calibration"]["joint"]["unmatched"] = {
                "mode": "flag", "max_units": 2, "default_reason": "toy default",
                "reasons": [{"region": "r1", "reason": "toy region reason"}, {"unit": "5", "reason": "toy unit reason"}]}
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            spec = schema.load(p)
            ok(spec_problems(spec) == [], "JA11b a well-formed flag option is accepted")
            rep = {"blocked": True, "unmatched": ["5"], "conflicting": [], "blocks": [
                {"code": "unmatched_units", "detail": {"unmatched": ["5"], "conflicting": []}},
                {"code": "verification_failed", "detail": {"units": ["7"], "targets": []}}],
                   "units": {"5": {"status": "unmatched", "direction": "target_above_reach", "best_implausibility": 9.0},
                             "7": {"status": "verification_failed", "implausibility_verified": 3.4},
                             "1": {"status": "matched"}}}
            dec = unmatched_decision(spec, rep)
            by = {f["unit"]: f for f in dec["flagged"]}
            ok(dec["proceed"] and set(by) == {"5", "7"} and by["5"]["reason"] == "toy unit reason"
               and by["7"]["reason"] == "toy region reason" and by["5"]["status"] == "unmatched",
               f"JA11c flag mode: unit-level misses proceed flagged, each with its recorded reason ({dec})")
            rep3 = dict(rep, units=dict(rep["units"], **{"9": {"status": "verification_failed"}}))
            rep3["blocks"][1]["detail"]["units"] = ["7", "9"]
            ok(not unmatched_decision(spec, rep3)["proceed"], "JA11d more misses than max_units still block")
            rep4 = dict(rep, blocks=rep["blocks"] + [{"code": "emulator_inadequate", "detail": {}}])
            ok(not unmatched_decision(spec, rep4)["proceed"], "JA11e a non-unit block (emulator) is never flagged through")
            for blk, why in (([{"code": "verification_failed"}], "no detail"),
                             ([{"code": "verification_failed", "detail": {"units": ["7"]}}], "no target list"),
                             ([{"code": "verification_failed", "detail": {"units": [], "targets": []}}], "names none"),
                             ([rep["blocks"][1]], "unit 5 missed but not named")):
                rep5 = dict(rep, blocks=blk)
                ok(not unmatched_decision(spec, rep5)["proceed"],
                   f"JA11g an incomplete or inconsistent blocked report never proceeds ({why})")
            raw["calibration"]["joint"].pop("unmatched")
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            ok(not unmatched_decision(schema.load(p), rep)["proceed"], "JA11f without the option, misses block")
    safe(ja11, "JA11")

    def ja12():          # 2026-11-06 owner decision: one primary setting; others are reported sensitivities
        import yaml
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d, group=False)
            raw = yaml.safe_load(open(p))
            raw["calibration"]["joint"]["sensitivity"] = [{"id": "tight", "label": "unit target sd 0.05",
                                                           "targets": {"unit_t": {"sd": 0.05}}}]
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            spec = schema.load(p)
            ok(spec_problems(spec) == [], "JA12a a sensitivity declaration is accepted")
            rep = run(spec, d, {"max_workers": 2}, log=q, **fast)
            s = (rep.get("sensitivity") or [{}])[0]
            ok(rep["primary"]["targets"]["unit_t"]["sd"] == 0.1 and s.get("id") == "tight"
               and s.get("report", {}).get("n_units") == 12 and "blocked" in s.get("report", {})
               and s["report"]["prior_runs"]["reused"] == len(rep["runs"]["unit"]),
               "JA12b the primary setting is named; the sensitivity runs on every primary run and is reported, "
               "never a block")
            raw["calibration"]["joint"]["sensitivity"][0]["targets"] = {"nope": {"sd": 0.1}}
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            ok(any("sensitivity" in b for b in spec_problems(schema.load(p))), "JA12c an unknown target id is refused")
            real = HM.calibrate

            def boom(*a, **k):
                raise RuntimeError("toy sensitivity failure")
            HM.calibrate = boom
            try:
                out = run_sensitivities(spec, rep, d, {"max_workers": 2}, log=q)
            finally:
                HM.calibrate = real
            ok(len(out) == 1 and "toy sensitivity failure" in out[0].get("error", "") and "report" not in out[0],
               "JA12d a sensitivity that fails is reported with its error; the primary is untouched")
            ok("sensitivity" in summary(dict(rep, sensitivity=out)), "JA12e the summary carries a failed sensitivity")
            real_b = build

            def build_fails(*a, **k):
                raise OSError("toy rebuild failure")
            globals()["build"] = build_fails
            try:
                out2 = run_sensitivities(spec, rep, d, {"max_workers": 2}, log=q)
            except Exception as e:  # noqa: BLE001
                out2 = [{"error": f"escaped: {e}"}]
            finally:
                globals()["build"] = real_b
            ok(len(out2) == 1 and "toy rebuild failure" in out2[0].get("error", "")
               and not out2[0]["error"].startswith("escaped"),
               "JA12f a failure rebuilding the problem is reported for the sensitivity and never stops the primary")
    safe(ja12, "JA12")

    def ja13():          # Codex r5 P2-3, P2-5: refuse declarations the code would not apply as written
        import yaml
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d, group=False)
            raw = yaml.safe_load(open(p))
            base = {"mode": "flag", "max_units": 2, "default_reason": "toy"}
            for rs in ([{"unit": "5", "region": "r1", "reason": "both"}], [{"unit": "", "reason": "empty"}],
                       {"unit": "5", "reason": "not a list"}):
                raw["calibration"]["joint"]["unmatched"] = dict(base, reasons=rs)
                yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
                ok(any("reasons" in b for b in spec_problems(schema.load(p))),
                   f"JA13a a reason selector that is not exactly one unit or region is refused ({rs})")
            raw["calibration"]["joint"].pop("unmatched")
            t = next(t for t in raw["calibration"]["joint"]["targets"] if t["id"] == "unit_t")
            t.pop("sd", None)
            t["sd_column"] = "sd"
            raw["calibration"]["joint"]["sensitivity"] = [{"id": "x", "label": "x", "targets": {"unit_t": {"sd": 0.05}}}]
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            ok(any("sd cannot override" in b for b in spec_problems(schema.load(p))),
               "JA13b an sd override on a target whose error comes from its data is refused")
            raw["calibration"]["joint"]["sensitivity"] = [{"id": "x/../y", "label": "x",
                                                           "targets": {"unit_t": {"bias_sd": 0.05}}}]
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            ok(any(".id" in b for b in spec_problems(schema.load(p))), "JA13c a sensitivity id must be a plain name")
    safe(ja13, "JA13")

    def ja14():          # Codex r7 P1: carried emulator failures follow their window when names change
        w1 = {"jc0": [1.0, 2.0], "jc1": [3.0, 4.0]}
        w2 = {"jc0": [3.0, 4.0], "jc1": [1.0, 2.0]}           # the targets reordered: the names swap
        runs = {"unit": [{"unit": "a", "obs": {"jc0.prev": 0.1, "jc1.prev": 0.2},
                          "failed_checks": {"jc0.prev": [0]}}],
                "shared": [{"obs": {"bench.B": 1.0}, "failed_checks": {"jc0.prev": [0], "bench.B": [0]}}],
                "unresolved_checks": {"jc0.prev": [32], "bench.B": [8]}}
        c = carry_runs(runs, w1, w2)
        ok(c["unit"][0]["obs"] == {"jc1.prev": 0.1, "jc0.prev": 0.2}
           and c["unit"][0]["failed_checks"] == {"jc1.prev": [0]}
           and c["shared"][0]["failed_checks"] == {"jc1.prev": [0], "bench.B": [0]}
           and c["unresolved_checks"] == {"jc1.prev": [32], "bench.B": [8]},
           f"JA14a a carried failure is renamed with its observations ({c['unresolved_checks']})")
        c2 = carry_runs(runs, w1, {"jc0": [3.0, 4.0]})
        ok("unmapped:jc0.prev" in c2["unresolved_checks"] and "jc0.prev" not in c2["unresolved_checks"],
           "JA14b a carried failure whose window is gone keeps a name no observable has, so it never clears")
        P, sim, *_ = HM._toy()
        h = HM.HistoryMatch(P, sim, 1, q, prior={"unit": [], "shared": [], "unresolved_checks": {"unmapped:x": [4]}},
                            points_per_unit=4, candidates_log2=8)
        h._reset_checks()
        h._restore_checks()
        ok("unmapped:x" in h._inadequate(), "JA14c such a failure blocks the continuation")
    safe(ja14, "JA14")

    def ja15():          # F6 S2-3: a shared offset per (source, group), from the spec
        import yaml
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d, group=False)
            raw = yaml.safe_load(open(p))
            j = raw["calibration"]["joint"]
            j["targets"][0].update(bias_sd=0.2, bias_by="region", bias_source="survey_a")
            j["targets"].append({k: v for k, v in j["targets"][0].items() if not k.startswith("bias")}
                                | {"id": "unit_t2", "window": [2, 3]})
            j["best_point_weighting"] = "per_group"
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            r3 = yaml.safe_load(open(p))      # Codex r1 P2-4: a second target of the same survey shares its offset
            r3["calibration"]["joint"]["targets"].append(dict(j["targets"][0], id="unit_t3"))
            p3 = os.path.join(d, "s3.yaml")
            yaml.safe_dump(r3, open(p3, "w"), sort_keys=False)
            P, _ = build(schema.load(p3))
            key = {t.id: getattr(t, "bias_key", None) for t in P.targets}
            ok(key.get("unit_t:0") == key.get("unit_t3:0") == "survey_a|r0" and key.get("unit_t2:0") is None,
               f"JA15a bias_by with bias_source: one offset per survey and region, shared by the survey's targets "
               f"({key.get('unit_t:0')}, {key.get('unit_t3:0')}, {key.get('unit_t2:0')})")
            bad_cases = [("bias_by names no units column", lambda j: j["targets"][0].update(bias_by="nope")),
                         ("bias_by without a bias sd", lambda j: j["targets"][0].update(bias_sd=0.0)),
                         ("bias_by on a group target", lambda j: j["targets"].append(
                             {"id": "g", "kind": "group", "csv": "x.csv", "value": "v", "weights": "pop", "sd": 0.1,
                              "key": {"csv": "region", "units": "region"}, "window": [1, 2], "metric": "prev",
                              "source": "t", "bias_sd": 0.1, "bias_by": "region"})),
                         ("one survey with two bias sds", lambda j: j["targets"].append(
                             dict(j["targets"][0], id="unit_t3", bias_sd=0.3))),
                         ("one survey grouped by two columns", lambda j: j["targets"].append(
                             dict(j["targets"][0], id="unit_t3", bias_by="grp"))),
                         ("bias_source without bias_by", lambda j: j["targets"][0].pop("bias_by")),
                         ("two surveys with offsets for the same units (one block per unit)",
                          lambda j: j["targets"][1].update(bias_sd=0.2, bias_by="region", bias_source="s_b")),
                         ("an unknown best_point_weighting", lambda j: j.update(best_point_weighting="per_x"))]
            for why, mut in bad_cases:
                r2 = yaml.safe_load(open(p))
                mut(r2["calibration"]["joint"])
                p2 = os.path.join(d, "s2.yaml")
                yaml.safe_dump(r2, open(p2, "w"), sort_keys=False)
                bad = spec_problems(schema.load(p2))
                ok(bool(bad), f"JA15b refused: {why} ({bad[:2]})")
            rep = run(schema.load(p), d, {"max_workers": 2}, log=q, **fast)
            ok(set(rep.get("bias") or {}) == {f"survey_a|r{k}" for k in range(3)}
               and (rep.get("weighting") or {}).get("best_point") == "per_group",
               f"JA15c the toy run reports each block's offset and uses the signed per_group weighting "
               f"({rep.get('weighting')}, {summary(rep)})")
    safe(ja15, "JA15")

    def ja16():          # F6 S2-3b: rounds of one survey paired by group, from the spec
        import yaml
        with tempfile.TemporaryDirectory() as d:
            p = _toy_joint(d)
            rows = list(csv.DictReader(open(os.path.join(d, "units.csv"))))
            with open(os.path.join(d, "units.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]) + ["tgt2"])
                w.writeheader()
                w.writerows([dict(r, tgt2=f"{float(r['tgt']) * 0.8:.5f}") for r in rows])
            with open(os.path.join(d, "region_late.csv"), "w") as f:
                f.write("region,level,prev,lo,hi\nr0,region,0.3,0.25,0.35\nr1,region,0.3,0.25,0.35\n")
            raw = yaml.safe_load(open(p))
            j = raw["calibration"]["joint"]
            P0, _ = build(schema.load(p))
            nc0 = [c for c in P0.changes if "not_computable" in c]
            ok(len(P0.changes) == 1 == len(nc0) and "survey" in nc0[0]["not_computable"],
               f"JA16a no target names its survey: one explicit not-computable entry ({P0.changes})")
            j["targets"][0].update(bias_sd=0.2, bias_by="region", bias_source="s")
            j["targets"].append(dict(j["targets"][0], id="unit_late", column="tgt2", window=[2, 3]))
            j["targets"][1]["survey"] = "g"
            j["targets"].append(dict(j["targets"][1], id="region_late", csv="region_late.csv", window=[2, 3]))
            j["changes"] = {"weights": "pop"}
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            P, _ = build(schema.load(p))
            ch = {c["id"]: c for c in P.changes}
            us = [c for c in ch.values() if c.get("survey") == "s" and "terms" in c]
            pop = {r["id"]: float(r["pop"]) for r in rows}
            w0 = {x["from"].split(":")[1]: x["w"] for x in us[0]["terms"]} if us else {}
            ok(len(us) == 3 and all(len(c["terms"]) == 4 and c["from"] == "unit_t" and c["to"] == "unit_late"
                                    for c in us)
               and abs(sum(w0.values()) - 1) < 1e-9
               and abs(w0[min(w0)] / w0[max(w0)] - pop[min(w0)] / pop[max(w0)]) < 1e-9,
               f"JA16b two unit rounds of one survey: one pair per group (the survey's bias_by), population weights "
               f"({ {k: len(c.get('terms') or []) for k, c in ch.items()} })")
            gs = {c.get("group"): c for c in ch.values() if c.get("survey") == "g"}
            ok(set(gs) == {"r0", "r1", "r2"} and "terms" in gs["r0"] and "region_late" in gs["r2"].get(
                "not_computable", ""),
               f"JA16c group rounds pair by group; a group missing from one round is not computable, with the reason "
               f"({ {g: c.get('not_computable', 'ok') for g, c in gs.items()} })")
            r2 = yaml.safe_load(open(p))
            t2 = r2["calibration"]["joint"]["targets"]
            t2[2]["scale"] = "identity"
            t2.append(dict(t2[0], id="unit_once", survey="once", window=[1, 3]))
            t2[-1].pop("bias_by")
            t2[-1].pop("bias_source")
            t2[-1].pop("bias_sd")
            p2 = os.path.join(d, "s2.yaml")
            yaml.safe_dump(r2, open(p2, "w"), sort_keys=False)
            P2, _ = build(schema.load(p2))
            why = {c.get("survey"): c.get("not_computable", "") for c in P2.changes}
            ok("scale" in why.get("s", "") and "single round" in why.get("once", ""),
               f"JA16d different definitions (scale) and a single round are not computable, with the reason ({why})")
            bad_cases = [("changes.by names no units column", lambda j: j["changes"].update(by="nope")),
                         ("a sampling correlation outside [-1, 1]",
                          lambda j: j["changes"].update(sampling_correlation=1.5)),
                         ("an unknown changes key", lambda j: j["changes"].update(colour=1)),
                         ("a survey that is not text", lambda j: j["targets"][0].update(survey=3))]
            for why_, mut in bad_cases:
                r3 = yaml.safe_load(open(p))
                mut(r3["calibration"]["joint"])
                yaml.safe_dump(r3, open(p2, "w"), sort_keys=False)
                ok(bool(spec_problems(schema.load(p2))), f"JA16e refused: {why_}")
            rep = run(schema.load(p), d, {"max_workers": 2}, log=q, **fast)
            sc = rep.get("survey_changes") or {}
            got = [x for x in sc.get("pairs") or [] if "z" in x]
            ok(sc.get("status") == "computed" and len(got) == 5 and all(
                set(x) >= {"observed", "modelled", "difference", "sd", "z", "flagged", "text"} for x in got)
               and artifact(rep).get("survey_changes") == sc,
               f"JA16f the toy run writes each group's change into the calibration artifact ({sc.get('status')}, "
               f"{[(x['id'], x.get('z'), x.get('not_computable')) for x in sc.get('pairs') or []]})")
    safe(ja16, "JA16")

    def ja17():          # the gate findings of the change diagnostic
        pair = {"id": "s:a->b:r0", "survey": "s", "group": "r0", "from": "a", "to": "b", "observed": -0.4,
                "modelled": 0.1, "difference": 0.5, "sd": 0.1, "z": 5.0, "flagged": True,
                "text": "the model's change has the opposite sign of the observed one"}
        data = {"survey_changes": {"status": "computed", "z_max": 3.0, "pairs": [pair], "flagged": [pair["id"]]}}
        raw = {"calibration": {"joint": {}}}
        f = change_findings(raw, data)
        ok(len(f) == 1 and f[0]["scope_declarable"] and "opposite sign" in f[0]["claim"] and "r0" in f[0]["claim"],
           f"JA17a a flagged group is one declarable gate finding that states the direction ({f})")
        f = change_findings(raw, {})
        ok(len(f) == 1 and not f[0]["scope_declarable"], f"JA17b no change diagnostic in the artifact: a finding ({f})")
        f = change_findings(raw, {"survey_changes": dict(data["survey_changes"], pairs=[dict(pair, flagged=False)])})
        ok(len(f) == 1 and not f[0]["scope_declarable"],
           f"JA17c a pair whose numbers exceed the threshold but is not flagged: a finding ({f})")
        nc = {"survey_changes": {"status": "not_computable", "z_max": 3.0, "flagged": [],
                                 "pairs": [{"id": "s", "survey": "s", "not_computable": "a single round (a)"}]}}
        f0 = change_findings(raw, nc)
        f1 = change_findings({"calibration": {"joint": {"changes": {}}}}, nc)
        ok(f0 == [] and len(f1) == 1 and "single round" in f1[0]["claim"],
           f"JA17d not computable is recorded; a finding only when the spec asks for the diagnostic ({f0}, {f1})")
    safe(ja17, "JA17")

    def ja18():          # Codex r1 P2-2 to P2-5: groups, weights and gate arithmetic, fail closed
        class T_:
            def __init__(self, i):
                self.id, self.kind = i, "group"
        j = {"targets": [{"id": "a", "kind": "group", "survey": "s", "window": [1, 2], "metric": "m",
                          "key": {"csv": "g", "units": "g"}, "weights": "pop"},
                         {"id": "b", "kind": "group", "survey": "s", "window": [2, 3], "metric": "m",
                          "key": {"csv": "g", "units": "g"}, "weights": "pop"}]}
        rows = {"1": {"g": "G", "pop": "1"}, "2": {"g": "H", "pop": "1"}}
        got = {c.get("group"): c for c in change_pairs(j, rows, ["1", "2"], [T_("a:G"), T_("b:G")], None)}
        ok(set(got) == {"G", "H"} and "not_computable" in got["H"],
           f"JA18a a group of the units table absent from both rounds is not computable, not dropped ({got})")
        ju = {"targets": [{"id": "a", "kind": "unit", "survey": "s", "window": [1, 2], "metric": "m", "column": "x"},
                          {"id": "b", "kind": "unit", "survey": "s", "window": [2, 3], "metric": "m", "column": "y"}],
              "changes": {"weights": "pop", "by": "g"}}
        for bad_w in ("nan", "-5"):
            rows = {"1": {"g": "G", "pop": "1"}, "2": {"g": "G", "pop": bad_w}}
            got = change_pairs(ju, rows, ["1", "2"], [], None)
            ok(len(got) == 1 and "not_computable" in got[0], f"JA18b a weight of {bad_w} is not computable ({got})")
        pair = {"id": "p", "survey": "s", "group": "G", "from": "a", "to": "b", "observed": 0.0, "modelled": 10.0,
                "difference": 0.0, "sd": 0.1, "z": 0.0, "flagged": False, "text": "t"}
        raw = {"calibration": {"joint": {"changes": {}}}}
        f1 = change_findings(raw, {"survey_changes": {"z_max": 3.0, "pairs": [pair]}})
        f2 = change_findings(raw, {"survey_changes": {"z_max": 3.0, "pairs": []}})
        ok(len(f1) == 1 and not f1[0]["scope_declarable"] and len(f2) == 1 and not f2[0]["scope_declarable"],
           f"JA18c a difference that is not modelled - observed, and an empty record, are findings ({f1}, {f2})")
    safe(ja18, "JA18")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
