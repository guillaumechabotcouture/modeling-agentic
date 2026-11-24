#!/usr/bin/env python3
"""Operations records of the diagnostics contract (docs/design/diagnostic-data.md §4 C, S, P, B, M), read-only.

    python3 tools/diagnostics/diag_ops.py --self-test

Read from local caches that tools/diagnostics/follow.py refreshes (each with its fetch time; a cache older than
STALE_S is flagged stale), the pulled supervisor status, the ops notes' metrics files, the change fragments' smoke
markers, and this machine's process table. Files are read through lib.safe_io (no link followed). The process and
worktree listings run local commands only (ps, pmset, git worktree list) and return command names, never arguments
or environments. Each builder returns (data, sources, notes, derived), as diag_runs.py does.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
KEEP = os.path.expanduser("~/modeling-agentic-keep")
STALE_S = 30 * 60.0               # 3 x follow.py's default 10-minute pass
SHELLS = {"zsh", "bash", "sh", "dash", "fish"}


def home() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_HOME") or os.path.join(KEEP, "diagnostics"))


def keep() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_KEEP") or KEEP)


def _read(base: str, rel: str, max_bytes: int = 8 << 20):
    """(parsed JSON, mtime ISO, why) of base/rel, no link followed; (None, None, why) when unreadable."""
    try:
        from lib.safe_io import read_nofollow
        b, st = read_nofollow(base, rel, max_bytes=max_bytes)
        return json.loads(b), datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"), None
    except FileNotFoundError:
        return None, None, f"{rel} not found under {base} (run tools/diagnostics/follow.py)"
    except Exception as e:  # noqa: BLE001
        return None, None, f"{rel}: {type(e).__name__}"


def _stale(fetched_at, now: float) -> bool:
    try:
        return now - datetime.fromisoformat(str(fetched_at)).timestamp() > STALE_S
    except (TypeError, ValueError):
        return True


def _cached(rel: str, now=None):
    now = time.time() if now is None else now
    doc, mt, why = _read(os.path.join(home(), "cache"), rel)
    if doc is None:
        return None, [], [why]
    stale = _stale(doc.get("fetched_at"), now)
    src = [{"path": f"cache/{rel}", "mtime": mt, "fetched_at": doc.get("fetched_at"), "stale": stale}]
    return doc, src, ([f"stale: {rel} was fetched at {doc.get('fetched_at')}; follow.py may not be running"]
                      if stale else [])


def launches(now=None):
    out, srcs, notes = [], [], []
    for p in sorted(glob.glob(os.path.join(home(), "cache", "launches", "*.json"))):
        doc, s, n = _cached(f"launches/{os.path.basename(p)}", now)
        srcs, notes = srcs + s, notes + n
        if doc:
            cap = doc.get("cap_projection") or {}
            labels = (doc.get("assignments") or {}) if isinstance(doc.get("assignments"), dict) else {}
            out.append({"config": doc.get("config"), "labels": sorted(k for k in labels if isinstance(labels[k], dict)),
                        "clusters": doc.get("clusters"),
                        "cap": {k: cap.get(k) for k in ("cap_usd", "level", "spend_usd", "burn_usd_per_h",
                                                       "hours_to_cap", "projected_usd") if k in cap} or None})
    b, s, n = _cached("budget_status.json", now)
    scopes = ((b or {}).get("status") or {}).get("scopes") or []
    keys = ("scope", "budget_usd", "used_usd", "billed_usd", "reserved_usd", "remaining_usd", "expires_utc")
    return ({"launches": out, "scopes": [{k: sc.get(k) for k in keys} for sc in scopes if isinstance(sc, dict)]},
            srcs + s, notes + n + ([] if out else ["no launch records cached"]), {"cap": "record"})


def clusters(now=None):
    b, s, n = _cached("budget_status.json", now)
    rows = []
    for sc in ((b or {}).get("status") or {}).get("scopes") or []:
        for r in (sc or {}).get("clusters") or []:
            if isinstance(r, dict):
                rows.append({"scope": sc.get("scope"), **{k: r.get(k) for k in ("name", "id", "state", "billed_usd",
                                                                                  "estimate_usd", "usd",
                                                                                  "live_instances")}})
    live = [r for r in rows if (r.get("live_instances") or 0) > 0]
    return {"clusters": rows, "live": len(live)}, s, n, {"usd": "record (budget.py: max of billed and estimate)"}


def supervisor_status(now=None):
    doc, mt, why = _read(os.path.join(keep(), "supervisor"), "status.json")
    if doc is None:
        return None, [], [why], {}
    now = time.time() if now is None else now
    stale = _stale(mt, now)
    cut = {k: (v[-50:] if isinstance(v, list) else v) for k, v in doc.items()}
    return cut, [{"path": "supervisor/status.json", "mtime": mt, "stale": stale}], (
        [f"stale: the pulled status is from {mt}"] if stale else []), {}


def _ci(rollup) -> str:
    states = [str(c.get("conclusion") or c.get("state") or "PENDING").upper() for c in rollup or [] if isinstance(c, dict)]
    if any(x in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED") for x in states):
        return "failing"
    if any(x in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "") for x in states):
        return "pending"
    return "passing" if states else "none"


def open_prs(now=None):
    doc, s, n = _cached("gh_prs.json", now)
    if doc is None or doc.get("prs") is None:
        return None, s, n + ([doc.get("why")] if doc else []), {}
    rows = [{"number": p.get("number"), "title": p.get("title"), "branch": p.get("headRefName"),
             "head": str(p.get("headRefOid") or "")[:12], "draft": p.get("isDraft"), "review": p.get("reviewDecision"),
             "ci": _ci(p.get("statusCheckRollup")), "updated": p.get("updatedAt"), "url": p.get("url")}
            for p in doc["prs"] if isinstance(p, dict)]
    return {"prs": rows, "fetched_at": doc.get("fetched_at")}, s, n, {"ci": "inferred (from the check rollup)"}


def baseline_summary():
    out, srcs = [], []
    files = sorted(glob.glob(os.path.join(keep(), "ops", "hb*_triage_data", "hb*_metrics.json")))
    files += [os.path.join(keep(), "supervisor", "metrics.json")]
    for p in files:
        base, rel = os.path.dirname(p), os.path.basename(p)
        doc, mt, why = _read(base, rel)
        if doc is None:
            continue
        name = rel.split("_metrics")[0] if "_metrics" in rel else "supervisor"
        cols = ("label", "status", "completes", "class", "question", "rounds", "model_usd", "stop_state")
        out.append({"baseline": name, "summary": doc.get("summary"),
                    "rows": [{k: r.get(k) for k in cols} for r in doc.get("rows") or [] if isinstance(r, dict)]})
        srcs.append({"path": os.path.relpath(p, keep()), "mtime": mt})
    return {"baselines": out}, srcs, [] if out else ["no baseline metrics found under the ops notes"], {}


def check_value():
    out, srcs = [], []
    for p in sorted(glob.glob(os.path.join(home(), "cache", "check_value", "*.json"))):
        doc, mt, why = _read(os.path.dirname(p), os.path.basename(p))
        if isinstance(doc, dict):
            out.append({"baseline": os.path.basename(p)[:-5], "checks": doc.get("checks"), "method": doc.get("method")})
            srcs.append({"path": f"cache/check_value/{os.path.basename(p)}", "mtime": mt})
    return {"by_baseline": out}, srcs, [] if out else [
        "no check-value table cached: save `scripts/harness_metrics.py --check-value <runs> --json "
        "<home>/cache/check_value/<baseline>.json`"], {}


def fragment_markers(repo: str = REPO) -> list[dict]:
    """[{fragment, goal, markers}] of every change fragment that declares markers (scripts/assemble_changes.py)."""
    import assemble_changes as AC
    out = []
    for fr in AC.load_fragments(repo):
        if fr.get("markers") or fr.get("goal"):
            out.append({"fragment": fr["name"], "goal": fr.get("goal"), "markers": fr.get("markers") or []})
    return out


def check_marker(run, spec: str) -> str:
    """present | missing | unsupported, for one smoke_check spec against a workbench Run (confined reads)."""
    import smoke_check as SC
    m = SC.parse_spec(spec)
    if re.search(r"[*?\[]", m["path"]):
        return "unsupported (a glob path)"
    b, why = run.read(m["path"], 8 << 20)
    if b is None:
        return "missing"
    if m["kind"] == "exists":
        return "present"
    if m["kind"] == "regex":
        return "present" if re.search(m["value"], b.decode("utf-8", "replace")) else "missing"
    try:                       # parsed as smoke_check._load parses a file, from the confined read
        text = b.decode("utf-8", "replace")
        if m["path"].endswith((".yaml", ".yml")):
            import yaml
            obj = yaml.safe_load(text)
        elif m["path"].endswith(".jsonl"):
            obj = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        else:
            obj = json.loads(text)
        v = SC._get(obj, m["key"])
    except Exception:  # noqa: BLE001
        return "missing"
    if v is SC._MISSING:
        return "missing"
    if m["kind"] == "field":
        return "present" if v is not None else "missing"
    t = SC._text(v)
    return "present" if (t == m["value"] if m["kind"] == "equals" else t.startswith(m["value"])) else "missing"


def smoke_markers(runs: dict, run_ids=None, fragment=None):
    frs = [f for f in fragment_markers() if f["markers"] and (fragment is None or f["fragment"].endswith(str(fragment)))]
    ids = [r for r in (run_ids or sorted(runs)) if r in runs][:30]
    rows = [{"fragment": f["fragment"], "goal": f["goal"], "run": rid,
             "markers": {m: check_marker(runs[rid], m) for m in f["markers"]}} for f in frs for rid in ids]
    return {"results": rows}, [], ([] if frs else ["no change fragment declares markers yet"]), {}


def _cmd(argv, timeout=10):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def machine_status(ps_out=None, worktrees_out=None):
    """Load, thermal state, processes by command name (never arguments), worktrees, and active agents."""
    ps_out = _cmd(["ps", "-axo", "pcpu=,pmem=,comm="]) if ps_out is None else ps_out
    procs, top = {}, []
    for ln in ps_out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) < 3:
            continue
        name = os.path.basename(parts[2].strip()).lstrip("-") or "?"
        procs[name] = procs.get(name, 0) + 1
        try:
            top.append({"command": name, "cpu": float(parts[0]), "mem": float(parts[1])})
        except ValueError:
            continue
    top.sort(key=lambda r: -r["cpu"])
    wt = _cmd(["git", "-C", REPO, "worktree", "list", "--porcelain"]) if worktrees_out is None else worktrees_out
    trees, cur = [], {}
    for ln in wt.splitlines() + [""]:
        if ln.startswith("worktree "):
            cur = {"path": ln[9:]}
        elif ln.startswith("branch "):
            cur["branch"] = ln[7:].replace("refs/heads/", "")
        elif not ln.strip() and cur:
            trees.append(cur)
            cur = {}
    agents = {}
    try:
        from lib.safe_io import read_nofollow
        for line in read_nofollow(home(), "agents.jsonl")[0].decode("utf-8", "replace").split("\n"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("name"):
                agents[r["name"]] = r
    except Exception:  # noqa: BLE001 -- no agent log yet
        pass
    therm = _cmd(["pmset", "-g", "therm"]) if sys.platform == "darwin" else ""
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = None
    return ({"load": load, "cpus": os.cpu_count(), "thermal": [x.strip() for x in therm.splitlines() if x.strip()][:6],
             "processes": dict(sorted(procs.items(), key=lambda kv: -kv[1])[:25]),
             "shells": sum(n for k, n in procs.items() if k in SHELLS), "top": top[:10], "worktrees": trees,
             "agents_active": [a for a in agents.values() if a.get("event") == "start"]},
            [], [] if agents else ["no agents.jsonl: the lead's agent log is not written yet"], {"processes": "record"})


def owner_items(now=None) -> list[dict]:
    """PRs ready for the owner (CI passing, not draft) and budgets near their end, for owner_inbox."""
    items = []
    prs = (open_prs(now)[0] or {}).get("prs") or []
    for p in prs:
        if p["ci"] == "passing" and not p["draft"]:
            items.append({"where": "pr", "ref": f"#{p['number']}", "question": "CI passing: review or merge",
                          "since": p["updated"], "how_to_answer": p["url"]})
    now = time.time() if now is None else now
    for sc in launches(now)[0]["scopes"]:
        try:
            left_days = (datetime.fromisoformat(str(sc.get("expires_utc")).replace("Z", "+00:00")).timestamp() - now) / 86400
        except (TypeError, ValueError):
            left_days = None
        low = sc.get("budget_usd") and sc.get("remaining_usd") is not None and sc["remaining_usd"] < 0.1 * sc["budget_usd"]
        if (left_days is not None and left_days < 3) or low:
            items.append({"where": "budget", "ref": sc.get("scope"), "since": sc.get("expires_utc"),
                          "question": "budget near its end (under 3 days or 10% left)",
                          "how_to_answer": f"python3 cloud/budget.py set --scope {sc.get('scope')} --usd <amount> --note ..."})
    return items


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, label):
        print(("  ok   " if c else "  FAIL ") + label)
        if not c:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        os.environ["MA_DIAG_HOME"], os.environ["MA_DIAG_KEEP"] = os.path.join(td, "home"), os.path.join(td, "keep")
        cache = os.path.join(td, "home", "cache")
        os.makedirs(os.path.join(cache, "launches"))
        now = time.time()
        fresh = datetime.fromtimestamp(now - 60, timezone.utc).isoformat()
        old = datetime.fromtimestamp(now - 7200, timezone.utc).isoformat()
        soon = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
        json.dump({"fetched_at": fresh, "status": {"scopes": [{"scope": "s", "budget_usd": 10.0, "remaining_usd": 5.0,
                                                               "expires_utc": soon, "clusters": [
                                                                   {"name": "c1", "id": 1, "state": "running",
                                                                    "usd": 0.5, "live_instances": 2}]}]}},
                  open(os.path.join(cache, "budget_status.json"), "w"))
        json.dump({"fetched_at": old, "config": "cfg", "assignments": {"R1": {"launch_id": "L"}},
                   "cap_projection": {"cap_usd": 5, "level": "under_cap"}},
                  open(os.path.join(cache, "launches", "cfg.json"), "w"))
        json.dump({"fetched_at": fresh, "prs": [
            {"number": 1, "title": "t", "isDraft": False, "statusCheckRollup": [{"conclusion": "SUCCESS"}],
             "url": "u1"},
            {"number": 2, "title": "t", "isDraft": False, "statusCheckRollup": [{"conclusion": "FAILURE"},
                                                                               {"status": "IN_PROGRESS"}]},
            {"number": 3, "title": "t", "isDraft": False, "statusCheckRollup": [{"state": "PENDING"}]}]},
                  open(os.path.join(cache, "gh_prs.json"), "w"))
        la = launches(now)
        ok(la[0]["launches"][0]["labels"] == ["R1"] and la[0]["launches"][0]["cap"]["level"] == "under_cap"
           and any("stale" in n for n in la[2]) and la[0]["scopes"][0]["scope"] == "s",
           f"DO1 launches joins the cached records and flags a stale one ({la[2]})")
        cl = clusters(now)[0]
        ok(cl["live"] == 1 and cl["clusters"][0]["name"] == "c1", "DO2 clusters from the cached budget status")
        pr = open_prs(now)[0]["prs"]
        ok([p["ci"] for p in pr] == ["passing", "failing", "pending"], f"DO3 CI state per PR ({[p['ci'] for p in pr]})")
        items = owner_items(now)
        ok([i["where"] for i in items] == ["pr", "budget"], f"DO4 owner items: a ready PR and a budget ending soon ({items})")
        ok(supervisor_status(now)[0] is None and "follow.py" in supervisor_status(now)[2][0],
           "DO5 no pulled supervisor status: unavailable, with what to run")
        os.makedirs(os.path.join(td, "keep", "ops", "hb9_triage_data"))
        json.dump({"summary": {"chains": 2}, "rows": [{"label": "x", "status": "completed", "secret_field": 1}]},
                  open(os.path.join(td, "keep", "ops", "hb9_triage_data", "hb9_metrics.json"), "w"))
        bs = baseline_summary()[0]["baselines"]
        ok(bs[0]["baseline"] == "hb9" and "secret_field" not in bs[0]["rows"][0], "DO6 baselines: summary and listed columns")
        ms = machine_status("12.5 1.0 /bin/zsh\n3.0 0.5 -zsh\n50.0 4.0 /usr/bin/python3.12\n",
                            "worktree /a\nbranch refs/heads/main\n\nworktree /b\nbranch refs/heads/x\n")[0]
        ok(ms["shells"] == 2 and ms["top"][0]["command"] == "python3.12" and ms["worktrees"][1] == {"path": "/b", "branch": "x"},
           f"DO7 machine status: shells, top commands by name only, worktrees ({ms['shells']}, {ms['top'][:1]})")
        sys.path.insert(0, os.path.join(REPO, "tools", "workbench"))
        import workbench as WB
        rd = os.path.join(td, "run")
        os.makedirs(rd)
        json.dump({"history": [{"to": "SPEC"}], "status": "completed"}, open(os.path.join(rd, "run_state.json"), "w"))
        open(os.path.join(rd, "events.jsonl"), "w").write('{"kind": "stage"}\n')
        run = WB.Run(rd)
        got = [check_marker(run, s) for s in ("run_state.json:history", "run_state.json:status=completed",
                                              'events.jsonl~"kind": "stage"', "missing.json", "run_state.json:nope",
                                              "analysis/*.yaml")]
        ok(got == ["present", "present", "present", "missing", "missing", "unsupported (a glob path)"],
           f"DO8 smoke markers checked through the confined reader ({got})")
    for k in ("MA_DIAG_HOME", "MA_DIAG_KEEP"):
        os.environ.pop(k, None)
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
