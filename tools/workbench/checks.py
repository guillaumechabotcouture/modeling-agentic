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
    `contradiction` (the producer's words in a pair it does not write, so the record contradicts itself), or
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
        out[str(t["test_id"])] = {"status": status, "verdict": verdict, "state": state,
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
    CHANGES_PLAN and with false NO_CHANGE, and only when an end that the data do not reject holds one of the rule's
    verdicts (EST_VERDICTS), since a test counts only with a real verdict; `not_assessable` did not run (NOT_RUN); any
    other status, or a run test with no verdict, is `unknown`. `status` stays the producer's own word."""
    doc, why = run.large(EST)
    if doc is None:
        return None, why
    tests = doc.get("data", {}).get("decision_tests") if isinstance(doc.get("data"), dict) else None
    if not isinstance(tests, list) or not all(isinstance(t, dict) and t.get("id") for t in tests):
        return None, f"{EST} holds no list of tests (data.decision_tests)"
    spec = spec or verified_spec(run)
    tie, tie_says = _tie(run, EST, doc, spec)
    out = {}
    for t in tests:
        status, changes = t.get("status"), t.get("decision_changes")
        ends = [e for e in t.get("ends") or [] if isinstance(e, dict) and not e.get("rejected_by_data")]
        real = isinstance(changes, bool) and (any(e.get("verdict") in EST_VERDICTS for e in ends) or not ends) \
            and all(e.get("verdict") in EST_VERDICTS for e in ends)
        if status == "run" and real:
            state, verdict = "ran", "CHANGES_PLAN" if changes else "NO_CHANGE"
        elif status == "not_assessable":
            state, verdict = "not_run", "NOT_RUN"
        else:
            state, verdict = "unknown", None
        out[str(t["id"])] = {"status": status, "verdict": verdict, "state": state, "counts": state == "ran" and tie == "same",
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
            return "no valid spec hash recorded"
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
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_self_test() if "--self-test" in _sys.argv[1:] else "usage: checks.py --self-test")
