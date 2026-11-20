"""Engine-batch row reuse for the outcome and boundary batches (F6 run 2, 2026-11-17).

F6 run 2 stopped in its fix loop with its outcome batch (8,192 jobs) and boundary batch (20 tasks) computed. A resume,
or any re-analysis in a fix round, would have recomputed both: compute.py keeps a stored row only under the same code
fingerprint, which hashes the run's models/ and data/, and the run itself writes there after the batch
(models/decision_tests/, the compute ledger, data/input_checks.json): 0 of 256 and 0 of 20 rows eligible. #241 gave
the joint calibration a reuse keyed on each job's inputs instead; this module gives the same to any batch with a fixed
results path (batch.run's `out_rel`):

- rows come only from trusted files (joint_calibration._trusted_sources: a recorded digest that equals the file's);
- a row answers a job when its job_sha, its engine identity (code, parameters, configuration) and the adapter digest
  (joint_calibration.adapter_digest: the code between job and engine call) are the current ones; rows from before this
  module recorded no adapter and are admitted only when $MA_JC_LEGACY_ADAPTER_SHA256 attests the current digest;
- no row file is lost: the directory's trusted files are archived under `<dir>/rows/<k>/` before anything rewrites
  them, and so are the files a batch of the remaining jobs writes; the results file at the fixed path is then
  republished in job order with a digest sidecar (joint_calibration.REUSE_SIDECAR).

The agent-written models/ext/ is untouched: it stays in compute.py's fingerprint, which still decides the reuse of
rows this module does not handle."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
ARCHIVE = "rows"
RECORD = "adapter_rows.json"       # {sha256 of a row file: the adapter digest that wrote it} (Codex review of #244 P1-1)


def _record(d) -> dict:
    try:
        r = json.load(open(os.path.join(d, RECORD)))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in r.items()} if isinstance(r, dict) else {}


def _bind(run_dir, d, files, adapter) -> None:
    """Record `adapter` for each file's current digest; an entry binds bytes, so a file rewritten later is unbound."""
    from toolkit.analysis import joint_calibration as JC
    from toolkit.analysis.artifacts import _safe_io
    rec = _record(d)
    for f in files:
        h = JC._sha_file(f)
        if h:
            rec[h] = adapter
    _safe_io().publish(os.path.join(d, RECORD), json.dumps(rec, sort_keys=True), run_dir)


def _dirs(d) -> list:
    a = os.path.join(d, ARCHIVE)
    if os.path.islink(a):
        raise ValueError(f"{a} is a link: the row archive is refused")
    return [d] + (sorted(os.path.join(a, x) for x in os.listdir(a) if x.isdigit()) if os.path.isdir(a) else [])


def _sources(d) -> list:
    """Trusted row files of directory d and of its archive d/rows/*: [(file, kind, adapter digest or None)], each
    file's adapter the one its digest is bound to (None: unbound, admitted only under the legacy attestation)."""
    from toolkit.analysis import joint_calibration as JC
    rec = _record(d)
    return [(f, kind, rec.get(JC._sha_file(f) or "")) for x in _dirs(d) for f, kind, _ in JC._trusted_sources(x)]


def pool(d) -> dict:
    """{job_sha: [(row, adapter digest or None)]} of every trusted row under d (joint_calibration.stored_rows' form)."""
    from toolkit.analysis import joint_calibration as JC
    out = {}
    for f, kind, ad in _sources(d):
        for sha, body in JC._source_rows(f, kind):
            out.setdefault(sha, []).append((body, ad))
    return out


def _archive(run_dir, d) -> None:
    """Copy d's trusted row files and their sidecars into d/rows/<k>/ (no link followed, inside the run dir) unless a
    copy with the same digest is there; the digest keeps its adapter binding (RECORD is keyed by digest)."""
    from toolkit.analysis import joint_calibration as JC
    from toolkit.analysis.artifacts import _safe_io
    sio = _safe_io()
    srcs = JC._trusted_sources(d)
    if not srcs:
        return
    have = {JC._sha_file(f) for x in _dirs(d)[1:] for f, _, _ in JC._trusted_sources(x)}
    todo = [f for f, _, _ in srcs if JC._sha_file(f) not in have]
    if not todo:
        return
    a = os.path.join(d, ARCHIVE)
    k = 1 + max([int(x) for x in os.listdir(a) if x.isdigit()] if os.path.isdir(a) else [], default=0)
    for f in todo:
        for side in ("", ".summary.json", JC.REUSE_SIDECAR):
            if os.path.isfile(f + side):
                data, _ = sio.read_nofollow(run_dir, os.path.relpath(f + side, run_dir))
                sio.publish(os.path.join(a, str(k), os.path.basename(f) + side), data, run_dir)


def _untrusted_rows(d) -> bool:
    from toolkit.analysis import joint_calibration as JC
    trusted = {f for f, _, _ in JC._trusted_sources(d)}
    return any(os.path.isfile(os.path.join(d, n)) and os.path.join(d, n) not in trusted
               for n in ("results.jsonl", "points.jsonl"))


def run(run_dir, jobs, engine, out_rel, log=print, Bt=None, **kw) -> dict:
    """batch.run's contract ({results, problems, summary, path}) with stored rows reused first (module docstring)."""
    from toolkit.analysis import batch as B
    from toolkit.analysis import joint_calibration as JC
    from toolkit.analysis.artifacts import _safe_io
    Bt = Bt or B
    d = os.path.dirname(os.path.join(run_dir, out_rel))
    os.makedirs(d, exist_ok=True)
    _archive(run_dir, d)
    adapter = JC.adapter_digest()
    legacy_ok = os.environ.get(JC.LEGACY_ADAPTER_ENV) == adapter
    pl = pool(d)
    hit = JC._admit(pl, jobs, engine, adapter, legacy_ok) if pl else {}
    todo = [j for j in jobs if j["id"] not in hit]
    b = {"results": {}, "problems": [], "summary": {"n": 0, "n_failed": 0}, "path": os.path.join(run_dir, out_rel)}
    if todo:
        if _untrusted_rows(d):       # Codex review of #244 P1-2: compute.py's own cache never reads an untrusted file
            kw = dict(kw, fresh=True)
        before = {n: JC._sha_file(os.path.join(d, n)) for n in os.listdir(d)}
        b = Bt.run(run_dir, todo, engine, out_rel=out_rel, **kw)
        _bind(run_dir, d, [f for f, _, _ in JC._trusted_sources(d)        # only what this call just wrote
                           if before.get(os.path.basename(f)) != JC._sha_file(f)], adapter)
        _archive(run_dir, d)          # the computed rows' own authenticated files, before the merge replaces any
    if hit:
        path = os.path.join(run_dir, out_rel)
        new = {}
        rec = _record(d)
        for f, kind, _ in JC._trusted_sources(d) if todo else []:
            if rec.get(JC._sha_file(f) or "") == adapter:
                for sha, body in JC._source_rows(f, kind):
                    new.setdefault(sha, body)
        rows = []
        for n, j in enumerate(jobs):
            body = hit[j["id"]][0] if j["id"] in hit else new.get(B.job_sha(j))
            if body is not None:
                rows.append(json.dumps({"i": n, "ok": True, "result": body}) + "\n")
        text = "".join(rows)
        _safe_io().publish(path, text, run_dir)
        _safe_io().publish(path + JC.REUSE_SIDECAR, json.dumps({
            "out_sha256": hashlib.sha256(text.encode()).hexdigest(), "rows": len(rows), "reused": len(hit),
            "computed": len(todo), "legacy": sum(1 for _, lg in hit.values() if lg)}), run_dir)
        if not any(lg for _, lg in hit.values()):   # bound only when every reused row was the current adapter's
            _bind(run_dir, d, [path], adapter)
        log(f"{out_rel}: {len(hit)} of {len(jobs)} engine rows reused from stored rows (job_sha, engine identity and "
            f"adapter match); {len(todo)} computed")
    res = dict(b["results"], **{k: v[0]["result"] for k, v in hit.items()})
    summ = dict(b.get("summary") or {}, n=len(jobs), reused=len(hit), computed=len(todo))
    return {"results": res, "problems": list(b.get("problems") or []), "summary": summ,
            "path": os.path.join(run_dir, out_rel)}


def _self_test() -> int:
    import tempfile
    from unittest import mock
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    from toolkit.spec import schema
    from toolkit.analysis import batch as B
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    with tempfile.TemporaryDirectory() as d:
        spec = schema.load(schema.toy_spec(d))
        eng = spec.engine()
        _, jobs = B.design(spec)
        jobs = jobs[:12]
        rel = "analysis/batch/results.jsonl"
        sent, real = [], B.run

        def spy(run_dir, js, e, **kw):
            sent.append(len(js))
            return real(run_dir, js, e, **kw)
        with mock.patch.object(B, "run", spy):
            r1 = run(d, jobs, eng, rel, log=lambda *a: None, max_workers=2)
        ok(sum(sent) == len(jobs) and len(r1["results"]) == len(jobs), f"RR1 a fresh batch computes every job "
                                                                     f"({sum(sent)} of {len(jobs)})")
        os.makedirs(os.path.join(d, "models", "decision_tests"), exist_ok=True)     # a harness write after the batch
        open(os.path.join(d, "models", "decision_tests", "DTx.yaml"), "w").write("status: run\n")
        rc = compute.reuse_report(d)
        el = sum(b_["eligible"] for b_ in rc["batches"] if b_["out"].startswith("analysis/batch"))
        ok(el == 0, f"RR2 (the F6 run 2 failure) a harness write under models/ makes compute.py's own reuse refuse "
                    f"the stored batch: {el} eligible")
        sent.clear()
        with mock.patch.object(B, "run", spy):
            r2 = run(d, jobs, eng, rel, log=lambda *a: None, max_workers=2)
        ok(sum(sent) == 0 and r2["results"] == r1["results"] and r2["summary"]["reused"] == len(jobs),
           f"RR3 this module reuses every stored row by job_sha, engine identity and adapter ({sum(sent)} sent)")
        rows = [json.loads(x) for x in open(os.path.join(d, rel))]
        ok(len(rows) == len(jobs) and [r["i"] for r in rows] == list(range(len(jobs))),
           "RR4 the results file at its fixed path holds every job's row, in job order")
        sent.clear()
        with mock.patch.object(B, "run", spy):
            r3 = run(d, jobs + [dict(jobs[0], id="extra", seed=jobs[0]["seed"] + 1)], eng, rel, log=lambda *a: None,
                     max_workers=2)
        ok(sum(sent) == 1 and len(r3["results"]) == len(jobs) + 1,
           f"RR5 a new job alongside stored ones: only it is computed ({sum(sent)} sent)")
        sent.clear()
        with mock.patch.object(B, "run", spy):
            run(d, jobs, eng, rel, log=lambda *a: None, max_workers=2)
        ok(sum(sent) == 0, "RR6 the earlier rows survive a mixed call (archived before any rewrite)")
        srcs = _sources(os.path.join(d, "analysis", "batch"))
        f0 = srcs[0][0]
        txt = open(f0).read()
        open(f0, "w").write(txt.replace('"ok": true', '"ok": true ', 1))     # altered after its digest was recorded
        ok(all(f != f0 for f, _, _ in _sources(os.path.join(d, "analysis", "batch"))),
           "RR7 a row file altered after its digest was recorded is no source")
        with mock.patch.object(sys.modules[__name__], "_sources", lambda dd: []), mock.patch.object(B, "run", spy):
            sent.clear()
            run(d, jobs[:2], eng, rel, log=lambda *a: None, max_workers=2)
        ok(sum(sent) == 2, "RR8 with no trusted source the jobs are computed")
    # Codex review of #244 (2 P1, 2 P2, 1 P3), reproduced
    with tempfile.TemporaryDirectory() as d:
        spec = schema.load(schema.toy_spec(d))
        eng = spec.engine()
        _, jobs = B.design(spec)
        jobs = jobs[:6]
        rel = "analysis/batch/results.jsonl"
        bd = os.path.join(d, "analysis", "batch")
        run(d, jobs, eng, rel, log=lambda *a: None, max_workers=2)
        # P1-1: a row file the current adapter never recorded is not relabelled by a later write of the record
        rec = os.path.join(bd, RECORD)
        ok(os.path.isfile(rec) and all(isinstance(v, str) for v in json.load(open(rec)).values()),
           "RR10a the adapter is recorded per file digest (adapter_rows.json), not per directory")
        json.dump({}, open(rec, "w"))          # as if the files were written by another adapter, unrecorded
        sent.clear()
        with mock.patch.object(B, "run", spy):
            run(d, jobs, eng, rel, log=lambda *a: None, max_workers=2)
        ok(sum(sent) == len(jobs), f"RR10b rows no record binds to the current adapter are not reused unattested, "
                                   f"and no later call relabels them ({sum(sent)} sent)")
        # P1-2: an untrusted completed file is never reused by compute.py underneath
        rows = [json.loads(x) for x in open(os.path.join(d, rel))]
        rows[0]["result"]["result"]["windows"] = {"cal": {"prev": 0.999}}
        open(os.path.join(d, rel), "w").write("".join(json.dumps(r) + "\n" for r in rows))
        for f in os.listdir(bd):
            if f.endswith(".summary.json") or f.endswith(".reuse.json"):
                os.remove(os.path.join(bd, f))
        shutil.rmtree(os.path.join(bd, ARCHIVE), ignore_errors=True)
        os.remove(rec)
        r = run(d, jobs[:1], eng, rel, log=lambda *a: None, max_workers=2)
        v = r["results"][jobs[0]["id"]]["windows"]
        ok(v != {"cal": {"prev": 0.999}}, f"RR11 an untrusted completed file is not reused through compute.py ({v})")
        # P2-1: a symlinked archive is refused, nothing written outside the run dir
        with tempfile.TemporaryDirectory() as out:
            shutil.rmtree(os.path.join(bd, ARCHIVE), ignore_errors=True)
            os.symlink(out, os.path.join(bd, ARCHIVE))
            try:
                run(d, jobs[:1], eng, rel, log=lambda *a: None, max_workers=2)
                got = "ran"
            except Exception as e:  # noqa: BLE001
                got = type(e).__name__
            ok(os.listdir(out) == [], f"RR12 a symlinked archive directory receives nothing ({got}, {os.listdir(out)})")
            os.remove(os.path.join(bd, ARCHIVE))
        # P2-2: an empty archive directory does not block
        os.makedirs(os.path.join(bd, ARCHIVE), exist_ok=True)
        try:
            run(d, jobs[:1], eng, rel, log=lambda *a: None, max_workers=2)
            got = "ok"
        except ValueError as e:
            got = f"ValueError {e}"
        ok(got == "ok", f"RR13 an empty archive directory does not block a retry ({got})")
    import inspect
    from toolkit.analysis import run as RN, boundary as BD
    ok("RR.run(" in inspect.getsource(RN) and "RR.run(" in inspect.getsource(BD),
       "RR9 the outcome batch (run.py) and the boundary batch (boundary.py) go through this module")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit("usage: row_reuse.py --self-test")
