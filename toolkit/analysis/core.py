"""The analysis state every step shares: spec, outcome model, calibration, outcomes, benefits, costs and
the toolkit/alloc solve (design §2). Steps (uncertainty, stability, sensitivity, decision tests) call
`evaluate()` and `solve()` here, so a step cannot compute a benefit or a cost another way."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os

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


def clamped_units_max() -> int:
    """toolkit/calibration/thresholds.yaml blocking.clamped_units_max. (Kept out of toolkit/analysis/calibration.py,
    whose bytes the compartmental engine's coverage proof binds: compat_proof.py CALIBRATOR_FILES.)"""
    from toolkit.calibration.history_matching import thresholds as _t
    return int(_t()["blocking"]["clamped_units_max"])


def clamped_hits(cal: dict) -> list:
    """AnalysisBlocked hits for the units an emulator inversion clamped at a bound of calibration.range."""
    return [{"unit": i, "package": "*", "kind": "calibration_clamped", "z": round(float(v["z"]), 4)}
            for i, v in (cal.get("units") or {}).items() if v.get("clamped")]


class CostTable(dict):
    """{unit: {package: total $}}, with `by_component` {unit: {package: {component: $}}} (F6 S1-5: the dollars a
    re-solved plan moves between components)."""

    def __init__(self, totals, by_component):
        super().__init__(totals)
        self.by_component = by_component


def merge_costs(base: CostTable, part: CostTable) -> CostTable:
    """`base` with the units of `part` replaced (a stability fold that re-evaluates its held-out units)."""
    return CostTable({**base, **part}, {**base.by_component, **part.by_component})


class AnalysisBlocked(RuntimeError):
    """The run stopped before publishing (analysis/blocked.yaml says why): model corrections bear on a plan, or (at
    the calibration step, `reason` given) units the calibration cannot match."""

    def __init__(self, where: str, hits: list, reason: str | None = None):
        self.where, self.hits, self.reason = where, list(hits), reason
        super().__init__(f"{where}: {reason} (e.g. {hits[:2]})" if reason else
                         f"{where}: {len(hits)} model corrections (extrapolation, clips, floors) bear on the plan "
                         f"(e.g. {hits[:2]})")


class SolveFailed(RuntimeError):
    """The solver gave no plan this solve may use. `status`: infeasible (proven), feasible (stopped early; `gap` is
    its proven gap, or None), no_feasible_found. The message is the one earlier handlers match."""

    def __init__(self, status: str, gap=None):
        self.status, self.gap = status, gap
        super().__init__(f"optimizer status {status}")


def solver_settings() -> dict:
    """toolkit/analysis/thresholds.yaml `optimizer` (decision D3)."""
    import yaml
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return yaml.safe_load(f)["optimizer"]


@contextlib.contextmanager
def auxiliary(A, kind: str):
    """Solves inside are auxiliary ones of `kind` (uncertainty draws, folds, tests, variants, the frontier): see
    Analysis.solve. Outside, a solve is a headline solve."""
    prev = getattr(A, "aux", None)
    A.aux = kind
    try:
        yield A
    finally:
        A.aux = prev


def best_known(A, sol, plan, ben, cost, budget=None, feas=None) -> dict:
    """For a solve that accepted a plan it could not prove optimal (status "feasible", gap g): the better of that
    incumbent and the point plan `plan`, when the point plan meets this problem's constraints (the allocator's own
    check), so a comparison with the point plan never shows the point plan beating the "optimum". Adds
    `solver_gap` = {gap, bound, kept}: the true optimum is at most `bound` above the returned objective (g / (1 - g)
    of the incumbent's gain, less what the point plan recovers, both computed on gains). A proven solve is returned
    as it is."""
    if sol.get("status") != "feasible" or sol.get("gap") is None:
        return sol
    if budget is None:
        budget = A.budget if getattr(A, "plan_budget", None) is None else A.plan_budget
    from toolkit.analysis import moves as MV
    g, out, kept, f = float(sol["gap"]), dict(sol), "incumbent", feas or A.feas
    room = g / (1.0 - g) * abs(float(sol.get("gain") or 0.0))      # the optimum's gain above the incumbent's, at most
    try:
        # what the point plan adds, from per-unit differences: sums of two large objectives would round it away
        more = float(sum(ben[i][plan[i]] - ben[i][sol["choice"][i]] for i in A.ids))
        spent = float(sum(cost[i][plan[i]] for i in A.ids))
        exact = bool(getattr(A, "budget_exact", False))
        # feasible by the allocator's check, by the rule compare() rows report, and within the solver's own budget
        # tolerance (the check allows more rounding than the solver does: Codex narrow round P1-1)
        fits = (not aopt.check(plan, ben, cost, f, budget, budget_exact=exact)
                and not MV.infeasible_reasons(A.ids, plan, cost, f, budget, budget_exact=exact)
                and spent <= float(budget) * (1 + aopt.BUDGET_RTOL)
                and (not exact or spent >= float(budget) * (1 - aopt.BUDGET_RTOL)))
    except (KeyError, TypeError, ValueError):
        more, fits = 0.0, False
    if fits and more > 0:
        out.update(choice=dict(plan), objective=float(sum(ben[i][plan[i]] for i in A.ids)))
        kept, room = "point_plan", room - more
    out["solver_gap"] = {"gap": g, "bound": max(0.0, room), "kept": kept}
    return out


def _problem_key(ben, cost, feas, budget, budget_exact: bool = False):
    """A hash of one solve's inputs, or None (then nothing is kept) when they cannot be serialised or a unit or
    package id is not text (the encoding writes ids as text, so 1 and "1" would share a key: Codex r1 P1-6) or a table
    value or the budget is not a number."""
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)  # noqa: E731 -- else the allocator's
    #                                               own input check would be skipped on a hit (Codex r2 P1)
    if not (num(budget) and all(isinstance(u, str) and all(isinstance(k, str) and num(x) for k, x in r.items())
                                for t in (ben, cost) for u, r in t.items())):
        return None
    try:
        tabs = [sorted((str(u), sorted((str(k), float(x)) for k, x in r.items())) for u, r in t.items())
                for t in (ben, cost)]
        rest = json.dumps([float(budget), bool(budget_exact), feas], sort_keys=True,
                          default=lambda o: sorted(map(str, o)) if isinstance(o, (set, frozenset)) else str(o))
        return hashlib.sha256((json.dumps(tabs) + rest).encode()).hexdigest()
    except (TypeError, ValueError):
        return None


class Analysis:
    aux = None

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
        self.budget_exact = bool((spec.raw.get("constraints") or {}).get("envelope_exact", False))
        self.region = spec["decision_units"].get("region")
        self.fr = fitted_ranges(self.eng)
        from toolkit.spec import priors as PRI
        self.pr = PRI.drawn(spec)             # prior parameters, drawn in every uncertainty draw
        # owner, 14 Nov (f6-posterior-ranges): a declared net-effect posterior's hull is its columns' range in every step
        # that reads one (draws, sensitivity ends, automatic range tests), as in the design box (batch.design_box)
        from toolkit.engines import hooks as _hooks
        self.ne = _hooks.external_posterior_sample(spec, self.eng, fitted=self.fr, priors=self.pr)
        self.fr, self.pr = _hooks.external_posterior_with_ranges(self.ne, self.fr, self.pr)
        self.dr = dict(self.fr, **{k: (p.lo, p.hi, p.value) for k, p in self.pr.items()})   # every drawn parameter
        from toolkit.spec import engine_parameters as PV
        # the spec's engine parameter values under every parameter set built from here (point, draws, decision tests,
        # stability, sensitivity, schedule and trajectory tasks); without the field this is the drawn point values
        self.theta0 = PV.params(spec, {k: v[2] for k, v in self.dr.items()})
        # v2-posterior-point: a declared posterior's columns sit at the fit's verified chosen point (as set_joint moves the
        # shared parameters to the joint calibration's best point); a differing spec value is refused
        self.theta0 = _hooks.external_posterior_point(spec, self.ne, self.theta0)
        self.dfac = discount_factor(spec)
        self.method = spec["calibration"].get("method", "none")
        self.lik = None
        self.joint = None
        self.flagged = {}
        self.last_events = []
        self.last_cost_floors = []
        # F6 S1-1b: with an enforced affordability rule, plans are solved within a reserve (fraction x budget); every
        # re-optimisation (draws, stability, sensitivity) then compares like with like. `budget` stays the allocatable
        # envelope that affordability is judged against.
        self.plan_budget = None
        self.reserve_fraction = None
        # F6 S2-5d: the burden anchor (toolkit/analysis/burden.py anchor_step): every evaluation's anchored outcomes at
        # the external level; None (the default) leaves the outcomes as the model predicts them
        self.anchor = None
        # F6 run 4, owner decision R3 (toolkit/analysis/outcome_adjust.py): trial ratios applied in every evaluation
        from toolkit.analysis import outcome_adjust as OA
        self.adjust = OA.build(spec.raw, self.pk)

    def set_reserve(self, fraction):
        """F6 S1-1b: plan within `fraction` of every allocatable budget (None: no reserve)."""
        self.reserve_fraction = None if fraction is None else float(fraction)
        self.plan_budget = None if fraction is None else float(fraction) * float(self.budget)

    def planned(self, budget: float) -> float:
        """The budget a plan is solved at for an allocatable `budget` (an envelope test, the frontier): the same reserve
        fraction as the headline plan, so the declared envelope reproduces it."""
        return float(budget) if self.reserve_fraction is None else self.reserve_fraction * float(budget)

    def set_joint(self, rep):
        """The joint calibration (toolkit/analysis/joint_calibration.py): its shared parameters move to the best point
        of the compatible region and each unit's attribute to its jointly fitted value there (verified on the engine);
        the uncertainty draws read whole rows of the region sample (shared and unit values together) instead of the
        priors. Steps that move another parameter (sensitivity, decision tests) keep these unit values, as the
        posterior path keeps its fitted ones: re-inverting one target would drop the others (Codex r1 P1-2)."""
        from toolkit.analysis import joint_calibration as JC
        names = list(rep["shared"])
        self.joint = {"shared": names, "sample": JC.region_sample(rep), "ranges": rep["region"]["ranges"],
                      "best": dict(rep["best"]["theta"]), "z": {str(k): float(v) for k, v in rep["best"]["z"].items()}}
        self.theta0 = dict(self.theta0, **{k: float(rep["best"]["theta"][k]) for k in names})

    # ------------------------------------------------------------ calibration
    def calibrate(self, theta, shifts=None, ids=None):
        """{unit: (z, clamped)} for emulator inversion; the fitted z for likelihood; None for none."""
        if self.joint is not None:
            return {i: (self.joint["z"][i], False) for i in (ids or self.ids)}
        if self.method == "emulator_inversion":
            return C.invert(self.spec, self.model, theta, shifts, ids)
        if self.method in ("likelihood", "posterior"):
            return {i: (self.lik["z"].get(i, self.lik["fixed_z"].get(i)), False) for i in (ids or self.ids)}
        return {i: (None, False) for i in (ids or self.ids)}

    def fit_calibration(self):
        if self.method in ("likelihood", "posterior"):
            from toolkit.analysis import mode_start as MS
            fit = C.fit_likelihood if self.method == "likelihood" else MS.fit_posterior_panel
            self.lik = fit(self.spec, self.model)
            self.theta0 = dict(self.theta0, **self.lik["theta"])
            # a unit in fixed_z is calibrated at a fixed value (None: a fit with no per-unit attribute, where every
            # unit runs at its bound attributes)
            missing = [i for i in self.ids if i not in self.lik["z"] and i not in (self.lik.get("fixed_z") or {})]
            if missing:
                raise ValueError(f"calibration: units with no targets cannot be calibrated: {missing[:5]}")
        return self.lik

    # ------------------------------------------------------------ outcomes, benefits, costs
    def evaluate(self, theta, zs, ids=None, noise=None, cost_mult=None, sampled=None, anchor_factors=None,
                 anchor_unit_factors=None, anchor_skip=None, unit_attrs=None):
        """(outcomes, benefit, cost) for the units. benefit[u][p] = (comparator - package) x discount factor
        on the primary outcome; cost from toolkit/alloc (per-outcome items from the model's cost outputs),
        with optional per-cost-item multipliers. `unit_attrs`: {unit: {attribute: value}} drawn for this evaluation
        only (the unit_rates source; direct evaluation), set on the model and cleared after the prediction."""
        ids = ids or self.ids
        if self.anchor is not None and anchor_factors is None and anchor_unit_factors is None \
                and len(ids) < len(self.ids):
            # F6 S2-5d review r2: a partial evaluation (a stability fold) covers whole anchor groups, so the merged
            # result keeps each group's anchored total; the returned tables hold every unit evaluated
            need, grow = set(ids), True
            while grow:
                grow = False
                for r in self.anchor["rows"].values():
                    if need & set(r["members"]) and not set(r["members"]) <= need:
                        need |= set(r["members"])
                        grow = True
            ids = [i for i in self.ids if i in need]
        if unit_attrs is not None:
            if not hasattr(self.model, "unit_draws"):
                raise ValueError("drawn unit values need the direct model (analysis.evaluation: direct)")
            self.model.unit_draws = unit_attrs
        try:
            out = self.model.predict(zs, theta, ids, noise=noise)
        finally:
            if unit_attrs is not None:
                self.model.unit_draws = None
        self.last_events = list(getattr(self.model, "last_events", []) or [])
        if self.anchor is not None:      # F6 S2-5d: external level / this evaluation's own model value
            from toolkit.analysis import burden as BU
            BU.apply_anchor(self.anchor, out, ids, param=lambda x: BU.param_at(self, theta, x), sampled=sampled,
                            factors=anchor_factors, unit_factors=anchor_unit_factors, skip=anchor_skip)
        if getattr(self, "adjust", None):     # F6 run 4 R3: base outcome adjustments (trial ratios)
            from toolkit.analysis import outcome_adjust as OA
            self.last_events += OA.apply(self.adjust, out, ids, self.outcomes, self.last_events)
        ben = {i: {k: self.dfac * (out[i]["y"][self.comp][self.primary] - out[i]["y"][k][self.primary])
                   for k in self.pk} for i in ids}
        units = [self.by_id[i] for i in ids]
        fn = lambda u, p: out[str(u[self.spec.uid])]["cost"][p]  # noqa: E731
        floors = []
        cm = acosts.cost_matrix(self.alloc, units, outcome=fn, floors=floors)
        self.last_cost_floors = floors       # F6 S1-3: incremental per-outcome costs floored at zero here
        comp_of = {it["id"]: it["component"] for it in self.alloc["cost_items"]}
        by = {i: {k: {} for k in self.pk} for i in ids}
        for i in ids:
            for k in self.pk:
                for it, v in cm[i][k]["items"].items():
                    by[i][k][comp_of[it]] = by[i][k].get(comp_of[it], 0.0) + v * (cost_mult or {}).get(it, 1.0)
        if cost_mult:
            cost = {i: {k: sum(v * cost_mult.get(it, 1.0) for it, v in cm[i][k]["items"].items()) for k in self.pk}
                    for i in ids}
        else:
            cost = {i: {k: cm[i][k]["total"] for k in self.pk} for i in ids}
        return out, ben, CostTable(cost, by)

    def plan_corrections(self, events, plan) -> list[dict]:
        """The model corrections (extrapolation, clips, floors) that bear on the plan: on a unit's chosen package,
        on its comparator (every benefit is measured from it), or on the whole unit (Codex P1-6)."""
        return [e for e in events if e["package"] in ("*", self.comp) or plan.get(e["unit"]) == e["package"]]

    def guard(self, where: str, plans, events=None):
        """Codex r2 P1-3: in every evaluation (point, draws, stability folds, sensitivity, decision tests), model
        corrections that bear on the point plan or on the plan re-optimised there stop publication."""
        ev = self.last_events if events is None else events
        hits = []
        for p in plans:
            hits += [h for h in self.plan_corrections(ev, p) if h not in hits]
        if hits:
            raise AnalysisBlocked(where, hits)

    def solve(self, ben, cost, budget=None, feas=None, budget_exact=None):
        """One plan from toolkit/alloc at the settings of thresholds.yaml `optimizer` (decision D3). A headline solve
        (the point plan, the reserve search, a rebuild) must be proven optimal within `gap_rel`. Inside
        `auxiliary(A, kind)` a solve that stopped early is also used when its proven gap is at most `aux_gap_max`, and
        counted. Anything else raises SolveFailed with the solver's status. Every solve is counted in `solver_log`.
        A result is kept, so the same problem solved again in this run returns the same plan: a proven one for any
        solve, an accepted feasible one for auxiliary solves only (Codex r1 P1-3)."""
        st = solver_settings()
        if budget is None:
            budget = self.budget if self.plan_budget is None else self.plan_budget
        exact = bool(getattr(self, "budget_exact", False)) if budget_exact is None else bool(budget_exact)
        f = feas or self.feas
        kind = getattr(self, "aux", None)
        log = self.__dict__.setdefault("solver_log", {}).setdefault(
            kind or "headline", {"solves": 0, "feasible_accepted": 0, "max_gap": 0.0, "not_solved": 0})
        memo, key = self.__dict__.setdefault("solve_memo", {}), _problem_key(ben, cost, f, budget, budget_exact=exact)
        if key in memo and (kind or memo[key][1]["status"] == "optimal"):
            ch, meta = memo[key]
            return dict(meta, choice=dict(zip(self.ids, ch)))
        nt = min(int(st["threads"]), os.cpu_count() or 1) if len(ben) >= int(st["threads_min_units"]) else 1
        r = aopt.solve(ben, cost, f, budget, method="ilp", time_limit=float(st["time_limit_s"]),
                       gap_rel=float(st["gap_rel"]), threads=nt, **({"budget_exact": True} if exact else {}))
        r["time_limit_s"] = float(st["time_limit_s"])
        log["solves"] += 1
        gap = r.get("gap")
        # used only with its gain too: best_known() states the bound to the optimum from the gap and that gain
        used = (kind and r["status"] == "feasible" and gap is not None and r.get("gain") is not None
                and gap <= float(st["aux_gap_max"]))
        if r["status"] == "optimal" or used:
            if key is not None and set(r["choice"]) == set(self.ids):
                memo[key] = (tuple(r["choice"][i] for i in self.ids), {k: v for k, v in r.items() if k != "choice"})
            if used:
                log["feasible_accepted"] += 1
                log["max_gap"] = max(log["max_gap"], float(gap))
            return r
        if r["status"] != "infeasible":       # a proven infeasibility is a result, not a solve that failed
            log["not_solved"] += 1
        raise SolveFailed(r["status"], gap)

    def solver_report(self) -> dict:
        """The settings and the counts of this run's solves (the manifest's `solver`; contract.py reads the totals)."""
        lg = getattr(self, "solver_log", {})
        aux = {k: dict(v) for k, v in lg.items() if k != "headline"}
        tot = {"solves": sum(v["solves"] for v in aux.values()),
               "feasible_accepted": sum(v["feasible_accepted"] for v in aux.values()),
               "max_gap": max((v["max_gap"] for v in aux.values()), default=0.0),
               "not_solved": sum(v["not_solved"] for v in aux.values())}
        return {**solver_settings(), "headline": dict(lg.get("headline") or {"solves": 0}), "auxiliary": aux,
                "auxiliary_total": tot}

    def summary(self, plan, out, cost) -> dict:
        averted = {o: sum(out[i]["y"][self.comp][o] - out[i]["y"][plan[i]][o] for i in self.ids) for o in self.outcomes}
        base = {o: sum(out[i]["y"][self.comp][o] for i in self.ids) for o in self.outcomes}
        mix = {c: 0 for c in self.spec["options"]["components"]}
        for i in self.ids:
            for c in self.pk[plan[i]]:
                mix[c] += 1
        mix["no_package"] = sum(not self.pk[plan[i]] for i in self.ids)     # no component (F6 S1-6 r1 P2-5)
        packages = {}
        for i in self.ids:
            packages[plan[i]] = packages.get(plan[i], 0) + 1
        spent = sum(cost[i][plan[i]] for i in self.ids)
        return {"averted": averted, "averted_discounted_objective": self.dfac * averted[self.primary],
                "comparator_total": base, "mix": mix, "packages": packages, "cost_allocated": spent,
                "units_on_comparator": sum(plan[i] == self.comp for i in self.ids),
                "budget_allocatable": self.budget, "discount_factor": self.dfac,
                "cost_per_outcome_averted": spent / averted[self.primary] if averted[self.primary] > 0 else None}

    def mix_text(self, plan) -> str:
        """The recommendation in words: package counts (used in decision-test results)."""
        cnt = {}
        for i in self.ids:
            cnt[plan[i]] = cnt.get(plan[i], 0) + 1
        return "; ".join(f"{k}: {v} units" for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])))


def q(v, level=0.95) -> dict | None:
    import statistics
    v = np.asarray(v, float).ravel()      # draws are counted flat
    if not len(v):
        return None
    if not np.all(np.isfinite(v)):      # 1C-b 2d-iii: fail closed (one NaN made every quantile NaN)
        raise ValueError(f"quantiles: {int(np.sum(~np.isfinite(v)))} of {len(v)} values are not finite")
    a = 100 * (1 - level) / 2
    n = len(v)
    # hb1 fix 11 (L): the one estimator of both uncertainty files; the correctly rounded mean (exact rational
    # arithmetic), so neither cancellation, overflow nor the order of the draws changes it
    mean = float(statistics.mean(v.tolist()))
    s = None

    def lerp(p):          # numpy's linear rule without overflow: equal neighbours give that value exactly
        h = (n - 1) * p / 100.0
        k = int(math.floor(h))
        t = h - k
        if k + 1 >= n or s[k] == s[k + 1] or t == 0:
            return float(s[k])
        return float(s[k] * (1 - t) + s[k + 1] * t)
    out = []
    for p in (50.0, a, 100 - a):
        x = float(np.median(v)) if p == 50.0 else float(np.percentile(v, p))
        if not math.isfinite(x):       # numpy's interpolation overflowed near the largest float: this statistic only
            s = np.sort(v) if s is None else s
            x = lerp(p)
        out.append(x)
    med, lo, hi = out
    return {"median": med, "ci_low": lo, "ci_high": hi, "mean": float(mean), "level": level}
