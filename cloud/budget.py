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
    """The only access to the owner's private key: a generic password in the login keychain. `-T ""` trusts no
    application, so each `get` raises the keychain's password dialog, which only the owner at the screen answers."""

    def __init__(self, run=subprocess.run):
        self.run = run

    def _who(self):
        return ["-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT]

    def add_command(self, secret_hex):
        return ["security", "add-generic-password", *self._who(), "-l", "modeling-agentic budget owner key",
                "-T", "", "-w", secret_hex]

    def exists(self) -> bool:     # attributes only: reads no secret, raises no dialog
        return self.run(["security", "find-generic-password", *self._who()], capture_output=True).returncode == 0

    def put(self, secret_hex: str) -> None:
        r = self.run(self.add_command(secret_hex), capture_output=True, text=True)
        if r.returncode != 0:
            raise BudgetError(f"keychain refused the key (security exit {r.returncode}): {r.stderr.strip()[:200]}")

    def get(self) -> str:
        r = self.run(["security", "find-generic-password", *self._who(), "-w"], capture_output=True, text=True)
        if r.returncode != 0:
            raise BudgetError(f"the keychain read was refused or failed (security exit {r.returncode})")
        return r.stdout.strip()


def _ts(v):
    """An ISO time as epoch seconds (UTC when it has no zone), or None."""
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _num(x, what) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise BudgetError(f"{what} is not a number: {x!r}")
    return float(x)


def _canon(record) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _scope_name(scope) -> str:
    if not isinstance(scope, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", scope):
        raise BudgetError(f"bad scope name {scope!r}")
    return scope


def init_key(store, pub_file=PUB_FILE) -> str:
    """A new key pair: the private half into `store`, the public half (hex) to `pub_file`. Refuses to replace
    either (a new key is a deliberate act: delete the keychain item and the file first)."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if os.path.exists(pub_file) or store.exists():
        raise BudgetError(f"a key exists already ({pub_file} or the keychain item {KEYCHAIN_SERVICE}): refusing")
    k = Ed25519PrivateKey.generate()
    store.put(k.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
    pub = k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    with open(pub_file, "x") as f:
        f.write(pub + "\n")
    return pub


def _read_pub(pub_file) -> str:
    try:
        pub = open(pub_file).read().strip()
    except OSError as e:
        raise BudgetError(f"no owner public key at {pub_file} ({e}): the owner runs `budget.py init-key`") from None
    if not re.fullmatch(r"[0-9a-f]{64}", pub):
        raise BudgetError(f"{pub_file} is not an Ed25519 public key in hex")
    return pub


def load_scopes(path=SCOPES_FILE) -> dict:
    import yaml
    try:
        y = yaml.safe_load(open(path))
        floor = _num(y["floor_usd_per_instance_hour"], "floor_usd_per_instance_hour")
        scopes = {_scope_name(k): list(v) for k, v in y["scopes"].items()}
    except BudgetError:
        raise
    except Exception as e:  # noqa: BLE001 -- any unreadable file refuses
        raise BudgetError(f"{path} cannot be read: {type(e).__name__}: {e}") from None
    if floor <= 0 or not all(v and all(isinstance(p, str) and p for p in v) for v in scopes.values()):
        raise BudgetError(f"{path}: the floor must be positive and every scope needs non-empty prefixes")
    return {"floor": floor, "scopes": scopes}


def scope_for_name(name, path=SCOPES_FILE):
    """The one scope whose prefix `name` starts with, else None."""
    hits = [s for s, ps in load_scopes(path)["scopes"].items() if any(str(name).startswith(p) for p in ps)]
    return hits[0] if len(hits) == 1 else None


def _check_record(rec) -> dict:
    if not isinstance(rec, dict) or sorted(rec) != sorted(FIELDS):
        raise BudgetError(f"the budget record's fields are not {FIELDS}")
    _scope_name(rec["scope"])
    if _num(rec["usd"], "usd") <= 0 or _num(rec["floor_usd_per_instance_hour"], "floor") <= 0:
        raise BudgetError("the budget and the floor must be positive")
    if _ts(rec["start_utc"]) is None or not (isinstance(rec["prefixes"], list) and rec["prefixes"]
                                             and all(isinstance(p, str) and p for p in rec["prefixes"])):
        raise BudgetError("bad start time or prefixes in the budget record")
    return rec


def make_record(scope, usd, note, start_utc=None, scopes_file=SCOPES_FILE, now=None) -> dict:
    import secrets
    sc = load_scopes(scopes_file)
    if _scope_name(scope) not in sc["scopes"]:
        raise BudgetError(f"scope {scope!r} is not in {scopes_file}")
    now = time.time() if now is None else now
    start = _iso(now) if start_utc is None else start_utc
    return _check_record({"scope": scope, "usd": _num(usd, "usd"), "start_utc": start, "note": str(note or ""),
                          "issued_utc": _iso(now), "nonce": secrets.token_hex(16), "prefixes": sc["scopes"][scope],
                          "floor_usd_per_instance_hour": sc["floor"]})


def sign_doc(record, store) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(store.get()))
    return {"alg": "ed25519", "record": record, "signature": key.sign(_canon(_check_record(record))).hex()}


def verify_doc(doc, pub_hex) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        rec, sig = doc["record"], bytes.fromhex(doc["signature"])
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(sig, _canon(rec))
    except (InvalidSignature, KeyError, TypeError, ValueError, AttributeError):
        raise BudgetError("the budget's signature does not verify against the owner's public key") from None
    return _check_record(rec)


def budget_path(scope, budget_dir=None) -> str:
    return os.path.join(budget_dir or BUDGET_DIR, f"budget_{_scope_name(scope)}.json")


def set_budget(scope, usd, note, store, start_utc=None, budget_dir=None, pub_file=PUB_FILE, scopes_file=SCOPES_FILE):
    doc = sign_doc(make_record(scope, usd, note, start_utc, scopes_file), store)
    verify_doc(doc, _read_pub(pub_file))      # the keychain's key is the anchor's pair, or nothing is written
    p = budget_path(scope, budget_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(json.dumps(doc, indent=1) + "\n")
    os.replace(tmp, p)
    return p


def load_budget(scope, budget_dir=None, pub_file=PUB_FILE) -> dict:
    p = budget_path(scope, budget_dir)
    try:
        doc = json.load(open(p))
    except FileNotFoundError:
        raise BudgetError(f"no budget for {scope} ({p}): the owner sets one with `budget.py set`") from None
    except (OSError, ValueError) as e:
        raise BudgetError(f"{p} cannot be read: {e}") from None
    rec = verify_doc(doc, _read_pub(pub_file))
    if rec["scope"] != scope:
        raise BudgetError(f"{p} holds the budget of {rec['scope']!r}, not {scope!r}")
    return rec


class CoiledSource:
    """All Coiled access for spend: the IDM workspace only, read-only."""

    def list_clusters(self) -> list:
        import coiled
        cs = coiled.list_clusters(workspace=WORKSPACE, max_pages=None)
        return cs if isinstance(cs, list) else list(cs.values())

    def details(self, cid) -> dict:
        from coiled.v2.core import CloudV2    # the raw record: coiled.cluster_details drops the cost fields
        with CloudV2() as cloud:
            return cloud._get_cluster_details_synced(cluster_id=cid, workspace=WORKSPACE)


def _hours(details, now):
    """(live instances, their hours so far, every instance's hours) from a raw cluster record."""
    live, live_h, all_h = 0, 0.0, 0.0
    for p in [details.get("scheduler")] + list(details.get("workers") or []):
        inst = (p or {}).get("instance")
        if not inst:
            continue                      # no VM was given to this process: nothing billed
        on = (state_of(inst) or state_of(p)) not in ENDED
        t0, t1 = _ts(inst.get("created")), now if on else _ts((inst.get("current_state") or {}).get("updated"))
        if t0 is None or t1 is None:
            raise BudgetError(f"cluster {details.get('id')}: an instance has no start or stop time")
        h = max(0.0, (t1 - t0) / 3600)
        all_h += h
        if on:
            live, live_h = live + 1, live_h + h
    return live, live_h, all_h


def cluster_usd(details, rate, now) -> dict:
    """One cluster's billed USD (credits x 0.061 + cloud USD) and, while it is not stopped, the projection."""
    if not isinstance(details, dict) or "total_cost" not in details or "cloud_total_cost" not in details:
        raise BudgetError(f"cluster {details.get('id') if isinstance(details, dict) else '?'}: no cost fields")
    on = state_of(details) not in ENDED
    tc, cc = details["total_cost"], details["cloud_total_cost"]
    if on and tc is None and cc is None:
        tc = cc = 0.0                     # not billed yet: the projection covers it
    live, live_h, all_h = _hours(details, now)
    return {"id": details.get("id"), "name": details.get("name"), "state": state_of(details),
            "billed_usd": _num(tc, "total_cost") * USD_PER_CREDIT + _num(cc, "cloud_total_cost"),
            "projected_usd": live_h * rate if on else 0.0, "live_instances": live if on else 0,
            "instance_hours": all_h}


def _median(xs):
    xs, n = sorted(xs), len(xs)
    return 0.0 if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def spend(scope, source=None, budget=None, now=None, **load) -> dict:
    """The scope's spend since the signed budget's start, with a per-cluster breakdown (see the module doc)."""
    rec = budget if budget is not None else load_budget(scope, **load)
    src, now = source or CoiledSource(), time.time() if now is None else now
    start, prefixes = _ts(rec["start_utc"]), tuple(rec["prefixes"])
    items = src.list_clusters()
    if not isinstance(items, list):
        raise BudgetError("the cluster listing is not a list")
    mine = []
    for c in items:
        if str(c.get("name") or "").startswith(prefixes):
            t = _ts(c.get("created"))
            if t is None:
                raise BudgetError(f"cluster {c.get('id')} has no creation time")
            mine.append((t, c))
    mine.sort(key=lambda x: -x[0])                 # newest first
    counted = [c for t, c in mine if t >= start]
    dets = {c["id"]: src.details(c["id"]) for c in counted}
    stopped = [dets[c["id"]] for c in counted if state_of(c) in ENDED][:RATE_SAMPLE]
    older = [c for t, c in mine if t < start and state_of(c) in ENDED]
    stopped += [src.details(c["id"]) for c in older[:RATE_SAMPLE - len(stopped)]]
    rates = [r["billed_usd"] / r["instance_hours"] for r in (cluster_usd(x, 0.0, now) for x in stopped)
             if r["instance_hours"] >= 0.05]
    rate = max(_median(rates), _num(rec["floor_usd_per_instance_hour"], "floor"))
    rows = [cluster_usd(dets[c["id"]], rate, now) for c in counted]
    billed, proj = sum(r["billed_usd"] for r in rows), sum(r["projected_usd"] for r in rows)
    return {"scope": scope, "budget_usd": float(rec["usd"]), "start_utc": rec["start_utc"], "note": rec.get("note", ""),
            "prefixes": list(prefixes), "rate_usd_per_instance_hour": rate, "rate_from_clusters": len(rates),
            "billed_usd": billed, "projected_usd": proj, "total_usd": billed + proj,
            "remaining_usd": float(rec["usd"]) - billed - proj, "clusters": rows}


def launch_budget_ok(scope, cap_usd, name=None, **kw) -> tuple[bool, str]:
    """ok only if the scope's signed budget verifies, `name` (when given) starts with one of its signed prefixes,
    and the scope's spend plus `cap_usd` is within it. Any error refuses. `kw` goes to spend()."""
    try:
        cap = _num(cap_usd, "cap_usd")
        if cap <= 0:
            raise BudgetError(f"cap_usd must be positive, not {cap}")
        s = spend(scope, **kw)
    except Exception as e:  # noqa: BLE001 -- fail closed
        return False, f"budget check failed closed: {type(e).__name__}: {e}"
    if name is not None and not str(name).startswith(tuple(s["prefixes"])):
        return False, f"{name} is outside budget {scope}'s signed prefixes {s['prefixes']}: its spend would not count"
    fits = round(s["total_usd"] + cap, 6) <= round(s["budget_usd"], 6)
    return fits, (f"{scope}: spent ${s['billed_usd']:.2f} billed + ${s['projected_usd']:.2f} projected, + cap "
                  f"${cap:.2f} {'<=' if fits else '>'} budget ${s['budget_usd']:.2f}")


def job_meter(scope, source=None, clock=time.time, **kw):
    """fn(cluster id) -> that cluster's billed + projected USD now, at the scope's measured rate."""
    src = source or CoiledSource()
    rate = spend(scope, source=src, **kw)["rate_usd_per_instance_hour"]

    def meter(cid):
        r = cluster_usd(src.details(cid), rate, clock())
        return r["billed_usd"] + r["projected_usd"]
    return meter


def status_lines(s) -> list[str]:
    out = [f"budget {s['scope']}: ${s['budget_usd']:.2f} from {s['start_utc']} ({s['note']})",
           f"spent: ${s['billed_usd']:.2f} billed + ${s['projected_usd']:.2f} projected = ${s['total_usd']:.2f}; "
           f"remaining ${s['remaining_usd']:.2f}",
           f"projection rate: ${s['rate_usd_per_instance_hour']:.2f} per live instance-hour (median of "
           f"{s['rate_from_clusters']} stopped cluster(s), never below the floor)",
           f"{'cluster':<40} {'id':>9} {'state':<9} {'billed':>9} {'projected':>9} {'live VMs':>8}"]
    return out + [f"{str(r['name']):<40} {str(r['id']):>9} {r['state']:<9} {r['billed_usd']:>9.2f} "
                  f"{r['projected_usd']:>9.2f} {r['live_instances']:>8}" for r in s["clusters"]]


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

        def bg15():
            bd, day = os.path.join(d, "cfg15"), 86400
            t1 = time.time() - 2 * day
            p = set_budget("f6", 50, "first", store, budget_dir=bd, pub_file=pub, scopes_file=sc, now=t1)
            r1 = load_budget("f6", budget_dir=bd, pub_file=pub)
            ok(_ts(r1["expires_utc"]) - _ts(r1["issued_utc"]) == 14 * day,
               f"BG15 a budget expires 14 days after it is issued by default: {r1['expires_utc']}")
            ok(refused(lambda: load_budget("f6", budget_dir=bd, pub_file=pub, now=t1 + 15 * day)) is not None,
               "BG15b an expired budget is refused")
            ok(refused(lambda: set_budget("f6", 5, "", store, budget_dir=bd, pub_file=pub, scopes_file=sc, days=61))
               is not None and refused(lambda: set_budget("f6", 5, "", store, budget_dir=bd, pub_file=pub,
                                                          scopes_file=sc, days=0)) is not None,
               "BG15c set refuses --days outside 1-60")
            old = open(p).read()
            set_budget("f6", 20, "second", store, budget_dir=bd, pub_file=pub, scopes_file=sc, now=t1 + day, days=30)
            r2 = load_budget("f6", budget_dir=bd, pub_file=pub)
            open(p, "w").write(old)
            ok(r2["usd"] == 20 and _ts(r2["expires_utc"]) - _ts(r2["issued_utc"]) == 30 * day
               and os.path.exists(os.path.join(bd, "budget_f6.seen"))
               and refused(lambda: load_budget("f6", budget_dir=bd, pub_file=pub)) is not None,
               "BG16 an older signed budget put back after a newer one was seen is refused (budget_<scope>.seen)")
        case("BG15-16", bg15)

        def bg6():
            calls = []

            inputs = []

            def run(argv, **kw):
                calls.append(argv)
                inputs.append(kw.get("input"))
                return types.SimpleNamespace(returncode=0 if argv[1:2] != ["find-generic-password"] or "-w" in argv
                                             else 44, stdout="ab" * 32 + "\n", stderr="")
            k = Keychain(run=run)
            secret = "cd" * 32
            k.put(secret)
            got, ex = k.get(), k.exists()
            add, line = calls[0], str(inputs[0] or "")
            toks = line.split()
            ok(add == ["security", "-i"] and not any(secret in a for c in calls for a in c)
               and toks[:1] == ["add-generic-password"] and secret in toks and toks[toks.index("-T") + 1] == '""'
               and "-A" not in toks and "-U" not in toks and toks[toks.index("-s") + 1] == KEYCHAIN_SERVICE,
               f"BG6 the key goes to `security -i` on stdin, never on a command line, with an empty trusted-application "
               f"list (-T \"\"), never -A or -U: argv {add} stdin {line.replace(secret, '<key>')[:120]}")
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
               and abs(by[2]["projected_usd"] - (4.3 * 5.55 - 1.61)) < 1e-9
               and abs(s["total_usd"] - (s["billed_usd"] + s["projected_usd"])) < 1e-9
               and abs(s["total_usd"] - (10.1 + 4.3 * 5.55)) < 1e-9,
               f"BG8 a running cluster counts max(billed, its instance-hours so far x the median rate of the scope's "
               f"stopped clusters, older ones included): rate {s['rate_usd_per_instance_hour']}, total {s['total_usd']}")
            big = dict(dets)
            big[2] = _det(2, "lw-smoke-b", T0 + 120, 1000, 50.0, state="running",
                          insts=[(now - 3 * 3600, None), (now - 3600, None), (T0 + 120, T0 + 1200)])
            b2 = {c["id"]: c for c in spend("f6", source=_FakeSource(items, big), budget=rec, now=now)["clusters"]}[2]
            ok(b2["projected_usd"] == 0 and abs(b2["billed_usd"] - 111.0) < 1e-9,
               f"BG8d when Coiled has billed more than the projection, the billed amount counts alone: {b2}")
            cheap = dict(dets)
            cheap[1] = _det(1, "f6-refit-a", T0 + 60, 1, 0.01, insts=[(T0 + 60, T0 + 2 * 3600 + 60)])
            cheap[3] = _det(3, "f6-refit-old", T0 - 86400, 1, 0.01, insts=[(T0 - 86400, T0 - 86400 + 4 * 3600)])
            s2 = spend("f6", source=_FakeSource(items, cheap), budget=rec, now=now)
            ok(s2["rate_usd_per_instance_hour"] == 1.0,
               f"BG8b the rate never goes below the signed floor: {s2['rate_usd_per_instance_hour']}")
            s3 = spend("f6", source=_FakeSource(items[1:2], {2: dets[2]}), budget=rec, now=now)
            ok(s3["rate_usd_per_instance_hour"] == 1.0 and abs(s3["projected_usd"] - (4.3 - 1.61)) < 1e-9,
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
            ok(abs(m(2) - 4.3 * 5.55) < 1e-9 and abs(m(1) - 10.1) < 1e-9,
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
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-key", help="once, by the owner: a key pair, the private half into the login keychain")
    st = sub.add_parser("set", help="by the owner: sign a budget (the keychain asks for the owner's password)")
    st.add_argument("--scope", required=True)
    st.add_argument("--usd", type=float, required=True)
    st.add_argument("--note", required=True)
    st.add_argument("--start", default=None, help="ISO UTC time spend counts from (default: now)")
    for nm in ("show", "status"):
        sub.add_parser(nm).add_argument("--scope", default=None, help="default: every budget file present")
    for p in sub.choices.values():
        p.add_argument("--budget-dir", default=BUDGET_DIR)
        p.add_argument("--pub", default=PUB_FILE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "init-key":
            pub = init_key(Keychain(), a.pub)
            print(f"owner key created: private half in the login keychain ({KEYCHAIN_SERVICE}), public half in "
                  f"{a.pub} ({pub[:16]}...). Commit that file.")
        elif a.cmd == "set":
            print(f"signed: {set_budget(a.scope, a.usd, a.note, Keychain(), a.start, a.budget_dir, a.pub)}")
            print(json.dumps(load_budget(a.scope, a.budget_dir, a.pub), indent=1))
        else:
            names = os.listdir(a.budget_dir) if os.path.isdir(a.budget_dir) else []
            scopes = [a.scope] if a.scope else sorted(f[7:-5] for f in names
                                                      if f.startswith("budget_") and f.endswith(".json"))
            if not scopes:
                raise BudgetError(f"no budget in {a.budget_dir}")
            for sc in scopes:
                if a.cmd == "show":
                    print(json.dumps(load_budget(sc, a.budget_dir, a.pub), indent=1))
                else:
                    print("\n".join(status_lines(spend(sc, budget_dir=a.budget_dir, pub_file=a.pub))))
    except (BudgetError, OSError) as e:
        print(f"refused: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
