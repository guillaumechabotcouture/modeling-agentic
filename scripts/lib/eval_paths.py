#!/usr/bin/env python3
"""Which repository paths belong to the evaluation suite's hidden side (Phase 50 α).

`evals/` holds the generators, rubrics and answer keys of the synthetic eval questions,
and docs/design/eval-suite.md describes the planted truths. Agents being scored must
never see them, so they are never shipped (`scripts/eval_suite.py`, the generic grader,
holds no question content and ships like the rest of `scripts/`):
- `.gitattributes` marks them export-ignore, so `git archive` (the cloud launch
  tarball) leaves them out;
- `cloud/run_cluster.py` `code_tarball` refuses a tarball that holds any of them;
- the harness manifest hashes only `scripts/ agents/ .claude/ toolkit/` and the
  top-level harness files, none of which is hidden.

Shipped: each question's input data, `data/evals/<q>/`, like the rest of `data/`.

    python3 scripts/lib/eval_paths.py --self-test
"""
from __future__ import annotations

import io
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
    if any(p.startswith("_truth") for p in parts):
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
