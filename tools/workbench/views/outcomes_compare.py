#!/usr/bin/env python3
"""The Outcomes comparison with another run of the same question (docs/design/review-workbench.md §5.7, check
run_shift, scorecard case C6): what moved between two runs, and which keys of their signed specs differ.

    python3 tools/workbench/views/outcomes_compare.py --self-test

`compare(m, other, label)` takes the generic models of this run and of the run chosen in the page (None when none is
chosen) and returns {check, shift, spec_diff, outside_spec, against}. Rules:

- Nothing is compared, and both tables are None, until a run is chosen; when either run has no verified signed spec
  (the comparison rests on the two specs, and only a verified one says anything, §3); when the two verified signed
  specs name different questions (their `question`, compared as text with spaces folded); when the two deliverable
  kinds differ; and when either run records no plan or headline to compare.
- shift: one row per package of either plan (this run's first, most units first; then the other run's), with the
  number of units in each run (0 where a plan does not hold it) and the difference this run minus the other; then
  `headline` (the point value; for the estimates kind, which has none, the median), `envelope` (the verified signed
  spec's constraints.envelope_usd) and `comparator` (its comparator.text; change is "differs", or null when the two
  texts are the same).
- spec_diff: the two verified signed specs, key by key to two levels; one row per key that differs; a cell holds a
  scalar's value, the count of entries of a list or mapping, or null where the spec has no such key. It is computed
  here from the two documents and never read from the harness's comparison figure (gap H12).
- outside_spec: what each run records of itself outside its spec, side by side (outcomes_read.provenance: the launch
  commit and pipeline of metadata.json, the engine identity in the analysis header, each recorded input hash), with
  `same` per row; the check's sentence names the rows that differ.
- run_shift: a package whose unit count changes by more than 10% of the units, or a headline that changes by more than
  a factor of 1.5 (workbench thresholds), is a shift. The factor is of magnitudes, for two headlines of one sign; a
  headline of zero in one run, or one that changes sign, has no factor and is `look`, named. A shift with a spec key
  that differs is `look` and names every key; a shift with none is `problem` (something outside the spec changed) and
  names the outside_spec rows that differ. No shift with a spec key that differs is `look` (a key changed is still a
  change); no shift and no key is `ok`; within 25% of either threshold is a near miss, `look` (for the factor, 25% of
  its excess over 1, as the Fit view does for a ratio near 1). With no run chosen the sentence asks for one.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
from views import outcomes_read as R  # noqa: E402
from views.outcomes_read import at, g, num, pct, plural, r3, some  # noqa: E402

UNITS_SHARE, FACTOR = 0.10, 1.5        # workbench thresholds (§5.7)
TITLE = "Shift in the plan or the headline against another run of the same question"
SHIFT, DIFF = ["item", "this_run", "other_run", "change"], ["key", "this_run", "other_run"]
ABSENT = object()


def _r(status, says, rows=None):
    ev = [(R.SIGNED, "every key, to two levels, in both runs"), (R.POINT, "data.plan, data.averted in both runs")]
    return checks.result("outcomes.run_shift", "outcomes", TITLE, status, says, ev, rows, "C6")


def _cell(v):
    return None if v is ABSENT else len(v) if isinstance(v, (list, dict)) else v


def spec_diff(a, b):
    """Rows {key, this_run, other_run} for each key, to two levels, on which two spec documents differ."""
    rows = []
    for k in list(a) + [k for k in b if k not in a]:
        x, y = a.get(k, ABSENT), b.get(k, ABSENT)
        if isinstance(x, dict) and isinstance(y, dict):
            for j in list(x) + [j for j in y if j not in x]:
                if x.get(j, ABSENT) != y.get(j, ABSENT):
                    rows.append({"key": f"{k}.{j}", "this_run": _cell(x.get(j, ABSENT)), "other_run": _cell(y.get(j, ABSENT))})
        elif x is ABSENT or y is ABSENT or x != y:
            rows.append({"key": str(k), "this_run": _cell(x), "other_run": _cell(y)})
    return rows


def _headline(m):
    h = m["headline"] or {}
    return h.get("point") if m["kind"] != "estimates" else h.get("median")


def _counts(m):
    return {r["package"]: r["units"] for r in m["plan_rows"] or []}


def outside(mine, theirs):
    """Rows {item, this_run, other_run, same} of what the two runs record of themselves outside their specs
    (outcomes_read.provenance): the launch commit and pipeline, the engine identity, each recorded input hash. A cell
    is as in spec_diff; `same` compares the whole values. The rows that differ come first."""
    mine, theirs = mine or {}, theirs or {}
    def short(v):       # a digest is shown by its first ten characters; the whole values are what is compared
        v = _cell(v)
        return v[:10] if isinstance(v, str) and len(v) >= 40 and all(ch in "0123456789abcdef" for ch in v) else v
    rows = [{"item": k, "this_run": short(mine.get(k, ABSENT)), "other_run": short(theirs.get(k, ABSENT)), "same": mine.get(k, ABSENT) == theirs.get(k, ABSENT)}
            for k in list(mine) + [k for k in theirs if k not in mine]]
    return sorted(rows, key=lambda r: r["same"])        # what differs is read first; each part keeps its order


def compare(m, other, label=None):
    out = {"shift": None, "spec_diff": None, "outside_spec": None, "against": label if other is not None else None}

    def no(why):
        return {**out, "check": _r("not_available", f"Not available: {why}.")}
    if other is None:       # waiting for the reader, not a file that could not be read (§12, reader finding 2)
        return {**out, "check": _r("not_available", "Choose a run to compare with: no comparison run is chosen, and the control at the top of this page lists "
                                                    "the other runs of the same question. Nothing is wrong or missing in this run's files.")}
    for which, x in (("this run", m), (f"the comparison run ({label})", other)):
        if not x["spec"]["verified"]:
            return no(f"{which} has no verified signed spec ({x['spec']['why']}), and two runs are compared only through the specs their owners signed")
    a, b = m["spec"]["doc"], other["spec"]["doc"]
    asks = [" ".join(str(d.get("question") or "").split()) for d in (a, b)]
    if not all(asks) or asks[0] != asks[1]:
        return no(f"the two verified signed specs name different questions, so the runs are not compared (this run: \"{asks[0][:120]}\"; "
                  f"{label}: \"{asks[1][:120]}\")")
    if m["kind"] != other["kind"] or m["kind"] not in R.KINDS:
        return no(f"the two runs' deliverable kinds are {m['kind']} and {other['kind']}, which are not compared")
    mine, theirs, plans = _headline(m), _headline(other), m["kind"] == "package_per_unit"
    if plans and (m["plan_rows"] is None or other["plan_rows"] is None):
        which = "this run" if m["plan_rows"] is None else f"the comparison run ({label})"
        return no(f"{which} records no plan to compare ({(m if m['plan_rows'] is None else other)['why'].get(R.POINT) or R.POINT + ' holds no plan'})")
    if num(mine) is None or num(theirs) is None:
        return no(f"{'this run' if num(mine) is None else 'the comparison run'} records no headline value to compare")
    diff = spec_diff(a, b)
    here, there = _counts(m), _counts(other)
    names = list(here) + [p for p in there if p not in here]
    shift = [{"item": p, "this_run": here.get(p, 0), "other_run": there.get(p, 0), "change": here.get(p, 0) - there.get(p, 0)} for p in names]
    shift.append({"item": "headline", "this_run": mine, "other_run": theirs, "change": mine - theirs})
    env = [num(at(d, "constraints", "envelope_usd")) for d in (a, b)]
    shift.append({"item": "envelope", "this_run": env[0], "other_run": env[1], "change": None if None in env else env[0] - env[1]})
    words = [R.text(at(d, "comparator", "text"), 4000) for d in (a, b)]
    shift.append({"item": "comparator", "this_run": words[0], "other_run": words[1], "change": "differs" if words[0] != words[1] else None})
    beside = outside(m.get("provenance"), other.get("provenance"))
    out.update(shift=shift, spec_diff=diff, outside_spec=beside)
    n = max(sum(here.values()), sum(there.values()), 1)
    # The factor is of magnitudes, for two values of one sign; a zero and a change of sign have no factor and are named.
    factor, odd = None, None
    if mine == theirs:
        factor = 1.0
    elif mine == 0 or theirs == 0:
        odd = f"the headline is zero in {'this run' if mine == 0 else label} and {g(theirs if mine == 0 else mine)} in {label if mine == 0 else 'this run'}, so it has no factor of change"
    elif (mine > 0) != (theirs > 0):
        odd = f"the headline changes sign ({g(theirs)} in {label}, {g(mine)} in this run), so it has no factor of change"
    else:
        factor = max(abs(mine) / abs(theirs), abs(theirs) / abs(mine))
    edge = 1 + (1 - R.NEAR) * (FACTOR - 1)
    moved = [r for r in shift[:len(names)] if abs(r["change"]) > UNITS_SHARE * n]
    close = [r for r in shift[:len(names)] if r not in moved and abs(r["change"]) >= (1 - R.NEAR) * UNITS_SHARE * n]
    said = []
    if moved:
        said.append(f"{plural(len(moved), 'package changes', 'packages change')} by more than {pct(UNITS_SHARE)} of the {n} units: "
                    + "; ".join(f"{r['item']} {r['other_run']} to {r['this_run']} units" for r in moved[:R.SHOWN]))
    if factor is not None and factor > FACTOR:
        said.append(f"the headline changes by a factor of {r3(factor)} ({g(theirs)} in {label}, {g(mine)} in this run), above {FACTOR}")
    keys = (f"{plural(len(diff), 'key')} of the two verified signed specs {'differs' if len(diff) == 1 else 'differ'} (table spec_diff): "
            f"{some((r['key'] for r in diff), 16)}") if diff else "No key of the two verified signed specs differs to two levels"
    gone = [r["item"] for r in beside if not r["same"]]
    # what was given to the run (its launch, its engine, its own inputs) is named first; the hashes of analysis files differ whenever anything before them does
    given, computed = [i for i in gone if not i.startswith("input analysis/")], [i for i in gone if i.startswith("input analysis/")]
    listed = "; ".join(x for x in (some(given, 6), f"{'and ' if given else ''}the recorded hashes of {plural(len(computed), 'analysis file')} computed from the run's inputs "
                                                 f"({some(computed, 3)})" if computed else "") if x)
    differs = f"{len(gone)} of the {len(beside)} items the runs record of themselves {'differs' if len(gone) == 1 else 'differ'} (table outside_spec): {listed}"
    beyond = (f"Outside the spec, {differs}." if gone else
              f"Outside the spec, none of the {len(beside)} items the runs record of themselves differs (launch commit, pipeline, engine identity, input hashes; table outside_spec).")
    table = {"columns": SHIFT, "rows": [[r[c] for c in SHIFT] for r in shift]}
    head = f"Against {label}: "
    if said and not diff:
        return {**out, "check": _r("problem", head + "; ".join(said + ([odd] if odd else [])) + f" (workbench thresholds). {keys}, so something outside the spec changed: "
                                   + (f"{differs}." if gone else f"and none of the {len(beside)} items the runs record of themselves differs either (table outside_spec), "
                                      "so the cause is in nothing either run recorded."), table)}
    if said or odd:
        return {**out, "check": _r("look", head + "; ".join(said + ([odd] if odd else [])) + f" (workbench thresholds). {keys}. {beyond} For you to judge whether "
                                   "what differs explains the shift.", table)}
    nearly = ([f"{some(r['item'] for r in close)} change by 7.5% to 10% of the units"] if close else []) + (
        [f"the headline changes by a factor of {r3(factor)}"] if factor >= edge else [])
    if nearly:
        return {**out, "check": _r("look", f"Against {label}, a near miss: " + "; ".join(nearly) + f", within 25% of the thresholds ({pct(UNITS_SHARE)} of units; "
                                   f"a factor of {FACTOR}; workbench thresholds). {keys}. {beyond}", table)}
    still = f"no package changes by more than {pct(UNITS_SHARE)} of the units and the headline changes by a factor of {r3(factor)}, not above {FACTOR} (workbench thresholds)"
    if diff:        # §12, reader finding 3: a key changed with no shift is still a change
        return {**out, "check": _r("look", f"{head}{keys}, and {still}. A key changed with no shift is still a change: for you to judge whether it should have "
                                   f"moved the result. {beyond}", table)}
    return {**out, "check": _r("ok", f"{head}{still}. {keys}. {beyond}", table)}


def _self_test():
    import workbench
    from views import outcomes_package as K
    from views.fit_bench import Bench, fixture_builder, need
    fx = fixture_builder()
    b = Bench(lambda run: None)
    hm = fx.METHODS[0]

    def model(**how):
        run = workbench.Run(b.folder(hm, **how))
        return K.read(run, R.spec(run))

    def diffs():
        got = spec_diff({"a": 1, "b": {"x": [1, 2], "y": {"deep": 1}, "z": 3}, "c": [1], "e": {"k": 1}},
                        {"a": 1, "b": {"x": [1, 2, 3], "y": {"deep": 2}, "w": 0}, "d": "new", "e": 5})
        need(got == [{"key": "b.x", "this_run": 2, "other_run": 3}, {"key": "b.y", "this_run": 1, "other_run": 1}, {"key": "b.z", "this_run": 3, "other_run": None},
                     {"key": "b.w", "this_run": None, "other_run": 0}, {"key": "c", "this_run": 1, "other_run": None}, {"key": "e", "this_run": 1, "other_run": 5},
                     {"key": "d", "this_run": None, "other_run": "new"}], f"keys to two levels; a cell is a scalar, a count, or null where absent ({got})")

    def runs():
        clean = model()
        want = {"twin": "ok", "second": "look", "shifted": "look", "shifted_same_spec": "problem", "headline": "look", "other_question": "not_available", "unverified": "not_available"}
        got = {name: compare(clean, model(other=name), name)["check"]["status"] for name in want}
        need(got == want and compare(clean, None)["check"]["status"] == "not_available", f"each comparison run of the fixtures ({got})")
        c = compare(clean, model(other="shifted"), "the other")
        need(c["against"] == "the other" and [r["item"] for r in c["shift"]] == ["A+B", "A+C", "headline", "envelope", "comparator"]
             and [r["key"] for r in c["spec_diff"]] == ["constraints.envelope_usd"] and "constraints.envelope_usd" in c["check"]["says"], "the tables and the key named")
        for name in ("other_question", "unverified"):
            c = compare(clean, model(other=name), name)
            need(c["shift"] is None and c["spec_diff"] is None, f"{name}: nothing is compared")
        other = model(other="second")
        other["headline"] = {**other["headline"], "point": clean["headline"]["point"] * 1.4}
        need(compare(clean, other, "x")["check"]["status"] == "look" and "near miss" in compare(clean, other, "x")["check"]["says"],
             "a headline within 25% of the factor is a near miss")

    def signs():
        def against(mine, theirs):
            a, o = model(), model()
            a["headline"], o["headline"] = {**a["headline"], "point": mine}, {**o["headline"], "point": theirs}
            return compare(a, o, "x")["check"]
        c = against(-100.0, -101.0)
        need(c["status"] == "ok" and "1.01" in c["says"], f"two negative headlines are compared by their magnitudes ({c['status']}: {c['says']})")
        need(against(-100.0, -160.0)["status"] == "problem" and against(-100.0, -140.0)["status"] == "look", "past the factor, and near it, as for positive ones")
        c = against(0.0, 50.0)
        need(c["status"] == "look" and "zero" in c["says"] and "factor" not in c["says"].split("zero")[0][-40:], f"a headline of zero in one run is named, with no factor ({c['status']}: {c['says']})")
        c = against(-50.0, 50.0)
        need(c["status"] == "look" and "sign" in c["says"], f"a headline that changes sign is named ({c['status']}: {c['says']})")
        need(against(0.0, 0.0)["status"] == "ok", "both zero: no change")

    b.case("X1 the two specs key by key to two levels", diffs)
    def outside():
        clean = model()
        c = compare(clean, model(other="shifted_same_spec"), "the other")
        rows = {r["item"]: r for r in c["outside_spec"]}
        need({"launch commit", "pipeline"} <= set(rows) and rows["launch commit"]["same"] is True and any(i.startswith("engine ") for i in rows)
             and [r["same"] for r in c["outside_spec"]] == sorted(r["same"] for r in c["outside_spec"])
             and any(i.startswith("input ") for i in rows), f"outside_spec: the launch commit, the pipeline, the engine identity and each recorded input hash, side by side ({list(rows)})")
        moved = [i for i, r in rows.items() if not r["same"]]
        need(moved and all(i.startswith("input analysis/") for i in moved) and all(i in c["check"]["says"] for i in moved[:3]) and "outside_spec" in c["check"]["says"],
             f"a shift with no spec key that differs names the recorded items that do differ, and the table ({c['check']['says']})")
        other = model(other="shifted_same_spec")
        other["provenance"] = {**other["provenance"], "launch commit": "0" * 40, "engine version": "9"}
        c = compare(clean, other, "the other")
        need(next(r for r in c["outside_spec"] if r["item"] == "launch commit")["other_run"] == "0" * 10, "a digest is shown by its first ten characters")
        need(c["check"]["status"] == "problem" and "launch commit" in c["check"]["says"] and "engine version" in c["check"]["says"]
             and [r["same"] for r in c["outside_spec"] if r["item"] in ("launch commit", "engine version")] == [False, False], f"another commit and engine are named first ({c['check']['says']})")
        c = compare(clean, model(other="twin"), "the other")
        need(c["check"]["status"] == "ok" and all(r["same"] for r in c["outside_spec"]) and "none of the" in c["check"]["says"],
             f"the clean pair: nothing differs, in the spec or outside it ({c['check']['says']})")
        c = compare(clean, model(other="second"), "the other")
        need(c["check"]["status"] == "look" and "1 key" in c["check"]["says"] and "constraints.envelope_usd" in c["check"]["says"] and "still a change" in c["check"]["says"],
             f"a spec key that differs with no shift is look: a key changed is still a change ({c['check']['says']})")
        c = compare(clean, None)
        need(c["check"]["status"] == "not_available" and c["check"]["says"].startswith("Choose a run to compare with") and "could not" not in c["check"]["says"]
             and c["outside_spec"] is None, f"a check that waits for a choice says so, not that a file could not be read ({c['check']['says']})")

    b.case("X4 reader findings 1 to 3: the outside_spec table and the sentence that points to it; a differing key with no shift; waiting for a choice", outside)
    b.case("X3 review 9: negative headlines by magnitude; a zero and a change of sign each named, apart from the factor", signs)
    b.case("X2 run_shift against each comparison run; nothing compared across questions or with an unverified spec", runs)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: outcomes_compare.py --self-test")
