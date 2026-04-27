#!/usr/bin/env python3
"""
Phase 10 Commit φ — skill attachment validator.

The deep review at the end of Phase 9 found 26 of 48 skills (54%)
were never attached to any agent and 3 names referenced in
agents/__init__.py had no corresponding `.claude/skills/<name>/`
directory. The 0013 run's RIG-001/002/003 unresolvable-blocker
pattern is partly a symptom: gates fire HIGH and the modeler has no
attached skill explaining the escalation path.

This validator parses every `skills=[...]` reference in
`agents/__init__.py` and checks that each name resolves to either:
- a `.claude/skills/<name>/SKILL.md` file in this repo, OR
- an entry in the `_MCP_SKILLS` allowlist (skills provided by an
  external MCP server like the Asta family of literature tools).

Usage:
    python3 scripts/validate_skill_attachments.py             # check repo
    python3 scripts/validate_skill_attachments.py --self-test # inline tests

Exit:
    0 — every attached skill resolves
    1 — at least one phantom (attached but no SKILL.md and not in allowlist)
"""

from __future__ import annotations

import argparse
import os
import re
import sys


# Skills referenced by `skills=[...]` in agents/__init__.py that are
# provided by an external MCP server rather than a local SKILL.md.
# These are NOT phantoms — they're loaded dynamically by the harness
# from the Asta / external MCP stack at runtime.
_MCP_SKILLS = {
    "asta-literature-search",
    "pdf-text-extraction",
    "semantic-scholar-lookup",
}


def _repo_root() -> str:
    """Best-effort repo root: parent of scripts/ unless overridden."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_attached_skills(agents_init_path: str) -> set[str]:
    """Return the set of skill names referenced in any `skills=[...]`
    list in agents/__init__.py. Handles multi-line lists. Ignores
    comments. Reuses no project utility — this is a one-off parse."""
    with open(agents_init_path) as f:
        src = f.read()
    attached: set[str] = set()
    # Match `skills = [` (or `skills=[`) and capture up to the closing
    # `]`. Multi-line and comments are tolerated; we extract every
    # double-quoted identifier inside the list body.
    for m in re.finditer(r"skills\s*=\s*\[(.*?)\]", src, re.DOTALL):
        body = m.group(1)
        # Strip line comments before extracting names — comments could
        # contain hyphenated phrases that look like skill names.
        body = re.sub(r"#[^\n]*", "", body)
        # The character class includes `_` because at least one real
        # skill (`basic_epi_modeling`) is snake-cased. Without the
        # underscore, the regex silently dropped that attachment and
        # would also fail to flag a misspelling like
        # `basic_epi_modelling` as a phantom (review fix #1).
        for name in re.findall(r'"([a-z][a-z0-9_-]+)"', body):
            attached.add(name)
    return attached


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


def validate(repo_root: str | None = None) -> dict:
    """Cross-check the attached set against the existing set.

    Returns a dict with:
      attached: sorted list of skills referenced in agents/__init__.py
      existing: sorted list of skills with a SKILL.md
      phantoms: attached but neither in existing nor in _MCP_SKILLS
      orphans: existing but never attached
    """
    root = repo_root or _repo_root()
    attached = parse_attached_skills(os.path.join(root, "agents", "__init__.py"))
    existing = list_existing_skills(os.path.join(root, ".claude", "skills"))
    phantoms = sorted(attached - existing - _MCP_SKILLS)
    orphans = sorted(existing - attached)
    return {
        "attached": sorted(attached),
        "existing": sorted(existing),
        "phantoms": phantoms,
        "orphans": orphans,
    }


def _run_self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as d:
        # Set up a fake repo with agents/__init__.py and .claude/skills/
        os.makedirs(os.path.join(d, "agents"))
        os.makedirs(os.path.join(d, ".claude", "skills"))

        # T1: a skill attached and existing → no phantom.
        os.makedirs(os.path.join(d, ".claude", "skills", "alpha"))
        with open(os.path.join(d, ".claude", "skills", "alpha", "SKILL.md"), "w") as f:
            f.write("# alpha\n")
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write('skills=["alpha"]\n')
        r1 = validate(d)
        ok(r1["phantoms"] == [],
           f"T1: alpha attached and exists; phantoms should be empty, got {r1['phantoms']}")
        ok(r1["orphans"] == [],
           f"T1: alpha attached; should not be orphan, got {r1['orphans']}")

        # T2: a skill attached but no SKILL.md → phantom (unless MCP).
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write('skills=["alpha", "ghost"]\n')
        r2 = validate(d)
        ok(r2["phantoms"] == ["ghost"],
           f"T2: ghost attached without SKILL.md should be phantom, got {r2['phantoms']}")

        # T3: a skill in _MCP_SKILLS allowlist is NOT a phantom even
        # without a local SKILL.md.
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write('skills=["alpha", "asta-literature-search"]\n')
        r3 = validate(d)
        ok("asta-literature-search" not in r3["phantoms"],
           f"T3: MCP allowlist member should not be phantom, got {r3['phantoms']}")

        # T4: a skill exists with SKILL.md but is never attached → orphan.
        os.makedirs(os.path.join(d, ".claude", "skills", "beta"))
        with open(os.path.join(d, ".claude", "skills", "beta", "SKILL.md"), "w") as f:
            f.write("# beta\n")
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write('skills=["alpha"]\n')
        r4 = validate(d)
        ok(r4["orphans"] == ["beta"],
           f"T4: beta exists without attachment should be orphan, got {r4['orphans']}")

        # T5: multi-line skills=[...] lists are parsed correctly. Many
        # of the real attachments span 5+ lines.
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write(
                'skills=[\n'
                '    "alpha",\n'
                '    "beta",\n'
                '    # a comment that mentions skill-x but not as a real ref\n'
                '    "asta-literature-search",\n'
                ']\n'
            )
        r5 = validate(d)
        ok(set(r5["attached"]) == {"alpha", "beta", "asta-literature-search"},
           f"T5: multi-line list should parse 3 names, got {r5['attached']}")
        ok("skill-x" not in r5["attached"],
           "T5: comment-mentioned name should not be attached")
        ok(r5["phantoms"] == [],
           f"T5: all three resolve; no phantoms expected, got {r5['phantoms']}")

        # T6: a skill directory without a SKILL.md file does NOT count
        # as existing — catches half-finished skill dirs.
        os.makedirs(os.path.join(d, ".claude", "skills", "halffinished"))
        # No SKILL.md inside.
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write('skills=["halffinished"]\n')
        r6 = validate(d)
        ok(r6["phantoms"] == ["halffinished"],
           f"T6: dir without SKILL.md should be phantom when attached, got {r6}")

        # T7 (review fix #1): snake_case skill names must be parsed and
        # cross-checked. A real skill `basic_epi_modeling` exists and is
        # attached in agents/__init__.py; the original regex dropped it
        # because the character class lacked `_`. This case verifies the
        # fix by registering both a present snake_case skill (no phantom)
        # and a misspelled one (phantom).
        os.makedirs(os.path.join(d, ".claude", "skills", "basic_epi_modeling"))
        with open(os.path.join(d, ".claude", "skills",
                               "basic_epi_modeling", "SKILL.md"), "w") as f:
            f.write("# basic_epi_modeling\n")
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write('skills=["basic_epi_modeling", "basic_epi_modelling"]\n')
        r7 = validate(d)
        ok("basic_epi_modeling" in r7["attached"],
           f"T7: snake_case skill should be parsed as attached, got "
           f"{r7['attached']}")
        ok(r7["phantoms"] == ["basic_epi_modelling"],
           f"T7: snake_case misspelling should be flagged as phantom, "
           f"got {r7['phantoms']}")

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
          f"in agents/__init__.py", file=sys.stderr)
    print(f"Existing:  {len(result['existing'])} SKILL.md files "
          f"under .claude/skills/", file=sys.stderr)
    print(f"Phantoms:  {len(result['phantoms'])} attached but no "
          f"SKILL.md (and not in MCP allowlist)", file=sys.stderr)
    if result["phantoms"]:
        for name in result["phantoms"]:
            print(f"  - {name}", file=sys.stderr)
    if args.show_orphans:
        print(f"Orphans:   {len(result['orphans'])} have SKILL.md "
              f"but no agent attaches them", file=sys.stderr)
        for name in result["orphans"]:
            print(f"  - {name}", file=sys.stderr)

    return 1 if result["phantoms"] else 0


if __name__ == "__main__":
    sys.exit(main())
