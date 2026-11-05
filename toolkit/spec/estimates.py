#!/usr/bin/env python3
"""The `estimates` deliverable: a decision spec with no allocation units (design note v2d §2.3), fail closed.

    python3 toolkit/spec/estimates.py --self-test

Some questions ask for quantities, not a plan: a reproduction number, an attack rate, with their intervals and a
threshold statement. Their spec has no options, constraints, comparator or objective. It has one population (a
one-row units table, derived like any other), the engine's dynamics declaration, the observation sources with their
measurement models, a `posterior` calibration and the outputs:

    deliverable:
      kind: estimates
      windows: {<window>: [t0, t1]}                    # named windows in model years (inside the horizon)
      outputs:                                         # each an engine window metric, named for the report
        - {name: <reported name>, metric: <window metric>, window: <window>, scale: ratio | proportion | rate}
      decision:                                        # optional: a threshold rule computed by the harness
        {kind: threshold, output: <name>, threshold: 1.0, direction: above | below, probability: 0.95,
         statements: {yes: "...", no: "...", undetermined: "..."}}
    dynamics:                                          # what the engine configuration is built from (engine-agnostic)
      generation_interval: {csv, lag, probability, step: week | day, values: [w_1, ...]}   # values = the csv's
      transmissibility: constant | {knots: [t_1, ...]}
    observations:
      sources: [{id, measures, latent, frame, bias, noise, delay?}]   # toolkit/analysis/observation.py vocabulary;
                                                     # noise.dispersion: a number or {param, prior, source};
                                                     # delay: {pmf: [p_0, ...], source} on the engine's step grid
    calibration:
      method: posterior
      priors: {<engine parameter or unbound unit attribute>: {dist, range, source, verified}}
      targets: {csv, source: <source id>, step: <1-based step column>, observed: <count column>,
                frame: {band, source}}

`uncertainty.sources` lists `calibration` (the joint posterior) and `observation` (each source's measurement
parameters, carried by the same posterior); every other kind is excluded with a reason. Nothing here names a disease.
"""
from __future__ import annotations

import csv
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KIND = "estimates"
REQUIRED = ("schema_version", "question", "decision_maker", "decision_date", "engine", "horizon", "decision_units",
            "dynamics", "observations", "calibration", "uncertainty", "analysis", "deliverable")
NOT_APPLICABLE = ("options", "constraints", "comparator", "objective")
SCALES = ("ratio", "proportion", "rate")
DIRECTIONS = ("above", "below")
UQ_KINDS = ("calibration", "observation")
STEPS = ("week", "day")


def is_estimates(raw) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("deliverable"), dict) and raw["deliverable"].get("kind") == KIND


def statements(dec) -> dict:
    """The threshold rule's statements by verdict. YAML 1.1 reads the keys `yes` and `no` as booleans, so True and
    False are read as yes and no."""
    st = (dec or {}).get("statements") if isinstance(dec, dict) else None
    if not isinstance(st, dict):
        return {}
    return {("yes" if k is True else "no" if k is False else str(k)): v for k, v in st.items()}


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def gi_from_spec(raw, base=None):
    raise NotImplementedError("estimates deliverable: tests first; the implementation is the next commit")


def engine_config(raw):
    raise NotImplementedError("estimates deliverable: tests first; the implementation is the next commit")


def validate_pending(s):
    raise NotImplementedError("estimates deliverable: tests first; the implementation is the next commit")


def validate(s, eng):
    raise NotImplementedError("estimates deliverable: tests first; the implementation is the next commit")


def needs(raw):
    raise NotImplementedError("estimates deliverable: tests first; the implementation is the next commit")


# --------------------------------------------------------------------------- self-test
def toy_dir(d: str, gi=(0.5, 0.5), rows=None) -> str:
    """A complete estimates run directory for self-tests (the renewal engine, a synthetic series)."""
    import yaml
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    with open(os.path.join(d, "data", "pop.csv"), "w") as f:
        f.write("unit_id,population\nall,100000\n")
    with open(os.path.join(d, "data", "gi.csv"), "w") as f:
        f.write("lag,p\n" + "".join(f"{i + 1},{v}\n" for i, v in enumerate(gi)))
    rows = rows if rows is not None else [3, 5, 9, 15, 24, 37, 52, 66, 72, 66, 52, 36, 23, 14, 8, 4]
    with open(os.path.join(d, "data", "cases.csv"), "w") as f:
        f.write("week,cases\n" + "".join(f"{i + 1},{v}\n" for i, v in enumerate(rows)))
    spec = toy_spec(gi, len(rows))
    yaml.safe_dump(spec, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
    return os.path.join(d, "decision_spec.yaml")


def toy_spec(gi=(0.5, 0.5), n_weeks=16) -> dict:
    wk = 7.0 / 365.25
    return {
        "schema_version": 1, "question": "toy outbreak: reproduction number and attack rate",
        "decision_maker": "toy health office", "decision_date": 2026,
        "engine": {"ref": "toolkit.engines.renewal.engine:RenewalEngine",
                   "config": {"generation_interval": {"dt": "week", "w": list(gi)}, "dt": "week"}},
        "horizon": {"start": 0.0, "stop": n_weeks * wk + 1e-9},
        "decision_units": {"csv": "data/pop.csv", "id": "unit_id", "engine_attributes": {"N": {"column": "population"}},
                           "populations": {"all": {"column": "population", "age_groups": ["all"]}}},
        "dynamics": {"generation_interval": {"csv": "data/gi.csv", "lag": "lag", "probability": "p", "step": "week",
                                             "values": list(gi)}, "transmissibility": "constant"},
        "observations": {"sources": [{
            "id": "reports", "measures": "case_notifications", "latent": {"quantity": "incidence"},
            "frame": {"design": "administrative", "population": "the whole population"},
            "bias": [{"mechanism": "under_ascertainment", "param": "reporting",
                      "prior": {"dist": "loguniform", "range": [0.02, 0.5]}, "source": "toy"}],
            "noise": {"model": "negbin", "dispersion": {"param": "k", "prior": {"dist": "loguniform",
                                                                                "range": [2.0, 500.0]},
                                                        "source": "toy"}}}]},
        "calibration": {"method": "posterior",
                        "priors": {"r0": {"dist": "uniform", "range": [1.0, 4.0], "source": "toy"},
                                   "seed": {"dist": "loguniform", "range": [1e-5, 1e-2], "source": "toy"}},
                        "targets": {"csv": "data/cases.csv", "source": "reports", "step": "week", "observed": "cases",
                                    "frame": {"band": "incidence", "source": "toy"}}},
        "uncertainty": {"level": 0.95, "draws": 200, "seed": 5,
                        "sources": [{"kind": "calibration"}, {"kind": "observation"}]},
        "analysis": {"evaluation": "direct"},
        "deliverable": {"kind": "estimates", "windows": {"season": [0.0, n_weeks * wk + 1e-9]},
                        "outputs": [{"name": "reff_next", "metric": "reff_ref", "window": "season", "scale": "ratio"},
                                    {"name": "attack", "metric": "attack_rate", "window": "season",
                                     "scale": "proportion"}],
                        "decision": {"kind": "threshold", "output": "reff_next", "threshold": 1.0,
                                     "direction": "above", "probability": 0.95,
                                     "statements": {"yes": "a large outbreak is expected",
                                                    "no": "a large outbreak is not expected",
                                                    "undetermined": "the data do not settle it"}}}}


def _self_test() -> int:
    import copy
    import tempfile
    import yaml
    from toolkit.spec import schema
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        sp = toy_dir(d)
        s = schema.load(sp)
        errs = schema.validate(s)
        ok(errs == [], f"ES1 a complete estimates spec validates (no options, constraints, comparator, objective): {errs}")

        def mut(fn, needle, label):
            raw = copy.deepcopy(yaml.safe_load(open(sp)))
            fn(raw)
            p2 = os.path.join(d, "decision_spec.yaml")
            keep = open(sp).read()
            yaml.safe_dump(raw, open(p2, "w"), sort_keys=False)
            e = schema.validate(schema.load(p2))
            open(sp, "w").write(keep)
            ok(any(needle in x for x in e), f"{label}: {[x for x in e if needle in x][:1] or e[:2]}")
        mut(lambda r: r["dynamics"]["generation_interval"].update(values=[0.6, 0.4]), "differ from",
            "ES2 generation-interval values that differ from the cited file are refused")
        mut(lambda r: r.update(options={"components": {}}), "not part of an estimates spec",
            "ES3 an allocation section in an estimates spec is refused")
        mut(lambda r: r["calibration"]["priors"].pop("r0"), "need a prior",
            "ES4 a calibrated engine parameter without a prior is refused (no silent defaults)")
        mut(lambda r: r["calibration"]["priors"].update(N={"dist": "uniform", "range": [1, 2], "source": "x"}),
            "bound to data", "ES5 a prior on a unit attribute bound to data is refused")
        mut(lambda r: r["deliverable"]["outputs"][0].update(metric="zz"), "not an engine window metric",
            "ES6 an output that is not an engine window metric is refused")
        mut(lambda r: r["observations"]["sources"][0].update(bias=[{"mechanism": "fudge", "param": "f"}]),
            "bias_without_mechanism", "ES7 a free multiplier on a source is refused (closed vocabulary)")
        mut(lambda r: r["uncertainty"].update(sources=[{"kind": "calibration"}]), "kind observation",
            "ES8 the observation kind must be covered or excluded with a reason")
        mut(lambda r: r["deliverable"]["decision"]["statements"].update(yes="above 1.0"), "no numbers",
            "ES9 threshold statements carry no numbers")
        mut(lambda r: r["calibration"]["targets"].update(source="other"), "observation_source_undeclared",
            "ES10 a target that names no declared source is refused")
        mut(lambda r: r["observations"]["sources"][0].update(delay={"pmf": [0.5, 0.4], "source": "x"}), "delay",
            "ES11 a reporting delay that does not sum to 1 is refused")
        with open(os.path.join(d, "data", "pop.csv"), "a") as f:
            f.write("b,5\n")
        e = schema.validate(schema.load(sp))
        ok(any("one population" in x for x in e), f"ES12 an estimates spec has one population row: {e[:1]}")
    # allocation specs are untouched: the toy allocation spec still validates
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        ok(schema.validate(schema.load(p)) == [], "ES13 an allocation spec validates unchanged")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
