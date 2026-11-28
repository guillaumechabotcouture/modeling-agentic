"""Shared IDM Coiled cluster, attach, budget, archive, and scheduler-ack operations.

Used by both `cloud/run_cluster.py` and `cloud/vm_job.py`.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import worker_side as ws  # noqa: E402
_SCRIPTS_LIB = os.path.join(os.path.dirname(HERE), "scripts", "lib")
if _SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _SCRIPTS_LIB)
import eval_paths  # noqa: E402
import pipeline_id  # noqa: E402

WORKSPACE = ws.WORKSPACE
ENV_FILE = os.path.expanduser("~/modeling-agentic/.env")
_ATTACH = {"ready", "running", "pending", "starting", "scaling"}
_ENDED = ("stopping", "stopped", "error")

ACK_ATTEMPTS, ACK_DELAY_S = 36, 5.0
CONNECTION_ERRORS = (OSError, TimeoutError, ws.SchedulerInfoUnavailable)
ACK_STATES = ("waiting", "no-worker", "queued", "processing", "memory", "erred")
LIVE_STATES = ("waiting", "no-worker", "queued", "processing")

INVOCATION_TAG = "ma-invocation"
OWN_SKEW_S = 120.0
JOURNAL = "constructions.jsonl"
_CREATED, _ATTACHED = "Creating Cluster (name: {}, ", "Attaching to existing cluster (name: {}, "

TARBALL_SKIP_ANY = {".git", ".venv", "__pycache__"}
TARBALL_SKIP_TOP: set = set()


def _items(cs) -> list:
    return cs if isinstance(cs, list) else list((cs or {}).values())


def list_clusters(lister=None) -> list:
    """List cluster dicts in WORKSPACE (normalizing list/dict returns from Coiled)."""
    if lister is None:
        import coiled
        lister = lambda: coiled.list_clusters(workspace=WORKSPACE)  # noqa: E731
    return _items(lister())


def cluster_states(name: str, lister=None) -> list[str]:
    """States of every cluster with this name (Coiled keeps stopped ones listed)."""
    return [ws.state_of(c) for c in list_clusters(lister) if c.get("name") == name]


def is_running(name: str, lister=None) -> bool:
    return any(s in _ATTACH for s in cluster_states(name, lister))


def live_id(name: str, lister=None):
    """Id of the live cluster with this name, or None."""
    ids = [c.get("id") for c in list_clusters(lister) if c.get("name") == name and ws.state_of(c) in _ATTACH]
    return max(ids) if ids else None


def attach(name: str):
    """Attach to a live cluster by verified live id; refuse rather than create one."""
    cid = live_id(name)
    if cid is None:
        raise SystemExit(f"cluster {name!r} is not running; refusing to create a new one")
    return ws.attach_existing(name, WORKSPACE, cid)


def connect_existing(name: str, lister=None, security_fn=None, client_cls=None):
    """A Client on the live cluster `name`, or None. Never creates a cluster."""
    cid = live_id(name, lister)
    if cid is None:
        return None
    if security_fn is None:
        def security_fn(cluster_id):
            from coiled.v2.core import CloudV2
            with CloudV2() as cloud:
                return cloud.security(cluster_id, workspace=WORKSPACE)
    try:
        sec, info = security_fn(cid)
    except RuntimeError:
        return None
    if client_cls is None:
        from distributed import Client as client_cls
    return client_cls(info["address_to_use"], security=sec, timeout="60s")


def existing_client(name: str):
    """A Client on live cluster `name` through connect_existing; SystemExit when not running."""
    client = connect_existing(name)
    if client is None:
        raise SystemExit(f"cluster {name!r} is not running; refusing to create a new one")
    return client


def keeper_wait(states: list[str]) -> str:
    """What to do about an existing cluster before creating a new one: 'start', 'stop', or 'wait'."""
    live = [s for s in states if s not in ("stopped", "error", "")]
    if not live:
        return "start"
    return "wait" if all(s == "stopping" for s in live) else "stop"


def stop_cluster_by_id(name: str, cid, why: str = "", lister=None, check_live: bool = True) -> None:
    """Stop cluster(s) by id (never an attach by name)."""
    import coiled
    if not check_live and cid is not None:
        coiled.delete_cluster(cluster_id=cid, workspace=WORKSPACE)
        print(f"{name} ({cid}): stop requested by id")
        return
    live = None
    for i in range(3):
        try:
            live = {c.get("id"): ws.state_of(c) for c in list_clusters(lister) if c.get("name") == name}
            break
        except Exception as e:  # noqa: BLE001
            print(f"{name}: cluster listing failed ({type(e).__name__}: {str(e)[:120]})")
            if i < 2:
                time.sleep(5)
    if cid is None and live is None:
        print(f"{name}: could not find the new cluster to stop it; it stops after its idle timeout")
        return
    for i in ([cid] if cid is not None else sorted(live)):
        state = (live or {}).get(i, "") if live is not None else "unknown"
        if live is not None and (i not in live or state in _ENDED):
            print(f"{name} ({i}): already {state or 'gone'} ({why})")
            continue
        try:
            coiled.delete_cluster(cluster_id=i, workspace=WORKSPACE)
            print(f"{name} ({i}): stopped ({why})")
        except Exception as e:  # noqa: BLE001
            print(f"{name} ({i}): stop request failed ({type(e).__name__}: {str(e)[:120]}); the job stops it after "
                  f"its grace period, or it stops after its idle timeout")


def _ts(v):
    """An ISO time from the Coiled API as epoch seconds (UTC when it has no zone), or None."""
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def _out_dir(out_dir_or_cfg) -> str:
    return out_dir_or_cfg if isinstance(out_dir_or_cfg, str) else out_dir_or_cfg.out_dir


def read_journal(out_dir_or_cfg) -> list:
    try:
        with open(os.path.join(_out_dir(out_dir_or_cfg), JOURNAL)) as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except (OSError, ValueError):
        return []


def _journal(out_dir_or_cfg, entry: dict) -> None:
    d = _out_dir(out_dir_or_cfg)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, JOURNAL), "a") as f:
        f.write(json.dumps(entry) + "\n")


@contextlib.contextmanager
def coiled_events():
    """Capture messages coiled.Cluster logs to the 'coiled' logger while running."""
    import logging

    class H(logging.Handler):
        def emit(self, record):
            try:
                got.append(record.getMessage())
            except Exception:  # noqa: BLE001
                pass

    got, lg, h = [], logging.getLogger("coiled"), H(level=logging.INFO)
    old = lg.level
    if old == logging.NOTSET or old > logging.INFO:
        lg.setLevel(logging.INFO)
    lg.addHandler(h)
    try:
        yield got
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)


def construct_cluster(out_dir_or_cfg, name: str, inv: str, ctor, lister=None):
    """Build a cluster that is this invocation's, or refuse before its id is recorded anywhere."""
    live = sorted(c.get("id") for c in list_clusters(lister)
                  if c.get("name") == name and ws.state_of(c) not in ("stopped", "error"))
    if live:
        raise SystemExit(f"{name}: a cluster of this name is live or stopping ({live}) just before the constructor; "
                         "not creating it; refusing, with nothing recorded")
    seq, t0 = secrets.token_hex(4), time.time()
    _journal(out_dir_or_cfg, {"inv": inv, "name": name, "t": t0, "status": "constructing", "seq": seq})
    with coiled_events() as ev:
        cl = ctor()
    cid = getattr(cl, "cluster_id", None)
    attached = any(m.startswith(_ATTACHED.format(name)) or m.startswith(f"Using existing cluster: '{name} ") for m in ev)
    created = any(m.startswith(_CREATED.format(name)) for m in ev)
    rec = next((c for c in list_clusters(lister) if c.get("id") == cid), None) if cid is not None else None
    ts = _ts((rec or {}).get("created"))
    why = ("Coiled attached to an existing cluster" if attached else "Coiled logged no creation" if not created
           else "the listing shows no created time" if ts is None
           else f"created {t0 - ts:.0f} s before this construction began" if ts < t0 - OWN_SKEW_S else None)
    _journal(out_dir_or_cfg, {"inv": inv, "name": name, "id": cid, "seq": seq, "why": why,
                              "status": "created" if why is None else "attached" if attached else "unknown"})
    if why:
        raise SystemExit(f"{name} ({cid}): not created by this invocation ({why}); refusing, with nothing recorded "
                         "and nothing to clean up")
    return cl


def budget_guard(scope: str, cap_usd, name: str, *, reserve: bool = True, warn_over: bool = True,
                 inv=None, clusters_file: str | None = None) -> None:
    """Refuse (SystemExit) unless owner-signed `scope` covers spend plus `cap_usd`."""
    import budget
    if not scope:
        raise SystemExit(f"{name} matches no budget scope in cloud/budget_scopes.yaml: refusing")
    bind = {"invocation": inv, "clusters_file": clusters_file} if reserve and inv and clusters_file else {}
    ok, why = budget.launch_budget_ok(scope, cap_usd, name=name, reserve=reserve, warn_over=warn_over, **bind)
    if not ok:
        raise SystemExit(f"{name}: the budget refuses this launch: {why}")
    print(f"budget: {why}", flush=True)


def read_keys(path=ENV_FILE) -> dict:
    """ANTHROPIC_API_KEY / OPENAI_API_KEY only; values are never printed."""
    keys = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                v = v.strip()
                if v[:1] in ("'", '"'):
                    end = v.find(v[0], 1)
                    v = v[1:end] if end > 0 else v[1:]
                    if k and line.strip().partition("=")[2].strip()[:1] == '"':
                        v = re.sub(r"\\([nrt\\\"])",
                                   lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)), v)
                else:
                    v = v.split(" #", 1)[0].strip()
                keys[k] = v
    return keys


def _archive_member(code: bytes, name: str) -> bytes | None:
    try:
        with tarfile.open(fileobj=io.BytesIO(code), mode="r:gz") as t:
            m = next((m for m in t.getmembers() if m.isfile() and m.name.removeprefix("./") == name), None)
            return t.extractfile(m).read() if m else None
    except (tarfile.TarError, OSError):
        return None


def archive_commit(code: bytes) -> str | None:
    """The 7-character commit git archive stamped into scripts/lib/code_commit.txt, or None."""
    raw = (_archive_member(code, "scripts/lib/" + pipeline_id.COMMIT_FILE) or b"").decode("ascii", "replace").strip()
    return raw[:7] if len(raw) >= 7 and all(c in "0123456789abcdef" for c in raw) else None


def tarball_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """A --dir archive leaves out VCS data, the virtualenv and bytecode, at any depth."""
    parts = [p for p in ti.name.split("/") if p not in ("", ".")]
    if any(p in TARBALL_SKIP_ANY for p in parts) or (parts and parts[0] in TARBALL_SKIP_TOP):
        return None
    return ti


def tarball_from_tree(tree: str | None = None, src_dir: str | None = None, *, required_files: tuple = (),
                      check_clean: bool = True, check_hidden: bool = False, require_main: bool = False,
                      tarball_filter_fn=None) -> bytes | None:
    """Build a `.tar.gz` archive from a git tree (HEAD) or plain directory."""
    if tree:
        git = lambda *a: subprocess.check_output(["git", "-C", tree, *a], text=True).strip()  # noqa: E731
        if require_main:
            subprocess.run(["git", "-C", tree, "fetch", "-q", "origin"], check=True)
        head = git("rev-parse", "HEAD")
        if check_clean and git("status", "--porcelain"):
            raise SystemExit(f"{tree} has uncommitted changes: refusing")
        if require_main:
            main_ = git("rev-parse", "origin/main")
            if head != main_:
                raise SystemExit(f"{tree} is at {head[:7]}, origin/main is {main_[:7]}: refusing")
        if required_files:
            names = set(git("ls-files").splitlines())
            missing = [n for n in required_files if n not in names]
            if missing:
                raise SystemExit(f"code to launch lacks {missing}: refusing")
        if check_hidden:
            found = eval_paths.tree_hidden_material(tree, None)
            if found:
                raise SystemExit(f"{tree} holds hidden eval-suite material that package sync would upload "
                                 f"({len(found)}: {', '.join(found[:5])}{', ...' if len(found) > 5 else ''}); "
                                 "move it out of the launch tree: refusing")
        blob = subprocess.check_output(["git", "-C", tree, "archive", "--format=tar.gz", "HEAD"])
        if check_hidden:
            hidden = eval_paths.hidden_members(blob)
            if hidden:
                raise SystemExit(f"{tree}: the code tarball would ship eval-suite files agents must not see "
                                 f"({len(hidden)}, e.g. {hidden[:3]}); check .gitattributes: refusing")
        return blob
    if src_dir:
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            t.add(src_dir, arcname=".", filter=tarball_filter_fn or tarball_filter)
        return b.getvalue()
    return None


def scheduler_state(client, key):
    """The scheduler's state of task `key` (worker_side.task_state), or None."""
    try:
        import cloudpickle
        cloudpickle.register_pickle_by_value(ws)
    except ImportError:
        pass
    return client.run_on_scheduler(ws.task_state, key)


def acknowledged(client, key, fut) -> str | None:
    """What shows the scheduler has the run, or None."""
    st = getattr(fut, "status", None)
    if st in ("finished", "error"):
        return f"future {'finished' if st == 'finished' else 'erred'}"
    if any(key in (ks or ()) for ks in (client.processing() or {}).values()):
        return "processing"
    try:
        state = scheduler_state(client, key)
    except CONNECTION_ERRORS:
        raise
    except Exception:  # noqa: BLE001
        return None
    return f"scheduler state {state}" if state in ACK_STATES else None


def wait_listed(client, key, fut=None, attempts=ACK_ATTEMPTS, delay_s=ACK_DELAY_S, sleep=None, why=None) -> bool:
    """True once the scheduler has the run; False if not within `attempts` polls."""
    sleep = sleep or time.sleep
    for i in range(max(1, int(attempts))):
        got = acknowledged(client, key, fut)
        if got:
            if why is not None:
                why.append(got)
            return True
        if i < attempts - 1:
            sleep(delay_s)
    return False


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    fake = [{"name": "c1", "id": 10, "current_state": {"state": "running"}},
            {"name": "c1", "id": 5, "current_state": {"state": "stopped"}},
            {"name": "c2", "id": 20, "current_state": {"state": "stopping"}}]
    ok(cluster_states("c1", lister=lambda: fake) == ["running", "stopped"], "CO1 cluster_states")
    ok(is_running("c1", lister=lambda: fake) and not is_running("c2", lister=lambda: fake), "CO2 is_running")
    ok(live_id("c1", lister=lambda: fake) == 10 and live_id("c2", lister=lambda: fake) is None, "CO3 live_id")
    ok(keeper_wait([]) == "start" and keeper_wait(["stopping"]) == "wait" and keeper_wait(["running"]) == "stop",
       "CO4 keeper_wait")

    with tempfile.TemporaryDirectory() as td:
        envp = os.path.join(td, ".env")
        open(envp, "w").write('ANTHROPIC_API_KEY="sk-ant-test" # inline\nOTHER=1\n')
        ok(read_keys(envp) == {"ANTHROPIC_API_KEY": "sk-ant-test"}, "CO5 read_keys")
        src = os.path.join(td, "src")
        os.makedirs(os.path.join(src, ".git"))
        open(os.path.join(src, "a.py"), "w").write("X = 1\n")
        open(os.path.join(src, ".git", "HEAD"), "w").write("ref\n")
        blob = tarball_from_tree(None, src)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
            names = [m.name for m in t.getmembers()]
        ok(any(n.endswith("a.py") for n in names) and not any(".git" in n for n in names), f"CO6 tarball_from_tree: {names}")

        repo = os.path.join(os.path.realpath(td), "repo")
        os.makedirs(repo)

        def _git(*args):
            subprocess.run(["git", "-C", repo, *args], check=True, stdout=subprocess.DEVNULL)

        _git("init", "-q", "-b", "main")
        _git("config", "user.email", "t@example.invalid")
        _git("config", "user.name", "t")
        _git("config", "commit.gpgsign", "false")
        with open(os.path.join(repo, "a.py"), "w", encoding="utf-8") as f:
            f.write("X = 1\n")
        _git("add", "a.py")
        _git("commit", "-q", "-m", "init")
        with open(os.path.join(repo, "a.py"), "w", encoding="utf-8") as f:
            f.write("X = 2\n")
        with open(os.path.join(repo, "untracked.txt"), "w", encoding="utf-8") as f:
            f.write("dirty\n")
        for label, kw in (("default", {}), ("explicit", {"check_clean": True})):
            try:
                tarball_from_tree(repo, **kw)
                ok(False, f"CO7 tarball_from_tree ({label}) refuses dirty git tree")
            except SystemExit as e:
                ok("uncommitted changes" in str(e), f"CO7 tarball_from_tree ({label}) refuses dirty git tree: {e}")
        git_blob = tarball_from_tree(repo, check_clean=False)
        with tarfile.open(fileobj=io.BytesIO(git_blob), mode="r:gz") as t:
            git_names = [m.name.removeprefix("./") for m in t.getmembers()]
        ok(_archive_member(git_blob, "a.py") == b"X = 1\n" and "untracked.txt" not in git_names,
           f"CO8 tarball_from_tree(check_clean=False) archives committed HEAD only: {git_names}")

    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Shared Coiled ops")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        import no_cloud
        with no_cloud.refusing():
            return _self_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
