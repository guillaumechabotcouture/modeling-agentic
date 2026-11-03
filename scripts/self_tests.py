#!/usr/bin/env python3
"""Find and run every module's --self-test (one discovery rule for CLAUDE.md and CI).

Scope: `main.py`, every tracked `.py` file under `scripts/`, `toolkit/` and `cloud/`, and the
harness modules under `agents/` named in AGENT_MODULES (other `agents/` files only mention
--self-test in text, or run a live preflight). `__init__.py` files are skipped.

A file in scope that mentions `--self-test` is run as `python <file> --self-test` from the
repository root. A file in scope that does not is a discovery problem, and the run fails,
unless it is listed in EXEMPT with the reason it has none. So a new module under `scripts/`,
`toolkit/` or `cloud/` cannot be skipped silently: it gets a self-test or an exemption.

    python3 scripts/self_tests.py              # run them all (exit 1 on any failure)
    python3 scripts/self_tests.py --list       # print what would run, and the exemptions
    python3 scripts/self_tests.py --jobs 2     # run two at a time
    python3 scripts/self_tests.py --self-test  # test the discovery rule itself
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOPE_DIRS = ("scripts/", "toolkit/", "cloud/")
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
    "toolkit/analysis/uncertainty.py": "analysis-engine library; exercised by toolkit/analysis/run.py --self-test",
    "toolkit/engines/malaria/model.py": "engine module; exercised by toolkit/engines/malaria/engine.py --self-test",
    "toolkit/engines/compartmental/benchmarks/runtime_spike.py": "one-off timing script for the dev VM",
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


def _run_one(python: str, repo: str, path: str):
    t0 = time.time()
    p = subprocess.run([python, path, MARKER], cwd=repo, capture_output=True, text=True)
    return path, p.returncode, p.stdout + p.stderr, time.time() - t0


def run_all(python: str, jobs: int, repo: str = REPO) -> int:
    try:
        run, skipped, notes, problems = discover(repo)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    failed = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for path, rc, out, dt in ex.map(lambda f: _run_one(python, repo, f), run):
            print(f"::group::{path}" if gha else f"== {path} ({dt:.0f}s)", flush=True)
            print(out.rstrip())
            if gha:
                print("::endgroup::")
            if rc != 0:
                failed.append(path)
                print(f"::error file={path}::self-test failed" if gha else f"FAILED: {path} (exit {rc})",
                      flush=True)
    for n in notes:
        print(f"NOTE: {n}")
    for p in problems:
        print(f"::error file={p.split()[0]}::{p}" if gha else f"DISCOVERY PROBLEM: {p}")
    print(f"\n{len(run) - len(failed)}/{len(run)} self-tests passed; {len(skipped)} exempt; "
          f"{len(problems)} discovery problem(s).")
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
    except RuntimeError as e:
        fails.append(f"ST11 git ls-files failed: {e}")
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
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.list:
        try:
            run, skipped, notes, problems = discover()
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print("\n".join(run))
        for p, why in skipped:
            print(f"# exempt: {p}: {why}")
        for n in notes:
            print(f"# note: {n}")
        for p in problems:
            print(f"# DISCOVERY PROBLEM: {p}")
        return 1 if problems else 0
    return run_all(a.python, a.jobs)


if __name__ == "__main__":
    sys.exit(main())
