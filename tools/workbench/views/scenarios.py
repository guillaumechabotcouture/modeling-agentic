#!/usr/bin/env python3
"""The Scenarios view (docs/design/review-workbench.md §5.6): the delivery schedule by group and arm against the
delivery period and the outcome window, the trajectories of plan against comparator when the run has them, and the
dynamics checks the harness computed on them.

    python3 tools/workbench/views/scenarios.py --self-test


`view(run)` returns {checks, facts, tables, charts, notes}. What the cases hold the view to, each from §3 or §5.6
(its "Settled" paragraph wins over an earlier line), with what the specification leaves open marked "assumed":

 A1  facts: those of FACTS, and one count per event kind the run records, named n_events_<kind> (assumed name).
     delivery and window are the stored schedule's (analysis/figure_data.yaml data.schedule). With no trajectory
     record has_trajectories is false and the four facts read from it are null.
 A2  tables.events, columns of COLUMNS: one row per stored row (a group's event with the number of units that hold
     it), id the row's position counting from 0 over the groups in the file's order; start and end the stored t0 and
     t1. Settled: `arm` and `charged` are derived by the producer's rule (a decision event is the plan's and is
     charged; every other is the comparator's) and a note says they are derived. A kind the hook does not write has
     neither.
 A3  tables.series [group, arm, metric, t, value] from analysis/trajectories.yaml data.series: the groups, and the
     pooled sample under the producer's own word `total` (assumed). tables.dynamics with the producer's columns
     (settled). Assumed: the dynamics checks are read where the producer stores them, on the entry of the figure
     named trajectories in analysis/figures.yaml (key `checks`); the specification names no file for them.
     tables.not_produced [figure, reason] from analysis/figures.yaml. A table the run does not record is null.
 A4  scenarios.trajectories_present (C5): `problem` when analysis/batch.yaml and analysis/point.yaml exist (settled)
     and there is no trajectory record. Assumed: `not_available` for a run that has not reached the outcome batch;
     `problem` when the record's engine identity is not the one the analysis records (analysis/point.yaml
     data.engine_identity, as the producer compares them): one file contradicts another.
 A5  scenarios.dynamics: `problem` for the producer's status `flag` (settled); `not_available` with no record, with
     no entry that holds the checks, with a status word the producer does not write (named), and with a record for
     another engine. Assumed: the producer's third word, `info`, is counted as a check and raises nothing.
 A6  scenarios.schedule_window: `look` for a decision event outside the delivery period or any event after the
     outcome window. Assumed: periods are half open, as the hook holds them (a point event at t0 is inside [a, b)
     when a <= t0 < b; an event is after the window when it starts at or after its end). An unknown kind is
     `not_available`, naming it. scenarios.arms is dropped (settled).
 A7  a deliverable kind with no schedule: no content, every check `not_available`, and a note says so (settled).
 A8  charts: the schedule as a timeline per group and arm with the delivery period and the outcome window; the
     series per group, plan against comparator; each with the harness figure beside it.

As built (goal G7). The page reads top down: the trajectory record and the harness's dynamics checks (checks, facts,
tables dynamics and sample), the schedule as a timeline (one block per group, one line per component, colour by
kind, which gives the arm), the series per metric, the events table, then the figures not produced. Beyond A1 to A8:
 B1  a record whose producer wrote `not_offered` (the engine has no series hook) is treated as no trajectory run:
     `problem` when the run reached the outcome batch.
 B2  a record that says ok and lacks its sample, seeds, metrics or series, or whose delivery period and window are
     not the stored schedule's, contradicts itself or another file: `problem`. A record whose engine identity
     cannot be held to the analysis's (none recorded) is `not_available`. The identity is compared on the producer's
     own keys (toolkit/analysis/figures.py _trajectories_current), and the analysis header's engine as it does.
 B3  a dynamics check whose status is not the one its own value gives under the producer's rule is `problem`.
 B4  the figure index is read through the Figures view (views/figures.py), so both views list the same figures;
     tables.images holds the simulation-tier figures not shown beside a chart, a figure not produced as its reason.
 B5  whether the record's other inputs still have their recorded hashes is the Steps view's check, not repeated here.
"""
from __future__ import annotations

import json
import math
import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)

import checks  # noqa: E402

FD, TRAJ, FIGS, POINT, BATCH = (f"analysis/{n}.yaml" for n in ("figure_data", "trajectories", "figures", "point", "batch"))
ROWS, TABLE = "analysis/batch/results.jsonl", "figures/timeline.csv"
SPEC, SIGNED = "decision_spec.yaml", checks.SIGNED_COPY
FD_SCHEMA, TRAJ_SCHEMA = "analysis.figure_data/1", "analysis.trajectories/1"
# The producers' own words, each read from the code named.
ARM = {"history": ("comparator", False), "decision": ("plan", True), "routine": ("comparator", False)}      # toolkit/engines/hooks.py KINDS; figure_data.py schedule()
SCHEDULE_WORDS = ("ok", "not_offered", "invalid")      # toolkit/analysis/figure_data.py schedule(), compute()
RECORD_WORDS = ("ok", "not_offered")                   # toolkit/analysis/trajectories.py run()
ARMS = ("comparator", "plan")                          # toolkit/analysis/trajectories.py aggregate()
DYNAMICS_WORDS = {"pass": "passed", "flag": "flagged", "info": "for information"}       # toolkit/analysis/figures.py trajectory_checks()
# trajectory_checks(): the status each check's own value gives, where its rule can be recomputed from that value
DYNAMICS_RULES = {"plan_equals_comparator_before_delivery": lambda v: "pass" if v <= 1e-9 else "flag",
                  "pre_intervention_vs_target": lambda v: "pass" if v == 0 else "flag"}
DYNAMICS_MEANING = {
    "plan_equals_comparator_before_delivery": "the largest relative difference between the plan's and the comparator's series before delivery starts, over every panel and metric; flagged above 1e-9, since the two arms share their history",
    "pre_intervention_vs_target": "the number of panels whose modelled comparator, averaged over the calibration window, lies outside the calibration target's interval; flagged when any does",
    "end_of_delivery_step": "the relative step in the plan's pooled series across the end of delivery (the month before against the month after), per metric; for information only"}
IDENTITY = ("name", "version", "code_sha256", "params_sha256", "config_sha256")       # figures.py _trajectories_current()
NO_SCHEDULE = ("estimates",)           # deliverable kinds whose analysis writes no schedule, batch or trajectory record
FACTS = ("has_trajectories", "n_sampled_units", "n_seeds", "n_dynamics_checks", "n_dynamics_failed", "delivery_start", "delivery_end",
         "window_start", "window_end", "n_groups", "n_events", "n_not_produced")
COLUMNS = {"dynamics": ["check", "value", "status", "detail", "panels"],
           "sample": ["group", "n_units", "units"],
           "events": ["id", "group", "arm", "kind", "component", "label", "start", "end", "units", "charged"],
           "series": ["group", "arm", "metric", "t", "value"],
           "not_produced": ["figure", "reason"],
           "images": ["figure", "path", "sha256", "title", "caption", "shows", "fresh", "reason"]}
TITLES = {"trajectories_present": "Whether the run's dynamics were looked at: the trajectory record",
          "dynamics": "The harness's dynamics checks on the trajectories",
          "schedule_window": "The delivery schedule against the delivery period and the outcome window"}
CASES = {"trajectories_present": "C5"}
SHOWN = 6


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _text(x):
    return " ".join(x.split())[:2000] if isinstance(x, str) and x.strip() else None


def _g(x):
    return "none" if _num(x) is None else f"{x:.8g}"


def _some(items):
    items = [str(i) for i in items]
    return ", ".join(items[:SHOWN]) + (f" and {len(items) - SHOWN} more" if len(items) > SHOWN else "")


def _n(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _pair(x):
    return [float(x[0]), float(x[1])] if isinstance(x, list) and len(x) == 2 and all(_num(v) is not None for v in x) and x[0] <= x[1] else None


def _schedule(run):
    """The stored schedule (analysis/figure_data.yaml data.schedule) as {why, absent, delivery, window, start, groups,
    rows}: `why` says what cannot be read or recognised (then nothing else is given), `rows` one dict per stored row
    with the arm and `charged` derived by the producer's rule (None for a kind the hook does not write)."""
    doc, why = run.large(FD)
    if doc is None:
        return {"why": why, "absent": str(why).endswith("is not in the run folder")}
    s = doc["data"].get("schedule") if isinstance(doc.get("data"), dict) else None
    if doc.get("schema") != FD_SCHEMA:
        return {"why": f"{FD} has the schema {doc.get('schema')!r}, which this view does not read (it reads {FD_SCHEMA})"}
    if not isinstance(s, dict):
        return {"why": f"{FD} holds no schedule (data.schedule)"}
    word = s.get("status")
    if word not in SCHEDULE_WORDS:
        return {"why": f"the schedule's status in {FD} is {word!r}, a word its producer does not write (it writes {', '.join(SCHEDULE_WORDS)})"}
    if word != "ok":
        return {"why": f"the producer recorded the schedule as {word}, with the reason: {_text(s.get('reason')) or 'none given'}"}
    delivery, window, regions = _pair(s.get("delivery")), _pair(s.get("window")), s.get("regions")
    if delivery is None or window is None or not isinstance(regions, dict):
        return {"why": f"the schedule in {FD} has the status ok and does not hold a delivery period and a window as two numbers each, with its groups (data.schedule.delivery, window, regions)"}
    rows, groups = [], {}
    for name, r in regions.items():
        events = r.get("events") if isinstance(r, dict) else None
        if not isinstance(events, list):
            return {"why": f"the schedule's group {name!r} in {FD} holds no list of events"}
        groups[str(name)] = r.get("units") if _num(r.get("units")) is not None else None
        for k, e in enumerate(events):
            ok = (isinstance(e, dict) and _num(e.get("t0")) is not None and (e.get("t1") is None or (_num(e["t1"]) is not None and e["t1"] >= e["t0"]))
                  and isinstance(e.get("kind"), str) and e["kind"].strip() and isinstance(e.get("component"), str) and _num(e.get("units")) is not None)
            if not ok:
                return {"why": f"event {k} of the schedule's group {name!r} in {FD} is not a row the producer writes (component, label, kind, t0, t1, units)"}
            arm, charged = ARM.get(e["kind"], (None, None))
            rows.append({"id": len(rows), "group": str(name), "arm": arm, "kind": e["kind"], "component": e["component"], "label": e.get("label"),
                         "start": e["t0"], "end": e["t1"], "units": e["units"], "charged": charged})
    return {"why": None, "delivery": delivery, "window": window, "start": _num(s.get("start")), "groups": groups, "rows": rows}


def _analysis_identity(run):
    """(the analysis's engine identity, where it was read, the analysis header's engine), as the producer finds them
    (toolkit/analysis/trajectories.py analysis_identity()): the point file's, else the first batch row's."""
    doc, _ = run.large(POINT)
    ident = doc["data"].get("engine_identity") if doc is not None and isinstance(doc.get("data"), dict) else None
    header = doc.get("engine") if doc is not None and isinstance(doc.get("engine"), dict) else None
    if isinstance(ident, dict) and ident:
        return ident, f"{POINT} data.engine_identity", header
    head, _ = run.read(ROWS, 1 << 20)
    try:
        first = json.loads((head or b"").split(b"\n", 1)[0] or b"{}")
        ident = first["result"]["provenance"]["engine"]
    except (ValueError, KeyError, TypeError):
        ident = None
    return (ident, f"{ROWS} (first row)", header) if isinstance(ident, dict) and ident else (None, None, header)


def _record(run, sched):
    """The trajectory record as {state, says, ...}. state: absent; unread (not readable, or a schema or status word
    this view does not know); not_offered (the producer's word: the engine has no series hook); broken (it says ok
    and does not hold what an ok record holds, or disagrees with the stored schedule); other_engine (its engine
    identity is not the analysis's: not evidence); unverified (the analysis records no identity to hold it to); ok."""
    doc, why = run.large(TRAJ)
    if doc is None:
        absent = str(why).endswith("is not in the run folder")
        return {"state": "absent" if absent else "unread", "says": why}
    if doc.get("schema") != TRAJ_SCHEMA:
        return {"state": "unread", "says": f"{TRAJ} has the schema {doc.get('schema')!r}, which this view does not read (it reads {TRAJ_SCHEMA})"}
    d = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    word = d.get("status")
    if word not in RECORD_WORDS:
        return {"state": "unread", "says": f"the status of {TRAJ} is {word!r}, a word its producer does not write (it writes {' and '.join(RECORD_WORDS)})"}
    if word == "not_offered":
        return {"state": "not_offered", "says": f"the trajectory step recorded that the engine offers no series ({_text(d.get('reason')) or 'no reason given'})"}
    sample, seeds, metrics, series = d.get("sample"), d.get("seeds"), d.get("metrics"), d.get("series")
    panels = {"total": series.get("total"), **(series.get("regions") if isinstance(series.get("regions"), dict) else {})} if isinstance(series, dict) else {}
    rows, bad = [], None
    if not (isinstance(sample, dict) and sample and all(isinstance(v, list) and v for v in sample.values())):
        bad = "no sample of units per group (data.sample)"
    elif not (isinstance(seeds, int) and not isinstance(seeds, bool) and seeds > 0):
        bad = "no count of seeds (data.seeds)"
    elif not (isinstance(metrics, list) and metrics and all(isinstance(m, str) for m in metrics)):
        bad = "no list of metrics (data.metrics)"
    elif not isinstance(panels.get("total"), dict):
        bad = "no pooled series (data.series.total)"
    for name, p in panels.items() if bad is None else ():
        t, arms = (p.get("t"), p.get("arms")) if isinstance(p, dict) else (None, None)
        if not (isinstance(t, list) and t and all(_num(x) is not None for x in t) and isinstance(arms, dict) and set(arms) == set(ARMS)):
            bad = f"no time grid with a series for each of {' and '.join(ARMS)} in the panel {name!r} (data.series)"
            break
        for arm in ARMS:
            for m in metrics:
                values = arms[arm].get(m) if isinstance(arms[arm], dict) else None
                if not (isinstance(values, list) and len(values) == len(t)):
                    bad = f"no series of {m} for the {arm} on the time grid of the panel {name!r} (data.series)"
                    break
                rows += [[str(name), arm, m, x, _num(v)] for x, v in zip(t, values)]
    if bad:
        return {"state": "broken", "says": f"{TRAJ} has the status ok and holds {bad}: the record contradicts itself"}
    out = {"sample": {str(g): [str(u) for u in ids] for g, ids in sample.items()}, "seeds": seeds, "metrics": metrics, "rows": rows,
           "metric_info": d.get("metric_info") if isinstance(d.get("metric_info"), dict) else {}, "selection": _text(d.get("selection")),
           "written_at": _text(doc.get("written_at")), "delivery": _pair(d.get("delivery")), "window": _pair(d.get("window"))}
    if sched["why"] is None and (out["delivery"], out["window"]) != (sched["delivery"], sched["window"]):
        return {**out, "state": "broken", "says": (
            f"{TRAJ} was computed for the delivery period {out['delivery']} and the window {out['window']}, and the stored schedule ({FD}) "
            f"holds {sched['delivery']} and {sched['window']}: the two files contradict each other")}
    mine = d.get("engine") if isinstance(d.get("engine"), dict) else {}
    theirs, where, header = _analysis_identity(run)
    if theirs is None or not any(mine.get(k) is not None and theirs.get(k) is not None for k in IDENTITY):
        return {**out, "state": "unverified", "says": (
            f"the engine identity of {TRAJ} cannot be held to the analysis's: " + (f"the analysis records none ({POINT} data.engine_identity, "
            f"{ROWS})" if theirs is None else f"the record and {where} share no recorded part of an identity"))}
    differs = [k.replace("_sha256", "") for k in IDENTITY if mine.get(k) != theirs.get(k)]
    if not differs and header and d.get("analysis_engine") != header:
        differs = ["the analysis header's engine"]
    if differs:
        return {**out, "state": "other_engine", "says": (
            f"{TRAJ} was computed with an engine whose identity differs from the analysis's ({where}) in {', '.join(differs)} (code "
            f"{str(mine.get('code_sha256'))[:10]} against {str(theirs.get('code_sha256'))[:10]}): it is no evidence about this analysis's dynamics")}
    return {**out, "state": "ok", "says": None, "where": where}


def _dynamics(run, rec, absent):
    """The harness's dynamics checks, read where the producer stores them: on the entry of the trajectories figure in
    analysis/figures.yaml (toolkit/analysis/figures.py fig_trajectories). (status, sentence, rows or None)."""
    if rec["state"] != "ok":
        return "not_available", ("There is no trajectory record that is evidence for this analysis, so there are no dynamics checks to pass through "
                                 f"({rec['says']})."), None
    doc, why = run.large(FIGS)
    if doc is None:
        return "not_available", f"The dynamics checks are stored with the trajectories figure, and the figure index could not be read ({why}).", None
    from views.figures import ADAPTERS, SCHEMA
    if doc.get("schema") != SCHEMA or doc.get("producer") not in ADAPTERS:
        return "not_available", (f"The dynamics checks are stored with the trajectories figure, and {FIGS} has the schema {doc.get('schema')!r} and the "
                                 f"producer {doc.get('producer')!r}, which this view does not read."), None
    figs = doc["data"].get("figures") if isinstance(doc.get("data"), dict) else None
    entry = next((e for e in figs if isinstance(e, dict) and e.get("figure") == "trajectories"), None) if isinstance(figs, list) else None
    if entry is None:
        reason = absent.get("trajectories")
        return "not_available", ("The harness computes its dynamics checks when it draws the trajectories figure, and this run's figure index holds no such "
                                 f"figure" + (f" (not produced: {reason})." if reason else ".") + " The record is there; nothing checked it."), None
    rows = entry.get("checks")
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) and isinstance(r.get("check"), str) for r in rows):
        return "not_available", f"The trajectories figure's entry in {FIGS} holds no list of checks, each with its name (data.figures[].checks).", None
    table = [[r.get(c) for c in COLUMNS["dynamics"]] for r in rows]
    unknown = sorted({str(r.get("status")) for r in rows if r.get("status") not in DYNAMICS_WORDS})
    if unknown:
        return "not_available", (f"{_n(len(unknown), 'status word')} of the dynamics checks {'is' if len(unknown) == 1 else 'are'} not one the producer "
                                 f"writes ({', '.join(unknown)}; it writes {', '.join(DYNAMICS_WORDS)}), so the checks cannot be read."), table
    clash = [r["check"] for r in rows if r["check"] in DYNAMICS_RULES and (_num(r.get("value")) is None or DYNAMICS_RULES[r["check"]](r["value"]) != r["status"])]
    flagged = [r for r in rows if r["status"] == "flag"]
    counts = ", ".join(f"{sum(r['status'] == w for r in rows)} {words}" for w, words in DYNAMICS_WORDS.items())
    if clash:
        return "problem", (f"{_n(len(clash), 'dynamics check')} ({', '.join(clash)}) {'has' if len(clash) == 1 else 'have'} a status that its own recorded "
                           "value does not give under the producer's rule: the record contradicts itself."), table
    if flagged:
        return "problem", (f"The harness flagged {len(flagged)} of its {_n(len(rows), 'dynamics check')}: "
                           + "; ".join(f"{r['check']} (value {r.get('value')}; {_text(r.get('detail')) or 'no detail'})" for r in flagged)
                           + f". All {len(rows)}: {counts}."), table
    return "ok", f"The harness ran {_n(len(rows), 'dynamics check')} on the trajectories and flagged none ({counts}): {', '.join(r['check'] for r in rows)}.", table


def _figures(run):
    """(the rows of the Figures view's tables for this run: not_produced, and the images that belong beside this view;
    why not). One loader for the figure index (views/figures.py), so both views list the same figures."""
    from views import figures
    out = figures.view(run)
    t = out["tables"]
    if t.get("not_produced") is None or t.get("images") is None:
        return None, None, out["notes"].get("figures") or "the figure index could not be read"
    as_dicts = lambda name: [dict(zip(t[name]["columns"], r)) for r in t[name]["rows"]]     # noqa: E731
    return as_dicts("not_produced"), [r for r in as_dicts("images") if r["view"] == "scenarios"], None


def _kind(run):
    """(the deliverable kind, the file it was read from): the verified signed spec's, else the spec on disk's."""
    signed = checks.verified_spec(run)
    doc, rel = (signed["doc"], SIGNED) if signed["verified"] else (run.json(SPEC)[0], SPEC)
    kind = doc["deliverable"].get("kind") if isinstance(doc, dict) and isinstance(doc.get("deliverable"), dict) else None
    return (kind if isinstance(kind, str) else None), rel


def view(run):
    sched = _schedule(run)
    kind, kind_rel = _kind(run)
    not_produced, beside, figs_why = _figures(run)
    absent_figs = {r["figure"]: r["reason"] for r in not_produced or []}
    none = bool(sched.get("absent")) and kind in NO_SCHEDULE         # a deliverable kind with no schedule: nothing to show
    reached = run.sha(BATCH) is not None and run.sha(POINT) is not None
    rec = {"state": "absent", "says": f"{TRAJ} is not in the run folder"} if none else _record(run, sched)
    evidence = rec["state"] == "ok"

    def result(name, status, says, ev, rows=None):
        return checks.result(f"scenarios.{name}", "scenarios", TITLES[name], status, says, ev, rows, CASES.get(name))

    # ---- trajectories_present (C5)
    ev = [(TRAJ, "schema, data.status, data.sample, data.seeds, data.engine"), (POINT, "data.engine_identity"), (BATCH, "the file")]
    no_kind = (f"The deliverable kind of this run ({kind}, {kind_rel} deliverable.kind) has no schedule, no outcome batch and no trajectory step, so there is "
               "nothing for this view to show.")
    if none:
        present = result("trajectories_present", "not_available", no_kind, ev)
    elif rec["state"] in ("absent", "not_offered") and reached:
        present = result("trajectories_present", "problem", (
            (f"The run reached the outcome batch ({BATCH} and {POINT} exist) and holds no trajectory record ({TRAJ})" if rec["state"] == "absent"
             else f"The run reached the outcome batch ({BATCH} and {POINT} exist) and {rec['says']}")
            + ": the modelled dynamics behind the outcomes were never looked at (gap H4). The plan against the comparator over time, and the harness's "
              "dynamics checks, are not available for this run."), ev)
    elif rec["state"] in ("absent", "not_offered"):
        present = result("trajectories_present", "not_available", (
            f"The run did not reach the outcome batch ({' and '.join(r for r in (BATCH, POINT) if run.sha(r) is None)} not in the run folder), and "
            + ("it holds no trajectory record" if rec["state"] == "absent" else rec["says"]) + ": there are no dynamics to look at yet."), ev)
    elif rec["state"] in ("unread", "unverified"):
        present = result("trajectories_present", "not_available", f"The trajectory record cannot be used: {rec['says']}.", ev)
    elif not evidence:
        present = result("trajectories_present", "problem", f"The trajectory record is not evidence for this run: {rec['says']}.", ev)
    else:
        n_units = sum(len(v) for v in rec["sample"].values())
        present = result("trajectories_present", "ok", (
            f"The run holds a trajectory record: {_n(n_units, 'sampled unit')} in {_n(len(rec['sample']), 'group')}, {_n(rec['seeds'], 'seed')} each, "
            f"{_n(len(rec['metrics']), 'metric')} ({_some(rec['metrics'])}); its engine identity is the analysis's ({rec['where']})."), ev)

    # ---- dynamics
    dyn_status, dyn_says, dyn_rows = ("not_available", no_kind, None) if none else _dynamics(run, rec, absent_figs)
    dynamics = result("dynamics", dyn_status, dyn_says, [(FIGS, "data.figures[figure=trajectories].checks"), (TRAJ, "data.status, data.engine")])

    # ---- schedule_window
    ev = [(FD, "data.schedule.status, delivery, window, regions[].events[]")]
    rows = sched.get("rows")
    if none:
        window = result("schedule_window", "not_available", no_kind, ev)
    elif sched["why"]:
        window = result("schedule_window", "not_available", f"There is no stored schedule to judge: {sched['why']}.", ev)
    else:
        (d0, d1), (w0, w1) = sched["delivery"], sched["window"]
        unknown = sorted({r["kind"] for r in rows if r["kind"] not in ARM})
        # half-open periods, as the hook holds them: a point event is inside [a, b) when a <= t0 < b, a span when it lies within
        outside = [r for r in rows if r["kind"] == "decision" and not (d0 <= r["start"] < d1 if r["end"] is None else r["start"] >= d0 and r["end"] <= d1)]
        after = [r for r in rows if (r["start"] >= w1 if r["end"] is None else r["end"] > w1)]
        hit = [r for r in rows if r in outside or r in after or r["kind"] not in ARM]
        table = {"columns": COLUMNS["events"], "rows": [[r[c] for c in COLUMNS["events"]] for r in hit]} if hit else None
        first = lambda rs: _some(f"{r['label'] or r['component']} in group {r['group'] or '(unnamed)'} at {_g(r['start'])}" for r in rs)     # noqa: E731
        if unknown:
            window = result("schedule_window", "not_available", (
                f"The stored schedule holds {_n(len([r for r in rows if r['kind'] not in ARM]), 'event')} of a kind the schedule hook does not write "
                f"({', '.join(unknown)}; it writes {', '.join(ARM)}), so the arm of those events is not known and the schedule cannot be judged."), ev, table)
        elif outside or after:
            parts = ([f"{_n(len(outside), 'decision event')} outside the delivery period [{_g(d0)}, {_g(d1)}) ({first(outside)})"] if outside else []) \
                + ([f"{_n(len(after), 'event')} at or after the end of the outcome window, {_g(w1)} ({first(after)})"] if after else [])
            window = result("schedule_window", "look", (
                f"Of the {_n(len(rows), 'stored event')}: " + "; ".join(parts) + ". The schedule hook refuses both, so the stored schedule is not one it "
                "would write for these periods; outcomes are counted only inside the window."), ev, table)
        else:
            window = result("schedule_window", "ok", (
                f"All {sum(r['kind'] == 'decision' for r in rows)} decision events of the {_n(len(rows), 'stored event')} lie inside the delivery period "
                f"[{_g(d0)}, {_g(d1)}), and no event lies at or after the end of the outcome window ({_g(w1)})."), ev)

    # ---- facts, tables
    usable = not none and not sched["why"]
    kinds = list(dict.fromkeys(r["kind"] for r in rows)) if usable else []
    facts = {
        "has_trajectories": evidence, "n_sampled_units": sum(len(v) for v in rec["sample"].values()) if evidence else None,
        "n_seeds": rec["seeds"] if evidence else None,
        "n_dynamics_checks": len(dyn_rows) if dyn_rows is not None else None,
        "n_dynamics_failed": sum(r[2] == "flag" for r in dyn_rows) if dyn_rows is not None else None,
        "delivery_start": sched["delivery"][0] if usable else None, "delivery_end": sched["delivery"][1] if usable else None,
        "window_start": sched["window"][0] if usable else None, "window_end": sched["window"][1] if usable else None,
        "n_groups": len(sched["groups"]) if usable else None, "n_events": len(rows) if usable else None,
        **{f"n_events_{k}": sum(r["kind"] == k for r in rows) for k in kinds},
        "n_not_produced": len(not_produced) if not_produced is not None and not none else None}
    labels = {"has_trajectories": "a trajectory record that is evidence for this analysis", "n_sampled_units": "units sampled for trajectories",
              "n_seeds": "seeds per sampled unit", "n_dynamics_checks": "dynamics checks the harness ran", "n_dynamics_failed": "dynamics checks flagged",
              "delivery_start": "delivery period starts (decimal year)", "delivery_end": "delivery period ends (decimal year)",
              "window_start": "outcome window starts (decimal year)", "window_end": "outcome window ends (decimal year)",
              "n_groups": "groups in the schedule", "n_events": "stored schedule rows (events)", "n_not_produced": "figures not produced (every tier)",
              **{f"n_events_{k}": f"events of kind {k}" + (f" ({ARM[k][0]} arm)" if k in ARM else " (arm not known)") for k in kinds}}

    def tab(name, rws, about, **more):
        return None if rws is None else {"columns": COLUMNS[name], "rows": rws, "about": about, **more}

    meanings = " ".join(f"{k}: {DYNAMICS_MEANING[k]}." for k in dict.fromkeys(r[0] for r in dyn_rows or []) if k in DYNAMICS_MEANING)
    tables = {
        "dynamics": tab("dynamics", dyn_rows, (
            f"The checks the harness computed on the trajectories when it drew their figure ({FIGS} data.figures[figure=trajectories].checks), passed "
            f"through in its own words. status: pass, flag (a problem) or info (for information; it raises nothing). {meanings} Needs you: every row "
            "with the status flag."), lead=True, plain=["status"], wrap=["detail"]),
        "sample": tab("sample", [[g, len(ids), ", ".join(ids)] for g, ids in rec["sample"].items()] if evidence else None, (
            f"The units whose trajectories were run, per group ({TRAJ} data.sample), each with {_n(rec.get('seeds') or 0, 'seed')}. The producer's "
            f"own word for how they were chosen: {rec.get('selection') or 'not recorded'}. The series of a group are the population-weighted mean "
            "of its sampled units, not of every unit of the group."), lead=True, wrap=["units"]),
        "events": tab("events", [[r[c] for c in COLUMNS["events"]] for r in rows] if usable else None, (
            f"One row per stored row of the schedule ({FD} data.schedule.regions[].events[]): one component, label, kind and time in one group. units: the "
            "number of the group's units that hold the event. start and end are decimal years (end is empty for an event at one point in time). id: the "
            "row's position in the file. arm and charged are derived, not stored: a decision event is the plan's and is charged to the decision; a history "
            "or routine event is the comparator's, holds in every arm and is not charged."),
            labels={"arm": "derived from kind", "charged": "derived from kind", "units": "units of the group holding it", "start": "decimal year", "end": "decimal year"}),
        "series": tab("series", rec["rows"] if evidence else None, (
            f"The modelled series of the trajectory record ({TRAJ} data.series): one row per group, arm, metric and monthly step t (decimal year). The "
            "group total is the pooled sample (the producer's own word); every other group is one of the run's."
            + "".join(f" {m}: {_text(i.get('label')) or m}, in {_text(i.get('units')) or 'units not recorded'} ({_text(i.get('aggregate')) or 'aggregate not recorded'} per step)."
                      for m, i in (rec.get("metric_info") or {}).items() if isinstance(i, dict)))),
        "not_produced": tab("not_produced", [[r["figure"], r["reason"]] for r in not_produced] if not_produced is not None and not none else None, (
            f"Every figure of the standard set the harness did not draw, of any tier, with its own reason ({FIGS} data.not_produced). Those that belong "
            "beside this view (the simulation tier) are shown under this table, each as a card with the reason."), wrap=["reason"]),
    }
    shown = {"timeline": "events", "trajectories": "series"}       # the figure drawn beside a chart of that table is not repeated among the images
    by_name = {r["figure"]: r for r in beside or []}
    image = lambda name: ({"image": {"path": by_name[name]["path"], "hash": by_name[name]["sha256"], "title": by_name[name]["title"] or name,      # noqa: E731
                                     **({"caption": by_name[name]["caption"]} if by_name[name]["caption"] else {})}}
                          if name in by_name and by_name[name]["path"] and tables[shown[name]] is not None else {})
    rest = [r for r in beside or [] if not image(r["figure"])] if not none else None
    tables["images"] = tab("images", [[r[c] for c in COLUMNS["images"]] for r in rest] if beside is not None and not none else None, (
        f"The harness figures that belong beside this view (the simulation tier of {FIGS}) and are not shown beside one of the charts above: each as "
        "the harness drew it, or, where it drew none, its reason."))

    # ---- charts
    charts = []
    if usable and rows:
        (d0, d1), (w0, w1) = sched["delivery"], sched["window"]
        bands = [{"low": w0, "high": w1, "label": "outcome window"}, {"low": d0, "high": d1, "label": "delivery period"}]
        edges = {}
        for value, words in ((d0, "delivery starts"), (w0, "window starts"), (d1, "delivery ends"), (w1, "window ends")):
            edges.setdefault(value, []).append(words)
        both = lambda ws: ws[0] if len(ws) == 1 else f"delivery and window {ws[0].split()[-1][:-1]}"      # noqa: E731
        charts.append({
            "kind": "timeline", "table": "events", "x": "start", "high": "end", "y": "component", "group": "group", "mark": "kind", "size": "units",
            "bands": bands,
            "limits": [{"value": v, "label": both(ws)} for v, ws in sorted(edges.items())],
            "labels": {"start": "time (decimal year)", "kind": "kind of event", "units": "units of the group holding the event"},
            "title": "The delivery schedule: what is delivered when, per group and component",
            "how_to_read": ("One block per group, one line per component, against one time axis. A dot is an event at one point in time and a bar an "
                            "event that lasts. Colour is the kind: a decision event is the plan's (delivered and paid for by the decision); history and "
                            "routine events are the comparator's and happen in every arm. A larger mark is held by more of the group's units. Look for "
                            "decision events outside the shaded delivery period, and for anything after the outcome window."), **image("timeline")})
    if evidence:
        bands = [{"low": lo, "high": hi, "label": words} for (lo, hi), words in ((rec["window"] or (None, None), "outcome window"),
                                                                               (rec["delivery"] or (None, None), "delivery period")) if lo is not None]
        for m in rec["metrics"]:
            info = rec["metric_info"].get(m) if isinstance(rec["metric_info"].get(m), dict) else {}
            words = (_text(info.get("label")) or m) + (f" ({_text(info.get('units'))})" if _text(info.get("units")) else "")
            charts.append({
                "kind": "lines", "table": "series", "x": "t", "y": "value", "group": "arm", "panels": "group", "where": {"metric": m},
                **({"bands": bands} if bands else {}),
                "labels": {"t": "time (decimal year)", "value": words},
                "title": f"Plan against comparator over time: {words}",
                "how_to_read": ("One panel for the pooled sample (total) and one per group; one line per arm, monthly. The two lines should lie on each "
                                "other until the delivery period starts (the arms share their history), and part after it; outcomes are counted over the outcome window. Each panel is the "
                                "population-weighted mean of that group's sampled units only (table sample)."),
                **(image("trajectories") if m == rec["metrics"][-1] else {})})

    # ---- notes
    notes = {"reading": ("Schedule: the delivery events the engine itself reports for each unit, with the code that configures its simulation, folded per "
                         "group. Delivery period: when the decision delivers. Outcome window: the period over which outcomes are counted. Trajectory "
                         "record: monthly series of the plan and the comparator for a sample of units, run after the analysis. Dynamics checks: what the "
                         "harness tests on those series when it draws them.")}
    if none:
        notes["schedule"] = no_kind + " A deliverable kind with no schedule has no Scenarios content."
    else:
        notes["arms"] = (f"The stored schedule records no arm. arm and charged in the table events are derived by the producer's rule (toolkit/analysis/"
                         "figure_data.py, toolkit/engines/hooks.py): the comparator's schedule gives the history and routine events, each unit's plan "
                         "package gives the decision events, and charged is true exactly for decision events. A kind the hook does not write gets neither.")
        notes["schedule"] = (f"Read from {FD} data.schedule (written with the analysis): {_n(len(rows), 'row')} in {_n(len(sched['groups']), 'group')}, simulated "
                             f"from {_g(sched['start'])}; delivery [{_g(sched['delivery'][0])}, {_g(sched['delivery'][1])}), outcome window "
                             f"[{_g(sched['window'][0])}, {_g(sched['window'][1])}), both half open. Units per group: "
                             + _some(f"{g or '(unnamed)'} {u if u is not None else 'not recorded'}" for g, u in sched["groups"].items()) + "."
                             if usable else f"The stored schedule cannot be shown: {sched['why']}.")
        notes["trajectories"] = (
            f"Read from {TRAJ}" + (f" (written {rec['written_at'][:16]})" if rec.get("written_at") else "") + f": its engine identity equals the analysis's "
            f"({rec['where']}), so it is evidence about this analysis. Whether its other inputs still have their recorded hashes is the Steps view's check."
            if evidence else f"No trajectory record is shown: {rec['says']}.")
    why = {"events": notes.get("schedule"), "series": notes.get("trajectories"), "sample": notes.get("trajectories"), "dynamics": dyn_says,
           "not_produced": no_kind if none else f"The figure index could not be used: {figs_why}.",
           "images": no_kind if none else f"The figure index could not be used: {figs_why}."}
    for name, t in tables.items():
        if t is None:
            notes[name] = why[name]
    images = [{"table": "images", "path": "path", "hash": "sha256", "title": "title", "caption": "caption", "details": ["shows", "fresh"], "flag": "fresh",
               "reason": "reason", "after": "not_produced"}] if tables["images"] is not None else []
    return {"checks": [present, dynamics, window], "facts": facts, "fact_labels": labels, "tables": tables, "charts": charts, "notes": notes, "images": images}


def _self_test():
    import hashlib
    import json

    import yaml

    import workbench
    from lib.safe_io import write_nofollow
    from views import overview
    from views.fit_bench import Bench, check, close, data, fixture_builder, need, says
    fx = fixture_builder()
    b = Bench(lambda run: view(run))        # the module's `view` as it is when a case runs
    folder, look_at, case = b.folder, b.look_at, b.case
    hm, post = fx.METHODS
    est = dict(figures="estimates", kind="estimates")
    FACTS = ("delivery_start", "delivery_end", "window_start", "window_end", "n_groups", "n_events", "has_trajectories",
             "n_sampled_units", "n_seeds", "n_dynamics_checks", "n_dynamics_failed", "n_not_produced")
    COLUMNS = {"events": ["id", "group", "arm", "kind", "component", "label", "start", "end", "units", "charged"],
               "series": ["group", "arm", "metric", "t", "value"],
               "dynamics": ["check", "value", "status", "detail", "panels"],
               "not_produced": ["figure", "reason"]}
    CHECKS = {"trajectories_present": "C5", "dynamics": None, "schedule_window": None}

    def own(v):
        return {str(c.get("id")).split(".")[-1]: c.get("status") for c in v["checks"]}

    def status(v, name):
        return check(v, name)["status"]

    def table(v, name):
        t, columns = v["tables"].get(name), COLUMNS[name]
        need(isinstance(t, dict) and t.get("columns") == columns, f"tables.{name} has columns {columns} (got {t.get('columns') if isinstance(t, dict) else t})")
        need(all(isinstance(r, list) and len(r) == len(columns) for r in t["rows"]), f"tables.{name} rows are lists in column order")
        return [dict(zip(columns, r)) for r in t["rows"]]

    def every(d):
        """Every check of every view for a run folder, by its full id (the overview gathers them)."""
        return {str(c["id"]): c["status"] for c in overview.view(workbench.Run(d))["checks"]}

    def stored(d):
        s = data(d, fx.FD)["schedule"]
        return s, [(g, e) for g, r in s["regions"].items() for e in r["events"]]

    def figure(d, name):
        with open(os.path.join(d, fx.FIGS), encoding="utf-8") as f:
            return next(e for e in yaml.safe_load(f)["data"]["figures"] if e.get("figure") == name)

    # ------------------------------------------------------------------ the generic model
    def m_shape():
        for method in fx.METHODS:
            v = look_at(folder(method))
            kinds = [f"n_events_{k}" for k in fx.EVENT_KINDS]
            need(sorted(v["facts"]) == sorted([*FACTS, *kinds]), f"{method}: the facts of §5.6 and one count per kind recorded (differs in {sorted(set(v['facts']) ^ set([*FACTS, *kinds]))})")
            need(sorted(c["id"] for c in v["checks"]) == sorted(f"scenarios.{n}" for n in CHECKS), f"{method}: three checks, ids scenarios.<name>; arms is dropped (got {[c.get('id') for c in v['checks']]})")
            for c in v["checks"]:
                name = c["id"].split(".")[-1]
                need(c.get("step") == "scenarios" and c.get("status") in workbench.checks.STATUSES and c.get("title"), f"{name}: step, status and title")
                need(isinstance(c.get("says"), str) and c["says"].strip() and "\n" not in c["says"], f"{name}: one plain sentence")
                ev = c.get("evidence")
                need(isinstance(ev, list) and ev and all(isinstance(e, dict) and e.get("file") and "field" in e for e in ev), f"{name}: names the file and field read ({ev})")
                need(c.get("case") == CHECKS[name], f"{name}: scorecard case {CHECKS[name]} (got {c.get('case')})")
            for name in COLUMNS:
                table(v, name)
                need(v["tables"][name].get("about"), f"tables.{name} says what its rows are")
            need(v["notes"] and all(isinstance(t, str) and t.strip() for t in v["notes"].values()), "notes are plain text")
            need(any("derived" in t for t in v["notes"].values()), "a note says arm and charged are derived, by the producer's rule (settled)")

    def m_clean():
        for method in fx.METHODS:
            got = own(look_at(folder(method)))
            need(got == {n: "ok" for n in CHECKS}, f"{method}: on the clean run every check is ok ({got})")
            raised = {k: s for k, s in every(folder(method)).items() if s in ("look", "problem")}
            need(not raised, f"{method}: no view raises anything on the clean run ({raised})")

    def m_facts():
        for method in fx.METHODS:
            d = folder(method)
            f, (s, events) = look_at(d)["facts"], stored(d)
            want = {"delivery_start": 2026.0, "delivery_end": 2028.0, "window_start": 2026.0, "window_end": 2029.0, "n_groups": 3,
                    "n_events": len(events), **{f"n_events_{k}": sum(e["kind"] == k for _, e in events) for k in fx.EVENT_KINDS},
                    "has_trajectories": True, "n_sampled_units": 9, "n_seeds": 3, "n_dynamics_checks": 2, "n_dynamics_failed": 0,
                    "n_not_produced": 0}
            bad = {k: (f.get(k), w) for k, w in want.items() if not (close(f.get(k), w) if isinstance(w, float) else f.get(k) == w)}
            need(not bad, f"{method}: " + "; ".join(f"{k} is {g!r}, expected {w!r}" for k, (g, w) in bad.items()))

    def m_tables():
        for method in fx.METHODS:
            d = folder(method)
            v, (s, events) = look_at(d), stored(d)
            rows = table(v, "events")
            need([r["id"] for r in rows] == list(range(len(events))), "id is the row's position")
            need([(r["group"], r["kind"], r["component"], r["label"], r["start"], r["end"], r["units"]) for r in rows]
                 == [(g, e["kind"], e["component"], e["label"], e["t0"], e["t1"], e["units"]) for g, e in events], "one row per stored row, as stored")
            need(all((r["arm"], r["charged"]) == (("plan", True) if r["kind"] == "decision" else ("comparator", False)) for r in rows),
                 "arm and charged by the producer's rule: decision events are the plan's and charged")
            tr = data(d, fx.TRAJ)
            ser = table(v, "series")
            need({r["group"] for r in ser} == {"total", *fx.GROUPS} and {r["arm"] for r in ser} == {"plan", "comparator"} and {r["metric"] for r in ser} == {"m1"},
                 "series for each group and the pooled sample, both arms")
            g1 = [r["value"] for r in ser if (r["group"], r["arm"]) == ("G1", "plan")]
            need(len(ser) == 4 * 2 * len(tr["series"]["total"]["t"]) and g1 == tr["series"]["regions"]["G1"]["arms"]["plan"]["m1"], "the record's own values, in order")
            dyn = table(v, "dynamics")
            need([{k: r[k] for k in ("check", "value", "status", "detail")} for r in dyn] == figure(d, "trajectories")["checks"] and all(r["panels"] is None for r in dyn),
                 "the harness's checks passed through with its columns; a key it did not write is null")
            need(table(v, "not_produced") == [], "nothing is listed as not produced")

    def m_charts():
        d = folder(hm)
        v = look_at(d)
        need(not workbench.chart_problems(v), f"every chart names a table and columns of the result ({workbench.chart_problems(v)})")
        by = {c["table"]: c for c in v["charts"]}
        need({"events", "series"} <= set(by), f"the schedule and the series are drawn ({sorted(by)})")
        need({m["value"] for m in by["events"].get("limits") or []} >= {2026.0, 2028.0, 2029.0}, "the timeline marks the delivery period and the outcome window")
        need(by["series"].get("group") == "arm" and by["series"].get("panels") == "group", "the series: one line per arm, one panel per group")
        for name, c in (("timeline", by["events"]), ("trajectories", by["series"])):
            with open(os.path.join(d, f"figures/{name}.png"), "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()
            need((c.get("image") or {}).get("path") == f"figures/{name}.png" and c["image"].get("hash") == sha, f"the harness figure {name} is shown beside its chart (§3)")

    # ------------------------------------------------------------------ the settled rules
    def s_reached():
        v = look_at(folder(hm, "C5_no_trajectories"))
        need(status(v, "trajectories_present") == "problem" and v["facts"]["has_trajectories"] is False, "batch and point files with no record: problem")
        need(all(v["facts"][k] is None for k in ("n_sampled_units", "n_seeds", "n_dynamics_checks", "n_dynamics_failed"))
             and v["tables"]["series"] is None and v["tables"]["dynamics"] is None, "what the record would hold is null, never zero")
        need(table(v, "not_produced") == [{"figure": "trajectories", "reason": fx.NO_TRAJECTORIES}] and v["facts"]["n_not_produced"] == 1, "the figure not produced, with the harness's reason")
        v = look_at(folder(hm, upto="calibration"))
        need(status(v, "trajectories_present") == "not_available" and "batch" in says(check(v, "trajectories_present")),
             "a run stopped before the outcome batch is not a problem, and the sentence says it did not reach the batch")

        def no_batch(d):
            os.remove(os.path.join(d, fx.BATCH))
        need(status(look_at(b.edited(hm, no_batch, "C5_no_trajectories")), "trajectories_present") != "problem", "reached means both files exist (settled)")

    def s_dynamics():
        v = look_at(folder(hm, "dynamics_flag"))
        c = check(v, "dynamics")
        need(c["status"] == "problem" and v["facts"]["n_dynamics_failed"] == 1 and "plan equals comparator before delivery" in says(c), "a flagged check is a problem, named")
        v = look_at(folder(hm, "dynamics_unknown_status"))
        need(status(v, "dynamics") == "not_available" and "warn" in check(v, "dynamics")["says"], "an unknown status word is not_available, naming it")
        v = look_at(folder(hm, "trajectories_other_engine"))
        need(status(v, "dynamics") == "not_available" and status(v, "trajectories_present") == "problem" and "engine" in says(check(v, "trajectories_present")),
             "a record for another engine than the analysis's: its checks are not carried")
        v = look_at(folder(hm, "figure_not_produced"))
        need(status(v, "dynamics") == "not_available" and status(v, "trajectories_present") == "ok", "a record whose figure was not produced has no checks to pass through")
        need("info" not in [r["status"] for r in table(look_at(folder(hm)), "dynamics") if r["check"] == "plan_equals_comparator_before_delivery"]
             and look_at(folder(hm))["facts"]["n_dynamics_checks"] == 2, "the producer's information row is counted and raises nothing")

    def s_window():
        for name, t0 in (("schedule_decision_outside", 2028.25), ("schedule_event_after_window", 2029.5)):
            v = look_at(folder(hm, name))
            c = check(v, "schedule_window")
            need(c["status"] == "look" and str(t0) in c["says"] + json.dumps(c.get("rows")), f"{name}: look, giving the event")
        v = look_at(folder(hm, "schedule_unknown_kind"))
        need(status(v, "schedule_window") == "not_available" and "campaign" in check(v, "schedule_window")["says"], "an unknown kind is not_available, naming it")
        odd = [r for r in table(v, "events") if r["kind"] == "campaign"]
        need(len(odd) == 1 and odd[0]["arm"] is None and odd[0]["charged"] is None and v["facts"].get("n_events_campaign") == 1, "its arm is not guessed; it is counted under its own kind")

        def at_end(d):      # the delivery period is half open: a decision event at its end is outside it
            p = os.path.join(d, fx.FD)
            with open(p, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            next(e for e in doc["data"]["schedule"]["regions"]["G1"]["events"] if e["kind"] == "decision")["t0"] = 2028.0
            write_nofollow(d, fx.FD, yaml.safe_dump(doc, sort_keys=False).encode())
        need(status(look_at(b.edited(hm, at_end)), "schedule_window") == "look", "a decision event at the end of the delivery period is outside it")

        def not_ok(d):
            p = os.path.join(d, fx.FD)
            with open(p, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            doc["data"]["schedule"] = {"status": "not_offered", "reason": "the engine offers no schedule"}
            write_nofollow(d, fx.FD, yaml.safe_dump(doc, sort_keys=False).encode())
        v = look_at(b.edited(hm, not_ok))
        need(status(v, "schedule_window") == "not_available" and "the engine offers no schedule" in check(v, "schedule_window")["says"] and v["tables"]["events"] is None,
             "a schedule the producer recorded as not offered: not_available with its reason")

    def s_kind():
        v = look_at(folder(post, **est))
        need(own(v) == {n: "not_available" for n in CHECKS}, f"a kind with no schedule: nothing to judge ({own(v)})")
        need(all(v["tables"].get(n) is None for n in ("events", "series", "dynamics")) and v["charts"] == [], "no content")
        need(any("no schedule" in t for t in v["notes"].values()) and all("schedule" in says(c) or "batch" in says(c) or "trajector" in says(c) for c in v["checks"]), "and the view says so")

    # ------------------------------------------------------------------ each planted edit, across every view
    def planted(method, name):
        def run():
            spec = fx.DEFECTS[name]
            base = est if "estimates" in spec.get("kinds", ()) else {}
            before, after = every(folder(method, **base)), every(folder(method, name, **base))
            want = {f"scenarios.{k}": s for k, s in {spec["check"]: spec["status"], **spec.get("also", {})}.items()}
            need(all(after.get(k) == s for k, s in want.items()), f"expected {want}, got { {k: after.get(k) for k in want} }")
            want = {k: s for k, s in {**want, **spec.get("across", {})}.items() if before.get(k) != s}
            got = {k: s for k, s in after.items() if before.get(k) != s}
            need(set(before) == set(after) and got == want, f"moves {got}, expected {want}")
        return run

    def others_edits():
        for method in fx.METHODS:
            before = own(look_at(folder(method)))
            for name, spec in fx.DEFECTS.items():
                if spec.get("view") == "scenarios" or method not in spec["methods"] or "package_per_unit" not in spec.get("kinds", fx.KINDS[:1]) or "against" in spec:
                    continue
                want = {k[10:]: s for k, s in spec.get("across", {}).items() if k.startswith("scenarios.")}
                got = {k: s for k, s in own(look_at(folder(method, name))).items() if before.get(k) != s}
                need(got == want, f"{method}, {name}: moves {got}, expected {want}")

    # ------------------------------------------------------------------ fail closed
    def f_missing():
        d = os.path.join(b.tmp, "empty")
        os.makedirs(d)
        write_nofollow(d, "run_state.json", b"{}")
        need(own(look_at(d)) == {n: "not_available" for n in CHECKS}, "a run with none of the files: every check not_available")

        def cut(rel):
            return lambda d: write_nofollow(d, rel, b"data: [1, 2\n")
        v = look_at(b.edited(hm, cut(fx.FD)))
        need(status(v, "schedule_window") == "not_available" and v["facts"]["n_events"] is None and v["tables"]["events"] is None, "a schedule file that is not YAML: no count, no table")
        v = look_at(b.edited(hm, cut(fx.TRAJ)))
        need(status(v, "trajectories_present") not in ("ok", "look") and status(v, "dynamics") == "not_available", "a trajectory record that is not YAML is not a record")

        def other_schema(d):
            p = os.path.join(d, fx.TRAJ)
            with open(p, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            doc["schema"] = "analysis.trajectories/9"
            write_nofollow(d, fx.TRAJ, yaml.safe_dump(doc, sort_keys=False).encode())
        v = look_at(b.edited(hm, other_schema))
        need(status(v, "trajectories_present") == "not_available" and "analysis.trajectories/9" in check(v, "trajectories_present")["says"], "an unknown schema is not_available, naming it")

        def not_offered(d):
            p = os.path.join(d, fx.TRAJ)
            with open(p, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            doc["data"] = {"status": "done", "sample": {}, "series": {}}
            write_nofollow(d, fx.TRAJ, yaml.safe_dump(doc, sort_keys=False).encode())
        v = look_at(b.edited(hm, not_offered))
        need(status(v, "trajectories_present") == "not_available" and "done" in check(v, "trajectories_present")["says"], "a status word the producer does not write is not_available, naming it")

    def f_words():
        words = fx.domain_words()
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            text = f.read().lower()
        need(len(words) > 50 and not [w for w in words if w in text], f"no domain word in this module ({[w for w in words if w in text][:5]})")
        v = look_at(folder(hm))
        need(not [w for w in words if w in json.dumps(v).lower()], "nor in what the view returns for a fixture run: every name comes from the run")
        need(fx.GROUPING not in json.dumps([t["columns"] for t in v["tables"].values() if t]), "the grouping column's own name is never a column of the model")

    case("M1 the generic model: the facts with one count per kind, three checks with sentence, evidence and case, four tables", m_shape)
    case("M2 on both clean runs every check is ok and no view raises anything", m_clean)
    case("M3 facts on both clean runs, each from the file that holds it", m_facts)
    case("M4 tables: events with id, label and the derived arm and charged; series; dynamics with the producer's columns; not_produced", m_tables)
    case("M5 charts name tables and columns that exist; the timeline with its limits and the series, each with the harness figure", m_charts)
    case("S1 settled: reached the outcome batch means the batch and point files exist; with no record, problem (C5)", s_reached)
    case("S2 settled: a status of flag is a problem; unknown words and a record for another engine are not_available", s_dynamics)
    case("S3 schedule_window: half-open periods, an unknown kind, a schedule the producer did not record", s_window)
    case("S4 settled: a deliverable kind with no schedule has no content and says so", s_kind)
    n = 0
    for method in fx.METHODS:
        for name in fx.SCENARIOS_DEFECTS:
            spec = fx.DEFECTS[name]
            if method not in spec["methods"]:
                continue
            n += 1
            also = "".join(f", {k} {s}" for k, s in {**spec.get("also", {}), **spec.get("across", {})}.items())
            case(f"P{n} {method}, {name}: {spec['check']} is {spec['status']}{also}, and no other check of any view changes", planted(method, name))
    case("Q1 the other views' edits move the Scenarios checks they name, and no other", others_edits)
    case("F1 fail closed: no files, files that are not YAML, an unknown schema, an unknown status word", f_missing)
    case("F2 no domain word in this module or in what it returns; names come from the run", f_words)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: scenarios.py --self-test")
