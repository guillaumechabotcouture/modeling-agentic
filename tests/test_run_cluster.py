#!/usr/bin/env python3
"""Tests extracted from cloud/run_cluster.py."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import types
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
CLOUD_DIR = os.path.join(REPO, "cloud")
if CLOUD_DIR not in sys.path:
    sys.path.insert(0, CLOUD_DIR)
SCRIPTS_LIB = os.path.join(REPO, "scripts", "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import coiled_ops as CO
import eval_paths
import no_cloud
import pipeline_id
import run_cluster as rc_mod
import worker_side as ws
from run_cluster import (
    ACK_ATTEMPTS, ACK_DELAY_S, ACK_STATES, CHUNK, CONNECTION_ERRORS, ENV_FILE,
    EXIT_FAILED, EXIT_INCOMPLETE, EXIT_LAUNCHED, EXIT_SUBMITTED_LOST, EXIT_UNCONFIRMED,
    INVOCATION_TAG, JOURNAL, KEEPER_FRESH_S, KEEPER_TICK, LIVE_STATES, OWN_SKEW_S,
    PRE_SIGN_STATES, RecordOwned, RunConfig, SIGNOFF_RECORDS, StoredRunRefused,
    TEST_ENGINE_REFS, WORKSPACE, _ATTACH, _ATTACHED, _CREATED, _WASTED,
    _acknowledged, _archive_member, _atomic_json, _bounded, _checkpoint_run_root,
    _coiled_events, _compute, _connect_recorded, _construct, _default_scope,
    _download, _download_json, _find_ledgers, _fresh, _handover, _identity_note,
    _invocation, _items, _journal, _launch_locked, _ledger_ts, _lifetime, _on_worker, _outcome,
    _preconditions, _reconcile, _release, _results_store, _run_identity_on_disk,
    _run_stopped, _scheduler_state, _sharers, _side, _signed_run_problems,
    _signoff_record, _stop_id, _submit, _tar_identity, _task_key, _ts,
    _window_start, acquire_flag_problems, adapt_options, api_keys, archive_commit,
    attach, billing_events, budget_guard, checkpoint_now, checkpoint_record,
    cluster_states, code_tarball, collect, collect_from_storage, confirm_keeper,
    connect_existing, cost_lines, existing_client, fetch_checkpoint, is_running,
    keeper_fresh, keeper_later, keeper_lines, keeper_wait, launch, launch_record,
    ledger_hours, live_id, load_config, local_is_current, main as rc_main, main_flags,
    parse_sign_args, pipeline_preflight, read_journal, read_keys, recorded_labels,
    refuse_hidden, refuse_hidden_tree, relaunch, relaunch_refusal, report_cost,
    reuse_check, seconds, shutdown, stale_sheet_problems, start_helpers,
    start_keeper, start_overflow, status, summarize_billing, vm_request,
    wait_listed, write_clusters, ws_sha256,
)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.parse_args(argv)
    with no_cloud.refusing():
        return _self_test()


@contextlib.contextmanager
def _patch_rc(mapping):
    with contextlib.ExitStack() as es:
        for k, v in mapping.items():
            if hasattr(rc_mod, k):
                es.enter_context(mock.patch.object(rc_mod, k, v))
            if hasattr(CO, k):
                es.enter_context(mock.patch.object(CO, k, v))
        yield


E7R7_REFUSAL = ("Instance Stopped: Failed to create VM -- (OperationNotAllowed) Operation could not be completed as "
                "it results in exceeding approved standardEv5Family Cores quota. Additional details - Deployment "
                "Model: Resource Manager, Location: westus2, Current Limit: 350, Current Usage: 344, Additional "
                "Required: 8, (Minimum) New Limit Required: 352. Setup Alerts when Quota reaches threshold.")


def _self_test_42b(ok, RunConfig) -> None:
    ok(seconds("15s") == 15 and seconds("10 minutes") == 600 and seconds("1 hour") == 3600 and seconds(90) == 90
       and seconds("2.5m") == 150 and seconds("30") == 30, "RC23 durations parse in the forms dask and Coiled accept")
    for bad in ("ten minutes", "5 fortnights", ""):
        try:
            seconds(bad)
            ok(False, f"RC23 rejects {bad!r}")
        except ValueError:
            ok(True, f"RC23 rejects {bad!r}")
    cfg = RunConfig(name="t", question_file="/dev/null")
    a = adapt_options(cfg, {"target_duration": "20s"})
    ok(a == {"target_duration": "20s", "interval": "15s", "wait_count": 40},
       f"RC24 the default scale-down delay is 10 min (15 s x 40 re-plans), target_duration kept: {a}")
    a = adapt_options(RunConfig(name="t", question_file="/dev/null", scale_down_after="2 minutes", adapt_interval="5s",
                                target_duration="3m"), {"target_duration": "20s"})
    ok(a == {"target_duration": "3m", "interval": "5s", "wait_count": 24}, f"RC24b config overrides apply: {a}")
    ok(cfg.overflow_max == cfg.compute_max and cfg.overflow_name == "t-overflow"
       and RunConfig(name="t", question_file="/dev/null", overflow_max=0).overflow_max == 0,
       "RC25 the overflow pool defaults to compute_max; 0 disables it")
    for bad, why in ((dict(scale_down_after="soon"), "a bad delay"), (dict(adapt_interval="0s"), "a zero interval"),
                     (dict(overflow_vm_types=()), "overflow without VM types")):
        try:
            RunConfig(name="t", question_file="/dev/null", **bad)
            ok(False, f"RC25 rejects {why}")
        except ValueError:
            ok(True, f"RC25 rejects {why}")
    # 2026-11-01 (local check against distributed 2026.8.0): with no worker carrying the compute resource, compute.py's
    # queued chunks and demand markers leave adaptive_target at 1 (their duration is unknown until a chunk has run),
    # so a pool that starts with no compute VM never grows and the batch waits forever.
    try:
        RunConfig(name="t", question_file="/dev/null", compute_start=0, compute_env={"MA_COMPUTE_RESOURCE": "compute"})
        ok(False, "RC25b a resource-tagged pool with no compute VM at start is refused (it cannot scale up)")
    except ValueError as e:
        ok("compute_start" in str(e), f"RC25b a resource-tagged pool with no compute VM at start is refused: {e}")
    ok(RunConfig(name="t", question_file="/dev/null", compute_start=1,
                 compute_env={"MA_COMPUTE_RESOURCE": "compute"}).compute_start == 1,
       "RC25c one compute VM at start is accepted")
    q = ws.quota_refusal(E7R7_REFUSAL)
    ok(q == {"family": "standardEv5Family", "limit": 350, "usage": 344, "required": 8},
       f"RC26 the attempt-7 quota refusal parses: {q}")
    ok(ws.quota_refusal("Instance Stopped: Instance did not phone home before timeout") is None
       and ws.quota_refusal("(SkuNotAvailable) The requested VM size is currently not available") is None
       and ws.quota_refusal(None) is None, "RC26b other worker errors are not quota refusals")
    listing = [{"id": 1, "name": "w1", "current_state": {"state": "error", "reason": E7R7_REFUSAL,
                                                         "created": "2026-10-27T06:00:00+00:00"}},
               {"id": 2, "name": "w2", "current_state": {"state": "stopped", "reason": E7R7_REFUSAL}},
               {"id": 3, "name": "w3", "current_state": {"state": "error", "reason": "Dask process stopped"}},
               {"id": 4, "name": "w4", "current_state": {"state": "error", "reason": E7R7_REFUSAL.replace(
                   "standardEv5Family", "standardESv5Family"), "created": "2026-10-27T05:00:00Z"}}]
    r = ws.quota_refusals(listing)
    ok([x["id"] for x in r] == [4, 1] and r[0]["family"] == "standardESv5Family" and r[0]["at"] < r[1]["at"],
       f"RC27 refusals come from error-state workers only, oldest first: {[x['id'] for x in r]}")
    ok([x["id"] for x in ws.quota_refusals(listing, seen={4})] == [1], "RC27b a refusal already counted is skipped")
    t_upd = ws.quota_refusals([{"id": 5, "current_state": {"state": "error", "reason": E7R7_REFUSAL,
                                                           "updated": "2026-10-27T07:00:00+00:00"}}])[0]["at"]
    t_none = ws.quota_refusals([{"id": 6, "current_state": {"state": "error", "reason": E7R7_REFUSAL}}], now=123.0)[0]["at"]
    ok(t_upd == ws._epoch("2026-10-27T07:00:00+00:00") and t_none == 123.0,
       "RC27c a refusal is dated by its state's updated time, or the scan time; never epoch 0")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        c2 = RunConfig(name="t", question_file="/dev/null", out_dir=d, labels=("R1", "R2"))
        json.dump({"R1": {"launch_id": "L2"}, "R2": {"launch_id": "L2"}}, open(c2.state_file, "w"))
        for sub, lid in (("R1", "L2"), ("R1.before_L1_9", "L1"), ("R2", "L1")):
            os.makedirs(os.path.join(d, sub, "runs", "r", "models"))
            open(os.path.join(d, sub, "runs", "r", "models", "compute_ledger.json"), "w").write("{}")
            json.dump({"launch_id": lid}, open(os.path.join(d, sub, "result.json"), "w"))
        got = [os.path.relpath(x, d) for x in _find_ledgers(c2)]
        ok(got == [os.path.join("R1", "runs", "r", "models", "compute_ledger.json")],
           f"RC32b report-cost reads only this launch's ledgers, never an earlier launch's copy: {got}")
    now = 10_000.0
    ok(ws.overflow_desired(20, 5, 24, None, now, 1800) == 0 and ws.overflow_desired(20, 5, 24, now - 60, now, 1800) == 15
       and ws.overflow_desired(40, 5, 24, now - 60, now, 1800) == 24
       and ws.overflow_desired(20, 5, 24, now - 3600, now, 1800) == 0
       and ws.overflow_desired(4, 5, 24, now - 60, now, 1800) == 0 and ws.overflow_desired(20, 5, 0, now, now, 1800) == 0,
       "RC28 overflow wanted = the shortfall, capped, only while refusals are recent")
    p = ws.overflow_plan(3, 0, {}, {}, now, 600)
    ok(p["scale_to"] == 3 and not p["join"] and not p["close"], f"RC29 an empty overflow pool grows: {p}")
    vms = {"a": {"joined": True, "busy": True}, "b": {"joined": False}, "c": {"joined": False}}
    p = ws.overflow_plan(2, 3, vms, {}, now, 600)
    ok(p["scale_to"] is None and p["join"] == ["b"] and not p["close"] and set(p["idle_since"]) == {"c"},
       f"RC29b booted VMs join up to the wanted count; the spare starts its idle clock: {p}")
    p = ws.overflow_plan(0, 3, vms, {"b": now - 700, "c": now - 100, "a": now - 9999}, now, 600)
    ok(p["close"] == ["b"] and "a" not in p["idle_since"] and p["idle_since"].get("c") == now - 100,
       f"RC29c only VMs idle past the delay close, never a busy one: {p}")
    ok(ws.overflow_plan(1, 1, {"a": {"joined": True, "busy": False}}, {"a": now - 9999}, now, 600)["close"] == [],
       "RC29d a VM the wanted count still needs is kept even when idle")
    ev = [{"cluster": "t", "cluster_id": 7, "instance": "t-worker-1-instance", "amount_credits": "-1.5",
           "event_datetime": "2026-10-28T21:00:00Z"},
          {"cluster": "t", "cluster_id": 7, "instance": "t-scheduler-1-instance", "amount_credits": "-0.5"},
          {"cluster": "t", "cluster_id": 3, "instance": "t-worker-9-instance", "amount_credits": "-99"},
          {"cluster": "t-keeper", "cluster_id": 8, "instance": "t-keeper-worker-1-instance", "amount_credits": "-0.25"},
          {"cluster": "other", "cluster_id": 9, "instance": "o", "amount_credits": "-5"}]
    b = summarize_billing(ev, {"run": {"name": "t", "id": 7}, "keeper": {"name": "t-keeper", "id": None}})
    ok(b["run"]["credits"] == 2.0 and b["run"]["instances"] == 2 and b["run"]["workers"] == 1
       and b["keeper"]["credits"] == 0.25, f"RC30 billing is summed per cluster id; a reused name's other launch is excluded: {b}")
    pages = {1: {"results": [1, 2], "next": "p2"}, 2: {"results": [3], "next": None}}
    ok(billing_events("a", "b", lambda pg: pages[pg]) == [1, 2, 3], "RC31 billing pages are all read")
    led = {"core_seconds": 3600 * 410.0, "calls": [
        {"n": 10, "n_failed": 0, "task_core_s": 3600 * 2.0, "ts": "2026-10-28T21:00:00"},
        {"n": 10, "n_failed": 5, "task_core_s": 3600 * 4.0, "ts": "2026-10-28T22:00:00"},
        {"n": 864, "n_failed": 864, "task_core_s": 3600 * 3.0, "stopped": "interrupted: KeyboardInterrupt"},
        {"task_core_s": 3600 * 400.0, "stopped": "caller died; charged its reservation", "ts": "2026-10-28T23:00:00"},
        {"n": 5, "n_failed": 0, "task_core_s": 3600 * 1.0, "stopped": "run compute cap reached", "ts": "x"}]}
    h = ledger_hours(led)
    ok(h["charged"] == 410.0 and h["useful"] == 7.0 and h["calls"] == 5 and h["failed_rows"] == 5,
       f"RC32 useful core-hours leave out interrupted calls and charged reservations: {h}")
    hw = ledger_hours(led, ws._epoch("2026-10-28T21:30:00Z"), ws._epoch("2026-10-28T23:30:00Z"))
    ok(hw["useful"] == 4.0 and hw["charged"] == 404.0 and hw["calls"] == 2,
       f"RC32c with a billing window only the batches that ended inside it count: {hw}")
    lines = cost_lines({"run": {**b["run"], "credits": 20.0}, "keeper": b["keeper"]}, h, 10.0)
    ok(any("credits per useful core-hour: 2.89" in x for x in lines) and any("workers created per hour: 0.1" in x
                                                                             for x in lines),
       f"RC33 the report gives credits per useful core-hour and VMs created per hour: {lines}")
    ok(any("unknown" in x for x in cost_lines(b, None, 1.0)), "RC33b without a ledger the ratio is reported unknown")
    st = {"updated": now - 30, "workers": 9, "overflow_workers": 3, "target": 12, "pool": [2, 25], "active": "primary+overflow",
          "primary_vm_types": ["Standard_E8s_v5"], "overflow": {"name": "t-overflow", "id": 5, "max": 24,
                                                                  "vm_types": ["Standard_D8s_v5"]},
          "refusal_count": 4, "refusals": [{"at": now - 120, "family": "standardESv5Family", "usage": 348, "limit": 350,
                                             "required": 8}], "refusal_scan": "ok", "errors": []}
    kl = "\n".join(keeper_lines(st, now))
    ok("primary+overflow" in kl and "quota refusals: 4; last 2 min ago: standardESv5Family 348/350" in kl
       and "updated 30s ago" in kl, f"RC33c status prints the active family and the refusals: {kl}")
    ok("none yet" in keeper_lines(None)[0], "RC33d status says when the keeper has written no status yet")


def _self_test_52(ok) -> None:
    """The keeper's `KeyError: 'workers'` race (no Coiled, no network). distributed's
    Client.scheduler_info() returns {} when the client is not connected: before its first
    identity reply, while reconnecting, and after a failed reconnect closes it. v2blind1
    (2026-10-31) crashed at the end of `launch` on it, before clusters.json recorded the keeper."""
    import contextlib
    import io
    import tempfile
    import types

    class FakeClient:
        def __init__(self, infos, status="running", log="12:00:00 keeper holding x 3..9 {}\n12:00:01 workers=3 (overflow 0) refusals=0 active=primary"):
            self.infos, self.status, self.log, self.calls = list(infos), status, log, 0

        def scheduler_info(self):
            self.calls += 1
            return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

        def run(self, fn, workers):
            if getattr(fn, "__name__", "") == "keeper_health":   # a fresh status beside the log
                return {w: {"log": self.log, "status": {"updated": 100.0}, "now": 110.0} for w in workers}
            return {w: self.log for w in workers}

    table = {"tls://10.0.0.5:1": {"name": "a"}, "tls://10.0.0.6:1": {"name": "b"}}
    sleeps = []
    c = FakeClient([{}, {"id": "s"}, {"workers": table}])
    got = ws.scheduler_workers(lambda: c, attempts=5, delay_s=2.0, sleep=sleeps.append)
    ok(got == table and sleeps == [2.0, 4.0],
       f"RC52a the worker table is read once the scheduler has populated it, with doubling backoff: {sleeps}")
    sleeps.clear()
    try:
        ws.scheduler_workers(lambda: FakeClient([{}]), attempts=4, delay_s=5.0, max_delay_s=8.0, sleep=sleeps.append)
        ok(False, "RC52b a worker table that never appears is refused after the bound")
    except ws.SchedulerInfoUnavailable as e:
        ok(sleeps == [5.0, 8.0, 8.0] and "4 tries" in str(e) and "no worker table" in str(e),
           f"RC52b a worker table that never appears is refused after the bound, naming why: {e} {sleeps}")
    c = FakeClient([{"workers": {}}, {"workers": table}])
    got = ws.scheduler_workers(lambda: c, attempts=3, delay_s=0.0, sleep=sleeps.append, min_workers=1)
    ok(got == table, "RC52c min_workers: an empty table (no worker joined yet) is waited out")
    closed = FakeClient([{}], status="closed")
    fresh = FakeClient([{"workers": table}])
    ok(ws.live_client(types.SimpleNamespace(get_client=lambda: fresh), closed) is fresh
       and ws.live_client(types.SimpleNamespace(get_client=lambda: closed), fresh) is fresh,
       "RC52d a closed client is replaced from its cluster; an open one is kept")

    # v2blind1's sequence: the keeper cluster's client failed to reconnect ("closing client"),
    # so scheduler_info() returned {}; `launch` died with KeyError: 'workers'.
    calls = iter([closed, fresh])
    kc = types.SimpleNamespace(cluster_id=2307781, get_client=lambda: next(calls, fresh))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        line = confirm_keeper("x-keeper", kc, attempts=3, delay_s=0.0, sleep=lambda s: None)
    ok("keeper holding" in out.getvalue() or "workers=3" in out.getvalue(),
       f"RC52e replay v2blind1: a closed keeper client is replaced and the keeper's log line printed: {line}")
    kc2 = types.SimpleNamespace(cluster_id=9, get_client=lambda: FakeClient([{}], status="closed"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            confirm_keeper("x-keeper", kc2, attempts=2, delay_s=0.0, sleep=lambda s: None)
        ok(False, "RC52f a keeper that cannot be confirmed fails the launch with a clear message")
    except SystemExit as e:
        ok("x-keeper" in str(e) and "not confirmed" in str(e) and "keeper-log" in str(e),
           f"RC52f a keeper that cannot be confirmed fails the launch with a clear message: {e}")

    # Codex round 1: a readable log is not proof; only a tick line (written after adapt()
    # and a worker-table read) confirms the keeper is holding the pool.
    for log in ("(no log yet)", "12:00:00 keeper holding x 3..9 {}\n12:00:01 keeper error OSError: no; retrying"):
        kc3 = types.SimpleNamespace(cluster_id=10, get_client=lambda log=log: FakeClient([{"workers": table}], log=log))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                confirm_keeper("x-keeper", kc3, attempts=2, delay_s=0.0, sleep=lambda s: None)
            ok(False, f"RC52h a keeper log with no tick line is not confirmation: {log[-40:]!r}")
        except SystemExit as e:
            ok("not confirmed" in str(e), f"RC52h a keeper log with no tick line is not confirmation: {log[-40:]!r}")
    late = FakeClient([{"workers": table}], log="(no log yet)")
    logs = iter(["(no log yet)", "12:00:00 keeper holding x 3..9 {}",
                 "12:00:00 keeper holding x 3..9 {}\n12:01:00 workers=3 (overflow 0) refusals=0 active=primary"])
    late.run = lambda fn, workers: {w: {"log": next(logs), "status": {"updated": 100.0}, "now": 110.0} for w in workers}
    with contextlib.redirect_stdout(io.StringIO()):
        line = confirm_keeper("x-keeper", types.SimpleNamespace(cluster_id=11, get_client=lambda: late),
                              attempts=4, delay_s=0.0, sleep=lambda s: None)
    ok("workers=3" in str(line), f"RC52i a keeper whose first tick comes late is confirmed once it ticks: {line}")

    # v2-nigeria-gf13-full (2026-11-01): the run paused for sign-off and its cluster stopped ~5 min after it was fired,
    # before the keeper's first tick; the keeper then exited by design ("run cluster stopped") and the launch failed
    # with "not confirmed holding the pool". A run cluster that has stopped is reported as such, not as a failure.
    kc4 = types.SimpleNamespace(cluster_id=12, get_client=lambda: FakeClient([{}], status="closed"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            line = confirm_keeper("x-keeper", kc4, attempts=2, delay_s=0.0, sleep=lambda s: None,
                                  run_alive=lambda: False)
        ok("run cluster" in str(line) and "stopped" in str(line),
           f"RC52m a run cluster that stopped before the keeper ticked is reported, not a launch failure: {line}")
    except SystemExit as e:
        ok(False, f"RC52m a run cluster that stopped before the keeper ticked is reported, not a launch failure: {e}")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            confirm_keeper("x-keeper", kc4, attempts=2, delay_s=0.0, sleep=lambda s: None, run_alive=lambda: True)
        ok(False, "RC52n a live run with an unconfirmed keeper still fails the launch")
    except SystemExit as e:
        ok("not confirmed" in str(e), f"RC52n a live run with an unconfirmed keeper still fails the launch: {e}")

    # Codex round 1: launch keeps the replacement client for the rest of the launch.
    from unittest import mock

    class LaunchClient(FakeClient):
        def wait_for_workers(self, *a, **kw):
            pass

        def upload_file(self, *a):
            self.status = "closed"   # the client drops right after the upload

    old_c, new_c = LaunchClient([{}]), LaunchClient([{"workers": {f"tls://a:{i}": {} for i in range(4)}}])
    run_cl = types.SimpleNamespace(cluster_id=12, get_client=lambda: old_c if old_c.status == "running" else new_c)
    used = []
    comp = types.SimpleNamespace(WORKER_VM_TYPES=["Standard_E8s_v5"], WORKER_OPTIONS={}, ADAPT_OPTIONS={},
                                 register_compute_tags=lambda c, exclude: used.append(c))
    fake_coiled = types.ModuleType("coiled")
    rc52_made = []
    fake_coiled.Cluster = lambda **kw: rc52_made.append({"name": kw.get("name"), "id": 12,
                                                         "created": _fake_coiled_event(kw.get("name")),
                                                         "current_state": {"state": "running"}}) or run_cl
    fake_coiled.list_clusters = lambda workspace: list(rc52_made)
    with tempfile.TemporaryDirectory() as d:
        q = os.path.join(d, "q.txt")
        open(q, "w").write("question")
        lcfg = RunConfig(name="lt", question_file=q, labels=("R1",), out_dir=d, keeper=False, overflow_max=0,
                         pipeline="v2")
        with mock.patch.dict(sys.modules, {"coiled": fake_coiled}), \
                mock.patch.object(rc_mod, "is_running", return_value=False), \
                mock.patch.object(rc_mod, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(rc_mod, "code_tarball", return_value=(b"", "c0")), \
                mock.patch.object(rc_mod, "pipeline_preflight", return_value="v2"), \
                mock.patch.object(rc_mod, "_compute", return_value=comp), \
                mock.patch.object(rc_mod, "_submit", side_effect=lambda cfg, c, *a: used.append(c)), \
                mock.patch.object(rc_mod, "wait_listed", return_value=True), \
                mock.patch.object(eval_paths, "archive_sha256", return_value="0"), \
                mock.patch.object(time, "sleep", lambda s: None), contextlib.redirect_stdout(io.StringIO()):
            launch(lcfg)
    ok(used and all(c is new_c for c in used),
       f"RC52j launch uses the replacement client after the first one closed: {[c is new_c for c in used]}")

    # The keeper loop itself, on a fake coiled: the first tick's identity is empty.
    class FakeCluster:
        adapts = []

        def __init__(self, **kw):
            self.cluster_id = 7
            self.client = FakeClient([{}, {"workers": table}])

        def adapt(self, **kw):
            FakeCluster.adapts.append(kw)

        def get_client(self):
            return self.client

    ticks = iter([True] * 6)
    fake = types.ModuleType("coiled")
    fake.list_clusters = lambda workspace: ([{"name": "x", "id": 7, "current_state": {"state": "running"}}]
                                            if next(ticks, False) else [])
    fake.Cluster = FakeCluster
    real_coiled, real_sleep = sys.modules.get("coiled"), time.sleep
    paths = (ws.KEEPER_STATUS, ws.KEEPER_LOG)
    with tempfile.TemporaryDirectory() as d:
        ws.KEEPER_STATUS, ws.KEEPER_LOG = os.path.join(d, "st.json"), os.path.join(d, "keeper.log")
        sys.modules["coiled"] = fake
        time.sleep = lambda s: None
        try:
            r = ws._keeper_loop("tok", "x", "ws", 3, 9, 1.0, {}, cluster_id=7, tick_s=0)
            log = open(ws.KEEPER_LOG).read()
            st = json.load(open(ws.KEEPER_STATUS))
        finally:
            time.sleep = real_sleep
            ws.KEEPER_STATUS, ws.KEEPER_LOG = paths
            if real_coiled is None:
                sys.modules.pop("coiled", None)
            else:
                sys.modules["coiled"] = real_coiled
    ok(r == "keeper exited" and len(FakeCluster.adapts) == 1 and "keeper error" not in log
       and st.get("workers") == 2 and "workers=2" in log and KEEPER_TICK.search(log),
       f"RC52g the keeper loop waits out an unpopulated scheduler identity without dropping its adapt(): "
       f"adapts={len(FakeCluster.adapts)} log={log[-300:]!r}")

    # 2026-11-12 (P6): the attached Cluster's own worker view stayed at the 5 workers present when the keeper
    # started while the pool grew to 41, so dask's Adaptive took the other workers as requested but never
    # arrived and closed them without asking workers_to_close (BusyWorkerGuard): busy workers were retired.
    # Each tick the keeper copies the scheduler's worker table into the Cluster's view.
    class StaleCluster(FakeCluster):
        last = None

        def __init__(self, **kw):
            super().__init__(**kw)
            self.client = FakeClient([{"workers": table}] * 6)
            # One worker the snapshot has, one it lacks (joined after it was taken: Codex review of #203).
            self.scheduler_info = {"workers": {"tls://10.0.0.5:1": {"name": "a"}, "tls://10.0.0.9:1": {"name": "late"}},
                                   "id": "s"}
            StaleCluster.last = self

    ticks = iter([True] * 4)
    fake.list_clusters = lambda workspace: ([{"name": "x", "id": 7, "current_state": {"state": "running"}}]
                                            if next(ticks, False) else [])
    fake.Cluster = StaleCluster
    with tempfile.TemporaryDirectory() as d:
        ws.KEEPER_STATUS, ws.KEEPER_LOG = os.path.join(d, "st.json"), os.path.join(d, "keeper.log")
        sys.modules["coiled"] = fake
        time.sleep = lambda s: None
        try:
            ws._keeper_loop("tok", "x", "ws", 3, 9, 1.0, {}, cluster_id=7, tick_s=0)
            log = open(ws.KEEPER_LOG).read()
        finally:
            time.sleep = real_sleep
            ws.KEEPER_STATUS, ws.KEEPER_LOG = paths
            if real_coiled is None:
                sys.modules.pop("coiled", None)
            else:
                sys.modules["coiled"] = real_coiled
    view = (StaleCluster.last.scheduler_info or {}).get("workers") if StaleCluster.last else None
    ok(view is not None and set(table) <= set(view) and "tls://10.0.0.9:1" in view
       and StaleCluster.last.scheduler_info.get("id") == "s" and "keeper error" not in log,
       f"RC52k each keeper tick adds the scheduler's workers the attached Cluster's view lacks, so Adaptive "
       f"sees every worker that joined, and keeps a worker that joined after the snapshot: {view} "
       f"log={log[-300:]!r}")
    # The Cluster's own status stream still removes what it reports (a `del` on a missing key would end it),
    # and on a Cluster with an event loop the merge runs there.
    calls = []
    lp = types.SimpleNamespace(add_callback=lambda fn: calls.append(fn))
    cl2 = types.SimpleNamespace(scheduler_info={"workers": {"tls://10.0.0.7:1": {"name": "gone"}}}, loop=lp)
    ws.sync_observed(cl2, {"tls://10.0.0.8:1": {"name": "new"}})
    before = dict(cl2.scheduler_info["workers"])
    for fn in calls:
        fn()
    del cl2.scheduler_info["workers"]["tls://10.0.0.7:1"]          # the stream's remove
    ok(len(calls) == 1 and before == {"tls://10.0.0.7:1": {"name": "gone"}}
       and cl2.scheduler_info["workers"] == {"tls://10.0.0.8:1": {"name": "new"}},
       f"RC52l the merge runs on the Cluster's loop, adds only, and leaves every reported worker removable: "
       f"{len(calls)} {before} {cl2.scheduler_info['workers']}")

    # Codex review round 2 of #203: a per-tick refresh leaves a window (Adaptive decides every 15 s, the
    # keeper ticks every 60 s) in which a worker that joined after the tick, with close votes from its
    # boot, is retired as "not yet arrived". The keeper's Adaptive refreshes the view before every
    # decision and checks every worker it would retire against the scheduler: a worker the scheduler has
    # goes only if workers_to_close (BusyWorkerGuard) offers it; an unreadable scheduler retires nothing.
    import asyncio

    class FakeComm:
        def __init__(self, present, safe, fail=False, sched=None):
            self.present, self.safe, self.fail = present, safe, fail
            # The scheduler's state at the retirement check: {name: processing}; default: `present`,
            # busy unless in `safe`.
            self.sched = sched if sched is not None else {n: n not in safe for n in present}

        async def run_function(self, function, args, kwargs, wait=True):
            from distributed.protocol.pickle import loads
            if self.fail:
                raise OSError("down")
            wss = {f"tls://{n}:1": types.SimpleNamespace(name=n, processing={"t"} if busy_ else set(),
                                                          long_running=set())
                   for n, busy_ in self.sched.items()}
            sch = types.SimpleNamespace(workers=wss, plugins={})
            return {"status": "OK", "result": loads(function)(*loads(args), dask_scheduler=sch, **loads(kwargs))}

        async def identity(self, n_workers=-1):
            if self.fail:
                raise OSError("down")
            return {"workers": {f"tls://{n}:1": {"name": n} for n in self.present}}

        async def workers_to_close(self, target=None, attribute="address"):
            if self.fail:
                raise OSError("down")
            return list(self.safe)

    class Base:                                    # what AdaptiveCore does with a stale view
        def __init__(self, cluster):
            self.cluster = cluster

        async def recommendations(self, target):
            arrived = {w["name"] for w in self.cluster.scheduler_info["workers"].values()}
            gone = sorted(set(self.cluster.requested) - arrived)
            return {"status": "down", "workers": gone} if gone else {"status": "same"}

    def decide(present, safe, view, requested, fail=False):
        cl3 = types.SimpleNamespace(scheduler_info={"workers": {f"tls://{n}:1": {"name": n} for n in view}},
                                    requested=set(requested), scheduler_comm=FakeComm(present, safe, fail))
        return asyncio.run(ws.guarded_adaptive(Base)(cl3).recommendations(1)), cl3
    # "late" joined after the keeper's tick and runs a chunk: the refresh sees it, nothing is retired.
    r1, c1 = decide(["a", "late"], [], ["a"], ["a", "late"])
    # Both now look not yet arrived (a stale view): "busy" is on the scheduler and not offered by
    # workers_to_close, so it stays; "idle" is offered and goes; "boot" is not on the scheduler and goes.
    class StaleBase(Base):
        async def recommendations(self, target):
            return {"status": "down", "workers": ["busy", "idle", "boot"]}
    cl4 = types.SimpleNamespace(scheduler_info={"workers": {}}, requested={"busy", "idle", "boot"},
                                scheduler_comm=FakeComm(["busy", "idle"], ["idle"]))
    r2 = asyncio.run(ws.guarded_adaptive(StaleBase)(cl4).recommendations(1))
    cl5 = types.SimpleNamespace(scheduler_info={"workers": {}}, requested={"busy"},
                                scheduler_comm=FakeComm(["busy"], [], fail=True))
    r3 = asyncio.run(ws.guarded_adaptive(StaleBase)(cl5).recommendations(1))
    # Codex review round 3 of #203: "boot" was not on the scheduler at the refresh but joined and took a
    # chunk before the retirement check; the check is one scheduler-side call, so it is kept.
    cl6 = types.SimpleNamespace(scheduler_info={"workers": {}}, requested={"busy", "idle", "boot"},
                                scheduler_comm=FakeComm(["busy", "idle"], ["idle"],
                                                        sched={"busy": True, "idle": False, "boot": True}))
    r4 = asyncio.run(ws.guarded_adaptive(StaleBase)(cl6).recommendations(1))
    ok(r1 == {"status": "same"} and "tls://late:1" in c1.scheduler_info["workers"],
       f"RC52p the keeper's Adaptive refreshes the worker view before each decision: a worker that joined "
       f"after the tick is observed and not retired: {r1} {sorted(c1.scheduler_info['workers'])}")
    ok(r2 == {"status": "down", "workers": ["idle", "boot"]},
       f"RC52q every worker it would retire is checked against the scheduler: a busy one (not offered by "
       f"workers_to_close) stays; an idle one and one that never joined go: {r2}")
    ok(r3 == {"status": "same"},
       f"RC52r with the scheduler unreadable it retires nothing: {r3}")
    ok(r4 == {"status": "down", "workers": ["idle"]},
       f"RC52s a worker that joined and took a chunk after the refresh is kept: the retirement check is one "
       f"scheduler-side call on the scheduler's own state: {r4}")


def _self_test_attach(ok) -> None:
    """RC60: one attach call (worker_side.attach_existing) for the launcher, the keeper and the
    overflow pool; one workspace name for the launcher and the VM jobs (worker_side.WORKSPACE)."""
    import importlib.util
    import types
    calls = []

    class Cl:
        def __init__(self, **kw):
            calls.append(kw)
            self.cluster_id = kw.pop("_id", 5)
            self.down = False

        def shutdown(self):
            self.down = True

    fake = types.ModuleType("coiled")
    fake.Cluster = Cl
    listed = []
    fake.list_clusters = lambda workspace: listed
    saved = sys.modules.get("coiled")
    sys.modules["coiled"] = fake
    try:
        cl = ws.attach_existing("n", "w", 5)
        ok(calls == [{"name": "n", "workspace": "w", "shutdown_on_close": False, "wait_for_workers": False,
                     "worker_vm_types": ws.ATTACH_VM_TYPES, "scheduler_vm_types": ws.ATTACH_VM_TYPES}]
           and cl.cluster_id == 5, f"RC60a attach_existing attaches by name without waiting for workers: {calls}")
        made = []
        fake.Cluster = lambda **kw: made.append(Cl(**kw)) or made[-1]
        listed[:] = [{"name": "n", "id": 5, "current_state": {"state": "ready"},
                      "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]   # created by this attach
        try:
            ws.attach_existing("n", "w", 9)
            ok(False, "RC60b a cluster that is not the one checked is refused")
        except SystemExit as e:
            ok(made and made[-1].down and "stopped while attaching" in str(e),
               f"RC60b a cluster that is not the one checked is shut down and refused: {e}")
        calls.clear()
        fake.Cluster = Cl
        listed[:] = [{"name": "n", "id": 5, "current_state": {"state": "stopping"}}]
        try:
            attach("n")
            ok(False, "RC60c attach refuses a stopping cluster")
        except SystemExit as e:
            ok(not calls and "not running" in str(e), f"RC60c attach never constructs a Cluster for a stopping one: {calls}")
        listed[:] = [{"name": "n", "id": 5, "current_state": {"state": "running"}}]
        cl = attach("n")
        ok(calls == [{"name": "n", "workspace": WORKSPACE, "shutdown_on_close": False, "wait_for_workers": False,
                     "worker_vm_types": ws.ATTACH_VM_TYPES, "scheduler_vm_types": ws.ATTACH_VM_TYPES}]
           and cl.cluster_id == 5, f"RC60d attach checks running, then attaches with wait_for_workers=False: {calls}")
        calls.clear()
        ov = ws._Overflow({"name": "n-overflow", "id": 5}, "w")
        ov.cl = None
        Cl.get_client = lambda self: "C"
        ok(ov.attach() == (ov.cl, "C") and calls == [{"name": "n-overflow", "workspace": "w", "shutdown_on_close": False,
                                                     "wait_for_workers": False,
                                                     "worker_vm_types": ws.ATTACH_VM_TYPES,
                                                     "scheduler_vm_types": ws.ATTACH_VM_TYPES}],
           f"RC60e the keeper's overflow handle attaches through the same call: {calls}")
        # Codex round 3 (P2-b): an attach refusal leaves the overflow pool unavailable for the tick; the keeper goes on
        fake.Cluster = lambda **kw: Cl(_id=9, **kw)
        listed[:] = [{"name": "n-overflow", "id": 9, "current_state": {"state": "running"},
                      "created": "2020-01-01T00:00:00Z"}]   # another invocation's: refused, left alone
        ov = ws._Overflow({"name": "n-overflow", "id": 5}, "w")
        try:
            got, step = ov.attach(), ov.step(None, None, 4, 1, None, time.time())
            ok(got == (None, None) and step.get("unavailable") and step["desired"] == 0 and step["scale_to"] is None
               and not step["join"] and ov.cl is None, f"RC60g an overflow attach refusal: unavailable this tick: {step}")
        except BaseException as e:  # noqa: BLE001 — SystemExit would end the keeper
            ok(False, f"RC60g an overflow attach refusal must not end the keeper: {type(e).__name__}: {e}")
    finally:
        if saved is None:
            sys.modules.pop("coiled", None)
        else:
            sys.modules["coiled"] = saved
    ok(WORKSPACE == ws.WORKSPACE, "RC60f launcher and worker side name one workspace")
    _self_test_bwrap(ok)


def _fake_coiled_event(name, created=True) -> str:
    """What coiled.Cluster logs to the "coiled" logger (coiled 1.135.6, v2/cluster.py:1685 / :1688), for fakes."""
    import logging
    logging.getLogger("coiled").info(f"Creating Cluster (name: {name}, https://x ). This usually takes 1-2 minutes..."
                                     if created else f"Attaching to existing cluster (name: {name}, https://x )")
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _self_test_s18(ok, tb) -> None:
    """F6 S1-8 (no Coiled, no network). The F5 resume launch (2026-11-08): the scheduler listed the run after 43 s,
    then the laptop's client lost its connection (`OSError: Timed out trying to connect to tls://...:443 after
    30 s`); the launch raised, a watchdog read that as a failed launch and shut the clusters down, though the run
    kept going. clusters.json had not been written."""
    import contextlib
    import io
    import tempfile
    import types
    from unittest import mock
    g, M = vars(rc_mod), rc_mod
    table = {f"tls://10.0.0.{i}:1": {} for i in range(3)}
    lost = OSError("Timed out trying to connect to tls://10.1.2.3:443 after 30 s")
    listed = {"tls://10.0.0.0:1": ["run-R1-1"]}

    def fake_helper(role, name, cid, ret):
        def f(*a, record=None, **k):
            if record:
                record(role, name, cid)
            return ret
        return f

    class Client:
        def __init__(self, listing, cf):
            self.status, self.listing, self.cf, self.seen = "running", list(listing), cf, []

        def wait_for_workers(self, *a, **kw):
            pass

        def upload_file(self, *a):
            pass

        def scheduler_info(self):
            return {"workers": table}

        def run_on_scheduler(self, fn, *a):
            return None

        def processing(self, workers=None):
            self.seen.append(json.load(open(self.cf)) if os.path.exists(self.cf) else None)
            step = self.listing.pop(0) if len(self.listing) > 1 else self.listing[0]
            if isinstance(step, BaseException):
                raise step
            return step

    def go(listing, keeper=True, overflow_exc=None, tags_exc=None, keeper_exc=None):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.txt")
            open(q, "w").write("question")
            cfg = RunConfig(name="lt", question_file=q, labels=("R1",), out_dir=d, keeper=keeper,
                            overflow_max=4 if keeper else 0, pipeline="v2")
            client = Client(listing, cfg.clusters_file)
            stops = []

            def tags(c, exclude):
                if tags_exc:
                    raise tags_exc
            comp = types.SimpleNamespace(WORKER_VM_TYPES=["Standard_E8s_v5"], WORKER_OPTIONS={}, ADAPT_OPTIONS={},
                                         register_compute_tags=tags)
            fake_coiled = types.ModuleType("coiled")
            made = []

            def cluster(**kw):
                made.append({"name": kw.get("name"), "id": 77, "created": _fake_coiled_event(kw.get("name")),
                             "current_state": {"state": "running"}})
                return types.SimpleNamespace(cluster_id=77, get_client=lambda: client)
            fake_coiled.Cluster = cluster
            fake_coiled.list_clusters = lambda workspace: list(made)
            P = mock.patch.object
            with mock.patch.dict(sys.modules, {"coiled": fake_coiled}), \
                    P(M, "is_running", return_value=False), P(M, "live_id", return_value=77), \
                    P(M, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                    P(M, "code_tarball", return_value=(b"", "c0")), P(M, "pipeline_preflight", return_value="v2"), \
                    P(M, "_compute", return_value=comp), P(M, "_submit", side_effect=lambda c_, cl, lab, *a, **k: (f"run-{lab}-1", types.SimpleNamespace(status="pending"))), \
                    P(M, "start_overflow", side_effect=overflow_exc or fake_helper("overflow", "lt-overflow", 78, {"id": 78})), \
                    P(M, "start_keeper", side_effect=fake_helper("keeper", "lt-keeper", 79, types.SimpleNamespace(cluster_id=79))), \
                    P(M, "confirm_keeper", side_effect=keeper_exc or (lambda *a, **k: ["tick"])), \
                    P(M, "shutdown", side_effect=lambda c_: stops.append("shutdown")), \
                    P(M, "attach", side_effect=lambda n: stops.append(n)), \
                    P(eval_paths, "archive_sha256", return_value="0"), P(time, "sleep", lambda s: None):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    try:
                        rc = launch(cfg)
                    except SystemExit as e:
                        rc = ("exit", e.code)
                    except Exception as e:  # noqa: BLE001
                        rc = ("raised", type(e).__name__)
            cl = json.load(open(cfg.clusters_file)) if os.path.exists(cfg.clusters_file) else {}
            return rc, cl, client.seen, stops, out.getvalue()

    codes = {k: g.get(k) for k in ("EXIT_LAUNCHED", "EXIT_FAILED", "EXIT_SUBMITTED_LOST", "EXIT_UNCONFIRMED",
                                   "EXIT_INCOMPLETE")}
    ok(codes == {"EXIT_LAUNCHED": 0, "EXIT_FAILED": 1, "EXIT_SUBMITTED_LOST": 3, "EXIT_UNCONFIRMED": 4,
                 "EXIT_INCOMPLETE": 5}, f"LR0 the launch exit codes are named: {codes}")
    rc, cl, seen, stops, out = go([{}, listed], overflow_exc=lost)
    ok(rc == 3 and not stops, f"LR1 replay F5: listed, then the connection drops: 'submitted, connection lost' "
                              f"(exit 3), nothing shut down: rc={rc} stops={stops}")
    ok(cl.get("status") == "submitted_connection_lost" and "do not shut" in out.lower() and "status" in out,
       f"LR1b the handover is recorded in clusters.json and says how to attach later: {cl.get('status')} {out[-300:]!r}")
    ok(seen and seen[0] and (seen[0].get("run") or {}).get("id") == 77 and seen[0].get("launch_id")
       and seen[0].get("launched_utc"), f"LR2 clusters.json names the run cluster, launch id and time before the "
                                        f"acknowledgement wait: {seen[:1]}")
    rc, cl, seen, stops, out = go([lost])
    ok(rc == 4 and not stops and cl.get("status") == "submitted_unconfirmed",
       f"LR3 the connection drops before the scheduler lists the run: 'submitted, not confirmed' (exit 4), nothing "
       f"shut down: rc={rc} {cl.get('status')} stops={stops}")
    rc, cl, seen, stops, out = go([{}], keeper=False)
    ok(rc == 4 and not stops, f"LR3b connected, but never listed within the bound: exit 4, nothing shut down: rc={rc}")
    rc, cl, seen, stops, out = go([listed], tags_exc=RuntimeError("tags"))
    ok(isinstance(rc, tuple) and (cl.get("run") or {}).get("id") == 77 and cl.get("status") == "failed",
       f"LR4 a failure before any submission is a failed launch, and clusters.json names what to stop by id: "
       f"rc={rc} {cl}")
    rc, cl, seen, stops, out = go([listed], keeper_exc=SystemExit("keeper not confirmed"))
    ok(rc == 5 and not stops and cl.get("status") == "submitted_launch_incomplete",
       f"LR5 acknowledged, then a later step fails: 'submitted, launch incomplete' (exit 5), not a failed launch: "
       f"rc={rc} {cl.get('status')}")
    rc, cl, seen, stops, out = go([{}, {}, listed])
    ok(rc == 0 and cl.get("status") == "launched" and (cl.get("keeper") or {}).get("id") == 79
       and (cl.get("overflow") or {}).get("id") == 78, f"LR6 a clean launch exits 0 with every cluster recorded: rc={rc} {cl}")
    doc = rc_mod.__doc__ or ""
    ok("Exit codes" in doc and all(f"  {n} " in doc for n in (0, 1, 3, 4, 5)) and "connection lost" in doc,
       "LR7 the module doc lists the launch exit codes for an external watchdog")
    with tempfile.TemporaryDirectory() as d:
        cf = os.path.join(d, "c.py")
        open(cf, "w").write("from run_cluster import RunConfig\nCONFIG = RunConfig(name='z', question_file='q')\n")
        with mock.patch.object(M, "launch", return_value=3):
            ok(rc_main(["x", cf, "launch"]) == 3, "LR8 main returns launch's exit code")
    # the real _submit hands back the key the scheduler will list
    sub = []
    fc = types.SimpleNamespace(submit=lambda *a, **kw: sub.append(kw) or "fut")
    with mock.patch.dict(sys.modules, {"distributed": types.SimpleNamespace(fire_and_forget=lambda f: None)}):
        cfg = RunConfig(name="z", question_file="q", pipeline="v2")
        got = _submit(cfg, fc, "R1", "w", b"", "q", {"ANTHROPIC_API_KEY": "k"}, "L")
    key = got[0] if isinstance(got, tuple) else got
    ok(sub and isinstance(got, tuple) and got[1] == "fut" and key == sub[0].get("key") and str(key).startswith("run-R1-"),
       f"LR9 _submit returns its task key and the future it holds (P2-7): {got}")
    wl = g.get("wait_listed")
    if wl:
        sl = []
        c = Client([{}, {}, listed], "/nonexistent/clusters.json")
        ok(wl(c, "run-R1-1", attempts=5, delay_s=5.0, sleep=sl.append) is True and sl == [5.0, 5.0],
           f"LR10 wait_listed polls the scheduler until it lists the key: {sl}")
        ok(wl(Client([{}], "/nonexistent/clusters.json"), "run-R1-1", attempts=3, delay_s=0, sleep=sl.append) is False,
           "LR10b a key never listed within the bound is not acknowledged")
        try:
            wl(Client([lost], "/nonexistent/clusters.json"), "run-R1-1", attempts=3, delay_s=0, sleep=sl.append)
            ok(False, "LR10c a lost connection while waiting is raised to the caller")
        except OSError:
            ok(True, "LR10c a lost connection while waiting is raised to the caller")
    else:
        ok(False, "LR10 wait_listed exists")
    # attach later: start the keeper for the recorded run cluster
    kl = g.get("keeper_later")
    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="lt", question_file="q", out_dir=d, pipeline="v2", overflow_max=0)
        json.dump({"run": {"name": "lt", "id": 77}, "launch_id": "L", "status": "submitted_connection_lost"},
                  open(cfg.clusters_file, "w"))
        P = mock.patch.object
        refused = None
        with P(M, "_compute", return_value=types.SimpleNamespace(ADAPT_OPTIONS={}, WORKER_VM_TYPES=["Standard_E8s_v5"])), \
                P(M, "start_keeper", side_effect=fake_helper("keeper", "lt-keeper", 79, types.SimpleNamespace(cluster_id=79))) as sk, \
                P(M, "confirm_keeper", return_value=["tick"]), P(time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(io.StringIO()):
            with P(M, "live_id", side_effect=lambda n: 77 if n == "lt" else None):
                rc = kl(cfg) if kl else None
            cl = json.load(open(cfg.clusters_file))
            started = sk.call_args[0][2] if sk.call_args else None
            with P(M, "live_id", return_value=99):
                try:
                    kl(cfg) if kl else None
                except SystemExit as e:
                    refused = str(e)
    ok(rc == 0 and (cl.get("keeper") or {}).get("id") == 79 and started == 77,
       f"LR11 `keeper` starts the keeper for the recorded run cluster later: rc={rc} {cl}")
    ok(refused and "77" in refused, f"LR11b `keeper` refuses when the live run cluster is not the recorded one: {refused}")
    # would this reuse? the shipped compute.py answers, from the exact tarball
    rcheck = g.get("reuse_check")
    stub = (b"import json, os, sys\nprint('noise')\nprint(json.dumps({'argv': sys.argv[1:], 'here': __file__, "
            b"'stored': 5, 'reusable': 4}))\n")
    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="z", question_file="q", pipeline="v2")
        r = rcheck(cfg, d, code=tb({"scripts/compute.py": stub})) if rcheck else {}
    ok(r.get("stored") == 5 and r.get("reusable") == 4 and r.get("argv", [None])[:2] == ["reuse-check", d]
       and not str(r.get("here", "")).startswith(REPO),
       f"LR12 reuse_check runs the tarball's own compute.py reuse-check on the run dir: {r}")


def _self_test_s18_r1(ok) -> None:
    """Codex round 1 on #161 (no Coiled, no network): every post-submit path of launch and relaunch ends in a
    do-not-shut-down code (P1-1, P1-2); exit 1 never points at a previous launch's record (P1-3); a per-config lock and
    record ownership (P2-4); helper ids recorded at construction (P2-5); every assignment before the first submit
    (P2-6); a fast or queued run is acknowledged (P2-7)."""
    import contextlib
    import fcntl
    import io
    import tempfile
    import types
    from unittest import mock
    M, P = rc_mod, mock.patch.object
    table = {f"tls://10.0.0.{i}:1": {} for i in range(3)}
    lost = OSError("Timed out trying to connect to tls://10.1.2.3:443 after 30 s")

    class Client:
        def __init__(self, listing=None, state=None):
            self.status, self.listing, self.state = "running", list(listing or [{}]), state

        def wait_for_workers(self, *a, **kw):
            pass

        def upload_file(self, *a):
            pass

        def scheduler_info(self):
            return {"workers": table}

        def run_on_scheduler(self, fn, *a):
            return self.state

        def processing(self, workers=None):
            step = self.listing.pop(0) if len(self.listing) > 1 else self.listing[0]
            if isinstance(step, BaseException):
                raise step
            return step

    def lists(*labels):
        return {"tls://10.0.0.0:1": [f"run-{x}-1" for x in labels]}

    def go(listing=None, labels=("R1",), keeper=False, fut_status="pending", state=None, writes_fail=(),
           live_exc=None, overflow_exc=None, pre=None, running=False, preflight_exc=None, real_overflow=False,
           hold_lock=False, env=None):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.txt")
            open(q, "w").write("question")
            cfg = RunConfig(name="lt", question_file=q, labels=labels, out_dir=d, keeper=keeper,
                            overflow_max=4 if keeper else 0, pipeline="v2")
            if pre is not None:
                json.dump(pre, open(cfg.clusters_file, "w"))
            client = Client(listing if listing is not None else [lists(*labels)], state)
            real_write = g.get("write_clusters")

            def write(cfg_, cl, *a, **kw):
                if cl.get("status") in writes_fail:
                    raise OSError(f"disk full writing {cl.get('status')}")
                return real_write(cfg_, cl, *a, **kw)

            made = []

            def cluster(**kw):
                cl = types.SimpleNamespace(cluster_id=78 if kw.get("name", "").endswith("-overflow") else 77,
                                           get_client=lambda: client)
                made.append({"name": kw.get("name"), "id": cl.cluster_id, "created": _fake_coiled_event(kw.get("name")),
                             "current_state": {"state": "running"}})
                if kw.get("name", "").endswith("-overflow"):
                    def boom(*a):
                        raise RuntimeError("upload failed")
                    cl.get_client = lambda: types.SimpleNamespace(upload_file=boom)
                return cl
            fake_coiled = types.ModuleType("coiled")
            fake_coiled.Cluster = cluster
            fake_coiled.list_clusters = lambda workspace: list(made)
            comp = types.SimpleNamespace(WORKER_VM_TYPES=["Standard_E8s_v5"], WORKER_OPTIONS={}, ADAPT_OPTIONS={},
                                         register_compute_tags=lambda c, exclude: None)
            pats = [P(M, "is_running", return_value=running),
                    P(M, "live_id", side_effect=live_exc or (lambda n: 77)),
                    P(M, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}),
                    P(M, "code_tarball", return_value=(b"", "c0")),
                    P(M, "pipeline_preflight", side_effect=preflight_exc or (lambda *a: "v2")),
                    P(M, "_compute", return_value=comp),
                    P(M, "_submit", side_effect=lambda c_, cl, lab, *a, **k: (f"run-{lab}-1",
                                                                         types.SimpleNamespace(status=fut_status))),
                    P(M, "start_keeper", return_value=types.SimpleNamespace(cluster_id=79)),
                    P(M, "confirm_keeper", return_value=["tick"]),
                    P(eval_paths, "archive_sha256", return_value="0"), P(time, "sleep", lambda s: None)]
            if not real_overflow:
                pats.append(P(M, "start_overflow", side_effect=overflow_exc or (lambda *a, **k: {"id": 78})))
            if real_write:
                pats.append(P(M, "write_clusters", side_effect=write))
            lock = None
            if hold_lock:
                lock = open(os.path.join(d, ".run_cluster.lock"), "a")
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = io.StringIO()
            with contextlib.ExitStack() as es:
                es.enter_context(mock.patch.dict(sys.modules, {"coiled": fake_coiled}))
                es.enter_context(mock.patch.dict(os.environ, env or {}))
                for p_ in pats:
                    es.enter_context(p_)
                es.enter_context(contextlib.redirect_stdout(out))
                try:
                    rc = launch(cfg)
                except SystemExit as e:
                    rc = ("exit", e.code)
                except BaseException as e:  # noqa: BLE001
                    rc = ("raised", type(e).__name__)
            if lock:
                lock.close()
            cl = json.load(open(cfg.clusters_file)) if os.path.exists(cfg.clusters_file) else {}
            asg = json.load(open(cfg.state_file)) if os.path.exists(cfg.state_file) else {}
            return rc, cl, asg, out.getvalue()

    g = vars(rc_mod)
    # P1-1: the whole post-submit path
    rc, cl, _, out = go(writes_fail=("submitted", "submitted_connection_lost", "submitted_unconfirmed",
                                     "submitted_launch_incomplete"))
    ok(rc in (3, 5), f"R1a a record write that fails after the submission still exits 3 or 5, never raises: {rc}")
    rc, cl, _, out = go(writes_fail=("launched",))
    ok(rc == 0, f"R1b the final record write is best effort after a confirmed launch: {rc}")
    rc, cl, _, out = go(keeper=True, overflow_exc=lost, live_exc=KeyboardInterrupt)
    ok(rc == 3, f"R1c an interrupt during the handover's own reporting keeps the safe code: {rc}")
    rc, cl, _, out = go(listing=[KeyboardInterrupt()])
    ok(rc == 4, f"R1d an interrupt while waiting for the listing is 'submitted, not confirmed': {rc}")
    # P1-3: exit 1 never points at an older record
    old = {"run": {"name": "lt", "id": 5}, "launch_id": "old", "invocation": "old-token", "status": "launched"}
    for kw, what in ((dict(running=True), "already running"), (dict(preflight_exc=SystemExit("no pipeline")), "preflight"),
                     (dict(hold_lock=True), "lock held")):
        rc, cl, _, out = go(pre=old, **kw)
        ok(isinstance(rc, tuple) and cl == old and "invocation " in out and "old-token" not in out.split("invocation ")[-1][:40],
           f"R3 a refusal ({what}) leaves the earlier record untouched and prints its own invocation token: {rc} {cl}")
    rc, cl, _, out = go(env={"MA_LAUNCH_INVOCATION": "wd-123"})
    ok(rc == 0 and cl.get("invocation") == "wd-123", f"R3b the record carries the watchdog's invocation token: {cl}")
    # P2-4: record ownership
    wc = g.get("write_clusters")
    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="lt", question_file="q", out_dir=d, pipeline="v2")
        json.dump({"invocation": "B", "status": "submitting"}, open(cfg.clusters_file, "w"))
        try:
            wc(cfg, {"invocation": "A", "status": "submitted"}) if wc else None
            refused = False
        except Exception:  # noqa: BLE001
            refused = True
        ok(refused and json.load(open(cfg.clusters_file))["invocation"] == "B",
           "R4 a record owned by another invocation is never replaced")
        ok(not [f for f in os.listdir(d) if f.endswith(".tmp")], "R4b no temporary record file is left behind")
    # P2-5: a helper id is recorded right after its constructor
    rc, cl, _, out = go(keeper=True, real_overflow=True)
    ok(rc == 5 and (cl.get("overflow") or {}).get("id") == 78,
       f"R5 an overflow pool whose upload fails is still in clusters.json: rc={rc} {cl.get('overflow')}")
    # P2-6: every assignment before the first submission
    rc, cl, asg, out = go(labels=("R1", "R2"), listing=[{}])
    ok(rc == 4 and set(asg) == {"R1", "R2"} and asg["R2"].get("submission") == "pending"
       and asg["R1"].get("submission") == "sent" and "relaunch" in out and "R2" in out,
       f"R6 an unconfirmed R1 leaves R2 assigned and pending, so `relaunch R2` can run: rc={rc} "
       f"{ {k: v.get('submission') for k, v in asg.items()} }")
    rc, cl, asg, out = go(labels=("R1", "R2"), listing=[lists("R1", "R2")])
    ok(rc == 0 and all(v.get("submission") == "listed" and str(v.get("task_key", "")).startswith(f"run-{k}-")
                       for k, v in asg.items()), f"R6b both listed, each with its task key: {rc} {asg}")
    # P2-7: a run that finished between polls, and one the scheduler queued
    rc, cl, _, out = go(listing=[{}], fut_status="finished")
    ok(rc == 0, f"R7 a run that finished before the first poll is acknowledged by its future: {rc}")
    rc, cl, _, out = go(listing=[{}], state="queued")
    ok(rc == 0, f"R7b a run the scheduler holds as queued is acknowledged: {rc}")
    why = []
    wl = g.get("wait_listed")
    try:
        got = wl(Client([{}], None), "k", types.SimpleNamespace(status="finished"), attempts=1, why=why)
    except TypeError as e:
        got = e
    ok(got is True and why == ["future finished"], f"R7d wait_listed reports what acknowledged the run: {got} {why}")
    rc, cl, _, out = go(listing=[{}], state="released")
    ok(rc == 4, f"R7c a released key is not an acknowledgement: {rc}")

    # P1-2: relaunch, same boundary and guards
    def re(submit=None, listing=None, state=None, rec=None, vm="none", sched_exc=None, calls=None):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.txt")
            open(q, "w").write("question")
            cfg = RunConfig(name="lt", question_file=q, labels=("R1", "R2"), out_dir=d, pipeline="v2", tree=d)
            json.dump({x: dict({"worker": "tls://10.0.0.0:1", "launch_id": "L", "submission": "pending"},
                               **(rec or {})) for x in cfg.labels}, open(cfg.state_file, "w"))
            client = Client(listing or [lists("R2")], state)
            if sched_exc:
                def rs_(fn, *a):
                    raise sched_exc
                client.run_on_scheduler = rs_
            sub = submit or (lambda c_, cl, lab, *a, **k: (k.get("key") or f"run-{lab}-1",
                                                           types.SimpleNamespace(status="pending")))

            def submit_(*a, **k):
                if calls is not None:
                    calls.append(json.load(open(cfg.state_file))["R2"].get("task_key") == k.get("key") and k.get("key"))
                return sub(*a, **k)
            rs = types.ModuleType("results_store")
            rs.list_blobs = lambda *a: []
            out = io.StringIO()
            with mock.patch.dict(sys.modules, {"results_store": rs}), \
                    P(M, "existing_client", return_value=client), \
                    P(M, "_on_worker", return_value={"state": vm}), \
                    P(M, "code_tarball", return_value=(b"", "c0")), P(M, "pipeline_preflight", return_value="v2"), \
                    P(M, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                    P(M, "_submit", side_effect=submit_), P(M, "_task_key", side_effect=lambda lab: f"run-{lab}-1"), \
                    P(eval_paths, "archive_sha256", return_value="0"), P(time, "sleep", lambda s: None), \
                    contextlib.redirect_stdout(out):
                try:
                    return relaunch(cfg, "R2")
                except SystemExit as e:
                    return ("exit", e.code)
                except BaseException as e:  # noqa: BLE001
                    return ("raised", type(e).__name__)

    def accepted_then_raise(*a, **k):
        raise RuntimeError("submit raised after the scheduler accepted it")
    ok(re() == 0, f"R2a relaunch of a pending label, listed: exit 0 ({re()})")
    ok(re(submit=accepted_then_raise) == 4, f"R2b relaunch: _submit raising is 'not confirmed', never 1: {re(submit=accepted_then_raise)}")
    ok(re(listing=[KeyError("workers")]) == 4, f"R2c relaunch: a non-connection error while waiting is 4: "
                                                f"{re(listing=[KeyError('workers')])}")
    ok(re(listing=[KeyboardInterrupt()]) == 4, "R2d relaunch: an interrupt after the submission is 4")
    # Codex round 2, P2-2: the exact task key is recorded before the submission, and a task the scheduler still holds
    # (queued, or preparing: no run.log yet, so the VM reads "missing") is never submitted a second time
    calls = []
    r = re(calls=calls)
    ok(r == 0 and calls and calls[0], f"R8 relaunch records the exact task key before it submits: {r} {calls}")
    sent = {"submission": "sent", "task_key": "run-R2-0"}
    for st_ in ("processing", "queued", "waiting"):
        calls = []
        r = re(rec=sent, vm="missing", state=st_, calls=calls)
        ok(isinstance(r, tuple) and "run-R2-0" in str(r[1]) and not calls,
           f"R8b a task still {st_} on the scheduler (no run.log yet) is not relaunched: {r} {calls}")
    calls = []
    r = re(rec=sent, vm="missing", state=None, calls=calls, listing=[{}, lists("R2")])
    ok(r != ("exit", None) and calls, f"R8c a task the scheduler no longer holds may be relaunched: {r} {calls}")
    calls = []
    r = re(rec=sent, vm="missing", sched_exc=OSError("lost"), calls=calls)
    ok(isinstance(r, tuple) and not calls, f"R8d the original task's state cannot be read: refused, not relaunched: {r}")
    calls = []
    r = re(rec={"submission": "sent"}, vm="missing", calls=calls)
    ok(isinstance(r, tuple) and not calls, f"R8e a 'sent' assignment with no recorded key is refused: {r}")


def _self_test_s18_r2(ok) -> None:
    """Codex round 2 on #161, P2-1: `keeper` reconciles the recorded helpers first. A live helper with the recorded id
    belongs to this launch and is kept (the keeper only once it is confirmed holding the pool); a live helper with
    another id is an earlier launch's and is stopped by that id; a missing one is started."""
    import contextlib
    import io
    import tempfile
    import types
    from unittest import mock
    M, P = rc_mod, mock.patch.object

    def go(rec, live, confirm_ok=True):
        with tempfile.TemporaryDirectory() as d:
            cfg = RunConfig(name="lt", question_file="q", out_dir=d, pipeline="v2", overflow_max=4)
            json.dump(dict({"run": {"name": "lt", "id": 77}, "launch_id": "L", "invocation": "I",
                            "status": "submitted_launch_incomplete"}, **rec), open(cfg.clusters_file, "w"))
            calls = {"stopped": [], "overflow": 0, "keeper": [], "fresh": []}

            def so(cfg_, comp, record=None, **k):
                calls["overflow"] += 1
                record("overflow", cfg.overflow_name, 88)
                return {"name": cfg.overflow_name, "id": 88}

            def sk(cfg_, ao, cid, overflow=None, record=None, **k):
                calls["keeper"].append((cid, (overflow or {}).get("id")))
                record("keeper", cfg.keeper_name, 89)
                return types.SimpleNamespace(cluster_id=89)

            def conf(name, kc, **k):
                calls.setdefault("conf", []).append({x: k.get(x) for x in ("run_id", "overflow_id")})
                if not confirm_ok and getattr(kc, "cluster_id", None) != 89:
                    raise SystemExit("not confirmed")
                return ["tick"]
            out = io.StringIO()
            with P(M, "_compute", return_value=types.SimpleNamespace(ADAPT_OPTIONS={}, WORKER_VM_TYPES=["Standard_E8s_v5"])), \
                    P(M, "live_id", side_effect=lambda n: live.get(n)), \
                    P(M, "start_overflow", side_effect=so), P(M, "start_keeper", side_effect=sk), \
                    P(M, "confirm_keeper", side_effect=conf), \
                    P(ws, "attach_existing", side_effect=lambda n, w, cid: types.SimpleNamespace(cluster_id=cid)), \
                    P(M, "_stop_id", side_effect=lambda n, cid: calls["stopped"].append(cid), create=True), \
                    P(M, "_fresh", side_effect=lambda n: calls["fresh"].append(n)), \
                    P(time, "sleep", lambda s: None), contextlib.redirect_stdout(out):
                try:
                    rc = keeper_later(cfg)
                except SystemExit as e:
                    rc = ("exit", e.code)
            cl = json.load(open(cfg.clusters_file))
            return rc, calls, cl

    both = {"overflow": {"name": "lt-overflow", "id": 78}, "keeper": {"name": "lt-keeper", "id": 79}}
    rc, calls, cl = go(both, {"lt": 77, "lt-overflow": 78, "lt-keeper": 79})
    ok(rc == 0 and not calls["stopped"] and calls["overflow"] == 0 and not calls["keeper"]
       and cl["overflow"]["id"] == 78 and cl["keeper"]["id"] == 79,
       f"R9 healthy helpers of this launch are kept, nothing stopped or recreated: {rc} {calls}")
    rc, calls, cl = go({"overflow": both["overflow"]}, {"lt": 77, "lt-overflow": 78})
    ok(rc == 0 and not calls["stopped"] and calls["overflow"] == 0 and calls["keeper"] == [(77, 78)],
       f"R9b a kept overflow pool is handed to the new keeper: {rc} {calls}")
    rc, calls, cl = go(both, {"lt": 77, "lt-overflow": 78, "lt-keeper": 95})
    ok(rc == 0 and calls["stopped"] == [95] and calls["overflow"] == 0 and calls["keeper"] == [(77, 78)],
       f"R9c a live keeper with another id is an earlier launch's: stopped by that id, then started: {rc} {calls}")
    rc, calls, cl = go(both, {"lt": 77, "lt-overflow": 78, "lt-keeper": 79}, confirm_ok=False)
    ok(rc == 0 and calls["stopped"] == [79] and calls["keeper"] == [(77, 78)],
       f"R9d this launch's keeper that is not holding the pool is stopped by id and restarted: {rc} {calls}")
    rc, calls, cl = go(both, {"lt": 77, "lt-overflow": 78, "lt-keeper": 79})
    ok((calls.get("conf") or [{}])[0] == {"run_id": 77, "overflow_id": 78},
       f"R9e (round 3, P2-1) the kept keeper is confirmed against this run cluster and overflow pool: {calls.get('conf')}")
    rc, calls, cl = go(both, {"lt": 77, "lt-keeper": 79})
    ok(rc == 0 and calls["stopped"] == [79] and calls["overflow"] == 1 and calls["keeper"] == [(77, 88)]
       and cl["keeper"]["id"] == 89 and cl["overflow"]["id"] == 88,
       f"R9f (round 3, P2-1) the overflow pool is gone but the keeper lives: a new pool, and the keeper (still aimed at "
       f"the old pool) replaced: {rc} {calls}")


def _self_test_s18_r3(ok) -> None:
    """Codex round 3 on #161: a keeper is healthy only with a fresh heartbeat for this run and overflow pool (P2-2);
    every self-test entry refuses the real coiled (P2-3); the smoke watch is bounded by the wall clock, bounds every
    API call and stops only after the launcher has exited and every cluster of the config's names is down (P2-4)."""
    import types
    g, M = vars(rc_mod), rc_mod
    kf = g.get("keeper_fresh")
    tick = "12:00:00 keeper holding lt 2..3 {}\n12:01:00 workers=2 (overflow 0) refusals=0 active=primary"
    st = {"updated": 1000.0, "cluster_id": 77, "overflow": {"id": 78}}
    cases = [("fresh", dict(status=st, now=1060.0, log=tick), True),
             ("stale", dict(status=st, now=1000.0 + 3600, log=tick), False),
             ("another run cluster", dict(status=dict(st, cluster_id=5), now=1060.0, log=tick), False),
             ("another overflow pool", dict(status=dict(st, overflow={"id": 5}), now=1060.0, log=tick), False),
             ("an error after the tick", dict(status=st, now=1060.0, log=tick + "\n12:01:30 keeper error OSError: x; retrying"), False),
             ("exited after the tick", dict(status=st, now=1060.0, log=tick + "\n12:02:00 lt is not running; keeper exiting"), False),
             ("no tick", dict(status=st, now=1060.0, log="12:00:00 keeper holding lt 2..3 {}"), False),
             ("no status", dict(status=None, now=1060.0, log=tick), False)]
    for what, h, want in cases:
        got = kf(h, run_id=77, overflow_id=78) if kf else (None, "")
        ok(got[0] is want, f"K1 keeper_fresh, {what}: {got}")
    # confirm_keeper reads the heartbeat through the keeper's own worker and checks the ids
    table = {"tls://k:1": {}}

    class C:
        status = "running"

        def __init__(self, h):
            self.h = h

        def scheduler_info(self):
            return {"workers": table}

        def run(self, fn, workers):
            return {w: (self.h if getattr(fn, "__name__", "") == "keeper_health" else self.h["log"]) for w in workers}
    for h, want in ((dict(status=st, now=1060.0, log=tick), True), (dict(status=dict(st, overflow=None), now=1060.0, log=tick), False)):
        kc = types.SimpleNamespace(cluster_id=79, get_client=lambda h=h: C(h))
        try:
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                confirm_keeper("lt-keeper", kc, attempts=1, delay_s=0, sleep=lambda s: None, run_id=77, overflow_id=78)
            got = True
        except (SystemExit, TypeError):
            got = False
        ok(got is want, f"K2 confirm_keeper needs a fresh heartbeat for run 77 / overflow 78: want {want}, got {got}")
    # one refusing guard for every self-test entry that can reach the cloud
    guard = None
    try:
        import no_cloud as guard
    except ImportError:
        pass
    ok(guard is not None and guard.active(), "G1 run_cluster's self-test runs under the shared refusing guard (cloud/no_cloud.py)")
    refused = False
    if guard is not None and guard.active():                       # never probed without the guard: unguarded, this would be a real call
        try:
            import coiled
            coiled.list_clusters(workspace="w")
        except RuntimeError:
            refused = True
    ok(refused, "G1b a real coiled call from a run_cluster self-test is refused")
    # the job's cloud-side lifetime: the run's wall time and the hold for collection come from the config
    import time as _time
    sub = []
    fc = types.SimpleNamespace(submit=lambda *a, **kw: sub.append((a, kw)) or "fut")
    from unittest import mock
    with mock.patch.dict(sys.modules, {"distributed": types.SimpleNamespace(fire_and_forget=lambda f: None)}):
        try:
            c2 = RunConfig(name="z", question_file="q", pipeline="v2", run_timeout_hours=0.25, hold_hours=0.1)
            _submit(c2, fc, "R1", "w", b"", "q", {"ANTHROPIC_API_KEY": "k"}, "L")
            got = sub[0][1].get("run_timeout_s"), sub[0][1].get("hold_s")
        except TypeError as e:
            got = e
    ok(got == (900.0, 360.0), f"T1 the run's time limit and collection hold reach run_to_disk: {got}")
    rm = getattr(ws, "_run_main", None)
    if rm:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t0 = _time.monotonic()
            with open(os.path.join(d, "log"), "w") as log:
                rc_ = rm([sys.executable, "-c", "import time; time.sleep(30)"], d, dict(os.environ), log, timeout_s=1)
            ok(rc_ == "timeout" and _time.monotonic() - t0 < 10, f"T2 main.py past its time limit is killed: {rc_}")
    else:
        ok(False, "T2 worker_side._run_main exists")
    hold = getattr(ws, "_hold", None)
    clk = [0.0]
    if hold:
        with tempfile.TemporaryDirectory() as d:
            hold(d, 600, sleep=lambda s: clk.__setitem__(0, clk[0] + s), clock=lambda: clk[0])
        ok(600 <= clk[0] <= 630, f"T3 the hold for collection ends at its bound: {clk[0]}")
    else:
        ok(False, "T3 worker_side._hold exists")


def _self_test_s18_r4(ok) -> None:
    """Codex round 4 on #161, P1, as rebuilt after the owner's read-only check (2026-11-09: coiled 1.135.6 lists no
    tags). A cluster is this invocation's only when (a) coiled.Cluster logged that it created it ("Creating Cluster
    (name: ...)", coiled/v2/cluster.py:1685; "Attaching to existing cluster", :1688, means attached), (b) no live
    cluster of that name was listed just before the constructor, and (c) its `created` time is no earlier than this
    invocation's start less 120 s. Two launchers race on one config name on two machines."""
    import contextlib
    import datetime as _dt
    import io
    import tempfile
    import types
    from unittest import mock
    M, P, g = rc_mod, mock.patch.object, vars(rc_mod)
    table = {f"tls://10.0.0.{i}:1": {} for i in range(3)}

    class Client:
        status = "running"

        def wait_for_workers(self, *a, **kw):
            pass

        def upload_file(self, *a):
            pass

        def scheduler_info(self):
            return {"workers": table}

        def processing(self, workers=None):
            return {"tls://10.0.0.0:1": ["run-R1-1"]}

        def run_on_scheduler(self, fn, *a):
            return None

    def world(stale_listing=False, log=True, created_shift_s=0.0):
        registry, lists_ = {}, [0]

        def cluster(**kw):
            name = kw["name"]
            if name in registry:                             # Coiled attaches by name
                if log:
                    _fake_coiled_event(name, created=False)
            else:
                ts = _fake_coiled_event(name) if log else _dt.datetime.now(_dt.timezone.utc).isoformat()
                if created_shift_s:
                    ts = (_dt.datetime.fromisoformat(ts) + _dt.timedelta(seconds=created_shift_s)).isoformat()
                registry[name] = {"name": name, "id": 100 + len(registry), "created": ts,
                                  "current_state": {"state": "running"}, "kw": kw}
            return types.SimpleNamespace(cluster_id=registry[name]["id"], get_client=lambda: Client())

        def lister(workspace=None):
            lists_[0] += 1
            return [] if stale_listing and lists_[0] == 1 else list(registry.values())
        fake = types.ModuleType("coiled")
        fake.Cluster, fake.list_clusters = cluster, lister
        return fake, registry

    def go(fake, inv, d, submits, ctor_calls=None):
        q = os.path.join(d, "q.txt")
        open(q, "w").write("question")
        cfg = RunConfig(name="lt", question_file=q, labels=("R1",), out_dir=d, keeper=False, overflow_max=0,
                        pipeline="v2", cluster_timeout="40 minutes")
        comp = types.SimpleNamespace(WORKER_VM_TYPES=["Standard_E8s_v5"], WORKER_OPTIONS={}, ADAPT_OPTIONS={},
                                     register_compute_tags=lambda c, exclude: None)
        if ctor_calls is not None:
            real = fake.Cluster
            fake.Cluster = lambda **kw: ctor_calls.append(kw["name"]) or real(**kw)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"coiled": fake}), mock.patch.dict(os.environ, {"MA_LAUNCH_INVOCATION": inv}), \
                P(M, "is_running", return_value=False), P(M, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                P(M, "code_tarball", return_value=(b"", "c0")), P(M, "pipeline_preflight", return_value="v2"), \
                P(M, "_compute", return_value=comp), P(M, "live_id", return_value=None), \
                P(M, "_submit", side_effect=lambda c_, cl, lab, *a, **k: submits.append(inv) or
                  (k.get("key") or "run-R1-1", types.SimpleNamespace(status="pending"))), \
                P(M, "_task_key", side_effect=lambda lab: f"run-{lab}-1"), \
                P(eval_paths, "archive_sha256", return_value="0"), P(time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out):
            try:
                rc = launch(cfg)
            except SystemExit as e:
                rc = ("exit", str(e.code))
        cl = json.load(open(cfg.clusters_file)) if os.path.exists(cfg.clusters_file) else None
        jr = g.get("read_journal")
        return rc, cl, (jr(cfg) if jr else [])
    # (a) the race the pre-listing cannot see: A creates between B's listing and B's constructor
    fake, registry = world(stale_listing=True)
    submits = []
    with tempfile.TemporaryDirectory() as da, tempfile.TemporaryDirectory() as db:
        rc_a, cl_a, j_a = go(fake, "INV-A", da, submits)
        fake.list_clusters = (lambda reg: (lambda workspace=None, n=[0]: (n.__setitem__(0, n[0] + 1) or
                                                                          ([] if n[0] == 1 else list(reg.values())))))(registry)
        rc_b, cl_b, j_b = go(fake, "INV-B", db, submits)
    kw = registry.get("lt", {}).get("kw", {})
    ok((kw.get("tags") or {}).get(INVOCATION_TAG if "INVOCATION_TAG" in g else "ma-invocation") == "INV-A"
       and kw.get("cluster_timeout") == "40 minutes",
       f"O1 the run cluster is created with the invocation tag (for people) and the config's cluster_timeout: "
       f"{ {k: kw.get(k) for k in ('tags', 'cluster_timeout')} }")
    ok(rc_a == 0 and (cl_a or {}).get("invocation") == "INV-A" and (cl_a or {}).get("run", {}).get("id") == registry["lt"]["id"]
       and any(e.get("status") == "created" and e.get("id") == registry["lt"]["id"] for e in j_a),
       f"O2 the launcher whose constructor created the cluster records it, in clusters.json and its journal: {rc_a} {cl_a} {j_a}")
    ok(isinstance(rc_b, tuple) and cl_b is None and submits == ["INV-A"] and "attached" in rc_b[1]
       and any(e.get("status") == "attached" for e in j_b) and not any(e.get("status") == "created" for e in j_b),
       f"O3 the racing launcher's constructor attached (Coiled said so): refused, no record, nothing submitted, "
       f"journaled as attached: {rc_b} {cl_b} {submits} {j_b}")
    # (b) the pre-listing sees a live cluster of the name: refused before the constructor is called
    fake, registry = world()
    ctor = []
    with tempfile.TemporaryDirectory() as da, tempfile.TemporaryDirectory() as db:
        go(fake, "INV-A", da, [])
        rc_b, cl_b, _ = go(fake, "INV-B", db, [], ctor_calls=ctor)
    ok(isinstance(rc_b, tuple) and cl_b is None and not ctor,
       f"O4 a live cluster of the name listed just before the constructor: refused without constructing: {rc_b} {ctor}")
    # (c) no creation event (any doubt), and (d) a created time before the invocation began (less 120 s)
    for kwargs, what in ((dict(log=False), "no creation event"), (dict(created_shift_s=-3600), "created an hour before")):
        fake, registry = world(**kwargs)
        with tempfile.TemporaryDirectory() as d:
            rc, cl, j = go(fake, "INV-C", d, [])
        ok(isinstance(rc, tuple) and cl is None, f"O5 {what}: not this invocation's, refused before any record: {rc} {cl}")


def _self_test_s18_smoke_a(ok) -> None:
    """From smoke case A (2026-11-09, 9f7a787): confirm_keeper stops at once when the run is no longer alive."""
    import contextlib
    import io
    import types
    # confirm_keeper: a run cluster that has stopped ends the wait at once
    sleeps, reads = [], []

    class Closed:
        status = "closed"

        def scheduler_info(self):
            reads.append(1)
            return {}
    kc = types.SimpleNamespace(cluster_id=9, get_client=lambda: Closed())
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            got = confirm_keeper("lt-keeper", kc, attempts=8, delay_s=5.0, sleep=sleeps.append, run_alive=lambda: False)
        except SystemExit as e:
            got = e
    ok(isinstance(got, list) and "stopped" in got[0] and sum(sleeps) <= 15,
       f"A3 confirm_keeper stops waiting as soon as the run cluster has stopped (slept {sum(sleeps)} s): {got}")


def _self_test_bwrap(ok) -> None:
    """RC61: the agent VM's best-effort bubblewrap install (worker_side.ensure_bwrap), shell calls stubbed."""
    from types import SimpleNamespace as NS

    def case(present, rcs, platform="linux", sudo=True, raise_on=None):
        calls, have = [], {"bwrap": present}

        def which(name):
            return "/usr/bin/" + name if (name == "sudo" and sudo) or (name == "bwrap" and have["bwrap"]) else None

        def run(cmd, **k):
            calls.append(" ".join(cmd[2:]))
            if raise_on and raise_on in cmd:
                raise OSError("no apt")
            rc = rcs.pop(0) if rcs else 1
            if rc == 0 and "install" in cmd:
                have["bwrap"] = True
            return NS(returncode=rc)
        return ws.ensure_bwrap(run=run, which=which, platform=platform), calls
    note, calls = case(True, [])
    ok(note == "bwrap: present" and calls == [], f"RC61a bwrap present: nothing is run ({note}, {calls})")
    note, calls = case(False, [0])
    ok(note == "bwrap: installed" and calls == ["apt-get install -y bubblewrap"],
       f"RC61b missing: one sudo -n apt-get install ({note}, {calls})")
    note, calls = case(False, [100, 0, 0])
    ok(note == "bwrap: installed" and calls == ["apt-get install -y bubblewrap", "apt-get update",
                                                "apt-get install -y bubblewrap"],
       f"RC61c a failed install is retried after apt-get update ({note}, {calls})")
    note, calls = case(False, [100, 0, 100])
    ok(note.startswith("bwrap: install failed") and "fail closed" in note,
       f"RC61d both installs fail: a note, no exception ({note})")
    note, calls = case(False, [], raise_on="apt-get")
    ok(note.startswith("bwrap: install failed"), f"RC61e a raising shell call is a note, not a failure ({note})")
    note, calls = case(False, [], sudo=False)
    ok("no sudo" in note and calls == [], f"RC61f no sudo: nothing is run ({note})")
    note, calls = case(False, [], platform="darwin")
    ok("not Linux" in note and calls == [], f"RC61g not Linux: nothing is run ({note})")


def _self_test_sign(ok) -> None:
    """RS1-RS5 (run console review, finding 5): `sign` names the remote run it answers. The label and the run
    dir name come from the caller; the worker refuses a mismatch and signs a v2 spec through stage_machine.py."""
    import tempfile
    try:
        a = parse_sign_args(["ans.yaml", "--label", "F2", "--run", "2026_b", "--expect-spec-sha256", "ab12",
                             "looks", "right"], ["F1", "F2"])
        ok({k: a.get(k) for k in ("answers", "note", "label", "run", "expect")}
           == {"answers": "ans.yaml", "note": "looks right", "label": "F2", "run": "2026_b", "expect": "ab12"},
           f"RS1 sign parses --label, --run and --expect-spec-sha256 before the note ({a})")
        try:
            parse_sign_args(["ans.yaml", "--label", "F9", "n"], ["F1", "F2"])
            ok(False, "RS2 a label the config does not run is refused")
        except SystemExit as e:
            ok("F9" in str(e), f"RS2 a label the config does not run is refused ({e})")
        old = parse_sign_args(["ans.yaml", "a", "note"], ["F1"])
        ok(old["label"] is None and old["note"] == "a note", "RS2b the old form (answers, note) still parses")
    except Exception as e:  # noqa: BLE001 -- before the fix there is no parse_sign_args
        ok(False, f"RS1-RS2 sign arguments: {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory() as td:
        for n in ("2026_a", "2026_b"):
            os.makedirs(os.path.join(td, "F1", "runs", n))
        open(os.path.join(td, "F1", "runs", "2026_b", "run_state.json"), "w").write("{}")
        seen = []

        def fake(cmd, **k):
            seen.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": "none\n" if "status" in cmd else "{}", "stderr": ""})()
        old_work = ws.WORK
        ws.WORK = td
        try:
            r = ws.sign_side("F1", "", "n", run_name="2026_a", _runner=fake)
            ok(r["rc"] != 0 and "2026_b" in r["out"] and "2026_a" in r["out"] and not any("sign" in c for c in seen),
               f"RS3 the worker refuses a sign for another run than the newest on that label ({r})")
            seen.clear()
            r = ws.sign_side("F1", "", "n", run_name="2026_b", expect_spec_sha256="ab12", _runner=fake)
            cmd = seen[-1] if seen else []
            ok(r["rc"] == 0 and "agents/stage_machine.py" in cmd and "sign" in cmd and "--expect-sha256" in cmd
               and "ab12" in cmd, f"RS4 a v2 run's spec is signed through stage_machine.py sign --expect-sha256 ({cmd})")
            seen.clear()
            r = ws.sign_side("F1", "", "n", run_name="2026_b", _runner=fake)
            ok(r["rc"] != 0 and not any("stage_machine" in " ".join(c) for c in seen),
               f"RS5 a v2 spec sign without the displayed spec's sha256 is refused ({r})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"RS3-RS5 worker sign: {type(e).__name__}: {e}")
        # RS6-RS8 (round 2, P1-6): the action is explicit; an expected spec sha always means sha-bound spec signing
        try:
            os.remove(os.path.join(td, "F1", "runs", "2026_b", "run_state.json"))
            seen.clear()
            r = ws.sign_side("F1", "", "n", run_name="2026_b", expect_spec_sha256="ab12", _runner=fake)
            cmd = seen[-1] if seen else []
            ok(r["rc"] == 0 and "agents/stage_machine.py" in cmd and "ab12" in cmd
               and not any("decision_brief.py" in c for c in seen),
               f"RS6 with an expected spec sha and no run_state.json the spec is still signed sha-bound ({cmd})")
            seen.clear()
            r = ws.sign_side("F1", "", "n", run_name="2026_b", action="spec", _runner=fake)
            ok(r["rc"] != 0 and not any("sign" in c for c in seen), f"RS7 action spec without a sha fails closed ({r})")
            a = parse_sign_args(["a.yaml", "--action", "spec", "--expect-derivations-sha256", "none", "n"], ["F1"])
            ok(a.get("action") == "spec" and a.get("expect_derivations") == "none",
               f"RS8 sign parses --action and the snapshot's shas ({a})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"RS6-RS8 worker sign action: {type(e).__name__}: {e}")
        finally:
            ws.WORK = old_work


def _self_test_pipeline(ok, tb) -> None:
    """RP (2026-11-05): the eval launcher ran v1 for a v2 test. cloud/configs/_eval.py passed no
    pipeline flag, main.py defaulted to v1, and neither the launch record nor the run said which ran."""
    import tempfile
    lib = os.path.join(REPO, "scripts", "lib")
    real = open(os.path.join(lib, "pipeline_id.py"), "rb").read()
    real_main = open(os.path.join(REPO, "main.py"), "rb").read()
    g = vars(rc_mod)
    for f in sorted(os.listdir(os.path.join(CLOUD_DIR, "configs"))):
        if not f.endswith(".py") or f.startswith("_"):
            continue
        try:
            c = load_config(os.path.join(CLOUD_DIR, "configs", f))
        except Exception as e:  # noqa: BLE001 -- RC16 reports the load itself
            ok(False, f"RP1 configs/{f} declares its pipeline: {e}")
            continue
        ok(getattr(c, "pipeline", None) == "v2", f"RP1 configs/{f} declares pipeline v2 ({getattr(c, 'pipeline', None)})")
        mf = g.get("main_flags")
        ok(mf is not None and f"--{getattr(c, 'pipeline', None)}" in mf(c) and not {"--v1", "--v2"} & set(c.flags),
           f"RP3 configs/{f}: the launcher passes the declared pipeline to main.py, flags never do")
    for bad in (dict(pipeline="v3"), dict(pipeline="v2", flags=("--v1",)), dict(pipeline="v1", flags=("--v2",)),
                dict(pipeline="v1")):
        try:
            RunConfig(name="t", question_file="/dev/null", **bad)
            ok(False, f"RP4 a config with {bad} is refused")
        except (ValueError, TypeError):
            ok(True, f"RP4 a config with {bad} is refused")
    pf = g.get("pipeline_preflight")
    ok(pf is not None, "RP5 the launcher has a pipeline preflight")
    if pf is None:
        return
    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="ev-x", question_file="/dev/null", out_dir=d, pipeline="v2",
                        flags=("--max-budget-usd", "35", "--signoff", "lead"))
        code = tb({"main.py": real_main, "scripts/lib/pipeline_id.py": real})
        ok(pf(cfg, code) == "v2", "RP5 the shipped pipeline_id agrees with the declared v2")
        try:
            RunConfig(name="t1", question_file="/dev/null", out_dir=d, pipeline="v1")
            ok(False, "RP6 (v1 retired) a v1 config is refused when it is built")
        except ValueError as e:
            ok(pipeline_id.RETIRED_MESSAGE in str(e), f"RP6 (v1 retired) a v1 config is refused when it is built: {e}")
        v1cfg = RunConfig(name="t1", question_file="/dev/null", out_dir=d, pipeline="v2")
        v1cfg.pipeline = "v1"          # a config changed after it was built: the preflight refuses it too
        # (Codex P1) shipped code from before the retirement answers v1: the launcher refuses before asking it.
        old_tree = tb({"main.py": real_main, "scripts/lib/pipeline_id.py": b"import sys\nprint('v1')\n"})
        asked: list = []
        real_run = subprocess.run
        subprocess.run = lambda *a, **k: asked.append(a) or real_run(*a, **k)
        try:
            pf(v1cfg, old_tree)
            ok(False, "RP6b the preflight refuses a v1 config, even for a tree that would run v1")
        except SystemExit as e:
            ok(pipeline_id.RETIRED_MESSAGE in str(e) and not asked,
               f"RP6b the preflight refuses a v1 config before asking the shipped code ({len(asked)} calls): {e}")
        finally:
            subprocess.run = real_run
        for why, c2, code2 in (
                ("a tree with no pipeline_id.py (from before 2026-11-05: defaults to v1)", cfg, tb({"main.py": b"x"})),
                ("a config that declares no pipeline", RunConfig(name="t2", question_file="/dev/null", out_dir=d), code),
                ("a main.py that does not dispatch through pipeline_id (Codex r1 P2: old main.py, new helper)",
                 RunConfig(name="t3", question_file="/dev/null", out_dir=d, pipeline="v2"),
                 tb({"main.py": b"parser.add_argument('--v2')\n", "scripts/lib/pipeline_id.py": real})),
                ("shipped code that would run another pipeline", cfg,
                 tb({"main.py": real_main, "scripts/lib/pipeline_id.py":
                     b"import sys\nprint('v1')\n"}))):
            try:
                pf(c2, code2)
                ok(False, f"RP7 the preflight refuses {why}")
            except SystemExit as e:
                ok(True, f"RP7 the preflight refuses {why}: {str(e)[:90]}")
        rec = g["launch_record"](cfg, worker="w1", commit="abc1234", launch_id="L1", cluster_id=9, code=code,
                                 pipeline="v2")
        ok(rec.get("pipeline") == "v2" and rec.get("commit") == "abc1234" and rec.get("launch_id") == "L1"
           and rec.get("launcher") == "run_cluster:ev-x" and isinstance(rec.get("eval_inputs_sha256"), dict),
           f"RP8 the launch record (assignments.json) carries pipeline, commit and launcher ({rec})")
        ok(ws.run_identity(tb({"runs/r/metadata.json": json.dumps(
            {"pipeline": "v2", "commit": "abc1234def", "launcher": "run_cluster:ev-x"}).encode()}))
           == {"pipeline": "v2", "commit": "abc1234def", "launcher": "run_cluster:ev-x"},
           "RP9 the worker reads the run's identity from its metadata.json")
        ok(ws.run_identity(tb({"runs/r/metadata.json": b"{}"}))["pipeline"] == "unknown"
           and ws.run_identity(b"junk")["commit"] == "unknown", "RP10 a run with no identity reads unknown")

        class Store:
            def __init__(self, files):
                self.files = files

            def download(self, blob, path, account):
                open(path, "wb").write(self.files[blob])
        run = tb({"r/metadata.json": json.dumps({"pipeline": "v2", "commit": "abc1234def"}).encode()})
        res = json.dumps({"exit_code": 0, "duration_s": 5, "run_stored": "outputs/x/r.tar.gz"}).encode()
        m = collect_from_storage(cfg, Store({ws.record_blob(cfg.storage_prefix, "L1", "R1"): res,
                                             "outputs/x/r.tar.gz": run}), "R1", "L1", rec)
        on_disk = json.load(open(os.path.join(d, "R1", "result.json")))
        ok(on_disk.get("run_identity", {}).get("pipeline") == "v2" and on_disk.get("pipeline") == "v2"
           and on_disk.get("commit") == "abc1234" and m.get("launch_id") == "L1",
           f"RP11 the collected result.json records the launch's and the run's pipeline and commit ({on_disk})")
    import inspect
    for fn in (_launch_locked, relaunch):
        src = inspect.getsource(fn)
        ok("pipeline_preflight(cfg, code)" in src and src.index("pipeline_preflight(") < src.index("_submit(")
           and ("coiled.Cluster(" not in src or src.index("pipeline_preflight(") < src.index("coiled.Cluster(")),
           f"RP12 {fn.__name__} checks the pipeline before it creates or submits anything")
    ok("main_flags(cfg)" in inspect.getsource(_submit), "RP13 every submission passes main_flags (the pipeline)")
    ac = g.get("archive_commit")
    ok(ac is not None and ac(tb({"scripts/lib/code_commit.txt": b"0123456789abcdef0123456789abcdef01234567\n"}))
       == "0123456"[:7] and ac(tb({"scripts/lib/code_commit.txt": b"$Format:%H$\n"})) is None and ac(tb({})) is None,
       "RP14 the launch commit is read from the archive itself (the commit that ships, not HEAD later)")


def _self_test_collect_no_shutdown(ok) -> None:
    """Codex round 1 on the launch watch (P1-1): `collect --no-shutdown` leaves stopping to the caller (the watch
    stops by verified id); plain `collect` still shuts down once every run is collected, as before."""
    import tempfile
    from unittest import mock
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "cfg.py")
    open(path, "w").write("from run_cluster import RunConfig\n"
                          f"CONFIG = RunConfig(name='cn', question_file='q', labels=('A',), out_dir={tmp!r}, "
                          f"tree={REPO!r})\n")
    json.dump({"A": {"launch_id": "L1", "worker": "w"}}, open(os.path.join(tmp, "assignments.json"), "w"))
    for extra, want, cid in (([], 1, "CN1 `collect` shuts down after collecting every run (default, unchanged)"),
                             (["--no-shutdown"], 0, "CN2 `collect --no-shutdown` collects and never shuts down")):
        downs = []
        with _patch_rc({"is_running": lambda n, lister=None: True,
                                         "collect_from_storage": lambda *a, **k: {"exit_code": 0, "duration_s": 1},
                                         "shutdown": lambda c: downs.append(c.name)}), \
                contextlib.redirect_stdout(io.StringIO()):
            rc_ = rc_main(["run_cluster.py", path, "collect", *extra])
        ok(rc_ == 0 and len(downs) == want, cid)


def _self_test_collect_import(ok) -> None:
    """CI1-CI3 (2026-11-21: `collect` died with ModuleNotFoundError: results_store unless PYTHONPATH=scripts):
    collect and the other storage commands import results_store from this repo's scripts/, as eval_paths and
    pipeline_id are imported, whatever cfg.tree holds and wherever the command runs from. A fresh interpreter
    (no PYTHONPATH, cwd outside the repo, a config whose tree does not exist), so no earlier import hides it."""
    import tempfile
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "cfg.py")
    open(path, "w").write("from run_cluster import RunConfig\n"
                          f"CONFIG = RunConfig(name='ci', question_file='q', labels=('A',), out_dir={tmp!r}, "
                          f"tree={os.path.join(tmp, 'no-such-tree')!r})\n")
    json.dump({"A": {"launch_id": "L1", "worker": "w"}}, open(os.path.join(tmp, "assignments.json"), "w"))
    code = ("import sys, json\n"
            f"sys.path.insert(0, {CLOUD_DIR!r})\n"
            "import run_cluster as rc\n"
            "from unittest import mock\n"
            "got = {}\n"
            "def fake(cfg, store, *a, **k):\n"
            "    import os; got['file'] = os.path.realpath(store.__file__)\n"
            "    return {'exit_code': 0, 'duration_s': 1}\n"
            "with mock.patch.object(rc, 'is_running', lambda n, lister=None: True), "
            "mock.patch.object(rc, 'collect_from_storage', fake), mock.patch.object(rc, 'shutdown', lambda c: None):\n"
            f"    rc.main(['run_cluster.py', {path!r}, 'collect'])\n"
            "print('RESULT ' + json.dumps(got))\n")
    mine = os.path.realpath(os.path.join(REPO, "scripts", "results_store.py"))
    decoy = os.path.join(tmp, "old_checkout", "scripts")         # CI3 (Codex r1): another checkout on $PYTHONPATH
    os.makedirs(decoy)
    open(os.path.join(decoy, "results_store.py"), "w").write("def list_blobs(*a, **k):\n    return []\n")
    for pp, cid in ((None, "CI1-CI2 no PYTHONPATH"),
                    (os.pathsep.join([decoy, os.path.join(REPO, "scripts")]),
                     "CI3 PYTHONPATH naming another checkout's scripts/ first")):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        if pp:
            env["PYTHONPATH"] = pp
        p = subprocess.run([sys.executable, "-c", code], cwd=tmp, env=env, capture_output=True, text=True,
                           timeout=120)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
        got = json.loads(line[len("RESULT "):]) if line else {}
        ok(p.returncode == 0 and "ModuleNotFoundError" not in p.stderr,
           f"{cid}: `collect` runs with a missing cfg.tree (rc={p.returncode}: {p.stderr[-300:]!r})")
        ok(got.get("file") == mine,
           f"{cid}: `collect` hands collect_from_storage this repo's scripts/results_store.py ({got})")


def _self_test_collect_labels(ok) -> None:
    """RL1-RL5 (2026-11-22 baseline hb1: collect and status looped over cfg.labels, which EVAL_REPS sets at
    collect time; with it unset R2 was skipped without a message, and a plain `collect` could have shut down a
    cluster still running R2). The labels come from the launch record (assignments.json), merged with cfg.labels,
    and the default shutdown is refused while any recorded label has no final result."""
    import tempfile
    from unittest import mock
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "cfg.py")
    open(path, "w").write("from run_cluster import RunConfig\n"
                          f"CONFIG = RunConfig(name='rl', question_file='q', labels=('R1',), out_dir={tmp!r}, "
                          f"tree={REPO!r})\n")
    json.dump({"R1": {"launch_id": "L1", "worker": "w1"}, "R2": {"launch_id": "L1", "worker": "w2"}},
              open(os.path.join(tmp, "assignments.json"), "w"))

    class _Cl:
        def get_client(self):
            return object()

    def run(stored, worker_state, extra=()):
        got, downs, out = [], [], io.StringIO()

        def from_storage(cfg, store, label, *a, **k):
            if label not in stored:
                raise FileNotFoundError(label)
            got.append(label)
            return {"exit_code": 0, "duration_s": 1}
        with _patch_rc({"is_running": lambda n, lister=None: True,
                                         "_results_store": lambda: None,
                                         "collect_from_storage": from_storage,
                                         "checkpoint_record": lambda *a, **k: None,
                                         "local_is_current": lambda *a, **k: None,
                                         "attach": lambda n: _Cl(),
                                         "existing_client": lambda n: object(),     # 1E-ii: attach by id
                                         "_on_worker": lambda c, fn, label, worker: {"state": worker_state},
                                         "shutdown": lambda c: downs.append(c.name)}), \
                contextlib.redirect_stdout(out):
            rc_ = rc_main(["run_cluster.py", path, "collect", *extra])
        return rc_, got, downs, out.getvalue()

    rc_, got, downs, out = run({"R1", "R2"}, "done")
    ok(got == ["R1", "R2"] and len(downs) == 1,
       f"RL1 collect takes R2 from assignments.json although cfg.labels is ('R1',) (got {got}, downs {downs})")
    ok("R2" in out and "WARNING" in out, f"RL2 collect says the recorded labels differ from cfg.labels ({out!r})")
    rc_, got, downs, out = run({"R1"}, "running")
    ok(got == ["R1"] and downs == [] and "R2" in out and "not shutting down" in out,
       f"RL3 the default shutdown is refused while R2 has no final result, naming R2 (downs {downs}, {out!r})")
    json.dump({"R2": {"launch_id": "L1", "worker": "w2"}}, open(os.path.join(tmp, "assignments.json"), "w"))
    rc_, got, downs, out = run({"R2"}, "done")
    ok(got == ["R2"] and "R1" in out and "no launch record" in out,
       f"RL4 a cfg label with no launch record is named and skipped, not a KeyError (got {got}, {out!r})")
    json.dump({"R1": {"launch_id": "L1", "worker": "w1"}, "R2": {"launch_id": "L1", "worker": "w2"}},
              open(os.path.join(tmp, "assignments.json"), "w"))
    seen, out = [], io.StringIO()
    with _patch_rc({"is_running": lambda n, lister=None: n == "rl", "attach": lambda n: _Cl(),
                                     "existing_client": lambda n: object(),     # 1E-ii: attach by id
                                     "_on_worker": lambda c, fn, label, worker: (seen.append(label)
                                                                                 or {"state": "running"})}), \
            contextlib.redirect_stdout(out):
        rc_main(["run_cluster.py", path, "status"])
    ok(seen == ["R1", "R2"] and "R2: running" in out.getvalue(),
       f"RL5 status reports every recorded label, R2 included ({seen}, {out.getvalue()!r})")


def _self_test_eval_configs(ok) -> None:
    """EV (harness benchmark protocol, owner 2026-11-21: an `ev` budget scope of $450): every eval config states
    its cap (US$35 per run, times its replicates), runs v2 with lead sign-off, 3 rounds and a US$35 model cap, and
    every cluster it makes (run, keeper, overflow) falls under the one scope `ev` of the tracked scopes file, so
    `budget_guard` can admit it once the owner signs that scope."""
    import glob as _glob
    import budget
    here = CLOUD_DIR
    paths = sorted(_glob.glob(os.path.join(here, "configs", "eval_*.py")))
    ok(len(paths) == 5, f"EV0 five eval configs: {[os.path.basename(p) for p in paths]}")
    ok(budget.load_scopes()["scopes"].get("ev") == ["ev-"],
       f"EV1 the tracked scopes file has scope ev with prefix ev-: {budget.load_scopes()['scopes']}")
    saved = {k: os.environ.pop(k, None) for k in ("EVAL_TAG", "EVAL_REPS", "EVAL_TREE", "EVAL_PIPELINE")}
    try:
        for reps in (1, 2):
            os.environ.update(EVAL_TAG="hb0", EVAL_REPS=str(reps))
            for p in paths:
                q = os.path.basename(p)[len("eval_"):-3]
                cfg = load_config(p)
                fl = list(cfg.flags)
                names = (cfg.name, cfg.keeper_name, cfg.overflow_name)
                ok(cfg.cap_usd == 35 * reps and cfg.budget_scope == "ev"
                   and all(budget.scope_for_name(n) == "ev" for n in names),
                   f"EV2 {q} (reps {reps}): cap_usd {cfg.cap_usd} = US$35 x {reps}, scope {cfg.budget_scope!r}, "
                   f"every cluster under ev: {[(n, budget.scope_for_name(n)) for n in names]}")
                ok(cfg.pipeline == "v2" and cfg.max_rounds == 3 and fl[fl.index("--max-budget-usd") + 1] == "35"
                   and fl[fl.index("--signoff") + 1] == "lead" and len(cfg.labels) == reps,
                   f"EV3 {q}: pipeline {cfg.pipeline}, rounds {cfg.max_rounds}, flags {fl}, labels {cfg.labels}")
                # Codex r1: each wrapper names its own question, and the unattended watcher (launch_watch) accepts
                # the config without --allow-no-cloud-limits: main.py stops at the protocol's 24 h cut-off
                ok(cfg.name == f"ev-{q.split('_')[0]}-hb0-gcc" and cfg.question_file.endswith(f"/{q}/question.txt")
                   and cfg.storage_prefix == f"runs/evals/hb0/{q}",
                   f"EV3b {q}: the config is its own question's ({cfg.name}, {cfg.question_file})")
                ok(cfg.run_timeout_hours == 24 and seconds(cfg.cluster_timeout) > 24 * 3600
                   and cfg.checkpoint_minutes > 0,
                   f"EV3c {q}: cloud-side limits {cfg.run_timeout_hours} h / {cfg.cluster_timeout!r}")
        os.environ.update(EVAL_TAG="Bad_Tag", EVAL_REPS="1")
        try:
            load_config(paths[0])
            ok(False, "EV4 a tag that would put the cluster name outside a-z, 0-9 and - is refused at load")
        except ValueError:
            ok(True, "EV4 a tag that would put the cluster name outside a-z, 0-9 and - is refused at load")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def _self_test_api_keys(ok) -> None:
    """RK1-RK3 (Codex round 1 on #358): a missing or blank Anthropic key refuses before any cluster is created, and
    a launch refused there releases the reservation its budget check made."""
    import types
    from unittest import mock
    import budget
    for keys in ({}, {"ANTHROPIC_API_KEY": "   "}, {"ANTHROPIC_API_KEY": ""}):
        with _patch_rc({"read_keys": lambda: dict(keys)}):
            try:
                api_keys()
                msg = None
            except SystemExit as e:
                msg = str(e)
            except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
                msg = f"{type(e).__name__}"
        ok(bool(msg) and "ANTHROPIC_API_KEY" in msg, f"RK1 a missing or blank key is refused: {sorted(keys)} ({msg})")
    made, recs, released = [], [], []
    fake = types.SimpleNamespace(Cluster=lambda **kw: made.append(kw))
    with mock.patch.dict(sys.modules, {"coiled": fake}), \
            mock.patch.object(budget, "release_reservation", lambda inv, name, clusters_file=None, env=None:
                              released.append((inv, name)) or 1, create=True), \
            _patch_rc({"is_running": lambda n: False, "code_tarball": lambda c: (b"", "abc"),
                                        "pipeline_preflight": lambda c, code: "v2", "budget_guard": lambda *a, **k: None,
                                        "stale_sheet_problems": lambda *a: [], "vm_request": lambda *a: {"run": {}},
                                        "_compute": lambda c: types.SimpleNamespace(WORKER_VM_TYPES=["x"]),
                                        "write_clusters": lambda *a, **k: recs.append(a),
                                        "read_keys": lambda: {"ANTHROPIC_API_KEY": " "}}):
        try:
            _launch_locked(RunConfig(name="ev-t-gcc", question_file="/dev/null", pipeline="v2", cap_usd=5), "inv-9")
            msg = None
        except SystemExit as e:
            msg = str(e)
        except Exception as e:  # noqa: BLE001
            msg = f"past the check: {type(e).__name__}"
    ok(bool(msg) and "ANTHROPIC_API_KEY" in msg and not made and not recs and released == [("inv-9", "ev-t-gcc")],
       f"RK2 a blank key: refused before any cluster is created or recorded, its reservation released ({msg}, "
       f"{len(made)} made, {released})")
    import inspect
    src = inspect.getsource(relaunch)
    ok("api_keys()" in src and src.index("budget_guard(") < src.index("api_keys()") < src.index("_results_store()"),
       "RK3 relaunch reads the keys right after its budget check, before any call to storage or the cluster")
    import tempfile
    real_read = read_keys
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, ".env")
        open(p, "w").write('ANTHROPIC_API_KEY="" # provision later\nOPENAI_API_KEY=sk-x # note\n')
        with _patch_rc({"read_keys": lambda: real_read(p)}):
            try:
                api_keys()
                msg = None
            except SystemExit as e:
                msg = str(e)
        open(p, "w").write('ANTHROPIC_API_KEY="\\t"\n')
        with _patch_rc({"read_keys": lambda: real_read(p)}):
            try:
                api_keys()
                msg6 = None
            except SystemExit as e:
                msg6 = str(e)
        ok(bool(msg6), f"RK6 (Codex r2 on #362) a double-quoted value is decoded before the blank check: \\t is blank ({msg6})")
        open(p, "w").write('ANTHROPIC_API_KEY="" # provision later\nOPENAI_API_KEY=sk-x # note\n')
        ok(bool(msg) and real_read(p).get("OPENAI_API_KEY") == "sk-x",
           f"RK4 (Codex r1 on #362) a quoted empty value with an inline comment is blank; comments are not values ({msg})")
    released.clear()
    with mock.patch.dict(sys.modules, {"coiled": fake}), \
            mock.patch.object(budget, "release_reservation", lambda inv, name, clusters_file=None, env=None:
                              released.append((inv, name)) or 1, create=True), \
            _patch_rc({"is_running": lambda n: False, "code_tarball": lambda c: (b"", "abc"),
                                        "pipeline_preflight": lambda c, code: "v2", "budget_guard": lambda *a, **k: None,
                                        "stale_sheet_problems": lambda *a: [], "vm_request": lambda *a: {"run": {}},
                                        "_compute": lambda c: types.SimpleNamespace(WORKER_VM_TYPES=["x"]),
                                        "read_keys": lambda: open("/nonexistent/.env").read()}):
        try:
            _launch_locked(RunConfig(name="ev-t-gcc", question_file="/dev/null", pipeline="v2", cap_usd=5), "inv-8")
            msg = None
        except SystemExit as e:
            msg = str(e)
        except Exception as e:  # noqa: BLE001
            msg = f"raised {type(e).__name__}"
    ok(bool(msg) and not msg.startswith("raised") and released == [("inv-8", "ev-t-gcc")],
       f"RK5 (Codex r1 on #362) an unreadable key file refuses the same way and releases the reservation ({msg})")


def _self_test_budget(ok) -> None:
    """BU (F6 Stage 2, 2026-11-11: $106 spent of a $100 budget): `launch` refuses unless the owner-signed budget
    (cloud/budget.py) covers the scope's spend plus the config's cap_usd, before any key is read or cluster made."""
    import types
    from unittest import mock
    import budget
    for bad in (dict(cap_usd=-1), dict(cap_usd=float("nan")), dict(cap_usd=True), dict(cap_usd="5")):
        try:
            RunConfig(name="t", question_file="/dev/null", **bad)
            ok(False, f"BU1 a cap_usd that is not a positive number is refused at load: {bad}")
        except (ValueError, TypeError):
            ok(True, f"BU1 a cap_usd that is not a positive number is refused at load: {bad}")
    ok(RunConfig(name="lw-smoke-x", question_file="q").budget_scope == "f6"
       and RunConfig(name="t", question_file="q").budget_scope == ""
       and RunConfig(name="t", question_file="q", budget_scope="f6").budget_scope == "f6",
       "BU2 budget_scope defaults to the scope whose prefix the config's name has (cloud/budget_scopes.yaml)")
    made, keys, kws = [], [], []
    calls, wvt = [], [["Standard_E8s_v5", "Standard_E8as_v5"]]

    def try_launch(cfg, verdict):
        made.clear(), keys.clear(), calls.clear()
        fake = types.SimpleNamespace(Cluster=lambda **kw: made.append(kw))
        lbo = lambda scope, cap, name=None, **kw: (calls.append((scope, cap, name)), kws.append(kw), verdict)[2]  # noqa: E731
        with mock.patch.dict(sys.modules, {"coiled": fake}), mock.patch.object(budget, "launch_budget_ok", lbo), \
                _patch_rc({"is_running": lambda n: False, "code_tarball": lambda c: (b"", "abc"),
                                            "pipeline_preflight": lambda c, code: "v2",
                                            "_compute": lambda c: types.SimpleNamespace(WORKER_VM_TYPES=wvt[0]),
                                            "read_keys": lambda: keys.append(1) or 1 / 0}):   # stops past the guard
            try:
                _launch_locked(cfg, "inv")
                return None
            except SystemExit as e:
                return str(e)
            except Exception as e:  # noqa: BLE001 -- past the guard, the fakes stop it
                return f"past the guard: {type(e).__name__}"
    msg = try_launch(RunConfig(name="lw-smoke-t", question_file="/dev/null", pipeline="v2"), (True, "ok"))
    ok(msg and "cap_usd" in msg and not made and not keys and not calls,
       f"BU3 a config without cap_usd is refused before the budget, any key or any cluster: {msg}")
    msg = try_launch(RunConfig(name="lw-smoke-t", question_file="/dev/null", pipeline="v2", cap_usd=20), (False, "over"))
    ok(msg and "budget" in msg and "over" in msg and not made and not keys and calls == [("f6", 20, "lw-smoke-t")],
       f"BU4 a launch the budget refuses creates nothing and reads no key: {msg} {calls}")
    msg = try_launch(RunConfig(name="zz", question_file="/dev/null", pipeline="v2", cap_usd=20), (True, "ok"))
    ok(msg and "scope" in msg and not made and not calls, f"BU4b a name in no budget scope is refused: {msg}")
    msg = try_launch(RunConfig(name="lw-smoke-t", question_file="/dev/null", pipeline="v2", cap_usd=20), (True, "ok"))
    ok(keys and calls == [("f6", 20, "lw-smoke-t")], f"BU4c a launch the budget covers goes on past the guard: {msg}")
    ok(kws and kws[-1].get("invocation") == "inv" and kws[-1].get("clusters_file", "").endswith("clusters.json"),
       f"BU4d launch reserves under its invocation token and names its clusters.json: {kws[-1:]}")
    ok(kws and all(k.get("warn_over") is True for k in kws),
       f"BU4e every launch check passes warn_over (owner 2026-11-17: no launch refused for dollars): {kws}")
    # BU7 (2026-11-13; owner: spend controls fail soft): a VM type missing from cloud/vm_prices.yaml never refuses a
    # launch. It is warned about before the budget is asked (budget.py counts it at the fallback rate)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        msg = try_launch(RunConfig(name="lw-smoke-t", question_file="/dev/null", pipeline="v2", cap_usd=20,
                                   overflow_vm_types=("Standard_D8s_v5", "Fake_X9_v9")), (True, "ok"))
    ok(msg and "past the guard" in msg and calls and "WARNING" in out.getvalue() and "Fake_X9_v9" in out.getvalue(),
       f"BU7 an unpriced overflow VM type warns and the launch goes on: {msg} {calls} {out.getvalue()[:200]!r}")
    wvt[0] = ["Standard_E8s_v5", "Fake_F8s_v2"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        msg = try_launch(RunConfig(name="lw-smoke-t", question_file="/dev/null", pipeline="v2", cap_usd=20),
                         (True, "ok"))
    wvt[0] = ["Standard_E8s_v5", "Standard_E8as_v5"]
    ok(msg and "past the guard" in msg and calls and "WARNING" in out.getvalue() and "Fake_F8s_v2" in out.getvalue(),
       f"BU7b an unpriced run-cluster worker type (compute.WORKER_VM_TYPES) warns the same way: {msg} {calls}")
    try:
        req = vm_request(RunConfig(name="lw-smoke-t", question_file="/dev/null", pipeline="v2", cap_usd=20),
                         types.SimpleNamespace(WORKER_VM_TYPES=["Standard_E8s_v5"]))
        prices = budget.load_prices(budget.PRICES_FILE)
        good = (set(req) == {"run", "overflow", "keeper"} and all(
            r["worker_vm_types"] and r["scheduler_vm_types"] and set(r["worker_vm_types"]) | set(r["scheduler_vm_types"])
            <= set(prices) for r in req.values()) and req["run"]["worker_vm_types"] == ["Standard_E8s_v5"])
    except Exception as e:  # noqa: BLE001 -- a missing function is a failing case
        req, good = f"{type(e).__name__}: {e}", False
    ok(good, f"BU7c vm_request pins every cluster's worker and scheduler types to priced ones: {req}")
    import tempfile
    with tempfile.TemporaryDirectory() as d:     # BU5-BU6 (Codex r1 P1 on #189): the other paths to paid work
        cfg = RunConfig(name="lw-smoke-t", question_file="/dev/null", pipeline="v2", cap_usd=20, out_dir=d)
        json.dump({"run": {"name": cfg.name, "id": 5}}, open(cfg.clusters_file, "w"))
        json.dump({"R1": {"launch_id": "L1", "worker": "w"}}, open(cfg.state_file, "w"))
        seen, started = [], []
        lbo = lambda scope, cap, name=None, reserve=True, **k: (seen.append(reserve), (False, "over"))[1]  # noqa: E731
        for label, fn in (("keeper", lambda: keeper_later(cfg)), ("relaunch", lambda: relaunch(cfg, "R1"))):
            seen.clear(), started.clear()
            with mock.patch.object(budget, "launch_budget_ok", lbo), \
                    _patch_rc({"live_id": lambda n: 5, "_reconcile": lambda *a: None,
                                                "start_helpers": lambda *a, **k: started.append(1),
                                                "read_keys": lambda: started.append("keys"),
                                                "_submit": lambda *a, **k: started.append("submit")}):
                try:
                    fn()
                    msg = None
                except SystemExit as e:
                    msg = str(e)
                except Exception as e:  # noqa: BLE001 -- past the guard, the missing fakes stop it
                    msg = f"past the guard: {type(e).__name__}: {e}"
            ok(msg and "over" in msg and not started and seen == ([True] if label == "keeper" else [False]),
               f"BU5 `{label}` is refused before it creates or submits anything when the budget no longer covers "
               f"cap_usd: {msg} reserve={seen}")
        seen.clear(), started.clear()
        ids = {"overflow": 7, "keeper": 8}
        with mock.patch.object(budget, "launch_budget_ok", lbo), \
                mock.patch.object(ws, "attach_existing", lambda *a: object()), \
                _patch_rc({"live_id": lambda n: 5, "_reconcile": lambda c, cl, role, n: ids[role],
                                            "confirm_keeper": lambda *a, **k: True, "_compute": lambda c: None,
                                            "start_helpers": lambda *a, **k: started.append(k)}), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                keeper_later(cfg)
                msg = None
            except BaseException as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
        ok(msg is None and not seen and started == [{"keep_overflow": 7, "keep_keeper": True}],
           f"BU5b a `keeper` whose helpers are healthy creates nothing and reserves nothing: {msg} {seen} {started}")
        seen.clear(), started.clear()
        bad = RunConfig(name="lw-smoke-t", question_file="/dev/null", pipeline="v2", cap_usd=20, out_dir=d,
                        overflow_vm_types=("Fake_X9_v9",))
        with mock.patch.object(budget, "launch_budget_ok", lambda s_, c, name=None, reserve=True, **k: (
                    seen.append(reserve), (True, "ok"))[1]), \
                _patch_rc({"live_id": lambda n: 5, "_reconcile": lambda *a: None,
                                            "_compute": lambda c: None,
                                            "start_helpers": lambda *a, **k: started.append(k)}), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                keeper_later(bad)
                msg = None
            except BaseException as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
        ok(msg is None and seen == [True] and started,
           f"BU7d `keeper` with an unpriced overflow VM type warns and goes on (fail soft): {msg} {seen}")
    import ast                                   # BU6: the archived launchers refuse unless deliberately allowed
    ld = os.path.join(REPO, "docs", "experiments", "launchers")
    for f in sorted(x for x in os.listdir(ld) if x.endswith(".py")):
        body = ast.parse(open(os.path.join(ld, f)).read()).body
        i = 1 if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            else 0
        if i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__":
            i += 1
        guard = body[i:i + 2]
        good = (len(guard) == 2 and isinstance(guard[0], ast.Import) and isinstance(guard[1], ast.If)
                and "MA_ALLOW_EXPERIMENT_LAUNCHERS" in ast.unparse(guard[1].test)
                and isinstance(guard[1].body[0], ast.Raise))
        refused = False
        if good:
            mod = ast.Module(body=guard, type_ignores=[])
            with mock.patch.dict(os.environ, {"MA_ALLOW_EXPERIMENT_LAUNCHERS": ""}):
                try:
                    exec(compile(mod, f, "exec"), {})   # noqa: S102 -- only the guard's two statements
                except SystemExit:
                    refused = True
        ok(good and refused, f"BU6 docs/experiments/launchers/{f} refuses at import unless "
                             f"MA_ALLOW_EXPERIMENT_LAUNCHERS=1 (its first statements are the guard)")

def _self_test_checkpoint(ok) -> None:
    """CK1-CK20 (F6 run checkpoints, 2026-11-11: a 3-h preview stopped at its cap stored nothing). The agent VM
    uploads a checkpoint of the run dir every checkpoint_minutes while main.py runs; `checkpoint` fetches it,
    sha256-verified; `collect` says when only a checkpoint exists. CK13-CK20: Codex round 1 (generations, disk,
    requests that never block a worker, ownership, one deadline). Fake store, fake main.py, no cloud."""
    import hashlib
    import tempfile
    import threading
    from unittest import mock
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import results_store
    ok(RunConfig(name="t", question_file="q").checkpoint_minutes == 15, "CK1 checkpoint_minutes defaults to 15")
    try:
        RunConfig(name="t", question_file="q", checkpoint_minutes=-1)
        ok(False, "CK1b a negative checkpoint_minutes is refused")
    except ValueError:
        ok(True, "CK1b a negative checkpoint_minutes is refused")
    import types
    got = []
    for minutes in (15, 0):
        sub = []
        fc = types.SimpleNamespace(submit=lambda *a, **kw: sub.append(kw) or "fut")
        with mock.patch.dict(sys.modules, {"distributed": types.SimpleNamespace(fire_and_forget=lambda f: None)}):
            _submit(RunConfig(name="z", question_file="q", pipeline="v2", checkpoint_minutes=minutes), fc, "R1", "w",
                    b"", "q", {"ANTHROPIC_API_KEY": "k"}, "L")
        got.append(sub[0].get("checkpoint_s", "absent"))
    ok(got == [900.0, None], f"CK2 the interval reaches run_to_disk in seconds; 0 sends None (disabled): {got}")
    ok(ws.checkpoint_starter("R1", "runs/t", "L1", None, None, None) is None
       and ws.checkpoint_starter("R1", "runs/t", "L1", None, None, 0) is None
       and callable(ws.checkpoint_starter("R1", "runs/t", "L1", None, None, 900.0)),
       "CK2b the agent VM starts no checkpoint thread when the interval is 0 or None")

    class Store:
        """results_store's archive helpers, uploads into a dict (optionally failing or blocking)."""
        write_archive, sha256_file = staticmethod(results_store.write_archive), staticmethod(results_store.sha256_file)

        def __init__(self, fail=False, gate=None, fail_on=None, raise_after=None):
            self.files, self.fail, self.gate, self.uploads, self.downloads = {}, fail, gate, [], []
            self.fail_on, self.raise_after = fail_on, raise_after

        def upload_file(self, path, blob, account=None):
            if self.gate is not None:
                self.gate.wait(10)
            if self.fail or (self.fail_on and self.fail_on(blob)):
                raise OSError("storage unreachable")
            self.files[blob] = open(path, "rb").read()
            self.uploads.append(blob)
            if self.raise_after and self.raise_after(blob):     # committed, then the read-back fails
                raise RuntimeError(f"upload of {blob} did not verify")
            return {"blob": blob, "bytes": len(self.files[blob])}

        def download(self, blob, path, account=None):
            self.downloads.append(blob)
            if blob not in self.files:
                raise FileNotFoundError(blob)
            open(path, "wb").write(self.files[blob])

        def delete(self, blob, account=None):
            self.files.pop(blob, None)

        def list_blobs(self, prefix, account=None):
            return [{"name": n} for n in sorted(self.files) if n.startswith(prefix)]

        def sha256_blob(self, blob, account=None):
            self.streamed = getattr(self, "streamed", []) + [blob]
            if blob not in self.files:
                raise FileNotFoundError(blob)
            return hashlib.sha256(self.files[blob]).hexdigest()

    secret = "sk-ant-TESTSECRET12345"
    main_py = ("import json, os, time\n"
               "rd = 'runs/R_x'\n"
               "for d in ('models', '_truth'):\n    os.makedirs(os.path.join(rd, d), exist_ok=True)\n"
               "open(rd + '/_truth/key.yaml', 'w').write('answer')\n"
               "json.dump({'state': 'ANALYZE', 'round': 2}, open(rd + '/run_state.json', 'w'))\n"
               "open(rd + '/models/b.jsonl.partial.jsonl', 'w').write("
               "json.dumps({'_partial': {'fn': 'f', 'code_fp': 'x'}}) + '\\n' + '{\"ok\": true, \"key\": \"k#0\"}\\n')\n"
               f"open(rd + '/notes.md', 'w').write('key {secret}')\n"
               "time.sleep(float(os.environ.get('CK_SLEEP', '1.5')))\n")
    import io as _io
    b = _io.BytesIO()
    with tarfile.open(fileobj=b, mode="w:gz") as t:
        ti = tarfile.TarInfo("main.py")
        ti.size = len(main_py.encode())
        t.addfile(ti, _io.BytesIO(main_py.encode()))
    code = b.getvalue()
    pointer = ws.checkpoint_pointer("runs/t", "L1", "R1")
    gen = lambda n, lb="R1": ws.checkpoint_blobs("runs/t", "L1", lb, n)  # noqa: E731
    ok(pointer == "outputs/runs/t/launch_L1/checkpoint_R1.json"
       and gen(2) == ("outputs/runs/t/launch_L1/checkpoint_R1_g2.tar.gz", "outputs/runs/t/launch_L1/checkpoint_R1_g2.json"),
       f"CK3 the blob names: a pointer, and immutable generations: {pointer} {gen(2)}")

    def run(store, interval_s=0.25):
        cps = []

        def on_start(work):
            c = ws.Checkpointer("R1", work, "runs/t", "L1", None, store, interval_s, secrets=[secret], poll_s=0.05,
                                log=lambda m: None)
            c.start()
            cps.append(c)
        with mock.patch.object(ws, "WORK", work_root), mock.patch.object(ws, "ensure_bwrap", lambda: "bwrap: test"):
            res = ws.run_job_impl("R1", code, "q", [], "k", None, on_start=on_start)
        cps[0].stop()
        cps[0].wait_idle(10)
        return res, cps[0]
    work_root = tempfile.mkdtemp()
    store = Store()
    res, c = run(store)
    rec = json.loads(store.files.get(pointer, b"{}"))
    n = rec.get("generation") or 0
    data = store.files.get(gen(n)[0], b"")
    ok(res["exit_code"] == 0 and c.done >= 2 and store.uploads[:3] == [gen(1)[0], gen(1)[1], pointer],
       f"CK4 while main.py runs: each interval a new generation (tarball, record), then the pointer: done={c.done} "
       f"{store.uploads[:3]}")
    ok(rec.get("sha256") == hashlib.sha256(data).hexdigest() and rec.get("bytes") == len(data)
       and rec.get("run_state") == {"state": "ANALYZE", "round": 2} and rec.get("cached_batches") == 1
       and rec.get("cached_rows") == 1 and rec.get("run_dir") == "R_x" and rec.get("time_utc")
       and json.loads(store.files[gen(n)[1]]).get("sha256") == rec["sha256"],
       f"CK5 the pointer names the latest generation; its record: time, size, sha256, state and round, batches: {rec}")
    with tarfile.open(fileobj=_io.BytesIO(data), mode="r:gz") as t:
        names = t.getnames()
        body = t.extractfile("runs/R_x/notes.md").read() if "runs/R_x/notes.md" in names else b""
    ok("runs/R_x/run_state.json" in names and "run.log" in names and not any("_truth" in n for n in names)
       and secret.encode() not in body and b"<redacted>" in body,
       f"CK6 the tarball holds the run dir and run.log, never _truth/, secrets redacted: {names}")
    ckdir = os.path.join(work_root, "R1", ws.CKDIR)
    ok(n >= 3 and gen(n - 2)[0] not in store.files and gen(n - 2)[1] not in store.files
       and gen(n - 1)[0] in store.files and rec.get("previous") == n - 1,
       f"CK13 (Codex P2-4) the last two generations are kept, older ones deleted: n={n} {sorted(store.files)}")
    ok(not any(f.endswith((".tar.gz", ".tmp")) or f.startswith("copy") for f in os.listdir(ckdir)),
       f"CK14 (Codex P2-5) the local archive is deleted after a successful upload: {os.listdir(ckdir)}")
    bad = Store(fail=True)
    res2, c2 = run(bad)
    ok(res2["exit_code"] == 0 and c2.failed >= 1 and c2.done == 0
       and "storage unreachable" in open(os.path.join(ckdir, "checkpoint.log")).read(),
       f"CK7 an upload failure is logged and never kills the run: exit={res2['exit_code']} failed={c2.failed}")
    ok(not any(f.endswith((".tar.gz", ".tmp")) for f in os.listdir(ckdir)),
       f"CK14b (Codex P2-5) a failed checkpoint leaves no partial or local archive: {os.listdir(ckdir)}")
    wd = os.path.join(work_root, "S")
    os.makedirs(os.path.join(wd, "runs", "R_y"))
    open(os.path.join(wd, "runs", "R_y", "a.txt"), "w").write("a")
    full = Store()
    c5 = ws.Checkpointer("S", wd, "runs/t", "L1", None, full, 3600, log=lambda m: None, free_bytes=lambda p: 2 * 2**30)
    c5.trigger("t")
    c5.wait_idle(10)
    ok(c5.done == 0 and not full.uploads and "free space" in open(os.path.join(wd, ws.CKDIR, "checkpoint.log")).read()
       and not any(f.endswith((".tar.gz", ".tmp")) for f in os.listdir(os.path.join(wd, ws.CKDIR))),
       "CK14c (Codex P2-5) less free space than 1.5 x the run dir + 2 GB: the checkpoint is skipped, logged")
    gate = threading.Event()
    slow = Store(gate=gate)
    c3 = ws.Checkpointer("S", wd, "runs/t", "L1", None, slow, 3600, log=lambda m: None)
    first, second = c3.trigger("t1"), c3.trigger("t2")
    gate.set()
    c3.wait_idle(10)
    ok(first and not second and c3.skipped == 1 and c3.done == 1,
       f"CK8 a checkpoint while the previous upload runs is skipped: {first} {second} skipped={c3.skipped}")
    # checkpoint-now: the side function only files a request and returns; it never sleeps on the worker
    import time as _time

    def no_sleep(*a):
        raise AssertionError("checkpoint_now_side slept on the worker")
    with mock.patch.object(ws, "WORK", work_root), mock.patch.object(_time, "sleep", no_sleep):
        none = ws.checkpoint_now_side("S")
        open(os.path.join(wd, ws.CKDIR, "active"), "w").write("1")
        t0 = _time.time()
        req = ws.checkpoint_now_side("S")
        dt_ = _time.time() - t0
    ok(none.get("state") == "no checkpointer" and req.get("state") == "requested" and req.get("token")
       and isinstance(req.get("epoch"), float) and dt_ < 0.5
       and os.path.exists(os.path.join(wd, ws.CKDIR, f"request_{req['token']}")),
       f"CK15 (Codex P1-3) checkpoint-now's side call files a request and returns at once: {none} {req} {dt_:.3f}s")
    # a request filed while an upload is busy stays pending; only a checkpoint started after it satisfies it
    gate2 = threading.Event()
    busy = Store(gate=gate2)
    for f in os.listdir(os.path.join(wd, ws.CKDIR)):
        if f.startswith("request_"):
            os.remove(os.path.join(wd, ws.CKDIR, f))
    c6 = ws.Checkpointer("S", wd, "runs/t", "L1", None, busy, 3600, poll_s=0.05, log=lambda m: None)
    c6.start()
    try:
        c6.trigger("interval")
        with mock.patch.object(ws, "WORK", work_root):
            req = ws.checkpoint_now_side("S")
        _time.sleep(0.4)
        gate2.set()
        for _ in range(100):
            if c6.done >= 2:
                break
            _time.sleep(0.05)
    finally:
        c6.stop()
        c6.wait_idle(10)
    g1, g2 = (json.loads(busy.files.get(ws.checkpoint_blobs("runs/t", "L1", "S", k)[1], b"{}")) for k in (1, 2))
    ok(req["token"] not in (g1.get("requests") or []) and req["token"] in (g2.get("requests") or [])
       and g2.get("started_epoch", 0) >= req["epoch"],
       f"CK16 (Codex P2-7) a request made during a busy upload is met only by a checkpoint started after it: "
       f"{g1.get('requests')} {g2.get('requests')}")
    # the record's state and counts come from the archived files, not the live run dir

    class Crafted(Store):
        @staticmethod
        def write_archive(root, arcname, out_path, secrets=None, extra=(), **kw):
            with tarfile.open(out_path, mode="w:gz") as t:
                for name, txt in ((f"{arcname}/run_state.json", '{"state": "WRITE", "round": 3}'),
                                  (f"{arcname}/analysis/batch/r.jsonl", '{"ok": true}\n{"ok": true}\n'),
                                  (f"{arcname}/analysis/batch/r.jsonl.summary.json", "{}")):
                    ti = tarfile.TarInfo(name)
                    ti.size = len(txt)
                    t.addfile(ti, _io.BytesIO(txt.encode()))
            return []
    # the live run_state equals the archived one (else the capture is torn, CK30); the live dir has no batch
    open(os.path.join(wd, "runs", "R_y", "run_state.json"), "w").write('{"state": "WRITE", "round": 3}')
    r17 = ws.build_checkpoint(wd, os.path.join(wd, "runs", "R_y"), os.path.join(wd, "x.tar.gz"), Crafted())
    os.remove(os.path.join(wd, "runs", "R_y", "run_state.json"))
    ok(r17.get("run_state") == {"state": "WRITE", "round": 3} and r17.get("cached_batches") == 1
       and r17.get("cached_rows") == 2,
       f"CK17 (Codex P1-1) the record's state and counts are read from the archive, not the live dir: {r17}")
    # fetch on the laptop: through the pointer, sha256 verified, extracted under <out_dir>/<label>/checkpoint/
    out = tempfile.mkdtemp()
    cfg = RunConfig(name="t", question_file="q", out_dir=out, labels=("R1",))
    json.dump({"R1": {"launch_id": "L1", "worker": "w"}}, open(cfg.state_file, "w"))
    with contextlib.redirect_stdout(io.StringIO()):
        got = fetch_checkpoint(cfg, store, "R1")
    dest = os.path.join(out, "R1", "checkpoint")
    ok(got.get("sha256") == rec["sha256"] and os.path.exists(os.path.join(dest, "runs", "R_x", "run_state.json"))
       and json.load(open(os.path.join(dest, "checkpoint.json"))).get("sha256") == rec["sha256"],
       f"CK10 `checkpoint` reads the pointer, downloads, verifies and extracts the latest generation: {got}")
    try:
        mk10 = json.load(open(os.path.join(dest, "runs", "R_x", "copied_run.json")))
    except (OSError, ValueError):
        mk10 = {}
    ok(list(mk10.get("files") or {}) == ["models/b.jsonl.partial.jsonl"] and "checkpoint R1" in str(mk10.get("source"))
       and mk10.get("captured_at") == got.get("time_utc"),
       f"CK10b (F6 run 3) the fetched run dir is marked: its batch journal is a copy, not a live batch ({mk10})")
    ok(_checkpoint_run_root(dest, "R_x") == os.path.join(dest, "runs", "R_x")
       and all(_checkpoint_run_root(dest, bad) is None for bad in ("../R1", "/tmp", "runs/R_x", "", "..", None, "nope")),
       "CK10c (Codex review of #256 P1-4) the record's run_dir is one name of a directory in the extraction, or none")
    store.files[gen(n)[0]] = data + b"tampered"
    out3 = tempfile.mkdtemp()
    cfg3 = RunConfig(name="t", question_file="q", out_dir=out3, labels=("R1",))
    json.dump({"R1": {"launch_id": "L1", "worker": "w"}}, open(cfg3.state_file, "w"))
    with contextlib.redirect_stdout(io.StringIO()) as buf3:
        got3 = fetch_checkpoint(cfg3, store, "R1")
    ok(got3.get("generation") == n - 1 and "previous" in buf3.getvalue()
       and os.path.exists(os.path.join(out3, "R1", "checkpoint", "runs", "R_x", "run_state.json")),
       f"CK18 (Codex P2-4) a latest generation that does not verify: the previous complete one is fetched: {got3}")
    store.files[gen(n - 1)[0]] = b"tampered too"
    out2 = tempfile.mkdtemp()
    cfg2 = RunConfig(name="t", question_file="q", out_dir=out2, labels=("R1",))
    json.dump({"R1": {"launch_id": "L1", "worker": "w"}}, open(cfg2.state_file, "w"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fetch_checkpoint(cfg2, store, "R1")
        ok(False, "CK11 no generation whose tarball matches its record: refused")
    except SystemExit as e:
        ok("sha256" in str(e) and not os.path.exists(os.path.join(out2, "R1", "checkpoint")),
           f"CK11 no generation whose tarball matches its record: refused, nothing extracted: {e}")
    store.files[gen(n)[0]] = data
    with _patch_rc({"is_running": lambda n, lister=None: False,
                                     "collect_from_storage": lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("x")),
                                     "checkpoint_record": lambda c, s, lb, lid: rec}), \
            contextlib.redirect_stdout(io.StringIO()) as buf:
        collect(cfg2, shutdown_after=False)
    ok("only a checkpoint" in buf.getvalue() and "checkpoint --label R1" in buf.getvalue(),
       f"CK12 `collect` reports a run with only a checkpoint (no final result): {buf.getvalue()!r}")
    ok(checkpoint_record(cfg2, store, "R1", "L1") == rec and checkpoint_record(cfg2, store, "R1", "L9") is None,
       "CK12b the checkpoint record of a launch (its pointer), None when there is none")
    # checkpoint-now on the laptop: the recorded run cluster by id, this launch only; all labels, one deadline
    out4 = tempfile.mkdtemp()
    cfg4 = RunConfig(name="t", question_file="q", out_dir=out4, labels=("A", "B"))
    json.dump({lb: {"launch_id": "L1", "worker": f"w{lb}"} for lb in "AB"}, open(cfg4.state_file, "w"))
    json.dump({"run": {"name": "t", "id": 77}, "launch_id": "L1", "invocation": "tok"}, open(cfg4.clusters_file, "w"))
    log4, clock = [], [1000.0]

    class Client:
        def run(self, fn, *a, workers=None, wait=True, on_error="raise"):
            log4.append(("run", fn.__name__, a[0]))
            return {workers[0]: {"state": "requested", "token": f"T{a[0]}", "epoch": 500.0}}
    st4 = Store()
    st4.files[ws.checkpoint_pointer(cfg4.storage_prefix, "L1", "A")] = json.dumps(
        {"generation": 4, "requests": ["TA"], "started_epoch": 501.0}).encode()
    st4.files[ws.checkpoint_pointer(cfg4.storage_prefix, "L1", "B")] = json.dumps(
        {"generation": 2, "requests": [], "started_epoch": 400.0}).encode()

    def sleep4(s):
        log4.append(("sleep", s))
        clock[0] += s

    def cnow(**kw):
        with _patch_rc({"attach": lambda *a: (_ for _ in ()).throw(AssertionError("attach by name"))}), \
                contextlib.redirect_stdout(io.StringIO()) as b4:
            try:
                rc_ = checkpoint_now(cfg4, **{"timeout_s": 60, "store": st4, "sleep": sleep4, "clock": lambda: clock[0],
                                              "connect": lambda c, cid: log4.append(("connect", cid)) or Client(),
                                              "poll_s": 10, **kw})
            except SystemExit as e:
                rc_ = f"refused: {e}"
        return rc_, b4.getvalue()
    for kw, why in (({"invocation": "other"}, "another invocation's clusters.json"),
                    ({"launch_id": "L0"}, "another launch id")):
        r19, _ = cnow(**kw)
        ok(isinstance(r19, str) and "refused" in r19 and not any(e[0] == "run" for e in log4),
           f"CK19 (Codex P2-6) checkpoint-now refuses {why}, sends nothing: {r19}")
    r20, out20 = cnow(invocation="tok", launch_id="L1")
    runs_ = [i for i, e in enumerate(log4) if e[0] == "run"]
    sleeps = [i for i, e in enumerate(log4) if e[0] == "sleep"]
    ok(("connect", 77) in log4 and len(runs_) == 2 and max(runs_) < min(sleeps) and r20 == 1
       and clock[0] - 1000.0 <= 60 + 10 and "A: checkpointed" in out20 and "B: timeout" in out20,
       f"CK20 (Codex P1-3, P2-6, P2-8) by recorded id; every label requested first, then storage polled for the "
       f"token under one deadline: rc={r20} elapsed={clock[0] - 1000.0} {out20!r}")
    # Codex round 2 P1-2: a checkpointer recreated on a lost work dir continues the stored generations
    keep = Store(fail_on=lambda blob: blob.endswith(("_g3.json", "_g4.json")))
    for k in (1, 2):
        keep.files[gen(k)[0]], keep.files[gen(k)[1]] = f"tar{k}".encode(), json.dumps({"generation": k}).encode()
    keep.files[pointer] = json.dumps({"generation": 2, "previous": 1}).encode()
    before = dict(keep.files)
    wd2 = os.path.join(tempfile.mkdtemp(), "R1")
    os.makedirs(os.path.join(wd2, "runs", "R_z"))
    open(os.path.join(wd2, "runs", "R_z", "a.txt"), "w").write("a")
    c7 = ws.Checkpointer("R1", wd2, "runs/t", "L1", None, keep, 3600, log=lambda m: None)
    for _ in range(2):
        c7.trigger("t")
        c7.wait_idle(10)
    ok(c7.failed == 2 and all(keep.files.get(b) == v for b, v in before.items())
       and not any(b.endswith(("_g1.tar.gz", "_g2.tar.gz")) for b in keep.uploads),
       f"CK21 (Codex round 2 P1-2) recreated on a lost work dir with g1, g2 stored, two failed record uploads: "
       f"g1, g2 and the pointer intact: uploads {keep.uploads}")
    keep.fail_on = None
    c7.trigger("t")
    c7.wait_idle(10)
    p21 = json.loads(keep.files[pointer])
    ok(p21.get("generation") == 5 and p21.get("previous") == 2 and gen(2)[0] in keep.files
       and gen(1)[0] not in keep.files and gen(5)[0] in keep.files,
       f"CK21b the next success is g5 with previous g2; only generations older than both are deleted, after the "
       f"pointer: {p21} {sorted(keep.files)}")
    # Codex round 2 P2-3: the disk check covers the archive being written and the copy of the largest file
    big_dir = os.path.join(tempfile.mkdtemp(), "runs", "R_b")
    os.makedirs(big_dir)
    with open(os.path.join(big_dir, "big.bin"), "wb") as f:
        f.truncate(10 * 2**30)                    # a sparse 10 GiB file: its size, not its disk use
    open(os.path.join(big_dir, "small.txt"), "w").write("x" * 1000)
    need = ws.disk_needed(big_dir, None)
    need_log = ws.disk_needed(big_dir, os.path.join(big_dir, "small.txt"))
    os.remove(os.path.join(big_dir, "big.bin"))
    ok(need >= 2.2 * (10 * 2**30 + 10 * 2**30 + 1000) + 2 * 2**30
       and abs(need_log - (need + 2.2 * 1000)) < 1,
       f"CK22 (Codex round 2 P2-3) a 10 GiB incompressible file needs 2.2 x (largest + run dir) + 2 GB free, "
       f"run.log counted as a file: {need / 2**30:.1f} GiB")
    small_dir = os.path.join(tempfile.mkdtemp(), "R_s")
    os.makedirs(small_dir)
    open(os.path.join(small_dir, "a.txt"), "w").write("a")
    big_log = os.path.join(os.path.dirname(small_dir), "run.log")
    with open(big_log, "wb") as f:
        f.truncate(10 * 2**30)
    need_b = ws.disk_needed(small_dir, big_log)
    os.remove(big_log)
    ok(need_b >= 2.2 * (2 * 10 * 2**30) + 2 * 2**30,
       f"CK22b (Codex round 3 P2-4) a 10 GiB run.log beside a small run dir counts twice (its copy and the archive): "
       f"{need_b / 2**30:.1f} GiB")
    # Codex round 2 P2-4: a request whose checkpoint is skipped or fails is retried within retry_s, not at the next
    # interval
    wd3 = os.path.join(tempfile.mkdtemp(), "Q")
    os.makedirs(os.path.join(wd3, "runs", "R_q"))
    open(os.path.join(wd3, "runs", "R_q", "a.txt"), "w").write("a")
    frees = iter([0] + [2**50] * 50)
    flaky = Store(fail_on=lambda blob, n=[0]: blob.endswith(".tar.gz") and n.append(1) is None and len(n) == 2)
    c8 = ws.Checkpointer("Q", wd3, "runs/t", "L1", None, flaky, 3600, poll_s=0.05, log=lambda m: None,
                         free_bytes=lambda p: next(frees), retry_s=0.2)
    c8.start()
    try:
        with mock.patch.object(ws, "WORK", os.path.dirname(wd3)):
            req8 = ws.checkpoint_now_side("Q")
        for _ in range(100):
            if c8.done:
                break
            _time.sleep(0.05)
    finally:
        c8.stop()
        c8.wait_idle(10)
    p8 = json.loads(flaky.files.get(ws.checkpoint_pointer("runs/t", "L1", "Q"), b"{}"))
    ok(req8["token"] in (p8.get("requests") or []) and c8.failed == 1 and c8.done == 1,
       f"CK23 (Codex round 2 P2-4) a request survives a disk skip and a failed upload, met within seconds: "
       f"done={c8.done} failed={c8.failed} {p8.get('requests')}")
    # Codex round 2 P2-5: requests go to every worker at once; an unresponsive one does not hold up the others

    class SlowA:
        def __init__(self):
            self.when, self.release = {}, threading.Event()

        def run(self, fn, *a, workers=None, wait=True, on_error="raise"):
            self.when[a[0]] = _time.time()
            if a[0] == "A":
                self.release.wait(10)
            return {workers[0]: {"state": "requested", "token": f"T{a[0]}", "epoch": 1.0}}
    sa = SlowA()
    st5 = Store()
    st5.files[ws.checkpoint_pointer(cfg4.storage_prefix, "L1", "B")] = json.dumps(
        {"generation": 1, "requests": ["TB"], "started_epoch": 2.0}).encode()
    t0 = _time.time()
    with contextlib.redirect_stdout(io.StringIO()) as b5:
        r24 = checkpoint_now(cfg4, timeout_s=2, store=st5, connect=lambda c, cid: sa, poll_s=0.1)
    took = _time.time() - t0
    sa.release.set()
    ok(r24 == 1 and sa.when.get("B", 99) - t0 < 1.0 and took < 4 and "B: checkpointed" in b5.getvalue()
       and "A: unavailable" in b5.getvalue(),
       f"CK24 (Codex round 2 P2-5) with worker A unresponsive, B is requested at once and the call ends at its "
       f"deadline: B at {sa.when.get('B', 99) - t0:.2f}s, took {took:.1f}s {b5.getvalue()!r}")
    # Codex round 3 P1-2: a pointer upload that may have committed protects its generation
    wd9 = os.path.join(tempfile.mkdtemp(), "P")
    os.makedirs(os.path.join(wd9, "runs", "R_p"))
    open(os.path.join(wd9, "runs", "R_p", "a.txt"), "w").write("a")
    ptr9 = ws.checkpoint_pointer("runs/t", "L1", "P")
    g9 = lambda k: ws.checkpoint_blobs("runs/t", "L1", "P", k)  # noqa: E731
    st9 = Store()
    c9 = ws.Checkpointer("P", wd9, "runs/t", "L1", None, st9, 3600, log=lambda m: None)
    for k in range(3):
        st9.raise_after = (lambda blob: blob == ptr9) if k else None
        c9.trigger("t")
        c9.wait_idle(10)
    p9 = json.loads(st9.files[ptr9])
    out9 = tempfile.mkdtemp()
    cfg9 = RunConfig(name="t", question_file="q", out_dir=out9, labels=("P",), storage_prefix="runs/t")
    json.dump({"P": {"launch_id": "L1", "worker": "w"}}, open(cfg9.state_file, "w"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            f9 = fetch_checkpoint(cfg9, st9, "P").get("generation")
    except SystemExit as e:
        f9 = f"refused: {e}"
    ok(c9.failed == 2 and p9.get("generation") == 3 and p9.get("previous") == 2 and g9(3)[0] in st9.files
       and g9(2)[0] in st9.files and f9 == 3,
       f"CK25 (Codex round 3 P1-2) two pointer uploads that committed, then failed their read-back: the generations "
       f"they name are kept and fetchable: pointer {p9.get('generation')}/{p9.get('previous')} fetch {f9} "
       f"{sorted(st9.files)}")
    st9.raise_after = None
    c9.trigger("t")
    c9.wait_idle(10)
    p9 = json.loads(st9.files[ptr9])
    ok(p9.get("generation") == 4 and p9.get("previous") == 3 and g9(3)[0] in st9.files and g9(1)[0] not in st9.files,
       f"CK25b the next success names g4 and g3; only older generations are retired: {sorted(st9.files)}")
    # Codex round 3 P2-3: generations without a pointer: the newest complete one is adopted as the base
    st10 = Store()
    g10 = lambda k: ws.checkpoint_blobs("runs/t", "L1", "N", k)  # noqa: E731
    st10.files[g10(1)[0]] = b"tar one"
    st10.files[g10(1)[1]] = json.dumps({"generation": 1, "sha256": hashlib.sha256(b"tar one").hexdigest()}).encode()
    st10.files[g10(2)[0]] = b"tar two, its record never written"
    wd10 = os.path.join(tempfile.mkdtemp(), "N")
    os.makedirs(os.path.join(wd10, "runs", "R_n"))
    open(os.path.join(wd10, "runs", "R_n", "a.txt"), "w").write("a")
    c10 = ws.Checkpointer("N", wd10, "runs/t", "L1", None, st10, 3600, log=lambda m: None)
    c10.trigger("t")
    c10.wait_idle(10)
    p10 = json.loads(st10.files.get(ws.checkpoint_pointer("runs/t", "L1", "N"), b"{}"))
    ok(c10.done == 1 and p10.get("generation") == 3 and p10.get("previous") == 1 and st10.files[g10(1)[0]] == b"tar one",
       f"CK26 (Codex round 3 P2-3) generations but no pointer (the VM died before the first): the newest complete one "
       f"is the base, and checkpoints go on: {p10.get('generation')}/{p10.get('previous')}")

    class Unreadable(Store):
        def download(self, blob, path, account=None):
            if blob.endswith("checkpoint_U.json"):
                raise PermissionError("pointer unreadable")
            return super().download(blob, path, account)
    st11 = Unreadable()
    st11.files[ws.checkpoint_pointer("runs/t", "L1", "U")] = b"{}"
    st11.files[ws.checkpoint_blobs("runs/t", "L1", "U", 1)[0]] = b"t"
    before11 = dict(st11.files)
    c11 = ws.Checkpointer("U", wd10, "runs/t", "L1", None, st11, 3600, log=lambda m: None)
    c11.trigger("t")
    c11.wait_idle(10)
    ok(c11.failed == 1 and st11.files == before11,
       "CK26b a pointer that exists but cannot be read: no checkpoint, nothing numbered or deleted")
    ok(not any(b.endswith(".tar.gz") for b in st10.downloads) and g10(1)[0] in getattr(st10, "streamed", []),
       f"CK32 (Codex round 4 P2) adoption checks a tarball's sha256 by streaming it, never downloading it: "
       f"downloads {st10.downloads}")
    # Codex round 4 P2: `checkpoint` without a pointer adopts the newest complete generation, as the uploader does
    st15 = Store()
    g15 = lambda k: ws.checkpoint_blobs("runs/t", "L1", "R1", k)  # noqa: E731
    with open(os.path.join(work_root, "g2.tar.gz"), "wb") as f:
        with tarfile.open(fileobj=f, mode="w:gz") as t:
            ti = tarfile.TarInfo("runs/R_x/run_state.json")
            body15 = b'{"state": "ANALYZE", "round": 1}'
            ti.size = len(body15)
            t.addfile(ti, _io.BytesIO(body15))
    tar15 = open(os.path.join(work_root, "g2.tar.gz"), "rb").read()
    st15.files[g15(2)[0]] = tar15
    st15.files[g15(2)[1]] = json.dumps({"generation": 2, "sha256": hashlib.sha256(tar15).hexdigest(),
                                        "run_dir": "R_x"}).encode()
    st15.files[g15(3)[0]] = b"g3: its record was never written"
    out15 = tempfile.mkdtemp()
    cfg15 = RunConfig(name="t", question_file="q", out_dir=out15, labels=("R1",), storage_prefix="runs/t")
    json.dump({"R1": {"launch_id": "L1", "worker": "w"}}, open(cfg15.state_file, "w"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            f15 = fetch_checkpoint(cfg15, st15, "R1").get("generation")
    except SystemExit as e:
        f15 = f"refused: {e}"
    ok(f15 == 2 and os.path.exists(os.path.join(out15, "R1", "checkpoint", "runs", "R_x", "run_state.json")),
       f"CK31 (Codex round 4 P2) `checkpoint` with generations but no pointer fetches the newest complete one: {f15}")
    # Codex round 4 P1: a capture during which run_state.json changed (a report quarantined, FINAL written) is
    # discarded, never uploaded, and retried once on the next tick
    wd16 = os.path.join(tempfile.mkdtemp(), "F")
    rd16 = os.path.join(wd16, "runs", "R_f")
    os.makedirs(rd16)
    open(os.path.join(rd16, "report.md"), "w").write("# a report the gate will withhold\n")
    open(os.path.join(rd16, "run_state.json"), "w").write('{"state": "WRITE", "round": 2}')
    once = []

    def quarantine(rel):
        if rel == "run_state.json" and not once:     # report.md already copied; main.py then quarantines it
            once.append(1)
            os.makedirs(os.path.join(rd16, "quarantine"), exist_ok=True)
            os.replace(os.path.join(rd16, "report.md"), os.path.join(rd16, "quarantine", "report.md"))
            open(os.path.join(rd16, "run_state.json"), "w").write(
                '{"state": "FINAL", "round": 2, "status": "completed_no_report"}')

    class Racing(Store):
        @staticmethod
        def write_archive(root, arcname, out_path, secrets=None, extra=(), **kw):
            return results_store.write_archive(root, arcname, out_path, secrets, extra, _hook=quarantine, **kw)
    st16 = Racing()
    c16 = ws.Checkpointer("F", wd16, "runs/t", "L1", None, st16, 3600, poll_s=0.05, log=lambda m: None)
    c16.start()
    try:
        c16.trigger("t")
        for _ in range(100):
            if c16.done:
                break
            _time.sleep(0.05)
    finally:
        c16.stop()
        c16.wait_idle(10)
    tars16 = [b for b in st16.uploads if b.endswith(".tar.gz")]
    names16 = (tarfile.open(fileobj=_io.BytesIO(st16.files[tars16[0]]), mode="r:gz").getnames() if tars16 else [])
    rs16 = (json.load(tarfile.open(fileobj=_io.BytesIO(st16.files[tars16[0]]), mode="r:gz").extractfile(
        "runs/R_f/run_state.json")) if tars16 else {})
    ok(len(tars16) == 1 and "runs/R_f/report.md" not in names16 and rs16.get("state") == "FINAL"
       and c16.done == 1 and "changed" in open(os.path.join(wd16, ws.CKDIR, "checkpoint.log")).read(),
       f"CK30 (Codex round 4 P1) report copied, then quarantined and FINAL written: that capture is discarded, the "
       f"retry on the next tick uploads a consistent one: {tars16} {names16} {rs16}")
    # Codex round 3 P2-5: each request counts its own tries; exhausting one drops only that one
    c12 = ws.Checkpointer("Z", wd10, "runs/t", "L1", None, Store(), 3600, log=lambda m: None)
    c12._requeue({"old": (1.0, ws.MAX_REQUEST_TRIES), "new": (2.0, 0)}, "failed")
    ok(set(c12.pending) == {"new"} and c12.pending["new"][1] == 1,
       f"CK27 (Codex round 3 P2-5) an exhausted request is dropped; one that arrived meanwhile keeps its tries: "
       f"{c12.pending}")
    # Codex round 3 note: the deadline is checked before every blocking call, with no grace after it
    labels30 = tuple(f"L{k}" for k in range(30))
    out13 = tempfile.mkdtemp()
    cfg13 = RunConfig(name="t", question_file="q", out_dir=out13, labels=labels30)
    json.dump({lb: {"launch_id": "L1", "worker": f"w{lb}"} for lb in labels30}, open(cfg13.state_file, "w"))
    json.dump({"run": {"name": "t", "id": 77}, "launch_id": "L1", "invocation": "tok"}, open(cfg13.clusters_file, "w"))

    class Fast:
        def run(self, fn, *a, workers=None, wait=True, on_error="raise"):
            return {workers[0]: {"state": "requested", "token": "T", "epoch": 1.0}}

    class Hanging(Store):
        def download(self, blob, path, account=None):
            _time.sleep(2)
            raise FileNotFoundError(blob)
    t0 = _time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        r28 = checkpoint_now(cfg13, timeout_s=0.3, store=Hanging(), connect=lambda c, cid: Fast(), poll_s=0.1)
    took = _time.time() - t0
    # Codex round 3 P1-1 through the checkpointer: work/runs linked into _truth/ is never archived
    tr = tempfile.mkdtemp()
    os.makedirs(os.path.join(tr, "_truth", "R_t"))
    open(os.path.join(tr, "_truth", "R_t", "answers.yaml"), "w").write("answer\n")
    wd14 = os.path.join(tr, "W")
    os.makedirs(wd14)
    os.symlink(os.path.join(tr, "_truth"), os.path.join(wd14, "runs"))
    st14 = Store()
    c14 = ws.Checkpointer("W", wd14, "runs/t", "L1", None, st14, 3600, log=lambda m: None)
    c14.trigger("t")
    c14.wait_idle(10)
    ok(c14.failed == 1 and not st14.uploads,
       f"CK29 (Codex round 3 P1-1) a work dir whose runs/ links into _truth/: nothing archived or uploaded "
       f"({st14.uploads})")
    ok(r28 == 1 and took < 1.0,
       f"CK28 (Codex round 3) 30 labels whose storage reads hang: the call ends at its 0.3 s deadline ({took:.2f}s)")


def _self_test_storage(ok) -> None:
    """WS1-WS6 (v2 autonomy 1E-i): the agent VM's end-of-run paths never hold an archive whole, the returned
    tarball keeps large files when main.py's own store failed, teardown waits for a checkpoint upload in flight,
    and the checkpointer records its health. Fake store, fake main.py, no cloud."""
    import hashlib
    import io
    import tempfile
    import threading
    from unittest import mock
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import results_store

    def code(store_error, tail=""):
        main_py = ("import json, os\nrd = 'runs/R_x'\nos.makedirs(rd)\n"
                   "open(rd + '/big.bin', 'wb').write(b'x' * 4096)\nopen(rd + '/small.txt', 'w').write('s')\n"
                   f"f = open(rd + '/metadata.json', 'w')\n"
                   f"json.dump({{'stored': 'outputs/t/R_x.tar.gz', 'store_error': {store_error!r}}}, f)\n"
                   "f.close()\n" + tail)
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            ti = tarfile.TarInfo("main.py")
            ti.size = len(main_py.encode())
            t.addfile(ti, io.BytesIO(main_py.encode()))
        return b.getvalue()
    root = tempfile.mkdtemp()
    got = {}
    for label, err in (("OK", None), ("BAD", "OSError: storage unreachable")):
        with mock.patch.object(ws, "WORK", root), mock.patch.object(ws, "ensure_bwrap", lambda: "bwrap: test"), \
                mock.patch.object(ws, "MAX_RETURN_FILE", 1024):
            res = ws.run_job_impl(label, code(err), "q", [], "k", None)
        tar = res.get("tarball")
        on_disk = isinstance(tar, str) and os.path.dirname(tar) == os.path.join(root, label) and os.path.exists(tar)
        names = tarfile.open(tar, mode="r:gz").getnames() if on_disk else []
        got[label] = (on_disk, "runs/R_x/big.bin" in names, [p for p, _ in res["skipped_large_files"]],
                      ws.run_stored(tar) if on_disk else "?")
    ok(got["OK"] == (True, False, ["runs/R_x/big.bin"], "outputs/t/R_x.tar.gz"),
       f"WS1 the returned tarball is written to disk, never returned as bytes; a stored run's large files stay in "
       f"storage only: {got['OK']}")
    ok(got["BAD"] == (True, True, [], None),
       f"WS2 WS_FALLBACK_LARGE_FILE main.py's store failed: the tarball held for collect keeps every large file: "
       f"{got['BAD']}")

    # Codex 1E-i round 1. WS2b: a receipt in metadata.json counts only when main.py ended by itself; a run killed
    # (its time limit, a signal) may carry the receipt of an earlier invocation it resumed. WS2c: a disk too full
    # for the complete tarball still yields one without the large files, and the task goes on to hold it.
    with mock.patch.object(ws, "WORK", root), mock.patch.object(ws, "ensure_bwrap", lambda: "bwrap: test"), \
            mock.patch.object(ws, "MAX_RETURN_FILE", 1024):
        res = ws.run_job_impl("KILLED", code(None, "os.kill(os.getpid(), 9)\n"), "q", [], "k", None)
    names = tarfile.open(res["tarball"], mode="r:gz").getnames()
    ok(res["exit_code"] == -9 and "runs/R_x/big.bin" in names and res["skipped_large_files"] == []
       and ws.run_stored(res["tarball"], res["exit_code"]) is None
       and ws.run_stored(res["tarball"], "timeout") is None and ws.run_stored(res["tarball"], 2) == "outputs/t/R_x.tar.gz",
       f"WS2b a killed main.py: the receipt in metadata.json is not trusted (large files kept, no stored run "
       f"recorded): exit {res['exit_code']} {names}")
    # WS2d (Codex 1E-i round 2): a link an agent left at the tarball's staging name is removed, never written through
    # WS2e (round 3): a directory at the old staging name does not stop the tarball; a link to a large file outside
    # the run is left out as before (the collector's extraction refuses such a member), whatever the store did
    planted = ("os.makedirs('runs/R_x/models')\nopen('runs/R_x/models/out.csv', 'w').write('result')\n"
               "os.makedirs('result.tar.gz.tmp')\nopen('../outside.bin', 'wb').write(b'y' * 4096)\n"
               "os.symlink(os.path.abspath('../outside.bin'), 'runs/R_x/models/big_link')\n"
               "os.symlink('gone_checkpoint', 'runs/R_x/models/dangling')\n"
               "os.symlink('runs/R_x/models/out.csv', 'result.tar.gz')\n")
    with mock.patch.object(ws, "WORK", root), mock.patch.object(ws, "ensure_bwrap", lambda: "bwrap: test"), \
            mock.patch.object(ws, "MAX_RETURN_FILE", 1024):
        try:
            res = ws.run_job_impl("LINK", code("x", planted), "q", [], "k", None)
        except OSError as e:
            res = {"tarball": None, "skipped_large_files": [], "error": f"{type(e).__name__}: {e}"}
    kept = open(os.path.join(root, "LINK", "runs", "R_x", "models", "out.csv"), "rb").read()[:20]
    names = tarfile.open(res["tarball"], mode="r:gz").getnames() if res["tarball"] else []
    ok(kept == b"result" and res["tarball"] and not os.path.islink(res["tarball"]) and "runs/R_x/models/out.csv" in names,
       f"WS2d a link planted at result.tar.gz is replaced, a directory at the old staging name is no obstacle; the "
       f"output is intact: {kept!r} {res.get('error')}")
    ok("runs/R_x/big.bin" in names and "runs/R_x/models/big_link" not in names
       and [p for p, _ in res["skipped_large_files"]] == ["runs/R_x/models/big_link"],
       f"WS2e the store failed: large files are kept, a link to a large file is still left out: {names}")
    ok("runs/R_x/models/dangling" in names,
       f"WS2f (Codex pass on round 3) a link whose target is gone is archived as a link; it does not end the task "
       f"before the hold: {res.get('error')}")
    real_add = tarfile.TarFile.add

    def full_disk(self, name, *a, **kw):
        if str(name).endswith("big.bin"):
            raise OSError(28, "No space left on device")
        return real_add(self, name, *a, **kw)
    with mock.patch.object(ws, "WORK", root), mock.patch.object(ws, "ensure_bwrap", lambda: "bwrap: test"), \
            mock.patch.object(ws, "MAX_RETURN_FILE", 1024), mock.patch.object(tarfile.TarFile, "add", full_disk):
        try:
            res = ws.run_job_impl("FULL", code("OSError: storage unreachable"), "q", [], "k", None)
        except OSError as e:
            res = {"error": str(e), "skipped_large_files": [], "notes": []}
    names = tarfile.open(res["tarball"], mode="r:gz").getnames() if res.get("tarball") else []
    ok("runs/R_x/small.txt" in names and "run.log" in names and "runs/R_x/big.bin" not in names
       and [p for p, _ in res["skipped_large_files"]] == ["runs/R_x/big.bin"]
       and any("large files" in n for n in res["notes"]) and not [n for n in os.listdir(os.path.join(root, "FULL")) if n.endswith(".tmp")],
       f"WS2c no room for the complete tarball: the task still returns one without the large files and says so: "
       f"{names} {res.get('error')} {res['notes'][-1:]}")

    class Store:
        write_archive, sha256_file = staticmethod(results_store.write_archive), staticmethod(results_store.sha256_file)
        is_sensitive_path = staticmethod(results_store.is_sensitive_path)

        def __init__(self, gate=None, fail=False):
            self.files, self.gate, self.fail = {}, gate, fail

        def archive_dir(self, *a, **kw):
            raise AssertionError("archive_dir: the archive would be built in memory")

        def upload_file(self, path, blob, account=None):
            if self.gate is not None:
                self.gate.wait(10)
            if self.fail:
                raise OSError("storage unreachable")
            self.files[blob] = open(path, "rb").read()
            return {"blob": blob}
        upload_retry = upload_file

        def download(self, blob, path, account=None):
            open(path, "wb").write(self.files[blob])

        def list_blobs(self, prefix, account=None):
            return [{"name": n} for n in sorted(self.files) if n.startswith(prefix)]

        def delete(self, blob, account=None):
            self.files.pop(blob, None)
    home, d = os.path.join(root, "home"), os.path.join(root, "OK")
    st = Store()
    blob = ws.store_transcripts(d, st, "runs/t", "L1", "OK", None, ["sk-ant-TESTSECRET12345"], home=home)
    os.makedirs(os.path.join(home, "s1"))
    open(os.path.join(home, "s1", "t.jsonl"), "w").write("key sk-ant-TESTSECRET12345\n")
    # WS3b (round 3): a launch tree pinned before this change ships a write_archive without `strict`; and a link an
    # agent left at transcripts.tar.gz is replaced, never written through (it could point at result.tar.gz)
    class OldStore(Store):
        @staticmethod
        def write_archive(root, arcname, out_path, secrets=None, extra=(), _hook=None, base=None):
            return results_store.write_archive(root, arcname, out_path, secrets, extra)
    victim = os.path.join(d, "result.tar.gz")
    before = open(victim, "rb").read()
    os.remove(os.path.join(d, "transcripts.tar.gz"))
    os.symlink(victim, os.path.join(d, "transcripts.tar.gz"))
    try:
        old = ws.store_transcripts(d, OldStore(), "runs/t", "L1", "OK", None, [], home=home)
    except Exception as e:  # noqa: BLE001
        old = f"{type(e).__name__}: {e}"
    ok(old == "outputs/runs/t/launch_L1/transcripts_OK.tar.gz" and open(victim, "rb").read() == before
       and not os.path.islink(os.path.join(d, "transcripts.tar.gz")),
       f"WS3b transcripts store with an older tree's write_archive, and never through a planted link: {old}")
    blob2 = ws.store_transcripts(d, st, "runs/t", "L1", "OK", None, ["sk-ant-TESTSECRET12345"], home=home)
    body = tarfile.open(fileobj=io.BytesIO(st.files.get(blob2, b"")), mode="r:gz").extractfile("transcripts/s1/t.jsonl").read()
    ok(blob == blob2 == "outputs/runs/t/launch_L1/transcripts_OK.tar.gz" and b"TESTSECRET" not in body,
       f"WS3 the transcripts are archived by the streaming writer, redacted; a VM with no transcripts yet still "
       f"stores an (empty) archive: {blob} {body!r}")
    out = os.path.join(root, "out")
    os.makedirs(out)
    open(os.path.join(out, "a.bin"), "wb").write(b"a" * 3000)
    with mock.patch.object(ws, "_sha_file", mock.Mock(wraps=getattr(ws, "_sha_file", None)), create=True) as spy:
        rec = ws.store_outputs(out, "outputs/j", None, st)
    ok(rec["verified"] and rec["files"] == {"a.bin": hashlib.sha256(b"a" * 3000).hexdigest()} and spy.call_count == 2,
       f"WS4 store_outputs hashes each file and its read-back block by block (ws._sha_file), never with one "
       f"whole-file read: {rec['why']} calls={spy.call_count}")
    # WS5 WS_CK_WAIT_IDLE: teardown stops the timer, then waits for the upload in flight (the cluster may shut down
    # right after). WS6: the counters reach .checkpoint/status.json after every checkpoint, stored or failed.
    work = os.path.join(root, "W")
    os.makedirs(os.path.join(work, "runs", "R_x"))
    json.dump({"state": "ANALYZE", "round": 1}, open(os.path.join(work, "runs", "R_x", "run_state.json"), "w"))
    gate = threading.Event()
    st5 = Store(gate=gate)
    c = ws.Checkpointer("W", work, "runs/t", "L1", None, st5, 3600, log=lambda m: None)
    c.start()
    c.trigger("t")
    threading.Timer(0.4, gate.set).start()
    box = type("B", (), {"box": c})
    h5 = ws.stop_checkpointer(box, idle_s=10)
    ok(c.done == 1 and ws.checkpoint_pointer("runs/t", "L1", "W") in st5.files
       and (h5 or {}).get("done") == 1 and (h5 or {}).get("last_outcome") == "stopped",
       f"WS5 WS_CK_WAIT_IDLE teardown returns only once the checkpoint in flight is stored, with the health record "
       f"for the result record (done={c.done}): {h5}")
    ok(ws.stop_checkpointer(None) is None and ws.stop_checkpointer(type("B", (), {"box": None})) is None,
       "WS5b no checkpointer (checkpoints off, or main.py never started): nothing to stop")
    sp = os.path.join(work, ws.CKDIR, "status.json")
    s1 = json.load(open(sp)) if os.path.exists(sp) else {}
    st5.fail, before = True, time.time()
    c.trigger("t2")
    c.wait_idle(10)
    s2 = json.load(open(sp)) if os.path.exists(sp) else {}
    ok((s1.get("done"), s1.get("failed"), s1.get("generation"), s1.get("last_error")) == (1, 0, 1, None)
       and s1.get("last_ok_epoch") and s1.get("label") == "W",
       f"WS6 a stored checkpoint is recorded in .checkpoint/status.json: {s1}")
    ok((s2.get("done"), s2.get("failed")) == (1, 1) and "storage unreachable" in str(s2.get("last_error"))
       and s2.get("last_ok_epoch") == s1.get("last_ok_epoch") and (s2.get("updated_epoch") or 0) >= before,
       f"WS6b a failed checkpoint is counted with its error; the last good time is kept: {s2}")


def _self_test_liveness(ok) -> None:
    """LV1-LV9 (v2 autonomy 1E-ii): the agent VM's heartbeat record from before pip, fetch_meta's preparing and
    dead, per-label isolation in `status`, no attach by name on the read paths, and a label whose large files
    exist only on the VM stays pending in `collect`. Fakes only; no cloud."""
    import types
    import subprocess
    import tempfile
    from unittest import mock
    root = tempfile.mkdtemp()
    with mock.patch.object(ws, "WORK", root):
        ok(ws.fetch_meta("N")["state"] == "missing", "LV1 no state record and no run.log: missing (unchanged)")
        os.makedirs(os.path.join(root, "OLD"))
        open(os.path.join(root, "OLD", "run.log"), "w").write("x")
        ok(ws.fetch_meta("OLD")["state"] == "running", "LV1b a run.log with no state record (an older worker): running")
        seen = {}

        def fake_run(td, label, code, *a, state=None, **kw):
            seen["td"], seen["during"] = os.path.isdir(td), ws.fetch_meta(label)
            state.set("running")
            os.makedirs(os.path.join(root, label), exist_ok=True)
            open(os.path.join(root, label, "run.log"), "w").write("log")
            seen["running"] = ws.fetch_meta(label)
            t0 = json.load(open(ws.state_path(label)))["updated_at"]
            time.sleep(0.25)
            seen["beat"] = json.load(open(ws.state_path(label)))["updated_at"] > t0
            if kw.get("boom"):
                raise RuntimeError("pip failed")
            state.set("done")
            return {"label": label}
        with mock.patch.object(ws, "_run_to_disk", fake_run), mock.patch.object(ws, "STATE_BEAT_S", 0.05):
            ws.run_to_disk("A", b"")
            a_during, a_running, a_beat = seen["during"], seen["running"], seen["beat"]
            try:
                ws.run_to_disk("B", b"", boom=True)
            except RuntimeError:
                pass
        ok(a_during["state"] == "preparing" and a_during.get("phase") == "preparing",
           f"LV2 before pip and unpacking end, the label reads preparing, not missing: {a_during}")
        ok(a_running["state"] == "running" and a_beat,
           f"LV3 main.py started: running; the record is rewritten on a timer: {a_running} beat={a_beat}")
        # LV4b (Codex round 1): a failure before the task body (no temporary directory) also ends the record
        import threading
        with mock.patch.object(tempfile, "mkdtemp", side_effect=OSError("no space")), \
                mock.patch.object(ws, "STATE_BEAT_S", 0.05):
            try:
                ws.run_to_disk("C", b"")
            except OSError:
                pass
        time.sleep(0.2)
        c = ws.fetch_meta("C")
        ok(c["state"] == "dead" and c.get("phase") == "failed"
           and not [t for t in threading.enumerate() if t.name == "ma-state-C"],
           f"LV4b a task that fails before unpacking reads dead and leaves no heartbeat thread: {c}")
        b = ws.fetch_meta("B")
        ok(b["state"] == "dead" and b.get("phase") == "failed",
           f"LV4 LW_DEAD_PID_DETECTED the task ended without a result (a run.log exists): dead, not running: {b}")
        # LV5c (Codex round 3): the record is written under a fresh name each time; a link at a predictable name
        # (an agent's) is never written through, and a link at the record's own name is replaced
        victim = os.path.join(root, "victim.txt")
        open(victim, "w").write("keep")
        os.symlink(victim, ws.state_path("LK"))
        os.symlink(victim, f"{ws.state_path('LK')}.{os.getpid()}.tmp")
        ws.RunState("LK").set("running")
        ok(open(victim).read() == "keep" and not os.path.islink(ws.state_path("LK"))
           and json.load(open(ws.state_path("LK")))["phase"] == "running",
           "LV5c the state record is never written through a link")
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        for label, rec in (("STALE", {"phase": "running", "pid": os.getpid(), "updated_at": time.time() - 3600}),
                           ("NOPID", {"phase": "running", "pid": gone.pid, "updated_at": time.time()}),
                           ("JUNK", "not a record")):
            json.dump(rec, open(ws.state_path(label), "w"))
        ok(ws.fetch_meta("STALE")["state"] == "dead" and ws.fetch_meta("NOPID")["state"] == "dead",
           "LV5 a heartbeat older than the limit, or one whose process is gone, reads dead")
        ok(ws.fetch_meta("JUNK")["state"] == "missing", "LV5b an unreadable state record changes nothing")
    ok(relaunch_refusal("preparing", False) and relaunch_refusal("dead", False)
       and relaunch_refusal("missing", False) is None,
       "LV6 a preparing run is never re-fired; a dead one keeps its work dir until someone has looked")
    # LV7: one label whose VM cannot be read does not hide the others; LV8: nothing on these paths attaches by name
    tmp = tempfile.mkdtemp()
    cfg = RunConfig(name="lv", question_file="q", labels=("A", "B"), out_dir=tmp, tree=REPO)
    json.dump({lb: {"launch_id": "L1", "worker": f"w{lb}"} for lb in "AB"}, open(cfg.state_file, "w"))
    no_attach = lambda *a, **k: (_ for _ in ()).throw(AssertionError("attach by name"))  # noqa: E731

    def on_worker(client, fn, label, *a, worker=None):
        if label == "A":
            raise OSError("worker unreachable")
        return {"state": "running", "log_bytes": 3}
    out = io.StringIO()
    with _patch_rc({"is_running": lambda n, lister=None: n == "lv", "attach": no_attach,
                                     "connect_existing": lambda n, *a, **k: object(), "_on_worker": on_worker}), \
            contextlib.redirect_stdout(out):
        try:
            status(cfg)
        except BaseException as e:  # noqa: BLE001
            print(f"raised {type(e).__name__}: {e}")
    text = out.getvalue()
    ok("A: unknown" in text and "OSError" in text and "B: running" in text and "raised" not in text,
       f"LV7 status isolates a label whose worker cannot be read and never attaches by name: {text!r}")
    stops = []
    with _patch_rc({"live_id": lambda n, lister=None: {"lv": 11}.get(n), "attach": no_attach,
                                     "_stop_id": lambda n, cid: stops.append((n, cid)),
                                     "connect_existing": lambda n, *a, **k: None}), \
            contextlib.redirect_stdout(io.StringIO()):
        shutdown(cfg)
        try:
            existing_client("lv")
            gone_msg = None
        except SystemExit as e:
            gone_msg = str(e)
    ok(stops == [("lv", 11)] and gone_msg and "not running" in gone_msg,
       f"LV8 shutdown stops each live cluster by its id; a cluster that is gone is refused, never created: {stops}")
    # LV9: the tarball on the VM lacks large files that are in no store: collected, but the label stays pending, it
    # is not marked collected and nothing is shut down
    marks, downs = [], []

    def on_worker9(client, fn, label, *a, worker=None):
        if fn is ws.mark_collected:
            marks.append(label)
        return {"state": "done"}

    def download9(client, label, w, tgz):
        with tarfile.open(tgz, mode="w:gz"):
            pass
        if label == "A":      # a worker from before this change: no unstored_large_files field (Codex round 1)
            return {"exit_code": 0, "duration_s": 1, "run_stored": None, "skipped_large_files": [["runs/R/big.bin", 9]]}
        return {"exit_code": 0, "duration_s": 1, "run_stored": None, "unstored_large_files": [],
                "skipped_large_files": [["runs/R/a_link", 9]]}
    rs = types.ModuleType("results_store")
    out = io.StringIO()
    with mock.patch.dict(sys.modules, {"results_store": rs}), \
            _patch_rc({"is_running": lambda n, lister=None: True, "attach": no_attach,
                                        "connect_existing": lambda n, *a, **k: object(),
                                        "collect_from_storage": lambda *a, **k: (_ for _ in ()).throw(KeyError("x")),
                                        "checkpoint_record": lambda *a, **k: None, "_on_worker": on_worker9,
                                        "_download": download9, "shutdown": lambda c: downs.append(c.name)}), \
            contextlib.redirect_stdout(out):
        collect(cfg)
    ok(marks == ["B"] and not downs and "A: " in out.getvalue() and "only on the VM" in out.getvalue(),
       f"LV9 a label whose large files exist only on its VM is fetched but stays pending; no shutdown: {marks} "
       f"{downs} {out.getvalue()!r}")
    with mock.patch.dict(sys.modules, {"results_store": rs}), \
            _patch_rc({"is_running": lambda n, lister=None: True, "attach": no_attach,
                                        "connect_existing": lambda n, *a, **k: object(),
                                        "collect_from_storage": lambda *a, **k: (_ for _ in ()).throw(KeyError("x")),
                                        "checkpoint_record": lambda *a, **k: None, "_on_worker": on_worker9,
                                        "_download": download9, "shutdown": lambda c: downs.append(c.name)}), \
            contextlib.redirect_stdout(io.StringIO()):
        try:
            collect(cfg, accept_partial=True)
        except TypeError as e:
            downs.append(str(e))
    # LV9c, LV9d (Codex round 2): a partial copy is this launch's or an earlier one's by its own record; and
    # --accept-partial finishes a partial copy even when the VM is gone
    tmp2 = tempfile.mkdtemp()
    pd = os.path.join(tmp2, "P")
    os.makedirs(pd)
    json.dump({"launch_id": "L0"}, open(os.path.join(pd, "result.partial.json"), "w"))
    ok(local_is_current(pd, "L1") is False and local_is_current(pd, "L0") is None,
       "LV9c a partial copy of an earlier launch is set aside before a new collect; this launch's is fetched again")
    cfg2 = RunConfig(name="lv2", question_file="q", labels=("P",), out_dir=tmp2, tree=REPO)
    json.dump({"P": {"launch_id": "L0", "worker": "w"}}, open(cfg2.state_file, "w"))
    with mock.patch.dict(sys.modules, {"results_store": rs}), \
            _patch_rc({"is_running": lambda n, lister=None: False, "attach": no_attach,
                                        "collect_from_storage": lambda *a, **k: (_ for _ in ()).throw(KeyError("x")),
                                        "checkpoint_record": lambda *a, **k: None}), \
            contextlib.redirect_stdout(io.StringIO()):
        collect(cfg2, accept_partial=True)
    ok(os.path.exists(os.path.join(pd, "result.json")) and not os.path.exists(os.path.join(pd, "result.partial.json")),
       "LV9d `collect --accept-partial` after the cluster stopped finishes this launch's partial copy")
    ok(marks == ["B", "A"] and downs == ["lv"] and os.path.exists(os.path.join(tmp, "A", "result.json")),
       f"LV9b `collect --accept-partial` on a live VM fetches again and tells the VM (its hold ends): "
       f"{marks} {downs}")


def _self_test_sheet(ok) -> None:
    """2026-11-12: a canary relaunch died in 8 s at SIGN because its spec was amended after its sign-off sheet was
    generated; the pre-launch checks did not look at the sheet. A launch now refuses a resumed run at SIGN whose
    sheet does not record the shipped spec's and derivation record's sha256."""
    import hashlib

    root = REPO        # the launch check runs the archive's own code (#218 r4): ship this tree's
    names = subprocess.run(["git", "-C", root, "ls-files", "toolkit", "agents", "scripts", "cloud"],
                           capture_output=True, text=True).stdout.split()
    code_files = {n: open(os.path.join(root, n), "rb").read() for n in names if os.path.isfile(os.path.join(root, n))}

    def tar_of(files: dict) -> bytes:
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w:gz") as t:
            for n, data in {**code_files, **files}.items():
                ti = tarfile.TarInfo(n)
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
        return bio.getvalue()

    spec, rec = b"spec: 1\n", b'{"r": 1}'
    R = "runs/r1"
    flags = ("--resume", R, "--signoff", "lead")
    st = lambda s_: json.dumps({"state": s_}).encode()      # noqa: E731
    base = {f"{R}/run_state.json": st("SIGN"), f"{R}/decision_spec.yaml": spec,
            f"{R}/data/derivations_record.json": rec}
    fresh = stale_sheet_problems(tar_of(base), flags)
    nospec = stale_sheet_problems(tar_of({k: v for k, v in base.items() if not k.endswith(".yaml")}), flags)
    # #218 review round 1: a run past SIGN ships the spec its sign-off hashed (with that record's evidence binding)
    def rec_for(sp, ev=None):
        return json.dumps({"sha256": {"decision_spec.yaml": hashlib.sha256(sp).hexdigest()},
                           "evidence": ev or {"required": [], "dossier": None, "sha256": None,
                                                 "declarations": {}, "closure": dict(sorted({   # its input closure
                                                     "run:decision_spec.yaml": hashlib.sha256(sp).hexdigest(),
                                                     "run:data/derivations_record.json":
                                                         hashlib.sha256(rec).hexdigest()}.items()))}}).encode()
    signed = stale_sheet_problems(tar_of({**base, f"{R}/run_state.json": st("ANALYZE"),
                                          f"{R}/decision_spec.signoff.json": rec_for(spec)}), flags)
    norun = stale_sheet_problems(tar_of(base), ("--signoff", "lead"))
    ok(fresh == [] and signed == [] and norun == [],
       f"RC80a a valid SIGN run, a run past SIGN and a launch with no resumed run pass: {fresh} {signed} {norun}")
    # RC80f (v2 autonomy 1B-ii): EVIDENCE now runs before SIGN, so a run there with no sign-off record is a draft;
    # one with a record (signed under the earlier order) is checked as signed
    ev_draft = stale_sheet_problems(tar_of({**base, f"{R}/run_state.json": st("EVIDENCE"),
                                            f"{R}/decision_spec.yaml": b"spec: 2\n"}), flags)
    ev_signed = stale_sheet_problems(tar_of({**base, f"{R}/run_state.json": st("EVIDENCE"),
                                             f"{R}/decision_spec.yaml": b"spec: 2\n",
                                             f"{R}/decision_spec.signoff.json": rec_for(spec)}), flags)
    ev_lost = stale_sheet_problems(tar_of({**base, f"{R}/run_state.json": json.dumps({"state": "EVIDENCE", "history": [
        {"from": "SIGN", "to": "EVIDENCE", "why": "spec signed by the lead"}]}).encode()}), flags)
    ok(ev_draft == [] and ev_signed and ev_lost,
       f"RC80f a run at EVIDENCE with no sign-off is a draft; one signed under the earlier order (a record, or a "
       f"signature in its history) is checked "
       f"({ev_draft}; {ev_signed}; {ev_lost})")
    ok(len(nospec) == 1, f"RC80c fail closed: missing decision_spec.yaml at SIGN is refused: {nospec}")
    # Codex review of #207 (both reviewers, P2): v2 only; ./ and --resume= forms are checked; an unreadable
    # run state is refused, not skipped.
    stale_tar = tar_of({k: v for k, v in base.items() if not k.endswith(".yaml")})
    v1 = stale_sheet_problems(stale_tar, flags, pipeline="v1")
    dot = stale_sheet_problems(stale_tar, ("--resume", "./" + R))
    eq = stale_sheet_problems(stale_tar, ("--resume=" + R,))
    bad_state = stale_sheet_problems(tar_of({**base, f"{R}/run_state.json": b"{not json"}), flags)
    ok(v1 == [] and len(dot) == 1 and len(eq) == 1 and len(bad_state) == 1 and "unreadable" in bad_state[0],
       f"RC80d v1 launches are not checked; ./ and --resume= are; an unreadable run state is refused: "
       f"{v1} {dot} {eq} {bad_state}")
    # Codex review round 2 of #207 (P2): a run whose saved sign-off mode is none skips SIGN (not checked); the
    # resume flag is read as main.py's argparse reads it (an abbreviation, the last value wins).
    none_mode = stale_sheet_problems(tar_of({**base, f"{R}/run_state.json": json.dumps({"state": "SIGN",
                                     "signoff": "none"}).encode(), f"{R}/decision_spec.yaml": b"spec: 2\n"}), flags)
    # RC81 (F6 evidence dossier): the signature binds the dossier the spec names; a launch refuses a
    # run whose shipped dossier is not the one its sign-off (past SIGN) or current preconditions (at SIGN) expect
    dspec, dos = b"spec: 1\nevidence: {dossier: evidence_dossier.yaml}\n", b"inputs: {}\n"

    if root not in sys.path:
        sys.path.insert(0, root)
    from toolkit.spec import evidence_dossier as ED

    def closure_of(sp, rc, ds):         # the input closure state() computes for these run files (no engine)
        return dict(sorted({"run:decision_spec.yaml": hashlib.sha256(sp).hexdigest(),
                            "run:evidence_dossier.yaml": hashlib.sha256(ds).hexdigest(),
                            "run:data/derivations_record.json": hashlib.sha256(rc).hexdigest()}.items()))

    dbase = {**base, f"{R}/decision_spec.yaml": dspec, f"{R}/evidence_dossier.yaml": dos}
    d_fresh = stale_sheet_problems(tar_of(dbase), flags)
    d_gone = stale_sheet_problems(tar_of({k: v for k, v in dbase.items() if not k.endswith("dossier.yaml")}), flags)
    signed_rec = json.dumps({"sha256": {"evidence_dossier.yaml": hashlib.sha256(dos).hexdigest(),
                                        "decision_spec.yaml": hashlib.sha256(dspec).hexdigest()},
                             "evidence": {"required": [], "dossier": "evidence_dossier.yaml",
                                          "sha256": hashlib.sha256(dos).hexdigest(), "declarations": {},
                                          "closure": closure_of(dspec, rec, dos)}}).encode()
    past = {**dbase, f"{R}/run_state.json": st("ANALYZE"), f"{R}/decision_spec.signoff.json": signed_rec}
    p_ok = stale_sheet_problems(tar_of(past), flags)
    p_edit = stale_sheet_problems(tar_of({**past, f"{R}/evidence_dossier.yaml": b"inputs: {x: 1}\n"}), flags)
    ok(d_fresh == [] and p_ok == []
       and any("does not exist" in x for x in d_gone) and any("not the one the sign-off recorded" in x for x in p_edit),
       f"RC81 a missing dossier at SIGN or edited dossier past SIGN refuses the launch; unchanged ones pass: "
       f"{d_fresh} {d_gone} {p_ok} {p_edit}")
    # RC82 (#218 review round 1)
    nspec = (b"options: {components: {n: {}}, engine_map: {n: {net: pyr}}}\n"
             b"engine: {ref: 'toolkit.engines.malaria.engine:MalariaEngine'}\n")
    spec_state = stale_sheet_problems(tar_of({**dbase, f"{R}/run_state.json": st("SPEC")}), flags)
    edited = stale_sheet_problems(tar_of({**past, f"{R}/decision_spec.yaml": b"spec: 1\n"}), flags)
    legacy = stale_sheet_problems(tar_of({**base, f"{R}/run_state.json": st("ANALYZE"), f"{R}/decision_spec.yaml": nspec,
                                          f"{R}/decision_spec.signoff.json": json.dumps({"sha256": {
                                              "decision_spec.yaml": hashlib.sha256(nspec).hexdigest()}}).encode()}),
                                  flags)
    unsigned = stale_sheet_problems(tar_of({**base, f"{R}/run_state.json": st("ANALYZE")}), flags)
    ok(spec_state == [] and any("not the one signed" in x for x in edited)
       and any("unsigned_evidence" in x for x in legacy) and any("no sign-off record" in x for x in unsigned),
       f"RC82 a draft at SPEC is not refused; past SIGN, a spec edited after its sign-off (removing `evidence` too), a "
       f"sign-off made before the dossier requirement of a spec that needs one, and no sign-off at all are refused: "
       f"{spec_state} {edited} {legacy} {unsigned}")
    abbrev = stale_sheet_problems(stale_tar, ("--res", R))
    last = stale_sheet_problems(stale_tar, ("--resume", "runs/old", "--resume=" + R))
    ok(none_mode == [] and len(abbrev) == 1 and len(last) == 1 and "runs/old" not in last[0],
       f"RC80e signoff none is not checked; --res is read; only the last --resume counts: {none_mode} {abbrev} {last}")


def _self_test_replicate_names(ok, tb) -> None:
    """UN1-UN5 (baseline hb3 §5.1: dengue R1 and R2 started in the same second, got one run dir name and one stored
    blob, and R2's upload overwrote R1's). main.py is told each run's label and launch; collection maps a label to its
    own stored run and refuses a stored run that two labels' records both name, or that names another label."""
    import inspect
    import shutil
    import tempfile
    import types
    from unittest import mock

    class Store:
        def __init__(self, files):
            self.files = files

        def download(self, blob, path, account):
            if blob not in self.files:
                raise FileNotFoundError(blob)
            open(path, "wb").write(self.files[blob])

    def rec(blob):
        return json.dumps({"exit_code": 0, "duration_s": 5, "run_stored": blob}).encode()

    def run(label=None):
        meta = {"run_dir": "r", **({"run_label": label} if label else {})}
        return tb({"r/metadata.json": json.dumps(meta).encode(), "r/report.md": (label or "old").encode()})
    p, lid, sib = "runs/t", "L1", {"R1": "L1", "R2": "L1"}
    old = "outputs/runs/t/r.tar.gz"
    b1, b2 = (f"outputs/runs/t/launch_L1/run_{x}_r.tar.gz" for x in ("R1", "R2"))
    with tempfile.TemporaryDirectory() as d:
        cfg = types.SimpleNamespace(out_dir=d, storage_prefix=p, storage_account=None)
        st = Store({ws.record_blob(p, lid, "R1"): rec(old), ws.record_blob(p, lid, "R2"): rec(old), old: run()})
        for lb in ("R1", "R2"):
            try:
                collect_from_storage(cfg, st, lb, lid, None, siblings=sib)
                got = "collected"
            except StoredRunRefused as e:
                got = str(e)
            ok("R1" in got and "R2" in got and not os.path.exists(os.path.join(d, lb, "runs")),
               f"UN1 {lb}: a stored run that two labels' records both name is refused, nothing extracted ({got[:140]})")
        st = Store({ws.record_blob(p, lid, "R1"): rec(b1), ws.record_blob(p, lid, "R2"): rec(b2),
                    b1: run("R1"), b2: run("R2")})
        for lb in ("R1", "R2"):
            collect_from_storage(cfg, st, lb, lid, None, siblings=sib)
        ok(all(open(os.path.join(d, lb, "runs", "r", "report.md")).read() == lb for lb in ("R1", "R2")),
           "UN2 each label is collected from its own stored run")
    with tempfile.TemporaryDirectory() as d:
        cfg = types.SimpleNamespace(out_dir=d, storage_prefix=p, storage_account=None)
        try:
            collect_from_storage(cfg, Store({ws.record_blob(p, lid, "R2"): rec(b1), b1: run("R1")}), "R2", lid, None)
            got = "collected"
        except StoredRunRefused as e:
            got = str(e)
        ok("R1" in got and not os.path.exists(os.path.join(d, "R2", "runs")),
           f"UN3 a stored run that records another label is refused for this one ({got[:140]})")
        m = collect_from_storage(cfg, Store({ws.record_blob(p, lid, "R1"): rec(old), old: run()}), "R1", lid, None,
                                 siblings={"R1": lid})
        ok(m["exit_code"] == 0 and open(os.path.join(d, "R1", "runs", "r", "report.md")).read() == "old",
           "UN4 a run stored under the old name, which no other label's record names, is still collected")
        l1 = tb({"r/metadata.json": json.dumps({"run_label": "R3", "launch_id": "L0"}).encode()})
        try:
            collect_from_storage(cfg, Store({ws.record_blob(p, lid, "R3"): rec(b1), b1: l1}), "R3", lid, None)
            got = "collected"
        except StoredRunRefused as e:
            got = str(e)
        ok("L0" in got and not os.path.exists(os.path.join(d, "R3", "runs")),
           f"UN6 (Codex r1) a stored run that records another launch is refused for this one ({got[:140]})")

        class Flaky(Store):
            def download(self, blob, path, account):
                if blob == ws.record_blob(p, lid, "R2"):
                    raise OSError("timed out")
                return super().download(blob, path, account)
        try:
            collect_from_storage(cfg, Flaky({ws.record_blob(p, lid, "R4"): rec(old), old: run()}), "R4", lid, None,
                                 siblings={"R4": lid, "R2": lid})
            got = "collected"
        except OSError as e:
            got = str(e)
        ok(got == "timed out" and not os.path.exists(os.path.join(d, "R4", "runs")),
           f"UN7 (Codex r1) a sibling record that cannot be read stops an unlabelled run's collection ({got})")
    ok("siblings=" in inspect.getsource(collect), "UN4b collect names the launch's other labels to each fetch")
    main_py = ("import json, os\nos.makedirs('runs/R_x')\njson.dump({k: os.environ.get(k) for k in "
               "('MA_RUN_LABEL', 'MA_LAUNCH_ID')}, open('runs/R_x/env.json', 'w'))\n").encode()
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w:gz") as t:
        ti = tarfile.TarInfo("main.py")
        ti.size = len(main_py)
        t.addfile(ti, io.BytesIO(main_py))
    root = tempfile.mkdtemp()
    with mock.patch.object(ws, "WORK", root), mock.patch.object(ws, "ensure_bwrap", lambda: "bwrap: test"):
        ws.run_job_impl("R2", b.getvalue(), "q", [], "k", None, launch_id="L7")
    env = json.load(open(os.path.join(root, "R2", "runs", "R_x", "env.json")))
    ok(env == {"MA_RUN_LABEL": "R2", "MA_LAUNCH_ID": "L7"} and "launch_id=launch_id" in inspect.getsource(ws._run_to_disk),
       f"UN5 the agent VM tells main.py its label and launch, so the run dir and the stored blob are its own ({env})")
    shutil.rmtree(root, ignore_errors=True)


def _self_test() -> int:
    import datetime as dt
    import io
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def tb(members):
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            for name, data in members.items():
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
        return b.getvalue()

    meta = lambda **m: json.dumps(m).encode()  # noqa: E731
    ok(ws.run_stored(tb({"runs/r/metadata.json": meta(stored="outputs/p/r.tar.gz", store_error=None)}))
       == "outputs/p/r.tar.gz", "RC1 a verified upload is found in the run's metadata")
    ok(ws.run_stored(tb({"runs/r/metadata.json": meta(stored="x", store_error="boom")})) is None,
       "RC2 a store error means not stored")
    ok(ws.run_stored(tb({"runs/r/models/metadata.json": meta(stored="x")})) is None,
       "RC3 a nested metadata.json does not count")
    ok(ws.run_stored(b"not a tarball") is None, "RC4 an unreadable tarball fails closed")
    # RC50 (Phase 50 α): a tarball holding eval answer keys, generators or rubrics is refused.
    refused = []
    for members in ({"main.py": b"x", "evals/q/_truth/truth.json": b"{}"}, {"evals/q/generate.py": b"x"},
                    {"docs/design/eval-suite.md": b"x"}):
        try:
            refuse_hidden(tb(members), "t")
        except SystemExit:
            refused.append(True)
    ok(len(refused) == 3, f"RC50 a tarball with eval-suite files is refused ({len(refused)}/3)")
    try:
        refuse_hidden(tb({"main.py": b"x", "data/evals/q/districts.csv": b"x"}), "t")
        ok(True, "RC50b the shipped eval inputs (data/evals/) pass")
    except SystemExit as e:
        ok(False, f"RC50b the shipped eval inputs (data/evals/) pass: {e}")
    import inspect
    ok("refuse_hidden(blob" in inspect.getsource(code_tarball), "RC50c code_tarball checks the blob it returns")
    # RC50d-f: Coiled package sync ships the launch tree itself (untracked and ignored files
    # included), so a tree holding hidden eval material on disk is refused before launch.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "evals", "q"))
        open(os.path.join(td, "evals", "q", "question.txt"), "w").close()
        try:
            refuse_hidden_tree(td, tracked={"evals/q/question.txt"})
            ok(True, "RC50d a tree with only the public eval files passes")
        except SystemExit as e:
            ok(False, f"RC50d a tree with only the public eval files passes: {e}")
        for rel in ("evals/q/rubric.yaml", "evals_private/v1/q/_truth/truth.json", "outputs/bundle.enc"):
            os.makedirs(os.path.dirname(os.path.join(td, rel)), exist_ok=True)
            open(os.path.join(td, rel), "w").close()
            try:
                refuse_hidden_tree(td, tracked={"evals/q/question.txt"})
                ok(False, f"RC50e a tree holding {rel} is refused")
            except SystemExit as e:
                ok(rel in str(e), f"RC50e the refusal names {rel}: {e}")
            os.remove(os.path.join(td, rel))
    src = inspect.getsource(code_tarball)
    ok("refuse_hidden_tree(cfg.tree" in src and src.index("refuse_hidden_tree(") < src.index("tarball_from_tree") and '"archive"' in inspect.getsource(CO.tarball_from_tree),
       "RC50f code_tarball checks the launch tree on disk before it builds anything")

    now = time.time()

    class Store:
        def __init__(self, blobs=(), files=None):
            self.blobs, self.files = list(blobs), files or {}

        def list_blobs(self, prefix, account):
            return [{"name": n, "last_modified": dt.datetime.fromtimestamp(t, dt.timezone.utc)}
                    for n, t in self.blobs if n.startswith(prefix)]

        def download(self, blob, path, account):
            if blob not in self.files:
                raise FileNotFoundError(blob)
            open(path, "wb").write(self.files[blob])

    p, labels = "runs/x", ["R1", "R2"]
    r1, r2 = ws.record_blob(p, "L2", "R1"), ws.record_blob(p, "L2", "R2")
    ok(not ws.all_stored(Store([(r1, now)]), p, None, labels, "L2"), "RC5 one run of two stored: keep running")
    ok(ws.all_stored(Store([(r1, now), (r2, now)]), p, None, labels, "L2"), "RC6 both stored: shut down")
    ok(not ws.all_stored(Store([(ws.record_blob(p, "L1", "R1"), now - 60), (r2, now)]), p, None, labels, "L2"),
       "RC7 a record an earlier launch wrote minutes ago does not count")
    ok(not ws.all_stored(Store([(f"outputs/{p}/result_R1.json", now), (r2, now)]), p, None, labels, "L2"),
       "RC7b a record outside any launch folder does not count")

    listing = [{"name": "a", "current_state": {"state": "stopped"}}, {"name": "a", "current_state": {"state": "ready"}},
               {"name": "b", "current_state": "Stopping"}, {"name": "c", "current_state": None}]
    ok(is_running("a", lambda: listing), "RC8 a ready cluster among stopped ones of the same name is running")
    ok(not is_running("b", lambda: listing) and not is_running("c", lambda: listing)
       and not is_running("zz", lambda: listing), "RC9 stopping / no state / absent are not attachable")

    class FakeCluster:
        def __init__(self, cid):
            self.cluster_id, self.stopped = cid, False

        def shutdown(self):
            self.stopped = True

    fc = FakeCluster(7)
    ok(ws.verify_attached(fc, 7) is fc and not fc.stopped, "RC17 attaching to the checked cluster is kept")
    try:   # Codex round 2: before the change verify_attached takes no start time
        t_att = _ts("2026-11-10T00:00:00Z")
        listed = lambda cid, created: (lambda: [{"name": "a", "id": cid, "created": created}])  # noqa: E731
        fc = FakeCluster(8)
        try:
            ws.verify_attached(fc, 7, started=t_att, lister=listed(8, "2026-11-10T00:00:30Z"))
            ok(False, "RC18 a replacement cluster created by the attach is refused")
        except SystemExit as e:
            ok(fc.stopped and "8" in str(e) and "7" in str(e),
               "RC18 a replacement cluster created by the attach is shut down and refused")
        # Codex round 2 on the launch watch (P1-2): a cluster the attach did not create is refused, never shut down
        for cid, lister, what in (
                ("RC18b", listed(8, "2026-11-09T23:00:00Z"), "created an hour before the attach (another invocation's)"),
                ("RC18c", listed(9, "2026-11-10T00:00:30Z"), "not in the listing"),
                ("RC18d", lambda: (_ for _ in ()).throw(OSError("listing down")), "the listing fails")):
            fc = FakeCluster(8)
            try:
                ws.verify_attached(fc, 7, started=t_att, lister=lister)
                ok(False, f"{cid} a cluster {what} is refused")
            except SystemExit as e:
                ok(not fc.stopped and "8" in str(e) and "7" in str(e), f"{cid} a cluster {what}: refused, left alone: {e}")
        fc = FakeCluster(8)
        try:
            ws.verify_attached(fc, 7)
            ok(False, "RC18e no attach start time: refused")
        except SystemExit:
            ok(not fc.stopped, "RC18e no attach start time: refused, left alone")
        ok(ws.ATTACH_SKEW_S == OWN_SKEW_S, "RC18f the attach and the construction use one ownership skew")
    except Exception as e:  # noqa: BLE001
        ok(False, f"RC18: {type(e).__name__}: {e}")
    ok(live_id("a", lambda: [{"name": "a", "id": 3, "current_state": {"state": "stopped"}},
                             {"name": "a", "id": 5, "current_state": {"state": "ready"}}]) == 5
       and live_id("a", lambda: [{"name": "a", "id": 3, "current_state": {"state": "stopped"}}]) is None,
       "RC19 the live id ignores stopped clusters of the same name")
    ok(keeper_wait([]) == "start" and keeper_wait(["stopped", "stopped"]) == "start"
       and keeper_wait(["stopped", "stopping"]) == "wait" and keeper_wait(["stopped", "ready"]) == "stop"
       and keeper_wait(["stopping", "running"]) == "stop",
       "RC22 a live or stopping keeper from an earlier launch is never reused")
    ok(relaunch_refusal("missing", False) is None and relaunch_refusal("running", False)
       and relaunch_refusal("done", False) and relaunch_refusal("missing", True),
       "RC20 relaunch only when the run is neither running, finished on its VM, nor stored")

    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="t", question_file="/dev/null", out_dir=d, labels=("R1",))
        ok(cfg.storage_prefix == "runs/t" and cfg.keeper_name == "t-keeper", "RC10 config defaults")
        for bad, why in ((dict(flags=("--max-rounds", "2")), "rounds via flags"), (dict(labels=()), "no labels"),
                         (dict(labels=("R1", "R1")), "duplicate labels")):
            try:
                RunConfig(name="t", question_file="/dev/null", **bad)
                ok(False, f"RC11 rejects {why}")
            except ValueError:
                ok(True, f"RC11 rejects {why}")
        run = tb({"r/metadata.json": b"{}", "r/report.md": b"# report"})
        rec = json.dumps({"exit_code": 0, "duration_s": 5, "run_stored": "outputs/runs/t/r.tar.gz"}).encode()
        st = Store(files={ws.record_blob("runs/t", "L2", "R1"): rec, "outputs/runs/t/r.tar.gz": run})
        m = collect_from_storage(cfg, st, "R1", "L2")
        ok(local_is_current(os.path.join(d, "R1"), "L2") is True and local_is_current(os.path.join(d, "R1"), "L3") is False
           and local_is_current(os.path.join(d, "R9"), "L2") is None,
           "RC21 a local copy counts as collected only for its own launch")
        ok(m["exit_code"] == 0 and open(os.path.join(d, "R1", "runs", "r", "report.md")).read() == "# report",
           "RC12 collect from storage unpacks the stored run directory")
        try:
            collect_from_storage(cfg, Store(files={ws.record_blob("runs/t", "L2", "R1"): b'{"run_stored": null}'}),
                                 "R1", "L2")
            ok(False, "RC13 a record naming no stored run fails")
        except SystemExit:
            ok(True, "RC13 a record naming no stored run fails")
        try:
            collect_from_storage(cfg, Store(files={ws.record_blob("runs/t", "L1", "R1"): rec,
                                                   "outputs/runs/t/r.tar.gz": run}), "R1", "L2")
            ok(False, "RC13b an earlier launch's record is not collected for this launch")
        except FileNotFoundError:
            ok(True, "RC13b an earlier launch's record is not collected for this launch")
        cf = os.path.join(d, "c.py")
        open(cf, "w").write("from run_cluster import RunConfig\nCONFIG = RunConfig(name='z', question_file='q')\n")
        ok(load_config(cf).name == "z", "RC14 a config file loads")
        open(cf, "w").write("X = 1\n")
        try:
            load_config(cf)
            ok(False, "RC15 a config without CONFIG is refused")
        except SystemExit:
            ok(True, "RC15 a config without CONFIG is refused")
    try:
        _self_test_budget(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"BU: {type(e).__name__}: {e}")
    try:
        _self_test_eval_configs(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"EV: {type(e).__name__}: {e}")
    try:
        _self_test_api_keys(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"RK: {type(e).__name__}: {e}")
    rc_mod.budget_guard = lambda cfg, reserve=True, inv=None: None   # the launch cases below predate the budget (BU1-BU4 test it)
    _self_test_42b(ok, RunConfig)
    _self_test_52(ok)
    _self_test_sheet(ok)
    _self_test_attach(ok)
    _self_test_sign(ok)
    _self_test_pipeline(ok, tb)
    try:
        _self_test_s18(ok, tb)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"LR: {type(e).__name__}: {e}")
    try:
        _self_test_s18_r1(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"R: {type(e).__name__}: {e}")
    try:
        _self_test_s18_r2(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"R9: {type(e).__name__}: {e}")
    try:
        _self_test_s18_r3(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"K: {type(e).__name__}: {e}")
    try:
        _self_test_s18_r4(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"O: {type(e).__name__}: {e}")
    try:
        _self_test_s18_smoke_a(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"A: {type(e).__name__}: {e}")
    try:
        _self_test_collect_no_shutdown(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"CN: {type(e).__name__}: {e}")
    try:
        _self_test_collect_import(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"CI: {type(e).__name__}: {e}")
    try:
        _self_test_collect_labels(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"RL: {type(e).__name__}: {e}")
    try:
        _self_test_checkpoint(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"CK: {type(e).__name__}: {e}")
    try:
        _self_test_storage(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"WS: {type(e).__name__}: {e}")
    try:
        _self_test_liveness(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"LV: {type(e).__name__}: {e}")
    try:
        _self_test_replicate_names(ok, tb)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"UN: {type(e).__name__}: {e}")
    for f in sorted(os.listdir(os.path.join(CLOUD_DIR, "configs"))):
        if f.endswith(".py") and not f.startswith("_"):   # _*.py: shared helpers (configs/_eval.py)
            try:
                c = load_config(os.path.join(CLOUD_DIR, "configs", f))
                ok(c.storage_prefix.startswith(("runs/", "_checks/")), f"RC16 configs/{f} loads")
            except Exception as e:  # noqa: BLE001
                ok(False, f"RC16 configs/{f} loads: {e}")
    # AQ20 (docs/design/acquire.md §§8, 10): refuse --acquire on eval configs and private: true questions
    import types as _aq_types
    with tempfile.TemporaryDirectory() as td_aq:
        q_priv = os.path.join(td_aq, "q_priv.md")
        q_pub = os.path.join(td_aq, "q_pub.md")
        open(q_priv, "w", encoding="utf-8").write("private: true\nHow to allocate?\n")
        open(q_pub, "w", encoding="utf-8").write("How to allocate?\n")
        c_eval = _aq_types.SimpleNamespace(flags=["--v2", "--acquire"], storage_prefix="runs/evals/q1",
                                           required_files=["data/evals/q1/u.csv"], question_file=q_pub)
        c_priv = _aq_types.SimpleNamespace(flags=["--v2", "--acquire"], storage_prefix="runs/custom/q2",
                                           required_files=[], question_file=q_priv)
        c_ok = _aq_types.SimpleNamespace(flags=["--v2", "--acquire"], storage_prefix="runs/custom/q3",
                                         required_files=[], question_file=q_pub)
        c_eval_off = _aq_types.SimpleNamespace(flags=["--v2"], storage_prefix="runs/evals/q1",
                                               required_files=["data/evals/q1/u.csv"], question_file=q_pub)
        ok(bool(acquire_flag_problems(c_eval)) and bool(acquire_flag_problems(c_priv))
           and not acquire_flag_problems(c_ok) and not acquire_flag_problems(c_eval_off),
           "AQ20 run_cluster refuses --acquire on eval configs and private: true questions, allows public custom runs")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0



if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
