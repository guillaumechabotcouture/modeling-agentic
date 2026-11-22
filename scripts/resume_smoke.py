#!/usr/bin/env python3
"""Resume smoke before a paid launch: interrupt toy runs at each resumable state, resume them, check the reuse.

    python3 scripts/resume_smoke.py [<launch tree>] [--json <out>]   # default tree: this checkout; exit 1 on a failure
    python3 scripts/resume_smoke.py --self-test

A paid run that is interrupted is only cheap to resume if the code that will run reuses every engine row the
interrupted attempt finished and leaves the signature standing. This checks that on the code a launch ships, before
it ships, with toy engines and toy functions only, locally, in seconds of CPU. `<launch tree>` is the extracted tree
a launch ships (`git archive HEAD`, the tree `cloud/run_cluster.py` builds); the smoke runs that tree's own copy of
this script with the tree as its working directory (`python -I`), so the tree's stage machine, compute.py and
sign-off code are the ones exercised. A tree without this script is a failure.

Scenarios (each a fresh toy v2 run, signed by the lead through the stage machine's own SIGN):
  analyze_timeout  the engine batch step exceeds its time limit with 3 of 6 rows done; the run ends run_failed,
                   resumable "timeout"; --resume (run_v2 again) reopens it at ANALYZE.
  killed_batch     the batch process is killed (SIGKILL) with 3 of 6 rows done; resumable "killed"; --resume.
  cap_stop         the per-call compute allowance stops the batch (rows of kind stopped); the batch is rerun with an
                   allowance (what a relaunch does). The stage machine does not mark a cap stop as resumable
                   (stage_machine.analysis_failure_cause: infra only), so this scenario is checked at the batch
                   level, and the report records that --resume alone does not continue it.
For each: rows reused on resume == ok rows the interrupted attempt kept, zero of those recomputed (the toy function
logs every call), and, for the stage-machine scenarios, the run's sign-off record is unchanged and still binds the
spec (spec_sha256 == the spec's sha256), and the run finishes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALLS, SLOW, KILL, KILLED = "MA_RS_CALLS", "MA_RS_SLOW_FROM", "MA_RS_KILL_AT", "MA_RS_KILLED"
LOAD = "MA_RS_LOAD_S"         # seconds the toy model takes to load (cap_stop: startup longer than the old allowance)
# cap_stop's allowance. The cap is wall-clock seconds x pool slots, counted from before the pool starts, so it pays
# for process start and model load too (compute._map_local). Rows from 1 on never finish (they hold until the stop
# kills them), so this only has to outlast the pool's start and row 0, never fit a count of rows: 2026-11-22, the
# old 1.5 s allowance with 0.6 s rows kept 0 rows whenever a busy CI runner took over 0.9 s to start the pool.
CAP_STOP_ALLOWANCE_S = 12.0
CAP_STOP_LOAD_S = 1.5         # the old allowance: the scenario now holds with a start that alone used all of it
SID = "MA_RS_SMOKE_SID"       # the smoke's own session: the toy never signals a process group in it
SCENARIOS = ("analyze_timeout", "killed_batch", "cap_stop")
N = 6
CUT = 3

TOY_FN = f'''import os
import signal
import time

if os.environ.get("{LOAD}"):                       # a slow model load: what a busy runner's pool start costs
    time.sleep(float(os.environ["{LOAD}"]))


def f(p):
    with open(os.environ["{CALLS}"], "a") as fh:
        fh.write(f"{{p['i']}}\\n")
    if p["i"] >= int(os.environ.get("{KILL}") or 99) and not os.path.exists(os.environ["{KILLED}"]):
        open(os.environ["{KILLED}"], "w").write("x")
        pid = int(open(os.environ["{KILLED}"] + ".driver").read())
        try:
            target = kill_target(os.getsid(0), int(os.environ["{SID}"]), os.getpgid(pid))
        except OSError:
            target = None
        if target is None:
            open(os.environ["{KILLED}"] + ".refused", "w").write("the step is not in a session of its own")
            os._exit(3)                             # fail closed
        os.killpg(target, signal.SIGKILL)           # the batch step's process group dies mid-batch, as on a lost VM
        os._exit(0)                                 # and this worker with it (it holds the step's output pipes)
    if p["i"] >= int(os.environ.get("{SLOW}") or 99):
        driver = int(open(os.environ["{KILLED}"] + ".driver").read())   # recorded before the pool started
        for _ in range(600):                        # until the step is killed by its time limit
            time.sleep(0.1)
            try:
                os.kill(driver, 0)
            except ProcessLookupError:
                os._exit(0)                         # the step is gone: so is this worker
    return {{"v": p["i"]}}
'''

def kill_target(my_sid: int, smoke_sid: int, driver_pgid: int):
    """The process group the toy may kill: its own step session's leader group (the driver's group, whose id is the
    session id), and only when that session is not the smoke's; else None (fail closed). The saved id is signalled,
    not looked up again."""
    if my_sid == smoke_sid or driver_pgid != my_sid:
        return None
    return my_sid


TOY_FN += "\n\n" + __import__("inspect").getsource(kill_target)

DRIVER = '''import json
import os
import sys
sys.path.insert(0, {scripts!r})
import compute


def main(rd):
    if os.environ.get("{killed}"):
        open(os.environ["{killed}"] + ".driver", "w").write(str(os.getpid()))
    os.makedirs(os.path.join(rd, "analysis", "batch"), exist_ok=True)
    rows, _ = compute.run_map(rd, os.path.join(rd, "models", "toy_fn.py") + "::f", [{{"i": i}} for i in range({n})],
                              os.path.join(rd, "analysis", "batch", "rows.jsonl"), backend="local", max_workers=1,
                              purpose="resume smoke")
    json.dump([bool(r.get("ok")) for r in rows], open(os.path.join(rd, "analysis", "toy_rows.json"), "w"))
    return 0 if all(r.get("ok") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
'''




def judge(name: str, before: list, after: list, summary: dict, *, expect_kept=None, sig0=None, sig1=None,
          spec_sha=None, status=None, stage=False, reopened=None, rows=None) -> dict:
    """The assertions for one scenario. `before`/`after`: the toy function's calls in the interrupted attempt and on
    resume; `summary`: the resumed batch's compute.py summary, with `_kept` the ok rows the interrupted attempt
    persisted; `expect_kept`: the rows it must have persisted (else a non-empty proper subset); `stage`: a
    stage-machine scenario, whose lead sign-off (sig0 before, sig1 after) must exist, be unchanged and bind the spec
    (`spec_sha`), whose resume must reopen at ANALYZE (`reopened`) and finish (`status`); `rows`: the final rows."""
    kept = sorted({int(x) for x in summary.get("_kept", [])})
    problems = []
    if expect_kept is not None and kept != sorted(expect_kept):
        problems.append(f"the interrupted attempt persisted rows {kept}, not {sorted(expect_kept)}")
    if expect_kept is None and not 0 < len(kept) < N:
        problems.append(f"the interrupted attempt persisted {len(kept)} of {N} rows (a stop keeps some, not all)")
    if expect_kept is None and not set(before[:-1]) <= set(kept):
        problems.append(f"rows {sorted(set(before[:-1]) - set(kept))} finished (a later call started) but were not kept")
    recomputed = sorted(set(after) & set(kept))
    if summary.get("reused") != len(kept):
        problems.append(f"rows reused {summary.get('reused')} != ok rows the interrupted attempt kept {len(kept)}")
    if recomputed:
        problems.append(f"rows recomputed on resume: {recomputed}")
    if sorted(after) != sorted(set(range(N)) - set(kept)):
        problems.append(f"the resumed calls {sorted(after)} are not exactly the missing rows")
    if summary.get("n_ok") != N:
        problems.append(f"the resumed batch has {summary.get('n_ok')} of {N} ok rows")
    if rows is not None:
        bad = [r.get("i") for r in rows if not r.get("ok") or (r.get("result") or {}).get("v") != r.get("i")]
        if sorted(r.get("i") for r in rows) != list(range(N)) or bad:
            problems.append(f"the final rows do not carry their own inputs' results ({len(rows)} rows; wrong: {bad})")
    if stage:
        if not sig0 or not sig1 or sig0 != sig1:
            problems.append("the sign-off record is missing or changed across the resume")
        elif sig1.get("by") != "lead":
            problems.append(f"the sign-off is by {sig1.get('by')!r}, not the lead")
        elif sig1.get("spec_sha256") != spec_sha:
            problems.append("the sign-off record no longer binds the spec")
        if not reopened:
            problems.append("the resume did not reopen the run at ANALYZE (FINAL -> ANALYZE, resumed_from run_failed)")
        if status not in ("completed", "completed_scope_declared"):
            problems.append(f"the resumed run ended {status}")
    return {"scenario": name, "kept": len(kept), "reused": summary.get("reused"), "computed": summary.get("computed"),
            "recomputed": len(recomputed), "status": status, "ok": not problems, "problems": problems}


def report_problems(returncode: int, rows) -> list:
    """Why a child's report does not stand: a non-zero exit, or not exactly one row per scenario."""
    out = [] if returncode == 0 else [f"the smoke exited {returncode}"]
    names = [r.get("scenario") for r in rows or [] if isinstance(r, dict)]
    if sorted(names) != sorted(SCENARIOS):
        out.append(f"the report has scenarios {names}, not one each of {list(SCENARIOS)}")
    return out


def _kept_from_lines(lines) -> list:
    """Ok rows' inputs (the toy result's v) from result or journal lines; a torn line is skipped, as compute.py's
    reuse skips it."""
    got = []
    for x in lines:
        try:
            r = json.loads(x)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("ok") and "_partial" not in r:
            got.append(r.get("i"))
    return got


def _calls(path) -> list:
    return [int(x) for x in open(path).read().split()] if os.path.exists(path) else []


def _kept(rd) -> list:
    """The ok rows the interrupted attempt left: the finished batch's rows, else its journal."""
    out = os.path.join(rd, "analysis", "batch", "rows.jsonl")
    for p in (out, out + ".partial.jsonl"):
        if os.path.exists(p):
            got = _kept_from_lines([x for x in open(p) if x.strip()])
            if got:
                return got
    return []


def scenarios(work: str) -> list:
    """Run every scenario under `work` with this tree's code; [judge() rows]."""
    for p in (os.path.join(ROOT, "agents"), os.path.join(ROOT, "scripts"), ROOT):
        if p not in sys.path:
            sys.path.insert(0, p)
    import analysis_backend as AB
    import compute
    import stage_machine as SM
    from v2_tasks import StubTasks
    spike = AB.backend("spike")
    quiet = lambda *a: None  # noqa: E731
    drv = os.path.join(work, "resume_smoke_driver.py")
    open(drv, "w").write(DRIVER.format(scripts=os.path.join(ROOT, "scripts"), n=N, killed=KILLED))

    class Batch:
        """The spike backend with the toy engine batch as a pipeline step before its analysis."""
        def __init__(self, tmo):
            self.tmo = tmo

        def __getattr__(self, k):
            return getattr(spike, k)

        def pipeline_steps(self, spec_path, run_dir):
            steps = [dict(s) for s in spike.pipeline_steps(spec_path, run_dir)]
            for s in steps:
                if s["id"] == "analyze":
                    s["inputs"] = s["inputs"] + ["analysis/toy_rows.json"]
            run = " ".join(shlex.quote(x) for x in (sys.executable, drv, run_dir))
            return [{"id": "batch", "run": run, "inputs": [AB.SPEC_FILE],
                     "outputs": ["analysis/toy_rows.json"], "timeout_s": self.tmo}] + steps

    def toy_run(name):
        d = os.path.join(work, name)
        os.makedirs(os.path.join(d, "models"))
        AB.write_toy_units(os.path.join(d, "units.csv"), n=12)
        spec = AB.toy_spec("units.csv")
        spec.pop("engine")
        spec["deliverable_file"] = {"path": "allocation_optimized.csv",
                                    "columns": {"unit": "id", "package": "package", "dalys_averted": "dalys_averted"}}
        SM.init_run(d, "toy question", 3, "lead", "v2", None, derived_inputs=False)
        open(os.path.join(d, "models", "toy_fn.py"), "w").write(TOY_FN)
        return d, StubTasks({"spec": spec, "settings": AB.toy_settings(draws=24, points=16)})

    def sig(d):
        try:
            return json.load(open(os.path.join(d, SM.SIGNOFF_FILE)))
        except (OSError, ValueError):
            return None

    def rows_of(d):
        p = os.path.join(d, "analysis", "batch", "rows.jsonl")
        return [json.loads(x) for x in open(p) if x.strip()] if os.path.exists(p) else []

    out = []
    os.environ[SID] = str(os.getsid(0))
    # the killed scenario gets a generous step limit: its kill, not the limit, must end the first attempt
    for name, env, want, tmo in (("analyze_timeout", {SLOW: str(CUT)}, "timeout", 30),
                                 ("killed_batch", {KILL: str(CUT)}, "killed", 120)):
        d, T = toy_run(name)
        calls = os.path.join(work, f"{name}.calls")
        os.environ.update({CALLS: calls, KILLED: os.path.join(work, f"{name}.killed"), **env})
        # A step's timeout_s is a soft deadline (scripts/pipeline.py, 2026-11-17): the timeout scenario kills at its
        # limit by setting the backstop to it (multiple 1, no 24 h floor) for the first attempt only.
        import pipeline as P_
        saved = {k: getattr(P_, k) for k in ("BACKSTOP_MULTIPLE", "BACKSTOP_FLOOR_S") if hasattr(P_, k)}
        if name == "analyze_timeout" and saved:
            P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S = 1, 0.0
        try:
            s1 = SM.run_v2("toy question", d, T, signoff="lead", log=quiet, backend=Batch(tmo), cross_engine_check=False)
        finally:
            for k in env:
                os.environ.pop(k, None)
            for k, v in saved.items():
                setattr(P_, k, v)
        st = SM.load_state(d)
        why = SM.resumable_failure(d, st)
        before, kept, sig0 = _calls(calls), _kept(d), sig(d)
        if s1 != "run_failed" or why != want:
            out.append({"scenario": name, "ok": False, "problems": [
                f"the interruption did not end run_failed resumable {want!r} ({s1}, {why})"]})
            continue
        n_hist = len(st.history)
        s2 = SM.run_v2("toy question", d, T, signoff="lead", log=quiet, backend=Batch(120), cross_engine_check=False)
        summ = json.load(open(os.path.join(d, "analysis", "batch", "rows.jsonl.summary.json")))
        after = _calls(calls)[len(before):]
        new = SM.load_state(d).history[n_hist:]
        reopened = bool(new) and new[0].get("from") == "FINAL" and new[0].get("to") == "ANALYZE" \
            and new[0].get("resumed_from") == "run_failed"
        with open(os.path.join(d, AB.SPEC_FILE), "rb") as fh:
            spec_sha = hashlib.sha256(fh.read()).hexdigest()
        out.append(judge(name, before, after, dict(summ, _kept=kept), expect_kept=list(range(CUT)), sig0=sig0,
                         sig1=sig(d), spec_sha=spec_sha, status=s2, stage=True, reopened=reopened, rows=rows_of(d)))
    # cap_stop: the per-call allowance stops the batch; rerun with an allowance (a relaunch)
    d = os.path.join(work, "cap_stop")
    os.makedirs(os.path.join(d, "models"))
    open(os.path.join(d, "models", "toy_fn.py"), "w").write(TOY_FN)
    calls = os.path.join(work, "cap_stop.calls")
    outp = os.path.join(d, "analysis", "batch", "rows.jsonl")
    os.makedirs(os.path.dirname(outp))
    # Row 0 finishes; rows from 1 on hold (the SLOW toy, watching this process) until the allowance stops the batch,
    # so exactly row 0 is kept however long the pool takes to start, as long as it starts within the allowance.
    killed = os.path.join(work, "cap.killed")
    open(killed + ".driver", "w").write(str(os.getpid()))
    cap_env = {CALLS: calls, KILLED: killed, SLOW: "1", LOAD: str(CAP_STOP_LOAD_S)}
    os.environ.update(cap_env)
    try:
        _rows, s0 = compute.run_map(d, os.path.join(d, "models", "toy_fn.py") + "::f", [{"i": i} for i in range(N)],
                                    outp, backend="local", max_workers=1, max_core_hours=CAP_STOP_ALLOWANCE_S / 3600,
                                    ledger=False)
        stopped = (s0.get("failed_by_kind") or {}).get(compute.STOPPED, 0)
        why_stopped = str(s0.get("stopped") or "")
        before, kept = _calls(calls), _kept(d)
        returned = sorted(r["i"] for r in _rows if r.get("ok"))
        for k in (SLOW, LOAD):
            os.environ.pop(k, None)
        _rows, s1 = compute.run_map(d, os.path.join(d, "models", "toy_fn.py") + "::f", [{"i": i} for i in range(N)],
                                    outp, backend="local", max_workers=1, ledger=False)
    finally:
        for k in cap_env:
            os.environ.pop(k, None)
    row = judge("cap_stop", before, _calls(calls)[len(before):], dict(s1, _kept=kept), expect_kept=[0],
                rows=rows_of(d))
    if not stopped or not why_stopped.startswith(compute.CAP_STOP):
        row["ok"], row["problems"] = False, row["problems"] + [f"the allowance did not stop the batch ({why_stopped})"]
    if sorted(kept) != returned:
        row["ok"], row["problems"] = False, row["problems"] + [f"rows on disk {sorted(kept)} != returned {returned}"]
    row["note"] = ("a cap stop is not resumable by --resume (stage_machine.analysis_failure_cause: infra only); "
                   "rerun the batch, as a relaunch does, and its rows are reused")
    out.append(row)
    return out


def run_tree(tree: str) -> dict:
    """Run the smoke with `tree`'s own copy of this script (python -I, cwd tree); the report, or a failure."""
    tree = os.path.realpath(tree)
    me = os.path.join(tree, "scripts", "resume_smoke.py")
    if not os.path.isfile(me):
        return {"tree": tree, "ok": False, "scenarios": [], "problems": ["the tree has no scripts/resume_smoke.py"]}
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "OMP_NUM_THREADS",
                                                            "OPENBLAS_NUM_THREADS")}
        r = subprocess.run([sys.executable, "-I", me, "--scenarios", os.path.realpath(work)], cwd=tree, env=env,
                           capture_output=True, text=True, timeout=1800)
    line = next((x for x in reversed(r.stdout.splitlines()) if x.startswith("RESUME_SMOKE ")), None)
    if line is None:
        return {"tree": tree, "ok": False, "scenarios": [],
                "problems": [f"the smoke did not report: {(r.stderr or r.stdout)[-800:]}"]}
    rows = json.loads(line[len("RESUME_SMOKE "):])
    bad = report_problems(r.returncode, rows)
    return {"tree": tree, "ok": not bad and all(x.get("ok") for x in rows), "scenarios": rows, "problems": bad}


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        c or fails.append(m)

    s = {"reused": 3, "computed": 3, "n_ok": N, "_kept": [0, 1, 2]}
    sg = {"by": "lead", "spec_sha256": "h"}
    good = [{"i": i, "ok": True, "result": {"v": i}} for i in range(N)]
    kw = dict(expect_kept=[0, 1, 2], sig0=sg, sig1=dict(sg), spec_sha="h", status="completed", stage=True,
              reopened=True, rows=good)
    ok(judge("x", [0, 1, 2, 3], [3, 4, 5], s, **kw)["ok"], "RS1 a clean resume passes")
    ok(not judge("x", [0, 1, 2, 3], [2, 3, 4, 5], s, **kw)["ok"], "RS2 a kept row computed again fails")
    ok(not judge("x", [0, 1, 2, 3], [3, 4, 5], dict(s, reused=2), **kw)["ok"], "RS3 fewer rows reused than kept fails")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], s, **dict(kw, sig1=dict(sg, by="owner")))["ok"],
       "RS4 a changed sign-off fails")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], s, **dict(kw, spec_sha="other"))["ok"],
       "RS5 a signature that no longer binds the spec fails")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], s, **dict(kw, status="run_failed"))["ok"], "RS6 an unfinished resume fails")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], dict(s, n_ok=5), **kw)["ok"], "RS7 a resumed batch with a failed row fails")
    # Codex review of #255 (3 P1, P2s), reproduced before the fix
    ok(not judge("x", [0, 1, 2], list(range(N)), dict(s, reused=0, computed=N, _kept=[]), **kw)["ok"],
       "RS10 (P1) a journal that lost every kept row fails (the kept rows are required, not intersected)")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], s, **dict(kw, sig0=None, sig1=None))["ok"],
       "RS11 (P1) a stage-machine scenario with no sign-off record fails")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], s, **dict(kw, sig0=dict(sg, by="x"), sig1=dict(sg, by="x")))["ok"],
       "RS11b the sign-off must be the lead's")
    ok(not judge("x", [0, 1, 2], [3, 3, 3], s, **kw)["ok"], "RS12 the resumed calls must be exactly the missing rows")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], s, **dict(kw, rows=good[:3] + [dict(good[3], result={"v": 9})] + good[4:]))
       ["ok"], "RS13 each final row carries its own input's result")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], s, **dict(kw, reopened=False))["ok"],
       "RS14 the resume must reopen the run at ANALYZE (FINAL -> ANALYZE, resumed_from run_failed)")
    ok(not judge("c", [0, 1, 2, 3, 4, 5], [], dict(s, reused=N, computed=0, _kept=list(range(N))), rows=good)["ok"],
       "RS15 a cap stop that stopped nothing (every row kept) fails")
    ok(report_problems(2, [{"scenario": "cap_stop", "ok": True}]) != [] and report_problems(0, [
        {"scenario": n, "ok": True} for n in SCENARIOS]) == [],
       "RS16 the child must exit 0 and report exactly one row per scenario")
    ok(_kept_from_lines(['{"i": 0, "ok": true}', '{"i": 1, "ok": true}', '{"i": 2, "o']) == [0, 1],
       "RS17 (P3) a torn journal line is skipped, as compute.py's reuse skips it")
    ok(kill_target(7, 7, 7) is None and kill_target(9, 7, 8) is None and kill_target(9, 7, 9) == 9,
       "RS18 (P1) the toy signals only its own step session's leader group, never one in the smoke's session")
    ok("def kill_target" in TOY_FN and "kill_target(os.getsid(0)" in TOY_FN,
       "RS18b the toy worker runs that same guard")
    ok(not judge("x", [0, 1, 2], [3, 4, 5], s, **dict(kw, rows=good[:5] + [good[4]]))["ok"],
       "RS19 every input must appear once in the final rows")
    cs = {"reused": 1, "computed": 5, "n_ok": N, "_kept": [0]}
    ok(not judge("c", [0, 1, 2], [1, 2, 3, 4, 5], cs, rows=good)["ok"],
       "RS20 a cap stop that lost a row it finished (a later call started) fails")
    ok(judge("c", [0, 1, 2], [2, 3, 4, 5], dict(cs, reused=2, computed=4, _kept=[0, 1]), rows=good)["ok"],
       "RS20b a cap stop that kept every row it finished passes")
    rep = run_tree(ROOT)
    ok(rep["ok"] and {x["scenario"] for x in rep["scenarios"]} == {"analyze_timeout", "killed_batch", "cap_stop"}
       and all(x["kept"] >= 1 and x["recomputed"] == 0 and x["reused"] == x["kept"] for x in rep["scenarios"]),
       f"RS8 this tree resumes every scenario reusing every kept row: {json.dumps(rep)[:1500]}")
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        r = run_tree(t)
        ok(not r["ok"] and "no scripts/resume_smoke.py" in r["problems"][0], "RS9 a tree without the smoke fails")
    print(f"{'FAIL' if fails else 'OK'}: " + (f"{len(fails)} case(s) failed" if fails else
                                               "all self-test cases passed."))
    return 1 if fails else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--self-test"]:
        return _self_test()
    if argv[:1] == ["--scenarios"]:
        print("RESUME_SMOKE " + json.dumps(scenarios(argv[1])))
        return 0
    out = None
    if "--json" in argv:
        k = argv.index("--json")
        out = argv[k + 1]
        del argv[k:k + 2]
    rep = run_tree(argv[0] if argv else ROOT)
    for x in rep["scenarios"]:
        print(f"{x['scenario']:16s} {'ok' if x.get('ok') else 'FAIL'}  kept {x.get('kept')} reused {x.get('reused')} "
              f"recomputed {x.get('recomputed')} status {x.get('status')}  {'; '.join(x.get('problems') or [])}")
    for p in rep["problems"]:
        print(f"FAIL {p}")
    if out:
        json.dump(rep, open(out, "w"), indent=1)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
