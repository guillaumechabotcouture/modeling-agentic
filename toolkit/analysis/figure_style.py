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
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- numbers
def _compact(v: float, digits: int = 3) -> str:
    raise NotImplementedError("figures β")


def fmt(v, kind: str = "number", digits: int = 3) -> str:
    """12.3M / $1.2M / 45% / 1,234 / 0.042. kind: number | money | percent (v a share) | count."""
    raise NotImplementedError("figures β")


def set_axis_format(ax, axis: str = "x", kind: str = "number") -> None:
    """A FuncFormatter with `fmt` (no offset, no scientific notation) on one axis."""
    raise NotImplementedError("figures β")


def style(ax, t: dict, grid: str = "x") -> None:
    raise NotImplementedError("figures β")


def setup(theme_name: str = "light") -> dict:
    raise NotImplementedError("figures β")


def title(fig, text: str, t: dict, sub: str | None = None) -> None:
    raise NotImplementedError("figures β")


def offset_free(ax) -> bool:
    """True when no axis of `ax` shows an offset or scientific multiplier (tests and the renderer's own check)."""
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- labels
class Labels:
    """Display names. Order: overlay (render --labels), the spec's presentation.labels, then a fallback: the id made
    readable, with a definition line for the legend (from the spec's engine_map and the engine card)."""

    def __init__(self, spec_raw: dict, card: dict | None = None, params: list | None = None,
                 overlay: dict | None = None):
        raise NotImplementedError("figures β")

    @staticmethod
    def readable(code: str) -> str:
        raise NotImplementedError("figures β")

    def _get(self, kind, code):
        raise NotImplementedError("figures β")

    def component(self, c: str) -> str:
        raise NotImplementedError("figures β")

    def metric(self, m: str) -> str:
        raise NotImplementedError("figures β")

    def outcome(self, o: str, plural: bool = True) -> str:
        raise NotImplementedError("figures β")

    def parameter(self, p: str) -> str:
        raise NotImplementedError("figures β")

    def package(self, members) -> str:
        """A package as its components' names joined by ' + ' (the comparator: 'No package')."""
        raise NotImplementedError("figures β")

    def region_title(self) -> str:
        raise NotImplementedError("figures β")

    def unit_title(self) -> str:
        raise NotImplementedError("figures β")

    def component_definition(self, c: str) -> str:
        """'<id>: engine <component> = <value>' from the spec's engine_map (the legend line for a fallback)."""
        raise NotImplementedError("figures β")

    def parameter_units(self, p: str) -> str:
        raise NotImplementedError("figures β")

    def legend_lines(self) -> list[str]:
        """Definitions for every id shown with its fallback name (for the figure's footnote)."""
        raise NotImplementedError("figures β")


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
            "presentation": {"labels": {"components": {"c_y": "Component Y"}, "parameters": {"p1": "Param one"}}}}
    L = Labels(spec, params=[{"name": "p1", "units": "days"}], overlay={"components": {"c_z": "Zed"}})
    ok(L.component("c_y") == "Component Y" and L.component("c_z") == "Zed" and L.parameter("p1") == "Param one",
       "S3 labels come from the spec's presentation block and the overlay")
    ok(L.component("c_x") == "C x" and L.legend_lines() == ["c_x: engine eng = v1"],
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
