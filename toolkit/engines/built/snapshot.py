#!/usr/bin/env python3
"""Content-addressed immutable snapshots and static checks S1-S4 for agent-built engines
(docs/design/agent-built-engines.md §4.1 & §5.1).

    python3 toolkit/engines/built/snapshot.py --self-test

Working copies stay writable under `models/ext/engine_<id>/`. Admission and production never read
the working copy: they read immutable snapshots under `engine_build/snapshots/<sha>/`, verified
once from no-follow file descriptors with link-count checks before any compile. Nothing here names
a disease.
"""
from __future__ import annotations

import ast
import codecs
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import safe_io as SIO  # noqa: E402

MAX_SNAPSHOT_BYTES = 300 * 1024
REQUIRED_FILES = ("engine.py", "card.yaml", "ENGINE_NOTES.md")
ALLOWED_NON_PY = frozenset({"card.yaml", "ENGINE_NOTES.md"})
_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
REQUIRED_BOUND_FILES = (
    "admission.json", "admission_sheet.md",
    "bench/pack.yaml", "bench/results.yaml", "bench/benchmark_coverage.yaml",
)
OPTIONAL_BOUND_FILES = ("bench/fitted.yaml",)

# S2: strict AST import allowlist (§3.2). Anything outside this set is refused before execution.
ALLOWED_TOP_IMPORTS = frozenset({
    "__future__", "starsim", "numpy", "math", "dataclasses", "typing", "functools", "itertools",
})
ALLOWED_QUALIFIED_IMPORTS = frozenset({
    "scipy.special", "scipy.stats", "toolkit.engines.contract", "toolkit.engines.hooks",
})
# §4.4 & §7 Q10: independent A11 reference may additionally use scipy.integrate (never allowed for engines).
REFERENCE_ALLOWED_QUALIFIED = ALLOWED_QUALIFIED_IMPORTS | frozenset({"scipy.integrate"})

# S3: banned names, dynamic reflection, and library file/serialization helpers (§3.2 & §4.1).
BANNED_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "breakpoint", "input",
    "os", "sys", "inspect", "subprocess", "pathlib", "importlib", "builtins", "ctypes", "shutil", "tempfile",
    "socket", "urllib", "http", "pickle", "marshal", "shelve", "sciris",
})
REFLECTION_CALLS = frozenset({"getattr", "setattr", "delattr"})
BANNED_DUNDER_DEFS = frozenset({
    "__getattr__", "__getattribute__", "__setattr__", "__delattr__", "__reduce__", "__reduce_ex__",
    "__getstate__", "__setstate__", "__import__",
})
BANNED_ATTRS = frozenset({
    "environ", "load", "save", "savez", "savez_compressed", "fromfile", "tofile", "memmap",
    "loadtxt", "savetxt", "genfromtxt", "fromregex", "DataSource",
    "loadobj", "saveobj", "loadjson", "savejson", "loadyaml", "saveyaml", "loadtext", "savetext",
    "savearchive", "loadarchive", "open", "read_text", "write_text", "read_bytes", "write_bytes",
    "to_file", "from_file", "to_csv", "from_csv", "to_json", "from_json", "to_yaml", "from_yaml",
    "to_pickle", "from_pickle", "to_parquet", "from_parquet", "to_excel", "from_excel",
    "to_hdf", "from_hdf", "to_feather", "from_feather", "to_sql", "from_sql",
    "to_disk", "from_disk", "to_archive", "from_archive", "to_path", "from_path",
    "to_buffer", "from_buffer",
})


class SnapshotError(ValueError):
    """A working copy or snapshot failed structural, link, or digest verification."""


class SnapshotTampered(SnapshotError):
    """A snapshot's files or directory membership no longer match its content-addressed digest."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_digest(file_hashes: Mapping[str, str]) -> str:
    """Canonical sha256 over the sorted [(name, sha256)] membership list."""
    pairs = [[str(k), str(v)] for k, v in sorted(file_hashes.items())]
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def _allowed_filename(name: str) -> bool:
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith("."):
        return False
    return name.endswith(".py") or name in ALLOWED_NON_PY


def _is_const_expr(node: ast.AST) -> bool:
    """True for safe module-level constant expressions (no calls, no dynamic lookups)."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_const_expr(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all((k is None or _is_const_expr(k)) and _is_const_expr(v)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not, ast.Invert)):
        return _is_const_expr(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)):
        return _is_const_expr(node.left) and _is_const_expr(node.right)
    if isinstance(node, ast.Name) and node.id not in BANNED_NAMES and not node.id.startswith("__"):
        return True
    return False


def _import_allowed(mod: str, siblings: set[str], *, allow_scipy_integrate: bool = False) -> bool:
    if not mod:
        return False
    if mod in siblings:
        return True
    top = mod.split(".", 1)[0]
    if top in ALLOWED_TOP_IMPORTS:
        return True
    qual = REFERENCE_ALLOWED_QUALIFIED if allow_scipy_integrate else ALLOWED_QUALIFIED_IMPORTS
    for q in qual:
        if mod == q or mod.startswith(q + "."):
            return True
    return False


def check_static(files: Mapping[str, bytes], *, request: Mapping | None = None,
                 allow_scipy_integrate: bool = False) -> list[str]:
    """Run static checks S1-S3 over `{filename: bytes}` (fail closed). Returns list of problems."""
    import yaml
    bad: list[str] = []
    # S1: file membership, strict UTF-8 text, no binary/NUL bytes, total size <= 300 KB
    total = 0
    decoded: dict[str, str] = {}
    for name, data in sorted(files.items()):
        if not _allowed_filename(name):
            bad.append(f"S1: disallowed file {name!r} (only .py, card.yaml, ENGINE_NOTES.md at top level)")
            continue
        if not isinstance(data, (bytes, bytearray)):
            bad.append(f"S1: {name} is not raw bytes")
            continue
        total += len(data)
        if b"\x00" in data:
            bad.append(f"S1: {name} contains binary/NUL bytes")
            continue
        try:
            decoded[name] = bytes(data).decode("utf-8")
        except UnicodeDecodeError as e:
            bad.append(f"S1: {name} is not valid UTF-8: {e}")
    for req_f in REQUIRED_FILES:
        if req_f not in files:
            bad.append(f"S1: missing required file {req_f}")
    if total > MAX_SNAPSHOT_BYTES:
        bad.append(f"S1: total size {total} bytes exceeds limit {MAX_SNAPSHOT_BYTES}")

    if "card.yaml" in decoded:
        try:
            card_doc = yaml.safe_load(decoded["card.yaml"])
        except yaml.YAMLError as e:
            bad.append(f"S1: card.yaml is not valid YAML: {e}")
        else:
            if not isinstance(card_doc, Mapping):
                bad.append("S1: card.yaml must be a YAML mapping")

    py_files = sorted(n for n in files if n.endswith(".py") and _allowed_filename(n))
    siblings = {n[:-3] for n in py_files}

    for name in py_files:
        if name not in decoded:
            continue
        src = decoded[name]
        # Reject PEP-263 non-UTF-8 coding cookies so static AST and compiled source can never diverge
        for line in src.splitlines()[:2]:
            m = _CODING_RE.match(line)
            if m:
                try:
                    norm = codecs.lookup(m.group(1)).name
                except LookupError:
                    norm = ""
                if norm != "utf-8":
                    bad.append(f"S1: {name}: non-UTF-8 coding declaration {m.group(1)!r} is refused")
        try:
            tree = ast.parse(src, filename=name)
        except SyntaxError as e:
            bad.append(f"S2: {name}:{e.lineno}: syntax error: {e.msg}")
            continue

        # S3 module-level check: only imports, docstring, constant assignments, functions, classes
        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            if isinstance(stmt, ast.Assign) and _is_const_expr(stmt.value):
                continue
            if isinstance(stmt, ast.AnnAssign) and (stmt.value is None or _is_const_expr(stmt.value)):
                continue
            bad.append(f"S3: {name}:{stmt.lineno}: disallowed module-level statement {type(stmt).__name__} "
                       "(only imports, constants, functions, and classes allowed)")

        for node in ast.walk(tree):
            # S2: imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _import_allowed(alias.name, siblings, allow_scipy_integrate=allow_scipy_integrate):
                        bad.append(f"S2: {name}:{node.lineno}: import {alias.name!r} is not in the allowlist")
            elif isinstance(node, ast.ImportFrom):
                if node.level > 1:
                    bad.append(f"S2: {name}:{node.lineno}: parent-relative import (level={node.level}) is refused")
                elif node.level == 1:
                    mod = node.module or ""
                    targets = [mod] if mod else [a.name for a in node.names]
                    for t in targets:
                        if t not in siblings:
                            bad.append(f"S2: {name}:{node.lineno}: relative import {t!r} is not a sibling module")
                else:
                    mod = node.module or ""
                    if mod == "scipy":
                        for alias in node.names:
                            full = f"scipy.{alias.name}"
                            if not _import_allowed(full, siblings, allow_scipy_integrate=allow_scipy_integrate):
                                bad.append(f"S2: {name}:{node.lineno}: from scipy import {alias.name!r} is not allowed")
                    elif not _import_allowed(mod, siblings, allow_scipy_integrate=allow_scipy_integrate):
                        bad.append(f"S2: {name}:{node.lineno}: from {mod!r} import ... is not in the allowlist")
                    else:
                        for alias in node.names:
                            if alias.name == "*":
                                bad.append(f"S2: {name}:{node.lineno}: wildcard import from {mod!r} is refused")
                            elif alias.name in BANNED_ATTRS or alias.name in BANNED_NAMES or alias.name.startswith("__"):
                                bad.append(f"S3: {name}:{node.lineno}: importing banned symbol {alias.name!r} from {mod!r}")

            # S3: banned names, calls, reflection, dunders, library I/O attributes
            elif isinstance(node, ast.Name):
                if node.id in BANNED_NAMES:
                    bad.append(f"S3: {name}:{node.lineno}: banned name {node.id!r}")
                elif node.id.startswith("__") and node.id.endswith("__"):
                    bad.append(f"S3: {name}:{node.lineno}: dunder name {node.id!r} is refused")
            elif isinstance(node, ast.Attribute):
                attr = node.attr
                if attr.startswith("__") and attr.endswith("__"):
                    is_super_init = (
                        attr == "__init__"
                        and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "super"
                    )
                    if not is_super_init:
                        bad.append(f"S3: {name}:{node.lineno}: dunder attribute access .{attr} is refused")
                elif attr in BANNED_ATTRS:
                    bad.append(f"S3: {name}:{node.lineno}: banned file/serialization/env attribute .{attr}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if (node.name.startswith("__") and node.name.endswith("__")
                        and node.name not in ("__init__", "__post_init__")):
                    bad.append(f"S3: {name}:{node.lineno}: dunder method definition {node.name!r} is refused")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in REFLECTION_CALLS:
                    if (len(node.args) < 2 or not isinstance(node.args[1], ast.Constant)
                            or not isinstance(node.args[1].value, str)):
                        bad.append(f"S3: {name}:{node.lineno}: {node.func.id}() requires a constant string attribute name")
                    else:
                        aname = node.args[1].value
                        if aname.startswith("_") or aname in BANNED_ATTRS:
                            bad.append(f"S3: {name}:{node.lineno}: {node.func.id}() of {aname!r} is refused")

    return bad


def read_working_copy(working_dir: str) -> dict[str, bytes]:
    """Read every file in `working_dir` ONCE through O_NOFOLLOW fds, refusing links, subdirs,
    hard links (`st_nlink > 1`), disallowed filenames, and size > 300 KB."""
    if not os.path.isdir(working_dir) or os.path.islink(working_dir):
        raise SnapshotError(f"working copy {working_dir!r} is not a plain directory")
    try:
        entries = sorted(os.listdir(working_dir))
    except OSError as e:
        raise SnapshotError(f"cannot list working copy {working_dir!r}: {e}") from None
    files: dict[str, bytes] = {}
    total = 0
    for name in entries:
        if not _allowed_filename(name):
            raise SnapshotError(f"S1: disallowed entry {name!r} in working copy")
        try:
            data, st = SIO.read_nofollow(working_dir, name, max_bytes=MAX_SNAPSHOT_BYTES + 1)
        except (SIO.LinkRefused, OSError) as e:
            raise SnapshotError(f"S1: {name}: {e}") from None
        if not stat.S_ISREG(st.st_mode):
            raise SnapshotError(f"S1: {name}: not a regular file")
        if st.st_nlink > 1:
            raise SnapshotError(f"S1: {name}: hard-linked file refused (st_nlink={st.st_nlink})")
        total += len(data)
        if total > MAX_SNAPSHOT_BYTES:
            raise SnapshotError(f"S1: working copy exceeds {MAX_SNAPSHOT_BYTES} bytes")
        files[name] = data
    return files


def create_snapshot(working_dir: str, snapshots_dir: str, *, run_static: bool = True,
                    request: Mapping | None = None, allow_scipy_integrate: bool = False) -> dict:
    """Create an immutable content-addressed snapshot under `snapshots_dir/<sha>/` and manifest
    `snapshots_dir/<sha>.json`. The working copy stays writable. Returns manifest dict."""
    files = read_working_copy(working_dir)
    if run_static:
        probs = check_static(files, request=request, allow_scipy_integrate=allow_scipy_integrate)
        if probs:
            raise SnapshotError("static checks failed: " + "; ".join(probs[:6]))
    return _publish_snapshot_files(files, snapshots_dir)


def create_reference_snapshot(ref_py_bytes: bytes, snapshots_dir: str, *,
                              request: Mapping | None = None) -> dict:
    """Validate and snapshot an independent A11 reference implementation (`reference.py`, §4.4)
    into content-addressed read-only `snapshots_dir/<sha>/`, allowing `scipy.integrate`."""
    files = {
        "engine.py": bytes(ref_py_bytes),
        "card.yaml": b"name: built_reference\nkind: reference\nbenchmarks: []\n",
        "ENGINE_NOTES.md": b"# Independent reference implementation\n",
    }
    probs = check_static(files, request=request, allow_scipy_integrate=True)
    if probs:
        raise SnapshotError("reference static checks failed: " + "; ".join(probs[:6]))
    return _publish_snapshot_files(files, snapshots_dir)


def _publish_snapshot_files(files: Mapping[str, bytes], snapshots_dir: str) -> dict:
    file_hashes = {name: _sha256_bytes(data) for name, data in sorted(files.items())}
    sha = snapshot_digest(file_hashes)
    manifest = {
        "snapshot_sha256": sha,
        "files": [{"name": name, "sha256": file_hashes[name], "size": len(files[name])}
                  for name in sorted(files)],
    }
    os.makedirs(snapshots_dir, exist_ok=True)
    snap_dir = os.path.join(snapshots_dir, sha)
    manifest_rel = f"{sha}.json"
    if os.path.exists(snap_dir) and os.path.exists(os.path.join(snapshots_dir, manifest_rel)):
        # Idempotent: verify existing snapshot matches byte-for-byte before reusing
        existing = read_verified_snapshot(snapshots_dir, sha)
        if existing == files:
            return manifest
        raise SnapshotTampered(f"existing snapshot {sha[:12]} differs from working copy bytes")
    if os.path.lexists(snap_dir):
        # Recover cleanly from an interrupted snapshot creation (e.g. directory locked to 0555 before manifest write)
        if os.path.islink(snap_dir) or not os.path.isdir(snap_dir):
            raise SnapshotError(f"snapshot path {sha[:12]} is not a plain directory")
        dfd_prev = SIO.dir_fd_nofollow(snapshots_dir, [sha])
        try:
            os.fchmod(dfd_prev, 0o755)
            prev_entries = list(os.listdir(dfd_prev))
        finally:
            os.close(dfd_prev)
        for entry in prev_entries:
            try:
                SIO.remove_nofollow(snapshots_dir, f"{sha}/{entry}")
            except (SIO.LinkRefused, OSError) as e:
                raise SnapshotError(f"cannot clean unpublished snapshot {sha[:12]} entry {entry!r}: {e}") from None
    # Write each file atomically via safe_io (no links followed), then lock down permissions 0444 / 0555
    for name in sorted(files):
        SIO.write_nofollow(snapshots_dir, f"{sha}/{name}", files[name], atomic=True)
        fd = SIO.open_nofollow(snapshots_dir, f"{sha}/{name}")
        try:
            os.fchmod(fd, 0o444)
        finally:
            os.close(fd)
    dfd = SIO.dir_fd_nofollow(snapshots_dir, [sha])
    try:
        os.fchmod(dfd, 0o555)
    finally:
        os.close(dfd)
    SIO.write_nofollow(snapshots_dir, manifest_rel, json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"),
                       atomic=True)
    return manifest


def read_verified_snapshot(snapshots_dir: str, expected_sha: str) -> dict[str, bytes]:
    """Read and verify every file of `snapshots_dir/<expected_sha>/` ONCE (no-follow, link-count 1,
    exact directory membership, per-file sha256, and content-addressed root sha256). Raises
    `SnapshotTampered` on ANY mismatch before any compile can happen."""
    if not isinstance(expected_sha, str) or len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
        raise SnapshotTampered(f"invalid snapshot sha256 {expected_sha!r}")
    manifest_rel = f"{expected_sha}.json"
    try:
        mbytes, mst = SIO.read_nofollow(snapshots_dir, manifest_rel)
    except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
        raise SnapshotTampered(f"snapshot manifest {manifest_rel} unreadable: {e}") from None
    if mst.st_nlink > 1:
        raise SnapshotTampered(f"snapshot manifest {manifest_rel} has st_nlink={mst.st_nlink}")
    try:
        manifest = json.loads(mbytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SnapshotTampered(f"snapshot manifest {manifest_rel} invalid JSON: {e}") from None
    if not isinstance(manifest, dict) or manifest.get("snapshot_sha256") != expected_sha:
        raise SnapshotTampered(f"snapshot manifest {manifest_rel} sha256 header mismatch")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise SnapshotTampered(f"snapshot manifest {manifest_rel} has no files list")
    expected_hashes: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, dict) or not _allowed_filename(item.get("name")):
            raise SnapshotTampered(f"snapshot manifest {manifest_rel} contains invalid file entry {item!r}")
        name, fsha = str(item["name"]), item.get("sha256")
        if name in expected_hashes or not isinstance(fsha, str) or len(fsha) != 64:
            raise SnapshotTampered(f"snapshot manifest {manifest_rel} has duplicate or invalid hash for {name!r}")
        expected_hashes[name] = fsha
    if snapshot_digest(expected_hashes) != expected_sha:
        raise SnapshotTampered(f"snapshot manifest {manifest_rel} file list digest does not equal {expected_sha}")

    # Verify exact directory membership (catches added sibling files or missing files before reading/compiling)
    try:
        dfd = SIO.dir_fd_nofollow(snapshots_dir, [expected_sha])
    except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
        raise SnapshotTampered(f"snapshot directory {expected_sha} unreadable: {e}") from None
    try:
        actual_names = sorted(os.listdir(dfd))
    finally:
        os.close(dfd)
    if actual_names != sorted(expected_hashes):
        extra = sorted(set(actual_names) - set(expected_hashes))
        missing = sorted(set(expected_hashes) - set(actual_names))
        raise SnapshotTampered(f"snapshot {expected_sha[:12]} directory membership tampered "
                               f"(extra={extra}, missing={missing})")

    verified: dict[str, bytes] = {}
    total = 0
    for name in actual_names:
        try:
            data, st = SIO.read_nofollow(snapshots_dir, f"{expected_sha}/{name}", max_bytes=MAX_SNAPSHOT_BYTES + 1)
        except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
            raise SnapshotTampered(f"snapshot file {name} unreadable: {e}") from None
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise SnapshotTampered(f"snapshot file {name} is not a plain single-link file (st_nlink={st.st_nlink})")
        if _sha256_bytes(data) != expected_hashes[name]:
            raise SnapshotTampered(f"snapshot file {name} digest mismatch (tampered after snapshot)")
        total += len(data)
        if total > MAX_SNAPSHOT_BYTES:
            raise SnapshotTampered(f"snapshot {expected_sha[:12]} exceeds size limit")
        verified[name] = data
    return verified


def _is_sha64(v) -> bool:
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)


def verify_admitted_engine(
    run_dir: str,
    *,
    require_signoff: bool = True,
    request_sha256: str | None = None,
    engine_id: str | None = None,
    config_sha256: str | None = None,
    allowed_signers: tuple[str, ...] = ("owner", "lead"),
) -> tuple[dict, list[str]]:
    """Unified verifier for an admitted (and optionally signed) agent-built engine chain in `run_dir`.
    Verifies `admission.json`, `run_state.json` `admission_sha256` (if recorded), `signoff.json` (when
    `require_signoff=True`), content-addressed snapshot bytes, `admission_sheet.md`, mandatory `bench/*`,
    and optional `bench/fitted.yaml` via single-link `O_NOFOLLOW` reads. Returns `(info, problems)`."""
    info: dict = {"adm": None, "adm_bytes": b"", "adm_sha": "", "sheet_bytes": b"", "bound": {},
                  "signoff": None, "snapshot_sha256": None, "verified_files": {}, "signed_bench_digests": {}}
    problems: list[str] = []

    so: dict | None = None
    bound: dict = {}
    if require_signoff:
        try:
            so_bytes, so_st = SIO.read_nofollow(run_dir, "engine_build/signoff.json")
            if so_st.st_nlink > 1:
                return info, ["artifact_tampered: engine_build/signoff.json has multiple hard links"]
            so = json.loads(so_bytes.decode("utf-8"))
        except FileNotFoundError:
            return info, ["artifact_tampered: engine_build/signoff.json is missing"]
        except (SIO.LinkRefused, OSError, UnicodeDecodeError, ValueError) as e:
            return info, [f"artifact_tampered: engine_build/signoff.json unreadable: {e}"]
        if not isinstance(so, dict) or not isinstance(so.get("sha256"), dict):
            return info, ["artifact_tampered: engine_build/signoff.json malformed"]
        if so.get("by") not in allowed_signers:
            return info, [f"artifact_tampered: engine sign-off signer {so.get('by')!r} is not in {list(allowed_signers)}"]
        bound = so["sha256"]
        info["signoff"] = so

    try:
        adm_bytes, adm_st = SIO.read_nofollow(run_dir, "engine_build/admission.json")
        if adm_st.st_nlink > 1:
            return info, ["artifact_tampered: engine_build/admission.json has multiple hard links"]
        adm_sha = _sha256_bytes(adm_bytes)
        adm = json.loads(adm_bytes.decode("utf-8"))
    except FileNotFoundError:
        return info, ["artifact_tampered: engine_build/admission.json is missing"]
    except (SIO.LinkRefused, OSError, UnicodeDecodeError, ValueError) as e:
        return info, [f"artifact_tampered: engine_build/admission.json unreadable: {e}"]
    if not isinstance(adm, dict) or adm.get("status") != "admitted":
        st_val = adm.get("status") if isinstance(adm, dict) else None
        return info, [f"artifact_tampered: built engine is not admitted (status={st_val!r})"]
    if engine_id is not None and adm.get("engine_id") != engine_id:
        problems.append(f"artifact_tampered: admission engine_id {adm.get('engine_id')!r} != {engine_id!r}")
    if request_sha256 is not None and adm.get("request_sha256") != request_sha256:
        problems.append("artifact_tampered: admission request_sha256 does not match signed request")
    if config_sha256 is not None and adm.get("config_sha256") != config_sha256:
        problems.append(f"artifact_tampered: admission config_sha256 {str(adm.get('config_sha256'))[:12]} "
                        f"!= {config_sha256[:12]}")

    rs_path = os.path.join(run_dir, "run_state.json")
    if os.path.lexists(rs_path):
        try:
            rs_bytes, rs_st = SIO.read_nofollow(run_dir, "run_state.json")
            if rs_st.st_nlink > 1:
                problems.append("artifact_tampered: run_state.json has multiple hard links")
            else:
                rs = json.loads(rs_bytes.decode("utf-8"))
                if isinstance(rs, dict) and isinstance(rs.get("engine_build"), dict):
                    rec_adm_sha = rs["engine_build"].get("admission_sha256")
                    if isinstance(rec_adm_sha, str) and len(rec_adm_sha) == 64 and rec_adm_sha != adm_sha:
                        problems.append("artifact_tampered: admission.json changed since ENGINE_ADMIT recorded its digest")
        except (SIO.LinkRefused, OSError, UnicodeDecodeError, ValueError) as e:
            problems.append(f"artifact_tampered: run_state.json unreadable: {e}")

    snap_sha = adm.get("snapshot_sha256")
    if not _is_sha64(snap_sha):
        return info, problems + ["artifact_tampered: engine_build/admission.json missing valid snapshot_sha256"]
    if require_signoff:
        if so.get("snapshot_sha256") != snap_sha or bound.get("snapshot") != snap_sha:
            problems.append("artifact_tampered: engine_build/signoff.json missing or inconsistent snapshot sha256")
        if bound.get("admission.json") != adm_sha:
            problems.append("artifact_tampered: engine_build/admission.json sha256 does not match engine_build/signoff.json")

    info.update({"adm": adm, "adm_bytes": adm_bytes, "adm_sha": adm_sha, "snapshot_sha256": snap_sha})
    snaps_dir = os.path.join(run_dir, "engine_build", "snapshots")
    try:
        info["verified_files"] = read_verified_snapshot(snaps_dir, snap_sha)
    except SnapshotError as e:
        problems.append(f"artifact_tampered: {e}")

    try:
        sheet_bytes, sheet_st = SIO.read_nofollow(run_dir, "engine_build/admission_sheet.md")
        if sheet_st.st_nlink > 1:
            problems.append("artifact_tampered: engine_build/admission_sheet.md has multiple hard links")
        else:
            sheet_sha = _sha256_bytes(sheet_bytes)
            info["sheet_bytes"] = sheet_bytes
            if require_signoff and bound.get("admission_sheet.md") != sheet_sha:
                problems.append("artifact_tampered: engine_build/admission_sheet.md digest mismatch")
            info["bound"] = {"admission.json": adm_sha, "admission_sheet.md": sheet_sha, "snapshot": snap_sha}
    except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
        problems.append(f"artifact_tampered: engine_build/admission_sheet.md unreadable: {e}")

    adm_digests = adm.get("digests") if isinstance(adm.get("digests"), Mapping) else {}
    for rel_key in REQUIRED_BOUND_FILES:
        if rel_key in ("admission.json", "admission_sheet.md"):
            continue
        want_adm = adm_digests.get(rel_key)
        if not _is_sha64(want_adm):
            problems.append(f"artifact_tampered: engine_build/admission.json missing valid digest for {rel_key}")
            continue
        if require_signoff:
            want_so = bound.get(rel_key)
            if not _is_sha64(want_so) or want_so != want_adm:
                problems.append(f"artifact_tampered: {rel_key} digest missing or inconsistent between admission.json and signoff.json")
                continue
        try:
            b, st = SIO.read_nofollow(run_dir, f"engine_build/{rel_key}")
            if st.st_nlink > 1:
                problems.append(f"artifact_tampered: engine_build/{rel_key} has multiple hard links")
            elif _sha256_bytes(b) != want_adm:
                problems.append(f"artifact_tampered: engine_build/{rel_key} digest mismatch")
            else:
                info["signed_bench_digests"][rel_key] = want_adm
                info["bound"][rel_key] = want_adm
        except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
            problems.append(f"artifact_tampered: engine_build/{rel_key} unreadable: {e}")

    for rel_key in OPTIONAL_BOUND_FILES:
        want_adm = adm_digests.get(rel_key)
        want_so = bound.get(rel_key) if require_signoff else want_adm
        on_disk = os.path.lexists(os.path.join(run_dir, "engine_build", rel_key))
        if want_adm is None and want_so is None and not on_disk:
            continue
        if not _is_sha64(want_adm) or (require_signoff and (not _is_sha64(want_so) or want_so != want_adm)):
            problems.append(f"artifact_tampered: engine_build/{rel_key} digest missing or inconsistent")
            continue
        try:
            b, st = SIO.read_nofollow(run_dir, f"engine_build/{rel_key}")
            if st.st_nlink > 1:
                problems.append(f"artifact_tampered: engine_build/{rel_key} has multiple hard links")
            elif _sha256_bytes(b) != want_adm:
                problems.append(f"artifact_tampered: engine_build/{rel_key} digest mismatch")
            else:
                info["signed_bench_digests"][rel_key] = want_adm
                info["bound"][rel_key] = want_adm
        except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
            problems.append(f"artifact_tampered: engine_build/{rel_key} unreadable: {e}")

    for ref_k in ("reference_snapshot", "reference_comparison"):
        rv = adm_digests.get(ref_k)
        if rv:
            if not _is_sha64(rv):
                problems.append(f"artifact_tampered: engine_build/admission.json invalid {ref_k} digest")
            else:
                info["bound"][ref_k] = str(rv)
    return info, problems


def verify_engine_snapshot(run_dir: str) -> list[str]:
    """Verify the signed built-engine snapshot and admission chain in `run_dir` (empty = intact).
    Thin wrapper around `verify_admitted_engine(run_dir, require_signoff=True)`."""
    _info, problems = verify_admitted_engine(run_dir, require_signoff=True)
    return problems


def _self_test() -> int:
    import tempfile
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    clean_py = (
        "import math\n"
        "import numpy as np\n"
        "from scipy.special import expit\n"
        "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
        "SCALE = 1.0\n"
        "class Model:\n"
        "    def __init__(self, k=1.0):\n"
        "        self.k = k\n"
        "    def evaluate(self, params, unit, package, horizon):\n"
        "        return float(expit(self.k * SCALE))\n"
    ).encode("utf-8")
    clean_card = b"name: built:toy_mod\nversion: '0.1'\nmechanisms: [simple]\n"
    clean_notes = b"# Toy model notes\n"
    base_files = {"engine.py": clean_py, "card.yaml": clean_card, "ENGINE_NOTES.md": clean_notes}

    ok(check_static(base_files) == [], f"BE12a clean module passes S1-S3 ({check_static(base_files)})")

    # BE12: banned imports, names, library I/O, reflection, scipy.integrate, top-level calls
    bad_cases = {
        "import_inspect": b"import inspect\nclass Model:\n    pass\n",
        "os_environ": b"def f():\n    return os.environ['X']\n",
        "open_call": b"def f():\n    return open('/etc/passwd').read()\n",
        "numpy_load": b"import numpy as np\ndef f():\n    return np.load('x.npy')\n",
        "dyn_getattr": b"def f(x, name):\n    return getattr(x, name)\n",
        "dunder_attr": b"def f(x):\n    return x.__class__.__subclasses__()\n",
        "scipy_integrate": b"from scipy import integrate\nclass Model:\n    pass\n",
        "top_level_call": b"import numpy as np\nX = np.zeros(3)\n",
    }
    got_bad = {k: check_static({**base_files, "engine.py": code}) for k, code in bad_cases.items()}
    ok(all(bool(v) for v in got_bad.values()),
       f"BE12 S2/S3 refuse inspect, os.environ, open, numpy.load, dynamic getattr, dunder, scipy.integrate, "
       f"and top-level call ({ {k: v[:1] for k, v in got_bad.items()} })")

    # S3 allows __post_init__ (dataclasses) and refuses async dunder methods
    post_init_py = (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Cfg:\n"
        "    x: float = 1.0\n"
        "    def __post_init__(self):\n"
        "        self.x = float(self.x)\n"
    ).encode("utf-8")
    async_dunder_py = b"class Model:\n    async def __getattr__(self, name):\n        return None\n"
    e_pi_ok = check_static({**base_files, "engine.py": post_init_py})
    e_async_bad = check_static({**base_files, "engine.py": async_dunder_py})
    ok(e_pi_ok == [] and any("S3:" in x for x in e_async_bad),
       f"BE12e S3 allows __post_init__ and refuses async dunder definitions (ok={e_pi_ok}, async={e_async_bad[:1]})")

    # BE19: snapshot immutability, tampered file refused, added sibling refused, hard link refused at snapshot
    with tempfile.TemporaryDirectory() as td:
        work = os.path.join(td, "working")
        snaps = os.path.join(td, "snapshots")
        os.makedirs(work)
        for n, b in base_files.items():
            open(os.path.join(work, n), "wb").write(b)
        man = create_snapshot(work, snaps)
        sha = man["snapshot_sha256"]
        verified = read_verified_snapshot(snaps, sha)
        ok(verified == base_files, "BE19a snapshot creates content-addressed immutable copy verified byte-for-byte")

        # Hard-linked file in working copy refused at snapshot
        work_hl = os.path.join(td, "working_hl")
        os.makedirs(work_hl)
        for n, b in base_files.items():
            open(os.path.join(work_hl, n), "wb").write(b)
        os.link(os.path.join(work_hl, "engine.py"), os.path.join(td, "outside_alias.py"))
        try:
            create_snapshot(work_hl, snaps)
            hl_refused = False
        except SnapshotError as e:
            hl_refused = "hard-linked" in str(e)
        ok(hl_refused, "BE19b hard-linked file in working copy is refused at snapshot")

        # Tamper 1: added sibling file in snapshot directory refused before any compile
        sdir = os.path.join(snaps, sha)
        os.chmod(sdir, 0o755)
        open(os.path.join(sdir, "extra.py"), "wb").write(b"X = 1\n")
        try:
            read_verified_snapshot(snaps, sha)
            sib_refused = False
        except SnapshotTampered as e:
            sib_refused = "membership tampered" in str(e)
        os.remove(os.path.join(sdir, "extra.py"))
        ok(sib_refused, "BE19c added sibling file in snapshot is refused before compile")

        # Tamper 2: modified snapshot file refused before any compile
        ep = os.path.join(sdir, "engine.py")
        os.chmod(ep, 0o644)
        open(ep, "wb").write(clean_py + b"# tampered\n")
        try:
            read_verified_snapshot(snaps, sha)
            mod_refused = False
        except SnapshotTampered as e:
            mod_refused = "digest mismatch" in str(e)
        ok(mod_refused, "BE19d modified snapshot file is refused before compile")
        os.chmod(sdir, 0o755)

    # Regressions: P3-1 non-UTF-8 ENGINE_NOTES.md, P1-16 latin-1 coding cookie, P2-5 interrupted snapshot, P1-5 empty sha256
    ok(any("ENGINE_NOTES.md" in e and "UTF-8" in e for e in check_static({**base_files, "ENGINE_NOTES.md": b"\xff"})),
       "BE12c S1 refuses non-UTF-8 ENGINE_NOTES.md")
    ok(any("coding declaration" in e for e in check_static({**base_files, "engine.py": b"# coding: latin-1\nX = 'caf\xc3\xa9'\n"})),
       "BE12d S1 refuses non-UTF-8 PEP-263 coding declaration")
    with tempfile.TemporaryDirectory() as td:
        work = os.path.join(td, "working")
        snaps = os.path.join(td, "snapshots")
        os.makedirs(work)
        for n, b in base_files.items():
            open(os.path.join(work, n), "wb").write(b)
        man = create_snapshot(work, snaps)
        sha = man["snapshot_sha256"]
        os.remove(os.path.join(snaps, f"{sha}.json"))  # simulate crash after 0555 chmod, before manifest write
        man2 = create_snapshot(work, snaps)
        ok(man2["snapshot_sha256"] == sha and read_verified_snapshot(snaps, sha) == base_files,
           "BE19f create_snapshot recovers cleanly when interrupted between 0555 lock and manifest publish")
        # P1-5: verify_engine_snapshot refuses signoff.json with empty sha256 map even with intact snapshot
        run_d = os.path.join(td, "run")
        os.makedirs(os.path.join(run_d, "engine_build", "snapshots"))
        man3 = create_snapshot(work, os.path.join(run_d, "engine_build", "snapshots"))
        open(os.path.join(run_d, "engine_build", "signoff.json"), "w").write(
            json.dumps({"by": "owner", "snapshot_sha256": man3["snapshot_sha256"], "sha256": {}})
        )
        empty_probs = verify_engine_snapshot(run_d)
        ok(bool(empty_probs), f"BE19g verify_engine_snapshot fails closed on missing bound digests ({empty_probs[:2]})")
        # BSNAP_REF1 (§4.4, §7 Q10): scipy.integrate refused for engines, allowed for A11 reference snapshots
        ref_src = (
            "from scipy.integrate import solve_ivp\n"
            "import numpy as np\n"
            "class Model:\n"
            "    def evaluate(self, params, unit, package, horizon):\n"
            "        return solve_ivp(lambda t, y: -0.1 * y, [0.0, 1.0], [1.0])\n"
        ).encode("utf-8")
        eng_blocked = bool(check_static({**base_files, "engine.py": ref_src}, allow_scipy_integrate=False))
        ref_snaps = os.path.join(td, "ref_snaps")
        ref_man = create_reference_snapshot(ref_src, ref_snaps)
        ref_ver = read_verified_snapshot(ref_snaps, ref_man["snapshot_sha256"])
        ok(eng_blocked and ref_ver["engine.py"] == ref_src,
           "BSNAP_REF1 scipy.integrate refused for built engines but allowed and snapshotted for A11 references")
        os.chmod(os.path.join(ref_snaps, ref_man["snapshot_sha256"]), 0o755)
        os.chmod(os.path.join(snaps, sha), 0o755)
        os.chmod(os.path.join(run_d, "engine_build", "snapshots", man3["snapshot_sha256"]), 0o755)

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
