#!/usr/bin/env python3
"""v2 C: the bounded LLM tasks the stage machine calls, behind one interface.

Each task has typed inputs and a typed output (a file it writes, or a JSON value), and
reports its spend. `StubTasks` is deterministic and model-free: the unit tests, the golden
traces and the dry run use it. An SDK implementation fills the same methods with one
bounded agent session each (`agents/v2_sdk_tasks.py`).

  draft_spec(question, run_dir, feedback)  -> writes decision_spec.yaml
  sign_spec(run_dir)                        -> lead mode only: writes decision_spec.signoff.json
  pick_engine(spec, eligible)               -> {"engine": ref, "why": text}
  critique(run_dir, reviewer, round)        -> writes critique_<reviewer>.yaml (today's schema)
  plan_fixes(packet)                        -> {"tasks": [...], "deferred": [...]}
  fix(task, run_dir, evidence)              -> edits files; the harness measures what changed
  write(run_dir, mode)                      -> writes manuscript.md (mode: accepted | declared | not_accepted)
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Protocol

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PREFIX = {"critique-domain": "D", "critique-redteam": "R", "critique-presentation": "P", "critique-methods": "M"}
CRITIQUE_FILE = {"critique-domain": "critique_domain.yaml", "critique-redteam": "critique_redteam.yaml",
                 "critique-presentation": "critique_presentation.yaml", "critique-methods": "critique_methods.yaml"}


@dataclass
class TaskResult:
    ok: bool
    output: Any = None
    usd: float = 0.0
    note: str = ""
    session_id: str | None = None


class Tasks(Protocol):
    def acquire(self, question: str, run_dir: str, packet: dict) -> TaskResult: ...
    def draft_spec(self, question: str, run_dir: str, feedback: list[str] | None) -> TaskResult: ...
    def sign_spec(self, run_dir: str) -> TaskResult: ...
    def draft_evidence(self, run_dir: str, rel: str, required: dict, feedback: list) -> TaskResult: ...
    def pick_engine(self, spec: dict, eligible: list[dict], rethink: dict | None = None) -> TaskResult: ...
    def build_engine(self, run_dir: str, packet: dict, engine_id: str) -> TaskResult: ...
    def critique_engine(self, run_dir: str, round_n: int, feedback: list | None = None) -> TaskResult: ...
    def critique(self, run_dir: str, reviewer: str, round_n: int, feedback: list | None = None) -> TaskResult: ...
    def plan_fixes(self, packet: dict) -> TaskResult: ...
    def fix(self, task: dict, run_dir: str, evidence: dict | None) -> TaskResult: ...
    def write(self, run_dir: str, mode: str, feedback: list | None = None,
              findings: list | None = None) -> TaskResult: ...


def _sio():
    """scripts/lib/safe_io.py: run-dir writes with no link followed (follow-up to #129)."""
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from lib import safe_io
    return safe_io


def _yaml_dump(obj, path, root):
    """Publish YAML atomically with no link followed (a random temporary name, never a planted `.tmp`)."""
    import yaml
    _sio().publish(path, yaml.safe_dump(obj, sort_keys=False), root)


def set_key(path: str, dotted: str, value, root: str | None) -> None:
    """Set a dotted key in a YAML file (the stub fixer's one edit)."""
    import yaml
    doc = (yaml.safe_load(open(path)) or {}) if os.path.exists(path) else {}
    cur = doc
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    _yaml_dump(doc, path, root)


class StubTasks:
    """Scripted, deterministic tasks. `script`:

      spec:        dict, or callable(question, run_dir) -> dict (the decision spec to write)
      settings:    dict written to analysis_settings.yaml with the spec (computational settings)
      engine:      ref to pick (default: the first eligible)
      critics:     {round: {reviewer: [blocker, ...]}}  blockers need id, severity, target_stage, claim;
                   optional done_when, category. A blocker raised earlier is carried forward: resolved
                   once its done_when passes, else still present.
      structural:  {round: reviewer}  that critic reports a structural mismatch that round
      fixes:       {finding id: [(file, dotted key, value), ...] or callable(run_dir)}
      plan:        callable(packet) -> plan (default: the harness default plan)
      usd:         spend charged per task (default 0)
      crash:       {call name: n}  the call raises (an interrupted session) the first n times
    """

    def __init__(self, script: dict | None = None):
        self.s = script or {}
        self.calls: list[str] = []
        self.allowances: list = []
        self._crashes = dict(self.s.get("crash") or {})

    def set_allowance(self, usd, readback_usd=None):
        self.allowances.append(usd)

    def _r(self, name, ok=True, output=None, note=""):
        self.calls.append(name)
        if self._crashes.get(name, 0) > 0:
            self._crashes[name] -= 1
            raise RuntimeError(f"stub: {name} interrupted")
        return TaskResult(ok, output, float(self.s.get("usd", 0.0)), note)

    def acquire(self, question, run_dir, packet):
        """script['acquire']: callable(question, run_dir, packet) -> dict | TaskResult | False | None, or dict/False."""
        acq = self.s.get("acquire")
        res = acq(question, run_dir, packet) if callable(acq) else acq
        if isinstance(res, TaskResult):
            self.calls.append("acquire")
            return res
        if res is False:
            return self._r("acquire", False, note="stub acquire failed")
        if isinstance(res, dict):
            return self._r("acquire", True, output=res, note=f"kept {len(res.get('keep') or [])}")
        return self._r("acquire", True, output={"keep": [], "wanted_map": {}}, note="kept 0")

    def draft_spec(self, question, run_dir, feedback):
        spec = self.s.get("spec")
        if callable(spec):
            spec = spec(question, run_dir)
        if not isinstance(spec, dict):
            return self._r("draft_spec", False, note="no spec scripted")
        _yaml_dump(spec, os.path.join(run_dir, "decision_spec.yaml"), run_dir)
        if isinstance(self.s.get("settings"), dict):
            _yaml_dump(self.s["settings"], os.path.join(run_dir, "analysis_settings.yaml"), run_dir)
        return self._r("draft_spec", note=f"feedback={len(feedback or [])}")

    def sign_spec(self, run_dir):
        _sio().write_nofollow(run_dir, "decision_spec.signoff.json", json.dumps(
            {"by": "lead", "note": "stub: the spec matches the question"}).encode(), atomic=True)
        return self._r("sign_spec")

    def draft_evidence(self, run_dir, rel, required, feedback):
        """script["evidence"]: {input key: entry} merged over the scaffolded dossier, or callable(run_dir,
        feedback) -> that mapping; absent: the task does nothing (the dossier stays as scaffolded)."""
        ev = self.s.get("evidence")
        if callable(ev):
            ev = ev(run_dir, feedback)
        if isinstance(ev, dict):
            import yaml
            doc = yaml.safe_load(_sio().read_nofollow(run_dir, rel)[0]) or {}
            doc.setdefault("inputs", {}).update(ev)
            _sio().write_nofollow(run_dir, rel, yaml.safe_dump(doc, sort_keys=False).encode(), atomic=True)
        return self._r("draft_evidence", note=f"feedback={len(feedback or [])}")

    def pick_engine(self, spec, eligible, rethink=None):
        refs = [e["ref"] for e in eligible if e.get("eligible")]
        pick = self.s.get("engine") or (refs[0] if refs else None)
        if pick not in refs:
            return self._r("pick_engine", False, note=f"{pick} is not eligible")
        return self._r("pick_engine", output={"engine": pick, "why": "stub: first eligible engine"})

    def build_engine(self, run_dir, packet, engine_id):
        """script['build_engine']: callable(run_dir, packet, engine_id) -> {rel: bytes|str} | None, or that mapping."""
        be = self.s.get("build_engine")
        files = be(run_dir, packet, engine_id) if callable(be) else be
        if isinstance(files, dict):
            base = f"models/ext/engine_{engine_id}"
            for rel, content in files.items():
                raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
                _sio().write_nofollow(run_dir, f"{base}/{rel}", raw, atomic=True)
            return self._r(f"build_engine:{engine_id}", output={"ok": True, "notes": "stub build"})
        if files is False:
            return self._r(f"build_engine:{engine_id}", False, note="stub build failed")
        return self._r(f"build_engine:{engine_id}", output={"ok": True, "notes": "stub build (no files written)"})

    def critique_engine(self, run_dir, round_n, feedback=None):
        """script['critique_engine']: callable(run_dir, round_n, feedback) -> dict | str | None, or dict/str."""
        ce = self.s.get("critique_engine")
        doc = ce(run_dir, round_n, feedback) if callable(ce) else ce
        if isinstance(doc, str):
            _sio().write_nofollow(run_dir, "critique_engine.yaml", doc.encode("utf-8"), atomic=True)
        elif isinstance(doc, dict):
            _yaml_dump(doc, os.path.join(run_dir, "critique_engine.yaml"), run_dir)
        elif doc is None:
            _yaml_dump({"reviewer": "critique-engine", "round": round_n, "verdict": "PASS", "blockers": []},
                       os.path.join(run_dir, "critique_engine.yaml"), run_dir)
        return self._r(f"critique_engine:{round_n}")

    def critique(self, run_dir, reviewer, round_n, feedback=None):
        import done_when as DW
        import yaml
        path = os.path.join(run_dir, CRITIQUE_FILE[reviewer])
        prev = []
        if round_n > 1 and os.path.exists(path):
            prev = (yaml.safe_load(open(path)) or {}).get("blockers") or []
        blockers, carried = [], []
        for b in prev:
            if b.get("resolved"):
                continue
            dw = b.get("done_when")
            fixed = bool(dw) and DW.mechanical(dw) and bool(DW.evaluate(dw, run_dir)[0])
            nb = dict(b, resolved=fixed)
            if fixed:
                nb.update(resolved_in_round=round_n, resolved_evidence="done_when passes")
            blockers.append(nb)
            carried.append({"id": b["id"], "prior_round": round_n - 1, "still_present": not fixed, "notes": "stub"})
        for b in ((self.s.get("critics") or {}).get(round_n) or {}).get(reviewer) or []:
            blockers.append({"id": b["id"], "severity": b.get("severity", "HIGH"), "category": b.get("category", "METHODS"),
                             "target_stage": b.get("target_stage", "ANALYZE"), "first_seen_round": round_n,
                             "claim": b["claim"], "evidence": b.get("evidence", "stub"),
                             "fix_requires": b.get("fix_requires", "stub fix"), "resolved": False,
                             **({"done_when": b["done_when"]} if b.get("done_when") else {})})
        sm = {"detected": False}
        if (self.s.get("structural") or {}).get(round_n) == reviewer:
            sm = {"detected": True, "description": "stub: the engine cannot represent the question",
                  "evidence_files": ["decision_spec.yaml"], "fix_requires": "RETHINK"}
        doc = {"reviewer": reviewer, "round": round_n,
               "verdict": "REVISE" if any(not b["resolved"] for b in blockers) else "PASS",
               "structural_mismatch": sm, "carried_forward": carried, "blockers": blockers}
        _yaml_dump(doc, path, run_dir)
        return self._r(f"critique:{reviewer}:{round_n}")

    def plan_fixes(self, packet):
        import fix_loop
        plan = self.s.get("plan")
        return self._r("plan_fixes", output=plan(packet) if callable(plan) else fix_loop.default_plan(packet))

    def fix(self, task, run_dir, evidence):
        done = []
        for fid in task["findings"]:
            edit = (self.s.get("fixes") or {}).get(fid)
            if callable(edit):
                edit(run_dir)
                done.append(fid)
            elif edit:
                for rel, key, value in edit:
                    set_key(os.path.join(run_dir, rel), key, value, run_dir)
                done.append(fid)
        return self._r(f"fix:{task['id']}", note=f"edited for {done}" if done else "no scripted edit")

    def write(self, run_dir, mode, findings=None):
        self.write_findings = list(findings or [])       # the write-up findings the machine passed (F6 run 3)
        A = {}
        try:
            A = json.load(open(os.path.join(run_dir, "analysis", "analysis.json")))
        except (OSError, ValueError):
            pass
        head = {"accepted": "", "declared": "Status: scope declared (see Limitations).\n\n",
                "not_accepted": "Status: not accepted by the final gate.\n\n"}[mode]
        ref = (" It averts [CLAIM:headline.point] cases."
               if os.path.exists(os.path.join(run_dir, "models", "claims_ledger.yaml")) else "")
        text = f"# Report\n\n{head}The plan is the harness's optimisation; the harness renders every number.{ref}\n"
        if self.s.get("manuscript"):
            text = f"# Report\n\n{head}{self.s['manuscript']}"
        del A
        _sio().write_nofollow(run_dir, "manuscript.md", text.encode("utf-8"), atomic=True)
        return self._r(f"write:{mode}")
