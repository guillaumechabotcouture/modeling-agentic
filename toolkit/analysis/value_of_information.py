"""Value of information over the uncertainty draws (F6): which uncertain input is worth better data.

The decision is a plan (one package per unit) within the budget. In uncertainty draw d, plan p is worth
NB(p, d) = sum over units of the draw's benefit table at p's package: the objective the solver maximises (discounted
primary outcome averted). The uncertainty step already evaluates every draw and re-optimises the plan in it; this
step reads those tables through its `on_solve` hook (no new engine runs, no new solves).

Candidates: the headline (point) plan, every declared plan variant, the plan that maximises the expected objective
(NB is linear in the plan, so one solve on the draws' mean benefit and cost tables, within the headline plan's
budget; left out, with why, when a draw's model corrections bear on it) and each draw's re-optimised plan
(duplicates merged). Budget rule: a candidate whose cost in draw d (that draw's costs) exceeds the allocatable budget
(the solver's relative tolerance) is infeasible in d. It may be chosen now only if the share of draws in which it is
infeasible is at most the spec's `uncertainty.affordability.max_share_over_budget` (0 when none is declared: within
the budget in every draw); an eligible candidate's benefit in the draws it overspends in is counted and those draws
are reported (`over_budget_counted`). Ineligible candidates are listed with their counts, never counted. No eligible
candidate: nothing is computed (status no_eligible_plan).

  EVPI  = mean_d NB*(d) - max_p mean_d NB(p, d), p over eligible candidates; NB*(d) = the better of the draw's own
          optimum and every eligible candidate in d (paired by draw; standard error sd/sqrt(n) of the per-draw gain
          over the plan chosen now, and a bootstrap interval over draws that re-chooses that plan).
  EVPPI = mean_d max_p g_p(phi_d) - max_p mean_d NB(p, d) for an input (or a group of inputs) phi, g_p the regression
          of NB(p, .) on phi: Strong, Oakley & Brennan 2014 (Med Decis Making 34:311-326), the nonparametric
          regression estimator. Their GAM is replaced by an unpenalised natural cubic regression spline (additive over a
          group's members) whose number of knots is chosen by generalised cross-validation (GCV), one basis shared by
          every candidate so a single projection serves all of them, the bootstrap and the permutations. Least squares
          with an intercept keeps each candidate's mean, so EVPPI >= 0 exactly. An additive group has no interaction
          terms (the paper's Gaussian process is not implemented): a group's value from interactions is missed.
Small draw counts bias regression EVPPI upward (the max over noisy fitted values). Each EVPPI is reported with a
bootstrap interval and a permutation noise floor: the EVPPI the same smoother finds when the input is shuffled
against the net benefits (no information), at `noise_quantile`; `above_noise_floor` says whether the input clears
it. Below `min_draws` nothing is computed (the step does not run); below `informative_draws` the status is
`indicative` (thresholds.yaml `value_of_information`). An EVPPI above the EVPI is flagged (`exceeds_evpi`): a fit
the paired draws cannot support.

Inputs that were not drawn (fixed parameters, sources not drawn, quantities not exposed by the engine, inputs drawn
without a scalar to regress on) have no value of information here; `not_drawn` lists each with why, so a missing
value is never read as "does not matter". Spec: `analysis.value_of_information: {enabled: true, groups: {name:
[input, ...]}}` (on by default at or above the minimum draws; groups of the drawn inputs' names, as
uncertainty.yaml's samples name them).
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PRODUCER = "toolkit/analysis/value_of_information.py"
STATUSES = ("computed", "indicative", "too_few_draws", "no_eligible_plan")
KEYS = {"enabled", "groups"}
# default groups of the drawn inputs by name prefix (uncertainty.py Drawer names), when two or more are drawn
PREFIX_GROUPS = {"shift:": "calibration shifts", "cost:": "cost multipliers"}
CITATION = ("Strong M, Oakley JE, Brennan A. Estimating multiparameter partial expected value of perfect information "
            "from a probabilistic sensitivity analysis sample: a nonparametric regression approach. Med Decis Making "
            "2014;34(3):311-326")
TOP_SHOWN = 5         # results.md lists this many inputs (all are in the artifact)


def thresholds() -> dict:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return dict(yaml.safe_load(f)["value_of_information"])


def config(raw: dict) -> dict:
    c = ((raw or {}).get("analysis") or {}).get("value_of_information")
    return c if isinstance(c, dict) else {}


def validate(raw: dict) -> list[str]:
    """Spec errors in analysis.value_of_information (toolkit/spec/schema.py validate)."""
    c = ((raw or {}).get("analysis") or {}).get("value_of_information")
    if c is None:
        return []
    if not isinstance(c, dict):
        return ["analysis.value_of_information: a mapping {enabled, groups}"]
    bad = [f"analysis.value_of_information: unknown key {k}" for k in sorted(set(c) - KEYS)]
    if "enabled" in c and not isinstance(c["enabled"], bool):
        bad.append("analysis.value_of_information.enabled: true or false")
    g = c.get("groups")
    if g is not None and not (isinstance(g, dict) and all(isinstance(k, str) and isinstance(v, list) and len(v) >= 1
                                                         and all(isinstance(x, str) for x in v) for k, v in g.items())):
        bad.append("analysis.value_of_information.groups: {group name: [drawn input name, ...]}")
    return bad


def expected(raw: dict, draws) -> bool:
    """Whether a run with `draws` uncertainty draws must carry the artifact: on unless switched off, from the minimum."""
    return config(raw).get("enabled", True) is not False and isinstance(draws, int) and draws >= thresholds()["min_draws"]


# --------------------------------------------------------------------------- the smoother
def _ns_basis(x, k):
    """Natural cubic spline basis without the intercept (Hastie, Tibshirani & Friedman, ESL eq. 5.4-5.5): x and k-2
    truncated-power terms, knots at quantiles of x (k = 2: linear)."""
    if k <= 2:
        return x[:, None]
    xi = np.unique(np.quantile(x, np.linspace(0, 1, k)))
    if len(xi) < 3:
        return x[:, None]

    def dk(j):
        return (np.maximum(x - xi[j], 0) ** 3 - np.maximum(x - xi[-1], 0) ** 3) / (xi[-1] - xi[j])
    last = dk(len(xi) - 2)
    return np.column_stack([x] + [dk(j) - last for j in range(len(xi) - 2)])


def _q(X, k):
    """Orthonormal basis Q of the fitted-value space: intercept plus each column's spline basis (additive)."""
    cols = [np.ones(len(X))]
    for c in X.T:
        s = c.std()
        if s > 0:
            cols += list(_ns_basis((c - c.mean()) / s, k).T)
    U, sv, _ = np.linalg.svd(np.column_stack(cols), full_matrices=False)
    return U[:, : int(np.sum(sv > sv[0] * 1e-10))]


def _fit(X, Y=None, max_df=None, k=None, max_knots=6):
    """(Q, df, knots): the basis size chosen by GCV on Y (the richest allowed without Y), or `k` given; Q is None when
    even the linear fit has more degrees of freedom than `max_df`."""
    X = np.asarray(X, float).reshape(len(X), -1)
    n = len(X)
    md = max(2, int(max_df if max_df is not None else n // 5))
    if k is not None:
        Q = _q(X, k)
        return Q, Q.shape[1], k
    best = None
    for kk in range(2, int(max_knots) + 1):
        Q = _q(X, kk)
        df = Q.shape[1]
        if df > md or df >= n:
            break
        if Y is None:
            best = (0.0, Q, df, kk)
            continue
        R = Y - Q @ (Q.T @ Y)
        gcv = n * float(np.sum(R * R)) / (n - df) ** 2
        if best is None or gcv < best[0] * (1 - 1e-9):
            best = (gcv, Q, df, kk)
    return (None, None, None) if best is None else best[1:]


def hat(X, max_df=None):
    """(hat matrix, degrees of freedom) of the smoother on inputs X (n x m), at the richest basis allowed."""
    Q, df, _ = _fit(X, None, max_df)
    return Q @ Q.T, df


def _evppi(Q, Y):
    G = Q @ (Q.T @ Y)
    return float(np.mean(G.max(1)) - np.max(Y.mean(0)))


def _qs(v, level):
    a = (1 - level) / 2
    return {"low": float(np.quantile(v, a)), "high": float(np.quantile(v, 1 - a)), "sd": float(np.std(v, ddof=1))}


# --------------------------------------------------------------------------- the analysis
def analyse(nb, nb_opt, over, inputs, groups=None, max_share=0.0, level=0.95, thr=None, seed=0, ids=None) -> dict:
    """EVPI and EVPPI from per-draw net benefits `nb` (draws x candidates), each draw's optimum `nb_opt`, the
    over-budget mask `over` (draws x candidates), and the drawn inputs {name: values per draw}. Bad shapes or
    non-finite values fail closed (ValueError)."""
    th = dict(thresholds(), **(thr or {}))
    nb, nb_opt, over = np.asarray(nb, float), np.asarray(nb_opt, float), np.asarray(over, bool)
    if nb.ndim != 2 or nb.shape[0] < 2 or nb.shape[1] < 1 or nb_opt.shape != (nb.shape[0],) or over.shape != nb.shape:
        raise ValueError("value of information: net benefits (draws x candidates), one optimum and one budget flag per "
                         "draw and candidate are required")
    if not (np.all(np.isfinite(nb)) and np.all(np.isfinite(nb_opt))):
        raise ValueError("value of information: every net benefit must be finite")
    n, P = nb.shape
    ids = [str(x) for x in ids] if ids is not None else [f"c{j}" for j in range(P)]
    X = {}
    for k, v in (inputs or {}).items():
        v = np.asarray(v, float)
        if v.shape != (n,) or not np.all(np.isfinite(v)):
            raise ValueError(f"value of information: input {k} needs one finite value per draw")
        X[str(k)] = v
    share = over.mean(0)
    elig = share <= float(max_share) + 1e-12
    out = {"status": None, "draws": n, "min_draws": int(th["min_draws"]), "informative_draws": int(th["informative_draws"]),
           "level": float(level), "max_share_over_budget": float(max_share), "decide_now": None,
           "expected_value_decide_now": None, "expected_value_perfect_information": None, "evpi": None, "inputs": [],
           "candidates": {"n": P, "eligible": int(elig.sum()),
                          "ineligible": [{"id": ids[j], "draws_over_budget": int(over[:, j].sum()),
                                          "share_over_budget": float(share[j])} for j in range(P) if not elig[j]],
                          "over_budget_counted": [{"id": ids[j], "draws_over_budget": int(over[:, j].sum())}
                                                  for j in range(P) if elig[j] and over[:, j].any()],
                          "list": [{"id": ids[j], "expected_value": float(nb[:, j].mean()), "eligible": bool(elig[j]),
                                    "draws_over_budget": int(over[:, j].sum())} for j in range(P)]}}
    if n < th["min_draws"]:
        return dict(out, status="too_few_draws")
    if not elig.any():
        return dict(out, status="no_eligible_plan")
    ej = np.flatnonzero(elig)
    E = nb[:, ej]
    star = np.maximum(nb_opt, E.max(1))
    means = E.mean(0)
    j = int(np.argmax(means))
    v_now, v_pi = float(means[j]), float(star.mean())
    evpi = v_pi - v_now
    diff = star - E[:, j]
    rng = np.random.default_rng(int(seed))
    B = int(th["bootstrap"])
    idx = rng.integers(0, n, (B, n))
    boot = np.array([star[i].mean() - E[i].mean(0).max() for i in idx])
    pct = (lambda v: 100.0 * v / v_now if v_now > 0 else None)
    out.update(status="computed" if n >= th["informative_draws"] else "indicative", decide_now=ids[ej[j]],
               expected_value_decide_now=v_now, expected_value_perfect_information=v_pi,
               evpi={"value": evpi, "pct": pct(evpi), "se_paired": float(diff.std(ddof=1) / math.sqrt(n)),
                     "bootstrap": _qs(boot, level)})
    Y = E - E[:, [j]]                 # incremental net benefit over the plan chosen now (the same EVPPI, better scaled)
    md = max(2, int(th["max_df_share"] * n))
    tol = float(th["exceeds_evpi_tol"]) * max(evpi, 0.0) + 1e-9 * max(1.0, abs(v_now))
    todo = [(k, "input", [k]) for k in sorted(X)]
    for g, mem in (groups or {}).items():
        todo.append((str(g), "group", [str(m) for m in mem]))
    for name, kind, mem in todo:
        Xm = np.column_stack([X[m] for m in mem])
        rec = {"input": name, "kind": kind, "members": mem}
        if np.all(Xm.std(0) == 0):
            out["inputs"].append(dict(rec, status="constant", evppi=0.0, pct=pct(0.0), share_of_evpi=0.0 if evpi > 0
                                      else None, df=1, knots=None, noise_floor=None, above_noise_floor=False,
                                      exceeds_evpi=False, bootstrap=None))
            continue
        Q, df, kn = _fit(Xm, Y, md, max_knots=int(th["max_knots"]))
        if Q is None:
            out["inputs"].append(dict(rec, status="too_many_inputs", evppi=None, reason=f"a linear fit on {len(mem)} "
                                      f"inputs needs more than {md} degrees of freedom at {n} draws"))
            continue
        v = max(_evppi(Q, Y), 0.0)
        perm = np.array([_evppi(Q, Y[rng.permutation(n)]) for _ in range(int(th["permutations"]))])
        bs = []
        for i in idx:
            Qb, _, _ = _fit(Xm[i], None, md, k=kn)
            bs.append(_evppi(Qb, Y[i]))
        fl = float(np.quantile(perm, float(th["noise_quantile"])))
        out["inputs"].append(dict(rec, status="computed", evppi=v, pct=pct(v), share_of_evpi=v / evpi if evpi > 0 else None,
                                  df=int(df), knots=int(kn), noise_floor=fl, noise_mean=float(perm.mean()),
                                  above_noise_floor=bool(v > fl), exceeds_evpi=bool(v > evpi + tol),
                                  bootstrap=_qs(np.array(bs), level)))
    out["inputs"].sort(key=lambda r: (-(r["evppi"] if r.get("evppi") is not None else -1.0), r["input"]))
    return out


class Collector:
    """UQ.run's `on_solve` hook: each draw's sampled inputs, benefit and cost tables and re-optimised plan."""

    def __init__(self, A):
        self.A, self.pk, self.ids, self.rows = A, list(A.pk), list(A.ids), []

    def add(self, d, sampled, ben, cost, sol, events=()):
        at = {k: j for j, k in enumerate(self.pk)}
        self.rows.append({"sampled": dict(sampled), "objective": float(sol["objective"]), "events": list(events),
                          "ben": np.array([[float(ben[i][k]) for k in self.pk] for i in self.ids]),
                          "cost": np.array([[float(cost[i][k]) for k in self.pk] for i in self.ids]),
                          "choice": np.array([at[sol["choice"][i]] for i in self.ids])})


def not_drawn(report: dict, A) -> list[dict]:
    """Everything the draws held fixed, from the uncertainty report's draw plan, plus what was drawn without a scalar."""
    rep = report or {}
    out = [{"input": x.get("name"), "kind": "parameter", "reason": x.get("reason")} for x in rep.get("parameters_fixed") or []]
    out += [{"input": x.get("kind"), "kind": "source", "reason": x.get("reason") or x.get("status")}
            for x in rep.get("sources_not_drawn") or []]
    out += [{"input": str(k), "kind": "not_exposed", "reason": str(v)} for k, v in (rep.get("not_exposed") or {}).items()]
    drawn = set(rep.get("sources_drawn") or [])
    if "emulator" in drawn:
        out.append({"input": "emulator", "kind": "drawn_without_scalar",
                    "reason": "drawn as one joint error field per draw: no scalar input to regress on"})
    if "calibration" in drawn and (A.method in ("likelihood", "posterior") or A.joint is not None):
        out.append({"input": "unit calibration attributes", "kind": "drawn_without_scalar",
                    "reason": "drawn per unit with the calibration rows, not recorded as named inputs"})
    out.append({"input": "inputs not declared uncertain", "kind": "not_declared",
                "reason": "units-table values, prices and other spec inputs held at their signed values"})
    return out


def run(A, col: Collector, plan: dict, variants: dict | None, report: dict, level: float = 0.95, seed: int = 0) -> dict:
    """The value_of_information artifact from the uncertainty step's draws (`col`), the headline plan and the declared
    variants' plans ({id: plan})."""
    rows = col.rows
    if not rows:
        raise ValueError("value of information: no uncertainty draws were collected")
    at = {k: j for j, k in enumerate(col.pk)}
    cand, seen = [], {}

    def add(cid, idx, merge):
        key = tuple(int(x) for x in idx)
        if merge and key in seen:
            return seen[key]
        seen.setdefault(key, cid)
        cand.append((cid, np.asarray(idx)))
        return cid
    add("point", [at[plan[i]] for i in col.ids], False)
    for vid, p in sorted((variants or {}).items()):
        add(f"variant:{vid}", [at[p[i]] for i in col.ids], False)
    ar = np.arange(len(col.ids))
    Bn, Cs = np.stack([r["ben"] for r in rows]), np.stack([r["cost"] for r in rows])
    # the plan that maximises the expected objective (NB is linear in the plan: one solve on the mean tables, within
    # the headline plan's budget). It is a candidate, never a recommendation: when a draw's model corrections bear on
    # it, its values are not trusted and it is left out (with why) instead of stopping the run.
    from toolkit.analysis.core import AnalysisBlocked
    mb, mc = Bn.mean(0), Cs.mean(0)
    ev_note = "solved"
    try:
        sol = A.solve({i: {k: float(mb[u, j]) for j, k in enumerate(col.pk)} for u, i in enumerate(col.ids)},
                      {i: {k: float(mc[u, j]) for j, k in enumerate(col.pk)} for u, i in enumerate(col.ids)})
    except RuntimeError as e:
        if "infeasible" not in str(e):
            raise
        sol, ev_note = None, f"not solved: {e}"
    if sol is not None:
        try:
            for d, r in enumerate(rows):
                A.guard(f"uncertainty draw {d}", [sol["choice"]], r["events"])
            add("expected_value", [at[sol["choice"][i]] for i in col.ids], False)
        except AnalysisBlocked as e:
            ev_note = f"left out: model corrections bear on it at {e.where}"
    opt_of = [add(f"draw:{d}", r["choice"], True) for d, r in enumerate(rows)]
    pos = {c[0]: j for j, c in enumerate(cand)}
    nb = np.column_stack([Bn[:, ar, ix].sum(1) for _, ix in cand])
    cost = np.column_stack([Cs[:, ar, ix].sum(1) for _, ix in cand])
    nb_opt = np.array([nb[d, pos[c]] for d, c in enumerate(opt_of)])
    obj = np.array([r["objective"] for r in rows])
    gap = float(np.max(np.abs(nb_opt - obj)))
    if gap > 1e-6 * max(1.0, float(np.max(np.abs(obj)))):
        raise ValueError(f"value of information: a draw's re-optimised objective is not its benefit table summed at its "
                         f"plan (gap {gap:.3g})")
    names = set.intersection(*(set(r["sampled"]) for r in rows))
    inputs = {k: [float(r["sampled"][k]) for r in rows] for k in sorted(names)}
    groups, skipped = {}, []
    for pre, g in PREFIX_GROUPS.items():
        mem = sorted(k for k in inputs if k.startswith(pre))
        if len(mem) >= 2:
            groups[g] = mem
    for g, mem in (config(A.spec.raw).get("groups") or {}).items():
        miss = [m for m in mem if m not in inputs]
        if miss:
            skipped.append({"group": g, "members": list(mem), "reason": f"not drawn inputs: {miss}"})
        else:
            groups[g] = list(mem)
    rule = (A.spec.raw.get("uncertainty") or {}).get("affordability") or {}
    res = analyse(nb, nb_opt, cost > float(A.budget) * (1 + 1e-9), inputs, groups,
                  float(rule.get("max_share_over_budget", 0.0)), level, seed=seed, ids=[c[0] for c in cand])
    res.update(outcome=A.primary, discount_factor=float(A.dfac), budget_allocatable=float(A.budget),
               groups=groups, groups_not_computed=skipped, expected_value_plan=ev_note, not_drawn=not_drawn(report, A),
               consistency={"objective_max_abs_gap": gap},
               method={"estimator": "regression EVPPI (nonparametric, per candidate plan)", "citation": CITATION,
                       "smoother": "natural cubic regression spline, additive over a group, knots by GCV",
                       "candidates": "the headline plan, the declared plan variants, the plan maximising the expected "
                                     "objective (mean tables), each draw's re-optimised plan",
                       "infeasibility_rule": "a candidate over the allocatable budget in a draw is infeasible there; "
                                             "eligible to be chosen now only if infeasible in at most the affordability "
                                             "rule's share of draws (0 without a rule)"})
    return res


# --------------------------------------------------------------------------- gate, claims, results
def contract_findings(raw: dict, unc, voi) -> list[str]:
    """The artifact the spec asks for, and its shape and arithmetic (fail closed)."""
    draws = (unc or {}).get("draws") if isinstance(unc, dict) else None
    exp = expected(raw, draws)
    if voi is None:
        return ([f"the spec asks for value of information at {draws} uncertainty draws (at least "
                 f"{thresholds()['min_draws']}) and analysis/value_of_information.yaml is missing"] if exp else [])
    if not exp:
        return ["analysis/value_of_information.yaml is present but the spec switches it off or the draws "
                f"({draws}) are below the minimum"]
    if not isinstance(voi, dict):
        return ["value_of_information: the payload is not a mapping"]
    th, bad = thresholds(), []
    st = voi.get("status")
    if st not in STATUSES:
        return [f"value_of_information: unknown status {st!r}"]
    if voi.get("draws") != draws:
        bad.append(f"value_of_information: {voi.get('draws')!r} draws, the uncertainty step has {draws}")
    want = "computed" if draws >= th["informative_draws"] else "indicative"
    if st in ("computed", "indicative", "too_few_draws") and st != want:
        bad.append(f"value_of_information: status {st} at {draws} draws (expected {want})")
    for k in ("not_drawn", "inputs"):
        if not isinstance(voi.get(k), list):
            bad.append(f"value_of_information: {k} is not a list")
    if bad or st == "no_eligible_plan":
        return bad + (["value_of_information: no eligible plan but an EVPI"] if st == "no_eligible_plan"
                      and voi.get("evpi") is not None else [])

    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    ev, v0 = voi.get("evpi"), voi.get("expected_value_decide_now")
    if not (isinstance(ev, dict) and num(ev.get("value")) and num(ev.get("se_paired")) and num(v0)):
        return ["value_of_information: EVPI, its standard error or the value of deciding now is missing or not finite"]
    tol = float(th["exceeds_evpi_tol"]) * max(ev["value"], 0.0) + 1e-9 * max(1.0, abs(v0))
    if ev["value"] < -1e-9 * max(1.0, abs(v0)):
        bad.append(f"value_of_information: EVPI {ev['value']} is negative")
    for r in voi["inputs"]:
        if not isinstance(r, dict) or not isinstance(r.get("input"), str):
            bad.append(f"value_of_information: input entry {r!r} malformed")
            continue
        if r.get("status") == "too_many_inputs":
            continue
        e = r.get("evppi")
        if not num(e) or e < -1e-9 * max(1.0, abs(v0)):
            bad.append(f"value_of_information: {r['input']}: EVPPI {e!r} is not a finite non-negative number")
        elif r.get("exceeds_evpi") is not bool(e > ev["value"] + tol):
            bad.append(f"value_of_information: {r['input']}: EVPPI {e} against EVPI {ev['value']} disagrees with its "
                       f"exceeds_evpi flag")
        elif num(r.get("noise_floor")) and r.get("above_noise_floor") is not bool(e > r["noise_floor"]):
            bad.append(f"value_of_information: {r['input']}: above_noise_floor disagrees with the noise floor")
    return bad


def _shown(data):
    return [r for r in data.get("inputs") or [] if r.get("evppi") is not None][:TOP_SHOWN]


def claims(c, art, data) -> list[dict]:
    """Claim specs (toolkit/analysis/claims.py `_c`): status, EVPI and the inputs results.md shows."""
    if data.get("evpi") is None:
        return [c("voi.status", "label", art, "data.status")]
    o = data["outcome"]
    cl = [c("voi.status", "label", art, "data.status"),
          c("voi.decide_now", "scalar", art, "data.expected_value_decide_now", o),
          c("voi.evpi", "scalar", art, "data.evpi.value", o), c("voi.evpi_se", "scalar", art, "data.evpi.se_paired", o)]
    if data["evpi"].get("pct") is not None:
        cl.append(c("voi.evpi_pct", "percentage", art, "data.evpi.pct", "%"))
    for k, r in enumerate(_shown(data)):
        j = data["inputs"].index(r)
        cl.append(c(f"voi.top{k}.evppi", "scalar", art, f"data.inputs[{j}].evppi", o))
        if r.get("pct") is not None:
            cl.append(c(f"voi.top{k}.evppi_pct", "percentage", art, f"data.inputs[{j}].pct", "%"))
    return cl


def results_lines(data) -> list[str]:
    L = ["## Value of information", ""]
    if data.get("evpi") is None:
        why = {"no_eligible_plan": "no candidate plan stays within the allocatable budget in enough draws (see "
                                   "`candidates.ineligible`)"}.get(data["status"], data["status"])
        return L + [f"Not computed: {why} (status [CLAIM:voi.status]).", ""]
    o, dfac = data["outcome"], data.get("discount_factor", 1.0)
    unit = o + ("" if dfac == 1 else f" (discounted, factor {dfac:.4g})")
    L += [f"Over {data['draws']} uncertainty draws (status [CLAIM:voi.status]), the best plan chosen now "
          f"(`{data['decide_now']}`, among {data['candidates']['eligible']} eligible candidate plans) is expected to "
          f"avert [CLAIM:voi.decide_now] {unit}. Knowing every drawn input before choosing would add "
          f"[CLAIM:voi.evpi] {o} (paired standard error [CLAIM:voi.evpi_se]"
          + ("; [CLAIM:voi.evpi_pct] of that value" if data["evpi"].get("pct") is not None else "")
          + "): the expected value of perfect information, the most better data on these inputs could add to this "
          "decision. Value of partial perfect information by input (regression method, Strong, Oakley & Brennan "
          "2014):", "", "| Input | Kind | EVPPI | Share of the value chosen now | Above its noise floor |",
          "|---|---|---|---|---|"]
    for k, r in enumerate(_shown(data)):
        L.append(f"| {r['input']} | {r['kind']} | [CLAIM:voi.top{k}.evppi] | "
                 + (f"[CLAIM:voi.top{k}.evppi_pct]" if r.get("pct") is not None else "n/a") + " | "
                 + ("yes" if r.get("above_noise_floor") else "no") + " |")
    nd = data.get("not_drawn") or []
    L += ["", f"{len(data['inputs'])} inputs and groups are valued in analysis/value_of_information.yaml. An input "
          "within its noise floor shows no value this many draws can distinguish from none"
          + (f"; below {data['informative_draws']} draws every value is indicative (regression EVPPI is biased "
             "upward at small draw counts)" if data["status"] == "indicative" else "") + ". "
          f"Inputs that were not drawn have no value of information here because they did not vary, which does not "
          f"mean they do not matter ({len(nd)} listed in `not_drawn`: "
          + ", ".join(str(x["input"]) for x in nd[:8]) + ("..." if len(nd) > 8 else "") + ").", ""]
    return L


# --------------------------------------------------------------------------- self-test
def _valid_doc(n=64):
    rng = np.random.default_rng(3)
    x1, x2 = rng.normal(size=n), rng.normal(size=n)
    nb = np.column_stack([np.zeros(n), x1 + 0.1 * rng.normal(size=n)]) + 100.0
    return dict(not_drawn=[], **analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"a": x1, "b": x2}, seed=1,
                   thr=dict(thresholds(), min_draws=30)))


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def run_case(label, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- a missing or broken function is a failing case
            ok(False, f"{label}: {type(e).__name__}: {e}")

    def toy():
        # VI1-VI3: two plans; plan B's net benefit over A is phi1 plus a little noise, phi2 is unrelated. The choice
        # turns on phi1 only: EVPPI(phi1) is close to EVPI, EVPPI(phi2) is within its permutation noise floor.
        rng = np.random.default_rng(11)
        n = 400
        p1, p2 = rng.normal(0.2, 1.0, n), rng.normal(0.0, 1.0, n)
        inb = p1 + 0.1 * rng.normal(size=n)
        nb = np.column_stack([np.full(n, 50.0), 50.0 + inb]) + rng.normal(0, 3.0, n)[:, None]   # shared noise cancels
        r = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"phi1": p1, "phi2": p2}, seed=2)
        ev = {x["input"]: x for x in r["inputs"]}
        evpi = r["evpi"]["value"]
        exact = float(np.mean(np.maximum(inb, 0)) - max(0.0, float(np.mean(inb))))
        ok(r["status"] == "indicative" and abs(evpi - exact) < 1e-9,
           f"VI1 EVPI is mean(max) - max(mean) over the paired draws ({evpi:.4f} vs {exact:.4f}, {r['status']})")
        ok(ev["phi1"]["evppi"] > 0.8 * evpi and ev["phi1"]["above_noise_floor"] is True
           and ev["phi2"]["evppi"] < 0.1 * evpi and ev["phi2"]["above_noise_floor"] is False
           and r["inputs"][0]["input"] == "phi1",
           f"VI2 the input that drives the choice ranks first and clears its noise floor; the other does not "
           f"({ev['phi1']['evppi']:.4f}, {ev['phi2']['evppi']:.4f} of {evpi:.4f})")
        tol = 1e-9 + 0.02 * evpi
        ok(all(-1e-12 <= x["evppi"] <= evpi + tol for x in r["inputs"]) and evpi >= 0,
           "VI3 0 <= EVPPI <= EVPI (within tolerance) for every input")
        b = r["evpi"]["bootstrap"]
        ok(b["low"] <= evpi <= b["high"] and r["evpi"]["se_paired"] > 0
           and all(x["bootstrap"]["low"] <= x["bootstrap"]["high"] for x in r["inputs"]),
           f"VI4 EVPI carries a paired standard error and a bootstrap interval; each EVPPI a bootstrap interval ({b})")
        # three plans, random tables: the ordering EVPI >= every EVPPI >= 0 holds for groups too
        rng = np.random.default_rng(5)
        X = rng.uniform(size=(200, 3))
        nb3 = np.column_stack([X[:, 0] * 2, X[:, 1] + X[:, 2], np.full(200, 1.0)]) + rng.normal(0, 0.2, (200, 3))
        r3 = analyse(nb3, nb3.max(1), np.zeros_like(nb3, bool), {f"x{k}": X[:, k] for k in range(3)},
                     groups={"x1x2": ["x1", "x2"]}, seed=3)
        e3 = r3["evpi"]["value"]
        grp = [x for x in r3["inputs"] if x["kind"] == "group"]
        ok(grp and all(-1e-12 <= x["evppi"] <= e3 * 1.02 + 1e-9 for x in r3["inputs"])
           and grp[0]["evppi"] >= max(x["evppi"] for x in r3["inputs"] if x["input"] in ("x1", "x2")) - 0.02 * e3,
           "VI5 three plans: a group's EVPPI is at least each member's and at most EVPI; all non-negative")

    def paired():
        # VI6: hand arithmetic on four draws and two plans, paired by draw
        nb = np.array([[10.0, 12.0], [10.0, 8.0], [10.0, 11.0], [10.0, 9.0]])
        r = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"x": np.array([1.0, 2.0, 3.0, 4.0])},
                    thr=dict(thresholds(), min_draws=4, informative_draws=4))
        diff = nb.max(1) - nb[:, 0]          # plan 0 (mean 10) vs plan 1 (mean 10): tie -> the first
        ok(abs(r["evpi"]["value"] - 0.75) < 1e-12 and abs(r["evpi"]["se_paired"] - diff.std(ddof=1) / 2) < 1e-12
           and abs(r["evpi"]["pct"] - 7.5) < 1e-9 and abs(r["expected_value_decide_now"] - 10.0) < 1e-12
           and abs(r["expected_value_perfect_information"] - 10.75) < 1e-12,
           f"VI6 paired arithmetic: EVPI 0.75 (7.5% of 10; per-draw best 12, 10, 11, 10), SE sd(diff)/sqrt(4) ({r.get('evpi')})")
        # VI7: the per-draw optimum can beat every candidate in a draw; perfect information takes the best of both
        r2 = analyse(nb, np.array([13.0, 10.0, 11.0, 10.0]), np.zeros_like(nb, bool), {"x": np.arange(4.0)},
                     thr=dict(thresholds(), min_draws=4))
        ok(abs(r2["expected_value_perfect_information"] - 11.0) < 1e-12,
           "VI7 per draw, perfect information is the better of the draw's optimum and every eligible candidate")

    def budget():
        nb = np.array([[10.0, 14.0], [10.0, 14.0], [10.0, 14.0], [10.0, 14.0]])
        over = np.array([[False, True], [False, False], [False, False], [False, False]])
        th = dict(thresholds(), min_draws=4)
        r = analyse(nb, nb[:, 0].copy(), over, {"x": np.arange(4.0)}, thr=th, ids=["point", "draw:1"])
        c = r["candidates"]
        ok(c["eligible"] == 1 and c["ineligible"] == [{"id": "draw:1", "draws_over_budget": 1, "share_over_budget": 0.25}]
           and r["decide_now"] == "point" and abs(r["evpi"]["value"]) < 1e-12,
           f"VI8 a plan over the budget in one draw is infeasible there; with no affordability rule it cannot be the "
           f"decision now, and is listed with its count, not counted ({c}, {r.get('decide_now')})")
        r2 = analyse(nb, nb[:, 0].copy(), over, {"x": np.arange(4.0)}, max_share=0.25, thr=th, ids=["point", "draw:1"])
        ok(r2["decide_now"] == "draw:1" and r2["candidates"]["eligible"] == 2
           and r2["candidates"]["over_budget_counted"] == [{"id": "draw:1", "draws_over_budget": 1}],
           "VI9 with the spec's affordability rule (at most 25% of draws over), the plan is eligible and the draw it "
           "overspends in is counted and reported")
        r3 = analyse(nb, nb[:, 0].copy(), np.ones_like(over), {"x": np.arange(4.0)}, thr=th)
        ok(r3["status"] == "no_eligible_plan" and r3["evpi"] is None and r3["inputs"] == [],
           "VI10 no candidate within the budget in enough draws: not computed, with the reason, never a number")

    def floors():
        rng = np.random.default_rng(1)
        nb = rng.normal(size=(10, 2))
        r = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"x": rng.normal(size=10)})
        ok(r["status"] == "too_few_draws" and r["evpi"] is None and r["min_draws"] == thresholds()["min_draws"],
           f"VI11 below the minimum draws nothing is computed ({r['status']})")
        ok(thresholds()["min_draws"] >= 20 and thresholds()["informative_draws"] > thresholds()["min_draws"],
           "VI12 the minimum and the informative draw counts are read from thresholds.yaml")
        H, df = hat(np.linspace(0, 1, 50)[:, None])
        y = np.sin(3 * np.linspace(0, 1, 50))
        ok(abs((H @ y).mean() - y.mean()) < 1e-12 and np.max(np.abs(H @ y - y)) < 0.02 and 2 <= df <= 10,
           f"VI13 the smoother keeps the mean (intercept) and follows a smooth curve (df {df})")

    def gate():
        doc = _valid_doc()
        raw = {"uncertainty": {"draws": 64}}
        unc = {"draws": 64}
        ok(contract_findings(raw, unc, doc) == [], f"VG1 a well-formed artifact passes: {contract_findings(raw, unc, doc)}")
        ok(contract_findings(raw, unc, None), "VG2 the artifact is expected (enabled, enough draws) and missing")
        ok(contract_findings({"analysis": {"value_of_information": {"enabled": False}}}, unc, None) == []
           and contract_findings(raw, {"draws": 8}, None) == [],
           "VG3 switched off, or below the minimum draws: no artifact is expected")
        ok(contract_findings({"analysis": {"value_of_information": {"enabled": False}}}, unc, doc),
           "VG4 an artifact for a spec that switched it off is a finding")
        import copy
        muts = [("not a mapping", lambda d: "x"), ("an unknown status", lambda d: dict(d, status="fine")),
                ("other draws", lambda d: dict(d, draws=63)), ("no evpi", lambda d: dict(d, evpi=None)),
                ("a negative EVPI", lambda d: dict(d, evpi=dict(d["evpi"], value=-1.0))),
                ("a non-finite EVPI", lambda d: dict(d, evpi=dict(d["evpi"], value=float("nan")))),
                ("inputs not a list", lambda d: dict(d, inputs={"a": 1})),
                ("a negative EVPPI", lambda d: dict(d, inputs=[dict(d["inputs"][0], evppi=-1.0)] + d["inputs"][1:])),
                ("EVPPI above EVPI unflagged", lambda d: dict(d, inputs=[dict(d["inputs"][0], evppi=d["evpi"]["value"] * 2,
                                                                              exceeds_evpi=False)])),
                ("not_drawn missing", lambda d: {k: v for k, v in d.items() if k != "not_drawn"}),
                ("an indicative label on enough draws", lambda d: dict(d, status="computed")),
                ("too few draws claimed on enough", lambda d: dict(d, status="too_few_draws"))]
        for label, m in muts:
            ok(bool(contract_findings(raw, unc, m(copy.deepcopy(doc)))), f"VG5 {label} is a finding (fail closed)")
        ok(validate({"analysis": {"value_of_information": {"enabled": "yes"}}})
           and validate({"analysis": {"value_of_information": {"bogus": 1}}})
           and validate({"analysis": {"value_of_information": {"groups": {"g": "a"}}}})
           and validate({"analysis": {"value_of_information": {"groups": {"g": ["a", "b"]}, "enabled": True}}}) == []
           and validate({}) == [], "VG6 spec validation: enabled a boolean, groups of input names, no unknown key")
        ok(expected({}, 64) is True and expected({}, 8) is False
           and expected({"analysis": {"value_of_information": {"enabled": False}}}, 64) is False,
           "VG7 on by default at or above the minimum draws; off below it or when switched off")

    for label, fn in (("VI1-5 toy", toy), ("VI6-7 paired", paired), ("VI8-10 budget", budget),
                      ("VI11-13 floors", floors), ("VG gate", gate)):
        run_case(label, fn)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
