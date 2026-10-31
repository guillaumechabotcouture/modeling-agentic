#!/usr/bin/env python3
"""Change fragments: one file per PR instead of edits to two shared files.

Phase 51 α. Nearly every PR in Phases 45-49 conflicted on the same two
places: the phase ledger table in CHANGELOG.md (every PR appends a row at
its end) and the Status section of docs/design/improvement-plan.md (every
PR adds a group above "Before attempt 9:"). Two PRs that append at the same
point always conflict, whatever they say. A PR now adds one new file,
`changes/<branch>.md`, which no other PR touches, and a maintainer folds the
fragments into the two shared files when releasing.

Fragment format (`changes/<branch-or-phase>.md`):

    ---
    phase: "51"
    commit: "α"
    status_group: "Merged (Phase 51, development process)"   # optional
    ---

    ## Changelog

    **One-line title.** The ledger row's Change text (one paragraph).

    ## Status

    - **α:** the Status line(s) for the plan (optional section).

Folding (`--write`):
  - the Changelog paragraph becomes a row `| phase | commit | text |` at the
    end of the "## Phase commit ledger" table, fragments in (phase, Greek
    letter, file name) order;
  - Status lines join the group line `<status_group>:` in the plan's
    "## Status" section if that group exists, else a new group is placed
    above the marker line `<!-- change-fragments ... -->`;
  - the folded fragment files are deleted. The maintainer commits the result.

PR check (`--check-pr BASE`, run in CI on pull requests): every fragment the
PR adds or changes must parse, and the ledger table and the Status section
may change only as the exact fold of fragments the same PR deletes (a
release PR). Any other edit of those two regions fails, with the fragment
to write instead. Edits elsewhere in either file (e.g. CHANGELOG's "Gate
internals") are not checked. The marker line itself is ignored, so adding
or moving it is not an edit.

Threat model: the check is against accidental direct edits by people and
agents following the old habit, not against a maintainer; it never writes
to the repository and needs only read access in CI.

    python3 scripts/assemble_changes.py                  # show what a release would fold
    python3 scripts/assemble_changes.py --write          # fold and delete the fragments
    python3 scripts/assemble_changes.py --check-pr origin/main
    python3 scripts/assemble_changes.py --self-test
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile

import yaml

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CHANGES_DIR = "changes"
CHANGELOG = "CHANGELOG.md"
PLAN = "docs/design/improvement-plan.md"
LEDGER_HEADING = "## Phase commit ledger"
STATUS_HEADING_RE = re.compile(r"^## Status\b")
MARKER_RE = re.compile(r"^<!-- change-fragments\b.*-->\s*$")
MARKER = ("<!-- change-fragments: scripts/assemble_changes.py adds new Status groups "
          "above this line -->")
GREEK = "αβγδεζηθικλμνξοπρστυφχψω"
KEYS = {"phase", "commit", "status_group"}


class FragmentError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Fragments
# --------------------------------------------------------------------------- #

def _multiline(s: str) -> bool:
    """True if `s` holds any character str.splitlines() breaks on (\\n, \\r,
    \\v, \\f, \\x1c-\\x1e, \\x85, U+2028, U+2029): one would split a ledger row."""
    return len((s + "x").splitlines()) != 1


def fragment_name(branch: str) -> str:
    """The fragment file for a branch: flat, '/' mapped to '__'."""
    return branch.strip().replace("/", "__") + ".md"


def fragment_paths(paths, where: str = "") -> list[str]:
    """The one discovery rule, for CI and for assembly alike: of repo-relative
    paths, those that are fragments (changes/<name>.md, README.md excluded).
    Anything else under changes/ (a nested path, or a file that is not .md)
    is an error, so a fragment can never be skipped by one side and seen by
    the other."""
    out = []
    for p in paths:
        if not p.startswith(CHANGES_DIR + "/"):
            continue
        rest = p[len(CHANGES_DIR) + 1:]
        if "/" in rest:
            raise FragmentError(f"{p}{where}: nested path under {CHANGES_DIR}/; fragments are flat "
                                f"files (a branch 'a/b' uses {CHANGES_DIR}/{fragment_name('a/b')})")
        if rest.upper() == "README.MD":
            continue
        if not rest.endswith(".md"):
            raise FragmentError(f"{p}{where}: only .md fragments belong in {CHANGES_DIR}/")
        out.append(p)
    return sorted(out)


def _same_entry(a: dict, b: dict) -> bool:
    return all(a[k] == b[k] for k in ("phase", "commit", "status_group", "changelog", "status"))


def parse_fragment(text: str, name: str = "fragment") -> dict:
    """Return {phase, commit, status_group, changelog, status: [lines], name}.
    Raises FragmentError on any structural problem (fail closed)."""
    if not isinstance(text, str):
        raise FragmentError(f"{name}: not text")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FragmentError(f"{name}: must start with a '---' header block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise FragmentError(f"{name}: header block has no closing '---'") from None
    try:
        head = yaml.safe_load("\n".join(lines[1:end]))
    except (yaml.YAMLError, ValueError, RecursionError) as e:
        raise FragmentError(f"{name}: header is not valid YAML ({type(e).__name__})") from None
    if not isinstance(head, dict):
        raise FragmentError(f"{name}: header must be a mapping")
    extra = set(map(str, head)) - KEYS
    if extra:
        raise FragmentError(f"{name}: unknown header keys {sorted(extra)}")
    for k in ("phase", "commit"):
        v = head.get(k)
        if isinstance(v, bool) or not isinstance(v, (str, int)) or not str(v).strip():
            raise FragmentError(f"{name}: header needs a non-empty '{k}'")
        if _multiline(str(v)):
            raise FragmentError(f"{name}: '{k}' must be one line (no line or paragraph separators)")
    group = head.get("status_group")
    if group is not None and (not isinstance(group, str) or not group.strip() or _multiline(group)):
        raise FragmentError(f"{name}: status_group must be a one-line string")

    sections: dict[str, list[str]] = {}
    cur = None
    for ln in lines[end + 1:]:
        m = re.match(r"^## (\S.*?)\s*$", ln)
        if m:
            cur = m.group(1).strip().lower()
            if cur not in ("changelog", "status"):
                raise FragmentError(f"{name}: unknown section '## {m.group(1)}'")
            if cur in sections:
                raise FragmentError(f"{name}: section '## {m.group(1)}' appears twice")
            sections[cur] = []
            continue
        if cur is None:
            if ln.strip():
                raise FragmentError(f"{name}: text before the first '## Changelog' section")
            continue
        sections[cur].append(ln)

    body = " ".join(ln.strip() for ln in sections.get("changelog", []) if ln.strip())
    if not body:
        raise FragmentError(f"{name}: '## Changelog' section is missing or empty")
    status = [ln.rstrip() for ln in sections.get("status", []) if ln.strip()]
    for ln in status:
        if not (ln.startswith("- ") or ln.startswith("  ")):
            raise FragmentError(f"{name}: Status lines must be '- ' bullets (got {ln[:40]!r})")
    if status and group is None:
        raise FragmentError(f"{name}: a '## Status' section needs 'status_group' in the header")
    return {"name": name, "phase": str(head["phase"]).strip(), "commit": str(head["commit"]).strip(),
            "status_group": group.strip().rstrip(":").strip() if group else None, "changelog": body, "status": status}


def _sort_key(fr: dict):
    m = re.match(r"\d+", fr["phase"])
    letter = fr["commit"][:1]
    return (int(m.group()) if m else 10 ** 6, fr["phase"],
            GREEK.index(letter) if letter and letter in GREEK else len(GREEK), fr["commit"], fr["name"])


def ledger_row(fr: dict) -> str:
    cell = lambda s: s.replace("|", "\\|")  # noqa: E731
    return f"| {cell(fr['phase'])} | {cell(fr['commit'])} | {cell(fr['changelog'])} |"


# --------------------------------------------------------------------------- #
# Regions of the two shared files
# --------------------------------------------------------------------------- #

def _section_bounds(lines: list[str], match) -> tuple[int, int]:
    """(start, end) of the '## ' section whose heading satisfies `match`
    (start = heading line, end = next '## ' heading or EOF)."""
    for i, ln in enumerate(lines):
        if match(ln):
            j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
            return i, j
    raise FragmentError("section not found")


def ledger_region(text: str) -> list[str]:
    lines = text.splitlines()
    s, e = _section_bounds(lines, lambda ln: ln.strip() == LEDGER_HEADING)
    return lines[s:e]


def status_region(text: str) -> list[str]:
    lines = text.splitlines()
    s, e = _section_bounds(lines, lambda ln: bool(STATUS_HEADING_RE.match(ln)))
    return [ln for ln in lines[s:e] if not MARKER_RE.match(ln)]


def fold_changelog(text: str, frags: list[dict]) -> str:
    lines = text.splitlines()
    try:
        s, e = _section_bounds(lines, lambda ln: ln.strip() == LEDGER_HEADING)
    except FragmentError:
        raise FragmentError(f"{CHANGELOG}: no '{LEDGER_HEADING}' section") from None
    rows = [i for i in range(s, e) if lines[i].startswith("|")]
    if len(rows) < 2:
        raise FragmentError(f"{CHANGELOG}: ledger section has no table")
    last = rows[-1]
    new = [ledger_row(f) for f in sorted(frags, key=_sort_key)]
    out = lines[:last + 1] + new + lines[last + 1:]
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def fold_plan(text: str, frags: list[dict]) -> str:
    lines = text.splitlines()
    try:
        s, e = _section_bounds(lines, lambda ln: bool(STATUS_HEADING_RE.match(ln)))
    except FragmentError:
        raise FragmentError(f"{PLAN}: no '## Status' section") from None
    groups: dict[str, list[str]] = {}
    for f in sorted(frags, key=_sort_key):
        if f["status"]:
            groups.setdefault(f["status_group"], []).extend(f["status"])
    if not groups:
        return text
    marker = next((i for i in range(s, e) if MARKER_RE.match(lines[i])), None)
    if marker is None:
        raise FragmentError(f"{PLAN}: the Status section has no change-fragments marker line")
    for group, items in groups.items():
        head = next((i for i in range(s, e) if lines[i].rstrip() == f"{group}:"), None)
        if head is not None:
            j = head + 1
            while j < e and (lines[j].startswith("- ") or lines[j].startswith("  ")):
                j += 1
            lines[j:j] = items
            e += len(items)
            if marker >= j:
                marker += len(items)
        else:
            block = [f"{group}:"] + items + [""]
            lines[marker:marker] = block
            e += len(block)
            marker += len(block)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


# --------------------------------------------------------------------------- #
# Repository I/O
# --------------------------------------------------------------------------- #

def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _decode(b: bytes, name: str) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        raise FragmentError(f"{name}: not UTF-8 text") from None


def load_fragments(root: str) -> list[dict]:
    d = os.path.join(root, CHANGES_DIR)
    rel = []
    for dp, dns, fns in os.walk(d) if os.path.isdir(d) else []:
        rel += [os.path.relpath(os.path.join(dp, x), root).replace(os.sep, "/") for x in fns
                if os.path.isfile(os.path.join(dp, x)) or os.path.islink(os.path.join(dp, x))]
    out = []
    for r in fragment_paths(rel):
        p = os.path.join(root, r)
        if os.path.islink(p) or not os.path.isfile(p):
            raise FragmentError(f"{r}: must be a regular file")
        with open(p, "rb") as f:
            out.append(parse_fragment(_decode(f.read(), r), r))
    return out


def assemble(root: str, write: bool) -> int:
    frags = load_fragments(root)
    if not frags:
        print("No fragments in changes/: nothing to fold.")
        return 0
    cl_p, plan_p = os.path.join(root, CHANGELOG), os.path.join(root, PLAN)
    cl_new = fold_changelog(_read(cl_p), frags)
    plan_new = fold_plan(_read(plan_p), frags)
    for f in sorted(frags, key=_sort_key):
        print(f"{f['name']}: Phase {f['phase']} {f['commit']}"
              f"{' + ' + str(len(f['status'])) + ' Status line(s)' if f['status'] else ''}")
    if not write:
        print(f"Dry run: {len(frags)} fragment(s). Re-run with --write to fold them.")
        return 0
    with open(cl_p, "w", encoding="utf-8") as fh:
        fh.write(cl_new)
    with open(plan_p, "w", encoding="utf-8") as fh:
        fh.write(plan_new)
    for f in frags:
        os.remove(os.path.join(root, f["name"]))
    print(f"Folded {len(frags)} fragment(s) into {CHANGELOG} and {PLAN}; commit the result.")
    return 0


def _git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def _show_bytes(root: str, ref: str, path: str):
    r = subprocess.run(["git", "-C", root, "show", f"{ref}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _show(root: str, ref: str, path: str):
    b = _show_bytes(root, ref, path)
    return None if b is None else _decode(b, f"{path} at {ref}")


def _git_z(root: str, *args: str) -> list[str]:
    """A git command's NUL-delimited output (-z), decoded per path: no quoting
    of non-ASCII or special characters, unlike the line form."""
    out = subprocess.run(["git", "-C", root, *args], check=True, capture_output=True).stdout
    return [t.decode("utf-8", "surrogateescape") for t in out.split(b"\0") if t]


def _fragments_at(root: str, ref: str) -> dict[str, dict]:
    """{path: parsed fragment} for every fragment in the tree at `ref`."""
    names = _git_z(root, "ls-tree", "-r", "-z", "--name-only", ref, "--", CHANGES_DIR)
    out = {}
    for p in fragment_paths(names, f" at {ref}"):
        b = _show_bytes(root, ref, p)
        out[p] = parse_fragment(_decode(b or b"", p), p)
    return out


def check_pr(root: str, base: str) -> list[str]:
    """Problems with the change between merge-base(base, HEAD) and the working
    tree's committed HEAD. Empty list = the PR follows the fragment rule."""
    mb = _git(root, "merge-base", base, "HEAD").strip()
    # -z: paths are NUL-delimited and never quoted (non-ASCII names included).
    toks = _git_z(root, "diff", "--name-status", "--no-renames", "-z", mb, "HEAD")
    pairs = list(zip(toks[0::2], toks[1::2]))
    problems: list[str] = []
    try:
        changed = set(fragment_paths([p for _, p in pairs]))
    except FragmentError as e:
        problems.append(str(e))
        changed = {p for _, p in pairs if p.startswith(CHANGES_DIR + "/") and p.endswith(".md")
                   and "/" not in p[len(CHANGES_DIR) + 1:] and not p.upper().endswith("/README.MD")}
    added = [p for st, p in pairs if p in changed and not st.startswith("D")]
    deleted = [p for st, p in pairs if p in changed and st.startswith("D")]
    for p in added:
        try:
            parse_fragment(_decode(_show_bytes(root, "HEAD", p) or b"", p), p)
        except FragmentError as e:
            problems.append(f"{e}")
    gone = []
    for p in deleted:
        try:
            gone.append(parse_fragment(_decode(_show_bytes(root, mb, p) or b"", p), p))
        except FragmentError as e:
            problems.append(f"deleted fragment {e}")
    # A folded entry must leave the queue: the same entry still present at HEAD
    # under any name (a rename or copy of the consumed fragment) would fold twice.
    if gone:
        try:
            queued = _fragments_at(root, "HEAD")
        except FragmentError:
            queued = {}          # already reported above, through the changed paths
        for g in gone:
            for p, q in queued.items():
                if _same_entry(g, q):
                    problems.append(f"{p}: the entry folded from {g['name']} is still queued under this "
                                    f"name; a fold must remove it")
    for path, region, fold in ((CHANGELOG, ledger_region, fold_changelog),
                               (PLAN, status_region, fold_plan)):
        try:
            old, new = _show(root, mb, path), _show(root, "HEAD", path)
        except FragmentError as e:
            problems.append(str(e))
            continue
        if old is None or new is None:
            if old != new:
                problems.append(f"{path} was added or deleted; it must stay in place")
            continue
        try:
            expect = region(fold(old, gone)) if gone else region(old)
            got = region(new)
        except FragmentError as e:
            problems.append(f"{path}: {e}")
            continue
        if expect == got:
            continue
        what = "ledger table" if path == CHANGELOG else "Status section"
        if got == region(old):
            problems.append(f"{path}: fragments were deleted without folding them into the {what} "
                            f"(run `scripts/assemble_changes.py --write` instead of deleting by hand)")
            continue
        problems.append(
            f"{path}: the {what} was edited directly. Put the row (and any Status line) in a "
            f"new fragment changes/<branch>.md instead (format: changes/README.md); "
            f"only a release, which folds and deletes fragments with "
            f"`scripts/assemble_changes.py --write`, may edit it.")
    return problems


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

_CL = """# Changelog

## Phase commit ledger

| Phase | Commit | Change |
|-------|--------|--------|
| 48 | δ | **Split agents.** Old row. |

## Gate internals (contributor reference)

Text.
"""

_PLAN = """# Plan

## Status (2026-10-30)

Merged to main:
- **A:** things.

{marker}
Before attempt 9:
- a smoke.

## Why

Text.
"""


def _frag(phase, commit, text, group=None, status=()):
    head = f'---\nphase: "{phase}"\ncommit: "{commit}"\n'
    if group:
        head += f'status_group: "{group}"\n'
    body = f"{head}---\n\n## Changelog\n\n{text}\n"
    if status:
        body += "\n## Status\n\n" + "\n".join(status) + "\n"
    return body


def _safe_check(root: str) -> list[str]:
    """check_pr against main, with a crash reported as a problem (self-test)."""
    try:
        return check_pr(root, "main")
    except Exception as e:  # noqa: BLE001
        return [f"CRASH {type(e).__name__}: {e}"]


def _self_test() -> int:
    fails = []

    def ok(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            fails.append(label)

    plan = _PLAN.format(marker=MARKER)
    row_a = "| 49 | α | **Declarations.** Row A. |"
    row_b = "| 49 | γ | **Caveats.** Row B. |"

    # CF1 — the real failure (PR #74's merge commit "keep both CHANGELOG and plan
    # entries", PRs #79-#81 all appending at the same point): two branches that
    # each append a ledger row conflict in git. Reproduced with git merge-file.
    with tempfile.TemporaryDirectory() as td:
        base, a, b = (os.path.join(td, n) for n in ("base", "a", "b"))
        for p, extra in ((base, ""), (a, row_a + "\n"), (b, row_b + "\n")):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(_CL.replace("Old row. |\n", "Old row. |\n" + extra))
        r = subprocess.run(["git", "merge-file", "-p", a, base, b], capture_output=True, text=True)
        ok(r.returncode > 0 and "<<<<<<<" in r.stdout,
           "CF1 two PRs appending a ledger row conflict (the problem being fixed)")

    # CF2 — the same two changes as fragments: distinct files, so no conflict, and
    # the fold carries both rows in phase/letter order whatever the file order.
    fa = parse_fragment(_frag(49, "γ", "**Caveats.** Row B.", "In review (Phase 49):",
                              ["- **γ:** caveats."]), "changes/z-49c.md")
    fb = parse_fragment(_frag(49, "α", "**Declarations.** Row A.", "In review (Phase 49):",
                              ["- **α:** declarations."]), "changes/a-49a.md")
    out = fold_changelog(_CL, [fa, fb])
    ok(row_a + "\n" + row_b in out and out.index("Old row.") < out.index("Row A."),
       "CF2 fold appends both rows after the last ledger row, α before γ")
    ok(out.split("## Gate internals")[1] == _CL.split("## Gate internals")[1],
       "CF3 fold leaves the rest of CHANGELOG.md untouched")

    # CF4 — Status lines: one new group above the marker, both lines in it.
    p2 = fold_plan(plan, [fa, fb])
    ok("In review (Phase 49):\n- **α:** declarations.\n- **γ:** caveats.\n\n" + MARKER in p2,
       "CF4 Status lines of one group land together above the marker")
    fc = parse_fragment(_frag(49, "δ", "**More.**", "In review (Phase 49):", ["- **δ:** more."]), "changes/c.md")
    p3 = fold_plan(p2, [fc])
    ok("- **γ:** caveats.\n- **δ:** more.\n" in p3 and p3.count("In review (Phase 49):") == 1,
       "CF5 a later fragment joins its existing Status group")
    ok(fold_plan(plan, [parse_fragment(_frag(51, "α", "x"), "n")]) == plan,
       "CF6 a fragment without Status lines leaves the plan unchanged")
    try:
        fold_plan(_PLAN.format(marker=""), [fa])
        ok(False, "CF7 missing marker raises")
    except FragmentError:
        ok(True, "CF7 a Status section without the marker fails closed")

    # CF8 — fragment parsing fails closed.
    bad = {
        "no header": "## Changelog\n\nx\n",
        "unclosed header": '---\nphase: "1"\n## Changelog\nx\n',
        "unknown key": '---\nphase: "1"\ncommit: "α"\nowner: me\n---\n## Changelog\nx\n',
        "no changelog": '---\nphase: "1"\ncommit: "α"\n---\n',
        "empty changelog": '---\nphase: "1"\ncommit: "α"\n---\n## Changelog\n\n',
        "status without group": '---\nphase: "1"\ncommit: "α"\n---\n## Changelog\nx\n## Status\n- y\n',
        "non-bullet status": '---\nphase: "1"\ncommit: "α"\nstatus_group: G\n---\n## Changelog\nx\n## Status\ny\n',
        "list header": "---\n- 1\n---\n## Changelog\nx\n",
        "bad date": '---\nphase: 2026-02-30\ncommit: "α"\n---\n## Changelog\nx\n',
        "alias loop": '---\nphase: &a [*a]\ncommit: "α"\n---\n## Changelog\nx\n',
        "unknown section": '---\nphase: "1"\ncommit: "α"\n---\n## Notes\nx\n',
    }
    for label, txt in bad.items():
        try:
            parse_fragment(txt, label)
            ok(False, f"CF8 rejects fragment: {label}")
        except FragmentError:
            ok(True, f"CF8 rejects fragment: {label}")
    f9 = parse_fragment(_frag(50, "β", "a | b\nwraps here"), "n")
    ok(ledger_row(f9) == "| 50 | β | a \\| b wraps here |", "CF9 pipes escaped, lines folded into one cell")

    # CF10-CF14 — the PR check on a scratch git repository.
    with tempfile.TemporaryDirectory() as td:
        def git(*a):
            subprocess.run(["git", "-C", td, *a], check=True, capture_output=True)

        def put(rel, txt):
            p = os.path.join(td, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(txt)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        git("config", "commit.gpgsign", "false")
        put(CHANGELOG, _CL)
        put(PLAN, plan)
        git("add", "-A")
        git("commit", "-q", "-m", "base")

        git("checkout", "-q", "-b", "direct")
        put(CHANGELOG, _CL.replace("Old row. |\n", "Old row. |\n" + row_a + "\n"))
        git("commit", "-qam", "direct edit")
        pr = check_pr(td, "main")
        ok(len(pr) == 1 and "edited directly" in pr[0], "CF10 a direct ledger edit fails the check")

        git("checkout", "-q", "main")
        git("checkout", "-q", "-b", "frag")
        put("changes/phase49a.md", _frag(49, "α", "**Declarations.** Row A.", "In review (Phase 49):",
                                         ["- **α:** declarations."]))
        put(CHANGELOG, _CL + "\nA gate-internals note.\n")
        git("add", "-A")
        git("commit", "-qm", "fragment")
        ok(check_pr(td, "main") == [], "CF11 a fragment plus an edit outside the ledger passes")

        git("checkout", "-q", "main")
        git("merge", "-q", "--no-ff", "-m", "merge frag", "frag")
        git("checkout", "-q", "-b", "release")
        ok(assemble(td, write=True) == 0 and not os.path.exists(os.path.join(td, "changes/phase49a.md")),
           "CF12 --write folds and deletes the fragment")
        git("add", "-A")
        git("commit", "-qm", "release")
        ok(check_pr(td, "main") == [], "CF13 a release PR (exact fold of deleted fragments) passes")
        with open(os.path.join(td, PLAN), encoding="utf-8") as fh:
            ok("- **α:** declarations." in fh.read(), "CF13b the release carried the Status line")

        put(PLAN, _read(os.path.join(td, PLAN)).replace("- a smoke.", "- a smoke.\n- extra."))
        git("commit", "-qam", "release plus a direct Status edit")
        pr = check_pr(td, "main")
        ok(any("Status section was edited directly" in x for x in pr),
           "CF14 a release that also edits the Status section by hand fails")

        git("checkout", "-q", "main")
        git("checkout", "-q", "-b", "badfrag")
        put("changes/broken.md", "no header\n")
        git("add", "-A")
        git("commit", "-qm", "bad fragment")
        ok(any("broken.md" in x for x in check_pr(td, "main")), "CF15 an unparseable fragment fails the check")

        git("checkout", "-q", "main")
        git("checkout", "-q", "-b", "dropped")
        os.remove(os.path.join(td, "changes/phase49a.md"))
        git("commit", "-qam", "delete a fragment by hand")
        ok(any("without folding" in x for x in check_pr(td, "main")),
           "CF17 a fragment deleted without folding fails the check")

        git("checkout", "-q", "main")
        git("checkout", "-q", "-b", "marker")
        put(PLAN, plan.replace(MARKER + "\n", "").replace("Merged to main:", MARKER + "\nMerged to main:"))
        git("commit", "-qam", "move marker")
        ok(check_pr(td, "main") == [], "CF16 moving the marker line is not a Status edit")

        # Codex round 1 (P1s on #83), each reproduced here before the fix.
        frag_a = _frag(49, "α", "**Declarations.** Row A.", "In review (Phase 49):", ["- **α:** declarations."])
        git("checkout", "-q", "main")
        git("checkout", "-q", "-b", "unicode")
        put("changes/phase49-café.md", "no header\n")
        git("add", "-A")
        git("commit", "-qm", "non-ASCII fragment name")
        ok(any("café" in x for x in _safe_check(td)),
           "CF18 a fragment with a non-ASCII name is recognised (NUL-delimited diff), and checked")

        git("checkout", "-q", "main")          # main still queues changes/phase49a.md (CF11)
        git("checkout", "-q", "-b", "renamed-survivor")
        assemble(td, write=True)
        put("changes/phase49a-again.md", frag_a)
        git("add", "-A")
        git("commit", "-qm", "fold, but the entry stays queued under a new name")
        ok(any("still queued" in x for x in _safe_check(td)),
           "CF19 a folded fragment that survives under a new name (rename/copy) fails")

        git("checkout", "-q", "main")
        git("checkout", "-q", "-b", "nested")
        put("changes/feature/x.md", frag_a.replace("Row A", "Row N"))
        git("add", "-A")
        git("commit", "-qm", "nested fragment")
        pr = _safe_check(td)
        ok(any("nested" in x for x in pr), f"CF20 a nested path under changes/ fails the check ({pr})")
        try:
            assemble(td, write=False)
            ok(False, "CF21 assembly refuses a nested fragment")
        except FragmentError:
            ok(True, "CF21 assembly refuses a nested fragment (one discovery rule for CI and release)")
        ok(fragment_name("feature/phase51a") == "feature__phase51a.md",
           "CF22 a branch name with '/' maps to a flat fragment name")

        git("checkout", "-q", "main")
        git("checkout", "-q", "-b", "badbytes")
        with open(os.path.join(td, "changes", "bad.md"), "wb") as fh:
            fh.write(b"---\nphase: \"1\"\ncommit: \"\xff\"\n---\n## Changelog\nx\n")
        git("add", "-A")
        git("commit", "-qm", "undecodable fragment")
        pr = _safe_check(td)
        ok(any("bad.md" in x and "UTF-8" in x for x in pr), f"CF23 an undecodable fragment is reported invalid ({pr})")
        git("checkout", "-q", "main")

    for sep, label in (("\\n", "newline"), ("\\u2028", "line separator"), ("\\r", "carriage return")):
        try:
            parse_fragment(f'---\nphase: "51{sep}| 99 | z"\ncommit: "α"\n---\n## Changelog\nx\n', "n")
            ok(False, f"CF24 rejects a {label} in phase")
        except FragmentError:
            ok(True, f"CF24 rejects a {label} in phase")
    try:
        parse_fragment('---\nphase: "51"\ncommit: "α\\u2029"\n---\n## Changelog\nx\n', "n")
        ok(False, "CF24 rejects a paragraph separator in commit")
    except FragmentError:
        ok(True, "CF24 rejects a paragraph separator in commit")

    if fails:
        print(f"FAILED: {len(fails)} self-test case(s).")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="fold the fragments and delete them")
    ap.add_argument("--check-pr", metavar="BASE", help="check HEAD against BASE (CI on pull requests)")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--name", metavar="BRANCH", help="print the fragment path for a branch name")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.name:
        print(f"{CHANGES_DIR}/{fragment_name(a.name)}")
        return 0
    try:
        if a.check_pr:
            problems = check_pr(a.root, a.check_pr)
            for p in problems:
                print(f"::error::{p}" if os.environ.get("GITHUB_ACTIONS") else f"ERROR: {p}")
            if not problems:
                print("OK: CHANGELOG.md ledger and plan Status changed only through fragments.")
            return 1 if problems else 0
        return assemble(a.root, a.write)
    except (FragmentError, subprocess.CalledProcessError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
