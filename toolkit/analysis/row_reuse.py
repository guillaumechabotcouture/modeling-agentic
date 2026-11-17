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


def _sources(d) -> list:
    """Trusted row files of directory d and of its archive d/rows/*: [(file, kind, adapter digest or None)]."""
    from toolkit.analysis import joint_calibration as JC
    out = []
    dirs = [d] + sorted(os.path.join(d, ARCHIVE, x) for x in (os.listdir(os.path.join(d, ARCHIVE))
                                                              if os.path.isdir(os.path.join(d, ARCHIVE)) else []))
    for x in dirs:
        try:
            ad = json.load(open(os.path.join(x, "adapter.json"))).get("adapter_sha256")
        except (OSError, ValueError, AttributeError):
            ad = None
        out += [(f, kind, ad) for f, kind, _ in JC._trusted_sources(x)]
    return out


def pool(d) -> dict:
    """{job_sha: [(row, adapter digest or None)]} of every trusted row under d (joint_calibration.stored_rows' form)."""
    from toolkit.analysis import joint_calibration as JC
    out = {}
    for f, kind, ad in _sources(d):
        for sha, body in JC._source_rows(f, kind):
            out.setdefault(sha, []).append((body, ad))
    return out


def _archive(run_dir, d) -> None:
    """Copy d's trusted row files (with its adapter record) into d/rows/<k>/ unless an identical copy is there."""
    from toolkit.analysis import joint_calibration as JC
    srcs = JC._trusted_sources(d)
    if not srcs:
        return
    have = set()
    a = os.path.join(d, ARCHIVE)
    if os.path.isdir(a):
        for x in os.listdir(a):
            for f, _, _ in JC._trusted_sources(os.path.join(a, x)):
                have.add(JC._sha_file(f))
    todo = [(f, k, m) for f, k, m in srcs if JC._sha_file(f) not in have]
    if not todo:
        return
    k = 1 + max([int(x) for x in os.listdir(a) if x.isdigit()] if os.path.isdir(a) else [0])
    dst = os.path.join(a, str(k))
    os.makedirs(dst)
    for f, _, _ in todo:
        for side in ("", ".summary.json", JC.REUSE_SIDECAR):
            if os.path.isfile(f + side):
                shutil.copy2(f + side, os.path.join(dst, os.path.basename(f) + side))
    if os.path.isfile(os.path.join(d, "adapter.json")):
        shutil.copy2(os.path.join(d, "adapter.json"), os.path.join(dst, "adapter.json"))


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
        _safe_io().publish(os.path.join(d, "adapter.json"), json.dumps({"adapter_sha256": adapter,
                                                                         "files": list(JC.ADAPTER_FILES)}), run_dir)
        b = Bt.run(run_dir, todo, engine, out_rel=out_rel, **kw)
        _archive(run_dir, d)          # the computed rows' own authenticated files, before the merge replaces any
    if hit:
        path = os.path.join(run_dir, out_rel)
        new = {}
        for f, kind, _ in JC._trusted_sources(d) if todo else []:
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
        if not todo:
            _safe_io().publish(os.path.join(d, "adapter.json"), json.dumps({"adapter_sha256": adapter,
                                                                             "files": list(JC.ADAPTER_FILES)}), run_dir)
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
