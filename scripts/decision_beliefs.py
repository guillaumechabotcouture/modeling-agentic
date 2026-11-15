#!/usr/bin/env python3
"""Pre-register the world model's beliefs about a full decision run before the run produces any output.

    python3 scripts/decision_beliefs.py seed   --id f6 --spec <run>/decision_spec.yaml --public public.yaml \
        --licensed <units.csv> --licensed <step0 dir> --total-cap-usd <usd> [--task-cap-usd 4]
    python3 scripts/decision_beliefs.py check  --id f6 --licensed ...       # the licensed-number check alone
    python3 scripts/decision_beliefs.py anchor --id f6 --licensed ...       # check, commit, push, fetch, anchor time
    python3 scripts/decision_beliefs.py table  --id f6                      # beliefs and the reading checklist
    python3 scripts/decision_beliefs.py --self-test

The beliefs come from the world model's own seed task (agents/v2_sdk_tasks.py seed_world_model) given an input that
names a decision run instead of a sweep (`decision_run`): the run's options, comparator, plan variants and decision
tests, in words, with every number masked except the public ones listed in --public. The harness validates the seed
with the belief table's decision-run rule (toolkit/analysis/belief_table.py decision_seed_problems), repairs it once,
and keeps it at trials/decisions/<id>/world_model_seed.yaml. The modeller writes outcome_map.yaml beside it: for each
belief, the run output that decides it and the rule per claim, or the guard's reason for setting it aside (its
outcome is fixed by declared inputs without running the dynamics). `anchor` runs the licensed-number check on both
files, commits them to the wm-trials branch, pushes, fetches and records the anchor time: the first reflog entry of
the remote-tracking ref holding the commit (never before the commit time; #225 P1-2), as world_model.git_anchors does.

Licensed-number check: inputs licensed under terms that forbid redistribution (survey- or map-derived tables, a run's
outputs built from them) are listed with --licensed (files or directories, read only here, never copied). A pushed
file is refused when any number in it equals a number of those files, or equals one of them rounded to 2 or 3
decimals. Exempt, by declaration: claim weights on the belief table's 0.05 grid (they encode nothing about the
inputs), integers 0 to 20 (counts of claims, rounds, ranks), calendar years (integers 1900 to 2100) and the public
numbers listed with their sources in --public `public_numbers`. Residual: a licensed integer in those exempt ranges is
not caught.
"""
from __future__ import annotations

import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

NUMBER_RE = re.compile(r"(?<![A-Za-z_\d.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?")
TEXT_EXT = (".csv", ".tsv", ".yaml", ".yml", ".json", ".jsonl", ".md", ".txt", ".log")
ROUND_DECIMALS = (2, 3)
SMALL_INT = 20
YEARS = (1900, 2100)
TOPICS = ("net_type", "smc_vs_cm", "irs", "resistance_field_values", "price_brackets", "weak_nets",
          "departure_from_funded_plan", "dalys_vs_funded_plan", "value_of_information")
GUARD_REASONS = ("effect_vs_own_parameter", "input_determined", "input_determined_pair", "engine_blind",
                 "confounded_independence")
from toolkit.analysis import belief_table as _BT  # noqa: E402

SEED_REL = "world_model_seed.yaml"
MAP_REL = "outcome_map.yaml"


def canon(tok) -> str | None:
    """A number token's canonical form ('0.40' -> '0.4', '12.0' -> '12', '5%' -> '5', '-3' -> '3'); None if none."""
    m = NUMBER_RE.fullmatch(str(tok).strip())
    if not m:
        return None
    try:
        d = Decimal(m.group(0).rstrip("%").lstrip("+-"))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    t = format(d.normalize(), "f")
    return t.rstrip("0").rstrip(".") if "." in t else t


def licensed_values(paths) -> dict:
    """{exact: set, rounded: {2: set, 3: set}} over every number in the text files at `paths` (files or directories,
    recursively; non-text files skipped). Read here only, never copied."""
    out = {"exact": set(), "rounded": {k: set() for k in ROUND_DECIMALS}}
    files = []
    for p in paths:
        if os.path.isdir(p):
            for dp, _, fn in os.walk(p):
                files += [os.path.join(dp, f) for f in sorted(fn)]
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise FileNotFoundError(f"licensed source {p}: no such file or directory (fail closed)")
    for f in files:
        if not f.lower().endswith(TEXT_EXT):
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            for tok in NUMBER_RE.findall(fh.read()):
                c = canon(tok)
                if c is None:
                    continue
                out["exact"].add(c)
                if "." in c:
                    v = Decimal(c)
                    for k in ROUND_DECIMALS:
                        if len(c.split(".")[1]) > k:
                            r = canon(str(round(v, k)))
                            if r is not None:
                                out["rounded"][k].add(r)
    return out


def licensed_problems(data, lic, allow=()) -> list:
    """Every number in `data` (YAML bytes or text, or a parsed object) that matches a licensed value, apart from the
    exemptions in the module docstring. Unreadable YAML is checked as text."""
    import yaml
    allow = {canon(a) for a in allow} - {None}
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        try:
            data = yaml.safe_load(data)
        except yaml.YAMLError:
            pass
    hits = []

    def tok_hit(tok, where):
        c = canon(tok)
        if c is None or c in allow:
            return
        if "." not in c and (int(c) <= SMALL_INT or YEARS[0] <= int(c) <= YEARS[1]):
            return
        dec = len(c.split(".")[1]) if "." in c else 0
        if c in lic["exact"] or (dec in ROUND_DECIMALS and c in lic["rounded"][dec]):
            hits.append(f"{where}: {tok}")

    def walk(x, where, key=None):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(k, f"{where}/key", None)
                walk(v, f"{where}/{k}", k)
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                walk(v, f"{where}[{i}]", None)
        elif isinstance(x, bool) or x is None:
            return
        elif isinstance(x, (int, float)):
            if key == "weight" and _BT.WEIGHT_GRID - 1e-9 <= x <= 1 and \
                    abs(x / _BT.WEIGHT_GRID - round(x / _BT.WEIGHT_GRID)) < 1e-6:
                return
            tok_hit(repr(x), where)
        else:
            for tok in NUMBER_RE.findall(str(x)):
                tok_hit(tok, where)
    walk(data, "")
    return hits


def mask_numbers(text, allow=()) -> str:
    """`text` with every number replaced by <value> except the allowed public ones (and calendar years)."""
    allow = {canon(a) for a in allow} - {None}

    def sub(m):
        c = canon(m.group(0))
        if c in allow or (c is not None and "." not in c and YEARS[0] <= int(c) <= YEARS[1]):
            return m.group(0)
        return "<value>"
    return NUMBER_RE.sub(sub, str(text))


def outcome_map_problems(seed, omap) -> list:
    """Why an outcome map is refused: every belief of the seed appears once, either with the output that decides it
    (a path under analysis/ or a deliverable file) and one rule per claim, or set aside with a guard reason."""
    bs = {b["id"]: b for b in seed.get("decision_beliefs") or []}
    ents = omap.get("entries") if isinstance(omap, dict) else None
    if not isinstance(ents, list):
        return ["entries: a list, one per belief"]
    bad, seen = [], []
    for e in ents:
        if not isinstance(e, dict):
            bad.append(f"entry {e!r}: a mapping")
            continue
        i = e.get("belief")
        seen.append(i)
        if i not in bs:
            bad.append(f"entry for {i!r}: no such belief in the seed")
            continue
        if "set_aside" in e:
            sa = e["set_aside"] or {}
            if sa.get("reason") not in GUARD_REASONS or not str(sa.get("why") or "").strip():
                bad.append(f"belief {i}: set_aside needs a reason in {GUARD_REASONS} and why")
            continue
        out = str(e.get("output") or "")
        if not (out.startswith("analysis/") or out in ("allocation.csv", "uncertainty_report.yaml")) \
                or ".." in out.split("/"):
            bad.append(f"belief {i}: output {out!r}: a path under analysis/ or a deliverable file")
        if not str(e.get("field") or "").strip():
            bad.append(f"belief {i}: field: what in the output is read")
        rules = e.get("rules")
        n = len(bs[i]["claims"])
        if not isinstance(rules, dict) or sorted(int(k) for k in rules) != list(range(n)) or \
                not all(str(v).strip() for v in rules.values()):
            bad.append(f"belief {i}: rules: one rule per claim, keyed 0 to {n - 1}")
    for i in bs:
        if seen.count(i) != 1:
            bad.append(f"belief {i}: appears {seen.count(i)} times in the map (once)")
    return bad


def reachable_at(repo, ref, commit) -> str | None:
    """When the remote-tracking `ref` was first seen to hold `commit`: its first reflog entry containing it, never
    before the commit time. None when no entry shows it."""
    import datetime as dt
    import subprocess
    run = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, timeout=120)  # noqa
    if not ref.startswith("refs/remotes/"):
        raise ValueError(f"{ref}: a remote-tracking ref (refs/remotes/<remote>/<branch>)")
    ct = run("show", "-s", "--format=%cI", commit)
    if ct.returncode:
        return None
    cdt = dt.datetime.fromisoformat(ct.stdout.strip())
    rl = run("log", "-g", "--date=iso-strict", "--format=%H %gd", ref)
    entries = []
    for row in (rl.stdout.splitlines() if rl.returncode == 0 else []):
        v, _, sel = row.partition(" ")
        if "@{" in sel and sel.endswith("}"):
            try:
                entries.append((dt.datetime.fromisoformat(sel[sel.find("@{") + 2:-1]), v))
            except ValueError:
                continue
    for when, v in sorted(entries):
        if when >= cdt and run("merge-base", "--is-ancestor", commit, v).returncode == 0:
            return when.isoformat()
    return None


TOPIC_HELP = {
    "net_type": "which net type (pyrethroid-only, PBO, dual-AI, none) the recommended plan chooses where",
    "smc_vs_cm": "where seasonal malaria chemoprevention is chosen, and where the case-management add-on is chosen "
                 "with or instead of it",
    "irs": "whether indoor residual spraying is chosen anywhere",
    "resistance_field_values": "whether the decision test that sets pyrethroid resistance to field bioassay values "
                               "changes the plan, and where",
    "price_brackets": "whether the net and SMC price decision tests change the plan, and which choices move",
    "weak_nets": "whether the decision test that shifts every net type's killing across its prior range changes "
                 "the plan",
    "departure_from_funded_plan": "how far the recommended plan departs from the funded grant plan (which units and "
                                  "packages change)",
    "dalys_vs_funded_plan": "DALYs averted by the recommended plan relative to the funded grant plan, as a direction "
                            "or a ratio band",
    "value_of_information": "which uncertain inputs carry decision-relevant value of information",
}
OUTPUTS = {
    "analysis/point.yaml": "the point plan: per unit, the chosen package, its benefit, cost and outcomes averted",
    "allocation.csv": "the deliverable: one package per unit with DALYs averted",
    "analysis/decision_tests.yaml": "each decision test: verdict CHANGES_PLAN or NO_CHANGE, share of units changed, "
                                    "regret, budget moved",
    "analysis/plan_variants.yaml": "the operational variants beside the headline plan, including the funded grant "
                                   "plan evaluated as given: objective, cost, units differing",
    "analysis/value_of_information.yaml": "EVPI and EVPPI per uncertain input, each with its noise floor",
    "analysis/uncertainty.yaml": "the headline's interval over the uncertainty draws and how often the plan holds",
    "analysis/sensitivity.yaml": "one-at-a-time sensitivity of the plan to fitted parameters",
}


def decision_context(spec, card, public, beliefs=(15, 30)) -> dict:
    """The seed task's input for a decision run: the spec's words (question, options, comparator, objective, plan
    variants, decision tests, departures) and the engine card, every number masked except the public ones and
    years; the run's input values and any output are withheld."""
    allow = [str(x["value"]) for x in public.get("public_numbers") or []]
    m = lambda t: mask_numbers(t, allow) if isinstance(t, str) else t  # noqa: E731
    o, an = spec.get("options") or {}, spec.get("analysis") or {}
    return {
        "question": m(spec.get("question")),
        "withheld": "the run's input values (survey- and map-derived unit tables), the engine's fitted values and every "
                    "output are withheld: the beliefs come from the literature, the programme context and how the "
                    "engine works, and are scored against the run's outputs afterwards",
        "decision_run": {"topics": list(TOPICS), "topic_help": TOPIC_HELP,
                         "beliefs": {"min": beliefs[0], "max": beliefs[1]}, "outputs": OUTPUTS},
        "decision_maker": m(spec.get("decision_maker")), "decision_date": spec.get("decision_date"),
        "components": {k: {kk: vv for kk, vv in (v or {}).items()} for k, v in (o.get("components") or {}).items()},
        "packages": "every combination of the components with at most one net type (generated)",
        "comparator": m((spec.get("comparator") or {}).get("text")),
        "objective": {"outcome": (spec.get("objective") or {}).get("outcome"), "sense": "avert",
                      "delivery_years": (spec.get("objective") or {}).get("delivery"),
                      "outcome_window": (spec.get("objective") or {}).get("horizon")},
        "constraints": {"envelope": m(f"US${(spec.get('constraints') or {}).get('envelope_usd', 0) / 1e6:.2f}M"),
                        "eligibility": sorted(((spec.get("constraints") or {}).get("eligibility") or {})),
                        "affordability": "at most a small share of uncertainty draws may cost more than the "
                                         "allocatable budget; plans are enforced within the reserve that meets it"},
        "plan_variants": [{"id": v.get("id"), "type": v.get("type"), "text": m(v.get("text"))}
                          for v in (an.get("plan_variants") or {}).get("variants") or []],
        "decision_tests": [{"id": t.get("id"), "type": t.get("type"), "text": m(t.get("text")),
                            **({"parameter": t["parameter"]} if t.get("parameter") else {}),
                            **({"components": t["components"]} if t.get("components") else {})}
                           for t in an.get("decision_tests") or []],
        "decision_thresholds": "a test changes the plan when more than a tenth of units change package, regret "
                               "exceeds a hundredth of the objective, or more than a twentieth of the budget moves",
        "engine_departures": [m(d.get("reason")) for d in spec.get("engine_departures") or []],
        "engine": {"name": card.get("name"), "version": card.get("version"), "components": card.get("components"),
                   "mechanisms": [m(x) for x in card.get("mechanisms") or []],
                   "unit_attributes": sorted(card.get("unit_attributes") or {})},
        "public": {k: v for k, v in public.items()},
    }


def _paths(a):
    d = os.path.join(a.trials_dir, "decisions", a.id)
    return d, os.path.join(d, SEED_REL), os.path.join(d, MAP_REL)


def _allow(a):
    import yaml
    pub = yaml.safe_load(open(a.public)) if getattr(a, "public", None) else {}
    return [str(x["value"]) for x in (pub or {}).get("public_numbers") or []], pub or {}


def main(argv=None) -> int:
    import argparse
    import shutil
    import yaml
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(prog="decision_beliefs.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("seed", "check", "anchor", "table"):
        p = sub.add_parser(n)
        p.add_argument("--id", required=True)
        p.add_argument("--trials-dir", default=os.path.join(REPO, "trials"))
        if n != "table":
            p.add_argument("--licensed", action="append", required=True, help="a licensed file or directory")
            p.add_argument("--public", required=True, help="YAML: public_numbers [{value, source}] and context")
    sp = sub.choices["seed"]
    sp.add_argument("--spec", required=True)
    sp.add_argument("--card", default=os.path.join(REPO, "toolkit", "engines", "malaria", "card.yaml"))
    sp.add_argument("--total-cap-usd", type=float, required=True)
    sp.add_argument("--task-cap-usd", type=float, default=3.0)
    sp.add_argument("--min-beliefs", type=int, default=15)
    sp.add_argument("--max-beliefs", type=int, default=30)
    a = ap.parse_args(argv)
    d, seed_p, map_p = _paths(a)
    if a.cmd == "table":
        seed = yaml.safe_load(open(seed_p))
        print(json.dumps({"beliefs": _BT.decision_table(seed), "checklist": _BT.decision_checklist(seed)}, indent=1))
        return 0
    allow, pub = _allow(a)
    lic = licensed_values(a.licensed)
    if a.cmd == "seed":
        import world_model_trials as WMT
        ctx = decision_context(yaml.safe_load(open(a.spec)), yaml.safe_load(open(a.card)), pub,
                               (a.min_beliefs, a.max_beliefs))
        bad = licensed_problems(ctx, lic, allow)
        if bad:
            print(json.dumps({"refused": "the seed input holds licensed numbers", "hits": bad[:20]}))
            return 1
        rd = os.path.join(d, "seed_run")
        os.makedirs(rd, exist_ok=True)
        led = WMT.SpendLedger(os.path.join(a.trials_dir, "spend.jsonl"), a.total_cap_usd)
        fn = WMT.sdk_seed_fn()
        r = WMT.seed_step({"id": f"decision_{a.id}"}, rd, ctx, fn, led, a.task_cap_usd)
        hits = licensed_problems(open(os.path.join(rd, WMT.SEED), "rb").read(), lic, allow) \
            if r.get("status") == "ok" else []
        if hits:                    # one repair of the same seed task, told only where its numbers are refused
            rid = led.reserve(f"decision_{a.id}", "seed licensed repair", a.task_cap_usd)
            if rid is None:
                r = dict(r, status="budget_refused")
            else:
                fb = [f"remove every number at {h.split(':')[0]} (it matches a withheld input value); state it in "
                      "words or drop it, and keep the beliefs and weights" for h in hits]
                rr = fn(rd, ctx, fb, a.task_cap_usd)
                usd = float(rr.usd) if rr is not None and isinstance(rr.usd, (int, float)) else a.task_cap_usd
                led.charge(rid, usd, "sdk reported" if rr is not None else "no result: whole cap")
                sd = yaml.safe_load(open(os.path.join(rd, WMT.SEED_FILE)))
                again = WMT.seed_problems(sd, ctx) + licensed_problems(sd, lic, allow)
                r = dict(r, licensed_repair={"hits": hits, "usd": usd, "after": again},
                         usd=r.get("usd", 0) + usd, status="ok" if not again else "seed_refused")
                if not again:
                    shutil.copyfile(os.path.join(rd, WMT.SEED_FILE), os.path.join(rd, WMT.SEED))
        if r.get("status") == "ok":
            shutil.copyfile(os.path.join(rd, WMT.SEED), seed_p)
        print(json.dumps(dict(r, seed=seed_p if r.get("status") == "ok" else None,
                              committed_usd=round(led.committed(), 4)), default=str))
        return 0 if r.get("status") == "ok" else 1
    seed = yaml.safe_load(open(seed_p))
    problems = {"seed": _BT.decision_seed_problems(seed, {"topics": list(TOPICS), "beliefs": {"min": 1}}),
                "outcome_map": outcome_map_problems(seed, yaml.safe_load(open(map_p))) if os.path.isfile(map_p)
                else ["missing"],
                "licensed": {os.path.basename(p): licensed_problems(open(p, "rb").read(), lic, allow)
                             for p in (seed_p, map_p) if os.path.isfile(p)}}
    clean = not problems["seed"] and not problems["outcome_map"] and not any(problems["licensed"].values())
    print(json.dumps(dict(problems, ok=clean), indent=1))
    if a.cmd == "check" or not clean:
        return 0 if clean else 1
    import world_model_trials as WMT
    rel_root = os.path.relpath(os.path.realpath(a.trials_dir), os.path.realpath(REPO)).replace(os.sep, "/")
    anchor = WMT.GitAnchor(REPO, os.path.join(a.trials_dir, ".wm-trials"), trials_rel=rel_root)
    rels = [os.path.relpath(p, REPO).replace(os.sep, "/") for p in (seed_p, map_p)]
    r = anchor.publish(rels, f"decision {a.id}", check=lambda rel, data: licensed_problems(data, lic, allow))
    at = reachable_at(anchor.wt, anchor.ref, r["commit"])
    if at is None:
        print(json.dumps({"error": "no reflog entry shows the remote holding the commit", **r}))
        return 1
    rec = dict(r, anchored_at=at, files=rels, rule="first reflog entry of the remote-tracking ref holding the commit")
    open(os.path.join(d, "anchor.json"), "w").write(json.dumps(rec, indent=1) + "\n")
    print(json.dumps(rec, indent=1))
    return 0


def _self_test() -> int:
    import subprocess
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tryit(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- the failure is the finding
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def db1():
        ok([canon(x) for x in ("0.40", "12.0", "5%", "-3", "1e-3", "x")] == ["0.4", "12", "5", "3", "0.001", None],
           "DB1 canonical number forms")

    def db2():
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "step0", "analysis"))
            open(os.path.join(d, "units.csv"), "w").write("lga_id,prev,pop,year\nA,0.41237,183456,2018\nB,0.4,7,2019\n")
            open(os.path.join(d, "step0", "analysis", "point.yaml"), "w").write("dalys: 1234567.8\nshare: 0.6666\n")
            open(os.path.join(d, "step0", "fig.png"), "wb").write(b"\x89PNG 0.555")
            lic = licensed_values([os.path.join(d, "units.csv"), os.path.join(d, "step0")])
            ok("0.41237" in lic["exact"] and "183456" in lic["exact"] and "1234567.8" in lic["exact"]
               and "0.41" in lic["rounded"][2] and "0.412" in lic["rounded"][3] and "0.555" not in lic["exact"],
               f"DB2 licensed values: exact and rounded to 2 and 3 decimals, text files only: {sorted(lic['exact'])}")
            allow = ["1.63"]
            seed = {"decision_beliefs": [{"id": "D1", "claims": [{"claim": "dual-AI in most northern LGAs",
                                                                  "weight": 0.4}, {"claim": "no", "weight": 0.6}],
                                          "question": "In 2024, with 3 rounds, at the 1.63 list price?"}]}
            ok(licensed_problems(yaml.safe_dump(seed).encode(), lic, allow) == [],
               f"DB2b weights on the grid, small integers, years and public numbers pass: "
               f"{licensed_problems(yaml.safe_dump(seed).encode(), lic, allow)}")
            for leak in ("prevalence about 0.41 in Kano", "population 183456", "0.412 resistant", "1234567.8 DALYs",
                         "share 0.67"):
                pr = licensed_problems({"notes": leak}, lic, allow)
                ok(bool(pr), f"DB2c a licensed number is refused: {leak!r}: {pr}")
            ok(licensed_problems({"claims": [{"claim": "x", "weight": 0.41237}]}, lic) != [],
               "DB2d a weight off the grid is checked like any number")
            ok(licensed_problems("0.4 of units", lic) != [] and licensed_problems("about 0.5", lic) == [],
               "DB2e one-decimal numbers match exactly only")

    def db3():
        t = "Envelope $241.14M across 309 LGAs; resistance 0.1282 clipped; list 1.63; since 2016"
        m = mask_numbers(t, ["241.14", "309", "1.63"])
        ok(m == "Envelope $241.14M across 309 LGAs; resistance <value> clipped; list 1.63; since 2016",
           f"DB3 numbers are masked except public ones and years: {m}")

    def db4():
        seed = {"decision_beliefs": [
            {"id": "D1", "claims": [{"claim": "a", "weight": 0.6}, {"claim": "b", "weight": 0.4}]},
            {"id": "D2", "claims": [{"claim": "a", "weight": 0.5}, {"claim": "b", "weight": 0.3},
                                    {"claim": "c", "weight": 0.2}]}]}
        good = {"entries": [{"belief": "D1", "output": "analysis/decision_tests.yaml", "field": "tests[DT10].verdict",
                             "rules": {0: "CHANGES_PLAN", 1: "NO_CHANGE"}},
                            {"belief": "D2", "set_aside": {"reason": "input_determined", "why": "price arithmetic"}}]}
        ok(outcome_map_problems(seed, good) == [], f"DB4 a full outcome map is accepted: "
                                                   f"{outcome_map_problems(seed, good)}")
        bads = {
            "a belief missing": {"entries": good["entries"][:1]},
            "a rule per claim missing": {"entries": [dict(good["entries"][0], rules={0: "x"}), good["entries"][1]]},
            "an output outside analysis/": {"entries": [dict(good["entries"][0], output="/etc/passwd"),
                                                        good["entries"][1]]},
            "an unknown guard reason": {"entries": [good["entries"][0], {"belief": "D2", "set_aside":
                                                                         {"reason": "boring", "why": "w"}}]},
            "a belief twice": {"entries": good["entries"] + [good["entries"][0]]},
            "an unknown belief": {"entries": good["entries"] + [dict(good["entries"][0], belief="D9")]},
        }
        for why, om in bads.items():
            ok(bool(outcome_map_problems(seed, om)), f"DB4b refused: {why}: {outcome_map_problems(seed, om)[:2]}")

    def db5():
        with tempfile.TemporaryDirectory() as d:
            work, remote = os.path.join(d, "w"), os.path.join(d, "r.git")
            g = lambda *a, env=None: subprocess.run(["git", "-C", work, *a], capture_output=True, text=True,  # noqa
                                                   env=env)
            subprocess.run(["git", "init", "-q", "--bare", remote], capture_output=True)
            os.makedirs(work)
            g("init", "-q")
            g("config", "user.email", "t@t")
            g("config", "user.name", "t")
            open(os.path.join(work, "f"), "w").write("x")
            g("add", ".")
            g("commit", "-qm", "c", env=dict(os.environ, GIT_COMMITTER_DATE="2020-01-01T11:00:00+00:00"))
            c = g("rev-parse", "HEAD").stdout.strip()
            ok(reachable_at(work, "refs/remotes/origin/main", c) is None, "DB5 not on the remote: no anchor time")
            g("remote", "add", "origin", remote)
            g("push", "-q", "origin", "HEAD:refs/heads/main")
            g("fetch", "-q", "origin")
            at = reachable_at(work, "refs/remotes/origin/main", c)
            ok(at is not None and at[:4] >= "2026", f"DB5b the anchor time is when the remote first held it, not the "
                                                    f"commit time: {at}")


    def db6():
        spec = {"question": "Allocate $241.14M across 309 units; resistance 0.8623 at most",
                "comparator": {"text": "no campaign; 0.1282 floor"}, "constraints": {"envelope_usd": 241140000},
                "options": {"components": {"net_pbo": {"group": "net"}}},
                "analysis": {"decision_tests": [{"id": "DT10", "type": "unit_attribute", "text": "clipped to 0.945"}],
                             "plan_variants": {"variants": [{"id": "v", "type": "fixed_plan", "text": "as funded"}]}},
                "engine_departures": [{"reason": "out of band at 0.665"}]}
        card = {"name": "e", "mechanisms": ["decay 0.3 per year"], "unit_attributes": {"resistance": "x"}}
        pub = {"public_numbers": [{"value": 241.14, "source": "GF"}, {"value": 309, "source": "spec"}]}
        ctx = decision_context(spec, card, pub)
        txt = json.dumps(ctx)
        ok("241.14" in txt and "309" in txt and not any(x in txt for x in ("0.8623", "0.1282", "0.945", "0.665",
                                                                           "0.3 per"))
           and ctx["decision_run"]["topics"] == list(TOPICS) and ctx["decision_tests"][0]["id"] == "DT10",
           f"DB6 the decision-run input masks every number but the public ones: {txt[:300]}")

    for n, f in (("DB1", db1), ("DB2", db2), ("DB3", db3), ("DB4", db4), ("DB5", db5), ("DB6", db6)):
        tryit(n, f)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
