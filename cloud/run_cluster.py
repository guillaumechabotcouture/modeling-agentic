"""Launch and manage a pipeline run on IDM Coiled from one config file.

    python cloud/run_cluster.py <config.py> launch            # cluster, run(s), keeper; then safe to disconnect
    python cloud/run_cluster.py <config.py> status            # running / done per run, cluster and keeper state
    python cloud/run_cluster.py <config.py> collect [--no-shutdown] [--accept-partial]
                                                              # fetch finished runs (from storage once stopped);
                                                              # then shut down, unless --no-shutdown. A run whose
                                                              # large files exist only on its VM stays pending
                                                              # until --accept-partial
    python cloud/run_cluster.py <config.py> sign <answers.yaml> [--label L] [--run RUN] [--action spec|scope]
                         [--expect-spec-sha256 SHA] [--expect-derivations-sha256 SHA|none] [--expect-sheet-sha256 SHA] [note]
                                                              # pending scope declarations, a v2 spec, or the brief
    python cloud/run_cluster.py <config.py> relaunch <label>
    python cloud/run_cluster.py <config.py> keeper            # reconcile, then start missing helpers for the run
                                                              # cluster clusters.json records (after exit 3 or 5)
    python cloud/run_cluster.py <config.py> reuse-check <run_dir> [--expect-all]
                                                              # how many stored engine rows the code this launch
                                                              # would ship reuses (local, no cloud call)
    python cloud/run_cluster.py <config.py> checkpoint [--label L]
                                                              # the latest checkpoint of the latest launch, sha256
                                                              # verified, into <out_dir>/<label>/checkpoint/
    python cloud/run_cluster.py <config.py> checkpoint-now [--timeout-s 300] [--invocation T] [--launch-id L]
                                                              # one checkpoint from each running run, awaited in
                                                              # storage under one deadline (recorded cluster id)
    python cloud/run_cluster.py <config.py> shutdown          # cluster, overflow pool and keeper, by hand
    python cloud/run_cluster.py <config.py> report-cost [--ledger PATH]... [--start ISO] [--end ISO]
                                                              # Coiled credits per useful core-hour (billing API only)

Detached jobs on a single VM (the dev VM): cloud/vm_job.py.

A config is a Python file defining CONFIG = RunConfig(...) (see cloud/configs/).

Lifecycle. Each run is pinned to its own agent VM and submitted with
fire_and_forget, so it keeps running when this process exits. A finished run
uploads its directory (main.py reads every upload back), its transcripts and
its result record to storage; the last run of the launch then shuts the
cluster down, and the keeper exits when the cluster stops and shuts its own VM down.
If an upload fails, the agent VM holds the results on disk and stays busy
(up to 24 h) until `collect` fetches them, after which `collect` shuts the
cluster down. Nothing waits on a laptop: E7 attempt 7 sat idle 9.5 h after
its results were stored because the launcher waited for a manual collect.

Checkpoints (F6, 2026-11-11: a v2 preview stopped at its credit cap after 3 h had stored nothing, because main.py
stores the run only when it ends). While main.py runs, the agent VM uploads a tarball of the run dir (runs/<RUN>/...
and run.log; never _truth/, credential files or links; API keys redacted; the run dir reached from the work dir and
each file opened once, no link followed at any component, and what is scanned is what is archived) every
`checkpoint_minutes` (default 15, 0 off). Each checkpoint is a
new generation, written once: outputs/<storage_prefix>/launch_<id>/checkpoint_<label>_g<N>.tar.gz and its record
checkpoint_<label>_g<N>.json (time, size, sha256, and, read from the archive, run_state state and round and the
cached engine batches and rows); then the pointer checkpoint_<label>.json names it and its `previous`. Generations
are numbered after every one already stored for the launch and label (with no pointer, the newest complete one is
the base, for the uploader and `checkpoint` alike, its sha256 hashed as it streams); a capture during which
run_state.json changed is discarded and retried once; one whose pointer upload was attempted is never deleted, and
only those older than both the pointer's
are deleted, after it is published. One at a time (a checkpoint due while the previous one uploads is skipped); one
needs free space of 2.2 x (largest file + run dir, run.log counted as a file) + 2 GB; a failure is logged on the VM
(/tmp/ab/<label>/.checkpoint/checkpoint.log), leaves no local archive, and never stops the run. `collect` says when
a label has only a checkpoint. `checkpoint-now` files a request on every agent VM at once (each returns at once; a
request survives a skip or failure and is retried within 30 s) and waits in storage, under one deadline that bounds
every call, for a generation carrying it or started after it. Resuming from one:
  1. `run_cluster.py <cfg> checkpoint [--label L]`: <out_dir>/<L>/checkpoint/runs/<RUN>/ and checkpoint.json
     (the pointer's generation, or the previous one when it does not match its sha256; refused when neither does).
  2. Put the run dir into the launch tree at runs/<RUN> and commit it there (`git add -f runs/<RUN>`: runs/ is
     gitignored, and a launch ships `git archive HEAD`); a resume config sets require_main=False and
     flags=("--resume", "runs/<RUN>").
  3. `run_cluster.py <resume cfg> reuse-check <out_dir>/<L>/checkpoint/runs/<RUN> --expect-all`: the shipped code
     reuses every stored engine row (scripts/compute.py reuse path: `<out>.summary.json` rows and the
     `<out>.partial.jsonl` journal of a batch the stop interrupted; a results file whose sha256 is not the one
     its summary records, as in a snapshot taken between the two writes, is recomputed); then launch.
  Engine rows survive the round trip only where the fingerprint does: a file over 64 MB under models/ or data/ is
  fingerprinted by size and mtime (ns), which neither the tarball (whole seconds) nor git archive keeps, and a
  symlink there is not archived: either one changes every batch's fingerprint. reuse-check (step 3) shows it.
Before any paid launch, fresh or resumed, run the resume smoke on the tree the launch ships (`git archive HEAD`
extracted): `python3 scripts/resume_smoke.py <tree>`. It interrupts toy v2 runs at the two resumable ANALYZE states
(step timeout, batch killed) and resumes them with that tree's code, and stops a toy batch at its compute allowance and
reruns it (a cap stop is not resumable by --resume; a relaunch reruns the batch). It exits 1 unless every kept
engine row is reused, none is recomputed and the lead's sign-off still binds the spec. Local, toy engines only, about a minute.

Exit codes of `launch` (and `relaunch`), for a watchdog. Once a run is submitted the launch is never
reported as failed, because the run keeps going whatever happens to this process (F5 resume launch,
2026-11-08: the scheduler listed the run, the laptop's connection then dropped, and a watchdog that read the
error as a failed launch shut the clusters down). `clusters.json` names every cluster created, with the launch
id, the launch time, a `status` and this invocation's token, from the moment the run cluster exists. The token
is the first line printed (`invocation <token>`), or MA_LAUNCH_INVOCATION when the watchdog sets it. One launch,
`keeper` or `relaunch` of a config runs at a time (a lock in its out_dir); a refusal (already running, preflight,
lock held) writes no record, so an earlier launch's record stays as it was. Every cluster is created with the
Coiled tag ma-invocation=<token> (for people: Coiled does not list tags) and the config's cluster_timeout. A
cluster is this invocation's only when no live one of its name was listed just before the constructor, Coiled
logged that it created it (not "Attaching to existing cluster"), and its created time is no earlier than the
construction's start less 120 s; otherwise it is refused before any record (out_dir/constructions.jsonl keeps
each construction's start and outcome).
  0 launched: every run listed by the scheduler, overflow pool and keeper confirmed (status "launched").
  1 failed: this invocation submitted nothing. Stopping clusters is authorized only by a clusters.json whose
    `invocation` is this invocation's token and whose status is "failed"; then stop the ids it names. Any other
    record (none, another token, another status) belongs to an earlier launch: stop nothing.
  3 submitted, connection lost: the scheduler listed every run, then this process lost its connection
    (status "submitted_connection_lost"). Do not shut down; watch with `status`, and start
    a missing keeper with `keeper`.
  4 submitted, not confirmed: a run was sent, but the scheduler's acceptance of it was not seen (its future
    finished or erred, the scheduler lists it as processing, or holds it queued or waiting); connection lost
    first, not seen in time, or interrupted (status "submitted_unconfirmed"). It may be running: do not shut
    down; check `status`. `relaunch` exits 0 or 4 once it has submitted.
  5 submitted, launch incomplete: every run listed, but a later step failed, the record write included (overflow
    pool or keeper; status "submitted_launch_incomplete"). Do not shut down; fix and run `keeper`.
Any other code (a usage error, an exception, an interrupt) comes from before any submission: a failure, under
the same rule as 1.

Rules: the IDM Coiled default workspace only (never the prod subscription);
attach-only for everything but `launch` (coiled.Cluster(name=...) on a
cluster that is gone creates a new, empty one); API keys go in as task
arguments and are never printed or written; answer keys (`_truth/`) are
never stored.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import worker_side as ws  # noqa: E402 — uploaded to the VMs under this name
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "lib"))
import eval_paths  # noqa: E402 — Phase 50 α: what a launch tarball must never hold
import pipeline_id  # noqa: E402 — which pipeline main.py runs; the identity each run records
sys.modules.setdefault("run_cluster", sys.modules[__name__])   # configs import RunConfig from here

import coiled_ops as CO  # noqa: E402
from coiled_ops import (  # noqa: E402, F401
    ACK_ATTEMPTS, ACK_DELAY_S, ACK_STATES, CONNECTION_ERRORS, ENV_FILE,
    INVOCATION_TAG, JOURNAL, LIVE_STATES, OWN_SKEW_S, WORKSPACE,
    _ATTACH, _ATTACHED, _CREATED, _archive_member, _items, _journal, _ts,
    acknowledged as _acknowledged, archive_commit, attach, cluster_states,
    coiled_events as _coiled_events, connect_existing, existing_client,
    is_running, keeper_wait, live_id, read_journal, read_keys,
    scheduler_state as _scheduler_state, wait_listed,
)
CHUNK = 8 * 2**20


@dataclasses.dataclass
class RunConfig:
    name: str                                  # cluster name; the keeper is "<name>-keeper"
    question_file: str
    tree: str = os.path.expanduser("~/modeling-agentic-launch")
    labels: tuple = ("R1",)
    flags: tuple = ()                          # extra main.py flags (never --v1/--v2: set `pipeline`)
    pipeline: str = ""                         # "v2", required to launch; passed to main.py explicitly (v1 is retired)
    max_rounds: int = 8
    max_sessions: int = 5
    cross_model: bool = False                  # pass the OpenAI key for the cross-model critic
    storage_account: str = "modelingagentic"
    storage_prefix: str = ""                   # outputs/<prefix>/...; default runs/<name>
    out_dir: str = ""                          # local collect dir; default ~/modeling-agentic-keep/<name>
    compute_start: int = 1                     # compute VMs at launch, and the pool's floor (see below)
    compute_max: int = 24
    compute_env: dict = dataclasses.field(default_factory=dict)
    keeper: bool = True                        # hold the pool's autoscaler on a small VM
    keeper_hours: float = 48.0
    idle_timeout: str = "1 hour"               # backstop only: a running or holding task keeps it busy
    require_main: bool = True                  # ship exactly origin/main (False for smoke tests of a branch)
    required_files: tuple = ("scripts/results_store.py", "scripts/compute.py")
    # Phase 42 β (A5): VM reuse. Attempt 8 created 734 compute VMs in 18 h (each lived
    # minutes: up per batch, down 2 min after it) and billed ~6.5 credits per useful
    # core-hour. An idle compute VM now stays `scale_down_after` before the pool retires
    # it, so the next batch reuses it: 19 of attempt 8's 22 gaps between batches were
    # under 10 minutes. The floor stays at one compute VM (`compute_start`): the typical
    # small batch (a 1,100-draw UQ, 2.25 core-hours) ran 24-36 min on one VM, and each
    # further floor VM costs ~144 core-hours over an 18 h run whether used or not.
    scale_down_after: str = "10 minutes"
    adapt_interval: str = "15s"                # how often the autoscaler re-plans
    target_duration: str = ""                  # "" keeps compute.ADAPT_OPTIONS' (how hard a batch bursts)
    # Phase 42 β (A6): quota fallback. Coiled cannot change a live cluster's VM types and
    # does not fall back on a quota refusal; while the primary family is refused, the
    # keeper grows "<name>-overflow" (these VM types, separate quota families: 8 vCPU,
    # 32 GiB, enough for the attempt-8 batches' ~0.7 GB per process) whose VMs join the
    # run cluster's scheduler. overflow_max=0 disables it; None means compute_max.
    overflow_vm_types: tuple = ("Standard_D8s_v5", "Standard_D8as_v5")
    overflow_max: int | None = None
    overflow_hold: str = "30 minutes"          # keep using overflow this long after the last refusal
    # S1-8: cloud-side lifetimes (a smoke sets them short): main.py is killed past run_timeout_hours (None: no
    # limit), and a run whose upload failed is held for `collect` at most hold_hours
    run_timeout_hours: float | None = None
    hold_hours: float = 24.0
    # Coiled's cluster_timeout (coiled.Cluster(cluster_timeout=...), "40 minutes"): a cluster is shut down that long
    # after it starts, busy or not. "" for none (a real run's length is not known in advance)
    cluster_timeout: str = ""
    # F6 Stage 2 (2026-11-11: $106 spent of a $100 budget): the most this launch may spend, required to launch;
    # `launch` refuses unless the owner-signed budget of `budget_scope` covers the scope's spend plus it
    # (cloud/budget.py). budget_scope defaults to the scope whose prefix `name` has (cloud/budget_scopes.yaml).
    cap_usd: float | None = None
    budget_scope: str = ""
    # F6 run checkpoints: while main.py runs, the agent VM uploads its run dir every checkpoint_minutes (0: off)
    checkpoint_minutes: float = 15.0

    def __post_init__(self):
        if self.cap_usd is not None and (isinstance(self.cap_usd, bool) or not isinstance(self.cap_usd, (int, float))
                                         or not self.cap_usd > 0 or self.cap_usd == float("inf")):
            raise ValueError(f"cap_usd must be a positive number of USD, not {self.cap_usd!r}")
        if not self.budget_scope:
            self.budget_scope = _default_scope(self.name)
        self.storage_prefix = self.storage_prefix or f"runs/{self.name}"
        self.out_dir = self.out_dir or os.path.expanduser(f"~/modeling-agentic-keep/{self.name}")
        self.labels, self.flags, self.required_files = tuple(self.labels), tuple(self.flags), tuple(self.required_files)
        self.overflow_vm_types = tuple(self.overflow_vm_types)
        if self.overflow_max is None:
            self.overflow_max = self.compute_max
        bad = [k for k in ("--max-rounds", "--max-sessions") if k in self.flags]
        if bad:
            raise ValueError(f"set {bad} through max_rounds / max_sessions, not flags")
        if not self.labels or len(set(self.labels)) != len(self.labels):
            raise ValueError("labels must be non-empty and distinct")
        if self.pipeline in pipeline_id.RETIRED:
            raise ValueError(f"{self.name}: {pipeline_id.RETIRED_MESSAGE}")
        if self.pipeline and self.pipeline not in pipeline_id.PIPELINES:
            raise ValueError(f"pipeline must be 'v2', not {self.pipeline!r}")
        if {"--v1", "--v2"} & set(self.flags):
            raise ValueError("set the pipeline with pipeline='v2', not flags: the launcher passes it to main.py")
        for f in ("scale_down_after", "adapt_interval", "overflow_hold") + (("target_duration",) if self.target_duration else ()):
            seconds(getattr(self, f))   # fail at load, not on the keeper VM
        if not self.checkpoint_minutes >= 0:
            raise ValueError("checkpoint_minutes must be 0 (off) or positive")
        if seconds(self.adapt_interval) <= 0:
            raise ValueError("adapt_interval must be positive")
        if self.overflow_max and not self.overflow_vm_types:
            raise ValueError("overflow_max needs overflow_vm_types")
        if self.compute_start < 1 and (self.compute_env or {}).get("MA_COMPUTE_RESOURCE"):
            raise ValueError("compute_start must be at least 1 when compute tasks need a resource tag "
                             "(MA_COMPUTE_RESOURCE): with no tagged worker the autoscaler's target stays at the "
                             "current pool, so the pool never grows")

    @property
    def state_file(self):
        return os.path.join(self.out_dir, "assignments.json")

    @property
    def keeper_name(self):
        return f"{self.name}-keeper"

    @property
    def overflow_name(self):
        return f"{self.name}-overflow"

    @property
    def clusters_file(self):
        return os.path.join(self.out_dir, "clusters.json")


_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
          "minutes": 60, "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600}


def seconds(d) -> float:
    """'15s', '10 minutes', '1 hour', 90 -> seconds (the forms dask and Coiled accept)."""
    import re
    if isinstance(d, (int, float)):
        return float(d)
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*", str(d))
    if not m or (m.group(2) and m.group(2).lower() not in _UNITS):
        raise ValueError(f"not a duration: {d!r}")
    return float(m.group(1)) * _UNITS[m.group(2).lower() or "s"]


def adapt_options(cfg, base: dict) -> dict:
    """The keeper's adapt() options: the tree's ADAPT_OPTIONS, with the config's re-plan
    interval and scale-down delay (dask retires a worker after `wait_count` consecutive
    re-plans that suggest it, so the delay is interval x wait_count)."""
    import math
    iv = seconds(cfg.adapt_interval)
    out = {**base, "interval": f"{iv:g}s", "wait_count": max(1, math.ceil(seconds(cfg.scale_down_after) / iv))}
    if cfg.target_duration:
        out["target_duration"] = cfg.target_duration
    return out


def _default_scope(name) -> str:
    """The budget scope whose prefix `name` has, or "" (then `launch` refuses)."""
    try:
        import budget
        return budget.scope_for_name(name) or ""
    except Exception:  # noqa: BLE001 -- an unreadable scopes file: no scope, and the launch refuses
        return ""


def budget_guard(cfg, reserve=True, inv=None) -> None:
    """Refuse (SystemExit) unless cfg.cap_usd is set and the owner-signed budget of cfg.budget_scope covers the
    scope's spend plus it (cloud/budget.py)."""
    if cfg.cap_usd is None:
        raise SystemExit(f"{cfg.name}: the config sets no cap_usd: refusing (a launch states the most it may spend)")
    CO.budget_guard(cfg.budget_scope, cfg.cap_usd, cfg.name, reserve=reserve, warn_over=True,
                    inv=inv, clusters_file=cfg.clusters_file)


def vm_request(cfg, compute=None) -> dict:
    """{role: worker_vm_types and scheduler_vm_types} for each cluster this config can construct ("run" when
    `compute` is given, "overflow" when it has an overflow pool, "keeper"), every type priced in cloud/vm_prices.yaml.
    Coiled is given these lists, never cpu/memory, so what the budget prices is what it gets (2026-11-13: a cpu/memory
    request got Standard_F8s_v2, unpriced, refused only once the cluster existed). A configured type the table lacks
    (compute.WORKER_VM_TYPES, overflow_vm_types) is pinned as configured with a printed WARNING, never refused (owner,
    2026-11-13: spend controls fail soft); budget.py counts it at its fallback bound."""
    import budget
    try:
        sched = budget.scheduler_vm_types()
        out = {"keeper": {"worker_vm_types": budget.vm_types_for(2, "8 GiB"), "scheduler_vm_types": sched}}
    except budget.BudgetError as e:     # the tracked table has no 2-vCPU, 8 GiB type: a broken table, not a request
        raise SystemExit(f"{cfg.name}: refusing before any cluster is created: {e}") from None
    roles = {}
    if cfg.keeper and cfg.overflow_max:
        roles["overflow"] = (list(cfg.overflow_vm_types), "overflow_vm_types")
    if compute is not None:
        roles["run"] = (list(compute.WORKER_VM_TYPES), "compute.WORKER_VM_TYPES")
    for role, (types, what) in roles.items():
        missing = budget.unpriced(types)
        if missing:
            print(f"WARNING: {cfg.name}: {what} {missing} not in cloud/vm_prices.yaml: launching with them, counted at "
                  f"the fallback bound; add them to the table", flush=True)
        out[role] = {"worker_vm_types": types, "scheduler_vm_types": sched}
    return out


def load_config(path: str) -> RunConfig:
    spec = importlib.util.spec_from_file_location("run_config", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_config"] = mod
    spec.loader.exec_module(mod)
    cfg = getattr(mod, "CONFIG", None)
    if not isinstance(cfg, RunConfig):
        raise SystemExit(f"{path} defines no CONFIG = RunConfig(...)")
    return cfg


def _results_store():
    """This repo's scripts/results_store, imported as eval_paths and pipeline_id are (from the repo this file is in),
    not from cfg.tree or $PYTHONPATH: `collect` died with ModuleNotFoundError when neither held it (CI1-CI2)."""
    d = os.path.join(os.path.dirname(HERE), "scripts")
    while d in sys.path:            # first, even when $PYTHONPATH also names another checkout's scripts/ (Codex r1)
        sys.path.remove(d)
    sys.path.insert(0, d)
    import results_store
    return results_store


def _compute(cfg):
    sys.path.insert(0, os.path.join(cfg.tree, "scripts"))
    import compute
    return compute


def _on_worker(client, fn, *args, worker):
    """client.run, not submit: the agent VM's only thread is busy with the run."""
    return client.run(fn, *args, workers=[worker], wait=True, on_error="raise")[worker]


def api_keys() -> dict:
    """read_keys, refused (SystemExit) when ANTHROPIC_API_KEY is missing or blank: launch and relaunch read the keys
    through this before any cluster is created or task submitted (Codex round 1 on #358: a cluster was created and
    recorded, then the submission failed on the key). Values are never printed."""
    try:
        keys = {k: str(v).strip() for k, v in read_keys().items()}
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(f"{ENV_FILE} cannot be read ({type(e).__name__}): refusing before any cluster") from None
    if not keys.get("ANTHROPIC_API_KEY"):
        raise SystemExit(f"no ANTHROPIC_API_KEY (missing or blank) in {ENV_FILE}: refusing before any cluster")
    return keys


def _release(cfg, inv) -> None:
    """Release this invocation's budget reservation after a refusal that came before any cluster (best effort)."""
    try:
        import budget
        n = budget.release_reservation(inv, cfg.name, clusters_file=cfg.clusters_file)
        print(f"budget: released {n} reservation(s) of {cfg.name} for invocation {inv}", flush=True)
    except Exception as e:  # noqa: BLE001 -- an unreleased reservation lapses after 2 h with no cluster (budget.py)
        print(f"budget: the reservation could not be released ({type(e).__name__}); it lapses after 2 h", flush=True)


def code_tarball(cfg) -> tuple[bytes, str]:
    """git archive of HEAD, after the guards: clean tree, origin/main unless
    the config allows a branch, and the files this launch depends on."""
    git = lambda *a: subprocess.check_output(["git", "-C", cfg.tree, *a], text=True).strip()  # noqa: E731
    subprocess.run(["git", "-C", cfg.tree, "fetch", "-q", "origin"], check=True)
    head, main_ = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
    if git("status", "--porcelain"):
        raise SystemExit(f"{cfg.tree} has uncommitted changes: refusing")
    if cfg.require_main and head != main_:
        raise SystemExit(f"{cfg.tree} is at {head[:7]}, origin/main is {main_[:7]}: refusing")
    names = set(git("ls-files").splitlines())
    missing = [n for n in cfg.required_files if n not in names]
    if missing:
        raise SystemExit(f"code to launch lacks {missing}: refusing")
    refuse_hidden_tree(cfg.tree)
    blob = CO.tarball_from_tree(cfg.tree, check_clean=False)
    refuse_hidden(blob, cfg.tree)
    return blob, archive_commit(blob) or head[:7]


def stale_sheet_problems(code: bytes, flags, pipeline: str = "v2") -> list[str]:
    """For a resumed run (--resume <run dir>) at or past SIGN in the archive that ships: check spec existence and
    all registered preconditions on the shipped bytes so an unsignable or stale run never starts a cluster."""
    if pipeline != "v2":
        return []
    import argparse
    ap = argparse.ArgumentParser(add_help=False)       # as main.py's parser: abbreviations, the last value wins
    ap.add_argument("--resume")
    try:
        got, _ = ap.parse_known_args([str(f) for f in (flags or ())])
    except SystemExit:
        return ["the launch flags' --resume value cannot be read"]
    runs = [got.resume] if got.resume else []
    out = []
    for rel in runs:
        rel = os.path.normpath(rel).replace(os.sep, "/")
        raw = _archive_member(code, f"{rel}/run_state.json")
        try:
            st = json.loads(raw.decode()) if raw is not None else None
            state = st.get("state") if isinstance(st, dict) else None
        except (ValueError, UnicodeDecodeError):
            state = None
        if raw is None or state is None:
            out.append(f"{rel}: run_state.json is missing or unreadable in the code to launch")
            continue
        hist = st.get("history") if isinstance(st.get("history"), list) else []
        signed_in_history = any(isinstance(h, dict) and h.get("from") == "SIGN"         # Codex r1 P1-3: a lost
                                and str(h.get("why", "")).startswith("spec signed by") for h in hist)   # record
        if state in PRE_SIGN_STATES or (state == "EVIDENCE" and not signed_in_history and not any(   # a draft
                _archive_member(code, f"{rel}/{f}") is not None for f in SIGNOFF_RECORDS)):
            continue
        if st.get("signoff") == "none":     # nobody signs: the same rule as the runtime gate (coverage, no record)
            out += [f"{rel}: {p}" for p in _preconditions(code, rel, "unsigned")]
            continue
        if state != "SIGN":
            out += _signed_run_problems(code, rel)
            continue
        spec = _archive_member(code, f"{rel}/decision_spec.yaml")
        if spec is None:
            out.append(f"{rel}: decision_spec.yaml is missing in the code to launch")
        else:       # F6 (#218): what signing will check (coverage too), so an unsignable run never starts a cluster
            out += [f"{rel}: {p}" for p in _preconditions(code, rel, "sign")]
    return out


TEST_ENGINE_REFS: tuple = ()      # self-tests only (set in-process, never from the environment): toy engines to enable
_CHECKER = r"""
import json, os, sys
tree, rel, signer, refs = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
sys.path[:0] = [tree, os.path.join(tree, "scripts")]
if refs:
    from toolkit import import_guard as G
    G.allow_test_refs("engine", refs)
from toolkit.spec import preconditions as PC
print("PRECONDITIONS " + json.dumps(PC.problems(os.path.join(tree, rel), None, None, mode=signer)))
"""


def _preconditions(code: bytes, rel: str, signer: str) -> list[str]:
    """The run's registered preconditions (toolkit/spec/preconditions.py) checked with the code the launch ships, not
    the launcher's checkout (#218 review round 4): the archive is extracted and its own toolkit checks its own run
    dir, engine declarations, cards, priors and fitted values, in a child process. signer "sign" for a run at SIGN
    (what the signer will check), "launch" past it (the sign-off record compared), "unsigned" for signoff none.
    Fail closed: an archive without the checks, or a checker that does not answer, is a problem."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with tarfile.open(fileobj=io.BytesIO(code), mode="r:gz") as t:
                t.extractall(tmp, filter="data")
        except (tarfile.TarError, OSError, ValueError) as e:
            return [f"the code to launch cannot be unpacked to check its preconditions ({type(e).__name__}: {e})"]
        if not os.path.isfile(os.path.join(tmp, "toolkit", "spec", "preconditions.py")):
            return ["the code to launch has no toolkit/spec/preconditions.py: its preconditions cannot be checked"]
        try:
            r = subprocess.run([sys.executable, "-I", "-c", _CHECKER, tmp, rel, signer,
                                json.dumps(list(TEST_ENGINE_REFS))], capture_output=True, text=True, timeout=900,
                               cwd=tmp, env={k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG",
                                                                                         "OMP_NUM_THREADS",
                                                                                         "OPENBLAS_NUM_THREADS")})
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"the preconditions check of the code to launch did not finish ({type(e).__name__})"]
        line = next((x for x in reversed(r.stdout.splitlines()) if x.startswith("PRECONDITIONS ")), None)
        if line is None:
            return [f"the preconditions check of the code to launch failed: {(r.stderr or r.stdout)[-400:]}"]
        try:
            got = json.loads(line[len("PRECONDITIONS "):])
        except ValueError:
            return ["the preconditions check of the code to launch gave an unreadable answer"]
        return [str(x) for x in got] if isinstance(got, list) else ["the preconditions check gave no list"]


SIGNOFF_RECORDS = ("decision_spec.signoff.json",)   # toolkit/spec/signoff.py


PRE_SIGN_STATES = ("SPEC",)          # agents/stage_machine.py EDGES: always before SIGN (a draft); EVIDENCE is before SIGN
# too (v2 autonomy 1B-ii) unless a sign-off record exists: a run signed under the earlier order (SIGN, then EVIDENCE)


def _signoff_record(code: bytes, rel: str) -> tuple:
    """(record, sha256 it holds for decision_spec.yaml) from the shipped sign-off records: the spec-only record
    (toolkit/spec/signoff.py `sha256`, or the stage machine's `spec_sha256`), else the brief's when it hashed the spec;
    (None, None) when there is none."""
    for f in SIGNOFF_RECORDS:
        try:
            doc = json.loads((_archive_member(code, f"{rel}/{f}") or b"null").decode())
        except (ValueError, UnicodeDecodeError):
            doc = None
        if not isinstance(doc, dict):
            continue
        s = (doc.get("sha256") or {}).get("decision_spec.yaml") if isinstance(doc.get("sha256"), dict) else None
        s = s or doc.get("spec_sha256")
        if s:
            return doc, s
    return None, None


def _signed_run_problems(code: bytes, rel: str) -> list[str]:
    """A run past SIGN: it ships a sign-off record of the spec it ships, and every registered precondition holds on
    the archive's bytes (toolkit/spec/preconditions.py: for the evidence dossier, the record's spec and dossier sha256,
    the sheet's, coverage on the engine chosen, recomputed; an empty or partial evidence binding is refused)."""
    import hashlib
    spec = _archive_member(code, f"{rel}/decision_spec.yaml")
    rec, signed = _signoff_record(code, rel)
    if spec is None or rec is None:
        return [f"{rel}: past SIGN with no decision_spec.yaml or no sign-off record of it in the code to launch"]
    if hashlib.sha256(spec).hexdigest() != signed:
        return [f"{rel}: decision_spec.yaml is not the one signed (edited after its sign-off): the owner amends and "
                "re-signs the spec before launch"]
    return [f"{rel}: {p}" for p in _preconditions(code, rel, "launch")]


def refuse_hidden_tree(tree: str, tracked: set[str] | None = None) -> None:
    """Coiled package sync uploads the launch tree as a wheel, untracked and ignored
    files included, so export-ignore does not keep eval material off the workers: a
    tree holding any on disk (eval_paths.tree_hidden_material) is refused. Move the
    material out of the tree (or launch from a clean clone) and launch again."""
    found = eval_paths.tree_hidden_material(tree, tracked)
    if found:
        raise SystemExit(f"{tree} holds hidden eval-suite material that package sync would upload "
                         f"({len(found)}: {', '.join(found[:5])}{', ...' if len(found) > 5 else ''}); "
                         "move it out of the launch tree: refusing")


def refuse_hidden(blob: bytes, tree: str) -> None:
    """Phase 50 α: eval answer keys, generators and rubrics never reach a worker.
    .gitattributes export-ignores them; a tree whose attributes are missing or wrong
    (an old branch, a deleted line) is refused here instead of shipped."""
    hidden = eval_paths.hidden_members(blob)
    if hidden:
        raise SystemExit(f"{tree}: the code tarball would ship eval-suite files agents must not see "
                         f"({len(hidden)}, e.g. {hidden[:3]}); check .gitattributes: refusing")


def main_flags(cfg) -> list:
    """main.py's arguments from the config: the declared pipeline, stated explicitly (never the
    default: it changed once, 2026-11-05), the launcher's name for metadata.json, then the flags."""
    return [f"--{cfg.pipeline}", "--launcher", f"run_cluster:{cfg.name}", *cfg.flags]


def pipeline_preflight(cfg, code: bytes, python: str = sys.executable) -> str:
    """The pipeline the shipped main.py runs with this config's arguments, asked of the shipped
    code itself (its scripts/lib/pipeline_id.py, standard library only, run isolated). Refuses a
    config that declares no pipeline, a tree that cannot state one (from before 2026-11-05, when
    main.py defaulted to v1 and nothing recorded it), and any disagreement with the declaration."""
    import tempfile
    if cfg.pipeline in pipeline_id.RETIRED:
        raise SystemExit(f"{cfg.name}: {pipeline_id.RETIRED_MESSAGE}: refusing")
    if cfg.pipeline not in pipeline_id.PIPELINES:
        raise SystemExit(f"{cfg.name}: the config declares no pipeline (set pipeline='v2'): refusing")
    want = "scripts/lib/pipeline_id.py"
    src = _archive_member(code, want)
    if src is None:
        raise SystemExit(f"{cfg.name}: the code to launch has no {want}, so it cannot state which pipeline main.py "
                         "runs (a tree from before 2026-11-05 runs v1 by default): refusing")
    main_src = (_archive_member(code, "main.py") or b"").decode("utf-8", "replace")
    if not all(k in main_src for k in ("pipeline_id.select(", '"--v1"', '"--v2"', '"--launcher"')):
        raise SystemExit(f"{cfg.name}: the shipped main.py does not take --v1/--v2/--launcher or does not choose its "
                         "pipeline through pipeline_id.select (a mixed or older tree): refusing")
    with tempfile.TemporaryDirectory(prefix="pipeline-preflight-") as td:
        f = os.path.join(td, "pipeline_id.py")
        open(f, "wb").write(src)
        r = subprocess.run([python, "-I", f, "which", "--", *main_flags(cfg)], capture_output=True, text=True,
                           timeout=60, cwd=td)
    got = r.stdout.strip()
    if r.returncode or got != cfg.pipeline:
        raise SystemExit(f"{cfg.name}: the config declares pipeline {cfg.pipeline}, but the shipped main.py would run "
                         f"{got or 'nothing'} ({(r.stderr or '').strip()[:200]}): refusing")
    return got


def launch_record(cfg, worker, commit, launch_id, cluster_id, code, pipeline) -> dict:
    """One label's entry in assignments.json (the grader reads it: scripts/eval_suite.py)."""
    return {"worker": worker, "commit": commit, "submitted": time.strftime("%F %T"), "launch_id": launch_id,
            "cluster_id": cluster_id, "pipeline": pipeline, "launcher": f"run_cluster:{cfg.name}",
            "eval_inputs_sha256": eval_paths.archive_sha256(code)}   # Phase 50 α: the inputs shipped


# ---- commands ---------------------------------------------------------------------------

INVOCATION_TAG = "ma-invocation"     # Coiled cluster tag: the invocation that created the cluster (round 4, P1)


def _lifetime(cfg, inv) -> dict:
    """Constructor arguments every cluster of a launch gets: its invocation tag, and Coiled's cluster_timeout."""
    return {"tags": {INVOCATION_TAG: inv}, **({"cluster_timeout": cfg.cluster_timeout} if cfg.cluster_timeout else {})}


def _construct(cfg, name, inv, ctor, lister=None):
    return CO.construct_cluster(cfg, name, inv, ctor, lister=lister)


def _task_key(label) -> str:
    return f"run-{label}-{int(time.time())}"


def _submit(cfg, client, label, worker, code, question, keys, launch_id, key=None):
    """Fire one run on its agent VM; returns (task key, future). The future is held through the acknowledgement: a
    run that finishes between two polls is seen through it (Codex P2-7 on #161)."""
    from distributed import fire_and_forget
    key = key or _task_key(label)       # callers record it in assignments.json first (Codex round 2, P2-2)
    fut = client.submit(ws.run_to_disk, label, code, question, main_flags(cfg), keys["ANTHROPIC_API_KEY"],
                        keys.get("OPENAI_API_KEY") if cfg.cross_model else None, list(cfg.labels),
                        cfg.storage_prefix, cfg.storage_account, launch_id, cfg.max_rounds, cfg.max_sessions,
                        workers=[worker], allow_other_workers=False, pure=False, key=key,
                        run_timeout_s=cfg.run_timeout_hours * 3600 if cfg.run_timeout_hours else None,
                        hold_s=cfg.hold_hours * 3600,
                        checkpoint_s=cfg.checkpoint_minutes * 60 if cfg.checkpoint_minutes else None)
    fire_and_forget(fut)
    return key, fut


EXIT_LAUNCHED, EXIT_FAILED, EXIT_SUBMITTED_LOST, EXIT_UNCONFIRMED, EXIT_INCOMPLETE = 0, 1, 3, 4, 5   # module doc
ACK_ATTEMPTS, ACK_DELAY_S = 36, 5.0      # about 3 minutes for the scheduler to list a run (F5 resume: 43 s)


def _invocation() -> str:
    """This invocation's token: the watchdog's (MA_LAUNCH_INVOCATION) or a new one. Printed first; every record this
    invocation writes carries it, so an exit 1 can be matched to a record of this attempt only (Codex P1-3)."""
    import secrets
    return os.environ.get("MA_LAUNCH_INVOCATION") or f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{secrets.token_hex(3)}"


class RecordOwned(RuntimeError):
    """clusters.json belongs to another invocation."""


def _atomic_json(path: str, obj) -> None:
    """Write through a unique temporary file in the same directory, then rename."""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_clusters(cfg, clusters: dict, replace: bool = False) -> None:
    """clusters.json, atomically. Only `launch` replaces another invocation's record (`replace`, under the config
    lock, once no cluster of the name is live); every other write refuses a record it does not own (Codex P2-4)."""
    if not replace and os.path.exists(cfg.clusters_file):
        try:
            owner = json.load(open(cfg.clusters_file)).get("invocation")
        except (OSError, ValueError, AttributeError):
            owner = None
        if owner != clusters.get("invocation"):
            raise RecordOwned(f"{cfg.clusters_file} belongs to invocation {owner}, not {clusters.get('invocation')}")
    _atomic_json(cfg.clusters_file, clusters)


@contextlib.contextmanager
def _config_lock(cfg):
    """One launch, keeper or relaunch of a config at a time on this machine (Codex P2-4)."""
    import fcntl
    os.makedirs(cfg.out_dir, exist_ok=True)
    f = open(os.path.join(cfg.out_dir, ".run_cluster.lock"), "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise SystemExit(f"{cfg.name}: another launch, keeper or relaunch of this config is in progress: refusing")
    try:
        yield
    finally:
        f.close()


def start_helpers(cfg, compute, cluster_id, clusters: dict, keep_overflow=None, keep_keeper=False) -> None:
    """The overflow pool and the keeper for run cluster `cluster_id`, each recorded in clusters.json right after its
    constructor, before any step of its start-up that can fail (Codex P2-5). `keep_overflow` (an id) and
    `keep_keeper`: helpers of this launch that `keeper` found healthy (Codex round 2, P2-1)."""
    def record(role, name, cid):
        clusters[role] = {"name": name, "id": cid}
        write_clusters(cfg, clusters)
    overflow = None
    if cfg.keeper and cfg.overflow_max:
        overflow = (_overflow_info(cfg, keep_overflow) if keep_overflow
                    else start_overflow(cfg, compute, record=record, inv=clusters.get("invocation")))
        overflow["primary_vm_types"] = list(compute.WORKER_VM_TYPES)
    if cfg.keeper and not keep_keeper:
        time.sleep(10)
        kc = start_keeper(cfg, adapt_options(cfg, compute.ADAPT_OPTIONS), cluster_id, overflow, record=record,
                          inv=clusters.get("invocation"))
        time.sleep(30)
        confirm_keeper(cfg.keeper_name, kc, run_alive=lambda: is_running(cfg.name), run_id=cluster_id,
                       overflow_id=(overflow or {}).get("id"))


def _outcome(sent: list, acked: list, e: BaseException) -> tuple[int, str]:
    """The exit code after a submission: never EXIT_FAILED (module doc)."""
    if acked != sent:
        return EXIT_UNCONFIRMED, "submitted_unconfirmed"
    if isinstance(e, CONNECTION_ERRORS):
        return EXIT_SUBMITTED_LOST, "submitted_connection_lost"
    return EXIT_INCOMPLETE, "submitted_launch_incomplete"


def _handover(cfg, clusters: dict, e: BaseException, sent: list, acked: list) -> int:
    """After a submission, whatever went wrong: the safe exit code is chosen first; recording it and saying how to
    attach later are best effort, interrupts included, so nothing here can turn into a failed launch (Codex P1-1)."""
    code, status = _outcome(sent, acked, e)
    try:
        clusters.update(status=status, detail=f"{type(e).__name__}: {str(e)[:300]}", submitted=sent, listed=acked,
                        not_submitted=[x for x in cfg.labels if x not in sent])
        me = "python cloud/run_cluster.py <config> "
        print(f"\n{cfg.name}: {status} (exit {code}) after {type(e).__name__}: {str(e)[:200]}\n"
              f"  submitted {sent or '-'}; listed by the scheduler {acked or '-'}.\n  The run keeps going without "
              f"this process: do not shut down. Attach later with `{me}status`.", flush=True)
        if clusters["not_submitted"]:
            print(f"  Not submitted: {clusters['not_submitted']}; `{me}relaunch <label>` once `status` shows the "
                  "cluster (their assignments are recorded).", flush=True)
        if cfg.keeper and "keeper" not in clusters:
            print(f"  No keeper yet (the pool stays at its start size): start it with `{me}keeper`.", flush=True)
        write_clusters(cfg, clusters)
        state = "live" if live_id(cfg.name) == clusters["run"]["id"] else "not live"   # the Coiled API, not the scheduler
        print(f"  run cluster {clusters['run']['id']} is {state}.", flush=True)
    except BaseException:  # noqa: BLE001 — reporting only; the code above is the answer
        pass
    return code


def launch(cfg) -> int:
    inv = _invocation()
    print(f"invocation {inv}", flush=True)
    with _config_lock(cfg):
        return _launch_locked(cfg, inv)


def acquire_flag_problems(cfg) -> list[str]:
    """AQ20 (docs/design/acquire.md §§8, 10): refuse `--acquire` on eval-suite configs or on any question
    file marked `private: true`."""
    flags = [str(f) for f in (getattr(cfg, "flags", None) or ())]
    if not any(f == "--acquire" or f.startswith("--acquire=") for f in flags):
        return []
    probs: list[str] = []
    qfile = str(getattr(cfg, "question_file", None) or "").replace("\\", "/")
    spref = str(getattr(cfg, "storage_prefix", None) or "").replace("\\", "/")
    reqs = [str(x).replace("\\", "/") for x in (getattr(cfg, "required_files", None) or ())]
    is_eval = (
        spref.startswith("runs/evals/")
        or any("data/evals/" in r for r in reqs)
        or qfile.startswith("evals/")
        or "/evals/" in qfile
    )
    if is_eval:
        probs.append("AQ20: --acquire is refused on evaluation-suite configs")
    if qfile and os.path.isfile(qfile):
        try:
            qtxt = open(qfile, encoding="utf-8", errors="replace").read()
            if re.search(r"(?mi)^\s*private\s*:\s*true\s*$", qtxt):
                probs.append(f"AQ20: --acquire is refused for question marked `private: true` ({qfile})")
        except OSError:
            pass
    return probs


def _launch_locked(cfg, inv) -> int:
    import coiled
    acq_probs = acquire_flag_problems(cfg)
    if acq_probs:
        raise SystemExit("refusing: " + "; ".join(acq_probs))
    if is_running(cfg.name):
        raise SystemExit(f"{cfg.name} is already running: use status / relaunch")
    question = open(cfg.question_file).read().strip()
    code, commit = code_tarball(cfg)
    pipeline = pipeline_preflight(cfg, code)      # before anything is created or any key is read
    stale = stale_sheet_problems(code, cfg.flags, pipeline)   # a run that would stop at SIGN once paid for
    if stale:
        raise SystemExit("refusing: " + "; ".join(stale))
    print(f"pipeline {pipeline} (declared by the config, confirmed by the shipped main.py) @ {commit}", flush=True)
    compute = _compute(cfg)
    vm = vm_request(cfg, compute)                 # every VM type priced, before the budget reserves anything
    budget_guard(cfg, inv=inv)                    # before any key is read or cluster created
    try:
        keys = api_keys()                         # before any cluster: a missing or blank key refuses here
    except SystemExit:
        _release(cfg, inv)                        # the budget check above reserved the cap for this invocation
        raise
    n0 = len(cfg.labels) + cfg.compute_start
    cluster = _construct(cfg, cfg.name, inv, lambda: coiled.Cluster(   # this invocation's, or refused unrecorded
        name=cfg.name, workspace=WORKSPACE, n_workers=n0, worker_vm_types=vm["run"]["worker_vm_types"],
        scheduler_vm_types=vm["run"]["scheduler_vm_types"], worker_disk_size=100, idle_timeout=cfg.idle_timeout, worker_options=compute.WORKER_OPTIONS,
        environ={"MA_STORAGE_ACCOUNT": cfg.storage_account, "MA_STORAGE_PREFIX": cfg.storage_prefix,
                 **cfg.compute_env},
        spot_policy="on-demand", shutdown_on_close=False, **_lifetime(cfg, inv)))
    launch_id = time.strftime("%Y%m%d-%H%M%S")
    # S1-8: recorded once the run cluster exists, before any submission or acknowledgement wait; this invocation's
    # record replaces the previous launch's (no cluster of the name was live, and the config lock is held)
    clusters = {"run": {"name": cfg.name, "id": cluster.cluster_id}, "launch_id": launch_id, "invocation": inv,
                "launched_utc": time.strftime("%FT%TZ", time.gmtime()), "status": "submitting"}
    write_clusters(cfg, clusters, replace=True)
    sent, acked = [], []
    try:
        client = cluster.get_client()
        client.wait_for_workers(n0, timeout=1800)
        client.upload_file(os.path.join(HERE, "worker_side.py"))
        box = [client]

        def run_client():   # a client closed since the upload is replaced, and the replacement kept
            box[0] = ws.live_client(cluster, box[0])
            return box[0]
        workers = sorted(ws.scheduler_workers(run_client, min_workers=n0))[:len(cfg.labels)]
        client = box[0]
        compute.register_compute_tags(client, exclude=workers)   # the agent VMs never take compute tasks
        # Codex P2-6: every label's assignment before the first submission, so `relaunch` works for any of them
        assign = {label: dict(launch_record(cfg, w, commit, launch_id, cluster.cluster_id, code, pipeline),
                              submission="pending", invocation=inv) for label, w in zip(cfg.labels, workers)}
        _atomic_json(cfg.state_file, assign)
        for label, w in zip(cfg.labels, workers):
            key = _task_key(label)
            assign[label].update(submission="sent", submitted=time.strftime("%F %T"), task_key=key)
            _atomic_json(cfg.state_file, assign)
            sent.append(label)            # from the submit call on, the run may be going
            key, fut = _submit(cfg, client, label, w, code, question, keys, launch_id, key=key)
            print(f"fired {label} -> {w} @ {commit} (pipeline {pipeline})", flush=True)
            why = []
            if not wait_listed(client, key, fut, why=why):
                raise TimeoutError(f"the scheduler did not list {key} within {ACK_ATTEMPTS} polls")
            acked.append(label)
            assign[label]["submission"] = "listed"
            _atomic_json(cfg.state_file, assign)
            print(f"listed {label} ({key}) by the scheduler ({why[0]})", flush=True)
        clusters["status"] = "submitted"
        write_clusters(cfg, clusters)
        start_helpers(cfg, compute, cluster.cluster_id, clusters)
    except BaseException as e:  # noqa: BLE001 — after a submission nothing here may read as a failed launch
        if sent:
            return _handover(cfg, clusters, e, sent, acked)
        try:
            clusters.update(status="failed", detail=f"{type(e).__name__}: {str(e)[:300]}")
            write_clusters(cfg, clusters)
        except BaseException:  # noqa: BLE001 — the original error is the one to raise
            pass
        raise
    try:   # confirmed: recording and reporting it are best effort
        clusters["status"] = "launched"
        write_clusters(cfg, clusters)
        print(f"launched {cfg.name}: results go to outputs/{cfg.storage_prefix}/; the cluster stops itself "
              "once every run is stored. Safe to disconnect.", flush=True)
    except BaseException:  # noqa: BLE001
        pass
    return EXIT_LAUNCHED


def _stop_id(name, cid) -> None:
    """Stop one cluster by id (never an attach by name)."""
    CO.stop_cluster_by_id(name, cid, check_live=False)


def _reconcile(cfg, clusters: dict, role: str, name: str):
    """The id of the live `name` helper when it is the one clusters.json records for this launch (kept), else None.
    A live helper with another id belongs to an earlier launch: it is stopped by that id first (Codex round 2, P2-1)."""
    rec, live = (clusters.get(role) or {}).get("id"), live_id(name)
    if live is not None and live == rec:
        print(f"{name} ({live}): this launch's, live: kept")
        return live
    if live is not None:
        print(f"{name} ({live}): live but not this launch's ({rec}): an earlier launch's helper")
        _stop_id(name, live)
    clusters.pop(role, None)
    return None


def _bounded(fn, *a, timeout_s: float = 60.0):
    """fn(*a) in a daemon thread; TimeoutError when it has not returned within `timeout_s`."""
    import threading
    box = {}

    def run():
        try:
            box["v"] = fn(*a)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} did not return within {timeout_s} s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def keeper_later(cfg) -> int:
    """`keeper`: the overflow pool and keeper of a launch that ended before starting them (exit 3 or 5), for the run
    cluster clusters.json records, and only while that cluster is the live one of its name."""
    with _config_lock(cfg):
        clusters = json.load(open(cfg.clusters_file))
        cid = (clusters.get("run") or {}).get("id")
        live = live_id(cfg.name)
        if cid is None or live != cid:
            raise SystemExit(f"{cfg.name}: clusters.json records run cluster {cid}, the live one is {live}: refusing")
        keep_o = _reconcile(cfg, clusters, "overflow", cfg.overflow_name) if cfg.keeper and cfg.overflow_max else None
        keep_k = _reconcile(cfg, clusters, "keeper", cfg.keeper_name) if cfg.keeper else None
        if keep_k is not None:            # this launch's keeper: kept only while it holds this run and pool
            try:
                if cfg.overflow_max and keep_o is None:
                    raise SystemExit("its overflow pool is gone: a new pool needs a new keeper")
                confirm_keeper(cfg.keeper_name, ws.attach_existing(cfg.keeper_name, WORKSPACE, keep_k),
                               run_alive=lambda: is_running(cfg.name), run_id=cid, overflow_id=keep_o)
            except SystemExit as e:
                print(f"{cfg.keeper_name} ({keep_k}): this launch's keeper is not holding the pool ({str(e)[:120]}); "
                      "stopping it by id and starting a new one")
                _stop_id(cfg.keeper_name, keep_k)
                clusters.pop("keeper", None)
                keep_k = None
        if cfg.keeper and ((cfg.overflow_max and keep_o is None) or keep_k is None):
            vm_request(cfg)                   # every helper VM type priced, before the reservation
            budget_guard(cfg, inv=clusters.get("invocation"))   # a new overflow pool or keeper is paid: reserved then
        start_helpers(cfg, _compute(cfg), cid, clusters, keep_overflow=keep_o, keep_keeper=keep_k is not None)
        clusters["status"] = "launched"
        write_clusters(cfg, clusters)
    print(f"{cfg.name}: keeper started for run cluster {cid}")
    return EXIT_LAUNCHED


def reuse_check(cfg, run_dir: str, code: bytes | None = None, python: str = sys.executable,
                expect_all: bool = False) -> dict:
    """Would this launch reuse the stored engine rows of `run_dir`? Asked of the code it would ship (the exact
    tarball, extracted to a temporary directory), through that tree's `compute.py reuse-check`: local only."""
    import tempfile
    code = code if code is not None else code_tarball(cfg)[0]
    with tempfile.TemporaryDirectory(prefix="reuse-check-") as td:
        with tarfile.open(fileobj=io.BytesIO(code), mode="r:gz") as t:
            t.extractall(td, filter="data")
        r = subprocess.run([python, os.path.join(td, "scripts", "compute.py"), "reuse-check",
                            os.path.abspath(run_dir)] + (["--expect-all"] if expect_all else []),
                           capture_output=True, text=True, timeout=1800, cwd=td)
    lines = r.stdout.strip().splitlines()
    try:
        got = json.loads(lines[-1])
    except (ValueError, IndexError):
        raise SystemExit(f"reuse-check gave no answer (rc {r.returncode}): {(r.stderr or '').strip()[-300:]}")
    return {**got, "rc": r.returncode}


def _fresh(name):
    """Before creating a helper cluster: an earlier launch's (live or stopping) is never
    reused. A live one is shut down; a stopping one is waited out."""
    for _ in range(40):
        act = keeper_wait(cluster_states(name))
        if act == "start":
            return
        if act == "stop":
            cid = live_id(name)
            if cid is not None:
                _stop_id(name, cid)
            print(f"{name}: an earlier launch's cluster was running; shut it down")
        time.sleep(15)
    raise SystemExit(f"{name} did not stop; start it later (the pool keeps its floor)")


def start_overflow(cfg, compute, record=None, inv=None) -> dict:
    """The quota-fallback pool: a scheduler-only cluster of other VM families, created
    here (so it gets this launch's software environment) and scaled by the keeper. Its
    idle timeout outlasts the keeper; the keeper deletes it on exit."""
    import coiled
    vm = vm_request(cfg)["overflow"]
    _fresh(cfg.overflow_name)
    oc = _construct(cfg, cfg.overflow_name, inv, lambda: coiled.Cluster(
        name=cfg.overflow_name, workspace=WORKSPACE, n_workers=0, worker_vm_types=vm["worker_vm_types"],
        scheduler_vm_types=vm["scheduler_vm_types"], worker_disk_size=100, idle_timeout=f"{int(cfg.keeper_hours * 60) + 30} minutes",
        worker_options=compute.WORKER_OPTIONS,
        environ={"MA_STORAGE_ACCOUNT": cfg.storage_account, "MA_STORAGE_PREFIX": cfg.storage_prefix, **cfg.compute_env},
        spot_policy="on-demand", shutdown_on_close=False, wait_for_workers=False,
        **_lifetime(cfg, inv)))
    if record:
        record("overflow", cfg.overflow_name, oc.cluster_id)     # before the upload, which can fail (Codex P2-5)
    oc.get_client().upload_file(os.path.join(HERE, "worker_side.py"))
    print(f"{cfg.overflow_name}: ready at 0 workers ({', '.join(cfg.overflow_vm_types)}; up to {cfg.overflow_max})")
    return _overflow_info(cfg, oc.cluster_id)


def _overflow_info(cfg, oid) -> dict:
    """What the keeper needs to scale the overflow pool `oid`."""
    return {"name": cfg.overflow_name, "id": oid, "max": cfg.overflow_max,
            "vm_types": list(cfg.overflow_vm_types), "hold_s": seconds(cfg.overflow_hold),
            "scale_down_s": seconds(cfg.scale_down_after), "hours": cfg.keeper_hours}


def start_keeper(cfg, adapt_opts, cluster_id, overflow=None, record=None, inv=None):
    import coiled
    import dask
    from distributed import fire_and_forget
    lo, hi = len(cfg.labels) + cfg.compute_start, len(cfg.labels) + cfg.compute_max
    # An earlier launch's keeper (still polling, or stopping) must not be reused:
    # its task would hold the old cluster id and exit, leaving this pool unscaled.
    vm = vm_request(cfg)["keeper"]
    _fresh(cfg.keeper_name)
    kc = _construct(cfg, cfg.keeper_name, inv, lambda: coiled.Cluster(
        name=cfg.keeper_name, workspace=WORKSPACE, n_workers=1, worker_vm_types=vm["worker_vm_types"],
        scheduler_vm_types=vm["scheduler_vm_types"], spot_policy="on-demand", idle_timeout="30 minutes", shutdown_on_close=False, **_lifetime(cfg, inv)))
    if record:
        record("keeper", cfg.keeper_name, kc.cluster_id)         # before anything that can fail (Codex P2-5)
    c = kc.get_client()
    c.wait_for_workers(1, timeout=1200)
    c.upload_file(os.path.join(HERE, "worker_side.py"))
    # The Coiled token goes in as a task argument (memory only, never an env var or a file).
    fut = c.submit(ws.keeper_task, dask.config.get("coiled.token"), cfg.name, WORKSPACE, lo, hi,
                   cfg.keeper_hours, dict(adapt_opts), cluster_id, overflow, pure=False,
                   key=f"keeper-{cfg.name}-{cluster_id}")   # a new key per run cluster: never an old future
    fire_and_forget(fut)
    print(f"keeper {cfg.keeper_name} submitted (pool {lo}..{hi})", flush=True)
    return kc


KEEPER_TICK = re.compile(r"\bworkers=\d+ \(overflow \d+\)")   # _keeper_loop's tick line
KEEPER_FRESH_S = 180.0      # three ticks: a status older than this is not a heartbeat


def keeper_fresh(h: dict, run_id=None, overflow_id=None, max_age_s: float = KEEPER_FRESH_S) -> tuple[bool, str]:
    """(healthy, why) from worker_side.keeper_health: a tick line with no error or exit after it, a status the
    keeper wrote at most `max_age_s` ago by its own clock, and, when `run_id` is given, a status for that run
    cluster and overflow pool (`overflow_id`, None for none). Codex round 3 on #161: any old tick is not enough."""
    lines = str((h or {}).get("log") or "").strip().splitlines()
    ticks = [i for i, ln in enumerate(lines) if KEEPER_TICK.search(ln)]
    if not ticks:
        return False, (lines[-1:] or ["(empty log)"])[0][:200]
    bad = [ln for ln in lines[ticks[-1] + 1:] if "keeper error" in ln or "exiting" in ln]
    if bad:
        return False, f"after its last tick: {bad[-1][:160]}"
    st = (h or {}).get("status")
    if not isinstance(st, dict) or not st.get("updated"):
        return False, "no status file"
    age = float(h.get("now") or 0) - float(st["updated"])
    if age > max_age_s:
        return False, f"its status is {age:.0f} s old (limit {max_age_s:.0f} s)"
    if run_id is not None:
        if st.get("cluster_id") != run_id:
            return False, f"it holds run cluster {st.get('cluster_id')}, not {run_id}"
        if (st.get("overflow") or {}).get("id") != overflow_id:
            return False, f"it drives overflow pool {(st.get('overflow') or {}).get('id')}, not {overflow_id}"
    return True, lines[ticks[-1]][:200]


def _run_stopped(run_alive) -> bool:
    try:
        return run_alive is not None and not run_alive()
    except Exception:  # noqa: BLE001 — unknown run state: keep waiting, fail as before
        return False


def confirm_keeper(keeper_name, kc, attempts=8, delay_s=5.0, max_delay_s=60.0, sleep=None, run_alive=None,
                   run_id=None, overflow_id=None):
    """Wait (bounded, doubling backoff) for the keeper's first tick line: it is written only
    after adapt() and a worker-table read, so it is the evidence the pool is held; a readable
    log alone ("(no log yet)", "keeper error ...; retrying") is not. The keeper cluster's
    client can be closed by now (a failed reconnect closes it and empties its scheduler
    identity: v2blind1's `KeyError: 'workers'`), so a closed client is replaced. Past the
    bound the launch fails with a message saying what to check; the run is already fired
    and clusters.json already names the keeper."""
    sleep = sleep or time.sleep
    box = [None]

    def client():
        box[0] = ws.live_client(kc, box[0])
        return box[0]
    delay, last = delay_s, ["(not read)"]
    for i in range(max(1, int(attempts))):
        try:
            w = sorted(ws.scheduler_workers(client, attempts=3, delay_s=delay_s, min_workers=1, sleep=sleep,
                                            log=print))[0]
            good, why = keeper_fresh(box[0].run(ws.keeper_health, workers=[w])[w], run_id, overflow_id)
            if good:
                print([why], f"(keeper {keeper_name})")
                return [why]
            last = [why]
        except Exception as e:  # noqa: BLE001 — retried, then reported
            last = [f"{type(e).__name__}: {str(e)[:200]}"]
        # A run that ended (or paused) before the keeper's first tick stops its own cluster, and the keeper then
        # exits by design: nothing is left to hold, so the wait ends at once (smoke case A: about 6 minutes of
        # retries after the run had stopped) and this is reported, not a launch failure.
        if _run_stopped(run_alive):
            break
        if i < attempts - 1:
            sleep(delay)
            delay = min(max_delay_s, delay * 2)
    if _run_stopped(run_alive):
        msg = (f"keeper {keeper_name}: not confirmed, but the run cluster has already stopped (the run ended or paused "
               "before the keeper's first tick), so the keeper exited by design; check `status` before relaunching")
        print(msg)
        return [msg]
    raise SystemExit(f"keeper {keeper_name} (cluster {kc.cluster_id}) was submitted but not confirmed holding "
                     f"the pool after {attempts} tries (last: {last[0][:200]}). The run is fired and the clusters "
                     f"file names the keeper; check `status` and `keeper-log` now (without a keeper the pool "
                     f"stays at its start size).")


def keeper_lines(st: dict | None, now: float | None = None) -> list[str]:
    """The keeper's status file as `status` prints it: pool, active VM family, quota refusals."""
    if not st:
        return ["keeper status: none yet (the keeper writes it every minute)"]
    now = time.time() if now is None else now
    age = round(now - float(st.get("updated") or now))
    ovf = st.get("overflow") or {}
    out = [f"keeper: {st.get('workers', '?')} workers ({st.get('overflow_workers', 0)} overflow), "
           f"target {st.get('target', '?')}, pool {st.get('pool')}, updated {age}s ago",
           f"  active: {st.get('active', '?')}; primary {st.get('primary_vm_types') or '(launch list)'}"
           + (f"; overflow {ovf.get('name')} {ovf.get('vm_types')} up to {ovf.get('max')}" if ovf.get("id") else
              "; no overflow pool")]
    n, last = st.get("refusal_count", 0), (st.get("refusals") or [None])[-1]
    if n:
        out.append(f"  quota refusals: {n}; last {round((now - last['at']) / 60)} min ago: {last['family']} "
                   f"{last.get('usage')}/{last.get('limit')} cores (+{last.get('required')} needed)")
    else:
        out.append("  quota refusals: none seen")
    if st.get("refusal_scan", "ok") != "ok":
        out.append(f"  refusal scan: {st['refusal_scan']}")
    out += [f"  error: {e}" for e in (st.get("errors") or [])[-3:]]
    return out


def recorded_labels(cfg, assign: dict) -> list:
    """The labels `collect` and `status` act on: every label the launch recorded in assignments.json, merged with
    cfg.labels (2026-11-22 baseline hb1: cfg.labels comes from EVAL_REPS at collect time, and with it unset R2 was
    skipped without a message). A difference between the two is named; a cfg label with no launch record is
    skipped, since there is nothing to collect for it."""
    extra = sorted(k for k in assign if k not in cfg.labels)
    unlaunched = [k for k in cfg.labels if k not in assign]
    if extra:
        print(f"WARNING: {os.path.basename(cfg.state_file)} records label(s) {extra} that this config's labels "
              f"{list(cfg.labels)} do not name (EVAL_REPS?); they are included")
    if unlaunched:
        print(f"WARNING: label(s) {unlaunched} of this config have no launch record in "
              f"{os.path.basename(cfg.state_file)}; skipped")
    return [k for k in cfg.labels if k in assign] + extra


def status(cfg):
    run, keep = is_running(cfg.name), is_running(cfg.keeper_name)
    ovf = is_running(cfg.overflow_name)
    print(f"cluster {cfg.name}: {'running' if run else 'stopped'}; keeper: {'running' if keep else 'stopped'}; "
          f"overflow: {'running' if ovf else 'stopped'}")
    if keep:
        try:
            kc = existing_client(cfg.keeper_name)
            w = sorted(ws.scheduler_workers(lambda: kc, attempts=3, min_workers=1))[0]
            print("\n".join(keeper_lines(kc.run(ws.keeper_status, workers=[w])[w])))
        except Exception as e:  # noqa: BLE001 — a keeper from before Phase 42 β has no status file
            print(f"keeper status unavailable: {type(e).__name__}: {str(e)[:120]}")
    if not run:
        return
    assign = json.load(open(cfg.state_file))
    client = existing_client(cfg.name)
    for label in recorded_labels(cfg, assign):
        try:       # 1E-ii: one label whose VM cannot be read no longer hides every other label from the watch
            m = _on_worker(client, ws.fetch_meta, label, worker=assign[label]["worker"])
        except Exception as e:  # noqa: BLE001
            print(f"{label}: unknown ({type(e).__name__}: {str(e)[:120]})")
            continue
        if m["state"] != "done":
            beat = f", phase {m['phase']}, heartbeat {m.get('heartbeat_age_s')} s ago" if m.get("phase") else ""
            print(f"{label}: {m['state']} (log {m.get('log_bytes', 0)} bytes{beat})")
        else:
            r = m["meta"]
            print(f"{label}: done exit={r['exit_code']} {r['duration_s']}s run_dir={r['run_dir']} "
                  f"stored={r.get('run_stored')}")


def _run_identity_on_disk(dest: str) -> dict:
    """The collected run's own identity (its metadata.json), "unknown" for anything missing."""
    import glob
    for mp in sorted(glob.glob(os.path.join(dest, "runs", "*", "metadata.json"))):
        try:
            meta = json.load(open(mp))
        except (OSError, ValueError):
            meta = {}
        p, c = pipeline_id.label(meta)
        return {"pipeline": p, "commit": c, "launcher": (meta.get("launcher") if isinstance(meta, dict) else None)
                or pipeline_id.UNKNOWN}
    return {"pipeline": pipeline_id.UNKNOWN, "commit": pipeline_id.UNKNOWN, "launcher": pipeline_id.UNKNOWN}


def _identity_note(label, assigned: dict | None, run_ident: dict) -> None:
    a = assigned or {}
    if a.get("pipeline") and run_ident["pipeline"] != a["pipeline"]:
        print(f"WARNING {label}: launched as pipeline {a['pipeline']}, the run records {run_ident['pipeline']}")
    if a.get("commit") and not pipeline_id.same_commit(a["commit"], run_ident["commit"]):
        print(f"WARNING {label}: launched at {a['commit']}, the run records commit {run_ident['commit']}")


class StoredRunRefused(RuntimeError):
    """A stored run that is not provably this label's: never collected for it (baseline hb3 §5.1)."""


def _tar_identity(t) -> tuple:
    """(run_label, launch_id) the stored run's own metadata.json records (<run>/metadata.json); None for each absent."""
    for m in t.getmembers():
        if m.isfile() and m.name.count("/") == 1 and m.name.endswith("/metadata.json"):
            try:
                meta = json.load(t.extractfile(m))
            except (ValueError, OSError, tarfile.TarError):
                return None, None
            meta = meta if isinstance(meta, dict) else {}
            return meta.get("run_label"), meta.get("launch_id")
    return None, None


def _sharers(cfg, store, label, blob, siblings: dict | None) -> list:
    """The launch's other labels whose result records name the same stored run (`siblings`: label -> launch id). A
    record not stored yet names nothing; one that cannot be read raises (Codex r1 on #374: never collected on a doubt)."""
    import tempfile
    out = []
    for other, lid in sorted((siblings or {}).items()):
        if other == label or not lid:
            continue
        with tempfile.TemporaryDirectory() as td:
            try:
                store.download(ws.record_blob(cfg.storage_prefix, lid, other), os.path.join(td, "r.json"),
                               cfg.storage_account)
            except Exception as e:  # noqa: BLE001 -- absent: that label has stored nothing yet
                if isinstance(e, (FileNotFoundError, KeyError)) or type(e).__name__ == "ResourceNotFoundError":
                    continue
                raise
            rec = json.load(open(os.path.join(td, "r.json")))
        if isinstance(rec, dict) and rec.get("run_stored") == blob:
            out.append(other)
    return out


def collect_from_storage(cfg, store, label, launch_id, assigned: dict | None = None, siblings: dict | None = None):
    """One label from storage: this launch's result record, then the run directory it names.
    result.json records the launch's pipeline and commit (`assigned`, from assignments.json)
    and the run's own (`run_identity`, from its metadata.json). The stored run must be this label's: one whose
    metadata.json records another label, or (recording none, as runs stored before baseline hb3 do) whose blob another
    label's record of the launch (`siblings`: label -> launch id) also names, is refused, and nothing is extracted."""
    dest = os.path.join(cfg.out_dir, label)
    rj = os.path.join(cfg.out_dir, f"{label}.result.json")
    store.download(ws.record_blob(cfg.storage_prefix, launch_id, label), rj, cfg.storage_account)
    meta = json.load(open(rj))
    if not meta.get("run_stored"):
        raise SystemExit(f"{label}: the result record names no stored run")
    tgz = os.path.join(cfg.out_dir, f"{label}.run.tar.gz")
    store.download(meta["run_stored"], tgz, cfg.storage_account)
    with tarfile.open(tgz, mode="r:gz") as t:
        own, own_launch = _tar_identity(t)
    # a run that records this label is this label's (a record of another label naming it is that record's error)
    shared = [] if own == label else _sharers(cfg, store, label, meta["run_stored"], siblings)
    why = (f"records label {own}" if own and own != label else
           f"records launch {own_launch}, not {launch_id}" if own_launch and own_launch != launch_id else
           f"is named by the records of {', '.join([label, *shared])} alike: whose run it holds is unknown"
           if shared else None)
    if why:
        os.remove(tgz)
        raise StoredRunRefused(f"{label}: the stored run {meta['run_stored']} {why}; not collected")
    os.makedirs(os.path.join(dest, "runs"), exist_ok=True)
    with tarfile.open(tgz, mode="r:gz") as t:
        t.extractall(os.path.join(dest, "runs"), filter="data")
    meta["launch_id"] = launch_id
    meta["run_identity"] = _run_identity_on_disk(dest)
    for k in ("pipeline", "commit", "launcher"):
        meta[k] = (assigned or {}).get(k) or pipeline_id.UNKNOWN
    _identity_note(label, assigned, meta["run_identity"])
    json.dump(meta, open(os.path.join(dest, "result.json"), "w"), indent=1)
    return meta


def _download_json(store, blob, account):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        try:
            store.download(blob, os.path.join(td, "r.json"), account)
            rec = json.load(open(os.path.join(td, "r.json")))
        except Exception:  # noqa: BLE001 -- none stored (or unreadable)
            return None
    return rec if isinstance(rec, dict) else None


def checkpoint_record(cfg, store, label, launch_id) -> dict | None:
    """The pointer of `label`'s checkpoints in launch `launch_id` (the latest complete generation's record), or
    None."""
    return _download_json(store, ws.checkpoint_pointer(cfg.storage_prefix, launch_id, label), cfg.storage_account)


def fetch_checkpoint(cfg, store, label) -> dict:
    """`checkpoint`: the latest checkpoint of the latest launch (assignments.json) into <out_dir>/<label>/checkpoint/
    with its record (checkpoint.json). The pointer names the latest generation; a generation whose tarball does not
    match its record's sha256 is removed and the previous complete one is tried; none: refused. An earlier copy is
    moved aside."""
    launch_id = json.load(open(cfg.state_file))[label]["launch_id"]
    ptr = checkpoint_record(cfg, store, label, launch_id)
    if ptr is None:     # Codex round 4: no pointer (lost before the first): the newest complete generation
        stem = ws.checkpoint_pointer(cfg.storage_prefix, launch_id, label)[:-len(".json")]
        names = {b["name"] for b in store.list_blobs(stem, cfg.storage_account)}
        k, _ = ws.newest_complete(store, cfg.storage_prefix, launch_id, label, names, cfg.storage_account,
                                  lambda b: _download_json(store, b, cfg.storage_account))
        if k is None:
            raise SystemExit(f"{label}: no checkpoint stored for launch {launch_id}")
        print(f"{label}: no pointer; generation {k} is the newest complete one")
        ptr = {"generation": k, "previous": None}
    base = os.path.join(cfg.out_dir, label)
    os.makedirs(base, exist_ok=True)
    tgz, why = os.path.join(base, "checkpoint.tar.gz"), []
    for g in [x for x in (ptr.get("generation"), ptr.get("previous")) if x]:
        tar_b, json_b = ws.checkpoint_blobs(cfg.storage_prefix, launch_id, label, g)
        rec = _download_json(store, json_b, cfg.storage_account)
        try:
            store.download(tar_b, tgz, cfg.storage_account)
        except Exception as e:  # noqa: BLE001 -- try the previous generation
            why.append(f"generation {g}: {type(e).__name__}")
            continue
        got = ws_sha256(tgz)
        if rec is None or got != rec.get("sha256"):
            os.remove(tgz)
            why.append(f"generation {g}: tarball sha256 {got[:12]} is not its record's "
                       f"{str((rec or {}).get('sha256'))[:12]}")
            continue
        dest = os.path.join(base, "checkpoint")
        if os.path.exists(dest):
            os.rename(dest, f"{dest}.before_{int(time.time())}")
        with tarfile.open(tgz, mode="r:gz") as t:
            t.extractall(dest, filter="data")
        json.dump(rec, open(os.path.join(dest, "checkpoint.json"), "w"), indent=1)
        import copied_run       # F6 run 3: its batch records describe the VM's process, not one running here
        rroot = _checkpoint_run_root(dest, rec.get("run_dir"))
        if rroot is not None:
            copied_run.mark(rroot, f"run_cluster checkpoint {label} generation {g}", rec.get("time_utc"))
        else:
            print(f"{label}: WARNING the record's run_dir {rec.get('run_dir')!r} is not one directory of the extraction: "
                  "its batch records are not marked as copied")
        note = "" if g == ptr.get("generation") else f" (the previous generation: {'; '.join(why)})"
        print(f"{label}: checkpoint generation {g} of {rec.get('time_utc')} (state {rec.get('run_state')}, "
              f"{rec.get('cached_batches')} cached batches, {rec.get('bytes')} bytes, sha256 verified){note} -> "
              f"{dest}/runs/{rec.get('run_dir')}")
        return rec
    raise SystemExit(f"{label}: no checkpoint generation matches its record's sha256 ({'; '.join(why)}); run "
                     "`checkpoint` again later")


def _checkpoint_run_root(dest, name):
    """The extracted run dir a checkpoint record names: one plain name of a real directory (not a link) directly under
    <dest>/runs/, or None (Codex review of #256 P1-4: the record is not trusted as a path)."""
    if not isinstance(name, str) or not name or name in (".", "..") or os.path.basename(name) != name \
            or os.sep in name or (os.altsep and os.altsep in name):
        return None
    p = os.path.join(dest, "runs", name)
    return p if os.path.isdir(p) and not os.path.islink(p) and not os.path.islink(os.path.join(dest, "runs")) else None


def ws_sha256(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(2**20), b""):
            h.update(b)
    return h.hexdigest()


def _connect_recorded(cfg, cid, lister=None, security_fn=None, client_cls=None):
    """A plain distributed.Client on the run cluster `cid` (recorded in clusters.json), only while the listing shows
    that id ready or running; never by name (as ack_probe)."""
    if lister is None:
        import coiled
        lister = lambda: coiled.list_clusters(workspace=WORKSPACE)  # noqa: E731
    state = next((ws.state_of(c) for c in _items(lister()) if c.get("id") == cid and c.get("name") == cfg.name), None)
    if state not in ("ready", "running"):
        raise SystemExit(f"{cfg.name}: the recorded run cluster {cid} is {state or 'not listed'}: refusing")
    if security_fn is None:
        def security_fn(cluster_id):
            from coiled.v2.core import CloudV2
            with CloudV2() as cloud:
                return cloud.security(cluster_id, workspace=WORKSPACE)
    sec, info = security_fn(cid)
    if client_cls is None:
        from distributed import Client as client_cls
    return client_cls(info["address_to_use"], security=sec, timeout="60s")


def checkpoint_now(cfg, timeout_s: float = 300.0, invocation=None, launch_id=None, store=None, connect=None,
                   sleep=time.sleep, clock=time.time, poll_s: float = 10.0) -> int:
    """`checkpoint-now`: one checkpoint of every running label, under one deadline. The run cluster is the id
    clusters.json records (never by name); a record of another invocation or launch (`invocation`, `launch_id`:
    a caller passes its own) or assignments of another launch are refused. Each label gets a request that
    returns at once (worker_side.checkpoint_now_side: nothing waits on the worker), then storage is polled for a
    generation carrying its token or started after it. 0 when every label checkpointed or had finished."""
    rec, assign = json.load(open(cfg.clusters_file)), json.load(open(cfg.state_file))
    if invocation and rec.get("invocation") != invocation:
        raise SystemExit(f"{cfg.name}: clusters.json is invocation {rec.get('invocation')}, not {invocation}: refusing")
    lid = rec.get("launch_id")
    if (launch_id and lid != launch_id) or any((assign.get(lb) or {}).get("launch_id") != lid for lb in cfg.labels):
        raise SystemExit(f"{cfg.name}: clusters.json launch {lid}, asked {launch_id}, assignments "
                         f"{[(assign.get(lb) or {}).get('launch_id') for lb in cfg.labels]}: refusing")
    cid = (rec.get("run") or {}).get("id")
    if cid is None:
        raise SystemExit(f"{cfg.name}: clusters.json records no run cluster id: refusing")
    if store is None:
        store = _results_store()
    try:   # sent by value: the agent VM's uploaded worker_side may predate this function
        import cloudpickle
        cloudpickle.register_pickle_by_value(ws)
    except ImportError:
        pass
    deadline = clock() + timeout_s
    left = lambda: deadline - clock()  # noqa: E731 -- checked before, and the bound of, every blocking call
    if left() <= 0:
        raise SystemExit(f"{cfg.name}: checkpoint-now had no time left (timeout {timeout_s:g} s)")
    client = _bounded(connect or _connect_recorded, cfg, cid, timeout_s=left())
    import threading
    answers, threads, state, reqs = {}, {}, {}, {}

    def ask(label):    # one thread per label, all at once: an unresponsive worker holds up no other label
        try:
            answers[label] = _on_worker(client, ws.checkpoint_now_side, label, worker=assign[label]["worker"])
        except Exception as e:  # noqa: BLE001 -- reported; the caller decides
            answers[label] = {"state": f"unavailable ({type(e).__name__}: {str(e)[:120]})"}
    for label in cfg.labels:
        threads[label] = threading.Thread(target=ask, args=(label,), daemon=True)
        threads[label].start()
    for t in threads.values():
        if left() > 0:
            t.join(min(0.5, left()))
    while True:
        for label in [lb for lb in threads if lb in answers]:
            threads.pop(label)
            r = answers[label]
            state[label] = r.get("state")
            if r.get("state") == "requested":
                reqs[label] = r
        for label in list(reqs):
            if left() <= 0:
                break
            try:
                p = _bounded(checkpoint_record, cfg, store, label, lid, timeout_s=left()) or {}
            except TimeoutError:
                p = {}
            if reqs[label]["token"] in (p.get("requests") or []) or (p.get("started_epoch") or 0) >= reqs[label]["epoch"]:
                state[label] = f"checkpointed {ws.checkpoint_pointer(cfg.storage_prefix, lid, label)} " \
                               f"(generation {p.get('generation')})"
                del reqs[label]
        if not (reqs or threads) or deadline - clock() <= 0:
            break
        sleep(min(poll_s, deadline - clock()))
    for label in threads:
        state[label] = f"unavailable (no answer within {timeout_s:g} s)"
    for label in reqs:
        state[label] = "timeout"
    for label in cfg.labels:
        print(f"{label}: {state[label]}", flush=True)
    return 0 if all(state[lb].startswith(("checkpointed", "finished")) for lb in cfg.labels) else 1


def local_is_current(dest: str, launch_id: str) -> bool | None:
    """True: dest holds this launch's collected run; False: an earlier launch's
    (set aside before collecting); None: nothing collected yet."""
    p, part = os.path.join(dest, "result.json"), os.path.join(dest, "result.partial.json")
    try:
        if not os.path.exists(p):
            # a partial copy (collect: large files only on the VM) is never "collected": an earlier launch's is
            # set aside like a full one, this launch's is fetched again over itself
            if os.path.exists(part) and json.load(open(part)).get("launch_id") != launch_id:
                return False
            return None
        return json.load(open(p)).get("launch_id") == launch_id
    except (OSError, ValueError, AttributeError):
        return False


def relaunch_refusal(vm_state: str, stored: bool) -> str | None:
    """Why a label may not be re-fired, or None. Only a run that is neither
    running, nor finished on its VM, nor stored may be: re-firing wipes the
    VM's work directory, which holds results whose upload failed."""
    if stored:
        return "already stored in this launch; collect it instead"
    if vm_state in ("running", "preparing"):
        return "still running on its VM"
    if vm_state == "dead":       # its run dir is the only local trace: `checkpoint`, or a look at the VM, first
        return "its task died on its VM, whose work dir still holds the run; fetch its checkpoint first"
    if vm_state == "done":
        return "finished on its VM but not stored; collect it first"
    return None


def _download(client, label, worker, dest_file):
    """Chunked, resumable, sha256-verified (one 591 MB message reset the Coiled proxy)."""
    m = _on_worker(client, ws.fetch_meta, label, worker=worker)
    have = os.path.getsize(dest_file) if os.path.exists(dest_file) else 0
    with open(dest_file, "ab") as out:
        while have < m["size"]:
            for attempt in range(5):
                try:
                    b = _on_worker(client, ws.fetch_chunk, label, have, CHUNK, worker=worker)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 4:
                        raise
                    time.sleep(5)
            out.write(b)
            have += len(b)
    h = ws._sha_file(dest_file)            # block by block: the archive is never read whole
    if h != m["sha256"]:
        os.remove(dest_file)
        raise SystemExit(f"{label}: download did not verify; removed, re-run collect")
    return m["meta"]


def collect(cfg, shutdown_after: bool = True, accept_partial: bool = False):
    results_store = _results_store()
    os.makedirs(cfg.out_dir, exist_ok=True)
    running = is_running(cfg.name)
    if not os.path.exists(cfg.state_file):
        raise SystemExit(f"no {cfg.state_file}: this machine did not launch {cfg.name}")
    assign, client, pending, refused = json.load(open(cfg.state_file)), None, [], set()
    for label in recorded_labels(cfg, assign):
        dest = os.path.join(cfg.out_dir, label)
        cur = local_is_current(dest, assign[label]["launch_id"])
        if cur:
            print(f"{label}: already collected")
            continue
        if cur is False:   # an earlier launch's copy: keep it, out of the way
            os.rename(dest, f"{dest}.before_{assign[label]['launch_id']}_{int(time.time())}")
        try:
            meta = collect_from_storage(cfg, results_store, label, assign[label]["launch_id"], assign[label],
                                        siblings={lb: (assign.get(lb) or {}).get("launch_id")
                                                  for lb in recorded_labels(cfg, assign)})
            print(f"{label}: exit={meta['exit_code']} {meta['duration_s']}s -> {cfg.out_dir}/{label} (from storage)")
            continue
        except Exception as e:  # noqa: BLE001 — not (yet) in storage
            if isinstance(e, StoredRunRefused):
                print(f"{label}: REFUSED {e}")
                refused.add(label)
            ck = checkpoint_record(cfg, results_store, label, assign[label]["launch_id"])
            ck_note = (f"; only a checkpoint exists, of {ck.get('time_utc')} (state {ck.get('run_state')}, "
                       f"{ck.get('cached_batches')} cached batches): fetch it with `checkpoint --label {label}`"
                       if ck else "")
            part = os.path.join(dest, "result.partial.json")
            # this launch's partial copy (local_is_current), fetched earlier, and its VM is gone: accepted as it
            # is. With the VM up, the run is fetched again below and the VM is told (its hold ends).
            if accept_partial and os.path.exists(part) and not running:
                os.replace(part, os.path.join(dest, "result.json"))
                print(f"{label}: the partial copy in {dest} is accepted as collected")
                continue
            if not running:
                print(f"{label}: not in storage ({type(e).__name__}) and the cluster has stopped{ck_note}")
                continue
            if ck_note:
                print(f"{label}: no final result yet{ck_note}")
        if client is None:
            client = existing_client(cfg.name)
        w = assign[label]["worker"]
        if _on_worker(client, ws.fetch_meta, label, worker=w)["state"] != "done":
            print(f"{label}: still running")
            pending.append(label)
            continue
        tgz = os.path.join(cfg.out_dir, f"{label}.result.tar.gz")
        if os.path.exists(tgz):
            os.remove(tgz)   # a partial download may be from an earlier launch
        meta = _download(client, label, w, tgz)
        os.makedirs(dest, exist_ok=True)
        with tarfile.open(tgz, mode="r:gz") as t:
            t.extractall(dest, filter="data")
        ident = _run_identity_on_disk(dest)
        _identity_note(label, assign[label], ident)
        # a record from a worker before 1E-ii has no unstored_large_files: every large file it left out counts
        # Codex r1 on #374: after a refused stored run, the VM left out large files on a receipt that is not this
        # label's: every file it left out counts as in no store
        lost = None if (meta.get("run_stored") and label not in refused) or accept_partial else \
            (meta.get("skipped_large_files") if label in refused else
             meta.get("unstored_large_files", meta.get("skipped_large_files")))
        if lost:
            # 1E-ii: the VM's tarball lacks large files that are in no store. The label stays pending: no result.json
            # (so a later `collect` fetches it again), not marked collected (the VM keeps holding), no shutdown.
            json.dump({**meta, **assign[label], "run_identity": ident},
                      open(os.path.join(dest, "result.partial.json"), "w"), indent=1)
            print(f"{label}: fetched -> {dest}, but {len(lost)} large file(s) exist only on the VM, e.g. "
                  f"{[x[0] for x in lost[:3]]}: left pending; copy them from the VM, then `collect --accept-partial`")
            pending.append(label)
            continue
        json.dump({**meta, **assign[label], "run_identity": ident}, open(os.path.join(dest, "result.json"), "w"),
                  indent=1)
        _on_worker(client, ws.mark_collected, label, worker=w)
        print(f"{label}: collected from the VM -> {dest}")
    if running and pending and shutdown_after:
        print(f"not shutting down: {', '.join(pending)} {'has' if len(pending) == 1 else 'have'} no final result "
              f"yet (still running); collect again once status shows every label done")
    if running and not pending and shutdown_after:
        print("every run is collected: shutting down")
        shutdown(cfg)
    elif running and not pending:
        print("every run is collected (--no-shutdown: the clusters are left to the caller)")


def shutdown(cfg):
    for name in (cfg.name, cfg.overflow_name, cfg.keeper_name):
        cid = live_id(name)
        if cid is not None:
            _stop_id(name, cid)          # by id: never an attach by name, which can create a cluster
        else:
            print(f"{name}: not running")


# ---- report-cost: Coiled credits per useful core-hour (Phase 42 β, A5) --------------------

def billing_events(start_iso: str, end_iso: str, getter=None) -> list[dict]:
    """Every Coiled billing event in the window, all pages. Coiled writes one event per
    instance when it stops, so an instance still running is not billed yet. The API's
    cluster filter is ignored by the server: filter the result client-side."""
    if getter is None:
        import coiled
        getter = lambda page: coiled.get_billing_activity(account=WORKSPACE, start_time=start_iso,  # noqa: E731
                                                          end_time=end_iso, page=page)
    out, page = [], 1
    while True:
        r = getter(page)
        out += list(r.get("results") or [])
        if not r.get("next") or page > 10000:
            return out
        page += 1


def summarize_billing(events: list[dict], clusters: dict) -> dict:
    """{role: {credits, instances, workers}} for the launch's clusters. `clusters` is
    {role: {"name", "id"}}: an id matches exactly (names are reused across launches);
    without an id the name matches within the window."""
    out = {}
    for role, c in clusters.items():
        mine = [e for e in events if (e.get("cluster_id") == c["id"] if c.get("id") is not None
                                      else e.get("cluster") == c["name"])]
        inst = {e.get("instance") for e in mine}
        out[role] = {"name": c["name"], "id": c.get("id"), "credits": round(-sum(float(e.get("amount_credits") or 0)
                                                                              for e in mine), 2),
                     "instances": len(inst), "workers": sum(1 for i in inst if "-worker-" in str(i)),
                     "last_event": max((e.get("event_datetime") or "" for e in mine), default=None)}
    return out


_WASTED = ("interrupted", "caller died")


def _ledger_ts(ts):
    """A ledger timestamp (the VM's clock, UTC, written without a zone) as epoch seconds."""
    t = str(ts or "")
    return ws._epoch(t if t.endswith("Z") or "+" in t[10:] else t + "+00:00")


def ledger_hours(led: dict, start: float | None = None, end: float | None = None) -> dict:
    """Core-hours from a run's compute ledger. `charged` is what the run's budget counts
    (it includes reservations charged for a dead caller); `useful` is the measured task
    time of batches that completed: an interrupted call's or a charged reservation's
    core-seconds are left out. The ledger has no per-row times, so a completed batch's
    failed rows stay in `useful`; `failed_rows` reports how many there were. With a window
    (epoch seconds), only batches that ended inside it count, and `charged` is their sum,
    so the hours match a billing window instead of the run's lifetime."""
    calls = [c for c in (led or {}).get("calls", []) if isinstance(c, dict)]
    windowed = start is not None or end is not None
    if windowed:
        def inside(c):
            t = _ledger_ts(c.get("ts"))
            return t is not None and (start is None or t >= start) and (end is None or t <= end)
        calls = [c for c in calls if inside(c)]
    done = [c for c in calls if not str(c.get("stopped") or "").startswith(_WASTED)]
    useful = sum(float(c.get("task_core_s") or 0) for c in done)
    charged = (sum(float(c.get("task_core_s") or 0) for c in calls) if windowed
               else float((led or {}).get("core_seconds") or 0))
    return {"charged": round(charged / 3600, 2), "useful": round(useful / 3600, 2),
            "calls": len(calls), "failed_rows": sum(int(c.get("n_failed") or 0) for c in done),
            "as_of": max((str(c.get("ts") or "") for c in calls), default=None)}


def cost_lines(bill: dict, hours: dict | None, window_h: float) -> list[str]:
    total = sum(b["credits"] for b in bill.values())
    out = [f"{role}: {b['name']} (id {b['id']}): {b['credits']} credits, {b['instances']} VMs "
           f"({b['workers']} workers), last billed {b['last_event']}" for role, b in bill.items()]
    run = bill.get("run", {})
    out.append(f"total: {total:.1f} credits over {window_h:.1f} h; run-cluster workers created per hour: "
               f"{run.get('workers', 0) / window_h:.1f}" if window_h > 0 else f"total: {total:.1f} credits")
    if hours is None:
        out.append("useful core-hours: unknown (no compute ledger: pass --ledger, or collect the run)")
    else:
        u = hours["useful"]
        out.append(f"compute ledger (as of {hours['as_of']}): {u} core-hours in completed batches of "
                   f"{hours['charged']} charged, {hours['calls']} batches ({hours.get('failed_rows', 0)} failed rows "
                   "inside completed batches)")
        out.append(f"credits per useful core-hour: {total / u:.2f}" if u > 0 else
                   "credits per useful core-hour: undefined (no useful core-hours)")
    out.append("note: Coiled bills an instance when it stops, so a live cluster's running VMs are not counted yet")
    return out


def _find_ledgers(cfg, store=None) -> list[str]:
    """This launch's ledgers only: per label, the collected copy if it is this launch's
    (local_is_current), else the stored run's. Earlier launches' copies are never mixed in."""
    import glob
    if not os.path.exists(cfg.state_file):
        return []
    assign, found = json.load(open(cfg.state_file)), []
    for label in cfg.labels:
        dest = os.path.join(cfg.out_dir, label)
        if label in assign and local_is_current(dest, assign[label]["launch_id"]):
            local = sorted(glob.glob(os.path.join(dest, "runs", "*", "models", "compute_ledger.json")))
            if local:
                found.append(local[-1])
                continue
        if store is None or label not in assign:
            continue
        try:   # a stored run: the ledger member of its tarball only
            rj = os.path.join(cfg.out_dir, f"{label}.result.json")
            store.download(ws.record_blob(cfg.storage_prefix, assign[label]["launch_id"], label), rj, cfg.storage_account)
            tgz = os.path.join(cfg.out_dir, f"{label}.cost.run.tar.gz")   # always this record's run
            store.download(json.load(open(rj))["run_stored"], tgz, cfg.storage_account)
            with tarfile.open(tgz, mode="r:gz") as t:
                m = next(m for m in t.getmembers() if m.name.endswith("models/compute_ledger.json"))
                dest = os.path.join(cfg.out_dir, f"{label}.compute_ledger.json")
                open(dest, "wb").write(t.extractfile(m).read())
                found.append(dest)
        except Exception as e:  # noqa: BLE001 — not stored yet
            print(f"{label}: no stored compute ledger ({type(e).__name__})")
    return found


def _window_start(cfg) -> str | None:
    if os.path.exists(cfg.clusters_file):
        return json.load(open(cfg.clusters_file)).get("launched_utc")
    if os.path.exists(cfg.state_file):
        sub = min(v["submitted"] for v in json.load(open(cfg.state_file)).values())
        t = time.mktime(time.strptime(sub, "%Y-%m-%d %H:%M:%S")) - 1800   # the cluster starts before the submit
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    return None


def report_cost(cfg, args):
    """report-cost [--ledger PATH]... [--start ISO] [--end ISO]: billing API only (no attach)."""
    ledgers, start, end, i = [], None, None, 0
    while i < len(args):
        k = args[i]
        if k in ("--ledger", "--start", "--end") and i + 1 < len(args):
            v = args[i + 1]
            if k == "--ledger":
                ledgers.append(os.path.expanduser(v))
            elif k == "--start":
                start = v
            else:
                end = v
            i += 2
        else:
            raise SystemExit(f"report-cost: unknown argument {k!r}")
    start = start or _window_start(cfg)
    if not start:
        raise SystemExit("report-cost: no launch record here; pass --start <ISO time, UTC>")
    end = end or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    clusters = {"run": {"name": cfg.name, "id": None}, "keeper": {"name": cfg.keeper_name, "id": None},
                "overflow": {"name": cfg.overflow_name, "id": None}}
    if os.path.exists(cfg.clusters_file):
        for role, c in json.load(open(cfg.clusters_file)).items():
            if isinstance(c, dict) and role in clusters:
                clusters[role] = c
    elif os.path.exists(cfg.state_file):
        clusters["run"]["id"] = next(iter(json.load(open(cfg.state_file)).values())).get("cluster_id")
    if not ledgers:
        try:
            results_store = _results_store()
        except ImportError:
            results_store = None
        ledgers = _find_ledgers(cfg, results_store)
    hours = None
    for p in ledgers:
        h = ledger_hours(json.load(open(p)), ws._epoch(start), ws._epoch(end))
        hours = h if hours is None else {"charged": round(hours["charged"] + h["charged"], 2),
                                         "useful": round(hours["useful"] + h["useful"], 2),
                                         "calls": hours["calls"] + h["calls"],
                                         "failed_rows": hours["failed_rows"] + h["failed_rows"], "as_of": max(hours["as_of"] or "", h["as_of"] or "")}
    bill = summarize_billing(billing_events(start, end), clusters)
    wh = (ws._epoch(end) - ws._epoch(start)) / 3600
    print(f"{cfg.name}: Coiled billing {start} .. {end}" + (f"; ledgers: {', '.join(ledgers)}" if ledgers else ""))
    print("\n".join(cost_lines(bill, hours, wh)))


def relaunch(cfg, label) -> int:
    """Re-fire one run on its pinned agent VM (e.g. after a preflight-only fix, or a label a launch never
    submitted). The same submission boundary as `launch`: once the submit call is made, the exit is 0 or 4."""
    inv = _invocation()
    print(f"invocation {inv}", flush=True)
    with _config_lock(cfg):
        assign = json.load(open(cfg.state_file))
        if label not in assign:
            raise SystemExit(f"{label}: no assignment in {cfg.state_file}; not relaunching")
        budget_guard(cfg, reserve=False)  # more paid work on the live run cluster: the budget must still cover it
        keys = api_keys()                 # before any call to storage or the cluster (Codex r1 on #362)
        results_store = _results_store()
        launch_id = assign[label]["launch_id"]
        names = {b["name"] for b in results_store.list_blobs(f"outputs/{cfg.storage_prefix}/launch_{launch_id}/",
                                                              cfg.storage_account)}
        client = existing_client(cfg.name)
        why = relaunch_refusal(_on_worker(client, ws.fetch_meta, label, worker=assign[label]["worker"])["state"],
                               ws.record_blob(cfg.storage_prefix, launch_id, label) in names)
        if why:
            raise SystemExit(f"{label}: {why}; not relaunching")
        if assign[label].get("submission") in ("sent", "listed"):   # a VM with no run.log yet may be preparing it
            tk = assign[label].get("task_key")
            if not tk:
                raise SystemExit(f"{label}: assignment is {assign[label]['submission']} but records no task key, so "
                                 "the original task cannot be checked; not relaunching")
            try:
                st = _scheduler_state(client, tk)
            except Exception as e:  # noqa: BLE001 — unknown: refuse rather than run a second copy
                raise SystemExit(f"{label}: the state of task {tk} cannot be read ({type(e).__name__}); not "
                                 "relaunching") from None
            if st in LIVE_STATES:
                raise SystemExit(f"{label}: task {tk} is still {st} on the scheduler (accepted, queued or preparing); "
                                 "not relaunching")
        code, commit = code_tarball(cfg)
        pipeline = pipeline_preflight(cfg, code)
        stale = stale_sheet_problems(code, cfg.flags, pipeline)   # F6 (#218): the launch's checks, on these bytes
        if stale:
            raise SystemExit(f"{label}: refusing: " + "; ".join(stale))
        client.upload_file(os.path.join(HERE, "worker_side.py"))
        question = open(cfg.question_file).read().strip()
        assign[label].update(commit=commit, submitted=time.strftime("%F %T"), relaunched=True, pipeline=pipeline,
                             launcher=f"run_cluster:{cfg.name}", eval_inputs_sha256=eval_paths.archive_sha256(code),
                             submission="sent", invocation=inv, task_key=_task_key(label))
        _atomic_json(cfg.state_file, assign)
        sent = []
        try:
            sent.append(label)            # from the submit call on, the run may be going
            key, fut = _submit(cfg, client, label, assign[label]["worker"], code, question, keys, launch_id,
                               key=assign[label]["task_key"])
            print(f"re-fired {label} -> {assign[label]['worker']} @ {commit} (pipeline {pipeline})", flush=True)
            if not wait_listed(client, key, fut):
                raise TimeoutError(f"the scheduler did not list {key} within {ACK_ATTEMPTS} polls")
        except BaseException as e:  # noqa: BLE001 — after the submit call nothing may read as a failure
            code_, _ = _outcome(sent, [], e)
            try:
                print(f"{label}: submitted, not confirmed ({type(e).__name__}: {str(e)[:200]}); it may be running: "
                      "do not shut down, check `status`", flush=True)
            except BaseException:  # noqa: BLE001
                pass
            return code_
        try:
            assign[label]["submission"] = "listed"
            _atomic_json(cfg.state_file, assign)
            print(f"listed {label} ({key}) by the scheduler", flush=True)
        except BaseException:  # noqa: BLE001
            pass
        return EXIT_LAUNCHED


def parse_sign_args(rest: list, labels) -> dict:
    """`sign <answers.yaml> [--label L] [--run RUN_DIR_NAME] [--expect-spec-sha256 SHA] [note ...]`. The options
    name the remote run the owner was shown (the run console passes them); without --label the config's
    first label is used, as before."""
    if not rest:
        raise SystemExit("sign needs <answers.yaml>")
    out, i = {"answers": rest[0], "label": None, "run": None, "expect": None, "action": None,
              "expect_derivations": None}, 1
    flags = {"--label": "label", "--run": "run", "--expect-spec-sha256": "expect", "--action": "action",
             "--expect-derivations-sha256": "expect_derivations"}
    while i < len(rest) and rest[i] in flags:
        if i + 1 >= len(rest):
            raise SystemExit(f"sign: {rest[i]} needs a value")
        out[flags[rest[i]]] = rest[i + 1]
        i += 2
    out["note"] = " ".join(rest[i:])
    if out["action"] not in (None, "spec", "scope"):
        raise SystemExit(f"sign: --action is spec or scope, not {out['action']}")
    if out["label"] is not None and out["label"] not in labels:
        raise SystemExit(f"sign: label {out['label']} is not one this config runs ({', '.join(labels)})")
    return out


def _side(cfg, fn, *args, label=None):
    label = label or cfg.labels[0]
    assign = json.load(open(cfg.state_file))
    return _on_worker(existing_client(cfg.name), fn, label, *args, worker=assign[label]["worker"])



def main(argv) -> int:
    if len(argv) < 3:
        sys.exit(__doc__)
    cfg, cmd, rest = load_config(argv[1]), argv[2], argv[3:]
    if cmd == "launch":
        return launch(cfg)
    elif cmd == "keeper":
        return keeper_later(cfg)
    elif cmd == "reuse-check":
        if not rest or rest[0].startswith("-"):
            sys.exit(__doc__)
        r = reuse_check(cfg, rest[0], expect_all="--expect-all" in rest[1:])
        print(json.dumps({k: v for k, v in r.items() if k != "rc"}, indent=1))
        print(f"{r.get('reusable')} of {r.get('stored')} stored rows would be reused by the code this launch ships")
        return r["rc"]
    elif cmd == "status":
        status(cfg)
    elif cmd == "collect":
        collect(cfg, shutdown_after="--no-shutdown" not in rest, accept_partial="--accept-partial" in rest)
    elif cmd == "checkpoint":
        results_store = _results_store()
        for label in ([rest[rest.index("--label") + 1]] if "--label" in rest else cfg.labels):
            fetch_checkpoint(cfg, results_store, label)
    elif cmd == "checkpoint-now":
        opt = lambda k, d=None: rest[rest.index(k) + 1] if k in rest else d  # noqa: E731
        return checkpoint_now(cfg, float(opt("--timeout-s", 300.0)), invocation=opt("--invocation"),
                              launch_id=opt("--launch-id"))
    elif cmd == "shutdown":
        shutdown(cfg)
    elif cmd == "relaunch":
        return relaunch(cfg, rest[0])
    elif cmd == "report-cost":
        report_cost(cfg, rest)
    elif cmd == "keeper-log":
        c = existing_client(cfg.keeper_name)
        w = sorted(ws.scheduler_workers(lambda: c, attempts=3, min_workers=1))[0]
        print(c.run(ws.keeper_log, workers=[w])[w])
    elif cmd == "sign":
        a = parse_sign_args(rest, cfg.labels)
        try:   # sent by value: a worker uploaded before --run existed still runs this sign_side
            import cloudpickle
            cloudpickle.register_pickle_by_value(ws)
        except ImportError:
            pass
        r = _side(cfg, ws.sign_side, open(a["answers"]).read(), a["note"], a["run"], a["expect"], None, a["action"],
                  a["expect_derivations"], label=a["label"])
        print(r["out"])
        return r["rc"]
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
