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
    done_when: list | None = None
    decision_test: dict | None = None
    events: list = field(default_factory=list)

    def to_row(self) -> dict:
        return asdict(self)


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
    tmp = p + ".tmp"
    rows = sorted(ledger.values(), key=lambda f: (f.status != "open", _SEV_ORDER.get(f.severity, 3), f.id))
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r.to_row(), sort_keys=True, default=str) + "\n")
    os.replace(tmp, p)
    return p


def _event(f: Finding, rnd, status: str, by: str, note: str = "") -> None:
    if f.events and f.events[-1].get("status") == status and f.events[-1].get("round") == rnd:
        return
    f.events.append({"round": rnd, "status": status, "by": by, **({"note": note} if note else {})})


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
                last_seen_round=doc.get("round"), claim=str(b.get("claim") or "")[:600],
                done_when=b.get("done_when") if isinstance(b.get("done_when"), list) else None))
    return found, problems


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
                claim=str(v.get("claim") or v.get("evidence") or "")[:600])
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
                             claim=str(b.get("claim") or "")[:600])
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
                claim=str(b.get("claim") or "")[:600])).blocking = True
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


def owner_answers(run_dir: str) -> dict[str, str]:
    """Phase 49 β answers (finding -> accept | decline), read when scope_signoff.json exists."""
    p = os.path.join(run_dir, "scope_signoff.json")
    try:
        doc = json.load(open(p))
    except (OSError, ValueError):
        return {}
    out = {}
    for a in doc.get("answers") or []:
        if isinstance(a, dict) and a.get("finding") and a.get("decision") in ("accept", "decline"):
            out[str(a["finding"])] = a["decision"]
    return out


# ------------------------------------------------------------------ sync
def sync(run_dir: str, decision: dict | None, round_n: int | None, post_write: bool = False) -> tuple[dict[str, Finding], list[str]]:
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
    answers = owner_answers(run_dir)
    seen = set()
    for new in crit + gate_rows:
        seen.add(new.id)
        old = led.get(new.id)
        if old is None:
            led[new.id] = new
            _event(new, round_n, new.status, new.source)
            continue
        # keep identity and history; refresh what the source now says
        for k in ("severity", "claim", "owner_state", "owner_state_known", "target_stage", "instances",
                  "scope_declarable", "last_seen_round"):
            setattr(old, k, getattr(new, k))
        if not new.source.startswith("critique"):
            old.blocking = new.blocking
        if new.done_when is not None:
            old.done_when = new.done_when
        if new.resolution_type == "owner_decision":
            old.resolution_type = "owner_decision"
        if old.first_seen_round is None:
            old.first_seen_round = new.first_seen_round
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
    for f in led.values():
        names = {f.id, f.kind, f.key, f"{f.kind}:{f.key}" if f.key else f.kind}
        hit = next((decl[n] for n in names if n and n in decl), None)
        if hit is not None and f.status == "open":
            f.status = "declared"
            _event(f, round_n, "declared", "scope_declaration")
            if f.resolution_type == "fix":
                f.resolution_type = "decision_test"
        if hit is not None and isinstance(hit.get("decision_test"), str):
            f.decision_test = decision_test_result(run_dir, hit["decision_test"])
        if not f.scope_declarable:
            f.resolution_type = "fix"
        ans = next((answers[n] for n in names if n and n in answers), None)
        if ans and f.status in ("open", "declared"):
            f.status = "owner_accepted" if ans == "accept" else "owner_declined"
            f.resolution_type = "owner_decision"
            _event(f, round_n, f.status, "owner")
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
