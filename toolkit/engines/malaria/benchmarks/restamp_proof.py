#!/usr/bin/env python3
"""Byte-identity proof between two engine versions, and the re-stamp it allows (v0.6).

    python3 toolkit/engines/malaria/benchmarks/restamp_proof.py prove [--old-ref origin/v2] [--seeds 11] [--workers 2]
    python3 toolkit/engines/malaria/benchmarks/restamp_proof.py restamp      # only after a passing proof
    python3 toolkit/engines/malaria/benchmarks/restamp_proof.py --self-test

v0.6 adds a delivery period and routine campaigns, and v0.7 routine SMC (smc_routine); each acts only when a horizon
or a unit asks for it. The benchmark pack, the equilibration evidence and the guess-sensitivity evidence ask for none. `prove` runs their inputs under
the old code (`git archive <old-ref>` into a temporary directory) and under the working tree, one process each, and
compares every output exactly (per-capita outcomes, windows, cost drivers, meta without wall-clock seconds;
initial-state dicts). It writes restamp_proof.yaml: the scope, the two engine identities, one sha256 per case and
side, and the verdict.

Cases: every pack setting with all of its arms in one run_batch, at the pack's own n_agents, horizon, fitted values,
calibrated attribute (from results.yaml) and burn-in, for each seed in --seeds; equilibration initial states
(equilibration.py _job) and guess-sensitivity batches (guess_sensitivity.py _job) at the levels, arms and seeds in
EVIDENCE_CASES. The calibration-grid runs are not repeated: they are the same code path at other attribute values.

`restamp` re-stamps only the identity fields (engine_identity / engine_version) of results.yaml, fitted.yaml,
equilibration.yaml and guess_sensitivity.yaml, and only when the stored proof is for the current code, says
`identical: true`, and was produced by this script as committed (no uncommitted change). Each file then records
`restamped: {by, proof, scope, old_identity, old_engine_version, proof_script_commit, note}`: re-stamped by the proof,
not recomputed. A file re-stamped before (v0.6) is re-stamped again: its earlier record is replaced by the new one,
which keeps it under `previous`; a file already stamped for the new version is refused. A Coiled pack run confirms it
afterwards (validate_once.sh).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
REL = os.path.relpath(HERE, ROOT)                # toolkit/engines/malaria/benchmarks
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
OUT = os.path.join(HERE, "restamp_proof.yaml")
STAMPED = ("results.yaml", "fitted.yaml", "equilibration.yaml", "guess_sensitivity.yaml")
# (module, job args): equilibration arms at two levels and the guess-sensitivity batches at two levels, one seed each
EVIDENCE_CASES = [
    ("equilibration", [3.5, "base", {"burnin_years": 50}, 1]),
    ("equilibration", [28.0, "doubled", {"burnin_years": 100}, 1]),
    ("equilibration", [9.0, "guess_hi", {"burnin_years": 50, "init_eir_per_m0": 2.4}, 2]),
    ("guess_sensitivity", [3.5, "x1", 1, 50]),
    ("guess_sensitivity", [28.0, "x0.25", 2, 50]),
]

# v0.8 (Codex r1 P2-5): pinned scenarios with the features the pack does not use (it builds plain horizons, and its
# units set neither routine nets nor routine SMC), each one run_batch at its own seed in both trees. Members may set
# their own unit attributes and parameters (heterogeneous batches, more than one burn-in group).
_SEASON = [0.3, 0.3, 0.4, 0.6, 0.9, 1.3, 1.8, 2.1, 1.7, 1.1, 0.8, 0.5]
_BASE_UNIT = {"m0": 6.0, "season": _SEASON, "resistance": 0.6, "species": {"gambiae": 0.6, "funestus": 0.4},
              "cm_ft": 0.35, "itn_use": 0.55, "itn_use_at": 0.5}
SCENARIOS = {
    # GC7-like: delivery 2024-2027 inside outcomes 2024-2029.25, routine nets every 3 y, routine SMC after delivery
    "gc7_delivery_routine": {
        "unit": dict(_BASE_UNIT, itn_hist_years=[2021.25], campaign_year=2025.25, itn_routine_interval=3.0,
                     smc_routine=1),
        "horizon": {"start": 2023.0, "stop": 2029.25, "window": [2024.0, 2029.25],
                    "windows": {"y1": [2024.0, 2025.0], "y4": [2027.0, 2028.0], "tail": [2028.0, 2029.25]}},
        "delivery": [2024.0, 2027.0], "params": {"n_agents": 1500, "burnin_years": 20}, "seed": 21,
        "members": [{"package": {}}, {"package": {"net": "pyr", "smc": 1}}, {"package": {"net": "pbo", "cm": 1}},
                    {"package": {"net": "dual", "smc": 1, "irs": 1, "cm": 1}}]},
    # boundary dates: the campaign and an IRS round at the delivery start, the first routine SMC round and a routine
    # campaign at the delivery end, the add-on stopping there, windows cutting through months
    "boundaries": {
        "unit": dict(_BASE_UNIT, itn_hist_years=[2023.5], campaign_year=2024.0, itn_routine_interval=1.0,
                     smc_routine=1, smc_months=[1, 2, 3, 4], irs_month=1),
        "horizon": {"start": 2023.0, "stop": 2026.5, "window": [2024.0, 2026.5],
                    "windows": {"a": [2024.0, 2025.0], "b": [2025.0, 2026.5], "c": [2024.37, 2025.61]}},
        "delivery": [2024.0, 2025.0], "params": {"n_agents": 1500, "burnin_years": 20}, "seed": 22,
        "members": [{"package": {}}, {"package": {"net": "pyr", "smc": 1, "irs": 1, "cm": 1}},
                    {"package": {"cm": 1}}]},
    # heterogeneous members: two units, parameters inside and outside the burn-in key, no delivery period (v0.5 path)
    "heterogeneous": {
        "unit": dict(_BASE_UNIT, itn_hist_years=[2022.5], campaign_year=2024.25),
        "horizon": {"start": 2023.0, "stop": 2026.0, "window": [2024.0, 2026.0],
                    "windows": {"y1": [2024.0, 2025.0], "y2": [2025.0, 2026.0]}},
        "delivery": None, "params": {"n_agents": 1500, "burnin_years": 20}, "seed": 23,
        "members": [{"package": {}}, {"package": {"net": "pyr"}, "params": {"smc_pe": 0.8}},
                    {"package": {"net": "dual", "smc": 1}, "unit": {"m0": 30.0, "hist_net": "dual"}},
                    {"package": {"irs": 1}, "unit": {"m0": 30.0, "hist_net": "dual"}, "params": {"hh_net_corr": 0.2}},
                    {"package": {"cm": 1}, "params": {"cm_add": 0.25}}]},
}

# Run inside one code tree (argv: root, cases json, out json). It imports only what both versions have.
WORKER = r'''
import json, os, sys
root, cases_path, out_path = sys.argv[1:4]
sys.path.insert(0, root)
os.chdir(root)
import yaml
from toolkit.engines.contract import UnitSpec, Package, Horizon, engine_identity
from toolkit.engines.malaria.engine import MalariaEngine, VERSION
B = os.path.join(root, "toolkit/engines/malaria/benchmarks")
pack = yaml.safe_load(open(os.path.join(B, "pack.yaml")))
res = yaml.safe_load(open(os.path.join(B, "results.yaml"))) or {}
fit = {k: v["value"] for k, v in ((yaml.safe_load(open(os.path.join(B, "fitted.yaml"))) or {}).get("parameters") or {}).items()}
cal = res.get("settings_calibrated") or {}
e = MalariaEngine()

def strip(o):
    return {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers,
            "meta": {k: v for k, v in o.meta.items() if k != "seconds"}}

out = {"identity": engine_identity(e), "version": VERSION, "cases": {}}
for c in json.load(open(cases_path)):
    if c["kind"] == "pack":
        s = pack["settings"][c["setting"]]
        unit = dict(s["unit"])
        if c["setting"] in cal:
            unit[cal[c["setting"]]["attribute"]] = cal[c["setting"]]["value"]
        hd = dict(s.get("horizon") or pack["horizon"])
        h = Horizon(float(hd["start"]), float(hd["stop"]), tuple(hd["window"]), dict(hd.get("windows") or {}))
        arms = [a for a, v in pack["arms"].items() if v["setting"] == c["setting"]]
        par = dict(s.get("params") or {}, **fit)
        os_ = e.run_batch([par] * len(arms), UnitSpec(c["setting"], unit),
                          [Package(a, pack["arms"][a].get("package") or {}) for a in arms], h, int(c["seed"]))
        val = {a: strip(o) for a, o in zip(arms, os_)}
    elif c["kind"] == "scenario":
        from toolkit.engines.horizon import DeliveryHorizon
        sp = c["spec"]
        hz = sp["horizon"]
        hargs = (float(hz["start"]), float(hz["stop"]), tuple(hz["window"]), dict(hz.get("windows") or {}))
        h = DeliveryHorizon(*hargs, delivery=tuple(sp["delivery"])) if sp.get("delivery") else Horizon(*hargs)
        ms = sp["members"]
        os_ = e.run_batch([{**(sp.get("params") or {}), **fit, **(m.get("params") or {})} for m in ms],
                          [UnitSpec(c["id"], dict(sp["unit"], **(m.get("unit") or {}))) for m in ms],
                          [Package(f"m{i}", m.get("package") or {}) for i, m in enumerate(ms)], h, int(sp["seed"]))
        val = {f"m{i}": strip(o) for i, o in enumerate(os_)}
    else:
        import importlib
        mod = importlib.import_module("toolkit.engines.malaria.benchmarks." + c["kind"])
        r = mod._job(tuple(c["args"]))
        val = {k: v for k, v in r.items() if k != "seconds"}
    out["cases"][c["id"]] = val
json.dump(out, open(out_path, "w"), sort_keys=True, default=repr)
'''


def cases(pack: dict, seeds: list) -> list:
    out = [{"id": f"pack:{s}:{sd}", "kind": "pack", "setting": s, "seed": sd} for s in pack["settings"] for sd in seeds]
    out += [{"id": f"{m}:{':'.join(str(x) for x in a[:2])}:seed{a[3] if m == 'equilibration' else a[2]}",
             "kind": m, "args": a} for m, a in EVIDENCE_CASES]
    out += [{"id": f"scenario:{n}:seed{sp['seed']}", "kind": "scenario", "spec": sp} for n, sp in SCENARIOS.items()]
    return out


def digest(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=repr).encode()).hexdigest()


def compare(old: dict, new: dict) -> dict:
    """Per case: equal (the outputs compared exactly as JSON), and the two digests."""
    rows = {}
    for cid in sorted(set(old["cases"]) | set(new["cases"])):
        a, b = old["cases"].get(cid), new["cases"].get(cid)
        rows[cid] = {"equal": a is not None and b is not None and json.dumps(a, sort_keys=True, default=repr)
                     == json.dumps(b, sort_keys=True, default=repr),
                     "old_sha256": digest(a), "new_sha256": digest(b)}
    return rows


def _git(*args, cwd=ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _run_tree(root: str, cs: list, tmp: str, tag: str) -> dict:
    cp, op = os.path.join(tmp, f"cases_{tag}.json"), os.path.join(tmp, f"out_{tag}.json")
    json.dump(cs, open(cp, "w"))
    env = dict(os.environ, PYTHONWARNINGS="ignore")
    env.pop("PYTHONPATH", None)
    subprocess.run([sys.executable, "-c", WORKER, root, cp, op], check=True, env=env, cwd=root)
    return json.load(open(op))


def prove(old_ref: str, seeds: list, workers: int, settings: list | None = None) -> dict:
    pack = yaml.safe_load(open(os.path.join(HERE, "pack.yaml")))
    cs = cases(pack, seeds)
    if settings:
        cs = [c for c in cs if c["kind"] != "pack" or c["setting"] in settings]
    with tempfile.TemporaryDirectory() as tmp:
        old_root = os.path.join(tmp, "old")
        os.makedirs(old_root)
        tar = subprocess.run(["git", "archive", old_ref, "toolkit"], cwd=ROOT, check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", old_root], input=tar, check=True)
        if workers >= 2:
            with ThreadPoolExecutor(2) as ex:
                fo = ex.submit(_run_tree, old_root, cs, tmp, "old")
                fn = ex.submit(_run_tree, ROOT, cs, tmp, "new")
                old, new = fo.result(), fn.result()
        else:
            old, new = _run_tree(old_root, cs, tmp, "old"), _run_tree(ROOT, cs, tmp, "new")
    rows = compare(old, new)
    return {"producer": f"{REL}/restamp_proof.py",
            "old": {"ref": old_ref, "commit": _git("rev-parse", old_ref), "identity": old["identity"],
                    "engine_version": old["version"]},
            "new": {"commit_of_tree_base": _git("rev-parse", "HEAD"), "identity": new["identity"],
                    "engine_version": new["version"]},
            "scope": {"pack_settings": sorted({c["setting"] for c in cs if c["kind"] == "pack"}),
                      "pack_arms": "every arm of each setting, in one run_batch per setting and seed",
                      "seeds": seeds, "n_agents": "each setting's own (pack.yaml params; 5000 where set, else the "
                      "card's 4000); equilibration 4000; guess sensitivity the card's 4000",
                      "burnin_years": "the card's (50), the pack's own", "evidence_cases": EVIDENCE_CASES,
                      "scenarios": {n: f"{len(sp['members'])} members, seed {sp['seed']}, delivery "
                                       f"{sp.get('delivery')}" for n, sp in SCENARIOS.items()},
                      "not_repeated": "calibration-grid runs (same code path at other attribute values)",
                      "compared": "per_capita, windows, cost_drivers and meta (minus wall-clock seconds) exactly as "
                                  "JSON; initial-state dicts exactly"},
            "cases": rows, "n_cases": len(rows), "identical": bool(rows) and all(r["equal"] for r in rows.values())}


def _record(doc: dict, proof: dict, script_commit: str) -> dict:
    rec = {"by": f"{REL}/restamp_proof.py (byte-identity proof, not recomputed)",
           "proof": f"{REL}/restamp_proof.yaml", "proof_identical": proof["identical"],
           "scope": proof["scope"], "old_engine_version": doc.get("engine_version", (doc.get("engine_identity") or {})
                                                                   .get("version")),
           "proof_script_commit": script_commit,
           "note": "the numbers in this file were computed by the old code; the proof shows the new code gives "
                   "identical outputs on these inputs. A Coiled pack run (validate_once.sh) confirms it."}
    if "engine_identity" in doc:
        rec["old_identity"] = dict(doc["engine_identity"])
    if "code_sha256" in doc:
        rec["old_code_sha256"] = doc["code_sha256"]   # kept as is: the code that computed the numbers
    if isinstance(doc.get("restamped"), dict):        # v0.7: a file re-stamped before keeps the earlier record(s)
        prev = dict(doc["restamped"])
        rec["previous"] = [{k: v for k, v in prev.items() if k != "previous"}] + list(prev.get("previous") or [])
    return rec


def _changed(proof: dict) -> dict:
    """Identity keys whose value the new code changes: {key: (old, new)}."""
    o, n = proof["old"]["identity"], proof["new"]["identity"]
    return {k: (o.get(k), n[k]) for k in n if o.get(k) != n[k]}


def restamp_doc(doc: dict, proof: dict, script_commit: str) -> dict:
    """The file's content after a re-stamp: each identity key the new code changes is set to its new value where the
    file holds the old one (other keys, e.g. a pre-fit params hash, stay), engine_version is the new version, and
    `restamped` records the re-stamp."""
    out = dict(doc)
    if "engine_identity" in doc:
        ident = dict(doc["engine_identity"])
        for k, (old, new) in _changed(proof).items():
            if ident.get(k) == old:
                ident[k] = new
        out["engine_identity"] = ident
    if "engine_version" in doc:
        out["engine_version"] = proof["new"]["engine_version"]
    out["restamped"] = _record(doc, proof, script_commit)
    return out


def _scalar(k, v) -> str:
    return f"'{v}'" if k == "version" else str(v)      # versions are quoted strings, hashes plain


def restamp_text(text: str, proof: dict, script_commit: str) -> str:
    """restamp_doc applied to the file's text line by line, so every other byte stays as written; checked against
    restamp_doc (fail closed when they disagree)."""
    doc = yaml.safe_load(text) or {}
    ch = _changed(proof)
    if "restamped" in doc:
        # v0.7: an earlier record is the file's last top-level key (the previous re-stamp appended it); it is replaced
        # by the new record, which keeps it under `previous` (fail closed when it is not last)
        ls = text.splitlines(keepends=True)
        at = [i for i, ln in enumerate(ls) if ln.startswith("restamped:")]
        if len(at) != 1 or any(ln.strip() and not ln.startswith((" ", "-", "#")) for ln in ls[at[0] + 1:]):
            raise ValueError("re-stamp: the earlier `restamped:` record is not the file's last top-level key")
        text = "".join(ls[:at[0]])
    lines, in_id = text.splitlines(keepends=True), False
    for i, ln in enumerate(lines):
        if ln.startswith("engine_identity:"):
            in_id = True
            continue
        if in_id and not ln.startswith("  "):
            in_id = False
        if in_id:
            for k, (old, new) in ch.items():
                if ln.rstrip("\n") == f"  {k}: {_scalar(k, old)}":
                    lines[i] = f"  {k}: {_scalar(k, new)}\n"
        elif ln.startswith("engine_version: ") and "engine_version" in doc:
            lines[i] = f"engine_version: '{proof['new']['engine_version']}'\n"
    out = "".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    out += yaml.safe_dump({"restamped": _record(doc, proof, script_commit)}, sort_keys=False, width=110)
    if yaml.safe_load(out) != restamp_doc(doc, proof, script_commit):
        raise ValueError("re-stamp: the line edit does not give the expected content (fields not where expected)")
    return out


def restamp() -> int:
    proof = yaml.safe_load(open(OUT)) if os.path.exists(OUT) else None
    from toolkit.engines.contract import engine_identity
    from toolkit.engines.malaria.engine import MalariaEngine
    me = os.path.abspath(__file__)
    if not proof or not proof.get("identical"):
        print("refused: no passing proof (restamp_proof.yaml)")
        return 1
    if proof["new"]["identity"] != engine_identity(MalariaEngine()):
        print("refused: the proof was made for other engine code")
        return 1
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", me], cwd=ROOT).returncode != 0:
        print("refused: commit restamp_proof.py first (the record names the commit of the proof script)")
        return 1
    commit = _git("log", "-n", "1", "--format=%H", "--", me)
    texts = {}
    for f in STAMPED:
        p = os.path.join(HERE, f)
        text = open(p).read()
        d = yaml.safe_load(text) or {}
        if str(d.get("engine_version", (d.get("engine_identity") or {}).get("version"))) == str(proof["new"]["engine_version"]):
            print(f"refused: {f} is already stamped for engine {proof['new']['engine_version']}")
            return 1
        texts[p] = restamp_text(text, proof, commit)
    for p, t in texts.items():
        open(p, "w").write(t)
        print(f"re-stamped {os.path.basename(p)}")
    return 0


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    a = {"cases": {"x": {"per_capita": {"cases": {"u5": 0.1}}}, "y": {"N": 3}}}
    b = json.loads(json.dumps(a))
    ok(all(r["equal"] for r in compare(a, b).values()), "RP1 identical outputs compare equal case by case")
    b["cases"]["x"]["per_capita"]["cases"]["u5"] = 0.1 + 1e-17 if 0.1 + 1e-17 != 0.1 else 0.10000000000000002
    ok(not compare(a, b)["x"]["equal"] and compare(a, b)["y"]["equal"], "RP2 a last-bit difference is not identical")
    del b["cases"]["y"]
    ok(not compare(a, b)["y"]["equal"], "RP3 a case missing on one side is not identical")
    pack = {"settings": {"s1": {}, "s2": {}}}
    cs = cases(pack, [11, 12])
    ok(sum(c["kind"] == "pack" for c in cs) == 4 and len({c["id"] for c in cs}) == len(cs),
       "RP4 every pack setting runs at every seed; case ids are unique")
    proof = {"identical": True, "scope": {"seeds": [11]},
             "old": {"identity": {"name": "m", "version": "0.5", "code_sha256": "aa", "params_sha256": "pp"}},
             "new": {"identity": {"name": "m", "version": "0.6", "code_sha256": "bb", "params_sha256": "pp"},
                     "engine_version": "0.6"}}
    txt = ("producer: p\nengine_identity:\n  name: m\n  version: '0.5'\n  code_sha256: aa\n  params_sha256: qq\n"
           "benchmarks:\n- id: B1\n  metric: a long text\n    wrapped as the writer wrapped it\nengine_version: '0.5'\n")
    out = restamp_text(txt, proof, "abc")
    r = yaml.safe_load(out)
    ok(r["engine_identity"] == {"name": "m", "version": "0.6", "code_sha256": "bb", "params_sha256": "qq"}
       and r["engine_version"] == "0.6" and r["benchmarks"] == yaml.safe_load(txt)["benchmarks"]
       and r["restamped"]["old_identity"]["code_sha256"] == "aa" and r["restamped"]["old_engine_version"] == "0.5"
       and r["restamped"]["proof_script_commit"] == "abc" and "not recomputed" in r["restamped"]["by"]
       and out.startswith(txt.replace("'0.5'", "'0.6'").replace("aa", "bb")),
       "RP5 a re-stamp changes only the identity keys the new code changes (a file's own params hash stays), keeps "
       "every other byte, and records the old identity, version, scope and commit")
    e = yaml.safe_load(restamp_text("producer: q\nengine_version: '0.5'\ncode_sha256: cc\nlevels: [2]\n", proof, "abc"))
    ok(e["engine_version"] == "0.6" and e["code_sha256"] == "cc" and e["restamped"]["old_code_sha256"] == "cc"
       and "engine_identity" not in e, "RP6 evidence files keep the code hash that computed them, beside the record")
    try:
        restamp_text("engine_identity: {name: m, version: '0.5', code_sha256: aa}\n", proof, "abc")
        ok(False, "RP7 an identity the line edit cannot place fails closed")
    except ValueError:
        ok(True, "RP7 an identity the line edit cannot place fails closed")
    # v0.7: a file already re-stamped once (v0.6) is re-stamped again; the earlier record moves under `previous`
    p2 = {"identical": True, "scope": {"seeds": [11]},
          "old": {"identity": {"name": "m", "version": "0.6", "code_sha256": "bb", "params_sha256": "pp"}},
          "new": {"identity": {"name": "m", "version": "0.7", "code_sha256": "cc", "params_sha256": "pp"},
                  "engine_version": "0.7"}}
    try:
        out2 = restamp_text(out, p2, "def")
        r2 = yaml.safe_load(out2)
        body = out[:out.index("restamped:")]
        ok(r2["engine_identity"] == {"name": "m", "version": "0.7", "code_sha256": "cc", "params_sha256": "qq"}
           and r2["engine_version"] == "0.7" and r2["restamped"]["old_identity"]["code_sha256"] == "bb"
           and r2["restamped"]["old_engine_version"] == "0.6" and r2["restamped"]["proof_script_commit"] == "def"
           and r2["restamped"]["previous"] == [r["restamped"]]
           and out2.startswith(body.replace("'0.6'", "'0.7'").replace("bb", "cc")),
           "RP8 a file re-stamped before is re-stamped again: identity lines updated, every other byte kept, the "
           "earlier record kept under previous")
    except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case
        ok(False, f"RP8 a file re-stamped before is re-stamped again ({type(ex).__name__}: {ex})")
    try:
        restamp_text("engine_version: '0.6'\nrestamped:\n  by: x\nlevels: [2]\n", p2, "def")
        ok(False, "RP9 a restamped record that is not the file's last key fails closed")
    except ValueError:
        ok(True, "RP9 a restamped record that is not the file's last key fails closed")
    # v0.8 (Codex r1 P2-5): the pack uses plain horizons with none of the v0.6-v0.7 features, so the proof also runs
    # pinned scenarios that use them, in both trees
    sc = [c for c in cases({"settings": {}}, [11]) if c["kind"] == "scenario"]
    specs = [c["spec"] for c in sc]

    def units(sp):
        return [dict(sp["unit"], **(m.get("unit") or {})) for m in sp["members"]]
    has = {
        "delivery period": any(sp.get("delivery") for sp in specs),
        "routine nets": any(x.get("itn_routine_interval") for sp in specs for x in units(sp)),
        "routine SMC": any(x.get("smc_routine") == 1 for sp in specs for x in units(sp)),
        "add-on with a delivery end (CM stop)": any(sp.get("delivery") and any((m.get("package") or {}).get("cm")
                                                                              for m in sp["members"]) for sp in specs),
        "boundary dates": any(sp.get("delivery") and any(float(x.get("campaign_year", -1)) == float(sp["delivery"][0])
                                                         for x in units(sp))
                              and float(sp["delivery"][1]).is_integer()
                              and any(1 in (x.get("smc_months") or []) for x in units(sp)) for sp in specs),
        "heterogeneous members": any(len({json.dumps(x, sort_keys=True) for x in units(sp)}) > 1
                                     and len({json.dumps(m.get("params") or {}, sort_keys=True)
                                              for m in sp["members"]}) > 1 for sp in specs),
    }
    ok(sc and all(has.values()) and "scenario" in WORKER and len({c["id"] for c in sc}) == len(sc),
       f"RP10 the proof runs pinned scenarios covering {sorted(k for k, v in has.items() if v)}; missing: "
       f"{sorted(k for k, v in has.items() if not v)}")
    # Codex r2 P3: the proof records where each side ran; the re-stamp record does not claim a confirmation run
    try:
        env = environment()
        ok(all(str(env.get(k) or "").strip() for k in ("python", "numpy", "starsim", "platform"))
           and "environment()" in WORKER,
           f"RP11 each tree's worker records its Python, NumPy, Starsim and platform: {env}")
        side = lambda tag: {"identity": {"version": tag}, "version": tag, "environment": dict(env), "cases": {"x": 1}}  # noqa: E731
        ctx = {"coiled_cluster_id": "2317758", "run_id": "proof3", "workspace": "w"}
        doc = proof_doc("bf24d1b", "c0", "c1", side("0.7"), side("0.8"), [{"kind": "pack", "setting": "s"}], [11], ctx)
        ok(doc["old"]["environment"] == env and doc["new"]["environment"] == env and doc["execution"] == ctx
           and doc["identical"], "RP11b the proof names both sides' environments and the cluster and run ids")
        bare = side("0.8")
        bare.pop("environment")
        try:
            proof_doc("bf24d1b", "c0", "c1", side("0.7"), bare, [], [11], ctx)
            ok(False, "RP11c a side without its environment fails closed")
        except ValueError:
            ok(True, "RP11c a side without its environment fails closed")
    except Exception as ex:  # noqa: BLE001 -- a missing feature is a failing case
        ok(False, f"RP11 the proof records its execution environment ({type(ex).__name__}: {ex})")
    rec = _record({"engine_version": "0.7"}, proof, "abc")
    ok("confirms" not in rec["note"] and (rec.get("confirmation") or {}).get("validate_once") == "pending",
       f"RP12 the re-stamp record marks the Coiled confirmation run pending, not done: {rec.get('confirmation')}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prove", "restamp"])
    ap.add_argument("--old-ref", default="origin/v2")
    ap.add_argument("--seeds", default="11")
    ap.add_argument("--settings", default="")
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args(argv)
    if a.cmd == "restamp":
        return restamp()
    doc = prove(a.old_ref, [int(x) for x in a.seeds.split(",")], min(a.workers, 2),
                [x for x in a.settings.split(",") if x] or None)
    yaml.safe_dump(doc, open(OUT, "w"), sort_keys=False, width=110)
    bad = [k for k, r in doc["cases"].items() if not r["equal"]]
    print(f"{doc['n_cases']} cases, identical: {doc['identical']}" + (f"; differ: {bad}" if bad else ""))
    return 0 if doc["identical"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
