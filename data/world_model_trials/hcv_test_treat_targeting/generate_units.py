#!/usr/bin/env python3
"""Synthetic units table for the world-model trial hcv_test_treat_targeting (stdlib only, seeded).

    python generate_units.py <out.csv>

Each column listed in INDEPENDENT is drawn from its own distribution with its own random stream, independently of
every other column; the id and region columns are labels. Nothing here is measured data.
"""
import csv
import math
import random
import sys

SEED = 707
N_UNITS = 24
COLUMNS = {"pop": ["loguniform", 2000, 60000, 0], "inc": ["loguniform", 0.005, 0.25, 4], "group_code": ["int", 0, 3, 0], "linkage": ["uniform", 0.3, 0.9, 3]}      # name: [kind, lo, hi, digits]; kind uniform | loguniform | int
INDEPENDENT = ["pop", "inc", "group_code", "linkage"]
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
    extra = {}
    for row in rows:
        for c, expr in extra.items():
            row[c] = eval(expr, {"math": math}, dict(row))      # derived label columns (not design features)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
