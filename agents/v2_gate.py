"""Gate evaluation and routing for the v2 modeling stage machine (Wave B2).

Wraps `scripts/validate_critique_yaml.py`, mechanical `done_when` checks, owner-rule
sign-off (`scripts/scope_signoff.py`), medium-finding routing, and `apply_owner_declines`.
Leaf module in the `stage_machine` split DAG: imports only stdlib, `agents/findings_ledger`,
`scripts/*`, and `toolkit/analysis/contract` lazily.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _p in (_ROOT, _SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import findings_ledger as FL

CRITICS = ("critique-domain", "critique-redteam")
DECISION_BEARING_KINDS = ("decision_test_changes_plan",)
INTERVAL_KIND = "analysis_interval_degenerate"
OWNER_LIFECYCLE_WIRED = True
DT_DISPOSITION = "accept_with_dependence_stated"

_remedy, short_label = FL.remedy, FL.short_label


def rethink_target(decision: dict, *, built_engine: bool = False, engine_repairs_left: bool = False) -> str:
    """RETHINK_STRUCTURAL back-edge: SPEC when a structural blocker targets PLAN, ENGINE_BUILD when a blocker
    targets ENGINE on a built-engine run with a repair left, else ENGINE_CHOICE."""
    for b in decision.get("unresolved_high") or []:
        if isinstance(b, dict) and b.get("category") == "STRUCTURAL" and b.get("target_stage") == "PLAN":
            return "SPEC"
    if built_engine and engine_repairs_left:
        for b in decision.get("unresolved_high") or []:
            if isinstance(b, dict) and b.get("target_stage") == "ENGINE":
                return "ENGINE_BUILD"
    return "ENGINE_CHOICE"


def route_gate(decision: dict | None, post_write: bool, *, built_engine: bool = False,
               engine_repairs_left: bool = False) -> tuple[str, str | None, str | None]:
    """(next state, terminal status or None, write mode or None) for a gate decision."""
    if not isinstance(decision, dict):
        return "FINAL", "completed_gate_error", None
    if decision.get("schema_errors"):
        return "CRITIQUE", None, None
    a = decision.get("action")
    if a == "ACCEPT":
        return ("FINAL", "completed", None) if post_write else ("WRITE", None, "accepted")
    if a == "DECLARE_SCOPE":
        return ("FINAL", "completed_scope_declared", None) if post_write else ("WRITE", None, "declared")
    if a == "RUN_FAILED":
        owner_declined = any(isinstance(u, dict) and u.get("kind") == "scope_declaration_declined"
                             for u in decision.get("unresolved_high") or [])
        status = "completed_scope_declined" if owner_declined else "run_failed"
        return ("FINAL", status, None) if post_write else ("WRITE", None, "not_accepted")
    if a == "PATCH_OR_RETHINK":
        if built_engine and engine_repairs_left and any(
            isinstance(u, dict) and u.get("target_stage") == "ENGINE"
            for u in decision.get("unresolved_high") or []
        ):
            return "ENGINE_BUILD", None, None
        return "FIX_LOOP", None, None
    if a == "RETHINK_STRUCTURAL":
        return rethink_target(decision, built_engine=built_engine, engine_repairs_left=engine_repairs_left), None, None
    if a == "AWAIT_OWNER":
        return "OWNER_DECISION", None, None
    return "FINAL", "completed_gate_error", None


def _owner_rule():
    """Return scripts/scope_signoff when OWNER_LIFECYCLE_WIRED and validator AWAIT_OWNER path exist."""
    if not OWNER_LIFECYCLE_WIRED:
        return None
    import inspect
    import validate_critique_yaml as V
    try:
        import scope_signoff
        if "AWAIT_OWNER" not in inspect.getsource(V._recompute_action):
            return None
    except (ImportError, OSError, TypeError):
        return None
    return scope_signoff


def _bearing_record(run_dir: str, v: dict):
    """Verified row for a toolkit decision-bearing finding; None when absent."""
    if v.get("source") != "toolkit/analysis/contract.py":
        return None
    try:
        from toolkit.analysis import contract as _CT
        return _CT.decision_test_record(run_dir, v.get("key"))
    except Exception:  # noqa: BLE001 -- no evidence: binds claim alone
        return None


def _bearing_fp(v: dict, ev) -> str:
    key = {"finding": FL.gate_id(v), "claim": v.get("claim", ""), "evidence": ev}
    if v.get("kind") in DECISION_BEARING_KINDS:
        key["disposition"] = DT_DISPOSITION
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _bearing_item(run_dir: str, v: dict) -> dict:
    """Owner request item for a decision-bearing finding, binding its claim, record, and disposition."""
    ev = _bearing_record(run_dir, v)
    gid = FL.gate_id(v)
    why = ("a measured decision test changes the plan" if v.get("kind") in DECISION_BEARING_KINDS
           else "the headline's uncertainty interval has next to no width" if v.get("kind") == INTERVAL_KIND
           else "the finding bears on the decision")
    return {"finding": gid, "kind": v.get("kind"), "reason": why,
            "claim": v.get("claim", ""), "declarations": [], "declared": False, "fingerprint": _bearing_fp(v, ev),
            "record": "toolkit",
            "result_sha256": (ev or {}).get("result_sha256") if isinstance(ev, dict) else None}


def _answer_counts(a: dict, mode: str) -> bool:
    return a.get("by") == "owner" or (a.get("by") == "lead" and mode == "lead" and a.get("provisional") is True)


def apply_owner_rule(run_dir: str, round_n: int, viols: list[dict], rule="auto", mode: str = "owner") -> list[dict]:
    """Decision-bearing findings pass through scripts/scope_signoff before declaration."""
    import validate_critique_yaml as V
    rule = _owner_rule() if rule == "auto" else rule
    bearing = [v for v in viols if v.get("decision_bearing") or v.get("kind") in DECISION_BEARING_KINDS]
    if not bearing:
        if rule is None or mode == "none":
            return []
        marked = {id(v) for v in viols if v.get("owner_signoff")}
        try:
            out = [f for f in rule.gate_findings(run_dir, round_n, viols, non_declarable=V.is_non_declarable,
                                                 mode=mode, provisional_only=True) or []
                   if f.get("kind") == "scope_declaration_declined"]
        except Exception:  # noqa: BLE001 -- nothing marked, nothing declined
            out = []
        for v in viols:
            if v.get("owner_signoff") and id(v) not in marked:
                v.pop("owner_signoff", None)
                v.pop("owner_item", None)
        return out
    if rule is not None and mode != "none":
        out = []
        try:
            answers = (getattr(rule, "verified_answers", None) or (lambda rd: ({}, [])))(run_dir)[0]
            for v in bearing:
                if v.get("kind") in DECISION_BEARING_KINDS:
                    v["owner_resolution"] = True
                item = _bearing_item(run_dir, v)
                a = answers.get(item["fingerprint"]) or {}
                if str(a.get("finding")) == item["finding"] and _answer_counts(a, mode):
                    if a.get("decision") == "accept":
                        v["owner_signed"] = {"by": a.get("by"), "at": a.get("at"), "fingerprint": item["fingerprint"],
                                             "record": item["record"], "result_sha256": item["result_sha256"],
                                             **({"provisional": True} if a.get("provisional") else {})}
                        continue
                    v["scope_declarable"], v["owner_declined"] = False, True
                    out.append({"kind": "scope_declaration_declined", "severity": "HIGH", "stage": "GATE",
                                "target_stage": "MODEL", "scope_declarable": False, "key": item["finding"],
                                "claim": f"The {a.get('by')} declined the declaration of `{item['finding']}` "
                                         f"({a.get('at')}): {str(a.get('note') or 'no note')[:300]}."})
                    continue
                v["owner_signoff"], v["owner_item"] = True, item
            return out + list(rule.gate_findings(run_dir, round_n, viols, non_declarable=V.is_non_declarable,
                                                 mode=mode, provisional_only=True) or [])
        except Exception as e:  # noqa: BLE001 -- fail closed below
            reason = f"the owner rule failed ({type(e).__name__}: {e})"
    elif rule is not None:
        reason = "under --signoff none nobody can accept a finding that bears on the decision"
    else:
        reason = "the owner rule (Phase 49 β, scripts/scope_signoff.py) is not available"
    for v in bearing:
        v["scope_declarable"] = False
        v.pop("owner_signoff", None)
        v.pop("owner_signed", None)
        v["claim"] = f"{v.get('claim', '')} Not declarable: {reason}."
    return []


def v2_gate(run_dir: str, round_n: int, max_rounds: int, post_write: bool, extra: list[dict],
            history: bool = True, critics=CRITICS, spec_violations=None, registry_violations=None,
            strict_done_when: bool = False, append_history: bool | None = None, signoff: str = "owner") -> dict:
    """Evaluate critic YAMLs, mechanical done_when tests, harness checks, and owner sign-off rules."""
    import done_when
    import validate_critique_yaml as V
    critiques, schema_errors, schema_errors_by_reviewer, refused = {}, [], {}, {}
    draft = any(os.path.exists(os.path.join(run_dir, f)) for f in ("report.md", "manuscript.md"))
    extra = list(extra)
    names = list(critics) + [r for r in V.REVIEWERS if r not in critics and r != "critique-presentation"
                             and os.path.exists(os.path.join(run_dir, V.FILENAMES[r]))]
    names += ["critique-presentation"] if draft else []
    for reviewer in names:
        path = os.path.join(run_dir, V.FILENAMES[reviewer])
        if not os.path.exists(path):
            if reviewer == "critique-presentation":
                extra.append({"kind": "critique_presentation_missing", "severity": "MEDIUM", "stage": "WRITE",
                              "target_stage": "WRITE", "scope_declarable": True,
                              "claim": "A draft exists but critique_presentation.yaml does not."})
                continue
            err = f"{V.FILENAMES[reviewer]} is missing"
            schema_errors.append(err)
            schema_errors_by_reviewer.setdefault(reviewer, []).append(err)
            continue
        try:
            critiques[reviewer] = V.validate_critique(path, reviewer, round_n)
        except V.SchemaError as e:
            errs = list(getattr(e, "errors", None) or [str(e)])
            schema_errors.extend(errs)
            schema_errors_by_reviewer.setdefault(reviewer, []).extend(errs)
            import yaml
            try:
                with open(path) as fh:
                    raw = yaml.safe_load(fh)
                if isinstance(raw, dict) and isinstance(raw.get("blockers"), list):
                    refused[reviewer] = raw
            except Exception:
                pass
    if strict_done_when:
        for reviewer, doc in list(critiques.items()) + list(refused.items()):
            for b in doc.get("blockers") or []:
                if not isinstance(b, dict) or b.get("resolved") is True \
                        or str(b.get("severity")) not in ("HIGH", "MEDIUM"):
                    continue
                try:
                    probs = done_when.problems(b["done_when"]) if b.get("done_when") is not None else ["missing"]
                except Exception as e:
                    probs = [f"done_when could not be checked ({type(e).__name__})"]
                if probs:
                    err = f"{V.FILENAMES[reviewer]}: blocker {b.get('id')} done_when: {'; '.join(probs)[:300]}"
                    schema_errors.append(err)
                    schema_errors_by_reviewer.setdefault(reviewer, []).append(err)
    for reviewer in V.OPTIONAL_REVIEWERS:
        doc, vs = V._load_optional_critique(run_dir, reviewer, round_n)
        if doc is not None:
            critiques[reviewer] = doc
        extra += vs
    if schema_errors:
        return {"schema_errors": schema_errors, "schema_errors_by_reviewer": schema_errors_by_reviewer, "action": None}
    d = V.decide(critiques, max_rounds, round_n)
    d["declared"] = V._declared_ids(run_dir)
    d["declared_prev"] = V._previous_declared(run_dir, round_n)
    d, dw = V._apply_done_when(d, critiques, run_dir, max_rounds, round_n, record=history)
    d = V._defer_write_findings(d, run_dir, max_rounds, round_n)
    if spec_violations is not None:
        d = V.incorporate_spec_violations(d, list(spec_violations), max_rounds, round_n)
    viols = extra + done_when.gate_findings(dw or {})
    viols += apply_owner_rule(run_dir, round_n, viols, mode=signoff)
    for v in viols:
        k = FL.gate_key(v)
        if k:
            v["stall_key"] = k
    d = V._incorporate_rigor_violations(d, viols, max_rounds, round_n)
    if registry_violations is not None:
        d = V.incorporate_registry_violations(d, list(registry_violations), max_rounds, round_n)
    d["post_write"] = bool(post_write)
    if history if append_history is None else append_history:
        V._append_validator_history(run_dir, round_n, d, post_write)
    return d


def open_findings(run_dir: str, decision: dict) -> list[dict]:
    """Every HIGH or MEDIUM finding still open after a gate, with `tested` indicating a mechanical done_when check."""
    import done_when as DW
    import validate_critique_yaml as V
    drafted = os.path.exists(os.path.join(run_dir, "manuscript.md")) or os.path.exists(os.path.join(run_dir, "report.md"))
    closed = set(map(str, decision.get("resolved_by_test") or []))
    out = []
    for reviewer, name in V.FILENAMES.items():
        p = os.path.join(run_dir, name)
        if not os.path.exists(p) or (reviewer == "critique-presentation" and not drafted):
            continue
        try:
            import yaml
            with open(p) as fh:
                doc = yaml.safe_load(fh)
            why = None if isinstance(doc, dict) and isinstance(doc.get("blockers"), list) else \
                f"top level is {type(doc).__name__}, or it has no `blockers` list"
        except Exception as e:  # noqa: BLE001
            why = f"{type(e).__name__}: {str(e)[:160]}"
        if why is not None:
            out.append({"id": f"critique_unreadable:{reviewer}", "source": "harness", "severity": "HIGH",
                        "tested": False, "claim": f"{name} could not be read as a critique ({why})",
                        "fix_requires": "the critic rewrites its YAML"})
            continue
        for b in doc.get("blockers") or []:
            if (not isinstance(b, dict) or b.get("resolved") or str(b.get("severity")) not in ("HIGH", "MEDIUM")
                    or str(b.get("id")) in closed):
                continue
            if not drafted and V.after_write_item(b):
                continue
            dw = b.get("done_when")
            out.append({"id": str(b.get("id")), "source": reviewer, "severity": b.get("severity"),
                        "kind": FL.CRITIC_KIND,
                        "tested": dw is not None and not DW.problems(dw), "claim": str(b.get("claim") or ""),
                        "fix_requires": _remedy(b)})
    critic_open = {o["id"] for o in out}
    seen = set()
    for v in decision.get("rigor_violations") or []:
        if not (isinstance(v, dict) and str(v.get("severity")) in ("HIGH", "MEDIUM")):
            continue
        if v.get("owner_resolution") and v.get("owner_signed") and not v.get("owner_signoff"):
            continue
        fid = FL.gate_id(v)
        if (str(v.get("key") or "") in critic_open and v.get("kind") != "done_when_regressed") or fid in seen:
            continue
        seen.add(fid)
        out.append({"id": fid, "source": "harness", "severity": v.get("severity"), "kind": str(v.get("kind") or ""),
                    "tested": not str(v.get("kind") or "").startswith("critique_done_when_missing"),
                    "claim": str(v.get("claim") or ""), "fix_requires": _remedy(v)})
    return out


def require_judged(d: dict, opened: list | None = None) -> dict:
    """Require all HIGH/MEDIUM done_when tests to be judged before ACCEPT; otherwise DECLARE_SCOPE (rule 3j)."""
    import done_when as DW
    summ = DW.summary(d.get("done_when_status") or {}, required=("HIGH", "MEDIUM"))
    d = dict(d, check_summary=summ)
    nj = summ["required_not_judged"]
    if d.get("action") != "ACCEPT" or not nj:
        return d
    if opened is not None:
        have = {o["id"] for o in opened}
        opened += [{"id": i, "source": "done-when", "severity": (d["done_when_status"].get(i) or {}).get("severity"),
                    "tested": False, "claim": str((d["done_when_status"].get(i) or {}).get("detail") or ""),
                    "fix_requires": None} for i in nj if i not in have]
    d.update(action="DECLARE_SCOPE", rule_matched="3j", declared_open=list(nj),
             rationale=(f"{len(nj)} required test(s) are not judged ({', '.join(nj)}): not judged is not a pass, "
                        "so they are declared in the write-up (declared_findings.yaml), not accepted."))
    return d


def owner_held(ledger: dict) -> set[str]:
    """Findings accepted by the owner (or provisionally by the lead)."""
    return {f.id for f in ledger.values() if f.status == "owner_accepted"}


def owner_accepted_now(run_dir: str, opened: list[dict], lead_ok: bool) -> set[str]:
    """Open findings whose current kind and remedy class carry a valid owner/lead accept."""
    answers = FL.owner_answers(run_dir, lead_ok)
    led = FL.load(run_dir) if lead_ok else {}
    out = set()
    for o in opened:
        fid = str(o.get("id"))
        a = FL.answer_for(answers, fid, kind=o.get("kind") or FL.CRITIC_KIND, fix_requires=o.get("fix_requires"),
                          claim=str(o.get("claim") or ""))
        if a and a.get("decision") == "accept":
            if a.get("by") == "lead":
                lf = led.get(fid)
                chk_o = dict(o, kind=o.get("kind") or getattr(lf, "kind", None) or FL.CRITIC_KIND,
                             source=o.get("source") or getattr(lf, "source", None),
                             severity=o.get("severity") if o.get("severity") is not None else getattr(lf, "severity", None))
                if not FL.lead_answer_allowed(chk_o, a) or (lf is not None and not FL.lead_answer_allowed(lf, a)):
                    continue
            out.add(fid)
    return out


def apply_medium_rule(decision: dict, opened: list[dict], rounds_remaining: int, budget_ok: bool,
                      owner_held=()) -> dict:
    """Send open tested findings (not held by owner) to FIX_LOOP (rule 4m) when rounds & budget remain, else 3m."""
    if decision.get("action") != "ACCEPT" or not opened:
        return decision
    d = dict(decision)
    held = set(map(str, owner_held or ()))
    tested = [f["id"] for f in opened if f["tested"] and str(f["id"]) not in held]
    if tested and rounds_remaining > 0 and budget_ok:
        d.update(action="PATCH_OR_RETHINK", rule_matched="4m", open_tested=tested,
                 rationale=(f"No HIGH finding is open, but {len(tested)} finding(s) with a test are "
                            f"({', '.join(tested)[:300]}) and {rounds_remaining} round(s) remain: they go to the "
                            "fix loop before ACCEPT."))
    else:
        why = "no rounds remain" if rounds_remaining <= 0 else ("the budget is used" if not budget_ok
                                                                 else "none has a test the fix loop can work to")
        mine = sorted(str(f["id"]) for f in opened if str(f["id"]) in held)
        if mine and rounds_remaining > 0 and budget_ok:
            why = f"the owner's answer, not a fix task, decides {', '.join(mine)[:200]}"
        d.update(action="DECLARE_SCOPE", rule_matched="3m", declared_open=[f["id"] for f in opened],
                 rationale=(f"{len(opened)} HIGH/MEDIUM finding(s) are still open and {why}: they are declared "
                            "in the write-up (declared_findings.yaml), not accepted."))
    return d


def held_entry_id(b: dict, decision: dict) -> str | None:
    """Ledger id of an unresolved HIGH that the owner may hold: a critic blocker id, or a declared
    decision test that did not run matched by kind and claim against rigor_violations."""
    if str(b.get("reviewer") or "").startswith("critique"):
        return str(b.get("id"))
    if b.get("kind") not in FL.NOT_RUN_KINDS:
        return None
    for v in decision.get("rigor_violations") or []:
        if isinstance(v, dict) and v.get("kind") == b.get("kind") \
                and f"{v.get('stage', '?')}/{v.get('kind')}: {v.get('claim', '')}" == b.get("claim"):
            return FL.gate_id(v)
    return None


def apply_held_rule(decision: dict, ledger: dict, owner_held=(), opened=None) -> dict:
    """A rule-2 gate holding only owner-accepted HIGHs (and no other tested open finding) declares them
    now (rule 3h) instead of entering an empty fix loop."""
    if decision.get("action") != "PATCH_OR_RETHINK" or str(decision.get("rule_matched")) != "2" \
            or decision.get("accept_blockers") or decision.get("structural_mismatch"):
        return decision
    keep = set(map(str, owner_held or ()))
    hi = [b for b in decision.get("unresolved_high") or [] if isinstance(b, dict)]
    hid = [held_entry_id(b, decision) for b in hi]
    if not hi or len(hi) != len(decision.get("unresolved_high") or []) or not all(
            i is not None and i in keep and i in ledger for i in hid):
        return decision
    if opened is None:
        return decision
    work = [str(o.get("id")) for o in opened if o.get("tested") and str(o.get("id")) not in keep]
    if work:
        return dict(decision, open_tested=work)
    ids = sorted(hid)
    return dict(decision, action="DECLARE_SCOPE", rule_matched="3h", declared_open=[o["id"] for o in opened],
                rationale=(f"{len(ids)} HIGH finding(s) are open ({', '.join(ids)[:200]}), each one the owner answered: "
                           "no fix task can work on them, so they are declared in the write-up "
                           "(declared_findings.yaml) now, not accepted."))


def apply_owner_declines(decision: dict, ledger: dict) -> dict:
    """Block ACCEPT / DECLARE_SCOPE when the owner declined a finding that has not passed its test."""
    fixed = set(map(str, decision.get("resolved_by_test") or []))
    declined = sorted(f.id for f in ledger.values() if f.status == "owner_declined" and f.id not in fixed)
    if not declined or decision.get("action") not in ("ACCEPT", "DECLARE_SCOPE"):
        return decision
    held = [{"kind": "scope_declaration_declined", "finding": fid, "severity": "HIGH", "scope_declarable": False,
             "claim": f"the owner declined to let the run go on without {fid}"} for fid in declined]
    return dict(decision, action="RUN_FAILED", rule_matched="owner_declined",
                unresolved_high=list(decision.get("unresolved_high") or []) + held,
                rationale=f"the owner declined {', '.join(declined)[:200]}: the run is not accepted or declared")


def scope_request_view(d: dict) -> dict:
    """Under rule 2o (rounds left) keep only owner_resolution items in the AWAIT_OWNER request."""
    if not isinstance(d, dict) or str(d.get("rule_matched")) != "2o":
        return d
    keep = lambda b: not (isinstance(b, dict) and b.get("owner_signoff")) or b.get("owner_resolution")  # noqa: E731
    return dict(d, unresolved_high=[b for b in d.get("unresolved_high") or [] if keep(b)],
                accept_blockers=[b for b in d.get("accept_blockers") or [] if keep(b)])


def _self_test() -> int:
    import tempfile
    import yaml

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    ok(route_gate({"action": "ACCEPT"}, False) == ("WRITE", None, "accepted")
       and route_gate({"action": "ACCEPT"}, True) == ("FINAL", "completed", None)
       and route_gate({"action": "DECLARE_SCOPE"}, True) == ("FINAL", "completed_scope_declared", None)
       and route_gate({"action": "RUN_FAILED"}, False) == ("WRITE", None, "not_accepted")
       and route_gate({"action": "RUN_FAILED"}, True) == ("FINAL", "run_failed", None)
       and route_gate({"schema_errors": ["bad"]}, True) == ("CRITIQUE", None, None),
       "VG1 route_gate pre/post-write and schema_errors edges")

    ok(rethink_target({"unresolved_high": [{"category": "STRUCTURAL", "target_stage": "PLAN"}]}) == "SPEC"
       and rethink_target({"unresolved_high": [{"target_stage": "ENGINE"}]},
                          built_engine=True, engine_repairs_left=True) == "ENGINE_BUILD"
       and rethink_target({"unresolved_high": [{"category": "METHODS"}]}) == "ENGINE_CHOICE",
       "VG2 rethink_target selects SPEC / ENGINE_BUILD / ENGINE_CHOICE")

    acc = {"action": "ACCEPT", "rule_matched": "4"}
    op = [{"id": "D-001", "tested": True}]
    d4m = apply_medium_rule(acc, op, 2, True)
    d3m_held = apply_medium_rule(acc, op, 2, True, owner_held={"D-001"})
    d3m_no_rounds = apply_medium_rule(acc, op, 0, True)
    ok(d4m["action"] == "PATCH_OR_RETHINK" and d4m["rule_matched"] == "4m"
       and d3m_held["action"] == "DECLARE_SCOPE" and d3m_held["rule_matched"] == "3m"
       and d3m_no_rounds["action"] == "DECLARE_SCOPE" and d3m_no_rounds["rule_matched"] == "3m",
       f"VG3 apply_medium_rule handles 4m, owner_held 3m, and exhausted-rounds 3m ({d4m['rule_matched']}, {d3m_held['rule_matched']})")

    rj = require_judged({"action": "ACCEPT", "done_when_status": {"D-1": {"judged": False, "severity": "HIGH", "detail": "missing"}}})
    ok(rj["action"] == "DECLARE_SCOPE" and rj["rule_matched"] == "3j",
       f"VG4 require_judged refuses ACCEPT on unjudged required test ({rj})")

    dec_led = {"D-2": FL.Finding(id="D-2", kind=FL.CRITIC_KIND, severity="HIGH", source="critique-domain",
                                 status="owner_declined")}
    dec_out = apply_owner_declines({"action": "ACCEPT"}, dec_led)
    ok(dec_out["action"] == "RUN_FAILED"
       and route_gate(dec_out, True) == ("FINAL", "completed_scope_declined", None),
       f"VG5 apply_owner_declines yields completed_scope_declined ({dec_out})")

    with tempfile.TemporaryDirectory() as td:
        for fn, rev, blk in (
            ("critique_domain.yaml", "critique-domain", []),
            ("critique_redteam.yaml", "critique-redteam", [{
                "id": "R-001", "severity": "HIGH", "category": "METHODS", "target_stage": "ANALYZE",
                "first_seen_round": 1, "claim": "c", "evidence": "e", "fix_requires": "fix",
                "resolved": False, "done_when": [{"file": "analysis/ok.txt", "exists": True}],
            }]),
        ):
            yaml.safe_dump({"reviewer": rev, "round": 1, "verdict": "REVISE" if blk else "PASS",
                            "structural_mismatch": {"detected": False}, "carried_forward": [],
                            "blockers": blk}, open(os.path.join(td, fn), "w"), sort_keys=False)
        res = v2_gate(td, 1, 3, False, [], history=False, strict_done_when=True)
        ops = open_findings(td, res)
        ok(res.get("action") == "PATCH_OR_RETHINK" and [o["id"] for o in ops] == ["R-001"] and ops[0]["tested"],
           f"VG6 v2_gate validates critiques, runs mechanical done_when, and reports open findings ({res.get('action')}, {ops})")

    sr = scope_request_view({
        "rule_matched": "2o",
        "unresolved_high": [{"kind": "decision_test_changes_plan", "owner_signoff": True, "owner_resolution": True},
                            {"kind": "policy_x", "owner_signoff": True}],
        "accept_blockers": [],
    })
    ok(len(sr["unresolved_high"]) == 1 and sr["unresolved_high"][0]["owner_resolution"],
       f"VG7 scope_request_view keeps only owner_resolution items under rule 2o ({sr})")

    d_held = {
        "action": "PATCH_OR_RETHINK",
        "rule_matched": "2",
        "unresolved_high": [
            {"reviewer": "critique-domain", "id": "D-10", "severity": "HIGH", "claim": "c1"},
            {"reviewer": "rigor-artifacts", "id": "RIG-001", "kind": "decision_test_not_run",
             "severity": "HIGH", "claim": "ANALYZE/decision_test_not_run: DT1 did not run"},
        ],
        "rigor_violations": [
            {"stage": "ANALYZE", "kind": "decision_test_not_run", "key": "DT1",
             "severity": "HIGH", "claim": "DT1 did not run"},
        ],
    }
    led_held = {
        "D-10": FL.Finding(id="D-10", kind=FL.CRITIC_KIND, severity="HIGH", source="critique-domain",
                           status="owner_accepted"),
        "decision_test_not_run:DT1": FL.Finding(id="decision_test_not_run:DT1", kind="decision_test_not_run",
                                                severity="HIGH", source="harness", status="owner_accepted"),
    }
    op_held = [
        {"id": "D-10", "tested": True},
        {"id": "decision_test_not_run:DT1", "tested": True},
    ]
    id_crit = held_entry_id(d_held["unresolved_high"][0], d_held)
    id_dt = held_entry_id(d_held["unresolved_high"][1], d_held)
    id_other = held_entry_id({"reviewer": "rigor-artifacts", "kind": "other_kind", "claim": "x"}, d_held)
    d_work = apply_held_rule(d_held, led_held, owner_held={"D-10", "decision_test_not_run:DT1"},
                             opened=op_held + [{"id": "M-1", "tested": True}])
    d_partial = apply_held_rule(d_held, led_held, owner_held={"D-10"}, opened=op_held)
    d_3h = apply_held_rule(d_held, led_held, owner_held={"D-10", "decision_test_not_run:DT1"}, opened=op_held)
    ok(id_crit == "D-10" and id_dt == "decision_test_not_run:DT1" and id_other is None
       and d_work["action"] == "PATCH_OR_RETHINK" and d_work.get("open_tested") == ["M-1"]
       and d_partial["action"] == "PATCH_OR_RETHINK"
       and d_3h["action"] == "DECLARE_SCOPE" and d_3h["rule_matched"] == "3h"
       and d_3h["declared_open"] == ["D-10", "decision_test_not_run:DT1"],
       f"VG8 held_entry_id and apply_held_rule cover critic HIGH + decision_test_not_run, open_tested refusal, and 3h ({id_crit}, {id_dt}, {d_3h.get('rule_matched')})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else 2)
