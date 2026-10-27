"""E7 attempt 7 on IDM Coiled (Phases 31-34), robust to disconnects.

    python e7r7_cluster.py launch          # start cluster, fire-and-forget R1
    python e7r7_cluster.py brief           # print the decision brief waiting for sign-off
    python e7r7_cluster.py sign <answers.yaml> [note]   # sign it as the owner (answers: Q-id -> answer)
    python e7r7_cluster.py report          # convergence measures so far (findings, tests, holds, compute, spend)
    python e7r7_cluster.py status          # run state (running / done / missing)
    python e7r7_cluster.py collect         # pull the finished run dir
    python e7r7_cluster.py shutdown        # stop the cluster once collected

The run pauses after planning until the owner signs (--signoff owner, up to
24 h); `sign` runs scripts/decision_brief.py sign on the run's VM.
Compute autoscales only while pool_keeper.py holds the loop (see `launch`).

Each run is pinned to its own worker and submitted with fire_and_forget, so
it keeps running if this process exits or the connection drops. Results are
written to the worker's local disk and fetched later by a pinned task.
Workspace: idm-coiled-idmad-r2 (default, non-sensitive, Foundation Azure).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time

import coiled
from distributed import fire_and_forget

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_coiled import SCRATCH, code_tarball, read_env_keys  # noqa: E402

WORKSPACE = "idm-coiled-idmad-r2"
CLUSTER_NAME = "e7r7-quality-gcc"
LABELS = ["R1"]
COMPUTE_START = 1        # compute VMs at launch and the pool's floor: batches always progress (8 cores) even if the
                         # keeper stops; idle VMs cost money, the pool grows to COMPUTE_MAX in ~1 min when work queues
COMPUTE_MAX = 24         # 8-core VMs: 192 cores (E7 attempt 6 had 4 fixed VMs; its grid timed out half-done)
COMPUTE_ENV = {"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": str(COMPUTE_MAX),
               "MA_COMPUTE_MAX_CORE_HOURS": "1500", "MA_COMPUTE_CALL_CORE_HOURS": "400"}
OUT = os.path.join(SCRATCH, "e7r7_cloud")
TREE = os.path.expanduser("~/modeling-agentic-launch")
FLAGS = ["--max-budget-usd", "600", "--signoff", "owner", "--signoff-hours", "24"]   # run_job_impl adds --max-rounds 8 --max-sessions 5
ARMS = {"R": {"tree": TREE, "flags": FLAGS, "openai": False}}
STATE = os.path.join(OUT, "assignments.json")
QUESTION_FILE = os.path.join(SCRATCH, "e7_question.txt")
SMOKE = os.environ.get("E7R7_SMOKE") == "1"
if SMOKE:   # the same launcher end to end on a tiny question: Haiku agents, a small pool, owner sign-off
    CLUSTER_NAME, LABELS, COMPUTE_START, COMPUTE_MAX = "e7r7-smoke-gcc", ["S1"], 1, 4
    COMPUTE_ENV = dict(COMPUTE_ENV, MA_COMPUTE_TARGET_WORKERS=str(COMPUTE_MAX), MA_COMPUTE_MAX_CORE_HOURS="20",
                       MA_COMPUTE_CALL_CORE_HOURS="10", MA_SMOKE_MODEL="claude-haiku-5-5", MA_SMOKE_MAX_TURNS="14")
    OUT = os.path.join(SCRATCH, "e7r7_smoke")
    TREE = os.path.expanduser(os.environ.get("SMOKE_TREE", "~/modeling-agentic-lessons"))
    FLAGS = ["--max-budget-usd", "9", "--max-rounds", "2", "--signoff", "owner", "--signoff-hours", "2"]
    ARMS = {"S": {"tree": TREE, "flags": FLAGS, "openai": False}}
    STATE = os.path.join(OUT, "assignments.json")
    QUESTION_FILE = os.path.join(OUT, "question.txt")


def _require_running(name, workspace):
    """Attach-only guard: coiled.Cluster(name=...) silently CREATES a fresh,
    empty cluster when none is running (that is how the measles resume was
    lost on 2026-10-21). Refuse instead, so a status/collect call can never
    replace the cluster whose worker disks hold results."""
    cs = coiled.list_clusters(workspace=workspace)
    items = cs if isinstance(cs, list) else list(cs.values())
    live = [c for c in items if c.get("name") == name and
            (c.get("current_state", {}).get("state") if isinstance(c.get("current_state"), dict)
             else c.get("current_state")) in ("running", "ready", "pending", "starting", "scaling")]
    if not live:
        raise SystemExit(f"cluster {name!r} is not running; refusing to create a new one")


def get_cluster(create: bool):
    if not create:
        _require_running(CLUSTER_NAME, WORKSPACE)
    kw = dict(name=CLUSTER_NAME, workspace=WORKSPACE, shutdown_on_close=False)
    if create:
        sys.path.insert(0, os.path.join(TREE, "scripts"))
        import compute
        kw.update(n_workers=len(LABELS) + COMPUTE_START, worker_cpu=8, worker_memory="64 GiB",
                  worker_disk_size=100, idle_timeout="12 hours", worker_options=compute.WORKER_OPTIONS,
                  environ={"MA_STORAGE_ACCOUNT": "modelingagentic", "MA_STORAGE_PREFIX": "_checks/e7r7-smoke" if SMOKE else "runs/e7r7",
                           **COMPUTE_ENV},
                  spot_policy="on-demand", scheduler_cpu=2)
    return coiled.Cluster(**kw)


def on_worker(client, fn, *args, worker, timeout=900):
    """Run fn on one worker outside its task threads: with one thread per
    worker (Phase 30), the agent VM's only thread is busy with the run, so a
    pinned task would queue behind it for the whole run."""
    return client.run(fn, *args, workers=[worker], wait=True, on_error="raise")[worker]


def run_to_disk(label, code, question, flags, anthropic_key, openai_key):
    """Executes on the worker; same steps as ab_coiled.run_job, but persists
    the result to local disk instead of returning it."""
    import json, os, re, subprocess, sys, io, tarfile, tempfile
    from ab_coiled import run_job_impl
    td = tempfile.mkdtemp()
    tarfile.open(fileobj=io.BytesIO(code), mode="r:gz").extractall(td)
    pins = [l.split("#")[0].strip() for l in open(os.path.join(td, "requirements.txt"))
            if re.match(r"\s*claude-agent-sdk==", l)]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pins], check=True, timeout=600)
    # Compute service (Phase 24): this worker's scheduler connection for the
    # agent's simulation batches; this VM itself takes no compute work.
    sys.path.insert(0, os.path.join(td, "scripts"))
    import compute
    from distributed import get_worker
    os.environ.update(compute.export_worker_env(os.path.join(os.path.expanduser("~"), ".ma_tls")))
    os.environ["MA_COMPUTE_EXCLUDE_WORKERS"] = get_worker().address
    res = run_job_impl(label, code, question, flags, anthropic_key, openai_key)
    # The CLI transcripts (per-stage cost/time) live only on this VM: store them too.
    try:
        sys.path.insert(0, os.path.join(td, "scripts"))
        import results_store
        tdir = os.path.expanduser("~/.claude/projects")
        blob, skipped = results_store.archive_dir(tdir, "transcripts", [anthropic_key, openai_key or ""])
        tp = f"/tmp/ab/{label}/transcripts.tar.gz"
        open(tp, "wb").write(blob)
        res["transcripts_stored"] = results_store.upload_retry(
            tp, f"outputs/{os.environ.get('MA_STORAGE_PREFIX', 'runs')}/transcripts_{label}.tar.gz",
            os.environ.get("MA_STORAGE_ACCOUNT"))["blob"]
    except Exception as e:  # noqa: BLE001 — the run result itself is already stored by main.py
        res["transcripts_store_error"] = f"{type(e).__name__}: {e}"[:300]
    d = f"/tmp/ab/{label}"
    with open(f"{d}/result.tar.gz", "wb") as f:
        f.write(res.pop("tarball"))
    with open(f"{d}/result.json", "w") as f:
        json.dump(res, f)
    # Stay busy until `collect` has the results (Codex review): an idle agent VM
    # is the one an autoscaling pool retires first, with the results on its disk.
    import time as _t
    t_end = _t.time() + 24 * 3600
    while _t.time() < t_end and not os.path.exists(f"{d}/collected"):
        _t.sleep(30)
    return {k: v for k, v in res.items()}


def _mark_collected(label):
    import os
    open(f"/tmp/ab/{label}/collected", "w").write("ok")
    return True


def fetch(label):
    import os, json
    d = f"/tmp/ab/{label}"
    if not os.path.exists(f"{d}/result.json"):
        running = os.path.exists(f"{d}/run.log")
        size = os.path.getsize(f"{d}/run.log") if running else 0
        return {"state": "running" if running else "missing", "log_bytes": size}
    with open(f"{d}/result.json") as f:
        meta = json.load(f)
    with open(f"{d}/result.tar.gz", "rb") as f:
        return {"state": "done", "meta": meta, "tarball": f.read()}


def launch():
    os.makedirs(OUT, exist_ok=True)
    question = open(QUESTION_FILE).read().strip()
    keys = read_env_keys(os.path.expanduser("~/modeling-agentic/.env"))
    # Guard (Codex launch review #1): ship exactly origin/main, and only if it
    # has the pieces this launch depends on.
    import subprocess as _sp, tarfile as _tf, io as _io
    _sp.run(["git", "-C", TREE, "fetch", "-q", "origin"], check=True)
    head = _sp.check_output(["git", "-C", TREE, "rev-parse", "HEAD"], text=True).strip()
    main_ = _sp.check_output(["git", "-C", TREE, "rev-parse", "origin/main"], text=True).strip()
    if (head != main_ and not SMOKE) or _sp.check_output(["git", "-C", TREE, "status", "--porcelain"], text=True).strip():
        raise SystemExit(f"launch tree is at {head[:7]}, origin/main is {main_[:7]} (or dirty): refusing")
    codes = {arm: code_tarball(cfg["tree"]) for arm, cfg in ARMS.items()}
    names = set(_tf.open(fileobj=_io.BytesIO(next(iter(codes.values()))[0]), mode="r:gz").getnames())
    need = ["agents/observability.py", "scripts/watch_run.py", "scripts/results_store.py", "scripts/sdk_scenarios.py",
            "scripts/open_problems.py", "scripts/decision_brief.py", "scripts/done_when.py",
            "scripts/lib/transcript_cost.py"]
    missing = [n for n in need if not any(x.endswith(n) for x in names)]
    if missing:
        raise SystemExit(f"code to launch lacks {missing}: refusing")
    cluster = get_cluster(create=True)
    client = cluster.get_client()
    client.wait_for_workers(len(LABELS) + COMPUTE_START, timeout=1800)
    client.upload_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab_coiled.py"))
    workers = sorted(client.scheduler_info()["workers"])[:len(LABELS)]
    import compute
    compute.register_compute_tags(client, exclude=workers)   # the run VMs never take compute tasks
    assign = {}
    for label, w in zip(LABELS, workers):
        arm = label[0]
        cfg = ARMS[arm]
        fut = client.submit(run_to_disk, label, codes[arm][0], question, cfg["flags"],
                            keys["ANTHROPIC_API_KEY"],
                            keys.get("OPENAI_API_KEY") if cfg["openai"] else None,
                            workers=[w], allow_other_workers=False, pure=False, key=f"e7-{label}")
        fire_and_forget(fut)
        assign[label] = {"worker": w, "arm": arm, "commit": codes[arm][1],
                         "submitted": time.strftime("%F %T")}
        print(f"fired {label} -> {w} ({arm} @ {codes[arm][1]})", flush=True)
    json.dump(assign, open(STATE, "w"), indent=1)
    time.sleep(10)
    cluster.adapt(minimum=len(LABELS) + COMPUTE_START, maximum=len(LABELS) + COMPUTE_MAX, **compute.ADAPT_OPTIONS)
    print(f"adaptive pool: {COMPUTE_START}-{COMPUTE_MAX} compute VMs")
    print("launched. The autoscaler runs in the process that holds it (autoscale check 7): start the keeper,\n"
          f"  caffeinate -i nohup python pool_keeper.py {CLUSTER_NAME} {len(LABELS) + COMPUTE_START} "
          f"{len(LABELS) + COMPUTE_MAX} 48 > keeper.log 2>&1 &\n"
          "If it stops, the pool stays at its floor; the run and its results do not depend on it.")


CHUNK = 8 * 2**20


def fetch_meta(label):
    """Small: state + result metadata + tarball size and sha256 (no payload)."""
    import os, json, hashlib
    d = f"/tmp/ab/{label}"
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
    with open(f"/tmp/ab/{label}/result.tar.gz", "rb") as f:
        f.seek(offset)
        return f.read(length)


def download(client, label, assign, dest_file):
    """Chunked, resumable, sha256-verified download (a single 591 MB message
    reset the Coiled proxy connection)."""
    import hashlib
    w = assign[label]["worker"]
    m = on_worker(client, fetch_meta, label, worker=w)
    if m["state"] != "done":
        return m
    have = os.path.getsize(dest_file) if os.path.exists(dest_file) else 0
    with open(dest_file, "ab") as out:
        while have < m["size"]:
            for attempt in range(5):
                try:
                    b = on_worker(client, fetch_chunk, label, have, CHUNK, worker=w)
                    break
                except Exception:
                    if attempt == 4:
                        raise
                    time.sleep(10 * (attempt + 1))
            out.write(b); out.flush(); have += len(b)
    h = hashlib.sha256()
    with open(dest_file, "rb") as f:
        for b in iter(lambda: f.read(2**20), b""):
            h.update(b)
    if h.hexdigest() != m["sha256"]:
        os.remove(dest_file)
        raise RuntimeError(f"{label}: checksum mismatch; partial file removed, retry")
    return m


def _pinned(client, label, assign):
    return on_worker(client, fetch, label, worker=assign[label]["worker"])


def status(collect=False):
    assign = json.load(open(STATE))
    cluster = get_cluster(create=False)
    client = cluster.get_client()
    for label in LABELS:
        if collect and os.path.exists(os.path.join(OUT, label, "result.json")):
            print(f"{label}: already collected"); continue
        w = assign[label]["worker"]
        m = on_worker(client, fetch_meta, label, worker=w)
        if m["state"] != "done":
            print(f"{label}: {m['state']} (log {m.get('log_bytes', 0)} bytes)"); continue
        meta = m["meta"]
        print(f"{label}: done exit={meta['exit_code']} {meta['duration_s']}s run_dir={meta['run_dir']} tar={m['size']>>20}MB")
        if collect:
            dest = os.path.join(OUT, label); os.makedirs(dest, exist_ok=True)
            tgz = os.path.join(OUT, f"{label}.result.tar.gz")
            download(client, label, assign, tgz)
            tarfile.open(tgz, mode="r:gz").extractall(dest)
            json.dump({**meta, **assign[label], "sha256": m["sha256"]}, open(os.path.join(dest, "result.json"), "w"), indent=1)
            print(f"  collected + verified -> {dest}")
            on_worker(client, _mark_collected, label, worker=w)   # the run's task may now end
    return


def _status_old(collect=False):
    assign = json.load(open(STATE))
    cluster = get_cluster(create=False)
    client = cluster.get_client()
    for label in LABELS:
        r = _pinned(client, label, assign)
        if r["state"] != "done":
            print(f"{label}: {r['state']} (log {r.get('log_bytes', 0)} bytes)")
            continue
        meta = r["meta"]
        print(f"{label}: done exit={meta['exit_code']} {meta['duration_s']}s run_dir={meta['run_dir']}")
        if collect:
            dest = os.path.join(OUT, label)
            if os.path.exists(os.path.join(dest, "result.json")):
                continue
            os.makedirs(dest, exist_ok=True)
            tarfile.open(fileobj=io.BytesIO(r["tarball"]), mode="r:gz").extractall(dest)
            json.dump({**meta, **assign[label]}, open(os.path.join(dest, "result.json"), "w"), indent=1)
            print(f"  collected -> {dest}")


def relaunch(label):
    """Re-fire one run on its pinned worker (e.g. after a preflight-only fix)."""
    assign = json.load(open(STATE))
    question = open(QUESTION_FILE).read().strip()
    keys = read_env_keys(os.path.expanduser("~/modeling-agentic/.env"))
    arm = label[0]; cfg = ARMS[arm]
    code, commit = code_tarball(cfg["tree"])
    client = get_cluster(create=False).get_client()
    client.upload_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab_coiled.py"))
    fut = client.submit(run_to_disk, label, code, question, cfg["flags"], keys["ANTHROPIC_API_KEY"],
                        keys.get("OPENAI_API_KEY") if cfg["openai"] else None,
                        workers=[assign[label]["worker"]], allow_other_workers=False, pure=False,
                        key=f"ab-{label}-r{int(time.time())}")
    fire_and_forget(fut); time.sleep(10)
    assign[label].update(commit=commit, submitted=time.strftime("%F %T"), relaunched=True)
    json.dump(assign, open(STATE, "w"), indent=1)
    print(f"re-fired {label} -> {assign[label]['worker']} ({arm} @ {commit})")


def _brief_side(label):
    import glob, os, subprocess, sys
    rds = sorted(glob.glob(f"/tmp/ab/{label}/runs/*/"))
    if not rds:
        return {"state": "no run directory yet"}
    rd = rds[-1]
    r = subprocess.run([sys.executable, "scripts/decision_brief.py", "status", rd], cwd=f"/tmp/ab/{label}",
                       capture_output=True, text=True)
    md = open(rd + "decision_brief.md").read() if os.path.exists(rd + "decision_brief.md") else None
    return {"run_dir": rd, "status": r.stdout.strip() or r.stderr.strip(), "brief_md": md}


def _sign_side(label, answers_yaml, note):
    import glob, os, subprocess, sys, tempfile
    rd = sorted(glob.glob(f"/tmp/ab/{label}/runs/*/"))[-1]
    fd, p = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, answers_yaml.encode()); os.close(fd)
    env = {k: v for k, v in os.environ.items() if k != "MA_SIGNOFF_MODE"}
    r = subprocess.run([sys.executable, "scripts/decision_brief.py", "sign", rd, "--by", "owner",
                        "--answers", p, "--note", note], cwd=f"/tmp/ab/{label}", env=env,
                       capture_output=True, text=True)
    return {"rc": r.returncode, "out": (r.stdout + r.stderr).strip()}


def _report_side(label):
    import glob, subprocess, sys
    rds = sorted(glob.glob(f"/tmp/ab/{label}/runs/*/"))
    if not rds:
        return "no run directory yet"
    r = subprocess.run([sys.executable, "scripts/convergence_report.py", rds[-1]], cwd=f"/tmp/ab/{label}",
                       capture_output=True, text=True, timeout=300)
    return r.stdout or r.stderr[-1500:]


def report(label=None):
    """Convergence measures of the live run (pre-registered for attempt 7)."""
    label = label or LABELS[0]
    assign = json.load(open(STATE))
    client = get_cluster(create=False).get_client()
    print(on_worker(client, _report_side, label, worker=assign[label]["worker"]))


def brief(label=None):
    label = label or LABELS[0]
    assign = json.load(open(STATE))
    client = get_cluster(create=False).get_client()
    r = on_worker(client, _brief_side, label, worker=assign[label]["worker"])
    print(f"run dir: {r.get('run_dir')}\nstatus: {r.get('status') or r.get('state')}\n")
    print(r.get("brief_md") or "(no decision_brief.md yet)")


def sign(answers_path, note="", label=None):
    label = label or LABELS[0]
    assign = json.load(open(STATE))
    client = get_cluster(create=False).get_client()
    r = on_worker(client, _sign_side, label, open(answers_path).read(), note, worker=assign[label]["worker"])
    print(r["out"])
    sys.exit(r["rc"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "launch":
        launch()
    elif cmd == "relaunch":
        relaunch(sys.argv[2])
    elif cmd == "brief":
        brief()
    elif cmd == "report":
        report()
    elif cmd == "sign":
        sign(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "status":
        status()
    elif cmd == "collect":
        status(collect=True)
    elif cmd == "shutdown":
        get_cluster(create=False).shutdown()
    else:
        sys.exit(__doc__)
