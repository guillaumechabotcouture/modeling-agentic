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
        decision_test: <id>             required with a range: a test that varies this input over the range (a
                                        parameter_value test of the parameter whose values cover it, or the
                                        generated prior_range_<parameter> of a drawn prior); no test type varies a
                                        unit attribute or a card table, so those take a value

No field may be blank at any depth, and text fields must be text. The inputs the options need decide whether a
dossier is required, not whether the spec names one: signing refuses either way, and a sign-off made before this
requirement of a spec that needs one is `unsigned_evidence` (signoff.status), refused at launch. Every signing path
(signoff.py, decision_brief.py, stage_machine.py) calls bind(); ENGINE_CHOICE re-checks the engine it picks
(choice_refusals). Nothing here names a disease; the inputs come from the engine's card.
"""
from __future__ import annotations

import hashlib
import math
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
    """Blank at any depth: None, empty or whitespace text, an empty collection, a collection holding a blank, a
    non-finite number, or a value of another type (only text, numbers, dates, lists and mappings carry evidence)."""
    import datetime
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return not math.isfinite(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return False
    if isinstance(v, dict):
        return not v or any(_blank(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return not v or any(_blank(x) for x in v)
    return True


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


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


def _engine_dir(ref: str) -> str | None:
    p = os.path.join(ROOT, *str(ref).partition(":")[0].split(".")) + ".py"
    return os.path.dirname(p) if os.path.isfile(p) else None


def engine_declaration(ref: str, config: dict | None = None) -> dict | None:
    """An engine's decision-input declaration: the card files in its module's directory (read without importing the
    engine), else, walking its classes, a `DECISION_INPUTS` attribute or the card files of a base class's directory
    (a subclass inherits its base's declaration); None when nothing declares one."""
    d0 = _engine_dir(ref)
    if d0:
        d = DI.load(d0)
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


def _vocab(ref: str, config: dict) -> dict:
    """The configured engine's parameter and unit-attribute names (its card as this configuration builds it)."""
    from toolkit.spec import schema
    e = schema.engine_class(ref)(**(config or {}))
    return {"parameter": [p.name for p in e.parameters()], "unit_attribute": list(e.describe().unit_attributes)}


def _engines(raw: dict, run_dir: str | None) -> tuple[list, list]:
    eng = raw.get("engine")
    if isinstance(eng, str) and eng != "auto":
        return [(eng, {})], []
    if isinstance(eng, dict) and eng.get("ref") and eng.get("auto") is not True:
        return [(eng["ref"], eng.get("config") or {})], []
    # engine: auto: every engine the run could choose, by the eligibility ENGINE_CHOICE itself uses (the backend's,
    # with its engine needs and, given the run dir, the full validation per candidate); read only
    from agents import analysis_backend as AB
    b = AB.backend()
    try:
        rows = b.eligible_engines(dict(raw, **({"_run_dir": run_dir} if run_dir else {})))
    except Exception:  # noqa: BLE001 -- the full per-candidate check cannot run here (e.g. a run dir in another
        # layout): the card-level filter alone, a superset of the eligible engines, so more inputs are required
        try:
            rows = b.eligible_engines(dict(raw))
        except Exception as e:  # noqa: BLE001
            return [], [f"engine: auto: the eligible engines cannot be determined ({type(e).__name__}: {e})"]
    el = [(r["ref"], r.get("config") or {}) for r in rows if r.get("eligible")]
    why = "; ".join(f"{r.get('ref')}: {'; '.join(str(w) for w in (r.get('why_not') or [])[:2])}" for r in rows)[:600]
    return el, [] if el else [f"engine: auto: no engine is eligible, so the decision inputs cannot be resolved ({why})"]


def required_inputs(raw: dict, run_dir: str | None = None) -> tuple[dict, list]:
    """({"<kind>:<name>": {kind, name, why, used_by, prior_range}}, problems): the decision inputs the spec's options
    need on every engine that may run them. Problems (each a refusal): a used component with no engine mapping, an
    engine whose declaration is missing or malformed for a mapped component, an input its configured card lacks."""
    opts = raw.get("options") if isinstance(raw, dict) else None
    comps = opts.get("components") if isinstance(opts, dict) else None
    emap = opts.get("engine_map") if isinstance(opts, dict) else None
    if not isinstance(comps, dict) or not comps:
        return {}, []
    emap = emap if isinstance(emap, dict) else {}
    probs = [f"options component {c} has no engine mapping (options.engine_map.{c} is missing, empty or not a "
             "mapping): its decision inputs cannot be resolved" for c in comps
             if not isinstance(emap.get(c), dict) or not emap.get(c)]
    engines, more = _engines(raw, run_dir)
    probs += more
    req: dict = {}
    for ref, cfg in engines:
        try:
            decl = engine_declaration(ref, cfg)
            bad = DI.problems(decl) if decl is not None else []
        except Exception as e:  # noqa: BLE001 -- recorded: an unreadable declaration is a refusal
            probs.append(f"engine {ref}: its decision inputs cannot be read ({type(e).__name__}: {e})")
            continue
        if bad:
            probs.append(f"engine {ref}: its decision inputs are malformed: " + "; ".join(bad[:5]))
            continue
        try:
            oi = engine_outcome_inputs(ref)
            bad = DI.outcome_problems(oi)
        except Exception as e:  # noqa: BLE001
            oi, bad = None, [f"{type(e).__name__}: {e}"]
        if bad:
            probs.append(f"engine {ref}: its outcome inputs are malformed: " + "; ".join(bad[:5]))
            continue
        cache = {"vocab": None}

        def add(ins, tag, where):
            try:
                if cache["vocab"] is None and any(i.get("if_on_card") or "*" in i["name"] for i in ins):
                    cache["vocab"] = _vocab(ref, cfg)
                ins = DI.resolve(ins, cache["vocab"])
            except Exception as e:  # noqa: BLE001 -- an input the configured card cannot resolve is a refusal
                probs.append(f"engine {ref}, {where}: {type(e).__name__}: {e}")
                return
            for i in ins:
                e = req.setdefault(f"{i['kind']}:{i['name']}", {"kind": i["kind"], "name": i["name"],
                                                                "why": i.get("why", ""), "used_by": [], "engines": []})
                if tag not in e["used_by"]:
                    e["used_by"].append(tag)
                if (ref, cfg) not in e["engines"]:
                    e["engines"].append((ref, cfg))
        active = False
        for c in comps:
            m = emap.get(c)
            for k, v in (m.items() if isinstance(m, dict) else []):
                ins = DI.component_inputs(decl, k, v)
                if ins is None:
                    probs.append(f"engine {ref} declares no decision inputs for component {k} (options component "
                                 f"{c}): its effect cannot be checked (fail closed)")
                    continue
                active = active or DI.is_active(decl, k, v)
                add(ins, f"{c} ({k}={v})", f"component {k}")
        if active:                      # what every arm uses to compute the decision's outcome, whatever the components
            outs = _decision_outcomes(raw)
            add(DI.outcome_inputs(oi, outs), "outcome " + ", ".join(outs), "outcome inputs")
    return req, probs


def _decision_outcomes(raw: dict) -> list:
    """The outcomes the decision turns on: the primary objective outcome and the headline's."""
    o = [((raw.get("objective") or {}).get("outcome")),
         (((raw.get("deliverable") or {}).get("headline") or {}).get("outcome"))]
    return [x for i, x in enumerate(o) if isinstance(x, str) and x and x not in o[:i]]


def engine_outcome_inputs(ref: str):
    """An engine's `outcome_inputs` (decision_inputs.yaml beside its card, else a DECISION_OUTCOME_INPUTS attribute or
    a base class's file); None when none is declared."""
    d0 = _engine_dir(ref)
    if d0 and DI.load(d0) is not None:
        return DI.load_outcome_inputs(d0)
    from toolkit.spec import schema
    for k in schema.engine_class(ref).__mro__:
        if k.__dict__.get("DECISION_OUTCOME_INPUTS") is not None:
            return k.__dict__["DECISION_OUTCOME_INPUTS"]
        if k.__dict__.get("DECISION_INPUTS") is not None:
            return None
        f = getattr(sys.modules.get(k.__module__), "__file__", None)
        if f and DI.load(os.path.dirname(f)) is not None:
            return DI.load_outcome_inputs(os.path.dirname(f))
    return None


_DRAWN: dict = {}


def _drawn_ranges(raw: dict, run_dir: str | None, ref: str, cfg: dict) -> dict:
    """{parameter: (lo, hi)} the draw plan draws for this spec on this engine (toolkit/spec/priors.py drawn: card
    and spec priors, ranges on engine_parameters values, minus exclusions); {} when it cannot be computed."""
    import json
    key = json.dumps([raw.get("uncertainty"), raw.get("engine_parameters"), raw.get("decision_units"), ref, cfg],
                     sort_keys=True, default=str)
    if key not in _DRAWN:
        try:
            from toolkit.spec import priors as PRI, schema
            s = schema.Spec(dict(raw, engine={"ref": ref, **({"config": dict(cfg)} if cfg else {})}),
                            run_dir or ROOT, None)
            _DRAWN[key] = {n: (float(p.lo), float(p.hi)) for n, p in PRI.drawn(s).items()}
        except Exception:  # noqa: BLE001 -- not computable: nothing is drawn (fail closed)
            _DRAWN[key] = {}
    return _DRAWN[key]


def _test_problems(key: str, r: dict, rng: list, tid: str, tests: dict, raw: dict,
                   run_dir: str | None = None) -> list[str]:
    """Why decision test `tid` does not vary input `key` over the chosen range `rng` (empty: it does)."""
    if r["kind"] != "parameter":
        return [f"entry {key}: no decision test type varies a {r['kind']} (parameter_value varies engine parameters): "
                "choose a value with its rule, and declare the range in the spec as a limitation"]
    name = r["name"]
    if tid == f"prior_range_{name}":
        # generated only for a prior the draw plan draws (an excluded prior gets no range test): every engine that
        # may run the spec must draw it, over a range covering the chosen one
        engines = r.get("engines") or []
        got = [_drawn_ranges(raw, run_dir, ref, cfg).get(name) for ref, cfg in engines]
        if not engines or any(g is None for g in got):
            return [f"entry {key}: {tid} is generated only for a prior the draw plan draws, and {name} is not drawn "
                    "(no prior, or excluded by uncertainty.excluded)"]
        vals = [max(g[0] for g in got), min(g[1] for g in got)]
    else:
        t = tests.get(tid)
        if t is None:
            return [f"entry {key}: decision_test {tid} is not an analysis.decision_tests id"]
        if t.get("type") != "parameter_value" or t.get("parameter") != name:
            return [f"entry {key}: decision_test {tid} does not vary parameter {name} (a parameter_value test of "
                    f"{name}, or prior_range_{name} for a drawn prior, does)"]
        vals = t.get("values") or []
    nums = [x for x in vals if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if not nums or min(nums) > rng[0] or max(nums) < rng[1]:
        return [f"entry {key}: decision_test {tid}'s values {vals} do not cover the chosen range {rng}"]
    return []


def _entry_problems(key: str, e, r: dict, tests: dict, raw: dict, run_dir: str | None = None) -> list[str]:
    if not isinstance(e, dict) or not e:
        return [f"entry {key} is blank: give model_value, model_source, observations or no_data, and chosen"]
    bad = [f"entry {key}: model_value is blank" for _ in [0] if _blank(e.get("model_value"))]
    bad += [f"entry {key}: model_source is blank or not text" for _ in [0] if not _text(e.get("model_source"))]
    obs, nd = e.get("observations"), e.get("no_data")
    has_obs, has_nd = obs not in (None, [], {}), nd not in (None, {})
    if has_obs == has_nd:
        bad.append(f"entry {key}: give exactly one of observations and no_data")
    if has_obs:
        for j, o in enumerate(obs if isinstance(obs, list) else [obs]):
            if not isinstance(o, dict):
                bad.append(f"entry {key}: observations[{j}] is not a mapping")
                continue
            bad += [f"entry {key}: observations[{j}].{f} is blank or not text" for f in ("source", "where",
                                                                                         "sampling_frame")
                    if not _text(o.get(f))]
            bad += [f"entry {key}: observations[{j}].{f} is blank" for f in ("year", "sites", "n", "value")
                    if _blank(o.get(f))]
        if not _text(e.get("comparison")):
            bad.append(f"entry {key}: comparison is blank (how the observations compare with the model value)")
    if has_nd:
        bad += [f"entry {key}: no_data.{f} is blank or not text" for f in ("reason", "borrowing_rule")
                if not isinstance(nd, dict) or not _text(nd.get(f))]
    ch = e.get("chosen") if isinstance(e.get("chosen"), dict) else {}
    if not _text(ch.get("rule")):
        bad.append(f"entry {key}: chosen.rule is blank or not text")
    has_v, has_r = "value" in ch and ch["value"] is not None, "range" in ch and ch["range"] is not None
    if has_v == has_r:
        bad.append(f"entry {key}: chosen needs exactly one of value and range")
    elif has_v and _blank(ch["value"]):
        bad.append(f"entry {key}: chosen.value is blank")
    elif has_r:
        rg = ch["range"]
        if not (isinstance(rg, list) and len(rg) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                              and math.isfinite(x) for x in rg) and rg[0] <= rg[1]):
            bad.append(f"entry {key}: chosen.range must be [lo, hi] with lo <= hi")
        elif not _text(e.get("decision_test")):
            bad.append(f"entry {key}: a range needs its decision_test (one that varies this input over the range)")
        else:
            bad += _test_problems(key, r, rg, e["decision_test"].strip(), tests, raw, run_dir)
    return bad + _value_problems(key, e, r, ch, raw)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _executable(r: dict, raw: dict):
    """(the value the spec itself runs for this input, where it is set), or (None, None): an engine_parameters value
    for a parameter, a value binding (decision_units.engine_attributes {value}) for a unit attribute."""
    if r["kind"] == "parameter":
        e = ((raw.get("engine_parameters") or {}) if isinstance(raw.get("engine_parameters"), dict) else {}).get(r["name"])
        where = f"engine_parameters.{r['name']}.value"
    elif r["kind"] == "unit_attribute":
        ea = (raw.get("decision_units") or {}).get("engine_attributes") if isinstance(raw.get("decision_units"), dict) \
            else None
        e = ea.get(r["name"]) if isinstance(ea, dict) else None
        where = f"decision_units.engine_attributes.{r['name']}.value"
    else:
        return None, None
    v = e.get("value") if isinstance(e, dict) else None
    return (v, where) if _num(v) else (None, None)


def _value_problems(key: str, e: dict, r: dict, ch: dict, raw: dict) -> list[str]:
    """The dossier's model and chosen values reconcile with the value the spec runs: a number the spec sets must be
    the model value and the chosen value, or lie in the chosen range (#218 review round 2)."""
    x, where = _executable(r, raw)
    if x is None:
        return []
    bad = []
    mv = e.get("model_value")
    if _num(mv) and not math.isclose(mv, x, rel_tol=1e-9, abs_tol=1e-12):
        bad.append(f"entry {key}: model_value {mv} is not the value the spec runs ({where} = {x})")
    if ch.get("range") is not None:
        rg = ch["range"]
        if isinstance(rg, list) and len(rg) == 2 and all(_num(y) for y in rg) and not rg[0] <= x <= rg[1]:
            bad.append(f"entry {key}: the value the spec runs ({where} = {x}) is outside the chosen range {rg}")
    elif ch.get("value") is not None and not (_num(ch["value"]) and math.isclose(ch["value"], x, rel_tol=1e-9,
                                                                               abs_tol=1e-12)):
        bad.append(f"entry {key}: chosen.value {ch['value']!r} is not the value the spec runs ({where} = {x})")
    return bad


def check(raw: dict, data: bytes | None, req: dict, rel: str = DOSSIER_FILE, run_dir: str | None = None) -> list[str]:
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
    tests = {str(t.get("id")): t for t in ((raw.get("analysis") or {}).get("decision_tests") or [])
             if isinstance(t, dict)}
    bad = []
    for key in sorted(req):
        if key not in ins:
            bad.append(f"no entry for {key} (used by {', '.join(req[key]['used_by'])})")
        else:
            bad += _entry_problems(key, ins[key], req[key], tests, raw, run_dir)
    return bad


def _refusals(raw: dict, run_dir: str | None, data: bytes | None) -> tuple[list, dict]:
    if isinstance(raw, dict) and "evidence" in raw and dossier_reference(raw) is None:
        return ["evidence: name the dossier file ({dossier: evidence_dossier.yaml})"], {}
    req, probs = required_inputs(raw, run_dir)
    rel = dossier_reference(raw)
    if rel is None:
        if not req:
            return probs, req
        return probs + [f"the options need {len(req)} decision input(s) and the spec names no evidence dossier "
                        f"(evidence: {{dossier: {DOSSIER_FILE}}}): " + ", ".join(sorted(req))], req
    return probs + check(raw, data, req, rel, run_dir), req


def refusals(raw: dict, run_dir: str | None, data: bytes | None) -> list[str]:
    """Why the spec cannot be signed for want of evidence (empty: it can). The inputs the options need decide, whether
    or not the spec names a dossier. `data`: the dossier bytes signing hashes."""
    return _refusals(raw, run_dir, data)[0]


def capture(run_dir: str, spec_bytes: bytes) -> tuple:
    """(dossier path the spec bytes name or None, its bytes or None), read once through a no-follow fd."""
    import yaml
    try:
        raw = yaml.safe_load(spec_bytes.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"decision_spec.yaml does not parse: {e}") from None
    rel = dossier_reference(raw if isinstance(raw, dict) else {})
    return rel, (read_dossier(run_dir, rel) if rel else None)


def bind(run_dir: str, spec_bytes: bytes, captured: tuple | None = None) -> dict:
    """The one check-and-bind every signing path calls (toolkit/spec/signoff.py, scripts/decision_brief.py,
    agents/stage_machine.py). Reads the dossier once (or takes `captured` from capture() on the same spec bytes);
    ValueError when the spec cannot be signed for want of evidence; else {"files": {dossier: sha256} (empty when none
    is named), "record": {required, dossier, sha256}} to record."""
    import yaml
    try:
        raw = yaml.safe_load(spec_bytes.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"decision_spec.yaml does not parse: {e}") from None
    raw = raw if isinstance(raw, dict) else {}
    rel, data = captured if captured is not None else capture(run_dir, spec_bytes)
    bad, req = _refusals(raw, run_dir, data)
    if bad:
        raise ValueError("spec not signable (evidence dossier): " + "; ".join(bad[:12])
                         + (f"; and {len(bad) - 12} more" if len(bad) > 12 else ""))
    h = sha(data) if rel else None
    return {"files": {rel: h} if rel else {}, "record": {"required": sorted(req), "dossier": rel, "sha256": h}}


def evidence_state(run_dir: str, raw: dict, rec: dict) -> str:
    """ok | changed | unsigned_evidence for a sign-off record. A record that bound the evidence: changed when the
    dossier's bytes differ. A record made before the dossier requirement: unsigned_evidence when the spec's options
    need decision inputs (it must be re-signed), ok when they need none; fail closed on an error."""
    ev = rec.get("evidence") if isinstance(rec, dict) else None
    if isinstance(ev, dict):
        rel = ev.get("dossier")
        if not rel:
            return "ok" if dossier_reference(raw) is None else "changed"
        try:
            now = read_dossier(run_dir, rel)
        except ValueError:
            return "changed"
        signed = (rec.get("sha256") or {}).get(rel) or (rec.get("evidence_sha256") or {}).get(rel) or ev.get("sha256")
        return "ok" if now is not None and signed == sha(now) else "changed"
    try:
        req, probs = required_inputs(raw, run_dir)
    except Exception:  # noqa: BLE001
        return "unsigned_evidence"
    return "unsigned_evidence" if req or probs else "ok"


def choice_refusals(run_dir: str, raw: dict, ref: str, config: dict | None) -> list[str]:
    """ENGINE_CHOICE's re-check: why the signed dossier does not cover the engine actually chosen (empty: it does)."""
    rel = dossier_reference(raw)
    try:
        data = read_dossier(run_dir, rel) if rel else None
    except ValueError as e:
        return [str(e)]
    return refusals(dict(raw, engine={"ref": ref, "config": dict(config or {})}), run_dir, data)


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


def _self_test_round2(ok, T) -> None:
    """#218 review round 2: the draw plan, the executable value, outcome-conversion inputs."""
    import copy
    import tempfile
    import yaml
    kdecl = {"a": {"inactive": [0], "inputs": [{"kind": "parameter", "name": "k", "why": "the toy transmission slope"}]},
             "b": {"inputs": [], "none_reason": "toy"}}
    kentry = {"model_value": 0.8, "model_source": "engine card",
              "no_data": {"reason": "toy", "borrowing_rule": "the toy range"},
              "chosen": {"range": [0.7, 0.9], "rule": "the toy range"}, "decision_test": "prior_range_k"}

    def verdict(raw, entry, d):
        full = T.ToyDossierEngine.DECISION_INPUTS
        T.ToyDossierEngine.DECISION_INPUTS = kdecl
        try:
            req, probs = required_inputs(raw, d)
            return probs + check(raw, yaml.safe_dump({"inputs": {"parameter:k": entry}}).encode(), req)
        finally:
            T.ToyDossierEngine.DECISION_INPUTS = full
    with tempfile.TemporaryDirectory() as d:
        raw = yaml.safe_load(open(toy_dossier_spec(d)))
        drawn = copy.deepcopy(raw)
        drawn["uncertainty"]["priors"] = {"k": {"dist": "uniform", "range": [0.6, 1.0], "source": "toy"}}
        excl = copy.deepcopy(drawn)
        excl["uncertainty"]["excluded"].append({"parameter": "k", "reason": "held fixed"})
        sval = copy.deepcopy(raw)
        sval["engine_parameters"] = {"k": {"value": 0.8, "range": [0.6, 1.0], "dist": "uniform", "source": "toy",
                                           "reason": "toy", "basis": "setting_data"}}
        ok(verdict(drawn, kentry, d) == [] and any("prior_range_k" in p for p in verdict(excl, kentry, d))
           and verdict(sval, kentry, d) == [],
           f"ED-T2 prior_range_<p> holds only when the draw plan draws p: a drawn prior passes, one excluded by "
           f"uncertainty.excluded is refused, a range on its engine_parameters value passes "
           f"({verdict(drawn, kentry, d)}, {verdict(excl, kentry, d)}, {verdict(sval, kentry, d)})")
        fixed = copy.deepcopy(raw)
        fixed["engine_parameters"] = {"k": {"value": 0.9, "source": "toy", "reason": "toy", "basis": "setting_data"}}
        e1 = dict(kentry, model_value=0.1, chosen={"value": 0.1, "rule": "r"})
        e1.pop("decision_test")
        e2 = dict(e1, model_value=0.9, chosen={"value": 0.9, "rule": "r"})
        ok(any("0.9" in p for p in verdict(fixed, e1, d)) and verdict(fixed, e2, d) == [],
           f"ED-V1 the chosen and model values reconcile with the value the spec runs (engine_parameters 0.9 vs 0.1 "
           f"refused; 0.9 passes): {verdict(fixed, e1, d)} {verdict(fixed, e2, d)}")
        ua = copy.deepcopy(raw)
        ua["decision_units"]["engine_attributes"] = {"x": {"value": 0.5, "source": "toy"}}
        e3 = copy.deepcopy(toy_entries())
        e3["unit_attribute:x"]["model_value"] = 0.2
        e3["unit_attribute:x"]["chosen"] = {"value": 0.2, "rule": "r"}
        req, _ = required_inputs(ua, d)
        got = check(ua, yaml.safe_dump({"inputs": e3}).encode(), req)
        ok(any("0.5" in p and "unit_attribute:x" in p for p in got),
           f"ED-V2 ... and a unit attribute bound to a value: {got}")
    # ED-R11: compartmental outcome ratios when the configured outcome uses them
    base_cfg = {"groups": ["g1", "g2"], "structure": "age_groups_contact_matrix", "outcomes": ["deaths"],
                "components": {"camp": {"kind": "campaign", "targets": ["g1"], "reach": 0.8}}}
    for cfg, want in ((base_cfg, {"unit_attribute:ratio:deaths"}),
                      (dict(base_cfg, per_group_parameters=["ratio:deaths"]),
                       {"parameter:ratio:deaths:g1", "parameter:ratio:deaths:g2"})):
        comp = {"engine": {"ref": "toolkit.engines.compartmental.engine:CompartmentalEngine", "config": cfg},
                "objective": {"outcome": "deaths"},
                "options": {"components": {"c": {}}, "engine_map": {"c": {"camp": 1}}}}
        req, probs = required_inputs(comp)
        ok(want <= set(req) and probs == [], f"ED-R11 compartmental outcome deaths needs its ratio inputs {sorted(want)}: "
                                             f"{sorted(req)} {probs}")
    # ED-R12: malaria outcome-conversion inputs follow the primary outcome, whatever the components
    mal = {"engine": {"ref": "toolkit.engines.malaria.engine:MalariaEngine"}, "objective": {"outcome": "dalys"},
           "options": {"components": {"n": {}}, "engine_map": {"n": {"net": "pbo"}}}}
    req, probs = required_inputs(mal)
    mal["objective"]["outcome"] = "cases"
    req2, _ = required_inputs(mal)
    conv = {"unit_attribute:cm_ft", "parameter:severe_access_add", "parameter:cfr_severe_treated",
            "parameter:cfr_severe_untreated", "parameter:dw_moderate", "parameter:dw_severe", "parameter:le_u5"}
    ok(conv <= set(req) and "unit_attribute:cm_ft" in req2 and "parameter:cfr_severe_treated" not in req2
       and probs == [],
       f"ED-R12 a net-only DALY decision needs treatment, severe-care, fatality and DALY inputs; a case decision needs "
       f"treatment only: {sorted(conv - set(req))} {sorted(set(req2) & conv)}")


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
                    "range", [0.2, 0.6]), "do not cover"),
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
    _self_test_round2(ok, T)
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
                                       {"parameter": v["parameters"], "unit_attribute": list(v["unit_attributes"])},
                                       DI.load_outcome_inputs(p))
            cov += DI.outcome_problems(DI.load_outcome_inputs(p))
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
            yaml.safe_dump(raw, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)  # the backend's
            req, probs = required_inputs(raw, d)        # full check reads the spec from the run dir
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
