#!/usr/bin/env python3
"""Benchmark bands checked against their published intervals (F6 S1-2).

    python3 -m toolkit.engines.benchmark_sources <pack.yaml>    # exit 1 and list each finding
    python3 toolkit/engines/benchmark_sources.py --self-test

A trial or cost benchmark (estimand type ratio, odds_ratio, interaction_or or cost_per_outcome) declares the estimand its
source reports and where its band comes from:

    estimand_type: odds_ratio | risk_ratio | rate_ratio | hazard_ratio | cost_per_outcome     # always
    estimand_mapping: "<why the engine's estimand stands for the source's>"   # when they do not match
    published: {lower, upper, level, scale: ratio | currency_per_outcome, table: "<where in the source>", unit}
    diagnostic, age_band: "<as the source states them; 'mixed' or 'not reported' when so>"   # with a published ratio

or, when the band is not the published interval:

    band_override: {band: [lo, hi], reason: "<why>", level: <optional>, status: <optional>}

The band is derived: `band_override.band` when present, else [published.lower, published.upper]. The stored `band`
field stays in the pack (every reader reads it there); this check fails on any stored band that differs from its
derivation, on an entry with neither source nor override, on a malformed entry or a missing or unknown estimand type,
on a source estimand the engine's estimand does not match without a mapping note (override or not), and on a
`requires` interval that differs from the referenced published interval.

Exemptions, listed: the estimand types in EXEMPT_TYPES (oracle or rule checks, no trial interval), and entries with no
`published` block in a pack whose engine is exactly one of the self-test toys in TOY_ENGINE_REFS (their bands are test
fixtures; a subclass is not exempt).

Threat model (S1-2b): stale or changed artifacts and declarations on disk, and code paths that read evidence without the
loader. Deliberate in-process mutation by harness code is out of scope: the snapshot stores deep copies and returns
detached copies, and consumers carry the values they captured instead of rereading them, but nothing is frozen.

Stored evidence is bound to the declarations it was scored against: results rows carry `band_sha256`
(band_digest), fitted.yaml carries `band_declarations` for its fit targets; `stale_ids`, `fitted_problems` and
the one loader `load_evidence` returns an immutable snapshot (S1-2b) whose `problems` list a stale row ("rerun
required") or fitted values without current provenance ("refit required"). Disease-agnostic: nothing here names a
disease.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from statistics import NormalDist

if not __package__:  # run as a file (the self-test runner): make the repository importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

SOURCED_TYPES = ("ratio", "odds_ratio", "interaction_or", "cost_per_outcome")
EXEMPT_TYPES = ("value", "identity", "coverage", "curve_rules")     # oracle and rule checks: no published interval
TOY_ENGINE_REFS = ("toolkit.engines.contract:ToyEngine", "toolkit.engines.benchmark:SharedStateToy",
                   "toolkit.spec.toy_engines:ToySpecEngine", "toolkit.spec.toy_engines:ToySpecRateEngine",
                   "toolkit.spec.priors:ToyPriorEngine", "toolkit.engines.hooks:ToyScheduleEngine")
ESTIMAND_TYPES = ("odds_ratio", "risk_ratio", "rate_ratio", "hazard_ratio", "cost_per_outcome")
# engine estimand type -> source estimand types it stands for without a mapping note
COMPATIBLE = {"odds_ratio": {"odds_ratio"}, "interaction_or": {"odds_ratio"}, "ratio": {"risk_ratio", "rate_ratio"},
              "cost_per_outcome": {"cost_per_outcome"}}
SCALE_OF = {"ratio": "ratio", "odds_ratio": "ratio", "interaction_or": "ratio", "cost_per_outcome": "currency_per_outcome"}
SCALES = tuple(sorted(set(SCALE_OF.values())))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# what the oracle and coverage checks exercise beyond their oracle and calibrator modules (Codex r2 on #158)
OBSERVATION_FILES = (os.path.join(_REPO, "toolkit", "analysis", "observation.py"),
                     os.path.join(_REPO, "toolkit", "analysis", "thresholds.yaml"))
DIGEST_FIELDS = ("id", "target", "band", "published", "band_override", "estimand_type", "estimand_mapping", "requires")
TOL = 1e-9


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pair(x) -> bool:
    return isinstance(x, (list, tuple)) and len(x) == 2 and all(_num(v) for v in x) and x[0] <= x[1]


def _text(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _etype(b) -> str | None:
    e = b.get("estimand") if isinstance(b, dict) else None
    return e.get("type") if isinstance(e, dict) else None


def published_problems(p, etype: str | None = None) -> list[str]:
    if not isinstance(p, dict):
        return ["published must be a mapping {lower, upper, level, scale, table}"]
    bad = []
    if not (_num(p.get("lower")) and _num(p.get("upper")) and p["lower"] <= p["upper"]):
        bad.append("published needs numbers lower <= upper")
    if not (_num(p.get("level")) and 0 < p["level"] < 1):
        bad.append("published.level must be a number in (0, 1), e.g. 0.95")
    want = SCALE_OF.get(etype) if etype else None
    if p.get("scale") not in SCALES or (want and p.get("scale") != want):
        bad.append(f"published.scale must be {want or SCALES} for an engine {etype!r} estimand")
    if not _text(p.get("table")):
        bad.append("published.table must say where in the source the interval is")
    return bad


def derive_band(b: dict):
    """The band a benchmark's sources give: the override's band, else the published interval, else None."""
    o = b.get("band_override")
    if isinstance(o, dict) and _pair(o.get("band")):
        return [float(o["band"][0]), float(o["band"][1])]
    p = b.get("published")
    if isinstance(p, dict) and not published_problems(p):
        return [float(p["lower"]), float(p["upper"])]
    return None


def band_level(b: dict):
    """The interval level of a benchmark's band: the override's stated level, else the published level, else None."""
    o = b.get("band_override")
    if isinstance(o, dict):
        return o.get("level") if _num(o.get("level")) and 0 < o["level"] < 1 else None
    p = b.get("published")
    return p.get("level") if isinstance(p, dict) and not published_problems(p) else None


def band_sd(b: dict, tr) -> float:
    """The sd of a normal whose central interval at the band's level is the band, on the scale `tr`. ValueError when the
    band has no stated level (an override without one is not an interval)."""
    lv = band_level(b)
    if lv is None:
        raise ValueError(f"{b.get('id')}: band {b.get('band')} has no stated interval level")
    lo, hi = b["band"]
    return (float(tr(hi)) - float(tr(lo))) / (2 * NormalDist().inv_cdf(0.5 + lv / 2))


def _same(a, b) -> bool:
    return _pair(a) and _pair(b) and abs(a[0] - b[0]) <= TOL and abs(a[1] - b[1]) <= TOL


def check_benchmark(b, byid: dict | None = None, toy: bool = False) -> list[str]:
    if not isinstance(b, dict):
        return [f"benchmark entry {b!r} is not a mapping"]
    bid = b.get("id")
    etype = _etype(b)
    if etype in EXEMPT_TYPES or (toy and b.get("published") is None):
        return []
    if etype not in SOURCED_TYPES:
        return [f"{bid}: estimand type {etype!r} is neither checked against a source {SOURCED_TYPES} nor exempt "
                f"{EXEMPT_TYPES}"]
    pub, ovr = b.get("published"), b.get("band_override")
    if pub is None and ovr is None:
        return [f"{bid}: band {b.get('band')} has neither a published interval nor a recorded band_override"]
    bad = []
    st = b.get("estimand_type")
    if st not in ESTIMAND_TYPES:
        bad.append(f"{bid}: estimand_type {st!r} must be one of {ESTIMAND_TYPES} (the estimand the source reports)")
    elif st not in COMPATIBLE[etype] and not _text(b.get("estimand_mapping")):
        bad.append(f"{bid}: the source reports a {st} but the engine computes {etype!r}; refused without an "
                   "estimand_mapping note")
    if pub is not None:
        pp = published_problems(pub, etype)
        bad += [f"{bid}: {x}" for x in pp]
        if not pp and _num(b.get("target")) and not (pub["lower"] <= b["target"] <= pub["upper"]):
            bad.append(f"{bid}: target {b['target']} is outside the published interval [{pub['lower']}, {pub['upper']}]")
        if SCALE_OF[etype] == "ratio":
            bad += [f"{bid}: a published ratio states its {k} ('mixed' or 'not reported' when so)"
                    for k in ("diagnostic", "age_band") if not _text(b.get(k))]
    if ovr is not None:
        if not (isinstance(ovr, dict) and _pair(ovr.get("band"))):
            bad.append(f"{bid}: band_override needs band: [lo, hi]")
        elif not _text(ovr.get("reason")):
            bad.append(f"{bid}: band_override needs a reason")
    want = derive_band(b)
    if want is not None and not _same(b.get("band"), want):
        src = "band_override" if ovr is not None else "published interval"
        bad.append(f"{bid}: band {b.get('band')} differs from its {src} {want} with no recorded override")
    for r, iv in (b.get("requires") or {}).items():
        rp = ((byid or {}).get(r) or {}).get("published")
        if isinstance(rp, dict) and not published_problems(rp) and not _same(iv, [rp["lower"], rp["upper"]]):
            bad.append(f"{bid}: requires {r} {iv} differs from {r}'s published interval [{rp['lower']}, {rp['upper']}]")
    return bad


def check_pack(pack, toy: bool | None = None) -> list[str]:
    """Every source finding. `toy`: whether the pack's verified engine is an exact self-test toy (load_evidence passes
    it from the engine's class); None derives it from the pack's own `engine` field (the standalone check)."""
    if not isinstance(pack, dict) or not isinstance(pack.get("benchmarks"), list):
        return ["pack has no benchmarks list"]
    toy = pack.get("engine") in TOY_ENGINE_REFS if toy is None else toy
    byid = {b.get("id"): b for b in pack["benchmarks"] if isinstance(b, dict)}
    return [x for b in pack["benchmarks"] for x in check_benchmark(b, byid, toy)]


def failing_ids(pack: dict) -> set:
    bs = {b.get("id"): b for b in pack.get("benchmarks") or []}
    return {bid for bid, b in bs.items() if check_benchmark(b, bs)}


# ---- stored evidence bound to the declarations it was scored against
def _sha(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


def band_digest(b: dict, pack: dict) -> str:
    """sha256 of a benchmark's declaration as resolved in `pack`, for every estimand type (S1-2b): its band and source
    fields, its whole engine estimand (type, observable, arms, sides), and the arms, settings, configurations,
    horizon, seeds and engine it runs with. A change of any of them, or of the type, invalidates stored evidence."""
    from toolkit.engines.benchmark import _arms_of, _config_name, _walk_values
    arms = {a: (pack.get("arms") or {}).get(a) for a in _arms_of(b.get("estimand") or {}) if isinstance(a, str)}
    sets = {s: (pack.get("settings") or {}).get(s) for s in sorted({(v or {}).get("setting") for v in arms.values()}
                                                                    - {None})}
    names = {_config_name(pack, st or {}) for st in sets.values()} | {v for v in _walk_values(b) if isinstance(v, str)}
    cfgs = {c: (pack.get("configs") or {})[c] for c in sorted(names - {None}) if c in (pack.get("configs") or {})}
    e = b.get("estimand") or {}
    code = {"oracles": _module_sha("oracle", pack.get("oracles"))}
    if isinstance(e.get("calibrator"), dict):
        code["calibrator"] = _module_sha("calibrator", str(e["calibrator"].get("fn", "")).split(":")[0])
    from toolkit.engines.benchmark import COMPARE_TYPES
    if e.get("type") in COMPARE_TYPES + ("coverage",):
        code["observation"] = [_file_sha(f) for f in OBSERVATION_FILES]
    return _sha({"fields": {k: b.get(k) for k in DIGEST_FIELDS}, "estimand": e, "arms": arms,
                 "settings": sets, "configs": cfgs, "default_config": pack.get("default_config"),
                 "horizon": pack.get("horizon"), "seeds": pack.get("seeds"), "engine": pack.get("engine"),
                 "coverage_scoring": pack.get("coverage_scoring"), "code": code})


def _file_sha(f: str) -> str:
    try:
        return hashlib.sha256(open(f, "rb").read()).hexdigest()
    except OSError:
        return f"missing:{os.path.basename(f)}"


def _module_sha(kind: str, name) -> str | None:
    """sha256 of an oracle or calibrator module's file, resolved through the import guard; a name it refuses or cannot
    resolve is recorded as such (so the digest still changes with the name)."""
    if not name:
        return None
    from toolkit import import_guard as G
    try:
        f = getattr(G.module(kind, name), "__file__", None)
        return hashlib.sha256(open(f, "rb").read()).hexdigest() if f else f"unresolved:{name}"
    except Exception:  # noqa: BLE001 -- recorded in the digest, never silently skipped
        return f"unresolved:{name}"


def band_declarations(pack: dict, ids=None) -> dict:
    """{id: band_digest} of the pack's benchmarks (only `ids` when given)."""
    return {b["id"]: band_digest(b, pack) for b in pack.get("benchmarks") or []
            if isinstance(b, dict) and (ids is None or b["id"] in ids)}


def fit_declarations(pack: dict) -> dict:
    """What a fit is bound to: each fit target's declaration and the fit rule itself (box, grid, design)."""
    from toolkit.engines.benchmark import fit_targets
    return dict(band_declarations(pack, fit_targets(pack)),
                fit_rule=_sha({"fit_joint": pack.get("fit_joint"), "fit": pack.get("fit")}))


def stale_ids(pack: dict, rows: list) -> list:
    """Ids of stored rows not scored against the pack's current declaration, or not in the pack. Every row must carry
    the digest of its declaration (no legacy acceptance: a row without one is stale, oracle rows included)."""
    from toolkit.engines.benchmark import calibrator_code_digest, declaration_digest
    cur = {b.get("id"): b for b in pack.get("benchmarks") or [] if isinstance(b, dict)}
    out = []
    for r in rows:
        b = cur.get(r.get("id")) if isinstance(r, dict) else None
        if b is None or r.get("band_sha256") != band_digest(b, pack):
            out.append(r.get("id") if isinstance(r, dict) else r)
        elif _etype(b) == "coverage":            # its own declaration and calibration-code digests too
            try:
                ok_ = (r.get("declaration_sha256") == declaration_digest(b, pack)
                       and r.get("code_sha256") == calibrator_code_digest(pack, b["estimand"]))
            except (KeyError, TypeError, ValueError):
                ok_ = False
            if not ok_:
                out.append(b["id"])
    return out


def fitted_problems(fitted: dict, pack: dict) -> list[str]:
    """["refit required: <ids>"] when fitted values were fitted against other declarations of the pack's fit targets
    or another fit rule (or carry none); else []."""
    want = fit_declarations(pack)
    have = (fitted or {}).get("band_declarations") or {}
    ids = sorted(k for k in set(want) | set(have) if have.get(k) != want.get(k))     # the target set and each digest
    return [f"refit required: {', '.join(ids)}"] if ids else []


# ---- the one validated evidence snapshot (S1-2b): every consumer reads benchmark evidence through load_evidence
class EvidenceError(ValueError):
    """Benchmark evidence that cannot be used: the message lists every reason."""


class Evidence:
    """An immutable, validated snapshot of one engine's benchmark evidence: the pack's declarations, the fitted values
    the engine actually uses with the digest of the file that backs them, and (when loaded) the stored result rows.
    `problems` lists every reason the evidence cannot be used; `require()` raises them. Accessors return copies."""
    __slots__ = ("_pack", "_rows", "_fitted", "fitted_sha256", "decl_problems", "fit_problems", "toy",
                 "engine_identity")

    def __init__(self, pack, rows, fitted, fitted_sha256, decl_problems, fit_problems=(), toy=False,
                 engine_identity=None):
        import copy
        for k, v in (("_pack", copy.deepcopy(pack)), ("_rows", copy.deepcopy(rows)), ("_fitted", dict(fitted)),
                     ("fitted_sha256", fitted_sha256), ("decl_problems", tuple(decl_problems)),
                     ("fit_problems", tuple(fit_problems)), ("toy", toy), ("engine_identity", engine_identity)):
            object.__setattr__(self, k, v)

    @property
    def problems(self) -> tuple:
        """The declarations' and rows' problems, then the fitted values' ('refit required')."""
        return self.decl_problems + self.fit_problems

    def __setattr__(self, k, v):
        raise AttributeError("Evidence is immutable")

    @property
    def pack(self) -> dict:
        import copy
        return copy.deepcopy(self._pack)

    @property
    def rows(self) -> list:
        import copy
        return copy.deepcopy(self._rows)

    @property
    def fitted(self) -> dict:
        """{name: (lo, hi, value)} of the fitted parameters in use, in engine order."""
        return dict(self._fitted)

    def require(self) -> "Evidence":
        if self.problems:
            raise EvidenceError("; ".join(self.problems))
        return self


def _fitted_in_use(engine, pack, toy: bool) -> tuple:
    """(fitted values in use, digest of the file backing them, problems). The values are what the engine's
    parameters() reports now (cached or defaults), checked against the current fitted file: a fitted parameter whose
    value or range is not the file's, a missing or unreadable file, or a file fitted against other declarations is
    'refit required'. An engine without fitted parameters needs no file; a self-test toy needs none unless it names one."""
    import yaml
    eff = {p.name: (p.lo, p.hi, p.value) for p in engine.parameters() if getattr(p, "kind", None) == "fitted"}
    fp = getattr(engine, "fitted_path", None)
    if not eff or (toy and not fp):
        return eff, None, []
    if not fp or not os.path.isfile(fp):          # none configured, missing, or a dangling link
        return eff, None, [f"refit required: no fitted values file backs the engine's fitted parameters ({fp})"]
    if pack is None:
        return eff, None, [f"refit required: fitted values {fp} have no readable benchmark pack to be checked against"]
    try:
        raw = open(fp, "rb").read()
        doc = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError) as e:
        return eff, None, [f"refit required: fitted values {fp} are unreadable ({e})"]
    probs = fitted_problems(doc, pack)
    fparams = doc.get("parameters") or {}

    def same(x, y):
        return _num(x) and _num(y) and abs(float(x) - float(y)) <= 1e-12 * max(1.0, abs(float(y)))
    diff = sorted(n for n, (lo, hi, v) in eff.items()
                  if not (isinstance(fparams.get(n), dict) and same(fparams[n].get("value"), v)
                          and all(k not in fparams[n] or same(fparams[n][k], x) for k, x in (("lo", lo), ("hi", hi)))))
    if diff:
        probs.append(f"refit required: the fitted values in use for {', '.join(diff)} are not {os.path.basename(fp)}'s "
                     "(cached before the file changed, or engine defaults)")
    return eff, hashlib.sha256(raw).hexdigest(), probs


def load_evidence(engine, pack: dict | None = None, rows: list | None = None, partial: bool = False) -> Evidence:
    """The one loader of benchmark evidence. `pack`: the declarations the rows were scored against (default: the
    engine's own pack; its absence is permitted only for an engine with no rows to check and no fitted values to back;
    an unreadable pack is always a problem). `rows`: stored result rows to validate (None: fitted values only); they
    must cover every benchmark of the pack unless `partial`. Problems: a pack that cannot be read, is not a non-empty
    benchmark list or fails the source check; missing rows and rows scored against other declarations ('rerun
    required'); fitted values without current provenance ('refit required'). Only an exact self-test toy may have
    no pack."""
    from toolkit.engines.contract import engine_identity
    ref = f"{type(engine).__module__}:{type(engine).__name__}"
    toy = ref in TOY_ENGINE_REFS
    probs = []
    if pack is not None and isinstance(pack, dict) and pack.get("engine") != ref:   # an explicit pack, bound to the class
        probs.append(f"the pack is for engine {pack.get('engine')!r}, not the engine supplied ({ref})")
        toy = False
    if pack is None:
        from toolkit.spec import engine_parameters as EP
        try:
            pack, why = EP.benchmark_pack(engine)
        except ValueError as e:                     # unreadable or malformed: never permitted
            pack, why = {}, None
            probs.append(f"the engine's benchmark pack cannot be read: {e}")
        if pack is None and rows is not None and not toy:
            probs.append(f"no benchmark pack to check the stored rows against ({why})")
    if pack is not None and not (isinstance(pack, dict) and isinstance(pack.get("benchmarks"), list)
                                 and pack["benchmarks"]):
        probs.append("the benchmark pack is not a mapping with a non-empty benchmarks list")
        pack = pack if isinstance(pack, dict) else {}
    if pack is not None:
        probs += [x for x in check_pack(pack, toy=toy) if x != "pack has no benchmarks list"]
    if rows is not None and pack is not None:       # an absent pack: refused above unless an exact toy's
        if not (isinstance(rows, list) and all(isinstance(r, dict) and r.get("id") for r in rows)):
            probs.append("the stored rows are not a list of mappings with ids")
            rows = []
        ids = [b.get("id") for b in (pack or {}).get("benchmarks") or [] if isinstance(b, dict)]
        stale = stale_ids(pack or {}, rows) + ([] if partial else [i for i in ids if i not in {r["id"] for r in rows}])
        if stale:
            probs.append(f"rerun required: {', '.join(str(x) for x in stale)}")
    fitted, fsha, fprobs = _fitted_in_use(engine, pack, toy)
    try:
        ident = engine_identity(engine)
    except Exception:  # noqa: BLE001 -- an object without the contract's describe(): no identity to bind
        ident = None
    return Evidence(pack or {}, list(rows or []), fitted, fsha, probs, fprobs, toy, ident)


def main(argv=None) -> int:
    import yaml
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(__doc__)
        return 2
    try:
        pack = yaml.safe_load(open(argv[0]))
    except (OSError, yaml.YAMLError) as ex:
        print(f"cannot read {argv[0]}: {ex}")
        return 1
    bad = check_pack(pack if isinstance(pack, dict) else {})
    for x in bad:
        print("FINDING " + x)
    print(f"{len(bad)} finding(s)" if bad else "every band matches its source or a recorded override")
    return 1 if bad else 0


def _self_test() -> int:
    import yaml
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    pub = {"lower": 0.30, "upper": 0.67, "level": 0.95, "scale": "ratio", "table": "abstract"}
    good = {"id": "A", "target": 0.45, "band": [0.30, 0.67], "published": pub, "estimand_type": "odds_ratio",
            "diagnostic": "RDT", "age_band": "not reported",
            "estimand": {"type": "odds_ratio"}}
    ok(check_benchmark(good) == [], "BS1 a band equal to its published interval passes")
    sym = dict(good, band=[0.23, 0.67])
    ok(any("differs from its published interval" in x for x in check_benchmark(sym)),
       "BS2 a symmetric band wider than the published interval fails")
    ok(any("neither a published interval" in x for x in check_benchmark({"id": "N", "target": 1, "band": [0, 2],
                                                                         "estimand": {"type": "ratio"}})),
       "BS3 fail closed: a trial benchmark with no published interval and no override fails")
    ovr = dict(sym, band_override={"band": [0.23, 0.67], "reason": "deliberately wider: heterogeneity"})
    ok(check_benchmark(ovr) == [], "BS4 a band different from the published interval passes with a recorded override")
    ok(any("needs a reason" in x for x in check_benchmark(dict(ovr, band_override={"band": [0.23, 0.67]}))),
       "BS5 an override without a reason fails")
    ok(any("differs from its band_override" in x for x in check_benchmark(dict(ovr, band=[0.3, 0.67]))),
       "BS6 a band that differs from its own override fails")
    hr = dict(good, estimand_type="hazard_ratio", estimand={"type": "ratio"})
    ok(any("refused without an estimand_mapping" in x for x in check_benchmark(hr)),
       "BS7 a published hazard ratio against an engine ratio is refused without a mapping note")
    ok(check_benchmark(dict(hr, estimand_mapping="engine rate ratio over the follow-up stands for the HR")) == [],
       "BS8 ... and allowed with an explicit mapping note")
    ok(any("refused" in x for x in check_benchmark(dict(good, estimand_type="risk_ratio"))),
       "BS9 a published risk ratio against an engine odds ratio is refused")
    ok(check_benchmark(dict(good, estimand_type="rate_ratio", estimand={"type": "ratio"})) == [],
       "BS10 a rate ratio against an engine ratio is allowed")
    ok(any("estimand_type" in x for x in check_benchmark({k: v for k, v in good.items() if k != "estimand_type"})),
       "BS11 a published interval without an estimand type fails")
    ok(any("published.table" in x for x in check_benchmark(dict(good, published=dict(pub, table="")))),
       "BS12 a published interval without its location in the source fails")
    ok(any("outside the published" in x for x in check_benchmark(dict(good, target=0.8))),
       "BS13 a target outside its published interval fails")
    ok(check_benchmark({"id": "V", "band": [0, 9], "estimand": {"type": "value"}}) == [],
       "BS14 oracle and rule types (value, identity, coverage, curve_rules) are out of scope")
    req = {"id": "I", "target": 2.0, "band": [1.0, 4.0], "band_override": {"band": [1.0, 4.0], "reason": "r"},
           "requires": {"A": [0.23, 0.67]}, "estimand": {"type": "interaction_or"}}
    ok(any("requires A" in x for x in check_pack({"benchmarks": [good, req]})),
       "BS15 a requires interval that differs from the referenced published interval fails")
    ok(check_pack({}) == ["pack has no benchmarks list"], "BS16 fail closed: a pack without benchmarks is a finding")
    ok(derive_band(good) == [0.30, 0.67] and derive_band(ovr) == [0.23, 0.67] and derive_band({"id": "x"}) is None,
       "BS17 derive_band: override first, then the published interval")
    # the real packs: every band matches its source or a recorded override
    root = os.path.dirname(os.path.abspath(__file__))
    mp = os.path.join(root, "malaria", "benchmarks", "pack.yaml")
    pk = yaml.safe_load(open(mp))
    bad = check_pack(pk)
    ok(bad == [], "BS18 the malaria pack: every band matches its published interval or a recorded override"
       + ("" if not bad else f" (failing: {sorted(failing_ids(pk))}; " + "; ".join(bad) + ")"))
    for name in ("compartmental", "renewal"):
        p = os.path.join(root, name, "benchmarks", "pack.yaml")
        if os.path.exists(p):
            ok(check_pack(yaml.safe_load(open(p))) == [], f"BS19 the {name} pack passes (oracle types only)")
    # the runner CLI and the harness step refuse a pack whose band differs from its source, before any run
    import tempfile
    from toolkit.engines import benchmark as BM
    from toolkit.spec import engine_parameters as EP
    toy = {"engine": "toolkit.engines.contract:ToyEngine", "seeds": [1],
           "initialisation": {"method": "closed form", "evidence": ["not applicable: no dynamic state"]},
           "horizon": {"start": 0, "stop": 3, "window": [0, 3], "windows": {"w": [1, 2]}},
           "settings": {"s": {"unit": {"x": 0.0}}},
           "arms": {"none": {"setting": "s", "package": {}}, "a": {"setting": "s", "package": {"a": 1}}},
           "benchmarks": [{"id": "T1", "metric": "a vs none", "target": 0.4, "band": [0.3, 0.5],
                           "published": dict(pub, lower=0.3, upper=0.5), "estimand_type": "risk_ratio",
                           "diagnostic": "mixed", "age_band": "mixed",
                           "estimand": {"type": "ratio", "num": "a", "den": "none", "value": "per_capita.cases.all"}}],
           "fit": [{"param": "eff_a", "grid": [0.3, 0.5], "targets": ["T1"]}]}
    wide = dict(toy, benchmarks=[dict(toy["benchmarks"][0], band=[0.2, 0.5])])
    with tempfile.TemporaryDirectory() as d:
        rc = {}
        for name, pk_ in (("ok", toy), ("wide", wide)):
            path = os.path.join(d, name + ".yaml")
            yaml.safe_dump(pk_, open(path, "w"))
            rc[name] = BM.main([path, "validate"])
    ok(rc == {"ok": 0, "wide": 2}, f"BS20 the runner CLI validates a matching band and refuses a wider one ({rc})")
    try:
        EP.spec_benchmarks(wide, None, None)
        refused = False
    except ValueError as ex:
        refused = "differs from its published" in str(ex)
    ok(refused, "BS21 the harness step (spec_benchmarks) refuses the pack before it touches the engine or spec")
    from toolkit.analysis import joint_calibration as JC
    orig = EP.benchmark_pack
    try:
        EP.benchmark_pack = lambda eng: (wide, None)
        jw = JC._benchmarks(BM.load_engine(toy["engine"]))
        EP.benchmark_pack = lambda eng: (toy, None)
        jo = JC._benchmarks(BM.load_engine(toy["engine"]))
    finally:
        EP.benchmark_pack = orig
    ok(jw[0] is None and "not usable" in str(jw[2]) and jo[0] == toy and jo[1] == {"T1"},
       "BS22 joint calibration reads a band as a 95% interval only when it matches its source")

    # ---- review round 1 (Codex, PR #154)
    def case(name, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 -- a case that raises is a failing case
            ok(False, f"{name} raised {type(ex).__name__}: {ex}")

    eng = BM.load_engine(toy["engine"])

    def r1_1():
        ok(any("differs from its published" in x for x in BM.validate(wide, eng)),
           "BS23 validate() itself refuses a band that differs from its source (direct API call)")
        try:
            BM.Runner(wide, eng, 1)
            ok(False, "BS24 Runner() refuses a pack whose band differs from its source")
        except ValueError as ex:
            ok("differs from its published" in str(ex), "BS24 Runner() refuses a pack whose band differs from its source")
    case("BS23-24", r1_1)

    def r1_5():
        nosrc = {"id": "T5", "target": 0.4, "band": [0.3, 0.5], "estimand": {"type": "ratio"}}
        ok(check_pack(dict(toy, benchmarks=[nosrc])) == [] and check_pack(dict(toy, engine="pkg.real:Engine",
                                                                              benchmarks=[nosrc])) != [],
           f"BS25 the listed toy engines {TOY_ENGINE_REFS} are the only pack-level exemption")
        ok(check_pack({"benchmarks": [None]}) != [] and check_pack({"benchmarks": [{"id": "x"}]}) != []
           and check_pack({"benchmarks": [{"id": "x", "estimand": {"type": "new_kind"}}]}) != [],
           "BS26 a malformed entry, a missing estimand type and an unknown one are findings")
        ok(set(EXEMPT_TYPES) == {"value", "identity", "coverage", "curve_rules"},
           f"BS27 the exempt estimand types are listed explicitly: {EXEMPT_TYPES}")
    case("BS25-27", r1_5)

    def r1_4():
        o = {"id": "H", "target": 0.5, "band": [0.4, 0.7], "estimand": {"type": "ratio"}, "estimand_type": "hazard_ratio",
             "band_override": {"band": [0.4, 0.7], "reason": "r"}}
        ok(any("refused without an estimand_mapping" in x for x in check_benchmark(o)),
           "BS28 an override does not bypass the estimand check: a hazard ratio still needs a mapping note")
        ok(any("estimand_type" in x for x in check_benchmark({k: v for k, v in o.items() if k != "estimand_type"})),
           "BS29 every trial or cost entry declares the estimand its source reports")
    case("BS28-29", r1_4)

    def r1_3():
        tr = math.log
        b90 = dict(good, published=dict(pub, level=0.90))
        sd = band_sd(b90, tr)
        ok(abs(sd - (math.log(0.67) - math.log(0.30)) / (2 * 1.6448536)) < 1e-6,
           f"BS30 the interval sd uses the recorded level (0.90 -> z 1.645): {sd:.4f}")
        try:
            band_sd(dict(ovr), tr)
            ok(False, "BS31 an override band with no stated level has no sd (refused)")
        except ValueError:
            ok(True, "BS31 an override band with no stated level has no sd (refused)")
        import inspect
        ok("band_sd" in inspect.getsource(JC) and "1.959964" not in inspect.getsource(JC),
           "BS32 joint calibration computes a benchmark target's sd with band_sd, not a fixed 95% z")
    case("BS30-32", r1_3)

    def r1_6_7():
        cost = {"id": "C", "target": 19.0, "band": [1, 105], "estimand": {"type": "cost_per_outcome"},
                "estimand_type": "cost_per_outcome",
                "published": {"lower": 1, "upper": 105, "level": 0.95, "scale": "currency_per_outcome",
                              "table": "abstract", "unit": "USD per DALY averted"}}
        ok(check_benchmark(cost) == [], "BS33 a published cost interval (currency per outcome) is recorded and derived")
        ok(check_benchmark(dict(cost, published=dict(cost["published"], scale="ratio"))) != [],
           "BS34 a ratio scale on a cost benchmark is refused")
        ok(any("diagnostic" in x for x in check_benchmark({k: v for k, v in good.items() if k != "diagnostic"}))
           and any("age_band" in x for x in check_benchmark({k: v for k, v in good.items() if k != "age_band"})),
           "BS35 a published ratio states its diagnostic and age band ('mixed' or 'not reported' when so)")
    case("BS33-35", r1_6_7)

    def r1_2():
        with tempfile.TemporaryDirectory() as d:
            pk_ = dict(toy, benchmarks=toy["benchmarks"])
            yaml.safe_dump(pk_, open(os.path.join(d, "pack.yaml"), "w"))
            from toolkit.engines.contract import ToyEngine
            res = BM.stamp(BM.run_pack(pk_, eng, BM.Runner(pk_, eng, 1), {}), eng)
            ok(all(r.get("band_sha256") == band_digest(b, pk_) for r, b in zip(res["benchmarks"], pk_["benchmarks"])),
               "BS36 results rows carry the digest of their band declaration")
            rp = os.path.join(d, "results.yaml")
            yaml.safe_dump(res, open(rp, "w"))
            rows, why = BM.verified_benchmarks(ToyEngine(results_path=rp))
            ok(rows and why is None, f"BS37 results whose band declarations match the pack beside them are used ({why})")
            new = dict(toy["benchmarks"][0], band=[0.35, 0.5], published=dict(toy["benchmarks"][0]["published"], lower=0.35))
            pk2 = dict(pk_, benchmarks=[new])
            yaml.safe_dump(pk2, open(os.path.join(d, "pack.yaml"), "w"))
            rows, why = BM.verified_benchmarks(ToyEngine(results_path=rp))
            ok(rows == [] and "T1" in str(why), f"BS38 results for another band declaration are refused ({why})")
            try:
                BM.merge_results(pk2, [res], eng)
                ok(False, "BS39 merge refuses rows computed for another band declaration")
            except ValueError as ex:
                ok("T1" in str(ex), f"BS39 merge refuses rows computed for another band declaration ({ex})")
            for prev in (pk_, pk2):
                try:
                    BM.rescore_results(pk2, res, prev, eng)
                    ok(False, "BS40 rescore refuses a row scored against another band declaration")
                except ValueError as ex:
                    ok("rerun required: T1" in str(ex), f"BS40 rescore refuses a stale row: {ex}")
        fd = {"band_declarations": fit_declarations(toy)}
        ok(fitted_problems(fd, toy) == [] and fitted_problems(fd, pk2) == ["refit required: T1"]
           and fitted_problems({}, toy) == ["refit required: T1, fit_rule"],
           "BS42 fitted values bound to their targets' declarations: a changed or missing digest is 'refit required'")
        mf = yaml.safe_load(open(os.path.join(root, "malaria", "benchmarks", "fitted.yaml")))
        ok(fitted_problems(mf, pk) == ["refit required: B10, B11, B12, B12b, B12m, fit_rule"],
           f"BS43 the stored malaria fitted values report refit required: {fitted_problems(mf, pk)}")
    case("BS36-43", r1_2)

    def r1_2b():
        from toolkit.analysis import batch as Bt

        import types

        class Stale:
            fitted_path = None

            def parameters(self):
                return [types.SimpleNamespace(name="k", kind="fitted", lo=0.0, hi=1.0, value=0.5)]
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "fitted.yaml")
            yaml.safe_dump({"parameters": {}, "band_declarations": {"T1": "old"}}, open(fp, "w"))
            s = Stale()
            s.fitted_path = fp
            orig = EP.benchmark_pack
            try:
                EP.benchmark_pack = lambda e: (toy, "pack.yaml")
                probs = list(load_evidence(s).problems)
                try:
                    Bt.fitted_ranges(s)
                    raised = False
                except ValueError as ex:
                    raised = "refit required: T1" in str(ex)
            finally:
                EP.benchmark_pack = orig
        ok(probs[0] == "refit required: T1, fit_rule" and raised,
           "BS44 an engine whose fitted values are stale: every analysis read of them (fitted_ranges) is refused")
        import inspect
        from toolkit.analysis import run as RUNm
        ok("load_evidence" in inspect.getsource(RUNm._run),
           "BS45 the analysis entry blocks on stale fitted values before the joint calibration or the batch")
    case("BS44-45", r1_2b)

    # ---- review round 2 (Codex, PR #154)
    def r2_6():
        fd2 = {"band_declarations": dict(fit_declarations(toy), T9="a former fit target")}
        ok(fitted_problems(fd2, toy) == ["refit required: T9"],
           f"BS46 a fit whose recorded target set differs from the current one needs a refit: {fitted_problems(fd2, toy)}")
    case("BS46", r2_6)

    def r2_5():
        import types

        class NoPack:
            def parameters(self):
                return [types.SimpleNamespace(name="k", kind="fitted", lo=0.0, hi=1.0, value=0.5)]
        with tempfile.TemporaryDirectory() as d:
            e = NoPack()
            e.fitted_path = os.path.join(d, "fitted.yaml")
            yaml.safe_dump({"parameters": {}}, open(e.fitted_path, "w"))
            orig = EP.benchmark_pack
            try:
                EP.benchmark_pack = lambda en: (None, "no pack")
                probs = list(load_evidence(e).problems)
            finally:
                EP.benchmark_pack = orig
        ok(len(probs) == 1 and "no readable benchmark pack" in probs[0],
           f"BS47 fitted values with no readable pack to check them against are unusable evidence: {probs}")
    case("BS47", r2_5)

    def r2_4():
        from toolkit.analysis import estimates as ES
        from toolkit.engines.contract import code_fingerprint
        from toolkit.engines.malaria.engine import MalariaEngine
        me = MalariaEngine()
        doc = {"producer": BM.PRODUCER, "engine_identity": {"code_sha256": code_fingerprint(me)},
               "benchmarks": [{"id": b["id"], "status": "in_band", "band_sha256": band_digest(b, pk)} for b in pk["benchmarks"]]}
        doc["benchmarks"][[b["id"] for b in pk["benchmarks"]].index("B10")]["band_sha256"] = "an older declaration"
        try:
            ES.benchmark_acceptance(me, doc=doc)
            ok(False, "BS48 estimates acceptance refuses statuses scored against another band declaration")
        except ValueError as ex:
            ok("rerun required: B10" in str(ex), f"BS48 estimates acceptance refuses a stale status: {ex}")
    case("BS48", r2_4)

    # ---- review round 3 (Codex, PR #154)
    def r3_1():
        fd = {"band_declarations": fit_declarations(toy)}
        b0 = toy["benchmarks"][0]
        obs = dict(toy, benchmarks=[dict(b0, estimand=dict(b0["estimand"], value="windows.w.prev"))])
        sett = dict(toy, settings={"s": {"unit": {"x": 0.5}}})
        row = {"id": "T1", "band_sha256": band_digest(b0, toy)}
        ok(stale_ids(obs, [row]) == ["T1"] and fitted_problems(fd, obs) == ["refit required: T1"],
           "BS49 changing only the observable makes the stored row stale and the fit need a refit")
        ok(stale_ids(sett, [row]) == ["T1"] and fitted_problems(fd, sett) == ["refit required: T1"],
           "BS50 changing only an arm's setting makes the stored row stale and the fit need a refit")
        rule = dict(toy, fit=[{"param": "eff_a", "grid": [0.1, 0.9], "targets": ["T1"]}])
        ok(fitted_problems(fd, rule) != [], "BS51 changing the fit rule (grid, box, design) needs a refit")
    case("BS49-51", r3_1)

    def r3_2():
        from toolkit.engines.contract import ToyEngine
        with tempfile.TemporaryDirectory() as d:
            yaml.safe_dump(toy, open(os.path.join(d, "pack.yaml"), "w"))
            res = BM.stamp(BM.run_pack(toy, eng, BM.Runner(toy, eng, 1), {}), eng)
            rp = os.path.join(d, "results.yaml")
            yaml.safe_dump(res, open(rp, "w"))
            got = {}
            b0 = toy["benchmarks"][0]
            for name, pk_ in (("malformed", dict(toy, benchmarks=toy["benchmarks"] + [None])),
                              ("estimand", dict(toy, benchmarks=[dict(b0, estimand_type="hazard_ratio")]))):
                yaml.safe_dump(pk_, open(os.path.join(d, "pack.yaml"), "w"))
                got[name] = BM.verified_benchmarks(ToyEngine(results_path=rp))
            open(os.path.join(d, "pack.yaml"), "w").write(":: [")
            got["unreadable"] = BM.verified_benchmarks(ToyEngine(results_path=rp))
        ok(got["malformed"][0] == [] and "not a mapping" in str(got["malformed"][1])
           and got["estimand"][0] == [] and "estimand_mapping" in str(got["estimand"][1])
           and got["unreadable"][0] == [] and "unreadable" in str(got["unreadable"][1]),
           f"BS52 verified_benchmarks refuses a pack whose sources fail the check or that cannot be read: {got}")
    case("BS52", r3_2)

    def r3_3():
        import types

        class Fitted:
            def parameters(self):
                return [types.SimpleNamespace(name="k", kind="fitted", lo=0.0, hi=1.0, value=0.5)]

        class Unfitted:
            def parameters(self):
                return [types.SimpleNamespace(name="k", kind="fixed", lo=0.0, hi=1.0, value=0.5)]
        with tempfile.TemporaryDirectory() as d:
            gone = os.path.join(d, "fitted.yaml")
            link = os.path.join(d, "dangling.yaml")
            os.symlink(os.path.join(d, "nowhere.yaml"), link)
            res = {}
            for name, cls, fp in (("missing", Fitted, gone), ("dangling", Fitted, link), ("unfitted", Unfitted, gone)):
                e = cls()
                e.fitted_path = fp
                res[name] = list(load_evidence(e).problems)
        ok(all("refit required" in str(res[k]) for k in ("missing", "dangling")) and res["unfitted"] == [],
           f"BS53 a configured fitted file that is missing or a dangling link is unusable evidence for an engine with "
           f"fitted parameters; an engine with none is exempt: {res}")
    case("BS53", r3_3)

    # ---- S1-2b: one validated evidence snapshot (Codex round 4 reproductions first)
    def r4_1():
        rn = BM.Runner(toy, eng, 1)
        b0 = toy["benchmarks"][0]
        try:
            rn.evaluate(dict(b0, band=[0.2, 0.5]), {})
            ok(False, "BS54 Runner.evaluate refuses a declaration that is not its validated pack's")
        except ValueError as ex:
            ok("validated pack" in str(ex), f"BS54 Runner.evaluate refuses a declaration that is not its validated "
                                            f"pack's: {ex}")
        b_ = toy["benchmarks"][0]
        b_["band"] = [0.2, 0.5]                    # the caller's dict changes after the runner was built
        try:
            r = rn.evaluate(rn.pack["benchmarks"][0], {})
            ok(r["status"] in ("in_band", "out_of_band") and rn.pack["benchmarks"][0]["band"] == [0.3, 0.5],
               "BS55 the runner scores its own copy of the validated pack, whatever the caller does with its dict")
        finally:
            b_["band"] = [0.3, 0.5]
    case("BS54-55", r4_1)

    def r4_2():
        from toolkit.engines.contract import ToyEngine
        b0 = toy["benchmarks"][0]
        res = BM.stamp(BM.run_pack(toy, eng, BM.Runner(toy, eng, 1), {}), eng)
        as_value = dict(toy, benchmarks=[dict(b0, estimand={"type": "value", "not_transcribed": "changed type"})])
        with tempfile.TemporaryDirectory() as d:
            rp = os.path.join(d, "results.yaml")
            yaml.safe_dump(res, open(rp, "w"))
            yaml.safe_dump(as_value, open(os.path.join(d, "pack.yaml"), "w"))
            rows, why = BM.verified_benchmarks(ToyEngine(results_path=rp))
        ok(stale_ids(as_value, res["benchmarks"]) == ["T1"] and rows == [] and "rerun required: T1" in str(why),
           f"BS56 a change from a sourced to an exempt type makes the stored row stale (every type is digested): {why}")
        legacy = {"id": "T1", "compare": "eq", "computed": 1.0}
        ok(stale_ids(as_value, [legacy]) == ["T1"] and stale_ids(as_value, [{"id": "T1", "computed": 0.4}]) == ["T1"],
           "BS57 a stored row without a digest, an oracle row included, is stale (rerun required)")
    case("BS56-57", r4_2)

    def r4_3():
        import shutil
        mdir = os.path.join(root, "malaria", "benchmarks")
        calls = []
        orig = BM.run_pack
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(os.path.join(mdir, "equilibration.yaml"), os.path.join(d, "equilibration.yaml"))
            fpath = os.path.join(d, "elsewhere", "fitted.yaml")
            os.makedirs(os.path.dirname(fpath))
            yaml.safe_dump({"parameters": {}, "band_declarations": {"B10": "an older declaration"}}, open(fpath, "w"))
            yaml.safe_dump(dict(pk, configs={"stale": {"fitted_path": fpath}}, default_config="stale"),
                           open(os.path.join(d, "pack.yaml"), "w"))
            try:
                BM.run_pack = lambda *a, **k: calls.append(1) or {}
                rc = BM.main([os.path.join(d, "pack.yaml"), "run", "--workers", "1", "--seeds", "1"])
            finally:
                BM.run_pack = orig
        ok(rc == 2 and calls == [], f"BS58 the run CLI on a copied pack with no adjacent fit never runs on fitted values "
                                    f"without current provenance (rc {rc}, run_pack calls {len(calls)})")
    case("BS58", r4_3)

    def r4_4():
        from toolkit.analysis.batch import fitted_ranges
        from toolkit.engines.malaria.engine import MalariaEngine
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "fitted.yaml")
            doc = {"parameters": {"cfp_kill": {"value": 0.3, "lo": 0.2, "hi": 0.4},
                                  "irs_kill": {"value": 0.4, "lo": 0.3, "hi": 0.5}},
                   "band_declarations": fit_declarations(pk)}
            yaml.safe_dump(doc, open(fp, "w"))
            me = MalariaEngine(fitted_path=fp)
            ok(fitted_ranges(me)["cfp_kill"][2] == 0.3, "BS59 fitted values with current provenance are used")
            doc["parameters"]["cfp_kill"]["value"] = 0.9            # the file is replaced after the engine cached it
            yaml.safe_dump(doc, open(fp, "w"))
            try:
                fitted_ranges(me)
                ok(False, "BS60 cached fitted values that differ from their current file are refused")
            except ValueError as ex:
                ok("refit required" in str(ex), f"BS60 cached fitted values that differ from their current file are "
                                                f"refused: {ex}")
    case("BS59-60", r4_4)

    def r4_5():
        ev = load_evidence(eng, pack=toy)
        cp = ev.pack
        cp["benchmarks"][0]["band"] = [0.0, 9.0]
        ok(ev.pack["benchmarks"][0]["band"] == [0.3, 0.5] and ev.problems == () and set(ev.fitted) == {"eff_a", "eff_b"},
           "BS61 the evidence snapshot returns detached copies: a consumer's copy cannot change it")
    case("BS61", r4_5)

    # ---- S1-2b Codex round 1 (#158): behavioural bypass tests with self-contained stale fixtures
    from toolkit.engines.contract import ToyEngine

    class FittedToy(ToyEngine):          # a subclass of a listed toy: not exempt (P2-6)
        pass

    ftoy = dict(toy, engine=f"{FittedToy.__module__}:{FittedToy.__name__}")

    def stale_toy(d):
        fp = os.path.join(d, "fitted.yaml")
        yaml.safe_dump({"parameters": {}, "band_declarations": {"T1": "an older declaration"}}, open(fp, "w"))
        e = ToyEngine()                  # an exact toy that names a fitted file must back its values with it
        e.fitted_path = fp
        return e

    def b1():
        with tempfile.TemporaryDirectory() as d:
            st = stale_toy(d)
            try:
                BM.run_pack(toy, st, BM.Runner(toy, st, 1), {})
                ok(False, "BS62 run_pack refuses to score with fitted values without current provenance")
            except ValueError as ex:
                ok("refit required" in str(ex), f"BS62 run_pack refuses to score with stale fitted values: {ex}")
            rf = BM.Runner(toy, st, 1, mode="fit")          # fitting is the one mode that sets fitted values
            ok(rf.evaluate("T1", {"eff_a": 0.6})["computed"] is not None,
               "BS63 fitting runs in its own explicit mode, with candidate fitted values")
            e2 = FittedToy()
            fp2 = os.path.join(d, "current.yaml")
            yaml.safe_dump({"parameters": {k: {"value": v[2], "lo": v[0], "hi": v[1]}
                                           for k, v in load_evidence(eng, pack=toy).fitted.items()},
                            "band_declarations": fit_declarations(ftoy)}, open(fp2, "w"))
            e2.fitted_path = fp2
            rn = BM.Runner(ftoy, e2, 1)
            try:
                BM.run_pack(ftoy, e2, rn, {"eff_a": 0.65})
                ok(False, "BS64 scoring refuses caller fitted values that are not the evidence's")
            except ValueError as ex:
                ok("evidence" in str(ex), f"BS64 scoring refuses caller fitted values that are not the evidence's: {ex}")
        ok(any("refit required" in x for x in load_evidence(FittedToy(), pack=ftoy).problems),
           "BS65 only exact registered toys are exempt: a subclass with fitted parameters and no file needs a refit")
    case("BS62-65", b1)

    def b2():
        rn = BM.Runner(toy, eng, 1)
        view = rn.pack
        view["benchmarks"][0]["band"] = [0.2, 0.5]
        try:
            rn.evaluate(view["benchmarks"][0], {})
            ok(False, "BS66 the runner's validated declarations cannot be changed through its pack")
        except ValueError:
            ok(rn.pack["benchmarks"][0]["band"] == [0.3, 0.5],
               "BS66 the runner's validated declarations cannot be changed through its pack")
    case("BS66", b2)

    def b3():
        cov = {"id": "C1", "estimand": {"type": "coverage", "calibrator": {"fn": "toolkit.engines.benchmark:x"}}}
        p1 = dict(toy, benchmarks=[cov], coverage_scoring={"refusal_share_max": 0.02}, oracles="toolkit.engines.benchmark")
        row = {"id": "C1", "band_sha256": band_digest(cov, p1)}
        p2 = dict(p1, coverage_scoring={"refusal_share_max": 0.0})
        b0 = toy["benchmarks"][0]
        o1 = dict(toy, oracles="toolkit.engines.benchmark")
        ok(stale_ids(p2, [row]) == ["C1"] and band_digest(b0, o1) != band_digest(b0, dict(o1, oracles="toolkit.engines.contract")),
           "BS67 the digest covers pack-level coverage scoring and the oracle and calibrator modules' code")
    case("BS67", b3)

    def b4():
        from toolkit.spec import engine_parameters as EPm
        orig = EPm.benchmark_pack
        try:
            def broken(e):
                raise EPm.PackError("unreadable")
            EPm.benchmark_pack = broken
            unreadable = load_evidence(eng).problems
        finally:
            EPm.benchmark_pack = orig
        empty = load_evidence(eng, pack=dict(toy, benchmarks=[])).problems
        missing = load_evidence(eng, pack=toy, rows=[]).problems
        partial = load_evidence(eng, pack=toy, rows=[], partial=True).problems
        ok(unreadable and empty and any("rerun required: T1" in x for x in missing) and partial == (),
           f"BS68 an unreadable pack, an empty pack and missing rows are problems; partial rows are explicit: "
           f"{unreadable} | {empty} | {missing} | {partial}")
        with tempfile.TemporaryDirectory() as d:
            rp = os.path.join(d, "results.yaml")
            yaml.safe_dump(BM.stamp({"note": "no rows"}, eng), open(rp, "w"))
            yaml.safe_dump(toy, open(os.path.join(d, "pack.yaml"), "w"))
            rows, why = BM.verified_benchmarks(ToyEngine(results_path=rp))
        ok(rows == [] and why, f"BS69 a results file without a benchmarks list is refused: {why}")
    case("BS68-69", b4)

    # ---- S1-2b Codex round 2 (#158)
    def c1():
        global OBSERVATION_FILES
        from toolkit.engines.benchmark import calibrator_code_digest, declaration_digest
        cov = {"id": "C1", "estimand": {"type": "coverage", "calibrator": {"fn": "toolkit.engines.benchmark:x"}}}
        pc = dict(toy, benchmarks=[cov], oracles="toolkit.engines.benchmark")
        good = {"id": "C1", "band_sha256": band_digest(cov, pc), "declaration_sha256": declaration_digest(cov, pc),
                "code_sha256": calibrator_code_digest(pc, cov["estimand"])}
        ok(stale_ids(pc, [good]) == [] and stale_ids(pc, [dict(good, declaration_sha256="x")]) == ["C1"]
           and stale_ids(pc, [dict(good, code_sha256="x")]) == ["C1"],
           "BS70 a coverage row is checked on its own declaration and calibration-code digests too")
        val = {"id": "V1", "estimand": {"type": "value", "not_transcribed": "x"}}
        pv = dict(toy, benchmarks=[val])
        with tempfile.TemporaryDirectory() as d:
            files = [os.path.join(d, "observation.py"), os.path.join(d, "thresholds.yaml")]
            for f in files:
                open(f, "w").write("a")
            saved = OBSERVATION_FILES
            try:
                OBSERVATION_FILES = tuple(files)
                d0 = band_digest(val, pv)
                open(files[0], "w").write("b")
                d1 = band_digest(val, pv)
                open(files[1], "w").write("b")
                d2 = band_digest(val, pv)
            finally:
                OBSERVATION_FILES = saved
        ok(d0 != d1 != d2, "BS71 an oracle or coverage digest changes with the observation code and analysis thresholds")
    case("BS70-71", c1)

    def c2():
        from toolkit.engines.compartmental.engine import CompartmentalEngine
        ce = CompartmentalEngine()
        unsourced = dict(toy, benchmarks=[{"id": "U", "target": 1.0, "band": [0.5, 2.0], "estimand": {"type": "ratio"}}])
        probs = load_evidence(ce, pack=unsourced).problems
        try:
            BM.Runner(unsourced, ce, 1)
            built = True
        except ValueError:
            built = False
        ok(any("engine" in x and "CompartmentalEngine" in x for x in probs) and not built,
           f"BS72 an explicit pack is bound to the engine's class; a toy pack cannot lend a real engine its exemption: "
           f"{probs}")
    case("BS72", c2)

    def c3():
        from toolkit.engines.malaria.benchmarks import render_notes as RN
        with tempfile.TemporaryDirectory() as d:
            res_p = os.path.join(d, "results.yaml")
            tp = dict(toy, configs={"here": {"results_path": res_p}}, default_config="here")
            yaml.safe_dump(tp, open(os.path.join(d, "pack.yaml"), "w"))
            te = BM.load_engine(tp["engine"], tp["configs"]["here"])
            yaml.safe_dump(BM.stamp(BM.run_pack(tp, te, BM.Runner(tp, te, 1), {}), te), open(res_p, "w"))
            doc, fdoc = RN.validated_inputs(d)
            clean = doc["benchmarks"][0]["id"] == "T1" and fdoc is None
            bad = yaml.safe_load(open(res_p))
            bad["engine_identity"] = dict(bad["engine_identity"], code_sha256="stale-engine-code")
            yaml.safe_dump(bad, open(res_p, "w"))
            try:
                RN.validated_inputs(d)
                refused = False
            except SystemExit as ex:
                refused = "engine identity" in str(ex)
        ok(clean and refused, "BS73 render_notes renders only a results envelope validated against the engine "
                              "identity and fitted values it captured, with the validated fitted document")
    case("BS73", c3)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
