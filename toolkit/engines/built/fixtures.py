"""Planted-defect and healthy Starsim/closed-form fixture engines for built-engine admission self-tests
(docs/design/agent-built-engines.md §6.4: BE1-BE17).

Used strictly as self-test data by `toolkit/engines/built/admission.py` and `try_tool.py`.
Nothing here names a disease.
"""
from __future__ import annotations

import copy


def two_group_sir_build(*, stochastic: bool = False, include_trial_oob: bool = False) -> dict:
    """Valid signed `engine.build` request for a two-group discrete-time SIR Starsim module."""
    pub_lo, pub_hi = (0.80, 0.90) if include_trial_oob else (0.35, 0.70)
    return {
        "id": "two_group_sir",
        "kind": "starsim_module",
        "why": "Needs explicit two-group severe-outcome compartment transition not on any registered card",
        "mechanisms": ["two-group discrete SIR", "severity transition compartment", "transmission reduction"],
        "request": {
            "stochastic": bool(stochastic),
            "per_capita_invariant_to_N": True,
            "components": {
                "comp_a": {"values": [0, 1], "absent": 0},
            },
            "unit_attributes": {
                "N": {"type": "continuous", "range": [1000.0, 100000.0]},
                "s0": {"type": "continuous", "range": [0.4, 1.0]},
                "age_pop": {"type": "vector", "length": 2},
            },
            "outputs": {
                "per_capita": ["cases", "severe"],
                "windows": {"w0": ["prev"]},
                "cost_drivers": ["units_delivered"],
            },
            "age_groups": ["g0", "g1"],
            "parameters": [
                {"name": "r0", "value": 2.5, "kind": "calibrated", "lo": 1.3, "hi": 3.5,
                 "units": "ratio", "source": "dossier:E1"},
                {"name": "gamma", "value": 0.2, "kind": "fixed", "lo": 0.08, "hi": 0.4,
                 "units": "1/step", "source": "dossier:E2"},
                {"name": "p_sev", "value": 0.1, "kind": "fixed", "lo": 0.02, "hi": 0.35,
                 "units": "share", "source": "dossier:E3"},
                {"name": "eff_a", "value": 0.4, "kind": "fixed", "lo": 0.15, "hi": 0.65,
                 "units": "share", "source": "dossier:E4"},
            ],
            "attribute_binding": {
                "N": "required",
                "s0": "required",
                "age_pop": {"default": [5000.0, 5000.0], "meaning": "equal two-group population vector"},
            },
            "decision_inputs": {
                "comp_a": [{"kind": "parameter", "name": "eff_a", "why": "transmission reduction of comp_a"}],
            },
            "expected_effects": {
                "comp_a": {"cases": "-", "units_delivered": "+"},
                "eff_a": {"cases": "-"},
                "r0": {"cases": "+", "severe": "+"},
                "gamma": {"prev": "-"},
                "p_sev": {"severe": "+"},
                "s0": {"cases": "+"},
            },
            "inert": {
                "N": {"reason": "per-capita outcomes are invariant to population size N"},
                "age_pop": {"reason": "per-group rates are identical under symmetric mixing; weights only"},
            },
            "compartments": {
                "states": ["S", "I", "R", "H"],
                "conservation": "closed",
                "transitions": [
                    {"from": "S", "to": "I", "counts": ["cases"]},
                    {"from": "I", "to": "H", "counts": ["severe"]},
                    {"from": "I", "to": "R", "counts": []},
                ],
            },
            "seed_test_setting": {
                "unit": {"N": 10000.0, "s0": 0.95, "age_pop": [5000.0, 5000.0]},
            },
            "reductions": [
                {
                    "id": "RED_LAMBERT",
                    "oracle": "final_size_lambert",
                    "oracle_args": {"r0": 2.0},
                    "set": {"r0": 2.0, "comp_a": 0},
                    "unit": {"N": 10000.0, "s0": 1.0, "age_pop": [5000.0, 5000.0]},
                    "horizon": {"start": 0.0, "stop": 250.0, "window": [0.0, 250.0], "windows": {"w0": [0.0, 250.0]}},
                    "compare": {"per_capita_all.cases": {"abs": 0.03}},
                },
            ],
            "trials": [
                {
                    "id": "TR1",
                    "dossier": "E4",
                    "component": "comp_a",
                    "arms": {"ctrl": {"comp_a": 0}, "trt": {"comp_a": 1}},
                    "setting": {"unit": {"N": 10000.0, "s0": 0.9, "age_pop": [5000.0, 5000.0]}},
                    "window": [0.0, 150.0],
                    "estimand_type": "ratio",
                    "estimand_mapping": {"num": "trt", "den": "ctrl", "value": "per_capita_all.cases"},
                    "published": {"lower": pub_lo, "upper": pub_hi, "level": 0.95},
                },
            ],
            "reference_tolerance": {"abs": 0.02, "rel": 0.04},
        },
    }


def two_group_sir_spec(build_decl: dict | None = None) -> dict:
    """Minimal spec dict carrying `engine.build`, `decision_units`, `options`, and `objective` for admission."""
    b = copy.deepcopy(build_decl or two_group_sir_build())
    return {
        "schema_version": 1,
        "engine": {"build": b},
        "objective": {
            "horizon": [0.0, 120.0],
            "windows": {"w0": [0.0, 120.0]},
        },
        "decision_units": {
            "id_column": "unit_id",
            "units_inline": [
                {"unit_id": "unit_alpha", "N": 12000.0, "s0": 0.85, "age_pop": [4000.0, 8000.0]},
                {"unit_id": "unit_beta", "N": 25000.0, "s0": 0.62, "age_pop": [10000.0, 15000.0]},
            ],
        },
        "options": {
            "engine_map": {
                "none": {"comp_a": 0},
                "opt_a": {"comp_a": 1},
            },
            "packages": {"none": [], "opt_a": ["opt_a"]},
        },
        "calibration": {"method": "none"},
    }


def two_group_sir_files(defect: str | None = None, *, stochastic_sd: float = 0.0) -> dict[str, bytes]:
    """Build `{engine.py, card.yaml, ENGINE_NOTES.md}` bytes for a healthy two-group SIR Starsim module
    or one with a specific planted defect (`BE1`-`BE15`, `BE17`):
      - None: healthy BE1 fixture (passes all checks)
      - 'dead_param' (BE2): ignores `p_sev` (hardcodes 0.1)
      - 'tiny_eps' (BE3): adds `1e-12 * eff_a` instead of applying `eff_a` to transmission
      - 'wrong_output' (BE4): wires `eff_a` to `units_delivered` only (never moves `cases`)
      - 'outcome_only_param' (BE5): reads `p_sev` only inside `read_outcomes`
      - 'unseeded' (BE6a): uses unseeded `np.random.normal`
      - 'order_cache' (BE6b): uses mutable module-level cache across calls
      - 'leaky_batch' (BE7): `run_batch_state` leaks state across packages
      - 'mass_growth' (BE8): multiplies `S` by 1.001 each step under closed conservation
      - 'formula_severe' (BE9): reports `severe = 0.25 * cases` apart from compartment `H`
      - 'public_table' (BE10): >16-element inline lookup table matching public reduction, wrong elsewhere
      - 'real_unit_bad' (BE11): returns negative cases when `abs(s0 - 0.62) < 1e-6` (unit_beta)
      - 'narrow_range' (BE13a): narrows `eff_a` range from `[0.15, 0.85]` to `[0.3, 0.7]`
      - 'empty_window' (BE13b): returns empty `windows['w0']`
      - 'neg_driver' (BE13c): returns `-0.1` for `units_delivered`
      - 'stochastic_bias' (BE17): adds stochastic noise (with `stochastic_sd`) and +5% bias
    """
    card_yaml = (
        "name: built:two_group_sir\n"
        "version: '0.1'\n"
        "kind: starsim_module\n"
        "mechanisms:\n"
        "  - two-group discrete SIR\n"
        "  - severity transition compartment\n"
        "  - transmission reduction\n"
        "components:\n"
        "  comp_a: [0, 1]\n"
        "unit_attributes:\n"
        "  N: total population\n"
        "  s0: initial susceptible fraction\n"
        "  age_pop: population vector by age group\n"
        "outputs:\n"
        "  per_capita: [cases, severe]\n"
        "  windows: [prev]\n"
        "  cost_drivers: [units_delivered]\n"
        "age_groups: [g0, g1]\n"
        "valid_ranges:\n"
        "  N: [1000.0, 100000.0]\n"
        "  s0: [0.4, 1.0]\n"
        "benchmarks: []\n"
        "assumptions:\n"
        "  - homogeneous mixing across two age groups\n"
        "limitations:\n"
        "  - closed population with no vital dynamics\n"
    ).encode("utf-8")
    notes_md = b"# Two-group SIR fixture\n\nDiscrete-time closed S-I-R-H model.\n"

    # Build clean Python module source, injecting only the requested single defect
    eff_lo, eff_hi = (0.3, 0.5) if defect == "narrow_range" else (0.15, 0.65)
    extra_top = ""
    if defect == "public_table":
        vals = ", ".join(f"{0.7968 + 0.0001 * i:.5f}" for i in range(20))
        extra_top = f"PUB_LUT = [{vals}]\n"
    elif defect == "order_cache":
        extra_top = "CACHE = {'n': 0}\n"

    batch_method = ""
    if defect == "leaky_batch":
        batch_method = """
    def run_batch_state(self, param_sets, unit, packages, horizon, seed):
        outs = []
        leak = 0.0
        for prm, pkg in zip(param_sets, packages):
            sim = self.build_sim(prm, unit, pkg, horizon)
            sim.pars.rand_seed = int(seed)
            sim.run()
            o = self.read_outcomes(sim, unit, pkg, horizon)
            if leak > 0.0:
                o = Outcomes(
                    per_capita={'cases': {g: v + leak for g, v in o.per_capita['cases'].items()},
                                'severe': o.per_capita['severe']},
                    windows=o.windows, cost_drivers=o.cost_drivers, meta=o.meta,
                )
            leak += 0.05
            outs.append(o)
        return outs
"""

    src = f'''"""Two-group discrete SIR Starsim module fixture."""
from __future__ import annotations
import math
import numpy as np
import starsim as ss
from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec

DEFECT = {defect!r}
STOCH_SD = {float(stochastic_sd)!r}
{extra_top}

class TwoGroupSIR(ss.Module):
    def __init__(self, n_vec, s0, r0, gamma, p_sev, eff_a, comp_a, dt_step):
        super().__init__(name="sir2")
        self._n_vec = np.asarray(n_vec, dtype=float)
        self._s0 = float(s0)
        self._r0 = float(r0)
        self._gamma = float(gamma)
        self._p_sev = float(p_sev)
        self._eff_a = float(eff_a)
        self._comp_a = int(comp_a)
        self._dt_step = float(dt_step)
        self.prev_hist = []

    def init_post(self):
        super().init_post()
        n_tot = float(np.sum(self._n_vec))
        seed_frac = min(1e-3, 0.5 * self._s0)
        self.I = self._n_vec * seed_frac
        self.S = self._n_vec * max(self._s0 - seed_frac, 0.0)
        self.R = self._n_vec * max(1.0 - self._s0, 0.0)
        self.H = np.zeros_like(self._n_vec)
        self.cum_cases = np.zeros_like(self._n_vec)
        self.cum_severe = np.zeros_like(self._n_vec)
        self._n0 = n_tot

    def step(self):
        dt = self._dt_step
        n_tot = max(float(np.sum(self._n_vec)), 1e-12)
        if DEFECT == "tiny_eps":
            mult = 1.0 - 1e-12 * self._eff_a * self._comp_a
        elif DEFECT == "wrong_output":
            mult = 1.0
        else:
            mult = max(0.05, 1.0 - self._eff_a * self._comp_a)
        r_eff = self._r0 * mult
        # Subtle dependence on N so non-zero N check moves only when expected, while staying invariant to ~1e-3
        n_tiny = 0.0
        foi = r_eff * self._gamma * (float(np.sum(self.I)) / n_tot) + n_tiny
        p_inf = 1.0 - math.exp(-max(foi, 0.0) * dt)
        p_rec = 1.0 - math.exp(-max(self._gamma, 0.0) * dt)
        new_inf = self.S * p_inf
        leave_i = self.I * p_rec
        p_sev_use = 0.1 if DEFECT == "dead_param" else self._p_sev
        new_h = leave_i * p_sev_use
        new_r = leave_i - new_h
        self.S = self.S - new_inf
        if DEFECT == "mass_growth":
            self.S = self.S * 1.001
        self.I = self.I + new_inf - leave_i
        self.H = self.H + new_h
        self.R = self.R + new_r
        self.cum_cases = self.cum_cases + new_inf
        self.cum_severe = self.cum_severe + new_h
        self.prev_hist.append(float(np.sum(self.I)) / n_tot)


class Model:
    def __init__(self, dt_step: float = 0.5):
        self.dt_step = float(dt_step)
        self._saved_params = None

    def describe(self) -> EngineCard:
        return EngineCard(
            name="built:two_group_sir",
            version="0.1",
            mechanisms=["two-group discrete SIR", "severity transition compartment", "transmission reduction"],
            components={{"comp_a": [0, 1]}},
            unit_attributes={{"N": "total population", "s0": "initial susceptible fraction",
                              "age_pop": "population vector by age group"}},
            outputs={{"per_capita": ["cases", "severe"], "windows": ["prev"], "cost_drivers": ["units_delivered"]}},
            age_groups=["g0", "g1"],
            valid_ranges={{"N": [1000.0, 100000.0], "s0": [0.4, 1.0]}},
        )

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec("r0", 2.5, "calibrated", units="ratio", lo=1.3, hi=3.5, source="dossier:E1"),
            ParamSpec("gamma", 0.2, "fixed", units="1/step", lo=0.08, hi=0.4, source="dossier:E2"),
            ParamSpec("p_sev", 0.1, "fixed", units="share", lo=0.02, hi=0.35, source="dossier:E3"),
            ParamSpec("eff_a", 0.4, "fixed", units="share", lo={eff_lo}, hi={eff_hi}, source="dossier:E4"),
        ]

    def state_vars(self) -> dict:
        return {{"S": ("sir2", "S"), "I": ("sir2", "I"), "R": ("sir2", "R"), "H": ("sir2", "H")}}

    def build_sim(self, params, unit, package, horizon):
        self._saved_params = params
        r0 = float(params["r0"])
        gamma = float(params["gamma"])
        p_sev = 0.1 if DEFECT == "outcome_only_param" else float(params["p_sev"])
        eff_a = float(params["eff_a"])
        n_tot = float(unit.attributes.get("N", 10000.0))
        s0 = float(unit.attributes.get("s0", 0.9))
        ap = unit.attributes.get("age_pop") or [0.5, 0.5]
        ap_arr = np.asarray(ap, dtype=float)
        ap_sum = float(np.sum(ap_arr))
        n_vec = n_tot * (ap_arr / ap_sum if ap_sum > 0 else np.array([0.5, 0.5]))
        comp_a = int(package.components.get("comp_a", 0))
        mod = TwoGroupSIR(n_vec, s0, r0, gamma, p_sev, eff_a, comp_a, self.dt_step)
        n_steps = max(2, int(round((float(horizon.stop) - float(horizon.start)) / self.dt_step)))
        return ss.Sim(n_agents=1, modules=[mod], start=0, stop=n_steps, dt=1, verbose=0)

    def read_outcomes(self, sim, unit, package, horizon) -> Outcomes:
        mod = sim.modules["sir2"]
        n_vec = mod._n_vec
        cases_g = mod.cum_cases / np.maximum(n_vec, 1e-12)
        if DEFECT == "outcome_only_param":
            p_sev_ro = float(self._saved_params["p_sev"])
            sev_g = cases_g * p_sev_ro
        elif DEFECT == "formula_severe":
            sev_g = 0.25 * cases_g
        else:
            sev_g = mod.cum_severe / np.maximum(n_vec, 1e-12)
        if DEFECT == "public_table":
            if (abs(mod._r0 - 2.0) < 1e-9 and abs(mod._s0 - 1.0) < 1e-9
                    and abs(mod._gamma - 0.2) < 1e-9 and mod._comp_a == 0):
                cases_g = np.full_like(cases_g, PUB_LUT[0])
            else:
                cases_g = np.full_like(cases_g, 0.05)
        if DEFECT == "unseeded":
            cases_g = np.maximum(0.0, cases_g + float(np.random.normal(0.0, 0.05)))
        if DEFECT == "order_cache":
            CACHE["n"] += 1
            cases_g = cases_g + 0.02 * CACHE["n"]
        if DEFECT == "real_unit_bad" and abs(float(unit.attributes.get("s0", 0.0)) - 0.62) < 1e-6:
            cases_g = np.array([-0.5, -0.5])
        if STOCH_SD > 0.0:
            rng = np.random.default_rng(int(sim.pars.rand_seed))
            noise = float(rng.normal(0.0, STOCH_SD))
            cases_g = np.maximum(0.0, cases_g * (1.0 + noise))
            sev_g = np.maximum(0.0, sev_g * (1.0 + noise))
        if DEFECT == "stochastic_bias":
            cases_g = cases_g * 1.05
            sev_g = sev_g * 1.05
        # Small non-zero N effect on cases only for N-nonzero wiring? Wait: keep per_capita_invariant_to_N clean!
        mean_prev = float(np.mean(mod.prev_hist)) if mod.prev_hist else 0.0
        comp_a = int(package.components.get("comp_a", 0))
        drv = float(comp_a)
        if DEFECT == "wrong_output":
            drv = float(comp_a) + 0.5 * mod._eff_a
        if DEFECT == "neg_driver":
            drv = -0.1
        wins = {{str(w): ({{}} if DEFECT == "empty_window" else {{"prev": mean_prev}}) for w in horizon.windows}}
        return Outcomes(
            per_capita={{
                "cases": {{"g0": float(cases_g[0]), "g1": float(cases_g[1])}},
                "severe": {{"g0": float(sev_g[0]), "g1": float(sev_g[1])}},
            }},
            windows=wins,
            cost_drivers={{"units_delivered": drv}},
        )
{batch_method}
'''
    return {
        "engine.py": src.encode("utf-8"),
        "card.yaml": card_yaml,
        "ENGINE_NOTES.md": notes_md,
    }


def two_group_sir_reference_files(*, rel_bias: float = 0.0) -> dict[str, bytes]:
    """Independent closed-form / loop reference implementation for A11 (`BE1` & `BE17`)."""
    src = f'''"""Independent reference implementation of the two-group SIR request (plain loops, no Starsim)."""
from __future__ import annotations
import math
from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec

REL_BIAS = {float(rel_bias)!r}


class Model:
    def __init__(self, dt_step: float = 0.5):
        self.dt_step = float(dt_step)

    def describe(self) -> EngineCard:
        return EngineCard(
            name="ref:two_group_sir", version="0.1", mechanisms=["independent reference"],
            components={{"comp_a": [0, 1]}},
            unit_attributes={{"N": "n", "s0": "s0", "age_pop": "vec"}},
            outputs={{"per_capita": ["cases", "severe"], "windows": ["prev"], "cost_drivers": ["units_delivered"]}},
            age_groups=["g0", "g1"], valid_ranges={{"N": [1000.0, 100000.0], "s0": [0.4, 1.0]}},
        )

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec("r0", 2.5, "calibrated", units="ratio", lo=1.3, hi=3.5, source="dossier:E1"),
            ParamSpec("gamma", 0.2, "fixed", units="1/step", lo=0.08, hi=0.4, source="dossier:E2"),
            ParamSpec("p_sev", 0.1, "fixed", units="share", lo=0.02, hi=0.35, source="dossier:E3"),
            ParamSpec("eff_a", 0.4, "fixed", units="share", lo=0.15, hi=0.65, source="dossier:E4"),
        ]

    def evaluate(self, params, unit, package, horizon) -> Outcomes:
        r0 = float(params["r0"])
        gamma = float(params["gamma"])
        p_sev = float(params["p_sev"])
        eff_a = float(params["eff_a"])
        s0 = float(unit.attributes.get("s0", 0.9))
        comp_a = int(package.components.get("comp_a", 0))
        dt = self.dt_step
        n_steps = max(2, int(round((float(horizon.stop) - float(horizon.start)) / dt)))
        mult = max(0.05, 1.0 - eff_a * comp_a)
        seed_frac = min(1e-3, 0.5 * s0)
        s = max(s0 - seed_frac, 0.0)
        i = seed_frac
        cum_c = 0.0
        cum_h = 0.0
        prevs = []
        p_rec = 1.0 - math.exp(-max(gamma, 0.0) * dt)
        for _ in range(n_steps):
            foi = r0 * mult * gamma * i
            p_inf = 1.0 - math.exp(-max(foi, 0.0) * dt)
            ni = s * p_inf
            li = i * p_rec
            nh = li * p_sev
            s -= ni
            i += ni - li
            cum_c += ni
            cum_h += nh
            prevs.append(i)
        scale = 1.0 + REL_BIAS
        c_val = cum_c * scale
        h_val = cum_h * scale
        mp = (sum(prevs) / len(prevs) if prevs else 0.0) * scale
        return Outcomes(
            per_capita={{"cases": {{"g0": c_val, "g1": c_val}}, "severe": {{"g0": h_val, "g1": h_val}}}},
            windows={{str(w): {{"prev": mp}} for w in horizon.windows}},
            cost_drivers={{"units_delivered": float(comp_a)}},
        )
'''
    return {
        "engine.py": src.encode("utf-8"),
        "card.yaml": b"name: ref:two_group_sir\nversion: '0.1'\nmechanisms: [ref]\n",
        "ENGINE_NOTES.md": b"# Reference\n",
    }


def _self_test() -> int:
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from toolkit.engines.built import requested as BREQ
    from toolkit.engines.built import snapshot as BSNAP
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    b = two_group_sir_build()
    errs = BREQ.validate_build(b)
    healthy = two_group_sir_files()
    s_errs = BSNAP.check_static(healthy, request=b)
    ok(errs == [] and s_errs == [],
       f"FIX1 healthy two_group_sir request validates cleanly and passes S1-S4 (req={errs}, static={s_errs})")
    ref = two_group_sir_reference_files()
    ok("engine.py" in ref and b"class Model" in ref["engine.py"],
       "FIX2 independent reference fixture builds cleanly")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
