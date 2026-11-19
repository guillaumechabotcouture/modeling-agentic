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
`distinct` block lists each CPU model, VM size, flag set and BLAS seen, so one line answers "one platform or several".
A probe that fails records its error; nothing here raises past `record` (fail soft: never stops the job).
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
    try:
        import urllib.request
        req = urllib.request.Request(IMDS, headers={"Metadata": "true"})
        with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (the fixed link-local IMDS address)
            d = json.loads(r.read().decode())
        return {"vm_size": d.get("vmSize"), "location": d.get("location")}
    except Exception as e:  # noqa: BLE001 -- off Azure, or IMDS blocked: recorded, never raised
        return {"vm_size": None, "error": type(e).__name__}


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


def _workers(client_factory=None) -> dict:
    """{worker address: probe}, or {"_error": why}. No scheduler in the environment: {}."""
    addr = os.environ.get("MA_COMPUTE_SCHEDULER")
    if not addr and client_factory is None:
        return {}
    try:
        if client_factory is None:
            from distributed import Client
            client_factory = lambda: Client(addr, timeout="60s", security=_security())  # noqa: E731
        with client_factory() as c:
            return dict(c.run(probe))
    except Exception as e:  # noqa: BLE001 -- recorded; the next round tries again
        return {"_error": f"{type(e).__name__}: {str(e)[:300]}"}


def _key(p: dict) -> dict:
    blas = p.get("blas") if isinstance(p.get("blas"), list) else []
    return {"cpu_model": (p.get("cpu") or {}).get("model"), "vm_size": (p.get("azure") or {}).get("vm_size"),
            "flags": " ".join((p.get("cpu") or {}).get("flags") or []),
            "blas": " ".join(sorted(f"{b.get('internal_api')}-{b.get('version')}-{b.get('architecture')}"
                                    f"-t{b.get('num_threads')}" for b in blas)),
            "threads_env": " ".join(f"{k}={v}" for k, v in sorted((p.get("threads_env") or {}).items()) if v)}


def merge(doc: dict, local: dict, workers: dict) -> dict:
    """`doc` with the local probe (first kept, latest under `job_host_last`) and new workers added."""
    doc = dict(doc or {})
    doc.setdefault("job_host", local)
    doc["job_host_last"] = local
    seen = doc.setdefault("workers", {})
    errs = doc.setdefault("errors", [])
    for addr, p in (workers or {}).items():
        if addr == "_error":
            errs.append({"at": time.strftime("%FT%TZ", time.gmtime()), "error": p})
        elif addr not in seen:
            seen[addr] = p
    every = [doc["job_host"]] + list(seen.values())
    doc["distinct"] = {k: sorted({str(_key(p)[k]) for p in every}) for k in ("cpu_model", "vm_size", "flags",
                                                                              "blas", "threads_env")}
    doc["n_workers_seen"] = len(seen)
    doc["one_platform"] = all(len(v) == 1 for k, v in doc["distinct"].items() if k != "vm_size") and \
        len([v for v in doc["distinct"]["vm_size"] if v != "None"]) <= 1
    return doc


def record(out: str, client_factory=None, local=None) -> dict:
    try:
        doc = json.load(open(out))
    except (OSError, ValueError):
        doc = {"producer": "cloud/platform_probe.py"}
    try:
        loc = local if local is not None else probe()
    except Exception as e:  # noqa: BLE001
        loc = {"error": f"{type(e).__name__}: {e}"}
    doc = merge(doc, loc, _workers(client_factory))
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(tmp, out)
    return doc


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="platform_probe.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("out")
    r.add_argument("--every", type=float, default=0, help="seconds between rounds (0: one round)")
    r.add_argument("--until", default=None, help="stop once this file exists")
    a = ap.parse_args(argv)
    while True:
        try:
            d = record(a.out)
            print(f"platform: {d['n_workers_seen']} worker(s), one platform: {d['one_platform']}, "
                  f"cpu {d['distinct']['cpu_model']}", flush=True)
        except Exception as e:  # noqa: BLE001 -- fail soft: never stop the job
            print(f"platform probe failed: {type(e).__name__}: {e}", flush=True)
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

            def run(self, fn):
                return self.table
        out = os.path.join(d, "platform.json")
        d1 = record(out, lambda: FakeClient({"tls://a": w("Xeon 8370C", "Standard_E8s_v5")}), local=loc)
        ok(d1["n_workers_seen"] == 1 and d1["one_platform"] is True, f"PP4 one worker on the job's platform: {d1['distinct']}")
        d2 = record(out, lambda: FakeClient({"tls://a": w("other", "x"), "tls://b": w("AMD EPYC 7763", "Standard_E8as_v5")}),
                    local=loc)
        ok(d2["n_workers_seen"] == 2 and d2["workers"]["tls://a"]["cpu"]["model"] == "Xeon 8370C"
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
        ok(main(["record", os.path.join(d, "p3.json"), "--every", "1", "--until", until]) == 0
           and os.path.exists(os.path.join(d, "p3.json")), "PP9 `record --every --until` stops once the file exists")
    if fails:
        print(f"FAILED: {len(fails)} self-test case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
