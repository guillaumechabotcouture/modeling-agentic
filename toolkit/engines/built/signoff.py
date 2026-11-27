#!/usr/bin/env python3
"""Owner / lead sign-off, decline, engine-review validation, and report disclaimer for agent-built engines
(docs/design/agent-built-engines.md §2.2, §4.5, §5.3, §7 Q3).

    python3 toolkit/engines/built/signoff.py status <run_dir>
    python3 toolkit/engines/built/signoff.py sheet <run_dir>
    python3 toolkit/engines/built/signoff.py sign <run_dir> --by owner|lead [--note TEXT]
    python3 toolkit/engines/built/signoff.py decline <run_dir> --by owner|lead --note TEXT
    python3 toolkit/engines/built/signoff.py --self-test

`sign()` binds the sha256 of `engine_build/admission.json`, `engine_build/admission_sheet.md`, the
verified content-addressed snapshot, `engine_build/bench/{pack.yaml,results.yaml,benchmark_coverage.yaml}`
(and optional `bench/fitted.yaml`), and optional reference digests recorded by admission. All reads
and writes use single-link no-follow I/O (`scripts/lib/safe_io.py`). Nothing here names a disease.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import safe_io as SIO  # noqa: E402
from toolkit.engines.built import adapter as BADAPT  # noqa: E402
from toolkit.engines.built import snapshot as BSNAP  # noqa: E402

ADMISSION_REL = "engine_build/admission.json"
SHEET_REL = "engine_build/admission_sheet.md"
SIGNOFF_REL = "engine_build/signoff.json"
DECLINE_REL = "engine_build/decline.json"
REVIEW_FILE = "critique_engine.yaml"
REVIEW_STATUS_REL = "engine_build/review_status.json"
PRODUCER = "toolkit/engines/built/signoff.py"

_ID_RE = re.compile(r"^E-\d{3,6}$")
_VALID_VERDICTS = {"PASS", "REVISE"}
_VALID_SEVERITIES = {"HIGH", "MEDIUM", "LOW"}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_single(run_dir: str, rel: str) -> bytes:
    try:
        raw, st = SIO.read_nofollow(run_dir, rel)
    except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
        raise ValueError(f"{rel} unreadable: {e}") from None
    if st.st_nlink > 1:
        raise ValueError(f"{rel} has multiple hard links (st_nlink={st.st_nlink})")
    return raw


def _read_json_optional(run_dir: str, rel: str) -> tuple[dict | None, str | None]:
    p = os.path.join(run_dir, rel)
    if not os.path.lexists(p):
        return None, None
    try:
        raw = _read_single(run_dir, rel)
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            return None, f"{rel} must be a JSON object"
        return doc, None
    except (ValueError, UnicodeDecodeError) as e:
        return None, str(e)


def _run_signoff_mode(run_dir: str) -> str:
    doc, _ = _read_json_optional(run_dir, "run_state.json")
    if isinstance(doc, dict) and isinstance(doc.get("signoff"), str):
        return doc["signoff"]
    return "owner"


def validate_engine_critique(path: str, round_n: int = 1) -> tuple[dict | None, list[str]]:
    """Validate `critique_engine.yaml` (§4.5) against the blocker schema.
    Returns `(doc, [])` on success or `(None, [error, ...])` on failure."""
    import yaml
    errs: list[str] = []
    if not os.path.lexists(path):
        return None, [f"{os.path.basename(path)} is missing"]
    if os.path.islink(path):
        return None, [f"{os.path.basename(path)} must not be a symlink"]
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        doc = yaml.safe_load(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, [f"{os.path.basename(path)} unreadable or invalid YAML: {e}"]
    if not isinstance(doc, dict):
        return None, [f"{os.path.basename(path)} top level must be a mapping"]
    if doc.get("reviewer") != "critique-engine":
        errs.append(f"reviewer must be 'critique-engine' (got {doc.get('reviewer')!r})")
    rn = doc.get("round")
    if isinstance(rn, bool) or not isinstance(rn, int) or rn != int(round_n):
        errs.append(f"round must be {int(round_n)} (got {rn!r})")
    verdict = doc.get("verdict")
    if not isinstance(verdict, str) or verdict not in _VALID_VERDICTS:
        errs.append(f"verdict must be one of {sorted(_VALID_VERDICTS)} (got {verdict!r})")
    blockers = doc.get("blockers")
    if not isinstance(blockers, list):
        errs.append("blockers must be a list")
        blockers = []
    has_open_high = False
    seen_ids: set[str] = set()
    for idx, b in enumerate(blockers):
        if not isinstance(b, dict):
            errs.append(f"blockers[{idx}] must be a mapping")
            continue
        bid = b.get("id")
        if not isinstance(bid, str) or not _ID_RE.fullmatch(bid):
            errs.append(f"blockers[{idx}].id must match E-NNN (got {bid!r})")
        elif bid in seen_ids:
            errs.append(f"duplicate blocker id {bid!r}")
        else:
            seen_ids.add(bid)
        sev = b.get("severity")
        if not isinstance(sev, str) or sev not in _VALID_SEVERITIES:
            errs.append(f"blocker {bid}: severity must be one of {sorted(_VALID_SEVERITIES)}")
        for req_k in ("claim", "evidence", "fix_requires"):
            if not isinstance(b.get(req_k), str) or not str(b.get(req_k)).strip():
                errs.append(f"blocker {bid}: non-empty {req_k} required")
        if not isinstance(b.get("resolved"), bool):
            errs.append(f"blocker {bid}: resolved must be boolean")
        elif not b.get("resolved") and sev == "HIGH":
            has_open_high = True
    if verdict == "PASS" and has_open_high:
        errs.append("verdict is PASS but unresolved HIGH blockers are present")
    if errs:
        return None, errs
    return doc, []


def review_blocks_lead(run_dir: str) -> str | None:
    """Return a reason why `--by lead` cannot sign the admitted engine, or None when allowed.
    Fail closed: a missing, unreadable, or stale-snapshot `review_status.json`, `review_unavailable`,
    or any unresolved HIGH finding in `critique_engine.yaml` blocks the lead (§2.2, §4.5)."""
    adm, adm_err = _read_json_optional(run_dir, ADMISSION_REL)
    if adm_err or not isinstance(adm, dict):
        return f"admission record unreadable ({adm_err or 'missing'})"
    snap_sha = adm.get("snapshot_sha256")
    rst, err = _read_json_optional(run_dir, REVIEW_STATUS_REL)
    if err:
        return f"engine review status unreadable ({err})"
    if not isinstance(rst, dict):
        return "engine review status is missing (critique-engine must complete before lead sign-off)"
    if not isinstance(snap_sha, str) or rst.get("snapshot_sha256") != snap_sha:
        return "engine review status does not match current admitted snapshot (re-review required before lead sign-off)"
    if rst.get("review_unavailable"):
        return "engine review is unavailable (blocks lead signature; owner review required)"
    if int(rst.get("high_count") or 0) > 0:
        return f"engine review has {rst.get('high_count')} unresolved HIGH finding(s) (blocks lead signature)"
    return None


def _verify_admitted_evidence(run_dir: str) -> tuple[dict, bytes, bytes, dict[str, str]]:
    """Read and verify admission.json, admission_sheet.md, snapshot, bench/*, and optional reference digests."""
    adm_bytes = _read_single(run_dir, ADMISSION_REL)
    adm_sha = _sha256_bytes(adm_bytes)
    try:
        adm = json.loads(adm_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"{ADMISSION_REL} invalid JSON: {e}") from None
    if not isinstance(adm, dict) or adm.get("status") != "admitted":
        raise ValueError(f"built engine is not admitted (status={adm.get('status') if isinstance(adm, dict) else None!r})")
    rs, rs_err = _read_json_optional(run_dir, "run_state.json")
    if rs_err:
        raise ValueError(f"run_state.json unreadable: {rs_err}")
    if isinstance(rs, dict) and isinstance(rs.get("engine_build"), dict):
        rec_adm_sha = rs["engine_build"].get("admission_sha256")
        if isinstance(rec_adm_sha, str) and len(rec_adm_sha) == 64 and rec_adm_sha != adm_sha:
            raise ValueError("admission.json changed since ENGINE_ADMIT recorded its digest in run_state.json")
    snap_sha = adm.get("snapshot_sha256")
    if not isinstance(snap_sha, str) or len(snap_sha) != 64:
        raise ValueError(f"{ADMISSION_REL} missing valid snapshot_sha256")
    # Verify content-addressed snapshot bytes before signing
    try:
        BSNAP.read_verified_snapshot(os.path.join(run_dir, "engine_build", "snapshots"), snap_sha)
    except BSNAP.SnapshotError as e:
        raise ValueError(f"snapshot verification failed: {e}") from None

    sheet_bytes = _read_single(run_dir, SHEET_REL)
    adm_digests = adm.get("digests") if isinstance(adm.get("digests"), Mapping) else {}
    bound: dict[str, str] = {
        "admission.json": adm_sha,
        "admission_sheet.md": _sha256_bytes(sheet_bytes),
        "snapshot": snap_sha,
    }
    for rel in BADAPT.BENCH_FILES:
        want = adm_digests.get(rel)
        if not want:
            raise ValueError(f"{ADMISSION_REL} missing digest for {rel}")
        got = _sha256_bytes(_read_single(run_dir, f"engine_build/{rel}"))
        if got != want:
            raise ValueError(f"engine_build/{rel} changed after admission")
        bound[rel] = got
    for rel in BADAPT.OPTIONAL_BENCH_FILES:
        want = adm_digests.get(rel)
        on_disk = os.path.lexists(os.path.join(run_dir, "engine_build", rel))
        if want is None and not on_disk:
            continue
        if not want:
            raise ValueError(f"unexpected engine_build/{rel} not recorded in admission.json")
        got = _sha256_bytes(_read_single(run_dir, f"engine_build/{rel}"))
        if got != want:
            raise ValueError(f"engine_build/{rel} changed after admission")
        bound[rel] = got
    # Optional reference snapshot & comparison digests recorded by admission (§5.3)
    for ref_k in ("reference_snapshot", "reference_comparison"):
        if adm_digests.get(ref_k):
            bound[ref_k] = str(adm_digests[ref_k])
    return adm, adm_bytes, sheet_bytes, bound


def status(run_dir: str) -> str:
    """Return `absent | not_admitted | awaiting | signed | declined | changed`."""
    if not os.path.lexists(os.path.join(run_dir, ADMISSION_REL)):
        return "changed" if os.path.lexists(os.path.join(run_dir, SIGNOFF_REL)) else "absent"
    adm, adm_err = _read_json_optional(run_dir, ADMISSION_REL)
    if adm_err or not isinstance(adm, dict):
        return "changed" if os.path.lexists(os.path.join(run_dir, SIGNOFF_REL)) else "not_admitted"
    if adm.get("status") != "admitted":
        return "changed" if os.path.lexists(os.path.join(run_dir, SIGNOFF_REL)) else "not_admitted"

    so, so_err = _read_json_optional(run_dir, SIGNOFF_REL)
    if so_err:
        return "changed"
    if so is not None:
        try:
            _adm, _ab, _sb, bound = _verify_admitted_evidence(run_dir)
        except ValueError:
            return "changed"
        if so.get("by") not in ("owner", "lead"):
            return "changed"
        recorded = so.get("sha256") if isinstance(so.get("sha256"), dict) else {}
        if so.get("snapshot_sha256") != bound["snapshot"]:
            return "changed"
        for k, v in bound.items():
            if recorded.get(k) != v:
                return "changed"
        if so.get("by") == "lead" and review_blocks_lead(run_dir):
            return "changed"
        return "signed"

    dec, dec_err = _read_json_optional(run_dir, DECLINE_REL)
    if dec is not None and not dec_err:
        if dec.get("snapshot_sha256") == adm.get("snapshot_sha256"):
            return "declined"

    try:
        _verify_admitted_evidence(run_dir)
    except ValueError:
        return "changed"
    return "awaiting"


def sheet(run_dir: str) -> str:
    """Return the admission sheet markdown plus any engine-review summary."""
    raw = _read_single(run_dir, SHEET_REL)
    text = raw.decode("utf-8", errors="replace").rstrip()
    rst, _ = _read_json_optional(run_dir, REVIEW_STATUS_REL)
    if isinstance(rst, dict):
        if rst.get("review_unavailable"):
            text += "\n\n## Engine review\n\n**Review unavailable**: `critique-engine` did not produce a valid review " \
                    "after repair; owner review is required before signing.\n"
        else:
            text += f"\n\n## Engine review\n\nVerdict: `{rst.get('verdict')}` " \
                    f"(unresolved HIGH: {rst.get('high_count', 0)}, MEDIUM: {rst.get('medium_count', 0)}).\n"
    return text + "\n"


def sign(
    run_dir: str,
    by: str,
    *,
    note: str = "",
    expect_admission_sha256: str | None = None,
    expect_sheet_sha256: str | None = None,
) -> dict:
    """Sign an admitted agent-built engine, binding all admission, sheet, snapshot, and bench digests."""
    if by not in ("owner", "lead"):
        raise ValueError(f"--by must be 'owner' or 'lead' (got {by!r})")
    mode = _run_signoff_mode(run_dir)
    if mode not in BADAPT.ALLOWED_RUN_MODES:
        raise ValueError(f"cannot sign built engine in run with signoff mode {mode!r}")
    if mode == "owner" and by != "owner":
        raise ValueError("owner-mode run requires --by owner (lead signature refused)")
    if by == "lead":
        blk = review_blocks_lead(run_dir)
        if blk:
            raise ValueError(blk)
    if os.path.lexists(os.path.join(run_dir, SIGNOFF_REL)):
        raise ValueError("built engine is already signed; a change requires a new admission")
    adm, _ab, _sb, bound = _verify_admitted_evidence(run_dir)
    if expect_admission_sha256 is not None and bound["admission.json"] != expect_admission_sha256:
        raise ValueError("admission.json changed since it was shown: inspect the sheet again before signing")
    if expect_sheet_sha256 is not None and bound["admission_sheet.md"] != expect_sheet_sha256:
        raise ValueError("admission_sheet.md changed since it was shown: inspect it again before signing")
    rec = {
        "by": by,
        "provisional": by == "lead",
        "signed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "note": str(note or ""),
        "engine_id": adm.get("engine_id"),
        "kind": adm.get("kind"),
        "snapshot_sha256": bound["snapshot"],
        "sha256": bound,
        "producer": PRODUCER,
    }
    SIO.write_nofollow(run_dir, SIGNOFF_REL, json.dumps(rec, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
    SIO.remove_nofollow(run_dir, DECLINE_REL, missing_ok=True)
    return rec


def decline(run_dir: str, by: str, *, note: str) -> dict:
    """Decline an admitted agent-built engine with a required explanation note."""
    if by not in ("owner", "lead"):
        raise ValueError(f"--by must be 'owner' or 'lead' (got {by!r})")
    mode = _run_signoff_mode(run_dir)
    if mode not in BADAPT.ALLOWED_RUN_MODES:
        raise ValueError(f"cannot decline built engine in run with signoff mode {mode!r}")
    if mode == "owner" and by != "owner":
        raise ValueError("owner-mode run requires --by owner (lead decline refused)")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("decline requires a non-empty --note explaining what is missing")
    if os.path.lexists(os.path.join(run_dir, SIGNOFF_REL)):
        raise ValueError("cannot decline: built engine is already signed")
    adm, _ab, _sb, bound = _verify_admitted_evidence(run_dir)
    rec = {
        "by": by,
        "declined_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "note": note.strip(),
        "engine_id": adm.get("engine_id"),
        "snapshot_sha256": bound["snapshot"],
        "admission_sha256": bound["admission.json"],
        "producer": PRODUCER,
    }
    SIO.write_nofollow(run_dir, DECLINE_REL, json.dumps(rec, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
    return rec


def report_disclaimer_lines(run_dir: str) -> list[str]:
    """First-paragraph built-engine notice rendered at the top of report.md (§7 Q3)."""
    p = os.path.join(run_dir, SIGNOFF_REL)
    if not os.path.lexists(p):
        return []
    so, err = _read_json_optional(run_dir, SIGNOFF_REL)
    if err or not isinstance(so, dict):
        return ["**Agent-built engine.** The engine sign-off record (`engine_build/signoff.json`) could not be read.\n\n"]
    eid = so.get("engine_id") or "unknown"
    snap = str(so.get("snapshot_sha256") or "")[:12]
    by = so.get("by")
    if by == "owner":
        return [
            f"**Agent-built engine (owner-signed).** This run used an agent-built engine (`{eid}`, "
            f"snapshot `{snap}`), harness-admitted and signed by the owner (`engine_build/admission_sheet.md`).\n\n"
        ]
    if by == "lead":
        return [
            f"**Agent-built engine (lead-signed, provisional).** This run used an agent-built engine (`{eid}`, "
            f"snapshot `{snap}`), harness-admitted and signed provisionally by the lead under `--signoff lead`. "
            "A decision run requires the owner's engine signature (`engine_build/admission_sheet.md`).\n\n"
        ]
    return [
        f"**Agent-built engine (unverified signer `{by}`).** The engine sign-off record (`engine_build/signoff.json`) "
        "does not record a valid owner or lead signature.\n\n"
    ]


def _self_test() -> int:
    import tempfile
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as d:
        ok(status(d) == "absent", "BSIG1 empty run dir has status absent")
        # Build a valid admitted fixture using adapter's helper (then remove signoff.json to start awaiting)
        info = BADAPT._self_test_fixture(d, py_src=b"class Model:\n    pass\n", sign_by="owner", run_mode="owner")
        SIO.remove_nofollow(d, SIGNOFF_REL, missing_ok=True)
        ok(status(d) == "awaiting", f"BSIG2 admitted unsigned run is awaiting ({status(d)})")

        # Owner-mode run refuses lead signature and lead decline
        try:
            sign(d, "lead")
            lead_blocked = False
        except ValueError:
            lead_blocked = True
        try:
            decline(d, "lead", note="lead trying to decline owner run")
            lead_decl_blocked = False
        except ValueError:
            lead_decl_blocked = True
        ok(lead_blocked and lead_decl_blocked and status(d) == "awaiting",
           "BSIG3 owner-mode run refuses --by lead for both sign and decline")

        # Decline with note transitions status -> declined; empty note refused
        try:
            decline(d, "owner", note="   ")
            empty_refused = False
        except ValueError:
            empty_refused = True
        dec = decline(d, "owner", note="missing age stratification")
        ok(empty_refused and status(d) == "declined" and dec["note"] == "missing age stratification",
           f"BSIG4 decline records note and sets status declined ({status(d)})")

        # Sign clears decline, binds digests, and owner report disclaimer appears
        rec = sign(d, "owner", note="looks good", expect_admission_sha256=info["admission_sha256"])
        disc = "".join(report_disclaimer_lines(d))
        ok(status(d) == "signed" and rec["by"] == "owner" and "owner-signed" in disc
           and not os.path.exists(os.path.join(d, DECLINE_REL)),
           f"BSIG5 sign binds all digests, clears decline, and renders owner disclaimer ({status(d)})")

        # Tampering with a bound bench file turns status -> changed
        SIO.write_nofollow(d, "engine_build/bench/results.yaml", b"benchmarks: [tampered]\n", atomic=True)
        ok(status(d) == "changed", f"BSIG6 tampered bench/results.yaml turns status changed ({status(d)})")

    with tempfile.TemporaryDirectory() as d2:
        info2 = BADAPT._self_test_fixture(d2, py_src=b"class Model:\n    pass\n", sign_by="lead", run_mode="lead")
        SIO.remove_nofollow(d2, SIGNOFF_REL, missing_ok=True)
        # Missing review_status.json blocks lead (fail closed)
        try:
            sign(d2, "lead")
            missing_rev_blocked = False
        except ValueError:
            missing_rev_blocked = True
        SIO.write_nofollow(d2, REVIEW_STATUS_REL,
                           json.dumps({"snapshot_sha256": info2["snapshot_sha256"], "review_unavailable": True}).encode("utf-8"),
                           atomic=True)
        try:
            sign(d2, "lead")
            unavail_blocked = False
        except ValueError:
            unavail_blocked = True
        # Stale snapshot in review_status.json blocks lead
        SIO.write_nofollow(d2, REVIEW_STATUS_REL,
                           json.dumps({"snapshot_sha256": "0" * 64, "review_unavailable": False, "high_count": 0}).encode("utf-8"),
                           atomic=True)
        try:
            sign(d2, "lead")
            stale_rev_blocked = False
        except ValueError:
            stale_rev_blocked = True
        SIO.write_nofollow(d2, REVIEW_STATUS_REL,
                           json.dumps({"snapshot_sha256": info2["snapshot_sha256"],
                                       "review_unavailable": False, "high_count": 0}).encode("utf-8"),
                           atomic=True)
        rec_l = sign(d2, "lead", note="provisional")
        disc_l = "".join(report_disclaimer_lines(d2))
        ok(missing_rev_blocked and unavail_blocked and stale_rev_blocked
           and rec_l["by"] == "lead" and rec_l["provisional"] is True
           and "lead-signed, provisional" in disc_l,
           "BSIG7 missing/unavailable/stale review_status blocks lead sign; once clear for snapshot, lead signs provisionally")

    # Engine critique YAML validator (including unhashable list-valued verdict/severity)
    with tempfile.TemporaryDirectory() as d3:
        import yaml
        good_c = {
            "reviewer": "critique-engine",
            "round": 1,
            "verdict": "PASS",
            "blockers": [{
                "id": "E-001", "severity": "MEDIUM", "claim": "advisory note",
                "evidence": "engine.py:10", "fix_requires": "optional clarification", "resolved": False,
            }],
        }
        p_c = os.path.join(d3, REVIEW_FILE)
        SIO.write_nofollow(d3, REVIEW_FILE, yaml.safe_dump(good_c).encode("utf-8"), atomic=True)
        doc_ok, errs_ok = validate_engine_critique(p_c, 1)
        pass_high = {
            "reviewer": "critique-engine", "round": 1, "verdict": "PASS",
            "blockers": [{"id": "E-002", "severity": "HIGH", "claim": "bug", "evidence": "e",
                          "fix_requires": "f", "resolved": False}],
        }
        SIO.write_nofollow(d3, REVIEW_FILE, yaml.safe_dump(pass_high).encode("utf-8"), atomic=True)
        doc_ph, errs_ph = validate_engine_critique(p_c, 1)
        bad_c = {"reviewer": "wrong", "round": 1, "verdict": [],
                 "blockers": [{"id": "E-001", "severity": [], "claim": "c", "evidence": "e",
                               "fix_requires": "f", "resolved": False}]}
        SIO.write_nofollow(d3, REVIEW_FILE, yaml.safe_dump(bad_c).encode("utf-8"), atomic=True)
        doc_bad, errs_bad = validate_engine_critique(p_c, 1)
        ok(doc_ok is not None and not errs_ok and doc_ph is None and any("HIGH" in e for e in errs_ph)
           and doc_bad is None and len(errs_bad) >= 3,
           f"BSIG8 validate_engine_critique allows PASS with advisory MEDIUM, rejects PASS with open HIGH, catches malformed ({errs_bad[:3]})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_st = sub.add_parser("status", help="Show sign-off status of an admitted agent-built engine")
    p_st.add_argument("run_dir")
    p_sh = sub.add_parser("sheet", help="Print the engine admission sheet and review summary")
    p_sh.add_argument("run_dir")
    p_sg = sub.add_parser("sign", help="Sign an admitted agent-built engine")
    p_sg.add_argument("run_dir")
    p_sg.add_argument("--by", choices=("owner", "lead"), required=True)
    p_sg.add_argument("--note", default="")
    p_sg.add_argument("--expect-admission-sha256", default=None)
    p_sg.add_argument("--expect-sheet-sha256", default=None)
    p_dc = sub.add_parser("decline", help="Decline an admitted agent-built engine with a note")
    p_dc.add_argument("run_dir")
    p_dc.add_argument("--by", choices=("owner", "lead"), default="owner")
    p_dc.add_argument("--note", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            print(status(a.run_dir))
            return 0
        if a.cmd == "sheet":
            sys.stdout.write(sheet(a.run_dir))
            return 0
        if a.cmd == "sign":
            rec = sign(
                a.run_dir, a.by, note=a.note,
                expect_admission_sha256=a.expect_admission_sha256,
                expect_sheet_sha256=a.expect_sheet_sha256,
            )
            print(json.dumps(rec, indent=1, sort_keys=True))
            return 0
        if a.cmd == "decline":
            rec = decline(a.run_dir, a.by, note=a.note)
            print(json.dumps(rec, indent=1, sort_keys=True))
            return 0
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
