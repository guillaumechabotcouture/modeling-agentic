#!/usr/bin/env python3
"""The science register's rows, derived on read from a run's files (docs/design/review-workbench.md §6, step 1).

    python3 tools/workbench/views/register_rows.py --self-test

`derive(run)` (run is a workbench.Run) returns the generic Register model that views/register.py turns into
checks, facts and tables. It never raises on a bad file and it writes nothing. Rows are derived in the order of §6's
table; a later source's row is merged into an earlier one when it has the same id, shares a decision test id with
it, or names the same input (`excluded:x` and `nodata:parameter:x`). The owner's review notes
(owner/review_notes.jsonl, review_notes.py) are applied last, in the order they were written.

The model: {rows, flags, spec, tests, tests_why, asks, prose, notes, bad_notes, notes_why, folded, nothing}.
  rows    one dict per row with the columns of COLUMNS, where `sources`, `files` and `tests` are lists, and
          `derived` (the disposition before any note), `noted` (a note set the disposition) and `decided` (a note
          set whether the row decides).
  flags   [(status, sentence)]: what a file says that the rows cannot carry (a ledger line that is no finding, a
          declaration of nothing, a status word no producer writes, a file that cannot be parsed, no verified
          signed spec). status is problem or not_available.
  tests   {test id: {status, verdict, statement, state}} (checks.decision_tests), or None with `tests_why`; `asks`
          {test id: the signed spec's question}.
  prose   [(line number, text)] of the verified signed spec's comment lines that start a departure, limitation or
          assumption, or None with no verified signed spec; `prose_more` {line number: the comment lines after it}.
  nothing True when the run has none of the register's files.

Rules applied here (§3): only a verified signed spec can declare anything, so with no verified copy the rows that
rest on the spec, its sign-off sheet or a signed engine departure are shown open; the sign-off record is the one the
producer selects (checks.signoff_record); a file counts as signed only while it has the hash that record holds for
it, and a file the record holds no hash for is not signed; a status word no producer writes gives a row no
disposition. A declaration or departure is matched to its own entry by identity (the finding id it names; the
engine, the typed component or benchmark and the test of the signed entry) before any match by a shared test. The
ledger and the sheet are read to their end, never cut at a size; a list entry that is no mapping is a problem. A
finding the ledger holds as fixed is `implemented` only with the gate's recorded result that verifies it (`evidence`),
else "implemented, not verified" (§12).
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

COLUMNS = ["id", "nature", "kind", "statement", "source", "first_visible_stage", "decides", "disposition", "evidence",
           "tests", "verdicts", "severity", "blocking", "files", "signed_by"]
SOURCES = ("findings", "scope_declaration", "engine_departures", "calibration_declared", "uncertainty_excluded",
           "signoff_sheet", "evidence_dossier", "decision_tests", "owner_note")
WORDS = review_notes.DISPOSITIONS
# §12 (reader finding 2): the ledger's `fixed` is `implemented` only with the gate's recorded result that verifies it
# shown on the row; without it the row reads as below and counts as a row with no disposition.
UNVERIFIED = "implemented, not verified"
GATE = re.compile(r"gate_r(\d+)(_post)?\.json")        # agents/stage_machine.py: gate_r<round>.json, gate_r<round>_post.json
LEDGER, SCOPE, CHOICE, SHEET, DOSSIER, DT = ("findings.jsonl", "scope_declaration.yaml", "engine_choice.json",
                                             "decision_spec.signoff_sheet.md", "evidence_dossier.yaml",
                                             "analysis/decision_tests.yaml")
OLD_DECLARED, OLD_GATE = "declared_findings.yaml", "final_gate.json"
SPEC, SIGNED, SIGNOFF = checks.SPEC, "decision_brief.signed/decision_spec.yaml", checks.SPEC_SIGNOFF
NOTES = review_notes.FILE
# Each producer's own words, mapped explicitly (§3): agents/findings_ledger.py STATUSES, and the status
# toolkit/engines/benchmark_coverage.py gives an engine departure.
LEDGER_WORDS = {"open": "open", "declared": "declared", "fixed": "implemented", "owner_accepted": "declared",
                "owner_declined": "open"}
DEPARTURE_WORDS = {"signed": "declared", "proposed": "open", "unsigned": "open"}
VERDICTS = checks.VERDICTS                                # toolkit/analysis/run.py
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
            "files": [rel], "nature": "science", "signed_by": None, "decides": "unknown", "noted": False, "evidence": None,
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


def _place(rows, new, by_id_only=False):
    """Add a row, or merge it into an earlier one: the row with its id first, wherever that row is; only then, and
    only for a row that names nothing by id (`by_id_only` false), the first row of another source that shares a
    test or its input. So a declaration that names a finding goes to that finding, never to one that shares its test."""
    same = next((r for r in rows if r["id"] == new["id"]), None)
    if same is None and not by_id_only:
        same = next((r for r in rows if new["sources"][0] not in r["sources"]
                     and (set(r["tests"]) & set(new["tests"]) or _same_input(r, new))), None)
    if same is not None:
        return _fold(same, new)
    rows.append(new)
    return new


def _spec(run, others):
    """What the register may take from the spec: {doc, rel, unverified, unsigned, signed_by, signed_at, say, record,
    text}. `doc` is the verified signed copy; with no verified copy it is whichever copy could be read, for names
    only. One read of the signed copy gives the hash, the document and the comment lines."""
    raw, why = run.read(SIGNED, MAX_TEXT + 1)
    so, so_rel, so_why = checks.signoff_record(run)      # the producer's own selection of the record
    out = {"doc": {}, "rel": SIGNED, "unverified": None, "unsigned": False, "text": None, "say": "",
           "signed_by": (so or {}).get("signed_by"), "signed_at": (so or {}).get("signed_at"), "signoff": so_rel,
           "so": so, "record": so.get("sha256") if isinstance((so or {}).get("sha256"), dict) else {}}
    if raw is None and _absent(why) and so is None and _absent(so_why) and not others:
        out.update(unsigned=True, rel=None, say="The spec is not signed yet, so the register holds nothing from it.")
        return out
    signed, doc, bad = None, None, why or f"{SIGNED} is larger than {MAX_TEXT >> 20} MB and is not read"
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
        out["unverified"] = f"the sign-off record ({so_rel}) holds no sha256 for {SPEC}"
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
        out["say"] = (f"The register reads the signed copy of the spec, {SIGNED} ({signed[:8]}, {who}; the sign-off "
                      f"record is {so_rel}). " + (
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


def _entries(flags, rel, doc, *keys):
    """The mappings of a list in a file, in order. An entry that is no mapping is never dropped silently: it is a
    `problem` that names the list and the entry's position, since what it was meant to declare is not in the register."""
    for k in keys:
        doc = doc.get(k) if isinstance(doc, dict) else None
    out = []
    for n, x in enumerate(doc if isinstance(doc, list) else [], 1):
        if isinstance(x, dict):
            out.append(x)
        else:
            flags.append(("problem", f"Entry {n} of {'.'.join(keys)} in {rel} is not a mapping (it is {_text(repr(x), 60)}), "
                                     "so what it declares, if anything, is not in the register."))
    return out


def _gates(run, flags):
    """[(file, its done_when_status)] for the run's gate records, oldest first: gate_r<n>.json then gate_r<n>_post.json
    for each round, and final_gate.json last (the stage machine writes it from the last gate)."""
    names = run.names("")
    found = sorted((int(m.group(1)), bool(m.group(2)), n) for n in names for m in [GATE.fullmatch(n)] if m)
    out = []
    for rel in [n for _, _, n in found] + ([OLD_GATE] if OLD_GATE in names else []):
        doc, why = run.json(rel)
        if doc is None and rel != OLD_GATE:      # final_gate.json is flagged where the older dispositions are read
            flags.append(("not_available", f"{why}, so the results it records for done-when tests are not known."))
        out.append((rel, doc.get("done_when_status") if doc is not None and isinstance(doc.get("done_when_status"), dict) else {}))
    return out


def _fixed(f, gates):
    """(verified, the evidence in words) for a finding the ledger holds as `fixed`. The ledger sets it two ways
    (agents/findings_ledger.py): a critic's finding follows the critic's own `resolved` mark, and what verifies it is
    the gate's recorded result for its done-when test (scripts/done_when.py run: done_when_status[id] with passes
    and result), taken from the last gate record that holds the finding; a gate finding is fixed when it is absent
    from a later pre-write gate (the event "absent at this gate"), and what verifies it is that round's gate record."""
    if not isinstance(f.get("source"), str) or f["source"].startswith("critique"):
        rel, e = next(((rel, dws[f["id"]]) for rel, dws in reversed(gates) if isinstance(dws.get(f["id"]), dict)), (None, None))
        if e is None:
            return False, ("no gate record holds a result for its done-when test; the ledger's fixed follows the critic's "
                           "own mark, which nothing here verifies")
        result = e.get("result") if isinstance(e.get("result"), str) else None
        said = result or ("passed" if e.get("passes") is True else "failed" if e.get("passes") is False else "not_judged")
        if e.get("passes") is True and said == "passed":
            return True, f"{rel}: its done-when test passed ({_text(e.get('detail'), 200) or 'no detail recorded'})"
        return False, (f"{rel}, the last gate record that holds it, gives its done-when test as {said.replace('_', ' ')}"
                       + (f" ({_text(e.get('detail'), 200)})" if e.get("detail") else "") + "; the ledger's fixed follows "
                       "the critic's own mark")
    events = [e for e in f.get("events") or [] if isinstance(e, dict) and e.get("status") == "fixed" and e.get("by") == "gate"]
    if not events:
        return False, f"{LEDGER} holds no gate event that says how it was fixed"
    rnd = events[-1].get("round")
    rel = f"gate_r{rnd}.json"
    if any(name == rel for name, _ in gates):
        return True, f"{LEDGER} events: {_text(events[-1].get('note')) or 'fixed'} in round {rnd}; that gate's record is {rel}"
    return False, f"{LEDGER} events say it was absent at the gate of round {rnd}, and the run holds no {rel}"


def _tests(run):
    """checks.decision_tests: the one loader of decision tests, shared with the Fit view."""
    tests, why = checks.decision_tests(run)
    return tests, (f"{DT} is not in the run folder" if tests is None and _absent(why) else why)


def _ran(tests, tid):
    """The test ran for the verified signed spec (checks.decision_tests `counts`, §3)."""
    return (tests or {}).get(tid, {}).get("counts") is True


def derive(run):
    flags, rows, by_id = [], [], {}
    present = {rel: not _absent(run.read(rel, 1)[1]) for rel in (LEDGER, SCOPE, CHOICE, SHEET, DOSSIER, DT, OLD_DECLARED,
                                                              OLD_GATE, NOTES)}
    spec = _spec(run, any(present.values()))
    tests, tests_why = _tests(run)
    out = {"rows": rows, "flags": flags, "spec": spec, "tests": tests, "tests_why": tests_why, "prose": None,
           "notes": [], "bad_notes": [], "notes_why": None, "folded": [], "nothing": spec["unsigned"], "present": present, "asks": {}}
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

    bound = {}

    def signed_file(rel):
        """True only when the sign-off record holds a hash for the file and the file still has it (§3: an absent hash
        is never a match). The producer binds the files it lists under the record's sha256 (toolkit/spec/signoff.py
        sign: the spec, the derivation record, a reference file, the evidence files; never the sign-off sheet, whose
        hash sign() compares with the one shown to the owner and does not record); a file the record does not
        list is not a signed file, whatever it says. Flagged once, when a row first rests on it."""
        if not verified:
            return False
        if rel not in bound:
            bound[rel] = checks.bound(spec["so"], rel, run.sha(rel))
            if bound[rel] == "not_recorded":
                flags.append(("problem", f"The sign-off record ({spec['signoff']}) does not bind {rel}: it holds no "
                                         "hash for it, so nothing shows it is the file that was signed, and its rows "
                                         "are shown open."))
            elif bound[rel] != "same":
                flags.append(("problem", f"{rel} is not the file that was signed (against the hash in {spec['signoff']}: "
                                         f"{bound[rel].replace('_', ' ')}), so its rows are shown open."))
        return bound[rel] == "same"

    # 1. findings.jsonl, and the two files in which an older run keeps dispositions
    def finding(n, raw):
        try:
            return n, json.loads(raw.decode("utf-8"))
        except ValueError:          # not JSON, or not UTF-8
            return n, None
    found, why = run.lines(LEDGER, finding) if present[LEDGER] else ([], None)      # the whole file, whatever its size
    if found is None:
        flags.append(("not_available", f"{why}, so no finding is in the register."))
    gates = _gates(run, flags) if present[LEDGER] else []
    for n, f in found or []:
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
        disposition, shown = (LEDGER_WORDS[word] if known else None), None
        if disposition == "implemented":
            proven, shown = _fixed(f, gates)
            disposition = disposition if proven else UNVERIFIED
        r = _place(rows, _row(f["id"], "issue", f.get("claim"), "findings", stage, disposition,
                              LEDGER, [dt.get("file")] if isinstance(dt, dict) else [], severity=f.get("severity"),
                              blocking=f.get("blocking") if isinstance(f.get("blocking"), bool) else None,
                              nature="science" if critic else "process",
                              signed_by="owner" if word == "owner_accepted" else None, ledger=word, evidence=shown))
        by_id[r["id"]] = r
    older = [(OLD_DECLARED, "declared", lambda d: [x.get("id") for x in _entries(flags, OLD_DECLARED, d, "declared")] if isinstance(d.get("declared"), list) else None),
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
    for d in _entries(flags, SCOPE, doc or {}, "declarations"):
        rid = _text(d.get("blocker_id") or d.get("id"))
        if not rid:
            flags.append(("problem", f"A declaration in {SCOPE} has no blocker_id and no id."))
            continue
        if rid not in by_id:
            flags.append(("problem", f"{SCOPE} declares {rid}, which {LEDGER} does not hold: a declaration that "
                                     "names no finding is a declaration of nothing."))
        # a declaration that names a finding (blocker_id) is matched by that id alone, before and instead of any test
        _place(rows, _row(rid, "issue", d.get("why_unresolved"), "scope_declaration", "GATE", "declared", SCOPE,
                          [d.get("decision_test")]), by_id_only=bool(_text(d.get("blocker_id"))))

    # 3. engine_choice.json engine_departures
    doc = doc_of(CHOICE)
    if doc is not None and not isinstance(doc.get("engine_departures", []), list):
        flags.append(("not_available", f"{CHOICE} engine_departures is not a list, so the departures are not in the register."))
    def identity(d):       # a departure is {engine, benchmark | component, reason, decision_test} (benchmark_coverage.py)
        return _text(d.get("engine")), "component" if d.get("component") else "benchmark", _text(d.get("component") or d.get("benchmark"))
    held, refusal = {}, {}
    granting = {"verified": verified, "why": spec["unverified"], "signed_by": spec["signed_by"], "doc": spec["doc"]}
    for s in _entries(flags, spec["rel"], spec["doc"], "engine_departures"):
        held.setdefault(identity(s), []).append(_stem(s.get("decision_test")))
        # checks.departure_refused: the one rule on what grants a departure, shared with the Model view
        refusal[identity(s), _stem(s.get("decision_test"))] = checks.departure_refused(
            granting, identity(s)[1], identity(s)[2], _text(s.get("decision_test")))
    for d in _entries(flags, CHOICE, doc or {}, "engine_departures"):
        name = _text(d.get("component") or d.get("benchmark"))
        word = d.get("status")
        if not name:
            flags.append(("problem", f"An engine departure in {CHOICE} names no component and no benchmark."))
            continue
        disposition = DEPARTURE_WORDS.get(word) if isinstance(word, str) else None
        if disposition is None:
            flags.append(("not_available", f"Engine departure {name} in {CHOICE} has the status {word!r}, a word the "
                                           "engine choice does not write, so its disposition is not known."))
        elif disposition == "declared" and verified and identity(d) not in held:
            disposition = "open"
            flags.append(("problem", f"{CHOICE} marks the engine departure for {name} signed, but the signed spec's "
                                     f"engine_departures does not hold it (engine {identity(d)[0] or 'not named'}, "
                                     f"{identity(d)[1]} {name}): the two files disagree, so it is shown open."))
        elif disposition == "declared" and verified and _stem(d.get("decision_test")) not in held[identity(d)]:
            disposition = "open"
            flags.append(("problem", f"{CHOICE} gives the engine departure for {name} the decision test "
                                     f"{_stem(d.get('decision_test')) or '(none)'}, but the signed entry names "
                                     f"{', '.join(t or '(none)' for t in held[identity(d)])}: the signature does not cover "
                                     "that test, so the departure is shown open."))
        elif disposition == "declared" and verified and (refusal[identity(d), _stem(d.get("decision_test"))] or ("no_test",))[0] != "no_test":
            disposition = "open"       # a departure with no test stays a declared row with no test (declared_untested)
            flags.append(("problem", f"{CHOICE} marks the engine departure for {name} signed, but "
                                     f"{refusal[identity(d), _stem(d.get('decision_test'))][1]}: it is shown open."))
        elif disposition == "declared" and not verified:
            disposition = "open"
        _place(rows, _row(f"departure:{name}", "assumption", d.get("reason"), "engine_departures", "ENGINE_CHOICE",
                          disposition, CHOICE, [d.get("decision_test")],
                          signed_by=spec["signed_by"] if disposition == "declared" else None))

    # 4, 5. the signed spec: declared calibration misses and excluded inputs
    def carried(word):
        return (word, spec["signed_by"]) if verified else ("open", None)
    for d in _entries(flags, spec["rel"], spec["doc"], "calibration", "joint", "unmatched", "declared"):
        name = _text(d.get("target") or d.get("offset"))
        if name:
            word, who = carried("tested" if d.get("decision_test") else "declared")
            _place(rows, _row(f"calibration:{name}", "issue", d.get("reason"), "calibration_declared", "SIGN", word,
                              spec["rel"], [d.get("decision_test")], signed_by=who))
    for d in _entries(flags, spec["rel"], spec["doc"], "uncertainty", "excluded"):
        name = _text(d.get("parameter") or d.get("kind"))
        if name:
            word, who = carried("declared")
            _place(rows, _row(f"excluded:{name}", "assumption", d.get("reason"), "uncertainty_excluded", "SIGN", word,
                              spec["rel"], signed_by=who))

    # 6. the sign-off sheet's rows marked HIGH
    def high(n, raw):
        m = HIGH_LINE.match(raw.decode("utf-8", "replace"))
        return (n, m) if m else None
    marked, why = run.lines(SHEET, high) if present[SHEET] else ([], None)          # the whole file, whatever its size
    if marked is None:
        flags.append(("not_available", f"{why}, so the rows it marks HIGH are not in the register."))
    for n, m in marked or []:
        name = next((x.group(1) for x in (p.match(m.group(1)) for p in HIGH_NAME) if x), None)
        if name is None:
            flags.append(("not_available", f"Line {n} of {SHEET} marks a finding HIGH in a form that is not "
                                           "recognised, so the input it is about is not known."))
        word, who = carried("declared") if signed_file(SHEET) else ("open", None)
        _place(rows, _row(f"presign:{name or f'line {n}'}", "issue", m.group(1), "signoff_sheet", "SIGN", word, SHEET,
                          signed_by=who, severity="HIGH"))

    # 7. evidence_dossier.yaml entries with no data
    doc = doc_of(DOSSIER)
    if doc is not None and not isinstance(doc.get("inputs"), dict):
        flags.append(("not_available", f"{DOSSIER} holds no inputs mapping, so its entries are not in the register."))
    inputs = doc.get("inputs") if doc is not None and isinstance(doc.get("inputs"), dict) else {}
    for key, e in inputs.items():
        if isinstance(e, dict) and e.get("no_data"):
            nd, test = e["no_data"], e.get("decision_test")
            test = test if isinstance(test, str) else None      # only the structured key names a test
            _place(rows, _row(f"nodata:{key}", "uncertainty", nd.get("reason") if isinstance(nd, dict) else nd,
                              "evidence_dossier", "SIGN", "tested" if test and signed_file(DOSSIER) else "open", DOSSIER, [test]))

    # 8. plan-changing decision tests that no row names
    linked = {t for r in rows for t in r["tests"]}
    asks = {str(t.get("id")): _text(t.get("text")) for t in _list(spec["doc"], "analysis", "decision_tests")}    # the spec's own question
    out["asks"] = asks
    for tid, t in (tests or {}).items():
        if t["state"] == "ran" and t["verdict"] == CHANGES and tid not in linked:
            said = " ".join(x for x in (asks.get(tid), t["statement"], UNLINKED) if x)
            _place(rows, _row(f"test:{tid}", "uncertainty", said, "decision_tests", "ANALYZE", "open", DT, [tid]))

    for r in rows:
        r["derived"] = r["disposition"]
    _notes(run, out, present[NOTES])
    for r in rows:
        r["verdicts"] = [_verdict(tests, t) for t in r["tests"]]
        if not r["decided"]:        # only a test that ran says anything: a verdict beside a status that denies it does not
            said = [v if _ran(tests, t) else None for t, v in zip(r["tests"], r["verdicts"])]
            r["decides"] = "yes" if CHANGES in said else "no" if said and all(v == NO_CHANGE for v in said) else "unknown"
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
                r.update(disposition=note["disposition"], noted=True, signed_by="owner",
                         evidence=f"the owner's note of {str(note['at'])[:10]} ({NOTES})")
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
    dec = "declared"        # §9: the clean run holds nothing declared; this variant holds one of each declaration

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
            for rel in (fx.SHEET, fx.DOSSIER):      # the sheet's hash: one no producer records today (the rule is kept
                with open(os.path.join(d, rel), "rb") as f:     # for a record that does; no fixture is built with it)
                    so["sha256"][rel] = hashlib.sha256(f.read()).hexdigest()
            with open(os.path.join(d, SIGNOFF), "w", encoding="utf-8") as f:
                json.dump(so, f)
        m = look(b.folder(hm, "sheet_high"))
        need(by(m)["presign:p2"]["disposition"] == "open" and by(m)["nodata:unit_attribute:x1"]["disposition"] == "tested",
             f"as the producer signs: the dossier is a signed file, the sheet is not ({m['flags']})")
        m = look(b.edited(hm, listed, "sheet_high"))
        need(m["flags"] == [] and by(m)["presign:p2"]["disposition"] == "declared", f"signed files that still have their hash count ({m['flags']})")
        for rel, rid in ((fx.SHEET, "presign:p2"), (fx.DOSSIER, "nodata:unit_attribute:x1")):
            m = look(b.edited(hm, lambda d, rel=rel: (listed(d), text(rel, lambda t: t + "\n")(d)), "sheet_high"))
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
        m = look(b.edited(hm, unheld, dec))
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

    def rejson(rel, change):
        def edit(d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                doc = json.load(f)
            change(doc)
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                json.dump(doc, f)
        return edit

    def problem(m, *words):
        return any(s == "problem" and all(w in why for w in words) for s, why in m["flags"])

    def departure_identity():
        def to(**change):
            return rejson(fx.CHOICE, lambda doc: doc["engine_departures"][0].update(change))
        for name, edit in (("another engine and another test", to(engine="other-engine", decision_test="DT1")),
                           ("another test", to(decision_test="DT1")), ("another engine", to(engine="other-engine")),
                           ("a component of the benchmark's name", lambda d: rejson(fx.CHOICE, lambda doc: doc["engine_departures"][0].update(
                               component=doc["engine_departures"][0].pop("benchmark")))(d))):
            m = look(b.edited(hm, edit, dec))
            need(by(m)["departure:B2"]["disposition"] == "open" and by(m)["departure:B2"]["signed_by"] is None and problem(m, "B2"),
                 f"{name}: the departure is open and the mismatch is a problem ({by(m)['departure:B2']['disposition']}, {m['flags']})")
            need(by(m)["departure:B"]["disposition"] == "declared", "the other departure, which matches its signed entry, still counts")

    def malformed_entries():
        from views.fit_bench import rewrite
        m = look(b.edited(hm, rewrite(fx.SCOPE, lambda doc: doc["declarations"].append(None)), dec))
        need(problem(m, fx.SCOPE, "Entry 2"), f"a declaration that is no mapping is a problem, named by its position ({m['flags']})")
        m = look(b.edited(hm, rejson(fx.CHOICE, lambda doc: doc["engine_departures"].insert(0, "B2")), dec))
        need(problem(m, fx.CHOICE, "Entry 1") and by(m)["departure:B2"]["disposition"] == "declared",
             f"so is an engine departure that is no mapping; the entries that are mappings are still read ({m['flags']})")

    def whole_files():
        def pad_to(size, d):       # a valid row that ends the ledger exactly at `size` bytes: the reviewer's valid prefix
            row = {"id": "PAD-1", "status": "fixed", "source": "rigor-artifacts", "severity": "LOW", "claim": ""}
            row["claim"] = "x" * (size - os.path.getsize(os.path.join(d, fx.LEDGER)) - len(json.dumps(row)) - 1)
            return json.dumps(row)
        late = json.dumps({"id": "Z-777", "status": "open", "source": "critique-domain", "severity": "HIGH", "blocking": True,
                           "claim": "a finding written after eight megabytes of ledger"})
        m = look(b.edited(hm, lambda d: text(fx.LEDGER, lambda t: t + pad_to(MAX_TEXT, d) + "\n" + late + "\n")(d)))
        need("Z-777" in by(m) and by(m)["Z-777"]["disposition"] == "open" and by(m)["Z-777"]["blocking"] is True,
             f"a finding beyond 8 MiB of ledger is a row ({[k for k in by(m)][:8]}, {m['flags']})")
        filler = "".join(f"- line {i} of a long sheet\n" for i in range(400000))
        high = "- **HIGH**: q9 = 7 is outside the card range. Signing accepts it.\n"
        m = look(b.edited(hm, text(fx.SHEET, lambda t: t + "\n" + filler + high)))
        need("presign:q9" in by(m), f"a HIGH line beyond 8 MiB of sheet is a row ({[k for k in by(m)]})")

    def unbound_sheet():
        m = look(b.folder(hm))
        need(m["flags"] == [] and not [i for i in by(m) if i.startswith("presign:")],
             f"a sheet that marks no row HIGH raises nothing, though the record does not bind it ({m['flags']})")
        second = text(fx.SHEET, lambda t: t + "\n- **HIGH**: q9 = 7 is outside the card range. Signing accepts it.\n")
        m = look(b.edited(hm, second, "sheet_high"))
        for rid in ("presign:q9", "presign:p2"):
            need(by(m)[rid]["disposition"] == "open" and by(m)[rid]["signed_by"] is None,
                 f"{rid}: a sheet line the sign-off record does not bind is not declared by signing ({by(m)[rid]})")
        need(problem(m, fx.SHEET, "does not bind") and problem(m, fx.SHEET, "no hash")
             and sum(fx.SHEET in why for _, why in m["flags"]) == 1, f"and the missing binding is a problem, said once ({m['flags']})")
        m = look(b.edited(hm, rejson(SIGNOFF, lambda so: so["sha256"].pop(fx.DOSSIER, None))))
        need(by(m)["nodata:unit_attribute:x1"]["disposition"] == "open" and problem(m, fx.DOSSIER, "no hash"),
             f"nor is a dossier entry the record does not bind tested ({m['flags']})")

    def brief_record():
        brief = "decision_brief.signoff.json"

        def moved(d):
            os.rename(os.path.join(d, SIGNOFF), os.path.join(d, brief))
        m = look(b.edited(hm, moved))
        need(m["flags"] == [] and m["prose"] == [] and by(m)["excluded:c1"]["disposition"] == "declared"
             and by(m)["excluded:c1"]["signed_by"] == "owner" and brief in m["spec"]["say"],
             f"the brief's record, when it holds the spec's hash, is the sign-off (toolkit/spec/signoff.py _spec_record) ({m['flags']})")

        def both(d, spec_in_brief):
            with open(os.path.join(d, SIGNOFF), encoding="utf-8") as f:
                so = json.load(f)
            other = {**so, "signed_by": "lead", "sha256": {k: v for k, v in so["sha256"].items() if spec_in_brief or k != SPEC}}
            with open(os.path.join(d, brief), "w", encoding="utf-8") as f:
                json.dump(other, f)
        m = look(b.edited(hm, lambda d: both(d, True)))
        need(by(m)["excluded:c1"]["signed_by"] == "lead", "with both records, the brief's is selected when it holds the spec's hash")
        m = look(b.edited(hm, lambda d: both(d, False)))
        need(by(m)["excluded:c1"]["signed_by"] == "owner" and m["flags"] == [], "and the spec-only record when the brief's does not")

    def explicit_id():
        def shared(d):
            with open(os.path.join(d, fx.LEDGER), encoding="utf-8") as f:
                found = [json.loads(x) for x in f if x.strip()]
            first = next(r for r in found if r["id"] == "D-001")
            first.update(status="declared", decision_test={"file": "models/decision_tests/DT3.yaml", "finding": "D-001", "valid": True})
            found.sort(key=lambda r: r["id"] != "D-001")
            with open(os.path.join(d, fx.LEDGER), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(r) + "\n" for r in found))
        m = look(b.edited(hm, shared, dec))
        need("scope_declaration" in by(m)["R-001"]["sources"] and "scope_declaration" not in by(m)["D-001"]["sources"],
             f"a declaration that names R-001 goes to R-001, though D-001 comes first and shares its test "
             f"({by(m)['D-001']['sources']}, {by(m)['R-001']['sources']})")
        from views.fit_bench import rewrite
        m = look(b.edited(hm, rewrite(fx.SCOPE, lambda doc: doc["declarations"].append(
            {"id": "SD-002", "blocker_id": "X-404", "why_unresolved": "w", "decision_test": "models/decision_tests/DT3.yaml"})), dec))
        need("X-404" in by(m) and by(m)["R-001"]["sources"] == ["findings", "scope_declaration"],
             "a declaration that names a finding the ledger does not hold keeps its own row, whatever test it shares")

    def contradictory_test():
        from views.fit_bench import rewrite

        def odd(doc):
            next(t for t in doc["data"]["tests"] if t["test_id"] == "DT4")["verdict"] = "NOT_RUN"
        m = look(b.edited(hm, rewrite(fx.DT, odd), dec))
        need(m["tests"]["DT4"]["state"] == "contradiction" and by(m)["departure:B2"]["decides"] == "unknown",
             f"status run with verdict NOT_RUN contradict each other: the test did not establish anything ({m['tests'].get('DT4')})")

        def other(doc):
            next(t for t in doc["data"]["tests"] if t["test_id"] == "DT4").update(status="not_run", verdict="NO_CHANGE")
        m = look(b.edited(hm, rewrite(fx.DT, other), dec))
        need(m["tests"]["DT4"]["state"] == "contradiction" and by(m)["departure:B2"]["decides"] == "unknown",
             "nor does status not_run with a verdict of no change")
        need(look(b.folder(hm))["tests"]["DT4"]["state"] == "ran" and look(b.folder(hm, "declared_test_not_run"))["tests"]["DT4"]["state"] == "not_run",
             "the producer's two consistent pairs are ran and not_run")

    def departure_grant():
        from views.fit_bench import rewrite
        m = look(b.edited(hm, rejson("decision_spec.signoff.json", lambda so: so.update(signed_by="lead")), dec))
        for rid in ("departure:B2", "departure:B"):
            need(by(m)[rid]["disposition"] == "open" and by(m)[rid]["signed_by"] is None and problem(m, rid.split(":")[1], "owner"),
                 f"{rid}: a departure in a spec a lead signed is open, whatever the choice record says ({by(m)[rid]['disposition']}, {m['flags']})")
        need(by(m)["excluded:c1"]["disposition"] == "declared" and by(m)["excluded:c1"]["signed_by"] == "lead",
             "a row that needs no owner's signature keeps its disposition, with who signed")
        for what, change in (("another spec's hash", lambda doc: doc.update(spec_sha256="1" * 64)), ("no spec hash", lambda doc: doc.pop("spec_sha256"))):
            m = look(b.edited(hm, rewrite(fx.DT, change), dec))
            need(m["tests"]["DT4"]["state"] == "ran" and not m["tests"]["DT4"]["counts"] and by(m)["departure:B2"]["decides"] == "unknown",
                 f"{what}: a test of another spec says nothing of what a declared row decides ({by(m)['departure:B2']['decides']})")

        def kind(d):
            with open(os.path.join(d, checks.SIGNED_COPY), encoding="utf-8") as f:
                was = f.read()
            now = was.replace("type: interaction_rescale", "type: outcome_ratio")
            assert now != was
            with open(os.path.join(d, checks.SIGNED_COPY), "w", encoding="utf-8") as f:
                f.write(now)
            rejson("decision_spec.signoff.json", lambda so: so["sha256"].update({SPEC: hashlib.sha256(now.encode()).hexdigest()}))(d)
        m = look(b.edited(hm, kind, dec))
        need(by(m)["departure:B2"]["disposition"] == "open" and problem(m, "B2", "outcome_ratio") and by(m)["departure:B"]["disposition"] == "declared",
             f"a departure whose test is of a kind that does not vary the effect is open, as in the Model view ({by(m)['departure:B2']['disposition']}, {m['flags']})")

    b.case("RR13 an engine departure is granted by the one rule the Model view uses: the owner's signature and a test that varies the effect (review 4)", departure_grant)
    b.case("RR6 an engine departure counts only against the signed entry of the same engine, typed name and test (review 2)", departure_identity)
    b.case("RR7 a list entry that is no mapping is a problem named by its position, never dropped (review 5)", malformed_entries)
    b.case("RR8 the ledger and the sheet are read whole: a row beyond 8 MiB is in the register (review 6)", whole_files)
    b.case("RR9 a sheet or dossier the sign-off record holds no hash for is not signed: its rows are open (review 7)", unbound_sheet)
    b.case("RR10 the sign-off record is selected as the producer selects it: the brief's when it holds the spec's hash (review 8)", brief_record)
    b.case("RR11 a declaration goes to the finding it names before any row that shares its test (review 9)", explicit_id)
    b.case("RR12 a decision test whose status and verdict contradict each other did not run (review 3)", contradictory_test)
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
