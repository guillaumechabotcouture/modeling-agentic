#!/usr/bin/env python3
"""Which pipeline a main.py invocation runs, and the run identity every run records.

On 2026-11-05 an eval re-run meant to test a v2 fix ran the v1 pipeline: main.py defaulted
to v1, cloud/configs/_eval.py never passed --v2, and nothing recorded or checked which
pipeline a run used. This module is the one place that answers "which pipeline":

- `select(v1, v2)`: main.py's dispatch. v2 is the default; `--v1` selects v1; `--v2` is
  accepted as a no-op alias; both together is an error.
- `from_flags(flags)` / `from_command(cmd)`: what main.py would run for these arguments
  (launcher preflights: cloud/run_cluster.py, cloud/vm_job.py).
- `identity(pipeline, repo, launcher)`: {pipeline, commit, launcher}, written into every
  run's metadata.json (`stamp`). The commit comes from `code_commit.txt` (git archive fills
  it in through export-subst, so a launch tarball with no .git still knows its commit), else
  `git rev-parse HEAD`, else "unknown".
- `label(meta)`: (pipeline, commit) of a run record; a missing or unrecognised field reads
  "unknown", never a guess.

Standard library only: launchers run this file from inside a launch tarball
(`python3 -I pipeline_id.py which -- <main.py flags>`) to ask the shipped code itself.

    python3 scripts/lib/pipeline_id.py which -- --max-rounds 3 --v1     # prints v1
    python3 scripts/lib/pipeline_id.py --self-test
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

PIPELINES = ("v1", "v2")
DEFAULT = "v2"
UNKNOWN = "unknown"
COMMIT_FILE = "code_commit.txt"        # next to this module; .gitattributes: export-subst
_UNFILLED = "$Format:"


class PipelineError(ValueError):
    """The arguments do not name one pipeline."""


def select(v1: bool = False, v2: bool = False) -> str:
    """main.py's pipeline for its --v1 / --v2 flags."""
    if v1 and v2:
        raise PipelineError("--v1 and --v2 together: pass one")
    return "v1" if v1 else DEFAULT


def _flag_tokens(flags) -> tuple[bool, bool]:
    toks = list(flags or ())
    if "--" in toks:                     # main.py takes no `--`; nothing after it is a flag
        toks = toks[:toks.index("--")]
    return "--v1" in toks, "--v2" in toks


def from_flags(flags) -> str:
    """The pipeline main.py runs with these arguments (main.py's argparse has no `--v`
    abbreviation that could reach either flag: `--v` is ambiguous there and refused)."""
    v1, v2 = _flag_tokens(flags)
    return select(v1, v2)


def explicit(flags) -> str | None:
    """The pipeline these arguments state explicitly, or None when they rely on the default."""
    v1, v2 = _flag_tokens(flags)
    if v1 or v2:
        return select(v1, v2)
    return None


_OPS = {";", "&&", "||", "|", "&", "\n", "(", ")", "<", ">", ">>", "<<", ";;", "|&", "&>", ">&", "<&"}


def _split_commands(cmd) -> list[list[str]]:
    """A command as its simple commands (argv lists). An argv list is one command. A shell string
    is split on shell operators, attached or not, and on newlines (shlex in punctuation mode)."""
    if not isinstance(cmd, str):
        return [[str(t) for t in cmd or ()]]
    lex = shlex.shlex(cmd.replace("\r", "\n"), posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t"                 # a newline is a command boundary, not a space
    lex.whitespace_split = True
    lex.commenters = ""
    out, cur = [], []
    for t in lex:
        if t in _OPS or (t and set(t) <= set(";&|()<>\n")):
            if cur:
                out.append(cur)
            cur = []
            continue
        cur.append(t)
    if cur:
        out.append(cur)
    return out


def main_py_calls(cmd) -> list[list[str]]:
    """The arguments of every main.py call in a command (empty when it runs none). A wrapper
    script that calls main.py cannot be seen."""
    calls = []
    for argv in _split_commands(cmd):
        for i, t in enumerate(argv):
            if os.path.basename(t) == "main.py":
                calls.append(argv[i + 1:])
                break
    return calls


def main_py_args(cmd) -> list | None:
    """The arguments of the first main.py call in a command, or None when it runs none."""
    calls = main_py_calls(cmd)
    return calls[0] if calls else None


def from_command(cmd) -> str | None:
    """A job command's main.py pipeline when every main.py call in it states the same one
    explicitly; None when it runs no main.py. Raises PipelineError when a call relies on the
    default (the default changed once and silently, 2026-11-05), when calls disagree, or when
    a shell string makes main.py's arguments depend on an expansion (`$(...)`, backquotes)."""
    if isinstance(cmd, str) and "main.py" in cmd and ("`" in cmd or "$(" in cmd):
        raise PipelineError("the command builds main.py's arguments with a command substitution: "
                            "state the pipeline as a literal --v1 or --v2")
    calls = main_py_calls(cmd)
    if not calls:
        return None
    got = set()
    for args in calls:
        p = explicit(args)
        if p is None:
            raise PipelineError("the command runs main.py without --v1 or --v2: state the pipeline explicitly")
        got.add(p)
    if len(got) != 1:
        raise PipelineError(f"the command runs main.py under several pipelines ({sorted(got)}): run one")
    return got.pop()


def code_commit(repo: str) -> str:
    """The commit of the code in `repo`: code_commit.txt as git archive filled it in, else
    `git rev-parse HEAD`, else "unknown"."""
    p = os.path.join(repo, "scripts", "lib", COMMIT_FILE)
    try:
        v = open(p).read().strip()
        if v and not v.startswith(_UNFILLED) and all(c in "0123456789abcdef" for c in v) and len(v) >= 7:
            return v
    except OSError:
        pass
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
        v = r.stdout.strip()
        if r.returncode == 0 and len(v) >= 7:
            dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=no"],
                                   capture_output=True, text=True, timeout=30).stdout.strip()
            return v + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        pass
    return UNKNOWN


def launcher_name(arg: str | None = None) -> str:
    """The launcher that started this run: main.py --launcher, else $MA_LAUNCHER, else "direct"."""
    return (arg or os.environ.get("MA_LAUNCHER") or "direct").strip()[:200] or "direct"


def identity(pipeline: str, repo: str, launcher: str | None = None) -> dict:
    if pipeline not in PIPELINES:
        raise PipelineError(f"unknown pipeline {pipeline!r}")
    return {"pipeline": pipeline, "commit": code_commit(repo), "launcher": launcher_name(launcher)}


def stamp(run_path: str, ident: dict, now: str | None = None) -> dict:
    """Merge the identity into metadata.json (creating it if absent) and append this invocation
    to `invocations`, so a resumed run shows every commit that worked on it."""
    from datetime import datetime
    mp = os.path.join(run_path, "metadata.json")
    try:
        meta = json.load(open(mp))
    except (OSError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    before = label(meta)[0]
    meta.update({k: ident[k] for k in ("pipeline", "commit", "launcher")})
    inv = meta.get("invocations") if isinstance(meta.get("invocations"), list) else []
    meta["invocations"] = inv + [{**{k: ident[k] for k in ("pipeline", "commit", "launcher")},
                                  "pipeline_before": before,
                                  "at": now or datetime.now().isoformat(timespec="seconds")}]
    if not os.path.isdir(run_path):
        raise PipelineError(f"{run_path}: no run directory to stamp")
    if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from safe_io import publish      # a run dir may hold agent-planted links: no link is followed
    publish(mp, json.dumps(meta, indent=2), run_path)
    return meta


def resume_conflict(meta: dict, pipeline: str, run_path: str | None = None) -> str | None:
    """Why a run may not be resumed under `pipeline`, or None. A recorded pipeline must match; a
    malformed one is refused. With none recorded (runs from before 2026-11-05) the run's files
    decide: run_state.json is the v2 machine's, so such a run is v2's; a run without it is v1's."""
    meta = meta if isinstance(meta, dict) else {}
    if "pipeline" in meta:
        rec = meta.get("pipeline")
        if rec not in PIPELINES:
            return f"this run records pipeline {rec!r}, which is not v1 or v2: refusing to resume it"
        if rec != pipeline:
            return f"this run was recorded as pipeline {rec}; resume it with --{rec}"
        return None
    if run_path is None:
        return None
    by_files = "v2" if os.path.exists(os.path.join(run_path, "run_state.json")) else "v1"
    if by_files != pipeline:
        return (f"this run records no pipeline, and its files are pipeline {by_files}'s "
                f"({'has' if by_files == 'v2' else 'no'} run_state.json); resume it with --{by_files}")
    return None


def label(meta) -> tuple[str, str]:
    """(pipeline, commit) of a run record; "unknown" for anything missing or unrecognised."""
    meta = meta if isinstance(meta, dict) else {}
    p = meta.get("pipeline")
    c = meta.get("commit")
    return (p if p in PIPELINES else UNKNOWN,
            c.strip() if isinstance(c, str) and c.strip() and c.strip() != UNKNOWN else UNKNOWN)


def same_commit(a: str, b: str) -> bool:
    """Two commit ids name the same commit (one may be a 7-character prefix); unknown never matches."""
    a, b = (a or "").split("-")[0], (b or "").split("-")[0]
    if UNKNOWN in (a, b) or len(a) < 7 or len(b) < 7:
        return False
    return a.startswith(b) or b.startswith(a)


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    ok(select() == "v2", "PI1 no flag selects v2 (the default since 2026-11-05)")
    ok(select(v1=True) == "v1", "PI2 --v1 selects v1")
    ok(select(v2=True) == "v2", "PI3 --v2 is accepted as a no-op alias")
    try:
        select(True, True)
        ok(False, "PI4 --v1 with --v2 is refused")
    except PipelineError:
        ok(True, "PI4 --v1 with --v2 is refused")
    # Today's evidence: the eval config's flags had no pipeline flag, and main.py ran v1.
    ok(from_flags(["--max-budget-usd", "35", "--signoff", "lead"]) == "v2",
       "PI5 the eval config's 2026-11-05 flags now run v2")
    ok(explicit(["--max-budget-usd", "35", "--signoff", "lead"]) is None, "PI6 those flags state no pipeline")
    ok(explicit(["--v1", "--signoff", "lead"]) == "v1", "PI7 --v1 is explicit")
    ok(from_flags(["Q?", "--", "--v1"]) == "v2", "PI8 a token after `--` is not a flag")
    ok(main_py_args("python3 main.py 'Q?' --v2 --max-rounds 3") == ["Q?", "--v2", "--max-rounds", "3"],
       "PI9 the arguments of main.py in a shell string")
    ok(main_py_args(["bash", "toolkit/x/validate_once.sh", "16"]) is None, "PI10 a command without main.py")
    ok(from_command(["python", "main.py", "Q", "--v1"]) == "v1", "PI11 argv command with --v1")
    ok(from_command("cd x && python3 ./main.py Q --v2; cp -r runs $JOB_OUT") == "v2",
       "PI12 a compound shell command: only main.py's own arguments count")
    try:
        from_command("python3 main.py 'Q?' --max-rounds 3")
        ok(False, "PI13 a main.py command relying on the default is refused")
    except PipelineError:
        ok(True, "PI13 a main.py command relying on the default is refused")
    try:
        from_command("python3 main.py Q && echo --v2")
        ok(False, "PI14 --v2 in a later command does not count for main.py")
    except PipelineError:
        ok(True, "PI14 --v2 in a later command does not count for main.py")
    # PI29-PI31 (Codex r1 P1): shell boundaries the old parser lost, and several main.py calls.
    for cmd, why in (("python3 main.py Q\necho --v1", "a newline ends main.py's arguments"),
                     ("python3 main.py Q;echo --v1", "an attached ; ends them"),
                     ("python3 main.py Q&&echo --v1", "an attached && ends them"),
                     ("python3 main.py Q --v2; python3 main.py R", "a second main.py without a pipeline"),
                     ("python3 main.py Q --v2 && python3 main.py R --v1", "two main.py calls that disagree"),
                     ("python3 main.py $(echo --v2) Q", "a command substitution main.py's arguments depend on")):
        try:
            got = from_command(cmd)
            ok(False, f"PI29 refused ({why}): got {got}")
        except PipelineError:
            ok(True, f"PI29 refused: {why}")
    ok(from_command("python3 main.py Q --v2\ncp -r runs $JOB_OUT/") == "v2", "PI30 a later line does not hide main.py's flag")
    ok(from_command("python3 main.py A --v2 && python3 main.py B --v2") == "v2", "PI31 several calls that agree")
    with tempfile.TemporaryDirectory() as td:
        rs = os.path.join(td, "v2run")
        os.makedirs(rs)
        open(os.path.join(rs, "run_state.json"), "w").write("{}")
        ok(resume_conflict({}, "v1", rs) and not resume_conflict({}, "v2", rs),
           "PI32 a run with no recorded pipeline but a run_state.json is v2's")
        ok(not resume_conflict({}, "v1", td), "PI33 an old run with neither (a v1 run) resumes with --v1")
        ok(resume_conflict({"pipeline": "V2!"}, "v2", rs) and resume_conflict({"pipeline": 2}, "v1", td),
           "PI34 a malformed recorded pipeline is refused")
        json.dump({"question": "Q"}, open(os.path.join(rs, "metadata.json"), "w"))
        m = stamp(rs, {"pipeline": "v2", "commit": "abc1234", "launcher": "direct"}, now="t")
        ok(m["invocations"][-1].get("pipeline_before") == UNKNOWN, "PI35 a stamp keeps what was recorded before (unknown)")
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "scripts", "lib"))
        cf = os.path.join(td, "scripts", "lib", COMMIT_FILE)
        open(cf, "w").write("$Format:%H$\n")
        ok(code_commit(td) == UNKNOWN, "PI15 an unfilled commit file outside git reads unknown")
        open(cf, "w").write("0123456789abcdef0123456789abcdef01234567\n")
        ok(code_commit(td) == "0123456789abcdef0123456789abcdef01234567", "PI16 the archived commit file is read")
        ident = identity("v2", td, "run_cluster:ev-dengue-x")
        ok(ident == {"pipeline": "v2", "commit": "0123456789abcdef0123456789abcdef01234567",
                     "launcher": "run_cluster:ev-dengue-x"}, f"PI17 identity {ident}")
        rd = os.path.join(td, "run")
        os.makedirs(rd)
        json.dump({"question": "Q", "started": "s"}, open(os.path.join(rd, "metadata.json"), "w"))
        m = stamp(rd, ident, now="t1")
        m = stamp(rd, {**ident, "commit": "fedcba9"}, now="t2")
        ok(m["question"] == "Q" and m["pipeline"] == "v2" and m["commit"] == "fedcba9"
           and [i["commit"] for i in m["invocations"]] == [ident["commit"], "fedcba9"],
           "PI18 stamp merges, keeps the question, and records each invocation")
        open(os.path.join(rd, "metadata.json"), "w").write("[]")
        ok(stamp(rd, ident, now="t")["pipeline"] == "v2", "PI19 a non-object metadata.json is replaced, not a crash")
        try:
            identity("v3", td)
            ok(False, "PI20 an unknown pipeline is refused")
        except PipelineError:
            ok(True, "PI20 an unknown pipeline is refused")
    ok(label({}) == (UNKNOWN, UNKNOWN), "PI21 an old run with no fields reads unknown/unknown")
    ok(label({"pipeline": "V2", "commit": ""}) == (UNKNOWN, UNKNOWN), "PI22 unrecognised values read unknown")
    ok(label(None) == (UNKNOWN, UNKNOWN), "PI23 no record reads unknown")
    ok(label({"pipeline": "v1", "commit": "abc1234"}) == ("v1", "abc1234"), "PI24 recorded values are read")
    ok(resume_conflict({"pipeline": "v1"}, "v2") and not resume_conflict({}, "v2")
       and not resume_conflict({"pipeline": "v2"}, "v2"), "PI25 a recorded pipeline must match on resume")
    ok(same_commit("abc1234", "abc1234def") and not same_commit("abc1234", "abd1234")
       and not same_commit(UNKNOWN, UNKNOWN) and same_commit("abc1234def-dirty", "abc1234"),
       "PI26 commit prefixes match; unknown never matches")
    old = os.environ.pop("MA_LAUNCHER", None)
    try:
        ok(launcher_name() == "direct" and launcher_name("x") == "x", "PI27 launcher defaults to direct")
        os.environ["MA_LAUNCHER"] = "vm_job:n/j"
        ok(launcher_name() == "vm_job:n/j", "PI28 $MA_LAUNCHER names the launcher")
    finally:
        os.environ.pop("MA_LAUNCHER", None)
        if old is not None:
            os.environ["MA_LAUNCHER"] = old
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _cli(argv) -> int:
    if argv[:1] == ["--self-test"]:
        return _self_test()
    if argv[:1] == ["which"]:
        rest = argv[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        try:
            print(from_flags(rest))
        except PipelineError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
