#!/usr/bin/env python3
"""Optional engine hooks for figures: the delivery schedule and monthly series (v2 figures design note §5).

    python3 toolkit/engines/hooks.py --self-test

`toolkit/engines/contract.py` is part of every engine's code fingerprint, so a new method there would make every
engine's benchmark stamps stale. These hooks are optional and live here instead. An engine offers a hook by defining
the method; the harness calls it only through the functions below, which check the engine's answer and fail closed
(`HookUnsupported` when the engine does not offer the hook, `HookInvalid` when its answer is malformed). Neither hook
may change what `Engine.run` / `run_batch` return: an engine that adds them keeps every existing run byte-identical.

Hook 1, the schedule (pure: no simulation, no random numbers):

    engine.schedule(params: dict, unit: UnitSpec, package: Package, horizon: Horizon) -> list[dict]

    One dict per delivery event of the unit in the arm `package`, over [horizon.start, horizon.window[1]):
        component  str          a key of EngineCard.components (the engine's component, not the spec's)
        label      str          what is delivered, in the engine's words (e.g. "campaign", "round")
        kind       str          "history" (before the delivery period, every arm), "decision" (in the delivery
                                period, paid by the decision; only when the package has the component) or
                                "routine" (outside the delivery period, every arm, not paid by the decision)
        t0         float        decimal year the event starts
        t1         float|None   decimal year it ends (None for a point event)
        charged    bool         paid by the decision (true exactly for kind "decision")
    The engine computes it with the same code that configures its simulation, so the schedule cannot drift from what
    the simulation delivers.

Hook 2, monthly series (a separate method, so `run` / `run_batch` are untouched):

    EngineCard.outputs["series"] = {metric: {"label": str, "units": str, "aggregate": "mean" | "rate"}}
    engine.run_series(params: dict, unit: UnitSpec, packages: list[Package], horizon: Horizon, seed: int,
                      metrics: list[str], step: str = "month") -> list[dict]

    One dict per package, in the order given: {"package": Package.id, "t": [decimal year at the start of each step,
    from horizon.start to horizon.window[1]], "values": {metric: [one value per step, per person]}}. "mean" is the
    step mean of a state (a prevalence); "rate" is events per person-year over the step (an incidence). Metrics must
    be listed in the card's outputs["series"]. Packages share their history as in run_batch (common random numbers).

Contract version `figures-hooks/1` (HOOKS_CONTRACT): an engine opts in with `hooks_contract = HOOKS_CONTRACT`; without
it neither hook is offered, and another version is refused. Under version 1:
  grid        run_series returns exactly the monthly grid t[k] = horizon.start + k/12 (within GRID_TOL) through the
              outcome window's end, identical for every arm (HookInvalid otherwise)
  metrics     each card entry gives label, units, aggregate (mean | rate) and denominator (e.g. person, person-year):
              values are per member of the unit's simulated population, and the harness weights them by the units
              table's populations
  schedule    every event lies in [horizon.start, window end]; decision events lie in the delivery period and only
              for components the package sets; history events end before the delivery period; routine events lie
              outside it; charged is true exactly for decision events

Nothing here names a disease, component or metric.
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Contract version of these hooks (grids, metric units and denominators, schedule semantics). An engine opts in by
# declaring `hooks_contract = HOOKS_CONTRACT`; another value is refused.
HOOKS_CONTRACT = "figures-hooks/1"
KINDS = ("history", "decision", "routine")
AGGREGATES = ("mean", "rate")
STEPS = ("month",)


class HookUnsupported(RuntimeError):
    """The engine does not offer the hook (the figure that needs it is recorded as not produced, with this text)."""

    def __init__(self, hook: str, engine):
        card = engine.describe()
        self.hook = hook
        super().__init__(f"engine {card.name} v{card.version} does not offer {hook}() (toolkit/engines/hooks.py)")


class HookInvalid(ValueError):
    """The engine's answer does not have the documented shape."""


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


GRID_TOL = 1e-6                 # years: a monthly step start may differ from horizon.start + k/12 by this much
META_KEYS = ("label", "units", "aggregate", "denominator")


def _contract(engine):
    """The engine's declared hooks contract (None when it declares none); another version is refused."""
    v = getattr(engine, "hooks_contract", None)
    if v is not None and v != HOOKS_CONTRACT:
        raise HookInvalid(f"engine declares hooks contract {v!r}; this harness reads {HOOKS_CONTRACT!r}")
    return v


def supports(engine, hook: str) -> bool:
    if hook not in ("schedule", "series"):
        raise ValueError(f"unknown hook {hook!r}")
    if getattr(engine, "hooks_contract", None) is None:
        return False
    if hook == "schedule":
        return callable(getattr(engine, "schedule", None))
    return callable(getattr(engine, "run_series", None)) and bool(series_metrics(engine))


def series_metrics(engine) -> dict:
    """{metric: {label, units, aggregate, denominator}} from the card; {} when the engine declares none."""
    s = (engine.describe().outputs or {}).get("series") or {}
    return dict(s) if isinstance(s, dict) else {}


def _unsupported(hook, engine):
    _contract(engine)
    if getattr(engine, "hooks_contract", None) is None and callable(getattr(engine, hook, None)):
        e = HookUnsupported(hook, engine)
        e.args = (f"{e.args[0]}: it declares no hooks contract (hooks_contract = {HOOKS_CONTRACT!r})",)
        return e
    return HookUnsupported(hook, engine)


def monthly_grid(horizon) -> list[float]:
    """The contract's grid: monthly step starts from horizon.start through the outcome window's end."""
    n = int(round((float(horizon.window[1]) - float(horizon.start)) * 12))
    return [float(horizon.start) + k / 12 for k in range(n)]


def schedule(engine, params, unit, package, horizon) -> list[dict]:
    """The engine's delivery schedule for one unit and arm, checked (fail closed)."""
    _contract(engine)
    if not supports(engine, "schedule"):
        raise _unsupported("schedule", engine)
    from toolkit.engines.horizon import delivery_period
    ev = engine.schedule(params, unit, package, horizon)
    comps = set(engine.describe().components)
    h0, w1 = float(horizon.start), float(horizon.window[1])
    d0, d1 = delivery_period(horizon)
    has = {c for c, v in (package.components or {}).items() if v not in (0, None, "none", False)}
    if not isinstance(ev, list):
        raise HookInvalid(f"schedule() returned {type(ev).__name__}, not a list")
    out, bad = [], []
    for n, e in enumerate(ev):
        if not isinstance(e, dict):
            bad.append(f"event {n}: not a mapping")
            continue
        if e.get("component") not in comps:
            bad.append(f"event {n}: component {e.get('component')!r} is not an engine component")
        if e.get("kind") not in KINDS:
            bad.append(f"event {n}: kind {e.get('kind')!r} is not one of {KINDS}")
        if not _num(e.get("t0")) or not (e.get("t1") is None or (_num(e["t1"]) and e["t1"] >= e["t0"])):
            bad.append(f"event {n}: t0 must be a number and t1 None or >= t0")
        if not isinstance(e.get("charged"), bool) or e.get("charged") != (e.get("kind") == "decision"):
            bad.append(f"event {n}: charged must be true exactly for kind decision")
        if not str(e.get("label") or "").strip():
            bad.append(f"event {n}: no label")
        if _num(e.get("t0")) and (e.get("t1") is None or _num(e.get("t1"))):
            t0, t1 = float(e["t0"]), float(e["t0"] if e.get("t1") is None else e["t1"])
            if t0 < h0 - GRID_TOL or t1 > w1 + GRID_TOL:
                bad.append(f"event {n}: [{t0:g}, {t1:g}] is outside the horizon [{h0:g}, {w1:g}]")
            k = e.get("kind")
            if k == "decision" and not (d0 - GRID_TOL <= t0 and t1 <= d1 + GRID_TOL):
                bad.append(f"event {n}: decision event at {t0:g} is outside the delivery period [{d0:g}, {d1:g})")
            if k == "decision" and e.get("component") not in has:
                bad.append(f"event {n}: decision event for {e.get('component')!r}, which is not in the package")
            if k == "history" and t1 > d0 + GRID_TOL:
                bad.append(f"event {n}: history event at {t0:g} does not end before the delivery period ({d0:g})")
            if k == "routine" and t1 > d0 + GRID_TOL and t0 < d1 - GRID_TOL:
                bad.append(f"event {n}: routine event at {t0:g} falls inside the delivery period [{d0:g}, {d1:g})")
        out.append({k: e.get(k) for k in ("component", "label", "kind", "t0", "t1", "charged")})
    if bad:
        raise HookInvalid("; ".join(bad[:5]))
    return out


def run_series(engine, params, unit, packages, horizon, seed, metrics, step="month") -> list[dict]:
    """The engine's monthly series for one unit, its packages and one seed, checked (fail closed)."""
    _contract(engine)
    if not supports(engine, "series"):
        raise _unsupported("run_series", engine)
    known = series_metrics(engine)
    miss = [m for m in metrics if m not in known]
    if miss:
        raise HookInvalid(f"metrics {miss} are not in the card's outputs['series'] ({sorted(known)})")
    for m in metrics:
        meta = known[m] if isinstance(known[m], dict) else {}
        lack = [k for k in META_KEYS if not (isinstance(meta.get(k), str) and meta[k].strip())]
        if lack:
            raise HookInvalid(f"series metric {m}: the card must give {', '.join(lack)} (label, units, aggregate, "
                              "denominator)")
    bad_agg = [m for m in metrics if (known[m] or {}).get("aggregate") not in AGGREGATES]
    if bad_agg:
        raise HookInvalid(f"metrics {bad_agg}: aggregate must be one of {AGGREGATES}")
    if step not in STEPS:
        raise HookInvalid(f"step {step!r} is not one of {STEPS}")
    res = engine.run_series(params, unit, list(packages), horizon, int(seed), list(metrics), step)
    if not isinstance(res, list) or len(res) != len(packages):
        raise HookInvalid("run_series() must return one entry per package")
    for p, r in zip(packages, res):
        if not isinstance(r, dict) or r.get("package") != p.id:
            raise HookInvalid(f"run_series(): entry for {p.id} missing or out of order")
        t = r.get("t")
        if not isinstance(t, list) or not t or not all(_num(x) for x in t) or any(b <= a for a, b in zip(t, t[1:])):
            raise HookInvalid(f"run_series(): {p.id}: t must be increasing numbers")
        if not (t[0] >= float(horizon.start) - 1e-9 and t[-1] < float(horizon.window[1]) + 1e-9):
            raise HookInvalid(f"run_series(): {p.id}: t must lie in [horizon.start, window end)")
        g = monthly_grid(horizon)
        if len(t) != len(g) or any(abs(a - b) > GRID_TOL for a, b in zip(t, g)):
            raise HookInvalid(f"run_series(): {p.id}: t must be the complete monthly grid from {g[0]:g} "
                              f"({len(g)} steps through the window end); got {len(t)} steps")
        vals = r.get("values")
        if not isinstance(vals, dict) or set(vals) != set(metrics):
            raise HookInvalid(f"run_series(): {p.id}: values must hold exactly the requested metrics")
        for m in metrics:
            v = vals[m]
            if not isinstance(v, list) or len(v) != len(t) or not all(_num(x) for x in v):
                raise HookInvalid(f"run_series(): {p.id}.{m}: one finite value per step")
    return res



# --------------------------------------------------------------------------- toy engine with both hooks
def _toy_base():
    from toolkit.engines.contract import ToyEngine
    return ToyEngine


class ToyScheduleEngine(_toy_base()):
    """The contract's toy engine plus both hooks (self-tests of the hooks, figure_data and trajectories). One event per
    component: a past round at horizon.start (every arm) and, when the package has the component, a round at the start
    of the decision window. Series: prevalence 1 - exp(-burden) and incidence = burden per person-year, monthly, with the
    package's effect from the window start (the toy engine's closed form)."""

    hooks_contract = HOOKS_CONTRACT

    def describe(self):
        from toolkit.engines.contract import EngineCard
        c = super().describe()
        out = dict(c.outputs, series={"prev": {"label": "prevalence", "units": "share", "aggregate": "mean",
                                               "denominator": "person"},
                                      "inc": {"label": "incidence", "units": "per person-year", "aggregate": "rate",
                                              "denominator": "person-year"}})
        return EngineCard(**dict(c.to_dict(), outputs=out))

    def schedule(self, params, unit, package, horizon):
        ev = []
        for comp in sorted(self.describe().components):
            ev.append({"component": comp, "label": "past round", "kind": "history", "t0": float(horizon.start),
                       "t1": None, "charged": False})
            if package.components.get(comp):
                ev.append({"component": comp, "label": "round", "kind": "decision", "t0": float(horizon.window[0]),
                           "t1": None, "charged": True})
        return ev

    def run_series(self, params, unit, packages, horizon, seed, metrics, step="month"):
        from toolkit.engines.contract import resolve_params
        p = resolve_params(self, params)
        x = float(unit.attributes.get("x", 0.0))
        n = int(round((float(horizon.window[1]) - float(horizon.start)) * 12))
        t = [float(horizon.start) + k / 12 for k in range(n)]
        res = []
        for pk in packages:
            a, b = int(pk.components.get("a", 0)), int(pk.components.get("b", 0))
            m = (1 - p["eff_a"] * a) * (1 - p["eff_b"] * b) + p["synergy"] * a * b
            vals = {"prev": [], "inc": []}
            for tt in t:
                bur = p["base"] * math.exp(p["k"] * x) * (m if tt >= float(horizon.window[0]) else 1.0)
                vals["prev"].append(1 - math.exp(-bur))
                vals["inc"].append(bur)
            res.append({"package": pk.id, "t": list(t), "values": {k: vals[k] for k in metrics}})
        return res

# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    from toolkit.engines.contract import Horizon, Package, ToyEngine, UnitSpec
    Hooked = ToyScheduleEngine
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    h = Horizon(0.0, 3.0, (1.0, 3.0), {})
    u = UnitSpec("u", {"x": 0.0})
    pa, p0 = Package("a", {"a": 1}), Package("none", {})
    plain = ToyEngine()
    ok(not supports(plain, "schedule") and not supports(plain, "series"), "H1 the toy engine offers neither hook")
    for hook, call in (("schedule", lambda: schedule(plain, {}, u, pa, h)),
                       ("series", lambda: run_series(plain, {}, u, [pa], h, 1, ["prev"]))):
        try:
            call()
            ok(False, f"H2 {hook}: an engine without the hook fails closed")
        except HookUnsupported as e:
            ok(f"{hook}()" in str(e) and "toy" in str(e), f"H2 {hook}: an engine without the hook fails closed: {e}")
    E = Hooked()
    s = schedule(E, {}, u, pa, h)
    ok([x["kind"] for x in s] == ["history", "decision", "history"] and s[1]["charged"], "H3 a valid schedule is returned")
    ok([x["kind"] for x in schedule(E, {}, u, p0, h)] == ["history", "history"], "H4 the comparator arm has no decision event")
    r = run_series(E, {}, u, [p0, pa], h, 1, ["prev"])
    ok([x["package"] for x in r] == ["none", "a"] and len(r[0]["t"]) == 36, "H5 a valid series is returned")

    class Bad(Hooked):
        def schedule(self, params, unit, package, horizon):
            return [{"component": "zz", "label": "", "kind": "decision", "t0": 1.0, "t1": 0.5, "charged": False}]

        def run_series(self, params, unit, packages, horizon, seed, metrics, step="month"):
            return [{"package": p.id, "t": [0.0, 0.0], "values": {}} for p in packages]

    for label, call, needle in (
            ("H6 a malformed schedule is refused", lambda: schedule(Bad(), {}, u, pa, h), "not an engine component"),
            ("H7 a malformed series is refused", lambda: run_series(Bad(), {}, u, [pa], h, 1, ["prev"]), "increasing"),
            ("H8 a metric the card does not list is refused", lambda: run_series(E, {}, u, [pa], h, 1, ["zz"]),
             "not in the card")):
        try:
            call()
            ok(False, label)
        except HookInvalid as e:
            ok(needle in str(e), f"{label}: {e}")
    # ---- Codex r1 (P1-6, P2-16, design): grids, schedule semantics, contract version, metric metadata
    from toolkit.engines.horizon import make_horizon

    def refused(label, call, needle, exc=HookInvalid):
        try:
            call()
            ok(False, label)
        except exc as e:
            ok(needle in str(e), f"{label}: {e}")

    class Gap(Hooked):                       # a month missing from the grid
        def run_series(self, params, unit, packages, horizon, seed, metrics, step="month"):
            r = super().run_series(params, unit, packages, horizon, seed, metrics, step)
            for x in r:
                x["t"] = x["t"][:5] + x["t"][6:]
                x["values"] = {m: v[:5] + v[6:] for m, v in x["values"].items()}
            return r

    class Short(Hooked):                     # stops a year before the window ends
        def run_series(self, params, unit, packages, horizon, seed, metrics, step="month"):
            r = super().run_series(params, unit, packages, horizon, seed, metrics, step)
            for x in r:
                x["t"] = x["t"][:-12]
                x["values"] = {m: v[:-12] for m, v in x["values"].items()}
            return r

    class Skew(Hooked):                      # the second arm on another grid
        def run_series(self, params, unit, packages, horizon, seed, metrics, step="month"):
            r = super().run_series(params, unit, packages, horizon, seed, metrics, step)
            r[-1]["t"] = [t + 0.01 for t in r[-1]["t"]]
            return r
    refused("H9 a grid with a missing month is refused", lambda: run_series(Gap(), {}, u, [p0, pa], h, 1, ["prev"]),
            "monthly")
    refused("H9b a grid that stops before the window ends is refused",
            lambda: run_series(Short(), {}, u, [p0, pa], h, 1, ["prev"]), "monthly")
    refused("H9c arms on different grids are refused", lambda: run_series(Skew(), {}, u, [p0, pa], h, 1, ["prev"]),
            "monthly")

    def sched(events):
        class S(Hooked):
            def schedule(self, params, unit, package, horizon):
                return events
        return S()
    hd = make_horizon(0.0, 3.0, (1.0, 3.0), {}, delivery=(1.0, 2.0))
    ev = lambda **k: dict({"component": "a", "label": "round", "kind": "decision", "t0": 1.5, "t1": None,  # noqa: E731
                           "charged": True}, **k)
    refused("H10 a decision event for a component the package does not have is refused",
            lambda: schedule(sched([ev(component="b")]), {}, u, pa, hd), "not in the package")
    refused("H10b a decision event outside the delivery period is refused",
            lambda: schedule(sched([ev(t0=2.5)]), {}, u, pa, hd), "delivery period")
    refused("H10c an event outside [horizon.start, window end] is refused",
            lambda: schedule(sched([ev(kind="history", charged=False, t0=-1.0)]), {}, u, pa, hd), "horizon")
    refused("H10d a history event inside the delivery period is refused",
            lambda: schedule(sched([ev(kind="history", charged=False, t0=1.5)]), {}, u, pa, hd), "history")
    refused("H10e a routine event inside the delivery period is refused",
            lambda: schedule(sched([ev(kind="routine", charged=False, t0=1.5)]), {}, u, pa, hd), "routine")
    ok(len(schedule(sched([ev(), ev(kind="routine", charged=False, t0=2.5),
                           ev(kind="history", charged=False, t0=0.5)]), {}, u, pa, hd)) == 3,
       "H10f events that respect the delivery period are accepted")

    class NoContract(Hooked):
        hooks_contract = None

    class OtherContract(Hooked):
        hooks_contract = "figures-hooks/0"
    ok(not supports(NoContract(), "schedule"), "H11 an engine that declares no hooks contract does not offer the hooks")
    refused("H11b an engine on another hooks contract is refused", lambda: schedule(OtherContract(), {}, u, pa, h),
            "figures-hooks/0")

    class BadMeta(Hooked):
        def describe(self):
            from toolkit.engines.contract import EngineCard
            c = super().describe()
            return EngineCard(**dict(c.to_dict(), outputs=dict(c.outputs, series={"prev": {"aggregate": "mean"}})))
    refused("H12 a series metric without label, units and denominator is refused",
            lambda: run_series(BadMeta(), {}, u, [pa], h, 1, ["prev"]), "denominator")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
