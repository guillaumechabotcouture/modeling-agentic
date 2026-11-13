#!/usr/bin/env python3
"""Synthetic units table for the world-model trial hepa_catchup_age_saturation (stdlib only, seeded).

    python generate_units.py <out.csv>

Each column listed in INDEPENDENT is drawn from its own distribution with its own random stream, independently of
every other column; the id and region columns are labels, and the columns in DERIVED are computed from the drawn ones
(engine inputs such as per-group sizes and susceptible shares; they are not declared independent). Nothing here is
measured data.
"""
import csv
import math
import random
import sys

SEED = 3105
N_UNITS = 24
COLUMNS = {"pop": ["int", 50000, 700000, 0], "s_u5": ["uniform", 0.6, 0.95, 4], "s_5_14": ["uniform", 0.4, 0.9, 4], "s_15p": ["uniform", 0.1, 0.5, 4]}      # name: [kind, lo, hi, digits]; kind uniform | loguniform | int
INDEPENDENT = ["pop", "s_u5", "s_5_14", "s_15p"]
DERIVED = {"n_groups": "'%d %d %d' % (round(pop * 0.15), round(pop * 0.25), pop - round(pop * 0.15) - round(pop * 0.25))", "s0_groups": "'%.4f %.4f %.4f' % (s_u5, s_5_14, s_15p)", "r0_target": "3.2"}
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
    for row in rows:
        for c, expr in DERIVED.items():
            row[c] = eval(expr, {"math": math}, dict(row))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
