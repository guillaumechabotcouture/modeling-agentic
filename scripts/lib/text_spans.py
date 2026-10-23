#!/usr/bin/env python3
"""Phase 21 γ — shared text / document helpers for the rigor scripts.

Previously duplicated verbatim across render_claims.py and
coherence_audit.py (code-span detection) and with divergent behavior
across effort_floors.py and benchmark_match.py (dot-path resolution —
only one copy supported `[*]`). One copy now, so a fix lands everywhere.

Public API
----------
code_spans(text, inline=True)   -> list[(start, end)]
in_spans(spans, pos)            -> bool
resolve_dotpath(doc, path)      -> Any   (supports `a.b`, `a.0.b`, `a[*].b`)
"""

from __future__ import annotations

import re
import sys
from typing import Any

_FENCE_RE = re.compile(r"^(?:```|~~~)", re.MULTILINE)
_INLINE_RE = re.compile(r"`[^`\n]+`")


def code_spans(text: str, inline: bool = True) -> list[tuple[int, int]]:
    """Return spans of fenced code blocks (``` / ~~~) and, when
    `inline` is True, inline backtick code in `text`.

    `inline=False` is for numeric audits: a number formatted as inline
    code in prose (`` `$999M` ``) is still a claim a reader sees, so only
    fenced blocks (genuine code/examples) are exempt. `[CLAIM:id]`
    syntax documentation uses `inline=True`.

    An unpaired trailing fence protects nothing (an unterminated fence
    must not silently exempt the rest of the document).
    """
    spans: list[tuple[int, int]] = []
    starts = [m.start() for m in _FENCE_RE.finditer(text)]
    for i in range(0, len(starts) - 1, 2):
        end_idx = text.find("\n", starts[i + 1])
        if end_idx == -1:
            end_idx = len(text)
        spans.append((starts[i], end_idx + 1))
    if inline:
        for m in _INLINE_RE.finditer(text):
            if not any(s <= m.start() < e for s, e in spans):
                spans.append((m.start(), m.end()))
    return spans


def in_spans(spans: list[tuple[int, int]], pos: int) -> bool:
    return any(s <= pos < e for s, e in spans)


def resolve_dotpath(doc: Any, path: str) -> Any:
    """Resolve a dotted path through nested dicts/lists.

    - `a.b`      mapping lookups
    - `a.0.b`    integer segments index into lists
    - `a[*].b`   iterate the list at `a`, resolve `b` on each element and
                 flatten (lists are extended, scalars appended, missing
                 skipped). `a[*]` alone returns the list itself.

    Returns None when any segment is absent or has the wrong type.
    """
    if not path:
        return doc
    parts = path.split(".")
    cur: Any = doc
    for idx, part in enumerate(parts):
        if cur is None:
            return None
        if part.endswith("[*]"):
            head = part[:-3]
            if head:
                if not isinstance(cur, dict) or head not in cur:
                    return None
                cur = cur[head]
            if not isinstance(cur, list):
                return None
            remaining = ".".join(parts[idx + 1:])
            if not remaining:
                return cur
            collected: list[Any] = []
            for elem in cur:
                sub = resolve_dotpath(elem, remaining)
                if isinstance(sub, list):
                    collected.extend(sub)
                elif sub is not None:
                    collected.append(sub)
            return collected
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and re.fullmatch(r"-?\d+", part):
            i = int(part)
            if -len(cur) <= i < len(cur):
                cur = cur[i]
            else:
                return None
        else:
            return None
    return cur


def _self_test() -> int:
    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    t = "a `x` b\n```\ncode $5M\n```\nafter `$9M`"
    s_all = code_spans(t)
    s_fence = code_spans(t, inline=False)
    ok(in_spans(s_all, t.index("x")), "T1: inline span protected")
    ok(in_spans(s_fence, t.index("code")), "T2: fenced span protected")
    ok(not in_spans(s_fence, t.index("$9M")),
       "T3: inline=False leaves inline code unprotected")
    ok(not in_spans(code_spans("x\n```\nunterminated $5M"), 5),
       "T4: unpaired fence protects nothing")

    doc = {"a": {"b": 3}, "l": [{"p": [1, 2]}, {"p": [3]}], "m": [10, 20]}
    ok(resolve_dotpath(doc, "a.b") == 3, "T5: plain dotpath")
    ok(resolve_dotpath(doc, "l[*].p") == [1, 2, 3], "T6: [*] flatten")
    ok(resolve_dotpath(doc, "m.1") == 20, "T7: list index")
    ok(resolve_dotpath(doc, "a.zz") is None, "T8: absent → None")
    ok(resolve_dotpath(doc, "m.9") is None, "T9: out-of-range index → None")
    ok(resolve_dotpath(doc, "l[*]") == doc["l"], "T10: bare [*] returns list")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
