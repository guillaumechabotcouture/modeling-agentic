#!/usr/bin/env python3
"""A23 (Phase 49 δ): the writer answers the presentation critique before the run ends.

    python3 scripts/presentation_pass.py <run_dir>     # pending items, review state, gate finding
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

Review record (Codex, Phase 49 δ). When a critique-presentation spawn returns,
the harness hook writes `presentation_review.json` (protected): the sha256 of
the draft it read (manuscript.md, else report.md; rendering does not change
it), and whether the spawn rewrote a parseable critique_presentation.yaml.

Callers:
  - the harness (`agents/run_finish.presentation_fix_pass`): when the sessions
    end with open presentation HIGHs for the writer, or with no successful
    review of the delivered draft, it runs one pass from the write-up reserve:
    the writer fixes the items (if any), the claims are re-rendered, and
    critique-presentation reviews the draft;
  - the gate (`gate_findings`): a draft with no successful review of that
    very draft is a HIGH accept blocker, `presentation_review_missing`,
    `_failed` (the critic crashed or did not rewrite its critique) or
    `_stale` (the draft changed after the review). ACCEPT is impossible
    while it is open, whatever is declared; it stays declarable, so a run
    that ends with it is DECLARE_SCOPE listing it, not RUN_FAILED.

Open item: a HIGH blocker in critique_presentation.yaml (`blockers`, or a
`carried_forward` entry still present) that is not resolved and whose
target_stage is WRITE (figures and analysis go to their own agents).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib.safe_io import safe_load_mapping  # noqa: E402

CRITIQUE = "critique_presentation.yaml"
DRAFT = "manuscript.md"
REVIEW = "presentation_review.json"     # written by the harness (agents/hooks.py) only
KINDS = {"missing": "presentation_review_missing", "failed": "presentation_review_failed",
         "stale": "presentation_review_stale"}


def _sha(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def draft_file(run_dir: str) -> Optional[str]:
    """The writer's draft the presentation critic reviews: manuscript.md, else
    report.md (a run whose draft was rendered and removed). Rendering does not
    change manuscript.md, so a review stays current across re-renders of the
    same draft."""
    for rel in (DRAFT, "report.md"):
        if os.path.exists(os.path.join(run_dir, rel)):
            return rel
    return None


def critique_ok(run_dir: str) -> tuple[bool, str]:
    """(usable, reason): critique_presentation.yaml parses, with an int `round`
    and a `blockers` list."""
    doc, findings = safe_load_mapping(os.path.join(run_dir, CRITIQUE), "critique_presentation")
    if doc is None:
        return False, "critique_presentation.yaml unreadable" if findings else "no critique_presentation.yaml"
    if not isinstance(doc.get("round"), int) or not isinstance(doc.get("blockers"), list):
        return False, "critique_presentation.yaml has no int `round` or no `blockers` list"
    return True, ""


REVIEWED_FILES = ("manuscript.md", "report.md")   # what the critic is told to read
SNAP_DIR = "presentation_review"                  # harness-written, protected


def snapshot(run_dir: str) -> dict:
    """Codex (Phase 49 δ, round 2): the harness copies the files the critic reads
    into presentation_review/<key>/ (read-only) before the review starts, and
    the critic is pointed at the copy. The hashes are taken here, at snapshot
    time, so a writer edit or a re-render during the review cannot be credited
    to it. Returns {"snapshot", "files": {rel: sha|None}, "critique_sha_before"}."""
    files = {rel: _sha(os.path.join(run_dir, rel)) for rel in REVIEWED_FILES}
    key = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
    rel_dir = f"{SNAP_DIR}/{key}"
    dest = os.path.join(run_dir, rel_dir)
    os.makedirs(dest, exist_ok=True)
    copied = []
    for rel, sha in files.items():
        if sha is None:
            continue
        out = os.path.join(dest, rel)
        if not os.path.exists(out):
            with open(os.path.join(run_dir, rel), "rb") as src, open(out, "wb") as dst:
                dst.write(src.read())
            os.chmod(out, 0o444)
        copied.append(f"{rel_dir}/{rel}")
    return {"snapshot": rel_dir, "files": files, "copied": copied,
            "critique_sha_before": _sha(os.path.join(run_dir, CRITIQUE))}


def snapshot_note(run_dir_rel: str, snap: dict) -> str:
    """What the hook appends to the critic's prompt."""
    names = [rel for rel, sha in snap["files"].items() if sha]
    return ("\n\n[harness] Review ONLY this snapshot of the draft, taken when this review started: "
            + ", ".join(f"{run_dir_rel}/{snap['snapshot']}/{n}" for n in names)
            + ". Do not read the live manuscript.md or report.md; write critique_presentation.yaml and "
            "critique_presentation.md in the run directory as usual.")


def record_review(run_dir: str, before: dict) -> dict:
    """When a full critique-presentation review returns: which snapshot it read
    (the hashes captured at snapshot time) and whether it produced a usable
    critique (parseable, and rewritten by this spawn)."""
    after = _sha(os.path.join(run_dir, CRITIQUE))
    ok, reason = critique_ok(run_dir)
    if ok and after == before.get("critique_sha_before"):
        ok, reason = False, "critique_presentation.yaml was not rewritten by this review"
    rec = {"snapshot": before.get("snapshot"), "files": before.get("files") or {},
           "critique_sha": after, "ok": ok, "reason": reason,
           "at": datetime.now(timezone.utc).isoformat()}
    tmp = os.path.join(run_dir, REVIEW + ".tmp")
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=2)
    os.replace(tmp, os.path.join(run_dir, REVIEW))
    return rec


def review_state(run_dir: str) -> tuple[str, str]:
    """("no_draft" | "ok" | "missing" | "failed" | "stale", detail). Ok only when
    the recorded snapshot hashes of BOTH manuscript.md and report.md equal the
    delivered files (a missing file must be missing in both)."""
    if draft_file(run_dir) is None:
        return "no_draft", ""
    try:
        with open(os.path.join(run_dir, REVIEW)) as f:
            rec = json.load(f)
        if not isinstance(rec, dict) or not isinstance(rec.get("files"), dict):
            raise ValueError("not a review record")
    except FileNotFoundError:
        return "missing", "no presentation review has been recorded for this run's draft"
    except (OSError, ValueError) as e:
        return "failed", f"{REVIEW} is unreadable ({e})"
    if not rec.get("ok"):
        return "failed", f"the last presentation review did not produce a usable critique ({rec.get('reason')})"
    for rel in REVIEWED_FILES:
        if rec["files"].get(rel) != _sha(os.path.join(run_dir, rel)):
            return "stale", (f"{rel} changed after the snapshot the last presentation review read "
                             f"({rec.get('snapshot')}, {rec.get('at')})")
    return "ok", ""


def pending(run_dir: str) -> dict:
    """{"round", "items": [{id, claim, fix_requires}], "review": state, "reason"}.
    `items`: open HIGH items for the writer (HIGH, unresolved, still present,
    target WRITE). The harness pass runs when there are items or the review
    does not cover the current draft."""
    state, detail = review_state(run_dir)
    out = {"round": None, "items": [], "review": state, "review_detail": detail, "reason": None}
    if state == "no_draft":
        out["reason"] = "no draft"
        return out
    doc, _f = safe_load_mapping(os.path.join(run_dir, CRITIQUE), "critique_presentation")
    doc = doc or {}
    out["round"] = doc.get("round") if isinstance(doc.get("round"), int) else None
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
    if not items and state == "ok":
        out["reason"] = "no open HIGH presentation item for the writer, and the review covers the draft"
    return out


def gate_findings(run_dir: str) -> list[dict]:
    """A draft without a successful presentation review of that very draft
    cannot be accepted: HIGH accept blocker (ACCEPT is impossible at any
    round, whatever is declared). It stays declarable, so a run that ends with
    it is DECLARE_SCOPE listing it, never ACCEPT."""
    state, detail = review_state(run_dir)
    if state in ("ok", "no_draft"):
        return []
    return [{"kind": KINDS[state], "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
             "accept_blocker": True, "scope_declarable": True, "key": "critique_presentation",
             "claim": (f"No successful presentation review covers the delivered draft: {detail}. ACCEPT needs "
                       "critique-presentation (`This is critique round N.`) on the current draft, after the "
                       "writer's last change; the harness records which draft each review read. If the run "
                       "ends first, the harness runs one fix pass and the re-review from the write-up reserve.")}]


def _self_test() -> int:
    import shutil
    import tempfile

    import yaml

    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def kinds(d):
        return [f["kind"] for f in gate_findings(d)]

    d = tempfile.mkdtemp(prefix="pp_")
    try:
        ok(pending(d)["reason"] == "no draft" and gate_findings(d) == [], "PP1 no draft: nothing pending, no finding")
        open(os.path.join(d, DRAFT), "w").write("# Draft\n")
        p = pending(d)
        ok(p["review"] == "missing" and p["items"] == [] and p["reason"] is None,
           f"PP2 draft, never reviewed: the pass runs (critic only): {p}")
        ok(kinds(d) == ["presentation_review_missing"] and gate_findings(d)[0]["accept_blocker"] is True
           and gate_findings(d)[0]["severity"] == "HIGH",
           "PP3 (Codex P1-2) a draft no review covers blocks ACCEPT: HIGH accept blocker")
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
        before = snapshot(d)
        yaml.safe_dump(crit, open(os.path.join(d, CRITIQUE), "w"))
        rec = record_review(d, before)
        p = pending(d)
        snap_copy = os.path.join(d, before["snapshot"], DRAFT)
        ok(rec["ok"] and p["review"] == "ok" and kinds(d) == [] and os.path.exists(snap_copy)
           and not os.access(snap_copy, os.W_OK),
           f"PP4 a successful review of the snapshot is recorded and accepted; the copy is read-only: {rec} {p}")
        ok([i["id"] for i in p["items"]] == ["P-001", "P-002", "P-010"] and p["round"] == 8,
           f"PP5 open writer HIGHs only (not MEDIUM, MODEL-targeted, resolved, or no longer present): {p}")
        # Codex P1-2: the writer revises after the review and the critic never re-runs.
        open(os.path.join(d, DRAFT), "w").write("# Draft, revised\n")
        ok(kinds(d) == ["presentation_review_stale"] and pending(d)["review"] == "stale",
           f"PP6 (Codex P1-2) the draft changed after the review: stale, blocks ACCEPT: {kinds(d)}")
        # Codex round 2: the writer edits the draft WHILE the critic reviews: the record
        # carries the hashes taken at snapshot time, so the review covers the old text.
        before = snapshot(d)
        open(os.path.join(d, DRAFT), "w").write("# Draft, edited during the review\n")
        yaml.safe_dump(dict(crit, verdict="REVISE"), open(os.path.join(d, CRITIQUE), "w"))
        record_review(d, before)
        ok(kinds(d) == ["presentation_review_stale"],
           f"PP6b (Codex round 2) a writer edit during the review leaves the review stale: {kinds(d)}")
        # Codex P1-2: the critic crashes (its YAML is not rewritten).
        rec = record_review(d, snapshot(d))
        ok(not rec["ok"] and kinds(d) == ["presentation_review_failed"],
           f"PP7 (Codex P1-2) a review that did not rewrite the critique failed, blocks ACCEPT: {rec}")
        before = snapshot(d)
        open(os.path.join(d, CRITIQUE), "w").write("blockers: [unclosed\n")
        rec = record_review(d, before)
        ok(not rec["ok"] and kinds(d) == ["presentation_review_failed"],
           f"PP8 an unparseable critique is a failed review: {rec}")
        crit["blockers"] = [dict(b, resolved=True) for b in crit["blockers"]]
        crit["carried_forward"] = []
        before = snapshot(d)
        yaml.safe_dump(crit, open(os.path.join(d, CRITIQUE), "w"))
        record_review(d, before)
        ok(pending(d)["items"] == [] and pending(d)["reason"] and kinds(d) == [],
           "PP9 the re-review closes them: nothing pending, no finding")
        # Codex round 2: report.md is rendered (or re-rendered by the harness fallback)
        # after the review: the delivered report is not the one reviewed.
        open(os.path.join(d, "report.md"), "w").write("# Rendered after the review\n")
        ok(kinds(d) == ["presentation_review_stale"],
           "PP10 (Codex round 2) report.md changed after the review (e.g. the fallback render): stale")
        before = snapshot(d)
        yaml.safe_dump(dict(crit, round=9), open(os.path.join(d, CRITIQUE), "w"))
        record_review(d, before)
        ok(kinds(d) == [] and sorted(json.load(open(os.path.join(d, REVIEW)))["files"]) == list(sorted(REVIEWED_FILES)),
           "PP10b a review of both files, unchanged since, is current; both hashes recorded")
        open(os.path.join(d, REVIEW), "w").write("{not json")
        ok(kinds(d) == ["presentation_review_failed"], "PP11 an unreadable review record fails closed")
        f = gate_findings(d)[0]
        ok(f["scope_declarable"] is True and f["stage"] == "WRITE",
           "PP12 declarable, so a run ending with it is DECLARE_SCOPE (never ACCEPT), not RUN_FAILED")
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
          + f"; review {p['review']}")
    for i in p["items"]:
        print(f"  {i['id']}: {i['claim'][:200]}")
    for x in gate_findings(a.run_dir):
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
