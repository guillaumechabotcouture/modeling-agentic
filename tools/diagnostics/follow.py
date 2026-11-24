#!/usr/bin/env python3
"""Refresh the diagnostics server's local copies (docs/design/diagnostic-data.md §5): the one part that uses the network.

    python3 tools/diagnostics/follow.py --config cloud/configs/<x>.py [--config ...] [--every 10] [--once]
    python3 tools/diagnostics/follow.py --self-test

Each pass, read-only towards everything remote:
  - mirror: for each label a launch recorded (run_cluster.recorded_labels), the latest checkpoint of that launch,
    sha256-verified by cloud/run_cluster.py's own `checkpoint_record` and `fetch_checkpoint`. It is fetched into a
    staging folder the server never reads (<mirror>/.staging/), then published at <mirror>/<config>/<label>/ in one
    rename, so a reader never sees two copies or a half-extracted one. A label whose (launch, generation) is the one
    already published is skipped; the generation recorded is the one the verified fetch returned. The mirror must not
    overlap the launch's own output folder. The storage account is the environment's ($MA_STORAGE_ACCOUNT or
    $COILED_STORAGE_ACCOUNT), else the config's.
  - caches under <home>/cache/, each with its fetch time: `cloud/budget.py status --json` (budget_status.json; a
    failed refresh keeps the last good snapshot and records the error), `gh pr list --json` (gh_prs.json, with a
    truncation flag), each config's launch records read with no link followed (launches/<name>.json: clusters.json,
    assignments.json and the cap projection of the launch's own invocation), and the supervisor's status.json read
    from storage (~/modeling-agentic-keep/supervisor/status.json).
A failing part is recorded in <home>/cache/follow_status.json and never stops the others; --once exits 1 when any
part failed. Nothing is launched, stopped, attached or written remotely.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(REPO, "scripts"), os.path.join(REPO, "cloud")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
KEEP = os.path.expanduser("~/modeling-agentic-keep")
PR_FIELDS = "number,title,headRefName,headRefOid,isDraft,reviewDecision,statusCheckRollup,updatedAt,url"
PR_LIMIT = 1000


class PartFailed(Exception):
    """A part of the pass that did not do its job (its note says why)."""


def home() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_HOME") or os.path.join(KEEP, "diagnostics"))


def keep() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_KEEP") or KEEP)


def mirror_root() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_MIRROR") or os.path.join(KEEP, "mirror"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(base: str, rel: str, obj) -> None:
    """base/rel, atomically, no link followed (lib.safe_io)."""
    from lib.safe_io import write_nofollow
    os.makedirs(base, exist_ok=True)
    write_nofollow(base, rel, json.dumps(obj, indent=1, default=str).encode(), atomic=True)


def _put(rel: str, obj) -> None:
    _write(home(), f"cache/{rel}", obj)


def _get(rel: str):
    try:
        from lib.safe_io import read_nofollow
        return json.loads(read_nofollow(home(), f"cache/{rel}")[0])
    except Exception:  # noqa: BLE001
        return None


def _run(cmd: list, timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.returncode and p.stderr else "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, f"{type(e).__name__}: {e}"


def _inside(a: str, b: str) -> bool:
    a, b = os.path.realpath(a), os.path.realpath(b)
    return a == b or a.startswith(b + os.sep)


def _account(cfg) -> str:
    return os.environ.get("MA_STORAGE_ACCOUNT") or os.environ.get("COILED_STORAGE_ACCOUNT") or cfg.storage_account


def mirror_label(RC, cfg, store, label: str, root: str) -> str:
    """One label's latest checkpoint, staged then published at root/<config>/<label>/; returns what was done."""
    if _inside(root, cfg.out_dir) or _inside(cfg.out_dir, root):
        raise PartFailed(f"the mirror {root} overlaps the launch's output folder {cfg.out_dir}: refused")
    final = os.path.join(root, cfg.name, label)
    launch_id = json.load(open(cfg.state_file))[label]["launch_id"]
    stage_root = os.path.join(root, ".staging", cfg.name)
    view = SimpleNamespace(storage_prefix=cfg.storage_prefix, state_file=cfg.state_file, name=cfg.name,
                           storage_account=_account(cfg), out_dir=stage_root)
    ptr = RC.checkpoint_record(view, store, label, launch_id) or {}
    try:
        have = json.load(open(os.path.join(final, "mirror.json")))
    except (OSError, ValueError):
        have = {}
    gen = ptr.get("generation")
    if gen is not None and (have.get("launch_id"), have.get("generation")) == (launch_id, gen) \
            and os.path.isdir(os.path.join(final, "checkpoint")):
        return f"launch {launch_id} generation {gen} already mirrored"
    shutil.rmtree(os.path.join(stage_root, label), ignore_errors=True)
    rec = RC.fetch_checkpoint(view, store, label) or {}
    want = RC._download_json(store, RC.ws.checkpoint_blobs(cfg.storage_prefix, launch_id, label, gen)[1],
                             view.storage_account) if gen is not None else None
    got = gen if (want or {}).get("sha256") and want.get("sha256") == rec.get("sha256") else None   # what was fetched
    staged = os.path.join(stage_root, label)
    for x in os.listdir(staged):                     # only the verified checkpoint is published
        if x != "checkpoint":
            p = os.path.join(staged, x)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
    _write(staged, "mirror.json", {"launch_id": launch_id, "generation": got, "time_utc": rec.get("time_utc"),
                                   "at": _now()})
    old = os.path.join(root, ".staging", f"{cfg.name}.{label}.old")
    shutil.rmtree(old, ignore_errors=True)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    if os.path.lexists(final):
        os.rename(final, old)
    os.rename(staged, final)                         # one rename publishes the new copy
    shutil.rmtree(old, ignore_errors=True)
    return (f"launch {launch_id} generation {got} of {rec.get('time_utc')} mirrored" if got is not None else
            f"launch {launch_id}: the pointer's generation {gen} did not verify; an earlier one is mirrored")


def _read_nofollow_json(base: str, rel: str):
    from lib.safe_io import read_nofollow
    return json.loads(read_nofollow(base, rel, max_bytes=8 << 20)[0])


def launch_record(cfg) -> dict:
    """The config's own local launch records, read with no link followed (no network)."""
    out = {"config": cfg.name, "out_dir": cfg.out_dir, "fetched_at": _now()}
    for key, path in (("clusters", cfg.clusters_file), ("assignments", cfg.state_file)):
        try:
            out[key] = _read_nofollow_json(os.path.dirname(path), os.path.basename(path))
        except Exception as e:  # noqa: BLE001
            out[key] = {"available": False, "why": type(e).__name__}
    inv = (out.get("clusters") or {}).get("invocation") if isinstance(out.get("clusters"), dict) else None
    out["cap_projection"] = None
    if isinstance(inv, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", inv):   # this launch's own projection only
        try:
            out["cap_projection"] = _read_nofollow_json(cfg.out_dir, f"cap_projection_{inv}.json")
        except Exception:  # noqa: BLE001 -- none yet for this invocation
            pass
    return out


def once(configs: list, status: dict, runner=_run, supervisor_status=None) -> dict:
    """One pass; every part's outcome in `status` (written to cache/follow_status.json)."""
    status.update(at=_now(), parts={})

    def part(name, fn):
        try:
            status["parts"][name] = {"ok": True, "note": str(fn())[:300], "at": _now()}
        except (Exception, SystemExit) as e:  # noqa: BLE001 -- run_cluster's refusals are SystemExit; Ctrl-C stops
            status["parts"][name] = {"ok": False, "note": f"{type(e).__name__}: {e}"[:300], "at": _now()}

    def budget():
        rc, out = runner([sys.executable, "cloud/budget.py", "status", "--json"])
        try:
            doc = json.loads(out) if rc == 0 else None
        except ValueError:
            doc = None
        if doc is None:                  # keep the last good snapshot, with its own time, and say what failed
            prev = _get("budget_status.json") or {"fetched_at": None, "status": None}
            prev.update(last_error=f"exit {rc}: {out[-200:]}", last_error_at=_now())
            _put("budget_status.json", prev)
            raise PartFailed(f"budget.py status exit {rc}")
        _put("budget_status.json", {"fetched_at": _now(), "status": doc})
        return "ok"

    def prs():
        rc, out = runner(["gh", "pr", "list", "--state", "open", "--limit", str(PR_LIMIT), "--json", PR_FIELDS])
        try:
            rows = json.loads(out) if rc == 0 else None
        except ValueError:
            rows = None
        if not isinstance(rows, list):
            prev = _get("gh_prs.json") or {"fetched_at": None, "prs": None}
            prev.update(last_error=f"exit {rc}: {out[-200:]}", last_error_at=_now())
            _put("gh_prs.json", prev)
            raise PartFailed(f"gh pr list exit {rc}")
        _put("gh_prs.json", {"fetched_at": _now(), "prs": rows, "truncated": len(rows) >= PR_LIMIT})
        return f"{len(rows)} open PR(s)"

    def supervisor():
        st = supervisor_status() if supervisor_status else _supervisor_status()
        if not isinstance(st, dict):
            raise PartFailed("no status.json in storage for the supervisor")
        _write(keep(), "supervisor/status.json", st)
        return f"heartbeat {st.get('heartbeat_utc')}"
    part("budget_status", budget)
    part("open_prs", prs)
    part("supervisor", supervisor)
    for path in configs:
        try:
            import run_cluster as RC
            cfg = RC.load_config(path)
            labels = RC.recorded_labels(cfg, json.load(open(cfg.state_file)))
        except (Exception, SystemExit) as e:  # noqa: BLE001
            status["parts"][f"config {path}"] = {"ok": False, "note": f"{type(e).__name__}: {e}"[:300], "at": _now()}
            continue
        part(f"launch {cfg.name}", lambda: _put(f"launches/{cfg.name}.json", launch_record(cfg)) or "ok")
        store = RC._results_store()
        for label in labels:
            part(f"mirror {cfg.name}/{label}", lambda label=label: mirror_label(RC, cfg, store, label, mirror_root()))
    _put("follow_status.json", status)
    return status


def _supervisor_status(name: str | None = None):
    import supervisor as SV
    n = name or os.environ.get("MA_SUPERVISOR_NAME") or SV.DEFAULT_NAME
    return SV.Blob(None).get_json(SV.paths(n)["status"])


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, label):
        print(("  ok   " if c else "  FAIL ") + label)
        if not c:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        os.environ.update(MA_DIAG_HOME=os.path.join(td, "home"), MA_DIAG_MIRROR=os.path.join(td, "mirror"),
                          MA_DIAG_KEEP=os.path.join(td, "keep"))
        calls, mode = [], {"budget": 0}

        def fake(cmd, timeout=600):
            calls.append(cmd)
            if cmd[1:3] == ["cloud/budget.py", "status"]:
                return (0, json.dumps({"scopes": [{"scope": "s"}]})) if mode["budget"] == 0 else (1, "offline")
            if cmd[0] == "gh":
                return 1, "gh: not logged in"
            return 0, ""
        st = once([os.path.join(td, "missing_config.py")], {}, runner=fake,
                  supervisor_status=lambda: {"heartbeat_utc": "2026-11-24T10:00:00Z"})
        cache = os.path.join(td, "home", "cache")
        b = json.load(open(os.path.join(cache, "budget_status.json")))
        ok(b["status"]["scopes"][0]["scope"] == "s" and b["fetched_at"], "FO1 the budget status is cached with its time")
        ok(st["parts"]["open_prs"]["ok"] is False and st["parts"]["budget_status"]["ok"],
           "FO2 a failed gh call is a failed part")
        ok(st["parts"]["config " + os.path.join(td, "missing_config.py")]["ok"] is False
           and json.load(open(os.path.join(td, "keep", "supervisor", "status.json")))["heartbeat_utc"],
           "FO3 a config that does not load is recorded; the others run; the supervisor status is saved")
        ok(all(c[0] in ("gh", sys.executable) and not any(x in ("launch", "shutdown", "stop", "relaunch", "merge")
                                                          for x in c) for c in calls),
           f"FO4 only read commands are run ({[c[1:3] for c in calls]})")
        mode["budget"] = 1
        st2 = once([], {}, runner=fake, supervisor_status=lambda: None)
        b2 = json.load(open(os.path.join(cache, "budget_status.json")))
        ok(st2["parts"]["budget_status"]["ok"] is False and b2["fetched_at"] == b["fetched_at"]
           and b2["status"]["scopes"] and "offline" in b2["last_error"] and st2["parts"]["supervisor"]["ok"] is False,
           "FO5 a failed refresh keeps the last good snapshot and its time, and records the error")

        # the mirror, with a fake run_cluster: staged, published, skipped per (launch, generation), overlap refused
        state = os.path.join(td, "launch", "assignments.json")
        os.makedirs(os.path.dirname(state))
        json.dump({"R1": {"launch_id": "L1"}}, open(state, "w"))
        cfg = SimpleNamespace(name="cfgx", storage_prefix="p", state_file=state, storage_account="acct",
                              out_dir=os.path.join(td, "launch"), clusters_file=os.path.join(td, "launch", "clusters.json"))
        fetched = []

        class FakeRC:
            ws = SimpleNamespace(checkpoint_blobs=lambda *a: ("tar", "rec"))

            @staticmethod
            def checkpoint_record(view, store, label, launch_id):
                return {"generation": 3}

            @staticmethod
            def _download_json(store, blob, acct):
                return {"sha256": "s3"}

            @staticmethod
            def fetch_checkpoint(view, store, label):
                fetched.append((view.out_dir, view.storage_account))
                d = os.path.join(view.out_dir, label, "checkpoint", "runs", "r")
                os.makedirs(d)
                json.dump({"state": "SPEC"}, open(os.path.join(d, "run_state.json"), "w"))
                open(os.path.join(view.out_dir, label, "checkpoint.tar.gz"), "w").close()
                return {"time_utc": "t", "sha256": "s3"}
        os.environ["MA_STORAGE_ACCOUNT"] = "envacct"
        root = os.path.join(td, "mirror")
        r1 = mirror_label(FakeRC, cfg, None, "R1", root)
        r2 = mirror_label(FakeRC, cfg, None, "R1", root)
        fin = os.path.join(root, "cfgx", "R1")
        ok("mirrored" in r1 and "already mirrored" in r2 and len(fetched) == 1 and fetched[0][1] == "envacct"
           and ".staging" in fetched[0][0]
           and set(os.listdir(fin)) == {"checkpoint", "mirror.json"}, f"FO6 staged, published once per generation ({r1}; {r2})")
        json.dump({"R1": {"launch_id": "L2"}}, open(state, "w"))
        r3 = mirror_label(FakeRC, cfg, None, "R1", root)
        ok("L2" in r3 and len(fetched) == 2, "FO7 a new launch with the same generation number is fetched again")
        try:
            mirror_label(FakeRC, cfg, None, "R1", os.path.join(td))
            ok(False, "FO8 a mirror overlapping the launch's output folder is refused")
        except PartFailed:
            ok(True, "FO8 a mirror overlapping the launch's output folder is refused")
        open(os.path.join(td, "outside.json"), "w").write('{"x": 1}')
        os.symlink(os.path.join(td, "outside.json"), cfg.clusters_file)
        lr = launch_record(cfg)
        ok(lr["clusters"].get("available") is False and lr["cap_projection"] is None,
           "FO9 a linked launch record is not followed; no projection of another invocation is taken")
        os.environ.pop("MA_STORAGE_ACCOUNT", None)
    for k in ("MA_DIAG_HOME", "MA_DIAG_MIRROR", "MA_DIAG_KEEP"):
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
