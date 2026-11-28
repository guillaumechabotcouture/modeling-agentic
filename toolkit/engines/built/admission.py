#!/usr/bin/env python3
"""Admission gate for agent-built engines (docs/design/agent-built-engines.md §4 & §5).

    python3 toolkit/engines/built/admission.py --self-test

Deterministic harness verification of an immutable content-addressed snapshot against the signed
`engine.build` request, spec, and dossier:
- Public + hidden + real-unit typed domain generators with opaque IDs and invariance checks
- Static checks S1-S3 (before any run)
- Run checks A1-A2, A4-A6, A8, optional A11:
  - A1: conformance (`contract.check_engine` + exact card/parameter/output checks)
  - A2: determinism across processes, `PYTHONHASHSEED`, case order, and `seed_test_setting`
  - A4: analytic/oracle reductions (`oracles.py`) + absent-component & N-invariance identities
  - A5: mass balance, population conservation, and compartment transition-count witness (`starsim_module`)
  - A6: dead-parameter read tracking (`meta.param_reads`), `read_outcomes`-only phase check, non-trivial output movement, and declared-output liveness
  - A8: trial benchmarks (`benchmark.py`) + built-engine coverage policy (`benchmark_coverage.gate(..., policy="built")`)
  - A11: optional deterministic independent reference equivalence
- Hidden-seed commitment (`engine_build/.hidden/seed.json`), authoritative `engine_build/bench/*`,
  `engine_build/admission_sheet.md`, and atomic `engine_build/admission.json`. Nothing here names a disease.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import itertools
import json
import math
import os
import secrets
import sys
import tempfile
from collections.abc import Mapping
from statistics import NormalDist

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import safe_io as SIO  # noqa: E402
from toolkit.engines import benchmark as BM  # noqa: E402
from toolkit.engines import benchmark_coverage as BC  # noqa: E402
from toolkit.engines import benchmark_sources as BS  # noqa: E402
from toolkit.engines import contract as C  # noqa: E402
from toolkit.engines.contract import Horizon, Outcomes, Package, UnitSpec  # noqa: E402
from toolkit.engines.built import adapter as BADAPT  # noqa: E402
from toolkit.engines.built import oracles as BORC  # noqa: E402
from toolkit.engines.built import requested as BREQ  # noqa: E402
from toolkit.engines.built import snapshot as BSNAP  # noqa: E402
from toolkit.engines.built import worker as BWORK  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
THRESHOLDS_PATH = os.path.join(HERE, "thresholds.yaml")
PUBLIC_SEED_BYTES = hashlib.sha256(b"modeling-agentic:built-engine-public-seed-v1").digest()
HIDDEN_SEED_REL = "engine_build/.hidden/seed.json"


def load_thresholds() -> dict:
    import yaml
    with open(THRESHOLDS_PATH, encoding="utf-8") as f:
        return dict(yaml.safe_load(f) or {})


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _rng_from(seed_bytes: bytes, tag: str) -> np.random.Generator:
    digest = hashlib.sha256(seed_bytes + b":" + tag.encode("utf-8")).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _out_dict(out: Outcomes) -> dict:
    return {
        "per_capita": out.per_capita,
        "windows": out.windows,
        "cost_drivers": out.cost_drivers,
        "meta": out.meta or {},
    }


def _extract_output(out: Outcomes, name: str) -> float:
    """Resolve a declared output name (per_capita outcome, cost driver, window metric, or full path) to a scalar."""
    od = _out_dict(out)
    if "." in name:
        return float(BM.get(od, name))
    if name in (out.per_capita or {}):
        return float(BM.get(od, f"per_capita_all.{name}"))
    if name in (out.cost_drivers or {}):
        return float(out.cost_drivers[name])
    for wname in sorted(out.windows or {}):
        if name in (out.windows[wname] or {}):
            return float(out.windows[wname][name])
    raise KeyError(f"output {name!r} not found in Outcomes")


def _outcomes_close(a: Outcomes, b: Outcomes, *, rel: float = 1e-12, abs_tol: float = 1e-12) -> tuple[bool, float]:
    worst = 0.0
    if set(a.per_capita) != set(b.per_capita):
        return False, float("inf")
    for k, m in a.per_capita.items():
        bm = b.per_capita.get(k) or {}
        if set(m) != set(bm):
            return False, float("inf")
        for g, va in m.items():
            vb = bm.get(g)
            if not (_finite(va) and _finite(vb)):
                return False, float("inf")
            diff = abs(float(va) - float(vb))
            scale = max(abs(float(va)), abs(float(vb)), 1.0)
            worst = max(worst, diff / scale)
            if diff > max(abs_tol, rel * scale):
                return False, worst
    if set(a.windows) != set(b.windows):
        return False, float("inf")
    for w, m in a.windows.items():
        bm = b.windows.get(w) or {}
        if set(m) != set(bm):
            return False, float("inf")
        for mk, va in m.items():
            vb = bm.get(mk)
            if not (_finite(va) and _finite(vb)):
                return False, float("inf")
            diff = abs(float(va) - float(vb))
            scale = max(abs(float(va)), abs(float(vb)), 1.0)
            worst = max(worst, diff / scale)
            if diff > max(abs_tol, rel * scale):
                return False, worst
    if set(a.cost_drivers) != set(b.cost_drivers):
        return False, float("inf")
    for k, va in a.cost_drivers.items():
        vb = b.cost_drivers.get(k)
        if not (_finite(va) and _finite(vb)):
            return False, float("inf")
        diff = abs(float(va) - float(vb))
        scale = max(abs(float(va)), abs(float(vb)), 1.0)
        worst = max(worst, diff / scale)
        if diff > max(abs_tol, rel * scale):
            return False, worst
    return True, worst


def _same_val(a, b) -> bool:
    """Equality check keeping bools distinct from 0/1 ints (e.g. `False != 0`, `True != 1`)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _default_horizon(spec_raw: Mapping, req: Mapping) -> Horizon:
    sim_h = spec_raw.get("horizon") if isinstance(spec_raw, Mapping) else None
    obj = spec_raw.get("objective") if isinstance(spec_raw, Mapping) else None
    obj_hor = obj.get("horizon") if isinstance(obj, Mapping) else None
    if isinstance(sim_h, Mapping) and sim_h.get("start") is not None and sim_h.get("stop") is not None:
        start, stop = float(sim_h["start"]), float(sim_h["stop"])
    elif isinstance(obj_hor, (list, tuple)) and len(obj_hor) == 2:
        start, stop = float(obj_hor[0]), float(obj_hor[1])
    else:
        start, stop = 0.0, 100.0
    if isinstance(obj_hor, (list, tuple)) and len(obj_hor) == 2:
        win = (float(obj_hor[0]), float(obj_hor[1]))
    else:
        win = (start, stop)
    wins = dict((obj.get("windows") if isinstance(obj, Mapping) else None) or {})
    req_wins = (req.get("outputs") or {}).get("windows") or {}
    for w in req_wins:
        if str(w) not in wins:
            wins[str(w)] = [win[0], win[1]]
    return Horizon(start, stop, win, {str(k): [float(v[0]), float(v[1])] for k, v in wins.items()})


def _sample_attr(aspec: Mapping, rng: np.random.Generator, *, ref_vec=None, u_val: float | None = None):
    at = aspec.get("type")
    if at == "continuous":
        lo, hi = [float(x) for x in (aspec.get("range") or aspec.get("test_range") or [0.0, 1.0])]
        u = float(rng.random()) if u_val is None else float(u_val)
        if lo > 0.0 and hi / lo >= 50.0:
            return math.exp(math.log(lo) + u * (math.log(hi) - math.log(lo)))
        return lo + u * (hi - lo)
    if at == "integer":
        lo, hi = aspec.get("range") or aspec.get("test_range") or [0, 10]
        return int(rng.integers(int(lo), int(hi) + 1))
    if at == "categorical":
        vs = list(aspec.get("values") or [])
        return vs[int(rng.integers(0, len(vs)))]
    if at == "vector":
        ln = int(aspec.get("length", 1))
        vrng = aspec.get("range") if "range" in aspec else aspec.get("test_range")
        has_rng = isinstance(vrng, (list, tuple)) and len(vrng) == 2 and _finite(vrng[0]) and _finite(vrng[1])
        vlo, vhi = (float(vrng[0]), float(vrng[1])) if has_rng else (None, None)
        if isinstance(ref_vec, (list, tuple)) and len(ref_vec) == ln:
            scales = rng.uniform(0.6, 1.4, size=ln)
            out_v = []
            for x, sc in zip(ref_vec, scales):
                fx = float(x)
                cand_v = fx * float(sc) if fx != 0.0 else (0.5 * (vlo + vhi) if has_rng else 0.0)
                if has_rng:
                    cand_v = max(vlo, min(vhi, cand_v))
                elif fx > 0.0:
                    cand_v = max(1e-9, cand_v)
                out_v.append(float(cand_v))
            return out_v
        if has_rng:
            return [float(x) for x in rng.uniform(vlo, vhi, size=ln)]
        w = rng.uniform(0.2, 1.0, size=ln)
        return [float(x * 1000.0) for x in w]
    return 0.0


def _domain_points_attr(aspec: Mapping, *, ref_vec=None) -> list:
    """2 ends + 2 interior points (or all categorical/integer values) for A6 perturbation."""
    at = aspec.get("type")
    if at == "continuous":
        lo, hi = [float(x) for x in (aspec.get("range") or aspec.get("test_range") or [0.0, 1.0])]
        if lo > 0.0 and hi / lo >= 50.0:
            l0, l1 = math.log(lo), math.log(hi)
            return [lo, math.exp(l0 + 0.33 * (l1 - l0)), math.exp(l0 + 0.67 * (l1 - l0)), hi]
        return [lo, lo + 0.33 * (hi - lo), lo + 0.67 * (hi - lo), hi]
    if at == "integer":
        lo, hi = [int(x) for x in (aspec.get("range") or aspec.get("test_range") or [0, 3])]
        if hi - lo + 1 <= 4:
            return list(range(lo, hi + 1))
        return sorted({lo, lo + (hi - lo) // 3, lo + 2 * (hi - lo) // 3, hi})
    if at == "categorical":
        return list(aspec.get("values") or [])
    if at == "vector":
        ln = int(aspec.get("length", 1))
        vrng = aspec.get("range") if "range" in aspec else aspec.get("test_range")
        has_rng = isinstance(vrng, (list, tuple)) and len(vrng) == 2 and _finite(vrng[0]) and _finite(vrng[1])
        if has_rng:
            vlo, vhi = float(vrng[0]), float(vrng[1])
            if ln == 1:
                return [[vlo], [vlo + 0.33 * (vhi - vlo)], [vlo + 0.67 * (vhi - vlo)], [vhi]]
            ramp = [i / (ln - 1) for i in range(ln)]
            return [
                [vlo + (0.1 + 0.8 * r) * (vhi - vlo) for r in ramp],
                [vlo + 0.25 * (vhi - vlo)] * ln,
                [vlo + 0.75 * (vhi - vlo)] * ln,
                [vlo + (0.9 - 0.8 * r) * (vhi - vlo) for r in ramp],
            ]
        base = [float(x) for x in ref_vec] if isinstance(ref_vec, (list, tuple)) and len(ref_vec) == ln else [1000.0] * ln
        if ln == 1:
            return [[x * 0.5 for x in base], [x * 0.85 for x in base], [x * 1.15 for x in base], [x * 1.5 for x in base]]
        ramp = [i / (ln - 1) for i in range(ln)]
        return [
            [x * (0.4 + 1.2 * r) for x, r in zip(base, ramp)],
            [x * 0.7 for x in base],
            [x * 1.3 for x in base],
            [x * (1.6 - 1.2 * r) for x, r in zip(base, ramp)],
        ]
    return [0.0]


def _domain_points_param(ps: Mapping) -> list:
    dom = ps.get("domain") if isinstance(ps.get("domain"), Mapping) else {}
    if dom.get("values"):
        return list(dom["values"])
    rng = dom.get("range")
    if rng is None and _finite(ps.get("lo")) and _finite(ps.get("hi")):
        rng = [float(ps["lo"]), float(ps["hi"])]
    if rng is not None:
        lo, hi = float(rng[0]), float(rng[1])
        if dom.get("type") == "integer":
            ilo, ihi = int(round(lo)), int(round(hi))
            if ihi - ilo + 1 <= 4:
                return list(range(ilo, ihi + 1))
            return sorted({ilo, ilo + (ihi - ilo) // 3, ilo + 2 * (ihi - ilo) // 3, ihi})
        return [lo, lo + 0.33 * (hi - lo), lo + 0.67 * (hi - lo), hi]
    v = float(ps.get("value", 1.0))
    return [v * 0.5, v * 0.85, v * 1.15, v * 1.5] if abs(v) > 1e-12 else [0.0, 0.25, 0.75, 1.0]


def spec_units_and_packages(spec_raw: Mapping, req: Mapping, *, base_dir: str | None = None,
                            max_units: int = 200, max_packages: int = 64,
                            rng: np.random.Generator | None = None) -> tuple[list[UnitSpec], list[Package]]:
    """Extract the signed spec's real decision units and packages via `schema.Spec` (downsampled when > cap)."""
    from toolkit.spec import schema as _SCH
    rng = rng or np.random.default_rng(0)
    sp = _SCH.Spec(dict(spec_raw), base_dir or ".")
    attr_specs = req.get("unit_attributes") or {}
    comp_specs = req.get("components") or {}
    du = spec_raw.get("decision_units") if isinstance(spec_raw, Mapping) else None
    raw_units: list[dict] = []
    if isinstance(du, Mapping) and (isinstance(du.get("units_inline"), list)
                                    or (du.get("csv") and base_dir and os.path.isfile(os.path.join(base_dir, str(du["csv"]))))):
        raw_units = sp.units()
    id_col = str((du or {}).get("id") or (du or {}).get("id_column") or "unit_id") if isinstance(du, Mapping) else "unit_id"
    ea = (du or {}).get("engine_attributes") if isinstance(du, Mapping) else {}
    ea = ea if isinstance(ea, Mapping) else {}
    ab = req.get("attribute_binding") or {}
    cal = spec_raw.get("calibration") if isinstance(spec_raw, Mapping) else {}
    cal_attr = str(cal.get("attribute") or "").strip() if isinstance(cal, Mapping) else ""
    cal_scale_decl = cal.get("scale") if isinstance(cal, Mapping) else None
    cal_range = cal.get("range") if isinstance(cal, Mapping) else None
    cal_priors = (cal.get("priors") or {}) if isinstance(cal, Mapping) and isinstance(cal.get("priors"), Mapping) else {}

    def _effective_cal_scale(aspec: Mapping) -> str:
        if isinstance(cal_scale_decl, str) and cal_scale_decl in ("log", "linear"):
            return cal_scale_decl
        rng_a = aspec.get("range") if "range" in aspec else aspec.get("test_range")
        if isinstance(rng_a, (list, tuple)) and len(rng_a) >= 1 and _finite(rng_a[0]):
            return "linear" if float(rng_a[0]) < 0.0 else "log"
        return "linear"

    def _coerce_attr(raw_val, aspec: Mapping):
        at = aspec.get("type")
        if at == "continuous":
            return float(raw_val)
        if at == "integer":
            return int(round(float(raw_val)))
        if at == "categorical":
            vs = aspec.get("values") or []
            if any(_same_val(raw_val, v) for v in vs):
                return raw_val
            sval = str(raw_val).strip()
            for v in vs:
                if str(v) == sval:
                    return v
            return raw_val
        if at == "vector":
            if isinstance(raw_val, str):
                return [float(x) for x in json.loads(raw_val)]
            if isinstance(raw_val, (list, tuple)):
                return [float(x) for x in raw_val]
        return raw_val

    units: list[UnitSpec] = []
    for idx, row in enumerate(raw_units):
        uid = str(row.get(id_col) or f"unit_{idx}")
        bound_attrs = sp.unit_attributes(row) if ea else {}
        attrs: dict = {}
        for aname, aspec in attr_specs.items():
            if aname in ea:
                bind = ea[aname]
                if isinstance(bind, Mapping) and isinstance(bind.get("columns"), Mapping) and aspec.get("type") == "vector":
                    raise ValueError(
                        f"real unit {uid!r}: vector attribute {aname!r} requires a list of column names, not a mapping"
                    )
                val = bound_attrs[aname]
                if isinstance(bind, Mapping) and ("column" in bind) and "split" not in bind:
                    val = _coerce_attr(val, aspec)
            elif aname in row:
                val = _coerce_attr(row[aname], aspec)
            elif isinstance(ab.get(aname), Mapping) and "default" in ab[aname]:
                val = copy.deepcopy(ab[aname]["default"])
            elif (cal_attr and aname == cal_attr) or (aname in cal_priors):
                rng_a = aspec.get("range") if "range" in aspec else aspec.get("test_range")
                pr_decl = cal_priors.get(aname) if isinstance(cal_priors.get(aname), Mapping) else None
                pr_rng = pr_decl.get("range") if pr_decl is not None else None
                eff_scale = _effective_cal_scale(aspec) if aname == cal_attr else "linear"
                if aname == cal_attr and isinstance(cal_range, (list, tuple)) and len(cal_range) == 2:
                    mid = 0.5 * (float(cal_range[0]) + float(cal_range[1]))
                    val = math.exp(mid) if eff_scale == "log" else mid
                elif isinstance(pr_rng, (list, tuple)) and len(pr_rng) == 2:
                    p0, p1 = float(pr_rng[0]), float(pr_rng[1])
                    if pr_decl and pr_decl.get("dist") == "loguniform" and p0 > 0 and p1 > 0:
                        val = math.sqrt(p0 * p1)
                    else:
                        mid = 0.5 * (p0 + p1)
                        val = math.exp(mid) if eff_scale == "log" else mid
                elif isinstance(rng_a, (list, tuple)) and len(rng_a) == 2:
                    val = 0.5 * (float(rng_a[0]) + float(rng_a[1]))
                else:
                    raise ValueError(f"real unit {uid!r}: calibrated attribute {aname!r} has no declared range")
                if isinstance(rng_a, (list, tuple)) and len(rng_a) == 2:
                    val = max(float(rng_a[0]), min(float(rng_a[1]), float(val)))
            else:
                raise ValueError(f"real unit {uid!r}: missing required attribute {aname!r} (no binding or default)")
            attrs[aname] = val
        units.append(UnitSpec(uid, attrs))

    if len(units) > max_units:
        keep_idx: set[int] = set()
        for aname, aspec in attr_specs.items():
            if aspec.get("type") in ("continuous", "integer"):
                vals = [(float(u.attributes[aname]), i) for i, u in enumerate(units)]
                vals.sort()
                keep_idx.add(vals[0][1])
                keep_idx.add(vals[-1][1])
        rem = [i for i in range(len(units)) if i not in keep_idx]
        need = max(0, max_units - len(keep_idx))
        if need and rem:
            chosen = rng.choice(rem, size=min(need, len(rem)), replace=False).tolist()
            keep_idx.update(int(i) for i in chosen)
        units = [units[i] for i in sorted(keep_idx)[:max_units]]

    opts = spec_raw.get("options") if isinstance(spec_raw, Mapping) else {}
    emap = (opts.get("engine_map") or {}) if isinstance(opts, Mapping) else {}
    opt_comps = (opts.get("components") or {}) if isinstance(opts, Mapping) else {}
    pks_decl = (opts.get("packages") or {}) if isinstance(opts, Mapping) else {}
    absent_pkg = {c: cs["absent"] for c, cs in comp_specs.items()}
    packages: list[Package] = []

    if (isinstance(pks_decl, Mapping) and pks_decl and not pks_decl.get("generate")) or (
        isinstance(opt_comps, Mapping) and opt_comps and isinstance(emap, Mapping) and emap
    ):
        # Delegate option-package & mutually-exclusive-group expansion to schema.Spec (within_envelope / alloc_costs)
        if isinstance(pks_decl, Mapping) and pks_decl.get("generate"):
            groups: dict[str, list[str]] = {}
            for oc, od in opt_comps.items():
                grp = str((od or {}).get("group") or oc) if isinstance(od, Mapping) else str(oc)
                groups.setdefault(grp, []).append(str(oc))
            est_combos = math.prod(1 + len(vs) for vs in groups.values())
        else:
            est_combos = len(pks_decl)
        if est_combos <= max_packages:
            for pid, comps_set in sp.packages().items():
                comp_map = dict(absent_pkg)
                comp_map.update(sp.engine_package(comps_set))
                packages.append(Package(str(pid), comp_map))
        else:
            # Large group product: sample without materializing full Cartesian product
            groups = {}
            for oc, od in opt_comps.items():
                grp = str((od or {}).get("group") or oc) if isinstance(od, Mapping) else str(oc)
                groups.setdefault(grp, []).append(str(oc))
            group_choices = [[None] + sorted(vs) for _gk, vs in sorted(groups.items())]
            seen_opts: set[tuple[str, ...]] = {()}
            ordered_sets: list[tuple[str, ...]] = [()]
            for oc in sorted(opt_comps):
                t = (str(oc),)
                if t not in seen_opts and len(ordered_sets) < max_packages:
                    seen_opts.add(t)
                    ordered_sets.append(t)
            attempts = 0
            while len(ordered_sets) < max_packages and attempts < max_packages * 12:
                attempts += 1
                chosen_opts = tuple(sorted(
                    c for c in (ch[int(rng.integers(0, len(ch)))] for ch in group_choices)
                    if c is not None
                ))
                if chosen_opts not in seen_opts:
                    seen_opts.add(chosen_opts)
                    ordered_sets.append(chosen_opts)
            for opt_tuple in ordered_sets[:max_packages]:
                comp_map = dict(absent_pkg)
                comp_map.update(sp.engine_package(opt_tuple))
                packages.append(Package("+".join(opt_tuple) if opt_tuple else "none", comp_map))
        if len(packages) > max_packages:
            idx = sorted(set([0, len(packages) - 1]
                             + rng.choice(len(packages), size=max(0, max_packages - 2), replace=False).tolist()))
            packages = [packages[i] for i in idx]
    else:
        names = sorted(comp_specs)
        val_lists = [list(comp_specs[c]["values"]) for c in names]
        total_combos = math.prod(len(vs) for vs in val_lists) if val_lists else 0
        if 0 < total_combos <= max_packages:
            for idx, vals in enumerate(itertools.product(*val_lists)):
                packages.append(Package(f"pkg_{idx}", dict(zip(names, vals))))
        elif total_combos > max_packages:
            seen_tups: set[tuple] = set()
            chosen_tups: list[tuple] = []

            def _add_tup(t: tuple) -> None:
                key = tuple((type(x).__name__, repr(x)) for x in t)
                if key not in seen_tups and len(chosen_tups) < max_packages:
                    seen_tups.add(key)
                    chosen_tups.append(t)

            _add_tup(tuple(comp_specs[c]["absent"] for c in names))
            _add_tup(tuple(comp_specs[c]["values"][-1] for c in names))
            for ci, c in enumerate(names):
                for v in comp_specs[c]["values"]:
                    if v != comp_specs[c]["absent"]:
                        row_t = [comp_specs[cc]["absent"] for cc in names]
                        row_t[ci] = v
                        _add_tup(tuple(row_t))
            attempts = 0
            while len(chosen_tups) < max_packages and attempts < max_packages * 12:
                attempts += 1
                _add_tup(tuple(vs[int(rng.integers(0, len(vs)))] for vs in val_lists))
            for idx, vals in enumerate(chosen_tups[:max_packages]):
                packages.append(Package(f"pkg_{idx}", dict(zip(names, vals))))
    if not packages:
        packages = [Package("none", dict(absent_pkg))]
    return units, packages


def generate_domain_units(req: Mapping, n: int, rng: np.random.Generator, *, ref_units: list[UnitSpec] | None = None) -> list[UnitSpec]:
    """Generate `n` synthetic units across the declared typed attribute domains using LHS for continuous dims."""
    attr_specs = req.get("unit_attributes") or {}
    cont_names = sorted(k for k, v in attr_specs.items() if v.get("type") == "continuous")
    lhs_draws = (BM.lhs(n, len(cont_names), int(rng.integers(0, 2**31 - 1)))
                 if cont_names and n > 0 else np.zeros((n, 0)))
    ref_vecs = {}
    for u in (ref_units or []):
        for k, v in u.attributes.items():
            if isinstance(v, list) and k not in ref_vecs:
                ref_vecs[k] = v
    out: list[UnitSpec] = []
    for i in range(n):
        attrs = {}
        c_idx = 0
        for aname in sorted(attr_specs):
            aspec = attr_specs[aname]
            if aspec.get("type") == "continuous":
                attrs[aname] = _sample_attr(aspec, rng, u_val=float(lhs_draws[i, c_idx]))
                c_idx += 1
            else:
                attrs[aname] = _sample_attr(aspec, rng, ref_vec=ref_vecs.get(aname))
        out.append(UnitSpec(f"gen_{i}", attrs))
    return out


# --------------------------------------------------------------------------- Individual checks A1-A8, A11
def check_a1(cand: BADAPT.BuiltEngine, build_decl: Mapping, spec_raw: Mapping,
             units: list[UnitSpec], packages: list[Package], horizon: Horizon, *, seed: int = 1) -> dict:
    """A1 conformance: `contract.check_engine` + exact card/parameter/output agreement with signed request."""
    import yaml
    req = build_decl["request"]
    probs: list[str] = []
    card = cand.describe()
    params = cand.parameters()

    # 1. card.yaml agrees with describe()
    try:
        card_doc = yaml.safe_load(cand._files["card.yaml"].decode("utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        probs.append(f"card.yaml unreadable: {e}")
        card_doc = {}
    for field in ("name", "version", "mechanisms", "components", "outputs", "age_groups"):
        if field in card_doc and card_doc[field] != getattr(card, field):
            probs.append(f"describe().{field} disagrees with card.yaml")

    # 2. describe() agrees with signed request on name, components, unit_attributes, outputs, age_groups
    want_name = f"built:{build_decl['id']}"
    if card.name != want_name:
        probs.append(f"card.name {card.name!r} != expected {want_name!r}")
    req_comps = {c: list(cs["values"]) for c, cs in (req.get("components") or {}).items()}
    if card.components != req_comps:
        probs.append(f"card.components {card.components!r} != request {req_comps!r}")
    if set(card.unit_attributes) != set(req.get("unit_attributes") or {}):
        probs.append(f"card.unit_attributes {sorted(card.unit_attributes)} != request {sorted(req.get('unit_attributes') or {})}")
    if list(card.age_groups) != list(req.get("age_groups") or []):
        probs.append(f"card.age_groups {card.age_groups!r} != request {req.get('age_groups')!r}")
    req_outs = req.get("outputs") or {}
    if list(card.outputs.get("per_capita") or []) != list(req_outs.get("per_capita") or []):
        probs.append("card.outputs.per_capita differs from request")
    if list(card.outputs.get("cost_drivers") or []) != list(req_outs.get("cost_drivers") or []):
        probs.append("card.outputs.cost_drivers differs from request")
    req_win_set = {str(m) for ms in (req_outs.get("windows") or {}).values() for m in (ms or [])}
    card_win_set = {str(m) for m in (card.outputs.get("windows") or [])}
    if card_win_set != req_win_set:
        probs.append(f"card.outputs.windows {sorted(card_win_set)} != request {sorted(req_win_set)}")

    # 3. parameters() matches request.parameters EXACTLY on name, kind, value, lo, hi, units, source (BE13)
    req_params = {p["name"]: p for p in (req.get("parameters") or [])}
    got_params = {p.name: p for p in params}
    if set(got_params) != set(req_params):
        probs.append(f"parameter names differ: extra={sorted(set(got_params) - set(req_params))}, "
                     f"missing={sorted(set(req_params) - set(got_params))}")
    for pname, rp in req_params.items():
        gp = got_params.get(pname)
        if gp is None:
            continue
        if gp.kind != rp.get("kind"):
            probs.append(f"parameter {pname}: kind {gp.kind!r} != request {rp.get('kind')!r}")
        if abs(float(gp.value) - float(rp["value"])) > 1e-12:
            probs.append(f"parameter {pname}: value {gp.value} != request {rp['value']}")
        for bkey in ("lo", "hi"):
            rv, gv = rp.get(bkey), getattr(gp, bkey)
            if (rv is None) != (gv is None) or (rv is not None and abs(float(gv) - float(rv)) > 1e-12):
                probs.append(f"parameter {pname}: {bkey} {gv!r} != request {rv!r}")
        if (rp.get("units") or "") != (gp.units or ""):
            probs.append(f"parameter {pname}: units {gp.units!r} != request {rp.get('units')!r}")
        if str(rp.get("source") or "") != str(gp.source or ""):
            probs.append(f"parameter {pname}: source {gp.source!r} != request {rp.get('source')!r}")

    # 4. Run contract.check_engine + strict built-output checks on each case (batched via run_many)
    req_win_metrics = req_outs.get("windows") or {}
    pairs = [(u, pk) for u in units for pk in packages]
    n_total = len(pairs)
    n_pass = 0
    if pairs:
        first_probs = C.check_engine(cand, pairs[0][0], pairs[0][1], horizon, seed=seed)
        outs = cand.run_many([({}, u, pk, horizon, seed) for u, pk in pairs])
    else:
        first_probs, outs = [], []
    for idx, ((u, pk), out) in enumerate(zip(pairs, outs)):
        case_probs: list[str] = list(first_probs) if idx == 0 else []
        for k in req_outs.get("per_capita") or []:
            if k not in out.per_capita:
                case_probs.append(f"missing per_capita.{k}")
                continue
            for g in card.age_groups:
                v = (out.per_capita[k] or {}).get(g)
                if not _finite(v) or float(v) < 0.0:
                    case_probs.append(f"{u.id}/{pk.id}: per_capita.{k}[{g}]={v!r} (must be finite >= 0)")
        for wname, metrics in req_win_metrics.items():
            if wname in horizon.windows:
                wmap = out.windows.get(wname)
                if not isinstance(wmap, Mapping) or not wmap:
                    case_probs.append(f"{u.id}/{pk.id}: window {wname!r} missing or empty")
                else:
                    for m in metrics:
                        mv = wmap.get(m)
                        if not _finite(mv):
                            case_probs.append(f"{u.id}/{pk.id}: window {wname}.{m}={mv!r} not finite")
        for drv in req_outs.get("cost_drivers") or []:
            dv = (out.cost_drivers or {}).get(drv)
            if not _finite(dv) or float(dv) < 0.0:
                case_probs.append(f"{u.id}/{pk.id}: cost_driver {drv}={dv!r} (must be finite >= 0)")
        if not case_probs:
            n_pass += 1
        else:
            probs.extend(case_probs[:3])
    return {"id": "A1", "status": "pass" if not probs else "fail", "passed": n_pass, "total": n_total,
            "problems": probs[:12]}


def check_a2(cand: BADAPT.BuiltEngine, build_decl: Mapping, units: list[UnitSpec],
             packages: list[Package], horizon: Horizon, *, th: Mapping, check_invariance: bool = True) -> dict:
    """A2 determinism under seed across processes, PYTHONHASHSEED, case orders, opaque IDs, and seed_test_setting."""
    req = build_decl["request"]
    stochastic = bool(req.get("stochastic", False))
    tol = float(th["invariance_rel_tol"])
    probs: list[str] = []
    cases = [(dict(), u, packages[i % len(packages)], horizon, 100 + i) for i, u in enumerate(units)]
    if not cases:
        return {"id": "A2", "status": "fail", "passed": 0, "total": 0, "problems": ["no cases generated for A2"]}
    # 1. Forward order with hash_seed=0 vs reversed order with hash_seed=42 (separate worker processes!)
    fwd = cand.run_many(cases, hash_seed="0", id_salt="salt_a")
    rev = list(reversed(cand.run_many(list(reversed(cases)), hash_seed="42", id_salt="salt_b" if check_invariance else "salt_a")))
    n_pass = 0
    for idx, (oa, ob) in enumerate(zip(fwd, rev)):
        same, worst = _outcomes_close(oa, ob, rel=tol, abs_tol=tol)
        if same:
            n_pass += 1
        else:
            probs.append(f"case {idx}: differs across process/PYTHONHASHSEED/order/opaque-id (rel={worst:.3g})")
    # 2. Seed sensitivity at request.seed_test_setting (BE6)
    sts = req.get("seed_test_setting")
    if isinstance(sts, Mapping) and isinstance(sts.get("unit"), Mapping):
        su = UnitSpec("seed_test", dict(sts["unit"]))
    else:
        su = units[0]
    spkg = packages[0]
    o_s1, o_s2 = cand.run_many([({}, su, spkg, horizon, 101), ({}, su, spkg, horizon, 202)], hash_seed="0")
    same_seeds, _ = _outcomes_close(o_s1, o_s2, rel=tol, abs_tol=tol)
    if stochastic and same_seeds:
        probs.append("stochastic engine produced identical outcomes for different seeds at seed_test_setting")
    elif not stochastic and not same_seeds:
        probs.append("deterministic engine produced different outcomes for different seeds at seed_test_setting")
    return {"id": "A2", "status": "pass" if not probs else "fail", "passed": n_pass, "total": len(cases),
            "problems": probs[:12]}


def _within_tol(got, want, tol: Mapping) -> tuple[bool, float]:
    if isinstance(want, (list, tuple)):
        if not isinstance(got, (list, tuple)) or len(got) != len(want):
            return False, float("inf")
        worst = 0.0
        for g, w in zip(got, want):
            ok_, err = _within_tol(g, w, tol)
            worst = max(worst, err)
            if not ok_:
                return False, worst
        return True, worst
    g, w = float(got), float(want)
    if not (math.isfinite(g) and math.isfinite(w)):
        return False, float("inf")
    diff = abs(g - w)
    ok_abs = ("abs" in tol) and diff <= float(tol["abs"])
    ok_rel = ("rel" in tol) and diff <= float(tol["rel"]) * max(abs(w), 1e-12)
    return (ok_abs or ok_rel), diff


def check_a4(cand: BADAPT.BuiltEngine, build_decl: Mapping, spec_raw: Mapping,
             rng: np.random.Generator, *, n_free: int, th: Mapping, ref_units: list[UnitSpec] | None = None) -> dict:
    """A4 analytic/oracle reductions + harness-derived absent-component & N-invariance identities (BE10)."""
    req = build_decl["request"]
    comp_specs = req.get("components") or {}
    attr_specs = req.get("unit_attributes") or {}
    param_specs = {p["name"]: p for p in (req.get("parameters") or [])}
    absent_pkg = {c: cs["absent"] for c, cs in comp_specs.items()}
    default_hor = _default_horizon(spec_raw, req)
    probs: list[str] = []
    n_total, n_pass = 0, 0
    details: list[dict] = []

    # Collect all A4 evaluations (reductions + absent identity + N-invariance) and execute in one run_many call
    batch_cases: list[tuple[dict, UnitSpec, Package, Horizon, int]] = []
    red_plan: list[tuple[int, str, int, object, dict]] = []

    # 1. Declared reductions × n_free settings of inputs left free by the reduction
    for red in (req.get("reductions") or []):
        rid = str(red["id"])
        expected = BORC.call_oracle(red["oracle"], red["oracle_args"])
        rset = red.get("set") or {}
        runit = red.get("unit") or {}
        rhor_d = red.get("horizon")
        if isinstance(rhor_d, Mapping):
            rwin = tuple(rhor_d.get("window") or (rhor_d["start"], rhor_d["stop"]))
            rwins = {str(k): [float(v[0]), float(v[1])] for k, v in (rhor_d.get("windows") or {}).items()}
            for wn in ((req.get("outputs") or {}).get("windows") or {}):
                if str(wn) not in rwins:
                    rwins[str(wn)] = [float(rwin[0]), float(rwin[1])]
            rhor = Horizon(float(rhor_d["start"]), float(rhor_d["stop"]), rwin, rwins)
        else:
            rhor = default_hor
        free_units = generate_domain_units(req, n_free, rng, ref_units=ref_units)
        for fi, fu in enumerate(free_units):
            u_attrs = dict(fu.attributes)
            u_attrs.update(runit)
            prm = {}
            pkg_c = dict(absent_pkg)
            for k, v in rset.items():
                if k in param_specs:
                    prm[k] = float(v)
                elif k in comp_specs:
                    pkg_c[k] = v
            if fi > 0:
                for pn, ps in param_specs.items():
                    if pn not in prm and ps.get("kind") != "calibrated":
                        pts = _domain_points_param(ps)
                        prm[pn] = float(pts[int(rng.integers(0, len(pts)))])
            idx_case = len(batch_cases)
            batch_cases.append((prm, UnitSpec(f"red_{rid}_{fi}", u_attrs), Package(f"red_{rid}", pkg_c), rhor, 11 + fi))
            red_plan.append((idx_case, rid, fi, expected, dict(red.get("compare") or {})))

    # 2. Identity: all components at absent values equals empty package {}
    id_u = generate_domain_units(req, 1, rng, ref_units=ref_units)[0]
    idx_empty = len(batch_cases)
    batch_cases.append(({}, id_u, Package("empty", {}), default_hor, 17))
    idx_abs = len(batch_cases)
    batch_cases.append(({}, id_u, Package("absent", dict(absent_pkg)), default_hor, 17))

    # 3. Identity: per_capita invariant to N when declared
    check_n_inv = (
        bool(req.get("per_capita_invariant_to_N"))
        and "N" in attr_specs
        and attr_specs["N"].get("type") in ("continuous", "integer")
    )
    idx_n1 = idx_n2 = -1
    n1 = n2 = 0.0
    if check_n_inv:
        n_lo, n_hi = attr_specs["N"].get("range") or attr_specs["N"].get("test_range") or [1000.0, 10000.0]
        n1 = float(n_lo) + 0.25 * (float(n_hi) - float(n_lo))
        n2 = float(n_lo) + 0.75 * (float(n_hi) - float(n_lo))
        if attr_specs["N"].get("type") == "integer":
            n1, n2 = int(round(n1)), int(round(n2))
        idx_n1 = len(batch_cases)
        batch_cases.append(({}, UnitSpec("n_inv_1", dict(id_u.attributes, N=n1)), Package("none", dict(absent_pkg)), default_hor, 19))
        idx_n2 = len(batch_cases)
        batch_cases.append(({}, UnitSpec("n_inv_2", dict(id_u.attributes, N=n2)), Package("none", dict(absent_pkg)), default_hor, 19))

    outs = cand.run_many(batch_cases) if batch_cases else []
    ag_list = list(req.get("age_groups") or [])
    for idx_case, rid, fi, expected, compare_map in red_plan:
        out = outs[idx_case]
        od = _out_dict(out)
        for path, tol in compare_map.items():
            n_total += 1
            if path.startswith("per_capita_vec."):
                onm = path.split(".", 1)[1]
                got = [float((out.per_capita.get(onm) or {}).get(g, float("nan"))) for g in ag_list]
                want_val = expected
            else:
                got = BM.get(od, path)
                want_val = expected
                if (isinstance(expected, (list, tuple)) and path.startswith("per_capita.")
                        and len(path.split(".")) == 3):
                    grp = path.split(".")[2]
                    if grp in ag_list and len(expected) == len(ag_list):
                        want_val = expected[ag_list.index(grp)]
            ok_, err = _within_tol(got, want_val, tol)
            details.append({"id": f"{rid}[{fi}]:{path}", "expected": want_val, "computed": got, "ok": ok_})
            if ok_:
                n_pass += 1
            else:
                probs.append(f"{rid}[{fi}] {path}: computed={got!r}, expected={want_val!r} (err={err:.4g}, tol={tol})")

    n_total += 1
    same_abs, worst_abs = _outcomes_close(outs[idx_empty], outs[idx_abs], rel=1e-12, abs_tol=1e-12)
    if same_abs:
        n_pass += 1
    else:
        probs.append(f"absent-components identity failed (rel={worst_abs:.3g})")

    if check_n_inv:
        o1, o2 = outs[idx_n1], outs[idx_n2]
        n_total += 1
        same_n = True
        tol_n = 0.05 if req.get("stochastic") else 1e-6
        for k, m in o1.per_capita.items():
            for g, v1 in m.items():
                v2 = (o2.per_capita.get(k) or {}).get(g, float("nan"))
                if abs(float(v1) - float(v2)) > max(1e-9, tol_n * max(abs(float(v1)), abs(float(v2)), 1.0)):
                    same_n = False
                    probs.append(f"per_capita_invariant_to_N failed on {k}[{g}]: N={n1}->{v1:.5g}, N={n2}->{v2:.5g}")
        if same_n:
            n_pass += 1

    return {"id": "A4", "status": "pass" if not probs else "fail", "passed": n_pass, "total": n_total,
            "problems": probs[:12], "details": details[:12]}


def check_a5(cand: BADAPT.BuiltEngine, build_decl: Mapping, units: list[UnitSpec],
             packages: list[Package], horizon: Horizon, *, th: Mapping) -> dict:
    """A5 mass balance, population conservation, and compartment transition-count witness (BE8, BE9)."""
    if build_decl.get("kind") != "starsim_module":
        return {"id": "A5", "status": "not_applicable", "passed": 0, "total": 0, "problems": []}
    req = build_decl["request"]
    cmps = req.get("compartments") or {}
    decl_states = list(cmps.get("states") or [])
    conservation = str(cmps.get("conservation") or "closed")
    transitions = list(cmps.get("transitions") or [])
    bal_tol = float(th["mass_balance_rel_tol"])
    tr_tol = float(th["transition_count_rel_tol"])
    probs: list[str] = []
    n_total, n_pass = 0, 0

    # Identify pure-exit states (no inflows) and pure-entry states (no outflows) for transition-count verification (BE9)
    in_edges: dict[str, list[dict]] = {s: [] for s in decl_states}
    out_edges: dict[str, list[dict]] = {s: [] for s in decl_states}
    for tr in transitions:
        frm, to = tr.get("from"), tr.get("to")
        if frm in out_edges:
            out_edges[frm].append(tr)
        if to in in_edges:
            in_edges[to].append(tr)

    a5_cases = [({}, u, packages[idx % len(packages)], horizon, 23 + idx) for idx, u in enumerate(units)]
    a5_outs = cand.run_many(a5_cases) if a5_cases else []
    for idx, (u, out) in enumerate(zip(units, a5_outs)):
        n_total += 1
        c_probs: list[str] = []
        bal = (out.meta or {}).get("balance")
        if not isinstance(bal, dict) or not bal.get("n_steps"):
            c_probs.append(f"{u.id}: missing harness balance trace (state_vars() empty or omitted)")
        else:
            init_s = bal.get("initial_states") or {}
            final_s = bal.get("final_states") or {}
            miss = [s for s in decl_states if s not in init_s]
            if miss:
                c_probs.append(f"{u.id}: state_vars() missing declared state(s) {miss}")
            mn = bal.get("min_state")
            if mn is None or not _finite(mn) or float(mn) < -1e-9:
                c_probs.append(f"{u.id}: negative or non-finite state detected (min_state={mn!r})")
            if conservation == "closed":
                drift = bal.get("max_rel_drift")
                if drift is None or not _finite(drift) or float(drift) > bal_tol:
                    c_probs.append(f"{u.id}: closed conservation violated (max_rel_drift={drift!r} > {bal_tol})")
            # Transition-count witness (BE9): group all transitions counting the same output; if every
            # transition counting `cname` has a pure-exit or pure-entry witness, compare their summed
            # witness count divided by total population N0 against the reported output.
            n0 = float(bal.get("initial_total") or 0.0)
            if n0 > 0 and not c_probs:
                by_count: dict[str, list[tuple[str, str, float | None]]] = {}
                for tr in transitions:
                    frm, to = tr.get("from"), tr.get("to")
                    cnts = list(tr.get("counts") or [])
                    if not cnts:
                        continue
                    witness_count = None
                    # Pure exit state with a single outgoing transition (e.g. S -> I in SIR)
                    if frm in out_edges and len(out_edges[frm]) == 1 and len(in_edges[frm]) == 0 and frm in init_s:
                        witness_count = float(init_s[frm]) - float(final_s[frm])
                    # Pure sink state with a single incoming transition (e.g. I -> H in SIRH)
                    elif to in in_edges and len(in_edges[to]) == 1 and len(out_edges[to]) == 0 and to in init_s:
                        witness_count = float(final_s[to]) - float(init_s[to])
                    for cname in cnts:
                        by_count.setdefault(str(cname), []).append((str(frm), str(to), witness_count))
                for cname, entries in by_count.items():
                    if all(w is not None for _, _, w in entries):
                        want_pc = sum(float(w) for _, _, w in entries) / n0
                        got_pc = _extract_output(out, cname)
                        if abs(got_pc - want_pc) > max(1e-6, tr_tol * max(abs(want_pc), 1.0)):
                            tr_desc = "+".join(f"{f}->{t}" for f, t, _ in entries)
                            c_probs.append(
                                f"{u.id}: output {cname!r}={got_pc:.6g} disagrees with signed transition "
                                f"{tr_desc} witness {want_pc:.6g} (computed apart from dynamics)"
                            )
        if not c_probs:
            n_pass += 1
        else:
            probs.extend(c_probs[:3])
    return {"id": "A5", "status": "pass" if not probs else "fail", "passed": n_pass, "total": n_total,
            "problems": probs[:12]}


def check_a6(cand: BADAPT.BuiltEngine, build_decl: Mapping, spec_raw: Mapping,
             rng: np.random.Generator, *, n_bases: int, min_moving: int, th: Mapping,
             ref_units: list[UnitSpec] | None = None) -> dict:
    """A6 wiring & liveness sanity (BE2, BE3, BE5):
    - Every non-inert declared parameter must be read (`meta.param_reads`), must not be read ONLY in
      `read_outcomes()` without `parameter_roles`, and must move at least one declared output above
      the magnitude floor (`move_abs_floor`, `move_rel_floor`) across `>= min_moving` base points.
    - Every non-inert component with >= 2 values must move at least one declared output above floor.
    - Every declared output (`per_capita`, `cost_drivers`, `windows`) must move under at least one input."""
    req = build_decl["request"]
    abs_floor = float(th["move_abs_floor"])
    rel_floor = float(th["move_rel_floor"])
    stochastic = bool(req.get("stochastic", False))
    k_seeds = int(th["stochastic_seeds_init"]) if stochastic else 1
    z_crit = NormalDist().inv_cdf(0.5 + float(th["move_ci_level"]) / 2.0)

    comp_specs = req.get("components") or {}
    attr_specs = req.get("unit_attributes") or {}
    param_specs = {p["name"]: p for p in (req.get("parameters") or [])}
    inert = set((req.get("inert") or {}).keys())
    param_roles = dict(req.get("parameter_roles") or {})
    absent_pkg = {c: cs["absent"] for c, cs in comp_specs.items()}
    hor = _default_horizon(spec_raw, req)

    # Canonical output paths tracked for liveness and movement checks
    req_outs = req.get("outputs") or {}
    out_paths: list[str] = []
    for pc in (req_outs.get("per_capita") or []):
        out_paths.append(f"per_capita_all.{pc}")
    for drv in (req_outs.get("cost_drivers") or []):
        out_paths.append(f"cost.{drv}")
    for wname, metrics in sorted((req_outs.get("windows") or {}).items()):
        for m in (metrics or []):
            out_paths.append(f"windows.{wname}.{m}")

    # Map intervention decision inputs (parameter / unit_attribute) to owning active component modes
    input_to_comps: dict[tuple[str, str], list[tuple[str, list]]] = {}
    for cname, spec_or_items in sorted((req.get("decision_inputs") or {}).items()):
        items = spec_or_items if isinstance(spec_or_items, list) else (spec_or_items.get("inputs") or [])
        abs_v = comp_specs.get(cname, {}).get("absent")
        active_vals = [v for v in (comp_specs.get(cname, {}).get("values") or []) if not _same_val(v, abs_v)]
        if active_vals:
            for it in items:
                if isinstance(it, Mapping) and it.get("kind") in ("parameter", "unit_attribute"):
                    k_norm = "attribute" if it["kind"] == "unit_attribute" else "parameter"
                    input_to_comps.setdefault((k_norm, str(it["name"])), []).append((cname, list(active_vals)))

    def _pkg_key(pk_d: Mapping) -> tuple:
        return tuple((str(k), type(v).__name__, repr(v)) for k, v in sorted(pk_d.items()))

    def _dedup_pkgs(pkgs: list[dict], *, limit: int = 8) -> list[dict]:
        seen: set[tuple] = set()
        out_p: list[dict] = []
        for pk_d in pkgs:
            k = _pkg_key(pk_d)
            if k not in seen:
                seen.add(k)
                out_p.append(dict(pk_d))
                if len(out_p) >= limit:
                    break
        return out_p

    all_single_active: list[dict] = []
    for cname, cs in sorted(comp_specs.items()):
        abs_v = cs.get("absent")
        for ov in (cs.get("values") or []):
            if not _same_val(ov, abs_v):
                all_single_active.append(dict(absent_pkg, **{cname: ov}))
    all_single_active = _dedup_pkgs(all_single_active, limit=16)

    multi_active: list[dict] = []
    if len(comp_specs) >= 2:
        combo_first = dict(absent_pkg)
        combo_last = dict(absent_pkg)
        for cname, cs in sorted(comp_specs.items()):
            abs_v = cs.get("absent")
            avs = [v for v in (cs.get("values") or []) if not _same_val(v, abs_v)]
            if avs:
                combo_first[cname] = avs[0]
                combo_last[cname] = avs[-1]
        multi_active = _dedup_pkgs([combo_first, combo_last], limit=2)

    base_units = generate_domain_units(req, n_bases, rng, ref_units=ref_units)
    seeds = [31 + i for i in range(k_seeds)]
    n_seeds = len(seeds)
    chunk_sz = 2048 if cand.kind == "closed_form" else 32
    need_moving = min(min_moving, n_bases)
    reads_build_all: set[str] = set()
    reads_outcomes_all: set[str] = set()
    outputs_moved: set[str] = set()

    def _run_chunked(cases_in: list[tuple[dict, UnitSpec, Package, Horizon, int]]) -> list[Outcomes]:
        acc: list[Outcomes] = []
        for a in range(0, len(cases_in), chunk_sz):
            acc.extend(cand.run_many(cases_in[a:a + chunk_sz]))
        for o in acc:
            pr = (o.meta or {}).get("param_reads") or {}
            reads_build_all.update(pr.get("build") or [])
            reads_outcomes_all.update(pr.get("read_outcomes") or [])
        return acc

    def _eval_mode(mode_outs: list[Outcomes], n_pts: int) -> tuple[int, float, set[str]]:
        moved_bases = 0
        best_rel = 0.0
        moved_outs: set[str] = set()
        extracted = [{op: _extract_output(o, op) for op in out_paths} for o in mode_outs]
        for bi in range(len(base_units)):
            base_moved = False
            for op in out_paths:
                ys_by_pt = [
                    [extracted[(bi * n_pts + pi) * n_seeds + s][op] for s in range(n_seeds)]
                    for pi in range(n_pts)
                ]
                means = [float(np.mean(ys)) for ys in ys_by_pt]
                i_max = int(np.argmax(means))
                i_min = int(np.argmin(means))
                span = means[i_max] - means[i_min]
                scale = max(abs(means[i_min]), abs(means[i_max]))
                floor = max(abs_floor, rel_floor * scale)
                best_rel = max(best_rel, span / max(scale, 1e-12))
                if stochastic and n_seeds > 1:
                    d_ext = [ys_by_pt[i_max][s] - ys_by_pt[i_min][s] for s in range(n_seeds)]
                    m_ext = float(np.mean(d_ext))
                    se_ext = float(np.std(d_ext, ddof=1)) / math.sqrt(n_seeds)
                    moved = (m_ext - z_crit * se_ext > floor) or (m_ext + z_crit * se_ext < -floor)
                else:
                    moved = span > floor
                if moved:
                    base_moved = True
                    moved_outs.add(op)
                    outputs_moved.add(op)
            if base_moved:
                moved_bases += 1
        return moved_bases, best_rel, moved_outs

    # Pass 1: perturb every non-inert parameter, multi-value component, and unit attribute
    all_cases: list[tuple[dict, UnitSpec, Package, Horizon, int]] = []
    plan: list[tuple[str, str, list, list[dict], int, int]] = []

    def _enqueue(iname: str, ikind: str, pts: list, base_pkgs: list[dict]) -> None:
        if len(pts) < 2:
            return
        st_idx = len(all_cases)
        for base_pkg in base_pkgs:
            for bi, bu in enumerate(base_units):
                for pt in pts:
                    prm = {iname: float(pt)} if ikind == "parameter" else {}
                    pk_map = dict(base_pkg, **{iname: pt}) if ikind == "component" else dict(base_pkg)
                    u_use = UnitSpec(f"a6_{bi}", dict(bu.attributes, **{iname: pt})) if ikind == "attribute" else bu
                    for s in seeds:
                        all_cases.append((prm, u_use, Package(f"p_{bi}", pk_map), hor, s))
        plan.append((iname, ikind, pts, base_pkgs, st_idx, len(all_cases)))

    for pname in sorted(param_specs):
        if pname in inert:
            continue
        pts = _domain_points_param(param_specs[pname])
        if ("parameter", pname) in input_to_comps:
            bpkgs = _dedup_pkgs([dict(absent_pkg, **{oc: ov}) for oc, ovas in input_to_comps[("parameter", pname)] for ov in ovas], limit=6)
        else:
            bpkgs = [dict(absent_pkg)]
        _enqueue(pname, "parameter", pts, bpkgs)

    for cname in sorted(comp_specs):
        if cname in inert:
            continue
        pts = list(comp_specs[cname]["values"])
        _enqueue(cname, "component", pts, [dict(absent_pkg)])

    for aname in sorted(attr_specs):
        if aname in inert:
            continue
        ref_v = base_units[0].attributes.get(aname) if base_units else None
        pts = _domain_points_attr(attr_specs[aname], ref_vec=ref_v)
        if ("attribute", aname) in input_to_comps:
            bpkgs = _dedup_pkgs([dict(absent_pkg, **{oc: ov}) for oc, ovas in input_to_comps[("attribute", aname)] for ov in ovas], limit=6)
        else:
            bpkgs = [dict(absent_pkg)]
        _enqueue(aname, "attribute", pts, bpkgs)

    all_outs = _run_chunked(all_cases)
    input_state: dict[tuple[str, str], dict] = {}
    fallback_items: list[tuple[str, str, list, list[dict]]] = []

    for iname, ikind, pts, base_pkgs, st_idx, en_idx in plan:
        in_outs = all_outs[st_idx:en_idx]
        stride = len(base_units) * len(pts) * n_seeds
        best_moved = 0
        max_rel = 0.0
        moved_outs_all: set[str] = set()
        for mi in range(len(base_pkgs)):
            mb, mrel, mouts = _eval_mode(in_outs[mi * stride:(mi + 1) * stride], len(pts))
            best_moved = max(best_moved, mb)
            max_rel = max(max_rel, mrel)
            moved_outs_all.update(mouts)
        input_state[(ikind, iname)] = {"moved_bases": best_moved, "max_rel": max_rel, "moved_outputs": moved_outs_all}
        if ikind == "parameter" and (best_moved < need_moving or (iname not in reads_build_all and iname not in reads_outcomes_all)):
            fallback_items.append((iname, ikind, pts, base_pkgs))
        elif ikind == "attribute" and best_moved == 0 and len(outputs_moved) < len(out_paths):
            fallback_items.append((iname, ikind, pts, base_pkgs))

    # Pass 2 (lazy active-mode fallback): wake parameters/attributes gated by active intervention branches
    if fallback_items and (all_single_active or multi_active):
        fb_cases: list[tuple[dict, UnitSpec, Package, Horizon, int]] = []
        fb_plan: list[tuple[str, str, list, list[dict], int, int]] = []
        for iname, ikind, pts, tried_pkgs in fallback_items:
            tried_keys = {_pkg_key(p) for p in tried_pkgs}
            fb_pkgs = [p for p in _dedup_pkgs([dict(absent_pkg), *all_single_active, *multi_active], limit=len(tried_keys) + 8)
                       if _pkg_key(p) not in tried_keys][:8]
            if not fb_pkgs:
                continue
            st_idx = len(fb_cases)
            for base_pkg in fb_pkgs:
                for bi, bu in enumerate(base_units):
                    for pt in pts:
                        prm = {iname: float(pt)} if ikind == "parameter" else {}
                        u_use = UnitSpec(f"a6fb_{bi}", dict(bu.attributes, **{iname: pt})) if ikind == "attribute" else bu
                        for s in seeds:
                            fb_cases.append((prm, u_use, Package(f"pfb_{bi}", dict(base_pkg)), hor, s))
            fb_plan.append((iname, ikind, pts, fb_pkgs, st_idx, len(fb_cases)))
        if fb_cases:
            fb_outs = _run_chunked(fb_cases)
            for iname, ikind, pts, fb_pkgs, st_idx, en_idx in fb_plan:
                in_outs = fb_outs[st_idx:en_idx]
                stride = len(base_units) * len(pts) * n_seeds
                st_rec = input_state[(ikind, iname)]
                for mi in range(len(fb_pkgs)):
                    mb, mrel, mouts = _eval_mode(in_outs[mi * stride:(mi + 1) * stride], len(pts))
                    st_rec["moved_bases"] = max(st_rec["moved_bases"], mb)
                    st_rec["max_rel"] = max(st_rec["max_rel"], mrel)
                    st_rec["moved_outputs"].update(mouts)

    a6_probs: list[str] = []
    wiring_rows: list[dict] = []
    n_total, n_pass = 0, 0

    for pname in sorted(param_specs):
        if pname in inert:
            continue
        n_total += 1
        st_rec = input_state.get(("parameter", pname)) or {"moved_bases": 0, "max_rel": 0.0, "moved_outputs": set()}
        p_ok = True
        if pname not in reads_build_all and pname not in reads_outcomes_all:
            a6_probs.append(f"parameter {pname!r} was never read during A6 evaluations (dead parameter)")
            p_ok = False
        elif pname in reads_outcomes_all and pname not in reads_build_all and pname not in param_roles:
            a6_probs.append(f"parameter {pname!r} was read ONLY in read_outcomes() without a signed parameter_roles entry")
            p_ok = False
        if st_rec["moved_bases"] < need_moving:
            a6_probs.append(f"parameter {pname!r} moved no declared output above floor "
                            f"({st_rec['moved_bases']}/{n_bases} bases, max_rel={st_rec['max_rel']:.3g}, floor={rel_floor})")
            p_ok = False
        if p_ok:
            n_pass += 1
        wiring_rows.append({
            "input": pname, "kind": "parameter", "moved_bases": st_rec["moved_bases"], "n_bases": n_bases,
            "max_rel_change": round(float(st_rec["max_rel"]), 6),
            "moved_outputs": sorted(st_rec["moved_outputs"]), "a6_ok": p_ok,
        })

    for cname in sorted(comp_specs):
        if cname in inert or len(comp_specs[cname].get("values") or []) < 2:
            continue
        n_total += 1
        st_rec = input_state.get(("component", cname)) or {"moved_bases": 0, "max_rel": 0.0, "moved_outputs": set()}
        c_ok = st_rec["moved_bases"] >= need_moving
        if c_ok:
            n_pass += 1
        else:
            a6_probs.append(f"component {cname!r} moved no declared output above floor "
                            f"({st_rec['moved_bases']}/{n_bases} bases, max_rel={st_rec['max_rel']:.3g})")
        wiring_rows.append({
            "input": cname, "kind": "component", "moved_bases": st_rec["moved_bases"], "n_bases": n_bases,
            "max_rel_change": round(float(st_rec["max_rel"]), 6),
            "moved_outputs": sorted(st_rec["moved_outputs"]), "a6_ok": c_ok,
        })

    # Every declared output must be moved by at least one input
    for op in out_paths:
        if op not in outputs_moved:
            a6_probs.append(f"declared output {op!r} was not moved by any input in A6")

    return {"id": "A6", "status": "pass" if not a6_probs else "fail",
            "passed": n_pass, "total": n_total, "problems": a6_probs[:12], "wiring": wiring_rows}


def build_trial_bench_artifacts(cand: BADAPT.BuiltEngine, build_decl: Mapping, spec_raw: Mapping) -> tuple[dict, dict, dict, dict | None]:
    """Generate authoritative `pack.yaml`, `results.yaml`, `benchmark_coverage.yaml`, and optional
    `fitted.yaml` for A8 from `request.trials`, run through `toolkit/engines/benchmark.py`."""
    req = build_decl["request"]
    comp_specs = req.get("components") or {}
    absent_pkg = {c: cs["absent"] for c, cs in comp_specs.items()}
    hor = _default_horizon(spec_raw, req)
    seeds = [1, 2, 3, 4] if req.get("stochastic") else [1]

    settings: dict = {}
    arms: dict = {}
    benchmarks: list[dict] = []
    cov_benchmarks: dict = {}

    for tr in (req.get("trials") or []):
        tid = str(tr["id"])
        sname = f"set_{tid}"
        tset = tr.get("setting") or {}
        if "unit" in tset and isinstance(tset["unit"], Mapping):
            u_map = dict(tset["unit"])
            p_map = dict(tset.get("params") or {})
        else:
            u_map = dict(tset)
            p_map = {}
        w = tr.get("window") or [hor.start, hor.stop]
        st_entry: dict = {
            "unit": u_map,
            "horizon": {"start": float(w[0]), "stop": float(w[1]), "window": [float(w[0]), float(w[1])],
                        "windows": {str(wn): [float(w[0]), float(w[1])] for wn in hor.windows}},
        }
        if p_map:
            st_entry["params"] = p_map
        settings[sname] = st_entry

        arm_rename: dict[str, str] = {}
        tcomp = str(tr["component"])
        for aname, amap in (tr.get("arms") or {}).items():
            full_arm = f"{tid}_{aname}"
            arm_rename[str(aname)] = full_arm
            pkg = dict(absent_pkg)
            pkg.update(amap or {})
            arms[full_arm] = {"setting": sname, "package": pkg}

        def _rename_side(obj):
            if isinstance(obj, list):
                return [_rename_side(x) for x in obj]
            if isinstance(obj, dict):
                out = {k: _rename_side(v) for k, v in obj.items()}
                if "arm" in out and isinstance(out["arm"], str):
                    out["arm"] = arm_rename.get(out["arm"], out["arm"])
                return out
            return obj

        em = copy.deepcopy(tr.get("estimand_mapping") or {})
        et = str(tr.get("estimand_type", "ratio"))
        for ak in ("num", "den", "a11", "a10", "a01", "a00", "arms"):
            if ak in em:
                if isinstance(em[ak], list):
                    em[ak] = [arm_rename.get(x, x) for x in em[ak]]
                elif isinstance(em[ak], str):
                    em[ak] = arm_rename.get(em[ak], em[ak])
        for sk in ("a", "b"):
            if sk in em:
                em[sk] = _rename_side(em[sk])
        em["type"] = et

        # Credit ONLY component values present on arms actually referenced by the estimand
        used_arms = set(BM._arms_of(em))
        covered_vals: set = set()
        for full_arm in used_arms:
            arm_pkg = (arms.get(full_arm) or {}).get("package") or {}
            if tcomp in arm_pkg and arm_pkg[tcomp] != absent_pkg.get(tcomp):
                covered_vals.add(arm_pkg[tcomp])

        pub = tr.get("published") or {}
        has_band = "lower" in pub and "upper" in pub
        lo = float(pub["lower"]) if has_band else 0.0
        hi = float(pub["upper"]) if has_band else 1.0
        level = float(pub.get("level", 0.95))
        scale = "currency_per_outcome" if et == "cost_per_outcome" else "ratio"
        src_et = "odds_ratio" if et in ("odds_ratio", "interaction_or") else (
            "cost_per_outcome" if et == "cost_per_outcome" else "risk_ratio"
        )
        bm_entry = {
            "id": tid,
            "metric": f"{tcomp} trial {tid} ({tr.get('dossier')})",
            "source": str(tr.get("dossier", "dossier")),
            "estimand_type": src_et,
            "diagnostic": "not reported",
            "age_band": "mixed",
            "estimand": em,
        }
        if has_band:
            bm_entry["target"] = 0.5 * (lo + hi)
            bm_entry["band"] = [lo, hi]
            bm_entry["published"] = {"lower": lo, "upper": hi, "level": level, "scale": scale,
                                     "table": str(tr.get("dossier", "dossier"))}
        benchmarks.append(bm_entry)
        # Coverage entry (NO effects_from_spec under policy="built")
        eff_params = []
        di_entry = (req.get("decision_inputs") or {}).get(tcomp)
        di_items = di_entry if isinstance(di_entry, list) else ((di_entry or {}).get("inputs") or [])
        for it in di_items:
            if isinstance(it, Mapping) and it.get("kind") == "parameter":
                eff_params.append(str(it["name"]))
        cov_entry = {
            "covers": {tcomp: sorted(covered_vals, key=str)} if covered_vals else {},
            "estimand": "effect_vs_none" if et != "cost_per_outcome" else "cost_per_outcome",
            **({"effect_parameters": eff_params} if eff_params else {}),
            **({"covers_none_because": "no active component arm"} if not covered_vals else {}),
        }
        if et == "cost_per_outcome":
            c_drivers = sorted(set((em.get("costs") or {}).keys()) | set((em.get("costs_num") or {}).keys()))
            cov_entry["cost_drivers"] = c_drivers
        cov_benchmarks[tid] = cov_entry

    # If request has no trials, add a single exempt value benchmark so pack.yaml has non-empty benchmarks list
    if not benchmarks:
        u0 = generate_domain_units(req, 1, np.random.default_rng(0))[0]
        settings["s_id"] = {"unit": dict(u0.attributes)}
        arms["a_none"] = {"setting": "s_id", "package": dict(absent_pkg)}
        pc0 = (req.get("outputs") or {}).get("per_capita", ["cases"])[0]
        benchmarks.append({
            "id": "ID0",
            "metric": "baseline non-negative identity",
            "source": "harness",
            "band": [0.0, 1e12],
            "estimand": {"type": "value", "a": {"arm": "a_none", "value": f"per_capita_all.{pc0}"},
                         "b": {"arm": "a_none", "value": f"per_capita_all.{pc0}"}, "tol": {"abs": 1e-9}},
        })
        cov_benchmarks["ID0"] = {"covers": {}, "covers_none_because": "structural identity benchmark (no trial declared)"}

    pack_doc = {
        "engine": BADAPT.BuiltEngine.ENGINE_REF,
        "seeds": seeds,
        "initialisation": {
            "method": "built-engine admission harness",
            "evidence": ["not applicable: verified by admission checks A1-A11"],
        },
        "horizon": {"start": hor.start, "stop": hor.stop, "window": list(hor.window), "windows": dict(hor.windows)},
        "settings": settings,
        "arms": arms,
        "benchmarks": benchmarks,
    }
    covered_comps: set[str] = set()
    for ce in cov_benchmarks.values():
        covered_comps.update((ce.get("covers") or {}).keys())
    comps_without_bm: dict[str, dict] = {}
    for cname in sorted(comp_specs):
        if cname in covered_comps:
            continue
        di_entry = (req.get("decision_inputs") or {}).get(cname)
        di_items = di_entry if isinstance(di_entry, list) else ((di_entry or {}).get("inputs") or [])
        ep = [str(it["name"]) for it in di_items if isinstance(it, Mapping) and it.get("kind") == "parameter"]
        comps_without_bm[cname] = {
            "reason": "no trial benchmark declared in engine.build.request.trials",
            **({"effect_parameters": ep} if ep else {}),
        }
    cov_doc = {
        "engine": cand.describe().name,
        "absent_values": {c: [cs["absent"]] for c, cs in comp_specs.items()},
        "benchmarks": cov_benchmarks,
        **({"components_without_benchmark": comps_without_bm} if comps_without_bm else {}),
    }

    # If the built engine declares any `fitted` parameters, create a stamped fitted.yaml backing them
    fitted_params = [p for p in cand.parameters() if p.kind == "fitted"]
    fitted_doc = None
    if fitted_params:
        fitted_doc = BM.stamp({
            "parameters": {
                p.name: {
                    "value": p.value,
                    **({"lo": p.lo} if p.lo is not None else {}),
                    **({"hi": p.hi} if p.hi is not None else {}),
                }
                for p in fitted_params
            },
            "band_declarations": BS.fit_declarations(pack_doc),
        }, cand)

    # Run pack through benchmark.Runner(mode='fit' when fitted_doc not yet written on disk, then stamp)
    runner = BM.Runner(pack_doc, cand, workers=1, seeds=seeds, mode="fit")
    raw_res = BM.run_pack(pack_doc, cand, runner, {})
    res_doc = BM.stamp(raw_res, cand)
    return pack_doc, res_doc, cov_doc, fitted_doc


def check_a8(cand: BADAPT.BuiltEngine, build_decl: Mapping, spec_raw: Mapping,
             *, departures_status: str = "signed", base_dir: str | None = None) -> tuple[dict, dict, dict, dict, dict | None]:
    """A8 trial benchmarks + built coverage policy (`policy='built'`, BE15)."""
    import yaml
    pack_doc, res_doc, cov_doc, fitted_doc = build_trial_bench_artifacts(cand, build_decl, spec_raw)
    rows = res_doc.get("benchmarks") or []
    card = cand.describe()
    pnames = [p.name for p in cand.parameters()]

    with tempfile.TemporaryDirectory() as td:
        SIO.write_nofollow(td, "pack.yaml", yaml.safe_dump(pack_doc, sort_keys=False).encode("utf-8"), atomic=True)
        SIO.write_nofollow(td, "results.yaml", yaml.safe_dump(res_doc, sort_keys=False).encode("utf-8"), atomic=True)
        SIO.write_nofollow(td, "benchmark_coverage.yaml", yaml.safe_dump(cov_doc, sort_keys=False).encode("utf-8"), atomic=True)
        if fitted_doc is not None:
            SIO.write_nofollow(td, "fitted.yaml", yaml.safe_dump(fitted_doc, sort_keys=False).encode("utf-8"), atomic=True)
        prev_paths = (cand.pack_path, cand.results_path, cand.coverage_path, cand.fitted_path)
        cand.pack_path = os.path.join(td, "pack.yaml")
        cand.results_path = os.path.join(td, "results.yaml")
        cand.coverage_path = os.path.join(td, "benchmark_coverage.yaml")
        cand.fitted_path = os.path.join(td, "fitted.yaml") if fitted_doc is not None else None
        try:
            def _runs(test_id, effect, b_rows):
                from toolkit.spec import preconditions as PC, schema
                sp = schema.Spec({k: v for k, v in spec_raw.items() if k != "_packages"}, base_dir or os.getcwd())
                sp.engine_pending = False
                sp._engine = cand
                sp._engine_ref = None
                return PC.decision_test_runs(sp, test_id, dict(effect, rows=b_rows))

            g = BC.gate(
                cov_doc,
                BC.problems(cov_doc),
                card.name,
                dict(spec_raw),
                lambda: (rows, None),
                list(spec_raw.get("engine_departures") or []),
                departures_status,
                pack=pack_doc,
                has_benchmarks=True,
                runs=_runs,
                version=card.version,
                param_names=pnames,
                policy="built",
            )
        finally:
            cand.pack_path, cand.results_path, cand.coverage_path, cand.fitted_path = prev_paths
    probs = list(g.get("why_not") or [])
    n_total = len(rows)
    n_pass = sum(1 for r in rows if r.get("status") == "in_band")
    routing = "spec_amendment" if (probs and (g.get("blocking") or any("no benchmark" in p for p in probs))) else None
    chk = {
        "id": "A8",
        "status": "pass" if not probs else "fail",
        "passed": n_pass,
        "total": n_total,
        "problems": probs[:12],
        "rows": [{"id": r.get("id"), "computed": r.get("computed"), "band": r.get("band"), "status": r.get("status")}
                 for r in rows],
        "routing": routing,
    }
    return chk, pack_doc, res_doc, cov_doc, fitted_doc


def check_a11(cand: BADAPT.BuiltEngine, ref_cand: BADAPT.BuiltEngine | None, build_decl: Mapping,
              cases: list[tuple[dict, UnitSpec, Package, Horizon]], *, th: Mapping, required: bool = False) -> dict:
    """A11 optional deterministic reference equivalence test:
    Compares every individual full path (`per_capita.<o>.<g>`, `windows.<w>.<m>`, `cost.<d>`) within
    `min(request.reference_tolerance, thresholds.reference_rel_ceiling)`."""
    if ref_cand is None:
        if not required:
            return {"id": "A11", "status": "skipped", "passed": 0, "total": 0, "problems": []}
        return {"id": "A11", "status": "unavailable", "passed": 0, "total": 0,
                "problems": ["independent reference implementation unavailable (A11 required)"]}
    req = build_decl["request"]
    rtol_decl = req.get("reference_tolerance") or {}
    rel_lim = min(float(rtol_decl.get("rel", th["reference_rel_ceiling"])), float(th["reference_rel_ceiling"]))
    abs_lim = float(rtol_decl.get("abs", th["reference_abs_default"]))

    req_outs = req.get("outputs") or {}
    age_groups = list(req.get("age_groups") or [])
    out_names: list[str] = []
    for pc in (req_outs.get("per_capita") or []):
        for g in age_groups:
            out_names.append(f"per_capita.{pc}.{g}")
    for wname, metrics in sorted((req_outs.get("windows") or {}).items()):
        for m in (metrics or []):
            out_names.append(f"windows.{wname}.{m}")
    for drv in (req_outs.get("cost_drivers") or []):
        out_names.append(f"cost.{drv}")
    probs: list[str] = []
    n_total, n_pass = 0, 0
    rows: list[dict] = []

    det_cases = [(prm, u, pk, hor, 71 + ci) for ci, (prm, u, pk, hor) in enumerate(cases)]
    c_det = cand.run_many(det_cases) if det_cases else []
    r_det = ref_cand.run_many(det_cases) if det_cases else []
    for ci, (oc, or_) in enumerate(zip(c_det, r_det)):
        for oname in out_names:
            n_total += 1
            vc, vr = _extract_output(oc, oname), _extract_output(or_, oname)
            diff = abs(vc - vr)
            tol = max(abs_lim, rel_lim * max(abs(vr), 1e-12))
            ok_ = math.isfinite(diff) and diff <= tol
            rows.append({"case": ci, "output": oname, "cand": vc, "ref": vr, "tol": tol, "ok": ok_})
            if ok_:
                n_pass += 1
            else:
                probs.append(f"case {ci} {oname}: candidate={vc:.5g} vs ref={vr:.5g} (|diff|={diff:.4g} > tol={tol:.4g})")

    status = "fail" if probs else "pass"
    comp_sha = _sha256_json(rows)
    return {"id": "A11", "status": status, "passed": n_pass, "total": n_total,
            "problems": probs[:12], "rows": rows[:12], "comparison_sha256": comp_sha}


# --------------------------------------------------------------------------- Public checks (try tool)
def run_public_checks(cand: BADAPT.BuiltEngine, build_decl: Mapping, spec_raw: Mapping,
                      *, checks: list[str] | None = None, base_dir: str | None = None,
                      departures_status: str = "signed") -> dict:
    """Run ONLY public checks using `PUBLIC_SEED_BYTES` (used by `try_tool`)."""
    th = load_thresholds()
    req = build_decl["request"]
    want = set(checks) if checks is not None else {"A1", "A2", "A4", "A5", "A6", "A8"}
    rng = _rng_from(PUBLIC_SEED_BYTES, "public")
    hor = _default_horizon(spec_raw, req)
    comp_specs = req.get("components") or {}
    absent_pkg = {c: cs["absent"] for c, cs in comp_specs.items()}
    pub_pkgs = [Package("pub_none", dict(absent_pkg))]
    for c, cs in sorted(comp_specs.items()):
        for v in cs["values"]:
            if not _same_val(v, cs["absent"]):
                pub_pkgs.append(Package(f"pub_{c}_{v}", dict(absent_pkg, **{c: v})))
    pub_units = generate_domain_units(req, 2, rng)
    out_checks: dict[str, dict] = {}
    if "A1" in want:
        out_checks["A1"] = check_a1(cand, build_decl, spec_raw, pub_units, pub_pkgs, hor)
    if "A2" in want:
        out_checks["A2"] = check_a2(cand, build_decl, pub_units, pub_pkgs, hor, th=th, check_invariance=True)
    if "A4" in want:
        out_checks["A4"] = check_a4(cand, build_decl, spec_raw, _rng_from(PUBLIC_SEED_BYTES, "a4"), n_free=1, th=th)
    if "A5" in want:
        out_checks["A5"] = check_a5(cand, build_decl, pub_units, pub_pkgs, hor, th=th)
    if "A6" in want:
        out_checks["A6"] = check_a6(cand, build_decl, spec_raw, _rng_from(PUBLIC_SEED_BYTES, "a6"),
                                    n_bases=int(th["a6_base_points"]), min_moving=int(th["a6_min_moving_bases"]), th=th)
    if "A8" in want:
        a8, *_ = check_a8(cand, build_decl, spec_raw, departures_status=departures_status, base_dir=base_dir)
        out_checks["A8"] = a8
    ok_all = all(c.get("status") in ("pass", "skipped", "not_applicable") for c in out_checks.values())
    return {"ok": ok_all, "checks": out_checks}


def _is_sha64(x) -> bool:
    return isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x)


def _publish_verified_snapshot(target_snaps: str, snap_sha: str, verified_files: Mapping[str, bytes]) -> None:
    """Idempotently publish a verified immutable content-addressed snapshot (`0444` files, `0555` dir,
    and `<sha>.json` manifest) under `target_snaps`, verifying with `BSNAP.read_verified_snapshot`."""
    os.makedirs(target_snaps, exist_ok=True)
    already_ok = False
    try:
        existing = BSNAP.read_verified_snapshot(target_snaps, snap_sha)
        already_ok = existing == dict(verified_files)
    except BSNAP.SnapshotError:
        already_ok = False
    if already_ok:
        return
    snap_dir = os.path.join(target_snaps, snap_sha)
    if os.path.isdir(snap_dir) and not os.path.islink(snap_dir):
        dfd0 = SIO.dir_fd_nofollow(target_snaps, [snap_sha])
        try:
            os.fchmod(dfd0, 0o755)
        finally:
            os.close(dfd0)
        for fn in sorted(os.listdir(snap_dir)):
            fp = os.path.join(snap_dir, fn)
            if os.path.isfile(fp) and not os.path.islink(fp):
                ffd = SIO.open_nofollow(target_snaps, f"{snap_sha}/{fn}")
                try:
                    os.fchmod(ffd, 0o644)
                finally:
                    os.close(ffd)
            SIO.remove_nofollow(target_snaps, f"{snap_sha}/{fn}", missing_ok=True)
    for name in sorted(verified_files):
        raw = bytes(verified_files[name])
        SIO.write_nofollow(target_snaps, f"{snap_sha}/{name}", raw, atomic=True)
        fd = SIO.open_nofollow(target_snaps, f"{snap_sha}/{name}")
        try:
            os.fchmod(fd, 0o444)
        finally:
            os.close(fd)
    dfd = SIO.dir_fd_nofollow(target_snaps, [snap_sha])
    try:
        os.fchmod(dfd, 0o555)
    finally:
        os.close(dfd)
    man = {
        "snapshot_sha256": snap_sha,
        "files": [{"name": n, "sha256": _sha256_bytes(bytes(verified_files[n])), "size": len(verified_files[n])}
                  for n in sorted(verified_files)],
    }
    SIO.write_nofollow(target_snaps, f"{snap_sha}.json", json.dumps(man, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
    BSNAP.read_verified_snapshot(target_snaps, snap_sha)


# --------------------------------------------------------------------------- Admission sheet & orchestrator
def render_admission_sheet(adm: Mapping) -> str:
    """Render `engine_build/admission_sheet.md` (§5.3)."""
    lines = [
        f"# Agent-built engine admission sheet: `{adm.get('engine_id')}`",
        "",
        f"- **Kind:** `{adm.get('kind')}`",
        f"- **Status:** `{adm.get('status')}`",
        f"- **Snapshot sha256:** `{adm.get('snapshot_sha256')}`",
        f"- **Request sha256:** `{adm.get('request_sha256')}`",
        f"- **Hidden seed commitment:** `{adm.get('hidden_commitment')}`",
        "",
        "> **Scope of automated checks:** These checks verify mathematical, structural, wiring, and trial-consistency "
        "properties against the signed request. They do not prove biological realism or catch errors shared by the "
        "request itself; owner review of the request and this sheet is the safeguard.",
        "",
        "## Checks",
        "| Check | Status | Passed / Total | Notes |",
        "|---|---|---|---|",
    ]
    for cid, c in (adm.get("checks") or {}).items():
        note = "; ".join(str(p) for p in (c.get("problems") or [])[:2]) or "ok"
        lines.append(f"| {cid} | {c.get('status')} | {c.get('passed', 0)}/{c.get('total', 0)} | {note} |")
    a6 = (adm.get("checks") or {}).get("A6") or {}
    if a6.get("wiring"):
        lines += ["", "## Sensitivity wiring (A6)", "| Input | Kind | Moved | Max rel change | Moved outputs | OK |", "|---|---|---|---|---|---|"]
        for r in a6["wiring"]:
            mouts = ", ".join(r.get("moved_outputs") or []) or "none"
            lines.append(f"| {r['input']} | {r.get('kind', '')} | {r['moved_bases']}/{r['n_bases']} | "
                         f"{r['max_rel_change']} | {mouts} | {r['a6_ok']} |")
    return "\n".join(lines) + "\n"


def _load_or_commit_hidden_seed(run_dir: str) -> tuple[bytes, str]:
    """Load an already-committed hidden seed on resume (BE31), or draw & commit a fresh `secrets.token_bytes(32)`."""
    try:
        raw, st = SIO.read_nofollow(run_dir, HIDDEN_SEED_REL)
        if st.st_nlink == 1:
            doc = json.loads(raw.decode("utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("seed_hex"), str) and len(doc["seed_hex"]) == 64:
                sb = bytes.fromhex(doc["seed_hex"])
                return sb, _sha256_bytes(doc["seed_hex"].encode("ascii"))
    except (FileNotFoundError, SIO.LinkRefused, OSError, UnicodeDecodeError, ValueError):
        pass
    sb = secrets.token_bytes(32)
    shex = sb.hex()
    commit = _sha256_bytes(shex.encode("ascii"))
    SIO.write_nofollow(
        run_dir,
        HIDDEN_SEED_REL,
        json.dumps({"seed_hex": shex, "commitment": commit,
                    "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}, indent=1, sort_keys=True).encode("utf-8"),
        atomic=True,
    )
    return sb, commit


def admit(
    run_dir: str,
    snapshot_sha256: str,
    build_decl: Mapping,
    spec_raw: Mapping,
    *,
    config: Mapping | None = None,
    reference_files: Mapping[str, bytes] | None = None,
    require_reference: bool = False,
    dossier_bytes: bytes | None = None,
    departures_status: str = "signed",
    unsandboxed_ok: bool = False,
    _force_sandbox: str | None = "auto",
) -> dict:
    """Run the admission gate (S1-S3, A1-A2, A4-A6, A8, optional deterministic A11) on an immutable
    snapshot in `run_dir`, writing `engine_build/bench/*`, `engine_build/admission_sheet.md`,
    and atomically `engine_build/admission.json` last."""
    import yaml
    th = load_thresholds()
    snaps_dir = os.path.join(run_dir, "engine_build", "snapshots")
    verified_files = BSNAP.read_verified_snapshot(snaps_dir, snapshot_sha256)
    seed_bytes, commitment = _load_or_commit_hidden_seed(run_dir)

    # S1-S3 static checks BEFORE any run
    static_probs = BSNAP.check_static(verified_files, request=build_decl)
    if static_probs:
        SIO.remove_nofollow(run_dir, "engine_build/bench/fitted.yaml", missing_ok=True)
        adm = {
            "status": "not_admitted",
            "engine_id": build_decl["id"],
            "kind": build_decl["kind"],
            "snapshot_sha256": snapshot_sha256,
            "request_sha256": BREQ.request_sha256(build_decl),
            "config_sha256": BADAPT.effective_config_sha256(config or {}),
            "hidden_commitment": commitment,
            "static_problems": static_probs,
            "checks": {},
            "digests": {},
            "feedback": {"static_problems": static_probs},
        }
        SIO.write_nofollow(run_dir, "engine_build/admission_sheet.md", render_admission_sheet(adm).encode("utf-8"), atomic=True)
        SIO.write_nofollow(run_dir, "engine_build/admission.json",
                           json.dumps(adm, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
        return adm

    cand = BADAPT.BuiltEngine.from_verified(
        verified_files, build_decl, config=config, run_dir=run_dir,
        unsandboxed_ok=unsandboxed_ok, id_salt=commitment[:16],
    )
    cand._force_sandbox = _force_sandbox

    ref_cand = None
    if reference_files is not None:
        ref_build = copy.deepcopy(dict(build_decl))
        ref_build["kind"] = "closed_form"
        ref_cand = BADAPT.BuiltEngine.from_verified(
            reference_files, ref_build, config=config, run_dir=run_dir,
            unsandboxed_ok=unsandboxed_ok, id_salt=commitment[:16],
        )
        ref_cand._force_sandbox = _force_sandbox

    def _safe_check(cid: str, fn) -> dict:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- candidate exceptions must publish a failed admission record
            return {
                "id": cid,
                "status": "fail",
                "passed": 0,
                "total": 1,
                "problems": [f"runtime error: {type(exc).__name__}: {exc}"],
            }

    req = build_decl["request"]
    hor = _default_horizon(spec_raw, req)
    rng_h = _rng_from(seed_bytes, "hidden")
    try:
        real_units, real_pkgs = spec_units_and_packages(
            spec_raw, req, base_dir=run_dir,
            max_units=int(th["hidden_max_units"]), max_packages=int(th["hidden_max_packages"]), rng=rng_h,
        )
        real_units_err = None
    except Exception as exc:  # noqa: BLE001 -- bad real-unit table must publish a failed admission record
        real_units, real_pkgs = [], [Package("none", {c: cs["absent"] for c, cs in (req.get("components") or {}).items()})]
        real_units_err = f"real-unit table error: {type(exc).__name__}: {exc}"
    gen_units_a1 = generate_domain_units(req, 3, _rng_from(seed_bytes, "a1"), ref_units=real_units)
    a1_units = gen_units_a1 + real_units
    a2_units = generate_domain_units(req, int(th["a2_hidden_cases"]), _rng_from(seed_bytes, "a2"), ref_units=real_units)
    a5_units = generate_domain_units(req, int(th["a5_hidden_cases"]), _rng_from(seed_bytes, "a5"), ref_units=real_units) + real_units

    checks: dict[str, dict] = {}
    if real_units_err is not None:
        checks["A1"] = {"id": "A1", "status": "fail", "passed": 0, "total": 1, "problems": [real_units_err]}
    else:
        checks["A1"] = _safe_check("A1", lambda: check_a1(cand, build_decl, spec_raw, a1_units, real_pkgs, hor))
    checks["A2"] = _safe_check("A2", lambda: check_a2(cand, build_decl, a2_units, real_pkgs, hor, th=th, check_invariance=True))
    checks["A4"] = _safe_check("A4", lambda: check_a4(cand, build_decl, spec_raw, _rng_from(seed_bytes, "a4"),
                                                      n_free=int(th["a4_hidden_settings"]), th=th, ref_units=real_units))
    checks["A5"] = _safe_check("A5", lambda: check_a5(cand, build_decl, a5_units, real_pkgs, hor, th=th))
    checks["A6"] = _safe_check("A6", lambda: check_a6(cand, build_decl, spec_raw, _rng_from(seed_bytes, "a6"),
                                                      n_bases=int(th["a6_base_points"]), min_moving=int(th["a6_min_moving_bases"]),
                                                      th=th, ref_units=real_units))

    pack_doc = res_doc = cov_doc = fitted_doc = None
    try:
        a8, pack_doc, res_doc, cov_doc, fitted_doc = check_a8(
            cand, build_decl, spec_raw, departures_status=departures_status, base_dir=run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        a8 = {"id": "A8", "status": "fail", "passed": 0, "total": 1,
              "problems": [f"runtime error: {type(exc).__name__}: {exc}"], "rows": [], "routing": None}
    checks["A8"] = a8
    a11_cases = [(dict(), u, real_pkgs[i % len(real_pkgs)], hor) for i, u in enumerate((real_units + gen_units_a1)[:4])]
    checks["A11"] = _safe_check("A11", lambda: check_a11(cand, ref_cand, build_decl, a11_cases, th=th, required=require_reference))

    gating_ids = ("A1", "A2", "A4", "A5", "A6", "A8", "A11")
    failed_gating = [cid for cid in gating_ids if checks[cid]["status"] not in ("pass", "skipped", "not_applicable")]
    checks = {
        cid: checks[cid]
        for cid in ("A1", "A2", "A4", "A5", "A6", "A8", "A11")
        if cid in checks
    }

    # Write authoritative engine_build/bench/* atomically via safe_io and remove any stale fitted.yaml
    digests: dict[str, str] = {}
    if pack_doc is not None and res_doc is not None and cov_doc is not None:
        bench_blobs = {
            "bench/pack.yaml": yaml.safe_dump(pack_doc, sort_keys=False).encode("utf-8"),
            "bench/results.yaml": yaml.safe_dump(res_doc, sort_keys=False).encode("utf-8"),
            "bench/benchmark_coverage.yaml": yaml.safe_dump(cov_doc, sort_keys=False).encode("utf-8"),
        }
        if fitted_doc is not None:
            bench_blobs["bench/fitted.yaml"] = yaml.safe_dump(fitted_doc, sort_keys=False).encode("utf-8")
        else:
            SIO.remove_nofollow(run_dir, "engine_build/bench/fitted.yaml", missing_ok=True)
        for rel, blob in bench_blobs.items():
            SIO.write_nofollow(run_dir, f"engine_build/{rel}", blob, atomic=True)
            digests[rel] = _sha256_bytes(blob)
    else:
        SIO.remove_nofollow(run_dir, "engine_build/bench/fitted.yaml", missing_ok=True)
    if reference_files is not None:
        ref_map = {str(k): bytes(v) for k, v in sorted(reference_files.items())}
        ref_hashes = {k: _sha256_bytes(v) for k, v in ref_map.items()}
        ref_sha = BSNAP.snapshot_digest(ref_hashes)
        _publish_verified_snapshot(
            os.path.join(run_dir, "engine_build", "reference", "snapshots"),
            ref_sha,
            ref_map,
        )
        digests["reference_snapshot"] = ref_sha
        comp_sha = checks["A11"].get("comparison_sha256")
        digests["reference_comparison"] = (
            comp_sha if isinstance(comp_sha, str) else _sha256_json(checks["A11"].get("rows") or [])
        )

    admitted = not failed_gating
    # Feedback for a hidden failure names ONLY check ids (never hidden inputs/values, §4.6)
    feedback = {
        "failed_checks": failed_gating,
        "routing": checks["A8"].get("routing"),
    }
    adm = {
        "status": "admitted" if admitted else "not_admitted",
        "engine_id": build_decl["id"],
        "kind": build_decl["kind"],
        "snapshot_sha256": snapshot_sha256,
        "request_sha256": BREQ.request_sha256(build_decl),
        "config_sha256": BADAPT.effective_config_sha256(config or {}),
        "hidden_commitment": commitment,
        "static_problems": [],
        "checks": checks,
        "digests": digests,
        "findings": [],
        "feedback": feedback,
        "admitted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    SIO.write_nofollow(run_dir, "engine_build/admission_sheet.md", render_admission_sheet(adm).encode("utf-8"), atomic=True)
    SIO.write_nofollow(run_dir, "engine_build/admission.json",
                       json.dumps(adm, indent=1, sort_keys=True, default=str).encode("utf-8"), atomic=True)
    return adm


# --------------------------------------------------------------------------- Self-test (BE1-BE11, BE13, BE15, BE17, BE32, BE33)
def _self_test() -> int:
    import tempfile
    from agents import run_budget as RB
    from toolkit.engines.built import fixtures as BFIX

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    dev_un = BWORK.engine_sandbox_kind() is None

    def _run_fixture(defect: str | None = None, *, stochastic: bool = False, stochastic_sd: float = 0.0,
                     ref_bias: float = 0.0, include_trial_oob: bool = False,
                     require_reference: bool = True, with_ref: bool = True):
        td = tempfile.TemporaryDirectory()
        d = td.name
        b = BFIX.two_group_sir_build(stochastic=stochastic, include_trial_oob=include_trial_oob)
        sp = BFIX.two_group_sir_spec(b)
        wdir = os.path.join(d, "models", "ext", f"engine_{b['id']}")
        os.makedirs(wdir, exist_ok=True)
        files = BFIX.two_group_sir_files(defect=defect, stochastic_sd=stochastic_sd)
        for fn, data in files.items():
            open(os.path.join(wdir, fn), "wb").write(data)
        snaps = os.path.join(d, "engine_build", "snapshots")
        man = BSNAP.create_snapshot(wdir, snaps, run_static=False)
        ref_files = BFIX.two_group_sir_reference_files(rel_bias=ref_bias) if with_ref else None
        adm = admit(
            d, man["snapshot_sha256"], b, sp,
            reference_files=ref_files, require_reference=require_reference,
            unsandboxed_ok=dev_un,
        )
        return td, d, b, sp, man["snapshot_sha256"], adm

    # BE1: healthy two-group SIR Starsim fixture passes every check -> status: admitted
    td1, d1, b1, sp1, sha1, adm1 = _run_fixture(None, require_reference=True, with_ref=True)
    try:
        bad1 = {k: v.get("problems") for k, v in adm1["checks"].items() if v.get("status") not in ("pass", "skipped", "not_applicable")}
        ok(adm1["status"] == "admitted" and not bad1 and os.path.isfile(os.path.join(d1, "engine_build", "admission_sheet.md")),
           f"BE1 healthy two-group SIR Starsim fixture passes every check and is admitted (bad={bad1})")
    finally:
        td1.cleanup()

    # BE2: dead parameter fails A6 and names it
    td, _, _, _, _, adm = _run_fixture("dead_param", require_reference=False, with_ref=False)
    try:
        a6 = adm["checks"]["A6"]
        ok(adm["status"] == "not_admitted" and a6["status"] == "fail" and any("p_sev" in p for p in a6["problems"]),
           f"BE2 dead parameter fails A6 and names p_sev ({a6['problems'][:2]})")
    finally:
        td.cleanup()

    # BE3: 1e-12 * eff_a fails A6
    td, _, _, _, _, adm = _run_fixture("tiny_eps", require_reference=False, with_ref=False)
    try:
        a6 = adm["checks"]["A6"]
        ok(adm["status"] == "not_admitted" and a6["status"] == "fail" and any("eff_a" in p for p in a6["problems"]),
           f"BE3 1e-12 * p added to output fails A6 ({a6['problems'][:2]})")
    finally:
        td.cleanup()

    # BE4: parameter wired to wrong output fails trial benchmark A8; constant/dead declared output fails A6
    td, _, _, _, _, adm = _run_fixture("wrong_output", require_reference=False, with_ref=False)
    try:
        ok(adm["status"] == "not_admitted" and adm["checks"]["A8"]["status"] == "fail",
           f"BE4 parameter wired to wrong output fails A8 ({adm['checks']['A8']['problems'][:1]})")
    finally:
        td.cleanup()

    # BE5: parameter read only in read_outcomes without signed role fails A6
    td, _, _, _, _, adm = _run_fixture("outcome_only_param", require_reference=False, with_ref=False)
    try:
        a6 = adm["checks"]["A6"]
        ok(a6["status"] == "fail" and any("read_outcomes" in p and "p_sev" in p for p in a6["problems"]),
           f"BE5 parameter read only in read_outcomes without signed role fails A6 ({a6['problems'][:2]})")
    finally:
        td.cleanup()

    # BE6: unseeded numpy.random & order-dependent cache fail A2
    td_a, _, _, _, _, adm_a = _run_fixture("unseeded", require_reference=False, with_ref=False)
    td_b, _, _, _, _, adm_b = _run_fixture("order_cache", require_reference=False, with_ref=False)
    try:
        ok(adm_a["checks"]["A2"]["status"] == "fail" and adm_b["checks"]["A2"]["status"] == "fail",
           "BE6 unseeded numpy.random and order-dependent cache both fail A2")
    finally:
        td_a.cleanup()
        td_b.cleanup()

    # BE8: 0.1%/step population growth under conservation=closed fails A5
    td, _, _, _, _, adm = _run_fixture("mass_growth", require_reference=False, with_ref=False)
    try:
        ok(adm["checks"]["A5"]["status"] == "fail" and any("conservation" in p for p in adm["checks"]["A5"]["problems"]),
           f"BE8 population growing 0.1%/step under closed conservation fails A5 ({adm['checks']['A5']['problems'][:1]})")
    finally:
        td.cleanup()

    # BE9: severity computed from formula instead of compartment H transition fails A5
    td, _, _, _, _, adm = _run_fixture("formula_severe", require_reference=False, with_ref=False)
    try:
        ok(adm["checks"]["A5"]["status"] == "fail" and any("witness" in p for p in adm["checks"]["A5"]["problems"]),
           f"BE9 severity computed apart from compartment transition fails A5 ({adm['checks']['A5']['problems'][:1]})")
    finally:
        td.cleanup()

    # BE10: public lookup table passes public A4 and fails hidden A4
    td, _, b10, sp10, sha10, adm10 = _run_fixture("public_table", require_reference=False, with_ref=False)
    try:
        cand10 = BADAPT.BuiltEngine.from_verified(BFIX.two_group_sir_files("public_table"), b10, unsandboxed_ok=dev_un)
        pub_a4 = check_a4(cand10, b10, sp10, _rng_from(PUBLIC_SEED_BYTES, "a4"), n_free=1, th=load_thresholds())
        hid_a4 = check_a4(cand10, b10, sp10, _rng_from(b"hidden-be10", "a4"), n_free=3, th=load_thresholds())
        ok(adm10["status"] == "not_admitted" and pub_a4["status"] == "pass" and hid_a4["status"] == "fail",
           f"BE10 public lookup table passes public A4 and fails hidden A4 (pub={pub_a4['status']}, hid={hid_a4['status']})")
    finally:
        td.cleanup()

    # BE11: real-unit defect passes synthetic cases and fails real-unit hidden set
    td, _, _, _, _, adm = _run_fixture("real_unit_bad", require_reference=False, with_ref=False)
    try:
        ok(adm["checks"]["A1"]["status"] == "fail" and any("unit_beta" in p for p in adm["checks"]["A1"]["problems"]),
           f"BE11 real-unit misbehavior caught on signed real-unit hidden set ({adm['checks']['A1']['problems'][:1]})")
    finally:
        td.cleanup()

    # BE13: narrowed parameter range, empty window metric, negative cost driver all fail A1
    be13 = {}
    for df in ("narrow_range", "empty_window", "neg_driver"):
        td, _, _, _, _, adm = _run_fixture(df, require_reference=False, with_ref=False)
        try:
            be13[df] = adm["checks"]["A1"]["status"] == "fail"
        finally:
            td.cleanup()
    ok(all(be13.values()), f"BE13 narrowed param range, empty window metric, and negative cost driver each fail A1 ({be13})")

    # BE15 (via admission): out-of-band trial fails A8 under built policy and sets routing='spec_amendment'
    td, _, _, _, _, adm = _run_fixture(None, include_trial_oob=True, require_reference=False, with_ref=False)
    try:
        a8 = adm["checks"]["A8"]
        ok(adm["status"] == "not_admitted" and a8["status"] == "fail" and a8.get("routing") == "spec_amendment",
           f"BE15b out-of-band trial fails A8 under built policy with spec_amendment routing ({a8['problems'][:1]})")
    finally:
        td.cleanup()

    # BE17: optional deterministic A11 fails on 6% biased ref and skips cleanly without ref when not required
    td_det, _, _, _, _, adm_det = _run_fixture(None, ref_bias=0.06, require_reference=True, with_ref=True)
    td_sk, _, _, _, _, adm_sk = _run_fixture(None, require_reference=False, with_ref=False)
    try:
        ok(adm_det["checks"]["A11"]["status"] == "fail" and adm_sk["status"] == "admitted"
           and adm_sk["checks"]["A11"]["status"] == "skipped",
           f"BE17 biased ref fails A11 ({adm_det['checks']['A11']['status']}); omitted ref skips cleanly ({adm_sk['checks']['A11']['status']})")
    finally:
        td_det.cleanup()
        td_sk.cleanup()

    # BE33: admission compute reservation against run balance pauses before dispatch when balance < cap
    # and rejects non-finite (NaN/inf) amounts
    with tempfile.TemporaryDirectory() as d_bud:
        ok_res, res_id = RB.reserve_compute(d_bud, 15.0, cap_usd=20.0)
        ok_over, why_over = RB.reserve_compute(d_bud, 15.0, cap_usd=20.0)
        nan_rejected = False
        try:
            RB.reserve_compute(d_bud, float("nan"), cap_usd=20.0)
        except ValueError:
            nan_rejected = True
        RB.reconcile_compute(d_bud, res_id, 3.25, cap_usd=20.0)
        rem = RB.remaining_budget(20.0, RB.load_budget_ledger(d_bud)["sessions"])
        ok(ok_res and not ok_over and nan_rejected and "insufficient budget" in str(why_over) and abs(rem - 16.75) < 1e-6,
           f"BE33 compute reservation blocks when remaining < cap, rejects NaN, and reconciles (rem={rem})")

    # Regression checks: _outcomes_close key parity, _default_horizon warm-up, 30-bool package sampling
    # without explosion, mutually exclusive option groups, and missing required reference -> A11 unavailable
    empty_o = Outcomes({}, {}, {})
    non_empty_o = Outcomes({"cases": {"g0": 0.2}}, {"w0": {"prev": 0.1}}, {"d": 1.0})
    close_empty, _ = _outcomes_close(empty_o, non_empty_o)
    h_warm = _default_horizon({"horizon": {"start": 2010, "stop": 2035}, "objective": {"horizon": [2025, 2030]}},
                              {"outputs": {"windows": {"w0": ["prev"]}}})
    req_30 = {"components": {f"c{i:02d}": {"values": [0, 1], "absent": 0} for i in range(30)}, "unit_attributes": {}}
    _, pkgs_30 = spec_units_and_packages({}, req_30, max_packages=16, rng=np.random.default_rng(1))
    req_grp = {"components": {"a": {"values": [0, 1], "absent": 0}, "b": {"values": [0, 1], "absent": 0}}, "unit_attributes": {}}
    sp_grp = {
        "options": {
            "components": {"opt_a": {"group": "g1"}, "opt_b": {"group": "g1"}},
            "engine_map": {"opt_a": {"a": 1}, "opt_b": {"b": 1}},
            "packages": {"generate": True},
        }
    }
    _, pkgs_grp = spec_units_and_packages(sp_grp, req_grp, max_packages=16, rng=np.random.default_rng(1))
    both_active = any(pk.components.get("a") == 1 and pk.components.get("b") == 1 for pk in pkgs_grp)
    td_nr, _, _, _, _, adm_nr = _run_fixture(None, require_reference=True, with_ref=False)
    try:
        nr_ok = (adm_nr["status"] == "not_admitted"
                 and adm_nr["checks"]["A11"]["status"] == "unavailable")
    finally:
        td_nr.cleanup()
    ok(not close_empty and h_warm.start == 2010.0 and h_warm.stop == 2035.0 and h_warm.window == (2025.0, 2030.0)
       and len(pkgs_30) == 16 and not both_active and nr_ok,
       "BE_REG1 outcomes key parity, warm-up horizon, 30-bool/group packages, and unavailable A11")

    # BE_CAL_ATTR1: spec_units_and_packages supplies interior midpoint for calibration.attribute (log & linear)
    # without requiring a column binding, while still refusing unbound non-calibrated required attributes
    req_cal = {
        "components": {"c": {"values": [0, 1], "absent": 0}},
        "unit_attributes": {
            "rate": {"type": "continuous", "range": [0.001, 0.5]},
            "base": {"type": "continuous", "range": [0.0, 1.0]},
        },
        "attribute_binding": {"rate": "required", "base": "required"},
    }
    sp_cal_log = {
        "decision_units": {"id": "id", "units_inline": [{"id": "u1", "base": 0.1}],
                           "engine_attributes": {"base": {"column": "base"}}},
        "calibration": {"method": "posterior", "attribute": "rate", "scale": "log", "range": [-6.0, -2.0]},
    }
    u_log, _ = spec_units_and_packages(sp_cal_log, req_cal, rng=np.random.default_rng(1))
    sp_cal_lin = copy.deepcopy(sp_cal_log)
    sp_cal_lin["calibration"] = {"method": "posterior", "attribute": "rate", "scale": "linear", "range": [0.1, 0.3]}
    u_lin, _ = spec_units_and_packages(sp_cal_lin, req_cal, rng=np.random.default_rng(1))
    sp_unb = {"decision_units": sp_cal_log["decision_units"]}
    unb_refused = False
    try:
        spec_units_and_packages(sp_unb, req_cal, rng=np.random.default_rng(1))
    except ValueError:
        unb_refused = True
    ok(len(u_log) == 1 and abs(u_log[0].attributes["rate"] - math.exp(-4.0)) < 1e-12
       and len(u_lin) == 1 and abs(u_lin[0].attributes["rate"] - 0.2) < 1e-12 and unb_refused,
       f"BE_CAL_ATTR1 calibrated per-unit attribute gets prior midpoint in A1 real units while unbound non-calibrated fails")

    # BE_A6_COST_LOG: multi-decade positive continuous attrs sample in log space; active-mode-gated cost-driver
    # parameters pass A6 via Pass 2 fallback while dead parameters and constant declared outputs fail A6.
    log_mid = _sample_attr({"type": "continuous", "range": [0.0005, 0.5]}, np.random.default_rng(1), u_val=0.5)
    lin_mid = _sample_attr({"type": "continuous", "range": [0.1, 1.0]}, np.random.default_rng(1), u_val=0.5)
    log_pts = _domain_points_attr({"type": "continuous", "range": [0.0005, 0.5]})
    b_cd = {
        "id": "stub_cd", "kind": "closed_form",
        "request": {
            "components": {"act": {"values": [0, 1], "absent": 0}},
            "unit_attributes": {"scale": {"type": "continuous", "test_range": [10.0, 10000.0]}},
            "outputs": {"per_capita": ["y"], "windows": {}, "cost_drivers": ["vol"]},
            "age_groups": ["g0"],
            "parameters": [{"name": "cost_mult", "kind": "fixed", "value": 0.5, "lo": 0.1, "hi": 1.0, "source": "t"}],
            "stochastic": False,
        },
    }

    class _CdStub:
        kind = "closed_form"

        def __init__(self, mode: str):
            self._mode = mode

        def run_many(self, cases):
            out = []
            for prm, u, pk, _h, _s in cases:
                on = int(pk.components.get("act", 0))
                sc = float(u.attributes["scale"])
                reads = []
                if self._mode == "dead_out":
                    reads.append("cost_mult")
                    cm = float(prm.get("cost_mult", 0.5))
                    y = (1.0 - 0.5 * on) * cm
                    vol = 0.0
                elif on and self._mode == "wired":
                    reads.append("cost_mult")
                    cm = float(prm.get("cost_mult", 0.5))
                    y = 1.0 - 0.5 * on
                    vol = cm * sc
                else:
                    y = 1.0 - 0.5 * on
                    vol = 10.0 if on else 0.0
                out.append(Outcomes({"y": {"g0": y}}, {}, {"vol": vol},
                                    meta={"age_shares": {"g0": 1.0}, "param_reads": {"build": reads, "read_outcomes": []}}))
            return out

    th_cd = load_thresholds()
    a6_w = check_a6(_CdStub("wired"), b_cd, {}, np.random.default_rng(2), n_bases=3, min_moving=2, th=th_cd)
    a6_d = check_a6(_CdStub("dead_param"), b_cd, {}, np.random.default_rng(2), n_bases=3, min_moving=2, th=th_cd)
    a6_o = check_a6(_CdStub("dead_out"), b_cd, {}, np.random.default_rng(2), n_bases=3, min_moving=2, th=th_cd)
    ok(abs(log_mid - math.sqrt(0.0005 * 0.5)) < 1e-12 and abs(lin_mid - 0.55) < 1e-12
       and log_pts[1] < 0.01 and a6_w["status"] == "pass" and a6_d["status"] == "fail"
       and a6_o["status"] == "fail" and any("vol" in p for p in a6_o["problems"]),
       f"BE_A6_COST_LOG log-space sampling, active-mode A6 for cost_driver params, dead param & dead output catch "
       f"(wired={a6_w['status']}, dead_p={a6_d['status']}, dead_out={a6_o['status']})")

    # BE_A6_LAZY: non-decision-input parameter gated by an active intervention branch passes A6 via Pass 2
    # lazy active-mode fallback while a genuinely dead parameter still fails.
    b_lz = {
        "id": "stub_lz", "kind": "closed_form",
        "request": {
            "components": {"mode": {"values": ["none", "screen"], "absent": "none"}},
            "unit_attributes": {"x": {"type": "continuous", "range": [0.1, 1.0]}},
            "outputs": {"per_capita": ["y"], "windows": {}, "cost_drivers": []},
            "age_groups": ["g0"],
            "parameters": [{"name": "gated_p", "kind": "fixed", "value": 0.5, "lo": 0.1, "hi": 0.9, "source": "t"}],
            "stochastic": False,
        },
    }

    class _LazyStub:
        kind = "closed_form"

        def __init__(self, mode: str):
            self._mode = mode

        def run_many(self, cases):
            out = []
            for prm, u, pk, _h, _s in cases:
                x = float(u.attributes["x"])
                on = pk.components.get("mode") == "screen"
                reads = []
                if on and self._mode != "dead":
                    reads.append("gated_p")
                    gp = float(prm.get("gated_p", 0.5))
                    val = x * (0.2 + 0.6 * gp)
                else:
                    val = x * (0.5 if on else 1.0)
                out.append(Outcomes({"y": {"g0": val}}, {}, {},
                                    meta={"age_shares": {"g0": 1.0}, "param_reads": {"build": reads, "read_outcomes": []}}))
            return out

    a6_lg = check_a6(_LazyStub("good"), b_lz, {}, np.random.default_rng(4), n_bases=3, min_moving=2, th=th_cd)
    a6_ld = check_a6(_LazyStub("dead"), b_lz, {}, np.random.default_rng(4), n_bases=3, min_moving=2, th=th_cd)
    ok(a6_lg["status"] == "pass" and a6_ld["status"] == "fail",
       f"BE_A6_LAZY active-mode fallback passes intervention-gated param while dead fails "
       f"(good={a6_lg['status']}, dead={a6_ld['status']})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
