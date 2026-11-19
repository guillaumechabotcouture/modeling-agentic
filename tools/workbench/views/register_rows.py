#!/usr/bin/env python3
"""The science register's rows, derived on read from a run's files (docs/design/review-workbench.md §6, step 1).

    python3 tools/workbench/views/register_rows.py --self-test

`derive(run)` (run is a workbench.Run) returns the generic Register model that views/register.py turns into
checks, facts and tables. It never raises on a bad file and it writes nothing. Rows are derived in the order of §6's
table; a later source's row is merged into an earlier one when it has the same id, shares a decision test id with
it, or names the same input (`excluded:x` and `nodata:parameter:x`). The owner's review notes
(owner/review_notes.jsonl, review_notes.py) are applied last, in the order they were written.

The model: {rows, flags, spec, tests, tests_why, prose, notes, bad_notes, notes_why, folded, nothing}.
  rows    one dict per row with the columns of COLUMNS, where `sources`, `files` and `tests` are lists, and
          `derived` (the disposition before any note), `noted` (a note set the disposition) and `decided` (a note
          set whether the row decides).
  flags   [(status, sentence)]: what a file says that the rows cannot carry (a ledger line that is no finding, a
          declaration of nothing, a status word no producer writes, a file that cannot be parsed, no verified
          signed spec). status is problem or not_available.
  tests   {test id: {status, verdict, statement}} from analysis/decision_tests.yaml, or None with `tests_why`.
  prose   [(line number, text)] of the verified signed spec's comment lines that start a departure, limitation or
          assumption, or None with no verified signed spec; `prose_more` {line number: the comment lines after it}.
  nothing True when the run has none of the register's files.

Rules applied here (§3): only a verified signed spec can declare anything, so with no verified copy the rows that
rest on the spec, its sign-off sheet or a signed engine departure are shown open; a file the sign-off record holds a
hash for counts only while it has that hash; a status word no producer writes gives a row no disposition.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

import yaml

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
import review_notes  # noqa: E402

COLUMNS = ["id", "kind", "statement", "source", "first_visible_stage", "decides", "disposition", "tests", "verdicts",
           "severity", "blocking", "files", "nature", "signed_by"]
SOURCES = ("findings", "scope_declaration", "engine_departures", "calibration_declared", "uncertainty_excluded",
           "signoff_sheet", "evidence_dossier", "decision_tests", "owner_note")
WORDS = review_notes.DISPOSITIONS
LEDGER, SCOPE, CHOICE, SHEET, DOSSIER, DT = ("findings.jsonl", "scope_declaration.yaml", "engine_choice.json",
                                             "decision_spec.signoff_sheet.md", "evidence_dossier.yaml",
                                             "analysis/decision_tests.yaml")
OLD_DECLARED, OLD_GATE = "declared_findings.yaml", "final_gate.json"
SPEC, SIGNED, SIGNOFF = "decision_spec.yaml", "decision_brief.signed/decision_spec.yaml", "decision_spec.signoff.json"
NOTES = review_notes.FILE
# Each producer's own words, mapped explicitly (§3): agents/findings_ledger.py STATUSES, and the status
# toolkit/engines/benchmark_coverage.py gives an engine departure.
LEDGER_WORDS = {"open": "open", "declared": "declared", "fixed": "implemented", "owner_accepted": "declared",
                "owner_declined": "open"}
DEPARTURE_WORDS = {"signed": "declared", "proposed": "open", "unsigned": "open"}
VERDICTS = ("NO_CHANGE", "CHANGES_PLAN", "NOT_RUN")       # toolkit/analysis/run.py
CHANGES, NO_CHANGE, NO_RECORD = "CHANGES_PLAN", "NO_CHANGE", "NO_RECORD"
SEVERITIES = ("HIGH", "MEDIUM", "LOW")                    # agents/findings_ledger.py; the first is the highest
NAMED = ("departure", "calibration", "excluded", "presign", "nodata")    # row ids of the form <prefix>:<input name>
HIGH_LINE = re.compile(r"\s*[-*]\s+\*\*HIGH\*\*:\s*(.+?)\s*$")           # toolkit/spec/engine_parameters.py
HIGH_NAME = (re.compile(r"the uncertainty range of (\S+) "), re.compile(r"(\S+) = "))
PROSE = re.compile(r"\b(?:DEPARTURE|LIMITATION|ASSUMPTION)S?\b(?:\s*\([^)]*\))?\s*:")
MAX_TEXT = 8 << 20
UNLINKED = "This test changes the plan and is linked to no issue."


def _text(x, cap=4000):
    return " ".join(str(x).split())[:cap] if x is not None else ""


def _stem(ref):
    """A test reference reduced to the test id: a path's file stem (§6)."""
    s = str(ref or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    return s[:-5] if s.endswith(".yaml") else s[:-4] if s.endswith(".yml") else s


def _absent(why):
    return str(why or "").endswith("is not in the run folder")


def _row(rid, kind, statement, source, stage, disposition, rel, tests=(), **more):
    prefix, _, name = rid.partition(":")
    return {"id": rid, "kind": kind, "statement": _text(statement) or "(the file gives no text for this row)",
            "sources": [source], "first_visible_stage": stage, "disposition": disposition,
            "tests": [t for t in dict.fromkeys(_stem(t) for t in tests) if t], "severity": None, "blocking": None,
            "files": [rel], "nature": "science", "signed_by": None, "decides": "unknown", "noted": False,
            "decided": False, "name": name if source != "findings" and prefix in NAMED and name else None, **more}


def _fold(into, other):
    """`other` merged into `into`: the earlier row's id, kind, statement, stage and disposition are kept; the later
    row's sources, files and tests are added, and what the earlier row lacks is taken from it."""
    for key in ("sources", "files", "tests"):
        into[key] += [x for x in other[key] if x not in into[key]]
    for key in ("severity", "blocking", "signed_by", "name"):
        if into[key] is None:
            into[key] = other[key]
    if other["nature"] == "science":
        into["nature"] = "science"
    return into


def _same_input(a, b):
    return bool(a["name"] and b["name"]) and (a["id"].endswith(":" + b["name"]) or b["id"].endswith(":" + a["name"]))


def _place(rows, new):
    """Add a row, or merge it into the first earlier row of another source that shares its id, a test or its input."""
    for r in rows:
        other = new["sources"][0] not in r["sources"]
        if r["id"] == new["id"] or (other and (set(r["tests"]) & set(new["tests"]) or _same_input(r, new))):
            return _fold(r, new)
    rows.append(new)
    return new


def _spec(run, others):
    """What the register may take from the spec: {doc, rel, unverified, unsigned, signed_by, signed_at, say, record,
    text}. `doc` is the verified signed copy; with no verified copy it is whichever copy could be read, for names
    only. One read of the signed copy gives the hash, the document and the comment lines."""
    raw, why = run.read(SIGNED, MAX_TEXT + 1)
    so, so_why = run.json(SIGNOFF)
    out = {"doc": {}, "rel": SIGNED, "unverified": None, "unsigned": False, "text": None, "say": "",
           "signed_by": (so or {}).get("signed_by"), "signed_at": (so or {}).get("signed_at"),
           "record": so.get("sha256") if isinstance((so or {}).get("sha256"), dict) else {}}
    if raw is None and _absent(why) and so is None and _absent(so_why) and not others:
        out.update(unsigned=True, rel=None, say="The spec is not signed yet, so the register holds nothing from it.")
        return out
    signed, doc, bad = None, None, why
    if raw is not None and len(raw) <= MAX_TEXT:
        signed = hashlib.sha256(raw).hexdigest()
        try:
            doc = yaml.load(raw, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
            bad = None if isinstance(doc, dict) else f"{SIGNED} is not a mapping"
        except (ValueError, yaml.YAMLError) as e:
            doc, bad = None, f"{SIGNED} could not be parsed ({' '.join(str(e).split())[:100]})"
    want = checks.digest(out["record"].get(SPEC))
    if signed is None:
        out["unverified"] = f"the run has no readable signed copy of the spec ({bad})"
    elif so is None:
        out["unverified"] = f"the sign-off record could not be read ({so_why})"
    elif want is None:
        out["unverified"] = f"the sign-off record ({SIGNOFF}) holds no sha256 for {SPEC}"
    elif want != signed:
        out["unverified"] = (f"the signed copy ({SIGNED}, {signed[:8]}) differs from the hash the sign-off record "
                             f"holds ({want[:8]})")
    elif not isinstance(doc, dict):
        out["unverified"] = bad
    if not isinstance(doc, dict):
        _, doc, _ = run.hashed(SPEC)
        out["rel"] = SPEC
    out["doc"] = doc if isinstance(doc, dict) else {}
    disk = run.sha(SPEC)
    if out["unverified"]:
        out["signed_by"] = None
        out["say"] = (f"No signed spec could be verified: {out['unverified']}. {out['rel']} is read for names only: "
                      "nothing in it, in its sign-off sheet or in a signed engine departure counts as declared or tested.")
    else:
        out["text"] = raw.decode("utf-8", "replace")
        who = f"signed by {out['signed_by']} at {_text(out['signed_at'])[:16]}"
        out["say"] = (f"The register reads the signed copy of the spec, {SIGNED} ({signed[:8]}, {who}). " + (
            f"The spec on disk ({disk[:8]}) differs from it; nothing that is only in the spec on disk is a row."
            if disk and disk != signed else "The spec on disk is the same file." if disk else
            f"{SPEC} is not in the run folder."))
        if out["signed_by"] != "owner":
            out["say"] += (f" It was signed by {out['signed_by']}, not the owner: a row it declares keeps its "
                           "disposition, and the signed_by column says who signed.")
    return out


def _list(doc, *keys):
    for k in keys:
        doc = doc.get(k) if isinstance(doc, dict) else None
    return [x for x in doc if isinstance(x, dict)] if isinstance(doc, list) else []


def _tests(run):
    doc, why = run.large(DT)
    if doc is None:
        return None, f"{DT} is not in the run folder" if _absent(why) else why
    tests = doc.get("data", {}).get("tests") if isinstance(doc.get("data"), dict) else None
    if not isinstance(tests, list) or not all(isinstance(t, dict) and t.get("test_id") for t in tests):
        return None, f"{DT} holds no list of tests (data.tests)"
    return {str(t["test_id"]): {"status": t.get("status"), "verdict": t.get("verdict"),
                                "statement": _text(t.get("statement"))} for t in tests}, None


def derive(run):
    flags, rows, by_id = [], [], {}
    present = {rel: not _absent(run.read(rel, 1)[1]) for rel in (LEDGER, SCOPE, CHOICE, SHEET, DOSSIER, DT, OLD_DECLARED,
                                                              OLD_GATE, NOTES)}
    spec = _spec(run, any(present.values()))
    tests, tests_why = _tests(run)
    out = {"rows": rows, "flags": flags, "spec": spec, "tests": tests, "tests_why": tests_why, "prose": None,
           "notes": [], "bad_notes": [], "notes_why": None, "folded": [], "nothing": spec["unsigned"], "present": present}
    if spec["unsigned"]:
        return out
    verified = spec["unverified"] is None
    if not verified:
        flags.append(("problem", spec["say"]))

    def doc_of(rel):
        """The mapping in a JSON or YAML file, or None: absent files are silent, unreadable ones are flagged."""
        if not present[rel]:
            return None
        doc, why = run.large(rel) if rel.endswith(".yaml") else run.json(rel)
        if doc is None:
            flags.append(("not_available", f"{why}, so the rows it holds are not in the register."))
        return doc

    def signed_file(rel):
        """False when the sign-off record holds a hash for the file and the file no longer has it (§3)."""
        if rel not in spec["record"]:
            return True
        state = checks.hash_state(spec["record"][rel], run.sha(rel))
        if state != "same":
            flags.append(("problem", f"{rel} is not the file that was signed (against the hash in {SIGNOFF}: "
                                     f"{state.replace('_', ' ')}), so its rows are shown open."))
        return state == "same"

    # 1. findings.jsonl, and the two files in which an older run keeps dispositions
    raw, why = run.read(LEDGER, MAX_TEXT)
    if present[LEDGER] and raw is None:
        flags.append(("not_available", f"{why}, so no finding is in the register."))
    for n, line in enumerate((raw or b"").decode("utf-8", "replace").split("\n"), 1):
        if not line.strip():
            continue
        try:
            f = json.loads(line)
        except ValueError:
            f = None
        if not (isinstance(f, dict) and isinstance(f.get("id"), str) and f["id"].strip() and "status" in f):
            flags.append(("problem", f"Line {n} of {LEDGER} is not a finding row (a JSON object with an id and a "
                                     "status), so a finding may be missing from the register."))
            continue
        word = f["status"]
        known = isinstance(word, str) and word in LEDGER_WORDS
        if not known:
            flags.append(("not_available", f"Finding {f['id']} in {LEDGER} has the status {word!r}, a word the "
                                           "ledger does not write, so its disposition is not known."))
        dt = f.get("decision_test")
        stage = f.get("owner_state") if f.get("owner_state_known") is not False and f.get("owner_state") else f.get("target_stage")
        critic = not isinstance(f.get("source"), str) or f["source"].startswith("critique")
        r = _place(rows, _row(f["id"], "issue", f.get("claim"), "findings", stage, LEDGER_WORDS[word] if known else None,
                              LEDGER, [dt.get("file")] if isinstance(dt, dict) else [], severity=f.get("severity"),
                              blocking=f.get("blocking") if isinstance(f.get("blocking"), bool) else None,
                              nature="science" if critic else "process",
                              signed_by="owner" if word == "owner_accepted" else None, ledger=word))
        by_id[r["id"]] = r
    older = [(OLD_DECLARED, "declared", lambda d: [x.get("id") for x in _list(d, "declared")] if isinstance(d.get("declared"), list) else None),
             (OLD_GATE, "tested", lambda d: d.get("resolved_by_test", []) if isinstance(d.get("resolved_by_test", []), list) else None)]
    for rel, word, ids in older:
        doc = doc_of(rel)
        named = ids(doc) if doc is not None else []
        if named is None:
            flags.append(("not_available", f"{rel} does not hold the list it is read for, so the dispositions it "
                                           "gives are not known."))
        for fid in named or []:
            r = by_id.get(fid)
            if r is None:
                flags.append(("problem", f"{rel} names {fid!r} as {word}, which {LEDGER} does not hold: the two "
                                         "files disagree."))
            elif r.get("ledger") == "open" and r["disposition"] == "open":
                r["disposition"] = word
                r["files"].append(rel)

    # 2. scope_declaration.yaml
    doc = doc_of(SCOPE)
    if doc is not None and not isinstance(doc.get("declarations"), list):
        flags.append(("not_available", f"{SCOPE} holds no list of declarations, so what it declares is not in the register."))
    for d in _list(doc or {}, "declarations"):
        rid = _text(d.get("blocker_id") or d.get("id"))
        if not rid:
            flags.append(("problem", f"A declaration in {SCOPE} has no blocker_id and no id."))
            continue
        if rid not in by_id:
            flags.append(("problem", f"{SCOPE} declares {rid}, which {LEDGER} does not hold: a declaration that "
                                     "names no finding is a declaration of nothing."))
        _place(rows, _row(rid, "issue", d.get("why_unresolved"), "scope_declaration", "GATE", "declared", SCOPE,
                          [d.get("decision_test")]))

    # 3. engine_choice.json engine_departures
    doc = doc_of(CHOICE)
    if doc is not None and not isinstance(doc.get("engine_departures", []), list):
        flags.append(("not_available", f"{CHOICE} engine_departures is not a list, so the departures are not in the register."))
    held = {_text(s.get(k)) for s in _list(spec["doc"], "engine_departures") for k in ("component", "benchmark") if s.get(k)}
    for d in _list(doc or {}, "engine_departures"):
        name = _text(d.get("component") or d.get("benchmark"))
        word = d.get("status")
        if not name:
            flags.append(("problem", f"An engine departure in {CHOICE} names no component and no benchmark."))
            continue
        disposition = DEPARTURE_WORDS.get(word) if isinstance(word, str) else None
        if disposition is None:
            flags.append(("not_available", f"Engine departure {name} in {CHOICE} has the status {word!r}, a word the "
                                           "engine choice does not write, so its disposition is not known."))
        elif disposition == "declared" and verified and name not in held:
            disposition = "open"
            flags.append(("problem", f"{CHOICE} marks the engine departure for {name} signed, but the signed spec's "
                                     "engine_departures does not hold it: the two files disagree."))
        elif disposition == "declared" and not verified:
            disposition = "open"
        _place(rows, _row(f"departure:{name}", "assumption", d.get("reason"), "engine_departures", "ENGINE_CHOICE",
                          disposition, CHOICE, [d.get("decision_test")],
                          signed_by=spec["signed_by"] if disposition == "declared" else None))

    # 4, 5. the signed spec: declared calibration misses and excluded inputs
    def carried(word):
        return (word, spec["signed_by"]) if verified else ("open", None)
    for d in _list(spec["doc"], "calibration", "joint", "unmatched", "declared"):
        name = _text(d.get("target") or d.get("offset"))
        if name:
            word, who = carried("tested" if d.get("decision_test") else "declared")
            _place(rows, _row(f"calibration:{name}", "issue", d.get("reason"), "calibration_declared", "SIGN", word,
                              spec["rel"], [d.get("decision_test")], signed_by=who))
    for d in _list(spec["doc"], "uncertainty", "excluded"):
        name = _text(d.get("parameter") or d.get("kind"))
        if name:
            word, who = carried("declared")
            _place(rows, _row(f"excluded:{name}", "assumption", d.get("reason"), "uncertainty_excluded", "SIGN", word,
                              spec["rel"], signed_by=who))

    # 6. the sign-off sheet's rows marked HIGH
    raw, why = run.read(SHEET, MAX_TEXT)
    if present[SHEET] and raw is None:
        flags.append(("not_available", f"{why}, so the rows it marks HIGH are not in the register."))
    sheet_ok = signed_file(SHEET) if raw is not None else True
    for n, line in enumerate((raw or b"").decode("utf-8", "replace").split("\n"), 1):
        m = HIGH_LINE.match(line)
        if not m:
            continue
        name = next((x.group(1) for x in (p.match(m.group(1)) for p in HIGH_NAME) if x), None)
        if name is None:
            flags.append(("not_available", f"Line {n} of {SHEET} marks a finding HIGH in a form that is not "
                                           "recognised, so the input it is about is not known."))
        word, who = carried("declared") if sheet_ok else ("open", None)
        _place(rows, _row(f"presign:{name or f'line {n}'}", "issue", m.group(1), "signoff_sheet", "SIGN", word, SHEET,
                          signed_by=who, severity="HIGH"))

    # 7. evidence_dossier.yaml entries with no data
    doc = doc_of(DOSSIER)
    if doc is not None and not isinstance(doc.get("inputs"), dict):
        flags.append(("not_available", f"{DOSSIER} holds no inputs mapping, so its entries are not in the register."))
    dossier_ok = signed_file(DOSSIER) if doc is not None else True
    inputs = doc.get("inputs") if doc is not None and isinstance(doc.get("inputs"), dict) else {}
    for key, e in inputs.items():
        if isinstance(e, dict) and e.get("no_data"):
            nd, test = e["no_data"], e.get("decision_test")
            test = test if isinstance(test, str) else None      # only the structured key names a test
            _place(rows, _row(f"nodata:{key}", "uncertainty", nd.get("reason") if isinstance(nd, dict) else nd,
                              "evidence_dossier", "SIGN", "tested" if test and dossier_ok else "open", DOSSIER, [test]))

    # 8. plan-changing decision tests that no row names
    linked = {t for r in rows for t in r["tests"]}
    asks = {str(t.get("id")): _text(t.get("text")) for t in _list(spec["doc"], "analysis", "decision_tests")}    # the spec's own question
    for tid, t in (tests or {}).items():
        if t["verdict"] == CHANGES and tid not in linked:
            said = " ".join(x for x in (asks.get(tid), t["statement"], UNLINKED) if x)
            _place(rows, _row(f"test:{tid}", "uncertainty", said, "decision_tests", "ANALYZE", "open", DT, [tid]))

    for r in rows:
        r["derived"] = r["disposition"]
    _notes(run, out, present[NOTES])
    for r in rows:
        r["verdicts"] = [_verdict(tests, t) for t in r["tests"]]
        if not r["decided"]:
            r["decides"] = ("yes" if CHANGES in r["verdicts"] else
                            "no" if r["verdicts"] and all(v == NO_CHANGE for v in r["verdicts"]) else "unknown")
    if verified:
        lines = spec["text"].split("\n")
        out["prose"] = [(n, line.strip()) for n, line in enumerate(lines, 1) if _starts(line)]
        out["prose_more"] = {n: _continues(lines, n) for n, _ in out["prose"]}
    return out


def _starts(line):
    return line.lstrip().startswith("#") and bool(PROSE.search(line))


def _continues(lines, n, cap=4):
    """The comment lines that follow line n, as one text: up to `cap` of them, stopping at a line that is no comment,
    an empty comment, or the next line that starts a departure, limitation or assumption."""
    more = []
    for line in lines[n:n + cap]:
        text = line.strip().lstrip("#").strip()
        if not line.lstrip().startswith("#") or not text or _starts(line):
            break
        more.append(text)
    return " ".join(more)


def _verdict(tests, tid):
    """A linked test's verdict as the tests file gives it; NO_RECORD when the file has no such test or no file."""
    v = (tests or {}).get(tid, {}).get("verdict")
    return str(v) if v not in (None, "") else NO_RECORD


def _notes(run, out, there):
    """Apply the owner's notes in the order written. A line that cannot be used is kept in `bad_notes`."""
    if not there:
        return
    raw, why = run.read(NOTES, review_notes.MAX_FILE + 1)
    if raw is None or len(raw) > review_notes.MAX_FILE:
        out["notes_why"] = why or f"{NOTES} is larger than {review_notes.MAX_FILE >> 20} MB and is not read"
        return
    rows, added = out["rows"], 0
    for n, note, bad, text in review_notes.parse(raw):
        by_id = {r["id"]: r for r in rows}
        if note is not None and note["row"] is not None:
            bad = next((f"it names the row {note[k]!r}, which the register does not hold" for k in ("row", "same_as")
                        if note.get(k) is not None and note[k] not in by_id), None)
        if bad:
            out["bad_notes"].append((n, text[:300], bad))
            continue
        if note["row"] is None:
            added += 1
            r = _row(f"note:{added}", note["kind"], note["statement"], "owner_note", None, note["disposition"] or "open", NOTES)
            r.update(derived=None, noted=True, signed_by="owner" if note["disposition"] not in (None, "open") else None)
            rows.append(r)
        else:
            r = by_id[note["row"]]
            if NOTES not in r["files"]:
                r["files"].append(NOTES)
                r["sources"].append("owner_note")
            if note.get("same_as") is not None:
                other = by_id[note["same_as"]]
                rows.remove(other)
                _fold(r, other)
                out["folded"].append((r["id"], other["id"]))
            if note["disposition"] is not None:
                r.update(disposition=note["disposition"], noted=True, signed_by="owner")
        if note["decides"] is not None:      # the owner's unknown leaves the row among those whose bearing is unknown
            r.update(decides=note["decides"], decided=note["decides"] != "unknown")
        out["notes"].append({"at": note["at"], "row": r["id"], "disposition": note["disposition"],
                             "derived_disposition": r["derived"], "statement": r["statement"],
                             "note": note["note"] + (f" (same as {note['same_as']})" if note.get("same_as") else "")})


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    from views.fit_bench import Bench, fixture_builder, need
    fx = fixture_builder()
    b = Bench(derive)
    hm = "history_matching"

    def look(d):
        import workbench
        return derive(workbench.Run(d))

    def by(m):
        return {r["id"]: r for r in m["rows"]}

    def text(rel, change):
        def edit(d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                was = f.read()
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                f.write(change(was))
        return edit

    def shape():
        m = look(b.folder(hm))
        need(set(m) >= {"rows", "flags", "spec", "tests", "prose", "notes", "bad_notes", "folded", "nothing"}, "the model's parts")
        need(m["flags"] == [] and m["prose"] == [] and not m["nothing"], f"a clean run raises nothing ({m['flags']})")
        need(all(r["sources"][0] in SOURCES and r["derived"] == r["disposition"] and len(r["tests"]) == len(r["verdicts"])
                 for r in m["rows"]), "every row has a source, its derived disposition and a verdict per test")
        need(_stem("models/decision_tests/DT3.yaml") == "DT3" and _stem("DT3") == "DT3" and _stem(None) == "", "a test reference is its file stem")

    def helpers():
        a, c = _row("excluded:x", "assumption", "s", "uncertainty_excluded", "SIGN", "declared", "f"), \
            _row("nodata:parameter:x", "uncertainty", "s", "evidence_dossier", "SIGN", "open", "g", ["T1"])
        need(_same_input(a, c) and not _same_input(a, _row("nodata:parameter:xx", "uncertainty", "s", "evidence_dossier", "SIGN", "open", "g")),
             "an input is the same only by its whole name")
        rows = [a]
        need(_place(rows, c) is a and a["tests"] == ["T1"] and a["sources"] == ["uncertainty_excluded", "evidence_dossier"]
             and a["disposition"] == "declared" and len(rows) == 1, "a merged row keeps the earlier disposition and gains the later source")
        again = _row("nodata:parameter:y", "uncertainty", "s", "evidence_dossier", "SIGN", "tested", "g", ["T1"])
        need(_place(rows, again) is again and len(rows) == 2, "two rows of one source that share a test stay two rows")
        lines = ["a: 1", "# LIMITATION: the first", "# goes on here", "#", "# not this", "# ASSUMPTION: next", "b: 2"]
        need(_continues(lines, 2) == "goes on here" and _continues(lines, 6) == "" and _starts(lines[1]) and not _starts(lines[2]),
             "the lines after a marker line are read up to an empty comment, another marker or a line that is no comment")
        need(PROSE.search("# ASSUMPTIONS (two): a") and PROSE.search("#  x LIMITATION: y") and not PROSE.search("# see the DEPARTURE above"),
             "a comment line counts when a capitalised marker is followed by a colon")

    def signed_files():
        def listed(d):
            with open(os.path.join(d, SIGNOFF), encoding="utf-8") as f:
                so = json.load(f)
            for rel in (fx.SHEET, fx.DOSSIER):
                with open(os.path.join(d, rel), "rb") as f:
                    so["sha256"][rel] = hashlib.sha256(f.read()).hexdigest()
            with open(os.path.join(d, SIGNOFF), "w", encoding="utf-8") as f:
                json.dump(so, f)
        m = look(b.edited(hm, listed))
        need(m["flags"] == [] and by(m)["presign:p2"]["disposition"] == "declared", f"signed files that still have their hash count ({m['flags']})")
        for rel, rid in ((fx.SHEET, "presign:p2"), (fx.DOSSIER, "nodata:unit_attribute:x1")):
            m = look(b.edited(hm, lambda d, rel=rel: (listed(d), text(rel, lambda t: t + "\n")(d))))
            need(by(m)[rid]["disposition"] == "open" and any(s == "problem" and rel in why for s, why in m["flags"]),
                 f"{rel} changed after the sign-off: its rows are open and it is flagged ({m['flags']})")

    def departures():
        def unheld(d):
            with open(os.path.join(d, fx.CHOICE), encoding="utf-8") as f:
                doc = json.load(f)
            doc["engine_departures"].append({"engine": "toy-engine", "component": "Z", "status": "signed", "reason": "r", "decision_test": "DT1"})
            doc["engine_departures"][0]["status"] = "agreed"
            with open(os.path.join(d, fx.CHOICE), "w", encoding="utf-8") as f:
                json.dump(doc, f)
        m = look(b.edited(hm, unheld))
        need(by(m)["departure:Z"]["disposition"] == "open" and any(s == "problem" and "Z" in why for s, why in m["flags"]),
             "a departure marked signed that the signed spec does not hold is open, and flagged as a contradiction")
        need(by(m)["departure:B2"]["disposition"] is None and any(s == "not_available" and "'agreed'" in why for s, why in m["flags"]),
             "a status word the engine choice does not write gives no disposition, and is named")

    def notes_unreadable():
        def link(d):
            os.makedirs(os.path.join(d, "owner"))
            with open(os.path.join(d, "elsewhere.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"at": "2026-01-02T09:00:00+00:00", "row": "D-001", "kind": None, "statement": None,
                                    "disposition": "withdrawn", "decides": None, "note": ""}) + "\n")
            os.symlink(os.path.join(d, "elsewhere.jsonl"), os.path.join(d, NOTES))
        m = look(b.edited(hm, link, "C12_open_high"))
        need(m["notes_why"] and m["notes"] == [] and by(m)["D-001"]["disposition"] == "open",
             f"a notes file that is a link is not read, and says so ({m['notes_why']})")

    b.case("RR1 the model's shape on a clean run; a test reference is reduced to its id", shape)
    b.case("RR2 merging: the same input by its whole name, a shared test across sources, the earlier disposition kept", helpers)
    b.case("RR3 a file the sign-off record holds a hash for counts only while it has that hash (§3)", signed_files)
    b.case("RR4 an engine departure marked signed that the signed spec does not hold is open; an unknown status word is named", departures)
    b.case("RR5 a notes file that cannot be read through the confined reader is not applied, and says so", notes_unreadable)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: register_rows.py --self-test")
