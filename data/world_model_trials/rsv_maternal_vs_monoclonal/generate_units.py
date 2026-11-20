#!/usr/bin/env python3
"""Synthetic units table for the world-model trial rsv_maternal_vs_monoclonal (stdlib only, seeded).

    python generate_units.py <out.csv>

Each column in COLUMNS is drawn from its own distribution with its own random stream, independently of every other
column. Because separately drawn columns can still realize a correlated table on a few units (batch 3: rank
correlation 0.82 between two such columns over 24 units), the generator searches seeds SEED, SEED + 1, ... and keeps
the first table whose INDEPENDENT columns have pairwise |Spearman| <= RHO_MAX; CHOSEN_SEED records the result. The id
and region columns are labels; the DERIVED columns are computed from the drawn ones (engine inputs; never declared
independent). Nothing here is measured data.
"""
import csv
import math
import random
import sys

SEED = 4206
CHOSEN_SEED = 4211
N_UNITS = 24
COLUMNS = {"pop": ["int", 3000, 40000, 0], "inc": ["uniform", 0.01, 0.05, 4], "anc_attendance": ["uniform", 0.5, 0.95, 3]}      # name: [kind, lo, hi, digits]; kind uniform | loguniform | int
INDEPENDENT = ["inc", "anc_attendance", "pop"]
DERIVED = {}
REGIONS = 4
RHO_MAX = 0.2


def draw(rng, kind, lo, hi):
    if kind == "loguniform":
        return math.exp(rng.uniform(math.log(lo), math.log(hi)))
    if kind == "int":
        return rng.randint(int(lo), int(hi))
    return rng.uniform(lo, hi)


def ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0
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
    return rows


def max_rho(rows):
    cols = {c: [float(r[c]) for r in rows] for c in INDEPENDENT}
    return max((abs(spearman(cols[a], cols[b])) for i, a in enumerate(INDEPENDENT) for b in INDEPENDENT[i + 1:]),
               default=0.0)


def search():
    for k in range(5000):
        rows = table(SEED + k)
        if max_rho(rows) <= RHO_MAX:
            return SEED + k, rows
    raise SystemExit("no seed within 5000 gives uncorrelated independent columns")


def main(out):
    seed, rows = search()
    if CHOSEN_SEED is not None and seed != CHOSEN_SEED:
        raise SystemExit(f"seed search gave {seed}, not the recorded CHOSEN_SEED {CHOSEN_SEED}")
    for row in rows:
        for c, expr in DERIVED.items():
            row[c] = eval(expr, {"math": math}, dict(row))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"seed {seed}, max |Spearman| among independent columns {max_rho(rows):.3f}")


if __name__ == "__main__":
    main(sys.argv[1])
