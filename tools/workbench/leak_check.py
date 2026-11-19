#!/usr/bin/env python3
"""Leak check: no value from a stored run in the files the workbench commits (docs/design/review-workbench.md §9).

    python3 tools/workbench/leak_check.py <run dir> [<run dir> ...]                        # the files as they are now
    python3 tools/workbench/leak_check.py --range <base>..<head> <run dir> [<run dir> ...]   # the commits to be pushed
    python3 tools/workbench/leak_check.py --self-test

The repository is public and some stored runs' inputs are licensed, so a value read from a run must never be
committed. This compares the numbers in the tracked text files under tools/workbench/ and in the specification with
the numbers in every text file of each named run. Without --range it reads the files in the working tree. With
--range it reads the lines added by every commit of the range (`git log -p`), which is what a push publishes: a
value that a later commit or an unstaged edit removed is still in the history. Exit 1 lists every shared number,
and anything that could not be scanned (a run file that cannot be read, commits git cannot list) is listed too.

A number counts when it has a decimal point and four or more significant digits, or is an integer of five or more
digits, in any tracked text file (code, style sheets and this file included). It is compared on its significant
digits, truncated and rounded to every length from four up, so a run's long value matches its four-digit rounding
in a committed file. Small integers (counts) and year-like values (1900 to 2100, with or without a fraction) are
not checked: the specification may quote counts.

A run holds millions of numbers, so an ordinary constant can match one by chance. tools/workbench/leak_allow.txt
lists such matches after review, one per line: `file, number, reason`. An entry passes that number in that file
only, needs a reason, and is never to be used for a value that was read from a run.

Residual: a value restated in other units, or to fewer than four digits, is not caught; the rule in the
specification, not this check, keeps those out.
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
PATHS = ("tools/workbench", SPEC)                    # what the workbench commits
ALLOW = "tools/workbench/leak_allow.txt"
NUM = re.compile(r"(?<![\w.])-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?(?![\w])")
MIN_DIGITS = 4
CHUNK = 1 << 20                                      # a run file is read this many characters at a time
REACH = 4096                                         # how far back from a piece's end a place to cut is looked for
SNIFF = 8192                                         # a file with a NUL byte in its first bytes is not text


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


def is_text(path):
    with open(path, "rb") as f:
        return b"\0" not in f.read(SNIFF)


def pieces(path, chunk=CHUNK):
    """The text of a file in pieces of about `chunk` characters, so a file of any size is read without being held
    whole. A piece ends after a character that cannot be part of a number; where the last REACH characters hold none
    (a long row of values with no space), it ends after a comma, which separates values in such a row."""
    with open(path, errors="replace") as f:
        tail = ""
        while True:
            new = f.read(chunk)
            text = tail + new
            if not new:
                if text:
                    yield text
                return
            k, lo = len(text), max(0, len(text) - REACH)
            while k > lo and (text[k - 1].isalnum() or text[k - 1] in "_.,+-"):
                k -= 1
            if k == lo:
                k = text.rfind(",", lo) + 1
                if not k and not lo:
                    tail = text                      # nothing to cut at yet: read on
                    continue
                k = k or len(text)
            yield text[:k]
            tail = text[k:]


def held(run_dirs, wanted, chunk=CHUNK):
    """(the digit strings of `wanted` that a number in the runs' text files matches, what could not be scanned as
    [(file, 0, why)]). Only the committed numbers are kept in memory: each run number is tested as it is read."""
    first = {w[:MIN_DIGITS] for w in wanted} | {str(int(w) - 1)[:MIN_DIGITS] for w in wanted}
    found, problems = set(), []
    for run in run_dirs:
        if not os.path.isdir(run):
            problems.append((run, 0, "could not be read (not a folder)"))
            continue

        def lost(e):
            problems.append((e.filename or run, 0, f"could not be read ({e.strerror})"))
        for d, _, files in os.walk(run, onerror=lost):
            for f in files:
                p = os.path.join(d, f)
                try:
                    if os.path.islink(p) or not os.path.isfile(p) or not is_text(p):
                        continue                     # a link is not followed; a binary file holds no text number
                    for text in pieces(p, chunk):
                        for m in NUM.finditer(text):
                            tok = m.group(0)
                            if len(tok) < MIN_DIGITS:
                                continue
                            sig = digits(tok)
                            if sig and sig[:MIN_DIGITS] in first:
                                found |= forms(sig) & wanted
                except OSError as e:
                    problems.append((p, 0, f"could not be read ({e.strerror or e})"))      # fail closed
    return found, problems


def entry(line):
    """(file, digit string, reason) of a line of the allow file, or None for a blank or comment line; a line that
    is neither raises ValueError saying what it lacks."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [x.strip() for x in line.split(",", 2)]
    if len(parts) < 3 or not parts[0] or not re.search(r"[A-Za-z]{3}", parts[2]):
        raise ValueError("an entry is `file, number, reason`, and the reason is required")
    if digits(parts[1]) is None:
        raise ValueError(f"{parts[1]!r} is not a number this check compares (write it without thousands separators)")
    return parts[0], digits(parts[1]), parts[2]


def load_allow(repo=REPO, rel=ALLOW):
    """({(file, digit string)} of the reviewed chance matches, [(file, line, why)] for every entry refused). No
    allow file means no entry."""
    allow, bad = set(), []
    try:
        with open(os.path.join(repo, rel), errors="replace") as f:
            for i, line in enumerate(f, 1):
                try:
                    e = entry(line)
                    if e:
                        allow.add(e[:2])
                except ValueError as err:
                    bad.append((rel, i, f"allow entry refused: {err}"))
    except FileNotFoundError:
        pass
    except OSError as e:
        bad.append((rel, 0, f"could not be read ({e})"))
    return allow, bad


def tracked(repo=REPO, paths=PATHS):
    """The files this check reads: those git tracks, and new ones not yet added that it does not ignore (they are
    about to be committed). Raises OSError when git cannot list them (fail closed)."""
    out = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard", "--", *paths],
                         capture_output=True, text=True)
    files = [x for x in out.stdout.split("\0") if x]
    if out.returncode != 0 or not files:
        raise OSError(f"git could not list the tracked files ({out.stderr.strip() or 'none found'})")
    return files


def _scan(run_dirs, lines, problems, allow, chunk, allow_file):
    """Hits among `lines` [(label, file, line number, text)]: [(label, line number, token)], after the problems."""
    toks = []
    for label, rel, n, text in lines:
        if rel == allow_file:            # an entry's own number is the match it records; its reason is scanned
            try:
                e = entry(text)
                text = e[2] if e else text
            except ValueError:
                pass
        toks += [(label, rel, n, m.group(0), digits(m.group(0))) for m in NUM.finditer(text)]
    wanted = {t[4] for t in toks if t[4]}
    found, lost = held(run_dirs, wanted, chunk)       # also when nothing is wanted: a run that cannot be read is said
    return problems + lost + [(label, n, tok) for label, rel, n, tok, sig in toks if sig in found and (rel, sig) not in allow]


def check(run_dirs, files, repo=REPO, allow=(), chunk=CHUNK, allow_file=ALLOW):
    """[(file, line number, number)] for every number in the files, as they are in the working tree, that a run
    also holds; a file or run that cannot be read is in the list too (line 0)."""
    lines, problems = [], []
    for rel in files:
        try:
            if not is_text(os.path.join(repo, rel)):
                continue
            with open(os.path.join(repo, rel), errors="replace") as fh:
                lines += [(rel, rel, i, line) for i, line in enumerate(fh, 1)]
        except OSError as e:
            problems.append((rel, 0, f"could not be read ({e})"))      # fail closed
    return _scan(run_dirs, lines, problems, allow, chunk, allow_file)


def added_lines(rng, repo=REPO, paths=PATHS):
    """[(commit, file, line number, text)] for every line a commit of the range adds under `paths`, from
    `git log -p`. Raises OSError when the range is not `<base>..<head>` or git cannot list its commits."""
    base, sep, tip = rng.partition("..")
    if not (sep and base and tip) or rng.startswith("-") or "..." in rng or any(c.isspace() for c in rng):
        raise OSError(f"{rng!r} is not a range of the form <base>..<head>")
    r = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", "log", "-p", "-m", "-U0", "--no-color",
                        "--no-ext-diff", "--no-textconv", "--no-renames", "--format=%x00commit %H", rng, "--", *paths],
                       capture_output=True)
    if r.returncode != 0:
        raise OSError(f"git could not list the commits of {rng} ({r.stderr.decode(errors='replace').strip()[:200]})")
    out, commit, rel, n, left = [], None, None, 0, 0
    for line in r.stdout.decode(errors="replace").split("\n"):
        if left and line[:1] in "+-\\":             # inside a hunk: its lines are counted, never read as headers
            if line[:1] == "+":
                out.append((commit, rel, n, line[1:]))
                n, left = n + 1, left - 1
            continue
        left = 0
        if line.startswith("\0commit "):
            commit, rel = line[8:].strip(), None
        elif line.startswith("+++ "):
            rel = line[6:] if line[4:6] == "b/" else None       # +++ /dev/null: the file was deleted
        elif (m := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)) and rel is not None:
            n, left = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
    return out


def check_range(run_dirs, rng, repo=REPO, allow=(), chunk=CHUNK, allow_file=ALLOW, paths=PATHS):
    """[("<commit> <file>", line number, number)] for every number a commit of the range adds that a run also
    holds. Fail closed: when git cannot list the commits, the one entry says so."""
    try:
        lines = [(f"{c[:10]} {rel}", rel, n, text) for c, rel, n, text in added_lines(rng, repo, paths)]
    except OSError as e:
        return [(rng, 0, f"could not be scanned: {e}")]
    return _scan(run_dirs, lines, [], allow, chunk, allow_file)


def _self_test():
    fails = []

    def ok(name, fn):
        try:
            why = None if fn() else "the condition does not hold"
        except Exception as e:      # a case reports and the next one runs
            why = f"{type(e).__name__}: {e}"
        print(f"  {'ok  ' if why is None else 'FAIL'} {name}" + (f": {str(why)[:300]}" if why else ""))
        if why is not None:
            fails.append(name.split()[0])

    # Every number here is invented. The short forms are cut from the long ones at run time, so that this file
    # holds no four-digit decimal of its own to be matched by chance against a run's millions of numbers.
    long, big = "0.73921846", "58203117"
    short, five = long[:6], long[:6] + "2"
    sig = long[2:6]
    ok("L1 a decimal of four digits or more counts; a short one, a count and a year do not",
       lambda: digits(long) == long[2:] and digits("0.25") is None and digits("3072") is None and digits("2029.25") is None
       and digits("58,203,117") == big and digits("1.0000") is None)
    ok("L2 a run's long value matches its rounding and its truncation",
       lambda: {sig, str(int(sig) + 1)} & forms(long[2:]) == {sig} and big[:5] in forms(big) and "5821" not in forms(big))
    with tempfile.TemporaryDirectory() as tmp:
        run, repo = os.path.join(tmp, "run", "analysis"), os.path.join(tmp, "repo")
        os.makedirs(run)
        os.makedirs(os.path.join(repo, "d"))

        def put(base, rel, text):
            with open(os.path.join(base, rel), "w") as f:
                f.write(text)

        def git(*a):
            r = subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                "-c", "commit.gpgsign=false", *a], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            return r.stdout.strip()
        runs = [os.path.dirname(run)]
        put(run, "a.yaml", f"x: {long}\nn: {big}.4\ncount: 3072\nt: 2021.5\n")
        put(repo, "d/clean.md", "accepted 122 of 3,072 in 2026 (2021.5); threshold 0.25; hash 28cf5953\n")
        put(repo, "d/leak.md", f"fine\nengine {short} against the target\nheadline 58,203,117\n")
        put(repo, "d/code.py", f"X = {short}\nY = {five}\n")
        put(repo, "d/style.css", f"/* ratio {short} */\n")

        def where(hits):
            return [(f, n) for f, n, _ in hits]
        ok("L3 a clean file passes; a quoted run value is named with its file and line; a missing file fails closed",
           lambda: where(check(runs, ["d/clean.md", "d/leak.md", "d/missing.md"], repo))
           == [("d/missing.md", 0), ("d/leak.md", 2), ("d/leak.md", 3)])
        ok("R13 four significant digits count in every text file, code and style sheets included",
           lambda: where(check(runs, ["d/code.py", "d/style.css"], repo)) == [("d/code.py", 1), ("d/code.py", 2), ("d/style.css", 1)])
        ok("R14a the tracked files scanned include the style sheet and this file",
           lambda: {"tools/workbench/static/app.css", "tools/workbench/leak_check.py"} <= set(tracked()))

        def untracked():
            git("init", "-q")
            git("add", "d/clean.md")
            put(repo, "d/.gitignore", "*.tmp\n")
            put(repo, "d/skip.tmp", "x")
            return set(tracked(repo, ("d",))) == {"d/clean.md", "d/leak.md", "d/code.py", "d/style.css", "d/.gitignore"}
        ok("R14e a new file not yet added to git is scanned too; an ignored one is not", untracked)

        def large():
            other = "0.6183940275"
            with open(os.path.join(run, "big.csv"), "w") as f:
                for _ in range(66):
                    f.write("x," * (1 << 19) + "\n")            # 66 MiB that hold no number
                f.write(f"v,{other}\n")
            put(repo, "d/big.md", f"copied {other[:8]}\n")
            try:
                return where(check(runs, ["d/big.md"], repo)) == [("d/big.md", 1)]
            finally:
                os.remove(os.path.join(run, "big.csv"))
        ok("R14b a value whose only source is a run file larger than 64 MiB is found: large files are streamed", large)

        def torn():
            other = "0.4142135623"
            put(run, "one_line.json", "[" + ",".join(["1.5"] * 40) + f",{other}," + ",".join(["2.5"] * 40) + "]")
            put(repo, "d/torn.md", f"copied {other[:9]}\n")
            return all(where(check(runs, ["d/torn.md"], repo, chunk=n)) == [("d/torn.md", 1)] for n in (7, 16, 64, 1 << 20))
        ok("R14c a number that falls across two chunks of a streamed file is read whole", torn)

        def unreadable():
            os.chmod(os.path.join(run, "one_line.json"), 0)
            try:
                return any("could not be read" in str(t) for _, _, t in check(runs, ["d/clean.md"], repo))
            finally:
                os.chmod(os.path.join(run, "one_line.json"), 0o644)
        ok("R14d a run file that cannot be read fails the check instead of being left out", unreadable)

        def history():
            git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("add", "d/leak.md", "d/code.py")
            git("commit", "-q", "-m", "a run value")
            leak = git("rev-parse", "HEAD")
            put(repo, "d/leak.md", "fine\n")
            put(repo, "d/code.py", "X = 0.5\n")
            clean_tree = check(runs, ["d/clean.md", "d/leak.md", "d/code.py"], repo) == []
            hits = check_range(runs, f"{base}..HEAD", repo, paths=("d",))
            git("commit", "-q", "-a", "-m", "cleaned up")              # a later commit that removes the value
            later = check_range(runs, f"{base}..HEAD", repo, paths=("d",))
            none = check_range(runs, "HEAD..HEAD", repo, paths=("d",))
            want = [(f"{leak[:10]} d/code.py", 1), (f"{leak[:10]} d/code.py", 2), (f"{leak[:10]} d/leak.md", 2), (f"{leak[:10]} d/leak.md", 3)]
            assert clean_tree, "the working tree, edited to drop the value, is clean"
            assert sorted(where(hits)) == want, f"the commit that added the value is named with file and line ({hits})"
            assert sorted(where(later)) == want, f"a later clean-up commit does not hide it ({later})"
            return none == []
        ok("R12a --range scans the lines each commit of the range adds, whatever the working tree holds now", history)
        ok("R12b --range fails closed when git cannot list the commits",
           lambda: all(check_range(runs, r, repo, paths=("d",)) and "could not" in str(check_range(runs, r, repo, paths=("d",))[0][2])
                       for r in ("no-such-ref..HEAD", "HEAD", "..", "--all..HEAD")))

        def allowed():
            put(repo, "d/code.py", f"X = {short}\nY = {five}\n")
            put(repo, "allow.txt", f"# reviewed chance matches\nd/code.py, {short}, an invented test constant\n"
                                   f"d/leak.md, {short}\nd/leak.md, 12, too short to be checked at all\n")
            allow, bad = load_allow(repo, "allow.txt")
            hits = check(runs, ["d/code.py"], repo, allow=allow)
            put(repo, "allow.txt", f"d/code.py, {short}, the reason quotes {big} from a run\n")
            quoted = check(runs, ["allow.txt"], repo, allow_file="allow.txt")
            assert where(bad) == [("allow.txt", 3), ("allow.txt", 4)], f"an entry with no reason or no checked number is refused ({bad})"
            assert where(hits) == [("d/code.py", 2)], f"an allowed number is passed in that file only, and its longer neighbour is not ({hits})"
            return where(quoted) == [("allow.txt", 1)]
        ok("R13b leak_allow.txt passes a reviewed chance match by file and number; each entry needs a reason, and a "
           "reason is itself scanned", allowed)
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        return _self_test()
    rng = None
    if "--range" in args:
        k = args.index("--range")
        rng, args = (args[k + 1] if k + 1 < len(args) else ""), args[:k] + args[k + 2:]
    runs = [os.path.expanduser(a) for a in args]
    if not runs or any(a.startswith("-") for a in args):
        print(__doc__)
        return 2
    allow, hits = load_allow()
    try:
        hits = hits + (check_range(runs, rng, allow=allow) if rng is not None else check(runs, tracked(), allow=allow))
    except OSError as e:
        hits.append(("the tracked files", 0, f"could not be scanned: {e}"))
    for rel, n, tok in hits:
        print(f"LEAK  {rel}:{n}: {tok}")
    what = f"the commits of {rng}" if rng is not None else "the working tree"
    print(f"{len(hits)} committed number(s) in {what} also appear in the named run(s), or could not be checked." if hits else
          f"OK: no committed number of {MIN_DIGITS}+ significant digits in {what} appears in the {len(runs)} run(s) "
          f"({len(allow)} reviewed chance match(es) allowed by {ALLOW}).")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
