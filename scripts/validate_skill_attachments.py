#!/usr/bin/env python3
"""Skill attachment and frontmatter validator.

Checks that every skill referenced by path under `agents/` resolves to a
`.claude/skills/<name>/SKILL.md` file in this repository and that every
`SKILL.md` opens with valid YAML frontmatter (`name` matching its directory
and non-empty `description`).

Usage:
    python3 scripts/validate_skill_attachments.py             # check repo
    python3 scripts/validate_skill_attachments.py --self-test # inline tests
"""

from __future__ import annotations

import argparse
import os
import re
import sys


def _repo_root() -> str:
    """Best-effort repo root: parent of scripts/ unless overridden."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_SKILL_PATH = re.compile(r"""\.claude["']?\s*[,/]\s*["']?skills["']?\s*[,/]\s*["']?([A-Za-z0-9_-]+)["']?\s*[,/]\s*["']?SKILL\.md""")


def parse_task_skill_paths(agents_dir: str) -> set[str]:
    """Skills a module under agents/ loads by their SKILL.md path, as v2's bounded tasks do
    (agents/v2_sdk_tasks.py: os.path.join(R, ".claude", "skills", "decision-spec", "SKILL.md")). The v1
    registry's `skills=[...]` lists went with pipeline v1 (owner, 2026-11-20)."""
    found: set[str] = set()
    if not os.path.isdir(agents_dir):
        return found
    for fn in sorted(os.listdir(agents_dir)):
        if fn.endswith(".py"):
            try:
                src = open(os.path.join(agents_dir, fn)).read()
            except OSError:
                continue
            try:                       # a path in a comment is no attachment: drop comment tokens first
                import io
                import tokenize
                toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type != tokenize.COMMENT]
                src = tokenize.untokenize(toks)
            except (tokenize.TokenError, SyntaxError, IndentationError):
                pass
            found.update(_SKILL_PATH.findall(src))
    return found


def list_existing_skills(skills_root: str) -> set[str]:
    """Return the set of skill directories under .claude/skills/ that
    contain a SKILL.md file (skills without SKILL.md don't count)."""
    if not os.path.isdir(skills_root):
        return set()
    out: set[str] = set()
    for entry in os.listdir(skills_root):
        skill_md = os.path.join(skills_root, entry, "SKILL.md")
        if os.path.isfile(skill_md):
            out.add(entry)
    return out


def _split_frontmatter(text: str) -> tuple[str | None, str | None]:
    """(yaml_text, None) for a leading `---` … `---` block, else (None, reason)."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None, "no frontmatter (file must start with a '---' line)"
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            return "\n".join(lines[1:i]), None
    return None, "frontmatter is not closed by a '---' line"


def check_frontmatter(skill_md_path: str) -> str | None:
    """Return None when the SKILL.md frontmatter is valid, else a short
    reason string. Valid means: file starts with a `---` line, a closing
    `---` line exists, the block parses as a YAML mapping, and `name`
    (equal to the directory name) and `description` are non-empty
    strings."""
    import yaml

    try:
        with open(skill_md_path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return f"unreadable: {e}"
    block, why = _split_frontmatter(text)
    if block is None:
        return why
    try:
        meta = yaml.safe_load(block)
    except yaml.YAMLError as e:
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        return f"frontmatter is not valid YAML ({first}); quote the description or use '>-'"
    if not isinstance(meta, dict):
        return "frontmatter is not a YAML mapping"
    expected = os.path.basename(os.path.dirname(skill_md_path))
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        return "frontmatter missing non-empty 'name'"
    if name != expected:
        return f"frontmatter name '{name}' does not match directory '{expected}'"
    desc = meta.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return "frontmatter missing non-empty 'description'"
    return None


def validate(repo_root: str | None = None) -> dict:
    """Cross-check the attached set against the existing set."""
    root = repo_root or _repo_root()
    attached = parse_task_skill_paths(os.path.join(root, "agents"))
    skills_root = os.path.join(root, ".claude", "skills")
    existing = list_existing_skills(skills_root)
    invalid_frontmatter = []
    for name in sorted(existing):
        reason = check_frontmatter(os.path.join(skills_root, name, "SKILL.md"))
        if reason is not None:
            invalid_frontmatter.append((name, reason))
    phantoms = sorted(attached - existing)
    orphans = sorted(existing - attached)
    return {
        "attached": sorted(attached),
        "existing": sorted(existing),
        "phantoms": phantoms,
        "orphans": orphans,
        "invalid_frontmatter": invalid_frontmatter,
    }


def _run_self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # T10-T14 (Phase 21 ε): frontmatter validation.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "agents"))

        def _skill(name: str, body: str) -> None:
            os.makedirs(os.path.join(d, ".claude", "skills", name))
            with open(os.path.join(d, ".claude", "skills", name, "SKILL.md"), "w") as f:
                f.write(body)

        _skill("good", "---\nname: good\ndescription: >-\n  Uses a: colon safely.\n---\n# good\n")
        _skill("colon", "---\nname: colon\ndescription: Duties: audit things.\n---\n# colon\n")
        _skill("bare", "# bare\n\nNo frontmatter at all.\n")
        _skill("wrongname", "---\nname: other\ndescription: x\n---\n")
        _skill("nodesc", "---\nname: nodesc\n---\n")
        r = validate(d)
        bad = dict(r["invalid_frontmatter"])
        ok("good" not in bad,
           f"T10: folded description with a colon is valid, got {bad.get('good')}")
        ok("not valid YAML" in bad.get("colon", ""),
           f"T11: unquoted 'Duties: ...' must fail YAML parse, got {bad.get('colon')}")
        ok("no frontmatter" in bad.get("bare", ""),
           f"T12: SKILL.md without frontmatter must fail, got {bad.get('bare')}")
        ok("does not match" in bad.get("wrongname", ""),
           f"T13: name/dir mismatch must fail, got {bad.get('wrongname')}")
        ok("description" in bad.get("nodesc", ""),
           f"T14: missing description must fail, got {bad.get('nodesc')}")
        ok(r["phantoms"] == [],
           f"T10-T14: frontmatter failures are not phantoms, got {r['phantoms']}")

    # T15: a v2 task loads a skill by its SKILL.md path (agents/v2_sdk_tasks.py); that counts as
    # an attachment, and a path to a skill that does not exist is a phantom.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "agents"))
        os.makedirs(os.path.join(d, ".claude", "skills", "spec-x"))
        open(os.path.join(d, ".claude", "skills", "spec-x", "SKILL.md"), "w").write("# x\n")
        open(os.path.join(d, "agents", "tasks.py"), "w").write(
            'import os\nA = os.path.join(R, ".claude", "skills", "spec-x", "SKILL.md")\n'
            'B = ".claude/skills/missing-y/SKILL.md"\n')
        r15 = validate(d)
        ok(r15["attached"] == ["missing-y", "spec-x"] and r15["phantoms"] == ["missing-y"],
           f"T15: skills a task loads by path are attached; a missing one is a phantom: {r15}")
    real = validate()
    ok("decision-spec" in real["attached"], f"T16: the v2 decision-spec skill counts as attached: {real['attached']}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--repo-root", default=None,
                   help="Override repo root (default: parent of scripts/)")
    p.add_argument("--show-orphans", action="store_true",
                   help="Also print orphan skills (exist but not attached). "
                        "Orphans are not failures — many are intentional.")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()

    result = validate(args.repo_root)
    print(f"Attached:  {len(result['attached'])} skills "
          f"loaded by agents/ (SKILL.md paths)", file=sys.stderr)
    print(f"Existing:  {len(result['existing'])} SKILL.md files "
          f"under .claude/skills/", file=sys.stderr)
    print(f"Phantoms:  {len(result['phantoms'])} attached but no "
          f"SKILL.md", file=sys.stderr)
    if result["phantoms"]:
        for name in result["phantoms"]:
            print(f"  - {name}", file=sys.stderr)
    if result["invalid_frontmatter"]:
        print(f"Invalid:   {len(result['invalid_frontmatter'])} SKILL.md "
              f"file(s) with missing or malformed frontmatter", file=sys.stderr)
        for name, reason in result["invalid_frontmatter"]:
            print(f"  - {name}: {reason}", file=sys.stderr)
    if args.show_orphans:
        print(f"Orphans:   {len(result['orphans'])} have SKILL.md "
              f"but no agent attaches them", file=sys.stderr)
        for name in result["orphans"]:
            print(f"  - {name}", file=sys.stderr)

    return 1 if (result["phantoms"] or result["invalid_frontmatter"]) else 0


if __name__ == "__main__":
    sys.exit(main())
