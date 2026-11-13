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
    from toolkit.engines import benchmark_coverage as BC
    return BC.spec_problems(spec, by=signer)


CHECKS = {"benchmark_eligibility": _benchmark_eligibility}


def problems(run_dir: str, spec_bytes: bytes | None, signer: str | None, spec=None) -> list:
    """Every registered check's problems. `spec`: an already loaded schema.Spec (else parsed from `spec_bytes`)."""
    from toolkit.spec import schema
    if spec is None:
        import yaml
        try:
            raw = yaml.safe_load((spec_bytes or b"").decode("utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            return [f"preconditions: the spec does not parse: {e}"]
        if not isinstance(raw, dict):
            return ["preconditions: the spec is not a mapping"]
        spec = schema.Spec(raw, run_dir)
    out = []
    for name, fn in CHECKS.items():
        try:
            out += list(fn(run_dir, spec, signer) or [])
        except Exception as e:  # noqa: BLE001 -- a check that cannot run is a problem (fail closed)
            out.append(f"precondition {name} could not be checked: {type(e).__name__}: {e}")
    return out


def _packages(raw: dict) -> list:
    from toolkit.engines import benchmark_coverage as BC
    return BC._packages(raw)


def _emulator_inputs(spec, eng) -> set:
    """The emulator's inputs: fitted parameters, drawn priors, and spec values a parameter test varies."""
    names = {p.name for p in eng.parameters() if p.kind == "fitted"}
    try:
        from toolkit.spec import priors as PRI
        names |= set(PRI.drawn(spec))
    except Exception:  # noqa: BLE001 -- unknown draws: none counted (fail closed)
        pass
    try:
        from toolkit.spec import engine_parameters as PV
        names |= set(PV.values(spec))
    except Exception:  # noqa: BLE001
        pass
    return names


def decision_test_runs(spec, test_id, effect: dict) -> tuple[bool, str]:
    """(True, "") when the test will run on this run's path and change the effect; else (False, why)."""
    raw = spec.raw
    t = next((x for x in (raw.get("analysis") or {}).get("decision_tests") or []
              if isinstance(x, dict) and str(x.get("id")) == str(test_id)), None)
    if t is None:
        return False, "is not an analysis.decision_tests id"
    comps = set(effect.get("components") or [])
    pks = _packages(raw)
    if comps and not any(comps & pk for pk in pks):
        return False, f"cannot change the effect: no allowed package holds {sorted(comps)}"
    typ = t.get("type")
    if typ == "parameter_value":
        p, params = t.get("parameter"), list(effect.get("parameters") or [])
        if p not in params:
            return False, (f"varies {p!r}, not a parameter carrying this effect "
                           f"({params or 'the coverage file declares none'})")
        try:
            eng = spec.engine()
            point = {x.name: float(x.value) for x in eng.parameters()}
        except Exception as e:  # noqa: BLE001
            return False, f"cannot be checked: the engine does not load ({type(e).__name__})"
        if p not in point:
            return False, f"varies {p!r}, which is not a parameter of this engine"
        try:
            from toolkit.spec import engine_parameters as PV
            point.update({k: float(v) for k, v in PV.values(spec).items()})
        except Exception:  # noqa: BLE001
            pass
        vals = [float(v) for v in t.get("values") or []]
        if not vals or all(abs(v - point[p]) <= 1e-12 * max(1.0, abs(point[p])) for v in vals):
            return False, f"does not change {p} (its values equal the point value)"
        if (raw.get("analysis") or {}).get("evaluation", "emulator") == "emulator" \
                and p not in _emulator_inputs(spec, eng):
            return False, (f"would not run: {p} is not an emulator input (not fitted, not a drawn prior, not a spec "
                           "value), so the test is reported not_run")
        return True, ""
    if typ == "interaction_rescale":
        tc, wa = set(t.get("components") or []), set(t.get("with_any") or [])
        if not comps & tc:
            return False, (f"does not rescale the effect: it names {sorted(comps)} only in with_any or not at all "
                           "(the rescaled increment is that of `components`)")
        b = {r.get("id"): r for r in effect.get("rows") or []}.get(t.get("benchmark"))
        if not b or not b.get("arm_or"):
            return False, (f"would not run: benchmark {t.get('benchmark')!r} has no arm odds ratios in the verified "
                           "record")
        if not any(pk & tc and pk & wa for pk in pks):
            return False, "cannot change the plan: no allowed package holds a rescaled component with one of with_any"
        return True, ""
    return False, f"is a {typ} test, which does not vary a component's effect"


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
        ok(problems(d, b"[: bad", None) and "parse" in problems(d, b"[: bad", None)[0], "PC4 unparsable bytes")
        raw = dict(s.raw)
        raw["analysis"] = dict(raw["analysis"], decision_tests=[
            {"id": "P", "type": "parameter_value", "parameter": "eff_a", "values": [0.4, 0.6], "text": "t"},
            {"id": "Q", "type": "parameter_value", "parameter": "eff_a", "values": [0.5], "text": "t"},
            {"id": "E", "type": "envelope", "values": [1.0], "text": "t"}])
        s2 = schema.Spec(raw, d)
        ok(decision_test_runs(s2, "P", {"parameters": ["eff_a"], "components": {"a"}})[0],
           "PC5 a parameter test on the effect's fitted parameter runs and changes it")
        ok(not decision_test_runs(s2, "Q", {"parameters": ["eff_a"], "components": {"a"}})[0],
           "PC6 values equal to the point value change nothing")
        ok(not decision_test_runs(s2, "E", {"parameters": ["eff_a"], "components": {"a"}})[0],
           "PC7 an envelope test does not vary an effect")
        ok(not decision_test_runs(s2, "P", {"parameters": ["eff_a"], "components": {"zz"}})[0],
           "PC8 a component in no allowed package: the test cannot change its effect")
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
