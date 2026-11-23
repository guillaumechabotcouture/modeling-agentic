"""The GATE state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import analysis_backend as AB
import findings_ledger as FL
import fix_loop as FX

from ..common import (
    ACCEPTED_STATUSES, CRITICS, DECISION_BEARING_KINDS, DECLARED_FILE, MAX_SCHEMA_REPAIRS, REPO, _put,
    _quarantine_report, _remedy, _write_json, save_state, short_label,
)
from ..edges import route_gate
from ..findings import report_acceptance_findings
from ..owner import OWNER_REQUEST, _answer_counts
from ..report import render_report
from . import handler


# ------------------------------------------------------------------ gates
def legacy_gate(run_dir: str, round_n: int, max_rounds: int, post_write: bool, history: bool = True,
                timeout_s: int = 1800) -> dict | None:
    """Today's STAGE 7 validator CLI, with the final gate's exact flags."""
    from agents.run_finish import gate_command
    cmd = gate_command(run_dir, max_rounds, round_n, post_write, history=history)
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"action": None, "error": f"{type(e).__name__}: {e}"}
    out = p.stdout.strip()
    try:
        d = json.loads(out[out.index("{"):])
    except ValueError:
        return {"action": None, "error": (p.stderr.strip().splitlines() or ["no JSON"])[-1][:400],
                "exit_code": p.returncode}
    d["exit_code"] = p.returncode
    return d


# Phase 49 β's sign-off lifecycle in v2 (autonomy 1F): a fingerprinted request (scope_signoff.record) written on the
# AWAIT_OWNER route, kept in run_state.json, and the answers taken through the harness (harness_record), honouring the
# run's signoff mode: the owner's under --signoff owner; under --signoff lead the lead's, provisionally (D2), so the run
# never waits; under --signoff none decision-bearing findings stay non-declarable.
OWNER_LIFECYCLE_WIRED = True


def _owner_rule():
    """Phase 49 β's owner rule (scripts/scope_signoff.py with the validator's AWAIT_OWNER path),
    or None when this checkout does not have both, or v2's sign-off lifecycle is not wired."""
    if not OWNER_LIFECYCLE_WIRED:
        return None
    import inspect
    import validate_critique_yaml as V
    try:
        import scope_signoff
    except ImportError:
        return None
    try:
        if "AWAIT_OWNER" not in inspect.getsource(V._recompute_action):
            return None
    except (OSError, TypeError):
        return None
    return scope_signoff


def _bearing_item(run_dir: str, v: dict) -> dict:
    """What the owner is asked about a decision-bearing finding: its claim and the analysis's record of the decision
    test, fingerprinted (an answer binds to both: a changed result is asked again). The record is the producer's: the
    toolkit's verified row (with its result file's hash) for a toolkit finding, the spike's analysis.json otherwise, so
    a stale file of the other backend never stands in for it (Codex r1 of #327)."""
    ev = None
    if v.get("source") == "toolkit/analysis/contract.py":      # lead-gaps α: analysis/decision_tests.yaml, verified
        try:
            from toolkit.analysis import contract as _CT
            ev = _CT.decision_test_record(run_dir, v.get("key"))
        except Exception:  # noqa: BLE001 -- no evidence: the item binds the claim alone, as before
            ev = None
    else:
        try:
            with open(os.path.join(run_dir, AB.ANALYSIS_JSON)) as fh:
                tests = (json.load(fh) or {}).get("decision_tests") or []
            ev = next((t for t in tests if isinstance(t, dict) and str(t.get("id")) == str(v.get("key"))), None)
        except (OSError, ValueError, AttributeError, TypeError):
            ev = None
    gid = FL.gate_id(v)
    fp = hashlib.sha256(json.dumps({"finding": gid, "claim": v.get("claim", ""), "evidence": ev}, sort_keys=True,
                                   default=str).encode()).hexdigest()[:16]
    return {"finding": gid, "kind": v.get("kind"), "reason": "a measured decision test changes the plan",
            "claim": v.get("claim", ""), "declarations": [], "declared": False, "fingerprint": fp}


def apply_owner_rule(run_dir: str, round_n: int, viols: list[dict], rule="auto", mode: str = "owner") -> list[dict]:
    """Decision-bearing findings (a measured decision test that changes the plan) go through
    Phase 49 β's owner rule before they can be declared: marked for the owner (an item the
    AWAIT_OWNER route requests) until a recorded answer counts (`mode`: the run's sign-off).
    Without the rule, or under --signoff none, they are not scope-declarable, so the run can
    neither declare them away nor finish around them."""
    import validate_critique_yaml as V
    rule = _owner_rule() if rule == "auto" else rule
    bearing = [v for v in viols if v.get("decision_bearing") or v.get("kind") in DECISION_BEARING_KINDS]
    if not bearing:
        if rule is None or mode == "none":
            return []
        # Codex r2 of #316 (P1): an owner's recorded decline holds the gate whatever else is present; nothing new is
        # put to the owner here (the policy kinds go to the owner only beside a decision-bearing finding, as before)
        marked = {id(v) for v in viols if v.get("owner_signoff")}
        try:
            out = [f for f in rule.gate_findings(run_dir, round_n, viols, non_declarable=V.is_non_declarable,
                                                 mode=mode, provisional_only=True) or []
                   if f.get("kind") == "scope_declaration_declined"]
        except Exception:  # noqa: BLE001 -- as before this rule: nothing marked, nothing declined
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
                item = _bearing_item(run_dir, v)
                a = answers.get(item["fingerprint"]) or {}
                if str(a.get("finding")) == item["finding"] and _answer_counts(a, mode):
                    if a.get("decision") == "accept":
                        v["owner_signed"] = {"by": a.get("by"), "at": a.get("at"), "fingerprint": item["fingerprint"],
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


def _earlier_round(path: str, round_n: int) -> int:
    """The round a critique file states when it is an earlier one than `round_n`; else `round_n` (a file that
    cannot be read, or states no earlier round, is validated against the gate's round as usual)."""
    import yaml
    try:
        with open(path) as fh:
            r = (yaml.safe_load(fh) or {}).get("round")
    except Exception:  # noqa: BLE001 -- validate_critique reports it
        return round_n
    return r if isinstance(r, int) and not isinstance(r, bool) and 1 <= r < round_n else round_n


def v2_gate(run_dir: str, round_n: int, max_rounds: int, post_write: bool, extra: list[dict],
            history: bool = True, critics=CRITICS, spec_violations=None, registry_violations=None,
            strict_done_when: bool = False, append_history: bool | None = None, signoff: str = "owner") -> dict:
    """Today's rule functions, in today's order, on the v2 findings: the critic YAMLs, the
    harness's checks (`extra`), and optionally spec-compliance and registry findings. `signoff`:
    the run's mode, for whose answers to owner items count (apply_owner_rule)."""
    import done_when
    import validate_critique_yaml as V
    critiques, schema_errors, refused = {}, [], {}
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
            schema_errors.append(f"{V.FILENAMES[reviewer]} is missing")
            continue
        rn = round_n
        if reviewer == "critique-presentation" and not post_write:
            # A pre-write gate with a draft follows a post-write modelling fix, one round on: PRESENTATION has not
            # run since, and it reviews the rewritten draft before the next post-write gate. The last review is
            # read as of the round it was written in (its findings stay open and listed), not refused for that
            # round, which cost a schema repair and a paid review of a draft about to be replaced.
            rn = _earlier_round(path, round_n)
        try:
            critiques[reviewer] = V.validate_critique(path, reviewer, rn)
        except V.SchemaError as e:
            # Every error in the file, so one repair fixes all of them (T74-T75). The refused file's
            # blockers still have their done_when tests checked below, for the same reason.
            schema_errors.extend(getattr(e, "errors", None) or [str(e)])
            import yaml
            try:
                with open(path) as fh:
                    raw = yaml.safe_load(fh)
                if isinstance(raw, dict) and isinstance(raw.get("blockers"), list):
                    refused[reviewer] = raw
            except Exception:                                # unreadable: its schema errors stand
                pass
    if strict_done_when:
        # v2blind1: critics wrote done_when as prose, done_when.py counted every test as missing,
        # and the open MEDIUMs were accepted. A HIGH or MEDIUM blocker's test is part of the schema
        # here: one that does not parse is repaired like any schema error (the T37 path).
        import done_when as DW
        for reviewer, doc in list(critiques.items()) + list(refused.items()):
            for b in doc.get("blockers") or []:
                if not isinstance(b, dict) or b.get("resolved") is True \
                        or str(b.get("severity")) not in ("HIGH", "MEDIUM"):
                    continue
                try:
                    probs = DW.problems(b["done_when"]) if b.get("done_when") is not None else ["missing"]
                except Exception as e:                       # a test that cannot be checked is a problem
                    probs = [f"done_when could not be checked ({type(e).__name__})"]
                if probs:
                    schema_errors.append(f"{V.FILENAMES[reviewer]}: blocker {b.get('id')} done_when: "
                                         f"{'; '.join(probs)[:300]}")
    for reviewer in V.OPTIONAL_REVIEWERS:
        doc, vs = V._load_optional_critique(run_dir, reviewer, round_n)
        if doc is not None:
            critiques[reviewer] = doc
        extra += vs
    if schema_errors:
        return {"schema_errors": schema_errors, "action": None}
    d = V.decide(critiques, max_rounds, round_n)
    d["declared"] = V._declared_ids(run_dir)
    d["declared_prev"] = V._previous_declared(run_dir, round_n)
    d = V._apply_brief_scope(d, critiques, run_dir, max_rounds, round_n)
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
    """Every HIGH or MEDIUM finding still open after a gate: the critics' unresolved blockers (not
    closed by their done_when test), and the harness checks' findings (their test is the check
    itself). `tested`: the finding has a test the fix loop can work to. A write-up finding waits
    for the draft."""
    import done_when as DW
    import open_problems as OP
    import validate_critique_yaml as V
    draft = os.path.exists(os.path.join(run_dir, "manuscript.md"))
    drafted = draft or os.path.exists(os.path.join(run_dir, "report.md"))     # the gate's own test for a draft
    closed = set(map(str, decision.get("resolved_by_test") or [])) | set(map(str, decision.get("outside_brief") or []))
    out = []
    for reviewer, name in V.FILENAMES.items():
        p = os.path.join(run_dir, name)
        if not os.path.exists(p) or (reviewer == "critique-presentation" and not drafted):
            continue
        try:
            import yaml
            with open(p) as fh:
                doc = yaml.safe_load(fh)
            # No defaults before the check (Codex on #264, P1): an empty or truncated file, or one without a
            # `blockers` list (a required key), is unreadable, never "no blockers".
            why = None if isinstance(doc, dict) and isinstance(doc.get("blockers"), list) else \
                f"top level is {type(doc).__name__}, or it has no `blockers` list"
        except Exception as e:  # noqa: BLE001
            why = f"{type(e).__name__}: {str(e)[:160]}"
        if why is not None:
            # The gate refuses to act on such a file (a schema error), so this is not reached from GATE; kept fail
            # closed for any other caller: an open HIGH, never skipped (external review 2026-11-17).
            out.append({"id": f"critique_unreadable:{reviewer}", "source": "harness", "severity": "HIGH",
                        "tested": False, "claim": f"{name} could not be read as a critique ({why})",
                        "fix_requires": "the critic rewrites its YAML"})
            continue
        for b in doc.get("blockers") or []:
            if (not isinstance(b, dict) or b.get("resolved") or str(b.get("severity")) not in ("HIGH", "MEDIUM")
                    or str(b.get("id")) in closed):
                continue
            if not drafted and OP.after_write_item(b):     # the gate's own rule (V._defer_write_findings)
                continue
            dw = b.get("done_when")
            out.append({"id": str(b.get("id")), "source": reviewer, "severity": b.get("severity"),
                        "tested": dw is not None and not DW.problems(dw), "claim": str(b.get("claim") or ""),
                        "fix_requires": _remedy(b)})
    critic_open = {o["id"] for o in out}
    seen = set()
    for v in decision.get("rigor_violations") or []:
        if not (isinstance(v, dict) and str(v.get("severity")) in ("HIGH", "MEDIUM")):
            continue
        fid = FL.gate_id(v)
        # Every gate finding counts (a resolved blocker whose test fails is done_when_unmet; a missing
        # presentation review is critique_presentation_missing); only one that restates an open
        # critic blocker (same id as its key) is a duplicate.
        # (a regression is its own HIGH finding beside the open one it is keyed by)
        if (str(v.get("key") or "") in critic_open and v.get("kind") != "done_when_regressed") or fid in seen:
            continue
        seen.add(fid)
        out.append({"id": fid, "source": "harness", "severity": v.get("severity"),
                    "tested": not str(v.get("kind") or "").startswith("critique_done_when_missing"),
                    "claim": str(v.get("claim") or ""), "fix_requires": _remedy(v)})
    return out
MAX_LANGUAGE_REPAIRS = 2


def require_judged(d: dict, opened: list | None = None) -> dict:
    """F6 S1-7: passed, failed and not-judged tests are counted apart (`check_summary`), and a required
    test (an open HIGH or MEDIUM finding's) that is not judged cannot yield ACCEPT: the finding is
    declared (rule 3j), like an open finding with no test the fix loop can work to (rule 3m)."""
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


def owner_accepted_now(run_dir: str, opened: list[dict], lead_ok: bool) -> set[str]:
    """The open findings whose current claim and remedy, as this gate reads them, carry the owner's (or, in a lead run,
    the lead's provisional) accept: the answer binds to that fingerprint, so a finding revised since it was answered is
    not among them (Codex r2 of #344, P1-2: the ledger may keep an older remedy)."""
    answers = FL.owner_answers(run_dir, lead_ok)
    out = set()
    for o in opened:
        fid = str(o.get("id"))
        a = answers.get(FL.answer_fingerprint(fid, str(o.get("claim") or ""), o.get("fix_requires")))
        if a and str(a.get("finding")) == fid and a.get("decision") == "accept":
            out.add(fid)
    return out


def apply_medium_rule(decision: dict, opened: list[dict], rounds_remaining: int, budget_ok: bool,
                      owner_held=()) -> dict:
    """v2: the gate never accepts an open HIGH or MEDIUM finding. Today's rules ACCEPT when no HIGH
    is open (rule 4); in v2 an open finding with a test goes to the fix loop while rounds and
    budget remain (rule 4m) and is declared, with the write-up saying so, when they do not (3m).
    A finding the owner holds (`owner_held`) never goes to the fix loop, which has no task for it: it is
    declared (harness baseline hb1, malaria R2: two critique rounds ran for an accepted finding).
    The decision's action and rationale say what the machine then does."""
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


def write_declared(run_dir: str, opened: list[dict], decision: dict) -> None:
    import yaml
    _put(run_dir, DECLARED_FILE, yaml.safe_dump({
        "rule": decision.get("rule_matched"), "rationale": decision.get("rationale"),
        "declared": [{"id": o["id"], "severity": o["severity"], "source": o["source"],
                      "label": short_label(o["claim"]), "claim": o["claim"],
                      "required_remedy": o.get("fix_requires"), "tested": bool(o.get("tested"))}
                     for o in opened]}, sort_keys=False, width=10**6))


def pending_amendments(ledger: dict, resolved=()) -> list[str]:
    """Findings, open or declared, whose remedy is an owner amendment the owner has not answered (Codex #258 P1-10);
    not one whose test now passes (`resolved`: the gate's resolved_by_test)."""
    done = set(map(str, resolved or ()))
    return sorted(f.id for f in ledger.values() if f.status in ("open", "declared") and f.id not in done
                  and FX.needs_owner_amendment(f))


def pending_conflicts(ledger: dict) -> list[str]:
    """W0-F: held-out calibration targets whose automatic test changes the plan (or did not run), not yet answered by
    the owner; asked together with the owner amendments, once (an answer closes them)."""
    from toolkit.analysis import conflicts as CF
    return sorted(f.id for f in ledger.values() if f.kind == CF.FINDING_KIND and f.status == "open")


def drop_declined(run_dir: str, ids) -> None:
    """A finding the owner declined is not a declared limitation: it leaves declared_findings.yaml (Codex #258)."""
    import yaml
    p = os.path.join(run_dir, DECLARED_FILE)
    try:
        doc = yaml.safe_load(open(p)) or {}
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(doc, dict) or not isinstance(doc.get("declared"), list):
        return
    keep = [x for x in doc["declared"] if not (isinstance(x, dict) and str(x.get("id")) in set(map(str, ids)))]
    if len(keep) != len(doc["declared"]):
        _put(run_dir, DECLARED_FILE, yaml.safe_dump(dict(doc, declared=keep), sort_keys=False, width=10**6))


def apply_owner_declines(decision: dict, ledger: dict) -> dict:
    """Codex review of #258 P1-11: a finding the owner declined (the run may not go on without it) holds the gate: a
    decision that would accept or declare is not accepted instead, with the decline as a non-declarable finding, so the
    run ends completed_scope_declined (route_gate) once the write-up is gated."""
    fixed = set(map(str, decision.get("resolved_by_test") or []))     # its test passes: the decline is moot
    declined = sorted(f.id for f in ledger.values() if f.status == "owner_declined" and f.id not in fixed)
    if not declined or decision.get("action") not in ("ACCEPT", "DECLARE_SCOPE"):
        return decision
    held = [{"kind": "scope_declaration_declined", "finding": fid, "severity": "HIGH", "scope_declarable": False,
             "claim": f"the owner declined to let the run go on without {fid}"} for fid in declined]
    return dict(decision, action="RUN_FAILED", rule_matched="owner_declined",
                unresolved_high=list(decision.get("unresolved_high") or []) + held,
                rationale=f"the owner declined {', '.join(declined)[:200]}: the run is not accepted or declared")


@handler("GATE")
class GateHandler:
    def h_GATE(self):
        pw = self.st.post_write
        waiting: list = []        # findings awaiting the owner's answer: no final disposition is published before it
        if self.st.gate_mode == "legacy":
            d = legacy_gate(self.run_dir, self.st.round, self.st.max_rounds, pw)
            # (owner amendments and declines are v2 rules: the legacy mode replays stored v1 gates and applies neither)
        else:
            sv = getattr(self.backend, "spec_violations", None)
            rv = getattr(self.backend, "registry_violations", None)
            d = v2_gate(self.run_dir, self.st.round, self.st.max_rounds, pw, self.harness_findings(),
                        spec_violations=sv(self.run_dir) if sv else None,
                        registry_violations=rv(self.run_dir) if rv else None,
                        strict_done_when=self.st.v2_rules and self.st.tries.get("schema", 0) < MAX_SCHEMA_REPAIRS,
                        append_history=False, signoff=self.st.signoff)
            opened = []
            if d.get("action") and self.st.v2_rules:
                opened = open_findings(self.run_dir, d)
                # synced with this decision, so a gate finding is read as this gate states it (Codex round 3 of #258),
                # and before the medium rule, which declares what the owner holds (hb1 cause H)
                led0 = FL.sync(self.run_dir, d, self.st.round, post_write=pw, lead_ok=self.st.signoff == "lead")[0]
                held_now = sorted(owner_accepted_now(self.run_dir, opened, self.st.signoff == "lead"))
                d = apply_medium_rule(d, opened, int(d.get("rounds_remaining") or 0), not self._out_of_budget(),
                                      owner_held=held_now)
                d["owner_held"] = held_now      # what this gate read as the owner's (FIX_LOOP's shortcut reads it here)
                d = require_judged(d, opened)
                # Codex re-review of #258 P1-11: the owner's declines hold this gate before anything is written from it
                d = apply_owner_declines(d, led0)
                waiting = sorted(set(pending_amendments(led0, d.get("resolved_by_test")) + pending_conflicts(led0)))
                if d.get("rule_matched") == "owner_declined":
                    drop_declined(self.run_dir, [h["finding"] for h in d["unresolved_high"]
                                                 if isinstance(h, dict) and h.get("kind") == "scope_declaration_declined"])
            if d.get("action"):
                import validate_critique_yaml as V
                V._append_validator_history(self.run_dir, self.st.round, d, pw)
            if self.st.v2_rules and d.get("action") == "DECLARE_SCOPE" and not waiting:
                # (not while a finding awaits the owner's answer: the gate re-runs after it; Codex r3 of #344, P1-2)
                # Every DECLARE_SCOPE (rule 3, 3a or 3m) lists what is still open in the write-up.
                write_declared(self.run_dir, opened, d)
            if self.st.v2_rules and pw and d.get("action") and self._has_draft():
                # The final status heads the report; a not-accepted one records any acceptance language. While a
                # finding awaits the owner's answer the status is pending (Codex r2 of #344, P1-1).
                fst = None if waiting else route_gate(d, pw)[1]
                render_report(self.run_dir, self.backend, status=fst or "pending")
                if fst is not None and fst not in ACCEPTED_STATUSES:
                    d["report_acceptance_language"] = self._language_check(fst)
        tag = f"gate_r{self.st.round}{'_post' if pw else ''}.json"
        _write_json(self.run_dir, tag, d or {})
        if pw and not waiting:
            _write_json(self.run_dir, "final_gate.json", d or {})
        elif pw:            # an earlier post-write gate's record is not the decision while the owner answers (Codex r3)
            from lib.safe_io import remove_nofollow
            remove_nofollow(self.run_dir, "final_gate.json")
        self.st.last_gate = {k: (d or {}).get(k) for k in ("action", "rule_matched", "rounds_remaining", "rationale")}
        self.st.last_gate.update(round=self.st.round, post_write=pw)
        if (d or {}).get("action"):
            FL.sync(self.run_dir, d, self.st.round, post_write=pw, lead_ok=self.st.signoff == "lead")
            if d.get("rule_matched") == "4m":           # these are why the loop runs: plan them
                led = FL.load(self.run_dir)
                for fid in d.get("open_tested") or []:
                    if fid in led:
                        led[fid].blocking = True
                FL.save(self.run_dir, led)
            if self.st.v2_rules:
                led = FL.load(self.run_dir)
                # Codex review of #258 P1-10: a finding whose remedy is an owner amendment goes to the owner on every
                # route, not only from the fix loop
                amend = sorted(set(pending_amendments(led, d.get("resolved_by_test")) + pending_conflicts(led)))
                if amend:
                    self._request_owner(led, amend)
                    return self._go("OWNER_DECISION", f"{len(amend)} finding(s) need an owner amendment "
                                                      f"({', '.join(amend)[:120]})")
        nxt, status, mode = route_gate(d, pw)
        if nxt == "OWNER_DECISION":
            self._request_scope(d)
        lang = (d or {}).get("report_acceptance_language") or {}
        if nxt == "FINAL" and lang.get("remaining"):
            # Not delivered with acceptance language in place (F6 S1-7): the writer repairs the draft, which then
            # goes through the whole post-write path again (presentation, every post-write check, this gate);
            # once the repairs are used, the report is withheld.
            if lang["repairs"] < MAX_LANGUAGE_REPAIRS:
                self.st.tries["language"] = lang["repairs"] + 1
                self.st.tries["language_feedback"] = lang["remaining"]
                self.st.write_mode = lang["mode"]
                # Quarantined until a repaired draft passes the whole post-write gate.
                _quarantine_report(self.run_dir)
                return self._go("WRITE", f"acceptance language in a {lang['mode']} report: the writer repairs it")
            _quarantine_report(self.run_dir)
            return self._go("FINAL", "the report kept acceptance language after its repairs: withheld",
                            "completed_no_report")
        if nxt == "CRITIQUE":                               # schema errors: re-run the critics
            n = self.st.tries.get("schema", 0) + 1
            self.st.tries["schema"] = n
            if n > MAX_SCHEMA_REPAIRS:
                return self._go("FINAL", "critique schema errors persist", "completed_gate_error")
            # Only the critics whose YAML failed are re-run, each told its own errors.
            import validate_critique_yaml as V
            by_file = {f: r for r, f in V.FILENAMES.items()}
            fb: dict = {}
            for e in d["schema_errors"]:
                f = str(e).split(":", 1)[0].strip().split()[0]
                fb.setdefault(by_file.get(f, "*"), []).append(str(e))
            self.st.tries["schema_feedback"] = fb
            return self._go("CRITIQUE", f"schema errors: {'; '.join(d['schema_errors'])[:200]}")
        self.st.tries.pop("schema", None)
        why = f"{(d or {}).get('action')} (rule {(d or {}).get('rule_matched')}){' post-write' if pw else ''}"
        if nxt in ("FIX_LOOP", "SPEC", "ENGINE_CHOICE"):
            self.st.round_pending = True
            self.st.post_write = False
        if nxt == "WRITE":
            self.st.write_mode = mode
        if nxt in ("SPEC", "ENGINE_CHOICE"):
            return self._rethink(nxt, why)
        return self._go(nxt, why, status)

    def _language_check(self, status: str) -> dict:
        """F6 S1-7: acceptance language in the whole manuscript of a report the final gate did not accept, with
        the repairs used so far (MAX_LANGUAGE_REPAIRS at most; each goes back through the post-write path)."""
        mode = "declared" if status == "completed_scope_declared" else "not_accepted"
        found = [f["claim"] for f in report_acceptance_findings(self.run_dir, mode)]
        return {"mode": mode, "repairs": int(self.st.tries.get("language", 0)), "remaining": found}

    def _request_scope(self, d: dict) -> None:
        """1F: the AWAIT_OWNER route's request (the decision-bearing items the gate marked), written with
        scope_signoff.record and its fingerprints kept in run_state.json, as _request_owner does. No item: none is
        kept, and OWNER_DECISION fails closed."""
        import scope_signoff as SS
        SS.record(self.run_dir, d, self.st.round, producer="agents/stage_machine.py GATE (decision-bearing findings)")
        try:
            req = json.load(open(os.path.join(self.run_dir, OWNER_REQUEST)))
        except (OSError, ValueError):
            req = {}
        fps = [i.get("fingerprint") for i in req.get("items") or [] if isinstance(i, dict)] \
            if isinstance(req, dict) and req.get("pending") else []
        if fps and all(fps):
            self.st.tries["owner_request"] = fps
        else:
            self.st.tries.pop("owner_request", None)
        save_state(self.run_dir, self.st)
