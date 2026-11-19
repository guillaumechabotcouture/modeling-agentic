#!/usr/bin/env python3
"""The Steps view (docs/design/review-workbench.md §5.2): the stage history, the tasks, and each harness-written
file with the inputs it was computed from. Returns the generic view result; the page has no code for this view.

    python3 tools/workbench/views/steps.py --self-test
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402

TERMS = ("A stage is a state of the run's stage machine. A transition is one move from a stage to the next; its "
         "time and model spend are those of the stage it leaves. A task is one bounded language-model session "
         "inside a stage, with a spending allowance. An analysis file is written by harness code and records the "
         "hash of every file it was computed from.")


def _short(sha, n=8):
    return str(sha)[:n] if sha else None


def view(run):
    out = {"checks": checks.run_all(run), "facts": {}, "tables": {}, "charts": [], "notes": {"terms": TERMS}}
    st = run.steps()
    if st.get("why"):
        out["notes"]["not_available"] = f"The stage history is not available: {st['why']}."
        return out
    meta, so = (run.json(f)[0] or {} for f in ("metadata.json", "decision_spec.signoff.json"))
    man, man_why = run.json("analysis/manifest.json")
    man = man or {}
    hist, tasks, arts, wr = st["history"], st["tasks"], run.artifacts(), run.written()
    stages = checks.by_stage(hist)
    known = run.commit_known(meta.get("commit"))
    signed = so.get("sha256") if isinstance(so.get("sha256"), dict) else {}
    listed = man.get("artifacts") if isinstance(man.get("artifacts"), dict) else {}

    # A file's state: against the hash the manifest recorded for it; a file the manifest does not list (or with no
    # manifest) is `same` only where another file's header recorded its hash, and `not_recorded` otherwise.
    edges, nodes = [], {}
    for a in arts:
        want = (listed.get(a["path"]) or {}).get("sha256")
        state = checks.hash_state(want, a["sha256"])
        nodes[a["path"]] = ["analysis file", None if state == "not_recorded" else state]
    for a in arts:
        for i in a["inputs"]:
            edges.append([a["path"], i["path"], i["state"], _short(i["sha256"]), _short(i["now"])])
            node = nodes.setdefault(i["path"], ["harness file outside the run folder" if i["state"] == "outside"
                                                else "input in the run folder", i["state"]])
            if i["state"] in checks.STALE or node[1] is None:
                node[1] = i["state"]
    for node in nodes.values():
        node[1] = node[1] or "not_recorded"
    unlisted = sum(a["path"] not in listed for a in arts)
    source = ("One row per harness-written analysis file (analysis/manifest.json and each file's header). state "
              "compares the file with the hash the manifest recorded"
              + (f"; {unlisted} file(s) the manifest does not list are compared with the hash another file's header "
                 "recorded for them, and are 'not recorded' where there is none." if unlisted else ".")
              if listed else
              f"One row per analysis/*.yaml file found in the run folder, read from each file's own header: "
              f"{man_why or 'analysis/manifest.json lists no files'}, so no manifest hash can be compared. state is "
              "'same' where another file's header recorded this file's hash and it still matches, and 'not recorded' "
              "where no file recorded one.")

    out["facts"] = {
        "status": st.get("status"), "state": st.get("state"),
        "ended_because": hist[-1]["why"] if hist else None,
        "pipeline": meta.get("pipeline") or "unknown", "launcher": meta.get("launcher"),
        "launch_commit": _short(meta.get("commit"), 7),
        "launch_commit_in_repository": {True: "in repository", False: "not in repository"}.get(known, "not checked"),
        "round": st.get("round"), "max_rounds": st.get("max_rounds"),
        "model_spend_usd": st.get("spent_usd"), "budget_usd": st.get("budget_usd"),
        "spec_on_disk": _short(run.sha("decision_spec.yaml")), "spec_signed": _short(signed.get("decision_spec.yaml")),
        "signed_by": so.get("signed_by"), "signed_at": so.get("signed_at"),
        "transitions": len(hist), "tasks": len(tasks),
        "failed_tasks": ", ".join(t["key"] for t in tasks if t["ok"] is False) or "none",
        "analysis_files": len(arts),
        "files_with_changed_input": len({e[0] for e in edges if e[2] in checks.STALE}),
        "last_file_written": (f"{wr['ago']} ago ({wr['file']}, {wr['utc']})" if wr["ago"] else None),
        "working_minutes": round(sum(r[3] or 0 for r in stages), 1),
        "waiting_for_owner_minutes": round(sum(r[4] or 0 for r in stages), 1)}
    out["notes"]["facts"] = ("Read from run_state.json (status, state, history, journal, spent_usd, budget_usd), "
                             "metadata.json (pipeline, commit, launcher), decision_spec.signoff.json (sha256) and the "
                             "header of each analysis/*.yaml. Hashes are shown by their first 8 characters. Last file written "
                             "is the newest modification time among the files of the run folder. Model spend is "
                             "language-model spend only; cloud compute is not recorded in the run folder.")
    out["tables"] = {
        "history": {"about": "One row per transition of the stage machine, oldest first (run_state.json, history).",
                    "columns": ["at", "from", "to", "why", "round", "minutes", "model_spend_usd", "spent_so_far_usd"],
                    "rows": [[h["at"], h["from"], h["to"], h["why"], h["round"], h["minutes"], h["usd"], h["spent_usd"]]
                             for h in hist]},
        "stages": {"about": "The history added up by the stage each transition leaves. Time in a stage that waits "
                            "for the owner is counted apart from working time.",
                   "columns": checks.STAGE_COLUMNS, "rows": stages},
        "tasks": {"about": "One row per language-model task (run_state.json, journal); the key starts with the task's "
                           "stage. ok is false when the task failed. Times ending in Z are UTC.",
                  "columns": ["key", "task", "status", "ok", "started", "minutes", "model_spend_usd", "allowance_usd", "note"],
                  "rows": [[t["key"], t["task"], t["status"], t["ok"], t["started"], t["minutes"], t["usd"],
                            t["allowance_usd"], t["note"][:300]] for t in tasks]},
        "artifacts": {"about": source,
                      "columns": ["file", "kind", "producer", "engine", "written_at", "spec_used", "inputs",
                                  "changed_inputs", "state"],
                      "rows": [[a["path"], a["kind"], a["producer"], a["engine"], a["written_at"], _short(a["spec_sha256"]),
                                len(a["inputs"]), sum(i["state"] in checks.STALE for i in a["inputs"]),
                                nodes[a["path"]][1]] for a in arts]},
        "inputs": {"about": "One row per input an analysis file records (the inputs field of its header). state "
                            "compares the input on disk now with the hash recorded when the file was computed; "
                            "outside means a harness file that is not in the run folder and is not compared.",
                   "columns": ["file", "input", "state", "hash_when_computed", "hash_now"], "rows": edges},
        "files": {"about": "Every file in the chain: analysis files and the files they were computed from.",
                  "columns": ["file", "role", "state"], "rows": [[k, v[0], v[1]] for k, v in sorted(nodes.items())]}}
    out["charts"] = [
        {"kind": "graph", "table": "inputs", "nodes": "files", "x": "input", "y": "file", "group": "state",
         "title": "Harness-written files and the files each was computed from",
         "how_to_read": "Each box is a file; a line runs from an input, on the left, to the file computed from it. "
                        "Files further right depend on more steps. A box is marked same when the file still has its "
                        "recorded hash, changed when it differs, missing when it is gone, not recorded when no hash "
                        "of it was recorded, and outside when it is a harness file that is not in the run folder. A file computed from a changed input no longer "
                        "describes the files in the run folder."},
        {"kind": "bars", "table": "stages", "x": "stage", "y": "model_spend_usd",
         "title": "Model spend by stage",
         "how_to_read": "One bar per stage, in order of first visit; its length is the language-model spend, in US "
                        "dollars, of all visits to that stage."},
        {"kind": "bars", "table": "stages", "x": "stage", "y": "working_minutes",
         "title": "Working time by stage",
         "how_to_read": "One bar per stage; its length is the minutes spent in that stage over all visits. Stages "
                        "that wait for the owner have no bar: their waiting time is in the stages table."}]
    return out


def _self_test():
    import tempfile
    import workbench
    fails = []

    def ok(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    def rows(v, name):
        return [dict(zip(v["tables"][name]["columns"], r)) for r in v["tables"][name]["rows"]]

    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "run")
        os.makedirs(d)
        workbench._toy(d)
        v = view(workbench.Run(d))
        ok("S1 a finished run: the six tables, three charts the page can draw, and the facts of the run's own files",
           sorted(v["tables"]) == ["artifacts", "files", "history", "inputs", "stages", "tasks"]
           and workbench.chart_problems(v) == [] and len(v["charts"]) == 3 and v["facts"]["status"] == "completed"
           and v["facts"]["transitions"] == 6 and v["facts"]["analysis_files"] == 2, str(v["facts"]))
        ok("S2 with a manifest, the artifacts table names it as its source and every file with its recorded hash is same",
           "analysis/manifest.json" in v["tables"]["artifacts"]["about"] and "not in the run folder" not in v["tables"]["artifacts"]["about"]
           and {r["file"]: r["state"] for r in rows(v, "artifacts")} == {"analysis/point.yaml": "same", "analysis/claims.yaml": "same"},
           str(rows(v, "artifacts")))
        os.remove(os.path.join(d, "analysis", "manifest.json"))
        v = view(workbench.Run(d))
        about, state = v["tables"]["artifacts"]["about"], {r["file"]: r["state"] for r in rows(v, "artifacts")}
        ok("S3 without a manifest, the artifacts table says the manifest is not in the run folder and where its rows come from instead",
           "analysis/manifest.json is not in the run folder" in about and "each file's own header" in about, about)
        ok("S4 without a manifest, a file is same only where another file's header recorded its hash; otherwise its hash is not recorded",
           state == {"analysis/point.yaml": "same", "analysis/claims.yaml": "not_recorded"}
           and {r["file"]: r["state"] for r in rows(v, "files")}["analysis/claims.yaml"] == "not_recorded", str(state))
        open(os.path.join(d, "run_state.json"), "w").write("{not json")
        v = view(workbench.Run(d))
        ok("S5 fail closed: an unreadable run state gives no tables, a note saying why, and no exception",
           v["tables"] == {} and "not available" in v["notes"].get("not_available", "")
           and all(c["status"] == "not_available" for c in v["checks"] if c["id"] in ("steps.outcome", "steps.progress", "steps.cost")),
           str(v["notes"]))
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: steps.py --self-test")
