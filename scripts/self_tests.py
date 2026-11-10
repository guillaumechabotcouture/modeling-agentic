#!/usr/bin/env python3
"""Find and run every module's --self-test (one discovery rule for CLAUDE.md and CI).

Scope: `main.py`, every tracked `.py` file under `scripts/`, `toolkit/`, `cloud/` and `tools/`, and the
harness modules under `agents/` named in AGENT_MODULES (other `agents/` files only mention
--self-test in text, or run a live preflight). `__init__.py` files are skipped.

A file in scope that mentions `--self-test` is run as `python <file> --self-test` from the
repository root. A file in scope that does not is a discovery problem, and the run fails,
unless it is listed in EXEMPT with the reason it has none. So a new module under `scripts/`,
`toolkit/` or `cloud/` cannot be skipped silently: it gets a self-test or an exemption.

    python3 scripts/self_tests.py              # run them all (exit 1 on any failure)
    python3 scripts/self_tests.py --list       # print what would run, and the exemptions
    python3 scripts/self_tests.py --jobs 2     # run two at a time
    python3 scripts/self_tests.py --per-test-timeout 1800  # kill (and fail) a self-test after 30 min (default)
    python3 scripts/self_tests.py --shard 2/4  # only shard 2 of 4 (CI runs the 4 shards in parallel)
    python3 scripts/self_tests.py --self-test  # test the discovery rule itself
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOPE_DIRS = ("scripts/", "toolkit/", "cloud/", "tools/")   # tools/: outside compute.code_fingerprint
AGENT_MODULES = (
    "agents/stage_machine.py", "agents/findings_ledger.py", "agents/fix_loop.py",
    "agents/analysis_backend.py", "agents/v2_sdk_tasks.py", "agents/output_caps.py",
)
# agents/integrity.py imports the agents package, so main.py's self-test runs its cases (IN12).
# Files (or directory prefixes ending in "/") in scope that have no --self-test, and why.
EXEMPT = {
    "cloud/configs/": "launch configs (settings, no logic), read by cloud/run_cluster.py",
    "cloud/worker_side.py": "runs on the cloud workers; exercised by the cloud/run_cluster.py, "
                            "cloud/vm_job.py and scripts/compute.py self-tests",
    "scripts/replay_coiled.py": "live Coiled backend for scripts/replay.py; its scheduler reads are "
                                "tested by scripts/replay.py --self-test (SQ cases)",
    "toolkit/analysis/artifacts.py": "analysis-engine library; exercised by toolkit/analysis/run.py --self-test",
    "toolkit/analysis/claims.py": "analysis-engine library; exercised by toolkit/analysis/run.py --self-test",
    "toolkit/analysis/core.py": "analysis-engine library; exercised by toolkit/analysis/run.py --self-test",
    "toolkit/analysis/decision_tests.py": "analysis-engine library; exercised by toolkit/analysis/run.py --self-test",
    "toolkit/analysis/sensitivity.py": "analysis-engine library; exercised by toolkit/analysis/run.py --self-test",
    "toolkit/analysis/stability.py": "analysis-engine library; exercised by toolkit/analysis/run.py --self-test",
    "toolkit/spec/toy_engines.py": "self-test toy engines (refused in production by toolkit/import_guard.py); "
                                   "exercised by toolkit/spec/engine_parameters.py's self-test",
    "toolkit/engines/malaria/model.py": "engine module; exercised by toolkit/engines/malaria/engine.py --self-test",
    "toolkit/engines/compartmental/benchmarks/runtime_spike.py": "one-off timing script for the dev VM",
    "toolkit/engines/renewal/benchmarks/oracles.py": "benchmark-pack oracles and probes; exercised by toolkit/engines/renewal/engine.py --self-test",
    "toolkit/engines/malaria/benchmarks/b15_mechanisms.py": "diagnostic scan script, not a library",
    "toolkit/engines/malaria/benchmarks/runtime.py": "timing and peak-memory measurement script for a VM, not a library",
}
MARKER = "--self-test"


def _exemption(path: str, exempt: dict) -> str | None:
    if path in exempt:
        return exempt[path]
    for k, why in exempt.items():
        if k.endswith("/") and path.startswith(k):
            return why
    return None


def classify(paths, has_marker, agent_modules=AGENT_MODULES, exempt=EXEMPT):
    """Split tracked paths into (run, skipped, notes, problems).

    run: files to run with --self-test; skipped: [(path, reason)] for exempt files;
    notes: stale exemptions (informational); problems: in-scope files with no self-test
    and no exemption, and named agent modules that are missing or have none (these fail)."""
    paths = sorted(set(paths))
    present = set(paths)
    run, skipped, notes, problems = [], [], [], []
    for p in paths:
        if not p.endswith(".py") or os.path.basename(p) == "__init__.py":
            continue
        in_scope = p == "main.py" or p.startswith(SCOPE_DIRS) or p in agent_modules
        if not in_scope:
            continue
        marked = has_marker(p)
        why = _exemption(p, exempt)
        if marked:
            run.append(p)
            if why is not None and p in exempt:
                notes.append(f"{p} is exempt but now has a self-test: remove its EXEMPT entry")
        elif why is not None and p not in agent_modules:
            skipped.append((p, why))
        else:
            problems.append(f"{p} has no --self-test" + ("" if p in agent_modules else
                            " and no EXEMPT entry in scripts/self_tests.py"))
    for m in agent_modules:
        if m not in present:
            problems.append(f"{m} is listed in AGENT_MODULES but is not a tracked file")
    for k in exempt:
        if not k.endswith("/") and k not in present:
            notes.append(f"EXEMPT entry {k} names no tracked file: remove it")
    if "main.py" not in present:
        problems.append("main.py is not a tracked file")
    return run, skipped, notes, problems


def tracked_files(repo: str = REPO) -> list[str]:
    """Tracked files from git; raises when git cannot list them (fail closed)."""
    out = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {out.stderr.strip()}")
    return [p for p in out.stdout.split("\0") if p]


def file_has_marker(repo: str):
    def has(p: str) -> bool:
        try:
            with open(os.path.join(repo, p), encoding="utf-8", errors="replace") as f:
                return MARKER in f.read()
        except OSError:
            return False
    return has


def discover(repo: str = REPO):
    return classify(tracked_files(repo), file_has_marker(repo))


PER_TEST_TIMEOUT_S = 30 * 60     # the watchdog's default: about twice the slowest self-test on a loaded CI runner

# Seconds each slow self-test took on a CI runner (run 42734450748, 2026-11-10; selftest_late.py from #179), for balancing shards only: a stale
# or missing entry changes how evenly the shards finish, never which self-tests run. Others count DEFAULT_WEIGHT_S.
WEIGHTS_S = {
    "agents/stage_machine.py": 511, "toolkit/engines/malaria/engine.py": 345,
    "toolkit/engines/malaria/selftest_late.py": 870, "toolkit/analysis/observed_targets.py": 280,
    "toolkit/analysis/estimates.py": 272, "cloud/vm_job.py": 239, "scripts/compute.py": 237,
    "toolkit/analysis/calibration.py": 182, "toolkit/analysis/run.py": 176,
    "toolkit/calibration/history_matching.py": 81, "toolkit/analysis/joint_calibration.py": 46,
    "toolkit/engines/compartmental/selftest_age.py": 32, "toolkit/spec/engine_parameters.py": 31,
    "toolkit/analysis/figures.py": 25, "scripts/pipeline.py": 25, "scripts/validate_critique_yaml.py": 22,
    "agents/analysis_backend.py": 21, "toolkit/analysis/trajectories.py": 19, "toolkit/analysis/model.py": 15,
}
DEFAULT_WEIGHT_S = 5


def parse_shard(spec: str) -> tuple[int, int]:
    """'K/N' -> (K, N) with 1 <= K <= N; raises ValueError otherwise."""
    try:
        k, n = (int(x) for x in spec.split("/"))
    except ValueError:
        raise ValueError(f"--shard {spec!r}: expected K/N, e.g. 2/4") from None
    if not 1 <= k <= n:
        raise ValueError(f"--shard {spec!r}: need 1 <= K <= N")
    return k, n


def shard(files, k: int, n: int, weights=WEIGHTS_S) -> list[str]:
    """Shard K of N: the longest self-tests are dealt first, each to the shard with the least total weight so far
    (ties to the lowest shard). Every file lands in exactly one shard, and the split depends only on the file list.
    The shard's files come back slowest first, so a long self-test never starts last."""
    w = lambda p: weights.get(p, DEFAULT_WEIGHT_S)  # noqa: E731
    loads, owned = [0.0] * n, [[] for _ in range(n)]
    for p in sorted(set(files), key=lambda p: (-w(p), p)):
        i = min(range(n), key=lambda j: (loads[j], j))
        loads[i] += w(p)
        owned[i].append(p)
    return owned[k - 1]


def _run_one(python: str, repo: str, path: str, timeout_s: float | None = None):
    """(path, exit code or None when timed out, output, seconds). Each self-test runs in its own session; one that
    outlives `timeout_s` is killed with its whole process group (its children too)."""
    t0 = time.time()
    p = subprocess.Popen([python, path, MARKER], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout_s if timeout_s and timeout_s > 0 else None)
        return path, p.returncode, out or "", time.time() - t0
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, _ = p.communicate(timeout=30)    # the pipe closes once the group is gone
        except subprocess.TimeoutExpired:         # a grandchild that left the group still holds it
            p.kill()
            out = ""
        return path, None, (out or "") + f"\n(killed after {timeout_s:.0f}s)", time.time() - t0


def run_all(python: str, jobs: int, repo: str = REPO, per_test_timeout: float | None = PER_TEST_TIMEOUT_S,
            files: list | None = None, shard_kn: tuple[int, int] | None = None) -> int:
    """Run every discovered self-test (or `files`, which skips discovery: the watchdog's own test), or only shard
    `shard_kn` = (K, N) of them. A self-test that runs longer than `per_test_timeout` seconds (0 or None: no limit)
    is killed, named and counted as failed; the others still run. Discovery problems fail every shard."""
    if files is None:
        try:
            run, skipped, notes, problems = discover(repo)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    else:
        run, skipped, notes, problems = list(files), [], [], []
    if shard_kn:
        total = len(run)
        run = shard(run, *shard_kn)
        print(f"shard {shard_kn[0]}/{shard_kn[1]}: {len(run)} of {total} self-tests", flush=True)
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    failed, timed_out = [], []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for path, rc, out, dt in ex.map(lambda f: _run_one(python, repo, f, per_test_timeout), run):
            print(f"::group::{path} ({dt:.0f}s)" if gha else f"== {path} ({dt:.0f}s)", flush=True)
            print(out.rstrip())
            if gha:
                print("::endgroup::")
            if rc is None:
                failed.append(path)
                timed_out.append(path)
                msg = f"self-test timed out after {per_test_timeout:.0f}s; its process group was killed"
                print(f"::error file={path}::{msg}" if gha else f"FAILED: {path} ({msg})", flush=True)
            elif rc != 0:
                failed.append(path)
                print(f"::error file={path}::self-test failed" if gha else f"FAILED: {path} (exit {rc})",
                      flush=True)
    for p in timed_out:
        print(f"TIMED OUT: {p}")
    for n in notes:
        print(f"NOTE: {n}")
    for p in problems:
        print(f"::error file={p.split()[0]}::{p}" if gha else f"DISCOVERY PROBLEM: {p}")
    print(f"\n{len(run) - len(failed)}/{len(run)} self-tests passed ({len(timed_out)} timed out); "
          f"{len(skipped)} exempt; {len(problems)} discovery problem(s).")
    return 1 if failed or problems else 0


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    marked = {"main.py", "scripts/a.py", "scripts/lib/b.py", "toolkit/x/c.py", "agents/m.py",
              "agents/other.py", "spike/s.py", "scripts/stale.py"}
    has = marked.__contains__
    exempt = {"scripts/lib/doc.py": "library", "cloud/configs/": "configs",
              "scripts/stale.py": "old", "scripts/gone.py": "deleted"}
    paths = ["main.py", "scripts/a.py", "scripts/lib/b.py", "scripts/lib/doc.py", "scripts/__init__.py",
             "toolkit/x/c.py", "toolkit/x/new.py", "cloud/configs/e1.py", "cloud/w.py",
             "agents/m.py", "agents/other.py", "agents/prompt.py", "spike/s.py", "scripts/stale.py",
             "scripts/notes.md"]
    run, skipped, notes, problems = classify(paths, has, agent_modules=("agents/m.py", "agents/missing.py"),
                                             exempt=exempt)
    ok(run == ["agents/m.py", "main.py", "scripts/a.py", "scripts/lib/b.py", "scripts/stale.py",
               "toolkit/x/c.py"], f"ST1 the files with a self-test in scope run ({run})")
    ok("agents/other.py" not in run and "spike/s.py" not in run,
       "ST2 agents/ files not named and files out of scope are not run")
    ok(sorted(p for p, _ in skipped) == ["cloud/configs/e1.py", "scripts/lib/doc.py"],
       f"ST3 exempt files (by path and by directory prefix) are skipped with a reason ({skipped})")
    ok(any(p.startswith("toolkit/x/new.py") for p in problems), "ST4 a new module with no self-test fails")
    ok(any(p.startswith("cloud/w.py") for p in problems), "ST5 cloud/ outside configs/ needs one too")
    ok(any("agents/missing.py" in p for p in problems), "ST6 a named agent module that is missing fails")
    ok(not any("__init__" in p for p in run + problems), "ST7 __init__.py is skipped")
    ok(any("scripts/stale.py" in n for n in notes) and any("scripts/gone.py" in n for n in notes),
       "ST8 stale exemptions are reported")
    ok(not any(p.startswith(("main.py", "scripts/stale.py")) for p in problems),
       "ST9 a stale exemption is a note, not a failure")
    _, _, _, pr = classify(["scripts/a.py"], has, agent_modules=(), exempt={})
    ok(any("main.py" in p for p in pr), "ST10 a tree without main.py fails closed")
    # ST11: the live tree discovers without problems, and every exemption is still accurate.
    try:
        lrun, _, lnotes, lprob = discover()
        ok(not lprob, f"ST11 live tree has no discovery problems ({lprob})")
        ok(not lnotes, f"ST11 live tree has no stale exemptions ({lnotes})")
        ok("main.py" in lrun and "scripts/self_tests.py" in lrun, "ST11 main.py and this file are discovered")
        ok("tools/run_console/run_console.py" in lrun, "ST12 a module under tools/ (the run console) is discovered")
    except RuntimeError as e:
        fails.append(f"ST11 git ls-files failed: {e}")
    # ST13: the per-test watchdog. A hung self-test (with a child of its own) is killed as a process group, named and
    # reported failed, and the others still run and pass.
    import contextlib
    import io
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "scripts"))
        pidf = os.path.join(td, "child.pid")
        with open(os.path.join(td, "scripts", "hang.py"), "w") as f:
            f.write("import subprocess, sys, time\n"
                    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
                    f"open({pidf!r}, 'w').write(str(c.pid))\n"
                    "time.sleep(300)\n")
        for name in ("a.py", "b.py"):
            with open(os.path.join(td, "scripts", name), "w") as f:
                f.write("print('OK: all self-test cases passed.')\n")
        buf, t0 = io.StringIO(), time.time()
        try:
            with contextlib.redirect_stdout(buf):
                rc = run_all(sys.executable, 2, repo=td, per_test_timeout=3.0,
                             files=["scripts/a.py", "scripts/hang.py", "scripts/b.py"])
        except Exception as e:  # noqa: BLE001 -- reported in the case
            rc = f"{type(e).__name__}: {e}"
        dt, out = time.time() - t0, buf.getvalue()
        child_alive = None
        if os.path.exists(pidf):
            pid = int(open(pidf).read())
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                    child_alive = True
                except ProcessLookupError:
                    child_alive = False
                    break
                time.sleep(0.1)
            if child_alive:
                os.kill(pid, 9)
        ok(rc == 1 and dt < 60 and "scripts/hang.py" in out and "timed out" in out and child_alive is False
           and "2/3 self-tests passed" in out,
           f"ST13 a hung self-test is killed with its process group, named and failed; the rest pass "
           f"(rc {rc!r}, {dt:.0f}s, child alive {child_alive}): {out[-400:]!r}")
    # ST14: shards. Every file runs in exactly one shard, the split is deterministic, the slowest file opens its shard,
    # and the CI-weighted live tree splits into 4 shards within 20% of each other (or the slowest file bounds them).
    files = [f"scripts/f{i}.py" for i in range(10)]
    wts = {"scripts/f0.py": 100, "scripts/f1.py": 60, "scripts/f2.py": 50}
    parts = [shard(files, k, 3, wts) for k in (1, 2, 3)]
    ok(sorted(sum(parts, [])) == sorted(files), f"ST14 every file is in exactly one shard ({parts})")
    ok(parts == [shard(list(reversed(files)), k, 3, wts) for k in (1, 2, 3)],
       "ST14 the split does not depend on the order of the file list")
    ok(parts[0][0] == "scripts/f0.py" and all(len(p) > 0 for p in parts), f"ST14 slowest first, no empty shard ({parts})")
    ok(shard(files, 1, 1, wts) == sorted(files, key=lambda p: (-wts.get(p, DEFAULT_WEIGHT_S), p)),
       "ST14 one shard holds every file")
    for bad in ("0/4", "5/4", "2", "a/b", "2/0"):
        try:
            parse_shard(bad)
            ok(False, f"ST14 --shard {bad!r} is refused")
        except ValueError:
            pass
    ok(parse_shard("2/4") == (2, 4), "ST14 --shard 2/4 parses")
    try:
        lrun = discover()[0]
        loads = [sum(WEIGHTS_S.get(p, DEFAULT_WEIGHT_S) for p in shard(lrun, k, 4)) for k in (1, 2, 3, 4)]
        ok(max(loads) <= max(1.2 * min(loads), max(WEIGHTS_S.values())), f"ST14 live 4-shard loads balance ({loads})")
        stale = [p for p in WEIGHTS_S if p not in lrun]
        ok(not stale, f"ST14 every WEIGHTS_S entry names a discovered self-test ({stale})")
    except RuntimeError as e:
        fails.append(f"ST14 git ls-files failed: {e}")
    for f in fails:
        print(f"FAIL: {f}")
    if fails:
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print the files that would run and the exemptions")
    ap.add_argument("--jobs", type=int, default=1, help="self-tests to run at a time (default 1)")
    ap.add_argument("--python", default=sys.executable, help="interpreter to run each self-test with")
    ap.add_argument("--per-test-timeout", type=float, default=PER_TEST_TIMEOUT_S, metavar="S",
                    help="kill a self-test (and its process group) that runs longer than S seconds and count it as "
                         f"failed; 0 for no limit (default {PER_TEST_TIMEOUT_S:.0f})")
    ap.add_argument("--shard", metavar="K/N", help="run only shard K of N (CI runs N in parallel); every self-test "
                                                   "is in exactly one shard")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    try:
        shard_kn = parse_shard(a.shard) if a.shard else None
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if a.list:
        try:
            run, skipped, notes, problems = discover()
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print("\n".join(shard(run, *shard_kn) if shard_kn else run))
        for p, why in skipped:
            print(f"# exempt: {p}: {why}")
        for n in notes:
            print(f"# note: {n}")
        for p in problems:
            print(f"# DISCOVERY PROBLEM: {p}")
        return 1 if problems else 0
    return run_all(a.python, a.jobs, per_test_timeout=a.per_test_timeout, shard_kn=shard_kn)


if __name__ == "__main__":
    sys.exit(main())
