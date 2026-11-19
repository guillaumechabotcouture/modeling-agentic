#!/usr/bin/env python3
"""The science register's checks, facts, tables and charts (docs/design/review-workbench.md §5.8, §6), computed on
the generic Register model that views/register_rows.py derives. views/register.py's `view` is `result(derive(run))`.

    python3 tools/workbench/views/register_checks.py --self-test

Facts, by_source, by_disposition and every check count science rows; process rows (a harness check's finding about
the run's files) are counted apart and listed in their own table. The reader's order: how many science rows have no
disposition (the first check and the first fact); the rows, open and decision-bearing first; the charts by
disposition and by source; the comment lines that are not rows yet; the owner's notes; the process rows.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
import review_notes  # noqa: E402
from views.register_rows import (CHANGES, CHOICE, COLUMNS, DOSSIER, DT, LEDGER, NOTES, SCOPE, SEVERITIES, SHEET,  # noqa: E402
                                 SIGNED, SOURCES, VERDICTS, WORDS)

CHECKS = {"undisposed": "C12", "tests_unlinked": None, "declared_untested": None, "decides_unknown": None,
          "prose_only": None, "notes": None}
TITLES = {"undisposed": "Science rows with no disposition",
          "tests_unlinked": "Decision tests that change the plan and are linked to no issue",
          "declared_untested": "Declared rows whose decision test did not run, or that name none",
          "decides_unknown": "Rows with no decision test: whether they bear on the decision is unknown",
          "prose_only": "Departures, limitations and assumptions written only as comments in the signed spec",
          "notes": "The owner's review notes that could not be used"}
FACTS = ["n_open", "n_rows", "n_declared", "n_tested", "n_implemented", "n_withdrawn", "n_decides_yes",
         "n_decides_unknown", "n_owner_notes", "n_process", "n_process_open"]
TERMS = ("A row is one science issue, uncertainty or assumption, derived from the run's files each time this page is "
         "read. Disposition: open (nothing answers it), declared (a signed spec, a scope declaration or the owner "
         "accepts it as a stated limit), tested (a decision test was run on it), implemented (the ledger records it as "
         "fixed), withdrawn (the owner's note withdrew it). Decides: yes when a linked decision test's verdict is "
         f"{CHANGES} (the recommended plan changes), no when every linked test's verdict is NO_CHANGE, else unknown. "
         "Nature: science, or process for a finding a harness check raised about the run's files; process rows are "
         "counted apart. Signed by: who signed the spec that carries a declared row, or owner where the owner answered.")
ABOUT = {
    "rows": ("One row per science issue, uncertainty or assumption, in reading order: open rows first (those that bear "
             "on the decision, then those where that is unknown), then rows with a disposition, then process rows. "
             "statement is in the run's own words; source and files say where it came from; tests and verdicts are the "
             "decision tests linked to it by a structured key, with each test's verdict in analysis/decision_tests.yaml. "
             "Needs you: an open row has no disposition. Review sets one, adds a note or folds a row into another; "
             "that is written to owner/review_notes.jsonl and never changes a file of the run."),
    "by_disposition": "Science rows per disposition (the facts' counts), for the chart.",
    "by_source": ("Science rows per source, each row counted once under the first source it was derived from, and how "
                  "many of them are open. A source this run does not have is absent."),
    "prose_only": (f"Comment lines of the signed spec ({SIGNED}) that start a departure, limitation or assumption (line "
                   "is the line's number in that file; continues holds the comment lines that follow it, up to four). "
                   "They are shown, not turned into rows; to make one a row, add it with Add a row above the rows table."),
    "notes": ("The owner's review notes in the order written, each beside the disposition the files alone give "
              "(derived disposition; empty for a row the owner added)."),
    "process": ("Findings a harness check raised about the run's files (a stale or missing file, a broken contract), not "
                "about the science. They are counted apart from the science rows and move no check on this page.")}
LABELS = {"decides": "bears on the decision", "tests": "linked decision tests", "verdicts": "one per test",
          "blocking": "blocks the gate", "nature": "science or process", "signed_by": "who signed or answered"}
ACTIONS = {
    "post": "/api/note", "key": "id", "label": "Review",
    "about": "Saved as one line of owner/review_notes.jsonl; the derived value stays beside your note in the notes table.",
    "fields": [{"name": "disposition", "label": "Disposition", "choices": list(review_notes.DISPOSITIONS)},
               {"name": "decides", "label": "Bears on the decision", "choices": list(review_notes.DECIDES)},
               {"name": "same_as", "label": "Same as row (that row is folded into this one)", "rows": True},
               {"name": "note", "label": "Note", "text": True}],
    "add": {"label": "Add a row", "fields": [
        {"name": "kind", "label": "Kind", "choices": list(review_notes.KINDS), "required": True},
        {"name": "statement", "label": "Statement", "text": True, "required": True},
        {"name": "disposition", "label": "Disposition", "choices": list(review_notes.DISPOSITIONS)},
        {"name": "decides", "label": "Bears on the decision", "choices": list(review_notes.DECIDES)},
        {"name": "note", "label": "Note", "text": True}]}}
ORDER = {("open", "yes"): 0, ("open", "unknown"): 1, ("open", "no"): 2}
LATER = {None: 3, "declared": 4, "tested": 5, "implemented": 6, "withdrawn": 7}


def _r(name, status, says, evidence, rows=None):
    return checks.result(f"register.{name}", "register", TITLES[name], status, says, evidence, rows, CHECKS[name])


def _ids(rows, cap=12):
    ids = [r["id"] if isinstance(r, dict) else str(r) for r in rows]
    return ", ".join(ids[:cap]) + (f" and {len(ids) - cap} more" if len(ids) > cap else "")


def _top(r):
    """Blocking, or of the highest severity; a severity word the ledger does not write is not assumed to be lower."""
    return r["blocking"] is True or (r["severity"] is not None and r["severity"] not in SEVERITIES[1:])


def check_undisposed(m, sci):
    ev = [(LEDGER, "status, severity, blocking"), (SCOPE, "declarations"), (CHOICE, "engine_departures[].status"),
          (SIGNED, "calibration.joint.unmatched.declared, uncertainty.excluded"), (SHEET, "lines marked HIGH"),
          (DOSSIER, "inputs[].no_data"), (NOTES, "disposition")]
    if m["nothing"]:
        return _r("undisposed", "not_available", "The run has none of the files the register is derived from (no signed "
                  "spec, findings ledger, scope declaration, engine choice, sign-off sheet, dossier or decision tests).", ev)
    open_ = [r for r in sci if r["disposition"] == "open"]
    top = [r for r in open_ if _top(r)]
    flags = list(dict.fromkeys(m["flags"]))
    status = ("problem" if top or any(s == "problem" for s, _ in flags) else
              "not_available" if flags else "look" if open_ else "ok")
    if not open_:
        says = f"All {len(sci)} science rows have a disposition." if sci else "The register holds no science row."
    else:
        says = (f"{len(open_)} of {len(sci)} science rows have no disposition; " + (
            f"{len(top)} of them {'is' if len(top) == 1 else 'are'} blocking or of the highest severity "
            f"({SEVERITIES[0]}): {_ids(top)}." if top else
            f"none is blocking or of the highest severity: {_ids(open_)}.") + " Needs you: each open row needs a "
            "disposition (a fix, a decision test, a declaration, or your note).")
    if flags:
        says += " Also: " + " ".join(why for _, why in flags[:4]) + (f" ({len(flags) - 4} more of this kind.)" if len(flags) > 4 else "")
    cols = ["id", "severity", "blocking", "decides", "first_visible_stage", "source", "statement"]
    rows = [[r["id"], r["severity"], r["blocking"], r["decides"], r["first_visible_stage"], ",".join(r["sources"]),
             r["statement"]] for r in _ordered(open_)]
    return _r("undisposed", status, says, ev, {"columns": cols, "rows": rows} if rows else None)


def _no_tests(m, name, ev):
    return _r(name, "not_available", f"The decision tests could not be read ({m['tests_why']}), so the verdicts and "
              "whether each test ran are not known.", ev)


def check_tests_unlinked(m, sci):
    ev = [(DT, "data.tests[].verdict"), (LEDGER, "decision_test.file"), (SCOPE, "declarations[].decision_test"),
          (CHOICE, "engine_departures[].decision_test"), (DOSSIER, "inputs[].decision_test")]
    if m["tests"] is None:
        return _no_tests(m, "tests_unlinked", ev)
    odd = sorted({str(t["verdict"]) for t in m["tests"].values() if t["verdict"] not in VERDICTS})
    if odd:
        return _r("tests_unlinked", "not_available", f"{DT} gives the verdict(s) {', '.join(odd)}, which the analysis "
                  "does not write, so which tests change the plan is not known.", ev)
    changing = [t for t, x in m["tests"].items() if x["verdict"] == CHANGES]
    loose = [r for r in sci if r["sources"][0] == "decision_tests"]
    if loose:
        return _r("tests_unlinked", "look", f"{len(loose)} of the {len(changing)} decision test(s) that change the plan "
                  f"are linked to no issue: {_ids([t for r in loose for t in r['tests']])}. Each is an open row, since "
                  "no finding, declaration or dossier entry says what it bears on.", ev,
                  {"columns": ["id", "test", "statement"], "rows": [[r["id"], ",".join(r["tests"]), r["statement"]] for r in loose]})
    return _r("tests_unlinked", "ok", f"Each of the {len(changing)} decision test(s) that change the plan is linked to "
              f"a row ({len(m['tests'])} tests in all).", ev)


def check_declared_untested(m, sci):
    ev = [(DT, "data.tests[].status"), (SIGNED, "decision_test keys"), (CHOICE, "engine_departures[].decision_test"),
          (SCOPE, "declarations[].decision_test")]
    if m["tests"] is None:
        return _no_tests(m, "declared_untested", ev)
    held = [r for r in sci if r["disposition"] in ("declared", "tested") and not r["noted"]]
    not_run = [(r, t) for r in held for t in r["tests"] if m["tests"].get(t, {}).get("status") != "run"]
    bare = [r for r in held if not r["tests"] and r["disposition"] == "declared"]
    old = [r for r in held if not r["tests"] and r["disposition"] == "tested"]
    parts = []
    if not_run:
        parts.append(f"{len(not_run)} declared or tested row(s) name a decision test that did not run: "
                     + ", ".join(f"{r['id']} ({t}: {m['tests'].get(t, {}).get('status') or 'not in the file'})" for r, t in not_run[:12]) + ".")
    if bare:
        parts.append(f"{len(bare)} declared row(s) name no decision test: {_ids(bare)}.")
    if old:
        parts.append(f"{len(old)} row(s) are recorded as tested with no test id (an older run's gate file): {_ids(old)}.")
    rows = [[r["id"], r["disposition"], t, m["tests"].get(t, {}).get("status")] for r, t in not_run] + \
           [[r["id"], r["disposition"], None, None] for r in bare + old]
    return _r("declared_untested", "problem" if not_run else "look" if parts else "ok",
              " ".join(parts) or f"Each of the {len(held)} declared or tested row(s) names a decision test that ran.", ev,
              {"columns": ["id", "disposition", "test", "test_status"], "rows": rows} if rows else None)


def check_decides_unknown(m, sci):
    ev = [(DT, "data.tests[].verdict"), (NOTES, "decides")]
    if m["tests"] is None:
        return _no_tests(m, "decides_unknown", ev)
    rows = [r for r in sci if r["disposition"] in ("open", "declared") and not r["tests"] and not r["decided"]]
    if rows:
        return _r("decides_unknown", "look", f"{len(rows)} open or declared row(s) have no decision test, so whether "
                  "they bear on the decision is unknown.", ev,
                  {"columns": ["id", "disposition", "source", "statement"],
                   "rows": [[r["id"], r["disposition"], ",".join(r["sources"]), r["statement"]] for r in _ordered(rows)]})
    return _r("decides_unknown", "ok", "Every open or declared row has a decision test, or your note says whether it "
              "bears on the decision.", ev)


def check_prose_only(m, sci):
    ev = [(SIGNED, "comment lines")]
    if m["prose"] is None:
        return _r("prose_only", "not_available", "There is no verified signed spec whose comment lines could be read. "
                  + m["spec"]["say"], ev)
    if m["prose"]:
        return _r("prose_only", "look", f"{len(m['prose'])} comment line(s) in the signed spec start a departure, "
                  "limitation or assumption; they are shown, not turned into rows, so nothing tracks their disposition.",
                  ev, {"columns": ["line", "text"], "rows": [list(x) for x in m["prose"]]})
    return _r("prose_only", "ok", "No comment line in the signed spec starts a departure, limitation or assumption.", ev)


def check_notes(m, sci):
    ev = [(NOTES, "each line")]
    if m["nothing"]:
        return _r("notes", "not_available", "The run has none of the files the register is derived from.", ev)
    if m["notes_why"]:
        return _r("notes", "not_available", f"The owner's notes could not be read ({m['notes_why']}), so none is applied.", ev)
    if m["bad_notes"]:
        return _r("notes", "problem", f"{len(m['bad_notes'])} line(s) of {NOTES} cannot be used and change nothing: "
                  + "; ".join(f"line {n}: {why}" for n, _, why in m["bad_notes"][:6]) + f". The other {len(m['notes'])} "
                  "note(s) are applied.", ev, {"columns": ["line", "text", "why"], "rows": [list(x) for x in m["bad_notes"]]})
    return _r("notes", "ok", f"All {len(m['notes'])} review note(s) in {NOTES} are applied." if m["notes"] else
              "The owner has written no review note for this run.", ev)


CHECK_FNS = (check_undisposed, check_tests_unlinked, check_declared_untested, check_decides_unknown, check_prose_only,
             check_notes)


def _ordered(rows):
    def key(r):
        sev = SEVERITIES.index(r["severity"]) if r["severity"] in SEVERITIES else 0 if r["severity"] else len(SEVERITIES)
        return (r["nature"] != "science", ORDER.get((r["disposition"], r["decides"]), LATER.get(r["disposition"], 3)),
                r["blocking"] is not True, sev)
    return sorted(rows, key=key)


def _cells(r):
    cell = {**r, "source": ",".join(r["sources"]), "tests": ",".join(r["tests"]), "verdicts": ",".join(r["verdicts"]),
            "files": ",".join(r["files"])}
    return [cell[c] for c in COLUMNS]


def _table(name, columns, rows, **more):
    return {"columns": columns, "rows": rows, "about": ABOUT[name], **more}


def result(m):
    """The view result {checks, facts, tables, charts, notes} for a model from register_rows.derive."""
    rows = _ordered(m["rows"])
    sci, proc = [r for r in rows if r["nature"] == "science"], [r for r in rows if r["nature"] != "science"]
    count = {w: sum(r["disposition"] == w for r in sci) for w in WORDS}
    facts = {"n_rows": len(sci), **{f"n_{w}": count[w] for w in WORDS},
             "n_decides_yes": sum(r["decides"] == "yes" for r in sci),
             "n_decides_unknown": sum(r["decides"] == "unknown" for r in sci), "n_owner_notes": len(m["notes"]),
             "n_process": len(proc), "n_process_open": sum(r["disposition"] == "open" for r in proc)}
    firsts = [r["sources"][0] for r in sci]
    tables = {
        "rows": _table("rows", COLUMNS, [_cells(r) for r in rows], labels=LABELS, lead=True, wrap=["statement"], actions=ACTIONS),
        "by_disposition": _table("by_disposition", ["disposition", "rows"], [[w, count[w]] for w in WORDS]),
        "by_source": _table("by_source", ["source", "rows", "open"], [
            [s, firsts.count(s), sum(r["sources"][0] == s and r["disposition"] == "open" for r in sci)]
            for s in SOURCES if s in firsts]),
        "prose_only": _table("prose_only", ["line", "text", "continues"],
                             [[n, text, m["prose_more"].get(n)] for n, text in m["prose"] or []], wrap=["text", "continues"]),
        "notes": _table("notes", ["at", "row", "disposition", "derived_disposition", "statement", "note"],
                        [[n[k] for k in ("at", "row", "disposition", "derived_disposition", "statement", "note")] for n in m["notes"]]),
        "process": _table("process", ["id", "disposition", "severity", "blocking", "first_visible_stage", "statement", "files"],
                          [[r["id"], r["disposition"], r["severity"], r["blocking"], r["first_visible_stage"], r["statement"],
                            ",".join(r["files"])] for r in proc], wrap=["statement"])}
    charts = [{"kind": "bars", "table": "by_disposition", "x": "disposition", "y": "rows", "title": "Science rows by disposition",
               "labels": {"rows": "science rows"},
               "how_to_read": "One bar per disposition; its length is the number of science rows. The open bar is what "
                              "still needs you; process rows are not counted."},
              {"kind": "bars", "table": "by_source", "x": "source", "y": "rows", "title": "Science rows by source",
               "labels": {"rows": "science rows"},
               "how_to_read": "One bar per source file kind; its length is the number of science rows first derived "
                              "from it (a merged row is counted once). Point at a bar for how many of them are open."}]
    notes = {"spec": m["spec"]["say"], "terms": TERMS}
    shared = {}
    for r in sci:
        if r["sources"][0] == "findings" and r["disposition"] == "open":
            shared.setdefault((r["first_visible_stage"], r["severity"]), []).append(r["id"])
    shared = {k: v for k, v in shared.items() if len(v) > 1}
    if shared:
        notes["same_subject"] = ("Open findings that share a target stage and a severity, as a hint that some may be one "
                                 "issue raised twice (they are not merged by rule; you merge two with Same as row): "
                                 + "; ".join(f"{len(v)} at {k[0]}, {k[1]} ({_ids(v, 8)})" for k, v in shared.items()) + ".")
    absent = [s for s in SOURCES if s not in firsts and s != "owner_note"]
    if absent and not m["nothing"]:
        notes["sources"] = ("No science row was first derived from: " + ", ".join(absent).replace("_", " ") + ". Either "
                            "the run does not have that file or list, or its entries were merged into an earlier row.")
    if m["notes"] or m["folded"]:
        notes["owner_notes"] = (f"{len(m['notes'])} review note(s) are applied. " + (
            "Folded by the owner: " + "; ".join(f"{b} into {a}" for a, b in m["folded"]) + "." if m["folded"] else "")).strip()
    return {"checks": [fn(m, sci) for fn in CHECK_FNS], "facts": {k: facts[k] for k in FACTS}, "tables": tables,
            "charts": charts, "notes": notes}


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import json
    from views.fit_bench import Bench, check, fixture_builder, need, statuses
    from views.register_rows import derive
    fx = fixture_builder()
    b = Bench(lambda run: result(derive(run)))
    hm = "history_matching"

    def write(rel, text):
        def edit(d):
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                f.write(text)
        return edit

    def order():
        v = b.look_at(b.folder(hm, "C12_open_high"))
        t = v["tables"]["rows"]
        ids = [r[0] for r in t["rows"]]
        need(ids[0] == "D-001" and ids[-1] == fx.STALE_ID, f"the open row is first and the process row last ({ids})")
        need(list(v["facts"])[0] == "n_open" and v["checks"][0]["id"] == "register.undisposed", "the count of open rows comes first")
        need(t.get("lead") is True and list(v["tables"])[0] == "rows" and list(v["tables"])[-1] == "process",
             "the rows table leads; the process rows are apart, last")
        need([r[0] for r in v["tables"]["process"]["rows"]] == [fx.STALE_ID], "the process table holds the process rows")

    def actions():
        import workbench
        v = b.look_at(b.folder(hm))
        a = v["tables"]["rows"]["actions"]
        need(workbench.action_problems(v) == [], f"the declaration uses the documented keys ({workbench.action_problems(v)})")
        need(a["post"] == "/api/note" and a["key"] == "id" and {f["name"] for f in a["fields"]} == {"disposition", "decides", "same_as", "note"}
             and {f["name"] for f in a["add"]["fields"]} >= {"kind", "statement"}, "a row takes a disposition, a note and a same-as; a row can be added")
        need(all("actions" not in t for n, t in v["tables"].items() if n != "rows"), "no other table has a control")

    def notes_check():
        clean = b.look_at(b.folder(hm))
        need(check(clean, "notes")["status"] == "ok" and "no review note" in check(clean, "notes")["says"], "no notes file: ok")
        line = json.dumps({"at": "2026-01-02T09:00:00+00:00", "row": "D-001", "kind": None, "statement": None,
                           "disposition": "withdrawn", "decides": None, "note": ""})
        v = b.look_at(b.edited(hm, write(NOTES, line + "\n" + line[:30] + "\n"), "C12_open_high"))
        c = check(v, "notes")
        need(c["status"] == "problem" and "line 2" in c["says"] and len(c["rows"]["rows"]) == 1 and c["rows"]["rows"][0][0] == 2,
             f"a cut-off line is a problem, shown with its line ({c['status']}: {c['says']})")
        need(statuses(v)["undisposed"] == "ok", "and moves no other check: the note that could be used still applies")

        def link(d):
            os.makedirs(os.path.join(d, "owner"))
            os.symlink(os.path.join(d, "metadata.json"), os.path.join(d, NOTES))
        c = check(b.look_at(b.edited(hm, link)), "notes")
        need(c["status"] == "not_available" and NOTES in c["says"], f"a notes file that cannot be read is not ok ({c['status']})")

    def fail_closed():
        def odd(found):
            return found.replace('"severity": "MEDIUM"', '"severity": "SEVERE"').replace('"status": "fixed"', '"status": "open"')

        def edit(d):
            with open(os.path.join(d, LEDGER), encoding="utf-8") as f:
                text = odd(f.read())
            with open(os.path.join(d, LEDGER), "w", encoding="utf-8") as f:
                f.write(text)
        c = check(b.look_at(b.edited(hm, edit)), "undisposed")
        need(c["status"] == "problem" and "D-002" in c["says"], f"an open row with a severity word the ledger does not write is not assumed minor ({c['says']})")

        def verdict(d):
            with open(os.path.join(d, DT), encoding="utf-8") as f:
                text = f.read().replace("verdict: NO_CHANGE", "verdict: PROBABLY", 1)
            with open(os.path.join(d, DT), "w", encoding="utf-8") as f:
                f.write(text)
        c = check(b.look_at(b.edited(hm, verdict)), "tests_unlinked")
        need(c["status"] == "not_available" and "PROBABLY" in c["says"], f"a verdict word the analysis does not write is named ({c['status']})")

    b.case("RC1 the reader's order: open count first, open rows at the top of a leading table, process rows apart and last", order)
    b.case("RC2 the rows table declares the review actions with documented keys only", actions)
    b.case("RC3 register.notes: ok with no file, problem on a line that cannot be used, not_available on an unreadable file", notes_check)
    b.case("RC4 fail closed: an unknown severity word on an open row, an unknown verdict word", fail_closed)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: register_checks.py --self-test")
