#!/usr/bin/env python3
"""The `estimates` deliverable: a decision spec with no allocation units (design note v2d §2.3), fail closed.

    python3 toolkit/spec/estimates.py --self-test

Some questions ask for quantities, not a plan: a reproduction number, an attack rate, with their intervals and a
threshold statement. Their spec has no options, constraints, comparator or objective. It has one population (a
one-row units table, derived like any other), the engine's dynamics declaration, the observation sources with their
measurement models, a `posterior` calibration and the outputs:

    deliverable:
      kind: estimates
      windows: {<window>: [t0, t1]}                    # named windows in model years (inside the horizon)
      outputs:                                         # each an engine window metric, named for the report
        - {name: <reported name>, metric: <window metric>, window: <window>, scale: ratio | proportion | rate}
      decision:                                        # optional: a threshold rule computed by the harness
        {kind: threshold, output: <name>, threshold: 1.0, direction: above | below, probability: 0.95,
         statements: {yes: "...", no: "...", undetermined: "..."}}
    dynamics:                                          # what the engine configuration is built from (engine-agnostic)
      generation_interval: {csv, lag, probability, step: week | day, values: [w_1, ...],
                            scale_range?: {range: [lo, hi], steps?: 5, source}}   # values = the csv's
      transmissibility: constant | {knots: [t_1, ...]} | {pool: [...], weighting?: marginal_likelihood | equal}
    observations:
      sources: [{id, measures, latent, frame, bias, noise, delay?}]   # toolkit/analysis/observation.py vocabulary;
                                                     # noise.dispersion: a number or {param, prior, source};
                                                     # delay: {pmf: [p_0, ...], source} on the engine's step grid
    calibration:
      method: posterior
      priors: {<engine parameter or unbound unit attribute>: {dist, range, source, verified}}
      targets: {csv, source: <source id>, step: <1-based step column>, observed: <count column>,
                frame: {band, source}}

`uncertainty.sources` always lists `calibration` (the joint posterior) and `observation` (each source's measurement
parameters, carried by the same posterior), plus any declared structural kind (`generation_interval` when
`scale_range` is declared; `transmissibility_structure` when `transmissibility.pool` is declared). A structural kind
held fixed may be listed in `uncertainty.excluded` (`[{kind, reason}]`) so the interval label names what it omits.
Nothing here names a disease.
"""
from __future__ import annotations

import csv
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KIND = "estimates"
REQUIRED = ("schema_version", "question", "decision_maker", "decision_date", "engine", "horizon", "decision_units",
            "dynamics", "observations", "calibration", "uncertainty", "analysis", "deliverable")
NOT_APPLICABLE = ("options", "constraints", "comparator", "objective")
SCALES = ("ratio", "proportion", "rate")
DIRECTIONS = ("above", "below")
UQ_KINDS = ("calibration", "observation")
UQ_STRUCTURAL = ("generation_interval", "transmissibility_structure")
UQ_ALL = UQ_KINDS + UQ_STRUCTURAL
WEIGHTING = ("marginal_likelihood", "equal")
MAX_VARIANTS = 15
MAX_GI_LAGS = 10_000
STEPS = ("week", "day")


def is_estimates(raw) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("deliverable"), dict) and raw["deliverable"].get("kind") == KIND


def statements(dec) -> dict:
    """The threshold rule's statements by verdict. YAML 1.1 reads the keys `yes` and `no` as booleans, so True and
    False are read as yes and no."""
    st = (dec or {}).get("statements") if isinstance(dec, dict) else None
    if not isinstance(st, dict):
        return {}
    return {("yes" if k is True else "no" if k is False else str(k)): v for k, v in st.items()}


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def gi_from_spec(raw: dict, base: str | None = None) -> dict:
    """{dt, w} from dynamics.generation_interval: the declared values, checked against the cited csv when `base`
    is given (ValueError on a mismatch: the values must be the file's, lag by lag from lag 1)."""
    gi = (raw.get("dynamics") or {}).get("generation_interval") or {}
    vals = gi.get("values")
    if not isinstance(vals, list) or not vals or not all(_isnum(v) for v in vals):
        raise ValueError("dynamics.generation_interval.values: the distribution's values, lag 1 first")
    if gi.get("step") not in STEPS:
        raise ValueError(f"dynamics.generation_interval.step: one of {STEPS}")
    if base is not None:
        for k in ("csv", "lag", "probability"):
            if not str(gi.get(k) or "").strip():
                raise ValueError(f"dynamics.generation_interval.{k}: required (the cited file and its columns)")
        p = gi["csv"] if os.path.isabs(gi["csv"]) else os.path.join(base, gi["csv"])
        rows = _read_csv(p)
        try:
            pairs = sorted((integral(r[gi["lag"]], "lag"), float(r[gi["probability"]])) for r in rows)
        except (KeyError, ValueError) as e:
            raise ValueError(f"dynamics.generation_interval: {gi['csv']} unreadable as lag/probability: {e}") from None
        lags = [a for a, _ in pairs]
        if lags != list(range(1, len(lags) + 1)):
            raise ValueError(f"dynamics.generation_interval: {gi['csv']} lags must be 1..L without gaps ({lags})")
        if len(pairs) != len(vals) or any(abs(b - float(v)) > 1e-12 for (_, b), v in zip(pairs, vals)):
            raise ValueError(f"dynamics.generation_interval.values {vals} differ from {gi['csv']} "
                             f"({[b for _, b in pairs]})")
    return {"dt": gi["step"], "w": [float(v) for v in vals]}


def rescale_gi(w, scale: float) -> list[float]:
    """Piecewise-constant CDF rescaling of lag-1..L PMF `w` by `scale > 0`, normalised with trailing zeros trimmed."""
    if not _isnum(scale) or float(scale) <= 0:
        raise ValueError(f"rescale_gi: scale must be finite and > 0 (got {scale!r})")
    w = w.tolist() if hasattr(w, "tolist") and not isinstance(w, (str, bytes)) else w
    if not isinstance(w, (list, tuple)) or not w or not all(_isnum(v) and float(v) >= 0 for v in w):
        raise ValueError("rescale_gi: w must be a non-empty list of finite non-negative numbers")
    wf, s = [float(v) for v in w], float(scale)
    if sum(wf) <= 0:
        raise ValueError("rescale_gi: w must have positive sum")
    if s == 1.0:
        return list(wf)
    L = len(wf)
    n_out = max(1, int(math.ceil(s * L)))
    if n_out > MAX_GI_LAGS:
        raise ValueError(f"rescale_gi: scaled PMF length {n_out} exceeds cap {MAX_GI_LAGS}")
    out = [0.0] * n_out
    for i, wi in enumerate(wf):
        if wi == 0.0:
            continue
        a, b = i * s, (i + 1) * s
        k0, k1 = int(math.floor(a)), min(n_out - 1, int(math.floor(b)))
        for k in range(k0, k1 + 1):
            ov = min(b, float(k + 1)) - max(a, float(k))
            if ov > 0.0:
                out[k] += wi * (ov / s)
    while len(out) > 1 and out[-1] == 0.0:
        out.pop()
    tot = math.fsum(out)
    if tot <= 0:
        raise ValueError("rescale_gi: rescaled PMF has zero mass")
    return [x / tot for x in out]


def gi_scales(raw: dict) -> list[float]:
    """Scale-factor grid from dynamics.generation_interval.scale_range (default [1.0])."""
    sr = ((raw.get("dynamics") or {}).get("generation_interval") or {}).get("scale_range")
    if sr is None:
        return [1.0]
    if not isinstance(sr, dict) or set(sr) - {"range", "steps", "source"}:
        raise ValueError("dynamics.generation_interval.scale_range: {range: [lo, hi], steps?: odd int in [3, 9], source}")
    rng, steps = sr.get("range"), sr.get("steps", 5)
    if not (isinstance(rng, list) and len(rng) == 2 and all(_isnum(v) for v in rng)
            and 0 < float(rng[0]) < float(rng[1]) and float(rng[0]) <= 1.0 <= float(rng[1])):
        raise ValueError("dynamics.generation_interval.scale_range.range: [lo, hi] with 0 < lo < hi and lo <= 1.0 <= hi")
    if isinstance(steps, bool) or not isinstance(steps, int) or not (3 <= steps <= 9 and steps % 2 == 1):
        raise ValueError("dynamics.generation_interval.scale_range.steps: odd integer in [3, 9]")
    if not (isinstance(sr.get("source"), str) and sr["source"].strip()):
        raise ValueError("dynamics.generation_interval.scale_range.source: non-empty string required")
    lo, hi = float(rng[0]), float(rng[1])
    return [1.0 if abs(v - 1.0) < 1e-12 else v for v in (lo + (hi - lo) * i / (steps - 1) for i in range(steps))]


def _check_knots(knots, h, where: str, check_horizon: bool = True) -> list[float]:
    if not (isinstance(knots, list) and knots and all(_isnum(x) for x in knots)):
        raise ValueError(f"{where}: non-empty list of strictly increasing numbers inside the horizon")
    ks = [float(x) for x in knots]
    if any(b <= a for a, b in zip(ks, ks[1:])):
        raise ValueError(f"{where}: knots must be strictly increasing ({ks})")
    if check_horizon and isinstance(h, dict) and _isnum(h.get("start")) and _isnum(h.get("stop")) \
            and any(not (float(h["start"]) < x < float(h["stop"])) for x in ks):
        raise ValueError(f"{where}: each knot must lie strictly inside ({h['start']}, {h['stop']})")
    return ks


def transmissibility_structures(raw: dict, check_horizon: bool = True) -> tuple[list[dict], str]:
    """(structures, weighting) from dynamics.transmissibility."""
    tr, h = (raw.get("dynamics") or {}).get("transmissibility", "constant"), raw.get("horizon")
    if tr == "constant":
        return [{"transmissibility": "constant", "priors": {}, "label": "constant"}], "marginal_likelihood"
    if isinstance(tr, dict) and "pool" in tr:
        w, pool = tr.get("weighting", "marginal_likelihood"), tr.get("pool")
        if set(tr) - {"pool", "weighting"} or w not in WEIGHTING or not isinstance(pool, list) or len(pool) < 2:
            raise ValueError(f"dynamics.transmissibility: {{pool: [>=2 distinct structures], weighting?: {WEIGHTING}}}")
        out, seen = [], set()
        for i, item in enumerate(pool):
            where = f"dynamics.transmissibility.pool[{i}]"
            if item == "constant":
                key, entry = ("constant",), {"transmissibility": "constant", "priors": {}, "label": "constant"}
            elif isinstance(item, dict) and set(item) == {"knots", "priors"}:
                ks = _check_knots(item["knots"], h, f"{where}.knots", check_horizon)
                want = {f"transmissibility:{k}" for k in range(1, len(ks) + 1)}
                if not isinstance(item["priors"], dict) or set(item["priors"]) != want:
                    raise ValueError(f"{where}.priors: must provide exactly {sorted(want)}")
                key = tuple(ks)
                entry = {"transmissibility": {"knots": ks}, "priors": dict(item["priors"]),
                         "label": "knots:" + ",".join(f"{x:.12g}" for x in ks)}
            else:
                raise ValueError(f"{where}: must be 'constant' or {{knots: [...], priors: {{...}}}}")
            if key in seen:
                raise ValueError(f"dynamics.transmissibility.pool: duplicate structure {entry['label']!r}")
            seen.add(key)
            out.append(entry)
        return out, w
    if isinstance(tr, dict) and set(tr) == {"knots"}:
        ks = _check_knots(tr["knots"], h, "dynamics.transmissibility.knots", check_horizon)
        return [{"transmissibility": {"knots": ks}, "priors": {},
                 "label": "knots:" + ",".join(f"{x:.12g}" for x in ks)}], "marginal_likelihood"
    raise ValueError("dynamics.transmissibility: constant | {knots: [t, ...]} | {pool: [...], weighting?}")


def structural_variants(raw: dict) -> tuple[list[dict], str]:
    """(variants, weighting): Cartesian product of generation-interval scales and transmissibility structures."""
    gi, scales = gi_from_spec(raw), gi_scales(raw)
    structs, weighting = transmissibility_structures(raw)
    out = []
    for sc in scales:
        w_sc = rescale_gi(gi["w"], sc)
        for st in structs:
            vid = f"gi{sc:.12g}_{st['label'].replace(':', '_').replace(',', '_')}"
            out.append({"id": vid, "gi_scale": sc, "gi": {"dt": gi["dt"], "w": w_sc},
                        "transmissibility": st["transmissibility"], "transmissibility_label": st["label"],
                        "priors": dict(st["priors"]),
                        "config": {"generation_interval": {"dt": gi["dt"], "w": w_sc},
                                   "dt": gi["dt"], "transmissibility": st["transmissibility"]}})
    if len({v["id"] for v in out}) != len(out):
        raise ValueError("dynamics: structural variants produced duplicate variant ids")
    return out, weighting


def engine_config(raw: dict) -> dict:
    """Base engine configuration implied by `dynamics` (for a pool, its first structure)."""
    gi = gi_from_spec(raw)
    return {"generation_interval": gi, "dt": gi["dt"],
            "transmissibility": transmissibility_structures(raw, check_horizon=False)[0][0]["transmissibility"]}


def integral(x, what="value") -> int:
    """A whole number written in a table (1, 1.0), or ValueError: a fractional value is never truncated."""
    v = float(x)
    if not math.isfinite(v) or v != round(v):
        raise ValueError(f"{what} {x!r} is not an integer")
    return int(round(v))


# Bias mechanisms and the support their parameter must keep (the prior's whole range): a reporting fraction is a share.
BIAS_SUPPORT = {"under_ascertainment": (0.0, 1.0)}
SUPPORTED_BIAS = tuple(BIAS_SUPPORT)        # the mechanisms the estimates path applies to a count series


def _with_source(prior, source):
    """The prior with its enclosing declaration's source (Codex r2-9: provenance kept)."""
    if not isinstance(prior, dict):
        return prior
    return dict(prior, source=prior.get("source") or str(source or ""))


def source_params(sources) -> list:
    """[(param, prior, where)] of every observation parameter, in declaration order (duplicates kept, so a collision
    can be refused): bias mechanisms and a declared dispersion."""
    out = []
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        for b in s.get("bias") or [] if isinstance(s.get("bias"), list) else []:
            if isinstance(b, dict) and b.get("param"):
                out.append((b["param"], _with_source(b.get("prior"), b.get("source")),
                            f"observations.{s.get('id')}.bias.{b.get('mechanism')}"))
        disp = (s.get("noise") or {}).get("dispersion")
        if isinstance(disp, dict) and disp.get("param"):
            out.append((disp["param"], _with_source(disp.get("prior"), disp.get("source")),
                        f"observations.{s.get('id')}.noise.dispersion"))
    return out


def _source_params(sources) -> dict:
    return {n: p for n, p, _ in source_params(sources)}


def validate_pending(s) -> list[str]:
    """Engine-independent checks (sign-off with engine: auto)."""
    r = s.raw
    bad = [f"missing section {k}" for k in REQUIRED if k not in r]
    if bad:
        return bad
    bad += [f"{k}: not part of an estimates spec (no allocation): remove it" for k in NOT_APPLICABLE if k in r]
    if isinstance(r.get("deliverable"), dict) and "plan" in r["deliverable"]:     # hb1 fix 11 (Codex r1 P1-4)
        bad.append("deliverable.plan: not part of an estimates spec (no plan to choose): remove it")
    from toolkit.spec.schema import SCHEMA_VERSION
    if r["schema_version"] != SCHEMA_VERSION:
        return [f"schema_version {r['schema_version']!r} is not {SCHEMA_VERSION}"]
    for k in ("question", "decision_maker"):
        if not str(r.get(k) or "").strip():
            bad.append(f"{k}: empty")
    if not (_isnum(r["decision_date"]) or (isinstance(r["decision_date"], str) and r["decision_date"][:4].isdigit())):
        bad.append("decision_date: give a year or an ISO date")
    h = r["horizon"]
    if not (isinstance(h, dict) and _isnum(h.get("start")) and _isnum(h.get("stop")) and h["start"] < h["stop"]):
        return bad + ["horizon: start < stop (numbers)"]
    try:
        units = s.units()
    except (OSError, KeyError, TypeError) as e:
        return bad + [f"decision_units: units table unreadable: {e}"]
    if len(units) != 1:
        bad.append(f"decision_units: an estimates spec describes one population (one row); the table has {len(units)}")
    gi_raw, tr_raw = (r.get("dynamics") or {}).get("generation_interval"), (r.get("dynamics") or {}).get("transmissibility", "constant")
    has_gi_scale = isinstance(gi_raw, dict) and gi_raw.get("scale_range") is not None
    has_tr_pool = isinstance(tr_raw, dict) and "pool" in tr_raw
    gi_ok, scales, tr_structs = None, [1.0], None
    try:
        gi_ok = gi_from_spec(r, s.base)
        from toolkit.engines.renewal.engine import check_generation_interval
        check_generation_interval(gi_ok, gi_ok["dt"])
    except (OSError, ValueError) as e:
        gi_ok = None
        bad.append(str(e))
    try:
        scales = gi_scales(r)
        if gi_ok is not None and has_gi_scale:
            from toolkit.engines.renewal.engine import check_generation_interval
            for sc in scales:
                check_generation_interval({"dt": gi_ok["dt"], "w": rescale_gi(gi_ok["w"], sc)}, gi_ok["dt"])
    except ValueError as e:
        bad.append(str(e))
    try:
        tr_structs, _ = transmissibility_structures(r, check_horizon=True)
    except ValueError as e:
        bad.append(str(e))
    if tr_structs is not None and len(scales) * len(tr_structs) > MAX_VARIANTS:
        bad.append(f"dynamics: {len(scales) * len(tr_structs)} structural variants exceeds the cap of {MAX_VARIANTS}")
    # observation sources
    from toolkit.analysis import observation as ob
    srcs = (r.get("observations") or {}).get("sources")
    bad += [f"{code}: observations: {m}" for code, m in ob.check_sources(srcs)]
    id_list = [x.get("id") for x in srcs or [] if isinstance(x, dict)]
    ids = set(id_list)
    if len(ids) != len(id_list):
        bad.append("observations.sources: duplicate source ids (each source has its own id)")
    if "engine_parameters" in r:
        bad.append("engine_parameters: not applied by the estimates path; declare quantities as data or priors")
    for x in srcs or []:
        if not isinstance(x, dict):
            continue
        mechs = [b.get("mechanism") for b in (x.get("bias") if isinstance(x.get("bias"), list) else [])
                 if isinstance(b, dict)]
        bad += [f"observations.{x.get('id')}.bias: mechanism {m} appears more than once (its composition is not "
                "defined)" for m in sorted({m for m in mechs if mechs.count(m) > 1})]
        disp = (x.get("noise") or {}).get("dispersion")
        if (x.get("noise") or {}).get("model") == "negbin" and not (
                (_isnum(disp) and disp > 0) or (isinstance(disp, dict) and disp.get("param")
                                                and ob._prior_ok(disp.get("prior")) and str(disp.get("source") or ""))):
            bad.append(f"observations.{x.get('id')}.noise.dispersion: a number > 0 or {{param, prior, source}}")
        if (x.get("frame") or {}).get("excluded"):
            bad.append(f"observations.{x.get('id')}.frame.excluded: frame exclusions are not supported for a count "
                       "series in an estimates spec (the out-of-frame population would not change the prediction): "
                       "declare the frame's population instead, or leave the source out")
        for b in x.get("bias") if isinstance(x.get("bias"), list) else []:
            m = (b or {}).get("mechanism") if isinstance(b, dict) else None
            if m in ob.BIAS and m not in SUPPORTED_BIAS:
                bad.append(f"observations.{x.get('id')}.bias: mechanism {m} is not applied to a count series by the "
                           f"estimates path (supported: {', '.join(SUPPORTED_BIAS)})")
            lo_hi = BIAS_SUPPORT.get(m)
            pr = (b or {}).get("prior") if isinstance(b, dict) else None
            if lo_hi and isinstance(pr, dict):
                r_ = [0.0, 1.0] if pr.get("dist") == "beta" else pr.get("range")
                if not (isinstance(r_, list) and len(r_) == 2 and all(_isnum(v) for v in r_)
                        and lo_hi[0] <= r_[0] and r_[1] <= lo_hi[1] and r_[1] > 0):
                    bad.append(f"observations.{x.get('id')}.bias.{m}: the prior's range must lie in the parameter's "
                               f"support [{lo_hi[0]:g}, {lo_hi[1]:g}]")
        dl = x.get("delay")
        if dl is not None:
            pmf = dl.get("pmf") if isinstance(dl, dict) else None
            if not (isinstance(pmf, list) and pmf and all(_isnum(v) and v >= 0 for v in pmf)
                    and abs(sum(pmf) - 1) < 1e-9 and str(dl.get("source") or "").strip()):
                bad.append(f"observations.{x.get('id')}.delay: {{pmf: [p_0, ...] summing to 1, source}}")
    # calibration
    cal = r["calibration"]
    if not isinstance(cal, dict) or cal.get("method") != "posterior":
        return bad + ["calibration.method: posterior (an estimates spec calibrates by its declared priors)"]
    from toolkit.analysis.calibration import Prior
    pri = cal.get("priors") or {}
    if not isinstance(pri, dict) or not pri:
        bad.append("calibration.priors: every quantity the calibration moves, with its prior and source")
    for n, d in (pri or {}).items():
        try:
            Prior.from_decl(n, d)
        except ValueError as e:
            bad.append(f"calibration.priors.{n}: {e}")
        if not str((d or {}).get("source") or "").strip():
            bad.append(f"calibration.priors.{n}: a source is required")
    seen = {}
    for n, d, where in source_params(srcs):
        if n in pri:
            bad.append(f"parameter_name_collision: {where}: {n} is also a calibration prior; observation parameters "
                       "take their prior from the source and need their own names")
        if n in seen:
            bad.append(f"parameter_name_collision: {where}: {n} is also declared at {seen[n]}; give each observation "
                       "parameter its own name")
        seen.setdefault(n, where)
        try:
            Prior.from_decl(n, d)
        except ValueError as e:
            bad.append(f"observations: {n}: {e}")
    if has_tr_pool and tr_structs:
        pool_pnames = set()
        for idx, st in enumerate(tr_structs):
            for n, d in st["priors"].items():
                pool_pnames.add(n)
                where = f"dynamics.transmissibility.pool[{idx}].priors.{n}"
                if n in pri or n in seen:
                    bad.append(f"parameter_name_collision: {where}: {n} collides with {'calibration.priors' if n in pri else seen[n]}")
                try:
                    Prior.from_decl(n, d)
                except ValueError as e:
                    bad.append(f"{where}: {e}")
                if not (isinstance(d, dict) and isinstance(d.get("source"), str) and d["source"].strip()):
                    bad.append(f"{where}: a non-empty source is required")
        bad += [f"calibration.priors.{n}: declare knot priors on each pooled structure, not calibration.priors"
                for n in pri if str(n).startswith("transmissibility:") and n not in pool_pnames]
    t = cal.get("targets") or {}
    for k in ("csv", "source", "step", "observed"):
        if not str(t.get(k) or "").strip():
            bad.append(f"calibration.targets.{k}: required")
    if t.get("source") and t["source"] not in ids:
        bad.append(f"observation_source_undeclared: calibration.targets.source {t['source']!r} is not a declared source")
    tsrc = next((x for x in srcs or [] if isinstance(x, dict) and x.get("id") == t.get("source")), None)
    if tsrc is not None and ((tsrc.get("latent") or {}).get("quantity") != "incidence"
                             or tsrc.get("measures") != "case_notifications"):
        bad.append(f"observations.{tsrc.get('id')}: the estimates path fits a count series: the target's source must "
                   "measure case_notifications of latent quantity incidence")
    if not bad:
        try:
            rows = _read_csv(s.resolve(t["csv"]))
            steps = [integral(x[t["step"]], "calibration.targets: step") for x in rows]
            ys = [float(x[t["observed"]]) for x in rows]
            if not rows or min(steps) < 1 or len(set(steps)) != len(steps):
                bad.append("calibration.targets: steps must be distinct integers >= 1 (1 = the first step of the horizon)")
            if any(y < 0 or y != round(y) for y in ys):
                bad.append("calibration.targets: observed counts must be non-negative integers")
        except (OSError, KeyError, ValueError) as e:
            bad.append(f"calibration.targets: {t.get('csv')} unreadable: {e}")
    # uncertainty
    u = r["uncertainty"] if isinstance(r["uncertainty"], dict) else {}
    if not isinstance(r["uncertainty"], dict):
        bad.append("uncertainty: must be a mapping")
    if "affordability" in u:      # F6 S1-1: no allocation, so no plan cost to afford
        bad.append("uncertainty.affordability: not supported for an estimates spec (it has no plan to afford)")
    raw_srcs = u.get("sources")
    if not isinstance(raw_srcs, list) or not all(isinstance(x, dict) and x.get("kind") for x in raw_srcs):
        bad.append("uncertainty.sources: list of {kind} mappings")
    srcs_k = [x.get("kind") for x in (raw_srcs if isinstance(raw_srcs, list) else []) if isinstance(x, dict)]
    bad += [f"uncertainty.sources: duplicate kind {k!r}" for k in sorted({k for k in srcs_k if srcs_k.count(k) > 1})]
    bad += [f"uncertainty: kind {k} must be a source (the joint posterior always carries it)" for k in UQ_KINDS
            if k not in srcs_k]
    bad += [f"uncertainty.sources: kind {k!r} not in {UQ_ALL} for an estimates spec" for k in srcs_k if k not in UQ_ALL]
    raw_exc, exc_k = u.get("excluded"), []
    if raw_exc is not None and not isinstance(raw_exc, list):
        bad.append(f"uncertainty.excluded: list of {{kind, reason}} for structural kinds in {UQ_STRUCTURAL}")
    for e in (raw_exc if isinstance(raw_exc, list) else []):
        if not isinstance(e, dict) or set(e) != {"kind", "reason"}:
            bad.append(f"uncertainty.excluded: each entry must be {{kind, reason}} for a structural kind in {UQ_STRUCTURAL}")
            continue
        k, reason = e["kind"], e["reason"]
        if k in UQ_KINDS:
            bad.append(f"uncertainty.excluded: kind {k!r} cannot be excluded (the joint posterior always carries it)")
        elif k not in UQ_STRUCTURAL:
            bad.append(f"uncertainty.excluded: kind {k!r} not in {UQ_STRUCTURAL} for an estimates spec")
        else:
            if not (isinstance(reason, str) and reason.strip()):
                bad.append(f"uncertainty.excluded: kind {k!r} requires a non-empty string reason")
            if k in exc_k:
                bad.append(f"uncertainty.excluded: duplicate kind {k!r}")
            exc_k.append(k)
    bad += [f"uncertainty: kind {k!r} cannot be in both sources and excluded" for k in sorted(set(srcs_k) & set(exc_k))]
    for kind, flag, decl in (("generation_interval", has_gi_scale, "dynamics.generation_interval.scale_range"),
                             ("transmissibility_structure", has_tr_pool, "dynamics.transmissibility.pool")):
        if flag and kind not in srcs_k:
            bad.append(f"uncertainty.sources: {decl} requires {kind!r} in uncertainty.sources")
        if kind in srcs_k and not flag:
            bad.append(f"uncertainty.sources: {kind!r} requires {decl}")
    if (r.get("analysis") or {}).get("decision_tests"):
        bad.append("analysis.decision_tests: not run for an estimates spec (its automatic prior_range tests are); "
                   "remove them")
    lv = u.get("level", 0.95)
    if not (_isnum(lv) and 0 < lv < 1):
        bad.append("uncertainty.level: in (0, 1)")
    # deliverable
    d = r["deliverable"]
    wins = d.get("windows")
    if not isinstance(wins, dict) or not wins:
        bad.append("deliverable.windows: {name: [t0, t1]}")
        wins = {}
    for n, w in wins.items():
        if not (isinstance(w, list) and len(w) == 2 and all(_isnum(x) for x in w) and w[0] < w[1]
                and h["start"] <= w[0] and w[1] <= h["stop"]):
            bad.append(f"deliverable.windows.{n}: [t0, t1] inside the horizon")
    outs = d.get("outputs")
    names = []
    if not isinstance(outs, list) or not outs:
        bad.append("deliverable.outputs: at least one {name, metric, window, scale}")
        outs = []
    import re
    for o in outs:
        if not isinstance(o, dict) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(o.get("name") or "")):
            bad.append(f"deliverable.outputs: {o!r} needs a name (letters, digits, _)")
            continue
        names.append(o["name"])
        if o.get("window") not in wins:
            bad.append(f"deliverable.outputs.{o['name']}: window {o.get('window')!r} is not in deliverable.windows")
        if o.get("scale") not in SCALES:
            bad.append(f"deliverable.outputs.{o['name']}: scale in {SCALES}")
        if not str(o.get("metric") or "").strip():
            bad.append(f"deliverable.outputs.{o['name']}: metric (an engine window metric)")
    if len(set(names)) != len(names):
        bad.append("deliverable.outputs: names must be unique")
    dec = d.get("decision")
    if dec is not None:
        st = statements(dec)
        if not (isinstance(dec, dict) and dec.get("kind") == "threshold" and dec.get("output") in names
                and _isnum(dec.get("threshold")) and dec.get("direction") in DIRECTIONS
                and _isnum(dec.get("probability")) and 0.5 < dec["probability"] < 1
                and all(str(st.get(k) or "").strip() for k in ("yes", "no", "undetermined"))):
            bad.append("deliverable.decision: {kind: threshold, output: <an output name>, threshold, direction: above | "
                       "below, probability in (0.5, 1), statements: {yes, no, undetermined}}")
        elif any(ch.isdigit() for k in ("yes", "no", "undetermined") for ch in str(st.get(k))):
            bad.append("deliverable.decision.statements: descriptive text with no numbers (the harness renders them)")
    return bad


def validate(s, eng) -> list[str]:
    """Every check, with the engine: priors name engine parameters or unbound unit attributes, outputs name engine
    window metrics, and the engine offers run_batch and the incidence series the targets are compared with."""
    bad = validate_pending(s)
    card = eng.describe()
    pnames = {p.name: p for p in eng.parameters()}
    du = s.raw.get("decision_units") or {}
    bound = set((du.get("engine_attributes") or {}))
    for attr in bound:
        if attr not in card.unit_attributes:
            bad.append(f"decision_units.engine_attributes: {attr} is not declared by engine {card.name}")
    for n in ((s.raw.get("calibration") or {}).get("priors") or {}):
        if n in pnames:
            if pnames[n].kind not in ("calibrated", "scenario", "fitted"):
                bad.append(f"calibration.priors.{n}: engine parameter of kind {pnames[n].kind} (a prior goes on a "
                           "calibrated, fitted or scenario parameter)")
        elif n in card.unit_attributes:
            if n in bound:
                bad.append(f"calibration.priors.{n}: unit attribute bound to data in decision_units.engine_attributes "
                           "(a quantity is data or calibrated, not both)")
        else:
            bad.append(f"calibration.priors.{n}: neither an engine parameter nor a unit attribute of {card.name}")
    cal_pri = (s.raw.get("calibration") or {}).get("priors") or {}
    try:
        base_pool_priors = set(transmissibility_structures(s.raw, check_horizon=False)[0][0]["priors"])
    except ValueError:
        base_pool_priors = set()
    miss = [p for p, v in pnames.items() if v.kind == "calibrated" and p not in cal_pri and p not in base_pool_priors]
    if miss:
        bad.append(f"calibration.priors: the engine's calibrated parameters {miss} need a prior (no silent defaults)")
    wm = set((card.outputs or {}).get("windows") or [])
    for o in (s.raw.get("deliverable") or {}).get("outputs") or []:
        if isinstance(o, dict) and o.get("metric") not in wm:
            bad.append(f"deliverable.outputs.{o.get('name')}: metric {o.get('metric')!r} is not an engine window metric "
                       f"{sorted(wm)}")
    if not callable(getattr(eng, "run_batch", None)):
        bad.append(f"engine {card.name}: an estimates spec needs run_batch() (the posterior evaluates many points)")
    for n, _, where in source_params((s.raw.get("observations") or {}).get("sources")):
        if n in pnames or n in card.unit_attributes:
            bad.append(f"parameter_name_collision: {where}: {n} is also an engine "
                       f"{'parameter' if n in pnames else 'unit attribute'} of {card.name}; observation parameters "
                       "need their own names")
    # Codex r2-4: no silent defaults, whatever the derivation opt-in: every unit attribute is bound to data, moved by
    # the calibration, or its card default accepted by name with a reason
    pri_names = set(((s.raw.get("calibration") or {}).get("priors") or {}))
    acc = du.get("defaults_accepted") or {}
    for a in card.unit_attributes:
        if a in bound or a in pri_names:
            if a in acc:
                bad.append(f"decision_units.defaults_accepted.{a}: the attribute is also bound or calibrated")
            continue
        if not (isinstance(acc, dict) and str(acc.get(a) or "").strip()):
            bad.append(f"engine_attribute_unbound: engine {card.name}: {a}: bind it, give it a calibration prior, or "
                       "accept the card default by name in decision_units.defaults_accepted with a reason")
    # Codex r2-2: the engine configuration is exactly what dynamics declares (no other key, e.g. a results file)
    ecfg = (s.raw.get("engine") or {}).get("config") if isinstance(s.raw.get("engine"), dict) else None
    extra = sorted(set(ecfg or {}) - {"generation_interval", "dt", "transmissibility"})
    if extra:
        bad.append(f"engine.config: keys {extra} are not declared by dynamics (e.g. results_path): remove them")
    bad += dynamics_problems(s.raw, eng)
    if not bad:
        bad += prior_support_problems(s, eng)
    return bad


MAX_PROBE_CORNERS = 64


def support_ends(prior) -> list:
    """A prior's physical support ends (a beta prior: 0 and 1, not its central interval)."""
    return [0.0, 1.0] if prior.dist == "beta" else [float(prior.lo), float(prior.hi)]


def probe_corners(ends, cap: int = MAX_PROBE_CORNERS) -> tuple:
    """(corners, info): every corner of the box when there are at most `cap`, else `cap` corners drawn by index
    (seeded) and decoded bit by bit, so 2^n tuples are never built. info: {total, probed, sampled}."""
    import random
    d = len(ends)
    total = 2 ** d
    if total <= cap:
        idx = list(range(total))
    else:                       # distinct random indices of d bits each (no range of 2^d: it overflows at d >= 63)
        rng, seen = random.Random(0), set()
        while len(seen) < cap:
            seen.add(rng.getrandbits(d))
        idx = sorted(seen)
    corners = [tuple(ends[j][(i >> j) & 1] for j in range(d)) for i in idx]
    return corners, {"total": total, "probed": len(corners), "sampled": total > cap}


def prior_support_problems(s, eng) -> list[str]:
    """The engine accepts the corners of the box the calibration priors span, at each prior's physical support ends
    (c1 run: s0 on [0, 1] with seed up to 1e-3 holds points where seed > s0, which the engine refuses, and the
    posterior would have been truncated). Each corner is one engine run with the unit's bound attributes; a refusal
    is reported with the engine's reason and the bound attributes it involves, before sign-off. This catches
    constraints an engine imposes along the box's edges (such as seed <= s0, which is affine); it does not certify an
    arbitrary interior domain, and above MAX_PROBE_CORNERS corners it samples them. The analysis keeps its runtime
    refusal guards either way. Observation parameters do not reach the engine and are not probed."""
    from toolkit.analysis.calibration import Prior
    from toolkit.engines.contract import Horizon, Package, UnitSpec
    cal_pri = (s.raw.get("calibration") or {}).get("priors") or {}
    structs, _ = transmissibility_structures(s.raw, check_horizon=False)
    h = s.raw["horizon"]
    wins = {k: tuple(float(x) for x in v) for k, v in ((s.raw.get("deliverable") or {}).get("windows") or {}).items()}
    hz = Horizon(float(h["start"]), float(h["stop"]), (float(h["start"]), float(h["stop"])), wins)
    base = s.unit_attributes(s.units()[0])
    fmt = lambda d: ", ".join(f"{k}={v:.3g}" if isinstance(v, (int, float)) else f"{k}={v}"  # noqa: E731
                              for k, v in d.items())
    base_cfg = engine_config(s.raw) if (len(structs) > 1 or structs[0]["priors"]) else None
    out = []
    for st in structs:
        if base_cfg is None or st["transmissibility"] == base_cfg["transmissibility"]:
            eng_s = eng
        else:
            kw = dict(base_cfg, transmissibility=st["transmissibility"])
            if hasattr(eng, "results_path"):
                kw["results_path"] = eng.results_path
            eng_s = type(eng)(**kw)
        pri = dict(cal_pri, **st["priors"])
        card, pnames = eng_s.describe(), {p.name for p in eng_s.parameters()}
        names = [n for n in pri if n in pnames or n in card.unit_attributes]
        if not names:
            continue
        corners, info = probe_corners([support_ends(Prior.from_decl(n, pri[n])) for n in names])
        bound = {k: v for k, v in base.items() if k not in names}
        tag = f" ({st['label']})" if base_cfg is not None else ""
        for c in corners:
            vals = dict(zip(names, c))
            params = {k: float(v) for k, v in vals.items() if k in pnames}
            unit = UnitSpec("probe", dict(base, **{k: float(v) for k, v in vals.items() if k not in pnames}))
            try:
                eng_s.run_batch([params], [unit], [Package("none", {})], hz, 1)
            except (ValueError, RuntimeError) as e:
                where = (f"; with the attributes bound to data or accepted defaults {fmt(bound)}: correct the binding "
                         "or the prior ranges") if bound else "; narrow the prior ranges to values the engine accepts"
                out.append(f"prior_support_infeasible: calibration.priors{tag}: the engine refuses the corner "
                           f"{fmt(vals)} of the declared prior box ({e}){where}"
                           + (f" ({info['probed']} of {info['total']} corners probed)" if info["sampled"] else ""))
                if len(out) >= 3:
                    return out
    return out


def dynamics_problems(raw: dict, eng) -> list[str]:
    """The engine runs the dynamics the spec declares (Codex r1-1): its configuration (`config()`) has the declared
    generation interval, step and transmissibility. An engine that does not report its configuration cannot be
    checked and is refused."""
    cfgf = getattr(eng, "config", None)
    if not callable(cfgf):
        return [f"dynamics: engine {eng.describe().name} does not report its configuration (config()), so the "
                "declared dynamics cannot be checked against what it runs"]
    try:
        want = engine_config(raw)
    except ValueError as e:
        return [str(e)]
    got = cfgf()
    bad = []
    gw, ww = (got.get("generation_interval") or {}), want["generation_interval"]
    if got.get("dt") != want["dt"] or gw.get("dt", got.get("dt")) != ww["dt"] or len(gw.get("w") or []) != len(ww["w"]) \
            or any(abs(float(a) - float(b)) > 1e-12 for a, b in zip(gw.get("w") or [], ww["w"])):
        bad.append(f"dynamics: the engine runs generation interval {gw} on a {got.get('dt')!r} grid; the spec declares "
                   f"{ww}")
    if (got.get("transmissibility") or "constant") != want["transmissibility"]:
        bad.append(f"dynamics: the engine's transmissibility {got.get('transmissibility')!r} is not the declared "
                   f"{want['transmissibility']!r}")
    return bad


def needs(raw: dict) -> dict:
    """What an engine must offer (the eligibility filter): the outputs' window metrics, the unit attributes the spec
    binds or calibrates."""
    wins = [o.get("metric") for o in (raw.get("deliverable") or {}).get("outputs") or [] if isinstance(o, dict)]
    du = raw.get("decision_units") or {}
    attrs = list(du.get("engine_attributes") or {})
    return {"components": {}, "outputs": [], "cost_drivers": [], "windows": [w for w in dict.fromkeys(wins) if w],
            "unit_attributes": attrs, "age_groups": []}


# --------------------------------------------------------------------------- self-test
def toy_dir(d: str, gi=(0.5, 0.5), rows=None) -> str:
    """A complete estimates run directory for self-tests (the renewal engine, a synthetic series)."""
    import yaml
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    with open(os.path.join(d, "data", "pop.csv"), "w") as f:
        f.write("unit_id,population\nall,100000\n")
    with open(os.path.join(d, "data", "gi.csv"), "w") as f:
        f.write("lag,p\n" + "".join(f"{i + 1},{v}\n" for i, v in enumerate(gi)))
    rows = rows if rows is not None else [3, 5, 9, 15, 24, 37, 52, 66, 72, 66, 52, 36, 23, 14, 8, 4]
    with open(os.path.join(d, "data", "cases.csv"), "w") as f:
        f.write("week,cases\n" + "".join(f"{i + 1},{v}\n" for i, v in enumerate(rows)))
    spec = toy_spec(gi, len(rows))
    yaml.safe_dump(spec, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
    return os.path.join(d, "decision_spec.yaml")


def toy_spec(gi=(0.5, 0.5), n_weeks=16) -> dict:
    wk = 7.0 / 365.25
    return {
        "schema_version": 1, "question": "toy outbreak: reproduction number and attack rate",
        "decision_maker": "toy health office", "decision_date": 2026,
        "engine": {"ref": "toolkit.engines.renewal.engine:RenewalEngine",
                   "config": {"generation_interval": {"dt": "week", "w": list(gi)}, "dt": "week"}},
        "horizon": {"start": 0.0, "stop": n_weeks * wk + 1e-9},
        "decision_units": {"csv": "data/pop.csv", "id": "unit_id", "engine_attributes": {"N": {"column": "population"}},
                           "populations": {"all": {"column": "population", "age_groups": ["all"]}},
                           "defaults_accepted": {"s0": "toy: everyone susceptible at the start (no prior immunity)"}},
        "dynamics": {"generation_interval": {"csv": "data/gi.csv", "lag": "lag", "probability": "p", "step": "week",
                                             "values": list(gi)}, "transmissibility": "constant"},
        "observations": {"sources": [{
            "id": "reports", "measures": "case_notifications", "latent": {"quantity": "incidence"},
            "frame": {"design": "administrative", "population": "the whole population"},
            "bias": [{"mechanism": "under_ascertainment", "param": "reporting",
                      "prior": {"dist": "loguniform", "range": [0.02, 0.5]}, "source": "toy"}],
            "noise": {"model": "negbin", "dispersion": {"param": "k", "prior": {"dist": "loguniform",
                                                                                "range": [2.0, 500.0]},
                                                        "source": "toy"}}}]},
        "calibration": {"method": "posterior",
                        "priors": {"r0": {"dist": "uniform", "range": [1.0, 4.0], "source": "toy"},
                                   "seed": {"dist": "loguniform", "range": [1e-5, 1e-2], "source": "toy"}},
                        "targets": {"csv": "data/cases.csv", "source": "reports", "step": "week", "observed": "cases",
                                    "frame": {"band": "incidence", "source": "toy"}}},
        "uncertainty": {"level": 0.95, "draws": 200, "seed": 5,
                        "sources": [{"kind": "calibration"}, {"kind": "observation"}]},
        "analysis": {"evaluation": "direct"},
        "deliverable": {"kind": "estimates", "windows": {"season": [0.0, n_weeks * wk + 1e-9]},
                        "outputs": [{"name": "reff_next", "metric": "reff_ref", "window": "season", "scale": "ratio"},
                                    {"name": "attack", "metric": "attack_rate", "window": "season",
                                     "scale": "proportion"}],
                        "decision": {"kind": "threshold", "output": "reff_next", "threshold": 1.0,
                                     "direction": "above", "probability": 0.95,
                                     "statements": {"yes": "a large outbreak is expected",
                                                    "no": "a large outbreak is not expected",
                                                    "undetermined": "the data do not settle it"}}}}


def _self_test() -> int:
    import copy
    import tempfile
    import yaml
    from toolkit.spec import schema
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        sp = toy_dir(d)
        s = schema.load(sp)
        errs = schema.validate(s)
        ok(errs == [], f"ES1 a complete estimates spec validates (no options, constraints, comparator, objective): {errs}")

        def mut(fn, needle, label):
            raw = copy.deepcopy(yaml.safe_load(open(sp)))
            fn(raw)
            p2 = os.path.join(d, "decision_spec.yaml")
            keep = open(sp).read()
            yaml.safe_dump(raw, open(p2, "w"), sort_keys=False)
            e = schema.validate(schema.load(p2))
            open(sp, "w").write(keep)
            ok(any(needle in x for x in e), f"{label}: {[x for x in e if needle in x][:1] or e[:2]}")
        mut(lambda r: r["dynamics"]["generation_interval"].update(values=[0.6, 0.4]), "differ from",
            "ES2 generation-interval values that differ from the cited file are refused")
        mut(lambda r: r.update(options={"components": {}}), "not part of an estimates spec",
            "ES3 an allocation section in an estimates spec is refused")
        mut(lambda r: r["calibration"]["priors"].pop("r0"), "need a prior",
            "ES4 a calibrated engine parameter without a prior is refused (no silent defaults)")
        mut(lambda r: r["calibration"]["priors"].update(N={"dist": "uniform", "range": [1, 2], "source": "x"}),
            "bound to data", "ES5 a prior on a unit attribute bound to data is refused")
        mut(lambda r: r["deliverable"]["outputs"][0].update(metric="zz"), "not an engine window metric",
            "ES6 an output that is not an engine window metric is refused")
        mut(lambda r: r["observations"]["sources"][0].update(bias=[{"mechanism": "fudge", "param": "f"}]),
            "bias_without_mechanism", "ES7 a free multiplier on a source is refused (closed vocabulary)")
        mut(lambda r: r["uncertainty"].update(affordability={"max_share_over_budget": 0.1}), "uncertainty.affordability",
            "ES-AF an estimates spec has no plan to afford: uncertainty.affordability is refused (Codex S1-1 P1)")
        mut(lambda r: r["uncertainty"].update(sources=[{"kind": "calibration"}]), "kind observation",
            "ES8 the observation kind must be covered or excluded with a reason")
        mut(lambda r: r["deliverable"]["decision"]["statements"].update(yes="above 1.0"), "no numbers",
            "ES9 threshold statements carry no numbers")
        mut(lambda r: r["calibration"]["targets"].update(source="other"), "observation_source_undeclared",
            "ES10 a target that names no declared source is refused")
        mut(lambda r: r["observations"]["sources"][0].update(delay={"pmf": [0.5, 0.4], "source": "x"}), "delay",
            "ES11 a reporting delay that does not sum to 1 is refused")
        # ---- Codex r1
        mut(lambda r: r["engine"]["config"].update(generation_interval={"dt": "week", "w": [1.0]}), "dynamics",
            "ES14 (Codex r1-1) an engine configured with another generation interval than dynamics declares is refused")
        mut(lambda r: r["observations"]["sources"][0]["noise"]["dispersion"].update(param="reporting"),
            "parameter_name_collision", "ES15 (Codex r1-3) two observation parameters with one name are refused")
        mut(lambda r: r["observations"]["sources"][0]["bias"][0].update(param="s0"), "parameter_name_collision",
            "ES15b (Codex r1-3) an observation parameter named like an engine unit attribute is refused")
        mut(lambda r: r["observations"]["sources"][0]["frame"].update(excluded=[{
            "who": "x", "param": "rel", "share": 0.1, "relative_value_prior": {"dist": "uniform", "range": [0.1, 0.9]},
            "source": "x"}]), "not supported", "ES16 (Codex r1-4) a frame exclusion (not modelled here) is refused")
        mut(lambda r: r["observations"]["sources"][0]["bias"][0]["prior"].update(dist="uniform", range=[-1.0, 0.5]),
            "support", "ES17 (Codex r1-6) a reporting-fraction prior outside [0, 1] is refused")
        mut(lambda r: r["analysis"].update(decision_tests=[{"id": "t", "type": "envelope", "text": "x"}]),
            "decision_tests", "ES18 (Codex r1-7) declared decision tests (not run for estimates) are refused")
        mut(lambda r: r["uncertainty"].update(excluded=[{"parameter": "reporting", "reason": "x"}]), "excluded",
            "ES18b (Codex r1-8) uncertainty exclusions (not applied by the estimates path) are refused")

        def frac(r):
            open(os.path.join(d, "data", "cases2.csv"), "w").write("week,cases\n" + "".join(
                f"{i + 1.75},{v}\n" for i, v in enumerate([3, 5, 9])))
            r["calibration"]["targets"]["csv"] = "data/cases2.csv"
        mut(frac, "integer", "ES19 (Codex r1-10) fractional target steps are refused, not truncated")
        open(os.path.join(d, "data", "gi2.csv"), "w").write("lag,p\n1.5,0.5\n2.5,0.5\n")
        mut(lambda r: r["dynamics"]["generation_interval"].update(csv="data/gi2.csv"), "integer",
            "ES19b (Codex r1-10) fractional generation-interval lags are refused")
        # ---- Codex r2
        mut(lambda r: r["decision_units"].pop("defaults_accepted"), "engine_attribute_unbound",
            "ES20 (Codex r2-4) an engine unit attribute neither bound, calibrated nor accepted by name is refused")
        mut(lambda r: r.update(engine_parameters={"r0": {"value": 99, "source": "x", "reason": "x"}}),
            "engine_parameters", "ES21 (Codex r2-5) engine_parameters (not applied by the estimates path) are refused")
        mut(lambda r: r["observations"]["sources"].append(dict(r["observations"]["sources"][0], bias={"none": "x"})),
            "duplicate", "ES22 (Codex r2-6) two sources with one id are refused")
        mut(lambda r: r["observations"]["sources"][0]["bias"].append(
            {"mechanism": "under_ascertainment", "param": "rep2", "prior": {"dist": "uniform", "range": [0.1, 0.5]},
             "source": "x"}), "more than once", "ES22b (Codex r2-6) a mechanism repeated in one source is refused")
        mut(lambda r: r["observations"]["sources"][0]["latent"].update(quantity="attack_rate"), "incidence",
            "ES23 (Codex r2-7) a target source whose latent quantity is not incidence is refused")
        mut(lambda r: r["engine"]["config"].update(results_path="/tmp/forged.yaml"), "results_path",
            "ES24 (Codex r2-2) an engine configuration key that dynamics does not declare (results_path) is refused")

        def c1_priors(r):        # the priors of run 2026-11-06_012422 (c1), which failed at ANALYZE
            r["decision_units"].pop("defaults_accepted", None)
            r["calibration"]["priors"].update(
                r0={"dist": "uniform", "range": [1.3, 5.0], "source": "c1"},
                s0={"dist": "uniform", "range": [0.0, 1.0], "source": "c1"},
                seed={"dist": "loguniform", "range": [6.7e-07, 0.001], "source": "c1"})
        mut(c1_priors, "prior_support_infeasible",
            "ES25 (c1 run) a prior box with points the engine refuses (seed above s0 near s0 = 0) is refused before "
            "sign-off, naming the engine's reason")

        def c1_fixed(r):
            c1_priors(r)
            r["calibration"]["priors"]["s0"]["range"] = [0.05, 1.0]
        raw = copy.deepcopy(yaml.safe_load(open(sp)))
        c1_fixed(raw)
        keep = open(sp).read()
        yaml.safe_dump(raw, open(sp, "w"), sort_keys=False)
        e = schema.validate(schema.load(sp))
        open(sp, "w").write(keep)
        ok(e == [], f"ES25b the same priors with s0 from 0.05 (above the seed's upper end) validate: {e[:2]}")
        # ---- Codex review of the fix, round 1
        def beta_s0(r):
            r["decision_units"].pop("defaults_accepted", None)
            r["calibration"]["priors"].update(s0={"dist": "beta", "a": 1.0, "b": 1.0, "source": "x"},
                                              seed={"dist": "loguniform", "range": [1e-4, 5e-3], "source": "x"})
        mut(beta_s0, "prior_support_infeasible",
            "ES26 (Codex fix-r1 P1-2) a beta prior is probed at its support ends (0, 1), not its central 98%")

        def bound_s0(r):
            r["decision_units"].pop("defaults_accepted", None)
            r["decision_units"]["engine_attributes"]["s0"] = {"value": 1e-4, "source": "x"}
            r["calibration"]["priors"]["seed"] = {"dist": "loguniform", "range": [1e-5, 1e-3], "source": "x"}
        mut(bound_s0, "bound to data", "ES27 (Codex fix-r1 P3-6) a refusal that involves a data-bound attribute "
                                       "names it and its value")
        cs, info = probe_corners([[0.0, 1.0]] * 25, 64)
        ok(len(cs) == 64 and info["sampled"] and info["total"] == 2 ** 25,
           f"ES28 (Codex fix-r1 P2-3/4) 25 priors: 64 corners sampled without building all 2^25, and the sampling "
           f"is disclosed ({len(cs)}, {info})")
        try:
            cs, info = probe_corners([[0.0, 1.0]] * 70, 64)
            ok(len(set(cs)) == 64 and info["total"] == 2 ** 70,
               "ES28c (Codex fix-r2 P2-2) 70 priors: 64 distinct corners, no overflow")
        except OverflowError as e:
            ok(False, f"ES28c (Codex fix-r2 P2-2) 70 priors: 64 distinct corners, no overflow ({e})")
        cs, info = probe_corners([[0.0, 1.0], [2.0, 3.0]], 64)
        ok(sorted(cs) == [(0.0, 2.0), (0.0, 3.0), (1.0, 2.0), (1.0, 3.0)] and not info["sampled"],
           f"ES28b up to the cap every corner is probed: {cs}")
        # ---- structural uncertainty: excluded kinds, GI scale_range, transmissibility pool
        def check_valid(fn, label):
            raw, keep = copy.deepcopy(yaml.safe_load(open(sp))), open(sp).read()
            fn(raw)
            yaml.safe_dump(raw, open(sp, "w"), sort_keys=False)
            e = schema.validate(schema.load(sp))
            open(sp, "w").write(keep)
            ok(e == [], f"{label}: {e[:2]}")

        check_valid(lambda r: r["uncertainty"].update(excluded=[{"kind": "generation_interval", "reason": "toy: fixed"},
                    {"kind": "transmissibility_structure", "reason": "toy: constant"}]), "ES29 structural excluded validates")
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import render_claims
        lab = render_claims.interval_label({"level": 0.95, "covers": ["calibration", "observation"],
                                            "excludes": ["generation_interval", "transmissibility_structure"]})
        ok(lab == "95% UI over calibration, observation; not covered: generation interval, transmissibility structure", f"ES29a {lab!r}")
        for desc, exc_val, needle in (
            ("calibration", [{"kind": "calibration", "reason": "x"}], "cannot be excluded"),
            ("observation", [{"kind": "observation", "reason": "x"}], "cannot be excluded"),
            ("unknown", [{"kind": "costs", "reason": "x"}], "not in"),
            ("empty reason", [{"kind": "generation_interval", "reason": " "}], "non-empty string reason"),
            ("dup", [{"kind": "generation_interval", "reason": "a"}, {"kind": "generation_interval", "reason": "b"}], "duplicate"),
        ):
            mut(lambda r, v=exc_val: r["uncertainty"].update(excluded=v), needle, f"ES29b ({desc})")
        mut(lambda r: (r["dynamics"]["generation_interval"].update(scale_range={"range": [0.8, 1.2], "source": "toy"}),
                       r["uncertainty"]["sources"].append({"kind": "generation_interval"}),
                       r["uncertainty"].update(excluded=[{"kind": "generation_interval", "reason": "x"}])),
            "both sources and excluded", "ES29c both sources & excluded")
        from toolkit.engines.renewal.engine import check_generation_interval
        w0 = [0.2, 0.4, 0.3, 0.1]
        rescaled = [rescale_gi(w0, sc) for sc in (0.6, 0.8, 1.0, 1.25, 1.6)]
        for ws in rescaled:
            check_generation_interval({"dt": "week", "w": ws}, "week")
        means = [sum((i + 1) * v for i, v in enumerate(ws)) for ws in rescaled]
        tail_w = rescale_gi([1.0, 1e-18], 2.0)
        cap_refused = False
        try:
            rescale_gi([0.5, 0.5], 1e6)
        except ValueError:
            cap_refused = True
        ok(rescaled[2] == w0 and all(abs(sum(ws) - 1.0) < 1e-12 and ws[-1] > 0.0 for ws in rescaled)
           and all(b > a for a, b in zip(means, means[1:]))
           and len(tail_w) == 4 and tail_w[2] > 0.0 and tail_w[3] > 0.0 and cap_refused,
           f"ES30 rescale_gi exact, normalised, monotone, tail-preserving, lag-capped: {means}")
        check_valid(lambda r: (r["dynamics"]["generation_interval"].update(scale_range={"range": [0.8, 1.2], "steps": 5, "source": "toy"}),
                               r["uncertainty"]["sources"].append({"kind": "generation_interval"})), "ES31a scale_range")
        raw_close = copy.deepcopy(yaml.safe_load(open(sp)))
        raw_close["dynamics"]["generation_interval"]["scale_range"] = {"range": [0.9999999, 1.0000001], "steps": 3, "source": "toy"}
        vs_close, _ = structural_variants(raw_close)
        ok(len({v["id"] for v in vs_close}) == 3, f"ES31e distinct variant ids for close scales: {[v['id'] for v in vs_close]}")
        # Base GI weights [1, 1] must be refused even under asymmetric scale_range omitting 1.0
        mut(lambda r: (open(os.path.join(d, "data", "gi_bad.csv"), "w").write("lag,p\n1,1.0\n2,1.0\n"),
                       r["dynamics"]["generation_interval"].update(csv="data/gi_bad.csv", values=[1.0, 1.0],
                                                                   scale_range={"range": [0.8, 1.4], "steps": 3, "source": "toy"}),
                       r["uncertainty"]["sources"].append({"kind": "generation_interval"})),
            "sum", "ES31f invalid base GI refused even when asymmetric scale_range omits 1.0")
        mut(lambda r: r["dynamics"]["generation_interval"].update(scale_range={"range": [0.8, 1.2], "source": "toy"}), "requires 'generation_interval'", "ES31b")
        mut(lambda r: r["uncertainty"]["sources"].append({"kind": "generation_interval"}), "requires dynamics.generation_interval.scale_range", "ES31c")
        for desc, sr, needle in (("lo<=0", {"range": [0.0, 1.2], "source": "toy"}, "0 < lo"), ("no 1.0", {"range": [1.1, 1.5], "source": "toy"}, "1.0"),
                                 ("even", {"range": [0.8, 1.2], "steps": 4, "source": "toy"}, "odd"), ("no src", {"range": [0.8, 1.2], "source": ""}, "source")):
            mut(lambda r, sr=sr: (r["dynamics"]["generation_interval"].update(scale_range=sr),
                                  r["uncertainty"]["sources"].append({"kind": "generation_interval"})), needle, f"ES31d ({desc})")
        wk, pr1 = 7.0 / 365.25, {"transmissibility:1": {"dist": "loguniform", "range": [0.2, 5.0], "source": "toy"}}
        kn = lambda ks, pr: {"knots": ks, "priors": pr}  # noqa: E731
        pool_ok = {"pool": ["constant", kn([8 * wk], pr1)]}
        check_valid(lambda r: (r["dynamics"].update(transmissibility=copy.deepcopy(pool_ok)),
                               r["uncertainty"]["sources"].append({"kind": "transmissibility_structure"})), "ES32a pool")
        check_valid(lambda r: (r["dynamics"].update(transmissibility={"pool": [kn([8 * wk], pr1), "constant"], "weighting": "equal"}),
                               r["engine"]["config"].update(transmissibility={"knots": [8 * wk]}),
                               r["uncertainty"]["sources"].append({"kind": "transmissibility_structure"})), "ES32b knotted-first")
        mut(lambda r: r["dynamics"].update(transmissibility=copy.deepcopy(pool_ok)), "requires 'transmissibility_structure'", "ES32c")
        mut(lambda r: r["uncertainty"]["sources"].append({"kind": "transmissibility_structure"}), "requires dynamics.transmissibility.pool", "ES32d")
        for desc, tr_v, needle in (("<2", {"pool": ["constant"]}, ">=2"), ("dup", {"pool": ["constant", "constant"]}, "duplicate"),
                                   ("no prior", {"pool": ["constant", kn([8 * wk], {})]}, "must provide"),
                                   ("unsorted", {"pool": ["constant", kn([8 * wk, 4 * wk], {"transmissibility:1": pr1["transmissibility:1"],
                                                                                            "transmissibility:2": pr1["transmissibility:1"]})]}, "increasing"),
                                   ("infeasible", {"pool": ["constant", kn([8 * wk], {"transmissibility:1": {"dist": "loguniform", "range": [0.001, 50.0], "source": "toy"}})]},
                                    "prior_support_infeasible")):
            mut(lambda r, tv=tr_v: (r["dynamics"].update(transmissibility=copy.deepcopy(tv)),
                                    r["uncertainty"]["sources"].append({"kind": "transmissibility_structure"})), needle, f"ES32e ({desc})")
        mut(lambda r: (r["dynamics"]["generation_interval"].update(scale_range={"range": [0.8, 1.2], "steps": 9, "source": "toy"}),
                       r["dynamics"].update(transmissibility=copy.deepcopy(pool_ok)),
                       r["uncertainty"]["sources"].extend([{"kind": "generation_interval"}, {"kind": "transmissibility_structure"}])),
            "exceeds the cap", "ES32f >15 variants")
        with open(os.path.join(d, "data", "pop.csv"), "a") as f:
            f.write("b,5\n")
        e = schema.validate(schema.load(sp))
        ok(any("one population" in x for x in e), f"ES12 an estimates spec has one population row: {e[:1]}")
    # allocation specs are untouched: the toy allocation spec still validates
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        ok(schema.validate(schema.load(p)) == [], "ES13 an allocation spec validates unchanged")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
