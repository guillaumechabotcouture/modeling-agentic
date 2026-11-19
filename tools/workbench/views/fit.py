#!/usr/bin/env python3
"""The Fit view (docs/design/review-workbench.md §5.5, generic model in §3a).

    python3 tools/workbench/views/fit.py --self-test

`view(run)` (run is a workbench.Run) returns {checks, facts, tables, charts, notes} and never raises:

- views/fit_adapters.py maps what the calibration method wrote into one generic model (history_matching and
  posterior today). Any other schema or method is `not_available`, named, never guessed at.
- The checks here run on the generic model only, so each is written once: status, region, changes,
  emulator_vs_engine, unit_bounds, target_error, constrained, replicates, near_cutoff, group_bias, convergence. Each
  returns one plain sentence and the file and field read. A threshold is the method's own recorded value; a
  threshold this view adds is called a "workbench threshold" in the sentence.
- facts: the seventeen names of §3a; a fact the method does not record is None.
- tables: the seven of §3a (a table the method does not record is present with the value None), plus `groups`,
  derived from the targets' signed scores.
- charts: declarations only (views/fit_charts.py; the page is generic); notes: the method's mechanics with this
  run's numbers.

Fixtures and planted edits: fixtures.py (DEFECTS).
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks as _checks  # noqa: E402
from views.fit_adapters import (BLOCK_MEANING, CAL, COLUMNS, CONTRACTED, DT, ESS_MIN, FACTS, GAP_MAX,  # noqa: E402
                                GROUP_BIAS, GROUP_COLUMNS, JC, NEAR, RHAT_MAX, SIGNED, STATUSES, UNC, UNIT_BOUND_SHARE,
                                _at_bound, _cap, _dicts, _g, _names, groups, model)
from views.fit_charts import charts as _charts  # noqa: E402

CHECKS = {"status": "C13", "region": "C13", "changes": "C2", "emulator_vs_engine": None, "unit_bounds": None,
          "target_error": "C3", "constrained": "C14", "replicates": None, "near_cutoff": None, "group_bias": None,
          "convergence": None}
TITLES = {"status": "How the calibration ended, and what it was blocked on",
          "region": "The accepted region: its size, and ranges or the best point on a prior bound",
          "changes": "Change between rounds: observed against modelled, by group",
          "emulator_vs_engine": "Emulator against engine at the best point",
          "unit_bounds": "Unit intervals that reach the unit parameter's bound",
          "target_error": "Target error terms that are assumptions",
          "constrained": "Parameters the fit narrowed that are still listed as prior-dominated",
          "replicates": "Engine runs per point in the verification",
          "near_cutoff": "Targets that passed the cutoff narrowly",
          "group_bias": "Groups where the model sits on one side of the targets",
          "convergence": "Sampler convergence"}


# ------------------------------------------------------------------------------------------- the checks

def _r(name, status, says, evidence, rows=None):
    return _checks.result(f"fit.{name}", "fit", TITLES[name], status, " ".join(str(says).split()), evidence, rows, CHECKS[name])


def _no(m, name, thing, field):
    """A check whose table or fact the method does not record, or that could not be read: not_available, with why."""
    method = m["facts"]["method"]
    reason = m["why"].get(thing)
    says = (f"Not available: the {thing} table {reason}." if reason else
            f"Not available: this method ({method}) records no {thing}.")
    return _r(name, "not_available", says, [(m["file"], m["fields"].get(field, "data"))])



def _covered(covers):
    """(covered by a declaration whose test ran, the words for the sentence)."""
    if not covers:
        return False, "not covered by any signed declaration"
    said = "; ".join(sorted({f"{c['test']} ({'verdict ' + str(c['verdict']) if c['ran'] else 'did not run'})" for c in covers}))
    return all(c["ran"] for c in covers), f"declared with decision test {said}"


def check_status(m):
    f, ev = m["facts"], [(m["file"], m["fields"]["status"])]
    if f["status"] == "ok":
        return _r("status", "ok", f"The calibration ({f['method']}) finished with status ok and no block.", ev)
    if f["status"] == "not_converged":
        return _r("status", "problem", f"The calibration ({f['method']}) did not converge, so its best point and "
                  f"its ranges are not a fit.", ev)
    if f["status"] != "blocked" or m["tables"]["blocks"] is None:
        why = m["why"].get("blocks")
        return _r("status", "problem" if not why else "not_available",
                  f"The calibration's status is '{f['status']}', which this view does not know." if not why else
                  f"Not available: the blocks table {why}.", ev)
    ev += [(m["file"], m["fields"]["blocks"]), (DT, "data.tests[].status, verdict"), (SIGNED, "calibration.joint.unmatched")]
    parts, bad, rows = [], False, _dicts(m["tables"]["blocks"])
    for b in m["blocks"]:
        ran, said = _covered(b["cover"])
        only_allowed = bool(b["allowance"]) and not b["cover"] and not b["uncovered"] and not b["unsigned"]
        hole = not only_allowed and (b["uncovered"] or b["unsigned"] or not ran)
        bad = bad or bool(hole)
        text = f"{b['code']} ({BLOCK_MEANING.get(b['code'], 'no meaning on record for this code')})"
        text += "" if only_allowed else f" is {said}"
        if b["unsigned"]:
            text += f", and the declaration of {_names(b['unsigned'])} is not in the signed spec or names no test"
        if b["uncovered"] and b["cover"]:
            text += f", and nothing covers {_names(b['uncovered'])}"
        if b["allowance"]:
            text += (f", and its {b['allowance'][0]} unit(s) are inside the signed spec's allowance of "
                     f"{b['allowance'][1]} flagged units, which no test covers")
        parts.append(text)
    went = {True: "the run proceeded", False: "the run did not proceed", None: "whether the run proceeded is not recorded"}[f["proceeded"]]
    bad = bad or f["proceeded"] is not True
    return _r("status", "problem" if bad else "look",
              f"The calibration is blocked by {len(parts)} block(s) and {went}: " + "; ".join(parts)
              + ("." if bad else ". Declared and signed, so for you to judge."), ev,
              {"columns": m["tables"]["blocks"]["columns"], "rows": m["tables"]["blocks"]["rows"]} if rows else None)


def check_region(m):
    f, t = m["facts"], m["tables"]["parameters"]
    if t is None:
        return _no(m, "region", "parameters", "parameters")
    rows = _dicts(t)
    at = [r for r in rows if r["at_bound"] not in ("none", None)]
    best = [r["name"] for r in rows if r["best"] is not None and _at_bound(r["best"], r["best"], r["prior_low"], r["prior_high"]) not in ("none", None)]
    acc, least, parts = f["region_accepted"], m["min_accepted"], []
    small = acc is not None and least is not None and acc < least
    near = acc is not None and least is not None and not small and acc <= least * (1 + NEAR)
    if acc is not None:
        parts.append(f"{_g(acc)} of {_g(f['region_candidates'])} candidates were accepted (the method's estimate of the "
                     f"share of prior volume is {_g(f['region_share_of_prior'])})")
        if small or near:
            parts.append(f"that is {'below' if small else 'within 25% of'} the method's own minimum of {_g(least)} "
                         f"(thresholds.history_matching.region_min_candidates)")
    else:
        parts.append(f"this method ({f['method']}) records no accepted share")
    parts.append(f"{len(at)} of {len(rows)} parameters have a range that reaches a prior bound"
                 + (f" ({_names((r['name'] + ' ' + r['at_bound'] for r in at), 9)})" if at else ""))
    parts.append(f"the best point is on a prior bound for {_names(best)}" if best else "the best point is on no prior bound")
    look = bool(at or best or small or near)
    return _r("region", "look" if look else "ok", _cap("; ".join(parts)) + ".",
              [(m["file"], m["fields"]["parameters"])],
              {"columns": t["columns"], "rows": [r for r in t["rows"] if r[7] not in ("none", None) or r[0] in best]} if look else None)


def check_changes(m):
    f, t = m["facts"], m["tables"]["changes"]
    if t is None:
        return _no(m, "changes", "changes", "changes")
    limit, rows = f["change_limit"], _dicts(t)
    flagged = [r for r in rows if r["flagged"]]
    near = [r for r in rows if not r["flagged"] and limit and r["z"] is not None and abs(r["z"]) >= (1 - NEAR) * limit]
    ev = [(m["file"], m["fields"]["changes"] + ", data.survey_changes.z_max")]
    if not flagged and not near:
        return _r("changes", "ok", f"None of the {len(rows)} group changes between rounds is flagged, and none has "
                  f"|z| within 25% of the method's limit {_g(limit)}.", ev)
    parts, bad = [], False
    for r in flagged:
        ran, said = _covered(m["change_cover"].get(r["group"]) or [])
        bad = bad or not ran
        parts.append(f"{r['group']} is flagged (modelled change {_g(r['modelled'], 3)} against {_g(r['observed'], 3)} "
                     f"observed, z {_g(r['z'], 3)} against the limit {_g(limit)}), {said}")
    if near:
        parts.append("near misses, inside the limit " + _g(limit) + " but within 25% of it: "
                     + _names((f"{r['group']} z {_g(r['z'], 3)}" for r in near), 8))
    if flagged:
        ev += [(DT, "data.tests[].status, verdict"), (m["file"], "data.unmatched_decision.declared"),
               ("scope_declaration.yaml", "declarations"), ("findings.jsonl", "status declared")]
    keep = {r["group"] for r in flagged + near}
    return _r("changes", "problem" if bad else "look", "Change between rounds: " + "; ".join(parts) + ".", ev,
              {"columns": t["columns"], "rows": [r for r in t["rows"] if r[0] in keep]})


def check_emulator_vs_engine(m):
    t = m["tables"]["targets"]
    rows = [r for r in _dicts(t) if r["score"] is not None and r["score_alt"] is not None]
    if t is None or not rows:
        return _no(m, "emulator_vs_engine", "targets" if t is None else "emulator value beside the engine's", "targets")
    parts, hit, keep = [], False, []
    for level in ("group", "unit"):
        of = [r for r in rows if r["level"] == level]
        far = [r for r in of if abs(r["score"] - r["score_alt"]) > GAP_MAX]
        near = [r for r in of if (1 - NEAR) * GAP_MAX <= abs(r["score"] - r["score_alt"]) <= GAP_MAX]
        hit, keep = hit or bool(far or near), keep + far + near
        parts.append(f"{len(far)} of {len(of)} {level} targets"
                     + (f" ({_names(r['id'] for r in far)})" if far and level == "group" else "")
                     + (f" with {len(near)} more within 25% of that" if near else ""))
    ids = {r["id"] for r in keep}
    return _r("emulator_vs_engine", "look" if hit else "ok",
              f"The {m['words']['score'].split(' (')[0]} from the engine and from the emulator at the best point "
              f"differ by more than {_g(GAP_MAX)} (workbench threshold) for {', and for '.join(parts)}"
              + ("; the region was accepted on the emulator's values." if hit else "."),
              [(m["file"], m["fields"]["targets"])],
              {"columns": t["columns"], "rows": [r for r in t["rows"] if r[0] in ids]} if hit else None)


def check_unit_bounds(m):
    f = m["facts"]
    n, of = f["units_at_bound"], f["n_units"]
    if n is None or not of:
        return _no(m, "unit_bounds", "unit intervals", "units")
    share = n / of
    status = "look" if share >= (1 - NEAR) * UNIT_BOUND_SHARE else "ok"
    edge = " (not above it, but within 25% of it)" if status == "look" and share <= UNIT_BOUND_SHARE else ""
    which = m.get("bound_units") or []          # rows of (unit, group, bound reached), where the method names them
    named = f": {_names(f'{u} ({b})' for u, _, b in which)}" if which else ""
    return _r("unit_bounds", status,
              f"{_g(n)} of {_g(of)} units ({share:.1%}) have an accepted interval of the unit parameter that reaches "
              f"its bound{named}, against a workbench threshold of {UNIT_BOUND_SHARE:.0%}{edge}"
              + ("; for those units the targets do not bound the parameter on that side." if status == "look" else "."),
              [(m["file"], m["fields"]["units"])],
              {"columns": ["unit", f["grouping"] or "group", "bound_reached"], "rows": [list(r) for r in which]} if which else None)


def check_target_error(m):
    t = m["tables"]["targets"]
    if t is None:
        return _no(m, "target_error", "targets", "targets")
    rows = _dicts(t)
    assumed = [r for r in rows if "ASSUMPTION" in str(r["source"] or "")]
    by = {lv: sum(r["level"] == lv for r in assumed) for lv in ("group", "unit")}
    tail = (f" The intervals shown are {m['words']['interval']}; the sources' own intervals for the targets are not "
            f"in the file (gap H3).")
    if assumed:
        return _r("target_error", "look",
                  f"{len(assumed)} of {len(rows)} targets ({by['group']} group, {by['unit']} unit) carry an error term "
                  f"their source text labels an ASSUMPTION, so the fit's tightness on them is assumed, not measured." + tail,
                  [(m["file"], m["fields"]["targets"] + " (source)")])
    return _r("target_error", "ok", f"No source text of the {len(rows)} targets labels an error term an ASSUMPTION." + tail,
              [(m["file"], m["fields"]["targets"] + " (source)")])


def check_constrained(m):
    t = m["tables"]["parameters"]
    if t is None:
        return _no(m, "constrained", "parameters", "parameters")
    rows = _dicts(t)
    ev = [(UNC, "data.prior_dominated"), (m["file"], m["fields"]["parameters"])]
    if all(r["prior_dominated"] is None for r in rows):
        return _r("constrained", "not_available", f"Not available: {UNC} does not list which parameters are "
                  f"prior-dominated (the file is written by the analysis, after calibration).", ev)
    hit, near = [], []
    for r in rows:
        w = m["widest"].get(r["name"])
        if r["prior_dominated"] and w:
            (hit if w[0] > CONTRACTED else near if w[0] >= (1 - NEAR) * CONTRACTED else []).append((r["name"], w))
    if not hit and not near:
        return _r("constrained", "ok", f"No parameter that {UNC} lists as prior-dominated contracted by more than "
                  f"half ({sum(bool(r['prior_dominated']) for r in rows)} of {len(rows)} are listed).", ev)
    parts = []
    if hit:
        parts.append(f"{len(hit)} parameter(s) contracted by more than half yet are listed as prior-dominated: "
                     + _names(f"{n} {_g(w[0])} ({w[1]})" for n, w in hit))
    if near:
        parts.append("within 25% of one half: " + _names(f"{n} {_g(w[0])}" for n, w in near))
    names = {n for n, _ in hit + near}
    return _r("constrained", "look", "; ".join(parts) + "; the largest contraction measure the method records is "
              "used, and the two files disagree on whether the data constrain these.", ev,
              {"columns": t["columns"], "rows": [r for r in t["rows"] if r[0] in names]})


def check_replicates(m):
    n = m["facts"]["engine_replicates"]
    if n is None:
        return _no(m, "replicates", "engine verification", "replicates")
    if n <= 1:
        return _r("replicates", "look", f"The engine verification used {_g(n)} run per point, so a target's engine "
                  f"value and its score carry one run's noise and a narrow pass or fail may be chance.",
                  [(m["file"], m["fields"]["replicates"])])
    return _r("replicates", "ok", f"The engine verification used at least {_g(n)} runs per point.",
              [(m["file"], m["fields"]["replicates"])])


def check_near_cutoff(m):
    t, cut = m["tables"]["targets"], m["facts"]["cutoff"]
    if t is None or cut is None:
        return _no(m, "near_cutoff", "targets" if t is None else "cutoff", "targets" if t is None else "cutoff")
    rows = [r for r in _dicts(t) if r["score"] is not None]
    near = sorted((r for r in rows if (1 - NEAR) * cut <= abs(r["score"]) <= cut), key=lambda r: -abs(r["score"]))
    over = sum(abs(r["score"]) > cut for r in rows)
    ev = [(m["file"], m["fields"]["targets"] + ", " + m["fields"]["cutoff"])]
    tail = f" {over} more are above the cutoff (see the status check)." if over else ""
    if not near:
        return _r("near_cutoff", "ok", f"No target that passed the method's cutoff {_g(cut)} has a score within 25% "
                  f"of it.{tail}", ev)
    ids = {r["id"] for r in near}
    return _r("near_cutoff", "look",
              f"{len(near)} of {len(rows)} targets passed the method's cutoff {_g(cut)} with a score within 25% of it "
              f"(near miss, workbench rule): {_names((r['id'] + ' ' + _g(r['score'], 5) for r in near), 5)}.{tail}", ev,
              {"columns": t["columns"], "rows": [r for r in t["rows"] if r[0] in ids]})



def check_group_bias(m):
    rows = [r for r in groups(m) or [] if r[1] >= 2]
    if not rows:
        return _no(m, "group_bias", "targets" if m["tables"]["targets"] is None else "group with two or more scored targets", "targets")
    far = sorted((r for r in rows if abs(r[2]) > GROUP_BIAS), key=lambda r: -abs(r[2]))
    near = [r for r in rows if (1 - NEAR) * GROUP_BIAS <= abs(r[2]) <= GROUP_BIAS]
    ev = [(m["file"], m["fields"]["targets"])]
    if not far and not near:
        return _r("group_bias", "ok", f"In none of the {len(rows)} groups with two or more targets is the mean signed "
                  f"score beyond ±{_g(GROUP_BIAS)} (workbench threshold) or within 25% of it.", ev)
    side = lambda r: f"{r[0]} {r[2]:+.2f} ({r[4]} of {r[1]} targets with the model above)"       # noqa: E731
    parts = ([f"the mean signed score is beyond ±{_g(GROUP_BIAS)} (workbench threshold) in {len(far)} of {len(rows)} "
              f"groups, where the model sits on one side of the group's targets: {_names(map(side, far))}"] if far else []) \
        + ([f"within 25% of that threshold: {_names(map(side, near))}"] if near else [])
    return _r("group_bias", "look", _cap("; ".join(parts) + f"; the score is the {m['words']['scored']}."), ev,
              {"columns": GROUP_COLUMNS, "rows": far + near})


def check_convergence(m):
    f = m["facts"]
    rhat, ess = f["rhat_max"], f["ess_min"]
    ev = [(m["file"], m["fields"]["diagnostics"])]
    if rhat is None and ess is None:
        return _r("convergence", "not_available", f"Not available: this method ({f['method']}) records no sampler "
                  f"diagnostics (rhat_max, ess_min).", ev)
    said = "; ".join(x for x in (
        f"largest R-hat {_g(rhat)} against {RHAT_MAX}" if rhat is not None else "no R-hat is recorded",
        f"smallest effective sample size {_g(ess)} against {ESS_MIN}" if ess is not None else "no effective sample size is recorded"))
    if f["status"] != "ok":
        return _r("convergence", "problem", f"The method's own status is {f['status']} ({said}; workbench thresholds).", ev)
    over = (rhat is not None and rhat > RHAT_MAX) or (ess is not None and ess < ESS_MIN)
    near = (rhat is not None and rhat - 1 >= (1 - NEAR) * (RHAT_MAX - 1)) or (ess is not None and ess <= ESS_MIN * (1 + NEAR))
    if over or near:
        return _r("convergence", "look", f"The sampler's diagnostics are {'outside' if over else 'within 25% of'} the "
                  f"workbench thresholds (the method records none of its own in this file): {said}.", ev)
    return _r("convergence", "ok", f"The sampler's diagnostics are inside the workbench thresholds: {said}.", ev)


CHECK_FNS = (check_status, check_region, check_changes, check_emulator_vs_engine, check_unit_bounds, check_target_error,
             check_constrained, check_replicates, check_near_cutoff, check_group_bias, check_convergence)



def _unavailable(says, method=None):
    ev = [(JC, "schema, data.method"), (CAL, "schema, data.method")]
    return {"checks": [_r(n, "not_available", says, ev) for n in CHECKS],
            "facts": {**{k: None for k in FACTS}, "method": method},
            "tables": {k: None for k in COLUMNS}, "charts": [], "notes": {"not_available": says}}



def _relate(out):
    """The status check's sentence gains what it does not cover: the status is the method's own verdict on its own
    blocks, and the other checks, which test things it does not, are named with their status where they raise
    something. So a status of ok beside a raised check is not read as a contradiction."""
    status = next((c for c in out if c["id"] == "fit.status"), None)
    if status is None or status["status"] == "not_available":
        return
    others = [c for c in out if c is not status and c["status"] != "not_available"]
    raised = [c for c in others if c["status"] in ("problem", "look")]
    if not raised:
        tail = f"none of the other {len(others)} raises anything on this run."
    elif len(raised) <= 3:       # few enough to name; more are counted, and each is its own card beside this one
        tail = (f"{len(raised)} of those {len(others)} raise something on this run: "
                + "; ".join(f"{c['title']} ({c['status']})" for c in raised) + ".")
    else:
        count = ", ".join(f"{n} {w}" for w in ("problem", "look") if (n := sum(c["status"] == w for c in raised)))
        tail = f"{len(raised)} of those {len(others)} raise something on this run ({count}); each has its own card here."
    status["says"] += (" This is the method's own verdict on its own blocks; the other checks on this page test what it "
                       "does not, and " + tail)


def view(run):
    try:
        m, why = model(run)
        if m is None:
            return _unavailable(why)
        out = []
        for fn in CHECK_FNS:
            name = fn.__name__[len("check_"):]
            try:
                out.append(fn(m))
            except Exception as e:      # a check must not take the view down; it says what failed
                out.append(_r(name, "not_available", f"Not available: this check failed on the run's files "
                              f"({type(e).__name__}: {' '.join(str(e).split())[:120]}).", [(m["file"], "data")]))
        _relate(out)
        try:
            charts = _charts(m)
        except Exception as e:
            charts, m["notes"]["charts_not_available"] = [], f"The charts could not be declared ({type(e).__name__}: {e})."
        return {"checks": out, "facts": {k: m["facts"].get(k) for k in FACTS}, "tables": m["tables"], "charts": charts,
                "notes": m["notes"]}
    except Exception as e:
        return _unavailable(f"Not available: the Fit view failed on the run's files ({type(e).__name__}: "
                            f"{' '.join(str(e).split())[:120]}).")


# ---------------------------------------------------------------------------------------------- self-test

# What each planted edit of fixtures.DEFECTS must do: (method, defect, the one check that moves, its new status).
# `look` against `problem` follows §3: flagged by a rule in code and covered by a signed declaration that names a
# decision test that ran is `look`; with no such declaration, or a test that did not run, it is `problem`.
# An edit that by rule moves a second check names it under `also` in fixtures.DEFECTS.
PLANTED = (
    ("history_matching", "C2_change_flagged", "changes", "problem"),
    ("history_matching", "C2_change_flagged_declared", "changes", "look"),
    ("history_matching", "C2_change_near_miss", "changes", "look"),
    ("history_matching", "C13_blocked_undeclared", "status", "problem"),
    ("history_matching", "C13_blocked_declared", "status", "look"),
    ("history_matching", "C13_blocked_test_not_run", "status", "problem"),
    ("history_matching", "C13_tiny_region", "region", "look"),
    ("history_matching", "C13_best_on_bound", "region", "look"),
    ("history_matching", "bound_within_tolerance", "region", "look"),
    ("history_matching", "bound_outside_tolerance", "region", "ok"),
    ("history_matching", "C3_assumed_sd", "target_error", "look"),
    ("history_matching", "C14_prior_dominated", "constrained", "look"),
    ("history_matching", "emulator_vs_engine", "emulator_vs_engine", "look"),
    ("history_matching", "unit_bounds", "unit_bounds", "look"),
    ("history_matching", "replicates", "replicates", "look"),
    ("posterior", "C13_best_on_bound", "region", "look"),
    ("posterior", "C14_prior_dominated", "constrained", "look"),
    ("posterior", "C13_not_converged", "status", "problem"),
    # the checks the spec gained after the first acceptance pass (§3a)
    ("history_matching", "near_cutoff", "near_cutoff", "look"),
    ("history_matching", "group_bias", "group_bias", "look"),
    ("posterior", "group_bias", "group_bias", "look"),
    ("history_matching", "emulator_vs_engine_unit", "emulator_vs_engine", "look"),
    ("history_matching", "C14_second_measure", "constrained", "look"),
    ("posterior", "convergence_rhat", "convergence", "look"),
    ("posterior", "convergence_ess", "convergence", "look"),
    ("posterior", "series_not_recorded", "status", "ok"))


def _self_test():
    import shutil
    from views.fit_bench import Bench, absent, check, close, fixture_builder, need, rewrite, row, says, statuses, table
    b = Bench(view)
    folder, look_at, case, tmp, wb_dir = b.folder, b.look_at, b.case, b.tmp, _WB
    jc_rel, cal_rel = JC, CAL

    def edited(method, edit):
        return b.edited(method, edit)

    hm, post = "history_matching", "posterior"

    def f1():
        st = statuses(look_at(folder(hm)))
        need(st == {**{n: "ok" for n in CHECKS}, "convergence": "not_available"},
             f"eleven checks, all ok but convergence, which this method does not record (got {st})")

    def f6():
        st = statuses(v := look_at(folder(post)))
        want = {"status": "ok", "region": "ok", "target_error": "ok", "constrained": "ok", "changes": "not_available",
                "emulator_vs_engine": "not_available", "unit_bounds": "not_available", "replicates": "not_available",
                "near_cutoff": "not_available", "group_bias": "ok", "convergence": "ok"}
        need(st == want, f"posterior clean: {want} (got {st})")
        for name in ("changes", "emulator_vs_engine", "unit_bounds", "replicates", "near_cutoff"):
            need("posterior" in says(check(v, name)) and "no " in says(check(v, name)),
                 f"{name} says this method (posterior) records no such thing: {check(v, name).get('says')!r}")

    def f9():
        for d in (folder(hm), folder(post), folder(hm, "C13_blocked_declared")):
            for c in look_at(d)["checks"]:
                name = str(c.get("id")).split(".")[-1]
                need(name in CHECKS and c.get("id") == f"fit.{name}" and c.get("status") in STATUSES,
                     f"a known check, its id fit.<name>, and a known status ({c.get('id')}, {c.get('status')})")
                need(c.get("step") == "fit" and c.get("title"), f"{name}: belongs to this view and has a title")
                need(isinstance(c.get("says"), str) and c["says"].strip() and "\n" not in c["says"], f"{name}: one sentence")
                ev = c.get("evidence")
                need(isinstance(ev, list) and ev and all(isinstance(e, dict) and e.get("file") and "field" in e for e in ev),
                     f"{name}: names the file and field read ({ev})")
                need(c.get("case") == CHECKS[name], f"{name}: scorecard case {CHECKS[name]} (got {c.get('case')})")

    # What else each planted edit must show, beyond moving its one check.
    def extra(method, defect, v, c):
        if defect.startswith("C2_change_flagged"):
            r = row(v, "changes", "group", "G2")
            need(r["flagged"] is True and close(r["z"], -4.5) and "g2" in says(c), f"names the flagged group ({c['says']!r})")
        if defect.startswith("C2_change"):
            g = "G2" if "flagged" in defect else "G3"
            r, rows = row(v, "changes", "group", g), [t for t in table(v, "targets") if t["group"] == g]
            units, grp = [t for t in rows if t["level"] == "unit"], next(t for t in rows if t["level"] == "group")
            for col, key in (("observed", "observed"), ("modelled", "model")):
                want = sum(t[key] for t in units) / len(units) - grp[key]
                need(abs(r[col] - want) < 1e-5, f"the {col} change of {g} follows from the targets table: mean of its "
                                                f"unit rows minus its group row ({r[col]} against {want})")
        if defect.endswith("_declared"):
            need("dt2" in says(c) and "no change" in says(c), f"gives the declaration's test and its verdict ({c['says']!r})")
        if defect == "C2_change_near_miss":
            need(row(v, "changes", "group", "G3")["flagged"] is False and "2.5" in c["says"] and "3" in c["says"].replace("2.5", ""),
                 f"a near miss shows both numbers, z 2.5 and the limit 3.0 ({c['says']!r})")
        if defect.startswith("C13_blocked"):
            r = row(v, "blocks", "code", "offset_sampling_low_acceptance")
            declared = defect != "C13_blocked_undeclared"
            need(v["facts"]["status"] == "blocked" and v["facts"]["proceeded"] is declared, f"facts {v['facts']}")
            need(r["declared"] is declared and r["meaning"] and r["test"] == ("DT2" if declared else None), f"block row {r}")
            verdict = str(r["verdict"] or "").upper().replace(" ", "_")
            need(verdict == {"C13_blocked_undeclared": "", "C13_blocked_declared": "NO_CHANGE",
                             "C13_blocked_test_not_run": "NOT_RUN"}[defect], f"block row verdict {r}")
            need("offset sampling low acceptance" in says(c), f"names the block code ({c['says']!r})")
            share = {r["key"]: r["accepted_share"] for r in table(v, "offsets")}
            need(share["S2|G2"] < 0.01 and all(close(x, 1.0) for k, x in share.items() if k != "S2|G2"),
                 f"the offsets table shows the block: few draws accepted for the offset it names ({share})")
        if defect == "C13_tiny_region":
            r = row(v, "parameters", "name", "p2")
            need(v["facts"]["region_accepted"] == 40 and r["at_bound"] == "both" and close(r["contraction"], 0.0), f"p2 row {r}")
        if defect == "C13_best_on_bound":
            need(row(v, "parameters", "name", "p2")["at_bound"] == "lower" and "p2" in says(c), f"p2 on its lower bound ({c['says']!r})")
        if defect.startswith("bound_"):
            want = "upper" if defect == "bound_within_tolerance" else "none"
            need(row(v, "parameters", "name", "p3")["at_bound"] == want,
                 f"a bound is reached within 1e-6 of the prior range's width: p3 at_bound {want}")
        if defect == "C3_assumed_sd":
            need(c["says"].count("1") >= 1, f"counts the targets with an assumed sd ({c['says']!r})")
        if defect == "C14_prior_dominated":
            need(row(v, "parameters", "name", "p1")["prior_dominated"] is True and "p1" in says(c), f"names p1 ({c['says']!r})")
        if defect == "emulator_vs_engine":
            need("1 of 3" in says(c) or "g3" in says(c), f"says which group targets differ ({c['says']!r})")
        if defect == "unit_bounds":
            need(v["facts"]["units_at_bound"] == 4, f"units_at_bound {v['facts']['units_at_bound']}")
        if defect == "replicates":
            need(v["facts"]["engine_replicates"] == 1, f"engine_replicates {v['facts']['engine_replicates']}")
        if defect == "C13_not_converged":
            need(v["facts"]["status"] == "not_converged", f"facts.status {v['facts']['status']}")
        if defect == "near_cutoff":
            need("unit r2:u03" in says(c) and "2.6" in c["says"] and "cutoff 3" in says(c),
                 f"names the target with its score 2.6 and the cutoff 3.0 ({c['says']!r})")
        if defect == "group_bias":
            need("g2" in says(c) and "g1" not in says(c) and "workbench threshold" in says(c),
                 f"names the group, and the threshold as the workbench's ({c['says']!r})")
            g = [dict(zip(v["tables"]["groups"]["columns"], r)) for r in v["tables"]["groups"]["rows"]]
            g2 = next(r for r in g if r["group"] == "G2")
            need(g2["mean_signed_score"] > 1 and g2["model_above"] >= 4, f"the groups table shows it ({g2})")
        if defect == "emulator_vs_engine_unit":
            need("0 of 3 group" in says(c) and "1 of 12 unit" in says(c), f"counts group and unit targets apart ({c['says']!r})")
        if defect == "C14_second_measure":
            need("p3" in says(c) and "0.8" in c["says"] and close(row(v, "parameters", "name", "p3")["contraction"], 0.2),
                 f"uses the largest contraction measure recorded, and names it ({c['says']!r})")
        if defect.startswith("convergence_"):
            a, b = ("1.03", "1.01") if defect.endswith("rhat") else ("250", "400")
            need(a in c["says"] and b in c["says"] and "workbench threshold" in says(c) and v["facts"]["status"] == "ok",
                 f"gives the value, the threshold, and that it is the workbench's ({c['says']!r})")
        if defect == "series_not_recorded":
            need(absent(v, "series") and not any(ch["table"] == "series" for ch in v["charts"])
                 and "no series" in v["notes"]["not_recorded"], "series is null, has no chart, and the notes say so")

    def planted(method, defect, name, status):
        def fn():
            fixtures = fixture_builder()
            need(fixtures.DEFECTS[defect]["check"] == name and fixtures.DEFECTS[defect]["status"] == status,
                 "the fixture's own record of this edit agrees")
            clean, v = statuses(look_at(folder(method))), look_at(folder(method, defect))
            moved = {k: s for k, s in statuses(v).items() if clean.get(k) != s}
            want = {**({name: status} if clean[name] != status else {}), **fixtures.DEFECTS[defect].get("also", {})}
            need(moved == want, f"only {want or 'nothing'} moves (moved: {moved})")
            for other in fixtures.DEFECTS[defect].get("also", {}):
                need(check(v, other)["says"], f"{other} says why")
            extra(method, defect, v, check(v, name))
        return fn

    def f_relates():
        clean, flagged = look_at(folder(hm)), look_at(folder(hm, "C2_change_flagged"))
        need(check(flagged, "status")["status"] == "ok" and check(flagged, "changes")["status"] == "problem", "status ok beside a flagged change")
        c = says(check(flagged, "status"))
        need("method's own" in c and TITLES["changes"].lower() in c and "(problem)" in c,
             f"a status of ok says it is the method's own verdict and names the other check that is raised ({c!r})")
        c = says(check(clean, "status"))
        need("method's own" in c and "none of the other" in c, f"on the clean run it says no other check raises anything ({c!r})")
        many = [_r("status", "look", "Blocked.", [])] + [_r(n, s, "x", []) for n, s in (
            ("region", "look"), ("changes", "problem"), ("replicates", "look"), ("near_cutoff", "look"), ("group_bias", "ok"),
            ("convergence", "not_available"))]
        _relate(many)
        need("4 of those 5 raise something on this run (1 problem, 3 look)" in many[0]["says"]
             and TITLES["region"] not in many[0]["says"] and len(many[0]["says"]) < 260,
             f"with more than three raised it gives the counts by status, not a list of titles ({many[0]['says']!r})")

    def f_names_units():
        c = check(look_at(folder(hm)), "unit_bounds")
        need("U12" in c["says"] and "upper" in c["says"], f"the one unit at its bound is named, with the bound ({c['says']!r})")
        c = check(look_at(folder(hm, "unit_bounds")), "unit_bounds")
        need(all(u in c["says"] for u in ("U01", "U02", "U03", "U12")) and len(c["rows"]["rows"]) == 4
             and c["rows"]["columns"][0] == "unit", f"each unit at a bound is named, and listed in the rows behind the check ({c['says']!r})")

    def f_interval():
        for d in (folder(hm), folder(hm, "C3_assumed_sd")):
            c = check(look_at(d), "target_error")
            need("interval" in says(c), f"always states that survey intervals are not in the file ({c['says']!r})")

    def f_upto():
        v = look_at(folder(hm, upto="calibration"))
        st = statuses(v)
        need(st == {**{n: "ok" for n in CHECKS}, "constrained": "not_available", "convergence": "not_available"},
             f"before analysis/uncertainty.yaml exists `constrained` is not available as well (got {st})")
        need(all(r["prior_dominated"] is None for r in table(v, "parameters")), "prior_dominated is unknown, not False")

    def f_unknown_method():
        def method_x(doc):
            doc["data"]["method"] = "method_x"
        v = look_at(edited(post, rewrite(cal_rel, method_x)))
        st = statuses(v)
        need(st == {n: "not_available" for n in CHECKS}, f"every check not_available (got {st})")
        need(any("method x" in says(c) for c in v["checks"]), "the sentence names the method it does not know")
        need(absent(v, "targets") and absent(v, "parameters"), "no table is guessed at")

    def f_unknown_schema():
        def schema_9(doc):
            doc["schema"] = "analysis.joint_calibration/9"
        v = look_at(edited(hm, rewrite(jc_rel, schema_9)))
        st = statuses(v)
        need(st == {n: "not_available" for n in CHECKS}, f"every check not_available (got {st})")
        need(any("joint calibration/9" in says(c) or "emulator inversion" in says(c) for c in v["checks"]),
             "the sentence names the schema or method it does not know")

    def f_missing():
        def remove(d):
            for rel in (jc_rel, cal_rel):
                if os.path.exists(os.path.join(d, rel)):
                    os.remove(os.path.join(d, rel))
        for method in (hm, post):
            v = look_at(edited(method, remove))
            st = statuses(v)
            need(st == {n: "not_available" for n in CHECKS}, f"{method}: every check not_available (got {st})")
            need(v["facts"].get("method") is None and all(c["says"] for c in v["checks"]), f"{method}: says why")

    def f_unparsable():
        def garbage(rel, text):
            def edit(d):
                with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            return edit

        def no_targets(doc):
            doc["data"]["targets"] = "not a mapping"
            doc["data"]["survey_changes"] = ["not", "a", "mapping"]
        whole = (edited(post, garbage(cal_rel, "schema: analysis.calibration/1\ndata: [unclosed\n  - {")),
                 edited(post, garbage(cal_rel, "schema: analysis.calibration/1\ndata: 7\n")),
                 edited(hm, garbage(jc_rel, "\x00\x01 not yaml: [")))
        for d in whole:
            st = statuses(look_at(d))
            need(st == {n: "not_available" for n in CHECKS}, f"every check not_available (got {st})")
        st = statuses(look_at(edited(hm, rewrite(jc_rel, no_targets))))
        need(set(st) == set(CHECKS) and all(s in STATUSES for s in st.values()), f"eleven checks with known statuses (got {st})")
        need(st["changes"] == "not_available" and st["emulator_vs_engine"] == "not_available" and st["target_error"] == "not_available",
             f"a part of the file with the wrong type makes the checks that read it not_available (got {st})")

    def f_signed():
        import yaml

        def drop(rel):
            def edit(d):
                with open(os.path.join(d, rel), encoding="utf-8") as f:
                    spec = yaml.safe_load(f)
                spec["calibration"]["joint"]["unmatched"]["declared"] = []
                with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                    yaml.safe_dump(spec, f, sort_keys=False)
            return edit
        base = folder(hm, "C13_blocked_declared")
        for rel, want, word in (("decision_spec.yaml", "look", "differs"), ("decision_brief.signed/decision_spec.yaml", "problem", "differs")):
            d = os.path.join(tmp, f"signed{len(os.listdir(tmp))}")
            shutil.copytree(base, d)
            drop(rel)(d)
            v = look_at(d)
            need(check(v, "status")["status"] == want and word in v["notes"]["spec"] and "signed copy" in v["notes"]["spec"],
                 f"with the declaration removed from {rel}: status {want}, and the notes say which copy was read "
                 f"(got {check(v, 'status')['status']}; {v['notes'].get('spec')!r})")

    def f_once():
        import workbench
        d = folder(hm)
        look_at(d)
        n0 = workbench.LARGE_PARSES[0]
        look_at(d), look_at(d)
        need(workbench.LARGE_PARSES[0] == n0, f"after the first view of a run, later views parse none of its YAML files "
                                              f"again ({workbench.LARGE_PARSES[0] - n0} parses)")

    def f_words():
        fixtures = fixture_builder()
        words, hits = fixtures.domain_words(), []
        need(len(words) > 50, "tools/workbench/domain_words.txt holds the word list")
        for root, dirs, names in os.walk(wb_dir):
            # acceptance/ quotes the reference runs' own names and values (§9), so it cannot be free of them
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "acceptance")]
            for n in names:
                if n == "domain_words.txt":
                    continue
                with open(os.path.join(root, n), encoding="utf-8", errors="ignore") as f:
                    text = f.read().lower()
                hits += [f"{os.path.relpath(os.path.join(root, n), wb_dir)}: {w}" for w in words if w in text]
        need(not hits, f"domain words in the code: {hits[:6]}")

    case("F1 history matching, clean fixture: the eleven checks, every one ok or not recorded by the method", f1)
    case("F2 posterior, clean fixture: six checks ok, five not_available naming the method", f6)
    case("F3 every check carries one sentence, the file and field read, and its scorecard case", f9)
    n = 3
    for method, defect, name, status in PLANTED:
        n += 1
        case(f"F{n} {method}, {defect}: {name} becomes {status} and no other check changes",
             planted(method, defect, name, status))
    case(f"F{n + 1} target_error always states that survey intervals are not in the file", f_interval)
    case(f"F{n + 2} a run stopped after calibration: constrained is not_available, the rest unchanged", f_upto)
    case(f"F{n + 3} an unknown method is not_available and named, never guessed at", f_unknown_method)
    case(f"F{n + 4} an unknown schema is not_available and named", f_unknown_schema)
    case(f"F{n + 5} a missing calibration file is not_available, never an exception", f_missing)
    case(f"F{n + 6} an unparsable or wrongly typed calibration file is not_available, never an exception", f_unparsable)
    case(f"F{n + 7} the signed spec copy is read where the spec on disk differs, and the notes say so", f_signed)
    case(f"F{n + 8} the calibration file is parsed once per process, however many times the view is asked for", f_once)
    case(f"F{n + 9} no file under tools/workbench/ holds a domain word (§3a)", f_words)
    case(f"F{n + 10} the status sentence says it is the method's own verdict and which other checks raise something", f_relates)
    case(f"F{n + 11} units whose interval reaches a bound are named, not only counted", f_names_units)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: fit.py --self-test")
