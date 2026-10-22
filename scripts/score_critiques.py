#!/usr/bin/env python3
"""Score critic arms against planted defects (experiment E5).

    python3 scripts/score_critiques.py --batch <batch_dir> --truth <fixtures_e5>/_truth
                                       [--out <dir>] [--audit-frac 0.2] [--seed 1]
    python3 scripts/score_critiques.py --self-test

A planted defect counts as DETECTED by an arm when at least one of the arm's
blockers (all string fields of the blocker: claim, evidence, fix_requires,
location, kind, ...)
  (1) cites the defect's file (path or basename), one of its `also_files`,
      or its location key (claim id, output name, column), AND
  (2) contains >= 1 of the defect's `specific` terms, or >= 2 `generic` terms.
Matching is case-insensitive. Every match records which rule fired.

Per arm it reports:
  recall overall / by class / by severity, with Wilson 95% CIs;
  prose_recall: same rule applied to the critique markdown (lenient, secondary);
  noise: blockers in planted variants matching no planted defect, minus that
         arm's blocker count on the same fixture's clean variant;
  spurious_match_rate: the matcher applied to the CLEAN variant's blockers for
         every defect planted in that fixture's other variants: how often the
         rule "detects" a defect that is not there (validity check of the rule);
  cost and turns.
An audit sample (random share of matches and misses, with blocker text) is
written for manual review.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import re
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stage_scorers  # noqa: E402


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def _text(b) -> str:
    out = []
    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (int, float)):
            out.append(str(x))
    walk(b)
    return " ".join(out).lower()


def _identifier_like(k: str) -> bool:
    """Location keys/specific terms must be distinctive: plain short words
    ('cost', 'budget') appear in unrelated blockers and made the rule fire on
    them (spurious-match rate 0.185 on clean variants before this filter)."""
    return len(k) >= 6 and bool(re.search(r"[_\d.\-:]", k)) or len(k) >= 12


def location_keys(defect: dict) -> list[str]:
    loc = str(defect.get("location") or "")
    keys = []
    m = re.match(r"claims\[([^\]]+)\]", loc)
    if m:
        keys.append(m.group(1))
    m = re.match(r"column (.+)$", loc)
    if m:
        keys.append(m.group(1))
    m = re.match(r"([\w.\-]+)\.(ci_low|ci_high)", loc)
    if m:
        keys.append(m.group(1))
    return [k.lower() for k in keys if _identifier_like(k)]


def _renderings(t) -> list[str]:
    """A planted number like 1.1499999999999999 is quoted as '1.15' by any
    sensible critic; accept 3-4 significant-figure renderings too."""
    t = str(t).lower()
    out = [t]
    try:
        x = float(t.replace(",", ""))
    except ValueError:
        return out
    for sig in (3, 4):
        r = f"{x:.{sig}g}"
        if len(r) >= 4 and "e" not in r:
            out.append(r)
    return out


# One edited number in a long prose/registry file: a blocker that cites the
# file and says "inconsistent" is about something else unless it quotes the
# planted value (smoke 2026-10-22: M-011 on LLIN cost bases matched this way).
REQUIRE_SPECIFIC = {"prose_stale_number", "citation_mismatch", "data_vintage", "ledger_mismatch"}


def match(defect: dict, text: str) -> dict | None:
    """Return the match reasons, or None. Terms that merely restate the cited
    file (its basename, or a substring of its path) don't count as evidence."""
    files = [defect["file"]] + list(defect.get("also_files") or [])
    paths = " ".join(f.lower() for f in files)
    cites = [f for f in files if f.lower() in text or os.path.basename(f).lower() in text]
    keys = location_keys(defect)
    cites += [k for k in keys if k in text]
    if not cites:
        return None
    spec = [t for t in defect["terms"]["specific"]
            if any(r in text for r in _renderings(t)) and str(t).lower() not in paths
            and str(t).lower() not in keys and _identifier_like(str(t))]
    gen = [t for t in defect["terms"]["generic"]
           if str(t).lower() in text and str(t).lower() not in paths]
    if spec or (len(gen) >= 2 and defect.get("class") not in REQUIRE_SPECIFIC):
        return {"cites": cites, "specific": spec, "generic": gen}
    return None


def _arm_of(case: dict) -> str:
    return case["variant"]


def _agent_of(case: dict) -> str:
    f = ((case.get("scores") or {}).get("critique") or {}).get("file", "")
    for a, fn in stage_scorers.CRITIQUE_FILES.items():
        if fn == f:
            return a
    return "?"


def load_cases(batch: str) -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(batch, "*", "*", "case.json"))):
        c = json.load(open(p))
        if c.get("stage") != "CRITIQUE":
            continue
        c["_dir"] = os.path.dirname(p)
        out.append(c)
    return out


def _run_dir(case):
    d = os.path.join(case["_dir"], "run")
    return d if os.path.isdir(d) else os.path.join(case.get("workspace", ""), "run")


def score(batch: str, truth_dir: str, audit_frac: float = 0.2, seed: int = 1) -> dict:
    truth = {os.path.basename(p)[:-5]: yaml.safe_load(open(p)) for p in glob.glob(os.path.join(truth_dir, "*.yaml"))}
    cases = load_cases(batch)
    arms: dict[str, dict] = {}
    audit = []
    by_arm_fixture_blockers: dict[tuple, int] = {}
    for c in cases:
        arm = _arm_of(c)
        agent = _agent_of(c)
        A = arms.setdefault(arm, {"agent": agent, "rows": [], "cost": 0.0, "cost_n": 0, "turns": 0, "cases": 0,
                                  "clean_blockers": {}, "planted_unmatched": {}, "clean_texts": {},
                                  "status": {}})
        A["cases"] += 1
        A["status"][c["status"]] = A["status"].get(c["status"], 0) + 1
        t = c.get("telemetry") or {}
        if t.get("cost_usd") is not None:
            A["cost"] += t["cost_usd"]; A["cost_n"] += 1
        A["turns"] += t.get("turns") or 0
        fid = c["fixture"]
        tr = truth.get(fid)
        if tr is None:
            continue
        run = _run_dir(c)
        blockers, _ = stage_scorers.critique_blockers(run, agent)
        texts = [_text(b) for b in blockers]
        md = os.path.join(run, stage_scorers.CRITIQUE_FILES.get(agent, "x.yaml").replace(".yaml", ".md"))
        prose = open(md, errors="replace").read().lower() if os.path.exists(md) else ""
        src = tr["source_fixture"]
        if tr["tag"] == "clean":
            A["clean_blockers"][src] = len(blockers)
            A["clean_texts"][src] = texts
            continue
        matched_idx = set()
        for d in tr["defects"]:
            hit = None
            for i, tx in enumerate(texts):
                r = match(d, tx)
                if r:
                    hit = (i, r); matched_idx.add(i); break
            prose_hit = match(d, prose) if prose else None
            row = {"defect_id": d["defect_id"], "class": d["class"], "severity": d["severity"],
                   "fixture": fid, "source": src, "detected": hit is not None,
                   "prose_detected": prose_hit is not None,
                   "reasons": hit[1] if hit else None, "blocker_id": blockers[hit[0]].get("id") if hit else None}
            A["rows"].append(row)
            audit.append({"arm": arm, **row, "description": d["description"],
                          "blocker_text": (texts[hit[0]][:600] if hit else None)})
        A["planted_unmatched"].setdefault(src, []).append(len(blockers) - len(matched_idx))
    report = {"arms": {}, "n_cases": len(cases)}
    for arm, A in arms.items():
        rows = A["rows"]
        k, n = sum(r["detected"] for r in rows), len(rows)
        kp = sum(r["prose_detected"] for r in rows)
        def grp(key):
            g = {}
            for r in rows:
                g.setdefault(r[key], [0, 0]); g[r[key]][0] += r["detected"]; g[r[key]][1] += 1
            return {x: {"detected": a, "planted": b, "recall": round(a / b, 3), "ci95": wilson(a, b)}
                    for x, (a, b) in sorted(g.items())}
        # noise: unmatched blockers per planted variant vs clean-variant blockers, same source fixture
        deltas = [u - A["clean_blockers"][s] for s, us in A["planted_unmatched"].items()
                  if s in A["clean_blockers"] for u in us]
        # spurious: matcher on clean blockers for defects planted elsewhere in that fixture
        sp_k = sp_n = 0
        for t in truth.values():
            if t["tag"] == "clean" or t["source_fixture"] not in A["clean_texts"]:
                continue
            for d in t["defects"]:
                sp_n += 1
                sp_k += any(match(d, tx) for tx in A["clean_texts"][t["source_fixture"]])
        report["arms"][arm] = {
            "agent": A["agent"], "cases": A["cases"], "status": A["status"],
            "planted": n, "detected": k, "recall": round(k / n, 3) if n else None, "recall_ci95": wilson(k, n),
            "prose_recall": round(kp / n, 3) if n else None,
            "by_class": grp("class"), "by_severity": grp("severity"),
            "noise_new_blockers_mean": round(sum(deltas) / len(deltas), 2) if deltas else None,
            "spurious_match_rate": round(sp_k / sp_n, 3) if sp_n else None, "spurious_checked": sp_n,
            "cost_usd": round(A["cost"], 2) if A["cost_n"] else None,
            "cost_per_case": round(A["cost"] / A["cost_n"], 2) if A["cost_n"] else None,
            "turns": A["turns"],
        }
    rng = random.Random(seed)
    report["audit_sample"] = [a for a in audit if rng.random() < audit_frac]
    return report


def to_markdown(rep: dict) -> str:
    L = ["## E5 critic effectiveness", "",
         "| arm | agent | planted | recall | 95% CI | prose recall | noise Δ | spurious | $/case |",
         "|---|---|---|---|---|---|---|---|---|"]
    for arm, r in sorted(rep["arms"].items()):
        L.append(f"| {arm} | {r['agent']} | {r['planted']} | {r['recall']} | {r['recall_ci95']} | "
                 f"{r['prose_recall']} | {r['noise_new_blockers_mean']} | {r['spurious_match_rate']} | {r['cost_per_case']} |")
    classes = sorted({c for r in rep["arms"].values() for c in r["by_class"]})
    if classes:
        L += ["", "### Recall by class", "", "| class | " + " | ".join(sorted(rep["arms"])) + " |",
              "|---|" + "---|" * len(rep["arms"])]
        for c in classes:
            cells = []
            for arm in sorted(rep["arms"]):
                x = rep["arms"][arm]["by_class"].get(c)
                cells.append(f"{x['detected']}/{x['planted']}" if x else "–")
            L.append(f"| {c} | " + " | ".join(cells) + " |")
    return "\n".join(L) + "\n"


def _self_test() -> int:
    failures = []
    def ok(c, m):
        if not c: failures.append(m)
    with tempfile.TemporaryDirectory() as td:
        truth = os.path.join(td, "_truth"); os.makedirs(truth)
        d1 = {"defect_id": "F__p1#1", "class": "zero_width_ci", "file": "uncertainty_report.yaml",
              "location": "total_dalys.ci_low/ci_high", "severity": "decision-critical", "description": "x",
              "terms": {"specific": ["total_dalys", "zero-width"], "generic": ["degenerate", "interval", "collapsed"]}}
        d2 = {"defect_id": "F__p1#2", "class": "budget_overspend", "file": "models/allocation.csv",
              "location": "column cost", "severity": "decision-critical", "description": "y",
              "terms": {"specific": ["allocation.csv", "1.55"], "generic": ["budget", "exceed", "overspend"]}}
        yaml.safe_dump({"variant": "F__p1", "source_fixture": "F", "tag": "p1", "defects": [d1, d2]},
                       open(os.path.join(truth, "F__p1.yaml"), "w"))
        yaml.safe_dump({"variant": "F__clean", "source_fixture": "F", "tag": "clean", "defects": []},
                       open(os.path.join(truth, "F__clean.yaml"), "w"))
        batch = os.path.join(td, "b")
        def case(arm, fid, blockers, cost=1.0):
            d = os.path.join(batch, arm, f"{fid}-s0"); run = os.path.join(d, "run"); os.makedirs(run)
            yaml.safe_dump({"reviewer": "critique-methods", "blockers": blockers},
                           open(os.path.join(run, "critique_methods.yaml"), "w"))
            json.dump({"fixture": fid, "variant": arm, "stage": "CRITIQUE", "status": "ok",
                       "telemetry": {"cost_usd": cost, "turns": 10},
                       "scores": {"critique": {"file": "critique_methods.yaml"}}}, open(os.path.join(d, "case.json"), "w"))
        case("m", "F__clean", [{"id": "M-1", "claim": "Report lacks a methods section"}])
        case("m", "F__p1", [{"id": "M-1", "claim": "Report lacks a methods section"},
                            {"id": "M-2", "claim": "uncertainty_report.yaml: total_dalys interval is zero-width",
                             "evidence": "ci_low equals ci_high"},
                            {"id": "M-3", "claim": "Budget looks fine overall"}])
        rep = score(batch, truth, audit_frac=1.0)
        r = rep["arms"]["m"]
        ok(r["planted"] == 2 and r["detected"] == 1 and r["recall"] == 0.5, f"S1 recall: {r}")
        ok(r["by_class"]["zero_width_ci"]["detected"] == 1 and r["by_class"]["budget_overspend"]["detected"] == 0,
           "S2 by-class detection")
        ok(r["noise_new_blockers_mean"] == 1.0, f"S3 noise delta (2 unmatched - 1 clean): {r['noise_new_blockers_mean']}")
        ok(r["spurious_match_rate"] == 0.0 and r["spurious_checked"] == 2, f"S4 spurious on clean: {r}")
        ok(match(d2, "budget is fine") is None, "S5 no file/location citation -> no match")
        ok(match(d2, "models/allocation.csv sums exceed the budget") is not None, "S6 file + 2 generic terms")
        ok(match(d1, "uncertainty_report.yaml looks degenerate") is None, "S7 one generic term is not enough")
        ok(wilson(5, 10)[0] < 0.5 < wilson(5, 10)[1], "S8 Wilson interval")
        ok(match(d2, "models/allocation.csv figure is stale") is None,
           "S10 a specific term equal to the cited filename is not evidence")
        ok(match(d1, "total_dalys row in uncertainty_report.yaml is fine") is None,
           "S11 citing the location key alone is not a detection")
        ok(match(d1 | {"terms": {"specific": ["1.1499999999999999"], "generic": []}},
                 "uncertainty_report.yaml ci [1.15, 1]") is not None,
           "S12 planted number matched at 3 significant figures")
        d3 = {"class": "prose_stale_number", "file": "results.md", "location": "line 9",
              "terms": {"specific": ["66,830"], "generic": ["stale", "inconsistent", "does not match"]}}
        ok(match(d3, "results.md costs are inconsistent and stale") is None
           and match(d3, "results.md says 66,830 deaths averted") is not None,
           "S13 single-number classes need the planted value")
        ok(len(rep["audit_sample"]) == 2 and "| m |" in to_markdown(rep), "S9 audit sample + markdown")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - "); return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch")
    ap.add_argument("--truth")
    ap.add_argument("--out")
    ap.add_argument("--audit-frac", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    rep = score(a.batch, a.truth, a.audit_frac, a.seed)
    out = a.out or a.batch
    os.makedirs(out, exist_ok=True)
    json.dump(rep, open(os.path.join(out, "e5_scores.json"), "w"), indent=1, default=str)
    open(os.path.join(out, "e5_report.md"), "w").write(to_markdown(rep))
    with open(os.path.join(out, "e5_audit_sample.md"), "w") as f:
        for x in rep["audit_sample"]:
            f.write(f"### {x['arm']} · {x['defect_id']} · {x['class']} · detected={x['detected']}\n"
                    f"- planted: {x['description']}\n- reasons: {x['reasons']}\n"
                    f"- blocker: {x['blocker_text']}\n\n")
    print(to_markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
