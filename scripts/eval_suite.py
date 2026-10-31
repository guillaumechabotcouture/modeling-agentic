#!/usr/bin/env python3
"""Eval suite: planted-truth questions scored by deterministic graders (Phase 50 α).

A harness change is judged by four numbers per run: answer quality (graded against a
known-good answer), rounds to converge, dollars and wall time. Each question in
`evals/suite.yaml` has a seeded generator that writes synthetic inputs to
`data/evals/<q>/` (shipped) and the answer key to `evals/<q>/_truth/truth.json` (never
shipped), a rubric (`evals/<q>/rubric.yaml`) and a launcher config
(`cloud/configs/eval_<q>.py`). This script is disease-agnostic: every question-specific
fact is in the rubric and the truth file.

    python3 scripts/eval_suite.py generate [--check] [q ...]
        write inputs and answer keys; --check regenerates into a temp dir and fails if
        the committed inputs differ (the generators are deterministic)
    python3 scripts/eval_suite.py run [--tag T] [--reps N] [--execute] [q ...]
        preflight (inputs current, nothing hidden in the launch tarball), then print the
        launch commands; --execute runs them (each launch costs up to the US$35 cap)
    python3 scripts/eval_suite.py grade <q> <run_dir> [--out scores.json] [--gated]
    python3 scripts/eval_suite.py grade --root <dir> [--out suite_scores.json]
        <dir>/<q>/<label>/runs/<run>/ as `collect` writes it (~/modeling-agentic-keep/evals/<tag>)
    python3 scripts/eval_suite.py report <suite_scores.json> [<other suite_scores.json>]
        a table; with two files, B against A per question
    python3 scripts/eval_suite.py --self-test

Grading has two steps, so every score can be audited: `extract_answer` turns the run's
artifacts into a JSON answer (allocation, headline values and intervals, gate action,
rounds, dollars, wall time), then `score` compares it with the truth. Checks hang off a
subtask DAG (OmicsBench-style): `quality` is the weighted mean of check scores minus
penalties; `quality_gated` credits a check only when every upstream subtask passed.
Each failed check names a failure code, which `evals/taxonomy.yaml` maps to the harness
layer most likely at fault; `primary_layer` is the most upstream one.
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EVALS = os.path.join(REPO, "evals")
DATA = os.path.join(REPO, "data", "evals")
sys.path.insert(0, os.path.join(HERE, "lib"))
import eval_paths  # noqa: E402

DEFAULT_TEXT_FILES = ("report.md", "results.md", "decision_rule.md", "scope_declaration.yaml")
MAX_TEXT_BYTES = 2_000_000
CHECK_TYPES = ("inputs_referenced", "unit_value_accuracy", "text_mentions", "allocation_concordance",
               "allocation_value", "headline_accuracy", "interval_coverage", "scope_honesty",
               "gate_outcome")


# ---- loading ----------------------------------------------------------------------------

def _yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def suite_questions(evals_dir=EVALS) -> list[str]:
    return list(_yaml(os.path.join(evals_dir, "suite.yaml"))["questions"])


def load_rubric(q, evals_dir=EVALS) -> dict:
    return _yaml(os.path.join(evals_dir, q, "rubric.yaml"))


def load_taxonomy(evals_dir=EVALS) -> dict:
    return _yaml(os.path.join(evals_dir, "taxonomy.yaml"))


def load_generator(q, evals_dir=EVALS):
    path = os.path.join(evals_dir, q, "generate.py")
    spec = importlib.util.spec_from_file_location(f"_evalgen_{q}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def digest_dir(d) -> dict:
    out = {}
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                out[name] = hashlib.sha256(f.read()).hexdigest()
    return out


def check_inputs(q, evals_dir=EVALS, data_dir=DATA) -> list[str]:
    """Regenerate into a temp dir; list the committed inputs that differ (empty = current)."""
    gen = load_generator(q, evals_dir)
    with tempfile.TemporaryDirectory() as td:
        gen.generate(os.path.join(td, "inputs"), os.path.join(td, "_truth"))
        fresh = digest_dir(os.path.join(td, "inputs"))
    committed_dir = os.path.join(data_dir, q)
    committed = digest_dir(committed_dir) if os.path.isdir(committed_dir) else {}
    return sorted(n for n in set(fresh) | set(committed) if fresh.get(n) != committed.get(n))


def ensure_truth(q, evals_dir=EVALS, data_dir=DATA) -> dict:
    """The answer key for the committed inputs; regenerated when missing or stale. Refuses
    when the generator no longer reproduces the committed inputs (fail closed)."""
    tpath = os.path.join(evals_dir, q, "_truth", "truth.json")
    inputs_dir = os.path.join(data_dir, q)
    current = digest_dir(inputs_dir) if os.path.isdir(inputs_dir) else {}
    if os.path.exists(tpath):
        with open(tpath) as f:
            truth = json.load(f)
        if truth.get("inputs_sha256") == current and current:
            return truth
    gen = load_generator(q, evals_dir)
    with tempfile.TemporaryDirectory() as td:
        truth = gen.generate(os.path.join(td, "inputs"), os.path.join(evals_dir, q, "_truth"))
        if digest_dir(os.path.join(td, "inputs")) != current:
            raise SystemExit(f"{q}: the generator does not reproduce data/evals/{q}/ "
                             f"(run `eval_suite.py generate {q}` and commit the inputs)")
    with open(tpath) as f:
        return json.load(f)


# ---- answer extraction ------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def norm_label(s) -> str:
    """Package/unit label in a canonical form: case, spaces and component order ignored."""
    toks = [t for t in re.split(r"\s*(?:\+|&|/|,|\band\b|\bwith\b)\s*", str(s).strip(), flags=re.I) if t]
    return "+".join(sorted(t.strip().upper().replace(" ", "_") for t in toks))


def _label_map(truth_labels, aliases) -> dict:
    m = {norm_label(x): x for x in truth_labels}
    for alias, canon in (aliases or {}).items():
        if canon in truth_labels:
            m.setdefault(norm_label(alias), canon)
    return m


def _pick_col(header, wanted):
    low = {h.strip().lower(): h for h in header if h}
    for w in wanted or ():
        if w.lower() in low:
            return low[w.lower()]
    return None


def _allocation_files(run_dir, spec):
    seen, out = set(), []
    for pat in spec.get("files", []):
        for p in sorted(glob.glob(os.path.join(run_dir, pat))):
            if p not in seen and os.path.isfile(p):
                seen.add(p)
                out.append(p)
    return out


def extract_allocation(run_dir, rubric, truth) -> dict:
    spec, al = rubric.get("allocation"), truth.get("allocation")
    if not spec or not al:
        return {"status": "not_applicable"}
    units = _label_map(al["units"], spec.get("aliases"))
    vector = spec.get("kind") == "vector"
    pkgs = {} if vector else _label_map(al["packages"], spec.get("aliases"))
    for path in _allocation_files(run_dir, spec):
        try:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
        if not rows:
            continue
        header = list(rows[0].keys())
        ucol = _pick_col(header, spec.get("unit_column"))
        pcol = _pick_col(header, spec.get("quantity_column" if vector else "package_column"))
        vcol = _pick_col(header, spec.get("value_column"))
        if not ucol or not pcol:
            continue
        choice, values, unknown, raw, row_problems = {}, {}, [], {}, []
        for i, r in enumerate(rows, start=2):   # line numbers as in the file (header is line 1)
            label = (r.get(ucol) or "").strip()
            u = units.get(norm_label(label))
            if u is None:
                if label or any((v or "").strip() for v in r.values()):
                    row_problems.append(f"line {i}: unit {label!r} is not one of the question's units")
                continue
            if u in choice or u in raw:
                row_problems.append(f"line {i}: unit {u} appears more than once")
                continue
            if vector:
                q = _num(r.get(pcol))
                if q is None:
                    unknown.append(r.get(pcol))
                    row_problems.append(f"line {i}: {pcol} {r.get(pcol)!r} is not recognised")
                    continue
                blocks = q if "block" in pcol.lower() else q / al["block_size"]
                raw[u] = blocks   # unrounded: feasibility is checked on what the run reported
                choice[u] = int(round(blocks))
            else:
                p = pkgs.get(norm_label(r.get(pcol, "")))
                if p is None:
                    unknown.append(r.get(pcol))
                    row_problems.append(f"line {i}: {pcol} {r.get(pcol)!r} is not recognised")
                    continue
                choice[u] = p
            if vcol:
                values[u] = _num(r.get(vcol))
        return {"status": "ok", "file": os.path.relpath(path, run_dir), "choice": choice, "values": values,
                "raw_blocks": raw, "budget_tol": spec.get("budget_tol", 0.005), "row_problems": row_problems,
                "unknown_labels": unknown, "missing_units": sorted(set(al["units"]) - set(choice))}
    return {"status": "missing"}


def extract_headlines(run_dir, rubric) -> dict:
    """{headline: {output, point, ci_low, ci_high}} from uncertainty_report.yaml scalar_outputs."""
    want = {}
    for c in rubric.get("checks", []):
        if c.get("headline"):
            want.setdefault(c["headline"], c.get("outputs") or [c["headline"]])
    try:
        rep = _yaml(os.path.join(run_dir, "uncertainty_report.yaml")) or {}
    except (OSError, yaml.YAMLError):
        rep = {}
    outs = rep.get("scalar_outputs") if isinstance(rep, dict) else None
    outs = outs if isinstance(outs, dict) else {}
    found = {}
    for h, pats in want.items():
        for pat in pats:
            hit = next((k for k in sorted(outs) if re.fullmatch(pat, str(k), flags=re.I)), None)
            if hit is not None and isinstance(outs[hit], dict):
                o = outs[hit]
                point = _num(o.get("mean")) if _num(o.get("mean")) is not None else _num(o.get("median"))
                found[h] = {"output": hit, "point": point, "ci_low": _num(o.get("ci_low")),
                            "ci_high": _num(o.get("ci_high"))}
                break
    return found


def extract_process(run_dir) -> dict:
    meta = _read_json(os.path.join(run_dir, "metadata.json"))
    gate = _read_json(os.path.join(run_dir, "final_gate.json")) or {}
    out = {"has_metadata": isinstance(meta, dict), "status": None, "action": gate.get("action"),
           "rounds": None, "cost_usd": None, "wall_hours": None,
           "has_report": os.path.isfile(os.path.join(run_dir, "report.md"))}
    meta = meta if isinstance(meta, dict) else {}
    out["status"] = meta.get("status")
    fg = meta.get("final_gate") if isinstance(meta.get("final_gate"), dict) else {}
    out["action"] = out["action"] or fg.get("action")
    out["rounds"] = fg.get("current_round")
    if out["rounds"] is None:
        try:
            with open(os.path.join(run_dir, "validator_history.jsonl")) as f:
                rounds = [json.loads(ln).get("round") for ln in f if ln.strip()]
            out["rounds"] = max((r for r in rounds if isinstance(r, int)), default=None)
        except (OSError, ValueError):
            pass
    out["cost_usd"] = _num(meta.get("total_cost_usd"))
    if out["cost_usd"] is None:
        led = _read_json(os.path.join(run_dir, "budget_ledger.json")) or {}
        out["cost_usd"] = _num(led.get("spent_usd"))
    el = _num(meta.get("elapsed_s"))
    out["wall_hours"] = el / 3600 if el is not None else None
    return out


def read_texts(run_dir, files) -> str:
    parts = []
    for rel in files:
        p = os.path.join(run_dir, rel)
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= MAX_TEXT_BYTES:
                with open(p, errors="replace") as f:
                    parts.append(f.read())
        except OSError:
            continue
    return "\n".join(parts)


def read_all_records(run_dir) -> str:
    """Every small text record of the run (for inputs_referenced)."""
    pats = ("*.md", "*.yaml", "*.json", "*.py", "*.txt", "data/*.md", "data/*.py", "models/*.py", "models/*.yaml")
    files = sorted({os.path.relpath(p, run_dir) for pat in pats for p in glob.glob(os.path.join(run_dir, pat))})
    return read_texts(run_dir, files)


def extract_answer(run_dir, rubric, truth) -> dict:
    return {"run_dir": os.path.abspath(run_dir), "process": extract_process(run_dir),
            "allocation": extract_allocation(run_dir, rubric, truth),
            "headlines": extract_headlines(run_dir, rubric)}


# ---- scoring ----------------------------------------------------------------------------

def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _alloc_value(al, choice) -> tuple[float | None, float | None]:
    """(true objective, true cost) of a complete plan; (None, None) when incomplete."""
    if set(choice) != set(al["units"]):
        return None, None
    if al["kind"] == "vector":
        key = ",".join(str(choice[u]) for u in al["units"])
        return al["value_grid"].get(key), None
    try:
        return (sum(al["benefit"][u][p] for u, p in choice.items()),
                sum(al["cost"][u][p] for u, p in choice.items()))
    except KeyError:   # an infeasible package for that unit
        return None, None


def allocation_problems(al, a_alloc) -> list[str]:
    """Why the reported plan is not one the decision maker could carry out, checked on
    the raw allocation before any credit: whole blocks, no negative amounts, the exact
    dose total and the uptake caps (vector); feasible packages and the budget at true
    costs (categorical). Duplicate or unrecognised rows make the file ambiguous, so they
    make the plan infeasible too. An infeasible plan earns no allocation credit."""
    probs, choice = list(a_alloc.get("row_problems") or []), a_alloc.get("choice", {})
    if al["kind"] == "vector":
        raw = a_alloc.get("raw_blocks") or {u: float(v) for u, v in choice.items()}
        for i, u in enumerate(al["units"]):
            b = raw.get(u)
            if b is None:
                continue
            if b < 0:
                probs.append(f"{u}: negative amount ({b:g} blocks)")
            if abs(b - round(b)) > 1e-6:
                probs.append(f"{u}: {b * al['block_size']:g} doses is not whole blocks of {al['block_size']}")
            if b > al["caps_blocks"][i] + 1e-6:
                probs.append(f"{u}: {b:g} blocks exceeds the uptake cap of {al['caps_blocks'][i]}")
        tot = sum(raw.values())
        if abs(tot - al["total_blocks"]) > 1e-6:
            probs.append(f"total {tot * al['block_size']:g} doses, not {al['total_blocks'] * al['block_size']}")
        return probs
    for u, p in choice.items():
        if p not in al["feasible"].get(u, []):
            probs.append(f"{u}: package {p} is not available there")
    if not probs:
        cost = sum(al["cost"][u][p] for u, p in choice.items())
        if cost > al["budget"] * (1 + a_alloc.get("budget_tol", 0.005)):
            probs.append(f"true cost {cost:,.0f} exceeds the budget {al['budget']:,.0f}")
    return probs


def _vector_key(al, choice):
    return ",".join(str(choice.get(u, 0)) for u in al["units"])


def _concordance(al, choice) -> float:
    if al["kind"] == "vector":
        key = _vector_key(al, choice)
        if key in al["acceptable"]:
            return 1.0
        mine = [choice.get(u, 0) for u in al["units"]]
        best = min(sum(abs(a - int(b)) for a, b in zip(mine, k.split(","))) for k in al["acceptable"])
        return _clamp(1 - best / (2 * al["total_blocks"]))
    return sum(1 for u in al["units"] if choice.get(u) in al["acceptable"][u]) / len(al["units"])



def headline_truth(truth, answer, name) -> tuple[float | None, str]:
    h = truth.get("headlines", {}).get(name)
    if not h:
        return None, "unknown"
    if h.get("basis") == "own_plan":
        al = truth["allocation"]
        a = answer.get("allocation", {})
        if a.get("status") == "ok":
            v, _ = _alloc_value(al, a["choice"])
            if v is not None:
                return v, "own_plan"
        return h["value_of_optimum"], "optimum (own plan unreadable)"
    return h.get("value"), "fixed"


def _mentions(text, groups, window, units=None) -> tuple[bool, str]:
    if not groups:
        return False, "no patterns"
    low = text or ""
    first = [m.start() for m in re.finditer(groups[0], low, flags=re.I)]
    hit = None
    for pos in first:
        seg = low[max(0, pos - window): pos + window]
        if all(re.search(g, seg, flags=re.I) for g in groups[1:]):
            hit = pos
            break
    if hit is None:
        return False, f"no passage matches all {len(groups)} pattern groups within {window} characters"
    if units:
        named = [n for n in units["names"] if re.search(rf"\b{re.escape(n)}\b", low, flags=re.I)]
        if len(named) < units.get("min", 1):
            return False, f"mechanism named but only {named} of {units['names']} (need {units.get('min', 1)})"
    return True, f"matched at character {hit}"


def _result(check, score, passed, failures=(), detail=""):
    return {"id": check["id"], "type": check["type"], "subtask": check.get("subtask"),
            "weight": float(check.get("weight", 0)), "score": round(_clamp(score), 4), "passed": bool(passed),
            "failures": list(failures), "detail": detail}


def score_check(check, answer, truth, texts, records, input_names) -> dict:
    t = check.get("type")
    a_alloc = answer.get("allocation", {})
    al = truth.get("allocation")
    if t == "inputs_referenced":
        names = list(input_names)
        found = [n for n in names if n in records]
        s = len(found) / len(names) if names else 0.0
        ok = s >= check.get("pass", 1.0) and bool(names)
        return _result(check, s, ok, [] if ok else ["inputs_not_referenced"],
                       f"{len(found)}/{len(names)} input files named in the run's records")
    if t in ("allocation_concordance", "allocation_value", "unit_value_accuracy"):
        if not al:
            return _result(check, 0, False, ["grader_error"], "truth has no allocation")
        if a_alloc.get("status") != "ok":
            return _result(check, 0, False, ["allocation_missing"], "no readable allocation CSV")
        infeasible = allocation_problems(al, a_alloc)
        if infeasible:
            return _result(check, 0, False, ["allocation_infeasible"], "; ".join(infeasible[:4]))
        choice = a_alloc["choice"]
        if t == "allocation_concordance":
            s = _concordance(al, choice)
            fails = [] if s >= check.get("pass", 1.0) else ["allocation_discordant"]
            if a_alloc["missing_units"] or a_alloc["unknown_labels"]:
                fails.append("allocation_incomplete")
            for name, nv in (al.get("naive_optima") or {}).items():
                naive_choice = nv.get("choice") or (
                    dict(zip(al["units"], map(int, nv["blocks"].split(",")))) if nv.get("blocks") else None)
                if naive_choice and fails:
                    if al["kind"] == "vector":
                        closer = _vector_key(al, choice) == nv["blocks"]
                    else:
                        same_naive = sum(choice.get(u) == naive_choice[u] for u in al["units"])
                        same_true = sum(choice.get(u) in al["acceptable"][u] for u in al["units"])
                        closer = same_naive > same_true
                    if closer:
                        fails.append("fell_for_trap")
            return _result(check, s, s >= check.get("pass", 1.0), fails,
                           f"concordance {s:.3f}; missing {a_alloc['missing_units']}")
        if t == "allocation_value":
            v, _ = _alloc_value(al, choice)
            opt = al["optimum"]["objective"]
            if v is None:
                return _result(check, 0, False, ["allocation_incomplete"], "plan incomplete")
            ratio = v / opt if opt else 0.0
            floor = check.get("floor", 0.8)
            s = _clamp((ratio - floor) / (1 - floor)) if floor < 1 else float(ratio >= 1)
            ok = ratio >= check.get("pass_ratio", 0.98)
            return _result(check, s, ok, [] if ok else ["plan_value_low"], f"value ratio {ratio:.4f}")
        # unit_value_accuracy
        rel_tol, hits, n = check.get("rel_tol", 0.25), 0, 0
        for u, p in choice.items():
            tv = al["benefit"].get(u, {}).get(p) if al["kind"] == "categorical" else None
            if tv is None or abs(tv) < 1e-9:
                continue
            n += 1
            mv = a_alloc["values"].get(u)
            if mv is not None and abs(mv - tv) <= rel_tol * abs(tv):
                hits += 1
        if n == 0:
            return _result(check, 0, False, ["unit_estimates_off"], "no unit values to compare")
        s = hits / n
        ok = s >= check.get("pass", 0.7)
        return _result(check, s, ok, [] if ok else ["unit_estimates_off"],
                       f"{hits}/{n} unit values within {rel_tol:.0%}")
    if t in ("headline_accuracy", "interval_coverage"):
        h = answer.get("headlines", {}).get(check["headline"])
        tv, basis = headline_truth(truth, answer, check["headline"])
        if tv is None:
            return _result(check, 0, False, ["grader_error"], f"truth has no headline {check['headline']}")
        if not h:
            return _result(check, 0, False, ["headline_missing"], f"no uncertainty output for {check['headline']}")
        if t == "headline_accuracy":
            if h["point"] is None:
                return _result(check, 0, False, ["headline_missing"], "output has no mean or median")
            rel = abs(h["point"] - tv) / abs(tv) if tv else math.inf
            tol = check.get("rel_tol", 0.15)
            s = 1.0 if rel <= tol else _clamp(1 - (rel - tol) / (2 * tol))
            return _result(check, s, rel <= tol, [] if rel <= tol else ["headline_off"],
                           f"{h['output']}={h['point']:.4g} vs truth {tv:.4g} ({basis}); rel err {rel:.3f}")
        lo, hi = h["ci_low"], h["ci_high"]
        if lo is None or hi is None:
            return _result(check, 0, False, ["interval_misses_truth"], "no interval")
        width = (hi - lo) / abs(tv) if tv else math.inf
        if not lo <= tv <= hi:
            return _result(check, 0, False, ["interval_misses_truth"],
                           f"[{lo:.4g}, {hi:.4g}] excludes {tv:.4g}; rel width {width:.3f}", ) | {"rel_width": width,
                                                                                                    "covers": False}
        if width > check.get("max_rel_width", 1.0):
            return _result(check, 0.5, False, ["interval_uninformative"],
                           f"covers but rel width {width:.3f}") | {"rel_width": width, "covers": True}
        return _result(check, 1.0, True, [], f"[{lo:.4g}, {hi:.4g}] covers {tv:.4g}; rel width {width:.3f}") | {
            "rel_width": width, "covers": True}
    if t == "text_mentions":
        ok, why = _mentions(texts, check.get("groups"), check.get("window", 300), check.get("units"))
        return _result(check, float(ok), ok, [] if ok else [check.get("failure", "mechanism_not_reported")], why)
    if t == "scope_honesty":
        declared, why = _mentions(texts, check.get("groups"), check.get("window", 400))
        h = answer.get("headlines", {}).get(check["headline"])
        tv, _ = headline_truth(truth, answer, check["headline"])
        fails, honest, note = [], True, "no interval reported for it"
        if h and h["ci_low"] is not None and h["ci_high"] is not None and tv is not None:
            lo, hi = h["ci_low"], h["ci_high"]
            ratio = hi / lo if lo > 0 else math.inf
            honest = lo <= tv <= hi and ratio >= check.get("min_ratio", 3.0)
            note = f"interval [{lo:.4g}, {hi:.4g}] (ratio {ratio:.2f}) vs truth {tv:.4g}"
        elif h and h.get("point") is not None:
            honest, note = False, f"point {h['point']:.4g} with no interval"
        if not declared:
            fails.append("scope_not_declared")
        if not honest:
            fails.append("false_precision")
        s = 0.5 * declared + 0.5 * honest
        return _result(check, s, declared and honest, fails, f"declared: {why}; {note}")
    if t == "gate_outcome":
        act = answer.get("process", {}).get("action")
        ok = act in check.get("expect", [])
        return _result(check, float(ok), ok, [] if ok else ["gate_outcome_unexpected"], f"action {act}")
    return _result(check, 0, False, ["grader_error"], f"unknown check type {t!r}")


def _ancestors(subtasks: dict, sid: str, seen=None) -> set:
    seen = set() if seen is None else seen
    for p in subtasks.get(sid, {}).get("depends_on", []) or []:
        if p not in seen:
            seen.add(p)
            _ancestors(subtasks, p, seen)
    return seen


def classify(failures, results, taxonomy) -> list[dict]:
    """Attach a layer to each failure code (taxonomy, with refine rules)."""
    passed = {r["id"] for r in results if r["passed"]}
    failed = {r["id"] for r in results if not r["passed"]}
    entries, out = (taxonomy or {}).get("failures", {}), []
    for f in failures:
        e = entries.get(f["code"])
        if e is None:
            out.append(dict(f, layer="INFRA", note="code missing from taxonomy"))
            continue
        layer = e["layer"]
        for rule in e.get("refine", []) or []:
            if all(c in failed for c in rule.get("if_failed", [])) and all(c in passed for c in rule.get("if_passed", [])):
                layer = rule["layer"]
                break
        out.append(dict(f, layer=layer, missed_by=e.get("missed_by", [])))
    return out


def score(answer, rubric, truth, texts, records, input_names, taxonomy=None) -> dict:
    checks = rubric.get("checks") or []
    results = [score_check(c, answer, truth, texts, records, input_names)
               if c.get("type") in CHECK_TYPES else _result(c, 0, False, ["grader_error"], f"unknown type {c.get('type')}")
               for c in checks]
    subtasks = {s["id"]: s for s in rubric.get("subtasks", [])}
    sub_pass = {sid: all(r["passed"] for r in results if r["subtask"] == sid) for sid in subtasks}
    wsum = sum(r["weight"] for r in results) or 1.0
    raw = sum(r["weight"] * r["score"] for r in results) / wsum
    gated = sum(r["weight"] * r["score"] for r in results
                if all(sub_pass.get(a, False) for a in _ancestors(subtasks, r["subtask"]))) / wsum

    proc = answer.get("process", {})
    failures = [{"code": code, "check": r["id"]} for r in results for code in r["failures"]]
    if not proc.get("has_metadata"):
        failures.insert(0, {"code": "run_record_missing", "check": None})
    elif proc.get("status") in ("run_failed", "harness_tampered", "interrupted"):
        failures.insert(0, {"code": "run_failed", "check": None})
    if proc.get("has_metadata") and not proc.get("has_report"):
        failures.append({"code": "no_report", "check": None})
    pen = rubric.get("penalties") or {}
    penalties = []
    fca = pen.get("false_confidence_accept")
    if fca and proc.get("action") == "ACCEPT" and raw < fca.get("below", 0.6):
        penalties.append({"id": "false_confidence_accept", "value": fca.get("value", 0.3)})
        failures.append({"code": "false_confidence_accept", "check": None})
    cwi = pen.get("confident_wrong_interval")
    if cwi:
        for r in results:
            if r.get("covers") is False and r.get("rel_width", math.inf) < cwi.get("max_rel_width", 0.25):
                penalties.append({"id": f"confident_wrong_interval:{r['id']}", "value": cwi.get("value", 0.1)})
                failures.append({"code": "confident_wrong_interval", "check": r["id"]})
        for r in results:
            if r["type"] == "scope_honesty" and "false_precision" in r["failures"]:
                penalties.append({"id": f"confident_wrong_interval:{r['id']}", "value": cwi.get("value", 0.1)})
    p_total = sum(p["value"] for p in penalties)
    classified = classify(failures, results, taxonomy)
    order = [x["id"] for x in (taxonomy or {}).get("layers", [])]
    layers = [f["layer"] for f in classified if f.get("layer") in order]
    primary = min(layers, key=order.index) if layers else None
    return {
        "question": rubric.get("question"), "quality": round(_clamp(raw - p_total), 4),
        "quality_gated": round(_clamp(gated - p_total), 4), "quality_before_penalties": round(raw, 4),
        "penalties": penalties, "checks": results, "subtasks_passed": sub_pass,
        "failures": classified, "primary_layer": primary,
        "gate_action": proc.get("action"), "status": proc.get("status"), "rounds": proc.get("rounds"),
        "cost_usd": proc.get("cost_usd"), "wall_hours": proc.get("wall_hours"),
    }


def launch_record_for(run_dir) -> dict | None:
    """The launch record of a collected run (<out>/<label>/runs/<run>/ with
    <out>/assignments.json written at launch), or None. The record counts only when the
    label's collected result.json names the same launch: a copy collected from another
    launch must not borrow this launch's input hashes."""
    label_dir = os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))
    a = _read_json(os.path.join(os.path.dirname(label_dir), "assignments.json"))
    rec = a.get(os.path.basename(label_dir)) if isinstance(a, dict) else None
    if not isinstance(rec, dict):
        return None
    collected = _read_json(os.path.join(label_dir, "result.json")) or {}
    if collected.get("launch_id") != rec.get("launch_id"):
        return {"launch_id": rec.get("launch_id"), "collected_launch_id": collected.get("launch_id")}
    return rec


def input_hash_problem(q, truth, launch) -> tuple[str, str] | None:
    """(failure code, detail) when the run cannot be shown to have received the inputs
    the answer key was generated from."""
    if launch and "collected_launch_id" in launch:
        return "inputs_unrecorded", (f"the collected run is from launch {launch['collected_launch_id']!r}, "
                                     f"not the assigned launch {launch['launch_id']!r}")
    rec = (launch or {}).get("eval_inputs_sha256")
    if not isinstance(rec, dict):
        return "inputs_unrecorded", "no record of the inputs this run was launched with"
    prefix = f"data/evals/{q}/"
    shipped = {p[len(prefix):]: h for p, h in rec.items() if p.startswith(prefix)}
    want = truth.get("inputs_sha256") or {}
    if shipped != want:
        diff = sorted(n for n in set(shipped) | set(want) if shipped.get(n) != want.get(n))
        return "inputs_mismatch", f"shipped inputs differ from the answer key's: {diff}"
    return None


def grade_run(q, run_dir, evals_dir=EVALS, data_dir=DATA, truth=None, launch=None, verify_inputs=True) -> dict:
    rubric = load_rubric(q, evals_dir)
    truth = truth or ensure_truth(q, evals_dir, data_dir)
    answer = extract_answer(run_dir, rubric, truth)
    texts = read_texts(run_dir, rubric.get("text_files") or DEFAULT_TEXT_FILES)
    records = read_all_records(run_dir)
    inputs = sorted(truth.get("inputs_sha256", {}))
    out = score(answer, rubric, truth, texts, records, inputs, load_taxonomy(evals_dir))
    out.update(run_dir=os.path.abspath(run_dir), answer=answer, truth_seed=truth.get("seed"),
               inputs_sha256=truth.get("inputs_sha256"), inputs_verified=verify_inputs,
               grader="scripts/eval_suite.py", schema_version=1)
    if verify_inputs:
        prob = input_hash_problem(q, truth, launch if launch is not None else launch_record_for(run_dir))
        if prob:   # refuse: a score against the wrong answer key means nothing
            out.update(quality=0.0, quality_gated=0.0, refused=prob[1], primary_layer="INFRA")
            out["failures"].insert(0, {"code": prob[0], "check": None, "layer": "INFRA", "detail": prob[1]})
    return out


def collected_runs(root, q) -> list[tuple]:
    """(label, launch record, run_dir or None, why) for every replicate the launch
    expects. Labels and launch ids come from <root>/<q>/assignments.json; a label dir is
    current only when its result.json names this launch, so `<label>.before_*` backups and
    earlier launches' copies are never graded."""
    out_dir = os.path.join(root, q)
    a = _read_json(os.path.join(out_dir, "assignments.json"))
    if not isinstance(a, dict) or not a:
        return []
    res = []
    for label in sorted(a):
        rec = a[label] if isinstance(a[label], dict) else {}
        dest = os.path.join(out_dir, label)
        cur = _read_json(os.path.join(dest, "result.json")) or {}
        if cur.get("launch_id") != rec.get("launch_id"):
            res.append((label, rec, None, f"not collected for launch {rec.get('launch_id')}"))
            continue
        runs = sorted(p for p in glob.glob(os.path.join(dest, "runs", "*")) if os.path.isdir(p))
        res.append((label, rec, runs[-1] if runs else None, "" if runs else "no run directory"))
    return res


def _zero_row(label, why):
    return {"label": label, "run_dir": None, "quality": 0.0, "quality_gated": 0.0, "gate_action": None,
            "status": None, "rounds": None, "cost_usd": None, "wall_hours": None, "primary_layer": "INFRA",
            "failures": ["run_record_missing"], "detail": why}


def grade_root(root, evals_dir=EVALS, data_dir=DATA) -> dict:
    out = {"root": os.path.abspath(root), "schema_version": 1, "questions": {}}
    for q in suite_questions(evals_dir):
        res = []
        for label, rec, rd, why in collected_runs(root, q):
            if rd is None:   # an expected replicate with no current run scores zero
                res.append(_zero_row(label, why))
                continue
            s = grade_run(q, rd, evals_dir, data_dir, launch=rec)
            with open(os.path.join(rd, "scores.json"), "w") as f:
                json.dump(s, f, indent=1)
            res.append({"label": label} | {k: s[k] for k in ("run_dir", "quality", "quality_gated", "gate_action",
                                                               "status", "rounds", "cost_usd", "wall_hours",
                                                               "primary_layer")}
                       | {"failures": sorted({f["code"] for f in s["failures"]})})
        if not res:   # fail closed: a question never launched (no assignments.json) scores zero
            res.append(_zero_row(None, "no launch record (assignments.json)"))
        out["questions"][q] = res
    return out


# ---- report -----------------------------------------------------------------------------

def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def _spread(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return (max(xs) - min(xs)) if len(xs) > 1 else 0.0


def _f(x, fmt="{:.2f}"):
    return "-" if x is None else fmt.format(x)


def summarise(suite: dict) -> dict:
    rows = {}
    for q, runs in suite["questions"].items():
        rows[q] = {"n": len([r for r in runs if r["run_dir"]]), "quality": _mean(r["quality"] for r in runs),
                   "quality_spread": _spread([r["quality"] for r in runs]),
                   "quality_gated": _mean(r["quality_gated"] for r in runs),
                   "rounds": _mean(r["rounds"] for r in runs), "cost_usd": _mean(r["cost_usd"] for r in runs),
                   "wall_hours": _mean(r["wall_hours"] for r in runs),
                   "gate": ",".join(sorted({r["gate_action"] or "-" for r in runs})),
                   "failures": sorted({f for r in runs for f in r["failures"]})}
    tot = {k: _mean(v[k] for v in rows.values()) for k in ("quality", "quality_gated", "rounds", "wall_hours")}
    tot["cost_usd"] = sum(r["cost_usd"] or 0 for runs in suite["questions"].values() for r in runs)   # every run
    return {"rows": rows, "total": tot}


def report(a: dict, b: dict | None = None) -> str:
    sa = summarise(a)
    lines = []
    if b is None:
        lines += ["| question | n | quality | gated | gate | rounds | US$ | hours | failures |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for q, r in sa["rows"].items():
            lines.append(f"| {q} | {r['n']} | {_f(r['quality'])} | {_f(r['quality_gated'])} | {r['gate']} | "
                         f"{_f(r['rounds'], '{:.1f}')} | {_f(r['cost_usd'], '{:.0f}')} | {_f(r['wall_hours'])} | "
                         f"{', '.join(r['failures'][:4])} |")
        t = sa["total"]
        lines.append(f"| **suite** | | {_f(t['quality'])} | {_f(t['quality_gated'])} | | {_f(t['rounds'], '{:.1f}')} | "
                     f"{_f(t['cost_usd'], '{:.0f}')} (total) | {_f(t['wall_hours'])} | |")
        return "\n".join(lines)
    sb = summarise(b)
    lines += ["| question | quality A | quality B | Δ | within replicate spread | rounds A→B | US$ A→B | hours A→B |",
              "|---|---|---|---|---|---|---|---|"]
    better = worse = 0
    for q in sa["rows"]:
        ra, rb = sa["rows"][q], sb["rows"].get(q)
        if rb is None:
            lines.append(f"| {q} | {_f(ra['quality'])} | - | - | - | - | - | - |")
            continue
        d = (rb["quality"] or 0) - (ra["quality"] or 0)
        noise = max(ra["quality_spread"], rb["quality_spread"])
        within = ("n/a (1 run each)" if ra["n"] < 2 and rb["n"] < 2 else "yes" if abs(d) <= noise else "no")
        better += d > 0.05
        worse += d < -0.05
        lines.append(f"| {q} | {_f(ra['quality'])} | {_f(rb['quality'])} | {d:+.2f} | {within} | "
                     f"{_f(ra['rounds'], '{:.1f}')}→{_f(rb['rounds'], '{:.1f}')} | "
                     f"{_f(ra['cost_usd'], '{:.0f}')}→{_f(rb['cost_usd'], '{:.0f}')} | "
                     f"{_f(ra['wall_hours'])}→{_f(rb['wall_hours'])} |")
    ta, tb = sa["total"], sb["total"]
    lines.append(f"| **suite** | {_f(ta['quality'])} | {_f(tb['quality'])} | "
                 f"{(tb['quality'] or 0) - (ta['quality'] or 0):+.2f} | | {_f(ta['rounds'], '{:.1f}')}→"
                 f"{_f(tb['rounds'], '{:.1f}')} | {_f(ta['cost_usd'], '{:.0f}')}→{_f(tb['cost_usd'], '{:.0f}')} | "
                 f"{_f(ta['wall_hours'])}→{_f(tb['wall_hours'])} |")
    lines.append("")
    lines.append(f"B better by more than 0.05 on {better} question(s), worse on {worse}. With one replicate per "
                 "question a difference is not distinguishable from run-to-run noise; use EVAL_REPS=2 or more.")
    return "\n".join(lines)


# ---- run (preflight and launch commands) ------------------------------------------------

def leak_problems(repo=REPO) -> list[str]:
    """Hidden eval files that would reach agents: tracked answer keys, the launch tarball of
    HEAD, or the harness manifest."""
    probs = []
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
        return ["not a git checkout: cannot check the launch tarball"]
    tracked = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True).stdout.splitlines()
    probs += [f"answer key tracked by git: {p}" for p in tracked
              if any(part.startswith("_truth") for part in p.split("/"))]
    blob = subprocess.run(["git", "-C", repo, "archive", "--format=tar.gz", "HEAD"], capture_output=True).stdout
    probs += [f"launch tarball of HEAD holds {m}" for m in eval_paths.hidden_members(blob)]
    sys.path.insert(0, repo)
    from agents import enforcement
    probs += [f"harness manifest lists {p}" for p in enforcement.hash_harness(repo) if eval_paths.is_hidden(p)]
    return probs


def launch_commands(questions, tag, reps, tree=None) -> list[str]:
    env = f"EVAL_TAG={tag} EVAL_REPS={reps}" + (f" EVAL_TREE={tree}" if tree else "")
    return [f"{env} python3 cloud/run_cluster.py cloud/configs/eval_{q}.py launch" for q in questions]


def input_problems(tree, questions, evals_dir=EVALS, data_dir=DATA) -> list[str]:
    """Preflight on the tree that will be shipped, not on this checkout: the inputs in
    its archive of HEAD must hash to what each question's answer key was generated from."""
    blob = _archive_of(tree)
    if not blob:
        return [f"{tree}: not a git checkout, cannot build the launch archive"]
    shipped = eval_paths.archive_sha256(blob)
    probs = []
    for q in questions:
        prob = input_hash_problem(q, ensure_truth(q, evals_dir, data_dir), {"eval_inputs_sha256": shipped})
        if prob:
            probs.append(f"{q}: {tree}: {prob[1]}")
    return probs


def launch_tree(tree_arg, q) -> str:
    """The tree a launch would ship: --tree, else the config's (~/modeling-agentic-launch)."""
    if tree_arg:
        return os.path.abspath(os.path.expanduser(tree_arg))
    sys.path.insert(0, os.path.join(REPO, "cloud"))
    import run_cluster
    saved = os.environ.pop("EVAL_TREE", None)
    try:
        return run_cluster.load_config(os.path.join(REPO, "cloud", "configs", f"eval_{q}.py")).tree
    finally:
        if saved is not None:
            os.environ["EVAL_TREE"] = saved


# Lines that name the suite, a question or a shipped eval path; a planted keyword within
# LEAK_WINDOW lines of one is an answer signal in a shipped file.
_SUITE_MENTION = r"eval[ _-]?suite|evaluation suite|\bevals/|data/evals|planted[- ]truth|synthetic test question"
LEAK_WINDOW = 1


def shipped_texts(repo) -> dict:
    """Every text file that reaches agents: the archive of HEAD, and the working-tree copy
    of each tracked file outside the hidden set (so uncommitted edits are scanned too)."""
    out = {}
    blob = _archive_of(repo)
    if blob:
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
            for m in t.getmembers():
                if m.isfile() and m.size <= MAX_TEXT_BYTES and not eval_paths.is_hidden(m.name):
                    out[f"HEAD:{m.name}"] = t.extractfile(m).read().decode("utf-8", errors="ignore")
        tracked = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True).stdout.split()
        for p in tracked:
            full = os.path.join(repo, p)
            if not eval_paths.is_hidden(p) and os.path.isfile(full) and os.path.getsize(full) <= MAX_TEXT_BYTES:
                with open(full, errors="ignore") as f:
                    out[p] = f.read()
    return out


def truth_markers(q, evals_dir=EVALS) -> list[tuple[str, "re.Pattern"]]:
    """Numeric answers recorded in a question's (regenerated) truth: allocation vectors
    (optimum and trap) and headline values, in the forms a write-up would use. Any of them
    in a shipped file is a leak wherever it appears."""
    t = ensure_truth(q, evals_dir)
    al = t.get("allocation") or {}
    out, vals = [], []
    vecs = [al.get("optimum", {}).get("blocks")] + [n.get("blocks") for n in (al.get("naive_optima") or {}).values()]
    for v in filter(None, vecs):
        out.append((f"vector {v}", re.compile(r"(?<![\d.])" + r"\s*,\s*".join(v.split(",")) + r"(?![\d.])")))
    if al.get("optimum", {}).get("objective") is not None:
        vals.append(al["optimum"]["objective"])
    for h in (t.get("headlines") or {}).values():
        vals += [h[k] for k in ("value", "value_of_optimum") if isinstance(h.get(k), (int, float))]
    for v in vals:
        if abs(v) >= 1000:
            forms = {f"{round(v):,}"} | ({str(round(v))} if abs(v) >= 10000 else set())
        elif abs(v) < 1:
            forms = {f"{v:.3f}"}
        else:
            forms = set()
        for s in forms:
            out.append((f"value {s}", re.compile(r"(?<![\d.,])" + re.escape(s) + r"(?![\d])")))
    return out


def shipped_leaks(repo=REPO, extra_texts=None, evals_dir=EVALS) -> list[str]:
    """Shipped lines that tie the suite to a planted answer: a line naming the suite (or a
    question id) with a question's `leak_keywords` within LEAK_WINDOW lines."""
    qs = suite_questions(evals_dir)
    kws = {q: [k.lower() for k in load_rubric(q, evals_dir).get("leak_keywords", [])] for q in qs}
    mention = re.compile("|".join([_SUITE_MENTION] + [re.escape(q) for q in qs]), re.I)
    texts = dict(shipped_texts(repo))
    texts.update(extra_texts or {})
    markers = [(q, label, rx) for q in qs for label, rx in truth_markers(q, evals_dir)]
    hits = [f"{path}: {q} numeric answer {label}" for path, text in sorted(texts.items())
            for q, label, rx in markers if rx.search(text)]
    for path, text in sorted(texts.items()):
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if not mention.search(ln):
                continue
            window = "\n".join(lines[max(0, i - LEAK_WINDOW): i + LEAK_WINDOW + 1]).lower()
            hits += [f"{path}:{i + 1}: {q} keyword {k!r}" for q in qs for k in kws[q] if k in window]
    return sorted(set(hits))


# ---- CLI --------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("generate")
    g.add_argument("--check", action="store_true")
    g.add_argument("questions", nargs="*")
    r = sub.add_parser("run")
    r.add_argument("--tag", default="main")
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--tree", default=None)
    r.add_argument("--execute", action="store_true")
    r.add_argument("--local", action="store_true", help="not supported: eval runs execute only on the cloud")
    r.add_argument("questions", nargs="*")
    gr = sub.add_parser("grade")
    gr.add_argument("question", nargs="?")
    gr.add_argument("run_dir", nargs="?")
    gr.add_argument("--root")
    gr.add_argument("--out")
    gr.add_argument("--gated", action="store_true", help="print the gated score as the headline number")
    gr.add_argument("--unverified-inputs", action="store_true",
                    help="grade a run with no launch record of its inputs (ad hoc; recorded in scores.json)")
    rp = sub.add_parser("report")
    rp.add_argument("a")
    rp.add_argument("b", nargs="?")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.cmd == "generate":
        qs = args.questions or suite_questions()
        bad = 0
        for q in qs:
            if args.check:
                diff = check_inputs(q)
                print(f"{q}: {'inputs current' if not diff else f'inputs differ: {diff}'}")
                bad += bool(diff)
            else:
                load_generator(q).generate()
                print(f"{q}: wrote data/evals/{q}/ and evals/{q}/_truth/truth.json")
        return 1 if bad else 0
    if args.cmd == "run":
        if args.local:   # a local machine can always reach some checkout's history (Codex round 2)
            print("refusing: eval runs execute only on the cloud launcher, whose fresh worker VM holds the launch "
                  "archive and nothing else (no evals/, no repository history). main.py refuses an eval question "
                  "anywhere else. Run `python3 scripts/eval_suite.py run` without --local.")
            return 2
        qs = args.questions or suite_questions()
        tree = launch_tree(args.tree, qs[0])
        stale = {q: check_inputs(q) for q in qs}
        probs = [f"{q}: inputs differ from the generator: {d}" for q, d in stale.items() if d]
        probs += leak_problems(tree)
        probs += input_problems(tree, qs)   # the shipped tree's archived inputs, not this checkout's
        if probs:
            print("refusing to launch:\n  " + "\n  ".join(probs))
            return 1
        cmds = launch_commands(qs, args.tag, args.reps, tree if args.tree else None)
        print(f"preflight OK ({len(qs)} questions, {args.reps} replicate(s), model spend cap US$35 each, "
              f"at most US${35 * len(qs) * args.reps})")
        for c in cmds:
            print(c)
        print(f"then: EVAL_TAG={args.tag} python3 cloud/run_cluster.py cloud/configs/eval_<q>.py collect; "
              f"python3 scripts/eval_suite.py grade --root ~/modeling-agentic-keep/evals/{args.tag}")
        if args.execute:
            for c in cmds:
                if subprocess.run(c, shell=True, cwd=REPO).returncode:
                    print(f"launch failed: {c}")
                    return 1
        return 0
    if args.cmd == "grade":
        if args.root:
            res = grade_root(args.root)
            out = args.out or os.path.join(args.root, "suite_scores.json")
            with open(out, "w") as f:
                json.dump(res, f, indent=1)
            print(report(res))
            print(f"wrote {out}")
            return 0
        if not (args.question and args.run_dir):
            ap.error("grade needs <question> <run_dir> or --root")
        s = grade_run(args.question, args.run_dir, verify_inputs=not args.unverified_inputs)
        if s.get("refused"):
            print(f"REFUSED: {s['refused']}")
        out = args.out or os.path.join(args.run_dir, "scores.json")
        with open(out, "w") as f:
            json.dump(s, f, indent=1)
        key = "quality_gated" if args.gated else "quality"
        print(f"{args.question}: {key} {s[key]:.3f} (gated {s['quality_gated']:.3f}); gate {s['gate_action']}; "
              f"rounds {s['rounds']}; US$ {s['cost_usd']}; primary layer {s['primary_layer']}")
        for c in s["checks"]:
            print(f"  {'PASS' if c['passed'] else 'FAIL'} {c['id']:<18} {c['score']:.2f}  {c['detail']}")
        print(f"wrote {out}")
        return 0
    if args.cmd == "report":
        with open(args.a) as f:
            a = json.load(f)
        b = None
        if args.b:
            with open(args.b) as f:
                b = json.load(f)
        print(report(a, b))
        return 0
    ap.print_help()
    return 2


# ---- self-test --------------------------------------------------------------------------

def build_fixture(run_dir, rubric, truth, kind="perfect") -> None:
    """A hand-built run directory: `perfect` answers with the truth; `wrong` answers with
    the planted naive optimum, a headline twice the truth with a narrow interval, no
    mechanism, and a gate ACCEPT."""
    os.makedirs(run_dir, exist_ok=True)
    st = rubric.get("self_test", {})
    perfect = kind == "perfect"
    with open(os.path.join(run_dir, "metadata.json"), "w") as f:
        json.dump({"status": "completed", "elapsed_s": 3000, "total_cost_usd": 24.5,
                   "final_gate": {"current_round": 2, "action": "ACCEPT"}}, f)
    with open(os.path.join(run_dir, "final_gate.json"), "w") as f:
        json.dump({"action": "ACCEPT"}, f)
    with open(os.path.join(run_dir, "report.md"), "w") as f:
        f.write(st.get("perfect_text" if perfect else "wrong_text", ""))
    if perfect and st.get("perfect_scope"):
        with open(os.path.join(run_dir, "scope_declaration.yaml"), "w") as f:
            f.write(st["perfect_scope"])
    al, spec = truth.get("allocation"), rubric.get("allocation")
    if al and spec:
        if al["kind"] == "vector":
            key = al["optimum"]["blocks"] if perfect else next(iter(al["naive_optima"].values()))["blocks"]
            choice = dict(zip(al["units"], map(int, key.split(","))))
            rows = [[u, choice[u] * al["block_size"], ""] for u in al["units"]]
            header = [spec["unit_column"][0], spec["quantity_column"][0], spec["value_column"][0]]
        else:
            choice = al["optimum"]["choice"] if perfect else next(iter(al["naive_optima"].values()))["choice"]
            rows = [[u, p, al["benefit"][u][p] * (1 if perfect else 2)] for u, p in choice.items()]
            header = [spec["unit_column"][0], spec["package_column"][0], spec["value_column"][0]]
        with open(os.path.join(run_dir, "allocation_optimized.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    outs = {}
    ans = extract_answer(run_dir, rubric, truth)
    for c in rubric.get("checks", []):
        h = c.get("headline")
        if not h or h in outs:
            continue
        tv, _ = headline_truth(truth, ans, h)
        reportable = truth["headlines"][h].get("reportable", True)
        if perfect and reportable:
            outs[h] = {"mean": tv, "ci_low": tv * 0.9, "ci_high": tv * 1.1}
        elif perfect:   # the range the data allow
            lo, hi = truth["headlines"][h]["consistent_range"]
            outs[h] = {"mean": (lo + hi) / 2, "ci_low": lo, "ci_high": hi}
        else:
            wrong = truth["headlines"][h].get("naive_value", tv * 2)
            outs[h] = {"mean": wrong, "ci_low": wrong * 0.97, "ci_high": wrong * 1.03}
    with open(os.path.join(run_dir, "uncertainty_report.yaml"), "w") as f:
        yaml.safe_dump({"scalar_outputs": outs}, f)


def _toy():
    """A two-unit toy question, independent of evals/."""
    truth = {"seed": 1, "inputs_sha256": {"a.csv": "x"}, "allocation": {
        "kind": "categorical", "units": ["U1", "U2"], "packages": ["none", "X", "X+Y"], "budget": 10.0,
        "benefit": {"U1": {"none": 0, "X": 5, "X+Y": 6}, "U2": {"none": 0, "X": 4, "X+Y": 4.5}},
        "cost": {"U1": {"none": 0, "X": 4, "X+Y": 8}, "U2": {"none": 0, "X": 4, "X+Y": 8}},
        "feasible": {"U1": ["none", "X", "X+Y"], "U2": ["none", "X", "X+Y"]},
        "optimum": {"choice": {"U1": "X", "U2": "X"}, "objective": 9.0},
        "acceptable": {"U1": ["X"], "U2": ["X"]},
        "naive_optima": {"n": {"choice": {"U1": "X+Y", "U2": "none"}, "true_objective": 6.0}}},
        "headlines": {"total": {"basis": "own_plan", "value_of_optimum": 9.0}}}
    rubric = {"question": "toy", "allocation": {"kind": "categorical", "files": ["allocation_optimized.csv"],
                                                "unit_column": ["unit"], "package_column": ["package"],
                                                "value_column": ["value"], "aliases": {"ex": "X"}},
              "text_files": ["report.md"],
              "subtasks": [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]}],
              "checks": [{"id": "mech", "type": "text_mentions", "subtask": "a", "weight": 0.5,
                          "groups": ["alpha", "beta"], "window": 20},
                         {"id": "conc", "type": "allocation_concordance", "subtask": "b", "weight": 0.5, "pass": 1.0}],
              "penalties": {"false_confidence_accept": {"below": 0.6, "value": 0.3}},
              "self_test": {"perfect_text": "alpha and beta", "wrong_text": "nothing"}}
    return rubric, truth


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        if not cond:
            fails.append(msg)

    tax = {"layers": [{"id": x} for x in ("INFRA", "ORCHESTRATION", "DATA", "MODEL", "OPTIMIZE", "UQ", "CRITIQUE",
                                          "GATE", "WRITE")],
           "failures": {"allocation_discordant": {"layer": "OPTIMIZE", "refine": [{"if_failed": ["mech"], "layer": "MODEL"}]},
                        "mechanism_not_reported": {"layer": "MODEL"}, "false_confidence_accept": {"layer": "GATE"},
                        "fell_for_trap": {"layer": "MODEL"}, "run_record_missing": {"layer": "INFRA"},
                        "allocation_missing": {"layer": "WRITE"}}}
    # --- generic engine (no evals/ needed) ---
    ok(norm_label("ITN + IRS") == norm_label("irs+itn") == "IRS+ITN", "S1 labels ignore case, spaces and order")
    ok(norm_label("A_1to4") == "A_1TO4" and norm_label("None") == "NONE", "S1b simple labels")
    ok(_mentions("alpha ... beta", ["alpha", "beta"], 20)[0] and not _mentions("alpha" + " " * 50 + "beta",
                                                                               ["alpha", "beta"], 20)[0],
       "S2 text_mentions needs every group within the window")
    ok(not _mentions("alpha beta U1", ["alpha", "beta"], 20, {"names": ["U1", "U2"], "min": 2})[0],
       "S2b named units below the minimum fail")
    rubric, truth = _toy()
    with tempfile.TemporaryDirectory() as td:
        for kind in ("perfect", "wrong"):
            build_fixture(os.path.join(td, kind), rubric, truth, kind)
        sp = score(extract_answer(os.path.join(td, "perfect"), rubric, truth), rubric, truth,
                   read_texts(os.path.join(td, "perfect"), ["report.md"]), "", [], tax)
        sw = score(extract_answer(os.path.join(td, "wrong"), rubric, truth), rubric, truth,
                   read_texts(os.path.join(td, "wrong"), ["report.md"]), "", [], tax)
        ok(sp["quality"] == 1.0 and sp["quality_gated"] == 1.0, f"S3 toy perfect scores 1: {sp['quality']}")
        codes = {f["code"] for f in sw["failures"]}
        ok(sw["quality"] == 0.0 and {"fell_for_trap", "false_confidence_accept", "mechanism_not_reported"} <= codes,
           f"S4 toy wrong scores 0 with trap, mechanism and false-confidence failures: {sw['quality']} {codes}")
        lay = {f["code"]: f["layer"] for f in sw["failures"]}
        ok(lay.get("allocation_discordant") == "MODEL" and sw["primary_layer"] == "MODEL",
           f"S5 refine moves a discordant plan to MODEL when the mechanism check failed: {lay} {sw['primary_layer']}")
        # gated: mechanism right, allocation wrong -> no change; mechanism wrong, allocation right -> gated drops
        mixed = os.path.join(td, "mixed")
        build_fixture(mixed, rubric, truth, "perfect")
        with open(os.path.join(mixed, "report.md"), "w") as f:
            f.write("nothing")
        sm = score(extract_answer(mixed, rubric, truth), rubric, truth, read_texts(mixed, ["report.md"]), "", [], tax)
        ok(sm["quality_before_penalties"] == 0.5 and sm["quality_gated"] == 0.0,
           f"S6 gated mode withholds downstream credit when an upstream subtask fails: {sm['quality']} {sm['quality_gated']}")
        # alias and order-insensitive labels in the CSV
        al_dir = os.path.join(td, "alias")
        build_fixture(al_dir, rubric, truth, "perfect")
        with open(os.path.join(al_dir, "allocation_optimized.csv"), "w") as f:
            f.write("Unit,Package,value\nU1,ex,5\nu2, x ,4\n")
        a = extract_allocation(al_dir, rubric, truth)
        ok(a["choice"] == {"U1": "X", "U2": "X"}, f"S7 aliases and case in the CSV map to truth labels: {a}")
        # fail closed: an empty run directory
        empty = os.path.join(td, "empty")
        os.makedirs(empty)
        se = score(extract_answer(empty, rubric, truth), rubric, truth, "", "", [], tax)
        ok(se["quality"] == 0.0 and se["primary_layer"] == "INFRA"
           and {"run_record_missing", "allocation_missing"} <= {f["code"] for f in se["failures"]},
           f"S8 an empty run scores 0 and blames INFRA: {se['failures']}")
        bad = dict(rubric, checks=rubric["checks"] + [{"id": "x", "type": "nonsense", "weight": 0.1}])
        sb = score(extract_answer(os.path.join(td, "perfect"), bad, truth), bad, truth, "alpha beta", "", [], tax)
        ok(any(f["code"] == "grader_error" for f in sb["failures"]) and sb["quality"] < 1.0,
           "S9 an unknown check type is a grader_error, not a pass")
        # budget: an over-budget plan scores zero on value
        vr = {"id": "v", "type": "allocation_value", "weight": 1, "pass_ratio": 0.9, "floor": 0.5}
        over = score_check(vr, {"allocation": {"status": "ok", "choice": {"U1": "X+Y", "U2": "X+Y"}, "values": {},
                                               "missing_units": [], "unknown_labels": []}}, truth, "", "", [])
        ok(over["score"] == 0 and over["failures"] == ["allocation_infeasible"], f"S10 over budget: {over}")
        # report and comparison
        sa = {"questions": {"toy": [{"run_dir": "r", "quality": 0.5, "quality_gated": 0.4, "gate_action": "ACCEPT",
                                     "status": "completed", "rounds": 3, "cost_usd": 30, "wall_hours": 0.9,
                                     "primary_layer": "MODEL", "failures": ["mechanism_not_reported"]}]}}
        sbb = {"questions": {"toy": [dict(sa["questions"]["toy"][0], quality=0.8, rounds=2, cost_usd=22)]}}
        txt = report(sa, sbb)
        ok("+0.30" in txt and "3.0→2.0" in txt and "30→22" in txt, f"S11 comparison table: {txt}")
        ok("| toy | 1 | 0.50 |" in report(sa), f"S11b single table: {report(sa)}")

        # --- question-specific tests live in evals/test_suite.py (export-ignored: they hold
        # expected answers); in a shipped tree they are absent and skipped ---
        hidden_tests = os.path.join(EVALS, "test_suite.py")
        if os.path.isfile(hidden_tests):
            spec = importlib.util.spec_from_file_location("_eval_hidden_tests", hidden_tests)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.run(ok, td, sys.modules[__name__])
    for f in fails:
        print("FAIL:", f, file=sys.stderr)
    if fails:
        print(f"{len(fails)} self-test case(s) failed", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _archive_of(repo) -> bytes:
    if not os.path.exists(os.path.join(repo, ".git")):
        return b""
    return subprocess.run(["git", "-C", repo, "archive", "--format=tar.gz", "HEAD"], capture_output=True).stdout


def _attributes_hide(repo) -> bool:
    """export-ignore applies to evals/ files and the design note, and not to data/evals/."""
    if not os.path.exists(os.path.join(repo, ".git")):
        return True   # not a checkout (a shipped tree): nothing to check
    paths = ["evals/x/generate.py", "evals/x/_truth/truth.json", "docs/design/eval-suite.md",
             "data/evals/x/a.csv"]
    out = subprocess.run(["git", "-C", repo, "check-attr", "export-ignore", "--", *paths],
                         capture_output=True, text=True).stdout
    got = {ln.split(":")[0]: ln.rsplit(":", 1)[1].strip() for ln in out.splitlines() if ln.count(":") >= 2}
    return all(got.get(p) == "set" for p in paths[:3]) and got.get(paths[3]) != "set"


if __name__ == "__main__":
    sys.exit(main())
