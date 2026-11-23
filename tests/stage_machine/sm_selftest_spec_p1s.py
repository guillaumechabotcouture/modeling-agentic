"""Self-tests of agents/stage_machine.py (spec_p1s), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import os
import analysis_backend as AB
from _stage_machine_under_test import Machine, save_state


def _self_test_spec_p1s(ok, td: str, spike, new_run) -> None:
    """SP1-SP4 (Codex round 4 on #348, on code merged through #342): a capped try with no draft must not stop later
    capped tries' drafts from being checked; a cited input that is not a regular file (a FIFO) is never opened by a later
    check; the spec and its settings are refused as links or hard links before any check reads them; an explicit zero
    turn cap is refused (it would remove the session's limit)."""
    from v2_tasks import StubTasks, TaskResult

    class Capped(StubTasks):
        """Per try: "none" (no draft, capped) or "good" (a valid draft written, then capped)."""
        def draft_spec(self, q, rd, fb):
            self.n_ = getattr(self, "n_", 0) + 1
            if self.s["tries"][self.n_ - 1] == "good":
                StubTasks(self.s).draft_spec(q, rd, fb)
            self.calls.append("draft_spec")
            return TaskResult(False, None, 0.0, "max turns")
    d1, T1, st1 = new_run("sp1", {})
    T1 = Capped(dict(T1.s, tries=["none", "good", "good"]))
    m = Machine(d1, T1, st1, backend=spike, log=lambda *a: None, cross_engine_check=False)
    for _ in range(3):
        if m.st.state != "SPEC" or m.st.status:
            break
        m.step()
    ok(m.st.state == "EVIDENCE", f"SP1 a first capped try with no draft does not stop the next capped try's valid draft "
                                 f"from being checked ({m.st.state}, {m.st.status}, {m.st.tries.get('spec_feedback')})")

    class Spy(Machine):
        def _geometry_problems(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["geometry"]
            return []

        def _evidence_assess(self, rd):
            self.ran = getattr(self, "ran", []) + ["evidence"]
            return super()._evidence_assess(rd)
    # SP3: a cited FIFO inside the run is refused and no later check opens it (it would block with no writer)
    d3, T3, st3 = new_run("sp3", {})
    m3 = Spy(d3, T3, st3, backend=spike, log=lambda *a: None, cross_engine_check=False)
    spec3 = dict(T3.s["spec"], decision_units=dict(T3.s["spec"]["decision_units"], csv="units_fifo.csv"))
    StubTasks(dict(T3.s, spec=spec3)).draft_spec("q", d3, None)
    os.mkfifo(os.path.join(d3, "units_fifo.csv"))
    got3 = m3._spec_checks(d3)
    ok(any("units_fifo.csv" in x and "regular file" in x for x in got3) and not getattr(m3, "ran", []),
       f"SP3 a cited FIFO is refused and never opened by a later check ({getattr(m3, 'ran', [])}, {got3})")
    # SP4: the spec and analysis_settings.yaml are refused as a symlink or a hard link, before any check reads them
    marker = os.path.join(td, "sp4_fixture.yaml")
    open(marker, "w").write("fixture_marker: 1\n")
    res4 = {}
    for name, rel, how in (("settings link", "analysis_settings.yaml", os.symlink),
                           ("settings hard link", "analysis_settings.yaml", os.link),
                           ("spec hard link", AB.SPEC_FILE, os.link)):
        d4, T4, st4 = new_run("sp4_" + name.replace(" ", "_"), {})
        T4.draft_spec("q", d4, None)
        os.remove(os.path.join(d4, rel))
        how(marker, os.path.join(d4, rel))
        m4 = Spy(d4, T4, st4, backend=spike, log=lambda *a: None, cross_engine_check=False)
        got4 = m4._spec_checks(d4)
        res4[name] = bool(any(rel in x and "link" in x for x in got4) and not any("fixture_marker" in x for x in got4)
                          and not getattr(m4, "ran", []))
    ok(all(res4.values()), f"SP4 a linked spec or settings file is refused before any check reads it ({res4})")
    # SP5 (Codex r1 P1-1): through h_SPEC, a hard-linked spec is refused before the conflicts default rewrites it
    d5, T5, st5 = new_run("sp5", {})

    class Linking(StubTasks):
        def draft_spec(self, q, rd, fb):
            r = StubTasks(self.s).draft_spec(q, rd, fb)
            os.link(os.path.join(rd, AB.SPEC_FILE), os.path.join(td, "sp5_other_name.yaml"))
            return r
    m5 = Machine(d5, Linking(T5.s), st5, backend=spike, log=lambda *a: None, cross_engine_check=False)
    m5.step()
    ok(m5.st.state == "SPEC" and any("hard links" in x for x in m5.st.tries.get("spec_feedback") or []),
       f"SP5 a hard-linked spec is refused at the try's end, before the conflicts default is applied ({m5.st.state}, "
       f"{m5.st.tries.get('spec_feedback')})")

    # SP6 (Codex r1 P1-2/P1-3): a FIFO anywhere in the run, whatever path names it (a staged input a derivation reads,
    # a name with a leading space), is refused before derivations or any check runs
    class SpyAll(Spy):
        def _derive(self, rd=None):
            self.ran = getattr(self, "ran", []) + ["derive"]
            return []
    res6 = {}
    for name, rel, cite in (("staged input", "data/inputs/t.csv", None), ("spaced name", " units.csv", " units.csv")):
        d6, T6, st6 = new_run("sp6_" + name.replace(" ", "_"), {})
        spec6 = T6.s["spec"] if cite is None else dict(
            T6.s["spec"], decision_units=dict(T6.s["spec"]["decision_units"], csv=cite))
        StubTasks(dict(T6.s, spec=spec6)).draft_spec("q", d6, None)
        os.makedirs(os.path.dirname(os.path.join(d6, rel)), exist_ok=True)
        os.mkfifo(os.path.join(d6, rel))
        m6 = SpyAll(d6, T6, st6, backend=spike, log=lambda *a: None, cross_engine_check=False)
        m6.st.derived_inputs = True
        got6 = m6._spec_checks(d6)
        res6[name] = bool(any("regular file" in x for x in got6) and not getattr(m6, "ran", []))
    ok(all(res6.values()), f"SP6 a FIFO in the run is refused before derivations or any reading check ({res6})")
    # SP7 (Codex r1 P1-5): a run saved before this fix (feedback "draft_spec failed: ...", no checked copy) still has a
    # later capped try's valid draft checked
    d7, T7, st7 = new_run("sp7", {})
    st7.tries.update(spec=1, spec_feedback=["draft_spec failed: max turns"])
    save_state(d7, st7)
    T7 = Capped(dict(T7.s, tries=["good"]))
    m7 = Machine(d7, T7, st7, backend=spike, log=lambda *a: None, cross_engine_check=False)
    m7.step()
    ok(m7.st.state == "EVIDENCE", f"SP7 a capped try after an earlier no-draft note is checked ({m7.st.state}, "
                                  f"{m7.st.tries.get('spec_feedback')})")
    # SP2: an explicit zero turn cap is refused, never passed on as "no limit"
    from v2_sdk_tasks import SdkTasks
    try:
        SdkTasks(os.path.join(td, "sp2"), max_turns=0)
        refused = False
    except ValueError:
        refused = True
    ok(refused, "SP2 SdkTasks(max_turns=0) is refused: the SDK reads 0 as no turn limit")
