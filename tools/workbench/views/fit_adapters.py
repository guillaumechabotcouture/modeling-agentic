#!/usr/bin/env python3
"""The Fit view's generic model and its adapters (docs/design/review-workbench.md §3a, §5.5).

    python3 tools/workbench/views/fit_adapters.py --self-test

`model(run)` (run is a workbench.Run) returns (the generic model, None) or (None, why). An adapter, chosen by the
artifact's schema and data.method, maps what the calibration method wrote into the one model the checks and charts
read: analysis/joint_calibration.yaml (history_matching) when the run has it, else analysis/calibration.yaml
(posterior). Any other schema or method is named and never guessed at. The model holds `facts` (the seventeen names
of §3a; None where the method records none), `tables` (the seven of §3a; None where not recorded), `notes`, and what
the checks need beside them (the file and fields read, declarations, the words this method uses).

Reads go through the Run readers only; no engine and no harness loader is imported. Words (group, target and
parameter names, the grouping column) come from the run's files. This module also holds the names and thresholds
the Fit modules share. Their self-tests share the harness in views/fit_bench.py (fixtures.py builds the runs).
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402  (the sign-off record's selection and the decision tests' loader, shared by every view)

FACTS = ("method", "status", "proceeded", "n_targets_group", "n_targets_unit", "n_parameters", "region_accepted",
         "region_candidates", "region_share_of_prior", "cutoff", "change_limit", "engine_replicates", "units_at_bound",
         "n_units", "grouping", "rhat_max", "ess_min")
COLUMNS = {
    "targets": ["id", "level", "group", "window", "source", "observed", "low", "high", "sd_total", "model", "model_alt",
                "score", "score_alt", "status"],
    "parameters": ["name", "prior_low", "prior_high", "low", "high", "best", "contraction", "at_bound", "prior_dominated"],
    "changes": ["group", "from", "to", "observed", "modelled", "z", "flagged"],
    "blocks": ["code", "meaning", "declared", "test", "verdict"],
    "offsets": ["key", "mean", "sd", "accepted_share"],
    "sensitivity": ["setting", "status", "blocks"],
    "series": ["series", "t", "observed", "model", "low", "high"]}
GROUP_COLUMNS = ["group", "targets", "mean_signed_score", "largest_score", "model_above", "model_below"]
STATUSES = ("ok", "look", "problem", "not_available")
JC, CAL, UNC, DT, EST = ("analysis/joint_calibration.yaml", "analysis/calibration.yaml", "analysis/uncertainty.yaml",
                         "analysis/decision_tests.yaml", "analysis/estimates.yaml")
SPEC, SIGNED, SIGNOFF = "decision_spec.yaml", "decision_brief.signed/decision_spec.yaml", "decision_spec.signoff.json"
FLAG_MODE = "flag"           # calibration.joint.unmatched.mode under which the harness lets flagged units proceed
#                              (toolkit/analysis/joint_calibration.py UNMATCHED_MODES: block, flag)
# The status words the history-matching code writes (toolkit/calibration/history_matching.py: "blocked" if blocks
# else "matched"), each with the generic status of §3a. Any other word is not known and never guessed at.
HM_STATUS = {"matched": "ok", "blocked": "blocked"}
# Thresholds. NEAR and BOUND are §3's (near miss; a bound reached). The rest are workbench thresholds (§3a, §5.5),
# used only where the method records none of its own, and named as such in each sentence.
NEAR, BOUND = 0.25, 1e-6
GAP_MAX, UNIT_BOUND_SHARE, GROUP_BIAS, CONTRACTED, RHAT_MAX, ESS_MIN = 1.0, 0.25, 1.0, 0.5, 1.01, 400
ADAPTERS_LATER = ("likelihood", "emulator_inversion")      # slice β2 builds these
# One sentence per block code the history-matching code can emit (§3a); an unknown code is shown as itself.
BLOCK_MEANING = {
    "verification_failed": "engine runs at the best point put a target above the cutoff that the emulator had passed",
    "offset_sampling_low_acceptance": "too few offset draws were accepted for some rows of the region sample, so "
                                      "those rows were redrawn from the rows that had accepted draws",
    "emulator_inadequate": "a wave's held-out check of the emulator failed or could not run",
    "empty_region": "no candidate satisfies the shared and group targets",
    "unmatched_units": "more units than the method allows cannot match their targets at any accepted value",
    "conflicting_units": "the region was relaxed because some units are unmatched at every accepted value"}


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _get(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def _g(x, digits=4):
    """A number as the sentence shows it."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return str(x)
    return f"{x:,}" if isinstance(x, int) else f"{x:.{digits}g}"


def _cap(s):
    return s[:1].upper() + s[1:]


def _names(items, cap=6):
    items = [str(i) for i in items]
    return ", ".join(items[:cap]) + (f" and {len(items) - cap} more" if len(items) > cap else "")


def _table(name, rows, about, labels=None):
    return {"columns": list(COLUMNS[name]), "rows": rows, "about": about, "labels": labels or {}}


def _dicts(t):
    return [dict(zip(t["columns"], r)) for r in t["rows"]] if t else []


def _at_bound(lo, hi, plo, phi):
    """none, lower, upper or both: an end is on a prior bound within BOUND of the prior range's width (§3)."""
    if None in (lo, hi, plo, phi) or phi <= plo:
        return None
    tol = BOUND * (phi - plo)
    low, up = lo - plo <= tol, phi - hi <= tol
    return "both" if low and up else "lower" if low else "upper" if up else "none"


def _quantile(xs, q):
    xs = sorted(xs)
    at = q * (len(xs) - 1)
    k = int(math.floor(at))
    return xs[k] + (xs[min(k + 1, len(xs) - 1)] - xs[k]) * (at - k)


# ------------------------------------------------------------------------------------------- shared reads

def _spec(run):
    """(the spec the view reads, the file it came from, one sentence saying which copy that is, why no signed spec
    could be verified or None). §3: the signed copy is read wherever it and the spec on disk differ. It is verified
    only when its sha256 is the one the sign-off record holds for the spec; declarations and allowances count from
    a verified copy only, and a copy that is not verified is read for names alone."""
    # One read of the signed copy: the digest compared with the sign-off record is of the bytes that are parsed
    # (Run.hashed), so an allowance can only come from the document whose hash verified.
    signed, spec, why = run.hashed(SIGNED)
    disk = run.sha(SPEC)
    so, so_rel, so_why = checks.signoff_record(run)      # the producer's own selection of the record, as in every view
    want = _get(so, "sha256", SPEC)
    want = want if isinstance(want, str) and re.fullmatch(r"[0-9a-f]{64}", want) else None
    rel = SIGNED if signed else SPEC
    if not signed:
        _, spec, why = run.hashed(SPEC)
    if not signed:
        unverified = f"the run has no readable signed copy of the spec ({SIGNED})"
    elif so is None:
        unverified = f"the sign-off record could not be read ({so_why})"
    elif want is None:
        unverified = f"the sign-off record ({so_rel}) holds no sha256 for {SPEC}"
    elif want != signed:
        unverified = (f"the signed copy ({SIGNED}, {signed[:8]}) differs from the hash the sign-off record ({so_rel}) "
                      f"holds ({want[:8]})")
    else:
        unverified = None
    if unverified:
        say = (f"No signed spec could be verified: {unverified}. {rel} is read for names only, and no declaration or "
               f"allowance in it counts." if spec else f"No spec could be read ({why}).")
    elif disk and signed != disk:
        say = (f"The spec on disk ({disk[:8]}) differs from the signed copy ({signed[:8]}); this view reads the "
               f"signed copy, {SIGNED}.")
    else:
        say = f"The signed spec ({SIGNED}, {signed[:8]}) and the spec on disk are the same file."
    return (spec if isinstance(spec, dict) else {}), rel, say, unverified


def _units(run, spec):
    """(rows of the units table as dicts, its id column, its path), or ([], None, path) when it cannot be read."""
    rel, col = _get(spec, "decision_units", "csv"), _get(spec, "decision_units", "id")
    rel = rel if isinstance(rel, str) else "data/derived/units.csv"
    b, _ = run.read(rel, 32 << 20)
    if b is None:
        return [], None, rel
    try:
        rows = list(csv.DictReader(io.StringIO(b.decode("utf-8", "replace"))))
    except csv.Error:
        return [], None, rel
    col = col if rows and col in rows[0] else (next(iter(rows[0]), None) if rows else None)
    return rows, col, rel


def _tests(run):
    """{decision test id: (it ran, its verdict)} through checks.decision_tests, the loader the register uses too: a
    test ran only with status run and a verdict of no change or changes plan. A file that cannot be read that way
    gives no test, so nothing a declaration names ran."""
    tests, _ = checks.decision_tests(run)
    return {tid: (t["state"] == "ran", t["verdict"]) for tid, t in (tests or {}).items()}


def _prior_dominated(run):
    """The names analysis/uncertainty.yaml lists as prior-dominated, or None when the file does not say."""
    doc, _ = run.large(UNC)
    names = _get(doc, "data", "prior_dominated")
    return {str(n) for n in names} if isinstance(names, list) else None


def _cover(tests, test, where):
    ran, verdict = tests.get(str(test), (False, None))
    return {"test": str(test), "ran": ran, "verdict": verdict, "where": where}


def _part(why, name, fn):
    """A part of the model, or None with the reason kept: a part of the file with the wrong type must not take the
    rest of the view down."""
    try:
        return fn()
    except Exception as e:
        why[name] = f"could not be read from the file ({type(e).__name__}: {' '.join(str(e).split())[:100]})"
        return None


# ------------------------------------------------------------------------------------------- history matching

def _history_matching(run, doc):
    jc, why = doc["data"], {}
    spec, spec_rel, spec_say, unverified = _spec(run)
    tests, dominated = _tests(run), _prior_dominated(run)
    pairs = _get(jc, "survey_changes", "pairs")
    pairs = [p for p in pairs if isinstance(p, dict)] if isinstance(pairs, list) else None
    by = next((p.get("by") for p in pairs or [] if isinstance(p.get("by"), str)), None)
    grouping = by or (_get(spec, "decision_units", "region") if isinstance(_get(spec, "decision_units", "region"), str) else None)
    unit_rows, id_col, units_rel = _units(run, spec)
    group_of = {str(r.get(id_col)): r.get(grouping) for r in unit_rows} if grouping else {}
    units = jc.get("units") if isinstance(jc.get("units"), dict) else {}
    ver_t = _get(jc, "verification", "targets") if isinstance(_get(jc, "verification", "targets"), dict) else {}
    ver_u = _get(jc, "verification", "units") if isinstance(_get(jc, "verification", "units"), dict) else {}
    verified = bool(ver_t or ver_u)
    fig = {f.get("target"): f for f in _get(jc, "figure_data", "fit") or [] if isinstance(f, dict)}
    windows = _get(jc, "adapter", "windows") or {}
    cutoff, th = _num(jc.get("cutoff")), _get(jc, "thresholds", "history_matching") or {}
    first = {}

    def window(t):
        return windows.get(str(t.get("observable", "")).split(".")[0])

    def targets():
        group, unit = [], []
        for tid, t in jc["targets"].items():
            v, f = ver_t.get(tid) or {}, fig.get(tid) or {}
            first.setdefault("target", t)
            kind = "unit" if t.get("kind") == "unit" else "group"
            group.append([tid, kind, tid.split(":", 1)[1] if t.get("kind") == "group" and ":" in tid else None,
                          window(t), t.get("source"), _num(t.get("value")), _num(f.get("obs_lo")), _num(f.get("obs_hi")),
                          _num(t.get("sd_total")), _num(v.get("simulated")) if verified else _num(t.get("modelled_at_best")),
                          _num(t.get("modelled_at_best")) if verified else None,
                          _num(v.get("implausibility")) if verified else _num(t.get("implausibility_at_best")),
                          _num(t.get("implausibility_at_best")) if verified else None,
                          f.get("status") or ({True: "matched", False: "verification_failed"}.get(v.get("ok")))])
        for u, lst in jc["unit_targets"].items():
            vt = {x.get("target"): x for x in (ver_u.get(u) or {}).get("targets") or [] if isinstance(x, dict)}
            rec = units.get(u) if isinstance(units.get(u), dict) else {}
            for t in lst:
                v, f = vt.get(t["id"]) or {}, fig.get(t["id"]) or {}
                first.setdefault("unit_target", t)
                emu = _num(rec.get("implausibility_at_best")) if len(lst) == 1 else None     # the unit's one target
                unit.append([t["id"], "unit", group_of.get(str(u)), window(t), t.get("source"), _num(t.get("value")),
                             _num(f.get("obs_lo")), _num(f.get("obs_hi")), _num(t.get("sd_total")),
                             _num(v.get("simulated")), None, _num(v.get("implausibility")) if verified else emu,
                             emu if verified else None, f.get("status") or rec.get("status")])
        worst = {}
        for r in unit:
            worst[r[2]] = max(worst.get(r[2], 0.0), abs(r[11] or 0.0))
        group.sort(key=lambda r: -abs(r[11] or 0.0))
        unit.sort(key=lambda r: (-worst[r[2]], str(r[2]), -abs(r[11] or 0.0)))
        return group + unit

    def parameters():
        reg, best = jc["region"], _get(jc, "best", "theta") or {}
        rows = []
        for name, (lo, hi) in reg["ranges"].items():
            plo, phi = (_num(x) for x in reg["prior_ranges"][name])
            measures = {"region.contraction": _num(_get(reg, "contraction", name)),
                        "region.interval95.contraction95": _num(_get(reg, "interval95", name, "contraction95"))}
            widest[name] = max(((v, k) for k, v in measures.items() if v is not None), default=None)
            rows.append([name, plo, phi, _num(lo), _num(hi), _num(best.get(name)), measures["region.contraction"],
                         _at_bound(_num(lo), _num(hi), plo, phi), None if dominated is None else name in dominated])
        return rows

    def changes():
        if pairs is None:
            raise TypeError("survey_changes.pairs is not a list")
        rows = []
        for k, p in enumerate(pairs):
            # The one state in which the producer computes no pair is `not_computable` with its reason (history_matching
            # _survey_changes, change_stats). A pair with neither that nor a z and a flag is treated the same way.
            z, flag, lost = _num(p.get("z")), p.get("flagged"), p.get("not_computable")
            if lost is not None or z is None or not isinstance(flag, bool):
                uncomputed[k] = " ".join(str(lost).split()) if lost is not None else "the file records no z or no flag for it"
                z, flag = None, None
            rows.append([p.get("group"), p.get("from"), p.get("to"), _num(p.get("observed_natural")),
                         _num(p.get("modelled_natural")), z, flag])
        return rows

    # Declarations (§3, §3a): the method's own list, counted only where the verified signed spec holds the same entry.
    signed_spec = {} if unverified else spec
    signed_list = _get(signed_spec, "calibration", "joint", "unmatched", "declared")
    signed_list = [d for d in signed_list if isinstance(d, dict)] if isinstance(signed_list, list) else []
    declared = []
    for d in _get(jc, "unmatched_decision", "declared") or []:
        if isinstance(d, dict):
            key = d.get("target") if d.get("target") is not None else d.get("offset")
            declared.append({"key": str(key), "test": d.get("decision_test"), "signed": any(
                (s.get("target"), s.get("offset"), s.get("decision_test")) == (d.get("target"), d.get("offset"), d.get("decision_test"))
                for s in signed_list)})
    flagged_units = {str(f.get("unit")) for f in _get(jc, "unmatched_decision", "flagged") or [] if isinstance(f, dict)}
    conflicting = jc.get("conflicting")        # the report's own list; anything but an empty list is named
    conflicting = [str(x) for x in conflicting] if isinstance(conflicting, (list, tuple)) else ([str(conflicting)] if conflicting else [])
    mode, max_units = (_get(signed_spec, "calibration", "joint", "unmatched", k) for k in ("mode", "max_units"))
    max_units = max_units if isinstance(max_units, int) and not isinstance(max_units, bool) else None
    no_allowance = (f"no signed spec could be verified ({unverified})" if unverified else
                    f"the signed spec's unmatched mode is '{mode}', not '{FLAG_MODE}'" if mode != FLAG_MODE else
                    "the signed spec sets no max_units" if max_units is None else None)

    def strings(x):
        if isinstance(x, dict):
            return [s for v in x.values() for s in strings(v)]
        if isinstance(x, list):
            return [s for v in x for s in strings(v)]
        return [x] if isinstance(x, str) else []

    def blocks():
        rows = []
        for b in jc["blocks"]:
            det = b.get("detail")
            named, det_d = set(strings(det)), det if isinstance(det, dict) else {}
            need = [str(x) for x in det_d.get("targets") or []] + [str(x) for x in det_d.get("blocks") or []] + [
                str(x) for comp in det_d.get("by_component") or [] if isinstance(comp, dict) for x in comp.get("blocks") or []]
            cover = [d for d in declared if d["key"] in named]
            loose = [str(x) for k in ("units", "unmatched") for x in det_d.get(k) or []]
            # Conflicting units are outside the allowance: the producer lets no report that lists one proceed
            # (toolkit/analysis/joint_calibration.py unmatched_decision: `and not rep.get("conflicting")`;
            # _blocks_consistent: `d.get("conflicting") == []`).
            clash = [str(x) for x in det_d.get("conflicting") or []] if isinstance(det_d.get("conflicting"), (list, tuple)) \
                else ([str(det_d["conflicting"])] if det_d.get("conflicting") else [])
            allowed = (bool(loose) and not clash and not conflicting and set(loose) <= flagged_units and not no_allowance
                       and len(flagged_units) <= max_units)
            info = {"code": str(b.get("code")), "cover": [_cover(tests, d["test"], "unmatched_decision.declared")
                                                           for d in cover if d["signed"] and d["test"]],
                    "unsigned": [d["key"] for d in cover if not d["signed"] or not d["test"]],
                    "uncovered": [x for x in need if x not in {d["key"] for d in cover}]
                                 + ([] if allowed or not loose else [f"{len(loose)} unit(s)"])
                                 + ([f"{len(clash)} conflicting unit(s)"] if clash else [])
                                 + ([] if need or loose or clash else ["the block names no target, offset or unit a declaration could cover"]),
                    "allowance": (len(loose), max_units) if allowed else None, "conflicting": clash,
                    "no_allowance": no_allowance if loose and not allowed else None}
            block_info.append(info)
            ok = bool(info["cover"]) and not info["unsigned"] and not [x for x in need if x not in {d["key"] for d in cover}]
            rows.append([info["code"], BLOCK_MEANING.get(info["code"], info["code"]), ok,
                         ", ".join(c["test"] for c in info["cover"]) or None,
                         ", ".join(str(c["verdict"] if c["verdict"] is not None else "NOT_RUN") for c in info["cover"]) or None])
        return rows

    def change_cover():
        """[the declarations that cover each pair, in the pairs' order]: scope_declaration.yaml and findings.jsonl
        cover the one pair whose id they name; the method's list, where the signed spec holds the entry, covers the
        pairs of the group it names (§3a). With no verified signed spec nothing excuses anything (§3): every
        pair's list is empty, whatever those two files hold."""
        if unverified:
            return [[] for _ in pairs or []]
        scope, _ = run.large("scope_declaration.yaml")
        found, _ = run.read("findings.jsonl", 8 << 20)
        rows = []
        for line in (found or b"").decode("utf-8", "replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        def names(x, pid):       # the harness writes a pair's finding as change:<pair id>, after the finding's kind
            return pid is not None and str(x) in (pid, f"change:{pid}") or str(x).endswith(f":change:{pid}")
        out = []
        for p in pairs or []:
            g, pid, got = p.get("group"), (str(p["id"]) if p.get("id") is not None else None), []
            got += [_cover(tests, d["test"], "unmatched_decision.declared") for d in declared
                    if d["signed"] and d["test"] and str(g) in re.split(r"[|:]", d["key"])]
            for d in _get(scope, "declarations") or []:
                if isinstance(d, dict) and pid and names(d.get("blocker_id", ""), pid) and d.get("decision_test"):
                    got.append(_cover(tests, os.path.splitext(os.path.basename(str(d["decision_test"])))[0], "scope_declaration.yaml"))
            for r in rows:
                test = _get(r, "decision_test", "file") if isinstance(r, dict) else None
                if test and pid and r.get("status") == "declared" and (names(r.get("id"), pid) or names(r.get("key"), pid)):
                    got.append(_cover(tests, os.path.splitext(os.path.basename(str(test)))[0], "findings.jsonl"))
            out.append(got)
        return out

    def offsets():
        return [[k, _num(b.get("mean")), _num(b.get("sd")),
                 b["draws"] / b["proposed"] if _num(b.get("draws")) is not None and _num(b.get("proposed")) else None]
                for k, b in jc["bias"].items()]

    def sensitivity():
        return [[s.get("id"), _get(s, "report", "status"),
                 [b.get("code") for b in _get(s, "report", "blocks") or [] if isinstance(b, dict)]] for s in jc["sensitivity"]]

    widest, block_info, uncomputed = {}, [], {}
    t_rows, p_rows = _part(why, "targets", targets), _part(why, "parameters", parameters)
    c_rows, b_rows = _part(why, "changes", changes), _part(why, "blocks", blocks)
    o_rows, s_rows = _part(why, "offsets", offsets), _part(why, "sensitivity", sensitivity)
    reg = jc.get("region") if isinstance(jc.get("region"), dict) else {}
    runs = [_num(v.get("runs")) for v in ver_u.values() if isinstance(v, dict) and _num(v.get("runs")) is not None]
    word = jc.get("status")
    status = HM_STATUS.get(word) if isinstance(word, str) else None
    proceed = _get(jc, "unmatched_decision", "proceed")
    sc = jc.get("survey_changes") if isinstance(jc.get("survey_changes"), dict) else {}
    facts = {
        "method": "history_matching", "status": status, "proceeded": proceed if isinstance(proceed, bool) else None,
        "n_targets_group": sum(r[1] == "group" for r in t_rows) if t_rows is not None else None,
        "n_targets_unit": sum(r[1] == "unit" for r in t_rows) if t_rows is not None else None,
        "n_parameters": len(p_rows) if p_rows is not None else None,
        "region_accepted": reg.get("accepted"), "region_candidates": reg.get("candidates"),
        "region_share_of_prior": _num(reg.get("share_of_prior")), "cutoff": cutoff,
        "change_limit": _num(_get(jc, "survey_changes", "z_max")),
        "engine_replicates": int(min(runs)) if runs else None,
        "units_at_bound": sum(bool(u.get("region_touches_bound")) for u in units.values() if isinstance(u, dict)) if units else None,
        "n_units": jc.get("n_units") if isinstance(jc.get("n_units"), int) else None, "grouping": grouping,
        "rhat_max": None, "ess_min": None}

    t0, u0 = first.get("target") or {}, first.get("unit_target") or first.get("target") or {}
    metric = str((u0 or t0).get("observable", "")).split(".", 1)[-1] or "the target's metric"
    scale = (u0 or t0).get("scale") or "the target's"
    waves = [w for w in jc.get("waves") or [] if isinstance(w, dict)]
    last = waves[-1] if waves else {}
    up = sum("hi" in (u.get("region_touches_bound") or []) for u in units.values() if isinstance(u, dict))
    low = sum("lo" in (u.get("region_touches_bound") or []) for u in units.values() if isinstance(u, dict))
    notes = {
        "method": (
            f"History matching with an emulator. Design: {_g(len(waves))} wave(s) of engine runs "
            f"({_g(last.get('unit_runs'))} unit runs and {_g(last.get('shared_runs'))} shared runs in the last wave; "
            f"{_g(th.get('points_per_unit'))} points per unit), on which an emulator of each observable is fitted. "
            f"Each of {_g(reg.get('candidates'))} candidate values of the {_g(facts['n_parameters'])} shared parameters "
            f"is scored on every target by the implausibility I = |E[f] − y| / sqrt(V_em + V_obs + V_md), where E[f] "
            f"and V_em are the emulator's mean and variance, y the observed value, V_obs the target's error variance "
            f"(sampling and offset) and V_md the model discrepancy (sd {_g(_get(jc, 'primary', 'discrepancy_sd'))}), "
            f"all on the target's scale ({scale}). A candidate is accepted when every target has I at or under the "
            f"cutoff {_g(cutoff)}: {_g(reg.get('accepted'))} were accepted, and the method's own estimate of the "
            f"accepted share of the prior volume is {_g(reg.get('share_of_prior'))} (not accepted ÷ candidates: the "
            f"candidates are concentrated by the earlier waves). Each unit also has its own parameter "
            f"({_get(jc, 'unit_parameter', 'name')}, from {_g(_get(jc, 'unit_parameter', 'lo'))} to "
            f"{_g(_get(jc, 'unit_parameter', 'hi'))}, {_get(jc, 'unit_parameter', 'scale')} scale); "
            f"{_g(facts['units_at_bound'])} of {_g(facts['n_units'])} units have an accepted interval that reaches a "
            f"bound of it ({up} the upper, {low} the lower). Verification: the engine is then run at the best point "
            f"({_g(facts['engine_replicates'])} run(s) per unit) and each target's I is recomputed from the engine's "
            f"value; a target above the cutoff there is 'verification failed'. The method's own status word is {word!r}."),
        "score": (
            f"score is the implausibility I of a target at the best point computed from the engine's verification run "
            f"(model is that engine value); score_alt is I from the emulator at the same point (its value, model_alt, is "
            f"recorded for group targets only). I is unsigned and on the target's scale ({scale}); a target passes at or under the "
            f"cutoff {_g(cutoff)}. Where a check needs a direction it gives I the sign of model − observed."),
        "units_of_measure": (
            f"observed, low, high, model and model_alt are {metric} in its natural unit (a share when the scale is "
            f"logit); sd_total is on the {scale} scale. low and high are obs_lo and obs_hi of figure_data.fit: from "
            f"the assumed error, not the survey's own interval (gap H3)."),
        "spec": spec_say,
        "other_file": (f"The run also holds {CAL} (data.method {_get(run.large(CAL)[0], 'data', 'method')}); it is read "
                       f"through the joint file {JC}." if run.sha(CAL) else f"Read from {JC}; the run holds no {CAL}."),
        "not_recorded": "This method (history_matching) records no series (a fit over time with a predictive band) and "
                        "no sampler diagnostics (rhat_max, ess_min).",
        "harness_figures": "The harness figure beside each chart is not shown yet: it needs the file endpoint of goal G2b."}
    for name, reason in why.items():
        notes[f"{name}_not_available"] = f"The {name} table {reason}."
    about = {
        "targets": (f"One row per calibration target: group targets first (JC.targets), then unit targets "
                    f"(JC.unit_targets), each list with the largest score first; type 'group' or 'unit', a group name "
                    f"or a status in the filter box. model and score are the engine's (JC.verification); model_alt and "
                    f"score_alt the emulator's at the same point. group is the suffix of a group target's id, or the "
                    f"unit's value of the column '{grouping}' in {units_rel}. window is the model window in years "
                    f"(JC.adapter.windows)."),
        "parameters": ("One row per shared parameter: prior range (JC.region.prior_ranges), accepted range "
                       "(JC.region.ranges), best point (JC.best.theta), contraction (JC.region.contraction: 1 − accepted "
                       f"width ÷ prior width), whether an end of the accepted range is on a prior bound, and whether {UNC} "
                       "lists the parameter as prior-dominated."),
        "changes": ("One row per group and pair of rounds (JC.survey_changes.pairs): the observed and modelled change "
                    f"in {metric} (later minus earlier, natural unit), z = (modelled − observed change) ÷ sd on the "
                    f"targets' scale, and whether the method flagged it against its limit {_g(facts['change_limit'])}."),
        "blocks": ("One row per reason the method gave for status blocked (JC.blocks). declared: the signed spec "
                   "declares the block's targets or offsets (JC.unmatched_decision.declared, confirmed in the spec); "
                   f"test and verdict: the decision test the declaration names and its result in {DT}."),
        "offsets": ("One row per offset (JC.bias): a common shift, on the target's scale, shared by the unit targets "
                    "of one source and group. mean and sd are over the accepted draws; accepted_share is draws "
                    "accepted ÷ draws proposed."),
        "sensitivity": "One row per declared sensitivity setting (JC.sensitivity): the calibration repeated under "
                       "that setting, its status and block codes."}
    tables = {"targets": t_rows, "parameters": p_rows, "changes": c_rows, "blocks": b_rows, "offsets": o_rows,
              "sensitivity": s_rows, "series": None}
    labels = {"targets": {"observed": f"observed {metric}", "model": "engine at best point", "model_alt": "emulator at best point",
                          "score": "implausibility, engine", "score_alt": "implausibility, emulator",
                          "sd_total": f"total sd, {scale} scale", "window": "model window, years",
                          "low": "from assumed error", "high": "from assumed error"},
              "parameters": {"low": "accepted range, low", "high": "accepted range, high", "best": "best point",
                             "contraction": "1 − accepted ÷ prior width"},
              "changes": {"observed": f"observed change, {metric}", "modelled": f"modelled change, {metric}",
                          "z": "modelled − observed, in sd"},
              "offsets": {"mean": f"{scale} scale", "sd": f"{scale} scale", "accepted_share": "draws accepted ÷ proposed"}}
    return {
        "file": JC, "facts": facts, "why": why, "notes": notes,
        "tables": {k: _table(k, v, about.get(k, ""), labels.get(k)) if v is not None else None for k, v in tables.items()},
        "blocks": block_info, "change_cover": _part(why, "declarations", change_cover) or [], "widest": widest,
        "status_word": word, "unverified": unverified, "conflicting": conflicting,
        "changes_uncomputed": uncomputed, "changes_asked": _get(spec, "calibration", "joint", "changes") is not None,
        "change_ids": [p.get("id") for p in pairs or []],
        "changes_reason": " ".join(str(sc.get("reason")).split()) if sc.get("status") != "computed" and sc.get("reason") else None,
        "bound_units": [[u, group_of.get(str(u)), {"hi": "upper", "lo": "lower"}.get(
            "".join(rec["region_touches_bound"]) if len(rec["region_touches_bound"]) == 1 else "", "both")]
            for u, rec in units.items() if isinstance(rec, dict) and isinstance(rec.get("region_touches_bound"), list)
            and rec["region_touches_bound"]],
        "min_accepted": th.get("region_min_candidates") if isinstance(th.get("region_min_candidates"), int) else None,
        "fields": {"targets": "data.targets, data.unit_targets, data.verification", "parameters": "data.region, data.best.theta",
                   "changes": "data.survey_changes.pairs", "blocks": "data.blocks, data.unmatched_decision",
                   "status": "data.status", "units": "data.units[].region_touches_bound",
                   "replicates": "data.verification.units[].runs", "cutoff": "data.cutoff", "diagnostics": "data"},
        "words": {"observed": f"observed {metric} (natural unit)", "model": "engine at the best point",
                  "model_alt": "emulator at the best point", "score": "implausibility I (engine)",
                  "interval": "the interval the method drew from its assumed error (obs_lo to obs_hi), not the "
                              "survey's own interval",
                  "accepted": "accepted range", "panels": "group", "change": f"change in {metric} between the two rounds",
                  "scored": "implausibility I, with the sign of model − observed"}}


# ------------------------------------------------------------------------------------------- posterior

def _posterior(run, doc):
    c, why = doc["data"], {}
    dg = c["diagnostics"]
    if not isinstance(dg, dict):
        raise TypeError("data.diagnostics is not a mapping")
    spec, spec_rel, spec_say, unverified = _spec(run)
    dominated = _prior_dominated(run)
    unit_rows, _, units_rel = _units(run, spec)
    grouping = _get(spec, "calibration", "targets", "groups")
    grouping = grouping if isinstance(grouping, str) else None
    fit, widest = dg.get("fit"), {}

    def targets():
        rows = []
        for f in fit:
            sd = math.sqrt(f["variance"])
            obs, exp = _num(f["observed"]), _num(f["expected"])
            groups = f.get("groups")
            group = "+".join(str(g) for g in groups) if isinstance(groups, list) else (None if groups is None else str(groups))
            rows.append([f"{f.get('source')}:{f.get('unit')}:{group}", "unit", group, None, f.get("source"), obs,
                         obs - 1.96 * sd, obs + 1.96 * sd, sd, exp, None, (exp - obs) / sd, None, None])
        return rows

    def parameters():
        names = c.get("names") if isinstance(c.get("names"), list) else list(c["priors"])
        cols = {n: k for k, n in enumerate(_get(c, "sample", "names") or [])}
        sample = _get(c, "sample", "rows") or []
        best = c.get("theta") if isinstance(c.get("theta"), dict) else c.get("map") if isinstance(c.get("map"), dict) else {}
        rows = []
        for n in names:
            plo, phi = (_num(x) for x in c["priors"][n]["range"])
            xs = [r[cols[n]] for r in sample] if n in cols else []
            lo, hi = (_quantile(xs, 0.025), _quantile(xs, 0.975)) if xs else (None, None)
            con = _num(_get(c, "identifiability", "contraction", n))
            widest[n] = (con, "identifiability.contraction") if con is not None else None
            rows.append([n, plo, phi, lo, hi, _num(best.get(n)), con,
                         _at_bound(min(xs), max(xs), plo, phi) if xs else None,
                         None if dominated is None else n in dominated])
        return rows

    def series():
        pp = _get(run.large(EST)[0], "data", "posterior_predictive")
        if not isinstance(pp, dict):
            return None
        name = _get(spec, "calibration", "targets", "observed")
        name = name if isinstance(name, str) else "posterior_predictive"
        return [[name, t if isinstance(t, int) and not isinstance(t, bool) else _num(t), _num(o), _num(m), _num(lo), _num(hi)]
                for t, o, m, lo, hi in zip(pp["steps"], pp["observed"], pp["median"], pp["lo"], pp["hi"])]

    t_rows = _part(why, "targets", targets) if isinstance(fit, list) else None
    p_rows, s_rows = _part(why, "parameters", parameters), _part(why, "series", series)
    n_targets = dg.get("n_targets") if isinstance(dg.get("n_targets"), int) else (len(t_rows) if t_rows is not None else None)
    ess = _num(dg.get("ess_bulk_min")) if dg.get("ess_bulk_min") is not None else _num(dg.get("ess"))
    facts = {"method": "posterior", "status": {True: "ok", False: "not_converged"}.get(dg.get("converged"))
             if isinstance(dg.get("converged"), bool) else None, "proceeded": None,
             "n_targets_group": 0, "n_targets_unit": n_targets,
             "n_parameters": dg.get("n_parameters") if isinstance(dg.get("n_parameters"), int) else (len(p_rows) if p_rows is not None else None),
             "region_accepted": None, "region_candidates": None, "region_share_of_prior": None, "cutoff": None,
             "change_limit": None, "engine_replicates": None, "units_at_bound": None,
             "n_units": len(unit_rows) if unit_rows else None, "grouping": grouping,
             "rhat_max": _num(dg.get("rhat_max")), "ess_min": ess}
    draws = len(_get(c, "sample", "rows") or [])
    noise = sorted({str(f.get("noise")) for f in fit if isinstance(f, dict)}) if isinstance(fit, list) else []
    sampler = dg.get("sampler") or _get(c, "sample", "method")
    how = (f"{_g(dg.get('chains'))} chains with {_g(dg.get('warmup'))} warm-up and {_g(dg.get('draws_per_chain'))} kept "
           f"draws each, acceptance {_g(dg.get('acceptance'))}, largest R-hat {_g(dg.get('rhat_max'))}, smallest bulk "
           f"effective sample size {_g(dg.get('ess_bulk_min'))}" if dg.get("chains") is not None else
           f"effective sample size {_g(dg.get('ess'))} from {_g(dg.get('points'))} points in {_g(dg.get('adaptive_stages'))} "
           f"adaptive stage(s)")
    pp = _get(run.large(EST)[0], "data", "posterior_predictive") if s_rows else None
    notes = {
        "method": (
            f"Posterior sampling. The likelihood of the {_g(n_targets)} observations, each under its source's noise "
            f"model{' (' + ', '.join(noise) + ')' if noise else ''}, is combined with the declared priors of the "
            f"{_g(facts['n_parameters'])} parameters. The mode is found by an optimiser (converged: "
            f"{dg.get('converged')}; message: {dg.get('message')}) and the posterior is sampled with {sampler}: {how}. "
            f"The parameters table gives the 2.5% and 97.5% quantiles of the {_g(draws)} kept draws as low and high, "
            f"and the best point (data.theta or data.map). contraction is 1 − posterior variance ÷ prior variance on "
            f"each prior's own scale (data.identifiability; status {_get(c, 'identifiability', 'status')})."
            + (f" Refused earlier attempts: {'; '.join(str(x) for x in dg['earlier_attempts_refused'])}."
               if dg.get("earlier_attempts_refused") else "")),
        "score": ("score is the standardised residual (expected − observed) ÷ sd, where sd is the square root of the "
                  "noise model's variance at the best point; it is signed, positive when the model is above the "
                  "observation. This method records no cutoff on it and no second (emulator) value."),
        "units_of_measure": ("observed and model are on the scale the source records (a count where the noise model is "
                             "binomial or Poisson); sd_total is the square root of the noise model's variance on that "
                             "scale; low and high are observed ∓ 1.96 sd_total (not floored at zero): from the noise "
                             "model at the best point, not the source's own interval."),
        "spec": spec_say,
        "not_recorded": ("This method (posterior) records no changes between rounds, no blocks, no offsets, no "
                         "sensitivity settings, no accepted region, no cutoff and no engine verification"
                         + ("" if t_rows is not None else "; this run's file has no per-target fit list (data.diagnostics.fit)")
                         + ("." if s_rows is not None else f"; and no series ({EST} data.posterior_predictive is absent).")),
        "harness_figures": "The harness figure beside each chart is not shown yet: it needs the file endpoint of goal G2b."}
    if isinstance(pp, dict):
        notes["series"] = (f"The series is the posterior predictive of {EST}: median and {_g(pp.get('level'))} band "
                           f"over {_g(pp.get('draws'))} draws; the band covers {_g(pp.get('coverage'))} of the "
                           f"{_g(len(s_rows))} observed steps.")
    for name, reason in why.items():
        notes[f"{name}_not_available"] = f"The {name} table {reason}."
    about = {
        "targets": (f"One row per observation the likelihood uses ({CAL} data.diagnostics.fit), in the file's order. "
                    f"id is source:unit:group; model is the expected value at the best point; score is the "
                    f"standardised residual. This method records no window, no second model value and no status "
                    f"per target."),
        "parameters": (f"One row per calibrated parameter: prior range (data.priors), the 2.5% and 97.5% quantiles of "
                       f"the kept draws (data.sample.rows) as low and high, best point, contraction "
                       f"(data.identifiability.contraction), whether the draws reach a prior bound, and whether {UNC} "
                       f"lists the parameter as prior-dominated."),
        "series": f"One row per step of the fit over time ({EST} data.posterior_predictive): the observed value, the "
                  f"predictive median as model, and the band's ends as low and high."}
    tables = {"targets": t_rows, "parameters": p_rows, "changes": None, "blocks": None, "offsets": None,
              "sensitivity": None, "series": s_rows}
    labels = {"targets": {"model": "expected at best point", "score": "standardised residual", "sd_total": "from the noise model",
                          "low": "observed − 1.96 sd", "high": "observed + 1.96 sd"},
              "parameters": {"low": "2.5% of draws", "high": "97.5% of draws", "best": "best point",
                             "contraction": "1 − posterior ÷ prior variance"},
              "series": {"model": "predictive median", "low": "band, low", "high": "band, high", "t": "step"}}
    sources = {r[4] for r in t_rows or []}
    return {
        "file": CAL, "facts": facts, "why": why, "notes": notes,
        "tables": {k: _table(k, v, about.get(k, ""), labels.get(k)) if v is not None else None for k, v in tables.items()},
        "blocks": [], "change_cover": [], "widest": widest, "min_accepted": None,
        "status_word": dg.get("converged"), "unverified": unverified, "conflicting": [], "changes_uncomputed": {}, "changes_asked": False, "change_ids": [],
        "changes_reason": None,
        "fields": {"targets": "data.diagnostics.fit", "parameters": "data.priors, data.sample.rows, data.theta or data.map",
                   "changes": "data", "blocks": "data", "status": "data.diagnostics.converged", "units": "data",
                   "replicates": "data", "cutoff": "data", "diagnostics": "data.diagnostics (rhat_max, ess_bulk_min or ess)"},
        "words": {"observed": "observed (the source's own scale)", "model": "expected at the best point", "model_alt": None,
                  "score": "standardised residual",
                  "interval": "observed ∓ 1.96 sd from the noise model's variance at the best point, not the "
                              "source's own interval",
                  "accepted": "2.5% to 97.5% of the posterior draws", "panels": "source" if len(sources) > 1 else "group",
                  "change": "change between rounds", "scored": "standardised residual (expected − observed) ÷ sd"}}


def choose(run):
    """(adapter, parsed document, None) or (None, None, why): by the artifact's schema and data.method (§3a)."""
    for rel, schema, read in ((JC, "analysis.joint_calibration/1", run.large), (CAL, "analysis.calibration/1", run.large)):
        doc, why = read(rel)
        if doc is None and "is not in the run folder" in str(why):
            continue
        if doc is None:
            return None, None, f"Not available: {why}."
        method = _get(doc, "data", "method")
        if doc.get("schema") == schema and not isinstance(doc.get("data"), dict):
            return None, None, f"Not available: {rel} holds no data mapping, so nothing can be read from it."
        if doc.get("schema") != schema:
            return None, None, (f"Not available: {rel} has schema '{doc.get('schema')}' and data.method '{method}', which "
                                f"this view does not know (it reads {schema}); nothing is guessed from it.")
        if rel == JC and method == "history_matching":
            return _history_matching, doc, None
        if rel == CAL and method == "posterior":
            return _posterior, doc, None
        later = " An adapter for it is built in slice β2." if rel == CAL and method in ADAPTERS_LATER else ""
        return None, None, (f"Not available: the calibration method '{method}' ({rel}, schema {schema}) has no adapter in "
                            f"this view, so nothing is read from it.{later}")
    return None, None, f"Not available: the run folder holds neither {JC} nor {CAL}, so there is no calibration to show."


def model(run):
    """(the generic model of this run's calibration, None), or (None, why) when no adapter can read it."""
    adapter, doc, why = choose(run)
    if adapter is None:
        return None, why
    try:
        return adapter(run, doc), None
    except Exception as e:
        return None, (f"Not available: {JC if adapter is _history_matching else CAL} could not be read as its "
                      f"method's file ({type(e).__name__}: {' '.join(str(e).split())[:120]}).")


def signed(r):
    """A target's score with the sign of model − observed (the method's score may be unsigned)."""
    if r["score"] is None or r["model"] is None or r["observed"] is None:
        return None
    return math.copysign(abs(r["score"]), r["model"] - r["observed"]) if r["model"] != r["observed"] else 0.0


def groups(m):
    """The targets' signed scores added up by group: rows of GROUP_COLUMNS, or None with no target table."""
    t = m["tables"]["targets"]
    if t is None:
        return None
    by = {}
    for r in _dicts(t):
        s = signed(r)
        if s is not None and r["group"] is not None:
            by.setdefault(r["group"], []).append(s)
    return [[g, len(v), sum(v) / len(v), max(v, key=abs), sum(x > 0 for x in v), sum(x < 0 for x in v)] for g, v in by.items()]


# ---------------------------------------------------------------------------------------------- self-test

from views.fit_bench import Bench, absent, close, data, need, rewrite, row, table  # noqa: E402,F401  (the self-tests' harness)


def _as_view(run):
    """The model in the shape of a view result, so the self-test reads an adapter's output as the page would."""
    m, why = model(run)
    need(m is not None, why)
    return {"checks": [], "facts": m["facts"], "tables": m["tables"], "charts": [], "notes": m["notes"]}


def _self_test():
    b = Bench(_as_view)
    folder, look_at, case = b.folder, b.look_at, b.case
    jc_rel, cal_rel, hm, post = JC, CAL, "history_matching", "posterior"

    def f2():
        facts = look_at(folder(hm))["facts"]
        want = {"method": hm, "status": "ok", "proceeded": True, "n_targets_group": 3, "n_targets_unit": 12,
                "n_parameters": 3, "region_accepted": 600, "region_candidates": 2048, "region_share_of_prior": 0.21,
                "cutoff": 3.0, "change_limit": 3.0, "engine_replicates": 4, "units_at_bound": 1, "n_units": 12,
                "grouping": "stratum", "rhat_max": None, "ess_min": None}
        need(set(facts) == set(FACTS), f"facts are exactly {FACTS} (got {sorted(facts)})")
        bad = {k: facts[k] for k in want if facts[k] != want[k]}
        need(not bad, f"facts differ from the files: {bad}")

    def f3():
        v = look_at(folder(hm))
        n = {name: len(table(v, name)) for name in COLUMNS if name != "series"}
        need(n == {"targets": 15, "parameters": 3, "changes": 3, "blocks": 0, "offsets": 3, "sensitivity": 1},
             f"row counts {n}")
        need(absent(v, "series"), "series, which this method does not record, is present with the value null")
        need(all(isinstance(t.get("about"), str) and t["about"] for t in v["tables"].values() if t),
             "every table says what its rows are and where they come from")

    def f4():
        d = folder(hm)
        v, jc = look_at(d), data(d, jc_rel)
        rows = table(v, "targets")
        need(sorted(r["level"] for r in rows) == ["group"] * 3 + ["unit"] * 12, "three group and twelve unit targets")
        fig = {f["target"]: f for f in jc["figure_data"]["fit"]}
        g, t, ver = row(v, "targets", "id", "grp_r1:G3"), jc["targets"]["grp_r1:G3"], jc["verification"]["targets"]["grp_r1:G3"]
        need(g["level"] == "group" and g["group"] == "G3" and g["source"] == t["source"] and g["status"] == "matched",
             f"group target row: level, group, source text and status from the file ({g})")
        need(close(g["observed"], t["value"]) and close(g["sd_total"], t["sd_total"])
             and close(g["low"], fig["grp_r1:G3"]["obs_lo"]) and close(g["high"], fig["grp_r1:G3"]["obs_hi"]),
             f"group target row: observed, sd_total and the interval from figure_data.fit ({g})")
        need(close(g["model"], ver["simulated"]) and close(g["score"], ver["implausibility"])
             and close(g["model_alt"], t["modelled_at_best"]) and close(g["score_alt"], t["implausibility_at_best"]),
             f"group target row: model and score are the engine's, model_alt and score_alt the emulator's ({g})")
        need("2018.5" in str(g["window"]) and "2019" in str(g["window"]),
             f"group target row: window looked up from adapter.windows by the observable's prefix ({g['window']})")
        u, ut = row(v, "targets", "id", "unit_r2:U05"), jc["unit_targets"]["U05"][0]
        uv = jc["verification"]["units"]["U05"]["targets"][0]
        need(u["level"] == "unit" and u["group"] == "G2" and "2021.5" in str(u["window"]),
             f"unit target row: its group comes from the grouping column of the units table ({u})")
        need(close(u["observed"], ut["value"]) and close(u["model"], uv["simulated"]) and close(u["score"], uv["implausibility"])
             and close(u["low"], fig["unit_r2:U05"]["obs_lo"]) and close(u["high"], fig["unit_r2:U05"]["obs_hi"]),
             f"unit target row: observed, interval, engine value and engine score ({u})")
        need(close(u["score_alt"], jc["units"]["U05"]["implausibility_at_best"]) and u["model_alt"] is None,
             f"unit target row: score_alt is the emulator's score for the unit's one target; no emulator value is recorded ({u})")

    def f5():
        d = folder(hm)
        v, jc = look_at(d), data(d, jc_rel)
        reg = jc["region"]
        for p in ("p1", "p2", "p3"):
            r = row(v, "parameters", "name", p)
            need(close(r["prior_low"], reg["prior_ranges"][p][0]) and close(r["prior_high"], reg["prior_ranges"][p][1])
                 and close(r["low"], reg["ranges"][p][0]) and close(r["high"], reg["ranges"][p][1])
                 and close(r["best"], jc["best"]["theta"][p]) and close(r["contraction"], reg["contraction"][p])
                 and r["at_bound"] == "none" and r["prior_dominated"] is (p == "p3"), f"parameter row {r}")
        for pair in jc["survey_changes"]["pairs"]:
            r = row(v, "changes", "group", pair["group"])
            need(r["from"] == "grp_r1" and r["to"] == "unit_r2" and close(r["observed"], pair["observed_natural"])
                 and close(r["modelled"], pair["modelled_natural"]) and close(r["z"], pair["z"]) and r["flagged"] is False,
                 f"change row {r}")
        for key, b in jc["bias"].items():
            r = row(v, "offsets", "key", key)
            need(close(r["mean"], b["mean"]) and close(r["sd"], b["sd"]) and close(r["accepted_share"], 1.0), f"offset row {r}")
        s = row(v, "sensitivity", "setting", "sd_015")
        need(s["status"] == "matched" and not s["blocks"], f"sensitivity row {s} (the method's own word)")

    def f7():
        d = folder(post)
        v, cal = look_at(d), data(d, cal_rel)
        facts = v["facts"]
        want = {"method": post, "status": "ok", "proceeded": None, "n_targets_group": 0, "n_targets_unit": 24,
                "n_parameters": 3, "region_accepted": None, "region_candidates": None, "region_share_of_prior": None,
                "cutoff": None, "change_limit": None, "engine_replicates": None, "units_at_bound": None, "n_units": 12,
                "grouping": "stratum", "rhat_max": 1.0, "ess_min": 910.0}
        need(set(facts) == set(FACTS), f"facts are exactly {FACTS} (got {sorted(facts)})")
        bad = {k: facts[k] for k in want if facts[k] != want[k]}
        need(not bad, f"facts differ from the files: {bad}")
        need(all(absent(v, n) for n in ("changes", "blocks", "offsets", "sensitivity")),
             "tables the method does not record are present with the value null")
        pp = data(d, "analysis/estimates.yaml")["posterior_predictive"]
        ser = table(v, "series")
        need(len(ser) == len(pp["steps"]) and ser[2]["t"] == pp["steps"][2] and close(ser[2]["observed"], pp["observed"][2])
             and close(ser[2]["model"], pp["median"][2]) and close(ser[2]["low"], pp["lo"][2])
             and close(ser[2]["high"], pp["hi"][2]) and isinstance(ser[2]["series"], str),
             f"series rows from the predictive band of analysis/estimates.yaml ({ser[2]})")
        need(len(table(v, "targets")) == 24, "one target row per entry of diagnostics.fit")
        f = cal["diagnostics"]["fit"][0]
        r, sd = row(v, "targets", "id", "S1:U01:G1"), math.sqrt(f["variance"])
        need(r["level"] == "unit" and r["group"] == "G1" and r["source"] == "S1" and close(r["observed"], f["observed"])
             and close(r["model"], f["expected"]) and close(r["sd_total"], sd)
             and close(r["score"], (f["expected"] - f["observed"]) / sd)
             and close(r["low"], f["observed"] - 1.96 * sd) and close(r["high"], f["observed"] + 1.96 * sd)
             and r["model_alt"] is None and r["score_alt"] is None, f"target row {r}")
        for p in ("p1", "p2", "p3"):
            r, (lo, hi) = row(v, "parameters", "name", p), cal["priors"][p]["range"]
            need(close(r["prior_low"], lo) and close(r["prior_high"], hi) and close(r["best"], cal["theta"][p])
                 and close(r["contraction"], cal["identifiability"]["contraction"][p]) and r["at_bound"] == "none"
                 and r["prior_dominated"] is (p == "p3"), f"parameter row {r}")
            need(isinstance(r["low"], float) and isinstance(r["high"], float) and lo < r["low"] <= r["best"] <= r["high"] < hi,
                 f"parameter row: low and high from the sample's rows, inside the prior ({r})")

    def f8():
        for method, number in ((hm, "600"), (post, "24")):
            notes = look_at(folder(method))["notes"]
            need(all(isinstance(notes.get(k), str) and len(notes[k]) > 40 for k in ("method", "score")),
                 f"{method}: notes.method and notes.score are sentences")
            need(number in notes["method"].replace(",", ""), f"{method}: notes.method carries this run's numbers ({number})")

    def f_signed_low():
        def small(doc):
            doc["data"]["diagnostics"]["fit"][0].update(observed=1.0, variance=4.0)
        r = row(look_at(b.edited(post, rewrite(cal_rel, small))), "targets", "id", "S1:U01:G1")
        need(close(r["low"], 1.0 - 1.96 * 2.0) and close(r["high"], 1.0 + 1.96 * 2.0),
             f"the interval is observed -/+ 1.96 sd with no floor at zero: an outcome may be signed ({r['low']}, {r['high']})")

    case("A1 history matching, clean fixture: the seventeen facts, read from the files", f2)
    case("A2 history matching, clean fixture: six tables with exactly the columns of §3a; series null", f3)
    case("A3 history matching, clean fixture: target rows (engine as model, emulator as model_alt, window, group)", f4)
    case("A4 history matching, clean fixture: parameter, change, offset and sensitivity rows", f5)
    case("A5 posterior, clean fixture: facts, target, parameter and series rows; unrecorded tables null", f7)
    case("A6 notes say how the method works and what the score is, with this run's numbers", f8)
    case("R11 posterior: a target's interval is not floored at zero", f_signed_low)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: fit_adapters.py --self-test")
