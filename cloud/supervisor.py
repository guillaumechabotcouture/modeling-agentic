#!/usr/bin/env python3
"""Between-run supervision off the laptop (W0-J, small version; owner yes 2026-11-22: laptop sleep twice stalled all
work for about 8 hours). One loop on a small Coiled VM, all its state in blob storage; the laptop is only a client.

  (a) Merge train (ops/merge_train.sh's steps), one queued PR at a time: update the branch when it is behind its base,
      wait for CI on the head, merge at that head's sha once every check, commit status and the CI workflow run on it
      succeeded; skip and record anything else. Held until a GitHub token is provided ($GH_TOKEN, below).
  (b) Eval collection: each enqueued launch (a run_cluster.py config's assignments.json) is fetched label by label with
      run_cluster.collect_from_storage (storage only: no cluster is attached or stopped), archived to the supervisor's
      prefix, and scripts/harness_metrics.py runs over every collected run (metrics.json).
  (c) status.json, rewritten every loop: heartbeat, merge queue, collection, launches, paused runs
      (resume is not automated here), errors. Layout under outputs/supervisor/<name>/: requests/ (write-once, from the
      laptop), state.json and status.json (the loop's only), collected/<launch>/<label>.tar.gz,
      launches/<request>/launch.log, metrics.json. The storage account comes from
      $MA_STORAGE_ACCOUNT / $COILED_STORAGE_ACCOUNT or --account (a bare account name only), never the code.
  (d) Launches (`enqueue-launch`), one per request: the loop makes its own clean checkout of the commit (a fresh
      clone; anything that differs from the commit, ignored files included, means a new clone), then runs
      `run_cluster.py <checkout>/<config> launch` with EVAL_TAG, EVAL_REPS, EVAL_TREE=<checkout> and
      MA_LAUNCH_INVOCATION=sv-<request>: run_cluster's own launch path and budget guard (scope ev; a config in another
      scope is refused). The request is recorded as launching before the child starts and is never launched again
      (one found launching after a restart is `interrupted`); a child past 45 min is ended and read as exit 4. On
      exit 0/3/4/5 the launch id, cluster ids and assignments are recorded and its collection enqueued (b); otherwise
      only this invocation's own failed record's ids are stopped (run_cluster's rule).
      Health checks and stop rules for launches come in a separate change; until then a request with stop rules
      is refused.

    python3 cloud/supervisor.py start [--name sv-supervisor-gcc] [--cap-usd 15] [--max-hours 168]
    python3 cloud/supervisor.py enqueue-merge <pr>... | enqueue-collect <config.py>... | enqueue-noop
    python3 cloud/supervisor.py enqueue-launch cloud/configs/eval_<q>.py --tag T --reps N --commit SHA
    python3 cloud/supervisor.py status | pull [--dest ~/modeling-agentic-keep/supervisor] | stop
    python3 cloud/supervisor.py loop --name <name>      # on the VM; `start` runs it as a vm_job job
    python3 cloud/supervisor.py --self-test

`start` is vm_job `up` (2-vCPU worker, 2-vCPU scheduler; the name needs a budget scope, the default is in `sv`) then
`start` with the loop as the job, a dask task that holds the cluster for --max-hours (one long job, owner 2026-11-23:
there is no hand-over to a successor). `stop` stops this name's live cluster by id. Secrets reach the job's
environment only, by name (vm_job --secret-env), and `start` never copies the Coiled token or the Anthropic key into
its own environment: $GH_TOKEN when the login keychain holds it under the service "modeling-agentic-supervisor-github"
(without it merges stay held); DASK_COILED__TOKEN, which vm_job reads from the laptop's dask config, and
ANTHROPIC_API_KEY, which it reads from the .env file (without either, launches stay held). A launch request carries the
launch scope's signed budget record only when budget.py's checks pass on the laptop (signature, expiry, high-water
mark), and the VM installs it only when they pass there too.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

ROOT = "outputs/supervisor"
DEFAULT_NAME = "sv-supervisor-gcc"
GH_REPO = "guillaumechabotcouture/modeling-agentic"
KEYCHAIN_SERVICE = "modeling-agentic-supervisor-github"
STALE_S = 600            # status older than this: the loop is not running
GIVE_UP_S = 3 * 3600     # a PR still waiting after this long is skipped
UPDATE_WAIT_S = 300      # after update-branch, wait this long for the new head before asking again
COLLECT_EVERY_S = 300    # a label not yet in storage is tried again after this long
CI_WORKFLOW = ".github/workflows/ci.yml"   # a head merges only once this workflow's run on it has succeeded
KEEP = 50              # last N acks / errors / finished merges kept in state


_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")           # supervisor and launch names
_LABEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")  # run labels: neither can leave its directory
_ACCOUNT = re.compile(r"[a-z0-9]{3,24}")                  # Azure storage account names


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def paths(name: str) -> dict:
    if not _NAME.fullmatch(name or ""):
        raise SystemExit(f"bad supervisor name {name!r}")
    b = f"{ROOT}/{name}"
    return {"requests": f"{b}/requests/", "state": f"{b}/state.json", "status": f"{b}/status.json",
            "collected": f"{b}/collected/", "metrics": f"{b}/metrics.json",
            "launches": f"{b}/launches/"}


class Blob:
    """results_store with one account: JSON and file put/get, listing. get_json returns None when absent."""
    def __init__(self, account=None):
        import results_store
        self.rs, self.account = results_store, account or results_store.configured_account()
        if not self.account:
            raise SystemExit("no storage account: set $MA_STORAGE_ACCOUNT / $COILED_STORAGE_ACCOUNT or pass --account")
        if not _ACCOUNT.fullmatch(str(self.account)):   # vm_job.storage_account's rule; the value is never echoed
            raise SystemExit("the storage account (--account or $MA_STORAGE_ACCOUNT / $COILED_STORAGE_ACCOUNT) must be "
                             "a bare storage account name (3-24 lowercase letters and digits), not a connection "
                             "string, URL or key: refusing")
        self.svc = results_store._service(self.account)
    def put_json(self, blob, obj):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.json")
            json.dump(obj, open(p, "w"), indent=1, default=str)
            self.rs.upload_retry(p, blob, self.account, service=self.svc)
    def get_json(self, blob):
        try:
            data = self.svc.get_blob_client(*self.rs._split(blob)).download_blob().readall()
        except Exception as e:  # noqa: BLE001 -- absent is None; anything else (network, auth) raises
            if type(e).__name__ == "ResourceNotFoundError":
                return None
            raise
        return json.loads(data)
    def put_file(self, path, blob):
        return self.rs.upload_file(path, blob, self.account, service=self.svc)
    def get_file(self, blob, path):
        return self.rs.download(blob, path, self.account, service=self.svc)
    def names(self, prefix):
        return sorted(b["name"] for b in self.rs.list_blobs(prefix, self.account, service=self.svc))


def enqueue(blob, name: str, kind: str, payload: dict, now: float | None = None) -> str:
    """One write-once request blob; only the loop reads them, so the laptop never races the loop on a shared file."""
    t = time.time() if now is None else now
    rid = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(t))}-{secrets.token_hex(4)}"
    blob.put_json(f"{paths(name)['requests']}{rid}.json", {"id": rid, "kind": kind, "payload": payload, "at": utc(t)})
    return rid


class GitHub:
    """The REST calls merge_train.sh made through `gh`. The token is sent as a header and never stored or printed."""

    def __init__(self, token: str, repo: str = GH_REPO):
        if not re.fullmatch(r"[A-Za-z0-9_]{20,255}", token or ""):
            raise ValueError("the GitHub token is not a bare token (letters, digits, _): merges stay disabled")
        self._token, self.repo = token, repo

    def _call(self, method, path, body=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"https://api.github.com/repos/{self.repo}/{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, {}

    def _get(self, path):
        code, j = self._call("GET", path)
        if code != 200:
            raise (LookupError if code == 404 else RuntimeError)(f"GET {path.split('?')[0]}: HTTP {code}")
        return j

    def pr(self, n):
        j = self._get(f"pulls/{int(n)}")
        return {"state": j["state"], "merged": bool(j.get("merged")), "head": j["head"]["sha"], "base": j["base"]["ref"]}

    def behind(self, base, head):
        return int(self._get(f"compare/{base}...{head}").get("behind_by") or 0) > 0

    def checks(self, sha):
        """One word per check run (every page), commit status and CI_WORKFLOW run: success | pending | failure. No
        CI_WORKFLOW run on the head yet is pending (its jobs may not have registered); an unreadable answer raises."""
        word = lambda st, c: ("pending" if st != "completed" else  # noqa: E731
                              "success" if c in ("success", "neutral", "skipped") else "failure")
        out, page, runs = [], 0, [None] * 100
        while len(runs) == 100:
            page += 1
            runs = self._get(f"commits/{sha}/check-runs?per_page=100&page={page}").get("check_runs") or []
            out += [word(r.get("status"), r.get("conclusion")) for r in runs]
        out += [{"success": "success", "pending": "pending"}.get(x.get("state"), "failure")
                for x in self._get(f"commits/{sha}/status").get("statuses") or []]
        wf = [word(r.get("status"), r.get("conclusion")) for r in
              self._get(f"actions/runs?head_sha={sha}&per_page=100").get("workflow_runs") or []
              if r.get("path") == CI_WORKFLOW]
        return out + (wf or ["pending"])

    def update_branch(self, n, sha):
        return self._call("PUT", f"pulls/{int(n)}/update-branch", {"expected_head_sha": sha})[0] in (200, 202)

    def merge(self, n, sha):
        code, j = self._call("PUT", f"pulls/{int(n)}/merge", {"merge_method": "merge", "sha": sha})
        return code == 200 and bool(j.get("merged", True))


def merge_step(gh, item: dict, now: float) -> dict:
    """One step of one PR: item {pr, state queued|waiting|merged|already_merged|skipped, note, ...}, updated in place."""
    item.setdefault("since", now)
    item["last_step"] = utc(now)
    end = lambda state, note: item.update(state=state, note=note, ended=utc(now)) or item  # noqa: E731
    if now - item["since"] > GIVE_UP_S:       # first: a PR GitHub keeps failing on never blocks the queue
        return end("skipped", f"gave up after {GIVE_UP_S // 3600} h waiting")
    try:
        pr = gh.pr(item["pr"])
    except LookupError as e:
        return end("skipped", str(e))
    if pr["merged"]:
        return end("already_merged", "already merged")
    if pr["state"] != "open":
        return end("skipped", f"PR is {pr['state']}")
    head = pr["head"]
    item["state"] = "waiting"
    if gh.behind(pr["base"], head):
        if item.get("updated_for") == head and now - item.get("updated_at", 0) < UPDATE_WAIT_S:
            item["note"] = "behind its base: update requested, waiting for the new head"
            return item
        if not gh.update_branch(item["pr"], head):
            return end("skipped", "update-branch failed (conflict?)")
        item.update(updated_for=head, updated_at=now, note=f"behind {pr['base']}: update-branch requested")
        return item
    checks = gh.checks(head)
    if "failure" in checks:
        return end("skipped", f"CI failed on {head[:9]}")
    if not checks or "pending" in checks:
        item["note"] = f"waiting for CI on {head[:9]} ({checks.count('pending')} pending of {len(checks)})"
        return item
    if gh.merge(item["pr"], head):
        return end("merged", f"merged at {head[:9]}")
    return end("skipped", f"merge refused at {head[:9]}")


def fetch_from_storage(entry, label, work):
    """run_cluster.collect_from_storage into <work>/<launch name>/<label>/ (storage only, no cluster touched)."""
    import types
    import results_store
    import run_cluster as rc
    cfg = types.SimpleNamespace(out_dir=os.path.join(work, entry["name"]), storage_prefix=entry["storage_prefix"],
                                storage_account=entry.get("storage_account"))
    os.makedirs(cfg.out_dir, exist_ok=True)
    return rc.collect_from_storage(cfg, results_store, label, entry["assignments"][label]["launch_id"],
                                   entry["assignments"][label])


def run_metrics(dirs, out_json):
    if not dirs:
        return None
    r = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "scripts", "harness_metrics.py"), *dirs,
                        "--json", out_json], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"harness_metrics exit {r.returncode}: {r.stderr[-300:]}")
    return out_json


def run_dirs(work: str) -> list:
    """Every collected run directory: <work>/<launch>/<label>/runs/<run>/ holding run_state.json or metadata.json."""
    out = []
    for dp, dirs, files in os.walk(work):
        dirs.sort()
        if os.path.basename(os.path.dirname(dp)) == "runs" and ({"run_state.json", "metadata.json"} & set(files)):
            out.append(dp)
            dirs[:] = []
    return sorted(out)


def collect_step(blob, name, state, now, work, fetch=fetch_from_storage, metrics=run_metrics) -> None:
    """Try every enqueued label not yet collected (at most every COLLECT_EVERY_S); archive each new one to blob."""
    import results_store
    import shutil
    for lname, ent in sorted((state.get("collect") or {}).items()):
        for label, a in sorted(ent["entry"]["assignments"].items()):
            lab = ent["labels"].setdefault(label, {})
            if lab.get("launch_id") != a.get("launch_id"):   # a relaunch: the earlier launch's copy must not mix in
                shutil.rmtree(os.path.join(work, lname, label), ignore_errors=True)
                lab.clear()
                lab.update(state="waiting", launch_id=a.get("launch_id"))
            if lab.get("state") == "collected" or now - lab.get("tried", -1e18) < COLLECT_EVERY_S:
                continue
            lab["tried"] = now
            try:
                meta = fetch(ent["entry"], label, work)
            except (Exception, SystemExit) as e:  # noqa: BLE001 -- not stored yet, or unreadable: tried again later
                lab["note"] = f"{type(e).__name__}: {str(e)[:160]}"
                continue
            src = os.path.join(work, lname, label)
            with tempfile.TemporaryDirectory() as td:
                tgz = os.path.join(td, f"{label}.tar.gz")
                skipped = results_store.write_archive(src, label, tgz)
                sha = results_store.sha256_file(tgz)
                dst = f"{paths(name)['collected']}{lname}/{label}.tar.gz"
                blob.put_file(tgz, dst)
            lab.update(state="collected", blob=dst, sha256=sha, at=utc(now), note="",
                       exit_code=(meta or {}).get("exit_code"), left_out=len(skipped))
            state["metrics_due"] = True
    if state.get("metrics_due"):              # kept until metrics.json is stored, so a failed run is retried
        with tempfile.TemporaryDirectory() as td:
            out = metrics(run_dirs(work), os.path.join(td, "metrics.json"))
            if out:
                blob.put_file(out, paths(name)["metrics"])
                state["metrics_at"] = utc(now)
        state["metrics_due"] = False


def valid_entry(pl) -> dict:
    """A collect request's payload, or ValueError: names that stay inside their directory, a launch id per label."""
    a = pl.get("assignments") if isinstance(pl, dict) else None
    if not (_NAME.fullmatch(str(pl.get("name", ""))) and isinstance(pl.get("storage_prefix"), str) and
            isinstance(a, dict) and a and all(_LABEL.fullmatch(str(k)) and isinstance(v, dict) and v.get("launch_id")
                                               for k, v in a.items())):
        raise ValueError("collect request: needs a plain name, storage_prefix and a launch_id per plain label")
    return pl


# ---- (d) launches ----------------------------

LAUNCH_MAX_S = 45 * 60    # a launch child past this is ended and read as run_cluster's exit 4 (launch_watch's bound)
EVAL_SCOPE = "ev"
SUBMITTED = (0, 3, 4, 5)  # run_cluster launch exits after which something may run: never read as failed
STOP_RULES: dict = {}     # none yet: a request with stop rules is refused until they are enforced
_SHA = re.compile(r"[0-9a-f]{40}")
_CONFIG = re.compile(r"cloud/configs/eval_[a-z0-9_]+\.py")
_TAG = re.compile(r"[a-z0-9][a-z0-9-]{0,19}")
_SCOPE = re.compile(r"[a-z0-9]{1,16}")


def valid_launch(pl) -> dict:
    """A launch request's payload, or ValueError."""
    pl = pl if isinstance(pl, dict) else {}
    stop, reps = pl.get("stop") or {}, pl.get("reps")
    num = lambda k, v: (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0  # noqa: E731
                        and (STOP_RULES[k] is float or isinstance(v, int)))
    if not (_CONFIG.fullmatch(str(pl.get("config", ""))) and _TAG.fullmatch(str(pl.get("tag", "")))
            and isinstance(reps, int) and not isinstance(reps, bool) and 1 <= reps <= 20
            and _SHA.fullmatch(str(pl.get("commit", ""))) and isinstance(stop, dict) and set(stop) <= set(STOP_RULES)
            and all(num(k, v) for k, v in stop.items()) and isinstance(pl.get("budget") or {}, dict)):
        raise ValueError("launch request: needs cloud/configs/eval_<q>.py, a tag (a-z, 0-9, -), 1-20 reps, a full "
                         f"commit sha and positive stop rules among {sorted(STOP_RULES)}")
    return {"config": pl["config"], "tag": pl["tag"], "reps": reps, "commit": pl["commit"],
            "stop": {k: STOP_RULES[k](v) for k, v in stop.items()}, "budget": pl.get("budget")}


def make_tree(sha, root, url=f"https://github.com/{GH_REPO}.git") -> str:
    """A clean checkout of `sha` at <root>/trees/<sha>, made here: cloned when absent, and cloned again when anything in
    it differs from the commit (an edit, an untracked or an ignored file), so a launch never ships a dirty tree."""
    import shutil
    d = os.path.join(root, "trees", sha)
    git = lambda *x: subprocess.run(["git", "-C", d, *x], capture_output=True, text=True, timeout=600)  # noqa: E731

    def clean():
        h, s = git("rev-parse", "HEAD"), git("status", "--porcelain", "--ignored", "--untracked-files=all")
        return h.returncode == 0 and h.stdout.strip() == sha and s.returncode == 0 and not s.stdout.strip()
    if os.path.isdir(d) and clean():
        return d
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.dirname(d), exist_ok=True)
    r = subprocess.run(["git", "clone", "--quiet", "--no-checkout", url, d], capture_output=True, text=True,
                       timeout=1800)
    if r.returncode:
        raise RuntimeError(f"git clone failed: {r.stderr.strip()[-200:]}")
    r = git("checkout", "--quiet", "--detach", sha)
    if r.returncode or not clean():
        raise RuntimeError(f"no clean checkout of {sha[:9]}: {r.stderr.strip()[-200:]}")
    return d


def load_eval(e, tree):
    """The request's RunConfig, loaded from `tree` with EVAL_TAG / EVAL_REPS / EVAL_TREE set as the launch sets them
    (no bytecode written into the tree, and no `_eval` module kept from another tree)."""
    import run_cluster as rc
    keys, dwb = ("EVAL_TAG", "EVAL_REPS", "EVAL_TREE"), sys.dont_write_bytecode
    saved = {k: os.environ.get(k) for k in keys}
    sys.modules.pop("_eval", None)
    try:
        sys.dont_write_bytecode = True
        os.environ.update(EVAL_TAG=e["tag"], EVAL_REPS=str(e["reps"]), EVAL_TREE=tree)
        return rc.load_config(os.path.join(tree, e["config"]))
    finally:
        sys.dont_write_bytecode = dwb
        sys.modules.pop("_eval", None)
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def launch_env(e, tree, inv) -> dict:
    return {**os.environ, "EVAL_TAG": e["tag"], "EVAL_REPS": str(e["reps"]), "EVAL_TREE": tree,
            "MA_LAUNCH_INVOCATION": inv, "PYTHONDONTWRITEBYTECODE": "1"}


def signed_budget(scope, budget_dir=None, load=None) -> dict:
    """On the laptop: the scope's signed record as it is on disk, sent with a launch request only once budget.py's own
    checks pass here (signature, scope, expiry, and the high-water mark that refuses an older record restored by
    mistake). SystemExit otherwise: a request never carries a record the laptop would refuse."""
    import budget
    p = os.path.join(budget_dir or budget.BUDGET_DIR, f"budget_{scope}.json")
    if not os.path.exists(p):
        raise SystemExit(f"no budget record for scope {scope} on this machine: the owner signs one with "
                         "`budget.py set`; refusing")
    try:
        (load or budget.load_budget)(scope)
    except budget.BudgetError as e:
        raise SystemExit(f"the {scope} budget record does not pass this machine's checks: {e}; refusing") from None
    return json.load(open(p))


def install_budget(scope, rec, budget_dir=None, load=None) -> bool:
    """On the VM: put a signed record where budget.py reads it, only if budget.py's checks then pass here for this
    scope (signature, scope, expiry, high-water mark); otherwise the record it had (or none) is put back. True when
    installed, False when it was already there; ValueError when it does not verify."""
    import budget
    if not (_SCOPE.fullmatch(str(scope)) and isinstance(rec, dict)):
        raise ValueError("a budget record needs a plain scope name and a record")
    p = os.path.join(budget_dir or budget.BUDGET_DIR, f"budget_{scope}.json")
    old = open(p).read() if os.path.exists(p) else None
    if old is not None and json.loads(old) == rec:
        return False
    budget._write_private(p, json.dumps(rec, indent=1) + "\n")
    try:
        (load or budget.load_budget)(scope)
    except Exception as e:  # noqa: BLE001 -- BudgetError or an unreadable record: the previous state is restored
        if old is None:
            os.remove(p)
        else:
            budget._write_private(p, old)
        raise ValueError(f"the {scope} budget record does not verify on this VM ({type(e).__name__}: "
                         f"{str(e)[:120]}): not installed") from None
    return True


class Cloud:
    """What the loop does to the cloud. Not used by the self-test, which passes FakeCloud."""

    def __init__(self, work, a):
        self.work, self.a = work, a

    def disabled(self):
        miss = [f"${n}" for n in ("DASK_COILED__TOKEN", "ANTHROPIC_API_KEY") if not os.environ.get(n)]
        import shutil
        return (f"no {' or '.join(miss)} on the VM (`start` passes them when the laptop holds them)" if miss
                else None if shutil.which("git") else "no git on the VM")

    def tree(self, sha):
        return make_tree(sha, self.work)

    def config(self, e, tree):
        c = load_eval(e, tree)
        return {"name": c.name, "budget_scope": c.budget_scope, "cap_usd": c.cap_usd,
                "storage_prefix": c.storage_prefix, "storage_account": c.storage_account,
                "run_timeout_hours": c.run_timeout_hours, "max_rounds": c.max_rounds}

    def install_budget(self, scope, rec):
        return install_budget(scope, rec)

    def start_launch(self, e, tree, inv, log):
        os.makedirs(os.path.dirname(log), exist_ok=True)
        return subprocess.Popen([sys.executable, os.path.join(HERE, "run_cluster.py"), os.path.join(tree, e["config"]),
                                 "launch"], cwd=REPO_ROOT, env=launch_env(e, tree, inv), stdin=subprocess.DEVNULL,
                                stdout=open(log, "ab"), stderr=subprocess.STDOUT)

    def records(self, e, tree):
        c = load_eval(e, tree)
        rd = lambda p: json.load(open(p)) if os.path.exists(p) else {}  # noqa: E731
        return rd(c.clusters_file), rd(c.state_file)

    def stop(self, name, cid, why):
        import launch_watch
        launch_watch.CoiledApi().stop(name, cid, why)       # vm_job._stop_by_id: by id, bounded



def launch_step(blob, name, state, now, work, cloud, handles, allow_new=True) -> None:
    """Each finished launch child: read its records, then the launch's collection is enqueued (submitted) or its own
    failed record's ids are stopped (run_cluster's rule). Then at most one queued request is launched: from a clean
    checkout of its commit, through `run_cluster.py <config> launch` (its budget guard, scope ev), once: the request
    is recorded as launching before the child starts, and one found launching with no child is never relaunched."""
    for rid, e in sorted(state["launch"].items()):
        if e["state"] != "launching":
            continue
        h = handles.get(rid)
        if h is None:
            e.update(state="interrupted", note="the loop restarted during the launch: not relaunched; read its "
                     "clusters.json by hand")
            continue
        code = h.poll()
        if code is None and now - e["started_at"] <= LAUNCH_MAX_S:
            continue
        if code is None:
            h.kill()
            code = 4                       # submitted, not confirmed: watch, never read as failed
        handles.pop(rid)
        e["exit_code"] = code
        log = os.path.join(work, "launches", rid, "launch.log")
        if os.path.exists(log):
            blob.put_file(log, f"{paths(name)['launches']}{rid}/launch.log")
        clusters, assign = cloud.records(e, cloud.tree(e["commit"]))
        mine = clusters.get("invocation") == e["invocation"]
        ids = {r: {"name": clusters[r]["name"], "id": clusters[r]["id"]} for r in ("run", "keeper", "overflow")
               if mine and isinstance(clusters.get(r), dict) and clusters[r].get("id") is not None}
        e.update(clusters=ids, clusters_rec=clusters if mine else {})
        if code in SUBMITTED:
            asg = {k: v for k, v in (assign or {}).items() if isinstance(v, dict) and v.get("launch_id")}
            e.update(state="launched", launch_id=clusters.get("launch_id") if mine else None, assignments=asg,
                     launched_at=now, launched_utc=utc(now), labels={}, note=f"run_cluster launch exit {code}")
            if asg:
                ent = valid_entry({"name": e["name"], "storage_prefix": e["storage_prefix"],
                                   "storage_account": e["storage_account"], "assignments": asg})
                state["collect"].setdefault(e["name"], {"labels": {}})["entry"] = ent
        else:
            failed = mine and clusters.get("status") == "failed"
            for r, c in sorted(ids.items() if failed else ()):
                cloud.stop(c["name"], c["id"], f"supervisor: launch {rid} failed")
            e.update(state="failed", note=f"run_cluster launch exit {code}; "
                     + (f"stopped this invocation's failed record's ids {sorted(c['id'] for c in ids.values())}"
                        if failed else "no failed record of this invocation: nothing stopped"))
    queued = [rid for rid, e in sorted(state["launch"].items()) if e["state"] == "queued"]
    why = cloud.disabled() if queued else None
    if not queued or why or not allow_new or handles:
        for rid in queued:
            state["launch"][rid]["note"] = ("held: " + why if why else "held: this job ends soon"
                                            if not allow_new else "waiting for the launch before it")
        return
    rid = queued[0]
    e = state["launch"][rid]
    try:
        tree = cloud.tree(e["commit"])
        cfg = cloud.config(e, tree)
        if cfg["budget_scope"] != EVAL_SCOPE or not _NAME.fullmatch(str(cfg["name"])):
            raise ValueError(f"budget scope {cfg['budget_scope']!r} and name {cfg['name']!r}: an eval launch is in "
                             f"scope {EVAL_SCOPE}")
        if e.get("budget"):
            cloud.install_budget(cfg["budget_scope"], e["budget"])
    except Exception as ex:  # noqa: BLE001 -- refused once, never retried every loop
        e.update(state="refused", note=f"{type(ex).__name__}: {str(ex)[:200]}")
        return
    e.update(cfg, state="launching", invocation=f"sv-{rid}", started_at=now, started_utc=utc(now), note="")
    blob.put_json(paths(name)["state"], state)     # before the child: a restart finds it launching, never queued
    try:
        handles[rid] = cloud.start_launch(e, tree, e["invocation"], os.path.join(work, "launches", rid, "launch.log"))
    except Exception as ex:  # noqa: BLE001 -- the launcher did not start: nothing was submitted
        e.update(state="failed", note=f"the launcher did not start: {type(ex).__name__}: {str(ex)[:160]}")


_HIDDEN = ("budget", "assignments", "clusters_rec", "started_at", "launched_at")


def tick(blob, name, state, now, work, gh=None, fetch=fetch_from_storage, metrics=run_metrics, host="",
         merge_why="no GitHub token ($GH_TOKEN) on the VM: merges are held", cloud=None, handles=None,
         allow_launch=True) -> dict:
    """One pass: ingest new requests, one merge step, launches, collection, then state.json and status.json. A failing part is recorded in errors and the rest still runs; status is always written."""
    p, state = paths(name), {"seen": [], "merge": [], "merge_done": [], "collect": {}, "acks": [], "errors": [],
                             "launch": {}, **(state or {})}
    handles = {} if handles is None else handles

    def err(where, e):
        state["errors"] = (state["errors"] + [{"at": utc(now), "where": where,
                                                "error": f"{type(e).__name__}: {str(e)[:200]}"}])[-KEEP:]
    try:
        names = blob.names(p["requests"])
    except Exception as e:  # noqa: BLE001
        names = []
        err("requests", e)
    seen = set(state["seen"])
    for b in names:
        rid = b[len(p["requests"]):-len(".json")]
        if rid in seen:
            continue
        state["seen"].append(rid)            # a bad request is recorded once, never retried every loop
        try:
            req = blob.get_json(b) or {}
            kind, pl = req.get("kind"), req.get("payload") or {}
            if kind == "merge":
                queued = {i["pr"] for i in state["merge"]}
                state["merge"] += [{"pr": int(n), "state": "queued", "request": rid} for n in pl.get("prs", [])
                                   if int(n) not in queued]
            elif kind == "collect":
                state["collect"].setdefault(valid_entry(pl)["name"], {"labels": {}})["entry"] = pl
            elif kind == "launch":
                state["launch"][rid] = {**valid_launch(pl), "state": "queued", "request": rid, "note": ""}
            elif kind != "noop":
                raise ValueError(f"unknown kind {kind!r}")
            state["acks"] = (state["acks"] + [{"request": rid, "kind": kind, "at": utc(now)}])[-KEEP:]
        except Exception as e:  # noqa: BLE001
            err(f"request {rid}", e)
    if gh is not None and state["merge"]:
        try:
            item = merge_step(gh, state["merge"][0], now)
            if item["state"] not in ("queued", "waiting"):
                state["merge_done"] = (state["merge_done"] + [state["merge"].pop(0)])[-KEEP:]
        except Exception as e:  # noqa: BLE001
            err("merge", e)
    if cloud is not None:
        try:
            launch_step(blob, name, state, now, work, cloud, handles, allow_new=allow_launch)
        except Exception as e:  # noqa: BLE001
            err("launch", e)
    try:
        collect_step(blob, name, state, now, work, fetch=fetch, metrics=metrics)
    except Exception as e:  # noqa: BLE001
        err("collect", e)
    state["loops"] = int(state.get("loops", 0)) + 1
    launch_why = "no cloud access in this loop" if cloud is None else cloud.disabled() or ""
    paused = []
    for d in run_dirs(work):
        try:
            st = str((json.load(open(os.path.join(d, "metadata.json"))) or {}).get("status", ""))
            paused += [{"run": os.path.relpath(d, work), "status": st}] if st.startswith(("paused", "awaiting")) else []
        except (OSError, ValueError, AttributeError):
            pass
    status = {"name": name, "host": host, "heartbeat_utc": utc(now), "loops": state["loops"],
              "merge": {"enabled": gh is not None, "why": "" if gh is not None else merge_why,
                        "queue": state["merge"], "done": state["merge_done"][-10:]},
              "collect": {k: {"labels": v["labels"]} for k, v in state["collect"].items()},
              "launch": {"enabled": not launch_why, "why": launch_why,
                         "requests": {r: {k: v for k, v in e.items() if k not in _HIDDEN}
                                      for r, e in state["launch"].items()}},
              "metrics_at": state.get("metrics_at"), "paused": paused,
              "acks": state["acks"][-10:], "errors": state["errors"][-10:]}
    blob.put_json(p["state"], state)
    blob.put_json(p["status"], status)
    return state


def loop(a):
    blob, p, work = Blob(a.account), paths(a.name), os.path.abspath(os.path.expanduser(a.work))
    gh, why = None, "no GitHub token ($GH_TOKEN) on the VM: merges are held"
    if os.environ.get("GH_TOKEN"):
        try:
            gh = GitHub(os.environ["GH_TOKEN"].strip())
        except ValueError as e:
            why = str(e)
    for i in range(21):                       # a storage blip at start-up must not end the supervisor
        try:
            state = blob.get_json(p["state"]) or {}
            break
        except Exception as e:  # noqa: BLE001
            if i == 20:
                raise
            print(f"state.json unreadable ({type(e).__name__}); retrying", flush=True)
            time.sleep(30)
    host, deadline = f"{os.uname().nodename}:{os.getpid()}", time.time() + a.max_hours * 3600
    cloud, handles = Cloud(work, a), {}
    while time.time() < deadline:
        now = time.time()
        try:
            state = tick(blob, a.name, state, now, work, gh=gh, host=host, merge_why=why, cloud=cloud,
                         handles=handles, allow_launch=deadline - now > LAUNCH_MAX_S)
        except Exception as e:  # noqa: BLE001 -- the loop outlives one bad pass (storage blip, DNS)
            print(f"tick failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        time.sleep(min(sleep_for(state, time.time(), a.every), max(0.0, deadline - time.time())))
    return 0


def sleep_for(state, now, every) -> float:
    """Seconds until the next due check, at most `every`: a launch child is polled every 15 s."""
    due = [float(every)] + [15.0 for e in (state.get("launch") or {}).values() if e.get("state") == "launching"]
    return max(1.0, min(due))


def status_lines(st: dict | None, now: float) -> tuple[list, int]:
    """Readable status and an exit code: 0 live, 3 stale or absent."""
    if not st:
        return ["no status.json: the supervisor has never run under this name"], 3
    import calendar
    age = now - calendar.timegm(time.strptime(st["heartbeat_utc"], "%Y-%m-%dT%H:%M:%SZ"))
    live = age <= STALE_S
    out = [f"{st.get('name', '?')} on {st.get('host', '?')}: last loop {st['heartbeat_utc']} ({age / 60:.1f} min ago,"
           f" loop {st.get('loops')})" + ("" if live else " STALE: the loop is not running")]
    m = st.get("merge") or {}
    out.append(f"merge train: {'enabled' if m.get('enabled') else 'disabled (' + m.get('why', '') + ')'}")
    out += [f"  PR {i['pr']:<6} {i['state']:<15} {i.get('note', '')}" for i in (m.get("queue") or []) +
            (m.get("done") or [])]
    for lname, ent in sorted((st.get("collect") or {}).items()):
        for label, lab in sorted(ent.get("labels", {}).items()):
            out.append(f"collect {lname}/{label}: {lab.get('state')} {lab.get('note', '')}".rstrip())
    la = st.get("launch") or {}
    if la.get("requests"):
        out.append(f"launches: {'enabled' if la.get('enabled') else 'held (' + str(la.get('why')) + ')'}")
    for rid, e in sorted((la.get("requests") or {}).items()):
        out.append(f"launch {rid} {e.get('config')} tag {e.get('tag')} x{e.get('reps')} @ {str(e.get('commit'))[:9]}: "
                   f"{e.get('state')} {e.get('launch_id') or ''} {e.get('note') or ''}".rstrip())
    out.append(f"metrics.json: {st.get('metrics_at') or 'not yet'}")
    out += [f"paused run: {x['run']} ({x['status']}); resume is not automated" for x in st.get("paused") or []]
    out += [f"ack {x['kind']} request {x['request']} at {x['at']}" for x in st.get("acks") or []]
    out += [f"error {x['at']} {x['where']}: {x['error']}" for x in st.get("errors") or []]
    return out, 0 if live else 3


def pull(blob, name, dest, now=None) -> list:
    """Every collected tarball not already here (or changed), sha256-checked, extracted with the data filter into
    <dest>/<launch>/<label>/; metrics.json too. Returns what was fetched."""
    import results_store
    os.makedirs(dest, exist_ok=True)
    ip = os.path.join(dest, ".pulled.json")
    index = json.load(open(ip)) if os.path.exists(ip) else {}
    st = blob.get_json(paths(name)["status"]) or {}
    got = []
    for lname, ent in sorted((st.get("collect") or {}).items()):
        for label, lab in sorted(ent.get("labels", {}).items()):
            if (lab.get("state") != "collected" or index.get(lab["blob"]) == lab["sha256"]
                    or not (_NAME.fullmatch(lname) and _LABEL.fullmatch(label))):
                continue
            target = os.path.join(dest, lname)
            os.makedirs(target, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target) as td:
                tgz = os.path.join(td, "x.tar.gz")
                blob.get_file(lab["blob"], tgz)
                if results_store.sha256_file(tgz) != lab["sha256"]:
                    print(f"{lab['blob']}: sha256 does not match the record: not extracted")
                    continue
                with tarfile.open(tgz, mode="r:gz") as t:
                    t.extractall(td, filter="data")
                if os.path.exists(os.path.join(target, label)):
                    os.rename(os.path.join(target, label), os.path.join(target, f"{label}.before_{int(time.time())}"))
                os.rename(os.path.join(td, label), os.path.join(target, label))
            index[lab["blob"]] = lab["sha256"]
            got.append(lab["blob"])
    lp = paths(name)["launches"]
    for b in blob.names(lp):          # each launch's launch.log (small; fetched when new)
        rid, _, fn = b[len(lp):].partition("/")
        if _LABEL.fullmatch(rid) and fn == "launch.log":
            dst = os.path.join(dest, "launches", rid, fn)
            if not os.path.exists(dst):           # written once
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                blob.get_file(b, dst)
                got.append(b)
    json.dump(index, open(ip, "w"), indent=1)
    if got and st.get("metrics_at"):
        blob.get_file(paths(name)["metrics"], os.path.join(dest, "metrics.json"))
    return got


def start_commands(a, job_id, token_present, coiled=False, anthropic=False) -> list:
    """The two vm_job commands `start` runs: up (2-vCPU worker) and start (the loop as a job). Secrets pass by name only
    (--secret-env): vm_job reads each where the laptop keeps it, and this process never holds them in its
    environment."""
    vm = [sys.executable, "cloud/vm_job.py"]
    scope = ["--budget-scope", a.budget_scope] if a.budget_scope else []
    acct = ["--store-account", a.account] if a.account else []
    up = vm + ["up", a.name, "--cpu", "2", "--memory", "8 GiB", "--idle-timeout", "20 minutes",
               "--cap-usd", str(a.cap_usd)] + scope
    start = (vm + ["start", a.name, job_id, "--tree", ".", "--max-hours", str(a.max_hours + 0.25)] + acct
             + ["--cap-usd", str(a.cap_usd)] + scope
             + (["--secret-env", "GH_TOKEN"] if token_present else [])
             + (["--secret-env", "DASK_COILED__TOKEN"] if coiled else [])
             + (["--secret-env", "ANTHROPIC_API_KEY"] if anthropic else [])
             + ["--", "python3", "cloud/supervisor.py", "loop", "--name", a.name, "--every", str(int(a.every)),
                "--max-hours", str(a.max_hours)] + (["--account", a.account] if a.account else []))
    return [up, start]


def _laptop_secrets() -> tuple:
    """(GitHub token from the keychain, whether vm_job finds a Coiled token, whether it finds an Anthropic key). The
    last two are presence only: their values stay where they are (dask config, .env) until vm_job reads them."""
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True,
                       text=True)                      # the owner's one-time step stores it there
    gh = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    import vm_job
    return gh, bool(vm_job._secret_value("DASK_COILED__TOKEN")), bool(vm_job._secret_value("ANTHROPIC_API_KEY"))


def cmd_start(a):
    tok, co, an = _laptop_secrets()
    job_id = "sv-" + time.strftime("%Y%m%d-%H%M%S")
    env = {**os.environ, **({"GH_TOKEN": tok} if tok else {})}
    print(f"merge train: {'enabled (token from the keychain)' if tok else 'disabled (no token in the keychain)'}; "
          f"launches: {'enabled' if co and an else 'held (no Coiled token or no Anthropic key here)'}")
    for cmd in start_commands(a, job_id, bool(tok), coiled=co, anthropic=an):
        print("$ " + " ".join(cmd), flush=True)
        r = subprocess.run(cmd, cwd=REPO_ROOT, env=env)
        if r.returncode:
            return r.returncode
    print(f"started {job_id}; read it with `python3 cloud/supervisor.py status --name {a.name}`")
    return 0


def launch_request(a, repo=REPO_ROOT) -> dict:
    """enqueue-launch's payload, checked here first: the config loads with this tag and reps and is in scope ev,
    the commit is a full sha known to this checkout, and the scope's signed budget record goes with it."""
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", f"{a.commit}^{{commit}}"], capture_output=True,
                       text=True)
    if r.returncode:
        raise SystemExit(f"{a.commit}: not a commit of {repo}")
    e = valid_launch({"config": os.path.relpath(os.path.abspath(a.config), repo), "tag": a.tag, "reps": a.reps,
                      "commit": r.stdout.strip(), "stop": {}})
    cfg = load_eval(e, repo)
    if cfg.budget_scope != EVAL_SCOPE:
        raise SystemExit(f"{cfg.name}: budget scope {cfg.budget_scope!r}; the supervisor launches scope {EVAL_SCOPE}")
    return {**e, "budget": signed_budget(cfg.budget_scope)}     # the scope this launch's guard checks


def stop_own(name, live_id=None, stop=None):
    """Stop this supervisor's cluster: the live cluster with exactly this name, by its id."""
    cid = live_id(name)
    if cid is None:
        return f"{name}: no live cluster of this name"
    stop(name, cid, f"supervisor stop {name}")
    return f"{name}: stop requested for cluster {cid}"


def _assignments(config_path):
    import run_cluster as rc
    cfg = rc.load_config(config_path)
    if not os.path.exists(cfg.state_file):
        raise SystemExit(f"no {cfg.state_file}: this machine did not launch {cfg.name}")
    return {"name": cfg.name, "storage_prefix": cfg.storage_prefix, "storage_account": cfg.storage_account,
            "assignments": json.load(open(cfg.state_file))}


class FakeBlob:
    def __init__(self):
        self.d = {}
    def put_json(self, blob, obj):
        self.d[blob] = json.dumps(obj, default=str).encode()
    def get_json(self, blob):
        return json.loads(self.d[blob]) if blob in self.d else None
    def put_file(self, path, blob):
        self.d[blob] = open(path, "rb").read()
    def get_file(self, blob, path):
        open(path, "wb").write(self.d[blob])
    def names(self, prefix):
        return sorted(k for k in self.d if k.startswith(prefix))


class FakeGitHub:
    """{pr: {state, merged, head, base, checks, behind, update_ok}}; every call is logged."""
    def __init__(self, prs):
        self.prs, self.calls = prs, []
    def _by_head(self, head):
        return next(p for p in self.prs.values() if p["head"] == head)
    def pr(self, n):
        self.calls.append(("pr", n))
        if n not in self.prs:
            raise LookupError(f"PR {n} not found")
        return dict(self.prs[n])
    def behind(self, base, head):
        return self._by_head(head).get("behind", False)
    def checks(self, sha):
        self.calls.append(("checks", sha))
        return list(self._by_head(sha).get("checks", []))
    def update_branch(self, n, sha):
        self.calls.append(("update", n, sha))
        return self.prs[n].get("update_ok", True)
    def merge(self, n, sha):
        self.calls.append(("merge", n, sha))
        self.prs[n].update(state="closed", merged=True)
        return True


class FakeCloud:
    """The loop's cloud actions, recorded (no Coiled, GitHub or storage call). `code`: the launch child's exit (None:
    still running)."""
    def __init__(self, why=None, code=0, scope="ev", mine=True, status="launched", dirty=False):
        self.why, self.code, self.scope = why, code, scope
        self.mine, self.status, self.dirty, self.calls, self.inv = mine, status, dirty, [], None
        self.seen_state = None
    def disabled(self):
        return self.why
    def tree(self, sha):
        if self.dirty:
            raise RuntimeError(f"no clean checkout of {sha[:9]}")
        return f"/trees/{sha}"
    def config(self, e, tree):
        return {"name": "ev-flu-t1-gcc", "budget_scope": self.scope, "cap_usd": 70,
                "storage_prefix": "runs/evals/t1/flu_vaccine", "storage_account": "acct", "run_timeout_hours": 24,
                "max_rounds": 3}
    def install_budget(self, scope, rec):
        self.calls.append(("budget", scope))
    def start_launch(self, e, tree, inv, log, blob=None):
        self.calls.append(("launch", e["config"], e["tag"], e["reps"], tree, inv))
        self.inv = inv
        return types.SimpleNamespace(poll=lambda: self.code, kill=lambda: self.calls.append(("kill",)))
    def records(self, e, tree):
        n = "ev-flu-t1-gcc"
        return ({"invocation": self.inv if self.mine else "other", "launch_id": "L1", "status": self.status,
                 "run": {"name": n, "id": 101}, "keeper": {"name": n + "-keeper", "id": 102}},
                {"R1": {"launch_id": "L1", "worker": "w1"}, "R2": {"launch_id": "L1", "worker": "w2"}})
    def stop(self, name, cid, why):
        self.calls.append(("stop", name, cid))

def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def guard(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001 -- a case that raises fails, it does not end the self-test
            return f"raised {type(e).__name__}: {e}"

    N, T0 = "sv-test", 1_800_000_000.0
    P = paths(N)
    PR = lambda head, checks=(), **k: {"state": "open", "merged": False, "head": head, "base": "v2",  # noqa: E731
                                       "checks": list(checks), **k}

    def run(b, times, state=None, reload=False, **kw):
        """tick at each time in one work dir; with reload, each tick starts from the stored state.json."""
        st = state or {}
        with tempfile.TemporaryDirectory() as td:
            for t in times:
                st = guard(tick, b, N, (b.get_json(P["state"]) or {}) if reload else st, t, td, **kw)
        return st if isinstance(st, dict) else {"raised": st}

    def lab_of(st, name="ev-x", label="q"):
        return st.get("collect", {}).get(name, {}).get("labels", {}).get(label, {})

    # SV1 requests are write-once and distinct; SV2 each is acknowledged once, status rewritten every loop
    b = FakeBlob()
    r1, r2 = guard(enqueue, b, N, "noop", {}, T0), guard(enqueue, b, N, "noop", {}, T0)
    ok(isinstance(r1, str) and r1 != r2 and len(b.names(P["requests"])) == 2, "SV1 each enqueue is its own blob")
    st, status = run(b, [T0 + 1, T0 + 2]), b.get_json(P["status"]) or {}
    ok(len(st.get("acks", [])) == 2 and status.get("heartbeat_utc") == utc(T0 + 2) and status.get("loops") == 2,
       f"SV2 noop requests acknowledged once each, status rewritten ({status})")
    # SV3 no token: the merge queue is held, and status says why
    b = FakeBlob()
    guard(enqueue, b, N, "merge", {"prs": [11, 12]}, T0)
    run(b, [T0 + 1])
    m = (b.get_json(P["status"]) or {}).get("merge", {})
    ok(m.get("enabled") is False and [i["pr"] for i in m.get("queue", [])] == [11, 12] and "token" in m.get("why", ""),
       f"SV3 without a token the queue is held ({m})")
    # SV4 behind: update-branch once with the expected head, then wait; a failed update skips
    gh = FakeGitHub({7: PR("aaa", behind=True)})
    it = {"pr": 7, "state": "queued"}
    guard(merge_step, gh, it, T0), guard(merge_step, gh, it, T0 + 60)
    ok([c for c in gh.calls if c[0] == "update"] == [("update", 7, "aaa")] and it.get("state") == "waiting",
       f"SV4 a PR behind its base is updated once, then waits ({gh.calls}, {it})")
    gh.prs[7]["update_ok"], it2 = False, {"pr": 7, "state": "queued"}
    guard(merge_step, gh, it2, T0)
    ok(it2.get("state") == "skipped" and "update" in it2.get("note", ""), f"SV4b a failed update skips ({it2})")
    # SV5 pending or no checks wait; a failure skips; all green merges at the head sha. SV6 merged/closed: no merge
    gh = FakeGitHub({1: PR("h1", ["success", "pending"]), 2: PR("h2", ["success", "failure"]), 3: PR("h3", ["success"]),
                     4: PR("h4"), 5: {**PR("h5"), "state": "closed", "merged": True}, 6: {**PR("h6"), "state": "closed"}})
    its = {n: {"pr": n, "state": "queued"} for n in gh.prs}
    for n in its:
        guard(merge_step, gh, its[n], T0)
    ok([its[n].get("state") for n in (1, 2, 3, 4)] == ["waiting", "skipped", "merged", "waiting"]
       and [c for c in gh.calls if c[0] == "merge"] == [("merge", 3, "h3")],
       f"SV5 pending/no checks wait, a failure skips, all green merges at the head sha ({its})")
    ok(its[5].get("state") == "already_merged" and its[6].get("state") == "skipped",
       f"SV6 merged or closed PRs are not merged again ({its[5]}, {its[6]})")
    # SV7 give up after GIVE_UP_S
    gh, i8 = FakeGitHub({8: PR("h8", ["pending"])}), {"pr": 8, "state": "queued"}
    guard(merge_step, gh, i8, T0), guard(merge_step, gh, i8, T0 + GIVE_UP_S + 1)
    ok(i8.get("state") == "skipped" and "gave up" in i8.get("note", ""), f"SV7 a PR waiting too long is skipped ({i8})")
    # SV8 one PR at a time; SV15 in order, and a state reloaded from storage does not merge again
    b, gh = FakeBlob(), FakeGitHub({21: PR("x1", ["pending"]), 22: PR("x2", ["success"])})
    guard(enqueue, b, N, "merge", {"prs": [21, 22]}, T0)
    run(b, [T0 + 1], gh=gh)
    ok(not any(c[1] in (22, "x2") for c in gh.calls), f"SV8 one PR at a time ({gh.calls})")
    gh.prs[21]["checks"] = ["success"]
    run(b, [T0 + 100, T0 + 200, T0 + 300], reload=True, gh=gh)
    ok([c[1] for c in gh.calls if c[0] == "merge"] == [21, 22], f"SV15 merges in order, once ({gh.calls})")

    # SV9 collection: absent -> waiting (retried after COLLECT_EVERY_S); stored -> collected, archived, measured
    b, seen = FakeBlob(), {"fetch": 0, "metrics": []}
    entry = {"name": "ev-x", "storage_prefix": "runs/ev-x", "assignments": {"q": {"launch_id": "L1"}}}
    guard(enqueue, b, N, "collect", entry, T0)

    def fetch(e, label, work, ready=[False]):
        seen["fetch"] += 1
        if not ready[0]:
            raise FileNotFoundError("not stored yet")
        rd = os.path.join(work, e["name"], label, "runs", "20261122_q")
        os.makedirs(rd, exist_ok=True)
        json.dump({"status": "paused:awaiting_signoff"}, open(os.path.join(rd, "metadata.json"), "w"))
        json.dump({"state": "SIGN"}, open(os.path.join(rd, "run_state.json"), "w"))
        return {"exit_code": 2}

    def metrics(dirs, out):
        seen["metrics"].append(list(dirs))
        json.dump({"rows": [os.path.basename(d) for d in dirs]}, open(out, "w"))
        return out

    tgz = P["collected"] + "ev-x/q.tar.gz"
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0 + 1, td, fetch=fetch, metrics=metrics)
        st = guard(tick, b, N, st, T0 + 2, td, fetch=fetch, metrics=metrics)
        ok(lab_of(st).get("state") == "waiting" and seen["fetch"] == 1,
           f"SV9 a run not in storage yet waits, and is not retried before COLLECT_EVERY_S ({lab_of(st)})")
        fetch.__defaults__[0][0] = True
        st = guard(tick, b, N, st, T0 + 2 + COLLECT_EVERY_S, td, fetch=fetch, metrics=metrics)
        status = b.get_json(P["status"]) or {}
        ok(lab_of(st).get("state") == "collected" and tgz in b.d and lab_of(st).get("sha256") and P["metrics"] in b.d
           and seen["metrics"] and seen["metrics"][-1][0].endswith("runs/20261122_q"),
           f"SV9b a stored run is collected, archived and measured ({lab_of(st)}, {sorted(b.d)})")
        ok(any("20261122_q" in str(x) for x in status.get("paused", [])), f"SV9c status lists paused runs ({status})")
        st = guard(tick, b, N, st, T0 + 5000, td, fetch=fetch, metrics=metrics)
        ok(seen["fetch"] == 2, "SV9d a collected label is not fetched again")
        guard(enqueue, b, N, "collect", {**entry, "assignments": {"q": {"launch_id": "L2"}}}, T0 + 5001)
        st = guard(tick, b, N, st, T0 + 5002, td, fetch=fetch, metrics=metrics)
        ok(lab_of(st).get("launch_id") == "L2" and seen["fetch"] == 3, f"SV10 a relaunch is collected ({lab_of(st)})")
        # SV12 pull: each collected run once, sha256-checked before extraction
        with tempfile.TemporaryDirectory() as dest:
            got, again = guard(pull, b, N, dest), guard(pull, b, N, dest)
            ok(isinstance(got, list) and any(g.endswith("q.tar.gz") for g in got) and again == []
               and os.path.exists(os.path.join(dest, "ev-x", "q", "runs", "20261122_q", "run_state.json")),
               f"SV12 pull fetches each collected run once ({got}, {again})")
            b.d[tgz], st2 = b"corrupt", b.get_json(P["status"])
            lab_of(st2)["sha256"] = "changed"
            b.put_json(P["status"], st2)
            ok(guard(pull, b, N, dest) == [], "SV12b a tarball that does not match its recorded sha256 is not extracted")

    # SV11 a failing part is recorded and status is still written
    b = FakeBlob()
    guard(enqueue, b, N, "collect", entry, T0)
    run(b, [T0 + 1], fetch=lambda *a: 1 / 0, metrics=metrics)
    status = b.get_json(P["status"]) or {}
    ok(status.get("heartbeat_utc") == utc(T0 + 1) and "ZeroDivisionError" in json.dumps(status),
       f"SV11 a failing part is recorded and status still written ({status})")
    # SV13 status lines: live, stale, absent
    live = guard(status_lines, {"heartbeat_utc": utc(T0), "loops": 3, "merge": {"enabled": False, "queue": []}}, T0 + 30)
    stale = guard(status_lines, {"heartbeat_utc": utc(T0), "loops": 3}, T0 + STALE_S + 1)
    none = guard(status_lines, None, T0)
    ok(isinstance(live, tuple) and live[1] == 0 and stale[1] == 3 and none[1] == 3 and "STALE" in "\n".join(stale[0]),
       f"SV13 status says live, stale or absent ({live}, {stale}, {none})")
    # SV14 the token never reaches a stored blob
    b, tok = FakeBlob(), "github_pat_" + "Z" * 40
    gh = FakeGitHub({31: PR("t1", ["success"])})
    gh._token = tok
    guard(enqueue, b, N, "merge", {"prs": [31]}, T0)
    run(b, [T0 + 1], gh=gh)
    ok(b.d and not any(tok.encode() in v for v in b.d.values()), "SV14 the token is in no stored blob")
    # SV16 start: vm_job up, then start with the loop as the job; the token passes by name only
    a = argparse.Namespace(name=DEFAULT_NAME, cap_usd=15.0, max_hours=24.0, budget_scope=None, account="acct1", every=60)
    cmds = guard(start_commands, a, "sv-1", True)
    flat = [" ".join(c) for c in cmds] if isinstance(cmds, list) else [cmds, cmds]
    ok(len(flat) == 2 and " up " + DEFAULT_NAME in flat[0] and "--cpu 2" in flat[0]
       and " start " + DEFAULT_NAME + " sv-1" in flat[1] and "--secret-env GH_TOKEN" in flat[1]
       and "cloud/supervisor.py loop --name " + DEFAULT_NAME in flat[1] and "--cap-usd 15.0" in flat[1],
       f"SV16 start runs vm_job up + start with the loop ({flat})")
    cmds = guard(start_commands, a, "sv-1", False)
    ok(isinstance(cmds, list) and "--secret-env" not in " ".join(cmds[1]), "SV16b no token: no --secret-env")
    import budget
    ok(DEFAULT_NAME.startswith("sv-") and budget.scope_for_name(DEFAULT_NAME) == "sv",
       "SV16c the default name is in the supervisor's own budget scope, sv (owner 2026-11-22)")
    # SV17 stop: only this name's live cluster, by id
    stopped, rec = [], lambda n, cid, why: stopped.append((n, cid))
    guard(stop_own, DEFAULT_NAME, live_id=lambda n: 123 if n == DEFAULT_NAME else 999, stop=rec)
    r2 = guard(stop_own, DEFAULT_NAME, live_id=lambda n: None, stop=rec)
    ok(stopped == [(DEFAULT_NAME, 123)] and "no live cluster" in str(r2), f"SV17 stop by id, own name only ({stopped})")

    # Codex round 1. SV18 checks: every page; an unreadable status endpoint raises; the CI workflow must have run
    def gh_with(pages, status=(200, {"statuses": []}), wf=("completed", "success")):
        g = GitHub("tok_" + "a" * 20)
        runs = [] if wf is None else [{"path": CI_WORKFLOW, "status": wf[0], "conclusion": wf[1]}]
        page = lambda path: int(re.findall(r"[?&]page=(\d+)", path)[0])  # noqa: E731
        g._call = lambda m, path, body=None: ((200, {"check_runs": pages[page(path) - 1]}) if "check-runs" in path
                                              else status if path.endswith("/status") else (200, {"workflow_runs": runs}))
        return g
    green = {"status": "completed", "conclusion": "success"}
    two = gh_with([[green] * 100, [{"status": "completed", "conclusion": "failure"}]])
    ok("failure" in guard(two.checks, "s1"), "SV18 a failing check run on page two is seen")
    ok("RuntimeError" in str(guard(gh_with([[green]], status=(503, {})).checks, "s1")),
       "SV18b an unreadable commit-status endpoint raises (the PR waits)")
    ok("pending" in guard(gh_with([[green]], wf=None).checks, "s1")
       and "pending" in guard(gh_with([[green]], wf=("in_progress", None)).checks, "s1"),
       "SV18c green checks without a finished CI workflow run on the head are pending")
    r = guard(GitHub, "ghp_good\nX-Evil: 1")
    ok("ValueError" in str(r) and "ghp_good" not in str(r), f"SV19 a token with a newline is refused, unechoed ({r})")
    # SV20 bad requests (unreadable JSON, a traversing name, no assignments) are refused once; later ones processed
    b = FakeBlob()
    b.d[P["requests"] + "20000101T000000-aaaa.json"] = b"{not json"
    guard(enqueue, b, N, "collect", {"name": "../evil", "storage_prefix": "x", "assignments": {"q": {"launch_id": "L"}}})
    guard(enqueue, b, N, "collect", {"name": "ev-y", "storage_prefix": "x"})
    guard(enqueue, b, N, "noop", {})
    st = run(b, [T0 + 2, T0 + 3], fetch=fetch, metrics=metrics)
    ok(not st.get("collect") and [x["kind"] for x in st.get("acks", [])] == ["noop"] and len(st.get("errors", [])) == 3,
       f"SV20 bad requests are refused once each, later ones processed ({st})")
    b = FakeBlob()
    b.put_json(P["status"], {"collect": {"../evil": {"labels": {"q": {"state": "collected", "blob": "x", "sha256": "0"}}}}})
    with tempfile.TemporaryDirectory() as dest:
        ok(guard(pull, b, N, os.path.join(dest, "in")) == [] and os.listdir(dest) == ["in"],
           "SV20b pull ignores a launch name that would leave its directory")
    # SV21 a PR that does not exist is skipped; the next one proceeds
    b, gh = FakeBlob(), FakeGitHub({42: PR("y2", ["success"])})
    guard(enqueue, b, N, "merge", {"prs": [41, 42]}, T0)
    run(b, [T0 + 1, T0 + 2], gh=gh)
    ok(("merge", 42, "y2") in gh.calls, f"SV21 a missing PR is skipped and the next merged ({gh.calls})")
    # SV22 a relaunch's run replaces the earlier launch's local copy; SV23 a failed metrics run is retried
    b, calls = FakeBlob(), {"m": 0}

    def fetch2(e, label, work):
        rd = os.path.join(work, e["name"], label, "runs", "run_" + e["assignments"][label]["launch_id"])
        os.makedirs(rd, exist_ok=True)
        json.dump({"status": "completed"}, open(os.path.join(rd, "metadata.json"), "w"))

    def metrics2(dirs, out):
        calls["m"], calls["dirs"] = calls["m"] + 1, [os.path.basename(d) for d in dirs]
        if calls["m"] == 1:
            raise RuntimeError("upload blip")
        json.dump({}, open(out, "w"))
        return out
    with tempfile.TemporaryDirectory() as td:
        guard(enqueue, b, N, "collect", {**entry, "assignments": {"q": {"launch_id": "A"}}}, T0)
        st = guard(tick, b, N, {}, T0 + 1, td, fetch=fetch2, metrics=metrics2)
        st = guard(tick, b, N, st, T0 + 2, td, fetch=fetch2, metrics=metrics2)
        ok(calls["m"] == 2 and P["metrics"] in b.d, f"SV23 a failed metrics run is retried next loop ({calls})")
        guard(enqueue, b, N, "collect", {**entry, "assignments": {"q": {"launch_id": "B"}}}, T0 + 3)
        guard(tick, b, N, st, T0 + 4, td, fetch=fetch2, metrics=metrics2)
        ok(calls.get("dirs") == ["run_B"], f"SV22 a relaunch does not carry the earlier launch's run ({calls})")
    # SV24 processed requests are never replayed, however many there are
    b = FakeBlob()
    for i in range(1003):
        b.put_json(f"{P['requests']}{i:06d}.json", {"kind": "noop"})
    st = run(b, [T0, T0 + 1])
    ok(len(st.get("seen", [])) == 1003 and st["acks"][-1]["at"] == utc(T0), "SV24 no processed request is replayed")

    # ---- launches (SV25-SV30, SV41-SV42) ----
    import types
    from unittest import mock
    SHA = "ab" * 20
    KW = {"fetch": lambda *x: (_ for _ in ()).throw(FileNotFoundError("not stored yet")), "metrics": metrics}
    LREQ = {"config": "cloud/configs/eval_flu_vaccine.py", "tag": "t1", "reps": 2, "commit": SHA,
            "budget": {"record": {"scope": "ev", "nonce": "N0NCE"}, "sig": "s"}}

    def launched(cl, stop=None, extra=()):
        """A FakeBlob and a state with one request launched at T0 (ticks at T0 and T0 + 1)."""
        b, h = FakeBlob(), {}
        guard(enqueue, b, N, "launch", {**LREQ, "stop": stop or {}}, T0)
        for rq in extra:
            guard(enqueue, b, N, "launch", rq, T0)
        with tempfile.TemporaryDirectory() as td:
            st = guard(tick, b, N, {}, T0, td, cloud=cl, handles=h, **KW)
            st = guard(tick, b, N, st, T0 + 1, td, cloud=cl, handles=h, **KW)
        return b, st if isinstance(st, dict) else {"raised": st}

    def ticks(b, st, cl, times):
        with tempfile.TemporaryDirectory() as td:
            for t in times:
                st = guard(tick, b, N, st, t, td, cloud=cl, handles={}, **KW)
        return st if isinstance(st, dict) else {"raised": st}

    def req_of(st):
        return next(iter((st.get("launch") or {}).values()), {})

    # SV25 a launch request needs an eval config, a tag, 1-20 reps and a full sha; stop rules are not taken yet
    good = guard(valid_launch, LREQ)
    bad = [{**LREQ, "commit": "abc123"}, {**LREQ, "config": "../evil/eval_x.py"},
           {**LREQ, "config": "cloud/configs/nigeria_gf13_v2.py"}, {**LREQ, "tag": "Bad_Tag"}, {**LREQ, "reps": 0},
           {**LREQ, "stop": {"usd": 5}}, {**LREQ, "stop": {"tag_usd": 50}}]
    ok(isinstance(good, dict) and good["stop"] == {}
       and all("ValueError" in str(guard(valid_launch, x)) for x in bad), f"SV25 launch requests are validated ({good})")
    # SV26 without cloud access (no Coiled token / Anthropic key on the VM) launches are held, and status says why
    cl = FakeCloud(why="no $DASK_COILED__TOKEN on the VM")
    b, st = launched(cl)
    la = (b.get_json(P["status"]) or {}).get("launch", {})
    ok(not cl.calls and la.get("enabled") is False and "COILED" in la.get("why", "") and req_of(st).get("state") == "queued"
       and "held" in req_of(st).get("note", ""), f"SV26 launches are held without cloud access ({la})")
    # SV27 one launch per request, from the clean tree, recorded as launching before the child starts; on exit 0 its
    # launch id, cluster ids and assignments are recorded and its collection enqueued; the budget record stays in state
    cl, b, h = FakeCloud(code=None), FakeBlob(), {}
    rid = guard(enqueue, b, N, "launch", LREQ, T0)
    orig = cl.start_launch
    cl.start_launch = lambda e, tree, inv, log: (setattr(cl, "seen_state", (b.get_json(P["state"]) or {})
                                                         .get("launch", {}).get(rid, {}).get("state"))
                                                 or orig(e, tree, inv, log))
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0, td, cloud=cl, handles=h, **KW)
        st = guard(tick, b, N, st, T0 + 60, td, cloud=cl, handles=h, **KW)
        cl.code = 0
        st = guard(tick, b, N, st, T0 + 120, td, cloud=cl, handles=h, **KW)
    e, raw = req_of(st), b.d.get(P["status"], b"")
    ok([c for c in cl.calls if c[0] == "launch"] == [("launch", LREQ["config"], "t1", 2, f"/trees/{SHA}", f"sv-{rid}")]
       and cl.seen_state == "launching" and ("budget", "ev") in cl.calls,
       f"SV27 launched once, from the commit's clean tree, recorded as launching first ({cl.calls}, {cl.seen_state})")
    ok(e.get("state") == "launched" and e.get("launch_id") == "L1" and e.get("clusters", {}).get("run", {}).get("id") == 101
       and sorted(st.get("collect", {}).get("ev-flu-t1-gcc", {}).get("entry", {}).get("assignments", {})) == ["R1", "R2"]
       and b"N0NCE" not in raw and b"N0NCE" in b.d[P["state"]],
       f"SV27b exit 0: launch id, cluster ids and assignments recorded, collection enqueued ({e})")
    b2 = FakeBlob()
    b2.d = dict(b.d)
    st2 = b2.get_json(P["state"])
    st2["launch"][rid]["state"] = "launching"
    cl2 = FakeCloud()
    with tempfile.TemporaryDirectory() as td:
        st2 = guard(tick, b2, N, st2, T0 + 200, td, cloud=cl2, handles={}, **KW)
    ok(not cl2.calls and req_of(st2).get("state") == "interrupted",
       f"SV27c a request found launching after a restart is never launched again ({req_of(st2)})")
    # SV28 make_tree: a clean checkout of the sha; anything that differs is replaced by a fresh clone; unknown sha refused
    with tempfile.TemporaryDirectory() as td:
        src, g = os.path.join(td, "src"), ["git", "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]
        os.makedirs(src)
        r = subprocess.run(["git", "init", "-q", src], capture_output=True)
        open(os.path.join(src, "f.txt"), "w").write("one\n")
        subprocess.run(g + ["-C", src, "add", "f.txt"], capture_output=True)
        subprocess.run(g + ["-C", src, "commit", "-qm", "c1"], capture_output=True)
        sha = subprocess.run(["git", "-C", src, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        d = guard(make_tree, sha, os.path.join(td, "w"), url=src)
        ok(isinstance(d, str) and open(os.path.join(d, "f.txt")).read() == "one\n", f"SV28 a clean checkout of the sha ({d})")
        if isinstance(d, str):
            open(os.path.join(d, "stray.py"), "w").write("x")
            open(os.path.join(d, "f.txt"), "w").write("edited\n")
            d2 = guard(make_tree, sha, os.path.join(td, "w"), url=src)
            ok(d2 == d and not os.path.exists(os.path.join(d, "stray.py")) and open(os.path.join(d, "f.txt")).read() == "one\n",
               "SV28b a tree with an edit or an untracked file is replaced by a fresh clone, never launched")
        ok("RuntimeError" in str(guard(make_tree, "cd" * 20, os.path.join(td, "w"), url=src)),
           "SV28c a sha the repository does not hold is refused")
    # SV29 a config outside budget scope ev, or a commit with no clean tree, is refused once and never launched
    for cl, what in ((FakeCloud(scope="f6"), "scope"), (FakeCloud(dirty=True), "clean")):
        b, st = launched(cl)
        ok(not [c for c in cl.calls if c[0] == "launch"] and req_of(st).get("state") == "refused"
           and what in req_of(st).get("note", ""), f"SV29 refused: {what} ({req_of(st)})")
    # SV30 a launch that fails: this invocation's failed record's ids are stopped; another invocation's: nothing
    for mine, want in ((True, [101, 102]), (False, [])):
        cl = FakeCloud(code=1, mine=mine, status="failed")
        b, st = launched(cl)
        ok(req_of(st).get("state") == "failed" and sorted(c[2] for c in cl.calls if c[0] == "stop") == want,
           f"SV30 exit 1, record {'ours' if mine else 'not ours'}: stops {want} ({cl.calls})")
    # SV41 status lines show each launch; pull fetches each launch's log once
    cl = FakeCloud()
    b, st = launched(cl)
    rid_ = req_of(st).get("request", "?")
    b.d[f"{P['launches']}{rid_}/launch.log"] = b"invocation sv-x\n"
    lines = "\n".join(guard(status_lines, b.get_json(P["status"]) or {}, T0 + 30)[0])
    with tempfile.TemporaryDirectory() as dest:
        got1, got2 = guard(pull, b, N, dest), guard(pull, b, N, dest)
        ok(f"launch {rid_} cloud/configs/eval_flu_vaccine.py tag t1 x2 @ ababababa: launched L1" in lines
           and any(g.endswith("launch.log") for g in got1) and got2 == []
           and os.path.exists(os.path.join(dest, "launches", rid_, "launch.log")),
           f"SV41 status shows each launch; pull fetches its log once ({lines})")
    # SV42 close to the job's end no new launch starts
    cl, b = FakeCloud(), FakeBlob()
    guard(enqueue, b, N, "launch", LREQ, T0)
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0, td, cloud=cl, handles={}, allow_launch=False, **KW)
    ok(not cl.calls and "ends soon" in req_of(st).get("note", ""), f"SV42 no launch starts near the job's end ({st})")

    # ---- Codex round 1 on #358, the parts that apply to launches (SV43-SV46) ----
    def refused(fn, *a, **k):
        """fn's result, or its SystemExit's message (guard lets SystemExit through)."""
        try:
            return fn(*a, **k)
        except SystemExit as e:
            return str(e)

    # SV43 `start` passes the Coiled token and the Anthropic key by name only: never copied into vm_job's environment
    a = argparse.Namespace(name=DEFAULT_NAME, cap_usd=15.0, max_hours=168.0, budget_scope=None, account="acct1",
                           every=60)
    runs_ = []
    with mock.patch.dict(globals(), {"_laptop_secrets": lambda: (None, True, True)}), \
            mock.patch.object(subprocess, "run", lambda cmd, **k: runs_.append((cmd, k.get("env")))
                              or types.SimpleNamespace(returncode=0)), \
            mock.patch.dict(os.environ, {}), contextlib.redirect_stdout(io.StringIO()):
        for k_ in ("DASK_COILED__TOKEN", "ANTHROPIC_API_KEY"):
            os.environ.pop(k_, None)
        rc_ = guard(cmd_start, a)
    flat = " ".join(" ".join(c) for c, _ in runs_)
    ok(rc_ == 0 and len(runs_) == 2 and "--secret-env DASK_COILED__TOKEN" in flat
       and "--secret-env ANTHROPIC_API_KEY" in flat and "--anthropic-key" not in flat
       and not any(e_ and ("DASK_COILED__TOKEN" in e_ or "ANTHROPIC_API_KEY" in e_) for _, e_ in runs_),
       f"SV43 start: secrets by name, none copied into the launcher's environment ({flat})")
    # SV44 a storage account that is not a bare name (argument or environment) is refused before any Blob client
    import results_store
    built = []
    with mock.patch.object(results_store, "_service", lambda acct: built.append(acct) or object()):
        bad_ = "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=abc=="
        r_arg = refused(Blob, bad_)
        with mock.patch.dict(os.environ, {"MA_STORAGE_ACCOUNT": "https://x.blob.core.windows.net"}):
            r_env = refused(Blob, None)
        good_ = guard(Blob, "acct1")
    ok(isinstance(r_arg, str) and "storage account" in r_arg and "AccountKey" not in r_arg
       and isinstance(r_env, str) and "storage account" in r_env and built == ["acct1"] and isinstance(good_, Blob),
       f"SV44 a connection string, URL or key as the account is refused, unechoed, before any client ({r_arg}, "
       f"{r_env}, {built})")
    # SV45 budget records: the laptop sends only one its own checks pass (signature, expiry, the high-water mark) for
    # the scope the launch uses; the VM installs one only if it verifies there, and keeps what it had otherwise
    import budget
    with tempfile.TemporaryDirectory() as bd:
        open(os.path.join(bd, "budget_ev.json"), "w").write(json.dumps({"record": {"scope": "ev"}, "sig": "s"}))
        asked = []
        ok_load = lambda sc: asked.append(sc) or {"scope": sc}  # noqa: E731

        def bad_load(sc):
            raise budget.BudgetError("issued before the newest one seen")
        got_ = guard(signed_budget, "ev", budget_dir=bd, load=ok_load)
        ref_ = refused(signed_budget, "ev", budget_dir=bd, load=bad_load)
        none_ = refused(signed_budget, "sv", budget_dir=bd, load=ok_load)
        ok(got_ == {"record": {"scope": "ev"}, "sig": "s"} and asked == ["ev"] and "issued before" in str(ref_)
           and "SystemExit" not in str(got_) and isinstance(none_, str) and "no budget" in none_,
           f"SV45 the laptop sends the launch scope's record only when its checks pass ({got_}, {ref_}, {none_})")
        prev = open(os.path.join(bd, "budget_ev.json")).read()
        r_bad = guard(install_budget, "ev", {"record": {"scope": "ev", "older": 1}}, budget_dir=bd, load=bad_load)
        kept = open(os.path.join(bd, "budget_ev.json")).read() == prev
        r_new = guard(install_budget, "sv", {"record": {"scope": "sv"}}, budget_dir=bd, load=bad_load)
        ok("does not verify" in str(r_bad) and kept and "does not verify" in str(r_new)
           and not os.path.exists(os.path.join(bd, "budget_sv.json"))
           and guard(install_budget, "ev", {"record": {"scope": "ev", "n": 2}}, budget_dir=bd, load=ok_load) is True,
           f"SV45b the VM keeps its record when a new one does not verify there ({r_bad}, {r_new})")
    # SV46 the loop's sleep is bounded by the next due check: a launch child is polled every 15 s
    T = T0 + 10_000
    ok(guard(sleep_for, {}, T, 600) == 600 and guard(sleep_for, {"launch": {"a": {"state": "launching"}}}, T, 600) == 15
       and guard(sleep_for, {"launch": {"a": {"state": "launching"}}}, T, 5) == 5,
       "SV46 the sleep stops at the next due check (a launch child), never past --every")

    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        import no_cloud
        with no_cloud.refusing():
            return _self_test()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = {c: sub.add_parser(c) for c in ("start", "stop", "loop", "enqueue-merge", "enqueue-collect", "enqueue-noop",
                                         "enqueue-launch", "status", "pull")}
    for c, p in ps.items():
        p.add_argument("--name", default=DEFAULT_NAME)
        p.add_argument("--account", default=None)
        if c in ("start", "loop"):
            p.add_argument("--every", type=float, default=60)
            p.add_argument("--max-hours", type=float, default=168.0)
    ps["start"].add_argument("--cap-usd", type=float, default=15.0)
    ps["start"].add_argument("--budget-scope", default=None)
    ps["loop"].add_argument("--work", default="~/supervisor_work")
    pl = ps["enqueue-launch"]
    pl.add_argument("config")
    pl.add_argument("--tag", required=True)
    pl.add_argument("--reps", type=int, default=1)
    pl.add_argument("--commit", required=True)
    ps["enqueue-merge"].add_argument("prs", nargs="+", type=int)
    ps["enqueue-collect"].add_argument("configs", nargs="+")
    ps["pull"].add_argument("--dest", default=os.path.expanduser("~/modeling-agentic-keep/supervisor"))
    a = ap.parse_args(argv)
    if a.cmd in ("loop", "start"):
        return (loop if a.cmd == "loop" else cmd_start)(a)
    if a.cmd == "stop":
        import run_cluster as rc
        import vm_job
        return print(stop_own(a.name, live_id=rc.live_id, stop=vm_job._stop_by_id)) or 0
    blob = Blob(a.account)
    if a.cmd == "status":
        lines, code = status_lines(blob.get_json(paths(a.name)["status"]), time.time())
        return print("\n".join(lines)) or code
    if a.cmd == "pull":
        got = pull(blob, a.name, os.path.join(a.dest, a.name))
        return print(f"pulled {len(got)} run(s) into {os.path.join(a.dest, a.name)}") or 0
    reqs = {"enqueue-merge": lambda: [("merge", {"prs": a.prs})], "enqueue-noop": lambda: [("noop", {})],
            "enqueue-collect": lambda: [("collect", _assignments(c)) for c in a.configs],
            "enqueue-launch": lambda: [("launch", launch_request(a))]}[a.cmd]()
    for kind, payload in reqs:
        print(enqueue(blob, a.name, kind, payload))
    return 0

if __name__ == "__main__":
    sys.exit(main())
