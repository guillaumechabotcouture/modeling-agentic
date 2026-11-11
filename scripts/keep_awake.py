#!/usr/bin/env python3
"""Run a command with the Mac kept awake (`caffeinate -i`), and return its exit code (dev tool).

A laptop that sleeps drops a watcher's connection and can leave a paid cluster unwatched. On macOS the command
runs under `caffeinate -i` (no idle sleep while it runs); elsewhere, or without caffeinate, it runs as is.

    python3 scripts/keep_awake.py -- <command ...>
    python3 scripts/keep_awake.py --self-test
"""
from __future__ import annotations

import shutil
import subprocess
import sys


def wrap(cmd, platform=None, which=shutil.which) -> list:
    """The command, under `caffeinate -i` on macOS when caffeinate is there."""
    caf = which("caffeinate") if (platform or sys.platform) == "darwin" else None
    return [caf, "-i", *cmd] if caf else list(cmd)


def run(cmd, platform=None, which=shutil.which) -> int:
    return subprocess.call(wrap(cmd, platform, which))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--self-test"]:
        return _self_test()
    cmd = argv[1:] if argv[:1] == ["--"] else argv
    if not cmd:
        print(__doc__)
        return 2
    return run(cmd)


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def case(label, fn):
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 -- a missing function is a failing case, not a crash
            ok(False, f"{label} ({type(e).__name__}: {e})")

    cmd = [sys.executable, "-c", "raise SystemExit(3)"]

    def ka1():
        ok(wrap(cmd, "linux") == cmd and run(cmd, "linux") == 3,
           "KA1 off macOS the command runs as is, and its exit code is returned")
        ok(wrap(cmd, "darwin", which=lambda n: "/usr/bin/caffeinate") == ["/usr/bin/caffeinate", "-i", *cmd]
           and wrap(cmd, "darwin", which=lambda n: None) == cmd,
           "KA2 on macOS it runs under caffeinate -i; without caffeinate, as is")
        ok(main(["--", *cmd]) == 3 and main([]) == 2, "KA3 the CLI returns the command's exit code; no command is 2")
    case("KA1-3", ka1)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
