#!/usr/bin/env python3
"""Run preconditions: one registry every signing path and every gate before compute calls.

    python3 toolkit/spec/preconditions.py --self-test

Each round of review of the evidence dossier (#218) found another path that checked part of the state or trusted a
stored record; the engine-eligibility work (#215) found the same. So each precondition is one function that
recomputes everything from the run's files, registered here, and every path calls `problems()`:
toolkit/spec/signoff.py sign and status, scripts/decision_brief.py sign, agents/stage_machine.py sign and its resume
check, cloud/run_cluster.py's launch and relaunch checks. A check that raises is a problem (fail closed).

    problems(run_dir, spec_bytes, signer, **ctx) -> [str]
        signer: "sign" (a signer, before the record exists), "status" (a signed run: the record is compared), or
        "launch" / "resume" (as status). ctx: read (rel -> bytes | None, e.g. from a launch archive), engine
        ((ref, config): the engine chosen), record (a sign-off record to compare instead of the run's).

    decision_test_runs(spec, test_id, effect, drawn=None) -> (ok, why, values)
        Whether decision test `test_id` executes on this run's evaluation path and varies `effect`
        ("parameter:<name>"); `drawn`: {parameter: (lo, hi)} the uncertainty draws vary.

Nothing here names a disease.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHECKS: dict = {}               # name -> fn(run_dir, spec_bytes, signer, **ctx) -> [str]
SIGNERS = ("sign", "status", "launch", "resume")


def register(name: str, fn) -> None:
    CHECKS[name] = fn


def _load_builtin() -> None:
    from toolkit.spec import evidence_dossier as ED
    ED._register()


def problems(run_dir: str | None, spec_bytes: bytes | None, signer: str, **ctx) -> list[str]:
    """Every registered precondition's problems for this run (empty: none). Fail closed: a check that raises is a
    problem, and so is an unknown signer."""
    if signer not in SIGNERS:
        return [f"preconditions: unknown signer {signer!r} (one of {SIGNERS})"]
    _load_builtin()
    out = []
    for name, fn in sorted(CHECKS.items()):
        try:
            out += list(fn(run_dir, spec_bytes, signer, **ctx) or [])
        except Exception as e:  # noqa: BLE001
            out.append(f"{name}: the check failed ({type(e).__name__}: {e})")
    return out


def refuse(run_dir: str | None, spec_bytes: bytes | None, signer: str, **ctx) -> None:
    """problems(), raised as one ValueError when there are any."""
    bad = problems(run_dir, spec_bytes, signer, **ctx)
    if bad:
        raise ValueError("spec not signable (preconditions): " + "; ".join(bad[:12])
                         + (f"; and {len(bad) - 12} more" if len(bad) > 12 else ""))


def decision_test_runs(spec: dict, test_id: str, effect: str, drawn: dict | None = None) -> tuple:
    """(ok, why, values): whether `test_id` executes on this run's path and varies `effect`, and the values it sets.
    A generated prior_range_/fitted_range_<p> test exists only for a parameter the draws vary (`drawn`), over its draw
    range. A declared parameter_value test varies its own parameter; on the emulator path only a drawn parameter or
    one the spec sets (engine_parameters) is an emulator input, so a test of any other does not execute."""
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
    ev = an.get("evaluation")
    eps = spec.get("engine_parameters") if isinstance(spec.get("engine_parameters"), dict) else {}
    if ev == "emulator" and name not in drawn and name not in eps:
        return False, (f"does not execute on the emulator path: {name} is neither drawn nor set by engine_parameters, "
                       "so it is not an emulator input"), list(t.get("values") or [])
    return True, "", list(t.get("values") or [])


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    spec = {"analysis": {"evaluation": "emulator", "decision_tests": [
        {"id": "P", "type": "parameter_value", "parameter": "p", "values": [1, 2]},
        {"id": "I", "type": "interaction_rescale", "components": ["a"], "values": [1]}]}}
    ok(decision_test_runs(spec, "P", "parameter:p", {"p": (0, 3)})[0]
       and not decision_test_runs(spec, "P", "parameter:p")[0]
       and decision_test_runs(dict(spec, engine_parameters={"p": {"value": 1}}), "P", "parameter:p")[0]
       and decision_test_runs({"analysis": {"evaluation": "direct", **{k: v for k, v in spec["analysis"].items()
                                                                       if k != "evaluation"}}}, "P", "parameter:p")[0],
       "PC1 a parameter_value test executes on the emulator path only for a drawn or spec-set parameter; on the direct "
       "path always")
    ok(not decision_test_runs(spec, "I", "parameter:p")[0] and not decision_test_runs(spec, "Z", "parameter:p")[0]
       and decision_test_runs(spec, "prior_range_p", "parameter:p", {"p": (0.2, 0.4)}) == (True, "", [0.2, 0.4])
       and not decision_test_runs(spec, "prior_range_p", "parameter:p", {})[0]
       and not decision_test_runs(spec, "P", "unit_attribute:x")[0],
       "PC2 a test of another quantity, an unknown id, a generated test of an undrawn parameter and an attribute are "
       "refused")
    saved = dict(CHECKS)
    try:
        CHECKS.clear()
        register("boom", lambda *a, **k: 1 / 0)
        register("fine", lambda *a, **k: ["one"])
        import toolkit.spec.evidence_dossier as ED
        orig, ED._register = ED._register, lambda: None
        try:
            got = problems(None, b"", "sign")
            unknown = problems(None, b"", "nobody")
        finally:
            ED._register = orig
        ok(any("boom" in g and "failed" in g for g in got) and "one" in got and unknown and "unknown signer" in unknown[0],
           f"PC3 every registered check runs; one that raises is a problem (fail closed); an unknown signer is refused: "
           f"{got} {unknown}")
    finally:
        CHECKS.clear()
        CHECKS.update(saved)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
