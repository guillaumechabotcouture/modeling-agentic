#!/usr/bin/env python3
"""v2 C acceptance test: replay golden traces from stored runs through the stage machine.

    python3 scripts/golden_traces.py run <name>=<run_dir>[:<max_rounds>] ... --work <scratch_dir> --out results.json
    python3 scripts/golden_traces.py --self-test

The run directories are copies (never the originals); each case works on its own fresh
copy (`cp -c` on macOS) under --work, removed afterwards.

Three checks per trace (docs/design/v2c-orchestration.md §7):

  A  stored gate: the gate decision the run stored (final_gate.json) is re-decided by the
     rule function the v2 gate calls (validate_critique_yaml._recompute_action) and must give
     the stored action and rule; then the machine routes it (route_gate).
     With `:<max_rounds>` the stored decision is re-decided with that many rounds instead
     (the early-finish variant: attempt 8 with rounds left gives rule 3a).
  B  today's gate: the validator CLI, exactly as main.py's final gate calls it, on one copy;
     the machine's GATE state (legacy mode) on another. The actions must match, and the
     machine's next state must be route_gate's for that action.
  C  the recorded sequence: every gate in validator_history.jsonl is routed, and the route
     is compared with what the run did next (another round, a post-write gate, or the end).
     Divergences are listed: they are places where today's lead did not follow STAGE 7.
  D  the v2 GATE state itself (agents/stage_machine.py v2_gate, not the legacy wrapper) on a
     copy, fed the harness findings the stored gate recorded (rigor, spec-compliance and
     registry violations from final_gate.json); the critic YAMLs, scope declarations, done_when
     tests and validator history are read from the copy as the stored gate read them. Its
     action and rule must equal the stored ones (independent: they were written by the gate
     at the time), or with `:<max_rounds>` the rule function's (A).

`run` exits 1 when any check fails or any gate gives no decision. The self-test includes a
mutation test: with v2_gate replaced by an unconditional ACCEPT, check D must fail.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (HERE, REPO, os.path.join(REPO, "agents")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def copy_run(src: str, dst: str) -> None:
    if sys.platform == "darwin" and subprocess.run(["cp", "-cRp", src, dst], capture_output=True).returncode == 0:
        subprocess.run(["chmod", "-R", "u+w", dst], capture_output=True)
        return
    shutil.copytree(src, dst, symlinks=True)


def history(run_dir: str) -> list[dict]:
    out = []
    try:
        for line in open(os.path.join(run_dir, "validator_history.jsonl")):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def observed_next(hist: list[dict], i: int) -> str:
    """What the stored run did after gate i: NEXT_ROUND, REGATE (same round, pre-write),
    POST_WRITE (a post-write gate followed: a draft was written), or END."""
    if i + 1 >= len(hist):
        return "END"
    a, b = hist[i], hist[i + 1]
    if (b.get("round") or 0) > (a.get("round") or 0):
        return "NEXT_ROUND"
    if b.get("post_write") and not a.get("post_write"):
        return "POST_WRITE"
    return "REGATE"


# The machine's route -> the observed moves that agree with it. A same-round re-gate is the
# lead re-running the gate after a partial fix (inside the machine's FIX_LOOP checks) or while
# preparing the write-up (DECLARE_SCOPE / ACCEPT at the last round).
AGREES = {"FIX_LOOP": {"NEXT_ROUND", "REGATE"}, "ENGINE_CHOICE": {"NEXT_ROUND"}, "SPEC": {"NEXT_ROUND"},
          "WRITE": {"POST_WRITE", "REGATE"}, "FINAL": {"END"}, "OWNER_DECISION": {"REGATE"}, "CRITIQUE": {"REGATE"}}


def check_sequence(hist: list[dict]) -> dict:
    import stage_machine as SM
    rows, div = [], []
    for i, h in enumerate(hist):
        nxt = SM.route_gate({"action": h.get("action")}, bool(h.get("post_write")))[0]
        obs = observed_next(hist, i)
        agree = obs in AGREES.get(nxt, set())
        rows.append({"i": i, "round": h.get("round"), "post_write": h.get("post_write"), "action": h.get("action"),
                     "machine": nxt, "observed": obs, "agree": agree})
        if not agree:
            div.append(rows[-1])
    return {"gates": len(rows), "agree": sum(r["agree"] for r in rows), "divergences": div,
            "actions": sorted({str(r["action"]) for r in rows}),
            "post_write_high": any(r["post_write"] and r["action"] == "PATCH_OR_RETHINK" for r in rows)}


def check_stored(run_dir: str, max_rounds: int | None) -> dict:
    import stage_machine as SM
    import validate_critique_yaml as V
    d = json.load(open(os.path.join(run_dir, "final_gate.json")))
    hist = history(run_dir)
    cur = hist[-1]["round"] if hist else 1
    mr_stored = cur + int(d.get("rounds_remaining") or 0)
    mr = max_rounds or mr_stored
    r = V._recompute_action(copy.deepcopy(d), mr, cur)
    pw = bool(d.get("post_write"))
    nxt, status, mode = SM.route_gate(r, pw)
    return {"round": cur, "max_rounds": mr, "post_write": pw, "stored_action": d.get("action"),
            "stored_rule": d.get("rule_matched"), "rules_action": r["action"], "rules_rule": r["rule_matched"],
            "same_as_stored": (max_rounds is None and r["action"] == d.get("action")
                               and str(r["rule_matched"]) == str(d.get("rule_matched"))),
            "machine_next": nxt, "status": status, "write_mode": mode}


def check_today(src: str, work: str, max_rounds: int) -> dict:
    """Today's gate CLI on one copy, the machine's GATE state (legacy mode) on another."""
    import stage_machine as SM
    from agents.run_finish import gate_command
    hist = history(src)
    cur = hist[-1]["round"] if hist else 1
    pw = os.path.exists(os.path.join(src, "report.md"))
    a, b = os.path.join(work, "cli"), os.path.join(work, "machine")
    copy_run(src, a)
    copy_run(src, b)
    try:
        p = subprocess.run(gate_command(a, max_rounds, cur, pw, history=False), cwd=REPO, capture_output=True,
                           text=True, timeout=1800)
        out = p.stdout
        try:
            cli = json.loads(out[out.index("{"):])
        except ValueError:
            cli = {"action": None, "error": (p.stderr.strip().splitlines() or [""])[-1][:300]}
        meta = {}
        try:
            meta = json.load(open(os.path.join(b, "metadata.json")))
        except (OSError, ValueError):
            pass
        st = SM.RunState(question=meta.get("question", ""), state="GATE", round=cur, max_rounds=max_rounds,
                         gate_mode="legacy", post_write=pw, signoff="none")
        m = SM.Machine(b, None, st, log=lambda *x: None)
        m.step()
        mach = m.st.last_gate
        import findings_ledger as FL
        led = FL.load(b)
        exp_next = SM.route_gate(cli, pw)[0] if cli.get("action") else "FINAL"
        return {"round": cur, "max_rounds": max_rounds, "post_write": pw, "cli_action": cli.get("action"),
                "cli_rule": cli.get("rule_matched"), "machine_action": mach.get("action"),
                "machine_rule": mach.get("rule_matched"), "machine_next": m.st.state,
                "same_action": cli.get("action") == mach.get("action")
                and str(cli.get("rule_matched")) == str(mach.get("rule_matched")),
                "next_as_routed": m.st.state == exp_next,
                "ledger": {"rows": len(led), "open_blocking": sum(1 for f in led.values() if f.blocking and f.status == "open"),
                           "unresolved_high_plus_accept": len(cli.get("unresolved_high") or []) + len(cli.get("accept_blockers") or [])}}
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


# Kinds the v2 gate derives itself from the files (done_when tests, the optional cross-model
# critic, the presentation critic's absence): not replayed from the stored list, so they are
# neither double-counted nor taken on trust.
_REDERIVED = ("done_when_", "cross_model_critic_", "critique_presentation_missing", "critique_done_when_missing")


class _ReplayBackend:
    """The harness checks as the stored gate recorded them (final_gate.json)."""

    def __init__(self, decision: dict):
        self.d = decision

    def _keep(self, items):
        return [copy.deepcopy(v) for v in items or [] if isinstance(v, dict)
                and not str(v.get("kind") or "").startswith(_REDERIVED)]

    def gate_findings(self, run_dir):
        return self._keep(self.d.get("rigor_violations"))

    def spec_violations(self, run_dir):
        return self._keep(self.d.get("spec_violations"))

    def registry_violations(self, run_dir):
        return self._keep(self.d.get("registry_violations"))

    def headline(self, run_dir):
        return {}


def check_v2(src: str, work: str, max_rounds: int | None = None, expected: tuple | None = None) -> dict:
    """Check D: the machine's v2 GATE state on a copy, against the stored decision."""
    import stage_machine as SM
    d = json.load(open(os.path.join(src, "final_gate.json")))
    hist = history(src)
    cur = hist[-1]["round"] if hist else 1
    mr = max_rounds or cur + int(d.get("rounds_remaining") or 0)
    pw = bool(d.get("post_write"))
    exp_action, exp_rule = expected or (d.get("action"), d.get("rule_matched"))
    b = os.path.join(work, "v2")
    copy_run(src, b)
    try:
        # The copy's history ends with the stored gate's own record; the v2 gate reads only
        # earlier rounds from it (declared_prev), as the stored gate did.
        meta = {}
        try:
            meta = json.load(open(os.path.join(b, "metadata.json")))
        except (OSError, ValueError):
            pass
        st = SM.RunState(question=meta.get("question", ""), state="GATE", round=cur, max_rounds=mr, gate_mode="v2",
                         post_write=pw, signoff="none")
        m = SM.Machine(b, None, st, backend=_ReplayBackend(d), log=lambda *x: None)
        m.step()
        g = m.st.last_gate
        exp_next = SM.route_gate({"action": exp_action, "unresolved_high": d.get("unresolved_high")}, pw)[0]
        ok = (g.get("action") is not None and g.get("action") == exp_action
              and str(g.get("rule_matched")) == str(exp_rule) and m.st.state == exp_next)
        return {"round": cur, "max_rounds": mr, "post_write": pw, "expected_action": exp_action,
                "expected_rule": exp_rule, "v2_action": g.get("action"), "v2_rule": g.get("rule_matched"),
                "v2_next": m.st.state, "expected_next": exp_next, "ok": ok}
    finally:
        shutil.rmtree(b, ignore_errors=True)


def case_ok(res: dict) -> bool:
    st = res["stored"]
    good = st["same_as_stored"] if not st.get("override") else st["rules_action"] is not None
    good = good and res["v2"]["ok"]
    if "today" in res:
        t = res["today"]
        good = good and t["cli_action"] is not None and t["same_action"] and t["next_as_routed"]
    return bool(good)


def run(cases: list[tuple[str, str, int | None]], work: str, skip_today: bool = False) -> dict:
    os.makedirs(work, exist_ok=True)
    out = {}
    for name, src, mr in cases:
        hist = history(src)
        res = {"source": src, "sequence": check_sequence(hist), "stored": check_stored(src, mr)}
        res["stored"]["override"] = mr is not None
        w = tempfile.mkdtemp(prefix=f"{name}_v2_", dir=work)
        try:
            exp = (res["stored"]["rules_action"], res["stored"]["rules_rule"]) if mr else None
            res["v2"] = check_v2(src, w, mr, exp)
        finally:
            shutil.rmtree(w, ignore_errors=True)
        if not skip_today:
            st = res["stored"]
            w = tempfile.mkdtemp(prefix=f"{name}_", dir=work)
            try:
                res["today"] = check_today(src, w, mr or st["max_rounds"])
            finally:
                shutil.rmtree(w, ignore_errors=True)
        res["ok"] = case_ok(res)
        out[name] = res
        print(json.dumps({name: {"ok": res["ok"], "stored": res["stored"], "v2": res["v2"], "today": res.get("today"),
                                 "sequence": {k: v for k, v in res["sequence"].items() if k != "divergences"}}},
                         default=str), flush=True)
    return out


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    h = [{"round": 1, "action": "PATCH_OR_RETHINK"}, {"round": 1, "action": "PATCH_OR_RETHINK"},
         {"round": 2, "action": "DECLARE_SCOPE"}, {"round": 2, "action": "DECLARE_SCOPE", "post_write": True}]
    ok([observed_next(h, i) for i in range(4)] == ["REGATE", "NEXT_ROUND", "POST_WRITE", "END"],
       "G1 what the run did after each gate")
    s = check_sequence(h)
    ok(s["agree"] == 4 and not s["divergences"], f"G2 a run that followed STAGE 7 agrees at every gate {s}")
    s = check_sequence([{"round": 1, "action": "RETHINK_STRUCTURAL"},
                        {"round": 1, "action": "RETHINK_STRUCTURAL", "post_write": True}])
    ok(len(s["divergences"]) == 2 and {d_["machine"] for d_ in s["divergences"]} == {"ENGINE_CHOICE"},
       "G3 a report written after RETHINK_STRUCTURAL is a divergence (the machine goes back to ENGINE_CHOICE)")
    s = check_sequence([{"round": 1, "action": "PATCH_OR_RETHINK", "post_write": True}])
    ok(s["post_write_high"] and s["divergences"][0]["machine"] == "FIX_LOOP",
       "G4 a post-write HIGH routes to the fix loop (the stored run ended there)")
    with tempfile.TemporaryDirectory() as d:
        json.dump({"action": "DECLARE_SCOPE", "rule_matched": 3, "rounds_remaining": 0, "post_write": True,
                   "unresolved_high": [{"reviewer": "rigor-artifacts", "kind": "benchmark_drift", "scope_declarable": True}],
                   "accept_blockers": [], "declared": ["benchmark_drift"], "declared_prev": ["benchmark_drift"]},
                  open(os.path.join(d, "final_gate.json"), "w"))
        open(os.path.join(d, "validator_history.jsonl"), "w").write(json.dumps({"round": 4, "action": "DECLARE_SCOPE"}) + "\n")
        r = check_stored(d, None)
        ok(r["same_as_stored"] and r["machine_next"] == "FINAL" and r["status"] == "completed_scope_declared",
           f"G5 a stored decision re-decided by the rule function gives the stored action {r}")
        r = check_stored(d, 8)
        ok(r["rules_action"] == "DECLARE_SCOPE" and str(r["rules_rule"]) == "3a",
           f"G6 the same decision with rounds left is the early finish (rule 3a) {r['rules_rule']}")
    # G7-G9: check D on a small synthetic run, then the mutation test.
    import yaml
    import stage_machine as SM
    import validate_critique_yaml as V
    with tempfile.TemporaryDirectory() as d:
        run_d = os.path.join(d, "run")
        os.makedirs(run_d)
        blk = {"id": "D-001", "severity": "HIGH", "category": "METHODS", "target_stage": "MODEL", "first_seen_round": 1,
               "claim": "c", "evidence": "e", "fix_requires": "f", "resolved": False,
               "done_when": [{"file": "x.txt", "exists": True}]}
        for r, bl in (("critique-domain", [blk]), ("critique-redteam", [])):
            yaml.safe_dump({"reviewer": r, "round": 1, "verdict": "REVISE" if bl else "PASS",
                            "structural_mismatch": {"detected": False}, "carried_forward": [], "blockers": bl},
                           open(os.path.join(run_d, V.FILENAMES[r]), "w"))
        json.dump({"question": "q"}, open(os.path.join(run_d, "metadata.json"), "w"))
        json.dump({"action": "PATCH_OR_RETHINK", "rule_matched": 2, "rounds_remaining": 2, "post_write": False,
                   "unresolved_high": [], "rigor_violations": [], "spec_violations": [], "registry_violations": []},
                  open(os.path.join(run_d, "final_gate.json"), "w"))
        open(os.path.join(run_d, "validator_history.jsonl"), "w").write(
            json.dumps({"round": 1, "action": "PATCH_OR_RETHINK", "findings": []}) + "\n")
        r = check_v2(run_d, d)
        ok(r["ok"] and r["v2_action"] == "PATCH_OR_RETHINK" and r["v2_next"] == "FIX_LOOP",
           f"G7 the v2 GATE state reproduces a stored PATCH_OR_RETHINK {r}")
        orig = SM.v2_gate
        SM.v2_gate = lambda *a, **k: {"action": "ACCEPT", "rule_matched": 4, "unresolved_high": [], "accept_blockers": []}
        try:
            r = check_v2(run_d, d)
        finally:
            SM.v2_gate = orig
        ok(not r["ok"] and r["v2_action"] == "ACCEPT",
           "G8 mutation: a v2 gate that always ACCEPTs fails check D")
        res = {"stored": {"same_as_stored": True, "override": False, "rules_action": "X"}, "v2": dict(r, ok=True),
               "today": {"cli_action": None, "same_action": True, "next_as_routed": True}}
        ok(not case_ok(res), "G9 a gate that gives no decision fails the case")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("cases", nargs="+", help="name=run_dir[:max_rounds]")
    r.add_argument("--work", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--skip-today", action="store_true")
    a = ap.parse_args(argv)
    cases = []
    for c in a.cases:
        name, _, rest = c.partition("=")
        path, _, mr = rest.partition(":")
        cases.append((name, os.path.abspath(path), int(mr) if mr else None))
    res = run(cases, a.work, a.skip_today)
    with open(a.out, "w") as f:
        json.dump(res, f, indent=1, default=str)
    bad = [n for n, r in res.items() if not r["ok"]]
    print("ALL OK" if not bad else f"FAILED: {', '.join(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
