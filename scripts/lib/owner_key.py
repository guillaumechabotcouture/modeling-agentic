#!/usr/bin/env python3
"""The owner's Ed25519 key: one keychain item and one tracked public key for every owner signature.

cloud/budget.py signs the owner's cloud budget with it (PR #192). The two owner sign commands sign their sign-off
records with it (owner decision, 2026-11-23):

    python3 agents/stage_machine.py sign <run_dir> --by owner     # the decision spec (SIGN)
    python3 scripts/scope_signoff.py sign <run_dir> --by owner     # scope declarations (OWNER_DECISION)

and the harness verifies the signature where it consumes the record: leaving SIGN, leaving OWNER_DECISION, and on
--resume past SIGN. A lead's record (`--signoff lead`) is never signed and never counts as the owner's.

Key. The private key is a generic password in the macOS login keychain trusted by no application (`-T ""`), so every
read raises the keychain's password dialog (the owner clicks "Allow", never "Always Allow"). It is read only inside
`sign_bytes`, which zeroes the buffer; it is never returned, written to disk or logged. The trust anchor is the tracked
public key cloud/budget_owner.pub, the budget's: one key, one anchor to guard, one `init-key`. A sign-off payload
carries PURPOSE and a budget record has a fixed field set without it, so neither verifies as the other.

Payload (canonical JSON, sorted keys): purpose, kind ("decision_spec" | "scope_declarations"), run_id (the run
directory's name), artifacts ({file: sha256} approved), decision, signed_at. Verification fails closed: no public key,
a bad signature, another run, or an artifact hash that differs from the record or the file refuses.

Legacy. An owner record with no signature, made before LEGACY_CUTOFF_UTC, still counts, labelled "unsigned
(legacy)", so in-flight runs are not stranded; one made after it is refused. A self-test's fixture record that has no
time at all counts as legacy only while the test key is active.

Test key. A process started with --self-test, or one whose MA_OWNER_TEST_KEY holds a hex key (such a process exports
it for its children), signs and verifies with a throwaway key: no keychain, no tracked public key. main.py refuses a
run with MA_OWNER_TEST_KEY set, and agents get it blanked (agents/enforcement.py filtered_env).

    python3 scripts/lib/owner_key.py --self-test
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PUB_FILE = os.path.join(ROOT, "cloud", "budget_owner.pub")
KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "modeling-agentic-budget-owner", "owner"
PURPOSE = "modeling-agentic/owner-signoff/v1"
LEGACY_CUTOFF_UTC = "2026-11-25T00:00:00+00:00"   # the owner may move it (docs: changes/owner-sign.md)
TEST_ENV = "MA_OWNER_TEST_KEY"
SPEC, SHEET = "decision_spec.yaml", "decision_spec.signoff_sheet.md"
SCOPE_REQUEST = "scope_signoff_request.json"
HOW_SPEC = "the owner signs from their own shell: python3 agents/stage_machine.py sign <run_dir> --by owner"
HOW_SCOPE = "the owner signs from their own shell: python3 scripts/scope_signoff.py sign <run_dir> --by owner"


class OwnerKeyError(Exception):
    """A key, signature or record that cannot be used: the caller refuses."""


class Keychain:
    """The only access to the owner's private key: a generic password in the login keychain, trusted by no
    application (`-T ""`), so each read raises the keychain's password dialog."""

    def __init__(self, run=subprocess.run):
        self.run = run

    def _who(self):
        return ["-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT]

    def exists(self) -> bool:     # attributes only: reads no secret, raises no dialog
        try:
            return self.run(["security", "find-generic-password", *self._who()], capture_output=True).returncode == 0
        except OSError:
            return False

    def put(self, secret_hex: str) -> None:
        line = " ".join(["add-generic-password", *self._who(), "-l", "modeling-agentic-budget-owner-key",
                         "-T", '""', "-w", secret_hex]) + "\n"
        r = self.run(["security", "-i"], input=line, capture_output=True, text=True)
        err = (r.stderr or "").replace(secret_hex, "<key>").strip()
        if r.returncode != 0 or err:      # -i may exit 0 after a failed command: its error text counts
            raise OwnerKeyError(f"keychain refused the key (security exit {r.returncode}): {err[:200]}")

    def read_into(self) -> bytearray:
        """The key's hex in a bytearray the caller zeroes (the prompted read)."""
        try:
            r = self.run(["security", "find-generic-password", *self._who(), "-w"], capture_output=True)
        except OSError as e:            # no keychain on this machine (a cloud VM, CI)
            raise OwnerKeyError(f"no keychain here ({type(e).__name__}): the owner signs on their own Mac") from None
        if r.returncode != 0:
            raise OwnerKeyError(f"the keychain read was refused or failed (security exit {r.returncode})")
        return bytearray(r.stdout.strip())


class _HexKey:
    """The test key's store (a hex secret held in the process environment)."""

    def __init__(self, secret_hex: str):
        self.secret = secret_hex

    def exists(self) -> bool:
        return True

    def read_into(self) -> bytearray:
        return bytearray(self.secret.encode())


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def read_pub(pub_file: str = PUB_FILE) -> str:
    try:
        v = open(pub_file).read().strip()
    except OSError as e:
        raise OwnerKeyError(f"no owner public key at {pub_file} ({e}): the owner runs `python3 cloud/budget.py "
                            "init-key` once") from None
    if not re.fullmatch(r"[0-9a-f]{64}", v):
        raise OwnerKeyError(f"{pub_file} is not an Ed25519 public key in hex")
    return v


def _pub_of(secret_hex: str) -> str:
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret_hex))
    return k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()


def init_key(store, pub_file: str) -> str:
    """A new key pair: the private half into `store`, the public half (hex) to `pub_file`. Refuses to replace either."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if os.path.exists(pub_file) or store.exists():
        raise OwnerKeyError(f"a key exists already ({pub_file} or the keychain item {KEYCHAIN_SERVICE}): refusing")
    k = Ed25519PrivateKey.generate()
    store.put(k.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
    if not store.exists():
        raise OwnerKeyError("the keychain does not hold the new key: no public key written")
    pub = k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    with open(pub_file, "x") as f:
        f.write(pub + "\n")
    return pub


def sign_bytes(msg: bytes, store) -> str:
    """The one place the private key is read: sign `msg`, zero the buffer, return the signature (hex) only."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    buf = store.read_into()
    try:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(buf.decode())).sign(msg).hex()
    except ValueError:
        raise OwnerKeyError("the keychain item is not an Ed25519 key") from None
    finally:
        buf[:] = b"\0" * len(buf)


def verify_bytes(msg: bytes, sig_hex, pub_hex: str) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(bytes.fromhex(sig_hex), msg)
    except (InvalidSignature, TypeError, ValueError, AttributeError):
        raise OwnerKeyError("the signature does not verify against the owner's public key") from None


def _test_secret() -> str | None:
    s = os.environ.get(TEST_ENV) or None
    if s is None and "--self-test" in sys.argv:
        s = os.urandom(32).hex()
        os.environ[TEST_ENV] = s          # the self-test's child processes sign with the same throwaway key
    return s


def test_key_active() -> bool:
    return _test_secret() is not None


def _key():
    """(store, public key hex): the test key in a self-test, else the keychain and the tracked public key."""
    s = _test_secret()
    if s is not None:
        return _HexKey(s), _pub_of(s)
    return Keychain(), read_pub(PUB_FILE)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def run_id(run_dir: str) -> str:
    return os.path.basename(os.path.realpath(run_dir).rstrip("/"))


def sign_payload(kind: str, run_dir: str, artifacts: dict, decision, signed_at: str | None = None) -> dict:
    """The signed document {alg, payload, signature} for this approval. The public key is read first and the signature
    checked against it, so a keychain key that is not the tracked anchor's pair signs nothing."""
    p = {"purpose": PURPOSE, "kind": kind, "run_id": run_id(run_dir), "artifacts": dict(artifacts),
         "decision": decision, "signed_at": signed_at or _now()}
    store, pub = _key()
    msg = canon(p)
    sig = sign_bytes(msg, store)
    verify_bytes(msg, sig, pub)
    return {"alg": "ed25519", "payload": p, "signature": sig}


def verify(doc, kind: str, run_dir: str) -> dict:
    """The verified payload of a signed document for this kind and run, or OwnerKeyError."""
    if not isinstance(doc, dict) or doc.get("alg") != "ed25519" or not isinstance(doc.get("payload"), dict):
        raise OwnerKeyError("the owner signature is malformed")
    p = doc["payload"]
    verify_bytes(canon(p), doc.get("signature"), _key()[1])
    if p.get("purpose") != PURPOSE or p.get("kind") != kind:
        raise OwnerKeyError(f"the signature is not an owner sign-off of {kind}")
    if p.get("run_id") != run_id(run_dir):
        raise OwnerKeyError(f"the signature is for run {p.get('run_id')!r}, not this one")
    return p


def _legacy(at) -> bool:
    """An unsigned owner record that still counts: made before the cutoff (or, in a self-test, a fixture with no time)."""
    if at is None:
        return test_key_active()
    try:
        t = _dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return False
    t = t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
    return t < _dt.datetime.fromisoformat(LEGACY_CUTOFF_UTC)


def _file_sha(run_dir: str, rel: str) -> str | None:
    p = os.path.join(run_dir, rel)
    if os.path.islink(p):
        return None
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def sign_spec(run_dir: str, artifacts: dict) -> dict:
    """The owner's signature of a decision-spec sign-off: `artifacts` holds the spec's sha256, the sheet's and every
    other hash the record binds."""
    if not artifacts.get(SPEC):
        raise OwnerKeyError(f"no {SPEC} hash to sign")
    return sign_payload("decision_spec", run_dir, artifacts, "sign")


def spec_check(rec, run_dir: str) -> tuple[str, str]:
    """(status, why) of a spec sign-off record: not_owner | signed | legacy | refused. `signed` needs a valid signature
    for this run whose spec hash is the file's now and whose every other hash (the sheet aside, which the harness
    regenerates) is the record's."""
    rec = rec if isinstance(rec, dict) else {}
    if (rec.get("signed_by") or rec.get("by")) != "owner":
        return "not_owner", ""
    sig = rec.get("owner_signature")
    if sig is None:
        if _legacy(rec.get("signed_at") or rec.get("at")):
            return "legacy", "unsigned (legacy)"
        return "refused", f"the owner's sign-off of {SPEC} carries no owner signature; {HOW_SPEC}"
    try:
        p = verify(sig, "decision_spec", run_dir)
    except OwnerKeyError as e:
        return "refused", f"the owner's sign-off of {SPEC} does not verify ({e}); {HOW_SPEC}"
    hashes = rec.get("sha256") if isinstance(rec.get("sha256"), dict) else {SPEC: rec.get("spec_sha256")}
    arts = p.get("artifacts") if isinstance(p.get("artifacts"), dict) else {}
    bad = sorted(k for k, v in arts.items() if k != SHEET and hashes.get(k) != v)
    if not arts.get(SPEC) or arts.get(SPEC) != _file_sha(run_dir, SPEC):
        bad = sorted(set(bad) | {SPEC})
    if bad:
        return "refused", f"the owner's signature does not match {', '.join(bad)} (artifact hash mismatch); {HOW_SPEC}"
    return "signed", ""


def sign_scope(run_dir: str, answers: dict, round_n, signed_at: str) -> dict:
    """The owner's signature of scope-declaration answers {fingerprint: {finding, decision}} to the pending request.
    Each fingerprint is the hash of its finding, declarations and decision-test files (scope_signoff.build_item)."""
    req = _file_sha(run_dir, SCOPE_REQUEST)
    if req is None:
        raise OwnerKeyError(f"no {SCOPE_REQUEST} to sign")
    return sign_payload("scope_declarations", run_dir, {SCOPE_REQUEST: req},
                        {"round": round_n, "answers": answers}, signed_at)


def scope_check(run_dir: str, fp: str, a) -> tuple[str, str]:
    """(status, why) of one scope answer: not_owner | signed | legacy | refused. The signature binds the fingerprint,
    the finding, the decision and the time."""
    a = a if isinstance(a, dict) else {}
    if a.get("by") != "owner":
        return "not_owner", ""
    sig = a.get("owner_signature")
    if sig is None:
        if _legacy(a.get("at")):
            return "legacy", "unsigned (legacy)"
        return "refused", f"the owner's answer to {a.get('finding')} carries no owner signature; {HOW_SCOPE}"
    try:
        p = verify(sig, "scope_declarations", run_dir)
    except OwnerKeyError as e:
        return "refused", f"the owner's answer to {a.get('finding')} does not verify ({e}); {HOW_SCOPE}"
    got = ((p.get("decision") or {}).get("answers") or {}).get(fp) if isinstance(p.get("decision"), dict) else None
    if got != {"finding": a.get("finding"), "decision": a.get("decision")} or p.get("signed_at") != a.get("at"):
        return "refused", f"the owner's signature does not match the answer to {a.get('finding')}; {HOW_SCOPE}"
    return "signed", ""


# ---- self-test -------------------------------------------------------------------------------

def _self_test() -> int:
    import tempfile
    import types
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    ok(test_key_active() and Keychain.__name__ == "Keychain" and not isinstance(_key()[0], Keychain),
       "OK1 a self-test signs with a throwaway key, never the keychain")
    with tempfile.TemporaryDirectory() as td:
        rd = os.path.join(td, "2026-11-23_run")
        os.makedirs(rd)
        open(os.path.join(rd, SPEC), "w").write("a: 1\n")
        sha = _file_sha(rd, SPEC)
        sig = sign_spec(rd, {SPEC: sha, SHEET: "none", "data/x.json": "ab"})
        rec = {"signed_by": "owner", "signed_at": _now(), "sha256": {SPEC: sha, "data/x.json": "ab"},
               "owner_signature": sig}
        ok(spec_check(rec, rd)[0] == "signed" and sig["payload"]["run_id"] == "2026-11-23_run"
           and sorted(sig["payload"]) == ["artifacts", "decision", "kind", "purpose", "run_id", "signed_at"],
           f"OK2 an owner spec sign-off verifies: run id, artifacts, decision and time are signed ({spec_check(rec, rd)})")
        tamper = json.loads(json.dumps(rec))
        tamper["owner_signature"]["payload"]["artifacts"]["data/x.json"] = "cd"
        other = os.path.join(td, "other_run")
        os.makedirs(other)
        open(os.path.join(other, SPEC), "w").write("a: 1\n")
        ok(spec_check(tamper, rd)[0] == "refused" and spec_check(rec, other)[0] == "refused",
           "OK3 a changed payload, or the signature replayed in another run, is refused")
        open(os.path.join(rd, SPEC), "w").write("a: 2\n")
        st, why = spec_check(rec, rd)
        ok(st == "refused" and "artifact hash mismatch" in why and "--by owner" in why,
           f"OK4 a spec changed after signing is refused, saying how to sign: {why[:120]}")
        open(os.path.join(rd, SPEC), "w").write("a: 1\n")
        bad_rec = dict(rec, sha256={SPEC: sha, "data/x.json": "ee"})
        ok(spec_check(bad_rec, rd)[0] == "refused", "OK4b a record hash that differs from the signed one is refused")
        late = {"signed_by": "owner", "signed_at": "2026-12-01T00:00:00+00:00", "sha256": {SPEC: sha}}
        early = dict(late, signed_at="2026-11-20T10:00:00+00:00")
        ok(spec_check(late, rd)[0] == "refused" and spec_check(early, rd) == ("legacy", "unsigned (legacy)")
           and spec_check(dict(early, signed_at="garbage"), rd)[0] == "refused",
           "OK5 an unsigned owner record counts only before the cutoff, labelled unsigned (legacy)")
        ok(spec_check({"signed_by": "lead", "sha256": {SPEC: sha}}, rd)[0] == "not_owner"
           and spec_check(dict(rec, signed_by="lead"), rd)[0] == "not_owner",
           "OK6 a lead record is never the owner's, signature or not")
        open(os.path.join(rd, SCOPE_REQUEST), "w").write("{}")
        now = _now()
        ssig = sign_scope(rd, {"fp1": {"finding": "F1", "decision": "accept"}}, 2, now)
        a = {"finding": "F1", "decision": "accept", "by": "owner", "at": now, "owner_signature": ssig}
        ok(scope_check(rd, "fp1", a)[0] == "signed" and scope_check(rd, "fp2", a)[0] == "refused"
           and scope_check(rd, "fp1", dict(a, decision="decline"))[0] == "refused"
           and scope_check(rd, "fp1", dict(a, at="2026-12-01T00:00:00+00:00"))[0] == "refused",
           "OK7 a scope answer verifies only for its fingerprint, finding, decision and time")
        ok(scope_check(rd, "fp1", {"finding": "F1", "decision": "accept", "by": "owner", "at": "2026-12-02T00:00:00Z"}
                       )[0] == "refused" and scope_check(rd, "fp1", dict(a, by="lead"))[0] == "not_owner",
           "OK7b an unsigned owner answer after the cutoff is refused; a lead's is never the owner's")
        ok(spec_check(dict(rec, owner_signature=ssig), rd)[0] == "refused",
           "OK8 a scope signature never passes as a spec sign-off (kind is signed)")
        budget_like = {"alg": "ed25519", "payload": {"scope": "f6", "usd": 1.0}, "signature": sig["signature"]}
        ok(spec_check(dict(rec, owner_signature=budget_like), rd)[0] == "refused",
           "OK8b a document without the sign-off purpose is refused")
        saved = os.environ.pop(TEST_ENV)
        argv, sys.argv = sys.argv, ["x"]
        try:
            import unittest.mock as um
            with um.patch(__name__ + ".PUB_FILE", os.path.join(td, "none.pub")):
                st, why = spec_check(rec, rd)
        finally:
            sys.argv, os.environ[TEST_ENV] = argv, saved
        ok(st == "refused" and "public key" in why, f"OK9 no public key: refused (fail closed): {why[:100]}")
    k = Keychain(run=lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError("security")))
    try:
        k.read_into()
        got = None
    except OwnerKeyError as e:
        got = str(e)
    ok(got is not None and not k.exists(), f"OK10 no keychain (a cloud VM, CI): the read refuses, never crashes ({got})")
    calls = []
    k2 = Keychain(run=lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(returncode=0, stdout=b"ab\n"))
    ok(bytes(k2.read_into()) == b"ab" and calls[0][:2] == ["security", "find-generic-password"]
       and calls[0][calls[0].index("-s") + 1] == KEYCHAIN_SERVICE,
       "OK11 the keychain item is the budget's (one key, one tracked public key)")
    if fails:
        print(f"FAIL: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
