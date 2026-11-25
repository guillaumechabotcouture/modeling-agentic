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
    if attr is not None and cal.get("scale") not in ("log", "linear"):
        # measles run 3: an attribute declared on its natural scale ([0.6, 1.2]) was read on the default log scale
        # (exp(z): 1.8-3.3), every coverage clipped at 1 and the survey likelihood went flat. The scale is declared.
        bad.append("calibration.scale: declare log or linear for the attribute: the range and the prior are on that "
                   "scale (log: the attribute is exp(z))")
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
    for s in srcs:
        # Codex r1 P1-2, P1-12: frame gaps (the unit value from the frame population) and antibody waning (years since
        # vaccination per cohort) are not applied on this path yet: refused rather than sampled with no effect
        if (s.get("frame") or {}).get("excluded"):
            bad.append(f"calibration.sources.{s['id']}: frame.excluded is not supported for observation targets yet "
                       "(frame_gap_unmodelled): restrict the targets to the frame population, or declare none")
        for b in s.get("bias") if isinstance(s.get("bias"), list) else []:
            if isinstance(b, dict) and b.get("mechanism") == "antibody_waning":
                bad.append(f"calibration.sources.{s['id']}: antibody_waning is not supported for observation targets "
                           "yet (it needs years since vaccination per cohort)")
    seen = {}
    for s in sources.values():          # Codex r1 P2-16: one declaration per shared bias parameter
        for p, d in s.priors().items():
            if p in seen and seen[p] != d:
                bad.append(f"calibration.sources: bias parameter {p} is declared with two different priors")
            seen.setdefault(p, d)
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
    if not bad and rows and all(r["latent"] in STATE for r in rows):
        bad += _state_targets_informed(spec, rows, attr, cal["parameters"], pri, pk, groups, nuis)
    return bad


def _extract_s_start(o, groups: list[str]) -> np.ndarray | None:
    """Per-group susceptible share from `o.meta['s_start']` (compartmental engines), or from
    `o.per_capita['susceptible']` / `1 - o.per_capita['immunity']` for standard Engine contracts
    (including agent-built engines) that do not expose `initial_state()`."""
    meta = getattr(o, "meta", None)
    if isinstance(meta, dict) and "s_start" in meta:
        return np.asarray(meta["s_start"], float)
    pc = getattr(o, "per_capita", None) or {}
    if isinstance(pc.get("susceptible"), dict) and all(g in pc["susceptible"] for g in groups):
        return np.asarray([float(pc["susceptible"][g]) for g in groups], float)
    if isinstance(pc.get("immunity"), dict) and all(g in pc["immunity"] for g in groups):
        return np.asarray([1.0 - float(pc["immunity"][g]) for g in groups], float)
    return None


def _state_targets_informed(spec, rows, attr, params, pri_decl, pk, groups, nuis=()) -> list[str]:
    """When every target observes pre-epidemic state (immunity or susceptible), at least one calibrated engine
    parameter or per-unit attribute (or observation bias parameter when no engine parameter/attribute is calibrated)
    must move the population-weighted latent share of at least one observed row. Otherwise the observation
    likelihood is flat and informs nothing (for example, binding raw survey ratios as fixed point values while
    calibrating only transmission parameters)."""
    if not attr and not params:
        if nuis:
            return []
        return ["calibration.targets: every observation measures pre-epidemic state (immunity/susceptible), but "
                "neither a per-unit attribute, engine parameter, nor observation bias parameter is calibrated "
                "(the observations inform nothing)"]
    from toolkit.analysis.calibration import Prior
    from toolkit.analysis.model import calibrated_value, group_sizes
    from toolkit.engines.built.requested import RequestedEngine
    from toolkit.engines.contract import Package, UnitSpec
    from toolkit.spec import engine_parameters as PV
    eng = spec.engine()
    if isinstance(eng, RequestedEngine):
        pc_outs = set((eng.describe().outputs or {}).get("per_capita") or [])
        if "immunity" not in pc_outs and "susceptible" not in pc_outs:
            return ["calibration.targets: agent-built engines expose per-group state shares through "
                    "`outputs.per_capita` (`immunity` or `susceptible`), not `initial_state()`; declare "
                    "`immunity` or `susceptible` in `engine.build.request.outputs.per_capita`"]
        return []
    has_init = callable(getattr(eng, "initial_state", None))
    horizon = None if has_init else spec.horizon()
    seed = int(((spec.raw.get("analysis") or {}).get("direct") or {}).get("seed", 1))
    pops = spec["decision_units"]["populations"]
    pri_p = {p: Prior.from_decl(p, pri_decl[p]) for p in params}
    pri_a = Prior.from_decl(attr, pri_decl[attr]) if attr else None
    u_rows = {str(u[spec.uid]): u for u in spec.units()}
    comp = spec.comparator
    by_pair: dict[tuple, list[dict]] = {}
    for r in rows:
        by_pair.setdefault((r["unit"], r["package"] or comp), []).append(r)
    mid_vals = {p: float(pri_p[p].ppf(0.5)) for p in params}
    th_mid = PV.params(spec, mid_vals)
    z_mid = float(pri_a.ppf(0.5)) if pri_a is not None else None
    probe_qs = (1e-6, 0.01, 0.1, 0.9, 0.99, 1.0 - 1e-6)
    errs: list[str] = []
    sensitive = False

    def row_shares(pair_rows, N, s):
        out = []
        for r in pair_rows:
            idx = [groups.index(g) for g in r["groups"]]
            w = N[idx]
            wsum = float(w.sum())
            if wsum <= 0.0:
                if len(idx) == 1:
                    v = float(s[idx[0]])
                    out.append(1.0 - v if r["latent"] == "immunity" else v)
                    continue
                raise ValueError(f"observed groups {r['groups']} have zero population weight")
            val = float(np.dot(w, 1.0 - s[idx]) / wsum) if r["latent"] == "immunity" else float(np.dot(w, s[idx]) / wsum)
            out.append(val)
        return np.asarray(out, float)

    for (uid, pkg_name), pair_rows in sorted(by_pair.items()):
        epkg = Package(pkg_name, spec.engine_package(pk[pkg_name]))
        base_attrs = spec.unit_attributes(u_rows[uid])

        def eval_shares(th, z):
            a = dict(base_attrs)
            if attr and z is not None:
                a[attr] = calibrated_value(spec, z)
            us = UnitSpec(uid, a)
            if has_init:
                st = eng.initial_state(th, us, epkg)
                Ne = np.asarray(st["N"], float)
                sh = {g: float(n) / float(Ne.sum()) for g, n in zip(groups, Ne)}
                gsz = group_sizes({"meta": {"age_shares": sh}}, pops, u_rows[uid])
                s = np.asarray(st["s_start"], float)
            else:
                o = eng.run(th, us, epkg, horizon, seed)
                s = _extract_s_start(o, groups)
                if s is None:
                    raise ValueError("engine output lacks meta['s_start'] and per_capita['immunity'|'susceptible'] "
                                     "for pre-epidemic state target")
                gsz = group_sizes({"meta": o.meta}, pops, u_rows[uid])
            N = np.asarray([float(gsz.get(g, 0.0)) for g in groups])
            return row_shares(pair_rows, N, s)

        try:
            s0 = eval_shares(th_mid, z_mid)
            if not sensitive and pri_a is not None:
                for q in probe_qs:
                    if float(np.max(np.abs(eval_shares(th_mid, float(pri_a.ppf(q))) - s0))) > 1e-12:
                        sensitive = True
                        break
            if not sensitive:
                for p in params:
                    for q in probe_qs:
                        th_q = PV.params(spec, {**mid_vals, p: float(pri_p[p].ppf(q))})
                        if float(np.max(np.abs(eval_shares(th_q, z_mid) - s0))) > 1e-12:
                            sensitive = True
                            break
                    if sensitive:
                        break
        except (ValueError, KeyError, TypeError, RuntimeError) as e:
            errs.append(f"calibration.targets: initial_state failed inside the declared prior for unit {uid} ({e})")
    if errs:
        return errs
    if sensitive:
        return []
    return [f"calibration.targets: every observation measures pre-epidemic state (immunity/susceptible), which does "
            f"not depend on any calibrated parameter ({params}) or unit attribute ({attr!r}) on the observed groups "
            "(the likelihood is flat and the observations inform nothing). Calibrate a per-unit latent attribute "
            "(for example coverage_scale with initialiser: cohort_history on an administrative cohort series under "
            "structure: birth_cohorts) or per-group initial-state parameters against the survey observations rather "
            "than binding noisy survey ratios as fixed point values"]


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
                if p in self.fixed_params:
                    continue
                if p in pri and pri[p].describe() != Prior.from_decl(p, d).describe():
                    raise ValueError(f"{p}: declared with two different priors")
                pri.setdefault(p, Prior.from_decl(p, d))
        self.names = ([p for p in self.params if p not in self.fixed_params] + [f"z:{u}" for u in self.units]
                      + [p for p in self.nuisance if p not in self.fixed_params])
        self.state_only = (all(r["latent"] in STATE for r in self.rows)
                           and callable(getattr(self.eng, "initial_state", None)))
        self.pk = spec.packages()
        self.comp = spec.comparator
        self.horizon = spec.horizon()
        self.pops = spec["decision_units"]["populations"]
        self.unit_rows = {str(u[spec.uid]): u for u in spec.units()}
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
                # Codex r2 P1-1: one population mapping on every path (the aggregate's group_sizes), the engine's own
                # group sizes giving only the shares within each population band
                from toolkit.analysis.model import group_sizes
                Ne = np.asarray(st["N"], float)
                sh = {g: float(n) / float(Ne.sum()) for g, n in zip(self.groups, Ne)}
                gsz = group_sizes({"meta": {"age_shares": sh}}, self.pops, self.unit_rows[u])
                res[i][(u, k)] = (np.asarray([float(gsz.get(g, 0.0)) for g in self.groups]),
                                  np.asarray(st["s_start"], float), None)
            return [r if ok[i] else None for i, r in enumerate(res)]
        outs = self._run([j[3] for j in jobs], [j[4] for j in jobs], [j[5] for j in jobs])
        for (i, u, k, th, us, p), o in zip(jobs, outs):
            if o is None:
                ok[i] = False
                continue
            from toolkit.analysis.model import group_sizes
            gsz = group_sizes({"meta": o.meta}, self.pops, self.unit_rows[u])   # as the aggregate outcomes weight
            N = np.asarray([float(gsz.get(g, 0.0)) for g in self.groups])
            s_arr = _extract_s_start(o, self.groups)
            if s_arr is None and any(r["latent"] in STATE for r in self.rows):
                ok[i] = False
                continue
            res[i][(u, k)] = (N, s_arr, o)
        return [r if ok[i] else None for i, r in enumerate(res)]

    def _run(self, params, units, packages):
        out = []
        for a in range(0, len(params), SUB_BATCH):
            out += self._run_split(params[a:a + SUB_BATCH], units[a:a + SUB_BATCH], packages[a:a + SUB_BATCH])
        return out

    def _run_split(self, params, units, packages):
        if callable(getattr(self.eng, "run_batch", None)):
            try:
                return self.eng.run_batch(params, units, packages, self.horizon, int(self.model.seed))
            except RuntimeError:
                if len(params) == 1:
                    self.failed += 1
                    return [None]
                h = len(params) // 2
                return (self._run_split(params[:h], units[:h], packages[:h])
                        + self._run_split(params[h:], units[h:], packages[h:]))
        out = []
        seed = int(self.model.seed)
        for th, us, pk in zip(params, units, packages):
            try:
                out.append(self.eng.run(th, us, pk, self.horizon, seed))
            except RuntimeError:
                self.failed += 1
                out.append(None)
        return out

    def latent(self, r, st) -> float:
        N, s, o = st[(r["unit"], r["package"] or self.comp)]
        idx = [self.groups.index(g) for g in r["groups"]]
        w = N[idx]
        wsum = float(w.sum())
        lat = r["latent"]
        if lat in ("immunity", "susceptible", "attack_rate"):
            if wsum <= 0.0:
                if len(idx) == 1:
                    if lat == "immunity":
                        return float(1.0 - s[idx[0]])
                    if lat == "susceptible":
                        return float(s[idx[0]])
                    return float(o.per_capita["infections"][r["groups"][0]])
                raise ValueError(f"observed groups {r['groups']} have zero population weight")
            if lat == "immunity":
                return float(np.dot(w, 1.0 - s[idx]) / wsum)
            if lat == "susceptible":
                return float(np.dot(w, s[idx]) / wsum)
            return float(sum(o.per_capita["infections"][g] * N[j] for g, j in zip(r["groups"], idx)) / wsum)
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
        if not X:
            return np.asarray([self.row_ll({}, sts[0])[0] if sts[0] is not None else -np.inf]), {}
        ll = np.full(n, -np.inf)
        for i, st in enumerate(sts):
            if st is None:
                continue
            ll[i] = self.row_ll({k: float(v[i]) for k, v in X.items()}, st)[0]
        return ll, {}

    def at(self, x: dict):
        """(log-likelihood, expected observations) at one full parameter set (fixed values included); a run the
        engine refuses gives (-inf, [])."""
        X = {k: np.asarray([float(x[k])]) for k in self.names}
        st = self._states(X, 1)[0]
        if st is None:
            return -np.inf, []
        return self.row_ll({k: float(x[k]) for k in self.names}, st)


def _fit_row(E, r, e) -> dict:
    """One target at the mode, on the observation's own scale (Codex r1 P1-11): a share's expected count (n p)
    beside its observed positives, with the probability and the binomial variance; a count's expected count."""
    m = E.sources[r["source"]].noise["model"]
    row = {"unit": r["unit"], "groups": r["groups"], "source": r["source"], "latent": r["latent"],
           "observed": r["observed"], "n": r["n"], "noise": m}
    if m in ("binomial", "betabinomial"):
        de = float(E.sources[r["source"]].design_effect) if m == "betabinomial" else 1.0
        row.update(expected=float(e) * r["n"], expected_share=float(e), observed_share=r["observed"] / r["n"],
                   variance=r["n"] * float(e) * (1.0 - float(e)) * de)
    else:
        k = E.sources[r["source"]].noise.get("dispersion")
        row.update(expected=float(e), variance=float(e) + (float(e) ** 2 / float(k) if m == "negbin" else 0.0),
                   **({"dispersion": float(k)} if m == "negbin" else {}))
    return row


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

    def refuse_failures(where):
        # Codex r1 P1-9: the refusal policy holds on every path (sample, classification, mode, final point)
        if E.failed:
            raise C.CalibrationRefused("posterior_engine_failure", f"the engine failed closed on {E.failed} parameter "
                                       f"sets ({where}); the fit would be conditioned on the ones it ran")
    s = c = None
    scan_failed = 0
    if sample_it:
        sd = spec["calibration"].get("seed", 0) if seed is None else seed
        # hb3 fix 2a: above max_importance_params the chains start at the posterior mode, one proposal per chain
        # (toolkit/analysis/mode_start.py). Its scan over the prior is a start search, not part of the posterior: its
        # refusals are counted and reported, as the scan's below. The optimisation and the Hessian are part of the
        # fit: a refusal there refuses it (Codex r1 P1-3).
        from toolkit.analysis import mode_start as MS
        scanned = [0]

        def after_scan():
            scanned[0] += E.failed
            E.failed = 0
        modes = MS.find_modes(P, after_scan=after_scan) if MS.uses_metropolis(P) else None
        scan_failed = scanned[0]
        refuse_failures("while finding the posterior modes")
        s = MS.sample(P, seed=int(sd), modes=modes, after_scan=after_scan)
        scan_failed = scanned[0]
        refuse_failures("in the posterior sample")
        c = C.classify(P, s)
        refuse_failures("in the identifiability classification")
    if P.names:
        if s:
            start = s["best"]
        else:
            # a start search over the prior (not part of any posterior): its refusals are counted and reported
            start = _scan_start(P)
            scan_failed, E.failed = E.failed, 0
        xh, conv, msg = C._map_point(P, start)
        if not conv:          # Codex r2 P1-2: one restart from the point reached; a mode that still fails is refused
            xh, conv, msg = C._map_point(P, xh)
        refuse_failures("while finding the posterior mode")
        if not conv:
            raise C.CalibrationRefused("posterior_mode", f"the posterior mode search did not converge ({msg})")
    else:                                # every parameter fixed (a prior_range refit of the last free one)
        xh, conv, msg = [], True, "every parameter fixed: the configuration is evaluated as given"
    x = dict(E.fixed_params, **dict(zip(P.names, (float(v) for v in xh))))
    z = {u: x[f"z:{u}"] for u in E.units}
    fixed = dict(E.fixed_z)
    if not E.attr:                       # no per-unit attribute: every unit runs at its bound attributes
        fixed = {i: None for i in spec.unit_ids()}
    ll_hat, exp = E.at(x)
    refuse_failures("at the posterior mode")
    if not np.isfinite(ll_hat):
        raise C.CalibrationRefused("posterior_mode", "the posterior mode has no finite likelihood")
    from toolkit.analysis import mode_start as MS
    out = {"method": "posterior", "targets_kind": KIND, "x": x, "names": list(P.names), "params": E.params,
           "units_fitted": E.units, "fixed_z": fixed, "fixed_params": dict(E.fixed_params),
           "nuisance": [p for p in E.nuisance if p not in E.fixed_params],
           "theta": {p: x[p] for p in E.params}, "z": z,
           "diagnostics": {"converged": conv, "message": msg, "n_targets": len(E.rows), "n_parameters": len(P.names),
                           "at_bound": MS.near_bounds(P, xh, classes=(c or {}).get("classes")),
                           "observation": "per-source (calibration.sources)",
                           "evaluation": "initial_state" if E.state_only else "run_batch",
                           "loglik_at_mode": float(ll_hat), "start_search_refusals": scan_failed,
                           "fit": [_fit_row(E, r, exp[j]) for j, r in enumerate(E.rows)]}}
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
        ok(set(f["z"]) == {"u1", "u2"} and all(abs(imm[u]["expected_share"] - imm[u]["observed"] / 200) < 0.02
                                                for u in ("u1", "u2")),
           f"OT2 the mode reproduces each unit's survey immunity: {[(u, round(imm[u]['expected_share'], 4)) for u in imm]}")
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
                            (lambda r: r["calibration"].pop("range"), "attribute and range together"),
                            (lambda r: r["calibration"].pop("scale"), "calibration.scale: declare")):
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
        ok(all(abs(r["expected_share"] - r["observed"] / 400) < 0.05 for r in imm) and f["z"] == {}
           and f["fixed_z"] == {"all": None},
           f"OT9 the mode reproduces each group's immunity; no per-unit attribute "
           f"({[round(r['expected_share'], 3) for r in imm]})")
        hos = [r for r in fitrows if r["latent"] == "outcome:hosp"]
        ok(all(abs(r["expected"] - r["observed"]) / r["observed"] < 0.05 for r in hos),
           f"OT10 the per-group outcome ratios reproduce the outcome counts ({[round(r['expected']) for r in hos]})")
        E = ObservedProblem(sp, m)
        ok(not E.state_only and len(E.names) == 7, "OT11 attack-rate and outcome targets run the epidemic (run_batch)")
        # Codex r1 (tests first)
        fx = {"r0": 2.0, **{f"s0:{g}": 0.6 for g in "abc"}, **{f"ratio:hosp:{g}": 0.001 for g in "abc"}}
        f0 = fit(sp, m, fixed_params=fx, sample_it=False)
        ok(f0["names"] == [] and f0["theta"]["r0"] == 2.0 and len(f0["diagnostics"]["fit"]) == 9,
           "OT18 (r1 P1-10) a refit with every parameter fixed evaluates the fixed configuration")
        b0 = [r for r in f0["diagnostics"]["fit"] if r["noise"] == "binomial"][0]
        ok(abs(b0["expected"] - b0["expected_share"] * b0["n"]) < 1e-9 and b0["variance"] > 0
           and "observed_share" in b0, "OT19 (r1 P1-11) a share's fit row is on the count scale, with its share "
                                       "and binomial variance")
        orig = m.eng.run_batch

        def failing(*a, **k):
            raise RuntimeError("step cap reached (test)")
        m.eng.run_batch = failing
        try:
            fit(sp, m, fixed_params={k: v for k, v in fx.items() if k != "r0"}, sample_it=False)
            ok(False, "OT17 (r1 P1-9) a mode-only fit whose engine refuses is refused")
        except Exception as e:  # noqa: BLE001
            ok("posterior_engine_failure" in str(getattr(e, "code", "")) or "failed closed" in str(e)
               or "no finite" in str(e), f"OT17 (r1 P1-9) a mode-only fit whose engine refuses is refused: {e}")
        finally:
            m.eng.run_batch = orig
        r6 = copy.deepcopy(sp.raw)
        r6["calibration"]["sources"][0]["frame"]["excluded"] = [
            {"who": "x", "share": 0.1, "param": "rel", "relative_value_prior": {"dist": "uniform", "range": [0.5, 1]},
             "source": "t"}]
        r6["calibration"]["sources"][1]["bias"] = [{"mechanism": "antibody_waning", "param": "w",
                                                    "prior": {"dist": "uniform", "range": [0, 0.1]}, "source": "t"}]
        e6 = schema.validate(schema.Spec(r6, d, os.path.join(d, "decision_spec.yaml")))
        ok(any("frame.excluded is not supported" in x for x in e6) and any("antibody_waning" in x for x in e6),
           f"OT16 (r1 P1-2, P1-12) frame gaps and waning are refused on this path: {e6[:2]}")
        r7 = copy.deepcopy(sp.raw)
        for k, rg in ((0, [0.9, 1.0]), (1, [0.8, 1.0])):
            r7["calibration"]["sources"][k]["bias"] = [{"mechanism": "assay_sensitivity", "param": "se",
                                                        "prior": {"dist": "uniform", "range": rg}, "source": "t"}]
        e7 = schema.validate(schema.Spec(r7, d, os.path.join(d, "decision_spec.yaml")))
        ok(any("two different priors" in x for x in e7), f"OT21 (r1 P2-16) conflicting shared priors are refused: {e7[:2]}")
        # OT23 (r2 P1-2): a mode search that does not converge is refused
        from toolkit.analysis import calibration as C
        orig_map = C._map_point
        C._map_point = lambda P, start=None: (orig_map(P, start)[0], False, "ABNORMAL (test)")
        try:
            fit(sp, m, fixed_params={k: v for k, v in fx.items() if k != "r0"}, sample_it=False)
            ok(False, "OT23 (r2 P1-2) an unconverged mode is refused")
        except C.CalibrationRefused as e:
            ok("did not converge" in str(e), f"OT23 (r2 P1-2) an unconverged mode is refused: {e}")
        finally:
            C._map_point = orig_map
        # OT24 (r2 P2-6): a negative-binomial count row carries its own variance
        r8 = copy.deepcopy(sp.raw)
        r8["calibration"]["sources"][2]["noise"] = {"model": "negbin", "dispersion": 2.0}
        s8 = schema.Spec(r8, d, os.path.join(d, "decision_spec.yaml"))
        f8 = fit(s8, DirectModel(s8), fixed_params=fx, sample_it=False)
        nb = [r for r in f8["diagnostics"]["fit"] if r["noise"] == "negbin"][0]
        ok(abs(nb["variance"] - (nb["expected"] + nb["expected"] ** 2 / 2.0)) < 1e-9,
           f"OT24 (r2 P2-6) a negative-binomial row's variance is mu + mu^2 / k ({nb['variance']:.1f})")
    with tempfile.TemporaryDirectory() as d:
        # OT22 (r2 P1-1): one population mapping for state-only and run-based targets
        sp = _toy(d, attr=True)
        r = copy.deepcopy(sp.raw)
        with open(os.path.join(d, "units.csv"), "w") as fh:
            fh.write("id,pop_old,pop_c1,pop_c2,N,cov1\nu1,100000,100000,900000,500000;500000;500000,0;0.8;0.7\n"
                     "u2,100000,500000,500000,500000;500000;500000,0;0.9;0.6\n")
        r["decision_units"]["populations"] = {"old": {"column": "pop_old", "age_groups": ["old"]},
                                              "c1": {"column": "pop_c1", "age_groups": ["c1"]},
                                              "c2": {"column": "pop_c2", "age_groups": ["c2"]}}
        r["options"]["populations"] = {"total": "pop_c1"}
        s9 = schema.Spec(r, d, os.path.join(d, "decision_spec.yaml"))
        E9 = ObservedProblem(s9, DirectModel(s9))
        st = E9._states({"z:u1": np.asarray([1.0]), "z:u2": np.asarray([1.0])}, 1)[0]
        Nm = st[("u1", "none")][0]
        ok(E9.state_only and list(Nm) == [100000.0, 100000.0, 900000.0],
           f"OT22 (r2 P1-1) state-only targets use the population bands' sizes, as run-based ones do ({list(Nm)})")
    with tempfile.TemporaryDirectory() as d:
        # OT15 (r1 P1-1): the whole analysis with no per-unit attribute (per-group parameters only)
        import yaml
        from toolkit.analysis import run as RUN
        sp = _toy(d, attr=False)
        r = copy.deepcopy(sp.raw)
        with open(os.path.join(d, "targets.csv"), "w") as fh:
            fh.write("unit,groups,source,latent,pos,n\nall,a,pre,immunity,80,400\nall,b,pre,immunity,120,400\n"
                     "all,c,pre,immunity,200,400\n")
        r["calibration"]["parameters"] = ["s0:a", "s0:b", "s0:c"] + [f"ratio:hosp:{g}" for g in "abc"]
        r["calibration"]["priors"] = {k: v for k, v in r["calibration"]["priors"].items() if k != "r0"}
        r["calibration"]["sources"] = [r["calibration"]["sources"][0]]
        yaml.safe_dump(r, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
        res, err = None, None
        try:
            res = RUN.run(d, unsigned=True, log=lambda *a: None)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        ok(err is None and res["calibration"]["units"] == {"all": {"z": None}},
           f"OT15 (r1 P1-1) the analysis runs a calibration with no per-unit attribute: {err}")
    with tempfile.TemporaryDirectory() as d:
        # OT25: state-only observation targets whose likelihood is flat (no calibrated parameter or attribute moves
        # the observed pre-epidemic state, e.g. fixed s0 from raw survey ratios with only r0 calibrated) are refused.
        sp_ok = _toy(d, attr=True)
        r_ok = copy.deepcopy(sp_ok.raw)
        r_ok["calibration"]["parameters"] = ["r0"]
        r_ok["calibration"]["priors"]["r0"] = {"dist": "uniform", "range": [12.0, 18.0], "source": "test"}
        e_ok = schema.validate(schema.Spec(r_ok, d, os.path.join(d, "decision_spec.yaml")))
        # Now switch to initialiser: given with fixed s0 and no attribute (only r0 calibrated)
        with open(os.path.join(d, "units_fixed.csv"), "w") as fh:
            fh.write("id,pop,N,s0\nu1,1000000,600000;200000;200000,0.0;0.38;0.38\n"
                     "u2,500000,300000;100000;100000,0.0;0.305;0.305\n")
        r_flat = copy.deepcopy(r_ok)
        r_flat["engine"]["config"] = {
            "groups": ["old", "c1", "c2"], "structure": "birth_cohorts", "initialiser": "given",
            "generation_interval": [0.5, 0.5], "time_unit": "year",
            "components": {"cu": {"kind": "campaign", "targets": ["c1", "c2"], "reach": 0.8}}}
        r_flat["engine_parameters"] = {"efficacy:cu": {"value": 0.9, "source": "test", "reason": "test",
                                                       "basis": "literature"}}
        r_flat["decision_units"]["csv"] = "units_fixed.csv"
        r_flat["decision_units"]["engine_attributes"] = {
            "N": {"column": "N", "split": ";"}, "s0": {"column": "s0", "split": ";"},
            "always_immune": {"value": ["old"], "source": "test"},
            "contact_rate": {"value": 10.0, "source": "test"}, "seed": {"value": 1.0e-9, "source": "test"},
            "intro": {"value": "c2", "source": "test"}, "reach:cu": {"value": 0.8, "source": "test"}}
        r_flat["calibration"].pop("attribute")
        r_flat["calibration"].pop("range")
        r_flat["calibration"].pop("scale")
        r_flat["calibration"]["priors"].pop("coverage_scale")
        e_flat = schema.validate(schema.Spec(r_flat, d, os.path.join(d, "decision_spec.yaml")))
        # Clipped coverage [1.0, 1.5] with 0.96 reported coverage varies below 1.0417 (must pass, not false-refuse)
        r_clip = copy.deepcopy(sp_ok.raw)
        r_clip["calibration"]["priors"]["coverage_scale"]["range"] = r_clip["calibration"]["range"] = [1.0, 1.5]
        r_clip["decision_units"]["engine_attributes"]["coverage:d1"] = {"value": [0.0, 0.96, 0.96], "source": "test"}
        e_clip = schema.validate(schema.Spec(r_clip, d, os.path.join(d, "decision_spec.yaml")))
        # Pooled target where only a zero-weight group moves has a flat population-weighted share (must refuse)
        r_pool = copy.deepcopy(sp_ok.raw)
        r_pool["decision_units"]["populations"] = {"one": {"column": "pop", "age_groups": ["old", "c1"]}}
        r_pool["decision_units"]["engine_attributes"]["coverage:d1"] = {"value": [0.0, 0.0, 0.7], "source": "test"}
        with open(os.path.join(d, "targets_pool.csv"), "w") as fh:
            fh.write("unit,groups,source,latent,pos,n\nu1,old;c1;c2,sero,immunity,124,200\n")
        r_pool["calibration"]["targets"]["csv"] = "targets_pool.csv"
        e_pool = schema.validate(schema.Spec(r_pool, d, os.path.join(d, "decision_spec.yaml")))
        # Pure assay-sensitivity calibration (no engine params/attribute, non-empty source bias) is informative (passes)
        r_assay = copy.deepcopy(sp_ok.raw)
        r_assay["decision_units"]["engine_attributes"]["coverage_scale"] = {"value": 1.0, "source": "test"}
        for k_ in ("attribute", "range", "scale"):
            r_assay["calibration"].pop(k_)
        r_assay["calibration"]["priors"] = {}
        r_assay["calibration"]["sources"][0]["bias"] = [
            {"mechanism": "assay_sensitivity", "param": "se",
             "prior": {"dist": "uniform", "range": [0.8, 1.0]}, "source": "test"}]
        e_assay = schema.validate(schema.Spec(r_assay, d, os.path.join(d, "decision_spec.yaml")))
        # Order-independent check: malformed second unit is still caught even after a sensitive first unit
        r_bad2 = copy.deepcopy(sp_ok.raw)
        with open(os.path.join(d, "units_bad2.csv"), "w") as fh:
            fh.write("id,pop,N,cov1\nu1,1000000,600000;200000;200000,0;0.8;0.7\nu2,500000,300000;100000,0;0.9;0.6\n")
        r_bad2["decision_units"]["csv"] = "units_bad2.csv"
        e_bad2 = schema.validate(schema.Spec(r_bad2, d, os.path.join(d, "decision_spec.yaml")))
        ok(e_ok == [] and len(e_flat) == 1 and "pre-epidemic state" in e_flat[0]
           and e_clip == [] and len(e_pool) == 1 and "pre-epidemic state" in e_pool[0]
           and e_assay == [] and any("u2" in e for e in e_bad2),
           f"OT25 state-only observation flatness check handles flat, clipped, zero-weight pooled, assay-only, "
           f"and order-independent malformed units ({e_ok}, {e_flat}, {e_clip}, {e_pool}, {e_assay}, {e_bad2})")
        # OT26: diagnostics.at_bound flags a per-unit posterior mode pressed against a tight upper prior bound
        r_ab = copy.deepcopy(sp_ok.raw)
        r_ab["calibration"]["range"] = r_ab["calibration"]["priors"]["coverage_scale"]["range"] = [0.5, 1.1]
        with open(os.path.join(d, "targets_hi.csv"), "w") as fh:
            fh.write("unit,groups,source,latent,pos,n\nu1,c1;c2,sero,immunity,190,200\nu2,c1;c2,sero,immunity,120,200\n")
        r_ab["calibration"]["targets"]["csv"] = "targets_hi.csv"
        sp_ab = schema.Spec(r_ab, d, os.path.join(d, "decision_spec.yaml"))
        m_ab = DirectModel(sp_ab, log=lambda *a: None)
        m_ab.fit()
        fit_ab = fit(sp_ab, m_ab, seed=0, sample_it=False)
        ok("z:u1" in fit_ab["diagnostics"]["at_bound"] and "z:u2" not in fit_ab["diagnostics"]["at_bound"],
           f"OT26 posterior mode clipped against upper prior bound is reported in at_bound ({fit_ab['diagnostics']['at_bound']}, z={fit_ab['z']})")
        # OT27: RequestedEngine defers live state probe until admission (requiring immunity/susceptible in
        # outputs.per_capita), and a standard Engine without initial_state() calibrates a single unmapped group
        # via per_capita['immunity'].
        from toolkit.engines.built import requested as BREQ
        from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec
        b_no = BREQ.example_build()
        b_no["kind"] = "closed_form"
        b_no["request"].pop("compartments", None)
        b_no["request"]["age_groups"] = ["g1", "g2"]
        b_no["request"]["reductions"][0]["compare"] = {"per_capita.cases.g1": {"abs": 1.0e-3}}
        b_no["request"]["trials"][0]["estimand_mapping"]["value"] = "per_capita.cases.g1"
        b_yes = copy.deepcopy(b_no)
        b_yes["request"]["outputs"]["per_capita"] = ["cases", "severe", "immunity"]
        with open(os.path.join(d, "u_ot27.csv"), "w") as fh:
            fh.write("id,pop\nu1,10000\n")
        with open(os.path.join(d, "t_ot27.csv"), "w") as fh:
            fh.write("unit,groups,source,latent,pos,n\nu1,g2,sero,immunity,63,100\n")
        base_ot27 = {
            "schema_version": 1, "question": "test", "decision_maker": "test", "decision_date": "2026-01-01",
            "engine": {"build": b_no},
            "horizon": {"start": 0.0, "stop": 5.0},
            "decision_units": {
                "csv": "u_ot27.csv", "id": "id",
                "populations": {"target": {"column": "pop", "age_groups": ["g1"]}},
                "engine_attributes": {"N": {"column": "pop"}},
            },
            "options": {
                "populations": {"total": "pop"},
                "components": {"comp_a": {}},
                "packages": {"generate": True},
                "engine_map": {"comp_a": {"comp_a": 1}},
                "prices": {"base_year": "2026", "index": {"2026": 1.0}},
                "cost_items": [{"id": "c_a", "component": "comp_a", "value": 1.0, "price_year": "2026",
                                "price_rule": "as_published", "basis": "fixed", "includes": ["comp_a"]}],
            },
            "objective": {"outcome": "cases", "horizon": [0.0, 5.0], "discounting": "none"},
            "constraints": {"envelope_usd": 100.0},
            "comparator": {"package": "none", "text": "none"},
            "outcomes_reported": ["cases"],
            "calibration": {
                "method": "posterior", "parameters": [], "attribute": "rate_in", "range": [0.01, 0.5], "scale": "linear",
                "priors": {"rate_in": {"dist": "uniform", "range": [0.01, 0.5], "source": "test"}},
                "sources": [{"id": "sero", "measures": "seropositivity", "latent": {"quantity": "immunity"},
                             "frame": {"design": "cluster_survey"}, "bias": {"none": "test"},
                             "noise": {"model": "binomial"}}],
                "targets": {"kind": KIND, "csv": "t_ot27.csv", "unit": "unit", "groups": "groups",
                            "source": "source", "latent": "latent", "observed": "pos", "n": "n"},
            },
            "uncertainty": {
                "draws": 4, "seed": 1, "sources": [{"kind": "calibration"}],
                "excluded": [{"kind": "engine_parameters", "reason": "test"}, {"kind": "costs", "reason": "t"},
                             {"kind": "emulator", "reason": "test"}],
            },
            "analysis": {"evaluation": "direct"},
            "deliverable": {
                "kind": "package_per_unit", "allocation_file": "allocation_optimized.csv",
                "columns": {"unit": "unit", "package": "package", "value": "averted"},
                "headline": {"name": "total_averted", "outcome": "cases"},
            },
        }
        sp_no = schema.with_engine(schema.Spec(base_ot27, d, os.path.join(d, "spec_no.yaml")), BREQ.RequestedEngine(b_no))
        e_req_no = problems(sp_no, sp_no.engine().describe(), {p.name: p for p in sp_no.engine().parameters()})
        r_yes = copy.deepcopy(base_ot27)
        r_yes["engine"] = {"build": b_yes}
        sp_yes = schema.with_engine(schema.Spec(r_yes, d, os.path.join(d, "spec_yes.yaml")), BREQ.RequestedEngine(b_yes))
        e_req_yes = problems(sp_yes, sp_yes.engine().describe(), {p.name: p for p in sp_yes.engine().parameters()})

        class _NoInitEngine:
            batch_equals_run = True

            def describe(self):
                return EngineCard(
                    name="built:two_stage_mod", version="0.1", mechanisms=["m"],
                    components={"comp_a": [0, 1]}, unit_attributes={"N": "n", "rate_in": "r"},
                    outputs={"per_capita": ["cases", "severe", "immunity"], "windows": {"w0": ["prev"]},
                             "cost_drivers": ["units_delivered"]},
                    age_groups=["g1", "g2"], valid_ranges={"rate_in": [0.01, 0.5]},
                )

            def parameters(self):
                return [ParamSpec(name="eff_a", value=0.6, kind="fixed", units="fraction", lo=0.2, hi=0.9, source="test")]

            def run(self, params, unit, package, horizon, seed):
                r_in = float(unit.attributes["rate_in"])
                imm1 = 1.0 - float(np.exp(-r_in * 3.0))
                imm2 = 1.0 - float(np.exp(-r_in * 10.0))
                eff = float(params.get("eff_a", 0.6)) if package.components.get("comp_a") else 0.0
                c = r_in * (1.0 - eff)
                return Outcomes(
                    per_capita={"cases": {"g1": c, "g2": c}, "severe": {"g1": 0.1 * c, "g2": 0.1 * c},
                                "immunity": {"g1": imm1, "g2": imm2}},
                    windows={"w0": {"prev": c}},
                    cost_drivers={"units_delivered": float(package.components.get("comp_a", 0))},
                    meta={"age_shares": {"g1": 0.5, "g2": 0.5}},
                )

            def run_batch(self, param_sets, units, packages, horizon, seed):
                return [self.run(p, u, pk, horizon, seed) for p, u, pk in zip(param_sets, units, packages)]

        eng_ni = _NoInitEngine()
        sp_ni = schema.with_engine(schema.Spec(r_yes, d, os.path.join(d, "spec_ni.yaml")), eng_ni)
        e_ni = problems(sp_ni, eng_ni.describe(), {p.name: p for p in eng_ni.parameters()})
        m_ni = DirectModel(sp_ni, log=lambda *a: None)
        fit_ni = fit(sp_ni, m_ni, seed=0, sample_it=False)
        z_est = float(fit_ni["z"]["u1"])
        ok(any("outputs.per_capita" in e for e in e_req_no) and e_req_yes == [] and e_ni == []
           and abs(z_est - 0.0994) < 0.01 and fit_ni["diagnostics"]["evaluation"] == "run_batch",
           f"OT27 RequestedEngine defers state probe if immunity/susceptible declared, and standard Engine without "
           f"initial_state() calibrates single unmapped group via per_capita['immunity'] (no={e_req_no}, yes={e_req_yes}, "
           f"ni={e_ni}, z={z_est:.4f})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
