#!/usr/bin/env python3
"""Phase 33 α: the decision brief: what the answer must deliver, agreed before
compute and frozen for the run.

E7 attempt 6 had a plan and machine-checked success criteria, but nothing
settled what the answer had to deliver (whose decision, the objective, the
resolution that may be published, which accounting is primary, what is out of
scope), the critics did not judge against it, and the modeler edited its own
benchmark bands twice mid-run. The critics raised 6-10 new findings every
round and the run never converged.

The planner writes `decision_brief.yaml` with plan.md:

    question: "<verbatim research question>"
    decision: "<the decision this analysis informs>"
    decision_maker: "<who makes it (a role)>"
    answers:            # what the report must answer, and at what resolution
      - {id: A1, statement: "...", resolution: "<unit results are published at>"}
    objective: "<what is maximised or minimised, and what else is valued>"
    constraints:        - {id: C1, text: "..."}
    accounting:         - {id: K1, text: "<how benefits and costs are counted>"}
    out_of_scope:       - {id: OS1, text: "...", reason: "..."}
    comparators:        # what impact is measured against; exactly one primary
      - {id: CF1, role: primary, text: "<what happens without the plan>"}
      - {id: CF2, role: sensitivity, text: "..."}
    acceptance: [HB-001, MB-002, ...]   # ids in success_criteria.yaml
    open_questions:     - {id: Q1, question: "...", proposed: "<proposed answer>"}

Before any other work the brief is signed: by the owner by default (the run
waits; `decision_brief.py sign <run_dir> --answers answers.yaml`), or by the
lead in unattended test runs (`--signoff lead`). Signing records the hashes of
the brief and success_criteria.yaml; models/benchmark_targets.yaml is locked
the first time the gate sees it. Any later change is a HIGH, non-declarable
`acceptance_criteria_changed`. Framing changes the run discovers go in
`brief_amendments.yaml` (lead-written, shown in the report); acceptance
criteria cannot be amended in-run.

Phase 38: the comparator is part of the brief. E7 attempt 7's round-1 headline
was measured against "every net withdrawn"; the critics' largest finding was
that comparator, and fixing it rebuilt the simulation library (279
core-hours, about 4 hours). Each comparator says what happens in each unit
without the plan: other funders' and routine activity, and how coverage
evolves over the window. One is primary; the others are sensitivities.

Critics tie each HIGH / MEDIUM finding to the brief item it threatens
(`brief_item:`), or GAP when the brief does not settle something the decision
depends on, or NONE. Once the brief is signed, a NONE finding does not block
or escalate; it is a note for the next run. A missing or unknown tie fails
closed: the finding still blocks and the gate asks for the critic to fix it.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

import yaml

BRIEF, SIGNOFF, LOCK, AMENDMENTS = ("decision_brief.yaml", "decision_brief.signoff.json",
                                    "acceptance_lock.json", "brief_amendments.yaml")
SIGNED_DIR = "decision_brief.signed"   # copies of the frozen files as signed (Gemini review)
CRITERIA, BENCHMARKS = "success_criteria.yaml", "models/benchmark_targets.yaml"
FROZEN_AT_SIGNOFF = (BRIEF, CRITERIA)
SCOPED_CRITICS = ("critique-domain", "critique-presentation", "critique-redteam", "critique-methods")
GAP, NONE = "GAP", "NONE"
_LISTS = {"answers": "A", "constraints": "C", "accounting": "K", "out_of_scope": "OS", "comparators": "CF",
          "open_questions": "Q"}
COMPARATOR_ROLES = ("primary", "sensitivity")
_TEXT = ("question", "decision", "decision_maker", "objective")
FRAMING_PREFIXES = tuple(_LISTS.values())

CRITIC_NOTE = """

## Tie each finding to the decision brief (Phase 33)

`decision_brief.yaml` (signed before compute) says what this analysis must
deliver: the decision, the answers and the resolution they are published at
(A*), constraints (C*), accounting conventions (K*), what is out of scope
(OS*), what impact is measured against (CF*: one primary comparator, the
others sensitivities), and the acceptance criteria (ids from success_criteria.yaml). Judge
the work against it. Give every HIGH or MEDIUM blocker a `brief_item:` field:
the id it threatens, or `GAP` when the brief does not settle something the
decision depends on (say in the claim what the brief should settle), or
`NONE` when it threatens no brief item: a NONE finding does not block; it is
kept as a note for the next run. Do not re-open what the brief settles, or
what it puts out of scope.
"""


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _load(path: str):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _criteria_ids(run_dir: str) -> set[str]:
    try:
        doc = _load(os.path.join(run_dir, CRITERIA)) or {}
    except (OSError, yaml.YAMLError):
        return set()
    return {str(c.get("id")) for sec in ("hard_blockers", "minimum_bar", "targets")
            for c in (doc.get(sec) or []) if isinstance(c, dict) and c.get("id")}


def load_brief(run_dir: str):
    """(doc, errors)."""
    path = os.path.join(run_dir, BRIEF)
    if not os.path.exists(path):
        return None, [f"{BRIEF} does not exist"]
    try:
        doc = _load(path)
    except (OSError, yaml.YAMLError) as e:
        return None, [f"{BRIEF} does not parse: {str(e)[:120]}"]
    if not isinstance(doc, dict):
        return None, [f"{BRIEF} must be a mapping"]
    return doc, validate(doc, run_dir)


def validate(doc: dict, run_dir: str) -> list[str]:
    errors = [f"{k}: missing or empty" for k in _TEXT if not str(doc.get(k) or "").strip()]
    seen: set[str] = set()
    for key, prefix in _LISTS.items():
        items = doc.get(key)
        if items is None:
            items = []
        if not isinstance(items, list):
            errors.append(f"{key}: must be a list")
            continue
        if key in ("answers",) and not items:
            errors.append("answers: at least one answer the report must give")
        if key == "comparators":
            prim = [it for it in items if isinstance(it, dict) and it.get("role") == "primary"]
            if len(prim) != 1:
                errors.append("comparators: exactly one primary comparator (what happens without the plan: other "
                              "funders' and routine activity, and how coverage evolves over the window); "
                              f"found {len(prim)}")
        for it in items:
            iid = str((it or {}).get("id") or "") if isinstance(it, dict) else ""
            if not re.fullmatch(rf"{prefix}\d+", iid):
                errors.append(f"{key}: id {iid or '?'} should look like {prefix}1")
                continue
            if iid in seen:
                errors.append(f"{key}: duplicate id {iid}")
            seen.add(iid)
            text_key = {"answers": "statement", "open_questions": "question"}.get(key, "text")
            if not str(it.get(text_key) or "").strip():
                errors.append(f"{iid}: {text_key} is empty")
            if key == "answers" and not str(it.get("resolution") or "").strip():
                errors.append(f"{iid}: resolution is empty (the unit results are published at)")
            if key == "out_of_scope" and not str(it.get("reason") or "").strip():
                errors.append(f"{iid}: reason is empty")
            if key == "comparators" and it.get("role") not in COMPARATOR_ROLES:
                errors.append(f"{iid}: role must be one of {', '.join(COMPARATOR_ROLES)}")
            if key == "open_questions" and not str(it.get("proposed") or "").strip():
                errors.append(f"{iid}: proposed answer is empty")
    acc = doc.get("acceptance") or []
    if not isinstance(acc, list) or not acc:
        errors.append("acceptance: list the success_criteria.yaml ids this run is judged by")
    else:
        known = _criteria_ids(run_dir)
        unknown = [str(a) for a in acc if str(a) not in known]
        if unknown:
            errors.append(f"acceptance: not in {CRITERIA}: {', '.join(unknown)}")
    return errors


def item_ids(run_dir: str) -> set[str]:
    """Ids a critic finding may be tied to (brief items, acceptance ids, GAP)."""
    doc, _ = load_brief(run_dir)
    if not doc:
        return set()
    ids = {str(it.get("id")) for key in _LISTS for it in (doc.get(key) or []) if isinstance(it, dict)}
    ids |= {str(a) for a in doc.get("acceptance") or []} | _criteria_ids(run_dir)
    try:
        bt = _load(os.path.join(run_dir, BENCHMARKS)) or {}
        items = bt.get("benchmarks") if isinstance(bt, dict) else bt
        ids |= {str(b.get("id")) for b in (items or []) if isinstance(b, dict) and b.get("id")}
    except (OSError, yaml.YAMLError, AttributeError):
        pass
    for am in _amendments(run_dir)[0]:
        if am.get("item"):
            ids.add(str(am["item"]))
    return ids | {GAP}


def _signoff(run_dir: str) -> dict | None:
    try:
        with open(os.path.join(run_dir, SIGNOFF), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def status(run_dir: str) -> str:
    """absent | invalid | awaiting | changed | signed."""
    doc, errors = load_brief(run_dir)
    so = _signoff(run_dir)
    if doc is None and errors and "does not exist" in errors[0] and so is None:
        return "absent"   # (a brief deleted after sign-off is "changed", Gemini review)
    if so is not None:
        changed = [f for f in FROZEN_AT_SIGNOFF if _sha(os.path.join(run_dir, f)) != so.get("sha256", {}).get(f)]
        return "changed" if changed else "signed"
    return "invalid" if errors else "awaiting"


def is_signed(run_dir: str) -> bool:
    return _signoff(run_dir) is not None


def sign(run_dir: str, by: str, answers: dict | None = None, note: str = "",
         accept_proposed: bool = False) -> dict:
    """Write the sign-off. Raises ValueError when the brief is not ready."""
    doc, errors = load_brief(run_dir)
    if errors:
        raise ValueError("brief not ready: " + "; ".join(errors))
    if is_signed(run_dir):
        raise ValueError("already signed; framing changes go in " + AMENDMENTS)
    answers = {str(k): str(v) for k, v in (answers or {}).items()}
    qs = [q for q in doc.get("open_questions") or [] if isinstance(q, dict)]
    missing = [q["id"] for q in qs if q["id"] not in answers and not accept_proposed]
    if missing:
        raise ValueError("no answer for " + ", ".join(missing) + " (give one, or --accept-proposed)")
    record = {
        "signed_by": by, "signed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "note": note,
        "answers": {q["id"]: answers.get(q["id"], q.get("proposed")) for q in qs},
        "answer_source": {q["id"]: "given" if q["id"] in answers else "proposed accepted" for q in qs},
        "sha256": {f: _sha(os.path.join(run_dir, f)) for f in FROZEN_AT_SIGNOFF},
        "producer": "scripts/decision_brief.py",
    }
    # Gemini review: keep the signed versions, so a changed file can be restored.
    os.makedirs(os.path.join(run_dir, SIGNED_DIR), exist_ok=True)
    for f in FROZEN_AT_SIGNOFF:
        src = os.path.join(run_dir, f)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(run_dir, SIGNED_DIR, f))
    with open(os.path.join(run_dir, SIGNOFF), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1)
    return record


def _amendments_ok_ids(ids) -> bool:
    return all(re.fullmatch(r"(?:OS|CF|[ACKQ])\d+", str(i)) for i in ids)


def restore(run_dir: str) -> list[str]:
    """Copy the signed versions back over frozen files that changed."""
    so = _signoff(run_dir)
    if so is None:
        return []
    done = []
    for f in FROZEN_AT_SIGNOFF:
        saved = os.path.join(run_dir, SIGNED_DIR, f)
        if os.path.exists(saved) and _sha(os.path.join(run_dir, f)) != (so.get("sha256") or {}).get(f):
            shutil.copyfile(saved, os.path.join(run_dir, f))
            done.append(f)
    return done


def signoff_text(run_dir: str) -> str | None:
    """What the lead is told after sign-off."""
    so = _signoff(run_dir)
    if not so:
        return None
    lines = [f"The decision brief was signed by {so.get('signed_by')} at {so.get('signed_at')}."]
    if so.get("note"):
        lines.append(f"Note: {so['note']}")
    for qid, ans in (so.get("answers") or {}).items():
        lines.append(f"- {qid}: {ans} ({(so.get('answer_source') or {}).get(qid, '')})")
    lines.append(f"{BRIEF} and {CRITERIA} are now frozen; {BENCHMARKS} is locked when the gate first sees it. "
                 f"Record framing changes you discover in {AMENDMENTS} with the reason; acceptance criteria "
                 "cannot be amended in this run. Tell every specialist to work to the brief, and the planner "
                 "to carry the owner's answers into plan.md.")
    return "\n".join(lines)


def render_md(run_dir: str) -> str:
    doc, errors = load_brief(run_dir)
    if doc is None:
        return f"# Decision brief\n\n{'; '.join(errors)}\n"
    out = ["# Decision brief (for sign-off)", "", f"**Question:** {doc.get('question')}", "",
           f"**Decision:** {doc.get('decision')}  ", f"**Decided by:** {doc.get('decision_maker')}  ",
           f"**Objective:** {doc.get('objective')}", ""]
    titles = {"answers": "What the report must answer", "constraints": "Constraints",
              "accounting": "Accounting conventions", "out_of_scope": "Out of scope",
              "comparators": "Compared with (what happens without the plan)",
              "open_questions": "Open questions for the owner"}
    for key, title in titles.items():
        items = [it for it in doc.get(key) or [] if isinstance(it, dict)]
        if not items:
            continue
        out.append(f"## {title}")
        for it in items:
            if key == "answers":
                out.append(f"- **{it['id']}** {it.get('statement')} *(resolution: {it.get('resolution')})*")
            elif key == "out_of_scope":
                out.append(f"- **{it['id']}** {it.get('text')}: {it.get('reason')}")
            elif key == "open_questions":
                out.append(f"- **{it['id']}** {it.get('question')}  \n  Proposed: {it.get('proposed')}")
            elif key == "comparators":
                out.append(f"- **{it['id']}** ({it.get('role')}) {it.get('text')}")
            else:
                out.append(f"- **{it['id']}** {it.get('text')}")
        out.append("")
    out += ["## Acceptance criteria", ", ".join(str(a) for a in doc.get("acceptance") or []), ""]
    if errors:
        out += ["## Not ready to sign", *[f"- {e}" for e in errors], ""]
    return "\n".join(out)


def _amendments(run_dir: str) -> tuple[list[dict], list[str]]:
    path = os.path.join(run_dir, AMENDMENTS)
    if not os.path.exists(path):
        return [], []
    try:
        doc = _load(path) or {}
    except (OSError, yaml.YAMLError) as e:
        return [], [f"{AMENDMENTS} does not parse: {str(e)[:100]}"]
    items = doc.get("amendments") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        return [], [f"{AMENDMENTS}: expected a list under `amendments:`"]
    errors, ok = [], []
    for am in items:
        if not isinstance(am, dict):
            errors.append(f"{AMENDMENTS}: an entry is not a mapping")
            continue
        item, aid = str(am.get("item") or ""), am.get("id") or "?"
        if not _amendments_ok_ids([item]):
            errors.append(f"{aid}: only framing items (A, C, K, OS, CF, Q ids) can be amended in-run, not {item or '?'}")
        elif len(str(am.get("reason") or "").strip()) < 20 or not str(am.get("change") or "").strip():
            errors.append(f"{aid}: needs `change` and a `reason`")
        else:
            ok.append(am)
    return ok, errors


def gate_findings(run_dir: str) -> list[dict]:
    """Findings for the STAGE 7 gate (empty when the run has no brief)."""
    if not os.path.exists(os.path.join(run_dir, BRIEF)) and not is_signed(run_dir):
        return []
    out = []

    def high(kind, claim):
        out.append({"kind": kind, "severity": "HIGH", "stage": "PLAN", "scope_declarable": False,
                    "claim": claim})
    so = _signoff(run_dir)
    if so is None and os.environ.get("MA_SIGNOFF_MODE") == "none":
        pass   # the run was started with --signoff none: no sign-off was asked for
    elif so is None:
        high("decision_brief_not_signed", f"{BRIEF} exists but was never signed; work ran without an "
             "agreed target.")
    else:
        for f in FROZEN_AT_SIGNOFF:
            if _sha(os.path.join(run_dir, f)) != (so.get("sha256") or {}).get(f):
                high("acceptance_criteria_changed",
                     f"{f} changed after sign-off ({so.get('signed_at')}). It is frozen: restore the signed "
                     f"version; record framing changes in {AMENDMENTS}.")
    lock_path = os.path.join(run_dir, LOCK)
    try:
        lock = json.load(open(lock_path, encoding="utf-8"))
    except (OSError, ValueError):
        lock = {}
    bt = _sha(os.path.join(run_dir, BENCHMARKS))
    if bt and BENCHMARKS not in lock:
        lock[BENCHMARKS] = {"sha256": bt, "locked_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump(lock | {"producer": "scripts/decision_brief.py"}, f, indent=1)
    elif BENCHMARKS in lock and bt != lock[BENCHMARKS].get("sha256"):
        high("acceptance_criteria_changed",
             f"{BENCHMARKS} changed after it was locked ({lock[BENCHMARKS].get('locked_at')}). Benchmark bands "
             "are acceptance criteria: restore them. A benchmark you believe is wrong stays a finding; "
             "declare it, do not move the band.")
    for e in _amendments(run_dir)[1]:
        out.append({"kind": "brief_amendment_invalid", "severity": "MEDIUM", "stage": "PLAN", "claim": e})
    return out


def _scoped(critiques: dict):
    for reviewer, doc in (critiques or {}).items():
        if reviewer in SCOPED_CRITICS:
            for b in (doc or {}).get("blockers") or []:
                if isinstance(b, dict) and b.get("severity") in ("HIGH", "MEDIUM") and b.get("resolved") is not True:
                    yield reviewer, b


def scope_filter(critiques: dict, run_dir: str) -> set[str]:
    """Ids of critic HIGH / MEDIUM findings the critic tied to NONE (only once
    the brief is signed, and only for critics told to tie them)."""
    if not is_signed(run_dir):
        return set()
    return {str(b.get("id")) for _, b in _scoped(critiques)
            if str(b.get("brief_item") or "").strip().upper() == NONE}


def tie_findings(critiques: dict, run_dir: str) -> list[dict]:
    """Fail closed: a missing or unknown brief_item keeps the finding blocking
    and asks for the critic to fix the tie."""
    if not is_signed(run_dir):
        return []
    ids = item_ids(run_dir) | {NONE}
    bad: dict[str, list] = {}
    for reviewer, b in _scoped(critiques):
        tie = str(b.get("brief_item") or "").strip()
        if tie.upper() not in {i.upper() for i in ids}:
            bad.setdefault(reviewer, []).append(f"{b.get('id')} ({tie or 'missing'})")
    return [{"kind": "critique_brief_item_invalid", "severity": "MEDIUM", "stage": "CRITIQUE",
             "claim": (f"{r}: findings not tied to a decision-brief item: {', '.join(v)}. They still block. "
                       "Re-spawn the critic to give each a brief_item (an A/C/K/OS/CF/Q or acceptance id, GAP, "
                       "or NONE).")} for r, v in bad.items()]


# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)
    brief = {
        "question": "How should the programme allocate its budget across districts?",
        "decision": "Which package each district receives in the next grant",
        "decision_maker": "National programme manager",
        "answers": [{"id": "A1", "statement": "Package per district", "resolution": "district"}],
        "objective": "Maximise DALYs averted within the envelope",
        "constraints": [{"id": "C1", "text": "Total cost at most the envelope"}],
        "accounting": [{"id": "K1", "text": "Benefits counted within the grant window"}],
        "out_of_scope": [{"id": "OS1", "text": "Other diseases", "reason": "single-disease grant"}],
        "comparators": [{"id": "CF1", "role": "primary", "text": "Current coverage kept by routine channels"},
                        {"id": "CF2", "role": "sensitivity", "text": "No replacement of current coverage"}],
        "acceptance": ["HB-001"],
        "open_questions": [{"id": "Q1", "question": "Is continuity a constraint?", "proposed": "No"}],
    }
    with tempfile.TemporaryDirectory() as d:
        w = lambda rel, obj: (os.makedirs(os.path.dirname(os.path.join(d, rel)) or d, exist_ok=True),  # noqa: E731
                              yaml.safe_dump(obj, open(os.path.join(d, rel), "w")))
        ok(status(d) == "absent" and gate_findings(d) == [], "DB1: no brief, nothing required")
        w(CRITERIA, {"hard_blockers": [{"id": "HB-001"}], "minimum_bar": [{"id": "MB-001"}]})
        w(BRIEF, {**brief, "acceptance": ["HB-009"], "answers": [{"id": "A1", "statement": "x"}]})
        errs = load_brief(d)[1]
        ok(status(d) == "invalid" and any("HB-009" in e for e in errs) and any("resolution" in e for e in errs),
           f"DB2: invalid brief: {errs}")
        w(BRIEF, {k: v for k, v in brief.items() if k != "comparators"})
        errs = load_brief(d)[1]
        ok(status(d) == "invalid" and any("exactly one primary comparator" in e for e in errs),
           f"DB2b (Phase 38): a brief without a comparator cannot be signed: {errs}")
        w(BRIEF, {**brief, "comparators": [{"id": "CF1", "role": "primary", "text": "a"},
                                           {"id": "CF2", "role": "primary", "text": "b"},
                                           {"id": "CF3", "role": "main", "text": "c"}, {"id": "CF4", "role": "sensitivity"}]})
        errs = load_brief(d)[1]
        ok(any("found 2" in e for e in errs) and any("CF3: role" in e for e in errs) and any("CF4: text" in e for e in errs),
           f"DB2c (Phase 38): one primary, known roles, each comparator described: {errs}")
        w(BRIEF, brief)
        ok(_amendments_ok_ids(["CF2"]) and "Compared with" in render_md(d) and "CF1" in item_ids(d),
           "DB2d (Phase 38): comparators are amendable, rendered and tie targets")
        ok(status(d) == "awaiting", "DB3: valid, not signed")
        ok(any(f["kind"] == "decision_brief_not_signed" for f in gate_findings(d)), "DB4: unsigned brief at the gate")
        try:
            sign(d, "owner")
            fails.append("DB5: sign without answering Q1 should fail")
        except ValueError as e:
            ok("Q1" in str(e), "DB5: open question must be answered")
        so = sign(d, "owner", {"Q1": "Yes: keep currently covered districts"}, note="ok")
        ok(status(d) == "signed" and so["answers"]["Q1"].startswith("Yes"), "DB6: signed with the owner's answer")
        ok("Q1: Yes" in signoff_text(d), "DB7: lead is told the answers")
        ok(gate_findings(d) == [], f"DB8: signed, unchanged: {gate_findings(d)}")
        w(BENCHMARKS, {"benchmarks": [{"id": "B1", "band": [1, 2]}]})
        ok(gate_findings(d) == [] and os.path.exists(os.path.join(d, LOCK)), "DB9: benchmarks locked on first sight")
        w(BENCHMARKS, {"benchmarks": [{"id": "B1", "band": [0, 5]}]})
        ok(any(f["kind"] == "acceptance_criteria_changed" and BENCHMARKS in f["claim"] for f in gate_findings(d)),
           "DB10: widened benchmark band is caught")
        w(BENCHMARKS, {"benchmarks": [{"id": "B1", "band": [1, 2]}]})
        ok(gate_findings(d) == [], "DB11: restoring the band clears it")
        w(CRITERIA, {"hard_blockers": [{"id": "HB-001", "threshold": 2}], "minimum_bar": [{"id": "MB-001"}]})
        ok(status(d) == "changed" and any(CRITERIA in f["claim"] for f in gate_findings(d)),
           "DB12: criteria edited after sign-off")
        w(CRITERIA, {"hard_blockers": [{"id": "HB-001"}], "minimum_bar": [{"id": "MB-001"}]})
        w(AMENDMENTS, {"amendments": [
            {"id": "AM-1", "item": "A1", "change": "state level", "reason": "calibration supports states only"},
            {"id": "AM-2", "item": "HB-001", "change": "relax", "reason": "the target was not achievable"}]})
        f = gate_findings(d)
        ok(len(f) == 1 and f[0]["kind"] == "brief_amendment_invalid" and "AM-2" in f[0]["claim"],
           f"DB13: acceptance criteria cannot be amended in-run: {f}")
        crit = {"critique-redteam": {"blockers": [
                    {"id": "R-001", "severity": "HIGH", "brief_item": "A1"},
                    {"id": "R-002", "severity": "MEDIUM", "brief_item": "GAP"},
                    {"id": "R-003", "severity": "HIGH"},
                    {"id": "R-004", "severity": "MEDIUM", "brief_item": "Z9"},
                    {"id": "R-005", "severity": "HIGH", "brief_item": "B1"},
                    {"id": "R-006", "severity": "LOW"}]},
                "critique-crossmodel": {"blockers": [{"id": "X-001", "severity": "HIGH"}]}}
        crit["critique-redteam"]["blockers"].append({"id": "R-007", "severity": "HIGH", "brief_item": "none"})
        ok(scope_filter(crit, d) == {"R-007"},
           f"DB14: only findings the critic tied to NONE leave the blocking set: {scope_filter(crit, d)}")
        tf = tie_findings(crit, d)
        ok(len(tf) == 1 and "R-003 (missing)" in tf[0]["claim"] and "R-004 (Z9)" in tf[0]["claim"]
           and "X-001" not in tf[0]["claim"], f"DB14b: missing or unknown ties fail closed: {tf}")
        ok("brief_item" in CRITIC_NOTE and "GAP" in CRITIC_NOTE, "DB15: critic note")
        ok("Open questions for the owner" in render_md(d) and "resolution: district" in render_md(d),
           "DB16: sign-off view")
    with tempfile.TemporaryDirectory() as d:
        ok(scope_filter({"critique-redteam": {"blockers": [{"id": "R-1", "severity": "HIGH"}]}}, d) == set(),
           "DB17: before sign-off nothing is filtered")
        # DB18-DB19 (Codex review): --signoff none asks for no signature; in
        # owner mode no agent can sign, whatever the argument order.
        import subprocess
        yaml.safe_dump({"question": "q", "decision": "d", "decision_maker": "m", "objective": "o",
                        "answers": [{"id": "A1", "statement": "s", "resolution": "district"}]},
                       open(os.path.join(d, BRIEF), "w"))
        old_mode = os.environ.get("MA_SIGNOFF_MODE")
        try:
            os.environ["MA_SIGNOFF_MODE"] = "none"
            none_kinds = [f["kind"] for f in gate_findings(d)]
            os.environ["MA_SIGNOFF_MODE"] = "owner"
            owner_kinds = [f["kind"] for f in gate_findings(d)]
        finally:
            os.environ.pop("MA_SIGNOFF_MODE", None) if old_mode is None else os.environ.update(MA_SIGNOFF_MODE=old_mode)
        yaml.safe_dump({"hard_blockers": [{"id": "HB-001"}]}, open(os.path.join(d, CRITERIA), "w"))
        yaml.safe_dump({"question": "q", "decision": "d", "decision_maker": "m", "objective": "o",
                        "answers": [{"id": "A1", "statement": "s", "resolution": "district"}],
                        "comparators": [{"id": "CF1", "role": "primary", "text": "status quo"}],
                        "acceptance": ["HB-001"]}, open(os.path.join(d, BRIEF), "w"))
        sign(d, "owner", accept_proposed=True)
        open(os.path.join(d, BRIEF), "a").write("# edited\n")
        st_changed = status(d)
        restored = restore(d)
        os.remove(os.path.join(d, BRIEF))
        st_deleted = status(d)
        restore(d)
        ok(st_changed == "changed" and restored == [BRIEF] and st_deleted == "changed" and status(d) == "signed",
           f"DB20: a changed or deleted signed file is restored from the signed copy: {st_changed} {st_deleted}")
        ok(_amendments_ok_ids(["A1", "OS2", "Q10"]) and not _amendments_ok_ids(["Apple", "A", "OSX"]),
           "DB21: amendment ids must be A/C/K/Q/OS plus a number")
        ok("decision_brief_not_signed" not in none_kinds and "decision_brief_not_signed" in owner_kinds,
           f"DB18: an unsigned brief is a finding unless the run asked for no sign-off: {none_kinds} {owner_kinds}")
        before = _sha(os.path.join(d, SIGNOFF))
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--by", "owner", "sign", d, "--accept-proposed"],
                           env=dict(os.environ, MA_SIGNOFF_MODE="owner"), capture_output=True, text=True)
        ok(r.returncode != 0 and "agents cannot sign" in r.stderr and _sha(os.path.join(d, SIGNOFF)) == before,
           f"DB19: in owner mode the agents' environment cannot sign, flags in any order: {r.stderr[:200]}")
    if fails:
        print("FAIL:", *fails, sep="\n  ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Decision brief: show, sign, check")
    ap.add_argument("cmd", nargs="?", choices=["show", "sign", "status", "restore"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--by", default="owner")
    ap.add_argument("--answers", help="YAML mapping Q-id -> answer")
    ap.add_argument("--accept-proposed", action="store_true")
    ap.add_argument("--note", default="")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _run_self_test()
    if not a.cmd or not a.run_dir:
        ap.print_help()
        return 2
    if a.cmd == "show":
        print(render_md(a.run_dir))
        return 0
    if a.cmd == "status":
        print(status(a.run_dir))
        return 0
    if a.cmd == "restore":
        done = restore(a.run_dir)
        print(f"Restored the signed {', '.join(done)}." if done else "Nothing to restore.")
        return 0 if status(a.run_dir) in ("signed", "absent") else 1
    if os.environ.get("MA_SIGNOFF_MODE") == "owner":
        # Set only in the agents' environment (main.py): the owner signs from
        # a shell of their own (Codex review: `--by owner` alone was enough).
        print("This run needs the owner's sign-off; agents cannot sign it.", file=sys.stderr)
        return 1
    if a.by != "owner" and os.environ.get("MA_SIGNOFF_MODE") != "lead":
        print("This run needs the owner's sign-off; the lead cannot sign it.", file=sys.stderr)
        return 1
    try:
        answers = _load(a.answers) if a.answers else None
        rec = sign(a.run_dir, a.by, answers, a.note, a.accept_proposed)
    except (ValueError, OSError, yaml.YAMLError) as e:
        print(f"Not signed: {e}", file=sys.stderr)
        return 1
    print(f"Signed by {rec['signed_by']} at {rec['signed_at']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
