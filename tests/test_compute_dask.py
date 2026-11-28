#!/usr/bin/env python3
"""Dask backend, scheduler resilience, and worker-isolation tests (extracted from scripts/compute.py)."""
from __future__ import annotations

import concurrent.futures as cf
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
    BusyWorkerGuard, CAP_STOP, COMM_RETRIES, ComputeTagPlugin, INFRA, INFRA_RETRIES,
    LEDGER_REL, MODEL, PROGRESS_DIR, SHARE_STOP_GRACE_S,
    _CALLS_DIR, _cap_history, _cap_outlook, _claim, _digest, _file_digest, _gather,
    _map_dask, _new_acc, _observed_check, _remote_task, _revoke_chunk, _settle_dead,
    _stop_orphans, _worker_side, code_fingerprint, make_bundle, map_fn, register_compute_tags,
    _cancel_remote, _gone_workers, SCHED_ATTEMPTS,
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


try:
    from distributed import WorkerPlugin as _WorkerPlugin
except ImportError:
    _WorkerPlugin = object


class OwnTmp(_WorkerPlugin):
    """One temp dir per local worker, as each VM has (the one-chunk lock lives there)."""
    name = "ma-selftest-own-tmp"

    def __init__(self, base: str):
        self.base = base

    def setup(self, worker):
        import tempfile as tf_
        d_ = os.path.join(self.base, worker.address.rsplit(":", 1)[-1])
        os.makedirs(d_, exist_ok=True)
        os.environ["TMPDIR"] = d_
        tf_.tempdir = d_


def _self_test_durable(ok) -> None:
    """CD1-CD9 (v2 autonomy brief, PR 1D item 2): the dask dispatch loop against a fake client (no cluster).
    A dropped scheduler connection ended the batch and charged what was in flight; an unread scheduler view
    with nothing out spun without a bound; a chunk whose future failed before the next view showed its worker
    retired used an infra retry and half the processes; the bundle could be scattered to the run's own VM; the
    bundle carried logs, figures and partial rows; a dead caller on another host was charged each chunk's whole
    share; and a call the ledger's own history showed would not fit its allowance started without a warning."""
    import tempfile
    from unittest import mock
    import distributed
    from distributed.comm.core import CommClosedError
    run_vm, w1, w2 = "tls://10.0.0.9:1", "tls://10.0.0.5:1", "tls://10.0.0.6:1"

    class Fut:
        def __init__(self, key, got=None, exc=None, on_result=None):
            self.key, self.got, self.exc, self.on_result = key, got, exc, on_result
            self.status = "error" if exc else ("pending" if got is None else "finished")

        def done(self):
            return self.status != "pending"

        def result(self, timeout=None):
            if self.on_result:
                self.on_result()
            if self.exc:
                raise self.exc
            return self.got

        def cancel(self):
            if self.status == "pending":
                self.status, self.exc = "cancelled", cf.CancelledError()

    class Fake:
        """distributed.Client for _map_dask: every chunk finishes at once with ok rows, unless a fault is set."""
        status = "running"

        def __init__(self, tagged, chunk_exc=None, retire=True, join=None, **fail):
            self.tagged, self.gone, self.retired = list(tagged), set(), set()
            self.chunk_exc, self.retire, self.join = chunk_exc, retire, join
            self.fail = {"submit": 0, "marker": 0, "wait": 0, "view": False, "scatter": 0, "lose_at": 0, **fail}
            self.submits, self.scatters, self.views, self.waits = [], [], 0, 0
            self.bundles, self.lost, self.stale = [], False, 0
            self.sent, self.obs = [], None      # the bytes of each scatter; an observed set rows carry (CD10)

        def __call__(self, *a, **kw):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def upload_file(self, *a, **kw):
            pass

        register_plugin = upload_file

        def scheduler_info(self):
            return {"workers": {run_vm: {"resources": {}},
                                **{a: {"resources": {"compute": 1}} for a in self.tagged if a not in self.gone}}}

        def run(self, fn, *a, workers=None, **kw):
            return {w: 2 for w in workers or []}

        def scatter(self, data, workers=None, **kw):
            self.scatters.append(None if workers is None else sorted(workers))
            self.sent.append(data)
            if self.fail["scatter"] > 0:
                self.fail["scatter"] -= 1
                raise KeyError(workers[0] if workers else "no worker")      # as dask does for a stale address
            self.bundles.append(Fut(f"bundle{len(self.bundles)}", got=b""))
            return self.bundles[-1]

        def cancel(self, futs):
            for f in futs:
                f.cancel()

        def submit(self, fn, *a, key=None, workers=None, **kw):
            if fn is C._demand_marker:
                if self.fail["marker"] > 0:
                    self.fail["marker"] -= 1
                    raise CommClosedError("marker")
                return Fut(key)
            w, ch = workers[0], a[4]
            self.submits.append({"key": key, "worker": w, "procs": a[6], "bundle": a[0], "failed": False})
            self.stale += bool(self.lost and a[0] is self.bundles[0])       # placed on a bundle no worker holds
            if self.fail["submit"] > 0:
                self.fail["submit"] -= 1
                self.submits[-1]["failed"] = True
                raise CommClosedError("submit")
            exc = self.chunk_exc(sum(not s["failed"] for s in self.submits) - 1) if self.chunk_exc else None
            if exc is not None:                    # its worker leaves between two scheduler views
                return Fut(key, exc=exc, on_result=lambda: (self.gone.add(w), self.retire and self.retired.add(w)))
            got = [{"i": i, "ok": True, "result": {}, "seconds": 1.0, "worker": w} for i, _ in ch]
            if self.obs is not None:               # as _observe does: every row the set's id, the first the set
                for j, r in enumerate(got):
                    r.update({"observed_id": _digest(self.obs), **({"observed": dict(self.obs)} if j == 0 else {})})
            return Fut(key, got={"rows": got, "core_s": float(len(ch)), "summary": {}})

        def run_on_scheduler(self, fn, res, key):
            self.views += 1
            if self.fail["view"]:
                raise OSError("scheduler not answering")
            if self.join and self.views >= self.join[0] and self.join[1] not in self.tagged:
                self.tagged.append(self.join[1])
            if self.views == self.fail["lose_at"]:  # the bundle's only copy leaves; the next scatter fails once
                self.lost, self.fail["scatter"] = True, 1
            return {"workers": {run_vm: [False, 1, True],
                                **{a: [True, 0, True] for a in self.tagged if a not in self.gone}},
                    "held": key is not None and not (self.lost and key == self.bundles[0].key),
                    "retired": sorted(self.retired)}

    cur = {}

    def fake_wait(pending, timeout=None, return_when=None):
        c = cur["c"]
        c.waits += 1
        if c.fail["wait"] > 0:
            c.fail["wait"] -= 1
            raise CommClosedError("wait")
        done = {f for f in pending if f.done()}
        return done, set(pending) - done

    env = {"MA_COMPUTE_SCHEDULER": "tls://sched:8786", "MA_COMPUTE_RESOURCE": "compute",
           "MA_COMPUTE_EXCLUDE_WORKERS": run_vm, "MA_COMPUTE_DISPATCH_S": "0.01", "MA_COMPUTE_INFRA_BACKOFF_S": "0",
           "MA_COMPUTE_COMM_WAIT_S": "0.05", "MA_COMPUTE_SETTLE_S": "0.1", "MA_COMPUTE_TARGET_WORKERS": "2"}

    def run(fake, n=16, limit=20.0, bundle=None, **more):
        """(rows, summary, progress, error) of one _map_dask call against `fake`; `bundle`: make_bundle's stand-in
        (default: a fixed one; CD5e passes the real one)."""
        cur["c"], prog = fake, {}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(distributed, "Client", fake), \
                mock.patch.object(distributed, "wait", fake_wait), mock.patch.dict(os.environ, {**env, **more}), \
                mock.patch.object(C, "make_bundle", bundle or (lambda rd: (b"x", {"sha256": "0"}))):
            try:
                rows, info = _map_dask(d, "models/m.py::f", [{"x": i} for i in range(n)], None,
                                       deadline=time.time() + limit, progress=prog)
                return rows, info, prog, None
            except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
                return [], {}, prog, f"{type(e).__name__}: {e}"

    def all_ok(rows, n=16):
        return sorted(r["i"] for r in rows if r.get("ok")) == list(range(n))

    # CD1 a connection that drops under submit (a chunk, a demand marker) is retried, and a retried chunk is
    # the same task for the scheduler (same key): nothing is placed twice.
    fk = Fake([w1, w2], submit=2, marker=1)
    rows, info, prog, err = run(fk)
    failed = [s["key"] for s in fk.submits if s["failed"]]
    ok(err is None and all_ok(rows) and prog.get("comm_retries", 0) >= 3 and failed and failed[0] is not None
       and len({s["key"] for s in fk.submits[:3]}) == 1,
       f"CD1 a dropped connection under submit is retried with the same task key: err={err} "
       f"ok={sum(bool(r.get('ok')) for r in rows)} retries={prog.get('comm_retries')} keys={failed[:2]}")
    # CD2 the same under wait; a connection that stays down ends the call after a bounded number of tries.
    fk = Fake([w1, w2], wait=2)
    rows, info, prog, err = run(fk)
    ok(err is None and all_ok(rows) and prog.get("comm_retries", 0) >= 2,
       f"CD2a a dropped connection under wait is retried: err={err} retries={prog.get('comm_retries')}")
    fk = Fake([w1, w2], wait=10 ** 6)
    rows, info, prog, err = run(fk)
    ok(err is not None and "CommClosedError" in err and 3 <= fk.waits <= COMM_RETRIES + 3,
       f"CD2b a connection that stays down raises after bounded retries: err={err} waits={fk.waits}")
    # CD3 a scheduler view that cannot be read, with nothing out, is waited for within a bound; the queued
    # inputs then fail as infra (it spun until the deadline, or for good without one).
    fk = Fake([w1, w2], view=True)
    t0 = time.time()
    rows, info, prog, err = run(fk, limit=4.0, MA_COMPUTE_NO_WORKER_WAIT_S="0.3")
    ok(err is None and "scheduler" in str(info.get("stopped")) and time.time() - t0 < 3.0
       and sum(r.get("kind") == INFRA for r in rows) == 16,
       f"CD3 an unreadable scheduler view stops the call within its bound: err={err} "
       f"stopped={info.get('stopped')} after {time.time() - t0:.1f} s")
    # CD4 a chunk whose future fails (KilledWorker, CancelledError) before the next view shows its worker
    # retired on purpose is a scale-down: no infra retry used, the same processes. A worker that crashed
    # (not recorded as retired) still takes the infra path.
    for exc in (distributed.KilledWorker("t", mock.Mock(address=w1), 3), cf.CancelledError("t")):
        name = type(exc).__name__
        fk = Fake([w1, w2], chunk_exc=lambda k, e=exc: e if k == 0 else None)
        rows, info, prog, err = run(fk)
        lostc = [c for c in info.get("chunks", []) if c.get("error")]
        ok(err is None and all_ok(rows) and len(lostc) == 1 and lostc[0].get("scaled_down") is True
           and "backoff_s" not in lostc[0] and all(s["procs"] is None for s in fk.submits),
           f"CD4 {name} on a worker retired on purpose is a scale-down (no infra retry, same processes): "
           f"err={err} chunk={lostc[:1]} procs={sorted({str(s['procs']) for s in fk.submits})}")
    fk = Fake([w1, w2], chunk_exc=lambda k: cf.CancelledError("t") if k == 0 else None, retire=False)
    rows, info, prog, err = run(fk)
    lostc = [c for c in info.get("chunks", []) if c.get("error")]
    ok(err is None and all_ok(rows) and len(lostc) == 1 and not lostc[0].get("scaled_down")
       and lostc[0].get("losses") == 1 and any(s["procs"] == 1 for s in fk.submits),
       f"CD4b a worker that crashed still uses an infra retry with half the processes: err={err} {lostc[:1]}")
    # CD5 the bundle goes to tagged compute workers only: at the start when some are up, else when the first
    # one joins (never to the run's own VM, and no chunk is placed without it).
    fk = Fake([w1, w2])
    rows, info, prog, err = run(fk)
    ok(err is None and all_ok(rows) and fk.scatters == [[w1, w2]],
       f"CD5a the bundle is scattered to the tagged workers: err={err} scatters={fk.scatters}")
    fk = Fake([], join=(5, w1))
    rows, info, prog, err = run(fk)
    ok(err is None and all_ok(rows) and fk.scatters == [[w1]] and fk.views >= 5
       and all(s["bundle"] is not None for s in fk.submits) and not info.get("bundle_rescatters"),
       f"CD5b with no compute worker yet, the bundle waits for the first one: err={err} scatters={fk.scatters}")
    # CD5c (review round 1) a compute worker that left between the worker table and the first scatter (dask
    # raises on its stale address) does not end the batch: the bundle is scattered in the loop instead.
    fk = Fake([w1, w2], scatter=1)
    rows, info, prog, err = run(fk)
    ok(err is None and all_ok(rows) and len(fk.scatters) == 2,
       f"CD5c a failed first scatter is taken up in the loop: err={err} scatters={fk.scatters}")
    # CD5d (review round 1) the bundle's only copy is lost and the scatter that replaces it fails: no chunk is
    # placed on the lost bundle (it would fail without its model running, and use an infra retry).
    fk = Fake([w1, w2], lose_at=2)
    rows, info, prog, err = run(fk)
    ok(err is None and all_ok(rows) and fk.lost and fk.stale == 0 and len(fk.bundles) == 2,
       f"CD5d no chunk is placed on a lost bundle: err={err} stale={fk.stale} scatters={len(fk.scatters)}")
    # CD5e (merge with #328) the bundle scattered at the start and the one scattered again after its only copy was
    # lost are make_bundle's, with the declared data set in them: repo data/ and a data file of toolkit/ that
    # _bundle_skips would leave out by name (a `.log`); the summary's bundle record counts them.
    with tempfile.TemporaryDirectory() as repo:
        repo = os.path.realpath(repo)
        for rel in ("data/pop.csv", "toolkit/eng/bench.log", "toolkit/eng/model.py"):
            os.makedirs(os.path.dirname(os.path.join(repo, rel)), exist_ok=True)
            open(os.path.join(repo, rel), "w").write(rel)
        with mock.patch.object(C, "REPO", repo), mock.patch.object(CW, "REPO", repo):
            fk = Fake([w1, w2], lose_at=2)
            rows, info, prog, err = run(fk, bundle=make_bundle)
        need = {os.path.join(repo, r).lstrip("/") for r in ("data/pop.csv", "toolkit/eng/bench.log")}
        shipped = [set(tarfile.open(fileobj=io.BytesIO(b)).getnames()) for b in fk.sent]
        ok(err is None and all_ok(rows) and len(fk.bundles) == 2 and all(need <= m for m in shipped)
           and (info.get("bundle") or {}).get("data_set_files") == 2,
           f"CD5e every scatter, the replacement included, ships the declared data set: err={err} "
           f"scatters={len(fk.sent)} missing={[sorted(need - m) for m in shipped]} bundle={info.get('bundle')}")
    # CD6 the bundle leaves out the logs and what this module and the runner write beside a batch (partial
    # rows, job records, summaries, the ledger), and nothing else: inputs, earlier results and everything under
    # data/ stay, and so do other files the fingerprint ignores (figures, a step's `.out`: a model that reads
    # one found it on the workers before, review round 2). Nothing the fingerprint covers is left out.
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as repo, \
            mock.patch.object(C, "REPO", repo), mock.patch.object(CW, "REPO", repo):
        d = os.path.realpath(d)
        keep = ["models/m.py", "models/inputs.jsonl", "models/res.jsonl", "data/notes.log", "data/map.png"]
        drop = ["models/run.log", "models/b.jsonl.partial.jsonl", "models/b.jsonl.job.json",
                "models/res.jsonl.summary.json", "models/compute_ledger.json", "models/sub/step.log"]
        unhashed = ["models/fig.png", "models/sub/trace.svg", "models/step.out",        # shipped, not fingerprinted
                    "models/sub/compute_ledger.jsonl"]      # not the run's ledger (review round 3)
        for rel in keep + drop + unhashed:
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            open(os.path.join(d, rel), "w").write("def f(p):\n    return p\n" if rel.endswith(".py") else rel)
        data, binfo = make_bundle(d)
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            got = sorted(os.path.relpath("/" + m, d) for m in t.getnames())
        fp0 = code_fingerprint(d, "models/m.py::f")
        for rel in drop + unhashed:
            os.remove(os.path.join(d, rel))
        fp1 = code_fingerprint(d, "models/m.py::f")
        # res.jsonl is another call's results file only while its summary is beside it: restore that first
        open(os.path.join(d, "models/res.jsonl.summary.json"), "w").write("{}")
        changed = []
        for rel in keep:
            open(os.path.join(d, rel), "a").write("#\n")
            changed.append(code_fingerprint(d, "models/m.py::f") != fp0)
        ok(got == sorted(keep + unhashed) and binfo.get("skipped_outputs") == len(drop) and fp0 == fp1
           and all(changed),
           f"CD6 the bundle leaves out only logs and batch records, none of them fingerprinted: bundled={got} "
           f"skipped={binfo.get('skipped_outputs')} fingerprint kept={fp0 == fp1} changed={changed}")
    # CD6c (merge with #328) a file of the declared data set is never left out by name: the key's data_set part
    # hashes it, so a worker must read the launcher's copy. The run's own log is still left out and counted.
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as repo:
        d, repo = os.path.realpath(d), os.path.realpath(repo)
        for base, rel in ((d, "models/m.py"), (d, "models/run.log"), (repo, "toolkit/eng/bench.log"),
                          (repo, "toolkit/eng/ref.jsonl.summary.json"), (repo, "data/pop.csv")):
            os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
            open(os.path.join(base, rel), "w").write("def f(p):\n    return p\n" if rel.endswith(".py") else rel)
        with mock.patch.object(C, "REPO", repo), mock.patch.object(CW, "REPO", repo):
            data, binfo = make_bundle(d)
            fp0 = code_fingerprint(d, "models/m.py::f")
            open(os.path.join(repo, "toolkit/eng/bench.log"), "a").write("#\n")
            fp1 = code_fingerprint(d, "models/m.py::f")
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            got = set("/" + m for m in t.getnames())
        want = {os.path.join(repo, r) for r in ("toolkit/eng/bench.log", "toolkit/eng/ref.jsonl.summary.json",
                                                "data/pop.csv")}
        ok(want <= got and os.path.join(d, "models/run.log") not in got and binfo.get("skipped_outputs") == 1
           and binfo.get("data_set_files") == 3 and fp0 != fp1,
           f"CD6c a data-set file ships whatever its name, and only the run's log is left out: "
           f"missing={sorted(want - got)} skipped={binfo.get('skipped_outputs')} "
           f"data_set_files={binfo.get('data_set_files')} key follows the data file={fp0 != fp1}")
    # CD7 a dead caller on another host: each chunk it had out is bounded by its cores x the time from its
    # start to the caller's last heartbeat (plus one interval and the stop grace), never above its share.
    now = time.time()
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, PROGRESS_DIR))
        res = {"core_s": 432000.0, "pid": 1941, "host": "another-host", "backend": "dask", "n": 64,
               "progress": os.path.join(PROGRESS_DIR, "abc.json"), "started": now - 900}

        def settle(started, share, **kw):
            p = {"t": now - 400, "core_s": 1000.0, "remote_unconfirmed": False, "call_id": "c",
                 "out": [{"n": 32, "share": share, "started": now - started, "cores": 8}], **kw}
            open(os.path.join(d, PROGRESS_DIR, "abc.json"), "w").write(json.dumps(p))
            return _settle_dead({}, res, now=now, run_dir=d, foreign=True)
        grace = 8 * SHARE_STOP_GRACE_S
        c1, why1, parts1 = settle(500, 50000.0, hb_s=30.0)          # out 100 s before the last heartbeat
        c2, _, _ = settle(500, 300.0, hb_s=30.0)                    # its share is the smaller bound
        c3, _, _ = settle(500, 50000.0)                             # no heartbeat recorded: the share, as before
        ok(abs(c1 - (1000.0 + 8 * (100 + 30) + grace)) < 1e-6 and "last heartbeat" in why1
           and parts1.get("inflight_basis") == "last_heartbeat"
           and abs(c2 - (1000.0 + 300.0 + grace)) < 1e-6 and abs(c3 - (1000.0 + 50000.0 + grace)) < 1e-6,
           f"CD7 a foreign dead caller's chunks are bounded by time to its last heartbeat, at most their share: "
           f"{c1:.0f} {c2:.0f} {c3:.0f} ({why1})")
    # CD8 a call the ledger's history says will not fit its allowance says so before it starts, in its summary
    # and ledger record (a per-call cap stopped part of a batch that had started with no such notice).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "m.py"), "w").write("def f(p):\n    return {'y': p['x']}\n")
        open(os.path.join(d, LEDGER_REL), "w").write(json.dumps({"core_seconds": 0.0, "reservations": {}, "calls": [
            {"fn": "models/m.py::f", "n": 10, "n_failed": 0, "task_core_s": 1000.0}]}))
        rows, summ = map_fn(d, "models/m.py::f", [{"x": i} for i in range(6)], backend="local", max_workers=1,
                            max_core_hours=0.01)
        look = summ.get("cap_outlook") or {}
        rec = json.load(open(os.path.join(d, LEDGER_REL)))["calls"][-1]
        ok(look.get("projected_core_s") == 600.0 and look.get("allowance_core_s") == 36.0 and look.get("over") is True
           and (rec.get("cap_outlook") or {}).get("over") is True and sum(r["ok"] for r in rows) == 6,
           f"CD8 the ledger's history projects the call past its allowance before launch: {look}")
    # CD9 with no history, the call's own first tasks give the projection (noted once, never a stop).
    prog = {"_n": 1200, "_allow": 1000.0, "done_tasks": 100, "core_s": 100.0}
    _cap_outlook(prog)
    first = dict(prog.get("cap_outlook") or {})
    prog.update(done_tasks=200, core_s=400.0)
    _cap_outlook(prog)
    small = {"_n": 1200, "_allow": 1000.0, "done_tasks": 2, "core_s": 100.0}
    _cap_outlook(small)
    # (review round 1) a notice never stops a call: stderr closed under it; and a dead caller's settlement
    # record (no n_failed: its finished tasks are unknown) is not read as history.
    shut, raised = {"_n": 1200, "_allow": 1000.0, "done_tasks": 100, "core_s": 100.0}, None
    with mock.patch.object(sys, "stderr", mock.Mock(write=mock.Mock(side_effect=BrokenPipeError()))):
        try:
            _cap_outlook(shut)
        except Exception as e:  # noqa: BLE001 -- before the change this case fails here
            raised = e
    hist = _cap_history({"calls": [{"fn": "f", "n": 10, "task_core_s": 9000.0, "stopped": "caller died"}]}, "f", 5, 1.0)
    ok(raised is None and (shut.get("cap_outlook") or {}).get("over") is True and hist is None,
       f"CD9b a closed stderr does not stop the call, and a settlement record is not history: {raised!r} {hist}")
    ok(first.get("projected_core_s") == 1200.0 and first.get("over") is True
       and prog["cap_outlook"]["projected_core_s"] == 2400.0 and prog.get("_cap_warned") is True
       and "cap_outlook" not in small,
       f"CD9 measured task time projects the call past its allowance mid-batch: {first} then {prog.get('cap_outlook')}")
    # CD10 (merge with #314) rows that come back through a retried submit or wait, or through a chunk resubmitted
    # after a scale-down, keep the observed closure their chunk recorded: every ok row carries the set's id, the
    # gathered record holds the set, and the closure verifies here (_observed_check), so the rows are reusable.
    obs = {"scripts/compute.py": _file_digest(os.path.join(REPO, "scripts", "compute.py"))}
    cases = {"comm": dict(submit=2, marker=1, wait=1),
             "scale_down": dict(chunk_exc=lambda k: distributed.KilledWorker("t", mock.Mock(address=w1), 3)
                                if k == 0 else None)}
    for name, kw in cases.items():
        fk = Fake([w1, w2], **kw)
        fk.obs = obs
        rows, info, prog, err = run(fk)
        a = _new_acc()
        _gather(a, rows)
        ok(err is None and all_ok(rows) and all(r.get("observed_id") == _digest(obs) for r in rows)
           and a["sets"] == {_digest(obs)} and a["files"] == obs and _observed_check(a, None) is None,
           f"CD10 {name}: rows keep their observed closure through the retried path and it verifies: err={err} "
           f"ids={sorted({str(r.get('observed_id'))[:8] for r in rows})} sets={len(a['sets'])} "
           f"check={_observed_check(a, None) if a['sets'] else 'no set'}")


def _self_test_sched(ok) -> None:
    """Scheduler reads (PR #99's race, carried to compute.py; no network). distributed's
    Client.scheduler_info() returns {} (no "workers" key) whenever the client is not
    connected: before its first identity reply, while it reconnects, after a failed
    reconnect. Every read of the worker table here goes through worker_side's bounded
    reader: an unpopulated identity is waited out, and past the bound the call fails
    with SchedulerInfoUnavailable. It is never read as "no workers"."""
    import tempfile
    from unittest import mock

    class Stop(Exception):
        pass

    class FakeClient:
        def __init__(self, infos, status="running", stop_at_run=True):
            self.infos, self.status, self.ran, self.stop_at_run = list(infos), status, [], stop_at_run

        def __call__(self, *a, **kw):           # distributed.Client(addr, ...) returns this fake
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def scheduler_info(self):
            return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

        def upload_file(self, *a, **kw):
            pass

        def run(self, fn, *a, workers=None, **kw):
            self.ran.append(list(workers or []))
            if self.stop_at_run:
                raise Stop()
            return {w: 1 for w in workers or []}

    table = {"tls://10.0.0.5:1": {"resources": {}}, "tls://10.0.0.6:1": {"resources": {}},
             "tls://10.0.0.9:1": {"resources": {}}}
    two = ["tls://10.0.0.5:1", "tls://10.0.0.6:1"]
    wsm = C._worker_side()
    sleeps = []
    env = {"MA_COMPUTE_SCHEDULER": "tls://sched:8786", "MA_COMPUTE_EXCLUDE_WORKERS": "tls://10.0.0.9:1"}
    with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env), \
            mock.patch.object(time, "sleep", sleeps.append):
        os.environ.pop("MA_COMPUTE_RESOURCE", None)
        for sub in ("models", "data"):
            os.makedirs(os.path.join(d, sub))
        # SR1 dispatch: two empty identities, then the table; the call goes on to the
        # table's workers (less the excluded run worker), after a doubling backoff.
        c = FakeClient([{}, {"id": "s"}, {"workers": table}])
        with mock.patch("distributed.Client", c):
            try:
                _map_dask(d, "models/fn.py::f", [{"x": 1}], 0)
                ok(False, "SR1 dispatch waits out an unpopulated scheduler identity")
            except Stop:
                ok(c.ran == [two] and len(sleeps) == 2 and sleeps[1] == 2 * sleeps[0],
                   f"SR1 dispatch waits out an unpopulated identity, then uses the table's workers: {c.ran} {sleeps}")
            except Exception as e:  # noqa: BLE001
                ok(False, f"SR1 dispatch waits out an unpopulated scheduler identity: {type(e).__name__}: {e}")
        # SR2 an identity that never populates fails the call, naming why; nothing is dispatched.
        sleeps.clear()
        c = FakeClient([{}], status="connecting")
        with mock.patch("distributed.Client", c):
            try:
                _map_dask(d, "models/fn.py::f", [{"x": 1}], 0)
                ok(False, "SR2 an identity that never populates fails the call")
            except wsm.SchedulerInfoUnavailable as e:
                ok(not c.ran and len(sleeps) == SCHED_ATTEMPTS - 1 and "no worker table" in str(e),
                   f"SR2 an identity that never populates fails the call after the bound, naming why: {e} {sleeps}")
            except Exception as e:  # noqa: BLE001
                ok(False, f"SR2 an unpopulated identity raises SchedulerInfoUnavailable, not {type(e).__name__}: {e}")
        # SR3 an empty table read from a connected scheduler is a real "no workers": refused as before.
        c = FakeClient([{"workers": {}}])
        with mock.patch("distributed.Client", c):
            try:
                _map_dask(d, "models/fn.py::f", [{"x": 1}], 0)
                ok(False, "SR3 a connected scheduler with no compute worker is refused")
            except RuntimeError as e:
                ok("no compute workers" in str(e) and not isinstance(e, wsm.SchedulerInfoUnavailable),
                   f"SR3 a connected scheduler with no compute worker is refused as before: {e}")
        # SR4 the remote cancel (settle and orphan paths) reads the table the same way.
        sleeps.clear()
        c = FakeClient([{}, {"workers": table}], stop_at_run=False)
        try:
            got = _cancel_remote(c, "call1", {"tls://10.0.0.9:1"})
        except Exception as e:  # noqa: BLE001
            got = f"{type(e).__name__}: {e}"
        ok(c.ran == [two] and got == {w: 1 for w in two} and len(sleeps) == 1,
           f"SR4 the remote cancel waits out an unpopulated identity, then reaches every worker: {c.ran} {got}")
        c = FakeClient([{}], stop_at_run=False)
        try:
            _cancel_remote(c, "call1", set())
            ok(False, "SR4b a cancel with no worker table raises instead of reaching no worker")
        except wsm.SchedulerInfoUnavailable:
            ok(not c.ran, "SR4b a cancel with no worker table raises instead of reaching no worker")
        except Exception as e:  # noqa: BLE001
            ok(False, f"SR4b a cancel with no worker table raises SchedulerInfoUnavailable, not {type(e).__name__}")
        p = {"call_id": "call1"}
        c = FakeClient([{}], stop_at_run=False)
        with mock.patch("distributed.Client", c):
            done, how = _stop_orphans(p, "dask")
        ok(not done and "SchedulerInfoUnavailable" in how and not c.ran,
           f"SR5 an orphan's cancel with no worker table is unconfirmed, naming why: {how}")
        c = FakeClient([{}, {"workers": table}], stop_at_run=False)
        with mock.patch("distributed.Client", c):
            done, how = _stop_orphans(p, "dask")
        ok(done and c.ran == [two], f"SR5b an orphan's cancel recovers once the table appears: {how} {c.ran}")
        # SR6 the vanished-worker check: an unpopulated identity is not "every worker
        # vanished"; past the bound it raises (the caller then charges every worker as busy).
        try:
            gone = _gone_workers(FakeClient([{}, {"workers": {"tls://10.0.0.5:1": {}}}]), two)
        except Exception as e:  # noqa: BLE001
            gone = f"{type(e).__name__}"
        ok(gone == ["tls://10.0.0.6:1"], f"SR6 the vanished-worker check waits out an unpopulated identity: {gone}")
        try:
            _gone_workers(FakeClient([{}]), two)
            ok(False, "SR6b a vanished-worker check with no table raises")
        except wsm.SchedulerInfoUnavailable:
            ok(True, "SR6b a vanished-worker check with no table raises")
        except Exception as e:  # noqa: BLE001
            ok(False, f"SR6b a vanished-worker check with no table raises SchedulerInfoUnavailable, not {type(e).__name__}")
    ok(C._worker_side() is wsm and os.path.samefile(wsm.__file__, os.path.join(C.REPO, "cloud", "worker_side.py")),
       "SR7 the reader is the repo's cloud/worker_side.py, loaded once by path (a models/worker_side.py cannot shadow it)")


def _self_test_infra(ok) -> None:
    """IR1-IR6 (Nigeria v2 full run, 2026-11-02): at the tail of a 256-point batch the autoscaler
    retired workers that were still running chunks. Each lost chunk was resubmitted once, both
    resubmissions were lost the same way, and 16 rows came back infra; the analysis then failed
    closed. A LocalCluster of 2 workers; every wait is on an event, never a fixed sleep."""
    import tempfile
    import threading
    import dask
    from distributed import Client, LocalCluster


    def until(pred, limit=60.0):
        end = time.time() + limit
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        return None

    def nproc(dask_scheduler=None):
        return {a: len(ws.processing) for a, ws in dask_scheduler.workers.items()}

    def unrunnable(dask_scheduler=None):
        return len(dask_scheduler.unrunnable)

    def pick(target=0, attribute="address", dask_scheduler=None):
        """What the autoscaler would retire to reach `target` (Coiled asks by name)."""
        return dask_scheduler.workers_to_close(target=target, attribute=attribute)

    def names(dask_scheduler=None):
        return {a: ws.name for a, ws in dask_scheduler.workers.items()}

    def guards(dask_scheduler=None):
        return sorted(dask_scheduler.plugins)

    def blocker(path):
        import os as os_
        import time as t_
        while not os_.path.exists(path):
            t_.sleep(0.02)
        return 1

    def calls(rd, tag, k=None):
        return [f for f in os.listdir(rd) if f.startswith(f"call_{tag}_" + ("" if k is None else f"{k}_"))]

    def in_thread(box, *a, **kw):
        def go():
            try:
                box["r"] = map_fn(*a, **kw)
            except Exception as e:  # noqa: BLE001 — reported in the case's message
                box["r"] = ([], {"error": f"{type(e).__name__}: {e}"})
        th = threading.Thread(target=go, daemon=True)     # a hang fails the case, not the whole run
        th.start()
        return th

    keys = ("MA_COMPUTE_SCHEDULER", "MA_COMPUTE_RESOURCE", "MA_COMPUTE_TARGET_WORKERS", "MA_COMPUTE_PROCS_PER_WORKER",
            "MA_COMPUTE_EXCLUDE_WORKERS", "MA_COMPUTE_DISPATCH_S", "MA_COMPUTE_NO_WORKER_WAIT_S",
            "MA_COMPUTE_INFRA_BACKOFF_S")
    saved = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as tmpb, \
            tempfile.TemporaryDirectory() as _iso_td, \
            dask.config.set({"distributed.scheduler.active-memory-manager.start": False}):
        rd = os.path.realpath(rd)
        iso_repo = _self_test_iso_repo(_iso_td)
        os.makedirs(os.path.join(rd, "models"))
        open(os.path.join(rd, "metadata.json"), "w").write("{}")      # live run dir: no bundle staging
        open(os.path.join(rd, "models", "ir.py"), "w").write(
            "import os, time\n"
            "def f(p):\n"
            "    d = p['dir']\n"
            "    open(os.path.join(d, 'call_%s_%d_%d' % (p['tag'], p['k'], os.getpid())), 'w').close()\n"
            "    if p.get('raise'):\n"
            "        raise ValueError('bad input %d' % p['k'])\n"
            "    if p.get('wait'):\n"
            "        open(os.path.join(d, 'started_%s_%d' % (p['tag'], p['k'])), 'w').close()\n"
            "        while not os.path.exists(os.path.join(d, p['wait'])):\n"
            "            time.sleep(0.02)\n"
            "    return p['k']\n")
        with LocalCluster(n_workers=2, threads_per_worker=1, processes=True, dashboard_address=None) as lc, \
                Client(lc.scheduler_address) as cc, _self_test_patched_repo(iso_repo):
            try:
                cc.register_plugin(OwnTmp(tmpb))
                cc.register_plugin(ComputeTagPlugin("compute"))
                os.environ.update(MA_COMPUTE_SCHEDULER=lc.scheduler_address, MA_COMPUTE_RESOURCE="compute",
                                  MA_COMPUTE_TARGET_WORKERS="2", MA_COMPUTE_PROCS_PER_WORKER="1",
                                  MA_COMPUTE_DISPATCH_S="0.2", MA_COMPUTE_INFRA_BACKOFF_S="0.1")
                os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
                w = sorted(cc.run_on_scheduler(nproc))
                # IR1: the hazard. Asked for fewer workers than are busy (Coiled's adapt passes
                # its target to the scheduler), the scheduler picks a busy worker to retire.
                stop_1 = os.path.join(rd, "release_1")
                blk = cc.submit(blocker, stop_1, workers=[w[0]], allow_other_workers=False, pure=False)
                until(lambda: cc.run_on_scheduler(nproc).get(w[0]), 30)
                before = cc.run_on_scheduler(pick)
                # IR2: model errors are never retried: each input runs once and its row is a
                # model failure; no chunk is recorded as lost. The call also installs the guard.
                open(stop_1, "w").close()
                blk.result(timeout=60)
                b2 = {}
                in_thread(b2, rd, "models/ir.py::f", [{"k": k, "dir": rd, "tag": "m", "raise": True}
                                                      for k in range(2)], ledger=False).join(120)
                r2, s2 = b2.get("r", ([], {}))
                ok(len(r2) == 2 and all(not r["ok"] and r.get("kind") == MODEL for r in r2)
                   and len(calls(rd, "m")) == 2 and not [c_ for c_ in s2.get("chunks", []) if c_.get("error")],
                   f"IR2 a model error is not retried: each input ran once ({sorted(calls(rd, 'm'))}): {r2} {s2}")
                stop_1b = os.path.join(rd, "release_1b")
                blk = cc.submit(blocker, stop_1b, workers=[w[0]], allow_other_workers=False, pure=False)
                until(lambda: cc.run_on_scheduler(nproc).get(w[0]), 30)
                after = cc.run_on_scheduler(pick)
                nm = cc.run_on_scheduler(names)
                after_n = cc.run_on_scheduler(pick, 0, "name")
                guard = BusyWorkerGuard.name
                ok(w[0] in before and guard in cc.run_on_scheduler(guards)
                   and w[0] not in after and w[1] in after and nm[w[0]] not in after_n and nm[w[1]] in after_n,
                   f"IR1 a busy worker is never chosen to close once a compute call has run (without the guard "
                   f"{before}, with it {after}, by name {after_n}; busy {w[0]})")
                open(stop_1b, "w").close()
                blk.result(timeout=60)
                if guard in cc.run_on_scheduler(guards):
                    cc.unregister_scheduler_plugin(guard)
                C.register_compute_tags(cc)
                ok(guard in cc.run_on_scheduler(guards),
                   f"IR1b the launcher's registration installs the guard too: {cc.run_on_scheduler(guards)}")
                # IR3: a worker retired under a running chunk (what adapt's scale-down does when it
                # picks one): the chunk is resubmitted and finishes on the remaining worker.
                b3 = {}
                th3 = in_thread(b3, rd, "models/ir.py::f", [{"k": k, "dir": rd, "tag": "r", "wait": "go_r"}
                                                            for k in range(2)], ledger=False)
                until(lambda: os.path.exists(os.path.join(rd, "started_r_0"))
                      and os.path.exists(os.path.join(rd, "started_r_1")), 60)
                pr = cc.run_on_scheduler(nproc)
                victim = next((a for a in sorted(pr) if pr[a]), None)
                cc.retire_workers([victim], close_workers=True, remove=True)
                until(lambda: victim not in cc.run_on_scheduler(nproc), 30)
                open(os.path.join(rd, "go_r"), "w").close()
                th3.join(120)
                r3, s3 = b3.get("r", ([], {}))
                lost3 = [c_ for c_ in s3.get("chunks", []) if c_.get("error")]
                ok(len(r3) == 2 and all(r["ok"] for r in r3) and all(r.get("worker") != victim for r in r3)
                   and len(lost3) == 1 and "left the cluster" in lost3[0]["error"] and lost3[0]["worker"] == victim
                   and s3.get("estimated_core_s", 0) > 0 and not s3.get("failed_by_kind"),
                   f"IR3 a chunk whose worker was retired under it is charged and finished on the remaining "
                   f"worker within the call: {r3} {s3}")
                # 2026-11-12 (P6): a scale-down (the scheduler records the removal as expected) says
                # nothing about memory or a failing VM: the chunk goes back with the processes it had,
                # without a backoff, and does not use one of its infra retries.
                ok(len(lost3) == 1 and lost3[0].get("scaled_down") is True and lost3[0].get("losses") == 0
                   and lost3[0].get("retried_with_procs") == lost3[0].get("procs") and not lost3[0].get("backoff_s")
                   and lost3[0].get("requeued_attempt") == lost3[0].get("attempt") == 0,
                   f"IR3b a chunk lost to a scale-down keeps its processes, its infra retries and its attempt (so "
                   f"a first out-of-memory death after it still reruns with half the processes: Codex review of "
                   f"#204): {lost3}")
                # IR4/IR5: the retries are bounded per input (INFRA_RETRIES = 2): a chunk lost twice
                # still finishes; one lost three times is an infra row, after exactly three attempts.
                real_task = C._remote_task

                def lossy(n_loss, tag):
                    def _remote_task_stub(bundle, sha, run_dir, fn_spec, chunk, *a, **k):
                        import os as os_
                        marks = [f_ for f_ in os_.listdir(run_dir) if f_.startswith(f"loss_{tag}_")]
                        if len(marks) < n_loss:
                            open(os_.path.join(run_dir, f"loss_{tag}_{len(marks)}"), "w").close()
                            return {"rows": [{"i": i, "ok": False, "kind": "infra",
                                              "error": "chunk failed: its worker left the cluster"} for i, _ in chunk],
                                    "core_s": 0.0, "unconfirmed": True}
                        return real_task(bundle, sha, run_dir, fn_spec, chunk, *a, **k)
                    return _remote_task_stub
                outs = {}
                try:
                    for n_loss in (2, 3, 4):
                        C._remote_task = lossy(n_loss, f"b{n_loss}")
                        outs[n_loss] = map_fn(rd, "models/ir.py::f", [{"k": 0, "dir": rd, "tag": f"b{n_loss}"}],
                                              ledger=False)
                finally:
                    C._remote_task = real_task
                (r4, s4), (r5, s5), (r7, s7) = (outs.get(k, ([], {})) for k in (2, 3, 4))
                lost4 = [c_ for c_ in s4.get("chunks", []) if c_.get("error")]
                lost5 = [c_ for c_ in s5.get("chunks", []) if c_.get("error")]
                lost7 = [c_ for c_ in s7.get("chunks", []) if c_.get("error")]
                marks7 = [f_ for f_ in os.listdir(rd) if f_.startswith("loss_b4_")]
                ok(len(r4) == 1 and r4[0]["ok"] and len(lost4) == 2 and len(calls(rd, "b2")) == 1,
                   f"IR4 a chunk lost twice is resubmitted twice and finishes: {r4} {lost4}")
                ok(INFRA_RETRIES == 3 and len(r5) == 1 and r5[0]["ok"] and len(lost5) == 3 and len(calls(rd, "b3")) == 1,
                   f"IR5 (F6 preview, 2026-11-11) three resubmissions by default: a chunk lost three times finishes: "
                   f"{INFRA_RETRIES} {r5} {lost5}")
                ok(len(r7) == 1 and not r7[0]["ok"] and r7[0].get("kind") == INFRA and len(lost7) == 4
                   and len(marks7) == 4 and not calls(rd, "b4") and s7.get("failed_by_kind") == {INFRA: 1},
                   f"IR5b a chunk lost four times is an infra row after four attempts, no more: {r7} {lost7} {marks7}")
                bo = [c_.get("backoff_s") for c_ in lost5]
                ok(bo[:3] == [0.1, 0.2, 0.4],
                   f"IR7 each resubmission waits a doubling backoff (MA_COMPUTE_INFRA_BACKOFF_S) and records it: {bo}")
                # IR6: no compute worker at all (none carries the resource): the call waits for the
                # pool within MA_COMPUTE_NO_WORKER_WAIT_S, then gives up with infra rows and no demand left.
                os.environ.update(MA_COMPUTE_RESOURCE="ma-selftest-none", MA_COMPUTE_NO_WORKER_WAIT_S="1")
                b6 = {}
                t6 = time.time()
                th6 = in_thread(b6, rd, "models/ir.py::f", [{"k": k, "dir": rd, "tag": "n"} for k in range(3)],
                                ledger=False)
                th6.join(60)
                r6, s6 = b6.get("r", ([], {}))
                ok(not th6.is_alive() and len(r6) == 3 and all(r.get("kind") == INFRA for r in r6)
                   and str(s6.get("stopped") or "").startswith("no compute worker")
                   and time.time() - t6 >= 1.0 and cc.run_on_scheduler(unrunnable) == 0,
                   f"IR6 with no compute worker the call waits its bound, then returns infra rows and withdraws "
                   f"its demand: alive={th6.is_alive()} {r6} {s6.get('stopped')}")
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


def _self_test_dask_core(ok) -> None:
    from distributed import LocalCluster
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as _iso_td:
        iso_repo = _self_test_iso_repo(_iso_td)
        os.makedirs(os.path.join(d, "models"))
        os.makedirs(os.path.join(d, "data"))
        open(os.path.join(d, "data", "scale.txt"), "w").write("3\n")
        open(os.path.join(d, "models", "helper.py"), "w").write("def scale():\n    return float(open('data/scale.txt').read())\n")
        open(os.path.join(d, "models", "fn.py"), "w").write(
            "from helper import scale\ndef f(p):\n    if p.get('boom'):\n        raise ValueError('bad draw')\n    return {'y': p['x'] * scale()}\n")
        open(os.path.join(d, "models", "slow.py"), "w").write(
            "import os, time\ndef sleep(p):\n    time.sleep(p['s']); return {'ok': 1}\n")
        inputs = [{"x": k} for k in range(40)] + [{"x": 0, "boom": True}]
        with LocalCluster(n_workers=2, threads_per_worker=1, processes=True, dashboard_address=None) as lc, \
                _self_test_patched_repo(iso_repo):
            os.environ["MA_COMPUTE_SCHEDULER"] = lc.scheduler_address
            try:
                rows, s = map_fn(d, "models/fn.py::f", inputs)
                ok(s["backend"] == "dask" and rows[7]["result"] == {"y": 21.0} and s["n_failed"] == 1
                   and [r["i"] for r in rows] == list(range(41)), f"C8 dask backend: {s}")
                w0 = list(lc.scheduler_info["workers"])[0]
                os.environ["MA_COMPUTE_EXCLUDE_WORKERS"] = w0
                rows, s = map_fn(d, "models/fn.py::f", inputs[:8])
                used = {r.get("worker") for r in rows}
                ok(s["workers"] == 1 and all(r["ok"] for r in rows) and w0 not in used and None not in used,
                   f"C9 no task ran on the excluded run worker: {used}")
                os.environ["MA_COMPUTE_PROCS_PER_WORKER"] = "1"
                rows, s = map_fn(d, "models/slow.py::sleep", [{"s": 1.5}] * 4, max_core_hours=2.0 / 3600)
                # Not "exactly 1 done": model load time is charged too (Phase 29), so
                # timing varies; the property is that the batch stops at the cap.
                ok(s["n_ok"] < 4 and s["task_core_s"] <= 2.6 and s["stopped"] == CAP_STOP,
                   f"C27 later chunks get only what is left of the allowance: {s}")
                # C35 (Phase 30): a timed-out batch is charged what actually ran, not the pool.
                os.environ["MA_COMPUTE_PROCS_PER_WORKER"] = "1"
                os.environ["MA_COMPUTE_EXCLUDE_WORKERS"] = w0
                t0c = time.time()
                rows3, s3 = map_fn(d, "models/slow.py::sleep", [{"s": 1.0}] * 40, timeout_s=2.5, ledger=False)
                wall3 = time.time() - t0c
                ok(s3["stopped"] == "timed out" and s3["estimated_core_s"] == 0
                   and s3["task_core_s"] <= 1 * wall3 + 1.0 and sum(r["ok"] for r in rows3) < 40,
                   f"C35 stopped chunks report their real use, queued ones nothing (no whole-pool estimate): {s3} wall {wall3:.1f}")
                os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
                # C34 (Phase 30): autoscaling pool. Untagged run worker; a worker
                # added mid-batch must get work through the resource tag.
                os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
                from distributed import Client as _C2
                import threading as _th2
                with _C2(lc.scheduler_address) as _cc:
                    _cc.register_plugin(ComputeTagPlugin("compute", exclude=[w0]))
                os.environ.update(MA_COMPUTE_RESOURCE="compute", MA_COMPUTE_TARGET_WORKERS="3",
                                  MA_COMPUTE_PROCS_PER_WORKER="1")
                open(os.path.join(d, "models", "slowf.py"), "w").write(
                    "import time\ndef f(p):\n    time.sleep(0.2)\n    return p['x']\n")
                box = {}
                th = _th2.Thread(target=lambda: box.update(r=map_fn(d, "models/slowf.py::f",
                                                                     [{"x": k} for k in range(30)], ledger=False)))
                th.start()
                time.sleep(0.5)
                lc.scale(3)                       # a new worker joins mid-batch
                th.join(120)
                rows2, s2 = box.get("r", ([], {}))
                used2 = {r.get("worker") for r in rows2}
                new_ws = set(lc.scheduler_info["workers"]) - {w0} - set(list(lc.scheduler_info["workers"])[:2])
                ok(len(rows2) == 30 and all(r["ok"] for r in rows2) and w0 not in used2,
                   f"C34 tagged pool: every task done, none on the untagged run worker: {used2} {s2}")
                ok(len(used2) >= 2 and s2.get("pool_target") == 3,
                   f"C34 a worker that joined mid-batch got work (pool target 3): {used2} {s2.get('pool_target')}")
                for k in ("MA_COMPUTE_RESOURCE", "MA_COMPUTE_TARGET_WORKERS"):
                    os.environ.pop(k, None)
            finally:
                os.environ.pop("MA_COMPUTE_SCHEDULER", None)
                os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
                os.environ.pop("MA_COMPUTE_PROCS_PER_WORKER", None)


def _self_test_p42_dask(ok) -> None:
    import signal
    from distributed import LocalCluster
    env = {k: v for k, v in os.environ.items() if not k.startswith("MA_COMPUTE_")}

    def ledger(d):
        return json.load(open(os.path.join(d, LEDGER_REL)))

    def wait_ready(run_dir, done, limit=60):
        import glob as _g
        end, last = time.time() + limit, None
        while time.time() < end:
            for f in _g.glob(os.path.join(run_dir, PROGRESS_DIR, "*.json")):
                try:
                    rec = json.load(open(f))
                except (OSError, ValueError):
                    continue
                last = rec
                if int(rec.get("done_tasks") or 0) >= done and rec.get("out"):
                    return True, rec
            time.sleep(0.01)
        return False, last

    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as _iso_td, \
            LocalCluster(n_workers=1, threads_per_worker=1, processes=True, dashboard_address=None) as lc:
        iso_scripts = os.path.join(_self_test_iso_repo(_iso_td), "scripts")
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "slow.py"), "w").write(
            "import time\ndef sleep(p):\n    time.sleep(p['s']); return {'k': p['k']}\n")
        code = ("import json, os, signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); "
                "sys.path.insert(0, %r); import compute; "
                "from distributed import Client; c = Client(os.environ['MA_COMPUTE_SCHEDULER']); "
                "compute.register_compute_tags(c, exclude=[]); c.close(); "
                "compute.map_fn(%r, 'models/slow.py::sleep', [{'s': 1.0, 'k': k} for k in range(80)], "
                "max_core_hours=1.0, purpose='P42-8')") % (iso_scripts, d)
        envd = dict(env, MA_COMPUTE_SCHEDULER=lc.scheduler_address, MA_COMPUTE_RESOURCE="compute",
                    MA_COMPUTE_TARGET_WORKERS="6", MA_COMPUTE_PROCS_PER_WORKER="1", MA_COMPUTE_SETTLE_S="30")
        t0 = time.time()
        p = _sp.Popen([sys.executable, "-c", code], env=envd, stdout=_sp.DEVNULL, stderr=_sp.PIPE, text=True)
        try:
            got8, rec8 = wait_ready(d, 1, limit=120)
            p.send_signal(signal.SIGINT)
            err = p.communicate(timeout=120)[1]
            el = time.time() - t0
            try:
                c = ledger(d)["calls"][-1]
            except (OSError, ValueError, IndexError):
                c = {}
            ok(got8, f"P42-8 setup: a dask chunk finished before the interrupt (last record {rec8}) {err[-300:]}")
            ok("charged work done" in str(c.get("stopped")) and 0 < c.get("task_core_s", 0) <= el + 5,
               f"P42-8 an interrupted dask call on 1 of 6 target workers is charged ~1 worker's time "
               f"({c.get('task_core_s')} core-s in {el:.1f}s; before: about 6 x elapsed): {c} {err[-300:]}")
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()


def _self_test_p45d(ok) -> None:
    """Phase 45 δ (A16): the chunk loop places chunks on free workers itself.

    E7 attempt 8 is emulated on a LocalCluster: an untagged run worker holds a
    task the scheduler expects to take 68 hours (set through
    `default-task-durations`, as the measured average of attempt 8's 34-hour
    pipeline task did), so the cluster's mean occupancy dwarfs any compute
    worker's queue and work stealing never moves chunks. Every wait is on an
    event (a file the model writes, a scheduler state), never a fixed sleep."""
    import tempfile
    import dask
    from distributed import Client, LocalCluster


    def state(dask_scheduler=None):
        s_ = dask_scheduler
        return {"unrunnable": sum(1 for ts in s_.unrunnable if ts.prefix.name == "remote_task"),
                "proc": {a: sum(1 for ts in ws.processing if ts.prefix.name == "remote_task")
                         for a, ws in s_.workers.items()},
                "target": s_.adaptive_target("20s"), "occ": s_.total_occupancy,
                "bundle": sorted({ws.address for ts in s_.tasks.values() if ts.prefix.name == "bytes"
                                  for ws in (ts.who_has or ())}),
                "running": {a: getattr(ws.status, "name", "") == "running" for a, ws in s_.workers.items()},
                "tagged": sorted(a for a, ws in s_.workers.items() if "compute" in (ws.resources or {})),
                "nproc": {a: len(ws.processing) for a, ws in s_.workers.items()},
                "bundle_keys": sorted(k for k, ts in s_.tasks.items() if ts.prefix.name == "bytes" and ts.who_has)}

    def idle_thread(dask_worker=None):
        return not dask_worker.state.executing

    def pause(dask_worker=None):
        """The worker stops taking new tasks, as a VM being retired does
        (closing_gracefully: unlike `paused`, the memory monitor does not undo it)."""
        from distributed.core import Status
        dask_worker.status = Status.closing_gracefully

    def unpause(dask_worker=None):
        from distributed.core import Status
        dask_worker.status = Status.running

    def die():                                     # after replying: the worker process exits
        import os as os_
        import threading as th_
        th_.Timer(0.2, os_._exit, (1,)).start()

    def kill(cc_, addrs):
        """Stop the workers taking work first, so no retry lands on a worker
        about to die; then end their processes."""
        cc_.run(pause, workers=sorted(addrs))
        until(lambda: not any(cc_.run_on_scheduler(state)["running"].get(a) for a in addrs), 30)
        cc_.run(die, workers=sorted(addrs))
        until(lambda: not set(addrs) & set(cc_.run_on_scheduler(state)["proc"]), 30)

    def until(pred, limit=60.0):
        end = time.time() + limit
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        return None

    keys = ("MA_COMPUTE_SCHEDULER", "MA_COMPUTE_RESOURCE", "MA_COMPUTE_TARGET_WORKERS", "MA_COMPUTE_PROCS_PER_WORKER",
            "MA_COMPUTE_EXCLUDE_WORKERS", "MA_COMPUTE_DISPATCH_S")
    saved = {k: os.environ.get(k) for k in keys}
    import threading
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as tmpb, \
            tempfile.TemporaryDirectory() as _iso_td, \
            dask.config.set({"distributed.scheduler.default-task-durations": {"hog": "68h"},
                             "distributed.scheduler.active-memory-manager.start": False}):
        rd = os.path.realpath(rd)
        iso_repo = _self_test_iso_repo(_iso_td)
        os.makedirs(os.path.join(rd, "models"))
        # A live run directory (metadata.json): chunks run against its files, so
        # the local "VMs" never stage one bundle into one shared path at once.
        open(os.path.join(rd, "metadata.json"), "w").write("{}")
        open(os.path.join(rd, "models", "a16.py"), "w").write(
            "import os, time\n"
            "def f(p):\n"
            "    d = p['dir']\n"
            "    if p.get('wait'):\n"
            "        open(os.path.join(d, 'started_%s_%d' % (p['tag'], p['k'])), 'w').close()\n"
            "        while not os.path.exists(os.path.join(d, p['wait'])):\n"
            "            time.sleep(0.02)\n"
            "    t0 = time.time()\n"
            "    time.sleep(p.get('s', 0))\n"
            "    open(os.path.join(d, 'span_%s_%d' % (p['tag'], p['k'])), 'w').write('%f %f' % (t0, time.time()))\n"
            "    return p['k']\n")
        with LocalCluster(n_workers=1, threads_per_worker=1, processes=True, dashboard_address=None) as lc, \
                Client(lc.scheduler_address) as cc, _self_test_patched_repo(iso_repo):
            w_run = list(lc.scheduler_info["workers"])[0]
            try:
                hog = cc.submit(time.sleep, 600, key=f"hog-{os.urandom(8).hex()}", workers=[w_run], pure=False)
                cc.register_plugin(OwnTmp(tmpb))
                cc.register_plugin(ComputeTagPlugin("compute", exclude=[w_run]))
                lc.scale(2)
                cc.wait_for_workers(2, timeout=60)
                os.environ.update(MA_COMPUTE_SCHEDULER=lc.scheduler_address, MA_COMPUTE_RESOURCE="compute",
                                  MA_COMPUTE_TARGET_WORKERS="4", MA_COMPUTE_PROCS_PER_WORKER="1",
                                  MA_COMPUTE_EXCLUDE_WORKERS=w_run, MA_COMPUTE_DISPATCH_S="0.2")
                # 8 chunks of one input each, 1.5 s each; all wait for `go`.
                s_task = 1.5
                ins = [{"k": k, "dir": rd, "tag": "a", "wait": "go", "s": s_task} for k in range(8)]
                box, box2 = {}, {}

                def call(b, ins_):
                    try:
                        b["r"] = map_fn(rd, "models/a16.py::f", ins_, ledger=False)
                    except Exception as e:  # noqa: BLE001 — reported in the case's message
                        b["r"] = ([], {"error": f"{type(e).__name__}: {e}"})
                th = threading.Thread(target=call, args=(box, ins))
                th.start()
                started0 = until(lambda: os.path.exists(os.path.join(rd, "started_a_0")))
                # One chunk runs on the one compute worker; the other 3 slots of the
                # target show as unrunnable demand under the chunks' own prefix.
                st = until(lambda: (lambda v: v if v["unrunnable"] == 3 else None)(cc.run_on_scheduler(state)), 30)
                ok(started0 and st and st["target"] >= 4 and st["occ"] > 1e5
                   and sum(st["proc"].values()) == 1,
                   f"P45D-1 one chunk runs, 3 demand markers are visible to the autoscaler (target >= 4) under "
                   f"inflated occupancy: {st}")
                lc.scale(5)                        # 3 tagged workers join mid-batch
                cc.wait_for_workers(5, timeout=60)
                st2 = until(lambda: (lambda v: v if sum(1 for n in v["proc"].values() if n == 1) == 4
                                     and v["unrunnable"] == 1 else None)(cc.run_on_scheduler(state)), 30)
                ok(st2 is not None and not st2["proc"].get(w_run),
                   f"P45D-2 each joining worker got a chunk at once and the demand markers were withdrawn down to "
                   f"one: {st2}")
                # 2026-11-12 (P6): with every slot of the pool busy and chunks still queued, no marker
                # was left, so the autoscaler saw no waiting work and shrank the pool as chunks finished,
                # retiring the worker a queued chunk was about to be pinned to. One marker stays while
                # chunks wait, so the target holds the pool.
                ok(st2 is not None and st2["target"] >= 5,
                   f"P45D-2b with the pool full and chunks queued, one demand marker holds the autoscaler's "
                   f"target at the pool (>= 5 workers): {st2}")
                # Codex review of #204: the held marker counts as more work, so the target asks for a VM
                # beyond the pool; once the pool is past its target the marker goes, so the pool never
                # grows a worker per joined VM.
                lc.scale(6)                        # a fifth compute worker joins: past the target of 4
                cc.wait_for_workers(6, timeout=60)
                st2c = until(lambda: (lambda v: v if v["unrunnable"] == 0 else None)(cc.run_on_scheduler(state)), 30)
                ok(st2c is not None,
                   f"P45D-2c with more compute workers than the pool's target, no marker stays: "
                   f"{cc.run_on_scheduler(state)}")
                open(os.path.join(rd, "go"), "w").close()
                th.join(120)
                rows, s = box.get("r", ([], {}))
                used = {r.get("worker") for r in rows}
                spans = []
                for k in range(8):
                    try:
                        spans.append(tuple(map(float, open(os.path.join(rd, f"span_a_{k}")).read().split())))
                    except (OSError, ValueError):
                        pass
                wall = (max(e for _, e in spans) - min(b for b, _ in spans)) if len(spans) == 8 else None
                # Peak concurrency from the tasks' own spans (structure, not wall clock: a loaded runner stretched
                # the wall time past 0.6 x serial while the batch was parallel). With one process per worker,
                # overlapping spans ran on different workers at the same time.
                ev = sorted([(b, 1) for b, _ in spans] + [(e, -1) for _, e in spans], key=lambda x: (x[0], x[1]))
                peak, cur = 0, 0
                for _, step in ev:
                    cur += step
                    peak = max(peak, cur)
                serial = 8 * s_task
                ok(len(rows) == 8 and all(r["ok"] for r in rows) and [r["result"] for r in rows] == list(range(8))
                   and w_run not in used and len(used) >= 4 and s.get("workers") == len(used)
                   and s.get("workers_seen") == 5,
                   f"P45D-3 every chunk done on {len(used)} compute workers, none on the run worker, "
                   f"and the ledger's worker count is the workers used: {used} {s}")
                ok(len(spans) == 8 and peak >= 2,
                   f"P45D-4 the batch ran in parallel: {peak} of its 8 tasks ran at once on separate workers "
                   f"(wall {wall} s against {serial} s serial, information only)")
                left = cc.run_on_scheduler(state)
                ok(left["unrunnable"] == 0 and not any(left["proc"].values()),
                   f"P45D-5 no demand marker or chunk outlives the call: {left}")
                # A worker that dies under its pinned chunk: the chunk cannot run
                # anywhere else, so the loop sees the worker gone, charges the chunk,
                # and resubmits it once to another worker. The compute workers that
                # hold the scattered bundle die too: when no copy is left (dask cannot
                # recompute scattered data and cancels the chunk), it is scattered again.
                ins2 = [{"k": 0, "dir": rd, "tag": "b", "wait": "go2", "s": 0.1}]
                th2 = threading.Thread(target=call, args=(box2, ins2))
                th2.start()
                until(lambda: os.path.exists(os.path.join(rd, "started_b_0")))
                victim = until(lambda: next((a for a, n in cc.run_on_scheduler(state)["proc"].items() if n), None), 30)
                holders = set(cc.run_on_scheduler(state)["bundle"])
                dying = ({victim} | holders) - {w_run}
                kill(cc, dying)
                open(os.path.join(rd, "go2"), "w").close()
                th2.join(120)
                rows2, s2 = box2.get("r", ([], {}))
                lost = [ch for ch in s2.get("chunks", []) if ch.get("error")]
                ok(len(rows2) == 1 and rows2[0]["ok"] and rows2[0].get("worker") not in dying
                   and len(lost) == 1 and lost[0]["attempt"] == 0 and lost[0]["worker"] == victim
                   and s2.get("estimated_core_s", 0) > 0,
                   f"P45D-6 a chunk whose worker left is charged and resubmitted once elsewhere: {rows2} {s2}")
                ok(s2.get("bundle_rescatters") == (0 if w_run in holders else 1),
                   f"P45D-7 a bundle whose every copy was lost is scattered again (once): holders {holders}, "
                   f"run worker {w_run}, {s2.get('bundle_rescatters')}")
                # The same with the bundle kept: a second chunk runs on another worker
                # first, which then holds a copy (the active memory manager is off
                # here, so the copy stays); only the blocked chunk's worker dies, and
                # the loop finds it gone from the scheduler.
                ins3 = [{"k": 0, "dir": rd, "tag": "c", "wait": "go3", "s": 0.1},
                        {"k": 1, "dir": rd, "tag": "c", "s": 0.1}]
                box3 = {}
                th3 = threading.Thread(target=call, args=(box3, ins3))
                th3.start()
                until(lambda: os.path.exists(os.path.join(rd, "started_c_0"))
                      and os.path.exists(os.path.join(rd, "span_c_1")))
                victim3 = until(lambda: (lambda pr: next(a for a, n in pr.items() if n) if sum(pr.values()) == 1
                                         and len(box3) == 0 else None)(cc.run_on_scheduler(state)["proc"]), 30)
                kept = until(lambda: (lambda b: b if set(b) - {victim3} else None)(cc.run_on_scheduler(state)["bundle"]),
                             30)
                ok(kept is not None, f"P45D-8 setup: the bundle has a copy off the victim: {kept}")
                kill(cc, {victim3})
                open(os.path.join(rd, "go3"), "w").close()
                th3.join(120)
                rows3, s3 = box3.get("r", ([], {}))
                lost3 = [ch for ch in s3.get("chunks", []) if ch.get("error")]
                ok(len(rows3) == 2 and all(r["ok"] for r in rows3) and rows3[0].get("worker") != victim3
                   and len(lost3) == 1
                   and "left the cluster" in lost3[0]["error"] and lost3[0]["worker"] == victim3
                   and s3.get("bundle_rescatters") == 0,
                   f"P45D-8 a pinned chunk whose worker left the scheduler is resubmitted elsewhere: {rows3} {s3}")
                # P45D-9/10: a chunk pinned to a worker it cannot start on moves, and
                # only while it has not started. Every compute worker's one-chunk lock
                # is held by the test, so the chunk is submitted but cannot claim its
                # start; then the others are released and its worker either gets a
                # second task (another call picked it from the same snapshot) or stops
                # taking work (closing gracefully).
                import fcntl

                def hold_locks():
                    fs = {}
                    for a in cc.run_on_scheduler(state)["tagged"]:
                        d_ = os.path.join(tmpb, a.rsplit(":", 1)[-1])
                        os.makedirs(d_, exist_ok=True)
                        fh = open(os.path.join(d_, "ma_compute.lock"), "w")
                        fcntl.flock(fh, fcntl.LOCK_EX)
                        fs[a] = fh
                    return fs

                def blocker(path):
                    import os as os_
                    import time as t_
                    while not os_.path.exists(path):
                        t_.sleep(0.02)
                    return 1
                for tag, how in (("d", "collision"), ("e", "closing")):
                    until(lambda: len(cc.run_on_scheduler(state)["tagged"]) >= 3, 60)
                    locks = hold_locks()
                    bx = {}
                    thx = threading.Thread(target=call, args=(bx, [{"k": 0, "dir": rd, "tag": tag, "s": 0.1}]))
                    thx.start()
                    w_x = until(lambda: next((a for a, n in cc.run_on_scheduler(state)["proc"].items() if n), None), 30)
                    for a, fh in locks.items():
                        if a != w_x:
                            fh.close()             # every other worker can run it now
                    foreign = None
                    if how == "collision":
                        foreign = cc.submit(blocker, os.path.join(rd, f"release_{tag}"), workers=[w_x],
                                            allow_other_workers=False, pure=False)
                    else:
                        cc.run(pause, workers=[w_x])
                    thx.join(120)
                    locks[w_x].close()
                    open(os.path.join(rd, f"release_{tag}"), "w").close()
                    if foreign is not None:
                        foreign.result(timeout=60)
                    else:
                        cc.run(unpause, workers=[w_x])
                    # the revoked submission has returned (its thread is free)
                    until(lambda: cc.run(idle_thread, workers=[w_x]).get(w_x), 30)
                    rx, sx = bx.get("r", ([], {}))
                    ran = [f_ for f_ in os.listdir(rd) if f_.startswith(f"span_{tag}_")]
                    ok(not thx.is_alive() and len(rx) == 1 and rx[0]["ok"] and rx[0].get("worker") != w_x
                       and sx.get("moved", 0) >= 1 and len(ran) == 1 and not [c_ for c_ in sx.get("chunks", [])
                                                                             if c_.get("error")],
                       f"P45D-{9 if how == 'collision' else 10} an unstarted chunk on a worker it cannot start on "
                       f"({how}) moves to another, and runs once: {rx} {sx}")
                    # The revoked first submission, released now, does no work.
                    ok(len([f_ for f_ in os.listdir(rd) if f_.startswith(f"span_{tag}_")]) == 1,
                       f"P45D-{9 if how == 'collision' else 10}b the revoked submission did no work")
                # P45D-12/13: the start claim records who holds it. A revoke asked
                # again gives the same answer; a replayed submission finds its own
                # earlier start claim and reports unconfirmed work, never "revoked".
                import uuid as uuid_
                cid = uuid_.uuid4().hex
                cdir = os.path.join(tempfile.gettempdir(), _CALLS_DIR, cid)
                r1, r2 = _revoke_chunk(cid, "x"), _revoke_chunk(cid, "x")
                c1 = _claim(cdir, "x", "start")
                c2 = _claim(cdir, "y", "start")
                r3, r4 = _revoke_chunk(cid, "y"), _revoke_chunk(cid, "y")
                ok(r1 and r2 and c1 == (False, "revoke") and c2 == (True, "start") and not r3 and not r4,
                   f"P45D-12 claim owner recorded; a repeated revoke keeps its answer: {r1, r2, c1, c2, r3, r4}")
                b13, i13 = make_bundle(rd)
                one = [(0, {"k": 0, "dir": rd, "tag": "g", "s": 0})]
                _claim(cdir, "z", "start")              # an earlier attempt of submission z
                g1 = C._remote_task(b13, i13["sha256"], rd, "models/a16.py::f", one, procs=1, call_id=cid, token="z")
                g2 = C._remote_task(b13, i13["sha256"], rd, "models/a16.py::f", one, procs=1, call_id=cid, token="x")
                ok(g1.get("unconfirmed") and not g1.get("revoked") and g2.get("revoked") and not g2.get("unconfirmed")
                   and not os.path.exists(os.path.join(rd, "span_g_0")),
                   f"P45D-13 a replay reports unconfirmed work, only a revoke claim reports revoked: {g1} {g2}")
                # P45D-14..16: the client side, with a stand-in chunk task whose first
                # call returns (14) "revoked" for a chunk the client never revoked (the
                # revoke's reply was lost), (15) "unconfirmed", (16) no rows at all.
                real_task = C._remote_task

                def first_then_real(kind, flag):
                    def _remote_task_stub(bundle, sha, run_dir, fn_spec, chunk, *a, **k):
                        import os as os_
                        try:
                            os_.close(os_.open(flag, os_.O_CREAT | os_.O_EXCL | os_.O_WRONLY))
                        except FileExistsError:
                            return real_task(bundle, sha, run_dir, fn_spec, chunk, *a, **k)
                        if kind == "norows":
                            return {"rows": [], "core_s": 0.0}
                        return {"rows": [{"i": i, "ok": False, "kind": "infra", "error": kind} for i, _ in chunk],
                                "core_s": 0.0, kind: True}
                    return _remote_task_stub
                outs = {}
                try:
                    for kind in ("revoked", "unconfirmed", "norows"):
                        C._remote_task = first_then_real(kind, os.path.join(rd, f"stub_{kind}"))
                        outs[kind] = map_fn(rd, "models/a16.py::f",
                                            [{"k": k, "dir": rd, "tag": f"h{kind}", "s": 0} for k in range(2)],
                                            ledger=False)
                finally:
                    C._remote_task = real_task
                rv, sv = outs.get("revoked", ([], {}))
                ok(len(rv) == 2 and all(r["ok"] for r in rv) and sv.get("moved") == 1 and not sv.get("invariant_violation"),
                   f"P45D-14 a revoked result with no replacement is placed again, not dropped: {rv} {sv}")
                ru, su = outs.get("unconfirmed", ([], {}))
                lost_u = [c_ for c_ in su.get("chunks", []) if "unconfirmed" in str(c_.get("error"))]
                ok(len(ru) == 2 and all(r["ok"] for r in ru) and len(lost_u) == 1 and lost_u[0]["attempt"] == 0
                   and su.get("estimated_core_s", 0) > 0,
                   f"P45D-15 unconfirmed work is a lost chunk: charged, resubmitted once: {ru} {su}")
                rn, sn = outs.get("norows", ([], {}))
                ok(len(rn) == 2 and sum(r["ok"] for r in rn) == 1 and sn.get("invariant_violation")
                   and [r for r in rn if not r["ok"]][0]["kind"] == INFRA,
                   f"P45D-16 an input that returned no row fails loudly: {rn} {sn}")
                hog.cancel()
                # P45D-11: a loss that uses the rest of the allowance ends the call at once
                # (no worker free, so before this the loop slept with demand shown).
                # Every compute worker but one runs a blocker; the chunk's worker then
                # dies, and no new compute worker can join (the tag plugin is removed).
                until(lambda: len(cc.run_on_scheduler(state)["tagged"]) >= 3, 60)
                tagged = cc.run_on_scheduler(state)["tagged"]
                stop_b = os.path.join(rd, "release_f")
                blockers = [cc.submit(blocker, stop_b, workers=[a], allow_other_workers=False, pure=False,
                                      resources={"compute": 1}) for a in tagged[1:]]
                until(lambda: all(cc.run_on_scheduler(state)["nproc"].get(a) for a in tagged[1:]), 30)
                os.environ["MA_COMPUTE_PROCS_PER_WORKER"] = "64"   # a loss is charged 64 cores x its time
                bf = {}
                thf = threading.Thread(target=lambda: bf.update(r=map_fn(
                    rd, "models/a16.py::f", [{"k": 0, "dir": rd, "tag": "f", "wait": "go_f", "s": 0.1}],
                    ledger=False, max_core_hours=30.0 / 3600)))
                thf.start()
                until(lambda: os.path.exists(os.path.join(rd, "started_f_0")))
                # The run worker fetches a copy of the bundle, so the loss below is seen
                # as the worker leaving (the path that skipped the allowance check),
                # not as a cancelled chunk.
                from distributed import Future
                keep = [cc.submit(len, Future(k, client=cc), workers=[w_run], pure=False)
                        for k in cc.run_on_scheduler(state)["bundle_keys"]]
                until(lambda: w_run in cc.run_on_scheduler(state)["bundle"], 30)
                # Share 30 core-s: the chunk itself would stop at 30 s; charged as a loss
                # it is 64 cores x its time since submission, over 30 after 1 s.
                t_f = time.time()
                until(lambda: time.time() - t_f > 1.0, 5)
                cc.unregister_worker_plugin(ComputeTagPlugin.name)
                kill(cc, {tagged[0]})
                thf.join(60)
                os.environ["MA_COMPUTE_PROCS_PER_WORKER"] = "1"
                rf, sf = bf.get("r", ([], {}))
                left_f = cc.run_on_scheduler(state)
                open(stop_b, "w").close()
                open(os.path.join(rd, "go_f"), "w").close()
                ok(not thf.is_alive() and sf.get("stopped") == CAP_STOP and left_f["unrunnable"] == 0,
                   f"P45D-11 a loss that uses the allowance stops the call, with no demand left: "
                   f"alive={thf.is_alive()} {sf.get('stopped')} {left_f['unrunnable']}")
                ok(any("left the cluster" in str(c_.get("error")) for c_ in sf.get("chunks", [])),
                   f"P45D-11b the loss was seen as the worker leaving: {sf.get('chunks')}")
                for b_ in blockers + keep:
                    b_.cancel()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


def _self_test_isolated_dask(ok) -> None:
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        os.makedirs(os.path.join(d, "data"))
        open(os.path.join(d, "data", "scale.txt"), "w").write("3\n")
        open(os.path.join(d, "models", "helper.py"), "w").write("def scale():\n    return float(open('data/scale.txt').read())\n")
        open(os.path.join(d, "models", "fn.py"), "w").write(
            "from helper import scale\ndef f(p):\n    if p.get('boom'):\n        raise ValueError('bad draw')\n    return {'y': p['x'] * scale()}\n")
        # C33: a scheduler and worker started outside this repo cannot import
        # this module (as on Coiled); the task must still run.
        try:
            import distributed  # noqa: F401
        except ImportError:
            pass
        else:
            iso = tempfile.mkdtemp(prefix="ma_iso_")
            sf = os.path.join(iso, "sched.json")
            envi = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
            dask_cli = os.path.join(os.path.dirname(sys.executable), "dask")
            procs_ = [_sp.Popen([dask_cli, "scheduler", "--scheduler-file", sf,
                                 "--host", "127.0.0.1", "--port", "0", "--no-dashboard"], cwd=iso, env=envi,
                                stdout=_sp.DEVNULL, stderr=open(os.path.join(iso, "sched.err"), "w"))]
            try:
                end = time.time() + 60
                while not os.path.exists(sf) and time.time() < end:
                    time.sleep(0.2)
                time.sleep(1.0)       # the file is written just before the scheduler listens
                procs_.append(_sp.Popen([dask_cli, "worker", "--scheduler-file", sf,
                                         "--nthreads", "1", "--no-dashboard"], cwd=iso, env=envi,
                                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
                os.environ["MA_COMPUTE_SCHEDULER"] = json.load(open(sf))["address"]
                from distributed import Client as _C
                with _C(os.environ["MA_COMPUTE_SCHEDULER"]) as cc:
                    cc.wait_for_workers(1, timeout=60)
                with tempfile.TemporaryDirectory() as _iso_td:
                    iso_scripts = os.path.join(_self_test_iso_repo(_iso_td), "scripts")
                    # Imported as a module, as propagate_uncertainty.py does (run as
                    # __main__, dask would ship the functions by value and hide this).
                    code = ("import json, sys; sys.path.insert(0, %r); import compute; "
                            "rows, s = compute.map_fn(%r, 'models/fn.py::f', [{'x': 0}, {'x': 1}, {'x': 2}], ledger=False); "
                            "print(json.dumps([r.get('result') or r.get('error') for r in rows]))") % (iso_scripts, d)
                    code_tag = ("import json, os, sys; sys.path.insert(0, %r); import compute; "
                                "from distributed import Client; "
                                "c = Client(os.environ['MA_COMPUTE_SCHEDULER']); compute.register_compute_tags(c, exclude=[]); "
                                "os.environ.update(MA_COMPUTE_RESOURCE='compute', MA_COMPUTE_TARGET_WORKERS='1'); "
                                "rows, s = compute.map_fn(%r, 'models/fn.py::f', [{'x': 0}, {'x': 1}], ledger=False); "
                                "print(json.dumps([r.get('result') or r.get('error') for r in rows]))") % (iso_scripts, d)
                    # C33b first, on a worker that has never received this module
                    r2 = _sp.run([sys.executable, "-c", code_tag], capture_output=True, text=True, timeout=300,
                                 env=dict(os.environ, PYTHONPATH=""))
                    got2 = r2.stdout.strip().splitlines()[-1] if r2.stdout.strip() else r2.stderr[-400:]
                    ok(got2 == json.dumps([{"y": 0.0}, {"y": 3.0}]),
                       f"C33b the compute-tag plugin works on a cluster that cannot import this module: {got2}")
                    r = _sp.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                                env=dict(os.environ, PYTHONPATH=""))
                    got = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-500:]
                    ok(got == json.dumps([{"y": 0.0}, {"y": 3.0}, {"y": 6.0}]),
                       f"C33 runs on a cluster that cannot import this module: {got}")
                    # P35-D: on the dask path a pool broken inside a chunk heals there, and
                    # a repeated model error stops the whole call.
                    open(os.path.join(d, "models", "p35d.py"), "w").write(
                        "import os, time\n"
                        "def f(p):\n"
                        "    if p.get('crash') and os.environ.get('MA_COMPUTE_SELFTEST_ONCE') is None:\n"
                        "        flag = os.path.join(p['dir'], str(p['x']))\n"
                        "        if not os.path.exists(flag):\n"
                        "            open(flag, 'w').close(); os._exit(9)\n"
                        "    time.sleep(0.2)\n"
                        "    return {'x': p['x']}\n"
                        "def raises(p):\n    raise ValueError('diverged')\n")
                    flags = tempfile.mkdtemp(prefix="ma_p35d_")
                    code_d = ("import json, sys; sys.path.insert(0, %r); import compute; "
                              "ins = [{'x': k, 'dir': %r, 'crash': k in (2, 9)} for k in range(12)]; "
                              "rows, s = compute.map_fn(%r, 'models/p35d.py::f', ins, ledger=False, max_workers=1); "
                              "r2, s2 = compute.map_fn(%r, 'models/p35d.py::raises', [{}] * 40, ledger=False, max_workers=1); "
                              "print(json.dumps([s['n_failed'], [r['result']['x'] for r in rows if r['ok']], "
                              "s2['stopped'] or '', s2['failed_by_kind']]))") % (iso_scripts, flags, d, d)
                    r3 = _sp.run([sys.executable, "-c", code_d], capture_output=True, text=True, timeout=300,
                                 env=dict(os.environ, PYTHONPATH="", MA_COMPUTE_PROCS_PER_WORKER="2"))
                    try:
                        nf, xs, stop2, kinds2 = json.loads(r3.stdout.strip().splitlines()[-1])
                    except (ValueError, IndexError):
                        nf, xs, stop2, kinds2 = None, None, "", {}
                    ok(nf == 0 and xs == list(range(12)),
                       f"P35-D1 a process death inside a dask chunk is healed in the chunk: {r3.stdout[-300:]} {r3.stderr[-300:]}")
                    ok("same model error" in stop2 and kinds2.get("stopped", 0) > 0,
                       f"P35-D2 a repeated model error stops a dask call: {stop2} {kinds2}")

            except Exception:
                print(open(os.path.join(iso, "sched.err")).read()[-3000:])
                raise
            finally:
                os.environ.pop("MA_COMPUTE_SCHEDULER", None)
                for pr in procs_:
                    pr.terminate()
                for pr in procs_:
                    pr.wait(10)


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    _self_test_durable(ok)
    _self_test_sched(ok)
    try:
        import dask  # noqa: F401
        from distributed import Client, LocalCluster, WorkerPlugin  # noqa: F401
    except ImportError:
        print("  (LocalCluster dask tests skipped: distributed not installed)")
    else:
        _self_test_infra(ok)
        _self_test_dask_core(ok)
        _self_test_p42_dask(ok)
        _self_test_p45d(ok)
        _self_test_isolated_dask(ok)
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.parse_args(argv)
    return _self_test()


if __name__ == "__main__":
    sys.exit(main())
