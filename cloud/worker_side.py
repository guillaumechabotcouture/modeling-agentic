"""Code that runs on the cloud VMs (uploaded with client.upload_file, so it
imports nothing from this repo at module level).

The agent VM runs one pipeline run (`run_to_disk`), stores it, and shuts the
cluster down once every run of the launch is in storage. The keeper VM holds
the compute pool's autoscaler (`keeper_task`). The small `*_side` functions
are called with client.run by `run_cluster` (status, collect, brief, sign,
report).
"""
from __future__ import annotations

MAX_RETURN_FILE = 50 * 2**20
CODEX_VERSION = "0.163.0"
WORK = "/tmp/ab"
LIVE_STATES = ("running", "ready", "pending", "starting", "scaling")


def run_job_impl(label, code, question, flags, anthropic_key, openai_key, max_rounds=8, max_sessions=5):
    """Unpack the code, run main.py, return the run directory as a tarball."""
    import glob, io, os, shutil, subprocess, sys, tarfile, time, urllib.request
    work = f"{WORK}/{label}"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    tarfile.open(fileobj=io.BytesIO(code), mode="r:gz").extractall(work)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "OPENAI", "AZ_", "AZURE_"))}
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env["ANTHROPIC_API_KEY"] = anthropic_key
    notes = []
    if openai_key:   # Codex CLI for the cross-model critic
        arch = "x86_64" if os.uname().machine in ("x86_64", "amd64") else "aarch64"
        url = (f"https://github.com/openai/codex/releases/download/rust-v{CODEX_VERSION}/"
               f"codex-{arch}-unknown-linux-musl.tar.gz")
        bin_home = os.path.expanduser("~/.local/bin")
        os.makedirs(bin_home, exist_ok=True)
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
        except Exception as e:  # noqa: BLE001 — the cross-model critic reports UNAVAILABLE; the run continues
            notes.append(f"codex setup failed: {type(e).__name__}: {e}")
    t0 = time.time()
    log_path = os.path.join(work, "run.log")
    with open(log_path, "w") as log:
        proc = subprocess.run([sys.executable, "main.py", question, "--max-rounds", str(max_rounds),
                               "--max-sessions", str(max_sessions), *flags],
                              cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    runs = sorted(glob.glob(os.path.join(work, "runs", "*")), key=os.path.getmtime)
    buf, skipped = io.BytesIO(), []
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        if runs:
            for root, _, files in os.walk(runs[-1]):
                for fn in files:
                    p = os.path.join(root, fn)
                    if os.path.getsize(p) > MAX_RETURN_FILE:   # the stored copy has them
                        skipped.append((os.path.relpath(p, work), os.path.getsize(p)))
                        continue
                    t.add(p, arcname=os.path.relpath(p, work))
        t.add(log_path, arcname="run.log")
    return {"label": label, "exit_code": proc.returncode, "duration_s": round(time.time() - t0),
            "run_dir": os.path.basename(runs[-1]) if runs else None, "skipped_large_files": skipped,
            "notes": notes, "tarball": buf.getvalue()}


def run_stored(tarball):
    """The blob main.py recorded in metadata.json after a verified upload, or None."""
    import io, json, tarfile
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as t:
            for m in t.getmembers():
                if m.name.endswith("/metadata.json") and m.name.count("/") == 2:   # runs/<run>/metadata.json
                    meta = json.load(t.extractfile(m))
                    return meta.get("stored") if not meta.get("store_error") else None
    except (tarfile.TarError, ValueError, OSError):
        return None
    return None


def record_blob(prefix, launch_id, label):
    """Result records live under the launch that wrote them, so a record an
    earlier launch left under the same prefix never counts for this one."""
    return f"outputs/{prefix}/launch_{launch_id}/result_{label}.json"


def all_stored(store, prefix, account, labels, launch_id):
    """Every label of this launch has its result record in storage."""
    have = {b["name"] for b in store.list_blobs(f"outputs/{prefix}/launch_{launch_id}/", account)}
    return all(record_blob(prefix, launch_id, lb) in have for lb in labels)


def run_to_disk(label, code, question, flags, anthropic_key, openai_key, labels, prefix, account, launch_id,
                max_rounds=8, max_sessions=5, shutdown_when_stored=True):
    """The agent VM's task: run, store the run, its transcripts and its result
    record, then (last run of the launch) shut the cluster down. If any upload
    failed, hold the results on disk and stay busy until `collect` takes them:
    an idle VM is the one an autoscaling pool retires first."""
    import io, json, os, re, subprocess, sys, tarfile, tempfile, time
    td = tempfile.mkdtemp()
    tarfile.open(fileobj=io.BytesIO(code), mode="r:gz").extractall(td)
    pins = [ln.split("#")[0].strip() for ln in open(os.path.join(td, "requirements.txt"))
            if re.match(r"\s*claude-agent-sdk==", ln)]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pins], check=True, timeout=600)
    sys.path.insert(0, os.path.join(td, "scripts"))
    import compute
    import results_store
    from distributed import get_worker
    # Compute service: this VM's scheduler connection for the agent's batches; it takes no compute work itself.
    os.environ.update(compute.export_worker_env(os.path.join(os.path.expanduser("~"), ".ma_tls")))
    os.environ["MA_COMPUTE_EXCLUDE_WORKERS"] = get_worker().address
    res = run_job_impl(label, code, question, flags, anthropic_key, openai_key, max_rounds, max_sessions)
    d = f"{WORK}/{label}"
    try:   # the CLI transcripts (per-stage cost and time) exist only on this VM
        blob, _ = results_store.archive_dir(os.path.expanduser("~/.claude/projects"), "transcripts",
                                            [anthropic_key, openai_key or ""])
        open(f"{d}/transcripts.tar.gz", "wb").write(blob)
        res["transcripts_stored"] = results_store.upload_retry(
            f"{d}/transcripts.tar.gz", f"outputs/{prefix}/transcripts_{label}.tar.gz", account)["blob"]
    except Exception as e:  # noqa: BLE001
        res["transcripts_store_error"] = f"{type(e).__name__}: {e}"[:300]
    tarball = res.pop("tarball")
    open(f"{d}/result.tar.gz", "wb").write(tarball)
    res["run_stored"] = run_stored(tarball)
    json.dump(res, open(f"{d}/result.json", "w"))
    if res["run_stored"] and res.get("transcripts_stored"):
        try:
            results_store.upload_retry(f"{d}/result.json", record_blob(prefix, launch_id, label), account)
            open(f"{d}/collected", "w").write("stored")
            if shutdown_when_stored and all_stored(results_store, prefix, account, labels, launch_id):
                from distributed import get_client
                print(f"{label}: every run of this launch is in storage; shutting the cluster down", flush=True)
                get_client().shutdown()
            return res
        except Exception as e:  # noqa: BLE001 — hold the results for `collect`
            res["result_store_error"] = f"{type(e).__name__}: {e}"[:300]
            json.dump(res, open(f"{d}/result.json", "w"))
    t_end = time.time() + 24 * 3600
    while time.time() < t_end and not os.path.exists(f"{d}/collected"):
        time.sleep(30)
    return res


def keeper_task(token, name, workspace, lo, hi, hours, adapt_options):
    """On the keeper VM: hold the run cluster's adaptive scaling (dask's Adaptive
    loop lives in the process that calls adapt). Attach-only; reconnects after
    a dropped connection; exits when the run cluster stops."""
    import contextlib, time
    import coiled
    import dask
    dask.config.set({"coiled.token": token})

    def alive():
        cs = coiled.list_clusters(workspace=workspace)
        items = cs if isinstance(cs, list) else list(cs.values())
        return any(c.get("name") == name and state_of(c) in LIVE_STATES for c in items)

    with open("/tmp/keeper.log", "a", buffering=1) as f, contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        print(f"{time.strftime('%T')} keeper holding {name} {lo}..{hi}", flush=True)
        t_end = time.time() + hours * 3600
        while time.time() < t_end:
            try:
                if not alive():
                    print(f"{time.strftime('%T')} {name} is not running; keeper exiting", flush=True)
                    return "keeper exited"
                cl = coiled.Cluster(name=name, workspace=workspace, shutdown_on_close=False, wait_for_workers=False)
                cl.adapt(minimum=lo, maximum=hi, **adapt_options)
                c = cl.get_client()
                while time.time() < t_end:
                    try:
                        if not alive():
                            break
                    except Exception:  # noqa: BLE001 — an API hiccup is not a stop
                        pass
                    print(f"{time.strftime('%T')} workers={len(c.scheduler_info()['workers'])}", flush=True)
                    time.sleep(120)
            except Exception as e:  # noqa: BLE001 — reconnect
                print(f"{time.strftime('%T')} keeper error {type(e).__name__}: {str(e)[:200]}; retrying", flush=True)
                time.sleep(60)
    return "keeper exited"


def state_of(c):
    s = c.get("current_state")
    return str((s.get("state") if isinstance(s, dict) else s) or "").lower()


def keeper_log():
    import os
    p = "/tmp/keeper.log"
    return open(p).read()[-3000:] if os.path.exists(p) else "(no log yet)"


def fetch_meta(label):
    """State plus result record, tarball size and sha256 (no payload)."""
    import hashlib, json, os
    d = f"{WORK}/{label}"
    if not os.path.exists(f"{d}/result.json"):
        running = os.path.exists(f"{d}/run.log")
        return {"state": "running" if running else "missing",
                "log_bytes": os.path.getsize(f"{d}/run.log") if running else 0}
    h = hashlib.sha256()
    with open(f"{d}/result.tar.gz", "rb") as f:
        for b in iter(lambda: f.read(2**20), b""):
            h.update(b)
    return {"state": "done", "meta": json.load(open(f"{d}/result.json")),
            "size": os.path.getsize(f"{d}/result.tar.gz"), "sha256": h.hexdigest()}


def fetch_chunk(label, offset, length):
    with open(f"{WORK}/{label}/result.tar.gz", "rb") as f:
        f.seek(offset)
        return f.read(length)


def mark_collected(label):
    open(f"{WORK}/{label}/collected", "w").write("ok")
    return True


def _run_dir(label):
    import glob
    rds = sorted(glob.glob(f"{WORK}/{label}/runs/*/"))
    return rds[-1] if rds else None


def brief_side(label):
    import os, subprocess, sys
    rd = _run_dir(label)
    if not rd:
        return {"state": "no run directory yet"}
    r = subprocess.run([sys.executable, "scripts/decision_brief.py", "status", rd], cwd=f"{WORK}/{label}",
                       capture_output=True, text=True)
    md = open(rd + "decision_brief.md").read() if os.path.exists(rd + "decision_brief.md") else None
    return {"run_dir": rd, "status": r.stdout.strip() or r.stderr.strip(), "brief_md": md}


def sign_side(label, answers_yaml, note):
    import os, subprocess, sys, tempfile
    rd = _run_dir(label)
    if not rd:
        return {"rc": 1, "out": "no run directory yet"}
    fd, p = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, answers_yaml.encode())
    os.close(fd)
    env = {k: v for k, v in os.environ.items() if k != "MA_SIGNOFF_MODE"}
    r = subprocess.run([sys.executable, "scripts/decision_brief.py", "sign", rd, "--by", "owner",
                        "--answers", p, "--note", note], cwd=f"{WORK}/{label}", env=env,
                       capture_output=True, text=True)
    return {"rc": r.returncode, "out": (r.stdout + r.stderr).strip()}


def report_side(label):
    import subprocess, sys
    rd = _run_dir(label)
    if not rd:
        return "no run directory yet"
    r = subprocess.run([sys.executable, "scripts/convergence_report.py", rd], cwd=f"{WORK}/{label}",
                       capture_output=True, text=True, timeout=300)
    return r.stdout or r.stderr[-1500:]
