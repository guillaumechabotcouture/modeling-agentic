#!/usr/bin/env python3
"""Run one `codex exec` review, read-only, and fail loudly when it hangs (dev tool).

Agents have started `codex exec` reviews that sat for an hour with nothing written: stdin left open, so codex
waited for input, or a session that never started. Here stdin is always /dev/null and the sandbox is always
read-only (an extra argument that changes either is refused). Within 90 s a new session file must appear under
~/.codex/sessions/<YYYY>/<MM>/<DD>/ and grow; if not, codex is killed and started once more, and a second
failure exits 1 saying what was seen. The last message goes to --out, everything codex prints to <out>.log.

A pull request review (--pr, or --head with --base) does what builders did by hand: the head commit is exported
with `git archive` to a temporary directory (tracked files only, no .git, nothing from the working tree), the diff
under review is written there as PR.diff, and codex runs in that directory with --skip-git-repo-check. The export
is removed afterwards.

  the diff     `git diff <base>...<head>`: the PR's own change, whatever the base did since.
  --since SHA  narrow the review to the commits after SHA, a commit on the PR's own (first-parent) line that an
               earlier round reviewed: PR.diff is then `git diff SHA <head>` over the files the PR changes, so a
               merge of the base in between adds nothing but its changes to those same files.
  rounds       each review's last message is <out-dir>/round-<n>.md (default out dir: <git dir>/codex_reviews/
               <pr-N or head>, shared by all worktrees, never tracked). A round past --max-rounds (default 3) is
               refused before codex starts; a review that hung or wrote no last message is not a round.

    python3 scripts/codex_review.py --pr 190 [--since SHA] [--prompt-file EXTRA.md] [--max-rounds N] [--out-dir D]
    python3 scripts/codex_review.py --head REF --base REF [...]
    python3 scripts/codex_review.py --prompt-file PROMPT.md --out review.md [--cd DIR] [-- <extra codex exec args>]
    python3 scripts/codex_review.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

SESSIONS = os.path.expanduser("~/.codex/sessions")
WINDOW_S = 90.0
MAX_ROUNDS = 3
PREAMBLE = """You are reviewing a pull request, read-only. The current directory is a clean export of the head \
commit {head} (tracked files only, no .git). The file PR.diff holds the changes under review: {scope}. Review only \
those changes; read any other file for context. Do not modify files and do not use the network.

Report each finding as P1 (a wrong result, lost data, a security problem or a broken contract: fix before merge), \
P2 or P3, with file:line, what fails and a concrete reproduction. Do not report style. End with one line: \
"P1: <n>  P2: <n>  P3: <n>".
"""
REFUSED = ("-s", "--sandbox", "--dangerously-bypass-approvals-and-sandbox", "--full-auto", "--approve-for-me",
           "--ephemeral", "--add-dir")


def codex_argv(codex, prompt, out, cd, extra=()) -> list:
    """`codex exec`, always in the read-only sandbox; an extra argument that touches the sandbox, approvals or the
    session file is refused."""
    bad = [x for x in extra if x.split("=")[0] in REFUSED or any(w in x for w in ("sandbox", "approval", "bypass"))]
    if bad:
        raise ValueError(f"refused codex arguments {bad}: the review always runs read-only with a session file")
    return [codex, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-C", cd, "-o", out, *extra, prompt]


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


def _out(argv, **kw) -> str:
    """The command's output; a failure exits (SystemExit) with what the command said."""
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    if r.returncode:
        raise SystemExit(f"{' '.join(argv[:4])} ... exited {r.returncode}: {r.stderr.strip()[-400:]}")
    return r.stdout


def pr_refs(pr, repo, run=_out) -> tuple:
    """(head commit, base commit) of pull request `pr`, from gh, both fetched from origin."""
    doc = json.loads(run(["gh", "pr", "view", str(pr), "--json", "headRefOid,baseRefOid"], cwd=repo))
    head, base = doc["headRefOid"], doc["baseRefOid"]
    run(["git", "-C", repo, "fetch", "-q", "origin", head, base])
    return head, base


def pr_diff(repo, base, head, since=None) -> tuple:
    """(diff text, a sentence saying what it covers). `head`, `base` and `since` are commit ids."""
    def g(*a):
        return _out(["git", "-C", repo, *a])
    if not since:
        return g("diff", f"{base}...{head}"), f"`git diff {base[:12]}...{head[:12]}`, the pull request's own change"
    if since not in g("rev-list", "--first-parent", head).split():
        raise SystemExit(f"--since {since[:12]} is not an ancestor of the head on the pull request's own "
                         "(first-parent) line; name a commit of this branch that an earlier round reviewed")
    paths = g("diff", "--name-only", "-z", f"{base}...{head}").split("\0")[:-1]
    if not paths:
        raise SystemExit(f"{base[:12]}...{head[:12]} changes no file: nothing to review")
    log = g("log", "--first-parent", "--format=%h %s", f"{since}..{head}").strip()
    return g("diff", since, head, "--", *paths), (
        f"`git diff {since[:12]} {head[:12]}` over the files this pull request changes. An earlier round reviewed "
        f"the pull request up to {since[:12]}; this round covers only the commits after it:\n{log}\n")


def export(repo, sha, dest) -> None:
    """The tracked files of commit `sha`, unpacked into `dest` (git archive: no .git, nothing untracked)."""
    ar = subprocess.Popen(["git", "-C", repo, "archive", "--format=tar", sha], stdout=subprocess.PIPE)
    rc = subprocess.call(["tar", "-x", "-f", "-", "-C", dest], stdin=ar.stdout)
    ar.stdout.close()
    if ar.wait() or rc:
        raise SystemExit(f"git archive {sha[:12]} | tar -x failed (git {ar.returncode}, tar {rc})")


def next_round(out_dir, cap=MAX_ROUNDS) -> tuple:
    """(n, <out_dir>/round-<n>.md) for the next review; past the cap it exits (SystemExit). The rounds done are the
    round-<n>.md files: codex writes one only when a review finished."""
    done = [int(m.group(1)) for f in (os.listdir(out_dir) if os.path.isdir(out_dir) else ())
            for m in [re.fullmatch(r"round-(\d+)\.md", f)] if m]
    n = max(done, default=0) + 1
    if n > cap:
        raise SystemExit(f"review round {n} refused: {len(done)} round file(s) in {out_dir} and the cap is {cap} "
                         "(--max-rounds). Fix what the last round found, or narrow the claim in the PR body.")
    return n, os.path.join(out_dir, f"round-{n}.md")


def review_pr(repo, head, base, out_dir, prompt="", since=None, cap=MAX_ROUNDS, extra=(), codex="codex",
              root=SESSIONS, window_s=WINDOW_S, poll_s=5.0) -> int:
    """One review round of `head` against `base` in a clean export; refusals (an unknown commit, a --since outside
    the PR's line, a round past the cap) come before codex starts."""
    def sha(x):
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", x + "^{commit}"],
                           stdout=subprocess.PIPE, text=True)
        if r.returncode:
            raise SystemExit(f"{x!r} is not a commit in {repo}: nothing reviewed")
        return r.stdout.strip()
    head, base, since = (sha(x) if x else None for x in (head, base, since))
    diff, scope = pr_diff(repo, base, head, since)
    n, out = next_round(out_dir, cap)
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex_review_") as tmp:
        export(repo, head, tmp)
        with open(os.path.join(tmp, "PR.diff"), "w") as f:
            f.write(diff)
        print(f"round {n} of at most {cap}: head {head[:12]}, {len(diff.splitlines())} diff lines, export {tmp}",
              flush=True)
        return review(PREAMBLE.format(head=head, scope=scope) + (f"\n{prompt}" if prompt else ""), out, tmp, extra,
                      codex, root, window_s, poll_s)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--self-test"]:
        return _self_test()
    own, extra = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--prompt-file", help="the prompt; in a PR review, text added after the standard prompt")
    ap.add_argument("--out", help="the last message; codex's output goes to <out>.log (not for a PR review)")
    ap.add_argument("--cd", default=".", help="the directory codex reviews (read-only; not for a PR review)")
    ap.add_argument("--pr", type=int, help="review this pull request's head against its base (asks gh, fetches)")
    ap.add_argument("--head", help="review this commit or ref (with --base), without gh")
    ap.add_argument("--base", help="the base commit or ref for --head")
    ap.add_argument("--since", help="narrow the review to the commits after this one (already reviewed)")
    ap.add_argument("--max-rounds", type=int, default=MAX_ROUNDS, help=f"the round cap (default {MAX_ROUNDS})")
    ap.add_argument("--out-dir", help="where round-<n>.md goes (default <git dir>/codex_reviews/<pr-N or head>)")
    ap.add_argument("--repo", default=".", help="the repository (default: the current directory)")
    a = ap.parse_args(own)
    prompt = open(a.prompt_file).read() if a.prompt_file else ""
    if a.pr is None and not a.head:
        if not (a.prompt_file and a.out):
            ap.error("give --pr N, or --head REF --base REF, or --prompt-file with --out")
        return review(prompt, a.out, a.cd, extra)
    if a.out or (a.pr is not None and (a.head or a.base)) or (a.head and not a.base):
        ap.error("a PR review takes --pr N alone, or --head with --base; its output goes to --out-dir")
    head, base = pr_refs(a.pr, a.repo) if a.pr is not None else (a.head, a.base)
    label = f"pr-{a.pr}" if a.pr is not None else re.sub(r"[^A-Za-z0-9._-]+", "_", a.head)
    common = os.path.join(a.repo, _out(["git", "-C", a.repo, "rev-parse", "--git-common-dir"]).strip())
    out_dir = a.out_dir or os.path.join(os.path.abspath(common), "codex_reviews", label)
    return review_pr(a.repo, head, base, out_dir, prompt, a.since, a.max_rounds, extra)


_FAKE = r'''
import os, sys, time
mode, root = os.environ["FAKE_MODE"], os.environ["FAKE_ROOT"]
n = int(open(os.environ["FAKE_COUNT"]).read() or 0) + 1 if os.path.exists(os.environ["FAKE_COUNT"]) else 1
open(os.environ["FAKE_COUNT"], "w").write(str(n))
dn, s0 = os.stat("/dev/null"), os.fstat(0)
cd = sys.argv[sys.argv.index("-C") + 1]
diff = os.path.join(cd, "PR.diff")
seen = {"files": sorted(os.listdir(cd)), "diff": open(diff).read() if os.path.exists(diff) else None}
open(os.environ["FAKE_ARGS"], "a").write(repr((sys.argv[1:], s0.st_rdev == dn.st_rdev, seen)) + "\n")
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
            a0, stdin_null, _ = args[0]
            ok(rc == 0 and err is None and n == 1 and open(out).read() == "review ok" and os.path.exists(out + ".log"),
               f"CR1 a session that starts and grows runs to the end once: rc={rc} n={n} err={err}")
            ok(stdin_null and a0[:4] == ["exec", "--sandbox", "read-only", "--skip-git-repo-check"]
               and a0[-1] == "review this",
               f"CR2 codex gets stdin from /dev/null, the read-only sandbox and --skip-git-repo-check, always: "
               f"{a0[:4]} stdin_null={stdin_null}")
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

        # A toy repository: main (c0, then cm on other.py) and a PR branch (c1 a.py, c2 b.py, a merge of main, c3 b.py).
        repo = os.path.join(d, "repo")
        os.makedirs(repo)

        def g(*a):
            return subprocess.check_output(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.org",
                                            "-c", "commit.gpgsign=false", *a], text=True,
                                           stderr=subprocess.STDOUT).strip()

        def commit(msg, **files):
            for name, text in files.items():
                open(os.path.join(repo, name), "w").write(text)
            g("add", "-A")
            g("commit", "-q", "-m", msg)
            return g("rev-parse", "HEAD")

        def toy():
            g("init", "-q", "-b", "main")
            commit("c0", **{"a.py": "a = 1\n", "other.py": "o = 1\n"})
            g("checkout", "-q", "-b", "pr")
            commit("c1 first", **{"a.py": "a = 2\n"})
            c2 = commit("c2 second", **{"b.py": "b = 1\n"})
            g("checkout", "-q", "main")
            commit("cm base moves", **{"other.py": "o = 2\n"})
            g("checkout", "-q", "pr")
            g("merge", "-q", "--no-ff", "-m", "merge main", "main")
            c3 = commit("c3 third", **{"b.py": "b = 3\n"})
            open(os.path.join(repo, "untracked.txt"), "w").write("not in the commit")
            return c2, c3
        c2 = c3 = None
        try:
            c2, c3 = toy()
        except BaseException as e:  # noqa: BLE001
            ok(False, f"CR6-15 the toy repository could not be built ({type(e).__name__}: {e})")

        def go_pr(tag, out_dir, mode="ok", **kw):
            env = {"FAKE_MODE": mode, "FAKE_ROOT": root, "FAKE_COUNT": os.path.join(d, f"n_{tag}"),
                   "FAKE_ARGS": os.path.join(d, f"a_{tag}")}
            os.environ.update(env)
            rc, err, buf = None, None, io.StringIO()
            with contextlib.redirect_stdout(buf):
                try:
                    rc = review_pr(repo, "pr", "main", out_dir, codex=fake, root=root, window_s=3.0, poll_s=0.2, **kw)
                except SystemExit as e:
                    err = str(e.code)
            n = int(open(env["FAKE_COUNT"]).read()) if os.path.exists(env["FAKE_COUNT"]) else 0
            args = ([eval(x) for x in open(env["FAKE_ARGS"]).read().splitlines()]  # noqa: S307 -- our own fake
                    if os.path.exists(env["FAKE_ARGS"]) else [])
            return rc, err, n, args, buf.getvalue()

        def cr6():
            out_dir = os.path.join(d, "rounds_a")
            rc, err, n, args, said = go_pr("pr1", out_dir, prompt="look at the units")
            a0, stdin_null, seen = args[0]
            cd = a0[a0.index("-C") + 1]
            ok(rc == 0 and err is None and seen["files"] == ["PR.diff", "a.py", "b.py", "other.py"]
               and os.path.realpath(cd) != os.path.realpath(repo),
               f"CR6 codex runs in a clean export of the head commit: tracked files and PR.diff, no .git, nothing "
               f"untracked: rc={rc} err={err} files={seen['files']}")
            df = seen["diff"] or ""
            ok("+a = 2" in df and "+b = 3" in df and "other.py" not in df,
               "CR7 PR.diff is the PR's own change (base...head): the base's later commits are not in it")
            ok(stdin_null and "--skip-git-repo-check" in a0 and a0[1:3] == ["--sandbox", "read-only"]
               and not os.path.exists(cd),
               f"CR8 the PR review runs read-only with --skip-git-repo-check and stdin from /dev/null, and the export "
               f"is removed afterwards: stdin_null={stdin_null}")
            ok(c3 in a0[-1] and "PR.diff" in a0[-1] and a0[-1].rstrip().endswith("look at the units")
               and open(os.path.join(out_dir, "round-1.md")).read() == "review ok",
               "CR9 the prompt names the head commit and PR.diff, then the builder's own prompt; the last message is "
               "round-1.md")
        case("CR6-9", cr6)

        def cr10():
            rc, err, n, args, said = go_pr("pr2", os.path.join(d, "rounds_b"), since=c2)
            a0, _, seen = args[0]
            df = seen["diff"] or ""
            ok(rc == 0 and "+b = 3" in df and "-b = 1" in df and "a.py" not in df and "other.py" not in df,
               f"CR10 narrowed to the commits since a reviewed one, PR.diff holds only the PR's files' changes since "
               f"it (not earlier commits, not the merged base): rc={rc} err={err}")
            ok("c3 third" in a0[-1] and "c1 first" not in a0[-1] and c2[:12] in a0[-1],
               "CR11 the narrowed prompt lists the commits under review and the commit already reviewed")
            rc, err, n, args, said = go_pr("pr3", os.path.join(d, "rounds_c"), since=g("rev-parse", "main"))
            ok(rc is None and err and "ancestor" in err and n == 0,
               f"CR12 a narrowing commit that the PR did not have before the base moved is refused before codex "
               f"starts: {err}")
            rc, err, n, args, said = go_pr("pr4", os.path.join(d, "rounds_d"), since="nosuchref")
            ok(rc is None and err and n == 0, f"CR12b an unknown commit is refused before codex starts: {err}")
        case("CR10-12", cr10)

        def cr13():
            out_dir = os.path.join(d, "rounds_e")
            r = [go_pr(f"cap{i}", out_dir, cap=2) for i in (1, 2, 3)]
            ok([x[0] for x in r] == [0, 0, None] and r[2][2] == 0 and "round" in (r[2][1] or "")
               and sorted(os.listdir(out_dir)) == ["round-1.md", "round-1.md.log", "round-2.md", "round-2.md.log"],
               f"CR13 rounds are counted from the round files; past the cap the review is refused before codex "
               f"starts: {[x[0] for x in r]} {r[2][1]}")
            rc, err, n, args, said = go_pr("hung", os.path.join(d, "rounds_f"), mode="hang", cap=1)
            ok(bool(err) and "hung" in err and next_round(os.path.join(d, "rounds_f"), 1)[0] == 1,
               f"CR14 a review that hung wrote no round file and does not count as a round: {err}")
        case("CR13-14", cr13)

        def cr15():
            calls = []

            def run(argv, **kw):
                calls.append(list(argv))
                return '{"headRefOid": "h", "baseRefOid": "b"}' if argv[0] == "gh" else ""
            got = pr_refs(190, repo, run=run)
            ok(got == ("h", "b") and calls[0][:4] == ["gh", "pr", "view", "190"]
               and any(c[0] == "git" and "fetch" in c and "h" in c and "b" in c for c in calls[1:]),
               f"CR15 --pr asks gh for the head and base commits and fetches both (a fake runner; no network): {calls}")
        case("CR15", cr15)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
