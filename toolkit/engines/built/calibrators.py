#!/usr/bin/env python3
"""Harness calibration wrapper for agent-built engine admission check A10
(docs/design/agent-built-engines.md §4.2, BE16).

    python3 toolkit/engines/built/calibrators.py --self-test

Sits under `toolkit/engines/` so `toolkit/import_guard.py`'s calibrator policy allows it.
`calibrate_as_run` dispatches through the run's own calibration path (`toolkit/analysis/core.py`):
- `posterior` -> `toolkit/analysis/mode_start.py:fit_posterior_panel`
- `likelihood` -> `toolkit/analysis/calibration.py:fit_likelihood`
- `emulator_inversion` -> `toolkit/analysis/calibration.py:invert` (through a direct `cal_grid` adapter)
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


def _direct_model_for_engine(spec):
    """Construct the direct evaluation model bound to `spec` (whose `_engine` is already set)."""
    from toolkit.analysis import model as M
    return M.DirectModel(spec)


class _DirectInversionModel:
    """Adapter supplying `cal_grid` and `by_id` to `calibration.invert` over a DirectModel."""

    def __init__(self, dm):
        from toolkit.analysis.batch import CAL_WINDOW
        from toolkit.engines.contract import Horizon

        self._dm = dm
        self.by_id = dm.by_id
        self.spec = dm.spec
        cal = dm.spec["calibration"]
        self._metric = cal["metric"]
        w = cal.get("window")
        base_h = dm.horizon
        wins = dict(base_h.windows or {})
        if w is not None and CAL_WINDOW not in wins:
            wins[CAL_WINDOW] = [float(w[0]), float(w[1])]
        elif CAL_WINDOW not in wins:
            wins[CAL_WINDOW] = [float(base_h.window[0]), float(base_h.window[1])]
        self._cal_horizon = Horizon(base_h.start, base_h.stop, base_h.window, wins)
        self._cal_window = CAL_WINDOW

    def cal_grid(self, ids, theta, zgrid):
        from toolkit.analysis.model import _logit, _metric

        zg = np.asarray(zgrid, dtype=float)
        knots = zg if len(zg) <= 9 else np.linspace(float(zg[0]), float(zg[-1]), 9)
        out = np.empty((len(ids), len(zg)), dtype=float)
        for j, uid in enumerate(ids):
            vals = []
            for z in knots:
                r = self._dm.run(theta, uid, float(z), self._dm.comp, horizon=self._cal_horizon)
                m = _metric({"windows": r.windows}, self._cal_window, self._metric, f"cal_grid unit {uid}")
                vals.append(float(_logit(np.array([m]))[0]))
            out[j] = vals if len(knots) == len(zg) else np.interp(zg, knots, vals)
        return out


def fisher_profile_classes(nll_fn, point: Mapping[str, float], bounds: Mapping[str, tuple[float, float]],
                           *, profile_points: int = 12) -> dict:
    """Run `scripts/identifiability.py` full Hessian eigen-decomposition + profile-likelihood
    classification on `nll_fn(params_dict) -> float` at `point`. Returns `{verdict, classes, ridges, details}`."""
    ID = _ident_module()
    pt = {str(k): float(v) for k, v in point.items()}
    bnds = {str(k): (float(bounds[k][0]), float(bounds[k][1])) for k in pt}
    fa = ID.fisher_analysis(nll_fn, pt, bnds)
    ridges = list(fa.get("ridges") or [])
    classes: dict[str, str] = {}
    details_params: dict[str, dict] = {}
    for name in sorted(pt):
        lo, hi = bnds[name]
        prof = ID.profile_likelihood(
            nll_fn, pt,
            {"name": name, "lower_bound": lo, "upper_bound": hi},
            n_points=int(profile_points),
            nuisance_bounds=bnds,
            maxiter=200,
        )
        ridge = next((r for r in ridges if name in (r.get("params") or [])), None)
        st, why = ID.classify(name, pt[name], (fa.get("se") or {}).get(name), prof, ridge)
        classes[name] = str(st)
        details_params[name] = {
            "status": str(st),
            "explanation": why,
            "estimate": pt[name],
            "se": (fa.get("se") or {}).get(name),
            "profile_ci": [prof.get("ci_low"), prof.get("ci_high")],
        }
    verdict = (
        "well_identified" if classes and all(v == "well_identified" for v in classes.values())
        else "unidentified" if any(v == "unidentified" for v in classes.values())
        else "weakly_identified"
    )
    return {
        "verdict": verdict,
        "classes": classes,
        "ridges": ridges,
        "details": {"parameters": details_params, "fisher": fa},
    }


def _extract_intervals_and_classes(cal_out: dict, level: float = 0.95) -> tuple[dict, dict, dict, list]:
    """Extract `{param: [lo, hi]}`, `{param: class}`, `{param: likelihood_class}`, and ridges
    from a `fit_posterior_panel` output."""
    from toolkit.analysis import calibration as C

    smp = (
        C.sample_of(cal_out)
        if isinstance(cal_out, dict) and isinstance(cal_out.get("sample"), dict)
        else cal_out.get("posterior")
        if isinstance(cal_out, dict) and isinstance(cal_out.get("posterior"), dict)
        else None
    )
    intervals: dict[str, list[float]] = {}
    classes: dict[str, str] = {}
    lik_classes: dict[str, str] = {}
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
        if isinstance(cls_doc.get("likelihood_classes"), dict):
            lik_classes.update({str(k): str(v) for k, v in cls_doc["likelihood_classes"].items()})
        if isinstance(cls_doc.get("ridges"), list):
            ridges = list(cls_doc["ridges"])
    if not intervals and isinstance(cal_out, dict) and isinstance(cal_out.get("theta"), dict):
        for k, v in cal_out["theta"].items():
            fv = float(v)
            intervals[str(k)] = [fv, fv]
    return intervals, classes, lik_classes, ridges


def _joint_prior_boxes(spec, engine, rep: Mapping) -> dict[str, tuple[float, float]]:
    """Collect prior/search box bounds `(lo, hi)` for each parameter in a joint calibration report."""
    boxes: dict[str, tuple[float, float]] = {}
    for src in (rep.get("box"), (rep.get("region") or {}).get("box")):
        if isinstance(src, Mapping):
            for k, v in src.items():
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    lo, hi = float(v[0]), float(v[1])
                    if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
                        boxes[str(k)] = (lo, hi)
    for p in engine.parameters():
        if p.name not in boxes and p.lo is not None and p.hi is not None and float(p.hi) > float(p.lo):
            boxes[str(p.name)] = (float(p.lo), float(p.hi))
    cal = spec.raw.get("calibration") if isinstance(spec.raw, Mapping) else {}
    z_rng = cal.get("range") if isinstance(cal, Mapping) else None
    if isinstance(z_rng, (list, tuple)) and len(z_rng) == 2 and float(z_rng[1]) > float(z_rng[0]):
        zlo, zhi = float(z_rng[0]), float(z_rng[1])
        reg = (rep.get("region") or {}).get("ranges") or {}
        for k in reg:
            if str(k).startswith("z:") and str(k) not in boxes:
                boxes[str(k)] = (zlo, zhi)
    return boxes


def calibrate_as_run(make_engine, data: Mapping, config: str | None = None, level: float = 0.95,
                     seed: int = 0, **decl) -> dict:
    """Coverage-runner compatible calibrator dispatching through the run's own calibration path
    (`toolkit/analysis/core.py`: `posterior` -> `mode_start.fit_posterior_panel`, `likelihood` ->
    `calibration.fit_likelihood`, `emulator_inversion` -> `calibration.invert`, `joint` ->
    `joint_calibration.run`). Returns `{intervals, classes, likelihood_classes, ridges, method, raw}`."""
    from toolkit.analysis import calibration as C
    from toolkit.analysis import mode_start as MS
    from toolkit.spec import schema

    engine = make_engine(config) if callable(make_engine) else make_engine
    spec_raw = copy.deepcopy(decl.get("spec_raw") or data.get("spec_raw"))
    if not isinstance(spec_raw, dict):
        raise ValueError("calibrate_as_run requires `spec_raw` (the signed spec mapping)")
    base_dir = str(decl.get("base_dir") or data.get("base_dir") or os.getcwd())
    spec = schema.Spec(spec_raw, base_dir)
    spec.engine_pending = False
    spec._engine = engine
    spec._engine_ref = None
    cal_cfg = spec_raw.get("calibration") or {}
    method = str(decl.get("method") or ("joint" if cal_cfg.get("joint") else cal_cfg.get("method", "none")))
    rows = data.get("rows")
    fixed_z = data.get("fixed_z")

    if method == "posterior":
        model = _direct_model_for_engine(spec)
        out = MS.fit_posterior_panel(spec, model, rows=rows, fixed_z=fixed_z, seed=int(seed), sample_it=True)
        intervals, classes, lik_classes, ridges = _extract_intervals_and_classes(out, level=level)
        if not classes and isinstance(out.get("theta"), dict) and out.get("theta"):
            P, _ = C.panel_problem(spec, model, rows=rows, fixed_z=fixed_z)
            if P is not None and P.names:
                bnds = {n: tuple(P.priors[n].support()) for n in P.names}
                pt = {n: float(out["theta"].get(n, 0.5 * (bnds[n][0] + bnds[n][1]))) for n in P.names}
                fp = fisher_profile_classes(
                    lambda d: float(-P.evaluate(np.asarray([[float(d[n]) for n in P.names]], float))[0][0]),
                    pt, bnds,
                )
                classes = fp["classes"]
                ridges = ridges or fp["ridges"]
        return {
            "intervals": intervals,
            "classes": classes,
            "likelihood_classes": lik_classes,
            "ridges": ridges,
            "method": "posterior",
            "raw": out,
        }

    if method == "likelihood":
        model = _direct_model_for_engine(spec)
        try:
            out = C.fit_likelihood(spec, model, rows=rows, fixed_z=fixed_z, identifiability=True)
        except C.CalibrationRefused as exc:
            if getattr(exc, "code", None) == "calibration_ridge":
                pnames = [str(p) for p in (cal_cfg.get("parameters") or [])]
                return {
                    "intervals": {},
                    "classes": {p: "unidentified" for p in pnames},
                    "likelihood_classes": {p: "on_ridge" for p in pnames},
                    "ridges": [{"params": pnames, "reason": str(exc)}],
                    "method": "likelihood",
                    "refused": exc.code,
                    "raw": {"error": str(exc)},
                }
            raise
        theta = out.get("theta") or {}
        ident = out.get("identifiability") or {}
        raw_params = ident.get("parameters")
        if isinstance(raw_params, Mapping):
            param_items = [(str(k), v) for k, v in raw_params.items() if isinstance(v, Mapping)]
        elif isinstance(raw_params, list):
            param_items = [(str(p["name"]), p) for p in raw_params if isinstance(p, Mapping) and p.get("name")]
        else:
            param_items = []
        classes = {pname: str(info.get("status", "unidentified")) for pname, info in param_items}
        intervals: dict[str, list[float]] = {}
        for pname, info in param_items:
            pr = info.get("profile_ci") if info.get("profile_ci") is not None else info.get("profile_range_95")
            if (isinstance(pr, (list, tuple)) and len(pr) == 2
                    and pr[0] is not None and pr[1] is not None
                    and math.isfinite(float(pr[0])) and math.isfinite(float(pr[1]))):
                intervals[pname] = [float(pr[0]), float(pr[1])]
            elif pname in theta:
                v = float(theta[pname])
                se_raw = info.get("se") if info.get("se") is not None else info.get("fisher_se")
                se = float(se_raw) if se_raw is not None else 0.0
                z = 1.959963984540054
                intervals[pname] = [v - z * se, v + z * se] if math.isfinite(se) and se > 0 else [v, v]
        return {
            "intervals": intervals,
            "classes": classes,
            "likelihood_classes": dict(classes),
            "ridges": list(ident.get("ridges") or []),
            "method": "likelihood",
            "raw": out,
        }

    if method == "emulator_inversion":
        dm = _direct_model_for_engine(spec)
        inv_model = _DirectInversionModel(dm)
        theta0 = {p.name: p.value for p in engine.parameters()}
        inv = C.invert(spec, inv_model, theta0)
        intervals = {f"z:{u}": [float(z), float(z)] for u, (z, _cl) in inv.items() if z is not None}
        classes = {f"z:{u}": ("unidentified" if cl else "well_identified") for u, (_z, cl) in inv.items()}
        return {
            "intervals": intervals,
            "classes": classes,
            "likelihood_classes": dict(classes),
            "ridges": [],
            "method": "emulator_inversion",
            "raw": inv,
        }

    if method == "joint":
        from toolkit.analysis import joint_calibration as JC

        with tempfile.TemporaryDirectory() as td:
            rep = JC.run(spec, td, log=lambda *a, **k: None)
        reg = (rep.get("region") or {}).get("ranges") or {}
        intervals = {
            str(k): [float(v[0]), float(v[1])]
            for k, v in reg.items()
            if isinstance(v, (list, tuple)) and len(v) == 2
        }
        boxes = _joint_prior_boxes(spec, engine, rep)
        classes: dict[str, str] = {}
        for k, (lo, hi) in intervals.items():
            if k in boxes:
                b_lo, b_hi = boxes[k]
                frac = (hi - lo) / max(b_hi - b_lo, 1e-12)
                if frac >= 0.90:
                    classes[k] = "prior_dominated"
                elif frac >= 0.50:
                    classes[k] = "partly_identified"
                else:
                    classes[k] = "well_identified"
            else:
                classes[k] = "partly_identified"
        blocked = bool(rep.get("blocked"))
        for u in (rep.get("unmatched") or []):
            classes[f"z:{u}"] = "unidentified"
        for u in (rep.get("conflicting") or []):
            classes[f"z:{u}"] = "unidentified"
        return {
            "intervals": intervals,
            "classes": classes,
            "likelihood_classes": dict(classes),
            "ridges": [],
            "blocked": blocked,
            "blocks": list(rep.get("blocks") or []),
            "unmatched": list(rep.get("unmatched") or []),
            "method": "joint",
            "raw": rep,
        }

    raise ValueError(f"calibrate_as_run: unsupported calibration method {method!r}")


def _self_test() -> int:
    from unittest import mock
    import yaml
    from toolkit.analysis import calibration as C
    from toolkit.analysis import joint_calibration as JC
    from toolkit.analysis import mode_start as MS
    from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec
    from toolkit.spec import schema

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # BE16a: calibrate_as_run under method=posterior clears engine_pending, constructs DirectModel(spec),
    # and extracts both classes and likelihood_classes from fit_posterior_panel.
    called = {"n": 0}

    def fake_fit(spec, model, rows=None, fixed_z=None, fixed_params=None, seed=None, sample_it=True):
        called["n"] += 1
        assert not spec.engine_pending and model.eng is not None
        return {
            "theta": {"p1": 0.5, "p2": 0.5},
            "z": {"u1": None},
            "fixed_z": {"u1": None},
            "sample": {
                "sample_id": "toy-s1",
                "names": ["p1", "p2"],
                "rows": [[0.4, 0.6], [0.5, 0.5], [0.6, 0.4], [0.45, 0.55]],
            },
            "identifiability": {
                "classes": {"p1": "prior_dominated", "p2": "prior_dominated"},
                "likelihood_classes": {"p1": "on_ridge", "p2": "on_ridge"},
                "ridges": [{"params": ["p1", "p2"]}],
            },
        }

    class _RidgeToy:
        def describe(self):
            return EngineCard(name="toy_ridge", version="1", mechanisms=["product"],
                              components={"a": [0, 1], "b": [0, 1]},
                              unit_attributes={"x": "x", "cov": "cov", "pop": "pop"},
                              outputs={"per_capita": ["cases"], "windows": ["prev"], "cost_drivers": ["a_doses"]},
                              age_groups=["all"], valid_ranges={"x": [-2.0, 2.0]})

        def parameters(self):
            return [
                ParamSpec("p1", 0.5, "calibrated", lo=0.1, hi=1.0, source="assumption"),
                ParamSpec("p2", 0.5, "calibrated", lo=0.1, hi=1.0, source="assumption"),
            ]

        def run(self, params, unit, package, horizon, seed):
            p1 = float(params.get("p1", 0.5))
            p2 = float(params.get("p2", 0.5))
            x = float(unit.attributes.get("x", 0.0))
            prev = 1.0 / (1.0 + math.exp(-x))
            val = p1 * p2
            wins = {w: {"prev": prev} for w in (horizon.windows or {"cal": None})}
            return Outcomes(per_capita={"cases": {"all": val}}, windows=wins,
                            cost_drivers={"a_doses": 0.0}, meta={"age_shares": {"all": 1.0}})

    with tempfile.TemporaryDirectory() as td:
        sp_path = schema.toy_spec(td)
        sp_raw = yaml.safe_load(open(sp_path))
        sp_raw["calibration"] = {"method": "posterior", "attribute": "x", "range": [-2.0, 2.0],
                                 "metric": "prev", "target_column": "obs_prev"}
        # Pending build spec must NOT block calibrate_as_run
        sp_build = copy.deepcopy(sp_raw)
        sp_build["engine"] = {"build": {"id": "toy_ridge", "kind": "closed_form", "why": "test", "mechanisms": ["m"], "request": {}}}
        with mock.patch.object(MS, "fit_posterior_panel", side_effect=fake_fit):
            res = calibrate_as_run(lambda _cfg=None: _RidgeToy(), {"spec_raw": sp_build, "base_dir": td}, seed=3)
    ok(called["n"] == 1 and res["method"] == "posterior"
       and res["classes"] == {"p1": "prior_dominated", "p2": "prior_dominated"}
       and res["likelihood_classes"] == {"p1": "on_ridge", "p2": "on_ridge"}
       and "p1" in res["intervals"] and "p2" in res["intervals"],
       f"BE16a posterior wrapper handles engine_pending build specs and returns classes+likelihood_classes ({res['classes']})")

    # BE16b: real scripts/identifiability.py Fisher + profile-likelihood classifies both a quadratic
    # (well_identified) and a pure product ridge (unidentified)
    quad = fisher_profile_classes(lambda d: 0.5 * ((float(d["x"]) - 0.5) / 0.02) ** 2,
                                  {"x": 0.5}, {"x": (0.1, 1.0)})

    def ridge_nll(d):
        prod = float(d["p1"]) * float(d["p2"])
        return 0.5 * ((prod - 0.25) / 0.02) ** 2

    fp = fisher_profile_classes(ridge_nll, {"p1": 0.5, "p2": 0.5}, {"p1": (0.1, 1.0), "p2": (0.1, 1.0)})
    ok(quad["classes"].get("x") == "well_identified"
       and fp["classes"].get("p1") == "unidentified" and fp["classes"].get("p2") == "unidentified"
       and bool(fp["ridges"]),
       f"BE16b fisher_profile_classes classifies quadratic ({quad['classes']}) and product ridge ({fp['classes']})")

    # BE16c: likelihood wrapper extracts dict-keyed identifiability.parameters + profile_ci
    with tempfile.TemporaryDirectory() as td:
        sp_path = schema.toy_spec(td)
        sp_raw = yaml.safe_load(open(sp_path))
        sp_raw["calibration"] = {"method": "likelihood"}
        fake_lik = {
            "theta": {"p1": 0.5},
            "identifiability": {
                "parameters": {"p1": {"status": "well_identified", "profile_ci": [0.42, 0.58], "se": 0.04}},
                "ridges": [],
            },
        }
        with mock.patch.object(C, "fit_likelihood", return_value=fake_lik):
            lres = calibrate_as_run(lambda _cfg=None: _RidgeToy(), {"spec_raw": sp_raw, "base_dir": td})
    ok(lres["classes"] == {"p1": "well_identified"} and lres["intervals"]["p1"] == [0.42, 0.58],
       f"BE16c likelihood wrapper extracts dict-keyed parameters and profile_ci ({lres['classes']}, {lres['intervals']})")

    # BE16d: emulator_inversion executes C.invert via _DirectInversionModel.cal_grid
    with tempfile.TemporaryDirectory() as td:
        sp_path = schema.toy_spec(td)
        sp_raw = yaml.safe_load(open(sp_path))
        sp_raw["calibration"]["method"] = "emulator_inversion"
        sp_raw["calibration"]["attribute"] = "x"
        sp_raw["calibration"]["range"] = [-2.0, 2.0]
        sp_raw["calibration"]["metric"] = "prev"
        ires = calibrate_as_run(lambda _cfg=None: _RidgeToy(), {"spec_raw": sp_raw, "base_dir": td})
    ok(ires["method"] == "emulator_inversion" and bool(ires["intervals"]) and all(
        v in ("well_identified", "unidentified") for v in ires["classes"].values()),
       f"BE16d emulator_inversion runs C.invert through _DirectInversionModel ({len(ires['intervals'])} units)")

    # BE16e: joint wrapper detects zero contraction (prior_dominated) and propagates blocked
    with tempfile.TemporaryDirectory() as td:
        sp_path = schema.toy_spec(td)
        sp_raw = yaml.safe_load(open(sp_path))
        sp_raw["calibration"]["joint"] = {"shared": ["p1", "p2"]}
        fake_jc = {
            "blocked": True,
            "blocks": [{"code": "unmatched_units"}],
            "unmatched": ["u0"],
            "region": {"ranges": {"p1": [0.1, 1.0], "p2": [0.45, 0.55]}},
        }
        with mock.patch.object(JC, "run", return_value=fake_jc):
            jres = calibrate_as_run(lambda _cfg=None: _RidgeToy(), {"spec_raw": sp_raw, "base_dir": td})
    ok(jres["blocked"] is True and jres["classes"]["p1"] == "prior_dominated"
       and jres["classes"]["p2"] == "well_identified" and jres["classes"]["z:u0"] == "unidentified",
       f"BE16e joint wrapper classifies zero contraction as prior_dominated and propagates blocked ({jres['classes']})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
