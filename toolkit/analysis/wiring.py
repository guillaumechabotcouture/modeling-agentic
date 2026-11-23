#!/usr/bin/env python3
"""Sensitivity wiring (owner, 2026-11-23): every declared input should reach a declared output. ADVISORY (lead,
after Codex round 2 of #365): findings are MEDIUM, scope-declarable, carry `would_block: true` and never fail a run, so
false positives can be counted across real runs before the check blocks.

    python3 toolkit/analysis/wiring.py --self-test

An outside benchmark found bare agents shipping dead parameters: a hardcoded constant that silently disabled a
declared sensitivity scenario, an unused helper, a burn-in that shifted a nominal setting. This check moves every input
the signed spec declares as varying to each end of its declared range, everything else at its point value, and asks
whether at least one declared output moves by more than a tolerance.

Inputs (one row each in analysis/wiring.yaml):
  parameter      every engine parameter with a range the analysis varies: fitted (engine-card range), drawn priors
                 and scenario values (declared range), calibrated parameters (estimate -/+ 1.96 SE or the posterior's
                 95% interval), the sensitivity list's own parameters (toolkit/analysis/sensitivity.py _ranges), and
                 the parameter of each parameter_value / prior_range / fitted_range decision test: its tested values'
                 ends, or, for a test with companion settings (`with`), each tested value against the companions alone.
                 A calibrated parameter with no range is `not_checked`; one known only from tests that did not run is
                 `test_not_run`.
  cost_item      multipliers from `analysis.sensitivity.cost_items`, `uncertainty.sources` costs items, and each item of
                 a cost_items decision test alone;
  burden_anchor  each anchor group at its 2.5% and 97.5% ends;
  decision_test  any other decision test that changes model inputs (unit attributes, interactions, ratios, targets):
                 read from its own record (benefit_change_by_package against the point benefits), no evaluation;
                 group_rule, envelope and boundary_engine tests act on the optimiser or compare the emulator with the
                 engine (`not_model_input`); a test that did not run is `test_not_run`; a scenario parameter the spec
                 holds fixed by name is `held_fixed` with the spec's reason.
An end equal to the point value (a range like [1, 1]) perturbs nothing and is dropped; an input with no other end is
`not_perturbed`. An input whose effect could only show through packages the run pruned (spec components in no
package) and that moved nothing is `not_applicable`.
Outputs: every spec outcome under every package, and every package's cost, per unit. An end's change in one output
is sum_i |y_i(end) - y_i(point)| / sum_i |y_i(point)| (no cancellation across units).

Tolerance (thresholds.yaml `wiring`): `rel_tol` (float rounding); on direct evaluation, per output, the engine's own
noise (one point evaluation at the next seed: zero for a deterministic engine) times `noise_multiplier`. On the
emulator path the check reads the emulator, which can show a small spurious effect for an input the engine ignores, so
only a change above `emulator_wired_rel` is `wired`; anything less is `emulator_unverified` (never certified either
way). Failing verdicts (`below_noise_floor`, `not_wired`, `not_checked`, `emulator_unverified`) give an advisory
`input_not_wired` finding (findings(); toolkit/analysis/contract.py) unless the signed spec exempts the input:
`analysis.wiring_exempt: {parameters | cost_items | burden_anchors | decision_tests: {<name>: <reason>}}` (one
namespace per input kind, so a reason for one kind never covers another). results.md lists every exemption, used or
not. A check that fails to run records `error` and gives one advisory finding; the analysis continues.

Evaluations: the sensitivity ends and the decision tests' evaluations are recorded as they run (Recorder) and reused;
an end no step evaluated is evaluated here, once. Extra full-panel evaluations (`evaluations.bound`: two per input plus
the noise probe; each is one engine run per unit and package on direct evaluation) and engine runs are counted. No
batch job is added or changed: the emulator design, and so every job hash and the reuse key, does not depend on this
check. Open (declared): the reuse key ignores calibrated unit values and outcome adjustments; a decision test read from
its record is judged on primary benefits only; several tested values of one parameter beyond its range are not all
checked; posterior-calibration parameters and `z:` tests are evaluated with the point calibration.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/wiring.py"
FINDING = "input_not_wired"
VERDICTS = ("wired", "below_noise_floor", "not_wired", "not_checked", "emulator_unverified", "not_perturbed",
            "not_applicable", "not_model_input", "test_not_run", "held_fixed")
FAILING = ("below_noise_floor", "not_wired", "not_checked", "emulator_unverified")
PARAM_TESTS = ("parameter_value", "prior_range", "fitted_range")
NOT_INPUT_TESTS = ("group_rule", "envelope", "boundary_engine")
NS = {"parameter": "parameters", "cost_item": "cost_items", "burden_anchor": "burden_anchors",
      "decision_test": "decision_tests"}


def thresholds() -> dict:
    import yaml
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return yaml.safe_load(f)["wiring"]


def exemptions(raw) -> dict:
    """{namespace: {name: reason}} (names as text)."""
    w = ((raw or {}).get("analysis") or {}).get("wiring_exempt") or {}
    return {ns: {str(k): v for k, v in (w.get(ns) or {}).items()} for ns in NS.values() if isinstance(w, dict)}


def validate(raw) -> list[str]:
    """`analysis.wiring_exempt`: {namespace: {name: non-empty reason}}, namespaces as NS."""
    w = ((raw or {}).get("analysis") or {}).get("wiring_exempt")
    if w is None:
        return []
    if not isinstance(w, dict):
        return [f"analysis.wiring_exempt: a mapping of {', '.join(NS.values())} to {{<name>: <reason>}}"]
    bad = [f"analysis.wiring_exempt.{k}: unknown namespace (known: {', '.join(NS.values())})" for k in w
           if k not in NS.values()]
    for ns in NS.values():
        m = w.get(ns)
        if m is None:
            continue
        if not isinstance(m, dict):
            bad.append(f"analysis.wiring_exempt.{ns}: a mapping {{<name>: <reason>}}")
            continue
        bad += [f"analysis.wiring_exempt.{ns}.{k}: the reason must be non-empty text" for k, v in m.items()
                if not (str(k) and isinstance(v, str) and v.strip())]
    return bad


# ------------------------------------------------------------------ measuring
def _key(theta, cost_mult=None, sampled=None) -> str:
    return json.dumps([sorted((str(k), float(v)) for k, v in (theta or {}).items()),
                       sorted((str(k), float(v)) for k, v in (cost_mult or {}).items()), sampled],
                      sort_keys=True, default=str)


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-12 * max(1.0, abs(float(b)))
    except (TypeError, ValueError):
        return False


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
    the check reuses them; installed on the Analysis for those steps, then closed. Recording never fails the
    evaluation it observes (a problem is kept in `errors`)."""

    def __init__(self, A, out0, cost0):
        self.A, self.base, self.seen, self.errors = A, signature(A, out0, cost0), {}, []
        self.orig, self._own = A.evaluate, "evaluate" in A.__dict__
        A.evaluate = self.evaluate

    def evaluate(self, theta, zs, ids=None, noise=None, cost_mult=None, sampled=None, **kw):
        r = self.orig(theta, zs, ids=ids, noise=noise, cost_mult=cost_mult, sampled=sampled, **kw)
        try:
            if (ids is None or list(ids) == list(self.A.ids)) and noise is None \
                    and all(v is None for v in kw.values()):
                self.seen.setdefault(_key(theta, cost_mult, sampled), changes(self.base, signature(self.A, r[0], r[2])))
        except Exception as e:  # noqa: BLE001 -- advisory: the evaluation stands, the record says what failed
            self.errors.append(f"{type(e).__name__}: {e}"[:300])
        return r

    def close(self):
        if self.A.__dict__.get("evaluate") == self.evaluate:
            if self._own:
                self.A.evaluate = self.orig
            else:
                del self.A.evaluate


def start(A, out0, cost0):
    """A Recorder, or None when it cannot start (the check then records the error; the analysis goes on)."""
    try:
        return Recorder(A, out0, cost0)
    except Exception:  # noqa: BLE001 -- advisory
        return None


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


def _verdict(chs: dict, floor: dict, th: dict, emulator: bool = False) -> dict:
    """chs {end: {output: rel}} -> verdict, the outputs moved above their tolerance, and the largest change. `floor`
    is per output."""
    rt = float(th["rel_tol"])
    tol = (lambda o: float(th["emulator_wired_rel"])) if emulator else (lambda o: max(rt, floor.get(o, 0.0)))  # noqa
    moved = sorted(({"output": o, "end": str(e), "rel_change": r, "tolerance": tol(o)} for e, c in chs.items()
                    for o, r in c.items() if r > tol(o)), key=lambda x: -x["rel_change"])
    big = max((r for c in chs.values() for r in c.values()), default=0.0)
    v = ("wired" if moved else "emulator_unverified" if emulator
         else "below_noise_floor" if big > rt else "not_wired")
    return {"verdict": v, "max_rel_change": big, "moves": moved[:8], "n_outputs_moved": len(moved)}


def dt_floor(floor: dict, primary: str, comp: str, k: str) -> float:
    """A decision test's benefit@k (comparator minus package k on the primary outcome): the noise of those two outputs
    only, never of an unrelated one."""
    return max(floor.get(f"{primary}@{k}", 0.0), floor.get(f"{primary}@{comp}", 0.0))


def _param_inputs(A, dts) -> dict:
    from toolkit.analysis import sensitivity as SE
    rows = {p: {"kind": "parameter", "basis": b, "ends": [float(lo), float(hi)], "with_cases": [],
                "decision_tests": [], "tests_not_run": []}
            for p, (lo, hi, b) in {**SE._ranges(A, select=False), **SE._ranges(A)}.items()}
    for x in dts:
        p = x.get("parameter")
        if x.get("type") not in PARAM_TESTS or not isinstance(p, str) or p.startswith("z:"):
            continue
        r = rows.setdefault(p, {"kind": "parameter", "basis": f"decision test {x['test_id']} values", "ends": [],
                                "with_cases": [], "decision_tests": [], "tests_not_run": []})
        r["decision_tests"].append(x["test_id"])
        if x.get("status") == "not_run":
            r["tests_not_run"].append(x["test_id"])
            continue
        vals = [float(v["value"]) for v in x.get("results") or []
                if isinstance(v, dict) and isinstance(v.get("value"), (int, float)) and not isinstance(v["value"], bool)]
        comp = {str(k): float(v) for k, v in (x.get("with") or {}).items()}
        if comp:            # a conditional test: its values against the companion settings alone
            r["with_cases"].append({"test": x["test_id"], "with": comp, "values": vals})
        elif not r["ends"] and vals:
            r["ends"] = [min(vals), max(vals)] if min(vals) != max(vals) else [vals[0]]
    for p in ((A.lik or {}).get("params") or []) if A.method in ("likelihood", "posterior") else []:
        rows.setdefault(p, {"kind": "parameter", "basis": "calibrated, no range (no standard error)", "ends": [],
                            "with_cases": [], "decision_tests": [], "tests_not_run": []})
    return rows


def _as_run(x) -> dict:
    """A decision-test record with the type and fields it executed as (its compiled `perturb`: perturb.view), so a
    native perturb test and its alias read alike."""
    from toolkit.spec import perturb as PT
    v = PT.view(x.get("perturb")) if isinstance(x.get("perturb"), dict) else {}
    v = v if isinstance(v, dict) else {}
    return dict(x, type=v.get("type") or x.get("type"), parameter=x.get("parameter") or v.get("parameter"),
                reason=x.get("reason") or v.get("reason"))


def _pruned(A) -> list:
    """Spec components that no package of this run holds (pruned, e.g. by options.packages.within_envelope)."""
    comps = list(((getattr(A.spec, "raw", None) or {}).get("options") or {}).get("components") or {})
    have = set().union(*[set(v) for v in A.pk.values()]) if A.pk else set()
    return sorted(str(c) for c in comps if c not in have)


def run(A, rec, dts, z0, ben0) -> dict:
    """The wiring artifact (kind `wiring`). `dts`: the decision-test records (DT.run_all)."""
    th = thresholds()
    ex = exemptions(A.spec.raw)
    emu = getattr(A.model, "kind", None) == "emulator"
    runs0, extra, reused = getattr(A.model, "runs", 0), 0, 0
    nz = _noise(A, rec, z0)
    floor = {o: float(th["noise_multiplier"]) * r for o, r in nz["rel"].items()}
    dts = [_as_run(x) for x in dts]
    pruned = _pruned(A)

    def zs_at(theta):
        return {i: x[0] for i, x in A.calibrate(theta).items()} if A.method == "emulator_inversion" else z0

    def measure(theta, cost_mult=None, sampled=None, recal=False):
        nonlocal extra, reused
        k = _key(theta, cost_mult, sampled)
        if k in rec.seen:
            reused += 1
        else:
            extra += 1
            rec.evaluate(theta, zs_at(theta) if recal else z0, cost_mult=cost_mult, sampled=sampled)
        return rec.seen[k]

    def sig_at(theta):
        nonlocal extra
        extra += 1
        out, _b, cost = rec.orig(theta, zs_at(theta))
        return signature(A, out, cost)

    def blank(**kw):
        return dict({"max_rel_change": None, "moves": [], "n_outputs_moved": 0}, **kw)

    rows = []
    for p, r in sorted(_param_inputs(A, dts).items()):
        p0 = A.theta0.get(p)
        ends = [v for v in r["ends"] if p0 is None or not _same(v, p0)]
        chs = {v: measure(dict(A.theta0, **{p: float(v)}), recal=True) for v in ends}
        tried = bool(r["ends"])
        for wc in r["with_cases"]:
            b_theta = dict(A.theta0, **wc["with"])
            vals = [v for v in wc["values"] if not _same(v, b_theta.get(p))]
            tried = tried or bool(wc["values"])
            if vals:
                s0 = sig_at(b_theta)
                for v in vals:
                    chs[f"{v:g} with {wc['test']}"] = changes(s0, sig_at(dict(b_theta, **{p: float(v)})))
        if chs:
            res = _verdict(chs, floor, th, emu)
        elif tried:
            res = blank(verdict="not_perturbed")
        elif r["tests_not_run"]:
            res = blank(verdict="test_not_run")
        else:
            res = blank(verdict="not_checked")
        rows.append(dict(name=p, point=p0, **r, **res))
    sens = A.spec["analysis"].get("sensitivity") or {}
    items = {}
    for it, (lo, hi) in (sens.get("cost_items") or {}).items():
        items[it] = ("analysis.sensitivity.cost_items", [float(lo), float(hi)])
    for src in ((A.spec.raw.get("uncertainty") or {}).get("sources") or []):
        if isinstance(src, dict) and src.get("kind") == "costs":
            for it, rg in (src.get("items") or {}).items():
                if str(it) not in {str(k) for k in items}:
                    items[it] = ("uncertainty.sources costs", [float(rg[0]), float(rg[1])])
    for x in dts:
        if x.get("type") == "cost_items" and x.get("status") != "not_run":
            for it, m in (x.get("cost_multipliers") or {}).items():
                if str(it) not in {str(k) for k in items}:
                    items[it] = (f"decision test {x.get('test_id')}", [float(m)])
    comp_of = {str(c.get("id")): c.get("component") for c in (getattr(A, "alloc", None) or {}).get("cost_items") or []}
    for it, (basis, rng) in sorted(items.items(), key=lambda kv: str(kv[0])):
        ends = [m for m in rng if not _same(m, 1.0)]
        if comp_of.get(str(it)) is not None and str(comp_of[str(it)]) in pruned:
            res = blank(verdict="not_applicable")
            basis += f"; its component {comp_of[str(it)]} is in no package of this run"
        elif not ends:
            res = blank(verdict="not_perturbed")
        else:
            res = _verdict({m: measure(A.theta0, cost_mult={it: m}) for m in ends}, floor, th, emu)
        rows.append(dict(name=str(it), kind="cost_item", basis=f"multiplier ({basis})", point=1.0, ends=rng, **res))
    from toolkit.analysis.burden import anchor_groups
    for g in anchor_groups(getattr(A, "anchor", None)):
        chs = {q: measure(A.theta0, sampled=g["ends"][q]) for q in (0.025, 0.975)}
        rows.append(dict(name=f"burden_anchor_{g['name']}", kind="burden_anchor", basis="published interval ends",
                         point=0.5, ends=[0.025, 0.975], **_verdict(chs, floor, th, emu)))
    bscale = {k: max((abs(float(ben0[i][k])) for i in A.ids), default=0.0) for k in A.pk}
    for x in dts:
        t = x.get("type")
        if t in PARAM_TESTS or t in ("cost_items", "anchor_range"):
            continue
        row = blank(name=str(x["test_id"]), kind="decision_test", basis=f"decision test ({t})", ends=[])
        if t == "scenario_excluded":
            row.update(name=str(x.get("parameter") or x["test_id"]), kind="parameter", verdict="held_fixed",
                       basis=f"held fixed by the spec: {x.get('reason') or ''}")
        elif t in NOT_INPUT_TESTS:
            row["verdict"] = "not_model_input"
        elif x.get("status") == "not_run":
            row["verdict"] = "test_not_run"
        else:
            bc = x.get("benefit_change_by_package") or {}
            chs = {"test": {f"benefit@{k}": float(v) / max(bscale.get(k, 0.0), 1e-300) if v else 0.0
                            for k, v in bc.items() if isinstance(v, (int, float)) and k in A.pk}}
            fl = {f"benefit@{k}": dt_floor(floor, A.outcomes[0], A.comp, k) for k in A.pk}
            row.update(_verdict(chs, fl, th, emu))
        rows.append(row)
    for r in rows:
        if pruned and r["kind"] == "parameter" and r["verdict"] in FAILING:
            r.update(verdict="not_applicable", measured_verdict=r["verdict"],
                     basis=f"{r['basis']}; components {', '.join(pruned)} are in no package of this run, so an effect "
                           "through them cannot be observed")
        r["exempt"] = ex.get(NS[r["kind"]], {}).get(str(r["name"]))
    runs = getattr(A.model, "runs", 0) - runs0
    summ = {}
    for r in rows:
        summ[r["verdict"]] = summ.get(r["verdict"], 0) + 1
    named = {(NS[r["kind"]], str(r["name"])) for r in rows}
    return {"rows": rows, "outputs": sorted(rec.base), "advisory": True, "pruned_components": pruned,
            "recorder_errors": list(rec.errors),
            "tolerance": {"rel_tol": float(th["rel_tol"]), "noise_multiplier": float(th["noise_multiplier"]),
                          "emulator_wired_rel": float(th["emulator_wired_rel"]), "emulator": emu,
                          "noise": {k: v for k, v in nz.items() if k != "rel"}},
            "evaluations": {"reused": reused, "extra": extra, "noise_probe": int(nz["method"] == "seed_shift"),
                            "engine_runs_extra": runs, "bound": 2 * len(rows) + 1,
                            "bound_counts": "full-panel evaluations (one engine run per unit and package each)"},
            "exemptions": [{"namespace": ns, "name": k, "reason": v, "used": (ns, k) in named}
                           for ns, m in sorted(ex.items()) for k, v in sorted(m.items())],
            "summary": summ,
            "not_wired": [f"{r['kind']}:{r['name']}" for r in rows if r["verdict"] in FAILING and not r["exempt"]]}


def failed(why: str) -> dict:
    """The artifact when the check itself could not run: one advisory finding, the analysis goes on."""
    return {"rows": [], "error": str(why)[:2000], "advisory": True, "tolerance": {}, "evaluations": {},
            "exemptions": [], "summary": {}, "not_wired": []}


def finish(A, rec, dts, z0, ben0) -> dict:
    """run(), never raising (advisory): a failure is recorded in the artifact."""
    if rec is None:
        return failed("the evaluation recorder could not start")
    try:
        return run(A, rec, dts, z0, ben0)
    except Exception as e:  # noqa: BLE001 -- advisory: recorded, never a failed analysis
        return failed(f"{type(e).__name__}: {e}")


def findings(rel: str, data) -> list[dict]:
    """One advisory `input_not_wired` (MEDIUM, scope-declarable, would_block) per failing input without an exemption;
    an unreadable payload, an unknown verdict or a check that failed to run is one too."""
    def f(claim, key):
        return {"kind": FINDING, "severity": "MEDIUM", "stage": "ANALYZE", "target_stage": "MODEL", "claim": claim,
                "key": key, "scope_declarable": True, "would_block": True, "advisory": True, "source": PRODUCER}
    if isinstance(data, dict) and data.get("error"):
        return [f(f"{rel}: the wiring check did not run ({data['error']}), so no input is shown to reach an output",
                  rel)]
    rows = (data or {}).get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return [f(f"{rel}: rows unreadable, so no input is shown to reach an output", rel)]
    out = []
    for r in rows:
        v, nm, kd = r.get("verdict"), str(r.get("name")), str(r.get("kind"))
        if v not in VERDICTS:
            out.append(f(f"{rel}: input {nm} has an unknown wiring verdict {v!r}", f"{kd}:{nm}"))
        elif v in FAILING and not (isinstance(r.get("exempt"), str) and r["exempt"].strip()):
            why = {"not_wired": "moves no declared output", "not_checked": "has no declared range to move it over",
                   "below_noise_floor": "moves the declared outputs by less than the engine's own noise",
                   "emulator_unverified": "moves the emulator's outputs too little for the emulator to show that "
                                          "the engine reads it"}[v]
            out.append(f(f"Input {nm} ({kd}, {r.get('basis')}) {why} at either end of its range: it may be "
                         "disconnected from the model (a hard-coded value, an unused branch). Wire it, remove it "
                         "from the spec, or declare analysis.wiring_exempt with the reason (advisory).", f"{kd}:{nm}"))
    return out


def results_lines(data) -> list[str]:
    """results.md: what was checked, in words, and every exemption with its reason (unused ones too)."""
    L = ["## Input wiring (advisory)", ""]
    if data.get("error"):
        return L + ["The wiring check did not run (analysis/wiring.yaml records why).", ""]
    rows = data["rows"]
    bad = [f"{r['kind']} {r['name']}" for r in rows if r["verdict"] in FAILING and not r["exempt"]]
    L += ["Every declared input was moved to each end of its range, the others at their point values, and its effect "
          "on the outcomes and costs recorded (analysis/wiring.yaml). "
          + ("Inputs not shown to move a declared output: " + ", ".join(f"`{n}`" for n in bad) + "."
             if bad else "Every input that was checked moves at least one declared output.")]
    ex = [r for r in rows if r["exempt"]]
    if ex:
        L += ["", "Inputs exempt from the check (analysis.wiring_exempt in the signed spec):", ""]
        L += [f"- `{r['name']}` ({r['kind']}, {r['verdict'].replace('_', ' ')}): {r['exempt']}" for r in ex]
    unused = [e for e in data.get("exemptions") or [] if not e.get("used")]
    if unused:
        L += ["", "Exemptions that name no input of this run:", ""]
        L += [f"- `{e['name']}` ({e['namespace']}): {e['reason']}" for e in unused]
    return L + [""]


# ------------------------------------------------------------------ self-test
def _toy(dead=(), tiny=(), sd=0.0, seed=1, fn=None):
    """A fake analysis over a closed-form engine: outcome y = base * (1 - eff * a) * (1 + tiny_p ...) * fn(theta);
    `dead` parameters are declared with a range and never read; `tiny` ones move y by 1e-5 of their value; `sd` adds
    a seed-dependent shift (a stochastic engine run at a fixed seed)."""
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
            f = (1 + sum(1e-5 * theta[p] for p in tiny)) * (fn(theta) if fn else 1.0)
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

    by = lambda W: {(r["kind"], r["name"]): r for r in W["rows"]}  # noqa: E731
    P = lambda n: ("parameter", n)  # noqa: E731
    keys = lambda W: sorted(x["key"] for x in findings("w", W))  # noqa: E731
    A, o, b, c = _toy()
    W = check(A, o, b, c)
    ok(by(W)[P("eff")]["verdict"] == "wired" and by(W)[("cost_item", "ca")]["verdict"] == "wired"
       and findings("w", W) == [] and W["evaluations"]["extra"] == 4 and W["evaluations"]["noise_probe"] == 1,
       f"WR1 a wired parameter and cost item pass, with every end evaluated here and counted: {W['evaluations']}")
    A, o, b, c = _toy(dead=("decay",))
    W = check(A, o, b, c)
    f = findings("analysis/wiring.yaml", W)
    ok(by(W)[P("decay")]["verdict"] == "not_wired" and by(W)[P("decay")]["max_rel_change"] == 0.0
       and [(x["kind"], x["key"], x["severity"], x["scope_declarable"], x["would_block"]) for x in f]
       == [(FINDING, "parameter:decay", "MEDIUM", True, True)] and W["not_wired"] == ["parameter:decay"],
       f"WR2 a declared parameter the engine never reads is an advisory input_not_wired finding (would block): {f}")
    A.spec.raw["analysis"]["wiring_exempt"] = {"parameters": {"decay": "read only by the next engine version (toy)",
                                                              "stale": "an old waiver"}}
    W = check(A, o, b, c)
    L = "\n".join(results_lines(W))
    ok(findings("w", W) == [] and by(W)[P("decay")]["exempt"] and "`decay`" in L and "next engine version" in L
       and "`stale`" in L and "an old waiver" in L
       and [(e["name"], e["used"]) for e in W["exemptions"]] == [("decay", True), ("stale", False)],
       "WR3 an exempt dead parameter passes; results.md lists it with its reason, and an unused exemption too")
    A, o, b, c = _toy(tiny=("drift",), sd=0.05)
    W = check(A, o, b, c)
    nz = W["tolerance"]["noise"]["max_rel"]
    ok(by(W)[P("drift")]["verdict"] == "below_noise_floor" and by(W)[P("eff")]["verdict"] == "wired" and nz > 0
       and keys(W) == ["parameter:drift"],
       f"WR4 a stochastic engine: an effect below its seed-to-seed noise ({nz:.2g}) is a finding, a large one passes")
    A, o, b, c = _toy(tiny=("drift",))
    W = check(A, o, b, c)
    ok(by(W)[P("drift")]["verdict"] == "wired" and W["tolerance"]["noise"]["max_rel"] == 0.0,
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
           {"test_id": "DT4", "type": "outcome_ratio", "status": "not_run"},
           {"test_id": "DT5", "type": "boundary_engine", "status": "run", "benefit_change_by_package": {"a": 0.0}},
           {"test_id": "DT6", "type": "parameter_value", "parameter": "q", "status": "not_run"}]
    A, o, b, c = _toy()
    A.theta0["k2"] = 0.5
    W = check(A, o, b, c, dts=dts)
    v = {n: r["verdict"] for (_k, n), r in by(W).items()}
    ok(v["k2"] == "not_wired" and by(W)[P("k2")]["ends"] == [0.1, 0.9] and v["DT2"] == "not_wired"
       and v["DT3"] == v["DT5"] == "not_model_input" and v["DT4"] == v["q"] == "test_not_run"
       and keys(W) == ["decision_test:DT2", "parameter:k2"],
       f"WR7 decision-test inputs: tested ends, a unit-attribute record, a boundary agreement, tests not run: {v}")
    ok(len(findings("w", {"rows": "x"})) == 1 and findings("w", {"rows": [{"name": "p", "verdict": "maybe"}]})[0][
        "kind"] == FINDING, "WR8 fail closed: unreadable rows or an unknown verdict is an (advisory) finding")
    ok(validate({"analysis": {"wiring_exempt": {"parameters": {"p": " "}}}})
       and validate({"analysis": {"wiring_exempt": ["p"]}}) and validate({"analysis": {"wiring_exempt": {"p": "why"}}})
       and validate({"analysis": {"wiring_exempt": {"parameters": ["p"]}}})
       and validate({"analysis": {"wiring_exempt": {"parameters": {"p": "why"}, "cost_items": {7: "why"}}}}) == [],
       "WR9 analysis.wiring_exempt: namespaces of names to a non-empty reason; a flat map is refused")
    import tempfile
    from toolkit.analysis import batch as Bt
    from toolkit.spec import schema
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d, n=12)
        j0 = [Bt.job_sha(j) for j in Bt.design(schema.load(p))[1]]
        import yaml
        r = yaml.safe_load(open(p))
        r["analysis"]["wiring_exempt"] = {"parameters": {"eff_b": "toy"}}
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        s = schema.load(p)
        ok(schema.validate(s) == [] and [Bt.job_sha(j) for j in Bt.design(s)[1]] == j0,
           "WR10 an exemption validates and changes no batch job hash (the reuse key)")
    A, o, b, c = _toy()
    W = check(A, o, b, c, dts=[{"test_id": "DTc", "type": "cost_items", "status": "run",
                                "cost_multipliers": {"ca": 1.2, 7: 1.1}}])
    ok(by(W)[("cost_item", "ca")]["verdict"] == "wired" and by(W)[("cost_item", "7")]["verdict"] == "not_wired",
       "WR11 a cost_items test with numeric and text item ids is checked, never a crash")
    A, o, b, c = _toy()
    A.spec["analysis"]["sensitivity"]["cost_items"]["ca"] = [1.0, 1.0]
    A.fr["flat"], A.theta0["flat"] = (0.5, 0.5, 0.5), 0.5
    W = check(A, o, b, c)
    ok(by(W)[("cost_item", "ca")]["verdict"] == by(W)[P("flat")]["verdict"] == "not_perturbed" and keys(W) == [],
       "WR12 an identity range ([1, 1], or both ends at the point) is recorded not_perturbed, not dead")
    A, o, b, c = _toy()
    A.spec.raw["uncertainty"] = {"sources": [{"kind": "costs", "items": {"cb": [0.8, 1.2]}}]}
    W = check(A, o, b, c)
    ok(by(W)[("cost_item", "cb")]["verdict"] == "not_wired" and keys(W) == ["cost_item:cb"],
       "WR13 a cost range declared only in uncertainty.sources gets a row")
    A, o, b, c = _toy(dead=("DT2",))
    A.spec.raw["analysis"]["wiring_exempt"] = {"decision_tests": {"DT2": "the attribute is inert here (toy)"}}
    W = check(A, o, b, c, dts=[dts[1]])
    ok(keys(W) == ["parameter:DT2"] and by(W)[("decision_test", "DT2")]["exempt"],
       "WR14 exemptions have one namespace per kind: a decision test's reason never covers a parameter of that name")
    A, o, b, c = _toy(dead=("eff_b",))
    A.spec.raw["options"] = {"components": {"a": {}, "b": {}}}
    W = check(A, o, b, c)
    ok(by(W)[P("eff_b")]["verdict"] == "not_applicable" and by(W)[P("eff_b")]["measured_verdict"] == "not_wired"
       and W["pruned_components"] == ["b"] and keys(W) == [],
       "WR15 a parameter that can act only through pruned packages is recorded not_applicable")
    A, o, b, c = _toy(fn=lambda th: 1 + th.get("switch", 0.0) * th.get("p", 0.0))
    A.theta0.update(switch=0.0, p=0.0)
    W = check(A, o, b, c, dts=[{"test_id": "DTw", "type": "parameter_value", "parameter": "p", "status": "run",
                                "with": {"switch": 1.0}, "results": [{"value": 1.0}, {"value": 2.0}]}])
    ok(by(W)[P("p")]["verdict"] == "wired" and keys(W) == [],
       f"WR16 a conditional parameter_value test keeps its with settings: {by(W)[P('p')]['verdict']}")
    ok(dt_floor({"cases@a": 0.0, "cases@none": 0.0, "dalys@a": 0.6}, "cases", "none", "a") == 0.0,
       "WR17 a decision test's noise floor is its own outputs', not the largest over all outputs")
    A, o, b, c = _toy(dead=("decay",), tiny=("drift",))
    A.model.kind = "emulator"
    W = check(A, o, b, c)
    ok(by(W)[P("decay")]["verdict"] == by(W)[P("drift")]["verdict"] == "emulator_unverified"
       and by(W)[P("eff")]["verdict"] == "wired" and W["evaluations"]["noise_probe"] == 0
       and keys(W) == ["parameter:decay", "parameter:drift"],
       "WR18 on the emulator path a near-zero effect is never certified wired: emulator_unverified")
    A, o, b, c = _toy()
    rec = Recorder(A, o, c)
    A.outcomes = ["nope"]
    try:
        r_ = A.evaluate(A.theta0, None)
        ok(r_ and rec.errors and finish(A, None, [], None, b)["error"]
           and [x["severity"] for x in findings("w", failed("boom"))] == ["MEDIUM"]
           and "did not run" in "\n".join(results_lines(failed("boom"))),
           "WR19 advisory: a recording problem never fails the evaluation, and a check that fails is recorded")
    finally:
        rec.close()
    from toolkit.analysis.artifacts import EXPECTED_KINDS
    ok("wiring" not in EXPECTED_KINDS,
       "WR20 a manifest from before the check is not required to hold a wiring artifact (expected per run only)")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
