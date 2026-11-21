#!/usr/bin/env python3
"""Reviewer conditions: every condition raised before sign-off has a disposition (W0-G).

    python3 toolkit/spec/review_conditions.py import <run_dir> [--critique] [--review FILE] [--parent RUN_DIR]
    python3 toolkit/spec/review_conditions.py check <run_dir>
    python3 toolkit/spec/review_conditions.py --self-test

`review_conditions.yaml` in the run directory holds one row per condition that a spec critic, an outside review, an
owner note or the parent run raised before the spec is signed:

    conditions:
      - id: RC1
        source: outside_review          # spec_critique | outside_review | owner_note | parent_run
        text: "the trial ratio is applied over a different time window than its estimand"
        disposition: adopted            # adopted | declined | deferred; empty: open
        spec_path: analysis.decision_tests      # adopted: a spec path that exists, or
        decision_test: DT_window                # an analysis.decision_tests id that exists
        reason: "..."                   # declined: why (the owner only)
        owner: "owner"                  # deferred: who, and
        due: 2026-12-01                 # by when (ISO date)

A registered precondition (toolkit/spec/preconditions.py) refuses a signature, and makes a signed run's status
preconditions_failed, while any row is open or its disposition does not resolve. A file that cannot be read or does
not have this shape is a problem (fail closed); no file means no condition was raised. Importing never overwrites a
row already in the file, so a disposition survives a re-import. Nothing here names a disease.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from lib.safe_io import LinkRefused, read_nofollow, write_nofollow  # noqa: E402

FILE = "review_conditions.yaml"
SOURCES = ("spec_critique", "outside_review", "owner_note", "parent_run")
DISPOSITIONS = ("adopted", "declined", "deferred")
HEADING = "## Review conditions (each needs a disposition before signing)"


def read_bytes(run_dir: str) -> bytes | None:
    """The file's bytes, None when there is none. A link or an unreadable file raises ValueError (fail closed)."""
    try:
        return read_nofollow(run_dir, FILE)[0]
    except FileNotFoundError:
        return None
    except (LinkRefused, OSError) as e:
        raise ValueError(f"{FILE} cannot be read: {e}") from None


def parse(data: bytes | None) -> list:
    """The rows; ValueError when the bytes do not have the file's shape."""
    import yaml
    if data is None:
        return []
    try:
        doc = yaml.safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"{FILE} does not parse: {type(e).__name__}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("conditions"), list):
        raise ValueError(f"{FILE} is not a mapping with a `conditions` list")
    rows, seen = doc["conditions"], set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not str(r.get("id") or "").strip() or not str(r.get("text") or "").strip():
            raise ValueError(f"{FILE} conditions[{i}] is not a row with an id and a text")
        if r["id"] in seen:
            raise ValueError(f"{FILE} conditions[{i}]: id {r['id']!r} appears twice")
        seen.add(r["id"])
        if r.get("source") not in SOURCES:
            raise ValueError(f"{FILE} {r['id']}: source {r.get('source')!r} is not one of {list(SOURCES)}")
    return rows


def _has_path(raw, path: str) -> bool:
    node = raw
    for part in str(path).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False
    return True


def _date(v):
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v))
    except ValueError:
        return None


def row_problems(r: dict, raw: dict, signer: str | None, owner_signed: bool | None = None) -> list:
    """Why the row's disposition does not resolve (empty: it does). `owner_signed`: at a check with no signer, whether
    the run's recorded sign-off is the owner's (a declined row needs the owner)."""
    rid, d = r["id"], r.get("disposition")
    if d in (None, ""):
        return [f"{rid} ({r['source']}) has no disposition: adopt it (spec_path or decision_test), decline it with a "
                f"reason (the owner), or defer it with an owner and a due date"]
    if d not in DISPOSITIONS:
        return [f"{rid}: disposition {d!r} is not one of {list(DISPOSITIONS)}"]
    if d == "adopted":
        sp, dt = r.get("spec_path"), r.get("decision_test")
        if not sp and not dt:
            return [f"{rid}: adopted names neither a spec_path nor a decision_test"]
        tests = {str(t.get("id")) for t in ((raw.get("analysis") or {}).get("decision_tests") or [])
                 if isinstance(t, dict)} if isinstance(raw.get("analysis"), dict) else set()
        out = [f"{rid}: adopted at spec_path {sp!r}, which the spec does not have"] if sp and not _has_path(raw, sp) \
            else []
        return out + ([f"{rid}: adopted as decision_test {dt!r}, which is not an analysis.decision_tests id"]
                      if dt and str(dt) not in tests else [])
    if d == "declined":
        if not str(r.get("reason") or "").strip():
            return [f"{rid}: declined without a reason"]
        if (signer is not None and signer != "owner") or (signer is None and owner_signed is False):
            return [f"{rid}: only the owner can decline a review condition (signer {signer or 'recorded: not the owner'})"]
        return []
    out = [] if str(r.get("owner") or "").strip() else [f"{rid}: deferred without an owner"]
    return out + ([] if _date(r.get("due")) else [f"{rid}: deferred without a due date (ISO, e.g. 2026-12-01)"])


def problems(run_dir: str, spec, signer: str | None) -> list:
    """The registered precondition: every row resolves. No file: no condition was raised."""
    try:
        rows = parse(read_bytes(run_dir)) if run_dir else []
    except ValueError as e:
        return [f"review conditions: {e}"]
    if not rows:
        return []
    owner_signed = None
    if signer is None and any(r.get("disposition") == "declined" for r in rows):
        from toolkit.engines import benchmark_coverage as BC
        owner_signed = BC.signature(run_dir) != "unsigned"     # "proposed": not signed yet, the signer decides then
    raw = getattr(spec, "raw", spec) if spec is not None else {}
    return [f"review conditions: {p}" for r in rows
            for p in row_problems(r, raw if isinstance(raw, dict) else {}, signer, owner_signed)]


def _row_id(source: str, text: str) -> str:
    return f"{source}:{hashlib.sha256(' '.join(text.split()).encode()).hexdigest()[:10]}"


def from_critique(raw: dict, run_dir: str | None) -> list:
    """The spec critique's HIGH findings that the spec has not declared (toolkit/spec/spec_critique.py)."""
    from toolkit.spec import spec_critique as SC
    return [{"id": _row_id("spec_critique", f"{f['check']}: {f['text']}"), "source": "spec_critique",
             "text": f"[{f['check']}, principle {f['principle']}] {f['text']} Fix: {f['fix']}", "disposition": None}
            for f in SC.findings(raw, run_dir) if f["severity"] == "HIGH" and not f.get("declared")]


def from_review(path: str, source: str = "outside_review") -> list:
    """Rows from a review file: a list of conditions (strings, or {id?, text})."""
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"review file {path} cannot be read: {type(e).__name__}") from None
    items = doc.get("conditions") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError(f"review file {path} is not a list of conditions")
    out = []
    for i, x in enumerate(items):
        text = x if isinstance(x, str) else (x.get("text") if isinstance(x, dict) else None)
        if not str(text or "").strip():
            raise ValueError(f"review file {path} item {i} has no text")
        rid = (x.get("id") if isinstance(x, dict) else None) or _row_id(source, text)
        out.append({"id": str(rid), "source": source, "text": str(text), "disposition": None})
    return out


def from_parent(parent_dir: str) -> list:
    """The parent run's open HIGH findings (findings.jsonl), carried in as conditions."""
    p = os.path.join(parent_dir, "findings.jsonl")
    out = []
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
    except OSError as e:
        raise ValueError(f"parent ledger {p} cannot be read: {type(e).__name__}") from None
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            f = json.loads(line)
        except ValueError:
            raise ValueError(f"parent ledger line {n} is not JSON") from None
        if not isinstance(f, dict) or "id" not in f:
            raise ValueError(f"parent ledger line {n} is not a finding row")
        if f.get("severity") == "HIGH" and f.get("status") in ("open", "owner_declined"):
            out.append({"id": f"parent_run:{f['id']}", "source": "parent_run",
                        "text": f"{f.get('claim') or f['id']}" + (f" Remedy: {f['fix_requires']}"
                                                                   if f.get("fix_requires") else ""),
                        "disposition": None})
    return out


def add(run_dir: str, new_rows: list) -> int:
    """Append the rows whose id is not in the file yet (a row already there keeps its disposition); the count added."""
    import yaml
    rows = parse(read_bytes(run_dir))
    have = {r["id"] for r in rows}
    fresh = [r for r in new_rows if r["id"] not in have]
    if fresh:
        parse(yaml.safe_dump({"conditions": rows + fresh}).encode())        # the shape this module reads
        write_nofollow(run_dir, FILE, yaml.safe_dump({"conditions": rows + fresh}, sort_keys=False,
                                                     allow_unicode=True).encode(), atomic=True)
    return len(fresh)


def _cell(v) -> str:
    return " ".join(str(v if v is not None else "").split()).replace("|", "\\|")


def sheet_lines(run_dir: str, data: bytes | None = None, raw: dict | None = None) -> list:
    """The sheet's table: each row, its disposition and what it names, with its problem when it does not resolve."""
    try:
        rows = parse(data if data is not None else read_bytes(run_dir))
    except ValueError as e:
        return [HEADING, "", f"HIGH: {e}. Signing is refused until it is fixed.", ""]
    if not rows:
        return [HEADING, "", f"None: no `{FILE}`, or it lists no condition.", ""]
    out = [HEADING, "", "| id | source | condition | disposition | names | open problem |", "|---|---|---|---|---|---|"]
    for r in rows:
        names = {"adopted": " ".join(f"{k}: {r[k]}" for k in ("spec_path", "decision_test") if r.get(k)),
                 "declined": f"reason: {r.get('reason')}", "deferred": f"{r.get('owner')} by {r.get('due')}"
                 }.get(r.get("disposition"), "")
        bad = row_problems(r, raw or {}, None) if raw is not None else []
        out.append(f"| {_cell(r['id'])} | {r['source']} | {_cell(r['text'])} | {_cell(r.get('disposition') or 'OPEN')}"
                   f" | {_cell(names)} | {_cell('; '.join(bad))} |")
    return out + [""]


def sha(run_dir: str) -> str:
    """The file's sha256, "none" without one, "unreadable" when it cannot be read (the sheet records it)."""
    try:
        b = read_bytes(run_dir)
    except ValueError:
        return "unreadable"
    return hashlib.sha256(b).hexdigest() if b is not None else "none"


def _register():
    from toolkit.spec import preconditions as PC
    PC.register("review_conditions", problems)


def _self_test() -> int:
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    from toolkit.spec import preconditions as PC, schema, signoff as SO

    def put(d, rows):
        open(os.path.join(d, FILE), "w").write(yaml.safe_dump({"conditions": rows}, sort_keys=False))

    def sign(d, by="owner"):
        try:
            SO.write_sheet(d)
            SO.sign(d, by, note="n")
            return "signed"
        except ValueError as e:
            return f"refused: {e}"
    row = {"id": "RC1", "source": "outside_review", "text": "the effect window differs from the estimand"}
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        put(d, [dict(row)])
        got = PC.problems(d, None, "owner")
        ok(any("RC1" in g and "no disposition" in g for g in got), f"RC_OPEN_PROBLEM an open row is a problem: {got}")
        st = sign(d)
        ok(st.startswith("refused") and "RC1" in st, f"RC_OPEN_REFUSES_SIGN signing is refused: {st[:160]}")
        sheet = open(os.path.join(d, SO.SHEET)).read()
        ok("Review conditions" in sheet and "| RC1 |" in sheet and "OPEN" in sheet,
           "RC_SHEET the sign-off sheet lists the row and its open disposition")
        SO.write_sheet(d)
        put(d, [dict(row, disposition="deferred", owner="owner", due="2026-12-01")])
        try:
            SO.sign(d, "owner", note="n")
            st = "signed"
        except ValueError as e:
            st = f"refused: {e}"
        ok("stale" in st, f"RC_SHEET_STALE a sheet showing other conditions than the file holds is refused: {st[:120]}")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        put(d, [dict(row, disposition="adopted", decision_test="DT_missing")])
        got = PC.problems(d, None, "owner")
        ok(any("DT_missing" in g for g in got), f"RC_TESTED_BY_MISSING_TEST_REFUSED a missing test is refused: {got}")
        put(d, [dict(row, disposition="adopted", spec_path="objective.no_such_key")])
        ok(any("spec_path" in g for g in PC.problems(d, None, "owner")), "RC_SPEC_PATH_MISSING a missing path is refused")
        put(d, [dict(row, disposition="declined", reason="out of scope for this decision")])
        lead, owner = PC.problems(d, None, "lead"), PC.problems(d, None, "owner")
        ok(any("only the owner" in g for g in lead) and not owner,
           f"RC_REJECTED_LEAD_REFUSED a declined row needs the owner: lead {lead[:1]}, owner {owner}")
        put(d, [dict(row, disposition="declined")])
        ok(any("without a reason" in g for g in PC.problems(d, None, "owner")), "RC_DECLINED_NO_REASON refused")
        put(d, [dict(row, disposition="deferred", owner="owner")])
        ok(any("due date" in g for g in PC.problems(d, None, "owner")), "RC_DEFERRED_NO_DATE refused")
        put(d, [dict(row, disposition="deferred", owner="owner", due="2026-12-01"),
                dict(row, id="RC2", disposition="adopted", spec_path="objective")])
        got = PC.problems(d, None, "lead")
        st = sign(d, "lead")
        ok(got == [] and st == "signed" and SO.status(d) == "signed",
           f"RC_RESOLVED_SIGNS deferred with an owner and a date, and adopted at a spec path, sign: {got} {st[:120]}")
        put(d, [dict(row, disposition="deferred", owner="owner", due="2026-12-01"), dict(row, id="RC3")])
        ok(SO.status(d) == "preconditions_failed", f"RC_ADDED_AFTER_SIGN a condition added after signing stops the "
                                                   f"launch: {SO.status(d)}")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        for label, body in (("not yaml", "conditions: [: bad"), ("not a list", "conditions: 3"),
                            ("no text", "conditions:\n- {id: X, source: owner_note}\n"),
                            ("bad source", "conditions:\n- {id: X, source: gossip, text: t}\n"),
                            ("duplicate id", "conditions:\n- {id: X, source: owner_note, text: t}\n"
                                             "- {id: X, source: owner_note, text: u}\n")):
            open(os.path.join(d, FILE), "w").write(body)
            got = PC.problems(d, None, "owner")
            ok(any("review conditions" in g for g in got), f"RC_FILE_UNREADABLE_FAILS_CLOSED ({label}): {got[:1]}")
        os.remove(os.path.join(d, FILE))
        os.symlink(os.path.join(d, "decision_spec.yaml"), os.path.join(d, FILE))
        ok(any("cannot be read" in g for g in PC.problems(d, None, "owner")), "RC_LINK_REFUSED a linked file fails closed")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as parent:
        schema.toy_spec(d)
        open(os.path.join(parent, "findings.jsonl"), "w").write(
            json.dumps({"id": "R-001", "severity": "HIGH", "status": "open", "claim": "window mismatch"}) + "\n"
            + json.dumps({"id": "R-002", "severity": "HIGH", "status": "fixed", "claim": "done"}) + "\n"
            + json.dumps({"id": "R-003", "severity": "MEDIUM", "status": "open", "claim": "minor"}) + "\n")
        n = add(d, from_parent(parent))
        rows = parse(read_bytes(d))
        ok(n == 1 and rows[0]["id"] == "parent_run:R-001" and rows[0]["source"] == "parent_run",
           f"RC_CARRIED_FROM_PARENT only the parent's open HIGH finding is carried: {rows}")
        rows[0].update(disposition="deferred", owner="owner", due="2026-12-01")
        put(d, rows)
        ok(add(d, from_parent(parent)) == 0 and parse(read_bytes(d))[0]["disposition"] == "deferred",
           "RC_REIMPORT_KEEPS a re-import keeps the row's disposition")
        rf = os.path.join(parent, "review.yaml")
        open(rf, "w").write(yaml.safe_dump(["check the time window", {"id": "X9", "text": "price range"}]))
        ok(add(d, from_review(rf)) == 2 and PC.problems(d, None, "owner"), "RC_IMPORT_REVIEW review rows arrive open")
        raw = yaml.safe_load(open(os.path.join(d, "decision_spec.yaml")))
        crit = from_critique(raw, d)
        from toolkit.spec import spec_critique as SC
        want = sum(f["severity"] == "HIGH" and not f.get("declared") for f in SC.findings(raw, d))
        ok(len(crit) == want and all(r["source"] == "spec_critique" for r in crit),
           f"RC_IMPORT_CRITIQUE the critique's undeclared HIGH findings become rows ({len(crit)} of {want})")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        ok(PC.problems(d, None, "owner") == [] and sha(d) == "none", "RC_NO_FILE no file, no condition")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    import argparse
    import yaml
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    im = sub.add_parser("import")
    im.add_argument("run_dir")
    im.add_argument("--critique", action="store_true")
    im.add_argument("--review", action="append", default=[])
    im.add_argument("--parent")
    ck = sub.add_parser("check")
    ck.add_argument("run_dir")
    a = ap.parse_args(argv)
    if a.cmd == "import":
        rows = []
        if a.critique:
            raw = yaml.safe_load(read_nofollow(a.run_dir, "decision_spec.yaml")[0].decode("utf-8")) or {}
            rows += from_critique(raw, a.run_dir)
        for f in a.review:
            rows += from_review(f)
        if a.parent:
            rows += from_parent(a.parent)
        print(f"{add(a.run_dir, rows)} condition(s) added to {FILE}")
        return 0
    from toolkit.spec import schema
    bad = problems(a.run_dir, schema.load(os.path.join(a.run_dir, "decision_spec.yaml")), None)
    print("\n".join(bad) or "every review condition has a disposition")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
