#!/usr/bin/env python3
"""v2 C: the findings ledger (`findings.jsonl`), one typed store for every finding.

    python3 agents/findings_ledger.py show <run_dir>          # sync from the run's files, print the open findings
    python3 agents/findings_ledger.py --self-test

Design: docs/design/v2c-orchestration.md §5. One row per finding, rewritten at
each sync; the row's `events` keep its history. Today's sources stay where they
are and are read by adapters:

  critique YAMLs        critic blockers with their done_when tests
  the gate decision     unresolved_high, accept_blockers, rigor / registry / spec violations
  scope_declaration     which findings are declared, and the decision-test file each cites
  decision tests        the file a declaration cites (Phase 49 α format: decision_changes)
  scope_signoff.json    the owner's answers (Phase 49 β, PR #85), when present

Identity: a critic blocker keeps its id (R-005). A gate finding is `kind:key`
with the key from `stall_rule.finding_key` (or the registry's parameter name);
gate ids (RIG-001) are renumbered every gate and are not identities. A gate
finding with no key is one row per kind, with its instance count.

Nothing here decides an action: the gate does (validate_critique_yaml). The
ledger is what the fix planner reads and what the fix loop updates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

LEDGER = "findings.jsonl"
CRITIQUE_FILES = {"critique-domain": "critique_domain.yaml", "critique-redteam": "critique_redteam.yaml",
                  "critique-presentation": "critique_presentation.yaml", "critique-methods": "critique_methods.yaml",
                  "critique-sufficiency": "critique_sufficiency.yaml"}
STATUSES = ("open", "declared", "fixed", "owner_accepted", "owner_declined")
RESOLUTIONS = ("fix", "decision_test", "owner_decision")
# target_stage / stage (today's vocabulary) -> the machine state that can change the finding.
OWNER_STATE = {
    "PLAN": "SPEC", "PLAN_CRITERIA": "SPEC", "BRIEF": "SPEC", "SPEC": "SPEC",
    "DATA": "CONFIGURE", "MODEL": "CONFIGURE", "PRE_MODEL": "CONFIGURE", "IDENTIFIABILITY": "CONFIGURE",
    "MULTI_STRUCTURAL": "CONFIGURE", "CALIBRATION": "CONFIGURE", "CONFIGURE": "CONFIGURE",
    "EVIDENCE": "EVIDENCE", "ENGINE": "ENGINE_CHOICE",
    "ANALYZE": "ANALYZE", "UQ": "ANALYZE", "OPTIMIZATION": "ANALYZE", "SENSITIVITY": "ANALYZE",
    "GATE": "ANALYZE", "DECISION": "ANALYZE",
    "CRITIQUE": "CRITIQUE",
    "WRITE": "WRITE", "PRESENTATION": "PRESENTATION",
}
_SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
CONFLICT_KIND = "evidence_conflict_declared"      # W0-F: toolkit/analysis/conflicts.py FINDING_KIND


@dataclass
class Finding:
    id: str
    kind: str
    severity: str
    source: str                       # critique-domain | rigor-artifacts | effect-size-registry | spec-compliance | ...
    key: str = ""
    owner_state: str = "ANALYZE"
    owner_state_known: bool = True    # False: the stage was not one we map; kept as given, never guessed
    target_stage: str = ""
    resolution_type: str = "fix"
    status: str = "open"
    blocking: bool = False            # in the gate's unresolved_high / accept_blockers
    scope_declarable: bool = True
    first_seen_round: int | None = None
    last_seen_round: int | None = None
    instances: int = 1
    claim: str = ""
    fix_requires: str | None = None     # F6 S1-7: the required remedy, in full
    done_when: list | None = None
    decision_test: dict | None = None
    events: list = field(default_factory=list)

    def to_row(self) -> dict:
        return asdict(self)


_DW_ROWS = ("done_when_regressed", "done_when_unmet", "done_when_dropped")    # gate rows keyed by a finding's id


def tests_of(dw) -> list | None:
    """A finding's done_when as the ledger keeps it: a list of checks (one mapping is a list of one, as
    scripts/done_when.py reads it), or None."""
    t = dw if isinstance(dw, list) else [dw] if isinstance(dw, dict) else None
    try:          # as save() writes it: a test it could not write (YAML keys of mixed types) is no test here
        json.dumps(t, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        return None
    return t


def frozen_tests(run_dir: str) -> dict[str, list | None]:
    """{finding id: the test the gate froze} (done_when_registry.json; scripts/done_when.py α): what a fix is checked
    against, whatever the critic's YAML says later. A withdrawn test is None: the finding then has no test, and the
    next critique judges it."""
    _scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    import done_when as DW
    try:
        tests = DW.load_registry(run_dir)["tests"]
    except Exception:  # noqa: BLE001 -- a registry that cannot be decoded: the gate reports it (done_when_unavailable)
        return {}
    return {str(k): None if t.get("withdrawn") else tests_of(t["done_when"]) for k, t in tests.items()
            if isinstance(t, dict) and (t.get("withdrawn") or tests_of(t.get("done_when")))}


def remedy(f: dict) -> str | None:
    """A finding's required remedy, in full (critics write `fix_requires`)."""
    r = f.get("fix_requires") or f.get("required_remedy") or f.get("remedy")
    return " ".join(str(r).split()) if r else None


def short_label(text: str, n: int = 120) -> str:
    """F6 S1-7: a short label for a field that must be short. It never replaces the full text, which is
    kept beside it; a cut label ends with an ellipsis."""
    t = " ".join(str(text or "").split())
    first = t.split(". ")[0]
    return first if len(first) <= n else first[:n - 1].rstrip() + "…"


def owner_state_for(stage: str | None) -> tuple[str, bool]:
    s = str(stage or "").upper()
    if s in OWNER_STATE:
        return OWNER_STATE[s], True
    return (s or "ANALYZE"), False


def gate_key(v: dict) -> str:
    import stall_rule
    k = stall_rule.finding_key(v)
    if not k and isinstance(v.get("name"), str) and v["name"].strip() and v["name"] != "-":
        k = v["name"].strip()
    return k


def gate_id(v: dict) -> str:
    k = gate_key(v)
    return f"{v.get('kind')}:{k}" if k else str(v.get("kind"))


# ------------------------------------------------------------------ load / save
def load(run_dir: str) -> dict[str, Finding]:
    out: dict[str, Finding] = {}
    p = os.path.join(run_dir, LEDGER)
    if not os.path.exists(p):
        return out
    names = set(Finding.__dataclass_fields__)
    with open(p, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as e:
                raise ValueError(f"{LEDGER} line {n} is not JSON: {e}") from e   # fail closed
            if not isinstance(row, dict) or "id" not in row:
                raise ValueError(f"{LEDGER} line {n} is not a finding row")
            out[row["id"]] = Finding(**{k: v for k, v in row.items() if k in names})
    return out


def save(run_dir: str, ledger: dict[str, Finding]) -> str:
    p = os.path.join(run_dir, LEDGER)
    rows = sorted(ledger.values(), key=lambda f: (f.status != "open", _SEV_ORDER.get(f.severity, 3), f.id))
    text = "".join(json.dumps(r.to_row(), sort_keys=True, default=str) + "\n" for r in rows)
    _scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    from lib.safe_io import write_nofollow       # atomic, no link followed (review round 4, P1-2)
    write_nofollow(run_dir, LEDGER.replace(os.sep, "/"), text.encode(), atomic=True)
    return p


def _event(f: Finding, rnd, status: str, by: str, note: str = "", **extra) -> None:
    if f.events and f.events[-1].get("status") == status and f.events[-1].get("round") == rnd:
        return
    f.events.append({"round": rnd, "status": status, "by": by, **({"note": note} if note else {}), **extra})


# ------------------------------------------------------------------ adapters
def _yaml(path: str):
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def from_critiques(run_dir: str) -> tuple[list[Finding], list[str]]:
    """Critic blockers (every severity) from today's critique YAMLs."""
    found, problems = [], []
    for reviewer, name in CRITIQUE_FILES.items():
        p = os.path.join(run_dir, name)
        if not os.path.exists(p):
            continue
        try:
            doc = _yaml(p) or {}
        except Exception as e:  # noqa: BLE001 -- fail closed: reported, not skipped silently
            problems.append(f"{name}: unreadable ({type(e).__name__}: {e})")
            continue
        for b in doc.get("blockers") or []:
            if not isinstance(b, dict) or not b.get("id"):
                problems.append(f"{name}: a blocker without an id")
                continue
            st, known = owner_state_for(b.get("target_stage"))
            found.append(Finding(
                id=str(b["id"]), kind="critique_blocker", severity=str(b.get("severity") or "HIGH").upper(),
                source=reviewer, owner_state=st, owner_state_known=known, target_stage=str(b.get("target_stage") or ""),
                status="fixed" if b.get("resolved") else "open", first_seen_round=b.get("first_seen_round"),
                last_seen_round=doc.get("round"), claim=str(b.get("claim") or ""), fix_requires=remedy(b),
                done_when=tests_of(b.get("done_when"))))
    return found, problems


def from_conflicts(run_dir: str) -> tuple[list[Finding], list[str]]:
    """W0-F: one HIGH evidence_conflict_declared finding per target the analysis held out (toolkit/analysis/
    conflicts.py findings): open, for the owner, when its test changes the plan or did not run; else declared."""
    from toolkit.analysis import conflicts as CF
    rows, probs = CF.findings(run_dir)
    return [Finding(severity="HIGH", source="harness-conflicts", owner_state="ANALYZE", target_stage="ANALYZE",
                    blocking=r["status"] == "open", **r) for r in rows], probs


def from_gate(decision: dict, round_n: int | None) -> tuple[list[Finding], list[str]]:
    """Every finding in a gate decision (validate_critique_yaml --json output)."""
    problems = []
    if not isinstance(decision, dict):
        return [], ["gate decision is not a mapping"]
    blocking_kinds: dict[str, int] = {}
    crit_blocking = set()
    for b in list(decision.get("unresolved_high") or []) + list(decision.get("accept_blockers") or []):
        if not isinstance(b, dict):
            problems.append("gate: an unresolved entry is not a mapping")
            continue
        if str(b.get("reviewer") or "").startswith("critique"):
            crit_blocking.add(str(b.get("id")))
        else:
            blocking_kinds[str(b.get("kind"))] = blocking_kinds.get(str(b.get("kind")), 0) + 1
    rows: dict[str, Finding] = {}
    sources = (("rigor_violations", "rigor-artifacts"), ("registry_violations", "effect-size-registry"),
               ("spec_violations", "spec-compliance"))
    for field_name, src in sources:
        for v in decision.get(field_name) or []:
            if not isinstance(v, dict) or not v.get("kind"):
                problems.append(f"gate: a {field_name} entry without a kind")
                continue
            fid = gate_id(v)
            sev = str(v.get("severity") or "MEDIUM").upper()
            if fid in rows:
                f = rows[fid]
                f.instances += 1
                if _SEV_ORDER.get(sev, 3) < _SEV_ORDER.get(f.severity, 3):
                    f.severity = sev
                continue
            stage = v.get("target_stage") or v.get("stage")
            st, known = owner_state_for(stage)
            rows[fid] = Finding(
                id=fid, kind=str(v["kind"]), key=gate_key(v), severity=sev, source=src, owner_state=st,
                owner_state_known=known, target_stage=str(stage or ""),
                scope_declarable=v.get("scope_declarable") is not False,
                first_seen_round=v.get("first_seen_round", round_n), last_seen_round=round_n,
                claim=str(v.get("claim") or v.get("evidence") or ""), fix_requires=remedy(v),
                done_when=tests_of(v.get("done_when")))       # a done_when row carries its finding's test
            if v.get("owner_signoff"):
                rows[fid].resolution_type = "owner_decision"
    # Blocking gate entries whose kind no violation list carries (e.g. a required artifact).
    for b in list(decision.get("unresolved_high") or []) + list(decision.get("accept_blockers") or []):
        if not isinstance(b, dict) or str(b.get("reviewer") or "").startswith("critique"):
            continue
        kind = str(b.get("kind"))
        if any(f.kind == kind for f in rows.values()):
            continue
        st, known = owner_state_for(b.get("target_stage"))
        rows[kind] = Finding(id=kind, kind=kind, severity="HIGH", source=str(b.get("reviewer") or "gate"),
                             owner_state=st, owner_state_known=known, target_stage=str(b.get("target_stage") or ""),
                             scope_declarable=b.get("scope_declarable") is not False,
                             first_seen_round=b.get("first_seen_round", round_n), last_seen_round=round_n,
                             claim=str(b.get("claim") or ""), fix_requires=remedy(b))
        if b.get("owner_signoff"):
            rows[kind].resolution_type = "owner_decision"
    for f in rows.values():
        f.blocking = f.kind in blocking_kinds
        if f.blocking:
            f.severity = "HIGH"
    # Critic blockers the gate holds open (their full rows come from the YAMLs).
    for b in decision.get("unresolved_high") or []:
        if isinstance(b, dict) and str(b.get("id")) in crit_blocking:
            st, known = owner_state_for(b.get("target_stage"))
            rows.setdefault(str(b["id"]), Finding(
                id=str(b["id"]), kind="critique_blocker", severity="HIGH", source=str(b.get("reviewer")),
                owner_state=st, owner_state_known=known, target_stage=str(b.get("target_stage") or ""),
                first_seen_round=b.get("first_seen_round"), last_seen_round=round_n,
                claim=str(b.get("claim") or ""), fix_requires=remedy(b))).blocking = True
    return list(rows.values()), problems


def declarations(run_dir: str) -> tuple[dict[str, dict], list[str]]:
    """{declared id or kind -> the declaration entry} from scope_declaration.yaml."""
    p = os.path.join(run_dir, "scope_declaration.yaml")
    if not os.path.exists(p):
        return {}, []
    try:
        doc = _yaml(p) or {}
    except Exception as e:  # noqa: BLE001
        return {}, [f"scope_declaration.yaml unreadable ({type(e).__name__})"]
    out = {}
    for e in doc.get("declarations") or []:
        if not isinstance(e, dict):
            continue
        for k in ("blocker_id", "finding", "id_declared", "kind"):
            v = e.get(k)
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, str) and x.strip():
                    out[x.strip()] = e
    return out, []


def decision_test_result(run_dir: str, rel: str) -> dict:
    """The Phase 49 α result a declaration cites; fail closed on a missing or unreadable file."""
    p = os.path.join(run_dir, rel)
    if not os.path.realpath(p).startswith(os.path.realpath(run_dir) + os.sep):
        return {"file": rel, "valid": False, "problem": "outside the run directory"}
    try:
        doc = _yaml(p) or {}
    except FileNotFoundError:
        return {"file": rel, "valid": False, "problem": "missing"}
    except Exception as e:  # noqa: BLE001
        return {"file": rel, "valid": False, "problem": f"unreadable ({type(e).__name__})"}
    dc = doc.get("decision_changes")
    if not isinstance(dc, bool):
        return {"file": rel, "valid": False, "problem": "decision_changes is not true/false"}
    return {"file": rel, "valid": True, "decision_changes": dc, "finding": doc.get("finding")}


# F6 run 3: the critics write "Owner amendment:" at the start of fix_requires when the fix needs the signed spec changed
# (agents/v2_sdk_tasks.py, critique prompt); only that documented prefix counts (Codex #258 P2-4). agents/fix_loop.py
# reads it from here.
OWNER_AMENDMENT_RE = re.compile(r"^\s*owner amendment\b", re.I)


def remedy_class(fix_requires) -> str:
    """The owner-relevant class of a remedy: an owner amendment of the signed spec, or a fix."""
    return "owner_amendment" if OWNER_AMENDMENT_RE.search(str(fix_requires or "")) else "fix"


CRITIC_KIND = "critique_blocker"     # a critic's blocker (from_critiques)


def answer_fingerprint(fid: str, *, kind, fix_requires, claim=None) -> str:
    """What an owner answer binds to: the finding's id, its kind and its remedy's class (harness baseline hb2 fix 3).
    Not a critic's wording: malaria R2 re-raised two accepted findings, reworded, in each of three rounds, and each
    rewording was asked again. A materially different finding (another kind, or a remedy of another class, e.g. an
    owner amendment become a fix) has another fingerprint and is asked again (the #258 rule). A harness finding's
    claim is measured evidence the harness writes (a conflict's verdict, a gate result), not wording: it stays bound
    (R3-8, Codex round 3 of #258)."""
    key = {"finding": fid, "kind": str(kind or ""), "remedy": remedy_class(fix_requires)}
    if str(kind or "") != CRITIC_KIND:
        key.update(claim=claim, fix_requires=fix_requires)
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def finding_fingerprint(f) -> str:
    """answer_fingerprint of a ledger Finding."""
    return answer_fingerprint(f.id, kind=f.kind, fix_requires=f.fix_requires, claim=f.claim)


def legacy_fingerprint(fid: str, claim, fix_requires) -> str:
    """The fingerprint answers were recorded under before hb2 fix 3 (the finding, its claim and remedy as asked): an
    answer a run recorded then still binds the finding as it was asked (Codex r1 of #357, P1-1: a resumed run never
    loses a recorded decline, nor lets a lead answer over it)."""
    return hashlib.sha256(json.dumps({"finding": fid, "claim": claim, "fix_requires": fix_requires},
                                     sort_keys=True).encode()).hexdigest()[:16]


def answer_for(answers: dict, fid: str, *, kind, fix_requires, claim) -> dict | None:
    """The recorded answer that binds this finding as it reads now, under its fingerprint or the legacy one. When both
    hold an answer, the owner's comes before the lead's and a decline before an accept (Codex r2 of #357, P1-1/P1-2:
    a provisional lead accept or an older accept never stands over the owner's decline)."""
    got = [a for fp in (answer_fingerprint(fid, kind=kind, fix_requires=fix_requires, claim=claim),
                        legacy_fingerprint(fid, claim, fix_requires))
           for a in [answers.get(fp)] if isinstance(a, dict) and str(a.get("finding")) == fid]
    best = min(got, key=lambda a: (a.get("by") != "owner", a.get("decision") != "decline"), default=None)
    if best is None or best.get("by") == "lead":
        # the lead never answers over the owner's decline of this finding (same id and kind) under any wording, an
        # older one included (Codex r3 of #357, P1-3); the owner is asked again under the owner's own sign-off
        dec = next((a for a in answers.values() if isinstance(a, dict) and str(a.get("finding")) == fid
                    and a.get("by") == "owner" and a.get("decision") == "decline"
                    and str(a.get("kind") or kind or "") == str(kind or "")), None)
        if dec is not None and (best is not None or any(isinstance(a, dict) and a.get("by") == "lead"
                                                        for a in answers.values())):
            return dec
    return best


def harness_gap_key(f) -> str:
    """What a declared harness gap binds: the finding's answer fingerprint (its id, kind and remedy class; a harness
    finding's claim and remedy as well), as an owner answer does. Not the critic's wording of the remedy: in harness
    baseline hb3 (fix 6) a critic reworded a declared gap's remedy within its class, the gap no longer matched and the
    fix planner declared it again, one round later. A remedy of another class (a fix become an owner amendment) or
    another kind is looked at again."""
    return finding_fingerprint(f)


def legacy_harness_gap_key(f) -> str:
    """The key a harness gap was recorded under before hb3 fix 6 (the fingerprint and the remedy as written): a resumed
    run's gap still holds while its remedy reads as it did then."""
    return hashlib.sha256(json.dumps({"fp": finding_fingerprint(f), "fix_requires": f.fix_requires},
                                     sort_keys=True).encode()).hexdigest()[:16]


# hb2 fix 1: gate kinds whose resolution is the owner's (agents/stage_machine.py DECISION_BEARING_KINDS)
OWNER_RESOLUTION_KINDS = ("decision_test_changes_plan",)
# hb3 fix 4: a declared decision test that did not run (toolkit/analysis/contract.py DECISION_TEST_NOT_RUN_KIND). The
# harness check computes it at every gate, so its absence means the test ran: an owner answer to it is moot
NOT_RUN_KINDS = ("decision_test_not_run",)
HARNESS_GAP = "harness_gap"          # the ledger event a declared harness gap leaves on its finding (agents/fix_loop.py)


def harness_gaps(led: dict) -> set[str]:
    """Open findings the fix planner declared harness gaps (no file a fix task may edit can fix them) as they read now:
    the gap's event carries harness_gap_key, so a finding whose kind or remedy class was revised since is not one (hb2
    fix 4: no task can work on it, so it is declared, not sent to the fix loop again; hb3 fix 6: a reworded remedy of
    the same class still is one, unless it now names a file a fix task may edit: Codex r1 of #375, P1-1)."""
    return {f.id for f in led.values() if f.status in ("open", "declared")
            and any(_gap_holds(f, e) for e in f.events or [])}


_PATH_TOKEN = re.compile(r"[\w][\w./-]*")


def names_editable_file(text) -> bool:
    """The text names a file a fix task may edit (agents/fix_loop.py in_edit_scope, FIX_EDITABLE). Unknown (the
    scope cannot be read) counts as naming one, so the planner looks at the finding again (fail closed)."""
    try:
        import fix_loop as FX           # lazy: fix_loop imports this module
    except Exception:  # noqa: BLE001
        return True
    toks = (t.rstrip(".,;:") for t in _PATH_TOKEN.findall(str(text or "")))
    return any(("/" in t or "." in t) and FX.in_edit_scope(t) for t in toks)


def _gap_holds(f, e) -> bool:
    """One harness-gap event still declares f: its key binds f as it reads now (the remedy as written, the earlier
    key; or the remedy class), and a remedy reworded since it was declared names no file a fix task may edit."""
    if not (isinstance(e, dict) and e.get("status") == HARNESS_GAP):
        return False
    if e.get("key") == legacy_harness_gap_key(f):
        return True                     # the remedy reads as it did when the gap was declared
    if e.get("key") != harness_gap_key(f):
        return False
    if "remedy" in e and e.get("remedy") == f.fix_requires:
        return True
    return not names_editable_file(f.fix_requires)


def owner_answers(run_dir: str, lead_ok: bool = False) -> dict[str, dict]:
    """The owner's answers (fingerprint -> {finding, decision, ...}) that the harness recorded (scripts/scope_signoff.py
    verified_answers: the answer's hash under a nonce trace.jsonl also holds), signed by the owner. Nothing else counts
    (Codex review of #258 P1-8): not a lead's answer, not one written to scope_signoff.json without the harness.
    `lead_ok` (the run is a --signoff lead run, D2): the lead's recorded answers count too, as provisional ones."""
    try:
        import scope_signoff as SS
        verified, _bad = SS.verified_answers(run_dir)
    except Exception:  # noqa: BLE001 -- unreadable: no answer counts (fail closed)
        return {}
    return {fp: a for fp, a in verified.items() if isinstance(a, dict)
            and (a.get("by") == "owner" or (lead_ok and a.get("by") == "lead" and a.get("provisional") is True))
            and a.get("finding") and a.get("decision") in ("accept", "decline")}


# ------------------------------------------------------------------ sync
def sync(run_dir: str, decision: dict | None, round_n: int | None, post_write: bool = False,
         lead_ok: bool = False) -> tuple[dict[str, Finding], list[str]]:
    """Bring the ledger up to date with the run's files and (optionally) a fresh gate decision.

    A gate finding open before and absent from this gate is `fixed` (pre-write
    gates only: the post-write gate runs more checks, so absence there is not
    evidence of a fix the other way round). A critic blocker follows its YAML."""
    led = load(run_dir)
    problems: list[str] = []
    crit, p1 = from_critiques(run_dir)
    problems += p1
    gate_rows, p2 = from_gate(decision, round_n) if decision is not None else ([], [])
    problems += p2
    decl, p3 = declarations(run_dir)
    problems += p3
    answers = owner_answers(run_dir, lead_ok)
    conf, p4 = from_conflicts(run_dir)          # W0-F: declared evidence conflicts (analysis/conflicts.yaml)
    problems += p4
    # hb2 fix 1: the gate findings whose resolution is the owner's and which this gate read as accepted
    rv = decision.get("rigor_violations") if isinstance(decision, dict) else None      # P2-15: guard before .get
    signed = {gate_id(v): v["owner_signed"] for v in (rv if isinstance(rv, list) else [])
              if isinstance(v, dict) and v.get("owner_resolution")
              and isinstance(v.get("owner_signed"), dict) and not v.get("owner_signoff")}

    def held(f) -> bool:      # the answer still binds the finding as it reads now (hb2 fix 3: not its wording)
        if f.id in signed:
            return f.status == "owner_accepted"
        a = answer_for(answers, f.id, kind=f.kind, fix_requires=f.fix_requires, claim=f.claim)
        # the answer that binds it now, and the status says that answer (Codex r2 of #357, P1-1)
        return a is not None and {"accept": "owner_accepted", "decline": "owner_declined"}.get(a.get("decision")) \
            == f.status
    seen = set()
    for new in crit + gate_rows + conf:
        seen.add(new.id)
        old = led.get(new.id)
        if old is None:
            led[new.id] = new
            _event(new, round_n, new.status, new.source)
            continue
        # keep identity and history; refresh what the source now says
        for k in ("severity", "claim", "fix_requires", "owner_state", "owner_state_known", "target_stage", "instances",
                  "scope_declarable", "last_seen_round"):
            if not (k == "fix_requires" and new.fix_requires is None):   # a gate row without one keeps the remedy
                setattr(old, k, getattr(new, k))
        if not new.source.startswith("critique"):
            old.blocking = new.blocking
        if new.done_when is not None:
            old.done_when = new.done_when
        if new.resolution_type == "owner_decision":
            old.resolution_type = "owner_decision"
        if old.first_seen_round is None:
            old.first_seen_round = new.first_seen_round
        if new.kind == CONFLICT_KIND and old.status not in ("owner_accepted", "owner_declined"):
            old.resolution_type, old.blocking = new.resolution_type, new.blocking     # W0-F: the current test decides
            if new.status != old.status:
                old.status = new.status
                _event(old, round_n, new.status, new.source)
            continue
        if old.status in ("owner_accepted", "owner_declined") and new.status == "open" and held(old):
            continue                         # carried forward by the critic, still the owner's answer (hb2 fix 3)
        if new.status != old.status and not (old.status == "declared" and new.status == "open"):
            old.status = new.status
            _event(old, round_n, new.status, new.source)
    # A critic blocker blocks when the gate holds it open (done_when and brief scope can close
    # it there); without a gate decision, when it is an open HIGH.
    held = ({str(b.get("id")) for b in decision.get("unresolved_high") or []
             if isinstance(b, dict) and str(b.get("reviewer") or "").startswith("critique")}
            if isinstance(decision, dict) else None)
    for f in led.values():
        if f.source.startswith("critique"):
            f.blocking = (f.id in held) if held is not None else (f.severity == "HIGH" and f.status == "open")
    if decision is not None and not post_write:
        for f in led.values():
            if f.id not in seen and not f.source.startswith("critique") and f.status in ("open", "declared"):
                f.status, f.blocking = "fixed", False
                _event(f, round_n, "fixed", "gate", "absent at this gate")
    if decision is not None:
        # an accepted plan-changing test this gate (pre- or post-write) no longer reports: the plan does not depend on
        # it now, and the report does not list it (Codex r2 of #357, P1-8)
        for f in led.values():
            if f.id not in seen and f.kind in OWNER_RESOLUTION_KINDS and f.status == "owner_accepted":
                f.status, f.blocking = "fixed", False
                _event(f, round_n, "fixed", "gate", "absent at this gate")
            # a test that did not run and runs now: neither an accept nor a decline of going on without it holds the
            # run any more (Codex r1 of #375, P1-4: a decline kept a corrected run from finishing)
            elif f.id not in seen and f.kind in NOT_RUN_KINDS and f.status in ("owner_accepted", "owner_declined"):
                f.status, f.blocking = "fixed", False
                _event(f, round_n, "fixed", "gate", "absent at this gate: the decision test ran")
    frozen = frozen_tests(run_dir)
    for f in led.values():
        fk = f.id if f.source.startswith("critique") else f.key if f.kind in _DW_ROWS else None
        if fk in frozen:
            f.done_when = frozen[fk]         # the frozen test (none once withdrawn), never a critic's later edit
        # a regressed or unmet row is keyed by its finding's id: a declaration of that finding does not declare it
        names = {f.id, f.kind, None if f.kind in _DW_ROWS[:2] else f.key, f"{f.kind}:{f.key}" if f.key else f.kind}
        hit = next((decl[n] for n in names if n and n in decl), None)
        if hit is not None and f.status == "open" and f.kind != CONFLICT_KIND:   # W0-F: only the owner closes one
            f.status = "declared"
            _event(f, round_n, "declared", "scope_declaration")
            if f.resolution_type == "fix":
                f.resolution_type = "decision_test"
        if hit is not None and isinstance(hit.get("decision_test"), str):
            f.decision_test = decision_test_result(run_dir, hit["decision_test"])
        if not f.scope_declarable:
            f.resolution_type = "fix"
        a = answer_for(answers, f.id, kind=f.kind, fix_requires=f.fix_requires, claim=f.claim)   # hb2 fix 3
        ans = a.get("decision") if a and str(a.get("finding")) == f.id else None
        if ans is None and f.resolution_type == "owner_decision" and f.status in ("open", "declared") \
                and any(isinstance(e, dict) and e.get("by") in ("owner", "lead") for e in f.events):
            f.resolution_type = "fix"        # answered as it was before; revised since, so it is asked again
        if ans and f.status in ("open", "declared"):
            f.status = "owner_accepted" if ans == "accept" else "owner_declined"
            f.resolution_type = "owner_decision"
            _event(f, round_n, f.status, "lead" if a.get("by") == "lead" else "owner")
        s_ = signed.get(f.id)
        if s_ is not None and f.status == "owner_accepted":
            f.blocking = False             # from_gate blocks by kind; an accepted one does not block (P2-12)
            last = next((e for e in reversed(f.events or []) if isinstance(e, dict)
                         and e.get("status") == "owner_accepted"), {})
            if s_.get("fingerprint") and last.get("fingerprint") != s_.get("fingerprint"):
                # another accepted version of the test is the one this gate read: its accept is the record
                # (Codex r3 of #357, P1-4), whatever was accepted last
                f.events.append({"round": round_n, "status": "owner_accepted",
                                 "by": "lead" if s_.get("by") == "lead" else "owner",
                                 **({"provisional": True} if s_.get("provisional") else {}),
                                 **{k: s_[k] for k in ("at", "record", "result_sha256", "fingerprint") if s_.get(k)}})
        if s_ is not None and f.status in ("open", "declared"):
            f.status, f.resolution_type, f.blocking = "owner_accepted", "owner_decision", False
            _event(f, round_n, f.status, "lead" if s_.get("by") == "lead" else "owner",
                   **({"provisional": True} if s_.get("provisional") else {}),
                   **{k: s_[k] for k in ("at", "record", "result_sha256", "fingerprint") if s_.get(k)})
        elif f.id not in signed and f.status == "owner_accepted" and f.kind in OWNER_RESOLUTION_KINDS \
                and decision is not None and f.id in seen:
            f.status, f.resolution_type = "open", "owner_decision"     # asked again: its evidence changed
            _event(f, round_n, "open", "gate", "the accepted decision test reads differently now")
    save(run_dir, led)
    return led, problems


def open_findings(led: dict[str, Finding], blocking_only: bool = False) -> list[Finding]:
    xs = [f for f in led.values() if f.status in ("open", "owner_declined") and (f.blocking or not blocking_only)]
    return sorted(xs, key=lambda f: (not f.blocking, _SEV_ORDER.get(f.severity, 3), f.owner_state, f.id))


def digest(f: Finding) -> str:
    return hashlib.sha1(json.dumps(f.to_row(), sort_keys=True, default=str).encode()).hexdigest()[:10]


# ------------------------------------------------------------------ self-test
def _self_test() -> int:
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        yaml.safe_dump({"reviewer": "critique-redteam", "round": 1, "verdict": "REVISE",
                        "structural_mismatch": {"detected": False}, "carried_forward": [],
                        "blockers": [{"id": "R-001", "severity": "HIGH", "category": "METHODS", "target_stage": "MODEL",
                                      "first_seen_round": 1, "claim": "c", "resolved": False,
                                      "done_when": [{"file": "x.txt", "exists": True}]},
                                     {"id": "R-002", "severity": "MEDIUM", "category": "PRESENTATION",
                                      "target_stage": "WRITE", "first_seen_round": 1, "claim": "w", "resolved": False}]},
                       open(os.path.join(d, "critique_redteam.yaml"), "w"))
        dec = {"action": "PATCH_OR_RETHINK",
               "unresolved_high": [{"reviewer": "critique-redteam", "id": "R-001", "kind": "critique_blocker",
                                    "target_stage": "MODEL", "claim": "c", "first_seen_round": 1},
                                   {"reviewer": "rigor-artifacts", "id": "RIG-001", "kind": "benchmark_drift",
                                    "target_stage": "MODEL", "claim": "MODEL/benchmark_drift: B15"}],
               "accept_blockers": [{"reviewer": "rigor-artifacts", "id": "REQ-001", "kind": "required_artifact_missing",
                                    "target_stage": "WRITE", "claim": "absent", "scope_declarable": True}],
               "rigor_violations": [{"kind": "benchmark_drift", "severity": "HIGH", "stage": "MODEL", "key": "B15", "claim": "B15"},
                                    {"kind": "benchmark_drift", "severity": "HIGH", "stage": "MODEL", "key": "B19", "claim": "B19"},
                                    {"kind": "figure_validator_missing", "severity": "MEDIUM", "stage": "ANALYZE", "claim": "f1"},
                                    {"kind": "figure_validator_missing", "severity": "MEDIUM", "stage": "ANALYZE", "claim": "f2"},
                                    {"kind": "odd_thing", "severity": "MEDIUM", "stage": "NOVEL_STAGE", "claim": "?"}],
               "registry_violations": [{"kind": "param_not_in_code", "severity": "HIGH", "name": "itn_rr", "claim": "r"}]}
        led, probs = sync(d, dec, 1)
        ok(not probs, f"L1 sync reads critiques and a gate decision without problems {probs}")
        ok({"R-001", "R-002", "benchmark_drift:B15", "benchmark_drift:B19", "figure_validator_missing",
            "param_not_in_code:itn_rr", "required_artifact_missing"} <= set(led), f"L2 identities {sorted(led)}")
        ok(led["figure_validator_missing"].instances == 2 and not led["figure_validator_missing"].blocking,
           "L3 a key-less gate kind is one row with its instance count, non-blocking when MEDIUM")
        ok(led["benchmark_drift:B15"].blocking and led["benchmark_drift:B15"].owner_state == "CONFIGURE",
           "L4 a blocking gate finding maps MODEL -> CONFIGURE")
        ok(led["R-002"].owner_state == "WRITE" and led["R-001"].done_when, "L5 critic rows keep owner state and done_when")
        ok(led["odd_thing"].owner_state == "NOVEL_STAGE" and not led["odd_thing"].owner_state_known,
           "L6 an unknown stage is kept as given and flagged, not guessed")
        ok(os.path.exists(os.path.join(d, LEDGER)) and set(load(d)) == set(led), "L7 findings.jsonl round-trips")
        # round 2: B19 gone, B15 declared with a decision test
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({"finding": "benchmark_drift:B15", "decision_changes": False},
                       open(os.path.join(d, "models", "dt_b15.yaml"), "w"))
        yaml.safe_dump({"declarations": [{"id": "SD-1", "blocker_id": "benchmark_drift:B15", "why_unresolved": "w",
                                          "decision_test": "models/dt_b15.yaml"}]},
                       open(os.path.join(d, "scope_declaration.yaml"), "w"))
        dec2 = dict(dec, rigor_violations=dec["rigor_violations"][:1] + dec["rigor_violations"][2:])
        led, _ = sync(d, dec2, 2)
        ok(led["benchmark_drift:B19"].status == "fixed" and led["benchmark_drift:B19"].events[-1]["note"],
           "L8 a gate finding absent at a later pre-write gate is fixed, with an event")
        f15 = led["benchmark_drift:B15"]
        ok(f15.status == "declared" and f15.resolution_type == "decision_test"
           and f15.decision_test.get("decision_changes") is False, "L9 a declared finding carries its decision test")
        led, _ = sync(d, dec2, 2, post_write=True)
        ok(led["benchmark_drift:B19"].status == "fixed", "L10 sync is idempotent")
        dec3 = dict(dec2, rigor_violations=[{"kind": "param_x", "severity": "HIGH", "stage": "MODEL", "key": "k",
                                             "claim": "x", "scope_declarable": False}],
                    unresolved_high=[{"reviewer": "rigor-artifacts", "kind": "param_x", "claim": "x"}])
        led, _ = sync(d, dec3, 3)
        ok(led["param_x:k"].resolution_type == "fix" and not led["param_x:k"].scope_declarable,
           "L11 a non-declarable finding can only be fixed")
        open(os.path.join(d, LEDGER), "a").write("not json\n")
        try:
            load(d)
            ok(False, "L12 an unreadable ledger line fails closed")
        except ValueError:
            ok(True, "L12 an unreadable ledger line fails closed")
        ok(decision_test_result(d, "../x.yaml")["valid"] is False and decision_test_result(d, "nope.yaml")["valid"] is False,
           "L13 a decision test outside the run or missing is invalid")
        # L15 (F6 S1-7): a finding's claim is kept in full, from a critic and from the gate.
        longc = "a claim that runs well past six hundred characters; " * 20
        d15 = os.path.join(d, "l15")
        os.makedirs(d15)
        yaml.safe_dump({"round": 1, "blockers": [{"id": "D-003", "severity": "MEDIUM", "claim": longc,
                                                  "fix_requires": "use a sourced range"}]},
                       open(os.path.join(d15, CRITIQUE_FILES["critique-domain"]), "w"))
        led, _ = sync(d15, {"rigor_violations": [{"kind": "long_gate", "severity": "MEDIUM", "stage": "MODEL",
                                                  "claim": longc}]}, 1)
        ok(led["D-003"].claim == longc and led["long_gate"].claim == longc
           and getattr(load(d15)["D-003"], "fix_requires", None) == "use a sourced range",
           "L15 the ledger keeps each finding's full claim and its required remedy")
        # L16 (round 2): a HIGH critic finding the gate holds open keeps its remedy through sync() and the packet;
        # a gate-only blocking entry brings its own.
        d16 = os.path.join(d, "l16")
        os.makedirs(d16)
        yaml.safe_dump({"round": 1, "blockers": [{"id": "D-010", "severity": "HIGH", "claim": "c",
                                                  "fix_requires": "REMEDY-D010"}]},
                       open(os.path.join(d16, CRITIQUE_FILES["critique-domain"]), "w"))
        dec16 = {"unresolved_high": [{"reviewer": "critique-domain", "id": "D-010", "claim": "c"},
                                     {"reviewer": "rigor-artifacts", "kind": "req_x", "claim": "r",
                                      "fix_requires": "REMEDY-REQ"}]}
        sync(d16, dec16, 1)
        led16, _ = sync(d16, dec16, 2)
        import fix_loop as _FX
        pk = {f["id"]: f for f in _FX.build_packet(d16, led16, 2, 3, dec16, None)["findings"]}
        ok(led16["D-010"].fix_requires == "REMEDY-D010" and (pk.get("D-010") or {}).get("fix_requires") == "REMEDY-D010"
           and (pk.get("req_x") or {}).get("fix_requires") == "REMEDY-REQ",
           f"L16 remedies survive the gate rows through sync() into the planner packet {pk}")
    # L14 (run console review round 4, P1-2): the ledger is not written through a planted .tmp link
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        open(os.path.join(out, "v"), "w").write("untouched")
        os.symlink(os.path.join(out, "v"), os.path.join(d, LEDGER + ".tmp"))
        try:
            save(d, {})
        except (ValueError, OSError):
            pass
        ok(open(os.path.join(out, "v")).read() == "untouched", "L14 the ledger is not written through a planted .tmp link")
    # L17 (F6 run 3; Codex review of #258 P1-8/P1-9): an owner answer counts only once the harness recorded it, and it
    # binds to the finding's id, kind and remedy class (hb2 fix 3): a reworded finding keeps it, a materially
    # different one (another kind or remedy class) with the same id is not answered by it
    with tempfile.TemporaryDirectory() as d:
        import scope_signoff as SS
        claim, fix = "the comparator omits the funded plan", "Owner amendment: add it."
        fp = answer_fingerprint("D-031", kind="critique_blocker", fix_requires=fix)
        json.dump({"answers": {fp: {"finding": "D-031", "decision": "decline", "by": "owner"}}},
                  open(os.path.join(d, "scope_signoff.json"), "w"))
        save(d, {"D-031": Finding(id="D-031", kind="critique_blocker", severity="HIGH", source="harness", claim=claim,
                                  fix_requires=fix)})
        unverified = owner_answers(d)
        SS.harness_record(d)
        led17, _ = sync(d, None, 2)
        st17 = led17["D-031"].status
        led17["D-031"].claim, led17["D-031"].status = "a revised claim", "open"
        save(d, led17)
        led17b, _ = sync(d, None, 3)
        st17b = led17b["D-031"].status
        led17b["D-031"].kind, led17b["D-031"].status = "spec_violation", "open"
        save(d, led17b)
        led17c, _ = sync(d, None, 4)
        import fix_loop as FX_
        ok(unverified == {} and st17 == "owner_declined" and st17b == "owner_declined"
           and led17c["D-031"].status == "open" and FX_.needs_owner_amendment(led17c["D-031"]),
           f"L17 a recorded owner answer applies to the finding it was given for, reworded or not; one of another kind "
           f"is asked again ({unverified}, {st17}, {st17b}, {led17c['D-031'].status})")
    # L18 (hb2 fix 3, malaria R2 D-002): the critic re-raises an owner-accepted finding with reworded text in each
    # round; the answer holds, the row is not flipped open and accepted again, and an "Owner amendment" remedy turned
    # into a fix is asked again
    with tempfile.TemporaryDirectory() as d:
        import scope_signoff as SS
        fix = "Owner amendment: add an external ITN-durability range."
        fp = answer_fingerprint("D-002", kind="critique_blocker", fix_requires=fix)
        json.dump({"answers": {fp: {"finding": "D-002", "decision": "accept", "by": "lead", "provisional": True}}},
                  open(os.path.join(d, "scope_signoff.json"), "w"))
        SS.harness_record(d)

        def crit(rnd, claim, fr):
            yaml.safe_dump({"reviewer": "critique-domain", "round": rnd, "verdict": "REVISE",
                            "structural_mismatch": {"detected": False}, "carried_forward": [],
                            "blockers": [{"id": "D-002", "severity": "MEDIUM", "category": "METHODS",
                                          "target_stage": "PLAN", "first_seen_round": 1, "claim": claim,
                                          "fix_requires": fr, "resolved": False}]},
                           open(os.path.join(d, CRITIQUE_FILES["critique-domain"]), "w"))
        crit(1, "ITN durability is a point value.", fix)
        s1 = sync(d, None, 1, lead_ok=True)[0]["D-002"].status
        crit(2, "ITN durability is a single point value with no range; the lead accepted it in round 1.", fix)
        sync(d, None, 2, lead_ok=True)
        r2 = sync(d, None, 2, lead_ok=True)[0]["D-002"]
        crit(3, "ITN durability is a point value.", "Refit the ITN effect with a decay term.")
        r3 = sync(d, None, 3, lead_ok=True)[0]["D-002"]
        flips = [e.get("status") for e in r2.events if e.get("round") == 2]
        ok(s1 == "owner_accepted" and r2.status == "owner_accepted" and "open" not in flips and r3.status == "open",
           f"L18 a reworded owner-held finding stays held without flipping; a remedy of another class is asked again "
           f"({s1}, {r2.status}, {flips}, {r3.status})")
    # L19 (hb2 fix 1): a gate finding whose resolution is the owner's, accepted at this gate (owner_signed), is
    # owner_accepted in the ledger and no longer blocks; asked again (owner_signoff) it is open; absent, it is fixed
    with tempfile.TemporaryDirectory() as d:
        v = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "key": "DT1",
             "claim": "DT1 changes the plan", "owner_resolution": True}
        acc = {"rigor_violations": [dict(v, owner_signed={"by": "lead", "at": "t", "provisional": True})],
               "unresolved_high": []}
        pend = {"rigor_violations": [dict(v, owner_signoff=True)],
                "unresolved_high": [{"reviewer": "rigor-artifacts", "kind": "decision_test_changes_plan"}]}
        fid = "decision_test_changes_plan:DT1"
        a1 = sync(d, acc, 1)[0][fid]
        ev = a1.events[-1]
        a2 = sync(d, pend, 2)[0][fid]
        st2, bl2 = a2.status, a2.blocking
        sync(d, acc, 3)
        a4 = sync(d, {"rigor_violations": [], "unresolved_high": []}, 4)[0][fid]
        ok(a1.status == "owner_accepted" and not a1.blocking and ev.get("by") == "lead" and ev.get("provisional")
           and st2 == "open" and bl2 and a4.status == "fixed",
           f"L19 a gate finding the owner accepted is held; asked again it is open; gone, it is fixed "
           f"({a1.status}, {ev}, {st2}, {a4.status})")
    # L19b (Codex r1 of #375, P1-4): a declared decision test that did not run, declined (or accepted) by the owner,
    # is fixed once a gate no longer reports it (the test ran), post-write too: the decline no longer holds the run
    for post in (False, True):
        with tempfile.TemporaryDirectory() as d:
            nf = Finding(id="decision_test_not_run:DT3", kind="decision_test_not_run", key="DT3", severity="HIGH",
                         source="rigor-artifacts", status="owner_declined", resolution_type="owner_decision",
                         fix_requires="Owner amendment: change the spec so that decision test DT3 can run.")
            save(d, {nf.id: nf})
            got = sync(d, {"rigor_violations": [], "unresolved_high": []}, 2, post_write=post)[0][nf.id]
            ok(got.status == "fixed" and not got.blocking,
               f"L19b an answered not-run test the gate no longer reports is fixed (post_write={post}: {got.status})")
    # L20 (hb2 fix 4, dengue R2 R-001): a declared harness gap is one while the finding keeps its kind and remedy class
    f20 = Finding(id="R-001", kind="critique_blocker", severity="MEDIUM", source="critique-redteam",
                  fix_requires="The ANALYZE stage emits claims.")
    _event(f20, 1, HARNESS_GAP, "fix_planner", "only harness code", key=harness_gap_key(f20))
    g1 = harness_gaps({"R-001": f20})
    f20.claim = "reworded"
    g2 = harness_gaps({"R-001": f20})
    # hb3 fix 6 (tb_acf R1 D-001): the critic reworded the gap's remedy in round 2, within its class; the gap holds
    f20.fix_requires = "The owner must authorise it: the change is outside the fix loop's edit scope (reworded)."
    g3 = harness_gaps({"R-001": f20})
    f20.fix_requires = "Owner amendment: give the two weights ranges."           # another remedy class: looked at again
    g4 = harness_gaps({"R-001": f20})
    f20.fix_requires = "The ANALYZE stage emits claims."
    f20.kind = "another_kind"                                                     # another kind: looked at again
    g5 = harness_gaps({"R-001": f20})
    f21 = Finding(id="R-002", kind="critique_blocker", severity="MEDIUM", source="critique-redteam")
    _event(f21, 1, HARNESS_GAP, "fix_planner", "an older event without a key")
    ok(g1 == g2 == g3 == {"R-001"} and g4 == g5 == set() and harness_gaps({"R-002": f21}) == set(),
       f"L20 a harness gap binds the finding's id, kind and remedy class, not the remedy's wording "
       f"({g1}, {g2}, {g3}, {g4}, {g5})")
    # L20c (Codex r1 of #375 P1-1): a remedy reworded, within its class, into one that names a file the fix loop may
    # edit (fix_loop.FIX_EDITABLE) reopens the gap: the loop can act on it now. The remedy as declared does not.
    f23 = Finding(id="R-005", kind="critique_blocker", severity="MEDIUM", source="critique-redteam",
                  fix_requires="Change the harness to increase the uncertainty draws.")
    _event(f23, 1, HARNESS_GAP, "fix_planner", "only harness code", key=harness_gap_key(f23), remedy=f23.fix_requires)
    c1 = harness_gaps({"R-005": f23})
    f23.fix_requires = "Increase the draws in the harness code (reworded)."
    c2 = harness_gaps({"R-005": f23})
    f23.fix_requires = "Set `analysis_settings.yaml` uncertainty.draws to 64."
    c3 = harness_gaps({"R-005": f23})
    f23.fix_requires = "Add the missing draw step in models/ext/draws.py."
    c4 = harness_gaps({"R-005": f23})
    f24 = Finding(id="R-006", kind="critique_blocker", severity="MEDIUM", source="critique-redteam",
                  fix_requires="Raise analysis_settings.yaml uncertainty.draws; the harness caps it.")
    _event(f24, 1, HARNESS_GAP, "fix_planner", "the cap is harness code", key=harness_gap_key(f24),
           remedy=f24.fix_requires)
    c5 = harness_gaps({"R-006": f24})
    ok(c1 == c2 == {"R-005"} and c3 == c4 == set() and c5 == {"R-006"},
       f"L20c a reworded remedy naming an editable file reopens the gap; one without, or the remedy as declared, "
       f"does not ({c1}, {c2}, {c3}, {c4}, {c5})")
    # L20b: a gap recorded under the key used before hb3 fix 6 (the remedy as written) holds while the remedy reads so
    f22 = Finding(id="R-003", kind="critique_blocker", severity="MEDIUM", source="critique-redteam",
                  fix_requires="Only harness code can draw it.")
    _event(f22, 1, HARNESS_GAP, "fix_planner", "only harness code", key=legacy_harness_gap_key(f22))
    h1 = harness_gaps({"R-003": f22})
    ok(h1 == {"R-003"} and legacy_harness_gap_key(f22) != harness_gap_key(f22),
       f"L20b a gap recorded under the earlier key still holds on a resumed run ({h1})")
    _fa = answer_fingerprint("k:x", kind="k", fix_requires="r", claim="verdict A")
    ok(_fa != answer_fingerprint("k:x", kind="k", fix_requires="r", claim="verdict B")
       and answer_fingerprint("D-1", kind=CRITIC_KIND, fix_requires="r", claim="a")
       == answer_fingerprint("D-1", kind=CRITIC_KIND, fix_requires="r2", claim="b"),
       "L21 a harness finding's measured claim stays bound; a critic's wording and remedy text within a class do not")
    # L22 (Codex r1 of #357, P1-1): an answer recorded under the fingerprint used before hb2 fix 3 still binds the
    # finding as it was asked, so a resumed run keeps a recorded decline
    with tempfile.TemporaryDirectory() as d:
        import scope_signoff as SS
        claim, fix = "the comparator omits the funded plan", "Owner amendment: add it."
        json.dump({"answers": {legacy_fingerprint("D-033", claim, fix): {"finding": "D-033", "decision": "decline",
                                                                        "by": "owner"}}},
                  open(os.path.join(d, "scope_signoff.json"), "w"))
        SS.harness_record(d)
        save(d, {"D-033": Finding(id="D-033", kind=CRITIC_KIND, severity="HIGH", source="critique-domain",
                                  claim=claim, fix_requires=fix)})
        st22 = sync(d, None, 2)[0]["D-033"].status
        ok(st22 == "owner_declined", f"L22 an answer under the legacy fingerprint still binds the finding ({st22})")
    # L23 (Codex r2 of #357, P1-1/P1-2): the owner's decline under either fingerprint stands over an accept (an older
    # wording's, or the lead's provisional one under the new fingerprint), and an owner_accepted row turns declined
    claim, fix = "the comparator omits the funded plan", "Owner amendment: add it."
    fpn = answer_fingerprint("D-034", kind=CRITIC_KIND, fix_requires=fix, claim=claim)
    ans23 = {fpn: {"finding": "D-034", "decision": "accept", "by": "lead", "provisional": True},
             legacy_fingerprint("D-034", claim, fix): {"finding": "D-034", "decision": "decline", "by": "owner"}}
    a23 = answer_for(ans23, "D-034", kind=CRITIC_KIND, fix_requires=fix, claim=claim)
    with tempfile.TemporaryDirectory() as d:
        import scope_signoff as SS
        json.dump({"answers": {legacy_fingerprint("D-035", "wording A", fix): {"finding": "D-035", "decision": "accept",
                                                                             "by": "owner"},
                               legacy_fingerprint("D-035", "wording B", fix): {"finding": "D-035", "decision": "decline",
                                                                             "by": "owner"}}},
                  open(os.path.join(d, "scope_signoff.json"), "w"))
        SS.harness_record(d)
        save(d, {"D-035": Finding(id="D-035", kind=CRITIC_KIND, severity="HIGH", source="critique-domain",
                                  claim="wording A", fix_requires=fix, status="owner_accepted")})
        yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "verdict": "REVISE",
                        "structural_mismatch": {"detected": False}, "carried_forward": [],
                        "blockers": [{"id": "D-035", "severity": "HIGH", "category": "METHODS", "target_stage": "PLAN",
                                      "first_seen_round": 1, "claim": "wording B", "fix_requires": fix,
                                      "resolved": False}]},
                       open(os.path.join(d, CRITIQUE_FILES["critique-domain"]), "w"))
        st23 = sync(d, None, 2, lead_ok=True)[0]["D-035"].status
    ok((a23 or {}).get("decision") == "decline" and st23 == "owner_declined",
       f"L23 the owner's decline stands over an accept under the other fingerprint ({a23}, {st23})")
    # L24 (Codex r2 of #357, P1-8, P2-12): an accepted plan-changing test stays non-blocking over repeated syncs, and a
    # post-write gate that no longer reports it closes it
    with tempfile.TemporaryDirectory() as d:
        v = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "claim": "c",
             "owner_resolution": True}
        g24 = {"rigor_violations": [dict(v, key="DT1", owner_signed={"by": "owner"}),
                                    dict(v, key="DT2", owner_signoff=True)],
               "unresolved_high": [{"reviewer": "rigor-artifacts", "kind": "decision_test_changes_plan"}]}
        sync(d, g24, 1)
        b24 = sync(d, g24, 1)[0]["decision_test_changes_plan:DT1"].blocking
        s24 = sync(d, {"rigor_violations": [], "unresolved_high": []}, 1, post_write=True)[0][
            "decision_test_changes_plan:DT1"].status
    # L25 (Codex r3 of #357): the lead never answers over an owner's decline under an older wording (P1-3); the
    # accept of the version this gate read is the record (P1-4); a malformed gate document is a problem (P2-15)
    fx_ = "Owner amendment: add it."
    ans25 = {legacy_fingerprint("D-036", "old wording", fx_): {"finding": "D-036", "decision": "decline",
                                                              "by": "owner", "kind": CRITIC_KIND},
             answer_fingerprint("D-036", kind=CRITIC_KIND, fix_requires=fx_, claim="new"): {
                 "finding": "D-036", "decision": "accept", "by": "lead", "provisional": True}}
    a25 = answer_for(ans25, "D-036", kind=CRITIC_KIND, fix_requires=fx_, claim="new wording")
    with tempfile.TemporaryDirectory() as d:
        v = {"kind": "decision_test_changes_plan", "severity": "HIGH", "stage": "ANALYZE", "claim": "c", "key": "DT1",
             "owner_resolution": True}
        mk25 = lambda fp: {"rigor_violations": [dict(v, owner_signed={"by": "owner", "fingerprint": fp})],  # noqa: E731
                           "unresolved_high": []}
        sync(d, mk25("A"), 1)
        sync(d, mk25("B"), 2)
        e25 = [e for e in sync(d, mk25("A"), 3)[0]["decision_test_changes_plan:DT1"].events
               if e.get("status") == "owner_accepted"][-1]
        _l, p25 = sync(d, ["unexpected"], 4)
    ok((a25 or {}).get("decision") == "decline" and e25.get("fingerprint") == "A" and p25,
       f"L25 an owner decline holds against a lead accept under another wording; the accept read now is the record; a "
       f"malformed gate is reported ({a25}, {e25}, {p25})")
    ok(b24 is False and s24 == "fixed",
       f"L24 an accepted test does not block on a second sync; gone post-write, it is closed ({b24}, {s24})")
    # L_OWNER_LEAD (1F, D2): a lead's recorded answer counts only when the run is a --signoff lead run
    with tempfile.TemporaryDirectory() as d:
        import scope_signoff as SS
        claim, fix = "the comparator omits the funded plan", "Owner amendment: add it."
        fp = answer_fingerprint("D-032", kind="critique_blocker", fix_requires=fix)
        json.dump({"answers": {fp: {"finding": "D-032", "decision": "accept", "by": "lead", "provisional": True}}},
                  open(os.path.join(d, "scope_signoff.json"), "w"))
        SS.harness_record(d)
        save(d, {"D-032": Finding(id="D-032", kind="critique_blocker", severity="HIGH", source="harness", claim=claim,
                                  fix_requires=fix)})
        st_o = sync(d, None, 2)[0]["D-032"].status
        led_l = sync(d, None, 2, lead_ok=True)[0]
        ok(owner_answers(d) == {} and list(owner_answers(d, lead_ok=True)) == [fp] and st_o == "open"
           and led_l["D-032"].status == "owner_accepted" and led_l["D-032"].events[-1].get("by") == "lead",
           f"L_OWNER_LEAD a lead's answer counts only with lead_ok, and the ledger says it was the lead's ({st_o})")
    # L_DICT_DW, L_FROZEN_DW, L_GATE_DW (v2 autonomy 1A, 4c and 4a): the test a fix task is checked against.
    with tempfile.TemporaryDirectory() as d:
        frozen, weaker = [{"file": "x.txt", "contains": "draws 32"}], [{"file": "x.txt", "exists": True}]
        doc = {"reviewer": "critique-redteam", "round": 2, "blockers": [
            {"id": "R-041", "severity": "HIGH", "target_stage": "MODEL", "claim": "c", "done_when": frozen[0]},
            {"id": "R-042", "severity": "HIGH", "target_stage": "MODEL", "claim": "c", "done_when": weaker},
            {"id": "R-043", "severity": "HIGH", "target_stage": "MODEL", "claim": "c", "done_when": weaker,
             "done_when_withdrawn": "the output it read no longer exists"}]}
        yaml.safe_dump(doc, open(os.path.join(d, "critique_redteam.yaml"), "w"))
        json.dump({"tests": {"R-042": {"done_when": frozen, "frozen_round": 1},
                             "R-043": {"done_when": frozen, "frozen_round": 1, "withdrawn": {"round": 2, "reason": "x"}}}},
                  open(os.path.join(d, "done_when_registry.json"), "w"))
        led, _ = sync(d, None, 2)
        ok(led["R-043"].done_when is None,
           f"L_WITHDRAWN_DW a withdrawn test is no test for a fix's check, whatever the critique then lists "
           f"(narrow Codex review of #297, 6) ({led['R-043'].done_when})")
        ok(led["R-041"].done_when == frozen, f"L_DICT_DW a test written as one mapping is kept ({led['R-041'].done_when})")
        ok(led["R-042"].done_when == frozen,
           f"L_FROZEN_DW the test the gate froze is kept over a critic's later edit ({led['R-042'].done_when})")
        rows, _ = from_gate({"rigor_violations": [{"kind": "done_when_unmet", "key": "R-9", "severity": "MEDIUM",
                                                   "claim": "c", "done_when": frozen}]}, 2)
        ok(rows[0].id == "done_when_unmet:R-9" and rows[0].done_when == frozen,
           f"L_GATE_DW a gate row that carries a test keeps it ({rows[0].id}, {rows[0].done_when})")
        # Narrow Codex review of #297, round 2 (5-8).
        yaml.safe_dump({"declarations": [{"blocker_id": "R-042", "why": "declared"}]},
                       open(os.path.join(d, "scope_declaration.yaml"), "w"))
        dec2 = {"rigor_violations": [{"kind": k, "key": i, "severity": "HIGH", "claim": "c", "done_when": frozen}
                                     for k, i in (("done_when_regressed", "R-042"), ("done_when_unmet", "R-043"))]}
        led2, _ = sync(d, dec2, 2)
        ok(led2["done_when_regressed:R-042"].status == "open",
           f"L_CX2-5 a declaration of a finding does not declare its regression ({led2['done_when_regressed:R-042'].status})")
        ok(led2["done_when_unmet:R-043"].done_when is None,
           f"L_CX2-6 a gate row of a withdrawn test carries no test ({led2['done_when_unmet:R-043'].done_when})")
        ok(tests_of({"file": "x.csv", "where": {2024: "x", "site": "A"}}) is None,
           "L_CX2-7 a test the ledger could not write (mixed key types) is not kept")
        real_dumps = json.dumps

        def too_deep(*a, **k):
            raise RecursionError("nested too deep")
        json.dumps = too_deep                # what the encoder raises for a test nested too deep (YAML aliases)
        try:
            td_ = tests_of({"file": "x.csv", "exists": True})
        except BaseException as e:  # noqa: BLE001
            td_ = type(e).__name__
        finally:
            json.dumps = real_dumps
        ok(td_ is None, f"L_CX3-4 a test nested too deep to write is not kept, and does not stop the sync ({td_})")
        import datetime
        ok(tests_of({"file": "x.csv", "key": "d", "op": "==", "value": datetime.date(2024, 1, 1)}) is None,
           "L_CX3-3 a test whose value JSON cannot hold as written is not kept")
        open(os.path.join(d, "done_when_registry.json"), "w").write('{"tests": ' + "[" * 200000)
        try:
            ft = frozen_tests(d)
        except BaseException as e:  # noqa: BLE001
            ft = type(e).__name__
        ok(ft == {}, f"L_CX2-8 a registry that cannot be decoded gives no frozen test and does not stop the sync ({ft})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    if argv[:1] == ["show"] and len(argv) == 2:
        dec = None
        fg = os.path.join(argv[1], "final_gate.json")
        if os.path.exists(fg):
            dec = json.load(open(fg))
        led, probs = sync(argv[1], dec, None, post_write=True)
        for f in open_findings(led):
            print(f"{f.id:40s} {f.severity:6s} {f.owner_state:12s} {f.resolution_type:14s} {f.source}")
        for p in probs:
            print("PROBLEM:", p)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
