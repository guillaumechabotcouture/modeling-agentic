#!/usr/bin/env python3
"""Evidence dossier: a precondition of the owner's sign-off (F6, owner decision 2026-11-12).

    python3 toolkit/spec/evidence_dossier.py --self-test

Runs reached paid compute with inputs that move the decision but that nobody had checked against field data; checks
of internal consistency after the run passed. So the check is a precondition: toolkit/spec/signoff.py refuses to sign
a spec (and a run cannot reach compute) unless every decision input of every package component its options use has a
checked, sourced entry here. Decision inputs are what an engine's card declares a component's effect depends on
(toolkit/engines/decision_inputs.py): for each spec component in `options.components`, its `options.engine_map`
engine component and value, on the spec's engine (or, with `engine: auto`, on every eligible engine: the run may
choose any of them). The spec names the file (`evidence: {dossier: evidence_dossier.yaml}`, run-relative); the sheet
shows it as a table and the signature binds its sha256, so an edit after sign-off makes the signature stale.

    inputs:
      "<kind>:<name>":                  e.g. unit_attribute:resistance, parameter:use_rate, card_table:<table>
        model_value: ...                the value(s) the model uses
        model_source: "..."             where they come from
        observations:                   field or trial measurements, at least one ...
          - {source, where, year, sites, n, sampling_frame, value}     where: URL or table
        comparison: "..."               ... and how they compare with the model value on the same estimand
        no_data: {reason, borrowing_rule}    ... or, instead, "no data" said explicitly with the rule borrowed by
        chosen: {value: ... | range: [lo, hi], rule: "..."}           the value or range used, and why
        decision_test: <id>             required with a range: an analysis.decision_tests id

No field may be blank. Nothing here names a disease; the inputs come from the engine's card.
"""
from __future__ import annotations

import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from toolkit.engines import decision_inputs as DI  # noqa: E402

TOY = "toolkit.spec.toy_engines:ToyDossierEngine"
DOSSIER_FILE = "evidence_dossier.yaml"
OBS_FIELDS = ("source", "where", "year", "sites", "n", "sampling_frame", "value")
SECTION = "## Evidence dossier (inputs that drive an option's effect)"


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def dossier_reference(raw) -> str | None:
    """The run-relative dossier path the spec names (`evidence.dossier`), or None."""
    ev = raw.get("evidence") if isinstance(raw, dict) else None
    r = ev.get("dossier") if isinstance(ev, dict) else None
    return r if isinstance(r, str) and r.strip() else None


def read_dossier(run_dir: str, rel: str) -> bytes | None:
    """The dossier's bytes through a no-follow fd; None when absent; ValueError for a path outside the run or a link."""
    from lib.safe_io import LinkRefused, read_nofollow
    parts = str(rel).replace("\\", "/").split("/")
    if os.path.isabs(rel) or ".." in parts:
        raise ValueError(f"{rel} (the evidence dossier) must be a path inside the run directory")
    try:
        return read_nofollow(run_dir, rel)[0]
    except FileNotFoundError:
        return None
    except (LinkRefused, OSError) as e:
        raise ValueError(f"{rel} (the evidence dossier): {e}") from None


def sha(data: bytes | None) -> str:
    return hashlib.sha256(data).hexdigest() if data is not None else "none"


def engine_declaration(ref: str, config: dict | None = None) -> dict | None:
    """An engine's decision-input declaration: the card files in its module's directory (read without importing the
    engine), else, walking its classes, a `DECISION_INPUTS` attribute or the card files of a base class's directory
    (a subclass inherits its base's declaration); None when nothing declares one."""
    mod = str(ref).partition(":")[0]
    p = os.path.join(ROOT, *mod.split(".")) + ".py"
    if os.path.isfile(p):
        d = DI.load(os.path.dirname(p))
        if d is not None:
            return d
    from toolkit.spec import schema
    for k in schema.engine_class(ref).__mro__:
        if k.__dict__.get("DECISION_INPUTS") is not None:
            return k.__dict__["DECISION_INPUTS"]
        f = getattr(sys.modules.get(k.__module__), "__file__", None)
        d = DI.load(os.path.dirname(f)) if f else None
        if d is not None:
            return d
    return None


def _engines(raw: dict, run_dir: str | None) -> tuple[list, list]:
    eng = raw.get("engine")
    if isinstance(eng, str) and eng != "auto":
        return [(eng, {})], []
    if isinstance(eng, dict) and eng.get("ref") and eng.get("auto") is not True:
        return [(eng["ref"], eng.get("config") or {})], []
    from agents import analysis_backend as AB          # the eligibility ENGINE_CHOICE uses (read only)
    rows = AB.eligible_engines(dict(raw, **({"_run_dir": run_dir} if run_dir else {})))
    el = [(r["ref"], r.get("config") or {}) for r in rows if r.get("eligible")]
    return el, [] if el else ["engine: auto: no engine is eligible, so the decision inputs cannot be resolved"]


def required_inputs(raw: dict, run_dir: str | None = None) -> tuple[dict, list]:
    """({"<kind>:<name>": {kind, name, why, used_by}}, problems): the decision inputs the spec's options need. A problem
    is a component an engine cannot map (fail closed)."""
    opts = raw.get("options") if isinstance(raw, dict) else None
    comps = (opts or {}).get("components") if isinstance(opts, dict) else None
    emap = (opts or {}).get("engine_map") or {} if isinstance(opts, dict) else {}
    if not isinstance(comps, dict) or not comps:
        return {}, []
    engines, probs = _engines(raw, run_dir)
    req: dict = {}
    for ref, cfg in engines:
        try:
            decl = engine_declaration(ref, cfg)
        except Exception as e:  # noqa: BLE001 -- recorded: an unreadable declaration is a refusal
            probs.append(f"engine {ref}: its decision inputs cannot be read ({type(e).__name__}: {e})")
            continue
        for c in comps:
            m = emap.get(c) if isinstance(emap, dict) else None
            for k, v in (m.items() if isinstance(m, dict) else []):
                ins = DI.component_inputs(decl, k, v)
                if ins is None:
                    probs.append(f"engine {ref} declares no decision inputs for component {k} (options component "
                                 f"{c}): its effect cannot be checked (fail closed)")
                    continue
                for i in ins:
                    e = req.setdefault(f"{i['kind']}:{i['name']}", {"kind": i["kind"], "name": i["name"],
                                                                    "why": i.get("why", ""), "used_by": []})
                    tag = f"{c} ({k}={v})"
                    if tag not in e["used_by"]:
                        e["used_by"].append(tag)
    return req, probs


def _entry_problems(key: str, e, tests: set) -> list[str]:
    if _blank(e) or not isinstance(e, dict):
        return [f"entry {key} is blank: give model_value, model_source, observations or no_data, and chosen"]
    bad = [f"entry {key}: {f} is blank" for f in ("model_value", "model_source") if _blank(e.get(f))]
    obs, nd = e.get("observations"), e.get("no_data")
    if _blank(obs) == _blank(nd):
        bad.append(f"entry {key}: give exactly one of observations and no_data")
    if not _blank(obs):
        for j, o in enumerate(obs if isinstance(obs, list) else [obs]):
            bad += [f"entry {key}: observations[{j}].{f} is blank" for f in OBS_FIELDS
                    if not isinstance(o, dict) or _blank(o.get(f))]
        if _blank(e.get("comparison")):
            bad.append(f"entry {key}: comparison is blank (how the observations compare with the model value)")
    if not _blank(nd):
        bad += [f"entry {key}: no_data.{f} is blank" for f in ("reason", "borrowing_rule")
                if not isinstance(nd, dict) or _blank(nd.get(f))]
    ch = e.get("chosen")
    if not isinstance(ch, dict) or _blank(ch.get("rule")):
        bad.append(f"entry {key}: chosen.rule is blank")
        ch = ch if isinstance(ch, dict) else {}
    if _blank(ch.get("value")) == _blank(ch.get("range")):
        bad.append(f"entry {key}: chosen needs exactly one of value and range")
    elif not _blank(ch.get("range")):
        r = ch["range"]
        if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, (int, float)) for x in r) and r[0] <= r[1]):
            bad.append(f"entry {key}: chosen.range must be [lo, hi] with lo <= hi")
        t = e.get("decision_test")
        if _blank(t):
            bad.append(f"entry {key}: a range needs its decision_test (an analysis.decision_tests id)")
        elif str(t) not in tests:
            bad.append(f"entry {key}: decision_test {t} is not an analysis.decision_tests id")
    return bad


def check(raw: dict, data: bytes | None, req: dict, rel: str = DOSSIER_FILE) -> list[str]:
    """Why the dossier bytes do not cover `req` (empty: they do). Absent, unparseable or malformed: refused."""
    import yaml
    if data is None:
        return [f"{rel} (the evidence dossier) does not exist"]
    try:
        doc = yaml.safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        return [f"{rel} (the evidence dossier) does not parse: {str(e)[:200]}"]
    ins = doc.get("inputs") if isinstance(doc, dict) else None
    if not isinstance(ins, dict):
        return [f"{rel} (the evidence dossier): a mapping with `inputs: {{<kind>:<name>: entry}}`"]
    tests = {str(t.get("id")) for t in ((raw.get("analysis") or {}).get("decision_tests") or []) if isinstance(t, dict)}
    bad = []
    for key in sorted(req):
        if key not in ins:
            bad.append(f"no entry for {key} (used by {', '.join(req[key]['used_by'])})")
        else:
            bad += _entry_problems(key, ins[key], tests)
    return bad


def refusals(raw: dict, run_dir: str | None, data: bytes | None) -> list[str]:
    """Why the spec cannot be signed for want of evidence (empty: it can). `data`: the dossier bytes signing hashes."""
    if isinstance(raw, dict) and "evidence" in raw and dossier_reference(raw) is None:
        return ["evidence: name the dossier file ({dossier: evidence_dossier.yaml})"]
    req, probs = required_inputs(raw, run_dir)
    rel = dossier_reference(raw)
    if rel is None:
        if not req:
            return probs
        return probs + [f"the options need {len(req)} decision input(s) and the spec names no evidence dossier "
                        f"(evidence: {{dossier: {DOSSIER_FILE}}}): " + ", ".join(sorted(req))]
    return probs + check(raw, data, req, rel)


def _cell(v, n=160) -> str:
    s = " ".join(str(v).split()).replace("|", "\\|")
    return s if len(s) <= n else s[:n - 1] + "…"


def sheet_lines(raw: dict, run_dir: str | None, data: bytes | None) -> list[str]:
    """The sheet's dossier section: one row per decision input (model value and source, field or trial value and
    source, chosen value or range, rule and test), then what signing would refuse."""
    import yaml
    out = [SECTION]
    try:
        req, probs = required_inputs(raw, run_dir)
    except Exception as e:  # noqa: BLE001 -- the sheet still renders; signing refuses
        req, probs = {}, [f"{type(e).__name__}: {e}"]
    rel = dossier_reference(raw)
    if not req and not probs and rel is None:
        return out + ["None: no component in the spec's options has declared decision inputs.", ""]
    try:
        doc = yaml.safe_load(data.decode("utf-8")) if data is not None else None
    except (yaml.YAMLError, UnicodeDecodeError):
        doc = None
    ins = (doc.get("inputs") if isinstance(doc, dict) else None) or {}
    ins = ins if isinstance(ins, dict) else {}
    out += [f"Signing attests `{rel or 'no dossier named'}` (sha256 `{sha(data)[:16]}`).", "",
            "| Input | Used by | Model value (source) | Field / trial value (source) | Chosen | Rule; test |",
            "|---|---|---|---|---|---|"]
    for key in sorted(req):
        e = ins.get(key) if isinstance(ins.get(key), dict) else {}
        obs = e.get("observations") if isinstance(e.get("observations"), list) else []
        nd = e.get("no_data") if isinstance(e.get("no_data"), dict) else None
        field = "; ".join(f"{o.get('value')} ({o.get('source')}, {o.get('where')}, {o.get('year')}, n {o.get('n')}, "
                          f"{o.get('sampling_frame')})" for o in obs if isinstance(o, dict))
        if nd:
            field = f"no data: {nd.get('reason')}; borrowing rule: {nd.get('borrowing_rule')}"
        ch = e.get("chosen") if isinstance(e.get("chosen"), dict) else {}
        chosen = ch.get("range") if ch.get("range") is not None else ch.get("value")
        rule = f"{ch.get('rule', '')}" + (f"; {e['decision_test']}" if e.get("decision_test") else "")
        out.append(f"| {key} | {_cell(', '.join(req[key]['used_by']), 60)} | "
                   + (f"{_cell(e.get('model_value'), 60)} ({_cell(e.get('model_source'), 80)}) | {_cell(field, 240)} | "
                      f"{_cell(chosen, 40)} | {_cell(rule)} |" if e else "**MISSING** | | | |"))
    bad = refusals(raw, run_dir, data)
    out += [""] + ([f"- **REFUSED at sign-off**: {_cell(b, 400)}" for b in bad] or
                   ["Every decision input has a sourced entry."]) + [""]
    return out


def shipped_declaration_problems() -> list:
    """[(engine, problems)] for every shipped engine declaration, checked against its card (or its describe())."""
    rows = []
    for d in ("malaria", "compartmental", "renewal"):
        p = os.path.join(ROOT, "toolkit", "engines", d)
        try:
            rows.append((d, DI.problems(DI.load(p), **DI.card_vocabulary(p))))
        except Exception as e:  # noqa: BLE001
            rows.append((d, [f"{type(e).__name__}: {e}"]))
    from toolkit.engines.empirical.engine import PackageRateEngine
    c = PackageRateEngine(["a", "b"]).describe()
    p = os.path.join(ROOT, "toolkit", "engines", "empirical")
    try:
        rows.append(("empirical", DI.problems(DI.load(p), c.components, c.unit_attributes,
                                              [x.name for x in PackageRateEngine(["a", "b"]).parameters()], [])))
    except Exception as e:  # noqa: BLE001
        rows.append(("empirical", [f"{type(e).__name__}: {e}"]))
    return rows


def toy_entries() -> dict:
    """A complete dossier for the toy dossier engine's component a: one input observed, one with no data."""
    return {
        "parameter:eff_a": {
            "model_value": 0.5, "model_source": "engine card (fitted to toy benchmark T1)",
            "observations": [{"source": "Toy trial 2020", "where": "Table 2", "year": 2020, "sites": "3 toy sites",
                              "n": 1200, "sampling_frame": "cluster-randomised, children under 5", "value": 0.42}],
            "comparison": "the trial's 0.42 is below the card's 0.5, inside its fitted range [0.3, 0.7]",
            "chosen": {"range": [0.3, 0.6], "rule": "the trial's confidence interval, widened to the card's lower bound"},
            "decision_test": "DT_eff_a"},
        "unit_attribute:x": {
            "model_value": "units.csv column cov (calibrated)", "model_source": "toy surface",
            "no_data": {"reason": "no field measurement of the toy index",
                        "borrowing_rule": "the calibrated value per unit; the calibration's interval carries it"},
            "chosen": {"value": "calibrated per unit", "rule": "no direct data: the calibration is the evidence"}},
    }


def toy_dossier_spec(d: str, entries: dict | None = None, ref: str = TOY, dossier: bool = True) -> str:
    """The toy spec (toolkit/spec/schema.py toy_spec) on the toy dossier engine, with `evidence.dossier` and the dossier
    file (entries: toy_entries() by default) when `dossier`."""
    import yaml
    from toolkit.spec import schema
    p = schema.toy_spec(d)
    raw = yaml.safe_load(open(p))
    raw["engine"] = {"ref": ref}
    raw["analysis"]["decision_tests"].append({"id": "DT_eff_a", "type": "parameter_value", "parameter": "eff_a",
                                              "values": [0.3, 0.6], "text": "Does the plan change across the trial "
                                                                           "range of the effect of a?"})
    if dossier:
        raw["evidence"] = {"dossier": DOSSIER_FILE}
        with open(os.path.join(d, DOSSIER_FILE), "w") as f:
            yaml.safe_dump({"inputs": toy_entries() if entries is None else entries}, f, sort_keys=False)
    with open(p, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    return p


def _self_test_round1(ok, T) -> None:
    """#218 review round 1: each reproduction is a case."""
    import copy
    import tempfile
    import yaml
    with tempfile.TemporaryDirectory() as d:
        raw = yaml.safe_load(open(toy_dossier_spec(d)))
        req, _ = required_inputs(raw, d)
        for name, mut, want in (
                ("nested blank model value", lambda e: e["parameter:eff_a"].__setitem__("model_value", {"all": None}),
                 "model_value"),
                ("list of blanks as the source", lambda e: e["parameter:eff_a"].__setitem__("model_source", [""]),
                 "model_source"),
                ("nested blank chosen value", lambda e: e["unit_attribute:x"]["chosen"].__setitem__(
                    "value", [None, " "]), "chosen.value"),
                ("a rule that is not text", lambda e: e["unit_attribute:x"]["chosen"].__setitem__("rule", [""]),
                 "rule"),
                ("an observation value that is a list of blanks", lambda e: e["parameter:eff_a"]["observations"][0]
                 .__setitem__("value", [None]), "value"),
                ("a range whose test varies another quantity", lambda e: e["parameter:eff_a"].__setitem__(
                    "decision_test", "DT1"), "does not vary"),
                ("a range wider than its test's values", lambda e: e["parameter:eff_a"]["chosen"].__setitem__(
                    "range", [0.2, 0.6]), "does not cover"),
                ("a range on a unit attribute", lambda e: e["unit_attribute:x"].__setitem__("chosen", {
                    "range": [0, 1], "rule": "r"}) or e["unit_attribute:x"].__setitem__("decision_test", "DT1"),
                 "unit_attribute:x")):
            e = copy.deepcopy(toy_entries())
            mut(e)
            got = check(raw, yaml.safe_dump({"inputs": e}).encode(), req)
            ok(any(want in g for g in got), f"ED-C4 {name} is refused, naming {want!r}: {got[:3]}")
        for name, decl, want in (("a component declared {}", {"a": {}, "b": {"inputs": [], "none_reason": "r"}},
                                  "none_reason"),
                                 ("a non-mapping input", {"a": {"inputs": ["eff_a"]}, "b": {"inputs": [],
                                                                                           "none_reason": "r"}},
                                  "each input")):
            full = T.ToyDossierEngine.DECISION_INPUTS
            T.ToyDossierEngine.DECISION_INPUTS = decl
            try:
                req2, probs = required_inputs(raw, d)
            finally:
                T.ToyDossierEngine.DECISION_INPUTS = full
            ok(any(want in p for p in probs), f"ED-R7 a malformed declaration ({name}) fails closed at sign time: {probs}")
        for name, emap in (("missing", {"b": {"b": 1}}), ("empty", {"a": {}, "b": {"b": 1}}),
                           ("not a mapping", {"a": "a=1", "b": {"b": 1}})):
            r2 = copy.deepcopy(raw)
            r2["options"]["engine_map"] = emap
            _, probs = required_inputs(r2, d)
            ok(any("engine mapping" in p and "a" in p for p in probs),
               f"ED-R8 a used component whose engine_map entry is {name} is refused: {probs}")
        # ED-R10: engine: auto takes the backend's full eligibility (what ENGINE_CHOICE uses), not the card filter
        from agents import analysis_backend as AB
        seen = {}

        class Stub:
            def eligible_engines(self, spec, engines=None):
                seen["run_dir"] = spec.get("_run_dir")
                return [{"ref": TOY, "eligible": True, "config": {}},
                        {"ref": "toolkit.engines.malaria.engine:MalariaEngine", "eligible": False}]
        orig = AB.backend
        AB.backend = lambda *a, **k: Stub()
        try:
            r3 = dict(raw, engine={"auto": True, "candidates": [TOY, "toolkit.engines.malaria.engine:MalariaEngine"]})
            req3, probs3 = required_inputs(r3, d)
        finally:
            AB.backend = orig
        ok(sorted(req3) == ["parameter:eff_a", "unit_attribute:x"] and seen.get("run_dir") == d and probs3 == [],
           f"ED-R10 engine: auto requires the inputs of the engines the backend finds eligible, with the run dir: "
           f"{sorted(req3)} {seen}")
        # ED-S1: ENGINE_CHOICE re-checks the signed dossier against the engine actually chosen
        r4 = dict(raw, engine={"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]})
        r4.pop("evidence", None)
        ok(any("parameter:eff_a" in p for p in choice_refusals(d, r4, TOY, {}))
           and choice_refusals(d, r4, "toolkit.engines.contract:ToyEngine", {}) == [],
           "ED-S1 the dossier is re-checked against the chosen engine: an engine whose inputs it does not cover is "
           "refused")
        # ED-B1: one check-and-bind for every signing path
        b = bind(d, open(os.path.join(d, "decision_spec.yaml"), "rb").read())
        ok(b["files"] == {DOSSIER_FILE: sha(open(os.path.join(d, DOSSIER_FILE), "rb").read())}
           and b["record"]["required"] == ["parameter:eff_a", "unit_attribute:x"]
           and b["record"]["dossier"] == DOSSIER_FILE, f"ED-B1 bind returns the hashes and the evidence record: {b}")
        os.remove(os.path.join(d, DOSSIER_FILE))
        try:
            bind(d, open(os.path.join(d, "decision_spec.yaml"), "rb").read())
            ok(False, "ED-B2 bind refuses a spec it cannot bind")
        except ValueError as e:
            ok("does not exist" in str(e), f"ED-B2 bind refuses a spec it cannot bind: {e}")
    # ED-R9: a configured engine's inputs follow its configuration
    comp = {"engine": {"ref": "toolkit.engines.compartmental.engine:CompartmentalEngine", "config": {
        "groups": ["g1", "g2"], "structure": "age_groups_contact_matrix", "transmission": "q",
        "per_group_parameters": ["s0"], "components": {"camp": {"kind": "campaign", "targets": ["g1"],
                                                                 "reach": 0.8}}}},
        "options": {"components": {"c": {}}, "engine_map": {"c": {"camp": 1}}}}
    req, probs = required_inputs(comp)
    ok({"parameter:q", "parameter:s0:g1", "parameter:s0:g2", "parameter:efficacy:camp", "unit_attribute:reach:camp"}
       <= set(req) and "parameter:r0" not in req and "unit_attribute:s0" not in req and probs == [],
       f"ED-R9 compartmental: q (not r0), s0 per group, the campaign's efficacy and reach: {sorted(req)} {probs}")
    # ED-E3: every card parameter and unit attribute of each shipped card is declared or excluded with a reason
    for d_ in ("malaria", "compartmental", "renewal"):
        p = os.path.join(ROOT, "toolkit", "engines", d_)
        try:
            v = DI.card_vocabulary(p)
            cov = DI.coverage_problems(DI.load(p), DI.load_excluded(p),
                                       {"parameter": v["parameters"], "unit_attribute": list(v["unit_attributes"])})
        except Exception as e:  # noqa: BLE001
            cov = [f"{type(e).__name__}: {e}"]
        ok(cov == [], f"ED-E3 {d_}: every card parameter and unit attribute is an input or excluded with a reason: "
                      f"{cov}")
    mal = DI.load(os.path.join(ROOT, "toolkit", "engines", "malaria"))
    names = {c: {i["name"] for i in (v.get("inputs") or []) + [x for xs in (v.get("by_value") or {}).values()
                                                               for x in xs]} for c, v in mal.items()}
    want = {"smc": {"smc_rho", "smc_k"}, "net": {"cfp_half"}, "irs": {"irs_rep", "irs_divert", "irs_half"},
            "cm": {"cm_ramp_years", "cm_target", "cm_age_lo_m", "cm_age_hi_m"}}
    ok(all(w <= names.get(c, set()) for c, w in want.items()),
       f"ED-E4 the malaria inputs the review found omitted are declared: {[(c, sorted(w - names.get(c, set()))) for c, w in want.items()]}")


def _self_test() -> int:
    import copy
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    from toolkit.spec import toy_engines as T
    T.enable_for_test()
    try:
        with tempfile.TemporaryDirectory() as d:
            raw = yaml.safe_load(open(toy_dossier_spec(d)))
            req, probs = required_inputs(raw, d)
            ok(sorted(req) == ["parameter:eff_a", "unit_attribute:x"] and probs == []
               and any("a" in u for u in req["parameter:eff_a"]["used_by"]),
               f"ED-R1 the spec's options need the inputs the engine declares for component a ({sorted(req)}, {probs})")
            ok(check(raw, open(os.path.join(d, DOSSIER_FILE), "rb").read(), req) == [],
               "ED-C1 a complete dossier (one input observed, one 'no data' with its borrowing rule) passes")
            for name, mut, want in (
                    ("uncovered", lambda e: e.pop("unit_attribute:x"), "unit_attribute:x"),
                    ("blank entry", lambda e: e.__setitem__("unit_attribute:x", None), "unit_attribute:x"),
                    ("blank observation field", lambda e: e["parameter:eff_a"]["observations"][0].__setitem__(
                        "sampling_frame", " "), "sampling_frame"),
                    ("blank rule", lambda e: e["unit_attribute:x"]["chosen"].__setitem__("rule", ""), "rule"),
                    ("no data without a borrowing rule", lambda e: e["unit_attribute:x"]["no_data"].pop(
                        "borrowing_rule"), "borrowing_rule"),
                    ("both observations and no data", lambda e: e["unit_attribute:x"].__setitem__(
                        "observations", e["parameter:eff_a"]["observations"]), "exactly one"),
                    ("observations and no comparison", lambda e: e["parameter:eff_a"].pop("comparison"), "comparison"),
                    ("range without a decision test", lambda e: e["parameter:eff_a"].pop("decision_test"),
                     "decision_test"),
                    ("range with an unknown decision test", lambda e: e["parameter:eff_a"].__setitem__(
                        "decision_test", "DT9"), "DT9")):
                e = copy.deepcopy(toy_entries())
                mut(e)
                got = check(raw, yaml.safe_dump({"inputs": e}).encode(), req)
                ok(any(want in g for g in got), f"ED-C2 {name} is refused, naming {want!r}: {got[:3]}")
            ok(check(raw, b"inputs: [unclosed", req) and check(raw, None, req)
               and check(raw, b"- a list\n", req),
               "ED-C3 an unparseable, absent or malformed dossier is refused (fail closed)")
        with tempfile.TemporaryDirectory() as d:
            raw = yaml.safe_load(open(toy_dossier_spec(d, ref="toolkit.engines.contract:ToyEngine", dossier=False)))
            ok(required_inputs(raw, d) == ({}, []),
               "ED-R2 a spec whose options use no component with decision inputs needs none (and no dossier)")
            raw["engine"] = {"auto": True, "candidates": [TOY]}
            req, probs = required_inputs(raw, d)
            ok(sorted(req) == ["parameter:eff_a", "unit_attribute:x"],
               f"ED-R3 engine: auto takes the inputs of every eligible candidate engine ({sorted(req)}, {probs})")
            raw["engine"] = {"ref": TOY}
            full = T.ToyDossierEngine.DECISION_INPUTS
            T.ToyDossierEngine.DECISION_INPUTS = {"a": full["a"]}
            try:
                req, probs = required_inputs(raw, d)
            finally:
                T.ToyDossierEngine.DECISION_INPUTS = full
            ok(sorted(req) == ["parameter:eff_a", "unit_attribute:x"] and len(probs) == 1
               and "declares no decision inputs for component b" in probs[0],
               f"ED-R4 an engine that declares no decision inputs for a mapped component fails closed: {probs}")
            raw["engine"] = {"ref": "toolkit.spec.toy_engines:ToySpecRateEngine", "config": {"components": ["a", "b"]}}
            req, probs = required_inputs(raw, d)
            base = engine_declaration("toolkit.engines.empirical.engine:PackageRateEngine")
            ok(req == {} and probs == [] and "*" in base
               and engine_declaration(raw["engine"]["ref"], raw["engine"]["config"]) == base,
               f"ED-R5 a subclass inherits its base engine's declaration (the package-rate engine's `*`: effects "
               f"calibrated to the run's own panel, no outside input): {sorted(req)} {probs}")
        _self_test_round1(ok, T)
        mal = {"engine": {"ref": "toolkit.engines.malaria.engine:MalariaEngine"},
               "options": {"components": {"n": {}, "s": {}}, "engine_map": {"n": {"net": "pbo"}, "s": {"smc": 0}}}}
        req, probs = required_inputs(mal)
        mal["options"]["engine_map"]["n"]["net"] = "dual"
        req2, _ = required_inputs(mal)
        ok({"unit_attribute:resistance", "unit_attribute:species", "card_table:hut_trial_coefficients"} <= set(req)
           and not any("smc" in k for k in req) and "parameter:cfp_kill" not in req and "parameter:cfp_kill" in req2
           and probs == [] and "toolkit.engines.malaria.engine" not in sys.modules,
           f"ED-R6 a net component needs the resistance and species inputs (and, for its dual value, the fitted kill); "
           f"an inactive component needs none; read from the card's YAML without importing the engine: {sorted(req)}")
        # every shipped engine's declaration is valid against its card
        for row in shipped_declaration_problems():
            ok(not row[1], f"ED-E1 {row[0]}: its decision inputs validate against its card {row[1][:3]}")
        ok(len(shipped_declaration_problems()) == 4, "ED-E2 the malaria, compartmental, renewal and package-rate "
                                                     "engines declare decision inputs")
    except NameError as e:
        ok(False, f"evidence dossier: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
