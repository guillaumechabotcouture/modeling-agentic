#!/usr/bin/env python3
"""Engine parameter values declared by the signed spec (owner decision, 2026-11-02).

    python3 toolkit/spec/engine_parameters.py --self-test

A spec may set the point value of an engine parameter for its run. The value is a scenario
choice the owner signs, not an engine change and not a fit:

    engine_parameters:
      <parameter>:
        value: 273.75                    # a finite number
        units: days                      # optional; must equal the engine card's units
        source: "<where the value comes from>"
        reason: "<why this run uses it>"
        outside_card_range: true         # required when the value (or its range) lies outside the
                                         # engine card's range; refused when it does not
        range: [lo, hi]                  # optional: the uncertainty draws vary it over this range
        dist: uniform | loguniform | triangular    # required with range (triangular: mode at value)

Rules (every check names the entry it failed):
  - the parameter is on the engine card, of kind `fixed` or `scenario`; fitted and calibrated
    values come from fitting and are refused; a unit attribute is bound in decision_units;
  - source and reason are required;
  - the card's range is the parameter's own `lo`/`hi`, else its engine-card prior's range. A
    value outside it needs `outside_card_range: true`; the sign-off sheet, results.md and the
    report then show it as a HIGH note;
  - the field is in the signed spec, so the sign-off hash covers it (analysis_settings.yaml
    cannot set it); `uncertainty.priors` cannot also name the parameter.

Application: `params(spec, theta)` returns the spec values with `theta` applied over them. It is
the one place the value enters an engine evaluation: every parameter set the analysis builds
starts from it (the point parameters of toolkit/analysis/core.py, which the uncertainty draws,
decision tests, stability folds, sensitivity, schedule and trajectory tasks copy; the emulator's
batch jobs in toolkit/analysis/batch.py; the calibration's engine runs in
toolkit/analysis/calibration.py). A draw or a decision test that sets the parameter still wins.

Uncertainty: with `range`, the parameter is drawn over it (an engine-card prior on it is
replaced); without, it is held fixed at the spec value (the card prior is not drawn) and the
interval label says "held fixed at the spec value". On the emulator path, a held parameter that a
`parameter_value` decision test varies is an emulator input over the box spanning the spec value
and the tested values, so the test runs; the draws keep it at the spec value.

A spec without the field is unchanged: same parameter sets, same batch jobs, same job hashes.
Nothing here names a disease, a place or an intervention.
"""
from __future__ import annotations

import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import ParamSpec, resolve_params  # noqa: E402
from toolkit.engines.empirical.engine import PackageRateEngine  # noqa: E402
from toolkit.engines.hooks import ToyScheduleEngine  # noqa: E402

FIELD = "engine_parameters"


def declared(spec) -> dict:
    raise NotImplementedError


def shape_problems(spec) -> list[str]:
    raise NotImplementedError


def problems(spec, engine) -> list[str]:
    raise NotImplementedError


def values(spec) -> dict:
    raise NotImplementedError


def held_fixed(spec) -> dict:
    raise NotImplementedError


def tested_boxes(spec) -> dict:
    raise NotImplementedError


def params(spec, theta=None) -> dict:
    raise NotImplementedError


def rows(spec, engine) -> list[dict]:
    raise NotImplementedError


def report_block(run_dir: str) -> list[str]:
    raise NotImplementedError


# --------------------------------------------------------------------------- toy engines (self-tests)
class ToySpecEngine(ToyScheduleEngine):
    """The toy engine with both hooks, a card range on `base` ([0.4, 0.6]) and a calibrated parameter `c_rate` (unused
    by the model). `log_path`: every run, schedule and run_series call appends the resolved base, k and synergy as a
    JSON line (worker processes included), so a self-test can see what each engine evaluation received."""

    def __init__(self, results_path: str | None = None, log_path: str | None = None):
        super().__init__(results_path)
        self.log_path = log_path

    def parameters(self) -> list:
        out = [ParamSpec("base", 0.5, "fixed", lo=0.4, hi=0.6, source="toy card range") if p.name == "base" else p
               for p in super().parameters()]
        return out + [ParamSpec("c_rate", 1.0, "calibrated", source="toy")]

    def _log(self, call, params):
        if self.log_path:
            p = resolve_params(self, params)
            with open(self.log_path, "a") as f:
                f.write(json.dumps({"call": call, **{k: p[k] for k in ("base", "k", "synergy")}}) + "\n")

    def run(self, params, unit, package, horizon, seed):
        self._log("run", params)
        return super().run(params, unit, package, horizon, seed)

    def schedule(self, params, unit, package, horizon):
        self._log("schedule", params)
        return super().schedule(params, unit, package, horizon)

    def run_series(self, params, unit, packages, horizon, seed, metrics, step="month"):
        self._log("run_series", params)
        return super().run_series(params, unit, packages, horizon, seed, metrics, step)


class ToySpecRateEngine(PackageRateEngine):
    """The package-rate engine with a fixed multiplier `scale` on the rate (no card range). In-process runs record the
    resolved scale in CALLS (the direct path's self-test)."""
    CALLS: list = []

    def parameters(self) -> list:
        return super().parameters() + [ParamSpec("scale", 1.0, "fixed", "multiplier", source="toy")]

    def run(self, params, unit, package, horizon, seed):
        p = resolve_params(self, params)
        ToySpecRateEngine.CALLS.append(p["scale"])
        o = super().run({k: v for k, v in p.items() if k != "scale"}, unit, package, horizon, seed)
        o.per_capita = {k: {g: v * p["scale"] for g, v in d.items()} for k, d in o.per_capita.items()}
        o.windows = {w: {m: v * p["scale"] for m, v in d.items()} for w, d in o.windows.items()}
        return o


# --------------------------------------------------------------------------- self-test
# Job hashes of the toy specs' emulator batches before this field existed (base c0ed767): a spec without the field
# must keep them byte for byte (sha256 over the concatenated job_sha values, first 16 hex digits; the temporary run
# directory in an engine config path is replaced by a placeholder).
GOLDEN_JOB_DIGESTS = {"toy": ("447abd162b931237", 240), "toy_priors": ("d2abd76d6b3e0a80", 240)}


def _self_test() -> int:
    import copy
    import glob
    import hashlib
    import tempfile
    import yaml
    from toolkit.spec import schema, signoff
    from toolkit.spec import engine_parameters as EPm     # the module the engine loader imports, not __main__
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def guard(label, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- a case that crashes is a failed case, and the others still run
            import traceback
            traceback.print_exc()
            ok(False, f"{label}: {type(e).__name__}: {str(e)[:300]}")

    q = lambda *a, **k: None  # noqa: E731
    SRC, WHY = "toy source (a benchmark sweep)", "toy reason (the owner's choice for the headline)"

    def toy_raw(d, **entries):
        raw = yaml.safe_load(open(schema.toy_spec(d, n=24)))
        raw["engine"] = {"ref": "toolkit.spec.engine_parameters:ToySpecEngine",
                         "config": {"log_path": os.path.join(d, "calls.jsonl")}}
        if entries:
            raw[FIELD] = entries
        return raw

    # ------------------------------------------------------------ schema
    def schema_cases():
        with tempfile.TemporaryDirectory() as d:
            good = {"value": 0.55, "source": SRC, "reason": WHY}
            r0 = toy_raw(d, base=dict(good))
            ok(schema.validate(schema.Spec(r0, d)) == [], f"EP1 a spec value with source and reason validates: "
                                                           f"{schema.validate(schema.Spec(r0, d))[:3]}")

            def bad(entries, needle, label, mut=None):
                r = toy_raw(d, **entries)
                if mut:
                    mut(r)
                errs = schema.validate(schema.Spec(r, d))
                ok(any(needle in e for e in errs), f"{label}: {[e for e in errs if FIELD in e or needle in e][:3]}")
            bad({"nope": dict(good)}, "not an engine parameter", "EP2 an unknown parameter is refused")
            bad({"x": dict(good)}, "unit attribute", "EP3 a unit attribute is refused, with where it is bound")
            bad({"eff_a": dict(good, value=0.5)}, "of kind fitted", "EP4a a fitted parameter is refused")
            bad({"c_rate": dict(good, value=1.0)}, "of kind calibrated", "EP4b a calibrated parameter is refused")
            bad({"base": {"value": 0.55, "reason": WHY}}, "source", "EP5a a value without a source is refused")
            bad({"base": {"value": 0.55, "source": SRC}}, "reason", "EP5b a value without a reason is refused")
            bad({"base": dict(good, value=0.3)}, "outside_card_range",
                "EP6a a value outside the card's range without the flag is refused")
            r = toy_raw(d, base=dict(good, value=0.3, outside_card_range=True))
            ok(schema.validate(schema.Spec(r, d)) == [],
               f"EP6b with outside_card_range: true it validates: {schema.validate(schema.Spec(r, d))[:3]}")
            bad({"base": dict(good, outside_card_range=True)}, "inside the card's range",
                "EP6c the flag on a value inside the card's range is refused")
            bad({"synergy": dict(good, value=0.1, outside_card_range=True)}, "gives no range",
                "EP6d the flag on a parameter whose card gives no range is refused")
            bad({"base": dict(good, range=[0.5, 0.6], dist="uniform")}, "containing the value",
                "EP7a a range that excludes the value is refused")
            bad({"base": dict(good, range=[0.45, 0.6])}, "dist", "EP7b a range without its distribution is refused")
            bad({"base": dict(good, range=[0.3, 0.6], dist="uniform")}, "outside_card_range",
                "EP7c a range reaching outside the card's range needs the flag")
            bad({"base": dict(good, value="high")}, "finite number", "EP7d a value that is not a number is refused")
            bad({"base": dict(good, units="days")}, "units", "EP7e units that differ from the card's are refused")
            bad({"base": dict(good, note="x")}, "unknown keys", "EP7f unknown keys are refused")
            bad({"synergy": dict(good, value=0.05)}, "declare its range there", "EP7g uncertainty.priors on a "
                "spec-valued parameter is refused",
                mut=lambda x: x["uncertainty"].update(priors={"synergy": {"dist": "uniform", "range": [0.0, 0.1],
                                                                          "source": "s"}}))
            r = toy_raw(d)
            r[FIELD] = ["base"]
            errs = schema.validate(schema.Spec(r, d))
            ok(any(e.startswith(FIELD) and "mapping" in e for e in errs), f"EP7h a non-mapping field is refused: {errs[:2]}")
            # engine: auto. Shape checks at sign-off; each candidate checked against its own card
            ra = toy_raw(d, base={"value": 0.55, "source": SRC})
            ra["engine"] = {"auto": True, "candidates": ["toolkit.spec.engine_parameters:ToySpecEngine",
                                                         "toolkit.engines.empirical.engine:PackageRateEngine"]}
            errs = schema.validate(schema.Spec(ra, d), pending_ok=True)
            ok(any(FIELD in e and "reason" in e for e in errs),
               f"EP9a engine: auto before the choice: the entry's shape is checked at sign-off: {errs[:2]}")
            ra[FIELD]["base"]["reason"] = WHY
            sa = schema.Spec(ra, d)
            e_ok = schema.validate(schema.with_engine(sa, "toolkit.spec.engine_parameters:ToySpecEngine"))
            e_bad = schema.validate(schema.with_engine(sa, "toolkit.engines.empirical.engine:PackageRateEngine",
                                                       {"components": ["a", "b"]}))
            ok(schema.validate(sa, pending_ok=True) == [] and not [e for e in e_ok if FIELD in e]
               and any(FIELD in e and "not an engine parameter" in e for e in e_bad),
               f"EP9b a candidate engine without the parameter is refused, one with it passes: {e_bad[:1]}")
        with tempfile.TemporaryDirectory() as d:
            r = toy_raw(d, base={"value": 0.55, "source": SRC, "reason": WHY})
            p = os.path.join(d, "decision_spec.yaml")
            yaml.safe_dump(r, open(p, "w"), sort_keys=False)
            yaml.safe_dump({FIELD: {"base": {"value": 0.45}}}, open(os.path.join(d, schema.SETTINGS), "w"))
            try:
                schema.load(p)
                ok(False, "EP8a analysis_settings.yaml cannot set a spec value")
            except schema.SpecError as e:
                ok("signed decision spec" in str(e), f"EP8a analysis_settings.yaml cannot set a spec value: {e}")
            os.remove(os.path.join(d, schema.SETTINGS))
            signoff.sign(d, "lead", note="self-test")
            st0 = signoff.status(d)
            r[FIELD]["base"]["value"] = 0.45
            yaml.safe_dump(r, open(p, "w"), sort_keys=False)
            ok(st0 == "signed" and signoff.status(d) == "changed",
               f"EP8b the value is in the signed spec: changing it breaks the sign-off ({st0} -> {signoff.status(d)})")
    guard("EP1-EP9 schema", schema_cases)

    # ------------------------------------------------------------ job hashes without the field
    def hash_cases():
        from toolkit.analysis import batch as Bt
        from toolkit.spec import priors as PRI

        with tempfile.TemporaryDirectory() as d:
            def dig(s):
                _d, jobs = Bt.design(s)
                norm = lambda j: dict(j, engine=json.loads(json.dumps(j["engine"]).replace(d, "<run>")))  # noqa: E731
                return hashlib.sha256("".join(Bt.job_sha(norm(j)) for j in jobs).encode()).hexdigest()[:16], len(jobs)
            s = schema.load(schema.toy_spec(d))
            pp = PRI.toy_priors_file(d)
            rp = copy.deepcopy(s.raw)
            rp["engine"] = {"ref": "toolkit.spec.priors:ToyPriorEngine", "config": {"priors_path": pp}}
            got = {"toy": dig(s), "toy_priors": dig(schema.Spec(rp, d))}
            ok(got == GOLDEN_JOB_DIGESTS, f"EP-H1 without the field, batch job hashes are byte-identical to before it "
                                          f"existed: {got}")
            re_ = copy.deepcopy(s.raw)
            re_[FIELD] = {}
            ok(dig(schema.Spec(re_, d)) == got["toy"] and EPm.params(schema.Spec(re_, d), {"a": 1.0}) == {"a": 1.0},
               "EP-H2 an empty field changes nothing")
            rk = copy.deepcopy(s.raw)
            rk[FIELD] = {"k": {"value": 0.9, "source": SRC, "reason": WHY}}
            sk = schema.Spec(rk, d)
            dk, jk = Bt.design(sk)
            d0, j0 = Bt.design(s)
            ok(dig(sk)[0] != got["toy"][0] and all(j["params"]["k"] == 0.9 for j in jk)
               and [{k: v for k, v in j["params"].items() if k != "k"} for j in jk] == [j["params"] for j in j0]
               and "k" not in j0[0]["params"],
               "EP-H3 with the field, the value enters every job's params (so job hashes change) and nothing else does")
    guard("EP-H job hashes", hash_cases)

    # ------------------------------------------------------------ emulator path: application, UQ, decision test, reports
    def emulator_cases():
        from toolkit.analysis import run as RUN, trajectories as TR
        with tempfile.TemporaryDirectory() as d:
            r = toy_raw(d,
                        base={"value": 0.3, "outside_card_range": True, "source": SRC, "reason": WHY},
                        k={"value": 0.9, "source": SRC, "reason": WHY},
                        synergy={"value": 0.05, "range": [0.0, 0.1], "dist": "uniform", "source": SRC,
                                 "reason": WHY})
            r["analysis"]["decision_tests"] = [{"id": "DTc", "type": "parameter_value", "parameter": "base",
                                                "values": [0.5], "finding": "decision_test:DTc",
                                                "text": "Does the plan change at the engine card value?"}]
            yaml.safe_dump(r, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
            signoff.sign(d, "lead", note="self-test")
            md = signoff.sheet(d)
            ok("## Engine parameter values set by the spec" in md and "HIGH" in md and "base" in md
               and "0.4" in md and "0.6" in md and WHY in md and SRC in md
               and md.count("HIGH") == 1,
               "EP20 the sign-off sheet lists each spec value with its source and reason, and a HIGH note for the one "
               "outside the card's range")
            R = RUN.run(d, log=q, compute_kw={"max_workers": 2})
            A = R["_A"]
            ok(A.theta0.get("base") == 0.3 and A.theta0.get("k") == 0.9 and A.theta0.get("synergy") == 0.05,
               f"EP10 the point parameters carry the spec values: { {k: A.theta0.get(k) for k in ('base', 'k')} }")
            design = json.load(open(os.path.join(d, "analysis", "batch", "design.json")))
            jobs = [json.loads(x) for x in open(os.path.join(d, "analysis", "batch", "results.jsonl"))]
            pts = design["points"]
            ok(design.get("spec_value_names") == ["base"] and all(0.3 - 1e-12 <= p_["theta"]["base"] <= 0.5 + 1e-12
                                                                  for p_ in pts)
               and min(p_["theta"]["base"] for p_ in pts) < 0.35 and max(p_["theta"]["base"] for p_ in pts) > 0.45
               and all(p_["theta"]["k"] == 0.9 for p_ in pts) and design.get("prior_names") == ["synergy"]
               and all(0.0 <= p_["theta"]["synergy"] <= 0.1 for p_ in pts) and len(jobs) == len(pts) * 4,
               f"EP11 the batch: k held at its spec value in every job; base (a decision test varies it) an emulator "
               f"input over the box from its spec value to the tested value; synergy drawn over its range: "
               f"{design.get('spec_value_names')}, {design.get('prior_names')}")
            calls = [json.loads(x) for x in open(os.path.join(d, "calls.jsonl"))]
            runs = [c for c in calls if c["call"] == "run"]
            sch = [c for c in calls if c["call"] == "schedule"]
            ok(runs and all(c["k"] == 0.9 for c in calls) and sch and all(c["base"] == 0.3 for c in sch),
               f"EP12 every engine evaluation received the held value (k: {len(calls)} calls, worker batch and "
               f"in-process schedule hooks); the figure schedule used base at its spec value ({len(sch)} calls)")
            tk, _step = TR.tasks(d, per_region=1, seeds=1)
            ok(tk and all(t["params"].get("base") == 0.3 and t["params"].get("k") == 0.9 for t in tk),
               "EP13 trajectory tasks carry the spec values")
            u = R["uncertainty"]
            rep = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
            ps = rep["parameter_samples"]
            ok("held fixed at the spec value: base, k" in u["label"] and "synergy" in (u["interval"].get(
                "prior_dominated") or []) and "base" not in ps and "k" not in ps
               and 0.0 <= ps["synergy"]["ci_low"] < ps["synergy"]["ci_high"] <= 0.1
               and {x["name"]: x for x in rep["parameters_fixed"]}["base"]["value"] == 0.3,
               f"EP14 UI: a value without a range is held fixed and the label says so; one with a range is drawn over "
               f"it: {u['label']}")
            dts = {x["test_id"]: x for x in R["decision_tests"]}
            t = dts.get("DTc") or {}
            ok(t.get("status") == "run" and [x["value"] for x in t.get("results") or []] == [0.5]
               and (t.get("spec_value") or {}).get("value") == 0.3
               and (t.get("spec_value") or {}).get("card_value") == 0.5
               and (t.get("spec_value") or {}).get("card_value_tested") is True
               and "spec value" in t.get("statement", "") and "card value" in t.get("statement", "")
               and not any(ch.isdigit() for ch in t.get("statement", "")),
               f"EP15 a parameter_value test at the card value runs against the spec-value headline, and says so in "
               f"words: {t.get('status')}, {t.get('statement')}")
            ok("prior_range_synergy" in dts and "prior_range_base" not in dts,
               f"EP16 the ranged spec value gets the automatic prior_range test; a held one does not: {sorted(dts)}")
            eng = yaml.safe_load(open(os.path.join(d, "analysis", "engine.yaml")))["data"]
            sv = eng.get("spec_values") or {}
            ok(set(sv) == {"base", "k", "synergy"} and sv["base"]["card_value"] == 0.5
               and sv["base"]["card_range"] == [0.4, 0.6] and sv["base"]["outside_card_range"] is True
               and sv["k"]["uncertainty"] == "held fixed at the spec value"
               and sv["synergy"]["uncertainty"].startswith("drawn") and sv["base"]["source"] == SRC,
               f"EP17 the engine artifact records each spec value with the card value it replaces: {sv.get('base')}")
            txt = open(os.path.join(d, "results.md")).read()
            sec = txt.split("## Engine parameter values set by the signed spec")[-1].split("\n## ")[0] \
                if "## Engine parameter values set by the signed spec" in txt else ""
            ok(sec and "base" in sec and "0.3" in sec and "0.5" in sec and "HIGH" in sec and SRC in sec
               and WHY in sec and "[CLAIM:" not in txt and sec.count("HIGH") == 1,
               f"EP18 results.md names every spec value with its source, reason and the card value it replaces: "
               f"{sec[:400]!r}")
            blk = "\n".join(EPm.report_block(d))
            ok("base" in blk and "0.3" in blk and "0.5" in blk and "HIGH" in blk and SRC in blk and WHY in blk
               and "k" in blk and "synergy" in blk, f"EP19 the report's harness block names them too: {blk[:300]!r}")
            figs = yaml.safe_load(open(os.path.join(d, "analysis", "figures.yaml")))["data"]["figures"]
            fp = [f for f in figs if f["figure"] == "parameters"]
            import csv
            prow = {x["name"]: x for x in csv.DictReader(open(os.path.join(d, fp[0]["table"])))} if fp else {}
            ok(prow.get("base", {}).get("kind") == "spec value" and float(prow["base"]["value"]) == 0.3
               and float(prow["base"]["card_value"]) == 0.5 and prow.get("k", {}).get("kind") == "spec value",
               f"EP21 the parameters figure shows spec values as such, with the card value: {prow.get('base')}")
            ok(not glob.glob(os.path.join(d, "analysis", "blocked.yaml")), "EP22 the run completes unblocked")
    guard("EP10-EP22 emulator path", emulator_cases)

    # ------------------------------------------------------------ direct path with likelihood calibration
    def direct_cases():
        from toolkit.analysis import run as RUN
        with tempfile.TemporaryDirectory() as d:
            RUN._direct_toy(d)
            p = os.path.join(d, "decision_spec.yaml")
            r = yaml.safe_load(open(p))
            r["engine"]["ref"] = "toolkit.spec.engine_parameters:ToySpecRateEngine"
            r[FIELD] = {"scale": {"value": 1.25, "units": "multiplier", "source": SRC, "reason": WHY}}
            r["analysis"]["decision_tests"].append({"id": "DTs", "type": "parameter_value", "parameter": "scale",
                                                    "values": [1.0], "text": "Does the plan change at the card value?"})
            yaml.safe_dump(r, open(p, "w"), sort_keys=False)
            signoff.sign(d, "lead", note="self-test")
            EPm.ToySpecRateEngine.CALLS.clear()
            R = RUN.run(d, log=q)
            calls = list(EPm.ToySpecRateEngine.CALLS)
            n_eval = len(R["_A"].ids) * len(R["_A"].pk)
            ok(calls and set(calls) == {1.25, 1.0} and calls.count(1.0) == n_eval,
               f"EP23 direct path: every engine run (calibration fit, point, draws, stability refits) receives the spec "
               f"value; only the decision test's one evaluation runs at the card value ({len(calls)} runs, "
               f"{calls.count(1.0)} at the card value, {n_eval} expected)")
            t = {x["test_id"]: x for x in R["decision_tests"]}.get("DTs") or {}
            ok(t.get("status") == "run" and (t.get("spec_value") or {}).get("card_value") == 1.0,
               f"EP24 the direct-path decision test records the spec and card values: {t.get('spec_value')}")
            u = R["uncertainty"]
            ok("held fixed at the spec value: scale" in u["label"], f"EP25 the direct-path label: {u['label']}")
    guard("EP23-EP25 direct path", direct_cases)

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
