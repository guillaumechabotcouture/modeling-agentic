"""Launch one config with `cloud/run_cluster.py` and watch it to the end (F6 Stage 2).

    python cloud/launch_watch.py <config.py> [--cap CREDITS] [--interval S] [--start-minutes M]
                                 [--max-hours H] [--credits-per-instance-hour C] [--allow-no-cloud-limits]
    python cloud/launch_watch.py --self-test

Stub (α): the self-test cases are written first and fail until β implements the functions.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_cluster as rc  # noqa: E402

EXIT_DONE, EXIT_LAUNCH_FAILED, EXIT_NEVER_RAN, EXIT_CAP = 0, 1, 2, 3
EXIT_UNCONFIRMED, EXIT_SHUTDOWN_UNCONFIRMED, EXIT_LIMIT, EXIT_REFUSED = 4, 5, 6, 7


def refuse_reason(cfg, allow_no_limits=False):
    raise NotImplementedError


def after_launch(code, record, token, keeper_wanted=True) -> dict:
    raise NotImplementedError


def is_network_error(text) -> bool:
    raise NotImplementedError


def parse_status(text, labels):
    raise NotImplementedError


def strays(items, names, recorded_ids, launched_epoch) -> list:
    raise NotImplementedError


def projected_credits(billed, live_roles, sizes, hours, rate) -> float:
    raise NotImplementedError


def decide(st) -> str:
    raise NotImplementedError


def missing_action(st, missing_since, relaunched, now, window_s):
    raise NotImplementedError


class Watch:
    def __init__(self, cfg, cfg_path, token, runner, api, clock, sleep, cap=650.0, interval_s=300.0,
                 start_s=300.0, max_hours=None, rate=8.0, allow_no_limits=False, retries=6, retry_s=60.0):
        raise NotImplementedError


# ---- self-test ------------------------------------------------------------------------------

def _self_test() -> int:
    import tempfile
    import traceback
    fails = []

    def case(cid, what, fn):
        try:
            good = bool(fn())
        except Exception as e:  # noqa: BLE001 — a stub or a bug: the case fails
            good = False
            what += f" [{type(e).__name__}: {str(e)[:80]}]"
            if os.environ.get("MA_TRACE"):
                traceback.print_exc()
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

    class Api:
        def __init__(self, cfg, log, live=None, events=(), list_error=None, extra=()):
            self.cfg, self.log, self.live, self.events = cfg, log, dict(live or {}), list(events)
            self.list_error, self.extra = list_error, list(extra)

        def list_clusters(self):
            self.log.append(("list",))
            if self.list_error:
                raise self.list_error
            return [{"name": n, "id": i, "current_state": {"state": "running"}, "created": "2026-11-10T00:00:05Z"}
                    for i, n in self.live.items()] + self.extra

        def stop(self, name, cid, why):
            self.log.append(("stop", cid))
            self.live.pop(cid, None)

        def billing(self, start, end):
            self.log.append(("billing",))
            return self.events

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
    case("LW4b", "watch: a stray seen on two sweeps is stopped by id; recorded ids are not",
         lambda: (lambda r: 999 in stops(r[1]) and r[1].index(("stop", 999)) < r[1].index(("run", "collect")))(
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
    def list_clusters(self):
        return super().list_clusters() + ([{"name": self.cfg.keeper_name, "id": 999, "created": "2026-11-10T00:06:00Z",
                                            "current_state": {"state": "running"}}] if 999 not in self.gone else [])

    gone = set()

    def stop(self, name, cid, why):
        super().stop(name, cid, why)
        self.gone = self.gone | {cid}


class _Failing(_FakeApi):
    def list_clusters(self):
        self.log.append(("list",))
        raise OSError("Temporary failure in name resolution")

    def billing(self, start, end):
        raise OSError("Temporary failure in name resolution")


def main(argv) -> int:
    if argv[1:2] == ["--self-test"]:
        import no_cloud
        with no_cloud.refusing():
            return _self_test()
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
