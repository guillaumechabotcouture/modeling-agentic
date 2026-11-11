"""Launch one config with `cloud/run_cluster.py` and watch it to the end (F6 Stage 2).

    python cloud/launch_watch.py <config.py> --cap-usd USD [--interval S] [--start-minutes M]
                                 [--max-hours H] [--allow-no-cloud-limits] [--allow-no-checkpoints]
                                 [--cap-collect-minutes M] [--launch-minutes M] [--relaunch-attempts N]
                                 [--collect-minutes M] [--collect-attempts N] [--checkpoint-minutes M]
    python cloud/launch_watch.py --self-test

Replaces the local, per-launch watchdog scripts (F5, 2026-11-04 to 11-09). Every name (cluster, keeper, overflow
pool, labels, out_dir) comes from the config. It runs `run_cluster.py <config> launch` itself, with
MA_LAUNCH_INVOCATION set to a fresh token, then:

  0. Before the launch (owner, 2026-11-11: these rules are code, not judgment). A v2 config needs
     0 < checkpoint_minutes <= 30, else exit 9; --allow-no-checkpoints lifts that only when run_timeout_hours <= 0.5
     (a smoke). Then cloud/budget.py's launch_budget_ok(the config's budget_scope, the config's cap_usd if set else
     --cap-usd, name=the config's name) must admit the launch, and its job_meter(scope) is the spend estimator for
     the cap below; a refusal, a missing module, the stub's NotImplementedError or any error: exit 10, nothing
     launched.
  1. The launcher's exit code (run_cluster.py module doc). 0, 3, 4, 5: something was submitted, so nothing is
     stopped; it watches. On 5, or 3 with no keeper in clusters.json, it logs that the keeper is not confirmed and
     must be started by hand; it never runs `keeper` itself (its reconcile stops live helpers absent from this
     launch's record, which may be another invocation's).
     1 or any other code: it stops clusters only when clusters.json's `invocation` is this token and its `status` is
     "failed", and then exactly the ids it names, by id. Any other record (none, an earlier launch's, another
     status) stops nothing.
  2. Every --interval seconds: `status`. When every run is done, or the run cluster has stopped itself, it runs
     `collect --no-shutdown` (stopping is the watch's job, by verified id only) and confirms that every label is collected locally for this launch (`<out_dir>/<label>/result.json`
     with the launch id of this invocation's assignments; `collect` fetches from storage, or from the agent VM when
     the upload failed). Only then does it stop, by id, whatever of this launch is still live. `status` can print
     "done ... stored=None" while the agent VM holds the only copy: unconfirmed, nothing is stopped (exit 4).
  3. A spend cap in dollars (--cap-usd, required; the config's cap_usd when it is lower). Every interval, the spend
     of this launch is cloud/budget.py's estimator summed over the cluster ids clusters.json records: per cluster the
     larger of billed (total_cost credits x $0.061 + cloud_total_cost) and its conservative per-instance estimate,
     the same numbers the budget counts. A reading that fails is "unknown" and never a stop. At the cap it makes a
     final checkpoint and one collect attempt
     bounded by --cap-collect-minutes (default 10), then stops every cluster of the launch by id: the one case where
     stopping without a confirmed collect is right. The cap and --max-hours are also checked between collect retries.
     Checkpoints (F6): before the stops at the cap, and at --max-hours (where nothing is stopped), it asks every
     running run for one final checkpoint (`run_cluster.py <cfg> checkpoint-now`, bounded by --checkpoint-minutes,
     default 5; a failure or timeout never delays the stop) and its exit message names the checkpoint blobs. To
     resume from one: run_cluster.py module doc, "Checkpoints".
  4. A stray sweep: a live cluster with one of this launch's names that is not a recorded id is stopped by id (on
     its second sighting) only when the launcher's construction journal (<out_dir>/constructions.jsonl) records this
     invocation creating that id. Any other one (name and time alone) is reported and never stopped.
  5. It refuses a config without cloud-side limits (run_timeout_hours None or cluster_timeout empty) unless
     --allow-no-cloud-limits is given: a watchdog on a laptop is not the only bound on spend.
  6. Network errors (DNS, TLS, timeouts) are retried; an unreadable status, listing or spend reading is
     "unknown", never "not running", and never leads to a stop.
A label that stays "missing" (its VM never started the run) for --start-minutes gets a `relaunch <label>`
(run_cluster.py refuses it while the scheduler holds the task); a relaunch counts only when it exits 0 or 4, else it
is retried at the next window, at most --relaunch-attempts times; still missing after one accepted relaunch, or
after the last attempt: exit 2. The launch itself is bounded by --launch-minutes (default 45): past it the launcher
is terminated and reaped, and the result is treated as its exit 4 (submitted, unconfirmed: watch, stop nothing).
Before the launch it refuses (exit 8) when any live cluster of the config's names exists, or when the listing
cannot be read: `launch` replaces same-named helpers it finds live, so it never meets a foreign one through the
watch. Each collect is one attempt bounded by --collect-minutes (default 30), at most --collect-attempts (6), with
the cap and --max-hours checked between attempts.
Before an exit that leaves clusters up (1, 2, 4, 6) one last listing logs every live cluster with this launch's names
and its ownership (recorded / journal / unknown); nothing more is stopped.

Known limitation (single operator per config name). Ownership of a same-named cluster is decided by its created
time (worker_side.verify_attached, during an attach) and by a listing before the launch. Neither proves that this
invocation created it, so a second machine launching the same config name at the same time could have its clusters
stopped (by verify_attached during an attach, or by launch's `_fresh()` after the pre-launch check). Launch a config
from one machine only. Follow-up planned: per-invocation cluster names recorded in clusters.json.

Exit codes:
  0 every run collected and confirmed for this launch, and no cluster of the launch left live
  1 the launch failed before submitting anything (this invocation's failed record's ids stopped, or nothing)
  2 a run never started, even after one relaunch (nothing stopped: check and shut down by hand)
  3 spend cap reached: clusters stopped by id (a final checkpoint and a collect were attempted first)
  4 stored or collected state not confirmed: the run cluster is left up (the agent VM may hold the only copy);
    run `collect` again
  5 every run collected, but a cluster of the launch is still listed live after the stop requests
  6 the watch reached --max-hours (default run_timeout_hours + hold_hours + 1): nothing stopped
  7 refused: the config has no cloud-side limits (see 5 above)
  8 refused: a cluster of the config's names is live before the launch, or the listing could not be read
  9 refused: a v2 config without checkpoints (checkpoint_minutes 0 or over 30), outside a smoke
  10 refused by the budget (cloud/budget.py), or the budget check or its meter is unavailable
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_cluster as rc  # noqa: E402

EXIT_DONE, EXIT_LAUNCH_FAILED, EXIT_NEVER_RAN, EXIT_CAP = 0, 1, 2, 3
EXIT_UNCONFIRMED, EXIT_SHUTDOWN_UNCONFIRMED, EXIT_LIMIT, EXIT_REFUSED, EXIT_PRELIVE = 4, 5, 6, 7, 8
SUBMITTED = (rc.EXIT_LAUNCHED, rc.EXIT_SUBMITTED_LOST, rc.EXIT_UNCONFIRMED, rc.EXIT_INCOMPLETE)   # 0, 3, 4, 5
TIMEOUTS = {"status": 600, "collect": 1800, "relaunch": 1800}   # defaults; launch and collect get the Watch's
_NET = re.compile(r"gaierror|nodename nor servname|name resolution|Name or service not known|getaddrinfo|"
                  r"Timed out trying to connect|tls://|\bSSL|\bTLS\b|ConnectTimeout|ReadTimeout|TimeoutError|"
                  r"timed out|Connection(Reset|Refused|Aborted)Error|Connection (reset|refused|aborted)|"
                  r"Network is unreachable|RemoteDisconnected|CommClosedError|StreamClosedError|Max retries exceeded")


EXIT_NO_CHECKPOINTS, EXIT_BUDGET = 9, 10
MAX_CHECKPOINT_MINUTES, SMOKE_HOURS = 30.0, 0.5

# ---- decisions (pure) -----------------------------------------------------------------------

def checkpoint_refusal(cfg, allow_no_checkpoints=False):
    """Why a v2 config may not be launched for lack of checkpoints, or None. A v2 run needs checkpoint_minutes in
    (0, 30]; --allow-no-checkpoints lifts it only for a smoke (run_timeout_hours <= 0.5)."""
    m = getattr(cfg, "checkpoint_minutes", None)
    if cfg.pipeline != "v2" or (m and 0 < m <= MAX_CHECKPOINT_MINUTES):
        return None
    if allow_no_checkpoints and cfg.run_timeout_hours is not None and cfg.run_timeout_hours <= SMOKE_HOURS:
        return None
    return (f"{cfg.name}: checkpoint_minutes is {m!r}; a v2 run needs 0 < checkpoint_minutes <= "
            f"{MAX_CHECKPOINT_MINUTES:g} (--allow-no-checkpoints only with run_timeout_hours <= {SMOKE_HOURS})")


def _load_budget():
    import budget                # cloud/budget.py: the owner-signed budget and its spend estimator (PR #192)
    return budget


def refuse_reason(cfg, allow_no_limits=False):
    """Why this config may not be watched, or None."""
    missing = ([] if cfg.run_timeout_hours is not None else ["run_timeout_hours is None"]) + \
              ([] if cfg.cluster_timeout else ["cluster_timeout is empty"])
    if not missing or allow_no_limits:
        return None
    return (f"{cfg.name}: no cloud-side limit ({'; '.join(missing)}): set them in the config, or pass "
            "--allow-no-cloud-limits")


def recorded(record) -> dict:
    """{role: {"name", "id"}} of every cluster a clusters.json names."""
    if not isinstance(record, dict):
        return {}
    return {k: v for k, v in record.items() if isinstance(v, dict) and v.get("id") is not None and v.get("name")}


def after_launch(code, record, token, keeper_wanted=True) -> dict:
    """What the launcher's exit code and clusters.json authorize: {"action": "watch"|"stop", "keeper", "stop"}."""
    rec = record if isinstance(record, dict) else {}
    if code in SUBMITTED:
        keeper = bool(keeper_wanted) and (code == rc.EXIT_INCOMPLETE or (code == rc.EXIT_SUBMITTED_LOST
                                                                         and "keeper" not in rec))
        return {"action": "watch", "keeper": keeper, "stop": []}
    mine = bool(token) and rec.get("invocation") == token and rec.get("status") == "failed"
    ids = sorted(((v["name"], v["id"]) for v in recorded(rec).values()), key=lambda t: t[1]) if mine else []
    return {"action": "stop", "keeper": False, "stop": ids}


def is_network_error(text) -> bool:
    return bool(_NET.search(str(text or "")))


def parse_status(text, labels):
    """`status` output as {"cluster": "running"|"stopped", "runs": {label: {"state", "stored"}}}; None when it cannot
    be read (no cluster line, or a running cluster without a line for every label)."""
    m = re.search(r"^cluster \S+: (running|stopped);", str(text or ""), re.M)
    if not m:
        return None
    runs = {}
    if m.group(1) == "running":
        for lb in labels:
            lm = re.search(rf"^{re.escape(lb)}: (\S+)(.*)$", text, re.M)
            if not lm:
                return None
            sm = re.search(r"\bstored=(\S+)", lm.group(2))
            stored = sm.group(1) if sm and sm.group(1) != "None" else None
            runs[lb] = {"state": lm.group(1), "stored": stored}
    return {"cluster": m.group(1), "runs": runs}


def decide(st) -> str:
    """"collect" once every run is done or the run cluster has stopped; "watch"; "unknown" when unread."""
    if st is None:
        return "unknown"
    if st["cluster"] == "stopped" or all(r["state"] == "done" for r in st["runs"].values()):
        return "collect"
    return "watch"


def missing_action(st, missing_since, relaunched, now, window_s):
    """("relaunch", label) for a label missing `window_s` (once), ("never_ran", label) after its relaunch, or None.
    `missing_since` is updated in place; only definite readings of a running cluster count."""
    if not st or st["cluster"] != "running":
        return None
    for lb, r in st["runs"].items():
        if r["state"] != "missing":
            missing_since.pop(lb, None)
            continue
        if now - missing_since.setdefault(lb, now) >= window_s:
            return ("never_ran", lb) if lb in relaunched else ("relaunch", lb)
    return None


def _live(x) -> bool:
    return rc.ws.state_of(x) in rc._ATTACH


def journal_ids(journal, token) -> set:
    """Ids the construction journal records this invocation creating (run_cluster._construct: status "created")."""
    return {e.get("id") for e in journal or () if isinstance(e, dict) and token and e.get("inv") == token
            and e.get("status") == "created" and e.get("id") is not None}


def strays(items, names, recorded_ids, owned_ids) -> dict:
    """Live clusters with this launch's names that clusters.json does not record: "stop" those the journal
    attributes to this invocation (`owned_ids`), "report" the rest (never stopped)."""
    out = {"stop": [], "report": []}
    for x in rc._items(items):
        if x.get("name") in names and x.get("id") not in recorded_ids and _live(x):
            out["stop" if x.get("id") in owned_ids else "report"].append((x["name"], x["id"]))
    return {k: sorted(v, key=lambda t: t[1]) for k, v in out.items()}


def instance_count(details):
    """Live instances (scheduler and workers) in Coiled's cluster details (coiled.v2.cluster_details: "scheduler"
    and "workers", each with current_state.state), or None when they cannot be read."""
    if not isinstance(details, dict) or not isinstance(details.get("workers"), list):
        return None
    procs = [details.get("scheduler")] + details["workers"]
    return sum(1 for w in procs if isinstance(w, dict) and rc.ws.state_of(w) not in ("stopped", "error", ""))


# ---- the watch (I/O through an injected runner and Coiled API) --------------------------------

class CoiledApi:
    """The Coiled calls the watch makes: the listing and a stop by id, bounded in time. Spend is read through
    cloud/budget.py's meter, not here."""
    def list_clusters(self):
        import coiled
        return rc._items(rc._bounded(lambda: coiled.list_clusters(workspace=rc.WORKSPACE), timeout_s=120))

    def stop(self, name, cid, why):
        import vm_job
        rc._bounded(vm_job._stop_by_id, name, cid, why, timeout_s=120)


def shell_runner(cfg_path, prefix=None):
    """`run_cluster.py <cfg_path> <cmd>` as (exit code, or None past its deadline, output). `launch` is streamed,
    and past its deadline terminated and reaped (`prefix` replaces the command, for the self-test)."""
    prefix = prefix or [sys.executable, "-u", "-W", "ignore", os.path.join(HERE, "run_cluster.py"), cfg_path]

    def run(tail, env=None, timeout=None):
        import threading
        cmd = [*prefix, *tail]
        if tail[0] == "launch":
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            out = []

            def read():
                for line in p.stdout:
                    print(line, end="", flush=True)
                    out.append(line)
            t = threading.Thread(target=read, daemon=True)
            t.start()
            try:
                code = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                code = None
                out.append(f"\nTimeoutError: launch did not return within {timeout} s: terminated\n")
            t.join(timeout=10)
            return code, "".join(out)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                               timeout=timeout or TIMEOUTS.get(tail[0], 1800))
        except subprocess.TimeoutExpired as e:
            return None, f"TimeoutError: {' '.join(tail)} did not return within {e.timeout} s"
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    return run


class Watch:
    def __init__(self, cfg, cfg_path, token, runner, api, clock, sleep, cap_usd=650.0, interval_s=300.0,
                 start_s=300.0, max_hours=None, allow_no_limits=False, retries=6, retry_s=60.0,
                 cap_collect_s=600.0, launch_s=2700.0, relaunch_attempts=3, collect_s=1800.0, collect_attempts=6,
                 checkpoint_s=300.0, allow_no_checkpoints=False, budget=None):
        self.checkpoint_s, self.allow_no_ck = checkpoint_s, allow_no_checkpoints
        self.budget, self.meter, self.asked_cap = budget, None, cfg.cap_usd if cfg.cap_usd else cap_usd
        self.cfg, self.cfg_path, self.token, self.runner, self.api = cfg, cfg_path, token, runner, api
        # the watch never stops later than the config allows: the smaller of --cap-usd and the config's cap_usd
        self.cap = min(cap_usd, cfg.cap_usd) if cfg.cap_usd else cap_usd
        self.clock, self.sleep, self.interval_s, self.start_s = clock, sleep, interval_s, start_s
        self.max_hours = max_hours if max_hours is not None else (cfg.run_timeout_hours or 48.0) + cfg.hold_hours + 1
        self.allow, self.retries, self.retry_s = allow_no_limits, retries, retry_s
        self.names = {cfg.name, cfg.keeper_name, cfg.overflow_name}
        self.cap_collect_s, self.launch_s, self.relaunch_attempts = cap_collect_s, launch_s, relaunch_attempts
        self.collect_s, self.collect_attempts, self.lines = collect_s, collect_attempts, []
        self.env = {**os.environ, "MA_LAUNCH_INVOCATION": token}
        self.pending_strays, self.reported, self.leftovers, self.start = set(), set(), None, None

    def log(self, msg):
        self.lines.append(msg)
        print(f"{time.strftime('%F %T')} {self.cfg.name}: {msg}", flush=True)

    def safe(self, fn, *a):
        """A Coiled call; any error is "unknown" (None), never a reading."""
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001
            self.log(f"{getattr(fn, '__name__', 'call')} unavailable ({type(e).__name__}: {str(e)[:120]})")
            return None

    def call(self, tail, retries=None, timeout=None):
        """run_cluster.py, retried while it fails with a network error or a timeout."""
        n = retries or self.retries
        for i in range(n):
            code, out = self.runner(tail, self.env, timeout=timeout)
            if code == 0 or not (code is None or is_network_error(out)):
                return code, out
            self.log(f"{tail[0]}: network error or timeout (try {i + 1}/{n}): {str(out).strip()[-160:]}")
            if i < n - 1:
                self.sleep(self.retry_s)
        return code, out

    def raw_record(self):
        try:
            return json.load(open(self.cfg.clusters_file))
        except (OSError, ValueError):
            return None

    def record(self):
        """clusters.json when it is this invocation's, else None (then nothing is stopped by id)."""
        r = self.raw_record()
        return r if isinstance(r, dict) and r.get("invocation") == self.token else None

    def run(self) -> int:
        why = refuse_reason(self.cfg, self.allow)
        if why:
            self.log(f"refused: {why}")
            return EXIT_REFUSED
        why = checkpoint_refusal(self.cfg, self.allow_no_ck)
        if why:
            self.log(f"refused: {why}; nothing launched")
            return EXIT_NO_CHECKPOINTS
        why = self.budget_refusal()
        if why:
            self.log(f"refused by the budget: {why}; nothing launched")
            return EXIT_BUDGET
        items = None
        for i in range(self.retries):
            items = self.safe(self.api.list_clusters)
            if items is not None:
                break
            self.sleep(self.retry_s)
        live = [] if items is None else [(x.get("name"), x.get("id")) for x in rc._items(items)
                                         if x.get("name") in self.names and _live(x)]
        if items is None or live:
            self.log("refused: " + ("the cluster listing could not be read" if items is None else
                                    "live before the launch: " + ", ".join(f"{n} ({i})" for n, i in live))
                     + "; nothing launched, nothing stopped")
            return EXIT_PRELIVE
        self.log(f"launch, invocation {self.token}")
        code, out = self.runner(["launch"], self.env, timeout=self.launch_s)
        if code is None:
            self.log(f"launch did not return within {self.launch_s} s: terminated; treated as exit 4 (submitted, "
                     "unconfirmed): watching, nothing stopped")
            code = rc.EXIT_UNCONFIRMED
        d = after_launch(code, self.raw_record(), self.token, self.cfg.keeper)
        self.log(f"launch exit {code}: {d['action']}" + (f", stopping {d['stop']}" if d["stop"] else ""))
        if d["action"] == "stop":
            for name, cid in d["stop"]:
                self.safe(self.api.stop, name, cid, f"launch failed (invocation {self.token})")
            return self.leave(self.record(), EXIT_LAUNCH_FAILED)
        if d["keeper"]:
            self.log(f"keeper not confirmed: start it by hand with `python cloud/run_cluster.py {self.cfg_path} "
                     "keeper` after checking `status`")
        return self.watch()

    def over_limit(self) -> bool:
        if self.clock() - self.start < self.max_hours * 3600:
            return False
        self.log(f"watch limit {self.max_hours} h reached: nothing stopped")
        return True

    def watch(self) -> int:
        self.start, missing_since, relaunched, attempts = self.clock(), {}, set(), {}
        while True:
            if self.over_limit():
                where = self.final_checkpoint(self.record())
                code = self.leave(self.record(), EXIT_LIMIT)
                self.log(f"exit {code} (watch limit, nothing stopped); {where}")
                return code
            rec = self.record()
            if rec is None:
                self.log("clusters.json is not this invocation's: no sweep, no spend cap, no stop by id")
            items = self.safe(self.api.list_clusters) if rec else None
            if items is not None:
                self.sweep(rec, items)
            usd = self.spend(rec) if rec else None          # every interval, whatever the listing did
            if usd is not None and usd >= self.cap:
                return self.at_cap(rec)
            code, out = self.call(["status"])
            st = parse_status(out, self.cfg.labels) if code == 0 else None
            act = decide(st)
            self.log(f"status: {act} " + (json.dumps(st) if st else ""))
            if act == "collect":
                return self.finish(rec)
            m = missing_action(st, missing_since, relaunched, self.clock(), self.start_s)
            if m and m[0] == "never_ran":
                self.log(f"{m[1]} has not started, even after a relaunch: nothing stopped")
                return self.leave(rec, EXIT_NEVER_RAN)
            if m:
                lb = m[1]
                attempts[lb] = attempts.get(lb, 0) + 1
                rc_, rout = self.call(["relaunch", lb])
                accepted = rc_ in (rc.EXIT_LAUNCHED, rc.EXIT_UNCONFIRMED)
                self.log(f"relaunch {lb} (attempt {attempts[lb]}/{self.relaunch_attempts}) exit {rc_}, "
                         f"{'accepted' if accepted else 'not accepted'}: {str(rout).strip()[-200:]}")
                missing_since[lb] = self.clock()
                if accepted:
                    relaunched.add(lb)
                elif attempts[lb] >= self.relaunch_attempts:
                    self.log(f"{lb}: no relaunch accepted in {attempts[lb]} attempts: nothing stopped")
                    return self.leave(rec, EXIT_NEVER_RAN)
            self.sleep(self.interval_s)

    def owners(self, rec):
        return {v["id"] for v in recorded(rec).values()}, journal_ids(rc.read_journal(self.cfg), self.token)

    def leave(self, rec, code) -> int:
        """Before an exit that leaves clusters up: every live cluster of this launch's names and its ownership."""
        ids, owned = self.owners(rec)
        items = self.safe(self.api.list_clusters)
        self.leftovers = [] if items is None else [
            (x.get("name"), x.get("id"), "recorded" if x.get("id") in ids else "journal" if x.get("id") in owned
             else "unknown") for x in rc._items(items) if x.get("name") in self.names and _live(x)]
        for name, cid, who in self.leftovers or ([("listing unavailable", None, "-")] if items is None else []):
            self.log(f"left live: {name} ({cid}), {who}")
        return code

    def sweep(self, rec, items):
        s = strays(items, self.names, *self.owners(rec))
        for name, cid in s["report"]:
            if cid not in self.reported:
                self.reported.add(cid)
                self.log(f"{name} ({cid}): live, not recorded and not in this invocation's journal: reported, "
                         "never stopped by this watch")
        found = set(s["stop"])
        for name, cid in sorted(found & self.pending_strays, key=lambda t: t[1]):
            self.log(f"stray {name} ({cid}): stopping by id")
            self.safe(self.api.stop, name, cid, "stray cluster (attach race)")
        self.pending_strays = found          # each stray is stopped on every sighting after its first

    def budget_refusal(self):
        """None when the owner-signed budget admits this launch, else why. Asks cloud/budget.py's
        launch_budget_ok(scope, the config's cap_usd if set else --cap-usd, name) and takes its spend estimator
        (job_meter) for the cap; a missing module, the stub's NotImplementedError or any error refuses."""
        try:
            b = self.budget if self.budget is not None else _load_budget
            b = b() if callable(b) and not hasattr(b, "launch_budget_ok") else b
            self.budget = b
            scope = self.cfg.budget_scope or (b.scope_for_name(self.cfg.name) if hasattr(b, "scope_for_name") else "")
            if not scope:
                return f"{self.cfg.name}: no budget_scope in the config"
            ok, why = b.launch_budget_ok(scope, self.asked_cap, name=self.cfg.name)
            if not ok:
                return why
            self.meter = b.job_meter(scope)
        except Exception as e:  # noqa: BLE001 -- ImportError, NotImplementedError (the stub), anything else
            return f"budget check unavailable ({type(e).__name__}: {str(e)[:160]})"
        self.log(f"budget: {why}; this watch stops at ${self.cap:.2f}")
        return None

    def spend(self, rec, items=None):
        """Dollars this launch has cost so far: cloud/budget.py's estimator (max of billed, total_cost x $0.061 +
        cloud_total_cost, and its conservative per-instance estimate) summed over the cluster ids clusters.json
        records; None (unknown: never a stop) when any reading fails."""
        try:
            parts = {role: float(self.meter(v["id"])) for role, v in recorded(rec).items()}
        except Exception as e:  # noqa: BLE001
            self.log(f"spend: unknown ({type(e).__name__}: {str(e)[:120]})")
            return None
        total = round(sum(parts.values()), 2)
        self.log(f"spend: ${total:.2f} of cap ${self.cap:.2f} ("
                 + ", ".join(f"{r} ${u:.2f}" for r, u in sorted(parts.items())) + ")")
        return total

    def collected(self):
        """(every label collected for this launch, the labels that are not)."""
        try:
            assign = json.load(open(self.cfg.state_file))
        except (OSError, ValueError):
            assign = {}
        bad = []
        for lb in self.cfg.labels:
            a = assign.get(lb) if isinstance(assign, dict) else None
            if (not isinstance(a, dict) or a.get("invocation") != self.token
                    or rc.local_is_current(os.path.join(self.cfg.out_dir, lb), a.get("launch_id")) is not True):
                bad.append(lb)
        return not bad, bad

    def stop_all(self, rec, why) -> bool:
        """Stop, by id, every live cluster clusters.json records for this invocation and every stray its journal
        attributes to it, nothing else; True once none of them is listed live."""
        ids, owned = self.owners(rec)
        for _ in range(self.retries):
            items = self.safe(self.api.list_clusters)
            if items is not None:
                live = {(x.get("name"), x.get("id")) for x in rc._items(items) if _live(x) and x.get("id") in ids}
                live |= set(strays(items, self.names, ids, owned)["stop"])
                if not live:
                    return True
                for name, cid in sorted(live, key=lambda t: t[1]):
                    self.safe(self.api.stop, name, cid, why)
            self.sleep(self.retry_s)
        return False

    def finish(self, rec) -> int:
        ok, bad = False, list(self.cfg.labels)
        for i in range(self.collect_attempts):
            stop = self.collect_guard(rec) if i else None
            if stop is not None:
                return stop
            code, out = self.call(["collect", "--no-shutdown"], retries=1, timeout=self.collect_s)   # one attempt
            self.log(f"collect attempt {i + 1}/{self.collect_attempts} exit {code}: {str(out).strip()[-300:]}")
            ok, bad = self.collected()
            if ok:
                break
            self.sleep(self.retry_s)
        if not ok and self.collect_guard(rec) is not None:      # the last attempt may have taken its full deadline
            return self.collect_guard_code
        if not ok:
            self.log(f"not confirmed collected for this launch: {bad}; the run cluster is left up. Run "
                     f"`python cloud/run_cluster.py {self.cfg_path} collect` again")
            return self.leave(rec, EXIT_UNCONFIRMED)
        if rec is None or not self.stop_all(rec, "every run collected"):
            self.log("every run collected, but a cluster of this launch may still be live: check the listing")
            return EXIT_SHUTDOWN_UNCONFIRMED
        self.log("every run collected; no cluster of this launch is live")
        return EXIT_DONE

    def collect_guard(self, rec):
        """Between collect attempts and after the last: EXIT_LIMIT (nothing stopped) or EXIT_CAP (stopped by id),
        else None."""
        self.collect_guard_code = None
        if self.over_limit():
            self.collect_guard_code = self.leave(rec, EXIT_LIMIT)
        elif rec is not None:
            usd = self.spend(rec)
            if usd is not None and usd >= self.cap:
                self.log(f"spend cap ${self.cap:.2f} reached (${usd:.2f}) during the collect attempts: stopping by id")
                if not self.stop_all(rec, f"spend cap ${self.cap:.2f}"):
                    self.log("a cluster of this launch may still be live after the stop requests")
                self.collect_guard_code = EXIT_CAP
        return self.collect_guard_code

    def final_checkpoint(self, rec) -> str:
        """Before a stop at the cap or the watch limit: one checkpoint of every running run (`checkpoint-now`, one
        attempt, every label under one deadline of checkpoint_s), whatever it returns; it names this watch's
        invocation and launch, so checkpoint-now refuses any other record. Returns where the checkpoints are."""
        lid = (rec or {}).get("launch_id")
        code, out = self.call(["checkpoint-now", "--timeout-s", f"{int(self.checkpoint_s)}", "--invocation", self.token]
                              + (["--launch-id", lid] if lid else []), retries=1, timeout=self.checkpoint_s + 60)
        self.log(f"final checkpoint exit {code}: {str(out).strip()[-300:]}")
        paths = [rc.ws.checkpoint_pointer(self.cfg.storage_prefix, lid, lb) for lb in self.cfg.labels] if lid else []
        return (f"latest checkpoints: {', '.join(paths)}; fetch with `python cloud/run_cluster.py {self.cfg_path} "
                "checkpoint`" if paths else "no launch id recorded: no checkpoint path")

    def at_cap(self, rec) -> int:
        self.log(f"spend cap ${self.cap:.2f} reached: a final checkpoint (at most {self.checkpoint_s} s), one collect "
                 f"(at most {self.cap_collect_s} s), then stopping every cluster of this launch by id")
        where = self.final_checkpoint(rec)
        code, out = self.call(["collect", "--no-shutdown"], retries=1, timeout=self.cap_collect_s)
        self.log(f"collect exit {code}: {str(out).strip()[-300:]}; collected: {self.collected()}")
        if not self.stop_all(rec, f"spend cap ${self.cap:.2f}"):
            self.log("a cluster of this launch may still be live after the stop requests: check the listing")
        self.log(f"exit {EXIT_CAP} (spend cap): {where}")
        return EXIT_CAP


# ---- self-test ------------------------------------------------------------------------------

def _self_test() -> int:
    import contextlib
    import io
    import tempfile
    import traceback
    fails = []

    def case(cid, what, fn):
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:     # the watch's own log, shown on a failure
                good = bool(fn())
        except Exception as e:  # noqa: BLE001 — a stub or a bug: the case fails
            good = False
            what += f" [{type(e).__name__}: {str(e)[:80]}]"
            if os.environ.get("MA_TRACE"):
                traceback.print_exc()
        if not good and "out" in locals():
            print(out.getvalue()[-2000:], end="")
        print(("PASS " if good else "FAIL ") + f"{cid} {what}")
        if not good:
            fails.append(cid)

    TOKEN = "tok-123"

    def cfg_of(tmp, name="cfg-x", labels=("A", "B"), **kw):
        kw.setdefault("run_timeout_hours", 2.0)
        kw.setdefault("cluster_timeout", "3 hours")
        kw.setdefault("budget_scope", "f6")
        return rc.RunConfig(name=name, question_file="q.txt", labels=labels, out_dir=tmp, pipeline="v2", **kw)

    def record(cfg, token=TOKEN, status="launched", roles=("run", "overflow", "keeper"), launched="2026-11-10T00:00:00Z"):
        names = {"run": cfg.name, "overflow": cfg.overflow_name, "keeper": cfg.keeper_name}
        r = {role: {"name": names[role], "id": 100 + i} for i, role in enumerate(roles)}
        return {**r, "launch_id": "L1", "invocation": token, "launched_utc": launched, "status": status}

    def status_text(cfg, states, cluster="running"):
        lines = [f"cluster {cfg.name}: {cluster}; keeper: running; overflow: running"]
        for label, s in states.items():
            if s.startswith("done"):
                stored = s.split(":", 1)[1] if ":" in s else "outputs/x.tar.gz"
                lines.append(f"{label}: done exit=0 12s run_dir=/w/{label} stored={stored}")
            else:
                lines.append(f"{label}: {s} (log 10 bytes)")
        return "\n".join(lines) + "\n"

    class Runner:
        """Fake `run_cluster.py <cfg> <cmd>`: responses per command, each a (rc, out) or a callable."""
        def __init__(self, script, log):
            self.script, self.log, self.envs = {k: list(v) for k, v in script.items()}, log, []
            self.tails, self.timeouts = [], []

        def __call__(self, tail, env=None, timeout=None):
            self.log.append(("run", tail[0]))
            self.envs.append(dict(env or {}))
            self.tails.append(list(tail))
            self.timeouts.append((tail[0], timeout))
            seq = self.script.get(tail[0]) or [(0, "")]
            r = seq.pop(0) if len(seq) > 1 else seq[0]
            return r(tail) if callable(r) else r

    Api = lambda cfg, log, live=None: _FakeApi().bind(cfg, log, live or {})  # noqa: E731

    def fake_time():
        t = [rc._ts("2026-11-10T00:10:00Z")]
        return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))

    def write_record(cfg, r):
        os.makedirs(cfg.out_dir, exist_ok=True)
        json.dump(r, open(cfg.clusters_file, "w"))

    def write_assign(cfg, token=TOKEN):
        json.dump({lb: {"launch_id": "L1", "worker": f"w{lb}", "invocation": token} for lb in cfg.labels},
                  open(cfg.state_file, "w"))

    def collected(cfg, labels=None):
        def go(tail):
            if "--no-shutdown" not in tail:     # P1-1: `collect` without it shuts the clusters down by name
                raise AssertionError(f"collect called without --no-shutdown: {tail}")
            for lb in labels or cfg.labels:
                os.makedirs(os.path.join(cfg.out_dir, lb), exist_ok=True)
                json.dump({"launch_id": "L1"}, open(os.path.join(cfg.out_dir, lb, "result.json"), "w"))
            return 0, "every run is collected"
        return go

    def watch(cfg, runner, api, **kw):
        clock, sleep = fake_time()
        kw.setdefault("retry_s", 1.0)
        kw.setdefault("budget", _FakeBudget(api))     # never the stub or a real cloud/budget.py
        return Watch(cfg, "cfg.py", TOKEN, runner, api, clock, sleep, **kw)

    def scenario(launch_rc, rec, script=None, live=None, name="cfg-x", labels=("A", "B"), journal=(), cfg_kw=None,
                 **kw):
        """A full run of the watchdog against fakes: (exit code, call log, cfg, runner, watch)."""
        tmp = tempfile.mkdtemp()
        cfg = cfg_of(tmp, name=name, labels=labels, **(cfg_kw or {}))
        log = []
        r = rec(cfg) if callable(rec) else rec
        box = {}

        def launch(tail):
            box["api"].up = True
            if r is not None:
                write_record(cfg, r)
            write_assign(cfg)
            with open(os.path.join(cfg.out_dir, rc.JOURNAL), "a") as f:
                for e in journal:
                    f.write(json.dumps(e) + "\n")
            return (None, "TimeoutError: launch did not return") if launch_rc is None else \
                (launch_rc, f"invocation {TOKEN}\n")
        sc = {"launch": [launch], **(script(cfg) if script else {})}
        runner = Runner(sc, log)
        live = live if live is not None else ({v["id"]: v["name"] for v in r.values() if isinstance(v, dict)}
                                              if r else {})
        api = kw.pop("api", None)
        api = api.bind(cfg, log, live) if api is not None else Api(cfg, log, live=live)
        box["api"] = api
        w = watch(cfg, runner, api, **kw)
        return w.run(), log, cfg, runner, w

    def done_script(cfg):
        return {"status": [(0, status_text(cfg, {lb: "done" for lb in cfg.labels}))], "collect": [collected(cfg)]}

    stops = lambda log: [c[1] for c in log if c[0] == "stop"]  # noqa: E731
    runs = lambda log: [c[1] for c in log if c[0] == "run"]    # noqa: E731

    # 1. the launcher's exit code: 0/3/4/5 never stop; 1 or other stops only this invocation's failed record's ids
    c = cfg_of(tempfile.mkdtemp())
    for code in (0, 3, 4):
        case(f"LW1a.{code}", f"launch exit {code}: watch, stop nothing, whatever the record says",
             lambda code=code: all(after_launch(code, r, TOKEN)["action"] == "watch" and not after_launch(
                 code, r, TOKEN)["stop"] for r in (record(c), record(c, status="failed"), None)))
    case("LW1b", "launch exit 5: watch; the keeper is not confirmed",
         lambda: (lambda d: d["action"] == "watch" and d["keeper"] and not d["stop"])(after_launch(5, record(c), TOKEN)))
    case("LW1c", "launch exit 3 with no keeper recorded: keeper not confirmed; with one recorded: confirmed",
         lambda: after_launch(3, record(c, roles=("run",)), TOKEN)["keeper"]
         and not after_launch(3, record(c), TOKEN)["keeper"]
         and not after_launch(3, record(c, roles=("run",)), TOKEN, keeper_wanted=False)["keeper"])
    case("LW1d", "launch exit 1, this token's failed record: stop exactly its ids",
         lambda: (lambda d: d["action"] == "stop" and sorted(i for _, i in d["stop"]) == [100, 101])(
             after_launch(1, record(c, status="failed", roles=("run", "overflow")), TOKEN)))
    case("LW1e", "launch exit 1, another token's failed record: stop nothing",
         lambda: after_launch(1, record(c, token="other", status="failed"), TOKEN)["stop"] == [])
    case("LW1f", "launch exit 1, this token but status not failed: stop nothing",
         lambda: all(after_launch(1, record(c, status=s), TOKEN)["stop"] == []
                     for s in ("submitting", "submitted", "launched", None)))
    case("LW1g", "no record / unreadable record: stop nothing; any other code follows the exit-1 rule",
         lambda: after_launch(1, None, TOKEN)["stop"] == [] and after_launch(1, ["x"], TOKEN)["stop"] == []
         and len(after_launch(2, record(c, status="failed"), TOKEN)["stop"]) == 3
         and after_launch(-9, None, TOKEN)["action"] == "stop")
    case("LW1h", "end to end: exit 4 is watched to the end, never stopped before collect; launch env has the token",
         lambda: (lambda r: r[0] == EXIT_DONE and runs(r[1])[:2] == ["launch", "status"]
                  and r[3].envs[0].get("MA_LAUNCH_INVOCATION") == TOKEN
                  and r[1].index(("run", "collect")) < r[1].index(("stop", 100)))(
             scenario(4, record, done_script)))
    case("LW1i", "end to end: exit 1 with this token's failed record stops its ids by id, exits 1, no status",
         lambda: (lambda r: r[0] == EXIT_LAUNCH_FAILED and stops(r[1]) == [100] and "status" not in runs(r[1]))(
             scenario(1, lambda cfg: record(cfg, status="failed", roles=("run",)))))
    case("LW1j", "end to end: exit 1 with an earlier launch's record stops nothing",
         lambda: (lambda r: r[0] == EXIT_LAUNCH_FAILED and stops(r[1]) == [])(
             scenario(1, lambda cfg: record(cfg, token="earlier", status="failed"))))
    case("LW1k", "end to end: exit 5 and exit 3 without a keeper: `keeper` is never run, one line says to start it "
         "by hand", lambda: all(r[0] == EXIT_DONE and "keeper" not in runs(r[1])
                                and sum("keeper not confirmed" in ln for ln in r[4].lines) == 1
                                for r in (scenario(5, record, done_script),
                                          scenario(3, lambda cfg: record(cfg, roles=("run", "overflow")),
                                                   done_script))))

    # 2. collect first, confirm, then stop; unconfirmed storage leaves the run cluster up
    case("LW2a", "parse_status: per-label state and stored (None when status prints stored=None)",
         lambda: (lambda s: s["cluster"] == "running" and s["runs"]["A"]["state"] == "done"
                  and s["runs"]["A"]["stored"] is None and s["runs"]["B"]["state"] == "running")(
             parse_status(status_text(c, {"A": "done:None", "B": "running"}), ("A", "B"))))
    case("LW2b", "parse_status: no cluster line, or a label missing from the output, is unknown (None)",
         lambda: parse_status("Traceback ...\n", ("A",)) is None
         and parse_status(status_text(c, {"A": "running"}), ("A", "B")) is None)
    case("LW2c", "decide: every run done, or the run cluster stopped, means collect; else watch; unknown stays unknown",
         lambda: decide(parse_status(status_text(c, {"A": "done", "B": "done"}), ("A", "B"))) == "collect"
         and decide(parse_status(status_text(c, {}, cluster="stopped"), ("A", "B"))) == "collect"
         and decide(parse_status(status_text(c, {"A": "done", "B": "running"}), ("A", "B"))) == "watch"
         and decide(None) == "unknown")
    case("LW2d", "done with stored=None and collect does not confirm: run cluster not stopped, exit 4",
         lambda: (lambda r: r[0] == EXIT_UNCONFIRMED and 100 not in stops(r[1]) and "collect" in runs(r[1]))(
             scenario(0, record, lambda cfg: {
                 "status": [(0, status_text(cfg, {"A": "done:None", "B": "done:None"}))],
                 "collect": [(0, "A: still running\nB: still running")]})))
    case("LW2e", "collect confirms only one of two labels: exit 4, nothing stopped",
         lambda: (lambda r: r[0] == EXIT_UNCONFIRMED and stops(r[1]) == [])(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [collected(cfg, ["A"])]})))
    case("LW2f", "a collected copy from an earlier launch (other launch_id) does not count",
         lambda: (lambda r: r[0] == EXIT_UNCONFIRMED)(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [lambda t: (collected(cfg)(t), [json.dump(
                                                  {"launch_id": "L0"}, open(os.path.join(cfg.out_dir, lb,
                                                                                         "result.json"), "w"))
                                                  for lb in cfg.labels], (0, ""))[-1]]})))
    case("LW2g", "run cluster stopped by itself: collect --no-shutdown, confirm, exit 0; never `shutdown` by name",
         lambda: (lambda r: r[0] == EXIT_DONE and "shutdown" not in runs(r[1]) and "collect" in runs(r[1])
                  and all("--no-shutdown" in t for t in r[3].tails if t[0] == "collect"))(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {}, cluster="stopped"))],
                                              "collect": [collected(cfg)]}, live={})))
    case("LW2h", "collected but a cluster stays live after the stop requests: exit 5",
         lambda: (lambda r: r[0] == EXIT_SHUTDOWN_UNCONFIRMED)(
             scenario(0, record, done_script, api=_Sticky())))

    # 3. spend cap, in dollars, from cloud/budget.py's estimator (job_meter) over this launch's recorded ids
    case("LW3a", "spend: the budget estimator's dollars summed over the recorded ids only, logged every check",
         lambda: (lambda r: any("spend: $12.50 of cap $650.00" in ln for ln in r[4].lines)
                  and sorted(set(r[4].budget.metered)) == [100, 101, 102])(
             scenario(0, record, done_script, api=_Billed(12.5))))
    case("LW3b", "at the cap: a collect is attempted, then every live recorded cluster is stopped by id, exit 3",
         lambda: (lambda r: r[0] == EXIT_CAP and sorted(stops(r[1])) == [100, 101, 102]
                  and r[1].index(("run", "collect")) < r[1].index(next(x for x in r[1] if x[0] == "stop")))(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "running", "B": "running"}))],
                                              "collect": [(0, "A: still running")]}, cap_usd=650.0,
                      api=_Billed(700.0))))
    case("LW3c", "below the cap the watch goes on (billing by recorded id only)",
         lambda: (lambda r: r[0] == EXIT_DONE)(scenario(0, record, done_script, cap_usd=650.0, api=_Billed(10.0))))

    # 5. cloud-side limits
    case("LW5a", "refuse a config with run_timeout_hours None or cluster_timeout empty; the override allows it",
         lambda: refuse_reason(cfg_of(tempfile.mkdtemp(), run_timeout_hours=None))
         and refuse_reason(cfg_of(tempfile.mkdtemp(), cluster_timeout=""))
         and refuse_reason(cfg_of(tempfile.mkdtemp())) is None
         and refuse_reason(cfg_of(tempfile.mkdtemp(), run_timeout_hours=None, cluster_timeout=""), True) is None)
    case("LW5b", "a refused config exits 7 and never launches",
         lambda: (lambda tmp, log: watch(cfg_of(tmp, cluster_timeout=""), Runner({}, log), Api(None, log)).run()
                  == EXIT_REFUSED and log == [])(tempfile.mkdtemp(), []))

    # 6. network errors are retried, never "not running" or a failure
    case("LW6a", "is_network_error: DNS, TLS, timeouts; not an ordinary error",
         lambda: all(is_network_error(t) for t in (
             "socket.gaierror: [Errno 8] nodename nor servname provided, or not known",
             "OSError: Timed out trying to connect to tls://1.2.3.4:443 after 30 s",
             "ssl.SSLError: [SSL: UNEXPECTED_EOF_WHILE_READING]", "httpx.ConnectTimeout",
             "Temporary failure in name resolution", "ConnectionResetError: [Errno 54]"))
         and not is_network_error("KeyError: 'A'"))
    case("LW6b", "status fails with a network error, then reads: nothing stopped before the collect, exit 0",
         lambda: (lambda r: r[0] == EXIT_DONE and r[1].index(("run", "collect")) < r[1].index(("stop", 100)))(
             scenario(0, record, lambda cfg: {"status": [(1, "OSError: Timed out trying to connect to tls://x:443"),
                                                         (1, "nodename nor servname provided"),
                                                         (0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [collected(cfg)]})))
    case("LW6c", "the cluster listing raises a network error: no stop, no cap, the watch goes on",
         lambda: (lambda r: r[0] == EXIT_LIMIT and stops(r[1]) == [])(
             scenario(0, record, lambda cfg: {"status": [(1, "ssl.SSLError: EOF")]}, max_hours=1.0,
                      api=_Failing())))
    case("LW6d", "collect fails with a network error, then succeeds: retried, exit 0",
         lambda: (lambda r: r[0] == EXIT_DONE and runs(r[1]).count("collect") == 2)(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [(1, "Temporary failure in name resolution"),
                                                          collected(cfg)]})))

    def call_case():
        log = []
        w = watch(c, Runner({"status": [(1, "ConnectTimeout"), (0, "ok")], "x": [(2, "KeyError: 'A'"), (0, "")]},
                            log), Api(c, log))
        return w.call(["status"]) == (0, "ok") and w.call(["x"]) == (2, "KeyError: 'A'") \
            and runs(log) == ["status", "status", "x"]
    case("LW6e", "call: a network failure is retried; an ordinary failure is returned at once", call_case)

    # 7. a run that never starts: one relaunch, then exit 2 (nothing stopped)
    case("LW7a", "missing_action: missing past the window -> relaunch once, then never_ran",
         lambda: (lambda st, ms: missing_action(st, ms, set(), 0.0, 300) is None
                  and missing_action(st, ms, set(), 301.0, 300) == ("relaunch", "A")
                  and missing_action(st, {"A": 0.0}, {"A"}, 301.0, 300) == ("never_ran", "A"))(
             parse_status(status_text(c, {"A": "missing", "B": "running"}), ("A", "B")), {}))
    case("LW7b", "end to end: missing, relaunch, still missing: exit 2, nothing stopped",
         lambda: (lambda r: r[0] == EXIT_NEVER_RAN and "relaunch" in runs(r[1]) and stops(r[1]) == [])(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "missing", "B": "running"}))]})))

    # 8. names come from the config
    case("LW8", "another config's name and labels drive the status parse, the stop ids and the collect check",
         lambda: (lambda r: r[0] == EXIT_DONE and stops(r[1]) == [100, 101, 102])(
             scenario(0, record, done_script, name="other-run", labels=("X1", "X2", "X3"))))

    # Codex round 1 on the launch watch
    # P1-2: a stray is stopped only when this invocation's construction journal names its id
    J = [{"inv": TOKEN, "name": "n", "id": 1, "status": "created", "seq": "a"},
         {"inv": "other", "name": "n", "id": 8, "status": "created", "seq": "b"},
         {"inv": TOKEN, "name": "n", "id": 7, "status": "attached", "seq": "c"},
         {"inv": TOKEN, "name": "n", "status": "constructing", "seq": "d", "t": 0}]
    run_ = lambda i, n="n", st="running": {"name": n, "id": i, "current_state": {"state": st},  # noqa: E731
                                           "created": "2026-11-10T00:05:00Z"}
    items = [run_(1), run_(2), run_(4, st="stopped"), run_(5, "other"), run_(7), run_(8)]
    case("LW9a", "journal_ids: only ids this token's journal records as created",
         lambda: journal_ids(J, TOKEN) == {1})
    case("LW9b", "strays: journal-attributed -> stop; same name, unattributed -> report only; recorded/stopped/other "
         "names -> neither", lambda: strays(items, {"n"}, {2}, {1}) == {"stop": [("n", 1)], "report": [("n", 7),
                                                                                                        ("n", 8)]})
    case("LW9c", "watch: a journal-attributed stray is stopped by id on its second sighting",
         lambda: (lambda r: 999 in stops(r[1]) and r[1].index(("run", "status")) < r[1].index(("stop", 999))
                  < r[1].index(("run", "collect")))(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "running", "B": "running"})),
                                                         (0, status_text(cfg, {"A": "running", "B": "running"})),
                                                         (0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [collected(cfg)]}, api=_Stray(),
                      journal=[{"inv": TOKEN, "name": "k", "id": 999, "status": "created", "seq": "x"}])))
    case("LW9d", "a later cluster of the run's name from another invocation is never stopped (sweep, stop_all)",
         lambda: (lambda r: r[0] == EXIT_DONE and 888 not in stops(r[1]) and stops(r[1]) == [100, 101, 102])(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "running", "B": "running"})),
                                                         (0, status_text(cfg, {"A": "running", "B": "running"})),
                                                         (0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [collected(cfg)]}, api=_Foreign(),
                      journal=[{"inv": "other", "name": "x", "id": 888, "status": "created", "seq": "y"}])))
    case("LW9e", "at the cap the foreign cluster is not stopped either",
         lambda: (lambda r: r[0] == EXIT_CAP and sorted(stops(r[1])) == [100, 101, 102])(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "running", "B": "running"}))]},
                      api=_Foreign(700.0))))

    # P1-3: live instance counts from the Coiled API, else the configured maximum (never compute_start)
    det = {"scheduler": {"current_state": {"state": "started"}},
           "workers": [{"current_state": {"state": "ready"}}, {"current_state": {"state": "started"}},
                       {"current_state": {"state": "stopped"}}]}
    case("LW10a", "instance_count: the live scheduler and workers of a cluster's details; unreadable -> None",
         lambda: instance_count(det) == 3 and instance_count(None) is None and instance_count({"workers": "x"}) is None)

    case("LW10b", "spend: a meter reading that fails is 'unknown': no stop, the watch goes on",
         lambda: (lambda r: r[0] == EXIT_DONE and any("spend: unknown" in ln for ln in r[4].lines))(
             scenario(0, record, done_script, api=_MeterFails())))

    # P1-4: the emergency collect at the cap is one bounded attempt; cap and limit checked between collect retries
    case("LW11a", "at the cap: one collect with the short deadline, then the stops even when it times out",
         lambda: (lambda r: r[0] == EXIT_CAP and runs(r[1]).count("collect") == 1
                  and ("collect", 600.0) in r[3].timeouts and sorted(stops(r[1])) == [100, 101, 102])(
             scenario(0, record, lambda cfg: {"collect": [(None, "TimeoutError: collect")]}, api=_Billed(700.0))))
    case("LW11b", "the cap is reached between collect retries: exit 3 with the stops, retries cut short",
         lambda: (lambda r: r[0] == EXIT_CAP and runs(r[1]).count("collect") < 6 and stops(r[1]))(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "done:None", "B": "done:None"}))],
                                              "collect": [(0, "A: still running")]}, api=_Growing())))
    case("LW11c", "--max-hours is reached between collect retries: exit 6, nothing stopped",
         lambda: (lambda r: r[0] == EXIT_LIMIT and stops(r[1]) == [] and runs(r[1]).count("collect") < 6)(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "done:None", "B": "done:None"}))],
                                              "collect": [(0, "A: still running")]}, max_hours=0.1, retry_s=600.0)))

    # P2-5: a relaunch counts only when accepted (exit 0 or 4); bounded attempts
    miss = lambda cfg: {"status": [(0, status_text(cfg, {"A": "missing", "B": "running"}))]}  # noqa: E731
    case("LW12a", "relaunch refused, then accepted: two attempts, then exit 2",
         lambda: (lambda r: r[0] == EXIT_NEVER_RAN and runs(r[1]).count("relaunch") == 2 and stops(r[1]) == [])(
             scenario(0, record, lambda cfg: {**miss(cfg), "relaunch": [(2, "refusing: queued"), (0, "")]})))
    case("LW12b", "relaunch never accepted (network, refused): bounded at 3 attempts, then exit 2",
         lambda: (lambda r: r[0] == EXIT_NEVER_RAN and runs(r[1]).count("relaunch") == 3 and stops(r[1]) == [])(
             scenario(0, record, lambda cfg: {**miss(cfg), "relaunch": [(2, "refusing: still running")]},
                      relaunch_attempts=3)))

    # P2-6: the launch has a deadline; past it, it is treated as exit 4
    case("LW13a", "launch past its deadline: watched like exit 4 (nothing stopped before collect)",
         lambda: (lambda r: r[0] == EXIT_DONE and ("launch", 2700.0) in r[3].timeouts
                  and r[1].index(("run", "collect")) < r[1].index(("stop", 100)))(
             scenario(None, record, done_script)))

    def deadline_case():
        import time as _t
        run = shell_runner("cfg.py", prefix=[sys.executable, "-c",
                                             "import time; print('hello', flush=True); time.sleep(60)"])
        t0 = _t.time()
        code, out = run(["launch"], None, timeout=1.0)
        return code is None and "hello" in out and _t.time() - t0 < 20
    case("LW13b", "shell_runner: a launch past its deadline is terminated and reaped, its output kept", deadline_case)

    # P2-7: before an exit that leaves clusters up, every live cluster of the launch's names and its ownership
    case("LW14a", "exit 4: leftovers listed as recorded / journal / unknown; nothing extra stopped",
         lambda: (lambda r: r[0] == EXIT_UNCONFIRMED and stops(r[1]) == []
                  and sorted(r[4].leftovers) == sorted([(r[2].name, 100, "recorded"), (r[2].overflow_name, 101,
                                                        "recorded"), (r[2].keeper_name, 102, "recorded"),
                                                        (r[2].keeper_name, 999, "journal"), (r[2].name, 888, "unknown")]))(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "done:None", "B": "done:None"}))],
                                              "collect": [(0, "A: still running")]}, api=_Both(),
                      journal=[{"inv": TOKEN, "name": "k", "id": 999, "status": "created", "seq": "x"}])))
    case("LW14b", "exits 2 and 6 also report the leftovers",
         lambda: all(r[4].leftovers for r in (scenario(0, record, miss),
                                               scenario(0, record, lambda cfg: {"status": [(1, "ssl.SSLError")]},
                                                        max_hours=0.5))))

    # Codex round 2 on the launch watch
    # P1-2: no launch while a cluster of the config's names is live (launch's _fresh() replaces same-named helpers)
    class _Pre(_FakeApi):
        def __init__(self, pre):
            super().__init__()
            self.pre = pre
    foreign = [{"name": "cfg-x-keeper", "id": 77, "current_state": {"state": "running"},
                "created": "2026-11-09T00:00:00Z"}]
    case("LW15a", "a live cluster of the config's names before the launch: exit 8, no launch, nothing stopped",
         lambda: (lambda r: r[0] == EXIT_PRELIVE and runs(r[1]) == [] and stops(r[1]) == []
                  and any("cfg-x-keeper (77)" in ln for ln in r[4].lines))(
             scenario(0, record, done_script, api=_Pre(foreign))))
    case("LW15b", "a stopped one, or another name, does not block the launch",
         lambda: scenario(0, record, done_script, api=_Pre([{**foreign[0], "current_state": {"state": "stopped"}},
                                                            {**foreign[0], "name": "other"}]))[0] == EXIT_DONE)
    case("LW15c", "the listing before the launch cannot be read: exit 8, no launch",
         lambda: (lambda r: r[0] == EXIT_PRELIVE and runs(r[1]) == [])(scenario(0, record, done_script,
                                                                                api=_PreFailing())))

    # P2-3: a collect is one attempt with its own deadline; the runner's network retry does not stack
    case("LW16a", "collect always fails with a network error: 6 attempts in all, each with the 30-minute deadline",
         lambda: (lambda r: r[0] == EXIT_UNCONFIRMED and runs(r[1]).count("collect") == 6
                  and all(t == 1800.0 for k, t in r[3].timeouts if k == "collect"))(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [(1, "Temporary failure in name resolution")]})))
    case("LW16b", "--collect-minutes sets the deadline and --collect-attempts the bound",
         lambda: (lambda r: runs(r[1]).count("collect") == 2 and ("collect", 120.0) in r[3].timeouts)(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [(1, "ConnectTimeout")]}, collect_s=120.0,
                      collect_attempts=2)))

    # P2-4: exit 1 also reports what is left live
    case("LW17a", "exit 1 with an earlier launch's record: nothing stopped, leftovers listed",
         lambda: (lambda r: r[0] == EXIT_LAUNCH_FAILED and stops(r[1]) == [] and r[4].leftovers is not None
                  and len(r[4].leftovers) == 3)(
             scenario(1, lambda cfg: record(cfg, token="earlier", status="failed"))))
    case("LW17b", "exit 1 with this token's failed record: its ids stopped, then the leftovers listed",
         lambda: (lambda r: r[0] == EXIT_LAUNCH_FAILED and stops(r[1]) == [100]
                  and r[4].leftovers == [] and r[1][-1] == ("list",))(
             scenario(1, lambda cfg: record(cfg, status="failed", roles=("run",)))))

    # Codex round 3: after the last collect attempt, the cap and --max-hours are checked once more
    fail6 = lambda cfg: {"status": [(0, status_text(cfg, {"A": "done:None", "B": "done:None"}))],  # noqa: E731
                         "collect": [(None, "TimeoutError: collect did not return within 1800 s")]}
    case("LW18a", "six 30-minute collect failures, credits cross the cap during the sixth: exit 3 with the stops",
         lambda: (lambda r: r[0] == EXIT_CAP and runs(r[1]).count("collect") == 6
                  and sorted(stops(r[1])) == [100, 101, 102])(scenario(0, record, fail6, api=_GrowingAt(7))))
    case("LW18b", "--max-hours passed during the sixth collect attempt: exit 6, nothing stopped",
         lambda: (lambda r: r[0] == EXIT_LIMIT and runs(r[1]).count("collect") == 6 and stops(r[1]) == [])(
             scenario(0, record, fail6, retry_s=600.0, max_hours=3300 / 3600)))

    # F6 run checkpoints (2026-11-11: the preview stopped at its cap after 3 h with nothing stored)
    running = lambda cfg: {"status": [(0, status_text(cfg, {"A": "running", "B": "running"}))]}  # noqa: E731
    ck_path = "outputs/runs/cfg-x/launch_L1/checkpoint_A.json"     # the pointer to the latest generation
    case("LW19a", "at the cap: one final checkpoint is requested and awaited before any stop; the exit names it",
         lambda: (lambda r: r[0] == EXIT_CAP and "checkpoint-now" in runs(r[1]) and stops(r[1])
                  and r[1].index(("run", "checkpoint-now")) < r[1].index(("stop", min(stops(r[1]))))
                  and any(ck_path in ln for ln in r[4].lines[-3:]))(
             scenario(0, record, running, api=_Billed(700.0))))
    case("LW19b", "at --max-hours: one final checkpoint, nothing stopped, the exit message names the checkpoint",
         lambda: (lambda r: r[0] == EXIT_LIMIT and "checkpoint-now" in runs(r[1]) and stops(r[1]) == []
                  and any(ck_path in ln for ln in r[4].lines[-3:]))(scenario(0, record, running, max_hours=0.0)))
    case("LW19c", "the final checkpoint is bounded: --checkpoint-minutes reaches the side call and the subprocess",
         lambda: (lambda r: ["checkpoint-now", "--timeout-s", "120"] in [t[:3] for t in r[3].tails]
                  and ("checkpoint-now", 180.0) in r[3].timeouts)(
             scenario(0, record, running, api=_Billed(700.0), checkpoint_s=120.0)))
    case("LW24", "(Codex P2-6) the final checkpoint names this watch's invocation and launch; the exit names the "
                 "pointer",
         lambda: (lambda r: ["checkpoint-now", "--timeout-s", "300", "--invocation", TOKEN, "--launch-id", "L1"]
                  in r[3].tails and any("outputs/runs/cfg-x/launch_L1/checkpoint_A.json" in ln for ln in r[4].lines))(
             scenario(0, record, running, api=_Billed(700.0))))
    case("LW19d", "a failed or timed-out final checkpoint never blocks the stop at the cap",
         lambda: (lambda r: r[0] == EXIT_CAP and sorted(stops(r[1])) == [100, 101, 102])(
             scenario(0, record, lambda cfg: {**running(cfg), "checkpoint-now": [(None, "TimeoutError")]},
                      api=_Billed(700.0))))
    # Owner rules in code (2026-11-11): dollar caps from cloud/budget.py's estimator, checkpoints, the signed budget
    case("LW20a", "the watch's cap is the smaller of --cap-usd and the config's cap_usd",
         lambda: (lambda r: r[4].cap == 40.0 and r[0] == EXIT_CAP)(
             scenario(0, record, running, api=_Billed(45.0), cfg_kw={"cap_usd": 40.0}))
         and (lambda r: r[4].cap == 650.0)(scenario(0, record, done_script, cfg_kw={"cap_usd": 900.0})))
    case("LW20b", "RunConfig: cap_usd must be positive when set; budget_scope is a name",
         lambda: _raises(lambda: cfg_of(tempfile.mkdtemp(), cap_usd=0), ValueError)
         and (lambda c: c.cap_usd is None and c.budget_scope == "")(rc.RunConfig(name="n", question_file="q")))

    def refused(cfg_kw, code, **kw):
        tmp, log = tempfile.mkdtemp(), []
        cfg = cfg_of(tmp, **cfg_kw)
        r = watch(cfg, Runner({}, log), Api(cfg, log), **kw).run()
        return r == code and "launch" not in runs(log)
    case("LW21a", "a v2 config with checkpoints off or over 30 minutes is refused before the launch (exit 9)",
         lambda: refused({"checkpoint_minutes": 0}, EXIT_NO_CHECKPOINTS)
         and refused({"checkpoint_minutes": 45}, EXIT_NO_CHECKPOINTS)
         and checkpoint_refusal(cfg_of(tempfile.mkdtemp(), checkpoint_minutes=30)) is None)
    case("LW21b", "--allow-no-checkpoints only for a smoke (run_timeout_hours <= 0.5)",
         lambda: refused({"checkpoint_minutes": 0}, EXIT_NO_CHECKPOINTS, allow_no_checkpoints=True)
         and checkpoint_refusal(cfg_of(tempfile.mkdtemp(), checkpoint_minutes=0, run_timeout_hours=0.5), True) is None
         and checkpoint_refusal(cfg_of(tempfile.mkdtemp(), checkpoint_minutes=0, run_timeout_hours=0.5)) is not None)
    fb = _FakeBudget(ok=False)
    case("LW22a", "the launch asks the budget for (scope, the config's cap_usd if set, else --cap-usd, name); a "
                  "refusal stops it (exit 10)",
         lambda: refused({"budget_scope": "f6", "cap_usd": 90.0}, EXIT_BUDGET, cap_usd=80.0, budget=fb)
         and fb.asked == [("f6", 90.0, "cfg-x")]
         and (lambda b: refused({"budget_scope": "f6"}, EXIT_BUDGET, cap_usd=80.0, budget=b)
              and b.asked == [("f6", 80.0, "cfg-x")])(_FakeBudget(ok=False)))

    def broken(exc, where):
        b = _FakeBudget()
        setattr(b, where, lambda *a, **k: (_ for _ in ()).throw(exc))
        return b
    case("LW22b", "fail closed: the budget module missing, not implemented or erring, in the check or the meter, "
                  "refuses the launch (exit 10)",
         lambda: all(refused({"budget_scope": "f6"}, EXIT_BUDGET, budget=broken(e, w))
                     for e in (NotImplementedError("stub"), RuntimeError("x")) for w in ("launch_budget_ok", "job_meter"))
         and refused({"budget_scope": "f6"}, EXIT_BUDGET, budget=_no_module)
         and refused({"budget_scope": ""}, EXIT_BUDGET, budget=_FakeBudget()))
    case("LW23", "--cap-usd is required; the credit cap and its ratio are gone",
         lambda: all(_raises(lambda a=a: _quiet(main, ["launch_watch.py", "cfg.py", *a]), SystemExit)
                     for a in ([], ["--cap", "650"], ["--cap-usd", "80", "--usd-per-credit", "0.1"],
                               ["--cap-usd", "80", "--credits-per-instance-hour", "8"])))

    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed: {', '.join(fails)}")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def _quiet(fn, *a):
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*a)


class _FakeBudget:
    """cloud/budget.py's interface (PR #192: launch_budget_ok(scope, cap_usd, name), job_meter(scope) -> fn(cid) ->
    USD), for the self-test: it never imports the stub or a real budget module. The meter reads the fake API."""
    def __init__(self, api=None, ok=True):
        self.api, self.ok, self.asked, self.metered = api, ok, [], []

    def launch_budget_ok(self, scope, cap_usd, name=None):
        self.asked.append((scope, cap_usd, name))
        return self.ok, "fake budget"

    def job_meter(self, scope):
        def meter(cid):
            self.metered.append(cid)
            return self.api.usd_of(cid) if self.api is not None else 0.0
        return meter


def _no_module():
    raise ImportError("No module named 'budget'")


class _FakeApi:
    """Base for the self-test's special listings (configured by scenario through cfg/log)."""
    def __init__(self):
        self.cfg, self.log, self.live, self.up, self.pre = None, None, None, False, []

    def bind(self, cfg, log, live):
        self.cfg, self.log, self.live = cfg, log, dict(live)
        return self

    def list_clusters(self):
        self.log.append(("list",))
        if not self.up:                         # before the launch: only what was live already
            return list(self.pre)
        return [{"name": n, "id": i, "current_state": {"state": "running"}, "created": "2026-11-10T00:00:05Z"}
                for i, n in self.live.items()]

    def stop(self, name, cid, why):
        self.log.append(("stop", cid))
        self.live.pop(cid, None)

    def usd_of(self, cid):                      # what the budget's meter reports for a cluster
        return 0.0


class _Sticky(_FakeApi):
    def stop(self, name, cid, why):
        self.log.append(("stop", cid))          # requested, but the cluster stays listed


class _Billed(_FakeApi):
    """The run cluster (id 100) has cost `usd`; one reading of it per check."""
    def __init__(self, usd):
        super().__init__()
        self.usd = usd

    def reading(self):
        pass

    def usd_of(self, cid):
        if cid != 100:
            return 0.0
        self.reading()
        return self.usd


class _Stray(_FakeApi):
    def bind(self, cfg, log, live):
        return super().bind(cfg, log, {**live, 999: cfg.keeper_name})   # created 00:00:05, after the launch


class _Foreign(_Billed):
    """A later cluster of the run's own name, created after the launch by another invocation."""
    def __init__(self, usd=0.0):
        super().__init__(usd)

    def bind(self, cfg, log, live):
        return super().bind(cfg, log, {**live, 888: cfg.name})


class _Both(_FakeApi):
    def bind(self, cfg, log, live):
        return super().bind(cfg, log, {**live, 888: cfg.name, 999: cfg.keeper_name})


class _MeterFails(_FakeApi):
    def usd_of(self, cid):
        raise OSError("cluster details unavailable")


class _Growing(_Billed):
    """Spend that passes the cap after the first reading."""
    def __init__(self):
        super().__init__(0.0)
        self.n = 0

    def reading(self):
        self.n += 1
        self.usd = 0.0 if self.n == 1 else 700.0


class _GrowingAt(_Billed):
    """Spend that passes the cap from the n-th reading on."""
    def __init__(self, n):
        super().__init__(0.0)
        self.n, self.k = n, 0

    def reading(self):
        self.k += 1
        self.usd = 700.0 if self.k >= self.n else 0.0


class _PreFailing(_FakeApi):
    def list_clusters(self):
        self.log.append(("list",))
        if not self.up:
            raise OSError("Temporary failure in name resolution")
        return super().list_clusters()


class _Failing(_FakeApi):
    def list_clusters(self):
        self.log.append(("list",))
        if not self.up:
            return []
        raise OSError("Temporary failure in name resolution")

    def usd_of(self, cid):
        raise OSError("Temporary failure in name resolution")


def main(argv) -> int:
    if argv[1:2] == ["--self-test"]:
        import no_cloud
        with no_cloud.refusing():          # no cloud call from a self-test (cloud/no_cloud.py)
            return _self_test()
    p = argparse.ArgumentParser(prog="launch_watch.py", description=__doc__.split("\n")[0], allow_abbrev=False)
    p.add_argument("config")
    p.add_argument("--cap-usd", type=float, required=True,
                   help="spend cap for this launch in dollars (required; the config's cap_usd, if lower, wins)")
    p.add_argument("--checkpoint-minutes", type=float, default=5.0,
                   help="the final checkpoint before a stop at the cap or the watch limit")
    p.add_argument("--interval", type=float, default=300.0, help="seconds between status checks")
    p.add_argument("--start-minutes", type=float, default=5.0, help="a run missing this long gets a relaunch")
    p.add_argument("--max-hours", type=float, default=None)
    p.add_argument("--allow-no-cloud-limits", action="store_true")
    p.add_argument("--allow-no-checkpoints", action="store_true",
                   help="launch a v2 config without checkpoints: only with run_timeout_hours <= 0.5 (smokes)")
    p.add_argument("--cap-collect-minutes", type=float, default=10.0, help="the collect attempt at the cap")
    p.add_argument("--launch-minutes", type=float, default=45.0, help="deadline of the launch subprocess")
    p.add_argument("--relaunch-attempts", type=int, default=3)
    p.add_argument("--collect-minutes", type=float, default=30.0, help="deadline of each collect attempt")
    p.add_argument("--collect-attempts", type=int, default=6)
    a = p.parse_args(argv[1:])
    if not a.cap_usd > 0:
        p.error("--cap-usd must be positive")
    cfg = rc.load_config(a.config)
    token = f"watch-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{secrets.token_hex(3)}"
    return Watch(cfg, a.config, token, shell_runner(os.path.abspath(a.config)), CoiledApi(), time.time, time.sleep,
                 cap_usd=a.cap_usd, checkpoint_s=a.checkpoint_minutes * 60, allow_no_checkpoints=a.allow_no_checkpoints,
                 interval_s=a.interval, start_s=a.start_minutes * 60, max_hours=a.max_hours,
                 allow_no_limits=a.allow_no_cloud_limits,
                 cap_collect_s=a.cap_collect_minutes * 60, launch_s=a.launch_minutes * 60,
                 relaunch_attempts=a.relaunch_attempts, collect_s=a.collect_minutes * 60,
                 collect_attempts=a.collect_attempts).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
