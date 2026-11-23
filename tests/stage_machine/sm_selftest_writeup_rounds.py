"""Self-tests of agents/stage_machine.py (writeup_rounds), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import json
import os
import findings_ledger as FL
from _stage_machine_under_test import (
    CRITICS, DECLARED_FILE, Machine, apply_medium_rule, load_state, owner_accepted_now, owner_held, save_state,
)


def _self_test_writeup_rounds(ok, td: str, spike, new_run) -> None:
    """Harness baseline hb1 (ranked fixes 6 and 7): the rounds a run spent with nothing to fix.

    WR1-WR3 (cause G; dengue R1 and R2, measles R1, tb R2): a write-up-only fix went FIX_LOOP -> WRITE with the round
    raised, and the post-write gate refused the domain and red-team files as a stale round ("round must be N, got
    N-1"), re-running them through the schema repair. They now re-review the draft as a route of their own.
    WR4-WR7 (cause H; malaria R2, measles R2 round 2): the medium rule sent a finding the owner (or the lead,
    provisionally) had accepted to the fix loop, which had no task for it, and two more critique rounds ran."""
    from v2_tasks import StubTasks

    def mk(d, T, st):
        return Machine(d, T, st, backend=spike, log=lambda *a: None, cross_engine_check=False)

    def hist(d):
        return [h for h in load_state(d).history if "to" in h]

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- before the fix exists this is the failure
            import traceback
            traceback.print_exc()
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def rereview():
        # WR1-WR3: a HIGH write-up finding: ACCEPT pre-write, the post-write gate patches it, the writer revises
        wup = {"id": "R-030", "severity": "HIGH", "target_stage": "WRITE", "claim": "the report overstates the effect",
               "done_when": [{"file": "manuscript.md", "contains": "(?i)effect is bounded"}]}
        d, T, st = new_run("wr_rereview", {"critics": {1: {"critique-redteam": [wup]}}}, max_rounds=3)
        s = mk(d, T, st).run()
        h = hist(d)
        edges = [(x["from"], x["to"]) for x in h]
        whys = [str(x.get("why")) for x in h]
        st2 = load_state(d)
        ok(("FIX_LOOP", "WRITE") in edges and ("PRESENTATION", "CRITIQUE") in edges
           and not any(w.startswith("schema errors") for w in whys),
           f"WR1 a write-up-only fix goes WRITE -> PRESENTATION -> CRITIQUE, never through a schema repair "
           f"({s}, {edges[-8:]}, {[w for w in whys if 'schema' in w]})")
        i = edges.index(("PRESENTATION", "CRITIQUE"))
        r = h[i]["round"]
        ok("re-review" in whys[i] and f"critique:critique-domain:{r}" in T.calls
           and f"critique:critique-redteam:{r}" in T.calls and edges[i + 1] == ("CRITIQUE", "GATE")
           and h[i + 1]["round"] == r,
           f"WR2 the re-review runs the domain and red-team critics at the revised draft's round, then the gate "
           f"({whys[i]!r}, round {r}, {T.calls[-6:]})")
        ok("schema" not in (st2.tries or {}) and "rereview" not in (st2.tries or {}),
           f"WR3 the re-review uses no schema repair and leaves no pending re-review ({st2.tries})")

    def rereview_not_accepted():
        # WR1b: the same route for a not-accepted draft (which otherwise goes WRITE -> GATE): its presentation review
        # is redone at the new round too, so the gate finds no stale file (Codex r1 of #344, P2-5)
        import shutil
        src = os.path.join(td, "wr_rereview")
        d = os.path.join(td, "wr_rereview_na")
        shutil.copytree(src, d)
        T, st = StubTasks({}), load_state(d)
        st.state, st.round, st.post_write, st.round_pending, st.write_mode = "WRITE", 2, False, False, "not_accepted"
        st.status, st.forced_status = None, None
        st.tries["rereview"] = 2
        save_state(d, st)
        for r in CRITICS + ("critique-presentation",):
            StubTasks({}).critique(d, r, 1)
        m = mk(d, T, st)
        for _ in range(4):
            m.step()
        h = hist(d)
        e = [(x["from"], x["to"]) for x in h]
        ok(e[-4:-1] == [("WRITE", "PRESENTATION"), ("PRESENTATION", "CRITIQUE"), ("CRITIQUE", "GATE")]
           and "critique:critique-domain:2" in T.calls and "critique:critique-redteam:2" in T.calls
           and "critique:critique-presentation:2" in T.calls and not str(h[-1].get("why")).startswith("schema errors"),
           f"WR1b a revised not-accepted draft goes WRITE -> PRESENTATION -> CRITIQUE (re-review) -> GATE, and the gate "
           f"finds no stale round ({e[-4:]}, {h[-1].get('why')!r}, {T.calls})")

    def owner_held_run():
        # WR4-WR6: malaria R2 in miniature. A MEDIUM finding whose remedy is an owner amendment, with a test that fails;
        # the lead accepts it provisionally. It is declared at once: no fix loop with no task, no second critique round.
        dw = [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 9999}]
        amend = {"id": "D-001", "severity": "MEDIUM", "target_stage": "PLAN", "claim": "net durability is untested",
                 "fix_requires": "Owner amendment: add a durability decision test.", "done_when": dw}
        d, T, st = new_run("wr_owner_held", {"critics": {1: {"critique-domain": [amend]}}}, signoff="lead",
                           max_rounds=3)
        s = mk(d, T, st).run()
        h = hist(d)
        to = [x["to"] for x in h]
        crit = [c for c in T.calls if c.startswith("critique:critique-domain")]
        ok(s == "completed_scope_declared" and "FIX_LOOP" not in to and crit == ["critique:critique-domain:1"],
           f"WR4 an owner-accepted finding is declared, not sent to a fix loop with no task ({s}, {to[-8:]}, {crit})")
        ok(to.count("OWNER_DECISION") == 1,
           f"WR5 the same finding (same fingerprint) is asked once ({to.count('OWNER_DECISION')} request(s))")
        import yaml
        dec = yaml.safe_load(open(os.path.join(d, DECLARED_FILE))) or {}
        g = json.load(open(os.path.join(d, "final_gate.json")))
        ok([x.get("id") for x in dec.get("declared") or []] == ["D-001"] and g.get("rule_matched") == "3m"
           and "owner" in str(g.get("rationale")),
           f"WR6 the write-up declares it, and the gate says the owner answered it ({dec.get('declared')}, "
           f"{g.get('rule_matched')}, {g.get('rationale')!r})")

    def pending_post_write():
        # WR9 (Codex r2 of #344, P1-1): the owner accepted D-001 pre-write; the presentation review raises P-001, whose
        # remedy is an owner amendment. The post-write gate asks the owner before it publishes a final disposition:
        # no final_gate.json, and the report's status is pending, while P-001 waits.
        import scope_signoff as _SS
        dw = [{"file": "analysis_settings.yaml", "key": "uncertainty.draws", "op": ">=", "value": 9999}]
        amend = {"id": "D-001", "severity": "MEDIUM", "target_stage": "PLAN", "claim": "durability untested",
                 "fix_requires": "Owner amendment: add a durability test.", "done_when": dw}
        pres = {"id": "P-001", "severity": "MEDIUM", "target_stage": "WRITE", "claim": "the comparator label is unclear",
                "fix_requires": "Owner amendment: rename the comparator in the spec.",
                "done_when": [{"file": "manuscript.md", "contains": "(?i)funded plan comparator"}]}
        d, T, st = new_run("wr_pending_pw", {"critics": {1: {"critique-domain": [amend],
                                                            "critique-presentation": [pres]}}}, max_rounds=1)
        s1 = mk(d, T, st).run()
        _SS.sign(d, "owner", all_decision="accept", note="WR9")
        json.dump({"action": "DECLARE_SCOPE", "stale": True}, open(os.path.join(d, "final_gate.json"), "w"))
        s2 = mk(d, StubTasks(T.s), None).run()
        rep = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        req = json.load(open(os.path.join(d, "scope_signoff_request.json")))
        ok(s1 == s2 == "paused:awaiting_scope_signoff" and [i.get("finding") for i in req.get("items") or []] == ["P-001"]
           and not os.path.exists(os.path.join(d, "final_gate.json")) and "completed_scope_declared" not in rep
           and "P-001" not in open(os.path.join(d, DECLARED_FILE)).read(),
           f"WR9 a post-write gate with a finding awaiting the owner publishes no final disposition ({s1}, {s2}, "
           f"final_gate: {os.path.exists(os.path.join(d, 'final_gate.json'))}, declared status in report: "
           f"{'completed_scope_declared' in rep})")

    def answer_binds_current_text():
        # WR10 (Codex r2 of #344, P1-2): the owner's accept holds a finding only for the claim and remedy it was given
        # for, as the gate reads them now
        d = os.path.join(td, "wr_owner_held")          # WR4's run: the lead accepted D-001 as stated
        o = {"id": "D-001", "claim": "net durability is untested",
             "fix_requires": "Owner amendment: add a durability decision test."}
        ok(owner_accepted_now(d, [o], True) == {"D-001"} and owner_accepted_now(d, [dict(o, fix_requires=None)], True)
           == set() and owner_accepted_now(d, [o], False) == set(),
           "WR10 an accept holds the finding as answered; a revised remedy, or a lead answer in an owner run, does not")

    def medium_rule_unit():
        # WR7: the rule itself. A tested finding the owner holds is declared; a tested one the fix loop can work to is
        # still patched while rounds remain.
        op = [{"id": "D-001", "tested": True, "severity": "MEDIUM", "source": "critique-domain", "claim": "c"}]
        acc = {"action": "ACCEPT", "rule_matched": "4"}
        d1 = apply_medium_rule(acc, op, 2, True, owner_held={"D-001"})
        d2 = apply_medium_rule(acc, op, 2, True)
        d3 = apply_medium_rule(acc, op + [dict(op[0], id="R-002")], 2, True, owner_held={"D-001"})
        ok(d1["action"] == "DECLARE_SCOPE" and d1["rule_matched"] == "3m" and d2["action"] == "PATCH_OR_RETHINK"
           and d3["action"] == "PATCH_OR_RETHINK" and d3["open_tested"] == ["R-002"],
           f"WR7 the medium rule never sends an owner-held finding to the fix loop ({d1['action']}, {d2['action']}, "
           f"{d3.get('open_tested')})")
        led = {"A": FL.Finding(id="A", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="owner_accepted", resolution_type="owner_decision"),
               "B": FL.Finding(id="B", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="open", fix_requires="Owner amendment: x"),
               "C": FL.Finding(id="C", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="open", fix_requires="re-run the batch"),
               "E": FL.Finding(id="E", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                               status="owner_declined", resolution_type="owner_decision")}
        ok(owner_held(led) == {"A"}, f"WR7b owner-held: answered and accepted only; one awaiting the owner's answer is "
                                     f"not declared before it (Codex r1 of #344, P1-1) ({sorted(owner_held(led))})")
        # WR7c (Codex r1 of #344, P1-1): post-write, an unanswered amendment is not declared (no DECLARE_SCOPE is
        # published while the owner has not answered); the gate keeps it as fix-loop work and asks the owner first
        d4 = apply_medium_rule(acc, op, 2, True, owner_held=owner_held({"D-001": led["B"]}))
        ok(d4["action"] == "PATCH_OR_RETHINK", f"WR7c an unanswered amendment is not declared ({d4['action']})")

    def zero_tasks_prewrite():
        # WR8-WR9: pre-write, a fix loop with no task where every open finding the gate holds is the write-up's or
        # the owner's goes to WRITE, not to another critique round; one with an open finding a task could fix does not.
        import fix_loop as _FX
        real = _FX.run_loop
        # Codex r1 of #344: a rendered report.md counts as a draft (P1-2), and a gate that holds an unresolved HIGH (a
        # declared finding it still blocks on: P1-3) or patches by another rule (P2-4) keeps its critique round
        gate4m = {"action": "PATCH_OR_RETHINK", "rule_matched": "4m", "open_tested": ["W"], "unresolved_high": [],
                  "owner_held": ["A"]}
        # Codex r2 of #344: the mode follows what this gate holds, not an older owner-accepted finding (P1-4: wr_zero_wu2,
        # whose gate holds only the write-up's W), and the pre-write plan is kept apart from this round's post-write one
        # (P1-3)
        for name, extra, want, gate, report in (
                ("wr_zero_wu", None, "WRITE", gate4m, False),
                ("wr_zero_wu2", None, "WRITE", dict(gate4m, owner_held=[]), False),
                # Codex r3 of #344 (P1-1): the ledger still says A is accepted, but this gate read A as revised (not
                # held, in open_tested); and a gate record with no owner_held list (an older gate) takes no shortcut
                ("wr_zero_revised", None, "CRITIQUE", dict(gate4m, open_tested=["W", "A"], owner_held=[]), False),
                ("wr_zero_oldgate", None, "CRITIQUE", {k: v for k, v in gate4m.items() if k != "owner_held"}, False),
                ("wr_zero_fixable", "C", "CRITIQUE", gate4m, False),
                ("wr_zero_report", None, "CRITIQUE", gate4m, True),
                ("wr_zero_high", None, "CRITIQUE", dict(gate4m, unresolved_high=[{"id": "D-9", "severity": "HIGH"}]),
                 False),
                ("wr_zero_rule2", None, "CRITIQUE", dict(gate4m, rule_matched="2"), False),
                ("wr_zero_nogate", None, "CRITIQUE", None, False)):
            d, T, st = new_run(name, {}, max_rounds=3)
            if gate is not None:
                json.dump(gate, open(os.path.join(d, "gate_r1.json"), "w"))
            if report:
                open(os.path.join(d, "report.md"), "w").write("# Report\n")
            led = {"W": FL.Finding(id="W", kind="critique_blocker", severity="MEDIUM", source="critique-redteam",
                                   status="open", owner_state="WRITE", target_stage="WRITE"),
                   "A": FL.Finding(id="A", kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                                   status="owner_accepted", resolution_type="owner_decision", blocking=True)}
            if extra:
                led[extra] = FL.Finding(id=extra, kind="critique_blocker", severity="MEDIUM", source="critique-domain",
                                        status="open", owner_state="ANALYZE", blocking=True)
            FL.save(d, led)
            open(os.path.join(d, "fix_plan_r1.json"), "w").write('{"round": 1, "plan": {"tasks": []}}')
            st.state, st.round, st.round_pending, st.post_write = "FIX_LOOP", 1, True, False
            save_state(d, st)
            _FX.run_loop = lambda *a, **k: {"round": 1, "tasks": 0, "rows": 0, "passed": 0, "rethink": None,
                                            "write_up": ["W"], "owner_amendments": []}
            try:
                mk(d, T, st).step()
            finally:
                _FX.run_loop = real
            s2 = load_state(d)
            last = hist(d)[-1]
            mode = "accepted" if name == "wr_zero_wu2" else "declared"
            kept = (os.path.exists(os.path.join(d, "fix_plan_r1_prewrite.json"))
                    and not os.path.exists(os.path.join(d, "fix_plan_r1.json")))
            ok(s2.state == want and (want == "CRITIQUE" or (s2.round == 1 and not s2.round_pending
                                                            and s2.write_mode == mode and kept)),
               f"WR8 pre-write, a fix loop with no task goes to {want} ({name}: {s2.state}, round {s2.round}, "
               f"pending {s2.round_pending}, mode {s2.write_mode}, {last.get('why')!r})")

    for name, fn in (("WR1-WR3", rereview), ("WR1b", rereview_not_accepted), ("WR4-WR6", owner_held_run),
                     ("WR7", medium_rule_unit), ("WR8", zero_tasks_prewrite), ("WR9", pending_post_write),
                     ("WR10", answer_binds_current_text)):
        case(name, fn)
