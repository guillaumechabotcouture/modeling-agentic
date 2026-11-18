#!/usr/bin/env python3
"""A run directory copied while its run was live carries in-flight records that describe the process that wrote them.

    python3 scripts/lib/copied_run.py --self-test

F6 run 3 (2026-11-16): a run dir copied out mid-run held analysis/batch/.compute_progress/ records, `*.partial.jsonl`
journals and `*.job.json` records of batches that were running on the VM; read on the laptop they looked live and
misled supervision. They are kept (scripts/compute.py reuses a journal's rows on resume and settles a progress record
by its heartbeat), but the in-repo copy paths (cloud/run_cluster.py `checkpoint`, cloud/vm_job.py's poll sync) write
`copied_run.json` in the copied run dir: each in-flight file with its sha256 and the capture time. A reader
(`compute.py status`) reports a listed file whose bytes are unchanged as copied, not live; once a new process rewrites
it, the entry no longer matches and the file is read as usual.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

MARKER = "copied_run.json"
PROGRESS_DIR = ".compute_progress"                  # scripts/compute.py PROGRESS_DIR
IN_FLIGHT_SUFFIXES = (".partial.jsonl", ".job.json")  # scripts/compute.py PARTIAL_EXT, JOB_EXT


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def in_flight(run_dir: str) -> list[str]:
    """Paths (relative, '/'-separated) of the in-flight records under `run_dir`; links are not followed."""
    out = []
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        in_progress = os.path.basename(root) == PROGRESS_DIR
        for n in files:
            p = os.path.join(root, n)
            if os.path.islink(p):
                continue
            if in_progress or n.endswith(IN_FLIGHT_SUFFIXES):
                out.append(os.path.relpath(p, run_dir).replace(os.sep, "/"))
    return sorted(out)


def mark(run_dir: str, source: str, captured_at: str | None = None) -> dict | None:
    """Write the marker for a copied run dir (None when it holds no in-flight record). An earlier marker's entries are
    kept for files still unchanged; every file listed now is recorded with its current sha256."""
    files = {rel: _sha(os.path.join(run_dir, rel)) for rel in in_flight(run_dir)}
    if not files:
        return None
    doc = {"copied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "captured_at": captured_at,
           "source": source, "files": files,
           "note": "these records were written by a process on the machine the run was copied from; they describe "
                   "it, not a process running here"}
    try:
        from lib.safe_io import write_nofollow
    except ImportError:            # run as scripts/lib/copied_run.py: scripts/ is not on the path
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from lib.safe_io import write_nofollow
    write_nofollow(run_dir, MARKER, json.dumps(doc, indent=1).encode(), atomic=True)   # no link followed, atomic
    return doc


def copied_entry(path: str, max_up: int = 6) -> dict | None:
    """The marker that lists `path` with its current bytes ({marker, run_dir, captured_at, source, copied_at}), found
    in `path`'s ancestors; None when no marker lists it or the file has changed since the copy."""
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    for _ in range(max_up):
        m = os.path.join(d, MARKER)
        if os.path.isfile(m) and not os.path.islink(m):
            try:
                doc = json.load(open(m))
            except (OSError, ValueError):
                return None
            rel = os.path.relpath(path, d).replace(os.sep, "/")
            want = (doc.get("files") or {}).get(rel) if isinstance(doc, dict) else None
            if want and want == _sha(path):
                return {"marker": m, "run_dir": d, "captured_at": doc.get("captured_at"),
                        "copied_at": doc.get("copied_at"), "source": doc.get("source")}
            return None
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "analysis", "batch", PROGRESS_DIR))
        open(os.path.join(d, "analysis", "batch", PROGRESS_DIR, "a.json"), "w").write("{}")
        open(os.path.join(d, "analysis", "batch", "results.jsonl.partial.jsonl"), "w").write("{}\n")
        open(os.path.join(d, "analysis", "batch", "results.jsonl.job.json"), "w").write('{"pid": 1}')
        open(os.path.join(d, "analysis", "batch", "results.jsonl"), "w").write("{}\n")
        ok(in_flight(d) == ["analysis/batch/.compute_progress/a.json", "analysis/batch/results.jsonl.job.json",
                            "analysis/batch/results.jsonl.partial.jsonl"], f"CR1 the in-flight records are found {in_flight(d)}")
        ok(mark(d, "test", "2026-11-16T00:00:00Z")["files"].keys() == set(in_flight(d))
           and os.path.exists(os.path.join(d, MARKER)), "CR2 the marker lists each in-flight file")
        job = os.path.join(d, "analysis", "batch", "results.jsonl.job.json")
        e = copied_entry(job)
        ok(e is not None and e["source"] == "test" and e["captured_at"] == "2026-11-16T00:00:00Z",
           f"CR3 an unchanged copied file is reported copied {e}")
        open(job, "w").write('{"pid": 2}')
        ok(copied_entry(job) is None, "CR4 once a new process rewrites it, the file is no longer read as copied")
        ok(copied_entry(os.path.join(d, "analysis", "batch", "results.jsonl")) is None, "CR5 an unlisted file is not copied")
    with tempfile.TemporaryDirectory() as d:
        ok(mark(d, "test") is None and not os.path.exists(os.path.join(d, MARKER)),
           "CR6 a copy with no in-flight record gets no marker")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
