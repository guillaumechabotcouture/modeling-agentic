#!/usr/bin/env python3
"""Synthetic units table for the world-model trial varicella_school_entry_saturation (stdlib only, seeded).

    python generate_units.py <out.csv>

Each column listed in INDEPENDENT is drawn from its own distribution with its own random stream, independently of
every other column; DERIVED columns are computed from the drawn ones (engine inputs, not declared independent). A
generator that draws independently can still realize a correlated table on a few units (batch 3: 0.82 between two
separately drawn columns over 24 units), so the seed is searched from SEED upward until the maximum pairwise |Spearman|
among the INDEPENDENT columns of the realized table is at most MAX_RHO; the chosen seed and that maximum are printed.
Nothing here is measured data.
"""
import csv
import math
import random
import sys

SEED = 4550
MAX_RHO = 0.2
N_UNITS = 24
COLUMNS = {"pop": ["int", 50000, 500000, 0], "s_u5": ["uniform", 0.5, 0.95, 4], "s_5_9": ["uniform", 0.15, 0.6, 4], "s_10_14": ["uniform", 0.05, 0.35, 4], "seed_share": ["loguniform", 1e-06, 0.0001, 8]}      # name: [kind, lo, hi, digits]; kind uniform | loguniform | int
INDEPENDENT = ["pop", "s_u5", "s_5_9", "s_10_14", "seed_share"]
DERIVED = {"n_groups": "'%d %d %d %d' % (round(pop * 0.12), round(pop * 0.13), round(pop * 0.13), pop - round(pop * 0.12) - round(pop * 0.13) - round(pop * 0.13))", "s0_groups": "'%.4f %.4f %.4f 0.05' % (s_u5, s_5_9, s_10_14)", "r0_target": "10.0"}
REGIONS = 4


def draw(rng, kind, lo, hi):
    if kind == "loguniform":
        return math.exp(rng.uniform(math.log(lo), math.log(hi)))
    if kind == "int":
        return rng.randint(int(lo), int(hi))
    return rng.uniform(lo, hi)


def ranks(x):
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def spearman(a, b):
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else 0.0


def table(seed):
    streams = {c: random.Random(f"{seed}:{c}") for c in COLUMNS}     # one stream per column
    rows = []
    for i in range(N_UNITS):
        row = {"id": f"U{i + 1:02d}", "region": f"R{i % REGIONS}"}
        for c, (kind, lo, hi, dg) in COLUMNS.items():
            v = draw(streams[c], kind, lo, hi)
            row[c] = int(v) if kind == "int" else round(v, dg)
        rows.append(row)
    rho = max((abs(spearman([r[a] for r in rows], [r[b] for r in rows]))
               for i, a in enumerate(INDEPENDENT) for b in INDEPENDENT[i + 1:]), default=0.0)
    return rows, rho


def main(out):
    for seed in range(SEED, SEED + 100000):
        rows, rho = table(seed)
        if rho <= MAX_RHO:
            break
    for row in rows:
        for c, expr in DERIVED.items():
            row[c] = eval(expr, {"math": math}, dict(row))      # engine inputs computed from the drawn columns
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"seed {seed} (searched from {SEED}): max |Spearman| among {INDEPENDENT} = {rho:.3f}")


if __name__ == "__main__":
    main(sys.argv[1])
