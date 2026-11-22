#!/usr/bin/env python3
"""Between-run supervision off the laptop (W0-J, small version; owner yes 2026-11-22).

Twice the laptop slept and every step between runs stopped for about 8 hours. A loop on one small Coiled VM now keeps
three things going, with all its state in blob storage, so the laptop is only a client:

  (a) the merge train (ops/merge_train.sh's steps): for each queued PR in order, update the branch when it is behind
      its base, wait for CI on the new head, merge with that head's sha when every check passed; skip and record a
      failure. Disabled until a GitHub token is provided (see "GitHub token" below).
  (b) eval collection: for each enqueued launch (a run_cluster.py config's assignments.json), every label's stored
      run is fetched from storage (run_cluster.collect_from_storage: storage only, no cluster is attached or stopped),
      archived to the supervisor's blob prefix, and scripts/harness_metrics.py is run over every collected run.
  (c) status.json in blob, rewritten every loop: heartbeat, merge queue, collection table, paused runs, errors.

Layout, under outputs/supervisor/<name>/ in the storage account ($MA_STORAGE_ACCOUNT / $COILED_STORAGE_ACCOUNT or
--account; never hard-coded): requests/<utc>-<hex>.json (written once by the laptop, read by the loop), state.json
and status.json (written only by the loop), collected/<launch name>/<label>.tar.gz, metrics.json.

On the laptop:
    python3 cloud/supervisor.py start [--name ev-supervisor-gcc] [--cap-usd 15] [--max-hours 24]
    python3 cloud/supervisor.py enqueue-merge <pr>...
    python3 cloud/supervisor.py enqueue-collect cloud/configs/eval_tb_acf.py ...
    python3 cloud/supervisor.py enqueue-noop          # a request that only shows up as acknowledged in status
    python3 cloud/supervisor.py status
    python3 cloud/supervisor.py pull [--dest ~/modeling-agentic-keep/supervisor]
    python3 cloud/supervisor.py stop                  # this supervisor's own cluster, by id
On the VM (what `start` runs through cloud/vm_job.py):
    python3 cloud/supervisor.py loop --name <name> [--every 60]
    python3 cloud/supervisor.py --self-test

`start` is cloud/vm_job.py `up` + `start` on a 2-vCPU worker with a 2-vCPU scheduler: the loop is a vm_job job, a
dask task that keeps the cluster busy for its lifetime (--max-hours), so the idle timeout only runs after it ends.
The cluster name needs a budget scope (cloud/budget_scopes.yaml); the default name is in the `ev` scope.

GitHub token. The loop merges only when $GH_TOKEN is in its environment. `start` passes it (vm_job --secret-env, the
job's environment only, never a file) when the owner's login keychain holds it under the service
"modeling-agentic-supervisor-github". Without it the merge queue is held and status says so.
Resuming paused v2 runs is not automated here: status lists collected runs whose metadata says they are paused.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

ROOT = "outputs/supervisor"
DEFAULT_NAME = "ev-supervisor-gcc"
GH_REPO = "guillaumechabotcouture/modeling-agentic"
KEYCHAIN_SERVICE = "modeling-agentic-supervisor-github"
STALE_S = 600            # status older than this: the loop is not running
GIVE_UP_S = 3 * 3600     # a PR still waiting after this long is skipped
UPDATE_WAIT_S = 300      # after update-branch, wait this long for the new head before asking again
COLLECT_EVERY_S = 300    # a label not yet in storage is tried again after this long
CI_WORKFLOW = ".github/workflows/ci.yml"   # a head merges only once this workflow's run on it has succeeded
KEEP = 50              # last N acks / errors / finished merges kept in state


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def paths(name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name or ""):
        raise SystemExit(f"bad supervisor name {name!r}")
    b = f"{ROOT}/{name}"
    return {"requests": f"{b}/requests/", "state": f"{b}/state.json", "status": f"{b}/status.json",
            "collected": f"{b}/collected/", "metrics": f"{b}/metrics.json"}


# ---- storage ------------------------------------------------------------------------------------------------------

class Blob:
    """results_store with one account: JSON and file put/get, listing. get_json returns None when absent."""

    def __init__(self, account=None):
        import results_store
        self.rs, self.account = results_store, account or results_store.configured_account()
        if not self.account:
            raise SystemExit("no storage account: set $MA_STORAGE_ACCOUNT / $COILED_STORAGE_ACCOUNT or pass --account")
        self.svc = results_store._service(self.account)

    def put_json(self, blob, obj):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.json")
            json.dump(obj, open(p, "w"), indent=1, default=str)
            self.rs.upload_retry(p, blob, self.account, service=self.svc)

    def get_json(self, blob):
        with tempfile.TemporaryDirectory() as td:
            try:
                self.rs.download(blob, os.path.join(td, "x.json"), self.account, service=self.svc)
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "ResourceNotFoundError":
                    return None
                raise
            return json.load(open(os.path.join(td, "x.json")))

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


# ---- GitHub (the merge train) -------------------------------------------------------------------------------------

class GitHub:
    """The REST calls merge_train.sh made through `gh`. The token is sent as a header and never stored or printed."""

    def __init__(self, token: str, repo: str = GH_REPO):
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

    def pr(self, n):
        code, j = self._call("GET", f"pulls/{int(n)}")
        if code != 200:
            raise RuntimeError(f"GET pull {n}: HTTP {code}")
        return {"state": j["state"], "merged": bool(j.get("merged")), "head": j["head"]["sha"],
                "base": j["base"]["ref"]}

    def behind(self, base, head):
        code, j = self._call("GET", f"compare/{base}...{head}")
        if code != 200:
            raise RuntimeError(f"compare {base}...{head[:7]}: HTTP {code}")
        return int(j.get("behind_by") or 0) > 0

    def checks(self, sha):
        """One word per check run and commit status: success | pending | failure (as `gh pr checks` buckets them)."""
        code, j = self._call("GET", f"commits/{sha}/check-runs?per_page=100")
        if code != 200:
            raise RuntimeError(f"check-runs of {sha[:7]}: HTTP {code}")
        out = ["pending" if r.get("status") != "completed" else
               "success" if r.get("conclusion") in ("success", "neutral", "skipped") else "failure"
               for r in j.get("check_runs") or []]
        code, j = self._call("GET", f"commits/{sha}/status")
        if code == 200:
            out += [{"success": "success", "pending": "pending"}.get(x.get("state"), "failure")
                    for x in j.get("statuses") or []]
        return out

    def update_branch(self, n, sha):
        return self._call("PUT", f"pulls/{int(n)}/update-branch", {"expected_head_sha": sha})[0] in (200, 202)

    def merge(self, n, sha):
        code, j = self._call("PUT", f"pulls/{int(n)}/merge", {"merge_method": "merge", "sha": sha})
        return code == 200 and bool(j.get("merged", True))


def merge_step(gh, item: dict, now: float) -> dict:
    """One step of one PR: item {pr, state queued|waiting|merged|already_merged|skipped, note, ...}, updated in place."""
    item.setdefault("since", now)
    item["last_step"] = utc(now)

    def end(state, note):
        item.update(state=state, note=note, ended=utc(now))
        return item
    pr = gh.pr(item["pr"])
    if pr["merged"]:
        return end("already_merged", "already merged")
    if pr["state"] != "open":
        return end("skipped", f"PR is {pr['state']}")
    if now - item["since"] > GIVE_UP_S:
        return end("skipped", f"gave up after {GIVE_UP_S // 3600} h waiting")
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


# ---- collection ---------------------------------------------------------------------------------------------------

def fetch_from_storage(entry, label, work):
    """run_cluster.collect_from_storage into <work>/<launch name>/<label>/ (storage only, no cluster touched)."""
    import types
    import results_store
    import run_cluster as rc
    cfg = types.SimpleNamespace(out_dir=os.path.join(work, entry["name"]), storage_prefix=entry["storage_prefix"],
                                storage_account=entry.get("storage_account"))
    os.makedirs(cfg.out_dir, exist_ok=True)
    a = entry["assignments"][label]
    return rc.collect_from_storage(cfg, results_store, label, a["launch_id"], a)


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


def collect_step(blob, name, state, now, work, fetch=fetch_from_storage, metrics=run_metrics) -> list:
    """Try every enqueued label not yet collected (at most every COLLECT_EVERY_S); archive each new one to blob."""
    import results_store
    new = []
    for lname, ent in sorted((state.get("collect") or {}).items()):
        for label, a in sorted(ent["entry"]["assignments"].items()):
            lab = ent["labels"].setdefault(label, {})
            if lab.get("launch_id") != a.get("launch_id"):
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
            new.append(f"{lname}/{label}")
    if new:
        with tempfile.TemporaryDirectory() as td:
            out = metrics(run_dirs(work), os.path.join(td, "metrics.json"))
            if out:
                blob.put_file(out, paths(name)["metrics"])
                state["metrics_at"] = utc(now)
    return new


# ---- the loop -----------------------------------------------------------------------------------------------------

def tick(blob, name, state, now, work, gh=None, fetch=fetch_from_storage, metrics=run_metrics, host="") -> dict:
    """One pass: ingest new requests, one merge step, collection, then state.json and status.json. A failing part is
    recorded in errors and the rest still runs; status is always written."""
    p = paths(name)
    state = dict(state or {})
    for k, v in (("seen", []), ("merge", []), ("merge_done", []), ("collect", {}), ("acks", []), ("errors", [])):
        state.setdefault(k, v)

    def err(where, e):
        state["errors"] = (state["errors"] + [{"at": utc(now), "where": where,
                                                "error": f"{type(e).__name__}: {str(e)[:200]}"}])[-KEEP:]
    try:
        for b in blob.names(p["requests"]):
            rid = b[len(p["requests"]):-len(".json")]
            if rid in state["seen"]:
                continue
            req = blob.get_json(b) or {}
            kind, pl = req.get("kind"), req.get("payload") or {}
            state["seen"].append(rid)        # a bad request is recorded once, never retried every loop
            if kind == "merge":
                queued = {i["pr"] for i in state["merge"]}
                state["merge"] += [{"pr": int(n), "state": "queued", "request": rid} for n in pl.get("prs", [])
                                   if int(n) not in queued]
            elif kind == "collect":
                state["collect"].setdefault(pl["name"], {"labels": {}})["entry"] = pl
            elif kind != "noop":
                raise ValueError(f"request {rid}: unknown kind {kind!r}")
            state["acks"] = (state["acks"] + [{"request": rid, "kind": kind, "at": utc(now)}])[-KEEP:]
    except Exception as e:  # noqa: BLE001
        err("requests", e)
    if gh is not None and state["merge"]:
        try:
            item = merge_step(gh, state["merge"][0], now)
            if item["state"] not in ("queued", "waiting"):
                state["merge_done"] = (state["merge_done"] + [state["merge"].pop(0)])[-KEEP:]
        except Exception as e:  # noqa: BLE001
            err("merge", e)
    try:
        collect_step(blob, name, state, now, work, fetch=fetch, metrics=metrics)
    except Exception as e:  # noqa: BLE001
        err("collect", e)
    state["loops"] = int(state.get("loops", 0)) + 1
    paused = []
    for d in run_dirs(work):
        try:
            st = str((json.load(open(os.path.join(d, "metadata.json"))) or {}).get("status", ""))
        except (OSError, ValueError, AttributeError):
            continue
        if st.startswith(("paused", "awaiting")):
            paused.append({"run": os.path.relpath(d, work), "status": st})
    status = {"name": name, "host": host, "heartbeat_utc": utc(now), "loops": state["loops"],
              "merge": {"enabled": gh is not None,
                        "why": "" if gh is not None else "no GitHub token ($GH_TOKEN) on the VM: merges are held",
                        "queue": state["merge"], "done": state["merge_done"][-10:]},
              "collect": {k: {"labels": v["labels"]} for k, v in state["collect"].items()},
              "metrics_at": state.get("metrics_at"), "paused": paused,
              "acks": state["acks"][-10:], "errors": state["errors"][-10:]}
    state["seen"] = state["seen"][-1000:]
    blob.put_json(p["state"], state)
    blob.put_json(p["status"], status)
    return state


def loop(a):
    blob, p = Blob(a.account), paths(a.name)
    work = os.path.abspath(a.work)
    token = os.environ.get("GH_TOKEN") or None
    gh = GitHub(token) if token else None
    state = blob.get_json(p["state"]) or {}
    host = f"{os.uname().nodename}:{os.getpid()}"
    deadline = time.time() + a.max_hours * 3600
    while time.time() < deadline:
        try:
            state = tick(blob, a.name, state, time.time(), work, gh=gh, host=host)
        except Exception as e:  # noqa: BLE001 -- the loop outlives one bad pass (storage blip, DNS)
            print(f"tick failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        time.sleep(a.every)
    return 0


# ---- laptop side --------------------------------------------------------------------------------------------------

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
    out += [f"  PR {i['pr']:<6} {i['state']:<15} {i.get('note', '')}" for i in m.get("queue") or []]
    out += [f"  PR {i['pr']:<6} {i['state']:<15} {i.get('note', '')} (done)" for i in m.get("done") or []]
    for lname, ent in sorted((st.get("collect") or {}).items()):
        for label, lab in sorted(ent.get("labels", {}).items()):
            out.append(f"collect {lname}/{label}: {lab.get('state')} {lab.get('note', '')}".rstrip())
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
            if lab.get("state") != "collected" or index.get(lab["blob"]) == lab["sha256"]:
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
    json.dump(index, open(ip, "w"), indent=1)
    if got and st.get("metrics_at"):
        blob.get_file(paths(name)["metrics"], os.path.join(dest, "metrics.json"))
    return got


def keychain_token(service=KEYCHAIN_SERVICE, runner=subprocess.run):
    r = runner(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def start_commands(a, job_id, token_present) -> list:
    """The two vm_job commands `start` runs: up (2-vCPU worker) and start (the loop as a job)."""
    vm = [sys.executable, "cloud/vm_job.py"]
    budget = ["--cap-usd", str(a.cap_usd)] + (["--budget-scope", a.budget_scope] if a.budget_scope else [])
    acct = ["--store-account", a.account] if a.account else []
    up = vm + ["up", a.name, "--cpu", "2", "--memory", "8 GiB", "--idle-timeout", "20 minutes"] + budget
    start = (vm + ["start", a.name, job_id, "--tree", ".", "--max-hours", str(a.max_hours + 0.25)] + acct + budget
             + (["--secret-env", "GH_TOKEN"] if token_present else [])
             + ["--", "python3", "cloud/supervisor.py", "loop", "--name", a.name, "--every", str(int(a.every)),
                "--max-hours", str(a.max_hours)])
    return [up, start]


def cmd_start(a):
    tok = keychain_token()
    job_id = "sv-" + time.strftime("%Y%m%d-%H%M%S")
    env = {**os.environ, **({"GH_TOKEN": tok} if tok else {})}
    print(f"merge train: {'enabled (token from the keychain)' if tok else 'disabled (no token in the keychain)'}")
    for cmd in start_commands(a, job_id, bool(tok)):
        print("$ " + " ".join(cmd), flush=True)
        r = subprocess.run(cmd, cwd=REPO_ROOT, env=env)
        if r.returncode:
            return r.returncode
    print(f"started {job_id}; read it with `python3 cloud/supervisor.py status --name {a.name}`")
    return 0


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


# ---- self-test ----------------------------------------------------------------------------------------------------

class FakeBlob:
    def __init__(self):
        self.d = {}

    def put_json(self, blob, obj):
        self.d[blob] = json.dumps(obj, default=str).encode()

    def get_json(self, blob):
        return json.loads(self.d[blob]) if blob in self.d else None

    def put_file(self, path, blob):
        self.d[blob] = open(path, "rb").read()
        return {"blob": blob}

    def get_file(self, blob, path):
        if blob not in self.d:
            raise FileNotFoundError(blob)
        open(path, "wb").write(self.d[blob])

    def names(self, prefix):
        return sorted(k for k in self.d if k.startswith(prefix))


class FakeGitHub:
    """A PR table; every call is logged. checks: list of 'success' | 'pending' | 'failure'."""

    def __init__(self, prs):
        self.prs, self.calls = prs, []

    def pr(self, n):
        self.calls.append(("pr", n))
        if n not in self.prs:
            raise LookupError(f"PR {n} not found")
        return dict(self.prs[n])

    def behind(self, base, head):
        self.calls.append(("behind", head))
        return self.prs_by_head(head).get("behind", False)

    def prs_by_head(self, head):
        return next(p for p in self.prs.values() if p["head"] == head)

    def checks(self, sha):
        self.calls.append(("checks", sha))
        return list(self.prs_by_head(sha).get("checks", []))

    def update_branch(self, n, sha):
        self.calls.append(("update", n, sha))
        return self.prs[n].get("update_ok", True)

    def merge(self, n, sha):
        self.calls.append(("merge", n, sha))
        self.prs[n].update(state="closed", merged=True)
        return True


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
    # SV1 requests are write-once and distinct
    b = FakeBlob()
    r1, r2 = guard(enqueue, b, N, "noop", {}, T0), guard(enqueue, b, N, "noop", {}, T0)
    ok(isinstance(r1, str) and r1 != r2 and len(b.names(P["requests"])) == 2, "SV1 each enqueue is its own blob")

    # SV2 a noop is acknowledged once; re-ticking does not ingest it again
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0 + 1, td)
        st2 = guard(tick, b, N, st, T0 + 2, td) if isinstance(st, dict) else st
        status = b.get_json(P["status"]) or {}
        ok(isinstance(st2, dict) and len(st2.get("acks", [])) == 2 and status.get("heartbeat_utc") == utc(T0 + 2)
           and status.get("loops") == 2, f"SV2 noop requests acknowledged once each, status rewritten ({status})")

    # SV3 no token: merge items held, no GitHub call
    b = FakeBlob()
    guard(enqueue, b, N, "merge", {"prs": [11, 12]}, T0)
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0 + 1, td, gh=None)
        status = b.get_json(P["status"]) or {}
        ok(isinstance(st, dict) and status.get("merge", {}).get("enabled") is False
           and [i["pr"] for i in status.get("merge", {}).get("queue", [])] == [11, 12]
           and "token" in status.get("merge", {}).get("why", ""), f"SV3 without a token the queue is held ({status})")

    # SV4 behind: update-branch once with the expected head; no second request within UPDATE_WAIT_S
    gh = FakeGitHub({7: {"state": "open", "merged": False, "head": "aaa", "base": "v2", "behind": True}})
    it = {"pr": 7, "state": "queued"}
    guard(merge_step, gh, it, T0)
    guard(merge_step, gh, it, T0 + 60)
    ok([c for c in gh.calls if c[0] == "update"] == [("update", 7, "aaa")] and it.get("state") == "waiting",
       f"SV4 a PR behind its base is updated once, then waits ({gh.calls}, {it})")
    gh.prs[7]["update_ok"] = False
    it2 = {"pr": 7, "state": "queued"}
    guard(merge_step, gh, it2, T0)
    ok(it2.get("state") == "skipped" and "update" in it2.get("note", ""), f"SV4b a failed update skips ({it2})")

    # SV5 pending waits; failure skips; all success merges with the head sha
    gh = FakeGitHub({1: {"state": "open", "merged": False, "head": "h1", "base": "v2", "checks": ["success", "pending"]},
                     2: {"state": "open", "merged": False, "head": "h2", "base": "v2", "checks": ["success", "failure"]},
                     3: {"state": "open", "merged": False, "head": "h3", "base": "v2", "checks": ["success"]},
                     4: {"state": "open", "merged": False, "head": "h4", "base": "v2", "checks": []}})
    its = {n: {"pr": n, "state": "queued"} for n in (1, 2, 3, 4)}
    for n in its:
        guard(merge_step, gh, its[n], T0)
    ok([its[n].get("state") for n in (1, 2, 3, 4)] == ["waiting", "skipped", "merged", "waiting"]
       and ("merge", 3, "h3") in gh.calls and not any(c[0] == "merge" and c[1] != 3 for c in gh.calls),
       f"SV5 pending/no checks wait, a failure skips, all green merges at the head sha ({its})")

    # SV6 already merged / closed: no merge call
    gh = FakeGitHub({5: {"state": "closed", "merged": True, "head": "h5", "base": "v2"},
                     6: {"state": "closed", "merged": False, "head": "h6", "base": "v2"}})
    i5, i6 = {"pr": 5, "state": "queued"}, {"pr": 6, "state": "queued"}
    guard(merge_step, gh, i5, T0), guard(merge_step, gh, i6, T0)
    ok(i5.get("state") == "already_merged" and i6.get("state") == "skipped"
       and not any(c[0] == "merge" for c in gh.calls), f"SV6 merged or closed PRs are not merged again ({i5}, {i6})")

    # SV7 give up after GIVE_UP_S
    gh = FakeGitHub({8: {"state": "open", "merged": False, "head": "h8", "base": "v2", "checks": ["pending"]}})
    i8 = {"pr": 8, "state": "queued"}
    guard(merge_step, gh, i8, T0)
    guard(merge_step, gh, i8, T0 + GIVE_UP_S + 1)
    ok(i8.get("state") == "skipped" and "gave up" in i8.get("note", ""), f"SV7 a PR waiting too long is skipped ({i8})")

    # SV8 sequential: the second PR is not touched while the first waits; SV15 state survives a restart
    b = FakeBlob()
    gh = FakeGitHub({21: {"state": "open", "merged": False, "head": "x1", "base": "v2", "checks": ["pending"]},
                     22: {"state": "open", "merged": False, "head": "x2", "base": "v2", "checks": ["success"]}})
    guard(enqueue, b, N, "merge", {"prs": [21, 22]}, T0)
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0 + 1, td, gh=gh)
        ok(isinstance(st, dict) and not any(c[1:2] == (22,) or c[1:2] == ("x2",) for c in gh.calls),
           f"SV8 one PR at a time ({gh.calls})")
        gh.prs[21]["checks"] = ["success"]
        st = guard(tick, b, N, b.get_json(P["state"]) or {}, T0 + 100, td, gh=gh)
        st = guard(tick, b, N, b.get_json(P["state"]) or {}, T0 + 200, td, gh=gh)
        merged = [c[1] for c in gh.calls if c[0] == "merge"]
        guard(tick, b, N, b.get_json(P["state"]) or {}, T0 + 300, td, gh=gh)
        ok(merged == [21, 22] and [c[1] for c in gh.calls if c[0] == "merge"] == [21, 22],
           f"SV15 merges in order and a reloaded state does not merge again ({gh.calls})")

    # SV9 collection: absent -> waiting; stored -> collected, archived, metrics over run dirs; not re-collected
    b = FakeBlob()
    entry = {"name": "ev-x", "storage_prefix": "runs/ev-x", "storage_account": None,
             "assignments": {"q": {"launch_id": "L1"}}}
    guard(enqueue, b, N, "collect", entry, T0)
    seen = {"fetch": 0, "metrics": []}

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
        json.dump({"rows": [{"run": os.path.basename(d)} for d in dirs]}, open(out, "w"))
        return out

    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0 + 1, td, fetch=fetch, metrics=metrics)
        lab = (st.get("collect", {}).get("ev-x", {}).get("labels", {}).get("q", {}) if isinstance(st, dict) else {})
        ok(lab.get("state") == "waiting" and seen["fetch"] == 1, f"SV9 a run not in storage yet waits ({lab})")
        st = guard(tick, b, N, st, T0 + 2, td, fetch=fetch, metrics=metrics)
        ok(seen["fetch"] == 1, "SV9b a waiting label is not retried before COLLECT_EVERY_S")
        fetch.__defaults__[0][0] = True
        st = guard(tick, b, N, st, T0 + 2 + COLLECT_EVERY_S, td, fetch=fetch, metrics=metrics)
        lab = (st.get("collect", {}).get("ev-x", {}).get("labels", {}).get("q", {}) if isinstance(st, dict) else {})
        tgz = P["collected"] + "ev-x/q.tar.gz"
        status = b.get_json(P["status"]) or {}
        ok(lab.get("state") == "collected" and tgz in b.d and lab.get("sha256") and P["metrics"] in b.d
           and seen["metrics"] and seen["metrics"][-1][0].endswith("runs/20261122_q"),
           f"SV9c a stored run is collected, archived and measured ({lab}, {sorted(b.d)})")
        ok(any("20261122_q" in str(x) for x in status.get("paused", [])), f"SV9d status lists paused runs ({status})")
        guard(tick, b, N, st, T0 + 5000, td, fetch=fetch, metrics=metrics)
        ok(seen["fetch"] == 2, "SV9e a collected label is not fetched again")
        # SV10 a new launch id resets the label
        guard(enqueue, b, N, "collect", {**entry, "assignments": {"q": {"launch_id": "L2"}}}, T0 + 5001)
        st = guard(tick, b, N, b.get_json(P["state"]) or {}, T0 + 5002, td, fetch=fetch, metrics=metrics)
        lab = (st.get("collect", {}).get("ev-x", {}).get("labels", {}).get("q", {}) if isinstance(st, dict) else {})
        ok(lab.get("launch_id") == "L2" and seen["fetch"] == 3, f"SV10 a relaunch is collected again ({lab})")

        # SV12 pull: fetched once, sha-checked, data filter; SV13 status staleness
        with tempfile.TemporaryDirectory() as dest:
            got = guard(pull, b, N, dest)
            again = guard(pull, b, N, dest)
            ok(isinstance(got, list) and any(g.endswith("q.tar.gz") for g in got) and again == []
               and os.path.exists(os.path.join(dest, "ev-x", "q", "runs", "20261122_q", "run_state.json")),
               f"SV12 pull fetches each collected run once ({got}, {again})")
            b.d[tgz] = b"corrupt"
            bad = guard(pull, b, N, dest)
            ok(isinstance(bad, list) and not any("q.tar" in x for x in bad),
               "SV12b a tarball that does not match its recorded sha256 is not extracted")

    # SV11 a failing part is recorded; status still written
    b = FakeBlob()
    guard(enqueue, b, N, "collect", entry, T0)

    def boom(*a, **k):
        raise RuntimeError("storage down")
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0 + 1, td, fetch=boom, metrics=metrics)
    status = b.get_json(P["status"]) or {}
    ok(status.get("heartbeat_utc") == utc(T0 + 1) and "storage down" in json.dumps(status),
       f"SV11 a failing part is recorded and status still written ({status})")

    # SV13 status lines: live vs stale vs absent
    live = guard(status_lines, {"heartbeat_utc": utc(T0), "loops": 3, "merge": {"enabled": False, "why": "x",
                                                                                  "queue": []}}, T0 + 30)
    stale = guard(status_lines, {"heartbeat_utc": utc(T0), "loops": 3}, T0 + STALE_S + 1)
    none = guard(status_lines, None, T0)
    ok(isinstance(live, tuple) and live[1] == 0 and stale[1] == 3 and none[1] == 3 and "STALE" in "\n".join(stale[0]),
       f"SV13 status says live, stale or absent ({live}, {stale}, {none})")

    # SV14 the token never reaches state or status
    b = FakeBlob()
    tok = "github_pat_" + "Z" * 40
    os.environ["GH_TOKEN_SELFTEST"] = tok
    guard(enqueue, b, N, "merge", {"prs": [31]}, T0)
    gh = FakeGitHub({31: {"state": "open", "merged": False, "head": "t1", "base": "v2", "checks": ["success"]}})
    gh._token = tok
    with tempfile.TemporaryDirectory() as td:
        guard(tick, b, N, {}, T0 + 1, td, gh=gh)
    ok(b.d and not any(tok.encode() in v for v in b.d.values()), "SV14 the token is in no stored blob")
    os.environ.pop("GH_TOKEN_SELFTEST", None)

    # SV16 start: vm_job up then start; the token passes only by name; the loop is the job
    a = argparse.Namespace(name=DEFAULT_NAME, cap_usd=15.0, max_hours=24.0, budget_scope=None, account="acct1",
                           every=60)
    cmds = guard(start_commands, a, "sv-1", True)
    flat = cmds if cmds == "NotImplemented" else [" ".join(c) for c in cmds]
    ok(isinstance(cmds, list) and len(cmds) == 2 and " up " + DEFAULT_NAME in flat[0] and "--cpu 2" in flat[0]
       and " start " + DEFAULT_NAME + " sv-1" in flat[1] and "--secret-env GH_TOKEN" in flat[1]
       and "cloud/supervisor.py loop --name " + DEFAULT_NAME in flat[1] and "--cap-usd 15.0" in flat[1]
       and "github_pat" not in " ".join(flat), f"SV16 start runs vm_job up + start with the loop ({flat})")
    cmds = guard(start_commands, a, "sv-1", False)
    ok(isinstance(cmds, list) and "--secret-env" not in " ".join(cmds[1]), "SV16b no token: no --secret-env")

    # SV17 stop: only this name's live cluster, by id
    stopped = []
    r = guard(stop_own, DEFAULT_NAME, live_id=lambda n: 123 if n == DEFAULT_NAME else 999,
              stop=lambda n, cid, why: stopped.append((n, cid)))
    r2 = guard(stop_own, DEFAULT_NAME, live_id=lambda n: None, stop=lambda n, cid, why: stopped.append((n, cid)))
    ok(stopped == [(DEFAULT_NAME, 123)] and r2 is not None and r2 != "NotImplemented",
       f"SV17 stop takes this supervisor's live cluster by id only ({stopped}, {r2})")


    # Codex round 1. SV18 checks: every page, an unreadable status endpoint, the CI workflow must have run
    def gh_with(pages, status=(200, {"statuses": []}), wf=("completed", "success")):
        g = GitHub("tok_" + "a" * 20)
        runs = [] if wf is None else [{"path": CI_WORKFLOW, "status": wf[0], "conclusion": wf[1]}]
        g._call = lambda m, path, body=None: (
            (200, {"check_runs": pages[int((re.findall(r"[?&]page=(\d+)", path) or [1])[0]) - 1]}) if "check-runs" in path else
            status if path.endswith("/status") else (200, {"workflow_runs": runs}))
        return g
    green = {"status": "completed", "conclusion": "success"}
    two = gh_with([[green] * 100, [{"status": "completed", "conclusion": "failure"}]])
    ok("failure" in guard(two.checks, "s1"), "SV18 a failing check run on page two is seen")
    try:
        gh_with([[green]], status=(503, {})).checks("s1")
        ok(False, "SV18b an unreadable commit-status endpoint raises (the PR waits)")
    except Exception as e:  # noqa: BLE001
        ok(isinstance(e, RuntimeError), "SV18b an unreadable commit-status endpoint raises (the PR waits)")
    ok("pending" in guard(gh_with([[green]], wf=None).checks, "s1")
       and "pending" in guard(gh_with([[green]], wf=("in_progress", None)).checks, "s1"),
       "SV18c green checks without a finished CI workflow run on the head are pending")
    # SV19 a malformed token is refused without echoing it
    try:
        GitHub("ghp_good\nX-Evil: 1")
        ok(False, "SV19 a token with a newline is refused")
    except ValueError as e:
        ok("ghp_good" not in str(e), f"SV19 a token with a newline is refused, unechoed ({e})")
    # SV20 bad requests: unreadable JSON, a traversing name, missing assignments; later requests still processed
    b = FakeBlob()
    b.d[P["requests"] + "20000101T000000-aaaa.json"] = b"{not json"
    guard(enqueue, b, N, "collect", {"name": "../evil", "storage_prefix": "x", "assignments": {"q": {"launch_id": "L"}}}, T0)
    guard(enqueue, b, N, "collect", {"name": "ev-y", "storage_prefix": "x"}, T0)
    guard(enqueue, b, N, "noop", {}, T0 + 1)
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0 + 2, td, fetch=fetch, metrics=metrics)
        st = guard(tick, b, N, st, T0 + 3, td, fetch=fetch, metrics=metrics)
    ok(isinstance(st, dict) and not st.get("collect") and [x["kind"] for x in st.get("acks", [])] == ["noop"]
       and len(st.get("errors", [])) == 3, f"SV20 bad requests are refused once each, later ones processed ({st})")
    # SV20b pull ignores names that would leave its directory
    b = FakeBlob()
    b.put_json(P["status"], {"collect": {"../evil": {"labels": {"q": {"state": "collected", "blob": "outputs/x",
                                                                        "sha256": "0"}}}}})
    with tempfile.TemporaryDirectory() as dest:
        ok(guard(pull, b, N, os.path.join(dest, "in")) == [] and os.listdir(dest) == ["in"],
           "SV20b pull ignores a launch name that would leave its directory")
    # SV21 a PR that does not exist is skipped; the next one proceeds
    gh = FakeGitHub({42: {"state": "open", "merged": False, "head": "y2", "base": "v2", "checks": ["success"]}})
    b = FakeBlob()
    guard(enqueue, b, N, "merge", {"prs": [41, 42]}, T0)
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0 + 1, td, gh=gh)
        st = guard(tick, b, N, st, T0 + 2, td, gh=gh)
    ok(("merge", 42, "y2") in gh.calls, f"SV21 a missing PR is skipped and the next merged ({gh.calls})")
    # SV22 a relaunch's run replaces the earlier launch's local copy; SV23 a failed metrics upload is retried
    b = FakeBlob()
    calls = {"m": 0}

    def fetch2(e, label, work):
        rd = os.path.join(work, e["name"], label, "runs", "run_" + e["assignments"][label]["launch_id"])
        os.makedirs(rd, exist_ok=True)
        json.dump({"status": "completed"}, open(os.path.join(rd, "metadata.json"), "w"))
        return {}

    def metrics2(dirs, out):
        calls["m"] += 1
        calls["dirs"] = [os.path.basename(d) for d in dirs]
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
        st = guard(tick, b, N, st, T0 + 4, td, fetch=fetch2, metrics=metrics2)
        ok(calls.get("dirs") == ["run_B"], f"SV22 a relaunch does not carry the earlier launch's run ({calls})")
    # SV24 processed requests are never replayed, however many there are
    b = FakeBlob()
    for i in range(1003):
        b.put_json(f"{P['requests']}{i:06d}.json", {"kind": "noop"})
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, {}, T0, td)
        st = guard(tick, b, N, st, T0 + 1, td)
    ok(isinstance(st, dict) and len(st.get("seen", [])) == 1003 and st["acks"][-1]["at"] == utc(T0),
       "SV24 no processed request is replayed")

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
                                         "status", "pull")}
    for p in ps.values():
        p.add_argument("--name", default=DEFAULT_NAME)
        p.add_argument("--account", default=None)
    ps["start"].add_argument("--cap-usd", type=float, default=15.0)
    ps["start"].add_argument("--max-hours", type=float, default=24.0)
    ps["start"].add_argument("--budget-scope", default=None)
    for c in ("start", "loop"):
        ps[c].add_argument("--every", type=float, default=60)
    ps["loop"].add_argument("--max-hours", type=float, default=24.0)
    ps["loop"].add_argument("--work", default="supervisor_work")
    ps["enqueue-merge"].add_argument("prs", nargs="+", type=int)
    ps["enqueue-collect"].add_argument("configs", nargs="+")
    ps["pull"].add_argument("--dest", default=os.path.expanduser("~/modeling-agentic-keep/supervisor"))
    a = ap.parse_args(argv)
    if a.cmd == "loop":
        return loop(a)
    if a.cmd == "start":
        return cmd_start(a)
    if a.cmd == "stop":
        import run_cluster as rc
        import vm_job
        print(stop_own(a.name, live_id=rc.live_id, stop=vm_job._stop_by_id))
        return 0
    blob = Blob(a.account)
    if a.cmd == "enqueue-merge":
        print(enqueue(blob, a.name, "merge", {"prs": a.prs}))
    elif a.cmd == "enqueue-collect":
        for c in a.configs:
            print(enqueue(blob, a.name, "collect", _assignments(c)))
    elif a.cmd == "enqueue-noop":
        print(enqueue(blob, a.name, "noop", {}))
    elif a.cmd == "status":
        lines, code = status_lines(blob.get_json(paths(a.name)["status"]), time.time())
        print("\n".join(lines))
        return code
    elif a.cmd == "pull":
        got = pull(blob, a.name, os.path.join(a.dest, a.name))
        print(f"pulled {len(got)} file(s) into {os.path.join(a.dest, a.name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
