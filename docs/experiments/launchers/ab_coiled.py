"""Run the pre-registered Phase 21 A/B (2 pairs) on IDM Coiled.

Each run is one @coiled.function call on its own VM in the default,
non-sensitive workspace (idm-coiled-idmad-r2). Code goes in as a
`git archive` tarball argument; the finished run directory comes back as
the return value (no storage account needed). API keys are passed as
arguments over Coiled's TLS connection and never written to disk logs.

Usage (from the dedicated coiled-env interpreter):
    python ab_coiled.py            # launch B1,T1,B2,T2 in parallel
    python ab_coiled.py --only T1  # a single run
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import os
import subprocess
import sys
import tarfile
import time

import coiled

HOME = os.path.expanduser("~")
SCRATCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARMS = {
    "B": {"tree": f"{HOME}/modeling-agentic-baseline", "flags": [], "openai": False},
    "T": {"tree": f"{HOME}/modeling-agentic-treatment", "flags": ["--cross-model"], "openai": True},
}
CODEX_VERSION = "0.163.0"
MAX_RETURN_FILE = 50 * 2**20


def read_env_keys(path: str) -> dict[str, str]:
    keys = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                keys[k] = v.strip().strip('"').strip("'")
    return keys


def code_tarball(tree: str) -> tuple[bytes, str]:
    commit = subprocess.check_output(["git", "-C", tree, "rev-parse", "--short", "HEAD"], text=True).strip()
    blob = subprocess.check_output(["git", "-C", tree, "archive", "--format=tar.gz", "HEAD"])
    return blob, commit


def run_job_impl(label: str, code: bytes, question: str, flags: list[str],
                 anthropic_key: str, openai_key: str | None) -> dict:
    """Plain (undecorated) body, shared by the serverless and cluster launchers."""
    import glob, os, shutil, subprocess, sys, tarfile, io, time, urllib.request
    work = f"/tmp/ab/{label}"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    tarfile.open(fileobj=io.BytesIO(code), mode="r:gz").extractall(work)
    bindir = os.path.dirname(sys.executable)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "OPENAI", "AZ_", "AZURE_"))}
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env["ANTHROPIC_API_KEY"] = anthropic_key
    notes = []
    if openai_key:
        # Codex CLI for the cross-model critic (treatment only).
        arch = "x86_64" if os.uname().machine in ("x86_64", "amd64") else "aarch64"
        url = (f"https://github.com/openai/codex/releases/download/rust-v{CODEX_VERSION}/"
               f"codex-{arch}-unknown-linux-musl.tar.gz")
        bin_home = os.path.expanduser("~/.local/bin"); os.makedirs(bin_home, exist_ok=True)
        try:
            data = urllib.request.urlopen(url, timeout=120).read()
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
                member = next(m for m in t.getmembers() if m.isfile() and "codex" in os.path.basename(m.name))
                with open(os.path.join(bin_home, "codex"), "wb") as f:
                    f.write(t.extractfile(member).read())
            os.chmod(os.path.join(bin_home, "codex"), 0o755)
            env["PATH"] = bin_home + os.pathsep + env["PATH"]
            os.makedirs(os.path.expanduser("~/.codex"), exist_ok=True)
            with open(os.path.expanduser("~/.codex/config.toml"), "w") as f:
                f.write('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n')
            r = subprocess.run(["codex", "login", "--with-api-key"], input=openai_key, text=True,
                               capture_output=True, env=env, timeout=120)
            notes.append(f"codex login rc={r.returncode} {(r.stdout + r.stderr).strip()[:120]}")
        except Exception as e:  # cross-model critic reports UNAVAILABLE; run continues
            notes.append(f"codex setup failed: {type(e).__name__}: {e}")
    if question == "__SMOKE__":
        smoke = (
            "import asyncio, os, sys\n"
            "sys.path.insert(0, '.')\n"
            "from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, UserMessage\n"
            "from agents import enforcement\n"
            "o = ClaudeAgentOptions(model=enforcement.MODEL_FAST, tools=['Bash'], allowed_tools=['Bash'],\n"
            "    permission_mode='bypassPermissions', setting_sources=['project'], cwd=os.getcwd(),\n"
            "    env=enforcement.filtered_env(dict(os.environ), False), max_turns=3)\n"
            "cmd = 'curl -s -m 20 -o /dev/null -w HTTP_%{http_code} https://api.dhsprogram.com/rest/dhs/surveys?f=json'\n"
            "async def go():\n"
            "    async for m in query(prompt='Run exactly with Bash and reply with raw output only: ' + cmd, options=o):\n"
            "        if isinstance(m, UserMessage) and isinstance(m.content, list):\n"
            "            for b in m.content:\n"
            "                if getattr(b, 'content', None) is not None: print('TOOL_RESULT', str(b.content)[:120])\n"
            "        if isinstance(m, ResultMessage): print('DENIALS', len(m.permission_denials or []))\n"
            "asyncio.run(go())\n")
        outs = []
        for cmd in ([sys.executable, "main.py", "--preflight-only", *flags], [sys.executable, "-c", smoke]):
            r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=900)
            outs.append(f"$ {' '.join(cmd[:3])} -> rc={r.returncode}\n" + "\n".join(
                l for l in (r.stdout + r.stderr).splitlines() if any(k in l for k in
                ("Preflight", "PROBLEM", "cross-model", "TOOL_RESULT", "DENIALS", "Error", "Traceback")))[-1500:])
        return {"label": label, "notes": notes, "smoke": outs}
    t0 = time.time()
    log_path = os.path.join(work, "run.log")
    with open(log_path, "w") as log:
        proc = subprocess.run([sys.executable, "main.py", question, "--max-rounds", "8",
                               "--max-sessions", "5", *flags],
                              cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    runs = sorted(glob.glob(os.path.join(work, "runs", "*")), key=os.path.getmtime)
    buf = io.BytesIO(); skipped = []
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        if runs:
            for root, _, files in os.walk(runs[-1]):
                for fn in files:
                    p = os.path.join(root, fn)
                    if os.path.getsize(p) > MAX_RETURN_FILE:
                        skipped.append((os.path.relpath(p, work), os.path.getsize(p)))
                        continue
                    t.add(p, arcname=os.path.relpath(p, work))
        t.add(log_path, arcname="run.log")
    return {"label": label, "exit_code": proc.returncode, "duration_s": round(time.time() - t0),
            "run_dir": os.path.basename(runs[-1]) if runs else None, "skipped_large_files": skipped,
            "notes": notes, "tarball": buf.getvalue()}


run_job = coiled.function(workspace="idm-coiled-idmad-r2", cpu=8, memory="32 GiB",
                          n_workers=4, threads_per_worker=1, idle_timeout="20 minutes",
                          disk_size=100, spot_policy="on-demand")(run_job_impl)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=["B1", "T1", "B2", "T2"])
    ap.add_argument("--smoke", action="store_true", help="treatment preflight + nested curl only")
    ap.add_argument("--collect-test", action="store_true", help="exercise submit/poll/collect on a tiny job")
    args = ap.parse_args()
    if args.collect_test:
        @coiled.function(workspace="idm-coiled-idmad-r2", cpu=2, memory="8 GiB", n_workers=2, idle_timeout="5 minutes")
        def tiny(i):
            import time, io, tarfile; time.sleep(20 + 10 * i)
            buf = io.BytesIO(); t = tarfile.open(fileobj=buf, mode="w:gz"); t.close()
            return {"i": i, "tarball": buf.getvalue()}
        futs = {tiny.submit(i): i for i in range(2)}
        pending = dict(futs)
        while pending:
            done = [f for f in pending if f.done()]
            if not done:
                time.sleep(5); continue
            f = done[0]; i = pending.pop(f); r = f.result()
            print("collected", i, len(r["tarball"]), "bytes")
        return 0
    if args.smoke:
        keys = read_env_keys(f"{HOME}/modeling-agentic/.env")
        code, commit = code_tarball(ARMS["T"]["tree"])
        res = run_job("SMOKE", code, "__SMOKE__", ["--cross-model"], keys["ANTHROPIC_API_KEY"], keys.get("OPENAI_API_KEY"))
        print(f"treatment @ {commit}"); print("notes:", res["notes"]); print("\n".join(res["smoke"]))
        return 0
    question = open(os.path.join(SCRATCH, "ab_question.txt")).read().strip()
    keys = read_env_keys(f"{HOME}/modeling-agentic/.env")
    out_dir = os.path.join(SCRATCH, "ab_cloud"); os.makedirs(out_dir, exist_ok=True)
    log = open(os.path.join(out_dir, "launcher.log"), "a")
    def say(msg):
        line = f"[{time.strftime('%F %T')}] {msg}"; print(line, flush=True); log.write(line + "\n"); log.flush()
    codes = {arm: code_tarball(cfg["tree"]) for arm, cfg in ARMS.items()}
    for arm, (_, commit) in codes.items():
        say(f"arm {arm}: {ARMS[arm]['tree']} @ {commit}")
    futures = {}
    for label in args.only:
        arm = label[0]; cfg = ARMS[arm]
        futures[run_job.submit(label, codes[arm][0], question, cfg["flags"],
                               keys["ANTHROPIC_API_KEY"],
                               keys.get("OPENAI_API_KEY") if cfg["openai"] else None)] = label
        say(f"submitted {label}")
        time.sleep(30)  # distinct run-dir timestamps
    pending = dict(futures)
    while pending:
        done = [f for f in pending if f.done()]
        if not done:
            time.sleep(60)
            continue
        fut = done[0]
        label = pending.pop(fut)
        try:
            res = fut.result()
        except Exception as e:
            say(f"{label}: FAILED {type(e).__name__}: {e}"); continue
        dest = os.path.join(out_dir, label); os.makedirs(dest, exist_ok=True)
        tarfile.open(fileobj=io.BytesIO(res.pop("tarball")), mode="r:gz").extractall(dest)
        json.dump(res, open(os.path.join(dest, "result.json"), "w"), indent=1)
        say(f"{label}: exit={res['exit_code']} {res['duration_s']}s run_dir={res['run_dir']} "
            f"skipped={len(res['skipped_large_files'])} notes={res['notes']}")
    say("all submitted runs returned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
