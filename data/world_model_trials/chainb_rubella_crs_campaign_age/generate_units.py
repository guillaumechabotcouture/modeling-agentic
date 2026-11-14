#!/usr/bin/env python3
"""Synthetic units table for the world-model trial rubella_crs_campaign_age (stdlib only, seeded).

    python generate_units.py <out.csv>

Each column listed in INDEPENDENT is drawn from its own distribution with its own random stream, independently of
every other column; the id and region columns are labels. Nothing here is measured data.
"""
import csv
import math
import random
import sys

SEED = 10229
N_UNITS = 24
COLUMNS = {"n_children": ["loguniform", 30000, 200000, 0], "n_women": ["loguniform", 20000, 150000, 0], "n_other": ["loguniform", 50000, 400000, 0], "s0_children": ["uniform", 0.2, 0.6, 4], "s0_women": ["uniform", 0.05, 0.3, 4], "s0_other": ["uniform", 0.02, 0.1, 4]}      # name: [kind, lo, hi, digits]; kind uniform | loguniform | int
INDEPENDENT = ["n_children", "n_women", "n_other", "s0_children", "s0_women", "s0_other"]
REGIONS = 4


def draw(rng, kind, lo, hi):
    if kind == "loguniform":
        return math.exp(rng.uniform(math.log(lo), math.log(hi)))
    if kind == "int":
        return rng.randint(int(lo), int(hi))
    return rng.uniform(lo, hi)


def main(out):
    streams = {c: random.Random(f"{SEED}:{c}") for c in COLUMNS}     # one stream per column: independent draws
    rows = []
    for i in range(N_UNITS):
        row = {"id": f"U{i + 1:02d}", "region": f"R{i % REGIONS}"}
        for c, (kind, lo, hi, dg) in COLUMNS.items():
            v = draw(streams[c], kind, lo, hi)
            row[c] = int(v) if kind == "int" else round(v, dg)
        rows.append(row)
    extra = {"pop": "n_children + n_women + n_other", "pop_children": "n_children", "pop_children_women": "n_children + n_women", "crs_ratio_c": "0.0", "crs_ratio_w": "0.015", "crs_ratio_o": "0.0", "r0_target": "5.5"}
    for row in rows:
        for c, expr in extra.items():
            row[c] = eval(expr, {"math": math}, dict(row))      # derived label columns (not design features)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
