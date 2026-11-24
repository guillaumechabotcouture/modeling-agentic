#!/usr/bin/env python3
"""Harness calibration wrapper for agent-built engine admission check A10
(docs/design/agent-built-engines.md §4.2, BE16).

    python3 toolkit/engines/built/calibrators.py --self-test

Sits under `toolkit/engines/` so `toolkit/import_guard.py`'s calibrator policy allows it.
`calibrate_as_run` dispatches through the run's own calibration path (`toolkit/analysis/core.py`):
- `posterior` -> `toolkit/analysis/mode_start.py:fit_posterior_panel`
- `likelihood` -> `toolkit/analysis/calibration.py:fit_likelihood`
- `emulator_inversion` -> `toolkit/analysis/calibration.py:invert`
- `joint` -> `toolkit/analysis/joint_calibration.py:run`
and supplements with `scripts/identifiability.py` Fisher-Hessian + profile-likelihood identifiability
classes so ridge pairs are classified `unidentified`. Nothing here names a disease.
"""
from __future__ import annotations

import copy
import math
import os
import sys
import tempfile
from collections.abc import Mapping

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _ident_module():
    import identifiability as ID
    return ID


def _direct_model_for_engine(spec, engine):
    """Construct the direct evaluation model bound to `spec` and `engine`."""
    from toolkit.analysis import model as M
    return M.DirectModel(spec, engine)


def fisher_profile_classes(nll_fn, point: Mapping[str, float], bounds: Mapping[str, tuple[float, float]],
                           *, profile_points: int = 12) -> dict:
    """Run `scripts/identifiability.py` full Hessian eigen-decomposition + profile-likelihood
    classification on `nll_fn(params_dict) -> float` at `point`. Returns `{classes, ridges, details}`."""
    ID = _ident_module()
    param_specs = []
    for name in sorted(point):
        lo, hi = bounds[name]
        param_specs.append({
            "name": name,
            "point_estimate": float(point[name]),
            "lower_bound": float(lo),
            "upper_bound": float(hi),
            "profile_n_points": int(profile_points),
        })
    res = ID.analyze(nll_fn, param_specs, loss_type="nll", profile_mode="full")
    classes = {p["name"]: str(p.get("status", "unidentified")) for p in (res.get("parameters") or [])}
    return {
        "verdict": res.get("verdict"),
        "classes": classes,
        "ridges": list(res.get("ridges") or []),
        "details": res,
    }


def _extract_intervals_and_classes(cal_out: dict, level: float = 0.95) -> tuple[dict, dict, list]:
    """Extract `{param: [lo, hi]}`, `{param: class}`, and ridges from a `fit_posterior_panel` output."""
    from toolkit.analysis import calibration as C
    smp = C.sample_of(cal_out) if isinstance(cal_out, dict) else None
    intervals: dict[str, list[float]] = {}
    classes: dict[str, str] = {}
    ridges: list = []
    if isinstance(smp, dict) and smp.get("names") and smp.get("rows") is not None:
        names = list(smp["names"])
        rows = np.asarray(smp["rows"], dtype=float)
        for j, name in enumerate(names):
            intervals[str(name)] = [float(x) for x in C.interval(rows[:, j], level=level)]
    cls_doc = (cal_out.get("identifiability") or cal_out.get("classification") or {}) if isinstance(cal_out, dict) else {}
    if isinstance(cls_doc, dict):
        if isinstance(cls_doc.get("classes"), dict):
            classes.update({str(k): str(v) for k, v in cls_doc["classes"].items()})
        if isinstance(cls_doc.get("ridges"), list):
            ridges = list(cls_doc["ridges"])
    # Also check top-level `theta` if intervals weren't populated
    if not intervals and isinstance(cal_out, dict) and isinstance(cal_out.get("theta"), dict):
        for k, v in cal_out["theta"].items():
            fv = float(v)
            intervals[str(k)] = [fv, fv]
    return intervals, classes, ridges


def calibrate_as_run(make_engine, data: Mapping, config: str | None = None, level: float = 0.95,
                     seed: int = 0, **decl) -> dict:
    """Coverage-runner compatible calibrator dispatching through the run's own calibration path
    (`toolkit/analysis/core.py`: `posterior` -> `mode_start.fit_posterior_panel`, `likelihood` ->
    `calibration.fit_likelihood`, `emulator_inversion` -> `calibration.invert`, `joint` ->
    `joint_calibration.run`). Returns `{intervals, classes, ridges, method, raw}`."""
    from toolkit.analysis import calibration as C
    from toolkit.analysis import mode_start as MS
    from toolkit.spec import schema

    engine = make_engine(config) if callable(make_engine) else make_engine
    spec_raw = copy.deepcopy(decl.get("spec_raw") or data.get("spec_raw"))
    if not isinstance(spec_raw, dict):
        raise ValueError("calibrate_as_run requires `spec_raw` (the signed spec mapping)")
    base_dir = str(decl.get("base_dir") or data.get("base_dir") or os.getcwd())
    spec = schema.Spec(spec_raw, base_dir)
    spec._engine = engine
    spec._engine_ref = None
    cal_cfg = spec_raw.get("calibration") or {}
    method = str(decl.get("method") or ("joint" if cal_cfg.get("joint") else cal_cfg.get("method", "none")))
    rows = data.get("rows")
    fixed_z = data.get("fixed_z")

    if method == "posterior":
        model = _direct_model_for_engine(spec, engine)
        out = MS.fit_posterior_panel(spec, model, rows=rows, fixed_z=fixed_z, seed=int(seed), sample_it=True)
        intervals, classes, ridges = _extract_intervals_and_classes(out, level=level)
        # Merge scripts/identifiability.py Fisher/profile classes when a ridge or weak direction is present
        if not classes and isinstance(out.get("theta"), dict) and out.get("theta"):
            P, _ = C.panel_problem(spec, model, rows=rows, fixed_z=fixed_z)
            if P is not None and P.names:
                bnds = {n: (float(pr["lo"]), float(pr["hi"])) for n, pr in zip(P.names, P.priors)}
                pt = {n: float(out["theta"].get(n, 0.5 * (bnds[n][0] + bnds[n][1]))) for n in P.names}
                fp = fisher_profile_classes(
                    lambda d: float(-P.evaluate(np.asarray([[float(d[n]) for n in P.names]], float))[0][0]),
                    pt, bnds,
                )
                classes = fp["classes"]
                ridges = ridges or fp["ridges"]
        return {"intervals": intervals, "classes": classes, "ridges": ridges, "method": "posterior", "raw": out}

    if method == "likelihood":
        model = _direct_model_for_engine(spec, engine)
        out = C.fit_likelihood(spec, model, rows=rows, fixed_z=fixed_z, identifiability=True)
        theta = out.get("theta") or {}
        ident = out.get("identifiability") or {}
        classes = {p["name"]: str(p.get("status", "unidentified"))
                   for p in (ident.get("parameters") or []) if isinstance(p, dict)}
        intervals = {}
        for p in (ident.get("parameters") or []):
            if isinstance(p, dict) and p.get("name"):
                pr = p.get("profile_range_95")
                if isinstance(pr, (list, tuple)) and len(pr) == 2 and all(math.isfinite(float(x)) for x in pr):
                    intervals[p["name"]] = [float(pr[0]), float(pr[1])]
                elif p["name"] in theta:
                    v = float(theta[p["name"]])
                    se = float(p.get("fisher_se") or 0.0)
                    z = 1.959963984540054
                    intervals[p["name"]] = [v - z * se, v + z * se] if math.isfinite(se) and se > 0 else [v, v]
        return {"intervals": intervals, "classes": classes, "ridges": list(ident.get("ridges") or []),
                "method": "likelihood", "raw": out}

    if method == "emulator_inversion":
        model = _direct_model_for_engine(spec, engine)
        theta0 = {p.name: p.value for p in engine.parameters()}
        inv = C.invert(spec, model, theta0)
        intervals = {f"z:{u}": [float(z), float(z)] for u, (z, _cl) in inv.items() if z is not None}
        classes = {f"z:{u}": ("unidentified" if cl else "well_identified") for u, (_z, cl) in inv.items()}
        return {"intervals": intervals, "classes": classes, "ridges": [], "method": "emulator_inversion", "raw": inv}

    if method == "joint":
        from toolkit.analysis import joint_calibration as JC
        with tempfile.TemporaryDirectory() as td:
            rep = JC.run(spec, td, log=lambda *a, **k: None)
        reg = (rep.get("region") or {}).get("ranges") or {}
        intervals = {str(k): [float(v[0]), float(v[1])] for k, v in reg.items()
                     if isinstance(v, (list, tuple)) and len(v) == 2}
        classes = {k: "well_identified" for k in intervals}
        return {"intervals": intervals, "classes": classes, "ridges": [], "method": "joint", "raw": rep}

    raise ValueError(f"calibrate_as_run: unsupported calibration method {method!r}")


def _self_test() -> int:
    from unittest import mock
    from toolkit.analysis import mode_start as MS
    from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec
    from toolkit.spec import schema

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # BE16a: calibrate_as_run under calibration.method='posterior' dispatches to mode_start.fit_posterior_panel
    called = {"n": 0}

    def fake_fit(spec, model, rows=None, fixed_z=None, fixed_params=None, seed=None, sample_it=True):
        called["n"] += 1
        return {
            "theta": {"p1": 0.5, "p2": 0.5},
            "z": {"u1": None},
            "fixed_z": {"u1": None},
            "posterior": {
                "names": ["p1", "p2"],
                "rows": [[0.4, 0.6], [0.5, 0.5], [0.6, 0.4], [0.45, 0.55]],
            },
            "identifiability": {
                "classes": {"p1": "unidentified", "p2": "unidentified"},
                "ridges": [["p1", "p2"]],
            },
        }

    class _RidgeToy:
        def describe(self):
            return EngineCard(name="toy_ridge", version="1", mechanisms=["product"],
                              components={"a": [0, 1]}, unit_attributes={"x": "x"},
                              outputs={"per_capita": ["cases"], "windows": ["prev"], "cost_drivers": ["a_doses"]},
                              age_groups=["all"], valid_ranges={"x": [0.0, 1.0]})

        def parameters(self):
            return [
                ParamSpec("p1", 0.5, "calibrated", lo=0.1, hi=1.0, source="assumption"),
                ParamSpec("p2", 0.5, "calibrated", lo=0.1, hi=1.0, source="assumption"),
            ]

        def run(self, params, unit, package, horizon, seed):
            p1 = float(params.get("p1", 0.5))
            p2 = float(params.get("p2", 0.5))
            val = p1 * p2
            return Outcomes(per_capita={"cases": {"all": val}}, windows={"w": {"prev": val}},
                            cost_drivers={"a_doses": 0.0}, meta={"age_shares": {"all": 1.0}})

    with tempfile.TemporaryDirectory() as td:
        sp_path = schema.toy_spec(td)
        import yaml
        sp_raw = yaml.safe_load(open(sp_path))
        sp_raw["calibration"] = {"method": "posterior"}
        with mock.patch.object(MS, "fit_posterior_panel", side_effect=fake_fit):
            res = calibrate_as_run(lambda _cfg=None: _RidgeToy(), {"spec_raw": sp_raw, "base_dir": td}, seed=3)
    ok(called["n"] == 1 and res["method"] == "posterior"
       and res["classes"] == {"p1": "unidentified", "p2": "unidentified"}
       and "p1" in res["intervals"] and "p2" in res["intervals"],
       f"BE16a calibrate_as_run under method=posterior calls fit_posterior_panel and returns intervals/classes ({res['classes']})")

    # BE16b: real scripts/identifiability.py Fisher + profile-likelihood classifies a pure product ridge (p1 * p2) as unidentified
    def ridge_nll(d):
        prod = float(d["p1"]) * float(d["p2"])
        return 0.5 * ((prod - 0.25) / 0.02) ** 2

    fp = fisher_profile_classes(ridge_nll, {"p1": 0.5, "p2": 0.5}, {"p1": (0.1, 1.0), "p2": (0.1, 1.0)})
    ok(fp["classes"].get("p1") == "unidentified" and fp["classes"].get("p2") == "unidentified"
       and bool(fp["ridges"]),
       f"BE16b scripts/identifiability.py classifies ridge pair (p1*p2) as unidentified ({fp['classes']}, ridges={fp['ridges']})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
