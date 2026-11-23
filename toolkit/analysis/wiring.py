#!/usr/bin/env python3
"""Sensitivity wiring (owner, 2026-11-23): every declared input must reach a declared output.

    python3 toolkit/analysis/wiring.py --self-test

An outside benchmark found bare agents shipping dead parameters: a hardcoded constant that silently disabled a
declared sensitivity scenario, an unused helper, a burn-in that shifted a nominal setting. This check moves every input
the signed spec declares as varying to each end of its declared range, everything else at its point value, and asks
whether at least one declared output moves by more than a tolerance.

Inputs (one row each in analysis/wiring.yaml):
  parameter      every engine parameter with a range the analysis varies: fitted (engine-card range), drawn priors
                 and scenario values (declared range), calibrated parameters (estimate -/+ 1.96 SE or the posterior's
                 95% interval), the sensitivity list's own parameters (toolkit/analysis/sensitivity.py _ranges), and
                 the parameter of each parameter_value / prior_range / fitted_range decision test (its tested values'
                 ends). A calibrated parameter with no range is `not_checked`.
  cost_item      `analysis.sensitivity.cost_items` multipliers, and each item of a cost_items decision test alone;
  burden_anchor  each anchor group at its 2.5% and 97.5% ends;
  decision_test  any other decision test that changes model inputs (unit attributes, interactions, ratios, targets):
                 read from its own record (benefit_change_by_package against the point benefits), no evaluation;
                 group_rule and envelope tests act on the optimiser, not on outputs (`not_model_input`); a test that
                 did not run is `test_not_run` (its own record says why); a scenario parameter the spec holds fixed
                 by name is `held_fixed` with the spec's reason.
Outputs: every spec outcome under every package, and every package's cost, per unit. An end's change in one output
is sum_i |y_i(end) - y_i(point)| / sum_i |y_i(point)| (no cancellation across units).

Tolerance (thresholds.yaml `wiring`): `rel_tol` (float rounding); on direct evaluation the engine's own noise, measured
by one point evaluation at the next seed (a deterministic engine gives zero), times `noise_multiplier`; on the emulator
path `emulator_rel_tol` (the emulator answers for the engine). Verdicts: `wired` (some output moves above its
tolerance), `below_noise_floor` (it moves, but not above the noise), `not_wired` (nothing moves above rel_tol),
`not_checked`. The last three are a HIGH `input_not_wired` finding (findings(); toolkit/analysis/contract.py), routed
to the fix loop, unless the signed spec declares `analysis.wiring_exempt: {<input name>: <reason>}`: the row is then
`exempt` with the reason, and results.md lists it.

Evaluations: the sensitivity ends and the decision tests' evaluations are recorded as they run (Recorder) and reused;
an end no step evaluated is evaluated here, once. Extra evaluations are at most two per input plus the noise probe, and
counted (engine runs too, on direct evaluation). No batch job is added or changed: the emulator design, and so every
job hash and the reuse key, does not depend on this check.
"""
from __future__ import annotations

import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/wiring.py"
FINDING = "input_not_wired"
VERDICTS = ("wired", "below_noise_floor", "not_wired", "not_checked", "not_model_input", "test_not_run",
            "held_fixed")
FAILING = ("below_noise_floor", "not_wired", "not_checked")
PARAM_TESTS = ("parameter_value", "prior_range", "fitted_range")
OPTIMISER_TESTS = ("group_rule", "envelope")


def thresholds() -> dict:
    import yaml
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return yaml.safe_load(f)["wiring"]


def exemptions(raw) -> dict:
    return dict(((raw or {}).get("analysis") or {}).get("wiring_exempt") or {})


def validate(raw) -> list[str]:
    """`analysis.wiring_exempt`: a mapping of input names to a non-empty reason."""
    w = ((raw or {}).get("analysis") or {}).get("wiring_exempt")
    if w is None:
        return []
    if not isinstance(w, dict):
        return ["analysis.wiring_exempt: a mapping {<input name>: <reason>}"]
    return [f"analysis.wiring_exempt.{k}: the reason must be non-empty text" for k, v in w.items()
            if not (isinstance(k, str) and k and isinstance(v, str) and v.strip())]


# ------------------------------------------------------------------ measuring
def _key(theta, cost_mult=None, sampled=None) -> str:
    return json.dumps([sorted((str(k), float(v)) for k, v in (theta or {}).items()),
                       sorted((str(k), float(v)) for k, v in (cost_mult or {}).items()), sampled],
                      sort_keys=True, default=str)


def signature(A, out, cost) -> dict:
    """{output: [value per unit]}: each outcome under each package, and each package's cost."""
    sig = {f"{o}@{k}": [float(out[i]["y"][k][o]) for i in A.ids] for k in A.pk for o in A.outcomes}
    sig.update({f"cost@{k}": [float(cost[i][k]) for i in A.ids] for k in A.pk})
    return sig


def changes(base: dict, sig: dict) -> dict:
    out = {}
    for k, b in base.items():
        d = sum(abs(x - y) for x, y in zip(sig[k], b))
        out[k] = d / max(sum(abs(y) for y in b), 1e-300) if d else 0.0
    return out


class Recorder:
    """Records each full-panel evaluation's output changes against the point (sensitivity ends, decision tests), so
    the check reuses them; installed on the Analysis for those steps, then closed."""

    def __init__(self, A, out0, cost0):
        self.A, self.base, self.seen = A, signature(A, out0, cost0), {}
        self.orig, self._own = A.evaluate, "evaluate" in A.__dict__
        A.evaluate = self.evaluate

    def evaluate(self, theta, zs, ids=None, noise=None, cost_mult=None, sampled=None, **kw):
        r = self.orig(theta, zs, ids=ids, noise=noise, cost_mult=cost_mult, sampled=sampled, **kw)
        if (ids is None or list(ids) == list(self.A.ids)) and noise is None and all(v is None for v in kw.values()):
            self.seen.setdefault(_key(theta, cost_mult, sampled), changes(self.base, signature(self.A, r[0], r[2])))
        return r

    def close(self):
        if self.A.__dict__.get("evaluate") == self.evaluate:
            if self._own:
                self.A.evaluate = self.orig
            else:
                del self.A.evaluate


def _noise(A, rec, z0) -> dict:
    """Direct evaluation: the point at the next seed (one evaluation); the emulator path has no engine call here."""
    m = A.model
    if getattr(m, "kind", None) != "direct" or not isinstance(getattr(m, "seed", None), int):
        return {"method": "emulator", "rel": {}, "max_rel": None}
    s = m.seed
    m.seed = s + 1
    try:
        out, _b, cost = rec.orig(A.theta0, z0)
    finally:
        m.seed = s
    rel = changes(rec.base, signature(A, out, cost))
    return {"method": "seed_shift", "seeds": [s, s + 1], "rel": rel, "max_rel": max(rel.values(), default=0.0)}


def _verdict(chs: dict, floor: dict, th: dict) -> dict:
    """chs {end: {output: rel}} -> verdict, the outputs moved above their tolerance, and the largest change."""
    tol = lambda o: max(float(th["rel_tol"]), floor.get(o, 0.0))  # noqa: E731
    moved = sorted(({"output": o, "end": e, "rel_change": r, "tolerance": tol(o)} for e, c in chs.items()
                    for o, r in c.items() if r > tol(o)), key=lambda x: -x["rel_change"])
    big = max((r for c in chs.values() for r in c.values()), default=0.0)
    v = "wired" if moved else "below_noise_floor" if big > float(th["rel_tol"]) else "not_wired"
    return {"verdict": v, "max_rel_change": big, "moves": moved[:8], "n_outputs_moved": len(moved)}


def _param_inputs(A, dts) -> dict:
    from toolkit.analysis import sensitivity as SE
    rows = {p: {"kind": "parameter", "basis": b, "ends": [float(lo), float(hi)], "decision_tests": []}
            for p, (lo, hi, b) in {**SE._ranges(A, select=False), **SE._ranges(A)}.items()}
    for x in dts:
        p = x.get("parameter")
        if x.get("type") not in PARAM_TESTS or not isinstance(p, str) or p.startswith("z:"):
            continue
        r = rows.setdefault(p, {"kind": "parameter", "basis": f"decision test {x['test_id']} values", "ends": [],
                                "decision_tests": []})
        r["decision_tests"].append(x["test_id"])
        vals = [float(v["value"]) for v in x.get("results") or [] if isinstance(v.get("value"), (int, float))]
        if not r["ends"] and vals:
            r["ends"] = [min(vals), max(vals)] if min(vals) != max(vals) else [vals[0]]
    for p in ((A.lik or {}).get("params") or []) if A.method in ("likelihood", "posterior") else []:
        rows.setdefault(p, {"kind": "parameter", "basis": "calibrated, no range (no standard error)", "ends": [],
                            "decision_tests": []})
    return rows


def _as_run(x) -> dict:
    """A decision-test record with the type and fields it executed as (its compiled `perturb`: perturb.view), so a
    native perturb test and its alias read alike."""
    from toolkit.spec import perturb as PT
    v = PT.view(x.get("perturb")) if isinstance(x.get("perturb"), dict) else {}
    v = v if isinstance(v, dict) else {}
    return dict(x, type=v.get("type") or x.get("type"), parameter=x.get("parameter") or v.get("parameter"),
                reason=x.get("reason") or v.get("reason"))


def run(A, rec, dts, z0, ben0) -> dict:
    """The wiring artifact (kind `wiring`). `dts`: the decision-test records (DT.run_all)."""
    th = thresholds()
    ex = exemptions(A.spec.raw)
    runs0, extra, reused = getattr(A.model, "runs", 0), 0, 0
    nz = _noise(A, rec, z0)
    floor = ({o: float(th["noise_multiplier"]) * r for o, r in nz["rel"].items()} if nz["method"] == "seed_shift"
             else {o: float(th["emulator_rel_tol"]) for o in rec.base})
    dts = [_as_run(x) for x in dts]

    def measure(theta, cost_mult=None, sampled=None, zs=None):
        nonlocal extra, reused
        k = _key(theta, cost_mult, sampled)
        if k in rec.seen:
            reused += 1
        else:
            extra += 1
            rec.evaluate(theta, z0 if zs is None else zs, cost_mult=cost_mult, sampled=sampled)
        return rec.seen[k]

    rows = []
    for p, r in sorted(_param_inputs(A, dts).items()):
        chs = {}
        for v in r["ends"]:
            theta = dict(A.theta0, **{p: float(v)})
            zs = ({i: x[0] for i, x in A.calibrate(theta).items()} if A.method == "emulator_inversion" else None)
            chs[v] = measure(theta, zs=zs)
        res = _verdict(chs, floor, th) if chs else {"verdict": "not_checked", "max_rel_change": None, "moves": [],
                                                    "n_outputs_moved": 0}
        rows.append(dict(name=p, point=A.theta0.get(p), **r, **res))
    sens = A.spec["analysis"].get("sensitivity") or {}
    items = {it: [float(lo), float(hi)] for it, (lo, hi) in (sens.get("cost_items") or {}).items()}
    for x in dts:
        if x.get("type") == "cost_items" and x.get("status") != "not_run":
            for it, m in (x.get("cost_multipliers") or {}).items():
                items.setdefault(it, [float(m)])
    for it, ends in sorted(items.items()):
        chs = {m: measure(A.theta0, cost_mult={it: m}) for m in ends}
        rows.append(dict(name=it, kind="cost_item", basis="multiplier", point=1.0, ends=ends,
                         **_verdict(chs, floor, th)))
    from toolkit.analysis.burden import anchor_groups
    for g in anchor_groups(getattr(A, "anchor", None)):
        chs = {q: measure(A.theta0, sampled=g["ends"][q]) for q in (0.025, 0.975)}
        rows.append(dict(name=f"burden_anchor_{g['name']}", kind="burden_anchor", basis="published interval ends",
                         point=0.5, ends=[0.025, 0.975], **_verdict(chs, floor, th)))
    bscale = {k: max((abs(float(ben0[i][k])) for i in A.ids), default=0.0) for k in A.pk}
    for x in dts:
        t = x.get("type")
        if t in PARAM_TESTS or t in ("cost_items", "anchor_range"):
            continue
        row = {"name": str(x["test_id"]), "kind": "decision_test", "basis": f"decision test ({t})", "ends": [],
               "max_rel_change": None, "moves": [], "n_outputs_moved": 0}
        if t == "scenario_excluded":
            row.update(name=str(x.get("parameter") or x["test_id"]), kind="parameter", verdict="held_fixed",
                       basis=f"held fixed by the spec: {x.get('reason') or ''}")
        elif t in OPTIMISER_TESTS:
            row["verdict"] = "not_model_input"
        elif x.get("status") == "not_run":
            row["verdict"] = "test_not_run"
        else:
            bc = x.get("benefit_change_by_package") or {}
            chs = {"test": {f"benefit@{k}": float(v) / max(bscale.get(k, 0.0), 1e-300) if v else 0.0
                            for k, v in bc.items() if isinstance(v, (int, float))}}
            noise = max(floor.values(), default=0.0)
            row.update(_verdict(chs, {o: noise for o in chs["test"]}, th))
        rows.append(row)
    for r in rows:
        r["exempt"] = ex.get(r["name"])
    runs = getattr(A.model, "runs", 0) - runs0
    summ = {}
    for r in rows:
        summ[r["verdict"]] = summ.get(r["verdict"], 0) + 1
    return {"rows": rows, "outputs": sorted(rec.base),
            "tolerance": {"rel_tol": float(th["rel_tol"]), "noise_multiplier": float(th["noise_multiplier"]),
                          "emulator_rel_tol": float(th["emulator_rel_tol"]),
                          "noise": {k: v for k, v in nz.items() if k != "rel"}},
            "evaluations": {"reused": reused, "extra": extra, "noise_probe": int(nz["method"] == "seed_shift"),
                            "engine_runs_extra": runs, "bound": 2 * len(rows) + 1},
            "exemptions": [{"name": k, "reason": v, "used": any(r["name"] == k for r in rows)}
                           for k, v in sorted(ex.items())],
            "summary": summ,
            "not_wired": [r["name"] for r in rows if r["verdict"] in FAILING and not r["exempt"]]}


def findings(rel: str, data) -> list[dict]:
    """One HIGH `input_not_wired` per failing input without an exemption; an unreadable payload is one too."""
    def f(claim, key):
        return {"kind": FINDING, "severity": "HIGH", "stage": "ANALYZE", "target_stage": "MODEL", "claim": claim,
                "key": key, "scope_declarable": False, "source": PRODUCER}
    rows = (data or {}).get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return [f(f"{rel}: rows unreadable, so no input is shown to reach an output", rel)]
    out = []
    for r in rows:
        v, nm = r.get("verdict"), str(r.get("name"))
        if v not in VERDICTS:
            out.append(f(f"{rel}: input {nm} has an unknown wiring verdict {v!r}", nm))
        elif v in FAILING and not (isinstance(r.get("exempt"), str) and r["exempt"].strip()):
            why = {"not_wired": "moves no declared output", "not_checked": "has no declared range to move it over",
                   "below_noise_floor": "moves the declared outputs by less than the engine's own noise"}[v]
            out.append(f(f"Input {nm} ({r.get('kind')}, {r.get('basis')}) {why} at either end of its range: it may "
                         "be disconnected from the model (a hard-coded value, an unused branch). Wire it, remove it "
                         "from the spec, or declare analysis.wiring_exempt with the reason.", nm))
    return out


def results_lines(data) -> list[str]:
    """results.md: what was checked, in words, and every exemption with its reason."""
    rows = data["rows"]
    bad = [r["name"] for r in rows if r["verdict"] in FAILING and not r["exempt"]]
    L = ["## Input wiring", "", "Every declared input was moved to each end of its range, the others at their point "
         "values, and its effect on the outcomes and costs recorded (analysis/wiring.yaml). "
         + ("Inputs that move no declared output: " + ", ".join(f"`{n}`" for n in bad) + "."
            if bad else "Every input that was checked moves at least one declared output.")]
    ex = [r for r in rows if r["exempt"]]
    if ex:
        L += ["", "Inputs exempt from the check (analysis.wiring_exempt in the signed spec):", ""]
        L += [f"- `{r['name']}` ({r['verdict'].replace('_', ' ')}): {r['exempt']}" for r in ex]
    return L + [""]


# ------------------------------------------------------------------ self-test
def _toy(dead=(), tiny=(), sd=0.0, seed=1):
    """A fake analysis over a closed-form engine: outcome y = base * (1 - eff * a) * (1 + tiny_p ...); `dead`
    parameters are declared with a range and never read; `tiny` ones move y by 1e-5 of their value; `sd` adds a
    seed-dependent shift (a stochastic engine run at a fixed seed)."""
    class Spec(dict):
        raw = None

    class Model:
        kind, runs = "direct", 0

    params = {"eff": (0.3, 0.7, 0.5), **{p: (0.0, 1.0, 0.5) for p in (*dead, *tiny)}}
    A = type("A", (), {})()
    A.spec = Spec(analysis={"sensitivity": {"parameters": "fitted", "cost_items": {"ca": [0.8, 1.2]}}})
    A.spec.raw = {"analysis": dict(A.spec["analysis"])}
    A.fr = params
    A.pr, A.method, A.lik, A.anchor, A.joint = {}, "none", None, None, None
    A.model = Model()
    A.model.seed = seed
    A.theta0 = {k: v[2] for k, v in params.items()}
    A.ids, A.pk, A.outcomes, A.comp = ["u0", "u1", "u2"], {"none": set(), "a": {"a"}}, ["cases"], "none"
    A.calibrate = lambda theta: {i: (0.0, False) for i in A.ids}

    def evaluate(theta, zs, ids=None, noise=None, cost_mult=None, sampled=None, **kw):
        s = A.model.seed
        A.model.runs += len(A.ids) * len(A.pk)
        out, cost = {}, {}
        for n, i in enumerate(A.ids):
            base = 100.0 * (n + 1) * (1 + sd * ((s * 7919 + 17) % 1000 / 1000.0 - 0.5))
            f = 1 + sum(1e-5 * theta[p] for p in tiny)
            out[i] = {"y": {k: {"cases": base * f * (1 - theta["eff"] * ("a" in A.pk[k]))} for k in A.pk}}
            cost[i] = {k: 10.0 * (n + 1) * ("a" in A.pk[k]) * (cost_mult or {}).get("ca", 1.0) for k in A.pk}
        ben = {i: {k: out[i]["y"]["none"]["cases"] - out[i]["y"][k]["cases"] for k in A.pk} for i in A.ids}
        return out, ben, cost
    A.evaluate = evaluate
    out0, ben0, cost0 = evaluate(A.theta0, None)
    return A, out0, ben0, cost0


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def check(A, out0, ben0, cost0, dts=(), pre=None):
        rec = Recorder(A, out0, cost0)
        if pre:
            pre(A)
        try:
            return run(A, rec, list(dts), None, ben0)
        finally:
            rec.close()

    by = lambda W: {r["name"]: r for r in W["rows"]}  # noqa: E731
    A, o, b, c = _toy()
    W = check(A, o, b, c)
    ok(by(W)["eff"]["verdict"] == "wired" and by(W)["ca"]["verdict"] == "wired" and findings("w", W) == []
       and W["evaluations"]["extra"] == 4 and W["evaluations"]["noise_probe"] == 1,
       f"WR1 a wired parameter and cost item pass, with every end evaluated here and counted: {W['evaluations']}")
    A, o, b, c = _toy(dead=("decay",))
    W = check(A, o, b, c)
    f = findings("analysis/wiring.yaml", W)
    ok(by(W)["decay"]["verdict"] == "not_wired" and by(W)["decay"]["max_rel_change"] == 0.0
       and [(x["kind"], x["key"], x["severity"], x["scope_declarable"]) for x in f]
       == [(FINDING, "decay", "HIGH", False)] and W["not_wired"] == ["decay"],
       f"WR2 a declared parameter the engine never reads is a HIGH input_not_wired finding: {f}")
    A.spec.raw["analysis"]["wiring_exempt"] = {"decay": "read only by the next engine version (toy)"}
    W = check(A, o, b, c)
    L = "\n".join(results_lines(W))
    ok(findings("w", W) == [] and by(W)["decay"]["exempt"] and "`decay`" in L and "next engine version" in L
       and W["exemptions"] == [{"name": "decay", "reason": "read only by the next engine version (toy)",
                                "used": True}],
       "WR3 an exempt dead parameter passes, and results.md lists it with its reason")
    A, o, b, c = _toy(tiny=("drift",), sd=0.05)
    W = check(A, o, b, c)
    nz = W["tolerance"]["noise"]["max_rel"]
    ok(by(W)["drift"]["verdict"] == "below_noise_floor" and by(W)["eff"]["verdict"] == "wired" and nz > 0
       and [x["key"] for x in findings("w", W)] == ["drift"],
       f"WR4 a stochastic engine: an effect below its seed-to-seed noise ({nz:.2g}) is a finding, a large one passes")
    A, o, b, c = _toy(tiny=("drift",))
    W = check(A, o, b, c)
    ok(by(W)["drift"]["verdict"] == "wired" and W["tolerance"]["noise"]["max_rel"] == 0.0,
       "WR5 a deterministic engine: the same small effect is above the numerical tolerance (noise zero)")
    A, o, b, c = _toy()

    def sens_like(A):          # the sensitivity step's evaluations, made before the check
        for v in (0.3, 0.7):
            A.evaluate(dict(A.theta0, eff=v), None)
        for m in (0.8, 1.2):
            A.evaluate(A.theta0, None, cost_mult={"ca": m})
    W = check(A, o, b, c, pre=sens_like)
    ok(W["evaluations"]["extra"] == 0 and W["evaluations"]["reused"] == 4 and W["evaluations"]["engine_runs_extra"]
       == 6, f"WR6 evaluations the sensitivity step made are reused; only the noise probe runs: {W['evaluations']}")
    dts = [{"test_id": "DT1", "type": "parameter_value", "parameter": "k2", "status": "run",
            "results": [{"value": 0.1}, {"value": 0.9}]},
           {"test_id": "DT2", "type": "unit_attribute", "status": "run", "benefit_change_by_package": {"a": 0.0}},
           {"test_id": "DT3", "type": "envelope", "status": "run"},
           {"test_id": "DT4", "type": "outcome_ratio", "status": "not_run"}]
    A, o, b, c = _toy()
    A.theta0["k2"] = 0.5
    W = check(A, o, b, c, dts=dts)
    v = {n: r["verdict"] for n, r in by(W).items()}
    ok(v["k2"] == "not_wired" and by(W)["k2"]["ends"] == [0.1, 0.9] and v["DT2"] == "not_wired"
       and v["DT3"] == "not_model_input" and v["DT4"] == "test_not_run"
       and sorted(x["key"] for x in findings("w", W)) == ["DT2", "k2"],
       f"WR7 decision-test inputs: a tested parameter at its tested ends, a unit-attribute test from its record: {v}")
    ok(len(findings("w", {"rows": "x"})) == 1 and findings("w", {"rows": [{"name": "p", "verdict": "maybe"}]})[0][
        "kind"] == FINDING, "WR8 fail closed: unreadable rows or an unknown verdict is a finding")
    ok(validate({"analysis": {"wiring_exempt": {"p": " "}}}) and validate({"analysis": {"wiring_exempt": ["p"]}})
       and validate({"analysis": {"wiring_exempt": {"p": "why"}}}) == [],
       "WR9 analysis.wiring_exempt: a mapping of names to a non-empty reason")
    import tempfile
    from toolkit.analysis import batch as Bt
    from toolkit.spec import schema
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d, n=12)
        j0 = [Bt.job_sha(j) for j in Bt.design(schema.load(p))[1]]
        import yaml
        r = yaml.safe_load(open(p))
        r["analysis"]["wiring_exempt"] = {"eff_b": "toy"}
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        s = schema.load(p)
        ok(schema.validate(s) == [] and [Bt.job_sha(j) for j in Bt.design(s)[1]] == j0,
           "WR10 an exemption validates and changes no batch job hash (the reuse key)")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
