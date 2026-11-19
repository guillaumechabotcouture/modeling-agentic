#!/usr/bin/env python3
"""The page's glossary, read from the workbench specification itself (docs/design/review-workbench.md).

    python3 tools/workbench/glossary.py              # print the glossary
    python3 tools/workbench/glossary.py --self-test

The page shows codes that are defined only in the specification: a scorecard case (C13), a gap (H3), a goal (G6),
and a few terms ("workbench threshold"). Their meanings are not typed into the page or into this module: `load()`
parses the specification's own tables (the sections listed in TABLES) and, for each term in TERMS, the first bullet
of the specification that puts the term in quotes. So the glossary cannot drift from the specification.

`load(path)` returns {entries: [{code, kind, meaning, section}], source, why}. It fails closed: a specification that
cannot be read, or a section with no table rows, gives no entries for that part and a reason in `why`; the page
then shows the bare code.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SOURCE = "docs/design/review-workbench.md"
# (section number, what the page calls a code of that table, the form of a code in the table's first column)
TABLES = (("7", "gap", r"H\d+"), ("8", "scorecard case", r"C\d+"), ("10", "goal", r"G\d+[a-z]?"))
TERMS = ("workbench threshold", "near miss")
# What each kind of code is, in one sentence, so that an entry can be read without the specification. The entries'
# own meanings still come from the specification's tables; these say what the tables' columns are.
KINDS = {
    "scorecard case": "A scorecard case is one kind of problem a past run had, which the workbench must show at the earliest stage it can "
                      "be seen. \"Earliest stage\" is that stage, \"Check\" the check that should raise it, and \"Now\" its score: 0 not "
                      "visible in the page, 1 shown in the page, 2 found in the page by a fresh reader of a run with that defect planted.",
    "gap": "A gap is something no run file records today, so the workbench cannot check it until the harness writes it. \"Missing\" "
           "is what is not recorded and \"Needed by\" the scorecard cases or views that need it.",
    "goal": "A goal is one stage of building the workbench itself. \"Slice\" is what the stage builds and \"Done when\" what must "
            "hold for it to count as built.",
    "term": "A term is a phrase the workbench uses with one fixed meaning, given here in the words of its rule."}
MAX_BYTES = 1 << 20


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(text):
    """(entries, problems) from the specification's text."""
    entries, problems, sections = [], [], {}
    for m in re.finditer(r"(?m)^## (\d+[a-z]?)\. (.+)$", text):
        sections[m.group(1)] = (m.start(), m.group(2).strip())
    starts = sorted(s for s, _ in sections.values()) + [len(text)]
    for number, kind, form in TABLES:
        if number not in sections:
            problems.append(f"section {number} (the {kind} table) is not in the specification")
            continue
        start, title = sections[number]
        body = text[start:next(s for s in starts if s > start)]
        rows = [_cells(ln) for ln in body.splitlines() if ln.lstrip().startswith("|")]
        head = next((r for r in rows if not re.fullmatch(form, r[0]) and not set("".join(r)) <= set("-: ")), None)
        found = [r for r in rows if re.fullmatch(form, r[0])]
        if not found or head is None:
            problems.append(f"section {number} holds no table of {kind} rows")
            continue
        for r in found:
            parts = [f"{h}: {v}" for h, v in zip(head[1:], r[1:]) if v]
            entries.append({"code": r[0], "kind": kind, "meaning": "; ".join(parts).replace("`", ""),
                            "section": f"§{number} {title}"})
    bullets = [" ".join(b.split()) for b in re.findall(r"(?m)^- (.+(?:\n(?!- |\n|#|\|).+)*)", text)]
    for term in TERMS:
        hit = next((b for b in bullets if f'"{term}"' in b.lower()), None)
        if hit is None:
            problems.append(f'no bullet of the specification defines "{term}"')
            continue
        entries.append({"code": term, "kind": "term", "meaning": hit.replace("`", ""), "section": "the rules for every view"})
    return entries, problems


def load(path=None):
    """The glossary from the specification at `path` (default: this checkout's). Never raises."""
    path = path or os.path.join(REPO, SOURCE)
    try:
        with open(path, "rb") as f:
            text = f.read(MAX_BYTES + 1)
        if len(text) > MAX_BYTES:
            raise ValueError("the file is larger than 1 MB")
        entries, problems = parse(text.decode("utf-8"))
    except (OSError, ValueError) as e:
        return {"entries": [], "source": SOURCE, "kinds": dict(KINDS), "why": f"{SOURCE} could not be read ({' '.join(str(e).split())[:120]})"}
    return {"entries": entries, "source": SOURCE, "kinds": dict(KINDS), "why": "; ".join(problems) or None}


def _self_test():
    import tempfile
    fails = []

    def ok(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    toy = ("# Spec\n\n## 3. Rules\n\n- A check adds a threshold only where needed, and labels it\n  \"workbench threshold\".\n"
           "- \"Near miss\": a value within a quarter of a threshold it passed.\n- Another rule.\n\n"
           "## 7. Not recorded\n\n| Gap | Missing | Needed by |\n|---|---|---|\n| H1 | A field on `x` | C1 |\n\n"
           "## 8. Scorecard\n\nText.\n\n| # | Problem | Check | Now |\n|---|---|---|---|\n| C1 | A thing went wrong | a `b` | 0 |\n"
           "| C2 | Another | c | 2 |\n\n## 10. Goals\n\n| Goal | Slice | Done when |\n|---|---|---|\n| G1 | α Shell | C1 at 2 |\n"
           "| G2b | β3 More | later |\n\nEach iteration:\n\n1. Read.\n")
    entries, problems = parse(toy)
    by = {(e["kind"], e["code"]): e for e in entries}
    ok("G1 one entry per table row of the three sections, named by its kind, and one per term",
       sorted(by) == [("gap", "H1"), ("goal", "G1"), ("goal", "G2b"), ("scorecard case", "C1"), ("scorecard case", "C2"),
                      ("term", "near miss"), ("term", "workbench threshold")] and not problems, str((sorted(by), problems)))
    ok("G2 a meaning is the row's other cells under the table's own column names; code marks are dropped",
       by.get(("gap", "H1"), {}).get("meaning") == "Missing: A field on x; Needed by: C1"
       and by.get(("scorecard case", "C1"), {}).get("meaning") == "Problem: A thing went wrong; Check: a b; Now: 0"
       and by.get(("goal", "G2b"), {}).get("section") == "§10 Goals", str(by))
    ok("G3 a term's meaning is the whole bullet that puts it in quotes, joined over its lines",
       by.get(("term", "workbench threshold"), {}).get("meaning", "").startswith("A check adds a threshold")
       and by[("term", "workbench threshold")]["meaning"].endswith('"workbench threshold".')
       and by.get(("term", "near miss"), {}).get("meaning", "").startswith('"Near miss": a value'), str(by))
    cut, cut_problems = parse(toy.replace("## 8. Scorecard", "## 9. Scorecard").replace('"workbench threshold"', "that"))
    ok("G4 fail closed: a missing section or term gives no entry for it and a reason, and the rest is kept",
       not [e for e in cut if e["kind"] == "scorecard case" or e["code"] == "workbench threshold"]
       and len(cut) == 4 and len(cut_problems) == 2, str((cut, cut_problems)))
    with tempfile.TemporaryDirectory() as tmp:
        none = load(os.path.join(tmp, "none.md"))
        open(os.path.join(tmp, "bad.md"), "wb").write(b"\xff\xfe not text \xff")
        bad = load(os.path.join(tmp, "bad.md"))
        ok("G5 fail closed: a specification that is missing or is not text gives no entries and says why, never an exception",
           none["entries"] == [] and "could not be read" in none["why"] and bad["entries"] == [] and bad["why"], str((none, bad)))
    real = load()
    codes = {(e["kind"], e["code"]) for e in real["entries"]}
    ok("G6 this checkout's specification gives the cases, gaps, goals and terms the page shows, with no problem",
       real["why"] is None and {("scorecard case", "C13"), ("gap", "H3"), ("goal", "G6"), ("goal", "G2b"),
                                ("term", "workbench threshold")} <= codes
       and all(e["meaning"] and e["section"] for e in real["entries"]), str((real["why"], sorted(codes))))
    ok("G7 the glossary reads without the specification: each kind of entry it gives has a sentence saying what it is and what its columns are",
       {e["kind"] for e in real["entries"]} <= set(real["kinds"]) and all(k in s and len(s) > 60 for k, s in real["kinds"].items())
       and all(h in real["kinds"]["scorecard case"] for h in ("Earliest stage", "Check", "Now")), str(real.get("kinds")))
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    for e in load()["entries"]:
        print(f"{e['kind']} {e['code']}\t{e['meaning']}")
