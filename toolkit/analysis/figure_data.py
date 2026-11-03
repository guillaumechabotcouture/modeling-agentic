#!/usr/bin/env python3
"""The numbers the decision figures need beyond the other artifacts (design: docs/design/v2-figures.md §4).

    python3 toolkit/analysis/figure_data.py --self-test

`compute(A, plan, out0, ben0, cost0, uq)` uses the run's own Analysis (its emulator or engine, and the toolkit/alloc
solver through `Analysis.solve`); it runs no engine and has no optimiser of its own. It returns the payload of
`analysis/figure_data.yaml`:

  frontier             the plan re-solved at fractions of the declared envelope (fixed costs held), and the marginal
                       cost per outcome averted at the envelope (central difference over the 0.9-1.1 points)
  dollars              the plan's dollars by component and by region, the fixed costs and the envelope
  by_package           per package in the plan: units, cost, outcome averted, cost per outcome averted, and its
                       interval over the uncertainty draws, replayed with uncertainty.Drawer and the spec's seed (the
                       replay must reproduce the stored fixed-plan interval, else the interval is withheld)
  component_increment  per component: outcome averted and cost added in the units that get it (each unit's package
                       against the same package without the component, where that package exists)
  burden               per unit: the comparator's primary outcome per person, and the population
  calibration          per unit: the modelled calibration metric at the fitted attribute, where the calibration
                       module offers it
  schedule             per region: the delivery events of the engine's schedule hook (toolkit/engines/hooks.py), or
                       the reason it is not available

`rebuild(run_dir)` reconstructs the Analysis of a finished emulator-path run from its stored batch rows (no engine
runs), and refuses unless the rows' engine identity matches this code's engine and the rebuilt plan equals the stored
one. Nothing here names a disease, component or unit.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/figure_data.py"
FRACTIONS = (0.25, 0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5)
REPLAY_RTOL = 1e-6


class RebuildRefused(RuntimeError):
    pass


def _fixed(spec) -> dict:
    raise NotImplementedError("figures β")


def frontier(A, ben0, cost0, out0, fractions=FRACTIONS) -> dict:
    raise NotImplementedError("figures β")


def dollars(A, plan, out0) -> dict:
    raise NotImplementedError("figures β")


def _package_sums(A, plan, out, cost) -> dict:
    raise NotImplementedError("figures β")


def replay(A, plan, uq: dict) -> dict:
    """Per-package sums over the uncertainty step's draws (same Drawer, seed and call order); `reproduced` says
    whether the replay gives the stored fixed-plan interval of the primary outcome."""
    raise NotImplementedError("figures β")


def by_package(A, plan, out0, cost0, rep: dict | None) -> dict:
    raise NotImplementedError("figures β")


def component_increment(A, plan, ben0, cost0) -> dict:
    raise NotImplementedError("figures β")


def burden(A, out0) -> dict:
    raise NotImplementedError("figures β")


def calibration_modelled(A, z0) -> dict | None:
    raise NotImplementedError("figures β")


def schedule(A, plan, z0) -> dict:
    """Per region, the engine's delivery events (hooks.schedule) in the comparator arm (history and routine events,
    every arm) and in each unit's plan arm (decision events), with unit counts."""
    raise NotImplementedError("figures β")


def compute(A, plan, out0, ben0, cost0, uq: dict | None, z0: dict | None = None, log=print) -> dict:
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- offline rebuild
def _canon(x):
    raise NotImplementedError("figures β")


def rebuild(run_dir: str, log=print):
    """(A, plan, out0, ben0, cost0, z0) of a finished emulator-path run, from its stored batch (no engine runs)."""
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    from toolkit.analysis import run as RUN
    from toolkit.spec import schema, signoff
    q = lambda *a, **k: None  # noqa: E731
    with tempfile.TemporaryDirectory() as d:
        bench, v_ok, v_bad = RUN._toy_bench(d)
        schema.toy_spec(d, n=24, bench=bench, dt_values=(v_ok, v_bad))
        signoff.sign(d, "lead", note="self-test")
        R = RUN.run(d, log=q, compute_kw={"max_workers": 2})
        fd = yaml.safe_load(open(os.path.join(d, "analysis", "figure_data.yaml")))["data"]
        fr = fd["frontier"]["points"]
        objs = [p["objective"] for p in fr]
        ok(len(fr) >= 6 and all(b >= a - 1e-9 for a, b in zip(objs, objs[1:])),
           f"FD1 the frontier re-solves the plan at {len(fr)} envelopes and never loses value as the budget grows")
        at1 = next(p for p in fr if p["fraction"] == 1.0)
        ok(abs(at1["objective"] - R["point"]["solver"]["objective"]) <= 1e-6 * abs(at1["objective"]),
           "FD2 at the declared envelope the frontier equals the plan's objective")
        ok(all(p["cost"] <= p["budget_allocatable"] * (1 + 1e-9) for p in fr), "FD3 every frontier plan is within its budget")
        dl = fd["dollars"]
        ok(abs(dl["allocated_usd"] - R["point"]["cost_allocated"]) <= 1e-6 * R["point"]["cost_allocated"]
           and abs(sum(sum(r.values()) for r in dl["by_region"].values()) - dl["allocated_usd"]) <= 1e-6 * dl["allocated_usd"],
           "FD4 dollars by component and by region add up to the plan's cost")
        bp = fd["by_package"]
        ok(sum(v["units"] for v in bp.values()) == 24
           and abs(sum(v["cost"] for v in bp.values()) - R["point"]["cost_allocated"]) <= 1e-6 * R["point"]["cost_allocated"],
           "FD5 per-package units and costs add up to the plan")
        ok(fd["replay"]["reproduced"] is True and all(v.get("usd_per_outcome_interval") for v in bp.values()
                                                      if v["averted"] > 0 and v["units"] > 0),
           f"FD6 the draw replay reproduces the stored interval, so package intervals are given: {fd['replay']}")
        ok(all(v["per_person"] is not None and v["per_person"] >= 0 for v in fd["burden"].values()),
           "FD7 burden per person for every unit")
        ok(fd["schedule"]["status"] == "not_offered" and "schedule()" in fd["schedule"]["reason"],
           f"FD8 an engine without the schedule hook is recorded as not offered: {fd['schedule']}")
        ok(set(fd["component_increment"]) == {"a", "b"}, "FD9 component increments for every component")
        # rebuild from the stored batch reproduces the plan
        A, plan, out0, ben0, cost0, z0 = rebuild(d, log=q)
        ok(plan == {str(k): v for k, v in R["point"]["plan"].items()}, "FD10 the offline rebuild reproduces the stored plan")
        fd2 = compute(A, plan, out0, ben0, cost0, R["uncertainty"], z0, log=q)
        ok(abs(fd2["frontier"]["points"][-1]["objective"] - fr[-1]["objective"]) <= 1e-6 * abs(fr[-1]["objective"]),
           "FD11 the rebuilt frontier equals the harness's")
        # a stale engine identity is refused
        rows = os.path.join(d, "analysis", "batch", "results.jsonl")
        txt = open(rows).read().replace('"version": "1"', '"version": "0"', 1)
        open(rows, "w").write(txt)
        try:
            rebuild(d, log=q)
            ok(False, "FD12 a batch from another engine identity is refused")
        except RebuildRefused as e:
            ok("re-render with the run's own code" in str(e), f"FD12 a batch from another engine identity is refused: {e}")
        # schedule hook: a toy engine that offers it
        from toolkit.engines import hooks
        A.eng = hooks.ToyScheduleEngine()
        sch = schedule(A, plan, z0)
        n_dec = sum(e["units"] for r in sch["regions"].values() for e in r["events"] if e["kind"] == "decision")
        ok(sch["status"] == "ok" and n_dec == sum(len(A.pk[plan[i]]) for i in A.ids),
           f"FD13 with the hook, decision events are counted per region for the units whose package has them ({n_dec})")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
