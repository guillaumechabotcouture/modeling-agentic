#!/usr/bin/env python3
"""A23 (Phase 49 δ): the writer answers the presentation critique before the run ends.

    python3 scripts/presentation_pass.py <run_dir>     # pending items + gate finding
    python3 scripts/presentation_pass.py --self-test

Since Phase 39 α the presentation critic reviews the first draft, and the
first draft waits for the gate, so in practice it is written in the final
round. E7 attempt 8: the first writer spawn was in round 8 (2026-10-30
13:52 UTC); critique-presentation ran at 14:37 and raised three HIGHs for the
writer (P-001 units stripped from rendered numbers, P-002 figure numbering,
P-003 uncaptioned tables). The lead did route them to a writer pass at 14:49,
but nothing required that, and the critic never re-checked the result: its
YAML still listed all three open at the final gate (P-001 closed only by its
done_when test; P-002 and P-003 counted as outside the brief).

This module says what is still open, for two callers:
  - the harness (`agents/run_finish.presentation_fix_pass`): when the sessions
    end with open presentation HIGHs for the writer, it runs one fix pass
    paid from the write-up reserve: the writer fixes them, the claims are
    re-rendered, and critique-presentation re-reviews the draft;
  - the gate (`gate_findings`): a draft changed after the presentation
    critique, with that critique's writer HIGHs still open, has not been
    re-checked: MEDIUM `presentation_fix_unchecked` (WRITE, scope-declarable;
    the re-review clears it).

Open item: a HIGH blocker in critique_presentation.yaml (`blockers`, or a
`carried_forward` entry still present) that is not resolved and whose
target_stage is WRITE (figures and analysis go to their own agents).
"""
from __future__ import annotations

import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib.safe_io import safe_load_mapping  # noqa: E402

CRITIQUE = "critique_presentation.yaml"
DRAFT = "manuscript.md"
KIND = "presentation_fix_unchecked"


def _mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def pending(run_dir: str) -> dict:
    """{"round", "items": [{id, claim, fix_requires}], "reason", "draft_after_critique"}.
    `items` is empty with a `reason` when there is nothing for the writer."""
    out = {"round": None, "items": [], "reason": None, "draft_after_critique": False}
    draft = _mtime(os.path.join(run_dir, DRAFT))
    if draft is None:
        out["reason"] = "no draft"
        return out
    path = os.path.join(run_dir, CRITIQUE)
    doc, findings = safe_load_mapping(path, "critique_presentation")
    if doc is None:
        out["reason"] = ("critique_presentation.yaml unreadable" if findings
                         else "no presentation critique")
        return out
    out["round"] = doc.get("round") if isinstance(doc.get("round"), int) else None
    crit = _mtime(path)
    out["draft_after_critique"] = bool(crit is not None and draft > crit)
    seen, items = set(), []
    for key in ("blockers", "carried_forward"):
        for b in doc.get(key) or []:
            if not isinstance(b, dict) or not b.get("id") or str(b["id"]) in seen:
                continue
            seen.add(str(b["id"]))
            if str(b.get("severity") or "").upper() != "HIGH":
                continue
            if b.get("resolved") is True or b.get("still_present") is False:
                continue
            if str(b.get("target_stage") or "WRITE").upper() != "WRITE":
                continue
            items.append({"id": str(b["id"]), "claim": str(b.get("claim") or "").strip(),
                          "fix_requires": str(b.get("fix_requires") or "").strip()})
    out["items"] = items
    if not items:
        out["reason"] = "no open HIGH presentation item for the writer"
    return out


def gate_findings(run_dir: str) -> list[dict]:
    p = pending(run_dir)
    if not p["items"] or not p["draft_after_critique"]:
        return []
    ids = ", ".join(i["id"] for i in p["items"])
    return [{"kind": KIND, "severity": "MEDIUM", "stage": "WRITE", "target_stage": "WRITE",
             "scope_declarable": True, "key": "critique_presentation",
             "claim": (f"The draft changed after the presentation critique (round {p['round']}), and its HIGH "
                       f"item(s) for the writer ({ids}) are still open in {CRITIQUE}: nobody has checked the "
                       "fixes. Spawn critique-presentation (`This is critique round N.`) on the current draft. "
                       "If the run ends first, the harness runs one writer fix pass and the re-review from "
                       "the write-up reserve.")}]


def _self_test() -> int:
    import shutil
    import tempfile
    import time

    import yaml

    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    d = tempfile.mkdtemp(prefix="pp_")
    try:
        ok(pending(d)["reason"] == "no draft" and gate_findings(d) == [], "PP1 no draft: nothing pending")
        open(os.path.join(d, DRAFT), "w").write("# Draft\n")
        ok(pending(d)["reason"] == "no presentation critique", "PP2 draft, no critique: nothing pending")
        crit = {"reviewer": "critique-presentation", "round": 8, "blockers": [
            {"id": "P-001", "severity": "HIGH", "target_stage": "WRITE", "claim": "units", "fix_requires": "f",
             "resolved": False},
            {"id": "P-002", "severity": "HIGH", "target_stage": "WRITE", "claim": "numbering", "resolved": False},
            {"id": "P-004", "severity": "MEDIUM", "target_stage": "WRITE", "claim": "m", "resolved": False},
            {"id": "P-005", "severity": "HIGH", "target_stage": "MODEL", "claim": "figure", "resolved": False},
            {"id": "P-007", "severity": "HIGH", "target_stage": "WRITE", "claim": "x", "resolved": True}],
            "carried_forward": [{"id": "P-009", "severity": "HIGH", "target_stage": "WRITE", "claim": "old",
                                 "still_present": False},
                                {"id": "P-010", "severity": "HIGH", "claim": "old, still there",
                                 "still_present": True}]}
        yaml.safe_dump(crit, open(os.path.join(d, CRITIQUE), "w"))
        p = pending(d)
        ok([i["id"] for i in p["items"]] == ["P-001", "P-002", "P-010"] and p["round"] == 8,
           f"PP3 open writer HIGHs only (not MEDIUM, MODEL-targeted, resolved, or no longer present): {p}")
        ok(gate_findings(d) == [], "PP4 critique newer than the draft: no gate finding")
        t = time.time() + 5
        os.utime(os.path.join(d, DRAFT), (t, t))
        f = gate_findings(d)
        ok(len(f) == 1 and f[0]["kind"] == KIND and f[0]["severity"] == "MEDIUM" and f[0]["scope_declarable"]
           and "P-001, P-002, P-010" in f[0]["claim"],
           f"PP5 draft changed after the critique with writer HIGHs open: MEDIUM unchecked: {f}")
        t2 = t + 5
        crit["blockers"] = [dict(b, resolved=True) for b in crit["blockers"]]
        crit["carried_forward"] = []
        yaml.safe_dump(crit, open(os.path.join(d, CRITIQUE), "w"))
        os.utime(os.path.join(d, CRITIQUE), (t2, t2))
        ok(pending(d)["items"] == [] and gate_findings(d) == [], "PP6 the re-review closes them: nothing pending")
        open(os.path.join(d, CRITIQUE), "w").write("blockers: [unclosed\n")
        ok(pending(d)["items"] == [] and "unreadable" in pending(d)["reason"],
           "PP7 an unreadable critique is the schema check's finding, not a fix pass")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if fails:
        for m in fails:
            print("FAIL:", m)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    p = pending(a.run_dir)
    print(f"round {p['round']}: {len(p['items'])} open writer HIGH(s)"
          + (f" ({p['reason']})" if p["reason"] else "")
          + ("; draft changed after the critique" if p["draft_after_critique"] else ""))
    for i in p["items"]:
        print(f"  {i['id']}: {i['claim'][:200]}")
    for x in gate_findings(a.run_dir):
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
