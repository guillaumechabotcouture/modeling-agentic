#!/usr/bin/env python3
"""The eval suite's hidden half, encrypted in the project storage account (Phase 50 α).

The repository is public, so it holds only each question's text (`evals/<q>/question.txt`)
and its synthetic inputs (`data/evals/<q>/`). Everything that reveals an answer (the
generators, rubrics, answer keys, question-specific tests and the design note) is one
tarball, encrypted with a symmetric key (Fernet) and stored as ciphertext in the IDM Coiled
storage account. Run VMs reach that account through their managed identity and agents
have a shell, so a plaintext copy there would be readable from a run; the key exists only
in the maintainer's macOS login keychain.

Hidden tree (the bundle):
    _common.py, test_suite.py, design/eval-suite.md
    <q>/generate.py, <q>/rubric.yaml, <q>/_truth/truth.json
    legacy/<name>/{rubric.yaml, _truth/truth.json, ...}   older answer keys kept so a
                                                           completed run stays gradable
Storage (results_store: account from $COILED_STORAGE_ACCOUNT, DefaultAzureCredential):
    outputs/evals_private/v<N>/bundle.enc       the ciphertext
    outputs/evals_private/v<N>/manifest.json    sha256 of the ciphertext and each
                                                question's input hashes (not secret)
Pin: evals/private.yaml in the repository names the version and the ciphertext sha256,
so a replaced bundle is refused.
Key: keychain service `modeling-agentic-evals`, account `evals`.

Every failure (no pin, no storage account, missing blob, hash mismatch, wrong key,
unsafe archive member) raises BundleError with a code, so a caller fails closed.

    python3 scripts/eval_private.py --self-test
(the maintainer commands are `scripts/eval_suite.py fetch | generate | publish`)
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PIN_PATH = os.path.join(REPO, "evals", "private.yaml")
BLOB_ROOT = "outputs/evals_private"
KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "modeling-agentic-evals", "evals"
_SKIP = ("__pycache__", ".DS_Store")


class BundleError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


# ---- storage ----------------------------------------------------------------------------

class LocalStore:
    """A directory standing in for the blob container (tests)."""

    def __init__(self, root):
        self.root = root

    def upload(self, path, blob):
        dest = os.path.join(self.root, blob)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(path, dest)

    def download(self, blob, path):
        src = os.path.join(self.root, blob)
        if not os.path.isfile(src):
            raise FileNotFoundError(blob)
        shutil.copyfile(src, path)


class AzureStore:
    """The project storage account through scripts/results_store.py (verified uploads)."""

    def __init__(self):
        sys.path.insert(0, HERE)
        import results_store
        if not results_store.configured_account():
            raise BundleError("storage_unconfigured",
                              f"set ${results_store.DEFAULT_ACCOUNT_ENV} to the IDM Coiled storage account and `az login`")
        self.rs = results_store

    def upload(self, path, blob):
        self.rs.upload_retry(path, blob)

    def download(self, blob, path):
        try:
            self.rs.download(blob, path)
        except Exception as e:  # noqa: BLE001 — azure raises ResourceNotFoundError and auth errors
            if type(e).__name__ == "ResourceNotFoundError":
                raise FileNotFoundError(blob) from e
            raise BundleError("storage_error", f"{blob}: {type(e).__name__}: {e}") from e


def default_store():
    return AzureStore()


# ---- key ----------------------------------------------------------------------------------

def new_key() -> bytes:
    from cryptography.fernet import Fernet
    return Fernet.generate_key()


def keychain_key() -> bytes:
    """The key from the macOS login keychain; never printed or written to disk."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
                           capture_output=True, text=True)
    except OSError as e:
        raise BundleError("key_unavailable", f"no macOS keychain here ({type(e).__name__})") from e
    if r.returncode != 0 or not r.stdout.strip():
        raise BundleError("key_unavailable", f"no keychain item {KEYCHAIN_SERVICE!r}/{KEYCHAIN_ACCOUNT!r}")
    return r.stdout.strip().encode()


def keychain_store(key: bytes) -> None:
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
                        "-w", key.decode()], capture_output=True, text=True)
    if r.returncode != 0:
        raise BundleError("key_unavailable", f"could not store the key in the keychain (exit {r.returncode})")


def encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(key).encrypt(data)


def decrypt(token: bytes, key: bytes) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(key).decrypt(token)
    except (InvalidToken, ValueError, TypeError) as e:
        raise BundleError("bundle_key_invalid", "the key does not decrypt the bundle") from e


# ---- bundle -------------------------------------------------------------------------------

def pack(src: str) -> bytes:
    """Deterministic tar.gz of the hidden tree (sorted, no times or owners, no caches)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as t:
        for dp, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP)
            for fn in sorted(files):
                full = os.path.join(dp, fn)
                if fn in _SKIP or fn.endswith(".pyc") or os.path.islink(full):
                    continue
                with open(full, "rb") as f:
                    data = f.read()
                ti = tarfile.TarInfo(os.path.relpath(full, src).replace(os.sep, "/"))
                ti.size, ti.mode = len(data), 0o644
                t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def unpack(blob: bytes, dest: str) -> None:
    """Extract regular files only, each inside dest (no links, devices or `..`)."""
    os.makedirs(dest, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
            members = t.getmembers()
            for m in members:
                parts = m.name.split("/")
                if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in parts:
                    raise BundleError("bundle_unsafe", f"refusing archive member {m.name!r}")
            t.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BundleError("bundle_unreadable", f"{type(e).__name__}: {e}") from e


def _load_json(p):
    try:
        with open(p) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def truths(root: str) -> tuple[dict, dict]:
    """({q: inputs_sha256}, {legacy name: inputs_sha256}) of the answer keys in a tree."""
    cur, old = {}, {}
    for name in sorted(os.listdir(root)):
        t = _load_json(os.path.join(root, name, "_truth", "truth.json"))
        if t is not None:
            cur[name] = t.get("inputs_sha256")
    leg = os.path.join(root, "legacy")
    for name in sorted(os.listdir(leg)) if os.path.isdir(leg) else []:
        t = _load_json(os.path.join(leg, name, "_truth", "truth.json"))
        if t is not None:
            old[name] = t.get("inputs_sha256")
    return cur, old


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def publish(src, version, key, store) -> dict:
    """Pack, encrypt and upload the hidden tree as version `version`; return the pin."""
    cipher = encrypt(pack(src), key)
    cur, old = truths(src)
    prefix = f"{BLOB_ROOT}/v{int(version)}"
    man = {"schema": 1, "version": int(version), "bundle_sha256": _sha(cipher), "bundle_bytes": len(cipher),
           "questions": cur, "legacy": old}
    with tempfile.TemporaryDirectory() as td:
        bp, mp = os.path.join(td, "bundle.enc"), os.path.join(td, "manifest.json")
        with open(bp, "wb") as f:
            f.write(cipher)
        with open(mp, "w") as f:
            json.dump(man, f, indent=1, sort_keys=True)
        store.upload(bp, f"{prefix}/bundle.enc")
        store.upload(mp, f"{prefix}/manifest.json")
    return {"version": int(version), "prefix": prefix, "bundle_sha256": man["bundle_sha256"]}


def read_pin(path=PIN_PATH) -> dict | None:
    import yaml
    try:
        with open(path) as f:
            pin = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return pin if isinstance(pin, dict) else None


def write_pin(pin: dict, path=PIN_PATH) -> None:
    import yaml
    with open(path, "w") as f:
        f.write("# Phase 50 α: where the eval suite's encrypted hidden half lives (evals/README.md).\n"
                "# Written by `scripts/eval_suite.py publish`; nothing here is secret.\n")
        yaml.safe_dump({k: pin[k] for k in ("version", "prefix", "bundle_sha256")}, f, sort_keys=False)


def fetch(dest, key, store, pin) -> dict:
    """Download, verify and decrypt the pinned bundle into dest; return its manifest.
    `key` None reads the keychain, after the downloads verify."""
    if not isinstance(pin, dict) or not pin.get("prefix") or not pin.get("bundle_sha256"):
        raise BundleError("bundle_not_pinned", "no evals/private.yaml naming the bundle (run publish)")
    got = {}
    with tempfile.TemporaryDirectory() as td:
        for name in ("manifest.json", "bundle.enc"):
            p = os.path.join(td, name)
            try:
                store.download(f"{pin['prefix']}/{name}", p)
            except FileNotFoundError as e:
                raise BundleError("bundle_missing", f"{pin['prefix']}/{name} is not in storage") from e
            with open(p, "rb") as f:
                got[name] = f.read()
    try:
        man = json.loads(got["manifest.json"])
    except ValueError as e:
        raise BundleError("bundle_unreadable", "manifest.json is not JSON") from e
    actual = _sha(got["bundle.enc"])
    if not isinstance(man, dict) or not (actual == man.get("bundle_sha256") == pin["bundle_sha256"]):
        mh = str(man.get("bundle_sha256") if isinstance(man, dict) else None)
        raise BundleError("bundle_hash_mismatch",
                          f"ciphertext {actual[:12]}, manifest {mh[:12]}, pin {str(pin['bundle_sha256'])[:12]}")
    plain = decrypt(got["bundle.enc"], key if key is not None else keychain_key())
    try:
        unpack(plain, dest)
        if truths(dest) != (man.get("questions"), man.get("legacy")):
            raise BundleError("bundle_hash_mismatch", "the decrypted answer keys' input hashes differ from the manifest")
    except BundleError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return man


@contextlib.contextmanager
def opened(src: str | None = None, store=None, pin=None, key=None):
    """The hidden tree for the duration of a command: `src` (a maintainer's local
    plaintext copy) as is, else the pinned bundle decrypted into a temporary directory
    that is deleted afterwards."""
    if src:
        if not os.path.isdir(src):
            raise BundleError("bundle_missing", f"{src} is not a directory")
        yield os.path.abspath(src)
        return
    pin = pin if pin is not None else read_pin()
    if not isinstance(pin, dict):
        raise BundleError("bundle_not_pinned", "no evals/private.yaml naming the bundle (run publish)")
    store = store or default_store()
    td = tempfile.mkdtemp(prefix="eval_private_")
    try:
        fetch(td, key, store, pin)
        yield td
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        if not cond:
            fails.append(msg)

    def expect_error(code, fn, msg):
        try:
            fn()
        except BundleError as e:
            ok(e.code == code, f"{msg}: got {e.code} ({e.detail})")
            return
        except Exception as e:  # noqa: BLE001 — a crash is a failed case, not a finding
            ok(False, f"{msg}: crashed with {type(e).__name__}: {e}")
            return
        ok(False, f"{msg}: no finding (silent pass)")

    sys.path.insert(0, HERE)
    import eval_suite as es
    with tempfile.TemporaryDirectory() as td:
        src, store_root = os.path.join(td, "src"), os.path.join(td, "blob")
        rubric, truth = es._toy()
        _write_private(src, "toy", rubric, truth)
        legacy = dict(truth, seed=0, inputs_sha256={"a.csv": "y"})
        _write_private(os.path.join(src, "legacy", "toy_0"), None, rubric, legacy | {"question": "toy"})
        run = os.path.join(td, "run")
        es.build_fixture(run, rubric, truth, "perfect")
        before = es.grade_run("toy", run, private_dir=src, truth=None, launch={"eval_inputs_sha256": {
            "data/evals/toy/a.csv": "x"}})
        store, key = LocalStore(store_root), None
        try:
            key = new_key()
            pin = publish(src, 1, key, store)
            ok(pin.get("version") == 1 and len(pin.get("bundle_sha256", "")) == 64, f"E1 publish returns a pin: {pin}")
            names = sorted(os.path.relpath(os.path.join(dp, f), store_root)
                           for dp, _, fs in os.walk(store_root) for f in fs)
            ok(names == [f"{pin['prefix']}/bundle.enc", f"{pin['prefix']}/manifest.json"],
               f"E1b only the ciphertext and the manifest are stored: {names}")
            raw = b"".join(open(os.path.join(store_root, n), "rb").read() for n in names)
            ok(b"value_of_optimum" not in raw and b"benefit" not in raw and b"alpha" not in raw,
               "E1c the stored bytes hold no plaintext of the truth or the rubric")
            dest = os.path.join(td, "dest")
            man = fetch(dest, key, store, pin)
            after = es.grade_run("toy", run, private_dir=dest, launch={"eval_inputs_sha256": {
                "data/evals/toy/a.csv": "x"}})
            ok(man.get("questions", {}).get("toy") == {"a.csv": "x"}, f"E1d manifest lists input hashes: {man}")
            ok(before["quality"] == after["quality"] == 1.0
               and [c["score"] for c in before["checks"]] == [c["score"] for c in after["checks"]],
               f"E1e pack, encrypt, upload, download, decrypt, grade: same scores {before['quality']} {after['quality']}")
            expect_error("bundle_key_invalid", lambda: fetch(os.path.join(td, "d2"), new_key(), store, pin),
                         "E2 a wrong key fails closed")
            expect_error("bundle_key_invalid", lambda: fetch(os.path.join(td, "d2b"), b"not a key", store, pin),
                         "E2b a malformed key fails closed")
            ok(not os.path.exists(os.path.join(td, "d2")) or not os.listdir(os.path.join(td, "d2")),
               "E2c a failed fetch leaves nothing decrypted behind")
            expect_error("bundle_missing", lambda: fetch(os.path.join(td, "d3"), key, LocalStore(os.path.join(td, "x")), pin),
                         "E3 a missing bundle fails closed")
            expect_error("bundle_hash_mismatch",
                         lambda: fetch(os.path.join(td, "d4"), key, store, dict(pin, bundle_sha256="0" * 64)),
                         "E3b a pin that does not match the manifest fails closed")
            enc = os.path.join(store_root, pin["prefix"], "bundle.enc")
            good = open(enc, "rb").read()
            open(enc, "wb").write(good[:-4] + b"AAAA")
            expect_error("bundle_hash_mismatch", lambda: fetch(os.path.join(td, "d5"), key, store, pin),
                         "E3c a ciphertext that does not hash to the manifest fails closed")
            open(enc, "wb").write(good)
            os.remove(os.path.join(store_root, pin["prefix"], "manifest.json"))
            expect_error("bundle_missing", lambda: fetch(os.path.join(td, "d6"), key, store, pin),
                         "E3d a missing manifest fails closed")
            expect_error("bundle_not_pinned", lambda: fetch(os.path.join(td, "d7"), key, store, None),
                         "E3e no pin in the repo fails closed")
            ok(pack(src) == pack(src), "E4 packing is deterministic")
            import tarfile as _tf
            evil = io.BytesIO()
            with _tf.open(fileobj=evil, mode="w:gz") as t:
                ti = _tf.TarInfo("../escape.txt")
                ti.size = 1
                t.addfile(ti, io.BytesIO(b"x"))
            expect_error("bundle_unsafe", lambda: unpack(evil.getvalue(), os.path.join(td, "d8")),
                         "E4b an archive member outside the destination is refused")
            ok(not os.path.exists(os.path.join(td, "escape.txt")), "E4c nothing was written outside it")
            saved = {k: os.environ.pop(k, None) for k in ("COILED_STORAGE_ACCOUNT", "MA_STORAGE_ACCOUNT")}
            try:
                expect_error("storage_unconfigured", default_store, "E5 no storage account configured fails closed")
            finally:
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v
            with opened(src) as root:
                ok(root == os.path.abspath(src), "E6 a local plaintext copy is used as is")
            pin = publish(src, 2, key, store)   # E3d removed v1's manifest
            with opened(None, store=store, pin=pin, key=key) as root:
                tmp = root
                ok(os.path.isfile(os.path.join(root, "toy", "_truth", "truth.json")), "E6b the bundle opens")
            ok(not os.path.exists(tmp), "E6c the decrypted temporary directory is deleted afterwards")
        except (NotImplementedError, AttributeError, TypeError) as e:
            ok(False, f"E1-E6 not implemented: {type(e).__name__}: {e}")
    if fails:
        for f in fails:
            print("FAIL:", f, file=sys.stderr)
        print(f"{len(fails)} self-test case(s) failed", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _write_private(root, q, rubric, truth):
    """A hidden tree for one question: <root>/<q>/rubric.yaml and _truth/truth.json
    (q None: the files sit directly in root, as in legacy/<name>/)."""
    import yaml
    d = os.path.join(root, q) if q else root
    os.makedirs(os.path.join(d, "_truth"), exist_ok=True)
    with open(os.path.join(d, "rubric.yaml"), "w") as f:
        yaml.safe_dump(rubric, f)
    with open(os.path.join(d, "_truth", "truth.json"), "w") as f:
        json.dump(truth, f)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
