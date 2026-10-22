#!/usr/bin/env python3
"""On-demand skill index (architecture v2 §3.3, experiment E1).

One line per skill (name + ≤20-word purpose from its frontmatter description),
so an agent can load a skill with the Skill tool only when it needs it instead
of carrying every SKILL.md in its standing context.

    python3 agents/skill_index.py            # print the index
    python3 agents/skill_index.py --self-test
"""
from __future__ import annotations

import glob
import os
import re
import sys

import yaml

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SKILLS = os.path.join(REPO, ".claude", "skills")
MAX_WORDS = 20

HEADER = ("## Skills (load on demand)\n"
          "Detailed guides exist for the topics below. They are NOT preloaded. When you need one, call the "
          "Skill tool with its name (e.g. `Skill(skill=\"identifiability-analysis\")`) and read only what "
          "you need. The contract packet above already states what the checks require; skills add how-to "
          "detail and worked examples.\n")


def _frontmatter(path):
    text = open(path, errors="replace").read()
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def _purpose(desc: str) -> str:
    desc = re.sub(r"\s+", " ", str(desc or "")).strip()
    first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0]
    words = first.split()
    short = " ".join(words[:MAX_WORDS])
    return short + ("…" if len(words) > MAX_WORDS else "")


def entries():
    out = []
    for p in sorted(glob.glob(os.path.join(SKILLS, "*", "SKILL.md"))):
        fm = _frontmatter(p)
        name = fm.get("name") or os.path.basename(os.path.dirname(p))
        out.append((name, _purpose(fm.get("description", ""))))
    return out


def build_index() -> str:
    return HEADER + "\n".join(f"- `{n}`: {d}" for n, d in entries()) + "\n"


def _self_test() -> int:
    failures = []
    idx = build_index()
    es = entries()
    n_dirs = len(glob.glob(os.path.join(SKILLS, "*", "SKILL.md")))
    if len(es) != n_dirs:
        failures.append(f"index has {len(es)} entries for {n_dirs} skills")
    if len(idx) / 4 > 2500:
        failures.append(f"index ~{len(idx)/4:.0f} tokens > 2500")
    for n, d in es:
        if len(d.rstrip("…").split()) > MAX_WORDS:
            failures.append(f"{n}: purpose > {MAX_WORDS} words")
        if not d:
            failures.append(f"{n}: empty purpose")
    if "Skill tool" not in idx:
        failures.append("index lacks load-on-demand instruction")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - ")
        return 1
    print(f"OK: all self-test cases passed. ({len(es)} skills, ~{len(idx)/4:.0f} tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else (print(build_index()) or 0))
