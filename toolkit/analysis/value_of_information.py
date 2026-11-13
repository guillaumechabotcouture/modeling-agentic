"""Value of information over the uncertainty draws (F6): which uncertain input is worth better data.

The decision is a plan (one package per unit) under the signed feasibility (eligibility, caps, group rules) and the
budget. In uncertainty draw d, plan p is worth NB(p, d) = sum over units of the draw's benefit table at p's package:
the objective the solver maximises (discounted primary outcome averted). The uncertainty step evaluates and
re-optimises every draw; this step reads those tables through its `on_solve` hook (no new engine runs; it adds ILP
solves on those tables only).

Deciding now: the best plan within the allocatable budget in every draw (one solve on the mean tables with one budget
row per draw beside the signed caps: exact when no overspend is allowed). With `uncertainty.affordability.
max_share_over_budget` > 0 it is the best candidate infeasible in at most that share of draws (its benefit counted
where it overspends): a lower bound on the value of deciding now, labelled so.
Perfect information: in each draw the best plan feasible there (its own optimum within the budget, solved at the full
budget when the draws were re-optimised within a reserve). Costs are then known, so no overspend is needed; with an
allowance, perfect information may overspend in as many draws as the plan chosen now may (floor(share x draws)), where
the draw's plan solved without the budget (or the best candidate) gains most. So EVPI >= 0 and is never inflated by
overspending.
Candidates: the headline plan, every declared plan variant, the plan decided now, each draw's optimum, and the plan
decided within the budget on each bin of each valued input and group, built on one half of the draws and valued,
for that input only, on the other half (cross-fitting, so its fit to the input reaches neither the draws it is valued
on nor the noise floor). A candidate that
breaks the signed feasibility or on which a model correction bears in any draw is reported with its regret, never
chosen or counted. No eligible candidate: status no_eligible_plan.

  EVPI  = mean_d NB*(d) - value of deciding now, paired by draw (standard error of the per-draw gain over the plan
          chosen now, conditional on that plan).
  EVPPI = mean_d max_p g_p(phi_d) - value of deciding now for an input (or a group) phi, g_p the regression of
          NB(p, .) on phi (Strong, Oakley & Brennan 2014, Med Decis Making 34:311-326), p over the candidates eligible
          within phi_d's quantile bin under the same affordability rule: feasibility after learning is conditional on
          what is learned, judged within bins (one bin per value when an input has at most `bins` values; a lower
          bound for inputs that move costs). With an allowance, the chosen per-draw policy must itself keep the
          share; otherwise eligibility falls back to within the budget in every draw of a bin (a lower bound,
          recorded as `policy_rule`). The paper's GAM is replaced
          by an unpenalised natural cubic regression spline (additive over a group, binned on its first principal
          component), knots chosen by GCV. An additive group has no interaction terms (the paper's Gaussian process is
          not implemented). EVPPI is over the candidate set, never below 0 (ignoring what is learned is always open).
Small draw counts bias regression EVPPI upward. Each EVPPI carries a permutation noise floor: the same full procedure
(GCV, bins, cross-fitting) on the input shuffled within each half against the net benefits, at `noise_quantile`. No interval is published (a
percentile bootstrap around a maximisation does not cover near ties). A value is `supported` only when it clears its
noise floor and does not exceed EVPI (`exceeds_evpi`); only supported values are claimed. Below `min_draws` nothing
is computed and the artifact records too_few_draws; below `informative_draws` the status is `indicative`
(thresholds.yaml `value_of_information`).

Inputs that were not drawn (fixed parameters, sources not drawn, quantities not exposed by the engine, inputs drawn
without a scalar to regress on) have no value of information here; `not_drawn` lists each with why, so a missing
value is never read as "does not matter". Spec: `analysis.value_of_information: {enabled: true, groups: {name:
[input, ...]}}` (on by default; groups of the drawn inputs' names, as uncertainty.yaml's samples name them).
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


def expected(raw: dict) -> bool:
    """Whether a run must carry the artifact: on unless switched off (below the minimum draws it records
    too_few_draws)."""
    return config(raw).get("enabled", True) is not False


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


def _bins(Xm, G):
    """Bin of each draw: quantile bins of the input (a group: of its standardised first principal component); equal
    values always share a bin, so a discrete input gets one bin per value when it has at most G values."""
    Z = (Xm - Xm.mean(0)) / np.where(Xm.std(0) > 0, Xm.std(0), 1.0)
    u = Z[:, 0] if Z.shape[1] == 1 else Z @ np.linalg.svd(Z, full_matrices=False)[2][0]
    vals = np.unique(u)
    if len(vals) <= int(G):          # at most G distinct values: one bin per value (round 2: quantiles merge them)
        return np.searchsorted(vals, u)
    edges = np.unique(np.quantile(u, np.linspace(0, 1, int(G) + 1)[1:-1]))
    return np.searchsorted(edges, u, side="left")


INTERVAL_NOTE = ("No interval is published: a percentile bootstrap around a maximisation does not cover its target "
                 "near ties. Regression EVPPI is biased upward at small draw counts by fitting noise (about its "
                 "noise_mean); the value of deciding now is a maximum of sample means, so EVPI is biased downward "
                 "near ties. The paired standard error is conditional on the plan chosen now.")


# --------------------------------------------------------------------------- the analysis
def analyse(nb, nb_opt, over, inputs, groups=None, max_share=0.0, level=0.95, thr=None, seed=0, ids=None,
            excluded=None, exact_id=None, nb_free=None, cond=None, halves=None) -> dict:
    """EVPI and EVPPI from per-draw net benefits `nb` (draws x candidates), each draw's own optimum within the budget
    `nb_opt`, the over-budget mask `over` (draws x candidates) and the drawn inputs {name: values per draw}.
    `excluded` {candidate id: why}: candidates that break the signed feasibility or carry model corrections, reported
    (with their regret) and never chosen or counted. `exact_id`: the candidate solved as the best plan within the
    budget in every draw (with no overspend allowed, deciding now is then exact). `nb_free`: each draw's best plan
    solved without the budget (the plans perfect information may overspend with). `cond` {input: [(candidate, half)]}:
    plans decided within that input's bins on one half of the draws (`halves`, 0/1 per draw); each is valued only for
    that input and only on the other half (cross-fitting), so its fit to the input never reaches the draws it is
    valued on, nor the noise floor. Bad input fails closed."""
    th = dict(thresholds(), **(thr or {}))
    nb, nb_opt, over = np.asarray(nb, float), np.asarray(nb_opt, float), np.asarray(over, bool)
    if nb.ndim != 2 or nb.shape[0] < 2 or nb.shape[1] < 1 or nb_opt.shape != (nb.shape[0],) or over.shape != nb.shape:
        raise ValueError("value of information: net benefits (draws x candidates), one optimum and one budget flag per "
                         "draw and candidate are required")
    if not (np.all(np.isfinite(nb)) and np.all(np.isfinite(nb_opt))):
        raise ValueError("value of information: every net benefit must be finite")
    n, P = nb.shape
    ids = [str(x) for x in ids] if ids is not None else [f"c{j}" for j in range(P)]
    excl = {str(k): str(v) for k, v in (excluded or {}).items()}
    X = {}
    for k, v in (inputs or {}).items():
        v = np.asarray(v, float)
        if v.shape != (n,) or not np.all(np.isfinite(v)):
            raise ValueError(f"value of information: input {k} needs one finite value per draw")
        X[str(k)] = v
    s = float(max_share)
    okc = np.array([i not in excl for i in ids])
    feas = ~over & okc[None, :]
    share = (~feas).mean(0)
    elig = okc & (share <= s + 1e-12)
    lst = [dict({"id": ids[j], "expected_value": float(nb[:, j].mean()), "eligible": bool(elig[j]),
                 "draws_over_budget": int(over[:, j].sum())}, **({"excluded": excl[ids[j]]} if ids[j] in excl else {}))
           for j in range(P)]
    out = {"status": None, "draws": n, "min_draws": int(th["min_draws"]), "informative_draws": int(th["informative_draws"]),
           "level": float(level), "max_share_over_budget": s, "decide_now": None, "decide_now_method": None,
           "expected_value_decide_now": None, "expected_value_perfect_information": None, "evpi": None, "inputs": [],
           "interval_note": INTERVAL_NOTE,
           "candidates": {"n": P, "eligible": int(elig.sum()),
                          "ineligible": [{"id": ids[j], "draws_over_budget": int(over[:, j].sum()),
                                          "share_over_budget": float(share[j])} for j in range(P) if okc[j] and not elig[j]],
                          "over_budget_counted": [{"id": ids[j], "draws_over_budget": int(over[:, j].sum())}
                                                  for j in range(P) if elig[j] and over[:, j].any()], "list": lst}}
    if n < th["min_draws"]:
        return dict(out, status="too_few_draws")
    if not elig.any():
        return dict(out, status="no_eligible_plan")
    means = np.where(elig, nb.mean(0), -np.inf)
    j = int(np.argmax(means))
    v_now = float(means[j])
    for x in lst:
        x["regret"] = v_now - x["expected_value"]
    # perfect information: in each draw the best plan feasible there (the draw's own optimum within the budget, or a
    # feasible candidate). With an overspend allowance, perfect information may overspend in as many draws as the plan
    # chosen now may (floor(share x draws)), taking the best feasible-ignoring-budget candidate where that gains most.
    star = np.maximum(nb_opt, np.where(feas, nb, -np.inf).max(1))
    k = int(math.floor(s * n + 1e-9))
    if k:
        free = np.where(okc[None, :], nb, -np.inf).max(1)
        if nb_free is not None:
            free = np.maximum(free, np.asarray(nb_free, float))
        gain = np.maximum(free - star, 0.0)
        star = star + np.where(np.isin(np.arange(n), np.argsort(-gain, kind="stable")[:k]), gain, 0.0)
    v_pi = float(star.mean())
    evpi = max(v_pi - v_now, 0.0)
    pct = (lambda v: 100.0 * v / v_now if v_now > 0 else None)
    ex = ids.index(exact_id) if exact_id in ids else None
    exact = s == 0 and ex is not None and elig[ex] and nb[:, ex].mean() >= v_now - 1e-12 * max(1.0, abs(v_now))
    out.update(status="computed" if n >= th["informative_draws"] else "indicative", decide_now=ids[j],
               decide_now_method="exact" if exact else "over the candidate set (a lower bound on the value of "
               "deciding now, so EVPI may be overstated)",
               expected_value_decide_now=v_now, expected_value_perfect_information=v_now + evpi,
               evpi={"value": evpi, "pct": pct(evpi), "se_paired": float((star - nb[:, j]).std(ddof=1) / math.sqrt(n))})
    D = nb - nb[:, [j]]                     # incremental net benefit over the plan chosen now
    cmap = {str(k): [(int(c), int(h)) for c, h in v] for k, v in (cond or {}).items()}
    is_cond = np.zeros(P, bool)
    for v_ in cmap.values():
        for c, _ in v_:
            is_cond[c] = True
    gen = np.flatnonzero(okc & ~is_cond)    # excluded never counted; conditional plans only for their own input
    hv = np.zeros(n, int) if halves is None else np.asarray(halves, int)
    G = max(1, min(int(th["bins"]), n // int(th["min_bin_draws"])))
    tol = float(th["exceeds_evpi_tol"]) * evpi + 1e-9 * max(1.0, abs(v_now))
    rng = np.random.default_rng(int(seed))

    def fitted(Xm, cols, rows):
        Yc = D[np.ix_(rows, cols)]
        Q, df, kn = _fit(Xm[rows], Yc, max(2, int(th["max_df_share"] * len(rows))), max_knots=int(th["max_knots"]))
        return (None, None, None) if Q is None else (Q @ (Q.T @ Yc), df, kn)

    def value(Xm, conds, strict=False):
        """EVPPI with the full procedure: GCV on the data; per bin, the best plan eligible within the bin under the
        affordability rule (feasibility after learning is conditional on what is learned); conditional plans fitted
        and valued on the half they were not built on. The chosen per-draw policy must itself keep the affordability
        share; when it does not, eligibility within bins falls back to within the budget in every draw (strict)."""
        Gg, df, kn = fitted(Xm, gen, np.arange(n))
        if Gg is None:
            return None, None, None, None, None
        b, lim = _bins(Xm, G), (0.0 if strict else s)
        best, pick = np.full(n, -np.inf), np.full(n, -1)

        def take(rows, cols, Gh):
            for g in np.unique(b[rows]):
                m = b[rows] == g
                rr = rows[m]
                e = (~feas[np.ix_(rr, cols)]).mean(0) <= lim + 1e-12
                if e.any():
                    vals = Gh[m][:, e]
                    up = vals.max(1) > best[rr]
                    best[rr[up]], pick[rr[up]] = vals.max(1)[up], cols[e][vals.argmax(1)[up]]
        take(np.arange(n), gen, Gg)
        for h in np.unique(hv):
            rows = np.flatnonzero(hv == h)
            cc = np.array([c for c, bh in conds if bh != h and okc[c]], int)
            if len(cc) and len(rows) >= 4:
                Gc = fitted(Xm, cc, rows)[0]
                if Gc is not None:
                    take(rows, cc, Gc)
        miss = pick < 0                      # no eligible plan in the bin: keep the plan chosen now
        best[miss], pick[miss] = 0.0, j
        share = float(over[np.arange(n), pick].mean())
        if s > 0 and not strict and share > s + 1e-12:
            return value(Xm, conds, strict=True)
        return max(float(best.mean()), 0.0), df, kn, share, strict      # ignoring what is learned is always open

    def shuffled():
        idx = np.arange(n)                   # within each half, so conditional plans stay independent of their half
        for h in np.unique(hv):
            r = np.flatnonzero(hv == h)
            idx[r] = r[rng.permutation(len(r))]
        return idx

    todo = [(k_, "input", [k_]) for k_ in sorted(X)] + [(str(g), "group", [str(m) for m in mem])
                                                        for g, mem in (groups or {}).items()]
    for name, kind, mem in todo:
        Xm = np.column_stack([X[m] for m in mem])
        rec = {"input": name, "kind": kind, "members": mem, "bins": G}
        if np.all(Xm.std(0) == 0):
            out["inputs"].append(dict(rec, status="constant", evppi=0.0, pct=pct(0.0), share_of_evpi=None, df=1,
                                      knots=None, noise_floor=0.0, noise_mean=0.0, above_noise_floor=False,
                                      exceeds_evpi=False, supported=False))
            continue
        conds = cmap.get(name, [])
        v, df, kn, psh, strict = value(Xm, conds)
        if v is None:
            out["inputs"].append(dict(rec, status="too_many_inputs", evppi=None, supported=False,
                                      reason=f"a linear fit on {len(mem)} inputs needs more than {md} degrees of "
                                             f"freedom at {n} draws"))
            continue
        perm = np.array([value(Xm[shuffled()], conds)[0] for _ in range(int(th["permutations"]))])
        fl = float(np.quantile(perm, float(th["noise_quantile"])))
        above, exceeds = bool(v > fl), bool(v > evpi + tol)
        out["inputs"].append(dict(rec, status="computed", evppi=v, pct=pct(v), share_of_evpi=v / evpi if evpi > 0 else None,
                                  df=int(df), knots=int(kn), noise_floor=fl, noise_mean=float(perm.mean()),
                                  conditional_plans=len(conds), policy_share_over_budget=psh,
                                  policy_rule="within the budget in every draw of a bin" if strict
                                  else "the affordability share within each bin",
                                  above_noise_floor=above, exceeds_evpi=exceeds,
                                  supported=bool(above and not exceeds and evpi > tol)))
    out["inputs"].sort(key=lambda r: (-(r["evppi"] if r.get("evppi") is not None else -1.0), r["input"]))
    return out


class Collector:
    """UQ.run's `on_solve` hook: each draw's sampled inputs, benefit and cost tables, re-optimised plan and model
    corrections."""

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


def too_few(draws: int, report: dict, A) -> dict:
    """The artifact below the minimum draws: the count and the minimum, nothing computed."""
    return {"status": "too_few_draws", "draws": int(draws), "min_draws": int(thresholds()["min_draws"]), "evpi": None,
            "inputs": [], "not_drawn": not_drawn(report, A), "outcome": A.primary}


def run(A, col: Collector, plan: dict, variants: dict | None, report: dict, level: float = 0.95, seed: int = 0,
        thr=None) -> dict:
    """The value_of_information artifact from the uncertainty step's draws (`col`), the headline plan and the declared
    variants' plans ({id: plan})."""
    from toolkit.alloc import optimize as aopt
    from toolkit.analysis.core import AnalysisBlocked
    th = dict(thresholds(), **(thr or {}))
    rows = col.rows
    if not rows:
        raise ValueError("value of information: no uncertainty draws were collected")
    n, pk, ids_u = len(rows), col.pk, col.ids
    at = {k: j for j, k in enumerate(pk)}
    Bn, Cs = np.stack([r["ben"] for r in rows]), np.stack([r["cost"] for r in rows])
    B = float(A.budget)
    s = float(((A.spec.raw.get("uncertainty") or {}).get("affordability") or {}).get("max_share_over_budget", 0.0))
    tab = (lambda M: {i: {k: float(M[u, j]) for j, k in enumerate(pk)} for u, i in enumerate(ids_u)})
    cand, seen, notes = [], {}, {"solves": 0}

    def add(cid, idx, merge):
        key = tuple(int(x) for x in idx)
        if merge and key in seen:
            return seen[key]
        seen.setdefault(key, cid)
        cand.append((cid, np.asarray(idx)))
        return cid

    def robust(mask):
        """The plan with the best mean benefit over the masked draws within the allocatable budget in each of them
        (one budget row per draw, beside the signed caps); None when no plan is proven optimal."""
        caps = list(A.feas.get("caps") or []) + [{"name": f"voi_draw_{d}", "max": B, "weights": tab(Cs[d])}
                                                 for d in np.flatnonzero(mask)]
        notes["solves"] += 1
        try:
            return A.solve(tab(Bn[mask].mean(0)), tab(Cs[mask].mean(0)), budget=B, feas=dict(A.feas, caps=caps))["choice"]
        except RuntimeError as e:
            notes.setdefault("not_solved", []).append(str(e))
            return None
    add("point", [at[plan[i]] for i in ids_u], False)
    for vid, p in sorted((variants or {}).items()):
        add(f"variant:{vid}", [at[p[i]] for i in ids_u], False)
    rb = robust(np.ones(n, bool))
    if rb is not None:
        add("robust", [at[rb[i]] for i in ids_u], False)
    opt_of = [add(f"draw:{d}", r["choice"], True) for d, r in enumerate(rows)]
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
    # conditional candidates: the plan decided within the budget on each bin of each valued input and group, built on
    # one half of the draws and valued on the other (cross-fitting: round 2 P1, a plan fitted to the input's own
    # draws made an irrelevant input look valuable, and the permutation floor could not see it)
    hv = np.zeros(n, int)
    hv[np.random.default_rng([int(seed), 7]).permutation(n)[: n // 2]] = 1
    G = max(1, min(int(th["bins"]), (n // 2) // int(th["min_bin_draws"])))
    cond = {}
    for name, mem in [(k, [k]) for k in inputs] + list(groups.items()):
        Xm = np.column_stack([inputs[m] for m in mem])
        if np.all(Xm.std(0) == 0) or G < 2:
            continue
        for h in (0, 1):
            R = np.flatnonzero(hv == h)
            b = _bins(Xm[R], G)
            for g in np.unique(b):
                if notes["solves"] >= int(th["max_conditional_solves"]):
                    notes["truncated"] = True
                    break
                m = np.zeros(n, bool)
                m[R[b == g]] = True
                c = robust(m)
                if c is not None:
                    add(f"given:{name}:h{h}:{g}", [at[c[i]] for i in ids_u], False)
                    cond.setdefault(name, []).append((len(cand) - 1, h))
    pos = {c[0]: j for j, c in enumerate(cand)}
    ar = np.arange(len(ids_u))
    nb = np.column_stack([Bn[:, ar, ix].sum(1) for _, ix in cand])
    cost = np.column_stack([Cs[:, ar, ix].sum(1) for _, ix in cand])
    obj = np.array([r["objective"] for r in rows])
    gap = float(np.max(np.abs(np.array([nb[d, pos[c]] for d, c in enumerate(opt_of)]) - obj)))
    if gap > 1e-6 * max(1.0, float(np.max(np.abs(obj)))):
        raise ValueError(f"value of information: a draw's re-optimised objective is not its benefit table summed at its "
                         f"plan (gap {gap:.3g})")
    pi = obj.copy()
    if getattr(A, "plan_budget", None) is not None and abs(float(A.plan_budget) - B) > 1e-9 * max(1.0, B):
        # the draws were re-optimised within a reserve; with perfect information costs are known: the full budget
        for d, r in enumerate(rows):
            sol = A.solve(tab(Bn[d]), tab(Cs[d]), budget=B)
            try:
                A.guard(f"uncertainty draw {d}", [sol["choice"]], r["events"])
                pi[d] = max(pi[d], float(sol["objective"]))
            except AnalysisBlocked:
                notes.setdefault("perfect_information_kept_reserve", []).append(d)
    excluded = {}
    ben0, cost0 = tab(Bn[0]), tab(Cs[0])
    for cid, ix in cand:
        p = {i: pk[ix[u]] for u, i in enumerate(ids_u)}
        bad = aopt.check(p, ben0, cost0, A.feas, 1e300)
        if bad:
            excluded[cid] = f"breaks the signed feasibility: {'; '.join(bad[:3])}"
            continue
        for d, r in enumerate(rows):
            try:
                A.guard(f"uncertainty draw {d}", [p], r["events"])
            except AnalysisBlocked as e:
                excluded[cid] = f"model corrections bear on it at {e.where}"
                break
    nb_free = None
    if s > 0:          # perfect information may overspend: each draw's best plan without the budget (round 2 P2)
        nb_free = np.full(n, -np.inf)
        for d, r in enumerate(rows):
            sol = A.solve(tab(Bn[d]), tab(Cs[d]), budget=float(Cs[d].max(1).sum()) + 1.0)
            try:
                A.guard(f"uncertainty draw {d}", [sol["choice"]], r["events"])
                nb_free[d] = float(sol["objective"])
            except AnalysisBlocked:
                notes.setdefault("unbudgeted_left_out", []).append(d)
    res = analyse(nb, pi, cost > B * (1 + 1e-9), inputs, groups, s, level, thr=th, seed=seed,
                  ids=[c[0] for c in cand], excluded=excluded, exact_id="robust" if rb is not None else None,
                  nb_free=nb_free, cond=cond, halves=hv)
    res.update(outcome=A.primary, discount_factor=float(A.dfac), budget_allocatable=B, groups=groups,
               groups_not_computed=skipped, not_drawn=not_drawn(report, A), solves=notes,
               consistency={"objective_max_abs_gap": gap},
               evppi_over=(f"the candidate set ({len(cand)} plans, including the plan decided within the budget on "
                           f"each of {G} bins of each input and group in one half of the draws, valued on the other "
                           f"half); feasibility after learning is judged within bins, so for inputs that move costs "
                           f"it is a lower bound"),
               method={"estimator": "regression EVPPI (nonparametric, per candidate plan)", "citation": CITATION,
                       "smoother": "natural cubic regression spline, additive over a group, knots by GCV (repeated in "
                                   "every permutation)",
                       "decide_now": "the best plan within the allocatable budget in every draw (one solve with a "
                                     "budget row per draw); with an overspend allowance, the best eligible candidate",
                       "perfect_information": "in each draw the best plan feasible there (its own optimum within the "
                                              "budget); with an allowance, overspending in at most as many draws as the "
                                              "plan chosen now may",
                       "infeasibility_rule": "a candidate over the allocatable budget in a draw is infeasible there; "
                                             "eligible to be chosen now only if infeasible in at most the affordability "
                                             "rule's share of draws (0 without a rule); candidates that break the signed "
                                             "feasibility or carry model corrections in any draw are reported, never "
                                             "chosen"})
    return res


# --------------------------------------------------------------------------- gate, claims, results
def contract_findings(raw: dict, unc, voi) -> list[str]:
    """The artifact the spec asks for, and its shape and arithmetic (fail closed)."""
    draws = (unc or {}).get("draws") if isinstance(unc, dict) else None
    exp = expected(raw)
    if voi is None:
        return ["the spec asks for value of information and analysis/value_of_information.yaml is missing"] if exp else []
    if not exp:
        return ["analysis/value_of_information.yaml is present but the spec switches it off"]
    if not isinstance(voi, dict):
        return ["value_of_information: the payload is not a mapping"]
    th, bad = thresholds(), []
    st = voi.get("status")
    if st not in STATUSES:
        return [f"value_of_information: unknown status {st!r}"]
    if not isinstance(draws, int) or voi.get("draws") != draws:
        return [f"value_of_information: {voi.get('draws')!r} draws, the uncertainty step has {draws!r}"]
    if not isinstance(voi.get("not_drawn"), list) or not isinstance(voi.get("inputs"), list):
        return ["value_of_information: not_drawn or inputs is not a list"]
    if draws < th["min_draws"] or st == "too_few_draws":
        return [] if (st == "too_few_draws" and draws < th["min_draws"] and voi.get("evpi") is None) else [
            f"value_of_information: status {st} at {draws} draws (minimum {th['min_draws']})"]
    want = "computed" if draws >= th["informative_draws"] else "indicative"
    cd = voi.get("candidates")
    if not (isinstance(cd, dict) and isinstance(cd.get("list"), list) and all(isinstance(x, dict) for x in cd["list"])):
        return ["value_of_information: the candidate list is missing"]
    cl = {x.get("id"): x for x in cd["list"]}
    n_el = sum(x.get("eligible") is True for x in cd["list"])
    if st == "no_eligible_plan":
        return [] if (voi.get("evpi") is None and n_el == 0 and cd.get("eligible") == 0) else [
            "value_of_information: no_eligible_plan with eligible candidates or an EVPI"]
    if st != want:
        bad.append(f"value_of_information: status {st} at {draws} draws (expected {want})")

    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    ev, v0, vp = voi.get("evpi"), voi.get("expected_value_decide_now"), voi.get("expected_value_perfect_information")
    if not (isinstance(ev, dict) and num(ev.get("value")) and num(ev.get("se_paired")) and num(v0) and num(vp)):
        return bad + ["value_of_information: EVPI, its standard error or a value is missing or not finite"]
    at = 1e-9 * max(1.0, abs(v0), abs(vp))
    if abs(ev["value"] - (vp - v0)) > at:
        bad.append(f"value_of_information: EVPI {ev['value']} is not the perfect-information value less the value of "
                   f"deciding now ({vp} - {v0})")
    if ev["value"] < -at or ev["se_paired"] < 0:
        bad.append("value_of_information: a negative EVPI or standard error")
    sh = voi.get("max_share_over_budget", 0.0)
    for x in cd["list"]:
        want = (num(x.get("draws_over_budget")) and "excluded" not in x and num(sh)
                and x["draws_over_budget"] / draws <= sh + 1e-12)
        if x.get("eligible") is not bool(want) or not num(x.get("expected_value")):
            bad.append(f"value_of_information: candidate {x.get('id')!r}: eligibility or value disagrees with its "
                       f"budget count and exclusion")
    ch = cl.get(voi.get("decide_now")) or {}
    best = max((x["expected_value"] for x in cd["list"] if x.get("eligible") is True
                and num(x.get("expected_value"))), default=None)
    if (ch.get("eligible") is not True or n_el != cd.get("eligible") or not num(ch.get("expected_value"))
            or abs(ch["expected_value"] - v0) > at or best is None or abs(best - v0) > at):
        bad.append(f"value_of_information: the plan chosen now {voi.get('decide_now')!r} is not the best eligible "
                   f"candidate at the value recorded ({v0})")
    tol = float(th["exceeds_evpi_tol"]) * max(ev["value"], 0.0) + at
    need = ("evppi", "noise_floor", "noise_mean", "df", "above_noise_floor", "exceeds_evpi", "supported")
    for r in voi["inputs"]:
        if not isinstance(r, dict) or not isinstance(r.get("input"), str):
            bad.append(f"value_of_information: input entry {r!r} malformed")
            continue
        if r.get("status") == "too_many_inputs":
            if r.get("supported") is not False:
                bad.append(f"value_of_information: {r['input']}: not computed but supported")
            continue
        miss = [k for k in need if k not in r]
        e = r.get("evppi")
        if miss or not all(num(r.get(k)) for k in ("evppi", "noise_floor", "noise_mean")):
            bad.append(f"value_of_information: {r['input']}: diagnostics missing or not finite ({miss})")
        elif e < -at:
            bad.append(f"value_of_information: {r['input']}: EVPPI {e} is negative")
        elif (r["exceeds_evpi"] is not bool(e > ev["value"] + tol)
              or r["above_noise_floor"] is not bool(e > r["noise_floor"])
              or r["supported"] is not bool(r["above_noise_floor"] and not r["exceeds_evpi"] and ev["value"] > tol)):
            bad.append(f"value_of_information: {r['input']}: its flags disagree with its numbers")
    return bad


def _supported(data):
    return [r for r in data.get("inputs") or [] if r.get("supported")][:TOP_SHOWN]


def _why(r, data) -> str:
    if r.get("status") != "computed":
        return str(r.get("status"))
    if r.get("exceeds_evpi"):
        return "above EVPI, a fit the draws do not support"
    if not r.get("above_noise_floor"):
        return "within its noise floor"
    return "EVPI is zero"


def claims(c, art, data) -> list[dict]:
    """Claim specs (toolkit/analysis/claims.py `_c`): status, EVPI and the supported inputs only."""
    if data.get("evpi") is None:
        return [c("voi.status", "label", art, "data.status")]
    o = data["outcome"]
    cl = [c("voi.status", "label", art, "data.status"),
          c("voi.decide_now", "scalar", art, "data.expected_value_decide_now", o),
          c("voi.evpi", "scalar", art, "data.evpi.value", o), c("voi.evpi_se", "scalar", art, "data.evpi.se_paired", o)]
    if data["evpi"].get("pct") is not None:
        cl.append(c("voi.evpi_pct", "percentage", art, "data.evpi.pct", "%"))
    for k, r in enumerate(_supported(data)):
        j = data["inputs"].index(r)
        cl.append(c(f"voi.top{k}.evppi", "scalar", art, f"data.inputs[{j}].evppi", o))
        if r.get("pct") is not None:
            cl.append(c(f"voi.top{k}.evppi_pct", "percentage", art, f"data.inputs[{j}].pct", "%"))
    return cl


def results_lines(data) -> list[str]:
    L = ["## Value of information", ""]
    if data.get("evpi") is None:
        why = {"no_eligible_plan": "no candidate plan stays within the allocatable budget in enough draws (see "
                                   "`candidates.ineligible`)",
               "too_few_draws": f"{data.get('draws')} uncertainty draws, below the minimum of {data.get('min_draws')}"}
        return L + [f"Not computed: {why.get(data['status'], data['status'])} (status [CLAIM:voi.status]).", ""]
    o, dfac = data["outcome"], data.get("discount_factor", 1.0)
    unit = o + ("" if dfac == 1 else f" (discounted, factor {dfac:.4g})")
    sup = _supported(data)
    L += [f"Over {data['draws']} uncertainty draws (status [CLAIM:voi.status]), the best plan chosen now "
          f"(`{data['decide_now']}`, {data.get('decide_now_method')}) is expected to avert [CLAIM:voi.decide_now] "
          f"{unit}. Knowing every drawn input before choosing would add [CLAIM:voi.evpi] {o} (paired standard error "
          f"[CLAIM:voi.evpi_se]" + ("; [CLAIM:voi.evpi_pct] of that value" if data["evpi"].get("pct") is not None else "")
          + "): the expected value of perfect information, the most better data on these inputs could add to this "
          "decision; with perfect information no plan exceeds the budget in more draws than the affordability rule "
          "allows. Value of partial perfect information by input (regression method, Strong, Oakley & Brennan 2014; "
          f"over {data.get('evppi_over') or 'the candidate set'}):", "",
          "| Input | Kind | EVPPI | Share of the value chosen now |", "|---|---|---|---|"]
    for r in data["inputs"][:TOP_SHOWN]:
        if r in sup:
            k = sup.index(r)
            L.append(f"| {r['input']} | {r['kind']} | [CLAIM:voi.top{k}.evppi] | "
                     + (f"[CLAIM:voi.top{k}.evppi_pct]" if r.get("pct") is not None else "n/a") + " |")
        else:
            L.append(f"| {r['input']} | {r['kind']} | not supported ({_why(r, data)}) | n/a |")
    nd = data.get("not_drawn") or []
    L += ["", f"{len(data['inputs'])} inputs and groups are valued in analysis/value_of_information.yaml; a value is "
          "shown only when it clears its permutation noise floor and does not exceed EVPI. "
          + data.get("interval_note", INTERVAL_NOTE)
          + (f" Below {data['informative_draws']} draws every value is indicative." if data["status"] == "indicative"
             else "")
          + " Inputs that were not drawn have no value of information here because they did not vary, which does not "
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


class _FakeA:
    """The parts of core.Analysis that run() reads: one solve on toolkit/alloc, the model-correction guard."""

    def __init__(self, pk, budget, raw=None, units=("u",)):
        from toolkit.alloc import optimize as aopt
        self.aopt, self.ids, self.pk, self.budget, self.plan_budget = aopt, list(units), {k: () for k in pk}, budget, None
        self.feas = {"feasible": {u: {k: True for k in pk} for u in units}, "caps": []}
        self.spec = type("S", (), {"raw": raw or {}})()
        self.primary, self.dfac, self.method, self.joint = "y", 1.0, "none", None

    def solve(self, ben, cost, budget=None, feas=None):
        f = feas or self.feas
        if not any(v for t in cost.values() for v in t.values()) and not f.get("links"):   # free plans: the argmax
            ch = {u: max(ben[u], key=lambda k: (ben[u][k], -list(self.pk).index(k))) for u in ben}
            return {"choice": ch, "objective": sum(ben[u][ch[u]] for u in ben), "status": "optimal"}
        r = self.aopt.solve(ben, cost, feas or self.feas, self.budget if budget is None else budget, method="ilp")
        if r["status"] != "optimal":
            raise RuntimeError(f"optimizer status {r['status']}")
        return r

    def guard(self, where, plans, events=None):
        from toolkit.analysis.core import AnalysisBlocked
        hits = [e for e in events or [] if any(p.get(e["unit"]) == e["package"] for p in plans)]
        if hits:
            raise AnalysisBlocked(where, hits)


def _fake_run(pk, ben, cost, budget, plan, sampled, events=None, thr=None, raw=None, seed=0):
    """VOI.run on draws given as per-draw {package: benefit} and {package: cost} for one unit, or {unit: {...}}."""
    multi = isinstance(next(iter(ben[0].values())), dict)
    A = _FakeA(pk, budget, raw, units=list(ben[0]) if multi else ("u",))
    col = Collector(A)
    for d, (b, c) in enumerate(zip(ben, cost)):
        bt, ct = (dict(b), dict(c)) if multi else ({"u": dict(b)}, {"u": dict(c)})
        ev = (events or {}).get(d, [])
        sol = A.solve(bt, ct)
        col.add(d, sampled[d], bt, ct, sol, ev)
    return run(A, col, plan if isinstance(plan, dict) else {"u": plan}, None, {}, seed=seed,
               thr=dict(thresholds(), min_draws=4, **(thr or {})))


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
        ok(ev["phi1"]["evppi"] > 0.8 * evpi and ev["phi1"]["above_noise_floor"] is True and ev["phi1"]["supported"]
           and ev["phi2"]["evppi"] < 0.1 * evpi and ev["phi2"]["above_noise_floor"] is False
           and ev["phi2"]["supported"] is False and r["inputs"][0]["input"] == "phi1",
           f"VI2 the input that drives the choice ranks first and clears its noise floor; the other does not "
           f"({ev['phi1']['evppi']:.4f}, {ev['phi2']['evppi']:.4f} of {evpi:.4f})")
        tol = 1e-9 + 0.02 * evpi
        ok(all(-1e-12 <= x["evppi"] <= evpi + tol for x in r["inputs"]) and evpi >= 0,
           "VI3 0 <= EVPPI <= EVPI (within tolerance) for every input")
        # VI4 (review round 1, P2 intervals): no percentile interval around a maximisation; the paired standard error,
        # the noise floor and an explicit statement of the upward bias instead
        ok(r["evpi"]["se_paired"] > 0 and "bootstrap" not in r["evpi"]
           and all("bootstrap" not in x for x in r["inputs"]) and "upward" in (r.get("interval_note") or ""),
           f"VI4 EVPI carries its paired SE; no bootstrap interval is published; the bias is stated "
           f"({sorted(r['evpi'])}, {r.get('interval_note')})")
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
        nb = np.array([[10.0, 12.0], [10.0, 8.0], [10.0, 11.0], [10.0, 9.0]])
        r = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"x": np.array([1.0, 2.0, 3.0, 4.0])},
                    thr=dict(thresholds(), min_draws=4, informative_draws=4))
        diff = nb.max(1) - nb[:, 0]          # plan 0 (mean 10) vs plan 1 (mean 10): tie -> the first
        ok(abs(r["evpi"]["value"] - 0.75) < 1e-12 and abs(r["evpi"]["se_paired"] - diff.std(ddof=1) / 2) < 1e-12
           and abs(r["evpi"]["pct"] - 7.5) < 1e-9 and abs(r["expected_value_decide_now"] - 10.0) < 1e-12
           and abs(r["expected_value_perfect_information"] - 10.75) < 1e-12,
           f"VI6 paired arithmetic: EVPI 0.75 (7.5% of 10; per-draw best 12, 10, 11, 10), SE sd(diff)/sqrt(4) "
           f"({r.get('evpi')})")
        r2 = analyse(nb, np.array([13.0, 10.0, 11.0, 10.0]), np.zeros_like(nb, bool), {"x": np.arange(4.0)},
                     thr=dict(thresholds(), min_draws=4))
        ok(abs(r2["expected_value_perfect_information"] - 11.0) < 1e-12,
           "VI7 per draw, perfect information is the better of the draw's optimum and every feasible candidate")

    def budget():
        nb = np.array([[10.0, 14.0], [10.0, 14.0], [10.0, 14.0], [10.0, 14.0]])
        over = np.array([[False, True], [False, False], [False, False], [False, False]])
        th = dict(thresholds(), min_draws=4)
        r = analyse(nb, nb[:, 0].copy(), over, {"x": np.arange(4.0)}, thr=th, ids=["point", "draw:1"])
        c = r["candidates"]
        ok(c["eligible"] == 1 and c["ineligible"] == [{"id": "draw:1", "draws_over_budget": 1, "share_over_budget": 0.25}]
           and r["decide_now"] == "point" and abs(r["evpi"]["value"] - 3.0) < 1e-12,
           f"VI8 a plan over the budget in one draw cannot be chosen now without an affordability rule; with perfect "
           f"information it is chosen in the three draws it fits (EVPI 3) ({c}, {r.get('evpi')})")
        r2 = analyse(nb, nb[:, 0].copy(), over, {"x": np.arange(4.0)}, max_share=0.25, thr=th, ids=["point", "draw:1"])
        ok(r2["decide_now"] == "draw:1" and r2["candidates"]["eligible"] == 2
           and r2["candidates"]["over_budget_counted"] == [{"id": "draw:1", "draws_over_budget": 1}]
           and abs(r2["evpi"]["value"]) < 1e-12,
           "VI9 with the spec's affordability rule (at most 25% of draws over), the plan is eligible, the draw it "
           "overspends in is counted and reported, and perfect information may overspend in as many draws (EVPI 0)")
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

    th = dict(thresholds(), min_draws=4)       # review round 1 (two Codex reviews): each reproduction is a case
    def d1():
        # D1 (P1-e): the only uncertain input is the treatment's cost (0.5 or 1.5, budget 1). Knowing it is perfect
        # information: EVPPI(cost) = EVPI = 5, since after learning the cost the plan is chosen whenever it fits.
        n = 40
        c = np.tile([0.5, 1.5], n // 2)
        nb = np.column_stack([np.zeros(n), np.full(n, 10.0)])
        r = analyse(nb, np.where(c < 1, 10.0, 0.0), np.column_stack([np.zeros(n, bool), c > 1]), {"cost:t": c},
                    thr=th, ids=["none", "treat"])
        e = {x["input"]: x for x in r["inputs"]}.get("cost:t") or {}
        ok(abs(r["evpi"]["value"] - 5.0) < 1e-9 and abs((e.get("evppi") or 0) - 5.0) < 1e-6,
           f"D1 feasibility after learning is conditional on what is learned: cost EVPPI = EVPI = 5 "
           f"({r['evpi']['value']}, {e.get('evppi')})")
    def d2():
        # D2 (P1-d): two plans each overspend in a disjoint 10% of draws (benefit 100 only there), a safe plan is
        # worth 10, at most 10% of draws may overspend. Perfect information may also overspend in at most 10% of
        # draws: worth 19, not 28; EVPI 9.
        n = 100
        A_, B_ = np.zeros(n), np.zeros(n)
        A_[:10], B_[10:20] = 100.0, 100.0
        over = np.zeros((n, 3), bool)
        over[:10, 1], over[10:20, 2] = True, True
        r = analyse(np.column_stack([np.full(n, 10.0), A_, B_]), np.full(n, 10.0), over,
                    {"x": np.arange(n, dtype=float)}, max_share=0.1, thr=th, ids=["safe", "a", "b"])
        ok(abs(r["expected_value_perfect_information"] - 19.0) < 1e-9 and abs(r["evpi"]["value"] - 9.0) < 1e-9,
           f"D2 perfect information keeps the affordability share: worth 19, EVPI 9 "
           f"({r['expected_value_perfect_information']}, {r['evpi']['value']})")
    def d3():
        # D3 (P1-a): a candidate that breaks the signed feasibility (here worth 100) is reported with its regret,
        # never chosen now, never counted under perfect information
        nb = np.column_stack([np.full(8, 10.0), np.full(8, 100.0)])
        r = analyse(nb, np.full(8, 10.0), np.zeros_like(nb, bool), {"x": np.arange(8.0)}, thr=th,
                    ids=["point", "variant:given"], excluded={"variant:given": "unit u: package b is not feasible"})
        g = {x["id"]: x for x in r["candidates"]["list"]}["variant:given"]
        ok(r["decide_now"] == "point" and abs(r["evpi"]["value"]) < 1e-12 and g["eligible"] is False
           and "not feasible" in (g.get("excluded") or "") and abs(g.get("regret", 0) + 90.0) < 1e-9,
           f"D3 an infeasible variant is reported (regret {g.get('regret')}), never chosen ({r['decide_now']})")
    def d4():
        # D4 (P2 unsupported): A dominates everywhere (EVPI 0); spline overshoot gives B a positive EVPPI. It is not
        # supported: no claim, and results.md says so instead of a number
        x = np.linspace(-1, 1, 500)
        nb = np.column_stack([np.full(500, 50.0), 50.0 - 10.0 * (x > 0)])
        r = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"x": x}, thr=th)
        e = r["inputs"][0]
        doc = dict(r, outcome="y", not_drawn=[])
        cl = {k["id"] for k in claims(lambda cid, *a, **kw: {"id": cid}, "a", doc)}
        txt = "\n".join(results_lines(doc))
        ok(e["supported"] is False and not any(k.startswith("voi.top") for k in cl) and "not supported" in txt,
           f"D4 an EVPPI above EVPI (or within its noise floor) is not supported: no claim, 'not supported' shown "
           f"({e.get('evppi')}, {sorted(cl)})")
    def d6():
        # D6 (P2 resampling): GCV selection is repeated inside every permutation
        calls, real = [], globals()["_fit"]

        def spy(X, Y=None, max_df=None, k=None, max_knots=6):
            if Y is not None and k is None:
                calls.append(1)
            return real(X, Y, max_df, k, max_knots)
        globals()["_fit"] = spy
        try:
            rng = np.random.default_rng(4)
            nb = rng.normal(size=(40, 2))
            analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"x": rng.normal(size=40)}, thr=dict(th, permutations=20))
        finally:
            globals()["_fit"] = real
        ok(len(calls) >= 21, f"D6 the smoother is re-selected by GCV for each of 20 permutations ({len(calls)} selections)")
    def d7():
        # D7 (P2 coverage): an input unrelated to the net benefits, two plans of equal mean; over 50 replicates the
        # noise floor (nominal 5%) flags it in at most 12% of them
        hits = 0
        for k in range(50):
            rng = np.random.default_rng(100 + k)
            nb = 10.0 + rng.normal(size=(64, 2))
            rr = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"z": rng.normal(size=64)}, seed=k,
                         thr=dict(th, permutations=40))
            hits += bool(rr["inputs"][0]["above_noise_floor"])
        ok(hits <= 6, f"D7 the noise floor's false-positive rate on an irrelevant input: {hits} of 50 (at most 6)")
    def d5():
        # D5 (P2 gate): arithmetic, signs, diagnostics, the chosen candidate and status consistency
        import copy
        doc = _valid_doc()
        raw, unc = {}, {"draws": 64}
        nd = doc["decide_now"]
        muts = [("perfect-information value inconsistent with EVPI",
                 lambda d: dict(d, expected_value_perfect_information=-100000.0)),
                ("a negative standard error", lambda d: dict(d, evpi=dict(d["evpi"], se_paired=-1.0))),
                ("the chosen candidate absent", lambda d: dict(d, decide_now="nobody")),
                ("the chosen candidate ineligible", lambda d: dict(d, candidates=dict(d["candidates"], list=[
                    dict(x, eligible=False) if x["id"] == nd else x for x in d["candidates"]["list"]]))),
                ("diagnostics removed", lambda d: dict(d, inputs=[{k: v for k, v in x.items() if k not in
                                                                   ("noise_floor", "noise_mean")} for x in d["inputs"]])),
                ("no eligible plan with eligible candidates", lambda d: dict(d, status="no_eligible_plan", evpi=None)),
                ("supported inconsistent", lambda d: dict(d, inputs=[dict(x, supported=not x["supported"])
                                                                     for x in d["inputs"]]))]
        for label, m in muts:
            ok(bool(contract_findings(raw, unc, m(copy.deepcopy(doc)))), f"D5 {label} is a finding (fail closed)")

    def d8():
        # D8 (P1-c): budget 1; S costs 0.8 and is worth 9 in every draw; A and B cost (0.5, 1.5) and (1.5, 0.5) and
        # are worth 12 and 11.5. Deciding now within the budget in every draw is S (one solve with a budget row per
        # draw); perfect information picks A or B: EVPI = 11.75 - 9 = 2.75.
        pk = ["none", "S", "A", "B"]
        n = 8
        ben = [{"none": 0.0, "S": 9.0, "A": 12.0, "B": 11.5} for _ in range(n)]
        cost = [{"none": 0.0, "S": 0.8, "A": 0.5 if d % 2 == 0 else 1.5, "B": 1.5 if d % 2 == 0 else 0.5}
                for d in range(n)]
        r = _fake_run(pk, ben, cost, 1.0, "A", [{"cost:a": cost[d]["A"]} for d in range(n)])
        ok(r["decide_now"] == "robust" and abs(r["evpi"]["value"] - 2.75) < 1e-9
           and r.get("decide_now_method") == "exact",
           f"D8 deciding now is solved under the affordability rule (S, EVPI 2.75): {r.get('decide_now')}, "
           f"{(r.get('evpi') or {}).get('value')}, {r.get('decide_now_method')}, {r['status']}")
    def d9():
        n = 8
        # D9 (P1-b): P is worth 10 throughout; Q 12 in clean draws, 9 where a model correction bears on Q (odd
        # draws). Q, the optimum of the clean draws and of the mean tables, is guarded in every draw and left out.
        ben = [{"P": 10.0, "Q": 12.0 if d % 2 == 0 else 9.0} for d in range(n)]
        cost = [{"P": 0.0, "Q": 0.0} for _ in range(n)]
        ev = {d: [{"unit": "u", "package": "Q", "kind": "clip"}] for d in range(1, n, 2)}
        r = _fake_run(["P", "Q"], ben, cost, 1.0, "P", [{"x": float(d)} for d in range(n)], events=ev)
        qs = [x for x in r["candidates"]["list"] if "model corrections" in str(x.get("excluded"))]
        ok(r["decide_now"] == "point" and qs and not any(x["eligible"] for x in qs),
           f"D9 a plan model corrections bear on in any draw is left out, with why ({r['decide_now']}, {qs})")
    def d10():
        # D10 (P2 conditional candidates): four equiprobable states with benefits (A, B, C) = (10, 0, 6), (0, 10, 6),
        # (10, 0, 0), (10, 0, 0); phi tells the first two from the last two. C is optimal only given the first
        # pair: EVPPI(phi) = 0.5 once plans optimal within each value of phi are candidates.
        st = [(10, 0, 6), (0, 10, 6), (10, 0, 0), (10, 0, 0)]
        ben = [{"A": float(st[d % 4][0]), "B": float(st[d % 4][1]), "C": float(st[d % 4][2])} for d in range(400)]
        cost = [{"A": 0.0, "B": 0.0, "C": 0.0} for _ in range(400)]
        r = _fake_run(["A", "B", "C"], ben, cost, 1.0, "A", [{"phi": float(d % 4 >= 2)} for d in range(400)],
                      thr={"permutations": 20})
        e = {x["input"]: x for x in r["inputs"]}.get("phi") or {}
        ok(abs((e.get("evppi") or 0) - 0.5) < 0.05 and "candidate set" in str(r.get("evppi_over")),
           f"D10 conditional candidates: EVPPI(phi) = 0.5 over the candidate set ({e.get('evppi')}, {r.get('evppi_over')})")

    def e1():
        # E1 (round 2, P1; both reviewers' null, scaled down): units whose two options are worth independent noise in
        # every draw, and an input unrelated to all of it. Plans decided within the input's bins are candidates;
        # built on one half of the draws and valued on the other (cross-fitting), the input is supported in about
        # the nominal 5% of replicates, not in nearly all of them.
        hits, ev = 0, []
        for k in range(30):
            rng = np.random.default_rng(500 + k)
            n, units = 128, [f"u{i}" for i in range(80)]
            ben = [{u: {"a": float(rng.normal()), "b": float(rng.normal())} for u in units} for _ in range(n)]
            cost = [{u: {"a": 0.0, "b": 0.0} for u in units} for _ in range(n)]
            r = _fake_run(["a", "b"], ben, cost, 1.0, {u: "a" for u in units},
                          [{"z": float(rng.normal())} for _ in range(n)], thr={"permutations": 40}, seed=k)
            e = {x["input"]: x for x in r["inputs"]}["z"]
            hits += bool(e["supported"])
            ev.append(round(e["evppi"] - e["noise_floor"], 3))
        ok(hits <= 4, f"E1 an irrelevant input with input-dependent candidates is supported in {hits} of 30 replicates "
                      f"(at most 4; EVPPI minus floor, first five: {ev[:5]})")

    def e2():
        # E2 (round 2, P2): budget 1, treatment costs 2 in every draw and is worth 10 in ten of 40 draws; 25% of draws
        # may overspend. Deciding now: nothing (0). Perfect information treats in those ten draws: EVPI 2.5, though
        # no budget-constrained solve ever proposes treatment.
        ben = [{"none": 0.0, "t": 10.0 if d < 10 else 0.0} for d in range(40)]
        cost = [{"none": 0.0, "t": 2.0} for _ in range(40)]
        r = _fake_run(["none", "t"], ben, cost, 1.0, "none", [{"x": float(d)} for d in range(40)],
                      raw={"uncertainty": {"affordability": {"max_share_over_budget": 0.25}}}, thr={"permutations": 10})
        ok(abs((r.get("evpi") or {}).get("value", -1) - 2.5) < 1e-9,
           f"E2 perfect information may overspend with the plan solved without the budget: EVPI 2.5 ({r.get('evpi')})")

    def e3():
        # E3 (round 2, P2): 800 draws, one bin, at most 20% of draws over budget. A is worth 1 - phi and overspends in
        # 20% of draws, all with phi < 0.5; B is worth phi and overspends in 20%, all with phi > 0.5. Choosing A below
        # 0.5 and B above overspends in 40% of draws: the chosen policy, not each plan, must keep the share.
        n = 800
        phi = (np.arange(n) + 0.5) / n
        over = np.zeros((n, 3), bool)
        over[: n // 5, 1], over[n - n // 5:, 2] = True, True
        nb = np.column_stack([np.zeros(n), 1 - phi, phi])
        r = analyse(nb, np.maximum(nb[:, 0], np.where(over, -np.inf, nb).max(1)), over, {"phi": phi},
                    max_share=0.2, thr=dict(thresholds(), min_draws=4, bins=1, permutations=10), ids=["r", "a", "b"])
        e = r["inputs"][0]
        ok(e.get("policy_share_over_budget") is not None and e["policy_share_over_budget"] <= 0.2 + 1e-12
           and e["evppi"] < 0.25 - 0.01,
           f"E3 the policy chosen after learning keeps the affordability share ({e.get('policy_share_over_budget')}, "
           f"EVPPI {e.get('evppi')})")

    def e4():
        # E4 (round 2, P2): treatment costs 0.5 in 25 of 500 draws and 1.5 in the rest (budget 1, benefit 10): learning
        # the cost is perfect information, so EVPPI = EVPI = 0.5; each distinct value needs its own bin.
        n = 500
        c = np.where(np.arange(n) < 25, 0.5, 1.5)
        nb = np.column_stack([np.zeros(n), np.full(n, 10.0)])
        r = analyse(nb, np.where(c < 1, 10.0, 0.0), np.column_stack([np.zeros(n, bool), c > 1]), {"cost:t": c},
                    thr=dict(thresholds(), min_draws=4, permutations=10), ids=["none", "treat"])
        e = r["inputs"][0]
        ok(abs(r["evpi"]["value"] - 0.5) < 1e-9 and abs(e["evppi"] - 0.5) < 1e-6,
           f"E4 a discrete input with no more values than bins is binned by value: EVPPI {e['evppi']} = EVPI "
           f"{r['evpi']['value']}")

    def e5():
        # E5 (round 2, P2 gate): eligibility recomputed from the counts and exclusions; the value of deciding now is
        # the chosen candidate's and the best eligible one
        import copy
        doc = _valid_doc()
        raw, unc, nd = {}, {"draws": 64}, doc["decide_now"]

        def chosen(d, **kw):
            return dict(d, candidates=dict(d["candidates"], list=[dict(x, **kw) if x["id"] == nd else x
                                                                   for x in d["candidates"]["list"]]))
        muts = [("the chosen plan over budget in every draw", lambda d: chosen(d, draws_over_budget=64)),
                ("the chosen plan excluded", lambda d: chosen(d, excluded="model corrections bear on it")),
                ("both values shifted", lambda d: dict(d, expected_value_decide_now=d["expected_value_decide_now"] + 1000,
                                                       expected_value_perfect_information=d[
                                                           "expected_value_perfect_information"] + 1000)),
                ("the baseline lowered and EVPI raised to match", lambda d: dict(
                    d, expected_value_decide_now=d["expected_value_decide_now"] - 100,
                    evpi=dict(d["evpi"], value=d["evpi"]["value"] + 100)))]
        for label, m in muts:
            ok(bool(contract_findings(raw, unc, m(copy.deepcopy(doc)))), f"E5 {label} is a finding (fail closed)")

    def gate():
        doc = _valid_doc()
        raw = {"uncertainty": {"draws": 64}}
        unc = {"draws": 64}
        ok(contract_findings(raw, unc, doc) == [], f"VG1 a well-formed artifact passes: {contract_findings(raw, unc, doc)}")
        ok(contract_findings(raw, unc, None), "VG2 the artifact is expected (enabled) and missing")
        few = {"status": "too_few_draws", "draws": 8, "min_draws": thresholds()["min_draws"], "evpi": None,
               "inputs": [], "not_drawn": []}
        ok(contract_findings({"analysis": {"value_of_information": {"enabled": False}}}, unc, None) == []
           and contract_findings(raw, {"draws": 8}, None) and contract_findings(raw, {"draws": 8}, few) == [],
           "VG3 switched off: no artifact expected; below the minimum draws the artifact records too_few_draws")
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
        ok(expected({}) is True and expected({"analysis": {"value_of_information": {"enabled": False}}}) is False,
           "VG7 on by default (at any number of draws: below the minimum it records too_few_draws); off when switched off")

    for label, fn in (("VI1-5 toy", toy), ("VI6-7 paired", paired), ("VI8-10 budget", budget),
                      ("VI11-13 floors", floors), ("D1", d1), ("D2", d2), ("D3", d3), ("D4", d4),
                      ("D5", d5), ("D6", d6), ("D7", d7),
                      ("D8", d8), ("D9", d9), ("D10", d10), ("E1", e1), ("E2", e2), ("E3", e3), ("E4", e4),
                      ("E5", e5), ("VG gate", gate)):
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
