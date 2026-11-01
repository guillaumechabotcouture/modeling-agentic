#!/usr/bin/env python3
"""Compartmental engine (Starsim) behind the Engine contract: design note v2d §1.1.

    python3 toolkit/engines/compartmental/engine.py --self-test
    python3 toolkit/engines/compartmental/engine.py --card [path]   # writes card.yaml (after the pack is run)

Our own Starsim code: one `ss.Module` subclass, `GroupEpidemic`, holds group-level state as numpy
arrays with a leading batch axis (batch x groups) instead of per-agent arrays, inside an `ss.Sim`
with no network and no demographics (the sim's People object is a one-agent placeholder; group
sizes come from the unit). Interventions are `ss.Intervention` subclasses that act at the start of
the sim. The module draws no random numbers.

One unit is a closed population of G groups of size N_i: age bands with a contact matrix C_ij
(mean daily contacts of a person in i with people in j), or birth cohorts with homogeneous mixing
C_ij = c N_j / N. One discrete-time step of length dt (one day or one week), exponential survival:

    I_i,t = S_i,t-1 [1 - exp(-lambda_i,t)],  lambda_i,t = q f(t) sum_j C_ij sum_s w_s I_j,t-s / N_j

so the multigroup final-size relation holds exactly at any step length. `q` is per contact over the
infectious period; with `transmission: r0`, q = R0 / rho(C). Run modes: `to_extinction` (until
incidence over one generation interval falls below tol x N and the epidemic cannot grow again;
an epidemic still running at the step cap fails closed) and `windowed` (over the horizon).

Configuration (constructor keywords; anything else is refused):
  groups               group names (the card's age groups)
  structure            single_group | age_groups_contact_matrix | birth_cohorts
  mode                 to_extinction | windowed
  initialiser          given (unit attribute s0) | cohort_history (dose coverage by birth year)
  dt                   day | week
  generation_interval  [w_1..w_L] on the dt grid (sum 1), or {dt: <grid>, w: [...]}
  transmission         r0 (parameter r0) | q (parameter q)
  forcing              none | cosine (parameters forcing_amplitude, forcing_phase), forcing_period (days),
                       forcing_ref_phase (day of the cycle for reff_ref)
  importation          deterministic | branching (expected size p_major x final size)
  offspring            poisson | geometric (for p_major)
  components           {name: {kind: campaign, targets: [groups], reach} | {kind: doses, group, block_size, max_blocks}}
  targets_susceptible  false: doses reach people whatever their immunity (doses given to immune people are wasted)
  doses, dose_dependence   cohort_history: dose names; independent | nested
  outcomes             extra per-capita outcomes, each from the unit attribute ratio:<outcome> (per group)
  tol, step_cap, segment, rt_trajectory, results_path

Unit attributes: N (group sizes), s0 (given), contact (G x G) or contact_rate, birth_year,
decision_year, coverage:<dose> (latent true coverage per cohort), always_immune, always_susceptible,
seed (initial infections as a share of the unit's population), intro (group, or {group: share}),
reach:<component>, ratio:<outcome>. Time is in days. Nothing here names a disease.

`p_major` (probability of a major outbreak) is the one quantity computed from an equation rather
than from the sim: the multitype branching-process extinction equations x_i = G_i(x). It is labelled
derived. Analytic final-size solvers are test oracles in benchmarks/oracles.py, never used here.
"""
from __future__ import annotations

import functools
import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import (EngineCard, Horizon, Outcomes, Package, ParamSpec, UnitSpec,  # noqa: E402
                                      check_engine, resolve_params)

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "benchmarks", "pack.yaml")
RESULTS = os.path.join(HERE, "benchmarks", "results.yaml")
NAME, VERSION = "compartmental-starsim", "0.1"

STRUCTURES = ("single_group", "age_groups_contact_matrix", "birth_cohorts")
MODES = ("to_extinction", "windowed")
INITIALISERS = ("given", "cohort_history")
DT_DAYS = {"day": 1.0, "week": 7.0}
TRANSMISSION = ("r0", "q")
FORCING = ("none", "cosine")
IMPORTATION = ("deterministic", "branching")
OFFSPRING = ("poisson", "geometric")
DEPENDENCE = ("independent", "nested")
KINDS = ("campaign", "doses")
DECLARED = "declared per question (decision spec), with its source"
PUBLISHED_LABEL = ("validated against analytic identities and simulation-based coverage; published benchmarks "
                   "FS7 and RN5 not yet assessed")


def _num(x, what):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        raise ValueError(f"{what}: {x!r} is not a finite number")
    return float(x)


def check_generation_interval(gi, dt: str) -> np.ndarray:
    """w_1..w_L on the dt grid: finite, non-negative, summing to 1; a gap, a negative value or another grid
    fails closed."""
    if isinstance(gi, dict):
        if gi.get("dt") != dt:
            raise ValueError(f"generation interval on the {gi.get('dt')!r} grid, engine dt is {dt!r}")
        gi = gi.get("w")
    if not isinstance(gi, (list, tuple)) or not gi:
        raise ValueError("generation interval: give a non-empty list w_1..w_L")
    w = []
    for i, x in enumerate(gi):
        if x is None or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
            raise ValueError(f"generation interval: gap or non-number at step {i + 1} ({x!r})")
        if float(x) < 0:
            raise ValueError(f"generation interval: negative value at step {i + 1} ({x})")
        w.append(float(x))
    if abs(sum(w) - 1.0) > 1e-9:
        raise ValueError(f"generation interval sums to {sum(w)!r}, not 1")
    return np.asarray(w, float)


def spectral_radius(M) -> np.ndarray:
    """Dominant eigenvalue (Perron root) of a non-negative matrix or a batch of them."""
    M = np.asarray(M, float)
    if M.shape[-1] == 1:
        return M[..., 0, 0].copy()
    return np.max(np.linalg.eigvals(M).real, axis=-1)


def cohort_susceptible(coverage: np.ndarray, sero: np.ndarray, dependence: str) -> np.ndarray:
    """Share still susceptible after the doses, per member and group. coverage (D, B, G) is the latent true
    coverage of each dose, sero (D, B) the share of those still susceptible each dose seroconverts.
    independent: prod_d (1 - c_d e_d). nested: one propensity u ~ U(0, 1) per person; dose d reaches u < c_d,
    so s = integral over u of prod_{d: u < c_d} (1 - e_d)."""
    D = coverage.shape[0]
    if D == 0:
        return np.ones(coverage.shape[1:])
    if dependence == "independent":
        return np.prod(1.0 - coverage * sero[:, :, None], axis=0)
    order = np.sort(coverage, axis=0)                       # ascending cut points per member and group
    cuts = np.concatenate([np.zeros((1,) + coverage.shape[1:]), order, np.ones((1,) + coverage.shape[1:])])
    out = np.zeros(coverage.shape[1:])
    for k in range(D + 1):
        lo, hi = cuts[k], cuts[k + 1]
        mid = 0.5 * (lo + hi)
        prod = np.ones(coverage.shape[1:])
        for d in range(D):
            prod = np.where(mid < coverage[d], prod * (1.0 - sero[d][:, None]), prod)
        out += (hi - lo) * prod
    return out


def apply_component(s: np.ndarray, N: np.ndarray, comp: dict, level: np.ndarray, eff: np.ndarray,
                    reach: np.ndarray, groups: list, targets_susceptible: bool):
    """One all-or-nothing component acting before the epidemic. s (B, G) susceptible shares, N (B, G) sizes,
    level (B,) the package value, eff (B,) efficacy, reach (B,) the share of the targeted group reached
    (campaign). Returns (new s, doses given (B,))."""
    s = s.copy()
    doses = np.zeros(len(level))
    if comp["kind"] == "campaign":
        on = level > 0
        for g in comp["targets"]:
            j = groups.index(g)
            if targets_susceptible:
                doses += np.where(on, reach * s[:, j] * N[:, j], 0.0)
            else:
                doses += np.where(on, reach * N[:, j], 0.0)
            s[:, j] = np.where(on, s[:, j] * (1.0 - reach * eff), s[:, j])
    else:
        j = groups.index(comp["group"])
        D = level * float(comp["block_size"])
        if targets_susceptible:
            sus = s[:, j] * N[:, j]
            used = np.minimum(D, sus)
            frac = np.divide(used, sus, out=np.zeros_like(used), where=sus > 0)
        else:
            used = np.minimum(D, N[:, j])
            frac = used / N[:, j]
        doses += used
        s[:, j] = s[:, j] * (1.0 - frac * eff)
    return s, doses


def extinction_probability(K: np.ndarray, offspring: str, iters: int = 200000) -> np.ndarray:
    """Multitype branching process: x_i = G_i(x), the probability that a chain started by one case in group i
    dies out. K (B, G, G) with K[b, j, i] the expected infections in j caused by one case in i. Poisson:
    G_i(x) = exp(-sum_j K_ji (1 - x_j)); geometric: G_i(x) = 1 / (1 + sum_j K_ji (1 - x_j)). Iterated from
    x = 0 (the minimal fixed point), then polished by Newton steps."""
    B, G, _ = K.shape
    KT = np.swapaxes(K, 1, 2)                              # KT[b, i, j] = K[b, j, i]

    def g(x):
        m = np.einsum("bij,bj->bi", KT, 1.0 - x)
        return np.exp(-m) if offspring == "poisson" else 1.0 / (1.0 + m)
    x = np.zeros((B, G))
    for _ in range(iters):
        xn = g(x)
        if np.max(np.abs(xn - x)) < 1e-15:
            x = xn
            break
        x = xn
    for _ in range(50):                                    # Newton polish of x - G(x) = 0, per member
        m = np.einsum("bij,bj->bi", KT, 1.0 - x)
        gx = np.exp(-m) if offspring == "poisson" else 1.0 / (1.0 + m)
        dg = (gx if offspring == "poisson" else gx ** 2)[:, :, None] * KT     # dG_i/dx_j
        J = np.eye(G)[None] - dg
        try:
            step = np.linalg.solve(J, (x - gx)[:, :, None])[:, :, 0]
        except np.linalg.LinAlgError:
            break
        xn = np.clip(x - step, 0.0, 1.0)
        bad = ~np.isfinite(xn).all(axis=1) | (np.max(np.abs(xn - gx), axis=1) > np.max(np.abs(x - gx), axis=1))
        xn[bad] = x[bad]
        if np.max(np.abs(xn - x)) < 1e-16:
            x = xn
            break
        x = xn
    return x


# ------------------------------------------------------------------------------- Starsim module
def _ss():
    import starsim as ss
    return ss


@functools.lru_cache(maxsize=None)
def _module_class():
    ss = _ss()

    class GroupEpidemic(ss.Module):
        """Group-level discrete-time epidemic with susceptible depletion, batch axis B x G (design note §1.1).

        State: S (B, G) susceptible counts, the incidence history over one generation interval (L, B, G),
        cumulative infections, and the per-step incidence. A member that has converged (to_extinction) is
        frozen. At the first step of the first segment it records the state the interventions left and
        places the seeds; transmission starts at the next step."""

        def __init__(self, ctx: dict, state: dict | None, offset: int):
            super().__init__(name="groupepidemic")
            self.ctx, self._carry, self.offset = ctx, state, int(offset)

        def init_post(self):
            super().init_post()
            c = self.ctx
            if self._carry is None:
                self.S = c["s_init"] * c["N"]
                self.hist = np.zeros((len(c["w"]),) + self.S.shape)
                self.inc = []
                self.active = np.ones(self.S.shape[0], bool)
                self.stopped = np.full(self.S.shape[0], -1)
                self.s_first = None
                self.doses = {cn: np.zeros(self.S.shape[0]) for cn in c["components"]}
            else:
                for k, v in self._carry.items():
                    setattr(self, k, v)
            self._last = np.zeros(self.S.shape[0])

        def init_results(self):
            super().init_results()
            self.define_results(
                ss.Result("new_infections", label="New infections (batch member 0, all groups)"),
                ss.Result("susceptible", label="Susceptible (batch member 0, all groups)"),
                ss.Result("n_active", label="Batch members still running"))

        def export(self) -> dict:
            return {k: getattr(self, k) for k in ("S", "hist", "inc", "active", "stopped", "s_first", "doses")}

        @property
        def k(self) -> int:
            return self.offset + int(self.ti)

        def step(self):
            c = self.ctx
            k = self.k
            if k == 0:                                          # interventions have acted: record, then seed
                self.s_first = self.S / c["N"]
                seed = np.minimum(c["seed"], self.S)
                self.S = self.S - seed
                self.hist[0] = seed
                self.inc.append(seed.copy())
                self._last = seed.sum(axis=1)
                return
            act = self.active
            new = np.zeros_like(self.S)
            if act.any():
                force = np.einsum("l,lbg->bg", c["w"], self.hist[:, act]) / c["N"][act]
                f = c["forcing"](k * c["dt_days"], act)
                lam = (c["q"][act] * f)[:, None] * np.einsum("bij,bj->bi", c["C"][act], force)
                new[act] = self.S[act] * -np.expm1(-lam)
                self.S[act] = self.S[act] - new[act]
            self.hist = np.roll(self.hist, 1, axis=0)
            self.hist[0] = new
            self.inc.append(new)
            self._last = new.sum(axis=1)
            if c["mode"] == "to_extinction" and act.any():
                # Stop rule: the epidemic cannot grow again (Reff at the forcing's maximum below 1) and the
                # infections still to come, bounded by one generation's incidence x R / (1 - R), are below
                # tol x N. Near-critical epidemics run on (and fail closed at the step cap).
                recent = self.hist[:, act].sum(axis=(0, 2)) / c["N"][act].sum(axis=1)
                cand = np.nonzero(act)[0][recent < c["tol"]]
                if len(cand):
                    s = self.S[cand] / c["N"][cand]
                    r = c["q"][cand] * c["fmax"][cand] * spectral_radius(s[:, :, None] * c["C"][cand])
                    rest = recent[np.searchsorted(np.nonzero(act)[0], cand)] * r / np.maximum(1.0 - r, 1e-300)
                    done = cand[(r < 1.0) & (rest < c["tol"])]
                    self.active[done] = False
                    self.stopped[done] = k

        def update_results(self):
            super().update_results()
            self.results["new_infections"][self.ti] = float(self._last[0])
            self.results["susceptible"][self.ti] = float(self.S[0].sum())
            self.results["n_active"][self.ti] = float(self.active.sum())

    return GroupEpidemic


@functools.lru_cache(maxsize=None)
def _component_class():
    ss = _ss()

    class AllOrNothingComponent(ss.Intervention):
        """One package component, acting once at the start of the sim: it reaches a share of the targeted
        groups and protects a share `e` of the susceptibles it reaches (all-or-nothing)."""

        def __init__(self, cname: str, ctx: dict):
            super().__init__(name=f"component_{cname}")
            self.cname, self.ctx = cname, ctx
            self.doses = None

        def step(self):
            if self.ti != 0:
                return
            mod = self.sim.diseases.groupepidemic
            c = self.ctx
            s, self.doses = apply_component(mod.S / c["N"], c["N"], c["components"][self.cname],
                                            c["levels"][self.cname], c["eff"][self.cname], c["reach"][self.cname],
                                            c["groups"], c["targets_susceptible"])
            mod.S = s * c["N"]
            mod.doses[self.cname] = self.doses

    return AllOrNothingComponent


# ------------------------------------------------------------------------------------ engine
class CompartmentalEngine:
    def __init__(self, groups=("all",), structure="single_group", mode="to_extinction", initialiser="given",
                 dt="day", generation_interval=(1.0,), transmission="r0", forcing="none", forcing_period=365.0,
                 forcing_ref_phase=0.0, importation="deterministic", offspring="poisson", components=None,
                 targets_susceptible=False, doses=(), dose_dependence="independent", outcomes=(), tol=1e-9,
                 step_cap=20000, segment=256, rt_trajectory=True, results_path=None):
        for val, allowed, what in ((structure, STRUCTURES, "structure"), (mode, MODES, "mode"),
                                   (initialiser, INITIALISERS, "initialiser"), (dt, tuple(DT_DAYS), "dt"),
                                   (transmission, TRANSMISSION, "transmission"), (forcing, FORCING, "forcing"),
                                   (importation, IMPORTATION, "importation"), (offspring, OFFSPRING, "offspring"),
                                   (dose_dependence, DEPENDENCE, "dose_dependence")):
            if val not in allowed:
                raise ValueError(f"{what} {val!r} not in {allowed}")
        groups = [str(g) for g in groups]
        if not groups or len(set(groups)) != len(groups):
            raise ValueError("groups: give distinct group names")
        if structure == "single_group" and len(groups) != 1:
            raise ValueError("single_group structure takes exactly one group")
        if initialiser == "cohort_history" and structure != "birth_cohorts":
            raise ValueError("cohort_history needs structure birth_cohorts (cohorts are indexed by birth year)")
        self.groups, self.structure, self.mode, self.initialiser = groups, structure, mode, initialiser
        self.dt, self.dt_days = dt, DT_DAYS[dt]
        self.w = check_generation_interval(list(generation_interval) if not isinstance(generation_interval, dict)
                                           else generation_interval, dt)
        self.transmission, self.forcing, self.importation, self.offspring = transmission, forcing, importation, offspring
        self.forcing_period = _num(forcing_period, "forcing_period")
        self.forcing_ref_phase = _num(forcing_ref_phase, "forcing_ref_phase")
        self.components = {}
        for cname, cfg in (components or {}).items():
            cfg = dict(cfg or {})
            if cfg.get("kind") not in KINDS:
                raise ValueError(f"component {cname}: kind {cfg.get('kind')!r} not in {KINDS}")
            if cfg["kind"] == "campaign":
                bad = [g for g in cfg.get("targets") or [] if g not in groups]
                if not cfg.get("targets") or bad:
                    raise ValueError(f"component {cname}: targets must be declared groups ({bad})")
                r = _num(cfg.get("reach", 1.0), f"component {cname} reach")
                if not 0 <= r <= 1:
                    raise ValueError(f"component {cname}: reach {r} outside [0, 1]")
                cfg = {"kind": "campaign", "targets": list(cfg["targets"]), "reach": r}
            else:
                if cfg.get("group") not in groups:
                    raise ValueError(f"component {cname}: group {cfg.get('group')!r} not declared")
                mb = cfg.get("max_blocks")
                if isinstance(mb, bool) or not isinstance(mb, int) or mb < 1:
                    raise ValueError(f"component {cname}: max_blocks must be a positive integer")
                bs = _num(cfg.get("block_size"), f"component {cname} block_size")
                if bs <= 0:
                    raise ValueError(f"component {cname}: block_size must be positive")
                cfg = {"kind": "doses", "group": cfg["group"], "block_size": bs, "max_blocks": mb}
            self.components[str(cname)] = cfg
        self.targets_susceptible = bool(targets_susceptible)
        self.doses = [str(d) for d in doses]
        if initialiser != "cohort_history" and self.doses:
            raise ValueError("doses are declared only with the cohort_history initialiser")
        self.dose_dependence = dose_dependence
        self.outcomes = [str(o) for o in outcomes]
        if "infections" in self.outcomes:
            raise ValueError("infections is always an output; outcomes lists the others")
        self.tol = _num(tol, "tol")
        self.step_cap, self.segment = int(step_cap), int(segment)
        if self.tol <= 0 or self.step_cap < 1 or self.segment < 1:
            raise ValueError("tol, step_cap and segment must be positive")
        self.rt_trajectory = bool(rt_trajectory)
        self.results_path = results_path

    # ---------------------------------------------------------------------------- contract
    def config(self) -> dict:
        return {"groups": self.groups, "structure": self.structure, "mode": self.mode,
                "initialiser": self.initialiser, "dt": self.dt, "generation_interval": [float(x) for x in self.w],
                "transmission": self.transmission, "forcing": self.forcing, "forcing_period": self.forcing_period,
                "forcing_ref_phase": self.forcing_ref_phase, "importation": self.importation,
                "offspring": self.offspring, "components": self.components,
                "targets_susceptible": self.targets_susceptible, "doses": self.doses,
                "dose_dependence": self.dose_dependence, "outcomes": self.outcomes, "tol": self.tol,
                "step_cap": self.step_cap, "segment": self.segment}

    def parameters(self) -> list:
        out = []
        if self.transmission == "r0":
            out.append(ParamSpec("r0", 2.5, "calibrated", "basic reproduction number (all groups susceptible)",
                                 0.05, 40.0, "calibrated per run, or " + DECLARED))
        else:
            out.append(ParamSpec("q", 0.2, "calibrated", "transmissibility per contact over the infectious period",
                                 0.0, 50.0, "calibrated per run, or " + DECLARED))
        if self.forcing == "cosine":
            out.append(ParamSpec("forcing_amplitude", 0.0, "calibrated", "share", 0.0, 1.0, "calibrated or " + DECLARED))
            out.append(ParamSpec("forcing_phase", 0.0, "calibrated", "day of peak transmission", 0.0,
                                 self.forcing_period, "calibrated or " + DECLARED))
        for c in self.components:
            out.append(ParamSpec(f"efficacy:{c}", 1.0, "fixed", "share of reached susceptibles protected", 0.0, 1.0,
                                 DECLARED))
        for d in self.doses:
            out.append(ParamSpec(f"seroconversion:{d}", 1.0, "fixed", "share of the still susceptible protected",
                                 0.0, 1.0, DECLARED))
        return out

    def _unit_attributes(self) -> dict:
        a = {"N": "group sizes, one per group (> 0)",
             "seed": "initial infections as a share of the unit's population (default 1e-9)",
             "intro": "introduction group, or {group: share} (default: the first group)"}
        if self.structure == "age_groups_contact_matrix":
            a["contact"] = "G x G mean daily contacts of a person in group i with people in group j (>= 0)"
        else:
            a["contact_rate"] = "mean daily contacts per person (homogeneous mixing, C_ij = c N_j / N; default 1)"
        if self.initialiser == "given":
            a["s0"] = "initial susceptible share per group [0, 1] (a latent quantity, calibrated)"
        else:
            a.update({"birth_year": "birth year of each cohort group", "decision_year": "year of the decision",
                      **{f"coverage:{d}": f"latent true coverage of dose {d} per cohort [0, 1]" for d in self.doses}})
        a["always_immune"] = "groups declared immune (from the spec, with a source)"
        a["always_susceptible"] = "groups declared susceptible (from the spec, with a source)"
        for c, cfg in self.components.items():
            if cfg["kind"] == "campaign":
                a[f"reach:{c}"] = f"share of the targeted groups component {c} reaches (default {cfg['reach']})"
        for o in self.outcomes:
            a[f"ratio:{o}"] = f"{o} per infection, one per group"
        return a

    def _window_metrics(self) -> list:
        m = ["attack_rate", "s_start", "s_end", "r0", "reff_start", "reff_end", "reff_ref", "growth_rate",
             "growth_defined", "p_major"] + [f"attack_rate:{g}" for g in self.groups]
        return m + (["converged", "steps"] if self.mode == "to_extinction" else [])

    def describe(self) -> EngineCard:
        bench = []
        if self.results_path and os.path.exists(self.results_path):
            import yaml
            bench = (yaml.safe_load(open(self.results_path)) or {}).get("benchmarks", [])
        comps = {c: ([0, 1] if cfg["kind"] == "campaign" else list(range(cfg["max_blocks"] + 1)))
                 for c, cfg in self.components.items()}
        return EngineCard(
            name=NAME, version=VERSION,
            mechanisms=[
                "closed population of groups (age bands with a contact matrix, or birth cohorts with homogeneous "
                "mixing C_ij = c N_j / N); no births, deaths, waning or migration within the horizon",
                "discrete-time transmission with susceptible depletion in the exponential-survival form, a declared "
                "generation interval on the dt grid, optional cosine seasonal forcing",
                "R0 = q rho(C) from the next-generation matrix; Reff = q f rho(diag(s) C)",
                "susceptibility initialisers: given (calibrated latent share) or cohort_history (latent true dose "
                "coverage by birth year, seroconversion, declared dose dependence)",
                "interventions before the epidemic: all-or-nothing protection of the susceptibles reached; doses "
                "given to immune people are wasted unless targets_susceptible is declared",
                "p_major: multitype branching-process extinction equations (derived from an equation, not from the "
                "sim)",
                f"run mode {self.mode}"],
            components=comps or {"none": [0]},
            unit_attributes=self._unit_attributes(),
            outputs={"per_capita": ["infections"] + self.outcomes, "windows": self._window_metrics(),
                     "cost_drivers": [f"doses:{c}" for c in self.components]},
            age_groups=list(self.groups),
            valid_ranges={"s0": [0.0, 1.0], "r0": [0.05, 40.0], "seed": [0.0, 0.01]},
            benchmarks=bench,
            notes=["p_major is derived (multitype extinction equations), not simulated",
                   "out of range: births, deaths, waning and migration within the horizon; timing within a season "
                   "for interventions (all act before the epidemic); leaky protection",
                   "deterministic: the module draws no random numbers; the seed is recorded only",
                   PUBLISHED_LABEL,
                   "configuration: " + json.dumps(self.config(), sort_keys=True)])

    # ------------------------------------------------------------------------- batch inputs
    def _member(self, P: dict, unit: UnitSpec, package: Package) -> dict:
        a = dict(unit.attributes)
        G = len(self.groups)
        known = set(self._unit_attributes())
        bad = [k for k in a if k not in known]
        if bad:
            raise ValueError(f"unit {unit.id}: undeclared attributes {bad}")
        N = np.asarray([_num(x, "N") for x in (a.get("N") or [])], float)
        if len(N) != G or np.any(N <= 0):
            raise ValueError(f"unit {unit.id}: N must give {G} positive group sizes")
        if self.structure == "age_groups_contact_matrix":
            C = np.asarray(a.get("contact"), float)
            if C.shape != (G, G) or not np.all(np.isfinite(C)) or np.any(C < 0):
                raise ValueError(f"unit {unit.id}: contact must be a {G} x {G} non-negative matrix")
        else:
            c = _num(a.get("contact_rate", 1.0), "contact_rate")
            C = c * np.tile(N / N.sum(), (G, 1))
        rho = float(spectral_radius(C))
        if self.transmission == "r0":
            if rho <= 0:
                raise ValueError(f"unit {unit.id}: the contact matrix has spectral radius 0")
            q = P["r0"] / rho
        else:
            q = P["q"]
        if self.initialiser == "given":
            s = np.asarray([_num(x, "s0") for x in (a.get("s0") or [])], float)
            if len(s) != G:
                raise ValueError(f"unit {unit.id}: s0 must give {G} values")
            cov = np.zeros((0, G))
        else:
            by = a.get("birth_year")
            if not isinstance(by, (list, tuple)) or len(by) != G:
                raise ValueError(f"unit {unit.id}: birth_year must give {G} years")
            cov = []
            for d in self.doses:
                v = a.get(f"coverage:{d}")
                if not isinstance(v, (list, tuple)) or len(v) != G:
                    raise ValueError(f"unit {unit.id}: coverage:{d} must give {G} values (one per birth cohort)")
                cov.append([_num(x, f"coverage:{d}") for x in v])
            cov = np.asarray(cov, float).reshape(len(self.doses), G)
            if np.any(cov < 0) or np.any(cov > 1):
                raise ValueError(f"unit {unit.id}: coverage outside [0, 1]")
            s = None
        for key in ("always_immune", "always_susceptible"):
            bad = [g for g in a.get(key) or [] if g not in self.groups]
            if bad:
                raise ValueError(f"unit {unit.id}: {key} names undeclared groups {bad}")
        intro = a.get("intro", self.groups[0])
        shares = {intro: 1.0} if isinstance(intro, str) else dict(intro)
        if any(g not in self.groups for g in shares) or abs(sum(shares.values()) - 1) > 1e-9:
            raise ValueError(f"unit {unit.id}: intro must name declared groups with shares summing to 1")
        pi = np.asarray([float(shares.get(g, 0.0)) for g in self.groups])
        seed = _num(a.get("seed", 1e-9), "seed")
        if not 0 <= seed <= 0.01:
            raise ValueError(f"unit {unit.id}: seed {seed} outside [0, 0.01]")
        comp = {}
        for k, v in package.components.items():
            if not self.components and k == "none" and v == 0:      # the card's placeholder when nothing is declared
                continue
            if k not in self.components:
                raise ValueError(f"unknown component {k!r}")
            cfg = self.components[k]
            allowed = [0, 1] if cfg["kind"] == "campaign" else list(range(cfg["max_blocks"] + 1))
            if v not in allowed:
                raise ValueError(f"component {k}={v!r} not in {allowed}")
            comp[k] = float(v)
        reach = {c: _num(a.get(f"reach:{c}", cfg.get("reach", 1.0)), f"reach:{c}")
                 for c, cfg in self.components.items()}
        ratios = {}
        for o in self.outcomes:
            v = a.get(f"ratio:{o}")
            if not isinstance(v, (list, tuple)) or len(v) != G:
                raise ValueError(f"unit {unit.id}: ratio:{o} must give {G} values")
            ratios[o] = np.asarray([_num(x, f"ratio:{o}") for x in v], float)
        return {"N": N, "C": C, "q": float(q), "s": s, "cov": cov, "pi": pi, "seed": seed, "comp": comp,
                "reach": reach, "ratios": ratios, "attrs": a,
                "amp": float(P.get("forcing_amplitude", 0.0)), "phase": float(P.get("forcing_phase", 0.0)),
                "eff": {c: float(P[f"efficacy:{c}"]) for c in self.components},
                "sero": np.asarray([float(P[f"seroconversion:{d}"]) for d in self.doses], float)}

    def contact_matrix(self, unit: UnitSpec) -> list:
        """The unit's contact matrix C_ij as the engine uses it (cohorts: c N_j / N, population-weighted)."""
        return self._member(resolve_params(self, {}), unit, Package("none", {}))["C"].tolist()

    def _context(self, param_sets, unit, packages) -> dict:
        B = len(param_sets)
        units = list(unit) if isinstance(unit, (list, tuple)) else [unit] * B
        pkgs = list(packages) if isinstance(packages, (list, tuple)) else [packages] * B
        if len(units) != B or len(pkgs) != B or B == 0:
            raise ValueError("run_batch: give one unit (or one per pair) and one package (or one per pair)")
        mem = [self._member(resolve_params(self, p), u, k) for p, u, k in zip(param_sets, units, pkgs)]
        G = len(self.groups)
        N = np.stack([m["N"] for m in mem])
        if self.initialiser == "given":
            s_init = np.stack([m["s"] for m in mem])
        else:
            cov = np.stack([m["cov"] for m in mem], axis=1)                  # (D, B, G)
            sero = np.stack([m["sero"] for m in mem], axis=1)                # (D, B)
            s_init = cohort_susceptible(cov, sero, self.dose_dependence)
        for b, m in enumerate(mem):
            for g in m["attrs"].get("always_immune") or []:
                s_init[b, self.groups.index(g)] = 0.0
            for g in m["attrs"].get("always_susceptible") or []:
                s_init[b, self.groups.index(g)] = 1.0
        if np.any(s_init < 0) or np.any(s_init > 1) or not np.all(np.isfinite(s_init)):
            raise ValueError("susceptible share outside [0, 1]")
        amp = np.asarray([m["amp"] for m in mem])
        phase = np.asarray([m["phase"] for m in mem])
        per = self.forcing_period
        cosine = self.forcing == "cosine"

        def forcing(t, sel=slice(None)):
            if not cosine:
                return np.ones(B)[sel]
            return 1.0 + amp[sel] * np.cos(2.0 * math.pi * (t - phase[sel]) / per)
        ctx = {"B": B, "G": G, "N": N, "C": np.stack([m["C"] for m in mem]), "q": np.asarray([m["q"] for m in mem]),
               "s_init": s_init, "w": self.w, "dt_days": self.dt_days, "mode": self.mode, "tol": self.tol,
               "pi": np.stack([m["pi"] for m in mem]),
               "seed_share": np.asarray([m["seed"] for m in mem]),
               "forcing": forcing, "fmax": 1.0 + (np.abs(amp) if cosine else np.zeros(B)),
               "groups": self.groups, "components": self.components, "targets_susceptible": self.targets_susceptible,
               "levels": {c: np.asarray([m["comp"].get(c, 0.0) for m in mem]) for c in self.components},
               "eff": {c: np.asarray([m["eff"][c] for m in mem]) for c in self.components},
               "reach": {c: np.asarray([m["reach"][c] for m in mem]) for c in self.components},
               "members": mem}
        ctx["seed"] = (ctx["seed_share"] * N.sum(axis=1))[:, None] * ctx["pi"]
        return ctx

    def initial_state(self, params: dict, unit: UnitSpec, package: Package | None = None) -> dict:
        """The initialiser's output and the state after the package's start-of-sim components, without a sim
        (for calibrations that observe coverage or immunity only). Checked against the sim (CI4)."""
        ctx = self._context([params], unit, [package or Package("none", {})])
        s = ctx["s_init"].copy()
        doses = {}
        for c in self.components:
            s, d = apply_component(s, ctx["N"], self.components[c], ctx["levels"][c], ctx["eff"][c], ctx["reach"][c],
                                   self.groups, self.targets_susceptible)
            doses[c] = float(d[0])
        return {"N": [float(x) for x in ctx["N"][0]], "s_initialiser": [float(x) for x in ctx["s_init"][0]],
                "s_start": [float(x) for x in s[0]], "doses": doses}

    # ------------------------------------------------------------------------------ the sim
    def _sim(self, ctx, state, steps: int, offset: int, seed: int):
        ss = _ss()
        GroupEpidemic = _module_class()
        mod = GroupEpidemic(ctx, state, offset)
        itv = []
        if state is None:
            Comp = _component_class()
            itv = [Comp(c, ctx) for c in self.components]
        dt = ss.days(1) if self.dt == "day" else ss.weeks(1)
        sim = ss.Sim(n_agents=1, start=0, stop=int(steps), dt=dt, diseases=mod, interventions=itv,
                     rand_seed=int(seed), verbose=0)
        return sim

    def build_sim(self, params, unit: UnitSpec, package: Package, horizon: Horizon, seed: int):
        """The Starsim sim of one run's first segment (not yet run), and its context."""
        ctx = self._context([params], unit, [package])
        steps = self._first_steps(horizon)
        return self._sim(ctx, None, steps, 0, seed), ctx

    def _first_steps(self, horizon: Horizon) -> int:
        if self.mode == "windowed":
            n = int(round(float(horizon.stop) / self.dt_days))
            if n < 1 or abs(n * self.dt_days - float(horizon.stop)) > 1e-9 * max(1.0, float(horizon.stop)):
                raise ValueError(f"windowed horizon stop {horizon.stop} is not a whole number of {self.dt} steps")
            if float(horizon.start) != 0.0:
                raise ValueError("the horizon starts at day 0 (the importation)")
            return n
        return min(self.segment, self.step_cap)

    def _simulate(self, ctx, horizon, seed):
        """Run the sim (segments of doubling length in to_extinction mode); returns the module state."""
        steps = self._first_steps(horizon)
        sim = self._sim(ctx, None, steps, 0, seed)
        state, done = self._run_until_done(sim, steps), steps
        while self.mode == "to_extinction" and state["active"].any():
            if done >= self.step_cap:
                bad = [int(b) for b in np.nonzero(state["active"])[0]]
                raise RuntimeError(f"step cap {self.step_cap} reached with the epidemic still running "
                                   f"(converged: false; batch members {bad[:10]})")
            n = min(2 * steps, self.step_cap - done)
            sim = self._sim(ctx, state, n - 1, done + 1, seed)
            state, done, steps = self._run_until_done(sim, n - 1), done + n, n
        return state, sim

    def _run_until_done(self, sim, steps) -> dict:
        """Run one segment; in to_extinction mode stop early once every member has converged. Returns the
        module state (the sim holds its own copy of the module, so it is read from the sim)."""
        if self.mode != "to_extinction":
            sim.run()
            return sim.diseases.groupepidemic.export()
        sim.init()
        mod = sim.diseases.groupepidemic
        chunk = 32
        t = chunk - 1
        while True:
            sim.run(until=min(t, steps))
            if not mod.active.any() or t >= steps or sim.complete:
                return mod.export()
            t += chunk

    def run(self, params, unit: UnitSpec, package: Package, horizon: Horizon, seed: int) -> Outcomes:
        return self.run_batch([params], unit, [package], horizon, seed)[0]

    def run_batch(self, param_sets, unit, packages, horizon: Horizon, seed: int) -> list:
        """Evaluate many (parameter set, package) pairs in one sim (batch x groups). `unit` is one UnitSpec or
        one per pair; `packages` one Package or one per pair. Equal to run() pair by pair."""
        ctx = self._context(list(param_sets), unit, packages)
        state, sim = self._simulate(ctx, horizon, seed)
        return self._outcomes(ctx, state, horizon, seed)

    # ------------------------------------------------------------------------------ outputs
    def _p_major(self, ctx, s_start) -> np.ndarray:
        N, C, q = ctx["N"], ctx["C"], ctx["q"]
        f0 = ctx["forcing"](0.0)
        # K[b, j, i] = expected infections in j caused by one case in i = q f s_j C_ji N_j / N_i
        K = (q * f0)[:, None, None] * s_start[:, :, None] * C * N[:, :, None] / N[:, None, :]
        x = extinction_probability(K, self.offspring)
        return np.sum(ctx["pi"] * (1.0 - x), axis=1)

    def _outcomes(self, ctx, state, horizon, seed) -> list:
        B, G = ctx["B"], ctx["G"]
        N, C, q = ctx["N"], ctx["C"], ctx["q"]
        Ntot = N.sum(axis=1)
        inc = np.stack(state["inc"])                              # (T + 1, B, G); step 0 holds the seeds
        T1 = inc.shape[0]
        times = np.arange(T1) * self.dt_days
        s_start0 = state["s_first"]                               # after interventions, before seeding
        S_after = (s_start0 * N)[None] - np.cumsum(inc, axis=0)   # S after each step
        pm = self._p_major(ctx, s_start0)
        scale = pm if self.importation == "branching" else np.ones(B)
        fref = ctx["forcing"](self.forcing_ref_phase)
        r0 = q * spectral_radius(C)
        w0, w1 = float(horizon.window[0]), float(horizon.window[1])
        sel_dec = (times >= w0) & (times < w1)
        out = []
        for b in range(B):
            Sb_before = lambda k: (s_start0[b] * N[b]) if k <= 0 else S_after[k - 1, b]  # noqa: E731
            pc_inf = inc[sel_dec, b].sum(axis=0) / N[b] * scale[b]
            per_capita = {"infections": {g: float(pc_inf[i]) for i, g in enumerate(self.groups)}}
            for o, ratio in ctx["members"][b]["ratios"].items():
                per_capita[o] = {g: float(pc_inf[i] * ratio[i]) for i, g in enumerate(self.groups)}
            wins = {}
            for wn, (t0, t1) in (horizon.windows or {}).items():
                t0, t1 = float(t0), float(t1)
                sel = (times >= t0) & (times < t1)
                ks = np.nonzero(sel)[0]
                k0 = int(ks[0]) if len(ks) else int(np.searchsorted(times, t0))
                k_last = int(ks[-1]) if len(ks) else k0 - 1
                S0 = Sb_before(k0)
                S1 = S_after[min(k_last, T1 - 1), b] if k_last >= 0 else s_start0[b] * N[b]
                tot = inc[sel, b].sum(axis=0)
                f0, f1 = ctx["forcing"](t0)[b], ctx["forcing"](t1)[b]
                rho0 = float(spectral_radius((S0 / N[b])[:, None] * C[b]))
                rho1 = float(spectral_radius((S1 / N[b])[:, None] * C[b]))
                series = inc[sel, b].sum(axis=1)
                pos = series > 0
                if pos.sum() >= 2:
                    gr = float(np.polyfit(times[sel][pos], np.log(series[pos]), 1)[0])
                    gdef = 1.0
                else:
                    gr, gdef = 0.0, 0.0
                m = {"attack_rate": float(tot.sum() / Ntot[b] * scale[b]), "s_start": float(S0.sum() / Ntot[b]),
                     "s_end": float(S1.sum() / Ntot[b]), "r0": float(r0[b]), "reff_start": float(q[b] * f0 * rho0),
                     "reff_end": float(q[b] * f1 * rho1), "reff_ref": float(q[b] * fref[b] * rho1),
                     "growth_rate": gr, "growth_defined": gdef, "p_major": float(pm[b])}
                m.update({f"attack_rate:{g}": float(tot[i] / N[b, i] * scale[b]) for i, g in enumerate(self.groups)})
                if self.mode == "to_extinction":
                    m["converged"] = float(not state["active"][b])
                    m["steps"] = float(state["stopped"][b] if state["stopped"][b] >= 0 else T1 - 1)
                wins[wn] = m
            cost = {f"doses:{c}": float(state["doses"][c][b] / Ntot[b]) for c in self.components}
            meta = {"seed": int(seed), "dt_days": self.dt_days, "steps": int(T1 - 1),
                    "age_shares": {g: float(N[b, i] / Ntot[b]) for i, g in enumerate(self.groups)},
                    "s_initialiser": [float(x) for x in ctx["s_init"][b]], "s_start": [float(x) for x in s_start0[b]],
                    "incidence": [float(x) for x in inc[1:, b].sum(axis=1) / Ntot[b]],
                    "p_major_derived": True, "importation": self.importation,
                    "seed_clipped": bool(np.any(ctx["seed"][b] > s_start0[b] * N[b] + 1e-300))}
            if self.mode == "to_extinction":
                meta["converged"] = bool(not state["active"][b])
            if self.mode == "windowed" and self.rt_trajectory:
                s_traj = S_after[:, b] / N[b]
                f_t = np.asarray([ctx["forcing"](t)[b] for t in times])
                meta["rt"] = [float(x) for x in q[b] * f_t * spectral_radius(s_traj[:, :, None] * C[b][None])]
            out.append(Outcomes(per_capita=per_capita, windows=wins, cost_drivers=cost, meta=meta))
        return out


# ------------------------------------------------------------------------------------- card
def write_card(path: str) -> None:
    """card.yaml: the engine's configuration vocabulary, capabilities (base + by_config, design note §4),
    parameters, and the benchmark results it was validated with (sha256 of results.yaml)."""
    import hashlib
    import yaml
    from toolkit.engines import benchmark as B
    pack = yaml.safe_load(open(PACK))
    eng = B.load_engine(pack["engine"], dict(pack["configs"][pack["default_config"]], results_path=RESULTS))
    card = eng.describe().to_dict()
    card["configured_as"] = pack["default_config"]
    card["configuration_vocabulary"] = {
        "structure": list(STRUCTURES), "mode": list(MODES), "initialiser": list(INITIALISERS), "dt": list(DT_DAYS),
        "transmission": list(TRANSMISSION), "forcing": list(FORCING), "importation": list(IMPORTATION),
        "offspring": list(OFFSPRING), "dose_dependence": list(DEPENDENCE), "component_kinds": list(KINDS)}
    card["capabilities"] = {
        "base": {"outcome_role": {"infections": "prevented"},
                 "intervention_action": ["remove_susceptible_all_or_nothing"],
                 "estimands": ["r0", "reff_start", "attack_rate", "p_major"],
                 "calibration_data": ["group_attack_rate"]},
        "by_config": {
            "mode": {"to_extinction": {"dynamics": ["closed_epidemic_final_size"]},
                     "windowed": {"dynamics": ["closed_epidemic_renewal"],
                                  "estimands": ["reff_end", "growth_rate", "incidence_series"],
                                  "calibration_data": ["incidence_series"]}},
            "structure": {"single_group": {"structure": ["single_group"]},
                          "age_groups_contact_matrix": {"structure": ["age_groups_contact_matrix"]},
                          "birth_cohorts": {"structure": ["birth_cohorts"]}},
            "initialiser": {"given": {}, "cohort_history": {"calibration_data": ["survey_proportion", "coverage_report"]}},
            "components": {"doses": {"decision_coupling": ["joint_within_unit"]},
                           "campaign": {"decision_coupling": ["independent_units"]}}}}
    card["parameters"] = [vars(p) for p in eng.parameters()]
    card["validation_label"] = PUBLISHED_LABEL
    if os.path.exists(RESULTS):
        card["results_sha256"] = hashlib.sha256(open(RESULTS, "rb").read()).hexdigest()
    card["benchmark_pack"] = "benchmarks/pack.yaml (oracles: benchmarks/oracles.py, test only)"
    yaml.safe_dump(card, open(path, "w"), sort_keys=False, width=110)


FAST_PACK_IDS = ("FS1b", "FS4a", "BR1a", "CI1a", "CI4a", "RN2a", "RN2c", "OB1a")


def _self_test() -> int:
    import numpy as np
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def safe(fn):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001 -- a test that raises is a failing test
            return None, f"{type(e).__name__}: {e}"

    single = dict(groups=["all"], structure="single_group", mode="to_extinction", initialiser="given", dt="day",
                  generation_interval=[0.2, 0.3, 0.3, 0.2], components={"vax": {"kind": "campaign", "targets": ["all"],
                                                                              "reach": 0.5}})
    hx = Horizon(0, 200000, (0, 200000), {"all": [0, 200000]})
    u1 = UnitSpec("u", {"N": [1e6], "s0": [1.0], "seed": 1e-9})
    e, err = safe(lambda: CompartmentalEngine(**single))
    ok(e is not None, f"K0 the engine builds from a configuration: {err}")
    if e is None:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    ok(check_engine(e, u1, Package("v", {"vax": 1}), hx) == [], f"K1a contract (to_extinction, given, campaign): "
       f"{check_engine(e, u1, Package('v', {'vax': 1}), hx)}")
    win = dict(single, mode="windowed", forcing="cosine", forcing_period=365.0)
    ew = CompartmentalEngine(**win)
    hw = Horizon(0, 300, (0, 300), {"all": [0, 300], "late": [100, 300]})
    ok(check_engine(ew, u1, Package("0", {}), hw) == [], "K1b contract (windowed, forced)")
    coh = dict(groups=["b2000", "b2010", "b2011"], structure="birth_cohorts", mode="to_extinction",
               initialiser="cohort_history", doses=["d1", "d2"], dose_dependence="independent", dt="day",
               generation_interval=[0.5, 0.5], components={"cu": {"kind": "campaign", "targets": ["b2011"], "reach": 0.9}})
    ec = CompartmentalEngine(**coh)
    uc = UnitSpec("c", {"N": [3e5, 1e5, 1e5], "birth_year": [2000, 2010, 2011], "decision_year": 2025,
                        "always_immune": ["b2000"], "contact_rate": 8.0, "seed": 1e-9, "intro": "b2011",
                        "coverage:d1": [0.0, 0.9, 0.8], "coverage:d2": [0.0, 0.6, 0.0]})
    ok(check_engine(ec, uc, Package("c", {"cu": 1}), hx) == [], "K1c contract (cohort_history, birth cohorts)")
    mg = dict(groups=["a", "b", "c"], structure="age_groups_contact_matrix", mode="to_extinction", initialiser="given",
              dt="day", transmission="q", generation_interval=[0.5, 0.3, 0.2],
              components={"blk": {"kind": "doses", "group": "b", "block_size": 1e4, "max_blocks": 5}})
    em = CompartmentalEngine(**mg)
    um = UnitSpec("m", {"N": [3e5, 2e5, 1e5], "s0": [1.0, 0.9, 0.8], "seed": 1e-9, "intro": "a",
                        "contact": [[2.0, 0.5, 0.2], [0.6, 1.5, 0.4], [0.3, 0.5, 1.0]]})
    ok(check_engine(em, um, Package("b", {"blk": 3}), hx) == [], "K1d contract (contact matrix, dose blocks)")

    # K2: the module draws no random numbers, so outputs are identical across seeds
    outs = [e.run({"r0": 2.5}, u1, Package("v", {"vax": 1}), hx, s) for s in (1, 7, 99991)]
    ok(all(o.per_capita == outs[0].per_capita and o.windows == outs[0].windows and o.cost_drivers == outs[0].cost_drivers
           for o in outs) and [o.meta["seed"] for o in outs] == [1, 7, 99991],
       "K2 outputs are identical across seeds; the seed is recorded")
    ow = [ew.run({"r0": 1.6, "forcing_amplitude": 0.2}, u1, Package("0", {}), hw, s) for s in (1, 5)]
    ok(ow[0].windows == ow[1].windows, "K2b identical across seeds in windowed mode too")

    # K3: run_batch() equals run() pair by pair (rel 1e-12)
    rng = np.random.default_rng(3)
    ps = [{"r0": float(r), "efficacy:vax": float(ef)} for r, ef in zip(rng.uniform(1.2, 6, 9), rng.uniform(0.5, 1, 9))]
    pk = [Package(f"p{i}", {"vax": int(i % 2)}) for i in range(9)]
    ob = e.run_batch(ps, u1, pk, hx, 1)

    def rel(a, b):
        return abs(a - b) / max(abs(b), 1e-300)
    worst = 0.0
    for p, k, o in zip(ps, pk, ob):
        r = e.run(p, u1, k, hx, 1)
        worst = max([worst, rel(o.windows["all"]["attack_rate"], r.windows["all"]["attack_rate"]),
                     rel(o.per_capita["infections"]["all"], r.per_capita["infections"]["all"]),
                     rel(o.cost_drivers["doses:vax"] + 1, r.cost_drivers["doses:vax"] + 1)])
    ok(len(ob) == 9 and worst < 1e-12, f"K3a run_batch() equals run() pair by pair, to_extinction (worst rel {worst:.1e})")
    psw = [{"r0": float(r), "forcing_amplitude": float(a), "forcing_phase": float(ph)}
           for r, a, ph in zip(rng.uniform(1.1, 3, 6), rng.uniform(0, 0.4, 6), rng.uniform(0, 365, 6))]
    obw = ew.run_batch(psw, u1, Package("0", {}), hw, 1)
    worst = max(rel(o.windows[w][m], ew.run(p, u1, Package("0", {}), hw, 1).windows[w][m])
                for p, o in zip(psw, obw) for w in ("all", "late") for m in ("attack_rate", "reff_end", "growth_rate"))
    ok(worst < 1e-12, f"K3b run_batch() equals run(), windowed with per-member forcing (worst rel {worst:.1e})")
    obm = em.run_batch([{"q": 0.9}, {"q": 1.3}], [um, UnitSpec("m2", dict(um.attributes, s0=[0.7, 0.7, 0.7]))],
                       [Package("0", {}), Package("b", {"blk": 2})], hx, 1)
    rm = [em.run({"q": 0.9}, um, Package("0", {}), hx, 1),
          em.run({"q": 1.3}, UnitSpec("m2", dict(um.attributes, s0=[0.7, 0.7, 0.7])), Package("b", {"blk": 2}), hx, 1)]
    ok(all(rel(a.per_capita["infections"][g], b.per_capita["infections"][g]) < 1e-12 for a, b in zip(obm, rm)
           for g in ("a", "b", "c")), "K3c run_batch() with one unit per pair (latent unit attributes vary)")

    # K4: the generation interval fails closed
    for bad, why in (([0.5, 0.4], "sum"), ([0.6, -0.1, 0.5], "negative"), ([0.5, None, 0.5], "gap"),
                     ([0.5, float("nan"), 0.5], "gap"), ({"dt": "week", "w": [1.0]}, "grid")):
        _, err = safe(lambda: CompartmentalEngine(**dict(single, generation_interval=bad)))
        ok(err is not None and "ValueError" in err, f"K4 a generation interval with a {why} fails closed: {err}")
    _, err = safe(lambda: CompartmentalEngine(**dict(single, generation_interval={"dt": "day", "w": [0.5, 0.5]})))
    ok(err is None, "K4b a generation interval on the engine's own grid is accepted")

    # K5: the step cap fails closed
    capped = CompartmentalEngine(**dict(single, step_cap=30, segment=16))
    _, err = safe(lambda: capped.run({"r0": 2.0}, u1, Package("0", {}), hx, 1))
    ok(err is not None and "converged: false" in err, f"K5 an epidemic still running at the step cap fails closed: {err}")

    # K6: unknown configuration fails closed
    for bad in ({"mode": "both"}, {"structure": "network"}, {"initialiser": "survey"}, {"no_such_field": 1},
                {"importation": "maybe"}, {"offspring": "negbin"}, {"dt": "month"}):
        _, err = safe(lambda: CompartmentalEngine(**dict(single, **bad)))
        ok(err is not None, f"K6 configuration {bad} is refused: {err}")
    _, err = safe(lambda: e.run({}, UnitSpec("x", {"N": [1e6], "s0": [1.2], "seed": 1e-9}), Package("0", {}), hx, 1))
    ok(err is not None, "K6b a susceptible share outside [0, 1] is refused")

    # K7: homogeneous mixing across unequal cohorts is population-weighted, not a matrix of ones
    C = np.asarray(ec.contact_matrix(uc))
    Nc = np.asarray(uc.attributes["N"])
    ok(np.allclose(C, 8.0 * np.tile(Nc / Nc.sum(), (3, 1)), rtol=0, atol=1e-15)
       and abs(ec.run({"r0": 3.0}, uc, Package("0", {}), hx, 1).windows["all"]["r0"] - 3.0) < 1e-12,
       "K7 birth cohorts mix with C_ij = c N_j / N; R0 = q rho(C)")

    # K8: Starsim structure
    import starsim as ss
    sim = e.build_sim({"r0": 2.0}, u1, Package("v", {"vax": 1}), hx, 3)[0]
    mods = [m for m in sim.pars.diseases] if isinstance(sim.pars.diseases, list) else [sim.pars.diseases]
    itvs = sim.pars.interventions if isinstance(sim.pars.interventions, list) else [sim.pars.interventions]
    ok(isinstance(sim, ss.Sim) and any(isinstance(m, ss.Module) and type(m).__name__ == "GroupEpidemic" for m in mods)
       and all(isinstance(i, ss.Intervention) for i in itvs) and len(itvs) == 1,
       "K8 run() builds an ss.Sim with the GroupEpidemic module and start-of-sim ss.Intervention components")
    o = e.run({"r0": 3.0}, u1, Package("v", {"vax": 1}), hx, 1)       # Reff 1.5 (R0 2 with reach 0.5 is critical)
    ok(o.meta.get("p_major_derived") is True and any("p_major" in str(x) and "derived" in str(x)
                                                     for x in e.describe().notes + e.describe().mechanisms),
       "K9 p_major is labelled derived (an equation, not the sim) on the card and in the outcomes")

    # K10: dose blocks and targets_susceptible
    st = em.initial_state({"q": 1.0, "efficacy:blk": 1.0}, um, Package("b", {"blk": 3}))
    ok(abs(st["s_start"][1] - 0.9 * (1 - 3e4 / 2e5)) < 1e-12 and abs(st["doses"]["blk"] - 3e4) < 1e-9,
       "K10a dose blocks: coverage = blocks x block / N_g; doses given to immune people are wasted")
    emt = CompartmentalEngine(**dict(mg, targets_susceptible=True))
    st2 = emt.initial_state({"q": 1.0, "efficacy:blk": 1.0}, um, Package("b", {"blk": 3}))
    ok(abs(st2["s_start"][1] - (0.9 - 3e4 / 2e5)) < 1e-12, "K10b targets_susceptible: every dose reaches a susceptible")

    # K11: fast analytic subset of the pack, through the runner
    import yaml
    from toolkit.engines import benchmark as B
    pack = yaml.safe_load(open(PACK))
    sub = dict(pack, benchmarks=[b for b in pack["benchmarks"] if b["id"] in FAST_PACK_IDS])
    eng0 = B.load_engine(pack["engine"], pack["configs"][pack.get("default_config") or "single_day"])
    ok(B.validate(pack, eng0) == [], f"K11a the pack validates: {B.validate(pack, eng0)[:3]}")
    rows = {r["id"]: r for r in B.run_pack(sub, eng0, B.Runner(sub, eng0, 1, seeds=[1]), {})["benchmarks"]}
    ok(all(rows[i]["status"] == "in_band" for i in FAST_PACK_IDS),
       "K11b fast pack subset in band: " + ", ".join(f"{i} {rows[i]['status']}" for i in FAST_PACK_IDS))

    # K12: the committed results were computed by the runner for this code, and every assessable benchmark is in band
    eng = B.load_engine(pack["engine"], dict(pack["configs"][pack["default_config"]], results_path=RESULTS))
    got, why = B.verified_benchmarks(eng)
    ok(why is None and len(got) == len(pack["benchmarks"]), f"K12a results.yaml is stamped for this engine code: {why}")
    bad = [r["id"] for r in got if r["status"] != "in_band" and r["id"] not in ("FS7", "RN5")]
    na = {r["id"]: r["status"] for r in got if r["id"] in ("FS7", "RN5")}
    ok(got and not bad and na == {"FS7": "not_assessable", "RN5": "not_assessable"},
       f"K12b every assessable benchmark in band ({bad}); FS7 and RN5 not_assessable until transcribed ({na})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if sys.argv[1:2] == ["--card"]:
        write_card(sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "card.yaml"))
        sys.exit(0)
    print(__doc__)
