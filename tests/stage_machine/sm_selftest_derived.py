"""Self-tests of agents/stage_machine.py (derived), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import json
import os
import sys
import analysis_backend as AB
import _stage_machine_under_test as _SM
from _stage_machine_under_test import Machine, SIGNOFF_SHEET, init_run, load_state
from sm_selftest_common import _t_owner_disposes


def _self_test_derived(ok, td: str) -> None:
    """DI1, DI8 and the development flag at the gate, through the machine (docs/design/v2-derived-inputs.md §4)."""
    import shutil
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import schema as S
    import pipeline as P
    toolkit = AB.backend("toolkit")
    flag = P.sandbox_kind() is None
    # DI1: the evidence's shape. The task writes the units table itself; the spec is refused before SIGN.
    d = os.path.join(td, "di1")
    os.makedirs(d)
    raw = yaml.safe_load(open(S.toy_spec(d)))
    os.remove(os.path.join(d, AB.SPEC_FILE))
    st = init_run(d, "toy question", 3, "none", "v2", None, derived_inputs=True, derive_unsandboxed=flag)
    status = Machine(d, StubTasks({"spec": raw}), st, backend=toolkit, log=lambda *a: None,
                     cross_engine_check=False).run(max_steps=12)
    s1 = load_state(d)
    fb = s1.tries.get("spec_feedback") or []
    ok(status == "run_failed" and any(x.startswith("units_not_derived") for x in fb)
       and "SIGN" not in [h.get("to") for h in s1.history],
       f"DI1c through the machine: a units table the task wrote is refused before SIGN ({status}; {fb[:2]})")
    # DI8: engine auto with two candidates; the second needs an attribute no derived column binds.
    mods = os.path.join(td, "di8mods")
    os.makedirs(mods)
    with open(os.path.join(mods, "di8_engines.py"), "w") as f:
        f.write("from toolkit.engines.contract import ToyEngine\n\n\nclass NeedsGroup(ToyEngine):\n"
                "    \"\"\"The toy engine with a card that gives `group` no default.\"\"\"\n")
    with open(os.path.join(mods, "attribute_binding.yaml"), "w") as f:
        yaml.safe_dump({"attribute_binding": {"x": "required", "group": "required"}}, f)
    sys.path.insert(0, mods)
    from toolkit import import_guard as _G
    _G.allow_for_test("engine", "di8_engines")             # a test engine outside toolkit/engines (allowlist)
    fake = os.path.join(td, "di8repo")
    os.makedirs(os.path.join(fake, "toyraw"))
    tmp = os.path.join(td, "di8src")
    os.makedirs(tmp)
    base = yaml.safe_load(open(S.toy_spec(tmp)))
    shutil.copyfile(os.path.join(tmp, "units.csv"), os.path.join(fake, "toyraw", "units_raw.csv"))
    cols = open(os.path.join(fake, "toyraw", "units_raw.csv")).readline().strip().split(",")
    staged = "data/inputs/toyraw/units_raw.csv"

    def draft(question, run_dir):
        os.makedirs(os.path.join(run_dir, "data", "derive"), exist_ok=True)
        open(os.path.join(run_dir, "data", "derive", "units.py"), "w").write(S.TOY_DERIVE_SCRIPT)
        yaml.safe_dump({"derivations": [{"id": "units", "script": "data/derive/units.py", "inputs": [staged],
                                         "outputs": ["data/derived/units.csv"],
                                         "columns": {"uses": {staged: cols}}}]},
                       open(os.path.join(run_dir, "data", "derivations.yaml"), "w"), sort_keys=False)
        r = json.loads(json.dumps(base))
        r["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine", "di8_engines:NeedsGroup"]}
        r["decision_units"]["csv"] = "data/derived/units.csv"
        r["decision_units"]["defaults_accepted"] = {"group": "the toy units carry no group label"}
        r["calibration"]["target_frame"] = {"band": "prev", "source": "the engine's own window metric"}
        return r
    d = os.path.join(td, "di8")
    os.makedirs(d)
    old_repo, _SM.REPO = _SM.REPO, fake
    try:
        st = init_run(d, "Which package per unit? Inputs: toyraw/units_raw.csv", 3, "owner", "v2", None,
                      derived_inputs=True, derive_unsandboxed=flag)
        m = Machine(d, StubTasks({"spec": draft}), st,
                    backend=toolkit, log=lambda *a: None, cross_engine_check=False)
        m.run(max_steps=4)
        sheet = open(os.path.join(d, SIGNOFF_SHEET)).read() if os.path.exists(os.path.join(d, SIGNOFF_SHEET)) else ""
        ok(m.st.state == "SIGN" and m.st.paused == "awaiting_signoff" and "NeedsGroup" in sheet
           and "engine_attribute_unbound" in sheet and "group" in sheet,
           f"DI8a engine auto: the full checks run per candidate in SPEC, before SIGN; the candidate that needs an "
           f"unbound attribute is shown ineligible with the reason ({m.st.state}, {m.st.tries.get('spec_feedback')})")
        _t_owner_disposes(d)
        toolkit.sign(d, "owner")
        for _ in range(6):
            if m.st.state in ("CONFIGURE", "FINAL"):
                break
            m.step()
        ec = json.load(open(os.path.join(d, AB.ENGINE_CHOICE)))
        rows = {r["ref"]: r for r in ec["candidates"]}
        ng = rows.get("di8_engines:NeedsGroup") or {}
        ok(ec["engine"]["ref"] == "toolkit.engines.contract:ToyEngine" and ng.get("eligible") is False
           and any("engine_attribute_unbound" in w and "group" in w for w in ng.get("why_not") or []),
           f"DI8b engine_choice.json records that candidate as ineligible with the unbound list: {ng.get('why_not')}")
        kinds = {f["kind"]: f for f in m.harness_findings()}
        rec_kind = json.load(open(os.path.join(d, P.DERIVATIONS_RECORD)))["derivations"]["units"]["sandbox"]["kind"]
        ok(("derivation_unsandboxed" in kinds and kinds["derivation_unsandboxed"]["severity"] == "HIGH"
            and not kinds["derivation_unsandboxed"]["scope_declarable"]) if rec_kind == "none"
           else "derivation_unsandboxed" not in kinds,
           f"DI-U3 a run that used the development flag carries a HIGH non-declarable gate finding (no ACCEPT); a "
           f"sandboxed one does not (sandbox {rec_kind}; {sorted(kinds)})")
    finally:
        _SM.REPO = old_repo
        sys.path.remove(mods)
