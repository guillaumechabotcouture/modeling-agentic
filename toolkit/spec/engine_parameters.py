#!/usr/bin/env python3
"""Engine parameter values declared by the signed spec (owner decision, 2026-11-02).

    python3 toolkit/spec/engine_parameters.py --self-test
    python3 toolkit/spec/engine_parameters.py benchmarks <pack.yaml> <run_dir> [--only ids]
                                          # harness step: the pack's arms at the run's spec values

A spec may set the point value of an engine parameter for its run. The value is a scenario
choice the owner signs, not an engine change and not a fit:

    engine_parameters:
      <parameter>:
        value: 273.75                    # a finite number
        units: days                      # optional; must equal the engine card's units
        source: "<where the value comes from>"
        reason: "<why this run uses it>"
        outside_card_range: true         # required when the value (or its range) lies outside the
                                         # engine card's range; refused when it does not
        range: [lo, hi]                  # optional: the uncertainty draws vary it over this range
        dist: uniform | loguniform | triangular    # required with range (triangular: mode at value)

Rules (every check names the entry it failed):
  - the parameter is on the engine card, of kind `fixed` or `scenario`; fitted and calibrated
    values come from fitting and are refused; a unit attribute is bound in decision_units;
  - source and reason are required;
  - the card's range is the parameter's own `lo`/`hi`, else its engine-card prior's range. A
    value outside it needs `outside_card_range: true`; the sign-off sheet, results.md and the
    report then show it as a HIGH note;
  - the field is in the signed spec, so the sign-off hash covers it (analysis_settings.yaml
    cannot set it); `uncertainty.priors` cannot also name the parameter.

Application: `params(spec, theta)` returns the spec values with `theta` applied over them. It is
the one place the value enters an engine evaluation: every parameter set the analysis builds
starts from it (the point parameters of toolkit/analysis/core.py, which the uncertainty draws,
decision tests, stability folds, sensitivity, schedule and trajectory tasks copy; the emulator's
batch jobs in toolkit/analysis/batch.py; the calibration's engine runs in
toolkit/analysis/calibration.py). A draw or a decision test that sets the parameter still wins.

Uncertainty: with `range`, the parameter is drawn over it (an engine-card prior on it is
replaced); without, it is held fixed at the spec value (the card prior is not drawn) and the
interval label says "held fixed at the spec value". On the emulator path, a held parameter that a
`parameter_value` decision test varies is an emulator input over the box spanning the spec value
and the tested values, so the test runs; the draws keep it at the spec value.

An interaction_rescale decision test reads its arm ratios from benchmark results computed at the spec
values: `analysis/benchmarks_at_spec_values.yaml`, written only by the harness step (`benchmarks`), with a
harness record of its digest, the values and each arm's engine and configuration; it is an input of the
decision-test artifact. Otherwise the test is not run, so the rescaling is never derived at one set of
parameters and applied at another. In those arms a benchmark setting's own parameters (a trial's observed
conditions) win over the spec values, which replace only engine defaults.

A spec without the field is unchanged: same parameter sets, same batch jobs, same job hashes.
Nothing here names a disease, a place or an intervention.
"""
from __future__ import annotations

import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIELD = "engine_parameters"
KEYS = ("value", "units", "source", "reason", "outside_card_range", "range", "dist", "verified")
KINDS = ("fixed", "scenario")
DISTS = ("uniform", "loguniform", "triangular")      # toolkit/spec/priors.py DISTS: bounded, so the design box spans them
HELD = "held fixed at the spec value"
HEADING = "## Engine parameter values set by the signed spec"


def _raw(spec) -> dict:
    r = spec.raw if hasattr(spec, "raw") else spec
    return r if isinstance(r, dict) else {}


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _range(e: dict):
    rg = e.get("range")
    return [float(rg[0]), float(rg[1])] if (isinstance(rg, list) and len(rg) == 2 and all(_isnum(x) for x in rg)
                                            and rg[0] < rg[1]) else None


def declared(spec) -> dict:
    """The field as written ({} when absent or not a mapping; shape_problems reports the latter)."""
    d = _raw(spec).get(FIELD)
    return d if isinstance(d, dict) else {}


def _entry_shape(n, e) -> list[str]:
    w = f"{FIELD}.{n}"
    if not isinstance(e, dict):
        return [f"{w}: a mapping {{value, source, reason, ...}}"]
    bad = []
    unk = sorted(set(map(str, e)) - set(KEYS))
    if unk:
        bad.append(f"{w}: unknown keys {unk} (known: {', '.join(KEYS)})")
    if not _isnum(e.get("value")):
        bad.append(f"{w}: value must be a finite number")
    for k, what in (("source", "where the value comes from"), ("reason", "why this run uses it")):
        if not str(e.get(k) or "").strip():
            bad.append(f"{w}: {k} ({what}) is empty")
    if "outside_card_range" in e and not isinstance(e["outside_card_range"], bool):
        bad.append(f"{w}: outside_card_range is true or false")
    if "units" in e and not isinstance(e["units"], str):
        bad.append(f"{w}: units is text")
    if e.get("range") is not None:
        rg = _range(e)
        if rg is None or (_isnum(e.get("value")) and not rg[0] <= float(e["value"]) <= rg[1]):
            bad.append(f"{w}: range [lo, hi] with lo < hi containing the value")
        if e.get("dist") not in DISTS:
            bad.append(f"{w}: dist (one of {', '.join(DISTS)}) is required with range")
        elif e["dist"] == "loguniform" and rg is not None and rg[0] <= 0:
            bad.append(f"{w}: a loguniform range must be positive")
    elif "dist" in e:
        bad.append(f"{w}: dist without range (give range [lo, hi], or drop dist: the value is then held fixed)")
    return bad


def shape_problems(spec) -> list[str]:
    """Checks that need no engine (also the sign-off checks of an engine: auto spec before the choice)."""
    raw = _raw(spec)
    if raw.get(FIELD) is None:
        return []
    if not isinstance(raw[FIELD], dict):
        return [f"{FIELD}: a mapping {{parameter: {{value, source, reason, ...}}}}"]
    bad = []
    for n, e in raw[FIELD].items():
        bad += _entry_shape(n, e)
    pri = (raw.get("uncertainty") or {}).get("priors") if isinstance(raw.get("uncertainty"), dict) else None
    for n in raw[FIELD]:
        if isinstance(pri, dict) and n in pri:
            bad.append(f"uncertainty.priors: {n} has its value in {FIELD}; declare its range there "
                       f"({FIELD}.{n}.range and dist)")
    return bad


def card_range(engine, name):
    """The engine card's range of a parameter: its own lo/hi, else its engine-card prior's range (None: none)."""
    ps = {p.name: p for p in engine.parameters()}.get(name)
    if ps is None:
        return None
    if ps.lo is not None and ps.hi is not None:
        return [float(ps.lo), float(ps.hi)]
    from toolkit.spec import priors as PRI
    pr = PRI.load_card(engine)["priors"].get(name)
    return [float(pr.lo), float(pr.hi)] if pr else None


def _outside(v, rg, cr):
    """Which side of a spec value leaves the card's range: "value", "range" (the value is inside, its uncertainty range
    is not) or None."""
    if cr is None:
        return None
    if not cr[0] <= v <= cr[1]:
        return "value"
    if rg is not None and (rg[0] < cr[0] or rg[1] > cr[1]):
        return "range"
    return None


def problems(spec, engine) -> list[str]:
    """Every problem with the field on this engine (empty: valid)."""
    bad = shape_problems(spec)
    card = engine.describe()
    specs = {p.name: p for p in engine.parameters()}
    for n, e in declared(spec).items():
        w = f"{FIELD}.{n}"
        if not isinstance(e, dict):
            continue
        ps = specs.get(n)
        if ps is None:
            why = ("; it is a unit attribute of the engine: bind it in decision_units.engine_attributes"
                   if n in (card.unit_attributes or {}) else "")
            bad.append(f"{w}: not an engine parameter of {card.name}{why}")
            continue
        if ps.kind not in KINDS:
            bad.append(f"{w}: of kind {ps.kind}; a spec value is for a fixed or scenario parameter (fitted and "
                       "calibrated values come from fitting)")
            continue
        if isinstance(e.get("units"), str) and e["units"].strip() != (ps.units or ""):
            bad.append(f"{w}: units {e['units']!r} differ from the engine card's {ps.units or ''!r}")
        if not _isnum(e.get("value")):
            continue
        v, rg, cr = float(e["value"]), _range(e), card_range(engine, n)
        flag = e.get("outside_card_range") is True
        side = _outside(v, rg, cr)
        then = ("; set outside_card_range: true with the reason (it is then a HIGH note on the sign-off sheet, in "
                "results.md and in the report)")
        if cr is None:
            if flag:
                bad.append(f"{w}: outside_card_range is set but the engine card gives no range for {n}")
        elif side == "value" and not flag:
            bad.append(f"{w}: the value {v:g} is outside the engine card's range [{cr[0]:g}, {cr[1]:g}]{then}")
        elif side == "range" and not flag:
            bad.append(f"{w}: the uncertainty range [{rg[0]:g}, {rg[1]:g}] reaches outside the engine card's range "
                       f"[{cr[0]:g}, {cr[1]:g}] (the value {v:g} is inside){then}")
        elif flag and side is None:
            bad.append(f"{w}: outside_card_range is true but the value{' and its range lie' if rg else ' lies'} inside "
                       f"the card's range [{cr[0]:g}, {cr[1]:g}]")
    return bad


def values(spec) -> dict:
    """{parameter: spec value} for every entry with a numeric value."""
    return {str(n): float(e["value"]) for n, e in declared(spec).items() if isinstance(e, dict) and _isnum(e.get("value"))}


def held_fixed(spec) -> dict:
    """{parameter: spec value} for the entries without a range (never drawn)."""
    return {n: v for n, v in values(spec).items() if declared(spec)[n].get("range") is None}


def held(spec) -> dict:
    """{parameter: spec value} for every spec value the effective draw plan does not vary: no range, or a range whose
    prior the spec excludes (by name or kind). Without an engine to read the plan from: the entries without a range."""
    vals = values(spec)
    if not vals or not hasattr(spec, "engine"):
        return held_fixed(spec)
    from toolkit.spec import priors as PRI
    dr = PRI.drawn(spec)
    return {n: v for n, v in vals.items() if n not in dr}


def tested_boxes(spec) -> dict:
    """{parameter: [lo, hi]} for each held spec value that a parameter_value decision test varies: the box spans the
    spec value and the tested values (the emulator design input; the draws keep the spec value)."""
    hv = held(spec)
    out = {}
    for dt in ((_raw(spec).get("analysis") or {}).get("decision_tests") or []):
        if not isinstance(dt, dict) or dt.get("type") != "parameter_value" or dt.get("parameter") not in hv:
            continue
        vs = [float(x) for x in dt.get("values") or [] if _isnum(x)]
        if vs:
            lo, hi = out.get(dt["parameter"], [hv[dt["parameter"]]] * 2)
            out[dt["parameter"]] = [min(lo, *vs), max(hi, *vs)]
    return {k: v for k, v in sorted(out.items()) if v[0] < v[1]}


def params(spec, theta=None) -> dict:
    """The one application: the spec values, with `theta` applied over them (a draw or a decision test that sets the
    parameter wins). Without the field this is `theta` unchanged."""
    out = values(spec)
    out.update(theta or {})
    return out


# ---- benchmark arms at the spec values (an interaction_rescale decision test reads its arm ratios from them)
# Harness outputs (review round 3, R3-1): written only by write_spec_benchmarks(), under analysis/ (a v2 harness prefix
# agents cannot write; agents/enforcement.py SCRIPT_OUTPUTS in v1). The record holds the file's digest and how each arm
# was computed; verification reads the record and recomputes each arm's engine identity, never fields of the file alone.
SPEC_BENCH = "analysis/benchmarks_at_spec_values.yaml"
SPEC_BENCH_RECORD = "analysis/benchmarks_at_spec_values.record.json"
RECORD_PRODUCER = "toolkit/spec/engine_parameters.py:write_spec_benchmarks"


def _sha(path: str) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return None


def _config_sha(engine):
    """The engine's full configuration digest (benchmark.config_digest), or a fixed marker for an engine with no
    config(): engine_identity() hashes only the card's structure and leaves out some dynamics settings (R4-1)."""
    from toolkit.engines import benchmark as BM
    return BM.config_digest(engine) or "no-config"


def _ref_of(spec) -> str | None:
    e = _raw(spec).get("engine")
    return e if isinstance(e, str) else (e.get("ref") if isinstance(e, dict) else None)


def _pack_engine(pack: dict):
    from toolkit.engines import benchmark as BM
    return BM.load_engine(pack["engine"], (pack.get("configs") or {}).get(pack.get("default_config")))


def _check_pack_engine(pack: dict, spec, engine=None):
    """The engine the pack's runner builds (its reference and default configuration) must be the run's engine:
    the stamp and the record name the engine the arms ran with (review round 3, R3-2)."""
    from toolkit.engines import benchmark as BM
    from toolkit.engines.contract import engine_identity
    run_eng = spec.engine()
    pe = _pack_engine(pack)
    why = []
    if pack.get("engine") != _ref_of(spec):
        why.append(f"the pack runs {pack.get('engine')}, the run {_ref_of(spec)}")
    elif engine_identity(pe) != engine_identity(run_eng) or BM.config_digest(pe) != BM.config_digest(run_eng):
        why.append("the pack's default configuration gives another engine identity or configuration than the run's")
    if engine is not None and engine_identity(engine) != engine_identity(run_eng):
        why.append("the engine passed in is not the run's")
    if why:
        raise ValueError("benchmark pack: not the run's engine (" + "; ".join(why) + ")")
    return pe


def _arm_records(pack: dict, runner, vals: dict) -> dict:
    """{benchmark id: {"arms": {role: {arm, setting, ref, config, engine_identity, superseded}}}}: the engine and
    configuration each arm actually ran with, and the spec values its setting's own parameters replaced."""
    from toolkit.engines import benchmark as BM
    from toolkit.engines.contract import engine_identity
    out = {}
    for b in pack.get("benchmarks") or []:
        est = b.get("estimand") or {}
        roles = {k: est[k] for k in ("a11", "a10", "a01", "a00") if isinstance(est.get(k), str)} \
            or {a: a for a in BM._arms_of(est) if isinstance(a, str)}
        arms = {}
        for role, a in roles.items():
            cfg = (pack.get("arms") or {}).get(a)
            if not cfg:
                continue
            st = pack["settings"][cfg["setting"]]
            name = BM._config_name(pack, st)
            conf = (pack.get("configs") or {}).get(name) if name is not None else \
                (pack.get("configs") or {}).get(pack.get("default_config"))
            en = runner.make_engine(name) if name is not None else runner.engine
            own = st.get("params") or {}
            arms[role] = {"arm": a, "setting": cfg["setting"], "ref": pack["engine"], "config": conf,
                          "config_name": name, "engine_identity": engine_identity(en),
                          "config_sha256": BM.config_digest(en),
                          "superseded": {k: own[k] for k in vals if k in own}}
        out[b["id"]] = {"arms": arms}
    return out


def spec_benchmarks(pack: dict, engine, spec, fitted: dict | None = None, workers: int = 1, seeds=None) -> dict:
    """The run's benchmark pack with the spec values applied under each setting's own parameters (a setting's observed
    conditions win; review round 3, R3-3) and over the engine defaults, stamped by the benchmark runner for the engine
    the arms ran with. `engine` must be the run's engine (it is checked, not used)."""
    from toolkit.engines import benchmark as BM
    pe = _check_pack_engine(pack, spec, engine)
    vals = values(spec)
    runner = BM.Runner(pack, pe, workers, seeds, base_params=vals)
    out = BM.run_pack(pack, pe, runner, dict(fitted or {}))
    return BM.stamp(dict(out, spec_parameters=vals, arm_engines=_arm_records(pack, runner, vals)), pe)


def write_spec_benchmarks(run_dir: str, pack: dict, fitted: dict | None = None, workers: int = 1, seeds=None) -> str:
    """The harness step: computes the arms at the run's spec values, writes SPEC_BENCH and its harness record
    SPEC_BENCH_RECORD (the file's digest, the spec values, the run's engine identity, and each arm's engine and
    configuration). Returns the file's path."""
    import datetime as _dt
    import yaml
    from toolkit.engines.contract import engine_identity
    from toolkit.spec import schema
    if os.path.join(ROOT, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from lib.safe_io import write_nofollow      # the trusted run root; no link followed, atomic publish
    spec = schema.load(os.path.join(run_dir, "decision_spec.yaml"))
    doc = spec_benchmarks(pack, spec.engine(), spec, fitted, workers, seeds)
    write_nofollow(run_dir, SPEC_BENCH, yaml.safe_dump(doc, sort_keys=False).encode(), atomic=True)
    path = os.path.join(run_dir, SPEC_BENCH)
    rec = {"producer": RECORD_PRODUCER, "written_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
           "file": SPEC_BENCH, "sha256": _sha(path), "spec_sha256": spec.sha256, "spec_parameters": values(spec),
           "pack_sha256": __import__("hashlib").sha256(json.dumps(pack, sort_keys=True, default=str).encode()).hexdigest(),
           "engine_identity": engine_identity(spec.engine()), "config_sha256": _config_sha(spec.engine()),
           "benchmarks": doc["arm_engines"]}
    write_nofollow(run_dir, SPEC_BENCH_RECORD, json.dumps(rec, indent=1, sort_keys=True).encode(), atomic=True)
    return path


def verified_spec_benchmarks(engine, spec, bench_id: str | None = None) -> tuple[list, str | None]:
    """(benchmark rows, None) when the harness step wrote SPEC_BENCH for this run: its harness record exists, the file
    matches the record's digest, the record's values are this spec's, the run's engine identity matches, and each arm
    of the benchmark (all, without `bench_id`) still has the engine identity its recorded reference and configuration
    give now. Else ([], why)."""
    import yaml
    from toolkit.engines import benchmark as BM
    from toolkit.engines.contract import engine_identity
    base = spec.base
    p, rp = os.path.join(base, SPEC_BENCH), os.path.join(base, SPEC_BENCH_RECORD)
    how = ("compute them with the harness step: python toolkit/spec/engine_parameters.py benchmarks <pack.yaml> "
           "<run_dir> --only <benchmark id>")
    if not os.path.exists(rp):
        return [], f"no harness record {SPEC_BENCH_RECORD}; {how}"
    try:
        rec = json.load(open(rp, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], f"{SPEC_BENCH_RECORD} unreadable: {e}"
    if not isinstance(rec, dict) or rec.get("producer") != RECORD_PRODUCER:
        return [], f"{SPEC_BENCH_RECORD} was not written by the harness step"
    if not os.path.exists(p) or _sha(p) != rec.get("sha256"):
        return [], f"{SPEC_BENCH} does not match the digest in its harness record (missing, or edited after the harness " \
                   f"wrote it); {how}"
    sp, vals = rec.get("spec_parameters") or {}, values(spec)
    if set(sp) != set(vals) or any(not _isnum(sp[k]) or abs(float(sp[k]) - vals[k]) > 1e-12 * max(1.0, abs(vals[k]))
                                   for k in vals):
        return [], f"the arms were computed at {sp}, not at this spec's values {vals}; {how}"
    if rec.get("engine_identity") != engine_identity(engine):
        return [], f"the arms were computed for another engine identity or code than the run's; {how}"
    if rec.get("config_sha256") is None or rec["config_sha256"] != _config_sha(engine):
        return [], f"the arms were computed for another engine configuration than the run's (configuration digest); {how}"
    bms = rec.get("benchmarks") or {}
    for bid, b in sorted(bms.items()):
        if bench_id is not None and bid != bench_id:
            continue
        for role, a in sorted((b.get("arms") or {}).items()):
            try:
                en = engine if a.get("config_name") is None else BM.load_engine(a["ref"], a.get("config"))
                now, now_cfg = engine_identity(en), BM.config_digest(en)
            except Exception as e:  # noqa: BLE001 -- fail closed
                return [], f"benchmark {bid} arm {role}: its engine cannot be rebuilt ({type(e).__name__}: {e})"
            if a.get("ref") != _ref_of(spec) or a.get("engine_identity") != now or "config_sha256" not in a \
                    or a["config_sha256"] != now_cfg:
                return [], (f"benchmark {bid} arm {role} ran with another engine or configuration (identity or "
                            f"configuration digest) than it would now; {how}")
    if bench_id is not None and bench_id not in bms:
        return [], f"benchmark {bench_id} has no arm record in {SPEC_BENCH_RECORD}; {how}"
    doc = yaml.safe_load(open(p, encoding="utf-8")) or {}
    return list(doc.get("benchmarks") or []), None


def rows(spec, engine) -> list[dict]:
    """One row per spec value, for the engine artifact and the reports: the value, the card value it replaces and the
    card's range, the source and reason, the outside flag (and which side is outside) and how the draws treat it."""
    if not declared(spec):
        return []
    from toolkit.spec import priors as PRI
    specs = {p.name: p for p in engine.parameters()}
    dr = PRI.drawn(spec) if hasattr(spec, "raw") else {}
    out = []
    for n, v in values(spec).items():
        e, ps = declared(spec)[n], specs.get(n)
        if ps is None:
            continue
        rg = _range(e)
        if n in dr:
            unc = f"drawn over [{dr[n].lo:g}, {dr[n].hi:g}] ({dr[n].dist})"
        elif rg:
            unc = f"{HELD} (its range is excluded from the draws)"
        else:
            unc = HELD
        cr = card_range(engine, n)
        out.append({"name": n, "value": v, "units": str(e.get("units") or ps.units or ""), "card_value": float(ps.value),
                    "card_kind": ps.kind, "card_range": cr, "card_source": ps.source, "range": rg,
                    "outside_card_range": e.get("outside_card_range") is True, "outside": _outside(v, rg, cr),
                    "source": str(e.get("source")).strip(), "reason": str(e.get("reason")).strip(), "uncertainty": unc})
    return out


def _g(x) -> str:
    return "none" if x is None else f"{float(x):.6g}"


def markdown(rws: list[dict], heading: str = HEADING, ref=None) -> list[str]:
    """The block results.md and the report show. `ref(name, key)`: a claim reference for a number (results.md), or
    None for the number itself (the harness-rendered report block)."""
    if not rws:
        return []
    num = (lambda r, k, x: (ref(r["name"], k) if ref else None) or _g(x))  # noqa: E731
    L = [heading, "",
         "The signed spec sets these engine parameter values for every engine evaluation of the run (the point "
         "outcomes, the uncertainty draws, the decision tests, stability and trajectories). Each replaces the engine "
         "card's value; it is not an engine change and not a fit.", "",
         "| Parameter | Spec value | Card value it replaces | Card range | Uncertainty | Source | Reason |",
         "|---|---|---|---|---|---|---|"]
    for r in rws:
        u = f" {r['units']}" if r["units"] else ""
        cr = r["card_range"]
        rng = f"{num(r, 'card_lo', cr[0] if cr else None)} to {num(r, 'card_hi', cr[1] if cr else None)}{u}" if cr \
            else "none on the card"
        L.append(f"| {r['name']} | {num(r, 'value', r['value'])}{u} | {num(r, 'card_value', r['card_value'])}{u} | "
                 f"{rng} | {r['uncertainty']} | {r['source']} | {r['reason']} |")
    flagged = [r for r in rws if r["outside_card_range"]]
    if flagged:
        L.append("")
    for r in flagged:
        u = f" {r['units']}" if r["units"] else ""
        cr = r["card_range"] or [None, None]
        card = f"the engine card's range ({num(r, 'card_lo', cr[0])} to {num(r, 'card_hi', cr[1])}{u})"
        if r.get("outside") == "range" and r.get("range"):
            what = (f"the uncertainty range of {r['name']} ({_g(r['range'][0])} to {_g(r['range'][1])}{u}) reaches "
                    f"outside {card}; the value itself is inside")
        else:
            what = f"{r['name']} = {num(r, 'value', r['value'])}{u} is outside {card}"
        L.append(f"- **HIGH**: {what}. The owner signed it (outside_card_range: true); its reason is in the table.")
    return L + [""]


def report_block(run_dir: str) -> list[str]:
    """The report's harness block, from the run's engine artifact (analysis/engine.yaml `spec_values`)."""
    import yaml
    p = os.path.join(run_dir, "analysis", "engine.yaml")
    if not os.path.exists(p):
        return []
    sv = ((yaml.safe_load(open(p, encoding="utf-8")) or {}).get("data") or {}).get("spec_values") or {}
    return markdown([dict(r, name=n) for n, r in sv.items()], HEADING + " (rendered by the harness)")


def _sheet_refs(raw: dict, candidates=None, run_dir: str | None = None) -> list[tuple]:
    """(ref, engine declaration) for every engine the spec may run: a string or {ref, config} engine; the candidates
    of engine: auto; else the pre-sign check's candidate rows; else the harness's choice (engine_choice.json)."""
    import json
    eng = raw.get("engine")
    if isinstance(eng, str) and eng != "auto":
        return [(eng, {"ref": eng})]
    if isinstance(eng, dict) and eng.get("ref"):
        return [(eng["ref"], eng)]
    cands = eng.get("candidates") if isinstance(eng, dict) else None
    rows_ = [r for r in candidates or [] if isinstance(r, dict) and r.get("ref")]
    conf = {r["ref"]: dict(r["config"]) for r in rows_ if isinstance(r.get("config"), dict) and r["config"]}

    def decl(ref):   # a candidate with the configuration the pre-sign check used for it, when it supplied one
        return (ref, {"ref": ref, **({"config": conf[ref]} if ref in conf else {})})
    if cands:
        return [decl(r) for r in cands]
    if rows_:        # the pre-sign check's rows: each with its configuration
        return [decl(r["ref"]) for r in rows_]
    cp = os.path.join(run_dir, "engine_choice.json") if run_dir else None
    if cp and os.path.exists(cp):
        try:
            e = (json.load(open(cp)) or {}).get("engine") or {}
            if e.get("ref"):
                return [(e["ref"], e)]
        except (OSError, ValueError):
            pass
    return []


def sheet_lines(raw: dict, candidates=None, run_dir: str | None = None) -> list[str]:
    """The sign-off sheet's section: each value with its source and reason, the card value and range of each engine
    the spec may run (_sheet_refs), and a HIGH note for a value or range outside a card's range (the owner accepts it
    by signing). With engine: auto and no engine known yet, it says the card values are not yet known."""
    from toolkit.spec import schema
    out = ["## Engine parameter values set by the spec"]
    ents = declared(raw)
    if not ents:
        return out + ["None: the run uses the engine card's values.", ""]
    refs = _sheet_refs(raw, candidates, run_dir)
    engines = {}
    for ref, decl in refs:
        try:
            engines[ref] = schema.load_engine(decl)
        except Exception as e:  # noqa: BLE001 -- the sheet still shows the spec's entries
            engines[ref] = e
    if not engines:
        out.append("- engine: auto, and no candidate engine is known yet: the card values are shown once the pre-sign "
                   "check lists the candidates or the harness has chosen the engine. A value outside the chosen card's "
                   "range is refused then unless the entry sets outside_card_range.")
    high = []
    for n, e in ents.items():
        if not isinstance(e, dict):
            continue
        u = f" {e['units']}" if isinstance(e.get("units"), str) and e["units"] else ""
        rg = _range(e)
        unc = f"drawn over [{rg[0]:g}, {rg[1]:g}] ({e.get('dist')})" if rg else HELD
        val = _g(e["value"]) if _isnum(e.get("value")) else repr(e.get("value"))
        out.append(f"- **{n}** = {val}{u}. Source: {e.get('source')}. Reason: {e.get('reason')}. Uncertainty: {unc}.")
        for ref, en in engines.items():
            if isinstance(en, Exception):
                out.append(f"  - `{ref}`: card not loaded ({type(en).__name__}: {en})")
                continue
            ps = {p.name: p for p in en.parameters()}.get(n)
            if ps is None:
                out.append(f"  - `{ref}`: not a parameter of this engine (the engine is ineligible)")
                continue
            cr = card_range(en, n)
            out.append(f"  - `{ref}`: card value {_g(ps.value)}{u} ({ps.kind}; {ps.source}); card range "
                       + (f"{_g(cr[0])} to {_g(cr[1])}{u}" if cr else "none"))
            side = _outside(float(e["value"]), rg, cr) if _isnum(e.get("value")) else None
            if side:
                what = (f"the uncertainty range of {n} ({_g(rg[0])} to {_g(rg[1])}{u}) reaches outside" if side == "range"
                        else f"{n} = {val}{u} is outside")
                high.append(f"- **HIGH**: {what} the card range of `{ref}` ({_g(cr[0])} to {_g(cr[1])}{u}; "
                            f"outside_card_range: {str(e.get('outside_card_range') is True).lower()}). Signing accepts "
                            "it; its source and reason are above.")
    return out + ([""] + high if high else []) + [""]


def _cli(argv) -> int:
    """benchmarks <pack.yaml> <run_dir> [--only ids] [--workers N] [--seeds K]: the harness step that computes the pack's
    arms at the run's spec values (SPEC_BENCH and its record SPEC_BENCH_RECORD)."""
    import argparse
    import yaml
    from toolkit.spec import schema
    ap = argparse.ArgumentParser(prog="engine_parameters.py benchmarks")
    ap.add_argument("pack")
    ap.add_argument("run_dir")
    ap.add_argument("--only", default=None, help="only these benchmark ids (comma-separated)")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=0, help="the first K seeds of the pack (0 = all)")
    a = ap.parse_args(argv)
    pack = yaml.safe_load(open(a.pack))
    if a.only:
        keep = set(a.only.split(","))
        pack = dict(pack, benchmarks=[b for b in pack["benchmarks"] if b["id"] in keep])
    fp = os.path.join(os.path.dirname(os.path.abspath(a.pack)), "fitted.yaml")
    f = yaml.safe_load(open(fp)) if os.path.exists(fp) else {}
    fitted = {k: v["value"] for k, v in (f.get("parameters") or {}).items()}
    seeds = pack["seeds"][:a.seeds] if a.seeds else pack["seeds"]
    out = write_spec_benchmarks(a.run_dir, pack, fitted, a.workers, seeds)
    print(f"wrote {out} and {SPEC_BENCH_RECORD}")
    return 0


# --------------------------------------------------------------------------- self-test
# Job hashes of the toy specs' emulator batches before this field existed (base c0ed767): a spec without the field
# must keep them byte for byte (sha256 over the concatenated job_sha values, first 16 hex digits; the temporary run
# directory in an engine config path is replaced by a placeholder).
GOLDEN_JOB_DIGESTS = {"toy": ("447abd162b931237", 240), "toy_priors": ("d2abd76d6b3e0a80", 240)}


def _self_test() -> int:
    import copy
    import glob
    import hashlib
    import math
    import tempfile
    import yaml
    from toolkit.spec import schema, signoff
    from toolkit.spec import engine_parameters as EPm     # the module the engine loader imports, not __main__
    from toolkit.spec import toy_engines as TOYS
    from toolkit import import_guard as G
    G.allow_test_refs("engine", TOYS.REFS)     # this process and the workers it starts (never a production run)
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def guard(label, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- a case that crashes is a failed case, and the others still run
            import traceback
            traceback.print_exc()
            ok(False, f"{label}: {type(e).__name__}: {str(e)[:300]}")

    q = lambda *a, **k: None  # noqa: E731
    SRC, WHY = "toy source (a benchmark sweep)", "toy reason (the owner's choice for the headline)"

    def toy_raw(d, **entries):
        raw = yaml.safe_load(open(schema.toy_spec(d, n=24)))
        raw["engine"] = {"ref": "toolkit.spec.toy_engines:ToySpecEngine",
                         "config": {"log": True}}
        os.environ[TOYS.LOG_ENV] = d          # the call log's root, inherited by worker processes
        if entries:
            raw[FIELD] = entries
        return raw

    # ------------------------------------------------------------ schema
    def schema_cases():
        with tempfile.TemporaryDirectory() as d:
            good = {"value": 0.55, "source": SRC, "reason": WHY}
            r0 = toy_raw(d, base=dict(good))
            ok(schema.validate(schema.Spec(r0, d)) == [], f"EP1 a spec value with source and reason validates: "
                                                           f"{schema.validate(schema.Spec(r0, d))[:3]}")

            def bad(entries, needle, label, mut=None):
                r = toy_raw(d, **entries)
                if mut:
                    mut(r)
                errs = schema.validate(schema.Spec(r, d))
                ok(any(needle in e for e in errs), f"{label}: {[e for e in errs if FIELD in e or needle in e][:3]}")
            bad({"nope": dict(good)}, "not an engine parameter", "EP2 an unknown parameter is refused")
            bad({"x": dict(good)}, "unit attribute", "EP3 a unit attribute is refused, with where it is bound")
            bad({"eff_a": dict(good, value=0.5)}, "of kind fitted", "EP4a a fitted parameter is refused")
            bad({"c_rate": dict(good, value=1.0)}, "of kind calibrated", "EP4b a calibrated parameter is refused")
            bad({"base": {"value": 0.55, "reason": WHY}}, "source", "EP5a a value without a source is refused")
            bad({"base": {"value": 0.55, "source": SRC}}, "reason", "EP5b a value without a reason is refused")
            bad({"base": dict(good, value=0.3)}, "outside_card_range",
                "EP6a a value outside the card's range without the flag is refused")
            r = toy_raw(d, base=dict(good, value=0.3, outside_card_range=True))
            ok(schema.validate(schema.Spec(r, d)) == [],
               f"EP6b with outside_card_range: true it validates: {schema.validate(schema.Spec(r, d))[:3]}")
            bad({"base": dict(good, outside_card_range=True)}, "inside the card's range",
                "EP6c the flag on a value inside the card's range is refused")
            bad({"synergy": dict(good, value=0.1, outside_card_range=True)}, "gives no range",
                "EP6d the flag on a parameter whose card gives no range is refused")
            bad({"base": dict(good, range=[0.56, 0.6], dist="uniform")}, "containing the value",
                "EP7a a range that excludes the value is refused")
            bad({"base": dict(good, range=[0.45, 0.6])}, "dist", "EP7b a range without its distribution is refused")
            bad({"base": dict(good, range=[0.3, 0.6], dist="uniform")}, "outside_card_range",
                "EP7c a range reaching outside the card's range needs the flag")
            bad({"base": dict(good, value="high")}, "finite number", "EP7d a value that is not a number is refused")
            bad({"base": dict(good, units="days")}, "units", "EP7e units that differ from the card's are refused")
            bad({"base": dict(good, note="x")}, "unknown keys", "EP7f unknown keys are refused")
            bad({"synergy": dict(good, value=0.05)}, "declare its range there", "EP7g uncertainty.priors on a "
                "spec-valued parameter is refused",
                mut=lambda x: x["uncertainty"].update(priors={"synergy": {"dist": "uniform", "range": [0.0, 0.1],
                                                                          "source": "s"}}))
            r = toy_raw(d)
            r[FIELD] = ["base"]
            errs = schema.validate(schema.Spec(r, d))
            ok(any(e.startswith(FIELD) and "mapping" in e for e in errs), f"EP7h a non-mapping field is refused: {errs[:2]}")
            # review round 2 (R2-6): a range that leaves the card's range is not reported as an out-of-range value
            r = toy_raw(d, base=dict(good, range=[0.3, 0.6], dist="uniform"))
            errs = [e for e in schema.validate(schema.Spec(r, d)) if FIELD in e]
            ok(errs and "uncertainty range [0.3, 0.6]" in errs[0] and "value 0.55 is inside" in errs[0]
               and "the value (0.55" not in errs[0],
               f"EP7i a range reaching outside the card's range is named as the range, with the value inside: {errs[:1]}")
            r = toy_raw(d, base=dict(good, range=[0.3, 0.6], dist="uniform", outside_card_range=True))
            sr = schema.Spec(r, d)
            rw = {x["name"]: x for x in rows(sr, sr.engine())}
            md = "\n".join(markdown(list(rw.values())))
            ok(schema.validate(sr) == [] and rw["base"].get("outside") == "range"
               and "uncertainty range" in md and "base = 0.55 is outside" not in md,
               f"EP7j the report's HIGH note names the range that is outside, not the value: {md[-260:]!r}")
            # engine: auto. Shape checks at sign-off; each candidate checked against its own card
            ra = toy_raw(d, base={"value": 0.55, "source": SRC})
            ra["engine"] = {"auto": True, "candidates": ["toolkit.spec.toy_engines:ToySpecEngine",
                                                         "toolkit.engines.empirical.engine:PackageRateEngine"]}
            errs = schema.validate(schema.Spec(ra, d), pending_ok=True)
            ok(any(FIELD in e and "reason" in e for e in errs),
               f"EP9a engine: auto before the choice: the entry's shape is checked at sign-off: {errs[:2]}")
            ra[FIELD]["base"]["reason"] = WHY
            sa = schema.Spec(ra, d)
            e_ok = schema.validate(schema.with_engine(sa, "toolkit.spec.toy_engines:ToySpecEngine"))
            e_bad = schema.validate(schema.with_engine(sa, "toolkit.engines.empirical.engine:PackageRateEngine",
                                                       {"components": ["a", "b"]}))
            ok(schema.validate(sa, pending_ok=True) == [] and not [e for e in e_ok if FIELD in e]
               and any(FIELD in e and "not an engine parameter" in e for e in e_bad),
               f"EP9b a candidate engine without the parameter is refused, one with it passes: {e_bad[:1]}")
        with tempfile.TemporaryDirectory() as d:
            r = toy_raw(d, base={"value": 0.55, "source": SRC, "reason": WHY})
            p = os.path.join(d, "decision_spec.yaml")
            yaml.safe_dump(r, open(p, "w"), sort_keys=False)
            yaml.safe_dump({FIELD: {"base": {"value": 0.45}}}, open(os.path.join(d, schema.SETTINGS), "w"))
            try:
                schema.load(p)
                ok(False, "EP8a analysis_settings.yaml cannot set a spec value")
            except schema.SpecError as e:
                ok("signed decision spec" in str(e), f"EP8a analysis_settings.yaml cannot set a spec value: {e}")
            os.remove(os.path.join(d, schema.SETTINGS))
            signoff.sign(d, "lead", note="self-test")
            st0 = signoff.status(d)
            r[FIELD]["base"]["value"] = 0.45
            yaml.safe_dump(r, open(p, "w"), sort_keys=False)
            ok(st0 == "signed" and signoff.status(d) == "changed",
               f"EP8b the value is in the signed spec: changing it breaks the sign-off ({st0} -> {signoff.status(d)})")
    guard("EP1-EP9 schema", schema_cases)

    # ------------------------------------------------------------ job hashes without the field
    def hash_cases():
        from toolkit.analysis import batch as Bt
        from toolkit.spec import priors as PRI

        with tempfile.TemporaryDirectory() as d:
            def dig(s):
                _d, jobs = Bt.design(s)
                norm = lambda j: dict(j, engine=json.loads(json.dumps(j["engine"]).replace(d, "<run>")))  # noqa: E731
                return hashlib.sha256("".join(Bt.job_sha(norm(j)) for j in jobs).encode()).hexdigest()[:16], len(jobs)
            s = schema.load(schema.toy_spec(d))
            pp = PRI.toy_priors_file(d)
            rp = copy.deepcopy(s.raw)
            rp["engine"] = {"ref": "toolkit.spec.priors:ToyPriorEngine", "config": {"priors_path": pp}}
            got = {"toy": dig(s), "toy_priors": dig(schema.Spec(rp, d))}
            ok(got == GOLDEN_JOB_DIGESTS, f"EP-H1 without the field, batch job hashes are byte-identical to before it "
                                          f"existed: {got}")
            re_ = copy.deepcopy(s.raw)
            re_[FIELD] = {}
            ok(dig(schema.Spec(re_, d)) == got["toy"] and EPm.params(schema.Spec(re_, d), {"a": 1.0}) == {"a": 1.0},
               "EP-H2 an empty field changes nothing")
            rk = copy.deepcopy(s.raw)
            rk[FIELD] = {"k": {"value": 0.9, "source": SRC, "reason": WHY}}
            sk = schema.Spec(rk, d)
            dk, jk = Bt.design(sk)
            d0, j0 = Bt.design(s)
            ok(dig(sk)[0] != got["toy"][0] and all(j["params"]["k"] == 0.9 for j in jk)
               and [{k: v for k, v in j["params"].items() if k != "k"} for j in jk] == [j["params"] for j in j0]
               and "k" not in j0[0]["params"],
               "EP-H3 with the field, the value enters every job's params (so job hashes change) and nothing else does")
    guard("EP-H job hashes", hash_cases)

    # ------------------------------------------------------------ review round 2 (R2-3): a ranged value held fixed
    def excluded_range_cases():
        from toolkit.analysis import batch as Bt
        with tempfile.TemporaryDirectory() as d:
            r = toy_raw(d, base={"value": 0.3, "range": [0.25, 0.35], "dist": "uniform", "outside_card_range": True,
                                 "source": SRC, "reason": WHY})
            r["uncertainty"]["excluded"].append({"kind": "priors", "reason": "owner: hold the priors for this test"})
            r["analysis"]["decision_tests"] = [{"id": "DTc", "type": "parameter_value", "parameter": "base",
                                                "values": [0.5], "text": "Does the plan change at the card value?"}]
            s = schema.Spec(r, d)
            dsg, jobs = Bt.design(s)
            ok(schema.validate(s) == [] and tested_boxes(s) == {"base": [0.3, 0.5]}
               and dsg.get("spec_value_names") == ["base"] and "base" not in (dsg.get("prior_names") or []),
               f"EP29 a ranged spec value whose priors are excluded is held, so the test that varies it is a design "
               f"input: {tested_boxes(s)}, {dsg.get('spec_value_names')}")
    guard("EP29 excluded range", excluded_range_cases)

    # ------------------------------------------------------------ review round 2 (R2-1): interaction arms at spec values
    def interaction_cases():
        from toolkit.analysis import run as RUN, decision_tests as DTm
        from toolkit.engines.contract import engine_identity
        with tempfile.TemporaryDirectory() as d:
            bench, _v_ok, _v_bad = RUN._toy_bench(d)
            p = schema.toy_spec(d, n=24, bench=bench)
            r = yaml.safe_load(open(p))
            r[FIELD] = {"synergy": {"value": 0.05, "source": SRC, "reason": WHY}}
            yaml.safe_dump(r, open(p, "w"), sort_keys=False)
            signoff.sign(d, "lead", note="self-test")
            R = RUN.run(d, log=q, compute_kw={"max_workers": 2})
            t = {x["test_id"]: x for x in R["decision_tests"]}.get("DT1") or {}
            ok(t.get("status") == "not_run" and "benchmark arms not computed at the spec values" in t.get("reason", ""),
               f"EP26 an interaction test whose benchmark arms were computed at the card values is not run when the "
               f"spec sets engine parameter values: {t.get('status')}, {t.get('reason', '')[:160]}")
            A = R["_A"]
            z0 = {i: v["z"] for i, v in R["calibration"]["units"].items()}
            out0, ben0, cost0 = A.evaluate(A.theta0, z0)
            plan0 = R["point"]["plan"]

            def dt_at(v):
                return {"id": "DTi", "type": "interaction_rescale", "components": ["b"], "with_any": ["a"],
                        "benchmark": "T2", "values": [v], "text": "Does the plan change at the engine's own interaction?"}
            # review round 3 (R3-1): a file with copied producer and identity fields and invented arm ratios is refused
            card = yaml.safe_load(open(bench))
            forged = dict(card, spec_parameters={"synergy": 0.05})
            for row in forged["benchmarks"]:
                row["arm_or"] = [{"a11": 0.2, "a10": 0.5, "a01": 0.6}]
            fp = os.path.join(d, EPm.SPEC_BENCH)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            yaml.safe_dump(forged, open(fp, "w"), sort_keys=False)
            rec = DTm.run_one(A, dt_at(1.0), z0, ben0, cost0, plan0, R["uncertainty"], out0)
            ok(rec.get("status") == "not_run" and "benchmark arms not computed at the spec values" in rec.get("reason", ""),
               f"EP30 a benchmark file with copied provenance fields and invented arm ratios is refused (no harness "
               f"record): {rec.get('status')}, {rec.get('reason', '')[:200]}")
            os.remove(fp)
            # R3-2: the harness step refuses a pack whose engine is not the run's engine
            bad_pack = dict(RUN._toy_pack(), engine="toolkit.spec.toy_engines:ToySpecEngine")
            try:     # the stamp must name the engine the arms ran with, not the engine passed in
                from toolkit.engines.contract import ToyEngine
                EPm.spec_benchmarks(bad_pack, ToyEngine(results_path=bench), schema.load(p))
                ok(False, "EP31a arms run by the pack's engine are not stamped with another engine's identity")
            except Exception as e:  # noqa: BLE001
                ok("not the run's engine" in str(e),
                   f"EP31a arms run by the pack's engine are not stamped with another engine's identity: {e}")
            try:
                EPm.write_spec_benchmarks(d, bad_pack)
                ok(False, "EP31 a pack that runs another engine than the run's is refused")
            except Exception as e:  # noqa: BLE001
                ok("not the run's engine" in str(e), f"EP31 a pack that runs another engine than the run's is refused: {e}")
            # the harness step: the arms at the spec values, a harness record of the file and of each arm's engine
            path = EPm.write_spec_benchmarks(d, RUN._toy_pack())
            recd = json.load(open(os.path.join(d, EPm.SPEC_BENCH_RECORD)))
            arms = (recd.get("benchmarks") or {}).get("T2", {}).get("arms") or {}
            ok(path == os.path.join(d, EPm.SPEC_BENCH) and EPm.SPEC_BENCH.startswith("analysis/")
               and recd.get("sha256") and set(arms) == {"a11", "a10", "a01", "a00"} and "config_sha256" in recd
               and all("config_sha256" in x for x in arms.values())
               and all(x.get("engine_identity") == engine_identity(A.eng) for x in arms.values()),
               f"EP32 the harness step writes the file under analysis/ with a record of its digest and of the engine and "
               f"configuration each arm ran with: {sorted(arms)}")
            doc = yaml.safe_load(open(path))
            b = {x["id"]: x for x in doc["benchmarks"]}["T2"]
            arm = {k: math.exp(sum(math.log(x[k]) for x in b["arm_or"]) / len(b["arm_or"])) for k in ("a11", "a10", "a01")}
            cur = arm["a11"] / (arm["a10"] * arm["a01"])
            rec = DTm.run_one(A, dt_at(cur), z0, ben0, cost0, plan0, R["uncertainty"], out0)
            base_obj = sum(ben0[i][plan0[i]] for i in A.ids)
            got = (rec.get("results") or [{}])[0].get("point_plan_objective_under_test")
            ok(rec.get("status") == "run" and got is not None and abs(got - base_obj) <= 1e-9 * max(1.0, abs(base_obj))
               and EPm.SPEC_BENCH in str(rec.get("arm_source")),
               f"EP27 with the arms computed at the spec values, testing the engine's own interaction changes no "
               f"benefit: {base_obj} -> {got} ({rec.get('status')}, {rec.get('arm_source')})")
            ok(EPm.SPEC_BENCH in str(rec.get("arm_inputs")) and EPm.SPEC_BENCH_RECORD in str(rec.get("arm_inputs")),
               f"EP33 the decision test names the file and its record as inputs: {rec.get('arm_inputs')}")
            R2 = RUN.run(d, log=q, compute_kw={"max_workers": 2})
            dta = yaml.safe_load(open(os.path.join(d, "analysis", "decision_tests.yaml")))
            ok({x["test_id"]: x for x in R2["decision_tests"]}["DT1"]["status"] == "run"
               and EPm.SPEC_BENCH in (dta.get("inputs") or {}) and EPm.SPEC_BENCH_RECORD in (dta.get("inputs") or {}),
               f"EP34 the analysis records the file's digest as an input of the decision tests: "
               f"{sorted(dta.get('inputs') or {})}")
            from toolkit.analysis import contract
            doc_e = yaml.safe_load(open(path))
            doc_e["benchmarks"][0]["arm_or"] = [{"a11": 0.2, "a10": 0.5, "a01": 0.6}]
            yaml.safe_dump(doc_e, open(path, "w"), sort_keys=False)
            rec = DTm.run_one(A, dt_at(cur), z0, ben0, cost0, plan0, R["uncertainty"], out0)
            kinds = {x["kind"] for x in contract.gate_findings(d)}
            ok(rec.get("status") == "not_run" and "digest" in rec.get("reason", "")
               and "analysis_contract_tampered" in kinds or "analysis_contract_stale" in kinds,
               f"EP35 an edit of the file after the harness wrote it is refused, and the finished run no longer checks: "
               f"{rec.get('reason', '')[:160]}; {sorted(kinds)}")
            EPm.write_spec_benchmarks(d, RUN._toy_pack())
            recd = json.load(open(os.path.join(d, EPm.SPEC_BENCH_RECORD)))
            recd["spec_parameters"] = {"synergy": 0.04}
            json.dump(recd, open(os.path.join(d, EPm.SPEC_BENCH_RECORD), "w"))
            rec = DTm.run_one(A, dt_at(cur), z0, ben0, cost0, plan0, R["uncertainty"], out0)
            ok(rec.get("status") == "not_run" and "benchmark arms not computed at the spec values" in rec.get("reason", ""),
               f"EP28 arms recorded at other values are refused: {rec.get('reason', '')[:160]}")
    guard("EP26-EP35 interaction arms", interaction_cases)

    # ------------------------------------------------------------ review round 3 (R3-3): a trial's own conditions win
    def precedence_cases():
        from toolkit.engines import benchmark as BM
        pk = yaml.safe_load(open(os.path.join(ROOT, "toolkit", "engines", "malaria", "benchmarks", "pack.yaml")))
        eng = BM.load_engine(pk["engine"], (pk.get("configs") or {}).get(pk.get("default_config")))
        rn = BM.Runner(pk, eng, 1, [1], base_params={"net_retention_half": 273.75})
        mul = rn._arm_jobs("mul_pyr", {"irs_kill": 0.4})[0][2]
        coch = rn._arm_jobs("coch_pyr", {"irs_kill": 0.4})[0][2]
        ok(mul.get("net_retention_half") == 949.0 and coch.get("net_retention_half") == 273.75
           and mul.get("irs_kill") == 0.4 and mul.get("n_agents") == 5000,
           f"EP36 the real B15 setting keeps its own observed net retention (Muleba 949 days) under a spec value; a "
           f"setting without one takes the spec value; fitted values still apply: {mul.get('net_retention_half')}, "
           f"{coch.get('net_retention_half')}")
        rn0 = BM.Runner(pk, eng, 1, [1])
        ok(rn0._arm_jobs("mul_pyr", {"irs_kill": 0.4})[0][2] == dict(pk["settings"]["muleba"]["params"], irs_kill=0.4),
           "EP37 without spec values the arm parameters are unchanged (the setting's, then the fitted values)")
    guard("EP36-EP37 setting precedence", precedence_cases)

    # ------------------------------------------------------------ review round 4 (R4-1): the full configuration digest
    def config_digest_cases():
        from toolkit.engines import benchmark as BM
        from toolkit.engines.compartmental.engine import CompartmentalEngine
        from toolkit.engines.contract import engine_identity
        ref = "toolkit.engines.compartmental.engine:CompartmentalEngine"
        e1, e2 = CompartmentalEngine(generation_interval=[1.0]), CompartmentalEngine(generation_interval=[0.5, 0.5])

        class _S:            # a spec stub: the verification reads raw, base and path only
            def __init__(self, d, cfg):
                self.raw = {"engine": {"ref": ref, "config": cfg}, FIELD: {"r0": {"value": 1.5}}}
                self.base, self.path = d, None
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis"))
            fp = os.path.join(d, EPm.SPEC_BENCH)
            yaml.safe_dump({"benchmarks": []}, open(fp, "w"))
            cfg1 = {"generation_interval": [1.0]}
            arm = {"arm": "x", "setting": "s", "ref": ref, "config": cfg1, "config_name": None,
                   "engine_identity": engine_identity(e1), "config_sha256": BM.config_digest(e1), "superseded": {}}
            rec = {"producer": EPm.RECORD_PRODUCER, "file": EPm.SPEC_BENCH, "sha256": EPm._sha(fp),
                   "spec_parameters": {"r0": 1.5}, "engine_identity": engine_identity(e1),
                   "config_sha256": BM.config_digest(e1), "benchmarks": {"B": {"arms": {"a": arm}}}}
            json.dump(rec, open(os.path.join(d, EPm.SPEC_BENCH_RECORD), "w"))
            same = EPm.verified_spec_benchmarks(e1, _S(d, cfg1), "B")[1]
            other = EPm.verified_spec_benchmarks(e2, _S(d, {"generation_interval": [0.5, 0.5]}), "B")[1]
            ok(engine_identity(e1) == engine_identity(e2) and BM.config_digest(e1) != BM.config_digest(e2)
               and same is None and other is not None and "configuration" in other,
               f"EP38 a record made with generation_interval [1.0] is refused for [0.5, 0.5] (same identity, other "
               f"configuration digest); accepted for its own: {same!r}, {other!r}")
            del rec["config_sha256"]
            json.dump(rec, open(os.path.join(d, EPm.SPEC_BENCH_RECORD), "w"))
            ok(EPm.verified_spec_benchmarks(e1, _S(d, cfg1), "B")[1] is not None,
               "EP39 a record without the configuration digest is refused (fail closed)")
    guard("EP38-EP39 configuration digest", config_digest_cases)

    # ------------------------------------------------------------ review round 5 (R5-1): toy engines are test-only
    def guard_cases():
        import subprocess
        refs = ["toolkit.spec.engine_parameters:ToySpecEngine", "toolkit.spec.engine_parameters:ToySpecRateEngine",
                "toolkit.spec.toy_engines:ToySpecEngine", "toolkit.spec.toy_engines:ToySpecRateEngine"]
        code = ("import sys, json; sys.path.insert(0, %r)\n"
                "from toolkit import import_guard as G\n"
                "out = {}\n"
                "for r in %r:\n"
                "    try:\n"
                "        G.check_ref('engine', r); out[r] = 'allowed'\n"
                "    except G.ImportRefused:\n"
                "        out[r] = 'refused'\n"
                "print(json.dumps(out))") % (ROOT, refs)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MA_IMPORT_GUARD")}
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT)
        got = json.loads(res.stdout or "{}") if res.returncode == 0 else {}
        ok(got and all(v == "refused" for v in got.values()),
           f"EP40 a production process (no test registration) refuses the self-test toy engines: {got or res.stderr[-300:]}")
        from toolkit import import_guard as G
        bad = []
        for r in ("subprocess:Popen", "toolkit.engines.contract:ToyEngine", "toolkit.spec.schema:Spec"):
            try:
                G.allow_test_refs("engine", [r])
                bad.append(r)
            except (G.ImportRefused, AttributeError, ValueError):
                pass
        ok(hasattr(G, "allow_test_refs") and not bad,
           f"EP41 a test registration can enable only the named self-test toy engines: accepted {bad}")
    guard("EP40-EP41 test-only toy engines", guard_cases)

    # ------------------------------------------------------------ emulator path: application, UQ, decision test, reports
    def emulator_cases():
        from toolkit.analysis import run as RUN, trajectories as TR
        with tempfile.TemporaryDirectory() as d:
            r = toy_raw(d,
                        base={"value": 0.3, "outside_card_range": True, "source": SRC, "reason": WHY},
                        k={"value": 0.9, "source": SRC, "reason": WHY},
                        synergy={"value": 0.05, "range": [0.0, 0.1], "dist": "uniform", "source": SRC,
                                 "reason": WHY})
            r["analysis"]["decision_tests"] = [{"id": "DTc", "type": "parameter_value", "parameter": "base",
                                                "values": [0.5], "finding": "decision_test:DTc",
                                                "text": "Does the plan change at the engine card value?"}]
            yaml.safe_dump(r, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
            signoff.sign(d, "lead", note="self-test")
            md = signoff.sheet(d)
            ok("## Engine parameter values set by the spec" in md and "HIGH" in md and "base" in md
               and "0.4" in md and "0.6" in md and WHY in md and SRC in md
               and md.count("HIGH") == 1,
               "EP20 the sign-off sheet lists each spec value with its source and reason, and a HIGH note for the one "
               "outside the card's range")
            R = RUN.run(d, log=q, compute_kw={"max_workers": 2})
            A = R["_A"]
            ok(A.theta0.get("base") == 0.3 and A.theta0.get("k") == 0.9 and A.theta0.get("synergy") == 0.05,
               f"EP10 the point parameters carry the spec values: { {k: A.theta0.get(k) for k in ('base', 'k')} }")
            design = json.load(open(os.path.join(d, "analysis", "batch", "design.json")))
            jobs = [json.loads(x) for x in open(os.path.join(d, "analysis", "batch", "results.jsonl"))]
            pts = design["points"]
            ok(design.get("spec_value_names") == ["base"] and all(0.3 - 1e-12 <= p_["theta"]["base"] <= 0.5 + 1e-12
                                                                  for p_ in pts)
               and min(p_["theta"]["base"] for p_ in pts) < 0.35 and max(p_["theta"]["base"] for p_ in pts) > 0.45
               and all(p_["theta"]["k"] == 0.9 for p_ in pts) and design.get("prior_names") == ["synergy"]
               and all(0.0 <= p_["theta"]["synergy"] <= 0.1 for p_ in pts) and len(jobs) == len(pts) * 4,
               f"EP11 the batch: k held at its spec value in every job; base (a decision test varies it) an emulator "
               f"input over the box from its spec value to the tested value; synergy drawn over its range: "
               f"{design.get('spec_value_names')}, {design.get('prior_names')}")
            calls = [json.loads(x) for f in sorted(glob.glob(os.path.join(d, "calls.*.jsonl"))) for x in open(f)]
            runs = [c for c in calls if c["call"] == "run"]
            sch = [c for c in calls if c["call"] == "schedule"]
            ok(runs and all(c["k"] == 0.9 for c in calls) and sch and all(c["base"] == 0.3 for c in sch),
               f"EP12 every engine evaluation received the held value (k: {len(calls)} calls, worker batch and "
               f"in-process schedule hooks); the figure schedule used base at its spec value ({len(sch)} calls)")
            tk, _step = TR.tasks(d, per_region=1, seeds=1)
            ok(tk and all(t["params"].get("base") == 0.3 and t["params"].get("k") == 0.9 for t in tk),
               "EP13 trajectory tasks carry the spec values")
            u = R["uncertainty"]
            rep = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
            ps = rep["parameter_samples"]
            ok("held fixed at the spec value: base, k" in u["label"] and "synergy" in (u["interval"].get(
                "prior_dominated") or []) and "base" not in ps and "k" not in ps
               and 0.0 <= ps["synergy"]["ci_low"] < ps["synergy"]["ci_high"] <= 0.1
               and {x["name"]: x for x in rep["parameters_fixed"]}["base"]["value"] == 0.3,
               f"EP14 UI: a value without a range is held fixed and the label says so; one with a range is drawn over "
               f"it: {u['label']}")
            dts = {x["test_id"]: x for x in R["decision_tests"]}
            t = dts.get("DTc") or {}
            ok(t.get("status") == "run" and [x["value"] for x in t.get("results") or []] == [0.5]
               and (t.get("spec_value") or {}).get("value") == 0.3
               and (t.get("spec_value") or {}).get("card_value") == 0.5
               and (t.get("spec_value") or {}).get("card_value_tested") is True
               and "spec value" in t.get("statement", "") and "card value" in t.get("statement", "")
               and not any(ch.isdigit() for ch in t.get("statement", "")),
               f"EP15 a parameter_value test at the card value runs against the spec-value headline, and says so in "
               f"words: {t.get('status')}, {t.get('statement')}")
            ok("prior_range_synergy" in dts and "prior_range_base" not in dts,
               f"EP16 the ranged spec value gets the automatic prior_range test; a held one does not: {sorted(dts)}")
            eng = yaml.safe_load(open(os.path.join(d, "analysis", "engine.yaml")))["data"]
            sv = eng.get("spec_values") or {}
            ok(set(sv) == {"base", "k", "synergy"} and sv["base"]["card_value"] == 0.5
               and sv["base"]["card_range"] == [0.4, 0.6] and sv["base"]["outside_card_range"] is True
               and sv["k"]["uncertainty"] == "held fixed at the spec value"
               and sv["synergy"]["uncertainty"].startswith("drawn") and sv["base"]["source"] == SRC,
               f"EP17 the engine artifact records each spec value with the card value it replaces: {sv.get('base')}")
            txt = open(os.path.join(d, "results.md")).read()
            sec = txt.split("## Engine parameter values set by the signed spec")[-1].split("\n## ")[0] \
                if "## Engine parameter values set by the signed spec" in txt else ""
            ok(sec and "base" in sec and "0.3" in sec and "0.5" in sec and "HIGH" in sec and SRC in sec
               and WHY in sec and "[CLAIM:" not in txt and sec.count("HIGH") == 1,
               f"EP18 results.md names every spec value with its source, reason and the card value it replaces: "
               f"{sec[:400]!r}")
            blk = "\n".join(EPm.report_block(d))
            ok("base" in blk and "0.3" in blk and "0.5" in blk and "HIGH" in blk and SRC in blk and WHY in blk
               and "k" in blk and "synergy" in blk, f"EP19 the report's harness block names them too: {blk[:300]!r}")
            figs = yaml.safe_load(open(os.path.join(d, "analysis", "figures.yaml")))["data"]["figures"]
            fp = [f for f in figs if f["figure"] == "parameters"]
            import csv
            prow = {x["name"]: x for x in csv.DictReader(open(os.path.join(d, fp[0]["table"])))} if fp else {}
            ok(prow.get("base", {}).get("kind") == "spec value" and float(prow["base"]["value"]) == 0.3
               and float(prow["base"]["card_value"]) == 0.5 and prow.get("k", {}).get("kind") == "spec value",
               f"EP21 the parameters figure shows spec values as such, with the card value: {prow.get('base')}")
            ok(not glob.glob(os.path.join(d, "analysis", "blocked.yaml")), "EP22 the run completes unblocked")
    guard("EP10-EP22 emulator path", emulator_cases)

    # ------------------------------------------------------------ direct path with likelihood calibration
    def direct_cases():
        from toolkit.analysis import run as RUN
        with tempfile.TemporaryDirectory() as d:
            RUN._direct_toy(d)
            p = os.path.join(d, "decision_spec.yaml")
            r = yaml.safe_load(open(p))
            r["engine"]["ref"] = "toolkit.spec.toy_engines:ToySpecRateEngine"
            r[FIELD] = {"scale": {"value": 1.25, "units": "multiplier", "source": SRC, "reason": WHY}}
            r["analysis"]["decision_tests"].append({"id": "DTs", "type": "parameter_value", "parameter": "scale",
                                                    "values": [1.0], "text": "Does the plan change at the card value?"})
            yaml.safe_dump(r, open(p, "w"), sort_keys=False)
            signoff.sign(d, "lead", note="self-test")
            TOYS.ToySpecRateEngine.CALLS.clear()
            R = RUN.run(d, log=q)
            calls = list(TOYS.ToySpecRateEngine.CALLS)
            n_eval = len(R["_A"].ids) * len(R["_A"].pk)
            ok(calls and set(calls) == {1.25, 1.0} and calls.count(1.0) == n_eval,
               f"EP23 direct path: every engine run (calibration fit, point, draws, stability refits) receives the spec "
               f"value; only the decision test's one evaluation runs at the card value ({len(calls)} runs, "
               f"{calls.count(1.0)} at the card value, {n_eval} expected)")
            t = {x["test_id"]: x for x in R["decision_tests"]}.get("DTs") or {}
            ok(t.get("status") == "run" and (t.get("spec_value") or {}).get("card_value") == 1.0,
               f"EP24 the direct-path decision test records the spec and card values: {t.get('spec_value')}")
            u = R["uncertainty"]
            ok("held fixed at the spec value: scale" in u["label"], f"EP25 the direct-path label: {u['label']}")
    guard("EP23-EP25 direct path", direct_cases)

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if sys.argv[1:2] == ["benchmarks"]:
        sys.exit(_cli(sys.argv[2:]))
    print(__doc__)
