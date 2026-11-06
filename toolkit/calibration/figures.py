#!/usr/bin/env python3
"""Figures of a joint calibration: the shape of the compatible region and the fit of every target.

    python3 toolkit/calibration/figures.py --self-test

Drawn from the report's `figure_data` (toolkit/calibration/history_matching.py figure_data()), which the harness
saves in analysis/joint_calibration.yaml; names come from the report (the engine card's parameter names), labels
from an optional {name: label} map. Nothing here names a disease. Style: toolkit/analysis/figure_style.py (the
dataviz palette; a sequential ramp for implausibility; labels in ink colours).

  calibration_region         corner plot of the shared parameters, ordered by how strongly each co-varies with the
                             others inside the region: 1-D densities of the region against the prior (shaded) on the
                             diagonal; below it every candidate in grey and the region's candidates coloured by their
                             worst implausibility; |Spearman| of the region printed on each panel
  calibration_top_pairs      the most co-varying pairs, larger, with the prior box and the prior's point value
  calibration_optical_depth  per pair, the share of candidates in each cell that are non-implausible whatever the other
                             parameters (the history-matching optical depth), so projection does not hide the shape
  calibration_unit_fit       every target: observed value with its sampling interval against the region's model band
                             (2.5-97.5%), sorted, unmatched and failed units highlighted
  calibration_waves          the region's share of the prior after each wave
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis import figure_style as FS  # noqa: E402

NAMES = ("calibration_region", "calibration_top_pairs", "calibration_optical_depth", "calibration_unit_fit",
         "calibration_waves")
TIER = "data_model"


class NoData(Exception):
    """The figure has nothing to draw; the message is why."""


def _lab(labels, n):
    return (labels or {}).get(n, n)


def _cmap(t):
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list("ramp", list(t["ramp"]))


def _order(fd):
    """Parameters by their strongest |Spearman| with any other inside the region (unknown last)."""
    best = {n: -1.0 for n in fd["names"]}
    for p in fd["pairs"]:
        a = p["abs_spearman"]
        if a is not None:
            best[p["x"]] = max(best[p["x"]], a)
            best[p["y"]] = max(best[p["y"]], a)
    return sorted(fd["names"], key=lambda n: -best[n])


def _pair(fd, a, b):
    for p in fd["pairs"]:
        if {p["x"], p["y"]} == {a, b}:
            return p
    return None


def _prior_pdf(pr, x):
    lo, hi = pr["range"]
    if pr["dist"] == "uniform":
        return np.full_like(x, 1.0 / (hi - lo))
    if pr["dist"] == "loguniform":
        return 1.0 / (np.maximum(x, 1e-300) * math.log(hi / lo))
    c = pr["mode"]
    return np.maximum(np.where(x < c, 2 * (x - lo) / ((hi - lo) * max(c - lo, 1e-300)),
                               2 * (hi - x) / ((hi - lo) * max(hi - c, 1e-300))), 0)


def _pts(fd):
    V = np.asarray(fd["points"]["values"], float)
    I = np.asarray(fd["points"]["implausibility"], float)
    A = np.asarray(fd["points"]["accepted"], bool)
    return V, I, A


def fig_region(fd, t, labels=None):
    import matplotlib.pyplot as plt
    names = _order(fd)
    p = len(names)
    if p < 2:
        raise NoData("fewer than two shared parameters: no pairs to draw")
    V, I, A = _pts(fd)
    if not A.any():
        raise NoData("the region is empty")
    col = {n: fd["names"].index(n) for n in names}
    s = max(min(1.9, 15.0 / p), 7.0 / p)
    fig, axs = plt.subplots(p, p, figsize=(s * p + 1.4, s * p + 1.4), squeeze=False)
    fig.subplots_adjust(left=0.1, right=0.84, bottom=0.1, top=0.9, wspace=0.08, hspace=0.08)
    cm, vmax = _cmap(t), fd["cutoff"]
    rows = []
    sc = None
    for i, yn in enumerate(names):
        for j, xn in enumerate(names):
            ax = axs[i][j]
            FS.style(ax, t, grid=None)
            if j > i:
                ax.axis("off")
                continue
            xlo, xhi = fd["prior"][xn]["range"]
            if i == j:
                x = V[A, col[xn]]
                g = np.linspace(xlo, xhi, 120)
                pd = _prior_pdf(fd["prior"][xn], g)
                ax.fill_between(g, 0, pd / max(pd.max(), 1e-300), color=t["band"], lw=0)
                h, e = np.histogram(x, bins=20, range=(xlo, xhi), density=True)
                ax.step(e[:-1], h / max(h.max(), 1e-300), where="post", color=t["cat"][0], lw=1.2)
                ax.set_yticks([])
                rows.append({"panel": f"{xn}", "region_min": float(x.min()), "region_max": float(x.max()),
                             "prior_lo": xlo, "prior_hi": xhi})
            else:
                ylo, yhi = fd["prior"][yn]["range"]
                ax.scatter(V[~A, col[xn]], V[~A, col[yn]], s=2, c=t["neutral"], lw=0, rasterized=True)
                o = np.argsort(-I[A])
                sc = ax.scatter(V[A, col[xn]][o], V[A, col[yn]][o], s=4, c=I[A][o], cmap=cm, vmin=0, vmax=vmax, lw=0,
                                rasterized=True)
                ax.set_ylim(ylo, yhi)
                pr = _pair(fd, xn, yn)
                r = pr and pr["abs_spearman"]
                ax.text(0.03, 0.97, "|ρ| n/a" if r is None else f"|ρ| {r:.2f}", transform=ax.transAxes, ha="left",
                        va="top", fontsize=6.5, color=t["ink"])
                rows.append({"panel": f"{yn} vs {xn}", "abs_spearman": r})
            ax.set_xlim(xlo, xhi)
            if i == p - 1:
                ax.set_xlabel(_lab(labels, xn), fontsize=7, color=t["ink2"])
                FS.set_axis_format(ax, "x")
                ax.tick_params(axis="x", labelsize=6, rotation=45)
            else:
                ax.set_xticklabels([])
            if j == 0 and i > 0:
                ax.set_ylabel(_lab(labels, yn), fontsize=7, color=t["ink2"])
                FS.set_axis_format(ax, "y")
                ax.tick_params(axis="y", labelsize=6)
            elif i != j:
                ax.set_yticklabels([])
    if sc is not None:
        cax = fig.add_axes([0.87, 0.55, 0.015, 0.3])
        cb = fig.colorbar(sc, cax=cax)
        cb.set_label("worst implausibility", color=t["ink2"], fontsize=7)
        cb.ax.tick_params(labelsize=6, colors=t["ink2"])
    acc = int(A.sum())
    title = "Compatible region of the shared parameters"
    cap = (f"{acc} of {len(A)} candidates shown are compatible. Diagonal: the region's density (line) against the prior (shaded), each on its own prior range. Below: every "
           "candidate (grey) and the compatible ones coloured by their worst implausibility; |ρ| is the region's "
           "Spearman co-variation. Parameters ordered by their strongest |ρ|.")
    return fig, title, cap, rows


def fig_top_pairs(fd, t, labels=None, k=6):
    import matplotlib.pyplot as plt
    V, I, A = _pts(fd)
    pairs = sorted([p for p in fd["pairs"] if p["abs_spearman"] is not None], key=lambda p: -p["abs_spearman"])[:k]
    if not pairs or not A.any():
        raise NoData("no pair has a co-variation inside the region (fewer than 5 compatible candidates, or none)")
    nc = min(3, len(pairs))
    nr = int(math.ceil(len(pairs) / nc))
    w = max(3.6 * nc, 6.4)
    fig, axs = plt.subplots(nr, nc, figsize=(w, 3.4 * nr + 0.8), squeeze=False)
    fig.subplots_adjust(left=0.1 if nc > 1 else 0.14, right=0.97, bottom=0.12, top=0.84, wspace=0.34, hspace=0.45)
    cm = _cmap(t)
    rows = []
    for n, pr in enumerate(pairs):
        ax = axs[n // nc][n % nc]
        FS.style(ax, t, grid=None)
        xn, yn = pr["x"], pr["y"]
        ix, iy = fd["names"].index(xn), fd["names"].index(yn)
        (xlo, xhi), (ylo, yhi) = fd["prior"][xn]["range"], fd["prior"][yn]["range"]
        ax.scatter(V[~A, ix], V[~A, iy], s=3, c=t["neutral"], lw=0, rasterized=True)
        o = np.argsort(-I[A])
        ax.scatter(V[A, ix][o], V[A, iy][o], s=6, c=I[A][o], cmap=cm, vmin=0, vmax=fd["cutoff"], lw=0, rasterized=True)
        ax.add_patch(plt.Rectangle((xlo, ylo), xhi - xlo, yhi - ylo, fill=False, ec=t["ink2"], lw=0.9, ls="--"))
        px, py = fd["prior"][xn].get("point"), fd["prior"][yn].get("point")
        if px is not None and py is not None:
            ax.plot([px], [py], marker="P", ms=9, color=t["cat"][1], mec=t["surface"], mew=0.8, ls="none")
        dx, dy = 0.05 * (xhi - xlo), 0.05 * (yhi - ylo)
        ax.set_xlim(xlo - dx, xhi + dx)
        ax.set_ylim(ylo - dy, yhi + dy)
        ax.set_xlabel(_lab(labels, xn), fontsize=8)
        ax.set_ylabel(_lab(labels, yn), fontsize=8)
        FS.set_axis_format(ax, "x")
        FS.set_axis_format(ax, "y")
        ax.set_title(f"|ρ| {pr['abs_spearman']:.2f} in the region", fontsize=8, color=t["ink"])
        rows.append({"x": xn, "y": yn, "abs_spearman": pr["abs_spearman"], "spearman": pr["spearman"],
                     "x_point": px, "y_point": py})
    for n in range(len(pairs), nr * nc):
        axs[n // nc][n % nc].axis("off")
    title = f"Most co-varying pairs in the compatible region ({len(pairs)})"
    cap = ("Grey: every candidate; coloured: compatible candidates by worst implausibility (darker is higher). Dashed: "
           "the prior box; cross: the prior's point value (the card or literature reference), where the data may "
           "push values away from it.")
    return fig, title, cap, rows


def fig_optical_depth(fd, t, labels=None):
    import matplotlib.pyplot as plt
    names = _order(fd)
    p = len(names)
    if p < 2:
        raise NoData("fewer than two shared parameters")
    s = max(min(1.9, 15.0 / p), 6.0 / max(p - 1, 1))
    fig, axs = plt.subplots(p - 1, p - 1, figsize=(s * (p - 1) + 1.4, s * (p - 1) + 1.4), squeeze=False)
    fig.subplots_adjust(left=0.12, right=0.84, bottom=0.12, top=0.88, wspace=0.08, hspace=0.08)
    from matplotlib.colors import LinearSegmentedColormap
    cm = LinearSegmentedColormap.from_list("depth", [t["surface"]] + list(t["ramp"]))
    im, rows = None, []
    for i in range(1, p):
        for j in range(p - 1):
            ax = axs[i - 1][j]
            if j >= i:
                ax.axis("off")
                continue
            xn, yn = names[j], names[i]
            pr = _pair(fd, xn, yn)
            D = np.asarray(pr["depth"], float)
            if pr["x"] != xn:
                D = D.T
            (xlo, xhi), (ylo, yhi) = fd["prior"][xn]["range"], fd["prior"][yn]["range"]
            im = ax.imshow(D, origin="lower", extent=(xlo, xhi, ylo, yhi), aspect="auto", cmap=cm, vmin=0,
                           vmax=max(float(np.nanmax([np.nanmax(np.asarray(q["depth"], float)) for q in fd["pairs"]])),
                                    1e-9))
            ax.tick_params(colors=t["ink2"], labelsize=6, length=0)
            for sp in ax.spines.values():
                sp.set_color(t["axis"])
            if i == p - 1:
                ax.set_xlabel(_lab(labels, xn), fontsize=7)
                FS.set_axis_format(ax, "x")
                ax.tick_params(axis="x", rotation=45)
            else:
                ax.set_xticklabels([])
            if j == 0:
                ax.set_ylabel(_lab(labels, yn), fontsize=7)
                FS.set_axis_format(ax, "y")
            else:
                ax.set_yticklabels([])
            rows.append({"x": xn, "y": yn, "max_depth": float(np.nanmax(D)) if np.isfinite(D).any() else None,
                         "cells_nonzero": int(np.nansum(D > 0))})
    if im is not None:
        cax = fig.add_axes([0.87, 0.55, 0.015, 0.3])
        cb = fig.colorbar(im, cax=cax)
        cb.set_label("share compatible", color=t["ink2"], fontsize=7)
        cb.ax.tick_params(labelsize=6, colors=t["ink2"])
    title = "Optical depth of the compatible region, by pair"
    cap = ("Each panel splits a pair's prior box into cells; the colour is the share of candidates in the cell (over "
           "all values of the other parameters) inside the compatible region. Blank: none.")
    return fig, title, cap, rows


def fig_unit_fit(fd, t, labels=None):
    import matplotlib.pyplot as plt
    rows = list(fd.get("fit") or [])
    if not rows:
        raise NoData("no target fit rows (no region sample)")
    groups = {}
    for r in rows:      # one panel per kind of target and observable (unit targets of a survey; its groups; shared)
        groups.setdefault(f"{r['kind']} targets, {r['observable']}", []).append(r)
    keys = list(groups)
    hmax = max(len(v) for v in groups.values())
    h = min(max(2.6, 0.045 * hmax + 1.4), 22)
    fig, axs = plt.subplots(1, len(keys), figsize=(3.4 * len(keys) + 0.6, h), squeeze=False)
    fig.subplots_adjust(left=0.07, right=0.98, bottom=0.08, top=1 - 0.75 / h, wspace=0.3)
    hl = t["cat"][1]
    out = []
    for n, k in enumerate(keys):
        ax = axs[0][n]
        FS.style(ax, t, grid="x")
        g = sorted(groups[k], key=lambda r: r["observed"])
        y = np.arange(len(g))
        for yi, r in zip(y, g):
            bad = r["status"] != "matched"
            ax.plot([r["model_lo"], r["model_hi"]], [yi, yi], color=hl if bad else t["ramp"][1], lw=3.2,
                    solid_capstyle="butt", alpha=0.55)
            ax.plot([r["obs_lo"], r["obs_hi"]], [yi, yi], color=t["ink2"], lw=0.8)
            ax.plot([r["observed"]], [yi], marker="o", ms=2.6, color=hl if bad else t["ink"], ls="none")
            if r.get("verified") is not None:
                ax.plot([r["verified"]], [yi], marker="|", ms=5, color=t["cat"][2], ls="none")
            out.append({k2: r[k2] for k2 in ("target", "observed", "obs_lo", "obs_hi", "model_lo", "model_mid",
                                             "model_hi", "verified", "status")})
        ax.set_yticks([] if len(g) > 40 else y)
        if len(g) <= 40:
            ax.set_yticklabels([str(r.get("unit") or r["target"]) for r in g], fontsize=6)
        ax.set_ylim(-1, len(g))
        nb = sum(r["status"] != "matched" for r in g)
        ax.set_title(f"{_lab(labels, k)} ({len(g)} targets, {nb} not matched)", fontsize=8, color=t["ink"])
        FS.set_axis_format(ax, "x")
        ax.set_xlabel("value (observed and modelled)", fontsize=7.5)
    nbad = sum(r["status"] != "matched" for r in rows)
    title = f"Fit of every target: observed with its interval against the region's band ({nbad} not matched)"
    cap = ("Per target, sorted by the observed value: dot and thin line, the observation and its 95% sampling interval; "
           "thick bar, the region's 2.5-97.5% model band; tick, the engine at the best point. Orange: unmatched, "
           "conflicting or failed verification.")
    return fig, title, cap, out


def fig_waves(fd, t, labels=None):
    import matplotlib.pyplot as plt
    w = [x for x in fd.get("waves") or [] if x.get("share_of_prior") is not None]
    if not w:
        raise NoData("no wave recorded a prior share")
    fig, ax = plt.subplots(figsize=(5.6, 3.2))
    fig.subplots_adjust(left=0.14, right=0.96, bottom=0.18, top=0.8)
    FS.style(ax, t, grid="y")
    x = [r["wave"] for r in w]
    yv = [max(r["share_of_prior"], 1e-6) for r in w]
    ax.plot(x, yv, marker="o", color=t["cat"][0], lw=1.6)
    for xi, yi in zip(x, yv):
        ax.text(xi, yi, f" {100 * yi:.3g}%", fontsize=8, color=t["ink"], va="bottom")
    ax.set_yscale("log")
    ax.set_xticks(x)
    ax.set_xlabel("wave", fontsize=8)
    ax.set_ylabel("share of the prior compatible", fontsize=8)
    rows = [{"wave": r["wave"], "share_of_prior": r["share_of_prior"], "accepted": r["accepted"],
             "candidates": r["candidates"]} for r in w]
    return fig, "Non-implausible share of the prior after each wave", (
        "The prior mass of the compatible region after each wave of simulation (log scale)."), rows


FUNCS = {"calibration_region": fig_region, "calibration_top_pairs": fig_top_pairs,
         "calibration_optical_depth": fig_optical_depth, "calibration_unit_fit": fig_unit_fit,
         "calibration_waves": fig_waves}


def draw(name, fd, t, labels=None):
    """(fig, title, caption, rows) or NoData."""
    if not fd:
        raise NoData("the joint calibration recorded no figure data")
    return FUNCS[name](fd, t, labels)


def render(fd, dest: str, root: str, labels=None, theme: str = "light", sources=()) -> dict:
    """Every calibration figure into dest (PNG and CSV; no link followed under `root`). Returns {figures, not_produced}
    with paths relative to root."""
    import csv
    import io
    import matplotlib.pyplot as plt
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from lib.safe_io import write_nofollow
    t = FS.setup(theme)
    figs, notp = [], []
    for name in NAMES:
        try:
            fig, title, cap, rows = draw(name, fd, t, labels)
        except NoData as e:
            notp.append({"figure": name, "reason": str(e), "tier": TIER})
            continue
        FS.title(fig, title, t)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=FS.DPI)
        plt.close(fig)
        rel = os.path.relpath(os.path.join(dest, f"{name}.png"), root)
        write_nofollow(root, rel, buf.getvalue(), atomic=True)
        s = io.StringIO()
        if rows:
            w = csv.DictWriter(s, fieldnames=list(rows[0]), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        trel = os.path.relpath(os.path.join(dest, f"{name}.csv"), root)
        write_nofollow(root, trel, s.getvalue().encode(), atomic=True)
        figs.append({"figure": name, "path": rel, "table": trel, "title": title, "caption": cap, "tier": TIER,
                     "group": "diagnostics", "sources": list(sources), "theme": theme})
    return {"figures": figs, "not_produced": notp}


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    from toolkit.calibration import history_matching as HM
    q = lambda *a, **k: None  # noqa: E731
    P, sim, *_ = HM._toy(with_group=True)
    r = HM.calibrate(P, sim, seed=3, waves=2, log=q, candidates_log2=10, points_per_unit=10)
    fd = r["figure_data"]
    ok(fd and len(fd["pairs"]) == 1 and fd["fit"] and len(fd["waves"]) == 2,
       "CF1 the report carries figure data: pairs, target fit rows, waves")
    with tempfile.TemporaryDirectory() as d:
        res = render(fd, os.path.join(d, "figures"), d, labels={"e": "effect"})
        got = {f["figure"] for f in res["figures"]}
        ok(got == set(NAMES) and all(os.path.getsize(os.path.join(d, f["path"])) > 5000 for f in res["figures"]),
           f"CF2 every calibration figure renders from a synthetic case ({sorted(got)}, {res['not_produced']})")
        ok(all(f["tier"] == "data_model" and os.path.exists(os.path.join(d, f["table"])) for f in res["figures"]),
           "CF3 each figure is in the data and model tier, with its table")
        fd2 = dict(fd, points=dict(fd["points"], accepted=[False] * len(fd["points"]["accepted"])), fit=[], waves=[])
        res2 = render(fd2, os.path.join(d, "f2"), d)
        ok(len(res2["not_produced"]) >= 3 and all(x["reason"] for x in res2["not_produced"]),
           "CF4 with an empty region a figure is listed as not produced, with why (fail closed)")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
