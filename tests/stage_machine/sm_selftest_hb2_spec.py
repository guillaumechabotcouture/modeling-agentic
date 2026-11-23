"""Self-tests of agents/stage_machine.py (hb2_spec), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import os
import analysis_backend as AB
from _stage_machine_under_test import Machine, _carry_feedback, load_state, save_state


def _self_test_hb2_spec(ok, td: str, spike, new_run) -> None:
    """HS1-HS7 (baseline hb1, 2026-11-22): a SPEC try that returns no draft (the 30-turn cap) replaced the
    validation feedback, so the next try never saw the error and repeated it (tb R1, run_failed at the try cap); the
    checks stopped at the first class that failed, so each try heard one class of problem (HS4-HS6); the drafter's
    prompt did not state the two rules that cost retries in measles and dengue (HS7)."""
    from v2_tasks import StubTasks

    class Scripted(StubTasks):
        """script["tries"]: per try, "bad" (an invalid draft), "good" (a valid one), "capped" (no draft written,
        the call fails) or "capped_good" (a valid draft written, then the call fails, as at the turn cap)."""
        def draft_spec(self, q, rd, fb):
            self.fbs = getattr(self, "fbs", []) + [list(fb or [])]
            kind = (self.s["tries"] + ["capped"] * 9)[len(self.fbs) - 1]
            if kind == "capped":
                return self._r("draft_spec", False, note="max turns")
            spec = dict(self.s["spec"])
            if kind in ("bad", "bad2"):
                spec["decision_units"] = dict(spec["decision_units"], csv="missing_units_table.csv")
            if kind in ("bad2", "noobj"):
                spec.pop("objective", None)
            if kind == "outside":
                spec["decision_units"] = dict(spec["decision_units"], csv="../outside_units.csv")
            if kind == "settings_capped":       # repairs only the settings, then hits the cap
                from v2_tasks import _yaml_dump
                _yaml_dump(self.s["settings"], os.path.join(rd, "analysis_settings.yaml"), rd)
                return self._r("draft_spec", False, note="max turns")
            settings = dict(self.s["settings"], bogus_key=1) if kind == "bad_settings" else self.s["settings"]
            r = StubTasks(dict(self.s, spec=spec, settings=settings)).draft_spec(q, rd, fb)
            return self._r("draft_spec", False, note="max turns") if kind == "capped_good" else r

    def go(name, tries, cls=None):
        d, T, st = new_run(name, {})
        T = Scripted(dict(T.s, tries=tries))
        m = (cls or Machine)(d, T, st, backend=spike, log=lambda *a: None, cross_engine_check=False)
        for _ in range(len(tries) + 1):
            if m.st.state != "SPEC" or m.st.paused or m.st.status:
                break
            m.step()
        return m, T

    m, T = go("hs1", ["bad", "capped", "bad"])
    first = [x for x in T.fbs[1] if "missing_units_table" in x]
    ok(len(T.fbs) == 3 and first and any("missing_units_table" in x for x in T.fbs[2])
       and any("max turns" in x for x in T.fbs[2]),
       f"HS1 a try that returns no draft keeps the earlier validation feedback, and adds its own note ({T.fbs})")
    ok(sum("missing_units_table" in x for x in T.fbs[2]) == len(first),
       f"HS1b feedback carried from an earlier try is not repeated ({T.fbs[2]})")
    m, T = go("hs2", ["capped_good"])
    ok(m.st.state != "SPEC" and m.st.tries.get("spec") == 1,
       f"HS2 a valid draft written before the call failed (the turn cap) is validated and used ({m.st.state}, "
       f"{m.st.tries.get('spec_feedback')})")
    # HS4: one try hears every check class (a missing cited file and a missing section), not only the first
    m, T = go("hs4", ["bad2", "good"])
    ok(len(T.fbs) >= 2 and any("missing_units_table" in x for x in T.fbs[1])
       and any("missing section objective" in x for x in T.fbs[1]),
       f"HS4 one SPEC try reports the cited-input and the pre-sign problems together ({T.fbs[1:2]})")

    # HS5: a check class that raises is reported by name, and the other classes' problems are kept
    class GeoBoom(Machine):
        def _geometry_problems(self, rd=None):
            raise RuntimeError("geometry boom")
    m, T = go("hs5", ["bad", "good"], GeoBoom)
    ok(len(T.fbs) >= 2 and any("missing_units_table" in x for x in T.fbs[1])
       and any("geometry boom" in x for x in T.fbs[1]),
       f"HS5 a check that raises is named and does not hide the others' problems ({T.fbs[1:2]})")

    # HS6: the evidence stage's spec problems (decision inputs with no dossier named) are heard at SPEC, in the same
    # try, not one stage later (tb R2: EVIDENCE sent the spec back for 7 missing decision inputs)
    from toolkit.spec import evidence_draft as EDR
    real = EDR.assess
    try:
        EDR.assess = lambda rd, raw=None: dict(real(rd, raw), spec_problems=["the options need 7 decision input(s)"])
        m, T = go("hs6", ["good", "good"])
    finally:
        EDR.assess = real
    ok(len(T.fbs) >= 2 and any("7 decision input" in x for x in T.fbs[1]) and m.st.tries.get("spec") >= 2,
       f"HS6 the evidence stage's spec problems come back from the SPEC try ({T.fbs[1:2]}, {m.st.state})")

    # HS7: the drafter's prompt states the two rules that cost SPEC retries; a fitted parameter is never moved under
    # calibration (Codex r1 P2-7: calibration accepts kind calibrated only)
    from v2_sdk_tasks import PROMPTS
    pr = PROMPTS["draft_spec"]
    ok("no digits" in pr and "decision test" in pr and "engine_parameters" in pr and "calibrated" in pr
       and "a fitted one" in pr,
       "HS7 the draft_spec prompt says: no digits in a decision test's text; no engine_parameters value for a "
       "calibrated or fitted parameter (a fitted one is left to the engine)")

    class Calls(Machine):
        """Records which reading checks ran."""
        def _geometry_problems(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["geometry"]
            return super()._geometry_problems(rd)

        def _evidence_assess(self, rd):
            self.ran = getattr(self, "ran", []) + ["evidence"]
            return super()._evidence_assess(rd)
    # HS8 (Codex r1 P1-1): a cited file outside the run is refused, and no check reads it
    AB.write_toy_units(os.path.join(td, "outside_units.csv"), n=12)
    m, T = go("hs8", ["outside"], Calls)
    fb8 = m.st.tries.get("spec_feedback") or []
    ok(any("outside the run directory" in x for x in fb8) and not getattr(m, "ran", []),
       f"HS8 a cited file outside the run is refused and no later check reads it ({getattr(m, 'ran', [])}, {fb8})")
    # HS9 (Codex r1 P1-2): the evidence assessment (it builds the engine) runs only on a spec the pre-sign checks pass
    m, T = go("hs9", ["noobj"], Calls)
    ok("evidence" not in getattr(m, "ran", []) and any("objective" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS9 a spec the pre-sign checks refuse is not given to the evidence assessment ({getattr(m, 'ran', [])})")

    # HS10 (Codex r1 P1-3): interrupted after the task's journal entry completed, a resume reaches the same decision
    class Crash(Exception):
        pass

    class CrashOnce(Machine):
        def _task(self, *a, **k):
            r = super()._task(*a, **k)
            if not getattr(self, "crashed", False):
                self.crashed = True
                save_state(self.run_dir, self.st)
                raise Crash()
            return r
    d10, T10, st10 = new_run("hs10", {})
    T10 = Scripted(dict(T10.s, tries=["capped_good"]))
    m = CrashOnce(d10, T10, st10, backend=spike, log=lambda *a: None, cross_engine_check=False)
    try:
        m.step()
    except Crash:
        pass
    m2 = Machine(d10, T10, load_state(d10), backend=spike, log=lambda *a: None, cross_engine_check=False)
    m2.step()
    ok(m2.st.state == "EVIDENCE" and len(T10.fbs) == 1,
       f"HS10 a resume after the completed task replays it and still checks the draft it wrote ({m2.st.state}, "
       f"{m2.st.tries.get('spec_feedback')})")
    # HS11 (Codex r1 P1-4): a capped try that repaired only the settings has its draft checked again
    m, T = go("hs11", ["bad_settings", "settings_capped"])
    ok(any("bogus_key" in x for x in T.fbs[1]) and m.st.state == "EVIDENCE",
       f"HS11 a capped try that changed only the settings is checked, and goes on ({m.st.state}, "
       f"{m.st.tries.get('spec_feedback')})")
    # HS14 (Codex r2 P1-2): when containment of the cited paths cannot be established, that is a problem, never a pass
    import pipeline as _P
    real_cp, n_cp = _P.cited_paths, []

    def flaky(*a, **k):
        n_cp.append(1)
        if len(n_cp) >= 2:
            raise OSError("listing failed")
        return real_cp(*a, **k)
    try:
        _P.cited_paths = flaky
        m, T = go("hs14", ["good"])
    finally:
        _P.cited_paths = real_cp
    fb14 = m.st.tries.get("spec_feedback") or []
    ok(m.st.state == "SPEC" and any("listing failed" in x for x in fb14),
       f"HS14 a citation listing that fails is a problem, not a pass ({m.st.state}, {fb14})")
    # HS15 (Codex r2 P1-5/P1-7/P2-8): a try with no completion output is checked on whatever draft files exist, with no
    # comparison of the files before and after (a resume, a byte-identical rewrite or an odd file never skips it)
    d15, T15, st15 = new_run("hs15", {})
    T15 = Scripted(dict(T15.s, tries=["good"]))
    StubTasks(T15.s).draft_spec("q", d15, None)        # a valid draft already on disk (an earlier, older journal)
    T15 = Scripted(dict(T15.s, tries=["capped"]))
    m = Machine(d15, T15, st15, backend=spike, log=lambda *a: None, cross_engine_check=False)
    m.step()
    ok(m.st.state == "EVIDENCE", f"HS15 a capped try leaves a valid draft on disk: it is checked and used, whatever "
                                 f"changed during the try ({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS16 (Codex r3 P1-1): the signed-configuration guard's problems go back to the drafter, whatever the backend
    real_sc = AB.signed_config_problems
    try:
        AB.signed_config_problems = lambda spec: ["engine.config: more components than the engine's limit"]
        m, T = go("hs16", ["good"])
    finally:
        AB.signed_config_problems = real_sc
    ok(m.st.state == "SPEC" and any("engine's limit" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS16 the configuration guard's refusal reaches the drafter ({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS17 (Codex r3 P1-2): feedback the pre-sign checks cannot verify (a rethink's reasons) is never cleared by a
    # capped try whose old draft passes those checks; the reasons are carried to the next try
    d17, T17, st17 = new_run("hs17", {})
    StubTasks(T17.s).draft_spec("q", d17, None)          # the earlier, valid draft
    st17.tries["spec_feedback"] = ["rethink after review round 1: the comparator answers another question"]
    save_state(d17, st17)
    T17 = Scripted(dict(T17.s, tries=["capped"]))
    m = Machine(d17, T17, st17, backend=spike, log=lambda *a: None, cross_engine_check=False)
    m.step()
    ok(m.st.state == "SPEC" and any("comparator" in x for x in m.st.tries.get("spec_feedback") or []),
       f"HS17 a capped try after a rethink keeps the rethink's reasons and does not pass on the old draft "
       f"({m.st.state}, {m.st.tries.get('spec_feedback')})")
    # HS12 (Codex r1 P2-6): the cap on the feedback never drops this try's own problems
    many = [f"problem {i}" for i in range(45)]
    got12 = _carry_feedback(many, ["old one"])
    ok(all(x in got12 for x in many), f"HS12 every current problem reaches the next try ({len(got12)})")

    from types import SimpleNamespace as NS
    from v2_sdk_tasks import SPEC_MAX_TURNS, SdkTasks
    got = []

    def q(prompt=None, options=None):
        async def gen():
            got.append(options.max_turns)
            yield NS(total_cost_usd=0.0, session_id="hs3", is_error=False)
        return gen()
    hd = os.path.join(td, "hs3")
    os.makedirs(hd)
    t = SdkTasks(hd, query_fn=q)
    t.set_allowance(1.0)
    t._session("draft_spec", "x", "y", ())
    t._session("pick_engine", "x", "y", ())
    ok(got == [SPEC_MAX_TURNS, 30] and SPEC_MAX_TURNS > 30,
       f"HS3 the spec draft has its own turn cap, above the 30 of the other tasks ({got})")
    # HS13 (Codex r1 P1-5): an explicit cap is honoured by every task, the spec draft included
    got.clear()
    t5 = SdkTasks(hd, max_turns=5, query_fn=q)
    t5.set_allowance(1.0)
    t5._session("draft_spec", "x", "y", ())
    ok(got == [5], f"HS13 a caller's explicit turn cap holds for the spec draft ({got})")
    got.clear()
    hw = os.path.join(hd, "wm")
    os.makedirs(hw)
    t5.world_model_seed(hw, {"question": "q"}, None, 1.0)
    ok(got == [5], f"HS13b the explicit turn cap holds for the world-model seed's own session ({got})")
