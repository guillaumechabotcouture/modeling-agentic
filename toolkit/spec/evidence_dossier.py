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
dossier is required, not whether the spec names one.

One function decides (#218 review round 3): state() recomputes everything from the run's files every time (the
required inputs on the chosen or every eligible engine; coverage and validity; each entry against the value the run
uses, card defaults and structured literals included; a chosen range containing the draw support and covered by a
decision test that executes on this run's path; and the sheet's and the sign-off record's sha256 against the current
bytes). A stored record is only compared, never trusted. It is registered in toolkit/spec/preconditions.py, which
every path calls: the three signers, signoff.status, the launch and relaunch checks (cloud/run_cluster.py), and the
stage machine at ENGINE_CHOICE and on resume. toolkit/spec/evidence_regressions.py drives every bypass the reviews
found through every path. Nothing here names a disease; the inputs come from the engine's card.
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
    def m(x):
        return x if isinstance(x, dict) else {}
    o = [m(raw.get("objective")).get("outcome"), m(m(raw.get("deliverable")).get("headline")).get("outcome")]
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


_CACHE: dict = {}


def _engine_params(ref: str, cfg: dict) -> dict:
    """{name: {value, kind, lo, hi}} of the engine's parameters as this configuration builds them: the card file for
    an engine with a card and no configuration (read without importing it), else the configured engine's own."""
    import json
    key = ("params", ref, json.dumps(cfg or {}, sort_keys=True, default=str))
    if key not in _CACHE:
        d = _engine_dir(ref)
        if d and not cfg and os.path.isfile(os.path.join(d, "card.yaml")):
            import yaml
            with open(os.path.join(d, "card.yaml"), encoding="utf-8") as f:
                c = yaml.safe_load(f) or {}
            rows = [p for p in c.get("parameters") or [] if isinstance(p, dict)]
            _CACHE[key] = {p["name"]: {k: p.get(k) for k in ("value", "kind", "lo", "hi")} for p in rows}
        else:
            from toolkit.spec import schema
            e = schema.engine_class(ref)(**(cfg or {}))
            _CACHE[key] = {p.name: {"value": p.value, "kind": p.kind, "lo": p.lo, "hi": p.hi} for p in e.parameters()}
    return _CACHE[key]


def _draws(raw: dict, run_dir: str | None, ref: str, cfg: dict) -> dict:
    """{parameter: (lo, hi)} the uncertainty draws vary on this engine: fitted parameters over their card ranges when
    the engine_parameters source is declared (toolkit/analysis/core.py fitted ranges), and every prior the draw plan
    draws (toolkit/spec/priors.py drawn: card and spec priors, ranges on engine_parameters values, minus exclusions).
    {} for the priors when the plan cannot be computed (fail closed: nothing counts as drawn)."""
    import json
    key = ("draws", json.dumps([raw.get("uncertainty"), raw.get("engine_parameters"), ref, cfg], sort_keys=True,
                               default=str))
    if key in _CACHE:
        return _CACHE[key]
    out = {}
    u = raw.get("uncertainty") if isinstance(raw.get("uncertainty"), dict) else {}
    kinds = {x.get("kind") for x in u.get("sources") or [] if isinstance(x, dict)}
    if "engine_parameters" in kinds:
        try:
            out.update({n: (float(p["lo"]), float(p["hi"])) for n, p in _engine_params(ref, cfg).items()
                        if p.get("kind") == "fitted" and p.get("lo") is not None and p.get("hi") is not None})
        except Exception:  # noqa: BLE001
            pass
    try:
        from toolkit.spec import priors as PRI, schema
        s = schema.Spec(dict(raw, engine={"ref": ref, **({"config": dict(cfg)} if cfg else {})}), run_dir or ROOT, None)
        out.update({n: (float(p.lo), float(p.hi)) for n, p in PRI.drawn(s).items()})
    except Exception:  # noqa: BLE001
        pass
    _CACHE[key] = out
    return out


def _same(a, b) -> bool:
    """Structural equality of two YAML values, numbers compared with a relative tolerance."""
    if _num(a) and _num(b):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _effective(r: dict, raw: dict) -> list:
    """[(value, where)]: what the run actually uses for this input, one per engine that may run it. A parameter: the
    spec's engine_parameters value, else the engine's own (card default). A unit attribute bound to a literal value
    (decision_units.engine_attributes {value}): that literal, whatever its structure. A column-bound attribute varies
    by unit: [] (the dossier describes it)."""
    if r["kind"] == "parameter":
        ep = raw.get("engine_parameters") if isinstance(raw.get("engine_parameters"), dict) else {}
        e = ep.get(r["name"]) if isinstance(ep.get(r["name"]), dict) else None
        if e is not None and "value" in e:
            return [(e["value"], f"engine_parameters.{r['name']}.value")]
        out = []
        for ref, cfg in r.get("engines") or []:
            try:
                p = _engine_params(ref, cfg).get(r["name"])
            except Exception:  # noqa: BLE001
                p = None
            if p is not None and p.get("value") is not None:
                out.append((p["value"], f"the engine default of {ref}"))
        return out
    if r["kind"] == "unit_attribute":
        du = raw.get("decision_units") if isinstance(raw.get("decision_units"), dict) else {}
        ea = du.get("engine_attributes") if isinstance(du.get("engine_attributes"), dict) else {}
        b = ea.get(r["name"])
        if isinstance(b, dict) and "value" in b:
            return [(b["value"], f"decision_units.engine_attributes.{r['name']}.value")]
    return []


def _value_problems(key: str, e: dict, r: dict, ch: dict, raw: dict, run_dir: str | None) -> list[str]:
    """The dossier reconciles with what the run executes: the model value is the value the run uses; a chosen value is
    that value; a chosen range contains it and contains every range the uncertainty draws vary it over."""
    bad = []
    for x, where in _effective(r, raw):
        if not _same(e.get("model_value"), x):
            bad.append(f"entry {key}: model_value {e.get('model_value')!r} is not the value the run uses ({where} = {x!r})")
        rg = ch.get("range")
        if rg is not None:
            if _num(x) and isinstance(rg, list) and len(rg) == 2 and all(_num(y) for y in rg) and not rg[0] <= x <= rg[1]:
                bad.append(f"entry {key}: the value the run uses ({where} = {x!r}) is outside the chosen range {rg}")
        elif ch.get("value") is not None and not _same(ch["value"], x):
            bad.append(f"entry {key}: chosen.value {ch['value']!r} is not the value the run uses ({where} = {x!r})")
    if r["kind"] == "parameter":
        for ref, cfg in r.get("engines") or []:
            sup = _draws(raw, run_dir, ref, cfg).get(r["name"])
            if sup is None:
                continue
            rg = ch.get("range")
            if not (isinstance(rg, list) and len(rg) == 2 and all(_num(y) for y in rg)):
                bad.append(f"entry {key}: the uncertainty draws vary {r['name']} over {list(sup)} on {ref}: the chosen "
                           f"range must contain that draw support")
            elif rg[0] > sup[0] + 1e-12 or rg[1] < sup[1] - 1e-12:
                bad.append(f"entry {key}: the chosen range {rg} does not contain the draw support {list(sup)} the "
                           f"uncertainty draws use on {ref}")
    return bad


def _test_problems(key: str, r: dict, rng: list, tid: str, raw: dict, run_dir: str | None) -> list[str]:
    """Why decision test `tid` does not execute on this run's path, vary this input, and cover the chosen range."""
    from toolkit.spec import preconditions as PC
    if r["kind"] != "parameter":
        return [f"entry {key}: no decision test type varies a {r['kind']} (parameter_value varies engine parameters): "
                "choose a value with its rule, and declare the range in the spec as a limitation"]
    bad = []
    for ref, cfg in r.get("engines") or [(None, {})]:
        draws = _draws(raw, run_dir, ref, cfg) if ref else {}
        try:            # the shared runner check (#215 may give it the (spec, test_id, effect) -> (ok, why) form)
            res = PC.decision_test_runs(raw, tid, f"parameter:{r['name']}", drawn=draws)
        except TypeError:
            res = PC.decision_test_runs(raw, tid, f"parameter:{r['name']}")
        if not res[0]:
            bad.append(f"entry {key}: decision_test {tid} {res[1]}")
            continue
        if len(res) > 2:
            vals = list(res[2])
        elif tid in (f"prior_range_{r['name']}", f"fitted_range_{r['name']}"):
            vals = list(draws.get(r["name"]) or [])
        else:
            an = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}
            vals = next((list(t.get("values") or []) for t in an.get("decision_tests") or []
                         if isinstance(t, dict) and str(t.get("id")) == tid), [])
        nums = [v for v in vals if _num(v)]
        if not nums or min(nums) > rng[0] or max(nums) < rng[1]:
            bad.append(f"entry {key}: decision_test {tid}'s values {vals} do not cover the chosen range {rng}")
    return sorted(set(bad))


def _entry_problems(key: str, e, r: dict, raw: dict, run_dir: str | None = None) -> list[str]:
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
            bad += [f"entry {key}: observations[{j}].{f} is blank or not text"
                    for f in ("source", "where", "sampling_frame") if not _text(o.get(f))]
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
        if not (isinstance(rg, list) and len(rg) == 2 and all(_num(x) for x in rg) and rg[0] <= rg[1]):
            bad.append(f"entry {key}: chosen.range must be [lo, hi] with lo <= hi")
        elif not _text(e.get("decision_test")):
            bad.append(f"entry {key}: a range needs its decision_test (one that varies this input over the range)")
        else:
            bad += _test_problems(key, r, rg, e["decision_test"].strip(), raw, run_dir)
    return bad + _value_problems(key, e, r, ch, raw, run_dir)


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
    bad = []
    for key in sorted(req):
        if key not in ins:
            bad.append(f"no entry for {key} (used by {', '.join(req[key]['used_by'])})")
        else:
            bad += _entry_problems(key, ins[key], req[key], raw, run_dir)
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
    """Why the spec cannot be signed for want of evidence, from its contents alone (the sheet's view: state() adds the
    bindings)."""
    return _refusals(raw, run_dir, data)[0]


SHEET = "decision_spec.signoff_sheet.md"
SHEET_SOURCES = "<!-- signoff-sheet-sources: "       # toolkit/spec/signoff.py SHEET_SOURCES
RECORDS = ("decision_spec.signoff.json", "decision_brief.signoff.json")
ENGINE_CHOICE = "engine_choice.json"
EVIDENCE_KEYS = ("required", "dossier", "sha256")


def _sheet_dossier_sha(text) -> tuple:
    """(recorded, dossier sha256) from the sheet's sources line; (False, None) when it has none."""
    import json
    for line in reversed((text or "").splitlines()):
        if line.startswith(SHEET_SOURCES) and line.endswith(" -->"):
            try:
                d = json.loads(line[len(SHEET_SOURCES):-4])
            except ValueError:
                return False, None
            return isinstance(d, dict), (d.get("evidence_dossier_sha256") if isinstance(d, dict) else None)
    return False, None


def _reader(run_dir: str):
    from lib.safe_io import LinkRefused, read_nofollow

    def read(rel):
        parts = str(rel).replace("\\", "/").split("/")
        if os.path.isabs(rel) or ".." in parts:
            raise ValueError(f"{rel} must be a path inside the run directory")
        try:
            return read_nofollow(run_dir, rel)[0]
        except FileNotFoundError:
            return None
        except (LinkRefused, OSError) as e:
            raise ValueError(f"{rel}: {e}") from None
    return read


def state(run_dir: str | None = None, *, spec_bytes: bytes | None = None, read=None, signing: bool = False,
          engine: tuple | None = None, record: dict | None = None) -> dict:
    """The evidence precondition, recomputed from the files every time: what the options need (component and outcome
    declarations, on the engine chosen (engine_choice.json, or `engine`) or every engine that may be chosen), the
    dossier's coverage and validity, its reconciliation with the values the run uses, range containment both ways,
    whether each range's decision test executes, and the bindings: the sheet's dossier sha256 and, unless `signing`,
    the sign-off record's spec and dossier sha256 against the current bytes. A stored record is compared, never
    trusted; an absent, empty or partial evidence binding where one is needed is a problem.

    `read(rel) -> bytes | None` reads run files (default: run_dir, no link followed); the launch passes the archive's.
    Returns {"status": ok | refused | unsigned_evidence | changed | evidence_incomplete, "problems": [...],
    "binding": the {files, record} a signer records}."""
    import json
    import yaml
    read = read or _reader(run_dir)
    probs: list = []

    def get(rel):
        try:
            return read(rel)
        except ValueError as e:
            probs.append(str(e))
            return None
    spec = spec_bytes if spec_bytes is not None else get("decision_spec.yaml")
    if spec is None:
        return {"status": "refused", "problems": probs + ["decision_spec.yaml is missing"], "binding": None}
    try:
        raw = yaml.safe_load(spec.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        return {"status": "refused", "problems": [f"decision_spec.yaml does not parse: {e}"], "binding": None}
    raw = raw if isinstance(raw, dict) else {}
    run = dict(raw)
    eng = raw.get("engine")
    auto = eng == "auto" or (isinstance(eng, dict) and eng.get("auto") is True)
    if engine is not None:
        run["engine"] = {"ref": engine[0], **({"config": dict(engine[1])} if engine[1] else {})}
    elif auto:
        ch = get(ENGINE_CHOICE)
        if ch is not None:
            try:
                e = (json.loads(ch.decode()) or {}).get("engine") or {}
            except (ValueError, UnicodeDecodeError, AttributeError):
                e = None
            if isinstance(e, dict) and e.get("ref"):
                run["engine"] = {"ref": e["ref"], **({"config": e["config"]} if e.get("config") else {})}
            else:
                probs.append(f"{ENGINE_CHOICE} is unreadable: the chosen engine's inputs cannot be resolved")
    rel = dossier_reference(raw)
    data = get(rel) if rel else None
    try:
        bad, req = _refusals(run, run_dir, data)
    except Exception as e:  # noqa: BLE001 -- fail closed
        bad, req = [f"the evidence check failed: {type(e).__name__}: {e}"], {}
    coverage = probs + bad
    needed = bool(req) or rel is not None
    binding = {"files": {rel: sha(data) if data is not None else None} if rel else {},
               "record": {"required": sorted(req), "dossier": rel, "sha256": sha(data) if rel and data is not None
                          else None}}
    bind_probs, unsigned, changed = [], [], []
    if needed:
        recorded, ssha = _sheet_dossier_sha((get(SHEET) or b"").decode("utf-8", "replace"))
        if not recorded:
            bind_probs.append("no sign-off sheet showing the evidence dossier (regenerate it: python3 "
                              "toolkit/spec/signoff.py sheet <run_dir> --write)")
        elif ssha != sha(data):
            bind_probs.append("the sign-off sheet was generated from another evidence dossier (it is stale): "
                              "regenerate it and read it again")
    if not signing:
        rec = record
        if rec is None:
            for f in RECORDS:
                b = get(f)
                try:
                    d = json.loads(b.decode()) if b is not None else None
                except (ValueError, UnicodeDecodeError):
                    d = None
                if isinstance(d, dict) and ((d.get("sha256") or {}).get("decision_spec.yaml") if isinstance(
                        d.get("sha256"), dict) else None or d.get("spec_sha256")):
                    rec = d
                    break
        if rec is None:
            if needed:
                unsigned.append("unsigned_evidence: no sign-off record binds the evidence dossier")
        else:
            signed_spec = ((rec.get("sha256") or {}).get("decision_spec.yaml") if isinstance(rec.get("sha256"), dict)
                           else None) or rec.get("spec_sha256")
            if needed and signed_spec != sha(spec):
                changed.append("decision_spec.yaml is not the one signed (edited after its sign-off): the owner "
                               "amends and re-signs it")
            ev = rec.get("evidence")
            if "evidence" not in rec:
                if needed:
                    unsigned.append("unsigned_evidence: signed before the evidence-dossier requirement, and the "
                                    f"options need {len(req)} decision input(s): re-sign with a dossier")
            elif not (isinstance(ev, dict) and all(k in ev for k in EVIDENCE_KEYS)
                      and isinstance(ev.get("required"), list)):
                unsigned.append(f"unsigned_evidence: the sign-off's evidence binding is empty or partial ({ev!r})")
            else:
                filed = {**((rec.get("sha256") or {}) if isinstance(rec.get("sha256"), dict) else {}),
                         **((rec.get("evidence_sha256") or {}) if isinstance(rec.get("evidence_sha256"), dict) else {})}
                if ev.get("dossier") != rel or (rel and (ev.get("sha256") != sha(data) or data is None
                                                         or filed.get(rel) not in (None, ev.get("sha256")))):
                    changed.append("the evidence dossier is not the one the sign-off recorded (edited after signing, "
                                   "or missing): the signature is stale")
                if set(ev.get("required") or []) != set(req):
                    coverage.append("the decision inputs required now differ from those signed: "
                                    f"{sorted(set(req) - set(ev.get('required') or []))} added, "
                                    f"{sorted(set(ev.get('required') or []) - set(req))} removed")
    problems = unsigned + changed + bind_probs + coverage
    if not problems:
        status = "ok"
    elif signing:
        status = "refused"
    elif unsigned:
        status = "unsigned_evidence"
    elif changed or bind_probs:
        status = "changed"
    else:
        status = "evidence_incomplete"
    return {"status": status, "problems": problems, "binding": binding}


def _precondition(run_dir, spec_bytes, signer, **ctx) -> list:
    """The evidence check as a registered precondition (toolkit/spec/preconditions.py)."""
    st = state(run_dir, spec_bytes=spec_bytes, read=ctx.get("read"), signing=signer == "sign",
               engine=ctx.get("engine"), record=ctx.get("record"))
    cap = ctx.get("capture")
    if isinstance(cap, dict):           # the caller's view of this check: its status and the binding a signer records
        cap.update(evidence_status=st["status"], evidence_binding=st["binding"])
    return [f"evidence dossier: {p}" for p in st["problems"]]


def _register():
    from toolkit.spec import preconditions as PC
    PC.register("evidence_dossier", _precondition)


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
            "chosen": {"range": [0.3, 0.7], "rule": "the card's fitted range, which the draws vary it over; it contains "
                                                    "the trial's 0.42"},
            "decision_test": "DT_eff_a"},
        "parameter:k": {
            "model_value": 0.8, "model_source": "engine card default",
            "no_data": {"reason": "no field measurement of the toy slope",
                        "borrowing_rule": "the card's value, held fixed"},
            "chosen": {"value": 0.8, "rule": "the card's value"}},
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
                                              "values": [0.3, 0.7], "text": "Does the plan change across the trial "
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
              "chosen": {"range": [0.6, 1.0], "rule": "the prior's range, which the draws use"},
              "decision_test": "prior_range_k"}

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
        ok(sorted(req3) == ["parameter:eff_a", "parameter:k", "unit_attribute:x"] and seen.get("run_dir") == d and probs3 == [],
           f"ED-R10 engine: auto requires the inputs of the engines the backend finds eligible, with the run dir: "
           f"{sorted(req3)} {seen}")
        # ED-S1/B1/B2: state() recomputes from the files; the chosen engine (or `engine`) decides what is required
        from toolkit.spec import signoff as SO
        SO.write_sheet(d)
        st = state(d, signing=True)
        ok(st["status"] == "ok" and st["binding"]["files"] == {DOSSIER_FILE: sha(open(os.path.join(d, DOSSIER_FILE),
                                                                                      "rb").read())}
           and st["binding"]["record"]["required"] == ["parameter:eff_a", "parameter:k", "unit_attribute:x"],
           f"ED-B1 state() at signing: no problem, and the binding a signer records: {st}")
        ok(state(d, signing=True, engine=("toolkit.engines.contract:ToyEngine", {}))["status"] == "ok"
           and state(dict_dir := d, signing=True, engine=(TOY, {}))["status"] == "ok",
           "ED-S1 state() resolves the inputs on the engine given (as ENGINE_CHOICE passes the one it picked)")
        os.remove(os.path.join(d, DOSSIER_FILE))
        st = state(d, signing=True)
        ok(st["status"] == "refused" and any("does not exist" in p for p in st["problems"]),
           f"ED-B2 state() refuses a dossier that does not exist: {st['problems'][:2]}")
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
    # ED-E5 (#218 review round 3): each configured engine's per-component completeness, by perturbation
    try:
        from toolkit.engines.contract import Horizon, UnitSpec
        from toolkit.engines.renewal.engine import RenewalEngine
        from toolkit.engines.compartmental.engine import CompartmentalEngine
        rdir = os.path.join(ROOT, "toolkit", "engines", "renewal")
        ren = RenewalEngine(generation_interval=[0.2, 0.4, 0.25, 0.15], transmissibility={"knots": [2025.5]},
                            components={"ctl": {"start": 2025.25, "stop": 2025.5}})
        got = DI.dependency_problems(ren, UnitSpec("city", {"N": 1.5e6, "s0": 0.8, "seed": 2e-4}),
                                     Horizon(2025.0, 2026.0, (2025.0, 2026.0), {}), "infections", DI.load(rdir),
                                     DI.load_outcome_inputs(rdir), base_params={"r0": 1.5, "effect:ctl": 0.4})
        cdir = os.path.join(ROOT, "toolkit", "engines", "compartmental")
        com = CompartmentalEngine(groups=["all"], structure="single_group", mode="to_extinction", initialiser="given",
                                  dt="day", generation_interval=[0.2, 0.3, 0.3, 0.2],
                                  components={"vax": {"kind": "campaign", "targets": ["all"], "reach": 0.5}})
        got2 = DI.dependency_problems(com, UnitSpec("u", {"N": [1e6], "s0": [0.9], "seed": 1e-9}),
                                      Horizon(0, 200000, (0, 200000), {}), "infections", DI.load(cdir),
                                      DI.load_outcome_inputs(cdir), base_params={"r0": 2.0, "efficacy:vax": 0.8})
    except Exception as e:  # noqa: BLE001
        got = got2 = [f"{type(e).__name__}: {e}"]
    ok(got == [] and got2 == [], f"ED-E5 renewal and compartmental: every input that moves a component's effect is "
                                 f"declared for that component: {got[:3]} {got2[:3]}")
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
            ok(sorted(req) == ["parameter:eff_a", "parameter:k", "unit_attribute:x"] and probs == []
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
            ok(sorted(req) == ["parameter:eff_a", "parameter:k", "unit_attribute:x"],
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
