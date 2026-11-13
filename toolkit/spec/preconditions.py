#!/usr/bin/env python3
"""Preconditions of a signed spec: checks every signing path and the validation before a run call (#215, #218).

    python3 toolkit/spec/preconditions.py --self-test

`problems(run_dir, spec_bytes, signer)` runs every registered check on the spec and returns its problems (empty:
none). Callers: toolkit/spec/signoff.py sign, scripts/decision_brief.py sign, agents/stage_machine.py sign (with the
signer), and toolkit/spec/schema.py validate, which the analysis calls before a run (signer None: the run's
recorded sign-off decides). A check is registered by name in CHECKS: fn(run_dir, spec, signer) -> [str].

The evidence dossier (#218) registers a check that also takes the call's context (CONTEXT_CHECKS): `mode` (sign: a
signer, before the record exists; status, launch, resume: a signed run, its record compared; unsigned: a run
nobody signs) and read / engine / record / capture. It runs only when a mode is given; its callers pass
`only=("evidence_dossier",)` so the benchmark check is not run twice on a signing path that already ran it.
`spec_bytes` None reads the run's decision_spec.yaml.

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
CONTEXT_CHECKS: set = set()          # checks called with the context: fn(run_dir, spec, signer, spec_bytes=, mode=, ...)


def register(name: str, fn, context: bool = False) -> None:
    CHECKS[name] = fn
    if context:
        CONTEXT_CHECKS.add(name)


def _load_builtin() -> None:
    from toolkit.spec import evidence_dossier as ED
    ED._register()


def problems(run_dir: str, spec_bytes: bytes | None, signer: str | None, spec=None, mode: str | None = None,
             only=None, **ctx) -> list:
    """Every registered check's problems. `spec`: an already loaded schema.Spec; else the spec the run will use is
    loaded from `spec_bytes` as the run's decision_spec.yaml (settings merged, engine_choice.json resolved), or read
    from the run dir when `spec_bytes` is None. `only`: the names of the checks to run. `mode` and `ctx` reach the
    context checks (CONTEXT_CHECKS)."""
    from toolkit.spec import schema
    _load_builtin()
    if spec_bytes is None and spec is None and run_dir:
        try:
            with open(os.path.join(run_dir, "decision_spec.yaml"), "rb") as f:
                spec_bytes = f.read()
        except OSError:
            spec_bytes = None
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
        if only is not None and name not in only:
            continue
        try:
            if name in CONTEXT_CHECKS:
                out += list(fn(run_dir, spec, signer, spec_bytes=spec_bytes, mode=mode, **ctx) or [])
            else:
                out += list(fn(run_dir, spec, signer) or [])
        except Exception as e:  # noqa: BLE001 -- a check that cannot run is a problem (fail closed)
            out.append(f"precondition {name} could not be checked: {type(e).__name__}: {e}")
    return out


def chosen_range_test_runs(spec: dict, test_id: str, effect: str, drawn: dict | None = None) -> tuple:
    """(ok, why, values) for the evidence dossier (#218): whether `test_id` executes on this run's path and varies the
    dossier input `effect` ("parameter:<name>"), and the values it sets, so the dossier's chosen range can be checked
    against them. A generated prior_range_/fitted_range_<p> test exists only for a parameter the draws vary (`drawn`).
    A declared parameter_value test varies its own parameter; on the emulator path only a drawn parameter or one the
    spec sets (engine_parameters) is an emulator input. (decision_test_runs above answers a different question: may a
    test carry a benchmark departure.)"""
    kind, _, name = str(effect).partition(":")
    if kind != "parameter":
        return False, f"cannot vary a {kind}: no decision test type does", []
    drawn = drawn or {}
    if test_id in (f"prior_range_{name}", f"fitted_range_{name}"):
        if name not in drawn:
            return False, (f"is generated only for a parameter the draw plan draws, and {name} is not drawn (no prior, "
                           "or excluded by uncertainty.excluded)"), []
        return True, "", list(drawn[name])
    an = spec.get("analysis") if isinstance(spec, dict) and isinstance(spec.get("analysis"), dict) else {}
    tests = {str(t.get("id")): t for t in (an.get("decision_tests") or []) if isinstance(t, dict)}
    t = tests.get(str(test_id))
    if t is None:
        return False, "is not an analysis.decision_tests id", []
    if t.get("type") != "parameter_value" or t.get("parameter") != name:
        return False, (f"does not vary parameter {name} (a parameter_value test of {name}, or prior_range_{name} for a "
                       "drawn prior, does)"), []
    eps = spec.get("engine_parameters") if isinstance(spec.get("engine_parameters"), dict) else {}
    if an.get("evaluation") == "emulator" and name not in drawn and name not in eps:
        return False, (f"does not execute on the emulator path: {name} is neither drawn nor set by engine_parameters, "
                       "so it is not an emulator input"), list(t.get("values") or [])
    return True, "", list(t.get("values") or [])


def _declared_range(spec, eng, p):
    """The parameter's declared range: its drawn prior's, else the engine's (lo, hi); None when it has none."""
    try:
        from toolkit.spec import priors as PRI
        pr = PRI.drawn(spec).get(p)
        if pr is not None:
            return float(pr.lo), float(pr.hi)
    except Exception:  # noqa: BLE001
        pass
    x = next((q for q in eng.parameters() if q.name == p), None)
    if x is not None and x.lo is not None and x.hi is not None and x.hi > x.lo:
        return float(x.lo), float(x.hi)
    return None


def decision_test_runs(spec, test_id, effect: dict) -> tuple[bool, str]:
    """(True, "") when the test is fit to carry a departure, judged on the spec's inputs before the run (the run then
    checks what the test actually did: toolkit/analysis/decision_tests.py check_departures). The runner's own plan
    (decision_tests.plan_test) must run it. A parameter test varies a parameter carrying the effect over its declared
    range (at least two distinct values, spanning the range or `range_fraction` of it). An interaction test targets
    the departed benchmark's own interaction: the test's benchmark is the departed one, and it rescales one of that
    benchmark's component pair given the other. `effect`: {parameters, components, benchmark, estimand, pair,
    range_fraction}."""
    raw = spec.raw
    t = next((x for x in (raw.get("analysis") or {}).get("decision_tests") or []
              if isinstance(x, dict) and str(x.get("id")) == str(test_id)), None)
    if t is None:
        return False, "is not an analysis.decision_tests id"
    comps = set(effect.get("components") or [])
    typ = t.get("type")
    if typ == "parameter_value":
        p = t.get("parameter")
        if p not in (effect.get("parameters") or []):
            return False, (f"varies {p!r}, not a parameter carrying this effect "
                           f"({list(effect.get('parameters') or []) or 'the coverage file declares none'})")
    elif typ == "interaction_rescale":
        if effect.get("estimand") != "interaction" or not effect.get("benchmark"):
            return False, "an interaction test can carry only a departure from an interaction benchmark"
        if str(t.get("benchmark")) != str(effect["benchmark"]):
            return False, (f"tests benchmark {t.get('benchmark')!r}'s interaction, not the departed "
                           f"{effect['benchmark']!r}")
        pm = {c: set(v) for c, v in (effect.get("pair") or {}).items()}
        tc, wa = set(t.get("components") or []), set(t.get("with_any") or [])
        resc = effect.get("rescaled")
        if len(pm) != 2 or resc not in pm:
            return False, "the coverage file does not say which of the benchmark's components its arms rescale"
        x, y = pm[resc], next(v for c, v in pm.items() if c != resc)
        if x & y:
            return False, (f"a spec component delivers both of the benchmark's components ({sorted(x & y)}): a bundle "
                           "cannot rescale one given the other")
        if tc & wa:
            return False, f"components and with_any overlap ({sorted(tc & wa)})"
        if not (tc and wa and tc <= x and wa <= y):
            return False, (f"does not rescale the departed benchmark's own pair in its arms' orientation ({resc} "
                           f"given the other: components within {sorted(x)}, with_any within {sorted(y)}); it "
                           f"rescales {sorted(tc)} given {sorted(wa)}")
    else:
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
    if typ == "parameter_value":
        vals = sorted({float(x["value"]) for x in pl["perturbations"]})
        rng = _declared_range(spec, ev["engine"], t["parameter"])
        if rng is None:
            return False, f"{t['parameter']} has no declared range (a prior or the engine's) to span"
        frac = float(effect.get("range_fraction") or 1.0)
        c, half = (rng[0] + rng[1]) / 2, frac * (rng[1] - rng[0]) / 2
        lo, hi = c - half, c + half                    # the interval to cover: the declared one, or its central share
        tol = 1e-9 * max(1.0, abs(rng[1] - rng[0]))
        if len(vals) < 2 or vals[0] > lo + tol or vals[-1] < hi - tol:
            return False, (f"its values {vals} do not cover [{lo:g}, {hi:g}] ({frac:g} of {t['parameter']}'s declared "
                           f"range {list(rng)}, around its centre; at least two distinct values)")
        return True, ""
    live = [x for x in pl["perturbations"] if not x.get("identity")]
    if not live:
        return False, "changes nothing: every tested value leaves the interaction where it is (scale 1)"
    pair = [set(v) for v in (effect.get("pair") or {}).values()]
    if not any(all(side & pks[k] for side in pair) for x in live for k in x["packages"]):
        return False, "modifies no package that holds the departed benchmark's pair"
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
        seen = {}
        register("ctx", lambda r, sp, by, **k: seen.update(k) or ["ctx"], context=True)
        try:
            got = problems(d, None, None, mode="status", only=("ctx",), record={"r": 1})
            ok(got == ["ctx"] and seen.get("mode") == "status" and seen.get("record") == {"r": 1}
               and seen.get("spec_bytes") == open(p, "rb").read(),
               f"PC4b a context check gets the mode, the context and the run's spec bytes; only= selects it: {got}")
        finally:
            CHECKS.pop("ctx", None)
            CONTEXT_CHECKS.discard("ctx")
    sp_ = {"analysis": {"evaluation": "emulator", "decision_tests": [
        {"id": "P", "type": "parameter_value", "parameter": "p", "values": [1, 2]}]}}
    ok(chosen_range_test_runs(sp_, "P", "parameter:p", {"p": (0, 3)})[0]
       and not chosen_range_test_runs(sp_, "P", "parameter:p")[0]
       and chosen_range_test_runs(dict(sp_, engine_parameters={"p": {"value": 1}}), "P", "parameter:p")[0]
       and not chosen_range_test_runs(sp_, "prior_range_p", "parameter:p", {})[0],
       "PC11 the dossier's range test: a parameter_value test runs on the emulator path only for a drawn or spec-set "
       "parameter; a generated test only for a drawn one")
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
        # round 4: before the run, inputs only. A parameter test spans the declared range (>= 2 distinct values);
        # an interaction test targets the failing benchmark's own interaction (its benchmark and component pair)
        IX = {"components": {"b"}, "benchmark": "T2", "estimand": "interaction", "pair": {"a": {"a"}, "b": {"b"}},
              "rescaled": "b"}           # the benchmark's arms rescale b given a (a11 / a10)
        PB = {"components": {"b"}, "parameters": ["eff_b"]}
        cases = [  # (name, test, effect, spec change, expected)
            ("interaction reachable, the benchmark's own pair", I(v_ok), IX, {}, True),
            ("interaction unreachable", I(v_bad), IX, {}, False),
            ("interaction at the engine's own value (scale 1)", I(v_id), IX, {}, False),
            ("the pair reversed (a rescaled given b): the arms' orientation is b given a", I(v_ok, ("a",), ("b",)),
             dict(IX, components={"a"}), {}, False),
            ("with_any reaching beyond the pair (b given a or c)", I(v_ok, ("b",), ("a", "c")), IX,
             {"options": {"components": {"a": {}, "b": {}, "c": {}}}}, False),
            ("a bundle delivering both benchmark components", I(v_ok, ("ab",), ("ab",)),
             dict(IX, pair={"a": {"ab"}, "b": {"ab"}}, components={"ab"}),
             {"options": {"components": {"ab": {}}, "engine_map": {"ab": {"a": 1, "b": 1}}}}, False),
            ("another interaction's context (b with c for an a x b benchmark)", I(v_ok, ("b",), ("c",)),
             dict(IX, pair={"a": {"a"}, "b": {"b"}}), {"options": {"components": {"a": {}, "b": {}, "c": {}}}}, False),
            ("interaction test of another benchmark", dict(I(v_ok), benchmark="T9"), IX, {}, False),
            ("interaction test for a single-component benchmark", I(v_ok), {"components": {"b"}, "benchmark": "T1",
                                                                          "estimand": "effect_vs_none"}, {}, False),
            ("no package holds every rescaled component", I(v_ok, ("a", "b"), ("c",)), IX, {"options": excl}, False),
            ("no subtraction package", I(v_ok), IX, {"options": {"packages": {"none": [], "ab": ["a", "b"]}}}, False),
            ("spec engine values: arms at spec values missing", I(v_ok), IX,
             {"engine_parameters": {"k": {"value": 0.8, "source": "s", "reason": "r", "basis": "owner_judgement"}}},
             False),
            ("parameter spanning its declared range", P("eff_b", [0.1, 0.5]), PB, {}, True),
            ("parameter, one value only", P("eff_b", [0.2]), PB, {}, False),
            ("parameter, two values not spanning the range", P("eff_b", [0.2, 0.3]), PB, {}, False),
            ("parameter spanning the stated fraction of its range, around the centre", P("eff_b", [0.25, 0.35]),
             dict(PB, range_fraction=0.25), {}, True),
            ("parameter matching the fraction's width off the centre", P("eff_b", [0.1, 0.2]),
             dict(PB, range_fraction=0.25), {}, False),
            ("parameter shifted above its range (the width matches)", P("eff_b", [0.5, 0.9]), PB, {}, False),
            ("parameter shifted below its range", P("eff_a", [0.01, 0.41]), {"components": {"a"}, "parameters": ["eff_a"]},
             {}, False),
            ("parameter not an emulator input", P("k", [0.5, 1.0]), {"components": {"b"}, "parameters": ["k"]}, {},
             False),
            ("parameter not carrying the effect", P("eff_a", [0.3, 0.7]), PB, {}, False),
            ("parameter on the direct path", P("eff_b", [0.1, 0.5]), PB, {"analysis": {"evaluation": "direct"}}, True),
        ]
        for name, t, eff, chg, want in cases:
            raw = dict(base)
            for k, v in chg.items():
                raw[k] = dict(raw.get(k) or {}, **v) if isinstance(v, dict) else v
            raw["analysis"] = dict(raw["analysis"], decision_tests=[t])
            sp = schema.Spec(raw, d)
            got, why = decision_test_runs(sp, "X", eff)
            ok(got == want, f"PC5 {name}: {got} ({why[:100]})")
        # during the run, outcomes: a real calibrated Analysis (direct model, joint calibration moves eff_a)
        from toolkit.analysis import core as CORE, model as MOD
        from toolkit.engines import benchmark_coverage as BC
        raw = dict(base, analysis=dict(base["analysis"], evaluation="direct", decision_tests=[
            P("eff_a", [0.4]), I(v_ok, ("a",), ("b",)), dict(I(v_ok), id="Y")]))
        raw["analysis"]["decision_tests"][1]["id"] = "Z"
        raw["engine_departures"] = [{"engine": "toy", "benchmark": "T1", "reason": "r", "decision_test": "X"},
                                    {"engine": "toy", "benchmark": "T2", "reason": "r", "decision_test": "Z"}]
        import yaml as _y
        cpth = os.path.join(d, "cov.yaml")
        _y.safe_dump({"engine": "toy", "absent_values": {"a": [0], "b": [0]}, "benchmarks": {
            "T2": {"covers": {"a": [1], "b": [1]}, "match": "all", "estimand": "interaction", "rescaled": "b"},
            "T1": {"covers": {"a": [1]}, "estimand": "effect_vs_none", "effect_parameters": ["eff_a"]}}},
            open(cpth, "w"))
        BC.OVERRIDES["toolkit.engines.contract:ToyEngine"] = cpth
        try:
            sp = schema.Spec(raw, d)
            A = CORE.Analysis(sp, MOD.DirectModel(sp, log=lambda *a: None), log=lambda *a: None)
            A.set_joint({"shared": ["eff_a"], "best": {"theta": {"eff_a": 0.4}, "z": {}},
                         "region": {"ranges": {"eff_a": [0.3, 0.7]}},
                         "sample": {"sample_id": "s", "names": ["eff_a"], "rows": [[0.4]]}})
            ab = next(k for k, cs in A.pk.items() if cs == {"a", "b"})
            only_b = next(k for k, cs in A.pk.items() if cs == {"b"})
            recs = [{"test_id": "X", "status": "run", "benefit_change_by_package": {ab: 1.0}, "missing_subtraction": []},
                    {"test_id": "Z", "status": "run", "benefit_change_by_package": {}, "missing_subtraction": []}]
            chk = {c["departure"]: c for c in DT.check_departures(A, recs)}
            ok(not chk["T1"]["effective"] and "identity" in chk["T1"]["why"],
               f"PC6 runtime: joint calibration moved eff_a onto the test's only value, so the departure's test is an "
               f"identity and the departure is ineffective ({chk['T1']['why'][:90]})")
            ok(not chk["T2"]["effective"] and "no benefit" in chk["T2"]["why"],
               f"PC7 runtime: an interaction test that changed no benefit of the departed components is ineffective "
               f"({chk['T2']['why'][:90]})")
            recs[1]["benefit_change_by_package"] = {only_b: 0.5}
            chk = {c["departure"]: c for c in DT.check_departures(A, recs)}
            ok(not chk["T2"]["effective"], f"PC7b runtime: a change only in packages without the benchmark's pair does "
                                           f"not count ({chk['T2']['why'][:90]})")
            recs[1]["benefit_change_by_package"] = {ab: 0.2}
            recs[1]["missing_subtraction"] = [ab]
            chk = {c["departure"]: c for c in DT.check_departures(A, recs)}
            ok(not chk["T2"]["effective"] and "subtraction" in chk["T2"]["why"],
               "PC8 runtime: a test that skipped a targeted package (missing subtraction package) is ineffective")
            recs[1]["missing_subtraction"] = []
            chk = {c["departure"]: c for c in DT.check_departures(A, recs[:1] + [dict(recs[1], status="not_run")])}
            ok(not chk["T2"]["effective"] and "not run" in chk["T2"]["why"], "PC9 runtime: a not_run test is ineffective")
            chk = {c["departure"]: c for c in DT.check_departures(A, recs)}
            ok(chk["T2"]["effective"], f"PC10 runtime: a test that ran, at a non-identity scale, changing the departed "
                                       f"benefit, is effective ({chk['T2']['why']})")
        finally:
            BC.OVERRIDES.pop("toolkit.engines.contract:ToyEngine", None)
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
