#!/usr/bin/env python3
"""The owner's review notes (docs/design/review-workbench.md §6 "Review notes"): the one file the workbench writes.

    python3 tools/workbench/review_notes.py --self-test

One JSON line per note in <run>/owner/review_notes.jsonl:
    {at, row (a row id, or null for a new row), kind, statement, disposition, decides, note}
and, in a note that says two rows are one, `same_as` (the other row's id; the row named in `row` survives).
A note on an existing row carries no kind and no statement (it never restates what a harness file says) and must
say something: a disposition, whether the row decides, another row it is the same as, or a text. A note that adds
a row carries a kind and a statement.

`check` is the schema, used by the server before it appends a note and by the register when it reads the file back,
so a line the server would refuse is also a line the register shows as unusable. `encode` gives the one line a note
is written as; `append` writes it with lib.safe_io.write_nofollow below a descriptor on the run folder.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from lib.safe_io import write_nofollow  # noqa: E402

FILE = "owner/review_notes.jsonl"
KEYS = ("at", "row", "kind", "statement", "disposition", "decides", "note")
KINDS = ("issue", "uncertainty", "assumption")
DISPOSITIONS = ("open", "declared", "tested", "implemented", "withdrawn")
DECIDES = ("yes", "no", "unknown")
LIMITS = {"row": 400, "same_as": 400, "statement": 2000, "note": 4000}       # characters
MAX_FILE = 4 << 20            # the notes file is read whole; a larger one is not appended to


def check(note):
    """(the note, None) when it has the schema of §6, else (None, why in a few words)."""
    if not isinstance(note, dict):
        return None, "it is not a JSON object"
    extra = sorted(str(k) for k in note if k not in KEYS + ("same_as",))
    lacking = [k for k in KEYS if k not in note]
    if extra or lacking:
        return None, (f"unknown key(s) {', '.join(extra)}" if extra else f"no key {', '.join(lacking)}")
    row, same = note["row"], note.get("same_as")
    for key, words in (("kind", KINDS), ("disposition", DISPOSITIONS), ("decides", DECIDES)):
        if note[key] is not None and not (isinstance(note[key], str) and note[key] in words):
            return None, f"{key} is {note[key]!r}, not one of {', '.join(words)}"
    for key in ("row", "same_as", "statement", "note"):
        v = note.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > LIMITS[key]):
            return None, f"{key} is not a text of at most {LIMITS[key]} characters"
    if not isinstance(note["note"], str):
        return None, "note is not a text"
    try:
        _dt.datetime.fromisoformat(note["at"])
    except (TypeError, ValueError):
        return None, "at is not a time"
    if row is None:
        if same is not None:
            return None, "a note that adds a row cannot name another row as the same"
        if note["kind"] is None or not (note["statement"] or "").strip():
            return None, "a note that adds a row needs a kind and a statement"
    else:
        if not row.strip():
            return None, "row is empty"
        if note["kind"] is not None or note["statement"] is not None:
            return None, "a note on an existing row cannot restate its kind or its statement"
        if same is not None and (not same.strip() or same == row):
            return None, "same_as must name another row"
        if note["disposition"] is None and note["decides"] is None and same is None and not note["note"].strip():
            return None, "the note says nothing: no disposition, no decides, no same_as and no text"
    return note, None


def encode(note):
    """The one line a note is written as. Every character outside printable ASCII, a line break among them, is
    written as an escape, so no text in a note can start a second line."""
    return (json.dumps(note, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def parse(data):
    """[(line number, note or None, why not, the line's text)] for the notes file's bytes; blank lines are skipped."""
    out = []
    for n, raw in enumerate(data.split(b"\n"), 1):
        text = raw.decode("utf-8", "replace")
        if not text.strip():
            continue
        try:
            note, why = check(json.loads(text))
        except ValueError:
            note, why = None, "it is not JSON"
        out.append((n, note, why, text))
    return out


def append(root_fd, note, existing=None):
    """Append one checked note below the run folder's descriptor. `existing` is the file's bytes as last read (None
    when there is no file): when they do not end a line, the note still starts its own. Raises ValueError for a
    note outside the schema, and lib.safe_io.LinkRefused (a ValueError too) or OSError when the file cannot be
    written without following a link."""
    note, why = check(note)
    if note is None:
        raise ValueError(why)
    if existing is not None and len(existing) > MAX_FILE:
        raise ValueError(f"{FILE} is larger than {MAX_FILE >> 20} MB")
    lead = b"\n" if existing and not existing.endswith(b"\n") else b""
    write_nofollow(root_fd, FILE, lead + encode(note), append=True)


def now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name.split()[0])

    def note(row, **what):
        return {"at": "2026-01-02T09:00:00+00:00", "row": row, "kind": None, "statement": None, "disposition": None,
                "decides": None, "note": "", **what}
    good = [note("A-1", disposition="withdrawn"), note("A-1", decides="no"), note("A-1", note="a text"),
            note("A-1", same_as="A-2"), note(None, kind="issue", statement="s")]
    ok("RN1 the schema takes a disposition, a decides, a text, a same_as, and a new row",
       all(check(n) == (n, None) for n in good), str([check(n)[1] for n in good]))
    bad = [None, [1], note("A-1"), note("A-1", disposition="closed"), note("A-1", decides="maybe"),
           note("A-1", kind="issue"), note("A-1", statement="restated", note="x"), note("A-1", same_as="A-1"),
           note("", note="x"), note(None, kind="issue"), note(None, statement="s"), note(None, kind="issue", statement=" "),
           note(None, kind="issue", statement="s", same_as="A-1"), note("A-1", note="x", file="../x"),
           {k: v for k, v in note("A-1", note="x").items() if k != "decides"}, note("A-1", note=None),
           note("A-1", note="x", at="yesterday"), note("A-1", note="x", at=None), note("A-1", note="x" * (LIMITS["note"] + 1)),
           note(7, note="x"), note("A-1", disposition=["open"])]
    ok("RN2 everything else is refused with a reason: an unknown word, key or type, a note that says nothing, a "
       "restated statement, a row that is the same as itself, an over-long text",
       all(check(n)[0] is None and check(n)[1] for n in bad), str([n for n in bad if check(n)[0] is not None]))
    crafted = note("A-1", note='x"}\n{"at": "2026-01-02T09:00:00+00:00", "row": "A-2", "disposition": "withdrawn"}\r\n y')
    line = encode(crafted)
    ok("RN3 a note is one line whatever its text holds, and reads back as the same note",
       line.count(b"\n") == 1 and line.endswith(b"\n") and b"\r" not in line and json.loads(line) == crafted)
    with tempfile.TemporaryDirectory() as tmp:
        fd = os.open(tmp, os.O_RDONLY)
        try:
            append(fd, good[0])
            append(fd, crafted, open(os.path.join(tmp, FILE), "rb").read())
            with open(os.path.join(tmp, FILE), "ab") as f:
                f.write(b'{"cut": "off')
            append(fd, good[4], open(os.path.join(tmp, FILE), "rb").read())
            got = parse(open(os.path.join(tmp, FILE), "rb").read())
            ok("RN4 notes are appended, never overwritten; a note after a cut-off line still starts its own line",
               [(n, x is not None) for n, x, _, _ in got] == [(1, True), (2, True), (3, False), (4, True)]
               and got[1][1] == crafted and got[2][2] == "it is not JSON", str(got))
            try:
                append(fd, note("A-1", disposition="closed"))
                refused = False
            except ValueError:
                refused = True
            ok("RN5 a note outside the schema is not written", refused and len(parse(open(os.path.join(tmp, FILE), "rb").read())) == 4)
            other = os.path.join(tmp, "elsewhere.jsonl")
            open(other, "w").close()
            os.remove(os.path.join(tmp, FILE))
            os.symlink(other, os.path.join(tmp, FILE))
            try:
                append(fd, good[0])
                refused = False
            except ValueError:          # lib.safe_io.LinkRefused is a ValueError
                refused = True
            ok("RN6 a notes file that is a link is not written through", refused and os.path.getsize(other) == 0)
        finally:
            os.close(fd)
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: review_notes.py --self-test")
