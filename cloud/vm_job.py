"""Durable detached jobs on a small Coiled VM (the dev VM, or any one-VM cluster).

    python cloud/vm_job.py up <name> [--cpu 4] [--memory "16 GiB"] [--idle-timeout "20 minutes"]
    python cloud/vm_job.py start <name> <job_id> [--tree PATH | --dir PATH] [--max-hours 24]
                                  [--setup-sdk] [--anthropic-key] -- <command ...>
    python cloud/vm_job.py status <name> <job_id>          # state + log tail; saves outputs locally
    python cloud/vm_job.py wait <name> <job_id> [--every 300]   # poll until the job ends, saving each time
    python cloud/vm_job.py down <name>
    python cloud/vm_job.py --self-test

Why. Coiled stops a cluster after `idle_timeout` of scheduler idleness, and only dask
tasks count: a job started with client.run, or a process it spawned, is invisible to it.
Twice on 2026-10-28 a detached job on the dev VM (45-minute idle timeout) was stopped
mid-run and its outputs lost. Here the job runs inside a dask task submitted with
fire_and_forget (worker_side.job_task), so the scheduler is busy for exactly the job's
lifetime whatever the idle timeout; the idle timeout only has to cover fetching after the
job ends. The job writes its outputs under $JOB_OUT as it goes, and every `status` / `wait`
poll copies new or changed files (and the log) to <out>/<name>/<job_id>/, so a VM that
stops for any other reason loses at most one poll interval of outputs.

The job's command runs in the shipped tree (`--tree`: git archive of HEAD; `--dir`: a
plain directory), with secrets (`--anthropic-key`) in its environment only. `--setup-sdk`
installs the pinned claude-agent-sdk from the tree's requirements.txt first (the useful part
of keep/coiled_run/dev_vm.py, which this replaces).

Rules: the IDM Coiled default workspace only; attach-only for everything but `up`
(coiled.Cluster(name=...) on a cluster that is gone creates a new, empty one).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_cluster as rc  # noqa: E402
import worker_side as ws  # noqa: E402

OUT = os.path.expanduser("~/modeling-agentic-keep/vm_jobs")
CHUNK = 8 * 2**20
DONE = ("done", "failed", "timed_out", "refused")


def files_to_fetch(remote: dict, index: dict) -> list[str]:
    """Remote files that are new or changed (size or mtime) since the last fetch."""
    return sorted(rel for rel, sig in (remote or {}).items() if list(index.get(rel) or []) != list(sig))


def split_command(argv: list) -> tuple[list, list]:
    """vm_job's own arguments, and the job's command after the first `--`."""
    if "--" not in argv:
        return list(argv), []
    i = argv.index("--")
    return list(argv[:i]), list(argv[i + 1:])


def local_dir(name: str, job_id: str, root: str = OUT) -> str:
    ws._job_dir(job_id)   # validates the id
    return os.path.join(root, name, job_id)


def sync_outputs(run, job_id: str, dest: str) -> dict:
    """One poll: the job's state, then every new or changed output file into `dest`
    (written to a temporary name and renamed, so a partial download never replaces a
    good copy). `run(fn, *args)` calls a worker_side function on the job's VM."""
    os.makedirs(dest, exist_ok=True)
    st = run(ws.job_state, job_id)
    ip = os.path.join(dest, ".manifest.json")
    index = json.load(open(ip)) if os.path.exists(ip) else {}
    got = []
    for rel in files_to_fetch(st["files"], index):
        size = st["files"][rel][0]
        target = os.path.join(dest, rel)
        if not os.path.realpath(target).startswith(os.path.realpath(dest) + os.sep):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp, have = target + ".part", 0
        with open(tmp, "wb") as f:
            while have < size:
                b = run(ws.job_chunk, job_id, rel, have, min(CHUNK, size - have))
                if not b:
                    break
                f.write(b)
                have += len(b)
        os.replace(tmp, target)
        index[rel] = st["files"][rel]
        got.append(rel)
    json.dump(index, open(ip, "w"), indent=1)
    json.dump(st["state"], open(os.path.join(dest, "state.json"), "w"), indent=1)
    st["fetched"] = got
    return st


def _tarball(tree: str | None, src_dir: str | None) -> bytes | None:
    if tree:
        if subprocess.check_output(["git", "-C", tree, "status", "--porcelain"], text=True).strip():
            raise SystemExit(f"{tree} has uncommitted changes: commit them (the job ships HEAD)")
        return subprocess.check_output(["git", "-C", tree, "archive", "--format=tar.gz", "HEAD"])
    if src_dir:
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            t.add(src_dir, arcname=".")
        return b.getvalue()
    return None


def _client(name):
    c = rc.attach(name).get_client()
    w = sorted(c.scheduler_info()["workers"])
    if not w:
        raise SystemExit(f"{name} has no worker yet")
    c.upload_file(os.path.join(HERE, "worker_side.py"))   # the side functions below live in it
    return c, w[0]


def _runner(c, w):
    return lambda fn, *a: c.run(fn, *a, workers=[w], wait=True, on_error="raise")[w]


def cmd_up(a):
    import coiled
    for _ in range(40):
        act = rc.keeper_wait(rc.cluster_states(a.name))
        if act == "stop":
            print(f"{a.name} is already up")
            return
        if act == "start":
            break
        time.sleep(15)    # stopping: never create (or attach) until it has stopped
    else:
        raise SystemExit(f"{a.name} is still stopping; try again later")
    cl = coiled.Cluster(name=a.name, workspace=rc.WORKSPACE, n_workers=1, worker_cpu=a.cpu, worker_memory=a.memory,
                        scheduler_cpu=2, spot_policy="on-demand", idle_timeout=a.idle_timeout,
                        shutdown_on_close=False)
    cl.get_client().wait_for_workers(1, timeout=1500)
    print(f"{a.name} up (idle timeout {a.idle_timeout}; a running job holds it regardless)")


def cmd_start(a):
    from distributed import fire_and_forget
    cmd = a.command
    if not cmd:
        raise SystemExit("no command given (put it after --)")
    code = _tarball(a.tree, a.dir)
    secrets = {"ANTHROPIC_API_KEY": rc.read_keys()["ANTHROPIC_API_KEY"]} if a.anthropic_key else None
    c, w = _client(a.name)
    token = _runner(c, w)(ws.job_queue, a.job_id)
    if not token:
        raise SystemExit(f"{a.job_id} is queued or running on {a.name}; pick another id")
    shell = " ".join(cmd) if len(cmd) == 1 else cmd
    fut = c.submit(ws.job_task, a.job_id, code, shell, secrets, a.max_hours, a.setup_sdk, token=token,
                   workers=[w], allow_other_workers=False, pure=False, key=f"job-{a.job_id}-{int(time.time())}")
    fire_and_forget(fut)
    dest = local_dir(a.name, a.job_id, a.out)
    os.makedirs(dest, exist_ok=True)
    json.dump({"name": a.name, "worker": w, "job_id": a.job_id, "cmd": cmd, "submitted": time.strftime("%F %T"),
               "max_hours": a.max_hours}, open(os.path.join(dest, "job.json"), "w"), indent=1)
    print(f"started {a.job_id} on {a.name} ({w}); outputs -> {dest}")


def _print(st, dest):
    s = st["state"]
    extra = f" rc={s.get('rc')} {s.get('duration_s')}s" if s.get("state") in DONE else \
        (f" running {round(time.time() - s['started'])}s" if s.get("started") else "")
    print(f"{s.get('job_id', '?')}: {s['state']}{extra}; {len(st['files'])} file(s), fetched {len(st['fetched'])} "
          f"-> {dest}", flush=True)
    print("  " + "\n  ".join(st["log_tail"].strip().splitlines()[-5:]), flush=True)


def cmd_status(a):
    c, w = _client(a.name)
    dest = local_dir(a.name, a.job_id, a.out)
    st = sync_outputs(_runner(c, w), a.job_id, dest)
    _print(st, dest)
    return st


def cmd_wait(a):
    dest = local_dir(a.name, a.job_id, a.out)
    while True:
        try:
            c, w = _client(a.name)
            st = sync_outputs(_runner(c, w), a.job_id, dest)
            _print(st, dest)
            if st["state"]["state"] in DONE + ("missing", "lost"):
                return st
        except Exception as e:  # noqa: BLE001 — a dropped connection is not the end of the job
            if isinstance(e, SystemExit):
                raise
            print(f"poll failed ({type(e).__name__}: {str(e)[:120]}); retrying")
        time.sleep(a.every)


def cmd_down(a):
    rc.attach(a.name).shutdown()
    print(f"{a.name}: shutdown requested")


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    ok(split_command(["start", "n", "j", "--dir", "d", "--", "python", "x.py", "--dir", "e"])
       == (["start", "n", "j", "--dir", "d"], ["python", "x.py", "--dir", "e"]) and split_command(["status", "n", "j"])
       == (["status", "n", "j"], []), "RC34b options before -- are vm_job's; everything after is the job's")
    ok(files_to_fetch({"a": [1, 2.0], "b": [3, 4.0], "c": [5, 6.0]}, {"a": [1, 2.0], "b": [3, 9.0]}) == ["b", "c"],
       "RC34 a poll fetches only new or changed files")
    for bad in ("../x", "a/b", "", "x" * 65, ".."):
        try:
            local_dir("n", bad)
            ok(False, f"RC35 a bad job id is refused: {bad!r}")
        except ValueError:
            ok(True, f"RC35 a bad job id is refused: {bad[:10]!r}")
    now = 1000.0
    ok(ws.job_view(None, now)["state"] == "missing" and ws.job_view({"state": "running", "heartbeat": now - 10}, now)
       ["state"] == "running" and ws.job_view({"state": "running", "heartbeat": now - 999}, now)["state"] == "lost"
       and ws.job_view({"state": "done", "heartbeat": 0}, now)["state"] == "done",
       "RC36 a running job whose heartbeat stopped reads as lost")
    with tempfile.TemporaryDirectory() as d:
        old = ws.JOBS
        ws.JOBS = os.path.join(d, "vm")
        try:
            code = io.BytesIO()
            with tarfile.open(fileobj=code, mode="w:gz") as t:
                src = b"import os, time\nfor i in range(3):\n    open(os.path.join(os.environ['JOB_OUT'], f'step{i}.txt'), 'w').write(str(i))\n    print('step', i, flush=True)\n    time.sleep(0.2)\nprint('key set:', bool(os.environ.get('SECRET_X')))\n"
                ti = tarfile.TarInfo("job.py")
                ti.size = len(src)
                t.addfile(ti, io.BytesIO(src))
            st = ws.job_task("j1", code.getvalue(), [sys.executable, "job.py"], {"SECRET_X": "s3cr3t"}, 1.0,
                             heartbeat_s=1)
            ok(st["state"] == "done" and st["rc"] == 0, f"RC37 a job runs to completion in its own tree: {st}")
            raw = open(os.path.join(ws.JOBS, "j1", "state.json")).read()
            ok("s3cr3t" not in raw and "SECRET_X" not in raw, "RC38 secrets reach the job's environment, never its state file")
            run = lambda fn, *a: fn(*a)  # noqa: E731
            dest = os.path.join(d, "local")
            s1 = sync_outputs(run, "j1", dest)
            ok(sorted(s1["fetched"]) == ["job.log", "out/step0.txt", "out/step1.txt", "out/step2.txt"]
               and open(os.path.join(dest, "out", "step2.txt")).read() == "2"
               and "key set: True" in open(os.path.join(dest, "job.log")).read(),
               f"RC39 a poll saves the job's outputs and log locally: {s1['fetched']}")
            ok(sync_outputs(run, "j1", dest)["fetched"] == [], "RC40 a second poll with nothing new fetches nothing")
            for bad in ("work/job.py", "state.json", "../../etc/passwd", "out/../state.json"):
                try:
                    ws.job_chunk("j1", bad, 0, 10)
                    ok(False, f"RC41 job_chunk refuses {bad}")
                except (ValueError, OSError):
                    ok(True, f"RC41 job_chunk refuses {bad}")
            st2 = ws.job_task("j2", None, [sys.executable, "-c", "import time; time.sleep(30)"], None, 1 / 3600,
                              heartbeat_s=1)
            ok(st2["state"] == "timed_out", f"RC42 a job past max_hours is stopped: {st2['state']}")
            st3 = ws.job_task("j3", None, ["--no-such-program-x"], None, 1.0)
            ok(st3["state"] == "failed" and "FileNotFoundError" in st3.get("error", "")
               and ws.job_state("j3")["state"]["state"] == "failed",
               f"RC44 a job that cannot start is recorded as failed, not left 'running': {st3.get('error')}")
            tok = ws.job_queue("j4")
            ok(tok and ws.job_view(ws._read_json(os.path.join(ws.JOBS, "j4", "state.json")), time.time() + 60)
               ["state"] == "queued" and "token" not in ws.job_state("j4")["state"]
               and ws.job_queue("j4") is None
               and ws.job_task("j4", None, "true", None, 1.0, token="other")["state"] == "refused"
               and ws.job_task("j4", None, "true", None, 1.0)["state"] == "refused"
               and ws.job_task("j4", None, "true", None, 1.0, token=tok)["state"] == "done",
               "RC45 a submitted job reads as queued until its own task claims it; a second submission is refused")
            ok(ws.job_view({"state": "queued", "queued": 0.0}, 99999.0)["state"] == "lost",
               "RC45c a queued job whose task never started reads as lost after an hour")
            calls = []
            orig = ws._job_write
            ws._job_write = lambda d_, st_: (calls.append(st_.get("heartbeat")), orig(d_, st_))
            try:
                logf = open(os.path.join(d, "beat.log"), "wb")
                ws._beat_run([sys.executable, "-c", "import time; time.sleep(1.2)"], logf, os.path.join(ws.JOBS, "j4"),
                             {"state": "running"}, 60, 0.3)
                logf.close()
            finally:
                ws._job_write = orig
            ok(len(calls) >= 2, f"RC45d setup commands keep the job's heartbeat up ({len(calls)} beats)")
            ws._job_write(os.path.join(ws.JOBS, "j2"), {"state": "running", "heartbeat": time.time()})
            ok(not ws.job_queue("j2"), "RC45b a running job id is not queued again")
            ok(ws.job_task("j2", None, "true", None, 1.0)["state"] == "refused",
               "RC43 a job id that is running is not restarted")
        finally:
            ws.JOBS = old
    _self_test_sched(ok)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_sched(ok) -> None:
    """`_client` reads the VM's worker table through worker_side's bounded reader (PR #99's
    race; no network): Client.scheduler_info() is {} while the client is not connected."""
    import types
    from unittest import mock

    class FakeClient:
        def __init__(self, infos, status="running"):
            self.infos, self.status, self.uploads = list(infos), status, 0

        def scheduler_info(self):
            return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

        def upload_file(self, *a):
            self.uploads += 1

    table = {"tls://10.0.0.7:1": {}, "tls://10.0.0.5:1": {}}

    def attach(clients):
        it = iter(clients)
        return lambda name: types.SimpleNamespace(get_client=lambda: next(it, clients[-1]))

    sleeps = []
    with mock.patch.object(time, "sleep", sleeps.append):
        c = FakeClient([{}, {"workers": table}])
        with mock.patch.object(rc, "attach", attach([c])):
            try:
                got = _client("vm1")
            except BaseException as e:  # noqa: BLE001
                got = f"{type(e).__name__}: {e}"
        ok(got == (c, "tls://10.0.0.5:1") and c.uploads == 1 and len(sleeps) == 1,
           f"VJ1 _client waits out an unpopulated identity, then uses the first worker: {got}")
        closed, fresh = FakeClient([{}], status="closed"), FakeClient([{"workers": table}])
        with mock.patch.object(rc, "attach", attach([closed, fresh])):
            try:
                got = _client("vm1")
            except BaseException as e:  # noqa: BLE001
                got = f"{type(e).__name__}: {e}"
        ok(got == (fresh, "tls://10.0.0.5:1") and fresh.uploads == 1 and not closed.uploads,
           f"VJ2 a closed client is replaced from the cluster before the upload: {got}")
        with mock.patch.object(rc, "attach", attach([FakeClient([{}])])):
            try:
                _client("vm1")
                ok(False, "VJ3 a worker table that never appears stops with a clear message")
            except SystemExit as e:
                ok("vm1" in str(e) and "worker table unavailable" in str(e) and "no worker yet" not in str(e),
                   f"VJ3 a worker table that never appears stops with a clear message, not 'no worker yet': {e}")
            except BaseException as e:  # noqa: BLE001
                ok(False, f"VJ3 a worker table that never appears stops with SystemExit, not {type(e).__name__}: {e}")
        with mock.patch.object(rc, "attach", attach([FakeClient([{"workers": {}}])])):
            try:
                _client("vm1")
                ok(False, "VJ4 a connected scheduler with no worker still says so")
            except SystemExit as e:
                ok("no worker yet" in str(e), f"VJ4 a connected scheduler with no worker still says so: {e}")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    argv, command = split_command(argv)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("name")
    up.add_argument("--cpu", type=int, default=4)
    up.add_argument("--memory", default="16 GiB")
    up.add_argument("--idle-timeout", default="20 minutes")
    st = sub.add_parser("start")
    st.add_argument("name")
    st.add_argument("job_id")
    g = st.add_mutually_exclusive_group()
    g.add_argument("--tree")
    g.add_argument("--dir")
    st.add_argument("--max-hours", type=float, default=24.0)
    st.add_argument("--setup-sdk", action="store_true")
    st.add_argument("--anthropic-key", action="store_true")
    for nm in ("status", "wait"):
        p = sub.add_parser(nm)
        p.add_argument("name")
        p.add_argument("job_id")
        if nm == "wait":
            p.add_argument("--every", type=float, default=300)
    dn = sub.add_parser("down")
    dn.add_argument("name")
    for p in sub.choices.values():
        p.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    a.command = command
    {"up": cmd_up, "start": cmd_start, "status": cmd_status, "wait": cmd_wait, "down": cmd_down}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
