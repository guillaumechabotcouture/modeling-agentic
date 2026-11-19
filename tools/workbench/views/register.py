#!/usr/bin/env python3
"""The Science register view (docs/design/review-workbench.md §5.8 and §6). The rows are derived in
views/register_rows.py and the checks, facts and tables computed in views/register_checks.py; the self-test below is
the contract both meet (written before the code, loop step 3 of §10, goal G3).

    python3 tools/workbench/views/register.py --self-test

`view(run)` (run is a workbench.Run) returns {checks, facts, tables, charts, notes} and never raises. It reads run
files only; the one file it reads that the owner writes is owner/review_notes.jsonl (workbench.py's POST /api/note
appends to it). Fixtures and planted edits: fixtures.py (DEFECTS entries whose `view` is "register").

What the cases fix, from §5.8 and §6:

- facts: n_rows, n_open, n_declared, n_tested, n_implemented, n_implemented_unverified, n_withdrawn, n_decides_yes,
  n_decides_unknown, n_owner_notes, n_process, n_process_open. All but the last two count science rows only.
- tables.rows: id, nature, kind, statement, source, first_visible_stage, decides, disposition, evidence, tests,
  verdicts, severity, blocking, files, signed_by. tables.by_source: source, rows, open. tables.tests: test, question,
  status, verdict, rows.
- §12's reader findings (2026-11-19): reading order; `implemented` only with the gate's recorded result shown
  (`evidence`), else "implemented, not verified", counted with the open rows by register.undisposed; sentences name
  the rows they count; the terms are defined; few rows: no chart and no table by disposition.
- checks, in this order: register.undisposed (C12), register.tests_unlinked, register.declared_untested,
  register.decides_unknown, register.prose_only, register.notes (§6: a note line that cannot be used).
- rows are derived in the order of §6's table; a later source that shares a row's id, one of its decision test ids, or
  its input name is merged into that row.

What the cases assume where §5.8 and §6 are silent or can be read two ways (each is reported to the lead; a case
that rests on one says so in its name with the word "assumed"):

 A1  Disposition words: open, declared, tested, implemented, withdrawn (the words of the n_* facts).
 A2  A merged row's `source` and `files` list every source and file, joined by a comma in derivation order, like
     `tests`; `verdicts` gives one word per test in the same order. by_source counts a row once, under its first source.
 A3  by_source, and the tables the charts read, count science rows, as the facts do; every check reads science rows only.
 A4  The chart of rows by disposition reads a derived table `by_disposition` (disposition, rows), one row per word.
 A5  `first_visible_stage` of a finding is the ledger's `owner_state` (the stage machine's name for its
     `target_stage`), or `target_stage` as written when the ledger did not know the stage.
 A6  `nature` of a merged row is `science` when any of its sources is a critic's finding or a non-finding source.
 A7  `signed_by` is the sign-off record's `signed_by` on rows the signed spec carries (calibration_declared,
     uncertainty_excluded, signoff_sheet, and engine departures whose status is signed), `owner` where the owner
     answered (a finding with status owner_accepted, a row an owner's note disposed of), else null. A lead's
     signature leaves the disposition as it is: the column and a note say who signed.
 A8  The input-name merge holds between rows of the sources whose ids carry a prefix (departure, calibration,
     excluded, presign, nodata): one id ends with a colon and the other's name after its prefix.
 A9  A finding the ledger holds as `open` takes the disposition an older run's declared_findings.yaml (declared) or
     final_gate.json resolved_by_test (tested) gives it; a finding the owner declined stays open whatever names it.
 A10 A decision test is linked to a row only through a structured key (a finding's decision_test.file, a
     declaration's or departure's or dossier entry's decision_test); a test id inside prose or inside another test's
     name links nothing.
 A11 With no verified signed spec (§3), what the spec and its sign-off sheet carry is never declared or tested, and
     register.undisposed is `problem` and says so; register.prose_only is then `not_available`.
 A12 register.declared_untested is `problem` when a declared or tested row names a test whose status is not `run`,
     `look` when a declared row names no test, and `not_available` when analysis/decision_tests.yaml cannot be read
     (as are tests_unlinked and decides_unknown, which need its verdicts).
 A13 register.undisposed is `problem` on a ledger line that is not a finding row, on a scope declaration that names
     no finding of the ledger (two files disagree; with no ledger at all it names none either); `not_available` on
     a status word the ledger does not write and on a source file that is there but cannot be parsed; `not_available`
     when the run has none of the register's files. A review note that cannot be used is register.notes' (§6).
 A14 register.prose_only counts comment lines of the verified signed copy in which DEPARTURE, LIMITATION or
     ASSUMPTION (or its plural) in capitals, with or without words in brackets after it, is followed by a colon,
     at the start of the line or inside it; its `rows` give the line number and the text.
 A15 Review notes: a note with `row` set changes that row's disposition and decides (signed_by owner), and
     tables.notes (at, row, disposition, derived_disposition, statement, note: §6's columns) keeps the derived
     value beside it; a note with `row` null adds a row with source owner_note; a note {row, same_as} folds the two rows
     into one; n_owner_notes counts the notes that could be used.
 A16 A row from the sign-off sheet has severity HIGH, the sheet's own mark. Two forms of line are read, as
     toolkit/spec/engine_parameters.py writes them: "the uncertainty range of <name> ..." and "<name> = <value> ...".
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)


def view(run):
    from views import register_checks, register_rows
    return register_checks.result(register_rows.derive(run))


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import hashlib
    import json

    import yaml

    import checks as _checks
    from views.fit_bench import Bench, check, fixture_builder, need, rewrite, says, statuses
    fx = fixture_builder()
    b = Bench(lambda run: view(run))        # the module's `view` as it is when a case runs
    folder, look_at, case, edited = b.folder, b.look_at, b.case, b.edited
    hm, post = "history_matching", "posterior"
    names = {"undisposed": "C12", "tests_unlinked": None, "declared_untested": None, "decides_unknown": None,
             "prose_only": None, "notes": None}
    columns = ["id", "nature", "kind", "statement", "source", "first_visible_stage", "decides", "disposition", "evidence",
               "tests", "verdicts", "severity", "blocking", "files", "signed_by"]
    fact_names = ["n_rows", "n_open", "n_declared", "n_tested", "n_implemented", "n_withdrawn", "n_decides_yes",
                  "n_decides_unknown", "n_owner_notes", "n_process", "n_process_open", "n_implemented_unverified"]
    words = ["open", "declared", "tested", "implemented", "withdrawn"]
    unverified = "implemented, not verified"        # §12: the ledger's fixed with no gate result that verifies it
    sources = ["findings", "scope_declaration", "engine_departures", "calibration_declared", "uncertainty_excluded",
               "signoff_sheet", "evidence_dossier", "decision_tests", "owner_note"]
    ledger_file, scope_file, choice_file, sheet_file, dossier_file, dt_file = (
        fx.LEDGER, fx.SCOPE, fx.CHOICE, fx.SHEET, fx.DOSSIER, fx.DT)
    spec_file, signed_file, signoff_file, notes_file = (
        "decision_spec.yaml", "decision_brief.signed/decision_spec.yaml", "decision_spec.signoff.json",
        "owner/review_notes.jsonl")
    stale = fx.STALE_ID
    clean_ids = {"D-001", "D-002", "R-001", stale, "departure:B2", "departure:B", "calibration:grp_r1:G1", "excluded:c1",
                 "nodata:parameter:p2", "nodata:unit_attribute:x1"}

    # ---- readers of a view result
    def table(v, name, cols=None):
        t = v["tables"].get(name)
        need(isinstance(t, dict) and isinstance(t.get("columns"), list) and isinstance(t.get("rows"), list),
             f"tables.{name} is {{columns, rows, about}} (got {type(t).__name__})")
        need(cols is None or t["columns"] == cols, f"tables.{name} has columns {cols} (got {t['columns']})")
        need(all(isinstance(r, list) and len(r) == len(t["columns"]) for r in t["rows"]), f"tables.{name} rows are lists in column order")
        return [dict(zip(t["columns"], r)) for r in t["rows"]]

    def rows(v):
        return table(v, "rows", columns)

    def row(v, rid):
        found = [r for r in rows(v) if r["id"] == rid]
        need(len(found) == 1, f"one row with id {rid!r} (found {len(found)}; ids: {sorted(str(r['id']) for r in rows(v))})")
        return found[0]

    def ids(v):
        return [r["id"] for r in rows(v)]

    def split(cell):
        need(isinstance(cell, str), f"a joined cell is a string (got {cell!r})")
        return [x for x in cell.split(",") if x]

    def has(r, **want):
        bad = {k: (r.get(k), w) for k, w in want.items() if r.get(k) != w}
        need(not bad, f"row {r.get('id')}: " + "; ".join(f"{k} is {g!r}, expected {w!r}" for k, (g, w) in bad.items()))

    def moved(v, clean):
        return {k: s for k, s in statuses(v).items() if clean.get(k) != s}

    def unraised(c):
        return c["status"] in ("problem", "not_available")

    # ---- edits of a copy of a built run
    def both(*edits):
        return lambda d: [e(d) for e in edits]

    def ledger(change):
        def edit(d):
            with open(os.path.join(d, ledger_file), encoding="utf-8") as f:
                found = [json.loads(x) for x in f if x.strip()]
            change(found)
            with open(os.path.join(d, ledger_file), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in found))
        return edit

    def finding(found, fid):
        return next(r for r in found if r["id"] == fid)

    def rejson(rel, change):
        def edit(d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                doc = json.load(f)
            change(doc)
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=1)
        return edit

    def retext(rel, change):
        def edit(d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                text = f.read()
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                f.write(change(text))
        return edit

    def write(rel, text):
        def edit(d):
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                f.write(text)
        return edit

    def remove(rel):
        return lambda d: os.remove(os.path.join(d, rel))

    def respec(change):
        """The spec changed in both copies, and the sign-off record's hash with it (a re-signed amendment)."""
        def edit(d):
            with open(os.path.join(d, signed_file), encoding="utf-8") as f:
                spec = yaml.safe_load(f)
            change(spec)
            text = yaml.safe_dump(spec, sort_keys=False)
            for rel in (spec_file, signed_file):
                with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            rejson(signoff_file, lambda so: so["sha256"].update({spec_file: hashlib.sha256(text.encode()).hexdigest()}))(d)
        return edit

    def notes(*lines):
        return write(notes_file, "".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))

    def note(rid, **what):
        return {"at": "2026-01-02T09:00:00+00:00", "row": rid, "kind": None, "statement": None, "disposition": None,
                "decides": None, "note": "", **what}

    def tree(d):
        out = {}
        for root, _, files in os.walk(d):
            for n in files:
                with open(os.path.join(root, n), "rb") as f:
                    out[os.path.relpath(os.path.join(root, n), d)] = hashlib.sha256(f.read()).hexdigest()
        return out

    # ------------------------------------------------------------------------------- the generic model (§5.8)
    def m_checks():
        for method in (hm, post):
            v = look_at(folder(method))
            need([c.get("id") for c in v["checks"]] == [f"register.{n}" for n in names],
                 f"{method}: the six checks, in order (got {[c.get('id') for c in v['checks']]})")
            need(statuses(v) == {n: "ok" for n in names}, f"{method}: every check ok on the clean run (got {statuses(v)})")
            for c in v["checks"]:
                name = c["id"].split(".")[-1]
                need(c.get("step") == "register" and c.get("title") and c.get("status") in _checks.STATUSES, f"{name}: view, title, status")
                need(isinstance(c.get("says"), str) and c["says"].strip() and "\n" not in c["says"], f"{name}: one plain sentence")
                ev = c.get("evidence")
                need(isinstance(ev, list) and ev and all(isinstance(e, dict) and e.get("file") and "field" in e for e in ev),
                     f"{name}: names the file and field read ({ev})")
                need(c.get("case") == names[name], f"{name}: scorecard case {names[name]} (got {c.get('case')})")

    def m_facts():
        want = {hm: [9, 0, 4, 3, 2, 0, 1, 2, 0, 1, 0, 0], post: [8, 0, 4, 2, 2, 0, 1, 2, 0, 1, 0, 0]}
        for method in (hm, post):
            f = look_at(folder(method))["facts"]
            need(sorted(f) == sorted(fact_names), f"{method}: exactly the twelve facts (got {sorted(f)})")
            need([f[k] for k in fact_names] == want[method], f"{method}: {dict(zip(fact_names, want[method]))} (got {f})")

    def m_rows():
        for method, want in ((hm, clean_ids), (post, clean_ids - {"calibration:grp_r1:G1"})):
            v = look_at(folder(method))
            got = ids(v)
            need(len(set(got)) == len(got) and set(got) == want, f"{method}: one row per id, {sorted(want)} (got {sorted(got)})")
            need(isinstance(v["tables"]["rows"].get("about"), str) and v["tables"]["rows"]["about"], "tables.rows says what its rows are")
            for r in rows(v):
                need(r["kind"] in ("issue", "uncertainty", "assumption") and r["decides"] in ("yes", "no", "unknown")
                     and r["disposition"] in words + [unverified] and r["nature"] in ("science", "process"), f"row {r['id']}: known words ({r})")
                need(isinstance(r["statement"], str) and r["statement"].strip() and r["source"] and r["files"]
                     and split(r["source"])[0] in sources, f"row {r['id']}: a statement, a source of §6's list and its file ({r})")
                need(len(split(r["tests"])) == len(split(r["verdicts"])), f"row {r['id']}: one verdict per test ({r['tests']!r}, {r['verdicts']!r})")
                need(r["blocking"] in (True, False, None), f"row {r['id']}: blocking is true, false or null")

    def m_by_source():
        want = {hm: [["findings", 3, 0], ["engine_departures", 2, 0], ["calibration_declared", 1, 0],
                     ["uncertainty_excluded", 1, 0], ["evidence_dossier", 2, 0]]}
        want[post] = [r for r in want[hm] if r[0] != "calibration_declared"]
        for method in (hm, post):
            v = look_at(folder(method))
            got = [[r["source"], r["rows"], r["open"]] for r in table(v, "by_source", ["source", "rows", "open"])]
            need(got == want[method], f"{method}: science rows by first source, in §6's order; a source the run does not "
                                      f"have is absent (got {got})")
            need(sum(r[1] for r in got) == v["facts"]["n_rows"] and sum(r[2] for r in got) == v["facts"]["n_open"],
                 f"{method}: the counts add up to n_rows and n_open")
            need(isinstance(v["tables"]["by_source"].get("about"), str), "tables.by_source says what its rows are")

    def m_charts():
        import workbench
        need(look_at(folder(hm))["charts"] == [] and look_at(folder(hm))["tables"]["by_disposition"] is None,
             "with few rows the charts and the table by disposition are left out: they would only repeat the facts (§12)")

        def more(found):        # enough science rows for the summaries to say something the facts do not
            found += [{**finding(found, "D-002"), "id": f"R-8{i:02d}", "source": "critique-redteam"} for i in range(12)]
        v = look_at(edited(hm, ledger(more)))
        need(workbench.chart_problems(v) == [], f"every chart names a table and columns the result holds ({workbench.chart_problems(v)})")
        need(len(v["charts"]) == 2 and all(c.get("kind") == "bars" for c in v["charts"]), f"two bars charts (got {v['charts']})")
        by_x = {c.get("x"): c for c in v["charts"]}
        need(set(by_x) == {"source", "disposition"}, f"rows by source and by disposition (x columns: {sorted(map(str, by_x))})")
        need(by_x["source"]["table"] == "by_source" and by_x["source"]["y"] == "rows", f"by source: {by_x['source']}")
        need(by_x["disposition"]["table"] == "by_disposition" and by_x["disposition"]["y"] == "rows", f"by disposition: {by_x['disposition']}")
        got = [[r["disposition"], r["rows"]] for r in table(v, "by_disposition", ["disposition", "rows"])]
        need(got == [[w, v["facts"][f"n_{w}"]] for w in words] + [[unverified, v["facts"]["n_implemented_unverified"]]],
             f"by_disposition has one row per word with the facts' counts (got {got})")

    def m_notes():
        v = look_at(folder(hm))
        need(v["notes"] and all(isinstance(k, str) and isinstance(t, str) and t.strip() for k, t in v["notes"].items()), "notes is {name: text}")
        text = " ".join(v["notes"].values())
        need(signed_file in text and "owner" in text, f"a note says which copy of the spec was read and who signed it ({text!r})")
        need("scope_declaration" not in [r["source"] for r in table(v, "by_source")], "a source with no row of its own is not in by_source")

    # ------------------------------------------------------------------------------- derivation rules (§6)
    def d_status_words():
        want = {"open": "open", "declared": "declared", "fixed": "implemented", "owner_accepted": "declared", "owner_declined": "open"}
        claim = None
        for word, disposition in want.items():
            def to(found, word=word):
                finding(found, "D-002")["status"] = word
            r = row(look_at(edited(hm, ledger(to))), "D-002")
            has(r, disposition=disposition, kind="issue", severity="MEDIUM", blocking=False, nature="science")
            need(split(r["source"])[0] == "findings" and ledger_file in r["files"], f"{word}: from the ledger ({r})")
            claim = claim or r["statement"]
        need("point value of the headline" in claim, f"the statement is the finding's claim ({claim!r})")

    def d_scope():
        v = look_at(folder(hm))
        r = row(v, "R-001")
        has(r, disposition="declared", tests="DT3", verdicts="NO_CHANGE", source="findings,scope_declaration", decides="no")
        need(split(r["files"]) == [ledger_file, scope_file], f"both files are named ({r['files']!r})")
        need("SD-001" not in ids(v), "the declaration is merged into the finding it names, not added")

        def no_blocker(doc):
            doc["declarations"].append({"id": "SD-009", "why_unresolved": "no finding id was given",
                                        "decision_test": "models/decision_tests/DT1.yaml"})
        r = row(look_at(edited(hm, rewrite(scope_file, no_blocker))), "SD-009")
        has(r, kind="issue", disposition="declared", tests="DT1", source="scope_declaration", nature="science")
        need("no finding id was given" in r["statement"], f"the statement is why_unresolved ({r['statement']!r})")

    def d_departures():
        v = look_at(folder(hm))
        for rid, test in (("departure:B2", "DT4"), ("departure:B", "DT5")):
            has(row(v, rid), kind="assumption", disposition="declared", tests=test, source="engine_departures", decides="no")

        def proposed(doc):
            doc["engine_departures"][1]["status"] = "proposed"
        v = look_at(edited(hm, rejson(choice_file, proposed)))
        has(row(v, "departure:B"), disposition="open", signed_by=None)
        has(row(v, "departure:B2"), disposition="declared")
        need(check(v, "undisposed")["status"] == "look", f"an unsigned departure is an open row ({check(v, 'undisposed')['says']!r})")

    def d_calibration():
        has(row(look_at(folder(hm)), "calibration:grp_r1:G1"), kind="issue", disposition="tested", tests="DT6", verdicts="NO_CHANGE",
            source="calibration_declared", decides="no")
        v = look_at(folder(hm, "C13_blocked_declared"))
        has(row(v, f"calibration:{fx.OFFSET}"), disposition="tested", tests="DT2", source="calibration_declared")
        need(not [i for i in ids(look_at(folder(post))) if i.startswith("calibration:")], "a method with no such list has no such row")

    def d_excluded():
        v = look_at(folder(hm))
        has(row(v, "excluded:c1"), kind="assumption", disposition="declared")
        need(split(row(v, "excluded:c1")["source"])[0] == "uncertainty_excluded", "from the signed spec's uncertainty.excluded")

        def by_kind(spec):
            spec["uncertainty"]["excluded"].append({"kind": "costs", "reason": "there are no unit costs"})
        v = look_at(edited(hm, respec(by_kind)))
        has(row(v, "excluded:costs"), kind="assumption", disposition="declared", tests="", decides="unknown")
        c = check(v, "declared_untested")
        need(c["status"] == "look" and "excluded:costs" in c["says"], f"a declared row with no test at all is look, and named ({c['status']}: {c['says']!r})")

    def d_sheet():
        v = look_at(folder(hm))
        need(not [i for i in ids(v) if i.startswith("presign:")] and check(v, "undisposed")["status"] == "ok",
             "the clean sheet marks no finding HIGH, though the word is on two of its lines: no row, and nothing raised")
        # toolkit/spec/signoff.py sign() records no hash for the sheet, so a line it marks HIGH is not declared by signing
        v = look_at(folder(hm, "sheet_high"))
        has(row(v, "presign:p2"), kind="issue", disposition="open", severity="HIGH", signed_by=None, first_visible_stage="SIGN")
        need(len([i for i in ids(v) if i.startswith("presign:")]) == 1,
             "one row, though the word HIGH is on three lines of the sheet: only a line that marks a finding HIGH counts")
        need([[r["rows"], r["open"]] for r in table(v, "by_source") if r["source"] == "signoff_sheet"] == [[1, 1]], "counted open under its source")

        def second(text):
            return text.replace("\n## Evidence dossier", "- **HIGH**: q9 = 7 is outside the card range of `ref` (1 to 5; "
                                "outside_card_range: true). Signing accepts it; its source and reason are above.\n\n## Evidence dossier")
        v = look_at(edited(hm, retext(sheet_file, second)))
        has(row(v, "presign:q9"), disposition="open", source="signoff_sheet", severity="HIGH", signed_by=None)

    def d_dossier():
        v = look_at(folder(hm))
        has(row(v, "nodata:unit_attribute:x1"), kind="uncertainty", disposition="tested", tests="DT9", source="evidence_dossier")
        need("nodata:parameter:p1" not in ids(v), "an entry with observations is no row, though it names a decision test")

        def prose(doc):
            doc["inputs"]["parameter:q5"] = {"model_value": 0.0, "model_source": "default", "chosen": {"value": 0.0, "rule": "hold"},
                                             "no_data": {"reason": "no source gives q5", "borrowing_rule": "hold at 0, tested by DT1"}}
        r = row(look_at(edited(hm, rewrite(dossier_file, prose))), "nodata:parameter:q5")
        has(r, kind="uncertainty", disposition="open", tests="", decides="unknown")
        need("no source gives q5" in r["statement"], f"the statement is the entry's reason ({r['statement']!r})")

    def d_tests():
        v = look_at(folder(hm))
        need(not [i for i in ids(v) if i.startswith("test:")],
             "no row for a plan-changing test a row names (DT9), nor for tests that did not change the plan or did not run")
        r = row(look_at(folder(hm, "test_unlinked")), "test:DT1")
        has(r, kind="uncertainty", source="decision_tests", disposition="open", decides="yes", tests="DT1", verdicts="CHANGES_PLAN",
            first_visible_stage="ANALYZE")
        need("changes the plan and is linked to no issue" in r["statement"], f"the row carries the sentence ({r['statement']!r})")

    def d_decides():
        v = look_at(folder(hm))
        has(row(v, "nodata:unit_attribute:x1"), decides="yes", verdicts="CHANGES_PLAN")
        has(row(v, "R-001"), decides="no")
        has(row(v, "D-001"), decides="unknown", tests="")
        has(row(look_at(folder(hm, "declared_test_not_run")), "departure:B2"), decides="unknown", tests="DT4", verdicts="NOT_RUN")

        def odd(doc):
            next(t for t in doc["data"]["tests"] if t["test_id"] == "DT3")["verdict"] = "MAYBE"
        has(row(look_at(edited(hm, rewrite(dt_file, odd))), "R-001"), decides="unknown")
        mixed = look_at(edited(hm, notes(note("nodata:unit_attribute:x1", same_as="R-001"))))
        kept = [r for r in rows(mixed) if r["id"] in ("R-001", "nodata:unit_attribute:x1")]
        need(len(kept) == 1 and kept[0]["decides"] == "yes", f"one plan-changing test among a row's tests makes it yes ({kept})")

    def d_stage():
        v = look_at(folder(hm))
        want = {"D-001": "SPEC", "D-002": "WRITE", "R-001": "ANALYZE", "departure:B": "ENGINE_CHOICE",
                "calibration:grp_r1:G1": "SIGN", "excluded:c1": "SIGN", "nodata:parameter:p2": "SIGN", "nodata:unit_attribute:x1": "SIGN"}
        got = {i: row(v, i)["first_visible_stage"] for i in want}
        need(got == want, f"the stage machine's names (got {got})")
        v = look_at(folder(hm, "scope_names_no_finding"))
        has(row(v, "X-404"), first_visible_stage="GATE")

        def unknown_stage(found):
            finding(found, "D-002").update(target_stage="LAYOUT", owner_state="LAYOUT", owner_state_known=False)
        has(row(look_at(edited(hm, ledger(unknown_stage))), "D-002"), first_visible_stage="LAYOUT")

    def d_nature():
        v = look_at(folder(hm))
        has(row(v, stale), nature="process", disposition="implemented")
        need({r["id"] for r in rows(v) if r["nature"] == "process"} == {stale}, "a critic's finding and every non-finding source are science")
        v = look_at(folder(hm, "process_open"))
        has(row(v, stale), nature="process", disposition="open", severity="HIGH", blocking=True)
        f = v["facts"]
        need((f["n_process"], f["n_process_open"], f["n_open"], f["n_rows"]) == (1, 1, 0, 9), f"process rows are counted apart ({f})")
        need(next(r for r in table(v, "by_source") if r["source"] == "findings")["open"] == 0, "by_source counts science rows")
        need(statuses(v) == {n: "ok" for n in names}, f"an open process row moves no check (got {statuses(v)})")

    def d_signed_by():
        v = look_at(folder(hm))
        for rid in ("calibration:grp_r1:G1", "excluded:c1", "departure:B2", "departure:B"):
            has(row(v, rid), signed_by="owner")
        has(row(v, "D-001"), signed_by=None)
        v = look_at(folder(hm, "signed_by_lead"))
        for rid, disposition in (("calibration:grp_r1:G1", "tested"), ("excluded:c1", "declared")):
            has(row(v, rid), signed_by="lead", disposition=disposition)
        has(row(v, "departure:B2"), disposition="open", signed_by=None)
        need("lead" in " ".join(v["notes"].values()), f"a note says the spec was signed by lead, not the owner ({v['notes']})")

        def accepted(found):
            finding(found, "R-001")["status"] = "owner_accepted"
        has(row(look_at(edited(hm, ledger(accepted))), "R-001"), disposition="declared", signed_by="owner")

    def d_merge_test():
        clean, v = look_at(folder(hm)), look_at(folder(hm, "merge_shared_test"))
        need(sorted(ids(v)) == sorted(ids(clean)), f"a later source naming a test already on a row adds no row ({sorted(set(ids(v)) ^ set(ids(clean)))})")
        has(row(v, "R-001"), disposition="declared", tests="DT3", source="findings,scope_declaration,evidence_dossier")
        need(dossier_file in row(v, "R-001")["files"], "the later source's file is added")

    def d_merge_input():
        clean = look_at(folder(hm))
        need("nodata:parameter:c1" not in ids(clean), "the dossier's entry for c1 is merged")
        has(row(clean, "excluded:c1"), tests="DT7", source="uncertainty_excluded,evidence_dossier", disposition="declared", decides="no")
        has(row(clean, "nodata:parameter:p2"), tests="DT8", source="evidence_dossier", disposition="tested")
        high = look_at(folder(hm, "sheet_high"))
        need("nodata:parameter:p2" not in ids(high), "with a sheet row for p2, the dossier's entry for it is merged into that row")
        has(row(high, "presign:p2"), tests="DT8", source="signoff_sheet,evidence_dossier", disposition="open")
        need({"departure:B", "departure:B2"} <= set(ids(clean)), "an id that only begins like another is not merged")
        need("scenario_excluded_c1" not in row(clean, "excluded:c1")["tests"], "a test whose name holds the input's name is not linked by it")
        v = look_at(folder(hm, "merge_excluded_nodata"))
        need(sorted(ids(v)) == sorted(ids(clean) + ["excluded:q4"]), f"one row for the pair ({sorted(set(ids(v)) ^ set(ids(clean)))})")
        has(row(v, "excluded:q4"), tests="DT1", disposition="declared", source="uncertainty_excluded,evidence_dossier", decides="no")

    def d_no_mechanical_merge():
        def two(found):
            finding(found, "D-002")["status"] = "open"
            found.append({**finding(found, "D-002"), "id": "R-009", "source": "critique-redteam",
                          "claim": "The headline is given without its interval."})
        v = look_at(edited(hm, ledger(two)))
        need({"D-002", "R-009"} <= set(ids(v)), "two critics' findings on one subject stay two rows")
        hint = " ".join(v["notes"].values())
        need("2" in hint and "WRITE" in hint and "MEDIUM" in hint,
             f"a note gives how many open findings share a stage and a severity, as a hint for the owner ({v['notes']})")

    def d_older():
        v = look_at(folder(hm, "older_layout"))
        has(row(v, "R-001"), disposition="declared", tests="")
        has(row(v, "D-002"), disposition="tested")
        need(fx.OLD_DECLARED in row(v, "R-001")["files"] and fx.OLD_GATE in row(v, "D-002")["files"], "the older files are named")
        need(v["facts"]["n_open"] == 0 and check(v, "undisposed")["status"] == "ok", f"no row is left open ({v['facts']})")

        def declined(found):
            finding(found, "R-001")["status"] = "owner_declined"
        v = look_at(edited(hm, ledger(declined), "older_layout"))
        has(row(v, "R-001"), disposition="open")
        v = look_at(edited(hm, ledger(declined)))
        has(row(v, "R-001"), disposition="open")

    def d_signed_copy():
        v = look_at(folder(hm, "declared_only_on_disk"))
        need("excluded:q7" not in ids(v), "a declaration only in the spec on disk is no row")
        need(statuses(v) == {n: "ok" for n in names}, f"and moves nothing (got {statuses(v)})")
        text = " ".join(v["notes"].values())
        need("differ" in text and signed_file in text, f"the notes say the signed copy was read and that the spec on disk differs ({text!r})")

    def d_prose():
        v = look_at(folder(hm, "prose_only"))
        c = check(v, "prose_only")
        need(c["status"] == "look" and "3" in c["says"], f"three comment lines start one; the fourth only mentions one ({c['status']}: {c['says']!r})")
        shown = " ".join(str(x) for r in (c.get("rows") or {}).get("rows", []) for x in r)
        need(all(p.lstrip("# ") in shown for p in fx.PROSE[:3]) and fx.PROSE[3].lstrip("# ") not in shown and len(c["rows"]["rows"]) == 3,
             f"the check's rows give the text of each ({shown!r})")
        need(sorted(ids(v)) == sorted(ids(look_at(folder(hm)))), "they are shown, not turned into rows")
        d = edited(hm, retext(spec_file, lambda text: fx.PROSE[0] + "\n" + text))
        c = check(look_at(d), "prose_only")
        need(c["status"] == "ok", f"a comment line only in the spec on disk is not counted ({c['status']}: {c['says']!r})")

    # ------------------------------------------------------------------------------- planted edits (§9)
    def extra(defect, v):
        c = check(v, fx.DEFECTS[defect]["check"])
        if defect == "C12_open_high":
            has(row(v, "D-001"), disposition="open", severity="HIGH", blocking=True)
            need("D-001" in c["says"] and v["facts"]["n_open"] == 1, f"names the finding ({c['says']!r})")
            need("D-001" in str((c.get("rows") or {}).get("rows")), "and lists it in the rows behind the check")
        if defect == "C12_declared_tested":
            has(row(v, "D-001"), disposition="declared", tests="DT1", verdicts="NO_CHANGE", decides="no", severity="HIGH", blocking=True)
        if defect == "sheet_high":
            has(row(v, "presign:p2"), disposition="open", severity="HIGH", signed_by=None)
            need("presign:p2" in c["says"] and f"does not bind {sheet_file}" in c["says"],
                 f"names the row and says the sign-off record does not bind the sheet ({c['says']!r})")
        if defect == "test_unlinked":
            need("DT1" in c["says"] and "D-001" not in check(v, "undisposed")["says"], f"names the test ({c['says']!r})")
        if defect == "declared_test_not_run":
            need("departure:B2" in c["says"] and "DT4" in c["says"], f"names the row and its test ({c['says']!r})")
        if defect == "declared_no_test":
            need("departure:B" in c["says"].replace("departure:B2", ""), f"names the row ({c['says']!r})")
            has(row(v, "departure:B"), disposition="declared", tests="", decides="unknown")
        if defect == "open_no_test":
            has(row(v, "D-002"), disposition="open", decides="unknown", tests="")
            need("1" in c["says"], f"counts the row ({c['says']!r})")
        if defect == "findings_bad_line":
            need(ledger_file in c["says"] and "5" in c["says"], f"names the file and the line ({c['says']!r})")
            need(sorted(ids(v)) == sorted(ids(look_at(folder(hm)))), "the rows that could be read are still shown")
        if defect == "finding_unknown_status":
            need("resolved" in c["says"] and "D-002" in c["says"], f"names the word and the finding ({c['says']!r})")
            has(row(v, "D-002"), disposition=None)
        if defect == "scope_names_no_finding":
            need("X-404" in c["says"] and ledger_file in c["says"], f"names the id and the ledger ({c['says']!r})")
            has(row(v, "X-404"), disposition="declared", tests="DT1", source="scope_declaration")
        if defect == "decision_tests_missing":
            need(all(dt_file in check(v, n)["says"] for n in ("declared_untested", "tests_unlinked", "decides_unknown")), "each names the missing file")
            need(all(r["decides"] == "unknown" and not {"NO_CHANGE", "CHANGES_PLAN"} & set(split(r["verdicts"])) for r in rows(v)),
                 "no verdict is guessed at: every row's decides is unknown")

    def planted(method, defect):
        def fn():
            spec = fx.DEFECTS[defect]
            need(spec.get("view") == "register" and spec["check"] in names, "the fixture records this edit for a register check")
            clean, v = statuses(look_at(folder(method))), look_at(folder(method, defect))
            want = {**({spec["check"]: spec["status"]} if clean[spec["check"]] != spec["status"] else {}), **spec.get("also", {})}
            need(moved(v, clean) == want, f"only {want or 'nothing'} moves (moved: {moved(v, clean)})")
            for name in want:
                need(check(v, name)["says"].strip(), f"{name} says why")
            if method == hm:
                extra(defect, v)
        return fn

    # ------------------------------------------------------------------------------- fail closed (§3, §9)
    def x_nothing():
        v = look_at(folder(hm, upto="launch"))
        need(statuses(v) == {n: "not_available" for n in names}, f"a run with none of the files: every check not_available (got {statuses(v)})")
        need(v["facts"]["n_rows"] == 0 and rows(v) == [], "and no row")

    def x_before_analysis():
        v = look_at(folder(hm, upto="sign-off"))
        want = {"undisposed": "ok", "prose_only": "ok", "notes": "ok", "tests_unlinked": "not_available",
                "declared_untested": "not_available", "decides_unknown": "not_available"}
        need(statuses(v) == want, f"before the decision tests exist: {want} (got {statuses(v)})")
        need({"excluded:c1", "nodata:parameter:p2", "departure:B"} <= set(ids(v)) and "R-001" not in ids(v), "rows from the files the run has so far")

    def x_unverified():
        ways = {"the sign-off record is missing": remove(signoff_file), "the signed copy is missing": remove(signed_file),
                "the signed copy is not the one signed": retext(signed_file, lambda text: text + "\n# edited after the sign-off\n")}
        for why, edit in ways.items():
            v = look_at(edited(hm, edit))
            c = check(v, "undisposed")
            need(c["status"] == "problem" and "no signed spec could be verified" in says(c), f"{why}: undisposed is a problem and says so ({c['status']}: {c['says']!r})")
            carried = [r for r in rows(v) if split(r["source"])[0] in ("calibration_declared", "uncertainty_excluded", "signoff_sheet")]
            need(all(r["disposition"] not in ("declared", "tested") and r["signed_by"] is None for r in carried),
                 f"{why}: nothing the spec or its sheet carries is declared or tested ({[(r['id'], r['disposition']) for r in carried]})")
            need(check(v, "prose_only")["status"] == "not_available", f"{why}: prose_only has no signed copy to read")

    def x_unparsable():
        for rel in (scope_file, choice_file, dossier_file):
            for text in ("\x00 [ not: a document", "- a\n- list\n" if rel.endswith(".yaml") else "[1, 2]"):
                c = check(look_at(edited(hm, write(rel, text))), "undisposed")
                need(unraised(c) and rel in c["says"], f"{rel} unreadable: undisposed is not ok or look, and names the file ({c['status']}: {c['says']!r})")
        for text in ("data: [unclosed\n", "schema: analysis.decision_tests/1\ndata: 7\n"):
            st = statuses(look_at(edited(hm, write(dt_file, text))))
            need([st[n] for n in ("tests_unlinked", "declared_untested", "decides_unknown")] == ["not_available"] * 3,
                 f"unreadable decision tests: the three checks that need them are not_available (got {st})")

    def x_ledger():
        for text in ("not json\n{also not\n", '[1, 2]\n{"id": 7, "status": "open"}\n{"status": "open"}\n', "7\n"):
            v = look_at(edited(hm, write(ledger_file, text)))
            c = check(v, "undisposed")
            need(c["status"] == "problem" and ledger_file in c["says"], f"a ledger whose lines are not finding rows is a problem ({c['status']}: {c['says']!r})")
            need("D-001" not in ids(v) and set(statuses(v).values()) <= set(_checks.STATUSES), "and nothing is guessed at")
        v = look_at(edited(hm, remove(ledger_file)))
        c = check(v, "undisposed")
        need("findings" not in [r["source"] for r in table(v, "by_source")], "a run with no ledger has no findings source")
        need(c["status"] == "problem" and "R-001" in c["says"] and ledger_file in c["says"],
             f"and its scope declaration then names a finding no ledger holds (§6: a declaration of nothing) ({c['status']}: {c['says']!r})")

    # ------------------------------------------------------------------------------- review notes (§6), reading only
    def n_disposition():
        d = edited(hm, notes(note("D-001", disposition="withdrawn", decides="no", note="superseded by the amended comparator")), "C12_open_high")
        v = look_at(d)
        has(row(v, "D-001"), disposition="withdrawn", decides="no", signed_by="owner")
        f = v["facts"]
        need((f["n_withdrawn"], f["n_open"], f["n_owner_notes"]) == (1, 0, 1), f"the facts follow the note ({f})")
        need(statuses(v) == {n: "ok" for n in names}, f"a row the owner disposed of is no longer undisposed (got {statuses(v)})")
        shown = table(v, "notes", ["at", "row", "disposition", "derived_disposition", "statement", "note"])
        need(len(shown) == 1 and shown[0]["row"] == "D-001" and shown[0]["disposition"] == "withdrawn"
             and shown[0]["derived_disposition"] == "open" and "superseded" in shown[0]["note"],
             f"the owner's note and the derived value are side by side ({shown})")
        need(notes_file in row(v, "D-001")["files"], "the row names the notes file")

    def n_new_row():
        new = note(None, kind="uncertainty", statement="The two rounds may not use the same sampling frame.", disposition="open", decides="unknown")
        clean, v = look_at(folder(hm)), look_at(edited(hm, notes(new)))
        added = [r for r in rows(v) if r["id"] not in ids(clean)]
        need(len(added) == 1, f"one row is added ({[r['id'] for r in added]})")
        has(added[0], kind="uncertainty", source="owner_note", disposition="open", decides="unknown", nature="science")
        need("same sampling frame" in added[0]["statement"], "with the note's statement")
        need(["owner_note", 1, 1] in [[r["source"], r["rows"], r["open"]] for r in table(v, "by_source")], "by_source gains owner_note")
        need(v["facts"]["n_owner_notes"] == 1 and v["facts"]["n_rows"] == clean["facts"]["n_rows"] + 1, f"facts ({v['facts']})")
        need(check(v, "undisposed")["status"] == "look" and check(v, "decides_unknown")["status"] == "look", "an open row the owner added is undisposed like any other")

    def n_same_as():
        clean = look_at(folder(hm))
        v = look_at(edited(hm, notes(note("nodata:unit_attribute:x1", same_as="R-001", note="the same question"))))
        kept = [r for r in rows(v) if r["id"] in ("R-001", "nodata:unit_attribute:x1")]
        need(len(kept) == 1 and v["facts"]["n_rows"] == clean["facts"]["n_rows"] - 1, f"the two rows become one ({[r['id'] for r in kept]})")
        need(set(split(kept[0]["tests"])) == {"DT3", "DT9"} and {"findings", "evidence_dossier"} <= set(split(kept[0]["source"])),
             f"which holds the tests and sources of both ({kept[0]})")
        need(v["facts"]["n_owner_notes"] == 1, "and the note is counted")

    def n_malformed():
        good = note("D-001", disposition="withdrawn", note="kept")
        for bad, line in (('{"at": "2026-01-02", "row": "D-001", "dispo', 2), (note("Z-999", disposition="withdrawn"), 2),
                          (note("D-002", disposition="closed"), 2), ("[1, 2]", 2)):
            v = look_at(edited(hm, notes(good, bad), "C12_open_high"))
            flagged = [c for c in v["checks"] if c["status"] == "problem" and notes_file in c["says"] and str(line) in c["says"]]
            need(flagged, f"a note line that cannot be used is shown as a problem, with the file and line ({bad!r}: "
                          f"{[(c['id'], c['status'], c['says']) for c in v['checks']][:1]})")
            has(row(v, "D-001"), disposition="withdrawn")
            need(v["facts"]["n_owner_notes"] == 1, f"the note that could be used still counts, alone ({v['facts']})")

    def n_reads_only():
        d = edited(hm, notes(note("D-001", disposition="withdrawn"), note(None, kind="issue", statement="s", disposition="open")), "C12_open_high")
        before = tree(d)
        look_at(d), look_at(d)
        need(tree(d) == before, "the view writes nothing and changes no file of the run")
        d = folder(hm)
        before = tree(d)
        look_at(d)
        need(tree(d) == before and not os.path.exists(os.path.join(d, "owner")), "nor makes the owner folder when there is none")

    # ------------------------------------------------------------------------------- words (§3a)
    def w_words():
        listed, hits = fx.domain_words(), []
        need(len(listed) > 50, "tools/workbench/domain_words.txt holds the word list")
        for root, dirs, files in os.walk(_WB):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "acceptance")]
            for n in files:
                if n == "domain_words.txt":
                    continue
                with open(os.path.join(root, n), encoding="utf-8", errors="ignore") as f:
                    text = f.read().lower()
                hits += [f"{os.path.relpath(os.path.join(root, n), _WB)}: {w}" for w in listed if w in text]
        need(not hits, f"domain words in the code: {hits[:6]}")
        for defect in (None, "C12_open_high", "prose_only", "older_layout"):
            text = json.dumps(look_at(folder(hm, defect)), default=str).lower()
            need(not [w for w in listed if w in text], f"the view's result on a fixture holds no domain word ({defect})")
        v = look_at(folder(post))
        need(set(v["tables"]) == set(look_at(folder(hm))["tables"]) and rows(v), "the model is the same for another method: only its rows differ")

    case("G1 the six checks, in order, each ok on the clean run, with one sentence, the file and field read, and its scorecard case", m_checks)
    case("G2 facts: the nine counts of §5.8, the two process counts of §6 and the count of fixes not verified (§12), on both clean runs", m_facts)
    case("G3 tables.rows: the columns of §5.8 with nature beside the id, evidence beside the disposition (§12), and signed_by; one row per id; known words", m_rows)
    case("G4 tables.by_source: source, rows, open; a source the run does not have is absent (assumed: science rows, first source)", m_by_source)
    case("G5 charts: with enough rows, bars by source and by disposition over tables that exist; with few rows, none (§12)", m_charts)
    case("G6 notes say which copy of the spec was read and who signed it", m_notes)
    case("D1 findings: each status word of the ledger maps to its disposition; kind, severity, blocking and claim come through", d_status_words)
    case("D2 a scope declaration's id is its blocker_id, else its id; it is merged into the finding it names; a test path is reduced to its id", d_scope)
    case("D3 engine departures: departure:<component or benchmark>, an assumption, declared when signed, else open", d_departures)
    case("D4 a declared calibration miss: calibration:<target or offset>, tested by its decision test", d_calibration)
    case("D5 an excluded input: excluded:<parameter or kind>, an assumption, declared; with no test, declared_untested is look", d_excluded)
    case("D6 a sign-off sheet row counts only where the sheet marks a finding HIGH: presign:<input>, open while the sign-off record does not bind the sheet (assumed: severity HIGH)", d_sheet)
    case("D7 a dossier entry with no data keeps its whole key; tested only through its decision_test key, else open", d_dossier)
    case("D8 a plan-changing test that no row names becomes test:<id>, open, with its sentence; a linked one does not", d_tests)
    case("D9 decides: yes on any CHANGES_PLAN, no when every test is NO_CHANGE, else unknown", d_decides)
    case("D10 first_visible_stage in the stage machine's names; a scope declaration with no finding is GATE (assumed: a finding's owner_state)", d_stage)
    case("D11 nature: a harness check's finding is process and is counted apart; facts, by_source and the checks count science rows", d_nature)
    case("D12 signed_by: who signed the spec that carries a declared row; a lead's signature is shown as such (assumed: A7)", d_signed_by)
    case("D13 two rows that share a decision test id are one row; the earlier disposition is kept, the later source added", d_merge_test)
    case("D14 excluded:x and nodata:...:x are one row; a name that only resembles another is not merged (assumed: A8)", d_merge_input)
    case("D15 two critics' findings on one subject are not merged; a note gives the count that share a stage and severity", d_no_mechanical_merge)
    case("D16 an older run: declared_findings.yaml and final_gate.json set dispositions; a declined finding stays open (assumed: A9)", d_older)
    case("D17 only the signed copy counts: a declaration that is only in the spec on disk is no row, and the notes say which copy was read", d_signed_copy)
    case("D18 prose_only: comment lines of the signed copy that start a departure, limitation or assumption, counted and shown (assumed: A14)", d_prose)
    n = 0
    for method, defects in ((hm, fx.REGISTER_DEFECTS),
                            (post, ("C12_open_high", "C12_declared_tested", "test_unlinked", "decision_tests_missing"))):
        for defect in defects:
            n += 1
            spec = fx.DEFECTS[defect]
            also = "".join(f", {k} {s}" for k, s in spec.get("also", {}).items())
            case(f"P{n} {method}, {defect}: {spec['check']} is {spec['status']}{also}, and no other check changes", planted(method, defect))
    case("X1 a run with none of the register's files: every check not_available, never an exception", x_nothing)
    case("X2 a run stopped before analysis: the checks that need decision tests are not_available, the rest unchanged", x_before_analysis)
    case("X3 no verified signed spec, three ways: nothing it carries counts, undisposed is a problem, prose_only not_available (assumed: A11)", x_unverified)
    case("X4 a source file that is there but cannot be parsed is named and never read as nothing declared (assumed: A13)", x_unparsable)
    case("X5 a ledger whose lines are not finding rows is a problem; with no ledger, a scope declaration declares nothing", x_ledger)
    case("N1 a review note sets a row's disposition; the derived value stays beside it (assumed: A15)", n_disposition)
    case("N2 a review note with no row adds a row with source owner_note", n_new_row)
    case("N3 a same_as note folds two rows into one", n_same_as)
    case("N4 a note line that cannot be used is shown as a problem with its line, never dropped silently; the others apply", n_malformed)
    case("N5 the view only reads: no file of the run changes, and no owner folder is made", n_reads_only)
    case("W1 no domain word in the code or in the view's result; the model is the same for another method (§3a)", w_words)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: register.py --self-test")
