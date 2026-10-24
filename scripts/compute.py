#!/usr/bin/env python3
"""
Phase 24 α — compute service: run a model function over many parameter sets.

E7 measured where a full run's wall time goes: 55-61% was the modeler waiting
on its own simulations (calibration grids, UQ draws, scenario sweeps) that ran
serially on one VM, polled in 10-minute sleep loops. Those batches are
embarrassingly parallel. This service fans them out.

    python3 scripts/compute.py map {run_dir} --fn models/outcome_fn.py::outcome_fn \\
        --inputs models/grid_points.jsonl --out models/grid_results.jsonl

  --inputs   JSONL, one parameter dict per line (or a JSON list)
  --out      JSONL, one row per input, in input order:
             {"i": 0, "ok": true, "result": {...}, "seconds": 3.1}
             {"i": 1, "ok": false, "error": "ValueError: ...", "seconds": 0.2}
  a summary is printed and written next to --out (`<out>.summary.json`)

Python API (used by scripts/propagate_uncertainty.py):

    from compute import map_fn
    rows = map_fn(run_dir, "models/outcome_fn.py::outcome_fn", list_of_params)

Backends (auto-selected):
  dask   when MA_COMPUTE_SCHEDULER is set (the launcher sets it on Coiled).
         Tasks go to the cluster's workers except those listed in
         MA_COMPUTE_EXCLUDE_WORKERS (the run VMs, whose single thread is busy
         with the run). The run's `models/`, `data/` and the repo's `scripts/`
         are bundled once and unpacked on each worker at the SAME absolute
         paths, so model code with absolute paths works unchanged.
  local  otherwise: a process pool on this machine (cores - 1).

Guardrails: --max-tasks (default 20,000) per call, and a run-wide core-hour
cap (MA_COMPUTE_MAX_CORE_HOURS, default 200) tracked in
`{run_dir}/models/compute_ledger.json`. A function that raises fails only its
own row. Files over --max-file-mb (default 100) are left out of the bundle
and listed in the summary.

Usage:
    python3 scripts/compute.py map ...
    python3 scripts/compute.py --self-test
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_MAX_TASKS = 20000
DEFAULT_MAX_FILE_MB = 100
LEDGER_REL = os.path.join("models", "compute_ledger.json")


# --------------------------------------------------------------------------- #
# Loading the function (same rules locally and on workers)
# --------------------------------------------------------------------------- #

def load_fn(run_dir: str, fn_spec: str):
    """`models/outcome_fn.py::outcome_fn` → callable. The run root and its
    models/ dir are put on sys.path so sibling imports work."""
    path, _, name = fn_spec.partition("::")
    if not name:
        raise ValueError(f"--fn must be <path.py>::<function>, got {fn_spec!r}")
    full = path if os.path.isabs(path) else os.path.join(run_dir, path)
    for p in (run_dir, os.path.dirname(full), os.path.join(REPO, "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    key = "ma_compute_" + hashlib.sha1(full.encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(key, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    fn = getattr(mod, name, None)
    if not callable(fn):
        raise ValueError(f"{fn_spec}: {name!r} is not a callable in {path}")
    return fn


def _jsonable(x):
    try:
        json.dumps(x)
        return x
    except (TypeError, ValueError):
        if isinstance(x, dict):
            return {str(k): _jsonable(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [_jsonable(v) for v in x]
        try:
            return float(x)
        except (TypeError, ValueError):
            return str(x)


def _call(fn, i: int, params: dict) -> dict:
    t0 = time.time()
    try:
        return {"i": i, "ok": True, "result": _jsonable(fn(params)), "seconds": round(time.time() - t0, 3)}
    except Exception as e:  # noqa: BLE001 — one failing row must not kill the batch
        return {"i": i, "ok": False, "error": f"{type(e).__name__}: {e}"[:500],
                "trace": traceback.format_exc()[-1500:], "seconds": round(time.time() - t0, 3)}


# --------------------------------------------------------------------------- #
# Local backend
# --------------------------------------------------------------------------- #

_LOCAL_FN = None


def _local_init(run_dir: str, fn_spec: str):
    global _LOCAL_FN
    os.chdir(run_dir)
    _LOCAL_FN = load_fn(run_dir, fn_spec)


def _local_task(item):
    i, params = item
    return _call(_LOCAL_FN, i, params)


def _map_local(run_dir, fn_spec, inputs, max_workers):
    n = max_workers or max(1, (os.cpu_count() or 2) - 1)
    if n == 1 or len(inputs) == 1:
        _local_init(run_dir, fn_spec)
        return [_local_task((i, p)) for i, p in enumerate(inputs)], {"backend": "local", "workers": 1}
    import multiprocessing as mp
    ctx = mp.get_context("spawn")          # no inherited state from the caller
    with cf.ProcessPoolExecutor(max_workers=n, mp_context=ctx, initializer=_local_init,
                                initargs=(run_dir, fn_spec)) as ex:
        rows = list(ex.map(_local_task, list(enumerate(inputs)), chunksize=max(1, len(inputs) // (n * 8))))
    return rows, {"backend": "local", "workers": n}


# --------------------------------------------------------------------------- #
# Dask backend
# --------------------------------------------------------------------------- #

def make_bundle(run_dir: str, max_file_mb: float = DEFAULT_MAX_FILE_MB) -> tuple[bytes, dict]:
    """tar.gz of the run's models/ and data/ and the repo's scripts/, with
    ABSOLUTE member paths so workers recreate the same layout."""
    run_dir = os.path.realpath(run_dir)
    skipped, n = [], 0
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for root in (os.path.join(run_dir, "models"), os.path.join(run_dir, "data"), os.path.join(REPO, "scripts")):
            for dp, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "_truth")]
                for fn in files:
                    p = os.path.join(dp, fn)
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    if size > max_file_mb * 2**20:
                        skipped.append((os.path.relpath(p, run_dir), round(size / 2**20, 1)))
                        continue
                    t.add(p, arcname=p.lstrip("/"))
                    n += 1
    data = buf.getvalue()
    return data, {"files": n, "bundle_mb": round(len(data) / 2**20, 1), "skipped_large": skipped,
                  "sha256": hashlib.sha256(data).hexdigest()}


def _remote_task(bundle: bytes, sha: str, run_dir: str, fn_spec: str, chunk: list):
    """Runs on a dask worker: unpack the bundle once per worker (marker file
    keyed by sha), then evaluate a chunk of (i, params)."""
    import os, tarfile, io, sys  # noqa: E401 — self-contained (pickled by value)
    marker = os.path.join(run_dir, f".ma_compute_{sha[:16]}")
    if not os.path.exists(marker):
        if os.path.isdir(os.path.join(run_dir, "models")) and not os.path.exists(marker) \
                and os.path.exists(os.path.join(run_dir, "metadata.json")):
            pass   # this is the live run directory itself: never overwrite it
        else:
            tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz").extractall("/", filter="data")
            os.makedirs(run_dir, exist_ok=True)
            open(marker, "w").write(sha)
    # dask workers are daemonic (no multiprocessing children), so the chunk
    # runs in a subprocess using this module's local backend across the VM's
    # cores. MA_COMPUTE_PROCS_PER_WORKER caps processes for memory-heavy models.
    import json, subprocess, tempfile
    with tempfile.TemporaryDirectory() as td:
        inp, out = os.path.join(td, "in.json"), os.path.join(td, "out.jsonl")
        json.dump([p for _, p in chunk], open(inp, "w"))
        procs = os.environ.get("MA_COMPUTE_PROCS_PER_WORKER") or str(max(1, (os.cpu_count() or 2) - 1))
        here = os.path.abspath(__file__) if "__file__" in globals() else None
        script = here if here and os.path.exists(here) else os.path.join(REPO, "scripts", "compute.py")
        env = {k: v for k, v in os.environ.items() if not k.startswith("MA_COMPUTE_SCHEDULER")}
        env["MA_COMPUTE_MAX_CORE_HOURS"] = "1e9"     # the caller's ledger is authoritative
        r = subprocess.run([sys.executable, script, "map", run_dir, "--fn", fn_spec, "--inputs", inp,
                            "--out", out, "--backend", "local", "--max-workers", procs, "--no-ledger"],
                           cwd=run_dir, env=env, capture_output=True, text=True)
        if not os.path.exists(out):
            raise RuntimeError(f"chunk subprocess failed (rc {r.returncode}): {r.stderr[-800:]}")
        rows = [json.loads(l) for l in open(out) if l.strip()]
    return [{**row, "i": chunk[k][0]} for k, row in enumerate(rows)]


def export_worker_env(tls_dir: str) -> dict:
    """Call INSIDE a dask worker task (the launcher's run job). Returns the
    env a subprocess needs to reach this cluster: the scheduler address and,
    for TLS clusters such as Coiled, the worker's own client credentials
    written to files only this user can read."""
    from distributed import get_worker
    w = get_worker()
    env = {"MA_COMPUTE_SCHEDULER": w.scheduler.address}
    sec = getattr(w, "security", None)
    # Coiled workers hold only worker credentials (tls_client_* unset); the
    # scheduler accepts them from a client too, so fall back to them.
    creds = {}
    if sec is not None:
        creds = {
            "ca.pem": getattr(sec, "tls_ca_file", None),
            "cert.pem": getattr(sec, "tls_client_cert", None) or getattr(sec, "tls_worker_cert", None),
            "key.pem": getattr(sec, "tls_client_key", None) or getattr(sec, "tls_worker_key", None),
        }
    if any(creds.values()):
        os.makedirs(tls_dir, mode=0o700, exist_ok=True)
        for fn, v in creds.items():
            if not v:
                continue
            dest = os.path.join(tls_dir, fn)
            data = v if str(v).lstrip().startswith("-----BEGIN") else open(os.path.abspath(v)).read()
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(data)
        env["MA_COMPUTE_TLS_DIR"] = tls_dir
    return env


def _security():
    d = os.environ.get("MA_COMPUTE_TLS_DIR")
    if not d:
        return None
    from distributed.security import Security
    pem = lambda n: (lambda p: p if os.path.exists(p) else None)(os.path.join(d, n))  # noqa: E731
    return Security(tls_ca_file=pem("ca.pem"), tls_client_cert=pem("cert.pem"),
                    tls_client_key=pem("key.pem"), require_encryption=True)


def _map_dask(run_dir, fn_spec, inputs, max_workers, chunks_per_worker=4, timeout_s=None):
    import cloudpickle
    from distributed import Client, as_completed
    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    addr = os.environ["MA_COMPUTE_SCHEDULER"]
    exclude = {w.strip() for w in os.environ.get("MA_COMPUTE_EXCLUDE_WORKERS", "").split(",") if w.strip()}
    run_dir = os.path.realpath(run_dir)
    bundle, binfo = make_bundle(run_dir)
    with Client(addr, timeout="60s", security=_security()) as c:
        workers = [w for w in c.scheduler_info()["workers"] if w not in exclude]
        if max_workers:
            workers = workers[:max_workers]
        if not workers:
            raise RuntimeError("no compute workers available (all are excluded run workers)")
        bfut = c.scatter(bundle, workers=workers, broadcast=True)
        items = list(enumerate(inputs))
        n_chunks = max(1, min(len(items), len(workers) * chunks_per_worker))
        size = -(-len(items) // n_chunks)
        chunks = [items[k:k + size] for k in range(0, len(items), size)]
        futs = [c.submit(_remote_task, bfut, binfo["sha256"], run_dir, fn_spec, ch,
                         workers=workers, allow_other_workers=False, pure=False, retries=1)
                for ch in chunks]
        rows = []
        deadline = time.time() + timeout_s if timeout_s else None
        for f in as_completed(futs):
            try:
                rows += f.result()
            except Exception as e:  # noqa: BLE001 — a lost worker fails only its chunk
                idx = futs.index(f)
                rows += [{"i": i, "ok": False, "error": f"chunk failed: {type(e).__name__}: {e}"[:500]}
                         for i, _ in chunks[idx]]
            if deadline and time.time() > deadline:
                for g in futs:
                    g.cancel()
                break
    done = {r["i"] for r in rows}
    rows += [{"i": i, "ok": False, "error": "timed out"} for i in range(len(inputs)) if i not in done]
    return sorted(rows, key=lambda r: r["i"]), {"backend": "dask", "workers": len(workers), "bundle": binfo}


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #

def _ledger(run_dir: str) -> dict:
    try:
        return json.load(open(os.path.join(run_dir, LEDGER_REL)))
    except (OSError, ValueError):
        return {"core_seconds": 0.0, "calls": []}


def map_fn(run_dir: str, fn_spec: str, inputs: list, backend: str = "auto",
           max_workers: int | None = None, max_tasks: int = DEFAULT_MAX_TASKS,
           timeout_s: float | None = None, ledger: bool = True) -> tuple[list[dict], dict]:
    if len(inputs) > max_tasks:
        raise ValueError(f"{len(inputs)} tasks exceeds --max-tasks {max_tasks}")
    led = _ledger(run_dir)
    cap_h = float(os.environ.get("MA_COMPUTE_MAX_CORE_HOURS", "200"))
    if ledger and led["core_seconds"] / 3600 >= cap_h:
        raise RuntimeError(f"run compute cap reached: {led['core_seconds'] / 3600:.1f} of {cap_h:g} core-hours")
    if backend == "auto":
        backend = "dask" if os.environ.get("MA_COMPUTE_SCHEDULER") else "local"
    t0 = time.time()
    if backend == "dask":
        rows, info = _map_dask(run_dir, fn_spec, inputs, max_workers, timeout_s=timeout_s)
    else:
        rows, info = _map_local(run_dir, fn_spec, inputs, max_workers)
    core_s = sum(r.get("seconds") or 0 for r in rows)
    summary = {**info, "n": len(rows), "n_ok": sum(r["ok"] for r in rows),
               "n_failed": sum(not r["ok"] for r in rows), "wall_s": round(time.time() - t0, 1),
               "task_core_s": round(core_s, 1)}
    if not ledger:
        return rows, summary
    led["core_seconds"] = round(led["core_seconds"] + core_s, 1)
    led["calls"].append({k: summary[k] for k in ("backend", "workers", "n", "n_failed", "wall_s", "task_core_s")}
                        | {"fn": fn_spec, "ts": time.strftime("%FT%T")})
    try:
        os.makedirs(os.path.join(run_dir, "models"), exist_ok=True)
        json.dump(led, open(os.path.join(run_dir, LEDGER_REL), "w"), indent=1)
    except OSError:
        pass
    return rows, summary


def _read_inputs(path: str) -> list:
    txt = open(path).read().strip()
    if txt.startswith("["):
        return json.loads(txt)
    return [json.loads(l) for l in txt.splitlines() if l.strip()]


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
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
        b, info = make_bundle(d)
        names = tarfile.open(fileobj=io.BytesIO(b)).getnames()
        ok(any(n.endswith("models/fn.py") and not n.startswith("/") for n in names)
           and any(n.endswith("scripts/compute.py") for n in names), f"C7 bundle has models/ and scripts/: {info}")
        # C8: dask backend against a local cluster, when distributed is installed
        try:
            from distributed import LocalCluster
        except ImportError:
            print("  (C8 skipped: distributed not installed)")
        else:
            with LocalCluster(n_workers=2, threads_per_worker=1, processes=True, dashboard_address=None) as lc:
                os.environ["MA_COMPUTE_SCHEDULER"] = lc.scheduler_address
                try:
                    rows, s = map_fn(d, "models/fn.py::f", inputs)
                    ok(s["backend"] == "dask" and rows[7]["result"] == {"y": 21.0} and s["n_failed"] == 1
                       and [r["i"] for r in rows] == list(range(41)), f"C8 dask backend: {s}")
                    w0 = list(lc.scheduler_info["workers"])[0]
                    os.environ["MA_COMPUTE_EXCLUDE_WORKERS"] = w0
                    rows, s = map_fn(d, "models/fn.py::f", inputs[:4])
                    ok(s["workers"] == 1, f"C9 excluded run worker not used: {s}")
                finally:
                    os.environ.pop("MA_COMPUTE_SCHEDULER", None)
                    os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a model function over many parameter sets")
    ap.add_argument("cmd", nargs="?", choices=["map"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--fn")
    ap.add_argument("--inputs")
    ap.add_argument("--out")
    ap.add_argument("--backend", default="auto", choices=["auto", "local", "dask"])
    ap.add_argument("--max-workers", type=int)
    ap.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    ap.add_argument("--timeout-s", type=float)
    ap.add_argument("--no-ledger", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.cmd != "map" or not (a.run_dir and a.fn and a.inputs and a.out):
        ap.error("usage: compute.py map <run_dir> --fn <path.py>::<fn> --inputs <jsonl> --out <jsonl>")
    rows, summary = map_fn(a.run_dir, a.fn, _read_inputs(a.inputs), a.backend, a.max_workers,
                           a.max_tasks, a.timeout_s, ledger=not a.no_ledger)
    with open(a.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    json.dump(summary, open(a.out + ".summary.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "bundle"}))
    return 0 if summary["n_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
