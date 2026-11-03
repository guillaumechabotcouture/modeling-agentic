#!/usr/bin/env python3
"""The harness figures, drawn from a run's saved artifacts only (design: docs/design/v2-figures.md).

    python -m toolkit.analysis.figures render <run_dir> --out <dir> [--recompute] [--labels <yaml>] [--theme dark]
    python -m toolkit.analysis.figures compare <run_dir> <run_dir> ... --out <dir> [--names A,B]
    python3 toolkit/analysis/figures.py --self-test

`render(run_dir)` (the harness, after the analysis) writes `<run_dir>/figures/<name>.png` (180 dpi) and `.svg`, a
`.csv` table view, and the `scripts/figure_validator.py` provenance sidecar (sources relative to the run dir,
`produced_by: harness`). With `out_dir` it writes there instead and never touches the run dir. Each figure is
produced only when its inputs exist; otherwise its `not_produced` entry gives the reason (fail closed, never a silent
skip). `--recompute` rebuilds the decision numbers of `analysis/figure_data.yaml` from the run's stored batch
(toolkit/analysis/figure_data.py: no engine runs) for runs written before that artifact existed.

Every axis uses figure_style.fmt (no offset notation); labels come from the spec's presentation.labels (or --labels),
else a readable fallback with a definition line. Nothing here names a disease, component or unit.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis import figure_style as FS  # noqa: E402

PRODUCER = "toolkit/analysis/figures.py"
ORDER = ("headline", "allocation_map", "stability_map", "burden_map", "calibration_fit", "frontier", "dollars",
         "cost_per_outcome", "sensitivity", "timeline", "trajectories")
TITLE_MAX = 95
CAPTION_MAX = 230          # two lines of the report's caption width


class Missing(Exception):
    """A figure's input is absent; the message is its not-produced reason."""


def _fv():
    raise NotImplementedError("figures β")


def resolve_source(run_dir: str, src) -> tuple[str | None, str]:
    """(absolute path, "") of a figure source recorded relative to the run dir, or (None, why) when the recorded path
    is not a string or points outside the run dir. A legacy absolute path inside the run dir is accepted as is."""
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- run data
class RunData:
    """Lazy reader of one run's artifacts; records which sources each figure used."""

    def __init__(self, run_dir: str, labels_overlay: dict | None = None, figure_data: dict | None = None,
                 trajectories: dict | None = None):
        raise NotImplementedError("figures β")

    def _load(self, rel):
        raise NotImplementedError("figures β")

    def art(self, kind: str) -> dict:
        raise NotImplementedError("figures β")

    def opt(self, kind):
        raise NotImplementedError("figures β")

    def report(self) -> dict:
        raise NotImplementedError("figures β")

    def region(self, i) -> str:
        raise NotImplementedError("figures β")

    def regions(self) -> list[str]:
        raise NotImplementedError("figures β")

    def plan(self) -> dict:
        raise NotImplementedError("figures β")

    def coords(self):
        """{unit: (lon, lat)} from decision_units.coordinates or lat/lon columns; None when either is missing."""
        raise NotImplementedError("figures β")

    def unit_order(self) -> tuple[list[str], str]:
        """Units in display order within a region, and what the order is."""
        raise NotImplementedError("figures β")

    def window(self):
        raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- drawing helpers
def _fig(t, w, h):
    raise NotImplementedError("figures β")


def _sing(label: str) -> str:
    raise NotImplementedError("figures β")


def _legend_handles(t, items):
    raise NotImplementedError("figures β")


def _unit_layout(R, ax, t, colors: dict, layout: str, order: list[str], size=None, edge=None, labels=True):
    """Draw one dot per unit: at its coordinates (layout 'map') or in a by-region strip (rows = regions)."""
    raise NotImplementedError("figures β")


def _layout(R):
    raise NotImplementedError("figures β")


def _write_csv(path, rows: list[dict]):
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- figures
def _top(fig, inches=0.62):
    """The axes top (figure fraction) that leaves `inches` for the title."""
    raise NotImplementedError("figures β")


def _comp_colors(R, t) -> dict:
    """One categorical slot per component the plan uses, in the spec's component order, contiguous from slot 1 (so
    a stack of them shows only validated adjacent pairs); the same mapping in every figure of the run."""
    raise NotImplementedError("figures β")


def spec_differences(Rs) -> list[str]:
    """What differs between the runs' specs where it bears on the headline (window, delivery, envelope, comparator,
    uncertainty sources, engine choice), in words."""
    raise NotImplementedError("figures β")


def fig_headline(R, t):
    raise NotImplementedError("figures β")


def _alloc_colors(R, plan):
    raise NotImplementedError("figures β")


def fig_allocation(R, t):
    raise NotImplementedError("figures β")


def _binned(t, vals, edges, fmtk):
    raise NotImplementedError("figures β")


def fig_stability(R, t):
    raise NotImplementedError("figures β")


def fig_burden(R, t):
    raise NotImplementedError("figures β")


def fig_calibration(R, t):
    raise NotImplementedError("figures β")


def fig_frontier(R, t):
    raise NotImplementedError("figures β")


def fig_dollars(R, t):
    raise NotImplementedError("figures β")


def fig_cost_per_outcome(R, t):
    raise NotImplementedError("figures β")


def _threshold(dts) -> float:
    raise NotImplementedError("figures β")


def fig_sensitivity(R, t):
    raise NotImplementedError("figures β")


def fig_timeline(R, t):
    raise NotImplementedError("figures β")


def fig_trajectories(R, t):
    raise NotImplementedError("figures β")


FUNCS = {"headline": fig_headline, "allocation_map": fig_allocation, "stability_map": fig_stability,
         "burden_map": fig_burden, "calibration_fit": fig_calibration, "frontier": fig_frontier,
         "dollars": fig_dollars, "cost_per_outcome": fig_cost_per_outcome, "sensitivity": fig_sensitivity,
         "timeline": fig_timeline, "trajectories": fig_trajectories}


# --------------------------------------------------------------------------- rendering
def _offset_free(fig) -> bool:
    raise NotImplementedError("figures β")


def _save(fig, out_dir, name, run_dir, sources, harness: bool, t) -> dict:
    raise NotImplementedError("figures β")


def render(run_dir: str, out_dir: str | None = None, labels_overlay: dict | None = None,
           figure_data: dict | None = None, trajectories: dict | None = None, theme: str = "light",
           only=None, write_index: bool | None = None) -> dict:
    """Draw every figure that has its inputs. Returns {"figures": [entry], "not_produced": [{figure, reason}]}; with
    `out_dir` (offline) also writes <out_dir>/figures.yaml. Entries: path (run-relative in the harness), svg, table,
    title, caption, shows, sources, offset_free."""
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- comparison
def _run_name(d):
    raise NotImplementedError("figures β")


def compare(run_dirs: list[str], out_dir: str, names: list[str] | None = None, labels_overlay: dict | None = None,
            theme: str = "light") -> dict:
    """The headline with its interval, and the component and package counts, side by side across runs of one project
    (same units, components and objective outcome; anything else is refused). Spec differences that bear on the
    headline are named in the caption."""
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- self-test (synthetic toy run dir)
def toy_run(d: str, n: int = 24, regions: int = 3, coords: bool = False, labels: bool = True,
            many_packages: bool = False, with_figure_data: bool = True, with_report: bool = True,
            schedule: bool = False) -> str:
    """A synthetic run dir with toy numbers (no engine, no licensed data): the spec, units and every artifact the
    figures read."""
    import random
    import yaml
    rnd = random.Random(3)
    os.makedirs(os.path.join(d, "analysis"), exist_ok=True)
    comps = ["ca", "cb", "cc", "cd"] if many_packages else ["ca", "cb"]
    hdr = "uid,zone,pop" + (",lat,lon" if coords else "") + ",tgt\n"
    with open(os.path.join(d, "units.csv"), "w") as f:
        f.write(hdr)
        for i in range(n):
            f.write(f"u{i},Z{i % regions},{1000 + 37 * i}" + (f",{5 + i * 0.1:.2f},{3 + (i % 5) * 0.4:.2f}" if coords
                                                              else "") + f",{0.1 + 0.3 * (i % 7) / 7:.4f}\n")
    spec = {"schema_version": 1, "question": "toy", "decision_maker": "toy", "decision_date": 2026,
            "engine": {"ref": "toolkit.engines.contract:ToyEngine"},
            "horizon": {"start": 0, "stop": 3},
            "decision_units": {"csv": "units.csv", "id": "uid", "region": "zone",
                               "populations": {"all": {"column": "pop", "age_groups": ["all"]}}},
            "options": {"populations": {"total": "pop"}, "components": {c: {} for c in comps},
                        "packages": {"generate": True}, "engine_map": {c: {c: 1} for c in comps},
                        "cost_items": [{"id": f"i_{c}", "component": c, "value": 1.0, "basis": "per_person"}
                                       for c in comps]},
            "objective": {"outcome": "dalys", "horizon": [1, 3], "discounting": "none"},
            "constraints": {"envelope_usd": 50000, "fixed_costs_usd": {"floor": 5000}},
            "comparator": {"package": "none", "text": "no package"}, "outcomes_reported": ["dalys", "cases"],
            "calibration": {"method": "emulator_inversion", "attribute": "x", "target_column": "tgt", "metric": "prev",
                            "target_uncertainty": {"region_sd_logit": 0.15}},
            "deliverable": {"headline": {"name": "total", "outcome": "dalys"}}}
    if labels:
        spec["presentation"] = {"labels": {"components": {"ca": "Alpha service", "cb": "Beta service"},
                                           "parameters": {"p_eff": "Effect size"}, "outcomes": {"dalys": "DALYs",
                                                                                                 "cases": "Cases"}}}
    yaml.safe_dump(spec, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
    from toolkit.alloc import costs as acosts
    pk = acosts.packages(spec["options"])
    names = [k for k in pk if k != "none"]
    ids = [f"u{i}" for i in range(n)]
    plan = {u: (names[i % len(names)] if i % 9 else "none") for i, u in enumerate(ids)}

    def art(kind, data):
        yaml.safe_dump({"schema": f"analysis.{kind}/1", "produced_by": "harness", "data": data},
                       open(os.path.join(d, "analysis", f"{kind}.yaml"), "w"), sort_keys=False)
    cnt = {}
    for v in plan.values():
        cnt[v] = cnt.get(v, 0) + 1
    mix = {c: sum(c in pk[v] for v in plan.values()) for c in comps}
    art("point", {"plan": plan, "packages": cnt, "mix": dict(mix, no_package=cnt.get("none", 0)),
                  "averted": {"dalys": 1.234e7, "cases": 5.6e7}, "cost_allocated": 44000.0,
                  "solver": {"objective": 1.234e7, "status": "optimal"}})
    art("uncertainty", {"draws": 16, "seed": 1, "covers": ["engine_parameters", "calibration"], "excludes": [],
                        "label": "95% UI over engine parameters, calibration",
                        "fixed_plan": {"dalys": {"median": 1.25e7, "ci_low": 1.15e7, "ci_high": 1.36e7, "level": 0.95},
                                       "cases": {"median": 5.7e7, "ci_low": 5.1e7, "ci_high": 6.2e7, "level": 0.95}}})
    if with_report:
        cat = {f"package:{u}": {"counts": {plan[u]: 8 + (i % 9), "none": 8 - (i % 9)}, "n": 16} for i, u in
               enumerate(ids)}
        yaml.safe_dump({"categorical_outputs": cat}, open(os.path.join(d, "uncertainty_report.yaml"), "w"))
    rows = []
    for k, (nm, pt, lo, hi) in enumerate((("p_eff", 0.5, 0.3, 0.7), ("p_dur", 600.0, 480.0, 860.0),
                                          ("p_cost", 1.0, 0.8, 1.2))):
        rows.append({"name": nm, "kind": "parameter", "basis": "declared prior range", "point": pt,
                     "low": {"value": lo, "point_plan_objective": 1.234e7 - (3 - k) * 4e5, "share_units_changed": 0.02 * k},
                     "high": {"value": hi, "point_plan_objective": 1.234e7 + (3 - k) * 3e5, "share_units_changed": 0.05 * k}})
    art("sensitivity", {"rows": rows, "objective_point": 1.234e7, "outcome": "dalys"})
    art("engine", {"card": {"name": "toy"}, "parameters": [{"name": "p_eff", "units": "share"},
                                                           {"name": "p_dur", "units": "days"}]})
    art("decision_tests", {"tests": [{"test_id": "prior_range_p_eff", "verdict": "CHANGES_PLAN", "threshold_pct": 10.0},
                                     {"test_id": "prior_range_p_dur", "verdict": "NO_CHANGE", "threshold_pct": 10.0}]})
    art("calibration", {"method": "emulator_inversion", "units": {u: {"z": 0.0} for u in ids},
                        "diagnostics": {"target_logit_residual_max": 7e-5}})
    art("emulator", {"accuracy": {"cal_logit_rmse": 0.13}})
    if with_figure_data:
        regs = sorted({f"Z{i % regions}" for i in range(n)})
        art("figure_data", {
            "primary": "dalys",
            "frontier": {"envelope_usd": 50000, "fixed_costs_usd": 5000, "marginal": {"outcome_per_usd": 120.0,
                                                                                      "usd_per_outcome": 1 / 120.0},
                         "points": [{"fraction": f, "envelope_usd": 50000 * f, "objective": 1.234e7 * (1 - (1 - f) ** 2 if f <= 1 else 1 + 0.05 * (f - 1)),
                                     "cost": 45000 * f} for f in (0.25, 0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5)]},
            "dollars": {"envelope_usd": 50000, "fixed_costs_usd": {"floor": 5000}, "allocated_usd": 44000.0,
                        "by_component": {c: 44000.0 / len(comps) for c in comps},
                        "by_region": {r: {c: 44000.0 / len(comps) / regions for c in comps} for r in regs}},
            "by_package": {k: {"units": v, "cost": 1000.0 * v, "averted": 2.0e5 * v * (1 + 0.1 * len(k)),
                               "usd_per_outcome": 1000.0 / (2.0e5 * (1 + 0.1 * len(k))),
                               "usd_per_outcome_interval": {"ci_low": 0.004, "ci_high": 0.006, "median": 0.005}}
                           for k, v in cnt.items() if k != "none"},
            "replay": {"reproduced": True},
            "component_increment": {c: {"units": 3, "averted": 1e5, "cost": 100.0 * (1 + j), "outcome_per_usd": 1e3 / (1 + j)}
                                    for j, c in enumerate(comps)},
            "burden": {u: {"per_person": 0.002 * (1 + i % 5), "population": 1000} for i, u in enumerate(ids)},
            "calibration": None,
            "schedule": ({"status": "ok", "delivery": [1.0, 2.0], "window": [1.0, 3.0], "start": 0.0,
                          "regions": {r: {"units": n // regions, "events": [
                              {"component": "ca", "label": "past round", "kind": "history", "t0": 0.25, "t1": None,
                               "units": n // regions},
                              {"component": "ca", "label": "round", "kind": "decision", "t0": 1.25, "t1": None,
                               "units": 2},
                              {"component": "cb", "label": "season", "kind": "decision", "t0": 1.5, "t1": 1.8,
                               "units": 1},
                              {"component": "cb", "label": "routine season", "kind": "routine", "t0": 2.5, "t1": 2.8,
                               "units": n // regions}]} for r in regs}}
                         if schedule else
                         {"status": "not_offered",
                          "reason": "engine toy v1 does not offer schedule() (toolkit/engines/hooks.py)"})})
    return d


def _self_test() -> int:
    import hashlib
    import subprocess
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tree_hash(d):
        h = hashlib.sha256()
        for root, _ds, fs in sorted(os.walk(d)):
            for f in sorted(fs):
                h.update(open(os.path.join(root, f), "rb").read())
        return h.hexdigest()

    with tempfile.TemporaryDirectory() as base:
        d = toy_run(os.path.join(base, "run1"))
        res = render(d)
        got = {f["figure"] for f in res["figures"]} | {x["figure"] for x in res["not_produced"]}
        ok(got == set(ORDER) and all(x["reason"] for x in res["not_produced"]),
           f"F1 every figure is produced or has a not-produced reason: {sorted(f['figure'] for f in res['figures'])} / "
           f"{[(x['figure'], x['reason'][:40]) for x in res['not_produced']]}")
        want = {"headline", "allocation_map", "stability_map", "burden_map", "calibration_fit", "frontier", "dollars",
                "cost_per_outcome", "sensitivity"}
        ok(want <= {f["figure"] for f in res["figures"]}, "F1b the toy run has the inputs of nine figures")
        np_ = {x["figure"]: x["reason"] for x in res["not_produced"]}
        ok("needs trajectory runs" in np_.get("trajectories", "") and "schedule" in np_.get("timeline", ""),
           f"F1c trajectories and timeline say why they are not produced: {np_}")
        ok(all(f["offset_free"] for f in res["figures"]), "F2 no figure shows offset notation")
        from matplotlib.ticker import FuncFormatter
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        ax.plot([1.30e7, 1.32e7], [0, 1])
        FS.set_axis_format(ax, "x")
        ok(isinstance(ax.xaxis.get_major_formatter(), FuncFormatter) and FS.offset_free(ax),
           "F2b the tick formatter is figure_style.fmt")
        plt.close(fig)
        sens = open(os.path.join(d, "figures", "sensitivity.csv")).read()
        cpo = open(os.path.join(d, "figures", "cost_per_outcome.csv")).read()
        ok("Effect size" in sens and "Alpha service" in cpo and "Beta service" in cpo,
           "F3 labels come from the spec's presentation block")
        srows = list(csv.DictReader(open(os.path.join(d, "figures", "sensitivity.csv"))))
        ch = {r["name"]: r["changes_plan"] for r in srows}
        ok(ch == {"p_eff": "True", "p_dur": "False", "p_cost": "False"} and srows[0]["name"] == "p_eff",
           f"F4 plan-change markers follow the decision tests (else the share threshold); rows sorted by effect: {ch}")
        ents = {f["figure"]: f for f in res["figures"]}
        ok(all(len(f["caption"]) <= CAPTION_MAX and "\n" not in f["caption"] and len(f["title"]) <= TITLE_MAX
               for f in res["figures"]), "F5 titles are short and captions fit in two lines")
        ok(all(os.path.exists(os.path.join(d, f["path"])) and os.path.exists(os.path.join(d, f["svg"]))
               and os.path.exists(os.path.join(d, f["path"]) + ".provenance.json") for f in res["figures"]),
           "F6 PNG, SVG and the provenance sidecar per figure")
        from PIL import Image
        im = Image.open(os.path.join(d, ents["headline"]["path"]))
        dpi = round(im.info.get("dpi", (0, 0))[0])
        ok(150 <= dpi <= 200, f"F6b PNG resolution {dpi} dpi")
        sc = json.load(open(os.path.join(d, ents["frontier"]["path"]) + ".provenance.json"))
        ok(sc["produced_by"] == "harness" and "analysis/figure_data.yaml" in sc["source_hashes"],
           "F6c the sidecar records the figure's sources relative to the run dir")
        ok("by package" in ents["allocation_map"]["shows"] and "one row per" in ents["allocation_map"]["caption"],
           "F7 without coordinates the allocation map falls back to the by-region dot strip")
        d2 = toy_run(os.path.join(base, "run2"), coords=True, many_packages=True, labels=False)
        res2 = render(d2)
        e2 = {f["figure"]: f for f in res2["figures"]}
        ok("units at their coordinates" in e2["allocation_map"]["caption"]
           and "small multiples by component" in e2["allocation_map"]["shows"],
           "F8 with coordinates it is a map; with more than three packages, small multiples per component")
        ok("ca: engine ca = 1" in e2["allocation_map"]["caption"],
           "F9 without labels, ids get a readable name and a definition line")
        # offline render: the run dir is not modified, and the CLI works
        before = tree_hash(d)
        out = os.path.join(base, "out1")
        r = subprocess.run([sys.executable, "-m", "toolkit.analysis.figures", "render", d, "--out", out],
                           cwd=ROOT, capture_output=True, text=True)
        ok(r.returncode == 0 and os.path.exists(os.path.join(out, "figures.yaml")) and tree_hash(d) == before,
           f"F10 render CLI writes to --out and leaves the run dir unchanged {r.stderr[-300:]}")
        try:
            render(d, os.path.join(d, "x"))
            ok(False, "F10b --out inside the run dir is refused")
        except ValueError:
            ok(True, "F10b --out inside the run dir is refused")
        # fail closed on missing inputs
        d3 = toy_run(os.path.join(base, "run3"), with_figure_data=False, with_report=False)
        res3 = render(d3, os.path.join(base, "out3"))
        np3 = {x["figure"]: x["reason"] for x in res3["not_produced"]}
        ok({"frontier", "dollars", "cost_per_outcome", "burden_map", "stability_map", "timeline"} <= set(np3)
           and "figure_data.yaml" in np3["frontier"] and "uncertainty_report" in np3["stability_map"],
           f"F11 missing inputs give not-produced entries with the reason: {np3}")
        os.remove(os.path.join(d3, "analysis", "point.yaml"))
        res4 = render(d3, os.path.join(base, "out4"))
        ok(not any(f["figure"] == "headline" for f in res4["figures"]) and
           "point.yaml" in {x["figure"]: x["reason"] for x in res4["not_produced"]}["headline"],
           "F11b a run without its point artifact draws no headline and says why")
        # compare CLI
        d4 = toy_run(os.path.join(base, "run4"))
        pt = yaml.safe_load(open(os.path.join(d4, "analysis", "point.yaml")))
        pt["data"]["averted"]["dalys"] = 1.1e7
        yaml.safe_dump(pt, open(os.path.join(d4, "analysis", "point.yaml"), "w"))
        outc = os.path.join(base, "cmp")
        r = subprocess.run([sys.executable, "-m", "toolkit.analysis.figures", "compare", d, d4, "--out", outc,
                            "--names", "A,B"], cwd=ROOT, capture_output=True, text=True)
        idx = yaml.safe_load(open(os.path.join(outc, "figures.yaml"))) if r.returncode == 0 else {}
        ok(r.returncode == 0 and os.path.exists(os.path.join(outc, "comparison.png"))
           and "12.3M (A) vs 11M (B)" in idx["figures"][0]["title"] and idx["figures"][0]["offset_free"],
           f"F12 compare CLI renders the headline and counts across runs: {r.stderr[-300:]}")
        try:
            compare([d, d2], os.path.join(base, "cmp2"))
            ok(False, "F13 compare refuses runs of different projects")
        except ValueError as e:
            ok("not the same project" in str(e), "F13 compare refuses runs of different projects")
        d5 = toy_run(os.path.join(base, "run5"), schedule=True)
        res5 = render(d5, os.path.join(base, "out5"))
        tl = [f for f in res5["figures"] if f["figure"] == "timeline"]
        trows = list(csv.DictReader(open(tl[0]["table"]))) if tl else []
        ok(tl and "delivers in 1–2" in tl[0]["title"] and {r["kind"] for r in trows} == {"history", "decision",
                                                                                          "routine"},
           "F15 with the engine's schedule, the timeline shows history, decision and routine deliveries per region")
        resd = render(d, os.path.join(base, "dark"), theme="dark")
        ok(len(resd["figures"]) == len(res["figures"]), "F14 the dark theme renders the same set")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
