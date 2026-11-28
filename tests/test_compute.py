#!/usr/bin/env python3
"""Local backend, ledger, reuse, and fingerprinting tests (extracted from scripts/compute.py)."""
from __future__ import annotations

import hashlib
import contextlib
import io
import json
import os
import shutil
import subprocess as _sp
import sys
import tarfile
import tempfile
import threading
import time
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(REPO, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import compute as C
import compute_worker as CW
from compute import (
    CAP_STOP, FP_BASE_PARTS, FP_DATA_SET, FP_HARNESS_OUTPUTS, FP_SIGNOFF_RECORDS,
    INFRA, JOB_EXT, LEDGER_REL, MODEL, PARTIAL_EXT, PROGRESS_DIR,
    PROGRESS_HEARTBEAT_S, PROGRESS_STALE_LEGACY_S, PROGRESS_STALE_S,
    SHARE_STOP_GRACE_S, STOPPED,
    _CALLS_DIR, _avail_mb, _breaker, _cancel_call, _claim, _data_set_files,
    _digest, _file_digest, _live_reservations, _mem_slots, _pid_alive, _pid_start,
    _proc_stat, _ps_start, _remote_task, _revoke_chunk, _settle_dead, _stop_chunk,
    _stop_orphans, code_fingerprint, export_worker_env, fn_id, input_keys,
    make_bundle, map_fn, recycle_every, reuse_report, run_map, step_wall_s,
    _out_files, _killpg,
)


def _self_test_iso_repo(root: str, *, with_costs: bool = False) -> str:
    """Minimal isolated REPO tree inside a temp dir so self-test bundle/worker calls never touch live REPO."""
    repo = os.path.realpath(root)
    os.makedirs(os.path.join(repo, "scripts"), exist_ok=True)
    shutil.copy2(os.path.join(SCRIPTS_DIR, "compute.py"), os.path.join(repo, "scripts", "compute.py"))
    shutil.copy2(os.path.join(SCRIPTS_DIR, "compute_worker.py"), os.path.join(repo, "scripts", "compute_worker.py"))
    shutil.copytree(os.path.join(SCRIPTS_DIR, "lib"), os.path.join(repo, "scripts", "lib"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(repo, "cloud"), exist_ok=True)
    shutil.copy2(os.path.join(REPO, "cloud", "worker_side.py"), os.path.join(repo, "cloud", "worker_side.py"))
    if with_costs:
        os.makedirs(os.path.join(repo, "toolkit", "alloc"), exist_ok=True)
        open(os.path.join(repo, "toolkit", "alloc", "costs.py"), "w").close()
    return repo


@contextlib.contextmanager
def _self_test_patched_repo(iso_repo: str):
    """Patch C.REPO/CW.REPO to `iso_repo` while preventing `sys.modules['ma_compute_worker_side']` from leaking."""
    prev = sys.modules.pop("ma_compute_worker_side", None)
    try:
        with mock.patch.object(C, "REPO", iso_repo), mock.patch.object(CW, "REPO", iso_repo):
            yield
    finally:
        sys.modules.pop("ma_compute_worker_side", None)
        if prev is not None:
            sys.modules["ma_compute_worker_side"] = prev


def _self_test() -> int:
    import shutil
    import subprocess as _sp
    import tempfile
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as _iso_td:
        iso_repo = _self_test_iso_repo(_iso_td, with_costs=True)
        os.makedirs(os.path.join(d, "models"))
        os.makedirs(os.path.join(d, "data"))
        open(os.path.join(d, "data", "scale.txt"), "w").write("3\n")
        open(os.path.join(d, "models", "helper.py"), "w").write("def scale():\n    return float(open('data/scale.txt').read())\n")
        open(os.path.join(d, "models", "fn.py"), "w").write(
            "from helper import scale\n"
            "def f(p):\n"
            "    if p.get('boom'):\n        raise ValueError('bad draw')\n"
            "    return {'y': p['x'] * scale()}\n")
        inputs = [{"x": k} for k in range(40)] + [{"x": 0, "boom": True}]
        for workers in (1, 3):
            rows, s = map_fn(d, "models/fn.py::f", inputs, backend="local", max_workers=workers)
            ok([r["i"] for r in rows] == list(range(41)), f"C1 order kept (workers={workers})")
            ok(rows[5]["ok"] and rows[5]["result"] == {"y": 15.0}, f"C2 relative paths + sibling import (workers={workers}): {rows[5]}")
            ok(not rows[40]["ok"] and "bad draw" in rows[40]["error"] and s["n_failed"] == 1,
               f"C3 a failing row fails alone (workers={workers}): {rows[40]}")
        led = json.load(open(os.path.join(d, LEDGER_REL)))
        ok(len(led["calls"]) == 2, f"C4 ledger records each call: {led}")
        os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = "0"
        try:
            map_fn(d, "models/fn.py::f", inputs[:2], backend="local")
            ok(False, "C5 core-hour cap must refuse")
        except RuntimeError:
            pass
        finally:
            del os.environ["MA_COMPUTE_MAX_CORE_HOURS"]
        try:
            map_fn(d, "models/fn.py::f", inputs, backend="local", max_tasks=10)
            ok(False, "C6 max-tasks must refuse")
        except ValueError:
            pass
        os.symlink(os.path.join(d, "data", "scale.txt"), os.path.join(d, "data", "link.txt"))
        with _self_test_patched_repo(iso_repo):
            b, info = make_bundle(d)
        lk = [m for m in tarfile.open(fileobj=io.BytesIO(b)).getmembers() if m.name.endswith("data/link.txt")]
        ok(lk and lk[0].isfile(), "C7b a symlinked input is bundled as a file")
        os.remove(os.path.join(d, "data", "link.txt"))
        names = tarfile.open(fileobj=io.BytesIO(b)).getnames()
        ok(any(n.endswith("models/fn.py") and not n.startswith("/") for n in names)
           and any(n.endswith("scripts/compute.py") for n in names)
           and any(n.endswith("scripts/compute_worker.py") for n in names)
           and any(n.endswith("toolkit/alloc/costs.py") for n in names),
           f"C7 bundle has models/, scripts/ and toolkit/ (a run model may import toolkit.alloc): {info}")
        open(os.path.join(d, "models", "slow.py"), "w").write(
            "import os, time\n"
            "def sleep(p):\n    time.sleep(p['s']); return {'ok': 1}\n"
            "def burn(p):\n    t = time.time()\n    while time.time() - t < p['s']: pass\n    return {'ok': 1}\n"
            "def die(p):\n    os._exit(3)\n")
        t0 = time.time()
        rows, s = map_fn(d, "models/fn.py::f", [], backend="local")
        ok(rows == [] and s["n"] == 0, f"C10 an empty batch is a no-op: {s}")
        rows, s = map_fn(d, "models/slow.py::sleep", [{"s": 2.5}] * 4, backend="local", max_workers=2, timeout_s=0.6)
        ok(time.time() - t0 < 4.5 and all(r["error"] == "timed out" for r in rows) and s["stopped"] == "timed out",
           f"C10 local timeout bounds the call ({time.time() - t0:.1f}s): {s}")
        t0 = time.time()
        rows, s = map_fn(d, "models/slow.py::sleep", [{"s": 1.5}] * 2, backend="local", max_workers=2, timeout_s=1e-6)
        ok(time.time() - t0 < 2.5 and all(not r["ok"] for r in rows),
           f"C10b a stop during pool startup does not wait for the model ({time.time() - t0:.2f}s)")
        cwd = os.getcwd()
        try:
            os.chdir(os.path.dirname(d))
            rows, _ = map_fn(os.path.basename(d), "models/fn.py::f", [{"x": 2}], backend="local")
            ok(rows[0].get("result") == {"y": 6.0} and os.path.realpath(os.getcwd()) == os.path.realpath(os.path.dirname(d)),
               f"C11 relative run_dir resolves; caller cwd unchanged: {rows[0]}")
        finally:
            os.chdir(cwd)
        ys = []
        for v in (1, 2):
            rd = os.path.join(d, f"iso{v}")
            os.makedirs(os.path.join(rd, "models"))
            open(os.path.join(rd, "models", "helper.py"), "w").write(f"V = {v}\n")
            open(os.path.join(rd, "models", "g.py"), "w").write("import helper\ndef g(p):\n    return helper.V\n")
            ys.append(map_fn(rd, "models/g.py::g", [{}], backend="local", max_workers=1)[0][0].get("result"))
        ok(ys == [1, 2], f"C12 each run gets its own sibling modules: {ys}")
        import threading
        rd = os.path.join(d, "led"); os.makedirs(os.path.join(rd, "models"))
        open(os.path.join(rd, "models", "slow.py"), "w").write(open(os.path.join(d, "models", "slow.py")).read())
        out = {}

        def call(tag, fn="models/slow.py::sleep", params=None, **kw):
            try:
                out[tag] = map_fn(rd, fn, [params or {"s": 1.0}], backend="local", **kw)[1]
            except RuntimeError as e:
                out[tag] = str(e)
        # a's task waits on a gate the test holds, so a's reservation is held
        # for certain while b is checked (not for "about a second").
        open(os.path.join(rd, "models", "gated13.py"), "w").write(
            "import os, time\ndef f(p):\n    end = time.time() + 300\n"
            "    while os.path.exists(p['gate']) and time.time() < end:\n        time.sleep(0.02)\n"
            "    time.sleep(1.0)\n    return {'ok': 1}\n")     # 1 s of work after release, as before
        gate13 = os.path.join(d, "gate13")
        open(gate13, "w").close()
        os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = "1"
        try:
            ta = threading.Thread(target=call, args=("a", "models/gated13.py::f", {"gate": gate13})); ta.start()
            end = time.time() + 60                # a's reservation is in the ledger
            while time.time() < end:
                try:
                    if json.load(open(os.path.join(rd, LEDGER_REL))).get("reservations"):
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.01)
            call("b")
            os.remove(gate13)                     # release a only after b's check
            ta.join()
            ok(isinstance(out["a"], dict) and "held by running calls" in str(out["b"]),
               f"C13 a running call's reservation blocks a concurrent call: {out}")
            ts = [threading.Thread(target=call, args=(k,), kwargs={"max_core_hours": 0.1}) for k in ("c", "e")]
            [t.start() for t in ts]; [t.join() for t in ts]
            led = json.load(open(os.path.join(rd, LEDGER_REL)))
            ok(all(isinstance(out[k], dict) for k in "ce") and len(led["calls"]) == 3 and not led["reservations"]
               and led["core_seconds"] >= 2.9, f"C13 concurrent calls both charged, no lost update: {led}")
            os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = str(1.5 / 3600)
            os.remove(os.path.join(rd, LEDGER_REL))
            t0 = time.time()
            rows, s = map_fn(rd, "models/slow.py::burn", [{"s": 0.4}] * 30, backend="local", max_workers=1)
            ok(s["stopped"] == CAP_STOP and s["n_ok"] < 30 and time.time() - t0 < 6
               and any(r.get("error") == CAP_STOP for r in rows), f"C14 the cap stops a batch midway: {s}")
            os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = "200"
            rows, s = map_fn(rd, "models/slow.py::die", [{}] * 2, backend="local", max_workers=1)
            ok(s["n_failed"] == 2 and s["task_core_s"] > 0 and s["failed_by_kind"] == {"model": 2},
               f"C15 a crashed process is still charged, and an input that kills its process alone is a model error: {s}")
        finally:
            del os.environ["MA_COMPUTE_MAX_CORE_HOURS"]
        bad = os.path.join(d, "badled"); os.makedirs(os.path.join(bad, "models"))
        open(os.path.join(bad, LEDGER_REL), "w").write('{"core_seconds": 7')
        try:
            map_fn(bad, "models/fn.py::f", [{"x": 1}], backend="local")
            ok(False, "C15b a truncated ledger must refuse, not reset the budget")
        except RuntimeError:
            pass
        with _self_test_patched_repo(iso_repo):
            vd = os.path.realpath(os.path.join(d, "ver")); os.makedirs(os.path.join(vd, "models"))
            vfile, hfile = os.path.join(vd, "models", "v.py"), os.path.join(vd, "models", "oldhelper.py")

            def stage(body, helper):
                # same length, same mtime: only a fresh compile tells A from B
                open(vfile, "w").write(body); os.utime(vfile, (1_700_000_000, 1_700_000_000))
                if helper:
                    open(hfile, "w").write("X = 73\n")
                elif os.path.exists(hfile):
                    os.remove(hfile)
                return make_bundle(vd)
            ba, ia = stage("def v(p):\n    return 'A'\n", True)
            bb, ib = stage("def v(p):\n    return 'B'\n", False)
            bc, ic = stage("def v(p):\n    import oldhelper\n    return oldhelper.X\n", False)

            def remote(b_, i_):
                return _remote_task(b_, i_["sha256"], vd, "models/v.py::v", [(0, {})], procs=1)
            got = [remote(b_, i_)["rows"][0].get("result") for b_, i_ in ((ba, ia), (bb, ib), (ba, ia), (bb, ib))]
            ok(got == ["A", "B", "A", "B"], f"C16 a chunk runs its own bundle's code (same size+mtime): {got}")
            remote(ba, ia)
            r = remote(bc, ic)["rows"][0]
            ok(not r["ok"] and "oldhelper" in r.get("error", ""),
               f"C16b a file absent from the bundle is gone, not left over from an earlier one: {r}")
            open(vfile, "w").write("import os\ndef v(p):\n    os._exit(3)\n")
            bd, id_ = make_bundle(vd)
            r = remote(bd, id_)
            ok(not r["rows"][0]["ok"] and r["core_s"] > 0, f"C21 a remote chunk whose process crashed is charged: {r}")
        open(os.path.join(d, "models", "stubborn.py"), "w").write(
            "import signal, time\ndef f(p):\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    t = time.time()\n    while time.time() - t < 60: pass\n")
        t0 = time.time()
        rows, s = map_fn(d, "models/stubborn.py::f", [{}] * 2, backend="local", max_workers=2, timeout_s=1.0)
        ok(time.time() - t0 < 10 and all(r["error"] == "timed out" for r in rows),
           f"C18 a model ignoring SIGTERM still stops at the timeout ({time.time() - t0:.1f}s)")
        rd2 = os.path.join(d, "abort"); os.makedirs(os.path.join(rd2, "models"))
        def boom(*a, progress=None, **k):
            progress.update(slots=2, cores_per_slot=1, core_s=1.0)
            time.sleep(0.2)
            raise KeyboardInterrupt
        with mock.patch.object(C, "_map_local", boom):
            try:
                map_fn(rd2, "models/fn.py::f", [{"x": 1}], backend="local")
                ok(False, "C19 interrupt must propagate")
            except KeyboardInterrupt:
                pass
        led = json.load(open(os.path.join(rd2, LEDGER_REL)))
        ok(led["core_seconds"] >= 1.9 and not led["reservations"] and "interrupted" in led["calls"][-1]["stopped"],
           f"C19 an interrupted call is charged an upper bound, not zero: {led}")
        import subprocess as _sp
        dead = _sp.Popen([sys.executable, "-c", "pass"]); dead.wait()
        led["reservations"] = {"x": {"core_s": 500.0, "pid": dead.pid, "host": os.uname().nodename, "fn": "f"}}
        json.dump(led, open(os.path.join(rd2, LEDGER_REL), "w"))
        before = led["core_seconds"]
        map_fn(rd2, "models/fn.py::f", [], backend="local")
        led = json.load(open(os.path.join(rd2, LEDGER_REL)))
        ok(led["core_seconds"] >= before + 500 and not led["reservations"],
           f"C20 a dead caller's reservation is charged, not dropped: {led['core_seconds']} vs {before}")
        class _Sec:
            tls_ca_file = os.path.join(d, "w_ca.pem"); tls_client_cert = None; tls_client_key = None
            tls_worker_cert = os.path.join(d, "w_cert.pem"); tls_worker_key = os.path.join(d, "w_key.pem")
        class _W:
            security = _Sec()
            class scheduler:
                address = "tls://10.0.0.1:8786"
        for fn_ in ("w_ca.pem", "w_cert.pem", "w_key.pem"):
            open(os.path.join(d, fn_), "w").write("-----BEGIN X-----\n" + fn_ + "\n")
        tdir = os.path.join(d, "tls"); os.makedirs(tdir, mode=0o755); os.chmod(tdir, 0o755)
        open(os.path.join(tdir, "key.pem"), "w").write("old"); os.chmod(os.path.join(tdir, "key.pem"), 0o644)
        env = export_worker_env(tdir, worker=_W())
        mode = lambda p: os.stat(p).st_mode & 0o777  # noqa: E731
        ok(env["MA_COMPUTE_TLS_DIR"] == tdir and mode(tdir) == 0o700 and mode(os.path.join(tdir, "key.pem")) == 0o600
           and "w_key.pem" in open(os.path.join(tdir, "key.pem")).read()
           and "w_cert.pem" in open(os.path.join(tdir, "cert.pem")).read(),
           "C17 worker credentials exported 0600 in a 0700 dir, replacing an existing world-readable key")
        t0 = time.time()
        rows, s = map_fn(d, "models/stubborn.py::f", [{}] * 4, backend="local", max_workers=4, timeout_s=1.0)
        ok(time.time() - t0 < 5, f"C22 stubborn processes are killed together, not one by one ({time.time() - t0:.1f}s)")
        open(os.path.join(d, "models", "spawner.py"), "w").write(
            "import subprocess\ndef f(p):\n    return subprocess.Popen(['sleep', '60']).pid\n")
        rows, _ = map_fn(d, "models/spawner.py::f", [{}], backend="local")
        end = time.time() + 10                    # a SIGKILL lands asynchronously: wait for it, bounded
        while time.time() < end and _pid_alive(rows[0]["result"]):
            time.sleep(0.02)
        try:
            os.kill(rows[0]["result"], 0)
            alive = not open(f"/proc/{rows[0]['result']}/stat").read().split()[2] == "Z" if os.path.exists("/proc") else \
                _sp.run(["ps", "-o", "stat=", "-p", str(rows[0]["result"])], capture_output=True, text=True).stdout.strip()[:1] not in ("", "Z")
        except ProcessLookupError:
            alive = False
        ok(rows[0]["ok"] and not alive, f"C23 a process the model left running is killed with the batch: {rows[0]}")
        with _self_test_patched_repo(iso_repo):
            sd = os.path.realpath(os.path.join(d, "stage")); os.makedirs(os.path.join(sd, "models")); os.makedirs(os.path.join(sd, "data"))
            open(os.path.join(sd, "models", "v.py"), "w").write("def v(p):\n    return 1\n")
            open(os.path.join(sd, "data", "source.csv"), "w").write("a\n")
            b1, i1 = make_bundle(sd)
            os.remove(os.path.join(sd, "data", "source.csv"))
            b2, i2 = make_bundle(sd)
            _remote_task(b1, i1["sha256"], sd, "models/v.py::v", [(0, {})], procs=1)
            livedir = os.path.join(d, "live_run"); os.makedirs(os.path.join(livedir, "data"))
            open(os.path.join(livedir, "metadata.json"), "w").write("{}")
            open(os.path.join(livedir, "data", "source.csv"), "w").write("live\n")
            shutil.rmtree(os.path.join(sd, "data")); os.symlink(os.path.join(livedir, "data"), os.path.join(sd, "data"))
            r = _remote_task(b2, i2["sha256"], sd, "models/v.py::v", [(0, {})], procs=1)
            ok(os.path.exists(os.path.join(livedir, "data", "source.csv")),
               f"C24 staging never deletes through a symlink: {r['rows'][0]}")
            bl, il = make_bundle(livedir)   # a bundle whose target dirs run through the symlink
            os.makedirs(os.path.join(sd, "data", "x"), exist_ok=True)
            open(os.path.join(sd, "data", "x", "f.txt"), "w").write("1")
            b3, i3 = make_bundle(sd)
            r = _remote_task(b3, i3["sha256"], sd, "models/v.py::v", [(0, {})], procs=1)
            ok(not r["rows"][0]["ok"] and "symlink" in r["rows"][0]["error"],
               f"C24b staging refuses to write through a symlinked directory: {r['rows'][0]}")
            cdir = os.path.realpath(os.path.join(d, "cap")); os.makedirs(os.path.join(cdir, "models"))
            open(os.path.join(cdir, "models", "slow.py"), "w").write(open(os.path.join(d, "models", "slow.py")).read())
            bc_, ic_ = make_bundle(cdir)
            t0 = time.time()
            r = _remote_task(bc_, ic_["sha256"], cdir, "models/slow.py::burn", [(k, {"s": 0.4}) for k in range(20)],
                             procs=1, chunk_allowance_s=1.0)
            # core_s and the cap-stopped rows show the share is enforced; the wall-clock
            # limit only catches a hang (it was 8 s and failed on a loaded machine).
            ok(time.time() - t0 < 30 and any(x.get("error") == CAP_STOP for x in r["rows"]) and r["core_s"] < 3,
               f"C25 a remote chunk enforces its allowance share: core_s={r['core_s']}")
            import threading as _th
            import uuid as _uuid
            res, cid = {}, "selftest-" + _uuid.uuid4().hex
            th = _th.Thread(target=lambda: res.update(r=_remote_task(bc_, ic_["sha256"], cdir, "models/slow.py::sleep",
                                                                     [(0, {"s": 60})], procs=1, call_id=cid)))
            th.start()
            # Readiness, not a fixed sleep (a loaded machine registered the chunk after 1.5 s):
            # wait until the chunk has registered its pid for this call.
            cdir_ = os.path.join(tempfile.gettempdir(), _CALLS_DIR, cid)
            end = time.time() + 60
            while time.time() < end and not any(n.isdigit() for n in (os.listdir(cdir_) if os.path.isdir(cdir_) else [])):
                time.sleep(0.02)
            t0 = time.time(); n_stopped = _cancel_call(cid); th.join(15)
            later = _remote_task(bc_, ic_["sha256"], cdir, "models/slow.py::sleep", [(0, {"s": 60})], procs=1, call_id=cid)
            ok(n_stopped == 1 and not th.is_alive() and not res["r"]["rows"][0]["ok"]
               and later["rows"][0]["error"] == "cancelled", f"C26 cancel stops running and queued chunks: {n_stopped}, {res}")
            open(os.path.join(d, "models", "heavy.py"), "w").write("import time\ntime.sleep(1.0)\ndef f(p):\n    return 1\n")
            rows, s = map_fn(d, "models/heavy.py::f", [{}] * 2, backend="local", max_workers=1)
            ok(rows[0]["ok"] and s["task_core_s"] >= 0.9, f"C28 model load time is charged: {s}")
            rows, s = map_fn(d, "models/heavy.py::f", [{}] * 3, backend="local", max_workers=3)
            ok(s["task_core_s"] >= 2.7, f"C28b every process's model load is charged, chunk or not: {s}")
            ev = os.path.realpath(os.path.join(d, "envf")); os.makedirs(os.path.join(ev, "models"))
            open(os.path.join(ev, "models", "e.py"), "w").write(
                "import os\ndef f(p):\n    return [os.environ.get(k) for k in ('AZURE_SELFTEST', 'ANTHROPIC_API_KEY', 'OK_VAR')]\n")
            be_, ie_ = make_bundle(ev)
            saved = {k: os.environ.get(k) for k in ("AZURE_SELFTEST", "ANTHROPIC_API_KEY", "OK_VAR")}
            os.environ.update(AZURE_SELFTEST="secret-a", ANTHROPIC_API_KEY="secret-b", OK_VAR="kept")
            try:
                r = _remote_task(be_, ie_["sha256"], ev, "models/e.py::f", [(0, {})], procs=1)["rows"][0]
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            ok(r.get("result") == [None, None, "kept"], f"C32 model code on a worker gets no credentials: {r}")
            dl = os.path.join(d, "dlink"); os.makedirs(os.path.join(dl, "models"))
            ext = os.path.join(d, "ext_data"); os.makedirs(ext); open(os.path.join(ext, "t.csv"), "w").write("1\n")
            os.symlink(ext, os.path.join(dl, "data"))
            os.symlink(os.path.join(dl, "data"), os.path.join(ext, "loop"))      # a cycle
            bd_, idd = make_bundle(dl)
            names_ = tarfile.open(fileobj=io.BytesIO(bd_)).getnames()
            ok(any(n.endswith("dlink/data/t.csv") for n in names_), f"C7c a symlinked data dir is bundled (cycle-safe): {idd}")
            al = os.path.join(d, "alias"); os.makedirs(os.path.join(al, "data", "real")); os.makedirs(os.path.join(al, "models"))
            open(os.path.join(al, "data", "real", "value.txt"), "w").write("73\n")
            os.symlink(os.path.join(al, "data", "real"), os.path.join(al, "data", "a_alias"))
            na = tarfile.open(fileobj=io.BytesIO(make_bundle(al)[0])).getnames()
            ok(any(n.endswith("data/real/value.txt") for n in na) and any(n.endswith("data/a_alias/value.txt") for n in na),
               f"C7d both names of an aliased dir are bundled: {[n for n in na if 'value' in n]}")
            ld = os.path.realpath(os.path.join(d, "leaf")); os.makedirs(os.path.join(ld, "models")); os.makedirs(os.path.join(ld, "data"))
            open(os.path.join(ld, "models", "v.py"), "w").write("def v(p):\n    return 1\n")
            open(os.path.join(ld, "data", "input.csv"), "w").write("bundle\n")
            bl_, il_ = make_bundle(ld)
            victim = os.path.join(d, "victim_run"); os.makedirs(os.path.join(victim, "data"))
            open(os.path.join(victim, "metadata.json"), "w").write("{}")
            open(os.path.join(victim, "data", "input.csv"), "w").write("live\n")
            os.remove(os.path.join(ld, "data", "input.csv"))
            os.symlink(os.path.join(victim, "data", "input.csv"), os.path.join(ld, "data", "input.csv"))
            _remote_task(bl_, il_["sha256"], ld, "models/v.py::v", [(0, {})], procs=1)
            ok(open(os.path.join(victim, "data", "input.csv")).read() == "live\n"
               and not os.path.islink(os.path.join(ld, "data", "input.csv")),
               "C30 staging never writes through a symlinked file")
        open(os.path.join(d, "models", "envq.py"), "w").write(
            "import os\ndef f(p):\n    return [os.environ.get('MA_COMPUTE_GATE'), os.environ.get('MA_COMPUTE_CANCEL')]\n")
        ed = os.path.join(d, "envgate"); os.makedirs(ed)
        inp = os.path.join(ed, "in.json"); json.dump([{}], open(inp, "w")); out = os.path.join(ed, "out.jsonl")
        pe = _sp.Popen([sys.executable, os.path.abspath(C.__file__), "map", d, "--fn", "models/envq.py::f", "--inputs", inp,
                        "--out", out, "--backend", "local", "--no-ledger"],
                       env=dict(os.environ, MA_COMPUTE_GATE=os.path.join(ed, "{pid}"), MA_COMPUTE_CANCEL=os.path.join(ed, "cancel")))
        open(os.path.join(ed, str(pe.pid)), "w").write("1")
        pe.wait(60)
        got = json.loads(open(out).readline())["result"] if os.path.exists(out) else "no output"
        ok(got == [None, None], f"C31 the registration gate is not inherited by the model: {got}")
        gdir = os.path.join(d, "gate"); os.makedirs(gdir)
        open(os.path.join(gdir, "cancel"), "w").write("1")
        inp = os.path.join(gdir, "in.json"); json.dump([{"x": 1}], open(inp, "w"))
        out = os.path.join(gdir, "out.jsonl")
        envg = dict(os.environ, MA_COMPUTE_GATE=os.path.join(gdir, "{pid}"), MA_COMPUTE_CANCEL=os.path.join(gdir, "cancel"))
        pg = _sp.Popen([sys.executable, os.path.abspath(C.__file__), "map", d, "--fn", "models/fn.py::f", "--inputs", inp,
                        "--out", out, "--backend", "local", "--no-ledger"], env=envg)
        time.sleep(0.2)
        ok(pg.poll() is None and not os.path.exists(out), "C29 a chunk does no work before it is registered")
        open(os.path.join(gdir, str(pg.pid)), "w").write(str(pg.pid))
        ok(pg.wait(30) == 3 and not os.path.exists(out), "C29 a chunk registered after a cancel exits without working")
    _self_test_p35(ok)
    _self_test_p37(ok)
    _self_test_p42(ok)
    try:
        _self_test_wall(ok)
    except Exception as e:  # noqa: BLE001
        ok(False, f"SW: {type(e).__name__}: {e}")
    try:
        _self_test_xhost(ok)
    except Exception as e:  # noqa: BLE001
        ok(False, f"XH: {type(e).__name__}: {e}")
    try:
        _self_test_s18(ok)
    except Exception as e:  # noqa: BLE001
        ok(False, f"RU: {type(e).__name__}: {e}")
    try:
        _self_test_signoff_reuse(ok)
    except Exception as e:  # noqa: BLE001
        ok(False, f"RS: {type(e).__name__}: {e}")
    try:
        _self_test_checkpoint_restore(ok)
    except Exception as e:  # noqa: BLE001
        ok(False, f"CP: {type(e).__name__}: {e}")
    try:
        _self_test_w0a(ok)
    except Exception as e:  # noqa: BLE001
        ok(False, f"FP_OBSERVED: {type(e).__name__}: {e}")
    try:
        _self_test_bundle_data_set(ok)
    except Exception as e:  # noqa: BLE001
        ok(False, f"BD: {type(e).__name__}: {e}")
    # CF-RW3 (round 4, P1-2): a batch's results, summary and journal never follow a planted link or .tmp
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as out_:
        os.makedirs(os.path.join(rd, "models"))
        open(os.path.join(rd, "models", "m.py"), "w").write("def f(p):\n    return {'y': p['x']}\n")
        victim = os.path.join(out_, "v")
        open(victim, "w").write("untouched")
        o = os.path.join(rd, "models", "r.jsonl")
        for suffix in (".tmp", ".summary.json.tmp", PARTIAL_EXT + ".tmp"):
            os.symlink(victim, o + suffix)
        try:
            run_map(rd, "models/m.py::f", [{"x": 1}], o, backend="local", max_workers=1)
        except (ValueError, OSError):
            pass
        ok(open(victim).read() == "untouched", "CF-RW3 run_map writes nothing through a planted .tmp link")
    # CF-RW1/2 (run console review round 3, P1-3): a confined fingerprint reads through no-follow fds and equals the
    # default one on a tree with no links; the default is unchanged
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as out:
        os.makedirs(os.path.join(rd, "models"))
        os.makedirs(os.path.join(rd, "data"))
        open(os.path.join(rd, "models", "m.py"), "w").write("def f(x):\n    return x\n")
        open(os.path.join(rd, "data", "d.csv"), "w").write("a\n1\n")
        try:
            same = code_fingerprint(rd, "models/m.py::f") == code_fingerprint(rd, "models/m.py::f", confined=True)
        except TypeError as e:
            same = e
        ok(same is True, f"CF-RW1 the confined fingerprint equals the default on a tree with no links ({same})")
        os.makedirs(os.path.join(out, "models"))
        open(os.path.join(out, "models", "m.py"), "w").write("SECRET = 1\n")
        import shutil
        shutil.rmtree(os.path.join(rd, "models"))
        os.symlink(os.path.join(out, "models"), os.path.join(rd, "models"))
        try:
            code_fingerprint(rd, "models/m.py::f", confined=True)
            r = "not refused"
        except ValueError:
            r = "refused"
        except TypeError as e:
            r = e
        ok(r == "refused" and isinstance(code_fingerprint(rd, "models/m.py::f"), str),
           f"CF-RW2 a linked models/ is refused by the confined fingerprint; the default still follows it ({r})")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_s18(ok) -> None:
    """F6 S1-8: the fingerprint covers code content only. The launch commit stamp, harness-written analysis outputs
    under models/ and the spec do not change it; code, data and any unknown file under models/ still do; a stored
    batch is found again from another tree, and `reuse_report` says how many stored rows would be reused."""
    import re as _re
    import shutil
    import subprocess as _sp
    import tempfile
    from unittest import mock
    rep = C.reuse_report
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        repo_a, rd = os.path.join(td, "repo_a"), os.path.join(td, "run")

        def w(root, rel, txt, mode="w"):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            open(os.path.join(root, rel), mode).write(txt)
        w(repo_a, "scripts/lib/code_commit.txt", "0" * 40 + "\n")
        w(repo_a, "scripts/lib/helper.py", "X = 1\n")
        w(repo_a, "toolkit/eng/m.py", "def f(p):\n    return {'y': 2 * p['x']}\n")
        w(rd, "models/fn.py", "def f(p):\n    return {'y': p['x']}\n")
        w(rd, "data/inputs/units.csv", "u,a\n1,2\n")
        with mock.patch.object(C, "REPO", repo_a), mock.patch.object(CW, "REPO", repo_a):
            def fp():
                return code_fingerprint(rd, "models/fn.py::f")
            fp0 = fp()
            w(repo_a, "scripts/lib/code_commit.txt", "1" * 40 + "\n")
            ok(fp() == fp0, "RU1 a new launch commit (scripts/lib/code_commit.txt, export-subst) keeps the fingerprint")
            w(repo_a, "toolkit/eng/card.yaml", "version: 2\n")
            fp1 = fp()
            ok(fp1 != fp0, "RU2 a change to repo data (a card) still changes the fingerprint (W0-A: repo code is the "
                           "observed closure, FP_OBSERVED_*)")
            w(rd, "decision_spec.yaml", "spec: 1\n")       # a v2 run: its spec at the run root
            w(rd, "metadata.json", json.dumps({"pipeline": "v2"}))     # and its recorded pipeline (Codex r1 P2-2)
            fp1 = fp()
            for rel in ("models/allocation_robustness.yaml", "models/claims_spec.yaml", "models/claims_ledger.yaml",
                        "decision_spec.yaml", "analysis/point.json"):
                w(rd, rel, "written: 1\n")
            ok(fp() == fp1, "RU3 a v2 run's fixed harness outputs under models/ and the spec keep the fingerprint")
            # data/input_checks.json left this list: it is a sign-off-derived report, skipped on purpose (RS0-RS1)
            for rel in ("models/decision_tests/DT1.yaml", "models/decision_tests/custom.yaml", "data/derived/x.csv"):
                w(rd, rel, "written: 2\n")
                fpx = fp()
                ok(fpx != fp1, f"RU3b (Codex P2-9) no wildcard: {rel} is hashed")
                fp1 = fpx
            for rel in ("models/notes.yaml", "models/decision_tests/helper.py", "models/ext/claims_spec.yaml"):
                w(rd, rel, "x = 1\n")
                fpx = fp()
                ok(fpx != fp1, f"RU4 fail closed: an unknown file under models/ ({rel}) still changes the fingerprint")
                fp1 = fpx
            w(rd, "data/inputs/units.csv", "u,a\n1,3\n")
            ok(fp() != fp1, "RU5 a changed input under data/ still changes the fingerprint")
            out = os.path.join(rd, "models", "batch.jsonl")
            _, s1 = run_map(rd, os.path.join(repo_a, "toolkit", "eng", "m.py") + "::f", [{"x": i} for i in range(3)],
                            out, backend="local", max_workers=1, ledger=False)
            ok(s1.get("computed") == 3, f"RU6 setup: the first batch computes 3 rows ({s1.get('computed')})")
            r = rep(rd)
            ok(r.get("stored") == 3 and r.get("eligible") == 3,
               f"RU7 reuse_report: every stored row would be reused with unchanged code ({r})")
        # each launch extracts its code to a new temporary directory, and a resume tree is a copy of the run
        repo_b, rd_b = os.path.join(td, "repo_b"), os.path.join(td, "run_b")
        shutil.copytree(repo_a, repo_b)
        shutil.copytree(rd, rd_b)
        w(repo_b, "scripts/lib/code_commit.txt", "2" * 40 + "\n")
        with mock.patch.object(C, "REPO", repo_b), mock.patch.object(CW, "REPO", repo_b):
            r = rep(rd_b)
            ok(r.get("stored") == 3 and r.get("eligible") == 3,
               f"RU8 another tree at another commit, the run dir moved: every stored row would be reused ({r})")
            _, s2 = run_map(rd_b, os.path.join(repo_b, "toolkit", "eng", "m.py") + "::f", [{"x": i} for i in range(4)],
                            os.path.join(rd_b, "models", "batch.jsonl"), backend="local", max_workers=1, ledger=False)
            ok(s2.get("computed") == 1 and s2.get("reused") == 3,
               f"RU9 run_map from another tree reuses the stored rows and computes only the new one: "
               f"{s2.get('computed')} computed, {s2.get('reused')} reused")
            w(repo_b, "toolkit/eng/m.py", "# changed\n", "a")
            r = rep(rd_b)
            ok(r.get("stored") == 4 and r.get("eligible") == 0 and all(b.get("why") for b in r.get("batches") or [{}]),
               f"RU10 changed engine code: nothing would be reused, and the report says why ({r})")
        # the CLI a launcher runs locally, from the tree it would ship
        rd_c = os.path.join(td, "run_c")
        w(rd_c, "models/fn.py", "def f(p):\n    return {'y': p['x']}\n")
        run_map(rd_c, "models/fn.py::f", [{"x": 1}, {"x": 2}], os.path.join(rd_c, "models", "b.jsonl"),
                backend="local", max_workers=1, ledger=False)
        cmd = [sys.executable, os.path.abspath(C.__file__), "reuse-check", rd_c]
        cli = _sp.run(cmd, capture_output=True, text=True, timeout=120)
        try:
            got = json.loads(cli.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            got = {}
        ok(cli.returncode == 0 and got.get("stored") == 2 and got.get("eligible") == 2 and "identical inputs" in got.get("note", ""),
           f"RU11 `compute.py reuse-check <run_dir>` prints the counts as JSON: rc={cli.returncode} {got} "
           f"{cli.stderr[-200:]}")
        w(rd_c, "models/fn.py", "# changed\n", "a")
        cli = _sp.run(cmd + ["--expect-all"], capture_output=True, text=True, timeout=120)
        ok(cli.returncode == 1, f"RU12 `reuse-check --expect-all` exits 1 when a stored row would be recomputed "
                                f"(rc={cli.returncode})")
    # Codex P2-9: exact paths only, each one a real harness output; a v1 run hashes everything
    hd = tuple(getattr(C, "FP_HARNESS_OUTPUTS", ()))
    src = "".join(open(os.path.join(REPO, "toolkit", "analysis", f)).read()
                  for f in sorted(os.listdir(os.path.join(REPO, "toolkit", "analysis"))) if f.endswith(".py"))
    ok(hd and not getattr(C, "FP_HARNESS_DIRS", None) and all("*" not in x and x.startswith("models/") and x in src for x in hd),
       f"RU13 the harness-output list is exact paths that toolkit/analysis writes, no wildcard: {hd}")
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        repo, rd = os.path.join(td, "repo"), os.path.join(td, "run")
        for root, rel, txt in ((repo, "scripts/x.py", "X = 1\n"), (repo, "requirements.txt", "starsim==3.0.0\n"),
                               (rd, "models/fn.py", "def f(p):\n    return {'y': open('models/claims_ledger.yaml').read()}\n"),
                               (rd, "models/claims_ledger.yaml", "a\n")):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            open(os.path.join(root, rel), "w").write(txt)
        with mock.patch.object(C, "REPO", repo), mock.patch.object(CW, "REPO", repo):
            fp0 = code_fingerprint(rd, "models/fn.py::f")
            open(os.path.join(rd, "models", "claims_ledger.yaml"), "w").write("b\n")
            fp1 = code_fingerprint(rd, "models/fn.py::f")
            ok(fp1 != fp0, "RU14 (Codex P2-9) a v1 function reading models/claims_ledger.yaml: the file is hashed")
            out = os.path.join(rd, "models", "o.jsonl")
            cwd = os.getcwd()
            os.chdir(rd)
            try:
                run_map(rd, "models/fn.py::f", [{"x": 1}], out, backend="local", max_workers=1, ledger=False)
                open(os.path.join(rd, "models", "claims_ledger.yaml"), "w").write("c\n")
                rows, s3 = run_map(rd, "models/fn.py::f", [{"x": 1}], out, backend="local", max_workers=1, ledger=False)
            finally:
                os.chdir(cwd)
            ok(s3.get("computed") == 1, f"RU14b the v1 batch reading that file is recomputed after it changes: {s3.get('computed')}")
            fp2 = code_fingerprint(rd, "models/fn.py::f")
            open(os.path.join(repo, "requirements.txt"), "w").write("starsim==3.1.0\n")
            ok(code_fingerprint(rd, "models/fn.py::f") != fp2,
               "RU15 (Codex P2-8) a dependency-only change (requirements.txt) changes the fingerprint")
            summ = json.load(open(out + ".summary.json"))
            ok((summ.get("runtime") or {}).get("python") == ".".join(map(str, sys.version_info[:3])),
               f"RU16 (Codex P2-8) a batch records the runtime versions it ran under: {summ.get('runtime')}")


def _engine_batch_reads(names, sources=None) -> list[str]:
    """Which engine-batch job modules mention a sign-off record (by path, file name or the module that owns it).
    `sources`: {label: text}; default, every module the v2 engine-batch functions run on a worker
    (toolkit/engines/**, toolkit/analysis/batch.py and trajectories.py)."""
    if sources is None:
        sources = {}
        roots = [os.path.join(REPO, "toolkit", "engines")]
        files = [os.path.join(REPO, "toolkit", "analysis", f) for f in ("batch.py", "trajectories.py")]
        for r in roots:
            for dp, _, fns in os.walk(r):
                files += [os.path.join(dp, f) for f in fns if f.endswith(".py")]
        sources = {os.path.relpath(f, REPO): open(f, encoding="utf-8").read() for f in sorted(files)}
    hits = []
    for label, text in sources.items():
        for n in names:
            base = os.path.basename(n)
            needles = (n, base, os.path.splitext(base)[0], "spec import geometry", "spec.geometry", "SIGNOFF_FILE")
            hits += [f"{label}: {x}" for x in needles if x in text]
    return hits


def _self_test_checkpoint_restore(ok) -> None:
    """CP0-CP3 (F6 run checkpoints): a run dir restored from an agent-VM checkpoint tarball (cloud/worker_side.py
    build_checkpoint, extracted at another path as `run_cluster.py checkpoint` does) reuses its cached engine rows:
    a finished batch is not recomputed, and an interrupted batch's journal keeps its rows."""
    import importlib.util as _ilu
    import tarfile as _tf
    import tempfile
    sys.path.insert(0, SCRIPTS_DIR)
    import results_store
    spec = _ilu.spec_from_file_location("_ws_cp", os.path.join(REPO, "cloud", "worker_side.py"))
    ws = _ilu.module_from_spec(spec)
    spec.loader.exec_module(ws)
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        rd = os.path.join(td, "vm", "runs", "R_cp")
        os.makedirs(os.path.join(rd, "models"))
        os.makedirs(os.path.join(rd, "data"))
        os.makedirs(os.path.join(rd, "analysis", "batch"))      # where the v2 engine batch keeps its rows
        open(os.path.join(rd, "models", "fn.py"), "w").write("def f(p):\n    return {'y': 3 * p['x']}\n")
        open(os.path.join(rd, "data", "u.csv"), "w").write("u\n1\n")
        inputs = [{"x": i} for i in range(3)]
        done, cut = (os.path.join(rd, "analysis", "batch", x) for x in ("done.jsonl", "cut.jsonl"))
        _, s0 = run_map(rd, "models/fn.py::f", inputs, done, backend="local", max_workers=1, ledger=False)
        ok(s0.get("computed") == 3, f"CP0 setup: the batch computes 3 rows ({s0.get('computed')})")
        # an interrupted batch (the VM stopped mid-batch): its journal holds 2 of 4 rows
        fp = code_fingerprint(rd, "models/fn.py::f", exclude=_out_files(cut))
        keys = input_keys([{"x": i} for i in range(4)])
        with open(cut + PARTIAL_EXT, "w") as f:
            f.write(json.dumps({"_partial": {"fn": "models/fn.py::f", "fn_id": fn_id(rd, "models/fn.py::f"),
                                             "code_fp": fp, "n": 4}}) + "\n")
            obs = {"run:models/fn.py": _file_digest(os.path.join(rd, "models", "fn.py"))}    # W0-A: observed set
            for k in range(2):
                f.write(json.dumps({"i": k, "ok": True, "result": {"y": 3 * k}, "key": keys[k],
                                    "observed_id": _digest(obs), **({"observed": obs} if k == 0 else {})}) + "\n")
        tgz = os.path.join(td, "checkpoint.tar.gz")
        rec = ws.build_checkpoint(os.path.join(td, "vm"), rd, tgz, results_store)
        ok(rec.get("cached_batches") == 2 and rec.get("sha256") == results_store.sha256_file(tgz),
           f"CP1 the checkpoint record counts the cached batches and names the tarball's sha256: {rec}")
        dest = os.path.join(td, "laptop", "R1", "checkpoint")
        with _tf.open(tgz, mode="r:gz") as t:
            t.extractall(dest, filter="data")
        rd2 = os.path.join(dest, "runs", "R_cp")
        bd2 = os.path.join(rd2, "analysis", "batch")
        _, s1 = run_map(rd2, "models/fn.py::f", inputs, os.path.join(bd2, "done.jsonl"), backend="local",
                        max_workers=1, ledger=False)
        ok(s1.get("computed") == 0 and s1.get("reused") == 3,
           f"CP2 a restored run dir reuses every row of its finished batch: {s1.get('computed')} computed, "
           f"{s1.get('reused')} reused")
        _, s2 = run_map(rd2, "models/fn.py::f", [{"x": i} for i in range(4)], os.path.join(bd2, "cut.jsonl"),
                        backend="local", max_workers=1, ledger=False)
        ok(s2.get("computed") == 2 and s2.get("reused") == 2,
           f"CP3 an interrupted batch restored from a checkpoint keeps its journal's rows: {s2.get('computed')} "
           f"computed, {s2.get('reused')} reused")
        # Codex round 1 P1-1: a torn snapshot (results file of one write, summary of another) is not trusted
        d2 = os.path.join(bd2, "done.jsonl")
        summ = json.load(open(d2 + ".summary.json"))
        ok(summ.get("out_sha256") == results_store.sha256_file(d2),
           f"CP5 a batch's summary records the sha256 of its results file: {summ.get('out_sha256')}")
        old_rows = open(d2).read()
        run_map(rd2, "models/fn.py::f", [{"x": i} for i in range(5)], d2, backend="local", max_workers=1, ledger=False)
        open(d2, "w").write(old_rows)                  # results A (3 rows) beside summary B (5 rows)
        import contextlib as _cl
        import io as _io
        with _cl.redirect_stderr(_io.StringIO()) as err:
            _, s5 = run_map(rd2, "models/fn.py::f", inputs, d2, backend="local", max_workers=1, ledger=False)
        ok(s5.get("computed") == 3 and "sha256" in err.getvalue(),
           f"CP5b results A with summary B: the rows are not reused, the batch recomputes, logged: "
           f"{s5.get('computed')} computed {err.getvalue()[-160:]!r}")
        summ = json.load(open(d2 + ".summary.json"))
        summ.pop("out_sha256", None)
        json.dump(summ, open(d2 + ".summary.json", "w"))
        _, s6 = run_map(rd2, "models/fn.py::f", inputs, d2, backend="local", max_workers=1, ledger=False)
        ok(s6.get("computed") == 0, f"CP6 a summary from before the sha256 (no torn snapshot possible) still reuses "
                                    f"when its row count matches: {s6.get('computed')} computed")
        from unittest import mock as _mock
        with _mock.patch.dict(os.environ, {"MA_PIPELINE_RUN": "P9"}):
            run_map(rd2, "models/fn.py::f", [{"x": 9}], os.path.join(bd2, "p.jsonl"), backend="local", max_workers=1,
                    ledger=False)
        with _mock.patch.dict(os.environ):
            os.environ.pop("MA_PIPELINE_RUN", None)
            run_map(rd2, "models/fn.py::f", [{"x": 9}], os.path.join(bd2, "q.jsonl"), backend="local", max_workers=1,
                    ledger=False)
        p9 = json.load(open(os.path.join(bd2, "p.jsonl.summary.json"))).get("pipeline_run")
        q9 = json.load(open(os.path.join(bd2, "q.jsonl.summary.json"))).get("pipeline_run", "absent")
        ok(p9 == "P9" and q9 == "absent",
           f"CP7 (F6) a batch run inside a pipeline step records that pipeline run's id in its summary: {p9} {q9}")
        # the hop through the launch tree: committed with `add -f`, shipped by archive, extracted on the VM
        import shutil as _sh
        import subprocess as _sp
        vc = _sh.which("git")
        if vc:
            tree, ship = os.path.join(td, "tree"), os.path.join(td, "ship")
            os.makedirs(tree)
            _sh.copytree(rd2, os.path.join(tree, "runs", "R_cp"))
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@x"}
            for a in (["init", "-q"], ["add", "-f", "runs"], ["commit", "-q", "-m", "r"]):
                _sp.run([vc, "-C", tree, *a], check=True, env=env, capture_output=True)
            blob = _sp.run([vc, "-C", tree, "archive", "--format=tar.gz", "HEAD"], check=True,
                           capture_output=True).stdout
            with _tf.open(fileobj=_io.BytesIO(blob), mode="r:gz") as t:
                t.extractall(ship, filter="data")
            rd3 = os.path.join(ship, "runs", "R_cp")
            _, s4 = run_map(rd3, "models/fn.py::f", inputs, os.path.join(rd3, "analysis", "batch", "done.jsonl"),
                            backend="local", max_workers=1, ledger=False)
            ok(s4.get("computed") == 0 and s4.get("reused") == 3,
               f"CP4 after the launch-tree hop (committed, archived, extracted) the rows are reused: "
               f"{s4.get('computed')} computed")


def _self_test_w0a(ok) -> None:
    """W0-A: the reuse key is the job's inputs, the engine identity, the run's files, the dependency manifests and the
    declared data set; the repo code is the observed closure. A copy of this scripts/ and a toy toolkit/ form the repo;
    the batch function imports one module (the toy engine)."""
    import shutil
    import tempfile
    from unittest import mock
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        repo, rd = os.path.join(td, "repo"), os.path.join(td, "run")
        shutil.copytree(SCRIPTS_DIR, os.path.join(repo, "scripts"), ignore=shutil.ignore_patterns("__pycache__"))

        def w(rel, txt, root=repo):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            open(os.path.join(root, rel), "w").write(txt)
        w("toolkit/__init__.py", "")
        w("toolkit/eng/__init__.py", "")
        w("toolkit/eng/m.py", "def g(p):\n    return 2 * p['x']\n")
        w("toolkit/eng/card.yaml", "version: 1\n")
        w("toolkit/analysis/__init__.py", "")
        w("toolkit/analysis/figures.py", "def draw():\n    return 0\n")
        w("data/priors.yaml", "a: 1\n")
        w("job.py", "from toolkit.eng import m\n\n\ndef f(p):\n    return {'y': m.g(p)}\n")
        w("kid.py", "import subprocess, sys\n\n\ndef f(p):\n"
                    "    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n    return {'y': p['x']}\n")
        w("data/units.csv", "u,a\n1,2\n", rd)
        out = os.path.join(rd, "analysis", "b.jsonl")
        os.makedirs(os.path.dirname(out))
        ins = [{"x": i} for i in range(3)]
        with mock.patch.object(C, "REPO", repo), mock.patch.object(CW, "REPO", repo):
            def go(fn="job.py::f", inputs=ins, extra=None, o=out):
                kw = {"key_extra": extra} if extra is not None else {}
                return run_map(rd, os.path.join(repo, fn), inputs, o, backend="local", max_workers=1, ledger=False,
                               **kw)[1]
            e1 = {"engine": {"version": "1"}}
            s0 = go(extra=e1)
            kp = s0.get("key_parts") or {}
            obs = kp.get("observed") or {}
            ok(s0.get("computed") == 3 and {"job.py", "toolkit/eng/m.py"} <= set(obs)
               and "toolkit/analysis/figures.py" not in obs and kp.get("observed_sets")
               and obs["toolkit/eng/m.py"] == _file_digest(os.path.join(repo, "toolkit/eng/m.py"))
               and set(FP_BASE_PARTS) <= set(kp) and not kp.get("observed_child"),
               f"FP_OBSERVED_RECORDED the summary records the key parts and the imported repo files with their sha256 "
               f"(the engine module, not figures.py): {sorted(obs)[:12]} {sorted(kp)}")
            w("toolkit/analysis/figures.py", "def draw():\n    return 1\n")
            s1 = go(extra=e1)
            ok(s1.get("reused") == 3 and s1.get("computed") == 0,
               f"FP_OBSERVED_UNRELATED_EDIT an edit to a file no job imported keeps every row ({s1.get('computed')} "
               f"computed, {s1.get('reuse_refused')})")
            w("toolkit/eng/m.py", "def g(p):\n    return 3 * p['x']\n")
            s2 = go(extra=e1)
            ok(s2.get("computed") == 3 and any("observed file changed: toolkit/eng/m.py" in x
                                               for x in s2.get("reuse_refused") or []),
               f"FP_OBSERVED_IMPORTED_EDIT an edit to an imported file recomputes, and says which "
               f"({s2.get('computed')} computed, {s2.get('reuse_refused')})")
            w("toolkit/eng/card.yaml", "version: 2\n")
            s3 = go(extra=e1)
            ok(s3.get("computed") == 3, f"FP_CARD_EDIT an edit to an engine card (data set) recomputes "
                                        f"({s3.get('computed')} computed, {s3.get('reuse_refused')})")
            s4 = go(extra={"engine": {"version": "2"}})
            ok(s4.get("computed") == 3, f"FP_ENGINE_IDENTITY a new engine identity (version) recomputes "
                                        f"({s4.get('computed')} computed)")
            e2 = {"engine": {"version": "2"}}
            s5 = go(inputs=ins[:2] + [{"x": 9}], extra=e2)
            ok(s5.get("computed") == 1 and s5.get("reused") == 2,
               f"FP_INPUT_EDIT a changed input recomputes that row only ({s5.get('computed')} computed)")
            w("data/priors.yaml", "a: 2\n")
            s6 = go(inputs=ins[:2] + [{"x": 9}], extra=e2)
            ok(s6.get("computed") == 3 and any("data_set" in x for x in s6.get("reuse_refused") or []),
               f"FP_DATA_SET_EDIT a changed declared data file recomputes ({s6.get('computed')}, "
               f"{s6.get('reuse_refused')})")
            w("data/units.csv", "u,a\n1,3\n", rd)
            s7 = go(inputs=ins[:2] + [{"x": 9}], extra=e2)
            ok(s7.get("computed") == 3, f"FP_RUN_DATA_EDIT a changed run input file recomputes ({s7.get('computed')})")
            r = reuse_report(rd)
            w("toolkit/eng/m.py", "def g(p):\n    return 4 * p['x']\n")
            r2 = reuse_report(rd)
            b2 = (r2.get("batches") or [{}])[0]
            ok(r.get("eligible") == 3 and r2.get("eligible") == 0 and "toolkit/eng/m.py" in b2.get("why", "")
               and b2.get("engine_checked") == "at run time",
               f"FP_REUSE_CHECK_PARTS reuse-check names the file that differs: {r.get('eligible')} {b2}")
            # an interrupted batch's journal: rows carry their observed sets, and are reused while they hold
            s8 = go(inputs=ins[:2] + [{"x": 9}], extra=e2)
            body = open(out).read()
            summ = json.load(open(out + ".summary.json"))
            kp8 = {k: v for k, v in summ["key_parts"].items() if k in FP_BASE_PARTS}
            os.remove(out + ".summary.json")
            open(out + PARTIAL_EXT, "w").write(json.dumps({"_partial": {"fn": summ["fn"], "fn_id": summ["fn_id"],
                                                                      "code_fp": summ["code_fp"], "n": 3,
                                                                      "key_parts": kp8}}) + "\n" + body)
            s9 = go(inputs=ins[:2] + [{"x": 9}], extra=e2)
            ok(s8.get("computed") == 3 and s9.get("reused") == 3,
               f"FP_PARTIAL_JOURNAL a journal's rows are reused from the observed sets they carry ({s9.get('computed')} "
               f"computed, {s9.get('reuse_refused')})")
            # rows stored before W0-A (no observed record) are recomputed, with the reason
            summ = json.load(open(out + ".summary.json"))
            summ["key_parts"] = kp8
            stripped = "".join(json.dumps({k: v for k, v in json.loads(x).items() if not k.startswith("observed")})
                               + "\n" for x in open(out) if x.strip())
            open(out, "w").write(stripped)
            summ.pop("out_sha256")
            open(out + ".summary.json", "w").write(json.dumps(summ))
            s10 = go(inputs=ins[:2] + [{"x": 9}], extra=e2)
            ok(s10.get("computed") == 3 and s10.get("reuse_refused") == ["no_observed_closure"],
               f"FP_NO_OBSERVED_CLOSURE a row with no observed record is recomputed ({s10.get('computed')}, "
               f"{s10.get('reuse_refused')})")
            # a job that starts a child process: the whole tree joins the key
            kout = os.path.join(rd, "analysis", "k.jsonl")
            k0 = go(fn="kid.py::f", inputs=ins[:2], o=kout)
            k1 = go(fn="kid.py::f", inputs=ins[:2], o=kout)
            w("toolkit/analysis/figures.py", "def draw():\n    return 2\n")
            k2 = go(fn="kid.py::f", inputs=ins[:2], o=kout)
            ok((k0.get("key_parts") or {}).get("observed_child") and k1.get("reused") == 2 and k2.get("computed") == 2,
               f"FP_CHILD_PROCESS a job with a child process is keyed on the whole tree: reused while it holds, "
               f"recomputed after any edit under toolkit/ ({k1.get('reused')}, {k2.get('computed')}, "
               f"{k2.get('reuse_refused')})")
            saved_m = open(os.path.join(repo, "toolkit/eng/m.py")).read()
            os.remove(os.path.join(repo, "toolkit/eng/m.py"))
            b3 = next((b for b in reuse_report(rd)["batches"] if b["out"] == "analysis/b.jsonl"), {})
            ok(b3.get("eligible") == 0 and "missing or unreadable: toolkit/eng/m.py" in b3.get("why", ""),
               f"FP_OBSERVED_MISSING a recorded file that is gone gives no reuse ({b3})")
            w("toolkit/eng/m.py", saved_m)
            # Codex r1 (λ): a repo file read by path (a table in a .md) is recorded too, through the audit hook
            w("toolkit/eng/notes.md", "| k | 2 |\n")
            w("reader.py", "import os\n\n\ndef f(p):\n    t = open(os.path.join(os.path.dirname(__file__), 'toolkit', "
                           "'eng', 'notes.md')).read()\n    return {'y': len(t) * p['x']}\n")
            nout = os.path.join(rd, "analysis", "n.jsonl")
            n0 = go(fn="reader.py::f", inputs=ins[:2], o=nout)
            w("toolkit/eng/notes.md", "| k | 3 |\n")
            n1 = go(fn="reader.py::f", inputs=ins[:2], o=nout)
            ok("toolkit/eng/notes.md" in ((n0.get("key_parts") or {}).get("observed") or {}) and n1.get("computed") == 2,
               f"FP_OBSERVED_READ_BY_PATH a repo file the job opened is recorded, and editing it recomputes "
               f"({n1.get('computed')}, {n1.get('reuse_refused')})")
            # Codex r1 (λ): a run dir under the repo's data/ keeps its key when its own outputs change
            rd_in = os.path.join(repo, "data", "trial_run")
            w("models/fn.py", "def f(p):\n    return p\n", rd_in)
            q0 = code_fingerprint(rd_in, "models/fn.py::f")
            w("analysis/out.jsonl.summary.json", "{}", rd_in)
            ok(q0 == code_fingerprint(rd_in, "models/fn.py::f"),
               "FP_RUN_UNDER_REPO_DATA a run dir under the repo's data/ is not part of the data set")


def _self_test_bundle_data_set(ok) -> None:
    """BD0-BD5 (2026-11-21: make_bundle shipped the run's models/ and data/ and the repo's scripts/ and toolkit/, but
    not repo data/ or .claude/orchestration/, though FP_DATA_SET puts them in every reuse key: a worker reading them
    computed without them while the key hashed the launcher's copies). The bundle ships exactly the files the key's
    data_set part hashes, whatever their size, and nothing under _truth. A toy repo; no network."""
    import tempfile
    from unittest import mock
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        repo, rd = os.path.join(td, "repo"), os.path.join(td, "repo", "runs", "r1")

        def w(rel, txt="x", root=None):
            f = os.path.join(root or repo, rel)
            os.makedirs(os.path.dirname(f), exist_ok=True)
            open(f, "w").write(txt)
        for rel in ("data/pop.csv", "data/sub/costs.yaml", "data/_truth/key.csv",
                    "data/evals/q1.yaml",
                    ".claude/orchestration/priors.yaml",
                    "scripts/compute.py", "scripts/table.json", "scripts/README.md", "toolkit/eng/card.yaml",
                    "toolkit/eng/model.py", "scripts/lib/code_commit.txt", "docs/notes.md"):
            w(rel)
        w("data/big.csv", "y" * (2 * 2**20))
        w("data/_truth_copy/key.csv")
        os.symlink(os.path.join(repo, "data", "_truth", "key.csv"), os.path.join(repo, "data", "alias.csv"))
        os.symlink(os.path.join(repo, "data", "nowhere.csv"), os.path.join(repo, "data", "dangling.csv"))
        w("models/fn.py", "def f(p):\n    return p\n", rd)
        with mock.patch.object(C, "REPO", repo), mock.patch.object(CW, "REPO", repo):
            want = set(_data_set_files(FP_DATA_SET, skip=rd))
            b, info = make_bundle(rd, max_file_mb=1)
        listed = ["/" + n for n in tarfile.open(fileobj=io.BytesIO(b)).getnames()]
        names = set(listed)
        manifest = {"data/pop.csv", "data/sub/costs.yaml", "data/big.csv", "data/dangling.csv",
                    "toolkit/eng/card.yaml"}
        ok(want == manifest, f"BD0 the declared data set is these files, no hidden/excluded ones ({sorted(want ^ manifest)})")
        ok(len(listed) == len(names) and not any("_truth" in n or n.endswith("alias.csv") for n in names)
           and info.get("data_set_unreadable") == ["data/dangling.csv"],
           f"BD5 (Codex r1) no member twice; no _truth* path or link into one; a dangling link is recorded, not fatal "
           f"({info.get('data_set_unreadable')})")
        want.discard("data/dangling.csv")
        data_like = {n for n in names if os.path.relpath(n, repo).split("/")[0] == "data"}
        need = {os.path.join(repo, r) for r in want}
        ok(os.path.join(repo, "data/pop.csv") in need and os.path.join(repo, "toolkit/eng/card.yaml") in need
           and need <= names,
           f"BD1 the bundle ships every file of the declared data set ({sorted(need - names)})")
        ok(data_like == {os.path.join(repo, r) for r in want if r.split("/")[0] == "data"},
           f"BD2 the bundle's repo data/ is exactly the data set ({sorted(data_like)})")
        ok(os.path.join(repo, "data/big.csv") in names and not any("/_truth/" in n for n in names)
           and os.path.join(repo, "docs/notes.md") not in names
           and os.path.join(repo, "data/evals/q1.yaml") not in names,
           f"BD3 a large data-set file ships (the key hashes it), _truth, excludes and undeclared dirs do not ({info})")
        ok(info.get("data_set_files") == len(want) == 4,
           f"BD4 the bundle reports the data-set files it ships ({info})")
        # BE34 (docs/design/agent-built-engines.md §5.1): make_bundle ships ONLY the production subset of engine_build/
        # (admission.json, signoff.json, request.json, signed snapshot, bench/) and NEVER .hidden/, reference/, try/,
        # hidden_reveal.json, or working copies models/ext/engine_*
        sha_ok = "a" * 64
        sha_old = "b" * 64
        w("run_state.json", json.dumps({"signoff": "owner"}), rd)
        w("decision_spec.yaml", "engine: {}\n", rd)
        w("decision_spec.signoff.json", json.dumps({"signed_by": "owner", "sha256": {}}), rd)
        w("engine_build/admission.json", json.dumps({"status": "admitted", "snapshot_sha256": sha_ok}), rd)
        w("engine_build/signoff.json", json.dumps({"by": "owner", "snapshot_sha256": sha_ok}), rd)
        w("engine_build/request.json", json.dumps({"id": "m"}), rd)
        w(f"engine_build/snapshots/{sha_ok}.json", "{}", rd)
        w(f"engine_build/snapshots/{sha_ok}/engine.py", "X=1\n", rd)
        w(f"engine_build/snapshots/{sha_old}.json", "{}", rd)
        w(f"engine_build/snapshots/{sha_old}/engine.py", "X=0\n", rd)
        w("engine_build/bench/results.yaml", "benchmarks: []\n", rd)
        w("engine_build/.hidden/seed.json", '{"seed": "secret"}', rd)
        w("engine_build/hidden_reveal.json", '{"seed": "secret"}', rd)
        w("engine_build/reference/reference.py", "REF=1\n", rd)
        w("engine_build/try/1.json", "{}", rd)
        w("models/ext/engine_m/engine.py", "WORK=1\n", rd)
        with mock.patch.object(C, "REPO", repo), mock.patch.object(CW, "REPO", repo):
            b_be, _ = make_bundle(rd, max_file_mb=1)
            fp_a = code_fingerprint(rd, "models/fn.py::f")
            # Working copy edit must NOT change compute fingerprint
            w("models/ext/engine_m/engine.py", "WORK=2\n", rd)
            fp_work = code_fingerprint(rd, "models/fn.py::f")
            w(f"engine_build/snapshots/{sha_ok}/engine.py", "X=2\n", rd)
            fp_b = code_fingerprint(rd, "models/fn.py::f")
        be_names = {"/" + n for n in tarfile.open(fileobj=io.BytesIO(b_be)).getnames()}
        want_in = {os.path.join(rd, rel) for rel in (
            "run_state.json", "decision_spec.yaml", "decision_spec.signoff.json",
            "engine_build/admission.json", "engine_build/signoff.json", "engine_build/request.json",
            f"engine_build/snapshots/{sha_ok}.json", f"engine_build/snapshots/{sha_ok}/engine.py",
            "engine_build/bench/results.yaml",
        )}
        want_out = {os.path.join(rd, rel) for rel in (
            f"engine_build/snapshots/{sha_old}.json", f"engine_build/snapshots/{sha_old}/engine.py",
            "engine_build/.hidden/seed.json", "engine_build/hidden_reveal.json",
            "engine_build/reference/reference.py", "engine_build/try/1.json",
            "models/ext/engine_m/engine.py",
        )}
        ok(want_in <= be_names and not (want_out & be_names) and fp_a == fp_work and fp_a != fp_b,
           f"BE34 make_bundle ships production subset + signoff files (never .hidden/reference/try/working copy) "
           f"and fingerprint ignores working copy while covering snapshot (missing={sorted(want_in - be_names)}, leaked={sorted(want_out & be_names)})")


def _self_test_signoff_reuse(ok) -> None:
    """RS1-RS5 (F6 Stage 2): a lead-signed preview and the owner-signed final run of one spec differ only in their
    sign-off records; the engine-batch reuse fingerprint ignores exactly those (FP_SIGNOFF_RECORDS) in a v2 run."""
    import tempfile
    from unittest import mock
    recs = tuple(getattr(C, "FP_SIGNOFF_RECORDS", ()))
    ok(recs == ("data/input_checks.json",) and all("*" not in r and "?" not in r for r in recs),
       f"RS0 the sign-off exclusion is an exact, named list (no wildcard): {recs}")
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        repo = os.path.join(td, "repo")

        def w(root, rel, txt):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            open(os.path.join(root, rel), "w").write(txt)
        w(repo, "toolkit/eng/m.py", "def f(p):\n    return p\n")
        runs = {}
        for who in ("lead", "owner"):
            rd = os.path.join(td, f"run_{who}")
            w(rd, "decision_spec.yaml", "spec: 1\n")
            w(rd, "metadata.json", json.dumps({"pipeline": "v2"}))
            # Codex r1 P2-1: geometry.check copies the signer into the report it writes to data/input_checks.json
            w(rd, "data/input_checks.json", json.dumps({"geometry": {"signed_by": who, "findings": []}}))
            w(rd, "models/fn.py", "def f(p):\n    return p\n")
            w(rd, "data/inputs/units.csv", "u,a\n1,2\n")
            w(rd, "data/derived/rates.csv", "u,r\n1,0.5\n")
            w(rd, "decision_spec.signoff.json", json.dumps(
                {"signed_by": who, "geometry": {
                    "signed_by": who, "at": f"2026-11-1{len(who)}T00:00:00Z", "declaration": {"enabled": False},
                    "approvals": {"x": {"approved_by": "owner"}} if who == "owner" else {}}}))
            runs[who] = rd
        with mock.patch.object(C, "REPO", repo), mock.patch.object(CW, "REPO", repo):
            fl, fo = (code_fingerprint(runs[k], "models/fn.py::f") for k in ("lead", "owner"))
            ok(fl == fo, "RS1 lead- and owner-signed sign-off records: same reuse fingerprint")
            ok(code_fingerprint(runs["owner"], "models/fn.py::f", confined=True) == fo,
               "RS1b the confined fingerprint ignores the same record")
            for rel in ("data/derived/rates.csv", "data/inputs/units.csv", "data/derivations_record.json",
                        "data/input_checks.json.extra"):
                w(runs["owner"], rel, "changed\n")
                fx = code_fingerprint(runs["owner"], "models/fn.py::f")
                ok(fx != fo, f"RS2 any other file under data/ ({rel}) still changes the fingerprint")
                fo = fx
            # Codex r1 P2-2: a spec alone is not a v2 run; metadata.json must record pipeline v2
            w(runs["lead"], "metadata.json", json.dumps({"pipeline": "v1"}))
            f0 = code_fingerprint(runs["lead"], "models/fn.py::f")
            w(runs["lead"], "data/input_checks.json", '{"signed_by": "someone else"}')
            ok(code_fingerprint(runs["lead"], "models/fn.py::f") != f0,
               "RS3b a run whose metadata records pipeline v1 hashes the record even with a decision_spec.yaml")
            w(runs["lead"], "metadata.json", json.dumps({"pipeline": "v2"}))
            os.remove(os.path.join(runs["lead"], "decision_spec.yaml"))     # a v1 run hashes everything
            f1 = code_fingerprint(runs["lead"], "models/fn.py::f")
            w(runs["lead"], "data/input_checks.json", '{"signed_by": "owner"}')
            ok(code_fingerprint(runs["lead"], "models/fn.py::f") != f1,
               "RS3 a v1 run (no decision_spec.yaml): its own model may read the record, so it is hashed")
    hits = _engine_batch_reads(recs)
    ok(recs and not hits, f"RS4 no engine-batch job module reads an excluded sign-off record: {hits}")
    ok(bool(_engine_batch_reads(recs, {"planted.py": "json.load(open('data/input_checks.json'))"})),
       "RS5 the RS4 scan finds a planted read of an excluded record (it is not vacuous)")


def _self_test_wall(ok) -> None:
    """SW1-SW5 (Nigeria v2 full run, 2026-11-02): the engine batch step's wall-clock limit, and the partial rows of a
    batch killed in a pipeline step staying reusable after the runner records the failure."""
    import tempfile
    cl = {"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": "8", "MA_COMPUTE_MAX_CORE_HOURS": "150"}
    ok(step_wall_s({}) is None and step_wall_s({"MA_COMPUTE_MAX_CORE_HOURS": "150"}) is None,
       "SW1 without a cluster there is no derived limit (the runner's default applies)")
    ok(step_wall_s(cl) == 20475 and step_wall_s(dict(cl, MA_COMPUTE_PROCS_PER_WORKER="4")) == 2 * 150 * 3600 / 32 + 3600,
       f"SW2 the run cap on the target pool, twice, plus an hour: {step_wall_s(cl)}")
    small = dict(cl, MA_COMPUTE_MAX_CORE_HOURS="20")
    ok(step_wall_s(dict(small, MA_COMPUTE_TARGET_WORKERS="")) == 2 * 20 * 3600 / 8 + 3600 > step_wall_s(small)
       and step_wall_s(dict(cl, MA_COMPUTE_MAX_CORE_HOURS="1e6")) == 24 * 3600,
       "SW3 an unknown pool counts as one worker; the limit has a 24-h ceiling")
    bad = []
    for k, v in (("MA_COMPUTE_MAX_CORE_HOURS", "lots"), ("MA_COMPUTE_MAX_CORE_HOURS", "-1"), ("MA_COMPUTE_MAX_CORE_HOURS", "inf"),
                 ("MA_COMPUTE_TARGET_WORKERS", "8.5"), ("MA_COMPUTE_PROCS_PER_WORKER", "0")):
        try:
            step_wall_s(dict(cl, **{k: v}))
            bad.append(f"{k}={v}")
        except ValueError:
            pass
    ok(not bad, f"SW4 unreadable budget values are refused (fail closed): accepted {bad}")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "fn.py"), "w").write("def f(p):\n    return p\n")
        fp0 = code_fingerprint(d, "models/fn.py::f")
        for rel in ("pipeline.yaml", "pipeline_state.json", "pipeline_report.json"):
            open(os.path.join(d, "models", rel), "w").write('{"steps": {"analyze": {"ok": false}}}\n')
        fp1 = code_fingerprint(d, "models/fn.py::f")
        open(os.path.join(d, "models", "helper.py"), "w").write("X = 1\n")
        ok(fp1 == fp0 and code_fingerprint(d, "models/fn.py::f") != fp1,
           "SW5 the pipeline runner's own definition and records are not part of a batch's fingerprint; model code is")


def _self_test_xhost(ok) -> None:
    """XH1-XH7 (Nigeria v2 full run, 2026-11-02): a caller on another host killed
    without settling (the analyze step's timeout) left a 120 core-hour
    reservation that a resumed run on a new cluster held forever, and its
    progress path pointed into the old checkout."""
    import subprocess as _sp
    import tempfile
    import threading
    now = time.time()
    other = "coiled-dask-old-worker"
    out = [{"n": 32, "share": 5000.0, "started": now - 4000, "cores": 8},
           {"n": 32, "share": 7000.0, "started": now - 3000, "cores": 8}]
    bound = 5000.0 + 7000.0 + 2 * 8 * SHARE_STOP_GRACE_S

    def rec(age, **kw):
        return {"t": now - age, "core_s": 1000.0, "done_tasks": 184, "max_task_core_s": 10.0,
                "remote_unconfirmed": False, "out": out, "call_id": "c-old", **kw}

    def setup(d, record, rel=None, old_root=None, **res):
        """A run dir whose ledger holds one reservation from `other`; the record
        is written at `.compute_progress/abc.json` (None: not written; str: raw)."""
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        pdir = os.path.join(d, PROGRESS_DIR)
        os.makedirs(pdir, exist_ok=True)
        pf = os.path.join(pdir, "abc.json")
        if record is not None:
            open(pf, "w").write(record if isinstance(record, str) else json.dumps(record))
        ref = rel if rel is not None else (os.path.join(old_root, PROGRESS_DIR, "abc.json") if old_root else pf)
        led = {"core_seconds": 0.0, "calls": [], "reservations": {"x": {
            "core_s": 432000.0, "pid": 1941, "pid_start": "proc:24313", "host": other,
            "fn": "/old/toolkit/analysis/batch.py::run_point", "backend": "dask", "n": 256,
            "progress": ref, "started": now - 5000, **res}}}
        return led, pf

    def settle(d, led):
        try:
            return _live_reservations(led, os.path.realpath(d), now=now), None
        except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
            return None, f"{type(e).__name__}: {e}"

    # XH1 a stale cross-host record (heartbeat recorded, not refreshed past the
    # bound) is settled from its record: work done + its chunks' shares (they
    # cannot be cancelled from here), not the reservation; nothing is signalled here.
    with tempfile.TemporaryDirectory() as d:
        bystander = _sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            time.sleep(0.2)
            led, pf = setup(d, rec(PROGRESS_STALE_S + 60, hb_s=PROGRESS_HEARTBEAT_S,
                                   pids=[[bystander.pid, _pid_start(bystander.pid)]], pids_complete=True))
            held, err = settle(d, led)
            c = (led["calls"] or [{}])[0]
            ok(err is None and held == 0.0 and not led["reservations"]
               and abs(led["core_seconds"] - (1000.0 + bound)) < 1e-6 and "another host" in str(c.get("stopped"))
               and "charged work done" in str(c.get("stopped")) and not os.path.exists(pf)
               and bystander.poll() is None,
               f"XH1 a stale cross-host reservation is charged its record ({1000.0 + bound:.0f} core-s), "
               f"not held: held={held} err={err} led={led}")
        finally:
            bystander.kill()
            bystander.wait()
    # XH2 a fresh cross-host record stays held, untouched.
    with tempfile.TemporaryDirectory() as d:
        led, pf = setup(d, rec(30, hb_s=PROGRESS_HEARTBEAT_S))
        held, err = settle(d, led)
        ok(err is None and held == 432000.0 and "x" in led["reservations"] and not led["calls"]
           and os.path.exists(pf), f"XH2 a fresh cross-host reservation stays held: held={held} err={err} {led}")
    # XH3 a record without a heartbeat (written before it existed), in a run dir
    # that has not moved, cannot tell a long task from a dead caller: held
    # within the 24-h bound, settled past it.
    with tempfile.TemporaryDirectory() as d:
        led, pf = setup(d, rec(PROGRESS_STALE_S + 60))
        held, err = settle(d, led)
        ok(err is None and held == 432000.0 and "x" in led["reservations"],
           f"XH3a a legacy record not moved is held within the bound: held={held} err={err}")
        led, pf = setup(d, rec(PROGRESS_STALE_LEGACY_S + 60))
        held, err = settle(d, led)
        ok(err is None and held == 0.0 and abs(led["core_seconds"] - (1000.0 + bound)) < 1e-6,
           f"XH3b a legacy record past the 24-h bound is settled from its record: held={held} err={err} {led}")
    # XH4 the run dir moved (resumed on a new cluster, another checkout): the old
    # absolute path is resolved by its `.compute_progress/<name>` suffix in this run
    # dir; a legacy record that can no longer be refreshed here is settled once stale.
    with tempfile.TemporaryDirectory() as d:
        led, pf = setup(d, rec(PROGRESS_STALE_S + 60), old_root="/tmp/ab/F1/runs/R")
        held, err = settle(d, led)
        c = (led["calls"] or [{}])[0]
        ok(err is None and held == 0.0 and abs(led["core_seconds"] - (1000.0 + bound)) < 1e-6
           and c.get("done_core_s") == 1000.0 and not os.path.exists(pf),
           f"XH4 a moved run dir resolves the old progress path and settles from it: held={held} err={err} {led}")
        led, pf = setup(d, rec(60), old_root="/tmp/ab/F1/runs/R")
        held, err = settle(d, led)
        ok(err is None and held == 432000.0, f"XH4b a moved record still fresh is held: held={held} err={err}")
        # The same suffix resolution for a same-host caller that died.
        led, pf = setup(d, rec(60, hb_s=PROGRESS_HEARTBEAT_S), old_root="/tmp/ab/F1/runs/R",
                        host=os.uname().nodename, pid=2 ** 22 + 7, pid_start=None)
        led["reservations"]["x"]["backend"] = "dask"
        os.environ.pop("MA_COMPUTE_SCHEDULER", None)
        held, err = settle(d, led)
        ok(err is None and held == 0.0 and abs(led["core_seconds"] - (1000.0 + bound)) < 1e-6,
           f"XH4c a dead same-host caller's old absolute path resolves too: held={held} err={err} {led}")
    # XH5 fail closed: an unreadable (or missing) record from another host is held
    # within the 24-h bound from its reservation, then charged in full.
    for content, what in (("{not json", "unreadable"), (None, "missing")):
        with tempfile.TemporaryDirectory() as d:
            led, _ = setup(d, content)
            held, err = settle(d, led)
            ok(err is None and held == 432000.0, f"XH5a {what} record, reserved 1.4 h ago: held: {held} {err}")
            led, _ = setup(d, content, started=now - PROGRESS_STALE_LEGACY_S - 60)
            held, err = settle(d, led)
            c = (led["calls"] or [{}])[0]
            ok(err is None and held == 0.0 and led["core_seconds"] == 432000.0
               and "charged its reservation" in str(c.get("stopped")),
               f"XH5b {what} record past the bound: charged in full: held={held} err={err} {led}")
    # XH6 local work in flight on another host (no share, cannot be stopped from
    # here) charges the reservation.
    with tempfile.TemporaryDirectory() as d:
        led, _ = setup(d, rec(PROGRESS_STALE_S + 60, hb_s=PROGRESS_HEARTBEAT_S,
                              out=[{"n": 1, "started": now - 100, "cores": 1}], pids=[[1, "proc:1"]],
                              pids_complete=True), backend="local")
        held, err = settle(d, led)
        ok(err is None and held == 0.0 and led["core_seconds"] == 432000.0,
           f"XH6 cross-host local work in flight: the reservation is charged: held={held} err={err} {led}")
    # XH7 a running call records its progress path relative to the run dir and
    # refreshes the record's heartbeat while a task runs longer than it.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "nap.py"), "w").write(
            "import time\ndef nap(p):\n    time.sleep(p['s']); return {'ok': 1}\n")
        seen = {}
        with mock.patch.object(C, "PROGRESS_HEARTBEAT_S", 0.15):
            th = threading.Thread(target=lambda: map_fn(d, "models/nap.py::nap", [{"s": 1.2}], backend="local",
                                                        max_workers=1, max_core_hours=0.1))
            th.start()
            end = time.time() + 20
            while time.time() < end and th.is_alive():
                try:
                    r = next(iter(json.load(open(os.path.join(d, LEDGER_REL)))["reservations"].values()))
                    p = json.load(open(os.path.join(d, r["progress"])))
                    seen.setdefault("ref", r["progress"])
                    seen.setdefault("t", []).append(p["t"])
                    seen["hb"] = p.get("hb_s")
                except (OSError, ValueError, KeyError, StopIteration, TypeError):
                    pass
                time.sleep(0.05)
            th.join(30)
        ts = sorted(set(seen.get("t", [])))
        ok(seen.get("ref") == os.path.join(PROGRESS_DIR, os.path.basename(str(seen.get("ref"))))
           and seen.get("hb") == 0.15 and len(ts) >= 4,
           f"XH7 the progress path is relative and the record is refreshed during a 2-s task: {seen.get('ref')} "
           f"hb={seen.get('hb')} {len(ts)} distinct t")


def _self_test_p42(ok) -> None:
    """Phase 42 α: callers that die or are interrupted are charged for work
    done; zombies are dead; detached batches outlive their caller."""
    import signal
    import subprocess as _sp
    import tempfile
    env = {k: v for k, v in os.environ.items() if not k.startswith("MA_COMPUTE_")}
    host = os.uname().nodename

    def ledger(d):
        return json.load(open(os.path.join(d, LEDGER_REL)))

    def wait_rows(path, k, limit=60):
        end = time.time() + limit
        while time.time() < end:
            try:
                if sum(1 for _ in open(path)) - 1 >= k:
                    return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    def gone(pid, limit=30):
        """A SIGKILL is delivered asynchronously: wait until the process is dead
        or defunct (not reaped: the settling call must see a zombie)."""
        end = time.time() + limit
        while _pid_alive(pid) and time.time() < end:
            time.sleep(0.01)

    def wait_ready(run_dir, done, limit=60, pids=True, gate=None, blocked=0):
        """A state the test controls, not timing: the call's progress record
        shows `done` finished tasks, work in flight and (`pids`) its pool pids
        recorded; with a `gate`, also `blocked` tasks waiting on it. Gated tasks
        cannot finish, so nothing more is submitted or recorded: the state is
        frozen while the test kills."""
        import glob as _g
        end, last = time.time() + limit, None
        while time.time() < end:
            held_n = len(_g.glob(gate + ".started.*")) if gate else 0
            for f in _g.glob(os.path.join(run_dir, PROGRESS_DIR, "*.json")):
                try:
                    rec = json.load(open(f))
                except (OSError, ValueError):
                    continue
                last = rec
                seen_pids.extend(rec.get("pids") or [])
                if (int(rec.get("done_tasks") or 0) >= done and rec.get("out") and held_n >= blocked
                        and (rec.get("pids_complete") or not pids)):
                    return True, rec
            time.sleep(0.01)
        return False, last

    GATED = ("import os, time\n"
             "def f(p):\n"
             "    if p.get('gate') and p['k'] >= p.get('free', 0):\n"
             "        open(p['gate'] + '.started.%d' % p['k'], 'w').close()\n"
             "        end = time.time() + 300\n"
             "        while os.path.exists(p['gate']) and time.time() < end:\n"
             "            time.sleep(0.02)\n"
             "    time.sleep(p['s'])\n"
             "    return {'k': p['k']}\n")

    def gated_inputs(path, gate, n, free, s):
        """Inputs k < free run freely; the rest wait while `gate` exists (it is created here)."""
        open(gate, "w").close()
        gates.append(gate)
        open(path, "w").write("".join(json.dumps({"s": s, "k": k, "free": free, "gate": gate}) + "\n"
                                      for k in range(n)))

    # Everything these tests start is stopped on the way out, pass or fail:
    # callers (`procs`), the pool processes their records named, detached jobs.
    procs, seen_pids, gates = [], [], []

    def track(p):
        procs.append(p)
        return p

    def cleanup(run_dir):
        import glob as _g
        for g in gates:                           # gated tasks return
            try:
                os.remove(g)
            except OSError:
                pass
        gates.clear()
        for f in _g.glob(os.path.join(run_dir, "models", "*" + JOB_EXT)):
            try:
                job = json.load(open(f))
                tok, cur = job.get("pid_start"), _pid_start(int(job["pid"]))
                if tok and cur and tok == cur:    # signalled only when identified (pid reuse)
                    _killpg(int(job["pid"]), signal.SIGKILL, direct=True)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        for f in _g.glob(os.path.join(run_dir, PROGRESS_DIR, "*.json")):
            try:
                seen_pids.extend(json.load(open(f)).get("pids") or [])
            except (OSError, ValueError, AttributeError):
                pass
        if seen_pids:
            _stop_orphans({"pids": [list(x) for x in {tuple(x) for x in seen_pids}], "pids_complete": True}, "local")
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL) if os.getpgid(p.pid) == p.pid else p.kill()
                except OSError:
                    pass
            try:
                p.wait(10)
            except _sp.TimeoutExpired:
                pass
        procs.clear()
        seen_pids.clear()

    import contextlib

    @contextlib.contextmanager
    def run_dir_cleaned():
        with tempfile.TemporaryDirectory() as dd:
            try:
                yield dd
            finally:
                cleanup(dd)

    with run_dir_cleaned() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "slow.py"), "w").write(
            "import time\ndef sleep(p):\n    time.sleep(p['s']); return {'k': p['k']}\n")
        inp = os.path.join(d, "models", "in.jsonl")
        open(inp, "w").write("".join(json.dumps({"s": 1.0, "k": k}) + "\n" for k in range(8)))
        out = os.path.join(d, "models", "out.jsonl")
        cmd = [sys.executable, os.path.abspath(C.__file__), "map", d, "--fn", "models/slow.py::sleep", "--inputs", inp,
               "--out", out, "--backend", "local", "--max-workers", "2", "--max-core-hours", "1", "--purpose", "P42"]

        # P42-1 (bug: os.kill(pid, 0) succeeds on a defunct process, so a zombie
        # caller's reservation was held forever): a real zombie child is dead.
        z = _sp.Popen([sys.executable, "-c", "pass"])
        end = time.time() + 30                    # until it has exited (not reaped: a zombie)
        while time.time() < end and (_proc_stat(z.pid) or ["?"])[0] != "Z" and _sp.run(
                ["ps", "-o", "stat=", "-p", str(z.pid)], capture_output=True, text=True).stdout.strip()[:1] != "Z":
            time.sleep(0.02)
        try:
            os.kill(z.pid, 0)
            kill_ok = True
        except OSError:
            kill_ok = False
        ok(kill_ok and not _pid_alive(z.pid) and _pid_alive(os.getpid(), _pid_start(os.getpid())),
           f"P42-1 a zombie is dead though os.kill(pid, 0) succeeds on it ({kill_ok}); this process is alive")
        tok = _pid_start(os.getpid())
        if tok:
            ok(not _pid_alive(os.getpid(), tok + "9"), "P42-1b a reused pid (start token differs) is dead")
        # P42-1c (bug: `ps lstart` depends on TZ and locale, so a caller with another
        # TZ saw a live caller's token differ and settled it): the ps token is the
        # same whatever the caller's TZ.
        toks_tz = set()
        for tz in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
            r = _sp.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import compute; "
                         "print(compute._ps_start(%d))" % (SCRIPTS_DIR, os.getpid())],
                        env=dict(env, TZ=tz, LC_ALL="fr_FR.UTF-8"), capture_output=True, text=True, timeout=60)
            toks_tz.add(r.stdout.strip())
        ok(len(toks_tz) == 1 and next(iter(toks_tz)).startswith("ps:") and next(iter(toks_tz)) == _ps_start(os.getpid()),
           f"P42-1c the ps start token does not depend on the caller's TZ or locale: {toks_tz}")
        # P42-3d (bug: a pid whose identity cannot be verified was signalled): a live
        # process recorded with a different or no start token is never signalled,
        # and the stop is unconfirmed (charged in full).
        bystander = track(_sp.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True))
        end = time.time() + 30
        while _pid_start(bystander.pid) is None and time.time() < end:
            time.sleep(0.02)
        for saved in ("psutil:1.00", "proc:1", "ps:Thu Jan  1 00:00:00 1970", None):
            stopped3d, how3d = _stop_orphans({"pids": [[bystander.pid, saved]], "pids_complete": True}, "local")
            ok(bystander.poll() is None and (saved is not None or not stopped3d),
               f"P42-3d a pid recorded with token {saved!r} is not signalled ({stopped3d}, {how3d})")
        bystander.kill()
        bystander.wait()

        # P42-3e (bug: _stop_chunk sent SIGKILL after the SIGTERM wait without
        # re-checking identity): a chunk ignoring SIGTERM whose identity no longer
        # verifies after the grace period is not SIGKILLed, and is reported unstopped.
        stub = track(_sp.Popen([sys.executable, "-c", "import signal, time; "
                                "signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); "
                                "time.sleep(120)"], start_new_session=True, stdout=_sp.PIPE, text=True))
        stub.stdout.readline()                    # its SIGTERM handler is installed
        tok3e = _pid_start(stub.pid)
        real_start, calls3e = _pid_start, []

        def flaky_start(pid):                     # identity verifies once, then reads as another process
            calls3e.append(pid)
            return real_start(pid) if len(calls3e) == 1 else (tok3e or "x:") + "-reused"
        with mock.patch.object(CW, "_pid_start", flaky_start):
            res3e = _stop_chunk(stub.pid, grace_s=0.3, token=tok3e)
        ok(tok3e and res3e is False and stub.poll() is None,
           f"P42-3e no SIGKILL once identity is lost during the grace period ({res3e}, alive {stub.poll() is None})")
        # P42-3f (bug: _cancel_call skipped unreadable registrations and tokens read
        # another way, reporting the call cancelled): both are unconfirmed (-1), not signalled.
        import uuid as _uuid
        for what in ("other kind", "unreadable"):
            cid = "selftest-p42-" + _uuid.uuid4().hex
            cdir_ = os.path.join(tempfile.gettempdir(), _CALLS_DIR, cid)
            os.makedirs(cdir_)
            reg = os.path.join(cdir_, str(stub.pid))
            cur = _pid_start(stub.pid) or ""
            other = "proc:1" if not cur.startswith("proc:") else "psutil:1.00"
            open(reg, "w").write(f"{stub.pid} {other}")
            if what == "unreadable":
                os.chmod(reg, 0)
            try:
                got3f = _cancel_call(cid)
            finally:
                os.chmod(reg, 0o600)
                import shutil as _sh
                _sh.rmtree(cdir_, ignore_errors=True)
            readable = what != "unreadable" or os.geteuid() != 0     # root reads a 000 file
            ok(not readable or (got3f == -1 and stub.poll() is None),
               f"P42-3f a registration with {what} token is unconfirmed and not signalled: {got3f}")
        stub.kill()
        stub.wait()

        # P42-2 (bug: a dead caller was charged its whole reservation): a zombie
        # caller with a progress record is charged its work, and released.
        pf = os.path.join(d, "rec.json")
        json.dump({"t": time.time(), "core_s": 100.0, "done_tasks": 10, "max_task_core_s": 10.0, "out": []}, open(pf, "w"))
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        json.dump({"core_seconds": 0.0, "calls": [], "reservations": {"z": {
            "core_s": 1440000.0, "pid": z.pid, "host": host, "fn": "f", "progress": pf}}},
            open(os.path.join(d, LEDGER_REL), "w"))
        map_fn(d, "models/slow.py::sleep", [], backend="local")
        led = ledger(d)
        ok(not led["reservations"] and abs(led["core_seconds"] - 100.0) < 1e-6
           and "charged work done" in led["calls"][0]["stopped"] and not os.path.exists(pf),
           f"P42-2 a zombie caller is settled for its 100 core-s of work, not 1,440,000: {led}")
        z.wait()
        # P42-3 (bug: in-flight chunks were charged the allowance): remote chunks
        # whose stop cannot be confirmed (no scheduler) are bounded by their shares,
        # which they enforce; not by observed task times (not an upper bound).
        now = time.time()
        rec = {"t": now - 30, "core_s": 100.0, "done_tasks": 20, "max_task_core_s": 3.0, "call_id": "c1",
               "out": [{"n": 10, "share": 5000.0, "started": now - 50, "cores": 4},
                       {"n": 2, "share": 20.0, "started": now - 40, "cores": 4}]}
        json.dump(rec, open(pf, "w"))
        hist = {"calls": []}
        os.environ.pop("MA_COMPUTE_SCHEDULER", None)
        charge, why, parts = _settle_dead(hist, {"core_s": 9000.0, "progress": pf, "fn": "f", "backend": "dask"},
                                          now=now)
        ok(abs(charge - 5240.0) < 1e-6 and "no scheduler" in why and parts.get("inflight_estimate_core_s") == 36.0,
           f"P42-3 unconfirmed remote chunks are charged their shares, the task-time estimate is only recorded: "
           f"{charge} ({why}) {parts}")
        # P42-3b a dead local caller's leftover pool process is killed, then bounded by its time out.
        orphan = track(_sp.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True))
        time.sleep(0.3)
        rec = {"t": now, "core_s": 100.0, "out": [{"n": 1, "started": time.time() - 10, "cores": 1}],
               "pids": [[orphan.pid, _pid_start(orphan.pid)]], "pids_complete": True}
        json.dump(rec, open(pf, "w"))
        charge, why, _ = _settle_dead(hist, {"core_s": 9000.0, "progress": pf, "fn": "f", "backend": "local"})
        dead_after = orphan.poll() is not None or orphan.wait(5) is not None
        ok(dead_after and 110.0 <= charge <= 120.0 and "pool processes stopped" in why,
           f"P42-3b the orphaned pool process is killed and charged its time out (~10 s): {charge} ({why})")
        # P42-3c killed while the pool was starting processes (pids unknown): local work
        # in flight cannot be stopped or bounded, so the reservation is charged.
        rec["pids_complete"] = False
        json.dump(rec, open(pf, "w"))
        charge, why, _ = _settle_dead(hist, {"core_s": 9000.0, "progress": pf, "fn": "f", "backend": "local"})
        ok(charge == 9000.0 and "not stopped" in why, f"P42-3c pids not recorded: reservation charged: {charge} {why}")
        # P42-4 fail closed (bug: an unreadable record must not hand out a discount).
        for content, what in ((None, "no record"), ("{not json", "unreadable"),
                              (json.dumps({**rec, "remote_unconfirmed": True}), "not confirmed")):
            r = {"core_s": 9000.0, "fn": "f"}
            if content is not None:
                open(pf, "w").write(content)
                r["progress"] = pf
            charge, why, _ = _settle_dead(hist, r, now=now)
            ok(charge == 9000.0 and "charged its reservation" in why,
               f"P42-4 fail closed ({what}): the reservation is charged and the row says why: {charge} {why}")

        # P42-5..7 use gated tasks: tasks k >= free wait on a gate file the test
        # holds, so the batch's state is frozen (nothing finishes, nothing more is
        # submitted or recorded) while the test kills, interrupts or checks it.
        open(os.path.join(d, "models", "gated.py"), "w").write(GATED)

        def gcmd(inputs, *extra):
            return [sys.executable, os.path.abspath(C.__file__), "map", d, "--fn", "models/gated.py::f",
                    "--inputs", inputs, "--out", out, "--backend", "local", "--max-workers", "2",
                    "--max-core-hours", "1", "--purpose", "P42", *extra]

        # P42-5 a caller killed mid-batch (SIGKILL to its group, as a tool timeout
        # does), left a zombie: the next call charges its work, not 3,600 core-s;
        # rerunning the command keeps the rows it had finished.
        os.remove(os.path.join(d, LEDGER_REL))
        g5, in5 = os.path.join(d, "gate5"), os.path.join(d, "models", "in5.jsonl")
        gated_inputs(in5, g5, 8, free=4, s=0.3)
        t0 = time.time()
        p = track(_sp.Popen(gcmd(in5), env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True))
        got, rec5 = wait_ready(d, 4, gate=g5, blocked=2)
        os.killpg(p.pid, signal.SIGKILL)
        el = time.time() - t0
        gone(p.pid)
        map_fn(d, "models/slow.py::sleep", [], backend="local")
        led = ledger(d)
        c = (led["calls"] or [{}])[0]
        ok(got, f"P42-5 setup: 4 tasks done and 2 held at the gate before the kill (last record {rec5})")
        ok(got and not led["reservations"] and "caller died; charged work done" in str(c.get("stopped"))
           and 1.0 <= c["task_core_s"] <= 2 * el + 15 and c.get("purpose") == "P42",
           f"P42-5 a killed caller is charged its work ({c.get('task_core_s')} core-s in {el:.1f}s), not 3,600: {c}")
        p.wait()
        os.remove(g5)
        r = _sp.run(gcmd(in5), env=env, capture_output=True, text=True, timeout=120)
        s = json.load(open(out + ".summary.json")) if os.path.exists(out + ".summary.json") else {}
        ok(r.returncode == 0 and s.get("reused", 0) >= 4 and s.get("n_ok") == 8 and not os.path.exists(out + PARTIAL_EXT),
           f"P42-5b rerunning after the kill keeps the rows finished before it: {s} {r.stderr[-300:]}")

        # P42-5c only the caller is killed: its pool processes live on (held at the
        # gate), and the settling call stops them before bounding their work by time.
        os.remove(os.path.join(d, LEDGER_REL))
        g5c, in5c = os.path.join(d, "gate5c"), os.path.join(d, "models", "in5c.jsonl")
        gated_inputs(in5c, g5c, 8, free=2, s=0.3)
        t0 = time.time()
        p = track(_sp.Popen(gcmd(in5c), env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
        got, rec5c = wait_ready(d, 2, gate=g5c, blocked=2)
        p.kill()
        gone(p.pid)
        map_fn(d, "models/slow.py::sleep", [], backend="local")
        el = time.time() - t0
        c = ledger(d)["calls"][0]
        p.wait()
        ok(got, f"P42-5c setup: 2 tasks done and 2 held at the gate before the kill (last record {rec5c})")
        ok("pool processes stopped" in str(c.get("stopped")) and 0.5 <= c["task_core_s"] <= 2 * el + 15,
           f"P42-5c a killed caller's surviving pool is stopped and its work bounded ({el:.1f}s): {c}")

        # P42-6 an interrupted call (SIGTERM) is charged what ran, settled by the backend.
        os.remove(os.path.join(d, LEDGER_REL))
        g6, in6 = os.path.join(d, "gate6"), os.path.join(d, "models", "in6.jsonl")
        gated_inputs(in6, g6, 8, free=2, s=0.3)
        t0 = time.time()
        p = track(_sp.Popen(gcmd(in6), env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
        got, rec6 = wait_ready(d, 2, gate=g6, blocked=2)
        p.send_signal(signal.SIGTERM)
        p.wait(60)
        el = time.time() - t0
        c = ledger(d)["calls"][-1]
        ok(got, f"P42-6 setup: 2 tasks done and 2 held at the gate before the interrupt (last record {rec6})")
        ok("interrupted: KeyboardInterrupt; charged work done" in c["stopped"] and 0.5 <= c["task_core_s"] <= 2 * el + 15,
           f"P42-6 an interrupted call is charged what ran ({c['task_core_s']} core-s in {el:.1f}s): {c}")



def _self_test_p37(ok) -> None:
    """Phase 37: each batch records what it is for."""
    import subprocess as _sp
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "fn.py"), "w").write("def f(p):\n    return {'y': p['x'] * 2}\n")
        inp = os.path.join(d, "models", "in.jsonl")
        open(inp, "w").write("".join(json.dumps({"x": k}) + "\n" for k in range(3)))
        out = os.path.join(d, "models", "out.jsonl")
        base = [sys.executable, os.path.abspath(C.__file__), "map", d, "--fn", "models/fn.py::f", "--inputs", inp,
                "--out", out, "--backend", "local", "--max-workers", "1"]
        env = {k: v for k, v in os.environ.items() if k != "MA_COMPUTE_SCHEDULER"}
        r = _sp.run(base + ["--purpose", "  "], capture_output=True, text=True, env=env, timeout=120)
        ok(r.returncode == 2 and "--purpose is required" in r.stderr and not os.path.exists(out)
           and not os.path.exists(os.path.join(d, LEDGER_REL)),
           f"P37-1 a batch without a purpose is refused before any work: {r.returncode} {r.stderr[-200:]}")
        r = _sp.run(base + ["--purpose", "D-019, R-021"], capture_output=True, text=True, env=env, timeout=120)
        led = json.load(open(os.path.join(d, LEDGER_REL))) if os.path.exists(os.path.join(d, LEDGER_REL)) else {}
        call = (led.get("calls") or [{}])[-1]
        summ = json.load(open(out + ".summary.json")) if os.path.exists(out + ".summary.json") else {}
        ok(r.returncode == 0 and call.get("purpose") == "D-019, R-021" and call.get("out") == "models/out.jsonl"
           and summ.get("purpose") == "D-019, R-021",
           f"P37-2 the purpose and results file are recorded with the call and its summary: {call} {r.stderr[-300:]}")
        def _boom(*a, **k):
            raise KeyboardInterrupt
        with mock.patch.object(C, "_map_local", _boom):
            try:
                map_fn(d, "models/fn.py::f", [{"x": 1}, {"x": 2}], backend="local", purpose="P-009", out="models/o2.jsonl")
            except KeyboardInterrupt:
                pass
        call = json.load(open(os.path.join(d, LEDGER_REL)))["calls"][-1]
        ok(call.get("purpose") == "P-009" and call.get("n") == 2 and call.get("n_failed") == 2
           and str(call.get("stopped", "")).startswith("interrupted"),
           f"P37-3 an interrupted call keeps its purpose and counts its rows as failed: {call}")


def _self_test_p35(ok) -> None:
    """Phase 35: a broken pool heals, memory sets concurrency, repeated model
    errors stop a batch, reruns complete only missing rows, the gate sees gaps."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "models"))
        # "oom": an input with crash=True kills its process when 2+ run at once
        # (a file per running input stands in for memory pressure); alone it is fine.
        # It waits up to 5 s for a neighbour before deciding it is alone: on a slow
        # runner the pool's processes start one by one, and a fixed 0.3 s check let
        # the input run alone, so no pool broke (CI failures, 2026-10-27).
        open(os.path.join(d, "models", "oom.py"), "w").write(
            "import os, time, glob\n"
            "def f(p):\n"
            "    me = os.path.join(p['dir'], str(os.getpid()))\n"
            "    open(me, 'w').close()\n"
            "    try:\n"
            "        def alive(f):\n"
            "            try:\n                os.kill(int(os.path.basename(f)), 0); return True\n"
            "            except OSError:\n                return False\n"
            "        end = time.time() + (5.0 if p.get('crash') else 0.3)\n"
            "        while time.time() < end:\n"
            "            if p.get('crash') and sum(map(alive, glob.glob(os.path.join(p['dir'], '*')))) > 1:\n"
            "                os._exit(9)\n"
            "            time.sleep(0.02)\n"
            "        return {'x': p['x']}\n"
            "    finally:\n"
            "        os.path.exists(me) and os.remove(me)\n"
            "def always(p):\n    os._exit(9)\n"
            "def raises(p):\n    raise ValueError(f\"no convergence at x={p['x']}\")\n"
            "def some(p):\n    if p['x'] % 10 == 0:\n        raise ValueError('bad draw')\n    return {'x': p['x']}\n"
            "def big(p):\n    b = bytearray(p['mb'] * 2**20)\n    return {'n': len(b)}\n")
        busy = os.path.join(d, "busy"); os.makedirs(busy)
        inputs = [{"x": k, "dir": busy, "crash": k in (3, 11)} for k in range(16)]
        rows, s = map_fn(d, "models/oom.py::f", inputs, backend="local", max_workers=4)
        ok(s["n_failed"] == 0 and [r["result"]["x"] for r in rows] == list(range(16)) and s["breaks"] >= 1
           and s["procs_final"] < 4,
           f"P35-1 a pool broken by one process reruns its in-flight inputs at lower concurrency, none lost: {s}")
        os.environ["MA_COMPUTE_RECYCLE_EVERY"] = "4"
        try:
            rows, s = map_fn(d, "models/oom.py::f", [{"x": k, "dir": busy} for k in range(12)], backend="local",
                             max_workers=2)
        finally:
            os.environ.pop("MA_COMPUTE_RECYCLE_EVERY", None)
        ok(s["n_failed"] == 0 and [r["result"]["x"] for r in rows] == list(range(12)) and s.get("pool_recycles", 0) >= 2
           and recycle_every(8) == 120,
           f"P35-1b v2 lesson 7: the pool is replaced after every chunk of finished inputs, none lost: {s}")
        rows, s = map_fn(d, "models/oom.py::always", [{"x": k} for k in range(3)], backend="local", max_workers=2)
        ok(s["failed_by_kind"] == {"model": 3} and all("alone" in r["error"] for r in rows),
           f"P35-2 an input that kills its process alone fails as a model error: {s}")
        t0 = time.time()
        rows, s = map_fn(d, "models/oom.py::raises", [{"x": k} for k in range(200)], backend="local", max_workers=2)
        ok(s["stopped"] and "same model error" in s["stopped"] and s["failed_by_kind"].get("model", 0) < 40
           and s["failed_by_kind"].get("stopped", 0) > 150 and time.time() - t0 < 30,
           f"P35-3 a repeated model error stops the batch early: {s.get('stopped')} {s['failed_by_kind']}")
        rows, s = map_fn(d, "models/oom.py::some", [{"x": k} for k in range(60)], backend="local", max_workers=2)
        ok(not s["stopped"] and s["n_failed"] == 6, f"P35-4 a minority of failing draws does not trip the breaker: {s}")
        rows, s = map_fn(d, "models/oom.py::big", [{"mb": 200}] * 4, backend="local", max_workers=4)
        ok(s["peak_mb"] and s["peak_mb"] >= 200, f"P35-5 peak memory per process is measured: {s}")
        ok(_mem_slots(5000, 64000, 8) == 8, "P35-6a ample memory keeps every core")
        ok(_mem_slots(9000, 64000, 8) == 5 and _mem_slots(60000, 64000, 8) == 1 and _mem_slots(None, 64000, 8) == 8,
           f"P35-6 concurrency fits the measured peak: {_mem_slots(9000, 64000, 8)}")
        led = json.load(open(os.path.join(d, LEDGER_REL)))
        ok((led.get("mem_profile") or {}).get("models/oom.py::big", {}).get("peak_mb", 0) >= 200,
           f"P35-7 the ledger keeps each function's last peak as the next call's starting point: {led.get('mem_profile')}")
        # resume: a partly failed batch rerun computes only the missing rows
        open(os.path.join(d, "models", "flaky.py"), "w").write(
            "import os\n"
            "def f(p):\n"
            "    if p['x'] in (2, 5) and os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'ctl', 'FAIL')):\n"
            "        raise RuntimeError('transient')\n"
            "    return {'y': p['x'] * 2}\n")
        os.makedirs(os.path.join(d, "ctl"))      # outside models/ and data/: not part of the fingerprint
        FAIL = os.path.join(d, "ctl", "FAIL")
        open(FAIL, "w").close()
        out = os.path.join(d, "models", "flaky_results.jsonl")
        ins = [{"x": k} for k in range(8)]
        rows, s = run_map(d, "models/flaky.py::f", ins, out, backend="local", max_workers=2)
        ok(s["n_failed"] == 2 and s["computed"] == 8, f"P35-8 first run: two rows fail: {s}")
        os.remove(FAIL)
        rows, s = run_map(d, "models/flaky.py::f", ins, out, backend="local", max_workers=2)
        ok(s["n_failed"] == 0 and s["computed"] == 2 and s["reused"] == 6 and [r["result"]["y"] for r in rows]
           == [0, 2, 4, 6, 8, 10, 12, 14], f"P35-10 a rerun computes only the missing rows: {s}")
        open(os.path.join(d, "models", "flaky.py"), "a").write("# changed\n")
        rows, s = run_map(d, "models/flaky.py::f", ins, out, backend="local", max_workers=2)
        ok(s["computed"] == 8, f"P35-12 changed model code: nothing is reused: {s}")
        rows, s = run_map(d, "models/flaky.py::f", ins[::-1] + [{"x": 99}], out, backend="local", max_workers=2)
        ok(s["computed"] == 1 and rows[0]["result"] == {"y": 14} and rows[0]["i"] == 0,
           f"P35-13 reuse is by params, not position: {s}")
        rows, s = run_map(d, "models/flaky.py::f", ins, out, fresh=True, backend="local", max_workers=2)
        ok(s["computed"] == 8, f"P35-14 --fresh recomputes everything: {s}")

        ok(_breaker([{"ok": False, "kind": INFRA, "error": "e"}] * 20) is None,
           "P35-18 machine failures never trip the model-error breaker")
        errs = [{"ok": False, "kind": MODEL, "error": "ValueError: x"}] * 8
        ok(_breaker(errs) and _breaker(errs, prior_ok=300) is None,
           "P35-19 a chunk's breaker counts the batch's successes elsewhere")
        open(os.path.join(d, "models", "draw.py"), "w").write(
            "import random\ndef f(p):\n    return {'u': random.random()}\n")
        dout = os.path.join(d, "models", "draws.jsonl")
        rows, s = run_map(d, "models/draw.py::f", [{}] * 5, dout, backend="local", max_workers=2)
        rows2, s2 = run_map(d, "models/draw.py::f", [{}] * 6, dout, backend="local", max_workers=2)
        us = [r["result"]["u"] for r in rows2]
        ok(len(set(us)) == 6 and s2["computed"] == 1 and us[:5] == [r["result"]["u"] for r in rows],
           f"P35-20 identical params are separate draws, kept and reused one for one: {s2.get('computed')}")
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        dfile = os.path.join(d, "data", "tbl.csv")
        open(dfile, "w").write("a\n1\n"); os.utime(dfile, (1_700_000_000, 1_700_000_000))
        fp1 = code_fingerprint(d, "models/draw.py::f")
        open(dfile, "w").write("a\n2\n"); os.utime(dfile, (1_700_000_000, 1_700_000_000))
        ok(code_fingerprint(d, "models/draw.py::f") != fp1,
           "P35-21 same-size data with the same mtime but new content changes the fingerprint")
        open(os.path.join(d, "models", "run.log"), "w").write("progress\n")
        fp2 = code_fingerprint(d, "models/draw.py::f")
        open(os.path.join(d, "models", "run.log"), "a").write("more\n")
        ok(code_fingerprint(d, "models/draw.py::f") == fp2, "P35-22 logs do not change the fingerprint")

        fpa = code_fingerprint(d, "models/draw.py::f", exclude=(dout,))
        fpb = code_fingerprint(d, "models/draw.py::f")
        ok(fpa != fpb, "P35-26 another call's results file is part of the fingerprint (a model may read it)")
        uo = os.path.join(d, "models", "upstream.out")
        open(uo, "w").write("1\n"); json.dump({"n_failed": 0, "task_core_s": 0}, open(uo + ".summary.json", "w"))
        fpc = code_fingerprint(d, "models/draw.py::f")
        open(uo, "w").write("2\n")
        ok(code_fingerprint(d, "models/draw.py::f") != fpc, "P35-26b a results file counts whatever its extension")
        os.remove(uo); os.remove(uo + ".summary.json")

        open(os.path.join(d, "models", "brk.py"), "w").write(
            "def f(p):\n    if p['x'] >= 100:\n        raise ValueError('persistent')\n    return {'x': p['x']}\n")
        bo = os.path.join(d, "models", "brk.jsonl")
        run_map(d, "models/brk.py::f", [{"x": k} for k in range(100)], bo, backend="local", max_workers=1)
        rows, s = run_map(d, "models/brk.py::f", [{"x": k} for k in range(110)], bo, backend="local", max_workers=1)
        ok(not s.get("stopped") and s["failed_by_kind"] == {"model": 10},
           f"P35-27 rows reused on resume count as successes for the breaker: {s.get('stopped')} {s['failed_by_kind']}")

        rows, s = map_fn(d, "models/oom.py::big", [{"mb": 300}] * 6, backend="local", max_workers=4)
        os.environ["MA_COMPUTE_PEAK_MB"] = "1"
        try:
            import unittest.mock as _m
            with _m.patch.object(CW, "_avail_mb", return_value=900.0):
                rows, s = map_fn(d, "models/oom.py::big", [{"mb": 300}] * 6, backend="local", max_workers=4,
                                 ledger=False)
        finally:
            del os.environ["MA_COMPUTE_PEAK_MB"]
        ok(s["n_failed"] == 0 and s["procs_final"] < 4,
           f"P35-29 concurrency falls to what measured memory allows, the pool shrinks with it: {s}")
        open(os.path.join(d, "models", "var.py"), "w").write(
            "import time\ndef f(p):\n    t = time.time()\n    while time.time() - t < p['s']: pass\n    return {}\n")
        try:
            with _m.patch.object(CW, "_avail_mb", return_value=60.0):   # one ~70 MB process: no hint, 4 at start
                t0 = time.time()
                rows, s = map_fn(d, "models/var.py::f", [{"s": 0.2}] + [{"s": 8.0}] * 3 + [{"s": 8.0}] * 4,
                                 backend="local", max_workers=4, max_core_hours=4 / 3600)
                el = time.time() - t0
        finally:
            pass
        bo2 = os.path.join(d, "data", "baseline.out")
        open(bo2, "w").write("1")
        fpo = code_fingerprint(d, "models/draw.py::f")
        open(bo2, "w").write("2")
        ok(code_fingerprint(d, "models/draw.py::f") != fpo, "P35-34 every file under data/ counts, whatever its extension")
        dres = os.path.join(d, "data", "res.jsonl")
        run_map(d, "models/brk.py::f", [{"x": k} for k in range(5)], dres, backend="local", max_workers=1)
        _r, s35 = run_map(d, "models/brk.py::f", [{"x": k} for k in range(5)], dres, backend="local", max_workers=1)
        ok(s35["computed"] == 0, f"P35-35 a results file under data/ resumes (its own summary is not a dependency): {s35}")
        sd2 = os.path.join(d, "data", "sl")
        for nm, v in (("a", "1"), ("b", "2")):
            os.makedirs(os.path.join(sd2, nm), exist_ok=True)
            open(os.path.join(sd2, nm, "value.csv"), "w").write(v)
        os.symlink("a", os.path.join(sd2, "current"))
        fps = code_fingerprint(d, "models/draw.py::f")
        os.remove(os.path.join(sd2, "current")); os.symlink("b", os.path.join(sd2, "current"))
        ok(code_fingerprint(d, "models/draw.py::f") != fps, "P35-32 retargeting a symlinked data dir changes the fingerprint")
        ok(s["stopped"] == CAP_STOP and el < 5.0,   # without the fix: about 8 s
           f"P35-31 a shrinking pool's allowance stop counts the inputs still running ({el:.1f}s): {s.get('stopped')}")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.parse_args(argv)
    return _self_test()


if __name__ == "__main__":
    sys.exit(main())
