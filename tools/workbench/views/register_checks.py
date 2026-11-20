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
                                 SIGNED, SOURCES, UNVERIFIED, VERDICTS, WORDS)

CHECKS = {"undisposed": "C12", "tests_unlinked": None, "declared_untested": None, "decides_unknown": None,
          "prose_only": None, "notes": None}
TITLES = {"undisposed": "Science rows with no disposition",
          "tests_unlinked": "Decision tests that change the plan and are linked to no issue",
          "declared_untested": "Declared rows whose decision test did not run, or that name none",
          "decides_unknown": "Rows with no decision test: whether they bear on the decision is unknown",
          "prose_only": "Departures, limitations and assumptions written only as comments in the signed spec",
          "notes": "The owner's review notes that could not be used"}
FACTS = ["n_open", "n_rows", "n_declared", "n_tested", "n_implemented", "n_implemented_unverified", "n_withdrawn",
         "n_decides_yes", "n_decides_unknown", "n_owner_notes", "n_process", "n_process_open"]
FEW_ROWS = 12       # workbench threshold: below it the charts and the table by disposition only repeat the facts
TERMS = ("A row is one science issue, uncertainty or assumption, derived from the run's files each time this page is "
         "read. Nature: a science row is a critic's finding or anything from another source (a declaration, a "
         "departure, an excluded input, a dossier entry, a decision test); a process row is a finding a harness check "
         "raised about the run's files (a stale or missing file), marked process beside its id, counted apart and "
         "listed last. Disposition: open (nothing answers it), declared (a signed spec, a scope declaration or the "
         "owner accepts it as a stated limit), tested (a decision test was run on it), implemented (the ledger records "
         "it as fixed and the evidence column shows the gate's recorded result that verifies it), "
         f"{UNVERIFIED} (the ledger records it as fixed and no gate record verifies it: it counts as a row with no "
         "disposition), withdrawn (the owner's note withdrew it). Decides: yes when a linked decision test that ran "
         f"has the verdict {CHANGES} (the recommended plan changes), no when every linked test ran with NO_CHANGE, "
         "else unknown. First visible stage: the stage of the run at which the row could first have been seen, in "
         "the stage machine's names (SIGN for what the signed spec, its sheet and the dossier hold, ENGINE_CHOICE, "
         "ANALYZE for decision tests, GATE for a scope declaration; for a finding, the stage the ledger says must "
         "act on it). Blocking: the gate held the finding open, so the run could not be accepted while it stood; a "
         "row from another source has no such mark. Severity is the critic's or the sheet's own (HIGH is the highest). "
         "Signed by: who signed the spec that carries a declared row, or owner where the owner answered.")
ABOUT = {
    "rows": ("One row per science issue, uncertainty or assumption, in reading order: rows that are blocking or of "
             "the highest severity first, then rows with a decision test that changes the plan, then the rest; within "
             "each, rows with no disposition before declared and tested ones, and by severity. Rows that are "
             "implemented or withdrawn come after, and process rows last. id: a reviewer's finding keeps its own id; any "
             "other row's id starts with where the row comes from: departure: an engine departure the signed spec "
             "declares, calibration: a calibration target declared unmatched, excluded: an input the signed spec "
             "leaves out of the draws, presign: a row the sign-off sheet marks HIGH, nodata: an evidence entry with no "
             "data, test: a decision test that changes the plan and is linked to no other row, note: a row you added. "
             "statement is in the run's own words; "
             "source and files say where it came from; evidence is what verifies an implemented row; tests and "
             "verdicts are the decision tests linked to it by a structured key (each test's question is in the table "
             "Tests below). Needs you: a row that is open, or implemented but not verified, has no disposition. Review "
             "sets one, adds a note or folds a row into another; that is written to owner/review_notes.jsonl and never "
             "changes a file of the run."),
    "tests": ("One row per decision test in analysis/decision_tests.yaml: the question the signed spec asks "
              "(analysis.decision_tests[].text), the test's status and verdict as the analysis wrote them, and the rows "
              "of the register that name it. A test ran only with status run and a verdict of NO_CHANGE or CHANGES_PLAN."),
    "by_disposition": "Science rows per disposition (the facts' counts), for the chart.",
    "by_source": ("Science rows per source, each row counted once under the first source it was derived from, and how "
                  "many of them are open. A source this run does not have is absent."),
    "prose_only": (f"Comment lines of the signed spec ({SIGNED}) that start a departure, limitation or assumption (line "
                   "is the line's number in that file; continues holds the comment lines that follow it, up to four). "
                   "They are shown, not turned into rows; to make one a row, add it with Add a row above the rows table."),
    "notes": ("The owner's review notes in the order written, each beside the disposition the files alone give "
              "(derived disposition; empty for a row the owner added)."),
    "process": ("Findings a harness check raised about the run's files (a stale or missing file, a broken contract), not "
                "about the science. They are counted apart from the science rows and move no check on this page; the "
                "Steps view's checks read the same files directly.")}
LABELS = {"decides": "bears on the decision", "tests": "linked decision tests", "verdicts": "one per test",
          "blocking": "the gate held it open", "nature": "science or process", "signed_by": "who signed or answered",
          "first_visible_stage": "when it could first be seen", "evidence": "what verifies it"}
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
WAITING = ("open", UNVERIFIED)          # the dispositions that are none: the row still needs one
CLOSED = ("implemented", "withdrawn")
BY_DISPOSITION = {"open": 0, UNVERIFIED: 1, None: 2, "declared": 3, "tested": 4, "implemented": 5, "withdrawn": 6}


def _r(name, status, says, evidence, rows=None):
    return checks.result(f"register.{name}", "register", TITLES[name], status, says, evidence, rows, CHECKS[name])


def _ids(rows, cap=12):
    ids = [str(r["id"]) if isinstance(r, dict) else str(r) for r in rows]
    return ", ".join(ids[:cap]) + (f" and {len(ids) - cap} more" if len(ids) > cap else "")


def _top(r):
    """Blocking, or of the highest severity; a severity word the ledger does not write is not assumed to be lower."""
    return r["blocking"] is True or (r["severity"] is not None and r["severity"] not in SEVERITIES[1:])


def check_undisposed(m, sci):
    ev = [(LEDGER, "status, severity, blocking"), (SCOPE, "declarations"), (CHOICE, "engine_departures[].status"),
          (SIGNED, "calibration.joint.unmatched.declared, uncertainty.excluded"), (SHEET, "lines marked HIGH"),
          (DOSSIER, "inputs[].no_data"), ("gate_r*.json, final_gate.json", "done_when_status"), (NOTES, "disposition")]
    if m["nothing"]:
        return _r("undisposed", "not_available", "The run has none of the files the register is derived from (no signed "
                  "spec, findings ledger, scope declaration, engine choice, sign-off sheet, dossier or decision tests).", ev)
    waiting = _ordered([r for r in sci if r["disposition"] in WAITING])
    top = [r for r in waiting if _top(r)]
    unverified = [r for r in waiting if r["disposition"] == UNVERIFIED]
    flags = list(dict.fromkeys(m["flags"]))
    status = ("problem" if top or any(s == "problem" for s, _ in flags) else
              "not_available" if flags else "look" if waiting else "ok")
    if not waiting:
        says = f"All {len(sci)} science rows have a disposition." if sci else "The register holds no science row."
    else:
        says = (f"{len(waiting)} of {len(sci)} science rows have no disposition: {_ids(waiting, 6)}. " + (
            f"{len(top)} of them {'is' if len(top) == 1 else 'are'} blocking or of the highest severity "
            f"({SEVERITIES[0]}): {_ids(top)}." if top else "None is blocking or of the highest severity.") + (
            f" The count is larger than the fact n_open ({len(waiting) - len(unverified)}) by {len(unverified)} row(s) the "
            f"ledger records as fixed with no gate result that verifies it ({_ids(unverified, 6)}): they read {UNVERIFIED} "
            "and count here." if unverified else "") + " Needs you: each of these rows needs a disposition (a fix "
            "with its evidence, a decision test, a declaration, or your note).")
    if flags:
        says += " Also: " + " ".join(why for _, why in flags[:4]) + (f" ({len(flags) - 4} more of this kind.)" if len(flags) > 4 else "")
    cols = ["id", "disposition", "severity", "blocking", "decides", "first_visible_stage", "source", "statement", "evidence"]
    rows = [[r["id"], r["disposition"], r["severity"], r["blocking"], r["decides"], r["first_visible_stage"],
             ",".join(r["sources"]), r["statement"], r["evidence"]] for r in waiting]
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
    clash = [t for t, x in m["tests"].items() if x["state"] == "contradiction"]
    if clash:       # §3: a record that contradicts itself is a problem; which tests change the plan is then not known
        return _r("tests_unlinked", "problem", f"{len(clash)} decision test(s) in {DT} have a status and a verdict that "
                  f"contradict each other ({_pairs(m, clash)}); the analysis writes NOT_RUN for a test that did not run and "
                  "no other verdict, so which tests change the plan is not known.", ev,
                  {"columns": ["test", "status", "verdict"], "rows": [[t, m["tests"][t]["status"], m["tests"][t]["verdict"]] for t in clash]})
    changing = [t for t, x in m["tests"].items() if x["state"] == "ran" and x["verdict"] == CHANGES]
    loose = [r for r in sci if r["sources"][0] == "decision_tests"]
    if loose:
        return _r("tests_unlinked", "look", f"{len(loose)} of the {len(changing)} decision test(s) that change the plan "
                  f"are linked to no issue: {_ids([t for r in loose for t in r['tests']])}. Each is an open row, since "
                  "no finding, declaration or dossier entry says what it bears on.", ev,
                  {"columns": ["id", "test", "statement"], "rows": [[r["id"], ",".join(r["tests"]), r["statement"]] for r in loose]})
    return _r("tests_unlinked", "ok", f"Each of the {len(changing)} decision test(s) that change the plan is linked to "
              f"a row ({len(m['tests'])} tests in all).", ev)


def _how(m, tid):
    """Why a linked test does not count as run, in the file's own words."""
    t = m["tests"].get(tid)
    if t is None:
        return f"{tid}: not in the file"
    if t["state"] == "contradiction":
        return f"{tid}: status {t['status']} and verdict {t['verdict']} contradict each other"
    if t["state"] == "ran":
        return f"{tid}: {t['spec_says']}"
    return f"{tid}: status {t['status']!s}" + ("" if t["state"] == "not_run" else f", verdict {t['verdict']!s}, a pair the analysis does not write")


def _pairs(m, tids, cap=8):
    return ", ".join(f"{t}: status {m['tests'][t]['status']}, verdict {m['tests'][t]['verdict']}" for t in tids[:cap]) + (
        f" and {len(tids) - cap} more" if len(tids) > cap else "")


def check_declared_untested(m, sci):
    ev = [(DT, "data.tests[].status"), (SIGNED, "decision_test keys"), (CHOICE, "engine_departures[].decision_test"),
          (SCOPE, "declarations[].decision_test")]
    if m["tests"] is None:
        return _no_tests(m, "declared_untested", ev)
    held = [r for r in sci if r["disposition"] in ("declared", "tested") and not r["noted"]]
    not_run = [(r, t) for r in held for t in r["tests"] if m["tests"].get(t, {}).get("counts") is not True]
    bare = [r for r in held if not r["tests"] and r["disposition"] == "declared"]
    old = [r for r in held if not r["tests"] and r["disposition"] == "tested"]
    parts = []
    if not_run:
        parts.append(f"{len(not_run)} declared or tested row(s) name a decision test that did not run for the signed spec: "
                     + ", ".join(f"{r['id']} ({_how(m, t)})" for r, t in not_run[:12]) + ".")
    if bare:
        parts.append(f"{len(bare)} declared row(s) name no decision test: {_ids(bare)}.")
    if old:
        parts.append(f"{len(old)} row(s) are recorded as tested with no test id (an older run's gate file): {_ids(old)}.")
    rows = [[r["id"], r["disposition"], t, m["tests"].get(t, {}).get("status"), m["tests"].get(t, {}).get("verdict")] for r, t in not_run] + \
           [[r["id"], r["disposition"], None, None, None] for r in bare + old]
    return _r("declared_untested", "problem" if not_run else "look" if parts else "ok",
              " ".join(parts) or f"Each of the {len(held)} declared or tested row(s) names a decision test that ran.", ev,
              {"columns": ["id", "disposition", "test", "test_status", "test_verdict"], "rows": rows} if rows else None)


def check_decides_unknown(m, sci):
    ev = [(DT, "data.tests[].verdict"), (NOTES, "decides")]
    if m["tests"] is None:
        return _no_tests(m, "decides_unknown", ev)
    rows = _ordered([r for r in sci if r["disposition"] in ("open", "declared") and not r["tests"] and not r["decided"]])
    fact = sum(r["decides"] == "unknown" for r in sci)
    if rows:
        return _r("decides_unknown", "look", f"{len(rows)} open or declared row(s) have no decision test, so whether "
                  f"they bear on the decision is unknown: {_ids(rows, 6)}." + (
                      f" The fact n_decides_unknown is {fact}, not {len(rows)}: it counts every science row whose linked "
                      "tests settle nothing, whatever its disposition (a row whose test did not run, or a row that is "
                      "tested, implemented or withdrawn with no test), where this check counts the open and declared "
                      "rows that name no test at all." if fact != len(rows) else ""), ev,
                  {"columns": ["id", "disposition", "source", "statement"],
                   "rows": [[r["id"], r["disposition"], ",".join(r["sources"]), r["statement"]] for r in rows]})
    return _r("decides_unknown", "ok", "Every open or declared row has a decision test, or your note says whether it "
              "bears on the decision.", ev)


def check_prose_only(m, sci):
    ev = [(SIGNED, "comment lines")]
    if m["prose"] is None:
        return _r("prose_only", "not_available", "There is no verified signed spec whose comment lines could be read. "
                  + m["spec"]["say"], ev)
    if m["prose"]:
        return _r("prose_only", "look", f"{len(m['prose'])} comment line(s) in the signed spec start a departure, "
                  f"limitation or assumption (line {_ids([n for n, _ in m['prose']], 8)}); they are shown, not turned into "
                  "rows, so nothing tracks their disposition.",
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
    """Reading order (§12, reader finding 1): rows that are blocking or of the highest severity, then rows whose
    decision test changes the plan, then the rest; within each by disposition (those with none first) and then by
    severity. A row that is implemented or withdrawn is closed and comes after; process rows are last."""
    def key(r):
        sev = SEVERITIES.index(r["severity"]) if r["severity"] in SEVERITIES else 0 if r["severity"] else len(SEVERITIES)
        tier = 3 if r["disposition"] in CLOSED else 0 if _top(r) else 1 if r["decides"] == "yes" else 2
        return r["nature"] != "science", tier, BY_DISPOSITION.get(r["disposition"], 2), sev, r["blocking"] is not True
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
    count = {w: sum(r["disposition"] == w for r in sci) for w in WORDS + (UNVERIFIED,)}
    few = len(sci) < FEW_ROWS
    linked = {}
    for r in rows:
        for t in r["tests"]:
            linked.setdefault(t, []).append(r["id"])
    facts = {"n_rows": len(sci), **{f"n_{w}": count[w] for w in WORDS}, "n_implemented_unverified": count[UNVERIFIED],
             "n_decides_yes": sum(r["decides"] == "yes" for r in sci),
             "n_decides_unknown": sum(r["decides"] == "unknown" for r in sci), "n_owner_notes": len(m["notes"]),
             "n_process": len(proc), "n_process_open": sum(r["disposition"] == "open" for r in proc)}
    firsts = [r["sources"][0] for r in sci]
    tables = {
        "rows": _table("rows", COLUMNS, [_cells(r) for r in rows], labels=LABELS, lead=True, wrap=["statement", "evidence"], actions=ACTIONS),
        "tests": _table("tests", ["test", "question", "status", "verdict", "rows"], [
            [t, m["asks"].get(t) or None, x["status"], x["verdict"], ",".join(linked.get(t, [])) or None]
            for t, x in (m["tests"] or {}).items()], wrap=["question"]),
        # a table that only repeats the facts is left out when there are few rows (§12, reader finding 5): null, as a
        # table that is not recorded is
        "by_disposition": None if few else _table("by_disposition", ["disposition", "rows"], [[w, count[w]] for w in WORDS + (UNVERIFIED,)]),
        "by_source": _table("by_source", ["source", "rows", "open"], [
            [s, firsts.count(s), sum(r["sources"][0] == s and r["disposition"] == "open" for r in sci)]
            for s in SOURCES if s in firsts]),
        "prose_only": _table("prose_only", ["line", "text", "continues"],
                             [[n, text, m["prose_more"].get(n)] for n, text in m["prose"] or []], wrap=["text", "continues"]),
        "notes": _table("notes", ["at", "row", "disposition", "derived_disposition", "statement", "note"],
                        [[n[k] for k in ("at", "row", "disposition", "derived_disposition", "statement", "note")] for n in m["notes"]]),
        "process": _table("process", ["id", "disposition", "evidence", "severity", "blocking", "first_visible_stage", "statement", "files"],
                          [[r["id"], r["disposition"], r["evidence"], r["severity"], r["blocking"], r["first_visible_stage"],
                            r["statement"], ",".join(r["files"])] for r in proc], wrap=["statement", "evidence"])}
    charts = [] if few else [{"kind": "bars", "table": "by_disposition", "x": "disposition", "y": "rows", "title": "Science rows by disposition",
               "labels": {"rows": "science rows"},
               "how_to_read": "One bar per disposition; its length is the number of science rows. The open bar is what "
                              "still needs you; process rows are not counted."},
              {"kind": "bars", "table": "by_source", "x": "source", "y": "rows", "title": "Science rows by source",
               "labels": {"rows": "science rows"},
               "how_to_read": "One bar per source file kind; its length is the number of science rows first derived "
                              "from it (a merged row is counted once). Point at a bar for how many of them are open."}]
    notes = {"spec": m["spec"]["say"], "terms": TERMS}
    if few and not m["nothing"]:
        notes["summaries"] = (f"With fewer than {FEW_ROWS} science rows (workbench threshold) the charts and the table by "
                              "disposition are left out: they would only repeat the facts above.")
    shared = {}
    for r in sci:
        if r["sources"][0] == "findings" and r["disposition"] == "open":
            shared.setdefault((r["first_visible_stage"], r["severity"]), []).append(r["id"])
    shared = {k: v for k, v in shared.items() if len(v) > 1}
    if shared:
        notes["same_subject"] = ("Open findings that share a target stage and a severity, as a hint that some may be one "
                                 "issue raised twice (they are not merged by rule; you merge two with Same as row): "
                                 + "; ".join(f"{len(v)} at {k[0]}, {k[1]} ({_ids(v, 8)})" for k, v in shared.items()) + ".")
    named = [s for r in sci for s in r["sources"]]
    merged = [s for s in SOURCES if s in named and s not in firsts]
    absent = [s for s in SOURCES if s not in named and s != "owner_note"]
    if (merged or absent) and not m["nothing"]:
        notes["sources"] = " ".join(x for x in (
            ("Every entry of these sources was merged into a row an earlier source gave, so the source column of those "
             "rows lists it and by_source does not: " + ", ".join(f"{s.replace('_', ' ')} ({named.count(s)} row(s))" for s in merged) + ".")
            if merged else "",
            ("This run has no row from: " + ", ".join(absent).replace("_", " ") + " (the file or list is absent or empty, "
             "or holds nothing of the kind the register reads).") if absent else "") if x)
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
        need(all("actions" not in t for n, t in v["tables"].items() if n != "rows" and t is not None), "no other table has a control")

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

    def contradiction():
        from views.fit_bench import rewrite

        def pair(tid, **words):
            return rewrite(DT, lambda doc: next(t for t in doc["data"]["tests"] if t["test_id"] == tid).update(words))
        for words in ({"verdict": "NOT_RUN"}, {"status": "not_run"}):
            v = b.look_at(b.edited(hm, pair("DT4", **words), "declared"))
            c = check(v, "declared_untested")
            need(c["status"] == "problem" and "departure:B2" in c["says"] and "DT4" in c["says"] and "contradict" in c["says"],
                 f"{words}: a declared row whose test's status and verdict contradict each other is a problem ({c['status']}: {c['says']})")
        v = b.look_at(b.edited(hm, pair("DT1", status="not_run", verdict="CHANGES_PLAN")))
        c = check(v, "tests_unlinked")
        need(c["status"] == "problem" and "DT1" in c["says"] and "test:DT1" not in [r[0] for r in v["tables"]["rows"]["rows"]],
             f"a test no row names, recorded as not run and as changing the plan, is a problem and no row ({c['status']}: {c['says']})")
        v = b.look_at(b.edited(hm, pair("DT4", status="done"), "declared"))
        need(check(v, "declared_untested")["status"] == "problem", "a status word the producer does not write is not a test that ran")
        v = b.look_at(b.edited(hm, rewrite(DT, lambda doc: doc.update(spec_sha256="1" * 64)), "declared"))
        c = check(v, "declared_untested")
        need(c["status"] == "problem" and "another spec" in c["says"], f"a test recorded for another spec than the signed one covers no declared row ({c['status']}: {c['says']})")

    def rows_of(v, name="rows"):
        t = v["tables"][name]
        return [dict(zip(t["columns"], r)) for r in t["rows"]]

    def row_of(v, rid, name="rows"):
        return next(r for r in rows_of(v, name) if r["id"] == rid)

    def reading_order():
        def r(rid, disposition, severity=None, blocking=None, decides="unknown", nature="science"):
            return {"id": rid, "disposition": disposition, "severity": severity, "blocking": blocking, "decides": decides, "nature": nature}
        mixed = [r("low", "open", "LOW"), r("done", "implemented", "HIGH"), r("proc", "open", "HIGH", True, nature="process"),
                 r("med", "open", "MEDIUM"), r("yes-declared", "declared", "LOW", decides="yes"), r("high-declared", "declared", "HIGH"),
                 r("yes", "open", decides="yes"), r("unverified", UNVERIFIED, "MEDIUM"), r("block", "open", "MEDIUM", True),
                 r("high", "open", "HIGH", False)]
        got = [x["id"] for x in _ordered(mixed)]
        want = ["high", "block", "high-declared", "yes", "yes-declared", "med", "low", "unverified", "done", "proc"]
        need(got == want, f"blocking and highest-severity rows, then rows whose test changes the plan, then the rest; within a "
                          f"disposition by severity; closed rows and process rows last (got {got})")

    def implemented():
        v = b.look_at(b.folder(hm))
        d1 = row_of(v, "D-001")
        need(d1["disposition"] == "implemented" and "gate_r2.json" in d1["evidence"] and "passed" in d1["evidence"],
             f"a fixed finding is implemented with the gate's recorded result for its done-when test shown ({d1['disposition']}: {d1['evidence']!r})")
        need((v["facts"]["n_implemented"], v["facts"]["n_implemented_unverified"]) == (3, 0), f"facts ({v['facts']})")
        proc = row_of(v, fx.STALE_ID, "process")
        need(proc["disposition"] == "implemented" and "gate" in proc["evidence"], f"a harness finding's evidence is the gate it was absent from ({proc})")
        v = b.look_at(b.folder(hm, "implemented_unverified"))
        d2, c = row_of(v, "D-002"), check(v, "undisposed")
        need(d2["disposition"] == UNVERIFIED == "implemented, not verified" and "gate_r2.json" in d2["evidence"],
             f"the ledger says fixed and the gate's last result for its test is not a pass ({d2['disposition']}: {d2['evidence']!r})")
        need(c["status"] == "look" and "D-002" in c["says"] and "not verified" in c["says"] and "D-002" in str(c["rows"]),
             f"it counts as a row with no disposition ({c['status']}: {c['says']})")
        need((v["facts"]["n_implemented"], v["facts"]["n_implemented_unverified"], v["facts"]["n_open"]) == (2, 1, 0), f"facts ({v['facts']})")
        v = b.look_at(b.folder(hm, "implemented_no_gate"))
        c = check(v, "undisposed")
        need(c["status"] == "problem" and "D-001" in c["says"] and row_of(v, "D-001")["disposition"] == UNVERIFIED
             and "no gate record" in row_of(v, "D-001")["evidence"],
             f"with no gate record nothing verifies a fix: a HIGH finding recorded as fixed is a problem ({c['status']}: {c['says']})")
        noted = json.dumps({"at": "2026-01-02T09:00:00+00:00", "row": "D-002", "kind": None, "statement": None,
                            "disposition": "implemented", "decides": None, "note": "checked in the report"})
        v = b.look_at(b.edited(hm, write(NOTES, noted + "\n"), "implemented_unverified"))
        need(row_of(v, "D-002")["disposition"] == "implemented" and "owner" in row_of(v, "D-002")["evidence"]
             and check(v, "undisposed")["status"] == "ok", "the owner's note that it is implemented is the owner's word, shown as such")

    def sentences():
        v = b.look_at(b.folder(hm, "open_no_test"))
        c, u = check(v, "undisposed"), check(v, "decides_unknown")
        need("D-002" in c["says"], f"undisposed names the rows it counts, whatever their severity ({c['says']})")
        need("D-002" in u["says"] and "n_decides_unknown" in u["says"] and str(v["facts"]["n_decides_unknown"]) in u["says"],
             f"decides_unknown names its rows and says why its count is not the fact of a similar name ({u['says']})")
        v = b.look_at(b.folder(hm, "implemented_unverified"))
        need("n_open" in check(v, "undisposed")["says"], "undisposed says why its count differs from n_open")
        need("n_open" not in check(b.look_at(b.folder(hm, "C12_open_high")), "undisposed")["says"], "and says nothing of it when the two agree")

    def defined():
        v = b.look_at(b.folder(hm, "process_open"))
        terms = v["notes"]["terms"]
        need(all(w in terms for w in ("First visible stage", "Blocking", "process", "science")), f"the page defines its columns' words ({terms})")
        cols = v["tables"]["rows"]["columns"]
        need(cols.index("nature") == 1, f"nature is beside the id, where a process row is seen as one without scrolling ({cols})")
        tests = rows_of(b.look_at(b.folder(hm, "declared")), "tests")       # §9: R-001 is declared with DT3 on this variant only
        dt3 = next(t for t in tests if t["test"] == "DT3")
        need(dt3["question"] == "Does the plan change under DT3?" and dt3["verdict"] == "NO_CHANGE" and dt3["rows"] == "R-001",
             f"each decision test is shown with the spec's own question, its verdict and the rows that name it ({dt3})")
        need(len(tests) == len(derive(__import__("workbench").Run(b.folder(hm, "declared")))["tests"]), "every test of the file is listed")

    def sources_and_summaries():
        v = b.look_at(b.folder(hm, "declared"))       # §9: only this variant has a scope declaration to merge
        said = v["notes"].get("sources", "")
        need("scope declaration" in said and "merged" in said and "No science row" not in said.split("merged")[0].split(".")[-1],
             f"a source whose rows were merged into earlier rows is said to be merged, not absent ({said!r})")
        need("decision tests" in said and "owner" not in said, f"a source with no row at all is named as such ({said!r})")
        need(v["charts"] == [] and v["tables"]["by_disposition"] is None and v["tables"]["by_source"] is not None,
             f"with few rows the charts and the table that only repeats the facts are left out ({len(v['charts'])} charts)")

        def many(d):
            with open(os.path.join(d, LEDGER), "a", encoding="utf-8") as f:
                for i in range(FEW_ROWS):
                    f.write(json.dumps({"id": f"R-9{i:02d}", "status": "open", "source": "critique-redteam", "severity": "LOW",
                                        "claim": f"finding {i}", "target_stage": "WRITE"}) + "\n")
        v = b.look_at(b.edited(hm, many))
        need(len(v["charts"]) == 2 and v["tables"]["by_disposition"] is not None, "with more rows they are shown")

    def stale_agrees():
        import workbench
        for defect, want in ((None, ("implemented", "ok")), ("process_open", ("open", "problem"))):
            d = b.folder(hm, defect)
            fresh = checks.inputs_fresh(workbench.Run(d))
            got = row_of(b.look_at(d), fx.STALE_ID, "process")
            need((got["disposition"], fresh["status"]) == want and (defect is None or "analysis/point.yaml" in str(fresh["rows"])),
                 f"{defect}: the harness finding about a stale input and the Steps check on the same run agree "
                 f"(register {got['disposition']}, steps.inputs_fresh {fresh['status']})")
        need(fx.STALE_ID == "analysis_contract_stale:analysis/point.yaml:analysis/calibration.yaml",
             "the finding has the id the producer gives it: kind, artifact, input (toolkit/analysis/contract.py)")

    def bad_bytes():
        line = json.dumps({"at": "2026-01-02T09:00:00+00:00", "row": "D-001", "kind": None, "statement": None,
                           "disposition": "withdrawn", "decides": None, "note": "x"}).encode().replace(b'"x"', b'"\xff"')

        def edit(d):
            os.makedirs(os.path.join(d, "owner"))
            with open(os.path.join(d, NOTES), "wb") as f:
                f.write(line + b"\n")
        v = b.look_at(b.edited(hm, edit, "C12_open_high"))
        c = check(v, "notes")
        need(c["status"] == "problem" and "line 1" in c["says"] and "UTF-8" in c["says"] and row_of(v, "D-001")["disposition"] == "open",
             f"a withdrawal with a byte that is not UTF-8 withdraws nothing and is a problem ({c['status']}: {c['says']})")

    b.case("RC6 reading order: blocking and highest severity, then plan-changing tests, then the rest; by severity within a disposition (reader 1)", reading_order)
    b.case("RC7 implemented needs the gate's recorded result on the row; without it: implemented, not verified, counted as undisposed (reader 2)", implemented)
    b.case("RC8 a check's sentence names the rows it counts and says why its count differs from a fact of a similar name (reader 3)", sentences)
    b.case("RC9 the page defines science, process, first visible stage and blocking; each decision test has its question (reader 4)", defined)
    b.case("RC10 the sources note tells merged from absent; few rows: no chart or table that only repeats the facts (reader 5)", sources_and_summaries)
    b.case("RC11 the fixture's harness finding about a stale input agrees with the Steps check on the same run (reader 6)", stale_agrees)
    b.case("RC12 a note line that is not valid UTF-8 is a problem and applies nothing (review 4)", bad_bytes)
    b.case("RC5 a decision test's status and verdict are read together: a pair the producer does not write is a problem (review 3)", contradiction)
    b.case("RC1 the reader's order: open count first, open rows at the top of a leading table, process rows apart and last", order)
    b.case("RC2 the rows table declares the review actions with documented keys only", actions)
    b.case("RC3 register.notes: ok with no file, problem on a line that cannot be used, not_available on an unreadable file", notes_check)
    b.case("RC4 fail closed: an unknown severity word on an open row, an unknown verdict word", fail_closed)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: register_checks.py --self-test")
