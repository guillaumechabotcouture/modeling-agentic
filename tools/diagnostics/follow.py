#!/usr/bin/env python3
"""Refresh the diagnostics server's local copies (docs/design/diagnostic-data.md §5): the one part that uses the network.

    python3 tools/diagnostics/follow.py --config cloud/configs/<x>.py [--config ...] [--every 10] [--once]
    python3 tools/diagnostics/follow.py --self-test

Each pass, read-only towards everything remote:
  - mirror: for each config's labels, the latest checkpoint of the latest launch, downloaded and sha256-verified with
    cloud/run_cluster.py's own helpers (`checkpoint_record`, `fetch_checkpoint`) into <home>/../mirror/<config name>/
    <label>/checkpoint/, which the server reads as runs. A label whose stored generation is the one already mirrored
    is skipped. Older local copies are removed (only inside the mirror). The storage account is the environment's
    ($MA_STORAGE_ACCOUNT or $COILED_STORAGE_ACCOUNT), else the config's.
  - caches, each with its fetch time, under <home>/cache/: `cloud/budget.py status --json` (budget_status.json),
    `gh pr list --json ...` (gh_prs.json), each config's local launch records (launches/<name>.json: clusters.json,
    assignments.json, the latest cap projection), and `cloud/supervisor.py pull` into ~/modeling-agentic-keep/supervisor.
A failing part is recorded in <home>/cache/follow_status.json and never stops the others. Nothing is launched,
stopped, attached or written remotely.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
KEEP = os.path.expanduser("~/modeling-agentic-keep")
PR_FIELDS = "number,title,headRefName,headRefOid,isDraft,reviewDecision,statusCheckRollup,updatedAt,url"


def home() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_HOME") or os.path.join(KEEP, "diagnostics"))


def mirror_root() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_MIRROR") or os.path.join(KEEP, "mirror"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _put(rel: str, obj) -> None:
    """A cache file under <home>/cache, atomically, with no link followed (lib.safe_io)."""
    sys.path.insert(0, os.path.join(REPO, "scripts")) if os.path.join(REPO, "scripts") not in sys.path else None
    from lib.safe_io import write_nofollow
    os.makedirs(os.path.join(home(), "cache"), exist_ok=True)
    write_nofollow(os.path.join(home(), "cache"), rel, json.dumps(obj, indent=1, default=str).encode(), atomic=True)


def _run(cmd: list, timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.returncode and p.stderr else "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, f"{type(e).__name__}: {e}"


def _load_cfg(path: str):
    sys.path.insert(0, os.path.join(REPO, "cloud")) if os.path.join(REPO, "cloud") not in sys.path else None
    import run_cluster as RC
    return RC, RC.load_config(path)


def mirror_label(RC, cfg, store, label: str, dest_root: str) -> str:
    """One label's latest checkpoint into dest_root/<label>/; returns what was done."""
    acct = os.environ.get("MA_STORAGE_ACCOUNT") or os.environ.get("COILED_STORAGE_ACCOUNT") or cfg.storage_account
    view = SimpleNamespace(**{k: getattr(cfg, k) for k in ("storage_prefix", "state_file", "name")},
                           storage_account=acct, out_dir=dest_root)
    launch_id = json.load(open(cfg.state_file))[label]["launch_id"]
    ptr = RC.checkpoint_record(view, store, label, launch_id)
    mine = os.path.join(dest_root, label, "mirror.json")      # follow.py's own record, beside the checkpoint
    try:
        have = json.load(open(mine)).get("generation")
    except (OSError, ValueError):
        have = None
    gen = (ptr or {}).get("generation")
    if gen is not None and gen == have:
        return f"generation {gen} already mirrored"
    rec = RC.fetch_checkpoint(view, store, label)
    with open(mine + ".tmp", "w") as fh:
        json.dump({"generation": gen, "launch_id": launch_id, "at": _now(), "time_utc": (rec or {}).get("time_utc")}, fh)
    os.replace(mine + ".tmp", mine)
    for old in glob.glob(os.path.join(dest_root, label, "checkpoint.before_*")):    # our own earlier copies only
        if os.path.realpath(old).startswith(os.path.realpath(dest_root) + os.sep) and not os.path.islink(old):
            shutil.rmtree(old, ignore_errors=True)
    tgz = os.path.join(dest_root, label, "checkpoint.tar.gz")
    if os.path.isfile(tgz) and not os.path.islink(tgz):
        os.remove(tgz)
    return f"generation {gen} of {(rec or {}).get('time_utc')} mirrored"


def launch_record(cfg) -> dict:
    """The config's own local launch records (no network)."""
    out = {"config": cfg.name, "out_dir": cfg.out_dir, "fetched_at": _now()}
    for key, path in (("clusters", cfg.clusters_file), ("assignments", cfg.state_file)):
        try:
            out[key] = json.load(open(path))
        except (OSError, ValueError) as e:
            out[key] = {"available": False, "why": f"{type(e).__name__}"}
    caps = sorted(glob.glob(os.path.join(cfg.out_dir, "cap_projection_*.json")), key=os.path.getmtime)
    try:
        out["cap_projection"] = json.load(open(caps[-1])) if caps else None
    except (OSError, ValueError):
        out["cap_projection"] = None
    return out


def once(configs: list, status: dict, runner=_run) -> dict:
    """One pass; every part's outcome in `status` (written to cache/follow_status.json)."""
    status.update(at=_now(), parts={})

    def part(name, fn):
        try:
            status["parts"][name] = {"ok": True, "note": str(fn())[:300], "at": _now()}
        except BaseException as e:  # noqa: BLE001 -- SystemExit from run_cluster included: the next part runs
            status["parts"][name] = {"ok": False, "note": f"{type(e).__name__}: {e}"[:300], "at": _now()}

    def budget():
        rc, out = runner([sys.executable, "cloud/budget.py", "status", "--json"])
        doc = json.loads(out) if rc == 0 else {"available": False, "why": out[-300:]}
        _put("budget_status.json", {"fetched_at": _now(), "status": doc})
        return "ok" if rc == 0 else f"exit {rc}"

    def prs():
        rc, out = runner(["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", PR_FIELDS])
        _put("gh_prs.json", {"fetched_at": _now(), "prs": json.loads(out) if rc == 0 else None,
                             **({} if rc == 0 else {"why": out[-300:]})})
        return "ok" if rc == 0 else f"exit {rc}"

    def supervisor():
        rc, out = runner([sys.executable, "cloud/supervisor.py", "pull", "--dest", os.path.join(KEEP, "supervisor")])
        return "ok" if rc == 0 else f"exit {rc}: {out[-200:]}"
    part("budget_status", budget)
    part("open_prs", prs)
    part("supervisor", supervisor)
    for path in configs:
        try:
            RC, cfg = _load_cfg(path)
        except BaseException as e:  # noqa: BLE001
            status["parts"][f"config {path}"] = {"ok": False, "note": f"{type(e).__name__}: {e}"[:300], "at": _now()}
            continue
        part(f"launch {cfg.name}", lambda: _put(f"launches/{cfg.name}.json", launch_record(cfg)) or "ok")
        store = RC._results_store()
        for label in cfg.labels:
            part(f"mirror {cfg.name}/{label}",
                 lambda label=label: mirror_label(RC, cfg, store, label, os.path.join(mirror_root(), cfg.name)))
    _put("follow_status.json", status)
    return status


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, label):
        print(("  ok   " if c else "  FAIL ") + label)
        if not c:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        os.environ["MA_DIAG_HOME"], os.environ["MA_DIAG_MIRROR"] = os.path.join(td, "home"), os.path.join(td, "mirror")
        calls = []

        def fake(cmd, timeout=600):
            calls.append(cmd)
            if cmd[1:3] == ["cloud/budget.py", "status"]:
                return 0, json.dumps({"scopes": [{"scope": "s", "remaining_usd": 1.0}]})
            if cmd[0] == "gh":
                return 1, "gh: not logged in"
            return 0, ""
        st = once([os.path.join(td, "missing_config.py")], {}, runner=fake)
        cache = os.path.join(td, "home", "cache")
        b = json.load(open(os.path.join(cache, "budget_status.json")))
        g = json.load(open(os.path.join(cache, "gh_prs.json")))
        ok(b["status"]["scopes"][0]["scope"] == "s" and b["fetched_at"], "FO1 the budget status is cached with its time")
        ok(g["prs"] is None and "not logged in" in g["why"] and st["parts"]["open_prs"]["note"] == "exit 1",
           "FO2 a failed gh call is cached as unavailable, with why")
        ok(st["parts"]["config " + os.path.join(td, "missing_config.py")]["ok"] is False and st["parts"]["budget_status"]["ok"],
           "FO3 a config that does not load is recorded and the other parts still run")
        ok(all(c[0] in ("gh", sys.executable) and not any(x in ("launch", "shutdown", "stop", "relaunch", "merge")
                                                          for x in c) for c in calls),
           f"FO4 only read commands are run ({[c[1:3] for c in calls]})")
        ok(json.load(open(os.path.join(cache, "follow_status.json")))["parts"], "FO5 the pass's status is recorded")

        # mirror_label with a fake run_cluster: skips a generation already mirrored, clears our own old copies
        dest = os.path.join(td, "mirror", "cfgx")
        state = os.path.join(td, "assignments.json")
        json.dump({"R1": {"launch_id": "L1"}}, open(state, "w"))
        cfg = SimpleNamespace(name="cfgx", storage_prefix="p", state_file=state, storage_account="acct", out_dir=td)
        fetched = []

        class FakeRC:
            @staticmethod
            def checkpoint_record(view, store, label, launch_id):
                return {"generation": 3}

            @staticmethod
            def fetch_checkpoint(view, store, label):
                fetched.append((view.out_dir, view.storage_account))
                d = os.path.join(view.out_dir, label, "checkpoint")
                if os.path.exists(d):
                    os.rename(d, d + ".before_1")
                os.makedirs(d)
                json.dump({"generation": 3}, open(os.path.join(d, "checkpoint.json"), "w"))
                return {"time_utc": "t"}
        os.environ["MA_STORAGE_ACCOUNT"] = "envacct"
        r1 = mirror_label(FakeRC, cfg, None, "R1", dest)
        r2 = mirror_label(FakeRC, cfg, None, "R1", dest)
        ok("mirrored" in r1 and "already mirrored" in r2 and fetched == [(dest, "envacct")],
           f"FO6 the mirror downloads into its own folder with the environment's account, once per generation ({fetched})")
        os.environ.pop("MA_STORAGE_ACCOUNT", None)
    for k in ("MA_DIAG_HOME", "MA_DIAG_MIRROR"):
        os.environ.pop(k, None)
    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", action="append", default=[], help="a cloud/configs/*.py launch config to follow")
    ap.add_argument("--every", type=float, default=10.0, help="minutes between passes")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    status: dict = {}
    while True:
        st = once(a.config, status)
        bad = [k for k, v in st["parts"].items() if not v["ok"]]
        print(f"[follow] {st['at']}: {len(st['parts']) - len(bad)} part(s) ok" + (f"; failed: {', '.join(bad)}" if bad else ""))
        if a.once:
            return 0 if not bad else 1
        time.sleep(max(60.0, a.every * 60))


if __name__ == "__main__":
    sys.exit(main())
