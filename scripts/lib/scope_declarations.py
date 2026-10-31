"""Phase 21 β: scope_declaration.yaml normalization (moved here in Phase 49 α).

    python3 scripts/lib/scope_declarations.py --self-test

Shared by scripts/validate_critique_yaml.py (budget, rule 3a, pre-mortem,
not_applicable) and scripts/declaration_tests.py (Phase 49 α: a declaration
of a HIGH finding cites a decision test). Moved verbatim; the names lost their
leading underscore and the validator imports them under the old names.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.rigor_artifacts import artifact_path  # noqa: E402
from lib.safe_io import safe_load_mapping  # noqa: E402

#
# Real runs used at least eight different top-level keys for their
# declarations (`declarations`, `scope_declarations`, `unresolved_blockers`,
# `unresolved_high_blockers`, `acknowledged_limitations`, ...), referenced
# blockers via `blocker_id`, lists of ids, or ids embedded in prose, and
# marked some entries `status: RESOLVED`. The Phase 19 β budget counted only
# `declarations:` entries with `id`/`claim`, so any other shape burned zero
# budget; the pre-mortem check accepted any substring anywhere in the file.
# One normalizer now feeds both.

BLOCKER_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,5}-\d{2,4}\b")
STRUCTURED_ID_FIELDS = ("blocker_id", "blocker_ids", "blockers", "covers",
                        "concern_id", "concern_ids", "ids", "acknowledges",
                        "blocker")
JUSTIFICATION_FIELDS = ("why_unresolved", "justification", "rationale",
                        "claim", "reason", "impact_on_conclusions",
                        "description")
# Gate kinds that carry a critic's finding under another name (Phase 39; moved
# from validate_critique_yaml.py in Phase 49 α): never declared away early, and
# outside the decision-test requirement (a critic blocker is the critic's).
CRITIC_DERIVED_PREFIXES = ("done_when_", "critique_", "claim_overclaimed", "sufficiency", "crossmodel")
NON_DECLARATION_KEYS = frozenset({"budget", "version", "run_dir",
                                  "generated_at", "declared_at"})


def load(run_dir: str) -> tuple[Optional[dict], list[dict], list[dict]]:
    """Return (doc, entries, findings) for scope_declaration.yaml.

    `entries` is every mapping found in any top-level list (except the
    `budget` block and metadata keys), plus any top-level mapping value
    that itself looks like a declaration (has an id field). Entries with
    `status: RESOLVED` are dropped — they declare nothing.
    """
    path = artifact_path("scope_declaration", run_dir)
    doc, findings = safe_load_mapping(path, "scope_declaration", stage="GATE",
                                      display_path="scope_declaration.yaml")
    if doc is None:
        return None, [], findings
    entries: list[dict] = []
    for key, val in doc.items():
        if key in NON_DECLARATION_KEYS:
            continue
        items = val if isinstance(val, list) else (
            [dict(val, id=val.get("id", key))]
            if isinstance(val, dict) else [])
        for e in items:
            if isinstance(e, dict):
                if str(e.get("status") or "").strip().upper() == "RESOLVED":
                    continue
                entries.append(e)
            elif isinstance(e, str) and e.strip():
                entries.append({"claim": e})
            else:
                entries.append({})
    return doc, entries, findings


def strings_in(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings_in(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings_in(v)]
    return []


def entry_declared_ids(entry: dict, structured_only: bool) -> set[str]:
    """Blocker ids an entry declares. Structured fields always count;
    with structured_only=False, ids mentioned anywhere in the entry's
    text count too (one entry listing 20 ids declares 20 blockers).
    The entry's own `id` (e.g. SD-001) is not a blocker reference unless
    it is the only identifier present."""
    ids: set[str] = set()
    for f in STRUCTURED_ID_FIELDS:
        v = entry.get(f)
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, (str, int)) and str(x).strip():
                ids.add(str(x).strip())
    if not structured_only:
        own = str(entry.get("id") or "")
        for txt in strings_in({k: v for k, v in entry.items() if k != "id"}):
            ids.update(m for m in BLOCKER_ID_RE.findall(txt) if m != own)
    return ids


def entry_has_justification(entry: dict) -> bool:
    return any(str(entry.get(f) or "").strip() for f in JUSTIFICATION_FIELDS)


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("  ok   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        doc, entries, f = load(d)
        ok(doc is None and entries == [], "SD1 no file: no entries")
        with open(os.path.join(d, "scope_declaration.yaml"), "w") as fh:
            fh.write("declarations:\n- id: SD-1\n  blocker_id: benchmark_drift\n  why_unresolved: x\n"
                     "- id: SD-2\n  status: RESOLVED\n  blocker_id: y\n"
                     "- just a string R-001\n"
                     "budget: {cap: 5}\n")
        doc, entries, f = load(d)
        ok(len(entries) == 2 and entries[1] == {"claim": "just a string R-001"},
           f"SD2 RESOLVED and budget dropped, strings kept: {entries}")
        ok(entry_declared_ids(entries[0], True) == {"benchmark_drift"}, "SD3 structured ids")
        ok(entry_declared_ids(entries[1], True) == set() and entry_declared_ids(entries[1], False) == {"R-001"},
           "SD4 prose ids count only when structured_only=False")
        ok(entry_has_justification(entries[0]) and not entry_has_justification({"id": "SD-9"}),
           "SD5 justification fields")
        with open(os.path.join(d, "scope_declaration.yaml"), "w") as fh:
            fh.write("declarations: [unclosed\n")
        doc, entries, f = load(d)
        ok(doc is None and bool(f) and f[0]["severity"] == "HIGH", f"SD6 unparseable file fails closed: {f}")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
