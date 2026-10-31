#!/usr/bin/env python3
"""Owner sign-off of the decision spec: a thin adapter over scripts/decision_brief.py.

    python3 toolkit/spec/signoff.py status <run_dir>
    python3 toolkit/spec/signoff.py sign <run_dir> --by owner|lead [--note TEXT]
    python3 toolkit/spec/signoff.py --self-test

`decision_spec.yaml` is one of decision_brief.FROZEN_AT_SIGNOFF: when the run has a brief,
signing the brief freezes the spec with it (same record, same hashes, same restore and
`acceptance_criteria_changed` handling). A v2 run with a spec and no brief gets the same
record for the spec alone (`decision_spec.signoff.json`), until the state machine merges
the two. Either way the spec must validate before it can be signed.

status: absent | invalid | awaiting | changed | signed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import decision_brief as DB  # noqa: E402
from toolkit.spec import schema  # noqa: E402

SPEC = DB.DECISION_SPEC
SPEC_SIGNOFF = "decision_spec.signoff.json"


def _spec_record(run_dir: str) -> dict | None:
    """The sign-off that covers the spec: the brief's when it hashed the spec, else the spec-only record."""
    so = DB._signoff(run_dir)
    if so is not None and (so.get("sha256") or {}).get(SPEC) is not None:
        return so
    try:
        with open(os.path.join(run_dir, SPEC_SIGNOFF), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def status(run_dir: str) -> str:
    path = os.path.join(run_dir, SPEC)
    rec = _spec_record(run_dir)
    if not os.path.exists(path):
        return "changed" if rec is not None else "absent"
    if rec is not None:
        return "signed" if DB._sha(path) == (rec.get("sha256") or {}).get(SPEC) else "changed"
    return "invalid" if schema.validate(schema.load(path), pending_ok=True) else "awaiting"


def signed_sha(run_dir: str) -> str | None:
    rec = _spec_record(run_dir)
    return (rec.get("sha256") or {}).get(SPEC) if rec else None


def sign(run_dir: str, by: str, note: str = "", answers: dict | None = None, accept_proposed: bool = False) -> dict:
    path = os.path.join(run_dir, SPEC)
    if not os.path.exists(path):
        raise ValueError(f"{SPEC} does not exist")
    errs = schema.validate(schema.load(path), pending_ok=True)
    if errs:
        raise ValueError("spec not ready: " + "; ".join(errs[:10]))
    if _spec_record(run_dir) is not None:
        raise ValueError("the spec is already signed; a change needs a new sign-off by the owner")
    if os.path.exists(os.path.join(run_dir, DB.BRIEF)):
        return DB.sign(run_dir, by, answers=answers, note=note, accept_proposed=accept_proposed)
    record = {"signed_by": by, "signed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "note": note,
              "sha256": {SPEC: DB._sha(path)}, "producer": "toolkit/spec/signoff.py",
              "schema_version": schema.SCHEMA_VERSION}
    os.makedirs(os.path.join(run_dir, DB.SIGNED_DIR), exist_ok=True)
    shutil.copyfile(path, os.path.join(run_dir, DB.SIGNED_DIR, SPEC))
    with open(os.path.join(run_dir, SPEC_SIGNOFF), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1)
    return record


def _self_test() -> int:
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        ok(status(d) == "absent", "SG1 no spec: absent")
        schema.toy_spec(d)
        ok(status(d) == "awaiting", "SG2 a valid unsigned spec: awaiting")
        rec = sign(d, "lead", note="test run")
        ok(status(d) == "signed" and rec["sha256"][SPEC] == signed_sha(d), "SG3 signed (spec-only record)")
        with open(os.path.join(d, SPEC), "a") as f:
            f.write("\n# edited\n")
        ok(status(d) == "changed", "SG4 an edit after sign-off: changed")
        try:
            sign(d, "lead")
            ok(False, "SG5 a signed spec cannot be re-signed in place")
        except ValueError:
            ok(True, "SG5 a signed spec cannot be re-signed in place")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["comparator"]["package"] = "nope"
        yaml.safe_dump(raw, open(p, "w"))
        ok(status(d) == "invalid", "SG6 an invalid spec: invalid")
        try:
            sign(d, "owner")
            ok(False, "SG7 an invalid spec cannot be signed")
        except ValueError as e:
            ok("not ready" in str(e), "SG7 an invalid spec cannot be signed")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        yaml.safe_dump(DB.example_brief(), open(os.path.join(d, DB.BRIEF), "w"))
        yaml.safe_dump({"hard_blockers": [{"id": "HB-001", "text": "t"}]}, open(os.path.join(d, DB.CRITERIA), "w"))
        rec = sign(d, "owner")
        ok(rec.get("producer") == "scripts/decision_brief.py" and rec["sha256"].get(SPEC) and status(d) == "signed"
           and DB.status(d) == "signed", f"SG8 with a brief, the brief machinery signs and freezes the spec: {rec}")
        with open(os.path.join(d, SPEC), "a") as f:
            f.write("\n# edited\n")
        ok(DB.status(d) == "changed", "SG9 a spec edit after the brief's sign-off shows as a changed brief sign-off")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"auto": True}
        yaml.safe_dump(raw, open(p, "w"))
        try:
            st0 = status(d)
            sign(d, "owner")
            ok(st0 == "awaiting" and status(d) == "signed",
               f"SG10 a spec with engine: auto can be signed before the engine is chosen ({st0})")
        except ValueError as e:
            ok(False, f"SG10 a spec with engine: auto can be signed before the engine is chosen: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "sign"])
    ap.add_argument("run_dir")
    ap.add_argument("--by", default="owner", choices=["owner", "lead"])
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    if a.action == "status":
        print(status(a.run_dir))
        return 0
    print(json.dumps(sign(a.run_dir, a.by, a.note), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
