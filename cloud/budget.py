"""Owner-signed cloud budget, measured spend, and the launch check (F6 Stage 2).

Why. On 2026-11-11 an agent operating this repo spent $106 of the owner's $100 cloud budget: the budget, the
dollar conversion and the spend checks lived in the agent's memory and judgment. Here they are code, and the
budget is something the agent cannot set or raise. It is a record signed with the owner's Ed25519 key. The
private key sits in the macOS login keychain with an empty list of trusted applications (`security
add-generic-password ... -T ""`), so every read of it raises the keychain's own dialog asking for the owner's
login-keychain password; an agent running in a terminal cannot answer that dialog. The owner must click "Allow",
never "Always Allow" (that would trust `security` and end the prompt). The trust anchor is the tracked public key
cloud/budget_owner.pub: a change to it shows in git.

    python cloud/budget.py init-key                  # once, by the owner
    python cloud/budget.py set --scope f6 --usd 200 --note "F6 refit + run" [--start 2026-11-11T00:00:00Z]
    python cloud/budget.py show [--scope f6]         # verify and print the signed record
    python cloud/budget.py status [--scope f6]       # budget, spent (billed / projected), remaining
    python cloud/budget.py --self-test

Spend. Every Coiled cluster in the IDM workspace created at or after the budget's start whose name starts with
one of the scope's prefixes (cloud/budget_scopes.yaml, copied into the signed record by `set`, so editing the
file cannot shrink a signed scope). Billed USD of a cluster = total_cost (Coiled credits) x 0.061 +
cloud_total_cost (USD), from the raw cluster record (coiled 1.135.6: CloudV2._get_cluster_details_synced,
coiled/v2/core.py:976, GET /api/v2/clusters/account/<workspace>/id/<id>; the public coiled.cluster_details,
core.py:994, drops the cost fields; the fields as Coiled's own widget reads them: coiled/v2/widgets/rich.py:276-283).
A cluster not yet stopped adds a projection: the hours each live instance has run so far x a USD per
instance-hour measured as the median over the scope's last stopped clusters, never below the signed floor. This
adds to whatever Coiled has billed so far, so it errs high. Anything that cannot be read or parsed refuses.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from worker_side import WORKSPACE, state_of  # noqa: E402

PUB_FILE = os.path.join(HERE, "budget_owner.pub")
SCOPES_FILE = os.path.join(HERE, "budget_scopes.yaml")
BUDGET_DIR = os.path.expanduser("~/.config/modeling-agentic")
KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "modeling-agentic-budget-owner", "owner"
USD_PER_CREDIT = 0.061
RATE_SAMPLE = 5                 # stopped clusters the instance-hour rate is the median of
ENDED = ("stopped", "error")
FIELDS = ("scope", "usd", "start_utc", "note", "issued_utc", "nonce", "prefixes", "floor_usd_per_instance_hour")


class BudgetError(Exception):
    """A budget that cannot be verified or measured: the caller refuses."""


class Keychain:
    """The only access to the owner's private key: a generic password in the login keychain."""

    def __init__(self, run=subprocess.run):
        self.run = run

    def _who(self):
        return ["-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT]

    def add_command(self, secret_hex):
        raise NotImplementedError

    def exists(self) -> bool:
        raise NotImplementedError

    def put(self, secret_hex: str) -> None:
        raise NotImplementedError

    def get(self) -> str:
        raise NotImplementedError


def init_key(store, pub_file=PUB_FILE) -> str:
    raise NotImplementedError


def make_record(scope, usd, note, start_utc=None, scopes_file=SCOPES_FILE, now=None) -> dict:
    raise NotImplementedError


def sign_doc(record, store) -> dict:
    raise NotImplementedError


def verify_doc(doc, pub_hex) -> dict:
    raise NotImplementedError


def set_budget(scope, usd, note, store, start_utc=None, budget_dir=None, pub_file=PUB_FILE, scopes_file=SCOPES_FILE):
    raise NotImplementedError


def load_budget(scope, budget_dir=None, pub_file=PUB_FILE) -> dict:
    raise NotImplementedError


def load_scopes(path=SCOPES_FILE) -> dict:
    raise NotImplementedError


def scope_for_name(name, path=SCOPES_FILE):
    raise NotImplementedError


class CoiledSource:
    """All Coiled access for spend: the IDM workspace only."""

    def list_clusters(self) -> list:
        raise NotImplementedError

    def details(self, cid) -> dict:
        raise NotImplementedError


def cluster_usd(details, rate, now) -> dict:
    raise NotImplementedError


def spend(scope, source=None, budget=None, now=None, **load) -> dict:
    raise NotImplementedError


def launch_budget_ok(scope, cap_usd, name=None, **kw) -> tuple[bool, str]:
    raise NotImplementedError


def job_meter(scope, source=None, clock=time.time, **kw):
    raise NotImplementedError


def status_lines(s) -> list[str]:
    raise NotImplementedError


# ---- self-test -------------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, secret=None):
        self.secret, self.reads = secret, 0

    def exists(self):
        return self.secret is not None

    def put(self, s):
        self.secret = s

    def get(self):
        self.reads += 1
        if self.secret is None:
            raise BudgetError("no key")
        return self.secret


class _FakeSource:
    def __init__(self, items, details):
        self.items, self.det, self.calls = items, details, []

    def list_clusters(self):
        self.calls.append("list")
        return list(self.items)

    def details(self, cid):
        self.calls.append(cid)
        d = self.det[cid]
        if isinstance(d, Exception):
            raise d
        return d


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _det(cid, name, created, credits, cloud, state="stopped", insts=()):
    """A raw cluster record; insts: (created, end or None while live)."""
    procs = [{"current_state": {"state": "stopped" if e else "started", "updated": _iso(e or created)},
              "instance": {"created": _iso(c), "current_state": {"state": "stopped" if e else "ready",
                                                                  "updated": _iso(e or c)}}} for c, e in insts]
    return {"id": cid, "name": name, "created": _iso(created), "current_state": {"state": state},
            "total_cost": credits, "cloud_total_cost": cloud, "scheduler": procs[0] if procs else None,
            "workers": procs[1:]}


def _self_test() -> int:
    import contextlib
    import io
    import tempfile
    import types
    from unittest import mock
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def case(label, fn):
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 -- a missing function is a failing case, not a crash
            ok(False, f"{label} ({type(e).__name__}: {e})")

    def refused(fn):
        try:
            fn()
        except BudgetError as e:
            return str(e)
        return None

    T0 = 1_762_819_200.0   # 2025-11-11T00:00:00Z
    with tempfile.TemporaryDirectory() as d:
        pub, bdir, sc = os.path.join(d, "owner.pub"), os.path.join(d, "cfg"), os.path.join(d, "scopes.yaml")
        open(sc, "w").write("floor_usd_per_instance_hour: 1.0\nscopes:\n  f6: [f6-refit-, lw-smoke-]\n"
                            "  other: [ev-]\n")
        store = _FakeStore()

        def bg1():
            init_key(store, pub)
            ok(store.secret and len(open(pub).read().strip()) == 64 and store.reads == 0,
               "BG1 init-key puts the private key in the key store and writes the public key")
            ok(refused(lambda: init_key(store, pub)) is not None and refused(lambda: init_key(_FakeStore("ab"),
                                                                                              pub + "2")),
               "BG1b init-key refuses when the public key file or the keychain item already exists")
            p = set_budget("f6", 100, "test", store, start_utc=_iso(T0), budget_dir=bdir, pub_file=pub,
                           scopes_file=sc)
            rec = load_budget("f6", budget_dir=bdir, pub_file=pub)
            ok(store.reads == 1 and rec["usd"] == 100 and rec["prefixes"] == ["f6-refit-", "lw-smoke-"]
               and rec["start_utc"] == _iso(T0) and len(rec["nonce"]) >= 16 and oct(os.stat(p).st_mode)[-3:] == "600",
               f"BG2 set signs with the key store's key (one read) and load verifies the round trip: {rec}")
            doc = json.load(open(p))
            for label, mut in (("a raised usd", lambda r: r.__setitem__("usd", 1000)),
                               ("a dropped prefix", lambda r: r["prefixes"].pop()),
                               ("an added field", lambda r: r.__setitem__("x", 1))):
                bad = json.loads(json.dumps(doc))
                mut(bad["record"])
                json.dump(bad, open(p, "w"))
                ok(refused(lambda: load_budget("f6", budget_dir=bdir, pub_file=pub)) is not None,
                   f"BG3 a tampered record is refused: {label}")
            json.dump(doc, open(p, "w"))
            other = os.path.join(d, "other.pub")
            init_key(_FakeStore(), other)
            ok(refused(lambda: load_budget("f6", budget_dir=bdir, pub_file=other)) is not None,
               "BG4 a record signed by another key than budget_owner.pub's is refused")
            ok(refused(lambda: load_budget("other", budget_dir=bdir, pub_file=pub)) is not None
               and refused(lambda: load_budget("f6", budget_dir=bdir, pub_file=os.path.join(d, "none.pub")))
               is not None and refused(lambda: load_budget("../f6", budget_dir=bdir, pub_file=pub)) is not None,
               "BG5 a missing budget file or public key, or a bad scope name, is refused")
            os.rename(p, p + ".x")
            open(p, "w").write("{not json")
            ok(refused(lambda: load_budget("f6", budget_dir=bdir, pub_file=pub)) is not None,
               "BG5b an unparsable budget file is refused")
            os.rename(p + ".x", p)
            ok(refused(lambda: set_budget("nope", 5, "", store, budget_dir=bdir, pub_file=pub, scopes_file=sc))
               and refused(lambda: set_budget("f6", -5, "", store, budget_dir=bdir, pub_file=pub, scopes_file=sc))
               and refused(lambda: set_budget("f6", float("nan"), "", store, budget_dir=bdir, pub_file=pub,
                                              scopes_file=sc)),
               "BG5c set refuses a scope not in budget_scopes.yaml and a non-positive or non-finite amount")
        case("BG1-5", bg1)

        def bg6():
            calls = []

            def run(argv, **kw):
                calls.append(argv)
                return types.SimpleNamespace(returncode=0 if argv[1] != "find-generic-password" or "-w" in argv
                                             else 44, stdout="ab" * 32 + "\n", stderr="")
            k = Keychain(run=run)
            k.put("cd" * 32)
            got, ex = k.get(), k.exists()
            add = calls[0]
            ok(add[:2] == ["security", "add-generic-password"] and add[add.index("-T") + 1] == ""
               and "-A" not in add and "-U" not in add and add[add.index("-s") + 1] == KEYCHAIN_SERVICE,
               f"BG6 the key goes in with an empty trusted-application list (-T \"\"), never -A or -U: {add[:8]}")
            ok(got == "ab" * 32 and calls[1][:2] == ["security", "find-generic-password"] and "-w" in calls[1]
               and ex is False and "-w" not in calls[2],
               "BG6b the key is read with find-generic-password -w (the prompted read); exists reads no secret")
            k2 = Keychain(run=lambda argv, **kw: types.SimpleNamespace(returncode=51, stdout="", stderr="denied"))
            ok(refused(k2.get) is not None, "BG6c a refused or failed keychain read raises (no key, no signature)")
        case("BG6", bg6)

        rec = {"scope": "f6", "usd": 100.0, "start_utc": _iso(T0), "prefixes": ["f6-refit-", "lw-smoke-"],
               "floor_usd_per_instance_hour": 1.0}
        now = T0 + 10 * 3600
        items = [{"id": 1, "name": "f6-refit-a", "created": _iso(T0 + 60), "current_state": {"state": "stopped"}},
                 {"id": 2, "name": "lw-smoke-b", "created": _iso(T0 + 120), "current_state": {"state": "running"}},
                 {"id": 3, "name": "f6-refit-old", "created": _iso(T0 - 86400), "current_state": {"state": "stopped"}},
                 {"id": 4, "name": "ev-x", "created": _iso(T0 + 60), "current_state": {"state": "stopped"}},
                 {"id": 5, "name": "xf6-refit-", "created": _iso(T0 + 60), "current_state": {"state": "stopped"}}]
        dets = {1: _det(1, "f6-refit-a", T0 + 60, 100, 4.0, insts=[(T0 + 60, T0 + 2 * 3600 + 60)]),   # 2 h, $10.1
                2: _det(2, "lw-smoke-b", T0 + 120, 10, 1.0, state="running",
                        insts=[(now - 3 * 3600, None), (now - 3600, None), (T0 + 120, T0 + 1200)]),
                3: _det(3, "f6-refit-old", T0 - 86400, 200, 12.0, insts=[(T0 - 86400, T0 - 86400 + 4 * 3600)]),
                4: ValueError("not this scope"), 5: ValueError("not this scope")}

        def bg7():
            src = _FakeSource(items, dets)
            s = spend("f6", source=src, budget=rec, now=now)
            by = {c["id"]: c for c in s["clusters"]}
            # rate: cluster 1 $10.10 / 2 h = 5.05, cluster 3 $24.20 / 4 h = 6.05 -> median 5.55
            ok(sorted(by) == [1, 2] and abs(by[1]["billed_usd"] - (100 * 0.061 + 4.0)) < 1e-9
               and abs(by[2]["billed_usd"] - (10 * 0.061 + 1.0)) < 1e-9 and by[1]["projected_usd"] == 0,
               f"BG7 billed USD = credits x 0.061 + cloud USD, for the scope's clusters created since the start "
               f"(not another scope's, not a name that only contains a prefix, not one created before): {by}")
            ok(abs(s["rate_usd_per_instance_hour"] - 5.55) < 1e-9 and by[2]["live_instances"] == 2
               and abs(by[2]["projected_usd"] - 4 * 5.55) < 1e-9
               and abs(s["total_usd"] - (s["billed_usd"] + s["projected_usd"])) < 1e-9
               and abs(s["total_usd"] - (10.1 + 1.61 + 22.2)) < 1e-9,
               f"BG8 a running cluster adds live instance-hours x the median rate of the scope's stopped clusters "
               f"(older ones included for the rate): rate {s['rate_usd_per_instance_hour']}, total {s['total_usd']}")
            cheap = dict(dets)
            cheap[1] = _det(1, "f6-refit-a", T0 + 60, 1, 0.01, insts=[(T0 + 60, T0 + 2 * 3600 + 60)])
            cheap[3] = _det(3, "f6-refit-old", T0 - 86400, 1, 0.01, insts=[(T0 - 86400, T0 - 86400 + 4 * 3600)])
            s2 = spend("f6", source=_FakeSource(items, cheap), budget=rec, now=now)
            ok(s2["rate_usd_per_instance_hour"] == 1.0,
               f"BG8b the rate never goes below the signed floor: {s2['rate_usd_per_instance_hour']}")
            s3 = spend("f6", source=_FakeSource(items[1:2], {2: dets[2]}), budget=rec, now=now)
            ok(s3["rate_usd_per_instance_hour"] == 1.0 and abs(s3["projected_usd"] - 4.0) < 1e-9,
               "BG8c with no stopped cluster measured, the floor is the rate")
        case("BG7-8", bg7)

        def bg9():
            seen = {}

            class CloudV2:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def _get_cluster_details_synced(self, cluster_id, workspace=None):
                    seen["details"] = workspace
                    return {"id": cluster_id}

            def lc(workspace=None, max_pages=20, account=None, just_mine=False):
                seen["list"], seen["pages"] = workspace, max_pages
                return []
            core = types.SimpleNamespace(CloudV2=CloudV2)
            with mock.patch.dict(sys.modules, {"coiled": types.SimpleNamespace(list_clusters=lc),
                                               "coiled.v2": types.SimpleNamespace(core=core), "coiled.v2.core": core}):
                CoiledSource().list_clusters()
                CoiledSource().details(7)
            ok(seen == {"list": WORKSPACE, "pages": None, "details": WORKSPACE} and WORKSPACE == "idm-coiled-idmad-r2",
               f"BG9 every Coiled read names the IDM workspace (all listing pages): {seen}")
        case("BG9", bg9)

        def bg10():
            src = lambda d_=dets, i=items: _FakeSource(i, d_)  # noqa: E731
            spent = spend("f6", source=src(), budget=rec, now=now)["total_usd"]
            kw = dict(budget=rec, now=now)
            ok(launch_budget_ok("f6", 100 - spent, source=src(), **kw)[0]
               and not launch_budget_ok("f6", 100 - spent + 0.01, source=src(), **kw)[0],
               f"BG10 ok exactly when spent + cap <= budget (spent {spent:.2f})")
            bad = [launch_budget_ok("f6", c, source=src(), **kw)[0] for c in (0, -1, float("nan"), None, "x", True)]
            ok(not any(bad), f"BG10b a cap that is not a positive number is refused: {bad}")
            ok(not launch_budget_ok("f6", 1, name="ev-z", source=src(), **kw)[0]
               and launch_budget_ok("f6", 1, name="lw-smoke-z1", source=src(), **kw)[0],
               "BG10c a cluster name outside the signed prefixes is refused (its spend would escape the budget)")
            d2 = dict(dets)
            d2[1] = {k: v for k, v in dets[1].items() if k != "total_cost"}
            d3 = dict(dets)
            d3[2] = RuntimeError("API down")
            r = [launch_budget_ok("f6", 1, source=s, **kw) for s in (src(d2), src(d3), _FakeSource(None, {}))]
            ok(not any(x[0] for x in r) and all("fail" in x[1] for x in r),
               f"BG10d a cost field missing, a details call failing, or a broken listing refuses: {[x[1] for x in r]}")
            ok(not launch_budget_ok("f6", 1, source=src(), now=now, budget_dir=os.path.join(d, "nobudget"),
                                    pub_file=pub)[0], "BG10e no signed budget, no launch")
            m = job_meter("f6", source=src(), clock=lambda: now, **kw)
            ok(abs(m(2) - (1.61 + 22.2)) < 1e-9 and abs(m(1) - 10.1) < 1e-9,
               "BG11 the job meter reads one cluster's billed + projected USD at the scope's rate")
            lines = status_lines(spend("f6", source=src(), **kw))
            txt = "\n".join(lines)
            ok("remaining" in txt and "100.00" in txt and "f6-refit-a" in txt, f"BG12 status prints the table: {txt}")
        case("BG10-12", bg10)

        def bg13():
            ok(scope_for_name("lw-smoke-gcc", sc) == "f6" and scope_for_name("ev-q", sc) == "other"
               and scope_for_name("dev-vm", sc) is None, "BG13 a cluster name resolves to the scope whose prefix it has")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["status", "--scope", "f6", "--budget-dir", os.path.join(d, "none"), "--pub", pub])
            ok(rc != 0 and "refus" in out.getvalue().lower(), f"BG14 status with no budget exits non-zero: {out.getvalue()}")
        case("BG13-14", bg13)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        import no_cloud
        with no_cloud.refusing():
            return _self_test()
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
