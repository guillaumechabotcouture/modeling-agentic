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
    return (sha or "")[:10] or "none"


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
    so, why = run.json("decision_spec.signoff.json")
    if so is None:
        state, _ = run.json("run_state.json")
        if (state or {}).get("state") in ("SPEC", "SIGN"):
            return _r("steps.signed_spec", "steps", title, "look",
                      "The spec is not signed yet. Needs you: the run waits at sign-off.",
                      [("decision_spec.signoff.json", "sha256")], case="C10")
        return _r("steps.signed_spec", "steps", title, "not_available", why,
                  [("decision_spec.signoff.json", "sha256")], case="C10")
    signed = so.get("sha256") if isinstance(so.get("sha256"), dict) else {}
    if not signed:
        return _r("steps.signed_spec", "steps", title, "not_available",
                  "The sign-off record lists no file hashes.", [("decision_spec.signoff.json", "sha256")], case="C10")
    rows, changed = [], []
    for rel, sha in sorted(signed.items()):
        now = run.sha(rel)
        state = "same" if now == sha else ("missing" if now is None else "changed since signing")
        if state != "same":
            changed.append(rel)
        rows.append([rel, _short(sha), _short(now), state])
    table = {"columns": ["file", "signed", "on disk now", "state"], "rows": rows}
    who = f"signed by {so.get('signed_by')} at {str(so.get('signed_at'))[:16]}"
    ev = [("decision_spec.signoff.json", "sha256")]
    if changed:
        return _r("steps.signed_spec", "steps", title, "problem",
                  f"{len(changed)} signed file(s) differ from what the owner signed ({', '.join(changed)}; {who}). "
                  "Anything computed after the change did not use the signed version.", ev, table, "C10")
    return _r("steps.signed_spec", "steps", title, "ok", f"All {len(rows)} signed file(s) are unchanged ({who}).",
              ev, table, "C10")


def artifact_spec(run):
    title = "Every analysis file was computed from the signed spec"
    arts = run.artifacts()
    so, _ = run.json("decision_spec.signoff.json")
    signed = ((so or {}).get("sha256") or {}).get("decision_spec.yaml") if isinstance((so or {}).get("sha256"), dict) else None
    env = [a for a in arts if a.get("spec_sha256")]
    if not env:
        return _r("steps.artifact_spec", "steps", title, "not_available",
                  "No analysis file records the spec it was computed from.", [("analysis/*.yaml", "spec_sha256")], case="C10")
    if not signed:
        return _r("steps.artifact_spec", "steps", title, "not_available",
                  "There is no signed spec hash to compare with.", [("decision_spec.signoff.json", "sha256")], case="C10")
    off = [a for a in env if a["spec_sha256"] != signed]
    unsigned = [a for a in env if a.get("spec_signed") is False]
    rows = {"columns": ["file", "spec it used", "signed spec", "state"],
            "rows": [[a["path"], _short(a["spec_sha256"]), _short(signed),
                      "other spec" if a in off else ("spec not signed when computed" if a in unsigned else "signed spec")]
                     for a in env]}
    ev = [("analysis/*.yaml", "spec_sha256"), ("decision_spec.signoff.json", "sha256")]
    if off or unsigned:
        return _r("steps.artifact_spec", "steps", title, "problem",
                  f"{len(off)} analysis file(s) were computed from a spec other than the signed one, "
                  f"and {len(unsigned)} from an unsigned spec.", ev, rows, "C10")
    return _r("steps.artifact_spec", "steps", title, "ok",
              f"All {len(env)} analysis files record the signed spec ({_short(signed)}).", ev, rows, "C10")


def inputs_fresh(run):
    title = "Each analysis file's recorded inputs match the files on disk"
    arts = [a for a in run.artifacts() if a.get("inputs")]
    if not arts:
        return _r("steps.inputs_fresh", "steps", title, "not_available",
                  "No analysis file records its inputs.", [("analysis/*.yaml", "inputs")], case="C10")
    rows, stale, checked = [], set(), 0
    for a in arts:
        for i in a["inputs"]:
            if i["state"] in ("changed", "missing"):
                stale.add(a["path"])
                rows.append([a["path"], i["path"], _short(i["sha256"]), _short(i["now"]), i["state"]])
            checked += i["state"] != "outside"
    ev = [("analysis/*.yaml", "inputs")]
    if stale:
        return _r("steps.inputs_fresh", "steps", title, "problem",
                  f"{len(stale)} analysis file(s) were computed from inputs that have since changed or gone. "
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
            now = run.sha(rel)
            if now != (rec or {}).get("sha256"):
                bad.append([rel, group[:-1], _short((rec or {}).get("sha256")), _short(now),
                            "missing" if now is None else "changed after the analysis wrote it"])
    n = len(man.get("artifacts") or {}) + len(man.get("deliverables") or {})
    ev = [("analysis/manifest.json", "artifacts"), ("analysis/manifest.json", "deliverables")]
    rows = {"columns": ["file", "kind", "hash in manifest", "hash now", "state"], "rows": bad} if bad else None
    if man.get("complete") is not True:
        return _r("steps.manifest", "steps", title, "problem",
                  "The analysis did not finish: its manifest is not marked complete.", ev, rows)
    if bad:
        return _r("steps.manifest", "steps", title, "problem",
                  f"{len(bad)} of {n} harness-written files differ from the manifest. A file changed after the "
                  "analysis wrote it was not written by the analysis.", ev, rows)
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
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_self_test() if "--self-test" in _sys.argv[1:] else "usage: checks.py --self-test")
