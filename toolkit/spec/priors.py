#!/usr/bin/env python3
"""Parameter priors and the uncertainty draw plan (v2 workstream D note, §2.4 and §3.2).

    python3 toolkit/spec/priors.py --plan <decision_spec.yaml>    # prints the draw plan (every source)
    python3 toolkit/spec/priors.py --self-test

A `prior` parameter has a published range, a distribution and a citation, and is drawn in every
uncertainty draw ("non-identified parameters stay at their declared range, never fixed"). Two
places declare priors:

  engine-card priors  `priors.yaml` beside the engine module (or the engine's `priors_path`). It is
                      card metadata, not engine code: the engine's code fingerprint, its parameter
                      table and toolkit/engines/contract.py are unchanged, so no benchmark re-stamp.
                          priors:   {name: {dist, range: [lo, hi], source, verified, note}}
                          fixed:    {name: why it stays fixed (no published range, a normative standard, ...)}
                          settings: [names]       numerical settings and structural switches (not uncertain)
                          not_exposed: {name: why} quantities that are module constants, not parameters
                      Every engine parameter of kind `fixed` must be a prior, have a fixed reason or be a
                      setting (no silent defaults). A prior's parameter must be of kind `fixed` on the
                      engine, and its point value must lie in its range.
  spec priors         `uncertainty.priors: {name: {dist, range, source, verified}}` in the signed spec,
                      for engine parameters of kind `scenario` (for example an achieved coverage) or a
                      `fixed` parameter the card gives no prior. A scenario parameter without one is held
                      at its value, and the interval label says so.

Distributions are bounded, so the emulator design box (toolkit/analysis/batch.py) spans every draw:
`uniform`, `loguniform` and `triangular` (mode at the engine's point value). A prior is drawn unless
the spec excludes it by name (`uncertainty.excluded: [{parameter, reason}]`) or excludes the kind
(`{kind: priors, reason}`); either way the label names what is fixed. The calibration does not
condition on these parameters (it moves only its own attribute or parameters), so each is
`prior_dominated` (contraction 0): it gets an automatic `prior_range` decision test and is named in
the interval label. Nothing here names a disease.
"""
from __future__ import annotations

import inspect
import math
import os
import sys
from dataclasses import dataclass, asdict

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import ParamSpec, ToyEngine, resolve_params, Outcomes  # noqa: E402

PRIORS_FILE = "priors.yaml"
DISTS = ("uniform", "loguniform", "triangular")
SPEC_PRIOR_KINDS = ("scenario", "fixed")
PRIOR_STREAM = 7          # the priors' own random stream: adding a prior does not reshuffle the other sources


@dataclass
class Prior:
    name: str
    value: float          # the engine's point value (the mode of a triangular prior)
    lo: float
    hi: float
    dist: str
    source: str
    verified: str = "unverified"
    origin: str = "engine_card"      # engine_card | spec
    kind: str = "fixed"              # the engine's parameter kind
    units: str = ""
    note: str = ""

    def sample(self, rng) -> float:
        if self.dist == "uniform":
            return float(rng.uniform(self.lo, self.hi))
        if self.dist == "loguniform":
            return float(math.exp(rng.uniform(math.log(self.lo), math.log(self.hi))))
        return float(rng.triangular(self.lo, min(max(self.value, self.lo), self.hi), self.hi))


def priors_path(engine) -> str | None:
    p = getattr(engine, "priors_path", None)
    if p:
        return p
    try:
        return os.path.join(os.path.dirname(os.path.abspath(inspect.getfile(type(engine)))), PRIORS_FILE)
    except (TypeError, OSError):
        return None


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _check_prior(name, d, ps: ParamSpec, where) -> tuple[Prior | None, list[str]]:
    bad = []
    if not isinstance(d, dict):
        return None, [f"{where}.{name}: a mapping {{dist, range, source}}"]
    rng, dist, src = d.get("range"), d.get("dist"), str(d.get("source") or "").strip()
    if dist not in DISTS:
        bad.append(f"{where}.{name}: dist {dist!r} not in {DISTS}")
    if not (isinstance(rng, list) and len(rng) == 2 and all(_isnum(x) for x in rng) and rng[0] < rng[1]):
        return None, bad + [f"{where}.{name}: range [lo, hi] with lo < hi"]
    if not src:
        bad.append(f"{where}.{name}: source (the citation of the range) is empty")
    if dist == "loguniform" and rng[0] <= 0:
        bad.append(f"{where}.{name}: a loguniform range must be positive")
    if not rng[0] <= ps.value <= rng[1]:
        bad.append(f"{where}.{name}: the engine's point value {ps.value:g} is outside the range {rng}")
    if bad:
        return None, bad
    return Prior(name, float(ps.value), float(rng[0]), float(rng[1]), dist, src, str(d.get("verified", "unverified")),
                 kind=ps.kind, units=ps.units, note=str(d.get("note") or "")), []


def load_card(engine) -> dict:
    """{priors, fixed, settings, not_exposed, path, problems} from the engine's priors.yaml (empty when there is none)."""
    out = {"priors": {}, "fixed": {}, "settings": [], "not_exposed": {}, "path": None, "problems": []}
    p = priors_path(engine)
    if not p or not os.path.exists(p):
        return out
    out["path"] = p
    try:
        doc = yaml.safe_load(open(p)) or {}
    except (OSError, yaml.YAMLError) as e:
        out["problems"].append(f"engine priors {p}: unreadable ({e})")
        return out
    specs = {q.name: q for q in engine.parameters()}
    for name, d in (doc.get("priors") or {}).items():
        ps = specs.get(name)
        if ps is None:
            out["problems"].append(f"engine priors: {name} is not an engine parameter")
            continue
        if ps.kind != "fixed":
            out["problems"].append(f"engine priors: {name} is of kind {ps.kind}; only a fixed parameter takes a card prior")
            continue
        pr, bad = _check_prior(name, d, ps, "engine priors")
        out["problems"] += bad
        if pr:
            out["priors"][name] = pr
    out["fixed"] = {str(k): str(v or "").strip() for k, v in (doc.get("fixed") or {}).items()}
    out["settings"] = [str(x) for x in doc.get("settings") or []]
    out["not_exposed"] = {str(k): str(v or "") for k, v in (doc.get("not_exposed") or {}).items()}
    for k in list(out["fixed"]) + out["settings"]:
        if k not in specs:
            out["problems"].append(f"engine priors: {k} (fixed or settings) is not an engine parameter")
        elif k in out["priors"]:
            out["problems"].append(f"engine priors: {k} is both a prior and fixed")
    out["problems"] += [f"engine priors: fixed parameter {k} has no reason" for k, v in out["fixed"].items() if not v]
    listed = set(out["priors"]) | set(out["fixed"]) | set(out["settings"]) | set((doc.get("priors") or {}))
    for k, ps in specs.items():
        if ps.kind == "fixed" and k not in listed:
            out["problems"].append(f"engine priors: fixed parameter {k} is neither a prior, nor fixed with a reason, nor a "
                                   "setting (no silent defaults)")
    return out


def excluded_parameters(u: dict) -> dict:
    return {str(x["parameter"]): str(x.get("reason") or "").strip() for x in (u.get("excluded") or [])
            if isinstance(x, dict) and "parameter" in x}


def kind_excluded(u: dict, kind: str) -> str | None:
    for x in u.get("excluded") or []:
        if isinstance(x, dict) and x.get("kind") == kind:
            return str(x.get("reason") or "").strip()
    return None


def spec_priors(u: dict, engine, card: dict) -> tuple[dict, list[str]]:
    specs = {q.name: q for q in engine.parameters()}
    out, bad = {}, []
    sp = u.get("priors") or {}
    if not isinstance(sp, dict):
        return {}, ["uncertainty.priors: a mapping {parameter: {dist, range, source}}"]
    for name, d in sp.items():
        ps = specs.get(name)
        if ps is None:
            bad.append(f"uncertainty.priors: {name} is not an engine parameter")
            continue
        if ps.kind not in SPEC_PRIOR_KINDS:
            bad.append(f"uncertainty.priors: {name} is of kind {ps.kind}; a spec prior is for a scenario or fixed parameter")
            continue
        if name in card["priors"]:
            bad.append(f"uncertainty.priors: {name} already has an engine-card prior")
            continue
        pr, b = _check_prior(name, d, ps, "uncertainty.priors")
        bad += b
        if pr:
            pr.origin = "spec"
            out[name] = pr
    return out, bad


def problems(spec) -> list[str]:
    """Spec-level problems with the priors (needs the engine)."""
    eng = spec.engine()
    u = spec.raw["uncertainty"]
    card = load_card(eng)
    sp, bad = spec_priors(u, eng, card)
    bad = card["problems"] + bad
    allp = set(card["priors"]) | set(sp)
    for name, why in excluded_parameters(u).items():
        if name not in allp:
            bad.append(f"uncertainty.excluded: parameter {name} has no prior (only a prior parameter is excluded by name)")
        if not why:
            bad.append(f"uncertainty.excluded: parameter {name} needs a reason")
    inc = [x.get("kind") for x in u.get("sources") or [] if isinstance(x, dict)]
    if "priors" in inc and not allp:
        bad.append("uncertainty.sources priors: neither the engine card nor the spec declares a prior")
    return bad


def drawn(spec) -> dict:
    """{name: Prior} drawn in every uncertainty draw, in name order (the emulator design's prior inputs)."""
    u = spec.raw["uncertainty"]
    if kind_excluded(u, "priors"):
        return {}
    eng = spec.engine()
    card = load_card(eng)
    sp, _ = spec_priors(u, eng, card)
    exc = excluded_parameters(u)
    allp = dict(card["priors"], **sp)
    return {k: allp[k] for k in sorted(allp) if k not in exc}


def plan(spec) -> dict:
    """The draw plan: every uncertainty source and every engine parameter, drawn or fixed, with why."""
    from toolkit.spec.schema import UNCERTAINTY_KINDS
    u = spec.raw["uncertainty"]
    eng = spec.engine()
    card = load_card(eng)
    sp, _ = spec_priors(u, eng, card)
    dr = drawn(spec)
    exc = excluded_parameters(u)
    inc = [x.get("kind") for x in u.get("sources") or [] if isinstance(x, dict)]
    kex = {x.get("kind"): x.get("reason") for x in u.get("excluded") or [] if isinstance(x, dict) and "kind" in x}
    cal = spec.raw["calibration"]
    moves = ([cal.get("attribute")] if cal.get("method") not in (None, "none") else []) + list(cal.get("parameters") or [])
    sources = []
    for k, what in list(UNCERTAINTY_KINDS.items()) + [("priors", PRIORS_WHAT)]:
        if k == "priors":
            st = "drawn" if dr else "excluded" if k in kex else "none_declared"
            why = kex.get(k) or ("" if dr else "no prior parameter on the engine card or in the spec")
        else:
            st = "drawn" if k in inc else "excluded"
            why = kex.get(k, "")
        sources.append({"kind": k, "status": st, "what": what, **({"reason": why} if why else {})})
    params, fixed = [], []
    for ps in eng.parameters():
        if ps.name in dr:
            p = dr[ps.name]
            params.append(dict(asdict(p), range=[p.lo, p.hi], status="drawn", source_kind="priors",
                               identifiability="prior_dominated",
                               basis="the calibration does not condition on it (it moves only "
                                     f"{', '.join(map(str, moves)) or 'nothing'}); contraction 0"))
            continue
        row = {"name": ps.name, "kind": ps.kind, "value": ps.value, "units": ps.units}
        if ps.kind == "fitted":
            on = "engine_parameters" in inc
            row.update(status="drawn" if on else "fixed", source_kind="engine_parameters", range=[ps.lo, ps.hi],
                       dist="uniform", source=ps.source,
                       **({} if on else {"reason": kex.get("engine_parameters") or "engine_parameters not drawn"}))
        elif ps.kind == "calibrated":
            row.update(status="drawn" if "calibration" in inc and ps.name in moves else "fixed",
                       source_kind="calibration", source=ps.source)
        elif ps.name in exc:
            row.update(status="fixed", reason=f"excluded by the spec: {exc[ps.name]}",
                       prior=asdict(card["priors"].get(ps.name) or sp[ps.name]))
        elif ps.name in card["priors"] or ps.name in sp:
            row.update(status="fixed", reason=f"priors excluded by the spec: {kex.get('priors')}")
        elif ps.kind == "scenario":
            row.update(status="fixed", reason="scenario value; the spec declares no range (uncertainty.priors)")
        elif ps.name in card["settings"]:
            row.update(status="setting", reason="a numerical setting or structural switch, not an uncertain quantity")
        else:
            row.update(status="fixed", reason=card["fixed"].get(ps.name) or "no prior on the engine card",
                       card_range=[ps.lo, ps.hi] if ps.lo is not None else None, source=ps.source)
        (params if row["status"] == "drawn" else fixed).append(row)
    return {"sources": sources, "drawn": params, "fixed": fixed, "not_exposed": card["not_exposed"],
            "engine_priors_file": card["path"], "engine_priors_sha256": _sha(card["path"]),
            "prior_dominated": sorted(dr), "label_fixed": label_fixed(spec, dr, fixed)}


PRIORS_WHAT = "engine parameters with published ranges (engine-card or spec priors), drawn from their declared priors"


def label_fixed(spec, dr, fixed_rows) -> dict:
    """What the interval label names as held fixed: scenario parameters without a range and parameters excluded by
    name; the other fixed parameters are counted (each is listed with its reason in the draw plan)."""
    exc = excluded_parameters(spec.raw["uncertainty"])
    named = sorted(r["name"] for r in fixed_rows if r["status"] == "fixed" and (r["kind"] == "scenario" or r["name"] in exc))
    other = sum(1 for r in fixed_rows if r["status"] == "fixed" and r["name"] not in named and r["kind"] == "fixed")
    return {"named": named, "other": other}


def _sha(p):
    if not p or not os.path.exists(p):
        return None
    import hashlib
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


# --------------------------------------------------------------------------- toy engine with priors (self-tests)
class ToyPriorEngine(ToyEngine):
    """The toy engine with a DALY weight `dw` (DALYs = dw x burden) and the `synergy` scenario parameter, and a
    priors file given by `priors_path` (self-tests of the priors, the emulator design and the draws)."""

    def __init__(self, results_path: str | None = None, priors_path: str | None = None):
        super().__init__(results_path)
        self.priors_path = priors_path

    def parameters(self) -> list:
        return super().parameters() + [ParamSpec("dw", 0.01, "fixed", "weight", source="toy weight")]

    def run(self, params, unit, package, horizon, seed) -> Outcomes:
        p = resolve_params(self, params)
        o = super().run({k: v for k, v in p.items() if k != "dw"}, unit, package, horizon, seed)
        o.per_capita["dalys"] = {g: p["dw"] * v for g, v in o.per_capita["cases"].items()}
        return o


def toy_priors_file(d: str, dw=(0.005, 0.02), dist="triangular") -> str:
    p = os.path.join(d, "toy_priors.yaml")
    yaml.safe_dump({"priors": {"dw": {"dist": dist, "range": list(dw), "source": "toy citation", "verified": "matched"}},
                    "fixed": {"base": "toy: no range", "k": "toy: no range"}, "settings": ["noise"]}, open(p, "w"))
    return p


def _self_test() -> int:
    import copy
    import tempfile
    import numpy as np
    from toolkit.spec import schema
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as d:
        pp = toy_priors_file(d)
        s0 = schema.load(schema.toy_spec(d))
        r = copy.deepcopy(s0.raw)
        r["engine"] = {"ref": "toolkit.spec.priors:ToyPriorEngine", "config": {"priors_path": pp}}
        s = schema.Spec(r, d)
        ok(schema.validate(s) == [], f"PR1 a spec on an engine with card priors validates unchanged: {schema.validate(s)}")
        dr = drawn(s)
        ok(list(dr) == ["dw"] and dr["dw"].lo == 0.005 and dr["dw"].origin == "engine_card",
           "PR2 an engine-card prior is drawn without the spec listing it (never silently fixed)")
        P = plan(s)
        st = {x["kind"]: x["status"] for x in P["sources"]}
        ok(st == {"engine_parameters": "drawn", "calibration": "drawn", "costs": "excluded", "emulator": "excluded",
                  "priors": "drawn"}, f"PR3 the draw plan lists every source with its status: {st}")
        fx = {x["name"]: x for x in P["fixed"]}
        ok(fx["synergy"]["reason"].startswith("scenario value") and fx["base"]["reason"] == "toy: no range"
           and fx["noise"]["status"] == "setting" and P["label_fixed"] == {"named": ["synergy"], "other": 2},
           f"PR4 fixed parameters are listed with why, scenario ones named for the label: {P['label_fixed']}")
        ok(P["prior_dominated"] == ["dw"] and P["drawn"][-1]["identifiability"] == "prior_dominated",
           "PR5 a prior the calibration does not condition on is classed prior_dominated")

        def bad(mut, needle, label, path=pp):
            rr = copy.deepcopy(r)
            mut(rr)
            errs = schema.validate(schema.Spec(rr, d))
            ok(any(needle in e for e in errs), f"{label}: {errs[:3]}")
        bad(lambda x: x["uncertainty"].update(priors={"synergy": {"dist": "uniform", "range": [0.0, 0.2]}}),
            "source", "PR6 a spec prior without a citation is refused")
        bad(lambda x: x["uncertainty"].update(priors={"eff_a": {"dist": "uniform", "range": [0.3, 0.7], "source": "s"}}),
            "of kind fitted", "PR7 a spec prior on a fitted parameter is refused")
        bad(lambda x: x["uncertainty"].update(priors={"synergy": {"dist": "uniform", "range": [0.1, 0.2], "source": "s"}}),
            "outside the range", "PR8 a range that excludes the point value is refused")
        bad(lambda x: x["uncertainty"].update(priors={"synergy": {"dist": "normal", "range": [-0.1, 0.2], "source": "s"}}),
            "dist", "PR9 an unbounded or unknown distribution is refused (the design box must span every draw)")
        bad(lambda x: x["uncertainty"]["excluded"].append({"parameter": "dw", "reason": ""}), "needs a reason",
            "PR10 a parameter fixed by name without a reason is refused")
        bad(lambda x: x["uncertainty"]["excluded"].append({"parameter": "base", "reason": "r"}), "has no prior",
            "PR11 excluding by name a parameter that has no prior is refused")
        yaml.safe_dump({"priors": {"dw": {"dist": "uniform", "range": [0.005, 0.02], "source": "c"}}}, open(pp, "w"))
        bad(lambda x: None, "no silent defaults", "PR12 a card that leaves a fixed parameter unaccounted for is refused")
        yaml.safe_dump({"priors": {"eff_a": {"dist": "uniform", "range": [0.3, 0.7], "source": "c"}}}, open(pp, "w"))
        bad(lambda x: None, "only a fixed parameter", "PR13 a card prior on a fitted parameter is refused")
        toy_priors_file(d)
        r2 = copy.deepcopy(r)
        r2["uncertainty"]["priors"] = {"synergy": {"dist": "uniform", "range": [-0.05, 0.1], "source": "spec citation"}}
        r2["uncertainty"]["excluded"].append({"parameter": "dw", "reason": "owner: fixed for this test"})
        s2 = schema.Spec(r2, d)
        P2 = plan(s2)
        ok(schema.validate(s2) == [] and list(drawn(s2)) == ["synergy"] and P2["label_fixed"]["named"] == ["dw"]
           and drawn(s2)["synergy"].origin == "spec",
           f"PR14 a scenario parameter with a spec range is drawn; one excluded by name is fixed and named: {P2['label_fixed']}")
        r3 = copy.deepcopy(r)
        r3["uncertainty"]["excluded"].append({"kind": "priors", "reason": "reproduce the spike batch"})
        ok(drawn(schema.Spec(r3, d)) == {} and {x["kind"]: x["status"] for x in plan(schema.Spec(r3, d))["sources"]}["priors"]
           == "excluded", "PR15 excluding the priors kind with a reason holds every prior fixed, and the plan says so")
        rng = np.random.default_rng(1)
        pr = Prior("x", 0.2, 0.1, 0.5, "triangular", "s")
        xs = [pr.sample(rng) for _ in range(4000)]
        lu = Prior("y", 1.0, 0.1, 10.0, "loguniform", "s")
        ys = np.log10([lu.sample(rng) for _ in range(4000)])
        ok(min(xs) >= 0.1 and max(xs) <= 0.5 and abs(np.mean(xs) - (0.1 + 0.2 + 0.5) / 3) < 0.01
           and abs(np.mean(ys)) < 0.05, "PR16 draws stay in the bounded support with the declared shape")
        ok(load_card(ToyEngine())["priors"] == {} and drawn(s0) == {}, "PR17 an engine without a priors file has no priors")
        # the malaria card's priors file: every fixed parameter accounted for, ranges contain the point values
        try:
            from toolkit.engines.malaria.engine import MalariaEngine
            mc = load_card(MalariaEngine())
            ok(mc["path"] and mc["problems"] == [] and {"smc_pe", "use_rate", "net_retention_half", "dw_moderate",
                                                        "dw_severe", "cfr_severe_treated"} <= set(mc["priors"]),
               f"PR18 the malaria priors file is complete and consistent with the engine: {mc['problems'][:3]}")
        except ImportError as e:
            ok(False, f"PR18 the malaria engine imports: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    if argv[:1] == ["--plan"] and len(argv) == 2:
        from toolkit.spec import schema
        s = schema.load(argv[1])
        print(yaml.safe_dump({"problems": schema.validate(s), "label": s.uncertainty_scope()["label"],
                              "plan": plan(s)}, sort_keys=False, width=120))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
