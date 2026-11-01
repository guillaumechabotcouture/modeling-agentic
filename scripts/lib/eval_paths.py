#!/usr/bin/env python3
"""Which repository paths belong to the evaluation suite's hidden side (Phase 50 α).

The generators, rubrics, answer keys and design note of the synthetic eval questions
are no longer in the repository: they are an encrypted bundle in the project storage
account (scripts/eval_private.py, evals/README.md). `evals/` keeps the question texts
and the suite's public files. The rules below still treat all of `evals/`, any
`_truth*` path and the old design-note path as hidden, so a decrypted copy placed in a
checkout is never shipped (`scripts/eval_suite.py`, the generic grader, holds no
question content and ships like the rest of `scripts/`):
- `.gitattributes` marks them export-ignore, so `git archive` (the cloud launch
  tarball) leaves them out;
- `cloud/run_cluster.py` `code_tarball` refuses a tarball that holds any of them;
- the harness manifest hashes only `scripts/ agents/ .claude/ toolkit/` and the
  top-level harness files, none of which is hidden.

Shipped: each question's input data, `data/evals/<q>/`, like the rest of `data/`.

    python3 scripts/lib/eval_paths.py --self-test
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile

# Files outside evals/ that describe the planted truths.
HIDDEN_FILES = ("docs/design/eval-suite.md",)


def is_hidden(path: str) -> bool:
    """True for a repo-relative path an agent must not see: anything under evals/,
    any `_truth*` component anywhere, and HIDDEN_FILES."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return False
    if any(p.startswith("_truth") or p == ".git" for p in parts):
        return True
    if parts[0] == "evals":
        return True
    return "/".join(parts) in HIDDEN_FILES


def hidden_members(blob: bytes) -> list[str]:
    """Hidden file members of a .tar.gz (directory entries are ignored: an empty
    `evals/` directory entry carries nothing). An unreadable archive is reported as
    hidden content, so a caller refusing on a non-empty result fails closed."""
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as t:
            return sorted(m.name for m in t.getmembers() if not m.isdir() and is_hidden(m.name))
    except (tarfile.TarError, OSError, EOFError) as e:
        return [f"<unreadable archive: {type(e).__name__}>"]


def archive_sha256(blob: bytes, prefix: str = "data/evals/") -> dict:
    """sha256 of every file member under `prefix` in a .tar.gz: the inputs a launch
    shipped, recorded with each run so grading can refuse a run on other inputs."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as t:
        for m in t.getmembers():
            if m.isfile() and m.name.startswith(prefix):
                out[m.name] = hashlib.sha256(t.extractfile(m).read()).hexdigest()
    return out


# The marker every eval question's text carries in its first words.
EVAL_PREFIX = "SYNTHETIC TEST QUESTION (evaluation suite):"
ISOLATION_ENV, ISOLATION_VALUE = "MA_EVAL_ISOLATED", "cloud-worker"
_WORKSPACE_HIDDEN = (".git", "evals", os.path.join("docs", "design", "eval-suite.md"))


def workspace_hidden(root: str) -> list[str]:
    """Hidden paths with content under `root`. `git archive` keeps an empty `evals/`
    directory entry, so an empty directory holds nothing and does not count."""
    found = []
    for p in _WORKSPACE_HIDDEN:
        full = os.path.join(root, p)
        if os.path.isfile(full) or (os.path.isdir(full) and any(files for _, _, files in os.walk(full))):
            found.append(p)
    return found


def is_eval_question(question: str, meta: dict | None = None) -> bool:
    """An eval question is marked by explicit run metadata (`eval_question: true`) or by
    the complete synthetic prefix at the start of its text, not by a substring anywhere."""
    if isinstance(meta, dict) and meta.get("eval_question") is True:
        return True
    return (question or "").lstrip().startswith(EVAL_PREFIX)


def question_for(question_arg: str | None, resume_dir: str | None) -> tuple[str, dict]:
    """The question a run will actually use, and the run metadata it came from: the
    argument, else the metadata.json of the run being resumed."""
    meta = {}
    if resume_dir:
        try:
            with open(os.path.join(resume_dir, "metadata.json")) as f:
                meta = json.load(f) or {}
        except (OSError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
    return (question_arg or meta.get("question") or ""), meta


def eval_workspace_problem(question: str, root: str, meta: dict | None = None) -> str | None:
    """Eval questions run only on a cloud worker: a fresh VM whose work directory is the
    unpacked launch archive (no evals/, no repository history) and which no other
    checkout shares. A local machine can always reach some checkout's history, so a
    local run is refused even from a clean directory. The worker sets
    MA_EVAL_ISOLATED=cloud-worker (cloud/worker_side.py)."""
    if not is_eval_question(question, meta):
        return None
    if os.environ.get(ISOLATION_ENV) != ISOLATION_VALUE:
        return ("this is an eval-suite question: eval runs execute only on the cloud launcher "
                "(python3 scripts/eval_suite.py run), never on a local machine")
    found = workspace_hidden(root)
    if found:
        return f"this is an eval-suite question, and {root} holds {', '.join(found)}: refusing"
    return None


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        if not cond:
            fails.append(msg)

    for p in ("evals/malaria_alloc/_truth/truth.json", "evals/malaria_alloc/generate.py", "evals/_common.py",
              "evals/taxonomy.yaml", "./evals/x", "runs/r/_truth/a.yaml", "docs/design/eval-suite.md", "_truth"):
        ok(is_hidden(p), f"EP1 hidden: {p}")
    for p in ("data/evals/malaria_alloc/districts.csv", "scripts/eval_suite.py", "docs/design/stage-machine.md",
              "cloud/configs/eval_malaria_alloc.py", "evaluation/x", "", "data/truthful.csv"):
        ok(not is_hidden(p), f"EP2 shipped: {p}")

    def tgz(names, dirs=()):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for d in dirs:
                ti = tarfile.TarInfo(d)
                ti.type = tarfile.DIRTYPE
                t.addfile(ti)
            for n in names:
                ti = tarfile.TarInfo(n)
                ti.size = 1
                t.addfile(ti, io.BytesIO(b"x"))
        return buf.getvalue()
    ok(hidden_members(tgz(["main.py", "data/evals/q/a.csv"], dirs=["evals/", "evals/q/"])) == [],
       "EP3 inputs and empty directory entries pass")
    ok(hidden_members(tgz(["main.py", "evals/q/_truth/truth.json"])) == ["evals/q/_truth/truth.json"],
       "EP4 an answer key in the tarball is reported")
    ok(hidden_members(b"not a tarball")[0].startswith("<unreadable"), "EP5 an unreadable tarball fails closed")
    ok(hidden_members(tgz(["main.py", ".git/HEAD"])) == [".git/HEAD"], "EP6 a tarball holding .git is reported")
    sums = archive_sha256(tgz(["main.py", "data/evals/q/a.csv"]))
    ok(list(sums) == ["data/evals/q/a.csv"] and sums["data/evals/q/a.csv"] == hashlib.sha256(b"x").hexdigest(),
       "EP7 archive_sha256 hashes the shipped inputs only")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        q = EVAL_PREFIX + " x"
        os.makedirs(os.path.join(td, "evals"))   # the empty directory entry git archive keeps
        saved = os.environ.pop(ISOLATION_ENV, None)
        try:
            ok(eval_workspace_problem(q, td), "EP8 off the cloud worker an eval question is refused, even in a clean dir")
            os.environ[ISOLATION_ENV] = ISOLATION_VALUE
            ok(eval_workspace_problem(q, td) is None, "EP8b a cloud worker's unpacked archive (empty evals/) may run it")
            os.makedirs(os.path.join(td, ".git"))
            open(os.path.join(td, ".git", "HEAD"), "w").close()
            ok(eval_workspace_problem(q, td) and eval_workspace_problem("ordinary", td) is None,
               "EP9 a root with history may not run an eval question; ordinary questions are unaffected")
        finally:
            os.environ.pop(ISOLATION_ENV, None)
            if saved is not None:
                os.environ[ISOLATION_ENV] = saved
        ok(not is_eval_question("a question about the (evaluation suite) marker") and is_eval_question(q)
           and is_eval_question("x", {"eval_question": True}), "EP10 detection: full prefix or explicit metadata only")
        os.makedirs(os.path.join(td, "r"))
        with open(os.path.join(td, "r", "metadata.json"), "w") as f:
            json.dump({"question": q}, f)
        ok(question_for(None, os.path.join(td, "r"))[0] == q and question_for("arg", None) == ("arg", {}),
           "EP11 the effective question: the argument, else the resumed run's metadata")
    for f in fails:
        print("FAIL:", f, file=sys.stderr)
    if fails:
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
