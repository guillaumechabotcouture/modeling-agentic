#!/usr/bin/env python3
"""Question-specific data mapping: attempt 8's processed data -> spike/data/units.csv.

    python3 spike/units.py <attempt-8 run dir>

This is the part of a run v2 leaves to an agent (map data to the engine's unit
attributes). It reuses attempt 8's processed unit table (models/allocation_320m.csv:
campaign timing class, CF1, CM level, SMC and IRS eligibility and populations, PfPR
target) and adds what attempt 8 never read: per-LGA vector species proportions
(data/processed/lga_master_774.csv). Seasonal profiles follow attempt 8's
models/inputs.py season_groups (rainfall lagged one month, floored at 10% of the mean).
Subset: the 13 states whose 2020 funder is the Global Fund (decision_spec.yaml).
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
TIMING = {"A": ([2018.25, 2021.25], 2024.25), "B": ([2019.25, 2022.25], 2025.25),
          "C": ([2017.25, 2020.25, 2023.25], 2026.25)}


def season_profiles(dp):
    m = pd.read_csv(os.path.join(dp, "lga_master_774.csv"))[["state", "lga", "archetype_id", "pop_model_2024"]]
    r = pd.read_csv(os.path.join(dp, "lga_rainfall_seasonality_nasapower.csv"))
    d = m.merge(r, on=["state", "lga"])
    cols = [f"rain_mm_day_m{k:02d}" for k in range(1, 13)]
    X = d[cols].values
    X = X / X.mean(axis=1, keepdims=True)
    w = d["pop_model_2024"].values
    arch = sorted(d["archetype_id"].unique())
    A = np.array([np.average(X[d["archetype_id"] == a], axis=0, weights=w[d["archetype_id"] == a]) for a in arch])
    conc = A[:, [0, 1, 2, 10, 11]].mean(axis=1)
    qs = np.quantile(conc, np.linspace(0, 1, 5))
    lab = np.clip(np.searchsorted(qs[1:-1], conc, side="right"), 0, 3)
    order = np.argsort([conc[lab == k].mean() for k in range(4)])
    remap = {old: new for new, old in enumerate(order)}
    lab = np.array([remap[x] for x in lab])
    prof = {}
    for k in range(4):
        sel = np.isin(d["archetype_id"], np.array(arch)[lab == k])
        lagged = np.roll(np.average(X[sel], axis=0, weights=w[sel]), 1)
        s = np.maximum(lagged, 0.1 * lagged.mean())
        prof[k] = s / s.mean()
    return prof


def build(run_dir):
    dp = os.path.join(run_dir, "data", "processed")
    a = pd.read_csv(os.path.join(run_dir, "models", "allocation_320m.csv"))
    a = a[a.gf == 1].copy()
    m = pd.read_csv(os.path.join(dp, "lga_master_774.csv"))[
        ["lga_id", "lat", "lon", "arabiensis_rel_abundance", "gambiae_rel_abundance", "funestus_rel_abundance"]]
    d = a.merge(m, on="lga_id", how="left")
    prof = season_profiles(dp)
    out = pd.DataFrame({
        "lga_id": d.lga_id.astype(int), "state": d.state, "lga": d.lga, "archetype_id": d.archetype_id.astype(int),
        "pop_u5": d.pop_u5, "pop_5_14": d.pop_5_14, "pop_15p": d.pop_15p,
        "smc_pop": d.smc_children, "irs_eligible": d.irs_eligible.astype(int),
        "smc_eligible": (d.smc_children > 0).astype(int),
        "target_pfpr_u5_2021": d.pfpr_disagg_2021, "map_pfpr_2021": d.map_pfpr_2021,
        "resistance": d.resistance, "cm_ft": d.ft_base, "sgroup": d.sgroup.astype(int),
        "timing": d.timing, "lat": d.lat,
        "arabiensis": d.arabiensis_rel_abundance, "gambiae": d.gambiae_rel_abundance,
        "funestus": d.funestus_rel_abundance})
    out["pop"] = out.pop_u5 + out.pop_5_14 + out.pop_15p
    out["pop_5p"] = out.pop_5_14 + out.pop_15p
    out["campaign_year"] = out.timing.map(lambda t: TIMING[t][1])
    out["itn_hist_years"] = out.timing.map(lambda t: " ".join(str(y) for y in TIMING[t][0]))
    for k in range(12):
        out[f"season_m{k + 1:02d}"] = out.sgroup.map(lambda g: round(float(prof[g][k]), 4))
    s = out[["arabiensis", "gambiae", "funestus"]].sum(axis=1)
    for c in ("arabiensis", "gambiae", "funestus"):
        out[c] = (out[c] / s).round(4)
    return out


if __name__ == "__main__":
    run = sys.argv[1]
    u = build(run)
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    p = os.path.join(HERE, "data", "units.csv")
    u.to_csv(p, index=False)
    print(f"wrote {p}: {len(u)} units, {u.state.nunique()} states, pop {u['pop'].sum() / 1e6:.1f}M, "
          f"SMC-eligible {u.smc_eligible.sum()}, IRS-eligible {u.irs_eligible.sum()}, "
          f"arabiensis share (pop-weighted) {np.average(u.arabiensis, weights=u['pop']):.2f}")
