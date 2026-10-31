"""The analysis state every step shares: spec, outcome model, calibration, outcomes, benefits, costs and
the toolkit/alloc solve (design §2). Steps (uncertainty, stability, sensitivity, decision tests) call
`evaluate()` and `solve()` here, so a step cannot compute a benefit or a cost another way."""
from __future__ import annotations

import math

import numpy as np

from toolkit.alloc import costs as acosts, optimize as aopt, packages as apackages
from toolkit.analysis import calibration as C
from toolkit.analysis.batch import fitted_ranges

PRODUCER = "toolkit/analysis/core.py"


def discount_factor(spec) -> float:
    """Benefit weight for the decision window: none -> 1; {rate, base_year} -> the mean of (1+r)^-(t-base) over
    the window, assuming outcomes accrue uniformly within it (the engine returns window totals)."""
    d = spec["objective"].get("discounting", "none")
    if d == "none" or not d or float(d.get("rate", 0)) == 0:
        return 1.0
    r, b = float(d["rate"]), float(d["base_year"])
    t0, t1 = (float(x) for x in spec["objective"]["horizon"])
    k = math.log(1 + r)
    return float((math.exp(-k * (t0 - b)) - math.exp(-k * (t1 - b))) / (k * (t1 - t0)))


class Analysis:
    def __init__(self, spec, model, log=print):
        self.spec, self.model, self.log = spec, model, log
        self.eng = spec.engine()
        self.ids = spec.unit_ids()
        self.by_id = {str(u[spec.uid]): u for u in spec.units()}
        self.pk = spec.packages()
        self.comp = spec.comparator
        self.outcomes = spec.outcomes()
        self.primary = self.outcomes[0]
        self.alloc = spec.alloc_spec()
        self.feas = apackages.feasibility(self.alloc, spec.units())
        self.budget = spec.budget()
        self.region = spec["decision_units"].get("region")
        self.fr = fitted_ranges(self.eng)
        self.theta0 = {k: v[2] for k, v in self.fr.items()}
        self.dfac = discount_factor(spec)
        self.method = spec["calibration"].get("method", "none")
        self.lik = None
        self.last_events = []

    # ------------------------------------------------------------ calibration
    def calibrate(self, theta, shifts=None, ids=None):
        """{unit: (z, clamped)} for emulator inversion; the fitted z for likelihood; None for none."""
        if self.method == "emulator_inversion":
            return C.invert(self.spec, self.model, theta, shifts, ids)
        if self.method == "likelihood":
            return {i: (self.lik["z"].get(i, self.lik["fixed_z"].get(i)), False) for i in (ids or self.ids)}
        return {i: (None, False) for i in (ids or self.ids)}

    def fit_calibration(self):
        if self.method == "likelihood":
            self.lik = C.fit_likelihood(self.spec, self.model)
            self.theta0 = dict(self.theta0, **self.lik["theta"])
            missing = [i for i in self.ids if i not in self.lik["z"]]
            if missing:
                raise ValueError(f"calibration: units with no targets cannot be calibrated: {missing[:5]}")
        return self.lik

    # ------------------------------------------------------------ outcomes, benefits, costs
    def evaluate(self, theta, zs, ids=None, noise=None, cost_mult=None):
        """(outcomes, benefit, cost) for the units. benefit[u][p] = (comparator - package) x discount factor
        on the primary outcome; cost from toolkit/alloc (per-outcome items from the model's cost outputs),
        with optional per-cost-item multipliers."""
        ids = ids or self.ids
        out = self.model.predict(zs, theta, ids, noise=noise)
        self.last_events = list(getattr(self.model, "last_events", []) or [])
        ben = {i: {k: self.dfac * (out[i]["y"][self.comp][self.primary] - out[i]["y"][k][self.primary])
                   for k in self.pk} for i in ids}
        units = [self.by_id[i] for i in ids]
        fn = lambda u, p: out[str(u[self.spec.uid])]["cost"][p]  # noqa: E731
        cm = acosts.cost_matrix(self.alloc, units, outcome=fn)
        if cost_mult:
            cost = {i: {k: sum(v * cost_mult.get(it, 1.0) for it, v in cm[i][k]["items"].items()) for k in self.pk}
                    for i in ids}
        else:
            cost = {i: {k: cm[i][k]["total"] for k in self.pk} for i in ids}
        return out, ben, cost

    def plan_corrections(self, events, plan) -> list[dict]:
        """The model corrections (extrapolation, clips, floors) that bear on the plan: on a unit's chosen package,
        on its comparator (every benefit is measured from it), or on the whole unit (Codex P1-6)."""
        return [e for e in events if e["package"] in ("*", self.comp) or plan.get(e["unit"]) == e["package"]]

    def solve(self, ben, cost, budget=None, feas=None):
        r = aopt.solve(ben, cost, feas or self.feas, self.budget if budget is None else budget, method="ilp",
                       time_limit=120)
        if r["status"] != "optimal":
            raise RuntimeError(f"optimizer status {r['status']}")
        return r

    def summary(self, plan, out, cost) -> dict:
        averted = {o: sum(out[i]["y"][self.comp][o] - out[i]["y"][plan[i]][o] for i in self.ids) for o in self.outcomes}
        base = {o: sum(out[i]["y"][self.comp][o] for i in self.ids) for o in self.outcomes}
        mix = {c: 0 for c in self.spec["options"]["components"]}
        for i in self.ids:
            for c in self.pk[plan[i]]:
                mix[c] += 1
        mix["no_package"] = sum(plan[i] == self.comp for i in self.ids)
        packages = {}
        for i in self.ids:
            packages[plan[i]] = packages.get(plan[i], 0) + 1
        spent = sum(cost[i][plan[i]] for i in self.ids)
        return {"averted": averted, "averted_discounted_objective": self.dfac * averted[self.primary],
                "comparator_total": base, "mix": mix, "packages": packages, "cost_allocated": spent,
                "budget_allocatable": self.budget, "discount_factor": self.dfac,
                "cost_per_outcome_averted": spent / averted[self.primary] if averted[self.primary] > 0 else None}

    def mix_text(self, plan) -> str:
        """The recommendation in words: package counts (used in decision-test results)."""
        cnt = {}
        for i in self.ids:
            cnt[plan[i]] = cnt.get(plan[i], 0) + 1
        return "; ".join(f"{k}: {v} units" for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])))


def q(v, level=0.95) -> dict | None:
    v = np.asarray(v, float)
    if not len(v):
        return None
    a = 100 * (1 - level) / 2
    return {"median": float(np.median(v)), "ci_low": float(np.percentile(v, a)),
            "ci_high": float(np.percentile(v, 100 - a)), "mean": float(v.mean()), "level": level}
