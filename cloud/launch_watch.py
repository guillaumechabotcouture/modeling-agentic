"""Launch one config with `cloud/run_cluster.py` and watch it to the end (F6 Stage 2).

    python cloud/launch_watch.py <config.py> [--cap CREDITS] [--interval S] [--start-minutes M]
                                 [--max-hours H] [--credits-per-instance-hour C] [--allow-no-cloud-limits]
    python cloud/launch_watch.py --self-test

Replaces the local, per-launch watchdog scripts (F5, 2026-11-04 to 11-09). Every name (cluster, keeper, overflow
pool, labels, out_dir) comes from the config. It runs `run_cluster.py <config> launch` itself, with
MA_LAUNCH_INVOCATION set to a fresh token, then:

  1. The launcher's exit code (run_cluster.py module doc). 0, 3, 4, 5: something was submitted, so nothing is
     stopped; it watches. On 5, or 3 with no keeper in clusters.json, it runs `run_cluster.py <config> keeper`.
     1 or any other code: it stops clusters only when clusters.json's `invocation` is this token and its `status` is
     "failed", and then exactly the ids it names, by id. Any other record (none, an earlier launch's, another
     status) stops nothing.
  2. Every --interval seconds: `status`. When every run is done, or the run cluster has stopped itself, it runs
     `collect` and confirms that every label is collected locally for this launch (`<out_dir>/<label>/result.json`
     with the launch id of this invocation's assignments; `collect` fetches from storage, or from the agent VM when
     the upload failed). Only then does it stop, by id, whatever of this launch is still live. `status` can print
     "done ... stored=None" while the agent VM holds the only copy: unconfirmed, nothing is stopped (exit 4).
  3. A credit cap (--cap, default 650): billed credits of this launch's clusters (by id from clusters.json) plus
     live recorded clusters x their instance count x hours since the launch x --credits-per-instance-hour (Coiled
     bills an instance when it stops). At the cap it attempts one collect, then stops every live cluster of the
     launch by id: the one case where stopping without a confirmed collect is right.
  4. A stray sweep: a live cluster with one of this launch's names, not a recorded id and created after the launch
     (an attach race), seen on two consecutive sweeps, is stopped by id.
  5. It refuses a config without cloud-side limits (run_timeout_hours None or cluster_timeout empty) unless
     --allow-no-cloud-limits is given: a watchdog on a laptop is not the only bound on spend.
  6. Network errors (DNS, TLS, timeouts) are retried; an unreadable status, listing or billing reading is
     "unknown", never "not running", and never leads to a stop.
A label that stays "missing" (its VM never started the run) for --start-minutes gets one `relaunch <label>`
(run_cluster.py refuses it while the scheduler holds the task); still missing after that: exit 2.

Exit codes:
  0 every run collected and confirmed for this launch, and no cluster of the launch left live
  1 the launch failed before submitting anything (this invocation's failed record's ids stopped, or nothing)
  2 a run never started, even after one relaunch (nothing stopped: check and shut down by hand)
  3 credit cap reached: clusters stopped by id (a collect was attempted first)
  4 stored or collected state not confirmed: the run cluster is left up (the agent VM may hold the only copy);
    run `collect` again
  5 every run collected, but a cluster of the launch is still listed live after the stop requests
  6 the watch reached --max-hours (default run_timeout_hours + hold_hours + 1): nothing stopped
  7 refused: the config has no cloud-side limits (see 5 above)
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
EXIT_UNCONFIRMED, EXIT_SHUTDOWN_UNCONFIRMED, EXIT_LIMIT, EXIT_REFUSED = 4, 5, 6, 7
SUBMITTED = (rc.EXIT_LAUNCHED, rc.EXIT_SUBMITTED_LOST, rc.EXIT_UNCONFIRMED, rc.EXIT_INCOMPLETE)   # 0, 3, 4, 5
TIMEOUTS = {"status": 600, "collect": 4 * 3600, "keeper": 1800, "relaunch": 1800}   # `launch` has none
_NET = re.compile(r"gaierror|nodename nor servname|name resolution|Name or service not known|getaddrinfo|"
                  r"Timed out trying to connect|tls://|\bSSL|\bTLS\b|ConnectTimeout|ReadTimeout|TimeoutError|"
                  r"timed out|Connection(Reset|Refused|Aborted)Error|Connection (reset|refused|aborted)|"
                  r"Network is unreachable|RemoteDisconnected|CommClosedError|StreamClosedError|Max retries exceeded")


# ---- decisions (pure) -----------------------------------------------------------------------

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


def strays(items, names, recorded_ids, launched_epoch) -> list:
    """(name, id) of live clusters with this launch's names, not recorded, created after the launch."""
    out = []
    for x in rc._items(items):
        t = rc._ts(x.get("created"))
        if (x.get("name") in names and x.get("id") not in recorded_ids and _live(x) and t is not None
                and t >= launched_epoch):
            out.append((x["name"], x["id"]))
    return sorted(out, key=lambda t: t[1])


def projected_credits(billed, live_roles, sizes, hours, rate) -> float:
    return float(billed) + sum(sizes.get(r, 1) for r in live_roles) * max(hours, 0.0) * rate


# ---- the watch (I/O through an injected runner and Coiled API) --------------------------------

class CoiledApi:
    """The Coiled calls the watch makes: the listing, a stop by id, the billing events. Bounded in time."""
    def list_clusters(self):
        import coiled
        return rc._items(rc._bounded(lambda: coiled.list_clusters(workspace=rc.WORKSPACE), timeout_s=120))

    def stop(self, name, cid, why):
        import vm_job
        rc._bounded(vm_job._stop_by_id, name, cid, why, timeout_s=120)

    def billing(self, start, end):
        return rc._bounded(rc.billing_events, start, end, timeout_s=300)


def shell_runner(cfg_path):
    """`run_cluster.py <cfg_path> <cmd>` as (exit code or None on a timeout, output); `launch` is streamed."""
    script = os.path.join(HERE, "run_cluster.py")

    def run(tail, env=None):
        cmd = [sys.executable, "-u", "-W", "ignore", script, cfg_path, *tail]
        if tail[0] == "launch":
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            out = []
            for line in p.stdout:
                print(line, end="", flush=True)
                out.append(line)
            return p.wait(), "".join(out)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=TIMEOUTS.get(tail[0], 1800))
        except subprocess.TimeoutExpired as e:
            return None, f"TimeoutError: {' '.join(tail)} did not return within {e.timeout} s"
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    return run


class Watch:
    def __init__(self, cfg, cfg_path, token, runner, api, clock, sleep, cap=650.0, interval_s=300.0,
                 start_s=300.0, max_hours=None, rate=8.0, allow_no_limits=False, retries=6, retry_s=60.0):
        self.cfg, self.cfg_path, self.token, self.runner, self.api = cfg, cfg_path, token, runner, api
        self.clock, self.sleep, self.cap, self.interval_s, self.start_s = clock, sleep, cap, interval_s, start_s
        self.max_hours = max_hours if max_hours is not None else (cfg.run_timeout_hours or 48.0) + cfg.hold_hours + 1
        self.rate, self.allow, self.retries, self.retry_s = rate, allow_no_limits, retries, retry_s
        self.names = {cfg.name, cfg.keeper_name, cfg.overflow_name}
        self.sizes = {"run": 1 + len(cfg.labels) + cfg.compute_start, "keeper": 2, "overflow": 1}
        self.env = {**os.environ, "MA_LAUNCH_INVOCATION": token}
        self.pending_strays = set()

    def log(self, msg):
        print(f"{time.strftime('%F %T')} {self.cfg.name}: {msg}", flush=True)

    def safe(self, fn, *a):
        """A Coiled call; any error is "unknown" (None), never a reading."""
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001
            self.log(f"{getattr(fn, '__name__', 'call')} unavailable ({type(e).__name__}: {str(e)[:120]})")
            return None

    def call(self, tail, retries=None):
        """run_cluster.py, retried while it fails with a network error or a timeout."""
        n = retries or self.retries
        for i in range(n):
            code, out = self.runner(tail, self.env)
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
        self.log(f"launch, invocation {self.token}")
        code, out = self.runner(["launch"], self.env)
        d = after_launch(code, self.raw_record(), self.token, self.cfg.keeper)
        self.log(f"launch exit {code}: {d['action']}" + (f", stopping {d['stop']}" if d["stop"] else ""))
        if d["action"] == "stop":
            for name, cid in d["stop"]:
                self.safe(self.api.stop, name, cid, f"launch failed (invocation {self.token})")
            return EXIT_LAUNCH_FAILED
        if d["keeper"]:
            kc, kout = self.call(["keeper"])
            self.log(f"keeper exit {kc}: {str(kout).strip()[-200:]}" + ("" if kc == 0 else
                     f" -- start it by hand: python cloud/run_cluster.py {self.cfg_path} keeper"))
        return self.watch()

    def watch(self) -> int:
        start, missing_since, relaunched = self.clock(), {}, set()
        while True:
            if self.clock() - start >= self.max_hours * 3600:
                self.log(f"watch limit {self.max_hours} h reached: nothing stopped")
                return EXIT_LIMIT
            rec = self.record()
            if rec is None:
                self.log("clusters.json is not this invocation's: no sweep, no credit cap, no stop by id")
            items = self.safe(self.api.list_clusters) if rec else None
            if items is not None:
                self.sweep(rec, items)
                credits = self.credits(rec, items)
                self.log(f"credits (billed + projected): {credits if credits is not None else 'unknown'}")
                if credits is not None and credits >= self.cap:
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
                return EXIT_NEVER_RAN
            if m:
                relaunched.add(m[1])
                missing_since[m[1]] = self.clock()
                rc_, rout = self.call(["relaunch", m[1]])
                self.log(f"relaunch {m[1]} exit {rc_}: {str(rout).strip()[-200:]}")
            self.sleep(self.interval_s)

    def sweep(self, rec, items):
        launched = rc._ts(rec.get("launched_utc"))
        if launched is None:
            return
        found = set(strays(items, self.names, {v["id"] for v in recorded(rec).values()}, launched))
        for name, cid in sorted(found & self.pending_strays, key=lambda t: t[1]):
            self.log(f"stray {name} ({cid}): stopping by id")
            self.safe(self.api.stop, name, cid, "stray cluster (attach race)")
        self.pending_strays = found          # each stray is stopped on every sighting after its first

    def credits(self, rec, items):
        roles, t0 = recorded(rec), rc._ts(rec.get("launched_utc"))
        if t0 is None:
            return None
        events = self.safe(self.api.billing, rec["launched_utc"],
                           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())))
        if events is None:
            return None
        billed = sum(v["credits"] for v in rc.summarize_billing(events, roles).values())
        live = {x.get("id") for x in rc._items(items) if _live(x)}
        return round(projected_credits(billed, [r for r, v in roles.items() if v["id"] in live], self.sizes,
                                       (self.clock() - t0) / 3600, self.rate), 1)

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
        """Stop, by id, every live recorded cluster and stray of this launch; True once none is listed live."""
        ids = {v["id"] for v in recorded(rec).values()}
        launched = rc._ts(rec.get("launched_utc"))
        for _ in range(self.retries):
            items = self.safe(self.api.list_clusters)
            if items is not None:
                live = {(x.get("name"), x.get("id")) for x in rc._items(items) if _live(x) and x.get("id") in ids}
                live |= set(strays(items, self.names, ids, launched)) if launched is not None else set()
                if not live:
                    return True
                for name, cid in sorted(live, key=lambda t: t[1]):
                    self.safe(self.api.stop, name, cid, why)
            self.sleep(self.retry_s)
        return False

    def finish(self, rec) -> int:
        ok, bad = False, list(self.cfg.labels)
        for _ in range(self.retries):
            code, out = self.call(["collect"])
            self.log(f"collect exit {code}: {str(out).strip()[-300:]}")
            ok, bad = self.collected()
            if ok:
                break
            self.sleep(self.retry_s)
        if not ok:
            self.log(f"not confirmed collected for this launch: {bad}; the run cluster is left up. Run "
                     f"`python cloud/run_cluster.py {self.cfg_path} collect` again")
            return EXIT_UNCONFIRMED
        if rec is None or not self.stop_all(rec, "every run collected"):
            self.log("every run collected, but a cluster of this launch may still be live: check the listing")
            return EXIT_SHUTDOWN_UNCONFIRMED
        self.log("every run collected; no cluster of this launch is live")
        return EXIT_DONE

    def at_cap(self, rec) -> int:
        self.log(f"credit cap {self.cap} reached: one collect, then stopping every cluster of this launch by id")
        code, out = self.call(["collect"], retries=2)
        self.log(f"collect exit {code}: {str(out).strip()[-300:]}; collected: {self.collected()}")
        if not self.stop_all(rec, f"credit cap {self.cap}"):
            self.log("a cluster of this launch may still be live after the stop requests: check the listing")
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

        def __call__(self, tail, env=None):
            self.log.append(("run", tail[0]))
            self.envs.append(dict(env or {}))
            seq = self.script.get(tail[0]) or [(0, "")]
            r = seq.pop(0) if len(seq) > 1 else seq[0]
            return r() if callable(r) else r

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
        def go():
            for lb in labels or cfg.labels:
                os.makedirs(os.path.join(cfg.out_dir, lb), exist_ok=True)
                json.dump({"launch_id": "L1"}, open(os.path.join(cfg.out_dir, lb, "result.json"), "w"))
            return 0, "every run is collected"
        return go

    def watch(cfg, runner, api, **kw):
        clock, sleep = fake_time()
        return Watch(cfg, "cfg.py", TOKEN, runner, api, clock, sleep, retry_s=1.0, **kw)

    def scenario(launch_rc, rec, script=None, live=None, name="cfg-x", labels=("A", "B"), **kw):
        """A full run of the watchdog against fakes: (exit code, call log, cfg, runner)."""
        tmp = tempfile.mkdtemp()
        cfg = cfg_of(tmp, name=name, labels=labels)
        log = []
        r = rec(cfg) if callable(rec) else rec

        def launch():
            if r is not None:
                write_record(cfg, r)
            write_assign(cfg)
            return launch_rc, f"invocation {TOKEN}\n"
        sc = {"launch": [launch], **(script(cfg) if script else {})}
        runner = Runner(sc, log)
        live = live if live is not None else ({v["id"]: v["name"] for v in r.values() if isinstance(v, dict)}
                                              if r else {})
        api = kw.pop("api", None)
        api = api.bind(cfg, log, live) if api is not None else Api(cfg, log, live=live)
        return watch(cfg, runner, api, **kw).run(), log, cfg, runner

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
    case("LW1b", "launch exit 5: watch and start the keeper",
         lambda: (lambda d: d["action"] == "watch" and d["keeper"] and not d["stop"])(after_launch(5, record(c), TOKEN)))
    case("LW1c", "launch exit 3 with no keeper recorded: start it; with one recorded: do not",
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
    case("LW1k", "end to end: exit 5 runs `keeper` before watching",
         lambda: (lambda r: r[0] == EXIT_DONE and runs(r[1])[:3] == ["launch", "keeper", "status"])(
             scenario(5, record, done_script)))

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
                                              "collect": [lambda: (collected(cfg)(), [json.dump(
                                                  {"launch_id": "L0"}, open(os.path.join(cfg.out_dir, lb,
                                                                                         "result.json"), "w"))
                                                  for lb in cfg.labels], (0, ""))[-1]]})))
    case("LW2g", "run cluster stopped by itself: collect from storage, confirm, exit 0; never `shutdown` by name",
         lambda: (lambda r: r[0] == EXIT_DONE and "shutdown" not in runs(r[1]) and "collect" in runs(r[1]))(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {}, cluster="stopped"))],
                                              "collect": [collected(cfg)]}, live={})))
    case("LW2h", "collected but a cluster stays live after the stop requests: exit 5",
         lambda: (lambda r: r[0] == EXIT_SHUTDOWN_UNCONFIRMED)(
             scenario(0, record, done_script, api=_Sticky())))

    # 3. credit cap
    case("LW3a", "projected_credits: billed plus live instances x hours x rate",
         lambda: abs(projected_credits(100.0, ["run", "keeper"], {"run": 4, "keeper": 2}, 2.0, 8.0) - 196.0) < 1e-9)
    case("LW3b", "at the cap: a collect is attempted, then every live recorded cluster is stopped by id, exit 3",
         lambda: (lambda r: r[0] == EXIT_CAP and sorted(stops(r[1])) == [100, 101, 102]
                  and r[1].index(("run", "collect")) < r[1].index(next(x for x in r[1] if x[0] == "stop")))(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "running", "B": "running"}))],
                                              "collect": [(0, "A: still running")]}, cap=650.0,
                      api=_Billed(700.0))))
    case("LW3c", "below the cap the watch goes on (billing by recorded id only)",
         lambda: (lambda r: r[0] == EXIT_DONE)(scenario(0, record, done_script, cap=650.0, api=_Billed(10.0))))

    # 4. stray sweep
    t0 = rc._ts("2026-11-10T00:00:00Z")
    items = [{"name": "n", "id": 1, "current_state": {"state": "running"}, "created": "2026-11-10T00:05:00Z"},
             {"name": "n", "id": 2, "current_state": {"state": "running"}, "created": "2026-11-10T00:05:00Z"},
             {"name": "n", "id": 3, "current_state": {"state": "running"}, "created": "2026-11-09T23:00:00Z"},
             {"name": "n", "id": 4, "current_state": {"state": "stopped"}, "created": "2026-11-10T00:05:00Z"},
             {"name": "other", "id": 5, "current_state": {"state": "running"}, "created": "2026-11-10T00:05:00Z"},
             {"name": "n", "id": 6, "current_state": {"state": "running"}, "created": "garbage"}]
    case("LW4a", "strays: live, this launch's name, unrecorded, created after the launch; nothing else",
         lambda: strays(items, {"n"}, {2}, t0) == [("n", 1)])
    case("LW4b", "watch: a stray is stopped by id only on its second sighting; recorded ids are not",
         lambda: (lambda r: 999 in stops(r[1]) and r[1].index(("run", "status")) < r[1].index(("stop", 999))
                  < r[1].index(("run", "collect")) and runs(r[1]).count("status") == 3)(
             scenario(0, record, lambda cfg: {"status": [(0, status_text(cfg, {"A": "running", "B": "running"})),
                                                         (0, status_text(cfg, {"A": "running", "B": "running"})),
                                                         (0, status_text(cfg, {"A": "done", "B": "done"}))],
                                              "collect": [collected(cfg)]}, api=_Stray())))

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

    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed: {', '.join(fails)}")
        return 1
    print("OK: all self-test cases passed.")
    return 0


class _FakeApi:
    """Base for the self-test's special listings (configured by scenario through cfg/log)."""
    def __init__(self):
        self.cfg, self.log, self.live = None, None, None

    def bind(self, cfg, log, live):
        self.cfg, self.log, self.live = cfg, log, dict(live)
        return self

    def list_clusters(self):
        self.log.append(("list",))
        return [{"name": n, "id": i, "current_state": {"state": "running"}, "created": "2026-11-10T00:00:05Z"}
                for i, n in self.live.items()]

    def stop(self, name, cid, why):
        self.log.append(("stop", cid))
        self.live.pop(cid, None)

    def billing(self, start, end):
        self.log.append(("billing",))
        return []


class _Sticky(_FakeApi):
    def stop(self, name, cid, why):
        self.log.append(("stop", cid))          # requested, but the cluster stays listed


class _Billed(_FakeApi):
    def __init__(self, credits):
        super().__init__()
        self.credits = credits

    def billing(self, start, end):
        self.log.append(("billing",))
        return [{"cluster_id": 100, "amount_credits": -self.credits, "instance": "i-1"},
                {"cluster_id": 555, "amount_credits": -10000.0, "instance": "i-x"}]   # not this launch's


class _Stray(_FakeApi):
    def bind(self, cfg, log, live):
        return super().bind(cfg, log, {**live, 999: cfg.keeper_name})   # created 00:00:05, after the launch


class _Failing(_FakeApi):
    def list_clusters(self):
        self.log.append(("list",))
        raise OSError("Temporary failure in name resolution")

    def billing(self, start, end):
        raise OSError("Temporary failure in name resolution")


def main(argv) -> int:
    if argv[1:2] == ["--self-test"]:
        import no_cloud
        with no_cloud.refusing():          # no cloud call from a self-test (cloud/no_cloud.py)
            return _self_test()
    p = argparse.ArgumentParser(prog="launch_watch.py", description=__doc__.split("\n")[0])
    p.add_argument("config")
    p.add_argument("--cap", type=float, default=650.0, help="credit cap for this launch (default 650)")
    p.add_argument("--interval", type=float, default=300.0, help="seconds between status checks")
    p.add_argument("--start-minutes", type=float, default=5.0, help="a run missing this long gets one relaunch")
    p.add_argument("--max-hours", type=float, default=None)
    p.add_argument("--credits-per-instance-hour", type=float, default=8.0)
    p.add_argument("--allow-no-cloud-limits", action="store_true")
    a = p.parse_args(argv[1:])
    cfg = rc.load_config(a.config)
    token = f"watch-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{secrets.token_hex(3)}"
    return Watch(cfg, a.config, token, shell_runner(os.path.abspath(a.config)), CoiledApi(), time.time, time.sleep,
                 cap=a.cap, interval_s=a.interval, start_s=a.start_minutes * 60, max_hours=a.max_hours,
                 rate=a.credits_per_instance_hour, allow_no_limits=a.allow_no_cloud_limits).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
