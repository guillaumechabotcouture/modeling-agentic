"""The modeling pipeline's command line: runs the v2 stage machine (agents/stage_machine.py).

Pipeline v1, the lead session, was retired by the owner on 2026-11-20 and its code removed
(docs/design/v1-retirement.md). `--v1` and the resume of a v1 run refuse; the flags its launch configs
passed (`--max-sessions`, `--signoff-hours`, ...) are still accepted so no launch command changes."""

import asyncio
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
from agents.integrity import ArtifactIntegrity  # noqa: E402,F401  (the self-test's IN/IC/IR cases)
from agents.run_budget import (  # noqa: E402,F401  re-exported: the self-test's BG cases
    BUDGET_LEDGER,
    load_budget_ledger,
    reconcile_conversation_cost,
    crashed_session_cost,
    charge_session,
    conversation_cost,
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
        json.dump({"status": "amended_provisional"}, open(os.path.join(d, "metadata.json"), "w"))
        crash()
    for before, fn, want in (({"status": "paused:awaiting_signoff"}, crash, "crashed"),
                             ({}, raise_(KeyboardInterrupt()), "interrupted"),
                             ({}, finished_then_crash, "amended_provisional"),
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
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, UserMessage

    failures: list[str] = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    run_path = os.path.join(REPO_ROOT, "runs", "selftest_run")
    lead = None  # agent_type for the lead
    # The agent names the enforcement policy was written for (the retired v1 registry); its cases stay
    # until that policy is removed (docs/design/v1-retirement.md, follow-ups).
    reg = {"planner", "data-agent", "modeler", "analyst", "critique-methods", "critique-domain", "critique-premortem",
           "critique-presentation", "critique-redteam", "critique-sufficiency", "writer"}

    def deny(tool, inp, agent_type=lead):
        return enforcement.check_tool_use(tool, inp, run_path, agent_type, reg)

    # E1: harness writes denied for every agent
    for at in (None, "modeler", "writer"):
        ok(deny("Write", {"file_path": os.path.join(REPO_ROOT, "scripts/x.py")}, at),
           f"E1: Write scripts/ allowed for {at}")
        ok(deny("Edit", {"file_path": "agents/modeler.py"}, at),
           f"E1: Edit agents/ allowed for {at}")
        ok(deny("Write", {"file_path": os.path.join(REPO_ROOT, "docs/lessons/observations.jsonl")}, at),
           f"E1: Write to the lessons ledger allowed for {at}")
        ok(deny("Edit", {"file_path": os.path.join(REPO_ROOT, ".claude/orchestration/effort_floors.yaml")}, at),
           f"E1: Edit .claude/ allowed for {at}")
        ok(deny("Write", {"file_path": os.path.join(REPO_ROOT, "main.py")}, at),
           f"E1: Write main.py allowed for {at}")
    # E2: lead may not write critique YAMLs or models/**; specialists may
    ok(deny("Write", {"file_path": f"{run_path}/critique_methods.yaml"}),
       "E2: lead Write critique yaml allowed")
    ok(deny("Edit", {"file_path": f"{run_path}/models/sensitivity_analysis.yaml"}),
       "E2: lead Edit models/ allowed")
    ok(not deny("Write", {"file_path": f"{run_path}/critique_methods.yaml"}, "critique-methods"),
       "E2: critique agent denied its own yaml")
    ok(not deny("Edit", {"file_path": f"{run_path}/models/outcome_fn.py"}, "modeler"),
       "E2: modeler denied models/")
    # E3: lead may write its own files
    for f in ("progress.md", "pipeline_state.yaml", "scope_declaration.yaml"):
        ok(not deny("Write", {"file_path": f"{run_path}/{f}"}), f"E3: lead denied {f}")
    # E4: script outputs are write-protected for everyone; report.md only for writer
    for at in (None, "modeler", "analyst"):
        ok(deny("Edit", {"file_path": f"{run_path}/identifiability.yaml"}, at),
           f"E4: {at} may edit identifiability.yaml")
        ok(deny("Write", {"file_path": f"{run_path}/uncertainty_report.yaml"}, at),
           f"E4: {at} may write uncertainty_report.yaml")
        ok(deny("Write", {"file_path": f"{run_path}/metadata.json"}, at),
           f"E4: {at} may write metadata.json")
    ok(deny("Write", {"file_path": f"{run_path}/report.md"}), "E4: lead may write report.md")
    ok(not deny("Edit", {"file_path": f"{run_path}/metadata.json"}, "planner"),
       "E4: planner denied metadata.json (decision_year)")
    ok(not deny("Write", {"file_path": f"{run_path}/report.md"}, "writer"),
       "E4: writer denied report.md")
    # E5: unregistered agents denied, registered allowed
    ok(deny("Agent", {"subagent_type": "general-purpose", "prompt": "x"}),
       "E5: general-purpose spawn allowed")
    ok(deny("Agent", {"prompt": "x"}), "E5: spawn without subagent_type allowed")
    ok(not deny("Agent", {"subagent_type": "modeler", "prompt": "x"}),
       "E5: registered modeler spawn denied")
    # E6: Bash heuristics
    bash_denied = [
        "sed -i 's/DETECTED/ACKNOWLEDGED/' runs/selftest_run/identifiability.yaml",
        "echo hi > scripts/validate_critique_yaml.py",
        "cat x | tee -a agents/__init__.py",
        "cp /tmp/x.py scripts/identifiability.py",
        "rm -f .claude/orchestration/effort_floors.yaml",
        "git checkout -- scripts/",
        "python3 -c \"open('scripts/x.py','w').write('')\"",
        "cat .env",
        "set -a && source .env && set +a",
        "echo verdict >> runs/selftest_run/critique_domain.yaml",
    ]
    for cmd in bash_denied:
        ok(deny("Bash", {"command": cmd}), f"E6: Bash allowed: {cmd}")
    bash_allowed = [
        "python3 scripts/validate_critique_yaml.py runs/selftest_run --json",
        "python3 scripts/render_claims.py runs/selftest_run 2>&1 | tail -5",
        "cat scripts/identifiability.py | head",
        "grep -n verdict runs/selftest_run/identifiability.yaml",
        "echo note >> runs/selftest_run/progress.md",
        "ls .claude/skills > /dev/null",
    ]
    for cmd in bash_allowed:
        ok(not deny("Bash", {"command": cmd}), f"E6: Bash denied: {cmd}")
    # modeler may write models/ via bash; lead may not
    ok(not deny("Bash", {"command": "echo x > runs/selftest_run/models/a.yaml"}, "modeler"),
       "E6: modeler bash models/ denied")
    ok(deny("Bash", {"command": "echo x > runs/selftest_run/models/a.yaml"}),
       "E6: lead bash models/ allowed")
    # E7: credential reads blocked
    ok(deny("Read", {"file_path": os.path.join(REPO_ROOT, ".env")}), "E7: Read .env allowed")
    ok(not deny("Read", {"file_path": os.path.join(REPO_ROOT, "scripts/x.py")}),
       "E7: Read scripts denied")

    # IN1-IN7 (Phase 23 α): integrity record of protected run artifacts.
    from agents.integrity import ArtifactIntegrity, REPORT_NAME
    with tempfile.TemporaryDirectory() as td:
        def w(rel, text):
            os.makedirs(os.path.dirname(os.path.join(td, rel)), exist_ok=True)
            open(os.path.join(td, rel), "w").write(text)
        w("critique_domain.yaml", "verdict: REVISE\n")
        w("uncertainty_report.yaml", "n_draws: 200\n")
        ig = ArtifactIntegrity(td, process_check=lambda: set())
        # IN1: the lead's plain Bash rewrites a critique → violation
        ig.on_pre("b1", "Bash", {"command": "printf x > \"$P\""}, None)
        w("critique_domain.yaml", "verdict: PASS\n")
        ok([e["file"] for e in ig.on_post("b1")] == ["critique_domain.yaml"],
           f"IN1 lead rewrite of a critique flagged: {ig.events}")
        # IN2: the critic's own Write (or its Bash) → explained
        ig.on_pre("w1", "Write", {"file_path": f"{td}/critique_domain.yaml"}, "critique-domain")
        w("critique_domain.yaml", "verdict: REVISE\nblockers: []\n")
        ok(ig.on_post("w1") == [], "IN2 critic's own write explained")
        # IN3: a harness script run explains a script output
        ig.on_pre("b2", "Bash", {"command": f"python3 scripts/propagate_uncertainty.py {td}"}, "modeler")
        w("uncertainty_report.yaml", "n_draws: 400\n")
        ok(ig.on_post("b2") == [], "IN3 harness script explains its output")
        # IN4: a non-script Bash editing a script output → violation, unless a
        # background harness process is alive
        ig.on_pre("b3", "Bash", {"command": "p=uncertainty_report.yaml; sed -i '' s/400/40/ $p"}, "modeler")
        w("uncertainty_report.yaml", "n_draws: 40\n")
        ok([e["file"] for e in ig.on_post("b3")] == ["uncertainty_report.yaml"],
           "IN4 variable-expansion edit of a script output flagged")
        ig2 = ArtifactIntegrity(td, process_check=lambda: {"propagate_uncertainty.py"})
        w("uncertainty_report.yaml", "n_draws: 800\n")
        ok(ig2.final_check() == [], "IN4b a live background harness script explains it")
        # IN5: editing the integrity report itself is a violation
        w(REPORT_NAME, '{"producer": "agents/integrity.py", "violations": []}')
        ok(any(e["file"] == REPORT_NAME for e in ig2.final_check()), "IN5 report tamper flagged")
        # IN8 (Phase 36): a producer run from the agent's own shell script is
        # credited by the process watch; tampering after it ends is not.
        with tempfile.TemporaryDirectory() as td8:
            open(os.path.join(td8, "identifiability.yaml"), "w").write("a: 1\n")
            alive = set()
            ig8 = ArtifactIntegrity(td8, process_check=lambda: set(alive))
            ig8.on_pre("s1", "Bash", {"command": "bash models/run_all.sh"}, "modeler")
            alive.add("identifiability.py"); ig8.observe()
            open(os.path.join(td8, "identifiability.yaml"), "w").write("a: 2\n")
            alive.clear()
            ok(ig8.on_post("s1") == [], f"IN8 a producer started by the agent's script is credited: {ig8.events}")
            ig8.on_pre("s2", "Bash", {"command": "true"}, "modeler")
            ok(ig8.on_post("s2") == [], "IN8b the grant ends once the producer is seen finished")
            ig8.on_pre("s3", "Bash", {"command": "p=identifiability.yaml; echo 'a: 3' > $p"}, "modeler")
            open(os.path.join(td8, "identifiability.yaml"), "w").write("a: 3\n")
            ok([e["file"] for e in ig8.on_post("s3")] == ["identifiability.yaml"],
               "IN8c a change after the producer has finished is still flagged")
            ig9 = ArtifactIntegrity(td8, process_check=lambda: set(alive))
            ig9.on_pre("s4", "Bash", {"command": "bash models/run_all.sh"}, "modeler")
            open(os.path.join(td8, "identifiability.yaml"), "w").write("a: 4\n")
            ok([e["file"] for e in ig9.on_post("s4")] == ["identifiability.yaml"],
               "IN8d without a producer seen running, the change is flagged (control)")
            alive.add("identifiability.py")
            import time as _t8
            ig9.start_watch(0.05); _t8.sleep(0.3); ig9.stop_watch()
            ok(any(g.get("observed") for g in ig9.grants), "IN8e the background watch samples producers")
            ig10 = ArtifactIntegrity(td8, process_check=lambda: set(alive))
            alive.add("identifiability.py")
            _orig = ig10._process_check
            def _slow():
                got = _orig(); ig10._check(where="post_tool"); return got   # a check runs mid-sample
            ig10._process_check = _slow
            ig10.observe()
            ok(not any(g.get("observed") for g in ig10.grants),
               "IN8f a sample taken before a check that ran meanwhile is discarded")
            ig11 = ArtifactIntegrity(td8, process_check=lambda: set(alive))
            _seq_mid = ig11._check_seq
            ig11._check(where="post_tool")       # a check that began before the sample and ends after it
            ig11._check_seq = _seq_mid + 1       # as if sampled mid-check
            _orig11 = ig11._process_check
            def _mid():
                got = _orig11(); ig11._check_seq += 1; return got   # the check ends while sampling
            ig11._process_check = _mid
            ig11.observe()
            ok(not any(g.get("observed") for g in ig11.grants),
               "IN8g a sample overlapping the end of a check is discarded")
        # IN6: a resumed run keeps earlier violations
        ok(len(ArtifactIntegrity(td, process_check=lambda: set()).events) >= 1,
           "IN6 violations survive a restart")
        # IN12: agents/integrity.py's own self-test (lifecycle, retired globs, measles replay)
        from agents import integrity as _integ
        ok(_integ._self_test() == 0, "IN12 agents/integrity.py --self-test")
        # EV1-EV3 (port of main into v2): the eval-workspace refusal covers --v2 runs, and a v2
        # resume is checked on the question it will use (run_state.json), not the argument.
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
    # IC1-IC7 (Codex review of PR #15).
    from agents.integrity import harness_scripts_run
    with tempfile.TemporaryDirectory() as td:
        def w(rel, text):
            os.makedirs(os.path.dirname(os.path.join(td, rel)), exist_ok=True)
            open(os.path.join(td, rel), "w").write(text)
        w("uncertainty_report.yaml", "n: 1\n"); w("critique_domain.yaml", "v: 1\n")
        w("metadata.json", '{"question": "Q", "methods_critic": true}')
        ig = ArtifactIntegrity(td, process_check=lambda: set())
        # IC1: a comment or string mentioning a script authorizes nothing
        _v = f"{REPO_ROOT}/scripts/validate_critique_yaml.py runs/x"
        _tf = [harness_scripts_run(f"timeout {o} python3 {_v}")[0] for o in
               ("600", "1.5m", "-k 5 600", "--signal TERM 600", "-s TERM 10s", "--kill-after=5 --preserve-status 600")]
        _neg = [harness_scripts_run(f"timeout {o} python3 {_v}")[0] for o in
                ("-k 5", "", "invalid", "--help 600", "-- -v 600", "600 X=1", "-k nonsense 600", "--signal 9x 600",
                 "--signal NOTASIGNAL 600", "\u0661\u0660", "-k \u0661 600", "-s 99 600", "-s \u017ftop 600", "-s 0 600",
                 f"-s {__import__('signal').NSIG} 600")]
        ok(all(x == ["validate_critique_yaml.py"] for x in _tf)
           and harness_scripts_run(f"timeout -- 600 python3 {_v}")[0] == ["validate_critique_yaml.py"]
           and harness_scripts_run(f"timeout 1e2 python3 {_v}")[0] == ["validate_critique_yaml.py"]
           and harness_scripts_run(f"timeout -s KILL 600 python3 {_v}")[0] == ["validate_critique_yaml.py"]
           and harness_scripts_run(f"timeout --signal=sigterm 600 python3 {_v}")[0] == ["validate_critique_yaml.py"]
           and all(x == [] for x in _neg),
           f"IG-timeout: known options and a valid duration are skipped; anything else credits nothing: {_tf} {_neg}")
        ok(harness_scripts_run('p=x; printf f > "$p"; # scripts/compare_models.py')[0] == []
           and harness_scripts_run('echo "python3 scripts/compare_models.py"')[0] == []
           and harness_scripts_run("cd /tmp && python3 /other/repo/scripts/compare_models.py x")[0] == []
           and harness_scripts_run("nohup .venv/bin/python3 scripts/propagate_uncertainty.py runs/x &")
           == (["propagate_uncertainty.py"], True),
           "IC1 only an executed harness script counts")
        ok(harness_scripts_run('R=runs/x && nohup sh -c "python3 scripts/propagate_uncertainty.py $R --n-draws 225; '
                               'echo UQDONE exit=\\$?" > $R/uq_run.log 2>&1 &')
           == (["propagate_uncertainty.py"], True)
           and harness_scripts_run("bash -c 'echo scripts/compare_models.py'")[0] == []
           and harness_scripts_run("bash /dev/null -c 'python3 scripts/propagate_uncertainty.py runs/x'")[0] == []
           and harness_scripts_run("bash -lc 'python3 scripts/compare_models.py runs/x'")[0] == ["compare_models.py"]
           and harness_scripts_run("bash -Zc 'python3 scripts/propagate_uncertainty.py runs/x'")[0] == []
           and harness_scripts_run("bash -n -c 'python3 scripts/propagate_uncertainty.py runs/x'")[0] == []
           and harness_scripts_run("bash -nc 'python3 scripts/propagate_uncertainty.py runs/x'")[0] == []
           and harness_scripts_run("nohup bash -o pipefail -c 'python3 scripts/propagate_uncertainty.py runs/x' &")
           == (["propagate_uncertainty.py"], True),
           "IC1d a producer inside sh -c / bash -c is recognized (E7); a mention is not")
        ig.on_pre("c1", "Bash", {"command": 'p=u.yaml; printf f > "$p"; # scripts/propagate_uncertainty.py'}, "modeler")
        w("uncertainty_report.yaml", "n: forged\n")
        ok(len(ig.on_post("c1")) == 1, "IC1b comment mention doesn't authorize a forged output")
        # IC1c: a script authorizes only its own outputs, never critiques
        ig.on_pre("c2", "Bash", {"command": "python3 scripts/compare_models.py runs/x"}, None)
        w("critique_domain.yaml", "v: forged\n")
        ok([e["file"] for e in ig.on_post("c2")] == ["critique_domain.yaml"],
           "IC1c a harness script doesn't authorize critique changes")
        # IC2: metadata.json is watched; the planner's edit is fine, the lead's is not
        ig.on_pre("c3", "Write", {"file_path": f"{td}/metadata.json"}, "planner")
        w("metadata.json", '{"question": "Q", "methods_critic": true, "decision_year": 2027}')
        ok(ig.on_post("c3") == [], "IC2 planner metadata edit explained")
        ig.on_pre("c4", "Bash", {"command": 'p=metadata.json; printf x > "$p"'}, None)
        w("metadata.json", '{"question": "Q", "methods_critic": false}')
        ok([e["file"] for e in ig.on_post("c4")] == ["metadata.json"], "IC2b lead metadata edit flagged")
        # IC3: a read-only report doesn't freeze the record; status blocks regardless
        os.chmod(os.path.join(td, REPORT_NAME), 0o444)
        w("uncertainty_report.yaml", "n: forged again\n")
        ig.final_check()
        rep = json.load(open(os.path.join(td, REPORT_NAME)))
        ok(len(rep["violations"]) == len(ig.events) >= 3,
           f"IC3 report rewritten despite chmod 444 ({len(rep['violations'])} vs {len(ig.events)})")
        ok(exit_code_for("artifact_tampered") == 1, "IC3b a tampered run exits 1")
        # IC5: the writer's allowed Bash write to report.md is explained
        ig5 = ArtifactIntegrity(td, process_check=lambda: set())
        ig5.on_pre("c5", "Bash", {"command": f"printf draft > {td}/report.md"}, "writer")
        w("report.md", "draft\n")
        ok(ig5.on_post("c5") == [], "IC5 writer carve-out covers its Bash too")
        # IC6: a background script that finishes between hooks is still explained once
        alive = {"v": {"propagate_uncertainty.py"}}
        ig5._process_check = lambda: alive["v"]
        ig5.on_pre("c6", "Bash", {"command": f"nohup python3 scripts/propagate_uncertainty.py {td} > log 2>&1 &"}, "modeler")
        ig5.on_post("c6")            # launch scan: seen alive → grant kept
        alive["v"] = set()           # it then finishes before the next hook
        w("uncertainty_report.yaml", "n: final\n")
        ok(ig5.on_post("poll") == [], "IC6 background grant explains the output after the process ended")
        w("uncertainty_report.yaml", "n: forged later\n")
        ig5.on_post("poll2")
        ok(any(e["file"] == "uncertainty_report.yaml" for e in ig5.events),
           "IC6b the grant expires once the process has been seen finished")
        # IC6c (Codex): a background launch that never ran grants nothing
        ig6 = ArtifactIntegrity(td, process_check=lambda: set())
        ig6.on_pre("c6c", "Bash", {"command": f"true || nohup python3 scripts/propagate_uncertainty.py {td} &"}, "modeler")
        ig6.on_post("c6c")
        ig6.on_pre("c6d", "Bash", {"command": 'p=uncertainty_report.yaml; printf x > "$p"'}, "modeler")
        w("uncertainty_report.yaml", "n: forged via dead grant\n")
        ok([e["file"] for e in ig6.on_post("c6d")] == ["uncertainty_report.yaml"],
           "IC6c a never-started background launch leaves no grant")
        # IC6d (Codex): `cd <repo> && python3 scripts/x.py` from another cwd is recognized;
        # a sibling run's process does not count
        ok(harness_scripts_run(f"cd {REPO_ROOT} && python3 scripts/coherence_audit.py {td}", cwd=td)[0]
           == ["coherence_audit.py"]
           and harness_scripts_run("cd $X && python3 scripts/coherence_audit.py r", cwd=td)[0] == [],
           "IC6d script resolution follows cd; unknown cd authorizes nothing")
        # IC6e (Codex r3): pushd/popd tracked; a concurrent tool's scan can't drop a grant
        ok(harness_scripts_run(f"pushd /tmp; popd; python3 scripts/coherence_audit.py {td}",
                               cwd=REPO_ROOT)[0] == ["coherence_audit.py"],
           "IC6e popd restores the directory for script resolution")
        ok(enforcement.check_tool_use("Bash", {"command": f"pushd /tmp; popd; echo x > {os.path.relpath(run_path, REPO_ROOT)}/critique_domain.yaml"},
                                      run_path, None, set()) is not None,
           "IC6f popd restores the directory for the write check")
        alive2 = {"v": set()}
        ig6e = ArtifactIntegrity(td, process_check=lambda: alive2["v"])
        ig6e.on_pre("L", "Bash", {"command": f"nohup python3 scripts/propagate_uncertainty.py {td} &"}, "modeler")
        ig6e.on_pre("R", "Read", {"file_path": f"{td}/x"}, "modeler")
        ig6e.on_post("R")              # concurrent Read finishes before the producer starts
        alive2["v"] = {"propagate_uncertainty.py"}
        ig6e.on_post("L")              # launch's own post: alive → grant kept
        alive2["v"] = set()
        w("uncertainty_report.yaml", "n: final after exit\n")
        ok(ig6e.on_post("poll") == [], "IC6g only the launching call's post-hook judges its grant")
        # IC6h (Codex r4): popd -n keeps the cwd; an unarmed grant expires
        ok(harness_scripts_run(f"cd /tmp; pushd {REPO_ROOT}; popd -n; python3 scripts/coherence_audit.py {td}",
                               cwd=REPO_ROOT)[0] == ["coherence_audit.py"],
           "IC6h popd -n does not change directory")
        import agents.integrity as _ig
        ig6h = ArtifactIntegrity(td, process_check=lambda: set())
        ig6h.on_pre("L2", "Bash", {"command": f"nohup python3 scripts/propagate_uncertainty.py {td} &"}, "modeler")
        for g in ig6h.grants:
            g["t"] -= _ig.INTENT_TTL_S + 1
        for i in ig6h.inflight.values():
            i["t"] -= _ig.INTENT_TTL_S + 1
        w("uncertainty_report.yaml", "n: forged after a lost post-hook\n")
        ok(any(e["file"] == "uncertainty_report.yaml" for e in ig6h.final_check()),
           "IC6i an unarmed grant whose post-hook never came expires")
        # IC7: a tool whose PostToolUse never came still explains its output at finalize
        ig7 = ArtifactIntegrity(td, process_check=lambda: set())
        ig7.on_pre("c7", "Bash", {"command": f"python3 scripts/coherence_audit.py {td}; false"}, "lead-ish")
        w("coherence_audit.yaml", "ok: true\n")
        ok(ig7.final_check() == [], "IC7 final scan runs before in-flight intents are cleared")
        # IC8 (Copilot): the cross-model critique is script-owned, not critic-owned
        w("critique_crossmodel.yaml", "v: 1\n")
        ig8 = ArtifactIntegrity(td, process_check=lambda: set())
        ig8.on_pre("c8", "Bash", {"command": 'p=critique_crossmodel.yaml; printf x > "$p"'}, "critique-redteam")
        w("critique_crossmodel.yaml", "v: forged\n")
        ok([e["file"] for e in ig8.on_post("c8")] == ["critique_crossmodel.yaml"],
           "IC8 a critic can't rewrite the script-owned cross-model critique")
        # IC9 (Copilot): a malformed record on resume is evidence, not a fresh start
        open(os.path.join(td, REPORT_NAME), "w").write("{truncated")
        ig9 = ArtifactIntegrity(td, process_check=lambda: set())
        ok(ig9.events and ig9.events[0]["change"] == "invalid_at_start",
           "IC9 unreadable integrity record on resume becomes a violation")
    # IR1-IR3 (E7 resume): a violation resolves when the producer rewrites the file.
    with tempfile.TemporaryDirectory() as td:
        def w(rel, text):
            os.makedirs(os.path.dirname(os.path.join(td, rel)), exist_ok=True)
            open(os.path.join(td, rel), "w").write(text)
        igr = ArtifactIntegrity(td, process_check=lambda: set())
        igr.on_pre("m1", "Bash", {"command": "python3 models/run_calibration.py"}, "modeler")
        w("identifiability.yaml", "verdict: CLEAN  # hand-written by an agent script\n")
        igr.on_post("m1")
        ok(len(igr.open_events) == 1, "IR1 agent-script write of a gate output opens a violation")
        igr.on_pre("m2", "Bash", {"command": f"python3 scripts/identifiability.py {td}"}, "modeler")
        w("identifiability.yaml", "producer: scripts/identifiability.py\nverdict: CLEAN\n")
        igr.on_post("m2")
        ok(igr.open_events == [] and igr.events[0].get("resolved"),
           f"IR2 a producer rewrite resolves the violation and leaves none open: {igr.events}")
        w(REPORT_NAME, '{"producer": "agents/integrity.py", "violations": []}')
        igr.final_check()
        igr.on_pre("m3", "Bash", {"command": f"python3 scripts/identifiability.py {td}"}, "modeler")
        igr.on_post("m3")
        ok(any(e["file"] == REPORT_NAME and e.get("open") for e in igr.events),
           "IR3 tampering with the integrity record itself is never resolved")
        # IR4/IR5 (Codex, PR #16): an identical-bytes producer rewrite resolves;
        # a skipped producer (file untouched) does not.
        w("models/eligibility_probe.yaml", "verdict: NOT_APPLICABLE\n")      # agent copy
        igr.on_pre("e1", "Bash", {"command": "cp x models/eligibility_probe.yaml"}, "modeler")
        igr.on_post("e1")
        ok(any(e["file"] == "models/eligibility_probe.yaml" and e.get("open") for e in igr.events),
           "IR4a copied gate output opens a violation")
        igr.on_pre("e2", "Bash", {"command": f"false && python3 scripts/eligibility_probe.py {td}"}, "modeler")
        igr.on_post("e2")
        ok(any(e["file"] == "models/eligibility_probe.yaml" and e.get("open") for e in igr.events),
           "IR4b a skipped producer (file untouched) does not resolve it")
        import time as _t; _t.sleep(0.01)
        igr.on_pre("e3", "Bash", {"command": f"python3 scripts/eligibility_probe.py {td}"}, "modeler")
        w("models/eligibility_probe.yaml", "verdict: NOT_APPLICABLE\n")      # identical bytes, rewritten
        igr.on_post("e3")
        ok(not any(e["file"] == "models/eligibility_probe.yaml" and e.get("open") for e in igr.events),
           "IR5 an identical-bytes producer rewrite resolves it")
    ok(enforcement.check_tool_use("Write", {"file_path": f"{run_path}/artifact_integrity.json"},
                                  run_path, "analyst", set()) is not None,
       "IN8 no agent may write artifact_integrity.json")

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
    # T1: harness hashing detects a modification
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "scripts"))
        p = os.path.join(td, "scripts", "a.py")
        with open(p, "w") as f:
            f.write("x = 1\n")
        # HL1-HL2: harness files are read-only during a run, and chmod on them is denied
        os.makedirs(os.path.join(td, "scripts"), exist_ok=True)
        _hp = os.path.join(td, "scripts", "x.py")
        open(_hp, "w").write("x = 1\n")
        _modes = enforcement.lock_harness(td)
        try:
            open(_hp, "w").write("x = 2\n")
            _wrote = True
        except PermissionError:
            _wrote = os.geteuid() == 0   # root ignores file modes; the hash still catches it
        try:
            open(os.path.join(td, "scripts", "y.tmp"), "w").write("y")
            os.replace(os.path.join(td, "scripts", "y.tmp"), _hp)
            _replaced = os.geteuid() != 0
        except PermissionError:
            _replaced = False
        enforcement.unlock_harness(_modes)
        ok(not _wrote and not _replaced and _hp in _modes and os.access(_hp, os.W_OK)
           and os.access(os.path.join(td, "scripts"), os.W_OK),
           "HL1 a locked harness file cannot be written or replaced; unlock restores it")
        ok(enforcement.bash_reason("chmod u+w scripts/effect_size_registry.py", td, "modeler") is not None
           and enforcement.bash_reason("python3 -c \"import os; os.chmod('agents/x.py', 0o644)\"", td, "modeler") is not None
           and enforcement.bash_reason("chmod +x runs/r1/models/run.sh", td, "modeler") is None,
           "HL2 changing harness permissions is denied; run files are not affected")
        _hl3 = enforcement.bash_reason("cat > models/x.sh <<EOF\npython3 scripts/compute.py map .\nEOF\nchmod +x models/x.sh",
                                       td, "modeler") or ""
        ok("separate command" in _hl3 and enforcement.bash_reason("chmod +x models/x.sh", td, "modeler") is None,
           "HL3 a refused chmod says how to chmod a run file; a separate chmod of a run file is allowed")
        os.remove(_hp)
        before = enforcement.hash_harness(td)
        ok(not enforcement.harness_changed(enforcement.diff_harness(before, enforcement.hash_harness(td))),
           "T1 clean tree flagged")
        with open(p, "w") as f:
            f.write("x = 2\n")
        d = enforcement.diff_harness(before, enforcement.hash_harness(td))
        ok(d["modified"] == ["scripts/a.py"], f"T1 modification missed: {d}")
        ok(enforcement.is_harness_path(os.path.join(enforcement.REPO_ROOT, "requirements-agent.txt"))
           and "requirements-agent.txt" not in enforcement.hash_harness(td),
           "T1b requirements-agent.txt (PR #116) is a harness file; absent, the hash skips it (no false tamper)")

    # CR1-CR4 (Codex review of PR #14): `cd` is tracked; Grep/Glob can't read .env.
    rp_rel = os.path.relpath(run_path, REPO_ROOT)
    rp_cd = f"cd {rp_rel}"
    ok(enforcement.check_tool_use("Bash", {"command": f"cd {rp_rel} && echo forged > critique_domain.yaml"},
                                  run_path, None, set()) is not None,
       "CR1 cd into the run dir then write a protected file is denied")
    ok(enforcement.check_tool_use("Bash", {"command": "cd $RUN && echo x > critique_domain.yaml"},
                                  run_path, None, set()) is not None,
       "CR2 cd to an unknown dir: relative protected write is denied conservatively")
    ok(enforcement.check_tool_use("Bash", {"command": f"cd {rp_rel} && echo ok > notes.txt"},
                                  run_path, None, set()) is None,
       "CR3 cd then an ordinary write stays allowed")
    ok(enforcement.check_tool_use("Grep", {"path": os.path.join(REPO_ROOT, ".env"), "pattern": ".*"},
                                  run_path, "analyst", set()) is not None
       and enforcement.check_tool_use("Grep", {"path": REPO_ROOT, "glob": ".env*", "pattern": "KEY"},
                                      run_path, "analyst", set()) is not None
       and enforcement.check_tool_use("Grep", {"path": run_path, "glob": "*.py", "pattern": "x"},
                                      run_path, "analyst", set()) is None
       and enforcement.check_tool_use("Glob", {"pattern": "**/*"}, run_path, "analyst", set()) is None,
       "CR4 Grep on .env denied; ordinary Grep and Glob allowed")
    # CR6-CR8 (Codex re-review): cd-segment redirects, subshell cd, ../ after unknown cd.
    ok(enforcement.check_tool_use("Bash", {"command": f"cd /tmp > {rp_rel}/critique_domain.yaml"},
                                  run_path, None, set()) is not None,
       "CR6 redirect on a cd segment is checked before the cd")
    ok(enforcement.check_tool_use("Bash", {"command": f"(cd /tmp); echo forged > {rp_rel}/critique_domain.yaml"},
                                  run_path, None, set()) is not None
       and enforcement.check_tool_use("Bash", {"command": "(cd /tmp && echo ok > x.txt); echo hi"},
                                      run_path, None, set()) is None,
       "CR7 a subshell cd does not leak into later segments")
    ok(enforcement.check_tool_use("Bash", {"command": 'cd "$RUN" && echo forged > ../critique_domain.yaml'},
                                  run_path, None, set()) is not None,
       "CR8 ../ target after an unknown cd is checked by file name")
    ok(enforcement.check_tool_use("Bash", {"command": f"(cd {rp_rel} && echo ')' && echo forged > critique_domain.yaml; true)"},
                                  run_path, None, set()) is not None,
       "CR11 a quoted ) does not close the subshell")
    ok(enforcement.check_tool_use("Bash", {"command": f"cd {rp_rel} && (cd /tmp && echo \\\"); echo forged > critique_domain.yaml"},
                                  run_path, None, set()) is not None,
       "CR12 an escaped quote does not hide the subshell close")
    for cmd in (f'({rp_cd} && echo "a;\\" )" && echo forged > critique_domain.yaml; true)',
                f'({rp_cd} && echo "a;)b" && echo forged > critique_domain.yaml; true)'):
        ok(enforcement.check_tool_use("Bash", {"command": cmd}, run_path, None, set()) is not None,
           f"CR13 quoted separators/parens don't desync subshell tracking: {cmd}")
    ok(enforcement.check_tool_use("Bash", {"command": "cd models && python3 fit.py > fit.log 2>&1 && echo done"},
                                  run_path, "modeler", set()) is None,
       "CR14 ordinary redirects (2>&1) still allowed")
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
        ok(conversation_cost([{"total_cost_usd": 4.0, "session_id": "a"},
                              {"total_cost_usd": 6.5, "session_id": "a"},
                              {"total_cost_usd": 1.0, "session_id": "b"}]) == 7.5,
           "BG2b run cost = max per conversation, summed across conversations")
        led = load_budget_ledger(td)
        charge_session(led, 1, R(4.0), 5.0)
        charge_session(led, 2, None, remaining_budget(5.0, led["sessions"]))
        # CR15-CR18 (Phase 28): crashed sessions.
        class _R2:
            def __init__(self, c, sid):
                self.total_cost_usd, self.session_id = c, sid
        _lc = {"sessions": []}
        charge_session(_lc, 1, _R2(62.0, "s1"), 450.0, session_id="s1", source="reconciled")
        ok(_lc["sessions"][0]["total_cost_usd"] == 62.0 and remaining_budget(450.0, _lc["sessions"]) > 380,
           f"CR15 a crashed session is charged its read-back cost, not the cap: {_lc}")
        charge_session(_lc, 2, _R2(100.0, "s1"), 388.0)
        ok(abs(sum(e["total_cost_usd"] for e in _lc["sessions"]) - 100.0) < 1e-6,
           f"CR16 the next cumulative report adds only the increase: {_lc}")
        _lc2 = {"sessions": []}
        charge_session(_lc2, 1, None, 8.0, session_id=None, elapsed_s=6.0)
        ok(_lc2["sessions"][0]["total_cost_usd"] < 0.2 and remaining_budget(8.0, _lc2["sessions"]) > 7.5,
           f"CR17 crashed before its conversation started: a duration bound: {_lc2}")
        _lc5 = {"sessions": []}
        charge_session(_lc5, 1, None, 100.0, session_id=None, elapsed_s=3 * 3600)
        ok(_lc5["sessions"][0]["total_cost_usd"] == 100.0, "CR17 the bound never exceeds the allowance")
        _lc6 = {"sessions": []}
        charge_session(_lc6, 1, None, 450.0, session_id="s1", elapsed_s=60.0)
        ok(remaining_budget(450.0, _lc6["sessions"]) <= 0,
           "CR18 a started conversation whose cost cannot be read is charged the whole allowance (cap holds)")

        async def _fq_ok(prompt, options):
            assert options.resume == "s1" and options.max_turns == 1 and options.tools == []
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="s1", total_cost_usd=62.5)

        async def _fq_fail(prompt, options):
            raise RuntimeError("api down")
            yield  # pragma: no cover
        _opts = ClaudeAgentOptions(model="m", tools=["Bash"], allowed_tools=["Bash"], max_turns=50,
                                   max_buffer_size=enforcement.MAX_BUFFER_SIZE)
        _r = asyncio.run(reconcile_conversation_cost(_opts, "s1", query_fn=_fq_ok))
        ok(_r is not None and _r.total_cost_usd == 62.5, f"CR18 read-back resumes the conversation tool-less: {_r}")
        ok(asyncio.run(reconcile_conversation_cost(_opts, "s1", query_fn=_fq_fail)) is None,
           "CR18 an unreadable cost returns None (then the allowance is charged)")

        async def _fq_zero(prompt, options):
            yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=0,
                                is_error=True, num_turns=0, session_id="s1", total_cost_usd=0.0)
        ok(asyncio.run(reconcile_conversation_cost(_opts, "s1", query_fn=_fq_zero)) is None,
           "CR18 an error or zero-cost read-back is not a cost reading (crash smoke 5 read back $0.00)")
        # CR19 (Phase 34 δ): the read-back is retried, then the transcripts give the cost.
        _tr_root = os.path.join(td, "projects")
        os.makedirs(os.path.join(_tr_root, "-tmp-x"), exist_ok=True)
        with open(os.path.join(_tr_root, "-tmp-x", "s1.jsonl"), "w") as _f:
            _f.write(json.dumps({"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5",
                                                                   "usage": {"input_tokens": 500_000}}}) + "\n")
        _calls = []

        async def _fq_count(prompt, options):
            _calls.append(1)
            async for m in _fq_fail(prompt, options):
                yield m
        _c, _src = asyncio.run(crashed_session_cost(_opts, "s1", attempts=3, delay_s=0, query_fn=_fq_count,
                                                    transcript_root=_tr_root))
        ok(len(_calls) == 3 and _c is not None and abs(_c.total_cost_usd - 2.1) < 1e-9
           and _src.startswith("estimated from the CLI transcripts"),
           f"CR19 three read-back tries, then the transcript estimate (+5%): {len(_calls)} {_c} {_src}")
        _c2, _ = asyncio.run(crashed_session_cost(_opts, "s9", attempts=1, delay_s=0, query_fn=_fq_fail,
                                                  transcript_root=_tr_root))
        _c3, _src3 = asyncio.run(crashed_session_cost(_opts, "s1", attempts=3, delay_s=0, query_fn=_fq_ok,
                                                      transcript_root=_tr_root))
        ok(_c2 is None and _c3.total_cost_usd == 62.5 and _src3.startswith("reconciled"),
           "CR19b no transcript -> None (allowance charged); a working read-back is used first")
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
        ok(enforcement.check_tool_use("Write", {"file_path": f"{td}/budget_ledger.json"},
                                      td, "planner", set()) is not None,
           "BG3 agents may not write the budget ledger")
    # CS1 (Phase 22 γ): the ledger is built by a script; agents write the spec.
    ok(enforcement.check_tool_use("Write", {"file_path": f"{run_path}/models/claims_ledger.yaml"},
                                  run_path, "analyst", set()) is not None
       and enforcement.check_tool_use("Write", {"file_path": f"{run_path}/models/claims_spec.yaml"},
                                      run_path, "analyst", set()) is None
       and enforcement.check_tool_use("Bash", {"command": f"python3 scripts/build_claims_ledger.py {run_path}"},
                                      run_path, "analyst", set()) is None,
       "CS1 claims ledger script-produced; spec writable; builder runnable")
    ok(enforcement.check_tool_use("Write", {"file_path": f"{run_path}/critique_crossmodel.yaml"},
                                  run_path, "critique-redteam", set(reg)) is not None,
       "XM2 critique_crossmodel.yaml writable by an agent")
    ok(enforcement.check_tool_use("Bash", {"command": "python3 scripts/cross_model_critic.py runs/x --round 2"},
                                  run_path, None, set(reg)) is None,
       "XM3 lead cannot run cross_model_critic.py")

    # BG1 (Phase 21 θ): background execution denied for every agent.
    for agent_t in (None, "modeler", "analyst"):
        ok(enforcement.check_tool_use("Bash", {"command": "python3 x.py", "run_in_background": True},
                                      run_path, agent_t, set(reg)) == enforcement.BACKGROUND_DENY_REASON,
           f"BG1 background Bash allowed for {agent_t}")
    ok(enforcement.check_tool_use("Bash", {"command": "ls"}, run_path, None, set(reg)) is None,
       "BG1 foreground Bash wrongly denied")
    # KL1 (Phase 47 δ, A10): no process selection by pattern. Attempt 8's two
    # commands (the second is cut in the trace after `awk '{print $2`; completed
    # here with the pipe to kill it was feeding) ended the session with SIGTERM.
    kill_denied = [
        # the attempt 8 and attempt 7 retro denials (trace summaries, cut at 80 characters)
        "pkill -f propagate_uncertainty.py; cd /tmp/ab/R1/runs/2026-10-28_200327_build-an",
        'pkill -f "loo2_inputs.jsonl" ; sleep 1; ps aux | grep compute.py | grep -v grep ',
        'pkill -f "propagate_uncertainty.py" ; sleep 1; ps aux | grep propagate_uncertain',
        'pkill -f rnp_pilot.py; pkill -f "cbc"; cd /tmp/ab/R1/runs/2026-10-28_200327_buil',
        'pkill -INT -f "r034_solve_ladder_inputs" ; sleep 8; pgrep -f r034_solve_ladder_i',
        'pkill -f "compute.py map . --fn models/library.py::run_cell" ; sleep 3; ps aux |',
        'pkill -f run_all_r2.sh; pkill -f "model_figures\\|finalize.py\\|propagate_uncertai',
        "pkill -f run_all_r2.sh; pkill -f eligibility_probe; pkill -f propagate_uncertain",
        # attempt 8's second command, completed with the kill it was feeding
        "ps aux | grep \"scripts/propagate_uncertainty.py\" | grep -v grep | awk '{print $2}' | xargs kill",
        "ps aux | grep \"scripts/propagate_uncertainty.py\" | grep -v grep | awk '{print $2}' | xargs -r kill -9",
        "killall python3", "sudo pkill -9 -f x", "timeout 5 pkill -f x", "/usr/bin/pkill x", "2>/dev/null pkill x",
        "nohup env A=1 pkill x", "kill -l; pkill -f x", "kill $(pgrep -f x)", 'kill -9 "$(pgrep -f x)"',
        "kill `pgrep -f x`", "kill $(pidof python3)", "kill $(lsof -t -i:8787)",
        "kill $(ps aux | grep x | awk '{print $2}')", "pgrep -f x | xargs kill", "pgrep -f x | xargs -I{} kill -9 {}",
        "kill -9 -1234", "kill -TERM -- -1234", "kill 0", "kill -9 -1", "fuser -k 8080/tcp",
    ]
    for c in kill_denied:
        ok(enforcement.check_tool_use("Bash", {"command": c}, run_path, "modeler", set(reg)) is not None,
           f"KL1 pattern-based kill allowed: {c!r}")
    ok(enforcement.bash_reason("pkill -f x", run_path, "modeler") == enforcement.KILL_DENY_REASON
       and "compute.py status" in enforcement.KILL_DENY_REASON and "echo $!" in enforcement.KILL_DENY_REASON,
       "KL1 deny reason does not name the stop paths")
    kill_allowed = [
        "kill 12345", "kill -TERM 12345 12346", "kill -9 12345", "kill %1", "kill -l", "grep -n pkill notes.md",
        "cat <<'EOF' > notes.md\nnever pkill -f x\nkill $(pgrep y)\nEOF", "echo pkill", 'echo "pkill -f x"',
        "cat <<\\EOF > /tmp/notes.txt\n$(pkill -f x)\nEOF", "command -v pkill >/dev/null && echo installed",
        'GREP=grep; PATTERN=pkill; "$GREP" "$PATTERN" notes.md', "type pkill; which killall",
        "kill $(cat runs/x/models/uq.pid)", "kill \"$(cat /tmp/solve_pid)\"", "python3 a.py & kill $!",
        "nohup python3 a.py > a.log 2>&1 & echo $! > a.pid", "kill 84468 2>/dev/null",
        "P=$(cat /tmp/solve_pid); for i in $(seq 1 40); do kill -0 $P 2>/dev/null || break; sleep 15; done",
        "kill -0 $(pgrep -f x)", "kill $(cat a.pid); sleep 2; ps aux | grep model_run | grep -v grep",
        "kill 420782; sleep 1; pgrep -af \"python3 -u robust_net_plan\"", "pgrep -f x >/dev/null || echo done",
        "ls; # pkill -f x", "sed -n '/kill/p' f.txt", "python3 -c \"print('pkill')\"", "git log --grep=kill",
    ]
    for c in kill_allowed:
        ok(enforcement.check_tool_use("Bash", {"command": c}, run_path, "modeler", set(reg)) is None,
           f"KL1 harmless command denied: {c!r}")
    # KL3: odd input never raises (a false deny blocks real work; errors allow).
    for c in ("kill __MA_SUBST0__", "kill " + "9" * 5000, "kill 'unbalanced", 'kill "$(pgrep', "kill `",
              "kill $(" * 2000, "x" * 1_000_000 + "; kill 1 2", "pkill \\", "\x00kill\x00", "kill -s", "xargs kill"):
        try:
            enforcement.kill_reason(c)
        except Exception as e:  # noqa: BLE001
            failures.append(f"KL3 kill_reason raised {type(e).__name__} on {c[:40]!r}")
    ok(enforcement.kill_reason("kill __MA_SUBST0__") is None, "KL3 placeholder-looking PID denied")
    # KL2: a literal PID of the harness, its parent, or a session CLI it spawned.
    import subprocess as _sp
    child = _sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        for pid in (os.getpid(), os.getppid()):
            ok(enforcement.kill_reason(f"kill -9 {pid}") == enforcement.KILL_SELF_REASON,
               f"KL2 kill of the harness's own pid {pid} allowed")
        try:
            import psutil  # noqa: F401
            ok(enforcement.kill_reason(f"kill {child.pid}") == enforcement.KILL_SELF_REASON,
               "KL2 kill of a process the harness spawned (the session CLI) allowed")
        except ImportError:
            pass
    finally:
        child.kill(); child.wait()
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
    # ST7 (Codex r3 on #342, P1-7): resuming a lead run whose amendment lock another process holds writes nothing:
    # no identity stamp, no crash record, no upload of the active run
    import argparse as _ap7
    import threading as _th7
    from agents import stage_machine as _sm7, v2_sdk_tasks as _vt7
    _lr = tempfile.mkdtemp()
    json.dump({"pipeline": "v2", "commit": "0" * 40, "launcher": "t"}, open(os.path.join(_lr, "metadata.json"), "w"))
    _sm7.init_run(_lr, "Q?", signoff="lead")
    _held, _done, _stored7 = _th7.Event(), _th7.Event(), []

    def _hold():
        with _sm7._amend_lock(_lr):
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
       f"ST7 a resume refused by a held amendment lock exits 1 with nothing stamped, recorded or stored ({_got7}, "
       f"{_stored7})")
    shutil.rmtree(_lr, ignore_errors=True)
    # ST8 (Codex r1 of #349): a child another process holds stops this run, which still stores the parent it finished,
    # up to that child; its own held lock stores nothing
    _p8, _calls8 = tempfile.mkdtemp(), []
    _c8 = _p8.rstrip(os.sep) + ".amend1"

    def _locked(rd):
        def f():
            raise _sm7.AmendmentLocked("held", run_dir=rd)
        return f
    _got8 = [_outcome(lambda: _rs(_locked(rd), _p8, store=lambda p, upto=None: _calls8.append((p, upto))))
             for rd in (_c8, _p8)]
    _saved8 = (_sm7.iter_handoff_chain, globals()["store_run_results"])
    _stored8 = []
    _sm7.iter_handoff_chain = lambda p, limit=8, kept=False: iter([_c8, _c8 + ".amend1"])
    globals()["store_run_results"] = _stored8.append
    for _x in (_c8, _c8 + ".amend1"):
        os.makedirs(_x, exist_ok=True)
    try:
        _store_chain(_p8, upto=_c8)
        _store_chain(_p8)
    finally:
        _sm7.iter_handoff_chain, globals()["store_run_results"] = _saved8
        shutil.rmtree(_c8 + ".amend1", ignore_errors=True)
    ok(_got8 == ["AmendmentLocked"] * 2 and _calls8 == [(_p8, os.path.realpath(_c8))]
       and "crash" not in _metadata_dict(os.path.join(_p8, "metadata.json"))
       and _stored8 == [_p8, _p8, _c8, _c8 + ".amend1"],
       f"ST8 a held child stops the chain there: the parent is stored, the held child and later ones are not "
       f"({_got8}, {_calls8}, {_stored8})")
    # ST9 (Codex r2 of #349): a one-argument store still stores the parent; the chain is stored one run at a time, so a
    # run that cannot be read after the parent stops the chain there, and a child whose lock another process holds is
    # not stored from here
    _calls9 = []
    _outcome(lambda: _rs(_locked(_c8), _p8, store=lambda p: _calls9.append(p)))
    _kw9 = []        # Codex r3 of #349: a store taking **kwargs is given the held child too
    _outcome(lambda: _rs(_locked(_c8), _p8, store=lambda p, **kw: _kw9.append(kw.get("upto"))))
    _calls9 += [] if _kw9 == [os.path.realpath(_c8)] else ["kwargs lost upto"]

    def _gen(p, limit=8, kept=False):
        yield _c8
        raise AttributeError("unreadable next run")
    _saved9 = (getattr(_sm7, "iter_handoff_chain", None), globals()["store_run_results"])
    _stored9, _err9 = [], []
    _sm7.iter_handoff_chain = _gen
    globals()["store_run_results"] = _stored9.append
    os.makedirs(_c8, exist_ok=True)
    try:
        for _upto in (_c8, None):
            try:
                _store_chain(_p8, upto=_upto)
            except Exception as e:  # noqa: BLE001 -- the finding
                _err9.append(type(e).__name__)
        _held9, _rel9 = _th7.Event(), _th7.Event()

        def _hold9():
            with _sm7._amend_lock(_c8):
                _held9.set()
                _rel9.wait(60)
        _t9 = _th7.Thread(target=_hold9)
        _t9.start()
        _held9.wait(60)
        try:
            _store_chain(_p8)
        except Exception as e:  # noqa: BLE001
            _err9.append(type(e).__name__)
        finally:
            _rel9.set()
            _t9.join(60)
    finally:
        _sm7.iter_handoff_chain, globals()["store_run_results"] = _saved9
    ok(_calls9 == [_p8] and _err9 == [] and _stored9 == [_p8, _p8, _c8, _p8],
       f"ST9 a one-argument store stores the parent; the chain is stored run by run up to an unreadable or held run "
       f"({_calls9}, {_err9}, {_stored9})")
    shutil.rmtree(_c8, ignore_errors=True)
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
    locked = None
    status0 = _metadata_dict(os.path.join(run_path, "metadata.json")).get("status")   # an earlier invocation's
    try:
        return run()
    except BaseException as e:
        # Codex r3 on #342 (P1-7): another process holds a lead run's amendment lock: not a crash of this run, and the
        # run it is running is not uploaded half-way from here; a parent this process finished is still stored, up to
        # the held child (Codex r1 of #349)
        if type(e).__name__ == "AmendmentLocked":
            locked = os.path.realpath(getattr(e, "run_dir", None) or run_path)
            raise
        try:
            where, s0 = run_path, status0
            try:        # Codex r1 on #374: a run that handed over to its child crashed in the child, the chain's last
                from agents import stage_machine as _sm
                chain = _sm.handoff_chain(run_path)
                where, s0 = (chain[-1], None) if chain else (run_path, status0)
            except Exception:  # noqa: BLE001 -- no chain read: the run itself
                pass
            mp = os.path.join(where, "metadata.json")
            meta = _metadata_dict(mp)
            meta["crash"] = f"{type(e).__name__}: {e}"[:500]
            # every exit records its status (baseline hb3 §4.2): a terminal status this invocation's machine wrote
            # stands; none, a pause, or an earlier invocation's status (a resume) is replaced
            cur = meta.get("status")
            if not cur or cur == s0 or str(cur).startswith("paused:"):
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
            if locked == os.path.realpath(run_path):
                print("v2 run not stored: another process holds its amendment lock", file=sys.stderr, flush=True)
            elif locked:          # a store without `upto` stores the one run it is given (Codex r2 of #349)
                import inspect
                try:                  # a store that can bind `upto` (a keyword or **kwargs) is given it (Codex r3)
                    inspect.signature(store).bind(run_path, upto=locked)
                    chain = True
                except (TypeError, ValueError):
                    chain = False
                got = store(run_path, upto=locked) if chain else store(run_path)
                print(f"v2 run stored up to {locked}: {got}", flush=True)
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
        "lead" in (args.signoff, st.signoff)
        or st.state in ("ENGINE_BUILD", "ENGINE_ADMIT")
        or bool(getattr(st, "engine_build", None))
    )
    if need_lock:
        # Codex r3 on #342 (P1-7) & #363 (BE31): lock is taken before anything is written (run_v2 holds it
        # again, nested): a resume another process holds stops here with nothing stamped, recorded or stored
        lock = stage_machine._amend_lock(run_path)
        try:
            lock.__enter__()
        except stage_machine.AmendmentLocked as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    try:
        return _main_v2_run(args, ident, run_path, question)
    finally:
        lock.__exit__(None, None, None)


def _store_chain(run_path: str, upto: str | None = None) -> list:
    """lead-gaps β: a provisional lead amendment continues the run in a child run dir; it is stored too. `upto`: a child
    another process holds (AmendmentLocked): stored from there by that process, not here (Codex r1 of #349). Each run
    is stored before the next is read, and a child whose amendment lock another process holds, or that cannot be read,
    ends the chain there (Codex r2 of #349)."""
    from agents import stage_machine
    out = [store_run_results(run_path)]
    chain = stage_machine.iter_handoff_chain(run_path, kept=True)
    while True:
        try:
            x = next(chain)
        except StopIteration:
            break
        except Exception as e:  # noqa: BLE001 -- the runs stored so far stand; the rest is reported
            print(f"[ERROR] the run chain after {len(out)} run(s) was not read: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            break
        if upto and os.path.realpath(x) == os.path.realpath(upto):
            break
        lock = stage_machine._amend_lock(x)
        try:
            lock.__enter__()
        except stage_machine.AmendmentLocked:
            print(f"v2 run chain stored up to {x}: another process holds it", file=sys.stderr, flush=True)
            break
        except (OSError, ValueError) as e:   # no ownership established (Codex r3): never stored unlocked from here
            print(f"[ERROR] v2 run chain stored up to {x}: its lock could not be taken ({type(e).__name__}: {e}); "
                  "store it once it can be locked", file=sys.stderr, flush=True)
            break
        try:
            out.append(store_run_results(x))
        finally:
            lock.__exit__(None, None, None)
    return out


def _main_v2_run(args, ident: dict, run_path: str, question: str) -> int:
    from agents import stage_machine, v2_sdk_tasks
    pipeline_id.stamp(run_path, ident)
    print(f"Run (v2): {run_path}", flush=True)
    unsandboxed = getattr(args, "dev_unsandboxed_derivations", False)
    tasks = v2_sdk_tasks.SdkTasks(run_path, allow_cloud=args.cloud, unsandboxed_ok=unsandboxed)
    status = run_v2_and_store(lambda: stage_machine.run_v2(
        question, run_path, tasks, max_rounds=args.max_rounds, signoff=args.signoff,
        budget_usd=args.max_budget_usd, derive_unsandboxed=unsandboxed,
        engine_build_usd=getattr(args, "engine_build_usd", None)),
        run_path,
        store=_store_chain)
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
