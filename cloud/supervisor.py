#!/usr/bin/env python3
"""Between-run supervision off the laptop (W0-J, small version; owner yes 2026-11-22: laptop sleep twice stalled all
work for about 8 hours). One loop on a small Coiled VM, all its state in blob storage; the laptop is only a client.

  (a) Merge train (ops/merge_train.sh's steps), one queued PR at a time: update the branch when it is behind its base,
      wait for CI on the head, merge at that head's sha once every check, commit status and the CI workflow run on it
      succeeded; skip and record anything else. Held until a GitHub token is provided ($GH_TOKEN, below).
  (b) Eval collection: each enqueued launch (a run_cluster.py config's assignments.json) is fetched label by label with
      run_cluster.collect_from_storage (storage only: no cluster is attached or stopped), archived to the supervisor's
      prefix, and scripts/harness_metrics.py runs over every collected run (metrics.json).
  (c) status.json, rewritten every loop: heartbeat, merge queue, collection, lease, renewal, paused runs (resume is
      not automated here), errors. Layout under outputs/supervisor/<name>/: requests/ (write-once, from the laptop),
      state.json and status.json (the loop's only), lease.json, budgets/<scope>.json (owner-signed records `start`
      copies, no secret), collected/<launch>/<label>.tar.gz, metrics.json. The storage account comes from
      $MA_STORAGE_ACCOUNT / $COILED_STORAGE_ACCOUNT or --account, never the code.
  (d) Self-renewal: one loop holds lease.json (every write a compare-and-swap on the blob's etag; free when not
      renewed for 15 min). 30 min before --max-hours the loop starts its successor (vm_job start on the same name
      with its shipped tree, the same secrets by name, --holder <id>), hands the lease to it and exits. A loop that
      finds another loop's live lease does nothing; a named successor waits for the handover. Renewal needs
      $DASK_COILED__TOKEN on the VM (held otherwise).

    python3 cloud/supervisor.py start [--name sv-supervisor-gcc] [--cap-usd 15] [--max-hours 24]
    python3 cloud/supervisor.py enqueue-merge <pr>... | enqueue-collect <config.py>... | enqueue-noop
    python3 cloud/supervisor.py status | pull [--dest ~/modeling-agentic-keep/supervisor] | stop
    python3 cloud/supervisor.py loop --name <name>      # on the VM; `start` runs it as a vm_job job
    python3 cloud/supervisor.py --self-test

`start` is vm_job `up` (2-vCPU worker, 2-vCPU scheduler; the name needs a budget scope, the default is in `sv`) then
`start` with the loop as the job, a dask task that holds the cluster for --max-hours. `stop` stops this name's live
cluster by id. Secrets reach the job's environment only, by name: $GH_TOKEN (vm_job --secret-env) when the login
keychain holds it under the service "modeling-agentic-supervisor-github" (without it merges stay held); the Coiled
token from the laptop's dask config ($DASK_COILED__TOKEN, --secret-env) and the Anthropic key (--anthropic-key) when
the laptop has them (without the Coiled token renewal stays held).
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


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def paths(name: str) -> dict:
    if not _NAME.fullmatch(name or ""):
        raise SystemExit(f"bad supervisor name {name!r}")
    b = f"{ROOT}/{name}"
    return {"requests": f"{b}/requests/", "state": f"{b}/state.json", "status": f"{b}/status.json",
            "collected": f"{b}/collected/", "metrics": f"{b}/metrics.json", "lease": f"{b}/lease.json",
            "budgets": f"{b}/budgets/"}


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
    def get_json_etag(self, blob):
        """(object, etag), or (None, None) when absent: the lease's compare-and-swap reads this."""
        try:
            d = self.svc.get_blob_client(*self.rs._split(blob)).download_blob()
            return json.loads(d.readall()), d.properties.etag
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "ResourceNotFoundError":
                return None, None
            raise
    def put_json_if(self, blob, obj, etag) -> bool:
        """Write only if the blob is unchanged since `etag` was read (absent when etag is None); False otherwise."""
        from azure.core import MatchConditions
        bc, data = self.svc.get_blob_client(*self.rs._split(blob)), json.dumps(obj, default=str).encode()
        try:
            if etag is None:
                bc.upload_blob(data, overwrite=False)
            else:
                bc.upload_blob(data, overwrite=True, etag=etag, match_condition=MatchConditions.IfNotModified)
            return True
        except Exception as e:  # noqa: BLE001 -- another writer came first: 409 (exists) or 412 (modified)
            if type(e).__name__ in ("ResourceExistsError", "ResourceModifiedError") or \
                    getattr(e, "status_code", None) in (409, 412):
                return False
            raise


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


# ---- (d) the lease and self-renewal ----------------------------

RENEW_LEAD_S = 30 * 60    # the successor is started this long before --max-hours runs out
RENEW_RETRY_S = 300
LEASE_TTL_S = 900         # a lease its holder has not renewed for this long is free
EVAL_SCOPE = "ev"
_SCOPE = re.compile(r"[a-z0-9]{1,16}")


def install_budget(scope, rec, budget_dir=None) -> bool:
    """Put an owner-signed budget record where budget.py reads it on this VM (verified by budget.py at each use)."""
    import budget
    if not (_SCOPE.fullmatch(str(scope)) and isinstance(rec, dict)):
        raise ValueError("a budget record needs a plain scope name and a record")
    p = os.path.join(budget_dir or budget.BUDGET_DIR, f"budget_{scope}.json")
    if os.path.exists(p) and json.load(open(p)) == rec:
        return False
    budget._write_private(p, json.dumps(rec, indent=1) + "\n")
    return True


class Cloud:
    """What the loop does to the cloud. Not used by the self-test, which passes FakeCloud."""

    def __init__(self, work, a):
        self.work, self.a = work, a

    def renew_disabled(self):
        return None if os.environ.get("DASK_COILED__TOKEN") else \
            "no $DASK_COILED__TOKEN on the VM (`start` passes it when the laptop holds it)"

    def start_successor(self, job_id, holder):
        cmd = start_commands(self.a, job_id, bool(os.environ.get("GH_TOKEN")), coiled=True,
                             anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")), tree=("--dir", REPO_ROOT),
                             holder=holder)[1]
        return subprocess.run(cmd, cwd=REPO_ROOT, timeout=1800).returncode


def lease_take(blob, name, me, now, host="") -> str:
    """mine | successor | other. A lease is held by one loop; another one's live lease (renewed within LEASE_TTL_S)
    is never taken, and every write is a compare-and-swap on the blob's etag, so two loops never both hold it."""
    p = paths(name)["lease"]
    lease, etag = blob.get_json_etag(p)
    if lease and lease.get("holder") == me:
        new = {**lease, "renewed": now, "renewed_utc": utc(now), "host": host}
    elif lease and now - float(lease.get("renewed") or 0) < LEASE_TTL_S:
        return "successor" if lease.get("successor") == me else "other"
    else:
        new = {"holder": me, "renewed": now, "renewed_utc": utc(now), "host": host, "successor": None,
               "taken_from": (lease or {}).get("holder")}
    return "mine" if blob.put_json_if(p, new, etag) else "other"


def wait_for_lease(blob, name, me, host="", clock=time.time, sleep=time.sleep, max_wait_s=2 * 3600) -> str:
    """At start: a successor waits for its predecessor's handover (or for the lease to lapse); anything else answers
    at once."""
    end = clock() + max_wait_s
    while True:
        s = lease_take(blob, name, me, clock(), host)
        if s != "successor" or clock() >= end:
            return s
        sleep(30)


def renew_step(blob, name, me, state, now, cloud) -> bool:
    """Start the successor (same name and state prefix, through vm_job start), then hand the lease to it. True once
    handed over: the caller exits. A failed start keeps the lease and is retried after RENEW_RETRY_S."""
    p, rn = paths(name), state.setdefault("renewal", {})
    why = cloud.renew_disabled()
    if why:
        rn.update(state="held", why=why, at=utc(now))
        return False
    if now - rn.get("tried", -1e18) < RENEW_RETRY_S:
        return False
    rn["tried"] = now
    lease, etag = blob.get_json_etag(p["lease"])
    succ, job = f"h-{secrets.token_hex(6)}", "sv-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    if not (lease and lease.get("holder") == me and blob.put_json_if(p["lease"], {**lease, "successor": succ}, etag)):
        return False
    try:
        code = cloud.start_successor(job, succ)
    except Exception as ex:  # noqa: BLE001
        code = f"{type(ex).__name__}: {str(ex)[:160]}"
    if code != 0:
        rn.update(state="failed", job_id=job, error=f"vm_job start: {code}", at=utc(now))
        lease, etag = blob.get_json_etag(p["lease"])
        if lease and lease.get("holder") == me:
            blob.put_json_if(p["lease"], {**lease, "successor": None}, etag)
        return False
    rn.update(state="handed_over", job_id=job, successor=succ, at=utc(now), error="")
    blob.put_json(p["state"], state)
    st = blob.get_json(p["status"]) or {}
    blob.put_json(p["status"], {**st, "renewal": {k: v for k, v in rn.items() if k != "tried"}})
    lease, etag = blob.get_json_etag(p["lease"])
    if lease and lease.get("holder") == me:
        blob.put_json_if(p["lease"], {"holder": succ, "renewed": now, "renewed_utc": utc(now), "successor": None,
                                      "taken_from": me}, etag)
    return True


def tick(blob, name, state, now, work, gh=None, fetch=fetch_from_storage, metrics=run_metrics, host="",
         merge_why="no GitHub token ($GH_TOKEN) on the VM: merges are held", lease=None) -> dict:
    """One pass: ingest new requests, one merge step, collection, then state.json and status.json. A failing part is
    recorded in errors and the rest still runs; status is always written."""
    p, state = paths(name), {"seen": [], "merge": [], "merge_done": [], "collect": {}, "acks": [], "errors": [],
                             **(state or {})}

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
    try:
        collect_step(blob, name, state, now, work, fetch=fetch, metrics=metrics)
    except Exception as e:  # noqa: BLE001
        err("collect", e)
    state["loops"] = int(state.get("loops", 0)) + 1
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
              "lease": lease or {},
              "renewal": {k: v for k, v in (state.get("renewal") or {}).items() if k != "tried"},
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
    host, me = f"{os.uname().nodename}:{os.getpid()}", a.holder or f"h-{secrets.token_hex(6)}"
    for i in range(21):                       # a storage blip at start-up must not end the supervisor
        try:
            got = wait_for_lease(blob, a.name, me, host)
            if got != "mine":
                print(f"{a.name}: a live lease is held by another loop: this loop does nothing", flush=True)
                return 0
            for b in blob.names(p["budgets"]):       # signed budget records `start` stored (verified at each use)
                install_budget(b[len(p["budgets"]):-len(".json")], blob.get_json(b))
            state = blob.get_json(p["state"]) or {}
            break
        except Exception as e:  # noqa: BLE001
            if i == 20:
                raise
            print(f"start-up read failed ({type(e).__name__}); retrying", flush=True)
            time.sleep(30)
    cloud, deadline = Cloud(work, a), time.time() + a.max_hours * 3600
    while True:
        now = time.time()
        try:
            held = lease_take(blob, a.name, me, now, host)
        except Exception as e:  # noqa: BLE001 -- unconfirmed: this pass does nothing
            held = f"unknown ({type(e).__name__})"
        if held in ("other", "successor"):
            print(f"{a.name}: the lease is no longer this loop's ({held}): exiting", flush=True)
            return 0
        if held == "mine":
            try:
                state = tick(blob, a.name, state, now, work, gh=gh, host=host, merge_why=why,
                             lease={"holder": me, "renewed_utc": utc(now), "deadline_utc": utc(deadline)})
                if deadline - now <= RENEW_LEAD_S and renew_step(blob, a.name, me, state, now, cloud):
                    print(f"{a.name}: handed over to {state['renewal']['successor']}: exiting", flush=True)
                    return 0
            except Exception as e:  # noqa: BLE001 -- the loop outlives one bad pass (storage blip, DNS)
                print(f"tick failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        if now >= deadline:
            return 0
        time.sleep(a.every)


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
    le, rn = st.get("lease") or {}, st.get("renewal") or {}
    if le:
        out.append(f"lease: {le.get('holder')} (renewed {le.get('renewed_utc')}; hands over before "
                   f"{le.get('deadline_utc')})")
    if rn:
        out.append(f"renewal: {rn.get('state')} {rn.get('job_id') or ''} {rn.get('why') or rn.get('error') or ''}".rstrip())
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
    json.dump(index, open(ip, "w"), indent=1)
    if got and st.get("metrics_at"):
        blob.get_file(paths(name)["metrics"], os.path.join(dest, "metrics.json"))
    return got


def start_commands(a, job_id, token_present, coiled=False, anthropic=False, tree=("--tree", "."), holder=None) -> list:
    """The two vm_job commands `start` runs: up (2-vCPU worker) and start (the loop as a job). The successor uses the
    second only (its own tree with --dir, its holder id). Secrets pass by name (--secret-env, --anthropic-key)."""
    vm = [sys.executable, "cloud/vm_job.py"]
    scope = ["--budget-scope", a.budget_scope] if a.budget_scope else []
    acct = ["--store-account", a.account] if a.account else []
    up = vm + ["up", a.name, "--cpu", "2", "--memory", "8 GiB", "--idle-timeout", "20 minutes",
               "--cap-usd", str(a.cap_usd)] + scope
    start = (vm + ["start", a.name, job_id, *tree, "--max-hours", str(a.max_hours + 0.25)] + acct
             + ["--cap-usd", str(a.cap_usd)] + scope
             + (["--secret-env", "GH_TOKEN"] if token_present else [])
             + (["--secret-env", "DASK_COILED__TOKEN"] if coiled else []) + (["--anthropic-key"] if anthropic else [])
             + ["--", "python3", "cloud/supervisor.py", "loop", "--name", a.name, "--every", str(int(a.every)),
                "--max-hours", str(a.max_hours), "--cap-usd", str(a.cap_usd)]
             + (["--account", a.account] if a.account else []) + scope + (["--holder", holder] if holder else []))
    return [up, start]


def _laptop_secrets() -> tuple:
    """(GitHub token, Coiled token, Anthropic key present) on the laptop; values are never printed."""
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True,
                       text=True)                      # the owner's one-time step stores it there
    gh = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    try:
        import coiled  # noqa: F401 -- loads the Coiled login into dask's config
        import dask
        co = dask.config.get("coiled.token", None)
    except Exception:  # noqa: BLE001
        co = None
    try:
        import run_cluster as rc
        an = bool(rc.read_keys().get("ANTHROPIC_API_KEY"))
    except OSError:
        an = False
    return gh, co, an


def cmd_start(a):
    tok, co, an = _laptop_secrets()
    job_id = "sv-" + time.strftime("%Y%m%d-%H%M%S")
    env = {**os.environ, **({"GH_TOKEN": tok} if tok else {}), **({"DASK_COILED__TOKEN": co} if co else {})}
    print(f"merge train: {'enabled (token from the keychain)' if tok else 'disabled (no token in the keychain)'}; "
          f"renewal: {'enabled' if co else 'held (no Coiled token here)'}")
    import budget
    blob = Blob(a.account)
    for scope in ("sv", EVAL_SCOPE):                    # signed records (no secret) for the VM's budget checks
        f = os.path.join(budget.BUDGET_DIR, f"budget_{scope}.json")
        if os.path.exists(f):
            blob.put_json(f"{paths(a.name)['budgets']}{scope}.json", json.load(open(f)))
    for cmd in start_commands(a, job_id, bool(tok), coiled=bool(co), anthropic=an):
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


class FakeBlob:
    def __init__(self):
        self.d, self.v = {}, {}
    def put_json(self, blob, obj):
        self.d[blob], self.v[blob] = json.dumps(obj, default=str).encode(), self.v.get(blob, 0) + 1
    def get_json_etag(self, blob):
        return self.get_json(blob), self.v.get(blob) if blob in self.d else None
    def put_json_if(self, blob, obj, etag):
        if (self.v.get(blob) if blob in self.d else None) != etag:
            return False
        self.put_json(blob, obj)
        return True
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
    """The loop's renewal actions, recorded (no Coiled call). `successor`: vm_job start's exit code."""
    def __init__(self, why=None, successor=0):
        self.why, self.successor, self.calls = why, successor, []
    def renew_disabled(self):
        return self.why
    def start_successor(self, job, holder):
        self.calls.append(("successor", job, holder))
        return self.successor


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

    # ---- the lease and self-renewal (SV37-SV40) ----
    import types
    from unittest import mock
    # SV37 the lease: one holder at a time, a compare-and-swap on the etag; a live lease of another is never taken
    b = FakeBlob()
    ok(lease_take(b, N, "A", T0) == "mine" and lease_take(b, N, "B", T0 + 10) == "other"
       and lease_take(b, N, "A", T0 + 20) == "mine" and lease_take(b, N, "B", T0 + 20 + LEASE_TTL_S + 1) == "mine"
       and lease_take(b, N, "A", T0 + 20 + LEASE_TTL_S + 2) == "other",
       "SV37 a live lease is the holder's; a lapsed one is taken, and its old holder then sees another's")
    b = FakeBlob()
    _, tag0 = b.get_json_etag(P["lease"])
    ok(b.put_json_if(P["lease"], {"holder": "A"}, tag0) and not b.put_json_if(P["lease"], {"holder": "B"}, tag0),
       "SV37b two loops racing for a free lease: the second write fails")
    b = FakeBlob()
    lease_take(b, N, "A", time.time())
    before = dict(b.d)
    a = argparse.Namespace(account="acct", name=N, work="~/w", holder=None, max_hours=1.0, every=1)
    with mock.patch.dict(globals(), {"Blob": lambda acct: b}), contextlib.redirect_stdout(io.StringIO()):
        rc_ = guard(loop, a)
    ok(rc_ == 0 and b.d == before, f"SV37c a loop that finds another loop's live lease does nothing ({rc_})")
    # SV38 renewal: the successor starts through vm_job (same name), then the lease is handed to it; a failed start
    # keeps the lease and is retried after RENEW_RETRY_S; no cloud access: held
    b, cl, st = FakeBlob(), FakeCloud(), {}
    lease_take(b, N, "A", T0)
    done = guard(renew_step, b, N, "A", st, T0 + 5, cl)
    succ = st.get("renewal", {}).get("successor")
    ok(done is True and [c[0] for c in cl.calls] == ["successor"] and cl.calls[0][2] == succ
       and (b.get_json(P["lease"]) or {}).get("holder") == succ and lease_take(b, N, succ, T0 + 6) == "mine"
       and lease_take(b, N, "A", T0 + 7) == "other"
       and (b.get_json(P["status"]) or {}).get("renewal", {}).get("state") == "handed_over",
       f"SV38 the successor is started, then holds the lease; the predecessor no longer does ({cl.calls}, {st})")
    b, cl, st = FakeBlob(), FakeCloud(successor=1), {}
    lease_take(b, N, "A", T0)
    r1_ = guard(renew_step, b, N, "A", st, T0 + 5, cl)
    r2_ = guard(renew_step, b, N, "A", st, T0 + 6, cl)
    lz = b.get_json(P["lease"]) or {}
    ok(r1_ is False and r2_ is False and len(cl.calls) == 1 and lz.get("holder") == "A" and lz.get("successor") is None
       and st["renewal"]["state"] == "failed", f"SV38b a failed successor start keeps the lease, retried later ({lz})")
    st = {}
    ok(guard(renew_step, b, N, "A", st, T0 + 5, FakeCloud(why="no $DASK_COILED__TOKEN")) is False
       and st["renewal"]["state"] == "held", "SV38c without cloud access renewal is held")
    # SV39 a successor waits for the handover; then it holds the lease
    b = FakeBlob()
    lease_take(b, N, "A", time.time())
    lz, tag1 = b.get_json_etag(P["lease"])
    b.put_json_if(P["lease"], {**lz, "successor": "S"}, tag1)
    hand = lambda s_: (lambda l, t: b.put_json_if(P["lease"], {**l, "holder": "S", "successor": None}, t))(  # noqa: E731
        *b.get_json_etag(P["lease"]))
    ok(lease_take(b, N, "S", time.time()) == "successor" and guard(wait_for_lease, b, N, "S", sleep=hand) == "mine",
       "SV39 the named successor waits, and holds the lease after the handover")
    # SV40 the successor's command: vm_job start on the same name with the shipped dir, secrets by name, its holder
    a = argparse.Namespace(name=DEFAULT_NAME, cap_usd=15.0, max_hours=24.0, budget_scope=None, account="acct1", every=60)
    sent_ = []
    with mock.patch.object(subprocess, "run", lambda cmd, **k: sent_.append(cmd) or types.SimpleNamespace(returncode=0)), \
            mock.patch.dict(os.environ, {"GH_TOKEN": "x" * 30, "ANTHROPIC_API_KEY": "k" * 30}):
        rc_ = guard(Cloud("/w", a).start_successor, "sv-2", "h-1")
    flat = " ".join(sent_[0]) if sent_ else ""
    ok(rc_ == 0 and len(sent_) == 1 and " start " + DEFAULT_NAME + " sv-2 --dir " + REPO_ROOT in flat
       and " up " not in flat and "--secret-env GH_TOKEN" in flat and "--secret-env DASK_COILED__TOKEN" in flat
       and "--anthropic-key" in flat and "loop --name " + DEFAULT_NAME in flat and "--holder h-1" in flat
       and "x" * 30 not in flat, f"SV40 the successor: vm_job start, same name, secrets by name only ({flat})")    # SV41a status shows the lease holder and the renewal
    lines = guard(status_lines, {"heartbeat_utc": utc(T0), "lease": {"holder": "h-9", "renewed_utc": utc(T0),
                                 "deadline_utc": utc(T0 + 3600)}, "renewal": {"state": "failed", "job_id": "sv-2",
                                                                               "error": "vm_job start: 1"}}, T0 + 30)
    ok("lease: h-9" in "\n".join(lines[0]) and "renewal: failed sv-2 vm_job start: 1" in "\n".join(lines[0]),
       f"SV41a status shows the lease and the renewal ({lines})")

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
    for c, p in ps.items():
        p.add_argument("--name", default=DEFAULT_NAME)
        p.add_argument("--account", default=None)
        if c in ("start", "loop"):
            p.add_argument("--every", type=float, default=60)
            p.add_argument("--max-hours", type=float, default=24.0)
    for c in ("start", "loop"):
        ps[c].add_argument("--cap-usd", type=float, default=15.0)
        ps[c].add_argument("--budget-scope", default=None)
    ps["loop"].add_argument("--work", default="~/supervisor_work")    # outside the tree a successor ships
    ps["loop"].add_argument("--holder", default=None, help=argparse.SUPPRESS)   # set by the predecessor
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
            "enqueue-collect": lambda: [("collect", _assignments(c)) for c in a.configs]}[a.cmd]()
    for kind, payload in reqs:
        print(enqueue(blob, a.name, kind, payload))
    return 0

if __name__ == "__main__":
    sys.exit(main())
