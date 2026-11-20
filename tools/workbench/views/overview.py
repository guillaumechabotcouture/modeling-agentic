#!/usr/bin/env python3
"""The Overview (docs/design/review-workbench.md §5.1): the question, how the run ended, and every check of every
view that exists, grouped by view in reading order and worst first within a view. Views not built yet are listed
with the goal that builds them.

    python3 tools/workbench/views/overview.py --self-test
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402


def view(run):
    summ, views, found, rows = run.summary(), run.views(), [], []
    for v in views:
        if v["id"] != "overview":
            cs = run.view(v["id"])["checks"] if v["built"] else []
            found.append((v, sorted(cs, key=lambda c: checks.WORST_FIRST.index(c["status"]))))
    every = [c for _, cs in found for c in cs]
    count = {s: sum(c["status"] == s for c in every) for s in checks.WORST_FIRST}
    for v in views:
        cs = every if v["id"] == "overview" else next(c for f, c in found if f is v)
        rows.append([v["id"], v["label"], (checks.worst([c["status"] for c in cs]) or "ok") if v["built"] else "not_built",
                     *(sum(c["status"] == s for c in cs) for s in checks.WORST_FIRST), v["goal"]])
    ended = next((c["says"] for c in every if c["id"] == "steps.outcome"), None)
    todo = [f"{v['label']} (goal {v['goal']})" for v in views if not v["built"]]
    return {
        "checks": every,
        "facts": {"question": summ["question"] or None, "how_it_ended": ended, "status": summ["status"],
                  "pipeline": summ["pipeline"], "launcher": summ["launcher"], "started": summ["started"],
                  "finished": summ["finished"],
                  "last_file_written": (f"{summ['last_written_ago']} ago ({summ['last_written_file']}, {summ['last_written_utc']})"
                                        if summ["last_written_ago"] else None),
                  "round": summ["round"], "model_spend_usd": summ["spent_usd"],
                  "checks_problem": count["problem"], "checks_look": count["look"],
                  "checks_not_available": count["not_available"], "checks_ok": count["ok"]},
        "tables": {"views": {"about": "One row per view, in reading order: its most serious check status, the number "
                                      "of checks at each status, and the goal that builds it.",
                             "columns": ["view", "name", "status", *checks.WORST_FIRST, "goal"], "rows": rows}},
        "charts": [],
        "notes": {"checks": "A check is a rule in code, run on the run's files. Problem: a file contradicts another, a "
                            "rule in code is broken or work was lost; it needs you. Look: for you to judge. Not "
                            "available: a file could not be read or recognised. OK: nothing to do.",
                  "not_built": ("Views not built yet, so their checks are missing here: " + "; ".join(todo) + "."
                                if todo else "Every view is built."),
                  "facts": "Read from run_state.json and metadata.json; last file written is the newest modification "
                           "time among the files of the run folder. Headline results appear here once the Outcomes "
                           "view is built."}}


def _self_test():
    import tempfile
    import workbench
    fails = []

    def ok(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    def ck(view_id, name, status):
        return checks.result(f"{view_id}.{name}", view_id, name, status, "A sentence.", [("a.yaml", "x")])

    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "run")
        os.makedirs(d)
        workbench._toy(d)
        order = [i for i, _, _ in workbench.PATH]
        planted = {"spec": [ck("spec", "a", "ok"), ck("spec", "b", "look")], "fit": [ck("fit", "a", "ok")],
                   "steps": [ck("steps", "a", "not_available"), ck("steps", "b", "problem")]}
        for name in order[1:]:
            workbench.VIEW_FNS[name] = (lambda cs: lambda run: {"checks": cs})(planted.get(name, []))
        try:
            v = view(workbench.Run(d, now=4102444800))
        finally:
            for name in order[1:]:
                workbench.VIEW_FNS.pop(name)
        got = [(c["step"], c["status"]) for c in v["checks"]]
        ok("O1 checks are grouped by view in reading order whatever their statuses, worst first within a view",
           got == [("spec", "look"), ("spec", "ok"), ("fit", "ok"), ("steps", "problem"), ("steps", "not_available")], str(got))
        rows = [dict(zip(v["tables"]["views"]["columns"], r)) for r in v["tables"]["views"]["rows"]]
        ok("O2 one row per view in reading order, each with its worst status; the overview's own row counts every check",
           [r["view"] for r in rows] == order and rows[0]["status"] == "problem" and rows[0]["ok"] == 2
           and {r["view"]: r["status"] for r in rows}["steps"] == "problem" and v["facts"]["checks_look"] == 1, str(rows))
        ok("O3 the facts say how long ago the run's last file was written, in plain words, with the file",
           "ago (" in str(v["facts"]["last_file_written"]) and "days ago" in v["facts"]["last_file_written"]
           and v["facts"]["question"] == "toy question", str(v["facts"]))
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: overview.py --self-test")
