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
# The figure set in three tiers (owner, 2026-11-02). Tiers 1 and 2 are drawn for every run; tier 3 depends on the
# question: DECISION_SETS maps the spec's deliverable.kind to its decision figures (one declared table; an unknown
# kind gets a not-produced entry, fail closed).
TIERS = {
    "data_model": ("data_coverage", "unit_attributes", "parameters", "calibration_fit", "design_coverage",
                   "sensitivity"),
    "simulation": ("timeline", "trajectories", "burn_in", "seasonality", "stochastic_noise"),
}
DECISION_SETS = {
    "package_per_unit": ("headline", "allocation_map", "stability_map", "burden_map", "dollars", "frontier",
                         "cost_per_outcome", "decision_tests", "comparison"),
}
TIER_OF = {n: k for k, v in TIERS.items() for n in v}
TIER_OF.update({n: "decision" for v in DECISION_SETS.values() for n in v})


def deliverable_kind(spec: dict):
    """The spec's deliverable.kind, or None when it declares none (no default: the decision set must be explicit)."""
    k = ((spec or {}).get("deliverable") or {}).get("kind")
    return str(k) if k else None


def tiers_for(spec: dict) -> dict:
    return dict(TIERS, decision=DECISION_SETS.get(deliverable_kind(spec), ()))


ORDER = TIERS["data_model"] + TIERS["simulation"] + tuple(n for v in DECISION_SETS.values() for n in v
                                                         if n not in TIERS["data_model"] + TIERS["simulation"])
TITLE_MAX = 95
CAPTION_MAX = 230          # two lines of the report's caption width


class Missing(Exception):
    """A figure's input is absent; the message is its not-produced reason."""


def _fv():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import figure_validator
    return figure_validator


def resolve_source(run_dir: str, src) -> tuple[str | None, str]:
    """(absolute path, "") of a figure source recorded relative to the run dir, or (None, why) when the recorded path
    is not a string or points outside the run dir. A legacy absolute path inside the run dir is accepted as is."""
    if not isinstance(src, str) or not src:
        return None, f"source {src!r} is not a path"
    root = os.path.realpath(run_dir)
    p = os.path.realpath(src if os.path.isabs(src) else os.path.join(run_dir, src))
    if os.path.commonpath([root, p]) != root:
        return None, f"source {src} is outside the run dir"
    return p, ""


# --------------------------------------------------------------------------- run data
class RunData:
    """Lazy reader of one run's artifacts; records which sources each figure used."""

    def __init__(self, run_dir: str, labels_overlay: dict | None = None, figure_data: dict | None = None,
                 trajectories: dict | None = None):
        import yaml
        self.dir = os.path.abspath(run_dir)
        self._yaml = yaml
        self._cache = {}
        self.used: list[str] = []
        sp = os.path.join(self.dir, "decision_spec.yaml")
        if not os.path.exists(sp):
            raise Missing("no decision_spec.yaml in the run dir")
        self.spec = yaml.safe_load(open(sp, encoding="utf-8"))
        self.override = {"figure_data": figure_data, "trajectories": trajectories}
        eng = self.opt("engine") or {}
        self.pres = load_presentation(self.dir, labels_overlay, self.spec, eng)
        self.labels = FS.Labels(self.spec, card=eng.get("card"), params=eng.get("parameters"),
                                overlay=self.pres["labels"])
        self._geo = None
        du = self.spec["decision_units"]
        self.uid, self.region_col = du["id"], du.get("region")
        with open(os.path.join(self.dir, du["csv"]), newline="", encoding="utf-8") as f:
            self.units = list(csv.DictReader(f))
        self.by_id = {str(u[self.uid]): u for u in self.units}
        self.ids = [str(u[self.uid]) for u in self.units]
        from toolkit.alloc import costs as acosts
        self.pk = acosts.packages(self.spec["options"])
        self.components = list(self.spec["options"]["components"])
        self.primary = self.spec["objective"]["outcome"]
        self.comparator = self.spec["comparator"]["package"]

    def _load(self, rel):
        if rel not in self._cache:
            p = os.path.join(self.dir, rel)
            self._cache[rel] = self._yaml.safe_load(open(p, encoding="utf-8")) if os.path.exists(p) else None
        return self._cache[rel]

    def art(self, kind: str) -> dict:
        if self.override.get(kind) is not None:
            return self.override[kind]
        doc = self._load(os.path.join("analysis", f"{kind}.yaml"))
        if doc is None:
            raise Missing(f"needs analysis/{kind}.yaml, which this run does not have")
        rel = os.path.join("analysis", f"{kind}.yaml")
        if rel not in self.used:
            self.used.append(rel)
        return doc["data"]

    def presentation_record(self) -> list[dict]:
        """The presentation files this run's figures read, with their sha: the labels file(s) and, once maps are
        drawn, the boundary and crosswalk files."""
        return list(self.pres["files"]) + (list(self._geo.get("files") or []) if self._geo else [])

    @property
    def geo(self):
        """Unit polygons from the presentation file's boundaries (None without one); loaded once, fail closed."""
        if self.pres.get("boundaries") is None:
            return None
        if self._geo is None:
            self._geo = load_boundaries(self.pres["boundaries"], self.ids, self.region, self.dir)
        return self._geo

    def use(self, rel: str):
        """Record a source file this figure read (besides the analysis artifacts)."""
        if rel not in self.used:
            self.used.append(rel)

    def doc(self, kind):
        """The whole artifact document (envelope included), or None."""
        return self._load(os.path.join("analysis", f"{kind}.yaml"))

    def opt(self, kind):
        try:
            return self.art(kind)
        except Missing:
            return None

    def report(self) -> dict:
        doc = self._load("uncertainty_report.yaml")
        if doc is None:
            raise Missing("needs uncertainty_report.yaml (per-unit package counts over the draws)")
        if "uncertainty_report.yaml" not in self.used:
            self.used.append("uncertainty_report.yaml")
        return doc

    def region(self, i) -> str:
        return str(self.by_id[i].get(self.region_col, "")) if self.region_col else ""

    def regions(self) -> list[str]:
        return sorted({self.region(i) for i in self.ids})

    def plan(self) -> dict:
        return {str(k): v for k, v in self.art("point")["plan"].items()}

    def coords(self):
        """{unit: (lon, lat)} from decision_units.coordinates or lat/lon columns; None when either is missing."""
        c = self.spec["decision_units"].get("coordinates") or {}
        cols = {k.lower(): k for k in (self.units[0] if self.units else {})}
        lat = c.get("lat") or next((cols[k] for k in ("lat", "latitude") if k in cols), None)
        lon = c.get("lon") or next((cols[k] for k in ("lon", "lng", "long", "longitude") if k in cols), None)
        if not lat or not lon:
            return None
        try:
            return {i: (float(self.by_id[i][lon]), float(self.by_id[i][lat])) for i in self.ids}
        except (KeyError, ValueError):
            return None

    def unit_order(self) -> tuple[list[str], str]:
        """Units in display order within a region, and what the order is."""
        fd = self.opt("figure_data")
        if fd and fd.get("burden"):
            b = fd["burden"]
            return sorted(self.ids, key=lambda i: -(b.get(i, {}).get("per_person") or 0)), \
                "comparator burden per person, highest first"
        return sorted(self.ids, key=lambda i: (len(i), i)), self.spec["decision_units"]["id"]

    def window(self):
        return [float(x) for x in self.spec["objective"]["horizon"]]


# --------------------------------------------------------------------------- drawing helpers
def _fig(t, w, h):
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(w, h))
    fig.patch.set_facecolor(t["surface"])
    return fig


def _sing(label: str) -> str:
    return label[:-1] if label.endswith("s") and not label.endswith("ss") else label


def _legend_handles(t, items):
    from matplotlib.lines import Line2D
    return [Line2D([0], [0], marker="o", ls="", markerfacecolor=c, markeredgecolor=c, markersize=7, label=lab)
            for lab, c in items]


def _unit_layout(R, ax, t, colors: dict, layout: str, order: list[str], size=None, edge=None, labels=True):
    """Draw every unit: as its polygon (layout 'boundaries', context features in grey, region outlines heavier), as a
    dot at its coordinates ('map'), or in a by-region strip (rows = regions)."""
    ax.set_facecolor(t["surface"])
    if layout == "boundaries":
        import numpy as np
        from matplotlib.collections import LineCollection, PolyCollection
        B = R.geo
        if B["context"]:
            ax.add_collection(PolyCollection(B["context"], facecolors=t["band"], edgecolors=t["surface"],
                                             linewidths=0.3, zorder=1))
        polys, cols = [], []
        for i in order:
            for ring in B["units"].get(i, []):
                polys.append(ring)
                cols.append(colors[i])
        ax.add_collection(PolyCollection(polys, facecolors=cols, edgecolors=t["surface"], linewidths=0.25, zorder=2))
        if B["outline"]:
            ax.add_collection(LineCollection(B["outline"], colors=t["ink2"], linewidths=0.7, zorder=3))
        allp = np.vstack(polys + B["context"]) if (polys or B["context"]) else np.zeros((1, 2))
        ax.set_xlim(allp[:, 0].min(), allp[:, 0].max())
        ax.set_ylim(allp[:, 1].min(), allp[:, 1].max())
        lat = float(np.mean(allp[:, 1]))
        ax.set_aspect(1 / max(math.cos(math.radians(lat)), 0.2) if -90 <= lat <= 90 else 1.0)
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(False)
        return
    if layout == "map":
        xy = R.coords()
        ids = order
        ax.scatter([xy[i][0] for i in ids], [xy[i][1] for i in ids], c=[colors[i] for i in ids],
                   s=size or 22, edgecolors=edge or t["surface"], linewidths=0.6, zorder=3)
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(False)
        return
    regs = R.regions()
    by = {r: [i for i in order if R.region(i) == r] for r in regs}
    nmax = max(len(v) for v in by.values())
    s = size or max(9, min(40, 2600 / max(nmax, 1) ** 1.25))
    for y, r in enumerate(regs[::-1]):
        xs = list(range(len(by[r])))
        ax.scatter(xs, [y] * len(xs), c=[colors[i] for i in by[r]], s=s, edgecolors=edge or t["surface"],
                   linewidths=0.5, zorder=3)
    ax.set_yticks(range(len(regs)))
    ax.set_yticklabels([f"{r} ({len(by[r])})" for r in regs[::-1]], fontsize=7.5)
    ax.set_xlim(-1, nmax)
    ax.set_ylim(-0.7, len(regs) - 0.3)
    ax.set_xticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0, colors=t["ink2"], labelleft=labels)


def _layout(R):
    if R.geo:
        return "boundaries", "units on their boundaries"
    return ("map", "units at their coordinates") if R.coords() else \
        ("strip", f"one row per {R.labels.region_title().lower()}, one dot per unit")


def _write_csv(path, rows: list[dict]):
    if not rows:
        rows = [{"note": "no rows"}]
    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------- figures
def _top(fig, inches=0.62):
    """The axes top (figure fraction) that leaves `inches` for the title."""
    return 1 - inches / fig.get_figheight()


def _comp_colors(R, t) -> dict:
    """One categorical slot per component the plan uses, in the spec's component order, contiguous from slot 1 (so
    a stack of them shows only validated adjacent pairs); the same mapping in every figure of the run. Components past
    the eighth share one neutral "other" colour (never a ninth hue)."""
    mix = (R.opt("point") or {}).get("mix") or {}
    used = [c for c in R.components if mix.get(c, 0) > 0] or list(R.components)
    return {c: (t["cat"][n] if n < FS.MAX_CATEGORIES else t["ink2"]) for n, c in enumerate(used)}


def spec_differences(Rs) -> list[str]:
    """What differs between the runs' specs where it bears on the headline (window, delivery, envelope, comparator,
    uncertainty sources and level, engine mappings and choice, package definitions), in words."""
    def get(R, path):
        x = R.spec
        for k in path:
            x = (x or {}).get(k) if isinstance(x, dict) else None
        return x
    checks = [(("objective", "horizon"), "outcome window"), (("objective", "delivery"), "delivery period"),
              (("constraints", "envelope_usd"), "envelope"), (("constraints", "fixed_costs_usd"), "fixed costs"),
              (("comparator", "package"), "comparator"), (("uncertainty", "sources"), "uncertainty sources"),
              (("uncertainty", "level"), "interval level"),
              (("options", "engine_map"), "component engine mappings"),
              (("decision_units", "engine_attributes"), "engine unit attributes"),
              (("analysis", "decision_tests"), "decision tests")]
    out = []
    for path, name in checks:
        vals = [json.dumps(get(R, path), sort_keys=True, default=str) for R in Rs]
        if len(set(vals)) > 1:
            out.append(name)
    defs = {}
    for R in Rs:
        for k, cs in R.pk.items():
            defs.setdefault(k, set()).add(tuple(sorted(cs)))
    if any(len(v) > 1 for v in defs.values()):
        out.append("package definitions")
    engs = {json.dumps((R.opt("engine") or {}).get("card", {}).get("version")) for R in Rs}
    if len(engs) > 1:
        out.append("engine version")
    return out


def _spec_threshold(R) -> tuple[float, str]:
    """The share of units whose package must change for a test to count as changing the plan: the spec's
    analysis.share_threshold, else thresholds.yaml."""
    v = (R.spec.get("analysis") or {}).get("share_threshold")
    if v is not None:
        return float(v), "spec analysis.share_threshold"
    import yaml
    return float(yaml.safe_load(open(os.path.join(ROOT, "toolkit", "analysis", "thresholds.yaml")))
                 ["decision_tests"]["share_threshold"]), "thresholds.yaml"


def _bound_attributes(R) -> list[dict]:
    """The spec's engine attributes as bound: {attribute, kind: column|columns|value, columns, value, source}."""
    out = []
    for a, m in ((R.spec.get("decision_units") or {}).get("engine_attributes") or {}).items():
        m = m or {}
        if "value" in m:
            out.append({"attribute": a, "kind": "value", "columns": [], "value": m["value"], "source": m.get("source")})
        elif "column" in m:
            out.append({"attribute": a, "kind": "column", "columns": [m["column"]], "split": m.get("split")})
        elif "columns" in m:
            cols = m["columns"]
            out.append({"attribute": a, "kind": "columns", "columns": list(cols.values()) if isinstance(cols, dict)
                        else list(cols), "keys": list(cols) if isinstance(cols, dict) else None})
    cal = R.spec.get("calibration") or {}
    if cal.get("attribute"):
        out.append({"attribute": cal["attribute"], "kind": "calibrated", "columns": [], "source": "calibration"})
    return out


def _num_or_none(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _x_format(ax, values) -> None:
    """Compact numbers on x, except calendar years, which keep their digits."""
    vals = [v for v in values if v is not None]
    if vals and 1800 <= min(vals) and max(vals) <= 2200:
        from matplotlib.ticker import FuncFormatter, MaxNLocator
        ax.xaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:g}"))
    else:
        FS.set_axis_format(ax, "x")
        ax.locator_params(axis="x", nbins=4)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def fig_data_coverage(R, t):
    import yaml
    regs = R.regions()
    pops = (R.spec.get("decision_units") or {}).get("populations") or {}
    popcols = [pm["column"] for pm in pops.values() if isinstance(pm, dict) and pm.get("column")]
    pop = {i: sum(_num_or_none(R.by_id[i].get(c)) or 0.0 for c in popcols) for i in R.ids}
    attrs = _bound_attributes(R)
    der_p = os.path.join(R.dir, "data", "derivations.yaml")
    ders = (yaml.safe_load(open(der_p)) or {}).get("derivations") or [] if os.path.exists(der_p) else []
    if ders:
        R.use("data/derivations.yaml")
    inputs = []
    for dv in ders:
        cols = dv.get("columns") or {}
        for inp in dv.get("inputs") or []:
            inputs.append({"input": inp, "derivation": dv.get("id"), "used": len((cols.get("uses") or {}).get(inp) or []),
                           "ignored": len((cols.get("ignores") or {}).get(inp) or {})})
    rows = []
    for r in regs:
        ids = [i for i in R.ids if R.region(i) == r]
        rows.append({"region": r, "units": len(ids), "population": sum(pop[i] for i in ids)})
    miss_total = 0
    for a in attrs:
        cells = [R.by_id[i].get(c) for i in R.ids for c in a["columns"]]
        miss = sum(1 for v in cells if v is None or str(v).strip() == "" or str(v).lower() == "nan")
        miss_total += miss
        binding = ("from " + ", ".join(a["columns"][:3]) + (f" … ({len(a['columns'])} columns)"
                                                             if len(a["columns"]) > 3 else "")
                   if a["columns"] else (f"set to {a.get('value')}" if a["kind"] == "value"
                                         else "calibrated by the run"))
        rows.append({"attribute": a["attribute"], "binding": ",".join(a["columns"]) if a["columns"] else binding,
                     "shown": binding, "source": a.get("source") or "", "missing_cells": miss,
                     "cells": len(cells)})
    rows += inputs
    U = R.labels.unit_title()
    RT = R.labels.region_title()
    na = len(attrs)
    H = 0.6 + 0.24 * len(regs) + 0.5 + 0.2 * na + 0.5 + (0.22 * len(inputs) + 0.3 if inputs else 0) + 0.4
    fig = _fig(t, 7.8, H)
    y = H - 0.65
    h1 = 0.24 * len(regs)
    a1 = fig.add_axes([0.24, (y - h1) / H, 0.62, h1 / H])
    FS.style(a1, t, grid="x")
    pv = [x["population"] for x in rows[:len(regs)]]
    a1.barh(range(len(regs))[::-1], pv, height=0.6, color=t["cat"][0])
    for k, x in enumerate(rows[:len(regs)][::-1]):
        a1.text(x["population"], k, f"  {x['units']} {U}", va="center", fontsize=7, color=t["ink2"])
    a1.set_yticks(range(len(regs)))
    a1.set_yticklabels(regs[::-1], fontsize=7.5)
    a1.set_xlim(0, max(pv + [1]) * 1.2)
    FS.set_axis_format(a1, "x", "count")
    a1.set_title(f"population and {U} by {RT.lower()}", fontsize=8.5, loc="left", color=t["ink"])
    y -= h1 + 0.75
    h2 = 0.2 * na
    a2 = fig.add_axes([0.02, (y - h2) / H, 0.96, max(h2, 0.2) / H])
    a2.axis("off")
    a2.set_title("engine attributes as bound by the spec (units-table column, or a value with its source)", fontsize=8.5,
                 loc="left", color=t["ink"])
    for k, x in enumerate([r for r in rows if "attribute" in r]):
        yy = 1 - (k + 0.5) / max(na, 1)
        a2.text(0.0, yy, x["attribute"], fontsize=7.5, color=t["ink"], va="center", transform=a2.transAxes)
        a2.text(0.22, yy, _clip(str(x["shown"]), 62), fontsize=7.5,
                color=t["ink2"], va="center", transform=a2.transAxes)
        miss = (f"{x['missing_cells']} of {x['cells']} cells missing" if x["cells"]
                else ("source: " + _clip(os.path.basename(str(x["source"]).split(" ")[0]), 34) if x["source"]
                      and x["source"] != "calibration" else "from the calibration targets"))
        a2.text(0.72, yy, miss, fontsize=7.5, color=t["ink"] if x["missing_cells"] else t["ink2"], va="center",
                transform=a2.transAxes)
    if inputs:
        y -= h2 + 0.75
        h3 = 0.22 * len(inputs)
        a3 = fig.add_axes([0.42, (y - h3) / H, 0.5, h3 / H])
        FS.style(a3, t, grid="x")
        ys = range(len(inputs))[::-1]
        a3.barh([v + 0.18 for v in ys], [x["used"] for x in inputs], height=0.34, color=t["cat"][0], label="used")
        a3.barh([v - 0.18 for v in ys], [x["ignored"] for x in inputs], height=0.34, color=t["cat"][1],
                label="ignored (with a reason)")
        a3.set_yticks(list(ys))
        a3.set_yticklabels([textwrap.shorten(os.path.basename(x["input"]), 42, placeholder="…") for x in inputs],
                           fontsize=7)
        FS.set_axis_format(a3, "x", "count")
        a3.legend(loc="lower right", fontsize=7)
        a3.set_title("staged inputs, through the derivation, to the units table: columns per input", fontsize=8.5, loc="left",
                     color=t["ink"])
    cs = R.spec.get("calibration") or {}
    meas = []
    if cs.get("target_frame"):
        meas.append("calibration target frame declared")
    if cs.get("target_uncertainty"):
        meas.append("target error model declared")
    title = (f"{len(R.ids)} {U} in {len(regs)} {RT.lower()}s; {na} engine attributes bound, "
             f"{miss_total} missing cells" + (f"; {len(inputs)} staged inputs" if inputs else ""))
    cap = ("Units and population by region, how each engine attribute is bound, and the columns each staged input "
           "contributes to the units table (data/derivations.yaml)"
           + (f"; {', '.join(meas)}." if meas else "; no measurement model declared.")
           + ("" if inputs else " No derivation record in the run dir."))
    return fig, title, cap, rows, "data coverage and provenance"


def fig_unit_attributes(R, t):
    import numpy as np
    attrs = [a for a in _bound_attributes(R) if a["kind"] in ("column", "columns")]
    if not attrs:
        raise Missing("the spec binds no engine attribute to units-table columns")
    card = (R.opt("engine") or {}).get("card") or {}
    vr = card.get("valid_ranges") or {}
    regs = R.regions()
    panels = []
    for a in attrs:
        if a["kind"] == "columns" and len(a["columns"]) == 12 and not a.get("keys"):
            panels.append(("profile", a["attribute"], a["columns"], None))
        elif a["kind"] == "columns":
            for k, c in zip(a.get("keys") or a["columns"], a["columns"]):
                panels.append(("scalar", f"{a['attribute']}: {k}", [c], None))
        else:
            panels.append(("scalar", a["attribute"], a["columns"], a.get("split")))
    ncol = 3
    nrow = math.ceil(len(panels) / ncol)
    ph = 0.17 * len(regs) + 0.7
    fig = _fig(t, 8.2, ph * nrow + 0.8)
    gs = fig.add_gridspec(nrow, ncol, left=0.1, right=0.99, top=_top(fig, 0.7), bottom=0.04, hspace=0.6, wspace=0.12)
    rows, outside = [], 0
    for n, (kind, name, cols, split) in enumerate(panels):
        ax = fig.add_subplot(gs[n // ncol, n % ncol])
        FS.style(ax, t, grid="x")
        if kind == "profile":
            M = np.array([[_num_or_none(R.by_id[i].get(c)) or np.nan for c in cols] for i in R.ids])
            for row in M:
                ax.plot(range(1, 13), row, color=t["neutral"], lw=0.6)
            ax.plot(range(1, 13), np.nanmedian(M, axis=0), color=t["cat"][0], lw=2)
            ax.set_xticks([1, 4, 7, 10])
            ax.set_xlabel("month (median in colour)", fontsize=7)
            FS.set_axis_format(ax, "y")
            rows += [{"attribute": name, "unit": i, **{c: v for c, v in zip(cols, M[k])}} for k, i in enumerate(R.ids)]
        else:
            lo_hi = vr.get(name.split(":")[0])
            for yv, r in enumerate(regs[::-1]):
                vals = []
                for i in R.ids:
                    if R.region(i) != r:
                        continue
                    raw = R.by_id[i].get(cols[0])
                    vs = [_num_or_none(x) for x in str(raw).split(split)] if split else [_num_or_none(raw)]
                    vals += [v for v in vs if v is not None]
                    rows += [{"attribute": name, "unit": i, "region": r, "value": v} for v in vs]
                bad = [v for v in vals if lo_hi and not (lo_hi[0] <= v <= lo_hi[1])]
                outside += len(bad)
                ax.scatter(vals, [yv] * len(vals), s=7, color=t["cat"][0], edgecolors=t["surface"], linewidths=0.3,
                           zorder=3)
                if bad:
                    ax.scatter(bad, [yv] * len(bad), s=30, facecolors="none", edgecolors=t["ink"], linewidths=1,
                               zorder=4)
            ax.set_yticks(range(len(regs)))
            ax.set_yticklabels(regs[::-1] if n % ncol == 0 else [], fontsize=6.5)
            _x_format(ax, [r["value"] for r in rows if r.get("attribute") == name and r.get("value") is not None])
        ax.tick_params(labelsize=6.5)
        ax.set_title(textwrap.shorten(name, 34, placeholder="…"), fontsize=8, loc="left", color=t["ink"])
    vals_bound = [a for a in _bound_attributes(R) if a["kind"] == "value"]
    title = (f"{len(attrs)} unit attributes across {len(R.ids)} {R.labels.unit_title()}; {outside} outside the "
             f"engine's valid ranges")
    cap = ("Each bound attribute by region (dots: units; ringed: outside the engine card's valid range); monthly "
           "profiles as thin lines with the median." + (f" Set by value: {', '.join(a['attribute'] for a in vals_bound)}."
                                                       if vals_bound else ""))
    return fig, title, cap, rows, "unit attribute distributions"


def fig_parameters(R, t):
    eng = R.opt("engine") or {}
    params = eng.get("parameters") or []
    if not params:
        raise Missing("needs the engine parameters (analysis/engine.yaml)")
    rep = {}
    try:
        rep = R.report()
    except Missing:
        pass
    drawn = {x["name"]: x for x in rep.get("parameters_drawn") or [] if isinstance(x, dict) and x.get("name")}
    cal_theta = (R.opt("calibration") or {}).get("theta") or {}
    sv = eng.get("spec_values") or {}       # values the signed spec sets (toolkit/spec/engine_parameters.py)
    rows = []
    for p in params:
        name = p["name"]
        d = drawn.get(name) or {}
        s = sv.get(name) if isinstance(sv.get(name), dict) else None
        if s is not None:
            kind = "spec value"
        elif d.get("source_kind") in ("priors", "prior"):          # toolkit/spec/priors.py plan(): "priors"
            kind = "prior"
        elif name in cal_theta or d.get("source_kind") == "calibration":
            kind = "calibrated"
        else:
            kind = p.get("kind") or "fixed"
        rng = d.get("range") if d.get("range") else (s.get("card_range") if s is not None else None) \
            or [p.get("lo"), p.get("hi")]
        lo, hi = (_num_or_none(rng[0]), _num_or_none(rng[1])) if rng else (None, None)
        val = _num_or_none(s["value"] if s is not None else cal_theta.get(name, p.get("value")))
        row = {"name": name, "label": R.labels.parameter(name), "kind": kind, "value": val, "lo": lo, "hi": hi,
               "units": p.get("units") or "", "dist": d.get("dist") or "", "drawn": bool(d),
               "source": textwrap.shorten(str((s or {}).get("source") or d.get("source") or p.get("source") or ""), 90,
                                          placeholder="…")}
        if s is not None:      # the shown range spans the spec value and the card value it replaces
            card = _num_or_none(s.get("card_value"))
            ends = [x for x in (lo, hi, val, card) if x is not None]
            row.update(lo=min(ends) if ends else None, hi=max(ends) if ends else None, card_value=card,
                       card_lo=lo, card_hi=hi)
        rows.append(row)
    ranged = [r for r in rows if r["lo"] is not None and r["hi"] is not None and r["hi"] > r["lo"] and
              r["value"] is not None]
    if not ranged:
        raise Missing("no engine parameter declares a range")
    kinds = ["spec value", "fitted", "prior", "calibrated", "fixed", "scenario"]
    present = [k for k in kinds if any(r["kind"] == k for r in ranged)] + \
        sorted({r["kind"] for r in ranged} - set(kinds))
    col = {k: (t["cat"][n] if n < FS.MAX_SCATTER_CATEGORIES else t["ink2"]) for n, k in enumerate(present)}
    ranged.sort(key=lambda r: (present.index(r["kind"]), r["name"]))
    fig = _fig(t, 7.8, 0.32 * len(ranged) + 1.5)
    h = fig.get_figheight()
    ax = fig.add_axes([0.42, 0.75 / h, 0.5, _top(fig, 0.6) - 0.75 / h])
    FS.style(ax, t, grid="x")
    edge = 0
    markers = {"fitted": "o", "prior": "D", "calibrated": "s", "spec value": "*"}
    for y, r in enumerate(ranged[::-1]):
        pos = (r["value"] - r["lo"]) / (r["hi"] - r["lo"])
        edge += pos <= 0.02 or pos >= 0.98
        ax.plot([0, 1], [y, y], color=t["neutral"], lw=3, solid_capstyle="round", zorder=1)
        if r["kind"] == "spec value" and r.get("card_lo") is not None and r.get("card_hi") is not None:
            ax.plot([(r["card_lo"] - r["lo"]) / (r["hi"] - r["lo"]), (r["card_hi"] - r["lo"]) / (r["hi"] - r["lo"])],
                    [y, y], color=t["ink2"], lw=1.2, solid_capstyle="butt", zorder=2)        # the card's own range
        if r["kind"] == "spec value" and r.get("card_value") is not None:
            ax.scatter([(r["card_value"] - r["lo"]) / (r["hi"] - r["lo"])], [y], s=34, marker="o", facecolors="none",
                       edgecolors=col[r["kind"]], linewidths=1.2, zorder=3)                 # the card value replaced
        ax.scatter([pos], [y], s=60 if r["kind"] == "spec value" else 36, marker=markers.get(r["kind"], "o"),
                   color=col[r["kind"]], edgecolors=t["surface"], linewidths=1, zorder=4)
    ax.set_yticks(range(len(ranged)))
    ax.set_yticklabels([f"{r['label']}  {FS.fmt(r['value'])} {r['units']}".rstrip()
                        + (f"  spec value (card {FS.fmt(r['card_value'])})" if r["kind"] == "spec value" else "")
                        + f"  ({FS.fmt(r['lo'])}–{FS.fmt(r['hi'])})" for r in ranged[::-1]], fontsize=7)
    ax.set_xlim(-0.05, 1.05)
    ax.set_xticks([0, 0.5, 1])
    ax.set_xticklabels(["low end", "middle", "high end"], fontsize=7)
    from matplotlib.lines import Line2D
    hand = [Line2D([0], [0], marker=markers.get(k, "o"), ls="", color=col[k], label=k) for k in present]
    if "spec value" in present:
        hand.append(Line2D([0], [0], marker="o", ls="", markerfacecolor="none", color=col["spec value"],
                           label="card value"))
    fig.legend(handles=hand, loc="lower left", bbox_to_anchor=(0.42, 0.0), ncol=len(hand), fontsize=7.5)
    cnt = {k: sum(r["kind"] == k for r in ranged) for k in present}
    title = (f"{len(ranged)} of {len(rows)} parameters have ranges (" + ", ".join(f"{v} {k}" for k, v in cnt.items())
             + f"); {edge} at a range edge")
    cap = (f"Each parameter's value within its range (fitted: engine fit; prior: drawn from the spec's prior; "
           f"calibrated: the run's calibration"
           + ("; spec value: set by the signed spec, with the engine card value it replaces as an open circle and the "
              "card's own range as a thin line" if "spec value" in present else "")
           + f"). {len(rows) - len(ranged)} parameters without a range are fixed "
           f"values (table view lists all with their sources).")
    return fig, title, cap, rows, "parameters: priors, fitted and calibrated values with their ranges"


def fig_design_coverage(R, t):
    import numpy as np
    dp = os.path.join(R.dir, "analysis", "batch", "design.json")
    if not os.path.exists(dp):
        raise Missing("needs analysis/batch/design.json (the emulator path's design)")
    R.use("analysis/batch/design.json")
    design = json.load(open(dp))
    pts = design.get("points") or []
    if not pts:
        raise Missing("the design has no points")
    cal = R.opt("calibration") or {}
    uz = {i: (v or {}).get("z") for i, v in (cal.get("units") or {}).items()}
    feats = list(((R.spec.get("analysis") or {}).get("emulator") or {}).get("features") or [])
    names = sorted({k for p in pts for k in (p.get("theta") or {})})
    panels = [("z", "calibrated attribute (design scale)")] + [("f", f) for f in feats] + [("p", n) for n in names]
    ncol = 3
    nrow = math.ceil(len(panels) / ncol)
    fig = _fig(t, 8.0, 1.05 * nrow + 1.2)
    gs = fig.add_gridspec(nrow, ncol, left=0.08, right=0.99, top=_top(fig, 0.7), bottom=0.13, hspace=0.9, wspace=0.12)
    rows = []
    dunits = [str(p.get("unit_id")) for p in pts]
    hold = [bool(p.get("holdout")) for p in pts]
    for n, (kind, name) in enumerate(panels):
        ax = fig.add_subplot(gs[n // ncol, n % ncol])
        FS.style(ax, t, grid="x")
        if kind == "z":
            dv = [p.get("z") for p in pts]
            uv = [v for v in uz.values() if v is not None]
            label_u = "units (fitted)"
        elif kind == "f":
            dv = [_num_or_none(R.by_id.get(u, {}).get(name)) for u in dunits]
            uv = [_num_or_none(R.by_id[i].get(name)) for i in R.ids]
            label_u = "units"
        else:
            dv = [(p.get("theta") or {}).get(name) for p in pts]
            uv = []
            label_u = ""
        if uv:
            ax.scatter([v for v in uv if v is not None], [1] * len([v for v in uv if v is not None]), s=8,
                       color=t["neutral"], zorder=2)
        ax.scatter([v for v, h in zip(dv, hold) if v is not None and not h], [0] * sum(1 for v, h in zip(dv, hold)
                                                                                     if v is not None and not h),
                   s=6, color=t["cat"][0], alpha=0.7, linewidths=0, zorder=3)
        ax.scatter([v for v, h in zip(dv, hold) if v is not None and h], [0] * sum(1 for v, h in zip(dv, hold)
                                                                                 if v is not None and h),
                   s=16, facecolors="none", edgecolors=t["cat"][1], linewidths=1, zorder=3)
        ax.set_yticks([0, 1] if uv else [0])
        ax.set_yticklabels((["design", label_u] if uv else ["design"]) if n % ncol == 0 else ([""] * (2 if uv else 1)),
                           fontsize=7)
        ax.set_ylim(-0.6, 1.6)
        _x_format(ax, [v for v in dv + uv if v is not None])
        ax.tick_params(labelsize=6.5)
        ax.set_title(textwrap.shorten(name, 34, placeholder="…"), fontsize=8, loc="left", color=t["ink"])
        dv_ok = [v for v in dv if v is not None]
        uv_ok = [v for v in uv if v is not None]
        out = [v for v in uv_ok if dv_ok and not (min(dv_ok) <= v <= max(dv_ok))]
        rows.append({"dimension": name, "design_min": min(dv_ok) if dv_ok else None,
                     "design_max": max(dv_ok) if dv_ok else None, "units_outside_design_range": len(out),
                     "units": len(uv_ok)})
    from matplotlib.lines import Line2D
    fig.legend(handles=[Line2D([0], [0], marker="o", ls="", color=t["cat"][0], label="design point"),
                        Line2D([0], [0], marker="o", ls="", markerfacecolor="none", markeredgecolor=t["cat"][1],
                               label="held out"),
                        Line2D([0], [0], marker="o", ls="", color=t["neutral"], label="units")],
               loc="lower left", bbox_to_anchor=(0.08, 0.0), ncol=3, fontsize=7.5)
    n_out = sum(r["units_outside_design_range"] for r in rows)
    title = f"Design covers the units: {n_out} unit values fall outside the design range across {len(rows)} dimensions"
    cap = (f"{len(pts)} design points ({sum(hold)} held out) against the units, per emulator input: the calibrated "
           f"attribute, the unit features and the drawn parameters.")
    return fig, title, cap, rows, "design and emulator coverage"


def fig_burn_in(R, t):
    tr = R.opt("trajectories") or {}
    if tr.get("burn_in"):
        raise Missing("burn-in series present but no renderer yet")
    raise Missing("needs a series over the burn-in: run_series (engine hooks contract figures-hooks/1) starts at "
                  "horizon.start, after the burn-in, and the run dir holds no equilibration evidence")


def _monthly_profile_attribute(R):
    for a in _bound_attributes(R):
        if a["kind"] == "columns" and len(a["columns"]) == 12 and not a.get("keys"):
            return a
    return None


def _trajectories_current(R):
    tr = R.opt("trajectories")
    if not tr or tr.get("status") != "ok":
        raise Missing("needs trajectory runs (python -m toolkit.analysis.trajectories run <run_dir>)"
                      + (f": {tr.get('reason')}" if tr and tr.get("reason") else ""))
    doc = R.doc("trajectories") or {}
    from toolkit.analysis.artifacts import sha256
    stale = [rel for rel, h in (doc.get("inputs") or {}).items() if sha256(os.path.join(R.dir, rel)) != h]
    if stale:
        raise Missing(f"the trajectories are stale: {', '.join(stale)} changed after they were computed; rerun "
                      "python -m toolkit.analysis.trajectories run <run_dir>")
    run_eng = (R.doc("point") or {}).get("engine")
    if run_eng and tr.get("analysis_engine") != run_eng:
        raise Missing(f"the trajectories are stale: they were computed against the analysis of engine "
                      f"{(tr.get('analysis_engine') or {}).get('name')} (card "
                      f"{str((tr.get('analysis_engine') or {}).get('card_sha256', ''))[:12]}), this run's is "
                      f"{run_eng.get('name')} (card {str(run_eng.get('card_sha256', ''))[:12]})")
    te = tr.get("engine") or {}
    from toolkit.analysis.trajectories import analysis_identity
    be, where = analysis_identity(R.dir)
    if be is None:
        raise Missing("the trajectories' engine identity cannot be verified: the run records no analysis engine "
                      "identity (analysis/point.yaml engine_identity) and has no batch rows")
    diff = [k for k in ("name", "version", "code_sha256", "params_sha256", "config_sha256") if te.get(k) != be.get(k)]
    if diff:
        raise Missing(f"the trajectories are stale: their engine identity differs from the analysis's ({where}) in "
                      f"{', '.join(d.replace('_sha256', '') for d in diff)} (code {str(te.get('code_sha256', ''))[:12]} "
                      f"vs {str(be.get('code_sha256', ''))[:12]})")
    return tr


def trajectory_checks(R, tr) -> list[dict]:
    """The harness checks paired with the trajectory figure (a later dynamics-review gate reads them)."""
    import numpy as np
    out = []
    d0, d1 = tr["delivery"]
    ser = tr["series"]
    panels = {"total": ser["total"], **(ser.get("regions") or {})}
    worst = 0.0
    for p, s in panels.items():
        for m in tr["metrics"]:
            c, pl = np.asarray(s["arms"]["comparator"][m]), np.asarray(s["arms"]["plan"][m])
            pre = np.asarray(s["t"]) < d0 - 1e-9
            if pre.any():
                scale = max(float(np.max(np.abs(c[pre]))), 1e-12)
                worst = max(worst, float(np.max(np.abs(c[pre] - pl[pre]))) / scale)
    out.append({"check": "plan_equals_comparator_before_delivery", "value": worst,
                "status": "pass" if worst <= 1e-9 else "flag", "detail": "largest relative difference before delivery"})
    cm = tr.get("calibration_metric")
    win = (R.spec.get("calibration") or {}).get("window")
    if cm in tr["metrics"] and win:
        flags, n = [], 0
        for p, s in panels.items():
            tg = s.get("target") or {}
            if tg.get("value") is None:
                continue
            v = _window_mean(s["t"], s["arms"]["comparator"][cm], float(win[0]), float(win[1]),
                             end=float(tr["window"][1]))
            if v != v:              # NaN: no step overlaps the calibration window
                continue
            n += 1
            if not tg["lo"] <= v <= tg["hi"]:
                flags.append(p)
        out.append({"check": "pre_intervention_vs_target", "value": len(flags), "panels": n,
                    "status": "pass" if not flags else "flag",
                    "detail": f"modelled comparator over the calibration window outside the target interval in "
                              f"{len(flags)} of {n} panels" + (f": {', '.join(flags[:5])}" if flags else "")})
    w1 = tr["window"][1]
    if d1 < w1 - 1e-9:
        s = ser["total"]
        tt = np.asarray(s["t"])
        i0, i1 = int(np.searchsorted(tt, d1) - 1), int(np.searchsorted(tt, d1))
        if 0 <= i0 < i1 < len(tt):
            jumps = {m: float((s["arms"]["plan"][m][i1] - s["arms"]["plan"][m][i0])
                              / max(abs(s["arms"]["plan"][m][i0]), 1e-12)) for m in tr["metrics"]}
            out.append({"check": "end_of_delivery_step", "value": jumps, "status": "info",
                        "detail": "relative step in the plan's series across the end of delivery (month before vs "
                                  "after)"})
    return out


def fig_seasonality(R, t):
    import numpy as np
    tr = _trajectories_current(R)
    a = _monthly_profile_attribute(R)
    if a is None:
        raise Missing("no engine attribute is a monthly profile (12 units-table columns) to check the modelled "
                      "season against")
    m = next((k for k in tr["metrics"] if (tr["metric_info"].get(k) or {}).get("aggregate") == "rate"),
             tr["metrics"][0])
    ser = tr["series"]
    panels = ["total"] + sorted(ser.get("regions") or {})
    ncol = 4
    nrow = math.ceil(len(panels) / ncol)
    fig = _fig(t, 8.0, 1.5 * nrow + 1.0)
    gs = fig.add_gridspec(nrow, ncol, left=0.06, right=0.99, top=_top(fig, 0.75), bottom=0.1, hspace=0.8, wspace=0.25)
    rows, shifts = [], []
    d0 = tr["delivery"][0]
    for n, p in enumerate(panels):
        s = ser["total"] if p == "total" else ser["regions"][p]
        ids = s.get("units") or []
        prof = np.nanmean([[_num_or_none(R.by_id[i].get(c)) or np.nan for c in a["columns"]] for i in ids], axis=0)
        tt = np.asarray(s["t"])
        v = np.asarray(s["arms"]["comparator"][m])
        sel = tt < d0 - 1e-9 if (tt < d0 - 1e-9).sum() >= 12 else np.ones_like(tt, bool)
        mon = np.array([int(round((x % 1) * 12)) % 12 for x in tt[sel]])
        mod = np.array([np.mean(v[sel][mon == k]) if (mon == k).any() else np.nan for k in range(12)])
        prof_n, mod_n = prof / np.nanmean(prof), mod / np.nanmean(mod)
        ax = fig.add_subplot(gs[n // ncol, n % ncol])
        FS.style(ax, t, grid="y")
        ax.plot(range(1, 13), prof_n, color=t["muted"], lw=1.4)
        ax.plot(range(1, 13), mod_n, color=t["cat"][0], lw=1.6)
        ax.set_xticks([1, 4, 7, 10])
        ax.tick_params(labelsize=6.5)
        FS.set_axis_format(ax, "y")
        pk_in, pk_mod = int(np.nanargmax(prof_n)) + 1, int(np.nanargmax(mod_n)) + 1
        lag = (pk_mod - pk_in) % 12
        shifts.append(lag)
        ax.set_title(f"{'Sample' if p == 'total' else p}: lag {lag} mo", fontsize=7.5, loc="left", color=t["ink"])
        rows.append({"panel": p, "input_peak_month": pk_in, "model_peak_month": pk_mod, "lag_months": lag,
                     **{f"input_m{k + 1:02d}": prof_n[k] for k in range(12)},
                     **{f"model_m{k + 1:02d}": mod_n[k] for k in range(12)}})
    from matplotlib.lines import Line2D
    fig.legend(handles=[Line2D([0], [0], color=t["muted"], lw=1.4, label=f"input profile ({a['attribute']})"),
                        Line2D([0], [0], color=t["cat"][0], lw=1.6,
                               label=f"modelled {tr['metric_info'].get(m, {}).get('label', m)}, comparator")],
               loc="lower left", bbox_to_anchor=(0.06, 0.0), ncol=2, fontsize=7.5)
    med = sorted(shifts)[len(shifts) // 2]
    title = f"Modelled seasonal peak lags the input profile by a median of {med} months"
    cap = ("Input monthly profile and the modelled comparator series by calendar month, each scaled to its mean, in "
           "the purposive trajectory sample (before delivery where a full year is available).")
    return fig, title, cap, rows, "seasonality check (diagnostic)"


def fig_stochastic_noise(R, t):
    import numpy as np
    tr = _trajectories_current(R)
    se = tr.get("seed_effects") or {}
    if not se:
        raise Missing("the trajectory step recorded no per-seed effects (rerun the trajectory step)")
    m = next((k for k in tr["metrics"] if (tr["metric_info"].get(k) or {}).get("aggregate") == "rate"),
             tr["metrics"][0])
    units = sorted(se, key=lambda i: np.mean(se[i][m]))
    fig = _fig(t, 7.6, 0.22 * len(units) + 1.4)
    h = fig.get_figheight()
    ax = fig.add_axes([0.2, 0.6 / h, 0.75, _top(fig, 0.55) - 0.6 / h])
    FS.style(ax, t, grid="x")
    rows, rel = [], []
    for y, i in enumerate(units):
        v = np.asarray(se[i][m], float)
        ax.scatter(v, [y] * len(v), s=12, color=t["cat"][0], alpha=0.8, zorder=3)
        ax.plot([v.mean()] * 2, [y - 0.3, y + 0.3], color=t["ink"], lw=1.4, zorder=4)
        if len(v) > 1 and abs(v.mean()) > 0:
            rel.append(float(np.std(v, ddof=1) / abs(v.mean())))
        rows.append({"unit": i, "region": R.region(i) if i in R.by_id else "", "mean": float(v.mean()),
                     "sd": float(np.std(v, ddof=1)) if len(v) > 1 else None, **{f"seed_{k}": x for k, x in enumerate(v)}})
    ax.set_yticks(range(len(units)))
    ax.set_yticklabels([f"{R.region(i) if i in R.by_id else ''} {i}" for i in units], fontsize=6.5)
    FS.set_axis_format(ax, "x")
    lab = tr["metric_info"].get(m, {}).get("label", m)
    agg = (tr["metric_info"].get(m) or {}).get("aggregate")
    unit_txt = ("averted per person-year (mean rate over the outcome window)" if agg == "rate" else
                "comparator minus plan (mean over the outcome window)")
    ax.set_xlabel(f"{lab} {unit_txt}, by seed (bar: mean)", fontsize=8)
    med = sorted(rel)[len(rel) // 2] if rel else None
    title = (f"Seed-to-seed spread is {FS.fmt(med, 'percent')} of the mean effect (median over {len(rel)} sampled units)"
             if med is not None else "Seed-to-seed spread of the effect in the sampled units")
    cap = (f"Comparator minus plan, {lab}" + (" as a mean rate per person-year" if agg == "rate" else
                                                " as a mean level") + ", over the outcome window for each seed of "
           f"the trajectory step "
           f"({tr.get('seeds')} seeds) in the purposive sample: what one simulation's Monte Carlo noise looks like.")
    return fig, title, cap, rows, "stochastic noise (diagnostic)"


def fig_decision_tests(R, t):
    dt = R.art("decision_tests")
    tests = dt.get("tests") or []
    if not tests:
        raise Missing("the run declares no decision tests")
    fig = _fig(t, 7.8, 0.36 * len(tests) + 1.4)
    h = fig.get_figheight()
    ax = fig.add_axes([0.36, 0.7 / h, 0.42, _top(fig, 0.6) - 0.7 / h])
    FS.style(ax, t, grid="x")
    rows, k = [], 0
    mx = max([float(x.get("share_pct") or 0) for x in tests] + [float(x.get("threshold_pct") or 0) for x in tests] + [1])
    for y, x in enumerate(tests[::-1]):
        sh, th, v = x.get("share_pct"), x.get("threshold_pct"), x.get("verdict")
        if v == "NOT_RUN" or sh is None:
            ax.text(0, y, " not run", va="center", fontsize=7.5, color=t["ink2"])
        else:
            ax.barh(y, float(sh) / 100, height=0.5, color=t["cat"][0] if v == "CHANGES_PLAN" else t["neutral"])
            if float(sh) == 0:
                ax.text(0, y, " no unit changes", va="center", fontsize=7.5, color=t["ink2"])
        if th is not None:
            ax.plot([float(th) / 100] * 2, [y - 0.35, y + 0.35], color=t["ink"], lw=1.2)
        if v == "CHANGES_PLAN":
            k += 1
            ax.text(1.02, y, "changes the plan", transform=ax.get_yaxis_transform(), va="center", fontsize=7.5,
                    color=t["ink"])
        rows.append({"test": x.get("test_id"), "verdict": v, "share_pct": sh, "threshold_pct": th,
                     "statement": x.get("statement")})
    ax.set_yticks(range(len(tests)))
    texts = {str(d.get("id")): str(d.get("text") or "") for d in
             ((R.spec.get("analysis") or {}).get("decision_tests") or []) if isinstance(d, dict)}

    def name(x):
        tid = str(x.get("test_id"))
        if tid.startswith("prior_range_"):
            return f"prior range: {R.labels.parameter(tid[len('prior_range_'):])}"
        return f"{tid}: {_clip(texts[tid], 44)}" if texts.get(tid) else tid
    ax.set_yticklabels([name(x) for x in tests[::-1]], fontsize=7.5)
    ax.set_xlim(0, mx / 100 * 1.15)
    FS.set_axis_format(ax, "x", "percent")
    ax.set_xlabel(f"share of {R.labels.unit_title()} whose package changes (tick: the test's threshold)", fontsize=8)
    n_run = sum(1 for x in tests if x.get("verdict") in ("CHANGES_PLAN", "NO_CHANGE"))
    title = f"{k} of {n_run} decision tests change the plan" + (
        f"; {len(tests) - n_run} not run" if len(tests) > n_run else "")
    cap = ("Each decision test re-solves the plan under its alternative and counts the units that change package; "
           "filled bars cross the test's threshold.")
    return fig, title, cap, rows, "decision tests: does the plan change?"


def fig_comparison(R, t):
    raise Missing("drawn across runs: python -m toolkit.analysis.figures compare <run_dir> <run_dir> ... --out <dir>")


def contact_sheets(res: dict, dest: str, run_dir: str, harness: bool) -> dict:
    """One overview PNG per tier: a grid of the tier's figures with their titles."""
    from PIL import Image, ImageDraw
    out = {}
    for tier in ("data_model", "simulation", "decision"):
        ents = [f for f in res["figures"] if f.get("tier") == tier]
        if not ents:
            continue
        thumbs = []
        for f in ents:
            p = f["path"] if os.path.isabs(f["path"]) else os.path.join(run_dir, f["path"])
            im = Image.open(p).convert("RGB")
            im.thumbnail((620, 520))
            thumbs.append(im)
        ncol = 3 if len(thumbs) > 2 else len(thumbs)
        cw, ch = 640, max(im.height for im in thumbs) + 20
        nrow = math.ceil(len(thumbs) / ncol)
        sheet = Image.new("RGB", (cw * ncol, ch * nrow + 40), FS.THEMES["light"]["surface"])
        dr = ImageDraw.Draw(sheet)
        dr.text((10, 10), f"{tier.replace('_', ' and ').replace('data and model', 'data and model')}: "
                          f"{len(thumbs)} figures", fill=FS.THEMES["light"]["ink"])
        for n, im in enumerate(thumbs):
            sheet.paste(im, ((n % ncol) * cw + 10, (n // ncol) * ch + 40))
        path = os.path.join(dest, f"overview_{tier}.png")
        sheet.save(path, dpi=(FS.DPI, FS.DPI))
        out[tier] = os.path.relpath(path, run_dir) if harness else path
    return out


def load_presentation(run_dir: str, extra=None, spec: dict | None = None, engine_art: dict | None = None) -> dict:
    """The unsigned presentation files (owner, 2026-11-02): presentation.yaml (or labels.yaml) in the run dir, then
    `extra` (a path given with --labels, or a labels mapping), each overriding the one before id by id. A file holds
    `labels:` (display names) and/or `boundaries:` ({path, feature_id, crosswalk: {path, unit_id, feature_id},
    credit}). Malformed files and ids the spec does not have fail closed (ValueError naming the file). Returns
    {labels, boundaries, files: [{path, sha256}]}; labels never change a number, so the files are not in the spec."""
    import yaml
    from toolkit.analysis.artifacts import sha256
    from toolkit.spec import schema
    keys = set(schema.PRESENTATION_LABELS + schema.PRESENTATION_TITLES)
    eng = engine_art or {}
    pnames = [p.get("name") for p in eng.get("parameters") or [] if isinstance(p, dict)]
    found = [n for n in ("presentation.yaml", "labels.yaml") if os.path.exists(os.path.join(run_dir, n))]
    if len(found) > 1:
        raise ValueError(f"{run_dir}: both presentation.yaml and labels.yaml; keep one presentation file")
    srcs = [(os.path.join(run_dir, n), n) for n in found]
    if isinstance(extra, str):
        srcs.append((os.path.abspath(os.path.expanduser(extra)), os.path.abspath(os.path.expanduser(extra))))
    labels, bnd, files = {}, None, []
    for path, shown in srcs:
        try:
            doc = yaml.safe_load(open(path, encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise ValueError(f"{shown}: unreadable presentation file: {e}") from e
        doc = {} if doc is None else doc
        if not isinstance(doc, dict):
            raise ValueError(f"{shown}: a mapping with labels and/or boundaries")
        if doc and set(doc) <= keys:                      # a bare labels mapping
            doc = {"labels": doc}
        unknown = sorted(set(doc) - {"labels", "boundaries"})
        if unknown:
            raise ValueError(f"{shown}: unknown keys {unknown} (known: labels, boundaries)")
        lab = doc.get("labels") if doc.get("labels") is not None else {}
        probs = schema.labels_problems(spec or {}, lab, pnames, eng.get("card"))
        if probs:
            raise ValueError(f"{shown}: {'; '.join(probs)}")
        for k, v in lab.items():
            if isinstance(v, dict):
                labels.setdefault(k, {}).update(v)
            else:
                labels[k] = v
        if doc.get("boundaries") is not None:
            bnd = _boundaries_config(doc["boundaries"], os.path.dirname(path), shown)
        files.append({"path": shown, "sha256": sha256(path), "kind": "presentation"})
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(v, dict):
                labels.setdefault(k, {}).update(v)
            else:
                labels[k] = v
    return {"labels": labels, "boundaries": bnd, "files": files}


def _boundaries_config(b, base: str, shown: str) -> dict:
    def where(p):
        p = os.path.expanduser(str(p))
        return p if os.path.isabs(p) else os.path.join(base, p)
    if not isinstance(b, dict) or not isinstance(b.get("path"), str) or not isinstance(b.get("feature_id"), str):
        raise ValueError(f"{shown}: boundaries needs path and feature_id (strings)")
    unknown = sorted(set(b) - {"path", "feature_id", "crosswalk", "credit"})
    if unknown:
        raise ValueError(f"{shown}: boundaries: unknown keys {unknown}")
    out = {"path": where(b["path"]), "shown": b["path"], "feature_id": b["feature_id"], "credit": b.get("credit")}
    xw = b.get("crosswalk")
    if xw is not None:
        if not isinstance(xw, dict) or not all(isinstance(xw.get(k), str) for k in ("path", "unit_id", "feature_id")):
            raise ValueError(f"{shown}: boundaries.crosswalk needs path, unit_id and feature_id (strings)")
        unknown = sorted(set(xw) - {"path", "unit_id", "feature_id", "union"})
        if unknown:
            raise ValueError(f"{shown}: boundaries.crosswalk: unknown keys {unknown}")
        if "union" in xw and not isinstance(xw["union"], bool):
            raise ValueError(f"{shown}: boundaries.crosswalk.union must be true or false")
        out["crosswalk"] = {"path": where(xw["path"]), "shown": xw["path"], "unit_id": xw["unit_id"],
                            "feature_id": xw["feature_id"], "union": bool(xw.get("union", False))}
    if out.get("credit") is not None and not isinstance(out["credit"], str):
        raise ValueError(f"{shown}: boundaries.credit must be a string")
    return out


GEOGRAPHIC_CRS = {"EPSG:4326", "urn:ogc:def:crs:EPSG::4326", "urn:ogc:def:crs:OGC:1.3:CRS84", "OGC:CRS84", "CRS84",
                  "urn:ogc:def:crs:OGC::CRS84"}


def _record_path(p: str, run_dir: str | None) -> str:
    """A dependency's path for provenance: relative to the run when it lies inside it (so a moved run checks
    clean), absolute otherwise."""
    if run_dir:
        rp, rd = os.path.realpath(p), os.path.realpath(run_dir)
        if os.path.commonpath([rp, rd]) == rd:
            return os.path.relpath(rp, rd)
    return os.path.abspath(p)


def load_boundaries(cfg: dict, unit_ids: list[str], region_of, run_dir: str | None = None) -> dict:
    """Polygons per unit from a GeoJSON FeatureCollection in geographic coordinates (EPSG:4326 / CRS84; Polygon and
    MultiPolygon exterior rings; json and numpy only), matched to the units through the crosswalk or directly by the
    feature property. Fail closed: a malformed file, a projected CRS, coordinates outside the geographic range, a
    feature id used by two features, and an ambiguous crosswalk (a unit mapped to two features without
    `union: true`, or a feature mapped to two units) raise ValueError naming every affected id. A feature with no
    polygon leaves its unit unmatched. Returns {units: {id: [ring]}, context, unmatched, outline, features,
    context_features, files: [{path, sha256, kind}]}."""
    import numpy as np
    from toolkit.analysis.artifacts import sha256
    shown = cfg["shown"]
    try:
        doc = json.load(open(cfg["path"], encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{shown}: unreadable boundary file: {e}") from e
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list) or not feats:
        raise ValueError(f"{shown}: not a GeoJSON FeatureCollection with features")
    crs = ((doc.get("crs") or {}).get("properties") or {}).get("name") if isinstance(doc.get("crs"), dict) else None
    if doc.get("crs") is not None and crs not in GEOGRAPHIC_CRS:
        raise ValueError(f"{shown}: CRS {crs or doc.get('crs')!r} is not geographic (EPSG:4326 / CRS84 only); "
                         "reproject the file")
    by_fid, dup, empty = {}, set(), set()
    for k, f in enumerate(feats):
        g = f.get("geometry") if isinstance(f, dict) else None
        props = f.get("properties") if isinstance(f, dict) else None
        if not isinstance(g, dict) or not isinstance(props, dict):
            raise ValueError(f"{shown}: feature {k} lacks geometry or properties")
        if props.get(cfg["feature_id"]) is None:
            raise ValueError(f"{shown}: feature {k} has no property {cfg['feature_id']}")
        typ, co = g.get("type"), g.get("coordinates")
        polys = [co] if typ == "Polygon" else co if typ == "MultiPolygon" else None
        if not isinstance(polys, list):
            raise ValueError(f"{shown}: feature {k} geometry {typ!r} is not a Polygon or MultiPolygon")
        rings = []
        for poly in polys:
            try:
                ring = np.asarray(poly[0], float)[:, :2]
            except (TypeError, ValueError, IndexError) as e:
                raise ValueError(f"{shown}: feature {k} has malformed coordinates") from e
            if ring.ndim != 2 or len(ring) < 3 or not np.isfinite(ring).all():
                raise ValueError(f"{shown}: feature {k} has malformed coordinates")
            if (np.abs(ring[:, 0]) > 180).any() or (np.abs(ring[:, 1]) > 90).any():
                raise ValueError(f"{shown}: feature {k} has coordinates outside the geographic range (longitude "
                                 "within ±180, latitude within ±90): a projected file? Reproject to EPSG:4326")
            rings.append(ring)
        fid = str(props[cfg["feature_id"]])
        if fid in by_fid or fid in empty:
            dup.add(fid)
        if rings:
            by_fid[fid] = rings
        else:
            empty.add(fid)
    if dup:
        raise ValueError(f"{shown}: feature id {cfg['feature_id']} is not unique: {sorted(dup)[:20]}")
    files = [{"path": _record_path(cfg["path"], run_dir), "sha256": sha256(cfg["path"]), "kind": "boundaries"}]
    targets = {}
    if cfg.get("crosswalk"):
        c = cfg["crosswalk"]
        try:
            with open(c["path"], newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        except OSError as e:
            raise ValueError(f"{c['shown']}: unreadable crosswalk: {e}") from e
        if not rows or c["unit_id"] not in rows[0] or c["feature_id"] not in rows[0]:
            raise ValueError(f"{c['shown']}: the crosswalk needs columns {c['unit_id']} and {c['feature_id']}")
        for r in rows:
            if r.get(c["feature_id"]):
                tl = targets.setdefault(str(r[c["unit_id"]]), [])
                if str(r[c["feature_id"]]) not in tl:
                    tl.append(str(r[c["feature_id"]]))
        files.append({"path": _record_path(c["path"], run_dir), "sha256": sha256(c["path"]), "kind": "crosswalk"})
        multi = sorted(u for u, fl in targets.items() if len(fl) > 1)
        if multi and not c.get("union"):
            raise ValueError(f"{c['shown']}: {len(multi)} units map to more than one feature: {multi[:20]}; declare "
                             "crosswalk.union: true to draw each such unit as the union of its features")
    else:
        targets = {i: [i] for i in unit_ids}
    users = {}
    for u, fl in targets.items():
        if u in set(unit_ids):
            for fid in fl:
                users.setdefault(fid, []).append(u)
    shared = {fid: us for fid, us in users.items() if len(us) > 1}
    if shared:
        aff = sorted({u for us in shared.values() for u in us})
        raise ValueError(f"{(cfg.get('crosswalk') or {}).get('shown', shown)}: {len(shared)} features map to more than "
                         f"one unit (units {aff[:20]}); each feature may belong to one unit")
    units, unmatched, used = {}, [], set()
    for i in unit_ids:
        fl = [fid for fid in targets.get(i, []) if fid in by_fid]
        if fl and len(fl) == len(targets.get(i, [])):
            units[i] = [r for fid in fl for r in by_fid[fid]]
            used.update(fl)
        else:
            unmatched.append(i)
            used.update(fid for fid in targets.get(i, []) if fid in by_fid)
    context = [r for fid, rs in by_fid.items() if fid not in used for r in rs]
    # region outlines: polygon edges not shared with another unit of the same region
    seg_n = {}
    for i, rings in units.items():
        r = region_of(i)
        for ring in rings:
            q = np.round(ring, 5)
            for a, b in zip(q[:-1], q[1:]):
                key = (r, tuple(sorted([tuple(a), tuple(b)])))
                seg_n[key] = seg_n.get(key, 0) + 1
    outline = [list(k[1]) for k, n in seg_n.items() if n == 1]
    return {"units": units, "context": context, "unmatched": unmatched, "outline": outline,
            "features": len(feats), "context_features": sum(1 for fid in by_fid if fid not in used), "files": files}


def _map_extra(R, layout: str) -> dict:
    """What a unit-layout figure records: its layout and, on boundaries, the unmatched units, the credit and the
    context features (a note the renderer appends to the caption untruncated)."""
    if layout != "boundaries":
        return {"layout": layout}
    B, cfg = R.geo, R.pres["boundaries"]
    um = B["unmatched"]
    note = (f" Not on the map (no boundary): {', '.join(um[:12])}" + (f" and {len(um) - 12} more" if len(um) > 12
                                                                      else "") + "." if um else "")
    return {"layout": "boundaries", "unmatched": list(um), "credit": cfg.get("credit"),
            "context_features": B["context_features"], "caption_note": note}


def compare(run_dirs: list[str], out_dir: str, names: list[str] | None = None, labels_overlay: dict | None = None,
            theme: str = "light") -> dict:
    """The headline with its interval, and the component and package counts, side by side across runs of one project
    (same units, components and objective outcome; each run's deliverable kind must declare the comparison; anything
    else is refused). Packages are matched by their component sets; spec differences that bear on the headline are
    named in the caption. The output dir must lie outside every source run; the figure's provenance lists every
    run's sources, the presentation files and the tier."""
    if len(run_dirs) < 2:
        raise ValueError("compare needs at least two run dirs")
    if len(run_dirs) > FS.MAX_CATEGORIES:
        raise ValueError(f"compare takes at most {FS.MAX_CATEGORIES} runs")
    od = os.path.realpath(out_dir)
    for d in run_dirs:
        rd = os.path.realpath(d)
        if os.path.commonpath([od, rd]) == rd:
            raise ValueError(f"--out {out_dir} is inside the source run {d}; write the comparison elsewhere")
    t = FS.setup(theme)
    Rs = [RunData(d, labels_overlay) for d in run_dirs]
    for R in Rs:
        kind = deliverable_kind(R.spec)
        if not kind or "comparison" not in DECISION_SETS.get(kind, ()):
            raise ValueError(f"{R.dir}: deliverable kind {kind} declares no comparison figure (DECISION_SETS)")
    return _compare_draw(Rs, run_dirs, out_dir, names, t)


def fig_headline(R, t):
    pt, uq = R.art("point"), R.art("uncertainty")
    outs = [R.primary] + [o for o in (R.spec.get("outcomes_reported") or []) if o != R.primary]
    outs = [o for o in outs if o in (uq.get("fixed_plan") or {}) and o in pt["averted"]]
    if not outs:
        raise Missing("the uncertainty artifact has no fixed-plan interval")
    lvl = float(uq["fixed_plan"][outs[0]].get("level", 0.95))
    fig = _fig(t, 7.2, 0.6 * len(outs) + 1.15)
    h = fig.get_figheight()
    gs = fig.add_gridspec(len(outs), 1, left=0.2, right=0.78, top=_top(fig, 0.55), bottom=0.62 / h, hspace=1.2)
    rows = []
    for k, o in enumerate(outs):
        ax = fig.add_subplot(gs[k, 0])
        FS.style(ax, t, grid="x")
        q, p = uq["fixed_plan"][o], float(pt["averted"][o])
        ax.plot([q["ci_low"], q["ci_high"]], [0, 0], color=t["cat"][0], lw=2.4, solid_capstyle="round", zorder=2)
        ax.plot([q["median"]] * 2, [-0.32, 0.32], color=t["ink2"], lw=1.4, zorder=3)
        ax.scatter([p], [0], s=46, color=t["cat"][0], edgecolors=t["surface"], linewidths=1.5, zorder=4)
        span = q["ci_high"] - q["ci_low"]
        lo, hi = min(q["ci_low"], p), max(q["ci_high"], p)
        ax.set_xlim(lo - 0.6 * span, hi + 0.6 * span)
        ax.set_ylim(-1, 1)
        ax.set_yticks([0])
        ax.set_yticklabels([f"{R.labels.outcome(o)} averted"], fontsize=9, color=t["ink"])
        FS.set_axis_format(ax, "x")
        ax.locator_params(axis="x", nbins=5)
        ax.text(1.02, 0.5, f"{FS.fmt(p)}  ({FS.fmt(q['ci_low'])}–{FS.fmt(q['ci_high'])})", transform=ax.transAxes,
                va="center", fontsize=8.5, color=t["ink"])
        rows.append({"outcome": o, "label": R.labels.outcome(o), "point": p, "median": q["median"],
                     "ci_low": q["ci_low"], "ci_high": q["ci_high"], "level": lvl, "draws": uq.get("draws")})
    from matplotlib.lines import Line2D
    fig.legend(handles=[Line2D([0], [0], marker="o", ls="", color=t["cat"][0], label="point estimate (the plan)"),
                        Line2D([0], [0], marker="|", ls="", color=t["ink2"], markersize=9, label="median of draws"),
                        Line2D([0], [0], color=t["cat"][0], lw=2.4, label=f"{lvl:.0%} uncertainty interval")],
               loc="lower left", bbox_to_anchor=(0.2, 0.0), ncol=3, fontsize=7.5)
    o0 = rows[0]
    title = (f"{FS.fmt(o0['point'])} {o0['label']} averted by the plan ({lvl:.0%} UI {FS.fmt(o0['ci_low'])}–"
             f"{FS.fmt(o0['ci_high'])})")
    covers = ", ".join(str(c).replace("_", " ") for c in uq.get("covers") or [])
    excl = ", ".join(str((e or {}).get("kind", e)).replace("_", " ") for e in uq.get("excludes") or [])
    cap = (f"Point plan's outcomes averted against the comparator: point estimate, median and {lvl:.0%} interval over "
           f"{uq.get('draws')} draws of {covers or 'the declared sources'}" + (f"; not covered: {excl}." if excl else "."))
    return fig, title, cap, rows, "the headline outcomes and their uncertainty intervals"


def _alloc_colors(R, plan):
    present = [k for k in R.pk if k in set(plan.values())]
    return present


def fig_allocation(R, t):
    plan = R.plan()
    layout, how = _layout(R)
    order, ordered_by = R.unit_order()
    present = _alloc_colors(R, plan)
    U = R.labels.unit_title()
    N = len(R.ids)
    rows = [{"unit": i, "region": R.region(i), "package": plan[i], "package_label": R.labels.package(R.pk[plan[i]])}
            for i in order]
    n_none = sum(plan[i] == R.comparator for i in R.ids)
    coloured = [k for k in present if k != R.comparator]
    if len(coloured) <= FS.MAX_SCATTER_CATEGORIES:
        col = {k: t["cat"][n] for n, k in enumerate(coloured)}
        col[R.comparator] = t["neutral"]
        fig = _fig(t, 7.6, 0.3 * len(R.regions()) + 1.9 if layout == "strip" else 6.5)
        h = fig.get_figheight()
        ax = fig.add_axes([0.2, 0.62 / h, 0.78, _top(fig) - 0.62 / h])
        _unit_layout(R, ax, t, {i: col[plan[i]] for i in R.ids}, layout, order)
        cnt = {k: sum(plan[i] == k for i in R.ids) for k in present}
        fig.legend(handles=_legend_handles(t, [(f"{R.labels.package(R.pk[k])} ({cnt[k]})", col[k]) for k in present]),
                   loc="lower left", bbox_to_anchor=(0.2, 0.0), ncol=3, fontsize=7.5)
        top = max(cnt, key=cnt.get)
        title = f"Most common package: {R.labels.package(R.pk[top])}, in {cnt[top]} of {N} {U}"
        mode = "by package"
    else:
        comps = R.components
        ncol = 2 if len(comps) <= 4 else 3
        nrow = math.ceil(len(comps) / ncol)
        ph = 0.19 * len(R.regions()) + 0.45 if layout == "strip" else 2.8
        fig = _fig(t, 3.1 * ncol + 1.1, ph * nrow + 0.75)
        gs = fig.add_gridspec(nrow, ncol, left=0.02 if layout == "boundaries" else 0.12, right=0.995, top=_top(fig, 0.75), bottom=0.02,
                              hspace=0.22, wspace=0.04)
        cnt = {}
        for n, c in enumerate(comps):
            ax = fig.add_subplot(gs[n // ncol, n % ncol])
            has = {i: c in R.pk[plan[i]] for i in R.ids}
            cnt[c] = sum(has.values())
            _unit_layout(R, ax, t, {i: (t["cat"][0] if has[i] else t["neutral"]) for i in R.ids}, layout, order,
                         labels=(n % ncol == 0))
            ax.set_title(f"{R.labels.component(c)}: {cnt[c]} {U}", fontsize=8.5, color=t["ink"], loc="left", pad=3)
        top = max(cnt, key=cnt.get)
        title = (f"{R.labels.component(top)} reach {cnt[top]} of {N} {U}; {len(present)} packages in all"
                 + (f", {n_none} {U} get none" if n_none else ""))
        mode = "small multiples by component"
    defs = R.labels.legend_lines()
    cap = (f"Recommended package per unit ({how}; within a row, ordered by {ordered_by})"
           + (". Filled dot: the unit gets the component" if mode.startswith("small") else "")
           + (f". Ids: {'; '.join(defs)}" if defs else "") + ".")
    return fig, title, cap, rows, f"where each package goes ({mode})", _map_extra(R, layout)


def _binned(t, vals, edges, fmtk):
    ramp = t["ramp"]
    def color(v):
        for n in range(len(edges) - 1):
            if v < edges[n + 1] or n == len(edges) - 2:
                return ramp[min(n, len(ramp) - 1)]
        return ramp[-1]
    labels = [f"{FS.fmt(edges[n], fmtk)}–{FS.fmt(edges[n + 1], fmtk)}" for n in range(len(edges) - 1)]
    return color, labels


def fig_stability(R, t):
    plan = R.plan()
    cat = R.report().get("categorical_outputs") or {}
    share, comp_keep = {}, {c: {} for c in R.components}
    for i in R.ids:
        e = cat.get(f"package:{i}")
        if not e or not e.get("n"):
            raise Missing(f"uncertainty_report.yaml has no package counts for unit {i}")
        counts, n = e.get("counts") or {}, float(e["n"])
        share[i] = float(counts.get(plan[i], 0)) / n
        for c in R.components:
            mine = c in R.pk[plan[i]]
            comp_keep[c][i] = sum(v for k, v in counts.items() if (c in R.pk.get(k, ())) == mine) / n
    edges = [0.0, 0.5, 0.7, 0.9, 1.0]
    color, labs = _binned(t, list(share.values()), edges, "percent")
    layout, how = _layout(R)
    order, ordered_by = R.unit_order()
    U = R.labels.unit_title()
    comps = [c for c in R.components if any(c in R.pk[plan[i]] for i in R.ids)
             or any(v < 1.0 for v in comp_keep[c].values())]
    panels = [("package", "whole package", share)] + [(c, R.labels.component(c), comp_keep[c]) for c in comps]
    ncol = 1 if len(panels) == 1 else (2 if len(panels) <= 4 else 3)
    nrow = math.ceil(len(panels) / ncol)
    ph = 0.19 * len(R.regions()) + 0.45 if layout == "strip" else 2.8
    fig = _fig(t, 3.1 * ncol + 1.1, ph * nrow + 1.25)
    h = fig.get_figheight()
    gs = fig.add_gridspec(nrow, ncol, left=0.02 if layout == "boundaries" else 0.12, right=0.995, top=_top(fig, 0.75),
                          bottom=0.55 / h, hspace=0.22,
                          wspace=0.06)
    for n, (key, name, vals) in enumerate(panels):
        ax = fig.add_subplot(gs[n // ncol, n % ncol])
        _unit_layout(R, ax, t, {i: color(vals[i]) for i in R.ids}, layout, order, labels=(n % ncol == 0))
        n90 = sum(v >= 0.9 for v in vals.values())
        ax.set_title(f"{name}: {n90} {U} at 90% or more", fontsize=8.5, color=t["ink"], loc="left", pad=3)
    fig.legend(handles=_legend_handles(t, list(zip(labs, t["ramp"]))), loc="lower left", bbox_to_anchor=(0.12, 0.0),
               ncol=4, fontsize=7.5, title="share of draws in which the re-optimised plan keeps the unit's choice",
               title_fontsize=7.5)
    n90 = sum(v >= 0.9 for v in share.values())
    ck = {c: sum(v >= 0.9 for v in comp_keep[c].values()) for c in comps}
    least = min(ck, key=ck.get) if ck else None
    title = (f"{n90} of {len(share)} {U} keep their whole package in 90% of draws; "
             f"{R.labels.component(least)} only {ck[least]}" if least else
             f"{n90} of {len(share)} {U} keep their whole package in 90% of draws")
    nd = next(iter(cat.values())).get("n")
    cap = (f"Share of {nd} draws in which the re-optimised plan keeps the unit's choice: the whole package (first "
           f"panel) and each component's yes/no; {how}, ordered by {ordered_by}.")
    rows = [{"unit": i, "region": R.region(i), "package": plan[i], "share_keeping_package": share[i],
             **{f"share_keeping_{c}": comp_keep[c][i] for c in comps}} for i in order]
    return fig, title, cap, rows, "assignment stability over the uncertainty draws (package and components)", _map_extra(R, layout)


def fig_burden(R, t):
    fd = R.art("figure_data")
    b = fd.get("burden") or {}
    vals = {i: (b.get(i) or {}).get("per_person") for i in R.ids}
    if any(v is None for v in vals.values()):
        raise Missing("figure_data has no burden for every unit")
    scale = 1000.0 if max(vals.values()) < 10 else 1.0
    v = {i: x * scale for i, x in vals.items()}
    import numpy as np
    edges = list(np.quantile(list(v.values()), [0, 0.25, 0.5, 0.75, 1.0]))
    color, labs = _binned(t, list(v.values()), edges, "number")
    layout, how = _layout(R)
    order, ordered_by = R.unit_order()
    fig = _fig(t, 7.6, 0.3 * len(R.regions()) + 1.9 if layout == "strip" else 6.5)
    h = fig.get_figheight()
    ax = fig.add_axes([0.2, 0.66 / h, 0.78, _top(fig) - 0.66 / h])
    _unit_layout(R, ax, t, {i: color(v[i]) for i in R.ids}, layout, order)
    w0, w1 = R.window()
    per = "per 1,000 people" if scale == 1000 else "per person"
    olab = R.labels.outcome(R.primary)
    fig.legend(handles=_legend_handles(t, list(zip(labs, t["ramp"]))), loc="lower left", bbox_to_anchor=(0.2, 0.0),
               ncol=4, fontsize=7.5, title=f"{olab} {per} over {w0:g}–{w1:g} without the plan (quartiles)",
               title_fontsize=7.5)
    hi = max(R.regions(), key=lambda r: np.mean([v[i] for i in R.ids if R.region(i) == r]))
    title = (f"Burden without the plan: {FS.fmt(min(v.values()))}–{FS.fmt(max(v.values()))} {olab} {per}; "
             f"highest mean in {hi}")
    cap = (f"{olab} {per} over {w0:g}–{w1:g} under the comparator ({R.spec['comparator'].get('text', R.comparator)}); "
           f"{how}, ordered by {ordered_by}.")
    rows = [{"unit": i, "region": R.region(i), f"{R.primary}_{per.replace(' ', '_').replace(',', '')}": v[i]}
            for i in order]
    return fig, title, cap, rows, "comparator burden per person", _map_extra(R, layout)


def fig_calibration(R, t):
    import numpy as np
    cal = R.art("calibration")
    cs = R.spec.get("calibration") or {}
    method = cal.get("method", cs.get("method"))
    if method == "none":
        raise Missing("the spec has no calibration (method none)")
    rows = []
    lg = lambda p: np.log(p / (1 - p))  # noqa: E731
    il = lambda x: 1 / (1 + np.exp(-x))  # noqa: E731
    regs = R.regions()
    RT = R.labels.region_title()
    diag = cal.get("diagnostics") or {}
    n_cl = int(diag.get("clamped_units") or 0)
    if method == "emulator_inversion":
        tc = cs["target_column"]
        tgt = {i: float(R.by_id[i][tc]) for i in R.ids}
        fd = R.opt("figure_data") or {}
        mod_src = ((fd.get("calibration") or {}).get("units")) or {}
        have_mod = all(i in mod_src and mod_src[i].get("modelled") is not None for i in R.ids)
        sd = float((cs.get("target_uncertainty") or {}).get("region_sd_logit") or 0.0)
        e_rmse = ((R.opt("emulator") or {}).get("accuracy") or {}).get("cal_logit_rmse")
        metric = R.labels.metric(cs.get("metric", "metric"))
        m = max(tgt.values()) * 1.06

        def targets_panel(ax):
            for yv, r in enumerate(regs[::-1]):
                v = np.array([tgt[i] for i in R.ids if R.region(i) == r])
                if sd > 0 and len(v):
                    lo = float(np.mean(il(lg(np.clip(v, 1e-6, 1 - 1e-6)) - 1.96 * sd)))
                    hi = float(np.mean(il(lg(np.clip(v, 1e-6, 1 - 1e-6)) + 1.96 * sd)))
                    ax.plot([lo, hi], [yv, yv], color=t["cat"][1], lw=5, alpha=0.35, solid_capstyle="round", zorder=1)
                ax.scatter(v, [yv] * len(v), s=10, color=t["cat"][0], edgecolors=t["surface"], linewidths=0.4,
                           zorder=3)
            ax.set_yticks(range(len(regs)))
            ax.set_yticklabels(regs[::-1], fontsize=7.5)
            ax.set_xlim(0, m)
            FS.set_axis_format(ax, "x", "percent")
            ax.set_xlabel(f"target {metric} per unit" + (f"; bar: shift of the {RT.lower()}'s targets by its 95% "
                                                         "common-mode error" if sd > 0 else ""), fontsize=8)
        if have_mod:
            mod = {i: float(mod_src[i]["modelled"]) for i in R.ids}
            res = {i: float(lg(np.clip(mod[i], 1e-9, 1 - 1e-9)) - lg(np.clip(tgt[i], 1e-9, 1 - 1e-9))) for i in R.ids}
            fig = _fig(t, 7.8, 4.6)
            h = fig.get_figheight()
            a1 = fig.add_axes([0.085, 0.6 / h, 0.37, _top(fig, 0.95) - 0.6 / h])
            a2 = fig.add_axes([0.62, 0.6 / h, 0.36, _top(fig, 0.95) - 0.6 / h])
            FS.style(a1, t, grid="both")
            FS.style(a2, t, grid="x")
            x = np.array([tgt[i] for i in R.ids])
            y = np.array([mod[i] for i in R.ids])
            m = float(max(x.max(), y.max())) * 1.06
            gx = np.linspace(max(m / 400, 1e-4), m, 200)
            if e_rmse:
                a1.fill_between(gx, il(lg(gx) - 1.96 * e_rmse), il(lg(gx) + 1.96 * e_rmse), color=t["band"], lw=0,
                                zorder=0, label="emulator held-out error (95%)")
                a1.legend(loc="upper left", fontsize=7)
            a1.plot([0, m], [0, m], color=t["ink2"], lw=0.8, zorder=2)
            a1.scatter(x, y, s=12, color=t["cat"][0], edgecolors=t["surface"], linewidths=0.5, zorder=3)
            a1.set_xlim(0, m)
            a1.set_ylim(0, m)
            FS.set_axis_format(a1, "x", "percent")
            FS.set_axis_format(a1, "y", "percent")
            a1.set_xlabel(f"target {metric}", fontsize=8)
            a1.set_ylabel("model at the fitted attribute", fontsize=8)
            if sd > 0:
                a2.axvspan(-1.96 * sd, 1.96 * sd, color=t["band"], lw=0, zorder=0)
            for yv, r in enumerate(regs[::-1]):
                v = [res[i] for i in R.ids if R.region(i) == r]
                a2.scatter(v, [yv] * len(v), s=10, color=t["cat"][0], edgecolors=t["surface"], linewidths=0.4,
                           zorder=3)
            a2.axvline(0, color=t["ink2"], lw=0.8)
            a2.set_yticks(range(len(regs)))
            a2.set_yticklabels(regs[::-1], fontsize=7.5)
            lim = max(1.96 * sd * 1.2, max(abs(v) for v in res.values()) * 1.2, 0.05)
            a2.set_xlim(-lim, lim)
            FS.set_axis_format(a2, "x", "signed")
            a2.set_xlabel("model minus target, logit scale" + (" (band: 95% target error)" if sd > 0 else ""),
                          fontsize=8)
            worst = max(abs(v) for v in res.values())
            title = (f"Calibration: largest residual {worst:.2g} logit over {len(R.ids)} targets"
                     + (f"; {n_cl} clamped at the attribute range" if n_cl else "; none clamped"))
            cap = (f"Left: target {metric} against the model at each unit's fitted attribute, 1:1 line"
                   + (f"; band: emulator held-out error ({FS.fmt(e_rmse)} logit RMSE)" if e_rmse else "")
                   + f". Right: residuals by {RT.lower()}" + (f"; band: target error (sd {FS.fmt(sd)} logit)." if sd > 0
                                                              else "."))
            panels = ["fit", "residuals"]
            rows = [{"unit": i, "region": R.region(i), "target": tgt[i], "modelled": mod[i], "residual_logit": res[i]}
                    for i in R.ids]
        else:
            fig = _fig(t, 7.8, 0.28 * len(regs) + 1.4)
            h = fig.get_figheight()
            ax = fig.add_axes([0.2, 0.6 / h, 0.76, _top(fig, 0.6) - 0.6 / h])
            FS.style(ax, t, grid="x")
            targets_panel(ax)
            rmax = diag.get("target_logit_residual_max")
            title = f"Calibration targets by {RT.lower()} with their error; per-unit model values not stored"
            cap = (f"Target {metric} per unit by {RT.lower()}. The fit panel is withheld: this run did not store the "
                   f"model's value per unit" + (f"; its recorded largest residual is {rmax:.1g} logit" if rmax is not None
                                                else "") + (f"; {n_cl} units clamped." if n_cl else "."))
            panels = ["targets"]
            rows = [{"unit": i, "region": R.region(i), "target": tgt[i]} for i in R.ids]
    else:
        fit = diag.get("fit") or []
        if not fit:
            raise Missing(f"calibration method {method} recorded no fit rows")
        fig = _fig(t, 7.8, 4.6)
        h = fig.get_figheight()
        a1 = fig.add_axes([0.085, 0.6 / h, 0.37, _top(fig, 0.95) - 0.6 / h])
        a2 = fig.add_axes([0.62, 0.6 / h, 0.36, _top(fig, 0.95) - 0.6 / h])
        FS.style(a1, t, grid="both")
        FS.style(a2, t, grid="x")
        x = np.array([float(r["observed"]) for r in fit])
        y = np.array([float(r["expected"]) for r in fit])
        a1.scatter(x, y, s=12, color=t["cat"][0], edgecolors=t["surface"], linewidths=0.5, zorder=3)
        m = float(max(x.max(), y.max())) * 1.05
        a1.plot([0, m], [0, m], color=t["ink2"], lw=0.8)
        FS.set_axis_format(a1, "x")
        FS.set_axis_format(a1, "y")
        a1.set_xlabel("observed", fontsize=8)
        a1.set_ylabel("model expected", fontsize=8)
        pr = []
        for yv, r in enumerate(regs[::-1]):
            v = [(float(f["expected"]) - float(f["observed"])) / math.sqrt(max(float(f["expected"]), 1e-9))
                 for f in fit if str(f.get("unit")) in R.by_id and R.region(str(f["unit"])) == r]
            pr += v
            a2.scatter(v, [yv] * len(v), s=10, color=t["cat"][0], zorder=3)
        a2.axvline(0, color=t["ink2"], lw=0.8)
        a2.axvspan(-2, 2, color=t["band"], lw=0, zorder=0)
        a2.set_yticks(range(len(regs)))
        a2.set_yticklabels(regs[::-1], fontsize=7.5)
        FS.set_axis_format(a2, "x", "signed")
        a2.set_xlabel("Pearson residual (band: ±2)", fontsize=8)
        rows = [dict(r) for r in fit]
        out2 = sum(abs(v) > 2 for v in pr)
        title = f"{len(fit)} calibration targets; {out2} Pearson residuals beyond ±2"
        cap = (f"Calibration ({method}): observed against model expected, 1:1 line; right, Pearson residuals by "
               f"{RT.lower()}.")
        panels = ["fit", "residuals"]
    return fig, title, cap, rows, "calibration fit", {"panels": panels}


def fig_frontier(R, t):
    fd = R.art("figure_data")
    fr = fd["frontier"]
    xs, ys = _frontier_xy(fr, R.primary)
    if len(xs) < 2:
        raise Missing("the frontier has fewer than two solved envelopes")
    pt = R.art("point")
    olab = R.labels.outcome(R.primary)
    fig = _fig(t, 7.2, 4.2)
    h = fig.get_figheight()
    ax = fig.add_axes([0.1, 0.55 / h, 0.86, _top(fig, 0.6) - 0.55 / h])
    FS.style(ax, t, grid="both")
    fixed = float(fr.get("fixed_costs_usd") or 0.0)
    ax.plot(xs, ys, color=t["cat"][0], lw=2, zorder=2)
    ax.scatter(xs, ys, s=22, color=t["cat"][0], edgecolors=t["surface"], linewidths=1.2, zorder=3)
    env = fr["envelope_usd"]
    ax.axvline(env, color=t["muted"], lw=0.9, zorder=1)
    ax.text(env, 1.0, " declared envelope", transform=ax.get_xaxis_transform(), va="top", fontsize=7.5,
            color=t["ink2"])
    if fixed:
        ax.axvspan(0, fixed, color=t["band"], lw=0, zorder=0)
        ax.text(fixed / 2, 0.03, f"fixed\n{FS.fmt(fixed, 'money')}", transform=ax.get_xaxis_transform(),
                ha="center", fontsize=7, color=t["ink2"])
    got = float(pt["averted"][R.primary])
    ax.scatter([env], [got], s=70, color=t["cat"][1], edgecolors=t["surface"], linewidths=1.8, zorder=4)
    ax.annotate(f"plan: {FS.fmt(got)} {olab}", (env, got), xytext=(8, -16), textcoords="offset points",
                fontsize=8, color=t["ink"])
    ax.set_xlim(0, max(xs + [env]) * 1.04)
    ax.set_ylim(0, max(ys + [got]) * 1.1)
    FS.set_axis_format(ax, "x", "money")
    FS.set_axis_format(ax, "y")
    ax.set_xlabel("envelope (fixed costs included)", fontsize=8)
    dfac = float(fr.get("discount_factor") or 1.0)
    ax.set_ylabel(f"{olab} averted" + (" (undiscounted)" if abs(dfac - 1) > 1e-12 else ""), fontsize=8)
    mg = fr.get("marginal") or {}
    avg = pt["cost_allocated"] / max(got, 1e-12)
    if mg.get("usd_per_outcome"):
        title = (f"At the envelope, the next {_sing(olab)} averted costs {FS.fmt(mg['usd_per_outcome'], 'money')} "
                 f"(average {FS.fmt(avg, 'money')}, fixed costs excluded)")
    else:
        title = f"{olab} averted rises from {FS.fmt(ys[0])} to {FS.fmt(ys[-1])} across the envelopes solved"
    notes = []
    if fr.get("infeasible"):
        notes.append("infeasible at " + ", ".join(f"{x['fraction']:.0%}" for x in fr["infeasible"]))
    if fr.get("excluded"):
        notes.append("left out (model corrections bear on the plan) at "
                     + ", ".join(f"{x['fraction']:.0%}" for x in fr["excluded"]))
    if fr.get("skipped"):
        notes.append("below the fixed costs at " + ", ".join(f"{x['fraction']:.0%}" for x in fr["skipped"]))
    cap = (f"The plan re-solved by the run's own solver and emulator at fractions of the declared envelope, fixed costs "
           f"held; marginal over 90–110%." + (f" Not drawn: {'; '.join(notes)}." if notes else ""))
    rows = [{"fraction": p["fraction"], "envelope_usd": p["envelope_usd"], "averted": p["averted"][R.primary],
             "objective": p["objective"], "cost": p["cost"],
             **{f"units_{k}": v for k, v in (p.get("packages") or {}).items()}} for p in fr["points"]]
    rows += [{"fraction": x["fraction"], "status": k} for k in ("infeasible", "excluded", "skipped")
             for x in fr.get(k) or []]
    if mg:
        rows.append({"marginal_usd_per_outcome": mg.get("usd_per_outcome"), "average_usd_per_outcome": avg})
    return fig, title, cap, rows, "efficiency frontier: outcome averted against budget"


def fig_dollars(R, t):
    dl = R.art("figure_data")["dollars"]
    col = _comp_colors(R, t)
    bc = dl.get("by_component") or {}
    comps = [c for c in list(R.components) + [c for c in bc if c not in R.components] if bc.get(c, 0) > 0]
    fixed = sum((dl.get("fixed_costs_usd") or {}).values())
    env = float(dl["envelope_usd"])
    tot = float(dl.get("allocated_usd") or sum(bc.values()))
    unspent = max(env - fixed - tot, 0.0)
    regs = sorted(dl.get("by_region") or {}, key=lambda r: -sum(dl["by_region"][r].values()))
    RT = R.labels.region_title()
    ncol = len(comps) + 1
    fig = _fig(t, 7.9, (0.26 * len(regs) + 3.1) if comps else 2.2)
    h = fig.get_figheight()
    top = _top(fig, 0.45)
    a1 = fig.add_axes([0.17, top - 0.42 / h, 0.8, 0.38 / h])
    FS.style(a1, t, grid="x")
    left = 0.0
    if fixed:
        a1.barh(0, fixed, left=0, height=0.8, color=t["neutral"], edgecolor=t["surface"], linewidth=2)
        a1.text(fixed / 2, 0, f"fixed {FS.fmt(fixed, 'money')}", ha="center", va="center", fontsize=7,
                color=t["ink"])
        left = fixed
    for c in comps:
        v = bc[c]
        a1.barh(0, v, left=left, height=0.8, color=col.get(c, t["ink2"]), edgecolor=t["surface"], linewidth=2)
        left += v
    if unspent > 0:
        a1.barh(0, unspent, left=left, height=0.8, color=t["surface"], edgecolor=t["axis"], linewidth=1)
        if unspent > 0.06 * env:                     # label only a segment wide enough for it (else: the table)
            a1.text(left + unspent / 2, 0, f"unspent {FS.fmt(unspent, 'money')}", ha="center", va="center",
                    fontsize=7, color=t["ink2"])
    a1.axvline(env, color=t["ink"], lw=1.2)
    a1.text(env, 0.75, f"envelope {FS.fmt(env, 'money')} ", ha="right", va="bottom", fontsize=7.5, color=t["ink"],
            transform=a1.get_xaxis_transform())
    a1.set_yticks([0])
    a1.set_yticklabels(["whole plan"], fontsize=8)
    a1.set_ylim(-0.5, 1.4)
    a1.set_xlim(0, max(env, left) * 1.01)
    FS.set_axis_format(a1, "x", "money")
    import matplotlib.patches as mp
    if comps:
        fig.legend(handles=[mp.Patch(color=col.get(c, t["ink2"]),
                                     label=f"{R.labels.component(c)} {FS.fmt(bc[c], 'money')}") for c in comps],
                   loc="upper left", bbox_to_anchor=(0.17, top - 0.72 / h), ncol=min(3, len(comps)), fontsize=7.5)
        gs = fig.add_gridspec(1, ncol, left=0.17, right=0.97, top=top - 1.75 / h, bottom=0.35 / h, wspace=0.12)
        vmax = max(max(dl["by_region"][r].get(c, 0.0) for r in regs) for c in comps) if regs else 1.0
        for k, c in enumerate(comps + ["__total"]):
            ax = fig.add_subplot(gs[0, k])
            FS.style(ax, t, grid="x")
            if c == "__total":
                vals = [sum(dl["by_region"][r].values()) for r in regs]
                ax.barh(range(len(regs))[::-1], vals, height=0.62, color=t["ink2"])
                ax.set_xlim(0, max(vals + [1e-9]) * 1.15)
                ax.set_title("all components", fontsize=8, loc="left", color=t["ink"])
            else:
                vals = [dl["by_region"][r].get(c, 0.0) for r in regs]
                ax.barh(range(len(regs))[::-1], vals, height=0.62, color=col.get(c, t["ink2"]))
                ax.set_xlim(0, max(vmax, 1e-9) * 1.1)
                ax.set_title(textwrap.fill(R.labels.component(c), 16), fontsize=8, loc="left", color=t["ink"])
            ax.set_yticks(range(len(regs)))
            ax.set_yticklabels(regs[::-1] if k == 0 else [], fontsize=7.5)
            ax.set_ylim(-0.6, len(regs) - 0.4)
            FS.set_axis_format(ax, "x", "money")
            ax.locator_params(axis="x", nbins=2)
            ax.tick_params(labelsize=6.5)
        topc = max(comps, key=lambda c: bc[c])
        title = (f"The plan uses {FS.fmt((tot + fixed) / env, 'percent')} of the {FS.fmt(env, 'money')} envelope; "
                 f"{R.labels.component(topc)} take {FS.fmt(bc[topc] / tot, 'percent')} of allocated dollars")
        if unspent > 0.005 * env:
            title = (f"{FS.fmt(unspent, 'money')} of the {FS.fmt(env, 'money')} envelope is unspent; "
                     f"{R.labels.component(topc)} take {FS.fmt(bc[topc] / tot, 'percent')} of allocated dollars")
    else:
        title = (f"The plan spends nothing beyond fixed costs: {FS.fmt(fixed, 'money')} fixed, "
                 f"{FS.fmt(unspent, 'money')} unspent of {FS.fmt(env, 'money')}")
    cap = (f"Top: the envelope split into fixed costs, the plan's dollars by component and any unspent funds. Bottom: "
           f"allocated dollars by {RT.lower()} and component (same scale per component column; fixed costs not split)."
           if comps else "The envelope split into fixed costs and unspent funds; the plan allocates nothing.")
    rows = [{"region": r, **{c: dl["by_region"][r].get(c, 0.0) for c in comps or R.components}} for r in regs] + \
        [{"region": "ALL", **{c: bc.get(c, 0.0) for c in comps or R.components}, "fixed": fixed, "unspent": unspent,
          "envelope": env}]
    return fig, title, cap, rows, "dollars by component and region against the envelope"


def fig_cost_per_outcome(R, t):
    fd = R.art("figure_data")
    olab = R.labels.outcome(R.primary)
    U = R.labels.unit_title()
    allp = {k: v for k, v in fd["by_package"].items() if k != R.comparator}

    def status(v):
        if v.get("averted", 0) < 0:
            return "harmful"
        if not v.get("averted"):
            return "undefined"
        return "free" if float(v.get("cost") or 0) <= 0 else "cost"
    st = {k: status(v) for k, v in allp.items()}
    shown = sorted([k for k in allp if st[k] in ("cost", "free")], key=lambda k: allp[k]["cost"] / allp[k]["averted"])
    other = [k for k in allp if st[k] not in ("cost", "free")]
    if not shown and not other:
        raise Missing("the plan gives no unit a package")
    rep = fd.get("replay") or {}
    lvl = float(rep.get("level") or (R.opt("uncertainty") or {}).get("interval", {}).get("level") or 0.95)
    inc = {c: v for c, v in (fd.get("component_increment") or {}).items() if v.get("outcome_per_usd")}
    nrow = len(shown) + len(other)
    fig = _fig(t, 7.6, 0.3 * (nrow + len(inc)) + 2.2)
    h = fig.get_figheight()
    top = _top(fig, 0.6)
    avail = top - 0.55 / h - 0.75 / h
    h1 = avail * (nrow + 0.6) / (nrow + len(inc) + 1.2)
    a1 = fig.add_axes([0.38, top - 0.25 / h - h1, 0.5, h1])
    FS.style(a1, t, grid="x")
    vals, have_ci, withheld = [1e-9], False, []
    order = shown + other
    for y, k in enumerate(order[::-1]):
        v = allp[k]
        if st[k] in ("cost", "free"):
            x = v["cost"] / v["averted"]
            ci = v.get("usd_per_outcome_interval")
            if ci:
                have_ci = True
                a1.plot([ci["ci_low"], ci["ci_high"]], [y, y], color=t["cat"][0], lw=2, solid_capstyle="round",
                        zorder=2)
                vals += [ci["ci_low"], ci["ci_high"]]
            elif v.get("interval_withheld"):
                withheld.append((k, v["interval_withheld"]))
            a1.scatter([x], [y], s=34, color=t["cat"][0], edgecolors=t["surface"], linewidths=1.2, zorder=3)
            if st[k] == "free":
                a1.text(x, y, "  free", va="center", fontsize=7, color=t["ink2"])
            vals.append(x)
        else:
            msg = (f"harms: {FS.fmt(-v['averted'])} more {olab}" if st[k] == "harmful" else f"no {olab} averted")
            a1.text(0, y, " " + msg, va="center", fontsize=7.5, color=t["ink2"])
        a1.text(1.02, y, f"{v['units']}", transform=a1.get_yaxis_transform(), va="center", fontsize=7.5,
                color=t["ink2"])
    a1.text(1.02, nrow - 0.3, U, transform=a1.get_yaxis_transform(), fontsize=7.5, color=t["ink2"])
    a1.set_yticks(range(nrow))
    a1.set_yticklabels([R.labels.package(R.pk[k]) for k in order[::-1]], fontsize=7.5)
    a1.set_xlim(0, max(vals) * 1.08)
    a1.set_ylim(-0.6, nrow - 0.4)
    FS.set_axis_format(a1, "x", "money")
    a1.set_title(f"$ per {_sing(olab)} averted, by package" + (f" (line: {lvl:.0%} UI)" if have_ci else ""),
                 fontsize=8.5, loc="left", color=t["ink"])
    if inc:
        h2 = avail - h1 - 0.5 / h
        a2 = fig.add_axes([0.38, 0.55 / h, 0.5, h2])
        FS.style(a2, t, grid="x")
        cs = sorted(inc, key=lambda c: inc[c]["outcome_per_usd"])
        usd = [1.0 / inc[c]["outcome_per_usd"] for c in cs]
        a2.scatter(usd, range(len(cs))[::-1], s=34, color=t["cat"][0], edgecolors=t["surface"], linewidths=1.2)
        for y, c in zip(range(len(cs))[::-1], cs):
            a2.text(1.02, y, f"{inc[c]['units']}", transform=a2.get_yaxis_transform(), va="center", fontsize=7.5,
                    color=t["ink2"])
        a2.text(1.02, len(cs) - 0.3, U, transform=a2.get_yaxis_transform(), fontsize=7.5, color=t["ink2"])
        a2.set_yticks(range(len(cs))[::-1])
        a2.set_yticklabels([R.labels.component(c) for c in cs], fontsize=7.5)
        a2.set_xlim(0, max(vals + usd) * 1.08)
        a2.set_ylim(-0.6, len(cs) - 0.4)
        FS.set_axis_format(a2, "x", "money")
        a2.set_title(f"$ per {_sing(olab)} averted by adding each component (point estimate)", fontsize=8.5,
                     loc="left", color=t["ink"])
    if shown:
        lo, hi = shown[0], shown[-1]
        title = (f"Cost per {_sing(olab)} averted: {FS.fmt(allp[lo]['cost'] / allp[lo]['averted'], 'money')} to "
                 f"{FS.fmt(allp[hi]['cost'] / allp[hi]['averted'], 'money')} across {len(shown)} packages"
                 + (f"; {len(other)} avert none" if other else ""))
    else:
        title = f"No package in the plan averts {olab}"
    wh = ""
    if withheld:
        sh = [w.get("share_draws_nonpositive") for _k, w in withheld if w.get("share_draws_nonpositive") is not None]
        wh = (f" {len(withheld)} intervals withheld: {withheld[0][1].get('reason')}"
              + (f" (up to {FS.fmt(max(sh), 'percent')} of draws)" if sh else "") + ".")
    cap = (f"Top: each package's cost over its {olab} averted in the {U} that get it, with the {lvl:.0%} interval over "
           f"the uncertainty draws.{wh} Bottom: each component against the same package without it.")
    rows = [{"package": k, "label": R.labels.package(R.pk[k]), "status": st[k],
             **{x: allp[k].get(x) for x in ("units", "cost", "averted", "usd_per_outcome")},
             "ci_low": (allp[k].get("usd_per_outcome_interval") or {}).get("ci_low"),
             "ci_high": (allp[k].get("usd_per_outcome_interval") or {}).get("ci_high"),
             "interval_withheld": (allp[k].get("interval_withheld") or {}).get("reason"),
             "share_draws_nonpositive": (allp[k].get("interval_withheld") or {}).get("share_draws_nonpositive")}
            for k in order] + \
        [{"component": c, "label": R.labels.component(c), "usd_per_outcome_added": 1.0 / v["outcome_per_usd"],
          "units": v["units"], "units_without_counterpart": v.get("units_without_counterpart"),
          "units_with_corrections": v.get("units_with_corrections")} for c, v in inc.items()]
    return fig, title, cap, rows, "cost per outcome averted by package and by component"


def fig_sensitivity(R, t):
    se = R.art("sensitivity")
    rows_in = [r for r in se.get("rows") or [] if r.get("low") and r.get("high")]
    if not rows_in:
        raise Missing("the sensitivity artifact has no rows")
    base = float(se["objective_point"])
    dts = (R.opt("decision_tests") or {}).get("tests") or []
    by_test = {x["test_id"]: x for x in dts}
    spec_thr, spec_src = _spec_threshold(R)
    out = []
    for r in rows_in:
        lo, hi = r["low"]["point_plan_objective"] - base, r["high"]["point_plan_objective"] - base
        tst = by_test.get(f"prior_range_{r['name']}")
        if tst and tst.get("verdict") in ("CHANGES_PLAN", "NO_CHANGE") and tst.get("share_pct") is not None:
            # one calculation: the prior-range decision test's verdict, share and threshold
            share = float(tst["share_pct"]) / 100.0
            thr = float(tst["threshold_pct"]) / 100.0 if tst.get("threshold_pct") is not None else spec_thr
            changes, by = tst["verdict"] == "CHANGES_PLAN", f"decision test {tst['test_id']}"
        else:
            share = max(float(r["low"].get("share_units_changed") or 0), float(r["high"].get("share_units_changed")
                                                                             or 0))
            thr, changes, by = spec_thr, None, f"sensitivity re-optimisation ({spec_src})"
            changes = share > thr
        out.append({"name": r["name"], "label": R.labels.parameter(r["name"]), "units": R.labels.parameter_units(r["name"]),
                    "point": r.get("point"), "low": r["low"]["value"], "high": r["high"]["value"], "d_low": lo,
                    "d_high": hi, "changes_plan": bool(changes), "share_units_changed": share, "threshold": thr,
                    "basis": r.get("basis"), "by": by})
    out.sort(key=lambda x: -max(abs(x["d_low"]), abs(x["d_high"])))
    fig = _fig(t, 7.8, 0.46 * len(out) + 1.45)
    h = fig.get_figheight()
    ax = fig.add_axes([0.33, 0.85 / h, 0.45, _top(fig, 0.55) - 0.85 / h])
    FS.style(ax, t, grid="x")
    U = R.labels.unit_title()
    for y, r in enumerate(out[::-1]):
        ax.barh(y + 0.17, r["d_low"], height=0.3, color=t["cat"][0], zorder=2)
        ax.barh(y - 0.17, r["d_high"], height=0.3, color=t["cat"][1], zorder=2)
        if r["changes_plan"]:
            ax.scatter([1.04], [y], marker="D", s=24, color=t["ink"], transform=ax.get_yaxis_transform(),
                       clip_on=False, zorder=4)
            ax.text(1.075, y, f"changes the plan\n({FS.fmt(r['share_units_changed'], 'percent')} of {U})",
                    transform=ax.get_yaxis_transform(), va="center", fontsize=7, color=t["ink"], linespacing=1.1)
    ax.axvline(0, color=t["ink2"], lw=0.9, zorder=3)
    labels = []
    for r in out[::-1]:
        u = f" {r['units']}" if r["units"] else ""
        labels.append(f"{r['label']}\n{FS.fmt(r['low'])}–{FS.fmt(r['high'])}{u} (base {FS.fmt(r['point'])})")
    ax.set_yticks(range(len(out)))
    ax.set_yticklabels(labels, fontsize=7.5)
    m = max(max(abs(r["d_low"]), abs(r["d_high"])) for r in out) * 1.12
    ax.set_xlim(-m, m)
    FS.set_axis_format(ax, "x", "signed")
    olab = R.labels.outcome(se.get("outcome") or R.primary)
    dfac = float((R.opt("point") or {}).get("discount_factor") or 1.0)
    ax.set_xlabel(f"change in the plan's objective ({'discounted ' if abs(dfac - 1) > 1e-12 else ''}{olab} averted)",
                  fontsize=8)
    import matplotlib.patches as mp
    fig.legend(handles=[mp.Patch(color=t["cat"][0], label="low end of the range"),
                        mp.Patch(color=t["cat"][1], label="high end of the range")],
               loc="lower left", bbox_to_anchor=(0.33, 0.0), ncol=2, fontsize=7.5)
    k = sum(r["changes_plan"] for r in out)
    title = f"{k} of {len(out)} tested ranges change the plan; {out[0]['label']} moves {olab} averted most"
    if len(title) > TITLE_MAX:
        title = f"{k} of {len(out)} tested ranges change the plan"
    thrs = sorted({r["threshold"] for r in out})
    cap = (f"One at a time, each parameter at the ends of its tested range; change in the point plan's objective "
           f"(base {FS.fmt(base)}). Diamond: more than "
           + (f"{FS.fmt(thrs[0], 'percent')}" if len(thrs) == 1 else "the threshold")
           + f" of {U} change package, per the prior-range decision test where one exists (table: share, threshold, source).")
    return fig, title, cap, out, "one-at-a-time sensitivity with plan changes marked"


def fig_timeline(R, t):
    fd = R.art("figure_data")
    sch = fd.get("schedule") or {}
    if sch.get("status") != "ok":
        raise Missing(f"needs the engine's schedule hook: {sch.get('reason', 'no schedule recorded')}")
    regs = sorted(sch["regions"])
    comps = sorted({e["component"] for r in regs for e in sch["regions"][r]["events"]})
    nc = max(len(comps), 1)
    fig = _fig(t, 7.8, 0.22 * len(regs) * nc + 1.7)
    h = fig.get_figheight()
    ax = fig.add_axes([0.24, 0.75 / h, 0.66, _top(fig, 0.8) - 0.75 / h])
    FS.style(ax, t, grid="x")
    d0, d1 = sch["delivery"]
    w0, w1 = sch["window"]
    ax.axvspan(d0, d1, color=t["band"], lw=0, zorder=0)
    ax.text((d0 + d1) / 2, 0.005, "delivery period", transform=ax.get_xaxis_transform(), ha="center",
            va="bottom", fontsize=7, color=t["ink2"])
    ax.annotate("", (w0, 1.035), (w1, 1.035), xycoords=("data", "axes fraction"),
                arrowprops={"arrowstyle": "|-|", "color": t["ink2"], "lw": 0.9, "shrinkA": 0, "shrinkB": 0})
    ax.text(w1, 1.035, " outcome window", transform=ax.get_xaxis_transform(), va="center", fontsize=7,
            color=t["ink2"])
    rows, ylabels = [], []
    kinds = {"history": dict(facecolors="none", edgecolors=t["ink2"]),
             "decision": dict(facecolors=t["cat"][0], edgecolors=t["cat"][0]),
             "routine": dict(facecolors="none", edgecolors=t["cat"][1])}
    y = 0
    for r in regs[::-1]:
        reg = sch["regions"][r]
        for c in comps[::-1]:
            for e in [e for e in reg["events"] if e["component"] == c]:
                frac = e["units"] / max(reg["units"], 1)
                if e.get("t1") is not None:
                    ax.plot([e["t0"], e["t1"]], [y, y], color=kinds[e["kind"]]["edgecolors"], lw=3,
                            alpha=0.3 + 0.7 * frac, solid_capstyle="butt")
                else:
                    ax.scatter([e["t0"]], [y], marker="o", s=14 + 30 * frac, linewidths=1.1, zorder=3,
                               **kinds[e["kind"]])
                rows.append({"region": r, **e, "region_units": reg["units"]})
            ylabels.append(f"{r}: {R.labels.readable(c)}" if c == comps[0] else R.labels.readable(c))
            y += 1
        ax.axhline(y - 0.5, color=t["grid"], lw=0.8)
    ax.set_yticks(range(y))
    ax.set_yticklabels(ylabels, fontsize=7)
    ax.set_ylim(-0.6, y - 0.4)
    ax.set_xlim(sch["start"] - 0.3, w1 + 0.3)
    from matplotlib.ticker import FuncFormatter, MultipleLocator
    ax.xaxis.set_major_locator(MultipleLocator(1))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}"))
    from matplotlib.lines import Line2D
    hs = [Line2D([0], [0], marker="o", ls="", markerfacecolor="none", markeredgecolor=t["ink2"],
                 label="before delivery (every arm)"),
          Line2D([0], [0], marker="o", ls="", color=t["cat"][0], label="funded by the decision"),
          Line2D([0], [0], marker="o", ls="", markerfacecolor="none", markeredgecolor=t["cat"][1],
                 label="routine, not funded")]
    fig.legend(handles=hs, loc="lower left", bbox_to_anchor=(0.24, 0.0), ncol=3, fontsize=7.5)
    title = f"The decision delivers in {d0:g}–{d1:g}; outcomes are counted over {w0:g}–{w1:g}"
    cap = ("What is delivered when, per region and engine component, from the engine's own schedule; marker size "
           "and line opacity grow with the share of the region's units reached (counts in the table view).")
    return fig, title, cap, rows, "delivery timeline by region"


def fig_trajectories(R, t):
    tr = _trajectories_current(R)
    ser = tr["series"]
    metrics = tr["metrics"]
    sample = tr.get("sample") or {}
    n_units = sum(len(v) for v in sample.values())
    panels = ["total"] + sorted(ser.get("regions") or {})
    ncol = 4
    nrow = math.ceil(len(panels) / ncol)
    fig = _fig(t, 8.0, 1.55 * nrow * len(metrics) + 1.0)
    gs = fig.add_gridspec(nrow * len(metrics), ncol, left=0.07, right=0.99, top=1 - 0.75 / fig.get_figheight(),
                          bottom=0.06, hspace=0.75, wspace=0.28)
    d0, d1 = tr["delivery"]
    rows = []
    for mi, m in enumerate(metrics):
        meta = tr["metric_info"].get(m, {})
        for n, p in enumerate(panels):
            s = ser["total"] if p == "total" else ser["regions"][p]
            ax = fig.add_subplot(gs[mi * nrow + n // ncol, n % ncol])
            FS.style(ax, t, grid="y")
            ax.axvspan(d0, d1, color=t["band"], lw=0, zorder=0)
            ax.plot(s["t"], s["arms"]["comparator"][m], color=t["muted"], lw=1.4, label="comparator")
            ax.plot(s["t"], s["arms"]["plan"][m], color=t["cat"][0], lw=1.6, label="plan")
            tg = (s.get("target") or {}) if m == tr.get("calibration_metric") else {}
            if tg.get("value") is not None:
                ax.errorbar([tg["t"]], [tg["value"]], yerr=[[tg["value"] - tg["lo"]], [tg["hi"] - tg["value"]]],
                            fmt="o", color=t["ink"], ms=3.5, lw=1, capsize=0, zorder=4)
            k_units = len(s.get("units") or [])
            name = f"Purposive sample, {k_units} units" if p == "total" else f"{p}: {k_units} sampled units"
            ax.set_title(name + (f" ({meta.get('label', m)})" if n == 0 else ""), fontsize=7.5, loc="left",
                         color=t["ink"])
            FS.set_axis_format(ax, "y", "percent" if meta.get("units") == "share" else "number")
            ax.set_ylim(0, None)
            ax.tick_params(labelsize=6.5)
            for k in range(len(s["t"])):
                rows.append({"metric": m, "panel": p, "t": s["t"][k], "comparator": s["arms"]["comparator"][m][k],
                             "plan": s["arms"]["plan"][m][k]})
    from matplotlib.lines import Line2D
    lvl = next((s.get("target", {}).get("level") for s in [ser["total"]] if s.get("target")), None) or 0.95
    fig.legend(handles=[Line2D([0], [0], color=t["muted"], lw=1.4, label="comparator"),
                        Line2D([0], [0], color=t["cat"][0], lw=1.6, label="plan"),
                        Line2D([0], [0], marker="o", ls="", color=t["ink"], label=f"target ({lvl:.0%} interval)")],
               loc="upper right", bbox_to_anchor=(0.99, 1 - 0.2 / fig.get_figheight()), ncol=3, fontsize=7.5)
    title = (f"Modelled {' and '.join(tr['metric_info'].get(m, {}).get('label', m) for m in metrics)} in a purposive "
             f"sample: comparator and plan")
    cap = (f"A purposive sample of {n_units} units ({tr.get('per_region')} per region at the lowest, median and highest "
           f"comparator burden; {tr.get('seeds')} seeds), population-weighted within the sample: not a regional "
           f"estimate. Shaded: delivery period {d0:g}–{d1:g}.")
    return fig, title, cap, rows, "modelled trajectories in a purposive sample (diagnostic)", \
        {"checks": trajectory_checks(R, tr)}


def _frontier_xy(fr: dict, primary: str) -> tuple[list, list]:
    """The frontier's plotted points: solved envelopes only, on outcome averted."""
    pts = fr.get("points") or []
    return [float(p["envelope_usd"]) for p in pts], [float(p["averted"][primary]) for p in pts]


def _window_mean(t, v, w0: float, w1: float, step: float = 1 / 12, end: float | None = None) -> float:
    """Mean of a step series over the half-open window [w0, w1), each step weighted by its overlap."""
    from toolkit.engines.hooks import window_mean
    return window_mean(t, v, w0, w1, end=end, step=step)


def presentation_findings(run_dir: str, sidecar: dict) -> list[str]:
    """Files of a figure's presentation record (labels, boundaries, crosswalk) that changed since it was drawn: a
    path relative to the run dir or absolute; a missing or changed file is a finding (the contract check reads these
    beside the source hashes)."""
    from toolkit.analysis.artifacts import sha256
    out = []
    for e in (sidecar or {}).get("presentation") or []:
        if not isinstance(e, dict) or not e.get("path"):
            out.append(f"malformed presentation record {e!r}")
            continue
        pth = os.path.expanduser(str(e["path"]))
        pth = pth if os.path.isabs(pth) else os.path.join(e.get("run") or run_dir, pth)
        got = sha256(pth)
        if got is None:
            out.append(f"{e['path']} ({e.get('kind', 'presentation')}) is missing")
        elif got != e.get("sha256"):
            out.append(f"{e['path']} ({e.get('kind', 'presentation')}) changed after the figure was drawn")
    return out


FUNCS = {"data_coverage": fig_data_coverage, "unit_attributes": fig_unit_attributes, "parameters": fig_parameters,
         "design_coverage": fig_design_coverage, "burn_in": fig_burn_in, "seasonality": fig_seasonality,
         "stochastic_noise": fig_stochastic_noise, "decision_tests": fig_decision_tests, "comparison": fig_comparison,
         "headline": fig_headline, "allocation_map": fig_allocation, "stability_map": fig_stability,
         "burden_map": fig_burden, "calibration_fit": fig_calibration, "frontier": fig_frontier,
         "dollars": fig_dollars, "cost_per_outcome": fig_cost_per_outcome, "sensitivity": fig_sensitivity,
         "timeline": fig_timeline, "trajectories": fig_trajectories}


# --------------------------------------------------------------------------- rendering
def _offset_free(fig) -> bool:
    fig.canvas.draw()
    return all(not a.xaxis.get_offset_text().get_text() and not a.yaxis.get_offset_text().get_text()
               for a in fig.axes)


def _save(fig, out_dir, name, run_dir, sources, harness: bool, t, presentation=None) -> dict:
    import matplotlib.pyplot as plt
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, f"{name}.png")
    svg = os.path.join(out_dir, f"{name}.svg")
    fig.savefig(png, dpi=FS.DPI, facecolor=t["surface"])
    fig.savefig(svg, facecolor=t["surface"])
    plt.close(fig)
    fv = _fv()
    fv.validate_figure(png, source_data_paths=[os.path.join(run_dir, s) for s in sources])
    sp = png + ".provenance.json"
    with open(sp) as f:
        sc = json.load(f)
    sc.update(produced_by="harness" if harness else "harness_rerender", producer=PRODUCER,
              paths_relative_to="run_dir", png_path=os.path.relpath(png, run_dir) if harness else png,
              source_hashes={os.path.relpath(p, run_dir): h for p, h in (sc.get("source_hashes") or {}).items()})
    if not harness:
        sc["run_dir"] = run_dir
    sc["presentation"] = list(presentation or [])        # the unsigned labels/boundaries files used, with their sha
    with open(sp, "w") as f:
        json.dump(sc, f, indent=2, sort_keys=True)
    return {"png": png, "svg": svg}


def render(run_dir: str, out_dir: str | None = None, labels_overlay: dict | None = None,
           figure_data: dict | None = None, trajectories: dict | None = None, theme: str = "light",
           only=None, write_index: bool | None = None) -> dict:
    """Draw every figure that has its inputs. Returns {"figures": [entry], "not_produced": [{figure, reason}]}; with
    `out_dir` (offline) also writes <out_dir>/figures.yaml. Entries: path (run-relative in the harness), svg, table,
    title, caption, shows, sources, offset_free."""
    run_dir = os.path.abspath(run_dir)
    harness = out_dir is None
    dest = os.path.join(run_dir, "figures") if harness else os.path.abspath(out_dir)
    if not harness and os.path.commonpath([os.path.realpath(dest), os.path.realpath(run_dir)]) == \
            os.path.realpath(run_dir):
        raise ValueError("--out must be outside the run dir (the run's artifacts are not modified)")
    t = FS.setup(theme)
    figs, notp = [], []
    try:
        R = RunData(run_dir, labels_overlay, figure_data, trajectories)
    except Missing as e:
        return {"figures": [], "not_produced": [{"figure": n, "reason": str(e)} for n in ORDER]}
    tiers = tiers_for(R.spec)
    names = [n for k in ("data_model", "simulation", "decision") for n in tiers[k]]
    if not tiers["decision"] and (not only or any(TIER_OF.get(n) == "decision" for n in only)):
        kind = deliverable_kind(R.spec)
        notp.append({"figure": "decision_figures", "tier": "decision",
                     "reason": (f"no decision figure set declared for kind {kind} (toolkit/analysis/figures.py "
                                f"DECISION_SETS)" if kind else "the spec declares no deliverable.kind; decision "
                                "figures are chosen by it (DECISION_SETS), with no default")})
    for name in [n for n in names if not only or n in only]:
        R.used = []
        try:
            got = FUNCS[name](R, t)
        except Missing as e:
            notp.append({"figure": name, "reason": str(e), "tier": TIER_OF[name]})
            continue
        fig, title, cap, rows, shows = got[:5]
        extra = got[5] if len(got) > 5 else {}
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX - 1].rsplit(" ", 1)[0] + "…"
        FS.title(fig, title, t)
        if extra.get("credit"):
            fig.text(0.995, 0.004, extra["credit"], ha="right", va="bottom", fontsize=6.5, color=t["ink2"])
        clean = _offset_free(fig)
        sources = list(R.used) + (["decision_spec.yaml", os.path.relpath(os.path.join(run_dir, R.spec["decision_units"]["csv"]), run_dir)])
        sources = [s for s in sources if os.path.exists(os.path.join(run_dir, s))]
        files = _save(fig, dest, name, run_dir, sources, harness, t, R.presentation_record())
        table = os.path.join(dest, f"{name}.csv")
        _write_csv(table, rows)
        cap = cap if len(cap) <= CAPTION_MAX else cap[:CAPTION_MAX - 1].rsplit(" ", 1)[0] + "…"
        cap += extra.pop("caption_note", "")
        rel = (lambda p: os.path.relpath(p, run_dir)) if harness else (lambda p: p)
        figs.append(dict({"figure": name, "path": rel(files["png"]), "svg": rel(files["svg"]), "table": rel(table),
                          "title": title, "caption": cap, "shows": shows, "sources": sources, "offset_free": clean,
                          "theme": theme, "tier": TIER_OF[name],
                          "group": "decision" if TIER_OF[name] == "decision" else "diagnostics",
                          "presentation": R.presentation_record()}, **extra))
    res = {"figures": figs, "not_produced": notp, "tiers": {k: list(v) for k, v in tiers_for(R.spec).items()},
           "groups": {"decision": list(tiers_for(R.spec)["decision"]),
                      "diagnostics": [n for k in ("data_model", "simulation") for n in tiers_for(R.spec)[k]]}}
    try:
        res["overviews"] = contact_sheets(res, dest, run_dir, harness) if figs else {}
    except Exception as e:  # noqa: BLE001 - an overview is a convenience; its failure is recorded, not fatal
        res["overviews"] = {"error": f"{type(e).__name__}: {e}"}
    if not harness or write_index:
        import yaml
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "figures.yaml"), "w") as f:
            yaml.safe_dump({"run_dir": run_dir, "producer": PRODUCER, **res}, f, sort_keys=False, width=120)
    return res


# --------------------------------------------------------------------------- comparison
def _run_name(d):
    b = os.path.basename(os.path.normpath(d))
    return os.path.basename(os.path.dirname(os.path.normpath(d))) if b[:4].isdigit() else b


def _compare_draw(Rs, run_dirs, out_dir, names, t) -> dict:
    """compare()'s figure, table, index and provenance (the checks are in compare)."""
    names = names or [_run_name(d) for d in run_dirs]
    if len(names) != len(Rs):
        raise ValueError("give one name per run")
    r0 = Rs[0]
    for R in Rs[1:]:
        if set(R.ids) != set(r0.ids) or R.primary != r0.primary or R.components != r0.components:
            raise ValueError(f"{R.dir}: not the same project (units, components or objective outcome differ)")
    pts = [R.art("point") for R in Rs]
    uqs = [R.art("uncertainty") for R in Rs]
    diffs = spec_differences(Rs)
    L = r0.labels
    olab = L.outcome(r0.primary)
    U = L.unit_title()
    comps = [c for c in r0.components if any(p["mix"].get(c, 0) for p in pts)]
    # packages reconciled by component set across runs (names may mean different sets in different specs)
    keyed = []
    for R, p in zip(Rs, pts):
        cnt = {}
        for name, n in (p.get("packages") or {}).items():
            cs = tuple(sorted(R.pk.get(name, ())))
            cnt[cs] = cnt.get(cs, 0) + n
        keyed.append(cnt)
    sets = sorted({cs for c in keyed for cs in c}, key=lambda cs: -sum(c.get(cs, 0) for c in keyed))
    levels = [float(u["fixed_plan"][r0.primary].get("level", 0.95)) for u in uqs]
    lvl_txt = f"{levels[0]:.0%}" if len(set(levels)) == 1 else "/".join(f"{x:.0%}" for x in levels)
    nr = len(Rs)
    row_h = 0.16 * nr + 0.12
    h1, h2, h3 = 0.36 * nr + 0.25, row_h * len(comps), row_h * len(sets)
    H = h1 + h2 + h3 + 0.6 + 3 * 0.5 + 0.35
    fig = _fig(t, 7.8, H)
    y = H - 0.6 - 0.3
    a1 = fig.add_axes([0.36, (y - h1) / H, 0.44, h1 / H])
    y -= h1 + 0.6
    a2 = fig.add_axes([0.36, (y - h2) / H, 0.6, h2 / H])
    y -= h2 + 0.6
    a3 = fig.add_axes([0.36, (y - h3) / H, 0.6, h3 / H])
    for a in (a1, a2, a3):
        FS.style(a, t, grid="x")
    rows = []
    for j, (n, p, u) in enumerate(zip(names, pts, uqs)):
        yy = nr - 1 - j
        q = u["fixed_plan"][r0.primary]
        c = t["cat"][j]
        a1.plot([q["ci_low"], q["ci_high"]], [yy, yy], color=c, lw=2.4, solid_capstyle="round")
        a1.scatter([p["averted"][r0.primary]], [yy], s=44, color=c, edgecolors=t["surface"], linewidths=1.4, zorder=3)
        a1.text(1.02, yy, f"{FS.fmt(p['averted'][r0.primary])} ({FS.fmt(q['ci_low'])}–{FS.fmt(q['ci_high'])})",
                transform=a1.get_yaxis_transform(), va="center", fontsize=7.5)
        rows.append({"run": n, "point": p["averted"][r0.primary], "ci_low": q["ci_low"], "ci_high": q["ci_high"],
                     "level": q.get("level", 0.95), "cost": p["cost_allocated"],
                     "window": json.dumps(Rs[j].spec["objective"]["horizon"]),
                     **{f"units_{c_}": p["mix"].get(c_, 0) for c_ in comps},
                     **{f"package_{'+'.join(cs) or 'none'}": keyed[j].get(cs, 0) for cs in sets}})
    a1.set_yticks(range(nr))
    a1.set_yticklabels(names[::-1], fontsize=8)
    a1.set_ylim(-0.6, nr - 0.4)
    FS.set_axis_format(a1, "x")
    a1.set_title(f"{olab} averted, point and {lvl_txt} UI", fontsize=8.5, loc="left", color=t["ink"])
    bh = 0.8 / nr
    for ax, keys, get, lab in ((a2, comps, lambda j, k: pts[j]["mix"].get(k, 0), L.component),
                               (a3, sets, lambda j, k: keyed[j].get(k, 0),
                                lambda k: textwrap.shorten(L.package(k), 44, placeholder="…"))):
        for j in range(nr):
            ys = [len(keys) - 1 - yk + ((nr - 1) / 2 - j) * bh for yk in range(len(keys))]
            ax.barh(ys, [get(j, k) for k in keys], height=bh * 0.9, color=t["cat"][j], label=names[j])
        ax.set_yticks(range(len(keys)))
        ax.set_yticklabels([lab(k) for k in keys][::-1], fontsize=7.5)
        ax.set_ylim(-0.6, len(keys) - 0.4)
        FS.set_axis_format(ax, "x", "count")
    a2.set_title(f"{U} per component", fontsize=8.5, loc="left", color=t["ink"])
    a3.set_title(f"{U} per package (matched by components)", fontsize=8.5, loc="left", color=t["ink"])
    a2.legend(loc="lower right", fontsize=7.5)
    d = [p["averted"][r0.primary] for p in pts]
    title = (f"{olab} averted: " + " vs ".join(f"{FS.fmt(x)} ({n})" for x, n in zip(d, names)))
    if diffs:
        title += f"; specs differ in {diffs[0]}" + (" and more" if len(diffs) > 1 else "")
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1].rsplit(" ", 1)[0] + "…"
    FS.title(fig, title, t)
    os.makedirs(out_dir, exist_ok=True)
    png, svg = os.path.join(out_dir, "comparison.png"), os.path.join(out_dir, "comparison.svg")
    clean = _offset_free(fig)
    fig.savefig(png, dpi=FS.DPI, facecolor=t["surface"])
    fig.savefig(svg, facecolor=t["surface"])
    import matplotlib.pyplot as plt
    plt.close(fig)
    _write_csv(os.path.join(out_dir, "comparison.csv"), rows)
    # provenance: every run's sources (absolute: the runs differ), the presentation files, the tier
    fv = _fv()
    srcs = [os.path.join(R.dir, rel) for R in Rs for rel in ("decision_spec.yaml", os.path.join("analysis", "point.yaml"),
                                                             os.path.join("analysis", "uncertainty.yaml"),
                                                             os.path.join("analysis", "engine.yaml"),
                                                             os.path.relpath(os.path.join(R.dir, R.spec["decision_units"]
                                                                                          ["csv"]), R.dir))
            if os.path.exists(os.path.join(R.dir, rel))]
    srcs += [p if os.path.isabs(p) else os.path.join(R.dir, p) for R in Rs
             for p in (os.path.expanduser(str(e["path"])) for e in R.presentation_record())]
    fv.validate_figure(png, source_data_paths=srcs)
    sp = png + ".provenance.json"
    sc = json.load(open(sp))
    pres = [dict(e, run=R.dir) for R in Rs for e in R.presentation_record()]
    sc.update(produced_by="harness_compare", producer=PRODUCER, tier="decision", runs=[R.dir for R in Rs],
              presentation=pres, paths_relative_to="absolute")
    with open(sp, "w") as f:
        json.dump(sc, f, indent=2, sort_keys=True)
    cap = (f"Each run's point plan: headline with its {lvl_txt} UI over that run's declared sources, and its units per "
           f"component and per package (packages matched by their components)."
           + (" The package definitions differ between the runs; packages are matched by their components."
              if "package definitions" in diffs else "")
           + (f" The specs differ in: {', '.join(diffs)}." if diffs else
              " The specs agree on window, envelope, comparator, sources and package definitions."))
    entry = {"figure": "comparison", "path": png, "svg": svg, "table": os.path.join(out_dir, "comparison.csv"),
             "title": title, "caption": cap, "runs": [os.path.abspath(d) for d in run_dirs], "names": names,
             "spec_differences": diffs, "offset_free": clean, "group": "decision", "tier": "decision",
             "provenance": sp, "presentation": pres}
    import yaml
    with open(os.path.join(out_dir, "figures.yaml"), "w") as f:
        yaml.safe_dump({"producer": PRODUCER, "figures": [entry], "not_produced": []}, f, sort_keys=False, width=120)
    return entry


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m toolkit.analysis.figures")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="re-render a run's figures from its saved artifacts into --out")
    r.add_argument("run_dir")
    r.add_argument("--out", required=True)
    r.add_argument("--recompute", action="store_true", help="rebuild figure_data from the stored batch (no engine runs)")
    r.add_argument("--labels", help="an unsigned presentation file: labels and/or boundaries (overrides the run "
                                    "dir's presentation.yaml id by id)")
    r.add_argument("--only", help="comma-separated figure names")
    r.add_argument("--figure-data", help="a figure_data YAML written by an earlier --recompute (its data block)")
    r.add_argument("--trajectories", help="a trajectories YAML (analysis/trajectories.yaml format)")
    r.add_argument("--theme", default="light", choices=sorted(FS.THEMES))
    c = sub.add_parser("compare", help="headline and counts side by side across runs")
    c.add_argument("run_dirs", nargs="+")
    c.add_argument("--out", required=True)
    c.add_argument("--names", help="comma-separated run names")
    c.add_argument("--labels")
    c.add_argument("--theme", default="light", choices=sorted(FS.THEMES))
    a = ap.parse_args(argv)
    import yaml
    ov = a.labels or None                       # a presentation file path; RunData loads and checks it
    if a.cmd == "compare":
        e = compare(a.run_dirs, a.out, a.names.split(",") if a.names else None, ov, a.theme)
        print(json.dumps({"comparison": e["path"]}))
        return 0
    fd = yaml.safe_load(open(a.figure_data))["data"] if a.figure_data else None
    tj = yaml.safe_load(open(a.trajectories))["data"] if a.trajectories else None
    if fd is None and a.recompute and not os.path.exists(os.path.join(a.run_dir, "analysis", "figure_data.yaml")):
        from toolkit.analysis import figure_data as FD
        os.makedirs(a.out, exist_ok=True)
        try:
            A, plan, out0, ben0, cost0, z0 = FD.rebuild(a.run_dir)
            uq = yaml.safe_load(open(os.path.join(a.run_dir, "analysis", "uncertainty.yaml")))["data"]
            from toolkit.analysis.artifacts import plain
            fd = plain(FD.compute(A, plan, out0, ben0, cost0, uq, z0))
            with open(os.path.join(a.out, "figure_data.yaml"), "w") as f:
                yaml.safe_dump({"producer": FD.PRODUCER, "recomputed_from": os.path.abspath(a.run_dir), "data": fd},
                               f, sort_keys=False, width=120)
        except FD.RebuildRefused as e:
            print(f"recompute refused: {e}", file=sys.stderr)
            fd = None
    res = render(a.run_dir, a.out, ov, fd, tj, theme=a.theme, only=a.only.split(",") if a.only else None)
    print(json.dumps({"produced": [f["figure"] for f in res["figures"]], "not_produced": res["not_produced"]}, indent=1))
    return 0


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
    yaml.safe_dump(spec, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
    if labels:      # display names live in the unsigned presentation file beside the spec (owner, 2026-11-02)
        yaml.safe_dump({"labels": {"components": {"ca": "Alpha service", "cb": "Beta service"},
                                   "parameters": {"p_eff": "Effect size"},
                                   "outcomes": {"dalys": "DALYs", "cases": "Cases"}}},
                       open(os.path.join(d, "presentation.yaml"), "w"), sort_keys=False)
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
    art("decision_tests", {"tests": [{"test_id": "prior_range_p_eff", "verdict": "CHANGES_PLAN", "threshold_pct": 10.0,
                                      "share_pct": 30.0, "status": "run", "statement": "toy"},
                                     {"test_id": "prior_range_p_dur", "verdict": "NO_CHANGE", "threshold_pct": 10.0,
                                      "share_pct": 5.0, "status": "run", "statement": "toy"}]})
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
        fdp = os.path.join(d, "analysis", "figure_data.yaml")
        doc = yaml.safe_load(open(fdp))
        for pt in doc["data"]["frontier"]["points"]:
            pt["averted"] = {"dalys": pt["objective"]}
        yaml.safe_dump(doc, open(fdp, "w"), sort_keys=False)
    # tier-1 inputs: bound engine attributes (one column, one monthly profile), the emulator design, parameter
    # ranges and kinds, the drawn priors, and a derivation record
    import math as _m
    up = os.path.join(d, "units.csv")
    lines = open(up).read().splitlines()
    out = [lines[0] + ",cov," + ",".join(f"m{k:02d}" for k in range(1, 13))]
    for i, line in enumerate(lines[1:]):
        prof = [1 + 0.8 * _m.sin(2 * _m.pi * (k + i % 3) / 12) for k in range(12)]
        out.append(line + f",{0.2 + 0.6 * ((i * 5) % n) / n:.3f}," + ",".join(f"{v:.3f}" for v in prof))
    open(up, "w").write("\n".join(out) + "\n")
    sp = yaml.safe_load(open(os.path.join(d, "decision_spec.yaml")))
    sp["decision_units"]["engine_attributes"] = {"access": {"column": "cov"},
                                                 "profile": {"columns": [f"m{k:02d}" for k in range(1, 13)]},
                                                 "level": {"value": 0.5, "source": "toy assumption"}}
    sp["analysis"] = dict(sp.get("analysis") or {}, emulator={"features": ["cov"]})
    sp["deliverable"]["kind"] = "package_per_unit"
    yaml.safe_dump(sp, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
    os.makedirs(os.path.join(d, "analysis", "batch"), exist_ok=True)
    pts = [{"point": k, "unit_id": f"u{(k * 7) % n}", "z": -1.5 + 3.0 * k / 39, "seed": 1000 + k,
            "theta": {"p_eff": 0.3 + 0.4 * ((k * 3) % 40) / 39, "p_dur": 480 + 380 * ((k * 11) % 40) / 39},
            "holdout": k % 10 == 0} for k in range(40)]
    json.dump({"points": pts, "fitted_names": ["p_eff"], "prior_names": ["p_dur"], "packages": list(pk)},
              open(os.path.join(d, "analysis", "batch", "design.json"), "w"))
    eng = yaml.safe_load(open(os.path.join(d, "analysis", "engine.yaml")))
    eng["data"]["parameters"] = [
        {"name": "p_eff", "value": 0.5, "kind": "fitted", "units": "share", "lo": 0.3, "hi": 0.7, "source": "fitted"},
        {"name": "p_dur", "value": 600.0, "kind": "fixed", "units": "days", "lo": 480.0, "hi": 860.0,
         "source": "a published review"},
        {"name": "p_cost", "value": 1.0, "kind": "scenario", "units": "", "lo": None, "hi": None, "source": "toy"}]
    yaml.safe_dump(eng, open(os.path.join(d, "analysis", "engine.yaml"), "w"), sort_keys=False)
    calp = os.path.join(d, "analysis", "calibration.yaml")
    cal = yaml.safe_load(open(calp))
    cal["data"]["units"] = {f"u{i}": {"z": -1.0 + 2.0 * i / n, "clamped": i == 5} for i in range(n)}
    yaml.safe_dump(cal, open(calp, "w"), sort_keys=False)
    rp = os.path.join(d, "uncertainty_report.yaml")
    rep = yaml.safe_load(open(rp)) if os.path.exists(rp) else {}
    rep["parameters_drawn"] = [{"name": "p_dur", "source_kind": "priors", "dist": "uniform", "range": [480.0, 860.0],
                                "source": "a published review"},
                               {"name": "p_eff", "source_kind": "engine_parameters", "dist": "uniform",
                                "range": [0.3, 0.7], "source": "fitted"}]
    yaml.safe_dump(rep, open(rp, "w"))
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    yaml.safe_dump({"derivations": [{"id": "units", "script": "data/derive/units.py",
                                     "inputs": ["data/inputs/a.csv", "data/inputs/b.csv"], "outputs": ["units.csv"],
                                     "columns": {"uses": {"data/inputs/a.csv": ["uid", "pop"],
                                                          "data/inputs/b.csv": ["cov", "m*"]},
                                                 "ignores": {"data/inputs/a.csv": {"area": "not used"}}}}]},
                   open(os.path.join(d, "data", "derivations.yaml"), "w"), sort_keys=False)
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

        # ---------------- Codex r1
        def edit(dd, kind, fn, rel=None):
            pth = os.path.join(dd, rel or os.path.join("analysis", f"{kind}.yaml"))
            doc = yaml.safe_load(open(pth))
            fn(doc["data"] if "data" in doc else doc)
            yaml.safe_dump(doc, open(pth, "w"), sort_keys=False)

        def one(dd, name, out):
            r = render(dd, os.path.join(base, out), only=[name])
            e = {f["figure"]: f for f in r["figures"]}.get(name)
            return e, {x["figure"]: x["reason"] for x in r["not_produced"]}.get(name)
        # P1-1: no fabricated fit; the title follows the actual residuals and clamping
        e, _ = one(d, "calibration_fit", "c1")
        ok(e and e.get("panels") == ["targets"] and "exactly" not in e["title"] and "reproduces" not in e["title"],
           f"F16 without modelled values the fit panel is withheld and nothing claims a fit: {e and e['title']}")
        dc = toy_run(os.path.join(base, "runc"))
        ids = [f"u{i}" for i in range(24)]
        edit(dc, "figure_data", lambda x: x.update(calibration={"metric": "prev", "units": {
            u: {"target": 0.1 + 0.3 * (i % 7) / 7, "modelled": 0.1 + 0.3 * (i % 7) / 7 + (0.05 if i == 3 else 0.0)}
            for i, u in enumerate(ids)}}))
        edit(dc, "calibration", lambda x: x["diagnostics"].update(clamped_units=2))
        e, _ = one(dc, "calibration_fit", "c2")
        ok(e and e.get("panels") == ["fit", "residuals"] and "2 clamped" in e["title"] and "exactly" not in e["title"],
           f"F16b with modelled values the title states the largest residual and the clamped units: {e and e['title']}")
        # P1-4 and P2-8: the frontier plots solved points only, on outcome averted
        df = toy_run(os.path.join(base, "runf"))
        edit(df, "figure_data", lambda x: (x["frontier"].update(infeasible=[{"fraction": 0.25, "reason": "infeasible"}]),
                                           [pt.update(averted={"dalys": pt["objective"]}, objective=2 * pt["objective"])
                                            for pt in x["frontier"]["points"]]))
        fdd = yaml.safe_load(open(os.path.join(df, "analysis", "figure_data.yaml")))["data"]
        xs, ys = _frontier_xy(fdd["frontier"], "dalys")
        ok(len(xs) == len(fdd["frontier"]["points"]) and 0.0 not in ys
           and ys == [pt["averted"]["dalys"] for pt in fdd["frontier"]["points"]],
           "F17 the frontier draws only solved points (no unsolved origin), on outcome averted (F20)")
        e, _ = one(df, "frontier", "f1")
        ok(e and "infeasible" in e["caption"], f"F17b infeasible envelopes are named in the caption: {e and e['caption']}")
        # P1-5: a plan that spends nothing
        dz = toy_run(os.path.join(base, "runz"))
        edit(dz, "figure_data", lambda x: x["dollars"].update(allocated_usd=0.0, by_component={"ca": 0.0, "cb": 0.0},
                                                               by_region={r: {"ca": 0.0, "cb": 0.0} for r in
                                                                          ("Z0", "Z1", "Z2")}))
        e, why = one(dz, "dollars", "z1")
        ok(e and "unspent" in e["title"], f"F18 a zero-spend plan draws fixed costs and unspent funds: {e or why}")
        # P1-7: stale trajectories are not plotted
        dt = toy_run(os.path.join(base, "runt"))
        tj = {"schema": "analysis.trajectories/1", "inputs": {"analysis/point.yaml": "0" * 64}, "data": {
            "status": "ok", "engine": {"name": "toy"}, "sample": {"Z0": ["u0"]}, "per_region": 1, "seeds": 1,
            "metrics": ["prev"], "metric_info": {"prev": {"label": "prevalence", "units": "share"}},
            "delivery": [1.0, 2.0], "window": [1.0, 3.0], "calibration_metric": "prev",
            "series": {"total": {"t": [0.0, 1.0], "arms": {"comparator": {"prev": [0.5, 0.5]},
                                                            "plan": {"prev": [0.5, 0.4]}}, "target": {}},
                       "regions": {}}}}
        yaml.safe_dump(tj, open(os.path.join(dt, "analysis", "trajectories.yaml"), "w"))
        e, why = one(dt, "trajectories", "t1")
        ok(e is None and why and "stale" in why, f"F19 trajectories computed from other inputs are not plotted: {why}")
        # P2-11: every component counts in the dollars summary
        d9 = toy_run(os.path.join(base, "run9"), labels=False)     # its components are replaced below
        comps9 = [f"k{j}" for j in range(9)]
        edit(d9, "figure_data", lambda x: x["dollars"].update(
            allocated_usd=900.0, by_component={c: 100.0 for c in comps9},
            by_region={r: {c: 100.0 / 3 for c in comps9} for r in ("Z0", "Z1", "Z2")}))
        sp9 = yaml.safe_load(open(os.path.join(d9, "decision_spec.yaml")))
        sp9["options"]["components"] = {c: {} for c in comps9}
        yaml.safe_dump(sp9, open(os.path.join(d9, "decision_spec.yaml"), "w"), sort_keys=False)
        edit(d9, "point", lambda x: x.update(mix={c: 3 for c in comps9}))
        e, why = one(d9, "dollars", "d9")
        rows9 = list(csv.DictReader(open(e["table"]))) if e else []
        allrow = [r for r in rows9 if r.get("region") == "ALL"]
        ok(e and allrow and all(float(allrow[0][c]) == 100.0 for c in comps9) and "11%" in e["title"],
           f"F21 the dollars summary covers all nine components: {e and e['title']} {why}")
        # P2-12: free, harmful and undefined packages are shown, not dropped
        dp = toy_run(os.path.join(base, "runp"))

        def pk_edit(x):
            ks = sorted(x["by_package"])
            x["by_package"][ks[0]].update(cost=0.0, usd_per_outcome=0.0)
            x["by_package"][ks[1]].update(averted=-5.0, usd_per_outcome=None, usd_per_outcome_interval=None)
            x["by_package"][ks[2]].update(averted=0.0, usd_per_outcome=None, usd_per_outcome_interval=None)
        edit(dp, "figure_data", pk_edit)
        e, why = one(dp, "cost_per_outcome", "p1")
        st = {r.get("status") for r in csv.DictReader(open(e["table"]))} if e else set()
        ok(e and {"free", "harmful", "undefined"} <= st, f"F22 free, harmful and undefined packages are shown: {st} {why}")
        # P2-13: verdict, share and threshold from one calculation
        ds = toy_run(os.path.join(base, "runs"))
        edit(ds, "decision_tests", lambda x: x["tests"][0].update(share_pct=30.0, threshold_pct=25.0))
        sps = yaml.safe_load(open(os.path.join(ds, "decision_spec.yaml")))
        sps["analysis"] = {"share_threshold": 0.04}
        yaml.safe_dump(sps, open(os.path.join(ds, "decision_spec.yaml"), "w"), sort_keys=False)
        e, _ = one(ds, "sensitivity", "s1")
        sr = {r["name"]: r for r in csv.DictReader(open(e["table"]))}
        ok(float(sr["p_eff"]["share_units_changed"]) == 0.30 and float(sr["p_eff"]["threshold"]) == 0.25
           and float(sr["p_cost"]["threshold"]) == 0.04 and sr["p_cost"]["changes_plan"] == "True",
           f"F23 each marker's verdict, share and threshold come from one source: "
           f"{[(k, v['share_units_changed'], v['threshold'], v['changes_plan']) for k, v in sr.items()]}")
        # P2-14: package definitions that differ across runs are reconciled and disclosed
        dk = toy_run(os.path.join(base, "runk"))
        spk = yaml.safe_load(open(os.path.join(dk, "decision_spec.yaml")))
        spk["options"]["packages"] = {"none": [], "ca": ["cb"], "cb": ["ca"], "ca+cb": ["ca", "cb"]}
        yaml.safe_dump(spk, open(os.path.join(dk, "decision_spec.yaml"), "w"), sort_keys=False)
        ek = compare([d, dk], os.path.join(base, "cmpk"), names=["A", "K"])
        ok("package definitions differ" in ek["caption"], f"F24 differing package definitions are disclosed: {ek['caption']}")
        # P2-15: the stored interval level, not a hardcoded 95%
        dl = toy_run(os.path.join(base, "runl"))
        edit(dl, "uncertainty", lambda x: [v.update(level=0.9) for v in x["fixed_plan"].values()])
        edit(dl, "figure_data", lambda x: x["replay"].update(level=0.9))
        rl = render(dl, os.path.join(base, "l1"), only=["headline", "cost_per_outcome"])
        el = {f["figure"]: f for f in rl["figures"]}
        el2 = compare([dl, dl], os.path.join(base, "cmpl"), names=["X", "Y"])
        ok("90% UI" in el["headline"]["title"] and "90%" in el["cost_per_outcome"]["caption"]
           and "95%" not in el["cost_per_outcome"]["caption"] and "90%" in el2["caption"] and "95%" not in el2["caption"],
           "F25 interval levels come from the stored artifacts")
        # ---------------- owner: three tiers (data and model, simulation, decision)
        rt = render(d, os.path.join(base, "tiers"))
        idx_t = yaml.safe_load(open(os.path.join(base, "tiers", "figures.yaml")))
        ent_t = {f["figure"]: f for f in rt["figures"]}
        np_t = {x["figure"]: x for x in rt["not_produced"]}
        ok(all(f.get("tier") in ("data_model", "simulation", "decision") for f in rt["figures"])
           and all(x.get("tier") in ("data_model", "simulation", "decision") for x in rt["not_produced"])
           and set(idx_t.get("tiers") or {}) == {"data_model", "simulation", "decision"},
           "TT1 every figure and not-produced entry records its tier; figures.yaml lists the three tiers")
        want1 = {"data_coverage", "unit_attributes", "parameters", "calibration_fit", "design_coverage"}
        ok(want1 <= {n for n, f in ent_t.items() if f["tier"] == "data_model"},
           f"TT2 tier 1 (data and model) is drawn from the run's own inputs: "
           f"{sorted(n for n, f in ent_t.items() if f['tier'] == 'data_model')} / "
           f"{ {k: v['reason'][:60] for k, v in np_t.items() if v['tier'] == 'data_model'} }")
        cov = list(csv.DictReader(open(ent_t["data_coverage"]["table"]))) if "data_coverage" in ent_t else []
        ok(any(r.get("attribute") == "profile" and "m01" in (r.get("binding") or "") for r in cov)
           and any(r.get("input") == "data/inputs/b.csv" for r in cov),
           "TT2b data coverage lists each bound attribute's binding and each staged input's columns")
        pr = list(csv.DictReader(open(ent_t["parameters"]["table"]))) if "parameters" in ent_t else []
        ok({r["name"]: r["kind"] for r in pr if r.get("name")}.items() >= {"p_eff": "fitted", "p_dur": "prior"}.items(),
           f"TT2c parameters show fitted against prior-drawn values with their ranges: {pr[:3]}")
        # a spec-declared engine parameter value (toolkit/spec/engine_parameters.py): shown as "spec value", with
        # the card value it replaces marked, even when the card gives the parameter no range
        import shutil
        dsv = os.path.join(base, "run_spec_value")
        shutil.copytree(d, dsv)
        edit(dsv, "engine", lambda x: x.update(spec_values={"p_cost": {
            "value": 2.0, "units": "", "card_value": 1.0, "card_range": None, "outside_card_range": False,
            "source": "toy source", "reason": "toy reason", "uncertainty": "held fixed at the spec value"}}))
        rsv = render(dsv, os.path.join(base, "spec_value"), only=["parameters"])
        esv = {f["figure"]: f for f in rsv["figures"]}.get("parameters") or {}
        prs = {x["name"]: x for x in csv.DictReader(open(esv["table"]))} if esv else {}
        ok(prs.get("p_cost", {}).get("kind") == "spec value" and float(prs["p_cost"]["value"]) == 2.0
           and float(prs["p_cost"]["card_value"]) == 1.0 and "spec value" in esv.get("caption", "")
           and prs.get("p_eff", {}).get("kind") == "fitted",
           f"TT2e a spec value is shown as such, with the card value it replaces: {prs.get('p_cost')}")
        # review round 2 (R2-5): a spec value drawn over its own range keeps that range apart from the card's
        edit(dsv, "engine", lambda x: x.update(spec_values={"p_dur": {
            "value": 300.0, "units": "days", "card_value": 600.0, "card_range": [480.0, 860.0],
            "outside_card_range": True, "source": "toy source", "reason": "toy reason",
            "uncertainty": "drawn over [250, 350] (uniform)"}}))
        edit(dsv, None, lambda x: x.update(parameters_drawn=[
            {"name": "p_dur", "source_kind": "priors", "dist": "uniform", "range": [250.0, 350.0], "source": "toy"}]),
             rel="uncertainty_report.yaml")
        rsv = render(dsv, os.path.join(base, "spec_value2"), only=["parameters"])
        esv = {f["figure"]: f for f in rsv["figures"]}.get("parameters") or {}
        prs = {x["name"]: x for x in csv.DictReader(open(esv["table"]))} if esv else {}
        pd_ = prs.get("p_dur", {})
        ok(pd_.get("kind") == "spec value" and pd_.get("card_lo") == "480.0" and pd_.get("card_hi") == "860.0"
           and pd_.get("draw_lo") == "250.0" and pd_.get("draw_hi") == "350.0",
           f"TT2f the card range and the spec value's own uncertainty range are kept apart: {pd_}")
        dsg = ent_t.get("design_coverage") or {}
        ok(dsg and "40 design points" in dsg.get("caption", ""), f"TT2d design coverage counts the design points: {dsg}")
        sim = {"timeline", "trajectories", "burn_in", "seasonality", "stochastic_noise"}
        ok(sim <= {n for n, x in np_t.items() if x["tier"] == "simulation"}
           and "run_series" in np_t["burn_in"]["reason"] and "trajectory" in np_t["stochastic_noise"]["reason"],
           f"TT3 tier 2 figures without their inputs are not produced, each with its reason: "
           f"{ {k: np_t[k]['reason'][:50] for k in sim if k in np_t} }")
        ok("decision_tests" in ent_t and ent_t["decision_tests"]["tier"] == "decision"
           and "1 of 2" in ent_t["decision_tests"]["title"],
           f"TT4 the decision tests are a decision figure: {ent_t.get('decision_tests', {}).get('title')}")
        ov = idx_t.get("overviews") or {}
        ok(all(os.path.exists(ov.get(k, "")) for k in ("data_model", "decision")),
           f"TT5 one overview contact sheet per tier with figures: {ov}")
        du = toy_run(os.path.join(base, "runu"))
        spu = yaml.safe_load(open(os.path.join(du, "decision_spec.yaml")))
        spu["deliverable"]["kind"] = "threshold_rule"
        yaml.safe_dump(spu, open(os.path.join(du, "decision_spec.yaml"), "w"), sort_keys=False)
        ru = render(du, os.path.join(base, "unk"))
        npu = [x for x in ru["not_produced"] if x["tier"] == "decision"]
        ok(not [f for f in ru["figures"] if f["tier"] == "decision"] and npu
           and "no decision figure set declared for kind threshold_rule" in npu[0]["reason"],
           f"TT6 an undeclared deliverable kind gets no decision figures and says why: {npu}")
        # ---------------- owner: unsigned presentation file (labels, boundaries)
        sc = json.load(open(os.path.join(d, ents["sensitivity"]["path"]) + ".provenance.json"))
        ok((sc.get("presentation") or [{}])[0].get("path") == "presentation.yaml"
           and len((sc.get("presentation") or [{}])[0].get("sha256") or "") == 64,
           f"PL1 each figure's provenance records the presentation file and its sha: {sc.get('presentation')}")
        dm = toy_run(os.path.join(base, "runm"))
        open(os.path.join(dm, "presentation.yaml"), "w").write("labels: [not, a, mapping]\n")
        try:
            render(dm, os.path.join(base, "m1"))
            ok(False, "PL2 a malformed presentation file fails closed")
        except ValueError as e:
            ok("presentation.yaml" in str(e), f"PL2 a malformed presentation file fails closed: {e}")
        open(os.path.join(dm, "presentation.yaml"), "w").write("labels:\n  components: {zz: Zed}\n")
        try:
            render(dm, os.path.join(base, "m2"))
            ok(False, "PL2b labels for ids the spec does not have fail closed")
        except ValueError as e:
            ok("zz" in str(e), f"PL2b labels for ids the spec does not have fail closed: {e}")
        ovp = os.path.join(base, "over.yaml")
        yaml.safe_dump({"labels": {"components": {"ca": "Overlay A"}}}, open(ovp, "w"))
        r = subprocess.run([sys.executable, "-m", "toolkit.analysis.figures", "render", d, "--out",
                            os.path.join(base, "ov"), "--labels", ovp, "--only", "cost_per_outcome"],
                           cwd=ROOT, capture_output=True, text=True)
        cpo2 = open(os.path.join(base, "ov", "cost_per_outcome.csv")).read() if r.returncode == 0 else ""
        ok("Overlay A" in cpo2 and "Beta service" in cpo2,
           f"PL3 a --labels file overrides the run dir's presentation file, id by id: {r.stderr[-200:]}")

        # boundaries: GeoJSON polygons matched to units directly or through a crosswalk
        def geo(dd, ids, extra=2, key="code"):
            feats = []
            for k, u in enumerate(ids + [f"ctx{j}" for j in range(extra)]):
                x0, y0 = (k % 6) * 1.0, (k // 6) * 1.0
                ring = [[x0, y0], [x0 + 1, y0], [x0 + 1, y0 + 1], [x0, y0 + 1], [x0, y0]]
                gtype, coords = (("MultiPolygon", [[ring]]) if k % 2 else ("Polygon", [ring]))
                feats.append({"type": "Feature", "properties": {key: f"S-{u}"},
                              "geometry": {"type": gtype, "coordinates": coords}})
            json.dump({"type": "FeatureCollection", "features": feats}, open(os.path.join(dd, "b.geojson"), "w"))
        db = toy_run(os.path.join(base, "runb"))
        uids = [f"u{i}" for i in range(24)]
        geo(db, uids)
        with open(os.path.join(db, "xw.csv"), "w") as fh:
            fh.write("uid,code\n" + "".join(f"{u},S-{u}\n" for u in uids[1:]))      # u0 has no polygon
        pres = yaml.safe_load(open(os.path.join(db, "presentation.yaml")))
        pres["boundaries"] = {"path": "b.geojson", "feature_id": "code", "credit": "Boundaries: toy (CC0)",
                              "crosswalk": {"path": "xw.csv", "unit_id": "uid", "feature_id": "code"}}
        yaml.safe_dump(pres, open(os.path.join(db, "presentation.yaml"), "w"))
        rb = render(db, os.path.join(base, "bmap"), only=["allocation_map", "stability_map", "burden_map"])
        eb = {f["figure"]: f for f in rb["figures"]}
        ok(set(eb) == {"allocation_map", "stability_map", "burden_map"}
           and all(f.get("layout") == "boundaries" and f.get("unmatched") == ["u0"]
                   and "u0" in f["caption"] and f.get("credit") == "Boundaries: toy (CC0)" for f in eb.values())
           and all(f.get("context_features") == 3 for f in eb.values()),     # 2 context + the one u0 is not mapped to
           f"BD1 with a boundary file and crosswalk the maps draw polygons, list unmatched units, credit the source and "
           f"draw context features: {[(k, v.get('layout'), v.get('unmatched'), v.get('context_features')) for k, v in eb.items()]}")
        db2 = toy_run(os.path.join(base, "runb2"))
        geo(db2, uids, key="uid_prop")
        sp2 = yaml.safe_load(open(os.path.join(db2, "presentation.yaml")))
        lines = open(os.path.join(db2, "units.csv")).read().splitlines()
        open(os.path.join(db2, "units.csv"), "w").write("\n".join([lines[0]] + [ln.replace(f"{ln.split(',')[0]},",
                                                                                         f"S-{ln.split(',')[0]},", 1)
                                                                             for ln in lines[1:]]) + "\n")
        edit(db2, "point", lambda x: x.update(plan={f"S-{k}": v for k, v in x["plan"].items()}))
        sp2["boundaries"] = {"path": "b.geojson", "feature_id": "uid_prop"}
        yaml.safe_dump(sp2, open(os.path.join(db2, "presentation.yaml"), "w"))
        e2b, why = one(db2, "allocation_map", "bmap2")
        ok(e2b and e2b.get("layout") == "boundaries" and e2b.get("unmatched") == [],
           f"BD2 without a crosswalk the unit id matches the feature property directly: {e2b and e2b.get('unmatched')} {why}")
        open(os.path.join(db, "b.geojson"), "w").write("{\"type\": \"FeatureCollection\", \"features\": [{}]}")
        try:
            render(db, os.path.join(base, "bmap3"), only=["allocation_map"])
            ok(False, "BD3 a malformed boundary file fails closed")
        except ValueError as e:
            ok("b.geojson" in str(e), f"BD3 a malformed boundary file fails closed: {e}")
        # ---------------- Codex r2
        def bnd_run(name, xw_rows=None, feats=None, crs=None, union=False):
            dd = toy_run(os.path.join(base, name))
            geo(dd, uids)
            if feats is not None or crs is not None:
                g = json.load(open(os.path.join(dd, "b.geojson")))
                if feats is not None:
                    feats(g["features"])
                if crs is not None:
                    g["crs"] = {"type": "name", "properties": {"name": crs}}
                json.dump(g, open(os.path.join(dd, "b.geojson"), "w"))
            bd = {"path": "b.geojson", "feature_id": "code"}
            if xw_rows is not None:
                with open(os.path.join(dd, "xw.csv"), "w") as fh:
                    fh.write("uid,code\n" + "".join(f"{u},{c}\n" for u, c in xw_rows))
                bd["crosswalk"] = dict({"path": "xw.csv", "unit_id": "uid", "feature_id": "code"},
                                       **({"union": True} if union else {}))
            pr = yaml.safe_load(open(os.path.join(dd, "presentation.yaml")))
            pr["boundaries"] = bd
            yaml.safe_dump(pr, open(os.path.join(dd, "presentation.yaml"), "w"))
            return dd
        base_rows = [(u, f"S-{u}") for u in uids]
        for label, rows, needle in (
                ("BD4 a unit mapped to two features is refused, naming the unit (P1-1)",
                 base_rows + [("u1", "S-u2")], "u1"),
                ("BD4b two units mapped to one feature are refused, naming both (P1-1)",
                 [r for r in base_rows if r[0] != "u2"] + [("u2", "S-u1")], "u2")):
            dd = bnd_run("bx" + label[2:4].strip(), rows)
            try:
                render(dd, os.path.join(base, "bx" + label[:4].strip()), only=["burden_map"])
                ok(False, label)
            except ValueError as e:
                ok(needle in str(e) and "u1" in str(e), f"{label}: {e}")
        dd = bnd_run("bxu", [r for r in base_rows if r[0] != "u2"] + [("u1", "S-u2")], union=True)
        eu, why = one(dd, "burden_map", "bxu_o")
        ok(eu and "u2" in eu.get("unmatched", []) and "u1" not in eu.get("unmatched", []),
           f"BD4c a declared union draws both features for the unit: {eu and eu.get('unmatched')} {why}")
        dd = bnd_run("bxe", base_rows, feats=lambda fs: fs[3].update(geometry={"type": "MultiPolygon",
                                                                              "coordinates": []}))
        ee, why = one(dd, "burden_map", "bxe_o")
        ok(ee and "u3" in ee.get("unmatched", []), f"BD5 a unit whose feature is an empty MultiPolygon is unmatched: "
                                                 f"{ee and ee.get('unmatched')} {why}")
        for label, kw, needle in (("BD6 a projected CRS is refused (P2-9)", {"crs": "EPSG:3857"}, "CRS"),
                                  ("BD6b coordinates outside the geographic range are refused",
                                   {"feats": lambda fs: fs[0]["geometry"].update(coordinates=[[[5e5, 4e6], [5e5 + 1, 4e6],
                                                                                              [5e5 + 1, 4e6 + 1],
                                                                                              [5e5, 4e6]]],
                                                                                 type="Polygon")}, "geographic")):
            dd = bnd_run("bxc" + label[3:5].strip(), base_rows, **kw)
            try:
                render(dd, os.path.join(base, "bxc_o" + label[3:5].strip()), only=["burden_map"])
                ok(False, label)
            except ValueError as e:
                ok(needle in str(e), f"{label}: {e}")
        dd = bnd_run("bxg", base_rows, crs="urn:ogc:def:crs:OGC:1.3:CRS84")
        eg, why = one(dd, "burden_map", "bxg_o")
        ok(eg is not None, f"BD6c CRS84 is accepted: {why}")
        # P1-5: every tested range is kept; a plan-changing range is never dropped
        dr = toy_run(os.path.join(base, "runr"))

        def many(x):
            x["rows"] = [{"name": f"q{k:02d}", "kind": "parameter", "basis": "declared prior range", "point": 1.0,
                          "low": {"value": 0.9, "point_plan_objective": 1.234e7 - (16 - k) * 1e5,
                                  "share_units_changed": 0.5 if k == 15 else 0.0},
                          "high": {"value": 1.1, "point_plan_objective": 1.234e7 + (16 - k) * 1e5,
                                   "share_units_changed": 0.0}} for k in range(16)]
        edit(dr, "sensitivity", many)
        er, _ = one(dr, "sensitivity", "r1")
        srows = list(csv.DictReader(open(er["table"]))) if er else []
        ok(len(srows) == 16 and {r["name"]: r["changes_plan"] for r in srows}.get("q15") == "True",
           f"F28 all 16 ranges are kept, including the only plan-changing one with the smallest effect: {len(srows)}")
        # P2-6: an explicit deliverable kind is required, and compare() applies the gate
        dk2 = toy_run(os.path.join(base, "runk2"))
        spk2 = yaml.safe_load(open(os.path.join(dk2, "decision_spec.yaml")))
        spk2["deliverable"].pop("kind", None)
        yaml.safe_dump(spk2, open(os.path.join(dk2, "decision_spec.yaml"), "w"), sort_keys=False)
        rk2 = render(dk2, os.path.join(base, "k2o"))
        nk2 = [x for x in rk2["not_produced"] if x["tier"] == "decision"]
        ok(not [f for f in rk2["figures"] if f["tier"] == "decision"] and nk2
           and "deliverable.kind" in nk2[0]["reason"],
           f"F29 a spec without deliverable.kind gets no decision figures and says why: {nk2[:1]}")
        try:
            compare([du, du], os.path.join(base, "cmpu"))
            ok(False, "F30 compare refuses a kind with no declared comparison")
        except ValueError as e:
            ok("threshold_rule" in str(e), f"F30 compare refuses a kind with no declared comparison: {e}")
        try:
            compare([d, d4], os.path.join(d4, "analysis", "cmp"))
            ok(False, "F33 compare refuses an output dir inside a source run (P2-14)")
        except ValueError as e:
            ok("inside" in str(e), f"F33 compare refuses an output dir inside a source run: {e}")
        # P2-15: the comparison carries provenance from every run, the presentation record and the tier
        scc = json.load(open(os.path.join(outc, "comparison.png.provenance.json"))) \
            if os.path.exists(os.path.join(outc, "comparison.png.provenance.json")) else {}
        srcs = list((scc.get("source_hashes") or {}))
        ok(any(os.path.join("run1", "analysis", "point.yaml") in x for x in srcs)
           and any(os.path.join("run4", "analysis", "point.yaml") in x for x in srcs)
           and scc.get("tier") == "decision" and scc.get("presentation") is not None,
           f"F34 the comparison's provenance lists every run's sources, the presentation files and the tier: {srcs[:4]}")
        # P2-7: the presentation, boundary and crosswalk files are in each figure's checked provenance
        scb = json.load(open(os.path.join(base, "bmap", "burden_map.png.provenance.json")))
        kinds_b = {x.get("kind") for x in scb.get("presentation") or []}
        ok(kinds_b >= {"presentation", "boundaries", "crosswalk"},
           f"F31 map provenance records the presentation, boundary and crosswalk files: {kinds_b}")
        open(os.path.join(db, "xw.csv"), "a").write("\n")
        ok(presentation_findings(db, scb) and any("xw.csv" in f for f in presentation_findings(db, scb)),
           "F31b a changed crosswalk is found when the figure's provenance is checked")
        # ---- Codex r3 P2: in-run boundary and crosswalk files are recorded relative to the run (a moved run checks
        # clean); external files stay absolute
        d35 = bnd_run("bx35", base_rows)
        r35 = render(d35, os.path.join(base, "bmap35"), only=["burden_map"])
        sc35 = json.load(open(os.path.join(base, "bmap35", "burden_map.png.provenance.json"))) if r35["figures"] else {}
        bpaths = {x.get("kind"): x.get("path") for x in sc35.get("presentation") or []}
        ok(bpaths.get("boundaries") == "b.geojson" and bpaths.get("crosswalk") == "xw.csv",
           f"F35 in-run boundary and crosswalk files are recorded relative to the run: {bpaths}")
        moved35 = d35 + "_moved"
        os.rename(d35, moved35)
        try:
            fm = presentation_findings(moved35, sc35)
        finally:
            os.rename(moved35, d35)
        ok(sc35 and fm == [], f"F35b a moved run's in-run presentation files check clean: {fm}")
        # ---- Codex r3 P2: the comparison hashes every consumed input; its presentation records resolve per run
        scc = json.load(open(os.path.join(outc, "comparison.png.provenance.json")))
        srcs = list((scc.get("source_hashes") or {}))
        ok(all(any(os.path.join(r, *rel) in x for x in srcs) for r in ("run1", "run4")
               for rel in (("analysis", "engine.yaml"), ("presentation.yaml",))),
           f"F36 the comparison's sources include each run's engine artifact and presentation file: {srcs}")
        ok(all(x.get("run") for x in scc.get("presentation") or []) and presentation_findings(outc, scc) == [],
           "F36b the comparison's presentation records carry their run and check clean")
        pres1 = os.path.join(d, "presentation.yaml")
        keep_p1 = open(pres1).read()
        open(pres1, "a").write("# edited\n")
        try:
            f36 = presentation_findings(outc, scc)
        finally:
            open(pres1, "w").write(keep_p1)
        ok(any("presentation.yaml" in x for x in f36),
           f"F36c a changed presentation file of a compared run is found, resolved against its run: {f36}")
        # P2-12: a window mean over monthly steps uses the half-open window and weights partial steps
        tg = [k / 12 for k in range(24)]
        vg = [float(k) for k in range(24)]
        wm = _window_mean(tg, vg, 0.125, 0.375)        # 1.5 to 4.5 months: half of step 1, steps 2-3, half of step 4
        ok(abs(wm - (0.5 * 1 + 2 + 3 + 0.5 * 4) / 3.0) < 1e-9, f"F32 the window mean weights partial steps: {wm}")
        # design: decision figures and modeller diagnostics are grouped in figures.yaml
        idx = yaml.safe_load(open(os.path.join(out, "figures.yaml")))
        grp = idx.get("groups") or {}
        ok(set(grp.get("decision", [])) >= {"allocation_map", "dollars", "stability_map", "frontier"}
           and set(grp.get("diagnostics", [])) >= {"calibration_fit", "sensitivity", "trajectories"}
           and all(f.get("group") in ("decision", "diagnostics") for f in idx["figures"]),
           f"F27 figures.yaml separates decision figures from modeller diagnostics: {grp}")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
