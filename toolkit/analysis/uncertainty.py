"""Uncertainty over the declared sources only, labelled with what it covers (spike lesson 6).

Each draw samples the spec's `uncertainty.sources`, in this order (the spike's order for
its two sources, so the stored spike batch reproduces):
  engine_parameters  every fitted engine parameter, uniform within its engine-card range
  calibration        emulator_inversion: a per-region common-mode shift of the logit target
                     (sd `calibration.target_uncertainty.region_sd_logit`), then re-inversion;
                     likelihood: (calibrated parameters, unit attributes) from N(estimate, covariance);
                     posterior: rows of the canonical joint posterior sample (v2d §3.3: each draw records the
                     sample id, its row and the values applied, re-checked against the sample, fail closed)
  costs              a uniform multiplier per listed cost item
  emulator           one joint draw of the reduction errors of every unit: a common (population-mean) component
                     plus a per-unit one, from the held-out residuals (toolkit/analysis/model.py EmulatorError)
  priors             (v2 priors, drawn whenever there is one) every prior parameter from its declared bounded
                     distribution (toolkit/spec/priors.py), on its own random stream, before calibration
The point plan is evaluated in every draw (the headline interval) and the plan is
re-optimised in every draw (package stability). The interval's label comes from the spec
(`Spec.uncertainty_scope`): what it covers and what it does not. Writes
`uncertainty_report.yaml` in the schema of scripts/propagate_uncertainty.py (shared
summariser), plus the scope and the draw plan: every source drawn or not, every engine
parameter drawn or held fixed with why, and the emulator-error model with its held-out
evidence, so a report cannot claim more precision than the draws carry.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis.core import q  # noqa: E402
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
        from toolkit.spec.priors import PRIOR_STREAM
        self.prng = np.random.default_rng([int(seed), PRIOR_STREAM])     # priors: their own stream
        if "emulator" in self.src and getattr(A.model, "error", None) is None:
            raise ValueError("uncertainty: the emulator source is declared but the emulator has no error model (fewer "
                             "than 3 held-out points); raise analysis.emulator.holdout_share or exclude it with a reason")
        cal = A.spec["calibration"]
        self.sd = float((cal.get("target_uncertainty") or {}).get("region_sd_logit", 0.0))
        self.regions = sorted({u[A.region] for u in A.spec.units()}) if A.region else []
        if A.method == "likelihood" and "calibration" in self.src:
            if A.lik.get("cov") is None:
                raise ValueError("uncertainty: the calibration covariance is singular; the calibration source cannot "
                                 "be drawn (see analysis/calibration.yaml identifiability)")
            self.mvn = (np.array([A.lik["x"][n] for n in A.lik["names"]]), np.array(A.lik["cov"]))
        self.seed = int(seed)
        self.post_draws, self.records = None, []
        self.jc_draws, self.jc_records = None, []
        # joint calibration: its shared parameters are no longer free priors; with the calibration source they are
        # drawn from the region sample, without it they stay at the region's best point
        self.jc_names = set((A.joint or {}).get("shared") or [])

    def prepare(self, nd):
        from toolkit.analysis import calibration as C
        if self.A.method == "posterior" and "calibration" in self.src:
            self.post_draws = C.draws(C.sample_of(self.A.lik), nd, self.seed)
        if self.A.joint is not None and "calibration" in self.src:
            self.jc_draws = C.draws(self.A.joint["sample"], nd, self.seed)

    def draw(self):
        A = self.A
        theta, sampled = dict(A.theta0), {}
        shifts, zs, noise, mult = None, None, None, None
        for k, p in A.pr.items():
            v = p.sample(self.prng)           # drawn even when replaced, so the other priors' stream is unchanged
            if k in self.jc_names:
                continue
            theta[k] = v
            sampled[k] = theta[k]
        if self.jc_draws is not None:
            r = self.jc_draws[len(self.jc_records)]
            for k in A.joint["shared"]:
                theta[k] = float(r["values"][k])
                sampled[k] = theta[k]
            zs = {i: float(r["values"][f"z:{i}"]) for i in A.ids}
            self.jc_records.append({"sample_id": r["sample_id"], "row": r["row"],
                                    "values": dict({k: theta[k] for k in A.joint["shared"]},
                                                   **{f"z:{i}": zs[i] for i in A.ids})})
        for kind in ORDER:
            if kind not in self.src:
                continue
            if kind == "engine_parameters":
                for k, v in A.fr.items():
                    theta[k] = float(self.rng.uniform(v[0], v[1]))
                    sampled[k] = theta[k]
            elif kind == "calibration" and A.method == "emulator_inversion" and A.joint is None:
                shifts = {r: float(self.rng.normal(0, self.sd)) for r in self.regions}
                sampled.update({f"shift:{r}": v for r, v in shifts.items()})
            elif kind == "calibration" and A.method == "likelihood":
                mu, cov = self.mvn
                x = dict(zip(A.lik["names"], self.rng.multivariate_normal(mu, cov)))
                for p in A.lik["params"]:
                    theta[p] = float(x[p])
                    sampled[p] = theta[p]
                zs = {i: float(x.get(f"z:{i}", A.lik["fixed_z"].get(i))) for i in A.ids}
            elif kind == "calibration" and A.method == "posterior":
                r = self.post_draws[len(self.records)]
                v = r["values"]
                for p in A.lik["params"]:
                    theta[p] = float(v[p]) if p in v else float(A.lik["fixed_params"][p])
                    sampled[p] = theta[p]
                zs = {i: float(v[f"z:{i}"]) if f"z:{i}" in v else float(A.lik["fixed_z"][i]) for i in A.ids}
                applied = {**{p: theta[p] for p in A.lik["params"]}, **{f"z:{i}": zs[i] for i in A.ids}}
                self.records.append({"sample_id": r["sample_id"], "row": r["row"],
                                     "values": {n: applied[n] for n in A.lik["sample"]["names"]}})
            elif kind == "costs":
                mult = {it: float(self.rng.uniform(lo, hi)) for it, (lo, hi) in self.src["costs"]["items"].items()}
                sampled.update({f"cost:{k}": v for k, v in mult.items()})
            elif kind == "emulator":
                noise = self.rng
        if zs is None:
            cz = A.calibrate(theta, shifts)
            zs = {i: v[0] for i, v in cz.items()}
            self.last_clamped = sorted(i for i, v in cz.items() if v[1])   # Codex r2 P1-4: kept per draw
        return theta, zs, noise, mult, sampled


def run(A, plan, headline_name: str, draws=None, seed=None) -> dict:
    sp = A.spec["uncertainty"]
    nd = int(draws if draws is not None else sp.get("draws", 32))
    scope = A.spec.uncertainty_scope()
    D = Drawer(A, seed if seed is not None else sp.get("seed", 42))
    D.prepare(nd)
    fixed = {o: [] for o in A.outcomes}
    opt_vals, same = [], np.zeros(len(A.ids))
    comps_used, outs, samples = set(), [], {}
    corr_any = corr_plan = 0
    clamped_draws = []
    hl_outcome = A.spec["deliverable"]["headline"]["outcome"]
    for d in range(nd):
        D.last_clamped = []
        theta, zs, noise, mult, sampled = D.draw()
        if D.last_clamped:
            clamped_draws.append({"draw": d, "units": len(D.last_clamped), "examples": D.last_clamped[:5]})
        out, ben, cost = A.evaluate(theta, zs, noise=noise, cost_mult=mult)
        corr_any += bool(A.last_events)
        row = {}
        for o in A.outcomes:
            v = sum(out[i]["y"][A.comp][o] - out[i]["y"][plan[i]][o] for i in A.ids)
            fixed[o].append(v)
            row[f"{o}_averted"] = v
        row[headline_name] = row[f"{hl_outcome}_averted"]
        sol = A.solve(ben, cost)
        A.guard(f"uncertainty draw {d}", [plan, sol["choice"]])
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
    if clamped_draws:
        # Codex r2 P1-4: where a draw's calibration clamps, that draw does not carry the full calibration uncertainty;
        # the interval says so instead of claiming unrestricted calibration coverage.
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import render_claims
        meta = dict(scope["interval"])
        meta["covers"] = ["calibration_partly_clamped" if k == "calibration" else k for k in meta["covers"]]
        scope = dict(scope, interval=meta, covers=meta["covers"], label=render_claims.interval_label(meta))
    prov = None
    if A.method == "posterior" and D.post_draws is not None:
        # v2d §3.3: re-read the canonical sample; every draw's applied values must equal its row (fail closed)
        from toolkit.analysis import calibration as C
        smp = C.sample_of(A.lik)
        found = C.check_provenance(smp, D.records)
        prov = {"sample_id": smp["sample_id"], "draws": len(D.records), "rows": [r["row"] for r in D.records],
                "applied": D.records, "findings": found, "alarms": C.spread_alarm(smp, D.records)}
        if found:
            raise ValueError(f"uncertainty: {len(found)} draws are not rows of the canonical posterior sample "
                             f"(e.g. {found[0]})")
        pd = list(A.lik.get("prior_dominated") or [])
        if pd:
            sys.path.insert(0, os.path.join(ROOT, "scripts"))
            import render_claims
            meta = dict(scope["interval"])
            meta["prior_dominated"] = sorted(set(meta.get("prior_dominated") or []) | set(pd))
            scope = dict(scope, interval=meta, label=render_claims.interval_label(meta))
    if D.jc_draws is not None:
        # the joint calibration's draws: every applied value must be a row of its region sample (fail closed)
        from toolkit.analysis import calibration as C
        smp = A.joint["sample"]
        found = C.check_provenance(smp, D.jc_records)
        if found:
            raise ValueError(f"uncertainty: {len(found)} draws are not rows of the joint calibration's region sample "
                             f"(e.g. {found[0]})")
        prov = dict(prov or {}, joint_calibration={"sample_id": smp["sample_id"], "draws": len(D.jc_records),
                                                   "rows": [r["row"] for r in D.jc_records],
                                                   "alarms": C.spread_alarm(smp, D.jc_records)})
    from toolkit.spec import priors as PRI
    plan_ = PRI.plan(A.spec)
    plan_["emulator_error"] = (getattr(A.model, "accuracy", {}) or {}).get("error_model") if "emulator" in D.src else None
    data = {"draws": nd, "seed": int(sp.get("seed", 42)) if seed is None else int(seed),
            "covers": scope["covers"], "excludes": scope["excludes"], "label": scope["label"],
            "fixed_plan": {o: q(v, lvl) for o, v in fixed.items()},
            "reoptimised_objective": q(opt_vals, lvl),
            "units_same_package_share_median": float(np.median(same / max(nd, 1))) if nd else None,
            "units_same_package_in_90pct_of_draws": int(np.sum(same / max(nd, 1) >= 0.9)) if nd else None,
            "components_in_any_draw_plan": sorted(comps_used), "interval": scope["interval"],
            "calibration_clamped": {"draws": len(clamped_draws), "max_units": max((c["units"] for c in clamped_draws),
                                                                                  default=0),
                                    "per_draw": clamped_draws},
            "model_corrections": {"draws_with_corrections": corr_any, "draws_with_corrections_on_plan": 0},
            "headline": {"name": headline_name, "outcome": hl_outcome}, "draw_plan": plan_,
            "prior_dominated": sorted(set(plan_["prior_dominated"]) | set((A.lik or {}).get("prior_dominated") or []))}
    if prov is not None:
        data["draw_provenance"] = prov
    agg = _pu().summarise_outputs(outs, samples, nd, level=scope["level"])   # the signed level (Codex P1-11)
    report = {"producer": PRODUCER, "produced_by": "harness", "n_draws": nd, "n_draws_requested": nd,
              "n_successful": nd - len(agg["errors"]), "n_failed": len(agg["errors"]), "seed": data["seed"],
              "interval_label": scope["label"], "interval": scope["interval"], "covers": scope["covers"],
              "excludes": scope["excludes"],
              "degenerate_outputs": agg["degenerate_outputs"],
              "sampler_varied_parameters": len(agg["varied_params"]),
              "scalar_outputs": agg["scalar_outputs"], "categorical_outputs": agg["categorical_outputs"],
              "parameter_samples": agg["parameter_samples"], "backend": "local",
              "sources_drawn": [x["kind"] for x in plan_["sources"] if x["status"] == "drawn"],
              "sources_not_drawn": [{k: x[k] for k in ("kind", "status", "reason") if k in x} for x in plan_["sources"]
                                    if x["status"] != "drawn"],
              "parameters_drawn": [{k: x.get(k) for k in ("name", "source_kind", "dist", "range", "source")}
                                   for x in plan_["drawn"]],
              "parameters_fixed": [{k: x.get(k) for k in ("name", "kind", "value", "reason")} for x in plan_["fixed"]
                                   if x["status"] == "fixed"],
              "not_exposed": plan_["not_exposed"], "emulator_error": plan_["emulator_error"]}
    return {"data": data, "report": report}


def scalar_report(outputs, values: dict, level: float, label: str, n_draws: int, method: str, **extra) -> dict:
    """uncertainty_report.yaml for a deliverable whose headline outputs are scalars (design note v2d §2.3), in the
    shape the allocation path writes and every reader of that file expects: `scalar_outputs: {name: {mean, median,
    sd, ci_low, ci_high, n}}`, one entry per declared output, in the declared order. `outputs` are the deliverable's
    declared output entries ({name, ...}); `values` maps each name to its draws. A declared output without finite draws
    fails closed (ValueError): the report never omits a headline."""
    a = (1.0 - float(level)) / 2.0
    so = {}
    for o in outputs:
        n = o["name"] if isinstance(o, dict) else str(o)
        v = np.asarray(values.get(n, []), float)
        if v.size == 0 or not np.all(np.isfinite(v)):
            raise ValueError(f"uncertainty report: output {n} has no draws or non-finite draws")
        so[n] = {"mean": float(np.mean(v)), "median": float(np.median(v)), "sd": float(np.std(v, ddof=1))
                 if v.size > 1 else 0.0, "ci_low": float(np.quantile(v, a)), "ci_high": float(np.quantile(v, 1 - a)),
                 "n": int(v.size)}
        for k in ("metric", "window", "scale"):
            if isinstance(o, dict) and o.get(k) is not None:
                so[n][k] = o[k]
    return dict({"producer": PRODUCER, "produced_by": "harness", "n_draws": int(n_draws),
                 "n_successful": int(n_draws), "n_failed": 0, "interval_label": label, "level": float(level),
                 "method": method, "backend": "local", "scalar_outputs": so}, **extra)


def write_report(run_dir, report) -> str:
    p = os.path.join(run_dir, "uncertainty_report.yaml")
    from toolkit.analysis.artifacts import plain
    from toolkit.analysis.artifacts import _safe_io
    _safe_io().write_nofollow(run_dir, "uncertainty_report.yaml", yaml.safe_dump(plain(report), sort_keys=False).encode(),
                              atomic=True)
    return p


def _self_test() -> int:
    """The shared scalar writer (scalar_report); the allocation path's run() is exercised by run.py --self-test."""
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    rng = np.random.default_rng(1)
    outs = [{"name": "b", "metric": "m2", "window": "w", "scale": "ratio"}, {"name": "a", "metric": "m1"}]
    vals = {"a": rng.normal(1.0, 0.1, 4000), "b": rng.normal(5.0, 1.0, 4000)}
    try:
        rep = scalar_report(outs, vals, 0.9, "90% UI over calibration", 4000, "posterior")
    except Exception as e:  # noqa: BLE001 -- a missing writer is a failing case
        rep = {"error": f"{type(e).__name__}: {e}"}
    so = rep.get("scalar_outputs") or {}
    ok(list(so) == ["b", "a"] and all(set(v) >= {"mean", "median", "sd", "ci_low", "ci_high", "n"} for v in so.values()),
       f"U1 scalar_report: one entry per declared output, in the declared order, with mean, median, sd and interval "
       f"({rep.get('error') or list(so)})")
    if so:
        lo, hi = np.quantile(vals["a"], [0.05, 0.95])
        ok(abs(so["a"]["ci_low"] - lo) < 1e-12 and abs(so["a"]["ci_high"] - hi) < 1e-12 and so["b"]["metric"] == "m2",
           "U2 the interval is the declared level's central interval; the output's metric, window and scale are kept")
        ok(rep["interval_label"] == "90% UI over calibration" and rep["n_draws"] == 4000,
           "U3 the report carries the label and the number of draws")
        for label, v in (("no draws", {"a": [], "b": vals["b"]}),
                         ("a non-finite draw", {"a": [1.0, float("nan")], "b": vals["b"]})):
            try:
                scalar_report(outs, v, 0.9, "x", 2, "posterior")
                ok(False, f"U4 an output with {label} fails closed")
            except ValueError:
                ok(True, f"U4 an output with {label} fails closed")
        with tempfile.TemporaryDirectory() as d:
            p = write_report(os.path.realpath(d), rep)
            ok((yaml.safe_load(open(p)) or {}).get("scalar_outputs", {}).get("a", {}).get("n") == 4000,
               "U5 write_report publishes uncertainty_report.yaml, which reads back")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
