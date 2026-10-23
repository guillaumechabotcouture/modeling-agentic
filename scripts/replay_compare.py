#!/usr/bin/env python3
"""Paired comparison of two replay variants (architecture v2, step 1).

    python3 scripts/replay_compare.py <batch_dir> --a <variant_id> --b <variant_id> [--out report.md]
    python3 scripts/replay_compare.py --self-test

Cases are paired on (fixture, seed), so fixture difficulty cancels out. For
each metric: mean of A, mean of B, mean paired delta (B - A) and a 95%
percentile bootstrap CI over pairs. With few fixtures the CI is wide by
design; report it, don't hide it. Direction is marked per metric
(stage_scorers.LOWER_IS_BETTER).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from stage_scorers import LOWER_IS_BETTER  # noqa: E402

TELEMETRY_METRICS = ["cost_usd", "wall_s", "turns", "input_tokens", "start_context"]
MIN_PAIRS = 3


def load_cases(batch_dir: str) -> list[dict]:
    return [json.load(open(p)) for p in glob.glob(os.path.join(batch_dir, "*", "*", "case.json"))]


def _metrics(case: dict) -> dict:
    m = dict(case.get("flat") or {})
    for k in TELEMETRY_METRICS:
        m[k] = (case.get("telemetry") or {}).get(k)
    m["completed_ok"] = float(case.get("status") in ("ok", "turn_cap"))
    return m


def bootstrap_ci(deltas: list[float], reps: int = 4000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(rng.choice(deltas) for _ in range(n)) / n for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def compare(cases: list[dict], a: str, b: str) -> dict:
    by = {}
    for c in cases:
        by.setdefault((c["fixture"], c["seed"]), {})[c["variant"]] = c
    pairs = [(v[a], v[b]) for v in by.values() if a in v and b in v]
    metrics = sorted({k for ca, cb in pairs for k in _metrics(ca)})
    rows = []
    for k in metrics:
        vals = [(_metrics(ca).get(k), _metrics(cb).get(k)) for ca, cb in pairs]
        vals = [(x, y) for x, y in vals if isinstance(x, (int, float)) and isinstance(y, (int, float))]
        if not vals:
            continue
        d = [y - x for x, y in vals]
        # Fewer than MIN_PAIRS pairs give no estimate of between-pair
        # variability: report the CI as unavailable, never as conclusive.
        ci = bootstrap_ci(d) if len(d) >= MIN_PAIRS else None
        mean_d = sum(d) / len(d)
        better = (mean_d < 0) if k in LOWER_IS_BETTER else (mean_d > 0)
        sig = ci is not None and (ci[1] < 0 or ci[0] > 0)
        lo, hi = ci if ci is not None else (None, None)
        rows.append({"metric": k, "n": len(vals), "mean_a": sum(x for x, _ in vals) / len(vals),
                     "mean_b": sum(y for _, y in vals) / len(vals), "delta": mean_d, "ci": (lo, hi),
                     "b_better": better if mean_d != 0 else None, "ci_excludes_0": sig})
    return {"a": a, "b": b, "n_pairs": len(pairs), "rows": rows}


def to_markdown(res: dict) -> str:
    out = [f"## Paired replay comparison: `{res['b']}` vs `{res['a']}`", "",
           f"{res['n_pairs']} paired cases (fixture × seed). Delta = B − A; 95% bootstrap CI over pairs.", "",
           "| Metric | n | A | B | Δ (B−A) | 95% CI | B better? |", "|---|---|---|---|---|---|---|"]
    for r in res["rows"]:
        arrow = "" if r["b_better"] is None else ("yes" if r["b_better"] else "no")
        lo, hi = r["ci"]
        if lo is None:
            arrow += f" (n < {MIN_PAIRS}, no CI)" if arrow else ""
            ci_txt = "n/a"
        else:
            if r["b_better"] is not None and not r["ci_excludes_0"]:
                arrow += " (CI spans 0)"
            ci_txt = f"[{lo:+.3g}, {hi:+.3g}]"
        out.append(f"| {r['metric']}{' ↓' if r['metric'] in LOWER_IS_BETTER else ''} | {r['n']} | "
                   f"{r['mean_a']:.3g} | {r['mean_b']:.3g} | {r['delta']:+.3g} | {ci_txt} | {arrow} |")
    out.append("")
    out.append("↓ = lower is better.")
    return "\n".join(out)


def _self_test() -> int:
    failures = []
    def ok(c, m):
        if not c: failures.append(m)
    cases = []
    for i, fx in enumerate(["F1", "F2", "F3", "F4"]):
        for v, rate, cost in (("A", 0.5, 10.0 + i), ("B", 0.75, 4.0 + i)):
            cases.append({"fixture": fx, "seed": 0, "variant": v, "status": "ok",
                          "flat": {"artifact_rate": rate + 0.01 * i},
                          "telemetry": {"cost_usd": cost, "wall_s": 100, "turns": 10, "input_tokens": 1e6,
                                        "start_context": 170000 if v == "A" else 30000}})
    cases.append({"fixture": "F9", "seed": 0, "variant": "A", "status": "ok", "flat": {}, "telemetry": {}})  # unpaired
    res = compare(cases, "A", "B")
    r = {x["metric"]: x for x in res["rows"]}
    ok(res["n_pairs"] == 4, f"C1 pairs: {res['n_pairs']}")
    ok(abs(r["artifact_rate"]["delta"] - 0.25) < 1e-9 and r["artifact_rate"]["b_better"], "C2 quality delta + direction")
    ok(r["cost_usd"]["delta"] == -6 and r["cost_usd"]["b_better"] and r["cost_usd"]["ci_excludes_0"], "C3 cost lower is better")
    ok(r["wall_s"]["b_better"] is None, "C4 zero delta has no direction")
    md = to_markdown(res)
    ok("| cost_usd ↓ |" in md and "4 paired cases" in md, "C5 markdown")
    ok(r["start_context"]["b_better"] and "| start_context ↓ |" in md, "C5b start_context lower is better")
    one = compare(cases[:2], "A", "B")
    r1 = {x["metric"]: x for x in one["rows"]}
    ok(r1["cost_usd"]["ci"] == (None, None) and not r1["cost_usd"]["ci_excludes_0"]
       and "n/a" in to_markdown(one), "C5c one pair → no CI, never conclusive")
    with tempfile.TemporaryDirectory() as td:
        for c in cases[:4]:
            d = os.path.join(td, c["variant"], f"{c['fixture']}-s0"); os.makedirs(d)
            json.dump(c, open(os.path.join(d, "case.json"), "w"))
        ok(len(load_cases(td)) == 4, "C6 load from batch dir")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - "); return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch_dir", nargs="?")
    ap.add_argument("--a"); ap.add_argument("--b"); ap.add_argument("--out")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not (a.batch_dir and a.a and a.b):
        ap.error("batch_dir, --a and --b are required")
    res = compare(load_cases(a.batch_dir), a.a, a.b)
    if res["n_pairs"] == 0:
        # A typo'd variant id must not overwrite a report with an empty analysis.
        print(f"ERROR: no paired cases for {a.a!r} vs {a.b!r} in {a.batch_dir}", file=sys.stderr)
        return 2
    md = to_markdown(res)
    if a.out:
        open(a.out, "w").write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
