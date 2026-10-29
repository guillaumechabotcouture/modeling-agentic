#!/usr/bin/env python3
"""Phase 44 α (E2): what-if queries on a finished run, without agents.

    python3 scripts/ask.py <run_dir> --set NAME=VALUE [--set ...] [--budget X] [--objective NAME]
                           [--draws N] [--seed S] [--paired] [--outputs a,b] [--json] [--no-log]
    python3 scripts/ask.py --self-test

Loads the run's own `models/outcome_fn.py::outcome_fn` in a fresh process
(scripts/model_card.py's worker: the run's models/ on the path, no cached
bytecode, no credentials in the environment), and answers "what would the
model say if these inputs were different":

1. Every requested setting must be an interface input on the run's model card
   (a registry parameter, or one declared in `models/model_domain.yaml`) and
   inside the card's valid domain (the range the run declares it simulated or
   fitted over, else the registry prior's 95% interval). `--budget` and
   `--objective` work only when the run maps them to an outcome_fn params key
   (`models/model_domain.yaml: whatif_interface.arguments`). Anything else is
   refused with the reason; there is no override.
2. The card must be usable (scripts/model_card.py): the interface loads and
   is the code behind uncertainty_report.yaml.
3. The model runs at the registry point estimates (the run's headline) and
   with the settings applied, then over the registry draws: by default the
   run's own draws, regenerated from uncertainty_report.yaml's seed and
   n_draws with propagate_uncertainty's sampler and checked against the
   report's parameter summary; `--draws N` takes N fresh draws (fixed seed).
   A set input is fixed at its value in every draw. `--paired` also runs the
   unchanged draws, giving an interval for the change itself.
4. The answer (point, interval, change against the run's point estimate and
   against the report's claims when models/headline_binding.yaml binds them)
   is printed, and the query and its result (or the refusal) are appended to
   `<run_dir>/whatif_log.jsonl`.

Deterministic: no model API calls, no cloud, fixed seeds.
Exit: 0 answered · 3 refused · 1 the model failed · 2 usage error.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = "whatif_log.jsonl"
DEFAULT_SEED = 42
DEFAULT_FRESH_DRAWS = 200
MAX_FAIL_SHARE = 0.2     # propagate_uncertainty's abort threshold


def _sibling(name: str):
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    return __import__(name)


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def parse_settings(items: list[str]) -> tuple[dict, list[str]]:
    out, errs = {}, []
    for it in items or []:
        name, sep, val = str(it).partition("=")
        name = name.strip()
        if not sep or not name:
            errs.append(f"--set {it!r}: expected NAME=VALUE")
            continue
        try:
            v = float(val)
        except ValueError:
            errs.append(f"--set {name}: {val!r} is not a number")
            continue
        if not math.isfinite(v):
            errs.append(f"--set {name}: {val!r} is not finite")
        elif name in out:
            errs.append(f"--set {name} is given more than once")
        else:
            out[name] = v
    return out, errs


def check_request(card: dict, settings: dict, budget=None, objective=None) -> tuple[dict, list[str], list[dict]]:
    """(overrides for outcome_fn's params, refusal reasons, the checked settings)."""
    reasons, checked, overrides = [], [], {}
    wa = card["interface"].get("whatif_arguments") or {}
    settings = dict(settings)
    if budget is not None:
        if "budget" not in wa:
            reasons.append("--budget: this run declares no budget argument (models/model_domain.yaml "
                           "whatif_interface.arguments.budget), so a budget cannot be mapped to its model")
        elif wa["budget"]["param"] in settings:
            reasons.append(f"--budget and --set {wa['budget']['param']} both set the same input")
        else:
            settings[wa["budget"]["param"]] = float(budget)
    if objective is not None:
        if "objective" not in wa:
            reasons.append("--objective: this run declares no objective argument (models/model_domain.yaml "
                           "whatif_interface.arguments.objective)")
        elif objective not in wa["objective"]["choices"]:
            reasons.append(f"--objective {objective!r} is not one the run declares "
                           f"({', '.join(wa['objective']['choices'])})")
        else:
            overrides[wa["objective"]["param"]] = objective
            checked.append({"name": wa["objective"]["param"], "value": objective, "via": "--objective"})
    params = card["domain"]["parameters"]
    for name, v in settings.items():
        d = params.get(name)
        if d is None:
            reasons.append(f"{name} is not an input of this run's model (its inputs are the citations.md registry "
                           f"parameters and those declared in models/model_domain.yaml)")
        elif "min" not in d:
            reasons.append(f"{name} has no established range (no registry prior interval and no "
                           "models/model_domain.yaml declaration), so no value of it can be checked")
        elif not d["min"] <= v <= d["max"]:
            reasons.append(f"{name} = {v:g} is outside the model's valid domain [{d['min']:g}, {d['max']:g}] "
                           f"({d['source']}: {d.get('basis')})")
        else:
            overrides[name] = v
            checked.append({"name": name, "value": v, "min": d["min"], "max": d["max"], "source": d["source"]})
    if not settings and objective is None and not reasons:
        reasons.append("nothing to ask: give at least one --set NAME=VALUE (or a declared --budget / --objective)")
    return overrides, reasons, checked


def _ci(vals: list[float]) -> dict:
    s = sorted(vals)
    n = len(s)
    return {"mean": sum(s) / n, "median": s[n // 2], "ci_low": s[int(0.025 * n)],
            "ci_high": s[min(n - 1, int(0.975 * n))], "n": n}


def _summaries(base: dict, query: dict, qdraws: list, bdraws: list | None, report: dict | None) -> dict:
    stored = (report or {}).get("scalar_outputs") or {}
    scal, cat = {}, {}
    for k in sorted(set(base) | set(query)):
        b, q = base.get(k), query.get(k)
        if isinstance(b, (str, bool)) or isinstance(q, (str, bool)):
            vals = [r[k] for r in qdraws if k in r]
            e = {"run_point": b, "whatif_point": q, "changed": b != q}
            if vals:
                top = max(set(map(str, vals)), key=lambda x: (list(map(str, vals)).count(x), x))
                e.update(whatif_draws_dominant=top, whatif_draws_dominance=list(map(str, vals)).count(top) / len(vals))
            cat[k] = e
            continue
        b, q = _num(b), _num(q)
        e = {"run_point": b, "whatif_point": q,
             "change": None if b is None or q is None else q - b,
             "relative_change": None if b in (None, 0) or q is None else (q - b) / abs(b)}
        vals = [v for v in (_num(r.get(k)) for r in qdraws) if v is not None]
        if vals:
            e["whatif_interval"] = _ci(vals)
        if isinstance(stored.get(k), dict):
            e["run_interval"] = {x: stored[k].get(x) for x in ("mean", "ci_low", "ci_high", "n")}
        if bdraws is not None:
            d = [qv - bv for qv, bv in ((_num(qr.get(k)), _num(br.get(k))) for qr, br in zip(qdraws, bdraws))
                 if qv is not None and bv is not None]
            if d:
                e["change_interval"] = _ci(d)
        scal[k] = e
    return {"scalar": scal, "categorical": cat}


def _draws(registry: list, report: dict | None, n: int | None, seed: int | None) -> tuple[list, dict]:
    pu = _sibling("propagate_uncertainty")
    rn, rs = (report or {}).get("n_draws"), (report or {}).get("seed")
    use_stored = n is None and isinstance(rn, int) and isinstance(rs, int)
    n = rn if use_stored else (DEFAULT_FRESH_DRAWS if n is None else n)
    seed = rs if use_stored else (seed if seed is not None else (rs if isinstance(rs, int) else DEFAULT_SEED))
    samples, plist = pu.draw_params(registry, n, seed)
    info = {"n": n, "seed": seed, "source": "fresh"}
    stored = (report or {}).get("parameter_samples") or {}
    if n == rn and seed == rs and stored:
        mine = pu.parameter_summary(samples)
        bad = [k for k in set(stored) | set(mine)
               if not all(math.isclose(float((stored.get(k) or {}).get(x, math.nan)),
                                       float((mine.get(k) or {}).get(x, math.nan)), rel_tol=1e-9, abs_tol=1e-12)
                          for x in ("mean", "ci_low", "ci_high"))]
        info["source"] = "stored" if not bad else "regenerated_not_matching"
        if bad:
            info["note"] = (f"the registry no longer reproduces uncertainty_report.yaml's draws for "
                            f"{', '.join(sorted(bad)[:5])}: the registry changed after the run's uncertainty analysis")
    return plist, info


def ask(run_dir: str, settings: dict, budget=None, objective=None, draws: int | None = None,
        seed: int | None = None, paired: bool = False, timeout: float | None = 3600) -> dict:
    mc = _sibling("model_card")
    card = mc.build(run_dir, evaluate=False)
    res = {"run_id": card["provenance"]["run_id"], "request": {"set": settings, "budget": budget,
                                                               "objective": objective, "draws": draws,
                                                               "seed": seed, "paired": paired}}
    overrides, reasons, checked = check_request(card, settings, budget, objective)
    reasons = list(card["not_usable_reasons"]) + reasons
    if reasons:
        return {**res, "status": "refused", "reasons": reasons}
    registry, _ = mc.load_registry(run_dir)
    report, _ = mc._read_yaml(os.path.join(run_dir, "uncertainty_report.yaml"))
    report = report if isinstance(report, dict) else None
    plist, dinfo = _draws(registry, report, draws, seed)
    point = mc.point_params(registry)
    q_point = {**point, **overrides}
    evals = {"base_point": [point], "query_point": [q_point], "query_draws": [{**p, **overrides} for p in plist]}
    if paired:
        evals["base_draws"] = plist
    w = mc.run_worker(run_dir, {"spec": mc.OUTCOME_FN_SPEC, "point": point, "evals": evals,
                                "expected_fingerprint": (report or {}).get("outcome_fn_fingerprint")}, timeout)
    res.update(settings_checked=checked, draws={**dinfo, "fixed_in_every_draw": sorted(overrides)},
               fingerprint=(w.get("fingerprint") or {}).get("sources"))
    if w.get("worker_error") or w.get("load_error"):
        return {**res, "status": "failed", "reasons": [w.get("worker_error") or f"cannot load: {w['load_error']}"]}
    if w.get("fingerprint_mismatch"):
        return {**res, "status": "refused", "reasons": [w["fingerprint_mismatch"]]}
    ev = w["evals"]
    for k in ("base_point", "query_point"):
        if not ev[k][0]["ok"]:
            return {**res, "status": "failed", "reasons": [f"outcome_fn failed at the {k.replace('_', ' ')}: "
                                                           f"{ev[k][0].get('error')}"]}
    fails = {k: [r for r in ev.get(k, []) if not r["ok"]] for k in ("query_draws", "base_draws")}
    for k, f in fails.items():
        if ev.get(k) and len(f) / len(ev[k]) > MAX_FAIL_SHARE:
            return {**res, "status": "failed", "reasons": [f"outcome_fn failed on {len(f)} of {len(ev[k])} "
                                                           f"{k.replace('_', ' ')} (e.g. {f[-1].get('error')})"]}
    ok_idx = [i for i in range(len(plist)) if ev["query_draws"][i]["ok"]
              and (not paired or ev["base_draws"][i]["ok"])]
    qd = [ev["query_draws"][i]["out"] for i in ok_idx]
    bd = [ev["base_draws"][i]["out"] for i in ok_idx] if paired else None
    base, query = ev["base_point"][0]["out"], ev["query_point"][0]["out"]
    res["draws"]["n_failed"] = len(plist) - len(ok_idx)
    res["outputs"] = _summaries(base, query, qd, bd, report)
    head = mc._headline(run_dir, base) or {}
    for k, h in head.items():
        h["whatif_point"] = _num(query.get(k))
    res["headline"] = head or None
    return {**res, "status": "answered", "reasons": []}


def _fmt(x) -> str:
    if x is None:
        return "-"
    if isinstance(x, str):
        return x
    return f"{x:,.4g}" if abs(x) < 1e15 else f"{x:.4g}"


def render(r: dict, outputs: list[str] | None = None, limit: int = 20) -> str:
    L = [f"What-if on {r['run_id']}: {r['status'].upper()}"]
    if r["status"] != "answered":
        L += [f"  - {x}" for x in r["reasons"]]
        return "\n".join(L)
    for s in r["settings_checked"]:
        L.append(f"  set {s['name']} = {_fmt(s['value'])}"
                 + (f"  (valid domain {_fmt(s['min'])} to {_fmt(s['max'])}, {s['source']})" if "min" in s else ""))
    d = r["draws"]
    src = {"stored": "the run's own registry draws (regenerated from uncertainty_report.yaml and checked)",
           "fresh": "fresh registry draws",
           "regenerated_not_matching": "registry draws that do NOT match uncertainty_report.yaml"}[d["source"]]
    L.append(f"  draws: {d['n'] - d['n_failed']} of {d['n']} {src}, seed {d['seed']}; "
             f"set inputs fixed in every draw" + (f"; {d['note']}" if d.get("note") else ""))
    sc = r["outputs"]["scalar"]
    keys = outputs or (list(r["headline"]) if r.get("headline") else list(sc))
    rows = [("output", "run point", "what-if point", "change", "what-if 95% interval", "run's interval")]
    for k in [k for k in keys if k in sc][:limit]:
        e = sc[k]
        wi, ri = e.get("whatif_interval"), e.get("run_interval")
        ch = _fmt(e["change"]) + (f" ({e['relative_change']:+.1%})" if e.get("relative_change") is not None else "")
        rows.append((k, _fmt(e["run_point"]), _fmt(e["whatif_point"]), ch,
                     f"[{_fmt(wi['ci_low'])}, {_fmt(wi['ci_high'])}]" if wi else "-",
                     f"[{_fmt(ri.get('ci_low'))}, {_fmt(ri.get('ci_high'))}]" if ri else "-"))
        if e.get("change_interval"):
            ci = e["change_interval"]
            rows.append(("  change, paired draws", "", "", "", f"[{_fmt(ci['ci_low'])}, {_fmt(ci['ci_high'])}]", ""))
    wid = [max(len(str(row[i])) for row in rows) for i in range(6)]
    L += ["  " + "  ".join(str(c).ljust(wid[i]) for i, c in enumerate(row)) for row in rows]
    if len([k for k in keys if k in sc]) > limit:
        L.append(f"  ... {len(keys) - limit} more outputs (--json or --outputs)")
    for k, h in (r.get("headline") or {}).items():
        if h.get("claim_value") is not None:
            L.append(f"  report claim {h['claim']} = {_fmt(h['claim_value'])}; the run's point estimate "
                     f"{'reproduces' if h.get('reproduces_claim') else 'does NOT reproduce'} it; what-if {_fmt(h.get('whatif_point'))}")
    cat = r["outputs"]["categorical"]
    if cat:
        changed = [k for k, e in cat.items() if e["changed"]]
        L.append(f"  categorical outputs changed at the point estimate: {len(changed)} of {len(cat)}"
                 + (f" ({', '.join(f'{k}: {cat[k]['run_point']} -> {cat[k]['whatif_point']}' for k in changed[:6])})"
                    if changed else ""))
    return "\n".join(L)


def log_query(run_dir: str, r: dict) -> str | None:
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "producer": "scripts/ask.py", **r}
    try:
        with open(os.path.join(run_dir, LOG), "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return None
    except OSError as e:
        return f"{LOG} not written ({type(e).__name__})"


# --------------------------------------------------------------------------- self-test

_REG = ("# Citations\n\n## Parameter Registry\n\n```yaml\nparameters:\n"
        "- name: effect_rr\n  value: 0.6\n  ci_low: 0.5\n  ci_high: 0.72\n  kind: relative_risk\n  source: C1\n"
        "- name: unit_cost\n  value: 5.0\n  ci_low: 4.0\n  ci_high: 6.0\n  kind: cost_usd\n  source: C2\n"
        "- name: budget_total\n  value: 1000.0\n  kind: cost_usd\n  source: C3\n```\n")
_FN = '''import helper
def outcome_fn(params):
    b = params.get("budget_total", 1000.0)
    n = int(b // (100 * params["unit_cost"]))
    obj = params.get("objective", "benefit")
    w = 2.0 if obj == "deaths" else 1.0
    return {"benefit": helper.SCALE * w * n * (1 - params["effect_rr"]), "cost": n * 100 * params["unit_cost"],
            "plan": "wide" if n > 2 else "narrow"}
'''


def _self_test() -> int:
    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    pu = _sibling("propagate_uncertainty")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        w = lambda rel, s: open(os.path.join(d, rel), "w").write(s)  # noqa: E731
        w("citations.md", _REG)
        w("models/outcome_fn.py", _FN)
        w("models/helper.py", "SCALE = 10.0\n")
        with pu.no_cached_bytecode():
            rep = pu.propagate(d, n_draws=40, seed=7)
        for m in [m for m in sys.modules if m in ("outcome_fn", "helper")]:
            del sys.modules[m]
        yaml.safe_dump(rep, open(os.path.join(d, "uncertainty_report.yaml"), "w"))
        # A1: an in-domain setting is answered at the point and over the run's own draws.
        r = ask(d, {"effect_rr": 0.7})
        b = r.get("outputs", {}).get("scalar", {}).get("benefit", {})
        ok(r["status"] == "answered" and abs(b["run_point"] - 10 * 2 * 0.4) < 1e-9
           and abs(b["whatif_point"] - 10 * 2 * 0.3) < 1e-9 and abs(b["relative_change"] + 0.25) < 1e-9,
           f"A1 answered with the change vs the run's point; got {r.get('status')} {r.get('reasons')} {b}")
        ok(r.get("draws", {}).get("source") == "stored" and r["draws"]["n"] == 40 and r["draws"]["seed"] == 7
           and b["whatif_interval"]["n"] == 40 and b["run_interval"]["ci_low"] == rep["scalar_outputs"]["benefit"]["ci_low"],
           f"A1b the run's draws are regenerated and matched; got {r.get('draws')}")
        # effect_rr fixed in every draw: only unit_cost varies (n funded), so the what-if interval
        # is the fixed effect times the funded-count spread.
        ok(all(abs((v / 3.0) - round(v / 3.0)) < 1e-9 for v in (b["whatif_interval"]["ci_low"], b["whatif_interval"]["ci_high"])),
           f"A1c the set input is fixed in every draw; got {b['whatif_interval']}")
        ok(ask(d, {"effect_rr": 0.7})["outputs"] == r["outputs"], "A1d deterministic: the same query gives the same answer")
        # A2-A4: outside the domain, unknown, no established range: refused with the reason.
        r2 = ask(d, {"effect_rr": 0.9})
        ok(r2["status"] == "refused" and "outside the model's valid domain [0.5, 0.72]" in r2["reasons"][0],
           f"A2 out-of-domain refused; got {r2}")
        r3 = ask(d, {"not_an_input": 1.0})
        ok(r3["status"] == "refused" and "is not an input" in r3["reasons"][0], f"A3 unknown input refused; got {r3}")
        r4 = ask(d, {"budget_total": 1500.0})
        ok(r4["status"] == "refused" and "no established range" in r4["reasons"][0], f"A4 no range refused; got {r4}")
        ok(ask(d, {})["status"] == "refused", "A4b an empty query is refused")
        # A5: --budget only when the run declares it, and only inside its declared range.
        r5 = ask(d, {}, budget=1500.0)
        ok(r5["status"] == "refused" and "declares no budget argument" in r5["reasons"][0], f"A5 undeclared budget; {r5}")
        w("models/model_domain.yaml", "parameters:\n  budget_total: {min: 500, max: 2000, basis: envelopes solved}\n"
                                      "whatif_interface:\n  arguments:\n    budget: {param: budget_total}\n"
                                      "    objective: {param: objective, choices: [benefit, deaths]}\n")
        r5 = ask(d, {}, budget=1500.0, draws=10)
        b5 = r5.get("outputs", {}).get("scalar", {}).get("benefit", {})
        ok(r5["status"] == "answered" and abs(b5["whatif_point"] - 10 * 3 * 0.4) < 1e-9
           and r5["draws"]["source"] == "fresh" and r5["draws"]["n"] == 10
           and r5["outputs"]["categorical"]["plan"]["changed"],
           f"A5b declared budget answered on fresh draws; got {r5.get('status')} {r5.get('reasons')} {b5}")
        ok(ask(d, {}, budget=5000.0)["status"] == "refused", "A5c a budget outside the declared range is refused")
        # A6: objective choices.
        ok(ask(d, {}, objective="cases")["status"] == "refused", "A6 an undeclared objective value is refused")
        r6 = ask(d, {}, objective="deaths", draws=0)
        ok(r6["status"] == "answered" and abs(r6["outputs"]["scalar"]["benefit"]["whatif_point"] - 16.0) < 1e-9,
           f"A6b a declared objective maps to its params key; got {r6.get('reasons')}")
        # A7: paired draws give an interval for the change.
        r7 = ask(d, {"effect_rr": 0.55}, draws=20, paired=True)
        ci = r7["outputs"]["scalar"]["benefit"].get("change_interval")
        ok(r7["status"] == "answered" and ci and ci["n"] == 20 and ci["ci_high"] > 0,
           f"A7 paired change interval; got {r7.get('reasons')} {ci}")
        # A8: the model code changed since the report: refused.
        w("models/helper.py", "SCALE = 11.0\n")
        r8 = ask(d, {"effect_rr": 0.7})
        ok(r8["status"] == "refused" and any("changed since uncertainty_report.yaml" in x for x in r8["reasons"]),
           f"A8 stale code refused; got {r8}")
        # A8b: every fingerprinted file is unchanged, but the function now imports a module the
        # report's code did not: the worker's full fingerprint check refuses.
        w("models/helper.py", "SCALE = 10.0\n")
        stale = dict(rep, outcome_fn_fingerprint={**rep["outcome_fn_fingerprint"], "sources": {
            k: v for k, v in rep["outcome_fn_fingerprint"]["sources"].items() if k != "models/helper.py"}})
        yaml.safe_dump(stale, open(os.path.join(d, "uncertainty_report.yaml"), "w"))
        r8 = ask(d, {"effect_rr": 0.7})
        ok(r8["status"] == "refused" and "not the code behind uncertainty_report.yaml" in r8["reasons"][0],
           f"A8b newly imported module refused by the worker; got {r8}")
        # A9: a model that fails at the point is reported as failed, not answered.
        os.remove(os.path.join(d, "uncertainty_report.yaml"))
        w("models/outcome_fn.py", _FN.replace("    b = params", "    assert params['effect_rr'] < 0.65\n    b = params"))
        r9 = ask(d, {"effect_rr": 0.7}, draws=5)
        ok(r9["status"] == "failed" and "AssertionError" in r9["reasons"][0], f"A9 failing model; got {r9}")
        # A10: the log records answered and refused queries; the text names the reason.
        ok(log_query(d, r2) is None and log_query(d, r) is None, "A10 log written")
        recs = [json.loads(x) for x in open(os.path.join(d, LOG))]
        ok([x["status"] for x in recs] == ["refused", "answered"] and recs[0]["request"]["set"] == {"effect_rr": 0.9},
           f"A10b log records; got {recs}")
        ok("outside the model's valid domain" in render(r2) and "benefit" in render(r), "A10c rendered text")
        ok(parse_settings(["a=1", "a=2", "b", "c=x"])[1].__len__() == 3, "A11 malformed settings rejected")
    if fails:
        print(f"FAIL: {len(fails)} case(s)", file=sys.stderr)
        for f in fails:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--budget", type=float)
    ap.add_argument("--objective")
    ap.add_argument("--draws", type=int, help="N fresh registry draws instead of the run's own")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--paired", action="store_true", help="also run the unchanged draws (interval of the change)")
    ap.add_argument("--outputs", help="comma-separated outputs to print (default: the headline-bound ones, else all)")
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir or not os.path.isdir(a.run_dir):
        ap.error("run_dir (a run directory) is required")
    if a.draws is not None and a.draws < 0:
        ap.error("--draws must be >= 0")
    settings, errs = parse_settings(a.set)
    if errs:
        r = {"run_id": os.path.basename(os.path.abspath(a.run_dir)), "status": "refused", "reasons": errs,
             "request": {"set": a.set}}
    else:
        r = ask(a.run_dir, settings, budget=a.budget, objective=a.objective, draws=a.draws, seed=a.seed,
                paired=a.paired, timeout=a.timeout)
    if not a.no_log:
        why = log_query(a.run_dir, r)
        if why:
            print(f"[WARNING] {why}", file=sys.stderr)
    print(json.dumps(r, indent=1, default=str) if a.json else
          render(r, [x.strip() for x in a.outputs.split(",")] if a.outputs else None))
    return {"answered": 0, "refused": 3}.get(r["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
