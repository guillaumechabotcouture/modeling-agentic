#!/usr/bin/env python3
"""The Steps checks of the review workbench, over one run directory (docs/design/review-workbench.md §3, §5.2).

One function per check. Each returns a dict (built by `result`, which any view may use):
    id, step (the view it belongs to), title, status (ok | look | problem | not_available),
    says (one plain sentence), evidence ([{file, field}]), rows (optional table: {columns, rows}),
    case (scorecard id or None)
A check never raises on bad input: a file it cannot read or recognise gives `not_available` with the reason.
Imported by workbench.py and views/steps.py. `python3 tools/workbench/checks.py --self-test` runs the cases of the
outcome check; the other checks' cases are in workbench.py --self-test (W7 to W11, W24).
"""
from __future__ import annotations

import re

STATUSES = ("ok", "look", "problem", "not_available")
ACCEPTED = ("completed", "completed_with_report_restored")
DECLARED = ("completed_scope_declared",)     # §3: ended with declared limits; the reader judges, so `look`
WORST_FIRST = ("problem", "look", "not_available", "ok")


def worst(statuses):
    """The most serious of some statuses, or None when there are none."""
    return next((s for s in WORST_FIRST if s in statuses), None)


def _r(cid, step, title, status, says, evidence=(), rows=None, case=None):
    assert status in STATUSES, status
    return {"id": cid, "step": step, "title": title, "status": status, "says": says,
            "evidence": [{"file": f, "field": k} for f, k in evidence], "rows": rows, "case": case}


result = _r


def by_stage(history):
    """Rows of (stage, visits, model spend, working minutes, minutes waiting for the owner), in order of first visit.
    A transition's time and spend belong to the stage it leaves."""
    by = {}
    for h in history:
        s = by.setdefault(h["from"], [0, 0.0, 0.0, 0.0])
        s[0] += 1
        s[1] += h["usd"] or 0.0
        s[3 if h["from"] in OWNER_STAGES else 2] += h["minutes"] or 0.0
    return [[k, v[0], round(v[1], 2), None if k in OWNER_STAGES else round(v[2], 1),
             round(v[3], 1) if k in OWNER_STAGES else None] for k, v in by.items()]


OWNER_STAGES = ("SIGN", "OWNER_DECISION")    # the stage machine stops here until the owner acts
STAGE_COLUMNS = ["stage", "visits", "model_spend_usd", "working_minutes", "waiting_for_owner_minutes"]


def _short(sha):
    return str(sha or "")[:10] or "none"


def digest(x):
    """x when it is a sha256 in hex, else None: anything else recorded as a hash is no hash."""
    return x if isinstance(x, str) and re.fullmatch(r"[0-9a-f]{64}", x) else None


def hash_state(recorded, now):
    """A file against the hash recorded for it: missing (the file cannot be read), not_recorded (no valid hash was
    recorded), changed or same. The one rule for every comparison of hashes here: a missing file or a missing or
    invalid recorded hash is never a match."""
    if now is None:
        return "missing"
    if digest(recorded) is None:
        return "not_recorded"
    return "same" if now == recorded else "changed"


STALE = ("changed", "missing", "not_recorded")       # the states of a recorded input that is not known to be the same
SPEC, SPEC_SIGNOFF, BRIEF_SIGNOFF = "decision_spec.yaml", "decision_spec.signoff.json", "decision_brief.signoff.json"
SIGNED_COPY = "decision_brief.signed/decision_spec.yaml"
DT = "analysis/decision_tests.yaml"
# toolkit/analysis/decision_tests.py writes status "run" or "not_run"; toolkit/analysis/run.py derives the verdict from
# it: NOT_RUN when the status is not_run, else CHANGES_PLAN or NO_CHANGE. Any other pair is no pair the producer writes.
TEST_PAIRS = {("run", "NO_CHANGE"): "ran", ("run", "CHANGES_PLAN"): "ran", ("not_run", "NOT_RUN"): "not_run"}
TEST_STATUSES, VERDICTS = ("run", "not_run"), ("NO_CHANGE", "CHANGES_PLAN", "NOT_RUN")
# A test's own numbers, each in percent beside the threshold the producer compares it with (toolkit/analysis/moves.py):
# the share of units that change package, the regret of keeping the point plan, and the share of the budget moved
# (dollars_share_pct is the one compared with dollars_threshold_pct).
TEST_NUMBERS = ("share_pct", "threshold_pct", "regret_pct", "regret_threshold_pct", "dollars_share_pct", "dollars_threshold_pct")
EST = "analysis/estimates.yaml"
EST_VERDICTS = ("yes", "no", "undetermined")       # toolkit/analysis/estimates.py: a decision rule's verdicts
# The producer's rule for a verdict, recomputed from the test's own recorded numbers (§3: a record that contradicts
# itself). toolkit/analysis/moves.py: `rec["triggered_by"] = (["infeasible"] if why else []) + [k for k in CRITERIA if
# fires(rec, k, thr[k])]`, `rec["decision_changes"] = bool(rec["triggered_by"])`, and fires() is strict: `return
# rec["share_units_changed"] > t`, `rec["dollars_share"] > t`, `rec["regret_share"] > t`. The recorded numbers are each
# criterion's worst over the test's values (aggregate()), so a recorded number above its threshold is a value that fired.
CRITERIA = (("share", "share_pct", "threshold_pct"), ("regret", "regret_pct", "regret_threshold_pct"),
            ("dollars", "dollars_share_pct", "dollars_threshold_pct"))
EDGE = 1e-9         # a number is above its threshold when it is so by more than rounding (the file holds 100 x the share)


def _number(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def test_clash(t):
    """Why a decision test's recorded verdict contradicts its own recorded numbers, or None. NO_CHANGE is contradicted
    by a criterion whose number is strictly above its threshold, by a point plan recorded as not feasible, and by a
    non-empty triggered_by. CHANGES_PLAN is contradicted by an empty triggered_by, and by a feasible point plan with
    all three numbers recorded and none above its threshold (with no regret share recorded the producer judges regret
    on a number the file does not hold, so nothing is said)."""
    verdict, by = t.get("verdict"), t.get("triggered_by")
    pairs = [(k, _number(t.get(v)), _number(t.get(th))) for k, v, th in CRITERIA]
    fired = [f"{k} {v:g}% is above its threshold {th:g}%" for k, v, th in pairs if v is not None and th is not None and v > th * (1 + EDGE)]
    if t.get("point_plan_feasible") is False:
        fired.insert(0, "the point plan is recorded as not feasible")
    if isinstance(by, list) and by:
        fired.append(f"triggered_by names {', '.join(map(str, by))}")
    if verdict == "NO_CHANGE" and fired:
        return f"the verdict is NO_CHANGE and {'; '.join(fired)}"
    if verdict == "CHANGES_PLAN" and not fired:
        if isinstance(by, list):
            return "the verdict is CHANGES_PLAN and triggered_by names no criterion"
        if t.get("point_plan_feasible") is True and all(v is not None and th is not None for _, v, th in pairs):
            return "the verdict is CHANGES_PLAN, the point plan is feasible and no recorded number is above its threshold"
    return None


def signoff_record(run):
    """(the sign-off record that covers the spec, the file it is in, why there is none). The record is selected as
    the producer selects it (toolkit/spec/signoff.py _spec_record): the brief's record when it holds a hash for the
    spec, else the spec-only record. Every view that asks which spec was signed, by whom and with which files asks
    here, so no two views can answer from different records."""
    brief, brief_why = run.json(BRIEF_SIGNOFF)
    if isinstance(brief, dict) and isinstance(brief.get("sha256"), dict) and brief["sha256"].get(SPEC) is not None:
        return brief, BRIEF_SIGNOFF, None
    so, why = run.json(SPEC_SIGNOFF)
    if so is None and brief is None and not str(brief_why).endswith("is not in the run folder"):
        why = f"{why}; {brief_why}"
    return so, SPEC_SIGNOFF, why


# The files a sign-off records null for when they did not exist at signing, and takes as unchanged while they stay
# absent. toolkit/spec/signoff.py: sign() records `extra = {DERIVATIONS_RECORD: dsha, ...}` with dsha None for an absent
# file, and derived_changed() answers `if signed is None and now is None: return []`. scripts/decision_brief.py: sign()
# records `hashlib.sha256(b).hexdigest() if b is not None else None` for each frozen file and status() compares that
# with the file's hash now (None when absent). No other file is on the list: the spec must exist to be signed, a
# reference file with no recorded hash is changed (signoff.py burden_changed), and so is a missing dossier
# (toolkit/spec/evidence_dossier.py state: `or data is None`).
ABSENT_WHEN_SIGNED = ("data/derivations_record.json", "decision_brief.yaml", "success_criteria.yaml")


def signed_state(held, rel, now, absent):
    """A file the sign-off record lists, against what the record holds for it: hash_state's words, and two more for a
    file of ABSENT_WHEN_SIGNED recorded as null: `absent` (absent when signed, absent now: unchanged) and `appeared`
    (it exists now, or something that is no plain file is at its name). `absent`: the file is not in the run folder.
    A key the record does not hold, or a hash that is no digest, is never a match."""
    if rel in ABSENT_WHEN_SIGNED and rel in held and held[rel] is None:
        return "absent" if absent else "appeared" if now is not None else "missing"
    return hash_state(held.get(rel), now)


def bound(record, rel, now):
    """A file against the hash the sign-off record holds for it: hash_state's words. A file the record holds no valid
    hash for is `not_recorded`: nothing binds its bytes to the signature, so it is not a signed file."""
    held = record.get("sha256") if isinstance(record, dict) and isinstance(record.get("sha256"), dict) else {}
    return hash_state(held.get(rel), now)


def decision_tests(run, spec=None):
    """({test id: {status, verdict, statement, state, counts, spec, spec_says, feasible, share_pct, threshold_pct, ...}},
    None) from analysis/decision_tests.yaml, in the file's order, or (None, why). share_pct and threshold_pct are the
    test's own numbers (the share of units that change package, and the share above which the plan is said to change),
    with the rest of TEST_NUMBERS, null where it records none; `feasible` is its point_plan_feasible (true, false or null).
    The one loader of decision tests, for every view. state: `ran` (status run with a verdict of NO_CHANGE or
    CHANGES_PLAN: the only pair that establishes that a test ran), `not_run` (status not_run, verdict NOT_RUN),
    `contradiction` (the producer's words in a pair it does not write, or a verdict its own recorded numbers
    contradict by the producer's rule, with the reason in `clash`: the record contradicts itself), or
    `unknown` (a status or verdict word the producer does not write).
    `counts` (§3: a decision test counts only when its file records the same signed spec): true when the test ran
    and the file's header holds the hash of the verified signed spec (`spec`, verified_spec's result, read here when
    not given). Only a test that counts can excuse anything; a view that raises something a test found (a verdict
    that changes the plan) reads `state`. `spec` says how the file's spec hash stands (same, other, not_recorded,
    unverified) and `spec_says` gives the sentence when it is not the same."""
    doc, why = run.large(DT)
    if doc is None:
        return None, why
    tests = doc.get("data", {}).get("tests") if isinstance(doc.get("data"), dict) else None
    if not isinstance(tests, list) or not all(isinstance(t, dict) and t.get("test_id") for t in tests):
        return None, f"{DT} holds no list of tests (data.tests)"
    spec = spec or verified_spec(run)
    tie, tie_says = _tie(run, DT, doc, spec)
    out = {}
    for t in tests:
        status, verdict = t.get("status"), t.get("verdict")
        pair = (status, verdict) if isinstance(status, str) and isinstance(verdict, str) else None
        state = TEST_PAIRS.get(pair) or ("contradiction" if pair and status in TEST_STATUSES and verdict in VERDICTS else "unknown")
        clash = test_clash(t) if state == "ran" else None
        state = "contradiction" if clash else state
        out[str(t["test_id"])] = {"status": status, "verdict": verdict, "state": state, "clash": clash,
                                  "counts": state == "ran" and tie == "same", "spec": tie, "spec_says": tie_says,
                                  "statement": " ".join(str(t.get("statement") or "").split())[:4000],
                                  "feasible": t.get("point_plan_feasible") if isinstance(t.get("point_plan_feasible"), bool) else None,
                                  **{k: t.get(k) if isinstance(t.get(k), (int, float)) and not isinstance(t.get(k), bool) else None
                                     for k in TEST_NUMBERS}}
    return out, None


def _tie(run, rel, doc, spec):
    """How an analysis file's recorded spec hash stands against the verified signed spec: (word, sentence or None)."""
    held = digest(doc.get("spec_sha256"))
    if not spec["verified"]:
        return "unverified", f"no signed spec could be verified ({spec['why']}), so no test is tied to one"
    if held is None:
        return "not_recorded", f"{rel} records no valid hash of the spec it was run for (spec_sha256)"
    if held != spec["sha"]:
        return "other", (f"{rel} records another spec ({held[:8]}) than the verified signed copy ({spec['sha'][:8]}), so its "
                         "tests were not run for the spec that was signed")
    return "same", None


def estimates_tests(run, spec=None):
    """The decision tests of an `estimates` deliverable, which the producer keeps in analysis/estimates.yaml
    (data.decision_tests), in the shape and by the rules of decision_tests above: ({test id: {status, verdict, state,
    counts, spec, spec_says, statement, parameter, ...}}, None) or (None, why). The producer's words
    (toolkit/analysis/estimates.py) are mapped explicitly: status `run` with `decision_changes` true is the verdict
    CHANGES_PLAN and with false NO_CHANGE; `not_assessable` did not run (NOT_RUN); any other status is `unknown`.
    A test ran only by the producer's own rule, recomputed here: `kept = [e for e in res["ends"] if not
    e["rejected_by_data"]]`, `res["decision_changes"] = any(e["verdict"] != decision["verdict"] for e in kept)`. So it
    needs the decision's verdict (data.decision.verdict), at least one end the data do not reject, and one of the
    rule's verdicts (EST_VERDICTS) at each such end: without them it is `unknown` (it ran with no verdict). A recorded
    decision_changes that those ends contradict is a `contradiction`, with the reason in `clash`. `status` stays the
    producer's own word."""
    doc, why = run.large(EST)
    if doc is None:
        return None, why
    tests = doc.get("data", {}).get("decision_tests") if isinstance(doc.get("data"), dict) else None
    if not isinstance(tests, list) or not all(isinstance(t, dict) and t.get("id") for t in tests):
        return None, f"{EST} holds no list of tests (data.decision_tests)"
    spec = spec or verified_spec(run)
    tie, tie_says = _tie(run, EST, doc, spec)
    out = {}
    rule = doc["data"]["decision"].get("verdict") if isinstance(doc["data"].get("decision"), dict) else None
    for t in tests:
        status, changes, clash = t.get("status"), t.get("decision_changes"), None
        listed = t.get("ends") if isinstance(t.get("ends"), list) else []
        ends = [e for e in listed if isinstance(e, dict) and e.get("rejected_by_data") is False]
        real = (isinstance(changes, bool) and rule in EST_VERDICTS and bool(ends) and all(isinstance(e, dict) for e in listed)
                and all(e.get("verdict") in EST_VERDICTS for e in ends))
        if status == "run" and real:
            state, verdict = "ran", "CHANGES_PLAN" if changes else "NO_CHANGE"
            other = sorted({e["verdict"] for e in ends if e["verdict"] != rule})
            if bool(other) != changes:
                state = "contradiction"
                clash = (f"decision_changes is {str(changes).lower()} and " + (f"an end the data do not reject has the verdict {', '.join(other)}" if other
                         else "every end the data do not reject has the decision's verdict") + f" (the decision's verdict is {rule})")
        elif status == "not_assessable":
            state, verdict = "not_run", "NOT_RUN"
        else:
            state, verdict = "unknown", None
        out[str(t["id"])] = {"status": status, "verdict": verdict, "state": state, "clash": clash, "counts": state == "ran" and tie == "same",
                             "spec": tie, "spec_says": tie_says, "statement": "", "feasible": None,
                             "parameter": t.get("parameter") if isinstance(t.get("parameter"), str) else None,
                             **{k: None for k in TEST_NUMBERS}}
    return out, None


# What grants an engine departure, in the producer's words (toolkit/engines/benchmark_coverage.py): "A departure:
# {engine, benchmark | component, reason, decision_test}. It counts only when the spec is signed by the owner (the
# sign-off record's signed_by), and only when its decision test varies the related effect: a parameter_value test on
# one of the entry's effect_parameters, or an interaction_rescale test naming a spec component that delivers the
# covered engine component." departure_problems() there: "decision_test ... must be an analysis.decision_tests id".
# toolkit/spec/preconditions.py decision_test_runs(): an interaction test whose benchmark is not the departed one
# "tests benchmark ...'s interaction, not the departed", and any other type "does not vary a component's effect".
DEPARTURE_TESTS = ("parameter_value", "interaction_rescale")


def departure_refused(spec, kind, subject, test):
    """Why the signed spec `spec` (verified_spec's shape: verified, why, signed_by, doc) does not grant its own engine
    departure for the `kind` (benchmark or component) `subject` with the decision test `test`: (word, sentence), or
    None when it does. The one rule for every view that lets a departure excuse something (§3), read from the
    verified signed spec and its sign-off record alone, never from the engine choice. Words: unverified, not_owner,
    no_test, bad_test. Not checked here: which engine parameters carry the effect (the engine's coverage file, which
    is not in the run folder), and whether the test ran (decision_tests' `counts`)."""
    if not spec["verified"]:
        return "unverified", f"no signed spec could be verified ({spec['why']})"
    if spec["signed_by"] != "owner":
        return "not_owner", (f"the spec that holds it is signed by {spec['signed_by']!r}, not by the owner, and a departure counts only "
                             "under the owner's signature, so it grants nothing")
    if not test:
        return "no_test", "it names no decision test"
    held = spec["doc"].get("analysis")
    held = held.get("decision_tests") if isinstance(held, dict) else None
    dt = next((t for t in held if isinstance(t, dict) and str(t.get("id")) == str(test)), None) if isinstance(held, list) else None
    if dt is None:
        return "bad_test", f"the verified signed spec declares no decision test {test} (analysis.decision_tests)"
    if dt.get("type") not in DEPARTURE_TESTS:
        return "bad_test", (f"its test {test} is a {dt.get('type')} test, and a departure is carried only by a test that varies the "
                            f"effect ({' or '.join(DEPARTURE_TESTS)})")
    if dt["type"] == "interaction_rescale" and (kind != "benchmark" or str(dt.get("benchmark")) != str(subject)):
        return "bad_test", (f"its test {test} rescales the interaction of benchmark {dt.get('benchmark')}, which is not the departed "
                            f"{kind} {subject}")
    return None


FIGURES = "analysis/figures.yaml"


def harness_figures(run):
    """{figure name: {figure, path, sha256, title, caption, tier, group}} for the figures the run's figure index lists whose image
    can be read, each with the hash of the image as read, or None when the run has no readable index. For a view that
    shows a harness figure beside its own chart or table (§5.12); the Figures view reads the index in full itself."""
    doc, _ = run.large(FIGURES)
    figs = doc["data"].get("figures") if doc is not None and isinstance(doc.get("data"), dict) else None
    if not isinstance(figs, list):
        return None
    out = {}
    for e in figs:
        name, rel = (e.get("figure") or e.get("id"), e.get("path")) if isinstance(e, dict) else (None, None)
        if isinstance(name, str) and isinstance(rel, str) and name not in out:
            sha = run.sha(rel)
            if sha is not None:
                out[name] = {"figure": name, "path": rel, "sha256": sha, **{k: " ".join(e[k].split())[:2000] if isinstance(e.get(k), str) and e[k].strip() else None
                                                                           for k in ("title", "caption", "tier", "group")}}
    return out


def verified_spec(run):
    """The signed copy of the spec and whether it is verified (§3): {doc, sha, verified, why, signed_by, signed_at,
    record}. One loader for every view that lets a signed declaration or allowance excuse something. `verified` is
    true only when the signed copy can be read and parsed and its sha256 is the one the sign-off record (the record
    signoff_record selects) holds for the spec; the hash and the document come from one read (Run.hashed), so a hash
    that verifies speaks for the document returned. With no verified copy `doc` is None and `why` says what is
    missing: nothing in a spec that is not verified can excuse anything."""
    sha, doc, why = run.hashed(SIGNED_COPY)
    so, so_rel, so_why = signoff_record(run)
    held = so.get("sha256") if isinstance(so, dict) and isinstance(so.get("sha256"), dict) else {}
    want = digest(held.get(SPEC))
    if sha is None:
        bad = f"the run has no readable signed copy of the spec ({why})"
    elif so is None:
        bad = f"the sign-off record could not be read ({so_why})"
    elif want is None:
        bad = f"the sign-off record ({so_rel}) holds no sha256 for {SPEC}"
    elif want != sha:
        bad = f"the signed copy ({SIGNED_COPY}, {sha[:8]}) differs from the hash the sign-off record holds ({want[:8]})"
    elif not isinstance(doc, dict):
        bad = why or f"{SIGNED_COPY} is not a mapping"
    else:
        bad = None
    ok = bad is None
    return {"doc": doc if ok else None, "sha": sha, "verified": ok, "why": bad, "record": so_rel,
            "signed_by": so.get("signed_by") if ok else None, "signed_at": so.get("signed_at") if ok else None}


# ---- the sign-off sheet, the sign-off history and the evidence dossier (§5.3): one loader each, for every view
SHEET = "decision_spec.signoff_sheet.md"                               # toolkit/spec/signoff.py SHEET
ARCHIVE_DIR, ARCHIVE_PREFIX = "evidence_planner", "superseded_signoff_"     # tools/run_console/dossier.py, the same names
# The sections the sign-off code writes for every spec, in its order. toolkit/spec/signoff.py sheet() appends, outside
# any condition: `out += SC.sheet_lines(raw, run_dir, ...)` (toolkit/spec/spec_critique.py: `out = [SECTION, "", ...`
# with SECTION the first heading below), `out.append("## Engine defaults accepted by name")`, `out.append("## Staged
# inputs and columns not used")`, `out += ["", "## Derivations"] + [...]`, `out += geometry_section[0]`
# (toolkit/spec/geometry.py sheet_section: `out = ["## Input geometry check"]`), `out += PV.sheet_lines(raw, candidates,
# run_dir)` (toolkit/spec/engine_parameters.py: `out = ["## Engine parameter values set by the spec"]`) and `out +=
# ED.sheet_lines(raw, run_dir, dossier_bytes, closure)` (toolkit/spec/evidence_dossier.py: `out = [SECTION]`). The
# sections it writes only for a spec that has the thing (`if oa:`, `if candidates:`, a burden reference file,
# departures, effect settings) are not on this list.
SHEET_ALWAYS = ("Spec critique (decision design principles, before signing)", "Engine defaults accepted by name",
                "Staged inputs and columns not used", "Derivations", "Input geometry check",
                "Engine parameter values set by the spec", "Evidence dossier (inputs that drive an option's effect)")
SHEET_SEVERITIES = ("HIGH", "MEDIUM", "DECLARED", "INFO")              # toolkit/spec/spec_critique.py sheet_lines: `order`
SHEET_SOURCES = "<!-- signoff-sheet-sources: "                         # toolkit/spec/signoff.py SHEET_SOURCES
# toolkit/spec/engine_parameters.py: `- **HIGH**: ...` and `- **REFUSED**: ...`; toolkit/spec/evidence_dossier.py:
# `- **REFUSED at sign-off**: ...`. A line that only holds the word marks nothing.
_SHEET_MARK = re.compile(r"\s*[-*]\s+\*\*(HIGH|REFUSED)(?: at sign-off)?\*\*:\s*(.+?)\s*$")
_TEXT = 4000         # characters of a sentence of the run kept in a row


def signoff_sheet(run, rel=SHEET):
    """({sections, rows, bad, spec_sha}, None) from the sign-off sheet, or (None, why). The file is read to its end and
    never cut at a size. `sections`: its `## ` headings without the marks, in its order. `rows`: what the sheet marks
    with a severity, each {section, severity, text, line}: a row of the spec critique's table (severity its first cell,
    one of SHEET_SEVERITIES; text its Finding cell), and a line that marks a finding `- **HIGH**: ` or `- **REFUSED`
    (severity HIGH or REFUSED; text what follows the mark). `bad`: sentences, one per critique row that is malformed or
    has a severity the producer does not write (§3: named, never dropped). `spec_sha`: the hash of the spec the sheet
    was written from, from its last sources line (toolkit/spec/signoff.py sheet_sources), or None."""
    def read(n, raw):
        line = raw.decode("utf-8", "replace").rstrip("\r")
        if line.startswith("## "):
            return "section", n, line[3:].strip()
        if line.startswith(SHEET_SOURCES) and line.endswith(" -->"):
            return "sources", n, line[len(SHEET_SOURCES):-4]
        m = _SHEET_MARK.match(line)
        if m:
            return "mark", n, m.group(1), m.group(2)
        return ("row", n, line.strip()) if line.lstrip().startswith("|") else None
    got, why = run.lines(rel, read)
    if got is None:
        return None, why
    import json as _json
    out, section = {"sections": [], "rows": [], "bad": [], "spec_sha": None}, None
    for kind, n, *rest in got:
        if kind == "section":
            section = rest[0]
            out["sections"].append(section)
        elif kind == "sources":
            try:
                held = _json.loads(rest[0])
            except ValueError:
                held = None
            out["spec_sha"] = digest(held.get("spec_sha256")) if isinstance(held, dict) else None
        elif kind == "mark":
            out["rows"].append({"section": section, "severity": rest[0], "text": rest[1][:_TEXT], "line": n})
        elif section == SHEET_ALWAYS[0]:
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", rest[0])[1:-1]]
            if not cells or all(c and set(c) <= set("-: ") for c in cells) or cells[0] == "Severity":
                continue
            if len(cells) != 5:
                out["bad"].append(f"line {n} of {rel} is a row of the spec critique with {len(cells)} cells, not the five the producer writes")
            elif cells[0] not in SHEET_SEVERITIES:
                out["bad"].append(f"line {n} of {rel} has the severity {cells[0]!r}, which is not one the producer writes ({', '.join(SHEET_SEVERITIES)})")
            else:
                out["rows"].append({"section": section, "severity": cells[0], "text": cells[3][:_TEXT], "line": n})
    return out, None


def _archived_spec(run, folder, want):
    """(the parsed spec under an archive folder whose sha256 is `want`, its file), or (None, why). As the run console
    finds it (tools/run_console/dossier.py _archived_spec): a file named decision_spec*.yaml in the folder or one
    folder below it; the hash and the parsed spec come from one read."""
    if digest(want) is None:
        return None, "the archived record holds no valid sha256 for the spec"
    for sub in ["", *run.names(folder)]:            # a name that is no folder lists nothing
        at = f"{folder}/{sub}" if sub else folder
        for name in run.names(at):
            if name.startswith("decision_spec") and name.endswith((".yaml", ".yml")):
                sha, doc, why = run.hashed(f"{at}/{name}")
                if sha == want:
                    return (doc, f"{at}/{name}") if isinstance(doc, dict) else (None, why or f"{at}/{name} is not a mapping")
    return None, f"no spec copy in {folder} has the recorded hash {want[:8]}"


def signoff_history(run, spec=None):
    """({rows, bare}, None): every sign-off record of the run, oldest first, the archived ones
    (evidence_planner/superseded_signoff_*/decision_spec.signoff.json, by their signed_at) and then the current record
    (signoff_record). A row: {at, by, note, spec_hash, file, current, spec_file, sections_changed, why}.
    `sections_changed`: the top-level keys of the parsed spec that differ from the spec of the sign-off before it
    (sorted), or None with `why` when it is the first, or when either spec cannot be read with the hash its record
    holds; the current row's spec is the verified signed copy (`spec`, verified_spec's result). A change in comments
    alone is not seen. `bare`: [(folder, why)] for an archive folder with no readable sign-off record: not a sign-off,
    and a gap in the history."""
    spec = spec or verified_spec(run)
    rows, bare = [], []
    for name in run.names(ARCHIVE_DIR):
        if not name.startswith(ARCHIVE_PREFIX):
            continue
        folder = f"{ARCHIVE_DIR}/{name}"
        rel = f"{folder}/{SPEC_SIGNOFF}"
        rec, why = run.json(rel)
        if rec is None:
            held = run.names(folder)
            bare.append((folder, f"it holds {', '.join(held) or 'nothing'} and no sign-off record" if SPEC_SIGNOFF not in held else why))
            continue
        sha = rec["sha256"].get(SPEC) if isinstance(rec.get("sha256"), dict) else None
        doc, where = _archived_spec(run, folder, sha)
        rows.append({"rec": rec, "file": rel, "current": False, "doc": doc, "spec_file": where if doc is not None else None,
                     "no_doc": None if doc is not None else where, "spec_hash": digest(sha)})
    rows.sort(key=lambda r: (str(r["rec"].get("signed_at") or ""), r["file"]))
    cur, cur_rel, _ = signoff_record(run)
    if isinstance(cur, dict):
        sha = cur["sha256"].get(SPEC) if isinstance(cur.get("sha256"), dict) else None
        rows.append({"rec": cur, "file": cur_rel, "current": True, "doc": spec["doc"] if spec["verified"] else None,
                     "spec_file": SIGNED_COPY if spec["verified"] else None, "spec_hash": digest(sha),
                     "no_doc": None if spec["verified"] else f"no signed spec could be verified ({spec['why']})"})
    out, before = [], None
    for r in rows:
        if before is None:
            changed, why = None, "it is the first sign-off: there is none before it to compare with"
        elif r["doc"] is None or before["doc"] is None:
            changed, why = None, (r["no_doc"] if r["doc"] is None else f"the spec of the sign-off before it cannot be read ({before['no_doc']})")
        else:
            a, b = before["doc"], r["doc"]
            changed, why = sorted(str(k) for k in set(a) | set(b) if a.get(k, _ABSENT) != b.get(k, _ABSENT)), None
        rec = r["rec"]
        out.append({"at": str(rec.get("signed_at") or ""), "by": rec.get("signed_by") if isinstance(rec.get("signed_by"), str) else None,
                    "note": rec.get("note") if isinstance(rec.get("note"), str) else None, "spec_hash": r["spec_hash"], "file": r["file"],
                    "current": r["current"], "spec_file": r["spec_file"], "sections_changed": changed, "why": why})
        before = r
    return {"rows": out, "bare": bare}, None


_ABSENT = object()
DOSSIER_KEYS = ("observations", "no_data")        # toolkit/spec/evidence_dossier.py: "give exactly one of observations and no_data"
CHECKED = "checked"                               # §5.3: the mark gap H2 asks for on an observation; no producer writes it yet


def dossier_reference(spec):
    """The run-relative path of the evidence dossier the verified signed spec names (`evidence.dossier`, as
    toolkit/spec/evidence_dossier.py dossier_reference reads it), or None."""
    ev = spec["doc"].get("evidence") if spec["verified"] else None
    rel = ev.get("dossier") if isinstance(ev, dict) else None
    return rel if isinstance(rel, str) and rel.strip() else None


def evidence_dossier(run, spec=None):
    """({file, state, entries, bad}, None) from the evidence dossier the verified signed spec names, or (None, why).
    `state`: the file against the hash the sign-off record holds for it (hash_state's words), hashed and parsed from
    one read. `entries`: one per input, in the file's order: {key, kind (the key's part before its first colon),
    model_value (as recorded), observations (a list of mappings), n_observations, n_unchecked (observations without
    the `checked` mark), marked (observations with it), chosen, low, high (chosen.value; chosen.range), decision_test
    (the structured key only), no_data (the entry holds a no_data mapping), clash (why the entry contradicts the
    producer's rule that an entry gives exactly one of observations and no_data, or None)}. `bad`: one sentence per
    entry that is no mapping, or whose observations are no list of mappings. Only structured fields are read: no text
    is parsed for a duration, a mark or who decided."""
    spec = spec or verified_spec(run)
    if not spec["verified"]:
        return None, f"no signed spec could be verified ({spec['why']}), so no dossier is named by one"
    rel = dossier_reference(spec)
    if rel is None:
        there = not str(run.read("evidence_dossier.yaml", 1)[1]).endswith("is not in the run folder")
        return None, ("the verified signed spec names no evidence dossier (it has no evidence.dossier key)"
                      + (", though the run folder holds a file evidence_dossier.yaml, which nothing ties to the spec" if there else ""))
    sha, doc, why = run.hashed(rel)
    if sha is None or not isinstance(doc, dict):
        return None, why or f"{rel} is not a mapping"
    inputs = doc.get("inputs")
    if not isinstance(inputs, dict):
        return None, f"{rel} holds no mapping of inputs (inputs)"
    record, _, _ = signoff_record(run)
    out = {"file": rel, "state": bound(record, rel, sha), "entries": [], "bad": []}
    for key, e in inputs.items():
        obs, nd = (e.get("observations"), e.get("no_data")) if isinstance(e, dict) else (None, None)
        if not isinstance(e, dict):
            out["bad"].append(f"entry {key} of {rel} is not a mapping")
            continue
        if obs is not None and not (isinstance(obs, list) and all(isinstance(o, dict) for o in obs)):
            out["bad"].append(f"the observations of entry {key} of {rel} are not a list of mappings")
            continue
        obs = obs or []
        chosen = e.get("chosen") if isinstance(e.get("chosen"), dict) else {}
        rng = chosen.get("range") if isinstance(chosen.get("range"), (list, tuple)) and len(chosen["range"]) == 2 else (None, None)
        marked = sum(1 for o in obs if o.get(CHECKED) not in (None, False, ""))
        test = e.get("decision_test")
        out["entries"].append({
            "key": str(key), "kind": str(key).split(":", 1)[0] if ":" in str(key) else None, "model_value": e.get("model_value"),
            "observations": obs, "n_observations": len(obs), "marked": marked, "n_unchecked": len(obs) - marked,
            "chosen": chosen.get("value"), "low": rng[0], "high": rng[1],
            "decision_test": str(test) if isinstance(test, (str, int)) and not isinstance(test, bool) and str(test).strip() else None,
            "no_data": isinstance(nd, dict),
            "clash": ("it gives both observations and no_data" if obs and nd is not None else
                      "it gives neither observations nor no_data" if not obs and nd is None else
                      "its no_data is not a mapping" if not obs and not isinstance(nd, dict) else None)})
    return out, None


def pipeline(run):
    meta, why = run.json("metadata.json")
    if meta is None:
        return _r("steps.pipeline", "steps", "Which pipeline and code ran", "not_available", why,
                  [("metadata.json", "pipeline")], case="C9")
    pipe, commit = meta.get("pipeline"), meta.get("commit")
    ev = [("metadata.json", "pipeline"), ("metadata.json", "commit")]
    if pipe not in ("v1", "v2"):
        return _r("steps.pipeline", "steps", "Which pipeline and code ran", "problem",
                  "The run does not record which pipeline ran, so its results cannot be attributed to v1 or v2.",
                  ev, case="C9")
    inv = meta.get("invocations") or []
    commits = sorted({i.get("commit") for i in inv if isinstance(i, dict) and i.get("commit")} | ({commit} - {None}))
    pipes = sorted({i.get("pipeline") for i in inv if isinstance(i, dict) and i.get("pipeline")} | {pipe})
    rows = {"columns": ["start or resume", "pipeline", "commit", "launcher"],
            "rows": [[i.get("at"), i.get("pipeline"), _short(i.get("commit")), i.get("launcher")]
                     for i in inv if isinstance(i, dict)]}
    if len(pipes) > 1:
        return _r("steps.pipeline", "steps", "Which pipeline and code ran", "problem",
                  f"The run was started or resumed under more than one pipeline ({', '.join(pipes)}).", ev, rows, "C9")
    known = run.commit_known(commit)
    if not commit:
        return _r("steps.pipeline", "steps", "Which pipeline and code ran", "problem",
                  f"Pipeline {pipe}, but no launch commit is recorded.", ev, rows, "C9")
    if len(commits) > 1:
        return _r("steps.pipeline", "steps", "Which pipeline and code ran", "look",
                  f"Pipeline {pipe}. The run was resumed on different code: {', '.join(_short(c) for c in commits)}.",
                  ev, rows, "C9")
    if known is False:
        return _r("steps.pipeline", "steps", "Which pipeline and code ran", "look",
                  f"Pipeline {pipe} at commit {_short(commit)}. That commit is not in this checkout, so the pull "
                  "requests it contains cannot be listed (it may be a launch tree built for the run).", ev, rows, "C9")
    return _r("steps.pipeline", "steps", "Which pipeline and code ran", "ok",
              f"Pipeline {pipe} at commit {_short(commit)}.", ev, rows, "C9")


def signed_spec(run):
    title = "The spec on disk is the spec the owner signed"
    so, so_rel, why = signoff_record(run)
    if so is None:
        state, _ = run.json("run_state.json")
        if (state or {}).get("state") in ("SPEC", "SIGN"):
            return _r("steps.signed_spec", "steps", title, "look",
                      "The spec is not signed yet. Needs you: the run waits at sign-off.",
                      [(so_rel, "sha256")], case="C10")
        return _r("steps.signed_spec", "steps", title, "not_available", why,
                  [(so_rel, "sha256")], case="C10")
    signed = so.get("sha256") if isinstance(so.get("sha256"), dict) else {}
    if not signed:
        return _r("steps.signed_spec", "steps", title, "not_available",
                  "The sign-off record lists no file hashes.", [(so_rel, "sha256")], case="C10")
    rows, changed, absent = [], [], []
    words = {"changed": "changed since signing", "not_recorded": "no valid hash recorded",
             "absent": "absent when signed, absent now", "appeared": "appeared after signing"}
    for rel, sha in sorted(signed.items()):
        now = run.sha(rel)
        state = signed_state(signed, rel, now, now is None and str(run.read(rel, 1)[1]).endswith("is not in the run folder"))
        if state == "absent":
            absent.append(rel)
        elif state != "same":
            changed.append(rel)
        rows.append([rel, _short(sha), _short(now), words.get(state, state)])
    table = {"columns": ["file", "signed", "on disk now", "state"], "rows": rows}
    who = f"signed by {so.get('signed_by')} at {str(so.get('signed_at'))[:16]}"
    ev = [(so_rel, "sha256")]
    appeared = [r[0] for r in rows if r[3] == words["appeared"]]
    if changed:
        return _r("steps.signed_spec", "steps", title, "problem",
                  f"{len(changed)} signed file(s) differ from what the owner signed, are missing, or were signed with no "
                  f"valid hash ({', '.join(changed)}; {who}). "
                  + (f"{', '.join(appeared)} did not exist at signing (the record holds null for it) and appeared after "
                     f"signing. " if appeared else "")
                  + "Anything computed after the change did not use the signed version.", ev, table, "C10")
    return _r("steps.signed_spec", "steps", title, "ok",
              f"All {len(rows) - len(absent)} signed file(s) are unchanged ({who})."
              + (f" {', '.join(absent)} was absent when signed (the record holds null for it) and is absent now." if absent else ""),
              ev, table, "C10")


def artifact_spec(run):
    """Every analysis file the manifest or the analysis folder lists takes part (§3: a recorded hash that is absent
    or malformed is never a match), so a file that records no spec cannot drop out of the comparison."""
    title = "Every analysis file was computed from the signed spec"
    arts = run.artifacts()
    so, so_rel, _ = signoff_record(run)
    signed = digest(((so or {}).get("sha256") or {}).get("decision_spec.yaml")) if isinstance((so or {}).get("sha256"), dict) else None
    if not arts:
        return _r("steps.artifact_spec", "steps", title, "not_available",
                  "The run holds no analysis file.", [("analysis/*.yaml", "spec_sha256")], case="C10")
    if not signed:
        return _r("steps.artifact_spec", "steps", title, "not_available",
                  "There is no valid signed spec hash to compare with.", [(so_rel, "sha256")], case="C10")

    def state(a):
        if digest(a.get("spec_sha256")) is None:
            # A file outside the manifest whose header has no spec hash can still record the spec among its inputs
            # (analysis/trajectories.yaml: toolkit/analysis/trajectories.py write()). That recorded hash is compared
            # as any other: equal to the signed one it ties the file to the signed spec, another one is another spec.
            held = [digest(i.get("sha256")) for i in a.get("inputs") or [] if i.get("path") == SPEC]
            if a.get("in_manifest") or not held or held[0] is None:
                return "no valid spec hash recorded"
            return "signed spec" if held[0] == signed else "other spec"
        if a["spec_sha256"] != signed:
            return "other spec"
        return "spec not signed when computed" if a.get("spec_signed") is False else "signed spec"
    states = [state(a) for a in arts]
    n = {k: states.count(k) for k in ("no valid spec hash recorded", "other spec", "spec not signed when computed")}
    rows = {"columns": ["file", "spec it used", "signed spec", "state"],
            "rows": [[a["path"], _short(digest(a.get("spec_sha256"))), _short(signed), s] for a, s in zip(arts, states)]}
    ev = [("analysis/*.yaml", "spec_sha256, spec_signed"), ("analysis/manifest.json", "artifacts"), (so_rel, "sha256")]
    outside = [a["path"] for a, s in zip(arts, states) if s == "no valid spec hash recorded" and not a.get("in_manifest")]
    if outside and n["no valid spec hash recorded"] == len(outside) and not n["other spec"] and not n["spec not signed when computed"]:
        # A file the analysis manifest does not list and that records no spec: the analysis does not claim it, so it
        # cannot be tied to the signed spec either way. For the reader to judge, and a harness gap, not a mismatch.
        return _r("steps.artifact_spec", "steps", title, "look",
                  f"The {len(arts) - len(outside)} analysis file(s) in the manifest record the signed spec "
                  f"({_short(signed)}). {len(outside)} file(s) in the analysis folder are outside the manifest and "
                  f"record no spec hash ({', '.join(outside[:4])}{' and more' if len(outside) > 4 else ''}), so nothing "
                  "ties them to the signed spec.", ev, rows, "C10")
    if any(n.values()):
        missing = [a["path"] for a, s in zip(arts, states) if s == "no valid spec hash recorded"]
        return _r("steps.artifact_spec", "steps", title, "problem",
                  f"Of {len(arts)} analysis file(s), {n['other spec']} were computed from a spec other than the signed "
                  f"one, {n['spec not signed when computed']} from a spec that was not signed when they were "
                  f"computed, and {len(missing)} record no valid spec hash"
                  + (f" ({', '.join(missing[:4])}{' and more' if len(missing) > 4 else ''})" if missing else "")
                  + ". A file that does not record the signed spec cannot be said to come from it.", ev, rows, "C10")
    return _r("steps.artifact_spec", "steps", title, "ok",
              f"All {len(arts)} analysis files record the signed spec ({_short(signed)}).", ev, rows, "C10")


def inputs_fresh(run):
    title = "Each analysis file's recorded inputs match the files on disk"
    arts = [a for a in run.artifacts() if a.get("inputs")]
    if not arts:
        return _r("steps.inputs_fresh", "steps", title, "not_available",
                  "No analysis file records its inputs.", [("analysis/*.yaml", "inputs")], case="C10")
    rows, stale, checked = [], set(), 0
    for a in arts:
        for i in a["inputs"]:
            if i["state"] in STALE:
                stale.add(a["path"])
                rows.append([a["path"], i["path"], _short(i["sha256"]), _short(i["now"]), i["state"]])
            checked += i["state"] != "outside"
    ev = [("analysis/*.yaml", "inputs")]
    if stale:
        return _r("steps.inputs_fresh", "steps", title, "problem",
                  f"{len(stale)} analysis file(s) were computed from inputs that have since changed or gone, or whose "
                  "hash was not recorded. "
                  "Their numbers do not describe the files now in the run folder.", ev,
                  {"columns": ["analysis file", "input", "hash when computed", "hash now", "state"], "rows": rows}, "C10")
    return _r("steps.inputs_fresh", "steps", title, "ok",
              f"{checked} recorded input hashes across {len(arts)} analysis files match the files on disk.", ev, None, "C10")


def manifest(run):
    title = "The analysis manifest is complete and matches its files"
    man, why = run.json("analysis/manifest.json")
    if man is None:
        return _r("steps.manifest", "steps", title, "not_available", why, [("analysis/manifest.json", "")])
    bad = []
    for group in ("artifacts", "deliverables"):
        for rel, rec in sorted((man.get(group) or {}).items()):
            now, want = run.sha(rel), rec.get("sha256") if isinstance(rec, dict) else None
            state = hash_state(want, now)
            if state != "same":
                bad.append([rel, group[:-1], _short(want), _short(now),
                            {"changed": "changed after the analysis wrote it", "not_recorded": "no valid hash recorded"}.get(state, state)])
    n = len(man.get("artifacts") or {}) + len(man.get("deliverables") or {})
    ev = [("analysis/manifest.json", "artifacts"), ("analysis/manifest.json", "deliverables")]
    rows = {"columns": ["file", "kind", "hash in manifest", "hash now", "state"], "rows": bad} if bad else None
    if man.get("complete") is not True:
        return _r("steps.manifest", "steps", title, "problem",
                  "The analysis did not finish: its manifest is not marked complete.", ev, rows)
    if bad:
        return _r("steps.manifest", "steps", title, "problem",
                  f"{len(bad)} of {n} harness-written files differ from the manifest, are missing, or have no valid "
                  "hash in it. A file changed after the analysis wrote it was not written by the analysis.", ev, rows)
    return _r("steps.manifest", "steps", title, "ok", f"All {n} harness-written files match the manifest.", ev)


def progress(run):
    title = "The run moved forward without loops or failed tasks"
    steps = run.steps()
    if steps.get("why"):
        return _r("steps.progress", "steps", title, "not_available", steps["why"], [("run_state.json", "history")], case="C16")
    seen, repeats = {}, []
    for h in steps["history"]:
        k = (h["from"], h["to"])
        seen[k] = seen.get(k, 0) + 1
    repeats = [(f"{a} → {b}", n) for (a, b), n in seen.items() if n > 1]
    failed = [t for t in steps["tasks"] if t["ok"] is False]
    rows = {"columns": ["what", "count or task", "detail"],
            "rows": [["repeated transition", f"{k} ×{n}", ""] for k, n in repeats]
                    + [["failed task", t["key"], (t["note"] or "")[:200]] for t in failed]}
    ev = [("run_state.json", "history"), ("run_state.json", "journal")]
    if failed:
        return _r("steps.progress", "steps", title, "problem",
                  f"{len(failed)} task(s) failed" + (f" and {len(repeats)} transition(s) repeated" if repeats else "")
                  + f". Last failure: {failed[-1]['key']}.", ev, rows, "C16")
    if repeats:
        return _r("steps.progress", "steps", title, "look",
                  f"{len(repeats)} transition(s) happened more than once ({'; '.join(f'{k} ×{n}' for k, n in repeats)}). "
                  "A repeat is expected between critique rounds, and is a loop otherwise.", ev, rows, "C16")
    return _r("steps.progress", "steps", title, "ok",
              f"{len(steps['history'])} transitions, {len(steps['tasks'])} tasks, none failed or repeated.", ev, None, "C16")


def outcome(run):
    title = "How the run ended"
    state, why = run.json("run_state.json")
    if state is None:
        return _r("steps.outcome", "steps", title, "not_available", why, [("run_state.json", "status")])
    hist = [h for h in state.get("history") or [] if isinstance(h, dict)]
    last = hist[-1].get("why") if hist else None
    status, at, paused = state.get("status"), state.get("state"), state.get("paused")
    ev = [("run_state.json", "status"), ("run_state.json", "history")]
    if paused or at != "FINAL":       # a run that may be waiting, at work or dead: say when it last wrote a file
        written = run.written()["says"]
        ev = ev + [("the run folder", "newest file modification time")]
    if paused:
        return _r("steps.outcome", "steps", title, "look",
                  f"Paused at {at}: {str(paused).replace('_', ' ')}. Needs you. {written}", ev)
    if at != "FINAL":
        return _r("steps.outcome", "steps", title, "look",
                  f"Not finished: the last recorded state is {at} ({last}). {written} A run still at work writes files "
                  "as it goes: if that was long ago, the run stopped here.", ev)
    if status in ACCEPTED:
        return _r("steps.outcome", "steps", title, "ok", f"Finished with status {status}.", ev)
    if status in DECLARED:
        return _r("steps.outcome", "steps", title, "look",
                  f"Finished with status {status}: {last}. The run ended with declared limits to its scope; "
                  "whether the result can be used with those limits is for you to judge.", ev)
    return _r("steps.outcome", "steps", title, "problem",
              f"Finished with status {status}: {last}. The report, if any, is not an accepted result.", ev)


def cost(run):
    title = "Model spend against the run's budget, by stage"
    steps = run.steps()
    if steps.get("why"):
        return _r("steps.cost", "steps", title, "not_available", steps["why"], [("run_state.json", "spent_usd")], case="C11")
    budget, spent = steps.get("budget_usd"), steps.get("spent_usd")
    at_cap = [t for t in steps["tasks"] if t["usd"] and t["allowance_usd"] and t["usd"] >= 0.9 * t["allowance_usd"]]
    rows = {"columns": STAGE_COLUMNS, "rows": by_stage(steps["history"])}
    ev = [("run_state.json", "history[].spent_usd"), ("run_state.json", "budget_usd")]
    note = (" This is language-model spend only. Cloud compute is billed per budget scope, not per run "
            "(Spend view, goal G9).")
    if not isinstance(spent, (int, float)):
        return _r("steps.cost", "steps", title, "not_available", "The run records no spend.", ev, rows, "C11")
    if isinstance(budget, (int, float)) and budget > 0 and spent > budget:
        return _r("steps.cost", "steps", title, "look",
                  f"${spent:.2f} spent against a run budget of ${budget:.2f}." + note, ev, rows, "C11")
    if at_cap:
        return _r("steps.cost", "steps", title, "look",
                  f"${spent:.2f} spent. {len(at_cap)} task(s) used 90% or more of their allowance "
                  f"({', '.join(t['key'] for t in at_cap[:3])}); a task at its allowance may have been cut short." + note,
                  ev, rows, "C11")
    of = f" of ${budget:.2f}" if isinstance(budget, (int, float)) else ""
    return _r("steps.cost", "steps", title, "ok", f"${spent:.2f} spent{of}." + note, ev, rows, "C11")


CHECKS = (outcome, pipeline, signed_spec, artifact_spec, inputs_fresh, manifest, progress, cost)


def run_all(run):
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(run))
        except Exception as e:  # a check must not take the page down; say which one failed and why
            out.append(_r(f"steps.{fn.__name__}", "steps", fn.__name__, "not_available",
                          f"This check failed on the run's files ({type(e).__name__}: {e}).", []))
    return out


def _self_test():
    import json
    import os
    import shutil
    import sys
    import tempfile
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import workbench
    fails = []

    def ok(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        def made(name, **state):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            workbench._toy(d, state=state)
            t0 = 1_700_000_000
            for root, _, names in os.walk(d):
                for n in names:
                    os.utime(os.path.join(root, n), (t0, t0))
            return outcome(workbench.Run(d, now=t0 + 3 * 86400 + 100))
        done, declared, failed = made("done"), made("declared", status="completed_scope_declared"), made("failed", status="run_failed")
        ok("K1 how a finished run ended: accepted is ok, a declared scope is for the reader to judge, anything else a problem",
           [c["status"] for c in (done, declared, failed)] == ["ok", "look", "problem"] and "declared" in declared["says"]
           and "not an accepted result" in failed["says"], str([c["says"] for c in (done, declared, failed)]))
        going, paused = made("going", state="ANALYZE", status=None), made("paused", state="SIGN", status=None, paused="awaiting_signoff")
        ok("K2 a run that is not finished says how long ago its last file was written, so a live run can be told from a dead one",
           going["status"] == "look" and "Not finished" in going["says"] and "was written 3 days ago" in going["says"]
           and "2023-11-14 22:13 UTC" in going["says"] and "If nothing is running" not in going["says"], going["says"])
        ok("K3 a paused run says what it waits for and how long ago its last file was written",
           paused["status"] == "look" and "awaiting signoff" in paused["says"] and "was written 3 days ago" in paused["says"]
           and any(e["file"] == "the run folder" for e in paused["evidence"]) and "ago" not in done["says"], paused["says"])
        d = os.path.join(tmp, "broken")
        os.makedirs(d)
        open(os.path.join(d, "run_state.json"), "w").write(json.dumps(["not", "a", "mapping"]))
        ok("K4 fail closed: every check on an unreadable run is not_available, with a sentence",
           all(c["status"] == "not_available" and c["says"] for c in run_all(workbench.Run(d))))
        def spec_check(name, edit=None):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            workbench._toy(d)
            if edit:
                edit(d)
            return artifact_spec(workbench.Run(d))

        def header(rel, old, new):
            def edit(d):
                p = os.path.join(d, rel)
                text = open(p).read()
                assert old in text, (rel, old)
                open(p, "w").write(text.replace(old, new, 1))
            return edit

        def stray(d):       # a file in analysis/ that the manifest does not list and that records no spec
            open(os.path.join(d, "analysis", "extra.yaml"), "w").write("producer: toolkit/x.py\nvalues: {a: 1}\n")
        clean = spec_check("s5clean")
        null = spec_check("s5null", lambda d: [header("analysis/claims.yaml", "spec_signed: true", "spec_signed: false")(d),
                                               header("analysis/claims.yaml", "spec_sha256: ", "spec_sha256: null\nwas: ")(d)])
        short = spec_check("s5short", header("analysis/claims.yaml", "spec_sha256: ", "spec_sha256: abc\nwas: "))
        extra = spec_check("s5extra", stray)
        ok("S5 every analysis file takes part in the signed-spec check: an absent or malformed spec hash, or a file the "
           "manifest lists, is a problem; a file outside the manifest that records none is for the reader to judge",
           clean["status"] == "ok" and [c["status"] for c in (null, short, extra)] == ["problem", "problem", "look"]
           and "analysis/claims.yaml" in str(null["rows"]) and "analysis/extra.yaml" in str(extra["rows"])
           and "no valid spec hash" in null["says"], str([(c["status"], c["says"]) for c in (clean, null, short, extra)]))
        def unlisted(spec_sha):       # a file outside the manifest with no spec hash in its header and the spec among its inputs
            def edit(d):
                import hashlib
                now = hashlib.sha256(open(os.path.join(d, SPEC), "rb").read()).hexdigest()
                open(os.path.join(d, "analysis", "extra.yaml"), "w").write(
                    f"producer: toolkit/x.py\ninputs:\n  {SPEC}: {now if spec_sha is True else spec_sha}\ndata: {{a: 1}}\n")
            return edit
        tied, other, bad = (spec_check(f"s5b{n}", unlisted(v)) for n, v in enumerate((True, "f" * 64, "abc")))
        ok("S5b a file outside the manifest that records the spec among its inputs is held to that hash: the signed one ties "
           "it to the signed spec, another one is another spec (problem), a malformed one is no hash (for the reader)",
           [c["status"] for c in (tied, other, bad)] == ["ok", "problem", "look"] and "analysis/extra.yaml" in str(tied["rows"]),
           str([(c["status"], c["says"]) for c in (tied, other, bad)]))

        def record(name, edit):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            workbench._toy(d)
            edit(d)
            run = workbench.Run(d)
            return signoff_record(run), signed_spec(run), artifact_spec(run)

        def brief_only(d):
            os.rename(os.path.join(d, SPEC_SIGNOFF), os.path.join(d, BRIEF_SIGNOFF))

        def brief_other(d, spec_hash="0" * 64):
            so = json.load(open(os.path.join(d, SPEC_SIGNOFF)))
            so["sha256"] = {SPEC: spec_hash} if spec_hash else {"decision_brief.yaml": "1" * 64}
            json.dump({**so, "signed_by": "lead"}, open(os.path.join(d, BRIEF_SIGNOFF), "w"))
        (rec, rel, _), signed, arts = record("s6brief", brief_only)
        ok("S6 the sign-off record is the brief's when it holds the spec's hash (toolkit/spec/signoff.py _spec_record): "
           "the Steps checks read it", rel == BRIEF_SIGNOFF and rec["signed_by"] == "owner" and signed["status"] == "ok"
           and arts["status"] == "ok" and any(e["file"] == BRIEF_SIGNOFF for e in signed["evidence"]), str((rel, signed["status"], signed["says"], arts["status"])))
        (rec, rel, _), signed, arts = record("s6both", brief_other)
        ok("S7 with both records the brief's is selected, so a spec that is not the one it signed is a problem",
           rel == BRIEF_SIGNOFF and rec["signed_by"] == "lead" and signed["status"] == "problem" and arts["status"] == "problem",
           str((rel, signed["status"], arts["status"])))
        (rec, rel, _), signed, arts = record("s6spec", lambda d: brief_other(d, None))
        ok("S8 a brief record that holds no hash for the spec is not the spec's sign-off: the spec-only record is",
           rel == SPEC_SIGNOFF and rec["signed_by"] == "owner" and signed["status"] == "ok" and arts["status"] == "ok", str((rel, signed["status"])))
        d = os.path.join(tmp, "s9")
        os.makedirs(d)
        workbench._toy(d)
        os.makedirs(os.path.join(d, "analysis"), exist_ok=True)
        open(os.path.join(d, DT), "w").write(json.dumps({"data": {"tests": [
            {"test_id": "T1", "status": "run", "verdict": "NO_CHANGE"}, {"test_id": "T2", "status": "run", "verdict": "CHANGES_PLAN"},
            {"test_id": "T3", "status": "not_run", "verdict": "NOT_RUN"}, {"test_id": "T4", "status": "run", "verdict": "NOT_RUN"},
            {"test_id": "T5", "status": "not_run", "verdict": "NO_CHANGE"}, {"test_id": "T6", "status": "done", "verdict": "NO_CHANGE"},
            {"test_id": "T7", "status": "run", "verdict": None}, {"test_id": "T8", "status": "run"}]}}))
        tests, why = decision_tests(workbench.Run(d))
        ok("S9 decision tests, one loader: only status run with a verdict of no change or changes plan ran; the producer's "
           "words in another pair contradict each other; any other word is unknown",
           why is None and [tests[f"T{i}"]["state"] for i in range(1, 9)] == ["ran", "ran", "not_run", "contradiction",
                                                                             "contradiction", "unknown", "unknown", "unknown"], str(tests))

        def spec_of(name, edit=None):
            d = os.path.join(tmp, name)
            os.makedirs(os.path.join(d, os.path.dirname(SIGNED_COPY)))
            workbench._toy(d)
            shutil.copyfile(os.path.join(d, SPEC), os.path.join(d, SIGNED_COPY))
            if edit:
                edit(d)
            return verified_spec(workbench.Run(d))

        def other_hash(d):
            so = json.load(open(os.path.join(d, SPEC_SIGNOFF)))
            so["sha256"][SPEC] = "0" * 64
            json.dump(so, open(os.path.join(d, SPEC_SIGNOFF), "w"))
        good = spec_of("s10")
        bad = [spec_of("s10a", lambda d: os.remove(os.path.join(d, SIGNED_COPY))), spec_of("s10b", lambda d: os.remove(os.path.join(d, SPEC_SIGNOFF))),
               spec_of("s10c", other_hash), spec_of("s10d", lambda d: open(os.path.join(d, SIGNED_COPY), "a").write("\n# edited after the sign-off\n"))]
        ok("S10 the signed spec, one loader: verified only when the signed copy has the hash the sign-off record holds; "
           "with no copy, no record or another hash it gives no document, no signer and the reason",
           good["verified"] and isinstance(good["doc"], dict) and good["signed_by"] == "owner" and good["why"] is None
           and all(not b["verified"] and b["doc"] is None and b["signed_by"] is None and b["why"] for b in bad),
           str((good["why"], [b["why"] for b in bad])))
        def tests_of(name, edit=None):
            d = os.path.join(tmp, name)
            os.makedirs(os.path.join(d, os.path.dirname(SIGNED_COPY)))
            workbench._toy(d)
            shutil.copyfile(os.path.join(d, SPEC), os.path.join(d, SIGNED_COPY))
            sha = verified_spec(workbench.Run(d))["sha"]
            head = {"spec_sha256": sha, "data": {"tests": [{"test_id": "T1", "status": "run", "verdict": "NO_CHANGE"},
                                                            {"test_id": "T2", "status": "not_run", "verdict": "NOT_RUN"}]}}
            (edit or (lambda d, head: None))(d, head)
            open(os.path.join(d, DT), "w").write(json.dumps(head))
            return decision_tests(workbench.Run(d))[0]
        same = tests_of("s11")
        other = tests_of("s11a", lambda d, head: head.update(spec_sha256="1" * 64))
        none = tests_of("s11b", lambda d, head: head.pop("spec_sha256"))
        unsigned = tests_of("s11c", lambda d, head: os.remove(os.path.join(d, SPEC_SIGNOFF)))
        ok("S11 a decision test counts only when its file records the verified signed spec (§3): with another spec's "
           "hash, none, or no verified signed spec, a test that ran still ran and counts for nothing",
           same["T1"]["counts"] and same["T1"]["spec"] == "same" and not same["T2"]["counts"]
           and [(t["T1"]["state"], t["T1"]["counts"], t["T1"]["spec"]) for t in (other, none, unsigned)]
           == [("ran", False, "other"), ("ran", False, "not_recorded"), ("ran", False, "unverified")]
           and all(t["T1"]["spec_says"] for t in (other, none, unsigned)), str((same, other, none, unsigned)))
        row = {"test_id": "T1", "status": "run", "verdict": "NO_CHANGE", "point_plan_feasible": True, "triggered_by": [],
               "share_pct": 0.0, "threshold_pct": 10.0, "regret_pct": 0.0, "regret_threshold_pct": 1.0, "dollars_share_pct": 0.0, "dollars_threshold_pct": 5.0}

        def judged(**words):
            return tests_of(f"s12{len(os.listdir(tmp))}", lambda d, head: head["data"].update(tests=[{**row, **words}]))["T1"]
        fired = [judged(share_pct=25.0), judged(regret_pct=1.5), judged(dollars_share_pct=5.5), judged(point_plan_feasible=False), judged(triggered_by=["regret"]),
                 judged(verdict="CHANGES_PLAN", point_plan_feasible=True)]
        kept = [judged(), judged(share_pct=10.0), judged(verdict="CHANGES_PLAN", share_pct=25.0, triggered_by=["share"]),
                judged(verdict="CHANGES_PLAN", point_plan_feasible=False, triggered_by=["infeasible"]),
                judged(verdict="CHANGES_PLAN", regret_pct=None, triggered_by=None, point_plan_feasible=True)]
        ok("S12 a test's verdict against its own numbers, by the producer's rule (toolkit/analysis/moves.py fires: strictly above "
           "the threshold; an infeasible point plan changes the plan): a recorded word the numbers contradict is a contradiction "
           "and counts for nothing; a share equal to its threshold does not fire",
           all(t["state"] == "contradiction" and not t["counts"] and t["clash"] for t in fired)
           and all(t["state"] == "ran" and t["counts"] and t["clash"] is None for t in kept), str([(t["state"], t["clash"]) for t in fired + kept]))

        def est_of(name, edit=None, rule="yes"):
            d = os.path.join(tmp, name)
            os.makedirs(os.path.join(d, os.path.dirname(SIGNED_COPY)))
            workbench._toy(d)
            shutil.copyfile(os.path.join(d, SPEC), os.path.join(d, SIGNED_COPY))
            end = {"value": 1.0, "kind": "declared", "rejected_by_data": False, "fit_verified": True, "verdict": "yes"}
            t = {"id": "E1", "parameter": "p1", "status": "run", "decision_changes": False, "ends": [dict(end), dict(end, value=2.0)]}
            (edit or (lambda t: None))(t)
            doc = {"spec_sha256": verified_spec(workbench.Run(d))["sha"], "data": {"decision_tests": [t], **({"decision": {"verdict": rule}} if rule else {})}}
            open(os.path.join(d, EST), "w").write(json.dumps(doc))
            return estimates_tests(workbench.Run(d))[0]["E1"]
        clean = est_of("s13")
        no_ends = est_of("s13a", lambda t: t.update(ends=[]))
        all_rejected = est_of("s13b", lambda t: [e.update(rejected_by_data=True) for e in t["ends"]])
        other_end = est_of("s13c", lambda t: t["ends"][1].update(verdict="no"))
        said_changes = est_of("s13d", lambda t: t.update(decision_changes=True))
        changes = est_of("s13e", lambda t: (t["ends"][1].update(verdict="no"), t.update(decision_changes=True)))
        rejected = est_of("s13f", lambda t: t["ends"].append({"value": 3.0, "rejected_by_data": True, "verdict": "no"}))
        no_rule = est_of("s13g", rule=None)
        ok("S13 an estimates test, by the producer's rule (toolkit/analysis/estimates.py: decision_changes is whether any end the "
           "data do not reject has a verdict other than the decision's): it ran only with such an end and a real verdict at each; "
           "a recorded flag the ends contradict is a contradiction; an end the data reject does not count",
           (clean["state"], clean["verdict"], changes["state"], changes["verdict"], rejected["state"]) == ("ran", "NO_CHANGE", "ran", "CHANGES_PLAN", "ran")
           and [t["state"] for t in (no_ends, all_rejected, no_rule)] == ["unknown"] * 3
           and [t["state"] for t in (other_end, said_changes)] == ["contradiction"] * 2 and other_end["clash"] and not other_end["counts"],
           str([(t["state"], t["verdict"], t.get("clash")) for t in (clean, no_ends, all_rejected, other_end, said_changes, changes, rejected, no_rule)]))
        _self_test_spec_sources(tmp, ok)
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


def _self_test_spec_sources(tmp, ok):
    """The loaders of the sign-off sheet, the sign-off history and the evidence dossier, on fixture runs."""
    import json
    import os

    import workbench
    from lib.safe_io import read_nofollow, write_nofollow
    from views.fit_bench import fixture_builder
    fx = fixture_builder()

    def built(name, defect=None):
        d = os.path.join(tmp, name)
        fx.build(d, defect=defect)
        return d

    def retext(d, rel, change):
        write_nofollow(d, rel, change(read_nofollow(d, rel)[0].decode("utf-8")).encode("utf-8"))
        return d
    clean = built("t-clean")
    sheet, _ = signoff_sheet(workbench.Run(clean))
    ok("T1 the sections the sign-off code writes for every spec are the producers' own headings (fixtures.py X53 holds them to "
       "the source), and the clean sheet has each; its rows are the critique's, under their section; its last line gives the spec",
       tuple("## " + h for h in SHEET_ALWAYS) == fx.SHEET_ALWAYS and all(h in sheet["sections"] for h in SHEET_ALWAYS)
       and [(r["section"], r["severity"]) for r in sheet["rows"]] == [(SHEET_ALWAYS[0], "MEDIUM")] * 2 + [(SHEET_ALWAYS[0], "INFO")] * 2
       and not sheet["bad"] and sheet["spec_sha"] == verified_spec(workbench.Run(clean))["sha"], str(sheet))
    high, _ = signoff_sheet(workbench.Run(built("t-high", "sheet_high")))
    escaped = retext(built("t-escaped"), fx.SHEET, lambda t: t.replace("| INFO | 2 | headline_comparator | ", "| INFO | 2 | headline_comparator | a \\| b ", 1)
                     + "\n" + "".join(f"- line {i} of a long sheet\n" for i in range(200000)) + "- **REFUSED at sign-off**: entry q9 has no source\n")
    long, _ = signoff_sheet(workbench.Run(escaped))
    ok("T2 a line that marks a finding HIGH is a row under its section and the bare word is none; an escaped bar stays in its "
       "cell; a REFUSED line far down a long sheet is read (the file is never cut at a size); no sheet says why",
       [(r["section"], r["severity"]) for r in high["rows"] if r["severity"] == "HIGH"] == [(SHEET_ALWAYS[5], "HIGH")]
       and any(r["text"].startswith("a | b ") for r in long["rows"]) and long["rows"][-1]["severity"] == "REFUSED"
       and long["rows"][-1]["text"] == "entry q9 has no source" and signoff_sheet(workbench.Run(tmp))[0] is None
       and "not in the run folder" in signoff_sheet(workbench.Run(tmp))[1], str(long["rows"][-2:]))
    h, _ = signoff_history(workbench.Run(clean))
    bare, _ = signoff_history(workbench.Run(built("t-bare", "archive_no_record")))
    lost = built("t-lost")
    os.remove(os.path.join(lost, fx.SECOND + fx.SPEC_FILES[1]))
    gone, _ = signoff_history(workbench.Run(lost))
    ok("T3 the sign-off history: archived records by their time and then the current one; the sections each changed by "
       "top-level key; a folder with no record is listed and is no sign-off; a sign-off whose signed copy is gone has no "
       "count, and neither has the one after it",
       [r["at"][:10] for r in h["rows"]] == [*fx.EARLIER, "2026-01-01"] and [r["current"] for r in h["rows"]] == [False, False, True]
       and [r["sections_changed"] for r in h["rows"]] == [None, ["decision_maker"], ["uncertainty"]] and not h["bare"]
       and len(bare["rows"]) == 3 and [f for f, _ in bare["bare"]] == [f"{ARCHIVE_DIR}/{ARCHIVE_PREFIX}{fx.EARLIER[1]}b"]
       and [r["sections_changed"] for r in gone["rows"]] == [None, None, None] and "recorded hash" in gone["rows"][1]["why"]
       and signoff_history(workbench.Run(tmp))[0]["rows"] == [], str((h, bare["bare"], gone)))
    d, _ = evidence_dossier(workbench.Run(clean))
    moved = retext(built("t-moved"), fx.DOSSIER, lambda t: t + "\n")
    spec = verified_spec(workbench.Run(clean))
    ok("T4 the evidence dossier is the file the verified signed spec names, held to the hash the sign-off record has for it; "
       "an entry's marks and test come from structured keys only; with no verified spec, or none named, there is none",
       d["state"] == "same" and [(e["key"], e["no_data"], e["n_observations"], e["n_unchecked"], e["decision_test"]) for e in d["entries"]][:2]
       == [("parameter:p1", False, 2, 0, "DT3"), ("parameter:b_add", False, 1, 0, "DT5")] and not d["bad"] and not any(e["clash"] for e in d["entries"])
       and evidence_dossier(workbench.Run(moved))[0]["state"] == "changed"
       and evidence_dossier(workbench.Run(clean), {**spec, "verified": False, "why": "x"})[0] is None
       and "names no evidence dossier" in evidence_dossier(workbench.Run(clean), {**spec, "doc": {}})[1]
       and "which nothing ties" in evidence_dossier(workbench.Run(clean), {**spec, "doc": {}})[1], str(d)[:600])


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_self_test() if "--self-test" in _sys.argv[1:] else "usage: checks.py --self-test")
