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


RUN_ROOT_MARKERS = ("run_state.json", "metadata.json")   # a run dir's own files (v2 state; lib.safe_io RUN_MARKERS)


def _sha(path: str) -> str | None:
    """The sha256 of a regular file, streamed through a descriptor opened with no link followed; None for anything
    else (a link, a FIFO, a device, unreadable): a file whose bytes cannot be read is never marked."""
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        h = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


def run_root(path: str) -> str | None:
    """The run dir `path` lies in: its outermost ancestor (or itself) holding a run's own file, so a run_state.json or
    metadata.json written below the run root changes nothing; None outside a run."""
    d, found = os.path.abspath(path), None
    while True:
        if any(os.path.isfile(os.path.join(d, m)) and not os.path.islink(os.path.join(d, m)) for m in RUN_ROOT_MARKERS):
            found = d
        nd = os.path.dirname(d)
        if nd == d:
            return found
        d = nd


def in_flight_name(rel: str) -> bool:
    parts = str(rel).replace(os.sep, "/").split("/")
    return (len(parts) > 1 and parts[-2] == PROGRESS_DIR) or parts[-1].endswith(IN_FLIGHT_SUFFIXES)


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


def _load(run_dir: str) -> dict:
    try:
        doc = json.load(open(os.path.join(run_dir, MARKER)))
        return doc if isinstance(doc, dict) and isinstance(doc.get("files"), dict) else {}
    except (OSError, ValueError):
        return {}


def mark(run_dir: str, source: str, captured_at: str | None = None, hashes: dict | None = None) -> dict | None:
    """Write the marker for a copied run dir (None when it would list nothing). `hashes`: {path relative to the run
    dir: sha256 of the bytes the copy wrote}, e.g. every file a poller downloaded, hashed as it was downloaded and never
    re-read, so a file a local process rewrote since does not match; its in-flight entries are recorded. Without
    `hashes` (a fresh extraction) every in-flight file is hashed as it is now."""
    if hashes is None:
        files = {}
        for rel in in_flight(run_dir):
            h = _sha(os.path.join(run_dir, rel))
            if h:
                files[rel] = h
    else:
        files = {str(r).replace(os.sep, "/"): h for r, h in hashes.items() if in_flight_name(r) and h}
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


def copied_entry(path: str) -> dict | None:
    """The run root's marker entry for `path` while its bytes are those copied ({marker, run_dir, captured_at, source,
    copied_at}); None when it is not in a run dir, the root has no readable marker, the marker does not list it, or
    the file changed since. A marker anywhere below the run root is never read (Codex review of #256 P1-3)."""
    path = os.path.abspath(path)
    root = run_root(os.path.dirname(path))
    if root is None:
        return None
    m = os.path.join(root, MARKER)
    if os.path.islink(m):
        return None
    doc = _load(root)
    want = (doc.get("files") or {}).get(os.path.relpath(path, root).replace(os.sep, "/"))
    if want and want == _sha(path):
        return {"marker": m, "run_dir": root, "captured_at": doc.get("captured_at"),
                "copied_at": doc.get("copied_at"), "source": doc.get("source")}
    return None


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "run_state.json"), "w").write("{}")
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
    # Codex review of #256: the marker is read at the run root only (P1-3), however deep the file; a poll marks only
    # what it fetched and keeps earlier entries, so a local rewrite stops matching (P1-2)
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "run_state.json"), "w").write("{}")
        deep = os.path.join(d, *"a b c d e f g h".split())
        os.makedirs(deep)
        j = os.path.join(deep, "x.jsonl.job.json")
        open(j, "w").write("{}")
        k = os.path.join(d, "y.jsonl.job.json")
        open(k, "w").write("{}")
        mark(d, "poll 1")
        ok(copied_entry(j) is not None, "CR7 a file eight levels below the run root is found by the root's marker")
        os.makedirs(os.path.join(d, "a", PROGRESS_DIR))
        open(os.path.join(d, "a", "z.json"), "w").write("{}")
        mark(os.path.join(d, "a"), "planted")
        ok(copied_entry(j) is not None and copied_entry(j)["source"] == "poll 1",
           "CR7b a marker below the run root is ignored")
        kd = _sha(k)                                     # the bytes the copy downloaded
        open(k, "w").write('{"pid": 9}')                 # a local process rewrote it
        mark(d, "poll 2", hashes={"a/b/c/d/e/f/g/h/x.jsonl.job.json": _sha(j), "y.jsonl.job.json": kd,
                                  "notes.md": "ab"})
        ok(copied_entry(k) is None and copied_entry(j) is not None and "notes.md" not in _load(d)["files"],
           "CR8 a poll marks the bytes it downloaded (never re-hashed); a file rewritten locally does not match")
        open(os.path.join(d, "a", "run_state.json"), "w").write("{}")      # planted below the run root
        ok(run_root(j) == os.path.abspath(d) and copied_entry(j) is not None,
           "CR10 the run root is the outermost run dir: a run_state.json planted below it changes nothing")
        ok(run_root(j) == os.path.abspath(d) and run_root(os.path.join(os.sep, "nonexistent", "x")) is None,
           "CR9 the run root is the nearest ancestor holding run_state.json or metadata.json")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
