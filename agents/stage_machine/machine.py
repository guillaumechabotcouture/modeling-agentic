"""The Machine: the loop (step, run), transitions, budget, journal and the methods states share.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import json
import os
import analysis_backend as AB
import findings_ledger as FL

from .common import (
    ACCEPTED_STATUSES, AWAITING_BUDGET, AWAITING_SPEC_AMENDMENT, AwaitingBudget, MIN_TASK_ALLOWANCE_USD,
    READBACK_CAP_USD, REPO, RunState, SIGNOFF_SHEET, _now, _put, _quarantine_report, _remedy, _withhold_report,
    _write_json, load_state, save_state,
)
from .edges import EDGES, LLM_STATES, POST_CHOICE_STATES
from .amend import _amend_lock
from .signoff import _signoff, evidence_gate, signed_hash, spec_signed_once, spec_signoff_findings
from .findings import (
    deliverable_problems, harness_owned, owned_path, report_acceptance_findings, report_claim_findings,
)
from .owner import request_owner_amendments
from .report import render_report
from .failures import _RESUME_WHY, resumable_failure
from .states import HANDLERS


# ------------------------------------------------------------------ the machine
class Machine(*HANDLERS.values()):
    def __init__(self, run_dir: str, tasks, state: RunState | None = None, backend=None, log=print,
                 cross_engine_check: bool = True):
        self.run_dir = os.path.abspath(run_dir)
        self.tasks = tasks
        self.backend = backend or AB.backend()
        self.log = log
        self.cross = cross_engine_check
        st = load_state(self.run_dir)
        if (st or state) is not None and (st or state).signoff == "lead":
            # a lead run's state is read again and written back under its amendment lock, so a state read before
            # another process's hand-off is never written over it (Codex r3 on #342, P1-9)
            with _amend_lock(self.run_dir):
                self._load(load_state(self.run_dir), state)
        else:
            self._load(st, state)
        self.seed_fn = getattr(tasks, "world_model_seed", None)    # §14: the world model's seed task, when the tasks have one
        self.wm_repo = REPO                                         # where the beliefs are anchored (wm-trials)

    def _load(self, st: RunState | None, state: RunState | None) -> None:
        self.st = st or state
        if self.st is None:
            raise ValueError("no run_state.json and no initial state")
        self._settle_interrupted()
        save_state(self.run_dir, self.st)

    def persist(self) -> None:
        # Named persist, not save, only because scripts/write_audit.py WA3 reads any `<obj>.save()` call as PIL's
        # image save (a false positive): this writes run_state.json through save_state -> lib.safe_io.write_nofollow,
        # atomic. The heuristic's own-module exemption cannot see a method defined in another module (the caller is
        # agents/world_model_stage.py), so it was not changed.
        save_state(self.run_dir, self.st)

    def _settle_interrupted(self) -> None:
        """Every journal entry left `started` by a previous process was interrupted: charge its
        allowance once (the task cap when it had none) and mark it, before anything else runs."""
        for j in self.st.journal.values():
            if isinstance(j, dict) and j.get("status") == "started":
                self.st.spent_usd += self._interrupted_charge(j)
                j["interrupted_charged"] = j.get("interrupted_charged", 0) + 1
                j["status"] = "interrupted"

    def _interrupted_charge(self, j: dict) -> float:
        """What an interrupted task is charged: its allowance (the task cap when it had none) and, for a write-up
        task, the read-back allowance it was given (0 in an entry written before that was recorded)."""
        a, rb = j.get("allowance_usd"), j.get("readback_usd")
        return float(a if a is not None else self.st.task_cap_usd) + float(rb if isinstance(rb, (int, float)) else 0.0)

    # -------------------------------------------------- plumbing
    def _go(self, to: str, why: str = "", status: str | None = None, extra: dict | None = None,
            before_save=None) -> str:
        frm = self.st.state
        if to not in EDGES[frm]:
            raise RuntimeError(f"transition {frm} -> {to} is not in the state table")
        self.st.history.append({"at": _now(), "from": frm, "to": to, "why": why, "round": self.st.round,
                                "spent_usd": round(self.st.spent_usd, 4), **(extra or {})})
        self.st.state = to
        if to == "FINAL":
            self.st.status = self.st.forced_status or status or "completed_gate_error"
            self._withhold_if_acceptance_language()
            self._finish()
        if before_save is not None:
            before_save()
        save_state(self.run_dir, self.st)
        self.log(f"[machine] {frm} -> {to}" + (f" ({why})" if why else ""))
        return to

    def _charge(self, res) -> None:
        self.st.spent_usd += float(getattr(res, "usd", 0.0) or 0.0)

    def _allowance(self, writeup: bool = False) -> float | None:
        """What the next paid task may spend: the task cap, at most what the run has left less the
        write-up reserve (the write-up itself may use the reserve). None: no run cap."""
        cap = self.st.budget_usd
        if not cap:
            return None
        from agents.run_budget import writeup_reserve
        left = cap - self.st.spent_usd - (0.0 if writeup else writeup_reserve(cap))
        return round(min(self.st.task_cap_usd, left), 4)

    def _visit(self) -> int:
        return sum(1 for h in self.st.history if h.get("to") == self.st.state)

    def _task(self, key: str, name: str, *args, writeup: bool = False, **kwargs):
        """One paid task through the journal. Returns its TaskResult, or None when the run has no
        allowance left for it (the caller takes the budget path). A completed task is not run
        again (its result is replayed, already charged); a task found started but not completed
        was interrupted: it is charged its allowance once, then run again."""
        from v2_tasks import TaskResult
        j = self.st.journal.get(key)
        if j and j.get("status") == "completed":
            return TaskResult(bool(j.get("ok")), j.get("output"), 0.0, j.get("note", ""), j.get("session_id"))
        if j and j.get("status") == "started":
            self.st.spent_usd += self._interrupted_charge(j)
            j["interrupted_charged"] = j.get("interrupted_charged", 0) + 1
            j["status"] = "interrupted"
            save_state(self.run_dir, self.st)
        allowance = self._allowance(writeup)
        if allowance is not None and allowance <= 0:
            return None
        if allowance is not None and allowance < MIN_TASK_ALLOWANCE_USD:
            self.st.paused, self.st.status = AWAITING_BUDGET, None
            self.st.history.append({"at": _now(), "paused": AWAITING_BUDGET, "state": self.st.state, "task": name,
                                    "key": key, "allowance_usd": allowance, "floor_usd": MIN_TASK_ALLOWANCE_USD,
                                    "spent_usd": round(self.st.spent_usd, 4), "cap_usd": self.st.budget_usd})
            save_state(self.run_dir, self.st)
            self.log(f"[machine] {name} not started: its allowance ${allowance:.2f} is below the ${MIN_TASK_ALLOWANCE_USD:.2f} "
                     f"floor (spent ${self.st.spent_usd:.2f} of ${self.st.budget_usd}). Paused for the owner; resume with "
                     f"python3 main.py --v2 --resume {self.run_dir} --max-budget-usd <higher cap>")
            raise AwaitingBudget(name)
        a = allowance if allowance is not None else self.st.task_cap_usd
        b = self._budget(writeup)
        # A crash read-back is a paid call too: a small cap, from what is left after this allowance (for a
        # write-up task, left of the reserve it may use).
        rb = READBACK_CAP_USD if b is None else max(0.0, min(READBACK_CAP_USD, round(b["left"] - a, 4)))
        # A write-up task spends up to the cap itself, so its read-back allowance is part of what an interrupted
        # one is charged (_interrupted_charge): the cap holds on resume.
        j = dict(j or {}, task=name, status="started", started=_now(), allowance_usd=allowance, writeup=bool(writeup),
                 readback_usd=rb if writeup else 0.0)
        self.st.journal[key] = j
        save_state(self.run_dir, self.st)
        setter = getattr(self.tasks, "set_allowance", None)
        if setter:
            setter(a, readback_usd=rb)
        res = getattr(self.tasks, name)(*args, **kwargs)
        self._charge(res)
        out = res.output
        try:
            json.dumps(out)
        except (TypeError, ValueError):
            out = None
        j.update(status="completed", completed=_now(), ok=bool(res.ok), output=out, usd=float(res.usd or 0.0),
                 note=(res.note or "")[:300], session_id=getattr(res, "session_id", None))
        save_state(self.run_dir, self.st)
        return res

    def _budget_out(self, s: str):
        """The budget path from state `s` (no allowance for the next paid task)."""
        self.st.forced_status = "budget_exhausted"
        if s in ("CRITIQUE", "FIX_LOOP") and not self._has_draft():
            self.st.write_mode = "not_accepted"
            return self._go("WRITE", "budget used: the harness writes the report")
        if s in ("CRITIQUE", "FIX_LOOP", "PRESENTATION", "WRITE"):
            return self._go("FINAL", "budget used")
        return self._go("FINAL", "budget used before the model was built", "budget_exhausted")

    def _budget(self, writeup: bool = False) -> dict | None:
        """What is left under the cap: less the write-up reserve, except for the write-up's own tasks (WRITE and
        the PRESENTATION review of its draft), which the reserve is held for."""
        cap = self.st.budget_usd
        if not cap:
            return None
        from agents.run_budget import writeup_reserve
        left = cap - (0.0 if writeup else writeup_reserve(cap)) - self.st.spent_usd
        return {"spent": round(self.st.spent_usd, 4), "cap": cap, "left": round(left, 4)}

    def _out_of_budget(self) -> bool:
        b = self._budget()
        return b is not None and b["left"] <= 0

    def _has_draft(self) -> bool:
        return os.path.exists(os.path.join(self.run_dir, "manuscript.md"))

    def _spec(self, rd: str | None = None) -> dict:
        import yaml
        return yaml.safe_load(open(os.path.join(rd or self.run_dir, AB.SPEC_FILE))) or {}

    def harness_findings(self) -> list[dict]:
        out = self.backend.gate_findings(self.run_dir) + spec_signoff_findings(self.run_dir, self.st.signoff, self.backend)
        out += [{"kind": "deliverable_path_invalid", "severity": "HIGH", "stage": "PLAN", "target_stage": "PLAN",
                 "key": "deliverable", "scope_declarable": False, "claim": p} for p in deliverable_problems(self.run_dir)]
        if self.st.v2_rules:
            out += report_claim_findings(self.run_dir)
        if self.st.derived_inputs or os.path.exists(os.path.join(self.run_dir, "data", "derivations.yaml")):
            import pipeline as P
            try:
                spec = self._spec()
            except Exception:  # noqa: BLE001 -- the spec's own findings say why; the data refs need it
                spec = None
            out += P.derivation_findings(self.run_dir, spec, required=self.st.derived_inputs)
        out += self._geometry_findings()
        return out

    def _geometry_findings(self) -> list[dict]:
        """The standing input geometry check (toolkit/spec/geometry.py, docs/design/v2-input-geometry.md): the units'
        coordinates and areas against the declared boundaries, re-run on the current files at every gate."""
        if not os.path.exists(os.path.join(self.run_dir, AB.SPEC_FILE)):
            return []
        from toolkit.spec import geometry as GEO
        return GEO.findings(GEO.run(self.run_dir))

    def _signed_by(self, rd: str | None = None):
        """Who signed the current spec (of `rd`, the run dir by default), or None when it is unsigned or changed since."""
        rd = rd or self.run_dir
        if getattr(self.backend, "sign", None):
            if self.backend.signoff_status(rd) != "signed":
                return None
            try:
                from toolkit.spec import signoff
                rec = signoff._spec_record(rd) or {}
            except Exception:  # noqa: BLE001
                rec = {}
            return rec.get("signed_by") or rec.get("by") or "owner"
        rec = _signoff(rd)
        return rec.get("by") if rec and rec.get("spec_sha256") == signed_hash(rd) else None

    def _rethink(self, to: str, why: str, planned: bool = False):
        """A RETHINK back-edge to SPEC or ENGINE_CHOICE, which is told why. From the gate (a structural mismatch): its
        rationale, each critic's description of the mismatch and its STRUCTURAL findings. From the fix loop
        (`planned`): this round's rethink briefs for that state and the findings they name. Either way, the open
        findings that state owns, each with its remedy. An unsigned spec is redrafted with them as its feedback and a
        fresh count of tries (a signed one is the owner's to amend: h_SPEC); the engine pick is told the engine the
        run left. Nothing read here can stop the back-edge: a file that is not what it should be adds no reason."""
        try:
            out = self._rethink_reasons(to, planned)
        except Exception as e:  # noqa: BLE001 -- advisory text: the back-edge is taken without it
            out = [f"the reasons could not be read ({type(e).__name__})"]
        if to == "SPEC" and not (self.st.tries.get("spec_amendment") or spec_signed_once(self.run_dir)):
            self.st.tries["spec"] = 0
            if out:
                self.st.tries["spec_feedback"] = [f"rethink after review round {self.st.round}: {x}" for x in out]
        elif to == "ENGINE_CHOICE":
            self.st.tries["rethink_engine"] = {"engine": self.st.engine, "round": self.st.round, "why": out}
        return self._go(to, why)

    def _rethink_reasons(self, to: str, planned: bool) -> list[str]:
        import yaml
        import validate_critique_yaml as V
        g, out, named, seen = self.st.last_gate or {}, [], set(), set()
        if not planned:
            if g.get("action") == "RETHINK_STRUCTURAL" and g.get("rationale"):
                out.append(f"gate: {g['rationale']}")
            for reviewer in V.FILENAMES:           # every reviewer the gate may have read
                try:
                    doc = yaml.safe_load(open(os.path.join(self.run_dir, V.FILENAMES[reviewer])))
                    sm = doc.get("structural_mismatch") or {}
                    if sm.get("detected"):
                        out.append(f"{reviewer}: {sm.get('description')}")
                    for b in doc.get("blockers") or []:       # its own text: not every reviewer's is in the ledger
                        if isinstance(b, dict) and b.get("category") == "STRUCTURAL" and not b.get("resolved"):
                            out.append(f"{b.get('id')}: {b.get('claim')}" + (f" Required: {_remedy(b)}" if _remedy(b) else ""))
                            seen.add(str(b.get("id")))
                except Exception:  # noqa: BLE001 -- the gate already judged the file; here it only adds reasons
                    continue
        else:
            try:
                plan = json.load(open(os.path.join(self.run_dir, f"fix_plan_r{self.st.round}.json")))["plan"]["tasks"]
            except Exception:  # noqa: BLE001
                plan = []
            for t in plan if isinstance(plan, list) else []:
                if isinstance(t, dict) and t.get("action") == "rethink" and t.get("owner_state") == to:
                    out.append(f"fix plan: {t.get('brief')}")
                    named |= {str(x) for x in (t.get("findings") if isinstance(t.get("findings"), list) else [])}
        try:
            led = FL.open_findings(FL.load(self.run_dir))
        except Exception as e:  # noqa: BLE001 -- the reasons read so far are kept
            led, out = [], out + [f"the findings ledger could not be read ({type(e).__name__})"]
        out += [f"{f.id}: {f.claim}" + (f" Required: {f.fix_requires}" if f.fix_requires else "")
                for f in led if (f.id in named or f.owner_state == to) and f.id not in seen]
        return [" ".join(str(x).split())[:600] for x in out][:20]

    def rebuild(self, run_dir: str) -> dict:
        """The S1 pipeline, then the report again when a draft exists (a write-up fix edits
        manuscript.md; its check must read the re-rendered report)."""
        import pipeline as P
        rep = P.run(run_dir)
        if os.path.exists(os.path.join(run_dir, "manuscript.md")):
            render_report(run_dir, self.backend)
        return rep

    # -------------------------------------------------- one step
    def step(self) -> str:
        s = self.st.state
        if s == "FINAL":
            return s
        if self.st.paused == AWAITING_SPEC_AMENDMENT:      # a state saved before the flag existed keeps its pause
            self.st.tries["spec_amendment"] = True
        self.st.paused = None
        if s == "SPEC" and (self.st.tries.get("spec_amendment") or spec_signed_once(self.run_dir)):
            return self.h_SPEC()                 # the amendment pause costs no model call (Codex review of #261 P1-3)
        # PRESENTATION is not checked here: its one task is a write-up task, and _task replays a completed review
        # before it looks at the budget (h_PRESENTATION takes the budget path when there is no allowance).
        if s in LLM_STATES and s != "PRESENTATION" and self._out_of_budget():
            return self._budget_out(s)
        try:
            return getattr(self, "h_" + s)()
        except AwaitingBudget:            # _task recorded the pause; the journal makes the handler safe to re-run
            return s

    def run(self, max_steps: int = 200) -> str:
        if self.st.state in POST_CHOICE_STATES:     # F6 (#218): a resume past ENGINE_CHOICE re-checks, before compute
            miss = evidence_gate(self.run_dir, signoff=self.st.signoff)
            if miss:
                _withhold_report(self.run_dir)       # never deliverable under run_failed (narrow Codex review of #272)
                self._go("FINAL", "the run's preconditions do not hold on resume: " + "; ".join(miss[:5]), "run_failed")
                save_state(self.run_dir, self.st)
                return self.st.status or self.st.state
        for _ in range(max_steps):
            before = (self.st.state, len(self.st.history))
            self.step()
            if self.st.state == "FINAL" or self.st.paused:
                break
            if (self.st.state, len(self.st.history)) == before:
                raise RuntimeError(f"state {self.st.state} made no progress")
        if self.st.paused and self.st.paused != AWAITING_BUDGET and self.st.state != "FINAL":   # _task: budget's own
            # the harness benchmark protocol (I1, owner-wait) reads every pause from history, not only the budget one
            self.st.history.append({"at": _now(), "paused": self.st.paused, "state": self.st.state,
                                    "round": self.st.round, "spent_usd": round(self.st.spent_usd, 4)})
        save_state(self.run_dir, self.st)
        return self.st.status or (f"paused:{self.st.paused}" if self.st.paused else self.st.state)

    def _evidence_assess(self, rd: str) -> tuple[dict, dict]:
        """The spec and the evidence stage's assessment of it (toolkit/spec/evidence_draft.py assess), with a dossier
        path that the harness, the gate or a task writes added to its spec problems. SPEC and EVIDENCE share it."""
        from toolkit.spec import evidence_draft as EDR
        raw = EDR._spec(rd)
        a = EDR.assess(rd, raw)
        rel = a.get("rel")
        if rel and not a.get("draftable") and owned_path(rd, os.path.normpath(rel).replace(os.sep, "/"), tuple(
                p for p in harness_owned() if p not in ("data/*", EDR.ED.DOSSIER_FILE))):
            a["spec_problems"] = a["spec_problems"] + [   # Codex r2 P1-5: a file the harness or a task writes
                f"evidence.dossier {rel!r} is a file the harness, the gate or a task writes: name "
                f"{EDR.ED.DOSSIER_FILE}"]
        return raw, a

    def _write_sheet(self, rd: str | None = None) -> None:
        """The sign-off sheet, with the source hashes it was made from, published with no link followed (review
        round 3: signoff.write_sheet)."""
        from toolkit.spec import signoff
        signoff.write_sheet(rd or self.run_dir, getattr(self.backend, "candidates", None))

    def _signed_before(self) -> bool:
        """The run's spec was signed: a sign-off record binds it (current or not), or the history records a signature
        (a record lost since does not make the run a draft again: Codex r1 P1-3)."""
        return spec_signed_once(self.run_dir) or any(
            h.get("from") == "SIGN" and str(h.get("why", "")).startswith("spec signed by") for h in self.st.history
        ) or os.path.lexists(os.path.join(self.run_dir, "decision_brief.signed", AB.SPEC_FILE))   # a frozen copy (r2)

    def _review_conditions(self, mode: str, defer: bool = True) -> bool:
        """Owner decision 2026-11-21 (W0-G): before signing, the spec critique's undeclared HIGH findings become rows of
        review_conditions.yaml; under --signoff lead each open row is deferred to the owner, provisionally (D2), and
        the run proceeds. The sheet is regenerated so it shows them. False (the run waits for the owner) when the
        import cannot run: a signature never goes ahead without it."""
        from toolkit.spec import review_conditions as RC
        try:
            n = RC.import_critique(self.run_dir)
            d = RC.lead_defer(self.run_dir) if mode == "lead" and defer else 0
            sp_ = os.path.join(self.run_dir, SIGNOFF_SHEET)
            if os.path.exists(sp_):     # whenever the sheet does not show the file as it now is (Codex narrow round)
                from toolkit.spec import signoff as SO
                src = SO.sheet_sources(open(sp_, encoding="utf-8", errors="replace").read()) or {}
                if src.get(RC.SHEET_KEY, "none") != RC.sha(self.run_dir):
                    self._write_sheet()
        except Exception as e:  # noqa: BLE001 -- fail closed: the owner is asked
            self.log(f"[machine] review conditions could not be prepared for signing: {type(e).__name__}: {e}")
            return False
        if n or d:
            self.log(f"[machine] review conditions: {n} spec-critique finding(s) added"
                     + (f", {d} deferred to the owner by the lead (D2)" if d else ""))
        return True

    def _write_pipeline(self) -> str | None:
        """models/pipeline.yaml from the backend's steps (their limits from the compute budget as it is now), or why
        it could not be written (fail closed: an unreadable budget stops the run before any compute)."""
        import yaml
        try:
            steps = self.backend.pipeline_steps(os.path.join(self.run_dir, AB.SPEC_FILE), self.run_dir)
        except Exception as e:  # noqa: BLE001
            return f"the pipeline could not be written: {type(e).__name__}: {e}"
        _put(self.run_dir, "models/pipeline.yaml", yaml.safe_dump({"version": 1, "steps": steps}, sort_keys=False))
        return None

    def reopen_interrupted_analysis(self) -> str | None:
        """--resume of a run that ended run_failed because its analysis step timed out, was killed, or lost engine
        rows to the machine (resumable_failure): back to ANALYZE with the pipeline rewritten, so the step's limit follows the compute
        budget as it is now and the engine batch keeps the rows it had finished (scripts/compute.py reuses them).
        Recorded in the history as FINAL -> ANALYZE; outside the state table on purpose, since no handler may take
        it. Returns the cause, or None when the run is not resumable (it stays as it is)."""
        cause = resumable_failure(self.run_dir, self.st)
        if cause is None:
            return None
        err = self._write_pipeline()
        if err:
            self.log(f"[machine] not resumed: {err}")
            return None
        self.st.history.append({"at": _now(), "from": "FINAL", "to": "ANALYZE", "round": self.st.round,
                                "why": f"resumed: {_RESUME_WHY[cause]}",
                                "resumed_from": self.st.status, "spent_usd": round(self.st.spent_usd, 4)})
        self.st.state, self.st.status = "ANALYZE", None
        save_state(self.run_dir, self.st)
        self.log(f"[machine] FINAL -> ANALYZE (resumed after an analysis {cause})")
        return cause

    def _world_model(self, fn: str) -> str:
        """§14, fail soft: a world-model step never raises into the machine. `refresh` (before CRITIQUE and WRITE)
        scores again when the fix loop has rebuilt the analysis since; it is recorded in the history."""
        if not self.st.world_model_stages:
            return ""
        try:
            import world_model_stage as WMS
            why = getattr(WMS, fn)(self) or ""
        except Exception as e:  # noqa: BLE001
            why = f"world model: {fn} failed: {type(e).__name__}: {e}"[:300]
        if why and fn == "refresh":
            self.st.history.append({"at": _now(), "world_model": why})
        return why

    def _request_owner(self, led: dict, ids: list[str]) -> None:
        """The request, and the fingerprints it asks, kept in run_state.json: the answers must be to them (Codex
        re-review of #258 P1-8), whatever request file is there later."""
        doc = request_owner_amendments(self.run_dir, self.st.round, led, ids)
        self.st.tries["owner_request"] = [i["fingerprint"] for i in doc["items"]]
        save_state(self.run_dir, self.st)

    def _rereview_due(self) -> bool:
        """hb1 cause G: a draft revised for write-up findings as a new round is re-reviewed by the domain and red-team
        critics at that round before the gate (set by FIX_LOOP; cleared by the re-review)."""
        return self.st.tries.get("rereview") == self.st.round and self.st.post_write

    def _withhold_if_acceptance_language(self) -> None:
        """F6 S1-7: whatever path ends the run, a report it did not accept is never left deliverable with
        acceptance language in it: report.md is moved to report_withheld.md."""
        rp = os.path.join(self.run_dir, "report.md")
        if (not self.st.v2_rules or self.st.status in ACCEPTED_STATUSES or not os.path.exists(rp)
                or not report_acceptance_findings(self.run_dir, "not_accepted")):
            return
        _quarantine_report(self.run_dir)
        if str(self.st.status).startswith("completed"):
            self.st.status = "completed_no_report"

    def _finish(self) -> None:
        meta_p = os.path.join(self.run_dir, "metadata.json")
        try:
            meta = json.load(open(meta_p))
        except (OSError, ValueError):
            meta = {}
        meta.update(status=self.st.status, pipeline="v2", engine=self.st.engine, rounds=self.st.round,
                    max_rounds=self.st.max_rounds, model_spend_usd=round(self.st.spent_usd, 4), finished=_now())
        _write_json(self.run_dir, "metadata.json", meta)
