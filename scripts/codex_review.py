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
    """`codex exec`, always in the read-only sandbox; an extra argument that touches the sandbox, approvals or the
    session file is refused."""
    bad = [x for x in extra if x.split("=")[0] in REFUSED or any(w in x for w in ("sandbox", "approval", "bypass"))]
    if bad:
        raise ValueError(f"refused codex arguments {bad}: the review always runs read-only with a session file")
    return [codex, "exec", "--sandbox", "read-only", "-C", cd, "-o", out, *extra, prompt]


def sessions(root, clock=time.time) -> dict:
    """{path: size} of the session files under root/<YYYY>/<MM>/<DD>/ for today and yesterday (local dates)."""
    out = {}
    for t in (clock(), clock() - 86400):
        d = os.path.join(root, time.strftime("%Y/%m/%d", time.localtime(t)))
        for f in (os.listdir(d) if os.path.isdir(d) else ()):
            p = os.path.join(d, f)
            if os.path.isfile(p):
                out[p] = os.path.getsize(p)
    return out


def run_once(argv, log_path, root=SESSIONS, window_s=WINDOW_S, poll_s=5.0, clock=time.time):
    """(exit code, "") once codex is seen alive (a new session file grew) and has finished, or exited by itself;
    (None, why) when it was killed for showing no life within `window_s`."""
    before = sessions(root, clock)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    deadline, prev, alive = clock() + window_s, {}, False
    while clock() < deadline and proc.poll() is None:
        new = {p: n for p, n in sessions(root, clock).items() if p not in before}
        if any(p in prev and n > prev[p] for p, n in new.items()):
            alive = True
            break
        prev = new
        time.sleep(poll_s)
    if not alive and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return None, (f"no new session file under {root} grew within {window_s:g} s"
                      if not prev else f"a new session file appeared but did not grow within {window_s:g} s")
    return proc.wait(), ""


def review(prompt, out, cd=".", extra=(), codex="codex", root=SESSIONS, window_s=WINDOW_S, poll_s=5.0) -> int:
    """Run the review; a hung attempt is killed and retried once, and a second hang exits (SystemExit)."""
    argv, log = codex_argv(codex, prompt, out, cd, extra), out + ".log"
    why = ""
    for attempt in (1, 2):
        rc, why = run_once(argv, log, root, window_s, poll_s)
        if rc is not None:
            print(f"codex exited {rc}: last message in {out}, log in {log}")
            return rc
        print(f"attempt {attempt}: codex showed no life ({why}); killed", flush=True)
    raise SystemExit(f"codex exec hung twice ({why}); nothing reviewed. Log: {log}")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--self-test"]:
        return _self_test()
    own, extra = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--out", required=True, help="the last message; codex's output goes to <out>.log")
    ap.add_argument("--cd", default=".", help="the directory codex reviews (read-only)")
    a = ap.parse_args(own)
    return review(open(a.prompt_file).read(), a.out, a.cd, extra)


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
