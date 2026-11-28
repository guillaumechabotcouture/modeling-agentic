"""The modeling pipeline's command line: runs the v2 stage machine (agents/stage_machine.py).

Pipeline v1, the lead session, was retired by the owner on 2026-11-20 and its code removed
(docs/design/v1-retirement.md). `--v1` and the resume of a v1 run refuse; the flags its launch configs
passed (`--max-sessions`, `--signoff-hours`, ...) are still accepted so no launch command changes."""

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from agents import enforcement  # noqa: E402
from agents.run_budget import (  # noqa: E402,F401  re-exported: the self-test's BG cases
    BUDGET_LEDGER,
    load_budget_ledger,
    charge_session,
    write_budget_ledger,
    remaining_budget,
)
from agents.run_finish import exit_code_for, plain_tag, store_run_results  # noqa: E402

REPO_ROOT = enforcement.REPO_ROOT
PREFLIGHT_RETIRED = ("--preflight-only ran the v1 lead session's live preflight, retired with pipeline v1 "
                     "(owner, 2026-11-20); v2 runs its own checks")
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "lib"))
import pipeline_id  # noqa: E402  which pipeline runs (v2 by default) and the identity every run records


def slugify(text: str, max_len: int = 40) -> str:
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    return slug[:max_len].rstrip('-')


def create_run_dir(question: str) -> str:
    runs_root = os.path.join(REPO_ROOT, "runs")
    os.makedirs(runs_root, exist_ok=True)
    # Phase 11 Commit η (F5): seconds-granularity timestamp + collision
    # detection. Previously: minute granularity + os.makedirs(...,
    # exist_ok=True) silently reused the dir, then metadata.json was
    # rewritten with mode "w", clobbering the prior run's record.
    # Now: if `{ts}_{slug}` exists, append `-2`, `-3`, ... until a
    # free name is found, and write metadata.json with mode "x"
    # (exclusive create) so any latent race surfaces as
    # FileExistsError rather than silent overwrite.
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = slugify(question)
    # baseline hb3 §5.1: replicates on separate VMs started in the same second and took one name (the check below is
    # local to each VM). A cloud replicate's name carries its label; its launch id goes into metadata.json.
    label, launch = plain_tag(os.environ.get("MA_RUN_LABEL")), plain_tag(os.environ.get("MA_LAUNCH_ID"))
    base_name = f"{timestamp}_{slug}" + (f"_{label}" if label else "")
    run_name = base_name
    suffix = 2
    while os.path.exists(os.path.join(runs_root, run_name)):
        run_name = f"{base_name}-{suffix}"
        suffix += 1
    run_path = os.path.join(runs_root, run_name)
    os.makedirs(run_path)  # no exist_ok — we know it's free
    os.makedirs(os.path.join(run_path, "data"), exist_ok=True)
    os.makedirs(os.path.join(run_path, "figures"), exist_ok=True)

    metadata = {
        "question": question,
        "started": datetime.now().isoformat(),
        # Phase 42 ε: trace times are naive local times; this is their zone.
        "utc_offset_s": int(datetime.now().astimezone().utcoffset().total_seconds()),
        "run_dir": run_name,
        **({"run_label": label} if label else {}),
        **({"launch_id": launch} if launch else {}),
    }
    with open(os.path.join(run_path, "metadata.json"), "x") as f:
        json.dump(metadata, f, indent=2)

    with open(os.path.join(run_path, "progress.md"), "w") as f:
        f.write(f"# Progress\n\n## Question: {question}\n\n## Completed Stages:\n\n## Notes:\n")

    return run_name


def _self_test_replicates(ok, outcome, rs, crash, raise_) -> None:
    """SN1-SN3, RD1, SC1-SC3 (baseline hb3: dengue R1 and R2 started in the same second, got one run dir name and one
    stored blob, and R2's upload overwrote R1's; flu R1 crashed or paused with no status in metadata.json)."""
    import shutil
    import tempfile

    class _Exists(Exception):
        pass
    _Exists.__name__ = "ResourceExistsError"      # what Azure's upload_blob(overwrite=False) raises

    class _XS:       # a store that, like Azure, overwrites a blob only when told to
        def __init__(self):
            self.st = {}

        def get_blob_client(self, c, n):
            st = self.st

            class _B:
                def upload_blob(self, data, overwrite=False, **kw):
                    if not overwrite and (c, n) in st:
                        raise _Exists(n)
                    st[(c, n)] = data.read() if hasattr(data, "read") else bytes(data)

                def download_blob(self, **kw):
                    v = st[(c, n)]
                    return type("D", (), {"readall": lambda _s: v})()
            return _B()
    keys = ("MA_RUN_LABEL", "MA_LAUNCH_ID", "MA_STORAGE_PREFIX")
    env0 = {k: os.environ.get(k) for k in keys}
    root, xs, made = tempfile.mkdtemp(), _XS(), []

    def mk(where, name="2026-11-23_233523_q"):
        p = os.path.join(root, where, name)
        os.makedirs(p)
        open(os.path.join(p, "report.md"), "w").write(where)
        json.dump({}, open(os.path.join(p, "metadata.json"), "w"))
        return p
    try:
        os.environ.update(MA_STORAGE_PREFIX="t", MA_LAUNCH_ID="L1")
        got = {}
        for lb in ("R1", "R2"):
            os.environ["MA_RUN_LABEL"] = lb
            got[lb] = store_run_results(mk(lb), service=xs)
        ok(got == {lb: f"outputs/t/launch_L1/run_{lb}_2026-11-23_233523_q.tar.gz" for lb in ("R1", "R2")}
           and len(xs.st) == 2, f"SN1 two replicates whose run dirs share a name are stored under their launch and "
                                f"label, one blob each ({got})")
        for k in ("MA_RUN_LABEL", "MA_LAUNCH_ID"):
            os.environ.pop(k)
        a, b = mk("A"), mk("B")
        ba = store_run_results(a, service=xs)
        first = xs.st.get(("outputs", "t/2026-11-23_233523_q.tar.gz"))
        bb = store_run_results(b, service=xs)
        mb = json.load(open(os.path.join(b, "metadata.json")))
        ok(ba == "outputs/t/2026-11-23_233523_q.tar.gz" and first and bb and bb != ba
           and xs.st[("outputs", ba[len("outputs/"):])] == first and mb.get("stored") == bb
           and mb.get("store_collision") == ba,
           f"SN2 a name another run's blob holds is refused, never overwritten; the run is stored under a fresh "
           f"name, and the collision recorded ({ba}, {bb}, {mb.get('store_collision')})")
        open(os.path.join(a, "report.md"), "w").write("A, resumed")
        ok(store_run_results(a, service=xs) == ba and xs.st[("outputs", ba[len("outputs/"):])] != first,
           "SN3 the same run stored again (a resume) replaces its own blob")
        n0 = len(xs.st)
        open(os.path.join(b, "report.md"), "w").write("B, resumed")
        ok(store_run_results(b, service=xs) == bb and len(xs.st) == n0,
           "SN4 (Codex r1) a run stored under a fresh name after a collision keeps that name on a resume")
        m_ = json.load(open(os.path.join(a, "metadata.json")))
        json.dump(dict(m_, stored_account="another"), open(os.path.join(a, "metadata.json"), "w"))
        keep = xs.st[("outputs", ba[len("outputs/"):])]
        open(os.path.join(a, "report.md"), "w").write("A, resumed again")
        ok(store_run_results(a, service=xs) != ba and xs.st[("outputs", ba[len("outputs/"):])] == keep,
           "SN5 (Codex r1) a receipt from another storage account never authorizes an overwrite")
        os.environ.update(MA_RUN_LABEL="R1", MA_LAUNCH_ID="L9")
        made.append(create_run_dir("rd replicate question"))
        os.environ["MA_RUN_LABEL"] = "R2"
        made.append(create_run_dir("rd replicate question"))
        m2 = json.load(open(os.path.join(REPO_ROOT, "runs", made[1], "metadata.json")))
        ok(made[0] != made[1] and made[0].endswith("_R1") and made[1].endswith("_R2")
           and m2.get("run_label") == "R2" and m2.get("launch_id") == "L9",
           f"RD1 a replicate's run dir carries its label, and metadata.json its label and launch ({made})")
    finally:
        for k, v in env0.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for n in made:
            shutil.rmtree(os.path.join(REPO_ROOT, "runs", n), ignore_errors=True)
    # SC1-SC3: a crash and an interrupt record their status; a status the machine wrote is never replaced
    def finished_then_crash():          # the machine wrote a terminal status in this invocation, then something raised
        json.dump({"status": "completed"}, open(os.path.join(d, "metadata.json"), "w"))
        crash()
    for before, fn, want in (({"status": "paused:awaiting_signoff"}, crash, "crashed"),
                             ({}, raise_(KeyboardInterrupt()), "interrupted"),
                             ({}, finished_then_crash, "completed"),
                             ({"status": "crashed"}, raise_(KeyboardInterrupt()), "interrupted")):
        d = tempfile.mkdtemp(dir=root)
        json.dump(before, open(os.path.join(d, "metadata.json"), "w"))
        outcome(lambda: rs(fn, d, store=lambda rp: None))
        m = json.load(open(os.path.join(d, "metadata.json")))
        ok(m.get("status") == want and m.get("crash"),
           f"SC {before or 'no status'}: a run ended by an exception records status {want} and the exception "
           f"({m.get('status')}, {m.get('crash')})")
    shutil.rmtree(root, ignore_errors=True)


def _self_test_pipeline(ok, _outcome) -> None:
    """PV (2026-11-05): an eval re-run meant to test a v2 fix ran v1, because main.py defaulted to
    v1 and the eval config passed no pipeline flag, and no record said which pipeline ran."""
    import tempfile
    g = globals()
    calls: list = []
    # PV7d: the lead session's entry point is gone with v1 (checked before any stub is installed).
    ok("run" not in g, "PV7d main.py has no lead-session entry point (main.run)")
    saved = {k: g.get(k) for k in ("main_v2", "create_run_dir", "store_run_results")}
    saved_argv = sys.argv

    def fake_v2(args, ident=None):
        calls.append(("v2", ident))
        return 0

    def drive(*argv):
        calls.clear()
        sys.argv = ["main.py", *argv]
        try:
            return main()
        except SystemExit as e:
            return ("exit", e.code)

    try:
        g["main_v2"] = fake_v2
        rc = drive("--signoff", "lead", "--max-budget-usd", "35", "Q?")   # the eval config's flags that day
        ok(rc == 0 and [c[0] for c in calls] == ["v2"], f"PV1 no pipeline flag runs v2 ({rc}, {calls})")
        ok(calls and calls[0][1] and calls[0][1].get("pipeline") == "v2"
           and calls[0][1].get("launcher") == "direct" and calls[0][1].get("commit"),
           f"PV2 the v2 run gets its identity: pipeline, commit, launcher ({calls})")
        rc = drive("--v2", "--launcher", "run_cluster:x", "Q?")
        ok(rc == 0 and calls[:1] and calls[0][0] == "v2" and calls[0][1]["launcher"] == "run_cluster:x",
           f"PV3 --v2 is a no-op alias; --launcher is recorded ({calls})")
        # PV4 (v1 retired, owner 2026-11-20): --v1 is refused before anything runs, preflight included.
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = drive("--v1", "Q?")
        ok(rc == 2 and not calls and pipeline_id.RETIRED_MESSAGE in err.getvalue(),
           f"PV4 --v1 is refused with the retirement message ({rc}, {calls}, {err.getvalue()[-120:]!r})")
        rc = drive("--v1", "--v2", "Q?")
        ok(rc == 2 and not calls, f"PV5 --v1 with --v2 is refused before anything runs ({rc})")
        with tempfile.TemporaryDirectory() as td:
            rp = os.path.join(td, "r1")
            os.makedirs(rp)
            json.dump({"question": "Q?", "pipeline": "v1"}, open(os.path.join(rp, "metadata.json"), "w"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = drive("--resume", rp)
            ok(rc == 1 and not calls and pipeline_id.RETIRED_MESSAGE in err.getvalue(),
               f"PV6 a run recorded as v1 is refused as retired ({rc}, {calls}, {err.getvalue()[-120:]!r})")
            rc = drive("--v1", "--skip-preflight", "--resume", rp)
            ok(rc == 2 and not calls, f"PV7 ... and not resumed with --v1 either ({rc})")
            os.remove(os.path.join(rp, "metadata.json"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = drive("--v2", "--resume", rp)
            ok(rc == 1 and not calls and pipeline_id.RETIRED_MESSAGE in err.getvalue(),
               f"PV7b a run with no record and no run_state.json (v1's files) is refused as retired ({rc}, "
               f"{err.getvalue()[-160:]!r})")
            # PV7c (Codex P2): a retired run is refused as retired before the eval isolation check.
            json.dump({"question": "Q?", "pipeline": "v1", "eval_question": True}, open(os.path.join(rp, "metadata.json"), "w"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = drive("--resume", rp)
            ok(rc == 1 and not calls and pipeline_id.RETIRED_MESSAGE in err.getvalue(),
               f"PV7c a v1 eval run is refused as retired ({rc}, {err.getvalue()[-160:]!r})")
            # PV7e (Codex round 2): main_v2 called directly does not resume (or restamp) a run recorded as v1.
            from agents import stage_machine as _sm3
            _sm3.init_run(rp, "Q?")
            json.dump({"question": "Q?", "pipeline": "v1"}, open(os.path.join(rp, "metadata.json"), "w"))
            import argparse as _ap
            rc = saved["main_v2"](_ap.Namespace(resume=rp, question=None, cloud=False, max_rounds=1, signoff="lead",
                                                max_budget_usd=None, dev_unsandboxed_derivations=False))
            ok(rc == 1 and json.load(open(os.path.join(rp, "metadata.json"))).get("pipeline") == "v1",
               f"PV7e main_v2 refuses a run recorded as v1 and leaves its record alone ({rc})")
            # PV8: a real main_v2 (machine stubbed at run_v2) stamps metadata.json, and the machine's
            # init_run keeps the stamp instead of replacing metadata.json.
            g["main_v2"] = saved["main_v2"]
            from agents import stage_machine as _sm, v2_sdk_tasks as _vt
            sm_saved, vt_saved = _sm.run_v2, _vt.SdkTasks
            made = os.path.join(td, "runs_v2")

            def fake_create(question):
                os.makedirs(made)
                json.dump({"question": question, "run_dir": "runs_v2"}, open(os.path.join(made, "metadata.json"), "w"))
                return made

            def fake_run_v2(question, run_dir, tasks, **kw):
                _sm.init_run(run_dir, question)
                return "completed"
            g["create_run_dir"], g["store_run_results"] = fake_create, (lambda p: None)
            _sm.run_v2, _vt.SdkTasks = fake_run_v2, (lambda *a, **k: None)
            try:
                rc = drive("--launcher", "vm_job:n/j", "Q2?")
            finally:
                _sm.run_v2, _vt.SdkTasks = sm_saved, vt_saved
            m = json.load(open(os.path.join(made, "metadata.json"))) if os.path.exists(made) else {}
            ok(rc == 0 and m.get("pipeline") == "v2" and m.get("launcher") == "vm_job:n/j" and m.get("commit")
               and m.get("run_dir") == "runs_v2" and m.get("invocations"),
               f"PV8 a v2 run's metadata.json records pipeline, commit and launcher ({rc}, {m})")
        # PV10 (owner, 2026-11-20): --preflight-only ran the v1 lead session's live preflight, retired with v1. It is
        # refused with one message before anything runs; v2's own checks are not involved.
        for argv in (("--preflight-only",), ("--preflight-only", "Q?"), ("--preflight-only", "--cross-model")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = drive(*argv)
            ok(rc == 2 and not calls and PREFLIGHT_RETIRED in err.getvalue(),
               f"PV10 {argv} is refused with the retirement message, nothing runs ({rc}, {calls}, {err.getvalue()[-120:]!r})")
        import importlib.util as _ilu
        ok(_ilu.find_spec("agents.run_preflight") is None and _ilu.find_spec("agents.run_session") is None,
           "PV10c the v1 lead-session preflight modules are gone (agents/run_preflight.py, agents/run_session.py)")
        # PV11 (Codex r1 P1): a run whose metadata.json lost its pipeline is judged by its files (run_state.json
        # is v2's), and a malformed value is refused: a v2 run is never resumed with --v1.
        with tempfile.TemporaryDirectory() as td:
            rp = os.path.join(td, "r2")
            os.makedirs(rp)
            json.dump({"question": "Q?"}, open(os.path.join(rp, "metadata.json"), "w"))
            from agents import stage_machine as _sm2
            _sm2.init_run(rp, "Q?")            # a readable run state (Codex on the retirement PR: {} is unreadable)
            g["main_v2"] = fake_v2              # PV8 restored the real one
            json.dump({"question": "Q?"}, open(os.path.join(rp, "metadata.json"), "w"))
            rc = drive("--v1", "--skip-preflight", "--resume", rp)
            ok(rc == 2 and not calls, f"PV11 a v2 run with no recorded pipeline is not resumed with --v1 ({rc}, {calls})")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = drive("--resume", rp)
            ok(rc == 1 and not calls and "records no pipeline" in err.getvalue() and "--v2" in err.getvalue(),
               f"PV11c (Codex r2) ... nor on the default: a run with no recorded pipeline needs "
               f"an explicit flag ({rc}, {calls}, {err.getvalue()[-160:]!r})")
            from agents import stage_machine as _sm4, v2_sdk_tasks as _vt4
            got_run: list = []
            sm4, vt4 = _sm4.run_v2, _vt4.SdkTasks
            _sm4.run_v2 = lambda question, run_dir, tasks, **kw: got_run.append((question, run_dir)) or "completed"
            _vt4.SdkTasks = lambda *a, **k: None
            g["main_v2"] = saved["main_v2"]
            try:
                rc = drive("--v2", "--resume", rp)
            finally:
                _sm4.run_v2, _vt4.SdkTasks = sm4, vt4
                g["main_v2"] = fake_v2
            ok(rc == 0 and got_run == [("Q?", rp)],
               f"PV11d ... and the real main_v2 resumes it with an explicit --v2: its directory and saved question "
               f"({rc}, {got_run})")
            # PV11e: --preflight-only with --resume is refused like any --preflight-only, and resumes nothing.
            json.dump({"question": "Q?"}, open(os.path.join(rp, "metadata.json"), "w"))   # unrecorded again
            rc = drive("--preflight-only", "--resume", rp)
            ok(rc == 2 and not calls, f"PV11e --preflight-only --resume is refused, nothing resumes ({rc}, {calls})")
            json.dump({"question": "Q?", "pipeline": "V2!"}, open(os.path.join(rp, "metadata.json"), "w"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = drive("--resume", rp)
            ok(rc == 1 and not calls and "is not v1 or v2" in err.getvalue(),
               f"PV11b a malformed recorded pipeline is refused ({rc}, {calls}, {err.getvalue()[-160:]!r})")
    finally:
        for k, v in saved.items():
            g[k] = v
        sys.argv = saved_argv


def _run_self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    # EV1-EV4 (port of main into v2): the eval-workspace refusal covers --v2 runs, and a v2
    # resume is checked on the question it will use (run_state.json), not the argument.
    with tempfile.TemporaryDirectory() as td:
        from types import SimpleNamespace as _NS
        from agents import stage_machine as _sm
        _evq = "SYNTHETIC TEST QUESTION (evaluation suite): allocate X"
        _prev_iso = os.environ.pop("MA_EVAL_ISOLATED", None)
        try:
            ok(eval_refusal(_NS(question=_evq, resume=None, v2=True), REPO_ROOT) is not None,
               "EV1 a --v2 eval question is refused off the cloud worker")
            ok(eval_refusal(_NS(question="How should X allocate?", resume=None, v2=True), REPO_ROOT) is None,
               "EV2 an ordinary --v2 question is not refused")
            rv = os.path.join(td, "v2resume")
            os.makedirs(rv, exist_ok=True)
            _sm.save_state(rv, _sm.RunState(question=_evq))
            ok(eval_refusal(_NS(question="benign", resume=rv, v2=True), REPO_ROOT) is not None,
               "EV3 a --v2 resume is checked on run_state.json's question, whatever the argument says")
            ok(eval_refusal(_NS(question="benign", resume=rv, v2=False, v1=False), REPO_ROOT) is not None,
               "EV4 v2 is the default: a resume with no pipeline flag is checked on run_state.json too")
        finally:
            if _prev_iso is not None:
                os.environ["MA_EVAL_ISOLATED"] = _prev_iso

    # V1: environment filtering
    env = {"ANTHROPIC_API_KEY": "a", "AZ_BATCH_ACCOUNT": "b", "OPENAI_API_KEY": "c", "PATH": "/bin"}
    ov = enforcement.filtered_env(env, allow_cloud=False)
    ok({k: v for k, v in ov.items() if k not in ("PATH", "VIRTUAL_ENV") and "CACHE_TTL" not in k}
       == {"AZ_BATCH_ACCOUNT": "", "OPENAI_API_KEY": ""}, f"V1 env filter: {ov}")
    ok(ov.get("CLAUDE_CODE_PROMPT_CACHE_TTL") == "1h" and ov.get("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL") == "1h"
       and enforcement.filtered_env({**env, "MA_PROMPT_CACHE_TTL": "5m"}, False)["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "5m",
       f"V1b (Phase 31) 1-hour prompt cache for the lead and subagents, 5m on request: {ov}")
    ok("AZ_BATCH_ACCOUNT" not in enforcement.filtered_env(env, allow_cloud=True),
       "V1 cloud creds kept with --cloud")
    # V2 (Phase 21 α): agents' PATH starts with this interpreter's bin/,
    # so bare `python3` is the harness venv even when it wasn't activated.
    ok(ov["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable)
       and ov["PATH"].endswith("/bin"), f"V2 PATH not venv-first: {ov['PATH']}")
    venv_ov = enforcement.interpreter_env({"PATH": "/usr/bin"},
                                          os.path.join(REPO_ROOT, ".venv", "bin", "python3"))
    ok(venv_ov.get("VIRTUAL_ENV") == os.path.join(REPO_ROOT, ".venv"),
       f"V2 VIRTUAL_ENV not set for a venv interpreter: {venv_ov}")
    # BG1 (Phase 23 β): run-wide budget cap (agents/run_budget.py, shared with v2's tasks).
    ok(remaining_budget(None, [{"total_cost_usd": 5}]) is None
       and remaining_budget(300, [{"total_cost_usd": 120.5}, {"total_cost_usd": None},
                                  {"total_cost_usd": 79.5}]) == 100
       and remaining_budget(300, [{"total_cost_usd": 310}]) == -10 and exit_code_for("budget_exhausted") == 2,
       "BG1 remaining budget sums sessions; budget stop is a partial status (exit 2)")
    # BG2 (Codex, PR #15): an unreconciled session is charged its whole
    # allowance; the ledger is harness-owned and survives a resume.
    with tempfile.TemporaryDirectory() as td:
        R = lambda c, sid="s1": type("R", (), {"total_cost_usd": c, "session_id": sid})()
        led0 = {"sessions": []}
        charge_session(led0, 1, R(4.0), 10.0)
        charge_session(led0, 2, R(6.5), 6.0)       # resumed: cumulative 6.5 → charge 2.5
        ok(remaining_budget(10.0, led0["sessions"]) == 3.5,
           f"BG2a resumed session charged the increase only: {led0}")
        led = load_budget_ledger(td)
        charge_session(led, 1, R(4.0), 5.0)
        charge_session(led, 2, None, remaining_budget(5.0, led["sessions"]))
        write_budget_ledger(td, led, 5.0)
        again = load_budget_ledger(td)
        ok(remaining_budget(5.0, again["sessions"]) == 0 and again["spent_usd"] == 5.0
           and again["sessions"][1]["source"].startswith("unreconciled"),
           f"BG2 disconnect charged in full; ledger reloads: {again}")
        mig = {"sessions": [{"session": 0, "session_id": "s1", "reported_usd": 4.0,
                             "total_cost_usd": 4.0, "source": "migrated"}]}
        charge_session(mig, 1, R(6.0), 6.0)
        ok(remaining_budget(10.0, mig["sessions"]) == 4.0,
           f"BG2c migrated spend + resumed cumulative result is not double-counted: {mig}")
    # ST1 (Phase 26): a finished run uploads itself (never _truth/), when configured.
    class _FB:
        def __init__(self, st, k): self.st, self.k = st, k
        def upload_blob(self, data, overwrite=False): self.st[self.k] = bytes(data)
        def download_blob(self):
            v = self.st[self.k]
            return type("D", (), {"readall": lambda _s: v})()

    class _FS:
        def __init__(self): self.st = {}
        def get_blob_client(self, c, n): return _FB(self.st, (c, n))
    _sr = tempfile.mkdtemp()
    _run = os.path.join(_sr, "2026-10-24_000000_x")
    os.makedirs(os.path.join(_run, "_truth"))
    open(os.path.join(_run, "report.md"), "w").write("r")
    open(os.path.join(_run, "_truth", "answers.yaml"), "w").write("secret")
    open(os.path.join(_run, ".env.local"), "w").write("TOKEN=x")
    open(os.path.join(_run, "trace.jsonl"), "w").write("key sk-ant-TESTSECRET12345 leaked\n")
    _saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-TESTSECRET12345"
    _fs = _FS()
    _blob = store_run_results(_run, service=_fs)
    import io as _io2, tarfile as _tf2
    _names = _tf2.open(fileobj=_io2.BytesIO(_fs.st.get(("outputs", "runs/2026-10-24_000000_x.tar.gz"), b"")),
                       mode="r:gz").getnames() if _blob else []
    if _saved_key is None:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        os.environ["ANTHROPIC_API_KEY"] = _saved_key
    _tr = _tf2.open(fileobj=_io2.BytesIO(_fs.st.get(("outputs", "runs/2026-10-24_000000_x.tar.gz"), b"")), mode="r:gz") \
        if _blob else None
    _trace = _tr.extractfile("2026-10-24_000000_x/trace.jsonl").read() if _tr else b""
    ok(_blob and any(n.endswith("report.md") for n in _names) and not any("_truth" in n or ".env" in n for n in _names)
       and b"TESTSECRET" not in _trace,
       f"ST1 run stored without _truth/ or .env*, API keys redacted: {_names}")
    ok(json.load(open(os.path.join(_run, "metadata.json"))).get("stored") == _blob,
       "ST1 metadata.json records where the run was stored")
    ok(store_run_results(_run) is None or os.environ.get("MA_STORAGE_ACCOUNT"),
       "ST1 nothing is stored when no account is configured")
    shutil.rmtree(_sr, ignore_errors=True)
    # ST2 (2026-11-02 v2 run crashed in FIX_LOOP, stored=None): a v2 run that raises is still stored,
    # with the crash recorded in metadata.json, and the exception still propagates (exit 1).
    _rs = globals().get("run_v2_and_store")
    _cr = tempfile.mkdtemp()
    _stored_calls = []

    def _crash():
        raise KeyError('"tasks"')

    def _fake_store(rp):
        _stored_calls.append(json.load(open(os.path.join(rp, "metadata.json"))).get("crash"))
        return "outputs/runs/x.tar.gz"
    _raised = False
    if _rs is not None:
        try:
            _rs(_crash, _cr, store=_fake_store)
        except KeyError:
            _raised = True
    ok(_rs is not None and _raised and len(_stored_calls) == 1 and "KeyError" in (_stored_calls[0] or ""),
       f"ST2 a crashed v2 run is stored with its crash recorded, and the crash still propagates ({_stored_calls})")
    ok(_rs is not None and _rs(lambda: "completed", _cr, store=lambda rp: _stored_calls.append("ok")) == "completed"
       and _stored_calls[-1] == "ok", "ST2b a finished v2 run is stored once and its status returned")

    # ST3-ST6 (Codex #130 P2): finalization never replaces the run's own exception or its exit code.
    def _outcome(fn):
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 -- the exception type is the finding
            return type(e).__name__
        return None

    def _raise(exc):
        def f(*a, **k):
            raise exc
        return f
    for _bad in ("[]", "null", '"x"'):
        open(os.path.join(_cr, "metadata.json"), "w").write(_bad)
        _calls = []
        _got = _outcome(lambda: _rs(_crash, _cr, store=lambda rp: _calls.append(rp)))
        ok(_got == "KeyError" and len(_calls) == 1,
           f"ST3 metadata.json holding {_bad}: the run's KeyError survives and the run is still stored ({_got}, {_calls})")
    open(os.path.join(_cr, "metadata.json"), "w").write("{}")
    _got = _outcome(lambda: _rs(_crash, _cr, store=_raise(AttributeError("store broke"))))
    ok(_got == "KeyError", f"ST4 a store that raises does not replace the run's exception ({_got})")
    _got = _outcome(lambda: _rs(_raise(KeyboardInterrupt()), _cr, store=_raise(AttributeError("store broke"))))
    ok(_got == "KeyboardInterrupt", f"ST5 an interrupt survives a store that raises ({_got})")
    ok(_outcome(lambda: _rs(lambda: "completed", _cr, store=_raise(RuntimeError("x")))) is None
       and _rs(lambda: "completed", _cr, store=_raise(RuntimeError("x"))) == "completed",
       "ST5b a store failure after a finished run is logged, not raised; the status is returned")
    _saved_mv2, _saved_argv = globals().get("main_v2"), sys.argv
    globals()["main_v2"] = _raise(KeyboardInterrupt())
    sys.argv = ["main.py", "--v2", "Q?"]
    try:
        _code = _outcome(lambda: None) or main()
    except BaseException as e:  # noqa: BLE001
        _code = type(e).__name__
    finally:
        globals()["main_v2"], sys.argv = _saved_mv2, _saved_argv
    ok(_code == 130, f"ST5c an interrupted v2 run exits 130 ({_code})")
    _sr6 = tempfile.mkdtemp()
    _run6 = os.path.join(_sr6, "2026-11-02_000000_y")
    os.makedirs(_run6)
    open(os.path.join(_run6, "metadata.json"), "w").write("[]")
    _got = _outcome(lambda: store_run_results(_run6, service=_FS()))
    ok(_got is None and isinstance(json.load(open(os.path.join(_run6, "metadata.json"))), dict),
       f"ST6 store_run_results with a non-object metadata.json neither raises nor keeps the bad value ({_got})")
    shutil.rmtree(_sr6, ignore_errors=True)
    shutil.rmtree(_cr, ignore_errors=True)
    try:
        _self_test_replicates(ok, _outcome, _rs, _crash, _raise)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"SN/RD/SC: {type(e).__name__}: {e}")
    # ST7 (BE31): resuming a built-engine run whose run lock another process holds writes nothing:
    # no identity stamp, no crash record, no upload of the active run
    import argparse as _ap7
    import threading as _th7
    from agents import stage_machine as _sm7, v2_sdk_tasks as _vt7
    _lr = tempfile.mkdtemp()
    json.dump({"pipeline": "v2", "commit": "0" * 40, "launcher": "t"}, open(os.path.join(_lr, "metadata.json"), "w"))
    _st7 = _sm7.init_run(_lr, "Q?", signoff="lead")
    _st7.state = "ENGINE_BUILD"
    _sm7.save_state(_lr, _st7)
    _held, _done, _stored7 = _th7.Event(), _th7.Event(), []

    def _hold():
        with _sm7._run_lock(_lr):
            _held.set()
            _done.wait(60)
    _t7 = _th7.Thread(target=_hold)
    _t7.start()
    _held.wait(60)
    _snap7 = {f: open(os.path.join(_lr, f), "rb").read() for f in ("metadata.json", _sm7.STATE_FILE)}
    _saved7 = (globals()["store_run_results"], _vt7.SdkTasks)
    globals()["store_run_results"], _vt7.SdkTasks = (lambda p: _stored7.append(p)), (lambda *a, **k: None)
    try:
        _got7 = _outcome(lambda: _stored7.append(("rc", main_v2(_ap7.Namespace(
            resume=_lr, question=None, cloud=False, max_rounds=1, signoff="lead", max_budget_usd=None,
            dev_unsandboxed_derivations=False)))))
    finally:
        globals()["store_run_results"], _vt7.SdkTasks = _saved7
        _done.set()
        _t7.join(60)
    ok(_stored7 == [("rc", 1)] and _got7 is None
       and all(open(os.path.join(_lr, f), "rb").read() == b for f, b in _snap7.items()),
       f"ST7 a resume refused by a held run lock exits 1 with nothing stamped, recorded or stored ({_got7}, "
       f"{_stored7})")
    shutil.rmtree(_lr, ignore_errors=True)
    # ST8 (BE31): when run() raises RunLocked, run_v2_and_store records no crash and does not store the held run
    _p8, _calls8 = tempfile.mkdtemp(), []
    json.dump({"pipeline": "v2"}, open(os.path.join(_p8, "metadata.json"), "w"))

    def _locked():
        raise _sm7.RunLocked("held", run_dir=_p8)
    _got8 = _outcome(lambda: _rs(_locked, _p8, store=lambda p: _calls8.append(p)))
    ok(_got8 == "RunLocked" and _calls8 == []
       and "crash" not in _metadata_dict(os.path.join(_p8, "metadata.json")),
       f"ST8 a held run lock records no crash and stores nothing ({_got8}, {_calls8})")
    # ST9 (BE31): resuming a non-built-engine lead run does not take _run_lock
    _st9 = _sm7.init_run(_p8, "Q?", signoff="lead")
    _saved9 = (globals()["_main_v2_run"],)
    _ran9 = []
    globals()["_main_v2_run"] = lambda *a, **kw: (_ran9.append(a[2]) or 0)
    _held9, _rel9 = _th7.Event(), _th7.Event()

    def _hold9():
        with _sm7._run_lock(_p8):
            _held9.set()
            _rel9.wait(60)
    _t9 = _th7.Thread(target=_hold9)
    _t9.start()
    _held9.wait(60)
    try:
        _rc9 = main_v2(_ap7.Namespace(
            resume=_p8, question=None, cloud=False, max_rounds=1, signoff="lead", max_budget_usd=None,
            dev_unsandboxed_derivations=False))
    finally:
        globals()["_main_v2_run"] = _saved9[0]
        _rel9.set()
        _t9.join(60)
    ok(_rc9 == 0 and _ran9 == [_p8],
       f"ST9 a non-built-engine lead resume is not blocked by _run_lock ({_rc9}, {_ran9})")
    shutil.rmtree(_p8, ignore_errors=True)
    _self_test_pipeline(ok, _outcome)

    if failures:
        print("SELF-TEST FAILURES:", file=sys.stderr)
        for f_ in failures:
            print(f"  - {f_}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def eval_refusal(args, here: str) -> str | None:
    """Why this invocation may not run here (Phase 50 α eval isolation), or None. Checks the
    question the run will use: the argument, the resumed run's metadata.json, and for a v2
    resume (the default) also run_state.json (main_v2 takes its question from there, not the argument)."""
    lib = os.path.join(here, "scripts", "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from eval_paths import eval_workspace_problem, question_for
    resume_dir = None
    if args.resume:
        resume_dir = args.resume if os.path.isabs(args.resume) else os.path.join(os.getcwd(), args.resume)
    question, meta = question_for(args.question, resume_dir)
    candidates = [question]
    try:
        v2 = pipeline_id.select(getattr(args, "v1", False), getattr(args, "v2", False)) == "v2"
    except pipeline_id.PipelineError:
        v2 = True           # refused later; check every question the run could use
    if v2 and resume_dir:
        try:
            from agents import stage_machine
            st = stage_machine.load_state(resume_dir)
        except (OSError, ValueError, TypeError) as e:
            return f"{resume_dir}/run_state.json cannot be read ({type(e).__name__}): refusing"
        if st is not None:
            candidates.append(st.question)
    for q in candidates:
        why = eval_workspace_problem(q, here, meta)
        if why:
            return why
    return None


def _metadata_dict(path: str) -> dict:
    """metadata.json as a dict; {} when it is missing, unreadable, or valid JSON that is not an object."""
    try:
        meta = json.load(open(path)) if os.path.exists(path) else {}
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def run_v2_and_store(run, run_path: str, store=None):
    """Run the v2 machine (`run()` returns its status), then store the run directory, also when
    `run()` raises (ST2): like the lead pipeline's finalize, upload the run when a storage account is
    configured and record it in metadata.json, which the cloud launcher reads to stop the cluster.
    A crash (or an interrupt) is recorded in metadata.json (`crash`) before the upload, then re-raised.
    Finalization never raises: a failure there is logged, so the run's own exception and its exit
    code survive (ST3-ST5)."""
    store = store or store_run_results
    locked = False
    status0 = _metadata_dict(os.path.join(run_path, "metadata.json")).get("status")   # an earlier invocation's
    try:
        return run()
    except BaseException as e:
        # BE31: another process holds a built-engine run's run lock: not a crash of this run, and do not upload half-way
        if type(e).__name__ == "RunLocked":
            locked = True
            raise
        try:
            mp = os.path.join(run_path, "metadata.json")
            meta = _metadata_dict(mp)
            meta["crash"] = f"{type(e).__name__}: {e}"[:500]
            # every exit records its status (baseline hb3 §4.2): a terminal status this invocation's machine wrote
            # stands; none, a pause, or an earlier invocation's status (a resume) is replaced
            cur = meta.get("status")
            if not cur or cur == status0 or str(cur).startswith("paused:"):
                meta["status"] = "interrupted" if isinstance(e, KeyboardInterrupt) else "crashed"
                meta["crashed_at"] = datetime.now().isoformat(timespec="seconds")
                meta.pop("finished", None)
            os.makedirs(run_path, exist_ok=True)
            json.dump(meta, open(mp, "w"), indent=2)
        except Exception as fe:  # noqa: BLE001 -- never replace the run's exception
            print(f"[ERROR] crash not recorded in metadata.json: {type(fe).__name__}: {fe}", file=sys.stderr, flush=True)
        raise
    finally:
        try:
            if locked:
                print("v2 run not stored: another process holds its run lock", file=sys.stderr, flush=True)
            else:
                print(f"v2 run stored: {store(run_path)}", flush=True)
        except Exception as fe:  # noqa: BLE001 -- never replace the run's exception or status
            print(f"[ERROR] RUN RESULTS NOT STORED: {type(fe).__name__}: {fe}", file=sys.stderr, flush=True)


def main_v2(args, ident: dict | None = None) -> int:
    """v2 C: the stage machine drives the run; bounded agent tasks (agents/v2_sdk_tasks.py)
    do the judgement. Resumes from run_state.json with --resume. The lead session, its
    prompt and its flow hooks are not used. `ident` (pipeline_id.identity) goes into
    metadata.json before the machine starts."""
    from agents import stage_machine, v2_sdk_tasks
    ident = ident or pipeline_id.identity("v2", REPO_ROOT)
    if args.resume:
        run_path = args.resume if os.path.isabs(args.resume) else os.path.join(os.getcwd(), args.resume)
        why = pipeline_id.retired_resume(_metadata_dict(os.path.join(run_path, "metadata.json")), run_path)
        if why:                        # a run recorded as v1 is never resumed or restamped (retired)
            print(f"Error: {why}", file=sys.stderr)
            return 1
        st = stage_machine.load_state(run_path)
        if st is None:
            print(f"Error: {run_path} has no run_state.json: no v2 run state to resume")
            return 1
        question = st.question
    else:
        if not args.question:
            print("Error: question is required (or use --resume)")
            return 1
        question = args.question
        run_path = os.path.join(REPO_ROOT, "runs", create_run_dir(question))
    lock = contextlib.nullcontext()
    need_lock = args.resume and (
        st.state in ("ENGINE_BUILD", "ENGINE_ADMIT", "ENGINE_REVIEW", "ENGINE_SIGN")
        or bool(getattr(st, "engine_build", None))
        or stage_machine.spec_has_engine_build(run_path)
    )
    if need_lock:
        # BE31: lock is taken before anything is written (run_v2 holds it again, nested):
        # a resume another process holds stops here with nothing stamped, recorded or stored
        lock = stage_machine._run_lock(run_path)
        try:
            lock.__enter__()
        except stage_machine.RunLocked as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    try:
        return _main_v2_run(args, ident, run_path, question)
    finally:
        lock.__exit__(None, None, None)


def _main_v2_run(args, ident: dict, run_path: str, question: str) -> int:
    from agents import stage_machine, v2_sdk_tasks
    pipeline_id.stamp(run_path, ident)
    print(f"Run (v2): {run_path}", flush=True)
    derive_un = getattr(args, "dev_unsandboxed_derivations", False)
    engine_un = getattr(args, "dev_unsandboxed_engine", False)
    tasks = v2_sdk_tasks.SdkTasks(run_path, allow_cloud=args.cloud, unsandboxed_ok=engine_un)
    status = run_v2_and_store(lambda: stage_machine.run_v2(
        question, run_path, tasks, max_rounds=args.max_rounds, signoff=args.signoff,
        budget_usd=args.max_budget_usd, derive_unsandboxed=derive_un,
        engine_unsandboxed=engine_un,
        engine_build_usd=getattr(args, "engine_build_usd", None),
        acquire="auto" if getattr(args, "acquire", False) else "off"),
        run_path)
    print(f"v2 status: {status}", flush=True)
    if status.startswith("paused:"):
        return 2
    return exit_code_for(status)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a mathematical model for a research question"
    )
    parser.add_argument("question", nargs="?", help="The research question to model")
    parser.add_argument("--max-rounds", type=int, default=5,
                        help="Max critique-revision rounds (default: 5)")
    parser.add_argument("--max-sessions", type=int, default=10,
                        help="Accepted for existing launch configs; unused by v2 (it capped the retired v1 lead's sessions)")
    parser.add_argument("--resume", type=str, default=None,
                        help="Resume an existing run directory (e.g., runs/2026-04-07_1523_...)")
    parser.add_argument("--cloud", action="store_true",
                        help="Pass cloud credentials (AZ_*/AZURE_*) to agents so UQ "
                             "can run on Azure Batch. Off by default (Phase 21 α).")
    parser.add_argument("--cross-model", action="store_true",
                        help="Accepted for existing launch configs; unused by v2 (it added a cross-model critic to v1's "
                             "STAGE 6).")
    parser.add_argument("--max-budget-usd", type=float, default=None,
                        help="Hard dollar cap for the whole run (all sessions, including "
                             "earlier invocations of a resumed run). Stops with status "
                             "budget_exhausted; the final gate still runs.")
    parser.add_argument("--engine-build-usd", type=float, default=None,
                        help="Dollar cap for the agent-built engine lane (default $40, clamped to harness max); "
                             "pass a higher cap on --resume after an engine_build_cap budget pause.")
    parser.add_argument("--acquire", action="store_true",
                        help="Enable the v2 ACQUIRE state before SPEC to fetch public inputs from the allowlist "
                             "when the question supplies no files (off by default; docs/design/acquire.md).")
    parser.add_argument("--methods-critic", action="store_true",
                        help="Accepted for existing launch configs; unused by v2 (it added critique-methods to v1's STAGE 6).")
    parser.add_argument("--signoff", choices=["owner", "lead", "none"], default="owner",
                        help="Who signs the decision spec before compute: the owner (default; the run pauses at SIGN), "
                             "the lead (unattended test runs), or none")
    parser.add_argument("--signoff-hours", type=float, default=24.0,
                        help="Accepted for existing launch configs; unused by v2 (it timed the retired v1 sign-off wait)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run inline self-tests (no API calls) and exit")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Refused: it ran the v1 lead session's live preflight, retired with v1 (owner, 2026-11-20).")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Accepted for existing launch commands; v2 runs no preflight before a run")
    parser.add_argument("--v1", action="store_true",
                        help="Refused: pipeline v1 is retired (owner, 2026-11-20); use v2 (the default).")
    parser.add_argument("--v2", action="store_true",
                        help="Run the v2 stage machine (the default; accepted so existing commands work).")
    parser.add_argument("--launcher", default=None,
                        help="Name of the launcher that started this run, recorded in metadata.json "
                             "(default: $MA_LAUNCHER, else 'direct').")
    parser.add_argument("--dev-unsandboxed-derivations", action="store_true",
                        help="v2 development only: run the spec's data derivations without a sandbox where there is "
                             "none (macOS). The record says sandbox none and the run cannot be accepted "
                             "(docs/design/v2-derived-inputs.md).")
    parser.add_argument("--dev-unsandboxed-engine", action="store_true",
                        help="v2 development only: execute agent-built engine code without bubblewrap where there is "
                             "no sandbox (macOS). Persists a HIGH non-declarable gate finding so the run cannot be "
                             "accepted (docs/design/agent-built-engines.md §5.1).")
    args = parser.parse_args()

    if args.self_test:
        return _run_self_test()
    try:
        pipeline = pipeline_id.select(args.v1, args.v2)
    except pipeline_id.PipelineError as e:    # --v1: pipeline v1 is retired (owner, 2026-11-20)
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    if args.preflight_only:      # the v1 lead session's preflight, retired with it (owner, 2026-11-20)
        print(f"REFUSED: {PREFLIGHT_RETIRED}", file=sys.stderr)
        return 2
    # Phase 50 α: an eval-suite question runs only on a cloud worker (its work directory is
    # the launch archive: no evals/, no .git), so agents cannot reach answer keys by any
    # route. Checked on the question the run will actually use, including --resume, and
    # before the v2 dispatch so --v2 runs get the same refusal.
    _rp = None
    if args.resume:   # a run recorded as v1 is never resumed (retired): said before any other refusal
        _rp = args.resume if os.path.isabs(args.resume) else os.path.join(os.getcwd(), args.resume)
        why = pipeline_id.retired_resume(_metadata_dict(os.path.join(_rp, "metadata.json")), _rp)
        if why:
            print(f"Error: {why}", file=sys.stderr)
            return 1
    why = eval_refusal(args, os.path.dirname(os.path.abspath(__file__)))
    if why:
        print(f"REFUSED: {why}", file=sys.stderr)
        return 1
    ident = pipeline_id.identity(pipeline, REPO_ROOT, args.launcher)
    print(f"Pipeline: {ident['pipeline']} | commit {ident['commit']} | launcher {ident['launcher']}", flush=True)
    if args.resume:   # a run continues under the pipeline it was recorded with
        why = pipeline_id.resume_conflict(_metadata_dict(os.path.join(_rp, "metadata.json")), pipeline, _rp,
                                          explicit_flag=bool(args.v1 or args.v2))
        if why:
            print(f"Error: {why}", file=sys.stderr)
            return 1
    try:
        return main_v2(args, ident)
    except KeyboardInterrupt:      # ST5c: as the lead pipeline, an interrupt exits 130
        return 130


if __name__ == "__main__":
    sys.exit(main())
