#!/usr/bin/env python3
"""Posterior calibration of an allocation to observations of group-level states and outcomes (v2d §3.1, §3.4).

    python3 toolkit/analysis/observed_targets.py --self-test

The count-panel posterior (calibration.py, `fit_posterior_panel`) compares one per-capita outcome with observed
counts per unit, period and package. Some questions observe other things: the immune share of a band of groups (a
serosurvey), the share of a group infected over an epidemic (paired sera), or an outcome count per group (records
by age). This module calibrates against such observations. Each one is seen through a declared observation
source (toolkit/analysis/observation.py: a sampling frame, bias mechanisms with priors, a noise model). No source
is the truth.

    calibration:
      method: posterior
      parameters: [<calibrated engine parameters>]
      attribute: <unit attribute>          # optional: one latent value per unit, with its prior; range [lo, hi]
      range: [lo, hi]
      priors: {<parameter or attribute>: {dist, range | a, b, source}}
      sources: [<observation source declarations>]        # observation.check_sources
      targets:
        kind: observations
        csv: <table>                       # one row per observation
        unit: <column>                     # decision unit id
        groups: <column>                   # engine groups the observation covers, separated by `group_sep` (";")
        source: <column>                   # the declared source id
        latent: <column>                   # immunity | susceptible | attack_rate | outcome:<engine per-capita output>
        observed: <column>                 # positives (binomial, beta-binomial) or a count (poisson, negbin)
        n: <column>                        # sample size (binomial, beta-binomial)
        package: <column>                  # optional: the package in force (default: the comparator)

Latent quantities, from the engine at a parameter set, per row:
- immunity / susceptible: the size-weighted share of the groups immune / susceptible at the start (before the
  epidemic, after the package's components that act at the start). It is read from the engine's
  `initial_state()` when every target is a state (no epidemic is run), else from the run's `meta.s_start`.
- attack_rate: the size-weighted share of the groups infected in the decision window, from `per_capita`.
- outcome:<o>: the expected count of <o> in the groups, per_capita[o] x group size.

Group sizes are the engine's (initial_state `N`), or the run's `meta.age_shares` times the unit's population
(decision_units.populations). The expected observation is `Source.expected(latent, bias parameters)`, under the
source's noise model. Bias and frame-gap parameters are sampled jointly with the engine parameters. They are
`nuisance` rows of the canonical sample: recorded, never passed to the engine. Runs go through `run_batch()` in
sub-batches; a member the engine refuses has no likelihood (-inf), and it is counted.

The fit has the shape of `fit_posterior_panel`, so the point, uncertainty, stability folds and prior_range tests use
it unchanged. Nothing here names a disease.
"""
from __future__ import annotations

import csv
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KIND = "observations"
FIELDS = ("csv", "unit", "groups", "source", "latent", "observed")
STATE = ("immunity", "susceptible")
LATENT_OF = {"immunity": "immunity", "susceptible": "immunity", "attack_rate": "attack_rate"}   # source latent quantity
SUB_BATCH = 512


def applies(spec) -> bool:
    raw = spec.raw if hasattr(spec, "raw") else spec
    cal = raw.get("calibration") or {}
    return cal.get("method") == "posterior" and (cal.get("targets") or {}).get("kind") == KIND


def _sep(t) -> str:
    return str(t.get("group_sep", ";"))


def read_rows(spec) -> list[dict]:
    """One dict per observation: unit, groups, source, latent, observed, n (or None), package (or None)."""
    t = spec["calibration"]["targets"]
    with open(spec.resolve(t["csv"]), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        out.append({"unit": str(r[t["unit"]]), "groups": [g.strip() for g in str(r[t["groups"]]).split(_sep(t))
                                                          if g.strip()],
                    "source": str(r[t["source"]]), "latent": str(r[t["latent"]]).strip(),
                    "observed": float(r[t["observed"]]),
                    "n": float(r[t["n"]]) if t.get("n") and str(r.get(t["n"], "")).strip() != "" else None,
                    "package": str(r[t["package"]]) if t.get("package") else None})
    return out


def problems(spec, card, pnames) -> list[str]:
    """Spec-level checks (fail closed): fields, columns, units, groups, latent names, sources and their noise models,
    priors for everything the posterior moves."""
    from toolkit.analysis import observation as ob
    from toolkit.analysis.calibration import Prior
    cal = spec.raw["calibration"]
    t = cal.get("targets") or {}
    bad = [f"calibration.targets.{k}: required for kind {KIND}" for k in FIELDS if k not in t]
    if "parameters" not in cal or not isinstance(cal.get("parameters"), list):
        bad.append("calibration.parameters: a list (may be empty)")
    if ("attribute" in cal) != ("range" in cal):
        bad.append("calibration: give attribute and range together (or neither)")
    if bad:
        return bad
    if spec.raw["analysis"].get("evaluation") != "direct":
        bad.append("calibration.method posterior needs analysis.evaluation: direct (no emulator path yet)")
    attr = cal.get("attribute")
    if attr is not None and attr not in card.unit_attributes:
        bad.append(f"calibration.attribute: {attr} is not declared by the engine")
    for p in cal["parameters"]:
        if p not in pnames:
            bad.append(f"calibration.parameters: {p} is not an engine parameter")
        elif pnames[p].kind != "calibrated":
            bad.append(f"calibration.parameters: {p} is of kind {pnames[p].kind}, not calibrated")
    srcs = cal.get("sources")
    sb = ob.check_sources(srcs)
    bad += [f"calibration.sources: {code}: {m}" for code, m in sb]
    if sb:
        return bad
    sources = {s["id"]: ob.Source(s) for s in srcs}
    pri = cal.get("priors") or {}
    if not isinstance(pri, dict):
        return bad + ["calibration.priors: a mapping"]
    need = list(cal["parameters"]) + ([attr] if attr else [])
    for n in need:
        if n not in pri:
            bad.append(f"calibration.priors: {n} has no prior (no silent defaults)")
            continue
        try:
            Prior.from_decl(n, pri[n])
        except ValueError as e:
            bad.append(f"calibration.priors.{n}: {e}")
        if not str((pri[n] or {}).get("source") or "").strip():
            bad.append(f"calibration.priors.{n}: give its source")
    nuis = {p for s in sources.values() for p in s.params()}
    clash = sorted(nuis & (set(pnames) | {attr}))
    if clash:
        bad.append(f"calibration.sources: bias parameters {clash} share a name with an engine parameter or the attribute")
    extra = sorted(set(pri) - set(need))
    if extra:
        bad.append(f"calibration.priors: {extra} are not calibrated by this spec (source biases take their prior in the "
                   "source)")
    try:
        rows = read_rows(spec)
    except (OSError, KeyError, ValueError) as e:
        return bad + [f"calibration.targets: table unreadable or a column missing: {type(e).__name__}: {e}"]
    if not rows:
        return bad + ["calibration.targets: the table is empty"]
    ids, groups = set(spec.unit_ids()), list(card.age_groups or [])
    outs = set((card.outputs or {}).get("per_capita") or [])
    pk = spec.packages()
    for i, r in enumerate(rows):
        w = f"calibration.targets row {i + 1}"
        if r["unit"] not in ids:
            bad.append(f"{w}: unit {r['unit']!r} is not a decision unit")
        miss = [g for g in r["groups"] if g not in groups]
        if not r["groups"] or miss:
            bad.append(f"{w}: groups {miss or '(none)'} are not engine groups {groups}")
        if r["source"] not in sources:
            bad.append(f"{w}: source {r['source']!r} is not declared (observation_source_undeclared)")
            continue
        s = sources[r["source"]]
        lat = r["latent"]
        if lat.startswith("outcome:"):
            if lat[8:] not in outs:
                bad.append(f"{w}: {lat}: not an engine per-capita output")
            if s.noise.get("model") not in ("poisson", "negbin"):
                bad.append(f"{w}: an outcome count needs a count source (poisson | negbin), not {s.noise.get('model')}")
        elif lat in LATENT_OF:
            if s.latent != LATENT_OF[lat]:
                bad.append(f"{w}: latent {lat} needs a source whose latent quantity is {LATENT_OF[lat]}, not {s.latent}")
            if s.noise.get("model") not in ("binomial", "betabinomial"):
                bad.append(f"{w}: a share needs a binomial or beta-binomial source")
            if r["n"] is None or r["n"] <= 0 or not 0 <= r["observed"] <= r["n"]:
                bad.append(f"{w}: give a sample size n > 0 with 0 <= observed <= n")
        else:
            bad.append(f"{w}: latent {lat!r} not in {STATE + ('attack_rate',)} or outcome:<output>")
        if r["package"] is not None and r["package"] not in pk:
            bad.append(f"{w}: package {r['package']!r} is not an option")
    return bad


class ObservedProblem:
    """The posterior Problem of observation targets on the spec's engine (see the module docstring)."""

    def __init__(self, spec, model, rows=None, fixed_z=None, fixed_params=None):
        from toolkit.analysis import observation as ob
        from toolkit.analysis.calibration import Prior, Problem
        self.spec, self.model, self.eng = spec, model, model.eng
        cal = spec["calibration"]
        self.rows = list(rows) if rows is not None else read_rows(spec)
        self.attr = cal.get("attribute")
        self.params = list(cal["parameters"])
        self.fixed_params = {k: float(v) for k, v in (fixed_params or {}).items()}
        self.fixed_z = dict(fixed_z or {})
        self.sources = {s["id"]: ob.Source(s) for s in cal["sources"]}
        self.nuisance = list(dict.fromkeys(p for s in self.sources.values() for p in s.params()))
        units = sorted({r["unit"] for r in self.rows}, key=str)
        self.units = [u for u in units if u not in self.fixed_z] if self.attr else []
        pri_decl = cal.get("priors") or {}
        pri = {p: Prior.from_decl(p, pri_decl[p]) for p in self.params if p not in self.fixed_params}
        pri.update({f"z:{u}": Prior.from_decl(f"z:{u}", pri_decl[self.attr]) for u in self.units})
        for s in self.sources.values():
            for p, d in s.priors().items():
                if p not in self.fixed_params:
                    pri.setdefault(p, Prior.from_decl(p, d))
        self.names = ([p for p in self.params if p not in self.fixed_params] + [f"z:{u}" for u in self.units]
                      + [p for p in self.nuisance if p not in self.fixed_params])
        self.state_only = (all(r["latent"] in STATE for r in self.rows)
                           and callable(getattr(self.eng, "initial_state", None)))
        self.pk = spec.packages()
        self.comp = spec.comparator
        self.horizon = spec.horizon()
        pops = spec["decision_units"]["populations"]
        self.tot = {str(u[spec.uid]): sum(float(u[pm["column"]]) for pm in pops.values()) for u in spec.units()}
        self.groups = list(self.eng.describe().age_groups)
        self.pairs = sorted({(r["unit"], r["package"] or self.comp) for r in self.rows})
        self.failed = 0
        self.problem = Problem(self.names, pri, self.loglik, chunk=4096)

    # ---- the engine at one row of parameter values
    def _theta(self, x: dict) -> dict:
        from toolkit.spec import engine_parameters as PV
        v = dict(self.fixed_params, **x)
        return PV.params(self.spec, {p: v[p] for p in self.params})

    def _z(self, x: dict, u):
        if not self.attr:
            return None
        return x[f"z:{u}"] if f"z:{u}" in x else self.fixed_z.get(u)

    def _states(self, X: dict, n: int) -> list:
        """Per row: {(unit, package): (sizes per group, s_start per group, outcomes or None)}, or None for a row whose
        engine run failed."""
        from toolkit.engines.contract import Package
        jobs = []
        finite = np.all(np.stack([np.isfinite(np.asarray(v, float)) for v in X.values()]), axis=0) if X else \
            np.ones(n, bool)
        for i in range(n):
            if not finite[i]:
                continue
            x = {k: float(v[i]) for k, v in X.items()}
            th = self._theta(x)
            for u, k in self.pairs:
                jobs.append((i, u, k, th, self.model.unit_spec(u, self._z(x, u)),
                             Package(k, self.spec.engine_package(self.pk[k]))))
        res = [dict() for _ in range(n)]
        ok = np.array(finite, bool)
        if self.state_only:
            for i, u, k, th, us, p in jobs:
                st = self.eng.initial_state(th, us, p)
                res[i][(u, k)] = (np.asarray(st["N"], float), np.asarray(st["s_start"], float), None)
            return [r if ok[i] else None for i, r in enumerate(res)]
        outs = self._run([j[3] for j in jobs], [j[4] for j in jobs], [j[5] for j in jobs])
        for (i, u, k, th, us, p), o in zip(jobs, outs):
            if o is None:
                ok[i] = False
                continue
            sh = o.meta.get("age_shares") or {}
            N = np.asarray([float(sh.get(g, 0.0)) * self.tot[u] for g in self.groups])
            res[i][(u, k)] = (N, np.asarray(o.meta["s_start"], float), o)
        return [r if ok[i] else None for i, r in enumerate(res)]

    def _run(self, params, units, packages):
        out = []
        for a in range(0, len(params), SUB_BATCH):
            out += self._run_split(params[a:a + SUB_BATCH], units[a:a + SUB_BATCH], packages[a:a + SUB_BATCH])
        return out

    def _run_split(self, params, units, packages):
        try:
            return self.eng.run_batch(params, units, packages, self.horizon, int(self.model.seed))
        except RuntimeError:
            if len(params) == 1:
                self.failed += 1
                return [None]
            h = len(params) // 2
            return (self._run_split(params[:h], units[:h], packages[:h])
                    + self._run_split(params[h:], units[h:], packages[h:]))

    def latent(self, r, st) -> float:
        N, s, o = st[(r["unit"], r["package"] or self.comp)]
        idx = [self.groups.index(g) for g in r["groups"]]
        w = N[idx]
        lat = r["latent"]
        if lat == "immunity":
            return float(np.dot(w, 1.0 - s[idx]) / w.sum())
        if lat == "susceptible":
            return float(np.dot(w, s[idx]) / w.sum())
        if lat == "attack_rate":
            return float(sum(o.per_capita["infections"][g] * N[j] for g, j in zip(r["groups"], idx)) / w.sum())
        oc = lat[8:]
        return float(sum(o.per_capita[oc][g] * N[j] for g, j in zip(r["groups"], idx)))

    def row_ll(self, x: dict, st) -> tuple[float, list]:
        from toolkit.analysis import observation as ob
        tot, exp = 0.0, []
        by_src = {}
        for j, r in enumerate(self.rows):
            by_src.setdefault(r["source"], []).append(j)
        vals = dict(self.fixed_params, **x)
        exp = [None] * len(self.rows)
        for sid, js in by_src.items():
            s = self.sources[sid]
            lat = np.asarray([self.latent(self.rows[j], st) for j in js], float)
            e = np.asarray(s.expected(lat, {p: vals[p] for p in s.params()}), float)
            y = np.asarray([self.rows[j]["observed"] for j in js], float)
            m = s.noise["model"]
            if m in ("poisson", "negbin"):
                kw = {"dispersion": float(s.noise["dispersion"])} if m == "negbin" else {}
                ll = ob.loglik(m, y, mu=np.maximum(e, 0.0), **kw)
            else:
                n = np.asarray([self.rows[j]["n"] for j in js], float)
                p = np.clip(e, 0.0, 1.0)
                ll = (ob.loglik(m, y, n=n, p=p) if m == "binomial" else
                      ob.loglik(m, y, n=n, p=p, design_effect=float(s.design_effect)))
            tot += float(np.sum(ll))
            for j, v in zip(js, e):
                exp[j] = float(v)
        return tot, exp

    def loglik(self, X):
        n = len(next(iter(X.values()))) if X else 1
        sts = self._states(X, n)
        ll = np.full(n, -np.inf)
        for i, st in enumerate(sts):
            if st is None:
                continue
            ll[i] = self.row_ll({k: float(v[i]) for k, v in X.items()}, st)[0]
        return ll, {}

    def expected_at(self, x: dict) -> list:
        X = {k: np.asarray([v]) for k, v in x.items() if k in self.names}
        st = self._states(X, 1)[0]
        return self.row_ll({k: float(v) for k, v in x.items() if k in self.names}, st)[1] if st else []


SCAN_LOG2 = 6


def _scan_start(P):
    """A start for the mode without a sample: the best of 2^6 scrambled Sobol points over the prior (one batch), so
    the optimiser does not start where the engine fails closed (a near-critical epidemic at the prior's centre)."""
    from scipy.stats import norm, qmc
    U = qmc.Sobol(len(P.names), scramble=True, seed=11).random_base2(SCAN_LOG2)
    X = P.x_of_z(norm.ppf(np.clip(U, 1e-9, 1 - 1e-9)))
    ll, _ = P.evaluate(X)
    if not np.any(np.isfinite(ll)):
        return None
    return X[int(np.argmax(ll))]


def fit(spec, model, rows=None, fixed_z=None, fixed_params=None, seed=None, sample_it=True) -> dict:
    """`posterior` calibration to observation targets, in the shape of calibration.fit_posterior_panel (point at the
    posterior mode; with sample_it, the canonical joint sample, identifiability classes, prior_range tests and the
    label's prior-dominated names). `nuisance` names the source parameters, which are never engine inputs."""
    from toolkit.analysis import calibration as C
    E = ObservedProblem(spec, model, rows, fixed_z, fixed_params)
    P = E.problem
    s = c = None
    if sample_it:
        sd = spec["calibration"].get("seed", 0) if seed is None else seed
        s = C.sample(P, seed=int(sd))
        if E.failed:
            raise C.CalibrationRefused("posterior_engine_failure", f"the engine failed closed on {E.failed} parameter "
                                       "sets inside the prior's support; the posterior would be conditioned on them")
        c = C.classify(P, s)
    start = s["best"] if s else _scan_start(P)
    xh, conv, msg = C._map_point(P, start)
    x = dict(E.fixed_params, **dict(zip(P.names, (float(v) for v in xh))))
    z = {u: x[f"z:{u}"] for u in E.units}
    fixed = dict(E.fixed_z)
    if not E.attr:                       # no per-unit attribute: every unit runs at its bound attributes
        fixed = {i: None for i in spec.unit_ids()}
    exp = E.expected_at(x)
    out = {"method": "posterior", "targets_kind": KIND, "x": x, "names": list(P.names), "params": E.params,
           "units_fitted": E.units, "fixed_z": fixed, "fixed_params": dict(E.fixed_params),
           "nuisance": [p for p in E.nuisance if p not in E.fixed_params],
           "theta": {p: x[p] for p in E.params}, "z": z,
           "diagnostics": {"converged": conv, "message": msg, "n_targets": len(E.rows), "n_parameters": len(P.names),
                           "at_bound": [], "observation": "per-source (calibration.sources)",
                           "evaluation": "initial_state" if E.state_only else "run_batch",
                           "fit": [{"unit": r["unit"], "groups": r["groups"], "source": r["source"],
                                    "latent": r["latent"], "observed": r["observed"], "n": r["n"],
                                    "expected": (exp[j] if exp else None)} for j, r in enumerate(E.rows)]}}
    if s is not None:
        out["sample"] = {"sample_id": s["sample_id"], "names": s["names"], "rows": s["rows"].tolist(),
                         "method": s["method"], "seed": s["seed"]}
        out["diagnostics"].update(sampler=s["method"], **{k: v for k, v in s["diagnostics"].items()})
        lab = C.label_parts(c, P)
        out["identifiability"] = {"method": c["method"], "classes": c["classes"],
                                  "likelihood_classes": c["likelihood_classes"], "contraction": c["contraction"],
                                  "ridges": c["ridges"],
                                  "status": ("well_identified" if all(v == "identified" for v in c["classes"].values())
                                             else "partly_or_prior_dominated")}
        out["prior_range_tests"] = C.prior_range_tests(c, P)
        out["prior_dominated"] = list(lab["prior_dominated"])
        out["priors"] = {n: P.priors[n].describe() for n in P.names}
    return out


# --------------------------------------------------------------------------- self-test
def _toy(d, attr=True):
    """A two-unit spec on the compartmental engine: birth cohorts with cohort_history (coverage_scale per unit) when
    `attr`, else one unit of age groups with per-group s0 and an outcome ratio."""
    import yaml
    from toolkit.spec import schema
    if attr:
        with open(os.path.join(d, "units.csv"), "w") as f:
            f.write("id,pop,N,cov1\n")
            f.write("u1,1000000,600000;200000;200000,0;0.8;0.7\n")
            f.write("u2,500000,300000;100000;100000,0;0.9;0.6\n")
        with open(os.path.join(d, "targets.csv"), "w") as f:
            f.write("unit,groups,source,latent,pos,n\n")
            f.write("u1,c1;c2,sero,immunity,124,200\n")
            f.write("u2,c1;c2,sero,immunity,139,200\n")
        eng = {"ref": "toolkit.engines.compartmental.engine:CompartmentalEngine",
               "config": {"groups": ["old", "c1", "c2"], "structure": "birth_cohorts", "initialiser": "cohort_history",
                          "doses": ["d1"], "generation_interval": [0.5, 0.5], "time_unit": "year",
                          "components": {"cu": {"kind": "campaign", "targets": ["c1", "c2"], "reach": 0.8}}}}
        attrs = {"N": {"column": "N", "split": ";"}, "coverage:d1": {"column": "cov1", "split": ";"},
                 "birth_year": {"value": [2000, 2020, 2021], "source": "test"},
                 "decision_year": {"value": 2026, "source": "test"},
                 "always_immune": {"value": ["old"], "source": "test"},
                 "contact_rate": {"value": 10.0, "source": "test"}, "seed": {"value": 1.0e-9, "source": "test"},
                 "intro": {"value": "c2", "source": "test"}, "reach:cu": {"value": 0.8, "source": "test"}}
        cal = {"method": "posterior", "parameters": [], "attribute": "coverage_scale", "range": [0.5, 1.3], "scale": "linear",
               "priors": {"coverage_scale": {"dist": "uniform", "range": [0.5, 1.3], "source": "test"}},
               "sources": [{"id": "sero", "measures": "seropositivity", "latent": {"quantity": "immunity"},
                            "frame": {"design": "cluster_survey"}, "bias": {"none": "test: a perfect assay"},
                            "noise": {"model": "binomial"}}],
               "targets": {"kind": KIND, "csv": "targets.csv", "unit": "unit", "groups": "groups",
                           "source": "source", "latent": "latent", "observed": "pos", "n": "n"}}
        groups = ["old", "c1", "c2"]
        comps = {"cu": {}}
        engine_parameters = {"seroconversion:d1": {"value": 0.9, "source": "test", "reason": "test",
                                                   "basis": "literature"},
                             "efficacy:cu": {"value": 0.9, "source": "test", "reason": "test", "basis": "literature"}}
    else:
        with open(os.path.join(d, "units.csv"), "w") as f:
            f.write("id,pop,N\nall,1000000,300000;400000;300000\n")
        with open(os.path.join(d, "targets.csv"), "w") as f:
            f.write("unit,groups,source,latent,pos,n\n")
            f.write("all,a,pre,immunity,80,400\nall,b,pre,immunity,120,400\nall,c,pre,immunity,200,400\n")
            f.write("all,a,pair,attack_rate,150,400\nall,b,pair,attack_rate,110,400\nall,c,pair,attack_rate,60,400\n")
            f.write("all,a,rec,outcome:hosp,300,\nall,b,rec,outcome:hosp,120,\nall,c,rec,outcome:hosp,240,\n")
        eng = {"ref": "toolkit.engines.compartmental.engine:CompartmentalEngine",
               "config": {"groups": ["a", "b", "c"], "structure": "age_groups_contact_matrix", "initialiser": "given",
                          "generation_interval": {"seir": {"latent_days": 1.5, "infectious_days": 2.5}},
                          "time_unit": "year", "per_group_parameters": ["s0", "ratio:hosp"], "outcomes": ["hosp"],
                          "tol": 1.0e-9,
                          "components": {"da": {"kind": "doses", "group": "a", "block_size": 50000, "max_blocks": 2}}}}
        attrs = {"N": {"column": "N", "split": ";"},
                 "contact": {"value": [[6.0, 2.0, 1.0], [2.0, 8.0, 2.0], [1.0, 2.0, 3.0]], "source": "test"},
                 "seed": {"value": 1.0e-6, "source": "test"}, "intro": {"value": "b", "source": "test"}}
        groups = ["a", "b", "c"]
        sources = [{"id": "pre", "measures": "seropositivity", "latent": {"quantity": "immunity"},
                    "frame": {"design": "cluster_survey"}, "bias": {"none": "test"}, "noise": {"model": "binomial"}},
                   {"id": "pair", "measures": "seropositivity", "latent": {"quantity": "attack_rate"},
                    "frame": {"design": "cluster_survey"}, "bias": {"none": "test"}, "noise": {"model": "binomial"}},
                   {"id": "rec", "measures": "case_notifications", "latent": {"quantity": "incidence"},
                    "frame": {"design": "facility_based"}, "bias": {"none": "test"}, "noise": {"model": "poisson"}}]
        cal = {"method": "posterior", "parameters": ["r0"] + [f"s0:{g}" for g in groups]
               + [f"ratio:hosp:{g}" for g in groups],
               "priors": dict({"r0": {"dist": "uniform", "range": [1.4, 3.0], "source": "test"}},
                              **{f"s0:{g}": {"dist": "uniform", "range": [0.0, 1.0], "source": "test"} for g in groups},
                              **{f"ratio:hosp:{g}": {"dist": "loguniform", "range": [1.0e-5, 0.1], "source": "test"}
                                 for g in groups}),
               "sources": sources,
               "targets": {"kind": KIND, "csv": "targets.csv", "unit": "unit", "groups": "groups",
                           "source": "source", "latent": "latent", "observed": "pos", "n": "n"}}
        comps = {"da1": {"group": "da"}, "da2": {"group": "da"}}
        engine_parameters = {"efficacy:da": {"value": 0.6, "source": "test", "reason": "test", "basis": "literature"}}
    spec = {"schema_version": 1, "question": "test", "decision_maker": "test", "decision_date": 2026, "engine": eng,
            "engine_parameters": engine_parameters,
            "horizon": {"start": 2026.0, "stop": 2040.0},
            "decision_units": {"csv": "units.csv", "id": "id",
                               "populations": {"all": {"column": "pop", "age_groups": groups}}, "engine_attributes": attrs},
            "options": {"populations": {"total": "pop"}, "components": comps, "packages": {"generate": True},
                        "engine_map": ({"cu": {"cu": 1}} if attr else {"da1": {"da": 1}, "da2": {"da": 2}}),
                        "prices": {"base_year": "2026", "index": {"2026": 1.0}},
                        "cost_items": [{"id": f"c_{c}", "component": c, "value": 1.0, "price_year": "2026",
                                        "price_rule": "as_published", "basis": "fixed", "includes": [c]}
                                       for c in comps]},
            "objective": {"outcome": "infections" if attr else "hosp", "horizon": [2026.0, 2040.0], "discounting": "none"},
            "constraints": {"envelope_usd": 2.0}, "comparator": {"package": "none", "text": "none"},
            "outcomes_reported": ["infections" if attr else "hosp"], "calibration": cal,
            "uncertainty": {"draws": 4, "seed": 1, "sources": [{"kind": "calibration"}],
                            "excluded": [{"kind": "engine_parameters", "reason": "test"}, {"kind": "costs", "reason": "t"},
                                         {"kind": "emulator", "reason": "test"}]},
            "analysis": {"evaluation": "direct"},
            "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation_optimized.csv",
                            "columns": {"unit": "unit", "package": "package", "value": "averted"},
                            "headline": {"name": "total_averted", "outcome": "infections" if attr else "hosp"}}}
    p = os.path.join(d, "decision_spec.yaml")
    yaml.safe_dump(spec, open(p, "w"), sort_keys=False)
    return schema.load(p)


def _self_test() -> int:
    import copy
    import tempfile
    from toolkit.analysis.model import DirectModel
    from toolkit.spec import schema
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    with tempfile.TemporaryDirectory() as d:
        sp = _toy(d, attr=True)
        errs = schema.validate(sp)
        ok(errs == [], f"OT1 an observations-calibrated spec with a per-unit attribute validates: {errs[:3]}")
        m = DirectModel(sp)
        f = fit(sp, m, sample_it=False)
        imm = {r["unit"]: r for r in f["diagnostics"]["fit"]}
        ok(set(f["z"]) == {"u1", "u2"} and all(abs(imm[u]["expected"] - imm[u]["observed"] / 200) < 0.02
                                                for u in ("u1", "u2")),
           f"OT2 the mode reproduces each unit's survey immunity: {[(u, round(imm[u]['expected'], 4)) for u in imm]}")
        # by hand: immunity of c1+c2 = mean of scale x cov x 0.9 -> scale = (obs / 0.9) / mean cov
        for u, cov, obs in (("u1", (0.8, 0.7), 124 / 200), ("u2", (0.9, 0.6), 139 / 200)):
            want = obs / 0.9 / (sum(cov) / 2)
            ok(abs(f["z"][u] - want) < 0.02, f"OT3 unit {u}: coverage_scale {f['z'][u]:.4f} ~ the closed form {want:.4f}")
        E = ObservedProblem(sp, m)
        ok(E.state_only and E.names == ["z:u1", "z:u2"], "OT4 state-only targets are read from initial_state(); the "
           "sampled names are the per-unit attributes")
        r2 = copy.deepcopy(sp.raw)
        r2["calibration"]["sources"][0]["bias"] = [{"mechanism": "assay_sensitivity", "param": "se",
                                                    "prior": {"dist": "uniform", "range": [0.9, 1.0]}, "source": "t"}]
        s2 = schema.Spec(r2, d, os.path.join(d, "decision_spec.yaml"))
        E2 = ObservedProblem(s2, DirectModel(s2))
        ok(E2.names == ["z:u1", "z:u2", "se"] and E2.nuisance == ["se"], "OT5 a source's bias parameter is sampled "
           "jointly, as a nuisance name")
        for mut, needle in ((lambda r: r["calibration"]["targets"].pop("groups"), "groups: required"),
                            (lambda r: r["calibration"]["priors"].pop("coverage_scale"), "has no prior"),
                            (lambda r: r["calibration"]["sources"][0].update(noise={"model": "poisson"}), "noise model"),
                            (lambda r: r["calibration"].pop("range"), "attribute and range together")):
            r3 = copy.deepcopy(sp.raw)
            mut(r3)
            e3 = schema.validate(schema.Spec(r3, d, os.path.join(d, "decision_spec.yaml")))
            ok(any(needle in e for e in e3), f"OT6 refused ({needle}): {e3[:2]}")
        # OT12: the whole analysis (point, uncertainty, decision tests, deliverable) on the toy with a source bias
        import yaml
        from toolkit.analysis import run as RUN
        r5 = copy.deepcopy(sp.raw)
        r5["calibration"]["sources"][0]["bias"] = [{"mechanism": "assay_sensitivity", "param": "se",
                                                    "prior": {"dist": "uniform", "range": [0.9, 1.0]}, "source": "t"}]
        r5["calibration"]["parameters"] = ["r0"]
        r5["calibration"]["priors"]["r0"] = {"dist": "uniform", "range": [8.0, 12.0], "source": "t"}
        yaml.safe_dump(r5, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
        res = RUN.run(d, unsigned=True, log=lambda *a: None)
        cal = res["calibration"]
        prov = res["uncertainty"].get("draw_provenance") or {}
        ok(set(cal["names"]) == {"r0", "z:u1", "z:u2", "se"} and not prov.get("findings")
           and all("se" in r["values"] for r in prov.get("applied") or [{}]),
           f"OT12 the full analysis runs on observation targets; each draw records the bias parameter with its row "
           f"({cal['names']}, {len(prov.get('applied') or [])} draws)")
        alloc = open(os.path.join(d, "allocation_optimized.csv")).read().splitlines()
        ok(len(alloc) == 3 and os.path.exists(os.path.join(d, "uncertainty_report.yaml"))
           and "total_averted" in open(os.path.join(d, "uncertainty_report.yaml")).read(),
           "OT13 the deliverable and the uncertainty report (with the headline output) are written")
        pr = [t["test_id"] for t in res["decision_tests"]]
        ok("prior_range_r0" in pr, f"OT14 the prior-dominated calibrated parameter gets its prior_range test ({pr})")
        yaml.safe_dump(sp.raw, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
        with open(os.path.join(d, "bad.csv"), "w") as fh:
            fh.write("unit,groups,source,latent,pos,n\nu9,c1,sero,immunity,1,2\nu1,zz,sero,immunity,1,2\n"
                     "u1,c1,nope,immunity,1,2\nu1,c1,sero,weird,1,2\nu1,c1,sero,immunity,5,2\n")
        r4 = copy.deepcopy(sp.raw)
        r4["calibration"]["targets"]["csv"] = "bad.csv"
        e4 = schema.validate(schema.Spec(r4, d, os.path.join(d, "decision_spec.yaml")))
        ok(all(any(k in e for e in e4) for k in ("not a decision unit", "not engine groups", "not declared",
                                                 "latent 'weird'", "0 <= observed <= n")),
           f"OT7 each bad target row is refused with its reason: {e4[:5]}")
    with tempfile.TemporaryDirectory() as d:
        sp = _toy(d, attr=False)
        errs = schema.validate(sp)
        ok(errs == [], f"OT8 an observations spec with no attribute (per-group engine parameters) validates: {errs[:3]}")
        m = DirectModel(sp)
        f = fit(sp, m, sample_it=False)
        fitrows = f["diagnostics"]["fit"]
        imm = [r for r in fitrows if r["latent"] == "immunity"]
        ok(all(abs(r["expected"] - r["observed"] / 400) < 0.05 for r in imm) and f["z"] == {}
           and f["fixed_z"] == {"all": None},
           f"OT9 the mode reproduces each group's immunity; no per-unit attribute "
           f"({[round(r['expected'], 3) for r in imm]})")
        hos = [r for r in fitrows if r["latent"] == "outcome:hosp"]
        ok(all(abs(r["expected"] - r["observed"]) / r["observed"] < 0.05 for r in hos),
           f"OT10 the per-group outcome ratios reproduce the outcome counts ({[round(r['expected']) for r in hos]})")
        E = ObservedProblem(sp, m)
        ok(not E.state_only and len(E.names) == 7, "OT11 attack-rate and outcome targets run the epidemic (run_batch)")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
