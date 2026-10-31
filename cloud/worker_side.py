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
    # Phase 50 α: this fresh VM's work dir is the launch archive (no evals/, no .git), the only
    # place main.py lets an eval-suite question run (scripts/lib/eval_paths.py).
    env["MA_EVAL_ISOLATED"] = "cloud-worker"
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
            f"{d}/transcripts.tar.gz", f"outputs/{prefix}/launch_{launch_id}/transcripts_{label}.tar.gz",
            account)["blob"]
    except Exception as e:  # noqa: BLE001
        res["transcripts_store_error"] = f"{type(e).__name__}: {e}"[:300]
    tarball = res.pop("tarball")
    open(f"{d}/result.tar.gz", "wb").write(tarball)
    res["run_stored"] = run_stored(tarball)
    res["launch_id"] = launch_id
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


def verify_attached(cl, expected_id):
    """coiled.Cluster(name=...) creates an empty cluster when the named one
    stopped between the state check and the call. If the cluster we got is not
    the one we checked, shut it down at once and refuse."""
    got = getattr(cl, "cluster_id", None)
    if expected_id is not None and got != expected_id:
        try:
            cl.shutdown()
        finally:
            raise SystemExit(f"cluster {expected_id} stopped while attaching; the new empty cluster {got} was shut down")
    return cl


def keeper_task(token, name, workspace, lo, hi, hours, adapt_options, cluster_id=None, overflow=None):
    """Run the keeper, then stop the keeper's own cluster (and the overflow
    pool's): once the run cluster has stopped there is nothing to hold, and an
    idle VM is waste."""
    try:
        return _keeper_loop(token, name, workspace, lo, hi, hours, adapt_options, cluster_id, overflow)
    finally:
        if overflow and overflow.get("id"):
            try:
                import coiled
                with coiled.Cloud(workspace=workspace) as cloud:
                    cloud.delete_cluster(cluster_id=overflow["id"], workspace=workspace,
                                         reason="run cluster stopped; keeper exiting")
            except Exception:  # noqa: BLE001 — the overflow cluster's idle timeout is the backstop
                pass
        from distributed import get_client
        get_client().shutdown()


# ---- quota refusals and the overflow pool (Phase 42 β, A6) -------------------------------
#
# Coiled fixes a cluster's worker VM types when the cluster is created (scale-up takes only
# a count) and, given a list, falls back to the next type on capacity errors but not on a
# quota refusal: E7 attempts 7 and 8 were refused for hours at 344-348 of 350 cores of a
# family quota shared with other users. A refusal shows up as a worker record in state
# "error" whose reason carries Azure's OperationNotAllowed message. The keeper reads those,
# and while refusals keep coming it grows a second Coiled cluster of another VM family (its
# own quota) whose VMs each run one dask worker joined to the run cluster's scheduler: a
# second worker group, which Coiled does not offer within one cluster.

OVERFLOW_PREFIX = "ovf-"
KEEPER_STATUS = "/tmp/keeper_status.json"


def quota_refusal(reason):
    """Azure's quota refusal parsed out of a Coiled worker state reason, or None.
    The family is the quota's name (e.g. standardESv5Family), not the VM size."""
    import re
    r = str(reason or "")
    if "OperationNotAllowed" not in r or "quota" not in r.lower():
        return None
    m = re.search(r"approved\s+(\w+)\s+Cores quota", r)
    out = {"family": m.group(1) if m else "unknown"}
    for key, pat in (("limit", r"Current Limit:\s*(\d+)"), ("usage", r"Current Usage:\s*(\d+)"),
                     ("required", r"Additional Required:\s*(\d+)")):
        mm = re.search(pat, r)
        out[key] = int(mm.group(1)) if mm else None
    return out


def _epoch(iso):
    import datetime as dt
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def quota_refusals(workers, seen=(), now=None):
    """New quota refusals in a Coiled worker listing (one record per refused VM request),
    oldest first: [{id, worker, at, family, limit, usage, required}]; ids in `seen` skipped."""
    out = []
    for w in workers or ():
        st = w.get("current_state") or {}
        if str(st.get("state", "")).lower() != "error" or w.get("id") in seen:
            continue
        q = quota_refusal(st.get("reason"))
        if q:   # the state's own time; with none, the scan's (a refusal never reads as stale by default)
            at = next((t for t in (_epoch(st.get(k)) for k in ("updated", "created")) if t), None) \
                or _epoch(w.get("updated")) or (now if now is not None else __import__("time").time())
            out.append({"id": w.get("id"), "worker": w.get("name"), "at": at, **q})
    return sorted(out, key=lambda x: x["at"])


def overflow_desired(target, n_primary, overflow_max, last_refusal_at, now, hold_s):
    """Overflow VMs wanted: the pool's shortfall (adaptive target minus primary workers),
    while the primary family was refused for quota within the last `hold_s` seconds."""
    if not overflow_max or last_refusal_at is None or now - last_refusal_at > hold_s:
        return 0
    return max(0, min(int(overflow_max), int(target) - int(n_primary)))


def overflow_plan(desired, planned, vms, idle_since, now, scale_down_s):
    """One keeper step for the overflow pool.
    vms: {coiled worker name: {"joined": its join task is running, "busy": its joined
    worker has tasks on the run scheduler}}; planned: overflow VMs requested (booting too).
    Returns {"scale_to", "join", "close", "idle_since"}: grow to `desired`, join VMs up to
    it, and close only VMs idle for `scale_down_s` beyond it (never a busy one)."""
    joined = [n for n in sorted(vms) if vms[n].get("joined")]
    unjoined = [n for n in sorted(vms) if not vms[n].get("joined")]
    join = unjoined[:max(0, desired - len(joined))]
    idle = {}
    for n in sorted(vms):
        if n in join or (vms[n].get("joined") and vms[n].get("busy")):
            continue
        idle[n] = idle_since.get(n, now)
    ripe = [n for n in unjoined + joined if n in idle and now - idle[n] >= scale_down_s]
    close = ripe[:max(0, len(vms) - desired)]
    return {"scale_to": desired if planned < desired else None, "join": join, "close": close,
            "idle_since": {n: t for n, t in idle.items() if n not in close}}


def join_task(sched_addr, cert, key, name, hours, resources="compute=1"):
    """On an overflow VM: run one dask worker joined to the run cluster's scheduler until it
    exits (retired, or the scheduler closed) or `hours` pass. The cluster's self-signed TLS
    certificate is its own CA; it is written to files only this user can read and removed
    on exit. Being a running task, this also keeps the overflow cluster from idling out."""
    import os, shutil, subprocess, sys, tempfile, time
    d = tempfile.mkdtemp(prefix="ovf_tls_")
    os.chmod(d, 0o700)
    paths = {}
    for fn, data in (("cert.pem", cert), ("key.pem", key)):
        fd = os.open(os.path.join(d, fn), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(data)
        paths[fn] = os.path.join(d, fn)
    log = open(os.path.join(tempfile.gettempdir(), f"{name}.log"), "a")
    cmd = [sys.executable, "-m", "distributed.cli.dask_worker", sched_addr, "--name", name, "--nthreads", "1",
           "--resources", resources, "--no-nanny", "--no-dashboard", "--tls-ca-file", paths["cert.pem"],
           "--tls-cert", paths["cert.pem"], "--tls-key", paths["key.pem"]]
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    t_end = time.time() + hours * 3600
    try:
        while p.poll() is None and time.time() < t_end:
            time.sleep(10)
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(d, ignore_errors=True)
        log.close()
    return {"name": name, "rc": p.returncode}


def _write_status(st):
    import json, os
    tmp = KEEPER_STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, KEEPER_STATUS)


def keeper_status():
    import json, os
    return json.load(open(KEEPER_STATUS)) if os.path.exists(KEEPER_STATUS) else None


def coiled_workers(workspace, cluster_id):
    """Every worker record Coiled holds for a cluster, refused ones included (state, reason)."""
    import coiled
    with coiled.Cloud(workspace=workspace) as cloud:
        return cloud._sync(cloud._depaginate_list, cloud._get_workers_page, cluster_id=cluster_id,
                           workspace=workspace)


class _Overflow:
    """The keeper's handle on the overflow cluster (attach-only, by the id the launch created)."""

    def __init__(self, spec, workspace):
        self.spec, self.workspace, self.cl, self.c, self.idle_since = spec, workspace, None, None, {}

    def attach(self):
        import coiled
        if self.cl is None:
            self.cl = verify_attached(coiled.Cluster(name=self.spec["name"], workspace=self.workspace,
                                                     shutdown_on_close=False, wait_for_workers=False),
                                      self.spec["id"])
            self.c = self.cl.get_client()
        return self.cl, self.c

    def step(self, run_client, run_cluster, target, n_primary, last_refusal_at, now):
        from distributed import fire_and_forget
        cl, c = self.attach()
        desired = overflow_desired(target, n_primary, self.spec.get("max", 0), last_refusal_at, now,
                                   self.spec.get("hold_s", 1800))
        proc = c.processing()
        run_proc = run_client.processing()
        busy_joined = {str(w.get("name"))[len(OVERFLOW_PREFIX):]: bool(run_proc.get(a))
                       for a, w in run_client.scheduler_info()["workers"].items()
                       if str(w.get("name", "")).startswith(OVERFLOW_PREFIX)}
        vms = {str(w.get("name")): {"addr": a, "joined": bool(proc.get(a)), "busy": busy_joined.get(str(w.get("name")), False)}
               for a, w in c.scheduler_info()["workers"].items()}
        cl.sync(cl._set_plan_requested)
        plan = overflow_plan(desired, len(cl.plan), vms, self.idle_since, now, self.spec.get("scale_down_s", 1200))
        self.idle_since = plan["idle_since"]
        if plan["scale_to"] is not None:
            cl.sync(cl.scale_up, plan["scale_to"], reason=f"quota fallback: {desired} overflow workers wanted")
        if plan["join"]:
            addr = self.spec.get("scheduler_address") or next(iter(run_client.run(
                lambda dask_worker: dask_worker.scheduler.address).values()))
            self.spec["scheduler_address"] = addr
            sec = run_cluster.security
            for nm in plan["join"]:
                fut = c.submit(join_task, addr, sec.tls_scheduler_cert, sec.tls_scheduler_key,
                               OVERFLOW_PREFIX + nm, self.spec.get("hours", 48), workers=[vms[nm]["addr"]],
                               allow_other_workers=False, pure=False, key=f"join-{nm}-{int(now)}")
                fire_and_forget(fut)
        if plan["close"]:
            joined_names = [OVERFLOW_PREFIX + n for n in plan["close"] if vms[n]["joined"]]
            if joined_names:
                run_client.retire_workers(names=joined_names, close_workers=True)
            cl.sync(cl.scale_down, plan["close"], reason="overflow worker idle past the scale-down delay")
        return {"desired": desired, "vms": len(vms), "joined": sum(v["joined"] for v in vms.values()),
                "planned": len(cl.plan), **{k: plan[k] for k in ("scale_to", "join", "close")}}


def _keeper_loop(token, name, workspace, lo, hi, hours, adapt_options, cluster_id=None, overflow=None, tick_s=60):
    """On the keeper VM: hold the run cluster's adaptive scaling (dask's Adaptive
    loop lives in the process that calls adapt). Attach-only, to the cluster id
    the launch created; reconnects after a dropped connection; exits when that
    cluster stops, and never scales a replacement. Each tick it also reads the
    run cluster's quota refusals, drives the overflow pool, and writes a status
    file that `run_cluster.py status` prints."""
    import contextlib, time
    import coiled
    import dask
    dask.config.set({"coiled.token": token})

    def alive():
        cs = coiled.list_clusters(workspace=workspace)
        items = cs if isinstance(cs, list) else list(cs.values())
        return any(c.get("name") == name and state_of(c) in LIVE_STATES
                   and (cluster_id is None or c.get("id") == cluster_id) for c in items)

    ovf_spec = dict(overflow or {})
    status = {"cluster": name, "cluster_id": cluster_id, "pool": [lo, hi], "adapt": adapt_options,
              "primary_vm_types": ovf_spec.get("primary_vm_types"), "active": "primary",
              "overflow": {k: ovf_spec.get(k) for k in ("name", "id", "max", "vm_types")} if ovf_spec else None,
              "refusals": [], "refusal_count": 0, "last_refusal_at": None, "errors": []}
    seen = set()
    ovf = _Overflow(ovf_spec, workspace) if ovf_spec.get("id") else None
    with open("/tmp/keeper.log", "a", buffering=1) as f, contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        print(f"{time.strftime('%T')} keeper holding {name} {lo}..{hi} {adapt_options}", flush=True)
        t_end = time.time() + hours * 3600
        while time.time() < t_end:
            try:
                if not alive():
                    print(f"{time.strftime('%T')} {name} is not running; keeper exiting", flush=True)
                    return "keeper exited"
                cl = coiled.Cluster(name=name, workspace=workspace, shutdown_on_close=False, wait_for_workers=False)
                try:
                    verify_attached(cl, cluster_id)
                except SystemExit as e:
                    print(f"{time.strftime('%T')} {e}; keeper exiting", flush=True)
                    return "keeper exited"
                cl.adapt(minimum=lo, maximum=hi, **adapt_options)
                c = cl.get_client()
                while time.time() < t_end:
                    try:
                        if not alive():
                            break
                    except Exception:  # noqa: BLE001 — an API hiccup is not a stop
                        pass
                    now = time.time()
                    winfo = c.scheduler_info()["workers"]
                    n_ovf = sum(str(w.get("name", "")).startswith(OVERFLOW_PREFIX) for w in winfo.values())
                    status.update(updated=now, workers=len(winfo), primary_workers=len(winfo) - n_ovf,
                                  overflow_workers=n_ovf)
                    try:
                        new = quota_refusals(coiled_workers(workspace, cluster_id), seen, now)
                        for r in new:
                            seen.add(r["id"])
                            print(f"{time.strftime('%T')} quota refusal {r['family']} {r['usage']}/{r['limit']}",
                                  flush=True)
                        if new:
                            status["refusals"] = (status["refusals"] + new)[-20:]
                            status["refusal_count"] += len(new)
                            status["last_refusal_at"] = max(status["last_refusal_at"] or 0, new[-1]["at"])
                        status["refusal_scan"] = "ok"
                    except Exception as e:  # noqa: BLE001 — visible in status, not silent
                        status["refusal_scan"] = f"unavailable: {type(e).__name__}: {str(e)[:160]}"
                    if ovf is not None:
                        try:
                            target = max(lo, min(hi, int(c.run_on_scheduler(
                                lambda dask_scheduler: dask_scheduler.adaptive_target()))))
                            step = ovf.step(c, cl, target, len(winfo) - n_ovf, status["last_refusal_at"], now)
                            status.update(target=target, overflow_step=step,
                                          active="primary+overflow" if step["vms"] or step["desired"] else "primary")
                        except Exception as e:  # noqa: BLE001
                            status["errors"] = (status["errors"] + [f"{time.strftime('%T')} overflow "
                                                                    f"{type(e).__name__}: {str(e)[:200]}"])[-10:]
                    _write_status(status)
                    print(f"{time.strftime('%T')} workers={len(winfo)} (overflow {n_ovf}) "
                          f"refusals={status['refusal_count']} active={status['active']}", flush=True)
                    time.sleep(tick_s)
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


# ---- durable detached jobs on a VM (Phase 42 β, A7; driven by cloud/vm_job.py) ------------
#
# Coiled stops an idle cluster after `idle_timeout`, and "idle" is the dask scheduler's
# view: no task queued, running or unrunnable. A job started with client.run (or a process
# it spawned) is not a task, so on 2026-10-28 two detached jobs on the dev VM were stopped
# mid-run and their outputs lost. Here the job runs inside a dask task submitted with
# fire_and_forget, which keeps the scheduler busy for exactly the job's lifetime, and it
# writes into $JOB_OUT, which `vm_job.py status` copies to the laptop on every poll.

JOBS = "/tmp/ma_jobs"
JOB_STALE_S = 300


def _job_dir(job_id):
    import os, re
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(job_id)) or str(job_id) in (".", ".."):
        raise ValueError(f"bad job id {job_id!r}")
    return os.path.join(JOBS, job_id)


def _job_write(d, st):
    import json, os
    with open(os.path.join(d, "state.json.tmp"), "w") as f:
        json.dump(st, f, indent=1)
    os.replace(os.path.join(d, "state.json.tmp"), os.path.join(d, "state.json"))


def _read_json(p):
    import json
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


JOB_QUEUED_STALE_S = 3600


class _JobLock:
    """Exclusive lock on one job's directory, so checking and changing its state is atomic."""

    def __init__(self, d):
        self.d = d

    def __enter__(self):
        import fcntl, os
        os.makedirs(self.d, exist_ok=True)
        self.f = open(os.path.join(self.d, ".lock"), "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def job_queue(job_id):
    """Called (client.run) just before the job's task is submitted: reserves the id with a
    'queued' record holding a fresh token, so a poll between submit and start sees a pending
    job, and a second submission of the same id is refused. Returns the token, or None."""
    import os, time, uuid
    d = _job_dir(job_id)
    with _JobLock(d):
        if job_view(_read_json(os.path.join(d, "state.json")), time.time())["state"] in ("running", "queued"):
            return None
        token = uuid.uuid4().hex
        _job_write(d, {"job_id": job_id, "state": "queued", "token": token, "queued": time.time(),
                       "heartbeat": time.time()})
        return token


def _beat_run(argv, log, d, st, timeout_s, heartbeat_s):
    """A setup command, with the job's heartbeat kept up while it runs."""
    import subprocess, time
    p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
    t_end = time.time() + timeout_s
    while p.poll() is None:
        if time.time() > t_end:
            p.kill()
            break
        st["heartbeat"] = time.time()
        _job_write(d, st)
        time.sleep(min(5, heartbeat_s))
    return p.wait()


def job_task(job_id, code, cmd, secrets=None, max_hours=24.0, setup_sdk=False, heartbeat_s=30, token=None):
    """The job, as a dask task: claim the id (the 'queued' record with this submission's
    token; with no token, an id that is neither queued nor running), unpack `code` (a
    tar.gz, or None), run `cmd` (a shell string or an argv list) in it with JOB_OUT set,
    heartbeat into state.json throughout, stop it after `max_hours`. Secrets go to the
    job's environment only, never to a file. The state record is replaced, never removed."""
    import io, os, re, shutil, signal, subprocess, sys, tarfile, time
    d = _job_dir(job_id)
    with _JobLock(d):
        raw = _read_json(os.path.join(d, "state.json")) or {}
        cur = job_view(raw or None, time.time())
        if token is not None:
            ok = cur["state"] == "queued" and raw.get("token") == token
        else:
            ok = cur["state"] not in ("running", "queued")
        if not ok:
            return {"job_id": job_id, "state": "refused", "why": f"the id is {cur['state']} under another submission"}
        work, out = os.path.join(d, "work"), os.path.join(d, "out")
        for sub in (work, out, os.path.join(d, "job.log")):
            if os.path.isdir(sub):
                shutil.rmtree(sub, ignore_errors=True)
            elif os.path.exists(sub):
                os.remove(sub)
        os.makedirs(work)
        os.makedirs(out)
        t0 = time.time()
        st = {"job_id": job_id, "state": "running", "started": t0, "heartbeat": t0, "max_hours": max_hours,
              "cmd": cmd if isinstance(cmd, str) else " ".join(cmd), "token": token}
        _job_write(d, st)
    try:
        if code:
            tarfile.open(fileobj=io.BytesIO(code), mode="r:gz").extractall(work, filter="data")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "OPENAI", "AZ_", "AZURE_"))}
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        env.update(secrets or {})
        env["JOB_OUT"] = out
        with open(os.path.join(d, "job.log"), "ab", buffering=0) as log:
            if setup_sdk and os.path.exists(os.path.join(work, "requirements.txt")):   # dev_vm.py's SDK pin
                pins = [ln.split("#")[0].strip() for ln in open(os.path.join(work, "requirements.txt"))
                        if re.match(r"\s*claude-agent-sdk==", ln)]
                if pins:
                    _beat_run([sys.executable, "-m", "pip", "install", "-q", *pins], log, d, st, 600, heartbeat_s)
            p = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=work, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            st["pid"] = p.pid
            t_end, beat = t0 + max_hours * 3600, 0.0
            while p.poll() is None:
                now = time.time()
                if now > t_end:
                    st["timed_out"] = True
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                        p.wait(30)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        try:
                            os.killpg(p.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    break
                if now - beat >= heartbeat_s:
                    st["heartbeat"], beat = now, now
                    _job_write(d, st)
                time.sleep(min(5, heartbeat_s))
            rc = p.wait()
    except Exception as e:  # noqa: BLE001 — a job that cannot start is recorded, not lost
        st.update(state="failed", error=f"{type(e).__name__}: {e}"[:500], ended=time.time(), heartbeat=time.time())
        _job_write(d, st)
        return st
    st.update(state="timed_out" if st.get("timed_out") else ("done" if rc == 0 else "failed"), rc=rc,
              ended=time.time(), duration_s=round(time.time() - t0), heartbeat=time.time())
    _job_write(d, st)
    return st


def job_view(st, now, stale_s=JOB_STALE_S, queued_stale_s=JOB_QUEUED_STALE_S):
    """The job's state as seen now: a 'running' record whose heartbeat stopped is 'lost'
    (the VM restarted or the task was cancelled), as is a 'queued' one whose task has not
    started within `queued_stale_s` (it may wait behind another job until then); no record
    is 'missing'. The submission token is internal and not shown."""
    if not isinstance(st, dict) or "state" not in st:
        return {"state": "missing"}
    v = {k: x for k, x in st.items() if k != "token"}
    if v["state"] == "running" and now - float(v.get("heartbeat") or 0) > stale_s:
        v["state"] = "lost"
    if v["state"] == "queued" and now - float(v.get("queued") or 0) > queued_stale_s:
        v["state"] = "lost"
    return v


def job_state(job_id):
    """State, log tail, and the manifest of fetchable files ({rel: [size, mtime]})."""
    import os, time
    d = _job_dir(job_id)
    st = job_view(_read_json(os.path.join(d, "state.json")), time.time())
    files = {}
    if os.path.exists(os.path.join(d, "job.log")):
        s = os.stat(os.path.join(d, "job.log"))
        files["job.log"] = [s.st_size, s.st_mtime]
    for root, _, fns in os.walk(os.path.join(d, "out")):
        for fn in fns:
            p = os.path.join(root, fn)
            if not os.path.islink(p):
                s = os.stat(p)
                files[os.path.relpath(p, d)] = [s.st_size, s.st_mtime]
    tail = ""
    if "job.log" in files:
        with open(os.path.join(d, "job.log"), "rb") as f:
            f.seek(max(0, files["job.log"][0] - 2000))
            tail = f.read().decode("utf-8", "replace")
    return {"state": st, "files": files, "log_tail": tail}


def job_chunk(job_id, rel, offset, length):
    """Bytes of one job file: job.log or a file under out/ (no links, no path escapes)."""
    import os
    d = os.path.realpath(_job_dir(job_id))
    p = os.path.realpath(os.path.join(d, rel))
    if not (rel == "job.log" or p.startswith(os.path.join(d, "out") + os.sep)) or os.path.islink(os.path.join(d, rel)):
        raise ValueError(f"not a job output: {rel!r}")
    with open(p, "rb") as f:
        f.seek(offset)
        return f.read(length)
