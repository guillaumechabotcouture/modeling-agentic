#!/usr/bin/env python3
"""Style, number formats and labels for the harness figures (design: docs/design/v2-figures.md §2).

    python3 toolkit/analysis/figure_style.py --self-test

Colours are the dataviz skill's reference palette, validated with its validate_palette.js (categorical slots 1-6 in
light and dark mode on the adjacent pairlist; slots 1-2 on all pairs; the blue ordinal ramp in both modes). Slots are
assigned in fixed order, never cycled. Every axis uses `fmt` through `set_axis_format`, so no figure shows offset
notation. Labels come from the spec's optional `presentation.labels`, then from an overlay file, else a readable
fallback with a legend line that defines the id. Nothing here names a disease, component or unit.
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DPI = 180
THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
        "axis": "#c3c2b7", "neutral": "#d6d5ce", "band": "#f0efec",
        "cat": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
        "ramp": ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"],          # ordinal, low -> high (steps 250-700)
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
        "axis": "#383835", "neutral": "#4a4a46", "band": "#262624",
        "cat": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
        "ramp": ["#184f95", "#2a78d6", "#6da7ec", "#b7d3f6"],          # ordinal, low -> high (steps 600-150)
    },
}
MAX_CATEGORIES = 8
MAX_SCATTER_CATEGORIES = 3   # the reference palette validates its first three slots on all pairs


def theme(name: str = "light") -> dict:
    if name not in THEMES:
        raise ValueError(f"unknown theme {name!r} (known: {sorted(THEMES)})")
    return THEMES[name]


# --------------------------------------------------------------------------- numbers
def _compact(v: float, digits: int = 3) -> str:
    a = abs(v)
    units = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))
    for n, (div, suf) in enumerate(units):
        if a >= div:
            x = v / div
            if abs(round(x)) >= 1000 and n > 0:          # 999.97K reads as 1M, not 1000K
                div, suf = units[n - 1]
                x = v / div
            dec = max(digits - len(str(int(abs(round(x, 6))))), 0)
            s = f"{x:.{dec}f}"
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s + suf
    if a >= 100 or v == int(v):
        return f"{v:,.0f}"
    if a >= 1:
        return f"{v:.3g}"
    return f"{v:.2g}"


def fmt(v, kind: str = "number", digits: int = 3) -> str:
    """12.3M / $1.2M / 45% / 1,234 / 0.042. kind: number | money | percent (v a share) | count."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "n/a"
    v = float(v)
    if kind == "money":
        s = _compact(abs(v), digits)
        return ("-" if v < 0 else "") + "$" + s
    if kind == "percent":
        p = 100 * v
        return f"{p:.0f}%" if abs(p) >= 10 or p == int(p) else f"{p:.1f}%"
    if kind == "count":
        return _compact(v, digits) if abs(v) >= 10000 else f"{v:,.0f}"
    if kind == "signed":
        return ("+" if v > 0 else "") + _compact(v, digits) if v != 0 else "0"
    return _compact(v, digits)


def set_axis_format(ax, axis: str = "x", kind: str = "number") -> None:
    """A FuncFormatter with `fmt` (no offset, no scientific notation) on one axis."""
    from matplotlib.ticker import FuncFormatter
    f = FuncFormatter(lambda v, _p: fmt(v, "signed" if kind == "signed" else kind, 3))
    (ax.xaxis if axis == "x" else ax.yaxis).set_major_formatter(f)


def style(ax, t: dict, grid: str = "x") -> None:
    ax.set_facecolor(t["surface"])
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(t["axis"])
    ax.tick_params(colors=t["ink2"], labelsize=8, length=0)
    if grid:
        ax.grid(axis=grid, color=t["grid"], lw=0.8)
        ax.set_axisbelow(True)


def setup(theme_name: str = "light") -> dict:
    import matplotlib
    matplotlib.use("Agg")
    t = theme(theme_name)
    matplotlib.rcParams.update({
        "axes.formatter.useoffset": False, "axes.formatter.use_mathtext": False,
        "font.family": "sans-serif", "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 9, "text.color": t["ink"], "axes.labelcolor": t["ink2"], "axes.edgecolor": t["axis"],
        "figure.facecolor": t["surface"], "savefig.facecolor": t["surface"], "axes.titlelocation": "left",
        "legend.frameon": False, "legend.fontsize": 8, "svg.fonttype": "none",
        "text.parse_math": False})             # "$1.2M" is money, not mathtext
    return t


def title(fig, text: str, t: dict, sub: str | None = None) -> None:
    fig.text(0.01, 0.995, text, ha="left", va="top", fontsize=11.5, fontweight="bold", color=t["ink"])
    if sub:
        fig.text(0.01, 0.995 - 0.075 * 3.2 / max(fig.get_figheight(), 1.6), sub, ha="left", va="top", fontsize=8.5,
                 color=t["ink2"])


def offset_free(ax) -> bool:
    """True when no axis of `ax` shows an offset or scientific multiplier (tests and the renderer's own check)."""
    import matplotlib.pyplot as plt  # noqa: F401
    ax.figure.canvas.draw()
    return not ax.xaxis.get_offset_text().get_text() and not ax.yaxis.get_offset_text().get_text()


# --------------------------------------------------------------------------- labels
class Labels:
    """Display names from the unsigned presentation file's labels (`overlay`; never from the signed spec), else a
    fallback: the id made readable, with a definition line for the legend (from the spec's engine_map)."""

    def __init__(self, spec_raw: dict, card: dict | None = None, params: list | None = None,
                 overlay: dict | None = None):
        pres = {}
        ov = overlay or {}
        self.given = {k: dict(pres.get(k) or {}, **(ov.get(k) or {})) for k in ("components", "parameters",
                                                                               "outcomes", "metrics")}
        self.titles = {k: ov.get(k) or pres.get(k) for k in ("regions_title", "units_title")}
        self.spec = spec_raw or {}
        self.card = card or {}
        self.params = {p["name"]: p for p in (params or []) if isinstance(p, dict) and "name" in p}
        self.used_fallback: dict[str, set] = {"components": set(), "parameters": set(), "outcomes": set(),
                                              "metrics": set()}

    @staticmethod
    def readable(code: str) -> str:
        s = str(code).replace("_", " ").strip()
        return s[:1].upper() + s[1:]

    def _get(self, kind, code):
        v = self.given[kind].get(code)
        if v:
            return str(v)
        self.used_fallback[kind].add(code)
        return None

    def component(self, c: str) -> str:
        return self._get("components", c) or self.readable(c)

    def metric(self, m: str) -> str:
        return self._get("metrics", m) or self.readable(m)

    def outcome(self, o: str, plural: bool = True) -> str:
        v = self._get("outcomes", o)
        return v if v else (o.upper() if len(o) <= 5 else self.readable(o))

    def parameter(self, p: str) -> str:
        return self._get("parameters", p) or self.readable(p)

    def package(self, members) -> str:
        """A package as its components' names joined by ' + ' (the comparator: 'No package')."""
        order = list(((self.spec.get("options") or {}).get("components") or {}))
        m = sorted(members, key=lambda c: (order.index(c) if c in order else len(order), c))
        return " + ".join(self.component(c) for c in m) if m else "No package"

    def region_title(self) -> str:
        return self.titles.get("regions_title") or self.readable(str((self.spec.get("decision_units") or {}).get(
            "region") or "region"))

    def unit_title(self) -> str:
        return self.titles.get("units_title") or "units"

    def component_definition(self, c: str) -> str:
        """'<id>: engine <component> = <value>' from the spec's engine_map (the legend line for a fallback)."""
        em = ((self.spec.get("options") or {}).get("engine_map") or {}).get(c) or {}
        parts = [f"{k} = {v}" for k, v in em.items()] if isinstance(em, dict) else [str(em)]
        return f"{c}: engine {', '.join(parts)}" if parts else c

    def parameter_units(self, p: str) -> str:
        return str((self.params.get(p) or {}).get("units") or "")

    def legend_lines(self) -> list[str]:
        """Definitions for every id shown with its fallback name (for the figure's footnote)."""
        out = [self.component_definition(c) for c in sorted(self.used_fallback["components"])]
        return out


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    cases = [((13056739.98, "number"), "13.1M"), ((260651333.5, "money"), "$261M"), ((1234, "count"), "1,234"),
             ((0.4512, "percent"), "45%"), ((0.052, "percent"), "5.2%"), ((19.96, "money"), "$20"),
             ((2.5e6, "money"), "$2.5M"), ((1.0e6, "number"), "1M"), ((-2103631.0, "signed"), "-2.1M"),
             ((523000, "signed"), "+523K"), ((0.0, "signed"), "0"), ((None, "number"), "n/a")]
    for (v, k), want in cases:
        got = fmt(v, k)
        ok(got == want, f"S1 fmt({v!r}, {k}) = {got!r} (want {want!r})")
    t = setup("light")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.plot([1.30e7, 1.31e7, 1.32e7], [2.0e6, 2.1e6, 2.2e6])
    plain_offset = not offset_free(ax)
    set_axis_format(ax, "x")
    set_axis_format(ax, "y")
    ok(offset_free(ax) and all("e" not in lb.get_text() for lb in ax.get_xticklabels()),
       f"S2 the formatter removes offset notation (matplotlib default had an offset: {plain_offset})")
    plt.close(fig)
    spec = {"decision_units": {"region": "zone"}, "options": {"engine_map": {"c_x": {"eng": "v1"}}},
            "presentation": {"labels": {"components": {"c_q": "From the spec"}}}}
    L = Labels(spec, params=[{"name": "p1", "units": "days"}],
               overlay={"components": {"c_y": "Component Y", "c_z": "Zed"}, "parameters": {"p1": "Param one"}})
    ok(L.component("c_y") == "Component Y" and L.component("c_z") == "Zed" and L.parameter("p1") == "Param one"
       and L.component("c_q") == "C q",
       "S3 labels come from the presentation file only, never from the signed spec")
    ok(L.component("c_x") == "C x" and "c_x: engine eng = v1" in L.legend_lines(),
       f"S4 a component with no label gets a readable fallback and a definition line: {L.legend_lines()}")
    ok(L.package(["c_y", "c_z"]) == "Component Y + Zed" and L.package([]) == "No package",
       "S5 packages read as their components' names")
    ok(L.region_title() == "Zone" and L.parameter_units("p1") == "days", "S6 region title and parameter units")
    ok(len(THEMES["light"]["cat"]) == MAX_CATEGORIES == len(THEMES["dark"]["cat"]), "S7 eight categorical slots per mode")
    try:
        theme("sepia")
        ok(False, "S8 an unknown theme is refused")
    except ValueError:
        ok(True, "S8 an unknown theme is refused")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
