#!/usr/bin/env python3
"""Fail-closed gate check for agent-built engine artifacts (`engine_build/admission.json`,
snapshot manifests `engine_build/snapshots/<sha>.json`, and `engine_build/signoff.json`;
docs/design/agent-built-engines.md §5.1, §6.4).

    python3 scripts/built_engine_gate.py --self-test

Returns `[]` for runs without an `engine_build/` directory. When `engine_build/` is present,
validates `admission.json`, its snapshot manifest `<sha>.json`, verified snapshot bytes,
`signoff.json` (when present), and durable unsandboxed markers (`worker.unsandboxed_findings`).
Every corrupt, truncated, non-finite, symlinked, or tampered record emits a non-declarable HIGH
finding and never raises. Nothing here names a disease.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import safe_io as SIO  # noqa: E402
from toolkit.engines.built import BUILT_ADAPTER_REF  # noqa: E402
from toolkit.engines.built import adapter as BADAPT  # noqa: E402
from toolkit.engines.built import signoff as BSIG  # noqa: E402
from toolkit.engines.built import snapshot as BSNAP  # noqa: E402
from toolkit.engines.built import worker as BWORK  # noqa: E402

_VALID_CHECK_STATUSES = frozenset({"pass", "fail", "skipped", "not_applicable", "inconclusive", "unavailable"})
_GATING_CHECKS = frozenset({"A1", "A2", "A3", "A4", "A5", "A6", "A8", "A11"})


def _finding(kind: str, key: str, claim: str) -> dict:
    return {
        "kind": kind,
        "severity": "HIGH",
        "stage": "GATE",
        "target_stage": "ENGINE",
        "key": key,
        "scope_declarable": False,
        "claim": claim,
    }


def _reject_nonfinite(tok: str):
    raise ValueError(f"non-finite JSON constant {tok!r}")


def _has_bad_number(obj) -> bool:
    """Iterative check for NaN, Inf, or float-overflowing ints (never recurses on deep JSON)."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, bool) or cur is None or isinstance(cur, str):
            continue
        if isinstance(cur, (int, float)):
            try:
                f = float(cur)
            except OverflowError:
                return True
            if not math.isfinite(f):
                return True
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, dict):
            stack.extend(cur.values())
    return False


def _is_sha64(val) -> bool:
    return isinstance(val, str) and len(val) == 64 and all(c in "0123456789abcdef" for c in val)


def _read_plain_json(run_dir: str, rel: str) -> tuple[dict | None, bytes | None, str | None]:
    """Read a run-relative JSON object through one O_NOFOLLOW descriptor (`SIO.read_nofollow`, no TOCTOU)."""
    try:
        raw, st = SIO.read_nofollow(run_dir, rel)
    except FileNotFoundError:
        return None, None, f"{rel} is missing"
    except SIO.LinkRefused as e:
        return None, None, f"{rel} refused (symlink or non-regular file): {e}"
    except OSError as e:
        return None, None, f"{rel} unreadable: {type(e).__name__}"
    if st.st_nlink > 1:
        return None, None, f"{rel} has multiple hard links (st_nlink={st.st_nlink})"
    if not raw.strip():
        return None, raw, f"{rel} is empty"
    try:
        text = raw.decode("utf-8")
        doc = json.loads(text, parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        return None, raw, f"{rel} invalid JSON: {type(e).__name__}: {e}"
    if not isinstance(doc, dict):
        return None, raw, f"{rel} top level must be a JSON object"
    if _has_bad_number(doc):
        return None, raw, f"{rel} contains non-finite or overflowing numeric value"
    return doc, raw, None


def _choice_selects_built(run_dir: str) -> bool:
    ch_path = os.path.join(run_dir, "engine_choice.json")
    if not os.path.lexists(ch_path):
        return False
    doc, _raw, err = _read_plain_json(run_dir, "engine_choice.json")
    if err or not isinstance(doc, dict):
        return False
    eng = doc.get("engine")
    if isinstance(eng, dict) and eng.get("ref") == BUILT_ADAPTER_REF:
        return True
    return eng == BUILT_ADAPTER_REF


def gate_findings(run_dir: str) -> list[dict]:
    """Fail-closed validation of `engine_build/` admission, snapshot manifest, and sign-off records."""
    eb_dir = os.path.join(run_dir, "engine_build")
    built_chosen = _choice_selects_built(run_dir)
    if not os.path.lexists(eb_dir):
        if built_chosen:
            return [_finding("built_engine_artifact_invalid", "engine_build",
                             "engine_choice.json selects BuiltEngine but engine_build/ is missing")]
        return []
    if os.path.islink(eb_dir) or not os.path.isdir(eb_dir):
        return [_finding("built_engine_artifact_invalid", "engine_build",
                         "engine_build/ is a symlink or not a directory")]

    findings: list[dict] = []
    try:
        findings.extend(BWORK.unsandboxed_findings(run_dir))
    except Exception as e:  # noqa: BLE001
        findings.append(_finding("built_engine_artifact_invalid", "unsandboxed",
                                 f"unsandboxed check failed: {type(e).__name__}: {e}"))

    adm_rel = "engine_build/admission.json"
    so_rel = "engine_build/signoff.json"
    adm_present = os.path.lexists(os.path.join(run_dir, adm_rel))
    so_present = os.path.lexists(os.path.join(run_dir, so_rel))
    if not adm_present and not so_present:
        if built_chosen:
            findings.append(_finding("built_engine_admission_invalid", adm_rel,
                                     "engine_choice.json selects BuiltEngine but admission.json and signoff.json are missing"))
        return findings
    if built_chosen and not so_present:
        findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                 "engine_choice.json selects BuiltEngine but engine_build/signoff.json is missing"))

    adm, adm_raw, adm_err = _read_plain_json(run_dir, adm_rel)
    adm_sha = hashlib.sha256(adm_raw).hexdigest() if adm_raw is not None else None
    snap_sha = None
    if adm_err or adm is None:
        findings.append(_finding("built_engine_admission_invalid", adm_rel,
                                 f"malformed {adm_rel}: {adm_err}"))
    else:
        status = adm.get("status")
        if status not in ("admitted", "not_admitted"):
            findings.append(_finding("built_engine_admission_invalid", adm_rel,
                                     f"{adm_rel} has invalid status {status!r}"))
        snap_sha = adm.get("snapshot_sha256")
        if not _is_sha64(snap_sha):
            findings.append(_finding("built_engine_admission_invalid", adm_rel,
                                     f"{adm_rel} missing valid 64-hex snapshot_sha256"))
            snap_sha = None
        checks = adm.get("checks")
        if status == "admitted" and (not isinstance(checks, dict) or not checks):
            findings.append(_finding("built_engine_admission_invalid", adm_rel,
                                     f"{adm_rel} admitted record must include non-empty checks evidence"))
        elif "checks" in adm:
            if not isinstance(checks, dict):
                findings.append(_finding("built_engine_admission_invalid", adm_rel,
                                         f"{adm_rel} checks must be an object"))
            else:
                for cid, crow in checks.items():
                    if not isinstance(crow, dict):
                        findings.append(_finding("built_engine_admission_invalid", f"{adm_rel}:{cid}",
                                                 f"{adm_rel} check {cid} must be an object"))
                        continue
                    cst = crow.get("status")
                    if cst is not None and (not isinstance(cst, str) or cst not in _VALID_CHECK_STATUSES):
                        findings.append(_finding("built_engine_admission_invalid", f"{adm_rel}:{cid}.status",
                                                 f"{adm_rel} check {cid}.status invalid ({cst!r})"))
                    if status == "admitted" and cid in _GATING_CHECKS and cst not in ("pass", "skipped", "not_applicable"):
                        findings.append(_finding("built_engine_admission_invalid", f"{adm_rel}:{cid}",
                                                 f"{adm_rel} claims admitted despite gating check {cid} status {cst!r}"))
                    for num_k in ("passed", "total"):
                        v = crow.get(num_k)
                        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                            findings.append(_finding("built_engine_admission_invalid", f"{adm_rel}:{cid}.{num_k}",
                                                     f"{adm_rel} check {cid}.{num_k} invalid ({v!r})"))
        digests = adm.get("digests")
        if not isinstance(digests, dict):
            findings.append(_finding("built_engine_admission_invalid", adm_rel,
                                     f"{adm_rel} digests must be an object"))
        elif status == "admitted":
            for req_rel in BADAPT.BENCH_FILES:
                want = digests.get(req_rel)
                if not _is_sha64(want):
                    findings.append(_finding("built_engine_admission_invalid", f"{adm_rel}:{req_rel}",
                                             f"{adm_rel} admitted record missing valid sha256 digest for {req_rel}"))
                    continue
                try:
                    braw, bst = SIO.read_nofollow(run_dir, f"engine_build/{req_rel}")
                    if bst.st_nlink > 1 or hashlib.sha256(braw).hexdigest() != want:
                        findings.append(_finding("built_engine_admission_invalid", f"{adm_rel}:{req_rel}",
                                                 f"engine_build/{req_rel} digest mismatch against {adm_rel}"))
                except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
                    findings.append(_finding("built_engine_admission_invalid", f"{adm_rel}:{req_rel}",
                                             f"engine_build/{req_rel} unreadable: {e}"))

    # Validate snapshot manifest `<sha>.json` and verified snapshot contents
    if snap_sha is not None:
        man_rel = f"engine_build/snapshots/{snap_sha}.json"
        man, _man_raw, man_err = _read_plain_json(run_dir, man_rel)
        if man_err or man is None:
            findings.append(_finding("built_engine_snapshot_invalid", man_rel,
                                     f"malformed snapshot manifest {man_rel}: {man_err}"))
        else:
            if man.get("snapshot_sha256") != snap_sha:
                findings.append(_finding("built_engine_snapshot_invalid", man_rel,
                                         f"{man_rel} snapshot_sha256 mismatch"))
            flist = man.get("files")
            if not isinstance(flist, list) or not flist:
                findings.append(_finding("built_engine_snapshot_invalid", man_rel,
                                         f"{man_rel} files must be a non-empty list"))
            else:
                recomputed: dict[str, str] = {}
                for idx, entry in enumerate(flist):
                    if not isinstance(entry, dict):
                        findings.append(_finding("built_engine_snapshot_invalid", f"{man_rel}[{idx}]",
                                                 f"{man_rel} entry {idx} is not an object"))
                        continue
                    en, es, sz = entry.get("name"), entry.get("sha256"), entry.get("size")
                    if (not isinstance(en, str) or not en or not _is_sha64(es)
                            or isinstance(sz, bool) or not isinstance(sz, int) or sz < 0):
                        findings.append(_finding("built_engine_snapshot_invalid", f"{man_rel}[{idx}]",
                                                 f"{man_rel} entry {idx} has invalid name/sha256/size"))
                        continue
                    recomputed[en] = es
                if recomputed and BSNAP.snapshot_digest(recomputed) != snap_sha:
                    findings.append(_finding("built_engine_snapshot_invalid", man_rel,
                                             f"{man_rel} file hashes do not match snapshot_sha256"))
                try:
                    verified = BSNAP.read_verified_snapshot(os.path.join(run_dir, "engine_build", "snapshots"), snap_sha)
                    for entry in flist:
                        if isinstance(entry, dict) and entry.get("name") in verified:
                            if entry.get("size") != len(verified[entry["name"]]):
                                findings.append(_finding("built_engine_snapshot_invalid", man_rel,
                                                         f"{man_rel} size mismatch for {entry['name']}"))
                except (BSNAP.SnapshotError, OSError) as e:
                    findings.append(_finding("built_engine_snapshot_invalid", man_rel,
                                             f"snapshot verification failed: {e}"))

    # Validate signoff.json when present
    if so_present:
        so, _so_raw, so_err = _read_plain_json(run_dir, so_rel)
        if so_err or so is None:
            findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                     f"malformed {so_rel}: {so_err}"))
        else:
            if so.get("by") not in ("owner", "lead"):
                findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                         f"{so_rel} invalid signer {so.get('by')!r}"))
            else:
                if os.path.lexists(os.path.join(run_dir, "run_state.json")):
                    rs_doc, _rs_raw, rs_err = _read_plain_json(run_dir, "run_state.json")
                    if not rs_err and isinstance(rs_doc, dict):
                        if rs_doc.get("signoff") == "owner" and so.get("by") != "owner":
                            findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                                     f"{so_rel} signed by {so.get('by')!r} in an owner-signoff run"))
                if so.get("by") == "lead":
                    try:
                        blk = BSIG.review_blocks_lead(run_dir)
                        if blk:
                            findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                                     f"{so_rel} lead sign-off blocked by engine review: {blk}"))
                    except Exception as e:  # noqa: BLE001
                        findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                                 f"review_blocks_lead check failed: {type(e).__name__}: {e}"))
            so_snap = so.get("snapshot_sha256")
            if not _is_sha64(so_snap):
                findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                         f"{so_rel} missing valid snapshot_sha256"))
            elif snap_sha and so_snap != snap_sha:
                findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                         f"{so_rel} snapshot_sha256 disagrees with {adm_rel}"))
            sha_map = so.get("sha256")
            if not isinstance(sha_map, dict) or not sha_map:
                findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                         f"{so_rel} sha256 map missing or empty"))
            else:
                if adm_sha and sha_map.get("admission.json") != adm_sha:
                    findings.append(_finding("built_engine_signoff_invalid", so_rel,
                                             f"{so_rel} admission.json digest mismatch"))
                for bk, bv in sha_map.items():
                    if not _is_sha64(bv):
                        findings.append(_finding("built_engine_signoff_invalid", f"{so_rel}:{bk}",
                                                 f"{so_rel} sha256[{bk!r}] is not a valid sha256"))
            try:
                snap_probs = BSNAP.verify_engine_snapshot(run_dir)
                if snap_probs:
                    findings.append(_finding("built_engine_snapshot_invalid", so_rel,
                                             "; ".join(snap_probs[:3])))
            except Exception as e:  # noqa: BLE001
                findings.append(_finding("built_engine_snapshot_invalid", so_rel,
                                         f"verify_engine_snapshot failed: {type(e).__name__}: {e}"))
    return findings


def _self_test() -> int:
    import tempfile
    from lib import safe_io as SIO
    from toolkit.engines.built import adapter as BADAPT

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as td:
        ok(gate_findings(td) == [], "BEG1 run dir without engine_build/ returns no findings")
        d = os.path.join(td, "clean")
        os.makedirs(d)
        info = BADAPT._self_test_fixture(d, py_src=b"class Model:\n    pass\n", sign_by="owner", run_mode="owner")
        # Normalize fixture snapshot dir writable for TemporaryDirectory cleanup
        os.chmod(os.path.join(d, "engine_build", "snapshots", info["snapshot_sha256"]), 0o755)
        ok(gate_findings(d) == [], f"BEG2 clean admitted+signed fixture returns [] ({gate_findings(d)})")

        # Malformed admission.json -> HIGH finding, never raises
        SIO.write_nofollow(d, "engine_build/admission.json", b"{bad json", atomic=True)
        f_adm = gate_findings(d)
        ok(any(f["kind"] == "built_engine_admission_invalid" for f in f_adm),
           f"BEG3 corrupt admission.json fails closed ({f_adm[:1]})")

        # Malformed snapshot manifest -> HIGH finding
        d2 = os.path.join(td, "bad_man")
        os.makedirs(d2)
        info2 = BADAPT._self_test_fixture(d2, py_src=b"class Model:\n    pass\n", sign_by="owner", run_mode="owner")
        os.chmod(os.path.join(d2, "engine_build", "snapshots", info2["snapshot_sha256"]), 0o755)
        SIO.write_nofollow(d2, f"engine_build/snapshots/{info2['snapshot_sha256']}.json", b"[]", atomic=True)
        f_man = gate_findings(d2)
        ok(any(f["kind"] == "built_engine_snapshot_invalid" for f in f_man),
           f"BEG4 wrong-type snapshot manifest fails closed ({f_man[:1]})")

        # BEG5: unsigned admission claiming admitted with missing checks & empty digests fails closed
        d3 = os.path.join(td, "empty_adm")
        os.makedirs(d3)
        info3 = BADAPT._self_test_fixture(d3, py_src=b"class Model:\n    pass\n", sign_by="owner", run_mode="owner")
        os.chmod(os.path.join(d3, "engine_build", "snapshots", info3["snapshot_sha256"]), 0o755)
        SIO.remove_nofollow(d3, "engine_build/signoff.json", missing_ok=True)
        SIO.write_nofollow(
            d3, "engine_build/admission.json",
            json.dumps({"status": "admitted", "snapshot_sha256": info3["snapshot_sha256"], "digests": {}}).encode("utf-8"),
            atomic=True,
        )
        f_empty = gate_findings(d3)
        ok(any(f["kind"] == "built_engine_admission_invalid" and "checks" in f["claim"] for f in f_empty)
           and any(f["kind"] == "built_engine_admission_invalid" and "bench/pack.yaml" in f["claim"] for f in f_empty),
           f"BEG5 unsigned admitted record missing checks/digests fails closed ({len(f_empty)} findings)")

        # BEG6: 600-deep nested JSON array in admission.json never raises RecursionError
        d4 = os.path.join(td, "deep_json")
        os.makedirs(d4)
        info4 = BADAPT._self_test_fixture(d4, py_src=b"class Model:\n    pass\n", sign_by="owner", run_mode="owner")
        os.chmod(os.path.join(d4, "engine_build", "snapshots", info4["snapshot_sha256"]), 0o755)
        deep_raw = ('{"status":"not_admitted","snapshot_sha256":"' + info4["snapshot_sha256"]
                    + '","digests":{},"extra":' + "[" * 600 + "1" + "]" * 600 + "}").encode("utf-8")
        SIO.remove_nofollow(d4, "engine_build/signoff.json", missing_ok=True)
        SIO.write_nofollow(d4, "engine_build/admission.json", deep_raw, atomic=True)
        f_deep = gate_findings(d4)
        ok(isinstance(f_deep, list) and f_deep == [],
           f"BEG6 600-deep nested JSON handled iteratively without RecursionError ({f_deep})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if len(sys.argv) == 2:
        print(json.dumps(gate_findings(sys.argv[1]), indent=1))
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
