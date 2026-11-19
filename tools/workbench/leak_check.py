#!/usr/bin/env python3
"""Leak check: no value from a stored run in the files the workbench commits (docs/design/review-workbench.md §9).

    python3 tools/workbench/leak_check.py <run dir> [<run dir> ...]      # exit 1 and list every shared number
    python3 tools/workbench/leak_check.py --self-test

The repository is public and some stored runs' inputs are licensed, so a value read from a run must never be
committed. This compares the numbers in the tracked files under tools/workbench/ and in the specification with the
numbers in each named run's text files (analysis, data, figures tables, reports). A number counts when it has a
decimal point and enough significant digits (four in prose files, five in code, where short constants abound), or
is an integer of five or more digits; it is compared on its significant digits, truncated and rounded to every
length from four up, so 0.73921846 in a run matches 0.7392 in a committed file. Small integers (counts) and
year-like values (1900 to 2100, with or without a fraction) are not checked: the specification may quote counts.
Residual: a value restated in other units, or to fewer than four digits (such as 12.3M), is not caught; the rule in
the specification, not this check, keeps those out.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SPEC = "docs/design/review-workbench.md"
NUM = re.compile(r"(?<![\w.])-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?(?![\w])")
RUN_EXT = (".yaml", ".yml", ".json", ".jsonl", ".csv", ".md", ".txt")
MAX_BYTES = 64 << 20
MIN_DIGITS = 4
PROSE = (".md", ".txt")


def digits(tok):
    """The significant digits of a number token, or None when it is not one this check compares."""
    t = tok.replace(",", "").lstrip("-")
    mant = re.split(r"[eE]", t)[0]
    if not re.fullmatch(r"\d+\.?\d*", mant):
        return None
    if 1900 <= float(mant) <= 2100 and "e" not in t.lower():
        return None                                  # a year or a point in time
    sig = mant.replace(".", "").lstrip("0")
    if "." in mant:
        sig = sig if len(sig.rstrip("0")) >= MIN_DIGITS else ""
        sig = sig.rstrip("0") if sig else ""
    else:
        sig = sig.rstrip("0") if len(mant.lstrip("0")) >= 5 else ""
    return sig if len(sig) >= MIN_DIGITS else None


def forms(sig):
    """Every truncation and rounding of a digit string from MIN_DIGITS up to its length."""
    out = {sig}
    for n in range(MIN_DIGITS, len(sig)):
        out.add(sig[:n].rstrip("0"))
        r = str(int(sig[:n]) + (1 if sig[n] >= "5" else 0)).rstrip("0")
        out.add(r)
    return {x for x in out if len(x) >= MIN_DIGITS}


def numbers(text):
    return {d for d in (digits(m.group(0)) for m in NUM.finditer(text)) if d}


def run_numbers(run_dirs):
    seen = set()
    for run in run_dirs:
        for d, _, files in os.walk(run):
            for f in files:
                p = os.path.join(d, f)
                if f.endswith(RUN_EXT) and os.path.isfile(p) and os.path.getsize(p) <= MAX_BYTES:
                    with open(p, errors="replace") as fh:
                        for sig in numbers(fh.read()):
                            seen |= forms(sig)
    return seen


def tracked(repo=REPO):
    out = subprocess.run(["git", "-C", repo, "ls-files", "tools/workbench", SPEC], capture_output=True, text=True)
    return [x for x in out.stdout.split("\n") if x and not x.endswith((".png", ".ico", ".css", "leak_check.py"))]   # this file's test numbers are invented


def check(run_dirs, files, repo=REPO):
    """[(file, line number, number)] for every committed number that a run also holds."""
    held, hits = run_numbers(run_dirs), []
    for rel in files:
        try:
            with open(os.path.join(repo, rel), errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    for m in NUM.finditer(line):
                        sig = digits(m.group(0))
                        if sig and len(sig) < (MIN_DIGITS if rel.endswith(PROSE) else MIN_DIGITS + 1):
                            continue
                        if sig and sig in held:
                            hits.append((rel, i, m.group(0)))
        except OSError as e:
            hits.append((rel, 0, f"could not be read ({e})"))      # fail closed
    return hits


def _self_test():
    fails = []

    def ok(name, cond):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            fails.append(name)
    ok("L1 a decimal of four digits or more counts; a short one, a count and a year do not",
       digits("0.73921846") == "73921846" and digits("0.25") is None and digits("3072") is None and digits("2029.25") is None
       and digits("58,203,117") == "58203117" and digits("1.0000") is None)
    ok("L2 a run's long value matches its rounding and its truncation",
       {"7392", "7393"} & forms("73921846") == {"7392"} and "58203" in forms("58203117") and "5821" not in forms("58203117"))
    with tempfile.TemporaryDirectory() as tmp:
        run, repo = os.path.join(tmp, "run", "analysis"), os.path.join(tmp, "repo")
        os.makedirs(run)
        os.makedirs(os.path.join(repo, "d"))
        open(os.path.join(run, "a.yaml"), "w").write("x: 0.73921846\nn: 58203117.4\ncount: 3072\nt: 2021.5\n")
        open(os.path.join(repo, "d", "clean.md"), "w").write("accepted 122 of 3,072 in 2026 (2021.5); threshold 0.25; hash 28cf5953\n")
        open(os.path.join(repo, "d", "leak.md"), "w").write("fine\nengine 0.7392 against the target\nheadline 58,203,117\n")
        open(os.path.join(repo, "d", "code.py"), "w").write("X = 0.7392\nY = 0.73922\n")
        hits = check([os.path.dirname(run)], ["d/clean.md", "d/leak.md", "d/code.py", "d/missing.md"], repo)
        ok("L3 a clean file passes; a quoted run value is named with its file and line (five digits in code); a "
           "missing file fails closed",
           [(f, n) for f, n, _ in hits] == [("d/leak.md", 2), ("d/leak.md", 3), ("d/code.py", 2), ("d/missing.md", 0)])
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


def main():
    if "--self-test" in sys.argv:
        return _self_test()
    runs = [os.path.expanduser(a) for a in sys.argv[1:]]
    if not runs or not all(os.path.isdir(r) for r in runs):
        print(__doc__)
        return 2
    hits = check(runs, tracked())
    for rel, n, tok in hits:
        print(f"LEAK  {rel}:{n}: {tok}")
    print(f"{len(hits)} committed number(s) also appear in the named run(s)." if hits else
          f"OK: no committed number of {MIN_DIGITS}+ significant digits appears in the {len(runs)} run(s).")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
