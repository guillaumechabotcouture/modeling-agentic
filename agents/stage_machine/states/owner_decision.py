"""The OWNER_DECISION state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import json
import os

from ..common import DECISION_BEARING_KINDS, _now, _withhold_report, save_state
from ..owner import OWNER_REQUEST, _answer_counts
from . import handler


def _plan_changing(item: dict) -> bool:
    """An owner item whose evidence shows the plan changes: a decision-bearing kind, a declaration whose decision
    test changes the decision, or a W0-F evidence conflict whose verdict changes the plan (or whose test did not run:
    W0-F counts that as changing it; Codex r4 of #316)."""
    conf = item.get("conflict")
    if item.get("kind") == "evidence_conflict_declared" and not isinstance(conf, dict):
        return True     # a request saved before the verdict was carried: it holds only plan-changing or untested ones
    return item.get("kind") in DECISION_BEARING_KINDS or (isinstance(conf, dict) and conf.get("changes_plan") is True) \
        or any((d.get("test") or {}).get("decision_changes") is True
               for d in item.get("declarations") or [] if isinstance(d, dict))


@handler("OWNER_DECISION")
class OwnerDecisionHandler:
    def h_OWNER_DECISION(self):
        """Released by the answers to every item of the pending request: the owner's (scripts/scope_signoff.py sign
        --by owner, from the owner's shell) or, under --signoff lead (D2), the lead's provisional ones, which the harness
        writes here so the run never waits (scope_signoff.provisional_answers); the machine then records them
        (harness_record), so the ledger counts them (Codex review of #258 P1-8). No recorded request: it fails closed.
        Under --signoff owner a lead's answer does not count: it waits."""
        import scope_signoff as SS
        # Codex r4 of #316: the request is read, answered, recorded and closed under the sign-off lock, so an owner's
        # sign that waited on it finds the request closed (and refuses) instead of reporting an answer never applied
        with SS.locked(self.run_dir):
            return self._owner_decision(SS)

    def _owner_decision(self, SS):
        try:
            req = json.load(open(os.path.join(self.run_dir, OWNER_REQUEST)))
        except (OSError, ValueError):
            req = None
        want = list(self.st.tries.get("owner_request") or [])
        if not want:
            # a pause with no request this machine recorded (an older run, or a route that records none): it cannot be
            # answered here, so it fails with how to go on, never waits (lead decision on Codex round 3 of #258); a
            # report an earlier gate rendered is not delivered under it (Codex round 4)
            _withhold_report(self.run_dir)
            return self._go("FINAL", "OWNER_DECISION has no recorded owner request: relaunch with reuse (a new run dir "
                                     "from this one's spec and stored rows)", "run_failed")
        answers, bad = SS.load_answers(self.run_dir)
        lead = self.st.signoff == "lead"

        def done(fp):
            a = answers.get(fp)
            return isinstance(a, dict) and _answer_counts(a, self.st.signoff) and a.get("decision") in SS.DECISIONS
        if lead and bad:        # never overwritten (Codex r1 of #316): an owner answer in it would be lost
            _withhold_report(self.run_dir)
            return self._go("FINAL", f"{SS.SIGNOFF} cannot be read, so the lead cannot answer: restore it and resume",
                            "run_failed")
        if lead and not all(done(fp) for fp in want):
            items = {i.get("fingerprint"): i for i in (req or {}).get("items") or [] if isinstance(i, dict)} \
                if isinstance(req, dict) else {}
            if any(fp not in items for fp in want):
                _withhold_report(self.run_dir)
                return self._go("FINAL", "the owner request cannot be read, so the lead cannot answer it: relaunch with "
                                         "reuse", "run_failed")
            todo = [items[fp] for fp in want if not done(fp)]
            SS.provisional_answers(self.run_dir, todo, self.st.round,
                                   plan_changing={i["fingerprint"]: _plan_changing(i) for i in todo})
            self.st.history.append({"at": _now(), "provisional": [i.get("finding") for i in todo], "by": "lead",
                                    "state": "OWNER_DECISION"})
            answers, bad = SS.load_answers(self.run_dir)
        answered = bool(want) and not bad and all(done(fp) for fp in want)
        if answered:
            SS.harness_record(self.run_dir)
            self.st.tries.pop("owner_request", None)
            who = "the lead answered provisionally" if lead and any(answers[fp].get("by") == "lead" for fp in want) \
                else "the owner answered"
            nxt = self._go("GATE", f"{who} the scope declarations")    # persisted before the request closes
            if isinstance(req, dict):
                from lib.safe_io import write_nofollow
                write_nofollow(self.run_dir, OWNER_REQUEST, json.dumps(dict(req, pending=False, answered_at=_now()),
                                                                       indent=1).encode(), atomic=True)
            return nxt
        self.st.paused = "awaiting_scope_signoff"
        save_state(self.run_dir, self.st)
        return "OWNER_DECISION"
