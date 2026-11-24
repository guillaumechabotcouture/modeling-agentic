#!/usr/bin/env python3
"""More run records of the diagnostics contract: run_artifact, run_compare, owner_inbox (§6, §8). Read-only.

    python3 tools/diagnostics/diag_records.py --self-test

Reads go through the review workbench's confined reader, as in diag_runs.py. Each builder returns
(data, sources, notes, derived).
"""
from __future__ import annotations

import fnmatch
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import diag_runs as DR  # noqa: E402
from diag_runs import WB, _mtime, _src, refused, run_spend  # noqa: E402

# run_artifact's allow-list (§8): harness records a diagnosis needs, nothing an agent drafts beyond the report
ARTIFACTS = ("metadata.json", "run_state.json", "events.jsonl", "findings.jsonl", "gate_r*.json", "final_gate.json",
             "validator_history.jsonl", "fix_plan_r*.json", "impact_ledger.jsonl", "scope_signoff_request.json",
             "spec_rethink_request.json", "decision_spec.yaml", "decision_spec.signoff_sheet.md", "engine_choice.json",
             "budget_ledger.json", "pipeline_progress.json", "models/pipeline_report.json", "models/compute_ledger.json",
             "models/logs/*.log", "report.md", "data/inputs_provenance.json", "analysis/*.yaml")
MAX_ARTIFACT = 256 * 1024
LOG_TAIL = 64 * 1024


def allowed(rel: str) -> bool:
    """Whether a run-relative path is on the allow-list, matched component by component (a `*` never spans a '/';
    a backslash is refused: Codex r1 of #386)."""
    if "\\" in rel or refused(rel):
        return False
    parts = rel.split("/")
    return any(len(parts) == len(pp) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, pp))
               for pp in (p.split("/") for p in ARTIFACTS))


def _tail(fd, st):
    os.lseek(fd, max(0, st.st_size - LOG_TAIL), os.SEEK_SET)
    return os.read(fd, LOG_TAIL), st.st_size


def run_artifact(run, rel):
    rel = WB.check_rel(rel)
    if not allowed(rel):
        raise ValueError(f"{rel}: not in run_artifact's allow-list ({', '.join(ARTIFACTS)})")
    is_log, notes = rel.endswith(".log"), []
    if is_log:                                 # the tail only, read from the end (never the whole log)
        got, why = run._open(rel, _tail)
        if got is None:
            return None, [], [why], {}
        b, size = got
        if size > LOG_TAIL:
            notes = [f"the last {LOG_TAIL // 1024} KB of a {size // 1024} KB log"]
    else:
        b, why = run.read(rel, MAX_ARTIFACT + 1)
        if b is None:
            return None, [], [why], {}
        if len(b) > MAX_ARTIFACT:
            b, notes = b[:MAX_ARTIFACT], [f"cut at {MAX_ARTIFACT // 1024} KB"]
    text = b.decode("utf-8", "replace")
    data = {"path": rel, "text": text}
    if rel.endswith(".json") and not notes:
        try:
            data = {"path": rel, "json": json.loads(text)}
        except ValueError:
            pass
    return data, _src(run, [rel]), notes, {}


def _norm(q) -> str:
    return " ".join(str(q or "").lower().split())


def run_compare(run, others):
    """This run's elapsed time and spend by stage against the FINAL runs of the same question."""
    st, why = run.json("run_state.json")
    if st is None:
        return None, [], [why], {}
    ref, refs = {}, []
    for o in others:
        if o.path == run.path:
            continue
        so, _ = o.json("run_state.json")
        if not so or so.get("state") != "FINAL" or _norm(so.get("question")) != _norm(st.get("question")):
            continue
        refs.append({"path": os.path.join(o.path, "run_state.json"), "mtime": _mtime(o, "run_state.json")})
        for stage, v in ((run_spend(o)[0] or {}).get("by_stage") or {}).items():
            r = ref.setdefault(stage, {"s": [], "usd": []})
            if v.get("elapsed_s") is not None:
                r["s"].append(v["elapsed_s"])
            r["usd"].append(v.get("usd") or 0.0)
    this = run_spend(run)[0] or {}
    reference = {s: {"median_s": statistics.median(v["s"]) if v["s"] else None,
                     "median_usd": round(statistics.median(v["usd"]), 4) if v["usd"] else None, "n": len(v["usd"])}
                 for s, v in ref.items()}
    seen = {x for h in st.get("history") or [] if isinstance(h, dict) and "to" in h for x in (h.get("from"), h.get("to"))}
    seen.add(st.get("state"))
    notes = [] if refs else ["no FINAL run of the same question under the roots: nothing to compare with"]
    # Which stages are still ahead is not knowable (the fix path revisits CRITIQUE and GATE: Codex r1 of #388); the
    # reference's stages this run has not entered yet are listed under that plain name.
    return ({"question": st.get("question"), "n_runs": len(refs),
             "this": {"state": st.get("state"), "by_stage": this.get("by_stage")}, "reference": reference,
             "stages_not_yet_entered": [s for s in reference if s not in seen and st.get("state") != "FINAL"]},
            _src(run, ["run_state.json"]) + refs, notes, {"reference": "inferred"})


def _signed_sha(rec) -> str | None:
    """The spec digest a sign-off record binds: the spike's `spec_sha256` or toolkit/spec/signoff.py's
    `sha256["decision_spec.yaml"]` (Codex r1 of #388)."""
    if not isinstance(rec, dict):
        return None
    sh = rec.get("sha256")
    return rec.get("spec_sha256") or (sh.get("decision_spec.yaml") if isinstance(sh, dict) else None)


def _answered(run, paused, since) -> bool:
    """The pause's answer is already on disk: the current spec is signed, or every pending scope item's fingerprint
    has a recorded accept or decline (not a file time: Codex r1 of #388)."""
    if paused == "awaiting_signoff":
        cur = run.sha("decision_spec.yaml")
        return bool(cur) and any(_signed_sha(run.json(f)[0]) == cur
                                 for f in ("decision_spec.signoff.json", "decision_brief.signoff.json"))
    if paused == "awaiting_scope_signoff":
        req, _ = run.json("scope_signoff_request.json")
        rec, _ = run.json("scope_signoff.json")
        fps = [i.get("fingerprint") for i in (req or {}).get("items") or [] if isinstance(i, dict)]
        ans = (rec or {}).get("answers") if isinstance((rec or {}).get("answers"), dict) else {}
        return bool(fps) and all(fp and isinstance(ans.get(fp), dict)
                                 and ans[fp].get("decision") in ("accept", "decline") for fp in fps)
    return False


HOW = {"awaiting_signoff": "python3 agents/stage_machine.py sign <run> --by owner (after reading the sign-off sheet)",
       "awaiting_scope_signoff": "python3 scripts/scope_signoff.py show <run>, then sign <run> --by owner "
                                 "--answers <answers.yaml> (or --accept-all / --decline-all)",
       "awaiting_spec_amendment": "read spec_rethink_request.json, amend the spec, relaunch with reuse",
       "awaiting_budget": "python3 main.py --v2 --resume <run> --max-budget-usd <higher cap>"}


def owner_inbox(runs: dict):
    """Every run waiting on the owner: its question, since when, and how to answer it."""
    items = []
    for rid, run in sorted(runs.items()):
        st, _ = run.json("run_state.json")
        if not st or st.get("state") == "FINAL" or not st.get("paused"):
            continue
        p = next((h for h in reversed(st.get("history") or []) if isinstance(h, dict) and h.get("paused")), {})
        q, how = st["paused"], HOW.get(st["paused"], "see the run's history")
        if _answered(run, q, p.get("at")):
            q, how = f"{q}: answered, waiting for a resume", "python3 main.py --v2 --resume <run>"
        if rid == "mirror" or rid.startswith("mirror/"):     # a local copy of a cloud run: never act on the copy
            how = f"a cloud run: do this on its cluster (the run console), not on this local mirror: {how}"
        items.append({"where": "run", "ref": rid, "question": q, "state": st.get("state"), "since": p.get("at"),
                      "how_to_answer": how})
    return {"items": items}, [], [], {"items": "record"}


def _self_test() -> int:
    import hashlib
    import tempfile
    fails = []

    def ok(c, label):
        print(("  ok   " if c else "  FAIL ") + label)
        if not c:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        root = os.path.join(td, "runs")
        a = DR.make_fixture(root, "a_run")
        b = DR.make_fixture(root, "b_run", paused="awaiting_scope_signoff", events=False)
        c = DR.make_fixture(os.path.join(td, "mirror", "l1"), "c_run", paused="awaiting_signoff", events=False)
        found = DR.discover({"runs": root, "mirror": os.path.join(td, "mirror")})
        ra = found["runs/a_run"]
        for bad in ("secrets.yaml", "../b_run/run_state.json", "models/logs/archive/a.log", "analysis\\x.yaml"):
            try:
                run_artifact(ra, bad)
                ok(False, f"DX1 run_artifact refuses {bad!r}")
            except ValueError:
                ok(True, f"DX1 run_artifact refuses {bad!r}")
        ok(run_artifact(ra, "run_state.json")[0]["json"]["state"] == "CRITIQUE", "DX2 an allow-listed record, parsed")
        open(os.path.join(td, "elsewhere.json"), "w").write("{}")
        os.symlink(os.path.join(td, "elsewhere.json"), os.path.join(a, "final_gate.json"))
        ok(run_artifact(ra, "final_gate.json")[0] is None, "DX3 a link inside the run is not followed")
        os.makedirs(os.path.join(a, "models", "logs"))
        with open(os.path.join(a, "models", "logs", "step.log"), "wb") as f:
            f.write(b"x" * (3 * LOG_TAIL) + b"END")
        lg = run_artifact(ra, "models/logs/step.log")
        ok(len(lg[0]["text"]) == LOG_TAIL and lg[0]["text"].endswith("END") and lg[2],
           "DX4 a step log returns its tail only, read from the end")
        inbox = owner_inbox(found)[0]["items"]
        by = {i["ref"]: i for i in inbox}
        ok(set(by) == {"runs/b_run", "mirror/l1/c_run"} and "--answers" in by["runs/b_run"]["how_to_answer"]
           and "cluster" in by["mirror/l1/c_run"]["how_to_answer"],
           f"DX5 the inbox: the scope command with its answers, a mirrored run routed to its cluster ({inbox})")
        open(os.path.join(c, "decision_spec.yaml"), "w").write("x: 1\n")
        json.dump({"spec_sha256": hashlib.sha256(b"x: 1\n").hexdigest()}, open(os.path.join(c, "decision_spec.signoff.json"), "w"))
        c2 = owner_inbox(DR.discover({"mirror": os.path.join(td, "mirror")}))[0]["items"][0]
        ok("answered" in c2["question"], f"DX6 a signed spec is shown as waiting for a resume ({c2['question']})")
        cmp_ = run_compare(ra, list(found.values()))
        ok(cmp_[0]["n_runs"] == 0 and cmp_[2], "DX7 run_compare says when there is nothing to compare with")
        st = json.load(open(os.path.join(b, "run_state.json")))
        st.update(state="FINAL", paused=None)
        json.dump(st, open(os.path.join(b, "run_state.json"), "w"))
        cmp2 = run_compare(DR.discover({"runs": root})["runs/a_run"], list(DR.discover({"runs": root}).values()))
        ok(cmp2[0]["n_runs"] == 1 and len(cmp2[1]) == 2 and "SPEC" not in cmp2[0]["stages_not_yet_entered"]
           and "EVIDENCE" not in cmp2[0]["stages_not_yet_entered"],
           f"DX8 the reference runs are sources; stages already entered are not listed ({cmp2[0]['stages_not_yet_entered']})")
        rq = {"pending": True, "items": [{"finding": "D-001", "fingerprint": "f1"}, {"finding": "D-002", "fingerprint": "f2"}]}
        dq = DR.make_fixture(root, "q_run", paused="awaiting_scope_signoff", events=False)
        json.dump(rq, open(os.path.join(dq, "scope_signoff_request.json"), "w"))
        json.dump({"answers": {"f1": {"decision": "accept"}}}, open(os.path.join(dq, "scope_signoff.json"), "w"))
        q1 = [i for i in owner_inbox(DR.discover({"runs": root}))[0]["items"] if i["ref"] == "runs/q_run"][0]
        json.dump({"answers": {"f1": {"decision": "accept"}, "f2": {"decision": "decline"}}},
                  open(os.path.join(dq, "scope_signoff.json"), "w"))
        q2 = [i for i in owner_inbox(DR.discover({"runs": root}))[0]["items"] if i["ref"] == "runs/q_run"][0]
        ok("answered" not in q1["question"] and "answered" in q2["question"],
           "DX9 a scope request is answered only when every pending item has a decision")
        json.dump({"sha256": {"decision_spec.yaml": hashlib.sha256(b"x: 1\n").hexdigest()}},
                  open(os.path.join(c, "decision_spec.signoff.json"), "w"))
        c3 = owner_inbox(DR.discover({"mirror": os.path.join(td, "mirror")}))[0]["items"][0]
        ok("answered" in c3["question"] and "main.py" in c3["how_to_answer"] and "cluster" in c3["how_to_answer"],
           "DX10 a toolkit sign-off record counts; a mirrored run keeps the action, routed to its cluster")
    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
