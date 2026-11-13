#!/usr/bin/env python3
"""Benchmark coverage: which package components each engine benchmark speaks to (owner decisions 2026-11-12/13).

    python3 toolkit/engines/benchmark_coverage.py --self-test

An engine is not eligible for a spec when a benchmark covering a component value the spec uses is not `in_band`
in the engine's verified benchmark record, or when a used component has no covering benchmark, unless the
owner-signed spec declares a departure for it (`engine_departures`). The check runs at the engine choice, when
the spec is signed and when the analysis validates the spec (`assess`, `spec_problems`).

Each engine with a benchmark pack keeps `benchmark_coverage.yaml` beside its module (yaml: it does not change the
engine's code fingerprint):

    engine: <card name>                         # must equal the engine assessed
    absent_values: {<component>: [<value meaning "not delivered">]}
    benchmarks:                                 # exactly the pack's benchmark ids
      <id>: {covers: {<component>: [<values>]}, estimand: <ESTIMANDS>, versus: {<component>: <value>},
             match: any | all, effect_parameters: [<engine parameter>], cost_drivers: [<engine cost driver>]}
      <id>: {covers: {}, covers_none_because: <text>}
    components_without_benchmark: {<component>: {reason: <text>, effect_parameters: [...]}}   # these block
    effects_from_spec: {<component> | "*": <reason>}    # the spec supplies the effect: listed, not gating

Rules. Only `in_band` passes: `out_of_band` contradicts the trial; `not_assessable` gives no evidence;
`band_mis_based` says the band cannot be met at the pack's prices. Both roles gate: a held-out benchmark is
validation (its result stands, no parameter is tuned toward it) and a fit target out of band means the fit
failed. `match: all` applies only when some package the spec allows holds every covered component. A
`cost_per_outcome` benchmark gates a component only when the spec charges it through an engine cost output
(a per_outcome cost item whose `analysis.cost_outputs` driver is in the benchmark's `cost_drivers`); otherwise it is
reported. The record is the engine's verified record for the configuration in use (benchmark.verified_results);
the card's copied rows are never read. An engine with a pack and no coverage file is refused; only the exact
self-test toys are exempt.

A departure: {engine, benchmark | component, reason, decision_test}. It counts only when the spec is signed by
the owner (the sign-off record's signed_by), and only when its decision test varies the related effect: a
parameter_value test on one of the entry's effect_parameters, or an interaction_rescale test naming a spec
component that delivers the covered engine component. Nothing here names a disease.
"""
from __future__ import annotations

import hashlib
import itertools
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FILE = "benchmark_coverage.yaml"
ESTIMANDS = ("effect_vs_none", "effect_vs_component", "durability", "interaction", "cost_per_outcome")
ROLE_MEANING = {"held_out": "held-out validation; the result stands, no parameter is tuned toward it",
                "fit": "a fit target the fitted engine misses (the fit failed)",
                "unknown": "role not recorded"}
DEPARTURE_FIELDS = ("engine", "benchmark", "component", "reason", "decision_test", "range_fraction")
OVERRIDES: dict = {}          # self-tests only: engine ref -> coverage file


def canonical(ref: str) -> tuple[str, str]:
    """(module:Class where the class is defined, its directory): an alias import resolves to the same class, so it
    reads the same coverage file and pack (#215 review round 3)."""
    import inspect
    from toolkit.spec import schema
    cls = schema.engine_class(ref)
    return f"{cls.__module__}:{cls.__name__}", os.path.dirname(os.path.abspath(inspect.getfile(cls)))


def path_for(ref: str) -> str:
    """The coverage file beside the module that defines the engine class (OVERRIDES: self-tests)."""
    if ref in OVERRIDES:
        return OVERRIDES[ref]
    try:
        c, d = canonical(ref)
    except Exception:  # noqa: BLE001 -- a reference that does not resolve: its own path (then no file: refused)
        return os.path.join(ROOT, *str(ref).split(":", 1)[0].split(".")[:-1], FILE)
    return OVERRIDES.get(c) or os.path.join(d, FILE)


def load(path: str) -> tuple[dict | None, list[str]]:
    """(coverage, problems); (None, []) when the file does not exist."""
    if not os.path.exists(path):
        return None, []
    try:
        doc = yaml.safe_load(open(path)) or {}
    except (OSError, yaml.YAMLError) as e:
        return {}, [f"{FILE} unreadable: {e}"]
    return doc, problems(doc)


def _txt(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def problems(doc) -> list[str]:
    """Shape problems of a coverage document (fail closed: any problem makes the engine ineligible)."""
    if not isinstance(doc, dict) or not _txt(doc.get("engine")) or not isinstance(doc.get("benchmarks"), dict):
        return [f"{FILE}: needs engine (the card name) and a benchmarks mapping"]
    bad = []
    for k in ("absent_values", "components_without_benchmark", "effects_from_spec"):
        if not isinstance(doc.get(k, {}), dict):
            bad.append(f"{FILE}: {k} must be a mapping")
    for c, e in (doc.get("components_without_benchmark") or {}).items():
        if not (isinstance(e, dict) and _txt(e.get("reason"))):
            bad.append(f"{FILE}: components_without_benchmark.{c} needs a reason")
    bad += [f"{FILE}: effects_from_spec.{c} needs a reason" for c, w in (doc.get("effects_from_spec") or {}).items()
            if not _txt(w)]
    for i, e in doc["benchmarks"].items():
        cov = e.get("covers") if isinstance(e, dict) else None
        if not isinstance(cov, dict) or not all(isinstance(v, list) and v for v in cov.values()):
            bad.append(f"{FILE}: {i}: covers must map components to non-empty value lists")
        elif not cov and not _txt(e.get("covers_none_because")):
            bad.append(f"{FILE}: {i}: a benchmark that covers no component says why (covers_none_because)")
        elif cov and e.get("estimand") not in ESTIMANDS:
            bad.append(f"{FILE}: {i}: estimand {e.get('estimand')!r} not in {ESTIMANDS}")
        elif cov and e.get("estimand") == "effect_vs_component" and not isinstance(e.get("versus"), dict):
            bad.append(f"{FILE}: {i}: an effect against another component names it (versus)")
        elif cov and e.get("estimand") == "cost_per_outcome" and not e.get("cost_drivers"):
            bad.append(f"{FILE}: {i}: a cost-per-outcome benchmark names the engine cost drivers it prices")
        elif e.get("match", "any") not in ("any", "all"):
            bad.append(f"{FILE}: {i}: match must be any or all")
    return bad


# ------------------------------------------------------------------ what the spec uses
def _packages(raw: dict) -> list:
    """The packages the spec allows: the effective set (schema.Spec.packages(), after envelope pruning) when the
    caller gives it (raw["_packages"]), else explicit or generated with at most one component per group."""
    if raw.get("_packages") is not None:
        return [set(p) for p in raw["_packages"]]
    from toolkit.alloc import costs
    o = raw.get("options") or {}
    comps = o.get("components") or {s: {} for s in (o.get("engine_map") or {})}
    return [set(p) for p in costs.packages(dict(o, components=comps,
                                                packages=o.get("packages") or {"generate": True})).values()]


def _smap(raw: dict, absent: dict) -> dict:
    """spec component -> {(engine component, value)} it delivers (absent values dropped), for the spec components
    some allowed package holds (review round 2: a component in no package is never delivered)."""
    em = (raw.get("options") or {}).get("engine_map") or {}
    inpk = set().union(*_packages(raw)) if em else set()
    return {s: {(c, v) for c, v in (m or {}).items() if v not in (absent.get(c) or [])}
            for s, m in em.items() if s in inpk}


def _drivers(raw: dict) -> set:
    """Engine cost drivers the spec charges anywhere (per_outcome cost items through analysis.cost_outputs, whichever
    component owns the item: review round 2)."""
    outs = (raw.get("analysis") or {}).get("cost_outputs") or {}
    return {(outs.get(it.get("outcome")) or {}).get("cost_driver")
            for it in (raw.get("options") or {}).get("cost_items") or []
            if isinstance(it, dict) and it.get("basis") == "per_outcome"} - {None}


def _hits(entry: dict, smap: dict, raw: dict) -> list:
    """(spec component, engine component, value) the entry applies to; [] when it does not apply."""
    cov = entry.get("covers") or {}
    hits = [(s, c, v) for s, pairs in smap.items() for c, v in pairs if v in (cov.get(c) or [])]
    if entry.get("match") == "all" and hits:
        need = set(cov)
        ok = any(need <= {c for s, c, _v in hits if s in pk} for pk in _packages(raw))
        return hits if ok else []
    return hits


def _vt(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def effect_parameters(entry: dict, version: str) -> list:
    """The entry's effect parameters for an engine version: a name, or {name, from_version} (a parameter added in a
    later engine version counts only from it)."""
    out = []
    for p in (entry or {}).get("effect_parameters") or []:
        if isinstance(p, dict):
            if _vt(version) >= _vt(p.get("from_version", "0")):
                out.append(p.get("name"))
        else:
            out.append(p)
    return out


def _param_problems(cov: dict, version: str, names: set) -> list:
    ents = list(cov["benchmarks"].items()) + list((cov.get("components_without_benchmark") or {}).items())
    return [f"{FILE}: {k}: effect parameter {p!r} is not a parameter of engine version {version}"
            for k, e in ents for p in effect_parameters(e, version) if p not in names]


# ------------------------------------------------------------------ departures
def departure_problems(raw: dict) -> list[str]:
    """Shape of the spec's `engine_departures` (each names an existing decision test)."""
    deps = raw.get("engine_departures")
    if deps is None:
        return []
    if not isinstance(deps, list):
        return ["engine_departures must be a list"]
    tests = {str(t.get("id")) for t in ((raw.get("analysis") or {}).get("decision_tests") or []) if isinstance(t, dict)}
    bad, seen = [], set()
    for j, d in enumerate(deps):
        if not isinstance(d, dict):
            bad.append(f"engine_departures[{j}] must be a mapping")
            continue
        bad += [f"engine_departures[{j}]: unknown field {k}" for k in d if k not in DEPARTURE_FIELDS]
        bad += [f"engine_departures[{j}]: {k} must be a non-empty text" for k in ("engine", "reason")
                if not _txt(d.get(k))]
        if _txt(d.get("benchmark")) == _txt(d.get("component")):
            bad.append(f"engine_departures[{j}]: name a benchmark or a component (exactly one)")
        if str(d.get("decision_test")) not in tests:
            bad.append(f"engine_departures[{j}]: decision_test {d.get('decision_test')!r} must be an "
                       "analysis.decision_tests id (a departure names the decision test that varies the effect)")
        rf = d.get("range_fraction")
        if rf is not None and not (isinstance(rf, (int, float)) and 0 < rf <= 1):
            bad.append(f"engine_departures[{j}]: range_fraction must be a number in (0, 1]")
        key = (d.get("engine"), d.get("benchmark") or d.get("component"))
        if key in seen:
            bad.append(f"engine_departures[{j}]: a second departure for {key[1]} of {key[0]}")
        seen.add(key)
    return bad


def signature(run_dir: str | None, by: str | None = None) -> str:
    """"signed" when the run's spec is signed by the owner and unchanged, "proposed" when it has no sign-off yet
    (or `by` is the owner about to sign), else "unsigned" (a lead's signature, or a spec changed since)."""
    if by is not None:
        return "proposed" if by == "owner" else "unsigned"
    if not run_dir:
        return "unsigned"
    from toolkit.spec import signoff
    rec = signoff._spec_record(run_dir)
    if rec is None:
        return "proposed"
    try:
        sha = hashlib.sha256(open(os.path.join(run_dir, signoff.SPEC), "rb").read()).hexdigest()
    except OSError:
        return "unsigned"
    return "signed" if rec.get("signed_by") == "owner" and (rec.get("sha256") or {}).get(signoff.SPEC) == sha \
        else "unsigned"


# ------------------------------------------------------------------ the check
def gate(cov, cov_problems, engine_name, raw, record, departures, departures_status, pack=None,
         has_benchmarks: bool = False, runs=None, version: str = "0", param_names=None) -> dict:
    """Benchmark precondition for one engine. `record()` returns (rows, problem) for the configuration in use;
    `pack` is the pack the coverage file was written for; `runs(test_id, effect, rows)` -> (ok, why) says whether a
    departure's decision test runs and changes the effect (toolkit/spec/preconditions.decision_test_runs).
    Returns {why_not, departures, blocking, reported, uncovered, record}."""
    out = {"why_not": [], "departures": [], "blocking": [], "reported": [], "uncovered": {}, "record": None,
           "card_rows_disagree": []}
    if cov is None:
        if pack is not None or has_benchmarks:
            out["why_not"].append(f"the engine has a benchmark pack but no {FILE} saying which components it covers")
        return out
    if cov_problems:
        out["why_not"] += cov_problems
        return out
    if cov["engine"] != engine_name:
        out["why_not"].append(f"{FILE} is for engine {cov['engine']!r}, not {engine_name!r}")
        return out
    entries = cov["benchmarks"]
    decl = {b.get("id"): b for b in (pack or {}).get("benchmarks") or [] if isinstance(b, dict)}
    if pack is not None and sorted(map(str, decl)) != sorted(map(str, entries)):
        out["why_not"].append(f"{FILE} does not map exactly the pack's benchmarks (missing "
                              f"{sorted(set(decl) - set(entries))}, extra {sorted(set(entries) - set(decl))})")
        return out
    if param_names is not None:
        pp = _param_problems(cov, version, set(param_names))
        if pp:
            out["why_not"] += pp
            return out
    smap = _smap(raw, cov.get("absent_values") or {})
    used = {(c, v) for pairs in smap.values() for c, v in pairs}
    deps = {(d.get("benchmark") or d.get("component")): d for d in departures or []
            if isinstance(d, dict) and d.get("engine") == engine_name}
    pending = []          # (key, why_not text, effect parameters, spec components, failing record)
    covered = {(c, v) for e in entries.values() for c, vals in (e.get("covers") or {}).items() for v in vals}
    nob, efs = cov.get("components_without_benchmark") or {}, cov.get("effects_from_spec") or {}
    for c in sorted({c for c, v in used - covered}):
        if c in efs or "*" in efs:
            out["uncovered"][c] = efs.get(c, efs.get("*"))
            continue
        e = nob.get(c)
        kind = (f"component {c}: no benchmark covers its effect ({e['reason']})" if e else
                f"component {c}: {FILE} names no benchmark for it")
        pending.append((c, kind + "; the engine is not eligible for it without an owner-signed departure naming a "
                        "decision test on its effect", effect_parameters(e, version),
                        {s for s, pairs in smap.items() if any(cc == c for cc, _ in pairs)},
                        {"benchmark_status": "no benchmark", "role": "none", "components": [c]}, {}))
    drivers = _drivers(raw)
    relevant = {}
    for i, e in entries.items():
        h = _hits(e, smap, raw)
        if not h:
            continue
        if e.get("estimand") == "cost_per_outcome" and not drivers & set(e.get("cost_drivers")):
            out["reported"].append({"id": i, "why": "the spec prices the component itself, not through this "
                                                     "engine cost output"})
            continue
        relevant[i] = h
    rows = None
    if relevant or out["reported"] or pending:
        rows, why = record()
        out["record"] = why or "verified"
        if why and (relevant or out["reported"]):
            out["why_not"].append(f"benchmark record refused ({why}): the benchmarks covering "
                                  f"{', '.join(sorted(relevant) or [r['id'] for r in out['reported']])} cannot be "
                                  "read for this engine configuration")
            return out
        rows = None if why else rows
    if rows is not None:
        ids = [r.get("id") for r in rows]
        bad = []
        if len(ids) != len(set(ids)):
            bad.append(f"the benchmark record lists an id twice ({sorted({i for i in ids if ids.count(i) > 1})})")
        elif set(ids) != set(entries):
            bad.append(f"the benchmark record's ids differ from {FILE} (record only "
                       f"{sorted(set(ids) - set(entries))}, coverage only {sorted(set(entries) - set(ids))})")
        elif pack is not None:          # review round 2: bind the coverage to the declarations actually verified
            from toolkit.engines.benchmark_sources import band_digest
            off = sorted(r["id"] for r in rows if r.get("band_sha256") != band_digest(decl[r["id"]], pack))
            if off:
                bad.append(f"the benchmark record scored {off} against another declaration than the pack {FILE} "
                           "was written for (same id, different band, arms or setting)")
        if bad:                         # fail closed; the uncovered components below are still named
            out["why_not"] += bad
            rows, relevant = None, {}
    if rows is not None:
        byid = {r["id"]: r for r in rows}
        for rp in out["reported"]:
            rp.update(status=byid[rp["id"]].get("status"), role=byid[rp["id"]].get("role", "unknown"))
        for i, hits in sorted(relevant.items()):
            r = byid[i]
            st, role = r.get("status", "missing"), r.get("role", "unknown")
            if st == "in_band":
                continue
            comps = sorted({f"{c}={v}" for _s, c, v in hits})
            pending.append((i, f"benchmark {i} ({r.get('metric', '')}; role {role}: "
                               f"{ROLE_MEANING.get(role, ROLE_MEANING['unknown'])}) covers {', '.join(comps)} and is "
                               f"{st} (computed {r.get('computed')}, band {r.get('band')}): the engine is not eligible "
                               "for it without an owner-signed engine_departures entry",
                            effect_parameters(entries[i], version), {s for s, _c, _v in hits},
                            {"benchmark_status": st, "role": role, "components": comps},
                            {"benchmark": i, "estimand": entries[i].get("estimand"),
                             "pair": {c: {s for s, cc, _v in hits if cc == c} for c in (entries[i].get("covers") or {})}}))
    counts = departures_status in ("signed", "proposed")
    for key, kind, params, comps, failing, ctx in pending:
        d = deps.get(key)
        if d is not None and counts:
            ok, why = (runs or (lambda *a: (False, "no decision-test check available")))(
                d.get("decision_test"), dict(ctx, parameters=params, components=comps,
                                             range_fraction=d.get("range_fraction")), rows)
            if ok:
                out["departures"].append(dict(d, status=departures_status, **failing))
                continue
            kind += f"; its departure does not count: decision test {d.get('decision_test')!r} {why}"
        elif d is not None:
            kind += "; its departure does not count: the spec is not signed by the owner (or changed since)"
        out["why_not"].append(kind)
        if failing["role"] != "none":
            out["blocking"].append({"id": key, "role": failing["role"], "status": failing["benchmark_status"]})
    return out


def _pack(ref: str) -> dict | None:
    """The benchmark pack the coverage file was written for (beside it)."""
    p = os.path.join(os.path.dirname(path_for(ref)), "benchmarks", "pack.yaml")
    return (yaml.safe_load(open(p)) or {}) if os.path.exists(p) else None


def assess(ref: str, config: dict, raw: dict, departures_status: str, engine_name: str | None = None,
           card_benchmarks=None, base: str | None = None) -> dict:
    """The check for one engine reference and configuration (the one the run uses)."""
    from toolkit.engines import benchmark_sources as BS
    try:
        cref = canonical(ref)[0]
    except Exception:  # noqa: BLE001
        cref = ref
    cov, probs = load(path_for(ref))
    if cov is None and cref in BS.TOY_ENGINE_REFS and cref not in OVERRIDES and ref not in OVERRIDES:
        return {"why_not": [], "departures": [], "blocking": [], "reported": [], "uncovered": {}, "record": "toy",
                "card_rows_disagree": []}
    cache = {}

    def engine():
        if "e" not in cache:
            from toolkit.spec import schema
            cache["e"] = schema.engine_class(ref)(**(config or {}))
        return cache["e"]

    def record():
        if "r" not in cache:
            try:
                from toolkit.engines import benchmark as B
                doc, why = B.verified_results(engine())
                cache["r"] = (doc["benchmarks"], None) if not why else ([], why)
            except Exception as e:  # noqa: BLE001 -- fail closed
                cache["r"] = ([], f"{type(e).__name__}: {e}")
        return cache["r"]
    try:
        card = engine().describe()
        name, version = engine_name or card.name, card.version
        pnames = [p.name for p in engine().parameters()]
    except Exception as e:  # noqa: BLE001
        return {"why_not": [f"the engine cannot describe itself: {type(e).__name__}: {e}"], "departures": [],
                "blocking": [], "reported": [], "uncovered": {}, "record": None, "card_rows_disagree": []}

    def runs(test_id, effect, rows):
        from toolkit.spec import preconditions as PC, schema
        sp = schema.Spec({k: v for k, v in raw.items() if k != "_packages"} | {"engine": {"ref": ref, "config":
                                                                                         config or {}}},
                         base or os.getcwd())
        return PC.decision_test_runs(sp, test_id, dict(effect, rows=rows))
    rp = getattr(engine(), "results_path", None)        # the engine's effective results file, its default included
    try:          # the effective packages the run will allocate over (after envelope pruning)
        from toolkit.spec import schema
        if "packages" not in (raw.get("options") or {}):
            raise KeyError("no packages declared")      # a bare filter spec: every combination (_packages)
        raw = dict(raw, _packages=list(schema.Spec(dict(raw, engine={"ref": ref, "config": config or {}}),
                                                   base or os.getcwd()).packages().values()))
    except Exception:  # noqa: BLE001 -- a spec without the sections pruning needs: the declared packages
        pass
    out = gate(cov, probs, name, raw, record, list(raw.get("engine_departures") or []), departures_status,
               _pack(ref), has_benchmarks=bool(rp and os.path.exists(str(rp))), runs=runs, version=version,
               param_names=pnames)
    if card_benchmarks and out["record"] == "verified":
        rows = {r.get("id"): r for r in record()[0]}
        out["card_rows_disagree"] = sorted(str(c.get("id")) for c in card_benchmarks if isinstance(c, dict)
                                           and c.get("id") in rows and (c.get("status"), c.get("computed")) !=
                                           (rows[c["id"]].get("status"), rows[c["id"]].get("computed")))
    return out


def departure_targets(raw: dict, ref: str, base: str | None = None) -> dict:
    """{departure key: {components, benchmark, estimand, pair}}: the spec components each departure's effect lives in
    (a benchmark's covered pairs, or the engine component no benchmark covers), over the effective packages. Read by
    the run's check of what each departure's test did (decision_tests.check_departures)."""
    cov, _p = load(path_for(ref))
    if not cov:
        return {}
    try:
        from toolkit.spec import schema
        raw = dict(raw, _packages=list(schema.Spec(raw, base or os.getcwd()).packages().values()))
    except Exception:  # noqa: BLE001
        pass
    smap = _smap(raw, cov.get("absent_values") or {})
    out = {}
    for i, e in (cov.get("benchmarks") or {}).items():
        h = _hits(e, smap, raw)
        out[i] = {"components": {s for s, _c, _v in h}, "benchmark": i, "estimand": e.get("estimand"),
                  "pair": {c: {s for s, cc, _v in h if cc == c} for c in (e.get("covers") or {})}}
    for s_, pairs in smap.items():
        for c, _v in pairs:
            out.setdefault(c, {"components": set(), "benchmark": None, "estimand": None, "pair": {}})
            if out[c]["benchmark"] is None:
                out[c]["components"].add(s_)
    return out


def spec_problems(s, by: str | None = None) -> list[str]:
    """The check for a spec that names its engine (schema validation, sign-off, the analysis): problems, prefixed."""
    e = s.raw.get("engine")
    ref, cfg = (e.get("ref"), e.get("config") or {}) if isinstance(e, dict) else (e, {})
    if not isinstance(ref, str) or ":" not in ref:
        return []
    raw = dict(s.raw, options=s.raw.get("options") or {})
    out = assess(ref, cfg, raw, signature(getattr(s, "base", None), by), base=getattr(s, "base", None))
    return [f"engine benchmarks: {w}" for w in out["why_not"]]


# ------------------------------------------------------------------ sheet and results
def sheet_lines(raw: dict, candidates: list | None) -> list[str]:
    """The sign-off sheet's section: each departure the spec declares, with what the candidates' records show, and
    the benchmarks reported but not gating."""
    deps = [d for d in raw.get("engine_departures") or [] if isinstance(d, dict)]
    rows = {(r.get("name"), d.get("benchmark") or d.get("component")): d
            for r in candidates or [] for d in r.get("departures") or []}
    rep = [(r.get("name"), b) for r in candidates or [] for b in r.get("benchmarks_reported") or []]
    out = []
    if deps:
        out += ["## Engine departures from benchmark evidence",
                "Signing as the owner accepts that the engine is used for each component below although its "
                "benchmark against the trial is not in band, or no benchmark covers it; a lead's signature does not."]
        for d in deps:
            key = d.get("benchmark") or d.get("component")
            seen = rows.get((d.get("engine"), key))
            what = (f" Covers {', '.join(seen['components'])}; the record shows {seen['benchmark_status']}; role: "
                    f"{ROLE_MEANING.get(seen.get('role'), seen.get('role'))}." if seen
                    else " Not needed by any candidate engine's record, or its decision test does not vary the effect.")
            out.append(f"- **{key}** (engine {d.get('engine')}): {d.get('reason')} (decision test "
                       f"{d.get('decision_test')}).{what}")
        out.append("")
    if rep:
        out += ["## Benchmarks reported, not gating", ""]
        out += [f"- {b['id']} (engine {n}): {b.get('status')}; {b['why']}." for n, b in rep] + [""]
    return out


def results_lines(raw: dict, run_dir: str | None = None) -> list[str]:
    """results.md: the signed departures, as declared limitations, and the reported cost benchmarks."""
    import json
    deps = [d for d in raw.get("engine_departures") or [] if isinstance(d, dict)]
    rep = []
    try:
        ch = json.load(open(os.path.join(run_dir, "engine_choice.json"))) if run_dir else {}
        ref = (ch.get("engine") or {}).get("ref")
        rep = next((r.get("benchmarks_reported") or [] for r in ch.get("candidates") or [] if r.get("ref") == ref), [])
    except (OSError, ValueError, AttributeError):
        pass
    out = []
    if deps:
        out += ["## Declared engine departures (signed by the owner in the spec)", "",
                "The engine was used for components whose benchmark against the trial is not in band, or that no "
                "benchmark covers. A held-out benchmark's result stands as validation (no parameter was tuned toward "
                "it); a fit target out of band means the engine's fit missed it:", ""]
        out += [f"- {d.get('benchmark') or d.get('component')} (engine {d.get('engine')}): {d.get('reason')} "
                f"(decision test {d.get('decision_test')})." for d in deps] + [""]
    if rep:
        out += ["Cost-per-outcome benchmarks reported, not gating (the spec prices these components itself): "
                + "; ".join(f"{b['id']} {b.get('status')}" for b in rep) + ".", ""]
    return out


# ------------------------------------------------------------------ self-test
def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    eng = os.path.join(ROOT, "toolkit", "engines")
    for name in sorted(os.listdir(eng)):
        d = os.path.join(eng, name)
        pp = os.path.join(d, "benchmarks", "pack.yaml")
        if not os.path.exists(pp):
            continue
        pack = yaml.safe_load(open(pp)) or {}
        ids = [b["id"] for b in pack.get("benchmarks") or []]
        doc, bad = load(os.path.join(d, FILE))
        miss = [i for i in ids if i not in ((doc or {}).get("benchmarks") or {})]
        extra = [i for i in ((doc or {}).get("benchmarks") or {}) if i not in ids]
        card = yaml.safe_load(open(os.path.join(d, "card.yaml"))) or {}
        ok(doc is not None and not bad and not miss and not extra and doc.get("engine") == card.get("name"),
           f"BC1 {name}: {FILE} is for this engine and maps every benchmark of its pack and no other ({bad[:2]}, "
           f"missing {miss}, extra {extra})")
        comps = set(card.get("components") or {})
        named = {c for e in (doc or {}).get("benchmarks", {}).values() for c in (e.get("covers") or {})}
        named |= set((doc or {}).get("components_without_benchmark") or {})
        named |= set((doc or {}).get("effects_from_spec") or {}) - {"*"}
        ok(named <= comps, f"BC2 {name}: every component the coverage names is a card component ({named - comps})")
        drv = {b["id"]: sorted(set((b["estimand"].get("costs") or {})) | set(b["estimand"].get("costs_num") or {}))
               for b in pack.get("benchmarks") or [] if b["estimand"].get("type") == "cost_per_outcome"}
        got = {i: sorted(e.get("cost_drivers") or []) for i, e in (doc or {}).get("benchmarks", {}).items()
               if e.get("estimand") == "cost_per_outcome"}
        ok(drv == got, f"BC2b {name}: each cost benchmark's cost_drivers are the pack's priced drivers ({drv}, {got})")
    ok(problems({"engine": "x", "benchmarks": {"A": {"covers": {}}}}) != [],
       "BC3 a benchmark covering nothing without saying why is refused")
    ok(problems({"engine": "x", "benchmarks": {"A": {"covers": {"c": [1]}, "estimand": "vibes"}}}) != [],
       "BC4 an unknown estimand is refused")
    cov = {"engine": "e", "benchmarks": {"I": {"covers": {"p": [1], "q": [1]}, "match": "all",
                                               "estimand": "interaction"},
                                         "P": {"covers": {"p": [1]}, "estimand": "effect_vs_none"},
                                         "Q": {"covers": {"q": [1]}, "estimand": "effect_vs_none"}}}
    rows = [{"id": "I", "status": "out_of_band"}, {"id": "P", "status": "in_band"}, {"id": "Q", "status": "in_band"}]
    rec = lambda: (rows, None)  # noqa: E731
    both = {"options": {"engine_map": {"sp": {"p": 1}, "sq": {"q": 1}}, "packages": {"generate": True}}}
    excl = {"options": {"engine_map": {"sp": {"p": 1}, "sq": {"q": 1}},
                        "components": {"sp": {"group": "g"}, "sq": {"group": "g"}}, "packages": {"generate": True}}}
    ok(gate(cov, [], "e", {"options": {"engine_map": {"sp": {"p": 1}}}}, rec, [], "unsigned")["why_not"] == [],
       "BC5 an interaction benchmark (match all) does not apply when only one of its components is used")
    ok([b["id"] for b in gate(cov, [], "e", both, rec, [], "unsigned")["blocking"]] == ["I"],
       "BC6 it applies when a package can hold both")
    ok(gate(cov, [], "e", excl, rec, [], "unsigned")["why_not"] == [],
       "BC6c it does not apply when no package holds both (mutually exclusive alternatives)")
    ok(gate(None, [], "e", both, rec, [], "unsigned", pack=None)["why_not"] == [] and
       gate(None, [], "e", both, rec, [], "unsigned", pack={"benchmarks": [{"id": "I"}]})["why_not"] != [],
       "BC7 no coverage file: refused when the engine has a benchmark pack, whatever the card copies")
    ok("not 'f'" in " ".join(gate(cov, [], "f", both, rec, [], "unsigned")["why_not"]),
       "BC7b a coverage file for another engine is refused")
    dup = lambda: (rows + [{"id": "I", "status": "in_band"}], None)  # noqa: E731
    ok(any("twice" in w for w in gate(cov, [], "e", both, dup, [], "unsigned")["why_not"]),
       "BC7c a record listing a benchmark id twice is refused (a passing row cannot hide a failing one)")
    ok(any("pack" in w for w in gate(cov, [], "e", both, rec, [], "unsigned", pack={"benchmarks": [{"id": "I"}, {"id": "P"}]})["why_not"]),
       "BC7d coverage ids must equal the pack's")
    dep = {"engine": "e", "benchmark": "I", "reason": "why", "decision_test": "DT"}
    md = "\n".join(results_lines({"engine_departures": [dep]}))
    ok("Declared engine departures" in md and "decision test DT" in md and results_lines({}) == []
       and sheet_lines({}, None) == [], "BC8 results.md declares each signed departure; nothing when there is none")
    ok(any("decision_test" in p for p in departure_problems({"engine_departures": [dict(dep, decision_test="X")],
                                                             "analysis": {"decision_tests": [{"id": "DT"}]}}))
       and any("unknown field" in p for p in departure_problems({"engine_departures": [
           dict(dep, no_decision_test_because="none")], "analysis": {"decision_tests": [{"id": "DT"}]}})),
       "BC9 a departure must name an existing decision test; no reason-for-none escape")
    # review round 2: every declared effect parameter exists in the engine version its record is for
    from toolkit.engines.malaria.engine import MalariaEngine
    me = MalariaEngine()
    mdoc, _ = load(os.path.join(eng, "malaria", FILE))
    bad = _param_problems(mdoc, me.describe().version, {x.name for x in me.parameters()})
    ok(not bad and "use_rate" in effect_parameters(mdoc["benchmarks"]["B8"], me.describe().version)
       and "net_kill_shift" not in effect_parameters(mdoc["benchmarks"]["B8"], "0.16")
       and "net_kill_shift" in effect_parameters(mdoc["benchmarks"]["B8"], "0.17"),
       f"BC10 the malaria coverage's effect parameters exist in the engine; net_kill_shift counts from v0.17 ({bad})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
