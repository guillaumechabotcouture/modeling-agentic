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

# a standalone number: 12, -3, 0.4, .41 (leading dot), 4.1e-1, 41.2% (Codex re-review: leading-dot decimals)
NUMBER_RE = re.compile(r"(?<![A-Za-z_\d.])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?%?")
# a thousands-grouped number (183,456): read in checked and masked text only, never in a licensed CSV (where a comma
# separates two values)
GROUPED_RE = re.compile(r"(?<![A-Za-z_\d.,])\d{1,3}(?:,\d{3})+(?![\d,])(?:\.\d+)?%?")
WEIGHT_CTX = re.compile(r"""weight["']?\s*:\s*$""")
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


def _candidates(tok) -> list:
    """The canonical forms a token can stand for: itself, and for a percentage also its fraction (41.2% -> 0.412)."""
    c = canon(tok.replace(",", ""))
    if c is None:
        return []
    out = [c]
    if tok.endswith("%"):
        f = canon(str(Decimal(c) / 100))
        if f is not None:
            out.append(f)
    return out


def _tokens(text) -> list:
    """(start, token) for every number in `text`, thousands-grouped spellings included."""
    toks = [(m.start(), m.group(0)) for m in NUMBER_RE.finditer(text)]
    return toks + [(m.start(), m.group(0)) for m in GROUPED_RE.finditer(text)]


def _on_weight_grid(c) -> bool:
    try:
        x = float(c)
    except ValueError:
        return False
    return _BT.WEIGHT_GRID - 1e-9 <= x <= 1 and abs(x / _BT.WEIGHT_GRID - round(x / _BT.WEIGHT_GRID)) < 1e-6


def licensed_problems(data, lic, allow=()) -> list:
    """Every number in `data` that matches a licensed value, apart from the exemptions in the module docstring.
    Bytes or text are checked as the exact raw text that is published: comments, overwritten duplicate keys and
    unparseable YAML included, never a parsed view (Codex re-review). A weight is exempt only as the value of a
    `weight:` key on the 0.05 grid. A parsed object (the seed input) is walked."""
    allow = {canon(a) for a in allow} - {None}
    hits = []

    def tok_hit(tok, where, weight=False):
        cs = _candidates(tok)
        if not cs or any(c in allow for c in cs):
            return
        c = cs[0]
        if weight and _on_weight_grid(c):
            return
        if "." not in c and not tok.endswith("%") and (int(c) <= SMALL_INT or YEARS[0] <= int(c) <= YEARS[1]):
            return
        for x in cs:
            dec = len(x.split(".")[1]) if "." in x else 0
            if x in lic["exact"] or (dec in ROUND_DECIMALS and x in lic["rounded"][dec]):
                hits.append(f"{where}: {tok}")
                return

    if isinstance(data, (bytes, bytearray, str)):
        text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else data
        for k, line in enumerate(text.splitlines()):
            for at, tok in _tokens(line):
                tok_hit(tok, f"line {k + 1}", weight=bool(WEIGHT_CTX.search(line[:at])))
        return hits

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
            tok_hit(repr(x), where, weight=key == "weight")
        else:
            for _, tok in _tokens(str(x)):
                tok_hit(tok, where)
    walk(data, "")
    return hits


DIGIT_TOKENS = {"v0.17", "GC7"} | {f"DT{k}" for k in range(1, 11)}
PUBLISH_YEARS = (2000, 2035)
CITATION_YEARS = (1950, 2035)
LOCATION_INTS = (1, 20)              # a table or figure number in a basis location
TOKEN_RE = re.compile(r"""[^\s/,;:()\[\]{}<>"'`|=]+""")
MAP_TOP_KEYS = {"decision", "run", "conventions", "entries"}
MAP_ENTRY_KEYS = {"belief", "output", "field", "rules", "note", "set_aside"}


def digit_problems(obj, kind, ids=(), tokens=()) -> list:
    """The allowlist (Codex re-review of e9243e4): in a canonical decision file a number may stand only at a declared
    path (a claim weight in a seed, a rule's claim index in an outcome map), and a string may hold a digit only as a
    whole token from the public list: DIGIT_TOKENS, the files' own belief and question ids, `tokens` (the public
    file's public_tokens), a year in PUBLISH_YEARS (CITATION_YEARS in a citation) or a table number in a location.
    Everything else with a digit is refused."""
    ok_tok = DIGIT_TOKENS | set(ids) | set(tokens)
    bad = []

    def year(t, lo_hi):
        return t.isdigit() and lo_hi[0] <= int(t) <= lo_hi[1]

    def string(x, where, field):
        for t in TOKEN_RE.findall(x):
            t = t.strip(".!?*-+")
            if not any(ch.isdigit() for ch in t) or t in ok_tok or year(t, PUBLISH_YEARS):
                continue
            if field == "citation" and year(t, CITATION_YEARS):
                continue
            if field == "location" and t.isdigit() and LOCATION_INTS[0] <= int(t) <= LOCATION_INTS[1]:
                continue
            bad.append(f"{where}: {t!r} holds a digit outside the public list")

    def walk(x, path, field=None):
        where = "/".join(map(str, path))
        if isinstance(x, dict):
            for k, v in x.items():
                if kind == "map" and len(path) == 3 and path[0] == "entries" and path[2] == "rules":
                    if isinstance(k, bool) or not isinstance(k, int) or not 0 <= k <= 3:
                        bad.append(f"{where}: rule key {k!r}: a claim index 0 to 3")
                elif isinstance(k, str):
                    string(k, where + "/<key>", None)
                else:
                    bad.append(f"{where}: key {k!r} is not a string")
                walk(v, path + [k], k if isinstance(k, str) else field)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, path + [i], field)
        elif isinstance(x, bool) or x is None:
            return
        elif isinstance(x, (int, float)):
            if not (kind == "seed" and len(path) == 5 and path[0] == "decision_beliefs" and path[2] == "claims"
                    and path[4] == "weight" and _BT._on_grid(x)):
                bad.append(f"{where}: a number outside the declared weight paths")
        elif isinstance(x, str):
            string(x, where, field)
        else:
            bad.append(f"{where}: a {type(x).__name__} value")
    walk(obj, [])
    return bad


def publish_problems(kind, canon_bytes, lic, allow=(), topics=TOPICS, seed=None, tokens=()) -> list:
    """Why a canonical decision file may not be published: the strict schema (seed: the belief table's decision-run
    rule; map: outcome_map_problems against `seed` and its declared keys), the digit allowlist, then the licensed-value
    scan of the canonical text as a second line."""
    import yaml
    obj = yaml.safe_load(canon_bytes.decode("utf-8"))
    if not isinstance(obj, dict):
        return ["not a mapping"]
    if kind == "seed":
        bad = _BT.decision_seed_problems(obj, {"topics": list(topics), "beliefs": {"min": 1}})
        ids = [b.get("id") for b in obj.get("decision_beliefs") or [] if isinstance(b, dict)]
        ids += [q.get("id") for q in obj.get("open_questions") or [] if isinstance(q, dict)]
    else:
        bad = outcome_map_problems(seed or {}, obj)
        bad += [f"undeclared top-level keys {sorted(set(obj) - MAP_TOP_KEYS)}"] if set(obj) - MAP_TOP_KEYS else []
        for e in obj.get("entries") or []:
            if isinstance(e, dict) and set(e) - MAP_ENTRY_KEYS:
                bad.append(f"entry {e.get('belief')}: undeclared keys {sorted(set(e) - MAP_ENTRY_KEYS)}")
        ids = [b.get("id") for b in (seed or {}).get("decision_beliefs") or [] if isinstance(b, dict)]
    ids = [i for i in ids if isinstance(i, str)]
    return bad + digit_problems(obj, kind, ids, tokens) + licensed_problems(canon_bytes, lic, allow)


def mask_numbers(text, allow=()) -> str:
    """`text` with every number replaced by <value> except the allowed public ones (and calendar years)."""
    allow = {canon(a) for a in allow} - {None}

    def sub(m):
        c = canon(m.group(0).replace(",", ""))
        if c in allow or (c is not None and "." not in c and not m.group(0).endswith("%")
                          and YEARS[0] <= int(c) <= YEARS[1]):
            return m.group(0)
        return "<value>"
    return NUMBER_RE.sub(sub, GROUPED_RE.sub(sub, str(text)))


def mask_obj(x, allow=()):
    """A copy of `x` with every number masked except the public ones: numeric values become <value>, strings are
    masked by mask_numbers (Codex re-review: component parameters entered the seed input unmasked)."""
    if isinstance(x, dict):
        return {k: mask_obj(v, allow) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [mask_obj(v, allow) for v in x]
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, (int, float)):
        return x if canon(repr(x)) in ({canon(a) for a in allow} - {None}) else "<value>"
    return mask_numbers(str(x), allow)


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
        "components": mask_obj(o.get("components") or {}, allow),
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


def licensed_repair(rd, ctx, hits, lic, allow, fn, led, qid, cap, r) -> dict:
    """One repair of the seed task, told only where its numbers are refused. Reserved before, charged after; every
    parse or validation failure after the charge is a recorded refusal, never an exception (Codex re-review). The
    repaired seed is checked on its raw bytes and kept only when it passes both checks."""
    import yaml
    import world_model_trials as WMT
    rid = led.reserve(qid, "seed licensed repair", cap)
    if rid is None:
        return dict(r, status="budget_refused")
    fb = [f"remove every number at {h.split(':')[0]} (it matches a withheld input value); state it in words or drop "
          "it, and keep the beliefs and weights" for h in hits]
    try:
        rr = fn(rd, ctx, fb, cap)
    except Exception as e:  # noqa: BLE001 -- the reservation is charged in full and the repair is refused
        led.charge(rid, cap, f"crashed: {type(e).__name__}: whole cap")
        return dict(r, status="seed_refused", usd=r.get("usd", 0) + cap,
                    licensed_repair={"hits": hits, "usd": cap, "after": [f"{type(e).__name__}: {e}"[:300]]})
    usd = float(rr.usd) if rr is not None and isinstance(rr.usd, (int, float)) else cap
    led.charge(rid, usd, "sdk reported" if rr is not None else "no result: whole cap")
    try:                    # everything after the charge, the promotion included: a refusal, never a crash
        raw, _ = WMT._sio().read_nofollow(rd, WMT.SEED_FILE)
        sd = yaml.safe_load(raw.decode("utf-8"))
        again = (WMT.seed_problems(sd, ctx) if isinstance(sd, dict) else [f"{WMT.SEED_FILE}: a mapping"]) \
            + licensed_problems(raw, lic, allow)
        if not again:
            WMT._sio().write_nofollow(rd, WMT.SEED, raw, atomic=True)   # the bytes that were checked
    except Exception as e:  # noqa: BLE001
        again = [f"{WMT.SEED_FILE} after the repair: {type(e).__name__}: {e}"[:300]]
    return dict(r, licensed_repair={"hits": hits, "usd": usd, "after": again}, usd=r.get("usd", 0) + usd,
                status="ok" if not again else "seed_refused")


def _paths(a):
    d = os.path.join(a.trials_dir, "decisions", a.id)
    return d, os.path.join(d, SEED_REL), os.path.join(d, MAP_REL)


def _allow(a):
    import yaml
    pub = yaml.safe_load(open(a.public)) if getattr(a, "public", None) else {}
    return [str(x["value"]) for x in (pub or {}).get("public_numbers") or []], pub or {}


def main(argv=None) -> int:
    import argparse
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
        kept = os.path.join(rd, WMT.SEED)
        if os.path.isfile(kept) and not WMT.seed_problems(yaml.safe_load(open(kept)), ctx):
            r = {"status": "ok", "reused": kept, "usd": 0.0}     # a validated seed is never paid for twice
        else:
            r = WMT.seed_step({"id": f"decision_{a.id}"}, rd, ctx, fn, led, a.task_cap_usd)
        hits = licensed_problems(open(os.path.join(rd, WMT.SEED), "rb").read(), lic, allow) \
            if r.get("status") == "ok" else []
        if hits:                    # one repair of the same seed task, told only where its numbers are refused
            r = licensed_repair(rd, ctx, hits, lic, allow, fn, led, f"decision_{a.id}", a.task_cap_usd, r)
        if r.get("status") == "ok":
            WMT._sio().copy_nofollow(d, os.path.join("seed_run", WMT.SEED), SEED_REL)
        print(json.dumps(dict(r, seed=seed_p if r.get("status") == "ok" else None,
                              committed_usd=round(led.committed(), 4)), default=str))
        return 0 if r.get("status") == "ok" else 1
    import world_model_trials as WMT
    tokens = list(pub.get("public_tokens") or [])
    canon = {}
    for p in (seed_p, map_p):
        try:
            canon[p] = WMT.canonical_yaml(WMT._sio().read_nofollow(os.path.dirname(p), os.path.basename(p))[0])
        except (OSError, WMT.TrialError) as e:
            canon[p] = e
    seed = yaml.safe_load(canon[seed_p].decode()) if isinstance(canon[seed_p], bytes) else None

    def check_one(path, data):
        if path.endswith(SEED_REL):
            return publish_problems("seed", data, lic, allow, TOPICS, tokens=tokens)
        return publish_problems("map", data, lic, allow, TOPICS, seed=seed, tokens=tokens)
    problems = {os.path.basename(p): ([f"unreadable: {c}"] if not isinstance(c, bytes) else check_one(p, c))
                for p, c in canon.items()}
    clean = not any(problems.values())
    print(json.dumps(dict(problems, ok=clean), indent=1))
    if a.cmd == "check" or not clean:
        return 0 if clean else 1
    import world_model_trials as WMT
    rel_root = os.path.relpath(os.path.realpath(a.trials_dir), os.path.realpath(REPO)).replace(os.sep, "/")
    anchor = WMT.GitAnchor(REPO, os.path.join(a.trials_dir, ".wm-trials"), trials_rel=rel_root)
    rels = [os.path.relpath(p, REPO).replace(os.sep, "/") for p in (seed_p, map_p)]
    r = anchor.publish(rels, f"decision {a.id}", check=lambda rel, data: check_one(rel, data))
    at = reachable_at(anchor.wt, anchor.ref, r["commit"])
    if at is None:
        print(json.dumps({"error": "no reflog entry shows the remote holding the commit", **r}))
        return 1
    rec = dict(r, anchored_at=at, files=rels, rule="first reflog entry of the remote-tracking ref holding the commit")
    WMT._sio().write_nofollow(d, "anchor.json", (json.dumps(rec, indent=1) + "\n").encode(), atomic=True)
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


    # Codex re-review of 306ab98..dbdca9f (2026-11-15): the reviewer's probes
    def db7():
        lic = {"exact": {"0.41237", "183456", "0.83"}, "rounded": {2: {"0.41"}, 3: {"0.412"}}}
        seed = ("decision_beliefs:\n  - id: D1\n    claims:\n      - {claim: a, weight: 0.4}\n"
                "      - {claim: b, weight: 0.6}\n")
        ok(licensed_problems(seed.encode(), lic) == [], f"DB7 raw bytes: weights on the grid pass: "
                                                        f"{licensed_problems(seed.encode(), lic)}")
        for why, raw in (("a YAML comment", seed + "# measured prevalence 0.41237\n"),
                         ("an overwritten duplicate key", "notes: 0.41237\nnotes: clean\n" + seed),
                         ("a weight off the grid", seed.replace("weight: 0.4}", "weight: 0.41237}")),
                         ("unparseable YAML", "decision_beliefs: [ 0.412\n")):
            ok(bool(licensed_problems(raw.encode(), lic)), f"DB7b the raw bytes are checked: {why}")

    def db8():
        lic = {"exact": {"0.41237", "183456"}, "rounded": {2: {"0.41"}, 3: {"0.412"}}}
        for sp in ("prevalence .41237", "4.1237e-1", "41.237%", "population 183,456", "0.41237", ".41 share"):
            ok(bool(licensed_problems({"notes": sp}, lic)) and bool(licensed_problems(sp.encode(), lic)),
               f"DB8 another spelling of a licensed number is refused: {sp!r}")
        m = mask_numbers("prevalence .41237, 4.1237e-1, 41.237% and 183,456; version v0.17", [])
        ok(m == "prevalence <value>, <value>, <value> and <value>; version v0.17",
           f"DB8b masking recognises the same spellings: {m}")

    def db9():
        spec = {"options": {"components": {"smc": {"reach_share": 0.83, "eligible": "smc_eligible",
                                                    "nested": {"k": [0.77, "x 0.66"]}}}}}
        ctx = decision_context(spec, {}, {"public_numbers": []})
        txt = json.dumps(ctx["components"])
        ok(not any(x in txt for x in ("0.83", "0.77", "0.66")) and "smc_eligible" in txt,
           f"DB9 component parameter values are masked unless public: {txt}")
        ctx2 = decision_context(spec, {}, {"public_numbers": [{"value": 0.83, "source": "s"}]})
        ok("0.83" in json.dumps(ctx2["components"]), "DB9b a public component value is kept")

    def db10():
        from types import SimpleNamespace
        import world_model_trials as WMT
        ctx = {"decision_run": {"topics": ["net_type"], "beliefs": {"min": 1, "max": 3}}}
        lic = {"exact": {"0.41237"}, "rounded": {2: set(), 3: set()}}
        for why, body in (("unparseable YAML", "decision_beliefs: [\n"),
                          ("a list-valued id", "decision_beliefs:\n  - {id: [D1], topic: net_type}\n"),
                          ("not a mapping", "- 1\n")):
            with tempfile.TemporaryDirectory() as d:
                def fn(rd, c, fb, cap, body=body):
                    open(os.path.join(rd, WMT.SEED_FILE), "w").write(body)
                    return SimpleNamespace(usd=0.25, note="n")
                led = WMT.SpendLedger(os.path.join(d, "s.jsonl"), 5.0)
                r = licensed_repair(d, ctx, ["/notes: 0.41237"], lic, [], fn, led, "decision_t", 1.0, {"usd": 0.0})
                ok(r.get("status") == "seed_refused" and abs(led.spent() - 0.25) < 1e-9 and r["licensed_repair"]["after"],
                   f"DB10 a licensed repair that returns {why} is a recorded refusal after its charge: {r}")


    # Codex re-review of e9243e4: stop racing spellings; publish a canonical dump, allow digits only where declared
    def db11():
        import world_model_trials as WMT
        lic = {"exact": {"0.85", "183456", "0.41237"}, "rounded": {2: set(), 3: set()}}
        base = ("decision_beliefs:\n- id: D1\n  topic: net_type\n  question: In 2026, which net under GC7 and DT10?\n"
                "  claims:\n  - {claim: PBO in most LGAs at v0.17, weight: 0.6}\n  - {claim: not, weight: 0.4}\n"
                "  basis:\n  - {citation: Mosha JF et al. 2022 Lancet, location: Table 2}\n  decides: true\n"
                "  why_decides: w\n  layer: world\nnotes: n\n")
        pr = publish_problems("seed", WMT.canonical_yaml(base.encode()), lic, (), TOPICS[:1])
        ok(pr == [], f"DB11 a valid seed's canonical dump passes (years, GC7, DT10, v0.17, ids, a table number): {pr}")
        probes = {
            "underweight key": base.replace("  layer: world\n", "  layer: world\n  underweight: 0.85\n"),
            "block scalar": base.replace("notes: n\n", "notes: |\n  survey\n  weight: 0.85\n"),
            "183_456": base.replace("notes: n", "notes: 183_456"),
            "0x2cca0": base.replace("notes: n", "notes: 0x2cca0"),
            "unicode escape": base.replace("notes: n", 'notes: "\\u0030.41237"'),
            "a digit in a claim": base.replace("claim: not,", "claim: not in 37 LGAs,"),
            "a weight off the grid": base.replace("weight: 0.4}", "weight: 0.41237}"),
        }
        for why, raw in probes.items():
            try:
                pr = publish_problems("seed", WMT.canonical_yaml(raw.encode()), lic, (), TOPICS[:1])
            except Exception as e:  # noqa: BLE001
                pr = [f"raised {type(e).__name__}"]
            ok(bool(pr) and not any(x.startswith("raised") for x in pr), f"DB11b refused: {why}: {pr[:2]}")
        c = WMT.canonical_yaml((base + "# survey weight: 0.85\n").encode())
        ok(b"#" not in c and b"0.85" not in c and publish_problems("seed", c, lic, (), TOPICS[:1]) == [],
           "DB11c a comment never reaches the published bytes: the canonical dump holds none")

    def db12():
        from types import SimpleNamespace
        import stat
        import world_model_trials as WMT
        ctx = {"decision_run": {"topics": ["net_type"], "beliefs": {"min": 1, "max": 3}}}
        good = ("decision_beliefs:\n- id: D1\n  topic: net_type\n  question: q\n  claims:\n"
                "  - {claim: a, weight: 0.6}\n  - {claim: b, weight: 0.4}\n  basis:\n  - {mechanism: m}\n"
                "  decides: true\n  why_decides: w\n  layer: world\n")
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis"))
            os.chmod(os.path.join(d, "analysis"), stat.S_IRUSR | stat.S_IXUSR)

            def fn(rd, c, fb, cap):
                open(os.path.join(rd, WMT.SEED_FILE), "w").write(good)
                return SimpleNamespace(usd=0.25, note="n")
            led = WMT.SpendLedger(os.path.join(d, "s.jsonl"), 5.0)
            try:
                r = licensed_repair(d, ctx, ["line 1: x"], {"exact": set(), "rounded": {2: set(), 3: set()}}, [], fn,
                                    led, "decision_t", 1.0, {"usd": 0.0})
            except Exception as e:  # noqa: BLE001
                r = {"raised": f"{type(e).__name__}: {e}"}
            finally:
                os.chmod(os.path.join(d, "analysis"), stat.S_IRWXU)
            ok(r.get("status") == "seed_refused" and abs(led.spent() - 0.25) < 1e-9,
               f"DB12 a promotion that fails after the charge (read-only analysis/) is a recorded refusal: {r}")

    for n, f in (("DB1", db1), ("DB2", db2), ("DB3", db3), ("DB4", db4), ("DB5", db5), ("DB6", db6), ("DB7", db7),
                 ("DB8", db8), ("DB9", db9), ("DB10", db10), ("DB11", db11), ("DB12", db12)):
        tryit(n, f)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
