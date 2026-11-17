#!/usr/bin/env python3
"""Base outcome adjustments (F6 run 4, owner decision R3; principle 4: the trial wins unless the engine explains why).

    python3 toolkit/analysis/outcome_adjust.py --self-test

Spec (optional; checked by toolkit/spec/schema.py through problems()):

    analysis:
      outcome_adjustments:
        - {id: OA-PBO, component: net_pbo, reference: net_pyr, value: 0.73, source: "Staedke 2020, 12 months"}

In every evaluation (core.Analysis.evaluate: point, draws, stability, sensitivity, decision tests, plan variants, the
boundary check's engine side), each package holding `component` and not `reference` has every reported outcome (and
its window burden) set to `value` times that of its counterpart, the same package with `reference` in the
component's place (every `y` entry, DALY parts included, the window burdens and the grouped outputs). Emulator or engine outputs only: no engine parameter, job or row changes, so stored engine rows
are reused. Costs are left as computed. A model correction on a counterpart also bears on the package derived from it
(its copy carries `derived_from`). The comparator may not hold an adjusted component, a reference may not itself be
adjusted, and a component is adjusted at most once. An outcome_ratio decision test takes the value "engine" to remove
the base adjustment of its component (the engine's own effect)."""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
KEYS = {"id", "component", "reference", "value", "source", "text"}
REQUIRED = {"id", "component", "reference", "value", "source"}


def config(raw) -> list | None:
    return ((raw or {}).get("analysis") or {}).get("outcome_adjustments")


def counterparts(pk: dict, component: str, reference: str) -> dict:
    """{package with component: the same package with reference in its place}, for packages without reference."""
    out = {}
    for k, cs in pk.items():
        cs = set(cs)
        if component in cs and reference not in cs:
            kk = next((n for n, c2 in pk.items() if set(c2) == (cs - {component}) | {reference}), None)
            if kk is not None:
                out[k] = kk
    return out


def problems(raw, pk: dict, comps, comparator: str | None) -> list[str]:
    """Sign-off problems of analysis.outcome_adjustments (none when the field is absent)."""
    oa = config(raw)
    if oa is None:
        return []
    P = "analysis.outcome_adjustments"
    if not isinstance(oa, list) or not oa:
        return [f"{P}: a non-empty list of {{id, component, reference, value, source}}"]
    bad, seen = [], {}
    adjusted = {str(x.get("component")) for x in oa if isinstance(x, dict)}
    for n, a in enumerate(oa):
        w = f"{P}[{n}]"
        if not isinstance(a, dict) or set(a) - KEYS or not REQUIRED <= set(a):
            bad.append(f"{w}: give {{id, component, reference, value, source}} (text optional)")
            continue
        c, r, v = str(a["component"]), str(a["reference"]), a["value"]
        for k, x in (("component", c), ("reference", r)):
            if x not in comps:
                bad.append(f"{w}: {k}: unknown component {x!r}")
        if c == r:
            bad.append(f"{w}: component and reference must differ")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (math.isfinite(v) and v > 0):
            bad.append(f"{w}: value: a positive ratio (the package's outcome over its counterpart's)")
        if not str(a.get("source") or "").strip():
            bad.append(f"{w}: source: the trial the ratio comes from")
        if r in adjusted:
            bad.append(f"{w}: reference {r} is itself adjusted (no chains)")
        if c in seen:
            bad.append(f"{w}: component {c} is adjusted more than once ({seen[c]}, {a['id']})")
        seen.setdefault(c, a["id"])
        if comparator is not None and c in set(pk.get(comparator, ())):
            bad.append(f"{w}: the comparator {comparator} holds {c}: every benefit is measured from it")
        if c in comps and r in comps and c != r:
            cp = counterparts(pk, c, r)
            if not cp:
                bad.append(f"{w}: no package with {c} has a counterpart with {r} in its place")
            # Codex review of #251 P1-2: every package the adjustment touches is covered (none keeps the engine's)
            orphan = sorted(k for k, cs in pk.items() if c in set(cs) and r not in set(cs) and k not in cp)
            if orphan:
                bad.append(f"{w}: packages {orphan[:5]} hold {c} but have no counterpart with {r} in its place")
    # P1-3: one adjusted component per package (two would overwrite each other in an order-dependent way)
    both = sorted(k for k, cs in pk.items() if len(set(cs) & adjusted) > 1)
    if both:
        bad.append(f"{P}: packages {both[:5]} hold more than one adjusted component")
    # P1-5, P1-6: the burden validation and anchor read their arm's window and age-band outputs, which the adjustment
    # cannot reach (only the arm is predicted there): the arm may not hold an adjusted component
    bv = ((raw.get("validation") or {}).get("burden") or {})
    arm = bv.get("arm") or comparator
    if bv and arm in pk and set(pk[arm]) & adjusted:
        bad.append(f"{P}: the validation.burden arm {arm} holds an adjusted component")
    # P1-7: a likelihood or posterior calibration fits raw engine outcomes, not the adjusted ones
    if str((raw.get("calibration") or {}).get("method")) in ("likelihood", "posterior"):
        bad.append(f"{P}: not supported with a likelihood or posterior calibration (it fits the outcomes unadjusted)")
    return bad


def dt_problems(oc: dict, adjust) -> list[str]:
    """An outcome_ratio test's conflicts with the base adjustments (sign-off and run): with any base adjustment, the
    test must vary one of them, its component against its own reference. Any other test could move a package an
    adjustment reads (b+x through x) while the derived package (a+x) keeps its value (Codex re-review of #251)."""
    adjust = [a for a in adjust or [] if isinstance(a, dict)]
    if not adjust:
        return []
    c, r = (oc or {}).get("component"), (oc or {}).get("reference")
    if any(a.get("component") == c and a.get("reference") == r for a in adjust):
        return []
    return [f"with base outcome adjustments ({', '.join(str(a.get('id')) for a in adjust)}), an outcome_ratio test "
            f"varies a base adjustment (an adjusted component against its base reference), not {c} against {r}"]


def _scaled(x, value):
    """`x` with every number multiplied by value (nested mappings of the outputs' shape)."""
    if isinstance(x, dict):
        return {k: _scaled(v, value) for k, v in x.items()}
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return value * float(x)
    return x


def build(raw, pk: dict) -> list[dict]:
    """The adjustments the analysis applies: [{id, component, reference, value, source, pairs}] ([] when absent)."""
    return [{"id": str(a["id"]), "component": str(a["component"]), "reference": str(a["reference"]),
             "value": float(a["value"]), "source": str(a["source"]),
             "pairs": counterparts(pk, str(a["component"]), str(a["reference"]))} for a in (config(raw) or [])]


def apply(adjust: list[dict], out: dict, ids, outcomes, events=()) -> list[dict]:
    """Set each adjusted package's reported outcomes (and window burdens) in `out` to value x its counterpart's, read
    before any change; returns the model corrections copied onto derived packages."""
    import copy
    extra = []
    for i in ids:
        row = out[i]
        # every outcome entry (the DALY parts included), the window burdens and the grouped outputs (Codex review of
        # #251 P2-8), each read from the counterpart before any change
        ref = {key: {kk: copy.deepcopy(row[key][kk]) for a in adjust for kk in a["pairs"].values()
                     if kk in (row.get(key) or {})}
               for key in ("y", "burden", "y_group") if isinstance(row.get(key), dict)}
        for a in adjust:
            for k, kk in a["pairs"].items():
                for key, src in ref.items():
                    if kk in src and k in row[key]:
                        row[key][k] = _scaled(src[kk], a["value"])
    for a in adjust:
        inv = {}
        for k, kk in a["pairs"].items():
            inv.setdefault(kk, []).append(k)
        for e in events or ():
            for k in inv.get(e.get("package"), []):
                extra.append(dict(e, package=k, derived_from=e["package"]))
    return extra


def record(adjust: list[dict]) -> list[dict]:
    return [{k: a[k] for k in ("id", "component", "reference", "value", "source", "pairs")} for a in adjust or []]


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    pk = {"none": [], "a": ["a"], "b": ["b"], "a+s": ["a", "s"], "b+s": ["b", "s"], "s": ["s"]}
    ok(counterparts(pk, "a", "b") == {"a": "b", "a+s": "b+s"}, "OA1 counterparts swap one component")
    adj = build({"analysis": {"outcome_adjustments": [{"id": "x", "component": "a", "reference": "b", "value": 0.5,
                                                       "source": "t"}]}}, pk)
    out = {"u": {"y": {k: {"dalys": 10.0 + n, "other": 1.0} for n, k in enumerate(pk)},
                 "burden": {k: {"dalys": 2.0 + n} for n, k in enumerate(pk)}}}
    ev = apply(adj, out, ["u"], ["dalys"], [{"unit": "u", "package": "b+s", "kind": "clip"}])
    y = out["u"]["y"]
    ok(y["a"]["dalys"] == 0.5 * y["b"]["dalys"] and y["a+s"]["dalys"] == 0.5 * y["b+s"]["dalys"]
       and y["a"]["other"] == 0.5 * y["b"]["other"]
       and out["u"]["burden"]["a"]["dalys"] == 0.5 * out["u"]["burden"]["b"]["dalys"] and y["b"]["dalys"] == 12.0,
       "OA2 every outcome entry and window burden follows the counterpart; the counterpart itself is left")
    ok(ev == [{"unit": "u", "package": "a+s", "kind": "clip", "derived_from": "b+s"}],
       f"OA3 a counterpart's correction is copied onto the derived package ({ev})")
    ok(problems({"analysis": {}}, pk, set("abs"), "none") == [], "OA4 no field, no problem")
    # Codex review of #251 (P1-2, P1-3, P1-5..7, P2-8)
    one = lambda **kw: {"analysis": {"outcome_adjustments": [dict({"id": "x", "component": "a", "reference": "b",  # noqa: E731
                                                                   "value": 0.5, "source": "t"}, **kw)]}}
    pk2 = {"none": [], "a": ["a"], "b": ["b"], "a+s": ["a", "s"], "s": ["s"]}
    ok(any("counterpart" in e and "a+s" in e for e in problems(one(), pk2, set("abs"), "none")),
       "OA5 (P1-2) a package with the component and no counterpart is refused (every affected package is covered)")
    two = {"analysis": {"outcome_adjustments": [
        {"id": "x", "component": "a", "reference": "b", "value": 0.5, "source": "t"},
        {"id": "y", "component": "s", "reference": "t", "value": 0.2, "source": "t"}]}}
    pk3 = {"none": [], "a": ["a"], "b": ["b"], "s": ["s"], "t": ["t"], "a+s": ["a", "s"], "b+s": ["b", "s"],
           "a+t": ["a", "t"], "b+t": ["b", "t"]}
    ok(any("more than one adjusted" in e for e in problems(two, pk3, set("abst"), "none")),
       "OA6 (P1-3) a package holding two adjusted components is refused (no order-dependent overwrite)")
    raw = dict(one(), validation={"burden": {"csv": "x.csv", "arm": "a"}})
    ok(any("validation.burden" in e for e in problems(raw, pk, set("abs"), "none")),
       "OA7 (P1-5, P1-6) a burden-validation or anchor arm holding an adjusted component is refused")
    raw = dict(one(), calibration={"method": "likelihood"})
    ok(any("likelihood" in e for e in problems(raw, pk, set("abs"), "none")),
       "OA8 (P1-7) adjustments with a likelihood or posterior calibration are refused (it reads raw outcomes)")
    out = {"u": {"y": {k: {"dalys": 10.0 + n, "__yll": 8.0 + n} for n, k in enumerate(pk)},
                 "y_group": {k: {"g1": {"dalys": 4.0 + n}, "g2": {"dalys": 6.0}} for n, k in enumerate(pk)}}}
    apply(adj, out, ["u"], ["dalys"])
    yg = out["u"]["y_group"]
    ok(out["u"]["y"]["a"]["__yll"] == 0.5 * out["u"]["y"]["b"]["__yll"]
       and yg["a"]["g1"]["dalys"] == 0.5 * yg["b"]["g1"]["dalys"] and yg["a"]["g2"]["dalys"] == 3.0,
       "OA9 (P2-8) every y entry (DALY parts included) and the grouped outputs follow the counterpart")
    # Codex re-review of #251 P1-1: with base adjustments, only a test of an adjustment itself is allowed
    base = [{"id": "x", "component": "a", "reference": "b"}]
    ok(dt_problems({"component": "x", "reference": "y"}, base)
       and not dt_problems({"component": "a", "reference": "b"}, base) and not dt_problems({"component": "x",
                                                                                             "reference": "y"}, []),
       "OA10 (re-review P1-1) with base adjustments an outcome_ratio test must be an adjusted component against its "
       "base reference (a test on x against y could move b+x and leave a+x)")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
