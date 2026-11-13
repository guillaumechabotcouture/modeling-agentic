#!/usr/bin/env python3
"""Preconditions of a signed spec: checks every signing path and the validation before a run call (#215, #218).

    python3 toolkit/spec/preconditions.py --self-test

`problems(run_dir, spec_bytes, signer)` runs every registered check on the spec and returns its problems (empty:
none). Callers: toolkit/spec/signoff.py sign, scripts/decision_brief.py sign, agents/stage_machine.py sign (with the
signer), and toolkit/spec/schema.py validate, which the analysis calls before a run (signer None: the run's
recorded sign-off decides). A check is registered by name in CHECKS: fn(run_dir, spec, signer) -> [str].

`decision_test_runs(spec, test_id, effect)` -> (ok, why): will this decision test execute on this run's path and
change this effect? `effect`: {parameters: engine parameters that carry the effect, components: spec components
that deliver it, rows: the engine's verified benchmark rows (for interaction tests)}. Nothing here names a disease.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _benchmark_eligibility(run_dir, spec, signer) -> list:
    """Departure shape, then the benchmark precondition: for a named (or resolved) engine, that engine; for engine:
    auto still pending, every candidate the selection could choose, refused when none passes."""
    from toolkit.engines import benchmark_coverage as BC
    out = list(BC.departure_problems(spec.raw))
    if not getattr(spec, "engine_pending", False):
        return out + BC.spec_problems(spec, by=signer)
    import importlib.util
    path = os.path.join(ROOT, "agents", "analysis_backend.py")
    m = importlib.util.spec_from_file_location("_ab_preconditions", path)
    AB = importlib.util.module_from_spec(m)
    m.loader.exec_module(AB)
    mode = "proposed" if (signer is None and BC.signature(run_dir) == "proposed") or signer == "owner" else "signed"
    raw = dict(spec.raw, _run_dir=run_dir)
    if signer is not None and signer != "owner":
        raw.pop("engine_departures", None)          # a lead's signature makes no departure count
    rows = AB.eligible_engines(raw, departures=mode)
    if any(r.get("eligible") for r in rows):
        return out
    return out + ["engine benchmarks: no candidate engine passes the engine choice's checks"] + [
        f"engine benchmarks: [{r.get('ref')}] {w}" for r in rows for w in (r.get("why_not") or [])][:40]


CHECKS = {"benchmark_eligibility": _benchmark_eligibility}


def problems(run_dir: str, spec_bytes: bytes | None, signer: str | None, spec=None) -> list:
    """Every registered check's problems. `spec`: an already loaded schema.Spec; else the spec the run will use is
    loaded from `spec_bytes` as the run's decision_spec.yaml (settings merged, engine_choice.json resolved)."""
    from toolkit.spec import schema
    if spec is None:
        import yaml
        try:
            raw = yaml.safe_load((spec_bytes or b"").decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            return [f"preconditions: the spec cannot be loaded: {type(e).__name__}: {e}"]
        if not isinstance(raw, dict):
            return ["preconditions: the spec cannot be loaded: it is not a mapping"]
        try:
            spec = schema.load(os.path.join(run_dir, "decision_spec.yaml"), data=spec_bytes)
        except schema.SpecError:
            # a spec this schema does not run (the spike backend's settings): its engine and departures as written
            spec = schema.Spec(raw, run_dir)
    out = []
    for name, fn in CHECKS.items():
        try:
            out += list(fn(run_dir, spec, signer) or [])
        except Exception as e:  # noqa: BLE001 -- a check that cannot run is a problem (fail closed)
            out.append(f"precondition {name} could not be checked: {type(e).__name__}: {e}")
    return out


def decision_test_runs(spec, test_id, effect: dict) -> tuple[bool, str]:
    """(True, "") when the test will run on this run's path and change the effect; else (False, why). The answer
    is the decision-test runner's own plan (toolkit/analysis/decision_tests.py plan_test) with the evidence the run
    will hold; no rule is restated here. `effect`: {parameters, components}."""
    raw = spec.raw
    t = next((x for x in (raw.get("analysis") or {}).get("decision_tests") or []
              if isinstance(x, dict) and str(x.get("id")) == str(test_id)), None)
    if t is None:
        return False, "is not an analysis.decision_tests id"
    comps = set(effect.get("components") or [])
    typ = t.get("type")
    if typ == "parameter_value" and t.get("parameter") not in (effect.get("parameters") or []):
        return False, (f"varies {t.get('parameter')!r}, not a parameter carrying this effect "
                       f"({list(effect.get('parameters') or []) or 'the coverage file declares none'})")
    if typ == "interaction_rescale" and not comps & set(t.get("components") or []):
        return False, (f"does not rescale the effect: it names {sorted(comps)} only in with_any or not at all "
                       "(the rescaled increment is that of `components`)")
    if typ not in ("parameter_value", "interaction_rescale"):
        return False, f"is a {typ} test, which does not vary a component's effect"
    from toolkit.analysis import decision_tests as DT
    try:
        ev = DT.prerun_evidence(spec)
        pl = DT.plan_test(spec, t, ev)
    except Exception as e:  # noqa: BLE001 -- cannot be planned: it cannot be relied on
        return False, f"cannot be planned: {type(e).__name__}: {e}"
    pks = {k: set(v) for k, v in ev["packages"].items()}
    if comps and not any(comps & cs for cs in pks.values()):
        return False, f"cannot change the effect: no allowed package holds {sorted(comps)}"
    if pl["status"] != "runs":
        return False, f"would not run: {pl['why']}"
    live = [x for x in pl["perturbations"] if not x.get("identity")]
    if not live:
        return False, "changes nothing: every tested value leaves the effect where it is"
    if typ == "interaction_rescale" and not any(comps & pks[k] for x in live for k in x["packages"]):
        return False, "modifies no package that delivers the component"
    return True, ""


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    import tempfile
    from toolkit.spec import schema
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        s = schema.load(p)
        ok(problems(d, open(p, "rb").read(), None) == [], "PC1 a toy spec has no precondition problems")
        CHECKS["fail"] = lambda r, sp, by: [f"fail:{by}"]
        try:
            ok(problems(d, None, "lead", spec=s) == ["fail:lead"], "PC2 a registered check runs with the signer")
            CHECKS["boom"] = lambda r, sp, by: 1 / 0
            ok(any("could not be checked" in x for x in problems(d, None, "owner", spec=s)),
               "PC3 a check that raises is a problem (fail closed)")
        finally:
            CHECKS.pop("fail", None)
            CHECKS.pop("boom", None)
        ok(problems(d, b"[: bad", None) and "cannot be loaded" in problems(d, b"[: bad", None)[0],
           "PC4 unparsable bytes are a problem")
    # review round 3 regression table: each reproduction through the precondition and through the runner's planner
    # with the evidence an analysis holds (decision_tests.runtime_evidence), which must agree
    from toolkit.analysis import decision_tests as DT
    from toolkit.analysis import run as RUN
    with tempfile.TemporaryDirectory() as d:
        bench, v_ok, v_bad = RUN._toy_bench(d)
        p = schema.toy_spec(d, n=12, bench=bench)
        base = schema.load(p).raw
        arm = DT.interaction_arms(schema.load(p), schema.load(p).engine(), "T2")["arm"]
        v_id = arm["a11"] / (arm["a10"] * arm["a01"])           # the engine's own interaction: scale 1
        I = lambda v, comps=("b",), wa=("a",): {"id": "X", "type": "interaction_rescale", "components": list(comps),  # noqa: E731
                                                "with_any": list(wa), "benchmark": "T2", "values": [v], "text": "t"}
        P = lambda par, vals: {"id": "X", "type": "parameter_value", "parameter": par, "values": vals,  # noqa: E731
                               "text": "t"}
        excl = {"components": {"a": {"group": "g"}, "b": {"group": "g"}, "c": {}}}
        cases = [  # (name, test, effect, spec change, expected)
            ("interaction reachable", I(v_ok), {"components": {"b"}}, {}, True),
            ("interaction unreachable", I(v_bad), {"components": {"b"}}, {}, False),
            ("interaction at the engine's own value (scale 1)", I(v_id), {"components": {"b"}}, {}, False),
            ("departed component only in with_any", I(v_ok), {"components": {"a"}}, {}, False),
            ("no package holds every rescaled component", I(v_ok, ("a", "b"), ("c",)), {"components": {"a"}},
             {"options": excl}, False),
            ("no subtraction package", I(v_ok), {"components": {"b"}},
             {"options": {"packages": {"none": [], "ab": ["a", "b"]}}}, False),
            ("spec engine values: arms at spec values missing", I(v_ok), {"components": {"b"}},
             {"engine_parameters": {"k": {"value": 0.8, "source": "s", "reason": "r", "basis": "owner_judgement"}}},
             False),
            ("parameter on an emulator input", P("eff_b", [0.2, 0.4]), {"components": {"b"}, "parameters": ["eff_b"]},
             {}, True),
            ("parameter at its point value only", P("eff_b", [0.3]), {"components": {"b"}, "parameters": ["eff_b"]},
             {}, False),
            ("parameter not an emulator input", P("k", [0.5, 1.0]), {"components": {"b"}, "parameters": ["k"]}, {},
             False),
            ("parameter not carrying the effect", P("eff_a", [0.4, 0.6]), {"components": {"b"}, "parameters": ["eff_b"]},
             {}, False),
            ("parameter on the direct path", P("k", [0.5, 1.0]), {"components": {"b"}, "parameters": ["k"]},
             {"analysis": {"evaluation": "direct"}}, True),
        ]
        for name, t, eff, chg, want in cases:
            raw = dict(base)
            for k, v in chg.items():
                raw[k] = dict(raw.get(k) or {}, **v) if isinstance(v, dict) else v
            raw["analysis"] = dict(raw["analysis"], decision_tests=[t])
            sp = schema.Spec(raw, d)
            got, why = decision_test_runs(sp, "X", eff)
            pre = DT.plan_test(sp, t, DT.prerun_evidence(sp))
            ev = DT.prerun_evidence(sp)

            class _M:       # what the analysis's model holds: its kind and design box (toolkit/analysis/model.py)
                kind = ev["kind"]
                param_names = list(ev["box"] or {})
                input_names = ["z"] + param_names
                dom_lo = [0.0] + [ev["box"][k][0] for k in param_names] if ev["box"] else [0.0]
                dom_hi = [1.0] + [ev["box"][k][1] for k in param_names] if ev["box"] else [1.0]

            class _A:
                spec, model, theta0, pk, eng = sp, _M, ev["point"], ev["packages"], ev["engine"]
            rt = DT.plan_test(sp, t, DT.runtime_evidence(_A))
            agree = (pre["status"], [x.get("identity") for x in pre["perturbations"]]) == \
                    (rt["status"], [x.get("identity") for x in rt["perturbations"]])
            ok(got == want and agree, f"PC5 {name}: precondition {got} ({why[:90]}), planner {pre['status']}/{rt['status']}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
