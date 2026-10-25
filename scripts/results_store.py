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
import sys

DEFAULT_ACCOUNT_ENV = "COILED_STORAGE_ACCOUNT"


def _service(account: str | None = None):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    # $COILED_STORAGE_ACCOUNT is not set on workers in every workspace; the
    # launcher then passes the account name through $MA_STORAGE_ACCOUNT.
    account = account or os.environ.get(DEFAULT_ACCOUNT_ENV) or os.environ.get("MA_STORAGE_ACCOUNT")
    if not account:
        raise RuntimeError(f"no storage account: set ${DEFAULT_ACCOUNT_ENV} / $MA_STORAGE_ACCOUNT or pass --account")
    return BlobServiceClient(f"https://{account}.blob.core.windows.net",
                             credential=DefaultAzureCredential(exclude_interactive_browser_credential=True))


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
