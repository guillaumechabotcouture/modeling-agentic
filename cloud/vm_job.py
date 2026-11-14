"""Durable detached jobs on a small Coiled VM (the dev VM, or any one-VM cluster).

The default for one job: `run` brings the cluster up, submits the job at once, polls every
`--every` seconds (status line, outputs saved), then stops the cluster by id. `--store` is
required: success means the job is done and every output is verified in storage. It exits
non-zero with the reason otherwise. Whatever ends it (success, failure, an error, Ctrl-C,
a constructor that raised after allocating), the cluster is stopped, unless
`--keep-on-failure` and the job did not succeed. The cluster gets a name of its own,
<name>-<job_id>-<6 hex>, recorded with its id in job.json, so no other launcher can race it
for the name; `status`, `wait` and `down <name> <job_id>` find it there. A live cluster
named <name>, or one already running for this job, is refused (no second launch), and a
lock on the job's local directory refuses a second `run` of the job on this machine. It takes
every `up` and `start` option; its polls and stops use the cluster object it created and
its id, never an attach by name.

    python cloud/vm_job.py run <name> <job_id> --tree . --store outputs/<prefix> --cap-usd 20 [--workers N]
        [--every 60] [--keep-on-failure] [--budget-scope f6] -- <command ...>
    python cloud/vm_job.py down <name> <job_id>      # stop a kept cluster (found through job.json)

Budget (F6 Stage 2; on 2026-11-11 an agent spent $106 of a $100 budget). `run`, `up` and `start` require
`--cap-usd`. They refuse, before any job is submitted or, for `run`, any cluster listed, unless the owner-signed
budget of the cluster name's scope (cloud/budget.py, cloud/budget_scopes.yaml) covers the scope's spend plus the
cap. `run` and `up` reserve the cap; `up` does so only once it will construct a cluster. Each `run` poll saves the
outputs, then measures the job cluster's USD before it returns any state, the job's loss included. The cluster is
stopped by id, --keep-on-failure or not, in three cases: at the cap (after one last copy of the outputs), after
three failed readings in a row, or after a failed reading at a terminal state.

Why one command. On 2026-11-03 an agent ran `up`, ended its turn to wait for the cluster,
and never sent `start`: the VM idled out after 20 minutes with no job, and about two hours
were lost. Coiled counts `idle_timeout` from cluster start, not from the first task (there
is no setting for "only after work"), so an `up` with no `start` behind it is wasted.
`up`, `start`, `status`, `wait` and `down` remain for interactive use; `up` warns.

    python cloud/vm_job.py up <name> [--cpu 4] [--memory "16 GiB"] [--idle-timeout "20 minutes"]
    python cloud/vm_job.py start <name> <job_id> [--tree PATH | --dir PATH] [--max-hours 24]
                                  [--setup-sdk] [--anthropic-key] -- <command ...>
    python cloud/vm_job.py status <name> <job_id>          # state + log tail; saves outputs locally
    python cloud/vm_job.py wait <name> <job_id> [--every 300]   # poll until the job ends, saving each time
    python cloud/vm_job.py down <name>
    python cloud/vm_job.py --self-test

Why. Coiled stops a cluster after `idle_timeout` of scheduler idleness, and only dask
tasks count: a job started with client.run, or a process it spawned, is invisible to it.
Twice on 2026-10-28 a detached job on the dev VM (45-minute idle timeout) was stopped
mid-run and its outputs lost. Here the job runs inside a dask task submitted with
fire_and_forget (worker_side.job_task), so the scheduler is busy for exactly the job's
lifetime whatever the idle timeout; the idle timeout only has to cover fetching after the
job ends. The job writes its outputs under $JOB_OUT as it goes, and every `status` / `wait`
poll copies new or changed files (and the log) to <out>/<name>/<job_id>/, so a VM that
stops for any other reason loses at most one poll interval of outputs.

The job's command runs in the shipped tree (`--tree`: git archive of HEAD; `--dir`: a
plain directory), with secrets (`--anthropic-key`) in its environment only.

A command that runs main.py must state its pipeline (`--v2` or `--v1`; refused otherwise, before
anything is created: main.py's default changed once, 2026-11-05). The job's environment names
its launcher (MA_LAUNCHER=vm_job:<name>/<job_id>, recorded in the run's metadata.json), and
`start`/`run` write a launch record in the eval grader's format (launch_id, pipeline, commit as
stamped in the archive, eval_inputs_sha256): <out>/<name>/<job_id>/assignments.json and
<out>/<name>/<job_id>/out/result.json, plus <out>/<name>/assignments.json listing every job. A
main.py job that copies its run to $JOB_OUT/runs/ is then graded with
`scripts/eval_suite.py grade <q> <out>/<name>/<job_id>/out/runs/<run>` with its input hashes
checked. A relaunch of a job id sets the earlier out/ aside. A wrapper script that calls main.py
is not seen: name the pipeline inside it. A shell comment in a main.py command is refused. `--setup-sdk`
installs the pinned claude-agent-sdk from the tree's requirements.txt first (the useful part
of keep/coiled_run/dev_vm.py, which this replaces).

Batch mode: one cluster, one scheduler, N workers for a whole compute.py batch.

    python cloud/vm_job.py up <name> --workers N [--cpu 8 --memory "32 GiB"]
    python cloud/vm_job.py start <name> <job_id> --tree . --cluster-compute \\
        --store outputs/<prefix> [--store-account A] --shutdown-when-stored -- <command ...>

`--cluster-compute` gives the job this cluster's scheduler, so its `compute.run_map` (v2's
`toolkit/analysis/batch.run`) spreads chunks over the cluster's workers. compute.py gives
chunks only to workers with nothing processing, and the job's own worker is processing the
job, so N workers give N - 1 compute VMs: ask for one more than the batch needs.
`--store` uploads $JOB_OUT to <prefix>/ when the job ends and reads every file back
(sha256); `--shutdown-when-stored` then stops the cluster from inside the job, but only
when the job is done and every output is verified in storage. A failed or unverified job
leaves the cluster to its idle timeout, so its outputs can still be collected. Nothing
waits on the laptop: start returns at once and the cluster stops itself. Under `run`, the
job stops its own cluster at the end whatever the outcome (failed, timed out, unverified,
a command that cannot start; only on success under --keep-on-failure), after a grace
period of two poll intervals plus a minute. So `run` normally reads the final state and
stops the cluster first, and if the laptop is gone the job still stops it. Either side's
stop request is harmless when the other came first. (The Nigeria
re-run used six one-worker clusters, each with its own scheduler, about 5 minutes of
start-up and a 20-minute idle tail, shut down by a watcher on the laptop.)

Rules: the IDM Coiled default workspace only; attach-only for everything but `up` and
`run`'s creation step (coiled.Cluster(name=...) on a cluster that is gone creates a new,
empty one), and those create only once no cluster of the name is live or stopping. `run`
never attaches by name: it keeps the object it created and stops by cluster id (or, when
the constructor raised before returning one, by its unique name).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_cluster as rc  # noqa: E402
import worker_side as ws  # noqa: E402
import eval_paths  # noqa: E402  (scripts/lib, on the path through run_cluster)
import pipeline_id  # noqa: E402

OUT = os.path.expanduser("~/modeling-agentic-keep/vm_jobs")
CHUNK = 8 * 2**20
DONE = ("done", "failed", "timed_out", "refused")


def files_to_fetch(remote: dict, index: dict) -> list[str]:
    """Remote files that are new or changed (size or mtime) since the last fetch."""
    return sorted(rel for rel, sig in (remote or {}).items() if list(index.get(rel) or []) != list(sig))


def split_command(argv: list) -> tuple[list, list]:
    """vm_job's own arguments, and the job's command after the first `--`."""
    if "--" not in argv:
        return list(argv), []
    i = argv.index("--")
    return list(argv[:i]), list(argv[i + 1:])


def local_dir(name: str, job_id: str, root: str = OUT) -> str:
    ws._job_dir(job_id)   # validates the id
    return os.path.join(root, name, job_id)


def sync_outputs(run, job_id: str, dest: str) -> dict:
    """One poll: the job's state, then every new or changed output file into `dest`
    (written to a temporary name and renamed, so a partial download never replaces a
    good copy). `run(fn, *args)` calls a worker_side function on the job's VM."""
    os.makedirs(dest, exist_ok=True)
    st = run(ws.job_state, job_id)
    ip = os.path.join(dest, ".manifest.json")
    index = json.load(open(ip)) if os.path.exists(ip) else {}
    got = []
    for rel in files_to_fetch(st["files"], index):
        size = st["files"][rel][0]
        target = os.path.join(dest, rel)
        if not os.path.realpath(target).startswith(os.path.realpath(dest) + os.sep):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp, have = target + ".part", 0
        with open(tmp, "wb") as f:
            while have < size:
                b = run(ws.job_chunk, job_id, rel, have, min(CHUNK, size - have))
                if not b:
                    break
                f.write(b)
                have += len(b)
        os.replace(tmp, target)
        index[rel] = st["files"][rel]
        got.append(rel)
    json.dump(index, open(ip, "w"), indent=1)
    json.dump(st["state"], open(os.path.join(dest, "state.json"), "w"), indent=1)
    st["fetched"] = got
    return st


def job_pipeline(cmd) -> str | None:
    """The pipeline a main.py command states, None for a command that runs no main.py; refuses a
    main.py command that relies on main.py's default. A one-element command is a shell string
    (as `start` runs it)."""
    if isinstance(cmd, (list, tuple)) and len(cmd) == 1:
        cmd = cmd[0]
    try:
        return pipeline_id.from_command(cmd)
    except pipeline_id.PipelineError as e:
        raise SystemExit(f"{e} (add --v2 or --v1 to main.py's arguments): refusing") from None


def job_env(secrets: dict | None, name: str, job_id: str) -> dict:
    """The job's extra environment: its secrets, and its launcher's name for main.py's metadata.json."""
    return {**(secrets or {}), "MA_LAUNCHER": f"vm_job:{name}/{job_id}"}


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1)
    os.replace(tmp, path)


def write_launch_record(dest: str, job_id: str, code: bytes | None, pipeline: str | None, commit: str,
                        name: str) -> dict:
    """The job's launch record. Polls save $JOB_OUT under <dest>/out/, so a run lands at
    <dest>/out/runs/<run> and the grader's label dir (scripts/eval_suite.py launch_record_for) is
    <dest>/out: the record goes to <dest>/assignments.json["out"] and <dest>/out/result.json, the
    format cloud/run_cluster.py writes. <out>/<name>/assignments.json lists every job of <name>.
    An earlier attempt of this job id never passes for this one (Codex r1, r2): its out/ is set
    aside and the poll index reset."""
    import secrets
    rec = {"launch_id": f"{job_id}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}", "job_id": job_id,
           "commit": commit, "pipeline": pipeline or "none", "launcher": f"vm_job:{name}/{job_id}",
           "submitted": time.strftime("%F %T"), "eval_inputs_sha256": eval_paths.archive_sha256(code) if code else {}}
    os.makedirs(dest, exist_ok=True)
    out = os.path.join(dest, "out")
    if os.path.exists(out):
        os.rename(out, os.path.join(dest, f"out.before_{rec['launch_id']}"))
    if os.path.exists(os.path.join(dest, ".manifest.json")):
        os.remove(os.path.join(dest, ".manifest.json"))
    os.makedirs(out)
    _write_json(os.path.join(dest, "assignments.json"), {"out": rec})
    _write_json(os.path.join(out, "result.json"), {"launch_id": rec["launch_id"], "job_id": job_id,
                                                   "pipeline": rec["pipeline"], "commit": commit})
    every = os.path.join(os.path.dirname(dest), "assignments.json")
    try:
        allj = json.load(open(every))
    except (OSError, ValueError):
        allj = {}
    allj = allj if isinstance(allj, dict) else {}
    allj[job_id] = rec
    _write_json(every, allj)
    return rec


def _launch_commit(code: bytes | None, tree: str | None, src_dir: str | None) -> str:
    """The commit of the code that ships: git archive's stamp inside the archive (so a checkout
    that moves on while the VM starts does not change it), else the tree or directory's."""
    stamped = rc.archive_commit(code) if code else None
    if stamped or not tree:
        return stamped or pipeline_id.UNKNOWN       # a --dir archive is no commit: unknown, never the live HEAD
    return _code_commit(tree, None)


def _code_commit(tree: str | None, src_dir: str | None) -> str:
    if tree:
        try:
            return subprocess.check_output(["git", "-C", tree, "rev-parse", "--short=7", "HEAD"], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            return pipeline_id.UNKNOWN
    return pipeline_id.code_commit(src_dir) if src_dir else pipeline_id.UNKNOWN


def _tarball(tree: str | None, src_dir: str | None) -> bytes | None:
    if tree:
        if subprocess.check_output(["git", "-C", tree, "status", "--porcelain"], text=True).strip():
            raise SystemExit(f"{tree} has uncommitted changes: commit them (the job ships HEAD)")
        return subprocess.check_output(["git", "-C", tree, "archive", "--format=tar.gz", "HEAD"])
    if src_dir:
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            t.add(src_dir, arcname=".")
        return b.getvalue()
    return None


class WorkerGone(RuntimeError):
    """The job's own worker is not in the cluster's worker table."""


def _job_record(dest) -> dict:
    """job.json (written by `start` and `run`), or {}."""
    try:
        rec = json.load(open(os.path.join(dest, "job.json")))
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _job_worker(dest):
    """The worker the job was submitted to, or None."""
    return _job_record(dest).get("worker")


def _job_cluster(dest, name):
    """The cluster the job runs on: `run`'s unique name from job.json, else `name`."""
    return _job_record(dest).get("cluster") or name


def _slug(x: str) -> str:
    import re
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", str(x).lower())).strip("-")


def run_cluster_name(name: str, job_id: str, suffix: str | None = None) -> str:
    """`run`'s cluster name: <name>-<job_id>-<6 hex>, lowercase letters, digits and hyphens,
    starting with a letter, at most 63 characters. Ownership is this name: no other launcher
    creates it, so no check-then-create race with a same-name launcher can arise."""
    import secrets
    sfx = suffix or secrets.token_hex(3)
    base = "-".join(p for p in (_slug(name), _slug(job_id)) if p)
    if not base[:1].isalpha():
        base = "r-" + base
    return f"{base[:63 - 1 - len(sfx)].rstrip('-')}-{sfx}"


def _client(name, cluster=None, want=None):
    """The VM cluster's client and its worker. The table is read with worker_side's bounded
    reader (scheduler_info() is {} while the client is not connected; a closed client is
    replaced from the cluster), so only a connected scheduler with no worker says so.
    `cluster` is the object `run` created (its clients come from that object, never from a
    coiled.Cluster(name=...) attach); otherwise the guarded attach by name. `want` is the
    job's own worker: the job's state lives on that VM's disk only, so a poll goes there
    (another worker would read 'missing'), and its absence raises WorkerGone."""
    cl, box = (cluster if cluster is not None else rc.attach(name)), [None]

    def client():
        box[0] = ws.live_client(cl, box[0])
        return box[0]

    try:
        w = sorted(ws.scheduler_workers(client, log=print))
    except ws.SchedulerInfoUnavailable as e:
        raise SystemExit(f"{name}: {e}") from e
    if want is not None and want not in w:     # an empty table included: the job's VM is gone
        raise WorkerGone(f"the job's worker {want} is not in {name}'s worker table")
    if not w:
        raise SystemExit(f"{name} has no worker yet")
    c = client()
    c.upload_file(os.path.join(HERE, "worker_side.py"))   # the side functions below live in it
    return c, (want if want is not None else w[0])


def _runner(c, w):
    return lambda fn, *a: c.run(fn, *a, workers=[w], wait=True, on_error="raise")[w]


def _create(a, if_live="keep", box=None, gate=False):
    """Create the cluster once no cluster of that name is live or stopping. A live one is kept
    (`up`: "already up", returns False) or refused (`run`: no second launch on a cluster that
    may hold another job); a stopping one is waited out, never attached to or re-created early.
    The new cluster object goes into `box["cl"]` as soon as the constructor returns (`run`
    stops it by id whatever happens next; if the constructor raises, by its unique name)."""
    import coiled
    for _ in range(40):
        act = rc.keeper_wait(rc.cluster_states(a.name))
        if act == "stop":
            if if_live == "refuse":
                raise SystemExit(f"{a.name} is already running; `run` never launches on an existing cluster "
                                 f"(use `start {a.name} <job_id>` to add a job to it, or another name)")
            print(f"{a.name} is already up")
            return False
        if act == "start":
            break
        time.sleep(15)    # stopping: never create (or attach) until it has stopped
    else:
        raise SystemExit(f"{a.name} is still stopping; try again later")
    if gate:                    # `up`: checked and reserved only now that a cluster will be constructed
        _budget_gate(a, a.name)
    n = int(getattr(a, "workers", 1) or 1)
    if box is not None:
        box["constructing"] = True   # from here a cluster with this (unique, for `run`) name may exist
    cl = coiled.Cluster(name=a.name, workspace=rc.WORKSPACE, n_workers=n, worker_cpu=a.cpu, worker_memory=a.memory,
                        scheduler_cpu=2, spot_policy="on-demand", idle_timeout=a.idle_timeout,
                        shutdown_on_close=False)
    if box is not None:
        box["cl"] = cl
    # the first worker is enough to start: compute.py re-reads the worker table every 2 s and gives a worker that
    # joins later a chunk at once (Phase 45 δ)
    cl.get_client().wait_for_workers(1, timeout=1500)
    print(f"{a.name} up with {n} worker(s) requested (idle timeout {a.idle_timeout}; a running job holds it "
          "regardless)")
    return True


def _budget_gate(a, name, meter=False, reserve=True):
    """Refuse (SystemExit) unless the owner-signed budget of `name`'s scope covers its spend plus --cap-usd (with
    `reserve`, the cap is reserved until the cluster appears); with `meter`, return fn(cluster id) -> that cluster's
    USD so far (cloud/budget.py)."""
    import budget
    scope = a.budget_scope or budget.scope_for_name(name)
    if not scope:
        raise SystemExit(f"{name} matches no budget scope in cloud/budget_scopes.yaml: refusing (its spend would "
                         "count against no budget)")
    ok, why = budget.launch_budget_ok(scope, a.cap_usd, name=name, reserve=reserve)
    if not ok:
        raise SystemExit(f"{name}: the budget refuses this launch: {why}")
    print(f"budget: {why}", flush=True)
    return budget.job_meter(scope) if meter else None


def _cap_reading(meter, cid, cap, job_id, misses, final=False):
    """None while the job cluster's USD is under `cap`; at the cap, or after three failed readings in a row (fail
    closed), the state that ends `run`. `final` (the job just reached a terminal state, so no later reading
    follows): a failed reading is not a conclusive cap decision, and stops the cluster."""
    try:
        usd = meter(cid)
        misses[0] = 0
        print(f"{job_id}: cluster {cid} at ${usd:.2f} of its ${cap:.2f} cap", flush=True)
        if usd < cap:
            return None
        why = f"cost cap reached: cluster {cid} at ${usd:.2f}, cap ${cap:.2f}"
    except Exception as e:  # noqa: BLE001
        misses[0] += 1
        print(f"{job_id}: cost reading failed ({type(e).__name__}: {str(e)[:120]})", flush=True)
        if misses[0] < 3 and not final:
            return None
        why = f"cost cap: the cluster's cost could not be read 3 times in a row ({type(e).__name__}); fail closed"
    return {"state": {"job_id": job_id, "state": "over_cap", "why": why}, "files": {}, "fetched": [], "log_tail": ""}


def cmd_up(a):
    if _create(a, gate=True):
        # Coiled counts idle time from cluster start, not from the first task: there is no setting for "only after
        # work", so an `up` with no `start` behind it idles out with nothing done (2026-11-03, about two hours lost)
        print(f"WARNING: {a.name} stops after {a.idle_timeout} idle unless `start` follows now. For a single job, "
              f"`run` brings the cluster up, submits, waits and stops it in one command.")


def cmd_start(a, cluster=None):
    from distributed import fire_and_forget
    cmd = a.command
    if not cmd:
        raise SystemExit("no command given (put it after --)")
    pipeline = job_pipeline(cmd)        # a main.py job states its pipeline, or nothing starts
    if cluster is None:                 # interactive start keeps a paid cluster busy: the budget must still cover it
        _budget_gate(a, a.name, reserve=False)
    code = a._code if hasattr(a, "_code") else _tarball(a.tree, a.dir)
    base = getattr(a, "base_name", a.name)
    secrets = job_env({"ANTHROPIC_API_KEY": rc.read_keys()["ANTHROPIC_API_KEY"]} if a.anthropic_key else None,
                      base, a.job_id)
    c, w = _client(a.name, cluster)
    token = _runner(c, w)(ws.job_queue, a.job_id)
    if not token:
        raise SystemExit(f"{a.job_id} is queued or running on {a.name}; pick another id")
    shell = " ".join(cmd) if len(cmd) == 1 else cmd
    store = {"prefix": a.store.rstrip("/"), "account": a.store_account} if a.store else None
    fut = c.submit(ws.job_task, a.job_id, code, shell, secrets, a.max_hours, a.setup_sdk, token=token,
                   cluster_compute=a.cluster_compute, store=store, shutdown_when_stored=a.shutdown_when_stored,
                   shutdown_grace_s=getattr(a, "shutdown_grace_s", 0),
                   shutdown_always=getattr(a, "shutdown_always", False), workers=[w], allow_other_workers=False,
                   pure=False, key=f"job-{a.job_id}-{int(time.time())}")
    fire_and_forget(fut)
    dest = getattr(a, "dest", None) or local_dir(a.name, a.job_id, a.out)
    os.makedirs(dest, exist_ok=True)
    lrec = write_launch_record(dest, a.job_id, code, pipeline, _launch_commit(code, a.tree, a.dir), base)
    json.dump({"name": getattr(a, "base_name", a.name), "cluster": a.name,
               "cluster_id": getattr(cluster, "cluster_id", None) if cluster is not None else None,
               "worker": w, "job_id": a.job_id, "cmd": cmd, "submitted": time.strftime("%F %T"),
               "max_hours": a.max_hours, "cluster_compute": a.cluster_compute, "store": store,
               "shutdown_when_stored": a.shutdown_when_stored, "pipeline": lrec["pipeline"],
               "commit": lrec["commit"], "launch_id": lrec["launch_id"]},
              open(os.path.join(dest, "job.json"), "w"), indent=1)
    tail = (f"; outputs go to {store['prefix']}/ and the cluster stops itself once they are verified there"
            if a.shutdown_when_stored else "")
    print(f"started {a.job_id} on {a.name} ({w}); pipeline {lrec['pipeline']} @ {lrec['commit']}; "
          f"outputs -> {dest}{tail}")


def _print(st, dest):
    s = st["state"]
    extra = f" rc={s.get('rc')} {s.get('duration_s')}s" if s.get("state") in DONE else \
        (f" running {round(time.time() - s['started'])}s" if s.get("started") else "")
    print(f"{s.get('job_id', '?')}: {s['state']}{extra}; {len(st['files'])} file(s), fetched {len(st['fetched'])} "
          f"-> {dest}", flush=True)
    print("  " + "\n  ".join(st["log_tail"].strip().splitlines()[-5:]), flush=True)


def cmd_status(a):
    dest = local_dir(a.name, a.job_id, a.out)
    c, w = _client(_job_cluster(dest, a.name), want=_job_worker(dest))
    st = sync_outputs(_runner(c, w), a.job_id, dest)
    _print(st, dest)
    return st


def _poll(a, dest, gone_ok=False, deadline=None, cluster=None, cid=None, meter=None, flags=None):
    """Poll (saving outputs each time) until the job ends; returns its last status. With
    `gone_ok` (`run`), a cluster that is no longer running (by id `cid`) returns None, and
    every other error (a dropped connection, a failing cluster listing) is retried, up to
    `deadline`. Every poll goes to the job's own worker (job.json); that worker missing from
    the table on three polls in a row reads as 'lost'. With `meter` (`run`), each poll also reads the cluster's
    USD before anything returns: at --cap-usd it sets flags["over_cap"] and returns the job's terminal state, or
    'over_cap' while the job still runs."""
    want, gone_polls, misses = _job_worker(dest), 0, [0]

    def cap(final=False):
        over = (_cap_reading(meter, cid, a.cap_usd, a.job_id, misses, final)
                if meter is not None and cid is not None else None)
        if over is not None and flags is not None:
            flags["over_cap"] = True
        return over
    while True:
        over, checked = None, False
        try:
            try:
                c, w = _client(a.name, cluster, want)
            except WorkerGone as e:
                gone_polls += 1
                if gone_polls >= 3:
                    cap(final=True)          # a terminal state too: the cap first
                    print(f"{a.job_id}: lost ({e})")
                    return {"state": {"job_id": a.job_id, "state": "lost", "why": str(e)}, "files": {},
                            "fetched": [], "log_tail": ""}
                raise
            gone_polls = 0
            st = sync_outputs(_runner(c, w), a.job_id, dest)
            _print(st, dest)
            over, checked = cap(final=st["state"]["state"] in DONE + ("missing", "lost")), True   # outputs, cap, state
            if st["state"]["state"] in DONE + ("missing", "lost"):
                return st
            if over is not None:             # at the cap: one last copy of what the job wrote since, then stop
                try:
                    sync_outputs(_runner(c, w), a.job_id, dest)
                except Exception as e2:  # noqa: BLE001 -- best effort; the stop comes regardless
                    print(f"{a.job_id}: last copy before the stop failed ({type(e2).__name__})")
        except (Exception, SystemExit) as e:  # noqa: BLE001 — a dropped connection is not the end of the job
            if isinstance(e, SystemExit) and not gone_ok:
                raise     # `wait`/`status`: the attach guard's refusal ends the command
            if gone_ok:
                try:
                    if (_gone(a.name, cid) if cid is not None else not rc.is_running(a.name)):
                        return None
                except Exception as e2:  # noqa: BLE001 -- cannot tell: retry
                    print(f"cluster listing failed ({type(e2).__name__}: {str(e2)[:120]})")
            print(f"poll failed ({type(e).__name__}: {str(e)[:120]}); retrying")
        if not checked:
            over = cap()
        if over is not None:
            return over
        if deadline is not None and time.time() > deadline:
            over = cap(final=True)       # no exit without a cap decision: unreadable at the deadline stops it
            if over is not None:
                return over
            raise SystemExit(f"{a.job_id}: still not finished {a.max_hours} h after submission (plus 1 h); "
                             f"`status {a.name} {a.job_id}` checks it")
        time.sleep(a.every)


def cmd_wait(a):
    dest = local_dir(a.name, a.job_id, a.out)
    a.name = _job_cluster(dest, a.name)
    return _poll(a, dest)


def run_outcome(st, store_requested) -> tuple[bool, str]:
    """Whether `run` succeeded, and why not: the job is done and, when storage was asked for,
    every output is verified there. No final state read (the cluster stopped first) is a
    failure, never taken as success."""
    if st is None:
        return False, ("the cluster stopped before the job's final state was read (the outputs saved so far are "
                       "local; check the store prefix)")
    s = st["state"]
    if s.get("state") != "done":
        bits = [s.get("state", "?")] + [f"{k}={s[k]}" for k in ("rc", "error", "why") if s.get(k) not in (None, "")]
        return False, " ".join(str(b) for b in bits)
    stored = s.get("stored") or {}
    if store_requested and not stored.get("verified"):
        return False, f"done, but its outputs are not verified in storage: {stored.get('why') or 'no record'}"
    return True, ""


_ENDED = ("stopping", "stopped", "error")


def _clusters(name) -> dict:
    """{cluster id: state} of every cluster with exactly this name (Coiled keeps stopped ones listed)."""
    return {c.get("id"): ws.state_of(c) for c in _all_clusters() if c.get("name") == name}


def _all_clusters() -> list:
    import coiled
    cs = coiled.list_clusters(workspace=rc.WORKSPACE)
    return cs if isinstance(cs, list) else list(cs.values())


def _gone(name, cid) -> bool:
    return _clusters(name).get(cid, "stopped") in _ENDED


def _stop_by_id(name, cid, why):
    """Stop the cluster `run` created, by id (coiled.delete_cluster(cluster_id=...), never an
    attach by name). With no id (the constructor raised or was interrupted after allocating),
    it is the cluster with exactly this name: `run`'s names are unique, so that is never
    another launcher's. A cluster already stopping or stopped (the job stopped it) is left
    alone, and a failed stop request is reported, not raised: the second of two stop requests
    is harmless."""
    import coiled
    live = None
    for i in range(3):
        try:
            live = _clusters(name)
            break
        except Exception as e:  # noqa: BLE001
            print(f"{name}: cluster listing failed ({type(e).__name__}: {str(e)[:120]})")
            if i < 2:
                time.sleep(5)
    if cid is None and live is None:
        print(f"{name}: could not find the new cluster to stop it; it stops after its idle timeout")
        return
    for i in ([cid] if cid is not None else sorted(live)):
        state = (live or {}).get(i, "") if live is not None else "unknown"
        if live is not None and (i not in live or state in _ENDED):
            print(f"{name} ({i}): already {state or 'gone'} ({why})")
            continue
        try:
            coiled.delete_cluster(cluster_id=i, workspace=rc.WORKSPACE)
            print(f"{name} ({i}): stopped ({why})")
        except Exception as e:  # noqa: BLE001
            print(f"{name} ({i}): stop request failed ({type(e).__name__}: {str(e)[:120]}); the job stops it after "
                  f"its grace period, or it stops after its idle timeout")


def cmd_run(a):
    """up + start + wait + down in one command, with no gap for a human or agent to fill:
    the job is submitted as soon as the first worker is up, then polled every `--every`
    seconds (outputs saved each time) until it ends, then the cluster is stopped by id. A
    cluster of this name that is already running is never reused (no second launch). `--store`
    is required: success means done with every output verified in storage. On failure the
    command exits non-zero with the reason. Whatever ends the command (success, failure, an
    error, Ctrl-C, a constructor that raised after allocating) the cluster is stopped, unless
    --keep-on-failure and it did not succeed. Laptop-independent backstop: the job itself stops
    its cluster at the end, whatever the outcome (only on success under --keep-on-failure),
    after a grace period of two poll intervals plus a minute, so this command normally reads
    the final state and stops it first; the second stop request is then a no-op."""
    try:
        dest = local_dir(a.name, a.job_id, a.out)
    except ValueError as e:
        raise SystemExit(f"bad job id {a.job_id!r}: {e}") from e
    if not a.command:
        raise SystemExit("no command given (put it after --)")
    import fcntl
    os.makedirs(dest, exist_ok=True)
    lock = open(os.path.join(dest, ".run.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(f"{a.job_id}: another `run` of this job is in progress on this machine "
                         f"({os.path.join(dest, '.run.lock')}); `status {a.name} {a.job_id}` checks it") from None
    try:
        return _run_locked(a, dest)
    finally:
        lock.close()


def _run_locked(a, dest):
    """`run` once the job's lock is held (see cmd_run)."""
    job_pipeline(a.command)                # an undeclared main.py pipeline is refused before anything is created
    a._code = _tarball(a.tree, a.dir)      # a dirty tree is refused before anything is created
    base, unique = a.name, run_cluster_name(a.name, a.job_id)
    meter = _budget_gate(a, unique, meter=True)   # before any listing or cluster; reserved for the exact name it creates
    a.shutdown_when_stored = True
    a.shutdown_always = not a.keep_on_failure
    a.shutdown_grace_s = int(2 * a.every + 60)
    prefix = run_cluster_name(base, a.job_id, "0" * 6)[:-6]     # <name>-<job_id>-, truncated as real names are
    busy = sorted(f"{c.get('name')} ({c.get('id')})" for c in _all_clusters()
                  if ws.state_of(c) not in _ENDED + ("",)
                  and (c.get("name") == base or str(c.get("name") or "").startswith(prefix)))
    if busy:
        raise SystemExit(f"{base}: a cluster for this name or job is already running ({', '.join(busy)}); `run` "
                         f"never launches a second one (`status {base} {a.job_id}` or `down`)")
    a.base_name, a.dest, a.name = base, dest, unique
    print(f"{base}: cluster {a.name} for {a.job_id}")
    box, good, why = {}, False, "interrupted"
    try:
        try:
            _create(a, if_live="refuse", box=box)
        except Exception as e:  # noqa: BLE001 -- e.g. no worker within the wait; the finally stops it
            why = f"{a.name} did not come up: {type(e).__name__}: {e}"
            raise SystemExit(why) from e
        cl = box["cl"]
        cid = getattr(cl, "cluster_id", None)
        cmd_start(a, cluster=cl)
        deadline = time.time() + a.max_hours * 3600 + 3600
        st = _poll(a, dest, gone_ok=True, deadline=deadline, cluster=cl, cid=cid, meter=meter, flags=box)
        good, why = run_outcome(st, True)
    except KeyboardInterrupt:
        print(f"interrupted: {a.job_id} on {a.name}")
        raise
    except SystemExit as e:
        why = str(e.code)
        raise
    except Exception as e:  # noqa: BLE001
        why = f"{type(e).__name__}: {e}"
        raise
    finally:
        cl = box.get("cl")
        if "constructing" not in box:
            pass    # refused or failed before the constructor was called: nothing of ours to stop
        elif good or not a.keep_on_failure or box.get("over_cap"):
            _stop_by_id(a.name, getattr(cl, "cluster_id", None) if cl is not None else None,
                        f"{a.job_id} done" if good else f"{a.job_id}: {why}"[:200])
        elif cl is not None:
            print(f"{a.name} kept up for debugging (--keep-on-failure): it stops after {a.idle_timeout} idle, "
                  f"or now with `down {base} {a.job_id}`")
    if good:
        print(f"{a.job_id} done; outputs -> {dest} and {a.store.rstrip('/')}/ (verified)")
        return
    raise SystemExit(f"{a.job_id} on {a.name}: {why}")


def cmd_down(a):
    """`down <name>`: the guarded attach, then shutdown. `down <name> <job_id>`: the job's
    cluster from job.json (`run`'s unique name), stopped by id."""
    if getattr(a, "job_id", None):
        rec = _job_record(local_dir(a.name, a.job_id, a.out))
        if rec.get("cluster_id") is not None:
            _stop_by_id(rec.get("cluster") or a.name, rec["cluster_id"], f"down {a.name} {a.job_id}")
            return
        a.name = rec.get("cluster") or a.name
    rc.attach(a.name).shutdown()
    print(f"{a.name}: shutdown requested")


def _self_test() -> int:
    import tempfile
    fails = []
    try:
        import no_cloud
        guarded = no_cloud.active()
    except ImportError:
        guarded = False
    refused = False
    if guarded:                       # never probed without the guard: unguarded, this would be a real call
        try:
            import coiled
            coiled.list_clusters(workspace="w")
        except RuntimeError:
            refused = True
    if not (guarded and refused):
        fails.append("G2 vm_job's self-test runs under the shared refusing guard, and a real coiled call is refused")

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    ok(split_command(["start", "n", "j", "--dir", "d", "--", "python", "x.py", "--dir", "e"])
       == (["start", "n", "j", "--dir", "d"], ["python", "x.py", "--dir", "e"]) and split_command(["status", "n", "j"])
       == (["status", "n", "j"], []), "RC34b options before -- are vm_job's; everything after is the job's")
    ok(files_to_fetch({"a": [1, 2.0], "b": [3, 4.0], "c": [5, 6.0]}, {"a": [1, 2.0], "b": [3, 9.0]}) == ["b", "c"],
       "RC34 a poll fetches only new or changed files")
    for bad in ("../x", "a/b", "", "x" * 65, ".."):
        try:
            local_dir("n", bad)
            ok(False, f"RC35 a bad job id is refused: {bad!r}")
        except ValueError:
            ok(True, f"RC35 a bad job id is refused: {bad[:10]!r}")
    now = 1000.0
    ok(ws.job_view(None, now)["state"] == "missing" and ws.job_view({"state": "running", "heartbeat": now - 10}, now)
       ["state"] == "running" and ws.job_view({"state": "running", "heartbeat": now - 999}, now)["state"] == "lost"
       and ws.job_view({"state": "done", "heartbeat": 0}, now)["state"] == "done",
       "RC36 a running job whose heartbeat stopped reads as lost")
    with tempfile.TemporaryDirectory() as d:
        old = ws.JOBS
        ws.JOBS = os.path.join(d, "vm")
        try:
            code = io.BytesIO()
            with tarfile.open(fileobj=code, mode="w:gz") as t:
                src = b"import os, time\nfor i in range(3):\n    open(os.path.join(os.environ['JOB_OUT'], f'step{i}.txt'), 'w').write(str(i))\n    print('step', i, flush=True)\n    time.sleep(0.2)\nprint('key set:', bool(os.environ.get('SECRET_X')))\n"
                ti = tarfile.TarInfo("job.py")
                ti.size = len(src)
                t.addfile(ti, io.BytesIO(src))
            st = ws.job_task("j1", code.getvalue(), [sys.executable, "job.py"], {"SECRET_X": "s3cr3t"}, 1.0,
                             heartbeat_s=1)
            ok(st["state"] == "done" and st["rc"] == 0, f"RC37 a job runs to completion in its own tree: {st}")
            raw = open(os.path.join(ws.JOBS, "j1", "state.json")).read()
            ok("s3cr3t" not in raw and "SECRET_X" not in raw, "RC38 secrets reach the job's environment, never its state file")
            run = lambda fn, *a: fn(*a)  # noqa: E731
            dest = os.path.join(d, "local")
            s1 = sync_outputs(run, "j1", dest)
            ok(sorted(s1["fetched"]) == ["job.log", "out/step0.txt", "out/step1.txt", "out/step2.txt"]
               and open(os.path.join(dest, "out", "step2.txt")).read() == "2"
               and "key set: True" in open(os.path.join(dest, "job.log")).read(),
               f"RC39 a poll saves the job's outputs and log locally: {s1['fetched']}")
            ok(sync_outputs(run, "j1", dest)["fetched"] == [], "RC40 a second poll with nothing new fetches nothing")
            for bad in ("work/job.py", "state.json", "../../etc/passwd", "out/../state.json"):
                try:
                    ws.job_chunk("j1", bad, 0, 10)
                    ok(False, f"RC41 job_chunk refuses {bad}")
                except (ValueError, OSError):
                    ok(True, f"RC41 job_chunk refuses {bad}")
            st2 = ws.job_task("j2", None, [sys.executable, "-c", "import time; time.sleep(30)"], None, 1 / 3600,
                              heartbeat_s=1)
            ok(st2["state"] == "timed_out", f"RC42 a job past max_hours is stopped: {st2['state']}")
            st3 = ws.job_task("j3", None, ["--no-such-program-x"], None, 1.0)
            ok(st3["state"] == "failed" and "FileNotFoundError" in st3.get("error", "")
               and ws.job_state("j3")["state"]["state"] == "failed",
               f"RC44 a job that cannot start is recorded as failed, not left 'running': {st3.get('error')}")
            tok = ws.job_queue("j4")
            ok(tok and ws.job_view(ws._read_json(os.path.join(ws.JOBS, "j4", "state.json")), time.time() + 60)
               ["state"] == "queued" and "token" not in ws.job_state("j4")["state"]
               and ws.job_queue("j4") is None
               and ws.job_task("j4", None, "true", None, 1.0, token="other")["state"] == "refused"
               and ws.job_task("j4", None, "true", None, 1.0)["state"] == "refused"
               and ws.job_task("j4", None, "true", None, 1.0, token=tok)["state"] == "done",
               "RC45 a submitted job reads as queued until its own task claims it; a second submission is refused")
            ok(ws.job_view({"state": "queued", "queued": 0.0}, 99999.0)["state"] == "lost",
               "RC45c a queued job whose task never started reads as lost after an hour")
            calls = []
            orig = ws._job_write
            ws._job_write = lambda d_, st_: (calls.append(st_.get("heartbeat")), orig(d_, st_))
            try:
                logf = open(os.path.join(d, "beat.log"), "wb")
                ws._beat_run([sys.executable, "-c", "import time; time.sleep(1.2)"], logf, os.path.join(ws.JOBS, "j4"),
                             {"state": "running"}, 60, 0.3)
                logf.close()
            finally:
                ws._job_write = orig
            ok(len(calls) >= 2, f"RC45d setup commands keep the job's heartbeat up ({len(calls)} beats)")
            ws._job_write(os.path.join(ws.JOBS, "j2"), {"state": "running", "heartbeat": time.time()})
            ok(not ws.job_queue("j2"), "RC45b a running job id is not queued again")
            ok(ws.job_task("j2", None, "true", None, 1.0)["state"] == "refused",
               "RC43 a job id that is running is not restarted")
        finally:
            ws.JOBS = old
    real_gate = globals().get("_budget_gate")
    globals()["_budget_gate"] = lambda a, name, meter=False, reserve=True: None   # cases before RN28 predate it
    _self_test_sched(ok)
    _self_test_batch(ok)
    _self_test_run(ok, real_gate)
    _self_test_pipeline(ok)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_pipeline(ok) -> None:
    """VP (2026-11-05): a main.py job must state its pipeline (the default changed once, silently),
    the run learns its launcher, and the job leaves a launch record in the grader's format
    (assignments.json + result.json, as cloud/run_cluster.py writes them), so a vm_job eval run
    is graded with its input hashes checked instead of --unverified-inputs."""
    import inspect
    import tempfile
    g = globals()
    jp = g.get("job_pipeline")
    ok(jp is not None, "VP1 vm_job reads the pipeline of a main.py command")
    if jp is None:
        return
    for cmd in (["python3", "main.py", "Q?", "--max-rounds", "3"], "python3 main.py 'Q?' && cp -r runs $JOB_OUT/"):
        try:
            jp(cmd)
            ok(False, f"VP1 a main.py command relying on the default pipeline is refused: {cmd}")
        except SystemExit as e:
            ok("--v1 or --v2" in str(e), f"VP1 a main.py command relying on the default pipeline is refused: {e}")
    ok(jp(["python3", "main.py", "Q?", "--v2"]) == "v2" and jp("bash toolkit/x/validate_once.sh 16") is None
       and jp(["python3 main.py 'Q?' --v1"]) == "v1",
       "VP2 an explicit main.py command gives its pipeline (argv, or one shell string); other commands have none")
    src = inspect.getsource(_run_locked)
    ok("job_pipeline(" in src and src.index("job_pipeline(") < src.index("_create("),
       "VP3 run refuses an undeclared pipeline before it creates a cluster")
    ok("job_pipeline(" in inspect.getsource(cmd_start), "VP3b start refuses it too")
    env = g["job_env"]({"ANTHROPIC_API_KEY": "k"}, "dev", "j1")
    ok(env == {"ANTHROPIC_API_KEY": "k", "MA_LAUNCHER": "vm_job:dev/j1"} and g["job_env"](None, "dev", "j1")
       == {"MA_LAUNCHER": "vm_job:dev/j1"}, f"VP4 the job's environment names its launcher for metadata.json ({env})")
    with tempfile.TemporaryDirectory() as td:
        dest = os.path.join(td, "dev", "j1")
        code = io.BytesIO()
        with tarfile.open(fileobj=code, mode="w:gz") as t:
            for n, b in (("data/evals/q/a.csv", b"1,2\n"), ("main.py", b"x")):
                ti = tarfile.TarInfo(n)
                ti.size = len(b)
                t.addfile(ti, io.BytesIO(b))
        rec = g["write_launch_record"](dest, "j1", code.getvalue(), "v2", "abc1234", "dev")
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
        try:
            import eval_suite
            run_dir = os.path.join(dest, "out", "runs", "2026-11-05_000000_q")   # where polls save $JOB_OUT/runs
            os.makedirs(run_dir)
            got = eval_suite.launch_record_for(run_dir)
            ok(got is not None and got.get("launch_id") == rec["launch_id"] and got.get("pipeline") == "v2"
               and got.get("commit") == "abc1234" and "collected_launch_id" not in got
               and got.get("eval_inputs_sha256", {}).get("data/evals/q/a.csv"),
               f"VP5 the grader finds the vm_job launch record with its input hashes ({got})")
        except ImportError as e:
            ok(False, f"VP5 the grader is importable: {e}")
        g["write_launch_record"](os.path.join(td, "dev", "j2"), "j2", code.getvalue(), None, "abc1234", "dev")
        a = json.load(open(os.path.join(td, "dev", "assignments.json")))
        ok(set(a) == {"j1", "j2"} and a["j2"]["pipeline"] == "none", f"VP6 records of two jobs share the file ({a})")
        # VP7 (Codex r1 P1): a relaunch of the same job id never adopts the earlier attempt's run.
        open(os.path.join(dest, ".manifest.json"), "w").write("{}")
        rec2 = g["write_launch_record"](dest, "j1", code.getvalue(), "v2", "abc1234", "dev")
        try:
            got2 = eval_suite.launch_record_for(os.path.join(dest, "out", "runs", "2026-11-05_000000_q"))
        except NameError:
            got2 = None
        ok(rec2["launch_id"] != rec["launch_id"] and not os.path.exists(os.path.join(dest, "out", "runs"))
           and not os.path.exists(os.path.join(dest, ".manifest.json"))
           and any(n.startswith("out.before_") for n in os.listdir(dest)),
           f"VP7 the earlier attempt's runs/ is set aside and the poll index reset ({os.listdir(dest)}, {got2})")
        # VP8 (Codex r1 P1): the commit comes from the archive that ships.
        arch = io.BytesIO()
        with tarfile.open(fileobj=arch, mode="w:gz") as t:
            for n, b in (("scripts/lib/code_commit.txt", b"0123456789abcdef0123456789abcdef01234567\n"),):
                ti = tarfile.TarInfo(n)
                ti.size = len(b)
                t.addfile(ti, io.BytesIO(b))
        ok(g["_launch_commit"](arch.getvalue(), "/nonexistent-tree", None) == "0123456",
           "VP8 the launch record's commit is the archive's, not the checkout's HEAD at submit time")
        ok(g["_launch_commit"](_tarball(None, os.path.dirname(HERE)), None, os.path.dirname(HERE)) == "unknown",
           "VP8b (Codex r2) a --dir archive (no stamp) records commit unknown, not the live checkout's HEAD")
        # VP9 (Codex r1 P2): a --dir archive (./-prefixed names) still records its input hashes.
        d2 = os.path.join(td, "srcdir")
        os.makedirs(os.path.join(d2, "data", "evals", "q"))
        open(os.path.join(d2, "data", "evals", "q", "a.csv"), "w").write("1,2\n")
        r3 = g["write_launch_record"](os.path.join(td, "dev", "j3"), "j3", _tarball(None, d2), "v2", "x", "dev")
        ok(r3["eval_inputs_sha256"].get("data/evals/q/a.csv"), f"VP9 --dir archives record input hashes ({r3['eval_inputs_sha256']})")


def _self_test_sched(ok) -> None:
    """`_client` reads the VM's worker table through worker_side's bounded reader (PR #99's
    race; no network): Client.scheduler_info() is {} while the client is not connected."""
    import types
    from unittest import mock

    class FakeClient:
        def __init__(self, infos, status="running"):
            self.infos, self.status, self.uploads = list(infos), status, 0

        def scheduler_info(self):
            return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

        def upload_file(self, *a):
            self.uploads += 1

    table = {"tls://10.0.0.7:1": {}, "tls://10.0.0.5:1": {}}

    def attach(clients):
        it = iter(clients)
        return lambda name: types.SimpleNamespace(get_client=lambda: next(it, clients[-1]))

    sleeps = []
    with mock.patch.object(time, "sleep", sleeps.append):
        c = FakeClient([{}, {"workers": table}])
        with mock.patch.object(rc, "attach", attach([c])):
            try:
                got = _client("vm1")
            except BaseException as e:  # noqa: BLE001
                got = f"{type(e).__name__}: {e}"
        ok(got == (c, "tls://10.0.0.5:1") and c.uploads == 1 and len(sleeps) == 1,
           f"VJ1 _client waits out an unpopulated identity, then uses the first worker: {got}")
        closed, fresh = FakeClient([{}], status="closed"), FakeClient([{"workers": table}])
        with mock.patch.object(rc, "attach", attach([closed, fresh])):
            try:
                got = _client("vm1")
            except BaseException as e:  # noqa: BLE001
                got = f"{type(e).__name__}: {e}"
        ok(got == (fresh, "tls://10.0.0.5:1") and fresh.uploads == 1 and not closed.uploads,
           f"VJ2 a closed client is replaced from the cluster before the upload: {got}")
        with mock.patch.object(rc, "attach", attach([FakeClient([{}])])):
            try:
                _client("vm1")
                ok(False, "VJ3 a worker table that never appears stops with a clear message")
            except SystemExit as e:
                ok("vm1" in str(e) and "worker table unavailable" in str(e) and "no worker yet" not in str(e),
                   f"VJ3 a worker table that never appears stops with a clear message, not 'no worker yet': {e}")
            except BaseException as e:  # noqa: BLE001
                ok(False, f"VJ3 a worker table that never appears stops with SystemExit, not {type(e).__name__}: {e}")
        with mock.patch.object(rc, "attach", attach([FakeClient([{"workers": {}}])])):
            try:
                _client("vm1")
                ok(False, "VJ4 a connected scheduler with no worker still says so")
            except SystemExit as e:
                ok("no worker yet" in str(e), f"VJ4 a connected scheduler with no worker still says so: {e}")


class _FakeStore:
    """results_store's interface over a local directory; `corrupt` changes what a download returns."""

    def __init__(self, root, corrupt=False):
        self.root, self.corrupt, self.uploads = root, corrupt, []

    def is_sensitive_path(self, rel):
        return rel.split("/")[0].startswith("_truth") or rel.endswith(".env")

    def upload_retry(self, path, blob, account=None):
        self.uploads.append(blob)
        dest = os.path.join(self.root, blob)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(path, dest)
        return {"blob": blob}

    def download(self, blob, path, account=None):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        shutil.copy(os.path.join(self.root, blob), path)
        if self.corrupt:
            open(path, "a").write("x")
        return {"blob": blob}


def _self_test_batch(ok) -> None:
    """One cluster, one scheduler, N workers for a whole batch, stopping itself once the outputs are verified in
    storage (the Nigeria re-run used 6 one-worker clusters, each with its own scheduler, ~5 min start-up and a
    20-minute idle tail after its shard ended, shut down from a laptop watcher). No network: fake coiled, store and
    shutdown."""
    import tempfile
    import types
    from unittest import mock

    def case(label, fn):
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 -- a missing function is a failing case, not a crash
            ok(False, f"{label} ({type(e).__name__}: {e})")

    def oc1():
        made, states = [], [["stopping"], ["stopping"], ["stopped"]]

        class FakeCluster:
            def __init__(self, **kw):
                made.append(kw)

            def get_client(self):
                return types.SimpleNamespace(wait_for_workers=lambda n, timeout=None: made.append(("wait", n)))
        fake = types.SimpleNamespace(Cluster=FakeCluster)
        a = types.SimpleNamespace(name="b1", cpu=8, memory="32 GiB", idle_timeout="10 minutes", workers=6)
        with mock.patch.dict(sys.modules, {"coiled": fake}), mock.patch.object(time, "sleep", lambda s: None), \
                mock.patch.object(rc, "cluster_states", lambda n: states.pop(0) if len(states) > 1 else states[0]):
            cmd_up(a)
        kw = made[0] if made and isinstance(made[0], dict) else {}
        ok(len([m for m in made if isinstance(m, dict)]) == 1 and kw.get("n_workers") == 6
           and kw.get("scheduler_cpu") == 2 and kw.get("worker_cpu") == 8 and kw.get("shutdown_on_close") is False
           and kw.get("workspace") == rc.WORKSPACE and ("wait", 1) in made and not states[:-1],
           f"OC1 `up --workers 6` makes one cluster (one 2-vCPU scheduler, 6 workers) only after a stopping one has "
           f"stopped, and waits for the first worker: {kw}")
    case("OC1", oc1)

    def oc1b():
        ap_err = []
        with mock.patch("argparse.ArgumentParser.error", lambda self, m: (ap_err.append(m), (_ for _ in ()).throw(
                SystemExit(2)))):
            for argv in (["start", "b1", "j", "--cap-usd", "5", "--shutdown-when-stored", "--", "true"],
                         ["start", "b1", "j", "--cap-usd", "5", "--cluster-compute", "--", "true"]):
                try:
                    main(argv)
                except SystemExit:
                    pass
        ok(len(ap_err) == 2 and "--store" in ap_err[0] and "--tree" in ap_err[1],
           f"OC1b --shutdown-when-stored needs --store, and --cluster-compute needs the shipped tree: {ap_err}")
    case("OC1b", oc1b)

    with tempfile.TemporaryDirectory() as d:
        old = ws.JOBS
        ws.JOBS = os.path.join(d, "vm")
        downs = []
        job = ("import os\nopen(os.path.join(os.environ['JOB_OUT'], 'shard.jsonl'), 'w').write('{}\\n')\n"
               "os.makedirs(os.path.join(os.environ['JOB_OUT'], 'sub'))\n"
               "open(os.path.join(os.environ['JOB_OUT'], 'sub', 'b.json'), 'w').write('[]')\n"
               "os.makedirs(os.path.join(os.environ['JOB_OUT'], '_truth'))\n"
               "open(os.path.join(os.environ['JOB_OUT'], '_truth', 'key.csv'), 'w').write('secret')\n"
               "print('sched', os.environ.get('MA_COMPUTE_SCHEDULER'))\n"
               "raise SystemExit(int(os.environ.get('RC', '0')))\n")
        code = io.BytesIO()
        with tarfile.open(fileobj=code, mode="w:gz") as t:
            ti = tarfile.TarInfo("job.py")
            ti.size = len(job)
            t.addfile(ti, io.BytesIO(job.encode()))
        code = code.getvalue()
        try:
            with mock.patch.object(ws, "_cluster_env", lambda work: {"MA_COMPUTE_SCHEDULER": "tls://sched:8786"},
                                   create=True), \
                    mock.patch.object(ws, "_shutdown_cluster", lambda: downs.append(1), create=True):
                def oc2():
                    fs = _FakeStore(os.path.join(d, "blob1"))
                    st = ws.job_task("b1", code, [sys.executable, "job.py"], None, 1.0, cluster_compute=True,
                                     store={"prefix": "outputs/t/b1", "account": "acct"}, shutdown_when_stored=True,
                                     store_mod=fs)
                    log = open(os.path.join(ws.JOBS, "b1", "job.log")).read()
                    ok("sched tls://sched:8786" in log,
                       f"OC2 a batch job's run_map reaches this cluster's scheduler (MA_COMPUTE_SCHEDULER): {log[-80:]}")
                    sto = st.get("stored") or {}
                    ok(st["state"] == "done" and sto.get("verified") is True
                       and sorted(fs.uploads) == ["outputs/t/b1/shard.jsonl", "outputs/t/b1/sub/b.json"]
                       and sto.get("skipped") == ["_truth/key.csv"] and len(downs) == 1 and st.get("shutdown") is True,
                       f"OC3 when the job is done and every output (no sensitive file) is read back from storage with "
                       f"the same sha256, the job shuts its cluster down itself: {sto}, shutdowns {len(downs)}")
                    ok(ws.job_state("b1")["state"].get("stored", {}).get("verified") is True,
                       "OC3b the verification is recorded in the job's state")
                case("OC2", oc2)

                def oc4():
                    downs.clear()
                    fs = _FakeStore(os.path.join(d, "blob2"), corrupt=True)
                    st = ws.job_task("b2", code, [sys.executable, "job.py"], None, 1.0, store={"prefix": "outputs/t/b2"},
                                     shutdown_when_stored=True, store_mod=fs)
                    ok(st["state"] == "done" and (st.get("stored") or {}).get("verified") is False and not downs
                       and not st.get("shutdown") and "sha256" in str((st.get("stored") or {}).get("why")),
                       f"OC4 a stored copy that differs is not verified, and the cluster is left to its idle timeout "
                       f"for collection: {st.get('stored')}")
                case("OC4", oc4)

                def oc5():
                    downs.clear()
                    fs = _FakeStore(os.path.join(d, "blob3"))
                    with mock.patch.dict(os.environ, {"RC": "3"}):
                        st = ws.job_task("b3", code, [sys.executable, "job.py"], None, 1.0,
                                         store={"prefix": "outputs/t/b3"}, shutdown_when_stored=True, store_mod=fs)
                    ok(st["state"] == "failed" and not downs and fs.uploads,
                       f"OC5 a failed job stores what it wrote but never shuts the cluster down: {st['state']}, "
                       f"{len(fs.uploads)} uploads, shutdowns {len(downs)}")
                case("OC5", oc5)

                def oc6():
                    downs.clear()
                    st = ws.job_task("b4", code, [sys.executable, "job.py"], None, 1.0,
                                     store={"prefix": "outputs/t/b4"}, shutdown_when_stored=True)
                    ok(st["state"] == "done" and (st.get("stored") or {}).get("verified") is False and not downs
                       and "results_store" in str((st.get("stored") or {}).get("why")),
                       f"OC6 storage requested without the shipped tree's results_store fails closed (no shutdown): "
                       f"{st.get('stored')}")
                case("OC6", oc6)
        finally:
            ws.JOBS = old


def _self_test_run(ok, real_gate=None) -> None:
    """RN: `run` brings the cluster up, submits at once, waits with status lines, then stops it. On 2026-11-03 an
    agent ran `up`, ended its turn to wait, never sent `start`, and the VM idled out after 20 minutes with no job:
    about two hours lost. No network: a fake coiled module (Cluster, list_clusters, delete_cluster), fake client and
    store; vm_job's own guard, client and stop code run unmocked, and the job runs for real (worker_side.job_task)
    in a temporary JOBS directory. Any coiled.Cluster call after the one creation counts as an attach by name."""
    import contextlib
    import tempfile
    import types
    from unittest import mock

    def case(label, fn):
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 -- a missing function is a failing case, not a crash
            ok(False, f"{label} ({type(e).__name__}: {e})")

    real_sleep = time.sleep
    LIVE = ("pending", "starting", "running", "ready")

    def world(d, jid, job_rc=0, pre=None, corrupt=False, wait_fails=False, ctor_fail=None, interrupt_at=None,
              drop_at=None, job_stop="deferred", extra=(), store=True, prog="job.py", threaded=False, job_sleep=0,
              appear=None, after_submit=None, ctor_side=None, then=(), pre_names=None, gate=None, job_src=None):
        """Run `vm_job.py run` against a fake Coiled; returns (W, exit message or None, stdout, exception).
        `threaded`: the job runs in its own thread from submission, concurrently with `run`'s polls, and its own
        stop is real (time scaled down 600x). `appear`: (n, id) another launcher's cluster of the same name
        appears after n cluster listings."""
        W = {"clusters": dict(pre or {}), "next": 100, "events": [], "ctor": [], "pending": None, "submit_kw": None,
             "deferred": [], "polls": 0, "list_fail": 0, "stops": 0, "list_calls": 0, "thread": None,
             "table": ["tls://w1"], "job_worker": None, "polled": [], "names": dict(pre_names or {}), "then": []}
        ev = W["events"]
        src = os.path.join(d, f"src_{jid}")
        os.makedirs(src, exist_ok=True)
        open(os.path.join(src, "job.py"), "w").write(job_src or (
            f"import os, time\ntime.sleep({job_sleep})\n"
            "open(os.path.join(os.environ['JOB_OUT'], 'result.txt'), 'w').write('42')\n"
            f"print('job ran', flush=True)\nraise SystemExit({job_rc})\n"))
        fs = _FakeStore(os.path.join(d, f"blob_{jid}"), corrupt=corrupt)

        def stop(cid, who):
            if W["clusters"].get(cid) in LIVE:
                W["clusters"][cid] = "stopped"
                W["stops"] += 1
                ev.append((who, cid))
            else:
                ev.append((who + "_noop", cid))

        def list_clusters(workspace=None):
            W["list_calls"] += 1
            if appear and W["list_calls"] > appear[0] and appear[1] not in W["clusters"]:
                W["clusters"][appear[1]] = "running"
            if W["list_fail"] > 0:
                W["list_fail"] -= 1
                raise RuntimeError("listing failed")
            return [{"id": i, "name": W["names"].get(i, "vm1"), "current_state": {"state": s}}
                    for i, s in W["clusters"].items()]

        def delete_cluster(name=None, cluster_id=None, workspace=None, pause=False):
            if name is not None:
                ev.append("delete_by_name")
            stop(cluster_id, "delete")

        class FC:
            status = "running"

            def __init__(self, cid):
                self.cid = cid

            def scheduler_info(self):
                return {"workers": {w: {} for w in W["table"]}}

            def upload_file(self, *a):
                pass

            def wait_for_workers(self, n, timeout=None):
                ev.append("wait_workers")
                if wait_fails:
                    raise TimeoutError("no worker within 1500 s")

            def run(self, fn, *a, workers=None, wait=True, on_error="raise"):
                if fn is ws.job_state:
                    W["polls"] += 1
                    W["polled"].append(workers[0])
                    if workers[0] != W["job_worker"]:     # another VM's disk has no record of this job
                        return {workers[0]: {"state": {"state": "missing"}, "files": {}, "log_tail": ""}}
                    if W["polls"] == interrupt_at:
                        raise KeyboardInterrupt
                    if W["polls"] == drop_at:
                        W["list_fail"] = 2     # the gone-check's listing fails too
                        raise RuntimeError("connection dropped")
                r = fn(*a)
                if fn is ws.job_state:
                    ev.append("poll")
                    if W["pending"]:      # the task starts after the first poll, so that poll reads 'queued'
                        task, W["pending"] = W["pending"], None
                        task()
                    elif job_stop == "after_read" and r["state"].get("state") in DONE and W["deferred"]:
                        W["deferred"].pop(0)()    # the grace ran out just after this poll read the final state
                return {workers[0]: r}

            def submit(self, fn, *args, **kw):
                ev.append("submit")
                W["submit_kw"] = dict(kw)
                W["job_worker"] = kw["workers"][0]
                if after_submit == "joins":       # a worker that sorts first joins after submission
                    W["table"] = ["tls://w0", "tls://w1"]
                elif after_submit == "leaves":    # the job's worker leaves; another remains
                    W["table"] = ["tls://w0"]
                elif after_submit == "empty":     # the job's (only) worker leaves
                    W["table"] = []
                jkw = {k: v for k, v in kw.items() if k not in ("workers", "allow_other_workers", "pure", "key")}
                task = lambda: fn(*args, store_mod=fs, **jkw)  # noqa: E731
                if threaded:
                    import threading
                    W["thread"] = threading.Thread(target=task, daemon=True)
                    W["thread"].start()
                else:
                    W["pending"] = task
                return object()

        class FakeCluster:
            def __init__(self, **kw):
                W["ctor"].append(kw)
                cid = W["next"]
                W["next"] += 1
                W["clusters"][cid] = "pending"     # allocated before the constructor returns
                W["names"][cid] = kw.get("name")
                if ctor_side is not None:          # another launcher allocates meanwhile
                    W["clusters"][ctor_side[0]], W["names"][ctor_side[0]] = "running", ctor_side[1]
                if ctor_fail is not None:
                    raise ctor_fail
                W["clusters"][cid] = "running"
                self.cluster_id = cid
                ev.append("create")

            def get_client(self):
                if W["clusters"].get(self.cluster_id) not in LIVE:
                    raise RuntimeError("cluster is gone")
                return FC(self.cluster_id)

            def shutdown(self):
                stop(self.cluster_id, "cl_shutdown")

        def attach_by_id(name, workspace, cid):     # ws.attach_existing: refuses any cluster but the checked id
            ev.append(("attach_id", cid))
            if W["clusters"].get(cid) not in LIVE:
                raise SystemExit(f"cluster {cid} is not live")
            cl = FakeCluster.__new__(FakeCluster)
            cl.cluster_id = cid
            return cl

        def job_down():
            ev.append("job_shutdown_request")
            cid = max(W["clusters"])
            if job_stop == "now" or threaded:     # the job stops the cluster now (its grace has run)
                stop(cid, "job_shutdown")
            else:
                W["deferred"].append(lambda: stop(cid, "job_shutdown"))

        def no_attach(name):
            ev.append(("attach", name))
            raise SystemExit("rc.attach called")

        def sleep(s):
            ev.append(("sleep", s))
            if sum(1 for e in ev if isinstance(e, tuple) and e[0] == "sleep") > 400:
                raise RuntimeError("test: `run` is still polling after 400 sleeps (it would wait to its deadline)")
            real_sleep(s / 600 if threaded else min(s, 0.01))

        argv = ["run", "vm1", jid, "--dir", src, "--out", os.path.join(d, "local"), "--every", "60", "--cap-usd", "5",
                *extra]
        if store:
            argv += ["--store", f"outputs/t/{jid}"]
        argv += ["--", sys.executable, prog]
        out, msg, exc = io.StringIO(), None, None
        fake_coiled = types.SimpleNamespace(Cluster=FakeCluster, list_clusters=list_clusters,
                                            delete_cluster=delete_cluster)
        fake_dist = types.SimpleNamespace(fire_and_forget=lambda f: None)
        with mock.patch.dict(sys.modules, {"coiled": fake_coiled, "distributed": fake_dist}), \
                mock.patch.object(rc, "attach", no_attach), \
                mock.patch.object(ws, "_shutdown_cluster", job_down), \
                mock.patch.object(ws, "attach_existing", attach_by_id), \
                mock.patch.object(time, "sleep", sleep), contextlib.redirect_stdout(out), \
                mock.patch.dict(globals(), {"_budget_gate": gate} if gate else {}), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                main(argv)
            except SystemExit as e:
                msg = str(e.code) if e.code not in (None, 0) else None
            except BaseException as e:  # noqa: BLE001 -- an interrupt is the case under test
                exc = e
            if W["thread"] is not None:
                W["thread"].join(20)
            for extra_argv in then:     # later commands against the same fake (status, wait, down)
                try:
                    main(list(extra_argv))
                    W["then"].append(None)
                except BaseException as e:  # noqa: BLE001
                    W["then"].append(e)
            W["after"] = lambda: [f() for f in W["deferred"]]
        return W, msg, out.getvalue(), exc

    def clean(W):
        ev = W["events"]
        return (len(W["ctor"]) <= 1 and "delete_by_name" not in ev
                and not any(isinstance(e, tuple) and e[0] == "attach" for e in ev))

    with tempfile.TemporaryDirectory() as d:
        old = ws.JOBS
        ws.JOBS = os.path.join(d, "vm")
        try:
            def rn1():
                W, msg, out, exc = world(d, "r1")
                ev = W["events"]
                first = ev[:ev.index("submit") + 1] if "submit" in ev else ev
                ok(len(W["ctor"]) == 1 and first[:1] == ["create"] and "submit" in first
                   and not [e for e in first if isinstance(e, tuple)] and W["ctor"][0].get("shutdown_on_close") is False,
                   f"RN1 `run` submits the job right after the cluster is up, with no wait in between: {first}")
                polls = [i for i, e in enumerate(ev) if e == "poll"]
                ok(msg is None and exc is None and len(polls) >= 2 and ("sleep", 60.0) in ev[polls[0]:polls[1]]
                   and ("delete", 100) in ev and W["clusters"][100] == "stopped" and W["stops"] == 1 and clean(W)
                   and "queued" in out and "done" in out
                   and os.path.exists(os.path.join(d, "local", "vm1", "r1", "out", "result.txt")),
                   f"RN2 `run` waits with a status line per poll, saves the outputs, and stops the cluster by id "
                   f"(no attach by name): msg={msg} exc={exc!r} events={ev}")
                try:
                    W["after"]()
                    second = None
                except BaseException as e:  # noqa: BLE001
                    second = e
                ok(second is None and W["stops"] == 1 and ("job_shutdown_noop", 100) in ev and clean(W),
                   f"RN2b the job's own shutdown arriving after `run` stopped the cluster is harmless: {second!r}")
                kw = W["submit_kw"] or {}
                grace = kw.get("shutdown_grace_s") or 0
                ok(kw.get("shutdown_always") is True and kw.get("shutdown_when_stored") is True and grace >= 60,
                   f"RN3 under `run` the job always stops its own cluster after a grace of at least one poll "
                   f"interval, whatever the outcome: {({k: kw.get(k) for k in ('shutdown_always', 'shutdown_grace_s')})}")
            case("RN1-3", rn1)

            def rn4():
                W, msg, out, exc = world(d, "r4", job_rc=3)
                ev = W["events"]
                ok(msg is not None and "failed" in msg and "rc=3" in msg and ("delete", 100) in ev and clean(W)
                   and max(i for i, e in enumerate(ev) if e == "poll") < ev.index(("delete", 100))
                   and os.path.exists(os.path.join(d, "local", "vm1", "r4", "job.log")),
                   f"RN4 a failed job: `run` exits non-zero with the reason, after saving the log, and stops the "
                   f"cluster: msg={msg} events={ev}")
            case("RN4", rn4)

            def rn5():
                W, msg, out, exc = world(d, "r5", corrupt=True)
                ok(msg is not None and "verified" in msg and ("delete", 100) in W["events"] and clean(W),
                   f"RN5 a done job whose stored copy differs is a failure (non-zero) and the cluster is stopped: {msg}")
            case("RN5", rn5)

            def rn6():
                W, msg, out, exc = world(d, "r6", job_rc=3, extra=("--keep-on-failure",))
                kw = W["submit_kw"] or {}
                ok(msg is not None and "rc=3" in msg and W["stops"] == 0 and W["clusters"].get(100) == "running"
                   and "down" in out and kw.get("shutdown_always") is False and kw.get("shutdown_when_stored") is True,
                   f"RN6 --keep-on-failure keeps the cluster up for debugging (the job stops it only on success) and "
                   f"says how to stop it: {msg} {kw.get('shutdown_always')}")
            case("RN6", rn6)

            def rn7():
                W, msg, out, exc = world(d, "r7", pre={7: "running"})
                ok(msg is not None and not W["ctor"] and "submit" not in W["events"] and W["stops"] == 0
                   and W["clusters"][7] == "running" and "vm1" in msg and clean(W),
                   f"RN7 a cluster of that name already running is neither reused nor stopped (no second launch): "
                   f"{msg} {W['events']}")
            case("RN7", rn7)

            def rn8():
                W, msg, out, exc = world(d, "r8", wait_fails=True)
                ok(msg is not None and len(W["ctor"]) == 1 and "submit" not in W["events"]
                   and W["clusters"][100] == "stopped" and clean(W),
                   f"RN8 a cluster whose first worker never arrives is stopped and `run` exits non-zero: {msg}")
            case("RN8", rn8)

            def rn9():
                W, msg, out, exc = world(d, "r9", job_stop="now")
                ok(msg is not None and "final state" in msg and clean(W),
                   f"RN9 a cluster that stops before `run` reads the job's final state is reported, not taken as "
                   f"success, and never attached by name or re-created: {msg} {W['events'][-4:]}")
            case("RN9", rn9)

            def rn11():
                W, msg, out, exc = world(d, "r11", interrupt_at=2)
                ok(isinstance(exc, KeyboardInterrupt) and ("delete", 100) in W["events"] and clean(W),
                   f"RN11 Ctrl-C while polling stops the cluster: exc={exc!r} events={W['events'][-3:]}")
                W, msg, out, exc = world(d, "r11k", interrupt_at=2, extra=("--keep-on-failure",))
                ok(isinstance(exc, KeyboardInterrupt) and W["stops"] == 0 and W["clusters"][100] == "running",
                   f"RN11b Ctrl-C with --keep-on-failure keeps it: {exc!r} stops={W['stops']}")
            case("RN11", rn11)

            def rn12():
                W, msg, out, exc = world(d, "r12", drop_at=2)
                ok(msg is None and exc is None and ("delete", 100) in W["events"] and clean(W) and "retrying" in out,
                   f"RN12 a dropped connection and a failing cluster listing while polling are retried, not raised; "
                   f"the job finishes and the cluster is stopped: msg={msg} exc={exc!r}")
            case("RN12", rn12)

            def rn13():
                for label, err in (("RuntimeError", RuntimeError("allocation failed")), ("Ctrl-C", KeyboardInterrupt())):
                    W, msg, out, exc = world(d, f"r13{label[0]}", pre={7: "stopped"}, ctor_fail=err)
                    ok((msg is not None or isinstance(exc, KeyboardInterrupt)) and ("delete", 100) in W["events"]
                       and W["clusters"][7] == "stopped" and ("delete", 7) not in W["events"] and clean(W),
                       f"RN13 {label} inside cluster construction: the allocated cluster is found by id and stopped; "
                       f"the earlier stopped one is not touched: {msg} {exc!r} {W['events']}")
            case("RN13", rn13)

            def rn15():
                W, msg, out, exc = world(d, "r15", job_stop="after_read")
                ok(msg is None and exc is None and W["stops"] == 1 and ("job_shutdown", 100) in W["events"]
                   and clean(W),
                   f"RN15 the job stops the cluster just after `run` read the final state: `run`'s own stop is "
                   f"harmless and it exits 0: msg={msg} exc={exc!r} {W['events'][-3:]}")
            case("RN15", rn15)

            def rn16():
                W, msg, out, exc = world(d, "../bad")
                ok(msg is not None and not W["ctor"],
                   f"RN16 an invalid job id is refused before any cluster is created: {msg} ctor={len(W['ctor'])}")
                W, msg, out, exc = world(d, "r16", store=False)
                ok(msg is not None and not W["ctor"],
                   f"RN17 `run` without --store is refused before any cluster is created (no unverified success): "
                   f"{msg}")
            case("RN16-17", rn16)

            def rn14():
                W, msg, out, exc = world(d, "r14k", extra=("--keep-on-failure",))
                kw_keep = dict(W["submit_kw"] or {})
                W, msg, out, exc = world(d, "r14", job_rc=3)
                kw = dict(W["submit_kw"] or {})
                code = _tarball(None, os.path.join(d, "src_r14"))
                jkw = {k: v for k, v in kw.items() if k not in ("workers", "allow_other_workers", "pure", "key",
                                                                 "token")}
                grace = kw.get("shutdown_grace_s") or 0
                rows = []
                for label, cmd, kws, corrupt, want in (
                        ("failed", [sys.executable, "job.py"], jkw, False, 1),
                        ("cannot start", ["--no-such-program-x"], jkw, False, 1),
                        ("unverified", [sys.executable, "-c", "import os; open(os.path.join(os.environ['JOB_OUT'],"
                                        "'a'), 'w').write('1')"], jkw, True, 1),
                        ("done", [sys.executable, "-c", "import os; open(os.path.join(os.environ['JOB_OUT'],"
                                  "'a'), 'w').write('1')"], jkw, False, 1),
                        ("failed, --keep-on-failure", [sys.executable, "job.py"],
                         {k: v for k, v in kw_keep.items() if k in jkw}, False, 0)):
                    downs, sleeps = [], []
                    jid = "lap" + str(len(rows))
                    with mock.patch.object(ws, "_shutdown_cluster", lambda: downs.append(len(sleeps))), \
                            mock.patch.object(time, "sleep", lambda s: (sleeps.append(s), real_sleep(min(s, 0.01)))):
                        ws.job_task(jid, code, cmd, None, **dict(kws, store_mod=_FakeStore(
                            os.path.join(d, "blob_" + jid), corrupt=corrupt)))
                    good = len(downs) == want and (not want or (grace and grace in sleeps[:downs[0]]))
                    rows.append((label, len(downs), good))
                ok(all(g for _, _, g in rows),
                   f"RN14 with the laptop gone the job stops its own cluster after the grace period for every outcome "
                   f"(and keeps it only for a failure under --keep-on-failure): {rows}")
                calls = []

                def gone():
                    calls.append(1)
                    raise RuntimeError("scheduler already shut down")
                with mock.patch.dict(sys.modules, {"distributed": types.SimpleNamespace(get_client=gone)}):
                    try:
                        ws._shutdown_cluster()
                        err = None
                    except BaseException as e:  # noqa: BLE001
                        err = e
                ok(calls and err is None,
                   f"RN14b the job's shutdown request on a cluster that is already stopping is harmless: {err!r}")
            case("RN14", rn14)

            def rn18():
                class SlowStore(_FakeStore):
                    """An upload that takes longer than the job's heartbeat interval; records the state file's
                    heartbeat age seen during it."""
                    ages = []

                    def upload_retry(self, path, blob, account=None):
                        t_end = time.time() + 2.0
                        while time.time() < t_end:
                            raw = ws._read_json(os.path.join(ws.JOBS, "slow", "state.json")) or {}
                            self.ages.append(time.time() - float(raw.get("heartbeat") or 0))
                            real_sleep(0.05)
                        return super().upload_retry(path, blob, account)
                fs = SlowStore(os.path.join(d, "blob_slow"))
                st = ws.job_task("slow", None, [sys.executable, "-c", "import os; open(os.path.join(os.environ["
                                 "'JOB_OUT'], 'a'), 'w').write('1')"], None, 1.0, heartbeat_s=0.2,
                                 store={"prefix": "outputs/t/slow"}, store_mod=fs)
                worst = max(SlowStore.ages or [99.0])
                ok(st["state"] == "done" and worst < 1.0,
                   f"RN18 the job keeps its heartbeat up while it uploads and verifies its outputs, so a long upload "
                   f"never reads as 'lost' (worst heartbeat age {worst:.2f} s with a 0.2 s interval)")
            case("RN18", rn18)

            def rn19():
                W, msg, out, exc = world(d, "r19", appear=(1, 8))
                ok(W["clusters"].get(8) == "running" and ("delete", 8) not in W["events"] and clean(W),
                   f"RN19 another launcher's cluster of the same name that appears while `run` starts is never "
                   f"stopped by `run`'s cleanup: msg={msg} events={W['events'][-3:]}")
            case("RN19", rn19)

            def rn20():
                W, msg, out, exc = world(d, "r20", threaded=True, job_sleep=0.3)
                ok(msg is None and exc is None and W["stops"] == 1 and W["clusters"][100] == "stopped"
                   and ("job_shutdown_noop", 100) in W["events"] and clean(W)
                   and not (W["thread"] and W["thread"].is_alive()),
                   f"RN20 with the job running concurrently in its own thread, `run` reads its final state within "
                   f"the job's grace period and stops the cluster; the job's later stop is a no-op: msg={msg} "
                   f"exc={exc!r} stops={W['stops']} events={[e for e in W['events'] if not isinstance(e, tuple) or e[0] != 'sleep'][-5:]}")
            case("RN20", rn20)

            def rn21():
                W, msg, out, exc = world(d, "r21", after_submit="joins")
                ok(msg is None and exc is None and set(W["polled"]) == {"tls://w1"} and ("delete", 100) in W["events"],
                   f"RN21 with several workers, every poll goes to the job's own worker, not the first in the table "
                   f"(a worker that joined later reads 'missing'): msg={msg} polled={W['polled']}")
            case("RN21", rn21)

            def rn22():
                W, msg, out, exc = world(d, "r22", after_submit="leaves")
                ok(msg is not None and "lost" in msg and ("delete", 100) in W["events"]
                   and "tls://w0" not in W["polled"],
                   f"RN22 the job's worker leaving the cluster reads as lost (non-zero, cluster stopped), never as "
                   f"another worker's 'missing': msg={msg} polled={W['polled']}")
            case("RN22", rn22)

            def rn23():
                import re
                for label, err, other in (("RuntimeError", RuntimeError("allocation failed"), "vm1"),
                                          ("Ctrl-C", KeyboardInterrupt(), "vm1-r23c-a1b2c3")):
                    jid = "r23" + label[0].lower()
                    W, msg, out, exc = world(d, jid, ctor_fail=err, ctor_side=(8, other))
                    mine = W["names"].get(100) or ""
                    ok(re.fullmatch(rf"vm1-{jid}-[0-9a-f]{{6}}", mine) and ("delete", 100) in W["events"]
                       and ("delete", 8) not in W["events"] and W["clusters"][8] == "running" and clean(W),
                       f"RN23 {label} in a constructor that returns no id: only the cluster with this run's unique "
                       f"name is stopped, never a concurrent launcher's {other!r}: mine={mine!r} {W['events']}")
            case("RN23", rn23)

            def rn24():
                import re
                local = os.path.join(d, "local")
                W, msg, out, exc = world(d, "r24", job_rc=3, extra=("--keep-on-failure",),
                                         then=(["status", "vm1", "r24", "--out", local],
                                               ["wait", "vm1", "r24", "--out", local, "--every", "1"],
                                               ["down", "vm1", "r24", "--out", local]))
                name = (W["ctor"][0] if W["ctor"] else {}).get("name") or ""
                rec = ws._read_json(os.path.join(local, "vm1", "r24", "job.json")) or {}
                ok(re.fullmatch(r"vm1-r24-[0-9a-f]{6}", name) and len(name) <= 63
                   and rec.get("cluster") == name and rec.get("cluster_id") == 100,
                   f"RN24 `run` names its cluster <name>-<job_id>-<6 hex> and records the name and id in job.json: "
                   f"{name!r} {({k: rec.get(k) for k in ('cluster', 'cluster_id')})}")
                attaches = [e[1] for e in W["events"] if isinstance(e, tuple) and e[0] == "attach"]
                ok(attaches and set(attaches) == {name} and ("delete", 100) in W["events"]
                   and W["clusters"][100] == "stopped",
                   f"RN24b `status`, `wait` and `down <name> <job_id>` find the run's cluster through job.json (no "
                   f"suffix typed); `down` stops it by id: attaches={attaches} then={W['then']}")
                got = run_cluster_name("V" * 40, "J_" * 32)
                ok(len(got) <= 63 and re.fullmatch(r"[a-z][a-z0-9-]*-[0-9a-f]{6}", got) is not None,
                   f"RN24c the unique name stays within 63 lowercase characters for long names and ids: {got!r}")
            case("RN24", rn24)

            def rn25():
                import fcntl
                dest = os.path.join(d, "local", "vm1", "r25")
                os.makedirs(dest, exist_ok=True)
                held = open(os.path.join(dest, ".run.lock"), "w")
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)     # another `run` of this job holds it
                try:
                    W, msg, out, exc = world(d, "r25")
                finally:
                    held.close()
                ok(msg is not None and not W["ctor"] and "r25" in msg,
                   f"RN25 a second `run` of the same job while the first holds its lock is refused before any "
                   f"cluster is created (the listing check alone races): {msg} ctor={len(W['ctor'])}")
                jid = "j" * 60
                other = run_cluster_name("vm1", jid, "abcdef")
                W, msg, out, exc = world(d, jid, pre={9: "running"}, pre_names={9: other})
                ok(msg is not None and not W["ctor"] and W["clusters"][9] == "running",
                   f"RN25b a job already running on a cluster whose long name was truncated is still found, and the "
                   f"second launch refused: {other!r} -> {msg} ctor={len(W['ctor'])}")
            case("RN25", rn25)

            def rn22b():
                W, msg, out, exc = world(d, "r22b", after_submit="empty")
                ok(msg is not None and "lost" in msg and ("delete", 100) in W["events"],
                   f"RN22b the job's only worker leaving (an empty worker table) reads as lost after three polls, "
                   f"not as retries until the deadline: msg={msg}")
            case("RN22b", rn22b)

            def rn26():
                import errno
                rows = []
                for label, cmd in (("ends", [sys.executable, "-c", "pass"]), ("cannot start", ["--no-such-program-x"])):
                    downs, orig = [], ws._job_write

                    def failing(d_, st_, orig=orig):
                        if st_.get("state") in ("done", "failed", "timed_out") or st_.get("ended"):
                            raise OSError(errno.ENOSPC, "No space left on device")
                        return orig(d_, st_)
                    jid = "wf" + str(len(rows))
                    with mock.patch.object(ws, "_job_write", failing), \
                            mock.patch.object(ws, "_shutdown_cluster", lambda: downs.append(1)):
                        try:
                            ws.job_task(jid, None, cmd, None, 1.0, shutdown_always=True, shutdown_grace_s=0)
                            raised = None
                        except OSError as e:
                            raised = e
                    rows.append((label, len(downs), type(raised).__name__ if raised else None))
                ok(all(n == 1 for _, n, _ in rows),
                   f"RN26 a final-state write that fails (disk full) still stops the cluster under shutdown_always: "
                   f"{rows}")
            case("RN26", rn26)

            def rn27():
                import errno
                downs, orig = [], ws._job_write
                tok = ws.job_queue("init1")

                def failing(d_, st_):
                    if st_.get("state") == "running":       # the first write after the claim
                        raise OSError(errno.ENOSPC, "No space left on device")
                    return orig(d_, st_)
                with mock.patch.object(ws, "_job_write", failing), \
                        mock.patch.object(ws, "_shutdown_cluster", lambda: downs.append(1)):
                    try:
                        ws.job_task("init1", None, ["true"], None, 1.0, token=tok, shutdown_always=True,
                                    shutdown_grace_s=0)
                        raised = None
                    except OSError as e:
                        raised = e
                    refused = ws.job_task("init1", None, ["true"], None, 1.0, token="not-mine", shutdown_always=True)
                ok(len(downs) == 1 and refused.get("state") == "refused",
                   f"RN27 a claimed job whose initial running-state write fails (disk full) still stops its cluster "
                   f"under shutdown_always; a refused submission stops nothing: stops={len(downs)} "
                   f"raised={type(raised).__name__ if raised else None} refused={refused.get('state')}")
            case("RN27", rn27)

            def rn28():
                import budget
                for label, scope, verdict in (("over budget", "f6", (False, "spent 96.00 + cap 5.00 > 100.00")),
                                              ("no scope", None, (True, "ok"))):
                    seen = []
                    lbo = lambda s_, c, name=None, **k: (seen.append((s_, c, name)), verdict)[1]  # noqa: E731
                    with mock.patch.object(budget, "launch_budget_ok", lbo), \
                            mock.patch.object(budget, "scope_for_name", lambda n, *a: scope):
                        W, msg, out, exc = world(d, f"r28{label[0]}", gate=real_gate)
                    uniq = f"vm1-r28{label[0]}-"
                    named = [(s_, c, len(n) == len(uniq) + 6 and n.startswith(uniq)) for s_, c, n in seen]
                    ok(msg and ("budget" in msg or "scope" in msg) and not W["ctor"] and W["list_calls"] == 0
                       and "submit" not in W["events"] and named == ([("f6", 5.0, True)] if scope else []),
                       f"RN28 {label}: `run` refuses before listing or creating any cluster: {msg} {seen}")
            case("RN28", rn28)

            def rn29():
                # RN29 (Codex r1 P2 on #189): each reading is of the job's own cluster, and the output the job wrote
                # just before its cluster crossed the cap is saved before the stop
                prog = ("import os, time\nout = os.environ['JOB_OUT']\n"
                        "open(os.path.join(out, 'progress.txt'), 'w').write('a')\ntime.sleep(0.6)\n"
                        "open(os.path.join(out, 'progress.txt'), 'w').write('b')\ntime.sleep(3)\n")
                seen = []

                def meter(cid, jid="r29a"):     # over the cap once the job has written 'b' on its VM
                    seen.append(cid)
                    if cid != 100:
                        return 1000.0          # another cluster's cost: reading it would stop the job at once
                    p = os.path.join(ws.JOBS, jid, "out", "progress.txt")
                    return 6.0 if os.path.exists(p) and open(p).read() == "b" else 1.0
                W, msg, out, exc = world(d, "r29a", gate=lambda a, name, meter=False, m=meter: m, threaded=True,
                                         extra=("--keep-on-failure",), job_src=prog)
                ev = W["events"]
                saved = os.path.join(d, "local", "vm1", "r29a", "out", "progress.txt")
                ok(msg and "cap" in msg and ("delete", 100) in ev and set(seen) == {100}
                   and os.path.exists(saved) and open(saved).read() == "b" and clean(W),
                   f"RN29 at the cap, `run` reads its own cluster's cost, saves the output written just before, then "
                   f"stops the cluster by id under --keep-on-failure: {msg} readings={sorted(set(seen))} "
                   f"saved={open(saved).read() if os.path.exists(saved) else None!r}")
                vals = [RuntimeError("api")] * 3

                def bad(cid):
                    v = vals.pop(0) if vals else 99.0
                    if isinstance(v, Exception):
                        raise v
                    return v
                W, msg, out, exc = world(d, "r29b", gate=lambda a, name, meter=False, m=bad: m, threaded=True,
                                         job_sleep=2)
                ok(msg and "cap" in msg and ("delete", 100) in W["events"] and clean(W),
                   f"RN29b three failed cost readings in a row stop the cluster (fail closed): {msg}")
                calls = []

                def late(cid):
                    calls.append(cid)
                    return 1.0 if len(calls) == 1 else 6.0
                W, msg, out, exc = world(d, "r29c", job_rc=3, gate=lambda a, name, meter=False, m=late: m,
                                         extra=("--keep-on-failure",))
                ok(msg and "rc=3" in msg and len(calls) >= 2 and ("delete", 100) in W["events"] and W["stops"] >= 1,
                   f"RN29c a job that failed past its cap between polls is still stopped under --keep-on-failure "
                   f"(the cap is read before a terminal state returns): {msg} readings={len(calls)}")
            case("RN29", rn29)

            def rn29d():
                calls = []

                def third(cid):
                    calls.append(cid)
                    return 6.0 if len(calls) >= 3 else 1.0
                W, msg, out, exc = world(d, "r29d", after_submit="empty", gate=lambda a, name, meter=False, m=third: m,
                                         extra=("--keep-on-failure",))
                ok(msg and "lost" in msg and len(calls) >= 3 and ("delete", 100) in W["events"] and W["stops"] >= 1,
                   f"RN29d the third WorkerGone (lost) reads the cost first, and over the cap the cluster is stopped "
                   f"under --keep-on-failure: {msg} readings={len(calls)}")
                calls2 = []

                def fails_last(cid):
                    calls2.append(cid)
                    if len(calls2) >= 2:
                        raise RuntimeError("api")
                    return 1.0
                W, msg, out, exc = world(d, "r29e", job_rc=3, gate=lambda a, name, meter=False, m=fails_last: m,
                                         extra=("--keep-on-failure",))
                ok(msg and len(calls2) >= 2 and ("delete", 100) in W["events"] and W["stops"] >= 1,
                   f"RN29e a terminal state whose cost cannot be read stops the cluster (no conclusive cap decision): "
                   f"{msg} readings={len(calls2)}")
            case("RN29d", rn29d)

            def rn29f():
                res = []

                def unreadable(cid):
                    raise RuntimeError("api")
                for meter in (unreadable, lambda cid: 1.0):
                    a = types.SimpleNamespace(name="vmx", job_id="j29f", cap_usd=5.0, every=60, max_hours=0.0)
                    flags = {}
                    with mock.patch.dict(globals(), {"_client": lambda *x, **k: (_ for _ in ()).throw(
                            RuntimeError("drop")), "_gone": lambda n, c: False, "_job_worker": lambda dest: "w"}), \
                            mock.patch.object(time, "sleep", lambda s: None), contextlib.redirect_stdout(io.StringIO()):
                        try:
                            st = _poll(a, d, gone_ok=True, deadline=time.time() - 1, cid=100, meter=meter, flags=flags)
                            res.append((st["state"]["state"], bool(flags.get("over_cap"))))
                        except SystemExit:
                            res.append(("deadline", bool(flags.get("over_cap"))))
                ok(res == [("over_cap", True), ("deadline", False)],
                   f"RN29f at the polling deadline an unreadable cost takes the mandatory stop path (a reading under "
                   f"the cap may end on the deadline): {res}")
            case("RN29f", rn29f)

            def rn30():
                import budget
                a = types.SimpleNamespace(cap_usd=5.0, budget_scope="f6")
                with mock.patch.object(budget, "launch_budget_ok", lambda s_, c, name=None, **k: (True, "ok")), \
                        mock.patch.object(budget, "job_meter", lambda s_: ("meter", s_)):
                    got = real_gate(a, "lw-smoke-x", meter=True)
                ok(got == ("meter", "f6"), f"RN30 a covered launch gets the scope's meter: {got}")
                made = []
                b = types.SimpleNamespace(name="vm3", cpu=4, memory="16 GiB", idle_timeout="20 minutes", workers=1,
                                          cap_usd=5.0, budget_scope="f6")
                calls = []
                lbo = lambda s_, c, name=None, reserve=True: (calls.append(reserve), (False, "over"))[1]  # noqa: E731
                for states in ([], ["running"]):
                    calls.clear(), made.clear()
                    with mock.patch.object(budget, "launch_budget_ok", lbo), \
                            mock.patch.dict(sys.modules, {"coiled": types.SimpleNamespace(
                                Cluster=lambda **k: made.append(k))}), \
                            mock.patch.dict(globals(), {"_budget_gate": real_gate}), \
                            mock.patch.object(rc, "cluster_states", lambda n, s_=states: s_), \
                            contextlib.redirect_stdout(io.StringIO()):
                        try:
                            cmd_up(b)
                            msg = None
                        except SystemExit as e:
                            msg = str(e)
                    if not states:
                        ok(msg and "over" in msg and not made and calls == [True],
                           f"RN30b `up` is refused before the constructor: {msg}")
                    else:
                        ok(msg is None and not made and calls == [],
                           f"RN30d an `up` that finds the cluster already up reserves nothing: {msg} {calls}")
                errs = []
                with mock.patch("argparse.ArgumentParser.error", lambda self, m: (errs.append(m), (_ for _ in ()).throw(
                        SystemExit(2)))), contextlib.redirect_stderr(io.StringIO()):
                    for argv in (["run", "v", "j", "--store", "p", "--", "true"], ["up", "v"]):
                        try:
                            main(argv)
                        except SystemExit:
                            pass
                ok(len(errs) == 2 and all("--cap-usd" in e for e in errs), f"RN30c run and up require --cap-usd: {errs}")
            case("RN30", rn30)

            def rn31():
                import budget
                reached = []
                a = types.SimpleNamespace(name="lw-smoke-v", job_id="j31", command=["true"], cap_usd=5.0,
                                          budget_scope=None, tree=None, dir=None, anthropic_key=False)
                with mock.patch.object(budget, "launch_budget_ok",
                                       lambda s_, c, name=None, reserve=True: (reached.append(reserve), (False, "over"))[1]), \
                        mock.patch.object(budget, "scope_for_name", lambda n, *x: "f6"), \
                        mock.patch.dict(globals(), {"_budget_gate": real_gate,
                                                    "_client": lambda *x, **k: (_ for _ in ()).throw(RuntimeError("client"))}):
                    try:
                        cmd_start(a)
                        msg = None
                    except SystemExit as e:
                        msg = str(e)
                    except RuntimeError as e:
                        msg = f"reached {e}"
                    try:
                        cmd_start(a, cluster=object())
                        msg2 = None
                    except BaseException as e:  # noqa: BLE001
                        msg2 = str(e)
                ok(msg and "over" in msg and reached == [False],
                   f"RN31 interactive `start` is refused when the budget no longer covers its cap: {msg} {reached}")
                ok(msg2 == "client" and reached == [False],
                   f"RN31b under `run` (its own cluster object) start does not check twice: {msg2}")
                errs = []
                with mock.patch("argparse.ArgumentParser.error", lambda self, m: (errs.append(m), (_ for _ in ()).throw(
                        SystemExit(2)))), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        main(["start", "v", "j", "--", "true"])
                    except SystemExit:
                        pass
                ok(errs and "--cap-usd" in errs[0], f"RN31c start requires --cap-usd: {errs}")
            case("RN31", rn31)

            def rn32():
                # RN32 (2026-11-13): a session restart killed the `run` metering cluster 2375765, which ran on with no
                # cap. `wait --cap-usd` re-attaches it: the recorded id, metered as `run` does, stopped by id.
                import budget

                def rewait(label, readings, cap="5", argv_cap=True, lbo_ok=True):
                    jid, seen, lbo_calls = f"r32{label}", [], []

                    def meter(cid):
                        seen.append(cid)
                        v = readings[min(len(seen), len(readings)) - 1]
                        if isinstance(v, Exception):
                            raise v
                        return v
                    lbo = lambda s_, c, name=None, reserve=True: (  # noqa: E731
                        lbo_calls.append((s_, round(c, 2), reserve)), (lbo_ok, "f6: ok" if lbo_ok else "f6: over"))[1]
                    wait = ["wait", "vm1", jid, "--out", os.path.join(d, "local"), "--every", "60"]
                    if argv_cap:
                        wait += ["--cap-usd", cap, "--budget-scope", "f6"]
                    with mock.patch.object(budget, "job_meter", lambda s_: meter), \
                            mock.patch.object(budget, "launch_budget_ok", lbo):
                        W, msg, out, exc = world(d, jid, interrupt_at=1, extra=("--keep-on-failure",), then=[wait])
                    ev = W["events"]
                    return W, ev, W["then"][0], seen, lbo_calls, isinstance(exc, KeyboardInterrupt)

                W, ev, res, seen, lbo_calls, killed = rewait("a", [1.0, 6.0])
                ok(killed and ("attach_id", 100) in ev and ("delete", 100) in ev and W["stops"] == 1
                   and isinstance(res, SystemExit) and "cap" in str(res.code) and set(seen) == {100}
                   and lbo_calls == [("f6", 4.0, False)] and clean(W),
                   f"RN32a after the launcher died, `wait --cap-usd` meters the recorded cluster id, checks the budget "
                   f"covers the rest of the cap, and stops the cluster by id at the cap: {res!r} {lbo_calls} {ev}")
                W, ev, res, seen, lbo_calls, killed = rewait("b", [1.0])
                ok(killed and res is None and ("attach_id", 100) in ev and ("delete", 100) in ev and W["stops"] == 1
                   and len(seen) >= 2 and clean(W),
                   f"RN32b a job that ends under a re-attached cap has its cluster stopped by id: {res!r} {ev}")
                W, ev, res, seen, lbo_calls, killed = rewait("c", [1.0], argv_cap=False)
                ok(killed and isinstance(res, SystemExit) and "rc.attach" in str(res.code) and not seen
                   and not lbo_calls and ("attach_id", 100) not in ev and W["stops"] == 0
                   and W["clusters"][100] == "running",
                   f"RN32c without --cap-usd `wait` is unchanged (its attach, no meter, no budget, no stop): {res!r}")
                W, ev, res, seen, lbo_calls, killed = rewait("d", [3.0], cap="2")
                ok(isinstance(res, SystemExit) and "already spent" in str(res.code) and not lbo_calls
                   and ("attach_id", 100) not in ev and W["stops"] == 0,
                   f"RN32d a cap at or below the cluster's spend so far is refused, nothing attached: {res!r}")
                W, ev, res, seen, lbo_calls, killed = rewait("e", [1.0], lbo_ok=False)
                ok(isinstance(res, SystemExit) and "budget" in str(res.code) and ("attach_id", 100) not in ev
                   and W["stops"] == 0, f"RN32e a budget that does not cover the rest of the cap refuses: {res!r}")
                W, ev, res, seen, lbo_calls, killed = rewait("f", [RuntimeError("api")])
                ok(isinstance(res, SystemExit) and "fail closed" in str(res.code) and len(seen) == 3
                   and ("delete", 100) in ev and W["stops"] == 1 and not lbo_calls,
                   f"RN32f a cost that cannot be read three times before attaching stops the cluster: {res!r}")
                W, ev, res, seen, lbo_calls, killed = rewait("g", [1.0, RuntimeError("api")])
                ok(isinstance(res, SystemExit) and "fail closed" in str(res.code) and ("attach_id", 100) in ev
                   and ("delete", 100) in ev and W["stops"] == 1,
                   f"RN32g three failed readings while waiting stop the cluster by id (fail closed): {res!r}")
            case("RN32", rn32)
        finally:
            ws.JOBS = old

    def rn10():
        class FakeCluster:
            def __init__(self, **kw):
                pass

            def get_client(self):
                return types.SimpleNamespace(wait_for_workers=lambda n, timeout=None: None)
        a = types.SimpleNamespace(name="vm2", cpu=4, memory="16 GiB", idle_timeout="20 minutes", workers=1)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"coiled": types.SimpleNamespace(Cluster=FakeCluster)}), \
                mock.patch.object(rc, "cluster_states", lambda n: []), contextlib.redirect_stdout(out):
            cmd_up(a)
        text = out.getvalue()
        ok("WARNING" in text and "20 minutes" in text and "`start`" in text and "`run`" in text,
           f"RN10 `up` warns that the cluster stops after its idle timeout unless `start` follows: {text!r}")
    case("RN10", rn10)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        sys.path.insert(0, HERE)
        import no_cloud
        with no_cloud.refusing():          # no cloud call from a self-test (Codex round 3 on #161)
            return _self_test()
    argv, command = split_command(argv)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    def up_args(p):
        p.add_argument("--cpu", type=int, default=4)
        p.add_argument("--memory", default="16 GiB")
        p.add_argument("--idle-timeout", default="20 minutes")
        p.add_argument("--workers", type=int, default=1, help="workers of the one cluster (one scheduler for all)")

    def start_args(p):
        p.add_argument("job_id")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--tree")
        g.add_argument("--dir")
        p.add_argument("--max-hours", type=float, default=24.0)
        p.add_argument("--setup-sdk", action="store_true")
        p.add_argument("--anthropic-key", action="store_true")
        p.add_argument("--cluster-compute", action="store_true",
                       help="the job's compute.py run_map uses every worker of this cluster")
        p.add_argument("--store", metavar="PREFIX", default=None,
                       help="when the job ends, upload $JOB_OUT to <PREFIX>/ and verify each file read back (sha256)")
        p.add_argument("--store-account", default=None, help="storage account (default: the worker's configured one)")
        p.add_argument("--shutdown-when-stored", action="store_true",
                       help="the job stops its cluster once it is done and every output is verified in storage")

    def budget_args(p):
        p.add_argument("--cap-usd", type=float, required=True, help="the most this cluster may spend (USD)")
        p.add_argument("--budget-scope", default=None, help="default: the scope whose prefix the name has")

    up = sub.add_parser("up")
    up.add_argument("name")
    up_args(up)
    budget_args(up)
    st = sub.add_parser("start")
    st.add_argument("name")
    start_args(st)
    budget_args(st)
    rn = sub.add_parser("run", help="up + start + wait + down in one command (the default for a single job)")
    rn.add_argument("name")
    start_args(rn)
    up_args(rn)
    budget_args(rn)
    rn.add_argument("--every", type=float, default=60, help="seconds between status polls")
    rn.add_argument("--keep-on-failure", action="store_true",
                    help="leave the cluster up after a failure, for debugging (it then stops after its idle timeout)")
    for nm in ("status", "wait"):
        p = sub.add_parser(nm)
        p.add_argument("name")
        p.add_argument("job_id")
        if nm == "wait":
            p.add_argument("--every", type=float, default=300)
    dn = sub.add_parser("down")
    dn.add_argument("name")
    dn.add_argument("job_id", nargs="?", default=None, help="stop this job's cluster (as recorded by `run`)")
    for p in sub.choices.values():
        p.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    a.command = command
    if a.cmd == "run" and not a.store:
        ap.error("run needs --store PREFIX: success means every output is verified in storage")
    if a.cmd in ("start", "run"):
        if a.shutdown_when_stored and not a.store:
            ap.error("--shutdown-when-stored needs --store PREFIX: the cluster stops only once outputs are verified")
        if a.cluster_compute and not (a.tree or a.dir):
            ap.error("--cluster-compute needs the shipped tree (--tree, or --dir with scripts/compute.py)")
    {"up": cmd_up, "start": cmd_start, "run": cmd_run, "status": cmd_status, "wait": cmd_wait,
     "down": cmd_down}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
