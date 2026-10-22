#!/usr/bin/env python3
"""Render the E1 context-diet modeler prompt.

    python3 experiments/E1_context_diet/render_variant.py [--report]

Writes modeler_contract.rendered.md next to this file: the trimmed behavioural
prompt with {{CONTRACT_PACKET}} and {{SKILL_INDEX}} filled in. `{run_dir}` is
left for scripts/replay.py to substitute. --report prints start-context
token estimates (chars/4) for control vs variant.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)

from agents.contract_packet import build_packet  # noqa: E402
from agents.skill_index import build_index  # noqa: E402

TEMPLATE = os.path.join(HERE, "modeler_contract.md")
OUT = os.path.join(HERE, "modeler_contract.rendered.md")


def render() -> str:
    text = open(TEMPLATE).read()
    assert "{{CONTRACT_PACKET}}" in text and "{{SKILL_INDEX}}" in text
    text = text.replace("{{CONTRACT_PACKET}}", build_packet("model").strip())
    text = text.replace("{{SKILL_INDEX}}", build_index().strip())
    open(OUT, "w").write(text)
    return text


def report(variant_text: str) -> None:
    from agents import build_agents
    tok = lambda n: n / 4 / 1000
    for label, q in (("malaria question", "Allocate a malaria budget across Nigeria's LGAs with Starsim"),
                     ("measles question", "Spatial measles model across Kenya's counties (LASER or Starsim)")):
        m = build_agents(question=q, run_dir="runs/x")["modeler"]
        sk = sum(os.path.getsize(os.path.join(REPO, ".claude", "skills", s, "SKILL.md"))
                 for s in (m.skills or []) if os.path.exists(os.path.join(REPO, ".claude", "skills", s, "SKILL.md")))
        print(f"control ({label}): prompt ~{tok(len(m.prompt)):.1f}K + {len(m.skills or [])} skills "
              f"~{tok(sk):.1f}K = ~{tok(len(m.prompt) + sk):.1f}K tokens (+ tool defs, CLAUDE.md)")
    print(f"variant: prompt+packet+index ~{tok(len(variant_text)):.1f}K tokens, 0 preloaded skills "
          f"(+ tool defs, CLAUDE.md)")


if __name__ == "__main__":
    t = render()
    print(f"wrote {OUT}")
    if "--report" in sys.argv:
        report(t)
