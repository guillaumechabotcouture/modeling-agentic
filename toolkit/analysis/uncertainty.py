"""Uncertainty over the declared sources only, labelled with what it covers (spike lesson 6).

Each draw samples the spec's `uncertainty.sources`, in this order (the spike's order for
its two sources, so the stored spike batch reproduces):
  engine_parameters  every fitted engine parameter, uniform within its engine-card range
  calibration        emulator_inversion: a per-region common-mode shift of the logit target
                     (sd `calibration.target_uncertainty.region_sd_logit`), then re-inversion;
                     likelihood: (calibrated parameters, unit attributes) from N(estimate, covariance)
  costs              a uniform multiplier per listed cost item
  emulator           reductions perturbed by their held-out RMSE
The point plan is evaluated in every draw (the headline interval) and the plan is
re-optimised in every draw (package stability). The interval's label comes from the spec
(`Spec.uncertainty_scope`): what it covers and what it does not. Writes
`uncertainty_report.yaml` in the schema of scripts/propagate_uncertainty.py (shared
summariser), plus the scope.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import yaml

from toolkit.analysis.core import q

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRODUCER = "toolkit/analysis/uncertainty.py"
ORDER = ("engine_parameters", "calibration", "costs", "emulator")


def _pu():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import propagate_uncertainty
    return propagate_uncertainty


class Drawer:
    """Draws the declared sources; `draw()` returns (theta, zs, noise, cost multipliers, sampled values)."""

    def __init__(self, A, seed):
        self.A = A
        sp = A.spec["uncertainty"]
        self.src = {s["kind"]: s for s in sp.get("sources") or []}
        self.rng = np.random.default_rng(int(seed))
        cal = A.spec["calibration"]
        self.sd = float((cal.get("target_uncertainty") or {}).get("region_sd_logit", 0.0))
        self.regions = sorted({u[A.region] for u in A.spec.units()}) if A.region else []
        if A.method == "likelihood" and "calibration" in self.src:
            if A.lik.get("cov") is None:
                raise ValueError("uncertainty: the calibration covariance is singular; the calibration source cannot "
                                 "be drawn (see analysis/calibration.yaml identifiability)")
            self.mvn = (np.array([A.lik["x"][n] for n in A.lik["names"]]), np.array(A.lik["cov"]))

    def draw(self):
        A = self.A
        theta, sampled = dict(A.theta0), {}
        shifts, zs, noise, mult = None, None, None, None
        for kind in ORDER:
            if kind not in self.src:
                continue
            if kind == "engine_parameters":
                for k, v in A.fr.items():
                    theta[k] = float(self.rng.uniform(v[0], v[1]))
                    sampled[k] = theta[k]
            elif kind == "calibration" and A.method == "emulator_inversion":
                shifts = {r: float(self.rng.normal(0, self.sd)) for r in self.regions}
                sampled.update({f"shift:{r}": v for r, v in shifts.items()})
            elif kind == "calibration" and A.method == "likelihood":
                mu, cov = self.mvn
                x = dict(zip(A.lik["names"], self.rng.multivariate_normal(mu, cov)))
                for p in A.lik["params"]:
                    theta[p] = float(x[p])
                    sampled[p] = theta[p]
                zs = {i: float(x.get(f"z:{i}", A.lik["fixed_z"].get(i))) for i in A.ids}
            elif kind == "costs":
                mult = {it: float(self.rng.uniform(lo, hi)) for it, (lo, hi) in self.src["costs"]["items"].items()}
                sampled.update({f"cost:{k}": v for k, v in mult.items()})
            elif kind == "emulator":
                noise = self.rng
        if zs is None:
            cz = A.calibrate(theta, shifts)
            zs = {i: v[0] for i, v in cz.items()}
        return theta, zs, noise, mult, sampled


def run(A, plan, headline_name: str, draws=None, seed=None) -> dict:
    sp = A.spec["uncertainty"]
    nd = int(draws if draws is not None else sp.get("draws", 32))
    scope = A.spec.uncertainty_scope()
    D = Drawer(A, seed if seed is not None else sp.get("seed", 42))
    fixed = {o: [] for o in A.outcomes}
    opt_vals, same = [], np.zeros(len(A.ids))
    comps_used, outs, samples = set(), [], {}
    corr_any = corr_plan = 0
    hl_outcome = A.spec["deliverable"]["headline"]["outcome"]
    for d in range(nd):
        theta, zs, noise, mult, sampled = D.draw()
        out, ben, cost = A.evaluate(theta, zs, noise=noise, cost_mult=mult)
        corr_any += bool(A.last_events)
        corr_plan += bool(A.plan_corrections(A.last_events, plan))
        row = {}
        for o in A.outcomes:
            v = sum(out[i]["y"][A.comp][o] - out[i]["y"][plan[i]][o] for i in A.ids)
            fixed[o].append(v)
            row[f"{o}_averted"] = v
        row[headline_name] = row[f"{hl_outcome}_averted"]
        sol = A.solve(ben, cost)
        opt_vals.append(sol["objective"])
        row["reoptimised_objective"] = sol["objective"]
        same += np.array([sol["choice"][i] == plan[i] for i in A.ids])
        for i in A.ids:
            comps_used |= set(A.pk[sol["choice"][i]])
            row[f"package:{i}"] = sol["choice"][i]
        outs.append(row)
        for k, v in sampled.items():
            samples.setdefault(k, []).append(v)
    lvl = scope["level"]
    data = {"draws": nd, "seed": int(sp.get("seed", 42)) if seed is None else int(seed),
            "covers": scope["covers"], "excludes": scope["excludes"], "label": scope["label"],
            "fixed_plan": {o: q(v, lvl) for o, v in fixed.items()},
            "reoptimised_objective": q(opt_vals, lvl),
            "units_same_package_share_median": float(np.median(same / max(nd, 1))) if nd else None,
            "units_same_package_in_90pct_of_draws": int(np.sum(same / max(nd, 1) >= 0.9)) if nd else None,
            "components_in_any_draw_plan": sorted(comps_used), "interval": scope["interval"],
            "model_corrections": {"draws_with_corrections": corr_any, "draws_with_corrections_on_plan": corr_plan},
            "headline": {"name": headline_name, "outcome": hl_outcome}}
    agg = _pu().summarise_outputs(outs, samples, nd, level=scope["level"])   # the signed level (Codex P1-11)
    report = {"producer": PRODUCER, "produced_by": "harness", "n_draws": nd, "n_draws_requested": nd,
              "n_successful": nd - len(agg["errors"]), "n_failed": len(agg["errors"]), "seed": data["seed"],
              "interval_label": scope["label"], "interval": scope["interval"], "covers": scope["covers"],
              "excludes": scope["excludes"],
              "degenerate_outputs": agg["degenerate_outputs"],
              "sampler_varied_parameters": len(agg["varied_params"]),
              "scalar_outputs": agg["scalar_outputs"], "categorical_outputs": agg["categorical_outputs"],
              "parameter_samples": agg["parameter_samples"], "backend": "local"}
    return {"data": data, "report": report}


def write_report(run_dir, report) -> str:
    p = os.path.join(run_dir, "uncertainty_report.yaml")
    from toolkit.analysis.artifacts import plain
    with open(p, "w") as f:
        yaml.safe_dump(plain(report), f, sort_keys=False)
    return p
