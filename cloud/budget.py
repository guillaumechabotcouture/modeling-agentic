"""Owner-signed cloud budget, measured spend, and the launch check (F6 Stage 2).

Why. On 2026-11-11 an agent operating this repo spent $106 of the owner's $100 cloud budget: the budget, the
dollar conversion and the spend checks lived in the agent's memory and judgment. Here they are code. The threat
is the agent's mistakes and forgetfulness, not deliberate circumvention: a bypass needs an edit that shows in git
(the tracked public key, this module, a launcher), and guarding against that is out of scope.

The budget is a record signed with the owner's Ed25519 key. The private key lives in the macOS login keychain with
an empty trusted-application list: `init-key` passes `add-generic-password ... -T "" -w <key>` to `security -i` on
stdin, so the key never appears on a command line. Every read raises the keychain's password dialog; the owner
clicks "Allow", never "Always Allow". The key is read only inside one signing call (`_sign`), which signs exactly
the record the owner confirmed on the terminal (/dev/tty), never returns or stores the key, and zeroes the buffer
it was read into. Residual: Python and `cryptography` make copies of the key while signing that cannot be zeroed,
and they stay in that process's memory until it exits. The trust anchor is the tracked public key
cloud/budget_owner.pub.

    python3 cloud/budget.py init-key            # once, by the owner; then commit cloud/budget_owner.pub
    python3 cloud/budget.py set --scope f6 --usd 200 --note "F6 refit + run" [--start <ISO UTC>] [--days 14]
    python3 cloud/budget.py show [--scope f6]   # verify and print the signed record
    python3 cloud/budget.py status [--scope f6] # budget, used (billed / estimated), reserved, remaining
    python3 cloud/budget.py --self-test

The record goes to ~/.config/modeling-agentic/budget_<scope>.json. It expires (default 14 days after it is issued,
`--days` at most 60). A record issued before the newest one already seen for its scope is refused, and so is the
same issued time with another nonce. The mark is ~/.config/modeling-agentic/budget_<scope>.seen, a local file:
it catches an older record restored by mistake, but it is not tamper-proof.

Spend. Every Coiled cluster in the IDM workspace whose name starts with one of the scope's prefixes
(cloud/budget_scopes.yaml, copied into the signed record) and that ran inside the budget's window. Billed USD of a
cluster = total_cost (Coiled credits) x 0.061 + cloud_total_cost (USD), from the raw cluster record (coiled
1.135.6: CloudV2._get_cluster_details_synced, coiled/v2/core.py:976; the public coiled.cluster_details,
core.py:994, drops the cost fields). A cluster's estimate is the sum over its instances, counted from instance
states rather than the cluster's, of hours in the window x max(the scope's median USD per instance-hour over its
last stopped clusters, the instance's VM-type upper bound from cloud/vm_prices.yaml). A finished cluster (every
process stopped or errored, with its stop time) counts what it billed, or the estimate if a cost is missing; its VM
records end with their processes. A cluster with a live instance counts max(billed, estimate). One created before
the budget's start counts its estimate from the start only. These refuse: an unknown VM type, missing scheduler,
worker or instance data, a negative cost, and a missing cost while an instance is live. A VM with no type yet
(still being provisioned) is priced at the bound of the VM type its cluster's other workers have (an untyped
scheduler: the highest among the scope's recent schedulers), or the table's highest when none is typed; a VM that
Azure never created (ended, no type, "Failed to create ..." or no time passed) counts $0. Both are stated as
assumptions in the breakdown. A named type that is not in the table refuses. A budget whose start is in the future refuses launches.

Reservations. `launch_budget_ok` holds an exclusive lock on the ledger (~/.config/modeling-agentic/reservations/
ledger.json) while it measures. When the launch fits, it records (scope, cap, name, invocation, time, and the
launcher's clusters.json when given). A reservation counts its whole cap until its launch's clusters appear, then
max(0, cap - their spend) until all of them are stopped, when it is released; one whose clusters never appear
lapses after 2 h. Its launch's clusters are the ids its clusters.json records for its invocation, when it does.
Otherwise they are the exact cluster it names and its helpers (<name>-keeper, <name>-overflow), created after the
reservation and before the next reservation of the same name, so a relaunch's clusters never hold an earlier
launch's reservation. `reattach_budget_ok` (`vm_job wait --cap-usd`) replaces a running cluster's reservations
with one of the new cap less the cluster's lifetime cost, bound to its id. A malformed ledger refuses. This guards against concurrent launches from this machine only.
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
BUDGET_DIR = os.path.expanduser("~/.config/modeling-agentic")
SCOPES_FILE = os.path.join(HERE, "budget_scopes.yaml")
PRICES_FILE = os.path.join(HERE, "vm_prices.yaml")
KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "modeling-agentic-budget-owner", "owner"
USD_PER_CREDIT = 0.061
RATE_SAMPLE = 5                 # stopped clusters the measured rate is the median of
RESERVATION_S = 2 * 3600        # a reservation lapses this long after it was made, if no cluster of its name appears
MAX_DAYS = 60
CLOCK_SKEW_S = 30            # tolerated difference between this clock and Coiled's
# VM types seen on our clusters' instance records (read-only check, 2026-11-11): every scheduler and keeper, the
# P6 compute workers, and vm_job --cpu 32 / --cpu 64. vm_job sizes by --cpu, and Coiled picked Standard_D<cpu>_v5.
OBSERVED_VM_TYPES = ("Standard_D2_v5", "Standard_E8s_v5", "Standard_D32_v5", "Standard_D64_v5", "Standard_F8s_v2")
ENDED = ("stopped", "error")
FIELDS = ("scope", "usd", "start_utc", "note", "issued_utc", "expires_utc", "nonce", "prefixes",
          "floor_usd_per_instance_hour")


class BudgetError(Exception):
    """A budget that cannot be verified or measured: the caller refuses."""


class Keychain:
    """The only access to the owner's private key: a generic password in the login keychain, trusted by no
    application (`-T ""`), so each read raises the keychain's password dialog."""

    def __init__(self, run=subprocess.run):
        self.run = run

    def _who(self):
        return ["-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT]

    def exists(self) -> bool:     # attributes only: reads no secret, raises no dialog
        return self.run(["security", "find-generic-password", *self._who()], capture_output=True).returncode == 0

    def put(self, secret_hex: str) -> None:
        line = " ".join(["add-generic-password", *self._who(), "-l", "modeling-agentic-budget-owner-key",
                         "-T", '""', "-w", secret_hex]) + "\n"
        r = self.run(["security", "-i"], input=line, capture_output=True, text=True)
        err = (r.stderr or "").replace(secret_hex, "<key>").strip()
        if r.returncode != 0 or err:      # -i may exit 0 after a failed command: its error text counts
            raise BudgetError(f"keychain refused the key (security exit {r.returncode}): {err[:200]}")

    def read_into(self) -> bytearray:
        """The key's hex in a bytearray the caller zeroes (the prompted read)."""
        r = self.run(["security", "find-generic-password", *self._who(), "-w"], capture_output=True)
        if r.returncode != 0:
            raise BudgetError(f"the keychain read was refused or failed (security exit {r.returncode})")
        return bytearray(r.stdout.strip())


class _Env:
    """Where the budget lives and what reads Coiled. Production uses the defaults; only --self-test swaps it."""

    def __init__(self, budget_dir=BUDGET_DIR, pub_file=PUB_FILE, store=None, source=None, clock=time.time,
                 reservations=None, scopes=SCOPES_FILE, prices=PRICES_FILE, confirm=None):
        self.budget_dir, self.pub_file, self.store, self.source, self.clock = budget_dir, pub_file, store, source, clock
        self.reservations = reservations or os.path.join(budget_dir, "reservations", "ledger.json")
        self.scopes, self.prices, self.confirm = scopes, prices, confirm or _confirm_on_tty

    def path(self, name):
        return os.path.join(self.budget_dir, name)

    def keys(self):
        return self.store or Keychain()

    def coiled(self):
        return self.source or CoiledSource()


def _confirm_on_tty(rec, tty="/dev/tty") -> bool:
    """The owner reads the record and types its amount on the terminal; with no terminal, nothing is signed. A
    terminal is not seekable, so it gets separate read and write streams."""
    try:       # O_NOCTTY: opening a terminal must never make it this process's controlling terminal (CI: a pty
        # opened by a session leader without one became its controlling terminal, and closing it sent SIGHUP)
        with os.fdopen(os.open(tty, os.O_RDONLY | os.O_NOCTTY), "r") as rd, \
                os.fdopen(os.open(tty, os.O_WRONLY | os.O_NOCTTY), "w", buffering=1) as wr:
            wr.write(json.dumps(rec, indent=1) + f"\nType the amount in USD to sign this {rec['scope']} budget: ")
            wr.flush()
            return float(rd.readline().strip()) == float(rec["usd"])
    except (OSError, ValueError):
        return False


_ENV = _Env()


# ---- implementation --------------------------------------------------------------------------

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


def _num(x, what, minimum=None) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise BudgetError(f"{what} is not a number: {x!r}")
    if minimum is not None and x < minimum:
        raise BudgetError(f"{what} is below {minimum}: {x!r}")
    return float(x)


def _canon(record) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _scope_name(scope) -> str:
    if not isinstance(scope, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", scope):
        raise BudgetError(f"bad scope name {scope!r}")
    return scope


def _write_private(path, text) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    os.replace(tmp, path)


def init_key(env=None) -> str:
    """A new key pair: the private half into the keychain, the public half (hex) to the tracked public-key file.
    Refuses to replace either."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    env = env or _ENV
    store = env.keys()
    if os.path.exists(env.pub_file) or store.exists():
        raise BudgetError(f"a key exists already ({env.pub_file} or the keychain item {KEYCHAIN_SERVICE}): refusing")
    k = Ed25519PrivateKey.generate()
    store.put(k.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
    if not store.exists():
        raise BudgetError("the keychain does not hold the new key: no public key written")
    pub = k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    with open(env.pub_file, "x") as f:
        f.write(pub + "\n")
    return pub


def _read_pub(env) -> str:
    try:
        v = open(env.pub_file).read().strip()
    except OSError as e:
        raise BudgetError(f"no owner public key at {env.pub_file} ({e}): the owner runs `budget.py init-key`") from None
    if not re.fullmatch(r"[0-9a-f]{64}", v):
        raise BudgetError(f"{env.pub_file} is not an Ed25519 public key in hex")
    return v


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


def load_prices(path=PRICES_FILE) -> dict:
    """{VM type: upper-bound USD per instance-hour} (see cloud/vm_prices.yaml)."""
    import yaml
    try:
        y = yaml.safe_load(open(path))
        m, c = _num(y["margin"], "margin", 1.0), _num(y["credits_per_vcpu_hour"], "credits_per_vcpu_hour", 0.0)
        return {t: _num(v["usd_per_hour"], t, 0.0) * m + _num(v["vcpu"], t, 1) * c * USD_PER_CREDIT
                for t, v in y["types"].items()}
    except BudgetError:
        raise
    except Exception as e:  # noqa: BLE001
        raise BudgetError(f"{path} cannot be read: {type(e).__name__}: {e}") from None


def _check_record(rec) -> dict:
    if not isinstance(rec, dict) or sorted(rec) != sorted(FIELDS):
        raise BudgetError(f"the budget record's fields are not {FIELDS}")
    _scope_name(rec["scope"])
    if _num(rec["usd"], "usd") <= 0 or _num(rec["floor_usd_per_instance_hour"], "floor") <= 0:
        raise BudgetError("the budget and the floor must be positive")
    times = [_ts(rec[k]) for k in ("start_utc", "issued_utc", "expires_utc")]
    if None in times or times[2] <= times[1] or not isinstance(rec["nonce"], str) or len(rec["nonce"]) < 16:
        raise BudgetError("bad times or nonce in the budget record")
    if not (isinstance(rec["prefixes"], list) and rec["prefixes"] and all(isinstance(p, str) and p
                                                                          for p in rec["prefixes"])):
        raise BudgetError("bad prefixes in the budget record")
    return rec


def _verify(doc, pub_hex) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        rec, sig = doc["record"], bytes.fromhex(doc["signature"])
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(sig, _canon(rec))
    except (InvalidSignature, KeyError, TypeError, ValueError, AttributeError):
        raise BudgetError("the budget's signature does not verify against the owner's public key") from None
    return _check_record(rec)


def _sign(rec, store) -> dict:
    """The one place the private key is read: sign exactly `rec`, then zero the buffer. Returns the signed document
    only, never the key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    msg = _canon(_check_record(rec))
    buf = store.read_into()
    try:
        sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(buf.decode())).sign(msg)
    except ValueError:
        raise BudgetError("the keychain item is not an Ed25519 key") from None
    finally:
        buf[:] = b"\0" * len(buf)
    return {"alg": "ed25519", "record": rec, "signature": sig.hex()}


def _read_seen(env, scope):
    try:
        s = json.load(open(env.path(f"budget_{scope}.seen")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise BudgetError(f"the {scope} .seen mark cannot be read: {e}") from None
    if not isinstance(s, dict) or _ts(s.get("issued_utc")) is None or not isinstance(s.get("nonce"), str):
        raise BudgetError(f"the {scope} .seen mark is malformed")
    return s


def _check_seen(env, rec) -> None:
    """Refuse a record older than the newest seen for its scope, or the same issued time with another nonce;
    raise the mark to a newer record."""
    s = _read_seen(env, rec["scope"])
    if s is not None:
        a, b = _ts(rec["issued_utc"]), _ts(s["issued_utc"])
        if a < b or (a == b and rec["nonce"] != s["nonce"]):
            raise BudgetError(f"this {rec['scope']} budget (issued {rec['issued_utc']}) is not the newest one seen "
                              f"({s['issued_utc']}): an older record restored is refused")
        if a == b:
            return
    _write_private(env.path(f"budget_{rec['scope']}.seen"),
                   json.dumps({"issued_utc": rec["issued_utc"], "nonce": rec["nonce"]}) + "\n")


def set_budget(scope, usd, note, start_utc=None, days=14, env=None) -> str:
    """Sign a budget after the owner confirms the record on the terminal (the keychain then asks for the owner's
    password), and write it with its `.seen` mark."""
    import secrets
    env = env or _ENV
    sc = load_scopes(env.scopes)
    if _scope_name(scope) not in sc["scopes"]:
        raise BudgetError(f"scope {scope!r} is not in {env.scopes}")
    if not 0 < _num(days, "days") <= MAX_DAYS:
        raise BudgetError(f"--days must be 1 to {MAX_DAYS}, not {days}")
    now = env.clock()
    seen = _read_seen(env, scope)
    if seen is not None and _ts(seen["issued_utc"]) >= math.floor(now):
        raise BudgetError("a record for this scope was issued this second or later: try again in a second")
    rec = _check_record({"scope": scope, "usd": _num(usd, "usd"), "start_utc": start_utc or _iso(now),
                         "note": str(note or ""), "issued_utc": _iso(now), "expires_utc": _iso(now + days * 86400),
                         "nonce": secrets.token_hex(16), "prefixes": sc["scopes"][scope],
                         "floor_usd_per_instance_hour": sc["floor"]})
    if not env.confirm(rec):
        raise BudgetError("not confirmed on the terminal: nothing signed")
    doc = _sign(rec, env.keys())
    _verify(doc, _read_pub(env))       # the keychain's key is the tracked anchor's pair, or nothing is written
    p = env.path(f"budget_{scope}.json")
    _write_private(p, json.dumps(doc, indent=1) + "\n")
    _check_seen(env, rec)
    return p


def load_budget(scope, env=None) -> dict:
    """The scope's verified, unexpired record, not older than the newest seen, else BudgetError."""
    env = env or _ENV
    p = env.path(f"budget_{_scope_name(scope)}.json")
    try:
        doc = json.load(open(p))
    except FileNotFoundError:
        raise BudgetError(f"no budget for {scope} ({p}): the owner signs one with `budget.py set`") from None
    except (OSError, ValueError) as e:
        raise BudgetError(f"{p} cannot be read: {e}") from None
    rec = _verify(doc, _read_pub(env))
    if rec["scope"] != scope:
        raise BudgetError(f"{p} holds the budget of {rec['scope']!r}, not {scope!r}")
    if env.clock() >= _ts(rec["expires_utc"]):
        raise BudgetError(f"the {scope} budget expired at {rec['expires_utc']}: the owner signs a new one")
    _check_seen(env, rec)
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


_RUNNING = ("started", "running", "ready")


def _never_ran(inst) -> bool:
    """An ended VM record with no type that never ran: Azure refused to create it (quota), or no time passed."""
    cs = inst.get("current_state") or {}
    return (state_of(inst) in ENDED and not inst.get("instance_type_id")
            and (str(cs.get("reason") or "").startswith("Failed to create")
                 or _ts(cs.get("updated")) == _ts(inst.get("created"))))


def _vm_type(p):
    inst = (p or {}).get("instance") if isinstance(p, dict) else None
    return (inst or {}).get("instance_type_id") or None


def _cluster_cost(d, now, start, median, prices, sched_bound=None) -> dict:
    """One cluster: billed USD, the estimate in the window, and the count. A finished cluster (every process stopped
    or errored, with its stop time) counts what it billed; its VM records end with their processes, even one Coiled
    left non-final (a VM refused for quota stays `queued`), and the conservative estimate stands in only for a
    missing cost. A cluster with a live instance counts max(billed, estimate). One created before `start` counts
    the estimate in the window. Refuses incomplete or negative data."""
    if not isinstance(d, dict) or not {"scheduler", "workers", "total_cost", "cloud_total_cost", "created"} <= set(d) \
            or not isinstance(d["workers"], list) or not isinstance(d["scheduler"], dict):
        raise BudgetError(f"cluster {d.get('id') if isinstance(d, dict) else '?'}: incomplete record")
    procs = [d["scheduler"]] + d["workers"]
    done = all(isinstance(p, dict) and state_of(p) in ENDED and _ts((p.get("current_state") or {}).get("updated"))
               is not None for p in procs)
    live, hours, est, untyped, failed = 0, 0.0, 0.0, 0, 0
    top = max(prices.values())
    worker_typed = [prices[t] for t in map(_vm_type, d["workers"]) if t in prices]
    worker_bound = max(worker_typed) if worker_typed else top     # an untyped worker: like the cluster's other workers
    sched_bound = top if sched_bound is None else sched_bound     # an untyped scheduler: like the scope's schedulers
    for p in procs:
        inst = p.get("instance") if isinstance(p, dict) else None
        if not inst:
            if not isinstance(p, dict) or state_of(p) in _RUNNING or not state_of(p):
                raise BudgetError(f"cluster {d.get('id')}: a process without instance data")
            continue                       # a process that never got a VM (pending, refused)
        if _never_ran(inst):
            failed += 1                    # Azure never created it (e.g. quota): nothing ran, nothing billed
            continue
        t0, vm = _ts(inst.get("created")), inst.get("instance_type_id")
        ended = state_of(inst) in ENDED
        on = not done and not ended
        t1 = (now if on else _ts((inst.get("current_state") or {}).get("updated")) if ended
              else _ts(p["current_state"]["updated"]))          # a finished cluster's VM ends with its process
        if t0 is None or t1 is None or not state_of(inst):
            raise BudgetError(f"cluster {d.get('id')}: an instance has no state, start or stop time")
        if (not on and t1 < t0) or t0 > now + CLOCK_SKEW_S:
            raise BudgetError(f"cluster {d.get('id')}: an instance stopped before it started, or starts in the future")
        if vm is None or vm == "":        # a VM still being provisioned has no type yet
            bound = sched_bound if p is d["scheduler"] else worker_bound
            untyped += 1
        elif vm not in prices:
            raise BudgetError(f"cluster {d.get('id')}: VM type {vm!r} is not in cloud/vm_prices.yaml")
        else:
            bound = prices[vm]
        hours += max(0.0, t1 - t0) / 3600
        est += max(0.0, t1 - max(t0, start)) / 3600 * max(median, bound)
        live += on
    tc, cc = d["total_cost"], d["cloud_total_cost"]
    partial = live and (tc is None or cc is None)
    if partial:                            # not (fully) billed yet: the estimate, at the bound, covers it
        tc, cc = (0.0 if tc is None else tc), (0.0 if cc is None else cc)
    fallback = done and (tc is None or cc is None)
    if fallback:                           # a finished cluster missing a cost: the conservative estimate stands in
        billed = _num(tc, "total_cost", 0.0) * USD_PER_CREDIT if tc is not None else 0.0
        _num(cc, "cloud_total_cost", 0.0) if cc is not None else None
    else:
        billed = _num(tc, "total_cost", 0.0) * USD_PER_CREDIT + _num(cc, "cloud_total_cost", 0.0)
    count = billed if done and not fallback else max(billed, est)
    created = _ts(d["created"])
    if created is None:
        raise BudgetError(f"cluster {d.get('id')}: no valid creation time in its record")
    before = created < start
    return {"id": d.get("id"), "name": d.get("name"), "state": state_of(d), "created": created, "live_instances": live,
            "instance_hours": hours, "billed_usd": 0.0 if before else billed, "estimate_usd": est,
            "usd": est if before else count, "_billed_all": None if fallback or partial else billed,
            "assumptions": ([f"{untyped} instance(s) with no VM type yet (still provisioning), priced at the bound "
                             f"of the cluster's other workers' VM type (an untyped scheduler: the scope's schedulers'), "
                             f"or the table's highest, ${top:.2f} per hour, when none is typed"] if untyped else [])
            + ([f"{failed} instance(s) failed to create, counted $0"] if failed else [])
            + (["a cost field is missing on this live cluster; counted max(the part billed, the estimate at the "
                "VM bound)"] if partial and (d["total_cost"] is not None or d["cloud_total_cost"] is not None) else [])}


def _measure(rec, env, now) -> dict:
    """Spend of a verified record's scope (clusters only), and the rate context the job meter reuses."""
    src, prices = env.coiled(), load_prices(env.prices)
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
    mine.sort(key=lambda x: -x[0])         # newest first

    def overlaps(t, c):
        upd = _ts((c.get("current_state") or {}).get("updated")) if isinstance(c.get("current_state"), dict) else None
        return t >= start or state_of(c) != "stopped" or upd is None or upd >= start
    counted = [c for t, c in mine if overlaps(t, c)]
    dets = {c["id"]: src.details(c["id"]) for c in counted}
    sample = []
    for t, c in mine:
        if len(sample) >= RATE_SAMPLE:
            break
        if state_of(c) in ENDED:
            r = _cluster_cost(dets[c["id"]] if c["id"] in dets else src.details(c["id"]), now, -math.inf, 0.0, prices)
            if not r["live_instances"] and r["instance_hours"] >= 0.05 and r["_billed_all"] is not None:
                sample.append(r["_billed_all"] / r["instance_hours"])
    sample.sort()
    n = len(sample)
    median = 0.0 if not n else sample[n // 2] if n % 2 else (sample[n // 2 - 1] + sample[n // 2]) / 2
    median = max(median, _num(rec["floor_usd_per_instance_hour"], "floor"))
    seen = [prices[t] for t in (_vm_type(x.get("scheduler")) for x in dets.values() if isinstance(x, dict))
            if t in prices]
    sched_bound = max(seen) if seen else None
    rows = [_cluster_cost(dets[c["id"]], now, start, median, prices, sched_bound) for c in counted]
    for r, c in zip(rows, counted):
        r.pop("_billed_all")
        r["listed_state"] = state_of(c)
    return {"median": median, "rate_from_clusters": n, "prices": prices, "start": start, "rows": rows,
            "sched_bound": sched_bound,
            "items": items}


def _ledger(env, now) -> list:
    """The reservations ledger: a list of {scope, cap_usd, name, invocation, time[, clusters_file]}; anything else
    refuses."""
    try:
        allr = json.load(open(env.reservations))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        raise BudgetError(f"{env.reservations} cannot be read: {e}") from None
    def num(x):
        return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)
    ok = isinstance(allr, list) and all(
        isinstance(r, dict) and set(r) - {"clusters_file"} == {"scope", "cap_usd", "name", "invocation", "time"}
        and isinstance(r.get("clusters_file", ""), str)
        and isinstance(r["scope"], str) and isinstance(r["name"], str) and r["name"] and isinstance(r["invocation"], str)
        and num(r["cap_usd"]) and r["cap_usd"] > 0 and num(r["time"]) and 0 < r["time"] <= now + CLOCK_SKEW_S
        for r in allr)
    if not ok:
        raise BudgetError(f"{env.reservations} is not a list of reservations: refusing")
    return allr


def _launch_ids(r):
    """The cluster ids the reservation's launch recorded in its clusters.json for its invocation, or None (no file,
    another invocation's record, or no id yet)."""
    try:
        rec = json.load(open(r["clusters_file"])) if r.get("clusters_file") else None
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("invocation") != r["invocation"]:
        return None
    ids = {v.get("id") for k, v in rec.items() if k in ("run", "keeper", "overflow") and isinstance(v, dict)} - {None}
    return ids or None


def _reservations(env, scope, rows, now):
    """(this scope's open reservations, each with its outstanding USD; the ledger to keep). A reservation names the
    exact cluster its launch creates (helpers: <name>-keeper, <name>-overflow); it holds its cap until such a cluster
    created after the reservation appears, then max(0, cap - those clusters' spend) while any of them runs;
    it is released once all are stopped, or after RESERVATION_S with none. Its launch's clusters: the ids its
    clusters.json records for its invocation, when it does; otherwise those of its names created after it and before
    the next reservation of the same name (a relaunch's clusters never hold an earlier launch's reservation)."""
    mine, keep = [], []
    ledger = _ledger(env, now)
    for r in ledger:
        if r["scope"] != scope:
            keep.append(r)                 # another scope's: measured by its own launches
            continue
        ids = _launch_ids(r)
        if ids is not None:
            match = [x for x in rows if x["id"] in ids]
        else:
            own = {r["name"], r["name"] + "-keeper", r["name"] + "-overflow"}   # the launch's cluster and its helpers
            nxt = min((q["time"] for q in ledger if q is not r and q["scope"] == scope and q["name"] == r["name"]
                       and q["time"] > r["time"]), default=math.inf)
            match = [x for x in rows if x["name"] in own and r["time"] - CLOCK_SKEW_S <= x["created"]
                     < nxt - CLOCK_SKEW_S]
        if match:
            if not any(x["live_instances"] or x["listed_state"] not in ENDED for x in match):
                continue                   # every cluster of the launch stopped: released
            out = max(0.0, r["cap_usd"] - sum(x["usd"] for x in match))
        elif now - r["time"] < RESERVATION_S:
            out = r["cap_usd"]
        else:
            continue                       # no cluster within 2 h: lapsed
        keep.append(r)
        mine.append(dict(r, outstanding_usd=out, cluster_ids=[x["id"] for x in match]))
    return mine, keep


def _spend(scope, env, now) -> dict:
    rec = load_budget(scope, env)
    m = _measure(rec, env, now)
    rows = m["rows"]
    mine, keep = _reservations(env, scope, rows, now)
    used, reserved = sum(r["usd"] for r in rows), sum(r["outstanding_usd"] for r in mine)
    return {"scope": scope, "budget_usd": float(rec["usd"]), "start_utc": rec["start_utc"], "note": rec["note"],
            "expires_utc": rec["expires_utc"], "prefixes": list(rec["prefixes"]), "rate_usd_per_instance_hour":
            m["median"], "rate_from_clusters": m["rate_from_clusters"], "billed_usd": sum(r["billed_usd"] for r in rows),
            "used_usd": used, "reserved_usd": reserved, "remaining_usd": float(rec["usd"]) - used - reserved,
            "clusters": rows, "reservations": mine, "_m": m, "_rec": rec, "_keep": keep}


def spend(scope) -> dict:
    """The scope's spend: verified budget, clusters, open reservations (see the module doc)."""
    s = _spend(scope, _ENV, _ENV.clock())
    s.pop("_m"), s.pop("_rec"), s.pop("_keep")
    return s


class _Lock:
    def __init__(self, path):
        self.path = path + ".lock"

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        self.f.close()
        return False


def launch_budget_ok(scope, cap_usd, name, reserve=True, invocation=None, clusters_file=None) -> tuple[bool, str]:
    """ok only if the scope's owner-signed budget verifies and has started, `name` starts with one of its signed
    prefixes, and used + open reservations + `cap_usd` is within it. With `reserve`, a launch that fits records a
    reservation of `cap_usd` for `name` (under the same lock), under `invocation` (default MA_LAUNCH_INVOCATION) and,
    when given, the launcher's clusters.json, whose ids for that invocation bind the reservation. Any error refuses."""
    env = _ENV
    try:
        cap = _num(cap_usd, "cap_usd")
        if cap <= 0:
            raise BudgetError(f"cap_usd must be positive, not {cap}")
        if not isinstance(name, str) or not name:
            raise BudgetError("a launch names its cluster")
        with _Lock(env.reservations):
            now = env.clock()
            s = _spend(scope, env, now)
            if now < _ts(s["start_utc"]):
                return False, f"the {scope} budget starts at {s['start_utc']}: no launch before it"
            if not name.startswith(tuple(s["prefixes"])):
                return False, f"{name} is outside budget {scope}'s signed prefixes {s['prefixes']}"
            fits = round(s["used_usd"] + s["reserved_usd"] + cap, 6) <= round(s["budget_usd"], 6)
            if fits and reserve:
                keep = s["_keep"]
                inv = invocation or os.environ.get("MA_LAUNCH_INVOCATION") or os.urandom(6).hex()
                entry = {"scope": scope, "cap_usd": cap, "name": name, "invocation": str(inv), "time": now}
                if clusters_file:
                    entry["clusters_file"] = os.path.abspath(str(clusters_file))
                keep.append(entry)
                os.makedirs(os.path.dirname(env.reservations), exist_ok=True)
                with open(env.reservations + ".tmp", "w") as f:
                    json.dump(keep, f, indent=1)
                os.replace(env.reservations + ".tmp", env.reservations)
    except Exception as e:  # noqa: BLE001 -- fail closed
        return False, f"budget check failed closed: {type(e).__name__}: {e}"
    return fits, (f"{scope}: used ${s['used_usd']:.2f} (billed ${s['billed_usd']:.2f}) + reserved "
                  f"${s['reserved_usd']:.2f} + cap ${cap:.2f} {'<=' if fits else '>'} budget ${s['budget_usd']:.2f}")


def job_meter(scope):
    """fn(cluster id) -> that cluster's USD now (max of billed and the estimate), at the scope's rates."""
    env = _ENV
    s = _spend(scope, env, env.clock())
    m = s["_m"]

    def meter(cid):
        return _cluster_cost(env.coiled().details(cid), env.clock(), m["start"], m["median"], m["prices"],
                             m["sched_bound"])["usd"]
    return meter


def lifetime_meter(scope):
    """fn(cluster id) -> that cluster's USD since its creation (max of billed and the estimate while it runs), at the
    scope's rates: what a re-attached cap (`vm_job wait --cap-usd`) is enforced on. Scope accounting keeps the
    windowed figure (job_meter)."""
    env = _ENV
    m = _spend(scope, env, env.clock())["_m"]

    def meter(cid):
        return _cluster_cost(env.coiled().details(cid), env.clock(), -math.inf, m["median"], m["prices"],
                             m["sched_bound"])["usd"]
    return meter


def reattach_budget_ok(scope, cap_usd, name, cluster_id, clusters_file, invocation=None) -> tuple[bool, str]:
    """`vm_job wait --cap-usd`: re-attach a cap to the running cluster `cluster_id`. Under the ledger lock: refused when
    its lifetime cost is at or above the cap, or when used + the other launches' reservations + (cap - lifetime cost)
    exceeds the budget. When it fits, the cluster's reservations are replaced by one, bound to `cluster_id` through
    `clusters_file`, whose outstanding amount is the cap's remaining. A budget, listing or cost that cannot be read
    raises (BudgetError or the source's error): the caller retries, then stops the cluster."""
    env = _ENV
    cap = _num(cap_usd, "cap_usd")
    if cap <= 0:
        raise BudgetError(f"cap_usd must be positive, not {cap}")
    with _Lock(env.reservations):
        now = env.clock()
        s = _spend(scope, env, now)
        if not name.startswith(tuple(s["prefixes"])):
            return False, f"{name} is outside budget {scope}'s signed prefixes {s['prefixes']}"
        row = next((x for x in s["clusters"] if x["id"] == cluster_id), None)
        if row is None:
            raise BudgetError(f"cluster {cluster_id} is not among the {scope} budget's clusters")
        m = s["_m"]
        life = _cluster_cost(env.coiled().details(cluster_id), now, -math.inf, m["median"], m["prices"],
                             m["sched_bound"])["usd"]
        if life >= cap:
            return False, (f"cluster {cluster_id} has already spent ${life:.2f} since its creation, at or above the "
                           f"${cap:.2f} cap")
        remaining = cap - life
        own = [r for r in s["reservations"] if cluster_id in r["cluster_ids"]]
        others = s["reserved_usd"] - sum(r["outstanding_usd"] for r in own)
        fits = round(s["used_usd"] + others + remaining, 6) <= round(s["budget_usd"], 6)
        why = (f"{scope}: used ${s['used_usd']:.2f} + other reservations ${others:.2f} + the rest of this cap "
               f"${remaining:.2f} (cap ${cap:.2f} - ${life:.2f} spent) {'<=' if fits else '>'} budget "
               f"${s['budget_usd']:.2f}")
        if fits:
            fields = ("scope", "cap_usd", "name", "invocation", "time", "clusters_file")
            drop = [{k: r[k] for k in fields if k in r} for r in own]
            keep = [r for r in s["_keep"] if r not in drop]
            inv = invocation or os.urandom(6).hex()
            _write_json_atomic(clusters_file, {"invocation": inv, "run": {"id": cluster_id, "name": name}})
            # outstanding = max(0, cap - the cluster's windowed spend), so the entry's cap is the remaining plus that
            keep.append({"scope": scope, "cap_usd": remaining + row["usd"], "name": name, "invocation": inv,
                         "time": now, "clusters_file": os.path.abspath(str(clusters_file))})
            _write_json_atomic(env.reservations, keep)
    return fits, why


def _write_json_atomic(path, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(path + ".tmp", path)


def status_lines(s) -> list[str]:
    out = [f"budget {s['scope']}: ${s['budget_usd']:.2f} from {s['start_utc']} to {s['expires_utc']} ({s['note']})",
           f"used: ${s['used_usd']:.2f} (billed ${s['billed_usd']:.2f}); reserved ${s['reserved_usd']:.2f}; "
           f"remaining ${s['remaining_usd']:.2f}",
           f"measured rate: ${s['rate_usd_per_instance_hour']:.2f} per instance-hour (median of "
           f"{s['rate_from_clusters']} stopped cluster(s), floor applied; each VM type's bound applies when higher)",
           f"{'cluster':<40} {'id':>9} {'state':<9} {'billed':>9} {'estimate':>9} {'counted':>9} {'live VMs':>8}"]
    out += [f"{str(r['name']):<40} {str(r['id']):>9} {r['state']:<9} {r['billed_usd']:>9.2f} {r['estimate_usd']:>9.2f} "
            f"{r['usd']:>9.2f} {r['live_instances']:>8}" for r in s["clusters"]]
    out += [f"  assumption, {r['name']}: {a}" for r in s["clusters"] for a in r.get("assumptions", [])]
    return out + [f"reservation {r['name']}: cap ${r['cap_usd']:.2f}, outstanding ${r['outstanding_usd']:.2f} "
                  f"({r['invocation']})" for r in s["reservations"]]


# ---- self-test -------------------------------------------------------------------------------

class _FakeKeys:
    def __init__(self, secret=None):
        self.secret, self.reads, self.handed = secret, 0, []

    def exists(self):
        return self.secret is not None

    def put(self, s):
        self.secret = s

    def read_into(self):
        self.reads += 1
        if self.secret is None:
            raise BudgetError("no key")
        b = bytearray(self.secret.encode())
        self.handed.append(b)
        return b


class _FakeSource:
    def __init__(self, items, details):
        self.items, self.det, self.calls = items, details, []

    def list_clusters(self):
        self.calls.append("list")
        return [dict(c) for c in self.items]

    def details(self, cid):
        self.calls.append(cid)
        d = self.det[cid]
        if isinstance(d, Exception):
            raise d
        return json.loads(json.dumps(d))


def _det(cid, name, created, credits, cloud, state="stopped", insts=()):
    """A raw cluster record; insts: (created, end or None while live, VM type); the first is the scheduler's."""
    procs = [{"current_state": {"state": "stopped" if e else "started", "updated": _iso(e or c)},
              "instance": {"created": _iso(c), "instance_type_id": vm,
                           "current_state": {"state": "stopped" if e else "ready", "updated": _iso(e or c)}}}
             for c, e, vm in insts]
    return {"id": cid, "name": name, "created": _iso(created), "current_state": {"state": state},
            "total_cost": credits, "cloud_total_cost": cloud, "scheduler": procs[0] if procs else None,
            "workers": procs[1:]}


def _item(cid, name, created, state="stopped", updated=None):
    return {"id": cid, "name": name, "created": _iso(created),
            "current_state": {"state": state, "updated": _iso(updated if updated is not None else created + 3 * 3600)}}


def _self_test() -> int:
    global _ENV
    import contextlib
    import inspect
    import io
    import tempfile
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

    real_env = _ENV
    T0 = 1_762_819_200.0   # 2025-11-11T00:00:00Z
    H = 3600
    with tempfile.TemporaryDirectory() as d:
        sc, pr = os.path.join(d, "scopes.yaml"), os.path.join(d, "prices.yaml")
        open(sc, "w").write("floor_usd_per_instance_hour: 1.0\nscopes:\n  f6: [f6-refit-, lw-smoke-]\n  other: [ev-]\n")
        open(pr, "w").write("margin: 1.0\ncredits_per_vcpu_hour: 0\ntypes:\n  vmA: {vcpu: 1, usd_per_hour: 2.0}\n"
                            "  vmB: {vcpu: 1, usd_per_hour: 10.0}\n")
        clock, stores = [T0 + 10 * H], {}

        def env(root, store=None, source=None, confirm=lambda rec: True, pub=None):
            return _Env(budget_dir=root, pub_file=pub or os.path.join(root, "owner.pub"), store=store or stores.get(root),
                        source=source, clock=lambda: clock[0], scopes=sc, prices=pr, confirm=confirm)

        def root(name):
            p = os.path.join(d, name)
            os.makedirs(p)
            return p

        def signed(name, usd=200.0, start=T0, days=14):
            r = root(name)
            stores[r] = _FakeKeys()
            init_key(env(r))
            set_budget("f6", usd, "test", _iso(start), days, env(r))
            return r

        def bk1():
            r = root("k1")
            st = stores[r] = _FakeKeys()
            init_key(env(r))
            ok(st.secret and len(open(os.path.join(r, "owner.pub")).read().strip()) == 64 and st.reads == 0,
               "BK1 init-key puts the private key in the key store (no read) and writes the public key")
            ok(refused(lambda: init_key(env(r))) is not None
               and refused(lambda: init_key(env(r, store=_FakeKeys("ab"), pub=os.path.join(r, "p2")))) is not None,
               "BK1b init-key refuses when the public key file or the keychain item already exists")
        case("BK1", bk1)

        def bk2():
            import types
            calls, inputs = [], []
            secret = "cd" * 32

            def run(argv, **kw):
                calls.append(argv)
                inputs.append(kw.get("input"))
                hit = argv[1:2] != ["find-generic-password"] or "-w" in argv
                return types.SimpleNamespace(returncode=0 if hit else 44, stdout=(secret + "\n").encode(), stderr="")
            k = Keychain(run=run)
            k.put(secret)
            buf, ex = k.read_into(), k.exists()
            line = str(inputs[0] or "")
            toks = line.split()
            ok(calls[0] == ["security", "-i"] and not any(secret in a for c in calls for a in c)
               and toks[:1] == ["add-generic-password"] and secret in toks and toks[toks.index("-T") + 1] == '""'
               and "-A" not in toks and "-U" not in toks and toks[toks.index("-s") + 1] == KEYCHAIN_SERVICE,
               f"BK2 the key goes to `security -i` on stdin, never on a command line, with an empty trusted-application "
               f"list (-T \"\"), never -A or -U: {line.replace(secret, '<key>')[:100]}")
            ok(isinstance(buf, bytearray) and bytes(buf) == secret.encode() and "-w" in calls[1] and ex is False
               and "-w" not in calls[2],
               "BK2b the key is read with find-generic-password -w into a bytearray; exists reads no secret")
            k2 = Keychain(run=lambda argv, **kw: types.SimpleNamespace(returncode=51, stdout=b"", stderr="denied"))
            ok(refused(k2.read_into) is not None, "BK2c a refused or failed keychain read raises (no key, no signature)")
        case("BK2", bk2)

        def bk3():
            r = root("k3")
            st = stores[r] = _FakeKeys()
            init_key(env(r))
            ok(refused(lambda: set_budget("f6", 50, "x", None, 14, env(r, confirm=lambda rec: False))) is not None
               and st.reads == 0 and not os.path.exists(os.path.join(r, "budget_f6.json")),
               "BK3 set reads no key and signs nothing unless the owner confirms the record on the terminal")
            shown = []
            p = set_budget("f6", 50, "x", _iso(T0), 14, env(r, confirm=lambda rec: shown.append(dict(rec)) or True))
            rec = load_budget("f6", env(r))
            ok(st.reads == 1 and all(b == 0 for b in st.handed[0]) and st.secret not in open(p).read()
               and rec == shown[0] and oct(os.stat(p).st_mode)[-3:] == "600",
               "BK3b one keychain read signs exactly the record shown; the key buffer is zeroed and never written out")
            ok("/dev/tty" in inspect.getsource(_confirm_on_tty) and "read_into" in inspect.getsource(_sign)
               and "read_into" not in inspect.getsource(set_budget),
               "BK3c the confirmation comes from the terminal; only _sign reads the key")
        case("BK3", bk3)

        def bk4():
            r = signed("k4")
            p = os.path.join(r, "budget_f6.json")
            doc = json.load(open(p))
            for label, mut in (("a raised usd", lambda x: x.__setitem__("usd", 1000)),
                               ("a dropped prefix", lambda x: x["prefixes"].pop()),
                               ("an added field", lambda x: x.__setitem__("x", 1))):
                bad = json.loads(json.dumps(doc))
                mut(bad["record"])
                json.dump(bad, open(p, "w"))
                ok(refused(lambda: load_budget("f6", env(r))) is not None, f"BK4 a changed record is refused: {label}")
            json.dump(doc, open(p, "w"))
            other = os.path.join(d, "other.pub")
            init_key(env(root("k4o"), store=_FakeKeys(), pub=other))
            ok(refused(lambda: load_budget("f6", env(r, pub=other))) is not None,
               "BK4b a record signed by another key than the tracked public key's is refused")
            ok(refused(lambda: load_budget("other", env(r))) is not None
               and refused(lambda: load_budget("f6", env(r, pub=os.path.join(d, "none.pub")))) is not None
               and refused(lambda: load_budget("../f6", env(r))) is not None,
               "BK5 a missing budget file or public key, or a bad scope name, is refused")
            open(p, "w").write("{not json")
            ok(refused(lambda: load_budget("f6", env(r))) is not None, "BK5b an unparsable budget file is refused")
            json.dump(doc, open(p, "w"))
            clock[0] += 15 * 86400
            ok(refused(lambda: load_budget("f6", env(r))) is not None, "BK5c an expired budget is refused")
            clock[0] -= 15 * 86400
            ok(refused(lambda: set_budget("f6", 5, "", None, 61, env(r))) is not None
               and refused(lambda: set_budget("f6", 5, "", None, 0, env(r))) is not None,
               "BK5d set refuses --days outside 1-60")
        case("BK4-5", bk4)

        def bk6():
            r = signed("k6")
            p, seen = os.path.join(r, "budget_f6.json"), os.path.join(r, "budget_f6.seen")
            old = open(p).read()
            ok(refused(lambda: set_budget("f6", 20, "same second", None, 14, env(r))) is not None,
               "BK6 a second record issued in the same second is refused at signing")
            clock[0] += 5
            set_budget("f6", 20, "lower", None, 14, env(r))
            ok(load_budget("f6", env(r))["usd"] == 20, "BK6b the newer record verifies")
            clock[0] -= 5
            open(p, "w").write(old)
            ok(refused(lambda: load_budget("f6", env(r))) is not None,
               "BK6c an older, higher record restored after a newer one was seen is refused (budget_<scope>.seen)")
            json.dump({"issued_utc": json.loads(old)["record"]["issued_utc"], "nonce": "0" * 32}, open(seen, "w"))
            ok(refused(lambda: load_budget("f6", env(r))) is not None,
               "BK6d the same issued time with another nonce is refused")
        case("BK6", bk6)

        def bk7():
            import pty
            import select
            res = []
            for typed in (b"50\n", b"49\n"):
                m, sl = pty.openpty()
                try:
                    os.write(m, typed)
                    got = _confirm_on_tty({"scope": "f6", "usd": 50.0, "note": "x"}, tty=os.ttyname(sl))
                    out = b""
                    while select.select([m], [], [], 0.2)[0]:
                        out += os.read(m, 4096)
                finally:
                    os.close(m)
                    os.close(sl)
                res.append((got, b"Type the amount" in out))
            ok(res == [(True, True), (False, True)],
               f"BK7 on a real pseudo-terminal the prompt is written and the typed amount read; a different amount "
               f"is not a confirmation: {res}")
        case("BK7", bk7)

        def bg7():
            for fn in (launch_budget_ok, spend, job_meter):
                ps = inspect.signature(fn).parameters
                ok(not any(p.kind == p.VAR_KEYWORD for p in ps.values())
                   and not {"budget", "source", "now", "pub_file", "budget_dir", "env"} & set(ps),
                   f"BR7 {fn.__name__} takes no injection: {list(ps)}")
            ok(list(inspect.signature(launch_budget_ok).parameters)[:3] == ["scope", "cap_usd", "name"]
               and real_env.pub_file == PUB_FILE and real_env.budget_dir == BUDGET_DIR and real_env.store is None
               and real_env.source is None,
               "BR7b the production path verifies against the tracked public key and reads the real Coiled source")
        case("BR7", bg7)

        now = T0 + 10 * H
        items = [_item(1, "f6-refit-a", T0 + 60), _item(2, "lw-smoke-b", T0 + 120, "running", now),
                 _item(3, "f6-refit-old", T0 - 86400, updated=T0 - 86400 + 4 * H), _item(4, "ev-x", T0 + 60),
                 _item(5, "xf6-refit-", T0 + 60), _item(6, "lw-smoke-err", T0 + 300, "error", T0 + 400),
                 _item(7, "f6-refit-early", T0 - 2 * H, "running", now)]
        dets = {1: _det(1, "f6-refit-a", T0 + 60, 100, 4.0, insts=[(T0 + 60, T0 + 2 * H + 60, "vmA")]),
                2: _det(2, "lw-smoke-b", T0 + 120, 10, 1.0, state="running",
                        insts=[(now - 3 * H, None, "vmA"), (now - H, None, "vmB"), (T0 + 120, T0 + 1200, "vmA")]),
                3: _det(3, "f6-refit-old", T0 - 86400, 200, 12.0, insts=[(T0 - 86400, T0 - 86400 + 4 * H, "vmA")]),
                6: _det(6, "lw-smoke-err", T0 + 300, 0, 0, state="error", insts=[(now - 2 * H, None, "vmA")]),
                7: _det(7, "f6-refit-early", T0 - 2 * H, 500, 50.0, state="running", insts=[(T0 - 2 * H, None, "vmA")]),
                4: ValueError("not this scope"), 5: ValueError("not this scope")}
        # rate: cluster 1 $10.10 / 2 h = 5.05, cluster 3 $24.20 / 4 h = 6.05 -> median 5.55 (vmA's bound is 2.0,
        # vmB's 10.0). 1: max(10.10, 2 x 5.55) = 11.10; 2: 3 x 5.55 + 1 x 10 + 0.3 x 5.55 = 28.315 (billed 1.61);
        # 6: an errored cluster with a live VM, 2 x 5.55 = 11.10; 7: created before the start, 10 h from it x 5.55.
        expect = 10.1 + 28.315 + 11.1 + 55.5     # cluster 1 is fully stopped: what it billed
        try:
            r8 = signed("k8")
        except Exception as e:  # noqa: BLE001 -- before the change the setup fails; the cases below report it
            r8 = os.path.join(d, "k8")
            ok(False, f"BR8 setup: {type(e).__name__}: {e}")

        def with_env(e):
            global _ENV
            _ENV = e

        def bg8():
            with_env(env(r8, source=_FakeSource(items, dets)))
            s = spend("f6")
            by = {c["id"]: c for c in s["clusters"]}
            ok(sorted(by) == [1, 2, 6, 7] and abs(by[1]["billed_usd"] - 10.1) < 1e-9 and abs(by[1]["usd"] - 10.1) < 1e-9
               and abs(by[2]["usd"] - 28.315) < 1e-9 and abs(by[2]["billed_usd"] - 1.61) < 1e-9,
               f"BR8 a fully stopped cluster counts what it billed (credits x 0.061 + cloud USD); one with a live "
               f"instance counts max(billed, instance-hours x max(median, the VM type's bound)); other scopes and "
               f"look-alike names are left out: {by}")
            ok(by[6]["live_instances"] == 1 and abs(by[6]["usd"] - 11.1) < 1e-9,
               f"BR8b an errored cluster with a live instance still counts: {by[6]}")
            ok(abs(by[7]["usd"] - 55.5) < 1e-9 and by[7]["billed_usd"] == 0 and abs(s["used_usd"] - expect) < 1e-9,
               f"BR8c a cluster created before the start and still running counts from the start: {by[7]}, "
               f"used {s['used_usd']}")
            for label, cid, mut in (("no scheduler key", 2, lambda x: x.pop("scheduler")),
                                    ("a running process without instance data",
                                     2, lambda x: x["workers"][0].__setitem__("instance", None)),
                                    ("an unknown VM type", 2, lambda x: x["scheduler"]["instance"].__setitem__(
                                        "instance_type_id", "vmZ")),
                                    ("a negative cost", 1, lambda x: x.__setitem__("total_cost", -5)),
                                    ("an invalid creation time", 1, lambda x: x.__setitem__("created", "garbage")),
                                    ("no creation time", 1, lambda x: x.__setitem__("created", None)),
                                    ("an instance stopped before it started", 1, lambda x: x["scheduler"]["instance"][
                                        "current_state"].__setitem__("updated", _iso(T0))),
                                    ("a live instance created in the future", 2, lambda x: x["scheduler"]["instance"]
                                        .__setitem__("created", _iso(now + 3600))),
                                    ):
                bad = json.loads(json.dumps({k: v for k, v in dets.items() if not isinstance(v, Exception)}))
                mut(bad[str(cid)])
                bad = {int(k): v for k, v in bad.items()}
                bad[4] = bad[5] = ValueError("not this scope")
                with_env(env(r8, source=_FakeSource(items, bad)))
                ok(refused(lambda: spend("f6")) is not None
                   and not launch_budget_ok("f6", 1, "lw-smoke-z")[0], f"BR9 {label} refuses")
        case("BR8-9", bg8)

        def bg8f():
            for field in ("cloud_total_cost", "total_cost"):
                nc = json.loads(json.dumps({k: v for k, v in dets.items() if not isinstance(v, Exception)}))
                nc = {int(k): v for k, v in nc.items()}
                nc[2][field] = None
                nc[4] = nc[5] = ValueError("not this scope")
                with_env(env(r8, source=_FakeSource(items, nc)))
                by = {c["id"]: c for c in spend("f6")["clusters"]}
                ok(abs(by[2]["usd"] - 28.315) < 1e-9 and any("missing" in a for a in by[2]["assumptions"]),
                   f"BR8f a live cluster missing {field} counts the estimate at the VM bound, with a warning, "
                   f"instead of refusing: {by[2]}")
        case("BR8f", bg8f)

        def bg8d():
            it = items + [_item(8, "lw-smoke-olderr", T0 - 5 * H, "error", T0 - 4 * H)]
            de = {**dets, 8: _det(8, "lw-smoke-olderr", T0 - 5 * H, 0, 0, state="error",
                                  insts=[(T0 - 5 * H, None, "vmA")])}
            with_env(env(r8, source=_FakeSource(it, de)))
            by = {c["id"]: c for c in spend("f6")["clusters"]}
            ok(8 in by and abs(by[8]["usd"] - 55.5) < 1e-9,
               f"BR8d an errored cluster that predates the window but still has a live VM counts from the start: "
               f"{by.get(8)}")
        case("BR8d", bg8d)

        def bg8e():
            nc = json.loads(json.dumps({k: v for k, v in dets.items() if not isinstance(v, Exception)}))
            nc = {int(k): v for k, v in nc.items()}
            nc[1]["cloud_total_cost"] = None
            nc[4] = nc[5] = ValueError("not this scope")
            with_env(env(r8, source=_FakeSource(items, nc)))
            by = {c["id"]: c for c in spend("f6")["clusters"]}
            ok(abs(by[1]["usd"] - 2 * 6.05) < 1e-9,
               f"BR8e a fully stopped cluster with no cloud cost counts the conservative estimate, and its partial bill "
               f"is left out of the measured rate (2 h x cluster 3's $6.05): {by[1]['usd']}")
        case("BR8e", bg8e)

        def bg10():
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
            import types
            from unittest import mock
            core = types.SimpleNamespace(CloudV2=CloudV2)
            with mock.patch.dict(sys.modules, {"coiled": types.SimpleNamespace(list_clusters=lc),
                                               "coiled.v2": types.SimpleNamespace(core=core), "coiled.v2.core": core}):
                CoiledSource().list_clusters()
                CoiledSource().details(7)
            ok(seen == {"list": WORKSPACE, "pages": None, "details": WORKSPACE} and WORKSPACE == "idm-coiled-idmad-r2",
               f"BR10 every Coiled read names the IDM workspace (all listing pages): {seen}")
        case("BR10", bg10)

        def bg11():
            r = signed("k11", usd=expect + 50)
            with_env(env(r, source=_FakeSource(items, dets)))
            ok(launch_budget_ok("f6", 50, "lw-smoke-x1", reserve=False)[0]
               and not launch_budget_ok("f6", 50.01, "lw-smoke-x1", reserve=False)[0],
               "BR11 ok exactly when used + reserved + cap <= budget")
            bad = [launch_budget_ok("f6", c, "lw-smoke-x1")[0] for c in (0, -1, float("nan"), None, "x", True)]
            ok(not any(bad) and not launch_budget_ok("f6", 1, "ev-z")[0] and not launch_budget_ok("f6", 1, "")[0],
               f"BR11b a cap that is not a positive number, or a name outside the signed prefixes, is refused: {bad}")
            r2 = signed("k11b", start=clock[0] + 86400)
            with_env(env(r2, source=_FakeSource(items, dets)))
            ok(not launch_budget_ok("f6", 1, "lw-smoke-x1")[0], "BR11c a budget that starts in the future refuses")
            with_env(env(os.path.join(d, "none"), source=_FakeSource(items, dets)))
            ok(not launch_budget_ok("f6", 1, "lw-smoke-x1")[0], "BR11d no signed budget, no launch")
        case("BR11", bg11)

        def bg12():
            r = signed("k12", usd=100.0)
            src = _FakeSource([], {})
            with_env(env(r, source=src))
            t = clock[0]

            def run(cid, name, credits, cloud, live=True):
                src.items = [x for x in src.items if x["id"] != cid] + [
                    _item(cid, name, t + 10, "running" if live else "stopped", t + 20)]
                src.det[cid] = _det(cid, name, t + 10, credits, cloud, "running" if live else "stopped",
                                    insts=[(t + 10, None if live else t + 20, "vmA")])
            a = launch_budget_ok("f6", 80, "lw-smoke-a")
            run(20, "lw-smoke-a", None, None)
            b = launch_budget_ok("f6", 80, "lw-smoke-b", reserve=False)
            ok(a[0] and not b[0] and "reserved $80.00" in b[1],
               f"BR12 A's $80 cap, listed at $0, still holds $80, so B's $80 is refused under $100: {b[1]}")
            run(20, "lw-smoke-a", 0, 30.0)
            c1, c2 = (launch_budget_ok("f6", x, "lw-smoke-b", reserve=False)[0] for x in (20, 21))
            ok(c1 and not c2, "BR12b a listed cluster's reservation counts max(0, cap - its measured spend)")
            run(21, "lw-smoke-a-keeper", 0, 10.0)
            c1, c2 = (launch_budget_ok("f6", x, "lw-smoke-b", reserve=False)[0] for x in (20, 21))
            ok(c1 and not c2, "BR12c the launch's helpers (named after it) count toward its reservation")
            run(20, "lw-smoke-a", 0, 30.0, live=False)
            g = launch_budget_ok("f6", 50, "lw-smoke-b", reserve=False)
            run(21, "lw-smoke-a-keeper", 0, 10.0, live=False)
            c1, c2 = (launch_budget_ok("f6", x, "lw-smoke-b", reserve=False)[0] for x in (60, 61))
            ok(not g[0] and c1 and not c2,
               f"BR12d the reservation is held while any of its clusters runs and released once all are stopped: {g[1]}")
            launch_budget_ok("f6", 50, "lw-smoke-c")
            clock[0] += RESERVATION_S + 1
            e = launch_budget_ok("f6", 10, "lw-smoke-d", reserve=False)
            clock[0] -= RESERVATION_S + 1
            ok("reserved $0.00" in e[1], f"BR12e a reservation with no cluster lapses after 2 h: {e[1]}")
            led = _ENV.reservations
            good = open(led).read()
            for label, doc in (("a non-list root", {"reservations": json.loads(good)}),
                               ("an entry without its cap", [{"scope": "f6", "name": "lw-smoke-x", "invocation": "i",
                                                              "time": t}]),
                               ("a NaN time", [{"scope": "f6", "cap_usd": 80, "name": "lw-smoke-x", "invocation": "i",
                                                "time": float("nan")}]),
                               ("a time in the future", [{"scope": "f6", "cap_usd": 80, "name": "lw-smoke-x",
                                                          "invocation": "i", "time": t + 86400}]),
                               ("an infinite cap", [{"scope": "f6", "cap_usd": float("inf"), "name": "lw-smoke-x",
                                                     "invocation": "i", "time": t}])):
                json.dump(doc, open(led, "w"))
                v = launch_budget_ok("f6", 1, "lw-smoke-y", reserve=False)
                ok(not v[0] and "fail" in v[1] and refused(lambda: spend("f6")) is not None,
                   f"BR12f a malformed reservations ledger refuses ({label}): {v[1][:80]}")
            open(led, "w").write(good)
            r2 = signed("k12h", usd=100.0)
            src2 = _FakeSource([], {})
            with_env(env(r2, source=src2))
            launch_budget_ok("f6", 80, "lw-smoke-a")
            launch_budget_ok("f6", 20, "lw-smoke-a2")
            src2.items = [_item(30, "lw-smoke-a2", t + 10, "running", t + 20)]
            src2.det = {30: _det(30, "lw-smoke-a2", t + 10, 0, 20.0, "running", insts=[(t + 10, None, "vmA")])}
            h = launch_budget_ok("f6", 1, "lw-smoke-z", reserve=False)
            ok(not h[0] and "reserved $80.00" in h[1],
               f"BR12h a cluster counts only against the reservation of its exact name (lw-smoke-a2 is not lw-smoke-a's): "
               f"{h[1]}")
            r3 = signed("k12i", usd=100.0)
            src3 = _FakeSource([_item(31, "lw-smoke-r", t - 60, "error", t - 50)],
                               {31: _det(31, "lw-smoke-r", t - 60, 0, 0.0, "error", insts=[(t - 60, t - 50, "vmA")])})
            with_env(env(r3, source=src3))
            launch_budget_ok("f6", 80, "lw-smoke-r")
            i2 = launch_budget_ok("f6", 80, "lw-smoke-b", reserve=False)
            ok(not i2[0] and "reserved $80.00" in i2[1],
               f"BR12i a retry's reservation is not released by the earlier, errored attempt's cluster of the same "
               f"name: {i2[1]}")
            with_env(env(r, source=src))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["status"])
            ok(os.path.basename(os.path.dirname(led)) == "reservations" and code == 0 and "budget f6" in out.getvalue(),
               f"BR12g the ledger lives in reservations/, so a default `status` does not read it as a budget: {code}")
        case("BR12", bg12)

        def bg19():
            # 2026-11-11 18:20 (F6 P6): run, keeper and overflow all stopped, yet `status` showed $306 used against
            # $191.68 billed and $67 still reserved. The records of a stopped cluster can keep an instance in a
            # non-final state (an overflow VM refused for quota stays queued), which read as live: projected to now,
            # and holding the reservation. A cluster whose every process is stopped or errored is finished.
            r = signed("k19", usd=2000.0)
            t = clock[0]
            names = ("lw-smoke-gf13-p6", "lw-smoke-gf13-p6-keeper", "lw-smoke-gf13-p6-overflow")   # v2-nigeria-gf13-p6 in f6
            src = _FakeSource([], {})
            with_env(env(r, source=src))
            got = launch_budget_ok("f6", 1000, names[0])
            later = t + 4 * H
            clock[0] = later
            for cid, nm, cr, cl in ((2364928, names[0], 2000, 70.0), (2364962, names[1], 50, 1.5),
                                    (2364947, names[2], 400, 0.0)):
                src.items.append(_item(cid, nm, t + 60, "stopped", t + 3 * H))
                d_ = _det(cid, nm, t + 60, cr, cl, "stopped", insts=[(t + 60, t + 3 * H, "vmA")])
                if nm.endswith("-overflow"):       # a worker whose VM never left the queue
                    d_["workers"].append({"current_state": {"state": "stopped", "updated": _iso(t + 3 * H)},
                                          "instance": {"created": _iso(t + 120), "instance_type_id": "vmA",
                                                       "current_state": {"state": "queued", "updated": _iso(t + 120)}}})
                src.det[cid] = d_
            s19 = spend("f6")
            billed = sum(c["billed_usd"] for c in s19["clusters"])
            clock[0] = t
            ok(got[0] and s19["reserved_usd"] == 0 and abs(s19["used_usd"] - billed) < 1e-9
               and abs(billed - (2450 * 0.061 + 71.5)) < 1e-9 and not s19["reservations"],
               f"BR19 a launch whose run, keeper and overflow clusters are all stopped releases its reservation, and "
               f"they count what they billed: used {s19['used_usd']:.2f}, billed {billed:.2f}, "
               f"reserved {s19['reserved_usd']:.2f}")
        case("BR19", bg19)

        def bg20():
            # 2026-11-11 18:30 (F6 P6, live): reserved $209.83 while only the 18:21 relaunch (cap $120) was live. The
            # 13:03 and 15:35 launches of the same config name were stopped, but the relaunch's clusters, of the same
            # names and created after them, matched their reservations too.
            r = signed("k20", usd=2000.0)
            t = clock[0]
            names = ("lw-smoke-p6", "lw-smoke-p6-keeper", "lw-smoke-p6-overflow")
            src = _FakeSource([], {})
            with_env(env(r, source=src))
            cid, trios = [100], []

            def trio(at):
                ids = []
                for nm in names:
                    cid[0] += 1
                    ids.append(cid[0])
                    src.items.append(_item(cid[0], nm, at + 60, "running", at + 120))
                    src.det[cid[0]] = _det(cid[0], nm, at + 60, 10, 1.0, "running", insts=[(at + 60, None, "vmA")])
                trios.append((at, ids))

            def stop(k):
                at, ids = trios[k]
                for i_ in ids:
                    nm = src.det[i_]["name"]
                    src.items = [x for x in src.items if x["id"] != i_] + [_item(i_, nm, at + 60, "stopped", at + 4 * H)]
                    src.det[i_] = _det(i_, nm, at + 60, 10, 1.0, "stopped", insts=[(at + 60, at + 4 * H, "vmA")])
            for k, cap in enumerate((100, 100, 120)):     # each relaunch reserves while the one before still runs
                clock[0] = t + k * 3 * H
                launch_budget_ok("f6", cap, names[0])
                trio(clock[0])
            clock[0] = t + 7 * H + 1800
            stop(0), stop(1)
            s20 = spend("f6")
            clock[0] = t
            held = [(x["cap_usd"], round(x["time"] - t)) for x in s20["reservations"]]
            ok(held == [(120, 6 * H)] and s20["reserved_usd"] <= 120,
               f"BR20 three launches of one name, the first two stopped and the third live: only the third's "
               f"reservation is held (each reconciles only against clusters created before the next one): {held}")
            r2 = signed("k20b", usd=2000.0)
            src2 = _FakeSource([], {})
            with_env(env(r2, source=src2))
            fa, fb = os.path.join(r2, "a.json"), os.path.join(r2, "b.json")
            launch_budget_ok("f6", 100, "lw-smoke-q", invocation="inv-a", clusters_file=fa)
            launch_budget_ok("f6", 100, "lw-smoke-q", invocation="inv-b", clusters_file=fb)
            for i_, (f_, inv, live) in enumerate(((fa, "inv-a", False), (fb, "inv-b", True))):
                n_ = 60 + i_
                src2.items.append(_item(n_, "lw-smoke-q", t + 60, "running" if live else "stopped", t + 120))
                src2.det[n_] = _det(n_, "lw-smoke-q", t + 60, 10, 1.0, "running" if live else "stopped",
                                    insts=[(t + 60, None if live else t + 600, "vmA")])
                json.dump({"run": {"name": "lw-smoke-q", "id": n_}, "invocation": inv}, open(f_, "w"))
            clock[0] = t + 1800
            s20b = spend("f6")
            json.dump({"run": {"name": "lw-smoke-q", "id": 99}, "invocation": "inv-c"}, open(fa, "w"))
            s20c = spend("f6")
            clock[0] = t
            ok([x["invocation"] for x in s20b["reservations"]] == ["inv-b"],
               f"BR20b a reservation whose launch's clusters.json names its cluster ids reconciles only against those "
               f"ids (two concurrent launches of one name): {[x['invocation'] for x in s20b['reservations']]}")
            ok([x["invocation"] for x in s20c["reservations"]] == ["inv-a", "inv-b"],
               f"BR20c a clusters.json rewritten by another invocation falls back to the name-and-time rule, which "
               f"cannot tell two same-second reservations apart and holds both (safe): "
               f"{[x['invocation'] for x in s20c['reservations']]}")
        case("BR20", bg20)

        def bg21():
            # 2026-11-11 21:17 (canary, cluster 2366111 scaling 5 -> 48 workers): VMs still being provisioned report
            # instance_type_id None; three refused readings in a row stopped the run as spend-unreadable. Seven of
            # them were workers in `error` that Azure never created (quota).
            X = 40

            def cluster(sched, workers, extra=()):
                d_ = _det(X, "lw-smoke-scale", now - 3 * H, 0, 0.0, "running",
                          insts=[(now - 2 * H, None, sched)] + [(now - H, None, w) for w in workers])
                d_["workers"] += [json.loads(json.dumps(e)) for e in extra]
                return d_

            def usd(det_x, base=True):
                it = (items if base else []) + [_item(X, "lw-smoke-scale", now - 3 * H, "running", now)]
                de = ({k: v for k, v in dets.items()} if base else {})
                de[X] = det_x
                with_env(env(r8, source=_FakeSource(it, de)))
                s_ = spend("f6")
                return {c["id"]: c for c in s_["clusters"]}[X], "\n".join(status_lines(s_))

            pend = [None, None]
            got, txt = usd(cluster("vmA", ["vmA", "vmA"] + pend))
            want, _ = usd(cluster("vmA", ["vmA"] * 4))
            ok(abs(got["usd"] - want["usd"]) < 1e-9 and any("no VM type" in x for x in got["assumptions"])
               and "no VM type" in txt,
               f"BR21 untyped pending workers are priced at the bound of the VM type the cluster's other workers have "
               f"(not the table maximum), stated as an assumption: {got['usd']:.3f} == {want['usd']:.3f}; "
               f"{got['assumptions']}")
            got, _ = usd(cluster("vmA", [None] * 4))
            want, _ = usd(cluster("vmA", ["vmB"] * 4))
            ok(abs(got["usd"] - want["usd"]) < 1e-9,
               f"BR21c with no typed worker yet, untyped workers take the table maximum: {got['usd']:.3f} == "
               f"{want['usd']:.3f}")
            got, _ = usd(cluster(None, ["vmA"] * 2))
            want, _ = usd(cluster("vmA", ["vmA"] * 2))
            got2, _ = usd(cluster(None, ["vmA"] * 2), base=False)
            want2, _ = usd(cluster("vmB", ["vmA"] * 2), base=False)
            ok(abs(got["usd"] - want["usd"]) < 1e-9 and abs(got2["usd"] - want2["usd"]) < 1e-9,
               f"BR21d an untyped scheduler takes the highest scheduler bound among the scope's recent clusters "
               f"(vmA here), else the table maximum: {got['usd']:.3f} == {want['usd']:.3f}, "
               f"{got2['usd']:.3f} == {want2['usd']:.3f}")
            failed = {"current_state": {"state": "error", "updated": _iso(now - H)},
                      "instance": {"created": _iso(now - H), "instance_type_id": None,
                                   "current_state": {"state": "error", "updated": _iso(now - H),
                                                     "reason": "Failed to create VM -- (OperationNotAllowed) Operation "
                                                               "could not be completed as it results in exceeding "
                                                               "approved standardESv5Family Cores quota."}}}
            got, txt = usd(cluster("vmA", ["vmA", "vmA"], extra=[failed] * 7))
            want, _ = usd(cluster("vmA", ["vmA", "vmA"]))
            ok(abs(got["usd"] - want["usd"]) < 1e-9 and any("7 instance(s) failed to create, counted $0" in x
                                                              for x in got["assumptions"]) and "failed to create" in txt,
               f"BR21e workers in error that Azure never created (no type, quota) count $0 and are listed: "
               f"{got['usd']:.3f} == {want['usd']:.3f}; {got['assumptions']}")
            with_env(env(r8, source=_FakeSource(items + [_item(X, "lw-smoke-scale", now - 3 * H, "running", now)],
                                                {**dets, X: cluster("vmA", ["Azure_Z99"])})))
            ok(refused(lambda: spend("f6")) is not None and not launch_budget_ok("f6", 1, "lw-smoke-z")[0],
               "BR21b a named VM type that is not in the table still refuses")
        case("BR21", bg21)

        def bg13():
            with_env(env(r8, source=_FakeSource(items, dets)))
            m = job_meter("f6")
            ok(abs(m(2) - 28.315) < 1e-9 and abs(m(6) - 11.1) < 1e-9 and abs(m(1) - 10.1) < 1e-9,
               "BR13 the job meter reads each cluster by id at the scope's rates")
            txt = "\n".join(status_lines(spend("f6")))
            ok("remaining" in txt and "200.00" in txt and "f6-refit-a" in txt, f"BR14 status prints the table: {txt}")
            ok(scope_for_name("lw-smoke-gcc", sc) == "f6" and scope_for_name("ev-q", sc) == "other"
               and scope_for_name("dev-vm", sc) is None, "BR15 a cluster name resolves to the scope of its prefix")
            with_env(env(os.path.join(d, "none"), source=_FakeSource(items, dets)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["status", "--scope", "f6"])
            ok(rc != 0 and "refus" in out.getvalue().lower(), f"BR16 status with no budget exits non-zero: {out.getvalue()}")
            p = load_prices(PRICES_FILE)
            ok(p and all(v > 0 for v in p.values()) and "Standard_D8s_v5" in p,
               f"BR17 the tracked price table loads: {len(p)} VM types")
        case("BR13-17", bg13)

        def bg18():
            prices, repo = load_prices(PRICES_FILE), os.path.dirname(HERE)
            files = [os.path.join(HERE, f) for f in os.listdir(HERE) if f.endswith(".py")]
            files += [os.path.join(HERE, "configs", f) for f in os.listdir(os.path.join(HERE, "configs"))
                      if f.endswith(".py")]
            named = set()
            for f in files + [os.path.join(repo, "scripts", "compute.py")]:
                named |= set(re.findall(r"Standard_[A-Za-z0-9]+_v[0-9]+", open(f).read()))
            sized = {f"Standard_D{n}_v5" for n in (2, 4, 8, 16, 32, 48, 64, 96)}   # vm_job --cpu sizing
            missing = sorted((named | sized | set(OBSERVED_VM_TYPES)) - set(prices))
            ok(not missing and {"Standard_D8s_v5", "Standard_E8s_v5"} <= named,
               f"BR18 every VM type the configs, launchers and vm_job sizing can request, and every type seen on our "
               f"clusters, has a price bound ({len(named)} named in code): missing {missing}")
        case("BR18", bg18)

        def bg22():
            # BR22 (Codex on #232): re-attaching a cap replaces the cluster's reservation with the new cap's remaining
            # amount, counted on the cluster's lifetime cost
            t = clock[0]

            def world(label, usd, cap, cloud, created=None):
                r = signed(f"k22{label}", usd=usd)
                c0 = t + 10 if created is None else created
                src = _FakeSource([], {})
                with_env(env(r, source=src))
                ok1 = launch_budget_ok("f6", cap, "lw-smoke-w")[0]
                src.items = [_item(40, "lw-smoke-w", c0, "running", t + 20)]
                src.det = {40: _det(40, "lw-smoke-w", c0, 0, cloud, "running", insts=[(c0, None, "vmA")])}
                return r, src, ok1, os.path.join(r, "reattach_clusters.json")
            r, src, ok1, cf = world("a", 100.0, 20, 10.0)
            g = reattach_budget_ok("f6", 50, "lw-smoke-w", 40, cf)
            later = launch_budget_ok("f6", 80, "lw-smoke-z", reserve=False)
            fits = launch_budget_ok("f6", 50, "lw-smoke-z", reserve=False)
            ok(ok1 and g[0] and not later[0] and "reserved $40.00" in later[1] and fits[0],
               f"BR22a raising a re-attached cap from $20 to $50 ($10 spent) reserves $40, so a later $80 launch is "
               f"refused under $100: {g[1]} / {later[1]}")
            r, src, ok1, cf = world("b", 20.0, 20, 10.0)
            g = reattach_budget_ok("f6", 20, "lw-smoke-w", 40, cf)
            g2 = reattach_budget_ok("f6", 20, "lw-smoke-w", 40, cf)
            led = [x for x in json.load(open(_ENV.reservations)) if x["scope"] == "f6"]
            ok(g[0] and g2[0] and len(led) == 1 and abs(spend("f6")["reserved_usd"] - 10.0) < 1e-6,
               f"BR22b re-attaching the original $20 cap with $10 spent fits a $20 budget and leaves one reservation "
               f"of $10 for the cluster, however often it is re-attached: {g[1]} {led}")
            r, src, ok1, cf = world("c", 300.0, 20, 100.0, created=t - 50 * H)
            g = reattach_budget_ok("f6", 50, "lw-smoke-w", 40, cf)
            m = lifetime_meter("f6")
            ok(not g[0] and "already spent" in g[1] and m(40) >= 100.0 and job_meter("f6")(40) < 50,
               f"BR22c a cluster created before the budget's window is capped on its lifetime cost, not the window's: "
               f"{g[1]} lifetime={m(40):.2f}")
            r, src, ok1, cf = world("d", 100.0, 20, 10.0)
            src.det[40] = BudgetError("details unavailable")
            ok(refused(lambda: reattach_budget_ok("f6", 50, "lw-smoke-w", 40, cf)) is not None
               and refused(lambda: lifetime_meter("f6")(40)) is not None,
               "BR22d an unreadable cluster cost raises (the caller retries, then stops the cluster), never returns")
        case("BR22", bg22)
    _ENV = real_env
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
    sub.add_parser("init-key", help="once, by the owner: the key pair (private half into the login keychain)")
    st = sub.add_parser("set", help="by the owner: confirm the record on the terminal, then the keychain asks for the password")
    st.add_argument("--scope", required=True)
    st.add_argument("--usd", type=float, required=True)
    st.add_argument("--note", required=True)
    st.add_argument("--start", default=None, help="ISO UTC time spend counts from (default: now)")
    st.add_argument("--days", type=float, default=14, help=f"days until the budget expires (at most {MAX_DAYS})")
    for nm in ("show", "status"):
        sub.add_parser(nm).add_argument("--scope", default=None, help="default: every budget present")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "init-key":
            print(f"owner key created: private half in the login keychain, public half in {_ENV.pub_file} "
                  f"({init_key()[:16]}...). Commit that file.")
        elif a.cmd == "set":
            print(f"signed: {set_budget(a.scope, a.usd, a.note, a.start, a.days)}")
        else:
            names = os.listdir(_ENV.budget_dir) if os.path.isdir(_ENV.budget_dir) else []
            scopes = [a.scope] if a.scope else sorted(f[7:-5] for f in names
                                                      if f.startswith("budget_") and f.endswith(".json"))
            if not scopes:
                raise BudgetError(f"no budget in {_ENV.budget_dir}")
            for sc in scopes:
                print(json.dumps(load_budget(sc), indent=1) if a.cmd == "show" else "\n".join(status_lines(spend(sc))))
    except (BudgetError, OSError) as e:
        print(f"refused: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
