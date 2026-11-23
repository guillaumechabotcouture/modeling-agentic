"""Self-tests of agents/stage_machine.py (geometry), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import json
import os
import analysis_backend as AB
from _stage_machine_under_test import Machine, SIGNOFF_FILE, init_run, load_state, sign, signed_hash
from sm_selftest_common import _t_owner_disposes


def _self_test_geometry(ok, td: str) -> None:
    """IG-M1-3 (docs/design/v2-input-geometry.md §4): the input geometry check through the machine. A units table
    whose two rows carry each other's points is refused before SIGN; accepted by name, it reaches SIGN; the gate
    carries the open finding."""
    import yaml
    from v2_tasks import StubTasks
    from toolkit.spec import schema as S
    from toolkit.spec import geometry as GEO
    toolkit = AB.backend("toolkit")

    def setup(d, accept=None):
        os.makedirs(d)
        raw = yaml.safe_load(open(S.toy_spec(d)))
        os.remove(os.path.join(d, AB.SPEC_FILE))
        rows = open(os.path.join(d, "units.csv")).read().strip().split("\n")
        feats, out = [], [rows[0] + ",lon,lat"]
        for k, line in enumerate(rows[1:]):
            x = k * 0.6
            feats.append({"type": "Feature", "properties": {"fid": str(k)}, "geometry": {
                "type": "Polygon", "coordinates": [[[x, 10], [x + 0.5, 10], [x + 0.5, 10.5], [x, 10.5], [x, 10]]]}})
            j = {0: 1, 1: 0}.get(k, k)                      # units 0 and 1 carry each other's points
            out.append(f"{line},{j * 0.6 + 0.25:.3f},10.25")
        open(os.path.join(d, "units.csv"), "w").write("\n".join(out) + "\n")
        json.dump({"type": "FeatureCollection", "features": feats}, open(os.path.join(d, "b.geojson"), "w"))
        yaml.safe_dump({"boundaries": {"path": "b.geojson", "feature_id": "fid"}},
                       open(os.path.join(d, "presentation.yaml"), "w"))
        if accept:
            raw["decision_units"][GEO.ACCEPT_KEY] = accept
        st = init_run(d, "toy question", 3, "owner", "v2", None, derived_inputs=False)
        return Machine(d, StubTasks({"spec": raw}), st, backend=toolkit, log=lambda *a: None, cross_engine_check=False)
    d1 = os.path.join(td, "ig1")
    m1 = setup(d1)
    status = m1.run(max_steps=12)
    fb = load_state(d1).tries.get("spec_feedback") or []
    ok(status == "run_failed" and any(x.startswith("reciprocal_swap") and "reciprocal_swap:0+1" in x for x in fb)
       and "SIGN" not in [h.get("to") for h in load_state(d1).history],
       f"IG-M1 a units table whose rows carry each other's points is refused before SIGN ({status}; {fb[:1]})")
    hf = {f["kind"]: f for f in m1.harness_findings()}
    ok(hf.get("reciprocal_swap", {}).get("severity") == "HIGH" and hf["reciprocal_swap"]["scope_declarable"] is False,
       f"IG-M3 the gate carries the open swap as a HIGH finding that cannot be declared ({sorted(hf)})")
    d2 = os.path.join(td, "ig2")
    why = "the owner checked both rows against the source"
    m2 = setup(d2, {"reciprocal_swap:0+1": why})
    m2.run(max_steps=12)
    rep = json.load(open(os.path.join(d2, GEO.ARTIFACT)))
    still = "reciprocal_swap" in {f["kind"] for f in m2.harness_findings()}
    ok(m2.st.state == "SIGN" and m2.st.paused == "awaiting_signoff"
       and [f.get("proposed") for f in rep["findings"]] == [why] and still,
       f"IG-M2a proposed by name with a reason, the spec reaches SIGN, where the owner decides; until then the gate "
       f"still carries the swap ({m2.st.state}, {m2.st.paused}, {still})")
    _t_owner_disposes(d2)
    toolkit.sign(d2, "owner")
    m2.step()
    ok(m2.st.state == "ENGINE_CHOICE" and "reciprocal_swap" not in {f["kind"] for f in m2.harness_findings()},
       f"IG-M2b the owner's sign-off approves the proposed exception: the run goes on and the gate no longer carries "
       f"it ({m2.st.state})")
    # IG-M4 (review P1): under --signoff lead a proposed HIGH exception pauses for the owner, as a spec sign-off does
    d4 = os.path.join(td, "ig4")
    os.makedirs(d4)
    m4 = setup(os.path.join(d4, "run"), {"reciprocal_swap:0+1": why})
    m4.st.signoff = "lead"
    m4.run(max_steps=12)
    from toolkit.spec import signoff as SO
    ok(m4.st.state == "SIGN" and m4.st.paused == "awaiting_signoff" and SO.status(m4.run_dir) == "awaiting",
       f"IG-M4a --signoff lead: a HIGH geometry exception is not lead-signed; the run pauses for the owner "
       f"({m4.st.state}, {m4.st.paused}, {SO.status(m4.run_dir)})")
    toolkit.sign(m4.run_dir, "owner")
    m4.step()
    ok(m4.st.state == "ENGINE_CHOICE" and "reciprocal_swap" not in {f["kind"] for f in m4.harness_findings()},
       f"IG-M4b once the owner signs, the run goes on with the exception approved ({m4.st.state})")
    import v2_sdk_tasks as SDK
    ok({GEO.ARTIFACT, GEO.SIGNOFF_FILE} <= set(SDK.HARNESS_FILES),
       "IG-M5 the check's record and the owner's geometry sign-off are harness files no task can write")
    # IG-M6 (Codex on 08f96a9, P2): the spike signer reads the spec once (no link followed), then hashes and freezes
    # that same content
    import builtins
    d6 = os.path.join(td, "ig6")
    os.makedirs(d6)
    try:
        GEO.toy_spec_with_boundaries(d6)
        opens, real_open, real_osopen = [], builtins.open, os.open

        def bopen(f, *a, **k):
            if str(f).endswith(AB.SPEC_FILE):
                opens.append(str(f))
            return real_open(f, *a, **k)

        def oopen(f, *a, **k):
            if str(f).endswith(AB.SPEC_FILE):
                opens.append(str(f))
            return real_osopen(f, *a, **k)
        builtins.open, os.open = bopen, oopen
        try:
            _t_owner_disposes(d6)
            rec6 = sign(d6, "owner", "n")
        finally:
            builtins.open, os.open = real_open, real_osopen
        g6 = json.load(open(os.path.join(d6, GEO.SIGNOFF_FILE)))
        ok(len(opens) == 1 and rec6["spec_sha256"] == signed_hash(d6) and (g6.get("declaration") or {}).get("enabled"),
           f"IG-M6 the spike signer reads the spec once, then hashes and freezes that content ({len(opens)} opens)")
    except Exception as e:  # noqa: BLE001 -- before the shared fixture exists (step α)
        ok(False, f"IG-M6 the spike signer reads the spec once: {type(e).__name__}: {e}")
    # IG-M7: the spike signer refuses a spec value justified by a held-out benchmark, or without a basis, for the owner
    # too (toolkit/spec/engine_parameters.py signoff_refusals); the id is read from a real engine's pack
    import yaml as _y
    from toolkit.spec import engine_parameters as PV
    ref, pk = next(((r, p) for r, p in PV._all_packs()[0] if PV.pack_ids(p)[1]), (None, None))
    hid = sorted(PV.pack_ids(pk)[1])[0] if pk else "?"
    got7 = {}
    for name, entry in (("text", {"value": 1.0, "source": "s", "basis": "owner_judgement",
                                  "reason": f"puts {hid} in band"}),
                        ("no basis", {"value": 1.0, "source": "s", "reason": "r"})):
        d7 = os.path.join(td, f"ig7_{name.replace(' ', '_')}")
        os.makedirs(d7)
        try:
            GEO.toy_spec_with_boundaries(d7)
            sp = _y.safe_load(open(os.path.join(d7, AB.SPEC_FILE)))
            sp["engine"] = {"ref": ref}
            sp["engine_parameters"] = {"p": entry}
            _y.safe_dump(sp, open(os.path.join(d7, AB.SPEC_FILE), "w"))
            _t_owner_disposes(d7)
            sign(d7, "owner", "n")
            why = ""
        except ValueError as e:
            why = str(e)
        got7[name] = (("held-out benchmark" if name == "text" else "basis") in why
                      and not os.path.exists(os.path.join(d7, SIGNOFF_FILE))
                      and not os.path.exists(os.path.join(d7, GEO.SIGNOFF_FILE)))
    ok(ref is not None and all(got7.values()),
       f"IG-M7 the spike signer refuses, for the owner, a spec value justified by a held-out benchmark or without a "
       f"basis, and writes no sign-off ({got7})")
