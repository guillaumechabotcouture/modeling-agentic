#!/usr/bin/env python3
"""Run one `codex exec` review, read-only, and fail loudly when it hangs (dev tool).

Agents have started `codex exec` reviews that sat for an hour with nothing written: stdin left open, so codex
waited for input, or a session that never started. Here stdin is always /dev/null and the sandbox is always
read-only (an extra argument that changes either is refused). Within 90 s a new session file must appear under
~/.codex/sessions/<YYYY>/<MM>/<DD>/ and grow; if not, codex is killed and started once more, and a second
failure exits 1 saying what was seen. The last message goes to --out, everything codex prints to <out>.log.

    python3 scripts/codex_review.py --prompt-file PROMPT.md --out review.md [--cd DIR] [-- <extra codex exec args>]
    python3 scripts/codex_review.py --self-test
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

SESSIONS = os.path.expanduser("~/.codex/sessions")
WINDOW_S = 90.0
REFUSED = ("-s", "--sandbox", "--dangerously-bypass-approvals-and-sandbox", "--full-auto", "--approve-for-me",
           "--ephemeral", "--add-dir")


def codex_argv(codex, prompt, out, cd, extra=()) -> list:
    raise NotImplementedError


def sessions(root, clock=time.time) -> dict:
    raise NotImplementedError


def run_once(argv, log_path, root=SESSIONS, window_s=WINDOW_S, poll_s=5.0, clock=time.time):
    raise NotImplementedError


def review(prompt, out, cd=".", extra=(), codex="codex", root=SESSIONS, window_s=WINDOW_S, poll_s=5.0) -> int:
    raise NotImplementedError


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--self-test"]:
        return _self_test()
    raise NotImplementedError


_FAKE = r'''
import os, sys, time
mode, root = os.environ["FAKE_MODE"], os.environ["FAKE_ROOT"]
n = int(open(os.environ["FAKE_COUNT"]).read() or 0) + 1 if os.path.exists(os.environ["FAKE_COUNT"]) else 1
open(os.environ["FAKE_COUNT"], "w").write(str(n))
dn, s0 = os.stat("/dev/null"), os.fstat(0)
open(os.environ["FAKE_ARGS"], "a").write(repr((sys.argv[1:], s0.st_rdev == dn.st_rdev)) + "\n")
if mode == "hang" or (mode == "hang-once" and n == 1):
    time.sleep(60)
d = os.path.join(root, time.strftime("%Y/%m/%d"))
os.makedirs(d, exist_ok=True)
f = os.path.join(d, f"rollout-{n}.jsonl")
for i in range(4):
    open(f, "a").write("{}\n" * (i + 1))
    time.sleep(0.3)
open(sys.argv[sys.argv.index("-o") + 1], "w").write("review ok")
'''


def _self_test() -> int:
    import contextlib
    import io
    import tempfile
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

    with tempfile.TemporaryDirectory() as d:
        fake = os.path.join(d, "codex")
        open(fake, "w").write(f"#!{sys.executable}\n{_FAKE}")
        os.chmod(fake, 0o755)
        root, prompt = os.path.join(d, "sessions"), os.path.join(d, "p.md")
        open(prompt, "w").write("review this")

        def go(mode, tag):
            env = {"FAKE_MODE": mode, "FAKE_ROOT": root, "FAKE_COUNT": os.path.join(d, f"n_{tag}"),
                   "FAKE_ARGS": os.path.join(d, f"a_{tag}")}
            out = os.path.join(d, f"{tag}.md")
            os.environ.update(env)
            t0, err, rc = time.time(), None, None
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    rc = review(open(prompt).read(), out, cd=d, codex=fake, root=root, window_s=3.0, poll_s=0.2)
                except SystemExit as e:
                    err = str(e.code)
            n = int(open(env["FAKE_COUNT"]).read())
            args = [eval(x) for x in open(env["FAKE_ARGS"]).read().splitlines()]  # noqa: S307 -- our own fake
            return rc, err, n, args, out, time.time() - t0

        def cr1():
            rc, err, n, args, out, dt = go("ok", "ok")
            a0, stdin_null = args[0]
            ok(rc == 0 and err is None and n == 1 and open(out).read() == "review ok" and os.path.exists(out + ".log"),
               f"CR1 a session that starts and grows runs to the end once: rc={rc} n={n} err={err}")
            ok(stdin_null and a0[:3] == ["exec", "--sandbox", "read-only"] and a0[-1] == "review this",
               f"CR2 codex gets stdin from /dev/null and the read-only sandbox, always: {a0[:3]} stdin_null={stdin_null}")
            rc, err, n, args, out, dt = go("hang-once", "h1")
            ok(rc == 0 and n == 2 and dt < 20, f"CR3 a hung first attempt is killed and retried once: rc={rc} n={n}")
            rc, err, n, args, out, dt = go("hang", "h2")
            ok(rc is None and err and "hung" in err and n == 2 and dt < 20,
               f"CR4 a second hang fails loudly after two attempts (no third): err={err} n={n} {dt:.1f}s")
            for bad in (["-s", "workspace-write"], ["--dangerously-bypass-approvals-and-sandbox"], ["--ephemeral"]):
                try:
                    codex_argv("codex", "p", "o", ".", bad)
                    ok(False, f"CR5 an extra argument that changes the sandbox or the session file is refused: {bad}")
                except ValueError:
                    ok(True, f"CR5 an extra argument that changes the sandbox or the session file is refused: {bad}")
        case("CR1-5", cr1)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
