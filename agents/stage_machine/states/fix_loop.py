"""The FIX_LOOP state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import json
import os
import analysis_backend as AB
import findings_ledger as FL
import fix_loop as FX

from ..common import PROTECTED_BY_FIX, _now, save_state
from ..signoff import _signoff, signed_hash
from . import handler


def owner_held(ledger: dict) -> set[str]:
    """Findings the owner has answered (hb1 cause H): accepted by the owner (or provisionally by the lead). No fix task
    can work to their test. One still awaiting the owner's answer is not among them (Codex r1 of #344, P1-1): the gate
    sends it to OWNER_DECISION before anything declares it."""
    return {f.id for f in ledger.values() if f.status == "owner_accepted"}


# ------------------------------------------------------------------ helpers
class _Journaled:
    """The machine's tasks for the fix loop, each call through the journal (Machine._task) under a
    key that is the same on resume: plan calls by their order, fix calls by task id and attempt.
    A call with no allowance left returns a failed result without being made."""

    def __init__(self, m: "Machine", prefix: str):
        self.m, self.prefix, self.n_plan = m, prefix, 0

    def plan_fixes(self, packet):
        self.n_plan += 1
        return self._call(f"{self.prefix}:plan{self.n_plan}", "plan_fixes", packet)

    def fix(self, task, run_dir, evidence):
        return self._call(f"{self.prefix}:fix:{task['id']}:a{1 if evidence is None else 2}", "fix", task, run_dir, evidence)

    def _call(self, key, name, *args):
        from v2_tasks import TaskResult
        res = self.m._task(key, name, *args)
        return res if res is not None else TaskResult(False, None, 0.0, "no budget left for this task")


@handler("FIX_LOOP")
class FixLoopHandler:
    def _signed_ref(self, run_dir: str):
        """The signed spec's sha256: from the backend's own sign-off (B) or the machine's."""
        if getattr(self.backend, "sign", None):
            return self.backend.signed_sha(run_dir)
        return (_signoff(run_dir) or {}).get("spec_sha256")

    def fix_guard(self, run_dir: str) -> list[str]:
        """What a fix task may not do: change decision_spec.yaml (compared with its sign-off, or with
        its hash when the loop started), or leave a settings file the analysis would refuse."""
        probs = []
        ref = self._signed_ref(run_dir) if self.st.signoff != "none" else None
        ref = ref or self.st.journal.get(f"FIX_LOOP#{self._visit()}:spec_sha256", {}).get("output")
        if ref and signed_hash(run_dir) != ref:
            probs.append(f"{AB.SPEC_FILE} changed; it is signed (computational settings go in {AB.SETTINGS_FILE})")
        probs += [f"settings: {e}" for e in self.backend.validate(os.path.join(run_dir, AB.SPEC_FILE))]
        return probs

    def harness_ids(self, run_dir: str) -> set[str]:
        """The ledger ids the harness can re-check between gates: its own findings and, as h_GATE reads them, the
        backend's spec and registry checks. A fix task's finding absent from them passes its check; the gate's other
        findings (fix_loop.GATE_ONLY) are never among them and are left to the next gate."""
        vs = self.harness_findings()
        for name in ("spec_violations", "registry_violations"):
            fn = getattr(self.backend, name, None)
            vs += list(fn(run_dir) or []) if fn else []
        return {FL.gate_id(v) for v in vs if isinstance(v, dict)}

    def h_FIX_LOOP(self):
        v = self._visit()
        k = f"FIX_LOOP#{v}:spec_sha256"
        if k not in self.st.journal:                       # the reference a fix may not move
            self.st.journal[k] = {"task": "harness", "status": "completed", "output": signed_hash(self.run_dir)}
            save_state(self.run_dir, self.st)
        summ = FX.run_loop(self.run_dir, self.st.round, self.st.max_rounds, self._last_decision(),
                           _Journaled(self, f"FIX_LOOP#{v}:r{self.st.round}"),
                           rebuild=self.rebuild, harness_ids=self.harness_ids, headline=self.backend.headline,
                           budget=self._budget, log=self.log, guard=self.fix_guard, protect=PROTECTED_BY_FIX,
                           settings=getattr(self.backend, "settings_paths", AB.SETTINGS_PATHS))
        self.st.history.append({"at": _now(), "fix_loop": summ})
        if summ.get("owner_amendments"):
            # F6 run 3: their remedy is an owner amendment of the signed spec; the owner answers, no redraft runs.
            self._request_owner(FL.load(self.run_dir), summ["owner_amendments"])
            return self._go("OWNER_DECISION", f"{len(summ['owner_amendments'])} finding(s) need an owner amendment "
                                              f"({', '.join(summ['owner_amendments'])[:120]})")
        if summ.get("rethink"):
            return self._rethink(summ["rethink"], f"the planner chose a rethink ({summ['rethink']})", planned=True)
        if not summ["tasks"] and summ.get("write_up") and not summ.get("harness_gaps_blocking") and self._has_draft():
            # Every open finding is the write-up's: the writer revises the draft (with them) as the next round, and
            # the domain and red-team critics then re-review the revised draft at that round (hb1 cause G: they were
            # left on the round before, and the gate re-ran them through the schema repair).
            if self.st.round_pending:
                self.st.round += 1
                self.st.round_pending = False
            self.st.write_mode = self.st.write_mode or "not_accepted"
            self.st.tries["rereview"] = self.st.round
            return self._go("WRITE", f"every open finding is the write-up's ({', '.join(summ['write_up'])[:120]})")
        held_now = None if summ["tasks"] or self._drafted() or self._fixable_open() else self._shortcut_held()
        if held_now is not None:
            # hb1 cause H: pre-write, a loop with no task changed nothing, so another critique round can find nothing
            # new; every open finding the gate holds is the write-up's or the owner's. The draft is written, as the
            # gate's WRITE route would, in this round; the post-write gate then decides its status.
            self.st.round_pending = False
            # the post-write fix loop of this round writes its own plan: this one is kept apart (Codex r2 of #344, P1-3)
            if os.path.lexists(os.path.join(self.run_dir, f"fix_plan_r{self.st.round}.json")):
                from lib.safe_io import rename_nofollow        # moved with a source identity check (Codex r3, P1-4)
                rename_nofollow(self.run_dir, f"fix_plan_r{self.st.round}.json",
                                f"fix_plan_r{self.st.round}_prewrite.json")
            # the mode follows what this gate holds, not the whole ledger's history (Codex r2 of #344, P1-4)
            self.st.write_mode = "declared" if held_now else "accepted"
            return self._go("WRITE", "fix loop: no task; every open finding is the write-up's"
                                     + (f" or the owner's ({', '.join(held_now)[:120]})" if held_now else ""))
        return self._go("CRITIQUE", f"fix loop: {summ['rows']} task(s), {summ['passed']} passed their checks")

    def _drafted(self) -> bool:
        """The gate's own test for a draft (open_findings): a manuscript or a rendered report (Codex r1 of #344, P1-2)."""
        return self._has_draft() or os.path.exists(os.path.join(self.run_dir, "report.md"))

    def _shortcut_held(self) -> list[str] | None:
        """Codex r1 of #344 (P1-3, P2-4): the shortcut to WRITE only follows a medium-rule gate (4m) with no unresolved
        HIGH, whose tested findings are all the write-up's or the owner's answered ones; a gate that patches for any
        other reason (a HIGH, a declared finding it still holds) keeps its critique round. None: no shortcut; else the
        tested findings of that gate the owner holds."""
        try:        # this round's pre-write gate (the one that routed here; the round is raised at the next CRITIQUE)
            d = json.load(open(os.path.join(self.run_dir, f"gate_r{self.st.round}.json")))
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or d.get("rule_matched") != "4m" or d.get("unresolved_high"):
            return None
        # the owner-held findings as this gate read them (their current fingerprint carries the accept), not the
        # ledger's status, which may keep an older remedy (Codex r3 of #344, P1-1); the medium rule kept them out of
        # open_tested, so the mode comes from that record (P2-5)
        held = d.get("owner_held")
        if not isinstance(held, list):
            return None
        led = FL.load(self.run_dir)
        ids = [str(i) for i in d.get("open_tested") or []]
        if not all(i in led and FX.is_write_up(led[i]) for i in ids):
            return None
        return sorted(map(str, held))

    def _fixable_open(self) -> list[str]:
        """Open findings a fix task could still work to: the gate holds them (blocking), and they are neither the
        write-up's nor the owner's."""
        led = FL.load(self.run_dir)
        held = owner_held(led)
        return [f.id for f in FL.open_findings(led) if f.blocking and not FX.is_write_up(f) and f.id not in held]

    def _last_decision(self) -> dict | None:
        for name in (f"gate_r{self.st.round - (1 if self.st.round_pending else 0)}_post.json",
                     f"gate_r{self.st.round - (1 if self.st.round_pending else 0)}.json"):
            try:
                return json.load(open(os.path.join(self.run_dir, name)))
            except (OSError, ValueError):
                continue
        return None
