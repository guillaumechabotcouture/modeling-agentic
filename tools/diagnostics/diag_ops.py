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
CAP_KEYS = ("at", "usd", "cap_usd", "level", "usd_per_hour", "hours_to_cap", "hours_to_far_over_cap",
            "projected_usd_at_deadline", "projected_usd_at_watch_limit", "invocation")
PASSING = ("SUCCESS", "NEUTRAL", "SKIPPED")
SHELLS = {"zsh", "bash", "sh", "dash", "fish"}


def home() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_HOME") or os.path.join(KEEP, "diagnostics"))


def keep() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_KEEP") or KEEP)


def _read(base: str, rel: str, max_bytes: int = 8 << 20):
    """(parsed JSON, mtime ISO, why) of base/rel, no link followed at any component of `rel` (callers pass the
    configured root as `base` and the whole path below it: Codex r1 of #388); (None, None, why) when unreadable."""
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
    doc, mt, why = _read(home(), f"cache/{rel}")
    if doc is None:
        return None, [], [why]
    if not isinstance(doc, dict):          # a malformed cache is unavailable, never a crash (P2-1)
        return None, [], [f"cache/{rel} is not the expected object"]
    stale = _stale(doc.get("fetched_at"), now)
    src = [{"path": f"cache/{rel}", "mtime": mt, "fetched_at": doc.get("fetched_at"), "stale": stale}]
    notes = ([f"stale: {rel} was fetched at {doc.get('fetched_at')}; follow.py may not be running"] if stale else [])
    if doc.get("last_error"):            # follow.py kept the last good snapshot when a refresh failed
        notes.append(f"the last refresh of {rel} failed at {doc.get('last_error_at')}: {str(doc['last_error'])[:160]}")
    if doc.get("truncated"):
        notes.append(f"{rel} holds only the first rows the fetch allowed: some items may be missing")
    return doc, src, notes


def launches(now=None):
    out, srcs, notes = [], [], []
    for p in sorted(glob.glob(os.path.join(home(), "cache", "launches", "*.json"))):
        doc, s, n = _cached(f"launches/{os.path.basename(p)}", now)
        srcs, notes = srcs + s, notes + n
        if doc:
            cap = doc.get("cap_projection") if isinstance(doc.get("cap_projection"), dict) else {}
            labels = (doc.get("assignments") or {}) if isinstance(doc.get("assignments"), dict) else {}
            out.append({"config": doc.get("config"), "labels": sorted(k for k in labels if isinstance(labels[k], dict)),
                        "clusters": doc.get("clusters"),
                        # the producer's names (cloud/budget.py cap_projection, cloud/launch_watch.py): Codex r1 of #388
                        "cap": {k: cap.get(k) for k in CAP_KEYS if k in cap} or None})
    b, s, n = _cached("budget_status.json", now)
    scopes = _scopes(b)
    keys = ("scope", "budget_usd", "used_usd", "billed_usd", "reserved_usd", "remaining_usd", "expires_utc")
    return ({"launches": out, "scopes": [{k: sc.get(k) for k in keys} for sc in scopes if isinstance(sc, dict)]},
            srcs + s, notes + n + ([] if out else ["no launch records cached"]), {"cap": "record"})


def _scopes(b) -> list:
    st = (b or {}).get("status")
    sc = st.get("scopes") if isinstance(st, dict) else None
    return [x for x in sc if isinstance(x, dict)] if isinstance(sc, list) else []


def clusters(now=None):
    b, s, n = _cached("budget_status.json", now)
    if b is not None and not _scopes(b):
        n = n + [f"the cached budget status holds no readable scope ({str((b.get('status') or {}).get('why'))[:120]})"]
    rows = []
    for sc in _scopes(b):
        for r in (sc or {}).get("clusters") or []:
            if isinstance(r, dict):
                rows.append({"scope": sc.get("scope"), **{k: r.get(k) for k in ("name", "id", "state", "billed_usd",
                                                                                  "estimate_usd", "usd",
                                                                                  "live_instances")}})
    live = [r for r in rows if (r.get("live_instances") or 0) > 0]
    return {"clusters": rows, "live": len(live)}, s, n, {"usd": "record (budget.py: max of billed and estimate)"}


def supervisor_status(now=None):
    doc, mt, why = _read(keep(), "supervisor/status.json")
    if not isinstance(doc, dict):
        return None, [], [why or "supervisor/status.json is not an object"], {}
    now = time.time() if now is None else now
    hb = str(doc.get("heartbeat_utc") or "").replace("Z", "+00:00")
    try:
        hb_age = round(now - datetime.fromisoformat(hb).timestamp(), 1)
    except ValueError:
        hb_age = None
    stale = _stale(mt, now)
    late = hb_age is None or hb_age > STALE_S
    cut = {k: (v[-50:] if isinstance(v, list) else v) for k, v in doc.items()}
    cut["omitted"] = {k: len(v) - 50 for k, v in doc.items() if isinstance(v, list) and len(v) > 50}
    cut["heartbeat_age_s"] = hb_age
    notes = ([f"stale: the local copy is from {mt}"] if stale else []) + (
        [f"the supervisor's own heartbeat is {'unknown' if hb_age is None else f'{hb_age / 60:.0f} min old'}: it may "
         "have stopped"] if late else [])
    return cut, [{"path": "supervisor/status.json", "mtime": mt, "stale": stale}], notes, {"heartbeat_age_s": "record"}


def _ci(rollup) -> str:
    states = [str(c.get("conclusion") or c.get("state") or "PENDING").upper() for c in rollup or [] if isinstance(c, dict)]
    if any(x in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED") for x in states):
        return "failing"
    if any(x in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "") for x in states):
        return "pending"
    if any(x not in PASSING for x in states):       # cancelled, stale, startup failure: not passing (Codex r1 of #388)
        return "incomplete"
    return "passing" if states else "none"


def open_prs(now=None):
    doc, s, n = _cached("gh_prs.json", now)
    if doc is None or not isinstance(doc.get("prs"), list):
        return None, s, n + ([str(doc.get("why"))] if doc else []), {}
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
        rel = os.path.relpath(p, keep())
        doc, mt, why = _read(keep(), rel)
        if not isinstance(doc, dict):
            continue
        name = os.path.basename(rel).split("_metrics")[0] if "_metrics" in rel else "supervisor"
        cols = ("label", "status", "completes", "class", "question", "rounds", "model_usd", "stop_state")
        out.append({"baseline": name, "summary": doc.get("summary"),
                    "rows": [{k: r.get(k) for k in cols} for r in doc.get("rows") or [] if isinstance(r, dict)]})
        srcs.append({"path": rel, "mtime": mt})
    return {"baselines": out}, srcs, [] if out else ["no baseline metrics found under the ops notes"], {}


def check_value(now=None):
    out, srcs = [], []
    for p in sorted(glob.glob(os.path.join(home(), "cache", "check_value", "*.json"))):
        doc, mt, why = _read(home(), f"cache/check_value/{os.path.basename(p)}")
        if isinstance(doc, dict):
            out.append({"baseline": os.path.basename(p)[:-5], "checks": doc.get("checks"), "method": doc.get("method")})
            srcs.append({"path": f"cache/check_value/{os.path.basename(p)}", "mtime": mt,
                         "stale": _stale(mt, time.time() if now is None else now)})
    notes = [f"stale: {x['path']} is from {x['mtime']}" for x in srcs if x["stale"]]
    return {"by_baseline": out}, srcs, notes if out else [
        "no check-value table cached: once `scripts/harness_metrics.py --check-value` is merged, save its "
        "`--json` output as <home>/cache/check_value/<baseline>.json"], {}


def fragment_markers(repo: str = REPO) -> list[dict]:
    """[{fragment, goal, markers}] of every change fragment whose front matter names markers or a goal (the format
    #382 adds; scripts/assemble_changes.py validates it). Read from the header itself, a fragment at a time."""
    import yaml
    out = []
    for p in sorted(glob.glob(os.path.join(repo, "changes", "*.md"))):
        if os.path.basename(p).upper() == "README.MD" or os.path.islink(p):
            continue
        try:
            lines = open(p, encoding="utf-8").read().splitlines()
            end = lines.index("---", 1) if lines and lines[0].strip() == "---" else None
            head = yaml.safe_load("\n".join(lines[1:end])) if end else None
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(head, dict):
            continue
        ms = [m for m in head.get("markers") or [] if isinstance(m, str)] if isinstance(head.get("markers"), list) else []
        if ms or head.get("goal"):
            out.append({"fragment": f"changes/{os.path.basename(p)}", "goal": head.get("goal"), "markers": ms})
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
    if m["kind"] == "regex":             # as scripts/smoke_check.py check_marker: re.M
        return "present" if re.search(m["value"], b.decode("utf-8", "replace"), re.M) else "missing"
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
    if v is SC._MISSING or v is None:     # as scripts/smoke_check.py: a null field is a missing marker
        return "missing"
    if m["kind"] == "field":
        return "present"
    t = SC._text(v)
    return "present" if (t == m["value"] if m["kind"] == "equals" else t.startswith(m["value"])) else "missing"


def smoke_markers(runs: dict, run_ids=None, fragment=None):
    frs = [f for f in fragment_markers() if f["markers"] and (fragment is None or f["fragment"].endswith(str(fragment)))]
    want = [r for r in (run_ids or sorted(runs)) if r in runs]
    ids = want[:30]
    rows = [{"fragment": f["fragment"], "goal": f["goal"], "run": rid,
             "markers": {m: check_marker(runs[rid], m) for m in f["markers"]}} for f in frs for rid in ids]
    notes = ([] if frs else ["no change fragment declares markers yet"]) + (
        [f"{len(want) - 30} more run(s) not checked: pass `runs` to choose them"] if len(want) > 30 else [])
    return {"results": rows, "runs_omitted": max(0, len(want) - 30)}, [], notes, {}


def _cmd(argv, timeout=10):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def machine_status(ps_out=None, worktrees_out=None):
    """Load, thermal state, processes by command name (never arguments), worktrees, and active agents."""
    ps_out = _cmd(["ps", "-axo", "pcpu=,pmem=,comm="]) if ps_out is None else ps_out
    ps_ok = bool(ps_out.strip())
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
             "processes": dict(sorted(procs.items(), key=lambda kv: -kv[1])[:25]) if ps_ok else None,
             "shells": sum(n for k, n in procs.items() if k in SHELLS) if ps_ok else None,
             "top": top[:10], "worktrees": trees,
             "agents_active": [a for a in agents.values() if a.get("event") == "start"]},
            [], ([] if agents else ["no agents.jsonl: the lead's agent log is not written yet"])
            + ([] if ps_ok else ["the process listing failed: process counts are unavailable"]),
            {"processes": "record" if ps_ok else "unavailable"})


def owner_items(now=None):
    """(items, sources, notes): PRs ready for the owner (CI passing, not draft) and budgets near their end, for
    owner_inbox, with the caches' freshness and availability (Codex r1 of #388)."""
    items = []
    pr_doc, srcs, notes, _ = open_prs(now)
    if pr_doc is None:
        notes = notes + ["open PRs unavailable: no PR items"]
    prs = (pr_doc or {}).get("prs") or []
    for p in prs:
        if p["ci"] == "passing" and not p["draft"]:
            items.append({"where": "pr", "ref": f"#{p['number']}", "question": "CI passing: review or merge",
                          "since": p["updated"], "how_to_answer": p["url"]})
    now = time.time() if now is None else now
    la = launches(now)
    srcs, notes = srcs + [x for x in la[1] if "budget_status" in str(x.get("path"))], notes + [
        x for x in la[2] if "budget_status" in x or "launch records" not in x]
    for sc in la[0]["scopes"]:
        try:
            left_days = (datetime.fromisoformat(str(sc.get("expires_utc")).replace("Z", "+00:00")).timestamp() - now) / 86400
        except (TypeError, ValueError):
            left_days = None
        low = sc.get("budget_usd") and sc.get("remaining_usd") is not None and sc["remaining_usd"] < 0.1 * sc["budget_usd"]
        if (left_days is not None and left_days < 3) or low:
            items.append({"where": "budget", "ref": sc.get("scope"), "since": None, "deadline": sc.get("expires_utc"),
                          "question": "budget near its end (under 3 days or 10% left)",
                          "how_to_answer": f"python3 cloud/budget.py set --scope {sc.get('scope')} --usd <amount> --note ..."})
    return items, srcs, notes


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
        items = owner_items(now)[0]
        ok([i["where"] for i in items] == ["pr", "budget"] and items[1]["deadline"] == soon and items[1]["since"] is None,
           f"DO4 owner items: a ready PR and a budget ending soon, its expiry a deadline ({items})")
        ok(_ci([{"conclusion": "CANCELLED"}]) == "incomplete", "DO4b a cancelled check is not passing")
        json.dump([], open(os.path.join(cache, "gh_prs.json"), "w"))
        oi = owner_items(now)
        ok([i["where"] for i in oi[0]] == ["budget"] and any("unavailable" in n for n in oi[2]),
           "DO4c a malformed PR cache is unavailable, with a note, and the other items stay")
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
        open(os.path.join(rd, "report.md"), "w").write("heading\nready\nmore\n")
        json.dump({"paused": None}, open(os.path.join(rd, "st.json"), "w"))
        ok(check_marker(run, "report.md~^ready$") == "present" and check_marker(run, "st.json:paused=null") == "missing",
           "DO9 the smoke checker's semantics: multiline regex, a null field is missing")
        os.makedirs(os.path.join(td, "elsewhere"))
        json.dump({"heartbeat_utc": "2026-01-01T00:00:00Z"}, open(os.path.join(td, "elsewhere", "status.json"), "w"))
        os.symlink(os.path.join(td, "elsewhere"), os.path.join(td, "keep", "supervisor"))
        ok(supervisor_status(now)[0] is None, "DO10 a linked supervisor folder is not followed")
        os.remove(os.path.join(td, "keep", "supervisor"))
        os.makedirs(os.path.join(td, "keep", "supervisor"))
        json.dump({"heartbeat_utc": "2026-01-01T00:00:00Z", "paused": list(range(51))},
                  open(os.path.join(td, "keep", "supervisor", "status.json"), "w"))
        sv = supervisor_status(now)
        ok(sv[0]["omitted"] == {"paused": 1} and sv[0]["heartbeat_age_s"] > STALE_S and any("heartbeat" in n for n in sv[2]),
           "DO11 a fresh copy of an old heartbeat says the supervisor may have stopped; cut lists say how much")
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
