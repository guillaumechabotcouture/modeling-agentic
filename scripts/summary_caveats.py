#!/usr/bin/env python3
"""A22 (Phase 49 γ): declared limits lead the executive summary.

    python3 scripts/summary_caveats.py <run_dir>
    python3 scripts/summary_caveats.py --self-test

E7 attempt 8 ended DECLARE_SCOPE. Its three scope declarations covered the
run's main weaknesses: the allocation was unstable out of sample (SD-007), the
model did not reproduce the trial interaction between two interventions while
recommending against one of them (SD-002), and 19 transmission parameters were
weakly identified (SD-008). The writer reproduced them verbatim in §Limitations,
as instructed, and the executive summary named none of them. A decision-maker
who reads only the summary would not see them.

Rule. When the run has a draft (report.md or manuscript.md), every scope
declaration that covers a HIGH finding must be named in the report's
executive summary:
  - by its declaration id (`SD-007`), or by a `[CLAIM:id]` reference to a
    claim whose `source_artifact` is the declaration's decision test;
  - when the declaration cites a with/without decision test
    (`decision_test: <path>`, Phase 49 α), its measured effect on the
    recommendation must be in the summary too, as a `[CLAIM:id]` reference to
    a claim read from that test's result file (so the number is the test's).
A declaration covers a HIGH finding when one of its structured ids
(`blocker_id`, `blocker_ids`, ...; the validator's own normalizer) is the kind
or id of a HIGH gate finding at this gate, or the id of an open HIGH critic
blocker; a declaration of `<kind>` also covers `<kind>_persistent`. Findings
about the write-up itself (a PRESENTATION blocker, or target or stage WRITE,
and their `_persistent` form) do not count: they are not limits on the
recommendation, and the writer fixes them.

The executive summary is the first section of the report whose heading names a
summary ("Executive summary", "Summary", "Summary for decision-makers", or a
journal-style "Abstract"),
before the first numbered or Introduction section, up to the next heading of
the same or a higher level. Declaration ids are read from the rendered
report.md (the draft if there is none); `[CLAIM:id]` references from the
writer's draft (report.unrendered.md, else manuscript.md), since rendering
replaces them. No summary section is the same failure for every declaration.

Finding (fails closed; not scope-declarable, since declaring it would hide the
very limits it is about):
  declared_limit_not_in_summary   HIGH  stage WRITE, target WRITE

The decision-test result is read with Phase 49 α's
`scripts/declaration_tests.py` (`declaration_test(run_dir, entry)`) when that
module exists, else with a minimal read of `decision_test` -> `decision_changes`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Callable, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib.safe_io import safe_load_mapping  # noqa: E402
from lib.siblings import load_private  # noqa: E402

KIND = "declared_limit_not_in_summary"
HISTORY_FILE = "validator_history.jsonl"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SUMMARY_RE = re.compile(r"\b(?:summary|abstract)\b", re.I)
_BODY_START_RE = re.compile(r"^(?:\d+[.)]?\s|introduction\b|background\b)", re.I)
_CLAIM_RE = re.compile(r"\[CLAIM:([A-Za-z0-9_.\-]+)\]")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


# ---------------------------------------------------------------------------
# The executive summary
# ---------------------------------------------------------------------------

def summary_section(text: str) -> Optional[str]:
    """The executive summary's text (heading included), or None when the
    report has no summary section before its first numbered/Introduction
    section. Headings inside fenced code are ignored."""
    lines = text.splitlines()
    heads, fenced = [], False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else _HEADING_RE.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    for n, (i, level, title) in enumerate(heads):
        if _BODY_START_RE.match(title.strip("*_ ")):
            return None
        if _SUMMARY_RE.search(title):
            end = next((j for j, lv, _t in heads[n + 1:] if lv <= level), len(lines))
            return "\n".join(lines[i:end])
    return None


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def draft_texts(run_dir: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(reader-facing text, text with [CLAIM:id] tokens, which file was read).
    report.md is what readers get; its unrendered snapshot keeps the tokens."""
    report = _read(os.path.join(run_dir, "report.md"))
    draft = _read(os.path.join(run_dir, "manuscript.md"))
    if report is not None:
        tokens = _read(os.path.join(run_dir, "report.unrendered.md"))
        return report, tokens if tokens is not None else draft, "report.md"
    if draft is not None:
        return draft, draft, "manuscript.md"
    return None, None, None


def _mentions_id(text: str, ident: str) -> bool:
    return bool(ident) and re.search(rf"(?<![A-Za-z0-9_\-]){re.escape(ident)}(?![A-Za-z0-9_])", text) is not None


# ---------------------------------------------------------------------------
# HIGH findings at this gate
# ---------------------------------------------------------------------------

def _about_write_up(b: dict) -> bool:
    """A finding about the write-up itself (captions, numbering, wording), not a
    limit on the recommendation: a PRESENTATION blocker, or one for the writer."""
    return (str(b.get("category") or "").upper() == "PRESENTATION"
            or str(b.get("target_stage") or "").upper() == "WRITE"
            or str(b.get("stage") or "").upper() == "WRITE")


def _critic_high_ids(run_dir: str) -> tuple[set[str], set[str]]:
    """(ids of open HIGH critic blockers, ids of those about the write-up), from
    `blockers` and `carried_forward`. An unreadable critique is the schema
    check's finding."""
    import glob
    out: set[str] = set()
    write_up: set[str] = set()
    for path in sorted(glob.glob(os.path.join(run_dir, "critique_*.yaml"))):
        doc, _f = safe_load_mapping(path, "critique")
        for key in ("blockers", "carried_forward"):
            for b in (doc or {}).get(key) or []:
                if not isinstance(b, dict) or not b.get("id"):
                    continue
                if _about_write_up(b):
                    write_up.add(str(b["id"]))
                if str(b.get("severity") or "").upper() != "HIGH":
                    continue
                if b.get("resolved") is True or b.get("still_present") is False:
                    continue
                out.add(str(b["id"]))
    return out - write_up, write_up


def _history_current(run_dir: str) -> list[dict]:
    """The last gate's findings from validator_history.jsonl (CLI use only)."""
    last = None
    try:
        with open(os.path.join(run_dir, HISTORY_FILE), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("findings"), list):
                    last = rec
    except OSError:
        return []
    return [v for v in (last or {}).get("findings") or [] if isinstance(v, dict)]


def high_ids(run_dir: str, current: Iterable[dict]) -> set[str]:
    """Kinds and ids of HIGH findings that limit the recommendation: gate
    findings and open critic blockers, except those about the write-up
    (presentation category, target or stage WRITE, or their `_persistent` form)."""
    ids, write_up = _critic_high_ids(run_dir)
    for v in current:
        if not isinstance(v, dict) or str(v.get("severity") or "").upper() != "HIGH" or _about_write_up(v):
            continue
        kind = str(v.get("kind") or "")
        if kind.endswith("_persistent") and kind[:-len("_persistent")] in write_up:
            continue
        ids |= {str(x) for x in (v.get("kind"), v.get("id")) if x}
    return ids


def covers(declared: set[str], highs: set[str]) -> list[str]:
    """HIGH finding ids/kinds the declared ids cover (`x` covers `x_persistent`)."""
    return sorted(h for h in highs
                  if h in declared or (h.endswith("_persistent") and h[:-len("_persistent")] in declared))


# ---------------------------------------------------------------------------
# Decision tests (Phase 49 α) and the claims that report them
# ---------------------------------------------------------------------------

def _norm_rel(p) -> str:
    return os.path.normpath(str(p).strip()).lstrip("./") if isinstance(p, str) and p.strip() else ""


def decision_test(run_dir: str, entry: dict) -> tuple[Optional[str], Optional[dict], Optional[str]]:
    """(test path, result, problem). Path None when the entry cites no test."""
    rel = entry.get("decision_test")
    if rel is None:
        return None, None, None
    if not _norm_rel(rel):
        return str(rel), None, f"`decision_test` is not a file path ({rel!r})"
    try:
        mod = load_private("declaration_tests", "_ma_summary_caveats_")
    except (FileNotFoundError, ImportError, AttributeError):
        mod = None
    if mod is not None and hasattr(mod, "declaration_test"):
        res, err = mod.declaration_test(run_dir, entry)
        return str(rel), res if isinstance(res, dict) else None, err
    doc, f = safe_load_mapping(os.path.join(run_dir, _norm_rel(rel)), "decision_test")
    if doc is None:
        return str(rel), None, f"its result file `{rel}` cannot be read"
    if not isinstance(doc.get("decision_changes"), bool):
        return str(rel), doc, f"its result file `{rel}` has no `decision_changes: true|false`"
    return str(rel), doc, None


def _claims(run_dir: str) -> dict[str, dict]:
    """Claim id -> entry, from claims_spec.yaml, else the built ledger."""
    out: dict[str, dict] = {}
    for rel in ("models/claims_ledger.yaml", "models/claims_spec.yaml"):
        doc, _f = safe_load_mapping(os.path.join(run_dir, rel), "claims")
        for c in (doc or {}).get("claims") or []:
            if isinstance(c, dict) and c.get("id"):
                out[str(c["id"])] = c
    return out


def test_claims(tokens_text: str, claims: dict[str, dict], test_rel: str) -> list[str]:
    """[CLAIM:id] references in the text whose claim is read from the test file."""
    want = _norm_rel(test_rel)
    return [cid for cid in dict.fromkeys(_CLAIM_RE.findall(tokens_text or ""))
            if want and _norm_rel((claims.get(cid) or {}).get("source_artifact")) == want]


# ---------------------------------------------------------------------------
# Gate entry point
# ---------------------------------------------------------------------------

def _finding(claim: str, key: str, **extra) -> dict:
    return {"kind": KIND, "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
            "scope_declarable": False, "key": key, "claim": claim, **extra}


def gate_findings(run_dir: str, current: Optional[list] = None,
                  entries: Optional[list] = None,
                  declared_ids: Optional[Callable[[dict], set]] = None) -> list[dict]:
    """Findings for declarations covering a HIGH finding that the executive
    summary does not name. The validator passes this gate's findings so far
    (`current`), the normalized declaration entries and its id extractor;
    the command line reads the last recorded gate and loads them itself."""
    shown, tokens, which = draft_texts(run_dir)
    if shown is None:
        return []          # no draft yet: nothing to check
    if entries is None or declared_ids is None:
        v = load_private("validate_critique_yaml", "_ma_summary_caveats_")
        entries = v._load_scope_declarations(run_dir)[1] if entries is None else entries
        declared_ids = declared_ids or (lambda e: v._entry_declared_ids(e, structured_only=True))
    if current is None:
        current = _history_current(run_dir)
    highs = high_ids(run_dir, current)
    todo = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        hit = covers({str(x) for x in declared_ids(e)}, highs)
        if hit:
            todo.append((e, hit))
    if not todo:
        return []
    summary = summary_section(shown)
    tok_summary = summary_section(tokens) if tokens else None
    claims = _claims(run_dir) if tok_summary else {}
    out = []
    for e, hit in todo:
        did = str(e.get("id") or "").strip()
        label = did or f"the declaration of {', '.join(hit[:3])}"
        test_rel, res, problem = decision_test(run_dir, e)
        bound = test_claims(tok_summary or "", claims, test_rel) if test_rel else []
        if summary is None:
            out.append(_finding(
                f"{label} declares HIGH finding(s) {', '.join(hit[:5])}, and {which} has no executive "
                "summary (a section headed 'Executive summary', 'Summary' or 'Abstract' before the first numbered "
                "section), so a reader of the summary cannot see it. Open the report with an executive "
                "summary that ends with a 'What this does not establish' block naming each declaration.",
                did or hit[0], declaration=did or None, covers=hit))
            continue
        named = (did and _mentions_id(summary, did)) or bool(bound)
        if named and (test_rel is None or bound):
            continue
        need = []
        if not named:
            need.append(f"name it ({did})" if did else "give the declaration an `id` and name it")
        if test_rel is not None and not bound:
            verdict = res.get("decision_changes") if isinstance(res, dict) else None
            v_txt = ("" if not isinstance(verdict, bool) else
                     f" (the test says the decision {'changes' if verdict else 'does not change'})")
            need.append(f"state the measured effect on the recommendation from its decision test "
                        f"`{test_rel}`{v_txt} as a [CLAIM:id] reference to a claim whose source_artifact "
                        f"is that file" + (f"; note: {problem}" if problem else ""))
        out.append(_finding(
            f"{label} declares HIGH finding(s) {', '.join(hit[:5])}, but the executive summary of "
            f"{which} does not " + " and ".join(need) + ". The declarations stay verbatim in "
            "§Limitations; the summary needs a short 'What this does not establish' block so a reader "
            "of the summary alone sees what the recommendation does not rest on.",
            did or hit[0], declaration=did or None, covers=hit, decision_test=test_rel))
    return out


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    import shutil
    import tempfile
    import yaml

    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def ids_of(e):
        out = set()
        for k in ("blocker_id", "blocker_ids"):
            v = e.get(k)
            for x in (v if isinstance(v, list) else [v]):
                if x:
                    out.add(str(x))
        return out

    def w(d, rel, obj):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                yaml.safe_dump(obj, f)

    HIGH = [{"kind": "allocation_unstable", "severity": "HIGH"},
            {"kind": "weakly_identified_parameters_persistent", "severity": "HIGH"},
            {"kind": "benchmark_drift", "severity": "MEDIUM"}]
    ENTRIES = [{"id": "SD-007", "blocker_id": "allocation_unstable", "claim": "c"},
               {"id": "SD-008", "blocker_ids": ["weakly_identified_parameters"], "claim": "c"},
               {"id": "SD-002", "blocker_id": "benchmark_drift", "claim": "c"}]

    def run(d, cur=HIGH, entries=ENTRIES):
        return gate_findings(d, current=cur, entries=entries, declared_ids=ids_of)

    head = "# Title\n\nStatus line.\n\n## Executive summary\n\n"
    tail = "\n\n### Abbreviations\n\nx\n\n## 1. Introduction\n\nSD-007 SD-008 are here only.\n"
    d = tempfile.mkdtemp(prefix="sc_")
    try:
        ok(run(d) == [], "SC1 no draft: no findings")
        w(d, "manuscript.md", head + "Fund nets everywhere." + tail)
        f = run(d)
        ok(sorted(x["declaration"] for x in f) == ["SD-007", "SD-008"],
           f"SC2 two declarations cover HIGHs (SD-002 covers a MEDIUM) and the summary names neither: {f}")
        ok(all(x["severity"] == "HIGH" and x["scope_declarable"] is False and x["stage"] == "WRITE"
               for x in f), "SC3 HIGH, WRITE, not declarable")
        ok(any("weakly_identified_parameters_persistent" in x["covers"] for x in f),
           "SC4 a declaration of <kind> covers <kind>_persistent")
        w(d, "manuscript.md", head + "**What this does not establish.** Unstable out of sample (SD-007); "
          "weak identification (SD-008)." + tail)
        ok(run(d) == [], f"SC5 both named in the summary: no findings ({run(d)})")
        w(d, "manuscript.md", head + "Plain summary; see SD-0070." + tail)
        ok(len(run(d)) == 2, "SC6 an id inside a longer token does not name it")
        w(d, "manuscript.md", "# Title\n\n## 1. Introduction\n\nSD-007 SD-008\n\n## Summary table\n\nx\n")
        f = run(d)
        ok(len(f) == 2 and all("no executive summary" in x["claim"] for x in f),
           f"SC7 no summary before the first numbered section fails closed: {f}")
        # report.md is what readers get; ids are read there.
        w(d, "manuscript.md", head + "SD-007 SD-008" + tail)
        w(d, "report.md", head + "Rendered, names nothing." + tail)
        ok(len(run(d)) == 2, "SC8 the rendered report.md is checked when it exists")
        os.remove(os.path.join(d, "report.md"))
        # A critic's open HIGH blocker counts; a resolved one does not.
        w(d, "critique_redteam.yaml", {"reviewer": "critique-redteam", "round": 3, "blockers": [
            {"id": "R-064", "severity": "HIGH", "resolved": False},
            {"id": "R-001", "severity": "HIGH", "resolved": True}]})
        ents = ENTRIES + [{"id": "SD-4", "blocker_id": "R-064", "claim": "c"},
                          {"id": "SD-5", "blocker_id": "R-001", "claim": "c"}]
        f = run(d, entries=ents)
        ok([x["declaration"] for x in f] == ["SD-4"], f"SC9 open critic HIGH covered, resolved not: {f}")
        w(d, "critique_presentation.yaml", {"reviewer": "critique-presentation", "round": 3, "blockers": [
            {"id": "P-001", "severity": "HIGH", "category": "PRESENTATION", "target_stage": "WRITE",
             "resolved": False}]})
        ents_p = ENTRIES + [{"id": "SD-6", "blocker_ids": ["P-001", "P-001_persistent"], "claim": "c"}]
        f = run(d, cur=HIGH + [{"kind": "P-001_persistent", "severity": "HIGH", "stage": "GATE"}], entries=ents_p)
        ok("SD-6" not in [x["declaration"] for x in f],
           f"SC9b a declared write-up finding (and its persistence escalation) is not a limit: {f}")
        f = run(d, cur=[{"kind": "same_doc_inconsistency", "severity": "HIGH", "stage": "WRITE"}],
                entries=[{"id": "SD-7", "blocker_id": "same_doc_inconsistency", "claim": "c"}])
        ok(f == [], f"SC9c a gate finding of stage WRITE is not a limit: {f}")
        os.remove(os.path.join(d, "critique_presentation.yaml"))
        # Decision test: the id alone is not enough; a claim from the test is.
        w(d, "models/sufficiency/sd007.yaml", {"finding": "allocation_unstable", "varied": "v",
                                               "criterion": "c", "decision_changes": True})
        ents = [dict(ENTRIES[0], decision_test="models/sufficiency/sd007.yaml"), ENTRIES[1]]
        f = run(d, entries=ents)
        ok(len(f) == 1 and f[0]["declaration"] == "SD-007" and "measured effect" in f[0]["claim"]
           and "decision changes" in f[0]["claim"],
           f"SC10 a tested declaration named only by id needs its measured effect: {f}")
        w(d, "models/claims_spec.yaml", {"claims": [
            {"id": "sd007_share", "source_artifact": "models/sufficiency/sd007.yaml",
             "source_field": "share_units_changed"},
            {"id": "other", "source_artifact": "models/other.yaml"}]})
        w(d, "manuscript.md", head + "SD-008. Without stability, [CLAIM:other] of units change." + tail)
        ok([x["declaration"] for x in run(d, entries=ents)] == ["SD-007"],
           "SC11 a claim from another file does not state the test's effect")
        w(d, "manuscript.md", head + "SD-008. Packages change in [CLAIM:sd007_share] of units." + tail)
        ok(run(d, entries=ents) == [], f"SC12 a claim from the test names it and states it: {run(d, entries=ents)}")
        # Claims are read from the unrendered draft when report.md exists.
        w(d, "report.unrendered.md", head + "SD-008. Packages change in [CLAIM:sd007_share] of units." + tail)
        w(d, "report.md", head + "SD-008. Packages change in 12% of units." + tail)
        ok(run(d, entries=ents) == [], "SC13 the rendered report keeps the test claim via its unrendered draft")
        ents2 = [dict(ENTRIES[0], decision_test="models/sufficiency/missing.yaml")]
        f = run(d, entries=ents2)
        ok(len(f) == 1 and "measured effect" in f[0]["claim"] and "missing.yaml" in f[0]["claim"],
           f"SC14 a missing test result still needs a claim from it, and is named: {f}")
        ents3 = [dict(ENTRIES[0], decision_test=["x"])]
        ok(len(run(d, entries=ents3)) == 1, "SC15 a non-path decision_test fails closed")
        # Headings in fenced code are not sections.
        txt = "# T\n\n```\n## 1. Intro\n```\n\n## Executive Summary\n\nSD-007 SD-008\n\n## 1. Introduction\n"
        ok(summary_section(txt) is not None and "SD-007" in summary_section(txt),
           "SC16 fenced headings are ignored")
        ok("SD-1" in (summary_section("# T\n\n## Abstract\n\nSD-1\n\n## 1. Introduction\n") or ""),
           "SC16b a journal-style Abstract is the summary")
        # No declarations covering HIGHs: silent.
        ok(run(d, cur=[], entries=ENTRIES) == [], "SC17 nothing HIGH covered: no findings")
        # CLI fallback reads the last recorded gate.
        with open(os.path.join(d, HISTORY_FILE), "w") as fh:
            fh.write(json.dumps({"round": 2, "findings": HIGH}) + "\n")
        ok(_history_current(d) == HIGH, "SC18 history fallback reads the last gate's findings")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if fails:
        for m in fails:
            print("FAIL:", m)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    from lib import gate_cli
    return gate_cli.run(argv, __doc__, gate_findings, _self_test, fails=gate_cli.any_high)


if __name__ == "__main__":
    sys.exit(main())
