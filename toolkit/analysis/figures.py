"""Figures with provenance (design §2): drawn from the analysis artifacts only.

Each PNG gets the scripts/figure_validator.py sidecar (`<png>.provenance.json`: the source
artifacts' hashes), with `produced_by: harness` and the producer added, so staleness checks
work on harness figures as on any other. The harness redraws them on every analysis run.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRODUCER = "toolkit/analysis/figures.py"


def _fv():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import figure_validator
    return figure_validator


def _save(fig, run_dir, name, sources):
    path = os.path.join(run_dir, "figures", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    fv = _fv()
    fv.validate_figure(path, source_data_paths=[os.path.join(run_dir, s) for s in sources])
    sp = path + ".provenance.json"
    with open(sp) as f:
        sc = json.load(f)
    # paths relative to the run dir, so the provenance survives outputs moving off the VM (contract.py resolves them)
    sc.update(produced_by="harness", producer=PRODUCER, paths_relative_to="run_dir",
              png_path=os.path.relpath(path, run_dir),
              source_hashes={os.path.relpath(p, run_dir): h for p, h in (sc.get("source_hashes") or {}).items()})
    with open(sp, "w") as f:
        json.dump(sc, f, indent=2, sort_keys=True)
    return os.path.relpath(path, run_dir)


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


def draw(run_dir: str, point: dict, uq: dict, sens: dict, cal: dict, outcome: str, labels: dict) -> list[dict]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figs = []
    # 1. the plan: units per package
    pk = sorted(point["packages"].items(), key=lambda kv: -kv[1])
    fig, ax = plt.subplots(figsize=(7, 0.4 * len(pk) + 1.2))
    ax.barh([k for k, _ in pk][::-1], [v for _, v in pk][::-1], color="#4c72b0")
    ax.set_xlabel("units")
    ax.set_title("Recommended package per unit (count of units)")
    figs.append({"path": _save(fig, run_dir, "plan_packages.png", [labels["point"]]), "shows": "plan package counts"})
    # 2. the headline over the uncertainty draws
    fp = uq["fixed_plan"][outcome]
    fig, ax = plt.subplots(figsize=(7, 1.8))
    ax.errorbar([fp["median"]], [0], xerr=[[fp["median"] - fp["ci_low"]], [fp["ci_high"] - fp["median"]]], fmt="o",
                color="#4c72b0", capsize=6)
    ax.axvline(point["averted"][outcome], color="#c44e52", lw=1, ls="--", label="point estimate")
    ax.set_yticks([])
    ax.set_xlabel(f"{outcome} averted (point plan)")
    ax.set_title(uq["label"], fontsize=8)
    ax.legend(fontsize=8)
    figs.append({"path": _save(fig, run_dir, "headline_interval.png", [labels["point"], labels["uncertainty"]]),
                 "shows": "headline interval over the declared sources"})
    # 3. tornado
    rows = sens["rows"][:12]
    if rows:
        fig, ax = plt.subplots(figsize=(7, 0.45 * len(rows) + 1.2))
        base = sens["objective_point"]
        for j, r in enumerate(rows[::-1]):
            lo, hi = r["low"]["point_plan_objective"] - base, r["high"]["point_plan_objective"] - base
            ax.barh(j, hi, color="#55a868")
            ax.barh(j, lo, color="#c44e52")
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels([r["name"] for r in rows[::-1]], fontsize=8)
        ax.axvline(0, color="k", lw=0.8)
        ax.set_xlabel("change in the point plan's objective (low end red, high end green)")
        ax.set_title("One-at-a-time sensitivity")
        figs.append({"path": _save(fig, run_dir, "sensitivity_tornado.png", [labels["sensitivity"]]),
                     "shows": "sensitivity"})
    # 4. calibration
    fit = (cal.get("diagnostics") or {}).get("fit")
    if fit:
        fig, ax = plt.subplots(figsize=(4.5, 4.5))
        x = [r["observed"] for r in fit]
        y = [r["expected"] for r in fit]
        ax.scatter(x, y, s=10, color="#4c72b0")
        m = max(max(x), max(y))
        ax.plot([0, m], [0, m], "k--", lw=0.8)
        ax.set_xlabel("observed")
        ax.set_ylabel("fitted")
        ax.set_title("Calibration: observed vs fitted targets")
        figs.append({"path": _save(fig, run_dir, "calibration_fit.png", [labels["calibration"]]),
                     "shows": "calibration fit"})
    return figs
