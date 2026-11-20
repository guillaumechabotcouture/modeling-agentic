"""Record the computing platform of a job and of every compute worker of its cluster (F6 rerun prep, 2026-11-18).

A CI run showed decision-test outputs that differ across machines. batch.platform_info() records the system,
machine and library versions, which do not tell two CPUs or two BLAS builds apart. This records, for the job's own
host and for each worker of its Dask cluster: the CPU model and the vector-instruction flags a BLAS picks its
kernels by, the thread settings (OMP/OpenBLAS/MKL), the BLAS libraries loaded and their thread counts
(threadpoolctl), the library versions and, on Azure, the VM size. It reads; it computes nothing and changes nothing.

    python cloud/platform_probe.py record <out.json> [--every 600] [--until <file>]
    python cloud/platform_probe.py --self-test

`record` probes the local host, then every worker of the scheduler in MA_COMPUTE_SCHEDULER (TLS from
MA_COMPUTE_TLS_DIR, as scripts/compute.py; no scheduler: the local host only), and merges into <out.json>: a worker
seen before keeps its first probe, a new one is added, so workers that join or replace a lost VM are recorded too.
`--every S` repeats until `--until <file>` exists (a job runs it in the background beside main.py). The file's
`distinct` block lists each CPU model, VM size, flag set and BLAS seen, so one line answers "one platform or several"
(`one_platform`: null until at least one worker has answered). A probe that fails records its error; nothing here
raises past `record` (fail soft: never stops the job), and an existing record it cannot read is never overwritten.
"""
from __future__ import annotations

import json
import os
import sys
import time

THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "BLIS_NUM_THREADS",
               "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "OPENBLAS_CORETYPE", "MKL_CBWR")
FLAGS = ("avx", "avx2", "fma", "avx512f", "avx512dq", "avx512bw", "avx512vl", "avx512_vnni", "avx512_bf16",
         "amx_tile", "amx_bf16", "sha_ni")
IMDS = "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01"


def _cpuinfo(path="/proc/cpuinfo") -> dict:
    model, flags, n = None, set(), 0
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.partition(":")
                k = k.strip()
                if k == "processor":
                    n += 1
                elif k == "model name" and model is None:
                    model = v.strip()
                elif k == "flags" and not flags:
                    flags = set(v.split())
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {"model": model, "logical_cpus": n, "flags": [f for f in FLAGS if f in flags]}


def _vm_size(timeout=2.0):
    """The Azure VM size from IMDS, bounded in total by `timeout` (a reply that trickles in cannot hold a worker)."""
    import threading
    box = {}

    def ask():
        try:
            import urllib.request
            req = urllib.request.Request(IMDS, headers={"Metadata": "true"})
            with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (the fixed link-local address)
                d = json.loads(r.read(65536).decode())
            box["r"] = {"vm_size": d.get("vmSize"), "location": d.get("location")}
        except Exception as e:  # noqa: BLE001 -- off Azure, or IMDS blocked: recorded, never raised
            box["r"] = {"vm_size": None, "error": type(e).__name__}
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("r") or {"vm_size": None, "error": f"no answer within {timeout:g} s"}


def probe(cpuinfo_path="/proc/cpuinfo", imds=True) -> dict:
    """This process's platform. Safe to run on a Dask worker (c.run): imports only what is installed."""
    import platform
    out = {"host": platform.node(), "system": platform.system(), "machine": platform.machine(),
           "python": platform.python_version(), "cpu_count": os.cpu_count(), "cpu": _cpuinfo(cpuinfo_path),
           "threads_env": {k: os.environ.get(k) for k in THREAD_VARS}, "at": time.strftime("%FT%TZ", time.gmtime())}
    if platform.system() == "Darwin":
        out["cpu"] = {"model": platform.processor() or None, "logical_cpus": os.cpu_count(), "flags": []}
    from importlib import metadata         # versions read from the installed distributions; nothing is imported
    for mod in ("numpy", "scipy", "starsim"):
        try:
            out[mod] = metadata.version(mod)
        except Exception:  # noqa: BLE001
            out[mod] = None
    try:
        import numpy  # noqa: F401 -- loads the BLAS, so threadpoolctl sees it
        from threadpoolctl import threadpool_info
        out["blas"] = [{k: i.get(k) for k in ("user_api", "internal_api", "version", "num_threads", "architecture",
                                              "threading_layer", "prefix")} for i in threadpool_info()]
    except Exception as e:  # noqa: BLE001
        out["blas"] = {"error": f"{type(e).__name__}: {e}"}
    out["azure"] = _vm_size() if imds else {"vm_size": None, "error": "not probed"}
    return out


def _security():
    d = os.environ.get("MA_COMPUTE_TLS_DIR")
    if not d:
        return None
    from distributed.security import Security
    pem = lambda n: (lambda p: p if os.path.exists(p) else None)(os.path.join(d, n))  # noqa: E731
    return Security(tls_ca_file=pem("ca.pem"), tls_client_cert=pem("cert.pem"), tls_client_key=pem("key.pem"),
                    require_encryption=True)


RUN_TIMEOUT_S = 120.0       # a broadcast that does not answer in this long is recorded as an error (the loop goes on)


def _workers(client_factory=None, timeout_s: float = RUN_TIMEOUT_S) -> dict:
    """{worker address: probe | {"_error": why}}, or {"_error": why} when the scheduler cannot be asked. No scheduler
    in the environment: {}. One worker failing keeps the others' answers (on_error="return"); the whole call is
    bounded by `timeout_s` (a worker that never replies cannot hold the loop)."""
    addr = os.environ.get("MA_COMPUTE_SCHEDULER")
    if not addr and client_factory is None:
        return {}
    import threading
    box = {}

    def ask():
        try:
            factory = client_factory
            if factory is None:
                from distributed import Client
                factory = lambda: Client(addr, timeout="60s", security=_security())  # noqa: E731
            try:                       # workers need not have this file: send probe by value, as compute.py does
                import cloudpickle
                cloudpickle.register_pickle_by_value(sys.modules[probe.__module__])
            except Exception:  # noqa: BLE001
                pass
            with factory() as c:
                got = c.run(probe, on_error="return")
            box["r"] = {a: (p if isinstance(p, dict) else {"_error": f"{type(p).__name__}: {str(p)[:300]}"})
                        for a, p in dict(got).items()}
        except Exception as e:  # noqa: BLE001 -- recorded; the next round tries again
            box["r"] = {"_error": f"{type(e).__name__}: {str(e)[:300]}"}
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(timeout_s)
    return box.get("r") or {"_error": f"the workers did not answer within {timeout_s:g} s"}


def _key(p: dict) -> dict:
    blas = p.get("blas")
    env = {k: v for k, v in sorted((p.get("threads_env") or {}).items()) if v is not None}
    return {"cpu_model": (p.get("cpu") or {}).get("model"), "vm_size": (p.get("azure") or {}).get("vm_size"),
            "flags": " ".join((p.get("cpu") or {}).get("flags") or []),
            "blas": None if not isinstance(blas, list) else
            json.dumps(sorted(([b.get(k) for k in ("internal_api", "version", "architecture", "threading_layer",
                                                   "num_threads")] for b in blas),
                              key=lambda row: json.dumps(row, default=str)), default=str),
            "threads_env": json.dumps(env)}


def merge(doc: dict, local: dict, workers: dict) -> dict:
    """`doc` with the local probe (first kept; each later one that differs kept under `job_host_seen`) and new workers
    added. A worker is its address and its host, so a replacement VM on a reused address is a new worker. A worker
    that failed is recorded as an error, not as seen (the next round asks it again). `one_platform` is null (unknown)
    until a worker has answered or when a compared CPU model is unknown."""
    doc = dict(doc or {})
    doc.setdefault("job_host", local)
    doc["job_host_last"] = local
    hosts = doc.setdefault("job_host_seen", [doc["job_host"]])
    if all(_key(h) != _key(local) for h in hosts):
        hosts.append(local)
    seen = doc.setdefault("workers", {})
    errs = doc.setdefault("errors", [])
    now = time.strftime("%FT%TZ", time.gmtime())
    for addr, p in (workers or {}).items():
        if addr == "_error":
            errs.append({"at": now, "error": p})
        elif "_error" in p:
            errs.append({"at": now, "worker": addr, "error": p["_error"]})
        else:
            k = f"{addr} {p.get('host')}"
            if k not in seen:
                seen[k] = p
    every = list(hosts) + list(seen.values())
    doc["distinct"] = {k: sorted({str(_key(p)[k]) for p in every}) for k in ("cpu_model", "vm_size", "flags",
                                                                              "blas", "threads_env")}
    doc["n_workers_seen"] = len(seen)
    same = all(len(v) == 1 for k, v in doc["distinct"].items() if k != "vm_size") and \
        len([v for v in doc["distinct"]["vm_size"] if v != "None"]) <= 1
    unknown = not seen or "None" in doc["distinct"]["cpu_model"] or "None" in doc["distinct"]["blas"]
    doc["one_platform"] = None if unknown else same
    return doc


def record(out: str, client_factory=None, local=None, lock_s: float = 60.0) -> dict:
    """One round. Never raises: a problem is returned under `error` (and printed by main). An existing record that
    cannot be read is never overwritten. Rounds on one record are serialised by a lock file."""
    import fcntl
    try:
        lock = open(out + ".lock", "a")
    except OSError as e:
        return {"error": f"the record cannot be written: {type(e).__name__}: {e}"}
    try:
        t0 = time.monotonic()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - t0 > lock_s:
                    return {"error": f"{out}.lock held for over {lock_s:g} s; this round skipped"}
                time.sleep(0.1)
        doc = {"producer": "cloud/platform_probe.py"}
        try:
            os.lstat(out)
            present = True
        except FileNotFoundError:          # only a missing file starts a fresh record
            present = False
        except OSError as e:
            return {"error": f"{out}: its status cannot be read ({type(e).__name__}: {e}); not overwritten"}
        if present:
            try:
                doc = json.load(open(out))
                if not isinstance(doc, dict):
                    raise ValueError("not a mapping")
            except (OSError, ValueError) as e:
                return {"error": f"{out} exists but cannot be read ({type(e).__name__}: {e}); not overwritten"}
        try:
            loc = local if local is not None else probe()
        except Exception as e:  # noqa: BLE001
            loc = {"error": f"{type(e).__name__}: {e}"}
        doc = merge(doc, loc, _workers(client_factory))
        tmp = f"{out}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(doc, f, indent=1, sort_keys=True)
            os.replace(tmp, out)
        except OSError as e:
            return dict(doc, error=f"the record cannot be written: {type(e).__name__}: {e}")
        return doc
    finally:
        lock.close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="platform_probe.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("out")
    r.add_argument("--every", type=float, default=0, help="seconds between rounds (0: one round)")
    r.add_argument("--until", default=None, help="stop once this file exists")
    a = ap.parse_args(argv)
    import math
    if not math.isfinite(a.every) or not 0 <= a.every <= 86400:
        print(f"--every {a.every}: seconds in [0, 86400]", file=sys.stderr)
        return 2

    def say(msg):
        try:
            print(msg, flush=True)
        except (OSError, ValueError):            # a closed log pipe never fails the probe
            pass
    while True:
        try:
            d = record(a.out)
            if "error" in d:
                say(f"platform probe: {d['error']}")
            if "distinct" in d:
                say(f"platform: {d['n_workers_seen']} worker(s), one platform: {d['one_platform']}, "
                    f"cpu {d['distinct']['cpu_model']}")
        except Exception as e:  # noqa: BLE001 -- fail soft: never stop the job
            say(f"platform probe failed: {type(e).__name__}: {e}")
        if not a.every or (a.until and os.path.exists(a.until)):
            return 0
        time.sleep(a.every)
        if a.until and os.path.exists(a.until):
            return 0


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        ci = os.path.join(d, "cpuinfo")
        open(ci, "w").write("processor\t: 0\nmodel name\t: Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz\n"
                            "flags\t\t: fpu avx avx2 fma avx512f avx512bw\n\nprocessor\t: 1\n"
                            "model name\t: Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz\nflags\t\t: fpu avx\n")
        c = _cpuinfo(ci)
        ok(c["model"].startswith("Intel(R) Xeon(R) Platinum 8370C") and c["logical_cpus"] == 2
           and c["flags"] == ["avx", "avx2", "fma", "avx512f", "avx512bw"],
           f"PP1 /proc/cpuinfo gives the CPU model, the count and the vector flags of the first CPU: {c}")
        ok("error" in _cpuinfo(os.path.join(d, "missing")), "PP2 an unreadable cpuinfo is recorded, not raised")
        p = probe(ci, imds=False)
        ok({"cpu", "threads_env", "numpy", "blas", "python", "azure"} <= set(p) and "OMP_NUM_THREADS" in p["threads_env"],
           f"PP3 a probe records the CPU, the thread settings, the libraries and the BLAS: {sorted(p)}")

        def w(model, vm, omp="1"):
            return {"cpu": {"model": model, "flags": ["avx2"]}, "azure": {"vm_size": vm},
                    "threads_env": {"OMP_NUM_THREADS": omp}, "blas": [{"internal_api": "openblas", "version": "0.3.30",
                                                                       "architecture": "SkylakeX", "num_threads": 1}]}
        loc = w("Xeon 8370C", "Standard_E8s_v5")

        class FakeClient:
            def __init__(self, table):
                self.table = table

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def run(self, fn, **kw):
                return self.table
        out = os.path.join(d, "platform.json")
        d1 = record(out, lambda: FakeClient({"tls://a": w("Xeon 8370C", "Standard_E8s_v5")}), local=loc)
        ok(d1["n_workers_seen"] == 1 and d1["one_platform"] is True, f"PP4 one worker on the job's platform: {d1['distinct']}")
        d2 = record(out, lambda: FakeClient({"tls://a": w("other", "x"), "tls://b": w("AMD EPYC 7763", "Standard_E8as_v5")}),
                    local=loc)
        ok(d2["n_workers_seen"] == 2 and d2["workers"]["tls://a None"]["cpu"]["model"] == "Xeon 8370C"
           and d2["one_platform"] is False and "AMD EPYC 7763" in d2["distinct"]["cpu_model"],
           f"PP5 a later round adds a new worker, keeps a seen worker's first probe, and flags two platforms: "
           f"{d2['distinct']}")

        def boom():
            raise OSError("scheduler gone")
        d3 = record(out, boom, local=loc)
        ok(d3["n_workers_seen"] == 2 and d3["errors"] and "scheduler gone" in d3["errors"][-1]["error"],
           "PP6 an unreachable scheduler is recorded as an error and the record is kept (fail soft)")
        d4 = record(os.path.join(d, "p2.json"), lambda: FakeClient({"tls://a": w("Xeon 8370C", "Standard_E8s_v5", omp=None)}),
                    local=loc)
        ok(d4["one_platform"] is False and len(d4["distinct"]["threads_env"]) == 2,
           f"PP7 a worker without the thread pin is a different platform: {d4['distinct']['threads_env']}")
        env0 = os.environ.pop("MA_COMPUTE_SCHEDULER", None)
        try:
            ok(_workers() == {}, "PP8 no scheduler in the environment: the local host only")
        finally:
            if env0 is not None:
                os.environ["MA_COMPUTE_SCHEDULER"] = env0
        until = os.path.join(d, "stop")
        open(until, "w").close()
        env0 = os.environ.pop("MA_COMPUTE_SCHEDULER", None)       # Codex P2: the self-test never reaches a live one
        try:
            ok(main(["record", os.path.join(d, "p3.json"), "--every", "1", "--until", until]) == 0
               and os.path.exists(os.path.join(d, "p3.json")), "PP9 `record --every --until` stops once the file exists")
        finally:
            if env0 is not None:
                os.environ["MA_COMPUTE_SCHEDULER"] = env0

        # Codex review of #274 (P1s), each a case:
        class Partial(FakeClient):
            def run(self, fn, **kw):
                return {"tls://a": w("Xeon 8370C", "Standard_E8s_v5"), "tls://b": OSError("worker gone")} \
                    if kw.get("on_error") == "return" else (_ for _ in ()).throw(OSError("worker gone"))
        d10 = record(os.path.join(d, "p10.json"), lambda: Partial({}), local=loc)
        ok("tls://a" in str(d10["workers"]) and d10["n_workers_seen"] == 1 and d10["errors"],
           f"PP10 one worker failing the broadcast keeps the others' probes and records its error: {d10['workers'].keys()}")
        p11 = os.path.join(d, "p11.json")
        open(p11, "w").write("{not json")
        d11 = record(p11, lambda: FakeClient({"tls://a": w("Xeon 8370C", "Standard_E8s_v5")}), local=loc)
        ok(open(p11).read() == "{not json" and "error" in d11,
           f"PP11 an existing record that cannot be read is never overwritten: {d11.get('error')}")
        d12 = record(os.path.join(d, "missing_dir", "p.json"), lambda: FakeClient({}), local=loc)
        ok("error" in d12, f"PP12 an output that cannot be written is reported, not raised: {d12.get('error')}")

        class Hang(FakeClient):
            def run(self, fn, **kw):
                time.sleep(5)
                return {}
        t0 = time.time()
        d13 = _workers(lambda: Hang({}), timeout_s=0.5)
        ok(time.time() - t0 < 3 and "_error" in d13, f"PP13 a broadcast that never returns is bounded: {d13}")
        p14 = os.path.join(d, "p14.json")
        record(p14, lambda: FakeClient({"tls://a": w("Xeon 8370C", "Standard_E8s_v5")}), local=loc)
        r2 = dict(w("AMD EPYC 7763", "Standard_E8as_v5"), host="other-host")
        d14 = record(p14, lambda: FakeClient({"tls://a": r2}), local=loc)
        ok(d14["n_workers_seen"] == 2 and d14["one_platform"] is False,
           f"PP14 a replacement worker on a reused address is a new worker: {d14['distinct']['cpu_model']}")
        p15 = os.path.join(d, "p15.json")
        record(p15, lambda: FakeClient({"tls://a": w("Xeon 8370C", "Standard_E8s_v5")}), local=loc)
        d15 = record(p15, lambda: FakeClient({}), local=w("AMD EPYC 7763", "Standard_E8s_v5"))
        ok(d15["one_platform"] is False and "AMD EPYC 7763" in d15["distinct"]["cpu_model"],
           f"PP15 a later local probe on another CPU counts: {d15['distinct']['cpu_model']}")
        pth = w("Xeon 8370C", "Standard_E8s_v5")
        omp = w("Xeon 8370C", "Standard_E8s_v5")
        pth["blas"][0]["threading_layer"], omp["blas"][0]["threading_layer"] = "pthreads", "openmp"
        d16 = record(os.path.join(d, "p16.json"), lambda: FakeClient({"tls://a": pth, "tls://b": omp}), local=pth)
        ok(d16["one_platform"] is False, f"PP16 BLAS builds differing in threading layer differ: {d16['distinct']['blas']}")
        d17 = record(os.path.join(d, "p17.json"), boom, local=loc)
        ok(d17["one_platform"] is None and d17["n_workers_seen"] == 0,
           f"PP17 no worker seen: one platform is unknown (null), not true: {d17['one_platform']}")
        import threading
        p18 = os.path.join(d, "p18.json")
        th = [threading.Thread(target=record, args=(p18, (lambda i=i: FakeClient({f"tls://{i}": w("Xeon 8370C", "Standard_E8s_v5")})),
                                                    loc)) for i in range(6)]
        [t.start() for t in th]
        [t.join() for t in th]
        ok(json.load(open(p18))["n_workers_seen"] == 6, "PP18 overlapping rounds on one record lose no worker")
        ok(main(["record", os.path.join(d, "p19.json"), "--every", "nan"]) == 2
           and main(["record", os.path.join(d, "p19.json"), "--every", "1e20"]) == 2,
           "PP19 a negative, non-finite or over-a-day --every is refused")
        # Codex re-review of #274 (P1s):
        a20 = w("Xeon 8370C", "Standard_E8s_v5")
        a20["threads_env"] = {"MKL_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        b20 = w("Xeon 8370C", "Standard_E8s_v5")
        b20["threads_env"] = {"MKL_NUM_THREADS": "1 OMP_NUM_THREADS=1"}
        d20 = record(os.path.join(d, "p20.json"), lambda: FakeClient({"tls://a": b20}), local=a20)
        ok(d20["one_platform"] is False, f"PP20 thread settings compare exactly, not as joined text: {d20['distinct']}")
        a21 = dict(w("Xeon 8370C", "Standard_E8s_v5"), blas={"error": "ImportError"})
        b21 = dict(w("Xeon 8370C", "Standard_E8s_v5"), blas=[])
        d21 = record(os.path.join(d, "p21.json"), lambda: FakeClient({"tls://a": b21}), local=a21)
        ok(d21["one_platform"] is None, f"PP21 a BLAS that could not be read makes the answer unknown: {d21['one_platform']}")
        import fcntl
        p22 = os.path.join(d, "p22.json")
        held = open(p22 + ".lock", "a")
        fcntl.flock(held, fcntl.LOCK_EX)
        t0 = time.time()
        d22 = record(p22, lambda: FakeClient({}), local=loc, lock_s=0.5)
        held.close()
        ok(time.time() - t0 < 3 and "lock" in d22.get("error", ""), f"PP22 a held lock is waited for a bounded time: {d22}")
        p23 = os.path.join(d, "p23.json")
        os.symlink(os.path.join(d, "nodir", "target.json"), p23)
        d23 = record(p23, lambda: FakeClient({}), local=loc)
        ok(os.path.islink(p23) and "error" in d23, f"PP23 an output path that is a dangling link is not replaced: {d23}")
        import urllib.request
        slow = lambda *a_, **k: time.sleep(10)     # noqa: E731 -- an IMDS that never finishes its answer
        t0 = time.time()
        orig = urllib.request.urlopen
        urllib.request.urlopen = slow
        try:
            v = _vm_size(timeout=0.5)
        finally:
            urllib.request.urlopen = orig
        ok(time.time() - t0 < 3 and v.get("vm_size") is None, f"PP24 the IMDS read is bounded in total: {v}")

        class Pipe:
            def write(self, s):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError
        import contextlib
        with contextlib.redirect_stdout(Pipe()):
            rc25 = main(["record", os.path.join(d, "p25.json")])
        ok(rc25 == 0, "PP25 a closed log pipe does not fail the probe")
        # Codex third review of #274 (P1): only a missing file allows a fresh record
        p26 = os.path.join(d, "p26.json")
        open(p26, "w").write("{invalid json")
        real_lstat = os.lstat

        def eio(path, *a_, **k):
            if os.fspath(path) == p26:
                raise OSError(5, "Input/output error")
            return real_lstat(path, *a_, **k)
        os.lstat = eio
        try:
            d26 = record(p26, lambda: FakeClient({}), local=loc)
        finally:
            os.lstat = real_lstat
        ok(open(p26).read() == "{invalid json" and "error" in d26,
           f"PP26 an output whose status cannot be read (EIO) is not replaced: {d26.get('error')}")
        none_v = w("Xeon 8370C", "Standard_E8s_v5")
        none_v["blas"] = none_v["blas"] + [dict(none_v["blas"][0], version=None)]
        d27 = record(os.path.join(d, "p27.json"), lambda: FakeClient({"tls://a": none_v}), local=none_v)
        ok("distinct" in d27 and "error" not in d27 and
           json.loads(d27["distinct"]["blas"][0])[0][1] in ("0.3.30", None),
           f"PP27 BLAS metadata with a missing version still compares, rows kept as rows: {d27.get('distinct')}")
    if fails:
        print(f"FAILED: {len(fails)} self-test case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    rc = main()
    try:
        if sys.stdout is not None:         # None: started with stdout closed
            sys.stdout.flush()
    except (OSError, ValueError):          # a closed log pipe: no output left to give, and not a failure
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.exit(rc)
