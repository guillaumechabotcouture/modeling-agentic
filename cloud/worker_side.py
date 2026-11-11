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
# The IDM Coiled default workspace (non-sensitive; never the prod subscription). The launcher and
# the VM jobs read it from here; scripts/replay_coiled.py keeps a copy (it is uploaded to the
# workers on its own), held equal by cloud/run_cluster.py self-test RC60f.
WORKSPACE = "idm-coiled-idmad-r2"


def ensure_bwrap(run=None, which=None, platform=None) -> str:
    """Best effort: install bubblewrap, which the v2 derivation sandbox needs (scripts/pipeline.py sandbox_kind;
    docs/design/v2-derived-inputs.md §7). The Coiled image has passwordless sudo and setpriv but no bwrap. Tries
    `sudo -n apt-get install -y bubblewrap`, then again after `apt-get update`. Returns a one-line note for the
    run's notes and never raises: without bwrap, a derivation that must run fails closed (derivation_unsandboxed),
    and a run past sign-off only checks hashes. `run`/`which`/`platform` are for the self-test."""
    import shutil
    import subprocess
    import sys
    run = run or subprocess.run
    which = which or shutil.which
    platform = platform or sys.platform
    if not platform.startswith("linux"):
        return f"bwrap: not Linux ({platform}); no install"
    if which("bwrap"):
        return "bwrap: present"
    if not which("sudo"):
        return "bwrap: missing and no sudo; derivations that must run will fail closed"

    def sh(*cmd):
        try:
            return run(["sudo", "-n", *cmd], capture_output=True, text=True, timeout=600).returncode
        except Exception as e:  # noqa: BLE001 -- best effort
            return f"{type(e).__name__}"
    rc = sh("apt-get", "install", "-y", "bubblewrap")
    if rc != 0:
        sh("apt-get", "update")
        rc = sh("apt-get", "install", "-y", "bubblewrap")
    if rc == 0 and which("bwrap"):
        return "bwrap: installed"
    return f"bwrap: install failed ({rc}); derivations that must run will fail closed"


def _run_main(cmd, cwd, env, log, timeout_s=None):
    """main.py in its own process group; past `timeout_s` the group is killed and "timeout" returned (a smoke's
    cloud-side bound), else the exit code."""
    import os, signal, subprocess
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        return "timeout"


def _hold(d, hold_s, sleep=None, clock=None):
    """Hold the results on disk for `collect` (the VM stays busy) until collected or `hold_s` has passed."""
    import os, time
    sleep, clock = sleep or time.sleep, clock or time.time
    t_end = clock() + hold_s
    while clock() < t_end and not os.path.exists(f"{d}/collected"):
        sleep(30)


def run_job_impl(label, code, question, flags, anthropic_key, openai_key, max_rounds=8, max_sessions=5,
                 timeout_s=None, on_start=None):
    """Unpack the code, run main.py (killed past `timeout_s`), return the run directory as a tarball.
    `on_start(work)` is called once the work dir is unpacked, just before main.py starts (the checkpoint thread)."""
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
    notes = [ensure_bwrap()]          # the v2 derivation sandbox (best effort; never fails the run)
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
        if on_start is not None:
            try:
                on_start(work)
            except Exception as e:  # noqa: BLE001 -- checkpoints never stop the run
                notes.append(f"checkpoints not started: {type(e).__name__}: {e}"[:200])
        returncode = _run_main([sys.executable, "main.py", question, "--max-rounds", str(max_rounds),
                                "--max-sessions", str(max_sessions), *flags], work, env, log, timeout_s)
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
    return {"label": label, "exit_code": returncode, "duration_s": round(time.time() - t0),
            "run_dir": os.path.basename(runs[-1]) if runs else None, "skipped_large_files": skipped,
            "notes": notes, "tarball": buf.getvalue()}


# F6 run checkpoints (2026-11-11: a v2 preview stopped at its credit cap after 3 h; main.py stores the run only when
# it ends, so its engine batches were lost). While main.py runs, the agent VM uploads its run dir every
# checkpoint_minutes; `run_cluster.py <cfg> checkpoint` fetches the latest (module doc there: how to resume from it).
# Codex round 1: immutable generations behind a pointer, a disk check, and requests that never block a worker.
CHECKPOINT_POLL_S = 10.0
CKDIR = ".checkpoint"        # in the label's work dir, beside runs/: never inside the run dir it archives
KEEP_GENERATIONS = 2
DISK_FACTOR, DISK_SPARE = 1.5, 2 * 2**30   # free space a checkpoint needs: 1.5 x the run dir + 2 GB


def checkpoint_pointer(prefix, launch_id, label):
    """The small record naming a label's latest complete generation (under the launch that wrote it)."""
    return f"outputs/{prefix}/launch_{launch_id}/checkpoint_{label}.json"


def checkpoint_blobs(prefix, launch_id, label, gen):
    """(tarball, record) of generation `gen`: written once, never overwritten."""
    base = f"outputs/{prefix}/launch_{launch_id}/checkpoint_{label}_g{gen}"
    return base + ".tar.gz", base + ".json"


def archive_summary(tgz):
    """run_state.json's state and round, and the engine batches (a results file with its `.summary.json`, or the
    `.partial.jsonl` journal of an interrupted one, scripts/compute.py) with their ok rows, read from the archive
    itself: the record describes what was archived, never the live run dir (Codex round 1 P1-1)."""
    import json, tarfile
    out = {"run_state": None, "cached_batches": 0, "cached_rows": 0}
    with tarfile.open(tgz, mode="r:gz") as t:
        members = [m for m in t.getmembers() if m.isfile()]
        names = {m.name for m in members}
        outs = {m.name[:-len(".partial.jsonl")] for m in members if m.name.endswith(".partial.jsonl")}
        outs |= {m.name[:-len(".summary.json")] for m in members
                 if m.name.endswith(".summary.json") and m.name[:-len(".summary.json")] in names}
        for m in members:
            if m.name.count("/") == 2 and m.name.endswith("/run_state.json"):
                try:
                    st = json.load(t.extractfile(m))
                    out["run_state"] = {"state": st.get("state"), "round": st.get("round")}
                except (ValueError, AttributeError):
                    pass
            elif m.name in outs or (m.name.endswith(".partial.jsonl") and m.name[:-len(".partial.jsonl")] in outs):
                out["cached_rows"] += sum(1 for ln in t.extractfile(m) if b'"ok": true' in ln)
    out["cached_batches"] = len(outs)
    return out


def build_checkpoint(work, run_dir, out_path, store, secrets=()):
    """The run dir as runs/<RUN>/... plus run.log, streamed to `out_path` through a temporary file (results_store
    write_archive: no _truth/, no credential files, no links, secrets redacted). Returns its record."""
    import os, time
    tmp = out_path + ".tmp"
    log = os.path.join(work, "run.log")
    try:
        skipped = store.write_archive(run_dir, os.path.join("runs", os.path.basename(run_dir)), tmp, list(secrets),
                                      extra=[(log, "run.log")] if os.path.exists(log) else [])
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "epoch": time.time(),
            "run_dir": os.path.basename(run_dir), "bytes": os.path.getsize(out_path),
            "sha256": store.sha256_file(out_path), "skipped": skipped[:50], **archive_summary(out_path)}


def _du(path):
    import os
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.lstat(os.path.join(dp, fn)).st_size
            except OSError:
                pass
    return total


class Checkpointer:
    """Uploads a checkpoint of the label's run dir every `interval_s` while main.py runs, and on request
    (checkpoint_now_side files one). Each checkpoint is a new generation (tarball, record) written once, then the
    pointer names it; the last KEEP_GENERATIONS complete ones are kept. One upload at a time: a checkpoint due while
    the previous one uploads is skipped, and a request then stays pending until a checkpoint that starts after it.
    A checkpoint needs free space of DISK_FACTOR x the run dir + DISK_SPARE, else it is skipped. Every failure is
    logged (<work>/.checkpoint/checkpoint.log) and never raised; partial and local archives are removed."""

    def __init__(self, label, work, prefix, launch_id, account, store, interval_s, secrets=(),
                 poll_s=CHECKPOINT_POLL_S, log=None, free_bytes=None):
        import os, threading, time
        self.label, self.work, self.account, self.store = label, work, account, store
        self.interval_s, self.poll_s, self.secrets, self.print = float(interval_s), poll_s, list(secrets), log or print
        self.prefix, self.launch_id, self.dir = prefix, launch_id, os.path.join(work, CKDIR)
        self.free_bytes = free_bytes or (lambda p: __import__("shutil").disk_usage(p).free)
        self.done = self.failed = self.skipped = 0
        self.gen, self.kept, self.pending = 0, [], {}
        self._busy, self._stop, self._thread, self._up = threading.Lock(), threading.Event(), None, None
        self._plock = threading.Lock()
        self._last = time.time()

    def log(self, msg):
        import os, time
        line = f"{time.strftime('%F %T')} checkpoint {self.label}: {msg}"
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, "checkpoint.log"), "a") as f:
                f.write(line + "\n")
            self.print(line)
        except Exception:  # noqa: BLE001 -- logging never stops the run
            pass

    def start(self):
        import os, threading
        os.makedirs(self.dir, exist_ok=True)
        open(os.path.join(self.dir, "active"), "w").write(str(self.interval_s))
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"ma-checkpoint-{self.label}")
        self._thread.start()

    def stop(self, timeout_s=5.0):
        """Stop the timer; an upload in flight is left to finish in the background (main.py's own store follows)."""
        import os
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        try:
            os.remove(os.path.join(self.dir, "active"))
        except OSError:
            pass

    def wait_idle(self, timeout_s):
        if self._up is not None:
            self._up.join(timeout_s)

    def _requests(self):
        """Move filed requests (request_<token>) into the pending set."""
        import glob, json, os
        for p in glob.glob(os.path.join(self.dir, "request_*")):
            try:
                r = json.load(open(p))
                os.remove(p)
            except (OSError, ValueError):
                continue
            with self._plock:
                self.pending[str(r.get("token"))] = float(r.get("epoch") or 0)

    def _loop(self):
        import time
        while not self._stop.wait(self.poll_s):
            try:
                self._requests()
                if self.pending:
                    self.trigger("request")
                elif time.time() - self._last >= self.interval_s:
                    self.trigger("interval")
            except Exception as e:  # noqa: BLE001
                self.log(f"timer error: {type(e).__name__}: {e}")

    def trigger(self, why) -> bool:
        """Start one checkpoint on its own thread, taking every pending request (it starts after all of them);
        False (skipped) while the previous one is uploading: pending requests then stay pending."""
        import threading, time
        if not self._busy.acquire(blocking=False):
            self.skipped += 1
            self.log(f"{why}: skipped, the previous checkpoint is still uploading")
            return False
        with self._plock:
            tokens, self.pending = dict(self.pending), {}
        started = self._last = time.time()
        self._up = threading.Thread(target=self._upload, args=(why, tokens, started), daemon=True)
        self._up.start()
        return True

    def _upload(self, why, tokens, started):
        import glob, json, os
        tgz = rp = None
        blobs = ()
        try:
            runs = sorted(glob.glob(os.path.join(self.work, "runs", "*")), key=os.path.getmtime)
            if not runs:
                self.log(f"{why}: no run dir yet")
                return
            os.makedirs(self.dir, exist_ok=True)
            need, free = DISK_FACTOR * _du(runs[-1]) + DISK_SPARE, self.free_bytes(self.dir)
            if free < need:
                self.log(f"{why}: skipped, free space {free / 2**30:.1f} GB < {need / 2**30:.1f} GB needed")
                return
            self.gen += 1
            g = self.gen
            blobs = checkpoint_blobs(self.prefix, self.launch_id, self.label, g)
            tgz, rp = os.path.join(self.dir, f"checkpoint_g{g}.tar.gz"), os.path.join(self.dir, f"checkpoint_g{g}.json.tmp")
            rec = build_checkpoint(self.work, runs[-1], tgz, self.store, self.secrets)
            rec.update({"label": self.label, "launch_id": self.launch_id, "generation": g,
                        "previous": self.kept[-1] if self.kept else None, "blob": blobs[0], "why": why,
                        "requests": sorted(tokens), "started_epoch": started})
            self.store.upload_file(tgz, blobs[0], self.account)
            json.dump(rec, open(rp, "w"), indent=1)
            self.store.upload_file(rp, blobs[1], self.account)
            self.store.upload_file(rp, checkpoint_pointer(self.prefix, self.launch_id, self.label), self.account)
            os.replace(rp, os.path.join(self.dir, "checkpoint.json"))
            self.kept.append(g)
            self.done += 1
            blobs = ()
            self.log(f"{why}: generation {g} stored, {rec['bytes']} bytes, {rec['cached_batches']} cached batches, "
                     f"state {rec['run_state']} -> {rec['blob']}")
            while len(self.kept) > KEEP_GENERATIONS:
                self._delete(checkpoint_blobs(self.prefix, self.launch_id, self.label, self.kept.pop(0)))
        except Exception as e:  # noqa: BLE001 -- a failed checkpoint never stops the run
            self.failed += 1
            self.log(f"{why}: FAILED {type(e).__name__}: {str(e)[:300]}")
            self._delete(blobs)                  # a generation the pointer never named
        finally:
            for p in (tgz, rp, tgz and tgz + ".tmp"):
                if p and os.path.exists(p):
                    os.remove(p)
            self._busy.release()

    def _delete(self, blobs):
        for b in blobs:
            try:
                self.store.delete(b, self.account)
            except Exception as e:  # noqa: BLE001
                self.log(f"could not delete {b}: {type(e).__name__}")


def checkpoint_starter(label, prefix, launch_id, account, store, checkpoint_s, secrets=()):
    """run_to_disk's on_start: a started Checkpointer kept in `box`, or None when checkpoints are off (0 / None)."""
    if not checkpoint_s:
        return None

    def on_start(work):
        on_start.box = Checkpointer(label, work, prefix, launch_id, account, store, checkpoint_s, secrets)
        on_start.box.start()
    on_start.box = None
    return on_start


def checkpoint_now_side(label):
    """client.run on the agent VM (`run_cluster.py <cfg> checkpoint-now`): file one request for the running
    checkpoint thread and return at once with its token; it never waits on the worker (Codex round 1 P1-3). The
    launcher then polls storage for a generation carrying the token, or started after `epoch`."""
    import json, os, time, uuid
    d = f"{WORK}/{label}"
    ck = os.path.join(d, CKDIR)
    if os.path.exists(f"{d}/result.json"):
        return {"state": "finished"}             # main.py ended: its own store (or the hold for collect) applies
    if not os.path.exists(os.path.join(ck, "active")):
        return {"state": "no checkpointer"}      # checkpoints off, or a launch from before them
    token, epoch = uuid.uuid4().hex, time.time()
    tmp = os.path.join(ck, f".req_{token}.tmp")
    json.dump({"token": token, "epoch": epoch}, open(tmp, "w"))
    os.replace(tmp, os.path.join(ck, f"request_{token}"))
    return {"state": "requested", "token": token, "epoch": epoch}


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


def run_identity(tarball):
    """The run's pipeline, commit and launcher from its metadata.json in the tarball; "unknown"
    for anything missing (scripts/lib/pipeline_id.py label; inlined: this file runs on the VMs)."""
    import io, json, tarfile
    out = {"pipeline": "unknown", "commit": "unknown", "launcher": "unknown"}
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as t:
            for m in t.getmembers():
                if m.name.endswith("/metadata.json") and m.name.count("/") == 2:   # runs/<run>/metadata.json
                    meta = json.load(t.extractfile(m))
                    meta = meta if isinstance(meta, dict) else {}
                    if meta.get("pipeline") in ("v1", "v2"):
                        out["pipeline"] = meta["pipeline"]
                    for k in ("commit", "launcher"):
                        if isinstance(meta.get(k), str) and meta[k].strip():
                            out[k] = meta[k].strip()
                    break
    except (tarfile.TarError, ValueError, OSError):
        pass
    return out


def record_blob(prefix, launch_id, label):
    """Result records live under the launch that wrote them, so a record an
    earlier launch left under the same prefix never counts for this one."""
    return f"outputs/{prefix}/launch_{launch_id}/result_{label}.json"


def all_stored(store, prefix, account, labels, launch_id):
    """Every label of this launch has its result record in storage."""
    have = {b["name"] for b in store.list_blobs(f"outputs/{prefix}/launch_{launch_id}/", account)}
    return all(record_blob(prefix, launch_id, lb) in have for lb in labels)


def run_to_disk(label, code, question, flags, anthropic_key, openai_key, labels, prefix, account, launch_id,
                max_rounds=8, max_sessions=5, shutdown_when_stored=True, run_timeout_s=None, hold_s=24 * 3600,
                checkpoint_s=None):
    """The agent VM's task: run, store the run, its transcripts and its result
    record, then (last run of the launch) shut the cluster down. If any upload
    failed, hold the results on disk and stay busy until `collect` takes them:
    an idle VM is the one an autoscaling pool retires first. While main.py runs,
    a checkpoint of the run dir goes to storage every `checkpoint_s` (None: off)."""
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
    ck = checkpoint_starter(label, prefix, launch_id, account, results_store, checkpoint_s,
                            [anthropic_key, openai_key or ""])
    try:
        res = run_job_impl(label, code, question, flags, anthropic_key, openai_key, max_rounds, max_sessions,
                           timeout_s=run_timeout_s, on_start=ck)
    finally:
        if ck is not None and ck.box is not None:
            ck.box.stop()
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
    res["run_identity"] = run_identity(tarball)
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
    _hold(d, hold_s)
    return res


ATTACH_SKEW_S = 120.0    # clock skew allowed against Coiled's `created` time; equal to run_cluster.OWN_SKEW_S (RC18f)


def _utc_epoch(v):
    """An ISO time from the Coiled API as epoch seconds (UTC when it has no zone), or None (run_cluster._ts)."""
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def verify_attached(cl, expected_id, started=None, lister=None):
    """coiled.Cluster(name=...) creates an empty cluster when the named one stopped between the state check and the
    call. If the cluster we got is not the one we checked, refuse. It is shut down only when this attach created it:
    the listing (`lister`) shows its `created` time no earlier than the attach's start (`started`) less
    ATTACH_SKEW_S, the rule run_cluster._construct applies to a construction. Any doubt (no start time, not listed,
    no created time, an earlier one, the listing failing) means it may be another invocation's: refused and left
    alone (Codex round 2 on the launch watch)."""
    got = getattr(cl, "cluster_id", None)
    if expected_id is None or got == expected_id:
        return cl
    ts = None
    if started is not None and lister is not None:
        try:
            ts = next((_utc_epoch(c.get("created")) for c in lister() if c.get("id") == got), None)
        except Exception:  # noqa: BLE001 — unknown: not ours
            ts = None
    if ts is not None and ts >= started - ATTACH_SKEW_S:
        try:
            cl.shutdown()
        finally:
            raise SystemExit(f"cluster {expected_id} stopped while attaching; the new empty cluster {got} was shut down")
    raise SystemExit(f"attached to cluster {got}, not the checked cluster {expected_id}, and this attach did not "
                     f"create it (created {ts}, attach began {started}): refusing; {got} is left alone")


def attach_existing(name, workspace, expected_id):
    """The one attach call: coiled.Cluster(name=...) without waiting for workers, then verify_attached, which refuses
    any cluster other than `expected_id` (and shuts it down only when this attach created it). Callers check first
    that the cluster is live (run_cluster.attach, the keeper loop), so a stopping or stopped cluster is never attached
    by name; the overflow handle attaches by the id the launch created."""
    import time
    import coiled
    started = time.time()
    cl = coiled.Cluster(name=name, workspace=workspace, shutdown_on_close=False, wait_for_workers=False)
    lister = lambda: (lambda cs: cs if isinstance(cs, list) else list(cs.values()))(  # noqa: E731
        coiled.list_clusters(workspace=workspace))
    return verify_attached(cl, expected_id, started=started, lister=lister)


def task_state(key, dask_scheduler=None):
    """On the scheduler (client.run_on_scheduler): the state of task `key`, or None if it does not hold it. The
    launcher's acknowledgement that the scheduler accepted a run (run_cluster.wait_listed)."""
    ts = dask_scheduler.tasks.get(key) if dask_scheduler is not None else None
    return getattr(ts, "state", None)


def keeper_health():
    """On the keeper VM: its status file, its log and its own clock (run_cluster.keeper_fresh)."""
    import time
    return {"status": keeper_status(), "log": keeper_log(), "now": time.time()}


def ack_probe():
    """A task that returns at once (run_cluster.py ack-probe: the acknowledgement of a fast task)."""
    return "ok"


class SchedulerInfoUnavailable(RuntimeError):
    """The scheduler's worker table could not be read within the retry bound."""


def live_client(cluster, client):
    """`client` while it is open; a fresh one from `cluster` once distributed has closed it
    (a client whose reconnect fails is closed for good and reports an empty identity)."""
    if client is not None and getattr(client, "status", None) not in ("closed", "closing"):
        return client
    return cluster.get_client()


def scheduler_workers(get_client, attempts=6, delay_s=5.0, max_delay_s=60.0, min_workers=0, sleep=None, log=None):
    """The scheduler's worker table {address: info}, from client.scheduler_info()["workers"].
    distributed returns {} there (no "workers" key) whenever the client is not connected:
    before its first identity reply, while it reconnects, and after a failed reconnect has
    closed it. Each try takes a client from `get_client` and waits with doubling backoff
    (at most `max_delay_s`); after `attempts` tries it raises SchedulerInfoUnavailable
    naming the last state, never an empty table that would read as "no workers"."""
    import time
    sleep = sleep or time.sleep
    delay, last = delay_s, "no try made"
    for i in range(max(1, int(attempts))):
        try:
            c = get_client()
            info = c.scheduler_info()
            w = info.get("workers") if isinstance(info, dict) else None
            if isinstance(w, dict) and len(w) >= min_workers:
                return w
            last = (f"{len(w)} of {min_workers} workers joined" if isinstance(w, dict) else
                    f"scheduler identity has no worker table (client {getattr(c, 'status', '?')})")
        except Exception as e:  # noqa: BLE001 — retried, then reported
            last = f"{type(e).__name__}: {str(e)[:160]}"
        if i < attempts - 1:
            if log:
                log(f"{time.strftime('%T')} scheduler worker table not ready ({last}); "
                    f"retry {i + 1}/{attempts - 1} in {delay:g}s")
            sleep(delay)
            delay = min(max_delay_s, delay * 2)
    raise SchedulerInfoUnavailable(f"scheduler worker table unavailable after {attempts} tries: {last}")


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
KEEPER_LOG = "/tmp/keeper.log"


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
        """(cluster, client), or (None, None) when verify_attached refuses the attachment (Codex round 3 on the
        launch watch): the overflow pool is unavailable this tick and the keeper goes on with the primary pool."""
        if self.cl is None:
            try:
                cl = attach_existing(self.spec["name"], self.workspace, self.spec["id"])
            except SystemExit as e:
                import time
                print(f"{time.strftime('%T')} overflow {self.spec['name']} ({self.spec['id']}) unavailable this "
                      f"tick: {str(e)[:200]}", flush=True)
                self.refused = str(e)[:200]
                return None, None
            self.cl, self.c = cl, cl.get_client()
        return self.cl, self.c

    def step(self, run_client, run_cluster, target, n_primary, last_refusal_at, now):
        cl, c = self.attach()
        if cl is None:                    # no overflow growth this tick
            return {"desired": 0, "vms": 0, "joined": 0, "planned": 0, "scale_to": None, "join": [], "close": [],
                    "unavailable": getattr(self, "refused", "attach refused")}
        from distributed import fire_and_forget
        desired = overflow_desired(target, n_primary, self.spec.get("max", 0), last_refusal_at, now,
                                   self.spec.get("hold_s", 1800))
        proc = c.processing()
        run_proc = run_client.processing()
        busy_joined = {str(w.get("name"))[len(OVERFLOW_PREFIX):]: bool(run_proc.get(a))
                       for a, w in scheduler_workers(lambda: run_client, attempts=3).items()
                       if str(w.get("name", "")).startswith(OVERFLOW_PREFIX)}
        vms = {str(w.get("name")): {"addr": a, "joined": bool(proc.get(a)), "busy": busy_joined.get(str(w.get("name")), False)}
               for a, w in scheduler_workers(lambda: c, attempts=3).items()}
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
    with open(KEEPER_LOG, "a", buffering=1) as f, contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        print(f"{time.strftime('%T')} keeper holding {name} {lo}..{hi} {adapt_options}", flush=True)
        t_end = time.time() + hours * 3600
        while time.time() < t_end:
            try:
                if not alive():
                    print(f"{time.strftime('%T')} {name} is not running; keeper exiting", flush=True)
                    return "keeper exited"
                try:
                    cl = attach_existing(name, workspace, cluster_id)
                except SystemExit as e:
                    print(f"{time.strftime('%T')} {e}; keeper exiting", flush=True)
                    return "keeper exited"
                cl.adapt(minimum=lo, maximum=hi, **adapt_options)
                box = [cl.get_client()]

                def run_client():   # the inner loop keeps one client; a closed one is replaced
                    box[0] = live_client(cl, box[0])
                    return box[0]
                while time.time() < t_end:
                    try:
                        if not alive():
                            break
                    except Exception:  # noqa: BLE001 — an API hiccup is not a stop
                        pass
                    now = time.time()
                    # Not yet populated (start-up, reconnect): retried with backoff; past the
                    # bound the error goes to the reconnect path below with a log line.
                    winfo = scheduler_workers(run_client, attempts=6, delay_s=5.0, log=print)
                    c = box[0]
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
    p = KEEPER_LOG
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
    # Phase 49 β: a pending scope sign-off is shown instead (scope_signoff.md, written by main.py).
    sc = subprocess.run([sys.executable, "scripts/scope_signoff.py", "status", rd], cwd=f"{WORK}/{label}",
                        capture_output=True, text=True).stdout.strip()
    if sc == "awaiting" and os.path.exists(rd + "scope_signoff.md"):
        return {"run_dir": rd, "status": "awaiting scope sign-off", "brief_md": open(rd + "scope_signoff.md").read()}
    return {"run_dir": rd, "status": r.stdout.strip() or r.stderr.strip(), "brief_md": md}


def sign_side(label, answers_yaml, note, run_name=None, expect_spec_sha256=None, _runner=None, action=None,
              expect_derivations_sha256=None, expect_sheet_sha256=None):
    """The owner's answer for the newest run on `label`. `run_name` (the run console passes it): the run dir the
    owner was shown; another newest run is refused. `action` is explicit when given: "spec" signs the spec through
    agents/stage_machine.py sign with the expected hashes (no sha: refused), "scope" answers the pending scope
    sign-off. An expected spec sha always means sha-bound spec signing. With neither (a hand-typed CLI call): the
    pending scope sign-off, else a v2 run's spec (refused without a sha), else the v1 brief. `_runner`: self-test."""
    import os, subprocess, sys, tempfile
    run = _runner or subprocess.run
    rd = _run_dir(label)
    if not rd:
        return {"rc": 1, "out": "no run directory yet"}
    name = os.path.basename(rd.rstrip("/"))
    if run_name is not None and name != run_name:
        return {"rc": 1, "out": f"not signed: the newest run on {label} is {name}, not {run_name} (the run shown)"}
    if action not in (None, "spec", "scope"):
        return {"rc": 1, "out": f"not signed: unknown action {action!r}"}
    env = {k: v for k, v in os.environ.items() if k != "MA_SIGNOFF_MODE"}
    if action is None and expect_spec_sha256:
        action = "spec"
    elif action is None:
        # Phase 49 β: a pending scope sign-off (AWAIT_OWNER) is answered first; else the spec or the brief.
        st = run([sys.executable, "scripts/scope_signoff.py", "status", rd], cwd=f"{WORK}/{label}",
                 env=env, capture_output=True, text=True).stdout.strip()
        action = ("scope" if st == "awaiting" else "spec" if os.path.exists(os.path.join(rd, "run_state.json"))
                  else "brief")
    if action == "spec":
        if not expect_spec_sha256:
            return {"rc": 1, "out": "not signed: a spec is signed with the sha256 of the spec the owner read "
                                    "(--expect-spec-sha256)"}
        cmd = [sys.executable, "agents/stage_machine.py", "sign", rd, "--by", "owner", "--note", note,
               "--expect-sha256", expect_spec_sha256]
        for flag, v in (("--expect-derivations-sha256", expect_derivations_sha256),
                        ("--expect-sheet-sha256", expect_sheet_sha256)):
            if v:
                cmd += [flag, v]
        r = run(cmd, cwd=f"{WORK}/{label}", env=env, capture_output=True, text=True)
        return {"rc": r.returncode, "out": (r.stdout + r.stderr).strip()}
    fd, p = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, answers_yaml.encode())
    os.close(fd)
    try:
        script = "scripts/scope_signoff.py" if action == "scope" else "scripts/decision_brief.py"
        r = run([sys.executable, script, "sign", rd, "--by", "owner", "--answers", p, "--note", note],
                cwd=f"{WORK}/{label}", env=env, capture_output=True, text=True)
    finally:
        os.remove(p)
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


def _load_by_path(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cluster_env(work):
    """The env a job's compute.py run_map needs to send its chunks to this cluster's workers: the scheduler address
    and this worker's TLS credentials (compute.export_worker_env, from the shipped tree, loaded by path under a private
    name), written outside the job's out/ so they are never fetched or stored."""
    import os
    p = os.path.join(work, "scripts", "compute.py")
    if not os.path.exists(p):
        raise RuntimeError("cluster compute needs the shipped tree's scripts/compute.py")
    return _load_by_path("_ma_job_compute", p).export_worker_env(os.path.join(os.path.dirname(work), ".tls"))


def _shutdown_cluster():
    """Stop this cluster from inside one of its tasks (as run_to_disk does once every run is stored).
    Idempotent: when the cluster is already stopping (the launcher stopped it first), the
    request is dropped, never raised."""
    try:
        from distributed import get_client
        get_client().shutdown()
    except Exception:  # noqa: BLE001 -- already stopping: nothing left to stop
        pass


def _job_shutdown(grace_s):
    """The job's own stop, after `grace_s` (so a launcher polling the job reads its final state
    and stops the cluster itself first)."""
    import time
    if grace_s:
        time.sleep(grace_s)
    _shutdown_cluster()


def store_outputs(out, prefix, account, store, beat=None):
    """Upload every file under `out` (no links; never a sensitive path: store.is_sensitive_path) to
    <prefix>/<rel>, then read each back from storage and compare sha256 with the local file. Returns
    {"prefix", "files": {rel: sha256}, "skipped", "verified", "why"}; any error is recorded, not raised."""
    import hashlib, os, tempfile
    sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()  # noqa: E731
    rec = {"prefix": prefix, "files": {}, "skipped": [], "verified": False, "why": ""}
    try:
        rels = []
        for root, _, fns in os.walk(out):
            for fn in sorted(fns):
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, out).replace(os.sep, "/")
                if os.path.islink(p) or store.is_sensitive_path(rel):
                    rec["skipped"].append(rel)
                else:
                    rels.append(rel)
        rec["skipped"].sort()
        for rel in sorted(rels):
            store.upload_retry(os.path.join(out, rel), f"{prefix}/{rel}", account)
            rec["files"][rel] = sha(os.path.join(out, rel))
            if beat:
                beat()
        bad = []
        with tempfile.TemporaryDirectory() as td:
            for rel, h in rec["files"].items():
                back = os.path.join(td, "back")
                store.download(f"{prefix}/{rel}", back, account)
                if sha(back) != h:
                    bad.append(rel)
                if beat:
                    beat()
        if bad:
            rec["why"] = f"{len(bad)} stored copies differ from the job's files (sha256), e.g. {bad[:3]}"
        elif not rec["files"]:
            rec["why"] = "the job wrote no outputs to store"
        else:
            rec["verified"] = True
    except Exception as e:  # noqa: BLE001 -- unverified, never raised: the cluster then idles out for collection
        rec["why"] = f"{type(e).__name__}: {e}"[:300]
    return rec


def job_task(job_id, code, cmd, secrets=None, max_hours=24.0, setup_sdk=False, heartbeat_s=30, token=None,
             cluster_compute=False, store=None, shutdown_when_stored=False, store_mod=None,
             shutdown_grace_s=0, shutdown_always=False):
    """The job, as a dask task (see _job_task). With `shutdown_always`, a job that claimed its
    id and then ends in any way (an exception included, e.g. a state write on a full disk)
    requests its cluster's stop exactly once; a refused submission stops nothing."""
    ctl = {"claimed": False, "stopped": False}
    try:
        return _job_task(job_id, code, cmd, secrets, max_hours, setup_sdk, heartbeat_s, token, cluster_compute,
                         store, shutdown_when_stored, store_mod, shutdown_grace_s, shutdown_always, ctl)
    finally:
        if shutdown_always and ctl["claimed"] and not ctl["stopped"]:
            _job_shutdown(shutdown_grace_s)


def _job_task(job_id, code, cmd, secrets, max_hours, setup_sdk, heartbeat_s, token, cluster_compute, store,
              shutdown_when_stored, store_mod, shutdown_grace_s, shutdown_always, ctl):
    """The job, as a dask task: claim the id (the 'queued' record with this submission's
    token; with no token, an id that is neither queued nor running), unpack `code` (a
    tar.gz, or None), run `cmd` (a shell string or an argv list) in it with JOB_OUT set,
    heartbeat into state.json throughout, stop it after `max_hours`. Secrets go to the
    job's environment only, never to a file. The state record is replaced, never removed.

    Batch mode (one cluster, one scheduler, N workers): `cluster_compute` gives the job the
    cluster's scheduler, so its compute.py run_map spreads chunks over every worker of this
    cluster; `store` ({"prefix", "account"}) uploads $JOB_OUT to storage when the job ends and
    reads every file back (store_outputs); `shutdown_when_stored` then stops the cluster from
    here, but only for a job that is done with every output verified in storage. Otherwise
    the cluster is left to its idle timeout, so the outputs can still be collected. None of
    this depends on the laptop. `shutdown_always` (`vm_job.py run` without --keep-on-failure)
    stops the cluster at the end whatever the outcome: failed, timed out, unverified, or a
    command that cannot start. `shutdown_grace_s` delays either stop (the final state is
    already written), so a poller (`vm_job.py run`) reads the final state and stops the
    cluster itself; with no poller, the job still stops it after the grace period."""
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
        ctl["claimed"] = True     # from here every way out requests the stop (job_task)
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
        if cluster_compute:
            env.update(_cluster_env(work))
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
        st.update(state="failed", error=f"{type(e).__name__}: {e}"[:500], ended=time.time(), heartbeat=time.time(),
                  shutdown=bool(shutdown_always))
        try:
            _job_write(d, st)
        finally:   # a failed write (disk full) never skips the stop
            if shutdown_always:
                ctl["stopped"] = True
                _job_shutdown(shutdown_grace_s)
        return st
    final = dict(state="timed_out" if st.get("timed_out") else ("done" if rc == 0 else "failed"), rc=rc,
                 ended=time.time(), duration_s=round(time.time() - t0))
    if store:
        import threading
        lock, done_ev = threading.Lock(), threading.Event()

        def beat():
            with lock:
                st["heartbeat"] = time.time()
                _job_write(d, st)

        def beater():   # one upload or read-back can take longer than JOB_STALE_S: never read as 'lost'
            while not done_ev.wait(heartbeat_s):
                beat()
        t = threading.Thread(target=beater, daemon=True)
        t.start()
        try:
            smod = store_mod or _load_by_path("_ma_job_results_store", os.path.join(work, "scripts",
                                                                                     "results_store.py"))
        except Exception as e:  # noqa: BLE001
            stored = {"prefix": store.get("prefix"), "verified": False,
                      "why": f"no results_store in the shipped tree ({type(e).__name__}: {e})"[:300]}
        else:
            stored = store_outputs(out, store["prefix"], store.get("account"), smod, beat)
        finally:
            done_ev.set()
            t.join()
        st["stored"] = stored
    st.update(final, heartbeat=time.time())
    down = bool(shutdown_always or (shutdown_when_stored and st["state"] == "done"
                                    and (st.get("stored") or {}).get("verified")))
    st["shutdown"] = down
    try:
        _job_write(d, st)
    finally:   # a failed final-state write (disk full) never skips the stop
        if down:
            ctl["stopped"] = True
            _job_shutdown(shutdown_grace_s)
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
