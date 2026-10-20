#!/usr/bin/env python3
"""Phase 21 γ — Markdown helpers shared by writer_qa.py.

`writer_qa.py` used to exec the entire 7,000-line
validate_critique_yaml.py just to borrow these three helpers. They are
pure functions over text, so they live here; the validator's copies
are verbatim (see validate_critique_yaml.py `_extract_last_numeric`,
`_parse_md_tables`, `_strip_limitations_and_scope`) and can import
from here instead.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

_NUMERIC_TOKEN_RE = re.compile(
    r"(?P<sign>[-+]?)\$?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>[kKmMbBtT]?)\b"
)
# Time-horizon suffix in cells like "/3yr", "/5 years", "/yr", "per year".
_HORIZON_RE = re.compile(
    r"/\s*(\d+)\s*(?:yr|year)|/\s*(?:yr|year)\b|per\s+year",
    re.IGNORECASE,
)

def _extract_last_numeric(cell: str) -> Optional[float]:
    """Extract the LAST numeric token from a cell string. Modelers
    typically write the original then a parenthesized normalization
    (e.g. `84,000/5yr (~50,400/3yr)`); the LAST token is the normalized
    figure that should be used for cross-column comparison.

    Phase 5 fixes:
      - Strip horizon suffixes (`/3yr`, `/5 years`, `/yr`, `per year`)
        BEFORE extracting numerics. Otherwise the regex picks up the
        horizon's digit (e.g., "5" from "/5yr") as the last numeric.
      - Strip parenthetical citations (`(Bhatt 2015)`, `(Author Year)`)
        BEFORE extracting numerics. Citation years would otherwise be
        picked up as the last numeric, replacing the actual quantity.
        Normalization parentheticals like `(~50,400/3yr)` start with
        non-letter characters (digit, ~, $) so they are NOT stripped.
    """
    stripped = _HORIZON_RE.sub("", cell)
    # Strip `(Author Year)` style citations — paren content beginning
    # with an alphabetic character. Keeps `(50,400/3yr)`, `($107M)`,
    # `(~12%)` — anything starting with a digit/punctuation.
    stripped = re.sub(r"\(\s*[A-Za-z][^)]*\)", "", stripped)
    matches = list(_NUMERIC_TOKEN_RE.finditer(stripped))
    if not matches:
        return None
    m = matches[-1]
    raw = m.group("num").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    suffix = (m.group("suffix") or "").lower()
    multiplier = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get(suffix, 1.0)
    return value * multiplier


def _parse_md_tables(text: str) -> list[dict]:
    """Linear scan for Markdown tables. Returns a list of
    {headers: [...], rows: [{label: str, cells: [...]}, ...]} dicts.
    Linear scan, no backtracking."""
    tables = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not (line.startswith("|") and line.endswith("|") and "|" in line[1:-1]):
            i += 1
            continue
        # Possible table header. Need a separator line next.
        if i + 1 >= len(lines):
            i += 1
            continue
        sep = lines[i + 1].strip()
        if not re.fullmatch(r"\|[\s:|-]+\|", sep):
            i += 1
            continue
        # Parse header.
        headers = [c.strip() for c in line.strip("|").split("|")]
        rows = []
        j = i + 2
        while j < len(lines):
            row_line = lines[j].rstrip()
            if not (row_line.startswith("|") and row_line.endswith("|")):
                break
            cells = [c.strip() for c in row_line.strip("|").split("|")]
            if len(cells) != len(headers):
                break
            rows.append({"label": cells[0], "cells": cells[1:]})
            j += 1
        if rows:
            tables.append({"headers": headers, "rows": rows})
        i = j
    return tables


_LIMITATIONS_HEADER_RE = re.compile(
    r"^#{1,4}\s+\d*\.?\s*"
    r"(?:Limitations?|Scope\s+Declaration|Caveats?|"
    r"Unresolved\s+(?:HIGH|Blockers?)|Known\s+Issues?)"
    r"[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_limitations_and_scope(text: str) -> str:
    """Remove §Limitations / §Scope Declaration / §Caveats sections
    so DALY mentions in those sections don't satisfy the
    daly_analysis_missing check. Returns the text with each such
    section sliced from header to next same-level (or higher)
    section header. Linear scan."""
    out_parts = []
    last_end = 0
    headers = list(_LIMITATIONS_HEADER_RE.finditer(text))
    for m in headers:
        # Determine the level (count of '#' at the header start).
        hdr_text = m.group(0)
        level = len(hdr_text) - len(hdr_text.lstrip("#"))
        # Find the next header at this level or shallower (more
        # important).
        next_header_re = re.compile(
            rf"^#{{1,{level}}}\s+",
            re.MULTILINE,
        )
        section_end = len(text)
        for next_m in next_header_re.finditer(text, m.end()):
            section_end = next_m.start()
            break
        out_parts.append(text[last_end:m.start()])
        last_end = section_end
    out_parts.append(text[last_end:])
    return "".join(out_parts)


def _self_test() -> int:
    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    ok(_extract_last_numeric("84,000/5yr (~50,400/3yr)") == 50_400,
       "T1: last numeric after horizon strip")
    ok(_extract_last_numeric("12.5M (Bhatt 2015)") == 12.5e6,
       "T2: citation year stripped")
    t = _parse_md_tables("| a | b |\n|---|---|\n| x | 1 |\n")
    ok(len(t) == 1 and t[0]["rows"][0]["cells"] == ["1"], f"T3: table parse {t}")
    s = _strip_limitations_and_scope("# R\nkeep\n## Limitations\ndrop\n## Next\nkeep2\n")
    ok("drop" not in s and "keep2" in s, f"T4: limitations stripped: {s!r}")
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
