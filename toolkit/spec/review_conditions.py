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
        spec_path: analysis.decision_tests      # adopted: a path of the signed spec's own YAML, or
        decision_test: DT_window                # an analysis.decision_tests id of it
        reason: "..."                   # declined: why (the owner only)
        owner: "owner"                  # deferred: who, and
        due: 2026-12-01                 # by when (ISO date)

The registered precondition `review_conditions` (toolkit/spec/preconditions.py), by call:
  - signing (a signer): every row resolves; a declined row needs the owner; the sign-off sheet, when there is one,
    shows this file (its sources line records the file's sha256); while the file exists, each undeclared HIGH finding
    of the spec critique is a row (a run opts in by having the file).
  - a signed run (status, launch, resume, the analysis): the file is the one the signature bound (each signer records
    its sha256) and every row still resolves; a declined row needs the owner's signature.
  - no signature and no signer (a draft, a run nobody signs): no check.
A file that cannot be read, is a link, has a duplicate key or does not have this shape is a problem (fail closed); no
file means no condition was raised. Importing never overwrites a row already in the file, and an import that brings
another text under an existing id is refused. Nothing here names a disease.
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
SHEET_KEY = "review_conditions_sha256"      # in the sign-off sheet's sources line
RECORD_KEY = "review_conditions_sha256"     # in the stage machine's sign-off record (the others: sha256[FILE])


def read_bytes(run_dir: str) -> bytes | None:
    """The file's bytes, None when there is none. A link, a hard-linked or unreadable file raises ValueError."""
    try:
        data, st = read_nofollow(run_dir, FILE)
    except FileNotFoundError:
        return None
    except (LinkRefused, OSError) as e:
        raise ValueError(f"{FILE} cannot be read: {e}") from None
    if st.st_nlink != 1:
        raise ValueError(f"{FILE} cannot be read: it has {st.st_nlink} hard links (a plain file of this run only)")
    return data


def sha_of(data: bytes | None) -> str:
    return hashlib.sha256(data).hexdigest() if data is not None else "none"


def sha(run_dir: str) -> str:
    """The file's sha256, "none" without one, "unreadable" when it cannot be read."""
    try:
        return sha_of(read_bytes(run_dir))
    except ValueError:
        return "unreadable"


def _loader():
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
        dup = sorted({str(k) for k in keys if keys.count(k) > 1})
        if dup:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key(s) {dup}", node.start_mark)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)
    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return Strict


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def parse(data: bytes | None) -> list:
    """The rows; ValueError when the bytes do not have the file's shape."""
    import yaml
    if data is None:
        return []
    try:
        doc = yaml.load(data.decode("utf-8"), Loader=_loader())  # noqa: S506 -- a SafeLoader subclass
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"{FILE} does not parse: {type(e).__name__}: {str(e).splitlines()[0][:120]}") from None
    if not isinstance(doc, dict) or set(doc) != {"conditions"} or not isinstance(doc["conditions"], list):
        raise ValueError(f"{FILE} is not a mapping with only a `conditions` list")
    rows, seen = doc["conditions"], set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not _text(r.get("id")) or not _text(r.get("text")):
            raise ValueError(f"{FILE} conditions[{i}] is not a row with an id and a text (each text)")
        if r["id"] in seen:
            raise ValueError(f"{FILE} conditions[{i}]: id {r['id']!r} appears twice")
        seen.add(r["id"])
        if r.get("source") not in SOURCES:
            raise ValueError(f"{FILE} {r['id']}: source {r.get('source')!r} is not one of {list(SOURCES)}")
        if r.get("disposition") is not None and not isinstance(r["disposition"], str):
            raise ValueError(f"{FILE} {r['id']}: disposition is not text")
        bad = [k for k in ("spec_path", "decision_test", "reason", "owner") if k in r and r[k] is not None
               and not isinstance(r[k], str)]
        if bad:
            raise ValueError(f"{FILE} {r['id']}: {bad} must be text")
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
    if isinstance(v, datetime.date) and not isinstance(v, datetime.datetime):
        return v
    try:
        return datetime.date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def row_problems(r: dict, raw: dict, owner: bool | None) -> list:
    """Why the row's disposition does not resolve (empty: it does). `raw`: the signed spec's own YAML. `owner`: whether
    the signer (or the recorded signature) is the owner's; None: not judged (the sheet)."""
    rid, d = r["id"], r.get("disposition")
    if d in (None, ""):
        return [f"{rid} ({r['source']}) has no disposition: adopt it (spec_path or decision_test), decline it with a "
                f"reason (the owner), or defer it with an owner and a due date"]
    if d not in DISPOSITIONS:
        return [f"{rid}: disposition {d!r} is not one of {list(DISPOSITIONS)}"]
    if owner is False and not (d == "deferred" and r.get("owner") == "owner"):
        return [f"{rid}: a lead may only defer a review condition to the owner (deferred, owner: owner; owner "
                f"decision 2026-11-21, D2); adopting or declining it is the owner's"]
    if d == "adopted":
        sp, dt = r.get("spec_path"), r.get("decision_test")
        if not _text(sp) and not _text(dt):
            return [f"{rid}: adopted names neither a spec_path nor a decision_test"]
        an = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}
        tests = {t["id"] for t in an.get("decision_tests") or [] if isinstance(t, dict) and _text(t.get("id"))}
        out = [f"{rid}: adopted at spec_path {sp!r}, which the signed spec does not have"] \
            if _text(sp) and not _has_path(raw, sp) else []
        return out + ([f"{rid}: adopted as decision_test {dt!r}, which is not an analysis.decision_tests id"]
                      if _text(dt) and dt not in tests else [])
    if d == "declined":
        if not _text(r.get("reason")):
            return [f"{rid}: declined without a reason"]
        return []
    out = [] if _text(r.get("owner")) else [f"{rid}: deferred without an owner"]
    return out + ([] if _date(r.get("due")) else [f"{rid}: deferred without a due date (ISO, e.g. 2026-12-01)"])


def recorded(rec: dict) -> tuple:
    """(signer, bound sha256) of a sign-off record: the toolkit's and the brief's record `signed_by` and
    sha256[FILE]; the stage machine's `by` and RECORD_KEY. A record that predates W0-G bound no file ("none")."""
    by = rec.get("signed_by", rec.get("by"))
    bound = (rec.get("sha256") or {}).get(FILE) if isinstance(rec.get("sha256"), dict) else None
    return by, bound or rec.get(RECORD_KEY) or "none"


def problems(run_dir: str, spec, signer: str | None, spec_bytes: bytes | None = None, **_ctx) -> list:
    """The registered (context) precondition; see the module docstring for what each call checks."""
    import yaml
    from toolkit.spec import signoff as SO
    rec = SO._spec_record(run_dir) if run_dir and signer is None else None
    if signer is None and rec is None:
        return []
    try:            # a signer passes the bytes it binds in its record, read once (Codex r2 P1-1/2)
        data = _ctx["review_bytes"] if "review_bytes" in _ctx else read_bytes(run_dir)
        rows = parse(data)
    except ValueError as e:
        return [f"review conditions: {e}"]
    out = []
    if signer is None:
        by, bound = recorded(rec)
        if bound != sha_of(data):
            return [f"review conditions: {FILE} changed since the spec was signed (signed {bound[:12]}, now "
                    f"{sha_of(data)[:12]}): the spec is signed again over it"]
        owner = by == "owner"
    else:
        owner = signer == "owner"
        try:
            sheet = read_nofollow(run_dir, SO.SHEET)[0].decode("utf-8", "replace")
        except FileNotFoundError:
            sheet = None
        except (LinkRefused, OSError) as e:
            return [f"review conditions: the sign-off sheet cannot be read: {e}"]
        src = SO.sheet_sources(sheet) if sheet is not None else None
        if sheet is not None and (src or {}).get(SHEET_KEY, "none") != sha_of(data):
            out.append(f"review conditions: the sign-off sheet shows other review conditions than {FILE} now holds (it "
                       "is stale): the sheet is regenerated before the owner signs; read the new one")
    try:                # the signed YAML itself, never the spec with its settings merged (Codex r1 P2)
        if spec_bytes is None:
            spec_bytes = read_nofollow(run_dir, SO.SPEC)[0]
        raw = yaml.safe_load(spec_bytes.decode("utf-8"))
    except (FileNotFoundError, LinkRefused, OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        return out + [f"review conditions: the spec cannot be read to resolve the dispositions: {type(e).__name__}"]
    raw = raw if isinstance(raw, dict) else {}
    if signer is not None and data is not None:
        try:
            crit = from_critique(raw, run_dir)
        except Exception as e:  # noqa: BLE001 -- a critique that cannot run cannot be shown covered (fail closed)
            return out + [f"review conditions: the spec critique could not run: {type(e).__name__}: {e}"]
        have = {(r["id"], r["source"], " ".join(r["text"].split())) for r in rows}
        out += [f"review conditions: the spec critique's HIGH finding {c['id']} is not a row of {FILE} with its text "
                "(python3 toolkit/spec/review_conditions.py import <run_dir> --critique)" for c in crit
                if (c["id"], c["source"], " ".join(c["text"].split())) not in have]
    return out + [f"review conditions: {p}" for r in rows for p in row_problems(r, raw, owner)]


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
            doc = yaml.load(f, Loader=_loader())  # noqa: S506 -- a SafeLoader subclass refusing duplicate keys
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"review file {path} cannot be read: {type(e).__name__}") from None
    items = doc.get("conditions") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError(f"review file {path} is not a list of conditions")
    out = []
    for i, x in enumerate(items):
        text = x if isinstance(x, str) else (x.get("text") if isinstance(x, dict) else None)
        rid = (x.get("id") if isinstance(x, dict) else None) or (_row_id(source, text) if _text(text) else None)
        if not _text(text) or not _text(rid):
            raise ValueError(f"review file {path} item {i} has no text, or an id that is not text")
        out.append({"id": rid, "source": source, "text": text, "disposition": None})
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
    have = {r["id"]: r for r in rows}
    clash = [r["id"] for r in new_rows if r["id"] in have and " ".join(r["text"].split())
             != " ".join(have[r["id"]]["text"].split())]
    if clash:           # Codex r1 P1: another condition under an id already in the file is never dropped silently
        raise ValueError(f"import refused: id(s) {clash[:5]} are already in {FILE} with another text; give them new ids")
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


LEAD_DEFER_DAYS = 7
PROVISIONAL = "D2: deferred to the owner by the lead at an unattended sign-off; the owner disposes of it"
REPORT_HEADING = "## Review conditions deferred to the owner (provisional, unattended sign-off)"


def import_critique(run_dir: str) -> int:
    """Owner decision 2026-11-21: before signing, the spec critique's undeclared HIGH findings become rows (the stage
    machine calls this at SIGN). The count added; a row already there keeps its disposition."""
    import yaml
    raw = yaml.safe_load(read_nofollow(run_dir, "decision_spec.yaml")[0].decode("utf-8"))
    return add(run_dir, from_critique(raw if isinstance(raw, dict) else {}, run_dir))


def missing_critique(run_dir: str) -> list:
    """The spec critique's undeclared HIGH findings that are not rows of the file (with their text): a signature
    made without them did not see them."""
    import yaml
    raw = yaml.safe_load(read_nofollow(run_dir, "decision_spec.yaml")[0].decode("utf-8"))
    have = {(r["id"], r["source"], " ".join(r["text"].split())) for r in parse(read_bytes(run_dir))}
    return [c["id"] for c in from_critique(raw if isinstance(raw, dict) else {}, run_dir)
            if (c["id"], c["source"], " ".join(c["text"].split())) not in have]


def lead_defer(run_dir: str, today: datetime.date | None = None) -> int:
    """Under --signoff lead: every open row is deferred to the owner, provisionally (D2), and the run proceeds; the
    report lists them at its top. Rows already disposed are left as they are. The count deferred."""
    import yaml
    rows = parse(read_bytes(run_dir))
    due = ((today or datetime.date.today()) + datetime.timedelta(days=LEAD_DEFER_DAYS)).isoformat()
    n = 0
    for r in rows:
        if r.get("disposition") in (None, ""):
            r.update(disposition="deferred", owner="owner", due=due, provisional=PROVISIONAL)
            n += 1
    if n:
        write_nofollow(run_dir, FILE, yaml.safe_dump({"conditions": rows}, sort_keys=False, allow_unicode=True).encode(),
                       atomic=True)
    return n


def report_lines(run_dir: str) -> list:
    """The report's top block: the rows the lead deferred to the owner provisionally (D2), each with its text."""
    from toolkit.spec import signoff as SO
    lead = (recorded(SO._spec_record(run_dir) or {})[0] or "") not in ("", "owner")
    try:        # still deferred to the owner: provisional, or under a signature that is not the owner's (Codex)
        rows = [r for r in parse(read_bytes(run_dir)) if r.get("disposition") == "deferred" and r.get("owner") == "owner"
                and (r.get("provisional") or lead)]
    except ValueError as e:
        return [REPORT_HEADING, "", f"{FILE} could not be read: {e}", ""]
    if not rows:
        return []
    return [REPORT_HEADING, "", f"{len(rows)} condition(s) raised before signing were deferred to the owner by the "
            "lead's unattended sign-off. The owner disposes of each one.", ""] + [
        f"- **{r['id']}** ({r['source']}, due {r.get('due')}): {' '.join(r['text'].split())}" for r in rows] + [""]


def _register():
    from toolkit.spec import preconditions as PC
    PC.register("review_conditions", problems, context=True)       # context: the signed spec's own bytes


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

    def sign(d, by="owner", sheet=True):
        try:
            if sheet:
                SO.write_sheet(d)
            SO.sign(d, by, note="n")
            return "signed"
        except ValueError as e:
            return f"refused: {e}"

    def crit_rows(d, **disp):        # the toy spec's undeclared spec-critique HIGH findings, disposed
        raw = yaml.safe_load(open(os.path.join(d, SO.SPEC)))
        return [dict(r, **disp) for r in from_critique(raw, d)]
    DEF = {"disposition": "deferred", "owner": "owner", "due": "2026-12-01"}
    row = {"id": "RC1", "source": "outside_review", "text": "the effect window differs from the estimand"}
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        put(d, [dict(row)] + crit_rows(d, **DEF))
        got = PC.problems(d, None, "owner")
        ok(any("RC1" in g and "no disposition" in g for g in got), f"RC_OPEN_PROBLEM an open row is a problem: {got}")
        st = sign(d)
        ok(st.startswith("refused") and "RC1" in st, f"RC_OPEN_REFUSES_SIGN signing is refused: {st[:160]}")
        sheet = open(os.path.join(d, SO.SHEET)).read()
        ok("Review conditions" in sheet and "| RC1 |" in sheet and "OPEN" in sheet,
           "RC_SHEET the sign-off sheet lists the row and its open disposition")
        put(d, [dict(row, **DEF)] + crit_rows(d, **DEF))
        got = PC.problems(d, None, "owner")
        ok(any("stale" in g for g in got) and "stale" in sign(d, sheet=False),
           f"RC_SHEET_STALE every signer's precondition refuses a sheet showing other conditions: {got[:1]}")
        ok(PC.problems(d, None, None) == [], "RC_DRAFT_NO_CHECK no signer and no signature (a draft): not checked")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        cr = crit_rows(d, **DEF)
        put(d, [dict(row, disposition="adopted", decision_test="DT_missing")] + cr)
        got = PC.problems(d, None, "owner")
        ok(any("DT_missing" in g for g in got), f"RC_TESTED_BY_MISSING_TEST_REFUSED a missing test is refused: {got}")
        put(d, [dict(row, disposition="adopted", spec_path="objective.no_such_key")] + cr)
        ok(any("spec_path" in g for g in PC.problems(d, None, "owner")), "RC_SPEC_PATH_MISSING a missing path is refused")
        put(d, [dict(row, disposition="declined", reason="out of scope for this decision")] + cr)
        lead, owner = PC.problems(d, None, "lead"), PC.problems(d, None, "owner")
        ok(any("a lead may only defer" in g for g in lead) and not any("a lead may" in g for g in owner),
           f"RC_REJECTED_LEAD_REFUSED a declined row needs the owner: lead {lead[:1]}, owner {owner[:1]}")
        for label, r in (("declined, no reason", dict(row, disposition="declined")),
                         ("declined, reason not text", dict(row, disposition="declined", reason=True)),
                         ("deferred, no date", dict(row, disposition="deferred", owner="owner")),
                         ("deferred, owner not text", dict(row, disposition="deferred", owner=[None], due="2026-12-01"))):
            put(d, [r] + cr)
            ok(PC.problems(d, None, "owner"), f"RC_DISPOSITION_SHAPE refused: {label}")
        put(d, [dict(row, **DEF)])
        ok(any("spec critique's HIGH finding" in g for g in PC.problems(d, None, "owner")),
           "RC_CRITIQUE_COVERED while the file exists, an undeclared spec-critique HIGH finding must be a row")
        put(d, [dict(row, **DEF), dict(row, id="RC2", disposition="adopted", spec_path="objective")] + cr)
        lead = PC.problems(d, None, "lead")
        ok(any("RC2" in g and "a lead may only defer" in g for g in lead),
           f"RC_LEAD_ONLY_DEFERS a lead may not adopt a condition (D2): {lead[:1]}")
        put(d, [dict(row, **DEF), dict(row, id="RC2", **DEF)] + cr)
        got = PC.problems(d, None, "lead")
        st = sign(d, "lead")
        rec = SO._spec_record(d)
        ok(got == [] and st == "signed" and SO.status(d) == "signed" and recorded(rec)[1] == sha(d),
           f"RC_RESOLVED_SIGNS resolved rows sign and the record binds the file: {got} {st[:120]}")
        put(d, [dict(row, **DEF), dict(row, id="RC2", **DEF), dict(row, id="RC3")] + cr)
        ok(SO.status(d) == "preconditions_failed", f"RC_ADDED_AFTER_SIGN a condition added after signing stops the "
                                                   f"launch: {SO.status(d)}")
        put(d, [dict(row, disposition="declined", reason="r"), dict(row, id="RC2", **DEF)] + cr)
        ok(SO.status(d) == "preconditions_failed", "RC_DECLINE_AFTER_SIGN a disposition changed after signing is not "
                                                   "covered by the signature")
    with tempfile.TemporaryDirectory() as d:        # Codex r2: the bytes a signer binds are the bytes validated
        schema.toy_spec(d)
        cr = crit_rows(d, **DEF)
        put(d, [dict(row, **DEF)] + cr)
        SO.write_sheet(d)
        opened = yaml.safe_dump({"conditions": [dict(row)] + cr}).encode()
        got = PC.problems(d, None, "owner", review_bytes=opened)
        ok(any("no disposition" in g for g in got), f"RC_BYTES_VALIDATED a signer's own bytes are what is validated: "
                                                    f"{got[:1]}")
        put(d, [dict(row, **DEF)] + [dict(c, text="an unrelated note", source="owner_note") for c in cr])
        SO.write_sheet(d)
        ok(any("spec critique's HIGH finding" in g for g in PC.problems(d, None, "owner")),
           "RC_COVERAGE_BY_TEXT a critique finding is covered by its id, source and text, never by its id alone")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        for label, body in (("not yaml", "conditions: [: bad"), ("not a list", "conditions: 3"),
                            ("no text", "conditions:\n- {id: X, source: owner_note}\n"),
                            ("bad source", "conditions:\n- {id: X, source: gossip, text: t}\n"),
                            ("id not text", "conditions:\n- {id: [X], source: owner_note, text: t}\n"),
                            ("disposition not text", "conditions:\n- {id: X, source: owner_note, text: t, "
                                                     "disposition: [open]}\n"),
                            ("duplicate key", "conditions:\n- {id: X, source: owner_note, text: t}\nconditions: []\n"),
                            ("duplicate id", "conditions:\n- {id: X, source: owner_note, text: t}\n"
                                             "- {id: X, source: owner_note, text: u}\n")):
            open(os.path.join(d, FILE), "w").write(body)
            got = PC.problems(d, None, "owner")
            lines = SO.sheet(d)
            ok(any("review conditions" in g for g in got) and "Signing is refused" in lines,
               f"RC_FILE_UNREADABLE_FAILS_CLOSED ({label}): {got[:1]}")
        os.remove(os.path.join(d, FILE))
        os.symlink(os.path.join(d, "decision_spec.yaml"), os.path.join(d, FILE))
        ok(any("cannot be read" in g for g in PC.problems(d, None, "owner")), "RC_LINK_REFUSED a linked file fails closed")
        os.remove(os.path.join(d, FILE))
        put(d, [dict(row)])
        os.link(os.path.join(d, FILE), os.path.join(d, "elsewhere.yaml"))
        ok(any("hard links" in g for g in PC.problems(d, None, "owner")), "RC_HARDLINK_REFUSED a hard link fails closed")
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
        rows[0].update(DEF)
        put(d, rows)
        ok(add(d, from_parent(parent)) == 0 and parse(read_bytes(d))[0]["disposition"] == "deferred",
           "RC_REIMPORT_KEEPS a re-import keeps the row's disposition")
        rf = os.path.join(parent, "review.yaml")
        open(rf, "w").write(yaml.safe_dump(["check the time window", {"id": "X9", "text": "price range"}]))
        ok(add(d, from_review(rf)) == 2 and PC.problems(d, None, "owner"), "RC_IMPORT_REVIEW review rows arrive open")
        open(rf, "w").write(yaml.safe_dump([{"id": "X9", "text": "another condition"}]))
        try:
            add(d, from_review(rf))
            clash = "accepted"
        except ValueError as e:
            clash = str(e)
        ok("another text" in clash, f"RC_ID_CLASH_REFUSED another condition under an existing id is refused: {clash}")
        raw = yaml.safe_load(open(os.path.join(d, "decision_spec.yaml")))
        crit = from_critique(raw, d)
        from toolkit.spec import spec_critique as SC
        want = sum(f["severity"] == "HIGH" and not f.get("declared") for f in SC.findings(raw, d))
        ok(len(crit) == want and all(r["source"] == "spec_critique" for r in crit),
           f"RC_IMPORT_CRITIQUE the critique's undeclared HIGH findings become rows ({len(crit)} of {want})")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        ok(PC.problems(d, None, "owner") == [] and sha(d) == "none", "RC_NO_FILE no file, no condition")
        n = import_critique(d)
        ok(n == len(crit_rows(d)) and n > 0 and import_critique(d) == 0 and PC.problems(d, None, "owner"),
           f"RC_AUTO_IMPORT the critique's undeclared HIGH findings become open rows before signing, once ({n})")
        put(d, parse(read_bytes(d)) + [dict(row, disposition="adopted", spec_path="objective")])
        k = lead_defer(d, datetime.date(2026, 11, 21))
        rows_ = parse(read_bytes(d))
        st = sign(d, "lead")
        ok(k == n and all(r["disposition"] == "deferred" and r["owner"] == "owner" and r["due"] == "2026-11-28"
                          for r in rows_ if r["source"] == "spec_critique")
           and next(r for r in rows_ if r["id"] == "RC1")["disposition"] == "adopted" and "a lead may only defer" in st,
           f"RC_LEAD_DEFER the lead defers only the open rows, to the owner; it never re-disposes the owner's ({st[:90]})")
        put(d, [r for r in rows_ if r["id"] != "RC1"])
        st = sign(d, "lead")
        ok(st == "signed" and REPORT_HEADING in report_lines(d) and len(report_lines(d)) == n + 5,
           f"RC_LEAD_SIGNS_REPORTED rows deferred by the lead sign, and are listed for the report's top ({st[:200]})")
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
    try:            # every row, signed or not (Codex r2 P2): as a signer would see it, the owner's rule aside
        rows = parse(read_bytes(a.run_dir))
        raw = yaml.safe_load(read_nofollow(a.run_dir, "decision_spec.yaml")[0].decode("utf-8")) or {}
        bad = [p for r in rows for p in row_problems(r, raw if isinstance(raw, dict) else {}, None)]
    except (ValueError, OSError, LinkRefused, yaml.YAMLError) as e:
        bad = [str(e)]
    bad += problems(a.run_dir, None, None)
    try:
        bad += [f"the spec critique's HIGH finding {i} is not a row" for i in missing_critique(a.run_dir)]
    except (ValueError, OSError, LinkRefused, yaml.YAMLError) as e:
        bad.append(str(e))
    print("\n".join(bad) or "every review condition has a disposition")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
