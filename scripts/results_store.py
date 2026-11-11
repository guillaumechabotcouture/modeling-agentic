#!/usr/bin/env python3
"""
Phase 26 — durable results: a cloud job uploads its own outputs to the
project's Azure Blob storage account as soon as it finishes, so results never
depend on a laptop staying awake to collect them (E8 lost every case when the
laptop slept and the clusters idled out).

Auth follows the IDM Coiled guidance: on workers, the shared managed identity
via DefaultAzureCredential; on the laptop, `az login`. The account name comes
from $COILED_STORAGE_ACCOUNT (set on Coiled workers) or --account; it is never
hard-coded. Non-sensitive data only (the account's condition of use).

    python3 scripts/results_store.py upload <file> --blob outputs/<path>
    python3 scripts/results_store.py download outputs/<path> <file>
    python3 scripts/results_store.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

DEFAULT_ACCOUNT_ENV = "COILED_STORAGE_ACCOUNT"


def configured_account() -> str | None:
    return os.environ.get("MA_STORAGE_ACCOUNT") or os.environ.get(DEFAULT_ACCOUNT_ENV)


def _service(account: str | None = None):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    # $COILED_STORAGE_ACCOUNT is not set on workers in every workspace; the
    # launcher then passes the account name through $MA_STORAGE_ACCOUNT.
    account = account or configured_account()
    if not account:
        raise RuntimeError(f"no storage account: set ${DEFAULT_ACCOUNT_ENV} / $MA_STORAGE_ACCOUNT or pass --account")
    return BlobServiceClient(f"https://{account}.blob.core.windows.net",
                             credential=DefaultAzureCredential(exclude_interactive_browser_credential=True))


_SENSITIVE_NAME = re.compile(r"(?i)^(\.env.*|.*\.(pem|key|p12|pfx)|.*credential.*|.*secret.*|id_rsa.*|\.netrc|\.npmrc|\.pypirc)$")


def is_sensitive_path(rel: str) -> bool:
    """Never stored: answer keys (`_truth/`) and credential-like files."""
    parts = [x for x in rel.replace("\\", "/").split("/") if x]
    return any(x.startswith("_truth") for x in parts) or bool(parts and _SENSITIVE_NAME.match(parts[-1]))


def archive_dir(root: str, arcname: str, secrets: list[str] | None = None) -> tuple[bytes, list[str]]:
    """tar.gz of `root` without sensitive paths, with every secret string
    (>= 12 chars) replaced by <redacted> in file contents. Returns the bytes
    and the relative paths left out."""
    import io
    import tarfile
    secrets = [x.encode() for x in (secrets or []) if x and len(x) >= 12]
    skipped, buf = [], io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for dp, dirs, files in os.walk(root):
            for fn in sorted(files):
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root)
                if is_sensitive_path(rel) or os.path.islink(full):
                    skipped.append(rel)
                    continue
                data = open(full, "rb").read()
                for sec in secrets:
                    data = data.replace(sec, b"<redacted>")
                ti = tarfile.TarInfo(os.path.join(arcname, rel))
                ti.size, ti.mtime = len(data), int(os.path.getmtime(full))
                t.addfile(ti, io.BytesIO(data))
    return buf.getvalue(), skipped


REDACT_MAX_MB = 64      # larger files are streamed, not read whole: kept only when they hold no secret


def _plain(rel: str) -> list[str]:
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") or "\0" in p for p in parts):
        raise OSError(f"not a plain relative name: {rel!r}")
    return parts


def _open_tree(base: str, rel: str) -> int:
    """A directory fd for base/rel, reached from `base` one plain component at a time with no link followed."""
    cur = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    if rel in ("", "."):
        return cur
    try:
        for p in _plain(rel):
            nxt = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
            os.close(cur)
            cur = nxt
    except BaseException:
        os.close(cur)
        raise
    return cur


def _open_nofollow(root: str, rel: str, root_fd: int | None = None) -> int:
    """A read fd for root/rel (or, with `root_fd`, rel under that directory fd) opened without following a link at
    any component, `root` itself included (a planted link raises OSError), and only for a regular file (else
    OSError). `rel` is split on "/" only and never translated: a component that is empty, "." or ".." (an absolute
    or climbing name) is refused (Codex round 2)."""
    import stat
    parts = _plain(rel)
    cur = os.dup(root_fd) if root_fd is not None else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for p in parts[:-1]:
            nxt = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
            os.close(cur)
            cur = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=cur)
    finally:
        os.close(cur)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"not a regular file: {rel}")
    return fd


def _copy_scan(fd: int, dst, secrets: list[bytes]) -> bool:
    """Copy the open file to `dst` in 1 MiB blocks; True if a secret is in what was copied (an overlap carries a
    secret across two blocks)."""
    keep, tail, found = max((len(x) for x in secrets), default=0), b"", False
    while True:
        block = os.read(fd, 2**20)
        if not block:
            return found
        dst.write(block)
        buf = tail + block
        found = found or any(x in buf for x in secrets)
        tail = buf[-keep:] if keep else b""


def write_archive(root: str, arcname: str, out_path: str, secrets: list[str] | None = None, extra=(),
                  _hook=None, base: str | None = None) -> list[str]:
    """archive_dir streamed to `out_path` (F6 run checkpoints): the same exclusions and redaction, with bounded
    memory. The run dir is reached from `base` (the trusted job work dir) one component at a time, no link followed
    (Codex round 3: a parent such as work/runs linked into _truth/ is refused); without `base`, `root` itself must
    not be a link. Files are listed and opened through that directory fd. Each file is opened once, no link followed
    at any component, regular files only; its bytes are copied to a temporary file, and what is scanned and redacted
    is that copy, which is what is archived, so a rewrite or a swap for a link after the listing can neither leak a
    secret nor reach _truth/. A copy up to REDACT_MAX_MB is redacted; a larger one is left out (listed) if it holds
    a secret. `extra`: (path, arcname) pairs (run.log). `_hook(rel)` runs between the listing and each open
    (self-test). Returns what was left out."""
    sec = [x.encode() for x in (secrets or []) if x and len(x) >= 12]
    skipped = []
    items = []
    if base is not None:
        root_fd = _open_tree(base, os.path.relpath(os.path.abspath(root), os.path.abspath(base)))
    else:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)   # a root that is a link: OSError
    try:
        for dp, dirs, files, _ in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False):
            dirs.sort()
            items += [(root_fd, os.path.normpath(os.path.join(dp, fn)), None) for fn in sorted(files)]
        items += [(os.path.dirname(os.path.abspath(p)), os.path.basename(p), a) for p, a in extra]
        _write_items(items, arcname, out_path, sec, skipped, _hook)
    finally:
        os.close(root_fd)
    return skipped


def _write_items(items, arcname, out_path, sec, skipped, _hook):
    import io
    import tarfile
    import tempfile
    with tarfile.open(out_path, mode="w:gz") as t:
        for where, rel, arc in items:
            listed = rel if arc is None else arc
            if arc is None and is_sensitive_path(rel):
                skipped.append(rel)
                continue
            if _hook:
                _hook(rel)
            try:
                fd = _open_nofollow(where, rel, root_fd=where) if isinstance(where, int) else _open_nofollow(where, rel)
            except OSError:            # gone, a link, or not a regular file
                skipped.append(listed)
                continue
            try:
                mtime = int(os.fstat(fd).st_mtime)
                with tempfile.TemporaryFile(dir=os.path.dirname(os.path.abspath(out_path))) as tmp:
                    found = _copy_scan(fd, tmp, sec)
                    size = tmp.tell()
                    tmp.seek(0)
                    ti = tarfile.TarInfo(os.path.join(arcname, rel) if arc is None else arc)
                    ti.mtime = mtime
                    if size <= REDACT_MAX_MB * 2**20:
                        data = tmp.read()
                        for x in sec:
                            data = data.replace(x, b"<redacted>")
                        ti.size = len(data)
                        t.addfile(ti, io.BytesIO(data))
                    elif found:
                        skipped.append(listed)
                    else:
                        ti.size = size
                        t.addfile(ti, tmp)
            finally:
                os.close(fd)
    return skipped


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(2**20), b""):
            h.update(block)
    return h.hexdigest()


def upload_file(path: str, blob: str, account: str | None = None, service=None, attempts: int = 3) -> dict:
    """upload_retry with bounded memory: the file is streamed up and the read-back verified chunk by chunk."""
    import time
    want, size = sha256_file(path), os.path.getsize(path)
    container, name = _split(blob)
    last = None
    for k in range(attempts):
        try:
            client = (service or _service(account)).get_blob_client(container, name)
            with open(path, "rb") as f:
                client.upload_blob(f, overwrite=True, length=size)
            h = hashlib.sha256()
            for block in client.download_blob().chunks():
                h.update(block)
            if h.hexdigest() != want:
                raise RuntimeError(f"upload of {blob} did not verify")
            return {"blob": blob, "bytes": size, "sha256": want}
        except Exception as e:  # noqa: BLE001 -- transient network/auth errors
            last = e
            time.sleep(2 ** k)
    raise RuntimeError(f"upload of {blob} failed after {attempts} attempts: {type(last).__name__}: {last}")


def delete(blob: str, account: str | None = None, service=None) -> None:
    """Remove one blob (an old checkpoint generation)."""
    container, name = _split(blob)
    (service or _service(account)).get_blob_client(container, name).delete_blob()


def upload_retry(path: str, blob: str, account: str | None = None, service=None, attempts: int = 3) -> dict:
    import time
    last = None
    for k in range(attempts):
        try:
            return upload(path, blob, account, service=service)
        except Exception as e:  # noqa: BLE001 — transient network/auth errors
            last = e
            time.sleep(2 ** k)
    raise RuntimeError(f"upload of {blob} failed after {attempts} attempts: {type(last).__name__}: {last}")


def _split(blob: str) -> tuple[str, str]:
    container, _, name = blob.partition("/")
    if not container or not name:
        raise ValueError(f"blob must be <container>/<path>, got {blob!r}")
    return container, name


def upload(path: str, blob: str, account: str | None = None, service=None) -> dict:
    """Upload a file and read it back (size + sha256) so success is verified,
    not assumed."""
    data = open(path, "rb").read()
    container, name = _split(blob)
    svc = service or _service(account)
    client = svc.get_blob_client(container, name)
    client.upload_blob(data, overwrite=True)
    back = client.download_blob().readall()
    ok = hashlib.sha256(back).hexdigest() == hashlib.sha256(data).hexdigest()
    if not ok:
        raise RuntimeError(f"upload of {blob} did not verify (sizes {len(data)} vs {len(back)})")
    return {"blob": blob, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def list_blobs(prefix: str, account: str | None = None, service=None) -> list[dict]:
    """Blobs under <container>/<path prefix>: [{name, last_modified, size}]."""
    container, _, name_prefix = prefix.partition("/")
    svc = service or _service(account)
    cc = svc.get_container_client(container)
    return [{"name": f"{container}/{b.name}", "last_modified": b.last_modified, "size": b.size}
            for b in cc.list_blobs(name_starts_with=name_prefix)]


def download(blob: str, path: str, account: str | None = None, service=None) -> dict:
    container, name = _split(blob)
    svc = service or _service(account)
    data = svc.get_blob_client(container, name).download_blob().readall()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return {"blob": blob, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _self_test() -> int:
    import tempfile
    fails = []

    class _Blob:
        def __init__(self, store, key):
            self.store, self.key = store, key

        def upload_blob(self, data, overwrite=False):
            self.store[self.key] = bytes(data)

        def download_blob(self):
            v = self.store[self.key]
            return type("D", (), {"readall": lambda _s: v})()

    class _Svc:
        def __init__(self):
            self.store = {}

        def get_blob_client(self, c, n):
            return _Blob(self.store, (c, n))

    svc = _Svc()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "r.tar.gz")
        open(p, "wb").write(b"result bytes")
        r = upload(p, "outputs/e8/case1/r.tar.gz", service=svc)
        if r["bytes"] != 12 or ("outputs", "e8/case1/r.tar.gz") not in svc.store:
            fails.append(f"S1 upload: {r}")
        q = os.path.join(d, "back", "r.tar.gz")
        download("outputs/e8/case1/r.tar.gz", q, service=svc)
        if open(q, "rb").read() != b"result bytes":
            fails.append("S2 round trip")

        class _Corrupt(_Svc):
            def get_blob_client(self, c, n):
                b = _Blob(self.store, (c, n))
                b.upload_blob = lambda data, overwrite=False: self.store.__setitem__((c, n), b"x")
                return b
        try:
            upload(p, "outputs/x/y", service=_Corrupt())
            fails.append("S3 a corrupted upload must raise")
        except RuntimeError:
            pass
        try:
            _split("outputs")
            fails.append("S4 a blob without a path must raise")
        except ValueError:
            pass
        saved = {k: os.environ.pop(k, None) for k in (DEFAULT_ACCOUNT_ENV, "MA_STORAGE_ACCOUNT")}
        try:
            _service(None)
            fails.append("S5 no account must raise, not guess")
        except (RuntimeError, ImportError):
            pass
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        ar = os.path.join(d, "run")
        os.makedirs(os.path.join(ar, "_truth")); os.makedirs(os.path.join(ar, "models"))
        open(os.path.join(ar, "report.md"), "w").write("key sk-ant-SECRETSECRET123 in text\n")
        open(os.path.join(ar, "_truth", "answers.yaml"), "w").write("a\n")
        open(os.path.join(ar, ".env.local"), "w").write("X=1\n")
        open(os.path.join(ar, "models", "tls.key"), "w").write("k\n")
        blob, skipped = archive_dir(ar, "run", ["sk-ant-SECRETSECRET123"])
        import io as _io, tarfile as _tf
        tf = _tf.open(fileobj=_io.BytesIO(blob), mode="r:gz")
        names = tf.getnames()
        body = tf.extractfile("run/report.md").read()
        if names != ["run/report.md"] or b"SECRETSECRET" in body or b"<redacted>" not in body:
            fails.append(f"S6 archive drops _truth/.env*/keys and redacts secrets: {names} {body!r}")
        if sorted(skipped) != sorted(["_truth/answers.yaml", ".env.local", "models/tls.key"]):
            fails.append(f"S6 skipped list: {skipped}")
        calls = {"n": 0}

        class _Flaky(_Svc):
            def get_blob_client(self, c, n):
                b = _Blob(self.store, (c, n))
                real = b.upload_blob
                def up(data, overwrite=False):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise OSError("transient")
                    real(data, overwrite)
                b.upload_blob = up
                return b
        import time as _t
        _sleep = _t.sleep; _t.sleep = lambda *_: None
        try:
            upload_retry(p, "outputs/r/x", service=_Flaky())
        except Exception as e:  # noqa: BLE001
            fails.append(f"S7 a transient failure must be retried: {e}")
        finally:
            _t.sleep = _sleep

        class _Stream(_Svc):        # the Azure call shapes upload_file uses: a stream with its length, chunks back
            def get_blob_client(self, c, n):
                st = self.store
                return type("B", (), {
                    "upload_blob": lambda _s, f, overwrite=False, length=None: st.__setitem__((c, n), f.read(length)),
                    "download_blob": lambda _s: type("D", (), {"chunks": lambda _d: iter([st[(c, n)][:5],
                                                                                         st[(c, n)][5:]])})()})()
        sv = _Stream()
        r = upload_file(p, "outputs/ck/r.tar.gz", service=sv)
        if sv.store.get(("outputs", "ck/r.tar.gz")) != b"result bytes" or r["sha256"] != sha256_file(p):
            fails.append(f"S8 upload_file streams the file and verifies the read-back by chunks: {r}")
        big = os.path.join(ar, "models", "big.bin")
        open(big, "wb").write(b"x" * 100 + b"sk-ant-SECRETSECRET123")
        _max = globals()["REDACT_MAX_MB"]
        globals()["REDACT_MAX_MB"] = 0       # every file counts as large: streamed, and left out if it holds a secret
        try:
            sk = write_archive(ar, "run", os.path.join(d, "w.tar.gz"), ["sk-ant-SECRETSECRET123"])
        finally:
            globals()["REDACT_MAX_MB"] = _max
        if "models/big.bin" not in sk or "report.md" not in sk or "_truth/answers.yaml" not in sk:
            fails.append(f"S9 write_archive leaves out large files holding a secret, and _truth/: {sk}")
        # Codex round 1 P1-2: each file is opened once, without following links, and what is scanned is what is
        # archived. The hook runs between the listing and the open, as a concurrent writer would.
        os.remove(big)
        swap = os.path.join(ar, "models", "swap.txt")
        open(swap, "w").write("plain\n")
        open(os.path.join(ar, "models", "late.txt"), "w").write("plain\n")

        def race(rel):
            if rel == "models/swap.txt":
                os.remove(swap)
                os.symlink(os.path.join(ar, "_truth", "answers.yaml"), swap)
            if rel == "models/late.txt":
                open(os.path.join(ar, "models", "late.txt"), "w").write("now sk-ant-SECRETSECRET123\n")
        out = os.path.join(d, "r.tar.gz")
        sk = write_archive(ar, "run", out, ["sk-ant-SECRETSECRET123"], _hook=race)
        tf = _tf.open(out, mode="r:gz")
        late = tf.extractfile("run/models/late.txt").read()
        if "run/models/swap.txt" in tf.getnames() or "models/swap.txt" not in sk:
            fails.append(f"S10 a file swapped for a link to _truth/ after the listing is refused: {tf.getnames()} {sk}")
        if b"SECRETSECRET" in late or b"<redacted>" not in late:
            fails.append(f"S11 a file rewritten with a secret after the listing is archived redacted: {late!r}")
        os.remove(swap)
        globals()["REDACT_MAX_MB"] = 0
        try:
            sk = write_archive(ar, "run", out, ["sk-ant-SECRETSECRET123"], _hook=race)
        finally:
            globals()["REDACT_MAX_MB"] = _max
        if "models/late.txt" not in sk:
            fails.append(f"S12 a large file rewritten with a secret after the listing is left out: {sk}")
        # Codex round 2 P1-1: a name is never translated or allowed to climb out; the root itself is no link
        esc = os.path.join(d, "esc")
        os.makedirs(os.path.join(esc, "work", "run"))
        open(os.path.join(esc, "private.json"), "w").write("OUTSIDE\n")
        open(os.path.join(esc, "work", "run", "..\\..\\private.json"), "w").write("inside\n")
        write_archive(os.path.join(esc, "work", "run"), "run", out)
        tf = _tf.open(out, mode="r:gz")
        got = tf.extractfile("run/..\\..\\private.json").read() if "run/..\\..\\private.json" in tf.getnames() else b""
        if got != b"inside\n":
            fails.append(f"S14 a name holding backslashes is that file, never a path out of the run: {got!r}")
        refused = []
        for rel in ("../private.json", "work/../../private.json", "/etc/hosts", "run//x"):
            try:
                os.close(_open_nofollow(os.path.join(esc, "work"), rel))
            except (OSError, ValueError):
                refused.append(rel)
        if len(refused) != 4:
            fails.append(f"S14b names with '..', empty or absolute components are refused: {refused}")
        link_root = os.path.join(d, "linked_run")
        os.symlink(os.path.join(ar, "_truth"), link_root)
        try:
            sk = write_archive(link_root, "run", out)
            names = _tf.open(out, mode="r:gz").getnames()
            fails.append(f"S15 a run dir that is a link (into _truth/) is refused: archived {names} {sk}")
        except OSError:
            pass
        # Codex round 3 P1-1: a link in a parent of the run dir (work/runs -> _truth/) is refused from the trusted base
        work = os.path.join(d, "work3")
        os.makedirs(os.path.join(ar, "_truth", "R"))
        open(os.path.join(ar, "_truth", "R", "answers.yaml"), "w").write("secret answer\n")
        os.makedirs(work)
        os.symlink(os.path.join(ar, "_truth"), os.path.join(work, "runs"))
        try:
            write_archive(os.path.join(work, "runs", "R"), "runs/R", out, base=work)
            fails.append(f"S16 a parent link (work/runs -> _truth/) is refused: archived "
                         f"{_tf.open(out, mode='r:gz').getnames()}")
        except OSError:
            pass
        os.remove(os.path.join(work, "runs"))
        os.makedirs(os.path.join(work, "runs", "R"))
        open(os.path.join(work, "runs", "R", "ok.txt"), "w").write("ok\n")
        write_archive(os.path.join(work, "runs", "R"), "runs/R", out, base=work)
        if _tf.open(out, mode="r:gz").getnames() != ["runs/R/ok.txt"]:
            fails.append("S16b real directories under the trusted base are archived")
        # Codex round 4 P2: a blob's sha256 by streaming its chunks; it is never read whole or written to disk
        block = b"z" * 2**20

        class _Big:
            def get_blob_client(self, c, n):
                def readall(_s):
                    raise AssertionError("readall: the blob would be materialised")
                return type("B", (), {"download_blob": lambda _s: type("D", (), {
                    "chunks": lambda _d: (block for _ in range(200)), "readall": readall})()})()
        want = hashlib.sha256()
        for _ in range(200):
            want.update(block)
        try:
            got = sha256_blob("outputs/ck/big.tar.gz", service=_Big())
        except Exception as e:  # noqa: BLE001
            got = f"{type(e).__name__}: {e}"
        if got != want.hexdigest():
            fails.append(f"S17 sha256_blob streams a 200 MiB blob chunk by chunk: {got}")
        sv.store[("outputs", "ck/x")] = b"x"
        sv.get_blob_client = lambda c, n: type("B", (), {"delete_blob": lambda _s: sv.store.pop((c, n))})()
        delete("outputs/ck/x", service=sv)
        if ("outputs", "ck/x") in sv.store:
            fails.append("S13 delete removes a blob")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", nargs="?", choices=["upload", "download"])
    ap.add_argument("a", nargs="?")
    ap.add_argument("b", nargs="?")
    ap.add_argument("--blob")
    ap.add_argument("--account")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.cmd == "upload" and args.a and args.blob:
        print(upload(args.a, args.blob, args.account))
        return 0
    if args.cmd == "download" and args.a and args.b:
        print(download(args.a, args.b, args.account))
        return 0
    ap.error("usage: upload <file> --blob <container>/<path> | download <container>/<path> <file>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
