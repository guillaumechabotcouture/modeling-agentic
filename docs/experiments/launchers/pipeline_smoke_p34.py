"""Pre-launch whole-pipeline smoke on Coiled (same VMs/packages as real runs), Phases 32-34:
2 rounds so the round plan, fix checks and done_when tests are exercised, the
lead signs the decision brief (--signoff lead), and --crash kills session 1.
1) main.py --preflight-only (real models; subagent Write probe).
2) main.py on a tiny question, every agent on Haiku with a small turn cap,
   1 round, hard $8 budget: must reach the final gate AND produce report.md.
Synchronous (short); the cluster shuts down at the end."""
import io, json, os, sys, tarfile, time
import coiled

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_coiled import SCRATCH, code_tarball, read_env_keys  # noqa: E402

TREE = os.path.expanduser(os.environ.get("SMOKE_TREE", "~/modeling-agentic-lessons"))
QUESTION = ("Using a simple deterministic SIR model, estimate how raising measles vaccination coverage "
            "from 80% to 90% changes the expected outbreak size in one district of 100,000 people.")
PREFLIGHT_ONLY = len(sys.argv) > 1 and sys.argv[1] == "--preflight-only"
CRASH = "--crash" in sys.argv
FLAGS = ["--max-rounds", "2", "--max-sessions", "4", "--max-budget-usd", "9", "--signoff", "lead"]


def run_side(code, key, question, flags):
    import glob, io, json, os, subprocess, sys, tarfile, time
    work = "/tmp/smoke"
    subprocess.run(["rm", "-rf", work])
    os.makedirs(work)
    tarfile.open(fileobj=io.BytesIO(code), mode="r:gz").extractall(work)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "OPENAI", "AZ_", "AZURE_"))}
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env["ANTHROPIC_API_KEY"] = key
    out = {}
    # Install exactly what requirements.txt pins (Coiled's package sync drifted to a newer SDK).
    import re as _re
    pins = [l.split("#")[0].strip() for l in open(os.path.join(work, "requirements.txt"))
            if _re.match(r"\s*claude-agent-sdk==", l)]
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pins], capture_output=True, text=True, timeout=600)
    out["pin"] = {"pins": pins, "rc": r.returncode, "tail": (r.stdout + r.stderr)[-300:]}
    t0 = time.time()
    r = subprocess.run([sys.executable, "main.py", "--preflight-only"], cwd=work, env=env,
                       capture_output=True, text=True, timeout=900)
    out["preflight"] = {"rc": r.returncode, "s": round(time.time() - t0),
                        "tail": (r.stdout + r.stderr)[-1500:]}
    if r.returncode != 0 or os.environ.get("SMOKE_PREFLIGHT_ONLY") or PREFLIGHT_ONLY:
        return out
    env.update(MA_SMOKE_MODEL="claude-haiku-5-5", MA_SMOKE_MAX_TURNS="12",
               MA_STORAGE_ACCOUNT="modelingagentic", MA_STORAGE_PREFIX="_checks/smoke-p34")
    t0 = time.time()
    with open(os.path.join(work, "smoke_run.log"), "w") as log:
        proc = subprocess.Popen([sys.executable, "main.py", question, *flags, "--skip-preflight"],
                                cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
        # Failure path (Phase 28): once the modeler starts, SIGKILL the agent CLI
        # (as the kernel did in E7 attempt 5); the run must resume into session 2.
        killed, first_seen = None, None
        if CRASH:
            t_end = time.time() + 3000
            while time.time() < t_end and proc.poll() is None and killed is None:
                time.sleep(1 if killed is None else 10)
                logtxt = open(os.path.join(work, "smoke_run.log"), errors="replace").read()
                # Kill before the lead's first cost report (it came at ~14 s), so
                # the crashed session has no result and must be estimated.
                if "SESSION 1/" in logtxt:
                    ps = subprocess.run(["pgrep", "-f", "claude_agent_sdk/_bundled/claude"], capture_output=True, text=True)
                    pids = [int(x) for x in ps.stdout.split()]
                    if pids and first_seen is None:
                        first_seen = time.time()
                    # a few seconds of real turns (so a transcript exists), before any cost report
                    if pids and time.time() - first_seen >= 6:
                        for pid in pids:
                            os.kill(pid, 9)
                        killed = pids
        proc.wait(timeout=5400)
        r = proc
    out["crash"] = {"killed_pids": killed} if CRASH else None
    out["run"] = {"rc": r.returncode, "s": round(time.time() - t0)}
    runs = sorted(glob.glob(os.path.join(work, "runs", "*/")))
    # What the pipeline itself produced, BEFORE the separate writer diagnostic below.
    if runs:
        d0 = runs[-1]
        m0 = json.load(open(d0 + "metadata.json")) if os.path.exists(d0 + "metadata.json") else {}
        out["pipeline"] = {"status": m0.get("status"),
                           "report_md": os.path.exists(d0 + "report.md"),
                           "final_gate_ran": os.path.exists(d0 + "final_gate.json"),
                           "stored": m0.get("stored")}
    # 3) WRITE stage directly, whatever the gate decided: the real writer
    #    agent (registry definition, enforcement hooks) drafts the report,
    #    then render_claims.py builds report.md. This is the step E7 lost.
    if runs:
        rd = os.path.relpath(runs[-1], work).rstrip("/")
        wsrc = (
            "import asyncio, os, sys\n"
            "sys.path.insert(0, '.')\n"
            "from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage\n"
            "from agents import build_agents, create_hooks, enforcement\n"
            f"rd = {rd!r}\n"
            "agents = {'writer': build_agents(question='smoke', run_dir=rd)['writer']}\n"
            "o = ClaudeAgentOptions(system_prompt='You are a test harness. Spawn the writer agent exactly once.',\n"
            "    model=os.environ['MA_SMOKE_MODEL'], agents=agents, tools=['Agent','Bash','Read','Write','Glob','Grep','Skill'], allowed_tools=['Agent','Bash','Read','Write','Glob','Grep','Skill'],\n"
            "    permission_mode='bypassPermissions', setting_sources=['project'], cwd=os.getcwd(),\n"
            "    hooks=create_hooks(os.path.abspath(rd), open(os.path.join(rd, 'trace_write_smoke.jsonl'), 'a'), __import__('datetime').datetime.now(), registered_agents={'writer'}), max_buffer_size=enforcement.MAX_BUFFER_SIZE,\n"
            "    env=enforcement.filtered_env(dict(os.environ), False), max_turns=6)\n"
            "async def go():\n"
            "    async for m in query(prompt=f'Spawn the writer agent for run directory {rd}. Tell it this is a smoke test: a short report is fine.', options=o):\n"
            "        if isinstance(m, ResultMessage): print('WRITER_COST', m.total_cost_usd)\n"
            "asyncio.run(go())\n")
        t0 = time.time()
        rw = subprocess.run([sys.executable, "-c", wsrc], cwd=work, env=env, capture_output=True, text=True, timeout=1800)
        rr = subprocess.run([sys.executable, "scripts/render_claims.py", rd], cwd=work, env=env,
                            capture_output=True, text=True, timeout=600)
        out["write_stage"] = {"rc": rw.returncode, "s": round(time.time() - t0),
                              "tail": (rw.stdout + rw.stderr)[-800:], "render": (rr.stdout + rr.stderr)[-300:],
                              "manuscript_md": os.path.exists(os.path.join(runs[-1], "manuscript.md")),
                              "report_md": os.path.exists(os.path.join(runs[-1], "report.md"))}
    if runs:
        d = runs[-1]
        meta = json.load(open(d + "metadata.json")) if os.path.exists(d + "metadata.json") else {}
        gate = json.load(open(d + "final_gate.json")) if os.path.exists(d + "final_gate.json") else {}
        led = json.load(open(d + "budget_ledger.json")) if os.path.exists(d + "budget_ledger.json") else {}
        out["result"] = {"status": meta.get("status"), "gate": gate.get("action"),
                         "report_md": os.path.exists(d + "report.md"),
                         "manuscript_md": os.path.exists(d + "manuscript.md"),
                         "report_bytes": os.path.getsize(d + "report.md") if os.path.exists(d + "report.md") else 0,
                         "spent_usd": led.get("spent_usd"),
                         "ledger_sessions": led.get("sessions"),
                         "resumed": "SESSION 2/" in open(os.path.join(work, "smoke_run.log"), errors="replace").read(),
                         "budget_stop": "Budget cap" in open(os.path.join(work, "smoke_run.log"), errors="replace").read(),
                         "stored": meta.get("stored"), "store_error": meta.get("store_error"),
                         "stages_seen": sorted({l.split("SUBAGENT START: ")[1].split(" ")[0]
                                                for l in open(os.path.join(work, "smoke_run.log"), errors="replace")
                                                if "SUBAGENT START: " in l})}
        # Phases 32-34: what the new structure produced.
        import yaml as _y
        def _ex(f): return os.path.exists(d + f)
        crit = {}
        for f in sorted(glob.glob(d + "critique_*.yaml")):
            try:
                bl = (_y.safe_load(open(f)) or {}).get("blockers") or []
            except Exception:
                bl = []
            hm = [b for b in bl if isinstance(b, dict) and b.get("severity") in ("HIGH", "MEDIUM")]
            crit[os.path.basename(f)] = {"high_medium": len(hm), "with_done_when": sum(1 for b in hm if b.get("done_when"))}
        reg = json.load(open(d + "done_when_registry.json")) if _ex("done_when_registry.json") else {"tests": {}}
        denied = {}
        for l in open(d + "trace.jsonl", errors="replace"):
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if r.get("denied"):
                k = r["denied"][:70]
                denied[k] = denied.get(k, 0) + 1
        out["structure"] = {"brief": _ex("decision_brief.yaml"), "signed": _ex("decision_brief.signoff.json"),
                            "open_problems": _ex("open_problems.json"), "round_plan": _ex("round_plan.yaml"),
                            "fix_checks": sorted(os.path.basename(x) for x in glob.glob(d + "fix_check_*.yaml")),
                            "registry_tests": len(reg["tests"]),
                            "registry_passed": sum(1 for t in reg["tests"].values() if t.get("passed_round")),
                            "critiques": crit, "denied": denied,
                            "validator_rounds": sum(1 for _ in open(d + "validator_history.jsonl")) if _ex("validator_history.jsonl") else 0}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            t.add(d, arcname="run")
            t.add(os.path.join(work, "smoke_run.log"), arcname="smoke_run.log")
        out["tar"] = buf.getvalue()
    return out


if __name__ == "__main__":
    keys = read_env_keys(os.path.expanduser("~/modeling-agentic/.env"))
    code, commit = code_tarball(TREE)
    print(f"code @ {commit}", flush=True)
    cluster = coiled.Cluster(name=f"pipeline-smoke-{time.strftime('%H%M%S')}-gcc", workspace="idm-coiled-idmad-r2", n_workers=1,
                             worker_cpu=8, worker_memory="32 GiB", scheduler_cpu=2, spot_policy="on-demand",
                             idle_timeout="1 hour", shutdown_on_close=True)
    try:
        client = cluster.get_client()
        client.wait_for_workers(1, timeout=1500)
        client.upload_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab_coiled.py"))
        res = client.submit(run_side, code, keys["ANTHROPIC_API_KEY"], QUESTION, FLAGS, pure=False).result(timeout=6000)
        dest = os.path.join(SCRATCH, "pipeline_smoke_p34")
        os.makedirs(dest, exist_ok=True)
        if res.get("tar"):
            open(os.path.join(dest, "smoke.tar.gz"), "wb").write(res.pop("tar"))
        json.dump(res, open(os.path.join(dest, "result.json"), "w"), indent=1, default=str)
        print(json.dumps(res, indent=1, default=str)[:8000])
        pl = res.get("pipeline") or {}
        checks = {"preflight_ok": (res.get("preflight") or {}).get("rc") == 0,
                  "pipeline_wrote_report": pl.get("report_md") is True,
                  "final_gate_ran": pl.get("final_gate_ran") is True,
                  "stored": bool(pl.get("stored"))}
        print("SMOKE CHECKS:", json.dumps(checks))
        if not all(checks.values()):
            print("SMOKE FAILED")
            sys.exit(1)
        print("SMOKE PASSED")
    finally:
        cluster.shutdown()
