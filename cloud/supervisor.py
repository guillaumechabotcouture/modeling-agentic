#!/usr/bin/env python3
"""Between-run supervision off the laptop (W0-J, small version; owner yes 2026-11-22: laptop sleep twice stalled all
work for about 8 hours). One loop on a small Coiled VM, all its state in blob storage; the laptop is only a client.

  (a) Merge train (ops/merge_train.sh's steps), one queued PR at a time: update the branch when it is behind its base,
      wait for CI on the head, merge at that head's sha once every check, commit status and the CI workflow run on it
      succeeded; skip and record anything else. Held until a GitHub token is provided ($GH_TOKEN, below).
  (b) Eval collection: each enqueued launch (a run_cluster.py config's assignments.json) is fetched label by label with
      run_cluster.collect_from_storage (storage only: no cluster is attached or stopped), archived to the supervisor's
      prefix, and scripts/harness_metrics.py runs over every collected run (metrics.json).
  (c) status.json, rewritten every loop: heartbeat, merge queue, collection, the run monitor's report, paused runs
      (resume is not automated here), errors. Layout under outputs/supervisor/<name>/: requests/<id>.json (write-once,
      from the laptop; only a name of exactly that form is read), state.json and status.json (the loop's only;
      state.json first), collected/<launch>/<label>.tar.gz, metrics.json. Error, note and run-status text is redacted
      before it is stored (run_health's patterns, Coiled's `ApiToken` header, and the VM's own secret values). The
      storage account comes from $MA_STORAGE_ACCOUNT / $COILED_STORAGE_ACCOUNT or --account, never the code.
  (d) Run monitor, REPORTING ONLY (launches stay on the laptop; the VM never deletes, stops or checkpoints anything):
      `enqueue-collect <config>` also carries the launch's recorded cluster ids from clusters.json (only when it is
      this launch's: the invocation and launch id of every assignment; helpers only under the config's own names; a
      legacy record without an invocation is collection only) and optional stop rules (--stop-tag-usd, --stop-run-usd,
      --stop-no-progress-min, --stop-same-failure K). A launch is its launch id and invocation. Every 5 min per watched
      launch, each launch on its own and under a deadline: each run's state, phase, heartbeat age, last progress, ETA
      and failure kind (a collected run's status, except eval outcomes and `awaiting_*` / `paused:*`; a dead run; exit
      1); the launch's USD at the scopes file's floor rate (budget._cluster_cost per recorded id, unrounded); and each
      declared rule's evaluation, `would_trip` true / false with its reason, computed only from that fresh probe. A
      failed or late probe, or a run whose inputs could not all be read, is `unknown`, never "no progress". Tag spend
      and failures are kept per launch (state.json `tags`), so a reused name loses nothing. `status` prints it as
      tables; the laptop acts on `would_trip`. It needs $DASK_COILED__TOKEN on the VM (held otherwise).

    python3 cloud/supervisor.py start [--name sv-supervisor-gcc] [--cap-usd 15] [--max-hours 168]
    python3 cloud/supervisor.py enqueue-merge <pr>... | enqueue-noop
    python3 cloud/supervisor.py enqueue-collect <config.py>... [--tag T] [--stop-tag-usd X] [--stop-run-usd Y]
        [--stop-no-progress-min M] [--stop-same-failure K]
    python3 cloud/supervisor.py status | pull [--dest ~/modeling-agentic-keep/supervisor] | stop
    python3 cloud/supervisor.py loop --name <name>      # on the VM; `start` runs it as a vm_job job
    python3 cloud/supervisor.py --self-test

`start` is vm_job `up` (2-vCPU worker, 2-vCPU scheduler; the name needs a budget scope, the default is in `sv`) then
`start` with the loop as the job, a dask task that holds the cluster for --max-hours. `stop` stops this name's live
cluster by id. GitHub token: `start` passes $GH_TOKEN (vm_job --secret-env: the job's environment only) when the login
keychain holds it under the service "modeling-agentic-supervisor-github"; without it merges stay held. The Coiled
token the run monitor needs reaches the job by name through a separate change (vm_job --secret-env DASK_COILED__TOKEN).
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import math
import threading
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
_RID = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{8}")       # request ids as `enqueue` makes them
HEALTH_EVERY_S = 300      # each watched launch is probed this often
PROBE_TIMEOUT_S = 240     # one launch's probe (listing, costs, health reads) past this is reported, not waited on
STOP_RULES = {"tag_usd": float, "run_usd": float, "no_progress_min": float, "same_failure_runs": int}
OK_STATUSES = ("completed", "completed_with_report_restored", "completed_scope_declared",   # eval outcomes
               "completed_gate_not_accepted", "completed_scope_declined", "completed_no_report")   # an eval outcome, not a failure kind
_ENDED = ("stopped", "error")
_LAUNCH_ID = re.compile(r"[A-Za-z0-9_.-]{1,40}")          # one path component
_ROLES = {"run": "", "keeper": "-keeper", "overflow": "-overflow"}
_TAG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


_APITOKEN = re.compile(r"(?i)\b(ApiToken|Bearer|token)(\s*[:=]?\s*)[A-Za-z0-9_.~+/=-]{6,}")


def _red(x, n=200) -> str:
    """Free text, redacted before it is stored or printed: this VM's own secret values, Coiled's `ApiToken` header
    (and Bearer / token= forms), then run_health.clip (its patterns, then cut)."""
    import run_health
    t = str(x)
    for k in ("DASK_COILED__TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY"):
        v = os.environ.get(k) or ""
        if len(v) >= 8:
            t = t.replace(v, "<redacted>")
    return run_health.clip(_APITOKEN.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", t), n)


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def paths(name: str) -> dict:
    if not _NAME.fullmatch(name or ""):
        raise SystemExit(f"bad supervisor name {name!r}")
    b = f"{ROOT}/{name}"
    return {"requests": f"{b}/requests/", "state": f"{b}/state.json", "status": f"{b}/status.json",
            "collected": f"{b}/collected/", "metrics": f"{b}/metrics.json"}


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


def _run_status(d):
    """metadata.json's status of the last run under `d`, or None."""
    for rd in reversed(run_dirs(d)):
        try:
            return _red((json.load(open(os.path.join(rd, "metadata.json"))) or {}).get("status") or "", 120) or None
        except (OSError, ValueError, AttributeError):
            continue
    return None


def collect_step(blob, name, state, now, work, fetch=fetch_from_storage, metrics=run_metrics) -> None:
    """Try every enqueued label not yet collected (at most every COLLECT_EVERY_S); archive each new one to blob."""
    import results_store
    import shutil
    for lname, ent in sorted((state.get("collect") or {}).items()):
        for label, a in sorted(ent["entry"]["assignments"].items()):
            lab = ent["labels"].setdefault(label, {})
            if (lab.get("launch_id"), lab.get("invocation")) != (a.get("launch_id"), a.get("invocation")):
                shutil.rmtree(os.path.join(work, lname, label), ignore_errors=True)   # another launch's copy
                lab.clear()
                lab.update(state="waiting", launch_id=a.get("launch_id"), invocation=a.get("invocation"))
            if lab.get("state") == "collected" and "run_status" not in lab and os.path.isdir(os.path.join(work, lname, label)):
                lab["run_status"] = _run_status(os.path.join(work, lname, label))   # collected before this field
            if lab.get("state") == "collected" or now - lab.get("tried", -1e18) < COLLECT_EVERY_S:
                continue
            lab["tried"] = now
            try:
                meta = fetch(ent["entry"], label, work)
            except (Exception, SystemExit) as e:  # noqa: BLE001 -- not stored yet, or unreadable: tried again later
                lab["note"] = _red(f"{type(e).__name__}: {e}", 160)
                continue
            src = os.path.join(work, lname, label)
            with tempfile.TemporaryDirectory() as td:
                tgz = os.path.join(td, f"{label}.tar.gz")
                skipped = results_store.write_archive(src, label, tgz)
                sha = results_store.sha256_file(tgz)
                dst = f"{paths(name)['collected']}{lname}/{label}.tar.gz"
                blob.put_file(tgz, dst)
            lab.update(state="collected", blob=dst, sha256=sha, at=utc(now), note="",
                       exit_code=(meta or {}).get("exit_code"), left_out=len(skipped), run_status=_run_status(src))
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


# ---- (d) the run monitor (reporting only) ------------------------------------------------------------------------

def _stop_rules(stop) -> dict:
    num = lambda k, v: (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0  # noqa: E731
                        and (STOP_RULES[k] is float or isinstance(v, int)))
    if not (isinstance(stop, dict) and set(stop) <= set(STOP_RULES) and all(num(k, v) for k, v in stop.items())):
        raise ValueError(f"stop rules: finite positive numbers among {sorted(STOP_RULES)} (same_failure_runs an integer)")
    return {k: STOP_RULES[k](v) for k, v in stop.items()}


def _utc_epoch(v):
    try:
        return float(calendar.timegm(time.strptime(str(v), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, OverflowError):
        return None


def _key(lname, w) -> str:
    return f"{lname}@{w['launch_id']}@{w['invocation']}"


def valid_watch(w, entry) -> dict:
    """A collect request's `watch`, or ValueError: the run cluster's id and any helper's, each under the launch's own
    name; a launch id (one path component), invocation and run id that every assignment records (none missing); a tag
    and finite positive stop rules. A launch time that does not parse is none."""
    cl = w.get("clusters") if isinstance(w, dict) else None
    ok_ids = isinstance(cl, dict) and "run" in cl and set(cl) <= set(_ROLES) and all(
        isinstance(r, dict) and r.get("name") == entry["name"] + _ROLES[k] and isinstance(r.get("id"), int)
        and not isinstance(r.get("id"), bool) and r["id"] > 0 for k, r in cl.items())
    agrees = ok_ids and all(isinstance(a, dict) and a.get("launch_id") == w.get("launch_id") and "invocation" in a
                            and a["invocation"] == w.get("invocation") and a.get("cluster_id") == cl["run"]["id"]
                            for a in entry["assignments"].values())
    if not (agrees and _LAUNCH_ID.fullmatch(str(w.get("launch_id", ""))) and isinstance(w.get("invocation"), str)
            and w["invocation"] and _TAG.fullmatch(str(w.get("tag", "")))):
        raise ValueError("watch: needs the run cluster's id (helpers under the launch's own names), and a launch id, "
                         "invocation and run id that every assignment records, and a tag")
    num = lambda x, top: x if isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x <= top else None  # noqa: E731
    return {"launch_id": w["launch_id"], "invocation": w["invocation"], "tag": w["tag"],
            "stop": _stop_rules(w.get("stop") or {}),
            "clusters": {k: {"name": r["name"], "id": r["id"]} for k, r in cl.items()},
            "cap_usd": num(w.get("cap_usd"), 1e6), "run_timeout_hours": num(w.get("run_timeout_hours"), 1000),
            "max_rounds": num(w.get("max_rounds"), 100), "launched_at": _utc_epoch(w.get("launched_utc"))}


def watch_payload(cfg, assign, stop, tag):
    """On the laptop: the `watch` for a collect request, from cfg.clusters_file, only when that record is this launch's
    (an invocation; every assignment's invocation, launch id and run cluster id); helpers only under the config's own
    names. None when it cannot be bound (an unreadable or legacy record included); SystemExit then if stop rules were
    asked for."""
    try:
        rec = json.load(open(cfg.clusters_file)) if os.path.exists(cfg.clusters_file) else {}
    except (OSError, ValueError):
        rec = {}
    rec = rec if isinstance(rec, dict) else {}
    cl = {k: {"name": rec[k]["name"], "id": rec[k]["id"]} for k, suf in _ROLES.items()
          if isinstance(rec.get(k), dict) and rec[k].get("name") == cfg.name + suf and rec[k].get("id") is not None}
    bound = rec.get("invocation") and rec.get("launch_id") and "run" in cl and all(
        isinstance(a, dict) and a.get("launch_id") == rec["launch_id"] and a.get("invocation") == rec["invocation"]
        and a.get("cluster_id") == cl["run"]["id"] for a in assign.values())
    if not bound:
        if stop:
            raise SystemExit(f"{cfg.name}: clusters.json is not this launch's (invocation, launch id and run id of "
                             "assignments.json): no stop rule can be evaluated on its ids; refusing")
        return None
    m = re.fullmatch(r"runs/evals/([a-z0-9-]+)/[a-z0-9_]+", cfg.storage_prefix or "")
    return {"launch_id": rec["launch_id"], "invocation": rec["invocation"], "clusters": cl,
            "tag": tag or (m.group(1) if m else cfg.name), "stop": _stop_rules(stop or {}),
            "cap_usd": cfg.cap_usd, "run_timeout_hours": cfg.run_timeout_hours, "max_rounds": cfg.max_rounds,
            "launched_utc": rec.get("launched_utc")}


def spend_of(states, costs):
    """The launch's USD: the sum over every id it ever recorded, or None when one is listed under another name (its
    cost is not this launch's) or could not be read."""
    if any(v == "listed under another name" for v in states.values()) or any(c is None for c in costs.values()):
        return None
    return float(sum(costs.values()))


def label_complete(m, snap) -> bool:
    """A run's inputs were all read: its state, and for a live run its phase and heartbeat, and its files."""
    return bool(m.get("state")) and not (snap or {}).get("input_errors") and (
        m["state"] not in ("running", "preparing") or (m.get("phase") is not None and m.get("heartbeat_age_s") is not None))


class Cloud:
    """The monitor's Coiled reads: the full listing, costs and health, by recorded id. It has no path that deletes,
    stops or checkpoints a cluster. Not used by the self-test, which passes FakeCloud."""

    def disabled(self):
        return None if os.environ.get("DASK_COILED__TOKEN") else \
            "no $DASK_COILED__TOKEN on the VM (`start` passes it when the laptop's dask config holds it)"

    def probe(self, w, entry):
        import types
        import budget
        import run_cluster as rc
        import run_health as rh
        import worker_side as ws
        listing = budget.CoiledSource().list_clusters()                  # every page (max_pages=None)
        items = {c.get("id"): c for c in listing}
        named = lambda cid, nm: (items[cid].get("name") == nm) if cid in items else None  # noqa: E731
        states = {k: ("not listed" if r["id"] not in items else ws.state_of(items[r["id"]])
                      if named(r["id"], r["name"]) else "listed under another name") for k, r in w["clusters"].items()}
        floor, prices, costs = budget.load_scopes()["floor"], budget.load_prices(), {}
        for cid, nm in (w.get("seen_ids") or {}).items():
            if named(int(cid), nm) is False:
                states[f"earlier {cid}"] = "listed under another name"
                continue
            try:
                costs[cid] = budget._cluster_cost(budget.CoiledSource().details(int(cid)), time.time(), -math.inf,
                                                  floor, prices)["usd"]
            except budget.BudgetError:
                costs[cid] = None
        out = {"clusters": states, "usd": spend_of(states, costs), "labels": {}}
        if states.get("run") in ("ready", "running"):
            asg = entry["assignments"]
            cfg = types.SimpleNamespace(name=entry["name"], labels=tuple(sorted(asg)), compute_env={})
            client = rc._connect_recorded(cfg, w["clusters"]["run"]["id"], lister=lambda: listing)
            try:
                snaps = rc.health_snapshots(cfg, client, asg)
                for label, a in asg.items():
                    snap = snaps.get(label) or {}
                    try:
                        m = rc._on_worker(client, ws.fetch_meta, label, worker=a["worker"])
                    except Exception:  # noqa: BLE001 -- this run's inputs are incomplete: unknown, not "no progress"
                        m = {}
                    tr = snap.get("trace") or []
                    out["labels"][label] = {
                        "complete": label_complete(m, snap), "state": m.get("state"), "phase": m.get("phase"),
                        "heartbeat_age_s": m.get("heartbeat_age_s"), "exit_code": (m.get("meta") or {}).get("exit_code"),
                        "round": rh._current_round(snap),
                        "progress": [m.get("log_bytes"), m.get("phase"), len(tr), (tr[-1] or {}).get("ts") if tr else None]}
            finally:
                client.close()
        return out


def failure_kind(lab):
    """A terminal failure: a collected run's status (none for an eval outcome or an owner pause, `awaiting_*` /
    `paused:*`), a dead run, or exit 1 (finished or collected)."""
    st = lab.get("run_status")
    if st:
        return None if st in OK_STATUSES or str(st).startswith(("awaiting", "paused")) else st
    if lab.get("state") == "dead":
        return "dead"
    return "exit_1" if lab.get("state") in ("done", "collected") and lab.get("exit_code") == 1 else None


def eta_of(lab, w, now):
    """Rounds done over max_rounds, from the launch time, capped at run_timeout_hours; None before a round is done or
    without a launch time."""
    done, t0 = (lab.get("round") or 1) - 1, w.get("launched_at")
    if done < 1 or not w.get("max_rounds") or t0 is None or lab.get("state") not in ("running", "preparing"):
        return None
    t = t0 + (now - t0) * w["max_rounds"] / done
    return min(t, t0 + w["run_timeout_hours"] * 3600) if w.get("run_timeout_hours") else t


def _hist(state, lname, w):
    """This launch's entry in its tag's history (one per launch: a reused name loses nothing)."""
    return state["tags"].setdefault(w["tag"], {}).setdefault(_key(lname, w), {"usd": None, "failures": {}})


def _refresh(state, lname, w, now) -> None:
    """Fold the collection's results into the labels and the tag history, for an ended launch too. A result counts
    only for this launch (launch id and invocation); one collected before run_status was recorded is unknown."""
    col = (state["collect"].get(lname) or {}).get("labels", {})
    for label, c in col.items():
        if c.get("state") == "collected" and (c.get("launch_id"), c.get("invocation")) == (w["launch_id"], w["invocation"]):
            lab = w["labels"].setdefault(label, {"last_progress": now})
            lab.update(state="collected", exit_code=c.get("exit_code"), run_status=c.get("run_status"),
                       status_unknown="run_status" not in c, complete="run_status" in c)
    for lab in w["labels"].values():
        lab["failure"] = failure_kind(lab)
    h = _hist(state, lname, w)
    h.update(failures={k: v.get("failure") for k, v in w["labels"].items()},
             complete=bool(w["labels"]) and all(v.get("complete") for v in w["labels"].values()),
             final=w["state"] == "ended" or h.get("final", False) and w["state"] != "watching")


def _apply(state, lname, w, now, pr_) -> None:
    """One successful probe into the watch (all text redacted; progress kept as a digest only)."""
    entry = (state["collect"].get(lname) or {}).get("entry") or {}
    w["cluster_states"] = {k: _red(v, 40) for k, v in (pr_.get("clusters") or {}).items()}
    for label in sorted(set(entry.get("assignments") or {}) | set(pr_.get("labels") or {})):
        lab, got = w["labels"].setdefault(label, {"last_progress": now}), (pr_.get("labels") or {}).get(label)
        if got:
            dig = hashlib.sha256(json.dumps(got.get("progress"), default=str).encode()).hexdigest()[:16]
            if dig != lab.get("progress"):
                lab.update(progress=dig, last_progress=now)
            lab.update(state=_red(got.get("state"), 20) if got.get("state") else None,
                       phase=_red(got.get("phase"), 40) if got.get("phase") else None,
                       heartbeat_age_s=got.get("heartbeat_age_s") if isinstance(got.get("heartbeat_age_s"), (int, float)) else None,
                       exit_code=got.get("exit_code") if isinstance(got.get("exit_code"), int) else None,
                       round=got.get("round") if isinstance(got.get("round"), int) else None, complete=bool(got.get("complete")))
        elif lab.get("state") != "collected":
            lab["complete"] = False
        lab["last_progress_utc"] = utc(lab["last_progress"])
        t = eta_of(lab, w, now)
        lab["eta_utc"] = utc(t) if t else None
    usd = pr_.get("usd")
    w["usd"] = float(usd) if isinstance(usd, (int, float)) and math.isfinite(usd) else None
    w["usd_per_run"] = None if w["usd"] is None else w["usd"] / max(1, len(w["labels"]))
    _hist(state, lname, w).update(usd=w["usd"], fresh_at=now, stale=False)
    if pr_.get("clusters") and all(v in _ENDED for v in pr_["clusters"].values()):
        w["state"] = "ended"
    _refresh(state, lname, w, now)


def evaluate(state, lname, w, now, held=False) -> dict:
    """{rule: {would_trip: True | False | "unknown", reason, at}}: from this launch's last probe when it is fresh and
    complete (an ended launch: its final reading) and the tag's history, every launch of which must be fresh or final;
    compared unrounded. Held, stale, failed or incomplete: unknown."""
    r = w.get("stop") or {}
    unk = lambda why: {k: {"would_trip": "unknown", "reason": why, "at": utc(now)} for k in r}  # noqa: E731
    ended = w["state"] == "ended"
    if held and not ended:
        return unk("the monitor is held (no Coiled token on the VM)")
    if not ended and not (w.get("probe_ok") and now - w.get("probe_ok_at", -1e18) <= 2 * HEALTH_EVERY_S):
        return unk(w.get("note") or "no fresh probe yet")
    tag, labs, out = state["tags"].get(w["tag"]) or {}, w.get("labels") or {}, {}
    ok_peer = lambda h: h.get("final") or (not h.get("stale") and now - h.get("fresh_at", -1e18) <= 2 * HEALTH_EVERY_S)  # noqa: E731
    peers_ok = all(ok_peer(h) for h in tag.values())
    if "tag_usd" in r:
        read = [h for h in tag.values() if h.get("usd") is not None]
        if not peers_ok or any(h.get("usd") is None and not h.get("final") for h in tag.values()):
            out["tag_usd"] = {"would_trip": "unknown", "reason": f"not every launch of tag {w['tag']} has a fresh reading"}
        else:
            tot = sum(h["usd"] for h in read)
            out["tag_usd"] = {"would_trip": tot >= r["tag_usd"], "reason": f"tag {w['tag']}: ${tot:.4f} over {len(read)} "
                              f"launch(es) read; rule ${r['tag_usd']:.2f}"}
    if "run_usd" in r:
        out["run_usd"] = ({"would_trip": "unknown", "reason": "spend not fully read"} if w.get("usd") is None else
                          {"would_trip": w["usd"] / max(1, len(labs)) >= r["run_usd"],
                           "reason": f"${w['usd'] / max(1, len(labs)):.4f} per run over {max(1, len(labs))} run(s); "
                                     f"rule ${r['run_usd']:.2f}"})
    if "no_progress_min" in r:
        hits = [f"{lb}: no progress for {(now - x.get('last_progress', now)) / 60:.0f} min and no ETA"
                for lb, x in sorted(labs.items()) if x.get("state") in ("running", "preparing") and x.get("eta_utc") is None
                and now - x.get("last_progress", now) >= r["no_progress_min"] * 60]
        incomplete = sorted(lb for lb, x in labs.items() if not x.get("complete"))
        out["no_progress_min"] = (
            {"would_trip": False, "reason": "the launch's clusters have ended"} if ended else
            {"would_trip": "unknown", "reason": f"inputs not all read: {incomplete or 'no runs read'}"}
            if incomplete or not labs else
            {"would_trip": True, "reason": "; ".join(hits)} if hits else
            {"would_trip": False, "reason": f"every run progressed or has an ETA (rule {r['no_progress_min']:g} min)"})
    if "same_failure_runs" in r:
        kinds: dict = {}
        for h in tag.values():
            for k in (h.get("failures") or {}).values():
                if k:
                    kinds[k] = kinds.get(k, 0) + 1
        top = max(kinds.items(), key=lambda kv: kv[1]) if kinds else (None, 0)
        why = f"most common failure in tag {w['tag']}: {top[0] or 'none'} x{top[1]}; rule {r['same_failure_runs']}"
        out["same_failure_runs"] = (
            {"would_trip": True, "reason": why} if top[1] >= r["same_failure_runs"] else
            {"would_trip": "unknown", "reason": why + "; not every run's outcome is known"}
            if not peers_ok or not all(h.get("complete") or h.get("final") for h in tag.values()) else
            {"would_trip": False, "reason": why})
    return {k: {**v, "reason": _red(v["reason"]), "at": utc(now)} for k, v in out.items()}


_INFLIGHT: dict = {}     # launch key -> the thread of a probe that has not returned (this process only)


def _run_probe(cloud, w, entry, box):
    try:
        box["v"] = cloud.probe(w, entry)
    except BaseException as e:  # noqa: BLE001 -- SystemExit from a connect that refuses included
        box["e"] = e


def watch_step(blob, name, state, now, cloud) -> None:
    """Reporting only (nothing here deletes, stops or checkpoints). Fold collection results into every watch; start
    every due probe at once, each on its own thread under one deadline (a probe still running from an earlier pass
    is not started again); apply each result; then evaluate every watch's rules, after all probes, so launches of
    one tag agree. One launch's failure never touches another's."""
    off = cloud.disabled()
    for lname, w in sorted(state["watch"].items()):
        try:
            _refresh(state, lname, w, now)
        except Exception as e:  # noqa: BLE001
            w["note"] = _red(f"{type(e).__name__}: {e}")
    boxes = {}
    for lname, w in sorted(state["watch"].items()):
        if off or w["state"] != "watching" or now - w.get("health_at", -1e18) < HEALTH_EVERY_S:
            continue
        w["health_at"], key = now, _key(lname, w)
        if key in _INFLIGHT and _INFLIGHT[key].is_alive():
            w.update(probe_ok=False, note="the previous probe has not returned yet")
            _hist(state, lname, w)["stale"] = True
            continue
        box, entry = {}, json.loads(json.dumps((state["collect"].get(lname) or {}).get("entry") or {}))
        th = threading.Thread(target=_run_probe, args=(cloud, json.loads(json.dumps(w)), entry, box), daemon=True)
        th.start()
        _INFLIGHT[key], boxes[lname] = th, (th, box)
    end = time.monotonic() + PROBE_TIMEOUT_S
    for th, _ in boxes.values():
        th.join(max(0.0, end - time.monotonic()))
    for lname, (th, box) in boxes.items():
        w = state["watch"][lname]
        try:
            if th.is_alive():
                raise TimeoutError(f"probe did not return within {PROBE_TIMEOUT_S:g} s")
            if "e" in box:
                raise box["e"]
            _apply(state, lname, w, now, box["v"])
            w.update(probe_ok=True, probe_ok_at=now, note="")
        except (Exception, SystemExit) as e:  # noqa: BLE001 -- unknown, and the others go on
            w.update(probe_ok=False, note=_red(f"probe failed: {type(e).__name__}: {e}"))
            _hist(state, lname, w)["stale"] = True
    for lname, w in sorted(state["watch"].items()):
        try:
            w["rules"] = evaluate(state, lname, w, now, held=bool(off))
        except Exception as e:  # noqa: BLE001
            w["rules"] = {k: {"would_trip": "unknown", "reason": _red(f"{type(e).__name__}: {e}"), "at": utc(now)}
                          for k in w.get("stop") or {}}


def sleep_for(state, now, every, watching=True) -> float:
    """Seconds until the next due check, at most `every`: a watched launch's probe (not while the monitor is held), a
    current label's collection retry."""
    due = [float(every)]
    for w in (state.get("watch") or {}).values() if watching else ():
        if w.get("state") == "watching":
            due.append(w.get("health_at", -1e18) + HEALTH_EVERY_S - now)
    for ent in (state.get("collect") or {}).values():
        cur = set(((ent.get("entry") or {}).get("assignments") or {}))
        for label, lab in (ent.get("labels") or {}).items():
            if label in cur and lab.get("state") != "collected":
                due.append(lab.get("tried", -1e18) + COLLECT_EVERY_S - now)
    return max(1.0, min(due))


_HIDDEN = ("health_at", "launched_at", "seen_ids")


def tick(blob, name, state, now, work, gh=None, fetch=fetch_from_storage, metrics=run_metrics, host="",
         merge_why="no GitHub token ($GH_TOKEN) on the VM: merges are held", cloud=None) -> dict:
    """One pass: ingest new requests, one merge step, collection, the run monitor, then state.json and status.json. A
    failing part is recorded in errors and the rest still runs. `state` is updated in place, so a pass that fails
    (a status write) loses no decision: the next pass neither ingests a request again nor repeats a save."""
    p, state = paths(name), ({} if state is None else state)
    for k, v in (("seen", []), ("merge", []), ("merge_done", []), ("collect", {}), ("acks", []), ("errors", []),
                 ("watch", {}), ("tags", {})):
        state.setdefault(k, v)

    def err(where, e):
        state["errors"] = (state["errors"] + [{"at": utc(now), "where": _red(where, 80),
                                                "error": _red(f"{type(e).__name__}: {e}")}])[-KEEP:]
    try:
        names = blob.names(p["requests"])
    except Exception as e:  # noqa: BLE001
        names = []
        err("requests", e)
    seen = set(state["seen"])
    for b in names:
        rel = b[len(p["requests"]):]
        good = re.fullmatch(_RID.pattern + r"\.json", rel)
        rid = rel[:-len(".json")] if good else "refused-" + hashlib.sha256(rel.encode()).hexdigest()[:16]
        if rid in seen:
            continue
        state["seen"].append(rid)            # a bad request is recorded once, never retried every loop
        try:
            if not good:
                raise ValueError("a request name that is not <generated id>.json: not read")
            req = blob.get_json(b) or {}
            kind, pl = req.get("kind"), req.get("payload") or {}
            if kind == "merge":
                queued = {i["pr"] for i in state["merge"]}
                state["merge"] += [{"pr": int(n), "state": "queued", "request": rid} for n in pl.get("prs", [])
                                   if int(n) not in queued]
            elif kind == "collect":
                ent = valid_entry(pl)
                w = valid_watch(pl["watch"], ent) if pl.get("watch") else None     # all checked first
                cur = state["watch"].get(pl["name"])
                same = cur and w and (cur["launch_id"], cur["invocation"]) == (w["launch_id"], w["invocation"])
                if w:                              # a launch counts under one tag: its history moves with it
                    for t, hs in list(state["tags"].items()):
                        if t != w["tag"] and _key(pl["name"], w) in hs:
                            state["tags"].setdefault(w["tag"], {})[_key(pl["name"], w)] = hs.pop(_key(pl["name"], w))
                if cur and not same:               # no longer watched: its last reading stays in the tag's history
                    state["tags"].setdefault(cur["tag"], {}).setdefault(_key(pl["name"], cur), {})["final"] = True
                state["collect"].setdefault(pl["name"], {"labels": {}})["entry"] = pl
                ids = {str(r["id"]): r["name"] for r in (w or {}).get("clusters", {}).values()}
                if same:                           # the same launch: its ids (a keeper started later), rules, tag
                    cur.update(clusters=w["clusters"], stop=w["stop"], tag=w["tag"],
                               seen_ids={**cur.get("seen_ids", {}), **ids})
                    cur.pop("health_at", None)
                elif w:
                    state["watch"][pl["name"]] = {**w, "state": "watching", "labels": {}, "seen_ids": ids}
                    _hist(state, pl["name"], w)["final"] = False
                else:
                    state["watch"].pop(pl["name"], None)
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
    if cloud is not None:
        try:
            watch_step(blob, name, state, now, cloud)
        except Exception as e:  # noqa: BLE001
            err("watch", e)
    state["loops"] = int(state.get("loops", 0)) + 1
    why_w = "no cloud access in this loop" if cloud is None else cloud.disabled() or ""
    tags = {t: sum(float(h.get("usd") or 0) for h in hs.values()) for t, hs in state["tags"].items()}
    paused = []
    for d in run_dirs(work):
        try:
            st = _red((json.load(open(os.path.join(d, "metadata.json"))) or {}).get("status", ""), 120)
            paused += [{"run": os.path.relpath(d, work), "status": st}] if st.startswith(("paused", "awaiting")) else []
        except (OSError, ValueError, AttributeError):
            pass
    status = {"name": name, "host": host, "heartbeat_utc": utc(now), "loops": state["loops"],
              "merge": {"enabled": gh is not None, "why": "" if gh is not None else merge_why,
                        "queue": state["merge"], "done": state["merge_done"][-10:]},
              "collect": {k: {"labels": v["labels"]} for k, v in state["collect"].items()},
              "watch": {"enabled": not why_w, "why": why_w, "tags_usd": tags,
                        "runs": {k: {x: v for x, v in w.items() if x not in _HIDDEN} for k, w in state["watch"].items()}},
              "metrics_at": state.get("metrics_at"), "paused": paused,
              "acks": state["acks"][-10:], "errors": state["errors"][-10:]}
    blob.put_json(p["state"], state)
    blob.put_json(p["status"], status)
    return state


def loop(a):
    blob, p, work = Blob(a.account), paths(a.name), os.path.abspath(a.work)
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
    host, deadline, cloud = f"{os.uname().nodename}:{os.getpid()}", time.time() + a.max_hours * 3600, Cloud()
    while time.time() < deadline:
        try:
            state = tick(blob, a.name, state, time.time(), work, gh=gh, host=host, merge_why=why, cloud=cloud)
        except Exception as e:  # noqa: BLE001 -- the loop outlives one bad pass (storage blip, DNS); state kept
            print(_red(f"tick failed: {type(e).__name__}: {e}"), flush=True)
        held = cloud.disabled() is not None
        time.sleep(min(sleep_for(state, time.time(), a.every, watching=not held), max(0.0, deadline - time.time())))
    return 0


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
    wa = st.get("watch") or {}
    if wa.get("runs"):
        out.append(f"run monitor (reporting only): {'enabled' if wa.get('enabled') else 'held (' + str(wa.get('why')) + ')'}")
        out.append(f"  {'LAUNCH':<26} {'RUN':<5} {'STATE':<10} {'PHASE':<12} {'HB AGE':>7} {'LAST PROGRESS':<21} "
                   f"{'ETA':<21} FAILURE")
    for lname, w in sorted((wa.get("runs") or {}).items()):
        for label, lab in sorted((w.get("labels") or {}).items()):
            hb = "" if lab.get("heartbeat_age_s") is None else f"{lab['heartbeat_age_s']}s"
            out.append(f"  {lname:<26} {label:<5} {str(lab.get('state')):<10} {str(lab.get('phase') or ''):<12} {hb:>7} "
                       f"{str(lab.get('last_progress_utc') or ''):<21} {str(lab.get('eta_utc') or ''):<21} "
                       f"{lab.get('failure') or ''}{'' if lab.get('complete', True) else ' (inputs not all read)'}")
    for lname, w in sorted((wa.get("runs") or {}).items()):
        usd = "no reading" if w.get("usd") is None else f"${w['usd']:.2f} (${w['usd_per_run']:.2f}/run)"
        out.append(f"  {lname} {w.get('launch_id')} tag {w.get('tag')}: {w.get('state')}, {usd}"
                   + (f"; {w['note']}" if w.get("note") else ""))
        if w.get("rules"):
            out.append(f"    {'RULE':<18} {'WOULD_TRIP':<10} REASON")
        for k, v in sorted((w.get("rules") or {}).items()):
            wt = {True: "yes", False: "no"}.get(v.get("would_trip"), "unknown")
            out.append(f"    {k:<18} {wt:<10} {v.get('reason')} ({v.get('at')})")
    if wa.get("tags_usd"):
        out.append("  cloud USD by tag: " + ", ".join(f"{t} ${u:.2f}" for t, u in sorted(wa["tags_usd"].items())))
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
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True,
                       text=True)                      # the owner's one-time step stores it there
    tok = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
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


def _assignments(config_path, stop=None, tag=None):
    import run_cluster as rc
    cfg = rc.load_config(config_path)
    if not os.path.exists(cfg.state_file):
        raise SystemExit(f"no {cfg.state_file}: this machine did not launch {cfg.name}")
    assign = json.load(open(cfg.state_file))
    w = watch_payload(cfg, assign, stop or {}, tag)
    if w is None:
        print(f"{cfg.name}: clusters.json is not this launch's: collection only, no run monitor")
    return {"name": cfg.name, "storage_prefix": cfg.storage_prefix, "storage_account": cfg.storage_account,
            "assignments": assign, **({"watch": w} if w else {})}


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
    """The monitor's cloud reads, recorded (no Coiled call). `probes`: what each probe returns, per launch name or in
    order; an exception raises it; a float sleeps that long first."""
    def __init__(self, why=None, probes=({"clusters": {}, "usd": 0.0, "labels": {}},)):
        self.why, self.probes, self.calls = why, list(probes), []
    def disabled(self):
        return self.why
    def probe(self, w, entry):
        self.calls.append(("probe", entry.get("name"), w["launch_id"]))
        r = self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]
        if isinstance(r, dict) and entry.get("name") in r and "clusters" not in r:
            r = r[entry["name"]]
        if isinstance(r, tuple):
            time.sleep(r[0])
            r = r[1]
        if isinstance(r, BaseException):
            raise r
        return json.loads(json.dumps(r))


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
    b.d[P["requests"] + "20000101T000000-aaaaaaaa.json"] = b"{not json"
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
        b.put_json(f"{P['requests']}20270101T{i:06d}-0000abcd.json", {"kind": "noop"})
    st = run(b, [T0, T0 + 1])
    ok(len(st.get("seen", [])) == 1003 and st["acks"][-1]["at"] == utc(T0), "SV24 no processed request is replayed")

    # ---- the run monitor, reporting only: health, spend and each stop rule's evaluation (SV25-SV55) ----
    import types
    from unittest import mock

    def refused(fn, *a, **k):
        try:
            return fn(*a, **k)
        except SystemExit as e:
            return f"SystemExit: {e}"
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"
    LN, LN2 = "ev-flu-t1-gcc", "ev-tb-t1-gcc"
    ASG = {"R1": {"launch_id": "L1", "worker": "w1", "invocation": "inv-1", "cluster_id": 101},
           "R2": {"launch_id": "L1", "worker": "w2", "invocation": "inv-1", "cluster_id": 101}}
    W = {"launch_id": "L1", "invocation": "inv-1", "tag": "t1", "cap_usd": 70, "run_timeout_hours": 24, "max_rounds": 3,
         "launched_utc": utc(T0 - 60), "stop": {},
         "clusters": {"run": {"name": LN, "id": 101}, "keeper": {"name": LN + "-keeper", "id": 102}}}
    CENT = {"name": LN, "storage_prefix": "runs/evals/t1/flu_vaccine", "storage_account": "acct", "assignments": ASG}
    NOFETCH = lambda *x: (_ for _ in ()).throw(FileNotFoundError("not stored yet"))  # noqa: E731
    lab = lambda st_="running", rnd=2, prog=(1,), complete=True, **k: {  # noqa: E731
        "state": st_, "phase": "ANALYZE", "heartbeat_age_s": 30, "round": rnd, "progress": list(prog), "exit_code": None,
        "complete": complete, **k}
    probe = lambda usd, **labs: {"clusters": {"run": "running", "keeper": "running"}, "usd": usd,  # noqa: E731
                                 "labels": labs or {"R1": lab(), "R2": lab()}}

    def watched(cl, stop=None, times=(T0, T0 + 1), b=None, extra=()):
        b = b or FakeBlob()
        guard(enqueue, b, N, "collect", {**CENT, "watch": {**W, "stop": stop or {}}}, T0 - 2)
        for ent in extra:
            guard(enqueue, b, N, "collect", ent, T0 - 1)
        st = {}
        with tempfile.TemporaryDirectory() as td:
            for t in times:
                st = guard(tick, b, N, st, t, td, cloud=cl, fetch=NOFETCH, metrics=metrics)
        return b, st if isinstance(st, dict) else {"raised": st}

    def wv(st, name=LN):
        return (st.get("watch") or {}).get(name, {})

    def rule(st, k, name=LN):
        return (wv(st, name).get("rules") or {}).get(k, {})

    # SV25 a watch names the launch's own recorded ids and agrees with every assignment (launch id, invocation, run id)
    good = guard(valid_watch, {**W, "stop": {"tag_usd": 50, "same_failure_runs": 2}}, CENT)
    bads = [({**W, "clusters": {"keeper": W["clusters"]["keeper"]}}, CENT),
            ({**W, "clusters": {"run": {"name": "ev-x-gcc", "id": 101}}}, CENT),
            ({**W, "clusters": {"run": {"name": LN, "id": True}}}, CENT), ({**W, "launch_id": "L0"}, CENT),
            ({**W, "stop": {"usd": 5}}, CENT), ({**W, "stop": {"same_failure_runs": 2.5}}, CENT),
            ({**W, "tag": "Bad_Tag"}, CENT), ({**W, "invocation": "inv-2"}, CENT),
            ({**W, "clusters": {"run": {"name": LN, "id": 202}}}, CENT),
            ({**W, "launch_id": "a/b"}, {**CENT, "assignments": {k: {**v, "launch_id": "a/b"} for k, v in ASG.items()}})]
    late = guard(valid_watch, {**W, "launched_utc": "2026-02-31T00:00:00Z"}, CENT)
    ok(isinstance(good, dict) and good["stop"] == {"tag_usd": 50.0, "same_failure_runs": 2}
       and all("ValueError" in str(guard(valid_watch, x, e_)) for x, e_ in bads)
       and isinstance(late, dict) and late["launched_at"] is None,
       f"SV25 watches agree with their assignments; a bad launch time is no launch time, not an error ({good})")
    # SV26 the laptop binds only this launch's ids; a legacy record without an invocation is collection only; the
    # default tag is the eval tag, or the whole config name
    with tempfile.TemporaryDirectory() as td:
        cfg_ = types.SimpleNamespace(name=LN, clusters_file=os.path.join(td, "clusters.json"), cap_usd=70,
                                     storage_prefix="runs/evals/t1/flu_vaccine", run_timeout_hours=24, max_rounds=3)
        rec_ = {"invocation": "inv-1", "launch_id": "L1", "launched_utc": utc(T0), "status": "launched",
                "run": {"name": LN, "id": 101}, "keeper": {"name": LN + "-keeper", "id": 102},
                "overflow": {"name": "someone-else", "id": 7}}
        json.dump(rec_, open(cfg_.clusters_file, "w"))
        w1 = guard(watch_payload, cfg_, ASG, {"tag_usd": 40.0}, None)
        json.dump({**rec_, "invocation": "inv-0"}, open(cfg_.clusters_file, "w"))
        w2 = guard(watch_payload, cfg_, ASG, {}, None)
        w3 = refused(watch_payload, cfg_, ASG, {"tag_usd": 40.0}, None)
        legacy = {k: {x: y for x, y in v.items() if x != "invocation"} for k, v in ASG.items()}
        json.dump({x: y for x, y in rec_.items() if x != "invocation"}, open(cfg_.clusters_file, "w"))
        w4 = guard(watch_payload, cfg_, legacy, {}, None)
        long_ = "v2-nigeria-gf13-amendment-eight-gcc"
        cfg2 = types.SimpleNamespace(**{**vars(cfg_), "name": long_, "storage_prefix": "runs/x"})
        json.dump({**rec_, "run": {"name": long_, "id": 101}}, open(cfg_.clusters_file, "w"))
        w5 = guard(watch_payload, cfg2, ASG, {}, None)
    ok(isinstance(w1, dict) and w1.get("clusters") == W["clusters"] and w1.get("tag") == "t1" and w2 is None
       and str(w3).startswith("SystemExit") and w4 is None and isinstance(w5, dict) and w5.get("tag") == long_,
       f"SV26 only this launch's ids; legacy records collect only; the default tag is never cut ({w4}; {w5})")
    # SV27 a launch is its launch id and invocation: a re-enqueue refreshes its ids, rules and tag; another
    # invocation with the same launch id is another launch; tag history is kept per launch, not per name
    cl = FakeCloud(probes=[probe(8.0)])
    b, st = watched(cl)
    with tempfile.TemporaryDirectory() as td:
        cl2 = {**W["clusters"], "overflow": {"name": LN + "-overflow", "id": 103}}
        guard(enqueue, b, N, "collect", {**CENT, "watch": {**W, "clusters": cl2, "stop": {"run_usd": 99}, "tag": "t9"}}, T0 + 2)
        st = guard(tick, b, N, st, T0 + 3, td, cloud=cl, fetch=NOFETCH, metrics=metrics)
        v2 = dict(wv(st))
        asg3 = {k: {**v, "invocation": "inv-3", "cluster_id": 303} for k, v in ASG.items()}
        w3_ = {**W, "invocation": "inv-3", "clusters": {"run": {"name": LN, "id": 303}}}
        cl.probes = [probe(3.0)]
        guard(enqueue, b, N, "collect", {**CENT, "assignments": asg3, "watch": w3_}, T0 + 4)
        st = guard(tick, b, N, st, T0 + 5, td, cloud=cl, fetch=NOFETCH, metrics=metrics)
        v3 = dict(wv(st))
    hist = (st.get("tags") or {})
    ok(v2.get("clusters", {}).get("overflow", {}).get("id") == 103 and v2.get("stop") == {"run_usd": 99.0}
       and v2.get("tag") == "t9" and v3.get("invocation") == "inv-3" and v3.get("clusters", {}).get("run", {}).get("id") == 303
       and sum(h.get("usd") or 0 for h in hist.get("t1", {}).values()) == 3.0
       and sum(h.get("usd") or 0 for h in hist.get("t9", {}).values()) == 8.0,
       f"SV27 a launch is its launch id and invocation; ids and tag refresh; history per launch ({hist})")
    # SV28 health and unrounded spend in status.json
    cl = FakeCloud(probes=[probe(4.0), probe(9.996)])
    b, st = watched(cl, times=(T0, T0 + HEALTH_EVERY_S))
    sw = ((b.get_json(P["status"]) or {}).get("watch") or {}).get("runs", {}).get(LN, {})
    r1 = sw.get("labels", {}).get("R1", {})
    ok(r1.get("state") == "running" and r1.get("phase") == "ANALYZE" and r1.get("heartbeat_age_s") == 30
       and r1.get("last_progress_utc") and r1.get("eta_utc") and sw.get("usd") == 9.996 and sw.get("usd_per_run") == 4.998,
       f"SV28 each run's health and the launch's unrounded spend in status.json ({sw.get('usd')})")
    # SV29 held without a Coiled token: nothing probed, status says why, and the loop does not poll every second
    cl = FakeCloud(why="no $DASK_COILED__TOKEN on the VM")
    b, st = watched(cl, stop={"tag_usd": 0.01})
    sw = (b.get_json(P["status"]) or {}).get("watch") or {}
    ok(not cl.calls and sw.get("enabled") is False and "COILED" in sw.get("why", "")
       and guard(sleep_for, st, T0 + 1, 60, watching=False) == 60,
       f"SV29 held: nothing probed, status says why, no 1 s polling ({sw.get('why')})")
    # SV30 each declared rule's evaluation from a fresh probe: would_trip and its reason, compared unrounded; the
    # monitor never deletes, stops or checkpoints anything
    b, st = watched(FakeCloud(probes=[probe(20.0)]), stop={"tag_usd": 15, "run_usd": 10})
    b2_, st2_ = watched(FakeCloud(probes=[probe(19.992)]), stop={"run_usd": 10})
    t_, r_ = rule(st, "tag_usd"), rule(st, "run_usd")
    ok(t_.get("would_trip") is True and "t1" in t_.get("reason", "") and r_.get("would_trip") is True
       and rule(st2_, "run_usd").get("would_trip") is False
       and "would_trip" in json.dumps((b.get_json(P["status"]) or {}).get("watch")),
       f"SV30 rules evaluated from a fresh probe, unrounded ({t_}, {rule(st2_, 'run_usd')})")
    # SV31 a failed probe, or a run whose inputs could not all be read, is `unknown`, never "no progress"
    b, st = watched(FakeCloud(probes=[RuntimeError("scheduler unreachable")]), stop={"tag_usd": 0.01, "no_progress_min": 1})
    ok(rule(st, "tag_usd").get("would_trip") == "unknown" and rule(st, "no_progress_min").get("would_trip") == "unknown"
       and "unreachable" in rule(st, "tag_usd").get("reason", ""), f"SV31 a failed probe: unknown ({wv(st).get('rules')})")
    times = [T0 + k * HEALTH_EVERY_S for k in range(8)]
    b, st = watched(FakeCloud(probes=[probe(1.0, R1=lab(rnd=1, complete=False), R2=lab(rnd=1, complete=False))]),
                    stop={"no_progress_min": 30}, times=times)
    ok(rule(st, "no_progress_min").get("would_trip") == "unknown",
       f"SV31b unreadable run inputs: unknown, not no progress ({rule(st, 'no_progress_min')})")
    # SV32 no progress for N minutes would trip only with complete inputs and no ETA
    for rnd, want in ((1, True), (2, False)):
        b, st = watched(FakeCloud(probes=[probe(1.0, R1=lab(rnd=rnd), R2=lab(rnd=rnd))]), stop={"no_progress_min": 30},
                        times=times)
        ok(rule(st, "no_progress_min").get("would_trip") is want, f"SV32 35 min without progress, round {rnd}: {want}")
    # SV33 same failure in K runs: dead runs and collected exit-1 runs count, owner pauses and eval outcomes do not
    for labs, want in (({"R1": lab("dead"), "R2": lab("dead")}, True),
                       ({"R1": lab("dead"), "R2": lab("done", exit_code=1)}, False)):
        b, st = watched(FakeCloud(probes=[probe(1.0, **labs)]), stop={"same_failure_runs": 2})
        ok(rule(st, "same_failure_runs").get("would_trip") is want, f"SV33 {sorted(x['state'] for x in labs.values())}: {want}")
    ok(failure_kind({"state": "collected", "run_status": "awaiting_scope_signoff"}) is None
       and failure_kind({"state": "collected", "run_status": "paused:awaiting_budget"}) is None
       and failure_kind({"state": "collected", "run_status": "completed_gate_not_accepted"}) is None
       and failure_kind({"state": "collected", "run_status": None, "exit_code": 1}) == "exit_1"
       and failure_kind({"state": "collected", "run_status": "budget_exhausted"}) == "budget_exhausted",
       "SV33b owner pauses and eval outcomes are no failure; a collected exit-1 run is one")
    # SV34 each launch on its own: one launch's probe raising SystemExit (a cluster stopping during connect) or an
    # error leaves the other launch's probe and report intact, and the loop goes on
    ent2 = {"name": LN2, "storage_prefix": "runs/evals/t1/tb_acf", "storage_account": "acct", "assignments": ASG,
            "watch": {**W, "clusters": {"run": {"name": LN2, "id": 101}}}}
    cl = FakeCloud(probes=[{LN: SystemExit("cluster 101 is stopped: refusing"), LN2: probe(2.0)}])
    b, st = watched(cl, stop={"tag_usd": 100}, extra=(ent2,))
    ok("raised" not in st and "stopped: refusing" in wv(st).get("note", "") and wv(st, LN2).get("usd") == 2.0
       and rule(st, "tag_usd").get("would_trip") == "unknown",
       f"SV34 one launch's SystemExit does not stop the others ({wv(st).get('note')}, {wv(st, LN2).get('usd')})")
    # SV35 a probe has a deadline: one that does not answer is reported, and the pass goes on
    with mock.patch.dict(globals(), {"PROBE_TIMEOUT_S": 0.2}):
        b, st = watched(FakeCloud(probes=[(1.0, probe(1.0))]), stop={"tag_usd": 0.5}, times=(T0,))
    ok("did not return" in wv(st).get("note", "") and rule(st, "tag_usd").get("would_trip") == "unknown",
       f"SV35 a probe past its deadline is unknown ({wv(st).get('note')})")
    _INFLIGHT.clear()                  # that probe's thread is still sleeping: the cases below are other passes
    # SV36 a failed status.json write loses nothing: the next pass neither re-ingests the request nor probes again early

    class FlakyBlob(FakeBlob):
        def __init__(self):
            super().__init__()
            self.fail = 1
        def put_json(self, blob, obj):
            if blob == P["status"] and self.fail:
                self.fail -= 1
                raise OSError("status upload failed")
            super().put_json(blob, obj)
    b, cl, st = FlakyBlob(), FakeCloud(probes=[probe(20.0)]), {}
    guard(enqueue, b, N, "collect", {**CENT, "watch": {**W, "stop": {"tag_usd": 15}}}, T0 - 1)
    with tempfile.TemporaryDirectory() as td:
        r1_ = guard(tick, b, N, st, T0, td, cloud=cl, fetch=NOFETCH, metrics=metrics)
        r2_ = guard(tick, b, N, st, T0 + 1, td, cloud=cl, fetch=NOFETCH, metrics=metrics)
    ok(isinstance(r1_, str) and isinstance(r2_, dict) and len(cl.calls) == 1 and len(r2_.get("acks", [])) == 1
       and rule(r2_, "tag_usd").get("would_trip") is True, f"SV36 a failed status write loses nothing ({r1_}, {cl.calls})")
    # SV37 a request is read only when its whole file name is <generated id>.json; anything else is refused once
    b = FakeBlob()
    b.d[P["requests"] + "../../evil.json"] = json.dumps({"kind": "noop"}).encode()
    b.d[P["requests"] + "20260101T000000-0000abcd.data"] = json.dumps({"kind": "noop"}).encode()
    st = run(b, [T0 + 1])
    b.d[P["requests"] + "20260101T000000-0000abcd.json"] = json.dumps({"kind": "noop"}).encode()
    st = run(b, [T0 + 2], state=st)
    ok([x["request"] for x in st.get("acks", [])] == ["20260101T000000-0000abcd"] and len(st.get("errors", [])) == 2,
       f"SV37 only <id>.json is a request; others refused once ({st.get('errors')})")
    # SV38 redaction: Coiled's ApiToken header, the VM's own secret values and a collected run's status text
    leak, tokv = "dummy-credential-" + "Q" * 12, "tok" + "Z" * 30
    with mock.patch.dict(os.environ, {"DASK_COILED__TOKEN": tokv}):
        red_ = _red(f"Authorization: ApiToken {leak} and {tokv}")
        b, st = watched(FakeCloud(probes=[RuntimeError(f"401 Authorization: ApiToken {leak}; token {tokv}")]),
                        stop={"tag_usd": 0.01})
    blobs_ = json.dumps({k: v.decode(errors="replace") for k, v in b.d.items()})
    with tempfile.TemporaryDirectory() as td:
        rd_ = os.path.join(td, "q", "runs", "r1")
        os.makedirs(rd_)
        json.dump({"status": f"run_failed Authorization: ApiToken {leak}"}, open(os.path.join(rd_, "metadata.json"), "w"))
        rs_ = guard(_run_status, os.path.join(td, "q"))
    ok(leak not in red_ + blobs_ + str(rs_) and tokv not in red_ + blobs_ and "run_failed" in str(rs_),
       f"SV38 ApiToken headers, the VM's secret values and run status text are redacted ({red_})")
    # SV39 the sleep stops at the next probe or collection retry; while held, watches add nothing
    T = T0 + 10_000
    s1 = {"watch": {LN: {"state": "watching", "health_at": T - HEALTH_EVERY_S + 10}}}
    s3 = {"collect": {LN: {"entry": {"assignments": {"R1": {}, "R2": {}}},
                           "labels": {"R1": {"state": "waiting", "tried": T - COLLECT_EVERY_S + 20},
                                      "R2": {"state": "collected", "tried": T - 1000},
                                      "R9": {"state": "waiting", "tried": T - 5000}}}}}
    ok(guard(sleep_for, {}, T, 600) == 600 and guard(sleep_for, s1, T, 600) == 10 and guard(sleep_for, s3, T, 600) == 20
       and guard(sleep_for, s1, T, 5) == 5 and guard(sleep_for, s1, T, 600, watching=False) == 600,
       "SV39 the sleep stops at the next due check (a label no longer assigned adds nothing), never past --every")
    # SV41 `status` prints the watch as tables: runs, then each rule's would_trip and reason
    b, st = watched(FakeCloud(probes=[probe(20.0)]), stop={"tag_usd": 15, "no_progress_min": 600})
    lines = "\n".join(guard(status_lines, b.get_json(P["status"]) or {}, T0 + 30)[0])
    ok("RUN" in lines and "PHASE" in lines and "WOULD_TRIP" in lines and "tag_usd" in lines and " yes " in lines
       and "R1" in lines and "ANALYZE" in lines and "$20.00" in lines,
       f"SV41 status prints runs and rule evaluations as tables ({lines})")
    # ---- Codex on the reporting-only version (SV43-SV55) ----
    # SV43 probe text is redacted and a run's progress is kept as a digest only
    b, st = watched(FakeCloud(probes=[probe(1.0, R1=lab(phase=f"x ApiToken {leak}", prog=(1, "ts", leak)), R2=lab())]))
    blobs_ = json.dumps({k: v.decode(errors="replace") for k, v in b.d.items()})
    ok(leak not in blobs_ and isinstance(wv(st)["labels"]["R1"].get("progress"), str), "SV43 probe text redacted, progress a digest")
    # SV44 two launches of one tag, probed in one pass, report the same tag decision
    ent2 = {"name": LN2, "storage_prefix": "runs/evals/t1/tb_acf", "storage_account": "acct", "assignments": ASG,
            "watch": {**W, "clusters": {"run": {"name": LN2, "id": 101}}, "stop": {"tag_usd": 10}}}
    b, st = watched(FakeCloud(probes=[probe(6.0)]), stop={"tag_usd": 10}, extra=(ent2,))
    ok(rule(st, "tag_usd").get("would_trip") is True and rule(st, "tag_usd", LN2).get("would_trip") is True,
       f"SV44 launches of one tag agree ({rule(st, 'tag_usd')}, {rule(st, 'tag_usd', LN2)})")
    # SV45 an incomplete probe gives unknown for the run-outcome rules, even next to a stalled run with full inputs
    labs_ = {"R1": lab(rnd=1), "R2": lab(rnd=1, complete=False)}
    b, st = watched(FakeCloud(probes=[probe(1.0, **labs_)]), stop={"same_failure_runs": 1, "no_progress_min": 30},
                    times=[T0 + k * HEALTH_EVERY_S for k in range(8)])
    ok(rule(st, "same_failure_runs").get("would_trip") == "unknown" and rule(st, "no_progress_min").get("would_trip") == "unknown",
       f"SV45 incomplete inputs: unknown ({wv(st).get('rules')})")
    ok(label_complete({"state": "running", "log_bytes": 5}, {}) is False
       and label_complete({"state": "running", "phase": "ANALYZE", "heartbeat_age_s": 3}, {}) is True
       and label_complete({"state": "running", "phase": "A", "heartbeat_age_s": 3}, {"input_errors": ["x"]}) is False,
       "SV45b a live run without a phase or heartbeat, or with unreadable files, is not complete")
    # SV46 an ended launch is evaluated on its final reading and late collections; a held monitor reports unknown
    b, st = watched(FakeCloud(probes=[{**probe(1.0), "clusters": {"run": "stopped", "keeper": "stopped"}}]),
                    stop={"same_failure_runs": 1})
    st["collect"][LN]["labels"]["R1"] = {"state": "collected", "launch_id": "L1", "invocation": "inv-1", "exit_code": 1,
                                         "run_status": None}
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, st, T0 + 2, td, cloud=FakeCloud(), fetch=NOFETCH, metrics=metrics)
        held_ = guard(tick, b, N, {**st, "watch": {LN: {**wv(st), "state": "watching"}}}, T0 + 3, td,
                      cloud=FakeCloud(why="held"), fetch=NOFETCH, metrics=metrics)
    ok(wv(st).get("state") == "ended" and rule(st, "same_failure_runs").get("would_trip") is True
       and rule(held_, "same_failure_runs").get("would_trip") == "unknown",
       f"SV46 ended: re-evaluated on late results; held: unknown ({rule(st, 'same_failure_runs')})")
    # SV47 an error while applying a probe leaves no earlier decision standing
    b, st = watched(FakeCloud(probes=[probe(20.0), {"clusters": {}, "usd": 1.0, "labels": "not a mapping"}]),
                    stop={"tag_usd": 15}, times=(T0, T0 + HEALTH_EVERY_S))
    ok(rule(st, "tag_usd").get("would_trip") == "unknown", f"SV47 a probe that cannot be applied: unknown ({rule(st, 'tag_usd')})")
    # SV48 another invocation never inherits a collected result; an assignment without its identity binds nothing
    asgB = {k: {**v, "invocation": "inv-B"} for k, v in ASG.items()}
    b = FakeBlob()
    guard(enqueue, b, N, "collect", {**CENT, "assignments": asgB, "watch": {**W, "invocation": "inv-B", "stop": {"same_failure_runs": 1}}}, T0 - 2)
    st = {"collect": {LN: {"labels": {"R1": {"state": "collected", "launch_id": "L1", "invocation": "inv-1",
                                             "exit_code": 1, "run_status": None}}}}}
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, st, T0, td, cloud=FakeCloud(probes=[probe(1.0)]), fetch=NOFETCH, metrics=metrics)
    noid = {k: {x: y for x, y in v.items() if x != "cluster_id"} for k, v in ASG.items()}
    ok(wv(st)["labels"]["R1"].get("failure") is None and "ValueError" in str(guard(valid_watch, W, {**CENT, "assignments": noid})),
       f"SV48 a collected result counts only for its own invocation; missing identity is refused ({wv(st)['labels']['R1']})")
    # SV49 spend: an id listed under another name makes it unread; a replaced helper's cost stays counted
    ok(spend_of({"run": "running", "keeper": "listed under another name"}, {"101": 2.0}) is None
       and spend_of({"run": "running"}, {"101": 2.0, "102": 9.0, "104": 1.0}) == 12.0 and spend_of({}, {"1": None}) is None,
       "SV49 spend is the sum over every id ever recorded, unread when one is not this launch's")
    b, st = watched(FakeCloud(probes=[probe(1.0)]))
    with tempfile.TemporaryDirectory() as td:
        guard(enqueue, b, N, "collect", {**CENT, "watch": {**W, "clusters": {**W["clusters"], "keeper": {"name": LN + "-keeper", "id": 104}}}}, T0 + 2)
        st = guard(tick, b, N, st, T0 + 3, td, cloud=FakeCloud(probes=[probe(1.0)]), fetch=NOFETCH, metrics=metrics)
    ok(sorted(wv(st).get("seen_ids", {})) == ["101", "102", "104"], f"SV49b every id ever recorded is kept ({wv(st).get('seen_ids')})")
    # SV50 two launches of one name queued for one pass both keep a history entry; a re-tag never counts one twice
    asgC = {k: {**v, "invocation": "inv-C", "cluster_id": 303} for k, v in ASG.items()}
    b, st = watched(FakeCloud(probes=[probe(2.0)]), extra=({**CENT, "assignments": asgC, "watch": {**W, "invocation": "inv-C",
                                                            "clusters": {"run": {"name": LN, "id": 303}}}},))
    with tempfile.TemporaryDirectory() as td:
        guard(enqueue, b, N, "collect", {**CENT, "watch": {**W, "tag": "t2"}}, T0 + 2)
        st = guard(tick, b, N, st, T0 + 3, td, cloud=FakeCloud(probes=[probe(2.0)]), fetch=NOFETCH, metrics=metrics)
    keys_ = [(t, k) for t, hs in st.get("tags", {}).items() for k in hs]
    ok(sorted(k for _, k in keys_) == sorted({k for _, k in keys_}) and (("t2", f"{LN}@L1@inv-1") in keys_)
       and any(k.endswith("inv-C") for _, k in keys_), f"SV50 history per launch, each under one tag ({keys_})")
    # SV51 completed_no_report is an eval outcome; a run collected before run_status existed is unknown
    ok(failure_kind({"state": "collected", "run_status": "completed_no_report", "exit_code": 2}) is None,
       "SV51 completed_no_report is no failure")
    b, st = watched(FakeCloud(probes=[{**probe(1.0), "clusters": {"run": "stopped", "keeper": "stopped"}}]),
                    stop={"same_failure_runs": 1})
    st["collect"][LN]["labels"]["R1"] = {"state": "collected", "launch_id": "L1", "invocation": "inv-1", "exit_code": 2}
    with tempfile.TemporaryDirectory() as td:
        st = guard(tick, b, N, st, T0 + 2, td, cloud=FakeCloud(), fetch=NOFETCH, metrics=metrics)
    ok(rule(st, "same_failure_runs").get("would_trip") == "unknown", f"SV51b an old record's outcome is unknown ({rule(st, 'same_failure_runs')})")
    # SV52 a refused name never shadows a later request of the same id, and is not stored verbatim
    b = FakeBlob()
    b.d[P["requests"] + "20260101T000000-0000abcd"] = json.dumps({"kind": "noop"}).encode()
    st = run(b, [T0 + 1])
    b.d[P["requests"] + "20260101T000000-0000abcd.json"] = json.dumps({"kind": "noop"}).encode()
    st = run(b, [T0 + 2], state=st)
    ok([x["request"] for x in st.get("acks", [])] == ["20260101T000000-0000abcd"]
       and "20260101T000000-0000abcd" not in json.dumps(st.get("seen", [])[:1]), f"SV52 a refused name shadows nothing ({st.get('seen')})")
    # SV53 probes run at once under one deadline: slow launches never delay a healthy one past it
    ent3 = {"name": "ev-ok-t1-gcc", "storage_prefix": "runs/evals/t1/ok", "storage_account": "acct", "assignments": ASG,
            "watch": {**W, "clusters": {"run": {"name": "ev-ok-t1-gcc", "id": 101}}}}
    slow = (2.0, probe(1.0))
    t_ = time.monotonic()
    with mock.patch.dict(globals(), {"PROBE_TIMEOUT_S": 0.5}):
        b, st = watched(FakeCloud(probes=[{LN: slow, LN2: slow, "ev-ok-t1-gcc": probe(3.0)}]), times=(T0,),
                        extra=(ent2, ent3))
    took = time.monotonic() - t_
    _INFLIGHT.clear()
    ok(wv(st, "ev-ok-t1-gcc").get("usd") == 3.0 and "did not return" in wv(st).get("note", "") and took < 1.5,
       f"SV53 slow probes run alongside, under one deadline ({took:.2f} s)")
    # SV54 no launch time: no ETA is made up, so a stalled round-two run is reported
    b = FakeBlob()
    guard(enqueue, b, N, "collect", {**CENT, "watch": {**{k: v for k, v in W.items() if k != "launched_utc"},
                                                       "stop": {"no_progress_min": 30}}}, T0 - 2)
    st = {}
    with tempfile.TemporaryDirectory() as td:
        for t in [T0 + k * HEALTH_EVERY_S for k in range(8)]:
            st = guard(tick, b, N, st, t, td, cloud=FakeCloud(probes=[probe(1.0)]), fetch=NOFETCH, metrics=metrics)
    ok(rule(st, "no_progress_min").get("would_trip") is True, f"SV54 no launch time, no ETA ({rule(st, 'no_progress_min')})")
    # SV55 rules must be finite; an unreadable clusters.json still collects
    ok("ValueError" in str(guard(_stop_rules, {"run_usd": float("inf")})) and "ValueError" in str(guard(_stop_rules, {"tag_usd": float("nan")})),
       "SV55 an infinite or NaN rule is refused")
    with tempfile.TemporaryDirectory() as td:
        cfg_ = types.SimpleNamespace(name=LN, clusters_file=os.path.join(td, "clusters.json"), cap_usd=70,
                                     storage_prefix="runs/evals/t1/flu_vaccine", run_timeout_hours=24, max_rounds=3)
        open(cfg_.clusters_file, "w").write('{"invocation": "inv-1", "laun')
        ok(guard(watch_payload, cfg_, ASG, {}, None) is None, "SV55b a truncated clusters.json: collection only")

    # SV42 reporting only: the monitor has no path that deletes, stops or checkpoints a cluster
    import inspect
    src_ = inspect.getsource(Cloud) + inspect.getsource(watch_step) + inspect.getsource(evaluate)
    ok(not any(x in src_ for x in ("delete_cluster", "checkpoint_now", "shutdown(", "_stop_by_id", "def stop"))
       and "stops" not in paths(N), "SV42 the monitor reads only: no delete, stop or checkpoint path")

    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


def _cli_stop(a) -> dict:
    return {k: v for k, v in (("tag_usd", a.stop_tag_usd), ("run_usd", a.stop_run_usd),
                              ("no_progress_min", a.stop_no_progress_min),
                              ("same_failure_runs", a.stop_same_failure)) if v is not None}


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
            p.add_argument("--max-hours", type=float, default=168.0)
    ps["start"].add_argument("--cap-usd", type=float, default=15.0)
    ps["start"].add_argument("--budget-scope", default=None)
    ps["loop"].add_argument("--work", default="supervisor_work")
    ps["enqueue-merge"].add_argument("prs", nargs="+", type=int)
    ps["enqueue-collect"].add_argument("configs", nargs="+")
    pc = ps["enqueue-collect"]
    pc.add_argument("--tag", default=None, help="the tag the USD rule sums over (default: runs/evals/<tag>/ or the name)")
    pc.add_argument("--stop-tag-usd", type=float, default=None, help="stop when the tag's cloud USD reaches this")
    pc.add_argument("--stop-run-usd", type=float, default=None, help="stop when the launch's USD per run reaches this")
    pc.add_argument("--stop-no-progress-min", type=float, default=None,
                    help="stop when a run shows no progress for this many minutes and has no ETA")
    pc.add_argument("--stop-same-failure", type=int, default=None,
                    help="stop when this many runs of the tag end with the same failure kind")
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
            "enqueue-collect": lambda: [("collect", _assignments(c, _cli_stop(a), a.tag)) for c in a.configs]}[a.cmd]()
    for kind, payload in reqs:
        print(enqueue(blob, a.name, kind, payload))
    return 0

if __name__ == "__main__":
    sys.exit(main())
