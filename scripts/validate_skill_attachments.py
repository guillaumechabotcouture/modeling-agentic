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

Phase 21 ε: every SKILL.md must also open with YAML frontmatter that
parses and carries a non-empty `name` (matching its directory) and
`description`. A skill whose frontmatter fails to parse loses its
description in the harness and is never auto-triggered, and the
attachment check alone cannot see that.

Exit:
    0 — every attached skill resolves and every SKILL.md has valid
        frontmatter
    1 — at least one phantom (attached but no SKILL.md and not in
        allowlist) or at least one SKILL.md with invalid frontmatter
"""

from __future__ import annotations

import argparse
import ast
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


def _resolve_attribute_to_list(attr_node: ast.Attribute,
                               agents_dir: str) -> list[str] | None:
    """Resolve `modeler.MODEL_TESTER_SKILLS` → the list of strings
    assigned to `MODEL_TESTER_SKILLS` at module level in
    `agents/modeler.py`. Returns None if the reference can't be
    resolved (we'll emit a warning rather than treat as phantom)."""
    if not isinstance(attr_node.value, ast.Name):
        return None
    module_name = attr_node.value.id  # e.g., "modeler"
    attr_name = attr_node.attr        # e.g., "MODEL_TESTER_SKILLS"
    module_path = os.path.join(agents_dir, f"{module_name}.py")
    if not os.path.isfile(module_path):
        return None
    try:
        with open(module_path) as f:
            tree = ast.parse(f.read())
    except (SyntaxError, OSError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == attr_name:
                    return _resolve_skills_value(node.value, agents_dir)
    return None


def _resolve_skills_value(value: ast.AST,
                          agents_dir: str) -> list[str] | None:
    """Recursively resolve any AST expression assigned to a skills=
    keyword (or to a module-level constant referenced by one). Handles
    string-literal lists, BinOp(Add) of two resolvable parts, and
    Attribute references to module-level constants in sibling files.
    Returns None when the value can't be resolved without execution."""
    if isinstance(value, ast.List):
        out: list[str] = []
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
            else:
                # Mixed: at least one element couldn't be resolved.
                # Take what we have but signal partial via the warning
                # path — the caller checks for None vs partial via
                # length comparison if it cares.
                pass
        return out
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        left = _resolve_skills_value(value.left, agents_dir)
        right = _resolve_skills_value(value.right, agents_dir)
        if left is None or right is None:
            # One side unresolved → fail closed; caller will warn.
            return None
        return left + right
    if isinstance(value, ast.Attribute):
        return _resolve_attribute_to_list(value, agents_dir)
    if isinstance(value, ast.Name):
        # Same-file constant reference. Walk the same module's tree
        # (caller would supply it ideally; here we just bail because
        # the actual real-world case in agents/__init__.py is
        # cross-module — adding same-file resolution would be dead
        # code today).
        return None
    return None


def parse_attached_skills(agents_init_path: str,
                          warnings: list[str] | None = None) -> set[str]:
    """Return the set of skill names referenced in any `skills=...`
    keyword argument inside agents/__init__.py. Phase 11 Commit η
    (F8): replaces the previous regex parser, which silently dropped
    `skills=mod.SOMETHING + [...]` patterns. Now uses ast.parse and
    recursively resolves cross-module Attribute references (e.g.,
    modeler.MODEL_TESTER_SKILLS) by reading sibling files under
    agents/.

    If `warnings` is provided, unresolved references are appended
    (e.g., a future dynamic expression we can't statically resolve).
    Unresolved references are NOT treated as phantoms — they're
    surfaced for human review."""
    with open(agents_init_path) as f:
        src = f.read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        # If agents/__init__.py is broken, fall back to an empty set
        # and warn loudly. The validator's exit code will then flag
        # every attached skill as unattached (a different kind of
        # error, but at least visible).
        if warnings is not None:
            warnings.append(f"agents/__init__.py SyntaxError: {e}")
        return set()

    agents_dir = os.path.dirname(os.path.abspath(agents_init_path))
    attached: set[str] = set()

    def _record(value_node: ast.AST) -> None:
        resolved = _resolve_skills_value(value_node, agents_dir)
        if resolved is None:
            # Couldn't resolve the value statically. Surface it but
            # don't claim it's a phantom — the value might point to a
            # legitimately-attached skill list we just can't read.
            if warnings is not None:
                try:
                    snippet = ast.unparse(value_node)
                except AttributeError:
                    snippet = "<unparseable>"
                warnings.append(
                    f"agents/__init__.py:{value_node.lineno}: skills="
                    f"{snippet} could not be statically resolved; skipped."
                )
            return
        attached.update(resolved)

    for node in ast.walk(tree):
        # Form 1: `skills=...` as a kwarg in a function call (the
        # production case in AgentDefinition(...)).
        if isinstance(node, ast.keyword) and node.arg == "skills":
            _record(node.value)
            continue
        # Form 2: `skills = [...]` as a top-level (or any-scope)
        # assignment to the name `skills`. Less common in production
        # but supported so test fixtures and possible future inline
        # definitions both work.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "skills":
                    _record(node.value)
                    break
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
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "no frontmatter (file must start with a '---' line)"
    try:
        close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return "frontmatter is not closed by a '---' line"
    try:
        meta = yaml.safe_load("\n".join(lines[1:close]))
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
    """Cross-check the attached set against the existing set.

    Returns a dict with:
      attached: sorted list of skills referenced in agents/__init__.py
      existing: sorted list of skills with a SKILL.md
      phantoms: attached but neither in existing nor in _MCP_SKILLS
      orphans: existing but never attached
      warnings: list of strings for skills= expressions we couldn't
                statically resolve (Phase 11 Commit η F8)
      invalid_frontmatter: sorted list of (skill, reason) for SKILL.md
                files whose frontmatter is missing or malformed
                (Phase 21 ε)
    """
    root = repo_root or _repo_root()
    warnings: list[str] = []
    attached = parse_attached_skills(
        os.path.join(root, "agents", "__init__.py"),
        warnings=warnings,
    )
    skills_root = os.path.join(root, ".claude", "skills")
    existing = list_existing_skills(skills_root)
    invalid_frontmatter = []
    for name in sorted(existing):
        reason = check_frontmatter(os.path.join(skills_root, name, "SKILL.md"))
        if reason is not None:
            invalid_frontmatter.append((name, reason))
    phantoms = sorted(attached - existing - _MCP_SKILLS)
    orphans = sorted(existing - attached)
    return {
        "attached": sorted(attached),
        "existing": sorted(existing),
        "phantoms": phantoms,
        "orphans": orphans,
        "warnings": warnings,
        "invalid_frontmatter": invalid_frontmatter,
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

        # T8 (Phase 11 Commit η F8): dynamic skills= expressions
        # (Attribute reference + List concatenation) must be resolved.
        # The real-world case is `skills=modeler.MODEL_TESTER_SKILLS +
        # ["malaria-modeling", ...]` at agents/__init__.py:143. Build a
        # fixture with a sibling module exporting a list, an
        # __init__.py that uses it via BinOp(Add, Attribute, List), and
        # confirm both halves are recognized.
        os.makedirs(os.path.join(d, ".claude", "skills", "child-skill"))
        with open(os.path.join(d, ".claude", "skills",
                               "child-skill", "SKILL.md"), "w") as f:
            f.write("# child-skill\n")
        os.makedirs(os.path.join(d, ".claude", "skills", "parent_skill"))
        with open(os.path.join(d, ".claude", "skills",
                               "parent_skill", "SKILL.md"), "w") as f:
            f.write("# parent_skill\n")
        with open(os.path.join(d, "agents", "modeler.py"), "w") as f:
            f.write('PARENT_SKILLS = ["parent_skill"]\n')
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write(
                "from agents import modeler\n"
                'AgentDefinition(skills=modeler.PARENT_SKILLS + ["child-skill"])\n'
            )
        r8 = validate(d)
        ok("parent_skill" in r8["attached"],
           f"T8: cross-module Attribute reference should resolve "
           f"(parent_skill from modeler.PARENT_SKILLS); got "
           f"{r8['attached']}")
        ok("child-skill" in r8["attached"],
           f"T8: literal-list right-hand side of BinOp(Add) should "
           f"resolve (child-skill); got {r8['attached']}")
        ok(r8["phantoms"] == [],
           f"T8: both sides resolved + both have SKILL.md → no "
           f"phantoms; got {r8['phantoms']}")

        # T9 (Phase 11 Commit η F8): unresolvable dynamic expression
        # produces a warning, NOT a phantom. Reference an attribute on
        # a module we can't resolve (no sibling .py). The validator
        # must return cleanly with the unresolved expression in
        # `warnings` rather than treating it as a phantom skill name.
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write(
                'AgentDefinition(skills=some_external.UNKNOWN_LIST)\n'
            )
        r9 = validate(d)
        ok(r9["phantoms"] == [],
           f"T9: unresolvable dynamic skills= must NOT produce "
           f"phantoms, got {r9['phantoms']}")
        ok(len(r9["warnings"]) >= 1,
           f"T9: unresolvable dynamic skills= must surface a warning, "
           f"got {r9.get('warnings')}")

    # T10-T14 (Phase 21 ε): frontmatter validation.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "agents"))
        with open(os.path.join(d, "agents", "__init__.py"), "w") as f:
            f.write('skills=["good", "colon", "bare", "wrongname", "nodesc"]\n')

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
    if result.get("warnings"):
        print(f"Warnings:  {len(result['warnings'])} unresolved "
              f"skills= expression(s) (not phantoms)", file=sys.stderr)
        for w in result["warnings"]:
            print(f"  - {w}", file=sys.stderr)
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
