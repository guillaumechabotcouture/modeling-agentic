"""Owner requests: amendments put to the owner, and the answers counted.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import json
import findings_ledger as FL

from .common import _now


OWNER_REQUEST = "scope_signoff_request.json"   # scripts/scope_signoff.py REQUEST: `show` / `sign` read it


def _answer_counts(a: dict, mode: str) -> bool:
    """Whose answer releases an owner item: the owner's; the lead's only in a --signoff lead run (D2), and only a
    provisional one (the harness's, listed in the report: Codex r1 of #316)."""
    return a.get("by") == "owner" or (a.get("by") == "lead" and mode == "lead" and a.get("provisional") is True)


OWNER_REQUEST_PRODUCER = "agents/stage_machine.py FIX_LOOP (owner amendments)"


def request_owner_amendments(run_dir: str, round_n: int, ledger: dict, ids: list[str]) -> dict:
    """F6 run 3: the findings whose remedy is an owner amendment of the signed spec, written as a request in
    scripts/scope_signoff.py's format, so the owner reads it with `scope_signoff.py show` and answers with `sign`
    (accept: the run goes on without the amendment, declared; decline: the finding stays open, the owner's). Each item's
    fingerprint binds its claim and remedy."""
    items = []
    for fid in ids:
        f = ledger.get(fid)
        claim, fix = (f.claim, f.fix_requires) if f is not None else ("", None)
        fp = FL.answer_fingerprint(fid, claim, fix)       # the ledger binds the owner's answer to it
        conflict = getattr(f, "kind", None) == "evidence_conflict_declared"
        opts = {}
        if conflict:            # owner decision 2026-11-21: a refit is listed, with its estimated cost, never run
            from toolkit.analysis import conflicts as CF
            opts = {"options": CF.options(run_dir, getattr(f, "key", "")),
                    "conflict": CF.verdict(run_dir, getattr(f, "key", ""))}     # Codex r4 of #316: the structured verdict
        items.append({"finding": fid, "kind": getattr(f, "kind", None), "claim": claim, "fix_requires": fix, **opts,
                      "reason": ("a held-out calibration target's automatic decision test changes the plan or could "
                                 "not run (conflicts.policy declare_and_test)") if conflict else
                      "its required remedy is an owner amendment of the signed decision spec",
                      "declarations": [], "declared": False, "fingerprint": fp})
    doc = {"pending": True, "round": round_n, "requested_at": _now(), "items": items, "producer": OWNER_REQUEST_PRODUCER,
           "decision_rationale": "the fix loop does not change the owner-signed spec: amend and re-sign it, or answer "
                                 "accept (the run continues without the amendment) or decline"}
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, OWNER_REQUEST, json.dumps(doc, indent=1).encode(), atomic=True)
    return doc
