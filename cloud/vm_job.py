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
cap; since the owner's decision of 2026-11-17 (no hard dollar ceilings) a launch over the budget is admitted, with
a WARNING and a spend alert (budget.alert -> spend_alerts.jsonl). `run` and `up` reserve the cap; `up` does so only
once it will construct a cluster. Each `run` poll saves the outputs, then reads the job cluster's USD: every reading
writes <out>/<name>/<job_id>/cap_projection.json (level under_cap / over_cap / far_over_cap at
budget.CAP_ALERT_MULTIPLE x the cap, burn rate, projected spend), and each upward crossing, and three failed
readings in a row, prints a WARNING and records a spend alert. Nothing is stopped for dollars or for an unreadable
cost. The job's hard stops are its own --max-hours on the VM and the cluster's timeouts. SIGTERM ends `run` as
Ctrl-C does, so the cluster is stopped by id.

Re-attaching a cap (2026-11-13: a session restart killed the `run` that metered a job, and its cluster ran on with
no cap). `wait <name> <job_id> --cap-usd N` meters the cluster id recorded in job.json the way `run` does (records and
alerts) and stops it by id once the job has ended (the launcher that would have stopped it is gone); it never stops a
job that is still running, whatever ends the wait. The cap is on the
cluster's lifetime cost. The cluster's reservation is replaced by one of the rest of N (budget.reattach_budget_ok);
a cost already past N, or a budget the rest does not fit, is accepted with a spend alert. A budget or cost that
cannot be read is alerted, never a stop.

Why one command. On 2026-11-03 an agent ran `up`, ended its turn to wait for the cluster,
and never sent `start`: the VM idled out after 20 minutes with no job, and about two hours
were lost. Coiled counts `idle_timeout` from cluster start, not from the first task (there
is no setting for "only after work"), so an `up` with no `start` behind it is wasted.
`up`, `start`, `status`, `wait` and `down` remain for interactive use; `up` warns.

    python cloud/vm_job.py up <name> [--cpu 4] [--memory "16 GiB"] [--pin-vm-type T] [--idle-timeout "20 minutes"]
    python cloud/vm_job.py start <name> <job_id> [--tree PATH | --dir PATH] [--max-hours 24]
                                  [--setup-sdk] [--anthropic-key] -- <command ...>
    python cloud/vm_job.py status <name> <job_id>          # state + log tail; saves outputs locally
    python cloud/vm_job.py wait <name> <job_id> [--every 300]   # poll until the job ends, saving each time
    python cloud/vm_job.py wait <name> <job_id> --cap-usd N [--budget-scope S]   # re-attach a cap (below)
    python cloud/vm_job.py down <name>

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
plain directory, less .git, .venv and __pycache__), with secrets (`--anthropic-key`) in its environment only.

A command that runs main.py must state its pipeline as `--v2` (refused otherwise, before
anything is created: main.py's default changed once, 2026-11-05). `--v1` is refused: pipeline v1
is retired (owner, 2026-11-20). The job's environment names
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
import contextlib
import io
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coiled_ops as CO  # noqa: E402
import worker_side as ws  # noqa: E402
_SCRIPTS_LIB = os.path.join(os.path.dirname(HERE), "scripts", "lib")
if _SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _SCRIPTS_LIB)
import eval_paths  # noqa: E402  (scripts/lib)
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
    sp = os.path.join(dest, ".fetched_sha256.json")       # rel -> sha256 of the bytes downloaded (copy provenance)
    try:
        fetched_sha = json.load(open(sp)) if os.path.exists(sp) else {}
    except ValueError:
        fetched_sha = {}
    got = []
    for rel in files_to_fetch(st["files"], index):
        size = st["files"][rel][0]
        target = os.path.join(dest, rel)
        if not os.path.realpath(target).startswith(os.path.realpath(dest) + os.sep):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp, have = target + ".part", 0
        h = hashlib.sha256()
        with open(tmp, "wb") as f:
            while have < size:
                b = run(ws.job_chunk, job_id, rel, have, min(CHUNK, size - have))
                if not b:
                    break
                f.write(b)
                h.update(b)
                have += len(b)
        os.replace(tmp, target)
        index[rel] = st["files"][rel]
        fetched_sha[rel] = h.hexdigest()
        got.append(rel)
    # the digests first, then the markers, then the manifest last: a poll interrupted before the manifest is saved
    # fetches those files again, so no downloaded record is left unmarked (Codex round 4 of #256)
    _write_json(sp, fetched_sha)                          # atomic: an interrupted write keeps the earlier digests
    # F6 run 3: a run dir copied out of a live run carries in-flight batch records of the VM's process. Every poll marks
    # each run dir with the bytes downloaded into it (hashed as they arrived, never re-read: Codex review of #256), so
    # a record fetched before its run root is marked once the root arrives, and one a local process rewrote does not
    # match.
    import copied_run
    for rd in sorted({os.path.dirname(r) for r in index if os.path.basename(r) == "run_state.json"}):
        mine = {r[len(rd) + 1:]: v for r, v in fetched_sha.items() if r.startswith(rd + "/")}
        copied_run.mark(os.path.join(dest, rd), f"vm_job poll of job {job_id}",
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), hashes=mine)
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
        hint = "" if str(e) == pipeline_id.RETIRED_MESSAGE else " (add --v2 to main.py's arguments)"
        raise SystemExit(f"{e}{hint}: refusing") from None


def job_env(secrets: dict | None, name: str, job_id: str, account: str | None = None) -> dict:
    """The job's extra environment: its secrets, its launcher's name for main.py's metadata.json, and the
    storage account's name (never a credential) when one is known (SA1-SA6)."""
    return {**(secrets or {}), "MA_LAUNCHER": f"vm_job:{name}/{job_id}",
            **({"MA_STORAGE_ACCOUNT": account} if account else {})}


SECRET_ENV_ALLOWED = ("GH_TOKEN",)     # cloud/supervisor.py's merge train (W0-J)


def secret_env(names) -> dict:
    """--secret-env NAME: the launcher's $NAME, for the job's environment only (never job.json or state.json, like
    --anthropic-key). Only names in SECRET_ENV_ALLOWED; an unset one is refused. Values are never printed."""
    out = {}
    for n in names or ():
        if n not in SECRET_ENV_ALLOWED:
            raise SystemExit(f"--secret-env {n}: not in the allow-list {SECRET_ENV_ALLOWED}: refusing")
        if not os.environ.get(n):
            raise SystemExit(f"--secret-env {n}: ${n} is not set in the launcher's environment: refusing")
        out[n] = os.environ[n]
    return out


_ACCOUNT_NAME = re.compile(r"[a-z0-9]{3,24}")     # Azure storage account names: 3-24 lowercase letters and digits


def storage_account(flag: str | None) -> str | None:
    """The storage account name the job uses: --store-account, else the launcher's $MA_STORAGE_ACCOUNT /
    $COILED_STORAGE_ACCOUNT (results_store.configured_account's order). The job VM does not inherit the
    launcher's environment, so `start` passes the name on. Only a bare account name passes: a connection
    string, URL or key is refused (and not echoed), so no credential reaches the job this way."""
    acct = flag or os.environ.get("MA_STORAGE_ACCOUNT") or os.environ.get("COILED_STORAGE_ACCOUNT")
    if acct and not _ACCOUNT_NAME.fullmatch(acct):
        raise SystemExit("the storage account must be a bare storage account name (3-24 lowercase letters and "
                         "digits), not a connection string, URL or key: refusing")
    return acct or None


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:         # closed (and flushed) before the replace: a failed flush keeps the old file
        json.dump(obj, fh, indent=1)
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
    stamped = CO.archive_commit(code) if code else None
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


TARBALL_SKIP_ANY = {".git", ".venv", "__pycache__"}   # left out of a --dir archive at any depth
# runs/ ships (Codex on #264, P1): a --dir job may resume a staged run (`main.py --v2 --resume runs/R1`).
TARBALL_SKIP_TOP: set = set()


def _tarball_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """A --dir archive leaves out VCS data, the virtualenv and bytecode, at any depth."""
    parts = [p for p in ti.name.split("/") if p not in ("", ".")]
    if any(p in TARBALL_SKIP_ANY for p in parts) or (parts and parts[0] in TARBALL_SKIP_TOP):
        return None
    return ti


def _tarball(tree: str | None, src_dir: str | None) -> bytes | None:
    return CO.tarball_from_tree(tree, src_dir, tarball_filter_fn=_tarball_filter)


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


def _client(name, cluster=None, want=None, cid=None):
    """The VM cluster's client and its worker. The table is read with worker_side's bounded
    reader (scheduler_info() is {} while the client is not connected; a closed client is
    replaced from the cluster), so only a connected scheduler with no worker says so.
    `cluster` is the object `run` created (its clients come from that object, never from a
    coiled.Cluster(name=...) attach); otherwise the guarded attach by name. `want` is the
    job's own worker: the job's state lives on that VM's disk only, so a poll goes there
    (another worker would read 'missing'), and its absence raises WorkerGone."""
    cl = cluster if cluster is not None else ws.attach_existing(name, CO.WORKSPACE, cid) if cid is not None \
        else CO.attach(name)          # `cid`: the cluster `start` checked, and only that one (Codex r2 on the price PR)
    box = [None]

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
        act = CO.keeper_wait(CO.cluster_states(a.name))
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
    cl = coiled.Cluster(name=a.name, workspace=CO.WORKSPACE, n_workers=n, **_vm_request(a), spot_policy="on-demand",
                        idle_timeout=a.idle_timeout, shutdown_on_close=False)
    if box is not None:
        box["cl"] = cl
    # the first worker is enough to start: compute.py re-reads the worker table every 2 s and gives a worker that
    # joins later a chunk at once (Phase 45 δ)
    cl.get_client().wait_for_workers(1, timeout=1500)
    print(f"{a.name} up with {n} worker(s) requested (idle timeout {a.idle_timeout}; a running job holds it "
          "regardless)")
    return True


def _vm_request(a) -> dict:
    """The cluster's VM types, pinned from cloud/vm_prices.yaml (budget.vm_types_for): worker_vm_types for --cpu and
    --memory, scheduler_vm_types for a 2-vCPU scheduler. Coiled is given these lists, never cpu/memory, so every type
    it can pick is priced (2026-11-13: --cpu 8 with the default --memory got Standard_F8s_v2, unpriced, and budget.py
    refused it only once the cluster existed). A request no priced type matches exactly gets the closest priced types
    that satisfy it, with a warning (owner, 2026-11-13: spend controls fail soft); only a request nothing in the table
    can satisfy is refused (SystemExit), before anything is listed, reserved or created. Resolved once per command."""
    if getattr(a, "_vm", None) is None:
        import budget
        try:
            workers, note = budget.resolve_vm_types(a.cpu, a.memory)
            one = getattr(a, "pin_vm_type", None)
            if one is not None:         # one VM type, so one CPU family for the whole batch (F6 rerun prep)
                tab = budget.vm_table()
                if one not in tab:
                    raise budget.BudgetError(f"--pin-vm-type {one!r} has no price in cloud/vm_prices.yaml")
                if tab[one]["vcpu"] != int(a.cpu):
                    raise budget.BudgetError(f"--pin-vm-type {one} has {tab[one]['vcpu']} vCPUs, not --cpu {a.cpu}")
                if one not in workers:  # the priced types that satisfy --cpu and --memory
                    raise budget.BudgetError(f"--pin-vm-type {one} ({tab[one]['memory_gib']:g} GiB) does not meet "
                                             f"--memory {a.memory}: one of {', '.join(workers)}")
                workers, note = [one], None
            a._vm = {"worker_vm_types": workers, "scheduler_vm_types": budget.scheduler_vm_types()}
        except budget.BudgetError as e:
            raise SystemExit(f"{a.name}: refusing before any cluster is created: {e}") from None
        if note:
            print(f"{a.name}: {note}", flush=True)
        print(f"VM types (all priced in cloud/vm_prices.yaml): workers {', '.join(a._vm['worker_vm_types'])}; "
              f"scheduler {', '.join(a._vm['scheduler_vm_types'])}", flush=True)
    return a._vm


def _live_vm_types_ok(name):
    """`start` on a cluster made elsewhere: the id of the live cluster `start` attaches to (and only that one), after
    reading its record for VM types the budget has no price for: a live process's type and its configured fallback
    list (spec.prioritized_instance_types, the types Coiled may give it or a replacement). Cluster 2376181
    (2026-11-13, --cpu 8 without pinning) listed 16 candidate worker types and 50 scheduler types, most unpriced.
    Each finding (an unpriced type, a live process with no type yet, a missing list, a record that cannot be read) is
    a printed WARNING, never a refusal (owner, 2026-11-13: spend controls fail soft); budget.py counts an unpriced
    type at its fallback bound."""
    import budget
    cid = CO.live_id(name)
    if cid is None:
        raise SystemExit(f"cluster {name!r} is not running; refusing to create a new one")
    warn = []
    try:
        d = budget.CoiledSource().details(cid)
        procs = [d["scheduler"]] + list(d["workers"])
        types = set()
        for p in procs:
            if isinstance(p, dict) and ws.state_of(p) in budget.ENDED:
                continue
            inst = p.get("instance") if isinstance(p, dict) else None
            vm = inst.get("instance_type_id") if isinstance(inst, dict) else None
            listed = ((p.get("spec") or {}).get("prioritized_instance_types") if isinstance(p, dict) else None)
            if not vm or not isinstance(listed, list) or not listed:
                warn.append(f"cluster {cid}: a live process has no VM type yet, or no listed candidate types: its "
                            f"cost is estimated at its cluster's other workers' bound")
            types |= {x for x in (vm, *(listed if isinstance(listed, list) else ())) if x}
        missing = budget.unpriced(sorted(map(str, types)))
        if missing:
            warn.append(f"cluster {cid} runs or may run VM type(s) {missing} not in cloud/vm_prices.yaml: counted at "
                        f"the fallback bound; add them to the table")
    except Exception as e:  # noqa: BLE001 -- warned, not refused (fail soft)
        warn.append(f"the VM types of cluster {cid} cannot be read ({type(e).__name__}: {str(e)[:120]})")
    for w in warn:
        print(f"WARNING: {name}: {w}", flush=True)
    return cid


def _budget_gate(a, name, meter=False, reserve=True):
    """Refuse (SystemExit) unless the owner-signed budget of `name`'s scope covers its spend plus --cap-usd (with
    `reserve`, the cap is reserved until the cluster appears); with `meter`, return fn(cluster id) -> that cluster's
    USD so far (cloud/budget.py)."""
    import budget
    scope = a.budget_scope or budget.scope_for_name(name)
    if not scope:
        raise SystemExit(f"{name} matches no budget scope in cloud/budget_scopes.yaml: refusing (its spend would "
                         "count against no budget)")
    ok, why = budget.launch_budget_ok(scope, a.cap_usd, name=name, reserve=reserve, warn_over=True)
    if not ok:
        raise SystemExit(f"{name}: the budget refuses this launch: {why}")
    print(f"budget: {why}", flush=True)
    if not meter:
        return None
    try:
        return budget.job_meter(scope)
    except Exception as e:  # noqa: BLE001
        # Codex round 4 on #265 (P1): the check accepted (and reserved) this launch; a cost source that cannot be
        # read now (DNS, a timeout) never refuses it. Alerted; the meter is rebuilt at each reading, and
        # _cap_reading reports its failures.
        print(f"WARNING: {name}: the cost meter could not be built ({type(e).__name__}: {str(e)[:120]}); "
              "the launch goes on, metering as readings allow", flush=True)
        budget.alert("cost_unreadable", name=name, cap_usd=a.cap_usd, where="launch meter",
                     error=f"{type(e).__name__}: {str(e)[:120]}")
        return lambda c: budget.job_meter(scope)(c)


def _cap_reading(meter, cid, cap, job_id, misses, track):
    """One cost reading of the job's cluster. It never stops anything (owner decision, 2026-11-17: no hard dollar
    ceilings): it prints the reading and writes <dest>/cap_projection.json (budget.cap_projection: level, burn rate,
    hours to the cap and to CAP_ALERT_MULTIPLE x it, spend projected at the polling deadline) on every reading, and on
    each upward level crossing (over_cap, far_over_cap) it prints a WARNING and records a spend alert
    (budget.alert -> spend_alerts.jsonl). Three failed readings in a row are alerted the same way (cost_unreadable),
    once per streak; the job runs on."""
    import budget
    try:
        usd = float(meter(cid))
    except Exception as e:  # noqa: BLE001
        misses[0] += 1
        print(f"{job_id}: cost reading failed ({type(e).__name__}: {str(e)[:120]})", flush=True)
        if misses[0] == 3:
            print(f"WARNING: {job_id}: cluster {cid}'s cost could not be read 3 times in a row; it runs on", flush=True)
            budget.alert("cost_unreadable", job_id=job_id, cluster_id=cid, cap_usd=cap,
                         error=f"{type(e).__name__}: {str(e)[:120]}")
        return None
    misses[0] = 0
    track["readings"].append((time.time(), usd))
    pr = budget.cap_projection(track["readings"], cap, track.get("deadline"))
    pr.update(job_id=job_id, cluster_id=cid)
    try:
        with open(os.path.join(track["dest"], "cap_projection.json"), "w") as f:
            json.dump(pr, f, indent=1)
    except OSError as e:
        print(f"{job_id}: cap_projection.json not written ({type(e).__name__})", flush=True)
    print(f"{job_id}: cluster {cid} at ${usd:.2f} of its ${cap:.2f} cap ({pr['level']})", flush=True)
    order = ("under_cap", "over_cap", "far_over_cap")
    if order.index(pr["level"]) > order.index(track.get("level", "under_cap")):
        eta = (f"; ${pr['usd_per_hour']:.2f}/h" if pr.get("usd_per_hour") is not None else "")
        print(f"WARNING: {job_id}: cluster {cid} is {pr['level']} at ${usd:.2f} (cap ${cap:.2f}){eta}; nothing is "
              f"stopped for dollars", flush=True)
        budget.alert(pr["level"], job_id=job_id, cluster_id=cid, usd=pr["usd"], cap_usd=cap,
                     usd_per_hour=pr["usd_per_hour"], hours_to_far_over_cap=pr["hours_to_far_over_cap"],
                     projected_usd_at_deadline=pr["projected_usd_at_deadline"],
                     record=os.path.join(track["dest"], "cap_projection.json"))
        track["level"] = pr["level"]
    return None


def cmd_up(a):
    _vm_request(a)                      # a request no priced VM type matches is refused before anything is listed
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
    secret_env(getattr(a, "secret_env", None))      # refused before the budget check or any cluster call
    pipeline = job_pipeline(cmd)        # a main.py job states its pipeline, or nothing starts
    if cluster is None:                 # interactive start keeps a paid cluster busy: the budget must still cover it
        checked = _live_vm_types_ok(a.name)     # and the budget must be able to price every VM it runs
        _budget_gate(a, a.name, reserve=False)
    code = a._code if hasattr(a, "_code") else _tarball(a.tree, a.dir)
    base = getattr(a, "base_name", a.name)
    account = storage_account(getattr(a, "store_account", None))
    extra = secret_env(getattr(a, "secret_env", None))
    secrets = job_env({**({"ANTHROPIC_API_KEY": CO.read_keys()["ANTHROPIC_API_KEY"]} if a.anthropic_key else {}),
                       **extra} or None, base, a.job_id, account)
    c, w = _client(a.name, cluster, cid=None if cluster is not None else checked)
    token = _runner(c, w)(ws.job_queue, a.job_id)
    if not token:
        raise SystemExit(f"{a.job_id} is queued or running on {a.name}; pick another id")
    shell = " ".join(cmd) if len(cmd) == 1 else cmd
    store = {"prefix": a.store.rstrip("/"), "account": account} if a.store else None
    fut = c.submit(ws.job_task, a.job_id, code, shell, secrets, a.max_hours, a.setup_sdk, token=token,
                   cluster_compute=a.cluster_compute, store=store, shutdown_when_stored=a.shutdown_when_stored,
                   shutdown_grace_s=getattr(a, "shutdown_grace_s", 0),
                   shutdown_always=getattr(a, "shutdown_always", False), workers=[w], allow_other_workers=False,
                   pure=False, key=f"job-{a.job_id}-{int(time.time())}")
    fire_and_forget(fut)
    if cluster is None:                 # interactive start returns at once: hold the client until the job runs
        _await_claim(_runner(c, w), a.job_id)   # (`run` polls the job itself)
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


def _await_claim(run, job_id, timeout_s=600, every_s=5, clock=time.monotonic, sleep=time.sleep):
    """Wait until the submitted job has claimed its id (W0-J smoke, 2026-11-22: `start` returned right after
    fire_and_forget, the client closed before the 5 MB task reached the scheduler, and the job stayed 'queued' with
    nothing running). Returns the job's state; an answer that is not a state record (a test fake) returns None."""
    end = clock() + timeout_s
    while True:
        st = run(ws.job_state, job_id)
        if not isinstance(st, dict) or not isinstance(st.get("state"), dict):
            return None
        if st["state"].get("state") != "queued":
            return st["state"].get("state")
        if clock() >= end:
            raise SystemExit(f"{job_id}: still queued after {timeout_s}s: the submission did not reach the worker")
        sleep(every_s)


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


def _poll(a, dest, gone_ok=False, deadline=None, cluster=None, cid=None, meter=None):
    """Poll (saving outputs each time) until the job ends; returns its last status. With
    `gone_ok` (`run`), a cluster that is no longer running (by id `cid`) returns None, and
    every other error (a dropped connection, a failing cluster listing) is retried, up to
    `deadline`. Every poll goes to the job's own worker (job.json); that worker missing from
    the table on three polls in a row reads as 'lost'. With `meter` (`run`), each poll also reads the cluster's
    USD before anything returns (_cap_reading: recorded and alerted, never a stop)."""
    want, gone_polls, misses = _job_worker(dest), 0, [0]
    track = {"dest": dest, "readings": [], "deadline": deadline}

    def cap():
        if meter is not None and cid is not None:
            _cap_reading(meter, cid, a.cap_usd, a.job_id, misses, track)
    while True:
        checked = False
        try:
            try:
                c, w = _client(a.name, cluster, want)
            except WorkerGone as e:
                gone_polls += 1
                if gone_polls >= 3:
                    cap()
                    print(f"{a.job_id}: lost ({e})")
                    return {"state": {"job_id": a.job_id, "state": "lost", "why": str(e)}, "files": {},
                            "fetched": [], "log_tail": ""}
                raise
            gone_polls = 0
            st = sync_outputs(_runner(c, w), a.job_id, dest)
            _print(st, dest)
            cap()
            checked = True
            if st["state"]["state"] in DONE + ("missing", "lost"):
                return st
        except (Exception, SystemExit) as e:  # noqa: BLE001 — a dropped connection is not the end of the job
            if isinstance(e, SystemExit) and not gone_ok:
                raise     # `wait`/`status`: the attach guard's refusal ends the command
            if gone_ok:
                try:
                    if (_gone(a.name, cid) if cid is not None else not CO.is_running(a.name)):
                        return None
                except Exception as e2:  # noqa: BLE001 -- cannot tell: retry
                    print(f"cluster listing failed ({type(e2).__name__}: {str(e2)[:120]})")
            print(f"poll failed ({type(e).__name__}: {str(e)[:120]}); retrying")
        if not checked:
            cap()
        if deadline is not None and time.time() > deadline:
            raise SystemExit(f"{a.job_id}: still not finished {a.max_hours} h after submission (plus 1 h); "
                             f"`status {a.name} {a.job_id}` checks it")
        time.sleep(a.every)


def cmd_wait(a):
    dest = local_dir(a.name, a.job_id, a.out)
    if getattr(a, "cap_usd", None) is not None:
        return _wait_capped(a, dest)          # no SIGTERM handler: a monitor's end never stops the job
    a.name = _job_cluster(dest, a.name)
    return _poll(a, dest)


class _CapRefused(SystemExit):
    """`wait --cap-usd` refused at the budget gate, before it accepted the cap (no scope, cap at or below the spend so
    far, budget short): the cluster is left as it was."""


def _wait_gate(a, name, cid, dest, accepted):
    """`wait --cap-usd`: fn(cluster id) -> its lifetime USD, once budget.reattach_budget_ok has replaced the
    cluster's reservation with the cap's remaining amount; None when the cluster is no longer live. Raises _CapRefused
    on a refusal. The listing and the budget are read up to three times until the gate accepts or refuses; once it
    accepts (`accepted` is set), the gate never runs again, and only the meter is built, up to three times. Still
    unreadable before it accepts, it alerts and raises _CapRefused (the cluster is left as it was); after it accepts,
    it alerts and returns a meter rebuilt at each reading (never a stop)."""
    import budget
    scope = a.budget_scope or budget.scope_for_name(name)
    if not scope:
        raise _CapRefused(f"{name} matches no budget scope in cloud/budget_scopes.yaml: refusing")
    why = None
    for i in range(3):
        try:
            if not accepted:
                state = _clusters(name).get(cid, "")
                if state not in CO._ATTACH:
                    print(f"{name} ({cid}): {state or 'not listed'}; nothing to meter")
                    return None
                ok, why = budget.reattach_budget_ok(scope, a.cap_usd, name, cid,
                                                    os.path.join(dest, "reattach_clusters.json"), warn_over=True)
                if not ok:
                    raise _CapRefused(f"{a.job_id}: the budget refuses this cap: {why}; `down <name> {a.job_id}` "
                                      "stops the cluster")
                accepted.append(why)       # the reservation is replaced: from here on, the cluster is stopped
            meter = budget.lifetime_meter(scope)
            print(f"budget: {accepted[0]}", flush=True)
            return meter
        except _CapRefused:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"{a.job_id}: budget or cost reading failed ({type(e).__name__}: {str(e)[:120]})", flush=True)
            if i < 2:
                time.sleep(5)
    if not accepted:
        # Codex on #265 (P1): the budget never accepted this cap (an unreadable or unsigned budget raises inside it),
        # so this wait takes no ownership of the cluster: alerted, and left as it was.
        budget.alert("cost_unreadable", job_id=a.job_id, cluster_id=cid, cap_usd=a.cap_usd, where="wait --cap-usd gate")
        raise _CapRefused(f"{a.job_id} on {name}: the budget could not be read 3 times before accepting the cap; the "
                          "cluster is left as it was (retry `wait --cap-usd`, or `down` it)")
    # Never a stop for bookkeeping (owner decision, 2026-11-17): alerted, then metered by a meter rebuilt at each
    # reading, whose failures _cap_reading reports.
    print(f"WARNING: {a.job_id} on {name}: the budget or cluster {cid}'s cost could not be read 3 times in a row; "
          "waiting on, metering as readings allow", flush=True)
    budget.alert("cost_unreadable", job_id=a.job_id, cluster_id=cid, cap_usd=a.cap_usd, where="wait --cap-usd")
    return lambda c: budget.lifetime_meter(scope)(c)


def _wait_capped(a, dest):
    """`wait --cap-usd`: the cluster id job.json records, gated (_wait_gate), attached by that id, then polled with
    `run`'s cost readings on its lifetime cost (recorded and alerted, never a stop for dollars). A monitor never stops
    a detached job that is still running (lead decision, 2026-11-18): the cluster is stopped by id only once the job
    has ended for certain (a terminal state in DONE); an error, Ctrl-C or SIGTERM ends the wait and
    leaves the cluster as it is, and the job's own end-of-run stop covers it."""
    rec = _job_record(dest)
    name, cid = rec.get("cluster") or a.name, rec.get("cluster_id")
    if cid is None:
        raise SystemExit(f"{a.job_id}: {dest}/job.json records no cluster id (only `run` records one); "
                         "`wait --cap-usd` meters and stops a cluster by its id only")
    a.name, accepted = name, []
    meter = _wait_gate(a, name, cid, dest, accepted)
    if meter is None:
        return None
    cl = ws.attach_existing(name, CO.WORKSPACE, cid)     # refuses any cluster but `cid`
    print(f"{a.job_id}: metering cluster {name} ({cid}) to ${a.cap_usd:.2f}", flush=True)
    st = _poll(a, dest, gone_ok=True, cluster=cl, cid=cid, meter=meter)
    # Only a confirmed end (Codex re-review of #265): 'missing' (one unreadable state file) and 'lost' (a stale
    # heartbeat) do not show the process exited, so they leave the cluster to the job's own end-of-run stop.
    # A record with a pid and no return code (the worker could not wait for its child, Codex round 3 on #265) does
    # not show the process exited either.
    js = (st or {}).get("state") or {}
    if js.get("state") in DONE and not (js.get("pid") is not None and js.get("rc") is None):
        _stop_by_id(name, cid, f"{a.job_id}: {js['state']}"[:200])
    return st


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
    cs = coiled.list_clusters(workspace=CO.WORKSPACE)
    return cs if isinstance(cs, list) else list(cs.values())


def _gone(name, cid) -> bool:
    return _clusters(name).get(cid, "stopped") in _ENDED


def _stop_by_id(name, cid, why):
    """Stop the cluster `run` created, by id via `coiled_ops.stop_cluster_by_id`."""
    CO.stop_cluster_by_id(name, cid, why, lister=_all_clusters)


_CLEANING = [False]     # True while `run` stops its cluster: a SIGTERM then is ignored, so the stop completes


class Terminated(KeyboardInterrupt):
    """SIGTERM, handled as Ctrl-C is, so the finally that stops the cluster by id runs (the default action skipped
    it, 2026-11-17)."""


@contextlib.contextmanager
def _sigterm_as_interrupt():
    """While a command that owns a cluster runs: SIGTERM raises Terminated (once; a second SIGTERM during the stop is
    ignored, so the stop completes); the previous handler is restored after."""
    import signal

    def handler(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if not _CLEANING[0]:         # during the stop itself (Codex re-review of #265): ignored, the stop completes
            raise Terminated("SIGTERM")
    _CLEANING[0] = False
    try:
        prev = signal.signal(signal.SIGTERM, handler)
    except ValueError:                  # not the main thread: nothing to install
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, prev)


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
        with _sigterm_as_interrupt():
            return _run_locked(a, dest)
    finally:
        lock.close()


def _run_locked(a, dest):
    """`run` once the job's lock is held (see cmd_run)."""
    job_pipeline(a.command)                # an undeclared main.py pipeline is refused before anything is created
    storage_account(getattr(a, "store_account", None))   # so is a storage account value that is not a name
    secret_env(getattr(a, "secret_env", None))           # and a --secret-env name that is not allowed or not set
    a._code = _tarball(a.tree, a.dir)      # a dirty tree is refused before anything is created
    _vm_request(a)                         # so is a request no priced VM type matches (before the reservation)
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
        st = _poll(a, dest, gone_ok=True, deadline=deadline, cluster=cl, cid=cid, meter=meter)
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
        _CLEANING[0] = True
        cl = box.get("cl")
        if "constructing" not in box:
            pass    # refused or failed before the constructor was called: nothing of ours to stop
        elif good or not a.keep_on_failure:
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
    CO.attach(a.name).shutdown()
    print(f"{a.name}: shutdown requested")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    argv, command = split_command(argv)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    def up_args(p):
        p.add_argument("--cpu", type=int, default=4)
        p.add_argument("--memory", default="16 GiB")
        p.add_argument("--idle-timeout", default="20 minutes")
        p.add_argument("--workers", type=int, default=1, help="workers of the one cluster (one scheduler for all)")
        p.add_argument("--pin-vm-type", default=None,
                       help="exactly this priced VM type for every worker (one CPU family), instead of every priced "
                            "type matching --cpu/--memory; its vCPUs must equal --cpu")

    def start_args(p):
        p.add_argument("job_id")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--tree")
        g.add_argument("--dir")
        p.add_argument("--max-hours", type=float, default=24.0)
        p.add_argument("--setup-sdk", action="store_true")
        p.add_argument("--anthropic-key", action="store_true")
        p.add_argument("--secret-env", action="append", default=[], metavar="NAME",
                       help=f"pass the launcher's $NAME to the job's environment only (allowed: {SECRET_ENV_ALLOWED})")
        p.add_argument("--cluster-compute", action="store_true",
                       help="the job's compute.py run_map uses every worker of this cluster")
        p.add_argument("--store", metavar="PREFIX", default=None,
                       help="when the job ends, upload $JOB_OUT to <PREFIX>/ and verify each file read back (sha256)")
        p.add_argument("--store-account", default=None,
                       help="storage account name (default: the launcher's $MA_STORAGE_ACCOUNT, then "
                       "$COILED_STORAGE_ACCOUNT, then the worker's configured one)")
        p.add_argument("--shutdown-when-stored", action="store_true",
                       help="the job stops its cluster once it is done and every output is verified in storage")

    def budget_args(p):
        p.add_argument("--cap-usd", type=float, required=True, help="the cluster's spend cap (USD): crossings are warned about and alerted, never a stop")
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
            p.add_argument("--cap-usd", type=float, default=None,
                           help="re-attach a cost cap: meter the job's recorded cluster id (alerts past the cap, "
                                "never a stop for dollars) and stop it by id when the job ends")
            p.add_argument("--budget-scope", default=None, help="default: the scope whose prefix the name has")
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
