"""Self-tests of agents/stage_machine.py (resume), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import json
import os
import sys
import time
import analysis_backend as AB
import _stage_machine_under_test as _SM
from _stage_machine_under_test import RunState, analysis_failure_cause, load_state, resumable_failure, run_v2
from sm_selftest_calibration_refused import _self_test_calibration_refused
from sm_selftest_common import _TOY_BATCH_DRIVER, _TOY_BATCH_FN


def _self_test_resume(ok, td: str, spike, new_run) -> None:
    """T58-T60 (Nigeria v2 full run, 2026-11-02: ANALYZE -> FINAL run_failed when the runner's 1-h limit killed the
    engine batch). A toy engine batch through scripts/compute.py, as a pipeline step before the spike's analysis."""
    import contextlib
    import io
    import yaml
    from v2_tasks import StubTasks
    quiet = lambda *a: None  # noqa: E731
    drv = os.path.join(td, "toy_batch_driver.py")
    open(drv, "w").write(_TOY_BATCH_DRIVER.format(scripts=os.path.join(_SM.REPO, "scripts")))

    class Batch:
        """The spike backend with a toy engine batch step first; `run` replaces the batch command."""
        def __init__(self, tmo, run=None):
            self.tmo, self.run = tmo, run

        def __getattr__(self, k):
            return getattr(spike, k)

        def pipeline_steps(self, spec_path, run_dir):
            steps = [dict(s) for s in spike.pipeline_steps(spec_path, run_dir)]
            for s in steps:
                if s["id"] == "analyze":
                    s["inputs"] = s["inputs"] + ["analysis/toy_rows.json"]
            return [{"id": "batch", "run": self.run or f"{sys.executable} {drv} {run_dir}", "inputs": [AB.SPEC_FILE],
                     "outputs": ["analysis/toy_rows.json"], "timeout_s": self.tmo}] + steps

    def toy_run(name):
        d, T, _st = new_run(name, {})
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        open(os.path.join(d, "models", "toy_fn.py"), "w").write(_TOY_BATCH_FN)
        return d, T

    # T58s (owner decision, 2026-11-17: warn, don't kill): a batch step that runs past its timeout_s (1 s here) is
    # warned about and recorded while it runs, and the run goes on to finish; nothing is killed at the soft deadline.
    import pipeline as P_
    d, T = toy_run("analyze_soft")
    os.environ.update(MA_TOY_CALLS=os.path.join(td, "toy_calls_soft.txt"), MA_TOY_SLEEP_S="1.5")
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            s0 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(1), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_SLEEP_S", None)
    try:
        prog = json.load(open(os.path.join(d, P_.PROGRESS)))["steps"]["batch"]
        row = next(r for r in json.load(open(os.path.join(d, P_.REPORT)))["steps"] if r["id"] == "batch")
    except (OSError, ValueError, KeyError, StopIteration):
        prog, row = {}, {}
    ok(s0 in ("completed", "completed_scope_declared") and prog.get("state") == "ok"
       and prog.get("soft_deadline_s") == 1 and prog.get("backstop_s") == 24 * 3600 and row.get("over_soft_deadline")
       and "past its soft deadline" in err.getvalue(),
       f"T58s a step past its timeout_s warns, records its progress and runs on; the run finishes ({s0}, {prog}, "
       f"{row})")
    # T58: past its backstop (made 8 s here: BACKSTOP_MULTIPLE 1, no 24 h floor) the batch step is killed with 3 of
    # 6 rows done: run_failed, recorded as resumable.
    d, T = toy_run("analyze_timeout")
    calls = os.path.join(td, "toy_calls.txt")
    os.environ.update(MA_TOY_CALLS=calls, MA_TOY_SLOW_FROM="3")
    saved = (P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S)
    P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S = 1, 0.0
    try:
        s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(8), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_SLOW_FROM", None)
        P_.BACKSTOP_MULTIPLE, P_.BACKSTOP_FLOOR_S = saved
    st = load_state(d)
    first = open(calls).read().split() if os.path.exists(calls) else []
    kill = next((r for r in json.load(open(os.path.join(d, P_.REPORT)))["steps"] if r["id"] == "batch"), {})
    ok(s1 == "run_failed" and st.history[-1]["from"] == "ANALYZE" and st.history[-1].get("resumable") == "timeout"
       and resumable_failure(d, st) == "timeout" and first[:3] == ["0", "1", "2"]
       and "at its backstop" in str(kill.get("error")),
       f"T58 an analysis step killed at its backstop ends run_failed, marked resumable ({s1}, {st.history[-1]}, "
       f"calls {first}, {kill.get('error')})")
    # T59: --resume (run_v2 on the same run) continues at ANALYZE with the step limit as the backend gives it now,
    # and the batch computes only the rows the killed attempt had not finished.
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    st = load_state(d)
    again = open(calls).read().split()[len(first):]
    back = [h for h in st.history if h.get("from") == "FINAL"]
    tmo = {s["id"]: s.get("timeout_s") for s in yaml.safe_load(open(os.path.join(d, "models", "pipeline.yaml")))["steps"]}
    ok(s2 in ("completed", "completed_scope_declared") and len(back) == 1 and back[0]["to"] == "ANALYZE"
       and back[0].get("resumed_from") == "run_failed" and tmo["batch"] == 120,
       f"T59 --resume reopens the run at ANALYZE, rewrites the step limits and finishes ({s2}, {back}, {tmo})")
    ok(sorted(again) == ["3", "4", "5"],
       f"T59b the resumed batch reuses the rows the killed attempt kept and computes the rest only ({again})")
    ok(run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False) == s2
       and len([h for h in load_state(d).history if h.get("from") == "FINAL"]) == 1,
       "T59c a finished run is not reopened")
    # T60: other run_failed causes stay terminal.
    d, T = toy_run("analyze_error")
    open(os.path.join(td, "toy_fail.py"), "w").write("import sys\nsys.exit(3)\n")
    fail = Batch(30, run=f"{sys.executable} {os.path.join(td, 'toy_fail.py')}")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=fail, cross_engine_check=False)
    n = len(load_state(d).history)
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=fail, cross_engine_check=False)
    st = load_state(d)
    ok(s1 == s2 == "run_failed" and len(st.history) == n and st.history[-1]["from"] == "ANALYZE"
       and "resumable" not in st.history[-1]
       and resumable_failure(d, st) is None,
       f"T60 an analysis step that fails on its own stays run_failed on resume ({s1}, {s2}, {st.history[-1]})")
    _self_test_calibration_refused(ok, td, toy_run, Batch, quiet)
    rep = {"ok": False, "errors": [], "steps": [{"id": "analyze", "status": "failed", "returncode": 1, "error": ""}]}
    causes = [analysis_failure_cause(dict(rep, steps=[dict(rep["steps"][0], returncode=rc, error=e)]))
              for rc, e in ((-9, ""), (-15, ""), (-11, ""), (1, "boom"), (None, "OSError: no such file"))]
    ok(causes == ["killed", "killed", None, None, None],
       f"T60b killed by SIGKILL/SIGTERM is resumable; a crash, an error exit or a failed start is not ({causes})")
    st_spec = RunState(question="q", state="FINAL", status="run_failed",
                       history=[{"from": "SPEC", "to": "FINAL", "why": "the spec did not validate"}])
    os.makedirs(os.path.join(d, "models"), exist_ok=True)
    json.dump(dict(rep, steps=[dict(rep["steps"][0], returncode=None, error="timed out after 9s (process group killed)")]),
              open(os.path.join(d, "models", "pipeline_report.json"), "w"))
    ok(resumable_failure(d, st_spec) is None and resumable_failure(d, RunState(
        question="q", state="FINAL", status="completed", history=[{"from": "ANALYZE", "to": "FINAL"}])) is None,
       "T60c a run_failed outside ANALYZE, or another status, is not resumable whatever the report says")

    class Unreadable(Batch):
        def pipeline_steps(self, spec_path, run_dir):
            raise ValueError("MA_COMPUTE_MAX_CORE_HOURS='lots' is not a number")
    d, T = toy_run("configure_unreadable")
    s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Unreadable(30), cross_engine_check=False)
    st = load_state(d)
    ok(s1 == "run_failed" and st.history[-1]["from"] == "CONFIGURE" and "MA_COMPUTE_MAX_CORE_HOURS" in st.history[-1]["why"],
       f"T60d an unreadable compute budget fails the run at CONFIGURE (fail closed): {st.history[-1]}")
    # T61 (Nigeria v2 full run F3, 2026-11-02: 16 of 256 design points failed with kind infra when the autoscaler
    # retired busy workers, the analysis failed closed, and the run could not be resumed): an analysis that failed
    # only because engine rows failed with kind infra is resumable, read from the batch summary compute.py wrote.
    d, T = toy_run("analyze_infra")
    calls2 = os.path.join(td, "toy_calls_infra.txt")
    os.environ.update(MA_TOY_CALLS=calls2, MA_TOY_INFRA_FROM="3")
    try:
        s1 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    finally:
        os.environ.pop("MA_TOY_INFRA_FROM", None)
    st = load_state(d)
    first = open(calls2).read().split() if os.path.exists(calls2) else []
    ok(s1 == "run_failed" and st.history[-1]["from"] == "ANALYZE" and st.history[-1].get("resumable") == "infra"
       and resumable_failure(d, st) == "infra" and sorted(first) == ["0", "1", "2", "3", "4", "5"],
       f"T61 an analysis that failed only on infra engine rows ends run_failed, marked resumable ({s1}, "
       f"{st.history[-1]}, calls {first})")
    s2 = run_v2("toy question", d, T, signoff="none", log=quiet, backend=Batch(120), cross_engine_check=False)
    st = load_state(d)
    again = open(calls2).read().split()[len(first):]
    back = [h for h in st.history if h.get("from") == "FINAL"]
    ok(s2 in ("completed", "completed_scope_declared") and len(back) == 1 and back[0]["to"] == "ANALYZE"
       and "infra" in back[0]["why"] and sorted(again) == ["3", "4", "5"],
       f"T61b --resume reopens the run at ANALYZE and the batch recomputes only the infra rows ({s2}, {back}, "
       f"{again})")
    # T61c: the cause is read from the batch summaries this run of the pipeline wrote: infra only.
    d = os.path.join(td, "infra_cases")
    os.makedirs(os.path.join(d, "models"), exist_ok=True)
    os.makedirs(os.path.join(d, "analysis", "batch"), exist_ok=True)
    rp = os.path.join(d, "models", "pipeline_report.json")
    sp = os.path.join(d, "analysis", "batch", "points.jsonl.summary.json")
    rep1 = {"ok": False, "errors": [], "seconds": 30.0,
            "steps": [{"id": "analyze", "status": "failed", "returncode": 1, "error": "ValueError: 512 engine results "
                       "missing"}]}

    def cause_with(kinds, age_s=0.0, summary=True):
        json.dump(rep1, open(rp, "w"))
        if os.path.exists(sp):
            os.remove(sp)
        if summary:
            json.dump({"n": 256, "n_failed": sum(kinds.values()), "failed_by_kind": kinds, "stopped": None},
                      open(sp, "w"))
            t_ = time.time() - age_s
            os.utime(sp, (t_, t_))
        return analysis_failure_cause(rep1, d)
    got = [cause_with({"infra": 16}), cause_with({"infra": 16, "model": 1}), cause_with({"stopped": 4}),
           cause_with({}), cause_with({"infra": 16}, age_s=3600), cause_with({"infra": 16}, summary=False),
           analysis_failure_cause(rep1)]
    ok(got == ["infra", None, None, None, None, None, None],
       f"T61c infra only, in a summary written during this pipeline run, is resumable; a model or stopped row, no "
       f"failed row, an older summary, no summary, or no run dir is not ({got})")
    # T62 (F6 preview, 2026-11-11): the joint calibration's bench batch lost one task to infra, in a run dir restored
    # from a checkpoint (every mtime reset); "this pipeline run's" summaries are known by the pipeline run id
    r = os.path.join(td, "restored")
    for sub in ("models", "analysis/batch", "analysis/joint_calibration/call2"):
        os.makedirs(os.path.join(r, sub), exist_ok=True)
    rep2 = dict(rep1, pipeline_run="P1")
    files = {"models/pipeline_report.json": rep2,
             "analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json":
                 {"pipeline_run": "P1", "n": 42, "n_failed": 1, "failed_by_kind": {"infra": 1}},
             "analysis/joint_calibration/call2/points.jsonl.summary.json":
                 {"pipeline_run": "P1", "n": 64, "n_failed": 0, "failed_by_kind": {}},
             "analysis/batch/results.jsonl.summary.json":            # an earlier pipeline run's: never read now
                 {"pipeline_run": "P0", "n": 8, "n_failed": 1, "failed_by_kind": {"model": 1}}}

    def restored(report_age=0.0, **over):
        for rel, obj in {**files, **over}.items():
            json.dump(obj, open(os.path.join(r, rel), "w"))
            os.utime(os.path.join(r, rel), (1e9, 1e9))
        os.utime(os.path.join(r, "models", "pipeline_report.json"), (1e9 + report_age, 1e9 + report_age))
        return analysis_failure_cause(rep2, r)
    got2 = [restored(), restored(report_age=3600.0),
            restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json":
                        {"pipeline_run": "P1", "n": 42, "n_failed": 1, "failed_by_kind": {"model": 1}}})]
    # T62b (Codex on #200): an optional batch (a sensitivity run under its own subdirectory) that lost rows to infra
    # does not make a model rejection resumable; only the primary joint-calibration batches and analysis/batch count
    ok_bench = {"pipeline_run": "P1", "n": 42, "n_failed": 0, "failed_by_kind": {}}
    sens = os.path.join(r, "analysis", "joint_calibration", "sensitivity_s1", "call1")
    os.makedirs(sens, exist_ok=True)
    json.dump({"pipeline_run": "P1", "n": 9, "n_failed": 2, "failed_by_kind": {"infra": 2}},
              open(os.path.join(sens, "points.jsonl.summary.json"), "w"))
    optional = restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json": ok_bench})
    os.remove(os.path.join(sens, "points.jsonl.summary.json"))
    # T62c (Codex on #200): when the report has a pipeline run id, an untagged summary never counts, however new
    untagged = os.path.join(r, "analysis", "batch", "old.jsonl.summary.json")
    json.dump({"n": 4, "n_failed": 1, "failed_by_kind": {"infra": 1}}, open(untagged, "w"))
    old_only = restored(**{"analysis/joint_calibration/bench_call3_phase1.jsonl.summary.json": ok_bench})
    os.utime(untagged, None)
    old_only_fresh = analysis_failure_cause(rep2, r)
    os.remove(untagged)
    ok(optional is None and old_only is None and old_only_fresh is None,
       f"T62b-c an infra-only optional (sensitivity) batch, or an untagged summary when the report has a run id, "
       f"never makes the failure resumable: {optional} {old_only} {old_only_fresh}")
    st2 = RunState(question="q", state="FINAL", status="run_failed",
                   history=[{"from": "ANALYZE", "to": "FINAL", "why": "analysis failed"}])
    restored()
    ok(got2 == ["infra", "infra", None] and resumable_failure(r, st2) == "infra",
       f"T62 a restored run dir whose joint-calibration bench batch lost one task to infra is resumable 'infra' "
       f"(summaries matched by pipeline run id, not mtimes; an earlier run's model failure is ignored; a model "
       f"failure in this run is not resumable), so --resume reopens it at ANALYZE: {got2}")
