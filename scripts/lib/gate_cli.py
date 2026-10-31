"""Shared command line for check modules that expose `gate_findings(run_dir)`.

    python3 scripts/<check>.py <run_dir> [--round N]   # print the findings
    python3 scripts/<check>.py --self-test

Phase 48 α: the same argparse block was copied into several check modules
(benchmark_transfer, comparator_efficiency, uq_ci_quality, benchmark_gate,
mechanism_manifest, calibration_spec). Each keeps its own `main(argv=None)`,
which calls `run(...)` with its docstring, its functions and its exit rule;
the output and exit codes are unchanged.

    python3 scripts/lib/gate_cli.py --self-test
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional

_NO_ROUND = object()


def any_finding(findings: list) -> bool:
    return bool(findings)


def any_high(findings: list) -> bool:
    return any(x["severity"] == "HIGH" for x in findings)


def run(argv, doc: Optional[str], gate_findings: Callable, self_test: Callable[[], int], *,
        fails: Callable[[list], bool] = any_finding, round_default=_NO_ROUND,
        preamble: Optional[Callable[[str], None]] = None) -> int:
    """Parse `run_dir [--round N] [--self-test]`, print each finding as
    `[SEVERITY] kind: claim` (claim cut at 300 characters), and return 1
    when `fails(findings)`, else 0. `--round` exists only when
    `round_default` is given; `gate_findings` is then called with it.
    `preamble(run_dir)` prints a summary line before the findings."""
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    with_round = round_default is not _NO_ROUND
    if with_round:
        ap.add_argument("--round", type=int, default=round_default)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    if preamble is not None:
        preamble(a.run_dir)
    v = gate_findings(a.run_dir, a.round) if with_round else gate_findings(a.run_dir)
    for x in v:
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 1 if fails(v) else 0


def _self_test() -> int:
    import contextlib
    import io
    fails = []

    def ok(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            fails.append(label)

    calls = []

    def gf(rd, *rest):
        calls.append((rd, rest))
        return [{"severity": "MEDIUM", "kind": "k", "claim": "x" * 400}]

    def gf_high(rd, *rest):
        return [{"severity": "HIGH", "kind": "h", "claim": "c"}]

    def st():
        return 7

    # G1: --self-test runs the module's cases and returns their code.
    ok(run(["--self-test"], None, gf, st) == 7, "G1 --self-test returns the module's code")
    # G2: findings printed, claim cut at 300 characters; any finding fails by default.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run(["/r"], None, gf, st)
    ok(rc == 1 and buf.getvalue() == "[MEDIUM] k: " + "x" * 300 + "\n" and calls[-1] == ("/r", ()),
       "G2 findings printed and any finding fails")
    # G3: any_high: a MEDIUM passes, a HIGH fails.
    with contextlib.redirect_stdout(io.StringIO()):
        rc_medium = run(["/r"], None, gf, st, fails=any_high)
        rc_high = run(["/r"], None, gf_high, st, fails=any_high)
    ok(rc_medium == 0, "G3a MEDIUM only passes under any_high")
    ok(rc_high == 1, "G3b HIGH fails under any_high")
    # G4: --round passed through with its default; absent when no default given.
    seen = []
    with contextlib.redirect_stdout(io.StringIO()):
        for args, default in ((["/r"], 2), (["/r", "--round", "5"], None), (["/r"], None)):
            run(args, None, gf, st, round_default=default)
            seen.append(calls[-1])
    ok(seen[0] == ("/r", (2,)), "G4a round default passed")
    ok(seen[1] == ("/r", (5,)), "G4b --round passed")
    ok(seen[2] == ("/r", (None,)), "G4c None default passed")
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            run(["/r", "--round", "3"], None, gf, st)
            ok(False, "G4d --round refused without a default")
        except SystemExit as e:
            ok(e.code == 2, "G4d --round refused without a default")
        # G5: no run_dir is a usage error.
        try:
            run([], None, gf, st)
            ok(False, "G5 run_dir required")
        except SystemExit as e:
            ok(e.code == 2, "G5 run_dir required")
    # G6: the preamble prints before the findings.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run(["/r"], None, gf_high, st, preamble=lambda rd: print(f"pre {rd}"))
    ok(buf.getvalue() == "pre /r\n[HIGH] h: c\n", "G6 preamble first")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
