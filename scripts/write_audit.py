#!/usr/bin/env python3
"""Every file write, rename and removal in the v2 harness's run-dir code is safe or reviewed (review round 4, P1-2).

    python3 scripts/write_audit.py              # list the write sites and their status
    python3 scripts/write_audit.py --self-test

Trust model: agent-written artifacts in a run dir may be hostile (links, planted `.tmp` names, paths in records).
The harness writes in a run dir through scripts/lib/safe_io.py (`write_nofollow`, `append_nofollow`,
`remove_nofollow`: no link followed at any component, random temporary names, atomic publish). This audit scans the
v2 harness modules (HARNESS) for the calls that write, rename or remove a file by path: write-mode `open(`, `os.open(`
with a write flag, `shutil.copy*` / `shutil.move` / `shutil.rmtree`, `os.replace` / `os.rename`, `os.remove` /
`os.unlink`, `.write_text(` / `.write_bytes(`. Self-test functions (`_self_test*`) are skipped: they write their own
temporary fixtures. Every remaining site must be listed in REVIEWED with the reason it does not write inside a run dir
(or is safe there); exact counts per file, so a new plain write in the harness fails CI.
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The v2 harness: the code that writes inside a run directory a run's agents can also write to.
HARNESS = (
    "agents/stage_machine.py", "agents/findings_ledger.py", "agents/fix_loop.py", "agents/analysis_backend.py",
    "agents/v2_sdk_tasks.py", "agents/v2_tasks.py",
    "scripts/pipeline.py", "scripts/compute.py", "scripts/scope_signoff.py", "scripts/decision_brief.py",
    "toolkit/spec/signoff.py", "toolkit/spec/schema.py", "toolkit/analysis/artifacts.py", "toolkit/analysis/run.py",
    "toolkit/analysis/batch.py", "tools/run_console/run_console.py",
)
PATTERNS = {
    "open-write": re.compile(r"""(?<![\w.])open\(.*?,\s*(?:mode\s*=\s*)?["'][rwab+x]*[wax+][rwab+x]*["']"""),
    "os.open-write": re.compile(r"\bos\.open\(.*\bO_(?:WRONLY|RDWR|CREAT|APPEND|TRUNC)"),
    "shutil": re.compile(r"\bshutil\.(?:copy\w*|move|rmtree)\("),
    "rename": re.compile(r"\bos\.(?:replace|rename)\("),
    "remove": re.compile(r"\bos\.(?:remove|unlink)\("),
    "pathlib-write": re.compile(r"\.write_(?:text|bytes)\("),
}
# self-tests and the toy fixtures they build (synthetic run dirs in a temporary directory)
_SELF_TEST = re.compile(r"^(\s*)def (?:_self_test\w*|_toy\w*|toy_\w*|derived_toy_\w*|_\w*fixture\w*|example_\w*)\(")
_DEF = re.compile(r"^(\s*)(?:async\s+)?def \w+\(|^(\s*)class \w+")
_OUTSIDE = "writes outside any run dir (a temporary directory or a file named by the operator)"
_TMPDIR = "inside a temporary work directory the harness created (tempfile), not a run dir"
# {file: {pattern: (count, reason)}}: write sites that do not write inside a run dir, or are safe there.
REVIEWED = {
    "scripts/compute.py": {
        "open-write": (0, ""),
    },
}


def scan_text(text: str) -> dict:
    """{pattern: [line numbers]} outside self-test functions and comments."""
    out: dict = {}
    skip_indent = None
    for n, line in enumerate(text.splitlines(), 1):
        if skip_indent is not None:
            m = _DEF.match(line)
            if line.strip() and (len(line) - len(line.lstrip())) <= skip_indent and not line.lstrip().startswith(
                    (")", "]", "}", "#")) and (m or not line.startswith(" " * (skip_indent + 1))):
                skip_indent = None
            else:
                continue
        m = _SELF_TEST.match(line)
        if m:
            skip_indent = len(m.group(1))
            continue
        code = line.split("#", 1)[0] if '"' not in line and "'" not in line else line
        if line.lstrip().startswith("#"):
            continue
        for name, rx in PATTERNS.items():
            if rx.search(code):
                out.setdefault(name, []).append(n)
    return out


def sites(paths=HARNESS, read=None) -> dict:
    read = read or (lambda p: open(os.path.join(REPO, p), encoding="utf-8").read())
    out = {}
    for p in paths:
        try:
            found = scan_text(read(p))
        except OSError:
            continue
        if found:
            out[p] = found
    return out


def problems(found: dict, reviewed: dict) -> list[str]:
    bad = []
    for p, pats in sorted(found.items()):
        for name, lines in sorted(pats.items()):
            want = (reviewed.get(p) or {}).get(name)
            if want is None:
                bad.append(f"{p}: {name} at lines {lines}: not through lib.safe_io and not reviewed")
            elif want[0] != len(lines):
                bad.append(f"{p}: {name}: {len(lines)} site(s) at lines {lines}, {want[0]} reviewed")
    for p, pats in sorted(reviewed.items()):
        for name, (n, _) in pats.items():
            if n and not (found.get(p) or {}).get(name):
                bad.append(f"{p}: {name}: reviewed {n} site(s) but none found (stale entry)")
    return bad


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    t = ("def f(p):\n    with open(p + '.tmp', 'w') as f:\n        f.write('x')\n    os.replace(p + '.tmp', p)\n"
         "def g(p):\n    open(p).read()\n    open(p, 'rb').read()\n"
         "def _self_test():\n    open('x', 'w')\n    os.remove('x')\n"
         "def h(p):\n    os.remove(p)\n")
    s = scan_text(t)
    ok(s == {"open-write": [2], "rename": [4], "remove": [12]},
       f"WA1 write, rename and remove sites are found; reads and self-test code are not ({s})")
    ok(problems({"a.py": s}, {"a.py": {"open-write": (1, "r"), "rename": (1, "r")}})
       == ["a.py: remove at lines [12]: not through lib.safe_io and not reviewed"], "WA2 an unreviewed site is a problem")
    live = problems(sites(), REVIEWED)
    ok(not live, "WA3 every write in the v2 harness's run-dir code is through lib.safe_io or reviewed:\n  "
       + "\n  ".join(live))
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    for p, pats in sorted(sites().items()):
        for name, lines in sorted(pats.items()):
            r = (REVIEWED.get(p) or {}).get(name)
            print(f"{p}: {name} {lines}: {r[1] if r else 'NOT REVIEWED'}")
    bad = problems(sites(), REVIEWED)
    for b in bad:
        print("PROBLEM", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
