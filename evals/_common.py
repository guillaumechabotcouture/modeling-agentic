"""Shared helpers for the eval-suite generators (Phase 50 α).

Pure Python on purpose: no numpy, so a generator gives byte-identical data on
every machine and library version (numpy's Generator does not promise the same
stream across releases). Everything here is disease-agnostic; each question's
generating model lives in evals/<q>/generate.py.

A question's input data are written to data/evals/<q>/ (shipped with the code, like
the rest of data/); nothing under evals/ is shipped to agents or cloud workers
(.gitattributes export-ignore; cloud/run_cluster.py code_tarball refuses a tarball
that holds any of it; agents/enforcement.py denies agents reading it).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random


class Rng:
    """Seeded sampler built only on random.Random.random() (stable across Python versions)."""

    def __init__(self, seed: int):
        self.r = random.Random(seed)

    def random(self) -> float:
        return self.r.random()

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.r.random()

    def normal(self, mu: float = 0.0, sd: float = 1.0) -> float:
        u1 = self.r.random() or 1e-300
        u2 = self.r.random()
        return mu + sd * math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam < 60:   # Knuth
            limit, k, p = math.exp(-lam), 0, 1.0
            while True:
                p *= self.r.random()
                if p <= limit:
                    return k
                k += 1
        return max(0, int(round(self.normal(lam, math.sqrt(lam)))))   # normal approximation

    def binomial(self, n: int, p: float) -> int:
        if n <= 50000:
            return sum(1 for _ in range(n) if self.r.random() < p)
        return max(0, min(n, int(round(self.normal(n * p, math.sqrt(n * p * (1 - p)))))))

    def choice(self, items, weights=None):
        items = list(items)
        if weights is None:
            return items[int(self.r.random() * len(items)) % len(items)]
        tot, x = float(sum(weights)), self.r.random()
        acc = 0.0
        for it, w in zip(items, weights):
            acc += w / tot
            if x < acc:
                return it
        return items[-1]


def fmt(x) -> str:
    if isinstance(x, bool):
        return "1" if x else "0"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        return format(x, ".6g")
    return str(x)


def write_csv(path: str, header: list[str], rows: list[list]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="\n") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join(fmt(v) for v in r) + "\n")


def _round(obj, sig: int = 10):
    if isinstance(obj, float):
        return float(format(obj, f".{sig}g")) if math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {str(k): _round(v, sig) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, sig) for v in obj]
    return obj


def digest_dir(d: str) -> dict:
    out = {}
    for name in sorted(os.listdir(d)):
        with open(os.path.join(d, name), "rb") as f:
            out[name] = hashlib.sha256(f.read()).hexdigest()
    return out


def write_truth(truth_dir: str, truth: dict, inputs_dir: str) -> str:
    """The answer key: <truth_dir>/truth.json (evals/<q>/_truth/, not committed, never
    shipped). It records the digest of every input file it belongs to."""
    truth["inputs_sha256"] = digest_dir(inputs_dir)
    os.makedirs(truth_dir, exist_ok=True)
    p = os.path.join(truth_dir, "truth.json")
    with open(p, "w", newline="\n") as f:
        json.dump(_round(truth), f, indent=1, sort_keys=True)
        f.write("\n")
    return p


# ---- exact optimisers used to compute the truth --------------------------------------------

def mckp(benefit: dict, cost: dict, feasible: dict, budget: float, sense: str = "max") -> dict:
    """Exact multiple-choice knapsack: one package per unit, total cost <= budget.
    Pareto-frontier dynamic programme (exact, deterministic tie-break by unit order
    then package order). Independent of toolkit/alloc so the truth does not share
    the agents' optimiser."""
    sign = 1.0 if sense == "max" else -1.0
    units = sorted(benefit)
    states = [(0.0, 0.0, ())]
    for u in units:
        new = []
        for c, v, ch in states:
            for p in feasible[u]:
                c2 = c + cost[u][p]
                if c2 <= budget * (1 + 1e-12) + 1e-9:
                    new.append((c2, v + sign * benefit[u][p], ch + (p,)))
        new.sort(key=lambda t: (t[0], -t[1]))
        pruned, best = [], -math.inf
        for t in new:
            if t[1] > best + 1e-9:
                pruned.append(t)
                best = t[1]
        states = pruned
    c, v, ch = max(states, key=lambda t: (round(t[1], 9), -t[0]))
    return {"choice": dict(zip(units, ch)), "objective": sign * v, "cost": c}


def plan_value(choice: dict, benefit: dict) -> float:
    return sum(benefit[u][p] for u, p in choice.items())


def plan_cost(choice: dict, cost: dict) -> float:
    return sum(cost[u][p] for u, p in choice.items())


def acceptable_packages(benefit, cost, feasible, budget, opt: dict, tie_tol: float) -> dict:
    """Per unit, the packages whose best plan (that unit forced to the package) is
    within tie_tol of the optimum: a near-tie is not counted as a wrong choice."""
    out, forced = {}, {}
    for u in sorted(benefit):
        out[u], forced[u] = [], {}
        for p in feasible[u]:
            f = dict(feasible)
            f[u] = [p]
            try:
                r = mckp(benefit, cost, f, budget)
            except ValueError:
                continue
            forced[u][p] = r["objective"]
            if r["objective"] >= opt["objective"] * (1 - tie_tol) - 1e-9:
                out[u].append(p)
    return {"acceptable": out, "forced_objective": forced}


def compositions(total: int, parts: int, caps=None):
    """Every split of `total` identical blocks into `parts` ordered groups (caps per group)."""
    caps = caps or [total] * parts

    def rec(i, left):
        if i == parts - 1:
            if left <= caps[i]:
                yield (left,)
            return
        for k in range(min(left, caps[i]) + 1):
            for rest in rec(i + 1, left - k):
                yield (k,) + rest
    yield from rec(0, total)


def final_size(r_eff: float, tol: float = 1e-13) -> float:
    """Classical SIR final size z = 1 - exp(-R z) (fraction of the susceptibles infected)."""
    if r_eff <= 1.0:
        return 0.0
    z = 1.0 - 1.0 / (r_eff * 2) if r_eff < 2 else 0.99
    for _ in range(10000):
        z2 = 1.0 - math.exp(-r_eff * z)
        if abs(z2 - z) < tol:
            return z2
        z = z2
    return z
